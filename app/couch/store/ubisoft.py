"""Ubisoft-Shop lesen: Listen, Suche, Einzelheiten. Ohne Anmeldung, nur Standardbibliothek.

    store.ubisoft.com/de/home            Algolia-Kennung, Suchschlüssel und Indexnamen der Shop-Seite
                                         (dazu appId/genomeID der Web-Anmeldung, siehe web_ids())
    <app>-dsn.algolia.net                Liste aller PC-Spiele (Bestseller-Reihenfolge) und Suche
    store.ubi.com/s/de_ubisoft/dw/shop   Salesforce-Commerce-Schnittstelle (OCAPI, öffentliche client_id):
                                         Texte, Bilder, Angebote und die Ubisoft-Connect-Spiel-ID
                                         (c_productOwnershipUplayGameID; Anno 1800 -> 4553 wie in umu)
Karten wie store.steam.SteamStore.card, mit store "ubisoft"; id = SFCC-Produkt-ID der Edition
(z. B. "660e5a17fbff4e2940488bce"). Zwischenspeicher: store/cache.py.
"""
from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import cache
from .steam import DETAIL_TTL, LIST_TTL, StoreError, clip, price_text, strip_html

HOME = "https://store.ubisoft.com/de/home"
ALGOLIA = "https://%s-dsn.algolia.net/1/indexes/%s/query"
OCAPI = "https://store.ubi.com/s/de_ubisoft/dw/shop/v22_10"
CLIENT_ID = "2a3b13e8-a80b-4795-853a-4cd52645919b"      # öffentliche Kennung der Shop-Seite (UplayDB/UbiServices)
SHOP = "https://store.ubisoft.com/de/%s/%s.html"
SHOP_BY_ID = "https://store.ubisoft.com/de/%s.html"      # leitet auf die richtige Adresse um (geprüft 2026-10-22)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT_S = 20.0
PAGE = 48
BATCH = 24                     # mehr Produkte nimmt OCAPI pro Abfrage nicht (Fehler „(0..24)“)
CONFIG_TTL = 12 * 3600
META_TTL = 4 * DETAIL_TTL
NOT_REACHABLE = "Der Ubisoft-Shop ist gerade nicht erreichbar."
NO_DETAILS = "Zu diesem Spiel hat Ubisoft keine Angaben."
# Werte der Shop-Seite vom 2026-10-22; gelten, bis die Seite andere nennt
FALLBACK = {"app": "XELY3U4LOD", "key": "5638539fd9edb8f2c6b024b49ec375bd",
            "index": "production__de_ubisoft__products__de_DE__best_sellers",
            "app_id": "82b650c0-6cb3-40c0-9f41-25a53b62b206", "genome_id": "42d07c95-9914-4450-8b38-267c4e462b21"}
# Nur PC-Spiele: DLCs, Ingame-Währung und Season-Pässe tragen product_type "DLCs" (deutscher Index)
FILTERS = ["product_type:Spiele", "Platform:PC (Digital)"]
NOT_GAME = re.compile(r"(?i)season.?pass|credits|\bunits\b|währung|jahres.?pass|year \d pass")
FIELDS = ("objectID,name,title,url,price,default_price,promotion_percentage,Genre,release_date,comingSoon,"
          "preorder,image_link,Edition,dlcType,product_type,Platform")
TABS = [("ubi_best", "Bestseller"), ("ubi_specials", "Angebote"), ("ubi_new", "Neu"),
        ("ubi_free", "Kostenlos"), ("ubi_soon", "Bald verfügbar")]
