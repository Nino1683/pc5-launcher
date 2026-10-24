"""Rockstar-Shop lesen: Liste, Angebote, Suche, Einzelheiten. Ohne Anmeldung.

Der Shop (store.rockstargames.com) ist eine Next.js-Seite; ihre Daten gibt es als JSON unter
    /_next/data/<buildId>/de/view-all-games.json      alle Spiele mit Bildern und Preisen (SKUs)
    /_next/data/<buildId>/de/game/<slug>.json         ein Spiel: Texte, Bilder, Editionen
Die buildId steht im __NEXT_DATA__ der Startseite store.rockstargames.com/de und wechselt mit jeder
neuen Fassung des Shops; dann antwortet die alte mit 404 und wird neu gelesen. Inoffiziell, aber
so lädt der Shop selbst seine Seiten (geprüft am 2026-10-22).
Karten wie store.steam.SteamStore.card, mit store "rockstar"; id = Shop-Slug („buy-red-dead-redemption-2“).
Nur PC-Fassungen (drm „Rockstar Games Launcher“); code ist das Kürzel des Rockstar Games Launchers.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import cache
from .steam import DETAIL_TTL, LIST_TTL, StoreError, clip, price_text

STORE = "https://store.rockstargames.com"
HOME = STORE + "/de"
DATA = STORE + "/_next/data/%s/de/%s.json"
SHOP = STORE + "/de/game/%s"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT_S = 20.0
PC = "Rockstar Games Launcher"
NOT_REACHABLE = "Der Rockstar-Shop ist gerade nicht erreichbar."
NOT_FOUND = "Dieses Spiel gibt es im Rockstar-Shop nicht."
TABS = [("rs_games", "Spiele"), ("rs_specials", "Angebote")]
# purchaseState der SKUs (aus dem Programmcode des Shops): 1 kaufbar, 2 besessen, 3 nicht verfügbar,
# 4 versteckt, 5 nicht einlösbar
PURCHASABLE, OWNED, HIDDEN = 1, 2, 4
# Spielwährung, Upgrades und Red Dead Online allein sind keine Edition des Spiels (sonst stünde bei
# RDR2 „19,99 €“ für Red Dead Online statt 59,99 € für das Spiel)
NOT_GAME = re.compile(r"GOLDBAR|SHARKCARD|CASHCARD|UPGRADE|ONLINE_EDITION", re.I)

# Launcher-Kürzel (aus der Playnite-Erweiterung, Stand 09/2026, und title_metadata.json von Rockstar):
# exe relativ im Spielordner, folder der übliche Ordnername unter „Rockstar Games“, registry der Name
# unter HKLM\SOFTWARE\WOW6432Node\Rockstar Games\ (Wert InstallFolder). Bei gta5_gen9 und rdr sind
# folder und registry nicht belegt, nur nach dem Muster der anderen angenommen.
TITLES = {
    "gta5": {"title": "Grand Theft Auto V Legacy", "exe": "PlayGTAV.exe", "folder": "Grand Theft Auto V",
             "registry": "Grand Theft Auto V", "slug": "buy-gta-v"},
    "gta5_gen9": {"title": "Grand Theft Auto V Enhanced", "exe": "GTA5_Enhanced.exe", "folder": "Grand Theft Auto V Enhanced",
                  "registry": "Grand Theft Auto V Enhanced", "slug": "buy-gta-v"},
    "rdr": {"title": "Red Dead Redemption", "exe": "RDR.exe", "folder": "Red Dead Redemption",
            "registry": "Red Dead Redemption", "slug": "buy-red-dead-redemption"},
    "rdr2": {"title": "Red Dead Redemption 2", "exe": "RDR2.exe", "folder": "Red Dead Redemption 2",
             "registry": "Red Dead Redemption 2", "slug": "buy-red-dead-redemption-2"},
    "gta4": {"title": "Grand Theft Auto IV", "exe": "GTAIV.exe", "folder": "Grand Theft Auto IV",
             "registry": "Grand Theft Auto IV", "slug": "buy-grand-theft-auto-iv"},
    "lanoire": {"title": "L.A. Noire", "exe": "LANoire.exe", "folder": "L.A. Noire",
                "registry": "LANoireMTL", "slug": "buy-la-noire"},
    "mp3": {"title": "Max Payne 3", "exe": "MaxPayne3.exe", "folder": "Max Payne 3",
            "registry": "MP3MTL", "slug": "buy-max-payne-3"},
    "bully": {"title": "Bully: Scholarship Edition", "exe": "Bully.exe", "folder": "Bully Scholarship Edition",
              "registry": "Bully", "slug": "buy-bully-scholarship-edition"},
    "gta3unreal": {"title": "GTA III – Definitive Edition", "exe": "Gameface/Binaries/Win64/LibertyCity.exe",
                   "folder": "GTA III - Definitive Edition", "registry": "GTA III - DE",
                   "slug": "buy-grand-theft-auto-the-trilogy-the-definitive-edition"},
    "gtavcunreal": {"title": "GTA Vice City – Definitive Edition", "exe": "Gameface/Binaries/Win64/ViceCity.exe",
                    "folder": "GTA Vice City - Definitive Edition", "registry": "GTA Vice City - DE",
                    "slug": "buy-grand-theft-auto-the-trilogy-the-definitive-edition"},
    "gtasaunreal": {"title": "GTA San Andreas – Definitive Edition", "exe": "Gameface/Binaries/Win64/SanAndreas.exe",
                    "folder": "GTA San Andreas - Definitive Edition", "registry": "GTA San Andreas - DE",
                    "slug": "buy-grand-theft-auto-the-trilogy-the-definitive-edition"},
    # die alte Trilogie verkauft der Shop als „The Trilogy Classic“
    "gta3": {"title": "Grand Theft Auto III", "exe": "gta3.exe", "folder": "Grand Theft Auto III",
             "registry": "Grand Theft Auto III", "slug": "buy-grand-theft-auto-the-trilogy"},
    "gtavc": {"title": "Grand Theft Auto: Vice City", "exe": "gta-vc.exe", "folder": "Grand Theft Auto Vice City",
              "registry": "Grand Theft Auto: Vice City", "slug": "buy-grand-theft-auto-the-trilogy"},
    "gtasa": {"title": "Grand Theft Auto: San Andreas", "exe": "gta_sa.exe", "folder": "Grand Theft Auto San Andreas",
              "registry": "GTA: San Andreas", "slug": "buy-grand-theft-auto-the-trilogy"},
}
# Ein Kauf im Shop kann mehrere Launcher-Titel bringen (GTA V: Legacy und Enhanced; Trilogie: drei
# Spiele). Das erste Kürzel ist das der Karte: Enhanced ist die Fassung, die Rockstar heute empfiehlt.
SLUG_CODES = {
    "buy-gta-v": ("gta5_gen9", "gta5"),
    "buy-red-dead-redemption-2": ("rdr2",),
    "buy-red-dead-redemption": ("rdr",),
    "buy-grand-theft-auto-iv": ("gta4",),
    "buy-la-noire": ("lanoire",),
    "buy-max-payne-3": ("mp3",),
    "buy-bully-scholarship-edition": ("bully",),
    "buy-grand-theft-auto-the-trilogy-the-definitive-edition": ("gta3unreal", "gtavcunreal", "gtasaunreal"),
    "buy-grand-theft-auto-the-trilogy": ("gta3", "gtavc", "gtasa"),
}
CARD_W, HERO_W, SHOT_W = 800, 1920, 1280
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
_BUILD = re.compile(r'"buildId"\s*:\s*"([A-Za-z0-9_-]{6,64})"')


def _get(url: str, timeout: float = TIMEOUT_S) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (feste https-Adresse)
        return resp.read(16 * 1024 * 1024).decode("utf-8", "replace")


def build_id(html_text: str) -> str:
    """buildId aus dem __NEXT_DATA__ einer Shop-Seite ("" wenn keine)."""
    m = _BUILD.search(html_text or "")
    return m.group(1) if m else ""


def next_data(html_text: str) -> dict:
    """Das __NEXT_DATA__-JSON einer Shop-Seite ({} wenn keins)."""
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', html_text or "", re.S)
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def sized(url, width: int) -> str:
    """Bilder des Shops liegen bei Contentful; ?w=…&fm=webp verkleinert sie dort (RDR2-Hülle: 34 KB statt 400 KB)."""
    url = str(url or "")
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("https://images.ctfassets.net/") and "?" not in url and not url.lower().endswith(".svg"):
        return "%s?w=%d&fm=webp" % (url, width)
    return url


def _asset(value) -> str:
    return str(value.get("url") or "") if isinstance(value, dict) else ""


def _sku(item_id: str, s: dict) -> dict:
    """Nur die Felder einer SKU, die der Store braucht (der Zwischenspeicher bleibt klein)."""
    def num(key, kind=float):
        try:
            return kind(s.get(key) or 0)
        except (TypeError, ValueError):
            return kind(0)
    return {"item": item_id, "external": str(s.get("externalId") or ""), "currency": str(s.get("currency") or ""),
            "sale": num("salePrice"), "original": num("originalPrice"), "release": num("releaseDate", int),
            "preorder": num("preorderDate", int), "state": num("purchaseState", int)}


def pc_skus(skus: dict, item_ids=None) -> list[dict]:
    """PC-SKUs (Rockstar Games Launcher) aus pageProps.skus ({item_id: [sku]}), ohne versteckte."""
    out = []
    for item_id, rows in (skus or {}).items() if isinstance(skus, dict) else []:
        if item_ids is not None and item_id not in item_ids:
            continue
        for s in rows if isinstance(rows, list) else []:
            if not isinstance(s, dict) or s.get("drm") != PC or s.get("visible") is False:
                continue
            if s.get("purchaseState") == HIDDEN:
                continue
            out.append(_sku(str(item_id), s))
    return out


def products(page_props: dict) -> list[dict]:
    """Spiele einer Listenseite (view-all-games): [{slug, title, item, boxart, landscape, skus}] mit PC-Fassung.
    Sucht die Produkte im ganzen Seitenbaum (der Aufbau der Seite ändert sich gern)."""
    page_props = page_props if isinstance(page_props, dict) else {}
    found, seen = [], set()

    def walk(o) -> None:
        if isinstance(o, dict):
            item = o.get("item")
            if o.get("__typename") == "ProductDetailPage" and isinstance(item, dict) and o.get("slug") \
                    and (item.get("sys") or {}).get("id") and o["slug"] not in seen:
                seen.add(o["slug"])
                found.append({"slug": str(o["slug"]), "title": str(item.get("title") or ""),
                              "item": str(item["sys"]["id"]), "boxart": _asset(item.get("boxart")),
                              "landscape": _asset(item.get("landscapeArt"))})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(page_props.get("page"))
    out = []
    for p in found:
        p["skus"] = pc_skus(page_props.get("skus"), {p["item"]})
        if p["skus"] and p["title"]:
            out.append(p)
    return out


def _euro(value: float) -> str:
    return price_text(int(round(float(value) * 100)))


def price(skus: list[dict]) -> dict:
    """Preis der günstigsten PC-Edition des Spiels: {price, original, discount}; ohne Preis alles leer."""
    games = [s for s in skus if not NOT_GAME.search(s["external"])] or list(skus)
    games = [s for s in games if s["currency"]]
    if not games:
        return {"price": "", "original": "", "discount": 0}
    best = min(games, key=lambda s: s["sale"])
    sale, original = max(0.0, best["sale"]), best["original"]
    discount = round(100 - 100 * sale / original) if 0 <= sale < original else 0
    return {"price": "Kostenlos" if sale <= 0 else _euro(sale), "original": _euro(original) if discount else "",
            "discount": discount}


def _date(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0)) if ms and ms > 0 else ""


class RockstarStore:
    def __init__(self, cache_dir: str, get=_get, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get
        self._clock = clock

    # ------------------------------------------------------------------ Zwischenspeicher
    def _cached(self, key: str, fetch, ttl: float, force: bool = False):
        # StoreError aus fetch ist schon ein Satz für die Oberfläche (etwa: gibt es nicht)
        return cache.cached(self.cache_dir, key, fetch, ttl, clock=self._clock, force=force, keep=(StoreError,),
                            unreachable=NOT_REACHABLE)

    def _build(self, force: bool = False) -> str:
        def fetch() -> dict:
            found = build_id(self._get(HOME))
            if not found:
                raise ValueError("keine buildId auf der Startseite")
            return {"buildId": found}
        return str(self._cached("rs-build", fetch, DETAIL_TTL, force=force).get("buildId") or "")

    def _data(self, path: str) -> dict:
        """pageProps einer Shop-Seite; bei 404 (neue Fassung des Shops) einmal mit neuer buildId."""
        for attempt in (0, 1):
            build = self._build(force=attempt > 0)
            try:
                data = json.loads(self._get(DATA % (build, path)))
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                try:
                    body = e.read(4096).decode("utf-8", "replace")
                except Exception:   # noqa: BLE001 (Antwort ohne Inhalt)
                    body = ""
                finally:
                    e.close()
                if '"notFound"' in body:
                    raise StoreError(NOT_FOUND) from e
                continue
            if not isinstance(data, dict) or data.get("notFound"):
                raise StoreError(NOT_FOUND)
            return data.get("pageProps") if isinstance(data.get("pageProps"), dict) else {}
        raise StoreError(NOT_FOUND)

    # ------------------------------------------------------------------ Karten
    @staticmethod
    def card(p: dict) -> dict:
        codes = SLUG_CODES.get(p["slug"], ())
        return {"store": "rockstar", "id": p["slug"], "title": p["title"], "slug": p["slug"],
                "image": sized(p.get("landscape") or p.get("boxart"), CARD_W), **price(p.get("skus") or []),
                "code": codes[0] if codes else ""}

    def games(self) -> list[dict]:
        """Alle Spiele mit PC-Fassung als Karten, in der Reihenfolge des Shops."""
        data = self._cached("rs-games", lambda: {"products": products(self._data("view-all-games"))}, LIST_TTL)
        return [self.card(p) for p in (data.get("products") or []) if isinstance(p, dict) and p.get("slug")]

    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste oder der Suche: {items, total, more}. Der Shop hat nur gut zehn
        PC-Spiele, darum gibt es nur eine Seite und die Suche filtert im Speicher."""
        empty = {"items": [], "total": 0, "more": False}
        if int(page or 0) > 0:
            return empty
        if kind == "rs_games":
            items = self.games()
        elif kind == "rs_specials":
            items = [c for c in self.games() if c["discount"] > 0]
        elif kind == "search":
            term = re.sub(r"\s+", " ", (term or "").strip())[:100].casefold()
            if not term:
                return empty
            words = term.split(" ")
            items = [c for c in self.games()
                     if all(w in (c["title"] + " " + c["slug"].replace("-", " ")).casefold() for w in words)]
        else:
            return empty
        return {"items": items, "total": len(items), "more": False}

    # ------------------------------------------------------------------ Einzelheiten
    @staticmethod
    def _page(pp: dict) -> dict:
        """Das Nötige einer Spielseite (für den Zwischenspeicher)."""
        page = pp.get("page") if isinstance(pp.get("page"), dict) else {}
        details = page.get("details") if isinstance(page.get("details"), dict) else {}
        item = details.get("item") if isinstance(details.get("item"), dict) else {}
        if not item.get("title"):
            raise StoreError("Zu diesem Spiel hat Rockstar keine Angaben.")
        main = str((item.get("sys") or {}).get("id") or "")
        # Inhalte je Plattform; die PC-Fassung heißt meist „Rockstar Games Launcher“, bei L.A. Noire
        # nur „Other“/„DRM Free“. Konsolen-Inhalte (andere Editionen) bleiben außen vor.
        layouts = [lay for lay in page.get("layouts") or [] if isinstance(lay, dict)]
        pick = [lay for lay in layouts if PC in (lay.get("platforms") or [])] or \
               [lay for lay in layouts if not all(re.match(r"(PlayStation|Xbox|Nintendo)", str(p))
                                                  for p in (lay.get("platforms") or ["PlayStation"]))][:1]
        blocks = [b for lay in pick for b in ((lay.get("layoutCollection") or {}).get("items") or []) if isinstance(b, dict)]
        editions, features, shots = {}, [], []
        for b in blocks:
            kind = b.get("__typename")
            if kind == "ComparisonTable":
                for item_id, row in (b.get("comparisonTableData") or {}).items():
                    if isinstance(row, dict) and row.get("title"):
                        editions.setdefault(str(item_id), str(row["title"]))
            elif kind == "ProductDetailPageKeyFeatureContainer":
                for f in ((b.get("keyfeaturesCollection") or {}).get("items") or []):
                    copy = str((f or {}).get("copy") or "").strip()
                    if copy and copy not in features:
                        features.append(copy)
            elif kind == "ScreenShotsVideos":
                for s in ((b.get("playListCollection") or {}).get("items") or []):
                    url = _asset((s or {}).get("asset")) if (s or {}).get("__typename") == "Screenshot" else ""
                    if url and url not in shots:
                        shots.append(url)
        editions.setdefault(main, str(item["title"]))
        skus = [s for s in pc_skus(pp.get("skus"), set(editions)) if not re.search(r"GOLDBAR|SHARKCARD|CASHCARD", s["external"])]
        hero = details.get("hero") if isinstance(details.get("hero"), dict) else {}
        return {"title": str(item["title"]), "main": main, "description": str(item.get("description") or ""),
                "long": str(item.get("longDescription") or ""), "features": features[:8],
                "developers": [str(d) for d in (item.get("developers") or item.get("publishers") or []) if d][:2],
                "genres": [str(g) for g in (item.get("genres") or []) if g][:4],
                "boxart": _asset(item.get("boxart")), "hero": _asset(hero.get("background")),
                "screenshots": shots[:12], "editions": editions, "skus": skus}

    def details(self, item_id: str, card: dict | None = None) -> dict:
        slug = str(item_id or "").strip()
        if not _SLUG.match(slug):
            raise StoreError("Dieses Spiel gibt es nicht.")
        d = self._cached("rs-game-" + slug, lambda: self._page(self._data("game/" + slug)), DETAIL_TTL)
        if not isinstance(d, dict) or not d.get("title"):
            raise StoreError("Zu diesem Spiel hat Rockstar keine Angaben.")
        from .epic import strip_markdown
        skus = [s for s in d.get("skus") or [] if isinstance(s, dict)]
        games = [s for s in skus if not NOT_GAME.search(s["external"])] or skus
        names = d.get("editions") or {}
        editions = []
        for s in sorted(games, key=lambda s: s["sale"]):
            title = names.get(s["item"]) or d["title"]
            if s["currency"] and title not in [e["title"] for e in editions]:
                editions.append({"title": title, "price": "Kostenlos" if s["sale"] <= 0 else _euro(s["sale"])})
        main = [s for s in games if s["item"] == d.get("main")] or games
        release = next((_date(s["release"]) for s in main if s["release"] > 0), "")
        today = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        image = (card or {}).get("image") or ""
        if not image:
            try:
                image = next((c["image"] for c in self.games() if c["id"] == slug), "")
            except StoreError:
                image = ""
        codes = SLUG_CODES.get(slug, ())
        about = "\n\n".join([d.get("long") or d.get("description") or ""] + list(d.get("features") or []))
        out = {
            "store": "rockstar", "id": slug, "type": "game", "title": d["title"], "slug": slug,
            "short": clip(d.get("description") or strip_markdown(d.get("long") or "")),
            "about": strip_markdown(about)[:6000],
            "image": image or sized(d.get("boxart"), CARD_W),
            "hero": sized(d.get("hero") or d.get("boxart"), HERO_W),
            "screenshots": [sized(u, SHOT_W) for u in d.get("screenshots") or []][:12],
            "release": release, "developers": list(d.get("developers") or []), "genres": list(d.get("genres") or []),
            "metacritic": 0, "controller": "", "deck": "", "protondb": "", "linux": False, "windows": True,
            "installable": bool(codes),
            "coming_soon": (bool(release) and release > today)
            or any(s["preorder"] > 0 and _date(s["release"]) > today for s in main),
            "size_gb": 0.0, "url": SHOP % slug, "code": codes[0] if codes else "", "editions": editions,
        }
        if card and "price" in card:
            out.update(price=card.get("price") or "", original=card.get("original") or "",
                       discount=int(card.get("discount") or 0))
        else:
            out.update(price(games))
        out["free"] = out["price"] == "Kostenlos"
        out["buyable"] = bool(out["price"]) and any(s["state"] == PURCHASABLE for s in games)
        return out


def genres() -> list[dict]:
    """Der Rockstar-Shop kennt keine Genres (zu wenige Spiele)."""
    return []
