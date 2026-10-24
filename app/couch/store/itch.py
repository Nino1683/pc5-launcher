"""itch.io-Shop lesen (Bauplan E100): Listen, Karten, Einzelheiten. Ohne Konto.

Alle Webseiten von itch.io liegen hinter einer Cloudflare-Prüfung (urllib bekommt 403 mit
cf-mitigated, live 2026-10-23). Die wird nicht umgangen. Ohne Prüfung erreichbar sind die RSS-Feeds
der Stöberseiten:
    https://itch.io/games/<abschnitt>/<genre>/platform-windows[/<tag>].xml?page=N
    36 Einträge je Seite: Titel, Preis mit Währung, Rabatt, Plattformen, Cover 315x250, Kurztext, Daten
Pfadfolge (live 2026-10-23): Abschnitt/Plattform/Genre leitet itch.io per 301 auf Abschnitt/Genre/
Plattform um, die kanonische Folge, die hier gebaut wird (sie direkt abzufragen bekam 429: insoweit
unbestätigt). Genre ohne Abschnitt und Tags hinter der Plattform antworteten direkt mit 200.
Nach wenigen RSS-Anfragen kommt ein 429 (live: die 5. in 30 s; die 8. in rund 100 s trotz mindestens
10 s Abstand). Darum eine Drossel für den ganzen Prozess (GATE), kräftig zwischenspeichern
(store/cache.py) und bei 429 den letzten Stand.
Eine Suche gibt es hier nicht (nur mit Anmeldung über die API, itch_account.search, sonst im Browser).

Karten-ID: "<benutzer>/<slug>" aus https://<benutzer>.itch.io/<slug>. Karten wie gog.GogStore.card,
mit store "itch". Einzelheiten kommen aus den gemerkten Einträgen der Listen (remember) bzw. aus den
Daten von owned-keys (item_from_game). Alles hier blockiert und gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from . import cache
from .steam import LIST_TTL, StoreError, clip, strip_html

FEED = "https://itch.io/games/%s.xml"
USER_AGENT = "PC5-Launcher/0.1 (itch.io)"
TIMEOUT_S = 15.0
PAGE = 36
PLATFORM = "platform-windows"        # die Konsole spielt Windows-Spiele (Proton) und zeigt nur sie
WEB_GAP_S = 6.0                      # Mindestabstand zweier Anfragen an itch.io-Webseiten (E100)
REST_S = 120.0                       # nach einem 429 so lange gar nicht fragen
MAX_WAIT_S = 20.0                    # länger wartet keine Anfrage auf ihren Platz in der Drossel
MAX_FEED_BYTES = 4 * 1024 * 1024
UNREACHABLE = "itch.io ist gerade nicht erreichbar."
BRAKES = "itch.io bremst gerade, versuch es gleich noch mal."

# Abschnitte: (Reiter-ID, Titel, Pfadteil)
SECTIONS = (("itch_popular", "Beliebt", "top-sellers"),
            ("itch_new", "Neu und beliebt", "new-and-popular"),
            ("itch_rated", "Top bewertet", "top-rated"),
            ("itch_free", "Kostenlos", "free"),
            ("itch_sale", "Angebote", "on-sale"))
# Genres von itch.io als Pfadteil (genre-…); Horror ist dort nur ein Tag (tag-…)
GENRES = (("action", "Action", "genre-action"), ("adventure", "Abenteuer", "genre-adventure"),
          ("platformer", "Plattformer", "genre-platformer"), ("puzzle", "Puzzle", "genre-puzzle"),
          ("rpg", "Rollenspiel", "genre-rpg"), ("shooter", "Shooter", "genre-shooter"),
          ("simulation", "Simulation", "genre-simulation"), ("strategy", "Strategie", "genre-strategy"),
          ("survival", "Überleben", "genre-survival"), ("visual-novel", "Visual Novel", "genre-visual-novel"),
          ("horror", "Horror", "tag-horror"), ("racing", "Rennspiel", "genre-racing"),
          ("fighting", "Kampfspiel", "genre-fighting"), ("card-game", "Kartenspiel", "genre-card-game"),
          ("rhythm", "Rhythmus", "genre-rhythm"))
# Genre-Namen, wie sie in den eckigen Klammern der RSS-Titel stehen („… [Free] [Visual Novel] [Windows]“)
GENRE_NAMES = {"action": "Action", "adventure": "Abenteuer", "platformer": "Plattformer", "puzzle": "Puzzle",
               "role playing": "Rollenspiel", "shooter": "Shooter", "simulation": "Simulation",
               "strategy": "Strategie", "survival": "Überleben", "visual novel": "Visual Novel",
               "racing": "Rennspiel", "fighting": "Kampfspiel", "card game": "Kartenspiel", "rhythm": "Rhythmus",
               "sports": "Sport", "educational": "Lernspiel", "interactive fiction": "Interaktive Geschichte"}
CURRENCIES = {"USD": "US$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "AU$", "JPY": "¥", "CHF": "CHF"}
API_ONLY = ("game_id", "pwyw")       # Felder, die nur die API kennt (remember behält sie)

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}/[a-z0-9][a-z0-9_-]{0,127}$")
_GAME_URL = re.compile(r"^https?://([a-z0-9][a-z0-9_-]{0,63})\.itch\.io/([a-z0-9][a-z0-9_-]{0,127})/?$")


# ------------------------------------------------------------------ Karten-ID

def id_from_url(url) -> str:
    """"<benutzer>/<slug>" aus https://<benutzer>.itch.io/<slug>; "" für alles andere (fremde Domains,
    Unterseiten, Profilseiten)."""
    text = str(url or "").strip().split("#", 1)[0].split("?", 1)[0].lower()
    m = _GAME_URL.match(text)
    return "%s/%s" % m.groups() if m and m.group(1) != "www" else ""


def valid_id(game_id) -> bool:
    return isinstance(game_id, str) and bool(_ID.match(game_id))


def url_of(game_id) -> str:
    """Spielseite zur Karten-ID ("" bei ungültiger ID)."""
    if not valid_id(game_id):
        return ""
    user, slug = game_id.split("/", 1)
    return "https://%s.itch.io/%s" % (user, slug)


# ------------------------------------------------------------------ Drossel

class Throttled(StoreError):
    """itch.io bremst (429) oder die Drossel lässt gerade keine Anfrage durch."""


class Gate:
    """Prozessweite Drossel für itch.io-Webanfragen (E100): eine zur Zeit, Abstand gap; nach einem
    429 ruht sie rest Sekunden. Wer länger als max_wait auf seinen Platz warten müsste, bekommt
    Throttled (der Zwischenspeicher zeigt dann den letzten Stand)."""

    def __init__(self, gap: float = WEB_GAP_S, rest: float = REST_S, max_wait: float = MAX_WAIT_S,
                 sleep=time.sleep, monotonic=time.monotonic):
        self.gap, self.rest_s, self.max_wait = gap, rest, max_wait
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = threading.Lock()
        self._last = None
        self._rest_until = 0.0

    def resting(self) -> float:
        """Sekunden, die die Drossel nach einem 429 noch ruht (0 = frei)."""
        return max(0.0, self._rest_until - self._monotonic())

    def wait(self) -> None:
        if self.resting() > 0:
            raise Throttled(BRAKES)
        if not self._lock.acquire(timeout=self.max_wait):
            raise Throttled(BRAKES)
        try:
            if self.resting() > 0:
                raise Throttled(BRAKES)
            if self._last is not None:
                pause = self.gap - (self._monotonic() - self._last)
                if pause > self.max_wait:
                    raise Throttled(BRAKES)
                if pause > 0:
                    self._sleep(pause)
            self._last = self._monotonic()
        finally:
            self._lock.release()

    def hit_limit(self) -> None:
        """itch.io hat mit 429 geantwortet."""
        self._rest_until = self._monotonic() + self.rest_s


GATE = Gate()


def _get(url: str, timeout: float = TIMEOUT_S) -> bytes:
    """Ein RSS-Feed von itch.io (Rohdaten). Wirft Throttled bei 429, StoreError bei allem anderen
    (Cloudflare-403, HTML-Fehlerseiten zählen wie kein Netz)."""
    GATE.wait()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (feste https-Adressen)
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            body = resp.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 429:
            GATE.hit_limit()
            raise Throttled(BRAKES) from e
        raise StoreError(UNREACHABLE) from e
    if len(body) > MAX_FEED_BYTES or "xml" not in ctype:
        raise StoreError(UNREACHABLE)
    return body


# ------------------------------------------------------------------ RSS lesen

def _date(text: str) -> str:
    """RFC-822-Datum der Feeds als ISO-Tag ("" ohne Datum)."""
    try:
        return email.utils.parsedate_to_datetime(text).date().isoformat() if text else ""
    except (TypeError, ValueError, IndexError):
        return ""


def _brackets(title: str) -> list:
    return re.findall(r"\[([^\]]*)\]", title or "")


def _plain(title: str) -> str:
    """Titel ohne die angehängten Marken („… [Free] [Action] [Windows]“)."""
    return re.sub(r"(\s*\[[^\]]*\])+\s*$", "", title or "").strip()


def _genres(title: str) -> list:
    out = []
    for mark in _brackets(title):
        label = GENRE_NAMES.get(mark.strip().casefold())
        if label and label not in out:
            out.append(label)
    return out


def _is_gif(url: str) -> bool:
    return str(url or "").split("?", 1)[0].lower().endswith(".gif")


def cover_of(url, still="") -> str:
    """Cover-Adresse, wie geliefert (Größe und Signatur gehören zusammen, nichts umbauen). GIFs nur
    über die stille Fassung (still_cover_url), sonst gar nicht: die Bildpipeline kann nur JPEG/PNG."""
    for candidate in (url, still):
        candidate = str(candidate or "").strip()
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith("https://") and not _is_gif(candidate):
            return candidate
    return ""


def platforms_of(obj) -> dict:
    """{windows, linux, osx} aus einem Spiel bzw. Upload der API, alt (traits [p_windows, …]) oder
    v2 (platforms {windows: "all"|"386"|"amd64"}), oder aus einem RSS-Eintrag ({windows: True})."""
    obj = obj if isinstance(obj, dict) else {}
    plats = obj.get("platforms")
    traits = obj.get("traits")
    out = {"windows": False, "linux": False, "osx": False}
    if isinstance(plats, dict):
        for key in out:
            out[key] = bool(plats.get(key))
    if isinstance(traits, list):
        for key in out:
            out[key] = out[key] or ("p_" + key) in traits
    return out


def parse_feed(body) -> dict:
    """RSS einer Stöberseite -> {"items": [eintrag], "count": n}. count zählt alle Einträge der Seite
    (auch die auf fremden Domains, die wegfallen): Daran hängt, ob es weitergeht. ValueError für alles,
    was kein RSS ist (HTML-Fehlerseite, Cloudflare), das zählt wie kein Netz."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError("kein RSS")
    low = bytes(body[:8192]).lower()
    # Feeds von itch.io kommen ohne DTD; Entitäten aus dem Netz nie auflösen
    if b"<!doctype" in low or b"<!entity" in bytes(body).lower():
        raise ValueError("kein RSS")
    try:
        root = ET.fromstring(bytes(body))
    except ET.ParseError as e:
        raise ValueError("kein RSS") from e
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("kein RSS")
    nodes = channel.findall("item")
    items = [it for it in (_item(n) for n in nodes) if it]
    return {"items": items, "count": len(nodes)}


