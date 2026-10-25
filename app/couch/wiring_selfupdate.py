"""Selbst-Update von GitHub (E116): Verdrahtung, eingehängt über wiring.PARTS (als letzter Teil).

  wiring.self_update      selfupdate.SelfUpdate; die Einstellungen holen ihn spät über deps.extra["late"]
                          (settings/system.py, Seite system.launcher unter Updates)
  Suche                   FIRST_S nach dem Bereit-Signal, dann stündlich nachsehen, ob 24 h um sind (nach der
                          Wanduhr, selfupdate.check): Der Zeitgeber läuft auf der monotonen Uhr und steht im
                          Ruhemodus. Nie, solange ein Spiel startet (shell.running_view: launching), und nur mit
                          update.check an. Im Prüfmodus ohne eingesteckte Netzfunktion (wiring.selfupdate_open)
                          gar nicht.
  Mitteilung              einmal je Fassung („PC5 Launcher X.Y.Z ist da“); gemerkt in <state>/selfupdate.json
  Punkt am Zahnrad        wiring_settings.push_update_badge zählt eine neue Fassung mit
  busy_checks             solange geladen oder installiert wird, fragen Ausschalten und Neustart nach
  Laden und Installieren  im eigenen Thread (bis zu 15 Minuten): Der gemeinsame Pool bleibt frei
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from . import log, paths, selfupdate

_log = log.get("wiring.selfupdate")

FIRST_S = 300.0          # erste Suche fünf Minuten nach dem Start: dann ist das Netz da und der Start vorbei
TICK_S = 3600.0          # danach stündlich nachsehen, ob ein Tag um ist
LAUNCH_WAIT_S = 600.0    # startet gerade ein Spiel: so viel später noch einmal
PAGES = ("system.launcher", "system.apps", "system.software")


def _thread(fn: Callable, *args: Any) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, name="couch-selfupdate", daemon=True)
    t.start()
    return t


def _badge(wiring: Any) -> None:
    """Arbeits-Thread: Punkt am Zahnrad neu (zählt Apps, Proton, Spiele und jetzt auch den PC5 Launcher)."""
    try:
        from . import wiring_settings
        wiring_settings.push_update_badge(wiring)
    except Exception:
        _log.exception("Update-Marke")


def launching(app: Any) -> bool:
    """Startet gerade ein Spiel oder eine App? Dann wartet die Suche (Hauptschleife)."""
    view = getattr(getattr(app, "shell", None), "running_view", None)
    try:
        return any(r.get("launching") for r in (view() if callable(view) else []) if isinstance(r, dict))
    except Exception:
        _log.exception("running_view")
        return True                 # im Zweifel später


def extend(wiring: Any) -> None:
    app = wiring.app
    injected = getattr(wiring, "selfupdate_open", None)       # Tests und Prüfläufe: Netz-Attrappe

    def show() -> None:
        tree = getattr(wiring, "tree", None)
        if tree is not None:
            for page in PAGES:
                tree.changed(page)

    svc = selfupdate.SelfUpdate(
        open_url=injected, notify=lambda title, body: app.notify.push(title, body, icon="update"),
        background=getattr(wiring, "selfupdate_thread", None) or _thread,
        changed=lambda: wiring.main.post(show), **(getattr(wiring, "selfupdate_options", None) or {}))
    wiring.self_update = svc
    wiring.busy_checks.append(svc.busy_text)
    wiring.after_ready.append(lambda: wiring._bg(svc.cleanup_leftovers))

    sched = getattr(getattr(app, "shell", None), "sched", None)
    timer: dict = {"handle": None, "closed": False}

    def arm(seconds: float) -> None:
        if timer["closed"] or sched is None:
            return
        timer["handle"] = sched.later(seconds * 1000, tick)

    def run_check() -> None:
        svc.check()
        _badge(wiring)

    def tick() -> None:                         # Hauptschleife
        timer["handle"] = None
        if timer["closed"]:
            return
        if not wiring.config.get("update.check", True):
            arm(TICK_S)                         # abgeschaltet: nachsehen, ob es wieder an ist
            return
        if launching(app):
            arm(LAUNCH_WAIT_S)
            return
        wiring._bg(run_check)
        arm(TICK_S)

    def start() -> None:
        if paths.test_mode() and injected is None:
            return                              # Prüfmodus: kein Netz, keine Suche
        wiring._bg(_badge, wiring)              # was die letzte Suche ergab, gleich zeigen
        arm(FIRST_S)

    def close() -> None:
        timer["closed"] = True
        handle, timer["handle"] = timer["handle"], None
        if handle is not None and sched is not None:
            try:
                sched.cancel(handle)
            except Exception:
                _log.exception("Zeitgeber der Suche")

    wiring.after_ready.append(start)
    wiring.shutdown_hooks.append(close)
