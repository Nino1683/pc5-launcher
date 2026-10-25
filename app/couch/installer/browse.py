"""Dateiauswahl für den Archiv-Installer (nur lesend).

Gezeigt werden nur feste Wurzeln, die der Aufrufer übergibt: im Produkt paths.install_roots
(Downloads, Dokumente, Bilder, Videos, Musik nach XDG, ~ selbst und jede bereits eingehängte
Platte, E99/E104), sonst default_roots (Downloads, Dokumente, eingehängte Platten).
Es wird nichts eingehängt. Aufstieg nur bis zur Wurzel; Symlinks, die aus den Wurzeln
hinausführen, werden nicht verfolgt; versteckte Einträge bleiben aus.

Sparsam mit der Platte (Befund 21: NTFS über FUSE nie durchsuchen): Es wird immer nur der eine
Ordner gelistet, nie rekursiv; stat nur für Archive (Größe, Datum); der Blick in den Dateikopf nur
bei Dateien ohne aussagekräftige Endung und nie auf langsamen Einhängungen.

list_dir() blockiert (Platte): gehört in einen Thread.
"""
from __future__ import annotations

import os
import re

from . import InstallError
from . import probe, setupexe, trash

ENTRY_LIMIT = 500        # so viele Einträge gehen höchstens an die Oberfläche
SCAN_LIMIT = 5000        # so viele Verzeichniseinträge werden höchstens gelesen
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar")
# Formate, die als Archiv gezeigt werden, damit probe() erklären kann, warum sie nicht gehen
OTHER_ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.zst", ".tar.bz2")
# unfertige Downloads sehen am Dateikopf wie Archive aus
PARTIAL_SUFFIXES = (".part", ".partial", ".crdownload", ".fdmdownload", ".download", ".!qb", ".tmp", ".aria2")
SLOW_FSTYPES = ("fuse", "ntfs", "exfat", "vfat", "msdos", "cifs", "smb", "nfs", "9p", "sshfs", "mtp")


class BrowseError(InstallError):
    """Ordner nicht anzeigbar. message ist ein deutscher Satz."""


def default_roots(home: str | None = None, media_dir: str | None = None, mounts=None) -> list:
    """Die Wurzeln der Dateiauswahl: [{"name", "path", "kind": "downloads"|"documents"|"disk"}]. Nur lesend.

    Downloads und Dokumente nach XDG (E104, user-dirs.dirs, sonst die deutschen bzw. englischen Namen);
    beschriftet bleiben sie deutsch. Platten zählen nur, wenn sie wirklich eingehängt sind (Abgleich mit
    /proc/self/mountinfo); ohne media_dir alle Orte aus paths.media_roots (auch /run/media/<name> und
    /media/<nutzer>)."""
    from .. import paths
    roots = []
    for label, kind, xdg in (("Downloads", "downloads", "DOWNLOAD"), ("Dokumente", "documents", "DOCUMENTS")):
        path = paths.user_dir_at(xdg, home) if home else paths.user_dir(xdg)
        if os.path.isdir(path):
            roots.append({"name": label, "path": os.path.realpath(path), "kind": kind})
    try:
        mounted = set((mounts or trash.read_mounts)())
        if media_dir:
            names = sorted(os.listdir(media_dir), key=str.casefold)
            disks = [(name, os.path.join(media_dir, name)) for name in names if not name.startswith(".")]
        else:
            disks = [(os.path.basename(p), p) for p in paths.media_roots(ismount=lambda p: True)]
    except OSError:
        return roots
    for name, raw in disks:
        path = os.path.realpath(raw)
        if path in mounted and all(r["path"] != path for r in roots):
            roots.append({"name": name, "path": path, "kind": "disk"})
    return roots


def _root_of(real: str, roots: list) -> dict | None:
    """Die Wurzel, unter der real liegt (die längste passende), oder None."""
    best = None
    for root in roots:
        base = root["path"]
        if (real == base or real.startswith(base.rstrip("/") + "/")) and (best is None or len(base) > len(best["path"])):
            best = root
    return best