def _item(node) -> dict | None:
    def text(tag: str) -> str:
        el = node.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""
    gid = id_from_url(text("link")) or id_from_url(text("guid"))
    if not gid:
        return None                                    # fremde Domain oder keine Spielseite
    raw_title = text("title")
    plats = {}
    pnode = node.find("platforms")
    if pnode is not None:
        for child in pnode:
            plats[child.tag] = (child.text or "").strip().lower() == "yes"
    # Ist ein Spiel auch im Browser spielbar, nennt <platforms> nur <html> (live 2026-10-23: „Troll the
    # King [Free] [Windows] [macOS] [Linux]“ mit <html>yes</html>); die Marken im Titel nennen die Downloads
    marks = {m.strip().casefold() for m in _brackets(raw_title)}
    for key, mark in (("windows", "windows"), ("linux", "linux"), ("osx", "macos"), ("android", "android")):
        plats[key] = bool(plats.get(key)) or mark in marks
    return {"id": gid, "title": text("plainTitle") or _plain(raw_title), "image": text("imageurl"),
            "price": text("price"), "currency": text("currency").upper(), "discount": text("discountpercent"),
            "full": text("fullPrice"), "sale_ends": _date(text("saleends")),
            "text": strip_html(text("description")), "genres": _genres(raw_title),
            "created": _date(text("createDate") or text("pubDate")), "updated": _date(text("updateDate")),
            "platforms": {k: bool(plats.get(k)) for k in ("windows", "linux", "osx", "android", "html")}}


