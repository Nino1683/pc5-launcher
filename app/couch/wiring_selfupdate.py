"""Selbst-Update von GitHub (E116): Verdrahtung, eingehängt über wiring.PARTS (als letzter Teil).

  wiring.self_update      selfupdate.SelfUpdate; die Einstellungen holen ihn spät über deps.extra["late"]
                          (settings/system.py, Seite system.launcher unter Updates)
  Suche                   FIRST_S nach dem Bereit-Signal, dann stündlich nachsehen, ob 24 h um sind (nach der
                          Wanduhr, selfupdate.check): Der Zeitgeber läuft auf der monotonen Uhr und steht im
                          Ruhemodus. Nie, solange ein Spiel startet (shell.running_view: launching), und nur mit
                          update.check an. Im Prüfmodus ohne eingesteckte Netzfunktion (wiring.selfupdate_open)
                          gar nicht.
  Mitteilung              einmal je Fassung („PC5 Launcher X.Y.Z ist da“); gemerkt in <state>/selfupdate.json
  Punkt am Zahnrad        wiring_settings.push_update_badge zählt eine neue Fassung mit (gebündelt über
                          request_update_badge)
  busy_checks             solange geladen oder installiert wird, fragen Ausschalten und Neustart nach
  Laden und Installieren  im eigenen Thread (bis zu 15 Minuten): Der gemeinsame Pool bleibt frei
  Was ist neu (E133)      nach dem Bereit-Signal: erster Start einer höheren Fassung -> Mitteilung „PC5 Launcher X ist
                          installiert“ und sys.state.whatsnew {version, since} (das Home bietet den Changelog einmal
                          an); RPC update.notes {which?, since?} -> changelog.payload in der Sprache der Oberfläche,
                          update.seen {version} -> whatsnew gelöscht (docs/RPC.md)
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from . import i18n, log, paths, selfupdate

_log = log.get("wiring.selfupdate")

FIRST_S = 300.0          # erste Suche fünf Minuten nach dem Start: dann ist das Netz da und der Start vorbei
TICK_S = 3600.0          # danach stündlich nachsehen, ob ein Tag um ist
LAUNCH_WAIT_S = 600.0    # startet gerade ein Spiel: so viel später noch einmal
PAGES = ("system.launcher", "system.apps", "system.software", "updates")    # E133: die Übersicht „Updates“


def _thread(fn: Callable, *args: Any) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, name="couch-selfupdate", daemon=True)
    t.start()
    return t


def _badge(wiring: Any) -> None:
    """Punkt am Zahnrad neu (zählt Apps, Proton, Spiele und jetzt auch den PC5 Launcher). Aus jedem Thread; gerechnet
    wird gebündelt im Pool (wiring_settings.request_update_badge)."""
    try:
        from . import wiring_settings
        wiring_settings.request_update_badge(wiring)
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


class DailyTimer:
    """Zeitplan einer Suche im Netz, die höchstens einmal am Tag etwas tut (Selbst-Update E116, Wunschliste E122):
    start(first_s) nach dem Bereit-Signal, danach alle TICK_S nachsehen (ob 24 h um sind, entscheidet die Suche selbst
    nach der Wanduhr; der Zeitgeber läuft auf der monotonen Uhr der Shell und steht im Ruhemodus). Ist der Schalter
    key aus, wird nur weiter nachgesehen, ob er wieder an ist; startet gerade ein Spiel, LAUNCH_WAIT_S später.
    run läuft im Pool (wiring._bg). Ohne Shell (sched) nichts. Hauptschleife."""

    def __init__(self, wiring: Any, key: str, run: Callable[[], Any], name: str):
        self.wiring, self.key, self.run, self.name = wiring, key, run, name
        self.sched = getattr(getattr(wiring.app, "shell", None), "sched", None)
        self.handle: Any = None
        self.closed = False

    def arm(self, seconds: float) -> None:
        if self.closed or self.sched is None:
            return
        self.handle = self.sched.later(seconds * 1000, self.tick)

    start = arm

    def tick(self) -> None:
        self.handle = None
        if self.closed:
            return
        if not self.wiring.config.get(self.key, True):
            self.arm(TICK_S)                    # abgeschaltet: nachsehen, ob es wieder an ist
            return
        if launching(self.wiring.app):
            self.arm(LAUNCH_WAIT_S)
            return
        self.wiring._bg(self.run)
        self.arm(TICK_S)

    def close(self) -> None:
        self.closed = True
        handle, self.handle = self.handle, None
        if handle is not None and self.sched is not None:
            try:
                self.sched.cancel(handle)
            except Exception:
                _log.exception("Zeitgeber %s", self.name)


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

    def publish_whatsnew() -> None:
        try:
            value = svc.whats_new()
        except Exception:
            _log.exception("Was ist neu: Zustand")
            value = None
        state = getattr(app, "state", None)
        if state is not None:
            wiring.main.post(lambda: state.update(whatsnew=value))

    def after_start() -> None:                  # Arbeits-Thread; niemand liest sein Ergebnis
        try:
            svc.cleanup_leftovers()
        except Exception:                       # sonst fielen „erster Start“ und „Was ist neu“ still aus
            _log.exception("Selbst-Update: Reste abräumen")
        try:
            version = svc.first_start()
        except Exception:
            _log.exception("Was ist neu: erster Start")
            version = ""
        if version:
            # Nur in die Liste: Das Home fragt ohnehin mit „Später | Was ist neu“ (update-notes.js), sonst stand derselbe
            # Satz zweimal da
            app.notify.push("PC5 Launcher %s ist installiert" % version,
                            "Was neu ist, steht unter Einstellungen › Updates › Was ist neu.", icon="update", silent=True)
        publish_whatsnew()

    wiring.after_ready.append(lambda: wiring._bg(after_start))

    def notes(params: dict) -> dict:
        which = "new" if params.get("which") == "new" else "all"
        return svc.notes(lang=i18n.language(wiring.config), which=which, since=str(params.get("since") or ""))

    def seen(params: dict) -> dict:
        svc.seen_notes(str(params.get("version") or ""))
        publish_whatsnew()
        return {}

    rpc = getattr(app, "rpc", None)
    if rpc is not None:
        from .wiring import pooled
        rpc.register("update.notes", pooled(wiring, notes))
        rpc.register("update.seen", pooled(wiring, seen))

    def run_check() -> None:
        svc.check()
        _badge(wiring)

    timer = DailyTimer(wiring, "update.check", run_check, "der Suche")

    def start() -> None:
        if paths.test_mode() and injected is None:
            return                              # Prüfmodus: kein Netz, keine Suche
        _badge(wiring)                          # was die letzte Suche ergab, gleich zeigen
        timer.start(FIRST_S)

    wiring.after_ready.append(start)
    wiring.shutdown_hooks.append(timer.close)
