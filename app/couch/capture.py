"""Bildschirmfoto mit dem Controller (Wunsch 2026-10-21, wie die Create-Taste an der Konsole).

Auslöser (padlogic): PS halten und Create drücken („create“) oder, mit input.capture_button =
"create", Create allein („create-solo“). Am PC sieht ein Spiel die Create-Taste trotzdem (als
Select/Back, oft Karte oder Menü); deshalb ist PS + Create die Vorgabe. Im Zeiger-Modus (E99) ist
„Create halten + Steuerkreuz/L1/R1“ das Merken einer Sprungstelle: Dort meldet der Eingabedienst
„create-solo“ erst beim Loslassen von Create und gar nicht, wenn dabei eine Stelle gemerkt wurde.

Aufgenommen wird von Gamescope selbst (`gamescopectl screenshot <pfad>`: das ganze Bild, wie es am
Fernseher steht, in voller Auflösung). Gamescope schreibt die Datei kurz danach; erst wenn sie da
ist, kommt die Mitteilung. Abgelegt im Bilder-Ordner nach XDG (E104, paths.user_dir_at) unter
„Bildschirmfotos“ (bzw. „Screenshots“, wenn der Bilder-Ordner englisch „Pictures“ heißt) als
„<Titel> <Datum Uhrzeit>.png“; z. B. ~/Bilder/Bildschirmfotos. Der Explorer zeigt den Ordner als
Ort „Bildschirmfotos“. Im Prüfmodus liegt er im Käfig (<COUCH_TEST_ROOT>/home, E99).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from . import log, paths

_log = log.get("capture")

DEBOUNCE_S = 1.0            # beide Wege zugleich (Create vor PS gedrückt) ergeben ein Foto
WAIT_S = 4.0
MODES = ("ps-create", "create")


def folder() -> str:
    """Ordner der Bildschirmfotos. Im Prüfmodus unter <COUCH_TEST_ROOT>/home: Ein Prüflauf außerhalb der
    Szenarien schriebe sonst ins echte ~/Bilder des Nutzers (E99). Fehlt der Bilder-Ordner ganz, legt ihn die Konsole
    deutsch an (missing="de", Oberfläche deutsch)."""
    pictures = paths.user_dir_at("PICTURES", missing="de")
    sub = "Screenshots" if os.path.basename(pictures.rstrip(os.sep)) == "Pictures" else "Bildschirmfotos"
    return os.path.join(pictures, sub)


def file_name(title: str, when: float) -> str:
    """„Alan Wake 2 2026-10-21 13-05-12.png“: ohne Zeichen, die Dateinamen stören."""
    clean = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', "", str(title or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" .") or "Home"
    return "%s %s.png" % (clean[:80].rstrip(" ."), time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(when)))


class Capture:
    """config.get; front_title() -> Titel der App vorn oder None (Home); run_write wie
    system.run_write; notify(title, body, ok); background(fn, *args) startet einen Arbeits-Thread."""

    def __init__(self, config: Any, front_title: Callable[[], str | None], run_write: Callable[..., Any],
                 notify: Callable[[str, str, bool], None], background: Callable[..., Any],
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.config, self.front_title, self.run_write = config, front_title, run_write
        self.notify, self.background = notify, background
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self._last = -1e9

    def on_gesture(self, kind: str) -> bool:
        """Hauptschleife. Liefert True, wenn ein Foto ausgelöst wurde."""
        mode = self.config.get("input.capture_button", "ps-create")
        if kind == "create-solo" and mode != "create":
            return False                        # Create allein gehört dem Spiel bzw. der Oberfläche
        now = self.clock()
        if now - self._last < DEBOUNCE_S:
            return False
        self._last = now
        self.background(self.take, self.front_title() or "Home")
        return True

    def take(self, title: str) -> str | None:
        """Arbeits-Thread: aufnehmen und auf die Datei warten. Liefert den Pfad oder None."""
        target = folder()
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            _log.warning("Bildschirmfoto: Ordner %s: %s", target, e)
            self.notify("Bildschirmfoto hat nicht geklappt", "Der Ordner für Bildschirmfotos lässt sich nicht anlegen.", False)
            return None
        base = os.path.join(target, file_name(title, self.wall()))[:-4]
        path, n = base + ".png", 2
        while os.path.lexists(path):
            path, n = "%s (%d).png" % (base, n), n + 1
        r = self.run_write(["gamescopectl", "screenshot", path], timeout=5, note="capture")
        waited = 0.0
        while r.returncode == 0 and waited < WAIT_S:
            try:
                if os.path.getsize(path) > 0:
                    _log.info("Bildschirmfoto: %s", path)
                    self.notify("Bildschirmfoto gespeichert", "Dateien › Bildschirmfotos · " + os.path.basename(path), True)
                    return path
            except OSError:
                pass
            self.sleep(0.1)
            waited += 0.1
        _log.warning("Bildschirmfoto: keine Datei (rc=%s) %s", r.returncode, (getattr(r, "stderr", "") or "").strip())
        self.notify("Bildschirmfoto hat nicht geklappt", "Gamescope hat kein Bild gespeichert.", False)
        return None