def item_from_game(game) -> dict | None:
    """Ein Spiel der API (owned-keys, Suche) in derselben Form wie ein RSS-Eintrag. Dazu game_id (die
    Nummer bei itch.io: ein kostenloses Spiel lässt sich damit auch ohne Schlüssel laden) und pwyw
    („Zahl, was du willst“: Mindestpreis 0, aber kaufbar), beides nur, wenn die API es nennt (E100)."""
    if not isinstance(game, dict):
        return None
    gid = id_from_url(game.get("url"))
    if not gid:
        return None
    try:
        cents = int(game.get("min_price") or 0)
    except (TypeError, ValueError):
        cents = 0
    raw = game.get("id")
    number = int(raw) if isinstance(raw, (int, str)) and not isinstance(raw, bool) \
        and re.fullmatch(r"[0-9]{1,15}", str(raw)) else 0
    plats = platforms_of(game)
    sale = game.get("sale") if isinstance(game.get("sale"), dict) else {}
    user = game.get("user") if isinstance(game.get("user"), dict) else {}
    traits = game.get("traits") if isinstance(game.get("traits"), list) else []
    # v1: traits [can_be_bought], v2: Feld can_be_bought (go-itchio liest beides)
    buyable = "can_be_bought" in traits or game.get("can_be_bought") is True
    extra = {"game_id": number} if number > 0 else {}
    if cents <= 0 and buyable:
        extra["pwyw"] = True
    return {**extra, "id": gid, "title": str(game.get("title") or ""),
            "image": cover_of(game.get("cover_url"), game.get("still_cover_url")),
            "price": "%d.%02d" % divmod(max(0, cents), 100), "currency": str(game.get("currency") or "USD").upper(),
            "discount": str(sale.get("rate") or ""), "full": "", "sale_ends": "",
            "text": strip_html(str(game.get("short_text") or "")), "genres": [],
            "created": str(game.get("published_at") or game.get("created_at") or "")[:10], "updated": "",
            "platforms": {**plats, "android": False, "html": str(game.get("type") or "") == "html"},
            "developer": str(user.get("display_name") or user.get("username") or "")}


