"""Verdrahtung der Bausteine (Bauplan 4.1): hängt Bibliotheksquellen, Bilder, Starter, Leistung,
Ton, Einstellungen und den Archiv-Installer an den Kern.

main.py lädt dieses Modul über PLUGINS und ruft setup(app) einmal, nach dem Zusammenbau der
Dienste und vor dem ersten Fenster. Jeder Baustein hängt für sich: Scheitert einer, läuft das
Home ohne ihn weiter (steht im Protokoll).

Größere Teile stehen in eigenen Modulen wiring_<teil>.py (PARTS). Jedes bietet extend(wiring)
und hängt sich am Ende von Wiring.run() ein, wieder jedes für sich. Für sie gibt es drei Listen:
  wiring.after_ready     Funktionen, die einmal nach dem Bereit-Signal laufen (alles Langsame
                         beim Start gehört dorthin und dort in einen Arbeits-Thread: wiring._bg)
  wiring.shutdown_hooks  laufen in wiring.stop(), jede nur kurz
  wiring.busy_checks     Funktionen ohne Parameter, die None liefern oder einen deutschen Satz,
                         warum jetzt nicht ausgeschaltet, abgemeldet oder an Steam übergeben
                         werden sollte; wiring.busy() sammelt die Sätze

Zwei Dinge gleicht dieses Modul aus:
- Die Bausteine rechnen mit (rc, stdout, stderr) und dürfen in Arbeits-Threads laufen. Die
  X-Verbindung (gs) und die Beobachter der Einstellungen gehören aber der Hauptschleife. Sie
  bekommen deshalb Stellvertreter, die solche Aufrufe dorthin tragen (_Main).
- Alles Schreibende geht durch system.run_write und wird im Prüfmodus nur protokolliert.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import log, paths, system

_log = log.get("wiring")

MAIN_TIMEOUT_S = 10.0
SLOW_STEP_MS = 50.0             # ab hier steht die Dauer eines Bausteins als Warnung im Protokoll

# Erweiterungen in eigenen Modulen (wiring_<teil>.py mit extend(wiring)), in dieser Reihenfolge
# E100: "itch" (itch.io) direkt nach "store", dessen Routen es erweitert
# E101/E102: "proton" nach "store" (Abbrechen unter Downloads über dessen Route store.cancel)
# E103 (Bauplan docs/research/e103/SPEC.md 4.5): die neuen Teile ans Ende, weil sie Attribute der
# bisherigen nutzen (store_routes, proton_jobs, idle, library): Einrichtung (E105), Sichern und
# Übernehmen (E106), Ruhemodus-Timer (E107), Rückblick (E108), Platz schaffen (E110)
# E116: "selfupdate" (Selbst-Update von GitHub) als letzter, er braucht nur Dienste des Kerns
PARTS = ("library", "art", "launch", "settings", "install", "files", "saves", "store", "itch", "launchers", "proton",
         "home", "setup", "backup", "sleeptimer", "stats", "cleanup", "selfupdate")

UNAVAILABLE = "Das ist gerade nicht verfügbar."
# RPC-Methoden je Baustein von Wiring.run(): Fällt er aus, antworten sie mit UNAVAILABLE
STEP_METHODS = {
    "Leistung": ("perf.get", "perf.set"),
    "Ton": ("audio.get", "audio.setVolume", "audio.setMuted", "audio.setSink"),
    "Einstellungen": ("settings.categories", "settings.page", "settings.set", "settings.action", "settings.reset"),
    "Archiv-Installer": ("install.browse", "install.probe", "install.start", "install.cancel", "install.jobs",
                         "install.setExe", "install.delete", "install.trashList", "install.restore",
                         "install.purge"),
    "Bibliothek": ("lib.setArt", "lib.resetArt", "lib.fetchArt", "lib.artCandidates", "lib.artPreview",
                   "lib.artBrowse"),
}


# ---------------------------------------------------------------- Befehle als Tupel
def read3(argv: Any, timeout: float = 10) -> tuple[int, str, str]:
    r = system.run_read([str(a) for a in argv], timeout=float(timeout))
    return r.returncode, r.stdout or "", r.stderr or ""


def write3(argv: Any, timeout: float = 30) -> tuple[int, str, str]:
    r = system.run_write([str(a) for a in argv], timeout=float(timeout), note="wiring")
    return r.returncode, r.stdout or "", r.stderr or ""


def _popen_bound(argv: list, **kwargs: Any) -> subprocess.Popen:
    """Hilfsprozess, der mit dem Home endet, auch wenn das Home hart beendet wird (sonst
    bliebe er bis zum Sitzungsende am Supervisor hängen)."""
    setpriv = shutil.which("setpriv")
    if setpriv:
        argv = [setpriv, "--pdeathsig", "TERM", *argv]
    return subprocess.Popen(argv, **kwargs)


# ---------------------------------------------------------------- in die Hauptschleife
class _Main:
    """Führt Aufrufe in der GLib-Hauptschleife aus und wartet auf das Ergebnis."""

    def __init__(self) -> None:
        self.ident = threading.get_ident()
        self.closed = threading.Event()     # das Home endet: niemand wartet mehr auf die Hauptschleife

    def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() == self.ident:
            return fn(*args, **kwargs)
        if self.closed.is_set():
            raise RuntimeError("Das Home endet gerade")
        from gi.repository import GLib
        done = threading.Event()
        box: dict = {}
        lock = threading.Lock()

        def run() -> bool:
            # Hat der Aufrufer schon aufgegeben (Fehler gemeldet), darf der Auftrag nicht doch
            # noch laufen: Die Einstellungen meldeten „hat nicht geklappt“, und kurz danach fuhr
            # der Rechner herunter bzw. startete das Spiel
            with lock:
                if box.get("abandoned"):
                    return False
                box["started"] = True
            try:
                box["value"] = fn(*args, **kwargs)
            except BaseException as e:      # geht an den wartenden Thread
                box["error"] = e
            done.set()
            return False
        GLib.idle_add(run, priority=GLib.PRIORITY_DEFAULT)  # Vorrang wie Zeitgeber: Leerlauf-Vorrang verhungerte am 2026-10-19 (loopwatch)
        waited = 0.0
        while not done.wait(0.1):
            waited += 0.1
            # Endet das Home, läuft die Hauptschleife nicht mehr: Arbeits-Threads dürfen das
            # Prozessende dann nicht aufhalten
            if self.closed.is_set() or waited >= MAIN_TIMEOUT_S:
                with lock:
                    started = box.get("started", False)
                    if not started:
                        box["abandoned"] = True
                if not started or self.closed.is_set():
                    raise RuntimeError("Die Hauptschleife antwortet nicht")
                # läuft schon in der Hauptschleife: das Ergebnis abwarten
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def post(self, fn: Callable, *args: Any) -> None:
        """Ohne zu warten (für Rückrufe aus Threads)."""
        if threading.get_ident() == self.ident:
            fn(*args)
            return
        from gi.repository import GLib
        GLib.idle_add(lambda: fn(*args) and False, priority=GLib.PRIORITY_DEFAULT)


class _MainProxy:
    """Stellvertreter: jede Methode des Ziels läuft in der Hauptschleife (X ist nicht für
    mehrere Threads gebaut, und die Rückrufe der Dienste erwarten die Hauptschleife)."""

    def __init__(self, target: Any, main: _Main):
        self._target = target
        self._main = main

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value):
            return value
        return lambda *args, **kwargs: self._main.call(value, *args, **kwargs)


class _ConfigProxy:
    """Einstellungen für Bausteine in Threads: Lesen direkt (Config sperrt selbst), Schreiben
    in der Hauptschleife, weil dort die Beobachter (Fokus, Eingabe) laufen müssen."""

    def __init__(self, config: Any, main: _Main):
        self._config = config
        self._main = main

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._main.call(self._config.set, key, value)

    def reset(self, key: str) -> None:
        self._main.call(self._config.reset, key)

    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        self._config.on_change(prefix, fn)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._config, name)


# ---------------------------------------------------------------- Zusammenbau
class Wiring:
    def __init__(self, app: Any):
        self.app = app
        self.main = _Main()
        self.config = _ConfigProxy(app.config, self.main)
        self.gs = _MainProxy(app.gs, self.main)
        self.sources: Any = None            # SourceContext
        self.art: Any = None                # ArtResolver
        self.perf: Any = None
        self.audio: Any = None
        self.tree: Any = None
        self.installer: Any = None
        self.launch: Any = None             # LaunchContext der Starter (wiring_launch.setup_launchers)
        self.after_ready: list[Callable[[], Any]] = []
        self.shutdown_hooks: list[Callable[[], Any]] = []
        self.busy_checks: list[Callable[[], str | None]] = []
        self.failed: list[str] = []         # Teile, die sich nicht einhängen ließen
        self.timings: dict[str, float] = {} # Dauer des Einhängens je Baustein in ms
        self._ready_done = False
        self._front: int | None = None
        self._snapshot = {"hdr": False, "size": (1920, 1080)}

    def run(self) -> None:
        for name, step in (("Einstellungen an die Seite", self._ui_settings),
                           ("Bibliothek", self._library),
                           ("Starter", self._launchers),
                           ("Leistung", self._perf),
                           ("Ton", self._audio),
                           ("Archiv-Installer", self._installer),
                           ("Einstellungen", self._settings),
                           ("Zustand", self._state)):
            began = time.monotonic()
            try:
                self._break(name)
                step()
            except Exception:
                _log.exception("Baustein „%s“ lässt sich nicht einhängen", name)
                self.failed.append(name)
            self._timed(name, began)
        self._extend()
        self._stubs()

    def _stubs(self) -> None:
        """Für ausgefallene Bausteine: Ihre Methoden antworten der Seite mit einem deutschen Satz
        statt mit „Unbekannte Methode“. Nur was nach dem Einhängen aller Teile wirklich fehlt
        (manche Teile holen ihr Anmelden in extend() nach)."""
        from .rpc import RpcError
        rpc = getattr(self.app, "rpc", None)
        if rpc is None or not self.failed:
            return
        try:
            known = set(rpc.names())
        except Exception:
            return

        def unavailable(params: dict) -> dict:
            raise RpcError(UNAVAILABLE, "Unavailable")
        for name in self.failed:
            for method in STEP_METHODS.get(name, ()):
                if method not in known:
                    rpc.register(method, unavailable)
                    _log.warning("%s: nicht verfügbar (Baustein „%s“ ist ausgefallen)", method, name)

    def _timed(self, name: str, began: float) -> None:
        """Das Einhängen läuft vor dem ersten Bild in der Hauptschleife: Was dort trödelt,
        verzögert den Start und gehört nach after_ready."""
        spent = (time.monotonic() - began) * 1000.0
        self.timings[name] = round(spent, 1)
        if spent >= SLOW_STEP_MS:
            # Ohne Bytecode (Prüfläufe mit PYTHONDONTWRITEBYTECODE) übersetzt Python hier jedes
            # Modul neu: Das allein kostet um 100 ms und ist kein Befund
            level = _log.info if sys.dont_write_bytecode else _log.warning
            level("Einhängen von „%s“ dauerte %.0f ms", name, spent)

    @staticmethod
    def _break(name: str) -> None:
        """Nur im Prüfmodus: COUCH_TEST_BREAK=<name>[,<name>…] lässt das Einhängen dieses
        Bausteins oder Teils scheitern. So lässt sich prüfen, dass das Home trotzdem startet
        und den Ausfall meldet."""
        if not paths.test_mode():
            return
        wanted = [n.strip() for n in os.environ.get("COUCH_TEST_BREAK", "").split(",") if n.strip()]
        if name in wanted:
            raise RuntimeError("COUCH_TEST_BREAK: Baustein „%s“ soll scheitern" % name)

    def _extend(self) -> None:
        """Hängt die Module wiring_<teil>.py ein. Fehlt eines, passiert nichts; wirft eines,
        läuft der Rest weiter (ein Home mit weniger Funktionen ist besser als keines)."""
        for part in PARTS:
            name = "%s.wiring_%s" % (__package__, part)
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as e:
                if e.name == name:
                    continue                # diesen Teil gibt es (noch) nicht
                _log.exception("Teil %s lässt sich nicht laden", part)
                self.failed.append(part)
                continue
            except Exception:
                _log.exception("Teil %s lässt sich nicht laden", part)
                self.failed.append(part)
                continue
            extend = getattr(module, "extend", None)
            if extend is None:
                continue
            began = time.monotonic()
            try:
                self._break(part)
                extend(self)
                _log.info("Teil %s eingehängt", part)
            except Exception:
                _log.exception("Teil %s: extend gescheitert", part)
                self.failed.append(part)
            self._timed("Teil " + part, began)

    def ready(self) -> None:
        """Von main gerufen, einmal nach dem Bereit-Signal (Hauptschleife)."""
        if self._ready_done:
            return
        self._ready_done = True
        for fn in list(self.after_ready):
            try:
                fn()
            except Exception:
                _log.exception("Nach dem Start: %r", fn)
        if self.failed:
            # sichtbar machen statt verschweigen
            try:
                self.app.notify.push("Ein Teil der Konsole ist nicht geladen",
                                     "Betroffen: %s. Einzelheiten stehen im Protokoll."
                                     % ", ".join(self.failed), icon="warn")
            except Exception:
                _log.exception("Mitteilung über ausgefallene Teile")

    def busy(self) -> list[str]:
        """Sätze aller busy_checks, die gerade etwas zu melden haben."""
        out: list[str] = []
        for fn in list(self.busy_checks):
            try:
                text = fn()
            except Exception:
                _log.exception("busy_check %r", fn)
                continue
            if text:
                out.append(str(text))
        return out

    def _bg(self, fn: Callable, *args: Any) -> Any:
        return self.app.background(fn, *args)

    # ============================================================ Einstellungen für die Seite
    # Der Teil „settings“ (Einstellungen, Ton, Leistung, sys.state) steht in wiring_settings.py;
    # die Methoden hier reichen nur dorthin (spät importiert, damit ein Fehler dort nur diesen
    # Baustein trifft).
    def _ui_settings(self) -> None:
        """Was die Seite selbst braucht (Töne, Bewegung, Textgröße, Uhr, Hintergrund), geht bei
        jeder Änderung als sys.state.ui hinaus. Jede Einstellung hat genau einen Schlüssel (E22)."""
        from . import wiring_settings
        wiring_settings.ui_settings(self)

    # ============================================================ Bibliothek und Bilder
    def _library(self) -> None:
        """Quellen und Bilder (wiring_library.py): legt self.sources an (im Prüfmodus zeigt der
        Kontext nur in den Attrappen-Baum, nie auf die echte Bibliothek), holt über _make_art den
        Auflöser nach self.art und meldet die Quellen samt Verknüpfungen (E25) am Index an."""
        from . import wiring_library
        wiring_library.setup(self)
        self._art_rpc()

    def _art_rpc(self) -> None:
        """lib.setArt {id, kind, path} und lib.resetArt {id} (E27): eigene Bildwahl je Eintrag.
        Steht in wiring_library.py (dort auch lib.fetchArt, lib.artCandidates, lib.artPreview,
        lib.artBrowse)."""
        from . import wiring_library
        wiring_library.register_art_rpc(self)

    def _make_art(self) -> None:
        """Der Auflöser für Bilder (lokale Quellen, Abruf aus dem Netz, Vorschaubilder).
        Steht in wiring_art.py."""
        from . import wiring_art
        self.art = wiring_art.make_art(self)

    # ============================================================ Starter
    def _launchers(self) -> None:
        """Starter anmelden (wiring_launch.py); der LaunchContext liegt danach in self.launch."""
        from . import wiring_launch
        wiring_launch.setup_launchers(self)

    def _refresh_snapshot(self) -> None:
        from . import wiring_launch
        wiring_launch.refresh_snapshot(self)

    # ============================================================ Leistung
    def _perf(self) -> None:
        """PerfService, RPC perf.get / perf.set, Bildraten-Limit je Spiel nur mit Teilern (E23)."""
        from . import wiring_settings
        wiring_settings.setup_perf(self)

    def _front_changed(self) -> None:
        """Bildraten-Limit und Leistungsanzeige je Spiel gelten nur, solange es vorn ist."""
        from . import wiring_settings
        wiring_settings.front_changed(self)

    def _proc_changed(self, running: Any, what: str) -> None:
        """Spielauflösung je Spiel beim Start anmelden, beim Ende zurückstellen."""
        from . import wiring_settings
        wiring_settings.proc_changed(self, running, what)

    # ============================================================ Ton
    def _audio(self) -> None:
        """Der eine AudioService, RPC audio.get / setVolume / setMuted / setSink (eigener
        Arbeits-Thread). Beobachter und Fernseher-Wahl starten nach dem Bereit-Signal."""
        from . import wiring_settings
        wiring_settings.setup_audio(self)

    def _audio_changed(self) -> None:
        from . import wiring_settings
        wiring_settings.audio_changed(self)

    # ============================================================ Archiv-Installer
    def _installer(self) -> None:
        # Aufbau, RPC install.* und der Weg der Ereignisse stehen in wiring_install.py. Dessen
        # extend() trägt danach die Haken ein (Aufräumen nach dem Bereit-Signal, Ende, busy).
        from . import wiring_install
        wiring_install.setup(self)              # setzt self.installer und app.jobs

    def _push_jobs(self) -> None:
        from . import wiring_install
        wiring_install.push_jobs(self)

    # ============================================================ Einstellungen
    def _settings(self) -> None:
        """SettingsTree mit deps und deps.extra, RPC settings.categories / page / set / action.
        Ergebnisse der Bereiche gehen unverändert an die Seite (auch error und trial)."""
        from . import wiring_settings
        wiring_settings.setup_settings(self)

    def started(self) -> None:
        """Von main gerufen, nachdem die Shell ihre Rückrufe eingehängt hat."""
        from . import wiring_settings
        wiring_settings.started(self)

    # ============================================================ sys.state
    def _state(self) -> None:
        """sys.state: ui, Ton, Netz, HDR (und die Aufträge des Installers)."""
        from . import wiring_settings
        wiring_settings.setup_state(self)

    # ============================================================ Ende
    def stop(self) -> None:
        """Am Ende des Home (auch nach einem Fehlstart). Darf mehrmals gerufen werden."""
        if self.main.closed.is_set():
            return
        self.main.closed.set()
        for fn in list(self.shutdown_hooks):
            try:
                fn()
            except Exception:
                _log.exception("Beim Beenden: %r", fn)
        if self.audio is not None:
            try:
                self.audio.stop_watch()
            except Exception:
                _log.exception("Ton-Beobachter beenden")
        if self.installer is not None:
            try:
                if self.installer.busy():
                    # kurz: Der Supervisor wartet nicht lange auf ein Home, das enden soll.
                    # Was liegen bleibt, räumt der Installer beim nächsten Start ab.
                    self.installer.shutdown(5.0)
            except Exception:
                _log.exception("Archiv-Installer beenden")


def setup(app: Any) -> Wiring:
    Wiring._break("wiring")         # nur im Prüfmodus: die ganze Verdrahtung scheitern lassen
    wiring = Wiring(app)
    wiring.run()
    app.wiring = wiring
    return wiring
