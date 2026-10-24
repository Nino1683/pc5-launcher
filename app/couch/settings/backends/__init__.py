"""Gemeinsame Leser der Einstellungsbereiche (PipeWire, NetworkManager, BlueZ, ostree, …).

Jeder Leser bekommt run_read hereingereicht oder nimmt reine Texte entgegen, damit er sich mit
erfundenen Ausgaben prüfen lässt. Schreibendes steht nie hier, außer klar gekapselt
(nm.wifi_connect, bluez.GioBluez) und im Prüfmodus abgeschaltet.
"""
from __future__ import annotations

import os
import time

from ... import paths as _paths


def test_mode() -> bool:
    """Prüfmodus nach Bauplan Abschnitt 7: Dann darf nichts den Rechner verändern. Dieselbe
    Definition wie überall im Home (paths.test_mode): COUCH_TEST_ROOT gesetzt ODER COUCH_TEST=1.
    Eine zweite, engere Regel hier ließ Bluetooth, WLAN und Systemupdate scharf, sobald ein
    Lauf nur COUCH_TEST=1 setzte."""
    return _paths.test_mode()


def log_write(paths, text: str) -> None:
    """Zeile nach <state>/writes.log, für Schreibwege, die nicht durch run_write gehen können."""
    try:
        os.makedirs(paths.state_dir, exist_ok=True)
        with open(os.path.join(paths.state_dir, "writes.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def silent_helper(path: str) -> bool:
    """Ist das ein Helfer, der nie nach einem Kennwort fragen kann? Bazzites polkit-Helfer rufen
    sich selbst über pkexec auf; zulässig ist das nur mit --disable-internal-agent (sonst könnte
    pkexec auf einem Terminal nachfragen). Fehlt die Datei oder die Option: nicht benutzen."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(8192)
    except OSError:
        return False
    return "pkexec" not in text or "--disable-internal-agent" in text


class TtlCache:
    """Kleiner Zwischenspeicher mit Ablaufzeit; die Uhr ist einsteckbar (Tests)."""

    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._data: dict = {}

    def get(self, key, loader):
        hit = self._data.get(key)
        now = self.clock()
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        value = loader()
        self._data[key] = (now, value)
        return value

    def peek(self, key):
        hit = self._data.get(key)
        return hit[1] if hit is not None else None

    def put(self, key, value) -> None:
        self._data[key] = (self.clock(), value)

    def drop(self, key=None) -> None:
        if key is None:
            self._data.clear()
        else:
            self._data.pop(key, None)


def fmt_bytes(n: int | float | None) -> str:
    """Größe wie Steam und Spielkonsolen sie zeigen: Schritte von 1024, beschriftet mit GB/TB,
    deutsches Komma. (So passen die Zahlen zu dem, was man aus Steam kennt.)"""
    if n is None:
        return "Unbekannt"
    n = float(n)
    for unit, size in (("TB", 1024.0 ** 4), ("GB", 1024.0 ** 3), ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if n >= size:
            v = n / size
            s = f"{v:.0f}" if v >= 100 else f"{v:.1f}".replace(".", ",")
            if s.endswith(",0"):
                s = s[:-2]
            return f"{s} {unit}"
    return f"{int(n)} B"
