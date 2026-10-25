"""Attrappen für Gamescope, Fokus, Apps und Eingabe (Schalter COUCH_FAKE_SERVICES=1, nur im
Prüfmodus) und eine Handuhr. So laufen Home-Kern und Shell, ohne dass die echten Dienste da
sind, und ohne dass ein Prozess gestartet oder ein Gerät geöffnet wird.

Sie halten sich an interfaces.py und schreiben mit, was verlangt wurde (calls, routes …).
Mit einer echten Anzeige (use_x) markiert FakeFocus die beiden Fenster so, dass ein
unsichtbares Gamescope sie wirklich zeigt; "Apps" bleiben reine Buchführung.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import APP_ID_BASE, HOME_APPID, log
from ..launch.spec import LaunchError, LaunchSpec

_log = log.get("fakes")


class ManualScheduler:
    """Uhr für Unit-Tests: Zeit vergeht nur über advance()."""

    def __init__(self) -> None:
        self.t = 1000.0
        self._timers: list[list] = []       # [fällig, laufende Nummer, fn, erledigt]
        self._n = 0

    def now(self) -> float:
        return self.t

    def later(self, ms: float, fn: Callable[[], None]) -> list:
        self._n += 1
        timer = [self.t + ms / 1000.0, self._n, fn, False]
        self._timers.append(timer)
        return timer

    def cancel(self, timer: list | None) -> None:
        if timer is not None:
            timer[3] = True

    def soon(self, fn: Callable[[], None]) -> None:
        fn()

    def advance(self, ms: float) -> None:
        end = self.t + ms / 1000.0
        while True:
            due = sorted((t for t in self._timers if not t[3] and t[0] <= end + 1e-9),
                         key=lambda t: (t[0], t[1]))
            if not due:
                break
            timer = due[0]
            timer[3] = True
            self.t = max(self.t, timer[0])
            timer[2]()
        self._timers = [t for t in self._timers if not t[3]]
        self.t = end


class FakeGamescope:
    def __init__(self, use_x: bool = False):
        self.props: dict[tuple[str, int | None], list[int]] = {}
        self._watchers: list[Callable[[str], None]] = []
        self.focused = HOME_APPID
        self.focusable = [HOME_APPID]
        self.size = (1280, 720)
        self.display_name = os.environ.get("STEAM_GAME_DISPLAY_0") or os.environ.get("DISPLAY", ":0")
        self._x: Any = None
        if use_x:
            self._attach_x()

    # ---- echte Anzeige (nur e2e): Wurzelgröße lesen, Fenster markieren, Größenwechsel melden
    def _attach_x(self) -> None:
        try:
            from gi.repository import GLib
            from Xlib import X, display as xdisplay
            d = xdisplay.Display()
            root = d.screen().root
            root.change_attributes(event_mask=X.StructureNotifyMask)
            d.sync()
            self._x = d

            def readable(*_a: Any) -> bool:
                changed = False
                while d.pending_events():
                    e = d.next_event()
                    if e.type == X.ConfigureNotify and e.window == root:
                        changed = True
                if changed:
                    self.fire("size")
                return True
            GLib.io_add_watch(d.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, readable)
        except Exception:
            _log.exception("keine X-Verbindung für die Attrappe")
            self._x = None

    def x_set(self, name: str, values: list[int], window: int | None = None) -> None:
        if self._x is None:
            return
        from Xlib import Xatom
        d = self._x
        target = d.create_resource_object("window", window) if window else d.screen().root
        target.change_property(d.intern_atom(name), Xatom.CARDINAL, 32, list(values))
        d.sync()                # steht am Server, bevor GTK das Fenster einblendet

    # ---- interfaces.Gamescope
    def get(self, name: str, window: int | None = None) -> list[int] | None:
        return self.props.get((name, window))

    def set(self, name: str, values: list[int], window: int | None = None) -> None:
        self.props[(name, window)] = list(values)
        self.x_set(name, values, window)

    def delete(self, name: str, window: int | None = None) -> None:
        self.props.pop((name, window), None)

    def focused_app(self) -> int:
        return self.focused

    def focusable_apps(self) -> list[int]:
        return list(self.focusable)

    def focusable_windows(self) -> list[tuple[int, int, int]]:
        return []

    def root_size(self) -> tuple[int, int]:
        if self._x is not None:
            g = self._x.screen().root.get_geometry()
            return g.width, g.height
        return self.size

    def game_display(self) -> str:
        return self.display_name

    def hdr_output_active(self) -> bool:
        return False

    def watch(self, callback: Callable[[str], None]) -> None:
        self._watchers.append(callback)

    def fire(self, what: str) -> None:
        for fn in list(self._watchers):
            fn(what)


class FakeFocus:
    def __init__(self, gs: FakeGamescope, config: Any = None):
        self.gs = gs
        self.foreground: int | None = None
        self.overlay_mode = "hidden"
        self.tagged: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.started = False
        self._observers: list[Callable[[], None]] = []

    def tag_window(self, role: str, xid: int) -> None:
        self.tagged[role] = xid
        self.calls.append(("tag", role, xid))
        if role == "home":
            self.gs.x_set("STEAM_GAME", [HOME_APPID], xid)
        else:
            self.gs.x_set("STEAM_OVERLAY", [1], xid)
            self.gs.x_set("STEAM_INPUT_FOCUS", [0], xid)
            self.gs.x_set("_NET_WM_WINDOW_OPACITY", [0], xid)

    def start(self) -> None:
        self.started = True
        self.calls.append(("start",))
        self.gs.set("GAMESCOPECTRL_BASELAYER_APPID", [HOME_APPID])

    def set_foreground(self, app_id: int | None) -> None:
        self.calls.append(("foreground", app_id))
        self.foreground = app_id
        self.gs.focused = app_id if app_id is not None else HOME_APPID
        self._changed()

    def overlay(self, mode: str) -> None:
        self.calls.append(("overlay", mode))
        self.overlay_mode = mode
        xid = self.tagged.get("overlay")
        if xid:
            self.gs.x_set("STEAM_INPUT_FOCUS", [1 if mode == "active" else 0], xid)
            self.gs.x_set("_NET_WM_WINDOW_OPACITY", [0 if mode == "hidden" else 0xFFFFFFFF], xid)
        self._changed()

    def apply_session_defaults(self) -> None:
        self.calls.append(("defaults",))

    def on_change(self, fn: Callable[[], None]) -> None:
        self._observers.append(fn)

    def _changed(self) -> None:
        for fn in list(self._observers):
            fn()


@dataclass(eq=False)
class FakeApp:
    app_id: int
    entry_id: str
    spec: LaunchSpec
    reaper_pid: int = 0
    started: float = 0.0
    frozen: bool = False
    visible: bool = False
    exit_code: int | None = None


class FakeProcs:
    """Buchführung statt Prozesse. Mit scheduler erscheint das "Fenster" von selbst
    (spec.env COUCH_FAKE_VISIBLE_MS, Vorgabe 200; -1 = nie) und close() endet nach 100 ms;
    ohne scheduler steuert der Test alles über show() und exit()."""

    def __init__(self, gs: FakeGamescope, session_env: dict | None = None, scheduler: Any = None):
        self.gs = gs
        self.session_env = dict(session_env or {})
        self.scheduler = scheduler
        self._apps: list[FakeApp] = []
        self._observers: list[Callable[[Any, str], None]] = []
        self._n = 0
        self.calls: list[tuple] = []
        self.close_exits = scheduler is not None     # close() beendet die App von selbst
        self.fail_next: LaunchError | None = None

    def _notify(self, app: FakeApp, what: str) -> None:
        for fn in list(self._observers):
            fn(app, what)

    # ---- interfaces.AppManager
    def launch(self, spec: LaunchSpec) -> FakeApp:
        self.calls.append(("launch", spec.entry_id))
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        if spec.env.get("COUCH_FAKE_LAUNCH_ERROR"):
            raise LaunchError(spec.env["COUCH_FAKE_LAUNCH_ERROR"])
        self._n += 1
        app = FakeApp(APP_ID_BASE + self._n, spec.entry_id, spec, reaper_pid=100000 + self._n,
                      started=time.time())
        self._apps.append(app)
        self._notify(app, "started")
        if self.scheduler is not None:
            visible_ms = int(spec.env.get("COUCH_FAKE_VISIBLE_MS", 200))
            if visible_ms >= 0:
                self.scheduler.later(visible_ms, lambda: self.show(app))
            if spec.env.get("COUCH_FAKE_EXIT_MS"):
                self.scheduler.later(int(spec.env["COUCH_FAKE_EXIT_MS"]),
                                     lambda: self.exit(app, int(spec.env.get("COUCH_FAKE_EXIT_CODE", 0))))
        return app

    def running(self) -> list[FakeApp]:
        return list(self._apps)

    def by_entry(self, entry_id: str) -> FakeApp | None:
        return next((a for a in self._apps if a.entry_id == entry_id), None)

    def by_app_id(self, app_id: int) -> FakeApp | None:
        return next((a for a in self._apps if a.app_id == app_id), None)

    def freeze(self, app: FakeApp) -> None:
        self.calls.append(("freeze", app.entry_id))
        if not app.frozen:
            app.frozen = True
            self._notify(app, "frozen")

    def thaw(self, app: FakeApp) -> None:
        self.calls.append(("thaw", app.entry_id))
        if app.frozen:
            app.frozen = False
            self._notify(app, "thawed")

    def close(self, app: FakeApp) -> None:
        self.calls.append(("close", app.entry_id))
        if not self.close_exits:
            return
        if self.scheduler is not None:
            self.scheduler.later(100, lambda: self.exit(app))
        else:
            self.exit(app)

    def kill(self, app: FakeApp) -> None:
        self.calls.append(("kill", app.entry_id))
        self.exit(app, 137)

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        for app in list(self._apps):
            app.frozen = False
            self.exit(app)

    def on_change(self, fn: Callable[[Any, str], None]) -> None:
        self._observers.append(fn)

    # ---- Steuerung durch Tests
    def show(self, app: FakeApp) -> None:
        if app not in self._apps or app.visible:
            return
        app.visible = True
        if app.app_id not in self.gs.focusable:
            self.gs.focusable.insert(0, app.app_id)
        self._notify(app, "visible")

    def exit(self, app: FakeApp, code: int = 0) -> None:
        if app not in self._apps:
            return
        self._apps.remove(app)
        app.exit_code = code
        if app.app_id in self.gs.focusable:
            self.gs.focusable.remove(app.app_id)
        if self.gs.focused == app.app_id:
            self.gs.focused = HOME_APPID        # Gamescope fällt von selbst aufs Home zurück
        self._notify(app, "exited")


# Sprungtasten des Zeiger-Modus (padmouse.SPOT_BUTTONS) und die Stelle, die spot-<taste> „merkt“
SPOT_NAMES = ("l1", "r1", "up", "down", "left", "right")
FAKE_SPOT = {"x": 0.5, "y": 0.5, "w": 1920, "h": 1080}


class FakeInput:
    """Tasten kommen nur über inject() (couchctl pad …). Zusätzliche Namen für Tests:
    ps (kurz), ps-long, ps-options [down = halb | press = voll | up = abgebrochen],
    pad-disconnect, pad-connect, spot-<l1|r1|up|down|left|right> (Create gehalten + Sprungtaste
    im Zeiger-Modus: merkt FAKE_SPOT und meldet on_spot, E99)."""

    def __init__(self, config: Any = None, gs: Any = None):
        self.route = "home"
        self.on_button: Callable[[str, bool], None] | None = None
        self.on_ps: Callable[[str, float], None] | None = None
        self.on_pads_changed: Callable[[], None] | None = None
        self.started = False
        self.held: set[str] = set()
        self.routes: list[tuple] = []
        self.mouse_profile = "mouse"
        self.mouse_layout = ""
        self.mouse_keys: dict = {}          # E109: Tastenbelegung aus set_route(keys=…)
        self.mouse_spots: dict = {}         # eigene Sprungstellen wie InputService (E99)
        self.spot_calls: list[dict] = []
        self.on_spot: Callable[[str, dict], None] | None = None
        self.exclusive = False
        self.rumbles: list[str] = []
        self.typed: list[str] = []
        self.keys: list[str] = []
        self._pads = [{"id": "fake0", "name": "Prüf-Pad", "battery": 80, "charging": False}]
        self._last_input = time.monotonic()

    def start(self) -> None:
        if os.environ.get("COUCH_FAKE_INPUT_FAIL") == "1":
            raise RuntimeError("Eingabe-Attrappe: Start schlägt auf Wunsch fehl")
        self.started = True

    def set_exclusive(self, on: bool) -> None:
        self.exclusive = bool(on)

    def set_route(self, route: str, display: str | None = None, profile: str = "mouse", look: float = 1.0,
                  layout: str = "", keys: dict | None = None) -> None:
        self.route = route
        self.mouse_profile = profile
        self.mouse_look = look
        self.mouse_layout = layout
        self.mouse_keys = dict(keys or {})  # E109: Tastenbelegung des Spiels (pad_keys); {} = Belegung von E64
        self.mouse_spots = {}               # gehören zum Spiel: die Shell setzt sie danach mit set_spots
        self.routes.append((route, display))

    def set_spots(self, spots: dict | None) -> None:
        self.mouse_spots = dict(spots or {})
        self.spot_calls.append(dict(self.mouse_spots))

    def inject(self, button: str, phase: str = "press") -> None:
        self._last_input = time.monotonic()
        if button == "ps":
            if phase in ("press", "up") and self.on_ps:
                self.on_ps("short", 0.0)
        elif button == "ps-long":
            if self.on_ps:
                self.on_ps("long", 0.0)
        elif button == "ps-options":
            if self.on_ps:
                if phase == "up":
                    self.on_ps("options-cancel", 0.0)
                else:
                    self.on_ps("options", 0.5)
                    if phase == "press":
                        self.on_ps("options-done", 1.0)     # wie der echte Eingabedienst
        elif button == "pad-disconnect":
            self._pads = []
            self.held.clear()
            if self.on_pads_changed:
                self.on_pads_changed()
        elif button.startswith("spot-") and button[5:] in SPOT_NAMES:
            # wie InputService: nur im Zeiger-Modus, gilt sofort für alle Pads, die Shell speichert
            if phase != "up" and self.route == "mouse" and self.mouse_profile == "cards":
                name = button[5:]
                self.mouse_spots = {**self.mouse_spots, name: dict(FAKE_SPOT)}
                if self.on_spot:
                    self.on_spot(name, dict(FAKE_SPOT))
        elif button == "pad-connect":
            self._pads = [{"id": "fake0", "name": "Prüf-Pad", "battery": 80, "charging": False}]
            if self.on_pads_changed:
                self.on_pads_changed()
        else:
            # wie InputService: Im OptiScaler-Menü (E118) meldet das Loslassen von Options on_button("options")
            opti_leave = button == "options" and self.route == "mouse" and self.mouse_profile == "opti"
            if phase == "up":
                self.held.discard(button)
                if opti_leave and self.on_button:
                    self.on_button("options", False)
                return
            if phase == "down":
                self.held.add(button)
            if self.route in ("home", "overlay") and self.on_button:
                self.on_button(button, False)
            elif opti_leave and phase == "press" and self.on_button:
                self.on_button("options", False)

    def all_neutral(self) -> bool:
        return not self.held

    def pads(self) -> list[dict]:
        return [dict(p) for p in self._pads]

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_input

    def rumble(self, kind: str) -> None:
        self.rumbles.append(kind)

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def typing_busy(self) -> bool:
        return False

    def press_key(self, name: str) -> None:
        from ..input.xtest import keycode_for
        keycode_for(name)               # wie InputService.press_key: unbekannter Name ist ein Fehler
        self.keys.append(name)


def make(config: Any, session_env: dict | None, scheduler: Any = None, use_x: bool = False) -> tuple:
    """-> (gs, focus, procs, input) wie die echten Dienste."""
    gs = FakeGamescope(use_x=use_x)
    return gs, FakeFocus(gs, config), FakeProcs(gs, session_env, scheduler), FakeInput(config, gs)
