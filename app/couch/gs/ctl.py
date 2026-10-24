"""Wayland-Protokoll "gamescope_control", ohne Fremdbibliothek: Bildschirmfoto MIT Overlay.

Warum nicht gamescopectl: `gamescopectl screenshot datei 3` reicht nur ein Argument weiter,
der Typ bleibt 1 (nur Basisebene). Ein Foto, das das Overlay zeigen soll, braucht Typ 3.

    screenshot("/pfad/bild.png")              # Typ 3 = volle Komposition
    pixels("/pfad/bild.png", [(100, 300)])    # -> ["#0000ff"]

Die Anzeige kommt aus dem Argument display, sonst aus $GAMESCOPE_WAYLAND_DISPLAY. Es gibt
bewusst keine Vorgabe "gamescope-0": Im Game-Mode wäre das die echte Sitzung.
Beide Funktionen blockieren (Foto: typisch 0,1 bis 0,3 s); aus der Hauptschleife nur in
Tests aufrufen, sonst über system.background().
"""
from __future__ import annotations

import os
import socket
import struct
import time

from . import GamescopeError

BASE_PLANE, ALL_REAL_LAYERS, FULL_COMPOSITION, SCREEN_BUFFER = 1, 2, 3, 4
_PNG_END = b"IEND\xaeB`\x82"

FEATURES = {0: "done", 1: "reshade_shaders", 2: "display_info", 3: "pixel_filter",
            4: "refresh_cycle_only_change_refresh_rate", 5: "mura_correction", 6: "look",
            7: "perf_query", 8: "keyboard_layout", 9: "sgsr_filter"}


class _Wayland:
    """Gerade so viel Wayland, wie für ein Objekt und ein paar Nachrichten nötig ist."""

    def __init__(self, name: str):
        if name.startswith("/"):
            path = name
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
            path = os.path.join(runtime, name)
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.s.connect(path)
        except OSError as e:
            self.s.close()
            raise GamescopeError("Gamescope-Socket %s nicht erreichbar: %s" % (path, e)) from e
        self.buf = b""
        self.next_id = 2

    def close(self) -> None:
        self.s.close()

    def new_id(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    @staticmethod
    def string(text: str) -> bytes:
        raw = text.encode() + b"\0"
        return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)

    def send(self, obj: int, opcode: int, payload: bytes = b"") -> None:
        self.s.sendall(struct.pack("<IHH", obj, opcode, 8 + len(payload)) + payload)

    def events(self, timeout: float):
        """Liefert (objekt, opcode, daten) für alles, was innerhalb von timeout ankommt."""
        end = time.monotonic() + timeout
        while True:
            while len(self.buf) >= 8:
                obj, opcode, size = struct.unpack("<IHH", self.buf[:8])
                if size < 8 or len(self.buf) < size:
                    break
                data, self.buf = self.buf[8:size], self.buf[size:]
                yield obj, opcode, data
            left = end - time.monotonic()
            if left <= 0:
                return
            self.s.settimeout(left)
            try:
                chunk = self.s.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            except OSError:
                return
            if not chunk:
                return
            self.buf += chunk

    def sync(self, timeout: float = 2.0, on_event=None) -> None:
        done = self.new_id()
        self.send(1, 0, struct.pack("<I", done))            # wl_display.sync
        for obj, opcode, data in self.events(timeout):
            if obj == done and opcode == 0:
                return
            if on_event:
                on_event(obj, opcode, data)


def _read_string(data: bytes, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", data, off)
    text = data[off + 4:off + 4 + n - 1].decode(errors="replace") if n else ""
    return text, off + 4 + n + (-n % 4)


def _display(display: str | None) -> str:
    name = display or os.environ.get("GAMESCOPE_WAYLAND_DISPLAY") or ""
    if not name:
        raise GamescopeError("GAMESCOPE_WAYLAND_DISPLAY ist nicht gesetzt.")
    return name


def _bind(display: str | None) -> tuple[_Wayland, int]:
    wl = _Wayland(_display(display))
    registry = wl.new_id()
    wl.send(1, 1, struct.pack("<I", registry))              # wl_display.get_registry
    found = []

    def on_global(obj, opcode, data):
        if obj == registry and opcode == 0 and not found:   # wl_registry.global
            (name,) = struct.unpack_from("<I", data, 0)
            iface, off = _read_string(data, 4)
            (version,) = struct.unpack_from("<I", data, off)
            if iface == "gamescope_control":
                control = wl.new_id()
                wl.send(registry, 0, struct.pack("<I", name) + _Wayland.string(iface) +
                        struct.pack("<II", min(version, 7), control))
                found.append(control)

    wl.sync(2.0, on_global)
    if not found:
        wl.close()
        raise GamescopeError("gamescope_control fehlt auf %s (kein Gamescope?)" % _display(display))
    return wl, found[0]


def _complete(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < 64:
                return False
            f.seek(-8, os.SEEK_END)
            return f.read() == _PNG_END
    except OSError:
        return False


def screenshot(path: str, kind: int = FULL_COMPOSITION, display: str | None = None,
               timeout: float = 10.0) -> str:
    """Schreibt ein PNG nach path und liefert den Pfad. kind: 1 nur Basisebene, 2 alle
    echten Ebenen, 3 volle Komposition (mit Overlay), 4 Bildschirmpuffer 1:1.
    GamescopeError, wenn kein Bild kommt (Gamescope zeigt dann gerade nichts an)."""
    path = os.path.abspath(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    wl, control = _bind(display)
    try:
        wl.send(control, 2, _Wayland.string(path) + struct.pack("<II", int(kind), 0))   # take_screenshot
        end = time.monotonic() + timeout
        for obj, opcode, _data in wl.events(timeout):
            if obj == control and opcode == 2:              # screenshot_taken
                break
        # Ältere Fassungen melden nichts zurück, und die Meldung kann vor dem letzten
        # Byte kommen: in jedem Fall auf die vollständige Datei warten.
        while True:
            if _complete(path):
                return path
            if time.monotonic() >= end + 1.0:
                break
            time.sleep(0.02)
    finally:
        wl.close()
    raise GamescopeError("Kein Bild von Gamescope (zeigt es gerade nichts an?)")


def info(display: str | None = None) -> dict:
    """Fähigkeiten und Bildschirm laut gamescope_control (nur lesen)."""
    wl, control = _bind(display)
    out: dict = {"features": {}, "displays": []}

    def on_event(obj, opcode, data):
        if obj != control:
            return
        if opcode == 0:                                     # feature_support
            feat, ver, flags = struct.unpack("<III", data[:12])
            out["features"][FEATURES.get(feat, str(feat))] = {"version": ver, "flags": flags}
        elif opcode == 1:                                   # active_display_info
            conn, off = _read_string(data, 0)
            make, off = _read_string(data, off)
            model, off = _read_string(data, off)
            flags, alen = struct.unpack_from("<II", data, off)
            rates = struct.unpack_from("<%dI" % (alen // 4), data, off + 8)
            out["displays"].append({"connector": conn, "make": make, "model": model,
                                    "internal": bool(flags & 1), "hdr": bool(flags & 2),
                                    "vrr": bool(flags & 4), "rates": list(rates)})

    try:
        wl.sync(2.0, on_event)
    finally:
        wl.close()
    return out


def pixels(path: str, points: list[tuple[int, int]]) -> list[str]:
    """Farben einzelner Punkte eines Fotos als "#rrggbb" (Pixelprobe für Tests)."""
    from PIL import Image
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        return ["#%02x%02x%02x" % rgb.getpixel((int(x), int(y))) for x, y in points]
