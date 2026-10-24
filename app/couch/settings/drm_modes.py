#!/usr/bin/python3
"""Bildschirm-Modi und HDR-/VRR-Fähigkeit eines DRM-Anschlusses lesen (nur lesen).

Als Modul (alles blockierend, etwa 25 ms; nie in der Hauptschleife rufen):

    from couch.settings import drm_modes
    name = drm_modes.tv_connector()        # E104: Anschluss des Fernsehers (Sitzung, sonst /sys/class/drm)
    s = drm_modes.summary(name)            # wirft nie; {"present": False, ...} wenn nichts da ist
    s["texts"]["mode"]                     # "3840 × 2160, 60 Hz"

Als Werkzeug:

    python3 drm_modes.py                 # Anschluss nach tv_connector(), JSON
    python3 drm_modes.py DP-1
    python3 drm_modes.py HDMI-A-1 --all  # auch 4:3-/Kinomodi in "choices"
    python3 drm_modes.py HDMI-A-1 --raw  # zusätzlich die ganze Modusliste des Kernels

Zwei Quellen, beide ohne Root, ohne DRM-Master und ohne Unterprozess:
  1. Kernel: DRM_IOCTL_MODE_GETRESOURCES/GETCONNECTOR/GETPROPERTY/GETENCODER/GETCRTC. Das sind
     reine Abfragen (genaue Bildrate, Eigenschaften des Anschlusses, laufender Modus).
  2. EDID aus /sys/class/drm/<card>-<name>/edid, hier selbst zerlegt (Name, HDR, VRR-Bereich).

Warum nicht /sys/.../modes: dort stehen nur Namen ("3840x2160"), keine Bildraten.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import json
import os
import struct
import sys


# _IOWR('d', nr, size)
def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("d") << 8) | nr


CONNECTOR_TYPES = {0: "Unknown", 1: "VGA", 2: "DVI-I", 3: "DVI-D", 4: "DVI-A", 5: "Composite",
                   6: "SVIDEO", 7: "LVDS", 8: "Component", 9: "DIN", 10: "DP", 11: "HDMI-A",
                   12: "HDMI-B", 13: "TV", 14: "eDP", 15: "Virtual", 16: "DSI", 17: "DPI",
                   18: "Writeback", 19: "SPI", 20: "USB"}
MODE_FLAG_INTERLACE = 1 << 4
MODE_FLAG_DBLSCAN = 1 << 5
MODE_TYPE_PREFERRED = 1 << 3
MODEINFO = struct.Struct("=I5H5HIII32s")          # struct drm_mode_modeinfo, 68 Bytes
CARD_RES = struct.Struct("=4Q8I")                 # struct drm_mode_card_res, 64 Bytes
GET_CONN = struct.Struct("=4Q12I")                # struct drm_mode_get_connector, 80 Bytes
GET_PROP = struct.Struct("=QQII32sII")            # struct drm_mode_get_property, 64 Bytes
GET_ENC = struct.Struct("=5I")                    # struct drm_mode_get_encoder, 20 Bytes
GET_CRTC = struct.Struct("=Q7I")                  # struct drm_mode_crtc ohne den Modus am Ende
IOCTL_GETRESOURCES = _iowr(0xA0, CARD_RES.size)
IOCTL_GETCRTC = _iowr(0xA1, GET_CRTC.size + MODEINFO.size)
IOCTL_GETENCODER = _iowr(0xA6, GET_ENC.size)
IOCTL_GETCONNECTOR = _iowr(0xA7, GET_CONN.size)
IOCTL_GETPROPERTY = _iowr(0xAA, GET_PROP.size)

SYS_DRM = "/sys/class/drm"
DEV_DRI = "/dev/dri"


def _ioctl(fd: int, req: int, buf: bytearray) -> bytearray:
    fcntl.ioctl(fd, req, buf, True)
    return buf


def _mode(raw, offset: int = 0) -> dict:
    (clock, hd, _hss, _hse, ht, _hskew, vd, _vss, _vse, vt, _vscan, _vrefresh, flags, typ,
     name) = MODEINFO.unpack_from(raw, offset)
    hz = clock * 1000.0 / (ht * vt) if ht and vt else 0.0
    if flags & MODE_FLAG_INTERLACE:
        hz *= 2
    if flags & MODE_FLAG_DBLSCAN:
        hz /= 2
    return {"width": hd, "height": vd, "refresh": round(hz, 3), "clock_khz": clock,
            "interlaced": bool(flags & MODE_FLAG_INTERLACE),
            "preferred": bool(typ & MODE_TYPE_PREFERRED),
            "name": name.split(b"\0")[0].decode("ascii", "replace")}


def _current_mode(fd: int, encoder_id: int) -> dict | None:
    """Der Modus, der gerade über diesen Anschluss läuft (Anschluss -> Encoder -> CRTC)."""
    if not encoder_id:
        return None
    try:
        eb = bytearray(GET_ENC.size)
        GET_ENC.pack_into(eb, 0, encoder_id, 0, 0, 0, 0)
        _ioctl(fd, IOCTL_GETENCODER, eb)
        crtc_id = GET_ENC.unpack_from(eb)[2]
        if not crtc_id:
            return None
        cb = bytearray(GET_CRTC.size + MODEINFO.size)
        GET_CRTC.pack_into(cb, 0, 0, 0, crtc_id, 0, 0, 0, 0, 0)
        _ioctl(fd, IOCTL_GETCRTC, cb)
        if not GET_CRTC.unpack_from(cb)[7]:          # mode_valid
            return None
        m = _mode(cb, GET_CRTC.size)
        return {"width": m["width"], "height": m["height"], "refresh": m["refresh"]}
    except OSError:
        return None


def _connector(fd: int, conn_id: int) -> dict:
    """Liest einen Anschluss: Typ, Zustand, Modi, Eigenschaften, laufender Modus."""
    # Nie mit count_modes == 0 fragen: Das wäre für einen DRM-Master der Auftrag, den Anschluss
    # neu abzutasten. Wir sind nie Master (Gamescope bzw. KWin ist es), aber so bleibt der
    # Aufruf auch dann eine reine Abfrage, wenn gerade niemand Master ist.
    n_modes, n_props, n_enc = 1, 0, 0
    buf = bytearray(GET_CONN.size)
    for _ in range(4):                               # die Liste kann sich zwischen zwei Aufrufen ändern
        modes = (ctypes.c_char * (MODEINFO.size * max(n_modes, 1)))()
        props = (ctypes.c_uint32 * max(n_props, 1))()
        vals = (ctypes.c_uint64 * max(n_props, 1))()
        encs = (ctypes.c_uint32 * max(n_enc, 1))()
        GET_CONN.pack_into(buf, 0, ctypes.addressof(encs), ctypes.addressof(modes),
                           ctypes.addressof(props), ctypes.addressof(vals),
                           max(n_modes, 1), n_props, n_enc, 0, conn_id, 0, 0, 0, 0, 0, 0, 0)
        _ioctl(fd, IOCTL_GETCONNECTOR, buf)
        f = GET_CONN.unpack_from(buf)
        if f[4] <= max(n_modes, 1) and f[5] <= n_props and f[6] <= n_enc:
            break
        n_modes, n_props, n_enc = f[4], f[5], f[6]
    else:
        raise OSError("Modusliste ändert sich laufend")
    out_modes = [_mode(modes, i * MODEINFO.size) for i in range(f[4])]
    prop_map = {}
    for i in range(f[5]):
        pb = bytearray(GET_PROP.size)
        GET_PROP.pack_into(pb, 0, 0, 0, props[i], 0, b"", 0, 0)
        try:
            _ioctl(fd, IOCTL_GETPROPERTY, pb)
        except OSError:
            continue
        pname = GET_PROP.unpack_from(pb)[4].split(b"\0")[0].decode("ascii", "replace")
        prop_map[pname] = int(vals[i])
    return {"type": CONNECTOR_TYPES.get(f[9], str(f[9])), "type_id": f[10],
            "connected": f[11] == 1, "mm": [f[12], f[13]], "modes": out_modes, "props": prop_map,
            "current": _current_mode(fd, f[7])}


# E104: Reihenfolge, wenn die Sitzung keinen Anschluss nennt: HDMI vor DP vor den eingebauten (eDP)
_PREFER = ("HDMI-A", "HDMI-B", "DP", "DVI-D", "DVI-I", "DVI-A", "VGA")
_INTERNAL = ("eDP", "LVDS", "DSI")


def connector_states(sys_drm: str = SYS_DRM) -> dict:
    """{"HDMI-A-1": "connected", "DP-3": "disconnected", …} aus /sys/class/drm/card*-*/status (nur lesen;
    der Kernel liefert den gemerkten Zustand, es wird nichts neu abgetastet). Schreib-Ausgänge fehlen."""
    out: dict = {}
    for path in sorted(glob.glob(os.path.join(sys_drm, "card[0-9]*-*"))):
        base = os.path.basename(path)
        name = base.split("-", 1)[1] if "-" in base else ""
        if not name or name.startswith("Writeback"):
            continue
        try:
            with open(os.path.join(path, "status"), encoding="ascii", errors="replace") as f:
                state = f.read(64).strip()
        except OSError:
            continue
        if out.get(name) != "connected":          # mehrere Karten: verbunden gewinnt
            out[name] = state
    return out


def _rank(name: str) -> tuple:
    kind = name.rsplit("-", 1)[0] if name.rsplit("-", 1)[-1].isdigit() else name
    if kind in _PREFER:
        return (0, _PREFER.index(kind), name)
    if kind in _INTERNAL:
        return (2, _INTERNAL.index(kind), name)
    return (1, 0, name)


def tv_connector(env: dict | None = None, sys_drm: str = SYS_DRM) -> str:
    """Anschluss des Fernsehers (E104): zuerst OUTPUT_CONNECTOR der Sitzung (Gamescopes --prefer-output,
    z. B. "HDMI-A-1,*"; ein genannter Anschluss gilt, wenn es ihn gibt, auch wenn der Fernseher gerade aus
    ist; "*" = der erste verbundene), sonst der erste verbundene Ausgang aus /sys/class/drm (HDMI vor DP vor
    eDP), sonst "" (die Einstellungen sagen dann „Kein Fernseher erkannt“). Liest nur; einige kleine
    Dateien, schnell. env: Umgebung der Sitzung (Vorgabe os.environ)."""
    env = os.environ if env is None else env
    raw = str(env.get("OUTPUT_CONNECTOR", "") or "")
    states = connector_states(sys_drm)
    connected = sorted((n for n, st in states.items() if st == "connected"), key=_rank)
    for name in (part.strip() for part in raw.split(",")):
        if not name:
            continue
        if name == "*":
            if connected:
                return connected[0]
            continue
        if not states or name in states:          # ohne lesbares /sys gilt die Sitzung wie bisher
            return name
    return connected[0] if connected else ""


def find_card(sys_drm: str = SYS_DRM, dev_dri: str = DEV_DRI) -> str:
    """Die Karte mit Anschlüssen (hier card1; die Nummer ist nicht über Neustarts fest)."""
    for p in sorted(glob.glob(os.path.join(sys_drm, "card[0-9]*"))):
        base = os.path.basename(p)
        if "-" not in base and glob.glob(p + "-*"):
            return os.path.join(dev_dri, base)
    return os.path.join(dev_dri, "card0")


def list_connectors(card: str | None = None) -> dict:
    """Alle Anschlüsse einer Karte als {Name: Daten}. Blockierend. Wirft OSError."""
    # Nur lesend geöffnet; die Abfragen brauchen kein Schreibrecht.
    fd = os.open(card or find_card(), os.O_RDONLY | os.O_CLOEXEC)
    try:
        buf = bytearray(CARD_RES.size)
        _ioctl(fd, IOCTL_GETRESOURCES, buf)
        n_conn = CARD_RES.unpack_from(buf)[6]
        ids = (ctypes.c_uint32 * max(n_conn, 1))()
        # nur die Anschluss-IDs holen; die anderen Zähler auf 0 lassen
        CARD_RES.pack_into(buf, 0, 0, 0, ctypes.addressof(ids), 0, 0, 0, n_conn, 0, 0, 0, 0, 0)
        _ioctl(fd, IOCTL_GETRESOURCES, buf)
        n_conn = min(n_conn, CARD_RES.unpack_from(buf)[6])
        out = {}
        for i in range(n_conn):
            try:
                c = _connector(fd, ids[i])
            except OSError:
                continue
            out["%s-%d" % (c["type"], c["type_id"])] = c
        return out
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- EDID

OUI_HDMI_FORUM = 0xC45DD8
OUI_HDR10_PLUS = 0x90848B
OUI_DOLBY = 0x00D046


def read_edid(connector: str, card: str | None = None, sys_drm: str = SYS_DRM) -> bytes:
    """EDID eines Anschlusses aus sysfs. Leer, wenn nichts angeschlossen ist."""
    card = os.path.basename(card or find_card(sys_drm))
    try:
        with open(os.path.join(sys_drm, "%s-%s" % (card, connector), "edid"), "rb") as f:
            return f.read(4096)
    except OSError:
        return b""


def parse_edid(data: bytes) -> dict:
    """Name, HDR- und VRR-Angaben aus einer EDID. Leeres dict, wenn es keine gültige ist.

    Gelesen wird nur, was die Einstellungen zeigen: Grundblock (Hersteller, Name, Größe) und die
    CTA-861-Erweiterung (HDR-Metadaten, Farbraum, HDMI-Forum-Block). Blöcke mit anderer Kennung
    werden übersprungen; der HDMI-over-LAN-Adapter hängt zwei Blöcke Datenmüll an.
    """
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {}
    b = data
    info = {
        "manufacturer": "".join(chr(64 + c) for c in ((b[8] >> 2) & 31,
                                                         ((b[8] & 3) << 3) | (b[9] >> 5), b[9] & 31)),
        "product_name": None,
        "size_cm": [b[21] or None, b[22] or None],
        "hdr10": False, "hlg": False, "hdr10plus": False, "dolby_vision": False,
        "bt2020": False, "max_luminance_nits": None, "max_tmds_mhz": None,
        "vrr_min_hz": None, "vrr_max_hz": None, "allm": False,
    }
    for off in (54, 72, 90, 108):                    # vier Beschreibungsblöcke zu je 18 Bytes
        d = b[off:off + 18]
        if d[0] == 0 and d[1] == 0 and d[3] == 0xFC:
            info["product_name"] = d[5:18].split(b"\n")[0].decode("cp437", "replace").strip() or None
    # Alle vorhandenen Blöcke ansehen, nicht nur so viele, wie Byte 126 nennt: HDMI-Geräte
    # melden dort oft 1 und die wahre Zahl erst in der Erweiterung selbst.
    for n in range(1, len(b) // 128):
        blk = b[n * 128:(n + 1) * 128]
        if blk[0] != 0x02 or sum(blk) % 256:         # nur CTA-861 mit stimmender Prüfsumme
            continue
        end = blk[2] if 4 <= blk[2] <= 127 else 4
        i = 4
        while i < end:
            tag, length = blk[i] >> 5, blk[i] & 31
            p = blk[i + 1:i + 1 + length]
            i += 1 + length
            if len(p) < length:
                break
            if tag == 3 and length >= 3:             # herstellerspezifisch
                if p[0] | (p[1] << 8) | (p[2] << 16) == OUI_HDMI_FORUM:
                    if length >= 5 and p[4]:
                        info["max_tmds_mhz"] = p[4] * 5
                    if length >= 8:
                        info["allm"] = bool(p[7] & 0x02)
                    if length >= 10:
                        vmin, vmax = p[8] & 0x3F, ((p[8] & 0xC0) << 2) | p[9]
                        info["vrr_min_hz"] = vmin or None
                        info["vrr_max_hz"] = vmax or None
            elif tag == 7 and length >= 2:           # erweiterte Kennung im ersten Byte
                ext = p[0]
                if ext == 0x06:                      # HDR Static Metadata
                    info["hdr10"] = bool(p[1] & 0x04)        # SMPTE ST 2084 (PQ)
                    info["hlg"] = bool(p[1] & 0x08)
                    if length >= 4 and p[3]:
                        info["max_luminance_nits"] = round(50.0 * 2 ** (p[3] / 32.0), 1)
                elif ext == 0x05:                    # Colorimetry
                    info["bt2020"] = bool(p[1] & 0x80)       # BT.2020 RGB, das verlangt Gamescope
                elif ext == 0x01 and length >= 4:    # herstellerspezifischer Videoblock
                    oui = p[1] | (p[2] << 8) | (p[3] << 16)
                    if oui == OUI_HDR10_PLUS:
                        info["hdr10plus"] = True
                    elif oui == OUI_DOLBY:
                        info["dolby_vision"] = True
    return info


# --------------------------------------------------------------------------- Auswahl und Texte

def ui_choices(modes: list, show_all: bool = False) -> list:
    """Aus der Kernel-Liste die Auswahl für ein Menü machen.

    Regeln: keine Interlaced-Modi, nur 16:9, Bildraten auf ganze Hz gerundet und
    zusammengefasst (59.94 und 60 sind für den Nutzer dasselbe), Kinoraten (24/25/30)
    nur mit show_all, weil das Menü bei 24 Hz ruckelt.
    """
    seen: dict = {}
    for md in modes:
        if md["interlaced"] or not md["height"]:
            continue
        w, h, hz = md["width"], md["height"], int(round(md["refresh"]))
        if not show_all:
            if abs(w / h - 16 / 9) > 0.01 or hz < 50 or h < 720:
                continue
        seen.setdefault((w, h), set()).add(hz)
    out = []
    for (w, h), rates in sorted(seen.items(), key=lambda kv: (-kv[0][0] * kv[0][1], -kv[0][0])):
        out.append({"width": w, "height": h, "label": "%s (%d × %d)" % (_short(w, h), w, h),
                    "refresh_rates": sorted(rates, reverse=True)})
    return out


def _short(w: int, h: int) -> str:
    return {2160: "4K", 1440: "1440p", 1080: "1080p", 720: "720p"}.get(h, "%dp" % h)


def mode_text(mode: dict | None) -> str:
    """{"width", "height", "refresh"} -> "3840 × 2160, 60 Hz"."""
    if not mode:
        return ""
    return "%d × %d, %d Hz" % (mode["width"], mode["height"], int(round(mode["refresh"])))


def summarize(connector: str, conn: dict | None, edid: dict | None) -> dict:
    """Zusammenfassung für die Info-Zeilen aus den Rohdaten (rein, ohne Zugriff aufs System)."""
    if conn is None:
        return {"connector": connector, "present": False, "connected": False, "name": "",
                "hdr": False, "vrr": False, "preferred": None, "current": None, "choices": [],
                "texts": {"name": "", "mode": "", "modes": "", "hdr": "", "vrr": ""}}
    edid = edid or {}
    props = conn.get("props", {})
    pref = next(({"width": m["width"], "height": m["height"], "refresh": m["refresh"]}
                 for m in conn["modes"] if m["preferred"]), None)
    choices = ui_choices(conn["modes"])
    maker, product = edid.get("manufacturer") or "", edid.get("product_name") or ""
    name = product if product.upper().startswith(maker.upper()) else ("%s %s" % (maker, product)).strip()
    # Dieselbe Regel wie Gamescope (01, 5.1): KMS-Eigenschaften und EDID mit BT.2020 RGB und PQ.
    hdr = bool("HDR_OUTPUT_METADATA" in props and "Colorspace" in props
               and edid.get("bt2020") and edid.get("hdr10"))
    vrr = props.get("vrr_capable") == 1
    formats = [label for key, label in (("hdr10", "HDR10"), ("hlg", "HLG"), ("hdr10plus", "HDR10+"),
                                        ("dolby_vision", "Dolby Vision")) if edid.get(key)]
    vmin, vmax = edid.get("vrr_min_hz"), edid.get("vrr_max_hz")
    if vrr and vmin and vmax:
        vrr_text = "%d–%d Hz" % (vmin, vmax)
    else:
        vrr_text = "Ja" if vrr else "Nein"
    return {
        "connector": connector, "present": True, "connected": bool(conn["connected"]),
        "name": name, "manufacturer": maker, "size_cm": edid.get("size_cm"),
        "preferred": pref, "current": conn.get("current"), "choices": choices,
        "hdr": hdr, "hdr_formats": formats, "bt2020": bool(edid.get("bt2020")),
        "max_luminance_nits": edid.get("max_luminance_nits"),
        "vrr": vrr, "vrr_range": [vmin, vmax] if vmin and vmax else None,
        "max_bpc": props.get("max bpc"),
        "texts": {
            "name": name,
            "mode": mode_text(conn.get("current") or pref),
            "modes": " · ".join("%s bis %d Hz" % (_short(c["width"], c["height"]), c["refresh_rates"][0])
                                for c in choices if c["height"] in (2160, 1440, 1080)),
            "hdr": ", ".join(formats) if hdr else "Nein",
            "vrr": vrr_text,
        },
    }


def read(connector: str | None = None, card: str | None = None) -> tuple[dict | None, dict, dict]:
    """(Anschluss, EDID-Angaben, alle Anschlüsse). Blockierend. Wirft OSError. connector None = tv_connector()."""
    connector = tv_connector() if connector is None else connector
    card = card or find_card()
    conns = list_connectors(card)
    c = conns.get(connector)
    edid = parse_edid(read_edid(connector, card)) if c is not None and c["connected"] else {}
    return c, edid, conns


def summary(connector: str | None = None, card: str | None = None) -> dict:
    """Zusammenfassung eines Anschlusses für die Einstellungen. Blockierend, wirft nie.
    connector None = tv_connector(); "" (kein Fernseher erkannt) liest nichts."""
    connector = tv_connector() if connector is None else connector
    if not connector:
        return summarize("", None, None)
    try:
        c, edid, _ = read(connector, card)
    except OSError as e:
        out = summarize(connector, None, None)
        out["error"] = str(e)
        return out
    return summarize(connector, c, edid)


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = [a for a in argv if not a.startswith("--")]
    name = args[0] if args else tv_connector()
    card = find_card()
    try:
        c, edid, conns = read(name, card)
    except OSError as e:
        print(json.dumps({"error": str(e), "card": card}))
        return 1
    if c is None:
        print(json.dumps({"error": "kein Anschluss " + name, "connectors": sorted(conns)}))
        return 1
    props = c["props"]
    out = {
        "card": card, "connector": name, "connected": c["connected"],
        "kernel_modes": len(c["modes"]),
        "preferred": next(({"width": x["width"], "height": x["height"],
                            "refresh": x["refresh"]} for x in c["modes"] if x["preferred"]), None),
        "current": c["current"],
        "choices": ui_choices(c["modes"], "--all" in argv),
        "edid": edid,
        # Eigenschaften, die der Treiber für diesen Anschluss anbietet
        "drm": {"vrr_capable": props.get("vrr_capable"),
                "has_hdr_output_metadata": "HDR_OUTPUT_METADATA" in props,
                "has_colorspace": "Colorspace" in props,
                "max_bpc": props.get("max bpc"),
                "props": sorted(props)},
        "summary": summarize(name, c, edid),
        "other_connectors": {k: v["connected"] for k, v in conns.items() if k != name},
    }
    if "--raw" in argv:
        out["modes"] = c["modes"]
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
