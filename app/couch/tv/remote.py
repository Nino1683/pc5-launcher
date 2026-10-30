"""Android-TV-Fernbedienung, Protokoll v2 (E142): Koppeln (Port 6467) und Steuern (Port 6466), beides TLS mit dem
eigenen Client-Zertifikat (keys.py). Das Zertifikat des Fernsehers ist selbst signiert und wird nicht geprüft; was
zählt, ist, dass er unser Zertifikat beim Koppeln angenommen hat.

Koppeln (Pairing): Hülle {1: Protokoll 2, 2: Status 200}. Anfrage (Feld 10: Dienst, Name) -> Optionen (Feld 20:
Kodierung HEX mit 6 Zeichen, bevorzugte Rolle INPUT) -> Konfiguration (Feld 30) -> der Fernseher zeigt einen Code
-> Geheimnis (Feld 40, keys.pairing_secret) -> Status 200 heißt gekoppelt. Pairing hält die Verbindung zwischen
start() und finish() offen (der Nutzer tippt den Code mit der Bildschirmtastatur), höchstens PAIR_WAIT_S.

Steuern (RemoteSession): Der Fernseher schickt remote_configure (Feld 1) und set_active (Feld 2), beides wird
beantwortet, danach nimmt er Tasten (Feld 10 {Code, SHORT}) und App-Links (Feld 90 {Adresse}); Pings (Feld 8) bekommen
ihre Antwort (Feld 9) im eigenen Lese-Thread, Feld 40 sagt, ob der Bildschirm an ist. Ein ungekoppeltes Zertifikat
lehnt der Fernseher ab (TLS-Fehler oder Ende vor remote_configure): NotPaired. Die Lautstärke aus Feld 50 nutzt die
Oberfläche nicht (bei Ton über einen Verstärker steht dort kein brauchbarer Wert).
"""
from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from typing import Any, Callable

from . import TvError, TEXT_REJECTED, TEXT_UNREACHABLE, keys, proto
from .proto import first, msg, parse, sub

log = logging.getLogger("couch.tv")

CONNECT_S = 3.0             # Verbindungsaufbau samt TLS
READY_S = 4.0               # bis remote_configure und set_active beantwortet sind
PAIR_STEP_S = 6.0           # je Schritt der Kopplung
PAIR_WAIT_S = 180.0         # so lange bleibt die Kopplung offen, bis der Code kommt
SERVICE_NAME = "atvremote"
CLIENT_NAME = keys.COMMON_NAME
# Fähigkeiten: Ping, Taste, Ein/Aus, Lautstärke, App-Link (IME und Sprache nicht)
FEATURES = 1 | 2 | 32 | 64 | 512
DIRECTION_SHORT = 3
STATUS_OK = 200
ENC_HEX, ROLE_INPUT = 3, 1


def _context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # selbst signiert; gekoppelt ist, wer unser Zertifikat kennt
    ctx.load_cert_chain(cert, key)
    return ctx


def tls_connect(host: str, port: int, cert: str, key: str, timeout: float = CONNECT_S,
                connect: Callable | None = None) -> ssl.SSLSocket:
    """TCP und TLS. Unreachable, wenn niemand antwortet; Rejected, wenn der Handschlag scheitert."""
    try:
        raw = (connect or socket.create_connection)((host, port), timeout=timeout)
    except OSError as e:
        raise TvError(TEXT_UNREACHABLE, "Unreachable") from e
    try:
        raw.settimeout(timeout)
        return _context(cert, key).wrap_socket(raw)
    except (ssl.SSLError, ConnectionError) as e:
        raw.close()
        raise TvError(TEXT_REJECTED, "NotPaired") from e
    except OSError as e:
        raw.close()
        raise TvError(TEXT_UNREACHABLE, "Unreachable") from e


# ---------------------------------------------------------------- Koppeln
def _pair_msg(*pairs: tuple[int, Any]) -> bytes:
    return msg((1, 2), (2, STATUS_OK), *pairs)


