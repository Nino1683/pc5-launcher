"""ADB über das Netz, nur für die Eingänge des Fernsehers (E142, wahlfrei).

Damit erfährt der PC5 Launcher, welche HDMI-Eingänge es gibt, wie die Geräte daran heißen (CEC) und welcher Eingang
gerade läuft. Der Nutzer schaltet dafür am Fernseher „Netzwerk-Debugging“ ein und bestätigt dort einmal „Debugging
zulassen?“ für den Schlüssel keys.ADB_KEY.

Nur feste Befehle (COMMANDS) und das Umschalten auf einen Eingang mit geprüfter Kennung (switch_command); nie
etwas, das ein Nutzer oder der Fernseher eingibt. Protokoll: CNXN, AUTH (Token unterschreiben, sonst der öffentliche
Schlüssel, dann fragt der Fernseher am Bildschirm), OPEN „shell:<befehl>“, WRTE/OKAY, CLSE. Ausgabe höchstens
MAX_OUTPUT Bytes. Ohne Zusage (allow_prompt) wartet connect() nie auf eine Rückfrage am Fernseher: Das braucht nur
„Eingänge genauer erkennen“, nie der Ruhemodus.
"""
from __future__ import annotations

import socket
import struct
from typing import Any, Callable

from . import TvError, keys, passthrough_uri

CNXN, AUTH, OPEN, OKAY, CLSE, WRTE = (struct.unpack("<I", c)[0] for c in (b"CNXN", b"AUTH", b"OPEN", b"OKAY", b"CLSE",
                                                                          b"WRTE"))
VERSION, MAXDATA = 0x01000001, 256 * 1024
TOKEN, SIGNATURE, RSAPUBLICKEY = 1, 2, 3
CONNECT_S = 3.0
STEP_S = 5.0
PROMPT_S = 60.0                 # so lange wartet „Eingänge genauer erkennen“ auf die Zusage am Fernseher
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PACKET = 1024 * 1024

# Die einzigen Befehle. Kennung -> Befehlszeile
COMMANDS = {
    "tv_input": "dumpsys tv_input",
}
TEXT_DENIED = ("Der Fernseher hat das Debugging nicht zugelassen. Wähle „Jetzt erkennen“ noch einmal und bestätige "
               "am Fernseher „Debugging zulassen?“ (am besten mit „Immer von diesem Computer zulassen“).")
TEXT_NO_ADB = ("Der Fernseher antwortet nicht auf Debugging. Schalte in seinen Entwickleroptionen „Netzwerk-Debugging“ "
               "(auch „ADB-Debugging“ oder „USB-Debugging“) ein.")


def switch_command(input_id: str) -> str:
    """am start mit dem App-Link eines Eingangs (Kennung geprüft, siehe passthrough_uri)."""
    return "am start -a android.intent.action.VIEW -d " + passthrough_uri(input_id)


def _packet(cmd: int, a0: int, a1: int, data: bytes = b"") -> bytes:
    return struct.pack("<6I", cmd, a0, a1, len(data), sum(data) & 0xFFFFFFFF, cmd ^ 0xFFFFFFFF) + data


class AdbClient:
    def __init__(self, host: str, port: int, key: Any, connect: Callable | None = None, connect_s: float = CONNECT_S,
                 step_s: float = STEP_S):
        self.host, self.port, self.key = host, port, key
        self._connect = connect or socket.create_connection
        self._connect_s, self._step_s = connect_s, step_s       # kürzer vor dem Ruhemodus (höchstens wenige Sekunden)
        self._sock: Any = None
        self._local = 0

    def _send(self, cmd: int, a0: int, a1: int, data: bytes = b"") -> None:
        self._sock.sendall(_packet(cmd, a0, a1, data))

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            part = self._sock.recv(n - len(buf))
            if not part:
                raise ConnectionError("Verbindung vom Fernseher geschlossen")
            buf += part
        return buf

    def _recv(self) -> tuple[int, int, int, bytes]:
        cmd, a0, a1, n, _crc, magic = struct.unpack("<6I", self._exact(24))
        if magic != cmd ^ 0xFFFFFFFF or n > MAX_PACKET:
            raise ConnectionError("kein ADB")
        return cmd, a0, a1, self._exact(n) if n else b""

    def connect(self, allow_prompt: bool = False, on_prompt: Callable[[], None] | None = None,
                prompt_s: float | None = None) -> None:
        try:
            self._sock = self._connect((self.host, self.port), timeout=self._connect_s)
        except OSError as e:
            raise TvError(TEXT_NO_ADB, "NoAdb") from e
        try:
            self._sock.settimeout(self._step_s)
            self._send(CNXN, VERSION, MAXDATA, b"host::\x00")
            sent_sig = sent_pub = False
            while True:
                cmd, a0, _a1, data = self._recv()
                if cmd == CNXN:
                    self._sock.settimeout(self._step_s)
                    return
                if cmd != AUTH or a0 != TOKEN:
                    continue
                if not sent_sig:
                    self._send(AUTH, SIGNATURE, 0, keys.sign_token(self.key, data))
                    sent_sig = True
                elif not sent_pub and allow_prompt:
                    self._send(AUTH, RSAPUBLICKEY, 0, keys.android_pubkey(self.key))
                    sent_pub = True
                    if on_prompt is not None:
                        on_prompt()
                    self._sock.settimeout(PROMPT_S if prompt_s is None else prompt_s)
                else:
                    raise TvError(TEXT_DENIED, "AdbDenied")
        except TvError:
            self.close()
            raise
        except socket.timeout as e:
            self.close()
            raise TvError(TEXT_DENIED if allow_prompt else TEXT_NO_ADB, "AdbDenied" if allow_prompt else "NoAdb") from e
        except (OSError, ConnectionError, struct.error) as e:
            self.close()
            raise TvError(TEXT_NO_ADB, "NoAdb") from e

    def _shell(self, line: str) -> str:
        if self._sock is None:
            raise TvError(TEXT_NO_ADB, "NoAdb")
        self._local += 1
        local = self._local
        out, remote = b"", 0
        try:
            self._send(OPEN, local, 0, b"shell:" + line.encode("ascii") + b"\x00")
            while True:
                cmd, a0, _a1, data = self._recv()
                if cmd == OKAY:
                    remote = a0
                elif cmd == WRTE:
                    out += data
                    if len(out) > MAX_OUTPUT:
                        raise ConnectionError("Ausgabe zu groß")
                    self._send(OKAY, local, a0)
                elif cmd == CLSE:
                    if remote:
                        self._send(CLSE, local, a0)
                    return out.decode("utf-8", "replace")
        except (OSError, ConnectionError, struct.error) as e:
            self.close()
            raise TvError(TEXT_NO_ADB, "NoAdb") from e

    def run(self, name: str) -> str:
        """Einer der festen Befehle aus COMMANDS."""
        if name not in COMMANDS:
            raise ValueError(name)
        return self._shell(COMMANDS[name])

    def switch(self, input_id: str) -> str:
        return self._shell(switch_command(input_id))

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
