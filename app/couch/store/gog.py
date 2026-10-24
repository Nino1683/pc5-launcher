"""GOG-Shop lesen (Bauplan E68, Teil 2): Listen, Suche, Einzelheiten. Ohne Anmeldung.

    catalog.gog.com/v1/catalog        Listen und Suche (Preise in Euro, Rabatt, Bilder)
    api.gog.com/products/<id>         Beschreibung, Bilder, Systeme
    api.gog.com/products/prices       Preis eines Spiels (für Seiten, die nicht aus einer Liste kommen)
Karten wie store.steam.SteamStore.card, mit store "gog". Zwischenspeicher: store/cache.py.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse

from . import cache
from .steam import LIST_TTL, DETAIL_TTL, StoreError, _get, clip, strip_html

CATALOG = "https://catalog.gog.com/v1/catalog"
PRODUCT = "https://api.gog.com/products/%d?expand=description,screenshots&locale=de-DE"
PRICES = "https://api.gog.com/products/prices?ids=%d&countryCode=DE&currency=EUR"
SHOP = "https://www.gog.com/de/game/%s"
BASE = {"limit": "48", "productType": "in:game,pack", "countryCode": "DE", "locale": "de-DE", "currencyCode": "EUR"}
PAGE = 48
# Genres, die GOGs Katalog kennt (E70)
GENRES = (("action", "Action"), ("adventure", "Abenteuer"), ("rpg", "Rollenspiel"), ("strategy", "Strategie"),
          ("simulation", "Simulation"), ("shooter", "Shooter"), ("racing", "Rennspiel"), ("sports", "Sport"))
SECTIONS = (("gog_bestsellers", "Bestseller", {"order": "desc:trending"}),
            ("gog_specials", "Angebote", {"order": "desc:discount", "discounted": "eq:true"}),
            ("gog_new", "Neu", {"order": "desc:releaseDate", "releaseStatuses": "in:new-arrival"}))


def _https(url: str) -> str:
    url = str(url or "")
    return "https:" + url if url.startswith("//") else url


_PLAIN = re.compile(r"^(https://images\.gog-statics\.com/[0-9a-f]{64})\.(?:png|jpg)$")


def _light(url: str) -> str:
    """Bild in voller Größe als WebP: GOGs Kachelbilder sind PNGs mit 1600x740 und rund 650 KB, die
    Kacheln standen darum lange halb schwarz da (2026-10-22). Dasselbe Bild als .webp hat rund 70 KB."""
    url = _https(url)
    m = _PLAIN.match(url)
    return m.group(1) + ".webp" if m else url


def _euro(text: str) -> str:
    """„9,09 €“ wie bei Steam (GOG schreibt ein festes Leerzeichen)."""
    return str(text or "").replace("\xa0", " ").strip()


class GogStore:
    def __init__(self, cache_dir: str, get=_get, clock=time.time):
        self.cache_dir = cache_dir
        self._get = get
        self._clock = clock

    def _cache(self, key: str, url: str, ttl: float):
        # eigener Satz: bisher hieß es auch hier „Der Steam-Shop ist gerade nicht erreichbar.“
        return cache.cached(self.cache_dir, key, lambda: self._get(url), ttl, clock=self._clock,
                            unreachable="GOG ist gerade nicht erreichbar.")

    @staticmethod
    def card(p: dict) -> dict:
        price = p.get("price") if isinstance(p.get("price"), dict) else {}
        discount = re.sub(r"[^0-9]", "", str(price.get("discount") or ""))
        final = _euro(price.get("final"))
        free = str((price.get("finalMoney") or {}).get("amount") or "") in ("0", "0.00")
        return {"store": "gog", "id": str(p.get("id") or ""), "title": str(p.get("title") or ""),
                "slug": str(p.get("slug") or ""), "image": _light(p.get("coverHorizontal") or ""),
                "price": "Kostenlos" if free else final, "discount": int(discount or 0),
                "original": _euro(price.get("base")) if discount else ""}

    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste oder der Suche: {items, total, more}."""
        extra = next((dict(e) for key, _t, e in SECTIONS if key == kind), None)
        if kind == "search":
            term = (term or "").strip()[:100]
            if not term:
                return {"items": [], "total": 0, "more": False}
            # desc:score ordnet nach Bewertung („Reigns: The Witcher“ vor The Witcher 3); bestselling nach Treffern, die man meint
            extra = {"order": "desc:bestselling", "query": "like:" + term}
        if extra is None:
            return {"items": [], "total": 0, "more": False}
        if genre in {g[0] for g in GENRES}:
            extra["genres"] = "in:" + genre
        extra["page"] = str(max(0, int(page)) + 1)
        url = CATALOG + "?" + urllib.parse.urlencode({**BASE, **extra})
        key = re.sub(r"[^\w]", "_", "gog-%s-%s-%s-%s" % (kind, genre, page, term))[:120]
        data = self._cache(key, url, LIST_TTL)
        if not isinstance(data, dict):
            raise StoreError("GOG hat keine Liste geliefert.")
        items = [self.card(p) for p in data.get("products") or [] if isinstance(p, dict) and p.get("id")]
        return {"items": items, "total": int(data.get("productCount") or 0),
                "more": int(page) + 1 < int(data.get("pages") or 0) and bool(items)}

    def details(self, product_id: int, card: dict | None = None) -> dict:
        data = self._cache("gog-%d" % product_id, PRODUCT % product_id, DETAIL_TTL)
        if not isinstance(data, dict) or not data.get("id"):
            raise StoreError("Zu diesem Spiel hat GOG keine Angaben.")
        images = data.get("images") or {}
        shots = []
        for s in data.get("screenshots") or []:
            tpl = str((s or {}).get("formatter_template_url") or "")
            if tpl:
                shots.append(tpl.replace("{formatter}", "ggvgm_2x"))
        systems = data.get("content_system_compatibility") or {}
        desc = data.get("description") or {}
        about = strip_html(str(desc.get("full") or desc.get("lead") or ""))
        out = {
            "store": "gog", "id": str(product_id), "type": str(data.get("game_type") or "game"),
            "title": str(data.get("title") or ""), "slug": str(data.get("slug") or ""),
            "short": clip(strip_html(str(desc.get("lead") or ""))),
            "about": about[:6000],
            "image": _https(images.get("logo2x") or images.get("logo") or ""),
            "hero": _https(images.get("background") or ""),
            "screenshots": shots[:12],
            "release": str(data.get("release_date") or "")[:10],
            "developers": [], "genres": [], "metacritic": 0, "controller": "", "deck": "", "protondb": "",
            "linux": bool(systems.get("linux")), "windows": bool(systems.get("windows")),
            "installable": bool(data.get("is_installable", True)),
            "coming_soon": bool(data.get("is_pre_order")) or bool(data.get("in_development")),
            "size_gb": 0.0, "url": SHOP % data.get("slug", product_id), "drm_free": True,
        }
        card = card or {}
        out.update(price=card.get("price", ""), original=card.get("original", ""), discount=int(card.get("discount") or 0))
        if not out["price"]:
            out.update(self.price(product_id))
        out["buyable"] = bool(out["price"]) and out["installable"]
        out["free"] = out["price"] == "Kostenlos"
        return out

    def products(self, ids: list[int]) -> list[dict]:
        """Karten zu Produkt-Nummern (eigene Spiele), je 50 auf einmal, nach Titel."""
        out = []
        ids = sorted(set(int(i) for i in ids))
        for start in range(0, len(ids), 50):
            part = ids[start:start + 50]
            url = "https://api.gog.com/products?ids=%s&locale=de-DE" % ",".join(map(str, part))
            # Schlüssel aus allen Nummern des Stücks (E99): Erste Nummer und Anzahl blieben nach einem Kauf
            # oft gleich, und das neue Spiel fehlte bis zu 6 h unter „Meine Spiele“
            key = "gog-owned-" + hashlib.sha1(",".join(map(str, part)).encode()).hexdigest()[:20]
            try:
                data = self._cache(key, url, DETAIL_TTL)
            except StoreError:
                continue
            for p in data if isinstance(data, list) else []:
                if not isinstance(p, dict) or not p.get("id") or str(p.get("game_type") or "game") not in ("game", "pack"):
                    continue
                images = p.get("images") or {}
                out.append({"store": "gog", "id": str(p["id"]), "title": str(p.get("title") or ""), "slug": str(p.get("slug") or ""),
                            "image": _https(images.get("logo2x") or images.get("logo") or ""), "price": "", "original": "", "discount": 0})
        return sorted(out, key=lambda c: c["title"].casefold())

    def price(self, product_id: int) -> dict:
        try:
            data = self._cache("gog-price-%d" % product_id, PRICES % product_id, LIST_TTL)
            item = ((data.get("_embedded") or {}).get("items") or [{}])[0]
            price = ((item.get("_embedded") or {}).get("prices") or [{}])[0]
        except (StoreError, AttributeError, IndexError, TypeError):
            return {}

        def cents(text) -> int:
            m = re.match(r"(\d+)", str(text or ""))
            return int(m.group(1)) if m else -1
        base, final = cents(price.get("basePrice")), cents(price.get("finalPrice"))
        if final < 0:
            return {}
        from .steam import price_text
        out = {"price": "Kostenlos" if final == 0 else price_text(final), "original": "", "discount": 0}
        if 0 <= final < base:
            out.update(original=price_text(base), discount=round(100 - 100 * final / base))
        return out


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label in GENRES]
