"""Nintendo of Europe (ohne Konto), nur für Switch- und Wii-U-Spiele.

  Suche   searching.nintendo-europe.com/de/select?q=…&fq=type:GAME
          Je Spiel: title, System, image_url_sq_s (quadratisch) und image_url_h2x1_s (2:1).
  Bilder  www.nintendo.com/eu/media/images/…  Der Zusatz „_image500w“ im Dateinamen ist die kleine
          Fassung; ohne ihn gibt es die große (gemessen: quadratisch 1000x1000, 2:1 2000x1000).

Dieselbe Suche nennt alle Systeme durcheinander („Mario Kart 8“ für Wii U vor „Mario Kart 8
Deluxe“): Jeder Kandidat trägt deshalb seine Systeme, der Titelvergleich wählt danach.
"""
from __future__ import annotations

import re

from .base import Source, host_of, https_url, image

SEARCH_URL = "https://searching.nintendo-europe.com/de/select"
IMAGE_HOSTS = ("www.nintendo.com", "fs-prod-cdn.nintendo-europe.com", "www.nintendo.eu")
_SMALL = re.compile(r"_image\d+w(?=\.[A-Za-z]{3,4}$)")
_QUERY_JUNK = re.compile(r"[^\w' ]+", re.UNICODE)
# Kennungen der Geräte in originally_for_t / playable_on_txt
_SYSTEM_CODES = {"HAC": "switch", "WUP": "wiiu"}


def _systems(doc: dict) -> list:
    out = []
    code = _SYSTEM_CODES.get(str(doc.get("originally_for_t") or "").upper())
    if code:
        out.append(code)
    kinds = " ".join(str(s) for s in doc.get("system_type") or []) if isinstance(doc.get("system_type"), list) \
        else str(doc.get("system_type") or "")
    for key, name in (("nintendoswitch_", "switch"), ("wiiu_", "wiiu")):
        if key in kinds and name not in out:
            out.append(name)
    return out or ["other"]


def _sizes(url) -> list:
    """Große Fassung zuerst, die kleine als Rückfall."""
    url = https_url(url)
    if not url:
        return []
    big = _SMALL.sub("", url)
    return [im for im in ([image(big)] if big != url else []) + [image(url)] if im]


class NintendoSource(Source):
    name = "nintendo"
    label = "Nintendo"
    systems = ("switch", "wiiu")
    kinds = ("tile", "hero")

    def __init__(self, http, search_url: str = SEARCH_URL, image_hosts: tuple = IMAGE_HOSTS):
        self.search_url = search_url
        self.hosts = tuple({host_of(search_url)} | set(image_hosts))
        super().__init__(http)

    def search(self, title: str, hints: dict | None = None) -> list:
        # Die Suche ist ein Solr: Doppelpunkt, Klammern und Striche wären dort Befehle
        text = re.sub(r"\s+", " ", _QUERY_JUNK.sub(" ", title)).strip()
        if not text:
            return []
        data = self.http.get_json(self.search_url, {"q": text, "fq": "type:GAME", "rows": "10", "wt": "json"},
                                  service=self.name)
        docs = ((data.get("response") if isinstance(data, dict) else None) or {}).get("docs") or []
        out = []
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get("fs_id") or not doc.get("title"):
                continue
            out.append(self._candidate(
                doc["fs_id"], doc["title"], game=doc.get("type", "GAME") == "GAME",
                thumb=doc.get("image_url_sq_s") or doc.get("image_url_h2x1_s"), systems=_systems(doc),
                data={"images": {"tile": doc.get("image_url_sq_s"), "hero": doc.get("image_url_h2x1_s")}}))
        return out

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data")
        pics = data.get("images") if isinstance(data, dict) else None
        if not isinstance(pics, dict):
            # gemerkte Wahl ohne Bilddaten: über den Namen neu suchen und die Nummer wiederfinden
            again = [c for c in self.search(str(candidate.get("name") or ""))
                     if c.get("ref") == str(candidate.get("ref"))]
            pics = (again[0]["data"]["images"] if again else {})
        out = {}
        for kind in ("tile", "hero"):
            found = _sizes(pics.get(kind))
            if found:
                out[kind] = found
        return out
