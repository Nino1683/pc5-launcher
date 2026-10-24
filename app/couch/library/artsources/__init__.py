"""Quellen für Cover, Hintergründe, Logos und Kacheln aus dem Netz (Bauplan E34).

Jede Quelle ist ein kleines Modul mit derselben Form (``base.Source``):

    search(title, hints)  -> Kandidaten
    details(candidates)   -> Kandidaten mit Art (Spiel oder Zubehör) und Bilddaten
    images(candidate)     -> {"cover"|"hero"|"logo"|"tile": [{"url", "width", "height"}, …]}

Reihenfolge für PC-Spiele: Steam, Microsoft-Store, GOG, Epic, Battle.net (nur Hintergründe),
SteamGridDB (nur mit Schlüssel). itch.io (E100) sucht nicht nach Titeln, sondern liefert nur für eine
gezielte Wahl (fetch_candidate nach der Installation aus dem Store) den Hintergrund.
Für Switch- und Wii-U-Spiele: Nintendo, SteamGridDB. Welche Quelle je Bildart gewinnt und
welcher Kandidat als sicherer Treffer gilt, entscheidet ``artfetch.ArtFetcher`` mit ``match``.
"""
from __future__ import annotations

from . import match
from .base import FetchError, Http, Source
from .battlenet import BattlenetSource
from .epic import EpicSource
from .gog import GogSource
from .itch import ItchSource
from .msstore import MsStoreSource
from .nintendo import NintendoSource
from .sgdb import SgdbSource
from .steam import SteamSource

ORDER = ("steam", "msstore", "gog", "epic", "battlenet", "nintendo", "itch", "sgdb")
CLASSES = {"steam": SteamSource, "msstore": MsStoreSource, "gog": GogSource, "epic": EpicSource,
           "battlenet": BattlenetSource, "nintendo": NintendoSource, "itch": ItchSource, "sgdb": SgdbSource}
LABELS = {name: cls.label for name, cls in CLASSES.items()}

__all__ = ["ORDER", "CLASSES", "LABELS", "FetchError", "Http", "Source", "match", "build"]


def build(http: Http, config=None, names=ORDER, steam: SteamSource | None = None, overrides: dict | None = None) -> list:
    """Quellen in der vereinbarten Reihenfolge anlegen.

    steam      eine schon angelegte Steam-Quelle weiterbenutzen
    overrides  {name: {parameter: wert}} für Tests (Adressen einer Attrappe)
    """
    out = []
    for name in ORDER:
        if name not in names:
            continue
        extra = dict((overrides or {}).get(name) or {})
        if name == "steam" and steam is not None and not extra:
            out.append(steam)
        elif name == "sgdb":
            out.append(SgdbSource(http, config, **extra))
        else:
            out.append(CLASSES[name](http, **extra))
    return out
