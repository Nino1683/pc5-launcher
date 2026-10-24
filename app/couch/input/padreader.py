"""Gamepad-Leser: hotplug-fest, mehrere Pads; greift nur auf Wunsch (set_exclusive, E56).

Aufbau:
  Backend      spricht mit dem System (EvdevBackend: python-evdev + pyudev). In Tests und im
               Prüfmodus eine Attrappe (fakepad.FakeBackend).
  PadManager   hält je Pad Zustand und Logik (padlogic.PadLogic) und meldet Ereignisse an
               einen Rückruf. Kennt weder GLib noch die Oberfläche; die Zeit kommt als Parameter.

Die Einbindung in die GLib-Schleife macht service.InputService.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import glob
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import ds_codes as c
from .padlogic import PadLogic, PadState

log = logging.getLogger("couch.input")


class DeviceGone(Exception):
    """Der Event-Knoten ist weg (Pad schläft, Akku leer, Kabel gezogen)."""


class NotAPad(Exception):
    """Der Knoten ist kein Gamepad für uns (anderes Gerät oder Steams virtuelles Pad): nie wieder versuchen."""


class PadDevice(Protocol):
    """Ein geöffnetes Gamepad. Wahlweise zusätzlich: vendor, product, absinfo(), filenos(),
    battery(), rumble(strong, weak, ms), rumble_stop(), set_lightbar(r, g, b), set_player(n)."""
    path: str        # /dev/input/eventN, ändert sich bei jeder Neuverbindung
    name: str
    uniq: str        # MAC des Pads, bleibt gleich: daran erkennen wir es wieder
    def fileno(self) -> int | None: ...
    def read(self) -> list[tuple[int, int, int]]: ...            # (typ, code, wert); DeviceGone
    def snapshot(self) -> tuple[set[int], dict[int, int]]: ...   # gedrückte Tasten, Achswerte
    def close(self) -> None: ...


class Backend(Protocol):
    def enumerate(self) -> list[str]: ...                        # Pfade aller Gamepad-Knoten
    def open(self, path: str) -> PadDevice | None: ...           # None: (noch) nicht zu öffnen; NotAPad
    def hotplug_fileno(self) -> int | None: ...
    def poll_hotplug(self) -> list[tuple[str, str]]: ...         # ("add"|"remove", pfad)


@dataclass
class PadEvent:
    kind: str            # connected, disconnected, button, press, ps
    pad: str             # uniq (MAC) oder Pfad
    code: int = 0        # button: evdev-Code
    value: float = 0     # button: 1/0; ps: Fortschritt 0..1
    slot: int = 0        # Spielernummer ab 1, stabil solange das Pad verbunden ist
    name: str = ""       # press: Taste für die Oberfläche; ps: short, long, options, options-done, options-cancel
    repeat: bool = False  # press: Wiederholung einer gehaltenen Richtung
    path: str = ""       # Knoten des Pads (wechselt bei jeder Neuverbindung)


@dataclass
class Pad:
    dev: PadDevice
    slot: int
    state: PadState = field(default_factory=PadState)
    logic: PadLogic = None   # type: ignore[assignment]
    dropped: bool = False
    virtual: bool = False    # Eingang der Prüfsteuerung: zählt nicht als angeschlossenes Pad
    active: bool = False     # beim letzten Bericht war etwas gedrückt oder ausgelenkt
    grabbed: bool = False    # exklusiv gelesen (EVIOCGRAB), siehe PadManager.set_exclusive

    @property
    def ident(self) -> str:
        return self.dev.uniq or self.dev.path

    @property
    def path(self) -> str:
        return self.dev.path


class PadManager:
    # udev meldet "add", bevor logind die Zugriffsrechte (uaccess-ACL) gesetzt hat:
    # das erste open() scheitert dann mit EACCES. Deshalb mehrfach nachfassen.
    OPEN_RETRY_EVERY = 0.15
    OPEN_RETRY_FOR = 3.0

    def __init__(self, backend: Backend, on_event: Callable[[PadEvent], None],
                 on_raw: Callable[[Pad, int, int, int, tuple | None], None] | None = None,
                 ps_long: float = 0.8, ps_quit: float = 3.0):
        self.backend = backend
        self.on_event = on_event
        self.on_raw = on_raw                            # jedes rohe Ereignis (für den Maus-Modus)
        self.pads: dict[str, Pad] = {}                  # nach Pfad
        self._pending: dict[str, tuple[float, float]] = {}   # pfad -> (nächster Versuch, Schluss)
        self._seen_not_pad: set[str] = set()            # nur einmal je Knoten ins Protokoll
        self._exclusive = False                         # Pads exklusiv lesen (Home/Control Center über einer App)
        self._touch_block = True                        # Touchpad nicht als Maus für Gamescope (set_touch_block)
        self.last_active: str | None = None             # Pad, das zuletzt eine Taste gedrückt hat
        self.last_input: float = 0.0                    # Zeit der letzten Eingabe irgendeines Pads
        self._ps_long, self._ps_quit = ps_long, ps_quit
        self._buttons = True

    # --- Geräte kommen und gehen -------------------------------------------------------
    def scan(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for path in self.backend.enumerate():
            self._add(path, now)

    def _add(self, path: str, now: float) -> None:
        if path in self.pads:
            return
        try:
            dev = self.backend.open(path)
        except NotAPad:
            if path not in self._seen_not_pad:
                self._seen_not_pad.add(path)
                log.info("Eingabe: %s ist kein Pad, übergangen", path)
            self._pending.pop(path, None)
            return
        if dev is None:
            first = self._pending.get(path, (0.0, now + self.OPEN_RETRY_FOR))
            self._pending[path] = (now + self.OPEN_RETRY_EVERY, first[1])
            return
        self._pending.pop(path, None)
        self.attach(dev, now)

    def attach(self, dev: PadDevice, now: float, virtual: bool = False) -> Pad:
        """Nimmt ein offenes Gerät auf. virtual: Eingang der Prüfsteuerung (ohne "connected")."""
        used = {p.slot for p in self.pads.values()}
        slot = 0 if virtual else next(n for n in range(1, len(used) + 2) if n not in used)
        pad = Pad(dev=dev, slot=slot, virtual=virtual)
        absinfo = getattr(dev, "absinfo", None)
        if absinfo:
            pad.state.configure(absinfo())
        # Stand beim Öffnen übernehmen, aber nicht als Tastendruck melden: wer das Pad mit
        # gedrückter PS-Taste aufweckt, soll damit nicht gleich etwas auslösen.
        keys, axes = dev.snapshot()
        pad.state.resync(keys, axes)
        pad.logic = PadLogic(pad.state, positional=c.is_positional(getattr(dev, "vendor", c.VENDOR_SONY)),
                             ps_long=self._ps_long, ps_quit=self._ps_quit)
        pad.logic.enabled = self._buttons
        pad.logic.prime(now)
        self.pads[dev.path] = pad
        if not virtual:
            self.last_input = now
            log.info("Eingabe: Pad verbunden: %s (%s, Platz %d)", getattr(dev, "name", "?"), dev.path, slot)
            self._emit(pad, "connected")
            if self._exclusive:
                self._grab(pad)
            self._block_touch(pad)
        return pad

    def _remove(self, path: str) -> None:
        self._pending.pop(path, None)
        pad = self.pads.pop(path, None)
        if pad is None:
            return
        try:
            pad.dev.close()
        except OSError:
            pass
        # Was beim Abreißen gedrückt war, sauber loslassen, sonst gilt es als weiter gehalten
        for code in sorted(pad.state.buttons):
            self._emit(pad, "button", code, 0)
        self._emit(pad, "disconnected")
        if not pad.virtual:
            log.info("Eingabe: Pad getrennt: %s", path)
        if self.last_active == pad.ident:
            self.last_active = None

    def on_hotplug(self, now: float | None = None, read_between: bool = False) -> None:
        """Meldungen des Backends abarbeiten. read_between (Attrappen ohne Dateinummer): vor dem
        Abziehen noch lesen, was das Pad geliefert hat, und nach dem Anstecken gleich lesen."""
        now = time.monotonic() if now is None else now
        for action, path in self.backend.poll_hotplug():
            if action == "add":
                self._add(path, now)
            elif action == "remove":
                if read_between:
                    self.on_readable(path, now)
                self._remove(path)
            if read_between:
                self.on_readable(path, now)

    def real_pads(self) -> list[Pad]:
        return sorted((p for p in self.pads.values() if not p.virtual), key=lambda p: p.slot)

    def fds(self) -> dict[int, str]:
        """Dateinummer -> Pfad des Pads, für die Überwachung in der Hauptschleife."""
        out = {}
        for path, pad in self.pads.items():
            filenos = getattr(pad.dev, "filenos", None)
            for fd in (filenos() if filenos else [pad.dev.fileno()]):
                if fd is not None and fd >= 0:
                    out[fd] = path
        return out

    def close(self) -> None:
        for pad in list(self.pads.values()):
            try:
                pad.dev.close()
            except OSError:
                pass
        self.pads.clear()
        self._pending.clear()

    # --- Einstellungen -----------------------------------------------------------------
    def set_timing(self, ps_long: float, ps_quit: float) -> None:
        self._ps_long, self._ps_quit = ps_long, ps_quit
        for pad in self.pads.values():
            pad.logic.ps.long_after, pad.logic.ps.quit_after = ps_long, ps_quit

    def set_buttons(self, on: bool) -> None:
        """Bekommt die Oberfläche Tasten? Jeder Aufruf gilt als Wechsel des Ziels (hold_off)."""
        self._buttons = on
        for pad in self.pads.values():
            pad.logic.set_enabled(on)

    # --- Ereignisse --------------------------------------------------------------------
    def on_readable(self, path: str, now: float | None = None) -> int:
        """Liest ein Pad. Liefert die Zahl der Ereignisse (0 auch, wenn das Pad dabei verschwand)."""
        pad = self.pads.get(path)
        if pad is None:
            return 0
        return self._read(pad, time.monotonic() if now is None else now)

    def read_all(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for pad in list(self.pads.values()):
            self._read(pad, now)

    def drop(self, path: str) -> None:
        """Pad aufgeben, als wäre es abgezogen worden (Knoten verhält sich kaputt)."""
        self._remove(path)

    def _read(self, pad: Pad, now: float) -> int:
        try:
            events = pad.dev.read()
        except DeviceGone:
            self._remove(pad.path)   # nicht auf udev warten: der fd wäre sonst dauernd "lesbar"
            return 0
        except Exception:
            log.exception("Pad %s: unerwarteter Fehler beim Lesen, wird getrennt", pad.path)
            self._remove(pad.path)
            return 0
        self.feed(pad, events, now)
        if self._exclusive != pad.grabbed:
            self._grab(pad)
        return len(events)

    def feed(self, pad: Pad, events: list[tuple[int, int, int]], now: float) -> None:
        """Verarbeitet rohe Ereignisse eines Pads. Auch der Weg der Prüfsteuerung."""
        dirty = False
        for etype, code, value in events:
            if etype == c.EV_SYN:
                if code == c.SYN_DROPPED:
                    pad.dropped = True           # Puffer übergelaufen: Rest bis SYN_REPORT ist unvollständig
                elif code == c.SYN_REPORT:
                    if pad.dropped:
                        pad.dropped = False
                        keys, axes = pad.dev.snapshot()
                        for change in pad.state.resync(keys, axes):
                            self._button(pad, change)
                            if self.on_raw:
                                self.on_raw(pad, c.EV_KEY, change[1], change[2], change)
                        if self.on_raw:      # Achsen können sich ebenfalls geändert haben
                            for axis, axis_value in axes.items():
                                self.on_raw(pad, c.EV_ABS, axis, axis_value, None)
                        self.last_input = now
                    if self.on_raw:
                        self.on_raw(pad, etype, code, value, None)
                    # nach JEDEM Bericht auswerten: ein Tippen (drücken und loslassen) kann in
                    # einem einzigen read() stecken
                    self._evaluate(pad, now)
                    dirty = False
                continue
            if pad.dropped:
                continue
            change = pad.state.feed(etype, code, value)
            if change:
                self._button(pad, change)
                self.last_input = now
            if self.on_raw:
                self.on_raw(pad, etype, code, value, change)
            dirty = True
        if dirty:
            self._evaluate(pad, now)

    def _button(self, pad: Pad, change: tuple[str, int, int]) -> None:
        if change[2]:
            self.last_active = pad.ident
        self._emit(pad, "button", change[1], change[2])

    def _evaluate(self, pad: Pad, now: float) -> None:
        for kind, name, value in pad.logic.update(now):
            if kind == "press":
                self.last_active = pad.ident
                self.on_event(PadEvent("press", pad.ident, slot=pad.slot, name=name, repeat=bool(value), path=pad.path))
            else:
                self.on_event(PadEvent("ps", pad.ident, code=c.PS, value=value, slot=pad.slot, name=name, path=pad.path))
        # auch das Loslassen ist eine Eingabe: Der Leerlauf zählt ab dem Moment, in dem alles ruht
        active = not pad.state.is_neutral()
        if active or pad.active:
            self.last_input = now
        pad.active = active

    def tick(self, now: float) -> None:
        """Zeitabhängiges: Wiederholung, gehaltene PS-Taste, erneute Öffnungsversuche."""
        for path, (due, until) in list(self._pending.items()):
            if now >= until:
                del self._pending[path]
                log.warning("Eingabe: %s ließ sich nicht öffnen (Zugriff), aufgegeben", path)
            elif now >= due:
                self._add(path, now)
        for pad in list(self.pads.values()):
            if pad.logic.needs_tick:
                self._evaluate(pad, now)

    def needs_tick(self) -> bool:
        return bool(self._pending) or any(p.logic.needs_tick for p in self.pads.values())

    def all_neutral(self) -> bool:
        return all(p.state.is_neutral() for p in self.pads.values())

    # --- exklusiv lesen ----------------------------------------------------------------
    def set_exclusive(self, on: bool) -> None:
        """Liegt das Home oder das Control Center über einer laufenden App, liest nur couch das Pad:
        EVIOCGRAB nimmt evdev- und joydev-Lesern (Kodi, Chromium/VacuumTube, SDL ohne hidraw) die
        Ereignisse weg, solange es gilt. hidraw-Leser (Steam, Proton) erreicht es nicht. Gegriffen
        wird erst, wenn das Pad ruht (sonst bliebe in der App eine Taste „gedrückt“), losgelassen sofort.
        Der Kernel löst den Griff mit dem Schließen des Knotens, also auch nach einem Absturz."""
        self._exclusive = bool(on)
        for pad in self.pads.values():
            if not pad.virtual and pad.grabbed != self._exclusive:
                self._grab(pad)

    def set_touch_block(self, on: bool) -> None:
        """Das Touchpad des DualSense ist ein eigenes Eingabegerät, das Gamescope (libinput) als Maus
        nimmt. In einem Spiel mit Controller-Unterstützung bewegte es so zugleich den Zeiger (Meldung
        2026-10-24: „Wenn ein Sony-Profil-Spiel läuft, sind die Touchpad-Tasten auch gleichzeitig die
        Maussteuerung“), im Home klickte es an der unsichtbaren Zeigerstelle. on: couch greift den
        Touchpad-Knoten (EVIOCGRAB auf dem eigenen Klick-Leser); Spiele lesen das Touchpad über hidraw
        weiter. Frei bleibt es nur in den Zeiger-Modi (InputService.set_route)."""
        self._touch_block = bool(on)
        for pad in self.pads.values():
            self._block_touch(pad)

    def _block_touch(self, pad: Pad) -> None:
        fn = getattr(pad.dev, "grab_touch", None)
        if pad.virtual or fn is None:
            return
        try:
            fn(self._touch_block)
        except Exception:
            log.exception("Pad %s: Touchpad %s", pad.path, "sperren" if self._touch_block else "freigeben")

    def _grab(self, pad: Pad) -> None:
        if pad.virtual:
            return
        want = self._exclusive
        if getattr(pad, "grabbed", None) == want:
            return          # schon so (connected ließ die Shell eben greifen): sonst EBUSY im Protokoll
        if want and not pad.state.is_neutral():
            return                                      # später, beim nächsten Lesen
        fn = getattr(pad.dev, "grab", None)
        if fn is None:
            return
        try:
            ok = fn(want)
        except Exception:
            log.exception("Pad %s: exklusiv lesen %s gescheitert", pad.path, "an" if want else "aus")
            ok = False
        if ok:
            pad.grabbed = want
            log.info("Eingabe: %s %s", pad.path, "exklusiv (Apps im Hintergrund bekommen nichts)" if want else "wieder geteilt")

    def _emit(self, pad: Pad, kind: str, code: int = 0, value: float = 0) -> None:
        self.on_event(PadEvent(kind=kind, pad=pad.ident, code=code, value=value, slot=pad.slot, path=pad.path))


# --- echtes Backend --------------------------------------------------------------------
_EVENT = struct.Struct("llHHi")              # struct input_event (64 Bit)
_EVIOCSMASK = 0x40104593                     # _IOW('E', 0x93, struct input_mask)
_EVIOCGRAB = 0x40044590                      # _IOW('E', 0x90, int)
_KEY_BYTES, _ABS_BYTES, _MSC_BYTES, _REL_BYTES = 96, 8, 1, 2


def _set_event_mask(fd: int, etype: int, codes: tuple[int, ...], size: int) -> None:
    """EVIOCSMASK: Der Kernel liefert diesem Leser nur noch die genannten Codes eines Typs. Die Größe muss
    ein Vielfaches von sizeof(long) sein (bits_from_user), sonst EINVAL: am echten Pad 2026-10-24 gesehen,
    EV_MSC mit 1 und EV_REL mit 2 Byte scheiterten, und der Touchpad-Klick blieb ganz aus."""
    size = -(-size // 8) * 8
    bits = bytearray(size)
    for code in codes:
        bits[code // 8] |= 1 << (code % 8)
    buf = ctypes.create_string_buffer(bytes(bits), size)
    fcntl.ioctl(fd, _EVIOCSMASK, struct.pack("=IIQ", etype, size, ctypes.addressof(buf)))


def read_events(fd: int) -> list[tuple[int, int, int]]:
    """Liest rohe input_event-Sätze von einer nicht blockierenden Dateinummer."""
    try:
        data = os.read(fd, _EVENT.size * 64)
    except BlockingIOError:
        return []
    usable = len(data) - len(data) % _EVENT.size
    return [(etype, code, value) for _, _, etype, code, value in _EVENT.iter_unpack(data[:usable])]


def open_touchpad_click(event_path: str, sys_root: str = "/sys", dev_root: str = "/dev/input") -> int | None:
    """Öffnet den Touchpad-Knoten desselben Pads, aber nur für den Klick (BTN_LEFT).

    Das Touchpad des DualSense ist ein eigenes Eingabegerät. Als Gamepad fassen wir es nicht an;
    der Klick ist aber eine Taste der Oberfläche. Die Ereignismaske sorgt dafür, dass uns der
    Kernel die Fingerbewegungen gar nicht erst schickt. Geht das Öffnen oder die Maske nicht
    (keine Rechte: auf diesem Rechner wahrscheinlich), gibt es eben keinen Touchpad-Klick.
    UNBESTÄTIGT: am echten Pad noch nicht gelaufen.
    """
    try:
        own = os.path.realpath(os.path.join(sys_root, "class/input", os.path.basename(event_path), "device"))
        hid = os.path.realpath(os.path.join(own, "device"))
        for node in sorted(glob.glob(os.path.join(hid, "input", "input*", "event*"))):
            parent = os.path.dirname(node)
            if os.path.realpath(parent) == own:
                continue
            with open(os.path.join(parent, "properties"), encoding="ascii") as f:
                props = int(f.read().strip() or "0", 16)
            if not props & (1 << c.INPUT_PROP_BUTTONPAD):
                continue                      # Bewegungssensor, Klinkenbuchse
            fd = os.open(os.path.join(dev_root, os.path.basename(node)), os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                _set_event_mask(fd, c.EV_ABS, (), _ABS_BYTES)
                _set_event_mask(fd, c.EV_MSC, (), _MSC_BYTES)
                _set_event_mask(fd, c.EV_REL, (), _REL_BYTES)
                _set_event_mask(fd, c.EV_KEY, (c.TOUCHPAD_CLICK,), _KEY_BYTES)
            except OSError:
                os.close(fd)                  # ohne Maske kämen hunderte Ereignisse je Sekunde
                continue
            return fd
    except (OSError, ValueError):
        pass
    return None


def read_ids(event_path: str, sys_root: str = "/sys") -> tuple[int, int]:
    """Hersteller und Produkt eines Knotens aus sysfs, ohne ihn zu öffnen. (0, 0) = unbekannt."""
    base = os.path.join(sys_root, "class/input", os.path.basename(event_path), "device/id")
    try:
        with open(os.path.join(base, "vendor"), encoding="ascii") as f:
            vendor = int(f.read().strip(), 16)
        with open(os.path.join(base, "product"), encoding="ascii") as f:
            product = int(f.read().strip(), 16)
        return vendor, product
    except (OSError, ValueError):
        return 0, 0


class _EvdevPad:
    TOUCH_RETRY_AFTER = 1.0      # der Touchpad-Knoten entsteht kurz nach dem Gamepad-Knoten

    def __init__(self, dev, touch_fd: int | None = None):
        self._dev = dev
        self._touch_fd = touch_fd
        self._touch_retry_at = None if touch_fd is not None else time.monotonic() + self.TOUCH_RETRY_AFTER
        self._touch_want: bool | None = None      # gewünschter Griff des Touchpad-Knotens (grab_touch)
        self._touch_grabbed = False
        self._rumble = None
        self.path, self.name, self.uniq = dev.path, dev.name, dev.uniq or ""
        self.vendor, self.product, self.bus = dev.info.vendor, dev.info.product, dev.info.bustype

    def fileno(self) -> int:
        return self._dev.fd

    def filenos(self) -> list[int]:
        return [self._dev.fd] if self._touch_fd is None else [self._dev.fd, self._touch_fd]

    def read(self) -> list[tuple[int, int, int]]:
        try:
            events = [(e.type, e.code, e.value) for e in self._dev.read()]
        except BlockingIOError:
            events = []
        except OSError as exc:
            # ENODEV ist der Normalfall (Pad schläft ein). Auch jeder andere Lesefehler beendet
            # das Pad: Ein Knoten, der dauernd "lesbar" ist und nur Fehler liefert, ließe die
            # Hauptschleife leer drehen.
            if exc.errno not in (errno.ENODEV, errno.EBADF, errno.EIO):
                log.warning("Pad %s: Lesefehler %s, wird getrennt", self.path, exc)
            raise DeviceGone(self.path) from exc
        if self._touch_retry_at is not None and time.monotonic() >= self._touch_retry_at:
            self._touch_retry_at = None                   # genau ein zweiter Versuch
            self._touch_fd = open_touchpad_click(self.path)
            if self._touch_want is not None:
                self.grab_touch(self._touch_want)
        if self._touch_fd is not None:
            try:
                events += read_events(self._touch_fd)
            except OSError:
                self._close_touch()           # der Gamepad-Knoten meldet das Ende selbst
        return events

    def snapshot(self) -> tuple[set[int], dict[int, int]]:
        try:
            keys = set(self._dev.active_keys())
            axes = {code: info.value for code, info in self._dev.capabilities(absinfo=True).get(c.EV_ABS, [])}
            return keys, axes
        except OSError:
            return set(), {}

    def absinfo(self) -> dict[int, tuple[int, int]]:
        try:
            return {code: (info.min, info.max) for code, info in self._dev.capabilities(absinfo=True).get(c.EV_ABS, [])}
        except OSError:
            return {}

    # --- Zubehör (alles darf still scheitern; der Aufrufer fängt OSError) ---------------
    def _sysfs(self):
        from . import dualsense_fx as fx
        return fx.PadSysfs.from_event_node(self.path)     # jedes Mal neu: die Namen wechseln je Verbindung

    def battery(self) -> tuple[int, str] | None:
        from . import dualsense_fx as fx
        pad = self._sysfs()
        return fx.read_battery(pad) if pad else None

    def hidraw(self) -> str | None:
        pad = self._sysfs()
        return pad.hidraw_node() if pad else None

    def grab_touch(self, on: bool) -> bool:
        """Touchpad-Knoten exklusiv (EVIOCGRAB) auf dem eigenen Klick-Leser: libinput bekommt dann keine
        Bewegung und keinen Klick mehr, couch weiter den Klick (Ereignismaske bleibt). False ohne Knoten."""
        self._touch_want = bool(on)
        if self._touch_fd is None:
            return False
        if self._touch_grabbed == self._touch_want:
            return True
        try:
            fcntl.ioctl(self._touch_fd, _EVIOCGRAB, 1 if on else 0)
        except OSError as e:
            log.warning("Pad %s: Touchpad grab(%s) gescheitert: %s", self.path, on, e)
            return False
        self._touch_grabbed = self._touch_want
        log.info("Eingabe: Touchpad von %s %s", self.path, "gesperrt (keine Maus für Gamescope)" if on else "als Maus frei")
        return True

    def grab(self, on: bool) -> bool:
        """EVIOCGRAB an/aus. False, wenn es nicht ging (schon von anderen gegriffen, Knoten weg)."""
        try:
            if on:
                self._dev.grab()
            else:
                self._dev.ungrab()
            return True
        except OSError as e:
            log.warning("Pad %s: grab(%s) gescheitert: %s", self.path, on, e)
            return False

    def set_lightbar(self, r: int, g: int, b: int) -> bool:
        from . import dualsense_fx as fx
        pad = self._sysfs()
        return bool(pad) and fx.set_lightbar(pad, r, g, b)

    def set_player(self, number: int) -> bool:
        from . import dualsense_fx as fx
        pad = self._sysfs()
        return bool(pad) and fx.set_player_leds(pad, number)

    def rumble(self, strong: float, weak: float, ms: int) -> None:
        from . import dualsense_fx as fx
        if self._rumble is None:
            self._rumble = fx.Rumble(self._dev)
        self._rumble.pulse(strong, weak, ms)

    def rumble_stop(self) -> None:
        from . import dualsense_fx as fx
        if self._rumble is None:
            self._rumble = fx.Rumble(self._dev)
        self._rumble.silence()

    def _close_touch(self) -> None:
        if self._touch_fd is not None:
            try:
                os.close(self._touch_fd)        # löst auch den Griff (Kernel)
            except OSError:
                pass
            self._touch_fd = None
            self._touch_grabbed = False

    def close(self) -> None:
        self._close_touch()
        self._dev.close()


class EvdevBackend:
    """python-evdev für die Knoten, pyudev für Hotplug. Gegriffen (EVIOCGRAB) wird nur über
    PadManager.set_exclusive (_EvdevPad.grab, E56)."""

    def __init__(self):
        import evdev
        import pyudev
        self._evdev = evdev
        self._ctx = pyudev.Context()
        self._mon = pyudev.Monitor.from_netlink(self._ctx)
        self._mon.filter_by("input")
        self._mon.start()

    def _role(self, dev) -> str | None:
        # absinfo=False: Achsen als Nummern. Die Vorgabe von python-evdev (absinfo=True) liefert
        # Paare (Nummer, AbsInfo); dann fand classify nie einen Stick, und kein Pad wurde geöffnet.
        caps = dev.capabilities(absinfo=False)
        try:
            props = set(dev.input_props())
        except OSError:
            props = set()
        return c.classify(dev.name, set(caps.get(c.EV_KEY, [])), set(caps.get(c.EV_ABS, [])), props)

    def enumerate(self) -> list[str]:
        # Über udev statt über /dev/input/event*: ID_INPUT_JOYSTICK filtert Tastaturen und
        # Mäuse heraus, ohne sie zu öffnen (auf diesem Rechner dürfte der angemeldete Benutzer sie öffnen).
        return sorted(d.device_node for d in self._ctx.list_devices(subsystem="input", ID_INPUT_JOYSTICK="1")
                      if d.device_node and d.sys_name.startswith("event"))

    def open(self, path: str):
        if c.is_excluded(*read_ids(path)):
            raise NotAPad(path)              # Steams virtuelles Pad gar nicht erst öffnen
        try:
            dev = self._evdev.InputDevice(path)
        except OSError:
            return None                      # EACCES kurz nach dem Einstecken, ENOENT wenn schon wieder weg
        try:
            if c.is_excluded(dev.info.vendor, dev.info.product) or self._role(dev) != c.GAMEPAD:
                dev.close()
                raise NotAPad(path)
        except OSError:
            dev.close()
            return None
        return _EvdevPad(dev, open_touchpad_click(path))

    def hotplug_fileno(self) -> int:
        return self._mon.fileno()

    def poll_hotplug(self) -> list[tuple[str, str]]:
        out = []
        while True:
            d = self._mon.poll(timeout=0)
            if d is None:
                return out
            if d.device_node and d.sys_name.startswith("event") and d.action in ("add", "remove"):
                # Beim Entfernen sind die Eigenschaften schon weg: alle "remove" durchreichen,
                # der Manager ignoriert Pfade, die er nicht kennt.
                if d.action == "remove" or d.properties.get("ID_INPUT_JOYSTICK") == "1":
                    out.append((d.action, d.device_node))
