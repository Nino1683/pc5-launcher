"""Microsoft-Store (ohne Konto; keine zugesicherte Schnittstelle, kann jederzeit ausfallen).

  Suche    www.microsoft.com/msstoreapiprod/api/autosuggest?query=…     Titel und BigCatalogId
  Angaben  displaycatalog.mp.microsoft.com/v7.0/products?bigIds=a,b     Art und Bilder mit Größe
  Bilder   store-images.s-microsoft.com/image/…                         Größe und Format per Parameter

Bildarten: Poster (1440x2160) = Cover, BoxArt (2160x2160) = Kachel, SuperHeroArt (3840x2160) =
Hintergrund. TitledHeroArt trägt den Schriftzug und ist deshalb nie Hintergrund; „Logo“ ist hier
ein quadratisches Symbol, kein freigestellter Schriftzug.
"""
from __future__ import annotations

from .base import Source, host_of, https_url, image

SUGGEST_URL = "https://www.microsoft.com/msstoreapiprod/api/autosuggest"
PRODUCTS_URL = "https://displaycatalog.mp.microsoft.com/v7.0/products"
CLIENT_ID = "7F27B536-CF6B-4C65-8638-A0F8CBDFCA65"      # die Kennung der Store-Webseite
IMAGE_HOSTS = ("store-images.s-microsoft.com", "store-images.microsoft.com")
PURPOSES = {"cover": ("Poster",), "tile": ("BoxArt",), "hero": ("SuperHeroArt",)}
MAX_SIDE = {"cover": (1440, 2160), "tile": (1080, 1080), "hero": (3840, 2160)}


class MsStoreSource(Source):
    name = "msstore"
    label = "Microsoft Store"
    kinds = ("cover", "hero", "tile")

    def __init__(self, http, suggest_url: str = SUGGEST_URL, products_url: str = PRODUCTS_URL,
                 market: str = "de-de", image_hosts: tuple = IMAGE_HOSTS):
        self.suggest_url = suggest_url
        self.products_url = products_url
        self.market = market
        self.hosts = tuple({host_of(suggest_url), host_of(products_url)} | set(image_hosts))
        super().__init__(http)

    def search(self, title: str, hints: dict | None = None) -> list:
        data = self.http.get_json(self.suggest_url, {
            "market": self.market, "clientId": CLIENT_ID, "sources": "DCatAll-Products",
            "filter": "+ClientType:StoreWeb", "counts": "10", "query": title}, service=self.name)
        out, seen = [], set()
        for group in (data.get("ResultSets") if isinstance(data, dict) else None) or []:
            for hit in (group.get("Suggests") if isinstance(group, dict) else None) or []:
                if not isinstance(hit, dict):
                    continue
                metas = {m.get("Key"): m.get("Value") for m in hit.get("Metas") or [] if isinstance(m, dict)}
                big_id = str(metas.get("BigCatalogId") or "").upper()
                if not big_id or big_id in seen:
                    continue
                if hit.get("Source") != "Game" and metas.get("ProductType") != "Games":
                    continue                           # Apps, Filme, Hardware
                seen.add(big_id)
                icon = https_url(metas.get("Icon") or hit.get("ImageUrl"))
                thumb = icon.split("?", 1)[0] + "?w=240&h=240" if icon else None
                # Upgrades und Zusatzinhalte stehen hier auch als „Game“: erst details() klärt es
                out.append(self._candidate(big_id, hit.get("Title", ""), game=None, thumb=thumb))
        return out

    def details(self, candidates: list) -> list:
        ids = [str(c.get("ref") or "") for c in candidates if c.get("ref")]
        found = {}
        if ids:
            lang = "%s-%s" % (self.market[:2], self.market[-2:].upper())
            data = self.http.get_json(self.products_url, {
                "bigIds": ",".join(ids), "market": self.market[-2:].upper(), "languages": lang},
                service=self.name)
            for prod in (data.get("Products") if isinstance(data, dict) else None) or []:
                if isinstance(prod, dict) and prod.get("ProductId"):
                    found[str(prod["ProductId"]).upper()] = prod
        for c in candidates:
            prod = found.get(str(c.get("ref") or "").upper())
            if prod is None:
                c["game"] = False
                continue
            local = next((lp for lp in prod.get("LocalizedProperties") or [] if isinstance(lp, dict)), {})
            c["game"] = prod.get("ProductKind") == "Game"
            c["name"] = str(local.get("ProductTitle") or c.get("name") or "")
            c["data"] = {"images": [
                {"purpose": im.get("ImagePurpose"), "uri": im.get("Uri"),
                 "width": im.get("Width"), "height": im.get("Height")}
                for im in local.get("Images") or [] if isinstance(im, dict)]}
        return candidates

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data")
        if not isinstance(data, dict) or "images" not in data:
            data = self.details([candidate])[0].get("data") or {}
        out = {}
        for kind, purposes in PURPOSES.items():
            found = []
            for im in data.get("images") or []:
                if im.get("purpose") not in purposes:
                    continue
                try:
                    w, h = int(im.get("width") or 0), int(im.get("height") or 0)
                except (TypeError, ValueError):
                    continue
                url = https_url(im.get("uri"))
                if not url or w < 16 or h < 16:
                    continue
                # Der Bilddienst verkleinert und wandelt selbst: nie mehr laden, als gebraucht wird,
                # und JPEG statt eines PNG von 20 MB
                max_w, max_h = MAX_SIDE[kind]
                scale = min(1.0, max_w / w, max_h / h)
                tw, th = max(1, round(w * scale)), max(1, round(h * scale))
                found.append(image("%s?w=%d&h=%d&q=90&format=jpg" % (url.split("?", 1)[0], tw, th), tw, th))
            found = [f for f in found if f]
            found.sort(key=lambda f: -(f["width"] or 0) * (f["height"] or 0))
            if found:
                out[kind] = found[:2]
        return out
