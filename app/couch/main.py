"""Zusammenbau des Home-Prozesses (Bauplan 4.1): eine Gtk.Application, die Dienste einmal
erzeugen und über das App-Objekt verbinden. Rückgabewerte nach Bauplan 3.3:
0 = Sitzung beenden, 10 = Übergabe an Steam, alles andere zählt der Supervisor als Absturz.
"""
from __future__ import annotations

import faulthandler
import importlib
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

from . import APP_ID, PRODUCT, VERSION, i18n, intro, log, paths, system

_log = log.get("main")
_STARTED = time.monotonic()         # Import dieses Moduls = Beginn des Home-Prozesses (für die Startzeiten)

EXIT_SESSION_END = 0
EXIT_HANDOVER = 10
EXIT_CRASH = 1
EXIT_NO_DISPLAY = 3
EXIT_SIGNAL = 143

# So lange darf das Ende nach quit() dauern (Installer beenden bis 7 s, Sichern bis 3 s, GTK
# abbauen); danach endet der Prozess hart mit dem gewollten Code. Der Supervisor wartet auf ein
# Home mit <run>/exiting länger (COUCH_EXIT_WAIT), damit er den Code nicht verliert.
QUIT_DEADLINE_S = 15.0

# Variablen der Sitzung, die GTK/WebKit im Home nur stören (der Supervisor nimmt sie schon
# heraus; hier noch einmal, falls das Home direkt gestartet wird). Gestartete Apps bekommen
# die unveränderte Sitzungsumgebung aus paths.session_env().
_OWN_ENV_DROP = ("GTK_IM_MODULE", "QT_IM_MODULE", "XCURSOR_THEME", "XCURSOR_SCALE", "MANGOHUD",
                 "WAYLAND_DISPLAY")

# Bausteine anderer Teile (echte Bibliotheksquellen und Starter, Einstellungen, Archiv-Installer):
# Jedes hier genannte Modul darf setup(app) anbieten und darin Quellen und Starter anmelden
# (library.register_source, launch.register_launcher), RPC-Methoden eintragen (app.rpc.register)
# und app.settings / app.jobs setzen. Fehlt ein Modul, läuft das Home ohne es.
PLUGINS = (
    "couch.wiring",
)


def fake_services() -> bool:
    """Attrappen statt Gamescope/Fokus/Apps/Eingabe: nur im Prüfmodus."""
    return os.environ.get("COUCH_FAKE_SERVICES") == "1" and paths.test_mode()


