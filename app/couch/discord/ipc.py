"""„Spielt gerade …“ über die lokale RPC-Schnittstelle des Discord-Clients (E139).

Das ist der Weg, den Discord für Rich Presence selbst anbietet: ein Unix-Socket des laufenden Clients, darauf Rahmen aus
Befehl (uint32, little endian), Länge (uint32, little endian) und JSON. Zuerst der Handschlag (Befehl 0,
{"v": 1, "client_id": <Application ID>}), Discord antwortet mit DISPATCH/READY; danach SET_ACTIVITY (Befehl 1) mit dem
Spiel oder mit activity null zum Löschen. Ein Konto, ein Token oder eine Anmeldung braucht das nicht: Der Client zeigt
die Aktivität für den angemeldeten Nutzer an, unter dem Namen der Application („PC5 Launcher“).

Sockets: $XDG_RUNTIME_DIR/discord-ipc-0…9, dann $XDG_RUNTIME_DIR/app/com.discordapp.Discord/discord-ipc-0…9 (dort legt
das Startskript des Flatpaks seine Weiterleitung an; Bazzite verweist discord-ipc-0 schon darauf).

Presence hat einen eigenen Arbeits-Thread: set_game(), clear() und close() merken sich nur, was gelten soll, und kehren
sofort zurück. Der Thread verbindet sich erst, wenn es etwas zu zeigen gibt, und trennt nach dem Löschen wieder (die
Aktivität eines getrennten Clients löscht Discord ohnehin). Fehler stehen nur im Protokoll; der nächste Wechsel versucht
es neu, sonst frühestens nach RETRY_S. Die Hauptschleife wartet nie auf Discord.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
import time
import uuid
from typing import Any, Callable

from . import FLATPAK

log = logging.getLogger("couch.discord")

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4
HEADER = struct.Struct("<II")
MAX_FRAME = 64 * 1024               # Antworten sind klein; mehr ist kein Discord
TIMEOUT_S = 2.0                     # je Verbindungsaufbau, Handschlag und Antwort
RETRY_S = 15.0                      # nach einem Fehler ohne neuen Wechsel frühestens so spät noch einmal
MAX_DETAILS = 128                   # Discord nimmt höchstens 128 Zeichen


class IpcError(Exception):
    """Discord ist nicht erreichbar oder lehnt ab (Satz nur fürs Protokoll)."""


def pack(op: int, payload: Any) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return HEADER.pack(int(op), len(data)) + data


def _read_exact(sock: Any, size: int) -> bytes:
    out = b""
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise IpcError("Verbindung von Discord geschlossen")
        out += chunk
    return out


def read_frame(sock: Any) -> tuple[int, dict]:
    """Einen Rahmen lesen: (Befehl, JSON als dict). IpcError bei Unsinn."""
    op, size = HEADER.unpack(_read_exact(sock, HEADER.size))
    if size > MAX_FRAME:
        raise IpcError("Rahmen zu groß (%d Bytes)" % size)
    try:
        data = json.loads(_read_exact(sock, size).decode("utf-8")) if size else {}
    except (UnicodeDecodeError, ValueError) as e:
        raise IpcError("kein JSON: %s" % e) from None
    return op, data if isinstance(data, dict) else {}


def socket_paths(runtime_dir: str) -> list[str]:
    """Wo der Client lauschen kann, in dieser Reihenfolge (doppelte über Verweise einmal)."""
    out: list[str] = []
    seen: set[str] = set()
    for base in (runtime_dir, os.path.join(runtime_dir, "app", FLATPAK)):
        for n in range(10):
            path = os.path.join(base, "discord-ipc-%d" % n)
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            out.append(path)
    return out


def activity(title: str, start: float | None) -> dict:
    """Die Aktivität eines Spiels: Titel als details, Startzeit in Sekunden (wie die Rich-Presence-Bibliotheken)."""
    act: dict = {"details": str(title or "")[:MAX_DETAILS] or "Spiel"}
    if start:
        act["timestamps"] = {"start": int(start)}
    return act


def _connect_unix(path: str) -> Any:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_S)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        raise
    return sock


class Presence:
    """Aktivität „Spielt gerade“ im Hintergrund. client_id() und paths() werden im Thread gefragt (jedes Mal neu:
    eine geänderte Application ID gilt beim nächsten Wechsel)."""

    def __init__(self, client_id: Callable[[], str], paths: Callable[[], list], *,
                 connect: Callable[[str], Any] | None = None, pid: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._client_id = client_id
        self._paths = paths
        self._connect = connect or _connect_unix
        self._pid = pid if pid is not None else os.getpid()
        self._clock = clock
        self._cond = threading.Condition()
        self._wanted: dict | None = None        # die Aktivität, die gelten soll (None = keine)
        self._serial = 0                        # zählt jeden Wechsel
        self._done = 0                          # der zuletzt erledigte Wechsel (0: noch keiner, nichts zu tun)
        self._failed = -1                       # der zuletzt gescheiterte Wechsel
        self._closed = False
        self._sock: Any = None
        self._sock_id = ""                      # Application ID der offenen Verbindung
        self._retry_at = 0.0
        self.sent: list[dict | None] = []       # für Tests und das Protokoll: was zuletzt angenommen wurde
        self._thread = threading.Thread(target=self._run, name="discord-ipc", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ Schnittstelle (kehrt sofort zurück)
    def set_game(self, title: str, start: float | None = None) -> None:
        self._want(activity(title, start))

    def clear(self) -> None:
        self._want(None)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Nur für Tests: bis der letzte Wechsel erledigt oder einmal gescheitert ist."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._serial not in (self._done, self._failed) and not self._closed:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.05))
        return True

    def _want(self, act: dict | None) -> None:
        with self._cond:
            if self._closed:
                return
            self._wanted = act
            self._serial += 1
            self._retry_at = 0.0                # ein neuer Wechsel versucht es sofort
            self._cond.notify_all()

    # ------------------------------------------------------------ Thread
    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and (self._done == self._serial or self._clock() < self._retry_at):
                    wait = None if self._done == self._serial else max(0.05, self._retry_at - self._clock())
                    self._cond.wait(wait)
                if self._closed:
                    break
                serial, wanted = self._serial, self._wanted
            try:
                ok = self._apply(wanted)
            except Exception:  # noqa: BLE001 (Unerwartetes kostet nur diesen Wechsel, nie den Thread)
                log.exception("Discord: „Spielt gerade“")
                self._drop()
                ok = False
            with self._cond:
                if ok:
                    self._done = serial
                else:
                    self._failed = serial
                    self._retry_at = self._clock() + RETRY_S
                self._cond.notify_all()
        self._drop()

    def _apply(self, wanted: dict | None) -> bool:
        """Zeigen bzw. löschen. True = erledigt (auch: nichts zu tun)."""
        cid = ""
        try:
            cid = str(self._client_id() or "")
        except Exception:  # noqa: BLE001
            log.exception("Discord: Application ID")
        if wanted is None:
            if self._sock is not None:
                try:
                    self._command(None)
                except (OSError, IpcError) as e:
                    log.info("Discord: Aktivität löschen: %s", e)
                self._drop()
            self.sent.append(None)
            return True
        if not cid:
            self._drop()                        # keine Application ID: nichts zeigen
            return True
        try:
            if self._sock is None or self._sock_id != cid:
                self._drop()
                self._open(cid)
            self._command(wanted)
        except (OSError, IpcError) as e:
            log.info("Discord: „Spielt gerade“ nicht gesetzt: %s", e)
            self._drop()
            return False
        self.sent.append(wanted)
        return True

    def _open(self, cid: str) -> None:
        last: Exception | None = None
        for path in self._paths() or []:
            try:
                sock = self._connect(path)
            except OSError as e:
                last = e
                continue
            try:
                sock.sendall(pack(OP_HANDSHAKE, {"v": 1, "client_id": cid}))
                op, data = read_frame(sock)
                if op == OP_CLOSE or data.get("evt") != "READY":
                    raise IpcError("Handschlag abgelehnt: %s" % (data.get("message") or data.get("evt") or op))
            except (OSError, IpcError, struct.error):
                sock.close()
                raise
            self._sock, self._sock_id = sock, cid
            log.info("Discord: verbunden über %s", path)
            return
        raise IpcError("kein Discord-Socket erreichbar (%s)" % (last or "keiner da"))

    def _command(self, act: dict | None) -> None:
        nonce = str(uuid.uuid4())
        self._sock.sendall(pack(OP_FRAME, {"cmd": "SET_ACTIVITY", "args": {"pid": self._pid, "activity": act},
                                           "nonce": nonce}))
        for _ in range(8):                      # dazwischen kann anderes kommen (PING, fremde Ereignisse)
            op, data = read_frame(self._sock)
            if op == OP_PING:
                self._sock.sendall(pack(OP_PONG, data))
                continue
            if op == OP_CLOSE:
                raise IpcError("Discord trennt: %s" % (data.get("message") or data.get("code")))
            if data.get("nonce") == nonce:
                if data.get("evt") == "ERROR":
                    err = data.get("data")
                    raise IpcError("abgelehnt: %s" % ((err.get("message") if isinstance(err, dict) else err) or "?"))
                return
        raise IpcError("keine Antwort auf SET_ACTIVITY")

    def _drop(self) -> None:
        sock, self._sock, self._sock_id = self._sock, None, ""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
