"""Attrappen-Backend: Pads, die es nur im Speicher gibt.

Im Prüfmodus (COUCH_TEST=1) öffnet der Eingabedienst keine echten Geräte; sonst würde der
Controller, mit dem gerade jemand spielt, die Tests steuern. Stattdessen arbeitet er mit diesem
Backend. Es legt KEIN Gerät im Kernel an (kein uinput): Ein FakePad ist nur ein Objekt, das
dieselben rohen Ereignisse liefert wie ein evdev-Knoten.

Im selben Prozess:
    backend = FakeBackend()
    pad = backend.add("p1")                 # anstecken
    pad.press(ds_codes.CROSS); pad.press(ds_codes.CROSS, 0)
    backend.remove("p1")                    # abziehen
Aus einem anderen Prozess über eine FIFO (Umgebungsvariable COUCH_FAKE_PAD_FIFO), eine
Anweisung je Zeile:
    add p1 [dualsense|xbox] [name=Mein Pad]
    key p1 304 1            EV_KEY: Code, 1 = gedrückt, 0 = los
    abs p1 0 32767          EV_ABS: Code, Wert
    tap p1 304              drücken und loslassen (zwei Berichte)
    battery p1 45 Discharging
    gone p1                 Knoten verschwindet beim Lesen (wie ENODEV), ohne udev-Meldung
    remove p1
Jede key/abs-Zeile ist ein eigener Bericht (mit SYN_REPORT).

Achsbereiche: Ohne Angabe wie ein Xbox-Pad (Sticks -32768..32767, Ruhe 0; Auslöser 0..255);
mit "dualsense" wie das echte Pad (Sticks 0..255, Ruhe 128). So ist beides abgedeckt.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
from typing import Callable

from . import ds_codes as c
from .padreader import DeviceGone

log = logging.getLogger("couch.input")

LAYOUTS = {
    "xbox": {c.ABS_X: (-32768, 32767), c.ABS_Y: (-32768, 32767), c.ABS_RX: (-32768, 32767), c.ABS_RY: (-32768, 32767),
             c.ABS_Z: (0, 255), c.ABS_RZ: (0, 255), c.ABS_HAT0X: (-1, 1), c.ABS_HAT0Y: (-1, 1)},
    "dualsense": dict(c.DUALSENSE_RANGES),
}
STICK_AXES = (c.ABS_X, c.ABS_Y, c.ABS_RX, c.ABS_RY)


class FakePad:
    """Ein Pad im Speicher (Protokoll PadDevice aus padreader)."""

    def __init__(self, ident: str, layout: str = "xbox", name: str | None = None,
                 battery: tuple[int, str] | None = None, on_change: Callable[[], None] | None = None):
        self.ident = ident
        self.path = f"fake:{ident}"
        self.uniq = ident
        self.layout = layout
        self.name = name or ("DualSense Wireless Controller" if layout == "dualsense" else "Attrappen-Pad")
        # Die Tasten heißen wie beim DualSense (Lage): Die Attrappe gibt sich als Sony-Pad aus
        self.vendor, self.product = c.VENDOR_SONY, c.PRODUCT_DUALSENSE
        self._ranges = dict(LAYOUTS[layout])
        self.keys: set[int] = set()
        self.axes: dict[int, int] = {code: ((lo + hi + 1) // 2 if code in STICK_AXES else 0)
                                     for code, (lo, hi) in self._ranges.items()}
        self.queue: list[tuple[int, int, int]] = []
        # Stand, den der Leser kennt (ausgelieferte Ereignisse). snapshot() liefert DIESEN Stand:
        # Sonst gälte eine Taste, die gleich nach "add" gedrückt wird, als "war beim Öffnen schon
        # gehalten" und würde nie gemeldet.
        self._seen_keys: set[int] = set()
        self._seen_axes: dict[int, int] = dict(self.axes)
        self.gone = False
        self.closed = False
        self.battery_state = battery
        self.rumbles: list[tuple] = []          # was der Dienst an Vibration verlangt hat
        self.lightbar: tuple[int, int, int] | None = None
        self.player: int | None = None
        self._on_change = on_change

    # --- PadDevice ---------------------------------------------------------------------
    def fileno(self) -> None:
        return None                              # kein Dateideskriptor: das Backend weckt den Dienst

    def read(self) -> list[tuple[int, int, int]]:
        if self.gone:
            raise DeviceGone(self.path)
        out, self.queue = self.queue, []
        for etype, code, value in out:
            if etype == c.EV_KEY:
                (self._seen_keys.add if value else self._seen_keys.discard)(code)
            elif etype == c.EV_ABS:
                self._seen_axes[code] = value
        return out

    def snapshot(self) -> tuple[set[int], dict[int, int]]:
        return set(self._seen_keys), dict(self._seen_axes)

    def absinfo(self) -> dict[int, tuple[int, int]]:
        return dict(self._ranges)

    def close(self) -> None:
        self.closed = True

    def battery(self) -> tuple[int, str] | None:
        return self.battery_state

    def rumble(self, strong: float, weak: float, ms: int) -> None:
        self.rumbles.append((strong, weak, ms))

    def rumble_stop(self) -> None:
        self.rumbles.append(("stop",))

    def set_lightbar(self, r: int, g: int, b: int) -> bool:
        self.lightbar = (r, g, b)
        return True

    def set_player(self, number: int) -> bool:
        self.player = number
        return True

    # --- Steuerung durch den Test ------------------------------------------------------
    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def press(self, code: int, down: int = 1) -> None:
        (self.keys.add if down else self.keys.discard)(code)
        self.queue += [(c.EV_KEY, code, 1 if down else 0), (c.EV_SYN, c.SYN_REPORT, 0)]
        self._changed()

    def tap(self, code: int) -> None:
        self.press(code, 1)
        self.press(code, 0)

    def axis(self, code: int, value: int) -> None:
        self.axes[code] = value
        self.queue += [(c.EV_ABS, code, value), (c.EV_SYN, c.SYN_REPORT, 0)]
        self._changed()

    def stick(self, x: float, y: float, right: bool = False) -> None:
        """Stick auf einen Vektor -1..1 je Achse setzen (in einem Bericht)."""
        events = []
        for code, v in zip((c.ABS_RX, c.ABS_RY) if right else (c.ABS_X, c.ABS_Y), (x, y)):
            lo, hi = self._ranges[code]
            center = (lo + hi + 1) // 2
            raw = max(lo, min(hi, int(round(center + v * ((hi - center) if v >= 0 else (center - lo))))))
            self.axes[code] = raw
            events.append((c.EV_ABS, code, raw))
        self.queue += events + [(c.EV_SYN, c.SYN_REPORT, 0)]
        self._changed()

    def trigger(self, axis: int, amount: float) -> None:
        """Auslöser (ABS_Z = L2, ABS_RZ = R2) auf 0..1 setzen."""
        lo, hi = self._ranges[axis]
        self.axis(axis, int(round(lo + max(0.0, min(1.0, amount)) * (hi - lo))))


class FakeBackend:
    """Backend-Attrappe (Protokoll Backend aus padreader), wahlweise mit FIFO für andere Prozesse."""

    manual_read = True      # die Pads haben keine Dateinummer: nach jedem Wecken alle lesen

    def __init__(self, fifo_path: str | None = None):
        self.devices: dict[str, FakePad] = {}       # nach Pfad ("fake:<id>")
        self.denied: dict[str, int] = {}            # pfad -> so oft scheitert open() noch (wie EACCES)
        self._hotplug: list[tuple[str, str]] = []
        self._wake_r, self._wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self._fifo_fd: int | None = None
        self._fifo_buf = b""
        self.fifo_path = fifo_path
        if fifo_path:
            self._open_fifo(fifo_path)

    # --- Backend -----------------------------------------------------------------------
    def enumerate(self) -> list[str]:
        return sorted(self.devices)

    def open(self, path: str) -> FakePad | None:
        if self.denied.get(path, 0) > 0:
            self.denied[path] -= 1
            return None
        pad = self.devices.get(path)
        if pad is not None:
            pad.closed = False
        return pad

    def hotplug_fileno(self) -> int:
        return self._wake_r

    def poll_hotplug(self) -> list[tuple[str, str]]:
        try:
            while os.read(self._wake_r, 4096):
                pass
        except BlockingIOError:
            pass
        out, self._hotplug = self._hotplug, []
        return out

    # --- Steuerung ---------------------------------------------------------------------
    def _wake(self) -> None:
        try:
            os.write(self._wake_w, b"x")
        except BlockingIOError:
            pass                                 # Röhre voll: geweckt wird ohnehin

    def add(self, ident: str, layout: str = "xbox", name: str | None = None,
            battery: tuple[int, str] | None = None) -> FakePad:
        """Steckt ein Pad an. Derselbe Name nach remove() = dasselbe Pad kommt wieder."""
        if f"fake:{ident}" in self.devices:
            return self.devices[f"fake:{ident}"]     # steckt schon
        pad = FakePad(ident, layout, name, battery, on_change=self._wake)
        self.devices[pad.path] = pad
        self._hotplug.append(("add", pad.path))
        self._wake()
        return pad

    def remove(self, ident: str) -> None:
        path = f"fake:{ident}"
        if self.devices.pop(path, None) is not None:
            self._hotplug.append(("remove", path))
            self._wake()

    def pad(self, ident: str) -> FakePad | None:
        return self.devices.get(f"fake:{ident}")

    def command(self, line: str) -> None:
        """Eine Textzeile ausführen (Format siehe Modulkopf). Fehler werden protokolliert."""
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            return
        try:
            verb, ident, args = parts[0], parts[1], parts[2:]
            if verb == "add":
                layout = next((a for a in args if a in LAYOUTS), "xbox")
                name = next((line.split("name=", 1)[1].strip() for a in args if a.startswith("name=")), None)
                self.add(ident, layout, name)
                return
            if verb == "remove":
                self.remove(ident)
                return
            pad = self.pad(ident)
            if pad is None:
                raise KeyError(f"kein Pad {ident!r}")
            if verb == "key":
                pad.press(int(args[0], 0), int(args[1], 0))
            elif verb == "abs":
                pad.axis(int(args[0], 0), int(args[1], 0))
            elif verb == "tap":
                pad.tap(int(args[0], 0))
            elif verb == "battery":
                pad.battery_state = (int(args[0]), args[1] if len(args) > 1 else "Discharging")
                self._wake()
            elif verb == "gone":
                pad.gone = True
                self._wake()
            else:
                raise ValueError(f"unbekannte Anweisung {verb!r}")
        except (IndexError, ValueError, KeyError) as exc:
            log.warning("fakepad: Zeile %r nicht verstanden: %s", line.strip(), exc)

    # --- FIFO --------------------------------------------------------------------------
    def _open_fifo(self, path: str) -> None:
        try:
            try:
                os.mkfifo(path, 0o600)
            except FileExistsError:
                pass
            if not stat.S_ISFIFO(os.stat(path).st_mode):
                raise OSError(errno.EINVAL, "keine FIFO", path)
            # Lesen UND Schreiben öffnen: Sonst meldet die FIFO Dateiende, sobald der erste
            # Schreiber schließt, und die Hauptschleife dreht leer.
            self._fifo_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            log.warning("fakepad: FIFO %s nicht nutzbar: %s", path, exc)
            self._fifo_fd = None

    def control_fileno(self) -> int | None:
        return self._fifo_fd

    def on_control_readable(self) -> None:
        """Zeilen aus der FIFO ausführen."""
        if self._fifo_fd is None:
            return
        try:
            while True:
                chunk = os.read(self._fifo_fd, 4096)
                if not chunk:
                    break
                self._fifo_buf += chunk
        except BlockingIOError:
            pass
        except OSError as exc:
            log.warning("fakepad: FIFO-Lesefehler: %s", exc)
            return
        *lines, self._fifo_buf = self._fifo_buf.split(b"\n")
        for raw in lines:
            self.command(raw.decode("utf-8", "replace"))

    def close(self) -> None:
        for fd in (self._wake_r, self._wake_w, self._fifo_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fifo_fd = None
