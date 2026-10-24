"""Battle.net-Shop lesen: Listen, Suche, Einzelheiten. Ohne Anmeldung.

Eine JSON-Schnittstelle für den Katalog gibt es nicht (Stand 2026-10). Der Shop ist eine
Next.js-Seite; seine Daten stecken als „Flight“-Zeilen in der Seite. Mit dem Kopf „RSC: 1“
liefert er nur diese Zeilen (rund 200 KB statt 4 bis 6 MB HTML):

    eu.shop.battle.net/de-de                  Startseite: Spieleliste im Menü, empfohlene Karten
    eu.shop.battle.net/de-de/family/<slug>    alle Editionen eines Spiels (Karten mit Preis, Bild)
    eu.shop.battle.net/de-de/product/<slug>   Einzelheiten: Texte, Bilder, Editionen, appGameCode, titleId

Ohne das Cookie autoLoginChecked-next leitet der Shop jede Seite erst zur Anmeldung um (307).
Karten wie store.steam.SteamStore.card, mit store "battlenet"; id = Shop-Slug (eine Karte je
Produktseite, Preis der günstigsten Edition). Zwischenspeicher wie bei Epic: ohne Netz der
letzte Stand, sonst StoreError mit deutschem Satz.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import cache
from .steam import DETAIL_TTL, LIST_TTL, StoreError, clip, strip_html

SHOP = "https://eu.shop.battle.net"
LOCALE = "de-de"
HOME = "%s/%s" % (SHOP, LOCALE)
FAMILY = HOME + "/family/%s"
PRODUCT = HOME + "/product/%s"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
TIMEOUT_S = 20.0
PAGE = 48
# Familien- und Produktseiten ändern sich selten; die Startseite (Angebote) öfter
CATALOG_TTL = 2 * 3600
MEMO_S = 60
WORKERS = 6
NOT_REACHABLE = "Der Battle.net-Shop ist gerade nicht erreichbar."
NO_GAME = "Zu diesem Spiel hat der Battle.net-Shop keine Angaben."
FREE_TEXT = "kostenlos spielen"
# Bildgrößen über das Bildernetz von Blizzard (Originale sind oft 1920x1080 und mehrere MB groß)
CARD_WIDTH, HERO_WIDTH, SHOT_WIDTH = 800, 1920, 1280
_RESIZABLE = ("https://blz-contentstack-images.akamaized.net/", "https://images.blz-contentstack.com/")

TABS = [("bnet_games", "Spiele"), ("bnet_specials", "Angebote"), ("bnet_free", "Kostenlos spielbar"),
        ("bnet_featured", "Empfohlen")]
# Genres = die Shop-Kategorien (Kartenfeld categoryId), unter denen Spiele stehen. Alles andere
# (rund 40 Kategorien: in-game-content, mounts, card-packs, cod-in-game-content, game-services, …)
# sind Ingame-Käufe, Dienste, Abos oder Fanartikel und fehlen im Store (Stand 2026-10-22).
GENRES = (("action-rpg", "Action-Rollenspiel"), ("massively-multiplayer-rpg", "Online-Rollenspiel"),
          ("action-shooter", "Shooter"), ("real-time-strategy", "Echtzeitstrategie"),
          ("action-strategy", "Action-Strategie"), ("team-based-action-game", "Teambasiertes Actionspiel"),
          ("strategy-card-game", "Kartenspiel"), ("moba", "MOBA"), ("action-adventure", "Actionabenteuer"),
          ("platformer", "Jump ’n’ Run"))
# Erweiterungen (Diablo III: Reaper of Souls) sind keine Ingame-Käufe
GAME_CATEGORIES = frozenset([key for key, _label in GENRES] + ["expansions"])
# Genre-Namen der Produktseiten und des Menüs (deutscher Shop) -> Kategorie
LABELS = {"action rpg": "action-rpg", "massively multiplayer rpg": "massively-multiplayer-rpg",
          "action shooter": "action-shooter", "echtzeitstrategie": "real-time-strategy",
          "action-strategie": "action-strategy", "teambasiertes actionspiel": "team-based-action-game",
          "strategisches kartenspiel": "strategy-card-game", "moba": "moba", "actionabenteuer": "action-adventure",
          "jump ’n’ run": "platformer", "jump 'n' run": "platformer", "plattformer": "platformer"}

# Bekannte Spiele: Launcher-Code (Battle.net.exe --exec="launch <Code>") -> Titel, titleId (aus
# account.battle.net/api/games-and-subs) und Agent-Kennung (product.db, --uid). Quellen: Playnite
# BattleNetLibrary (BattleNetGames.cs, 2026-01), Lutris (services/battlenet.py, util/battlenet),
# Shop-Produktseiten (appGameCode/titleId, 2026-10-22). Wo Shop und Playnite verschiedene Codes
# nennen, gilt der Shop (WAR1, W2BN, DRTL), außer bei der Arcade Collection (Shop „PR“, Playnite und
# Lutris „RTRO“). Ob jeder Code mit --exec="launch …" startet, ist unbestätigt. Fehlt die
# Agent-Kennung, ist sie unbekannt.
CODES = {
    "WoW": {"title": "World of Warcraft", "title_id": 5730135, "agent_uid": "wow"},
    "WoWC": {"title": "World of Warcraft Classic", "title_id": 5730135, "agent_uid": "wow_classic"},
    "WoWF": {"title": "World of Warcraft: Forever", "title_id": 0, "agent_uid": ""},
    "Fen": {"title": "Diablo IV", "title_id": 4613486, "agent_uid": "fenris"},
    "OSI": {"title": "Diablo II: Resurrected", "title_id": 5198665, "agent_uid": "osi"},
    "D3": {"title": "Diablo III", "title_id": 17459, "agent_uid": "diablo3"},
    "DRTL": {"title": "Diablo", "title_id": 1146246220, "agent_uid": ""},          # Playnite: „D1“
    "ANBS": {"title": "Diablo Immortal", "title_id": 1095647827, "agent_uid": "anbs"},
    "Pro": {"title": "Overwatch", "title_id": 5272175, "agent_uid": "prometheus"},
    "WTCG": {"title": "Hearthstone", "title_id": 1465140039, "agent_uid": "hs_beta"},
    "Hero": {"title": "Heroes of the Storm", "title_id": 1214607983, "agent_uid": "heroes"},
    "S2": {"title": "StarCraft II", "title_id": 21298, "agent_uid": "s2"},
    "S1": {"title": "StarCraft: Remastered", "title_id": 21297, "agent_uid": "s1"},
    "W3": {"title": "Warcraft III: Reforged", "title_id": 22323, "agent_uid": "w3"},
    "W1R": {"title": "Warcraft: Remastered", "title_id": 5714258, "agent_uid": "w1r"},
    "W2R": {"title": "Warcraft II: Remastered", "title_id": 5714514, "agent_uid": "w2r"},
    "WAR1": {"title": "Warcraft: Orcs & Humans", "title_id": 1463898673, "agent_uid": "w1"},   # Playnite: „W1“
    "W2BN": {"title": "Warcraft II: Battle.net Edition", "title_id": 1462911566, "agent_uid": "w2"},  # Playnite: „W2“
    "GRY": {"title": "Warcraft Rumble", "title_id": 4674137, "agent_uid": "gryphon"},
    "RTRO": {"title": "Blizzard Arcade Collection", "title_id": 1381257807, "agent_uid": "rtro"},
    "AUKS": {"title": "Call of Duty", "title_id": 1096108883, "agent_uid": "auks"},
    "FORE": {"title": "Call of Duty: Vanguard", "title_id": 1179603525, "agent_uid": "fore"},
    "ZEUS": {"title": "Call of Duty: Black Ops Cold War", "title_id": 1514493267, "agent_uid": "zeus"},
    "ODIN": {"title": "Call of Duty: Modern Warfare (2019)", "title_id": 1329875278, "agent_uid": "odin"},
    "LAZR": {"title": "Call of Duty: Modern Warfare 2 Campaign Remastered", "title_id": 1279351378, "agent_uid": "lazarus"},
    "VIPR": {"title": "Call of Duty: Black Ops 4", "title_id": 1447645266, "agent_uid": "viper"},
    "WLBY": {"title": "Crash Bandicoot 4: It's About Time", "title_id": 1464615513, "agent_uid": "wlby"},
    "ARIS": {"title": "DOOM: The Dark Ages", "title_id": 1095911763, "agent_uid": "aris"},
    "SCOR": {"title": "Sea of Thieves", "title_id": 1396920146, "agent_uid": "scorpio"},
    "ARK": {"title": "The Outer Worlds 2", "title_id": 4280907, "agent_uid": "arkansas"},
    "LBRA": {"title": "Tony Hawk's Pro Skater 3 + 4", "title_id": 1279414849, "agent_uid": "libra"},
    "AQUA": {"title": "Avowed", "title_id": 1095849281, "agent_uid": "aqua"},
    "URSA": {"title": "The Witcher 3: Wild Hunt — Remastered", "title_id": 1431458625, "agent_uid": ""},
}
# Shop-Slug -> Launcher-Code für Karten (die Listen nennen keinen Code, die Produktseiten schon).
# Gelesen von den Produktseiten am 2026-10-22; details() nimmt immer den Code der Seite. "" heißt:
# kein Spiel für die Battle.net-App (Diablo II von 2000 läuft über den alten Installer).
SLUG_CODES = {
    "world-of-warcraft-forever": "WoW", "world-of-warcraft-free-trial": "WoW", "world-of-warcraft-midnight": "WoW",
    "warcraft-rumble": "GRY", "warcraft-3-reforged": "W3", "warcraft-remastered-battle-chest": "W3",
    "warcraft-1-remastered": "W1R", "warcraft-2-remastered": "W2R", "warcraft-orcs-and-humans": "WAR1",
    "warcraft-2-battle-net-edition": "W2BN", "diablo-iv-lord-of-hatred": "Fen", "diablo-iv": "Fen",
    "diablo-immortal": "ANBS", "diablo-iii": "D3", "diablo-iii-ros": "D3", "diablo-iii-rise-of-the-necromancer": "D3",
    "diablo_ii_resurrected": "OSI", "diablo_ii_resurrected_reign_of_the_warlock": "OSI",
    "diablo-ii-lord-of-destruction": "OSI", "diablo-ii": "", "diablo": "DRTL", "overwatch": "Pro",
    "overwatch-invasion-bundle": "Pro", "hearthstone": "WTCG", "starcraft-ii": "S2", "starcraft-remastered": "S1",
    "starcraft-cartooned": "S1", "heroes-of-the-storm": "Hero", "call-of-duty-modern-warfare-4": "AUKS",
    "call-of-duty-black-ops-7": "AUKS", "call-of-duty-black-ops-6": "AUKS", "call-of-duty-warzone-2": "AUKS",
    "call-of-duty-modern-warfare-iii": "AUKS", "call-of-duty-modern-warfare-ii": "AUKS",
    "call-of-duty-vanguard": "FORE", "call-of-duty-black-ops-cold-war": "ZEUS", "call-of-duty-modern-warfare": "ODIN",
    "call-of-duty-modern-warfare-2-campaign-remastered": "LAZR", "call-of-duty-black-ops-4": "VIPR",
    "avowed": "AQUA", "blizzard-arcade-collection": "RTRO", "crash-bandicoot-4": "WLBY",
    "doom-the-dark-ages": "ARIS", "doom-the-dark-ages-revelations": "ARIS", "sea-of-thieves": "SCOR",
    "the-outer-worlds-2": "ARK", "tony-hawks-pro-skater-3-4": "LBRA", "the-witcher-3-wild-hunt-remastered": "URSA"}
# Familienseiten, auf denen alle Spiele denselben Code haben (für neue Slugs, die oben noch fehlen)
FAMILY_CODES = {"diablo-iv": "Fen", "overwatch": "Pro", "hearthstone": "WTCG", "heroes-of-the-storm": "Hero",
                "starcraft-ii": "S2", "diablo-iii": "D3", "diablo-ii": "OSI", "diablo-immortal": "ANBS",
                "warcraft-rumble": "GRY", "world-of-warcraft": "WoW", "call-of-duty": "AUKS",
                "call-of-duty-vanguard": "FORE", "call-of-duty-black-ops-cold-war": "ZEUS",
                "call-of-duty-mw": "ODIN", "call-of-duty-mw2cr": "LAZR", "call-of-duty-black-ops-4": "VIPR",
                "doom-the-dark-ages": "ARIS", "sea-of-thieves": "SCOR"}


def code_for(title_id) -> str:
    """Launcher-Code zu einer titleId (eigene Spiele) oder ""."""
    try:
        tid = int(title_id or 0)
    except (TypeError, ValueError):
        return ""
    return next((code for code, info in CODES.items() if tid and info["title_id"] == tid), "")


def _known_code(code: str, title_id: int = 0) -> str:
    """Code der Produktseite; ist er unbekannt, der zur titleId (die Arcade Collection meldet im Shop
    „PR“, Playnite startet sie seit Jahren mit „RTRO“)."""
    if not title_id or code in CODES:
        return code
    return code_for(title_id) or code


# ---------------------------------------------------------------------- Netz
def _get(url: str, timeout: float = TIMEOUT_S) -> str:
    """Seite als Text (Flight-Zeilen, notfalls HTML). Das Cookie spart die Umleitung zur Anmeldung;
    die Keksdose fängt eine trotzdem nötige Umleitung samt Rückkehr ab."""
    jar = http.cookiejar.CookieJar()
    jar.set_cookie(_cookie("autoLoginChecked-next", "1", urllib.parse.urlsplit(SHOP).hostname))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "RSC": "1", "Accept-Language": "de-DE,de;q=0.9",
                                               "Accept": "text/x-component, text/html;q=0.9, */*;q=0.5"})
    with opener.open(req, timeout=timeout) as resp:      # noqa: S310 (feste https-Adressen)
        return resp.read(16 * 1024 * 1024).decode("utf-8", "replace")


def _cookie(name: str, value: str, host: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(0, name, value, None, False, host, False, False, "/", True, True, None, False,
                                 None, None, {})


# ---------------------------------------------------------------------- Flight-Zeilen lesen
_ROW = re.compile(rb"([0-9a-f]{1,8}):")
_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)


def flight(text: str) -> dict:
    """{zeile: wert} aus der Antwort (Flight-Text oder HTML mit self.__next_f.push). Textzeilen
    („1b:T795,…“) haben eine Länge in Bytes und dürfen Zeilenumbrüche enthalten."""
    text = text or ""
    if text.lstrip()[:1] == "<":
        parts = []
        for m in _PUSH.finditer(text):
            try:
                arr = json.loads(m.group(1))
            except ValueError:
                continue
            if len(arr) > 1 and arr[0] == 1 and isinstance(arr[1], str):
                parts.append(arr[1])
        text = "".join(parts)
    raw = text.encode("utf-8")
    rows, i, n = {}, 0, len(raw)
    while i < n:
        m = _ROW.match(raw, i)
        if not m:
            j = raw.find(b"\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        rid, i = m.group(1).decode(), m.end()
        if raw[i:i + 1] == b"T":
            comma = raw.find(b",", i)
            try:
                length = int(raw[i + 1:comma], 16)
            except ValueError:
                break
            rows[rid] = raw[comma + 1:comma + 1 + length].decode("utf-8", "replace")
            i = comma + 1 + length
            continue
        j = raw.find(b"\n", i)
        j = n if j < 0 else j
        body, i = raw[i:j], j + 1
        if body[:1] in (b"[", b"{", b'"'):
            try:
                rows[rid] = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                pass
    return rows


def _resolve(rows: dict, value, depth: int = 0):
    """Verweise der Flight-Daten auflösen: „$1d“ (Zeile), „$7:props:a:b“ (Pfad darin; „props“ ist
    bei React-Elementen ["$", typ, schlüssel, props] der vierte Eintrag), „$undefined“, „$$“."""
    if depth > 50 or not (isinstance(value, str) and value.startswith("$")):
        return value
    if value.startswith("$$"):
        return value[1:]
    if value == "$undefined":
        return None
    m = re.fullmatch(r"\$([0-9a-f]+)((?::[^:]+)*)", value)
    if not m:
        return value
    cur = rows.get(m.group(1))
    for seg in [s for s in m.group(2).split(":") if s]:
        cur = _resolve(rows, cur, depth + 1)
        if isinstance(cur, list) and cur[:1] == ["$"] and seg in ("type", "key", "props"):
            cur = cur[{"type": 1, "key": 2, "props": 3}[seg]] if len(cur) > 3 else None
        elif isinstance(cur, list):
            cur = cur[int(seg)] if seg.isdigit() and int(seg) < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return _resolve(rows, cur, depth + 1)


def _deep(rows: dict, value, depth: int = 0):
    value = _resolve(rows, value)
    if depth > 30:
        return value
    if isinstance(value, dict):
        return {k: _deep(rows, v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(rows, v, depth + 1) for v in value]
    return value


def _walk(value, depth: int = 0):
    """Alle Objekte (dict) in den Zeilen, ohne Verweise aufzulösen."""
    if depth > 200:
        return
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v, depth + 1)


# ---------------------------------------------------------------------- Auszüge aus den Seiten
_CARD_KEYS = ("slug", "title", "productIds", "destination", "categoryId", "description", "marketingBadge",
              "priceLineOption", "priceLineTextOverride", "price", "image")


def page_cards(rows: dict) -> list[dict]:
    """Alle Shop-Karten einer Seite (Listen unter items/cards mit slug und price), in Seitenfolge,
    gekürzt auf die Felder, die card() braucht."""
    out = []
    for obj in (o for v in rows.values() for o in _walk(v)):
        for key in ("items", "cards"):
            items = _resolve(rows, obj.get(key))
            if not (isinstance(items, list) and items):
                continue
            for raw in items:
                raw = _resolve(rows, raw)
                if isinstance(raw, dict) and "slug" in raw and "price" in raw and "priceLineOption" in raw:
                    out.append({k: _deep(rows, raw.get(k)) for k in _CARD_KEYS})
    return out


def page_nav(rows: dict) -> list[dict]:
    """Die Spiele aus dem Menü des Shops: [{title, kind ("family"|"product"), slug, genre}]."""
    out, seen = [], set()
    for obj in (o for v in rows.values() for o in _walk(v)):
        nav = _resolve(rows, obj.get("navigation"))
        if not isinstance(nav, dict) or "primaryMenu" not in nav:
            continue
        groups = list(_resolve(rows, nav.get("primaryMenu")) or [])
        groups.append(_resolve(rows, nav.get("overflowMenu")) or {})
        for group in groups:
            group = _resolve(rows, group)
            for it in (_resolve(rows, group.get("items")) or []) if isinstance(group, dict) else []:
                it = _resolve(rows, it)
                dest = str((it or {}).get("destination") or "") if isinstance(it, dict) else ""
                m = re.match(r"^/(family|product)/([\w-]+)$", dest)
                if not m or dest in seen:
                    continue
                seen.add(dest)
                out.append({"title": _clean(it.get("text")), "kind": m.group(1), "slug": m.group(2),
                            "genre": _clean(it.get("category"))})
        break
    return out


def page_product(rows: dict) -> dict:
    """Einzelheiten einer Produktseite, gekürzt: {title, code, title_id, category, genre, developers,
    marketing, sections, gallery, editions, ld}. {} wenn die Seite kein Produkt zeigt."""
    page = state = None
    for obj in (o for v in rows.values() for o in _walk(v)):
        if page is None and "gallery" in obj and "titleId" in obj and "appGameCode" in obj:
            page = obj
        if state is None and isinstance(_resolve(rows, obj.get("preloadedState")), dict):
            state = (_resolve(rows, obj["preloadedState"]).get("product")) or None
    if page is None:
        return {}
    page = {k: _deep(rows, v) for k, v in page.items()
            if k in ("title", "slug", "category", "marketingText", "titleId", "appGameCode", "developers",
                     "publishers", "details", "gallery", "featureSections")}
    state = _deep(rows, state) if isinstance(state, dict) else {}
    editions = []
    for p in state.get("products") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        editions.append({"id": p.get("id"), "name": _clean(p.get("name")),
                         "price": (p.get("priceInfo") or {}).get("price") if isinstance(p.get("priceInfo"), dict) else None,
                         "free": bool(p.get("freeToPlay")), "coming_soon": bool(p.get("comingSoon")),
                         "available": p.get("available") is not False,
                         "category": str((p.get("analytics") or {}).get("category") or "") if isinstance(p.get("analytics"), dict) else ""})
    gallery = []
    for cat in page.get("gallery") or []:
        for it in (cat or {}).get("items") or [] if isinstance(cat, dict) else []:
            for media in (it or {}).get("media") or [] if isinstance(it, dict) else []:
                img = (media or {}).get("imageAsset") if isinstance(media, dict) else None
                if isinstance(img, dict) and img.get("url"):
                    gallery.append({"url": str(img["url"]), "w": _int(img.get("width")), "h": _int(img.get("height")),
                                    "video": bool(media.get("videoAsset"))})
    sections = []
    for sec in page.get("featureSections") or []:
        if not isinstance(sec, dict):
            continue
        feats = []
        for f in sec.get("features") or []:
            if isinstance(f, dict):
                img = f.get("image") if isinstance(f.get("image"), dict) else {}
                feats.append({"name": _clean(f.get("name")), "text": str(f.get("description") or ""),
                              "image": str(img.get("url") or ""), "w": _int(img.get("width")), "h": _int(img.get("height"))})
        sections.append({"title": _clean(sec.get("title")), "summary": str(sec.get("summary") or ""), "features": feats})
    details = page.get("details") if isinstance(page.get("details"), dict) else {}
    ld = {}
    for v in rows.values():
        if isinstance(v, str) and v.lstrip().startswith("{") and '"@type":"Product"' in v.replace(" ", ""):
            try:
                ld = json.loads(v)
            except ValueError:
                ld = {}
            break

    def names(items) -> list[str]:
        return [_clean((d.get("image") or {}).get("description")) for d in items or []
                if isinstance(d, dict) and isinstance(d.get("image"), dict) and (d["image"].get("description") or "").strip()]
    return {"title": _clean(page.get("title")), "slug": str(page.get("slug") or ""),
            "code": str(page.get("appGameCode") or ""), "title_id": _int(page.get("titleId")),
            "category": _clean(page.get("category")),
            "genre": _clean(((details.get("genre") or {}) if isinstance(details.get("genre"), dict) else {}).get("name")),
            "developers": names(page.get("developers")) or names(page.get("publishers")),
            "marketing": str(page.get("marketingText") or ""), "sections": sections, "gallery": gallery,
            "editions": editions,
            "ld": {"description": str(ld.get("description") or ""), "image": str(ld.get("image") or "")}}


# ---------------------------------------------------------------------- Karten
def _clean(text) -> str:
    """Titel ohne ®/™ und feste Leerzeichen („Diablo® IV: Lord of Hatred – …“)."""
    text = re.sub(r"[®™©]", "", str(text or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _base_title(title: str) -> str:
    """Titel ohne Edition: „Diablo IV: Lord of Hatred – Standard Edition“ -> „Diablo IV: Lord of Hatred“."""
    out = re.sub(r"\s+[–—-]\s+[^–—]*?Edition\s*$", "", title, flags=re.I).strip()
    return out or title


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _euro(text) -> str:
    return str(text or "").replace("\xa0", " ").strip()


def sized(url: str, width: int) -> str:
    """Bild verkleinert und als JPEG über die Bildparameter von Contentstack (Blizzards Bildernetz).
    Gemessen am 2026-10-22 bei 800 px Breite: ein Overwatch-PNG 54 KB statt 640 KB mit ?imwidth=800;
    AVIF-Bilder (Diablo III) verkleinert ?imwidth gar nicht. SVG und GIF bleiben, wie sie sind."""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    url = urllib.parse.quote(url, safe=":/?&=%()_-.~,+!*'@")
    if url.startswith(_RESIZABLE) and "?" not in url and not re.search(r"\.(svg|gif)$", url, re.I):
        return "%s?width=%d&format=jpg&quality=80" % (url, width)
    return url


def _money(price: dict) -> dict | None:
    """{"price", "original", "discount", "raw"} aus einem Preisfeld des Shops, None ohne Euro-Preis
    (Platin, Kugeln, „Mehr erfahren“, Abos „/Monat“)."""
    if not isinstance(price, dict) or price.get("virtualCurrency"):
        return None
    full, now = _euro(price.get("fullAmount")), _euro(price.get("discountAmount"))
    if not full.endswith("€") or (now and not now.endswith("€")):
        return None
    try:
        raw = float(price.get("raw") or 0)
    except (TypeError, ValueError):
        raw = 0.0
    if now and now != full:
        pct = _int(price.get("discountPercentage"))
        if not pct:
            try:
                f = float(full.replace("€", "").replace(".", "").replace(",", ".").strip())
                pct = round(100 - 100 * raw / f) if f and raw else 0
            except ValueError:
                pct = 0
        return {"price": now, "original": full, "discount": pct, "raw": raw}
    return {"price": full, "original": "", "discount": 0, "raw": raw}


def _is_free(raw: dict) -> bool:
    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
    text = str(raw.get("priceLineTextOverride") or price.get("labelFormat") or "").strip().lower()
    return not price.get("fullAmount") and text == FREE_TEXT


def card(raw: dict, family: str = "") -> dict | None:
    """Karte aus einer Shop-Karte; None für Ingame-Käufe, Dienste, Abos und Karten ohne Euro-Preis."""
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("slug") or "")
    cat = str(raw.get("categoryId") or "")
    if not re.fullmatch(r"[\w-]{1,120}", slug) or cat not in GAME_CATEGORIES \
            or not str(raw.get("destination") or "").startswith("/product/"):
        return None
    free = _is_free(raw)
    money = None if free else _money(raw.get("price"))
    if not free and money is None:
        return None
    code = SLUG_CODES[slug] if slug in SLUG_CODES else FAMILY_CODES.get(family, "")
    badge = raw.get("marketingBadge") if isinstance(raw.get("marketingBadge"), dict) else {}
    image = raw.get("image") if isinstance(raw.get("image"), dict) else {}
    return {"store": "battlenet", "id": slug, "title": _clean(raw.get("title")), "slug": slug,
            "image": sized(image.get("url"), CARD_WIDTH),
            "price": "Kostenlos" if free else money["price"], "discount": 0 if free else money["discount"],
            "original": "" if free else money["original"], "code": code,
            "title_id": CODES.get(code, {}).get("title_id", 0) if code else 0,
            "genre": cat, "badge": _clean(badge.get("text")), "raw_price": 0.0 if free else money["raw"]}


def merge(cards: list[dict]) -> list[dict]:
    """Eine Karte je Slug (Produktseite), in der Reihenfolge des ersten Auftretens. Bei mehreren
    Editionen zählt die günstigste (kostenlos vor bezahlt); der Titel verliert dann die Edition."""
    groups: dict[str, list[dict]] = {}
    for c in cards:
        if c:
            groups.setdefault(c["id"], []).append(c)
    out = []
    for slug, group in groups.items():
        lead = min(group, key=lambda c: (c["price"] != "Kostenlos", c.get("raw_price") or 0.0))
        c = dict(lead)
        if len({g["title"] for g in group}) > 1:
            c["title"] = _base_title(lead["title"])
        c["image"] = lead["image"] or next((g["image"] for g in group if g["image"]), "")
        c["code"] = next((g["code"] for g in group if g["code"]), "")
        c["title_id"] = next((g["title_id"] for g in group if g["title_id"]), 0)
        c["badge"] = next((g["badge"] for g in group if g["badge"]), "")
        out.append(c)
    return out


def _public(c: dict) -> dict:
    return {k: v for k, v in c.items() if k != "raw_price"}


# ---------------------------------------------------------------------- Shop
class BattlenetStore:
    def __init__(self, cache_dir: str, get=_get, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get
        self._clock = clock
        self._memo: tuple | None = None         # (Zeit, Katalog): Blättern liest nicht 30 Dateien je Seite

    # ------------------------------------------------------------------ Zwischenspeicher
    def _cached(self, key: str, fetch, ttl: float):
        return cache.cached(self.cache_dir, key, fetch, ttl, clock=self._clock, unreachable=NOT_REACHABLE)

    # ------------------------------------------------------------------ Seiten
    def _home(self) -> dict:
        def fetch():
            rows = flight(self._get(HOME))
            nav, cards = page_nav(rows), page_cards(rows)
            if not nav and not cards:
                raise ValueError("Startseite ohne Spiele")
            return {"nav": nav, "cards": cards}
        return self._cached("bnet-home", fetch, LIST_TTL)

    def _family(self, slug: str) -> dict:
        """{"cards": [Spiele-Karten der Seite], "all": Zahl aller Karten}. Nur Spiele werden
        gespeichert (eine Familie hat bis zu 120 Karten mit Packungen, Reittieren, Münzen).
        Manche Familien (starcraft-ii) zeigt noch der alte Shop ohne Daten in der Seite: all = 0."""
        def fetch():
            cards = page_cards(flight(self._get(FAMILY % slug)))
            return {"cards": [c for c in cards if str(c.get("categoryId") or "") in GAME_CATEGORIES], "all": len(cards)}
        return self._cached("bnet-family-" + slug, fetch, CATALOG_TTL)

    def _product(self, slug: str, ttl: float = DETAIL_TTL, empty_ok: bool = False) -> dict:
        """Gekürzte Produktseite (page_product). empty_ok: eine Seite ohne Produkt gilt als {} und
        bleibt im Zwischenspeicher, statt bei jedem Katalog neu geladen zu werden."""
        def fetch():
            try:
                text = self._get(PRODUCT % slug)
            except urllib.error.HTTPError as e:
                e.close()
                if e.code == 404:
                    return {}               # gibt es nicht: details() sagt das, statt „nicht erreichbar“
                raise
            data = page_product(flight(text))
            if not data and not empty_ok:
                raise ValueError("Produktseite ohne Produkt")
            return data
        return self._cached("bnet-product-" + slug, fetch, ttl)

    @staticmethod
    def product_card(slug: str, p: dict, genre_label: str = "") -> dict | None:
        """Karte aus einer Produktseite (Spiele im Menü ohne Familienseite)."""
        lead = _lead(p.get("editions") or [])
        if lead is None:
            return None
        image = _hero(p) or p.get("ld", {}).get("image", "")
        code = _known_code(p.get("code") or SLUG_CODES.get(slug, ""), p.get("title_id") or 0)
        genre = next((LABELS[g.lower()] for g in (p.get("genre"), p.get("category"), genre_label)
                      if g and g.lower() in LABELS), "")
        return {"store": "battlenet", "id": slug, "title": p.get("title") or lead["name"], "slug": slug,
                "image": sized(image, CARD_WIDTH), "price": lead["price"], "discount": lead["discount"],
                "original": lead["original"], "code": code,
                "title_id": p.get("title_id") or (CODES.get(code, {}).get("title_id", 0) if code else 0),
                "genre": genre, "badge": "", "raw_price": lead["raw"]}

    def catalog(self) -> dict:
        """{"games": [Karte] (Menüfolge, je Familie deren Reihenfolge), "featured": [Karte] (Startseite)}.
        Familien- und Produktseiten kommen parallel; fehlt eine, fehlen nur ihre Spiele."""
        memo = self._memo
        if memo and 0 <= self._clock() - memo[0] < MEMO_S:
            return memo[1]
        home = self._home()
        nav = [n for n in home.get("nav") or [] if isinstance(n, dict) and n.get("slug")]

        def one(entry: dict) -> list:
            slug = str(entry["slug"])
            try:
                if entry.get("kind") == "family":
                    fam = self._family(slug)
                    if fam.get("all"):
                        return [card(r, slug) for r in fam.get("cards") or []]
                # Produkt im Menü oder Familie im alten Shop: die Produktseite gleichen Namens
                c = self.product_card(slug, self._product(slug, CATALOG_TTL, empty_ok=True), entry.get("genre", ""))
                return [c] if c else []
            except StoreError:
                return []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            parts = list(pool.map(one, nav))
        games = merge([c for part in parts for c in part])
        featured = merge([card(r) for r in home.get("cards") or []])
        # Empfohlene Karten mit den Angaben aus dem ganzen Katalog (günstigste Edition, Code)
        known = {c["id"]: c for c in games}
        featured = [known.get(c["id"], c) for c in featured]
        for c in featured:
            if c["id"] not in known:
                games.append(c)
                known[c["id"]] = c
        data = {"games": games, "featured": featured}
        self._memo = (self._clock(), data)
        return data

    # ------------------------------------------------------------------ Listen
    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste oder der Suche: {items, total, more}. Der Katalog ist klein
        (rund 50 Spiele); gefiltert wird im Speicher."""
        page = max(0, int(page))
        if kind == "search":
            term = _clean(term)[:100].casefold()
            if not term:
                return {"items": [], "total": 0, "more": False}
        elif kind not in {k for k, _label in TABS}:
            return {"items": [], "total": 0, "more": False}
        data = self.catalog()
        items = data["featured"] if kind == "bnet_featured" else data["games"]
        if kind == "bnet_specials":
            items = sorted((c for c in items if c["discount"] > 0), key=lambda c: -c["discount"])
        elif kind == "bnet_free":
            items = [c for c in items if c["price"] == "Kostenlos"]
        elif kind == "search":
            items = [c for c in items if term in c["title"].casefold()]
        if genre:
            items = [c for c in items if c.get("genre") == genre]
        total = len(items)
        part = items[page * PAGE:(page + 1) * PAGE]
        return {"items": [_public(c) for c in part], "total": total, "more": (page + 1) * PAGE < total}

    # ------------------------------------------------------------------ Einzelheiten
    def details(self, item_id: str, card: dict | None = None) -> dict:
        slug = str(item_id or "")
        if not re.fullmatch(r"[\w-]{1,120}", slug):
            raise StoreError("Dieses Spiel gibt es nicht.")
        p = self._product(slug)
        if not isinstance(p, dict) or not p.get("title"):
            raise StoreError(NO_GAME)
        lead = _lead(p.get("editions") or [])
        code = _known_code(p.get("code") or SLUG_CODES.get(slug, "") or (card or {}).get("code", ""), p.get("title_id") or 0)
        hero = _hero(p)
        shots = _screenshots(p)
        texts = [strip_html(p.get("marketing") or "")]
        for sec in p.get("sections") or []:
            head = "\n".join(x for x in (sec.get("title"), strip_html(sec.get("summary") or "")) if x)
            body = ["%s\n%s" % (f["name"], strip_html(f["text"])) if f.get("name") else strip_html(f["text"])
                    for f in sec.get("features") or [] if f.get("text") or f.get("name")]
            texts.append("\n\n".join(x for x in [head] + body if x))
        about = "\n\n".join(t for t in texts if t).strip()
        # Kurztext: Werbezeile, sonst der erste Abschnitt; die Beschreibung für Suchmaschinen (ld)
        # ist manchmal englisch (StarCraft II) und mitten im Wort abgeschnitten (Overwatch)
        short = strip_html(p.get("marketing") or "") \
            or next((strip_html(x) for sec in p.get("sections") or []
                     for x in [sec.get("summary") or ""] + [f.get("text") or "" for f in sec.get("features") or []] if strip_html(x)), "") \
            or re.sub(r"\s*(\.\.\.|…)$", "", strip_html(p.get("ld", {}).get("description") or ""))
        genre = p.get("genre") or p.get("category") or ""
        genre = dict(GENRES).get(LABELS.get(genre.lower(), ""), genre)
        out = {
            "store": "battlenet", "id": slug, "type": "game", "title": p["title"], "slug": slug,
            "short": clip(short), "about": about[:6000],
            "image": (card or {}).get("image") or sized(hero or p.get("ld", {}).get("image", ""), CARD_WIDTH),
            "hero": sized(hero or p.get("ld", {}).get("image", ""), HERO_WIDTH),
            "screenshots": shots[:12], "release": "",
            "developers": list(dict.fromkeys(p.get("developers") or []))[:3],
            "genres": [genre] if genre else [],
            "metacritic": 0, "controller": "", "deck": "", "protondb": "", "linux": False, "windows": True,
            # Vorverkauf (Badge „Zum Vorverkauf“ auf der Karte): kaufbar, aber noch nicht spielbar
            "installable": bool(code), "size_gb": 0.0,
            "coming_soon": bool(lead and lead["coming_soon"]) or "vorverkauf" in str((card or {}).get("badge") or "").lower(),
            "url": PRODUCT % slug, "code": code,
            "title_id": p.get("title_id") or (CODES.get(code, {}).get("title_id", 0) if code else 0),
        }
        card = card or {}
        if card.get("price"):
            out.update(price=card["price"], original=card.get("original", ""), discount=int(card.get("discount") or 0))
        elif lead:
            out.update(price=lead["price"], original=lead["original"], discount=lead["discount"])
        else:
            out.update(price="", original="", discount=0)
        out["free"] = out["price"] == "Kostenlos"
        out["buyable"] = bool(out["price"]) and bool(lead and lead["available"])
        return out