def _cents(text) -> int | None:
    """„$4.99“, „£5.39“, „4,99 €“, „1,234.00“ -> Cent; None ohne Zahl."""
    digits = re.sub(r"[^0-9.,]", "", str(text or ""))
    if not digits:
        return None
    m = re.match(r"^(.*?)[.,](\d{1,2})$", digits)
    whole, frac = (m.group(1), m.group(2)) if m else (digits, "0")
    whole = re.sub(r"[.,]", "", whole) or "0"
    try:
        return int(whole) * 100 + int(frac.ljust(2, "0"))
    except ValueError:
        return None


def money(cents: int, currency: str = "USD") -> str:
    """Preis auf Deutsch: „4,99 US$“, „4,99 €“, „5,39 £“."""
    text = ("%.2f" % (int(cents) / 100.0)).replace(".", ",")
    cur = str(currency or "USD").upper()
    return text + " " + CURRENCIES.get(cur, cur)


# ------------------------------------------------------------------ Pfade

def feed_path(kind: str, genre: str = "") -> str:
    """Pfad hinter /games/ für einen Abschnitt (und ein Genre); "" für unbekannte Abschnitte."""
    section = next((p for key, _t, p in SECTIONS if key == kind), None)
    if section is None:
        return ""
    extra = next((p for key, _l, p in GENRES if key == genre), "")
    parts = [section]
    if extra.startswith("genre-"):
        parts.append(extra)
    parts.append(PLATFORM)
    if extra.startswith("tag-"):
        parts.append(extra)
    return "/".join(parts)


