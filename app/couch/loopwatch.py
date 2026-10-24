"""Wächter für die GLib-Hauptschleife: misst, wie lange sie am Stück nicht drankommt, und
schreibt Aussetzer mit dem Python-Stapel des Schuldigen ins Protokoll.

Fein (TICK_MS, ab LIMIT_MS) nur im Prüfmodus (COUCH_TEST=1) oder mit COUCH_LOOP_WATCH=1. Im
normalen Betrieb läuft eine grobe Fassung (start_quiet: alle QUIET_TICK_MS, Aussetzer ab
QUIET_LIMIT_MS), weil die Hauptschleife am Fernseher sekundenlang stand und niemand sah, wo
(2026-10-18, „Die Hauptschleife antwortet nicht“); COUCH_LOOP_WATCH=0 schaltet auch sie ab.
Zwei Teile:

- Ein Zeitgeber in der Hauptschleife (alle TICK_MS) notiert, wann er zuletzt lief. Kommt er
  später als geplant, stand die Schleife mindestens so lange ("ms"); weil ein Aussetzer auch
  kurz nach einem Lauf beginnen kann, höchstens TICK_MS länger. Gemeldet wird deshalb schon
  ab LIMIT_MS - TICK_MS: Nichts über LIMIT_MS geht durch.
- Ein Thread sieht alle paar Millisekunden nach. Ist der Zeitgeber so lange überfällig, hält
  er den Stapel des Hauptthreads fest (sys._current_frames): Dort steht, welcher Aufruf die
  Schleife gerade aufhält. Kommt die Schleife wieder dran, wird die Dauer dazugeschrieben.

Hält der Schuldige die Sperre des Interpreters (C-Code ohne Freigabe), kommt auch der Thread
nicht dran; dann steht der Aussetzer ohne Stapel da („kein Stapel“).
- Dazu eine Probe mit der niedrigsten Stufe (GLib.idle_add): Am 2026-10-19 lief die Schleife
  weiter (keine Aussetzer), aber Leerlauf-Aufträge kamen minutenlang nicht dran, weil ständig
  etwas mit höherem Vorrang bereit war. Wartet die Probe länger als IDLE_LIMIT_S, schreibt der
  Wächter das ins Protokoll, mit den häufigsten Stellen, an denen der Hauptthread in der Zeit
  stand (Stichproben; „in GLib/GTK“ heißt: kein Python-Rückruf, sondern C-Code der Schleife).

    watch = loopwatch.start()            # in der Hauptschleife, nach dem Zusammenbau
    watch.report()                       # {"limit_ms", "ticks", "count", "max_ms", "items": [...]}
    watch.reset()

Die Prüfsteuerung reicht das als Befehl "stalls [reset]" heraus (ui/testctl.py).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from typing import Any

from . import log, paths

_log = log.get("loopwatch")

TICK_MS = 20
QUIET_TICK_MS = 250
QUIET_LIMIT_MS = 1500.0
LIMIT_MS = 100.0                # Aussetzer, die so lange dauern können, werden gemeldet
KEEP = 60                       # so viele Aussetzer bleiben im Speicher
IDLE_LIMIT_S = 3.0              # so lange darf ein Leerlauf-Auftrag warten, bevor es gemeldet wird
IDLE_SAMPLES = 400              # höchstens so viele Stichproben je Meldung


def wanted() -> bool:
    return paths.control_enabled() or os.environ.get("COUCH_LOOP_WATCH") == "1"


def _limit() -> float:
    try:
        return max(20.0, float(os.environ.get("COUCH_LOOP_LIMIT_MS", "") or LIMIT_MS))
    except ValueError:
        return LIMIT_MS


class LoopWatch:
    def __init__(self, limit_ms: float | None = None, tick_ms: int = TICK_MS):
        self.limit_ms = float(limit_ms if limit_ms is not None else _limit())
        self.tick_ms = int(tick_ms)
        # Schwelle für die gemessene Mindestdauer (siehe Modulkopf)
        self.limit = max(10.0, self.limit_ms - self.tick_ms) / 1000.0
        self._main = threading.main_thread().ident
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._pending: dict | None = None       # Stapel eines laufenden Aussetzers
        self._count = 0
        self._max = 0.0
        self._ticks = 0
        self._stop = threading.Event()
        self._source = 0
        self._thread: threading.Thread | None = None
        self.started = time.time()
        self._idle_sent: float | None = None    # Probe mit Leerlauf-Vorrang unterwegs seit
        self._idle_told = False                 # diese Wartezeit schon gemeldet
        self._idle_samples: dict[str, int] = {}
        self._idle_waits = 0
        self._idle_max = 0.0

    # ------------------------------------------------------------ Start und Ende
    def start(self) -> "LoopWatch":
        from gi.repository import GLib
        self._main = threading.get_ident()
        self._last = time.monotonic()
        self._source = GLib.timeout_add(self.tick_ms, self._tick)
        self._thread = threading.Thread(target=self._watch, name="couch-loopwatch", daemon=True)
        self._thread.start()
        _log.info("Wächter der Hauptschleife läuft (meldet Aussetzer ab %.0f ms)", self.limit_ms)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._source:
            from gi.repository import GLib
            try:
                GLib.source_remove(self._source)
            except Exception:
                pass
            self._source = 0

    # ------------------------------------------------------------ Hauptschleife
    def _tick(self) -> bool:
        now = time.monotonic()
        gap = now - self._last
        self._last = now
        self._ticks += 1
        # TICK_MS Abstand ist gewollt; was darüber hinausgeht, hat jemand aufgehalten
        held = gap - self.tick_ms / 1000.0
        if held >= self.limit:
            self._record(held)
        else:
            with self._lock:
                self._pending = None
        if self._idle_sent is None:
            from gi.repository import GLib
            self._idle_sent = now
            GLib.idle_add(self._idle_probe)     # Vorrang G_PRIORITY_DEFAULT_IDLE, wie bisher alle Rückrufe
        return not self._stop.is_set()

    def _idle_probe(self) -> bool:
        sent, self._idle_sent = self._idle_sent, None
        waited = time.monotonic() - sent if sent is not None else 0.0
        if waited >= IDLE_LIMIT_S:
            with self._lock:
                samples, self._idle_samples = self._idle_samples, {}
                self._idle_told = False
                self._idle_waits += 1
                self._idle_max = max(self._idle_max, waited)
            _log.warning("Leerlauf-Aufträge warteten %.1f s (Schleife lief, etwas mit Vorrang war ständig bereit)%s",
                         waited, _top(samples))
        return False

    def _record(self, seconds: float) -> None:
        with self._lock:
            pending, self._pending = self._pending, None
            stack = pending["stack"] if pending else []
            item = {"ms": round(seconds * 1000.0, 1), "time": round(time.time(), 3),
                    "since_start": round(time.time() - self.started, 2), "stack": stack}
            self._items.append(item)
            del self._items[:-KEEP]
            self._count += 1
            self._max = max(self._max, seconds)
        where = stack[-1] if stack else "kein Stapel (C-Code oder Sperre des Interpreters gehalten)"
        _log.warning("Hauptschleife stand mindestens %.0f ms: %s", seconds * 1000.0, where)
        if stack:
            _log.info("Stapel des Aussetzers:\n  %s", "\n  ".join(stack))

    # ------------------------------------------------------------ Thread
    def step(self) -> float:
        """Abstand der Nachschau des Threads. Im Prüfmodus fein (20 ms); im groben Betrieb im Takt
        des Zeitgebers (250 ms), sonst weckte er den Prozess 50-mal je Sekunde (E99)."""
        return max(0.02, min(self.limit / 4, self.tick_ms / 1000.0))

    def _watch(self) -> None:
        step = self.step()
        while not self._stop.wait(step):
            self._watch_idle()
            late = time.monotonic() - self._last - self.tick_ms / 1000.0
            if late < self.limit:
                continue
            with self._lock:
                if self._pending is not None:
                    continue                    # dieser Aussetzer hat seinen Stapel schon
            frame = sys._current_frames().get(self._main)
            if frame is None:
                continue
            stack = ["%s:%d %s" % (_short(f.filename), f.lineno or 0, f.name)
                     for f in traceback.extract_stack(frame)][-14:]
            with self._lock:
                self._pending = {"stack": stack}

    def _watch_idle(self) -> None:
        """Stichproben, solange die Leerlauf-Probe zu lange wartet; einmal melden, auch wenn sie
        nie mehr drankommt."""
        sent = self._idle_sent
        if sent is None or time.monotonic() - sent < IDLE_LIMIT_S:
            return
        frame = sys._current_frames().get(self._main)
        where = "in GLib/GTK"
        if frame is not None:
            stack = traceback.extract_stack(frame)
            inner = [f for f in stack if not f.filename.endswith(("threading.py", "Gtk.py", "GLib.py"))]
            if inner and inner[-1].name not in ("run", "main", "_run"):
                where = " < ".join("%s:%d %s" % (_short(f.filename), f.lineno or 0, f.name) for f in inner[-3:][::-1])
        with self._lock:
            if sum(self._idle_samples.values()) < IDLE_SAMPLES:
                self._idle_samples[where] = self._idle_samples.get(where, 0) + 1
            told, self._idle_told = self._idle_told, True
            samples = dict(self._idle_samples)
        if not told:
            _log.warning("Leerlauf-Aufträge kommen seit %.0f s nicht dran%s", IDLE_LIMIT_S, _top(samples))

    # ------------------------------------------------------------ Auskunft
    def report(self) -> dict:
        with self._lock:
            return {"limit_ms": self.limit_ms, "tick_ms": self.tick_ms, "ticks": self._ticks,
                    "count": self._count, "max_ms": round(self._max * 1000.0, 1),
                    "idle_waits": self._idle_waits, "idle_max_s": round(self._idle_max, 2),
                    "items": [dict(i) for i in self._items]}

    def reset(self) -> None:
        with self._lock:
            self._items.clear()
            self._count = 0
            self._max = 0.0
            self._pending = None


def _top(samples: dict) -> str:
    """Die häufigsten Stichproben als Anhang für das Protokoll."""
    if not samples:
        return ""
    total = sum(samples.values())
    best = sorted(samples.items(), key=lambda kv: -kv[1])[:4]
    return "; Hauptthread stand bei: " + "; ".join("%s (%d von %d)" % (w, n, total) for w, n in best)


def _short(path: str) -> str:
    """Pfade im Stapel kurz halten: ab „couch/“ bzw. nur der Dateiname."""
    marker = os.sep + "couch" + os.sep
    if marker in path:
        return "couch/" + path.rsplit(marker, 1)[1]
    return os.path.basename(path)


def start(limit_ms: float | None = None) -> Any:
    """Startet den Wächter, wenn er gewünscht ist; sonst None. Wirft nie."""
    if not wanted():
        return None
    try:
        return LoopWatch(limit_ms).start()
    except Exception:
        _log.exception("Wächter der Hauptschleife startet nicht")
        return None


def start_quiet() -> Any:
    """Grobe Fassung für den normalen Betrieb (siehe Modulkopf); None bei COUCH_LOOP_WATCH=0."""
    if os.environ.get("COUCH_LOOP_WATCH") == "0":
        return None
    try:
        return LoopWatch(QUIET_LIMIT_MS, QUIET_TICK_MS).start()
    except Exception:
        _log.exception("Wächter der Hauptschleife startet nicht")
        return None
