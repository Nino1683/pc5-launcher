"""NetworkManager: Lesen über `nmcli -t` (durch run_read), Verbinden mit Kennwort über libnm.

Das Kennwort eines WLANs darf nie auf einer Befehlszeile stehen (für jeden Prozess sichtbar);
deshalb geht genau dieser eine Schreibweg nicht durch run_write, sondern über D-Bus (libnm) und
ist im Prüfmodus abgeschaltet. Alles andere Schreibende sind nmcli-Befehle, die der Bereich
network durch run_write schickt.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("couch.settings.nm")

# C.UTF-8: Zustandswörter englisch (parsebar), Namen bleiben UTF-8. Mit LC_ALL=C würde glib
# Umlaute in Verbindungsnamen durch "?" ersetzen.
NMCLI = ["env", "LC_ALL=C.UTF-8", "nmcli"]


def split_terse(line: str) -> list[str]:
    """Zeile aus `nmcli -t` (Tabellenform) zerlegen: Trenner ist ":", in Werten als "\\:" maskiert."""
    out, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def parse_general(text: str) -> dict:
    """`nmcli -t -f STATE,CONNECTIVITY,WIFI-HW,WIFI general status`"""
    line = next((l for l in text.splitlines() if l.strip()), "")
    f = split_terse(line) + ["", "", "", ""]
    return {"state": f[0], "connectivity": f[1] or "unknown",
            "wifi_hw": f[2] in ("enabled", "yes"), "wifi": f[3] in ("enabled", "yes")}


def parse_device_show(text: str) -> list[dict]:
    """`nmcli -t -f GENERAL.DEVICE,…,IP4.DNS device show`: Blöcke je Gerät, "FELD:Wert" je Zeile.
    In dieser Form maskiert nmcli nichts; alles nach dem ersten Doppelpunkt ist der Wert."""
    devices, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur.get("device"):
                devices.append(cur)
            cur = {}
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        val = val.strip()
        base = key.split("[", 1)[0]
        if base == "GENERAL.DEVICE":
            cur["device"] = val
        elif base == "GENERAL.TYPE":
            cur["type"] = val
        elif base == "GENERAL.HWADDR":
            cur["hwaddr"] = val
        elif base == "GENERAL.STATE":
            # "100 (connected)" / "20 (unavailable)"
            num, _, word = val.partition(" ")
            cur["state_code"] = int(num) if num.isdigit() else 0
            cur["state"] = word.strip("()") if word else val
        elif base == "GENERAL.CONNECTION":
            cur["connection"] = val
        elif base == "CAPABILITIES.SPEED":
            cur["speed"] = "" if val in ("unknown", "--") else val
        elif base == "IP4.ADDRESS":
            cur.setdefault("ip4", []).append(val)
        elif base == "IP4.GATEWAY":
            cur["gateway"] = "" if val == "--" else val
        elif base == "IP4.DNS":
            cur.setdefault("dns", []).append(val)
    for d in devices:
        d.setdefault("type", "")
        d.setdefault("hwaddr", "")
        d.setdefault("state", "")
        d.setdefault("state_code", 0)
        d.setdefault("connection", "")
        d.setdefault("speed", "")
        d.setdefault("ip4", [])
        d.setdefault("gateway", "")
        d.setdefault("dns", [])
        d["connected"] = d["state_code"] == 100 and "externally" not in d["state"]
    return devices


def parse_connections(text: str) -> list[dict]:
    """`nmcli -t -f NAME,UUID,TYPE connection show` -> [{name, uuid, type}]"""
    out = []
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) >= 3 and f[1]:
            out.append({"name": f[0], "uuid": f[1], "type": f[2]})
    return out


def parse_wifi_list(text: str) -> list[dict]:
    """`nmcli -t -f IN-USE,BSSID,SSID,CHAN,SIGNAL,SECURITY device wifi list --rescan no`.
    Je SSID nur der stärkste Zugangspunkt; versteckte Netze (leere SSID) entfallen."""
    best: dict[str, dict] = {}
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 6 or not f[2]:
            continue
        try:
            signal = int(f[4])
        except ValueError:
            signal = 0
        sec = "" if f[5].strip() in ("", "--") else f[5].strip()
        ap = {"in_use": f[0].strip() == "*", "bssid": f[1], "ssid": f[2], "signal": signal,
              "security": sec, "secured": bool(sec), "enterprise": "802.1X" in sec}
        old = best.get(ap["ssid"])
        if old is None or ap["in_use"] or (signal > old["signal"] and not old["in_use"]):
            best[ap["ssid"]] = ap
    return sorted(best.values(), key=lambda a: (not a["in_use"], -a["signal"], a["ssid"].casefold()))


def read_state(run_read) -> dict:
    """Zustand für die Hauptseite (zwei nmcli-Aufrufe, zusammen rund 30 ms). Blockierend."""
    rc, out, err = run_read(NMCLI + ["-t", "-f", "STATE,CONNECTIVITY,WIFI-HW,WIFI", "general", "status"], timeout=5)
    if rc != 0:
        return {"error": (err or "nmcli fehlgeschlagen").strip(), "state": "", "connectivity": "unknown",
                "wifi_hw": False, "wifi": False, "devices": [], "primary": None}
    st = parse_general(out)
    rc, out, _ = run_read(NMCLI + ["-t", "-f", "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.HWADDR,GENERAL.STATE,"
                                               "GENERAL.CONNECTION,CAPABILITIES.SPEED,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                                   "device", "show"], timeout=5)
    devs = [d for d in (parse_device_show(out) if rc == 0 else []) if d["type"] in ("ethernet", "wifi")]
    st["devices"] = devs
    # Kabel vor WLAN, wie NetworkManager die Standardroute wählt
    st["primary"] = next((d for d in devs if d["connected"] and d["type"] == "ethernet"), None) \
        or next((d for d in devs if d["connected"]), None)
    st["wifi_device"] = next((d["device"] for d in devs if d["type"] == "wifi"), None)
    st["error"] = ""
    return st


def read_saved_wifi(run_read) -> list[dict]:
    rc, out, _ = run_read(NMCLI + ["-t", "-f", "NAME,UUID,TYPE", "connection", "show"], timeout=5)
    return [c for c in parse_connections(out) if c["type"] == "802-11-wireless"] if rc == 0 else []


def read_wifi_list(run_read) -> list[dict]:
    rc, out, _ = run_read(NMCLI + ["-t", "-f", "IN-USE,BSSID,SSID,CHAN,SIGNAL,SECURITY",
                                   "device", "wifi", "list", "--rescan", "no"], timeout=8)
    return parse_wifi_list(out) if rc == 0 else []


def check_connectivity(run_read) -> str:
    """Lässt NetworkManager die Erreichbarkeit neu prüfen: full | limited | portal | none | unknown.
    Verändert nichts; blockiert bis zu einigen Sekunden."""
    rc, out, _ = run_read(NMCLI + ["-t", "networking", "connectivity", "check"], timeout=15)
    word = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else "unknown"
    return word if word in ("full", "limited", "portal", "none") else "unknown"


# --- Schreibweg mit Kennwort (libnm) -------------------------------------------------------------

def key_mgmt_for(security: str) -> str | None:
    """Schlüsselverwaltung aus der SECURITY-Spalte von nmcli. None = offenes Netz."""
    sec = security.upper()
    if not sec.strip() or sec.strip() == "--":
        return None
    if "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec:
        return "sae"
    return "wpa-psk"


def build_wifi_connection(ssid: str, password: str | None, security: str = "WPA2"):
    """Verbindungsprofil für ein WLAN bauen (rein, ohne D-Bus). Liefert NM.SimpleConnection."""
    import gi
    gi.require_version("NM", "1.0")
    from gi.repository import GLib, NM
    conn = NM.SimpleConnection.new()
    s_con = NM.SettingConnection.new()
    s_con.set_property(NM.SETTING_CONNECTION_ID, ssid)
    s_con.set_property(NM.SETTING_CONNECTION_TYPE, "802-11-wireless")
    s_con.set_property(NM.SETTING_CONNECTION_UUID, NM.utils_uuid_generate())
    conn.add_setting(s_con)
    s_wifi = NM.SettingWireless.new()
    s_wifi.set_property(NM.SETTING_WIRELESS_SSID, GLib.Bytes.new(ssid.encode("utf-8")))
    conn.add_setting(s_wifi)
    mgmt = key_mgmt_for(security)
    if mgmt and password:
        s_sec = NM.SettingWirelessSecurity.new()
        s_sec.set_property(NM.SETTING_WIRELESS_SECURITY_KEY_MGMT, mgmt)
        s_sec.set_property(NM.SETTING_WIRELESS_SECURITY_PSK, password)
        conn.add_setting(s_sec)
    return conn


def wifi_connect(ssid: str, password: str | None, security: str = "WPA2", timeout: float = 45.0,
                 client_factory=None) -> tuple[bool, str]:
    """SCHREIBT: neues WLAN verbinden, Kennwort nur über D-Bus. Blockierend bis timeout.

    Läuft in einem eigenen GLib-Kontext, braucht also keine Hauptschleife und gehört in einen
    Thread. Schlägt die Verbindung fehl (falsches Kennwort), wird das eben angelegte Profil
    wieder gelöscht, damit kein kaputtes „gemerktes Netz“ zurückbleibt. Liefert (ok, deutscher
    Satz). Am 2026-10-13 nur gegen Attrappen geprüft: Das WLAN des Entwicklungsrechners ist aus.
    client_factory: nur für Tests (Attrappe statt NM.Client).
    """
    import gi
    gi.require_version("NM", "1.0")
    from gi.repository import GLib, NM

    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        client = client_factory() if client_factory else NM.Client.new(None)
        dev = next((d for d in client.get_devices() if d.get_device_type() == NM.DeviceType.WIFI), None)
        if dev is None:
            return False, "Kein WLAN-Adapter gefunden."
        ap = None
        for a in dev.get_access_points():
            raw = a.get_ssid()
            if raw and NM.utils_ssid_to_utf8(raw.get_data()) == ssid and (ap is None or a.get_strength() > ap.get_strength()):
                ap = a
        conn = build_wifi_connection(ssid, password, security)
        box: dict = {}

        def added(c, res):
            try:
                box["ac"] = c.add_and_activate_connection_finish(res)
            except GLib.Error as e:
                box["error"] = e.message

        client.add_and_activate_connection_async(conn, dev, ap.get_path() if ap else None, None, added)
        tick = GLib.timeout_source_new(200)       # weckt iteration(), damit die Frist greift
        tick.set_callback(lambda *_: True)
        tick.attach(ctx)
        deadline = time.monotonic() + timeout
        ok, message = False, "Zeit abgelaufen. Das Netz antwortet nicht."
        try:
            while time.monotonic() < deadline:
                ctx.iteration(True)
                if "error" in box:
                    message = "Verbindung fehlgeschlagen."
                    log.warning("WLAN %r: %s", ssid, box["error"])
                    break
                ac = box.get("ac")
                if ac is None:
                    continue
                state = ac.get_state()
                if state == NM.ActiveConnectionState.ACTIVATED:
                    ok, message = True, ""
                    break
                if state == NM.ActiveConnectionState.DEACTIVATED:
                    message = "Verbindung fehlgeschlagen. Stimmt das Kennwort?"
                    break
            if not ok and box.get("ac") is not None:
                _delete_profile(ctx, box["ac"])
        finally:
            tick.destroy()
        return ok, message
    except Exception as e:  # noqa: BLE001 - nie eine Ausnahme bis in die Oberfläche
        log.exception("WLAN verbinden")
        return False, f"Verbindung fehlgeschlagen ({type(e).__name__})."
    finally:
        ctx.pop_thread_default()


def _delete_profile(ctx, active) -> None:
    remote = active.get_connection()
    if remote is None:
        return
    done = {}
    remote.delete_async(None, lambda r, res: done.setdefault("x", True))
    end = time.monotonic() + 3
    while "x" not in done and time.monotonic() < end:
        ctx.iteration(True)
