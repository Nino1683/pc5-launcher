"""Eingänge des Fernsehers (E142): dumpsys tv_input lesen und den Eingang des PCs aus der EDID bestimmen.

dumpsys tv_input (AOSP TvInputManagerService und TvInputHardwareManager), gelesen wird nur:
  TvInputHardwareInfo {id=N, type=9, …, hdmi_port=P}   Gerät N ist HDMI-Eingang P (type 9 = HDMI)
  mHardwareInputIdMap: deviceId -> inputId              Kennung des Eingangs je Gerät
  mHdmiDeviceList: CEC: logical_address: 0xLL … display_name: NAME … physical_address: 0xPPPP port_id: P
                                                        Name eines CEC-Geräts an Eingang P
  mHdmiInputIdMap: id -> inputId                        Eingänge der CEC-Geräte (id = logische Adresse)
  inputMap: <inputId>: info: …; state: S                0 verbunden, 1 Standby, 2 getrennt
  sessionStateMap … inputId / sessionToken, mainSessionToken
                                                        der laufende Eingang (auch ein CEC-Eingang: dann sein Port)
Was fehlt oder anders aussieht, bleibt einfach weg; die Oberfläche kommt dann mit weniger aus (HDMI 1 bis 4).

EDID: Der HDMI-Herstellerblock (CTA-861-Erweiterung, IEEE-OUI 00-0C-03) trägt die CEC-Adresse, die der Fernseher
diesem Anschluss gibt (vier Stellen a.b.c.d zu je 4 Bit, z. B. 0x3000). Die erste Stelle ist der Eingang des
Fernsehers, auch hinter einem Verstärker (0x4100 -> Eingang 4).
"""
from __future__ import annotations

import re

from . import MAX_PORT, valid_input_id

TYPE_HDMI = 9
OUI_HDMI = 0x000C03
STATE_TEXT = {0: "connected", 1: "standby", 2: "disconnected"}

_HW = re.compile(r"TvInputHardwareInfo \{id=(\d+), type=(\d+)[^}]*?\bhdmi_port=(\d+)")
_MAP_LINE = re.compile(r"^\s+(\d+):\s+(\S+)\s*$")
_STATE = re.compile(r"^\s+(\S+): info: TvInputInfo\{.*\}; state: (\d+)\s*$")
_CEC = re.compile(r"\bCEC: logical_address: 0x([0-9A-Fa-f]+)\b(.*)$")
_NAME = re.compile(r"\bdisplay_name: (.*?)(?=\s+(?:power_status|physical_address|port_id|vendor_id|device_type)"
                   r":|\s*$)")
_PHYS = re.compile(r"\bphysical_address: 0x([0-9A-Fa-f]{1,4})\b")
_PORT_ID = re.compile(r"\bport_id: (\d+)\b")
_DEVTYPE = re.compile(r"\bdevice_type: (\d+)\b")
_SESSION_INPUT = re.compile(r"^\s+inputId: (\S+)\s*$")
_SESSION_TOKEN = re.compile(r"^\s+sessionToken: (\S+)\s*$")
_MAIN_TOKEN = re.compile(r"^\s+mainSessionToken: (\S+)\s*$")
MAX_NAME = 40


def _section(lines: list[str], header: str) -> list[tuple[str, str]]:
    """Zeilen „  n: wert“ direkt unter der Überschrift (bis zur ersten anderen Zeile)."""
    out: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        if line.strip().startswith(header):
            for follow in lines[i + 1:]:
                m = _MAP_LINE.match(follow)
                if not m:
                    break
                out.append((m.group(1), m.group(2)))
            break
    return out


def _clean_name(raw: str) -> str:
    name = "".join(ch for ch in raw if ch.isprintable()).strip()
    return name[:MAX_NAME]


