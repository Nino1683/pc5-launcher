"""Netz für den Fernseher (E142): suchen, MAC lernen, wecken. Alles kurz und ohne Rechte.

Suche: mDNS-Dienst _androidtvremote2._tcp, den jedes Google TV und Android TV mit der Fernbedienung anbietet, über
„avahi-browse -rpt“ (Avahi läuft auf Bazzite, SteamOS und den meisten Desktops). Gelesen werden die Zeilen „=“
(aufgelöst): Name, Rechnername, IPv4-Adresse (ein Fernseher nur mit IPv6 entfällt: clean_host nimmt nur IPv4). Der
TXT-Teil bleibt ungelesen. Fehlt avahi-browse, sagt die Seite das und bietet „Adresse eingeben“ an.

MAC: nach einer gelungenen Verbindung aus der Nachbartabelle (/proc/net/arp), mit dem Netzgerät dazu; nur dafür, den
Fernseher im Standby per Wake-on-LAN zu wecken.

Wake-on-LAN: das Magic Packet (6 × FF, 16 × MAC) per UDP an 255.255.255.255:9 und an die Broadcast-Adresse des
Netzgeräts (SIOCGIFBRDADDR). Der Fernseher wacht nur auf, wenn in seinen Einstellungen „Netzwerk-Standby“ (je nach
Hersteller „Wake on LAN“) an ist.

Prüfmodus: nie das echte Netz. Gefunden wird nur, was COUCH_TEST_TV_FIND (JSON-Liste {name, host, hostname?}) nennt,
die Nachbartabelle nur aus COUCH_TEST_TV_NEIGH (Datei im Format von /proc/net/arp), Wake-on-LAN geht nur an
127.0.0.1:<COUCH_TEST_TV_WOL> (sonst gar nicht).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import socket
import struct
from typing import Callable

from .. import paths

log = logging.getLogger("couch.tv")

DISCOVER_S = 5.0
ARP = "/proc/net/arp"
SIOCGIFBRDADDR = 0x8919
_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_IFACE = re.compile(r"[A-Za-z0-9_.@-]{1,15}")
MAX_FOUND = 12
TEXT_NO_AVAHI = ("Für die Suche im Heimnetz fehlt avahi-browse (Paket avahi-tools bzw. avahi). Gib die Adresse des "
                 "Fernsehers stattdessen ein: Du findest sie in seinen Netzwerk-Einstellungen.")


def _unescape(text: str) -> str:
    """avahi-browse -p schreibt Sonderzeichen als \\DDD (dezimal), z. B. \\032 für ein Leerzeichen."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text).replace("\\.", ".").replace("\\\\", "\\")


def parse_avahi(text: str) -> list[dict]:
    """Zeilen „=;iface;IPv4;Name;Typ;Domäne;Rechner;Adresse;Port;TXT“ -> [{name, host, hostname}] (je Name einmal, nur
    mit IPv4-Adresse; eine IPv6-Zeile hält nur den Platz des Namens in der Reihenfolge)."""
    found: dict[str, dict] = {}
    for line in (text or "").splitlines():
        parts = line.split(";")
        if len(parts) < 9 or parts[0] != "=":
            continue
        name, hostname, addr = _unescape(parts[3]).strip(), parts[6].strip(), parts[7].strip()
        if not name or not addr:
            continue
        v4 = parts[2] == "IPv4" and re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", addr) is not None
        if not v4 and parts[2] != "IPv6":
            continue
        have = found.get(name)
        if have is not None and (have["v4"] or not v4):
            continue
        found[name] = {"name": name[:60], "host": addr, "hostname": hostname[:120], "v4": v4}
    out = [{k: v for k, v in d.items() if k != "v4"} for d in found.values() if d["v4"]]
    return out[:MAX_FOUND]


def can_discover() -> bool:
    if paths.test_mode():
        return True
    return shutil.which("avahi-browse") is not None


def discover(run_read: Callable, service: str) -> list[dict]:
    """Fernseher im Heimnetz. Blockiert bis DISCOVER_S. Wirft nie (leere Liste)."""
    if paths.test_mode():
        try:
            items = json.loads(os.environ.get("COUCH_TEST_TV_FIND") or "[]")
        except ValueError:
            return []
        return [{"name": str(i.get("name") or ""), "host": "127.0.0.1", "hostname": str(i.get("hostname") or "")}
                for i in items if isinstance(i, dict) and i.get("host") == "127.0.0.1"][:MAX_FOUND]
    if shutil.which("avahi-browse") is None:
        return []
    try:
        r = run_read(["avahi-browse", "-rpt", service], timeout=DISCOVER_S)
    except Exception:  # noqa: BLE001 (Suche ist Beiwerk)
        log.exception("avahi-browse")
        return []
    return parse_avahi(getattr(r, "stdout", "") or "")


def neighbour(host: str, path: str | None = None) -> tuple[str, str] | None:
    """(MAC, Netzgerät) des Fernsehers aus der Nachbartabelle, None ohne vollständigen Eintrag."""
    if path is None:
        if paths.test_mode():
            path = os.environ.get("COUCH_TEST_TV_NEIGH") or ""
            if not path:
                return None
        else:
            path = ARP
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            lines = f.read(64 * 1024).splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        cols = line.split()
        if len(cols) >= 6 and cols[0] == host:
            mac, flags, dev = cols[3].lower(), cols[2], cols[5]
            if _MAC.fullmatch(mac) and mac != "00:00:00:00:00:00" and flags != "0x0" and _IFACE.fullmatch(dev):
                return mac, dev
    return None


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", ""))
    if len(raw) != 6:
        raise ValueError(mac)
    return b"\xff" * 6 + raw * 16


def broadcast_of(iface: str) -> str | None:
    """IPv4-Broadcast eines Netzgeräts (ioctl, ohne Unterprozess)."""
    if not iface or not _IFACE.fullmatch(iface):
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        raw = fcntl.ioctl(s.fileno(), SIOCGIFBRDADDR, struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(raw[20:24])
    except OSError:
        return None
    finally:
        s.close()


def wake_on_lan(mac: str, iface: str = "") -> int:
    """Magic Packet senden; liefert, an wie viele Ziele es ging. Im Prüfmodus nur an 127.0.0.1:COUCH_TEST_TV_WOL."""
    if not mac or not _MAC.fullmatch(mac.lower()):
        return 0
    packet = magic_packet(mac.lower())
    if paths.test_mode():
        port = os.environ.get("COUCH_TEST_TV_WOL") or ""
        targets = [("127.0.0.1", int(port))] if port.isdigit() else []
    else:
        targets = [("255.255.255.255", 9)]
        bcast = broadcast_of(iface)
        if bcast and bcast != "255.255.255.255":
            targets.append((bcast, 9))
    sent = 0
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if not paths.test_mode():
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                s.sendto(packet, target)
                sent += 1
            except OSError as e:
                log.info("Wake-on-LAN an %s: %s", target[0], e)
    finally:
        s.close()
    return sent