# Genres: Werte der Algolia-Facette „Genre“ (gemischt deutsch/englisch, Stand 2026-10-22)
GENRES = (("action", "Action & Abenteuer", ("Action / Adventure",)),
          ("strategy", "Strategie", ("Strategie", "strategy-simulator", "strategy-simulation")),
          ("shooter", "Shooter", ("Shooter",)),
          ("racing", "Rennspiel", ("Rennen", "racing-competitive-sport")),
          ("family", "Familie", ("Familie", "casual-mutiplayer", "musical")),
          ("indie", "Indie", ("Indie",)),
          ("sports", "Sport", ("Sport", "racing-competitive-sport")),
          ("simulation", "Simulation", ("Simulation", "strategy-simulator", "strategy-simulation")),
          ("fighting", "Kampfspiel", ("Kampfspiel",)))
# Bildgrößen über den Bilddienst der Shop-Seite (dis_base_link + ?sw=<Breite>)
CARD_W, HERO_W, SHOT_W = 800, 1920, 1280


def _read(req) -> tuple[str, str]:
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:     # noqa: S310 (feste https-Adressen)
        return resp.headers.get("Content-Type", ""), resp.read(16 * 1024 * 1024).decode("utf-8", "replace")


def _get(url: str):
    """JSON als dict/list, alles andere (die Shop-Seite) als Text."""
    kind, body = _read(urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                            "Accept": "application/json, text/html;q=0.9"}))
    return json.loads(body) if "json" in kind else body


def _post(url: str, body: dict, headers: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                                          "Accept": "application/json", **headers})
    return json.loads(_read(req)[1])


def parse_home(text: str) -> dict:
    """Algolia-Werte und Web-Anmelde-Kennungen aus dem HTML der Startseite. ValueError ohne Algolia."""
    text = str(text or "")
    out = {}
    m = re.search(r'"ALGOLIA_APP_ID":"([A-Z0-9]{6,20})"', text)
    k = re.search(r'"ALGOLIA_API_KEY":"([0-9a-f]{32})"', text)
    if not m or not k:
        raise ValueError("Algolia-Werte fehlen")
    out.update(app=m.group(1), key=k.group(1))
    idx = re.search(r'"ALGOLIA_INDEXES_V2":"((?:[^"\\]|\\.)*)"', text)
    try:
        table = json.loads(json.loads('"%s"' % idx.group(1))) if idx else {}
        de = table.get("de_DE") or {}
        index = de.get("best_selling") or de.get("defaultIndex") or ""
    except (ValueError, AttributeError):
        index = ""
    out["index"] = index if re.fullmatch(r"[\w.-]{5,120}", index) else FALLBACK["index"]
    for key, name in (("app_id", "appId"), ("genome_id", "genomeID")):
        found = re.search(r'"%s":"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"' % name, text)
        out[key] = found.group(1) if found else FALLBACK[key]
    return out


def _close(error: Exception) -> None:
    """HTTP-Fehler halten die Antwort offen, bis man sie schließt."""
    if isinstance(error, urllib.error.HTTPError):
        try:
            error.close()
        except Exception:
            pass


def _euro(value) -> str:
    """69.99 -> „69,99 €“, 0 -> „Kostenlos“, fehlt -> „“."""
    if value is None or value == "":
        return ""
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError):
        return ""
    return "Kostenlos" if cents == 0 else price_text(cents)


def _eur(prices) -> float | None:
    try:
        return float((prices or {}).get("EUR")) if isinstance(prices, dict) else None
    except (TypeError, ValueError):
        return None


def _slug(url: str) -> str:
    m = re.match(r"^(?:https://store\.ubisoft\.com)?/de/([^/]+)/[0-9a-f]{24}\.html", str(url or ""))
    return m.group(1) if m else ""


def _uplay(value) -> int:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else 0


def _sized(url: str, width: int) -> str:
    """Bilddienst-Adressen verkleinern; andere Adressen bleiben, wie sie sind."""
    url = str(url or "")
    return url + "?sw=%d" % width if "/dw/image/v2/" in url and "?" not in url else url


def _images(product: dict, view: str) -> list[str]:
    for group in product.get("image_groups") or []:
        if isinstance(group, dict) and group.get("view_type") == view:
            return [str(i.get("dis_base_link") or i.get("link") or "") for i in group.get("images") or []
                    if isinstance(i, dict) and (i.get("dis_base_link") or i.get("link"))]
    return []


