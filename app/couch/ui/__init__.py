"""Fenster und Brücke zur Oberfläche (host), Prüfsteuerung (testctl), Attrappen der Dienste (fakes).

Dieses Modul selbst lädt kein GTK: Die Prüfung der Anzeige muss laufen, bevor GTK beim Import
eine Verbindung zu $DISPLAY aufbaut.
"""
from __future__ import annotations

import os


class DisplayError(RuntimeError):
    pass


def require_gamescope_display() -> str:
    """Bricht ab, wenn DISPLAY kein Gamescope-Xwayland ist. Das Home darf nie ein Fenster auf
    einem Desktop öffnen (SAFETY 13); erkannt wird Gamescope nur an GAMESCOPE_XWAYLAND_SERVER_ID
    (andere GAMESCOPE_*-Eigenschaften setzt Steam auch auf dem KDE-Desktop)."""
    name = os.environ.get("DISPLAY", "")
    if not name:
        raise DisplayError("DISPLAY ist nicht gesetzt")
    from Xlib import X, display as xdisplay
    from .. import xfix
    try:
        d = xfix.repair(xdisplay.Display(name))
    except Exception as e:
        raise DisplayError("keine Verbindung zu %s: %s" % (name, e)) from None
    try:
        prop = d.screen().root.get_full_property(
            d.intern_atom("GAMESCOPE_XWAYLAND_SERVER_ID"), X.AnyPropertyType)
    finally:
        d.close()
    if prop is None:
        raise DisplayError("%s ist kein Gamescope-Xwayland; das Home startet nur im Game-Mode" % name)
    return name
