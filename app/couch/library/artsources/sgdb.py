"""SteamGridDB (API v2), NUR mit eigenem Schlüssel aus der Einstellung ``library.sgdb_key``.

Ohne Schlüssel geht keine einzige Anfrage hinaus. Der Schlüssel steht ausschließlich in der
Kopfzeile ``Authorization`` der Anfragen an www.steamgriddb.com: nie in einer Adresse, einem
Fehlertext, einem Ereignis oder im Protokoll.

  Suche    /api/v2/search/autocomplete/<begriff>      -> data[{id, name, types, verified}]
  Cover    /api/v2/grids/game/<id>?dimensions=600x900,660x930
  Kachel   /api/v2/grids/game/<id>?dimensions=1024x1024,512x512
  Hinterg. /api/v2/heroes/game/<id>?dimensions=3840x1240,1920x620
  Logo     /api/v2/logos/game/<id>
  jeweils  -> data[{url, thumb, width, height, style, score, mime, nsfw, humor, language}]

Auswahl: nur unbewegte Bilder ohne nsfw/humor; Stile ohne verfälschten Schriftzug zuerst
(„alternate“ vor „blurred“, „material“, „white_logo“, „no_logo“), dann die Bewertung.
Stand: nach der öffentlichen Beschreibung der Schnittstelle gebaut und nur gegen eine Attrappe
geprüft (ohne Schlüssel lässt sich der echte Dienst nicht abfragen).
"""
from __future__ import annotations

import re
from urllib.parse import quote

from .base import FetchError, Source, host_of, image

API_URL = "https://www.steamgriddb.com/api/v2/"
IMAGE_HOSTS = ("cdn2.steamgriddb.com", "cdn.steamgriddb.com")
CONFIG_KEY = "library.sgdb_key"
_KEY_FORM = re.compile(r"[A-Za-z0-9]{16,128}")

GRID_STYLES = ("alternate", "blurred", "material", "white_logo", "no_logo")
HERO_STYLES = ("alternate", "blurred", "material")
LOGO_STYLES = ("official", "white", "black", "custom")
REQUESTS = {
    "cover": ("grids", {"dimensions": "600x900,660x930"}, GRID_STYLES),
    "tile": ("grids", {"dimensions": "1024x1024,512x512"}, GRID_STYLES),
    "hero": ("heroes", {"dimensions": "3840x1240,1920x620"}, HERO_STYLES),
    "logo": ("logos", {"mimes": "image/png"}, LOGO_STYLES),
}


class SgdbSource(Source):
    name = "sgdb"
    label = "SteamGridDB"
    systems = ("*",)                    # Spiele aller Art, auch Konsolenspiele
    kinds = ("cover", "hero", "logo", "tile")

    def __init__(self, http, config=None, api_url: str = API_URL, image_hosts: tuple = IMAGE_HOSTS):
        self.config = config
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self.hosts = tuple({host_of(self.api_url)} | set(image_hosts))
        super().__init__(http)

    def _key(self) -> str:
        try:
            value = self.config.get(CONFIG_KEY, "") if self.config is not None else ""
        except Exception:                       # eine kaputte Einstellung darf den Abruf nicht sprengen
            value = ""
        value = value.strip() if isinstance(value, str) else ""
        return value if _KEY_FORM.fullmatch(value) else ""

    def available(self) -> bool:
        return bool(self._key())

    def _get(self, path: str, params: dict | None = None) -> list:
        key = self._key()
        if not key:
            # ohne Schlüssel keine Anfrage, auch nicht „zum Probieren“
            raise FetchError("no-key")
        try:
            data = self.http.get_json(self.api_url + path, params,
                                      headers={"Authorization": "Bearer " + key}, service=self.name)
        except FetchError as exc:
            if exc.reason in ("http-401", "http-403"):
                raise FetchError("key-rejected") from None
            if exc.reason == "http-404":
                return []
            raise
        if not isinstance(data, dict) or not data.get("success"):
            return []
        rows = data.get("data")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def search(self, title: str, hints: dict | None = None) -> list:
        term = quote(title.replace("/", " "), safe="")
        if not term:
            return []
        out = []
        for row in self._get("search/autocomplete/" + term)[:10]:
            if row.get("id") is None or not row.get("name"):
                continue
            out.append(self._candidate(row["id"], row["name"], game=True))
        return out

    def images(self, candidate: dict, kinds=None) -> dict:
        ref = str(candidate.get("ref") or "")
        if not re.fullmatch(r"\d{1,12}", ref):
            return {}
        out = {}
        for kind in (kinds or self.kinds):
            if kind not in REQUESTS:
                continue
            path, extra, styles = REQUESTS[kind]
            params = {"types": "static", "nsfw": "false", "humor": "false", "epilepsy": "false"}
            params.update(extra)
            rows = self._get("%s/game/%s" % (path, ref), params)
            good = []
            for row in rows:
                if row.get("nsfw") or row.get("humor") or row.get("epilepsy"):
                    continue
                if str(row.get("mime") or "image/png") not in ("image/png", "image/jpeg"):
                    continue
                im = image(row.get("url"), row.get("width"), row.get("height"))
                if not im:
                    continue
                style = str(row.get("style") or "")
                rank = styles.index(style) if style in styles else len(styles)
                lang = 0 if str(row.get("language") or "en") in ("de", "en") else 1
                try:
                    votes = float(row.get("score") or 0)
                except (TypeError, ValueError):
                    votes = 0.0
                im["thumb"] = row.get("thumb")
                good.append((rank, lang, -votes, len(good), im))
            good.sort(key=lambda g: g[:4])
            if good:
                out[kind] = [g[4] for g in good[:3]]
        return out

    def thumb(self, candidate: dict) -> str | None:
        return candidate.get("thumb_url")
