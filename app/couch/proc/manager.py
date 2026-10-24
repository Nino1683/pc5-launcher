"""AppManager: gestartete Apps als Kinder eines reaper (Bauplan 4.5, E3 bis E5).

Jede App läuft als `reaper SteamLaunch AppId=<id> -- <befehl>` auf Xwayland 1. Alles, was
zu ihr gehört, ist Nachkomme ihres reaper; nur darüber wird angehalten und beendet
(proc/tree.py), nie über Namen, cgroups oder fremde Prozessgruppen.

Nichts hier blockiert die Hauptschleife: Das Ende einer App meldet ein pidfd, die Stufen
beim Beenden laufen über Zeitgeber. Einzige Ausnahme ist shutdown(), das höchstens
COUCH_KILL_GRACE + 2 Sekunden wartet; es ist nur für ein hartes Prozessende ohne laufende
Hauptschleife gedacht. Die Shell schließt vor Ausschalten und Übergabe über close().

Ereignisse an on_change(fn(app, ereignis)): "started" (nach der Rückkehr von launch, nie
aus launch heraus), "visible", "frozen", "thawed", "exited" (app.exit_code, app.runtime;
runtime zählt die Zeit nicht mit, in der die App angehalten war).
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .. import APP_ID_BASE, jsontext, log, paths, system  # noqa: E402
from ..launch.spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec, strip_env  # noqa: E402
from . import tree  # noqa: E402

try:                                    # GLib.unix_fd_add_full ist in neuem PyGObject abgekündigt
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    _fd_add = GLibUnix.fd_add_full
except (ValueError, ImportError, AttributeError):
    _fd_add = GLib.unix_fd_add_full

_log = log.get("proc")

APP_ID_MAX = 0x7FFFFFFF
KILL_GRACE = 8.0            # Sekunden zwischen höflicher Bitte und SIGKILL (E5)
POLITE_FOLLOWUP = 2.0       # nach einem polite_stop-Befehl: so lange bis zum SIGTERM hinterher
POLITE_POLL = 0.05          # Takt, in dem auf das Ende des polite_stop-Befehls gesehen wird
POLITE_PATIENCE = 0.75      # Anteil der Schonfrist, den ein noch laufender polite_stop-Befehl bekommt
POLITE_NOTHING = 3          # Rückgabewert "nichts zu beenden gefunden" (couch-flatpak-stop, couch-desktop --logout)
LAST_RESORT = 1.7           # nach SIGKILL an die Nachkommen: dann trifft es auch den reaper
FREEZE_RECHECKS = (0.25, 1.0)

# Aus der Sitzungsumgebung entfernen, bevor eine App startet (03 Abschnitt 5.3, 01 8.2 d):
# Reste eines Steam- oder Lutris-Elternprozesses (dieselbe Liste wie in den Startern, spec.py)
# kippen Proton in den Steam-Modus, die IM-/Zeiger-Variablen der Sitzung gelten nur für Steam,
# MANGOHUD regelt die Sitzung selbst. "*" = Muster (spec.strip_env).
# ENABLE_GAMESCOPE_WSI, GAMESCOPE_WAYLAND_DISPLAY, QT_QPA_PLATFORM, DXVK_HDR bleiben;
# DISABLE_GAMESCOPE_WSI des Home nicht (die Desktop-App setzt es ausdrücklich über spec.env).
SCRUB = STEAM_LEFTOVERS + (
    "LUTRIS_*",
    "GTK_IM_MODULE", "QT_IM_MODULE", "XCURSOR_THEME", "XCURSOR_SCALE",
    "MANGOHUD", "SRT_URLOPEN_PREFER_STEAM",
    "DISABLE_GAMESCOPE_WSI",            # setzt couch-home nur für sich; Spiele brauchen die Schicht (HDR)
    "GSK_RENDERER", "WEBKIT_FORCE_VBLANK_TIMER",   # ebenso (Versuche bzw. Takt der Oberfläche)
    "COUCH_SESSION_ENV", "COUCH_LOG_FILE",
)
# Setzt nur der AppManager selbst; auch spec.env kommt daran nicht vorbei. WAYLAND_DISPLAY:
# Apps sprechen X11 auf Xwayland 1, und in Tests zeigt die Variable auf den Desktop des Nutzers.
RESERVED = ("WAYLAND_DISPLAY", "DISPLAY", "COUCH_APP_ID", "COUCH_ENTRY_ID",
            "COUCH_PRELAUNCH", "COUCH_PRELAUNCH_TIMEOUT")

# Im Prüfmodus starten nur Attrappen (SAFETY 12). Diese Namen sind harmlose Bausteine dafür.
_TEST_HARMLESS = frozenset({"sleep", "true", "false", "sh", "bash", "env", "python3",
                            "cat", "echo", "touch", "printf",
                            "couch-steam-game"})   # startet im Prüfmodus nur eine Steam-Attrappe, sonst nichts


def kill_grace() -> float:
    try:
        return max(0.0, float(os.environ.get("COUCH_KILL_GRACE", "") or KILL_GRACE))
    except ValueError:
        return KILL_GRACE


def build_env(session_env: dict, spec: LaunchSpec, display: str, app_id: int) -> dict[str, str]:
    """Umgebung einer App: Sitzungsumgebung bereinigt, ohne spec.unset, plus spec.env.

    spec.unset wirkt auf die geerbte Umgebung, Namen mit "*" als Muster (wie SCRUB; vorher hier
    nur wörtlich, E99); was spec.env ausdrücklich setzt, gewinnt.
    """
    kept = strip_env(session_env, SCRUB + tuple(spec.unset or ()))
    env: dict[str, str] = {str(key): str(value) for key, value in kept.items()}
    for key, value in (spec.env or {}).items():
        if value is None:
            env.pop(str(key), None)
        else:
            env[str(key)] = str(value)
    for key in RESERVED:
        env.pop(key, None)
    if "XDG_SESSION_TYPE" not in (spec.env or {}):
        # Die echte Sitzung exportiert x11 (gamescope-session-plus). Steht hier "wayland" (Shell
        # eines Desktops, Prüflauf), wählen Electron-Programme Wayland und enden sofort.
        env["XDG_SESSION_TYPE"] = "x11"
    env["DISPLAY"] = display
    env["COUCH_APP_ID"] = str(app_id)
    env["COUCH_ENTRY_ID"] = spec.entry_id
    return env


def find_executable(name: str, env: dict, cwd: str | None) -> str | None:
    if os.sep in name:
        path = name if os.path.isabs(name) else os.path.join(cwd or os.getcwd(), name)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which(name, path=env.get("PATH") or os.defpath)


def test_mode_allows(exe: str) -> bool:
    """Darf dieses Programm im Prüfmodus laufen? Nur Attrappen: Name beginnt mit "fake-",
    liegt (nach Auflösen aller Verweise) unter COUCH_TEST_ROOT oder in der Prüf-Welt
    COUCH_FIXTURES, ist ein harmloser Baustein oder steht in COUCH_TEST_ALLOW_EXEC (mit ":"
    getrennte Namen oder Pfade). Ein Verweis aus der Prüf-Welt auf ein echtes Programm zählt
    nicht: Verglichen wird der aufgelöste Pfad."""
    base = os.path.basename(exe)
    if base.startswith("fake-") or base in _TEST_HARMLESS:
        return True
    real = os.path.realpath(exe)
    roots = [r for r in (paths.test_root(), paths.fixtures_dir()) if r]
    if roots and paths.is_under(real, roots):
        return True
    for item in os.environ.get("COUCH_TEST_ALLOW_EXEC", "").split(":"):
        if item and (item == base or os.path.realpath(item) == real):
            return True
    return False


def _spec_from_dict(raw: Any, entry_id: str) -> LaunchSpec:
    """LaunchSpec aus running.json; unbekannte Felder (andere Fassung) fallen weg."""
    known = {f.name for f in fields(LaunchSpec)}
    data = {k: v for k, v in raw.items() if k in known} if isinstance(raw, dict) else {}
    data["entry_id"] = entry_id
    data.setdefault("argv", [])
    try:
        return LaunchSpec(**data)
    except TypeError:
        return LaunchSpec(entry_id=entry_id, argv=[])


@dataclass(eq=False)
class RunningApp:
    """Eine laufende App (interfaces.RunningApp). Nach "exited" nur noch zum Nachlesen."""
    app_id: int
    entry_id: str
    reaper_pid: int
    started: float                      # Uhrzeit des Starts (time.time)
    spec: LaunchSpec
    frozen: bool = False
    visible: bool = False               # hatte schon ein Fenster in GAMESCOPE_FOCUSABLE_APPS
    reaper_starttime: int = 0           # mit der PID zusammen eindeutig (Wiederverwendung)
    closing: bool = False               # close() oder kill() läuft
    exited: bool = False
    exit_code: int | None = None        # None bei übernommenen Apps (nicht unser Kind)
    runtime: float = 0.0                # Sekunden, gesetzt bei "exited"; ohne die angehaltene Zeit
    frozen_total: float = 0.0           # Sekunden, die die App bisher angehalten war
    frozen_since: float = 0.0           # Uhrzeit des laufenden Anhaltens, 0 = läuft
    adopted: bool = False               # aus running.json übernommen
    log_path: str = ""
    _popen: Any = field(default=None, repr=False)
    _pidfd: int = field(default=-1, repr=False)
    _watch: int = field(default=0, repr=False)
    _timers: set = field(default_factory=set, repr=False)
    _polite: Any = field(default=None, repr=False)
    _announced: bool = field(default=False, repr=False)


class AppManager:
    def __init__(self, gs, session_env: dict, *, reaper: str | None = None, adopt: bool = True):
        self._gs = gs
        self._session_env = dict(session_env)
        self._reaper = reaper or os.path.join(os.path.dirname(os.path.realpath(__file__)), "reaper")
        self._file = paths.running_file()
        self._apps: dict[int, RunningApp] = {}
        self._listeners: list[Callable[[RunningApp, str], None]] = []
        self._next_n = 1
        gs.watch(self._on_gs)
        if adopt:
            self.adopt()

    # ------------------------------------------------------------ Abfragen
    def running(self) -> list[RunningApp]:
        return sorted(self._apps.values(), key=lambda a: (a.started, a.app_id))

    def by_entry(self, entry_id: str) -> RunningApp | None:
        for app in self._apps.values():
            if app.entry_id == entry_id:
                return app
        return None

    def by_app_id(self, app_id: int) -> RunningApp | None:
        return self._apps.get(app_id)

    def on_change(self, fn: Callable[[RunningApp, str], None]) -> None:
        self._listeners.append(fn)

    # ------------------------------------------------------------ starten
    def launch(self, spec: LaunchSpec) -> RunningApp:
        if spec.handover is not None:
            raise LaunchError("Dieser Eintrag wird an Steam übergeben und nicht als App gestartet.",
                              "LaunchSpec.handover ist gesetzt; die Übergabe macht die Shell.")
        current = self.by_entry(spec.entry_id)
        if current is not None:
            return current                      # höchstens eine laufende App je Eintrag
        title = spec.title or spec.entry_id
        argv = [str(a) for a in (spec.argv or [])]
        if not argv:
            raise LaunchError("Für „%s“ ist kein Startbefehl hinterlegt." % title)
        if getattr(self._gs, "closed", False):
            raise LaunchError("Gamescope ist nicht erreichbar.", "X-Verbindung verloren")
        try:
            display = self._gs.game_display()
        except Exception as e:
            raise LaunchError("Gamescope ist nicht erreichbar.", str(e)) from e
        n = self._free_n()
        app_id = APP_ID_BASE + n
        if app_id in self._apps:
            raise LaunchError("„%s“ läuft schon." % title, "App-ID %d ist belegt" % app_id)
        env = build_env(self._session_env, spec, display, app_id)
        if spec.cwd and not os.path.isdir(spec.cwd):
            raise LaunchError("Der Ordner von „%s“ fehlt." % title, spec.cwd)
        exe = find_executable(argv[0], env, spec.cwd)
        if exe is None:
            raise LaunchError("Das Programm für „%s“ wurde nicht gefunden." % title, argv[0])
        if paths.test_mode() and not test_mode_allows(exe):
            raise LaunchError("Im Prüfmodus werden nur Attrappen gestartet.", exe)
        if not (os.path.isfile(self._reaper) and os.access(self._reaper, os.X_OK)):
            raise LaunchError("Der Starthelfer „reaper“ fehlt oder ist nicht ausführbar.", self._reaper)
        prelaunch = self._prelaunch(spec, env)
        if prelaunch:
            # Läuft im reaper vor dem eigentlichen Befehl: so wartet die Hauptschleife nicht,
            # und die Vorab-Befehle gehören schon zum Baum der App (beenden, Protokoll).
            env["COUCH_PRELAUNCH"] = json.dumps(prelaunch)
            limit = os.environ.get("COUCH_PRELAUNCH_TIMEOUT")
            if limit:
                env["COUCH_PRELAUNCH_TIMEOUT"] = limit
        try:
            log_path, logf = self._open_log(spec.entry_id, app_id, argv)
        except OSError as e:
            # Das Protokoll ist Beiwerk (Platte voll, Ordner gesperrt): ohne es starten
            _log.warning("Protokoll für %s lässt sich nicht anlegen (%s): Ausgabe wird verworfen",
                         spec.entry_id, e)
            log_path, logf = os.devnull, open(os.devnull, "wb")
        started = time.time()
        try:
            popen = subprocess.Popen(
                [self._reaper, "SteamLaunch", "AppId=%d" % app_id, "--", *argv],
                env=env, cwd=spec.cwd or None, stdin=subprocess.DEVNULL, stdout=logf,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            raise LaunchError("„%s“ ließ sich nicht starten." % title, str(e)) from e
        finally:
            logf.close()
        app = RunningApp(app_id=app_id, entry_id=spec.entry_id, reaper_pid=popen.pid,
                         started=started, spec=spec,
                         reaper_starttime=tree.starttime(popen.pid) or 0, log_path=log_path)
        app._popen = popen
        self._attach(app)
        self._apps[app_id] = app
        self._next_n = n + 1
        self._save()
        _log.info("gestartet: %s AppId=%d reaper=%d", spec.entry_id, app_id, popen.pid)
        GLib.idle_add(self._announce, app, priority=GLib.PRIORITY_HIGH)
        return app

    def _free_n(self) -> int:
        n = self._next_n
        for _ in range(len(self._apps) + 2):
            if n < 1 or APP_ID_BASE + n > APP_ID_MAX:
                n = 1                           # Bereich aufgebraucht: vorn wieder anfangen
            if APP_ID_BASE + n not in self._apps:
                break
            n += 1
        return n

    def _prelaunch(self, spec: LaunchSpec, env: dict) -> list[list[str]]:
        out = []
        for cmd in spec.prelaunch or []:
            cmd = [str(a) for a in cmd]
            if not cmd:
                continue
            if paths.test_mode():
                exe = find_executable(cmd[0], env, spec.cwd)
                if exe is None or not test_mode_allows(exe):
                    system.record_write(cmd, "proc: Vorab-Befehl im Prüfmodus nicht ausgeführt")
                    continue
            out.append(cmd)
        return out

    def _open_log(self, entry_id: str, app_id: int, argv: list[str]):
        path = paths.app_log(entry_id)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            os.replace(path, path + ".1")       # genau ein voriger Lauf bleibt liegen
        except FileNotFoundError:
            pass
        f = open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        # Argumente stehen nicht im Protokoll: Die Oberfläche zeigt dessen Ende bei Fehlern
        # an, und manche Starter tragen Zugangsdaten in der Kommandozeile.
        head = "=== %s  %s  AppId=%d  %s (+%d Argumente)\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), entry_id, app_id, argv[0], len(argv) - 1)
        f.write(head.encode("utf-8", "replace"))
        f.flush()
        return path, f

    # ------------------------------------------------------------ überwachen
    def _attach(self, app: RunningApp) -> None:
        if app._pidfd < 0:
            try:
                app._pidfd = os.pidfd_open(app.reaper_pid)
            except OSError as e:
                _log.warning("kein pidfd für reaper %d (%s), frage stattdessen nach", app.reaper_pid, e)
                self._poll_exit(app)
                return
        app._watch = _fd_add(GLib.PRIORITY_DEFAULT, app._pidfd,
                             GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                             self._on_pidfd, app)

    def _poll_exit(self, app: RunningApp) -> None:
        def check(a: RunningApp) -> None:
            gone = a._popen.poll() is not None if a._popen is not None \
                else not tree.alive(a.reaper_pid, a.reaper_starttime)
            if gone:
                self._finish(a)
            else:
                self._later(a, 0.5, check)
        self._later(app, 0.5, check)

    def _on_pidfd(self, _fd, _condition, app: RunningApp) -> bool:
        app._watch = 0                          # die Quelle endet mit diesem Rückgabewert
        self._finish(app)
        return False

    def _finish(self, app: RunningApp) -> None:
        if app.exited:
            return
        code = None
        if app._popen is not None:
            code = app._popen.poll()
            if code is None:
                try:
                    code = app._popen.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    code = None
            if code is not None and code < 0:
                code = 128 - code               # der reaper selbst wurde durch ein Signal beendet
        app.exited = True
        app.exit_code = code
        self._count_frozen(app)
        app.runtime = max(0.0, time.time() - app.started - app.frozen_total)
        app.frozen = False
        self._detach(app)
        self._apps.pop(app.app_id, None)
        self._save()
        _log.info("beendet: %s AppId=%d Code=%s nach %.0f s", app.entry_id, app.app_id, code, app.runtime)
        self._emit(app, "exited")

    def _detach(self, app: RunningApp) -> None:
        for sid in list(app._timers):
            GLib.source_remove(sid)
        app._timers.clear()
        if app._watch:
            GLib.source_remove(app._watch)
            app._watch = 0
        if app._pidfd >= 0:
            try:
                os.close(app._pidfd)
            except OSError:
                pass
            app._pidfd = -1
        polite, app._polite = app._polite, None
        if polite is not None and polite.poll() is None:
            polite.kill()                       # eigener Hilfsbefehl, der die App überlebt hat
            try:
                polite.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass

    def _later(self, app: RunningApp, seconds: float, fn: Callable[[RunningApp], None]) -> None:
        """Zeitgeber, der mit der App stirbt (statt sleep: die Hauptschleife läuft weiter)."""
        holder: list[int] = []

        def fire() -> bool:
            app._timers.discard(holder[0])
            if not app.exited:
                fn(app)
            return False

        holder.append(GLib.timeout_add(max(1, int(seconds * 1000)), fire))
        app._timers.add(holder[0])

    def _on_gs(self, kind: str) -> None:
        if kind != "apps" or not self._apps:
            return
        try:
            ids = set(self._gs.focusable_apps())
        except Exception:
            return
        for app in list(self._apps.values()):
            if not app.visible and app.app_id in ids:
                app.visible = True
                _log.info("sichtbar: %s nach %.1f s", app.entry_id, max(0.0, time.time() - app.started))
                self._emit(app, "visible")

    def _announce(self, app: RunningApp) -> bool:
        if not app._announced:
            app._announced = True
            self._call(app, "started")
        return False

    def _emit(self, app: RunningApp, event: str) -> None:
        self._announce(app)                     # "started" kommt immer zuerst
        self._call(app, event)

    def _call(self, app: RunningApp, event: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(app, event)
            except Exception:
                _log.exception("Beobachter für %s (%s)", app.entry_id, event)

    # ------------------------------------------------------------ anhalten
    @staticmethod
    def _count_frozen(app: RunningApp) -> None:
        """Ein laufendes Anhalten zur angehaltenen Zeit schlagen (Spielzeit zählt sie nicht mit)."""
        if app.frozen_since:
            app.frozen_total += max(0.0, time.time() - app.frozen_since)
            app.frozen_since = 0.0

    def _tree(self, app: RunningApp, sig: int, include_root: bool = False) -> set[int]:
        return tree.signal_tree(app.reaper_pid, sig, include_root=include_root,
                                root_starttime=app.reaper_starttime or None)

    def freeze(self, app: RunningApp) -> None:
        """SIGSTOP an alle Nachkommen, nicht an den reaper (er muss ansprechbar bleiben)."""
        if app.exited or app.closing:
            return
        if not app.spec.freeze:
            _log.info("%s darf nicht angehalten werden (freeze=False)", app.entry_id)
            return
        self._tree(app, signal.SIGSTOP)
        if not app.frozen:
            _log.info("angehalten: %s", app.entry_id)
            app.frozen = True
            app.frozen_since = time.time()
            self._save()
            # Nachzügler: ein Kind, das im Moment des Anhaltens gerade entstand
            for delay in FREEZE_RECHECKS:
                self._later(app, delay, self._refreeze)
            self._emit(app, "frozen")

    def _refreeze(self, app: RunningApp) -> None:
        if app.frozen and not app.closing:
            self._tree(app, signal.SIGSTOP)

    def thaw(self, app: RunningApp) -> None:
        if app.exited or not app.frozen:
            return
        app.frozen = False                      # zuerst: ein wartender Nachzügler-Lauf hält nicht mehr an
        _log.info("fortgesetzt: %s", app.entry_id)
        self._count_frozen(app)
        self._tree(app, signal.SIGCONT)
        self._save()
        self._emit(app, "thawed")

    # ------------------------------------------------------------ beenden
    def close(self, app: RunningApp) -> None:
        """Stufen nach E5: auftauen, höflich bitten, COUCH_KILL_GRACE Sekunden später SIGKILL
        an alle Nachkommen. Kehrt sofort zurück; das Ende meldet "exited"."""
        if app.exited or app.closing:
            return
        app.closing = True
        self._wake(app)
        self._polite_stop(app)
        self._later(app, self._grace(app), self._on_grace)

    @staticmethod
    def _grace(app: RunningApp) -> float:
        try:
            own = float(app.spec.kill_grace or 0)
        except (TypeError, ValueError):
            own = 0.0
        return own if own > 0 else kill_grace()

    def kill(self, app: RunningApp) -> None:
        if app.exited:
            return
        app.closing = True
        for sid in list(app._timers):
            GLib.source_remove(sid)
        app._timers.clear()
        was_frozen, app.frozen = app.frozen, False
        self._count_frozen(app)
        if self._tree(app, signal.SIGKILL):     # wirkt auch auf angehaltene Prozesse
            self._later(app, LAST_RESORT, self._on_last)
        else:
            # noch kein Kind (reaper startet gerade) oder keins mehr: dann ihn selbst
            self._tree(app, signal.SIGKILL, include_root=True)
        if was_frozen:
            self._save()

    def _wake(self, app: RunningApp) -> None:
        # immer, nicht nur bei frozen=True: Angehaltene Prozesse nehmen kein SIGTERM an
        self._tree(app, signal.SIGCONT)
        if app.frozen:
            app.frozen = False
            self._count_frozen(app)
            self._save()
            self._emit(app, "thawed")

    def _term(self, app: RunningApp) -> None:
        """SIGTERM an den reaper: Er gibt es an sein Hauptkind weiter (siehe proc/reaper)."""
        try:
            if app._pidfd >= 0:
                signal.pidfd_send_signal(app._pidfd, signal.SIGTERM)
            elif tree.alive(app.reaper_pid, app.reaper_starttime or None):
                os.kill(app.reaper_pid, signal.SIGTERM)
        except OSError:
            pass

    def _polite_stop(self, app: RunningApp) -> None:
        cmd = [str(a) for a in (app.spec.polite_stop or [])]
        if cmd and self._run_polite(app, cmd):
            # Der Befehl (SIGTERM in die eigene Flatpak-Sandbox, Plasma abmelden) beendet die
            # App; Reste des Startskripts bekommen kurz danach die übliche Bitte. Wie lange
            # "kurz" ist, hängt davon ab, wie der Befehl ausgeht (_polite_poll).
            grace = self._grace(app)
            now = time.monotonic()
            follow = now + min(POLITE_FOLLOWUP, grace / 2)
            limit = max(follow, now + grace * POLITE_PATIENCE)
            self._later(app, POLITE_POLL, lambda a: self._polite_poll(a, follow, limit))
        else:
            self._term(app)

    def _polite_poll(self, app: RunningApp, follow: float, limit: float) -> None:
        """Sieht nach dem polite_stop-Befehl:
          läuft noch      weiter warten, höchstens bis limit (er meldet z. B. Plasma ab und
                          wartet selbst auf dessen Ende), dann SIGTERM
          Rückgabewert 0  hat etwas beendet: SIGTERM für die Reste zum Zeitpunkt follow
          sonst           nichts gefunden (3) oder gescheitert: sofort SIGTERM ans Hauptkind,
                          statt die Nachfrist abzuwarten"""
        polite = app._polite
        code = polite.poll() if polite is not None else POLITE_NOTHING
        now = time.monotonic()
        if code is None:
            if now >= limit:
                self._term(app)
            else:
                self._later(app, POLITE_POLL, lambda a: self._polite_poll(a, follow, limit))
        elif code == 0 and now < follow:
            self._later(app, follow - now, self._term)
        else:
            if code != 0:
                _log.info("polite_stop von %s endete mit %s: SIGTERM ans Hauptkind", app.entry_id, code)
            self._term(app)

    def _run_polite(self, app: RunningApp, cmd: list[str]) -> bool:
        try:
            env = build_env(self._session_env, app.spec, self._gs.game_display(), app.app_id)
        except Exception:
            return False
        # Die Schonfrist dieser App: couch-flatpak-stop wartet höchstens die Hälfte davon auf das
        # Ende der Sandbox, den Rest übernehmen SIGTERM und SIGKILL von hier (E99)
        env["COUCH_KILL_GRACE"] = "%g" % self._grace(app)
        exe = find_executable(cmd[0], env, None)
        if exe is None:
            _log.warning("polite_stop von %s nicht gefunden: %s", app.entry_id, cmd[0])
            return False
        if paths.test_mode() and not test_mode_allows(exe):
            system.record_write(cmd, "proc: polite_stop im Prüfmodus nicht ausgeführt")
            return False
        try:
            out: Any = open(app.log_path, "ab") if app.log_path else subprocess.DEVNULL
        except OSError:
            out = subprocess.DEVNULL
        try:
            app._polite = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            return True
        except OSError as e:
            _log.warning("polite_stop von %s scheitert: %s", app.entry_id, e)
            return False
        finally:
            if out is not subprocess.DEVNULL:
                out.close()

    def _on_grace(self, app: RunningApp) -> None:
        hit = self._tree(app, signal.SIGKILL)
        _log.warning("%s reagiert nicht auf die Bitte, SIGKILL an %d Prozesse", app.entry_id, len(hit))
        self._later(app, LAST_RESORT, self._on_last)

    def _on_last(self, app: RunningApp) -> None:
        # Sollte nie nötig sein: Der reaper endet von selbst, sobald er keine Kinder mehr hat.
        _log.error("reaper %d von %s lebt noch, wird mit beendet", app.reaper_pid, app.entry_id)
        self._tree(app, signal.SIGKILL, include_root=True)

    def shutdown(self) -> None:
        """Alles auftauen und schließen, ohne laufende Hauptschleife. BLOCKIERT bis zu
        COUCH_KILL_GRACE + 2 s: nie aus der Hauptschleife rufen, solange das Home weiterläuft
        (die Shell schließt dort über close() und wartet auf "exited")."""
        apps = [a for a in self._apps.values() if not a.exited]
        if not apps:
            return
        grace = kill_grace()
        t0 = time.monotonic()
        for app in apps:
            try:
                self.close(app)                 # auftauen + höfliche Stufe
            except Exception:
                _log.exception("Schließen von %s", app.entry_id)
        self._wait(apps, t0 + min(POLITE_FOLLOWUP, grace / 2))
        for app in apps:
            if not app.exited and app._polite is not None:
                self._term(app)
        self._wait(apps, t0 + grace)
        left = [a for a in apps if not a.exited]
        for app in left:
            self._tree(app, signal.SIGKILL)
        self._wait(left, t0 + grace + LAST_RESORT)
        for app in left:
            if not app.exited:
                self._tree(app, signal.SIGKILL, include_root=True)
        self._wait(left, t0 + grace + 2.0)
        for app in left:
            if not app.exited:
                _log.error("%s (reaper %d) ließ sich nicht beenden", app.entry_id, app.reaper_pid)

    def _wait(self, apps: list[RunningApp], deadline: float) -> None:
        while True:
            pending = [a for a in apps if not a.exited]
            left = deadline - time.monotonic()
            if not pending or left <= 0:
                return
            poller = select.poll()
            by_fd = {}
            for app in pending:
                if app._pidfd >= 0:
                    poller.register(app._pidfd, select.POLLIN)
                    by_fd[app._pidfd] = app
            for fd, _event in poller.poll(min(left, 0.25) * 1000):
                self._finish(by_fd[fd])
            for app in pending:                 # ohne pidfd: nachsehen
                if app._pidfd < 0 and not app.exited:
                    gone = app._popen.poll() is not None if app._popen is not None \
                        else not tree.alive(app.reaper_pid, app.reaper_starttime or None)
                    if gone:
                        self._finish(app)

    # ------------------------------------------------------------ running.json
    def _record(self, app: RunningApp) -> dict:
        return {"app_id": app.app_id, "entry_id": app.entry_id, "reaper_pid": app.reaper_pid,
                "reaper_starttime": app.reaper_starttime, "spec": asdict(app.spec),
                "started": app.started, "frozen": app.frozen,
                "frozen_total": round(app.frozen_total, 1), "frozen_since": app.frozen_since}

    def _save(self) -> None:
        data = {"version": 1, "next_n": self._next_n, "apps": [self._record(a) for a in self.running()]}
        try:
            system.write_atomic(self._file, jsontext.dumps(data, indent=1, default=str) + "\n")
        except OSError as e:
            _log.error("running.json nicht schreibbar: %s", e)

    def _load(self) -> dict | None:
        try:
            with open(self._file, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            _log.warning("running.json nicht lesbar (%s), beginne leer", e)
            return {}
        return data if isinstance(data, dict) else {}

    def adopt(self) -> list[RunningApp]:
        """Übernimmt lebende reaper aus running.json (Home-Neustart, Befund 23) und taut alle
        auf: Ein Spiel darf nie eingefroren liegen bleiben. Tote Einträge fallen weg.
        Meldet keine Ereignisse; die übernommenen Apps stehen danach in running()."""
        data = self._load()
        if data is None:
            return []
        try:
            self._next_n = max(self._next_n, int(data.get("next_n") or 1))
        except (TypeError, ValueError):
            pass
        try:
            focusable = set(self._gs.focusable_apps())
        except Exception:
            focusable = set()
        apps = data.get("apps")
        adopted = []
        for item in apps if isinstance(apps, list) else []:
            app = self._adopt_one(item, focusable) if isinstance(item, dict) else None
            if app is not None:
                adopted.append(app)
        for app in self._apps.values():
            self._next_n = max(self._next_n, app.app_id - APP_ID_BASE + 1)
        self._save()
        return adopted

    def _adopt_one(self, item: dict, focusable: set) -> RunningApp | None:
        try:
            # Ältere Fassungen schrieben bei Steam-Spielen Steams Nummer nach app_id und die des
            # reaper nach reaper_app_id (E57, verworfen): Es gilt immer die Nummer des reaper
            app_id = int(item.get("reaper_app_id") or item["app_id"])
            pid = int(item["reaper_pid"])
            start = int(item["reaper_starttime"])
            entry_id = str(item["entry_id"])
            started = float(item.get("started") or time.time())
        except (KeyError, TypeError, ValueError):
            return None
        if app_id in self._apps or self.by_entry(entry_id) is not None:
            return None
        if not (APP_ID_BASE < app_id <= APP_ID_MAX) or pid <= 1 or not start:
            return None
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return None
        try:
            # PID allein reicht nicht: Startzeit, Besitzer, Name und Kommandozeile müssen passen
            ok = (tree.alive(pid, start) and os.stat("/proc/%d" % pid).st_uid == os.getuid()
                  and tree.reaper_app_id(pid) == app_id)
        except OSError:
            ok = False
        if not ok:
            os.close(fd)
            _log.info("running.json: %s (reaper %d) läuft nicht mehr", entry_id, pid)
            return None
        app = RunningApp(app_id=app_id, entry_id=entry_id, reaper_pid=pid, started=started,
                         spec=_spec_from_dict(item.get("spec"), entry_id), reaper_starttime=start,
                         adopted=True, log_path=paths.app_log(entry_id))
        try:
            # angehaltene Zeit aus dem alten Home mitnehmen; ein laufendes Anhalten endet jetzt
            app.frozen_total = max(0.0, float(item.get("frozen_total") or 0.0))
            since = float(item.get("frozen_since") or 0.0)
            if 0 < since <= time.time():
                app.frozen_total += time.time() - since
        except (TypeError, ValueError):
            app.frozen_total = 0.0
        app._announced = True
        app._pidfd = fd
        self._attach(app)
        self._apps[app_id] = app
        self._tree(app, signal.SIGCONT)
        app.visible = app_id in focusable
        _log.info("übernommen: %s AppId=%d reaper=%d", entry_id, app_id, pid)
        return app
