"""Leerlauf: Bildschirmschoner und Ruhemodus (Wunsch 2026-10-21: automatisch in den Ruhemodus wie
an der Konsole; „Wenn der Leerlauf startet, soll ein Screensaver starten“).

Bedienung heißt wie an der Konsole: der Controller (Eingabedienst idle_seconds). Nach
ui.saver_after Minuten legt sich der Bildschirmschoner über Home oder Spiel (Overlay, passiv: das
Spiel läuft weiter, der erste Druck schließt ihn; evdev-Leser bekommen ihn nicht, hidraw-Leser wie
Proton im Controller-Modus „direkt“ schon, Shell.open_saver). Nach power.sleep_after
Minuten (bei Medien-Apps power.sleep_after_media) geht die Konsole in den Ruhemodus, eine Minute
vorher kommt eine Mitteilung.

Was vorn ist, entscheidet:
  Home, Spiel, Emulator       Bildschirmschoner und Ruhemodus (power.sleep_after)
  YouTube, Kodi, Browser      nur Ruhemodus (power.sleep_after_media): beim Zuschauen drückt
                              niemand eine Taste, der Schoner läge über dem Film
  Desktop, Werkzeuge          nie: dort wird mit Tastatur und Maus gearbeitet, die das Home nicht sieht

Nie in den Ruhemodus, solange etwas arbeitet (blockers: Installation, Download, Dateiauftrag,
System-Update, Ein/Aus) oder ein Programm ihn bei logind sperrt. Nach dem Start und nach dem
Aufwachen zählt der Leerlauf neu (die monotone Uhr steht im Ruhemodus).

Ruhemodus-Timer (E107): set_timer(until) nach der Wanduhr (übersteht den Ruhemodus und einen
Neustart des Home, wiring_sleeptimer legt ihn ab). Eine Minute vorher die Mitteilung, ab until
dieselben Aufschub-Gründe wie beim Leerlauf, dann in TIMER_POSTPONE_S-Schritten aufgeschoben;
sonst derselbe suspend-Rückruf. Danach und nach dem Aufwachen ist der Timer aus. Eine Taste in
der letzten Minute verlängert ihn nicht (anders als beim Leerlauf).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import log

_log = log.get("idle")

CHECK_MS = 10_000
WARN_S = 60
MAX_MINUTES = 24 * 60
SAVER_KINDS = (None, "game", "emulator")
SLEEP_KEYS = {None: "power.sleep_after", "game": "power.sleep_after", "emulator": "power.sleep_after",
              "media": "power.sleep_after_media", "browser": "power.sleep_after_media"}
# E107: Ruhemodus-Timer
TIMER_POSTPONE_S = 300              # aufgeschoben wird in 5-Minuten-Schritten
TIMER_TITLE = "Ruhemodus-Timer"
TIMER_WARN = "Die Konsole geht in einer Minute in den Ruhemodus. Abbrechen im Ein/Aus-Menü."
TIMER_HELD = "Ruhemodus aufgeschoben: %s"


def minutes(raw: Any) -> float:
    """Wert einer Einstellung in Minuten; alles Unbrauchbare heißt 0 = nie."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return float(raw) if 0 < raw <= MAX_MINUTES else 0.0


