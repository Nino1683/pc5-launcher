"""Prüfsteuerung: Unix-Socket <run>/control.sock, ein Befehl je Zeile, eine JSON-Antwortzeile
{"ok": true, "result": …} oder {"ok": false, "error": …, "message": …}.

Immer erlaubt (Supervisor, Einschaltknopf am Gehäuse):
  power-key                       Einschaltknopf (E38): Menü, beim zweiten Druck Rückfrage, beim dritten bestätigt
Nur mit COUCH_TEST=1:
  ping
  pad <taste> [press|down|up]     wie eine Taste am Pad (InputService.inject)
  call <methode> [json]           RPC-Methode aufrufen, als käme sie von einer Seite (Rolle "test").
                                  Die Antwort trägt zusätzlich "deferred" (die Methode hat ein
                                  Future geliefert) und "ms" (Dauer bis zur Antwort).
  eval <home|overlay> <js>        Ausdruck in der Seite auswerten (keine Promises)
  await <home|overlay> <js>       Rumpf einer async-Funktion ("return await couch.call(…)")
  emit <home|overlay|all> <ereignis> [json]
  snapshot <home|overlay> <datei.png>
  state                           sys.state plus route, ready, firstFrames, reloads, wiringFailed
  frames [reset]                  von GTK gezeichnete Bilder je Fenster
  xid <home|overlay>
  crash <home|overlay>            Web-Prozess beenden (prüft das Neuladen)
  reload <home|overlay>
  quit [code]                     Home beenden (0 = Sitzung endet, 10 = Übergabe an Steam)
  methods                         Namen aller angemeldeten RPC-Methoden
  events start|stop|clear         Ereignisse an die Seiten mitschneiden (höchstens 2000)
  events [get] [ab]               das Mitgeschnittene ab laufender Nummer: [{n, time, event, target, payload}]
  plan <eintrag>                  was beim Start dieses Eintrags geschähe (Befehl, Umgebung, Prefix,
                                  Übergabe, Fehler), ohne etwas zu starten oder zu kopieren
  stalls [reset]                  Aussetzer der Hauptschleife (couch/loopwatch.py), Dauer des
                                  Zusammenbaus und des Einhängens je Baustein
  pool                            Arbeits-Threads: {threads, busy, queued, stuck}, dazu rpcTimeouts
  hang <anzahl> <sekunden>        so viele Aufträge in den Arbeits-Pool, die nur schlafen (stellt
                                  hängende Aufträge nach, z. B. eine schlafende USB-Platte)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .. import log, paths  # noqa: E402

_log = log.get("testctl")
ROLES = ("home", "overlay")
EVENTS_KEEP = 2000


class TestControl:
    __test__ = False        # kein Testfall, auch wenn der Name so aussieht

    def __init__(self, app: Any):
        self.app = app
        directory = paths.run_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = paths.control_socket()
        try:
            os.unlink(self.path)        # Rest eines abgestürzten Home
        except FileNotFoundError:
            pass
        # Socket-Pfade sind auf 107 Bytes begrenzt und Gio kürzt längere stillschweigend
        # (08, Fallstrick 8). Über /proc/self/fd/<n> bleibt der Pfad kurz, egal wie tief der
        # Ordner liegt.
        self._dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        short = "/proc/self/fd/%d/%s" % (self._dirfd, os.path.basename(self.path))
        self._events: list[dict] = []
        self._event_n = 0
        self._recording = False
        self._sink_added = False
        self.service = Gio.SocketService()
        self.service.add_address(Gio.UnixSocketAddress.new(short), Gio.SocketType.STREAM,
                                 Gio.SocketProtocol.DEFAULT, None)
        self.service.connect("incoming", self._incoming)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        _log.info("Prüfsteuerung auf %s (%s)", self.path,
                  "alle Befehle" if paths.control_enabled() else "nur power-key")

    # ------------------------------------------------------------ Ereignisse mitschneiden
    def _record_event(self, target: Any, event: str, payload: Any) -> None:
        if not self._recording:
            return
        self._event_n += 1
        self._events.append({"n": self._event_n, "time": round(time.time(), 3), "event": event,
                             "target": target, "payload": payload})
        del self._events[:-EVENTS_KEEP]

    def _events_command(self, arg: str) -> Any:
        word, _, rest = arg.partition(" ")
        if word == "start":
            if not self._sink_added:
                self.app.rpc.add_sink(self._record_event)
                self._sink_added = True
            self._recording = True
            return {"recording": True, "next": self._event_n + 1}
        if word == "stop":
            self._recording = False
            return {"recording": False, "count": len(self._events)}
        if word == "clear":
            self._events.clear()
            return {"recording": self._recording, "next": self._event_n + 1}
        if word == "get":
            word = rest.strip()
        since = int(word) if word.isdigit() else 0
        return [e for e in self._events if e["n"] >= since]

    # ------------------------------------------------------------ Startplan
    def _plan(self, entry_id: str, ok: Callable, bad: Callable) -> None:
        """Was der Starter für diesen Eintrag bauen würde. Rechnet im Arbeits-Thread (die
        Starter fragen die Platte), startet und kopiert nichts."""
        app = self.app
        entry = app.library.get(entry_id)
        if entry is None:
            bad("Diesen Eintrag gibt es nicht: %s" % entry_id, "NotFound")
            return

        def work() -> dict:
            from ..launch import builders
            from ..launch.spec import LaunchError
            ctx = getattr(getattr(app, "wiring", None), "launch", None)
            if entry.source != "fixture" and ctx is not None:
                return builders.plan_entry(entry, ctx)
            # Prüf-Einträge (und ein Home ohne Starter-Teil): der Auftrag des angemeldeten Starters
            from .. import launch as launch_mod
            out = {"id": entry.id, "title": entry.title, "source": entry.source, "error": None}
            try:
                spec = launch_mod.build_spec(entry, app)
            except LaunchError as e:
                out.update(error=e.message, error_detail=getattr(e, "detail", ""),
                           error_reason=getattr(e, "reason", ""))
                return out
            out.update(argv=list(spec.argv), cwd=spec.cwd, env=dict(spec.env), unset=list(spec.unset),
                       kind=spec.kind, freeze=spec.freeze, mouse=spec.mouse, polite_stop=spec.polite_stop,
                       prelaunch=list(spec.prelaunch or []), handover=getattr(spec, "handover", None))
            return out

        def done(fut: Any) -> None:
            def finish() -> bool:
                try:
                    ok(fut.result())
                except Exception as e:
                    bad("%s: %s" % (type(e).__name__, e), "PlanFailed")
                return False
            GLib.idle_add(finish)
        app.background(work).add_done_callback(done)

    def close(self) -> None:
        try:
            self.service.stop()
            self.service.close()
            os.unlink(self.path)
        except (OSError, GLib.Error):
            pass
        try:
            os.close(self._dirfd)
        except OSError:
            pass

    # ------------------------------------------------------------ Verbindung
    def _incoming(self, service: Any, conn: Any, source: Any) -> bool:
        self._read(conn, Gio.DataInputStream.new(conn.get_input_stream()))
        return True

    def _read(self, conn: Any, din: Any) -> None:
        def got(stream: Any, res: Any) -> None:
            try:
                line, _ = stream.read_line_finish_utf8(res)
            except GLib.Error:
                line = None
            if line is None:
                try:
                    conn.close(None)
                except GLib.Error:
                    pass
                return

            def reply(obj: dict) -> None:
                try:
                    data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode()
                    conn.get_output_stream().write_all(data, None)
                except GLib.Error:
                    return
                self._read(conn, din)
            try:
                self._handle(line.strip(), reply)
            except Exception as e:
                _log.exception("Befehl %r", line)
                reply({"ok": False, "error": type(e).__name__, "message": str(e)})
        din.read_line_async(GLib.PRIORITY_DEFAULT, None, got)

    # ------------------------------------------------------------ Befehle
    def _handle(self, line: str, reply: Callable[[dict], None]) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        app = self.app

        def ok(value: Any = None) -> None:
            reply({"ok": True, "result": value})

        def bad(message: str, error: str = "BadCommand") -> None:
            reply({"ok": False, "error": error, "message": message})

        def role_and_rest() -> tuple[str, str]:
            role, _, rest = arg.partition(" ")
            if role not in ROLES:
                raise ValueError("Rolle muss home oder overlay sein")
            return role, rest.strip()

        if cmd == "power-key":
            app.power.key()
            ok()
            return
        if not paths.control_enabled():
            bad("nur im Prüfmodus (COUCH_TEST=1)", "Forbidden")
            return

        if cmd == "ping":
            ok("pong")
        elif cmd == "pad":
            button, _, phase = arg.partition(" ")
            app.input.inject(button, phase.strip() or "press")
            ok()
        elif cmd == "call":
            method, _, raw = arg.partition(" ")
            params = json.loads(raw) if raw.strip() else {}
            meta: dict = {}
            began = time.monotonic()

            def answered(obj: dict) -> None:
                # Für docs/RPC.md: Hat die Methode sofort geantwortet oder ein Future geliefert,
                # und wie lange hat die Antwort gebraucht?
                obj = dict(obj)
                obj["deferred"] = bool(meta.get("deferred"))
                obj["ms"] = round((time.monotonic() - began) * 1000.0, 1)
                reply(obj)
            app.rpc.handle("test", {"method": method, "params": params}, answered, meta)
        elif cmd in ("eval", "await"):
            role, js = role_and_rest()
            app.ui.eval_full(role, js, lambda good, value: ok(value) if good
                             else bad(str(value), "JsError"), asynchronous=cmd == "await")
        elif cmd == "emit":
            target, _, rest = arg.partition(" ")
            event, _, raw = rest.strip().partition(" ")
            app.rpc.emit(event, json.loads(raw) if raw.strip() else {},
                         target=target if target in ROLES else None)
            ok()
        elif cmd == "snapshot":
            role, path = role_and_rest()
            app.ui.snapshot(role, path, lambda good: ok({"file": path}) if good
                            else bad("Snapshot fehlgeschlagen", "SnapshotError"))
        elif cmd == "state":
            state = app.state.get()
            wiring = getattr(app, "wiring", None)
            state.update(route=app.input.route, ready=app.ready,
                         firstFrames=app.ui.first_frames(), reloads=dict(app.ui.reloads),
                         wiringFailed=list(getattr(wiring, "failed", []) or []) if wiring is not None else None)
            ok(state)
        elif cmd == "frames":
            ok(app.ui.frames(reset=arg == "reset"))
        elif cmd == "xid":
            ok(app.ui.xid(role_and_rest()[0]))
        elif cmd == "crash":
            app.ui.crash(role_and_rest()[0])
            ok()
        elif cmd == "reload":
            app.ui.reload(role_and_rest()[0])
            ok()
        elif cmd == "quit":
            code = int(arg) if arg else 0
            ok()
            GLib.idle_add(lambda: app.quit(code) and False)
        elif cmd == "methods":
            ok(app.rpc.names())
        elif cmd == "events":
            ok(self._events_command(arg))
        elif cmd == "plan":
            self._plan(arg, ok, bad)
        elif cmd == "pool":
            from .. import system
            stats = system.pool_stats()
            stats["rpcTimeouts"] = getattr(app.rpc, "timed_out", 0)
            ok(stats)
        elif cmd == "hang":
            from .. import system
            try:
                count, seconds = (float(x) for x in arg.split())
            except ValueError:
                bad("hang <anzahl> <sekunden>")
                return
            for _ in range(max(0, min(int(count), 32))):
                system.background(time.sleep, max(0.0, min(seconds, 600.0)))
            ok(system.pool_stats())
        elif cmd == "stalls":
            watch = getattr(app, "loopwatch", None)
            report = watch.report() if watch is not None else {"count": 0, "max_ms": 0.0, "items": []}
            report["build_ms"] = getattr(app, "build_ms", 0.0)
            report["start_ms"] = dict(getattr(app, "times", None) or {})
            report["wiring_ms"] = dict(getattr(getattr(app, "wiring", None), "timings", None) or {})
            if arg == "reset" and watch is not None:
                watch.reset()
            ok(report)
        else:
            bad("unbekannter Befehl: %s" % cmd)