class App:
    """Verbindet die Dienste. Andere Teile greifen nur über diese Attribute aufeinander zu."""

    def __init__(self, session_env: dict[str, str]):
        self.session_env = session_env
        self.exit_code = EXIT_CRASH          # bis zu einem gewollten Ende gilt alles als Absturz
        self.ready = False
        self.config: Any = None
        self.rpc: Any = None
        self.state: Any = None
        self.ui: Any = None
        self.gs: Any = None
        self.focus: Any = None
        self.procs: Any = None
        self.input: Any = None
        self.library: Any = None
        self.settings: Any = None
        self.jobs: Any = None
        self.shell: Any = None
        self.power: Any = None
        self.notify: Any = None
        self.control: Any = None
        self.wiring: Any = None             # setzt couch.wiring.setup()
        self.loopwatch: Any = None          # couch.loopwatch: fein im Prüfmodus, grob im Betrieb
        self.build_ms = 0.0                 # Dauer des Zusammenbaus (_build)
        self.times: dict[str, float] = {}   # ms seit Prozessbeginn: first_frame, ready
        self.writer: Any = None             # system.WriteBehind für settings.json und library.json
        self.gtk: Any = None
        self.background = system.background
        self._first_home = False
        self._beat_since_frame = False      # sys.beat der Home-Seite seit ihrem letzten ersten Bild
        self._input_ok = False
        self._quitting = False
        self._deadline: threading.Timer | None = None

    # ================================================================ Lauf
    def run(self) -> int:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gio, GLib, Gtk

        # NON_UNIQUE: Eine zweite Instanz (Prüf-Gamescope neben der echten Sitzung) soll sich
        # nicht über den Sitzungsbus an die erste hängen und sofort enden (08, Fallstrick 11).
        # Name gegenüber dem Tonsystem (WebKit reicht ihn an seine Tonströme weiter): Unter
        # „python3“ hatte PipeWire 0 % gespeichert, die Töne der Oberfläche blieben stumm (2026-10-18)
        GLib.set_prgname("couch")
        GLib.set_application_name(PRODUCT)          # so heißt der Tonstrom im Mixer (OWN_NAMES)
        self.gtk = Gtk.Application(application_id=APP_ID,          # E104: eine Konstante (couch.APP_ID)
                                   flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.gtk.connect("activate", self._on_activate)
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)
        self.gtk.run(None)
        self._stop_wiring()                     # auch nach einem Fehlstart (ohne quit())
        self._flush_writes()
        # Wartende Aufträge absagen; laufende (Daemon-Threads) halten das Ende nicht auf
        dropped = system.shutdown_pool()
        if dropped:
            _log.info("%d wartende Aufträge beim Ende abgesagt", dropped)
        if self._deadline is not None:
            self._deadline.cancel()
        return self.exit_code

    def _flush_writes(self) -> None:
        """Vor dem Ende: Was noch nicht auf der Platte ist (Einstellungen, Spielzeiten), schreiben."""
        if self.writer is not None and not self.writer.flush(3.0):
            _log.error("Einstellungen oder Bibliothek ließen sich vor dem Ende nicht mehr sichern")

    def _stop_wiring(self) -> None:
        stop = getattr(self.wiring, "stop", None)
        if stop:
            try:
                stop()                          # Hilfsprozesse beenden, wartende Threads freigeben
            except Exception:
                _log.exception("wiring.stop")

    def _on_signal(self, sig: int) -> bool:
        # Der Supervisor beendet ein hängendes Home so. Laufende Spiele bleiben unberührt;
        # er räumt sie selbst ab oder das nächste Home übernimmt sie (running.json).
        _log.warning("Signal %d: Home endet", sig)
        self.quit(EXIT_SIGNAL)
        return False

    def _on_activate(self, gtk_app: Any) -> None:
        if self.ui is not None:
            return
        try:
            self._build()
        except Exception as e:
            # Rückgabewert ungleich 0/10: Der Supervisor zählt das als Fehlstart
            _log.critical("Start fehlgeschlagen: %s", e, exc_info=True)
            self.exit_code = EXIT_CRASH
            self._quitting = True
            self.gtk.quit()

    # ================================================================ Zusammenbau
    def _build(self) -> None:
        from gi.repository import GLib

        from .config import Config
        from .launch import fixture as launch_fixture
        from .library import fixture as library_fixture
        from .library import index as library_index
        from .notify import Notifications
        from .notify import store_file as notify_store
        from .power import Power
        from .rpc import Rpc
        from .shell import Shell
        from .state import StateStore
        from .ui.host import UiHost
        from .ui.testctl import TestControl

        def on_main(fn: Any) -> None:
            # Antworten aus Arbeits-Threads mit dem Vorrang von Zeitgebern, nicht dem des Leerlaufs:
            # Am 2026-10-19 kamen Leerlauf-Rückrufe minutenlang nicht dran (kein Start ging mehr)
            GLib.idle_add(lambda: fn() and False, priority=GLib.PRIORITY_DEFAULT)

        def later(seconds: float, fn: Any) -> Any:
            """Zeitgeber in der Hauptschleife; liefert eine Funktion, die ihn abbricht."""
            box = {"id": 0}

            def fire() -> bool:
                box["id"] = 0
                fn()
                return False
            box["id"] = GLib.timeout_add(int(max(0.0, seconds) * 1000), fire)

            def cancel() -> None:
                if box["id"]:
                    GLib.source_remove(box["id"])
                    box["id"] = 0
            return cancel

        built = time.monotonic()
        # settings.json und library.json schreibt ein eigener Thread: kein fsync in der Hauptschleife
        self.writer = system.WriteBehind()
        self.config = Config(writer=self.writer)
        # E104: Bestand (ältere Installation) einmal auf die neuen Schlüssel heben, vor allem anderen, was
        # settings.json, library.json oder home-order.json schreiben könnte; wirft nie
        from . import portable
        portable.migrate(self.config, writer=self.writer)
        self.rpc = Rpc(defer=on_main, later=later)
        self._apply_language()                  # Englisch: Ausgabe der Brücke übersetzen (i18n, 2026-10-25)
        if paths.test_mode() and os.environ.get("COUCH_RPC_TIMEOUT"):
            try:                                # nur für Prüfläufe: kürzere Zeitgrenze der Antworten
                self.rpc.default_timeout = max(0.5, float(os.environ["COUCH_RPC_TIMEOUT"]))
            except ValueError:
                pass
        self.state = StateStore(self.rpc.emit)
        # Die Liste übersteht einen Neustart des Home (E99); geschrieben wird hinter der Schleife
        self.notify = Notifications(self.rpc, self.state, self.config, defer=on_main,
                                    store=notify_store(), writer=self.writer)

        if fake_services():
            from .shell import GLibScheduler
            from .ui import fakes
            _log.warning("COUCH_FAKE_SERVICES=1: Gamescope, Fokus, Apps und Eingabe sind Attrappen")
            self.gs, self.focus, self.procs, self.input = fakes.make(
                self.config, self.session_env, scheduler=GLibScheduler(), use_x=True)
        else:
            from .gs.atoms import connect
            from .gs.focus import FocusManager
            from .input.service import InputService
            from .proc.manager import AppManager
            self.gs = connect()
            self.focus = FocusManager(self.gs, self.config)
            self.procs = AppManager(self.gs, self.session_env)
            # Launcher-Spiele (E94): Fenster des Launchers bleiben beim Start verborgen
            self.focus.hide_windows = lambda app_id: getattr(
                getattr(self.procs.by_app_id(app_id), "spec", None), "hide_windows", ()) or ()
            self.input = InputService(self.config, self.gs)

        library_fixture.setup()
        launch_fixture.setup()
        self.library = library_index.LibraryIndex(on_main=on_main, writer=self.writer)

        self.shell = Shell(self)
        if self.focus is not None:
            self.focus.on_launcher = self.shell.launcher_progress
        self.notify.on_toast = self.shell.on_toast
        self.power = Power(self)

        self.ui = UiHost(self.gtk, self.rpc, root_size=self.gs.root_size)
        self.ui.on_realize = self.focus.tag_window
        self.ui.on_first_frame = self._on_first_frame
        self.ui.on_beat = self._on_beat
        self.ui.on_page_failed = self._on_page_failed
        self.ui.on_crash_loop = self._on_crash_loop
        self.ui.want_intro = lambda: intro.due(self.config)
        self.ui.lang = lambda: i18n.language(self.config)
        self.config.on_change("ui.language", lambda _k, _v: self._language_changed())

        self._register_sys()
        library_index.register_rpc(self.rpc, self.library, self.procs)
        self.shell.register_rpc(self.rpc)
        self.power.register_rpc(self.rpc)
        self.notify.register_rpc(self.rpc)
        self._load_plugins()
        self._require_wiring()

        self.ui.start()                         # Fenster entstehen, on_realize markiert sie
        self.focus.start()
        self.focus.apply_session_defaults()
        self.shell.start()
        started = getattr(self.wiring, "started", None)
        if started:
            try:
                started()                       # Bausteine, die sich hinter die Shell hängen
            except Exception:
                _log.exception("wiring.started")
        self.gs.watch(self._on_gs)
        # nur im Prüfmodus (bzw. mit COUCH_LOOP_WATCH=1): Aussetzer der Hauptschleife messen
        from . import loopwatch
        self.loopwatch = loopwatch.start() or loopwatch.start_quiet()
        self.control = TestControl(self)

        try:
            self.input.start()
            self._input_ok = True
        except Exception:
            # Ohne Eingabe ist das Home vom Sofa aus nicht bedienbar: kein Bereit-Signal,
            # der Supervisor fällt nach 30 s auf die nächste Stufe zurück.
            _log.critical("Eingabe startet nicht", exc_info=True)
        self.library.rescan()
        self._maybe_ready()
        # Der Zusammenbau läuft am Stück in der Hauptschleife (vor dem ersten Bild): im Blick behalten
        self.build_ms = round((time.monotonic() - built) * 1000.0, 1)
        _log.info("couch %s gestartet in %.0f ms (Prüfmodus: %s, Attrappen: %s)", VERSION,
                  self.build_ms, paths.test_mode(), fake_services())

    def _load_plugins(self) -> None:
        for name in PLUGINS:
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError as e:
                # Auch ein fehlendes Modul gehört ins Protokoll: Ohne Verdrahtung ist das Home leer
                _log.error("%s lässt sich nicht laden: %s", name, e)
                continue
            except Exception:
                _log.exception("%s lässt sich nicht laden", name)
                continue
            setup = getattr(module, "setup", None)
            if setup is None:
                continue
            try:
                setup(self)
                _log.info("Baustein %s eingehängt", name)
            except Exception:
                _log.exception("%s.setup", name)

    def _require_wiring(self) -> None:
        """Ohne Verdrahtung gibt es weder Bibliothek noch Einstellungen noch Steam-Kachel, nur
        ein leeres Home, das der Supervisor für gesund hielte. Dann lieber als Fehlstart enden:
        So greift previous bzw. Steam. Nur mit Attrappen-Diensten (Prüfseite) ist das gewollt."""
        if self.wiring is not None or fake_services():
            return
        raise RuntimeError("Die Verdrahtung der Bausteine (couch.wiring) fehlt oder ist gescheitert; "
                           "ohne sie wäre das Home leer")

    def _apply_language(self) -> None:
        self.rpc.out_filter = i18n.translate_tree if i18n.language(self.config) == "en" else None

    def _language_changed(self) -> None:
        """Systemsprache gewechselt: Kern sofort, Seiten neu laden (sie lesen ?lang= nur beim Laden)."""
        self._apply_language()
        for role in ("home", "overlay"):
            try:
                self.ui.reload(role)
            except Exception:
                _log.exception("Sprache: %s neu laden", role)

    def _register_sys(self) -> None:
        def sys_state(params: dict) -> dict:
            state = self.state.get()
            state["role"] = params.get("_role")
            return state

        def sys_log(params: dict) -> dict:
            level = getattr(logging, str(params.get("level", "info")).upper(), logging.INFO)
            log.get("web.%s" % params.get("_role")).log(level, "%s", params.get("text", ""))
            return {}

        self.rpc.register("sys.state", sys_state)
        self.rpc.register("sys.log", sys_log)

    # ================================================================ Signale an den Supervisor
    def _on_first_frame(self, role: str) -> None:
        self.shell.page_ready(role)             # neu geladene Seite: offene Ansicht noch einmal schicken
        if role == "home":
            self.times.setdefault("first_frame", round((time.monotonic() - _STARTED) * 1000.0, 1))
            first = not self._first_home
            self._first_home = True
            # Das erste Bild zählt als Herzschlag: beim Start, und nach einem Neuladen nur, wenn
            # die Seite davor wirklich geschlagen hat (eine einmal hängende Seite, S25). In einer
            # Absturzschleife (Bild, Absturz, Neuladen, Bild …) kommt kein sys.beat: Dann bleibt
            # die Datei alt und der Supervisor greift ein.
            if first or self._beat_since_frame:
                self._write_beat()
            self._beat_since_frame = False
            self._maybe_ready()

    def _on_crash_loop(self, role: str) -> None:
        """Der Web-Prozess einer Seite stirbt immer wieder, ohne dazwischen zu schlagen: Das ist
        ein schwarzes Bild ohne Ende. Als Absturz enden, damit der Supervisor zählt und auf
        previous bzw. Steam ausweicht."""
        _log.critical("%s: Web-Prozess in Absturzschleife, Home endet als Absturz", role)
        self.quit(EXIT_CRASH)

    def _on_page_failed(self, role: str, uri: str, text: str) -> None:
        """Die Home-Seite lässt sich nicht laden. Vor dem ersten Bild heißt das: Diese Fassung
        zeigt nie etwas. Sofort als Fehlstart enden, dann greift die nächste Stufe nach
        Sekunden statt nach dreimal 30 s schwarzem Bild."""
        if role == "home" and not self._first_home:
            _log.critical("Home-Seite nicht ladbar (%s): Start abgebrochen", text)
            self.quit(EXIT_CRASH)

    def _maybe_ready(self) -> None:
        """<run>/ready erst, wenn die Home-Seite ein Bild gezeigt hat UND die Eingabe läuft."""
        if self.ready or not (self._first_home and self._input_ok):
            return
        try:
            system.write_atomic(paths.ready_file(), "%d %.3f\n" % (os.getpid(), time.time()))
            self.ready = True
            self.times["ready"] = round((time.monotonic() - _STARTED) * 1000.0, 1)
            _log.info("bereit")
            _log.info("Start: erstes Bild nach %.0f ms, bereit nach %.0f ms (Zusammenbau %.0f ms)",
                      self.times.get("first_frame", 0.0), self.times["ready"], self.build_ms)
        except OSError:
            _log.critical("Bereit-Signal nicht schreibbar", exc_info=True)
            return
        ready = getattr(self.wiring, "ready", None)
        if ready:
            from gi.repository import GLib
            # erst nach diesem Durchlauf der Hauptschleife: Das Bereit-Signal wartet auf nichts.
            # Vorrang wie Zeitgeber (wie on_main): Leerlauf-Aufträge warteten im Betrieb bis zu
            # 212 s, und hieran hängen Ton, HDR, Netz und die Bluetooth-Probezeit (E99)
            GLib.idle_add(self._wiring_ready, ready, priority=GLib.PRIORITY_DEFAULT)

    @staticmethod
    def _wiring_ready(ready: Any) -> bool:
        try:
            ready()                             # Bausteine holen nach, was beim Start warten durfte
        except Exception:
            _log.exception("wiring.ready")
        return False

    def _on_beat(self, role: str) -> None:
        if role != "home":
            return
        self._beat_since_frame = True
        self._write_beat()

    def _write_beat(self) -> None:
        # Der Wert kommt aus der Seite (über die Brücke), nicht aus einem Python-Timer: Hängt
        # der Web-Prozess, bleibt die Datei alt und der Supervisor greift ein.
        if self._quitting:
            return
        try:
            system.write_atomic(paths.beat_file(), "%.3f\n" % time.time())
        except OSError:
            _log.error("Herzschlag nicht schreibbar", exc_info=True)

    def _on_gs(self, what: str) -> None:
        if what == "size":
            self.ui.resize(*self.gs.root_size())

    # ================================================================ Ende
    def quit(self, code: int = EXIT_SESSION_END) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.exit_code = int(code)
        # Zuerst die Marke für den Supervisor: Hängt das Ende danach (Installer, Sichern, GTK),
        # muss er hart beenden und weiß trotzdem, dass es eine Übergabe bzw. ein Abmelden war.
        # ready und beat bleiben liegen (start_home des Supervisors räumt sie weg): Eine
        # Übergabe kurz nach dem Bereit-Signal zählte sonst als Fehlstart.
        try:
            system.write_atomic(paths.exiting_file(), "%d %d %.3f\n" % (self.exit_code, os.getpid(), time.time()))
        except OSError:
            _log.error("Ende-Marke nicht schreibbar", exc_info=True)
        _log.info("Home endet mit %d", self.exit_code)
        self._arm_deadline(self.exit_code)
        stop = getattr(self.input, "stop", None)
        if stop:
            try:
                stop()
            except Exception:
                _log.exception("input.stop")
        if self.control:
            self.control.close()
        self._stop_wiring()
        self._flush_writes()
        self.gtk.quit()

    def _arm_deadline(self, code: int) -> None:
        """Daemon-Zeitgeber: Kommt das Ende nicht binnen QUIT_DEADLINE_S zustande, endet der
        Prozess hart mit dem gewollten Code (nie mit einem stummen Hänger)."""
        def hard() -> None:
            _log.error("Das Ende dauert länger als %.0f s: Prozess endet hart mit %d", QUIT_DEADLINE_S, code)
            hard_exit(code)
        self._deadline = threading.Timer(QUIT_DEADLINE_S, hard)
        self._deadline.daemon = True
        self._deadline.start()


