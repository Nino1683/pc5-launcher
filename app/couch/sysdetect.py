"""Erkennung des Systems (E104, Bauplan 4.7) für den Installer und den Kern. Nur Standardbibliothek, liest nur.

  detect(root=None) -> dict

root (Vorgabe: Umgebung PC5_SYSROOT, sonst "/") stellt alle Systempfade auf eine Attrappe um
(tests/fixtures/sysroot/<system>). Nutzer und HOME kommen immer aus der Umgebung.

  {"os": {"id": "bazzite"|"steamos"|"fedora"|"arch"|"ubuntu"|"debian"|"other", "name", "version",
          "variant": "deck"|"desktop"|"", "atomic": bool},
   "device": "steam-machine"|"steam-deck"|"",
   "session": {"kind": "gsp"|"steamos"|"none", "clients": [...], "unit_ok": bool},
   "cpu": {"x86_64_v3": bool}, "user": "<name>", "home": "<pfad>"}

Sitzungsarten (Bauplan 2.1): gsp = gamescope-session-plus liest ~/.config/gamescope-session-plus/sessions.d/
<client> (Bazzite, ChimeraOS, Nobara …); steamos = Valves gamescope-session.target hält
steam-launcher.service am Laufen (SteamOS 3; laut CachyOS-Kopie von Valves Sitzung, unbestätigt am Gerät);
none = keine Spielmodus-Sitzung. Als Skript: python3 -I sysdetect.py [--json].
"""
import json
import os
import pwd
import re
import sys

KNOWN_IDS = ("bazzite", "steamos", "fedora", "arch", "ubuntu", "debian")
LIKE_ORDER = ("fedora", "arch", "ubuntu", "debian")       # ID_LIKE: ubuntu vor debian (Mint: "ubuntu debian")
CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GSP_SCRIPTS = ("usr/share/gamescope-session-plus/gamescope-session-plus",
               "usr/lib/gamescope-session-plus/gamescope-session-plus")
VALVE_UNIT = "usr/lib/systemd/user/steam-launcher.service"
VALVE_TARGET = "usr/lib/systemd/user/gamescope-session.target"
VALVE_LAUNCHER = "/usr/lib/steamos/steam-launcher"
# DMI-Produktnamen (unbestätigt): Jupiter = Steam Deck LCD, Galileo = Steam Deck OLED, Fremont = Steam Machine
DECKS = ("jupiter", "galileo")
MACHINES = ("fremont",)
# x86-64-v3 wie proton_repair.cpu_arch (dieselben Flags)
V3_FLAGS = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}
READ_LIMIT = 256 * 1024


def _root(root):
    root = root or os.environ.get("PC5_SYSROOT") or "/"
    return os.path.realpath(root)


def _path(root, rel):
    return os.path.join(root, rel.lstrip("/"))


