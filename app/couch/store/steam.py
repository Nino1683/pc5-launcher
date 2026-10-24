"""Steam-Shop lesen (Bauplan E68): Listen, Suche, Einzelheiten, Steam-Deck- und ProtonDB-Einstufung.

Quellen sind die Schnittstellen, die die Shop-Seite selbst benutzt (ohne Anmeldung):
    /api/featuredcategories     Angebote, Topseller, Neuerscheinungen, Bald verfügbar
    /search/results             Listen und Suche (wie die Shop-Seite, E70)
    /api/appdetails             Einzelheiten eines Spiels (Preis, Texte, Bilder, Daten)
    /saleaction/ajaxgetdeckappcompatibilityreport   Steam-Deck-Einstufung
    protondb.com/api/v1/reports/summaries/<id>.json  Einstufung der Linux-Spieler
Preise in Euro, Texte auf Deutsch. Antworten liegen im Zwischenspeicher (Listen 30 min,
Einzelheiten 6 h, store/cache.py); ohne Netz gilt der letzte Stand, sonst StoreError mit deutschem Satz.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request

from . import cache
from .cache import StoreError  # noqa: F401 (die anderen Shops nehmen sie von hier)


class NotListed(StoreError):
    """appdetails sagt success:false: Der Shop führt das Spiel nicht (mehr). Kein Netzfehler (E99)."""

SHOP = "https://store.steampowered.com"
CDN = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%d/%s"
# Neuere Spiele legen ihre Bilder unter steam/apps/<id>/<Prüfkennung>/<name> ab; die echte Adresse
# nennt nur GetItems (asset_url_format). Die feste Adresse oben liefert dann 404 (2026-10-21)
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
CC, LANG = "de", "german"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"
TIMEOUT_S = 15.0
LIST_TTL = 30 * 60
DETAIL_TTL = 6 * 3600
SECTIONS = (("specials", "Angebote"), ("top_sellers", "Topseller"), ("new_releases", "Neu"),
            ("coming_soon", "Bald verfügbar"))
# Steam verkauft auch Geräte; im Store der Konsole stehen nur Spiele
HARDWARE = re.compile(r"^(steam (machine|controller|deck|frame|link)|valve index)\b", re.I)
DECK = {3: "verified", 2: "playable", 1: "unsupported"}
PAGE = 50
# Listen der Shop-Suche (E70): je Reiter die Schalter der Suche; immer nur Spiele (category1=998)
LISTS = {"specials": {"specials": "1"}, "top_sellers": {"filter": "topsellers"},
         "new_releases": {"filter": "popularnew"}, "coming_soon": {"filter": "comingsoon"}}
# Genres (E70, Wunsch: „nach Kategorien filtern … also nach Genre“): Steam-Tags
GENRES = (("action", "Action", 19), ("adventure", "Abenteuer", 21), ("rpg", "Rollenspiel", 122),
          ("strategy", "Strategie", 9), ("simulation", "Simulation", 599), ("shooter", "Shooter", 1774),
          ("horror", "Horror", 1667), ("openworld", "Open World", 1695), ("survival", "Überleben", 1662),
          ("racing", "Rennspiel", 699), ("sports", "Sport", 701), ("fighting", "Kampfspiel", 1743),
          ("platformer", "Plattformer", 1625), ("puzzle", "Puzzle", 1664), ("coop", "Koop", 1685),
          ("indie", "Indie", 492), ("casual", "Gelegenheitsspiel", 597))


def _get(url: str, timeout: float = TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (feste https-Adressen)
        return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8"))


def price_text(cents, currency: str = "EUR") -> str:
    try:
        value = int(cents) / 100.0
    except (TypeError, ValueError):
        return ""
    text = ("%.2f" % value).replace(".", ",")
    return text + (" €" if (currency or "EUR") == "EUR" else " " + str(currency))


def strip_html(text: str) -> str:
    """Beschreibungen kommen als HTML; die Konsole zeigt reinen Text mit Absätzen."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", text or "")
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def clip(text: str, limit: int = 400) -> str:
    """Kurzbeschreibung höchstens limit Zeichen, am Satz- oder Wortende gekürzt mit „…“ (GOG endete
    sonst mitten im Wort: „… sonnigen und ries“, 2026-10-22)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= limit // 2:
        return cut[:end + 1]
    return cut[:cut.rfind(" ")].rstrip(" ,;:–-") + " …" if " " in cut else cut + "…"


class SteamStore:
    def __init__(self, cache_dir: str, get=_get, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get
        self._clock = clock

    # ------------------------------------------------------------ Zwischenspeicher
    def _cached(self, key: str, url: str, ttl: float):
        return cache.cached(self.cache_dir, key, lambda: self._get(url), ttl, clock=self._clock,
                            unreachable="Der Steam-Shop ist gerade nicht erreichbar.")

    # ------------------------------------------------------------ Bilder
    def assets(self, appids: list) -> dict:
        """Echte Bildadressen über GetItems: {appid: {"hero", "cover", "header"}}, je die größte
        Fassung (Hintergrund 3840x1240, wo es ihn gibt). Zwischengespeichert. Wirft StoreError."""
        ids = sorted({int(a) for a in appids})
        if not ids:
            return {}
        req = {"ids": [{"appid": a} for a in ids], "context": {"language": LANG, "country_code": CC.upper()},
               "data_request": {"include_assets": True}}
        url = ITEMS_URL + "?input_json=" + urllib.parse.quote(json.dumps(req, separators=(",", ":")))
        data = self._cached("assets-" + "-".join(str(a) for a in ids)[:150], url, DETAIL_TTL)
        out: dict = {}
        items = ((data.get("response") if isinstance(data, dict) else None) or {}).get("store_items") or []
        for it in items:
            a = it.get("assets") if isinstance(it, dict) and isinstance(it.get("assets"), dict) else {}
            fmt = str(a.get("asset_url_format") or "")
            if not fmt or not str(it.get("appid") or "").isdigit():
                continue

            def pick(*keys: str) -> str:
                return next((ASSET_BASE + fmt.replace("${FILENAME}", str(a[k])) for k in keys if a.get(k)), "")
            out[int(it["appid"])] = {"hero": pick("library_hero_2x", "library_hero"),
                                     "cover": pick("library_capsule_2x", "library_capsule"),
                                     "header": pick("header_2x", "header")}
        return out

    def _real_images(self, cards: list) -> list:
        """Vorschaubilder mit der echten Adresse (Kopfbild, doppelt groß, wo es das gibt): Die
        feste Adresse aus card() fehlte am 2026-10-21 bei 3 bis 5 von 12 Spielen je Reiter.
        Ohne Antwort von GetItems bleibt sie, wie sie ist."""
        ids = [int(c["id"]) for c in cards if str(c.get("id") or "").isdigit()]
        try:
            real = self.assets(ids)
        except StoreError:
            return cards
        for c in cards:
            header = (real.get(int(c["id"])) or {}).get("header") if str(c.get("id") or "").isdigit() else ""
            if header:
                c["image"] = header
        return cards

    # ------------------------------------------------------------ Listen
    @staticmethod
    def card(appid: int, name: str, image: str = "", final=None, original=None, discount: int = 0,
             currency: str = "EUR", free: bool = False) -> dict:
        out = {"store": "steam", "id": str(appid), "title": name, "image": image or CDN % (appid, "header.jpg"),
               "price": "", "original": "", "discount": int(discount or 0)}
        if free:
            out["price"] = "Kostenlos"
        elif final is not None:
            out["price"] = "Kostenlos" if int(final) == 0 else price_text(final, currency)
            if out["discount"] and original:
                out["original"] = price_text(original, currency)
        return out

    def front(self) -> list[dict]:
        """[{id, title, items: [Karte]}] für die Reiter des Stores."""
        data = self._cached("featured", "%s/api/featuredcategories?cc=%s&l=%s" % (SHOP, CC, LANG), LIST_TTL)
        out = []
        for key, title in SECTIONS:
            seen, items = set(), []
            for it in ((data.get(key) or {}).get("items") or []) if isinstance(data, dict) else []:
                appid = it.get("id")
                if not isinstance(appid, int) or appid in seen or HARDWARE.search(str(it.get("name") or "")):
                    continue
                seen.add(appid)
                card = self.card(appid, str(it.get("name") or ""), str(it.get("header_image") or ""),
                                 it.get("final_price"), it.get("original_price"),
                                 it.get("discount_percent") if it.get("discounted") else 0,
                                 str(it.get("currency") or "EUR"))
                if key == "coming_soon" and not it.get("final_price") and "demo" not in card["title"].lower():
                    card["price"] = "Bald"          # noch ohne Preis, nicht kostenlos
                items.append(card)
            out.append({"id": key, "title": title, "items": items})
        return out

    # ------------------------------------------------------------ Einzelheiten
    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste (Reiter) oder der Suche: {items, total, more}. Seiten zu je PAGE."""
        params = {"query": "", "start": str(max(0, int(page)) * PAGE), "count": str(PAGE), "category1": "998",
                  "cc": CC, "l": LANG, "infinite": "1", **LISTS.get(kind, {})}
        if kind == "search":
            params["term"] = (term or "").strip()[:100]
            if not params["term"]:
                return {"items": [], "total": 0, "more": False}
        tag = next((g[2] for g in GENRES if g[0] == genre), None)
        if tag:
            params["tags"] = str(tag)
        url = SHOP + "/search/results/?" + urllib.parse.urlencode(params)
        data = self._cached("list-" + re.sub(r"[^\w]", "_", "%s-%s-%s-%s" % (kind, genre, page, params.get("term", "")))[:120],
                            url, LIST_TTL)
        if not isinstance(data, dict):
            raise StoreError("Der Steam-Shop hat keine Liste geliefert.")
        items = parse_results(str(data.get("results_html") or ""))
        if kind == "coming_soon":
            for c in items:                 # noch ohne Preis heißt nicht kostenlos
                if c["price"] in ("", "Kostenlos") and "demo" not in c["title"].lower():
                    c["price"] = "Bald"
        total = int(data.get("total_count") or 0)
        return {"items": self._real_images(items), "total": total, "more": (int(page) + 1) * PAGE < total and bool(items)}

    def details(self, appid: int) -> dict:
        url = "%s/api/appdetails?appids=%d&cc=%s&l=%s" % (SHOP, appid, CC, LANG)
        raw = self._cached("app-%d" % appid, url, DETAIL_TTL)
        node = (raw or {}).get(str(appid)) if isinstance(raw, dict) else None
        if not isinstance(node, dict) or not node.get("success") or not isinstance(node.get("data"), dict):
            raise NotListed("Zu diesem Spiel hat der Steam-Shop keine Angaben.")
        d = node["data"]
        po = d.get("price_overview") if isinstance(d.get("price_overview"), dict) else {}
        shots = [s.get("path_full") for s in d.get("screenshots") or [] if isinstance(s, dict) and s.get("path_full")]
        req = d.get("pc_requirements") if isinstance(d.get("pc_requirements"), dict) else {}
        platforms = d.get("platforms") if isinstance(d.get("platforms"), dict) else {}
        out = {
            "store": "steam", "id": str(appid), "type": str(d.get("type") or ""),
            "title": str(d.get("name") or ""), "short": strip_html(str(d.get("short_description") or "")),
            "about": strip_html(str(d.get("about_the_game") or d.get("detailed_description") or ""))[:6000],
            "image": str(d.get("header_image") or CDN % (appid, "header.jpg")),
            "hero": CDN % (appid, "library_hero.jpg"), "cover": CDN % (appid, "library_600x900.jpg"),   # s. u. (assets)
            "screenshots": shots[:12],
            "free": bool(d.get("is_free")),
            "price": "Kostenlos" if d.get("is_free") else str(po.get("final_formatted") or "").replace("€", " €").strip(),
            "original": str(po.get("initial_formatted") or "").replace("€", " €").strip() if po.get("discount_percent") else "",
            "discount": int(po.get("discount_percent") or 0),
            "buyable": bool(po) or bool(d.get("is_free")),
            "release": str((d.get("release_date") or {}).get("date") or ""),
            "coming_soon": bool((d.get("release_date") or {}).get("coming_soon")),
            "developers": [str(x) for x in d.get("developers") or []][:3],
            "publishers": [str(x) for x in d.get("publishers") or []][:3],
            "genres": [str(g.get("description")) for g in d.get("genres") or [] if isinstance(g, dict)][:4],
            "metacritic": int((d.get("metacritic") or {}).get("score") or 0),
            "controller": str(d.get("controller_support") or ""),
            "linux": bool(platforms.get("linux")),
            "size_gb": required_gb(str(req.get("minimum") or "")),
            "url": "%s/app/%d/" % (SHOP, appid),
        }
        try:
            real = self.assets([appid]).get(appid) or {}
        except StoreError:
            real = {}                               # ohne Antwort bleibt die feste Adresse (ältere Spiele)
        for key in ("hero", "cover"):
            if real.get(key):
                out[key] = real[key]
        if real and not real.get("hero") and shots:
            out["hero"] = shots[0]                  # kein breites Titelbild hinterlegt: erster Screenshot
        return out

    def deck(self, appid: int) -> str:
        """"verified" | "playable" | "unsupported" | "" (unbekannt)."""
        url = "%s/saleaction/ajaxgetdeckappcompatibilityreport?nAppID=%d&l=%s" % (SHOP, appid, LANG)
        try:
            data = self._cached("deck-%d" % appid, url, DETAIL_TTL)
            return DECK.get(int(((data or {}).get("results") or {}).get("resolved_category") or 0), "")
        except (StoreError, TypeError, ValueError, AttributeError):
            return ""

    def protondb(self, appid: int) -> str:
        """Einstufung der Linux-Spieler: platinum, gold, silver, bronze, borked oder ""."""
        url = "https://www.protondb.com/api/v1/reports/summaries/%d.json" % appid
        try:
            data = self._cached("protondb-%d" % appid, url, DETAIL_TTL)
            tier = str((data or {}).get("tier") or "")
            return tier if tier in ("platinum", "gold", "silver", "bronze", "borked") else ""
        except (StoreError, AttributeError):
            return ""


