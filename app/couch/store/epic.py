"""Epic-Shop lesen (Bauplan E73): Listen, Suche, Gratis-Spiele, Einzelheiten. Ohne Anmeldung.

    store.epicgames.com/graphql      searchStore (Angebote, Suche, Genres über Tags, Namensraum),
                                     collectionLayout (Topseller, Neuerscheinungen, Meistgewünscht),
                                     catalogOffer (Beschreibung, Bilder, Entwickler)
    store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions   Gratis-Spiele der Woche
Karten wie store.steam.SteamStore.card, mit store "epic"; id = "<namespace>:<angebot>" (das Angebot
fehlt bei eigenen Spielen aus legendary, dann sucht details() es über den Namensraum).
Die GraphQL-Schnittstelle antwortet nur mit einem Browser-Kennzeichen (USER_AGENT).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from . import cache
from .steam import DETAIL_TTL, LIST_TTL, StoreError, clip

GRAPHQL = "https://store.epicgames.com/graphql"
FREE = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions?locale=de&country=DE&allowCountries=DE"
SHOP = "https://store.epicgames.com/de/p/%s"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT_S = 20.0
PAGE = 40
CATEGORY = "games/edition/base|bundles/games|editors"
NOT_REACHABLE = "Der Epic-Shop ist gerade nicht erreichbar."
# Bildgrößen über das Bildernetz von Epic (spart beim Blättern viel Datenmenge)
CARD_SIZE = "?h=270&quality=medium&resize=1&w=480"
HERO_SIZE = "?h=1080&quality=high&resize=1&w=1920"
SHOT_SIZE = "?h=720&quality=medium&resize=1&w=1280"

# Genres: Tag-Nummern aus Epics Katalog (Gruppe "genre"; Koop ist ein Merkmal)
GENRES = (("action", "Action", "1216"), ("adventure", "Abenteuer", "1117"), ("rpg", "Rollenspiel", "1367"),
          ("strategy", "Strategie", "1115"), ("simulation", "Simulation", "1393"), ("shooter", "Shooter", "1210"),
          ("horror", "Horror", "1218"), ("openworld", "Offene Welt", "1307"), ("survival", "Überleben", "1080"),
          ("racing", "Rennspiel", "1212"), ("sports", "Sport", "1283"), ("fighting", "Kampfspiel", "1344"),
          ("platformer", "Plattformer", "1151"), ("puzzle", "Rätsel", "1298"), ("coop", "Koop", "1264"),
          ("indie", "Indie", "1263"), ("casual", "Gelegenheitsspiel", "1296"))
# Listen: Sammlungen (bis 100 Spiele, eine Seite) und Suchen (seitenweise)
COLLECTIONS = {"epic_top": "top-sellers", "epic_new": "top-new-releases", "epic_wished": "top-wishlisted"}
SEARCHES = {"epic_specials": {"onSale": True, "sortBy": "currentPrice", "sortDir": "DESC"}}
LISTS = ("epic_top", "epic_free", "epic_specials", "epic_new", "epic_wished")

_OFFER_FIELDS = """title id namespace productSlug urlSlug offerType
  catalogNs { mappings(pageType: "productHome") { pageSlug } }
  keyImages { type url } tags { id }
  price(country: $country) { totalPrice { discountPrice originalPrice fmtPrice(locale: $locale) { originalPrice discountPrice } } }"""
SEARCH_QUERY = """query searchStoreQuery($country: String!, $locale: String, $count: Int, $start: Int, $sortBy: String,
  $sortDir: String, $category: String, $keywords: String, $onSale: Boolean, $tag: String, $namespace: String) {
 Catalog { searchStore(country: $country, locale: $locale, count: $count, start: $start, sortBy: $sortBy, sortDir: $sortDir,
   category: $category, keywords: $keywords, onSale: $onSale, tag: $tag, namespace: $namespace) {
  elements { %s }
  paging { count total } } } }""" % _OFFER_FIELDS
COLLECTION_QUERY = """query collectionLayoutQuery($locale: String, $country: String!, $slug: String) {
 Storefront { collectionLayout(locale: $locale, slug: $slug) { collectionOffers { %s } } } }""" % _OFFER_FIELDS
OFFER_QUERY = """query catalogOfferQuery($namespace: String!, $id: String!, $locale: String, $country: String!) {
 Catalog { catalogOffer(namespace: $namespace, id: $id, locale: $locale) {
  title description longDescription developerDisplayName publisherDisplayName releaseDate pcReleaseDate
  keyImages { type url } tags { id name groupName }
  catalogNs { mappings(pageType: "productHome") { pageSlug } }
  price(country: $country) { totalPrice { discountPrice originalPrice fmtPrice(locale: $locale) { originalPrice discountPrice } } } } } }"""


def _post(query: str, variables: dict, timeout: float = TIMEOUT_S):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (feste https-Adresse)
        return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8"))


def _get(url: str, timeout: float = TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (feste https-Adresse)
        return json.loads(resp.read(8 * 1024 * 1024).decode("utf-8"))


def _euro(text) -> str:
    """„24,50 €“ wie bei Steam (Epic schreibt ein festes Leerzeichen); „0“ heißt kostenlos."""
    text = str(text or "").replace("\xa0", " ").strip()
    return "Kostenlos" if text in ("0", "0,00 €") else text


def _image(images, *kinds: str) -> str:
    by = {}
    for k in images or []:
        if isinstance(k, dict) and k.get("url") and k.get("type") not in by:
            by[k["type"]] = str(k["url"])
    return next((by[t] for t in kinds if t in by), "")


def _sized(url: str, size: str) -> str:
    return url + size if url.startswith("https://cdn1.epicgames.com/") and "?" not in url else url


def _slug(offer: dict) -> str:
    for m in ((offer.get("catalogNs") or {}).get("mappings") or []):
        if isinstance(m, dict) and m.get("pageSlug"):
            return str(m["pageSlug"])
    slug = str(offer.get("productSlug") or offer.get("urlSlug") or "")
    return slug.split("/")[0] if slug and not re.fullmatch(r"[0-9a-f]{32}", slug) else ""


def _date(text) -> str:
    return str(text or "")[:10] if re.match(r"\d{4}-\d\d-\d\d", str(text or "")) else ""


def strip_markdown(text: str) -> str:
    """Epics Beschreibungen: Markdown mit HTML-Kommentaren und eingestreutem HTML -> Text mit Absätzen."""
    from .steam import strip_html
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                     # Bilder
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                 # Verweise: nur der Text
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)                    # Überschriften
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)                       # Aufzählungen
    text = re.sub(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", r"\2", text)    # fett, kursiv
    return strip_html(text)


class EpicStore:
    def __init__(self, cache_dir: str, post=_post, get=_get, clock=time.time):
        self.cache_dir = cache_dir
        self._post = post
        self._get = get
        self._clock = clock

    # ------------------------------------------------------------------ Zwischenspeicher
    def _cached(self, key: str, fetch, ttl: float):
        def check(data) -> None:
            if isinstance(data, dict) and data.get("errors") and not data.get("data"):
                raise StoreError(str((data["errors"][0] or {}).get("message") or "Fehler")[:200])
        return cache.cached(self.cache_dir, key, fetch, ttl, clock=self._clock, check=check, unreachable=NOT_REACHABLE)

    def _gql(self, key: str, query: str, variables: dict, ttl: float):
        base = {"country": "DE", "locale": "de"}
        return self._cached(key, lambda: self._post(query, {**base, **variables}), ttl)

    # ------------------------------------------------------------------ Karten
    @staticmethod
    def card(offer: dict) -> dict:
        total = ((offer.get("price") or {}).get("totalPrice") or {}) if isinstance(offer.get("price"), dict) else {}
        fmt = total.get("fmtPrice") or {}
        try:
            cur, orig = int(total.get("discountPrice")), int(total.get("originalPrice"))
        except (TypeError, ValueError):
            cur = orig = -1
        discount = round(100 - 100 * cur / orig) if 0 <= cur < orig else 0
        price = "Kostenlos" if cur == 0 else _euro(fmt.get("discountPrice"))
        return {"store": "epic", "id": "%s:%s" % (offer.get("namespace") or "", offer.get("id") or ""),
                "title": str(offer.get("title") or ""), "slug": _slug(offer),
                "image": _sized(_image(offer.get("keyImages"), "OfferImageWide", "DieselStoreFrontWide",
                                       "DieselGameBox", "Thumbnail", "featuredMedia"), CARD_SIZE),
                "price": price, "discount": discount,
                "original": _euro(fmt.get("originalPrice")) if discount else "",
                "tags": [str(t.get("id")) for t in offer.get("tags") or [] if isinstance(t, dict)]}

    @staticmethod
    def _usable(offer) -> bool:
        return isinstance(offer, dict) and bool(offer.get("namespace")) and bool(offer.get("id")) \
            and str(offer.get("title") or "") not in ("", "Mystery Game")

    # ------------------------------------------------------------------ Listen
    def browse(self, kind: str, page: int = 0, genre: str = "", term: str = "") -> dict:
        """Eine Seite einer Liste oder der Suche: {items, total, more}."""
        page = max(0, int(page))
        tag = next((t for key, _label, t in GENRES if key == genre), "")
        if kind == "epic_free":
            items = self.free() if page == 0 else []
            items = [c for c in items if not tag or tag in c.get("tags", [])]
            return {"items": items, "total": len(items), "more": False}
        if kind in COLLECTIONS:
            if page:
                return {"items": [], "total": 0, "more": False}
            data = self._gql("epic-col-" + COLLECTIONS[kind], COLLECTION_QUERY, {"slug": COLLECTIONS[kind]}, LIST_TTL)
            layout = (((data or {}).get("data") or {}).get("Storefront") or {}).get("collectionLayout") or {}
            items, seen = [], set()
            for o in layout.get("collectionOffers") or []:
                if not self._usable(o):
                    continue
                c = self.card(o)
                if c["id"] in seen or (tag and tag not in c["tags"]):
                    continue
                seen.add(c["id"])
                items.append(c)
            return {"items": items, "total": len(items), "more": False}
        if kind == "search":
            term = (term or "").strip()[:100]
            if not term:
                return {"items": [], "total": 0, "more": False}
            extra = {"keywords": term}
        elif kind in SEARCHES:
            extra = dict(SEARCHES[kind])
        else:
            return {"items": [], "total": 0, "more": False}
        if tag:
            extra["tag"] = tag
        variables = {"count": PAGE, "start": page * PAGE, "category": CATEGORY, **extra}
        key = "epic-%s-%s-%d-%s" % (kind, genre, page, term.lower()[:60] if kind == "search" else "")
        data = self._gql(key, SEARCH_QUERY, variables, LIST_TTL)
        store = (((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore") or {}
        items = [self.card(o) for o in store.get("elements") or [] if self._usable(o)]
        total = int((store.get("paging") or {}).get("total") or 0)
        return {"items": items, "total": total, "more": (page + 1) * PAGE < total and bool(items)}

    def free(self) -> list[dict]:
        """Gratis-Spiele: jetzt gratis (Preis „Gratis bis …“) und demnächst („Gratis ab …“)."""
        data = self._cached("epic-free", lambda: self._get(FREE), LIST_TTL / 3)
        elements = ((((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
        now, soon = [], []
        for e in elements:
            if not self._usable(e):
                continue
            promo = e.get("promotions") or {}

            def window(group: str):
                for block in promo.get(group) or []:
                    for o in (block or {}).get("promotionalOffers") or []:
                        if ((o or {}).get("discountSetting") or {}).get("discountPercentage") == 0:
                            return o
                return None
            current, upcoming = window("promotionalOffers"), window("upcomingPromotionalOffers")
            if current is None and upcoming is None:
                continue
            c = self.card(e)
            c["image"] = _sized(_image(e.get("keyImages"), "OfferImageWide", "DieselStoreFrontWide", "Thumbnail"), CARD_SIZE)
            if current is not None:
                c.update(price="Gratis bis %s" % _day(current.get("endDate")), discount=0, original="", free_now=True)
                now.append(c)
            else:
                c.update(price="Gratis ab %s" % _day(upcoming.get("startDate")), discount=0, original="", free_now=False)
                soon.append(c)
        return now + soon

    def by_namespace(self, namespace: str) -> dict | None:
        """Das Angebot (Hauptspiel) zu einem Namensraum, z. B. für eigene Spiele aus legendary."""
        data = self._gql("epic-ns-" + namespace, SEARCH_QUERY,
                         {"count": 5, "start": 0, "namespace": namespace, "category": CATEGORY}, DETAIL_TTL)
        elements = ((((data or {}).get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []
        offers = [o for o in elements if self._usable(o)]
        return self.card(offers[0]) if offers else None

    # ------------------------------------------------------------------ Einzelheiten
    def details(self, item_id: str, card: dict | None = None) -> dict:
        namespace, _sep, offer = str(item_id).partition(":")
        if not namespace:
            raise StoreError("Dieses Spiel gibt es nicht.")
        if not offer:
            found = self.by_namespace(namespace)
            if found is None:
                raise StoreError("Zu diesem Spiel hat Epic keine Angaben im Shop.")
            namespace, _sep, offer = found["id"].partition(":")
            card = card or found
        data = self._gql("epic-offer-%s-%s" % (namespace, offer), OFFER_QUERY, {"namespace": namespace, "id": offer}, DETAIL_TTL)
        o = (((data or {}).get("data") or {}).get("Catalog") or {}).get("catalogOffer")
        if not isinstance(o, dict) or not o.get("title"):
            raise StoreError("Zu diesem Spiel hat Epic keine Angaben.")
        o = {**o, "namespace": namespace, "id": offer}
        base = self.card(o)
        images = o.get("keyImages") or []
        shots = []
        for kind in ("GalleryImage", "Screenshot", "featuredMedia"):
            shots += [_sized(str(k["url"]), SHOT_SIZE) for k in images
                      if isinstance(k, dict) and k.get("type") == kind and k.get("url")
                      and re.search(r"\.(jpe?g|png|webp)$", str(k["url"]), re.I)]
            if shots:
                break
        release = _date(o.get("pcReleaseDate") or o.get("releaseDate"))
        tags = [t for t in o.get("tags") or [] if isinstance(t, dict)]
        out = {
            "store": "epic", "id": base["id"], "type": "game", "title": str(o["title"]), "slug": base["slug"],
            "short": clip(str(o.get("description") or "")),
            "about": strip_markdown(str(o.get("longDescription") or o.get("description") or ""))[:6000],
            "image": base["image"],
            "hero": _sized(_image(images, "OfferImageWide", "DieselStoreFrontWide", "featuredMedia"), HERO_SIZE),
            "screenshots": shots[:12], "release": release,
            "developers": [d for d in (o.get("developerDisplayName"), o.get("publisherDisplayName")) if d][:1],
            "genres": [str(t.get("name")) for t in tags if t.get("groupName") == "genre"][:4],
            "metacritic": 0, "controller": "full" if any(str(t.get("id")) == "9549" for t in tags) else "",
            "deck": "", "protondb": "", "linux": False, "windows": True, "installable": True,
            "coming_soon": bool(release) and release > time.strftime("%Y-%m-%d"),
            "size_gb": 0.0, "url": SHOP % base["slug"] if base["slug"] else "https://store.epicgames.com/de/",
        }
        card = card or {}
        out.update(price=card.get("price") or base["price"], original=card.get("original", base["original"]),
                   discount=int(card.get("discount", base["discount"]) or 0))
        out["free"] = out["price"] == "Kostenlos" or str(out["price"]).startswith("Gratis bis")
        out["buyable"] = bool(out["price"]) and not str(out["price"]).startswith("Gratis ab")
        return out


def _day(text) -> str:
    """„2026-10-23T15:00:00.000Z“ -> „23.10.“"""
    m = re.match(r"\d{4}-(\d\d)-(\d\d)", str(text or ""))
    return "%s.%s." % (m.group(2), m.group(1)) if m else "bald"


def genres() -> list[dict]:
    return [{"id": key, "label": label} for key, label, _tag in GENRES]