class Pairing:
    """Eine Kopplung mit einem Fernseher. start() -> der Fernseher zeigt den Code; finish(code) -> gekoppelt.
    Nicht für mehrere Threads gleichzeitig (der Teil hält höchstens eine offen)."""

    def __init__(self, host: str, port: int, cert: str, key: str, cert_pem: bytes, connect: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.host, self.port = host, port
        self._cert, self._key, self._pem = cert, key, cert_pem
        self._connect = connect
        self._clock = clock
        self._sock: Any = None
        self.started_at: float | None = None
        self.server_name = ""

    def _send(self, payload: bytes) -> dict:
        self._sock.sendall(proto.frame(payload))
        reply = parse(proto.read_frame(self._sock))
        status = first(reply, 2, 0)
        if status != STATUS_OK:
            raise TvError("Der Fernseher hat die Kopplung abgelehnt (Status %s)." % status, "Rejected")
        return reply

    def start(self) -> None:
        try:
            self._sock = tls_connect(self.host, self.port, self._cert, self._key, connect=self._connect)
        except TvError as e:
            if e.code == "NotPaired":       # schon der Handschlag scheitert: kein Fernseher mit dieser Fernbedienung
                raise TvError("Unter dieser Adresse antwortet kein Google TV oder Android TV.", "Unreachable") from None
            raise
        try:
            self._sock.settimeout(PAIR_STEP_S)
            ack = self._send(_pair_msg((10, msg((1, SERVICE_NAME), (2, CLIENT_NAME)))))
            name = first(sub(ack, 11), 2, b"")
            self.server_name = name.decode("utf-8", "replace") if isinstance(name, bytes) else ""
            enc = msg((1, ENC_HEX), (2, 6))
            self._send(_pair_msg((20, msg((1, enc), (3, ROLE_INPUT)))))
            self._send(_pair_msg((30, msg((1, enc), (2, ROLE_INPUT)))))
        except TvError:
            self.close()
            raise
        except (OSError, proto.ProtoError) as e:
            self.close()
            raise TvError("Die Kopplung mit dem Fernseher brach ab. Versuch es bitte noch einmal.", "Unreachable") from e
        self.started_at = self._clock()
        self._sock.settimeout(PAIR_STEP_S)
        log.info("Fernseher zeigt den Code zur Kopplung")

    def expired(self) -> bool:
        return self._sock is None or self.started_at is None or self._clock() - self.started_at > PAIR_WAIT_S

    def finish(self, code: str) -> None:
        """code: schon geprüft (6 Hex-Zeichen, groß). Passt die Prüfsumme nicht, bleibt die Kopplung offen (BadCode,
        nichts wurde gesendet); lehnt der Fernseher ab, ist sie zu."""
        if self.expired():
            self.close()
            raise TvError("Die Kopplung ist abgelaufen. Wähle den Fernseher noch einmal aus.", "Expired")
        secret = keys.pairing_secret(self._pem, self._sock.getpeercert(binary_form=True), code)
        if not keys.check_code(secret, code):
            from . import TEXT_CODE_TYPO
            raise TvError(TEXT_CODE_TYPO, "BadCode")
        try:
            self._send(_pair_msg((40, msg((1, secret)))))
        except TvError:
            self.close()
            raise TvError("Der Fernseher hat den Code nicht angenommen. Fordere einen neuen Code an.", "Rejected") from None
        except (OSError, proto.ProtoError) as e:
            self.close()
            raise TvError("Die Kopplung mit dem Fernseher brach ab. Versuch es bitte noch einmal.", "Unreachable") from e
        self.close()

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------- Steuern
class RemoteSession:
    """Eine Verbindung zur Fernbedienung. open() wartet, bis der Fernseher Tasten annimmt; ein eigener Thread liest
    (Pings, Ein/Aus). key()/app_link() senden sofort. Endet die Verbindung, ist alive False; eine neue braucht open()."""

    def __init__(self, host: str, port: int, cert: str, key: str, connect: Callable | None = None):
        self.host, self.port = host, port
        self._cert, self._key, self._connect = cert, key, connect
        self._sock: Any = None
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._power = threading.Event()
        self.on: bool | None = None             # Bildschirm an (Feld 40); None = noch nicht gemeldet
        self.errors = 0
        self._reading = False

    # ------------------------------------------------------------ Aufbau
    def open(self, timeout: float = READY_S) -> "RemoteSession":
        self._sock = tls_connect(self.host, self.port, self._cert, self._key, timeout=min(CONNECT_S, timeout),
                                 connect=self._connect)
        self._sock.settimeout(None)
        self._reading = True
        threading.Thread(target=self._reader, args=(self._sock,), name="tv-remote", daemon=True).start()
        end = time.monotonic() + timeout
        while not self._ready.is_set() and not self._closed.is_set() and time.monotonic() < end:
            self._ready.wait(0.05)
        if not self._ready.is_set():
            got_any = self._closed.is_set()
            self.close()
            # Die Verbindung stand, aber der Fernseher hat abgelehnt (unbekanntes Zertifikat) bzw. schwieg
            raise TvError(TEXT_REJECTED, "NotPaired") if got_any else TvError(TEXT_UNREACHABLE, "Unreachable")
        return self

    @property
    def alive(self) -> bool:
        return self._ready.is_set() and not self._closed.is_set()

    def _send(self, payload: bytes) -> None:
        sock = self._sock
        if sock is None or self._closed.is_set():
            raise TvError(TEXT_UNREACHABLE, "Unreachable")
        try:
            with self._send_lock:
                sock.sendall(proto.frame(payload))
        except OSError as e:
            self.close()
            raise TvError(TEXT_UNREACHABLE, "Unreachable") from e

    def _reader(self, sock: Any) -> None:
        try:
            while not self._closed.is_set():
                self._handle(parse(proto.read_frame(sock)))
        except (OSError, ConnectionError, ssl.SSLError, proto.ProtoError, ValueError, TvError) as e:
            if not self._closed.is_set():
                log.info("Fernbedienung getrennt: %s", type(e).__name__)
        finally:
            self._closed.set()
            self._power.set()
            # Nur dieser Thread schließt den Socket: Schlösse ihn close() von außen, bekäme die nächste Verbindung
            # womöglich dieselbe Dateinummer, und dieser Thread läse deren TLS-Daten (so in den Tests gesehen)
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, m: dict) -> None:
        if 1 in m:                              # remote_configure: Fähigkeiten und Angaben zu uns
            # Die Angaben des Fernsehers selbst (Modell, Hersteller) braucht der Launcher nicht: nicht gelesen
            info = msg((1, CLIENT_NAME), (2, "couch"), (3, 1), (4, "1"), (5, SERVICE_NAME), (6, "1.0.0"))
            self._send(msg((1, msg((1, FEATURES), (2, info)))))
        if 2 in m:                              # set_active
            self._send(msg((2, msg((1, FEATURES)))))
            self._ready.set()
        if 8 in m:                              # Ping
            self._send(msg((9, msg((1, first(sub(m, 8), 1, 0))))))
        if 40 in m:                             # remote_start: Bildschirm an/aus
            self.on = bool(first(sub(m, 40), 1, 0))
            self._power.set()
        if 3 in m:                              # remote_error
            self.errors += 1
            log.info("Fernbedienung meldet einen Fehler")

    # ------------------------------------------------------------ Befehle
    def wait_power(self, seconds: float) -> bool | None:
        """Ein/Aus, sobald der Fernseher es gemeldet hat (höchstens seconds); None = unbekannt."""
        if self.on is None:
            self._power.wait(max(0.0, seconds))
        return self.on

    def key(self, code: int) -> None:
        self._send(msg((10, msg((1, int(code)), (2, DIRECTION_SHORT)))))

    def app_link(self, uri: str) -> None:
        self._send(msg((90, msg((1, uri)))))

    def close(self) -> None:
        self._closed.set()
        self._power.set()
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            if self._reading:                   # weckt den Lese-Thread (EOF), der schließt dann selbst
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            else:
                sock.close()
        except OSError:
            pass