def _lead(editions: list) -> dict | None:
    """Die günstigste Edition mit Euro-Preis (kostenlos spielbar zuerst): {name, price, original,
    discount, raw, coming_soon, available}."""
    best = None
    for e in editions:
        if not isinstance(e, dict):
            continue
        if e.get("free"):
            cand = {"price": "Kostenlos", "original": "", "discount": 0, "raw": -1.0}
        else:
            cand = _money(e.get("price"))
            if cand is None:
                continue
        cand.update(name=e.get("name") or "", coming_soon=bool(e.get("coming_soon")), available=bool(e.get("available", True)))
        if best is None or cand["raw"] < best["raw"]:
            best = cand
    if best is not None and best["raw"] < 0:
        best["raw"] = 0.0
    return best


def _landscape(img: dict, min_width: int) -> bool:
    return bool(img.get("url")) and img.get("w", 0) >= min_width and img.get("w", 0) > img.get("h", 0)


def _hero(p: dict) -> str:
    """Großes Querformat: erstes breites Bild der Galerie (kein Video), sonst des größten Abschnitts,
    sonst das Vorschaubild eines Videos."""
    gallery = [g for g in p.get("gallery") or [] if isinstance(g, dict)]
    pick = next((g["url"] for g in gallery if not g.get("video") and _landscape(g, 1280)), "")
    if pick:
        return pick
    feats = [{"url": f["image"], "w": f.get("w", 0), "h": f.get("h", 0)}
             for sec in p.get("sections") or [] for f in sec.get("features") or [] if f.get("image")]
    feats = sorted((f for f in feats if _landscape(f, 1280)), key=lambda f: -f["w"])
    if feats:
        return feats[0]["url"]
    return next((g["url"] for g in gallery if _landscape(g, 640)), "")


def _screenshots(p: dict) -> list[str]:
    urls = [g["url"] for g in p.get("gallery") or [] if isinstance(g, dict) and not g.get("video") and _landscape(g, 640)]
    urls += [f["image"] for sec in p.get("sections") or [] for f in sec.get("features") or []
             if f.get("image") and _landscape({"url": f["image"], "w": f.get("w", 0), "h": f.get("h", 0)}, 640)]
    return [sized(u, SHOT_WIDTH) for u in dict.fromkeys(urls)]


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label in GENRES]
