"""Steam-Store und Steam-CDN (ohne Schlüssel).

  Suche     store.steampowered.com/api/storesearch/?term=…           Name und App-ID
  Angaben   api.steampowered.com/IStoreBrowseService/GetItems/v1     Art (Spiel/DLC) und Dateinamen
  Bilder    shared.akamai.steamstatic.com/store_item_assets/…        die doppelt großen Fassungen

Gemessen am 2026-10-14: „library_capsule_2x“ (Datei library_600x900_2x.jpg) ist 600x900, eine
größere Hochkant-Fassung bietet Steam nicht an; „library_hero_2x“ ist 3840x1240, bei älteren
Spielen gibt es nur 1920x620. Das Logo nennt GetItems nicht: Es liegt bei älteren Spielen unter
dem festen Namen logo_2x.png, bei neueren nur unter dem Namen aus Steams appinfo.
"""
from __future__ import annotations

import json

from . import match
from .base import Source, host_of, image

SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
CDN_URL = "https://shared.akamai.steamstatic.com/store_item_assets/"

# Unser Name -> Schlüssel in der Antwort von GetItems, die beste Fassung zuerst
ASSET_KEYS = {
    "cover": ("library_capsule_2x", "library_capsule"),            # 600x900 / 300x450
    "hero": ("library_hero_2x", "library_hero"),                   # 3840x1240 / 1920x620
    "header": ("header_2x", "header", "main_capsule"),             # 920x430 / 460x215 / 616x353
    "background": ("raw_page_background", "page_background"),      # 1438x810
}
# Nenngrößen (gemessen wird nach dem Laden): damit eine zu kleine Fassung gar nicht erst geholt wird
NOMINAL = {"library_capsule_2x": (600, 900), "library_capsule": (300, 450),
           "library_hero_2x": (3840, 1240), "library_hero": (1920, 620),
           "header_2x": (920, 430), "header": (460, 215), "main_capsule": (616, 353)}
# Das Logo steht nicht in der Antwort; bei älteren Spielen liegt es unter dem festen Namen.
LEGACY_NAMES = {"logo": ("logo_2x.png", "logo.png")}
GAME_TYPE = 0


