"""Protobuf von Hand und die Rahmen der Android-TV-Fernbedienung (E142).

Beide Ports (Koppeln 6467, Steuern 6466) schicken Nachrichten mit einer Länge als varint davor. Gebraucht werden nur
varint- und Längenfelder; ein eigener Codegenerator oder ein fremdes Paket ist dafür nicht nötig. parse() liefert
{feld: [werte]} (varint als int, Länge als bytes), so wie die Nachrichten auf dem Draht stehen.
"""
from __future__ import annotations

from typing import Any

MAX_FRAME = 64 * 1024           # Nachrichten der Fernbedienung sind klein; mehr ist kein Fernseher


class ProtoError(Exception):
    """Unsinn auf der Leitung (falscher Drahttyp, abgeschnitten, zu groß)."""


def varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1           # negative Zahlen wie protobuf als 64 Bit
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(num: int, value: Any) -> bytes:
    """int/bool -> varint, bytes/str -> Länge, list -> wiederholt."""
    if isinstance(value, list):
        return b"".join(field(num, v) for v in value)
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return varint(num << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return varint((num << 3) | 2) + varint(len(value)) + bytes(value)


def msg(*pairs: tuple[int, Any]) -> bytes:
    return b"".join(field(n, v) for n, v in pairs)


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise ProtoError("varint abgeschnitten")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def parse(b: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    i = 0
    while i < len(b):
        key, i = _read_varint(b, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 2:
            n, i = _read_varint(b, i)
            if i + n > len(b):
                raise ProtoError("Feld %d abgeschnitten" % num)
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        else:
            raise ProtoError("Drahttyp %d" % wt)
        out.setdefault(num, []).append(v)
    return out


def first(m: dict, num: int, default: Any = None) -> Any:
    values = m.get(num)
    return values[0] if values else default


def sub(m: dict, num: int) -> dict:
    """Eingebettete Nachricht (Feld num) oder {} (auch bei Unsinn: die Fernbedienung soll nicht daran sterben)."""
    value = first(m, num, b"")
    if not isinstance(value, (bytes, bytearray)):
        return {}
    try:
        return parse(bytes(value))
    except ProtoError:
        return {}


def frame(payload: bytes) -> bytes:
    return varint(len(payload)) + payload


def _exact(sock: Any, n: int) -> bytes:
    data = b""
    while len(data) < n:
        part = sock.recv(n - len(data))
        if not part:
            raise ConnectionError("Verbindung vom Fernseher geschlossen")
        data += part
    return data


def read_frame(sock: Any) -> bytes:
    """Eine Nachricht samt Längenpräfix lesen (blockierend bis zur Zeitgrenze des Sockets)."""
    length, shift = 0, 0
    while True:
        c = _exact(sock, 1)[0]
        length |= (c & 0x7F) << shift
        shift += 7
        if c < 0x80:
            break
        if shift > 28:
            raise ProtoError("Länge zu groß")
    if length > MAX_FRAME:
        raise ProtoError("Nachricht zu groß (%d Bytes)" % length)
    return _exact(sock, length) if length else b""