def required_gb(text: str) -> float:
    """Speicherplatz aus den Mindestanforderungen („Speicherplatz: 70 GB verfügbar“), sonst 0."""
    plain = strip_html(text)
    m = re.search(r"(?:Speicherplatz|Storage|Festplatte)[^0-9]{0,40}(\d+(?:[.,]\d+)?)\s*(GB|MB|TB)", plain, re.I)
    if not m:
        return 0.0
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return value / 1024.0 if unit == "MB" else value * 1024.0 if unit == "TB" else value


def parse_results(html_text: str) -> list[dict]:
    """Karten aus dem HTML der Shop-Suche (data-ds-appid, Titel, Rabatt, Preise). Bundles und
    Pakete (mehrere Nummern) und Geräte fallen weg."""
    out, seen = [], set()
    for m in re.finditer(r'<a href="https://store\.steampowered\.com/app/(\d+)/[^"]*"[^>]*data-ds-appid="(\d+)"(.*?)</a>',
                         html_text, re.S):
        appid = int(m.group(1))
        body = m.group(3)
        title_m = re.search(r'<span class="title">(.*?)</span>', body, re.S)
        title = html.unescape(title_m.group(1)).strip() if title_m else ""
        if not title or appid in seen or HARDWARE.search(title):
            continue
        seen.add(appid)
        disc = re.search(r'data-discount="(\d+)"', body)
        final = re.search(r'class="discount_final_price[^"]*">([^<]+)<', body)
        original = re.search(r'class="discount_original_price">([^<]+)<', body)
        discount = int(disc.group(1)) if disc else 0
        price = html.unescape(final.group(1)).strip().replace("€", " €").replace("  ", " ") if final else ""
        if not price:
            cents = re.search(r'data-price-final="(\d+)"', body)
            price = "Kostenlos" if cents and cents.group(1) == "0" else ""
        if price.lower().startswith(("free", "kostenlos")):
            price = "Kostenlos"
        card = SteamStore.card(appid, title)
        card.update(price=price, discount=discount,
                    original=html.unescape(original.group(1)).strip().replace("€", " €") if original and discount else "")
        out.append(card)
    return out


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label, _tag in GENRES]
