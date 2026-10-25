"""InputService: Pads lesen, in Tasten der Oberfläche übersetzen, PS-Taste, Maus-Modus, Tippen.

Vertrag: interfaces.InputService. Alles läuft in der GLib-Hauptschleife, ohne Thread: Dateinummern
der Pads und des Hotplug-Monitors hängen als Quellen in der Schleife; ein Takt von 60 Hz läuft nur,
solange etwas Zeitabhängiges ansteht (Wiederholung einer Richtung, gehaltene PS-Taste, ausgelenkter
Stick im Maus-Modus, Nachfassen beim Öffnen). Gegriffen (EVIOCGRAB) wird nur, solange Home oder
Overlay über einer laufenden App liegen (set_exclusive, E56).

Ziel der Tasten (route):
    home, overlay   on_button(taste, wiederholung); die Shell reicht an die richtige Seite
    app             nichts an on_button (ein Spiel ist vorn und liest das Pad selbst)
    mouse           padmouse: das Pad als Maus/Tastatur per XTEST; Dreieck meldet on_button("triangle", False),
                    im OptiScaler-Menü (profile "opti", E118) Options on_button("options", False)
PS-Ereignisse (on_ps) kommen in jeder Route. Ausnahme Zeiger-Modus (profile "cards"): Create allein
("create-solo", Bildschirmfoto E83) kommt erst beim Loslassen von Create und gar nicht, wenn
Create gehalten eine Sprungstelle gemerkt hat (E99, on_spot).

Einstellungen (Bauplan E22), alle sofort wirksam:
    input.rumble            Vibration bei der Bedienung der Oberfläche ("stop" gilt immer)
    input.lightbar          Farbe der Leuchtleiste ({"color", "brightness"} oder "#rrggbb")
    input.idle_off_minutes  0 = nie (Vorgabe). Sonst meldet der Dienst ein Bluetooth-Pad, an dem
                            so lange nichts gedrückt wurde, über on_idle_off({id, name, slot});
                            die Verdrahtung trennt es dann (run_write, bluez.cmd_disconnect).
                            Nie, solange eine App vorn ist (route "app"/"mouse"): Ein Spiel
                            liest das Pad selbst, und in einer Zwischensequenz drückt niemand.
    input.ps_long_ms, input.ps_quit_ms, input.mouse_speed

Prüfmodus (COUCH_TEST=1 oder COUCH_TEST_ROOT gesetzt): Es werden KEINE echten Geräte geöffnet,
außer mit COUCH_REAL_PADS=1. Pads kommen dann nur über inject() oder das Attrappen-Backend
(fakepad.FakeBackend, auch über die FIFO in COUCH_FAKE_PAD_FIFO).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from . import ds_codes as c
from . import dualsense_fx as fx
from .padmouse import Config as MouseConfig
from .padlogic import CREATE_SOLO
from .padmouse import PadMouse
from .padreader import Pad, PadEvent, PadManager

log = logging.getLogger("couch.input")

ROUTES = ("home", "overlay", "app", "mouse")
TOUCH_POINTER_PROFILES = ("mouse", "game", "cards", "opti")    # hier darf das Touchpad den Zeiger bewegen
TICK_MS = 16                    # 60 Hz
BATTERY_FIRST_MS = 3000         # der Treiber meldet nach dem Verbinden zunächst 100 % / Unknown
BATTERY_EVERY_MS = 45000        # Akkustand ändert sich ohne Ereignis: nachsehen
EMPTY_READS_LIMIT = 200         # so oft darf ein Knoten "lesbar" sein, ohne etwas zu liefern
IDLE_CHECK_MS = 30000           # Leerlauf-Abschaltung: so oft nachsehen (nur wenn eingeschaltet)
IDLE_MAX_MINUTES = 24 * 60

_SYN = (c.EV_SYN, c.SYN_REPORT, 0)
# Tasten der Prüfsteuerung, die am Pad Achsen sind: name -> (achse, gedrückt, los)
_INJECT_AXES = {
    "up": (c.DPAD_Y, -1, 0), "down": (c.DPAD_Y, 1, 0), "left": (c.DPAD_X, -1, 0), "right": (c.DPAD_X, 1, 0),
    "l2": (c.L2_ANALOG, 255, 0), "r2": (c.R2_ANALOG, 255, 0),
    "ls-up": (c.LEFT_Y, 0, 128), "ls-down": (c.LEFT_Y, 255, 128),
    "ls-left": (c.LEFT_X, 0, 128), "ls-right": (c.LEFT_X, 255, 128),
    "rs-up": (c.RIGHT_Y, 0, 128), "rs-down": (c.RIGHT_Y, 255, 128),
    "rs-left": (c.RIGHT_X, 0, 128), "rs-right": (c.RIGHT_X, 255, 128),
}


def in_test_mode() -> bool:
    from .. import paths                # die eine Definition des Prüfmodus
    return paths.test_mode()


def _usb_dualsense(dev: Any) -> bool:
    """DualSense oder DualSense Edge am USB-Kabel (BUS_USB = 3; Bluetooth ist 5)."""
    return getattr(dev, "bus", 0) == 0x03 and c.is_dualsense(getattr(dev, "vendor", 0), getattr(dev, "product", 0))


class GLibLoop:
    """Dünne Hülle um die GLib-Hauptschleife; in Unit-Tests durch eine Attrappe ersetzbar."""

    def __init__(self):
        import gi
        from gi.repository import GLib
        self._glib = GLib
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            self._fd_add = GLibUnix.fd_add_full          # Nachfolger von GLib.unix_fd_add_full
        except (ImportError, ValueError):
            self._fd_add = GLib.unix_fd_add_full
        self._cond = GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR

    def watch_fd(self, fd: int, callback: Callable[[], None]) -> int:
        def run(*_args) -> bool:
            # Eine Ausnahme darf die Quelle nicht beenden: Das Pad wäre danach stumm
            try:
                callback()
            except Exception:
                log.exception("Eingabe: Fehler beim Lesen von Dateinummer %d", fd)
            return True
        return self._fd_add(self._glib.PRIORITY_DEFAULT, fd, self._cond, run)

    def timeout(self, ms: int, callback: Callable[[], bool]) -> int:
        """callback liefert True, solange der Zeitgeber weiterlaufen soll."""
        def run() -> bool:
            try:
                return bool(callback())
            except Exception:
                log.exception("Eingabe: Fehler in einem Zeitgeber (%d ms)", ms)
                return False
        return self._glib.timeout_add(ms, run)

    def remove(self, source_id: int) -> None:
        self._glib.source_remove(source_id)


class _NoBackend:
    """Vor start(): keine Geräte."""
    def enumerate(self): return []
    def open(self, path): return None
    def hotplug_fileno(self): return None
    def poll_hotplug(self): return []


class _InjectPad:
    """Eingang der Prüfsteuerung: ein Pad ohne Gerät. Es durchläuft dieselbe Strecke wie ein echtes."""
    path, name, uniq = "inject", "Prüfsteuerung", "inject"
    vendor, product = c.VENDOR_SONY, c.PRODUCT_DUALSENSE
    def fileno(self): return None
    def read(self): return []
    def snapshot(self): return set(), {}
    def absinfo(self): return dict(c.DUALSENSE_RANGES)
    def close(self): pass


class InputService:
    def __init__(self, config: Any, gs: Any, *, backend: Any = None, loop: Any = None,
                 clock: Callable[[], float] = time.monotonic,
                 sink_factory: Callable[[str], Any] | None = None,
                 typer_factory: Callable[..., Any] | None = None):
        """config: Einstellungen (get/on_change), gs: Gamescope nach interfaces.py oder None.

        backend, loop, clock und die Fabriken sind für Tests; im Betrieb bleiben sie leer.
        """
        self.route = "home"
        self._mouse_profile = "mouse"
        self._mouse_look = 1.0                  # Spiel-Modus: Tempo des Umschauens (E64)
        self._mouse_layout = ""                 # Zeiger-Modus: Sprungstellen des Spiels (E97)
        self._mouse_spots: dict = {}            # Zeiger-Modus: eigene Sprungstellen {name: {x, y, w, h}} (E99)
        self._mouse_keys: dict = {}             # Spiel-Modus: Tastenbelegung des Spiels {taste: ziel} (E109)
        self._create_held: set[str] = set()     # Pads, deren „create-solo“ aufs Loslassen von Create wartet
        self.on_button: Callable[[str, bool], None] | None = None
        self.on_ps: Callable[[str, float], None] | None = None
        self.on_pads_changed: Callable[[], None] | None = None
        self.on_type_error: Callable[[str], None] | None = None     # Zusatz: Tippen gescheitert (deutscher Satz)
        # Zusatz: ein Bluetooth-Pad war input.idle_off_minutes lang unbenutzt ({id, name, slot});
        # die Verdrahtung trennt es. Ohne Rückruf passiert nichts.
        self.on_idle_off: Callable[[dict], None] | None = None
        # Zusatz: ein DualSense steckt per USB ({id, name, hidraw}); die Verdrahtung trägt diesen
        # PC als sein Bluetooth-Gegenüber ein (E78)
        self.on_cable_pad: Callable[[dict], None] | None = None
        # Zusatz: Zeiger-Modus, Create gehalten + Sprungtaste hat die Zeigerstelle gemerkt
        # (name, {x, y, w, h}); die Shell speichert sie je Spiel (Option pad_spots, E99)
        self.on_spot: Callable[[str, dict], None] | None = None
        self._config, self._gs = config, gs
        self._given_backend, self._loop, self._clock = backend, loop, clock
        self._sink_factory, self._typer_factory = sink_factory, typer_factory
        self.backend: Any = _NoBackend()
        self._mgr = PadManager(self.backend, self._on_pad_event, ps_long=self._ms("input.ps_long_ms", 800),
                               ps_quit=self._ms("input.ps_quit_ms", 3000))
        self._inject: Pad = self._mgr.attach(_InjectPad(), clock(), virtual=True)
        self._mgr.last_input = clock()          # der Leerlauf zählt ab jetzt, nicht ab dem Systemstart
        self._empty_reads: dict[str, int] = {}
        self._started = False
        self._hotplug_watch: int | None = None
        self._control_watch: int | None = None
        self._watches: dict[tuple[int, str], int] = {}
        self._tick_id: int | None = None
        self._in_tick = False
        self._battery: dict[str, tuple[int, str]] = {}          # nach Pfad
        self._battery_timer: int | None = None
        self._battery_first: int | None = None
        self._mice: dict[str, PadMouse] = {}                    # nach Pfad
        self._sink: Any = None
        self._sink_display: str | None = None
        self._typer: Any = None
        self._typer_display: str | None = None
        self._typer_watch: int | None = None
        self._typer_timer: int | None = None
        self._idle_timer: int | None = None
        self._idle_sent: set[str] = set()       # Pads, die für diesen Leerlauf schon gemeldet sind

    # --- Einstellungen -----------------------------------------------------------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self._config.get(key, default) if self._config is not None else default
        except Exception:
            value = default
        return default if value is None else value

    def _ms(self, key: str, default: int) -> float:
        try:
            return max(0.05, float(self._cfg(key, default)) / 1000.0)
        except (TypeError, ValueError):
            return default / 1000.0

    def _on_config(self, key: str, _value: Any = None) -> None:
        if key in ("input.ps_long_ms", "input.ps_quit_ms"):
            self._mgr.set_timing(self._ms("input.ps_long_ms", 800), self._ms("input.ps_quit_ms", 3000))
        elif key == "input.lightbar":
            for pad in self._mgr.real_pads():
                self._apply_lightbar(pad)
        elif key == "input.idle_off_minutes":
            self._idle_sent.clear()
            self._update_idle_timer()

    # --- Leerlauf-Abschaltung ----------------------------------------------------------
    def _idle_minutes(self) -> float:
        """Einstellung input.idle_off_minutes; alles Unbrauchbare heißt 0 = nie."""
        raw = self._cfg("input.idle_off_minutes", 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        return float(raw) if 0 < raw <= IDLE_MAX_MINUTES else 0.0

    def _update_idle_timer(self) -> None:
        """Der Zeitgeber läuft nur, wenn die Abschaltung eingeschaltet ist und ein Pad da ist."""
        if self._loop is None:
            return
        want = self._started and self._idle_minutes() > 0 and bool(self._mgr.real_pads())
        if want and self._idle_timer is None:
            self._idle_timer = self._loop.timeout(IDLE_CHECK_MS, self._idle_check)
        elif not want and self._idle_timer is not None:
            self._loop.remove(self._idle_timer)
            self._idle_timer = None

    def _idle_check(self) -> bool:
        minutes = self._idle_minutes()
        pads = self._mgr.real_pads()
        if minutes <= 0 or not pads:
            self._idle_timer = None
            return False
        idle = self.idle_seconds()
        if idle < minutes * 60:
            self._idle_sent.clear()             # es wurde wieder gedrückt: neu zählen
            return True
        if self.route not in ("home", "overlay"):
            return True                         # eine App ist vorn: nie trennen (siehe Modulkopf)
        for pad in pads:
            # nur Bluetooth: Ein Pad am Kabel lässt sich nicht trennen und braucht keinen Akku
            if getattr(pad.dev, "bus", 0) != c.BUS_BLUETOOTH or pad.ident in self._idle_sent:
                continue
            self._idle_sent.add(pad.ident)
            log.info("Pad %s seit %.0f min unbenutzt: wird zum Abschalten gemeldet", pad.ident, idle / 60)
            self._call(self.on_idle_off, {"id": pad.ident, "name": pad.dev.name, "slot": pad.slot})
        return True

    # --- Start und Ende ----------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._loop is None:
            self._loop = GLibLoop()
        try:
            self.backend = self._given_backend if self._given_backend is not None else self._make_backend()
        except Exception:
            self._started = False
            raise
        self._mgr.backend = self.backend
        fd = self.backend.hotplug_fileno()
        if fd is not None:
            self._hotplug_watch = self._loop.watch_fd(fd, self._on_hotplug)
        control = getattr(self.backend, "control_fileno", None)
        if control and control() is not None:
            self._control_watch = self._loop.watch_fd(control(), self._on_control)
        on_change = getattr(self._config, "on_change", None)
        if on_change:
            try:
                on_change("input.", self._on_config)
            except Exception:
                log.exception("Eingabe: Einstellungen lassen sich nicht beobachten")
        self._mgr.last_input = self._clock()
        self._mgr.scan(self._clock())
        self._after()
        self._update_idle_timer()
        log.info("Eingabe läuft: %s, %d Pad(s)", type(self.backend).__name__, len(self._mgr.real_pads()))

    def _make_backend(self) -> Any:
        if in_test_mode() and os.environ.get("COUCH_REAL_PADS") != "1":
            from .fakepad import FakeBackend
            return FakeBackend(os.environ.get("COUCH_FAKE_PAD_FIFO") or None)
        # Fehlt evdev/pyudev (Systemupdate), soll start() scheitern: Ein Home ohne Eingabe wäre
        # eine Sackgasse; ohne Bereit-Signal greift die Rückfallebene.
        from .padreader import EvdevBackend
        return EvdevBackend()

    def stop(self) -> None:
        """Alles loslassen und schließen (Sitzungsende, Tests)."""
        self._leave_mouse()
        loop = self._loop
        ids = [self._hotplug_watch, self._control_watch, self._tick_id, self._battery_timer, self._battery_first,
               self._typer_watch, self._typer_timer, self._idle_timer, *self._watches.values()]
        for source in ids:
            if source is not None and loop is not None:
                loop.remove(source)
        self._hotplug_watch = self._control_watch = self._tick_id = None
        self._battery_timer = self._battery_first = self._typer_watch = self._typer_timer = None
        self._idle_timer = None
        self._idle_sent.clear()
        self._watches.clear()
        for thing in (self._sink, self._typer):
            if thing is not None:
                try:
                    thing.close()
                except Exception:
                    pass
        self._sink = self._typer = self._sink_display = self._typer_display = None
        self._mgr.close()
        close = getattr(self.backend, "close", None)
        if close:
            close()
        # Zustand wie frisch gebaut: inject() geht weiter, start() darf erneut laufen
        self.backend = self._mgr.backend = _NoBackend()
        self._inject = self._mgr.attach(_InjectPad(), self._clock(), virtual=True)
        self._battery.clear()
        self._started = False

    # --- Schleife ----------------------------------------------------------------------
    def poll(self, now: float | None = None) -> None:
        """Einmal von Hand: Hotplug, Lesen, Takt. Für Tests ohne Hauptschleife und für Attrappen."""
        now = self._clock() if now is None else now
        self._mgr.on_hotplug(now, read_between=getattr(self.backend, "manual_read", False))
        self._mgr.read_all(now)
        self._mgr.tick(now)
        for mouse in list(self._mice.values()):
            self._mouse_call(mouse.tick, now)
        self._after()

    def _on_hotplug(self) -> None:
        now = self._clock()
        manual = getattr(self.backend, "manual_read", False)
        self._mgr.on_hotplug(now, read_between=manual)
        if manual:
            self._mgr.read_all(now)
        self._after()

    def _on_control(self) -> None:
        self.backend.on_control_readable()
        self._on_hotplug()

    def _on_fd(self, path: str) -> None:
        if self._mgr.on_readable(path, self._clock()):
            self._empty_reads.pop(path, None)
        elif path in self._mgr.pads:
            # "lesbar", aber nichts zu lesen: Bleibt das so, würde die Schleife leer drehen
            count = self._empty_reads[path] = self._empty_reads.get(path, 0) + 1
            if count >= EMPTY_READS_LIMIT:
                log.error("Pad %s meldet sich lesbar, liefert aber nichts: wird aufgegeben", path)
                self._empty_reads.pop(path, None)
                self._mgr.drop(path)
        self._after()

    def _after(self) -> None:
        """Nach jeder Verarbeitung: Überwachung, Takt und Meldungen an den Stand anpassen."""
        self._rewatch()
        self._update_tick()

    def _rewatch(self) -> None:
        if self._loop is None:
            return
        wanted = {(fd, path) for fd, path in self._mgr.fds().items()}
        for key in [k for k in self._watches if k not in wanted]:
            self._loop.remove(self._watches.pop(key))
        for key in wanted:
            if key not in self._watches:
                self._watches[key] = self._loop.watch_fd(key[0], lambda path=key[1]: self._on_fd(path))

    def _needs_tick(self) -> bool:
        return self._mgr.needs_tick() or any(m.wants_tick for m in self._mice.values())

    def _update_tick(self) -> None:
        if self._loop is None or self._in_tick:
            return
        need = self._needs_tick()
        if need and self._tick_id is None:
            for mouse in self._mice.values():
                mouse.reset_clock()
            self._tick_id = self._loop.timeout(TICK_MS, self._tick)
        elif not need and self._tick_id is not None:
            self._loop.remove(self._tick_id)
            self._tick_id = None

    def _tick(self) -> bool:
        now = self._clock()
        self._in_tick = True
        try:
            self._mgr.tick(now)
            for mouse in list(self._mice.values()):
                self._mouse_call(mouse.tick, now)
            self._rewatch()
            if self._needs_tick():
                return True
        except Exception:
            # Der Takt endet; das nächste Ereignis eines Pads meldet ihn neu an
            log.exception("Eingabe: Fehler im Takt")
        finally:
            self._in_tick = False
        self._tick_id = None        # kein Leerlauf-Takt: Die Quelle endet, bis wieder etwas ansteht
        return False

    def _call(self, fn: Callable | None, *args: Any) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            log.exception("Eingabe: Rückruf %s%r gescheitert", getattr(fn, "__name__", fn), args)

    # --- Ereignisse der Pads -----------------------------------------------------------
    def _on_pad_event(self, ev: PadEvent) -> None:
        if ev.kind == "press":
            if self.route in ("home", "overlay"):
                self._call(self.on_button, ev.name, ev.repeat)
        elif ev.kind == "ps":
            if ev.name == CREATE_SOLO and self._hold_create(ev.path):
                return
            self._call(self.on_ps, ev.name, float(ev.value))
        elif ev.kind == "connected":
            pad = self._mgr.pads.get(ev.path)
            if pad is not None:
                self._connected(pad)
        elif ev.kind == "disconnected":
            self._battery.pop(ev.path, None)
            self._empty_reads.pop(ev.path, None)
            self._idle_sent.discard(ev.pad)
            self._create_held.discard(ev.path)
            mouse = self._mice.pop(ev.path, None)
            if mouse is not None:
                self._mouse_call(mouse.set_enabled, False)
            log.info("Pad getrennt: %s", ev.pad)
            if not self._mgr.real_pads() and self._battery_timer is not None and self._loop is not None:
                self._loop.remove(self._battery_timer)
                self._battery_timer = None
            self._update_idle_timer()
            self._call(self.on_pads_changed)

    def _connected(self, pad: Pad) -> None:
        log.info("Pad verbunden: %s (%s), Platz %d", pad.dev.name, pad.ident, pad.slot)
        self._read_battery(pad)
        self._apply_lightbar(pad)
        self._apply_player(pad)
        if self.route == "mouse" and self._sink is not None:
            self._add_mouse(pad)
        if self._loop is not None:
            if self._battery_timer is None:
                self._battery_timer = self._loop.timeout(BATTERY_EVERY_MS, self._battery_poll)
            if self._battery_first is None:
                self._battery_first = self._loop.timeout(BATTERY_FIRST_MS, self._battery_once)
        self._update_idle_timer()
        self._call(self.on_pads_changed)        # sofort: die Shell kennt das Pad, bevor seine Tasten kommen
        if self.on_cable_pad is not None and _usb_dualsense(pad.dev):
            hidraw = getattr(pad.dev, "hidraw", None)
            self._call(self.on_cable_pad, {"id": pad.ident, "name": pad.dev.name,
                                           "hidraw": hidraw() if callable(hidraw) else None})

    # --- Akku, Lichtleiste, Vibration --------------------------------------------------
    def _read_battery(self, pad: Pad) -> bool:
        """Liest den Akku eines Pads; True, wenn sich Stand oder Ladezustand geändert haben."""
        reader = getattr(pad.dev, "battery", None)
        try:
            value = reader() if reader else None
        except Exception as exc:
            log.debug("Akku von %s nicht lesbar: %s", pad.ident, exc)
            value = None
        old = self._battery.get(pad.path)
        if value is None:
            self._battery.pop(pad.path, None)
        else:
            self._battery[pad.path] = value
        return value != old

    def _battery_poll(self) -> bool:
        changed = False
        for pad in self._mgr.real_pads():
            changed = self._read_battery(pad) or changed
        if changed:
            self._call(self.on_pads_changed)
        return True

    def _battery_once(self) -> bool:
        self._battery_first = None
        self._battery_poll()
        return False

    def _lightbar_color(self) -> tuple[int, int, int] | None:
        """Farbe aus der Einstellung input.lightbar; None = nicht anfassen (Farbe des Treibers).

        Zwei Formen: "#rrggbb" oder die des Bereichs „Zubehör“ ({"color": Name, "brightness": %},
        auch True/False); Namen und Helligkeit rechnet dessen lightbar_rgb() um.
        """
        raw = self._cfg("input.lightbar", "")
        if raw == "":
            return None
        if isinstance(raw, str):
            return fx.parse_color(raw)
        try:
            from ..settings.accessories import lightbar_rgb
            r, g, b = lightbar_rgb(raw)
            return int(r), int(g), int(b)
        except Exception as exc:
            log.debug("Lichtleiste: Einstellung %r nicht auswertbar: %s", raw, exc)
            return None

    def _apply_lightbar(self, pad: Pad) -> None:
        color = self._lightbar_color()
        setter = getattr(pad.dev, "set_lightbar", None)
        if color is None or setter is None:
            return
        try:
            if not setter(*color):
                log.info("Lichtleiste von %s nicht schreibbar", pad.ident)
        except Exception as exc:
            log.info("Lichtleiste von %s: %s", pad.ident, exc)

    def _apply_player(self, pad: Pad) -> None:
        """Spieler-LEDs auf unsere Platznummer; ohne Schreibrecht in sysfs passiert nichts."""
        setter = getattr(pad.dev, "set_player", None)
        if setter is None:
            return
        try:
            setter(pad.slot)
        except Exception as exc:
            log.debug("Spieler-LEDs von %s: %s", pad.ident, exc)

    def rumble(self, kind: str) -> None:
        """Kurze Rückmeldung am Pad: "tick", "confirm", "error", "boundary", "home"; "stop" = Motoren aus.

        "stop" gilt immer (vor dem Anhalten eines Spiels), der Rest nur mit Einstellung input.rumble.
        Scheitert still.
        """
        try:
            pads = self._mgr.real_pads()
            if kind == "stop":
                for pad in pads:
                    stop = getattr(pad.dev, "rumble_stop", None)
                    if stop:
                        stop()
                return
            if not self._cfg("input.rumble", True):
                return
            pattern = fx.FEEDBACK.get(kind)
            if pattern is None:
                log.debug("Vibration: unbekanntes Muster %r", kind)
                return
            # nur das Pad in der Hand: das, an dem zuletzt gedrückt wurde
            active = [p for p in pads if p.ident == self._mgr.last_active] or pads
            for pad in active:
                pulse = getattr(pad.dev, "rumble", None)
                if pulse:
                    pulse(*pattern)
        except Exception as exc:
            log.info("Vibration %r gescheitert: %s", kind, exc)

    # --- Vertrag -----------------------------------------------------------------------
    @property
    def mouse_profile(self) -> str:
        return self._mouse_profile

    @property
    def mouse_look(self) -> float:
        return self._mouse_look

    @property
    def mouse_layout(self) -> str:
        return self._mouse_layout

    @property
    def mouse_spots(self) -> dict:
        return dict(self._mouse_spots)

    @property
    def mouse_keys(self) -> dict:
        """Spiel-Modus: die geltende Tastenbelegung des Spiels (E109, Kopie); {} = Belegung von E64."""
        return dict(self._mouse_keys)

    def set_spots(self, spots: dict | None) -> None:
        """Zeiger-Modus: eigene Sprungstellen des Spiels vorn ({name: {x, y, w, h}}, Option pad_spots,
        E99), über den eingebauten (layout). Wirkt sofort auf alle Pads, ohne die Maus neu
        aufzubauen: ein gehaltener Klick bleibt gehalten. set_route setzt sie zurück."""
        self._mouse_spots = dict(spots or {})
        for mouse in list(self._mice.values()):
            self._mouse_call(mouse.set_spots, self._mouse_spots)

    def set_exclusive(self, on: bool) -> None:
        """Pads exklusiv lesen (PadManager.set_exclusive): Home/Control Center über einer laufenden
        App, damit die App im Hintergrund nicht mitbedient wird."""
        self._mgr.set_exclusive(on)

    def set_route(self, route: str, display: str | None = None, profile: str = "mouse", look: float = 1.0,
                  layout: str = "", keys: dict | None = None) -> None:
        """profile (nur route "mouse"): "mouse" = Pad als Maus und Tastatur, "keys" = nur
        Pfeiltasten, Enter, Escape, Rücktaste (Apps mit Fernseh-Oberfläche, padmouse.KEYS_TV),
        "game" = Tastatur und Maus für Spiele ohne Controller (padmouse.KEYS_GAME, E64; look =
        Tempo des Umschauens; keys = Tastenbelegung des Spiels {taste: ziel}, Option pad_keys, E109,
        None bzw. {} = E64), "term" = Terminal „Claude Code“ (padmouse.KEYS_TERM, E72), "cards" =
        Zeiger für Spiele, die man nur mit der Maus spielt (E97; layout = Sprungstellen,
        padmouse.LAYOUTS), "opti" = Menü von OptiScaler (padmouse.KEYS_OPTI, E118)."""
        if route not in ROUTES:
            raise ValueError(f"unbekanntes Ziel: {route!r}")
        keys = dict(keys) if isinstance(keys, dict) and profile == "game" else {}
        if route == self.route and (route != "mouse" or (display in (None, self._sink_display)
                                                          and profile == self._mouse_profile
                                                          and look == self._mouse_look
                                                          and layout == self._mouse_layout
                                                          and keys == self._mouse_keys)):
            return
        self._leave_mouse()             # lässt jede von padmouse gehaltene Taste los
        self._mouse_profile = profile
        self._mouse_look = look
        self._mouse_layout = layout
        self._mouse_keys = keys
        self._mouse_spots = {}          # gehören zum Spiel: die Shell setzt sie danach mit set_spots
        self.route = route
        # Durchrutsch-Schutz: Was jetzt gedrückt ist, gilt im neuen Ziel erst nach dem Loslassen
        self._mgr.set_buttons(route in ("home", "overlay"))
        # Touchpad als Maus nur in den Zeiger-Modi; sonst liest es nur couch (Klick) bzw. das Spiel
        # über hidraw (2026-10-24: im Sony-Profil bewegte es zugleich den Zeiger)
        self._mgr.set_touch_block(not (route == "mouse" and profile in TOUCH_POINTER_PROFILES))
        if route == "mouse":
            self._enter_mouse(display)
        self._update_tick()

    def inject(self, button: str, phase: str = "press") -> None:
        """Gemeinsamer Eingang für die Prüfsteuerung: Tasten wie vom Pad.

        button: up down left right cross circle square triangle l1 r1 l2 r2 l3 r3 options create
        touchpad ps, dazu die Sticks als ls-up … ls-right und rs-up … rs-right.
        phase: "down", "up" oder "press" (antippen = beides).
        Kurzformen: "ps" + press = kurzer Druck; "ps-long" + press = langer Druck sofort;
        "ps-options" + press = Kombination sofort bis zum Ende. Mit down/up laufen "ps-long" und
        "ps-options" in echter Zeit (wie am Pad gehalten).
        """
        if phase not in ("press", "down", "up"):
            raise ValueError(f"unbekannte Phase: {phase!r}")
        if button in ("ps-long", "ps-options"):
            if phase == "press":
                if button == "ps-long":
                    self._call(self.on_ps, "long", 0.0)
                else:
                    self._call(self.on_ps, "options", 0.5)
                    self._call(self.on_ps, "options-done", 1.0)
                return
            names = ["ps"] if button == "ps-long" else ["ps", "options"]
        else:
            names = [button]
        now = self._clock()
        for down in ((True, False) if phase == "press" else (phase == "down",)):
            for name in names:
                self._mgr.feed(self._inject, self._raw(name, down), now)
        self._after()

    @staticmethod
    def _raw(button: str, down: bool) -> list[tuple[int, int, int]]:
        if button in _INJECT_AXES:
            axis, on, off = _INJECT_AXES[button]
            return [(c.EV_ABS, axis, on if down else off), _SYN]
        code = c.BUTTON_CODES.get(button)
        if code is None:
            raise ValueError(f"unbekannte Taste: {button!r}")
        return [(c.EV_KEY, code, 1 if down else 0), _SYN]

    def all_neutral(self) -> bool:
        """Nichts gedrückt, nichts ausgelenkt, an keinem Pad. Erst dann darf ein Spiel auftauen."""
        return self._mgr.all_neutral()

    def pads(self) -> list[dict]:
        out = []
        for pad in self._mgr.real_pads():
            dev = pad.dev
            battery = self._battery.get(pad.path)
            out.append({
                "id": pad.ident,
                "name": dev.name,
                "battery": battery[0] if battery else None,
                "charging": bool(battery and battery[1] in ("Charging", "Full")),
                "type": c.pad_type(getattr(dev, "vendor", 0), getattr(dev, "product", 0)),
                "slot": pad.slot,
            })
        return out

    def idle_seconds(self) -> float:
        """Sekunden seit der letzten Eingabe; 0, solange etwas gehalten wird."""
        if not self._mgr.all_neutral():
            return 0.0
        return max(0.0, self._clock() - self._mgr.last_input)

    # --- Maus-Modus --------------------------------------------------------------------
    def _enter_mouse(self, display: str | None) -> None:
        if display is None and self._gs is not None:
            try:
                display = self._gs.game_display()
            except Exception as exc:
                log.warning("Maus-Modus: Display der Spiele unbekannt: %s", exc)
        if not display:
            log.warning("Maus-Modus ohne Gamescope-Display: bleibt aus")
            return
        if self._sink is None or self._sink_display != display:
            self._close_sink()
            try:
                if self._sink_factory is not None:
                    self._sink = self._sink_factory(display)
                else:
                    from .padmouse import XTestSink
                    self._sink = XTestSink(display)     # verweigert alles außer Gamescope-Xwayland
            except Exception as exc:
                log.error("Maus-Modus auf %s nicht möglich: %s", display, exc)
                self._sink = None
                return
            self._sink_display = display
        for pad in self._mgr.pads.values():
            self._add_mouse(pad)
        self._mgr.on_raw = self._on_raw

    def _mouse_config(self) -> MouseConfig:
        """Zeigertempo: Einstellung input.mouse_speed als Faktor (am Fernseher abzustimmen)."""
        try:
            factor = min(4.0, max(0.25, float(self._cfg("input.mouse_speed", 1.0))))
        except (TypeError, ValueError):
            factor = 1.0
        base = MouseConfig()
        return MouseConfig(pointer_min=base.pointer_min * factor, pointer_max=base.pointer_max * factor)

    def _add_mouse(self, pad: Pad) -> PadMouse:
        mouse = PadMouse(self._sink, self._mouse_config(), self._osk, state=pad.state,
                         profile=getattr(self, "_mouse_profile", "mouse"), look=getattr(self, "_mouse_look", 1.0),
                         layout=getattr(self, "_mouse_layout", ""), spots=self._mouse_spots,
                         on_spot=self._spot_remembered, keys=getattr(self, "_mouse_keys", None),
                         on_leave=self._opti_leave)
        # Was schon gehalten wird, löst nichts aus; solange PS gehalten wird, gehört das Pad der Shell
        self._mouse_call(mouse.set_enabled, c.PS not in pad.state.buttons)
        self._mice[pad.path] = mouse
        return mouse

    def _leave_mouse(self) -> None:
        self._mgr.on_raw = None
        self._create_held.clear()       # ein zurückgehaltenes Create-Foto verfällt mit dem Maus-Modus
        for mouse in self._mice.values():
            self._mouse_call(mouse.set_enabled, False)
        self._mice.clear()

    def _close_sink(self) -> None:
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
        self._sink = self._sink_display = None

    def _osk(self) -> None:
        self._call(self.on_button, "triangle", False)

    def _opti_leave(self) -> None:
        self._call(self.on_button, "options", False)

    def _on_raw(self, pad: Pad, etype: int, code: int, _value: int, change: tuple | None) -> None:
        mouse = self._mice.get(pad.path)
        if mouse is None:
            return
        if change and change[1] == c.PS:
            if change[2]:
                self._create_held.discard(pad.path)     # Create, dann PS: das ist PS + Create (eigenes Foto)
            self._mouse_call(mouse.set_enabled, not change[2])
            return
        self._mouse_call(mouse.handle, etype, code, change)
        if change and change[1] == c.CREATE and not change[2] and pad.path in self._create_held:
            self._create_held.discard(pad.path)
            if not mouse.spot_taken:            # Create nur gedrückt: das zurückgehaltene Foto
                self._call(self.on_ps, CREATE_SOLO, 0.0)

    def _hold_create(self, path: str) -> bool:
        """Zeiger-Modus (E99): Create allein kann der Anfang von „Create halten + Sprungtaste“ sein.
        Das Bildschirmfoto (create-solo, E83) wartet deshalb aufs Loslassen und entfällt, wenn
        dabei eine Stelle gemerkt wurde. True = zurückgehalten."""
        mouse = self._mice.get(path)
        if mouse is None or mouse.profile != "cards" or not mouse.enabled:
            return False
        self._create_held.add(path)
        return True

    def _spot_remembered(self, name: str, spot: dict) -> None:
        """Ein Pad hat eine Sprungstelle gemerkt: für alle Pads gültig, die Shell speichert sie."""
        self._mouse_spots = {**self._mouse_spots, name: dict(spot)}
        for mouse in list(self._mice.values()):
            mouse.set_spots(self._mouse_spots)
        self._call(self.on_spot, name, dict(spot))

    def _mouse_call(self, fn: Callable, *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:
            # X-Verbindung weg: Maus-Modus beenden statt bei jedem Ereignis erneut zu scheitern
            log.error("Maus-Modus abgebrochen: %r", exc)
            self._mgr.on_raw = None
            self._mice.clear()
            self._create_held.clear()   # ohne Maus kommt das Loslassen von Create hier nie mehr an (E99)
            self._close_sink()

    # --- Tippen ------------------------------------------------------------------------
    def type_text(self, text: str) -> None:
        """Text per XTEST in die vordere App (Xwayland der Spiele). Fehler: Protokoll und on_type_error."""
        if not text:
            return
        typer = self._get_typer()
        if typer is not None:
            typer.type_text(text)
            self._pump_typer()

    def typing_busy(self) -> bool:
        """Noch Tasten per XTEST unterwegs (Reihenfolge mit dem direkten Weg, oskdirect)."""
        return bool(self._typer is not None and getattr(self._typer, "busy", False))

    def press_key(self, name: str) -> None:
        """Einzelne Taste: Return, BackSpace, Escape, Tab, Up, Down, Left, Right, Delete, Home, End …"""
        from .xtest import keycode_for
        keycode_for(name)               # unbekannter Name ist ein Programmierfehler: ValueError
        typer = self._get_typer()
        if typer is not None:
            typer.press_key(name)
            self._pump_typer()

    def _type_error(self, message: str) -> None:
        self._call(self.on_type_error, message)

    def _get_typer(self) -> Any:
        display = None
        if self._gs is not None:
            try:
                display = self._gs.game_display()
            except Exception as exc:
                log.warning("Tippen: Display der Spiele unbekannt: %s", exc)
        if not display:
            log.warning("Tippen nicht möglich: kein Gamescope-Display")
            self._type_error("Tippen ist hier nicht möglich.")
            return None
        if self._typer is not None and (self._typer_display != display or getattr(self._typer, "broken", False)):
            self._drop_typer()
        if self._typer is None:
            try:
                if self._typer_factory is not None:
                    self._typer = self._typer_factory(display, on_error=self._type_error)
                else:
                    from .xtest import XTestTyper
                    self._typer = XTestTyper(display, on_error=self._type_error)
            except Exception as exc:
                log.error("Tippen auf %s nicht möglich: %s", display, exc)
                self._type_error("Tippen ist hier nicht möglich.")
                self._typer = None
                return None
            self._typer_display = display
            if self._loop is not None:
                self._typer_watch = self._loop.watch_fd(self._typer.fileno(), self._pump_typer)
        return self._typer

    def _drop_typer(self) -> None:
        for attr in ("_typer_watch", "_typer_timer"):
            source = getattr(self, attr)
            if source is not None and self._loop is not None:
                self._loop.remove(source)
            setattr(self, attr, None)
        if self._typer is not None:
            try:
                self._typer.close()
            except Exception:
                pass
        self._typer = self._typer_display = None

    def _pump_typer(self) -> None:
        if self._typer is None:
            return
        if self._typer_timer is not None and self._loop is not None:
            self._loop.remove(self._typer_timer)
            self._typer_timer = None
        wait = self._typer.pump()
        if getattr(self._typer, "broken", False):
            self._drop_typer()
        elif wait is not None and self._loop is not None:
            self._typer_timer = self._loop.timeout(max(1, int(wait * 1000)), self._typer_due)

    def _typer_due(self) -> bool:
        self._typer_timer = None
        self._pump_typer()
        return False
