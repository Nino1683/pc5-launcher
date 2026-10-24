"""Kabel-Kopplung wie an der Konsole: Steckt ein DualSense per USB, trägt der PC sich als sein
Bluetooth-Gegenüber ein. Nach dem Abziehen meldet sich das Pad per PS-Taste beim PC.

Die Konsole schreibt über USB ihre Adresse und einen Schlüssel ins Pad (Feature-Bericht 0x0A) und
schaltet dessen Funk kurz aus und ein (0x08). Hier geht nur die Adresse hinein; der Schlüssel ist
Zufall, den BlueZ nicht kennt. Das Pad verbindet sich, findet keinen passenden Schlüssel und koppelt
neu ("Just Works"). Bazzite lässt das ohne Rückfrage zu (/etc/bluetooth/main.conf:
JustWorksRepairing=always, AlwaysPairable=true). So braucht es weder Root noch einen Eingriff in
/var/lib/bluetooth. BlueZ' eigenes Kabel-Koppeln (plugins/sixaxis.c) kennt nur PS3- und PS4-Pads
und macht es dort genauso (Schlüssel aus Nullen).

Aufbau der Berichte (controllers.fandom.com/wiki/Sony_DualSense/Data_Structures, Längen aus dem
HID-Deskriptor des Pads):
  0x09 lesen, 20 Bytes:  ID, Pad-Adresse (6, rückwärts), 08 25 00, Gegenüber (6, rückwärts), 3 frei
  0x0A schreiben, 27 B.: ID, Gegenüber (6, rückwärts), Schlüssel (16), 4 frei
  0x08 schreiben, 48 B.: ID, Funk (1 = ein, 2 = aus), Rest frei
UNBESTÄTIGT: dass das Pad nach falschem Schlüssel selbst neu koppelt (beim DS4 belegt, BlueZ #2678).

Alles hier blockiert kurz (ioctl, ein paar Zehntelsekunden Pause) und gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import fcntl
import os
import re
import time

REPORT_PAIRING_INFO, REPORT_SET_PAIRING, REPORT_BT_CONTROL = 0x09, 0x0A, 0x08
SIZE_PAIRING_INFO, SIZE_SET_PAIRING, SIZE_BT_CONTROL = 20, 27, 48
BT_ON, BT_OFF = 1, 2
PAUSE = 0.3                 # Sekunden zwischen den Schritten, damit das Pad sie einzeln verarbeitet
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _ioc_rw(nr: int, size: int) -> int:
    # _IOC(_IOC_READ | _IOC_WRITE, 'H', nr, size) aus linux/hidraw.h
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


def hidiocgfeature(size: int) -> int:
    return _ioc_rw(0x07, size)


def hidiocsfeature(size: int) -> int:
    return _ioc_rw(0x06, size)


def valid_mac(mac: str) -> bool:
    return bool(_MAC.match(str(mac).upper()))


def mac_to_le(mac: str) -> bytes:
    """"12:34:56:78:9A:BC" -> b"\\xbc\\x9a\\x78\\x56\\x34\\x12" (so steht sie im Pad)."""
    if not valid_mac(mac):
        raise ValueError(f"keine Bluetooth-Adresse: {mac!r}")
    return bytes(reversed(bytes.fromhex(mac.replace(":", ""))))


def mac_from_le(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in reversed(data))


def parse_pairing_info(data: bytes) -> tuple[str, str] | None:
    """Bericht 0x09 -> (Adresse des Pads, Adresse seines Gegenübers). None, wenn er nicht passt."""
    if len(data) < 16 or data[0] != REPORT_PAIRING_INFO:
        return None
    return mac_from_le(data[1:7]), mac_from_le(data[10:16])


def build_set_pairing(host_mac: str, key: bytes) -> bytes:
    if len(key) != 16:
        raise ValueError("Schlüssel braucht 16 Bytes")
    body = bytes([REPORT_SET_PAIRING]) + mac_to_le(host_mac) + bytes(key)
    return body + bytes(SIZE_SET_PAIRING - len(body))


def build_bt_control(state: int) -> bytes:
    return bytes([REPORT_BT_CONTROL, state]) + bytes(SIZE_BT_CONTROL - 2)


class Hidraw:
    """Feature-Berichte über einen geöffneten hidraw-Knoten (Tests ersetzen das durch eine Attrappe)."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)

    def get_feature(self, report_id: int, size: int) -> bytes:
        buf = bytearray(size)
        buf[0] = report_id
        n = fcntl.ioctl(self.fd, hidiocgfeature(size), buf, True)
        return bytes(buf[:n if isinstance(n, int) and 0 < n <= size else size])

    def set_feature(self, data: bytes) -> None:
        fcntl.ioctl(self.fd, hidiocsfeature(len(data)), bytearray(data), True)

    def close(self) -> None:
        os.close(self.fd)


def read_pairing(dev) -> tuple[str, str] | None:
    try:
        return parse_pairing_info(dev.get_feature(REPORT_PAIRING_INFO, SIZE_PAIRING_INFO))
    except OSError:
        return None


def pair_over_cable(dev, host_mac: str, key: bytes | None = None, pause: float | None = None) -> dict:
    """Trägt host_mac als Gegenüber ein, wenn das Pad noch ein anderes hat.

    Ergebnis {"status": ..., "pad": Adresse des Pads, "host": Gegenüber vorher}; status ist
    "already" (koppelt schon mit diesem PC), "paired" (neu eingetragen und nachgelesen),
    "ignored" (das Gerät hat geschrieben, aber nicht übernommen – Funk-Adapter, die sich als Pad
    ausgeben) oder "unreadable".
    """
    host_mac = host_mac.upper()
    pause = PAUSE if pause is None else pause
    info = read_pairing(dev)
    if info is None:
        return {"status": "unreadable", "pad": "", "host": ""}
    pad, before = info
    out = {"pad": pad, "host": before}
    if before == host_mac:
        return {**out, "status": "already"}
    try:
        dev.set_feature(build_set_pairing(host_mac, key if key is not None else os.urandom(16)))
        time.sleep(pause)
        # Funk aus und wieder ein, wie es die Konsole tut: erst dann sucht das Pad das neue Gegenüber
        dev.set_feature(build_bt_control(BT_OFF))
        time.sleep(pause)
        dev.set_feature(build_bt_control(BT_ON))
        time.sleep(pause)
    except OSError as e:
        return {**out, "status": "ignored", "error": str(e)}
    after = read_pairing(dev)
    if after is None or after[1] != host_mac:
        return {**out, "status": "ignored"}
    return {**out, "status": "paired"}
