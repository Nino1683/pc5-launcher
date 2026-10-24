"""Gamescope über X-Eigenschaften steuern und beobachten (python-xlib).

    gs = connect()                      # Server 0 = $DISPLAY, Server 1 = $STEAM_GAME_DISPLAY_0
    gs.set("GAMESCOPECTRL_BASELAYER_APPID", [app_id, HOME_APPID])
    gs.watch(lambda kind: ...)          # "focus" | "apps" | "size" | "baselayer" | "basewindow"

Alle Steuer-Eigenschaften liegen an der Wurzel von Server 0. Fenster-Eigenschaften
(STEAM_GAME, STEAM_OVERLAY, STEAM_INPUT_FOCUS, _NET_WM_WINDOW_OPACITY) am Fenster; liegt es
auf Server 1, `server=1` mitgeben: Fenster-IDs sind zwischen den Servern nicht eindeutig.

Ein Display gilt nur als Gamescope, wenn seine Wurzel GAMESCOPE_XWAYLAND_SERVER_ID trägt.
Andere GAMESCOPE_*-Eigenschaften setzt Steam auch auf dem KDE-Desktop; daran erkannt, würde
das Home auf den Schreibtisch des Nutzers schreiben.

Die X-Verbindungen hängen über ihre Dateinummern in der GLib-Hauptschleife, es gibt keinen
eigenen Thread. Rückrufe kommen nur, wenn sich ein Wert wirklich geändert hat: Gamescope
schreibt GAMESCOPE_FOCUSABLE_* mehrmals pro Sekunde mit gleichem Inhalt neu (01, Fallen).
"""
from __future__ import annotations

import glob
import os
import threading
import time
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402
from Xlib import X, Xatom, display, error  # noqa: E402
from Xlib.ext import res as xres  # noqa: E402

from .. import log, xfix  # noqa: E402
from . import GamescopeError  # noqa: E402,F401  (hier unter dem festgelegten Namen erreichbar)

try:                                    # GLib.unix_fd_add_full ist in neuem PyGObject abgekündigt
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
    _fd_add = GLibUnix.fd_add_full
except (ValueError, ImportError, AttributeError):
    _fd_add = GLib.unix_fd_add_full

_log = log.get("gs")

SERVER_ID = "GAMESCOPE_XWAYLAND_SERVER_ID"
# Wurzel-Eigenschaften, deren Änderung watch() meldet, und unter welchem Namen
KINDS = {
    "GAMESCOPE_FOCUSED_APP": "focus",
    "GAMESCOPE_FOCUSED_APP_GFX": "focus",
    "GAMESCOPE_FOCUSABLE_APPS": "apps",
    "GAMESCOPE_FOCUSABLE_WINDOWS": "apps",
    "GAMESCOPECTRL_BASELAYER_APPID": "baselayer",
    "GAMESCOPECTRL_BASELAYER_WINDOW": "basewindow",
}
_X_ERRORS = (error.XError, error.ConnectionClosedError, OSError, RuntimeError)


def _plain(name: str) -> str:
    return name[:-2] if name.endswith(".0") else name


def _open(name: str):
    if name.startswith(":"):
        # Gamescopes Xwaylands sind immer lokale Sockets. Ohne diese Vorprüfung versucht
        # python-xlib bei fehlendem Socket noch eine TCP-Verbindung.
        number = _plain(name)[1:]
        if not number.isdigit() or not os.path.exists("/tmp/.X11-unix/X" + number):
            raise GamescopeError("X-Display %s gibt es nicht." % name)
    try:
        return xfix.repair(display.Display(name))
    except Exception as e:      # python-xlib wirft je nach Lage sehr verschiedene Ausnahmen
        raise GamescopeError("X-Display %s ist nicht erreichbar: %s" % (name, e)) from e


def _root_value(d, name: str) -> int | None:
    prop = d.screen().root.get_full_property(d.intern_atom(name), X.AnyPropertyType)
    return int(prop.value[0]) if prop is not None and len(prop.value) else None


