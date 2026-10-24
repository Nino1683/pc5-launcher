"""Absturzschleife eines Web-Prozesses erkennen (ohne GTK, für Tests einzeln benutzbar).

UiHost lädt eine Seite nach jedem Absturz ihres Web-Prozesses neu. Stirbt er immer wieder, ohne
dass die Seite dazwischen einmal geschlagen hat (sys.beat kommt alle 5 s), sieht der Nutzer nur ein
schwarzes Bild, während jedes neue erste Bild dem Supervisor Leben vorgaukeln könnte. Ab LIMIT
solcher Abstürze binnen WINDOW_S meldet crashed() True: Das Home endet dann als Absturz, und die
Rückfallebene (previous, Steam) greift.
"""
from __future__ import annotations

import time
from typing import Callable

LIMIT = 5
WINDOW_S = 60.0


class CrashWatch:
    def __init__(self, limit: int = LIMIT, window_s: float = WINDOW_S,
                 clock: Callable[[], float] = time.monotonic):
        self.limit = max(1, int(limit))
        self.window_s = float(window_s)
        self._clock = clock
        self._crashes: dict[str, list[float]] = {}

    def beat(self, role: str) -> None:
        """Die Seite lebt wirklich (sys.beat): Abstürze davor zählen nicht mehr."""
        self._crashes.pop(role, None)

    def crashed(self, role: str) -> bool:
        """Ein Absturz. True = Absturzschleife (LIMIT binnen WINDOW_S ohne Herzschlag dazwischen)."""
        now = self._clock()
        recent = [t for t in self._crashes.get(role, []) if now - t < self.window_s] + [now]
        self._crashes[role] = recent
        return len(recent) >= self.limit

    def count(self, role: str) -> int:
        now = self._clock()
        return sum(1 for t in self._crashes.get(role, []) if now - t < self.window_s)
