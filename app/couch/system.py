"""Systembefehle des Home. Zwei Türen, damit im Prüfmodus nichts den Rechner verändert:

  run_read(argv)   liest nur (pactl list, nmcli -t …) und läuft immer.
  run_write(argv)  verändert etwas (Ausschalten, steamosctl, pactl set-…, nmcli-Änderungen,
                   bluetoothctl, flatpak). Im Prüfmodus wird nur eine Zeile nach
                   <state>/writes.log geschrieben und Erfolg gemeldet.

ALLE schreibenden Befehle des Home gehen durch run_write, nie direkt durch subprocess.
Beide blockieren: aus der Hauptschleife nur über background() aufrufen, wenn es länger als
etwa 50 ms dauern kann.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import Future
from typing import Any, Callable

from . import jsontext, log, paths

_log = log.get("system")
_write_lock = threading.Lock()
_pool: "Pool | None" = None


def _result(argv: list[str], code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(list(argv), code, out, err)


def _run(argv: list[str], timeout: float, env: dict | None, cwd: str | None,
         input: str | None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              env=env, cwd=cwd, input=input, errors="replace",
                              stdin=None if input is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        return _result(argv, 127, "", "Befehl nicht gefunden: %s" % argv[0])
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return _result(argv, 124, out, "Zeitüberschreitung nach %.0f s" % timeout)
    except OSError as e:
        return _result(argv, 126, "", str(e))


def run_read(argv: list[str], timeout: float = 5.0, env: dict | None = None,
             cwd: str | None = None, input: str | None = None) -> subprocess.CompletedProcess:
    """Lesender Befehl. Wirft nie: fehlendes Programm = 127, Zeitüberschreitung = 124."""
    return _run(argv, timeout, env, cwd, input)


# Befehle, die Gamescope über dessen eigenen Wayland-Socket ansprechen (Bildraten-Limit, E23)
GAMESCOPE_TOOLS = ("gamescopectl",)
GAMESCOPE_SOCKET_VAR = "GAMESCOPE_WAYLAND_DISPLAY"
_session_socket: list[str] = []         # gemerkt: Name aus der Sitzungsumgebung ("" = keiner)


def gamescope_env(argv: list[str], env: dict | None = None) -> dict | None:
    """Umgebung für gamescopectl: Der Befehl findet Gamescope nur über GAMESCOPE_WAYLAND_DISPLAY
    (ohne die Variable endet er mit 1, das Limit bliebe aus). Das Home erbt sie vom Supervisor;
    fehlt sie in der eigenen Umgebung, steht sie in der der Sitzung. Alle anderen Befehle
    bekommen env unverändert."""
    if not argv or os.path.basename(str(argv[0])) not in GAMESCOPE_TOOLS:
        return env
    out = dict(os.environ if env is None else env)
    if not out.get(GAMESCOPE_SOCKET_VAR):
        if not _session_socket:
            try:
                _session_socket.append(paths.session_env().get(GAMESCOPE_SOCKET_VAR, "") or "")
            except Exception:           # eine unlesbare Datei darf keinen Befehl verhindern
                _session_socket.append("")
        if _session_socket[0]:
            out[GAMESCOPE_SOCKET_VAR] = _session_socket[0]
    return out


def run_write(argv: list[str], timeout: float = 15.0, env: dict | None = None,
              cwd: str | None = None, input: str | None = None,
              note: str = "") -> subprocess.CompletedProcess:
    """Verändernder Befehl. Im Prüfmodus nur protokolliert (Rückgabewert 0, leere Ausgabe)."""
    for_gamescope = bool(argv) and os.path.basename(str(argv[0])) in GAMESCOPE_TOOLS
    env = gamescope_env(argv, env)
    if paths.test_mode():
        socket_name = (env or {}).get(GAMESCOPE_SOCKET_VAR) if for_gamescope else None
        record_write(argv, note, {GAMESCOPE_SOCKET_VAR: socket_name} if socket_name else None)
        return _result(argv, 0)
    _log.info("run_write %s%s", " ".join(argv), " (%s)" % note if note else "")
    return _run(argv, timeout, env, cwd, input)


def record_write(argv: list[str], note: str = "", env: dict | None = None) -> None:
    """Eine Zeile JSON je unterdrücktem Befehl; Tests lesen die Datei. env: nur, was der Befehl
    zusätzlich gebraucht hätte (GAMESCOPE_WAYLAND_DISPLAY für gamescopectl)."""
    record: dict = {"time": round(time.time(), 3), "argv": list(argv), "note": note}
    if env:
        record["env"] = dict(env)
    line = jsontext.dumps(record)
    with _write_lock:
        try:
            os.makedirs(paths.state_dir(), mode=0o700, exist_ok=True)
            with open(paths.writes_log(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            _log.warning("writes.log nicht schreibbar: %s", e)


def read_writes() -> list[dict]:
    """Inhalt von writes.log (für Tests)."""
    out = []
    try:
        with open(paths.writes_log(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except (OSError, ValueError):
        pass
    return out


class Pool:
    """Arbeits-Threads für background().

    Grundstock BASE Threads wie ein gewöhnlicher Pool. Hängen Aufträge (schlafende USB-Platte,
    FUSE, ein Unterprozess ohne Ende), zählen ihre Threads nach STUCK_S nicht mehr mit: Für
    wartende Aufträge kommen weitere Threads dazu, bis CAP. Sonst legten vier hängende
    Aufträge Einstellungen, Starts und „Ausschalten“ lahm, während das Home weiter schlägt.
    Überzählige Threads enden nach IDLE_S ohne Arbeit wieder.

    Alle Threads sind Daemons: Ein hängender Auftrag hält das Ende des Home-Prozesses nie auf
    (sonst ginge eine Übergabe an Steam verloren und der Supervisor hielte das Home für hängend).
    Ein Wächter schreibt Aufträge, die länger als SLOW_S laufen, einmal mit Stapel ins Protokoll.
    """

    BASE = 4
    CAP = 16
    STUCK_S = 5.0
    IDLE_S = 30.0
    SLOW_S = 30.0
    WATCH_S = 1.0

    def __init__(self, name: str = "couch-bg", base: int | None = None, cap: int | None = None,
                 stuck_s: float | None = None, slow_s: float | None = None,
                 idle_s: float | None = None, watch_s: float | None = None):
        self.name = name
        self.base = base or self.BASE
        self.cap = max(self.base, cap or self.CAP)
        self.stuck_s = self.STUCK_S if stuck_s is None else stuck_s
        self.slow_s = self.SLOW_S if slow_s is None else slow_s
        self.idle_s = self.IDLE_S if idle_s is None else idle_s
        self.watch_s = self.WATCH_S if watch_s is None else watch_s
        self._cond = threading.Condition()
        self._queue: collections.deque = collections.deque()
        self._workers: dict[threading.Thread, dict] = {}     # Thread -> {"since", "what", "warned"}
        self._idle = 0
        self._closed = False
        self._counter = 0
        self._watching = False

    # ------------------------------------------------------------ Aufträge
    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        fut: Future = Future()
        with self._cond:
            if self._closed:
                fut.set_exception(RuntimeError("Das Home endet gerade"))
                return fut
            self._queue.append((fut, fn, args, kwargs))
            self._grow()
            self._cond.notify()
            if not self._watching:
                self._watching = True
                threading.Thread(target=self._watch, name=self.name + "-watch", daemon=True).start()
        return fut

    def _grow(self) -> None:
        """Unter dem Schloss: genug Threads für die wartenden Aufträge? Hängende zählen nicht."""
        if self._closed or self._idle >= len(self._queue):
            return
        now = time.monotonic()
        stuck = sum(1 for w in self._workers.values()
                    if w["since"] is not None and now - w["since"] >= self.stuck_s)
        alive = len(self._workers)
        if alive - stuck >= self.base or alive >= self.cap:
            return
        self._counter += 1
        t = threading.Thread(target=self._work, name="%s-%d" % (self.name, self._counter), daemon=True)
        self._workers[t] = {"since": None, "what": "", "warned": False}
        t.start()
        if stuck:
            _log.warning("Arbeits-Threads: %d hängen seit über %.0f s, ein weiterer kommt dazu (%d)",
                         stuck, self.stuck_s, alive + 1)

    def _next(self, record: dict) -> tuple | None:
        """Unter dem Schloss: nächster Auftrag oder None (Thread soll enden)."""
        self._idle += 1
        try:
            idle_since = time.monotonic()
            while not self._queue:
                if self._closed:
                    return None
                if len(self._workers) > self.base and time.monotonic() - idle_since >= self.idle_s:
                    return None                 # überzähliger Thread ohne Arbeit
                self._cond.wait(self.idle_s)
            item = self._queue.popleft()
        finally:
            self._idle -= 1
        record["since"] = time.monotonic()
        record["what"] = getattr(item[1], "__qualname__", None) or repr(item[1])
        record["warned"] = False
        return item

    def _work(self) -> None:
        me = threading.current_thread()
        try:
            while True:
                with self._cond:
                    record = self._workers[me]
                    item = self._next(record)
                if item is None:
                    return
                fut, fn, args, kwargs = item
                if fut.set_running_or_notify_cancel():
                    try:
                        value = fn(*args, **kwargs)
                    except BaseException as e:  # geht an den, der auf das Future wartet
                        fut.set_exception(e)
                    else:
                        fut.set_result(value)
                # Ein ruhender Thread hielte sonst Auftrag, Future samt Rückrufen und Ergebnis bis
                # zum nächsten Auftrag fest (Absturzanalyse H1, E99)
                item = fut = fn = args = kwargs = value = None
                with self._cond:
                    record["since"] = None
        finally:
            with self._cond:
                self._workers.pop(me, None)
                self._grow()                    # falls gerade jemand wartet

    # ------------------------------------------------------------ Wächter
    def _watch(self) -> None:
        """Läuft nur, solange es Arbeit gibt: holt Threads nach, wenn alle hängen, und meldet
        lange Aufträge einmal mit Stapel."""
        while True:
            time.sleep(self.watch_s)
            with self._cond:
                busy = [(t, w) for t, w in self._workers.items() if w["since"] is not None]
                if self._closed or (not busy and not self._queue):
                    self._watching = False
                    return
                self._grow()
                now = time.monotonic()
                slow = []
                for t, w in busy:
                    if now - w["since"] >= self.slow_s and not w["warned"]:
                        w["warned"] = True
                        slow.append((t, w["what"], now - w["since"]))
            for t, what, seconds in slow:
                frame = sys._current_frames().get(t.ident or -1)
                stack = "".join(traceback.format_stack(frame)) if frame is not None else "(kein Stapel)\n"
                _log.warning("Auftrag %s läuft seit %.0f s (Thread %s):\n%s", what, seconds, t.name,
                             stack.rstrip())

    # ------------------------------------------------------------ Zustand, Ende
    def stats(self) -> dict:
        with self._cond:
            now = time.monotonic()
            busy = [w for w in self._workers.values() if w["since"] is not None]
            return {"threads": len(self._workers), "busy": len(busy), "queued": len(self._queue),
                    "stuck": sum(1 for w in busy if now - w["since"] >= self.stuck_s)}

    def shutdown(self) -> int:
        """Das Home endet: Wartendes absagen (CancelledError), laufende Threads nicht abwarten."""
        with self._cond:
            self._closed = True
            dropped = list(self._queue)
            self._queue.clear()
            self._cond.notify_all()
        for fut, _fn, _args, _kwargs in dropped:
            fut.cancel()
        return len(dropped)


def background(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Führt fn in einem Arbeits-Thread aus. Das Future darf eine RPC-Methode zurückgeben;
    wer selbst weiterarbeitet, holt sich das Ergebnis über GLib.idle_add in die Hauptschleife."""
    global _pool
    if _pool is None:
        _pool = Pool()
    return _pool.submit(fn, *args, **kwargs)


