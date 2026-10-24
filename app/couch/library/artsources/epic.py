"""Epic Games Store (ohne Konto; keine zugesicherte Schnittstelle, kann jederzeit ausfallen).

Es gibt keine Suche, nur die Produktseite unter ihrem Kurznamen:
  store-content.ak.epicgames.com/api/de/content/products/<kurzname>     404 = gibt es nicht
Der Kurzname wird aus dem Titel geraten („Alan Wake 2“ -> alan-wake-2); ob die Seite wirklich
zum Spiel gehört, entscheidet danach der Titelvergleich mit ihrem productName.

Bilder der Startseite des Produkts: hero.portraitBackgroundImageUrl (hochkant, 1200x1600),
hero.backgroundImageUrl (2560x1440), hero.logoImage (klein). Die Größe steht im Dateinamen.
"""
from __future__ import annotations

import re

from .base import FetchError, Source, host_of, image
from .match import slugify

PRODUCT_URL = "https://store-content.ak.epicgames.com/api/de/content/products/"
IMAGE_HOSTS = ("cdn1.epicgames.com", "cdn2.epicgames.com", "cdn1.unrealengine.com", "cdn2.unrealengine.com")
_SIZE = re.compile(r"-(\d{2,5})x(\d{2,5})-[0-9a-f]{6,}\.(?:jpe?g|png)$", re.I)
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _sized(url) -> dict | None:
    m = _SIZE.search(url or "") if isinstance(url, str) else None
    return image(url, *(m.groups() if m else (None, None)))


class EpicSource(Source):
    name = "epic"
    label = "Epic Games"
    kinds = ("cover", "hero", "logo")

    def __init__(self, http, product_url: str = PRODUCT_URL, image_hosts: tuple = IMAGE_HOSTS):
        self.product_url = product_url if product_url.endswith("/") else product_url + "/"
        self.hosts = tuple({host_of(self.product_url)} | set(image_hosts))
        super().__init__(http)

    def _product(self, slug: str) -> dict | None:
        if not _SLUG.fullmatch(slug or "") or len(slug) > 80:
            return None
        try:
            data = self.http.get_json(self.product_url + slug, service=self.name)
        except FetchError as exc:
            if exc.reason in ("http-404", "http-403", "http-410", "content-type"):
                return None                            # diese Seite gibt es nicht: kein Fehler
            raise
        return data if isinstance(data, dict) else None

    @staticmethod
    def _pictures(data: dict) -> dict:
        pages = [p for p in data.get("pages") or [] if isinstance(p, dict)]
        page = next((p for p in pages if p.get("_slug") == "home" or p.get("type") == "productHome"), None)
        page = page or (pages[0] if pages else data)
        hero = ((page.get("data") or {}).get("hero") or {}) if isinstance(page.get("data"), dict) else {}
        logo = hero.get("logoImage") if isinstance(hero.get("logoImage"), dict) else {}
        pics = {"cover": hero.get("portraitBackgroundImageUrl"), "hero": hero.get("backgroundImageUrl"),
                "logo": logo.get("src")}
        return {k: v for k, v in pics.items() if isinstance(v, str) and v}

    def search(self, title: str, hints: dict | None = None) -> list:
        slug = slugify(title)
        data = self._product(slug)
        if not data:
            return []
        name = data.get("productName") or data.get("_title") or ""
        pics = self._pictures(data)
        return [self._candidate(slug, name, game=True, thumb=pics.get("cover") or pics.get("hero"),
                                data={"images": pics})]

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data")
        pics = data.get("images") if isinstance(data, dict) else None
        if not isinstance(pics, dict):
            found = self._product(str(candidate.get("ref") or ""))
            pics = self._pictures(found) if found else {}
        out = {}
        for kind in ("cover", "hero", "logo"):
            im = _sized(pics.get(kind))
            if im:
                out[kind] = [im]
        return out
