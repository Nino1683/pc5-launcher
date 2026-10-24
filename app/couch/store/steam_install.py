"""Steam-Spiele installieren, ohne Steam zu zeigen (Bauplan E68).

Steam liest beim Start die Einträge steamapps/appmanifest_<id>.acf seiner Bibliotheken. Ein
Eintrag mit StateFlags 1026 (Update nötig) für ein Spiel, das dir gehört, lässt Steam das Spiel
beim nächsten Start von selbst laden, ohne Fenster und ohne Rückfrage (am 2026-10-19 mit
„Proton BattlEye Runtime“ geprüft: Steam unsichtbar gestartet, nach 100 s fertig).
Deshalb: Einträge nur schreiben, solange KEIN Steam läuft (SAFETY.md: Steam-Dateien nur bei
beendetem Steam), danach Steam unsichtbar starten (couch-steam-game --download). Den Fortschritt
schreibt Steam selbst in den Eintrag (BytesDownloaded/BytesToDownload, dann Staged).
Abbrechen legt Eintrag, Teil-Download und angelegten Spielordner in den Papierkorb.
"""
from __future__ import annotations

import os
import re
import shutil

from ..library import steam as steam_lib

FULLY_INSTALLED = 4
UPDATE_REQUIRED = 2
UPDATE_STARTED = 1024
NEW_STATE = UPDATE_REQUIRED | UPDATE_STARTED            # 1026: Steam lädt beim nächsten Start
SKIP_FS = ("ntfs", "ntfs3", "fuseblk", "vfat", "exfat", "msdos")   # Steam-Spiele dort machen Ärger


def _fs_type(path: str) -> str:
    """Dateisystem des Einhängepunkts, zu dem path gehört (aus /proc/self/mounts)."""
    best, fs = "", ""
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    mnt = parts[1].replace("\\040", " ")
                    if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                        best, fs = mnt, parts[2]
    except OSError:
        pass
    return fs


def libraries(ctx) -> list[dict]:
    """Bibliotheken, in die installiert werden darf: [{path, free, fs}] (eingehängt, beschreibbar,
    kein NTFS/FAT)."""
    out = []
    for lib in steam_lib.library_folders(ctx):
        path = lib["path"]
        steamapps = os.path.join(path, "steamapps")
        if not lib["mounted"] or not os.access(steamapps, os.W_OK):
            continue
        fs = _fs_type(os.path.realpath(path))
        if fs in SKIP_FS:
            continue
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        out.append({"path": path, "free": free, "fs": fs})
    return out


def pick_library(libs: list[dict], wanted: str = "", games_disk: str = "") -> dict | None:
    """Gewählte Bibliothek, sonst die auf der Spiele-Platte, sonst die mit dem meisten Platz."""
    for lib in libs:
        if wanted and os.path.realpath(lib["path"]) == os.path.realpath(wanted):
            return lib
    if games_disk:
        for lib in libs:
            if lib["path"].startswith(games_disk.rstrip("/") + "/") or lib["path"] == games_disk:
                return lib
    return max(libs, key=lambda lib: lib["free"]) if libs else None


def manifests(ctx) -> dict:
    """{appid: {"lib", "path", "flags", "done", "total", "dl_done", "dl_total", "result", "installdir", "name"}}.
    done/total zählen Laden und Entpacken (Staging) zusammen, für Prozent und Stillstand; dl_done/dl_total
    nur das Laden, für Menge und Geschwindigkeit unter Downloads (E99: sonst „20 GB von 90 GB“ für 50 GB)."""
    out = {}
    for appid, (lib, state) in steam_lib._manifests(ctx, steam_lib.library_folders(ctx)).items():
        def num(key):
            return steam_lib.to_int(steam_lib.ci(state, key))
        total = num("BytesToDownload") + num("BytesToStage")
        done = num("BytesDownloaded") + num("BytesStaged")
        out[appid] = {"lib": lib["path"], "path": os.path.join(lib["path"], "steamapps", "appmanifest_%d.acf" % appid),
                      "flags": num("StateFlags"), "done": done, "total": total, "result": num("UpdateResult"),
                      "dl_done": num("BytesDownloaded"), "dl_total": num("BytesToDownload"),
                      "installdir": str(steam_lib.ci(state, "installdir", default="") or ""),
                      "name": str(steam_lib.ci(state, "name", default="") or "")}
    return out


def installed(info: dict | None) -> bool:
    """Fertig geladen, nichts mehr offen (Ende eines Downloads)."""
    return bool(info) and info["flags"] & FULLY_INSTALLED and not info["flags"] & (UPDATE_REQUIRED | UPDATE_STARTED)


def present(info: dict | None) -> bool:
    """Installiert und spielbar, auch mit offenem Update (StateFlags 6), wie die Bibliothek es sieht
    (library/steam.py: flags & 4). Der Store zeigt dann „Spielen“, nie „Installieren“ (E99)."""
    return bool(info) and bool(info["flags"] & FULLY_INSTALLED)


def update_pending(info: dict | None) -> bool:
    return present(info) and bool(info["flags"] & UPDATE_REQUIRED)


