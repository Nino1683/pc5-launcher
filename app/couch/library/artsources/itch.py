"""itch.io (Bauplan E100): Bilder für Spiele aus dem itch.io-Store des PC5 Launchers.

Gezielte Wahl (ArtFetcher.fetch_candidate nach der Installation): ref = Karten-ID "<benutzer>/<slug>",
die Bildadressen stehen in candidate["data"] (aus owned-keys bzw. dem Store).
Titelsuche nur mit Anmeldung (Wunsch 2026-10-24: „Es sollen auch die Cover von itch geholt werden“, etwa
für ein Spiel aus einem heruntergeladenen Archiv): Die Webseiten liegen hinter Cloudflare, die Suche
der API (/search/games) geht mit dem Schlüssel der Anmeldung (am echten Konto gesehen). wiring_itch
setzt dafür SEARCH und READY; ohne Anmeldung zählt die Quelle für die Titelsuche nicht
(searchable, artfetch.sources_for), damit kein „nichts gefunden“ für 7 Tage hängen bleibt.
Kandidaten:
    {"cover": "https://img.itch.zone/…/315x250%23c/….png", "still_cover": "…", "covers": [weitere]}
Bilder: img.itch.zone, Adressen wie geliefert (Größe und Signatur gehören zusammen). GIFs fallen
weg (die Bildpipeline nimmt nur JPEG/PNG); dafür gibt es still_cover_url.

Das Cover von itch.io ist querformatig (315x250 bzw. 630x500, 1,26:1): Die Quelle liefert es als
Hintergrund ("hero"); Cover und Kachel setzt art.py daraus zusammen wie bei Battle.net (E97).
hero_min_ratio sagt dem Abrufer, dass diese Form hier als Hintergrund zählt (sonst ab 1,3:1).
"""
from __future__ import annotations

from ...store import itch
from .base import FetchError, Source, https_url, image

IMAGE_HOSTS = ("img.itch.zone",)
MAX_IMAGES = 3
MAX_HITS = 5

# Titelsuche über die Anmeldung (wiring_itch): SEARCH(titel) -> [spiel der API], READY() -> bool
SEARCH = None
READY = None


def _gif(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".gif")


class ItchSource(Source):
    name = "itch"
    label = "itch.io"
    kinds = ("hero",)
    systems = ()                                # PC-Spiele; Titelsuche nur angemeldet (searchable)
    hero_min_ratio = 1.2

    def __init__(self, http, image_hosts: tuple = IMAGE_HOSTS):
        self.hosts = tuple(image_hosts)
        super().__init__(http)

    def searchable(self) -> bool:
        try:
            return SEARCH is not None and READY is not None and bool(READY())
        except Exception:                       # noqa: BLE001  (Konto nicht lesbar: keine Suche)
            return False

    def search(self, title: str, hints: dict | None = None) -> list:
        if not self.searchable():
            return []
        try:
            games = SEARCH(title) or []
        except Exception as e:                  # ItchError (Netz, Bremse): später noch einmal
            raise FetchError("itch.io-Suche", transient=bool(getattr(e, "transient", True))) from None
        out = []
        for g in games[:MAX_HITS]:
            ref = itch.id_from_url(g.get("url"))
            cover, still = https_url(g.get("cover_url")), https_url(g.get("still_cover_url"))
            if not ref or not (cover or still):
                continue
            thumb = still if cover and _gif(cover) else (cover or still)
            out.append(self._candidate(ref, str(g.get("title") or ""), game=True, thumb=thumb,
                                       data={"cover": cover, "still_cover": still}))
        return out

    def images(self, candidate: dict, kinds=None) -> dict:
        data = candidate.get("data") if isinstance(candidate, dict) else None
        if not isinstance(data, dict):
            return {}                           # ohne Adressen nichts zu holen (kein Nachschlagen)
        urls = [data.get("cover"), data.get("still_cover")]
        more = data.get("covers")
        if isinstance(more, list):
            urls += more
        out = []
        for url in urls:
            url = https_url(url)
            if not url or _gif(url) or url in (o["url"] for o in out):
                continue
            im = image(url)
            if im:
                out.append(im)                  # Größe unbekannt: wird nach dem Laden gemessen
        return {"hero": out[:MAX_IMAGES]} if out else {}
