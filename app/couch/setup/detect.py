"""Was auf dem Rechner schon da ist (E105, Bauplan 8.3; setup.detect). Nur lesend, blockierend
(mountinfo, statvfs, ein paar Dateien): im Arbeits-Thread rufen.

Ergebnis von detect():
  {system: {id, label, variant, supported, note}, user, steam: {present, flatpak},
   internet: {state}, flatpak: {present, userRemote, gi}, umu: {present, where: "system"|"own"|""},
   proton: {present, default}, places: [{id, label, path, base, free, total, fs, ok, reason}],
   emudeck: {present, emulationPath} | null, installed: {<flatpak-id>: "system"|"user"}, lutris: bool}

Im Prüfmodus kennt detect nur Orte im Käfig (Interner Speicher = <COUCH_TEST_ROOT>/home/Games und die
Spiele-Platte der Prüf-Welt, wenn sie im Käfig liegt): So legt kein Test je einen Ordner auf den
echten Platten des Nutzers an.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any

from .. import paths
from . import catalog, folders

GIB = 1024 ** 3
MIN_FREE = 20 * GIB                       # darunter ist ein Ort für Spiele gesperrt
# Dateisysteme ohne Unix-Rechte: Windows-Spiele (Prefixe, Verweise) gehen dort nicht
_NO_UNIX = {"exfat": "exFAT", "vfat": "FAT", "msdos": "FAT", "fat": "FAT", "ntfs": "NTFS", "ntfs3": "NTFS",
            "fuseblk": "NTFS", "fuse.ntfs-3g": "NTFS", "fuse.lowntfs-3g": "NTFS"}
_TYPELIB_DIRS = ("/usr/lib64/girepository-1.0", "/usr/lib/girepository-1.0",
                 "/usr/lib/x86_64-linux-gnu/girepository-1.0")
_OS_NAMES = {"bazzite": "Bazzite (Fedora Atomic)", "steamos": "SteamOS", "fedora": "Fedora", "arch": "Arch Linux",
             "ubuntu": "Ubuntu", "debian": "Debian", "chimeraos": "ChimeraOS", "nobara": "Nobara"}
SUPPORTED = ("bazzite", "steamos", "chimeraos", "nobara")


def _unescape(field: str) -> str:
    """mountinfo schreibt Leerzeichen und Sonderzeichen als \\040 usw."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def read_mounts(path: str = "/proc/self/mountinfo") -> list:
    """[{target, fstype, source, ro}] aus mountinfo."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(4 << 20).splitlines()
    except OSError:
        return out
    for line in lines:
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields, rest = left.split(), right.split()
        if len(fields) < 6 or len(rest) < 2:
            continue
        opts = fields[5].split(",")
        out.append({"target": _unescape(fields[4]), "fstype": rest[0], "source": _unescape(rest[1]),
                    "ro": "ro" in opts or "ro" in (rest[2].split(",") if len(rest) > 2 else [])})
    return out


def _space(path: str) -> tuple:
    try:
        st = os.statvfs(path)
    except OSError:
        return 0, 0
    return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize


def _gb(n: int) -> str:
    return ("%.0f GB" % (n / 1e9)) if n >= 10e9 else ("%.1f GB" % (n / 1e9)).replace(".", ",")


def min_free() -> int:
    """Grenze „zu wenig Platz“; nur im Prüfmodus über COUCH_SETUP_MIN_FREE_GB änderbar (der Scratch ist klein)."""
    raw = os.environ.get("COUCH_SETUP_MIN_FREE_GB") if paths.test_mode() else None
    try:
        return int(float(raw) * GIB) if raw is not None else MIN_FREE
    except ValueError:
        return MIN_FREE


def _judge(place: dict) -> dict:
    fs = str(place.get("fs") or "")
    if fs in _NO_UNIX:
        place.update(ok=False, reason="Diese Platte ist mit %s formatiert. Windows-Spiele brauchen ext4 oder btrfs; "
                                      "für Emulator-Spiele geht sie." % _NO_UNIX[fs])
    elif place.get("ro"):
        place.update(ok=False, reason="Nur lesbar")
    elif place.get("free", 0) < min_free():
        place.update(ok=False, reason="Zu wenig Platz (unter 20 GB frei)")
    else:
        place.update(ok=True, reason="")
    place["detail"] = "%s frei von %s" % (_gb(place.get("free", 0)), _gb(place.get("total", 0)))
    return place


def _fs_of(path: str, mounts: list) -> tuple:
    """(fstype, ro) des Dateisystems, auf dem path liegt (längster passender Einhängepunkt)."""
    real = os.path.realpath(path)
    best = None
    for m in mounts:
        t = m["target"]
        if real == t or real.startswith(t.rstrip("/") + "/"):
            if best is None or len(t) > len(best["target"]):
                best = m
    return (best["fstype"], best["ro"]) if best else ("", False)


def places(user_home: str, user: str, mounts: list | None = None, media_roots=None, extra=()) -> list:
    """Orte für Spiele: Interner Speicher (~/Games) und eingehängte Platten unter /run/media/<nutzer>,
    /run/media (z. B. mmcblk0p1) und /media/<nutzer>. Nie selbst einhängen."""
    mounts = read_mounts() if mounts is None else mounts
    out = []
    free, total = _space(user_home)
    fs, ro = _fs_of(user_home, mounts)
    out.append(_judge({"id": "internal", "label": "Interner Speicher", "path": os.path.join(user_home, "Games"),
                       "base": user_home, "free": free, "total": total, "fs": fs, "ro": ro, "kind": "internal"}))
    roots = media_roots if media_roots is not None else (
        os.path.join("/run/media", user), "/run/media", os.path.join("/media", user))
    seen = set()
    for m in mounts:
        target = m["target"]
        parent = os.path.dirname(target.rstrip("/"))
        if parent not in roots or target in seen or target in roots:
            continue
        if m["fstype"].startswith("fuse.") and m["fstype"] not in _NO_UNIX:
            continue                            # Portale, sshfs und Ähnliches sind kein Spiele-Ort
        seen.add(target)
        out.append(_place_of(target, m["fstype"], m["ro"], m["source"]))
    for path in extra:
        if path and path not in seen and os.path.isdir(path):
            fs, ro = _fs_of(path, mounts)
            seen.add(path)
            out.append(_place_of(path, fs, ro, ""))
    return out


def _place_of(target: str, fstype: str, ro: bool, source: str) -> dict:
    name = os.path.basename(target.rstrip("/"))
    sd = "mmcblk" in source or name.startswith("mmcblk")
    label = ("microSD-Karte „%s“" if sd else "Platte „%s“") % name
    free, total = _space(target)
    return _judge({"id": "mount:" + target, "label": label, "path": target, "base": target, "free": free,
                   "total": total, "fs": fstype, "ro": ro, "kind": "sd" if sd else "disk"})


def test_places(user_home: str, games_disk: str | None) -> list:
    """Prüfmodus: nur Orte im Käfig (realpath unter COUCH_TEST_ROOT)."""
    root = os.path.realpath(paths.test_root() or "/nonexistent")
    out = []
    for path, place_id, label, base in ((os.path.join(user_home, "Games"), "internal", "Interner Speicher", user_home),
                                        (games_disk or "", "mount:" + (games_disk or ""), "Platte „Spiele“",
                                         games_disk or "")):
        if not path or not base:
            continue
        real = os.path.realpath(base)
        if not (real == root or real.startswith(root + os.sep)) or not os.path.isdir(base):
            continue
        free, total = _space(base)
        out.append(_judge({"id": place_id, "label": label, "path": path, "base": base, "free": max(free, 0),
                           "total": total, "fs": "ext4", "ro": False, "kind": "internal" if place_id == "internal"
                           else "disk"}))
    return out


def emudeck(home: str, srm_dir: str | None = None) -> dict | None:
    """{present, emulationPath} oder None (kein EmuDeck). Ort wie folders.emudeck_root."""
    root = folders.emudeck_root(home, srm_dir)
    if not root and not os.path.isfile(os.path.join(home, ".config/EmuDeck/settings.sh")):
        return None
    return {"present": bool(root), "emulationPath": root}


def installation_of(root: str) -> str:
    return "system" if os.path.realpath(root) == "/var/lib/flatpak" or root.endswith("flatpak-system") else "user"


def installed_flatpaks(dirs, ids=None) -> dict:
    """{flatpak-id: "system"|"user"} der Katalog-Apps (Muster library/apps.py: current/active)."""
    out: dict = {}
    for app_id in ids if ids is not None else catalog.flatpak_ids():
        for root in dirs:
            if os.path.isdir(os.path.join(root, "app", app_id, "current", "active")):
                out.setdefault(app_id, installation_of(root))
    return out


def user_remote(user_flatpak: str) -> bool:
    """Gibt es die Nutzer-Quelle flathub? (repo/config der Nutzer-Installation, nur gelesen)"""
    try:
        with open(os.path.join(user_flatpak, "repo", "config"), encoding="utf-8", errors="replace") as fh:
            return '[remote "flathub"]' in fh.read(256 * 1024)
    except OSError:
        return False


def flatpak_gi() -> bool:
    return any(os.path.isfile(os.path.join(d, "Flatpak-1.0.typelib")) for d in _TYPELIB_DIRS)


def os_release(root: str = "/") -> dict:
    out: dict = {}
    for name in ("etc/os-release", "usr/lib/os-release"):
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    k, sep, v = line.strip().partition("=")
                    if sep:
                        out[k] = v.strip().strip("\"'")
            break
        except OSError:
            continue
    return out


def system_info(sys_root: str | None = None) -> dict:
    """{id, label, variant, supported, note, version, device} aus sysdetect (Teil P2), sonst aus os-release.
    sys_root None: wie sysdetect (PC5_SYSROOT, sonst /)."""
    info: dict = {}
    label = ""
    try:
        from .. import sysdetect
        info = sysdetect.detect(sys_root) or {}
        label = str(sysdetect.label(info) or "")
    except Exception:  # noqa: BLE001  (fehlt oder scheitert: os-release genügt)
        info = {}
    osi = info.get("os") if isinstance(info.get("os"), dict) else {}
    rel = os_release(sys_root or "/") if not osi else {}
    os_id = str(osi.get("id") or rel.get("ID") or "other")
    version = str(osi.get("version") or rel.get("VERSION_ID") or "")
    device = str(info.get("device") or "")
    if not label:
        name = _OS_NAMES.get(os_id, str(osi.get("name") or rel.get("NAME") or "Linux"))
        label = ("SteamOS %s" % ".".join(version.split(".")[:2])).strip() if os_id == "steamos" else name
        dev = {"steam-machine": "Steam Machine", "steam-deck": "Steam Deck"}.get(device, "")
        label += (" · " + dev) if dev else ""
    session = info.get("session") if isinstance(info.get("session"), dict) else {}
    supported = os_id in SUPPORTED or session.get("kind") in ("gsp", "steamos")
    note = "" if supported else "nicht offiziell unterstützt"
    if note:
        label = "%s – %s" % (label, note)
    return {"id": os_id, "label": label, "variant": str(osi.get("variant") or ""), "supported": supported,
            "note": note, "version": version, "device": device}


def detect(env: dict) -> dict:
    """env: home (Quellen-Home), user_home (Ort für ~/Games), user, games_disk, steam_root, srm_dir,
    flatpak_dirs, user_flatpak, network ({state}), proton (summary oder None), games_proton, test,
    mounts (optional), sys_root."""
    home = env["home"]
    dirs = list(env.get("flatpak_dirs") or [])
    installed = installed_flatpaks(dirs)
    umu = paths.tool("umu-run")
    own = os.path.join(paths.data_dir(), "tools", "umu")
    where = "" if not umu else ("own" if os.path.realpath(umu).startswith(os.path.realpath(own)) or
                                umu.startswith(own + os.sep) else "system")
    if env.get("test"):
        place_list = test_places(env["user_home"], env.get("games_disk"))
    else:
        place_list = places(env["user_home"], env.get("user") or paths.user_name(), env.get("mounts"))
    proton = env.get("proton") or {}
    steam_root = env.get("steam_root") or os.path.join(home, ".local/share/Steam")
    flatpak_present = bool(env.get("flatpak_present", shutil.which("flatpak") or flatpak_gi()))
    network = env.get("network") or {}
    return {
        "system": system_info(env.get("sys_root")),
        "user": env.get("user") or paths.user_name(),
        "steam": {"present": os.path.isfile(os.path.join(steam_root, "steam.sh")),
                  "flatpak": os.path.isdir(os.path.join(home, ".var/app/com.valvesoftware.Steam"))},
        "internet": {"state": str(network.get("state") or "unknown")},
        "flatpak": {"present": flatpak_present, "userRemote": user_remote(env.get("user_flatpak") or
                                                                          os.path.join(home, ".local/share/flatpak")),
                    "gi": flatpak_gi()},
        "umu": {"present": bool(umu), "where": where},
        "proton": {"present": int(proton.get("installed") or 0) > 0,
                   "default": str(env.get("games_proton") or "") or "proton-cachyos"},
        "places": place_list,
        "emudeck": emudeck(home, env.get("srm_dir")),
        "installed": installed,
        "lutris": os.path.isfile(os.path.join(home, ".local/share/lutris/pga.db")) or bool(shutil.which("lutris")
                                                                                         and not env.get("test")),
    }