def connect(display_name: str | None = None, game_display: str | None = None) -> "Gamescope":
    """Verbindet zu Server 0 ($DISPLAY) und Server 1. Server 1 kommt aus $STEAM_GAME_DISPLAY_0;
    fehlt die Variable, wird er gesucht (find_game_display). GamescopeError, wenn eines kein
    Gamescope ist.

    Die echte Sitzung exportiert nur DISPLAY und GAMESCOPE_WAYLAND_DISPLAY: STEAM_GAME_DISPLAY_0
    setzt Gamescope nur für eigene Kinder, und der Client der Sitzung ist keins. Ohne die Suche
    landeten alle Spiele neben Home und Overlay auf Server 0, und die Spielauflösung griffe nie.
    """
    name0 = display_name or os.environ.get("DISPLAY") or ""
    if not name0:
        raise GamescopeError("DISPLAY ist nicht gesetzt.")
    name1 = game_display or os.environ.get("STEAM_GAME_DISPLAY_0") or ""
    if not name1:
        name1 = find_game_display(name0) or ""
        if name1:
            _log.info("Spiel-Server gefunden: %s (Xwayland 1 zum Gamescope von %s)", name1, name0)
        else:
            _log.warning("kein zweites Xwayland zu %s gefunden: Spiele laufen neben dem Home "
                         "auf Server 0, die Spielauflösung ist ohne Wirkung", name0)
            name1 = name0
    if _plain(name1) == _plain(name0):
        name1 = name0                           # ":1" und ":1.0" sind derselbe Server
    return Gamescope(name0, name1)


def pick_game_display(found: list[tuple[str, int, int]], name0: str) -> str | None:
    """Aus dem Ergebnis von discover() den Server 1 desselben Gamescope wie name0 wählen.
    Verglichen wird über GAMESCOPE_PID: Nebenan können andere Gamescope-Instanzen laufen
    (Prüfläufe), deren Server 1 nicht unserer ist. Ohne eindeutige PID wird nicht geraten."""
    pid0 = next((pid for name, sid, pid in found if _plain(name) == _plain(name0) and sid == 0), 0)
    if not pid0:
        return None
    hits = [name for name, sid, pid in found
            if sid == 1 and pid == pid0 and _plain(name) != _plain(name0)]
    return hits[0] if len(hits) == 1 else None


