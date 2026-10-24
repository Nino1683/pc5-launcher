"""Ruhemodus-Timer (E107, Teil P8): Verdrahtung, eingehängt über wiring.PARTS.

  RPC power.timer {minutes}     0 = aus; 15, 30, 60, 90, 120 → {ok, until | None, minutes}
  sys.state.sleep_timer         {until: <Unix-Sekunden>, minutes} | None
  power.menu                    letzter Eintrag "sleep-timer" mit note und timer (power.py)
  <state>/sleep-timer.json      {until, minutes}: übersteht einen Neustart des Home

Der Takt selbst steht in idle.IdleWatch (check alle 10 s, dieselben Aufschub-Gründe und derselbe
suspend-Weg wie beim Leerlauf, E82). Die IdleWatch legt wiring_settings.started() erst nach dem
Einhängen der Teile an: Der Dienst bindet sich deshalb spät an wiring.idle (nach dem Bereit-Signal
und bei jedem Aufruf), bis dahin hält er den Stand selbst.

Beim Start wird ein abgelegter Timer nur übernommen, wenn er in der Zukunft liegt (nie gleich nach
einem Absturz schlafen; auch dann kommt erst die Mitteilung, eine Minute später der Ruhemodus).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable

from . import log, paths, system
from .power import TIMER_CHOICES
from .rpc import RpcError

_log = log.get("sleeptimer")

FILE_NAME = "sleep-timer.json"
NOT_HERE = "Der Ruhemodus-Timer ist hier nicht verfügbar."
NO_SUSPEND = "Der Rechner erlaubt keinen Ruhemodus."
BAD_MINUTES = "Diese Zeit gibt es beim Ruhemodus-Timer nicht."
# Spielraum für einen abgelegten Timer: höchstens die längste Wahl (plus eine Minute) voraus
MAX_AHEAD_S = max(TIMER_CHOICES) * 60 + 60


def timer_file() -> str:
    return os.path.join(paths.state_dir(), FILE_NAME)


def _minutes(raw: Any) -> int | None:
    """Gültige Wahl aus TIMER_CHOICES oder None (auch bool, Text, Bruchteile)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = int(raw)
    except (ValueError, OverflowError):     # nan, inf
        return None
    return value if value == raw and value in TIMER_CHOICES else None