def install_dir_name(ctx, appid: int, name: str) -> str:
    """Ordnername unter steamapps/common: wie Steam ihn kennt (appinfo config/installdir), sonst
    aus dem Namen ohne Zeichen, die in Ordnernamen stören."""
    try:
        info = steam_lib.load_appinfo(os.path.join(ctx.steam_root, "appcache/appinfo.vdf"), only={appid})
        given = steam_lib.ci((info.get(appid) or {}).get("appinfo") or {}, "config", "installdir", default="")
    except Exception:
        given = ""
    given = str(given or "").strip()
    if given and "/" not in given and given not in (".", ".."):
        return given
    clean = re.sub(r"[^\w .,'()&+-]", "", name or "", flags=re.U).strip(" .")
    return clean or "app_%d" % appid


def manifest_text(appid: int, name: str, installdir: str, owner: int | None) -> str:
    def q(value) -> str:
        return '"%s"' % str(value).replace("\\", "").replace('"', "")
    rows = [("appid", appid), ("Universe", 1), ("name", name), ("StateFlags", NEW_STATE), ("installdir", installdir),
            ("LastUpdated", 0), ("SizeOnDisk", 0), ("buildid", 0)]
    if owner:
        rows.append(("LastOwner", owner))
    rows += [("BytesToDownload", 0), ("BytesDownloaded", 0), ("AutoUpdateBehavior", 0),
             ("AllowOtherDownloadsWhileRunning", 0), ("ScheduledAutoUpdate", 0)]
    return '"AppState"\n{\n' + "".join("\t%s\t\t%s\n" % (q(k), q(v)) for k, v in rows) + "}\n"


def write_manifest(lib_path: str, appid: int, name: str, installdir: str, owner: int | None = None) -> str:
    """Neuen Eintrag anlegen (nie überschreiben). NUR aufrufen, wenn kein Steam läuft."""
    path = os.path.join(lib_path, "steamapps", "appmanifest_%d.acf" % appid)
    if os.path.lexists(path):
        raise FileExistsError(path)
    tmp = path + ".couch-neu"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(manifest_text(appid, name, installdir, owner))
    os.link(tmp, path)          # schlägt fehl, falls es ihn inzwischen gibt; nie überschrieben
    os.unlink(tmp)
    return path


def leftovers(info: dict, appid: int, uninstall: bool = False) -> list[str]:
    """Was ein abgebrochener Download hinterlässt: Eintrag, steamapps/downloading/<id> und der
    Spielordner, solange das Spiel nie fertig installiert war. Ist es installiert (Bit 4, etwa mit
    offenem Update), bleiben Eintrag und Spielordner liegen, nur der Teil-Download geht (E99: sonst
    vergaßen Steam und Bibliothek ein installiertes Spiel). uninstall: auch Eintrag und Spielordner
    eines fertigen Spiels und sein Shader-Zwischenspeicher. compatdata (Proton-Prefix mit den
    Spielständen) bleibt immer liegen."""
    steamapps = os.path.join(info["lib"], "steamapps")
    keep_game = not uninstall and bool(info["flags"] & FULLY_INSTALLED)
    out = [] if keep_game else [info["path"]]
    extra = ["downloading", "temp"] + (["shadercache"] if uninstall else [])
    for path in (os.path.join(steamapps, name, str(appid)) for name in extra):
        if os.path.lexists(path):
            out.append(path)
    if info["installdir"] and "/" not in info["installdir"] and info["installdir"] not in (".", ".."):
        game = os.path.join(steamapps, "common", info["installdir"])
        if os.path.lexists(game) and not keep_game:
            out.append(game)
    return out


def owned_games(ctx, owned: set) -> list[tuple[int, str]]:
    """[(appid, name)] der Spiele (keine DLC, Werkzeuge, Soundtracks) unter den eigenen Apps, nach
    Namen; Namen aus Steams appinfo-Zwischenspeicher (dort stehen alle eigenen Apps)."""
    if not owned:
        return []
    try:
        info = steam_lib.load_appinfo(os.path.join(ctx.steam_root, "appcache/appinfo.vdf"), only=set(owned))
    except Exception:
        return []
    out = []
    for appid, rec in info.items():
        app = rec.get("appinfo") or {}
        if str(steam_lib.ci(app, "common", "type", default="")).lower() != "game":
            continue
        name = steam_lib.clean_text(steam_lib.ci(app, "common", "name", default=""), "")
        if name:
            out.append((int(appid), name))
    return sorted(out, key=lambda t: t[1].casefold())


def app_name(ctx, appid: int) -> str:
    """Name aus Steams appinfo-Zwischenspeicher ("" ohne): für eigene Spiele, die der Shop nicht
    mehr führt (E99)."""
    try:
        info = steam_lib.load_appinfo(os.path.join(ctx.steam_root, "appcache/appinfo.vdf"), only={appid})
    except Exception:
        return ""
    app = (info.get(appid) or {}).get("appinfo") or {}
    return steam_lib.clean_text(steam_lib.ci(app, "common", "name", default=""), "")


def percent(info: dict) -> int:
    if not info or info["total"] <= 0:
        return 0
    return max(0, min(100, int(info["done"] * 100 / info["total"])))