class IdleWatch:
    """sched: later(ms, fn)/cancel/now (Shell); front_kind() -> None (Home) oder LaunchSpec.kind;
    saver: open() -> bool, close(), is_open(); blockers() -> [Grund]; notify(title, body);
    suspend() schickt die Konsole in den Ruhemodus; wall() Wanduhr des Ruhemodus-Timers (E107,
    Vorgabe time.time). on_timer(until | None) meldet, wenn der Timer sich selbst ändert
    (aufgeschoben, ausgelöst, nach dem Aufwachen aus); gesetzt von wiring_sleeptimer."""

    def __init__(self, sched: Any, config: Any, idle_seconds: Callable[[], float],
                 front_kind: Callable[[], str | None], saver: Any, blockers: Callable[[], list],
                 notify: Callable[[str, str], None], suspend: Callable[[], None],
                 wall: Callable[[], float] | None = None):
        self.sched, self.config = sched, config
        self.idle_seconds, self.front_kind = idle_seconds, front_kind
        self.saver, self.blockers, self.notify, self.suspend = saver, blockers, notify, suspend
        self.wall = wall or time.time
        self._since = sched.now()
        self._warned_at: float | None = None
        self._held_by = ""
        self._timer = None
        # E107: Ruhemodus-Timer (Wanduhr): Ziel, Zeitpunkt der Mitteilung, zuletzt gemeldeter Aufschub
        self.timer_until: float | None = None
        self._timer_warned_at: float | None = None
        self._timer_held = ""
        self.on_timer: Callable[[float | None], None] | None = None

    def start(self) -> None:
        self._timer = self.sched.later(CHECK_MS, self._tick)

    def stop(self) -> None:
        self.sched.cancel(self._timer)
        self._timer = None

    def resumed(self) -> None:
        """Aufgewacht (ResumeWatch): Leerlauf zählt neu, auch nach einem Ruhemodus von Hand.
        Ein Ruhemodus-Timer ist danach aus (E107), egal wer den Rechner schlafen ließ."""
        self._since = self.sched.now()
        self._warned_at = None
        if self.timer_until is not None:
            _log.info("Ruhemodus-Timer nach dem Aufwachen aus")
            self.set_timer(None)
            self._timer_changed()

    # ------------------------------------------------------------ Ruhemodus-Timer (E107)
    def set_timer(self, until: float | None) -> None:
        """Ruhemodus zur Wanduhrzeit until (Unix-Sekunden), None = aus. Meldet nichts über
        on_timer zurück (der Aufrufer weiß es schon)."""
        self.timer_until = float(until) if until is not None else None
        self._timer_warned_at = None
        self._timer_held = ""

    def _timer_changed(self) -> None:
        fn = self.on_timer
        if fn is None:
            return
        try:
            fn(self.timer_until)
        except Exception:
            _log.exception("Ruhemodus-Timer melden")

    def _check_timer(self) -> bool:
        """Takt des Timers; True, wenn er gerade den Ruhemodus ausgelöst hat."""
        until = self.timer_until
        if until is None:
            return False
        now = self.wall()
        if now < until - WARN_S:
            return False
        # In der letzten Minute schon fragen: Die Sperren bei logind liest ein Arbeits-Thread
        # nach, bis until sind sie frisch
        held = [str(b) for b in (self.blockers() or []) if b]
        if now < until:
            if not held and self._timer_warned_at is None:
                self._timer_warned_at = now
                self.notify(TIMER_TITLE, TIMER_WARN)
            return False
        if held:
            self.timer_until = now + TIMER_POSTPONE_S
            self._timer_warned_at = None
            if held[0] != self._timer_held:
                self._timer_held = held[0]
                self.notify(TIMER_TITLE, TIMER_HELD % held[0])
            _log.info("Ruhemodus-Timer aufgeschoben: %s", held[0])
            self._timer_changed()
            return False
        # Immer erst die Mitteilung, eine Minute später schlafen (auch nach einem Aufschub oder
        # einem Neustart des Home kurz vor der Zeit): nie ohne Vorwarnung
        if self._timer_warned_at is None:
            self._timer_warned_at = now
            self.notify(TIMER_TITLE, TIMER_WARN)
            return False
        if now - self._timer_warned_at < WARN_S:
            return False
        _log.info("Ruhemodus durch den Timer")
        self.set_timer(None)
        self._timer_changed()
        self.saver.close()
        self._since = self.sched.now()
        self._warned_at = None
        self.suspend()
        return True

    def _tick(self) -> None:
        self._timer = self.sched.later(CHECK_MS, self._tick)
        try:
            self.check()
        except Exception:
            _log.exception("Leerlauf")

    def idle(self) -> float:
        return max(0.0, min(self.idle_seconds(), self.sched.now() - self._since))

    def check(self) -> None:
        if self._check_timer():
            return
        kind = self.front_kind()
        idle = self.idle()
        saver = minutes(self.config.get("ui.saver_after", 0))
        if kind in SAVER_KINDS and saver and idle >= saver * 60 and not self.saver.is_open():
            if self.saver.open():
                _log.info("Bildschirmschoner nach %.0f min ohne Bedienung", idle / 60)
        key = SLEEP_KEYS.get(kind)
        limit = minutes(self.config.get(key, 0)) * 60 if key else 0.0
        if not limit or idle < limit - WARN_S:
            self._warned_at = None
            self._held_by = ""
            return
        held = [str(b) for b in (self.blockers() or []) if b]
        if held:
            if held[0] != self._held_by:
                self._held_by = held[0]
                _log.info("Ruhemodus aufgeschoben: %s", held[0])
            self._warned_at = None
            return
        self._held_by = ""
        # Immer erst die Mitteilung, eine Minute später schlafen: auch wenn ein Download erst
        # endet, als die Zeit schon lange um war
        now = self.sched.now()
        if self._warned_at is None:
            self._warned_at = now
            self.notify("Ruhemodus in einer Minute", "Drück eine Taste am Controller, um wach zu bleiben.")
            return
        if idle < limit or now - self._warned_at < WARN_S:
            return
        self._warned_at = None
        _log.info("Ruhemodus nach %.0f min ohne Bedienung", idle / 60)
        self.saver.close()
        self._since = self.sched.now()
        self.suspend()