def dedicated(fn: Callable[..., Any], *args: Any, name: str = "couch-own", **kwargs: Any) -> Future:
    """Wie background(), aber in einem eigenen Thread nur für diesen Auftrag: für das, was nie
    hinter anderen Aufträgen warten darf (Ausschalten, Neustart und die logind-Abfrage dafür)."""
    fut: Future = Future()

    def run() -> None:
        if not fut.set_running_or_notify_cancel():
            return
        try:
            value = fn(*args, **kwargs)
        except BaseException as e:
            fut.set_exception(e)
        else:
            fut.set_result(value)
    threading.Thread(target=run, name=name, daemon=True).start()
    return fut


def pool_stats() -> dict:
    """Zahlen des Pools (für die Prüfsteuerung und das Protokoll)."""
    return _pool.stats() if _pool is not None else {"threads": 0, "busy": 0, "queued": 0, "stuck": 0}


def shutdown_pool() -> int:
    """Am Ende des Home: wartende Aufträge absagen. Liefert ihre Zahl."""
    return _pool.shutdown() if _pool is not None else 0


class WriteBehind:
    """Schreibt Dateien atomar in einem eigenen Thread, damit das fsync nie die Hauptschleife
    aufhält (settings.json, library.json, library-cache.json; unter Last braucht ein fsync auch
    einmal Zehntelsekunden). Je Pfad gilt der letzte Stand: Was sich schneller ändert, als
    geschrieben wird, überspringt Zwischenstände.

        writer = WriteBehind()
        writer.write(pfad, text)        # kehrt sofort zurück
        writer.flush(3.0)               # vor dem Ende des Home: wartet, bis alles auf der Platte ist
    """

    def __init__(self, name: str = "couch-write"):
        self._name = name
        self._cond = threading.Condition()
        self._pending: dict[str, tuple[bytes | str, int]] = {}
        self._busy = False
        self._thread: threading.Thread | None = None

    def write(self, path: str, data: bytes | str, mode: int = 0o600) -> None:
        with self._cond:
            self._pending.pop(path, None)       # neuer Stand ersetzt den wartenden, hinten angestellt
            self._pending[path] = (data, mode)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._pending:
                    self._busy = False
                    self._cond.notify_all()
                    self._cond.wait()
                path = next(iter(self._pending))
                data, mode = self._pending.pop(path)
                self._busy = True
            try:
                write_atomic(path, data, mode)
            except Exception as e:              # volle Platte o. Ä.: Der Wert gilt im Speicher weiter
                _log.error("%s nicht speicherbar: %s", path, e)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wartet, bis nichts mehr aussteht. False, wenn das in timeout Sekunden nicht gelang."""
        end = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._pending or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True


def write_atomic(path: str, data: bytes | str, mode: int = 0o600) -> None:
    """Temp-Datei im selben Ordner + rename: Leser sehen nie eine halbe Datei."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