def parse_tv_input(text: str) -> dict:
    """{ports: [{port, id, name, state}], current: {id, port} | None}. ports nach Nummer, nur HDMI. Wirft nie."""
    lines = (text or "").splitlines()
    hw_port: dict[str, int] = {}                        # Gerät -> Eingang
    for m in _HW.finditer(text or ""):
        if int(m.group(2)) == TYPE_HDMI and 1 <= int(m.group(3)) <= MAX_PORT:
            hw_port[m.group(1)] = int(m.group(3))
    port_id: dict[int, str] = {}
    for dev, input_id in _section(lines, "mHardwareInputIdMap:"):
        if dev in hw_port and valid_input_id(input_id):
            port_id.setdefault(hw_port[dev], input_id)
    cec: dict[int, dict] = {}                           # logische Adresse -> {port, name, type}
    for line in lines:
        m = _CEC.search(line)
        if not m:
            continue
        rest = m.group(2)
        pid, phys = _PORT_ID.search(rest), _PHYS.search(rest)
        port = int(pid.group(1)) if pid else 0
        if not 1 <= port <= MAX_PORT and phys:
            port = int(phys.group(1), 16) >> 12
        name, dtype = _NAME.search(rest), _DEVTYPE.search(rest)
        cec[int(m.group(1), 16)] = {"port": port if 1 <= port <= MAX_PORT else 0,
                                    "name": _clean_name(name.group(1)) if name else "",
                                    "type": int(dtype.group(1)) if dtype else -1}
    cec_input: dict[str, int] = {}                      # Kennung eines CEC-Eingangs -> Eingang
    for logical, input_id in _section(lines, "mHdmiInputIdMap:"):
        dev = cec.get(int(logical))
        if dev and dev["port"]:
            cec_input[input_id] = dev["port"]
    states: dict[str, int] = {}
    for line in lines:
        m = _STATE.match(line)
        if m:
            states[m.group(1)] = int(m.group(2))
    tokens: dict[str, str] = {}
    main = None
    pending = None
    for line in lines:
        m = _SESSION_INPUT.match(line)
        if m:
            pending = m.group(1)
            continue
        m = _SESSION_TOKEN.match(line)
        if m and pending:
            tokens[m.group(1)] = pending
            pending = None
            continue
        m = _MAIN_TOKEN.match(line)
        if m:
            main = m.group(1)
    ports = []
    for port in sorted(port_id):
        input_id = port_id[port]
        devices = [d for d in cec.values() if d["port"] == port and d["name"]]
        # Ein Wiedergabegerät vor dem Verstärker (Typ 5, Audiosystem), der oft nur durchreicht
        devices.sort(key=lambda d: (d["type"] == 5, d["type"]))
        ports.append({"port": port, "id": input_id, "name": devices[0]["name"] if devices else "",
                      "state": STATE_TEXT.get(states.get(input_id, 0), "connected")})
    current = None
    input_id = tokens.get(main) if main else None
    if input_id:
        by_id = {p["id"]: p["port"] for p in ports}
        port = by_id.get(input_id) or cec_input.get(input_id) or 0
        current = {"id": input_id if valid_input_id(input_id) else "", "port": port or None}
    return {"ports": ports, "current": current}


# ---------------------------------------------------------------- EDID
def cec_address(edid: bytes) -> tuple[int, int, int, int] | None:
    """CEC-Adresse (a, b, c, d) aus dem HDMI-Herstellerblock, None ohne Block bzw. bei 0x0000 oder 0xFFFF."""
    if not edid or len(edid) < 256 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    for n in range(1, len(edid) // 128):
        blk = edid[n * 128:(n + 1) * 128]
        if blk[0] != 0x02 or sum(blk) % 256:            # nur CTA-861 mit stimmender Prüfsumme
            continue
        end = blk[2] if 4 <= blk[2] <= 127 else 4
        i = 4
        while i < end:
            tag, length = blk[i] >> 5, blk[i] & 31
            p = blk[i + 1:i + 1 + length]
            i += 1 + length
            if len(p) < length:
                break
            if tag == 3 and length >= 5 and (p[0] | (p[1] << 8) | (p[2] << 16)) == OUI_HDMI:
                addr = (p[3] >> 4, p[3] & 15, p[4] >> 4, p[4] & 15)
                if addr in ((0, 0, 0, 0), (15, 15, 15, 15)):
                    return None
                return addr
    return None


def pc_port_from_edid(edid: bytes) -> int | None:
    addr = cec_address(edid)
    if addr is None or not 1 <= addr[0] <= MAX_PORT:
        return None
    return addr[0]