def read_saved(path: str, now: float) -> dict | None:
    """Abgelegter Timer {until, minutes}, wenn er gültig ist und in der Zukunft liegt."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _log.warning("%s unlesbar, Timer verworfen", path)
        return None
    if not isinstance(data, dict):
        return None
    until, minutes = data.get("until"), _minutes(data.get("minutes"))
    if isinstance(until, bool) or not isinstance(until, (int, float)) or not minutes:
        return None
    if not now < until <= now + MAX_AHEAD_S:
        return None                     # abgelaufen (Absturz, ausgeschaltet) oder unsinnig weit
    return {"until": float(until), "minutes": minutes}


class SleepTimer:
    """Stand des Timers (Hauptschleife). wiring: app (state, power), idle (spät), _bg."""

    def __init__(self, wiring: Any, wall: Callable[[], float] | None = None, path: str | None = None):
        self.wiring = wiring
        self.app = wiring.app
        self.wall = wall or time.time
        self.path = path or timer_file()
        self.until: float | None = None
        self.minutes = 0
        self._bound: Any = None
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------ Stand
    def info(self) -> dict | None:
        if self.until is None:
            return None
        return {"until": self.until, "minutes": self.minutes}

    def idle(self) -> Any:
        """Die IdleWatch der Verdrahtung, beim ersten Sehen mit dem Stand verbunden."""
        idle = getattr(self.wiring, "idle", None)
        if idle is None or not callable(getattr(idle, "set_timer", None)):
            return None
        if idle is not self._bound:
            self._bound = idle
            idle.on_timer = self._from_idle
            idle.set_timer(self.until)
            if self.until is not None:
                _log.info("Ruhemodus-Timer an den Leerlauf gebunden (noch %.0f s)", self.until - self.wall())
        return idle

    def available(self) -> bool:
        return self.idle() is not None

    def after_ready(self) -> None:
        """Nach dem Bereit-Signal: an die IdleWatch binden. Gibt es keine (Attrappen-Dienste),
        fällt ein abgelegter Timer weg, statt als Mond stehen zu bleiben, der nie auslöst."""
        if self.idle() is None and self.until is not None:
            _log.warning("Ruhemodus-Timer verworfen: kein Leerlauf-Dienst")
            self.until, self.minutes = None, 0
            self._publish()
            self._save()

    def restore(self) -> None:
        """Beim Start: abgelegten Timer übernehmen oder (abgelaufen) die Datei entfernen."""
        saved = read_saved(self.path, self.wall())
        if saved is None:
            if os.path.lexists(self.path):
                self._save()
            return
        self.until, self.minutes = saved["until"], saved["minutes"]
        _log.info("Ruhemodus-Timer wiederhergestellt: %d min, noch %.0f s", self.minutes, self.until - self.wall())
        self._publish()
        self.idle()

    # ------------------------------------------------------------ Ändern
    def set(self, minutes: int) -> dict:
        """power.timer: 0 = aus, sonst ab jetzt minutes Minuten (eine neue Wahl ersetzt die alte)."""
        if minutes:
            if self.idle() is None:
                raise RpcError(NOT_HERE, "Unavailable")
            power = getattr(self.app, "power", None)
            can = getattr(power, "can_do", None)
            if callable(can) and can("suspend") is False:
                raise RpcError(NO_SUSPEND, "Unavailable")
            self.until = self.wall() + minutes * 60
            self.minutes = minutes
            _log.info("Ruhemodus-Timer: in %d min", minutes)
        else:
            if self.until is not None:
                _log.info("Ruhemodus-Timer aus")
            self.until, self.minutes = None, 0
        idle = self.idle()
        if idle is not None:
            idle.set_timer(self.until)
        self._publish()
        self._save()
        return {"ok": True, "until": self.until, "minutes": self.minutes}

    def _from_idle(self, until: float | None) -> None:
        """IdleWatch hat den Timer selbst geändert (aufgeschoben, ausgelöst, aufgewacht)."""
        if until is None:
            self.until, self.minutes = None, 0
        else:
            self.until = float(until)
        self._publish()
        self._save()

    def _publish(self) -> None:
        try:
            self.app.state.update(sleep_timer=self.info())
        except Exception:
            _log.exception("sys.state.sleep_timer")

    def _save(self) -> None:
        """Im Arbeits-Thread (write_atomic schreibt mit fsync). Jeder Auftrag schreibt den Stand zur
        Zeit seiner Ausführung: Aufträge in anderer Reihenfolge enden trotzdem beim jüngsten."""
        bg = getattr(self.wiring, "_bg", None)
        if callable(bg):
            try:
                bg(self._write)
                return
            except Exception:
                _log.exception("Ruhemodus-Timer ablegen")
        self._write()

    def _write(self) -> None:
        with self._write_lock:
            info = self.info()
            try:
                if info is None:
                    try:
                        os.unlink(self.path)        # eine eigene Datei, nichts Rekursives
                    except FileNotFoundError:
                        pass
                else:
                    system.write_atomic(self.path, json.dumps(info) + "\n")
            except OSError as e:
                _log.warning("%s nicht schreibbar: %s", self.path, e)


def extend(wiring: Any, wall: Callable[[], float] | None = None) -> None:
    """RPC power.timer, sys.state.sleep_timer, Eintrag im Ein/Aus-Menü (Bauplan 4.3, 9.2).
    wall nur für Tests (Handuhr)."""
    app = wiring.app
    svc = SleepTimer(wiring, wall=wall)
    wiring.sleep_timer = svc
    power = getattr(app, "power", None)
    if power is not None:
        power.sleep_timer = svc
    svc.restore()

    def power_timer(params: dict) -> dict:
        minutes = _minutes((params or {}).get("minutes"))
        if minutes is None:
            raise RpcError(BAD_MINUTES, "BadValue")
        return svc.set(minutes)
    app.rpc.register("power.timer", power_timer)
    # Die IdleWatch entsteht in wiring_settings.started(), nach diesem Einhängen
    wiring.after_ready.append(svc.after_ready)