def _slow_filesystem(real: str, mountinfo: str = "/proc/self/mountinfo") -> bool:
    """Liegt real auf FUSE, NTFS, einem Netzlaufwerk oder Ähnlichem? Dann nur das Nötigste lesen."""
    best, fstype = "", ""
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                left, _, right = line.partition(" - ")
                fields = left.split(" ")
                if len(fields) < 5:
                    continue
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
                if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
                    best, fstype = point, right.split(" ")[0]
    except OSError:
        return True
    return fstype.lower().startswith(SLOW_FSTYPES)


def archive_type_by_name(name: str) -> str | None:
    """"archive", "other" oder None (= die Endung sagt nichts, der Dateikopf entscheidet)."""
    low = name.lower()
    if low.endswith(PARTIAL_SUFFIXES):
        return "other"
    if low.endswith(ARCHIVE_SUFFIXES) or low.endswith(OTHER_ARCHIVE_SUFFIXES):
        return "archive"
    if any(rx.match(name) for rx, _kind in probe.VOLUME_PATTERNS):
        return "archive"
    ext = os.path.splitext(low)[1]
    if ext == "" or ext[1:].isdigit():
        return None
    return "other"


def list_dir(path: str | None = None, *, roots: list | None = None, limit: int = ENTRY_LIMIT) -> dict:
    """Listet einen Ordner der Dateiauswahl. Blockierend, nur lesend, nie rekursiv.

    path=None liefert die Wurzeln. Rückgabe:
      {"path", "parent", "name", "entries": [{"name", "path", "type": "dir"|"archive"|"installer"|"other", "size"}],
       "truncated": bool, "total": int, "message": str}
    "installer" (E114): setup.exe bzw. .msi, mit Größe und Datum wie ein Archiv.
    parent ist None an einer Wurzel (von dort geht es nur zurück zur Liste der Wurzeln).
    size steht nur bei Archiven und Installern (bei mehrteiligen die Summe; dazu "parts"), sonst None.
    Mehrteilige Archive erscheinen als ein Eintrag (ihr erster Teil).
    Wirft BrowseError mit deutschem Text, wenn der Ordner außerhalb der Wurzeln liegt oder nicht lesbar ist."""
    roots = default_roots() if roots is None else roots
    if path is None or path == "":
        entries = [{"name": r["name"], "path": r["path"], "type": "dir", "size": None, "kind": r.get("kind", "disk")}
                   for r in roots]
        return {"path": None, "parent": None, "name": "", "entries": entries, "truncated": False,
                "total": len(entries), "message": ""}
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise BrowseError("bad-path", "Dieser Ordner kann nicht angezeigt werden.")
    real = os.path.realpath(path)
    root = _root_of(real, roots)
    if root is None:
        raise BrowseError("outside-roots", "Dieser Ordner liegt außerhalb der erlaubten Orte.")
    inside = real[len(root["path"].rstrip("/")):].strip("/")
    if any(part.startswith(".") for part in inside.split("/") if part):
        raise BrowseError("hidden", "Versteckte Ordner werden nicht angezeigt.")
    if not os.path.isdir(real):
        raise BrowseError("not-a-dir", "Dieser Ordner ist nicht vorhanden.")
    slow = _slow_filesystem(real)

    # 1. Durchgang: nur Namen und Art aus dem Verzeichnis selbst (kein stat je Eintrag)
    dirs, files, scanned, truncated = [], [], 0, False
    try:
        with os.scandir(real) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                scanned += 1
                if scanned > SCAN_LIMIT:
                    truncated = True
                    break
                try:
                    if entry.is_symlink():
                        target = os.path.realpath(entry.path)
                        if _root_of(target, roots) is None:
                            files.append((entry.name, "other"))      # führt hinaus: zeigen, nicht verfolgen
                        elif os.path.isdir(target):
                            dirs.append(entry.name)
                        else:
                            files.append((entry.name, None))
                    elif entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append((entry.name, None))
                    else:
                        files.append((entry.name, "other"))          # FIFO, Socket, Gerät
                except OSError:
                    continue
    except OSError:
        raise BrowseError("unreadable", "Dieser Ordner kann nicht gelesen werden.")

    # Mehrteilige Archive: nur der erste vorhandene Teil bleibt als Eintrag
    names = [name for name, _forced in files]
    old_style = any(re.search(r"(?i)\.[rz]\d{2}$", name) for name in names)   # name.r00 / name.z01 vorhanden?
    volume_parts, hidden_parts = {}, set()
    for name, forced in files:
        if forced == "other" or name in hidden_parts or name in volume_parts:
            continue
        numbered = any(rx.match(name) for rx, _kind in probe.VOLUME_PATTERNS)
        if not numbered and not (old_style and name.lower().endswith((".rar", ".zip"))):
            continue
        vols = probe.volume_set(os.path.join(real, name), names)
        if vols["kind"] != "single" and len(vols["parts"]) > 1:
            parts = [os.path.basename(p) for p in vols["parts"]]
            lead = os.path.basename(vols["first"]) if vols["first"] else parts[0]
            volume_parts[lead] = parts
            hidden_parts.update(part for part in parts if part != lead)
    shown = [(name, forced) for name, forced in files if name not in hidden_parts]

    dirs.sort(key=str.casefold)
    shown.sort(key=lambda item: item[0].casefold())
    total = len(dirs) + len(shown)
    if total > limit:
        truncated = True
    dirs = dirs[:limit]
    shown = shown[:max(0, limit - len(dirs))]

    # 2. Durchgang: nur für die gezeigten Dateien Art bestimmen; stat nur für Archive und Installer
    entries = [{"name": name, "path": os.path.join(real, name), "type": "dir", "size": None} for name in dirs]
    budget = None if slow else setupexe.Budget()     # Blick in den Dateikopf nur für wenige .exe, nie auf langsamen Platten
    for name, forced in shown:
        full = os.path.join(real, name)
        kind = forced or archive_type_by_name(name)
        if kind is None:
            kind = "archive" if (not slow and probe.sniff(full, deep=False) in probe.SUPPORTED_FORMATS) else "other"
        item = {"name": name, "path": full, "type": kind, "size": None}
        if kind == "other" and not forced and name.lower().endswith((".exe", ".msi")):
            # E114: Installationsprogramme (erkannt, oder der Name sagt es: dann erklärt die Prüfung)
            try:
                st = os.stat(full)
            except OSError:
                st = None
            if st is not None and (setupexe.listing_flag(full, name, st.st_size, budget) or setupexe.is_setup_name(name)):
                item.update(type="installer", size=st.st_size, mtime=st.st_mtime)
        if kind == "archive":
            parts = volume_parts.get(name, [name])
            size, mtime = 0, None
            for part in parts:
                try:
                    st = os.stat(os.path.join(real, part))
                except OSError:
                    continue
                size += st.st_size
                mtime = st.st_mtime if mtime is None else max(mtime, st.st_mtime)
            item.update(size=size, mtime=mtime)
            if len(parts) > 1:
                item["parts"] = len(parts)
        entries.append(item)

    parent = None if real == root["path"] else os.path.dirname(real)
    message = "Es werden nur die ersten %d Einträge angezeigt." % len(entries) if truncated else ""
    return {"path": real, "parent": parent, "name": root["name"] if parent is None else os.path.basename(real),
            "entries": entries, "truncated": truncated, "total": total, "message": message, "slow": slow}


def is_allowed_archive(path: str, roots: list | None = None) -> bool:
    """Liegt die Datei (aufgelöst) unter einer der Wurzeln und nicht in einem versteckten Ordner? Nur lesend."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    roots = default_roots() if roots is None else roots
    real = os.path.realpath(path)
    root = _root_of(real, roots)
    if root is None or not os.path.isfile(real):
        return False
    inside = real[len(root["path"].rstrip("/")):].strip("/")
    return not any(part.startswith(".") for part in inside.split("/") if part)