class ItchStore:
    def __init__(self, cache_dir: str, get=None, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get or _get
        self._clock = clock

    def _cache(self, key: str, url: str, ttl: float):
        try:
            return cache.cached(self.cache_dir, key, lambda: parse_feed(self._get(url)), ttl, clock=self._clock,
                                unreachable=UNREACHABLE)
        except StoreError as e:
            if isinstance(e.__cause__, Throttled):
                raise StoreError(BRAKES) from e.__cause__
            raise

    # ------------------------------------------------------------ Karten
    @staticmethod
    def card(item: dict) -> dict:
        item = item if isinstance(item, dict) else {}
        cents, full = _cents(item.get("price")), _cents(item.get("full"))
        currency = str(item.get("currency") or "USD")
        try:
            discount = max(0, min(100, int(str(item.get("discount") or "0").strip() or 0)))
        except ValueError:
            discount = 0
        free = cents == 0
        plats = item.get("platforms") if isinstance(item.get("platforms"), dict) else {}
        gid = str(item.get("id") or "")
        return {"store": "itch", "id": gid, "title": str(item.get("title") or ""), "slug": gid,
                "image": cover_of(item.get("image")),
                "price": "Kostenlos" if free else (money(cents, currency) if cents is not None else ""),
                "discount": discount if (discount and full) else 0,
                "original": money(full, currency) if (discount and full and full > (cents or 0)) else "",
                "free": free, "platforms": {k: bool(plats.get(k)) for k in ("windows", "linux", "osx")}}

    def card_from_game(self, game: dict) -> dict | None:
        """Karte zu einem Spiel der API (owned-keys, Suche); merkt den Eintrag für details()."""
        item = item_from_game(game)
        if item is None:
            return None
        self.remember([item])
        return self.card(item)

    # ------------------------------------------------------------ Gedächtnis der Einträge
    def _item_path(self, game_id: str) -> str:
        # Schlüssel über einen Hash: "a_b/c" und "a/b_c" ergäben sonst dieselbe Datei
        return cache.path_for(self.cache_dir, "itch-game-" + hashlib.sha1(game_id.encode()).hexdigest()[:24])

    def remember(self, items) -> None:
        """Einträge (aus Listen oder owned-keys) für details() merken: je Karten-ID eine Datei; frische
        Einträge (jünger als LIST_TTL) bleiben, wie sie sind. Nur die API kennt game_id und pwyw (die
        Feeds nicht): Ein Eintrag aus einem Feed behält sie, und ein Eintrag der API (mit game_id)
        trägt sie auch in einen frischen Eintrag ein (E100: kostenlose Suchtreffer lassen sich sonst
        nicht laden)."""
        now = self._clock()
        for it in items or []:
            if not isinstance(it, dict) or not valid_id(it.get("id")):
                continue
            path = self._item_path(it["id"])
            try:
                fresh = now - os.path.getmtime(path) < LIST_TTL
            except OSError:
                fresh = False
            api = "game_id" in it                   # aus der API: ihre Angaben gelten
            old = self.remembered(it["id"]) or {}
            if fresh:
                if api and old and any(old.get(k) != it.get(k) for k in API_ONLY):
                    merged = {k: v for k, v in old.items() if k not in API_ONLY}
                    merged.update({k: it[k] for k in API_ONLY if k in it})
                    cache.write(self.cache_dir, path, merged)
                continue
            if not api and any(k in old for k in API_ONLY):
                it = {**it, **{k: old[k] for k in API_ONLY if k in old}}
            cache.write(self.cache_dir, path, it)

    def remembered(self, game_id: str) -> dict | None:
        try:
            with open(self._item_path(game_id), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("id") == game_id else None

    # ------------------------------------------------------------ Listen
    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite eines Abschnitts: {items, total, more}. page zählt ab 0 (wie GogStore; die
        Oberfläche schickt 0, 1, …), der Feed ab 1. total ist geschätzt (der Feed nennt keine Zahl).
        Eine Suche (kind "search") gibt es hier nicht: leere Liste."""
        path = feed_path(kind, genre)
        if not path:
            return {"items": [], "total": 0, "more": False}
        page = max(0, int(page))
        url = FEED % path + ("?page=%d" % (page + 1) if page else "")
        data = self._cache(re.sub(r"[^\w]", "_", "itch-%s-%s-%d" % (kind, genre, page)), url, LIST_TTL)
        rows = data.get("items") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise StoreError("itch.io hat keine Liste geliefert.")
        rows = [r for r in rows if isinstance(r, dict) and valid_id(r.get("id"))]
        self.remember(rows)
        count = int(data.get("count") or len(rows))
        more = count >= PAGE
        return {"items": [self.card(r) for r in rows], "total": page * PAGE + count + (PAGE if more else 0),
                "more": more}

    # ------------------------------------------------------------ Einzelheiten
    def details(self, game_id: str, card: dict | None = None) -> dict:
        """Karte plus Einzelheiten. Quelle: der gemerkte Eintrag (Listen, owned-keys); die Karte der
        Oberfläche hat den aktuelleren Preis und gewinnt. StoreError, wenn nichts bekannt ist."""
        gid = str(game_id or "").strip().lower()
        if not valid_id(gid):
            raise StoreError("Dieses Spiel gibt es nicht.")
        item = self.remembered(gid)
        card = card if isinstance(card, dict) and str(card.get("id") or "").lower() == gid else None
        if item is None and card is None:
            raise StoreError("Zu diesem Spiel hat itch.io keine Angaben.")
        item = item or {"id": gid}
        out = self.card(item)
        for key in ("title", "image", "price", "original", "discount", "free", "platforms"):
            if card is not None and card.get(key) not in (None, "", {}):
                out[key] = card[key]
        plats = out.get("platforms") or {}
        web = bool((item.get("platforms") or {}).get("html"))
        text = str(item.get("text") or (card or {}).get("short") or "")
        user = gid.split("/", 1)[0]
        out.update({
            "type": "game", "short": clip(text), "about": text[:6000], "hero": out["image"], "screenshots": [],
            "release": str(item.get("created") or "")[:10], "developers": [str(item.get("developer") or user)],
            "genres": list(item.get("genres") or []), "metacritic": 0, "controller": "", "deck": "", "protondb": "",
            "windows": bool(plats.get("windows")), "linux": bool(plats.get("linux")), "osx": bool(plats.get("osx")),
            "web": web, "installable": bool(plats.get("windows") or plats.get("linux")), "coming_soon": False,
            "size_gb": 0.0, "url": url_of(gid), "drm_free": True, "sale_ends": str(item.get("sale_ends") or ""),
        })
        out["free"] = bool(out.get("free"))
        out["buyable"] = bool(out["price"]) and not out["free"]
        return out


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label, _p in GENRES]


def sections() -> list[dict]:
    return [{"id": key, "title": title} for key, title, _p in SECTIONS]
