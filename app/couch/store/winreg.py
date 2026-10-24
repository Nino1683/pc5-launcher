"""Registry eines Wine-/Proton-Prefix lesen (system.reg, user.reg), nur lesend (Bauplan E94).

Die Launcher von Blizzard, Ubisoft und Rockstar tragen installierte Spiele in die Registry ein; die
Konsole erkennt daran, was im Prefix des Launchers installiert ist. Wine schreibt die Datei als Text:

    [Software\\\\Wow6432Node\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\Battle.net] 1792668775
    #time=1dd621914f8cd70
    "DisplayName"="Battle.net"
    "EstimatedSize"=dword:00000400

Schlüssel kommen ohne Wurzel (system.reg = HKEY_LOCAL_MACHINE) mit einfachen Backslashes und klein
geschrieben zurück, Wertnamen klein. Nur Zeichenketten und DWORDs; alles andere wird übergangen.
Nur Standardbibliothek.
"""
from __future__ import annotations

import os
import re

_SECTION = re.compile(r"^\[(.+?)\](?:\s+\d+)?\s*$")
_VALUE = re.compile(r'^(?:"((?:[^"\\]|\\.)*)"|(@))=(.*)$')


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "x":
            m = re.match(r"[0-9a-fA-F]{1,4}", text[i + 2:])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
                continue
        out.append({"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(nxt, nxt))
        i += 2
    return "".join(out)


def _value(raw: str):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return _unescape(raw[1:-1])
    if raw.startswith("str(2):") or raw.startswith("str(7):"):
        inner = raw.split(":", 1)[1].strip()
        return _unescape(inner[1:-1]) if inner.startswith('"') and inner.endswith('"') else None
    if raw.startswith("dword:"):
        try:
            return int(raw[6:], 16)
        except ValueError:
            return None
    return None


def parse(text: str, prefixes: tuple = ()) -> dict:
    """{schlüssel: {wertname: wert}}. prefixes (klein, einfache Backslashes): nur Schlüssel, die so
    beginnen (spart bei großen Dateien Zeit und Speicher)."""
    want = tuple(p.lower() for p in prefixes)
    out: dict = {}
    current = None
    for line in text.splitlines():
        if line.startswith("["):
            m = _SECTION.match(line)
            key = _unescape(m.group(1)).lower() if m else ""
            current = None
            if key and (not want or key.startswith(want)):
                current = out.setdefault(key, {})
            continue
        if current is None or not line or line[0] not in '"@':
            continue
        m = _VALUE.match(line)
        if not m:
            continue
        name = "@" if m.group(2) else _unescape(m.group(1)).lower()
        value = _value(m.group(3))
        if value is not None:
            current[name] = value
    return out


def read(path: str, prefixes: tuple = ()) -> dict:
    """Wie parse(), aus einer Datei; fehlt sie oder ist sie unlesbar: {}."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse(fh.read(), prefixes)
    except OSError:
        return {}


def subkeys(data: dict, parent: str) -> dict:
    """{letzter Teil: werte} der direkten Unterschlüssel von parent."""
    parent = parent.lower().rstrip("\\") + "\\"
    return {key[len(parent):]: values for key, values in data.items()
            if key.startswith(parent) and "\\" not in key[len(parent):]}


def to_linux(win_path: str, prefix: str) -> str:
    """Windows-Pfad im Prefix als Linux-Pfad: C:\\… → <prefix>/drive_c/…, Z:\\… → /…; sonst ""."""
    p = str(win_path or "").replace("\\", "/").strip().strip('"')
    m = re.match(r"^([A-Za-z]):/?(.*)$", p)
    if not m:
        return ""
    drive, rest = m.group(1).lower(), m.group(2)
    if drive == "z":
        return "/" + rest
    if drive == "c":
        return os.path.join(prefix, "drive_c", rest)
    target = os.path.join(prefix, "dosdevices", drive + ":")
    try:
        return os.path.join(os.path.realpath(target), rest)
    except OSError:
        return ""


def to_windows(linux_path: str, prefix: str) -> str:
    """Linux-Pfad als Windows-Pfad für Programme im Prefix (drive_c → C:, sonst Z:)."""
    real = os.path.realpath(linux_path)
    drive_c = os.path.realpath(os.path.join(prefix, "drive_c"))
    if real == drive_c or real.startswith(drive_c + "/"):
        return "C:\\" + real[len(drive_c):].lstrip("/").replace("/", "\\")
    return "Z:" + real.replace("/", "\\")
