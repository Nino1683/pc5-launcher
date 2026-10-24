"""Der Zustand, den beide Seiten über sys.state holen und über das Ereignis "state" nachgeführt
bekommen. Er liegt in Python, damit eine neu geladene Seite (Absturz des Web-Prozesses) sofort
wieder weiß, was läuft."""
from __future__ import annotations

import copy
import time
from typing import Any, Callable

from . import VERSION


def initial_state() -> dict:
    return {
        "version": VERSION,
        "foreground": None,          # entryId der App vorn, None = Home
        "overlay": "hidden",         # "hidden" | "passive" | "active"
        "running": [],               # [{entryId, appId, title, kind, frozen, started, closing}]
        "pads": [],                  # [{id, name, battery, charging, type, slot}]
        "volume": {"level": 0, "muted": False, "sink": ""},
        "network": {"state": "unknown", "type": "", "name": ""},
        "hdr": {"supported": False, "enabled": False, "active": False},
        "notifications": 0,
        "jobs": [],                  # [{id, title, percent, state}]
        "downloads": [],             # fetcher.view: eigene Downloads und die des Browsers (local: true, E59)
        "mouseMode": False,
        "visible": True,             # False: Home nicht vorn und Overlay versteckt -> Seiten ruhen
        "saver": False,              # Bildschirmschoner offen: Die Home-Seite ruht (E99, schon vor dem Start der Shell)
        # Einstellungen, die die Seite selbst auswertet (die Verdrahtung führt sie aus der Config nach)
        # soundPack: E104 Vorgabe "couch" (das Paket "pc5" nur, wenn web/sounds/pc5 da ist)
        "ui": {"sounds": True, "soundVolume": 60, "soundPack": "couch", "reduceMotion": False, "scale": 1.0,
               "clock24h": True, "liveBackground": True, "areaScale": 100},
        "update_available": False,   # Marke am Zahnrad: ein System-Update wartet (E32)
        "sleep_timer": None,         # E107: {"until": <unix>, "minutes": n} | None (wiring_sleeptimer)
        "setup": {"due": False, "state": ""},   # E105: due = Assistent jetzt von selbst zeigen (wiring_setup)
    }


class StateStore:
    def __init__(self, emit: Callable[[str, dict], None] | None = None, initial: dict | None = None):
        self._emit = emit
        self._state = initial_state()
        if initial:
            self._state.update(copy.deepcopy(initial))

    def get(self) -> dict:
        out = copy.deepcopy(self._state)
        out["time"] = time.time()
        return out

    def value(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._state.get(key, default))

    def update(self, **partial: Any) -> None:
        """Übernimmt die Werte und schickt nur, was sich wirklich geändert hat."""
        changed = {}
        for key, value in partial.items():
            if key not in self._state or self._state[key] != value:
                self._state[key] = copy.deepcopy(value)
                changed[key] = copy.deepcopy(value)
        if changed and self._emit:
            self._emit("state", changed)