def hard_exit(code: int) -> None:
    """Prozess sofort beenden (os._exit), vorher Protokoll und Ausgaben leeren."""
    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(int(code))


def finish(code: int) -> int:
    """Nach App.run: Hält noch ein Thread ohne Daemon-Flag das Ende auf (eine Bibliothek, die
    eigene Threads startet), endet der Prozess hart mit code statt zu warten. Sonst normal, damit
    WebKit beim Ende seine Sockets unter $XDG_RUNTIME_DIR/webkitgtk abräumt."""
    left = [t.name for t in threading.enumerate()
            if t is not threading.main_thread() and t.is_alive() and not t.daemon]
    if left:
        _log.warning("Threads ohne Daemon-Flag laufen noch (%s): Prozess endet hart mit %d",
                     ", ".join(left), code)
        hard_exit(code)
    return code


EXIT_BAD_TEST_SETUP = 2


def main(argv: list[str] | None = None) -> int:
    if paths.test_mode_incomplete():
        # noch bevor irgendein Ordner angelegt oder ein Protokoll geöffnet wird
        sys.stderr.write("couch: COUCH_TEST=1 ohne COUCH_TEST_ROOT. Der Prüfmodus braucht seinen "
                         "Ordner (sonst zeigen alle Orte auf die echten); nichts gestartet.\n")
        return EXIT_BAD_TEST_SETUP
    log.setup()
    # Ein Absturz in C (SIGSEGV in GLib) hinterließ bisher keine Zeile in home.log: Python-Stapel
    # aller Threads nach stderr (app/couch-home leitet es nach home.log um; log.py rotiert über
    # dieselbe Dateinummer, faulthandler schreibt also immer in die aktuelle Datei)
    try:
        faulthandler.enable(all_threads=True)
    except (RuntimeError, ValueError, OSError):
        _log.warning("faulthandler lässt sich nicht einschalten", exc_info=True)
    # Sitzungsumgebung festhalten, bevor die eigene angepasst wird
    session_env = paths.session_env()
    os.environ["GDK_BACKEND"] = "x11"           # unter Gamescope sind wir ein X11-Client
    for name in _OWN_ENV_DROP:
        os.environ.pop(name, None)
    for missing in paths.ensure_dirs():
        _log.error("Ordner lässt sich nicht anlegen: %s", missing)
    # E104: Was vor diesem Start schon da lag (Bestand), festhalten, bevor das Home etwas schreibt
    from . import portable
    portable.snapshot()
    for stale in (paths.ready_file(), paths.beat_file(), paths.exiting_file()):
        try:
            os.unlink(stale)                    # Reste eines früheren Laufs wären ein falsches Signal
        except OSError:
            pass
    _log.info("couch %s startet, pid %d, DISPLAY=%s", VERSION, os.getpid(), os.environ.get("DISPLAY"))
    # Noch vor dem Import von GTK (der verbindet sich schon mit $DISPLAY): Das Home läuft nur
    # in Gamescope und öffnet nie ein Fenster auf einem Desktop (SAFETY 13)
    from .ui import DisplayError, require_gamescope_display
    try:
        require_gamescope_display()
    except DisplayError as e:
        _log.critical("Start abgebrochen: %s", e)
        return EXIT_NO_DISPLAY
    return finish(App(session_env).run())


if __name__ == "__main__":
    sys.exit(main())
