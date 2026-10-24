"""Reparatur für python-xlib 0.33: Xlib.ext.randr.init trägt seine Fehlerklassen unter den rohen
Nummern 0, 1 und 2 ein, ohne den first_error der Erweiterung. Auf jedem Gamescope-Xwayland (es hat
RandR) gilt damit der gewöhnliche X-Fehler BadValue (2) als BadRRModeError und BadRequest (1) als
BadRRCrtcError. Beide erben nicht von XError: Beim Auswerten wirft python-xlib AttributeError
("… has no attribute 'sequence_number'") mitten im Lesen, die Antwort auf die laufende Anfrage kommt
nie an. Gesehen am 2026-10-22 um 20:21: ein BadValue beim Fokuswechsel, das Home hing 25 s, der
Supervisor startete es neu. repair() stellt nach dem Öffnen die Klassen der Kernfehler wieder her.
"""
from __future__ import annotations

from typing import Any


def repair(d: Any) -> Any:
    """Für ein frisch geöffnetes Xlib.display.Display; gibt es zurück."""
    from Xlib import error
    classes = getattr(getattr(d, "display", None), "error_classes", None)
    if isinstance(classes, dict):
        classes.update(error.xerror_class)
    return d
