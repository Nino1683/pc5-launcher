"""GOG (ohne Konto).

  Suche   catalog.gog.com/v1/catalog?query=like:…&productType=in:game,pack
          Die Antwort trägt schon alle Bilder: coverVertical (Hochkant-Cover),
          galaxyBackgroundImage (Hintergrund), logo.
  Bilder  images.gog-statics.com/<prüfsumme>.jpg|png

Die Suche liefert gern Werkzeuge und Pakete zuerst („The Witcher 3 REDkit“): Welcher Kandidat
zählt, entscheidet allein der Titelvergleich.
"""
from __future__ import annotations

import re

from .base import Source, host_of, https_url, image

CATALOG_URL = "https://catalog.gog.com/v1/catalog"
GAME_URL = "https://api.gog.com/v2/games/"
IMAGE_HOSTS = (".gog-statics.com",)
_FORMATTER = re.compile(r"_\{formatter\}")
_QUERY_JUNK = re.compile(r"[^\w' ]+", re.UNICODE)


class GogSource(Source):
    name = "gog"
    label = "GOG"
    kinds = ("cover", "hero", "logo")

    def __init__(self, http, catalog_url: str = CATALOG_URL, game_url: str = GAME_URL,
                 image_hosts: tuple = IMAGE_HOSTS):
        self.catalog_url = catalog_url
        self.game_url = game_url if game_url.endswith("/") else game_url + "/"
        self.hosts = tuple({host_of(catalog_url), host_of(self.game_url)} | set(image_hosts))
        super().__init__(http)

    def search(self, title: str, hints: dict | None = None) -> list:
        # Der Katalog lehnt Bindestriche und andere Zeichen in der Suche mit HTTP 400 ab
        # (gemessen: „Half-Life Alyx“): nur Wörter, Ziffern und Apostrophe gehen hinaus
        text = re.sub(r"\s+", " ", _QUERY_JUNK.sub(" ", title)).strip()
        if not text:
            return []
        data = self.http.get_json(self.catalog_url, {
            "limit": "10", "query": "like:" + text, "order": "desc:score",
            "productType": "in:game,pack"}, service=self.name)
        out = []
        for prod in (data.get("products") if isinstance(data, dict) else None) or []:
            if not isinstance(prod, dict) or not prod.get("id"):
                continue
            pics = {"cover": prod.get("coverVertical"), "hero": prod.get("galaxyBackgroundImage"),
                    "logo": prod.get("logo")}
            out.append(self._candidate(
                prod["id"], prod.get("title", ""), game=prod.get("productType") in ("game", "pack"),
                thumb=prod.get("coverHorizontal") or prod.get("coverVertical"),
                data={"images": {k: v for k, v in pics.items() if https_url(v)}}))
        return out

    def _lookup(self, ref: str) -> dict:
        """Bilder eines Produkts über seine Nummer (für eine gemerkte Wahl). Blockiert."""
        if not re.fullmatch(r"\d{1,12}", ref or ""):
            return {}
        data = self.http.get_json(self.game_url + ref, {"locale": "de-DE"}, service=self.name)
        links = data.get("_links") if isinstance(data, dict) else None
        if not isinstance(links, dict):
            return {}

        def href(key):
            node = links.get(key)
            value = node.get("href") if isinstance(node, dict) else None
            return _FORMATTER.sub("", value) if isinstance(value, str) else None
        pics = {"cover": href("boxArtImage"), "hero": href("galaxyBackgroundImage") or href("backgroundImage"),
                "logo": href("logo")}
        return {k: v for k, v in pics.items() if https_url(v)}

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data")
        pics = data.get("images") if isinstance(data, dict) else None
        if not isinstance(pics, dict):
            pics = self._lookup(str(candidate.get("ref") or ""))
        out = {}
        for kind in ("cover", "hero", "logo"):
            im = image(pics.get(kind))
            if im:
                out[kind] = [im]                       # Größe unbekannt: wird nach dem Laden gemessen
        return out
