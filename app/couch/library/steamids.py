"""Kennungen der Steam-Verknüpfungen (E25), an einer Stelle für Quelle, Beschreibung und Starter.

Absichtlich ohne Abhängigkeiten (kein vdf, keine anderen Module der Bibliothek): launch/__init__.py nimmt das von
hier, und der Starter soll vdf nicht laden, das braucht nur library/steam.py zum Lesen von shortcuts.vdf.

  Eintrag     "steam:shortcut:<appid>"
  Spielkennung (appid << 32) | 0x02000000 für steam://rungameid/…; die appid steht in shortcuts.vdf
              vorzeichenbehaftet, gemeint sind ihre unteren 32 Bit
"""
from __future__ import annotations

SHORTCUT_PREFIX = "steam:shortcut:"
SHORTCUT_FLAG = 0x02000000          # untere Hälfte der Spielkennung einer Verknüpfung


def is_shortcut(entry) -> bool:
    """Ist der Eintrag eine Steam-Verknüpfung (Kennung "steam:shortcut:…")?"""
    return str(getattr(entry, "id", "") or "").startswith(SHORTCUT_PREFIX)


def shortcut_gameid(appid: int) -> int:
    """Die Nummer für steam://rungameid/… einer Verknüpfung: (appid << 32) | 0x02000000."""
    return ((int(appid) & 0xFFFFFFFF) << 32) | SHORTCUT_FLAG
