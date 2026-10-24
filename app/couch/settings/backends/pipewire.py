"""PipeWire lesen: Ausgänge, Eingänge, Standardgeräte, HDMI-Anschlüsse der Grafikkarte.

Quelle ist `pw-dump` (rund 20 ms, echtes UTF-8). `pactl -f json` ersetzt Texte mit Umlauten durch
"(null)" und ist deshalb für Namen unbrauchbar (05 Abschnitt 0).
"""
from __future__ import annotations

import json
import re

from ... import APP_ID

EMPTY = {"default_sink": None, "default_source": None, "configured_sink": None,
         "sinks": [], "sources": [], "hdmi_ports": [], "error": ""}

# USB-Audio der Controller (DualSense, DualShock 4): nie anbieten, nie von selbst wählen
_PAD_NAME = re.compile(r"wireless controller|dualsense|dualshock", re.I)
_SONY = "0x054c"


def _kv(flat) -> dict:
    """pw-dump liefert Routen-Infos als [n, k1, v1, k2, v2, …]."""
    if not isinstance(flat, list):
        return {}
    return dict(zip(flat[1::2], flat[2::2]))


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _list(v) -> list:
    return v if isinstance(v, list) else []


def _is_pad(node_props: dict, dev_props: dict) -> bool:
    vendor = str(dev_props.get("device.vendor.id") or node_props.get("device.vendor.id") or "").lower()
    if vendor == _SONY:
        return True
    for src in (node_props, dev_props):
        for key in ("alsa.card_name", "api.alsa.card.name", "device.description", "node.description"):
            if _PAD_NAME.search(str(src.get(key) or "")):
                return True
    return False


def _level(channel_volumes) -> float:
    """Lautstärke wie wpctl/pactl sie zeigen: dritte Wurzel der linearen Verstärkung."""
    vals = [v for v in _list(channel_volumes) if isinstance(v, (int, float)) and v >= 0]
    if not vals:
        return 1.0
    return round(max(vals) ** (1 / 3), 3)


def parse_pw_dump(text: str) -> dict:
    """Macht aus der Ausgabe von pw-dump den Ton-Zustand. Kaputte Teile werden übersprungen;
    ist das Ganze kein JSON, kommt der leere Zustand mit "error" zurück (nie eine Ausnahme)."""
    try:
        objs = json.loads(text)
    except (ValueError, TypeError) as e:
        return {**EMPTY, "sinks": [], "sources": [], "hdmi_ports": [], "error": f"pw-dump unlesbar: {e}"}
    if not isinstance(objs, list):
        return {**EMPTY, "sinks": [], "sources": [], "hdmi_ports": [], "error": "pw-dump unlesbar"}
    objs = [o for o in objs if isinstance(o, dict) and isinstance(o.get("type"), str)]

    defaults: dict = {}
    for o in objs:
        if o["type"].endswith("Metadata") and _dict(o.get("props")).get("metadata.name") == "default":
            for m in _list(o.get("metadata")):
                if not isinstance(m, dict) or "key" not in m:
                    continue
                v = m.get("value")
                defaults[m["key"]] = v.get("name") if isinstance(v, dict) else v

    devices = {o.get("id"): o for o in objs if o["type"].endswith(":Device")}
    sinks, sources, own = [], [], []
    for o in objs:
        if not o["type"].endswith(":Node"):
            continue
        info = _dict(o.get("info"))
        p = _dict(info.get("props"))
        cls = p.get("media.class")
        if cls == "Stream/Output/Audio" and is_own_stream(p):
            pr = _dict((_list(_dict(info.get("params")).get("Props")) or [{}])[0])
            own.append({"id": o.get("id"), "serial": str(p.get("object.serial") or ""),
                        "target": p.get("target.object") or "", "volume": _level(pr.get("channelVolumes")),
                        "mute": bool(pr.get("mute"))})
            continue
        if cls not in ("Audio/Sink", "Audio/Source") or not p.get("node.name"):
            continue
        pr = _dict((_list(_dict(info.get("params")).get("Props")) or [{}])[0])
        dev = _dict(devices.get(p.get("device.id")))
        dinfo = _dict(dev.get("info"))
        dprops = _dict(dinfo.get("props"))
        # aktive Route dieses Knotens (Port-Name und bei HDMI der Bildschirmname)
        route = next((r for r in _list(_dict(dinfo.get("params")).get("Route"))
                      if isinstance(r, dict) and r.get("device") == p.get("card.profile.device")), {})
        rinfo = _kv(route.get("info"))
        entry = {
            "id": o.get("id"), "name": p["node.name"],
            "description": p.get("node.description") or "",
            "nick": p.get("node.nick") or "",
            "card": dprops.get("device.name"), "bus": p.get("device.bus"),
            "port": route.get("name"), "port_description": route.get("description") or "",
            "product_name": rinfo.get("device.product.name"),
            "is_hdmi": rinfo.get("port.type") == "hdmi" or ".hdmi-" in p["node.name"],
            "volume": _level(pr.get("channelVolumes")), "mute": bool(pr.get("mute")),
            "controller": _is_pad(p, dprops),
        }
        key = "default.audio.sink" if cls == "Audio/Sink" else "default.audio.source"
        entry["is_default"] = entry["name"] == defaults.get(key)
        entry["label"] = _label(entry)
        (sinks if cls == "Audio/Sink" else sources).append(entry)

    hdmi = []
    for d in devices.values():
        dinfo = _dict(d.get("info"))
        params = _dict(dinfo.get("params"))
        card = _dict(dinfo.get("props")).get("device.name") or ""
        profiles = {p.get("index"): p for p in _list(params.get("EnumProfile")) if isinstance(p, dict)}
        active = _dict((_list(params.get("Profile")) or [{}])[0]).get("name")
        for r in _list(params.get("EnumRoute")):
            if not isinstance(r, dict):
                continue
            info = _kv(r.get("info"))
            if info.get("port.type") != "hdmi" or not r.get("name"):
                continue
            names = [profiles[i].get("name") for i in _list(r.get("profiles"))
                     if i in profiles and isinstance(profiles[i].get("name"), str)]
            stereo = next((n for n in names if n.startswith("output:") and "stereo" in n), None)
            hdmi.append({
                "card": card, "port": r["name"], "available": r.get("available") or "unknown",
                "product_name": info.get("device.product.name"),
                "profiles": names, "active_profile": active, "stereo_profile": stereo,
                # so heißt die Senke, sobald das Stereo-Profil dieses Ports aktiv ist
                "stereo_sink": (card.replace("alsa_card.", "alsa_output.", 1) + "." + stereo.split(":", 1)[1])
                if stereo and card else None,
            })
    sinks.sort(key=lambda s: (not s["is_default"], s["label"].casefold()))
    sources.sort(key=lambda s: (not s["is_default"], s["label"].casefold()))
    return {"default_sink": defaults.get("default.audio.sink"),
            "default_source": defaults.get("default.audio.source"),
            "configured_sink": defaults.get("default.configured.audio.sink"),
            "sinks": sinks, "sources": sources, "hdmi_ports": hdmi, "own_streams": own, "error": ""}