class SteamSource(Source):
    name = "steam"
    label = "Steam"
    kinds = ("cover", "hero", "logo")

    def __init__(self, http, search_url: str = SEARCH_URL, items_url: str = ITEMS_URL, cdn_url: str = CDN_URL,
                 language: str = "german", country: str = "DE", second_language: str = "english"):
        self.search_url = search_url
        self.items_url = items_url
        self.cdn_url = cdn_url if cdn_url.endswith("/") else cdn_url + "/"
        self.language = language
        self.second_language = second_language if second_language != language else ""
        self.country = country
        self.hosts = tuple({host_of(u) for u in (search_url, items_url, self.cdn_url)} | {".steamstatic.com"})
        super().__init__(http)

    # -------------------------------------------------------------- Netz

    def search(self, title: str, hints: dict | None = None) -> list:
        """Der deutsche Store nennt manche Spiele deutsch („Baphomets Fluch“ statt „Broken
        Sword“). Passt kein einziger Name auch nur entfernt zu den Suchbegriffen, geht dieselbe
        Suche deshalb noch einmal englisch hinaus."""
        found = self._search(title, self.language)
        terms = [t for t in (hints or {}).get("terms") or [title] if t]
        if self.second_language and not any(match.relate(t, c["name"])[0] for c in found for t in terms):
            # dieselbe App unter ihrem englischen Namen ersetzt die deutsche Zeile
            merged = {c["ref"]: c for c in found}
            merged.update({c["ref"]: c for c in self._search(title, self.second_language)})
            found = list(merged.values())
        return found

    def _search(self, title: str, language: str) -> list:
        data = self.http.get_json(self.search_url, {"term": title, "l": language, "cc": self.country},
                                  service=self.name)
        out, seen = [], set()
        for it in (data.get("items") if isinstance(data, dict) else None) or []:
            if not isinstance(it, dict) or it.get("type") != "app" or "id" not in it:
                continue
            try:
                appid = int(it["id"])
            except (TypeError, ValueError):
                continue
            if appid in seen:
                continue
            seen.add(appid)
            # Die Suche nennt auch DLC und Musik als "app": erst GetItems sagt, was ein Spiel ist
            out.append(self._candidate(appid, it.get("name", ""), game=None, thumb=it.get("tiny_image"),
                                       lang=language))
        return out

    def store_items(self, appids: list, language: str | None = None) -> list:
        """GetItems für App-IDs: Liste der Antwort-Einträge (name, type, assets …). Blockiert."""
        req = {"ids": [{"appid": int(i)} for i in appids],
               "context": {"language": language or self.language, "country_code": self.country},
               "data_request": {"include_assets": True, "include_basic_info": True}}
        data = self.http.get_json(self.items_url, {"input_json": json.dumps(req, separators=(",", ":"))},
                                  service=self.name)
        items = ((data.get("response") if isinstance(data, dict) else None) or {}).get("store_items") or []
        return [it for it in items if isinstance(it, dict)]

    def details(self, candidates: list) -> list:
        by_lang = {}
        for c in candidates:
            try:
                by_lang.setdefault(c.get("lang") or self.language, []).append(int(c["ref"]))
            except (KeyError, TypeError, ValueError):
                c["game"] = False
        found = {}
        for language, ids in by_lang.items():
            # in der Sprache der Suche fragen: sonst hieße der Kandidat danach wieder anders
            for it in self.store_items(ids, language):
                try:
                    found[int(it.get("appid", 0) or 0)] = it
                except (TypeError, ValueError):
                    continue
        for c in candidates:
            try:
                it = found.get(int(c["ref"]))
            except (KeyError, TypeError, ValueError):
                it = None
            if it is None or not it.get("success"):
                c["game"] = False
                continue
            c["game"] = it.get("type") == GAME_TYPE
            c["name"] = str(it.get("name") or c.get("name") or "")
            c["data"] = {"item": it}
        return candidates

    def by_appid(self, appid: int, extra_names: dict | None = None) -> dict | None:
        """Kandidat für ein Steam-Spiel der Bibliothek (keine Suche nötig). Blockiert."""
        for it in self.store_items([appid]):
            if it.get("success") and int(it.get("appid", 0) or 0) == int(appid):
                return self._candidate(appid, it.get("name", ""), game=True,
                                       data={"item": it, "extra_names": extra_names})
        if extra_names and any(extra_names.values()):
            # Aus dem Store genommene Spiele kennt GetItems nicht mehr, ihre Bilder liegen aber
            # oft noch unter den Namen aus appinfo
            return self._candidate(appid, "", game=True,
                                   data={"item": {"appid": int(appid)}, "extra_names": extra_names})
        return None

    # -------------------------------------------------------------- Adressen

    def asset_urls(self, item: dict, extra_names: dict | None = None) -> dict:
        """Bild-Adressen eines GetItems-Eintrags: {art: [adresse, …]}, die beste Fassung zuerst."""
        return {kind: [im["url"] for im in imgs] for kind, imgs in self._assets(item, extra_names).items()}

    def _assets(self, item: dict, extra_names: dict | None = None) -> dict:
        assets = item.get("assets") or {}
        fmt = assets.get("asset_url_format") or ""
        appid = item.get("appid") or item.get("id")
        out = {}
        for kind, keys in ASSET_KEYS.items():
            imgs = [image(self.cdn_url + fmt.replace("${FILENAME}", assets[k]), *NOMINAL.get(k, (None, None)))
                    for k in keys if assets.get(k) and fmt]
            if imgs:
                out[kind] = imgs
        names = dict(LEGACY_NAMES)
        for kind, wanted in (extra_names or {}).items():
            names[kind] = tuple(wanted) + tuple(n for n in names.get(kind, ()) if n not in wanted)
        if appid:
            for kind, wanted in names.items():
                have = out.setdefault(kind, [])
                known = {im["url"].split("?", 1)[0] for im in have}
                for name in wanted:
                    url = "%ssteam/apps/%d/%s" % (self.cdn_url, int(appid), name)
                    if name and url not in known:
                        known.add(url)
                        have.append(image(url))
        return {kind: [im for im in imgs if im] for kind, imgs in out.items()}

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data") or {}
        item = data.get("item")
        if not isinstance(item, dict):
            found = self.details([candidate])[0]
            item = (found.get("data") or {}).get("item")
            if not isinstance(item, dict):
                return {}
        got = self._assets(item, data.get("extra_names"))
        return {kind: got[kind] for kind in ("cover", "hero", "logo", "header") if got.get(kind)}

    def thumb(self, candidate: dict) -> str | None:
        item = (candidate.get("data") or {}).get("item")
        if isinstance(item, dict):
            header = self._assets(item).get("header") or []
            small = [im for im in header if im.get("width") == 460] or header
            if small:
                return small[0]["url"]
        return candidate.get("thumb_url")
