"""Startanimation des Home (Bauplan E92, Wunsch 2026-10-22: „Erstelle eine Startanimation, wenn der
PC5 Launcher startet“, „animiert mit Ton“).

Hier fällt nur die Entscheidung, ob die Seite sie zeigt (der UI-Host hängt dann intro=1 an die
Adresse); die Animation selbst ist app/web/js/components/intro.js.

Gezeigt wird einmal je Game-Mode-Sitzung: beim Einschalten, bei der Rückkehr aus dem
Desktop-Modus und nach „PC5 Launcher neu starten“. Nicht nach der Rückkehr aus Steam Big Picture
und nicht, wenn der Supervisor das Home nach einem Absturz neu startet: Dann liegt im
Laufzeitordner die Marke intro-shown, jünger als die session.env, die der Supervisor zu Beginn
jeder Sitzung schreibt. Ein Neuladen der Seite im selben Prozess fragt gar nicht erst (host.py).
Einstellung ui.intro (Home-Bildschirm › Startanimation). Im Prüfmodus nur mit COUCH_TEST_INTRO=1.
"""
from __future__ import annotations

import os
from typing import Any

from . import log, paths

_log = log.get("intro")

MARK = "intro-shown"
SESSION_ENV = "session.env"         # schreibt der Supervisor einmal zu Beginn der Sitzung


def _mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def due(config: Any) -> bool:
    """Soll die Seite jetzt die Startanimation zeigen? Setzt dann zugleich die Marke."""
    if paths.test_mode() and os.environ.get("COUCH_TEST_INTRO") != "1":
        return False
    try:
        if config is not None and config.get("ui.intro", True) is False:
            return False
    except Exception:
        _log.exception("ui.intro")
    run = paths.run_dir()
    mark, session = _mtime(os.path.join(run, MARK)), _mtime(os.path.join(run, SESSION_ENV))
    if mark is not None and (session is None or mark >= session):
        return False
    try:
        os.makedirs(run, mode=0o700, exist_ok=True)
        with open(os.path.join(run, MARK), "w", encoding="utf-8") as f:
            f.write("1\n")
    except OSError as e:
        _log.warning("Marke der Startanimation nicht schreibbar: %s", e)
    return True


def forget() -> None:
    """„PC5 Launcher neu starten“: beim nächsten Start wieder zeigen."""
    try:
        os.unlink(os.path.join(paths.run_dir(), MARK))
    except FileNotFoundError:
        pass
    except OSError as e:
        _log.warning("Marke der Startanimation nicht entfernt: %s", e)