def _card_image(product: dict) -> str:
    """Querformat für die Kachel: eigenes Kachelbild, sonst das erste Bild der Galerie, sonst das
    breite Seitenbanner (3,5:1, darum breiter geladen, die Kachel schneidet die Mitte aus)."""
    for view in ("horizontal", "media_slider_PD"):
        found = _images(product, view)
        if found:
            return _sized(found[0], CARD_W)
    banner = _images(product, "pdpbanner")
    if banner:
        return _sized(banner[0], 2 * CARD_W)
    shots = _images(product, "large")[1:]      # [0] ist die Packung im Hochformat
    return _sized(shots[0], CARD_W) if shots else ""


def _local_date(iso: str) -> str:
    """„2025-03-19T22:00:00.000Z“ -> „2025-03-20“ (deutsche Zeit; Ubisoft legt Mitternacht in UTC ab)."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)", str(iso or ""))
    if not m:
        return str(iso or "")[:10] if re.match(r"\d{4}-\d\d-\d\d$", str(iso or "")[:10]) else ""
    ts = calendar.timegm(tuple(int(x) for x in m.groups()) + (0,))
    try:
        from zoneinfo import ZoneInfo
        day = datetime.datetime.fromtimestamp(ts, ZoneInfo("Europe/Berlin"))
    except Exception:       # ohne Zeitzonendaten: Winterzeit reicht für das Datum fast immer
        day = datetime.datetime.fromtimestamp(ts + 3600, datetime.timezone.utc)
    return day.strftime("%Y-%m-%d")


def _release(custom: str, iso: str) -> str:
    """Erscheinungsdatum als „JJJJ-MM-TT“; „2200-01-01“ und reine Jahreszahlen heißen „noch offen“."""
    m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", str(custom or ""))
    day = "%s-%02d-%02d" % (m.group(3), int(m.group(2)), int(m.group(1))) if m else _local_date(iso)
    return day if day and day < "2100" else ""


def _whole(text: str) -> str:
    """Ubisoft kappt den ersten Absatz nach 255 Zeichen mitten im Wort („… als Yasuke, der h“):
    dann bis zum letzten ganzen Satz, sonst bis zum letzten ganzen Wort mit „…“."""
    text = str(text or "").strip()
    if len(text) < 250 or re.search(r"[.!?…\"“”»)]$", text):
        return text
    end = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
    if end >= 60:
        return text[:end + 1]
    return text[:text.rfind(" ")].rstrip(" ,;:–-") + " …" if " " in text else text


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))


def _title(text) -> str:
    """Deutscher Name ohne ®/™ und doppelte Leerzeichen („DIE SIEDLER III  History Edition“)."""
    return re.sub(r"\s{2,}", " ", re.sub(r"[®™]", "", str(text or ""))).strip()


class UbisoftStore:
    def __init__(self, cache_dir: str, get=_get, post=_post, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get
        self._post = post
        self._clock = clock
        self._meta_lock = threading.Lock()

    # ------------------------------------------------------------------ Zwischenspeicher
    def _path(self, key: str) -> str:
        return cache.path_for(self.cache_dir, key)

    def _cached(self, key: str, fetch, ttl: float):
        def check(data) -> None:
            if isinstance(data, dict) and data.get("fault"):
                raise StoreError(str((data["fault"] or {}).get("message") or "Fehler")[:200])
        return cache.cached(self.cache_dir, key, fetch, ttl, clock=self._clock, check=check, on_error=_close,
                            unreachable=NOT_REACHABLE)

    def _write(self, path: str, data) -> None:
        cache.write(self.cache_dir, path, data)

    # ------------------------------------------------------------------ Shop-Seite
    def config(self) -> dict:
        """Algolia-Werte und Anmelde-Kennungen; ohne Seite die zuletzt gelesenen, sonst FALLBACK."""
        try:
            data = self._cached("ubi-config", lambda: parse_home(self._get(HOME)), CONFIG_TTL)
            return {**FALLBACK, **data} if isinstance(data, dict) else dict(FALLBACK)
        except StoreError:
            return dict(FALLBACK)

    def web_ids(self) -> dict:
        """{"app_id", "genome_id"} der Web-Anmeldung, wie sie die Shop-Seite gerade nennt."""
        c = self.config()
        return {"app_id": c["app_id"], "genome_id": c["genome_id"]}

    def _algolia(self, key: str, params: dict, ttl: float) -> dict:
        c = self.config()

        def fetch():
            try:
                return self._post(ALGOLIA % (c["app"], c["index"]),
                                  {"params": urllib.parse.urlencode(params)},
                                  {"X-Algolia-Application-Id": c["app"], "X-Algolia-API-Key": c["key"]})
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):       # Schlüssel oder Index getauscht: Seite neu lesen
                    try:
                        os.unlink(self._path("ubi-config"))
                    except OSError:
                        pass
                raise
        data = self._cached(key, fetch, ttl)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ Karten
    @staticmethod
    def _is_game(hit) -> bool:
        if not isinstance(hit, dict) or not hit.get("objectID") or not (hit.get("name") or hit.get("title")):
            return False
        if hit.get("dlcType") or str(hit.get("product_type") or "Spiele") != "Spiele":
            return False
        if "PC" not in str(hit.get("Platform") or "PC"):
            return False
        name = _title(hit.get("name") or hit.get("title"))
        return not NOT_GAME.search(str(hit.get("Edition") or "")) and not NOT_GAME.search(name)

    @staticmethod
    def _soon(hit: dict, today: str) -> bool:
        day = str(hit.get("release_date") or "")[:10]
        return hit.get("comingSoon") == "Yes" or day > today

    @classmethod
    def card(cls, hit: dict, meta: dict | None = None, today: str = "") -> dict:
        meta = meta or {}
        today = today or _today()
        price, base = _eur(hit.get("price")), _eur(hit.get("default_price"))
        try:
            discount = int(str(hit.get("promotion_percentage") or "0").strip() or 0)
        except ValueError:
            discount = 0
        if not discount and price is not None and base and price < base:
            discount = round(100 - 100 * price / base)
        if price == 0 and cls._soon(hit, today):
            price = None            # noch nicht zu haben: Ubisoft führt dann 0 € statt keinem Preis
        image = meta.get("i") or str(hit.get("image_link") or "")
        return {"store": "ubisoft", "id": str(hit["objectID"]), "title": _title(hit.get("name") or hit.get("title")),
                "slug": _slug(hit.get("url")), "image": image, "price": _euro(price),
                "discount": discount if price is not None and discount > 0 else 0,
                "original": _euro(base) if price is not None and discount > 0 and base else "",
                "uplay_id": int(meta.get("u") or 0)}

    def _meta(self, ids: list[str]) -> dict:
        """{id: {"u": Ubisoft-Connect-ID, "i": Querformat-Bild, "s": spaceId}} über OCAPI, je 24 auf einmal.
        Eigene Datei mit Zeitstempel je Spiel; ohne Netz gilt der letzte Stand, sonst bleibt es leer.
        Neue Editionen nennen oft keine Spiel-ID; dann gilt die einer anderen Edition desselben Spiels
        (gleicher spaceId), notfalls über uplay_ids()."""
        path = self._path("ubi-meta")
        with self._meta_lock:
            try:
                with open(path, encoding="utf-8") as fh:
                    known = json.load(fh)
                known = known if isinstance(known, dict) else {}
            except (OSError, ValueError):
                known = {}
            now = self._clock()
            todo = [i for i in dict.fromkeys(ids)
                    if not isinstance(known.get(i), dict) or now - float(known[i].get("at") or 0) > META_TTL]
            changed = False
            for start in range(0, len(todo), BATCH):
                part = todo[start:start + BATCH]
                url = "%s/products/(%s)?%s" % (OCAPI, ",".join(part), urllib.parse.urlencode({
                    "client_id": CLIENT_ID, "locale": "de-DE", "expand": "images",
                    "select": "(data.(id,c_productOwnershipUplayGameID,c_productSpaceId,"
                              "image_groups.(view_type,images.(dis_base_link,link))))"}))
                try:
                    data = self._get(url)
                except Exception as e:  # Karten gehen auch ohne: Hochformat-Bild, uplay_id 0
                    _close(e)
                    continue
                found = {str(p.get("id")): p for p in (data or {}).get("data") or [] if isinstance(p, dict)} \
                    if isinstance(data, dict) else {}
                for i in part:
                    p = found.get(i) or {}
                    known[i] = {"u": _uplay(p.get("c_productOwnershipUplayGameID")), "i": _card_image(p),
                                "s": str(p.get("c_productSpaceId") or ""), "at": now}
                    changed = True
            if changed:
                self._write(path, known)
            out = {i: dict(known[i]) for i in ids if isinstance(known.get(i), dict)}
            by_space = {}
            for v in known.values():
                if isinstance(v, dict) and v.get("s") and v.get("u"):
                    by_space.setdefault(v["s"], v["u"])
        missing = sorted({v["s"] for v in out.values() if not v.get("u") and v.get("s") and v["s"] not in by_space})
        if missing:
            by_space.update(self.uplay_ids(missing))
        for v in out.values():
            if not v.get("u") and v.get("s"):
                v["u"] = int(by_space.get(v["s"]) or 0)
        return out

    def _cards(self, hits: list[dict]) -> list[dict]:
        meta = self._meta([str(h["objectID"]) for h in hits])
        today = _today(self._clock())
        return [self.card(h, meta.get(str(h["objectID"])), today) for h in hits]

    # ------------------------------------------------------------------ Listen
    def _games(self) -> list[dict]:
        """Alle PC-Spiele des Shops in Bestseller-Reihenfolge (rund 270, eine Abfrage)."""
        params = {"query": "", "hitsPerPage": 1000, "page": 0, "facetFilters": json.dumps(FILTERS),
                  "attributesToRetrieve": FIELDS, "attributesToHighlight": "[]"}
        data = self._algolia("ubi-games", params, LIST_TTL)
        seen, out = set(), []
        for h in data.get("hits") or []:
            if self._is_game(h) and h["objectID"] not in seen:
                seen.add(h["objectID"])
                out.append(h)
        return out

    @staticmethod
    def _genre_values(genre: str) -> tuple:
        return next((values for key, _label, values in GENRES if key == genre), ())

    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste oder der Suche: {items, total, more}."""
        page = max(0, int(page))
        values = self._genre_values(genre)
        empty = {"items": [], "total": 0, "more": False}
        if kind == "search":
            term = (term or "").strip()[:100]
            if not term:
                return empty
            facets = list(FILTERS) + ([["Genre:" + v for v in values]] if values else [])
            params = {"query": term, "hitsPerPage": PAGE, "page": page, "facetFilters": json.dumps(facets),
                      "attributesToRetrieve": FIELDS, "attributesToHighlight": "[]"}
            data = self._algolia("ubi-search-%s-%d-%s" % (genre, page, term.lower()[:60]), params, LIST_TTL)
            hits = [h for h in data.get("hits") or [] if self._is_game(h)]
            total = int(data.get("nbHits") or 0)
            return {"items": self._cards(hits), "total": total,
                    "more": page + 1 < int(data.get("nbPages") or 0) and bool(hits)}
        if kind not in {k for k, _t in TABS}:
            return empty
        games = [h for h in self._games() if not values or h.get("Genre") in values]
        today = _today(self._clock())
        if kind == "ubi_specials":
            def off(h):
                return self.card(h, None, today)["discount"]
            games = sorted((h for h in games if off(h) > 0), key=off, reverse=True)
        elif kind == "ubi_new":
            games = sorted((h for h in games if h.get("release_date") and not self._soon(h, today)
                            and str(h["release_date"])[:10] <= today),
                           key=lambda h: str(h.get("release_date") or ""), reverse=True)
        elif kind == "ubi_free":
            games = [h for h in games if _eur(h.get("price")) == 0 and not self._soon(h, today)]
        elif kind == "ubi_soon":
            games = sorted((h for h in games if self._soon(h, today) or h.get("preorder") == "true"),
                           key=lambda h: str(h.get("release_date") or "9999"))
        part = games[page * PAGE:(page + 1) * PAGE]
        return {"items": self._cards(part), "total": len(games), "more": (page + 1) * PAGE < len(games)}

    # ------------------------------------------------------------------ Einzelheiten
    def _product(self, item_id: str) -> dict:
        url = "%s/products/(%s)?%s" % (OCAPI, item_id, urllib.parse.urlencode({
            "client_id": CLIENT_ID, "locale": "de-DE", "currency": "EUR",
            "expand": "images,prices,promotions,availability"}))
        data = self._cached("ubi-product-" + item_id, lambda: self._get(url), DETAIL_TTL)
        items = (data or {}).get("data") if isinstance(data, dict) else None
        p = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
        if not p or not p.get("name"):
            raise StoreError(NO_DETAILS)
        return p

    def details(self, item_id: str, card: dict | None = None) -> dict:
        item_id = str(item_id or "").strip()
        if not re.fullmatch(r"[0-9a-f]{24}", item_id):
            raise StoreError("Dieses Spiel gibt es nicht.")
        p = self._product(item_id)
        uplay_id = _uplay(p.get("c_productOwnershipUplayGameID"))
        first = _whole(strip_html(str(p.get("c_productDescriptionFirstParagraphString") or p.get("page_description")
                                      or p.get("c_productDescriptionFirstParagraphString_MDM") or "")))
        long_text = strip_html(str(p.get("long_description") or p.get("c_longDescription_MDM")
                                   or p.get("c_productDescriptionString") or ""))
        if not long_text:
            extra = strip_html(str(p.get("page_description") or ""))
            long_text = first + ("\n\n" + extra if extra and extra not in first else "")
        release = _release(p.get("c_productCustomReleaseDateString"), p.get("c_productReleaseDateTime"))
        year = re.match(r"^\s*(\d{4})\s*$", str(p.get("c_productCustomReleaseDateString") or ""))
        today = _today(self._clock())
        coming = (bool(release) and release > today) or (bool(year) and year.group(1) > today[:4])
        shots = []
        for url in _images(p, "media_slider_PD") or _images(p, "large")[1:]:
            sized = _sized(url, SHOT_W)
            if sized not in shots and re.search(r"\.(jpe?g|png|webp)$", url, re.I):
                shots.append(sized)
        hero = (_images(p, "pdpbanner") or _images(p, "media_slider_PD") or _images(p, "horizontal") or [""])[0]
        genres = []
        for g in re.split(r"\s*[,/]\s*", str(p.get("c_productGenreDisplayString") or "")):
            if g and g.casefold() not in {x.casefold() for x in genres}:
                genres.append(g)
        dev = str(p.get("c_productDeveloperString") or p.get("c_productPublisherString") or p.get("manufacturer_name") or "")
        slug = str((card or {}).get("slug") or "")
        # Preis: Liste, darunter der niedrigste Angebotspreis (product_promotions[].promotional_price)
        price = p.get("price") if isinstance(p.get("price"), (int, float)) else None
        promo = [x.get("promotional_price") for x in p.get("product_promotions") or []
                 if isinstance(x, dict) and isinstance(x.get("promotional_price"), (int, float))]
        out_price, original, discount = _euro(price), "", 0
        if price and promo and min(promo) < price:
            out_price, original, discount = _euro(min(promo)), _euro(price), round(100 - 100 * min(promo) / price)
        if price == 0 and coming:
            out_price = ""
        inv = p.get("inventory") if isinstance(p.get("inventory"), dict) else {}
        orderable = bool(inv.get("orderable") or inv.get("preorderable")) if inv else True
        out = {
            "store": "ubisoft", "id": item_id, "type": "game", "title": _title(p.get("name")), "slug": slug,
            "short": clip(first), "about": long_text[:6000],
            "image": str((card or {}).get("image") or "") or _card_image(p),
            "hero": _sized(hero, HERO_W), "screenshots": shots[:12], "release": release,
            "developers": [dev.strip()] if dev.strip() else [], "genres": genres[:4],
            "metacritic": 0, "controller": "", "deck": "", "protondb": "", "linux": False, "windows": True,
            "installable": bool(uplay_id), "coming_soon": coming, "size_gb": 0.0,
            "url": SHOP % (slug, item_id) if slug else SHOP_BY_ID % item_id,
        }
        # Wie bei Epic: der Preis der Karte, aus der man kam (dieselbe Liste, derselbe Stand)
        card = card or {}
        out.update(price=str(card.get("price") or out_price),
                   original=str(card.get("original", original) or "") if card.get("price") else original,
                   discount=int(card.get("discount", discount) or 0) if card.get("price") else discount)
        out["free"] = out["price"] == "Kostenlos"
        out["buyable"] = bool(out["price"]) and orderable
        out["uplay_id"] = uplay_id
        return out

    # ------------------------------------------------------------------ Zuordnung für das Konto
    def uplay_ids(self, space_ids: list[str]) -> dict:
        """{spaceId: Ubisoft-Connect-ID} für eigene Spiele (GraphQL nennt nur spaceIds). Über die
        Shop-Suche (c_productSpaceId, mehrere mit „|“) und die Produkte; unbekannt -> nicht im Ergebnis."""
        spaces = sorted({s for s in space_ids if re.fullmatch(r"[0-9a-f-]{36}", str(s or ""))})
        out = {}
        for start in range(0, len(spaces), 20):
            part = spaces[start:start + 20]
            key = "ubi-spaces-" + hashlib.sha1(",".join(part).encode()).hexdigest()[:16]

            def fetch(part=part):
                found = {}
                url = "%s/product_search?%s" % (OCAPI, urllib.parse.urlencode({
                    "client_id": CLIENT_ID, "locale": "de-DE", "count": 200, "refine_1": "c_productSpaceId=" + "|".join(part),
                    "select": "(hits.(product_id,represented_product.(id)))"}))
                ids = [str(h.get("product_id")) for h in (self._get(url) or {}).get("hits") or []
                       if isinstance(h, dict) and (h.get("represented_product") or {}).get("id") == h.get("product_id")]
                for b in range(0, len(ids), BATCH):
                    if len(found) >= len(part):
                        break
                    url = "%s/products/(%s)?%s" % (OCAPI, ",".join(ids[b:b + BATCH]), urllib.parse.urlencode({
                        "client_id": CLIENT_ID, "locale": "de-DE",
                        "select": "(data.(id,c_productSpaceId,c_productOwnershipUplayGameID))"}))
                    for p in (self._get(url) or {}).get("data") or []:
                        sid, uid = str(p.get("c_productSpaceId") or ""), _uplay(p.get("c_productOwnershipUplayGameID"))
                        if sid in part and uid and sid not in found:
                            found[sid] = uid
                return found
            try:
                data = self._cached(key, fetch, META_TTL)
            except StoreError:
                continue
            out.update({k: int(v) for k, v in (data or {}).items() if str(v).isdigit()})
        return out


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label, _values in GENRES]