# E104: die App-Kennung aus couch.APP_ID (eigene Tonströme)
OWN_APP_IDS = (APP_ID,)
OWN_NAMES = ("PC5 Launcher", "couch")         # "couch" bis 2026-10-19


def is_own_stream(props: dict) -> bool:
    """Tonstrom der Oberfläche der Konsole (WebKit-Seiten von Home und Overlay)."""
    return (props.get("pipewire.access.portal.app_id") in OWN_APP_IDS
            or (props.get("application.name") in OWN_NAMES
                and props.get("application.process.binary") == "WebKitWebProcess"))


def _label(entry: dict) -> str:
    """Name für die Liste: bei HDMI der Bildschirm, sonst die Beschreibung des Knotens."""
    if entry.get("is_hdmi"):
        monitor = entry.get("product_name") or entry.get("nick")
        if monitor:
            return str(monitor)
    return str(entry.get("description") or entry.get("nick") or entry.get("name") or "Unbekanntes Gerät")


def read_state(run_read) -> dict:
    """Blockierend (rund 20 ms). Liefert immer einen Zustand; Fehler stehen in "error"."""
    rc, out, err = run_read(["pw-dump"], timeout=5)
    if rc != 0:
        return {**EMPTY, "sinks": [], "sources": [], "hdmi_ports": [],
                "error": (err or "pw-dump fehlgeschlagen").strip()}
    return parse_pw_dump(out)


def find_monitor_port(state: dict, monitor_name: str) -> dict | None:
    """HDMI-Port, an dem der Bildschirm mit diesem EDID-Namen hängt. None, solange der Ausgang
    dunkel ist: Ohne Bild gibt es keine ELD, der Port ist "no" und trägt keinen Namen."""
    for p in state.get("hdmi_ports", []):
        if p.get("product_name") == monitor_name and p.get("available") != "no" and p.get("stereo_sink"):
            return p
    return None


def sink_by_name(state: dict, name: str | None) -> dict | None:
    return next((s for s in state.get("sinks", []) if s["name"] == name), None)


def source_by_name(state: dict, name: str | None) -> dict | None:
    return next((s for s in state.get("sources", []) if s["name"] == name), None)