def _read(path, limit=READ_LIMIT):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _keyvals(text):
    """KEY=wert-Zeilen wie in os-release (Anführungszeichen weg, keine Ersetzungen)."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def os_info(root):
    data = _keyvals(_read(_path(root, "etc/os-release")) or _read(_path(root, "usr/lib/os-release")))
    raw = data.get("ID", "").lower()
    like = data.get("ID_LIKE", "").lower().split()
    if raw in KNOWN_IDS:
        os_id = raw
    else:
        os_id = next((name for name in LIKE_ORDER if name in like), "other")
    variant_id = data.get("VARIANT_ID", "").lower()
    variant = "deck" if "deck" in variant_id else ("desktop" if variant_id else "")
    atomic = (os.path.exists(_path(root, "run/ostree-booted")) or raw == "steamos"
              or os.path.exists(_path(root, "usr/bin/steamos-readonly")))
    return {"id": os_id, "name": data.get("NAME") or data.get("PRETTY_NAME") or raw or "Linux",
            "version": data.get("VERSION_ID", ""), "variant": variant, "atomic": bool(atomic)}


def device(root):
    product = _read(_path(root, "sys/class/dmi/id/product_name"), 256).strip().lower()
    if product in DECKS:
        return "steam-deck"
    if product in MACHINES:
        return "steam-machine"
    return ""


def _desktop_exec(text):
    for line in text.splitlines():
        if line.startswith("Exec="):
            return line[5:].strip()
    return ""


def gsp_clients(root):
    """Clients der Sitzung: zuerst der aus der Anmeldung (sddm), dann alle aus wayland-sessions."""
    sessions = {}
    folder = _path(root, "usr/share/wayland-sessions")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".desktop"):
            continue
        words = _desktop_exec(_read(os.path.join(folder, name))).split()
        if len(words) >= 2 and os.path.basename(words[0]) == "gamescope-session-plus" and CLIENT_NAME.match(words[1]):
            sessions[name] = words[1]
    chosen = ""
    files = [_path(root, "etc/sddm.conf")]
    conf_d = _path(root, "etc/sddm.conf.d")
    try:
        files += [os.path.join(conf_d, n) for n in sorted(os.listdir(conf_d)) if n.endswith(".conf")]
    except OSError:
        pass
    for path in files:                      # sddm liest conf.d sortiert, Späteres gilt
        for line in _read(path).splitlines():
            line = line.strip()
            if line.startswith("Session="):
                chosen = line[8:].strip()
    out = []
    default = sessions.get(chosen)
    if not default:
        m = re.fullmatch(r"gamescope-session-(.+)\.desktop", chosen)
        if m and CLIENT_NAME.match(m.group(1)) and sessions:
            default = m.group(1)
    if default:
        out.append(default)
    for client in sessions.values():
        if client not in out:
            out.append(client)
    return out


def valve_unit_ok(root):
    """Sieht steam-launcher.service so aus, wie die Ergänzungsdatei es erwartet (ExecStart = Valves Launcher)?"""
    text = _read(_path(root, VALVE_UNIT))
    starts = [line.split("=", 1)[1].strip() for line in text.splitlines() if line.strip().startswith("ExecStart=")]
    return bool(starts) and starts[-1].split()[0] == VALVE_LAUNCHER


def session(root, os_id):
    gsp = next((rel for rel in GSP_SCRIPTS if os.path.isfile(_path(root, rel))), None)
    valve = os.path.isfile(_path(root, VALVE_UNIT)) and os.path.isfile(_path(root, VALVE_TARGET))
    if valve and (os_id == "steamos" or not gsp):
        return {"kind": "steamos", "clients": [], "unit_ok": valve_unit_ok(root)}
    if gsp:
        clients = gsp_clients(root)
        # liest das Skript die eigenen sessions.d des Nutzers? (gamescope-session-plus:10, 117-121)
        reads_local = "sessions.d" in _read(_path(root, gsp))
        return {"kind": "gsp", "clients": clients, "unit_ok": bool(clients) and reads_local}
    return {"kind": "none", "clients": [], "unit_ok": False}


def cpu(root):
    flags = set()
    for line in _read(_path(root, "proc/cpuinfo")).splitlines():
        if line.startswith("flags"):
            flags = set(line.split(":", 1)[1].split()) if ":" in line else set()
            break
    return {"x86_64_v3": V3_FLAGS <= flags}


def user_name():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER", "")


def detect(root=None):
    root = _root(root)
    info = os_info(root)
    return {"os": info, "device": device(root), "session": session(root, info["id"]), "cpu": cpu(root),
            "user": os.environ.get("USER") or user_name(), "home": os.path.expanduser("~")}


def label(info):
    """Kurze Beschreibung für Meldungen: „SteamOS 3.8 · Steam Machine“, „Bazzite (Fedora Atomic)“."""
    osi = info.get("os", {})
    text = osi.get("name") or "Linux"
    version = osi.get("version", "")
    if osi.get("id") == "steamos" and version:
        text += " " + ".".join(version.split(".")[:2])
    if osi.get("id") == "bazzite":
        text += " (Fedora Atomic)"
    dev = {"steam-machine": "Steam Machine", "steam-deck": "Steam Deck"}.get(info.get("device", ""))
    return text + (" · " + dev if dev else "")


if __name__ == "__main__":
    result = detect()
    if "--json" in sys.argv[1:]:
        print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(label(result))
        print("Sitzung: %s %s" % (result["session"]["kind"], " ".join(result["session"]["clients"])))