def _xwayland_count(pid: int) -> int:
    """--xwayland-count aus der Befehlszeile von Gamescope; 1, wenn nichts dasteht, 0 = unbekannt."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return 0
    for i, arg in enumerate(args):
        value = None
        if arg == b"--xwayland-count" and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith(b"--xwayland-count="):
            value = arg.split(b"=", 1)[1]
        if value is not None:
            try:
                return max(1, int(value))
            except ValueError:
                return 0
    return 1


def _proc_stat(pid: int, proc_root: str = "/proc") -> tuple[str, int, int] | None:
    """(Name, Prozessgruppe, Sitzung) aus <proc>/<pid>/stat; None, wenn es den Prozess nicht gibt."""
    try:
        with open(os.path.join(proc_root, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            raw = f.read()
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        rest = raw[raw.rindex(")") + 2:].split()
        return comm, int(rest[2]), int(rest[3])
    except (OSError, ValueError, IndexError):
        return None


def _gamescope_pid(name0: str) -> int:
    """GAMESCOPE_PID an der Wurzel von name0 (dem eigenen Display); 0, wenn es fehlt."""
    try:
        d = _open(name0)
        try:
            return _root_value(d, "GAMESCOPE_PID") or 0
        finally:
            d.close()
    except Exception:
        return 0


def own_displays(name0: str, gamescope_pid: int | None = None, proc_root: str = "/proc") -> list[str] | None:
    """Die X-Server des Gamescope, dem name0 gehört, ohne einen fremden X-Socket anzufassen:
    GAMESCOPE_PID an der Wurzel von name0, dazu alle Prozesse „Xwayland“ aus derselben Sitzung
    bzw. Prozessgruppe (Gamescope startet sie abgenabelt, sie sind keine Kinder mehr) mit dem
    Display aus ihrer Befehlszeile ("Xwayland :2 -rootless …").
    None, wenn sich das nicht bestimmen lässt (name0 selbst muss dabei sein)."""
    pid = _gamescope_pid(name0) if gamescope_pid is None else gamescope_pid
    mine = _proc_stat(pid, proc_root) if pid else None
    if mine is None:
        return None
    _comm, pgrp, sid = mine
    found: list[str] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    for entry in names:
        if not entry.isdigit():
            continue
        st = _proc_stat(int(entry), proc_root)
        if st is None or not st[0].startswith("Xwayland") or (st[2] != sid and st[1] != pgrp):
            continue
        try:
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        for arg in args[1:]:
            text = arg.decode("ascii", "replace")
            if text.startswith(":") and text[1:].isdigit():
                found.append(text)
                break
    if _plain(name0) not in [_plain(n) for n in found]:
        return None                             # passt nicht zusammen: lieber nichts behaupten
    return sorted(set(found), key=lambda n: int(n[1:]))


def _probe_for(name0: str):
    """Die Suche für find_game_display: nur die eigenen X-Server. Lassen die sich nicht
    bestimmen, werden in der echten Sitzung alle Sockets gefragt (wie früher; dort gibt es nur
    die von Gamescope). Im Prüfmodus nie: Nebenan liegt der X-Socket des echten Desktops."""
    def probe() -> list[tuple[str, int, int]]:
        from .. import paths
        test = paths.test_mode()
        pid = _gamescope_pid(name0)
        names = own_displays(name0, gamescope_pid=pid) if pid else None
        if names is not None and not test and len(names) < _xwayland_count(pid):
            # Gamescope nennt mehr X-Server, als sich über seine Sitzung finden ließen: nicht
            # auf die halbe Liste verlassen (sonst landeten die Spiele neben dem Home)
            _log.info("nur %d von %d X-Servern des Gamescope gefunden: frage alle X-Sockets",
                      len(names), _xwayland_count(pid))
            names = None
        if names is None:
            if test:
                _log.warning("X-Server des Gamescope von %s nicht bestimmbar: im Prüfmodus wird "
                             "kein fremder X-Socket gefragt", name0)
                names = [name0]
            else:
                _log.info("X-Server des Gamescope von %s nicht bestimmbar: frage alle X-Sockets", name0)
        return discover(names=names)
    return probe


def find_game_display(name0: str, wait: float = 3.0, probe=None) -> str | None:
    """Sucht Xwayland 1 des Gamescope, dem name0 als Server 0 gehört. None, wenn es keins gibt.

    Gefragt werden nur die X-Server dieses Gamescope (own_displays), nie fremde Sockets unter
    /tmp/.X11-unix. GAMESCOPE_XWAYLAND_SERVER_ID erscheint erst kurz nach dem Start an der
    Wurzel. Sagt die Befehlszeile von Gamescope, dass es mehrere X-Server gibt, wird deshalb bis
    zu wait Sekunden nachgefragt; sonst genügt ein Durchgang. Blockiert: nur beim Start aufrufen."""
    probe = probe or _probe_for(name0)
    end = time.monotonic() + max(0.0, wait)
    while True:
        found = probe()
        hit = pick_game_display(found, name0)
        if hit:
            return hit
        pid0 = next((pid for name, sid, pid in found
                     if _plain(name) == _plain(name0) and sid == 0), 0)
        if not pid0 or _xwayland_count(pid0) < 2 or time.monotonic() >= end:
            return None
        time.sleep(0.2)


def discover(timeout: float = 2.0, names: list[str] | None = None) -> list[tuple[str, int, int]]:
    """Gamescope-Xwaylands: (display, server_id, gamescope_pid). names: nur diese Displays
    fragen; ohne Angabe alle Sockets unter /tmp/.X11-unix (für Werkzeuge und Tests). Liest nur.
    Jede Probe läuft in einem eigenen Thread, damit ein hängender X-Server die Suche nicht
    aufhält."""
    found: list[tuple[str, int, int]] = []
    lock = threading.Lock()

    def probe(name: str) -> None:
        try:
            d = xfix.repair(display.Display(name))
            try:
                sid = _root_value(d, SERVER_ID)
                pid = _root_value(d, "GAMESCOPE_PID")
            finally:
                d.close()
            if sid is not None:
                with lock:
                    found.append((name, sid, pid or 0))
        except Exception:
            pass

    if names is None:
        names = [":" + os.path.basename(sock)[1:] for sock in sorted(glob.glob("/tmp/.X11-unix/X*"))]
    threads = []
    for name in names:
        t = threading.Thread(target=probe, args=(name,), daemon=True)
        t.start()
        threads.append(t)
    end = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        return sorted(found, key=lambda e: (e[2], e[1]))


class Gamescope:
    """interfaces.Gamescope, dazu einige Zusätze für FocusManager und Tests."""

    def __init__(self, name0: str, name1: str | None = None):
        name1 = name1 or name0
        self._names = [name0, name1]
        d0 = _open(name0)
        try:
            sid = _root_value(d0, SERVER_ID)
            if sid is None:
                raise GamescopeError("%s ist kein Gamescope-Xwayland (%s fehlt an der Wurzel)." % (name0, SERVER_ID))
            if sid != 0:
                raise GamescopeError("%s ist Gamescopes Server %d, gebraucht wird Server 0." % (name0, sid))
            if name1 == name0:
                d1 = d0
            else:
                d1 = _open(name1)
                try:
                    sid1 = _root_value(d1, SERVER_ID)
                    if sid1 is None:
                        raise GamescopeError("%s ist kein Gamescope-Xwayland (%s fehlt an der Wurzel)."
                                             % (name1, SERVER_ID))
                    pid0, pid1 = _root_value(d0, "GAMESCOPE_PID"), _root_value(d1, "GAMESCOPE_PID")
                    if sid1 == 0 or (pid0 and pid1 and pid0 != pid1):
                        raise GamescopeError("%s gehört nicht als Spiel-Server zum Gamescope von %s." % (name1, name0))
                except BaseException:
                    d1.close()
                    raise
        except BaseException:
            d0.close()
            raise
        self._conns = [d0] if d1 is d0 else [d0, d1]
        self._d = [d0, d1]
        self._roots = [d0.screen().root, d1.screen().root]
        self.has_game_server = d1 is not d0
        self.closed = False
        for d in self._conns:
            # Fehler für Fenster, die es schon nicht mehr gibt, kommen verspätet an; sie
            # sollen nicht auf stderr landen und nichts abbrechen
            d.set_error_handler(self._on_x_error)
        self._callbacks: list[Callable[[str], None]] = []
        self._window_callbacks: list[Callable[[int, int], None]] = []
        self._cache: dict[str, tuple | None] = {}
        self._atoms: dict[int, str] | None = None      # Atom -> Name der beobachteten Eigenschaften
        self._size = self._query_size(0)
        self._sources: list[int] = []
        self._idle = 0
        self._draining = False

    # ------------------------------------------------------------ Grundlegendes
    @staticmethod
    def _on_x_error(err, _request=None) -> None:
        _log.debug("X-Fehler: %s", err)

    def _fail(self, e: Exception):
        if isinstance(e, (error.ConnectionClosedError, OSError)):
            self._lost()
            return GamescopeError("Die Verbindung zu Gamescope ist weg: %s" % e)
        return GamescopeError("X-Fehler: %s" % e)

    def _lost(self) -> None:
        if not self.closed:
            self.closed = True
            _log.error("Verbindung zu Gamescope verloren")
            self._unwatch()

    def _target(self, window: int | None, server: int):
        d = self._d[1 if server else 0]
        return d, (self._roots[1 if server else 0] if window is None else d.create_resource_object("window", window))

    def game_display(self) -> str:
        return self._names[1]

    def display_name(self) -> str:
        return self._names[0]

    def close(self) -> None:
        self.closed = True
        self._unwatch()
        for d in self._conns:
            try:
                d.close()
            except Exception:
                pass

    # ------------------------------------------------------------ lesen / schreiben
    def get(self, name: str, window: int | None = None, server: int = 0) -> list[int] | None:
        """Wert einer Eigenschaft (CARDINAL/32 und Ähnliches) oder None, wenn sie fehlt."""
        if self.closed:
            raise GamescopeError("Die Verbindung zu Gamescope ist weg.")
        d, target = self._target(window, server)
        try:
            prop = target.get_full_property(d.intern_atom(name), X.AnyPropertyType)
        except error.XError:
            return None                         # Fenster gibt es nicht mehr
        except _X_ERRORS as e:
            raise self._fail(e) from e
        finally:
            self._kick()
        if prop is None:
            return None
        try:
            return [int(v) for v in prop.value]
        except (TypeError, ValueError):         # Text-Eigenschaft in alter python-xlib-Fassung
            return [ord(c) for c in prop.value]

    def set(self, name: str, values: list[int], window: int | None = None, server: int = 0) -> None:
        if self.closed:
            raise GamescopeError("Die Verbindung zu Gamescope ist weg.")
        d, target = self._target(window, server)
        try:
            target.change_property(d.intern_atom(name), Xatom.CARDINAL, 32, [int(v) for v in values])
            d.flush()
        except _X_ERRORS as e:
            raise self._fail(e) from e

    def delete(self, name: str, window: int | None = None, server: int = 0) -> None:
        if self.closed:
            raise GamescopeError("Die Verbindung zu Gamescope ist weg.")
        d, target = self._target(window, server)
        try:
            target.delete_property(d.intern_atom(name))
            d.flush()
        except _X_ERRORS as e:
            raise self._fail(e) from e

    def sync(self, server: int | None = None) -> None:
        """Wartet, bis der Server alles Bisherige verarbeitet hat (vor dem Einblenden eines
        Fensters durch GTK: die Markierung muss dann schon stehen)."""
        conns = self._conns if server is None else [self._d[1 if server else 0]]
        try:
            for d in conns:
                d.sync()
        except _X_ERRORS as e:
            raise self._fail(e) from e
        finally:
            self._kick()

    # ------------------------------------------------------------ Rückmeldungen von Gamescope
    def _first(self, name: str) -> int:
        v = self.get(name)
        return v[0] if v else 0

    def focused_app(self) -> int:
        """App-ID des Fensters mit der Eingabe; 0, solange das Overlay die Eingabe hat."""
        return self._first("GAMESCOPE_FOCUSED_APP")

    def focused_app_gfx(self) -> int:
        """App-ID der Basisebene; bleibt beim Spiel, auch wenn das Overlay offen ist."""
        return self._first("GAMESCOPE_FOCUSED_APP_GFX")

    def focusable_apps(self) -> list[int]:
        return self.get("GAMESCOPE_FOCUSABLE_APPS") or []

    def focusable_windows(self) -> list[tuple[int, int, int]]:
        """(xid, app_id, pid) über beide Server. XIDs wiederholen sich zwischen den Servern."""
        v = self.get("GAMESCOPE_FOCUSABLE_WINDOWS") or []
        return [(v[i], v[i + 1], v[i + 2]) for i in range(0, len(v) - 2, 3)]

    def hdr_output_active(self) -> bool:
        return self._first("GAMESCOPE_HDR_OUTPUT_FEEDBACK") == 1

    def _query_size(self, server: int) -> tuple[int, int]:
        try:
            geo = self._roots[1 if server else 0].get_geometry()
        except _X_ERRORS as e:
            raise self._fail(e) from e
        return int(geo.width), int(geo.height)

    def root_size(self) -> tuple[int, int]:
        """Größe der Wurzel von Server 0 = Ausgabegröße."""
        if self.closed:
            return self._size
        try:
            self._size = self._query_size(0)
        finally:
            self._kick()
        return self._size

    def game_root_size(self) -> tuple[int, int]:
        """Größe der Wurzel von Server 1 = Auflösung, in der Spiele rendern."""
        try:
            return self._query_size(1)
        finally:
            self._kick()

    # ------------------------------------------------------------ Fenster
    def window_state(self, xid: int, server: int = 0) -> dict | None:
        """Was das Netz für Fenster ohne App-ID wissen muss. None, wenn es das Fenster nicht gibt."""
        d, win = self._target(xid, server)
        try:
            attrs = win.get_attributes()
            tag = win.get_full_property(d.intern_atom("STEAM_GAME"), X.AnyPropertyType)
            pid = self._client_pid(d, win)
        except error.XError:
            return None
        except _X_ERRORS as e:
            raise self._fail(e) from e
        finally:
            self._kick()
        return {"viewable": attrs.map_state == X.IsViewable,
                "input_only": attrs.win_class == X.InputOnly,
                "override": bool(attrs.override_redirect),
                "steam_game": int(tag.value[0]) if tag is not None and len(tag.value) else None,
                "pid": pid}

    @staticmethod
    def _client_pid(d, win) -> int | None:
        # Zuerst XRes: die PID, wie der X-Server sie sieht (so findet auch Gamescope sie).
        # _NET_WM_PID setzt das Programm selbst; in einer Flatpak-Sandbox steht dort die
        # PID aus deren eigenem Namensraum.
        try:
            reply = d.res_query_client_ids([{"client": win.id, "mask": xres.LocalClientPIDMask}])
            for item in reply.ids:
                if item.spec.mask & xres.LocalClientPIDMask and item.value:
                    return int(item.value[0])
        except Exception:
            pass
        prop = win.get_full_property(d.intern_atom("_NET_WM_PID"), X.AnyPropertyType)
        return int(prop.value[0]) if prop is not None and len(prop.value) else None

    def resize_window(self, xid: int, width: int, height: int, server: int = 0) -> None:
        d, win = self._target(xid, server)
        try:
            win.configure(x=0, y=0, width=int(width), height=int(height))
            d.flush()
        except _X_ERRORS as e:
            raise self._fail(e) from e

    # ------------------------------------------------------------ beobachten
    def watch(self, callback: Callable[[str], None]) -> None:
        """callback(art) mit art in "focus" | "apps" | "size" | "baselayer" | "basewindow"; nur bei
        geänderten Werten, immer aus der Hauptschleife."""
        self._callbacks.append(callback)
        self._ensure_watch()

    def watch_windows(self, callback: Callable[[int, int], None]) -> None:
        """callback(server, xid) für jedes neu eingeblendete Fenster (beide Server)."""
        self._window_callbacks.append(callback)
        self._ensure_watch()

    def _snapshot(self, name: str) -> tuple | None:
        v = self.get(name)
        return tuple(v) if v is not None else None

    def _ensure_watch(self) -> None:
        if self._sources or self.closed:
            return
        try:
            for i, d in enumerate(self._conns):
                mask = X.SubstructureNotifyMask | X.StructureNotifyMask
                if i == 0:
                    mask |= X.PropertyChangeMask
                d.screen().root.change_attributes(event_mask=mask)
                d.flush()
        except _X_ERRORS as e:
            raise self._fail(e) from e
        for name in KINDS:
            self._cache[name] = self._snapshot(name)
        cond = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR
        for index, d in enumerate(self._conns):
            self._sources.append(_fd_add(GLib.PRIORITY_DEFAULT, d.fileno(), cond, self._on_fd, index))
        self._kick()

    def _unwatch(self) -> None:
        for sid in self._sources:
            if sid:
                GLib.source_remove(sid)
        self._sources = []
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0

    def _kick(self) -> None:
        # Jede Anfrage mit Antwort liest nebenbei wartende Ereignisse in die Warteschlange
        # von python-xlib; die Dateinummer meldet sich dafür nicht mehr. Also nachsehen, mit dem
        # Vorrang der Dateinummer: Leerlauf-Aufträge warteten am Fernseher oft 3 bis 5 s (loopwatch)
        if self._sources and not self._idle and not self._draining:
            self._idle = GLib.idle_add(self._on_idle, priority=GLib.PRIORITY_DEFAULT)

    def _on_idle(self) -> bool:
        self._idle = 0
        self._drain()
        return False

    def _on_fd(self, _fd, condition, index) -> bool:
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self._sources[index] = 0            # diese Quelle endet mit dem Rückgabewert selbst
            self._lost()
            return False
        try:
            self._drain()
        except Exception:               # sonst nähme GLib die Quelle weg und kein Ereignis käme mehr an
            _log.exception("Ereignisse von Gamescope")
        return bool(self._sources)

    def _drain(self) -> None:
        if self._draining or self.closed:
            return
        kinds: list[str] = []
        maps: list[tuple[int, int]] = []
        self._draining = True
        try:
            while True:
                touched, resized = self._collect(maps)
                if not touched and not resized:
                    break
                if resized:
                    size = self._query_size(0)
                    if size != self._size:
                        self._size = size
                        if "size" not in kinds:
                            kinds.append("size")
                for name in touched:
                    value = self._snapshot(name)
                    if value == self._cache.get(name):
                        continue
                    self._cache[name] = value
                    kind = KINDS.get(name)
                    if kind and kind not in kinds:
                        kinds.append(kind)
        except GamescopeError:
            return
        except _X_ERRORS as e:
            self._fail(e)
            return
        finally:
            self._draining = False
        # Rückrufe erst nach dem Leeren: sie dürfen selbst lesen und schreiben
        for kind in kinds:
            for cb in list(self._callbacks):
                self._call(cb, kind)
        for server, xid in maps:
            for cb in list(self._window_callbacks):
                self._call(cb, server, xid)

    def _collect(self, maps: list) -> tuple[list[str], bool]:
        """Holt alle wartenden Ereignisse ab: geänderte Eigenschaften (Namen), Größenwechsel."""
        touched: list[str] = []
        resized = False
        for index, d in enumerate(self._conns):
            root = d.screen().root
            while d.pending_events():
                e = d.next_event()
                if e.type == X.PropertyNotify and index == 0 and e.window == root:
                    # Die Wurzel meldet jede Eigenschaft (auch Zähler, die laufend wechseln):
                    # hier nur nachschlagen, gelesen wird später und nur das Beobachtete
                    if self._atoms is None:
                        self._atoms = {d.intern_atom(n): n for n in KINDS}
                    name = self._atoms.get(e.atom)
                    if name and name not in touched:
                        touched.append(name)
                elif e.type == X.ConfigureNotify and index == 0 and e.window == root:
                    resized = True
                elif e.type == X.MapNotify and e.event == root and self._window_callbacks:
                    server = 1 if (d is self._d[1] and self.has_game_server) else 0
                    maps.append((server, e.window.id))
        return touched, resized

    @staticmethod
    def _call(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:
            _log.exception("Beobachter %r", cb)
