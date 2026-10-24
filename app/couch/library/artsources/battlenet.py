"""Battle.net-Shop (ohne Konto; keine zugesicherte Schnittstelle, kann jederzeit ausfallen).

Für Blizzard-Spiele, die es bei Steam, GOG und Epic nicht gibt (Hearthstone, StarCraft, Heroes of
the Storm, Diablo III …; Wunsch 2026-10-23: „Für Hearthstone werden keine Cover gefunden“). Keine
Suche, nur die Produktseite unter ihrem Kurznamen (wie der Store des PC5 Launchers sie liest,
store/battlenet.py):
  eu.shop.battle.net/de-de/product/<kurzname>       Flight-Zeilen (RSC), 404 = gibt es nicht
Kurznamen: aus dem Titel geraten („Hearthstone“ -> hearthstone) und die bekannten Kurznamen der
Spiele mit gleichem Titel (store.battlenet.SLUG_CODES, „Warcraft III: Reforged“ -> warcraft-3-reforged).
Ob die Seite zum Spiel gehört, entscheidet danach der Titelvergleich.

Bilder: nur Hintergründe (Querformat 1920x1080 aus der Galerie der Seite, sonst große Bilder der
Abschnitte). Hochkant-Cover führt der Shop nicht; das Cover setzt art.py aus dem Hintergrund
zusammen (composite), die Kachel schneidet es daraus.
"""
from __future__ import annotations

from ...store import battlenet as shop
from .base import FetchError, Source, host_of, image
from .match import norm, slugify

PRODUCT_URL = shop.PRODUCT
IMAGE_HOSTS = ("blz-contentstack-images.akamaized.net", "images.blz-contentstack.com")
PAGE_TYPES = ("text/x-component", "text/html")
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_SLUGS = 3
HERO_MIN_WIDTH = 1280
# Das Cookie spart die Umleitung zur Anmeldeprüfung (account.battle.net), RSC liefert die Daten der Seite
HEADERS = {"RSC": "1", "Cookie": "autoLoginChecked-next=1", "Accept-Language": "de-DE,de;q=0.9"}


def slugs_for(title: str) -> list:
    """Kurznamen, unter denen der Shop das Spiel führen könnte (höchstens MAX_SLUGS)."""
    want = norm(title or "")
    out = [slugify(title or "")]
    for slug, code in shop.SLUG_CODES.items():
        known = shop.CODES.get(code) or {}
        if code and norm(known.get("title") or "") == want:
            out.append(slug)
    return [s for s in dict.fromkeys(out) if s][:MAX_SLUGS]


def heroes(p: dict) -> list:
    """Querformat-Bilder der Seite, größtes zuerst: Galerie ohne Videos, dann die Abschnitte."""
    found = [g for g in p.get("gallery") or [] if isinstance(g, dict) and not g.get("video")]
    found += [{"url": f.get("image"), "w": f.get("w", 0), "h": f.get("h", 0)}
              for sec in p.get("sections") or [] for f in sec.get("features") or [] if isinstance(f, dict)]
    found = [g for g in found if shop._landscape(g, HERO_MIN_WIDTH)]
    found.sort(key=lambda g: -g["w"])                    # stabil: bei gleicher Breite die Reihenfolge der Seite
    out = []
    for g in found:
        im = image(shop.sized(g["url"], shop.HERO_WIDTH), min(g["w"], shop.HERO_WIDTH),
                   round(g["h"] * min(1.0, shop.HERO_WIDTH / g["w"])))
        if im and im["url"] not in (o["url"] for o in out):
            out.append(im)
    return out


class BattlenetSource(Source):
    name = "battlenet"
    label = "Battle.net"
    kinds = ("hero",)

    def __init__(self, http, product_url: str = PRODUCT_URL, image_hosts: tuple = IMAGE_HOSTS):
        self.product_url = product_url
        self.hosts = tuple({host_of(product_url % "x")} | set(image_hosts))
        super().__init__(http)

    def _product(self, slug: str) -> dict | None:
        try:
            body, _ctype = self.http.get(self.product_url % slug, accept=PAGE_TYPES, max_bytes=MAX_PAGE_BYTES,
                                         headers=HEADERS, service=self.name)
        except FetchError as exc:
            if exc.reason in ("http-404", "http-403", "http-410", "content-type", "redirect-foreign"):
                return None                            # diese Seite gibt es nicht: kein Fehler
            raise
        try:
            data = shop.page_product(shop.flight(body.decode("utf-8", "replace")))
        except Exception:
            return None
        return data if data.get("title") else None

    def search(self, title: str, hints: dict | None = None) -> list:
        out = []
        for slug in slugs_for(title):
            p = self._product(slug)
            if not p:
                continue
            pics = heroes(p)
            out.append(self._candidate(slug, p["title"], game=True, thumb=(pics[0]["url"] if pics else None),
                                       data={"heroes": pics}))
            break                                      # erste Seite mit Produkt genügt
        return out

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data")
        pics = data.get("heroes") if isinstance(data, dict) else None
        if not isinstance(pics, list):
            found = self._product(str(candidate.get("ref") or ""))
            pics = heroes(found) if found else []
        return {"hero": pics} if pics else {}