def logind_blockers(run_read: Callable[..., Any]) -> list[str]:
    """Programme, die Schlaf oder Leerlauf bei logind sperren (Modus block), als Gründe.
    run_read wie system.run_read (CompletedProcess)."""
    import json
    try:
        r = run_read(["busctl", "--system", "--json=short", "call", "org.freedesktop.login1",
                      "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListInhibitors"], timeout=5)
        if r.returncode != 0:
            return []
        rows = json.loads(r.stdout).get("data", [[]])[0]
    except Exception:  # noqa: BLE001 - ohne Antwort sperrt nichts
        return []
    out_list = []
    for row in rows:
        if len(row) >= 4 and row[3] == "block" and any(w in str(row[0]).split(":") for w in ("sleep", "idle")):
            out_list.append("%s sperrt den Ruhemodus (%s)" % (row[1], row[2]))
    return out_list


class ResumeWatch:
    """Erkennt das Aufwachen aus dem Ruhemodus: Die monotone Uhr steht im Schlaf, die Wanduhr
    läuft weiter. Springt die Wanduhr zwischen zwei Takten um mehr als JUMP_S weiter als die
    monotone, war der PC im Ruhemodus; dann on_resume() in der Hauptschleife."""

    TICK_MS = 2000
    JUMP_S = 5.0

    def __init__(self, sched: Any, on_resume: Callable[[], None], wall: Callable[[], float] | None = None):
        import time as _time
        self.sched, self.on_resume = sched, on_resume
        self.wall = wall or _time.time
        self._last = (self.wall(), sched.now())
        self._timer = None

    def start(self) -> None:
        self._timer = self.sched.later(self.TICK_MS, self._tick)

    def stop(self) -> None:
        self.sched.cancel(self._timer)
        self._timer = None

    def _tick(self) -> None:
        self._timer = self.sched.later(self.TICK_MS, self._tick)
        wall, mono = self.wall(), self.sched.now()
        jump = (wall - self._last[0]) - (mono - self._last[1])
        self._last = (wall, mono)
        if jump > self.JUMP_S:
            _log.info("Aufgewacht nach %.0f s Ruhemodus", jump)
            try:
                self.on_resume()
            except Exception:
                _log.exception("nach dem Aufwachen")
