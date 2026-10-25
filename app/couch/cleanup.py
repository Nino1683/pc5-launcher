"""Platz schaffen (E110, Teil P13): Suchlauf, was Platz belegt und sich gefahrlos freigeben lässt.

scan(ctx) liest nur. Hier gibt es keine Löschstelle (SAFETY 13): Die Oberfläche (screens/cleanup.js) ruft je
Eintrag einen vorhandenen Weg, und das nur nach einer Rückfrage mit Vorgabe Abbrechen:

    action   Weg                                   Gruppe
    purge    install.purge {trashId: ref}          console-trash  Papierkorb der Konsole (installer/trash.py)
    empty    files.op {op: "empty"}                system-trash   Papierkorb des Systems (xdgtrash, E81)
    trash    files.op {op: "trash", paths: ref}    archives       in den Papierkorb des Systems, wiederherstellbar
    open     Datei-Explorer im Ordner ref          downloads      nichts wird geändert
    show     nur anzeigen                          prefixes, shadercache

Ergebnis: {groups: [{id, title, size, count, more, items, note, action}], total_reclaimable, complete, free}.
Je Eintrag {id, title, path, size, age_days, action, ref, note}. size in Bytes (belegter Platz, st_blocks),
None = nicht gemessen. total_reclaimable zählt, was diese Seite selbst frei machen kann (beide Papierkörbe,
Archive). complete ist False, wenn das Zeitbudget (20 s) nicht reichte. free: [{id, label, path, free, total}].

Grenzen des Suchlaufs: nie in FUSE, NTFS oder Netzlaufwerke (storage._UNSAFE_FS, auch kein Auflisten),
keinen Verweisen folgen, Größen nur auf dem Dateisystem des Ordners (st_dev: keine Einhängung darin).
Läuft im Arbeits-Thread (wiring_cleanup).
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .settings.storage import fstype_of, steam_libraries, unsafe_fs

BUDGET_S = 20.0                     # ganzer Suchlauf (Bauplan 9.5)
QUICK_S = 2.0                       # davon zurückgehalten für die Liste des Konsolen-Papierkorbs ohne Größen
MIN_DOWNLOAD = 500 * 1000 ** 2      # große Downloads: ab 500 MB …
MIN_DOWNLOAD_DAYS = 7               # … und älter als eine Woche
LIST_MAX = 20                       # Einträge je Gruppe, der Rest zählt nur mit („more“)
SHADER_MAX = 10                     # Steams Shader-Zwischenspeicher: die zehn größten
DAY = 86400
JOBS_FILE = "install-jobs.json"
ACTIVE_JOBS = ("queued", "running", "finishing", "installing", "needs-choice")
RECLAIMABLE = ("console-trash", "system-trash", "archives")
TRASH_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")

TITLES = {
    "console-trash": "Papierkorb der Konsole",
    "system-trash": "Papierkorb des Systems",
    "archives": "Archive schon installierter Spiele",
    "downloads": "Große Downloads",
    "prefixes": "Prefixe ohne Spiel",
    "shadercache": "Shader-Zwischenspeicher von Steam",
}
NOTE_LATE = "Noch nicht fertig durchsucht."
NOTE_UNSAFE = "Liegt auf einem Laufwerk, das hier nicht durchsucht wird (FUSE, NTFS oder Netz)."
NOTE_LINK = "Der Ordner ist ein Verweis und wird nicht durchsucht."
NOTE_SIZES = "Größen werden noch gemessen."
NOTE_PREFIXES = "Ein Prefix hält Einstellungen und Spielstände mancher Spiele. Er wird hier nur angezeigt."
NOTE_SHADERS = "Steam legt ihn beim nächsten Start neu an."


@dataclass
class Context:
    """Was der Suchlauf braucht; die Verdrahtung (wiring_cleanup) füllt es, Tests stecken Attrappen ein."""
    state_dir: str = ""                                  # <state>/install-jobs.json des Archiv-Installers
    games_root: str = ""                                 # Ordner des Installers (darin .couch-trash)
    downloads: str = ""                                  # paths.user_dir("DOWNLOAD")
    prefix_root: str = ""                                # games.prefix_root (eigene Prefixe)
    steam_roots: list = field(default_factory=list)      # Steam-Wurzeln; Bibliotheken aus libraryfolders.vdf
    entries: Callable[[], list] | None = None            # Einträge der Bibliothek (id, available, …)
    prefix_name: Callable[[Any], str] | None = None      # launch.prefix.prefix_name
    console_trash: Callable[[], dict] | None = None      # installer.trash_list() -> {items, days}
    # dasselbe ohne Größen (nur die kleinen Begleitdateien), wenn die Größen nicht ins Budget passen
    console_trash_quick: Callable[[], dict] | None = None
    system_trash: Callable[[], list] | None = None       # xdgtrash.list_items(…, sizes=True)
    allowed: Callable[[str], bool] | None = None         # files.allowed: darf der Explorer dort in den Papierkorb legen?
    volumes: list = field(default_factory=list)          # [(id, label, path)] für „Frei: …“
    mountinfo: str = "/proc/self/mountinfo"
    min_download: int = MIN_DOWNLOAD
    min_download_days: int = MIN_DOWNLOAD_DAYS
    budget: float = BUDGET_S
    now: float | None = None
    clock: Callable[[], float] = time.monotonic


class _Run:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.now = time.time() if ctx.now is None else ctx.now
        self.deadline = ctx.clock() + max(0.0, float(ctx.budget))
        self.complete = True
        self.cut = 0                            # wie oft das Budget etwas abgeschnitten hat

    def left(self) -> float:
        return self.deadline - self.ctx.clock()

    def late(self) -> bool:
        if self.left() <= 0:
            self.complete = False
            self.cut += 1
            return True
        return False

    def age(self, stamp: Any) -> int | None:
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
            return None
        return int(max(0.0, self.now - float(stamp)) // DAY)

    def safe(self, real: str) -> bool:
        """Darf in diesen Ordner geschaut werden? Nicht in FUSE, NTFS, Netz oder Unbekanntes."""
        return not unsafe_fs(fstype_of(real, self.ctx.mountinfo))


def _item(group: str, key: str, title: str, path: str, size: int | None, age: int | None, action: str,
          ref: Any = None, note: str = "") -> dict:
    return {"id": "%s:%s" % (group, key), "title": title, "path": path, "size": size, "age_days": age,
            "action": action, "ref": ref, "note": note}


def _group(gid: str, items: list, note: str = "", action: str | None = None, limit: int = LIST_MAX) -> dict:
    items = sorted(items, key=lambda i: (-(i["size"] or 0), str(i["title"]).casefold()))
    return {"id": gid, "title": TITLES[gid], "size": sum(i["size"] or 0 for i in items), "count": len(items),
            "more": max(0, len(items) - limit), "items": items[:limit], "note": note,
            "action": action if items else None}


def _timed(run: _Run, fn: Callable | None, reserve: float = 0.0) -> tuple[bool, Any]:
    """fn im eigenen Thread, höchstens bis reserve Sekunden vor dem Ende des Budgets. (fertig, Ergebnis). Ein
    Thread, der zu lange braucht (Größen des Papierkorbs), läuft für sich zu Ende; sein Dienst merkt sich die
    Größen, und der nächste Suchlauf bekommt sie gleich."""
    if fn is None:
        return True, None
    if run.late():
        return False, None
    box: dict = {}

    def work() -> None:
        try:
            box["result"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e
    worker = threading.Thread(target=work, name="couch-cleanup", daemon=True)
    worker.start()
    worker.join(max(0.0, run.left() - reserve))
    if worker.is_alive():
        run.complete = False
        run.cut += 1
        return False, None
    return "error" not in box, box.get("result")


def tree_size(path: str, run: _Run) -> int | None:
    """Belegter Platz eines Ordners (st_blocks): folgt keinem Verweis, betritt keine Einhängung (st_dev des
    Ordners), mit dem Budget des Laufs. None, wenn die Zeit um oder der Ordner nicht lesbar ist."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512
    total, stack, seen = st.st_blocks * 512, [path], 0
    while stack:
        if run.late():
            return None
        current = stack.pop()
        try:
            listing = os.scandir(current)
        except OSError:
            continue
        with listing:
            for entry in listing:
                seen += 1
                if seen % 256 == 0 and run.late():
                    return None
                try:
                    s = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if s.st_dev != st.st_dev:
                    continue                    # Einhängung im Ordner: weder betreten noch zählen
                total += s.st_blocks * 512
                if stat.S_ISDIR(s.st_mode):
                    stack.append(entry.path)
    return total


def _plain_file(path: str) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


# ---------------------------------------------------------------- Gruppen
def _console_trash(run: _Run) -> dict:
    ctx = run.ctx
    quick = ctx.console_trash_quick
    done, result = _timed(run, ctx.console_trash, QUICK_S if quick is not None else 0.0)
    pending = False
    if not done and quick is not None:
        done, result = _timed(run, quick)       # Einträge ohne Größen: löschen lassen sie sich trotzdem
        pending = done
    if not done:
        return _group("console-trash", [], NOTE_LATE)
    result = result if isinstance(result, dict) else {}
    items = []
    for it in result.get("items") or []:
        if not isinstance(it, dict) or not it.get("present") or it.get("purging"):
            continue                            # fehlt auf der Platte bzw. wird schon endgültig gelöscht
        trash_id = it.get("trash_id")
        if not isinstance(trash_id, str) or not TRASH_ID_RE.match(trash_id):
            continue
        size = it.get("size_bytes")
        size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
        path = os.path.join(ctx.games_root, ".couch-trash", trash_id) if ctx.games_root else ""
        items.append(_item("console-trash", trash_id, str(it.get("title") or it.get("name") or trash_id), path, size,
                           run.age(it.get("deleted")), "purge", trash_id))
    days = result.get("days")
    note = ("Leert sich %d Tage nach dem Löschen von selbst." % days
            if items and isinstance(days, int) and not isinstance(days, bool) and days >= 1 else "")
    if pending and items:
        note = NOTE_SIZES
    return _group("console-trash", items, note, "purge")


def _system_trash(run: _Run) -> dict:
    done, result = _timed(run, run.ctx.system_trash)
    if not done:
        return _group("system-trash", [], NOTE_LATE)
    items, unsized = [], False
    for it in result if isinstance(result, list) else []:
        if not isinstance(it, dict) or not isinstance(it.get("id"), str):
            continue
        size = it.get("size")
        if not isinstance(size, int) or isinstance(size, bool):
            size, unsized = None, True
        items.append(_item("system-trash", it["id"], str(it.get("name") or os.path.basename(it["id"])),
                           str(it.get("orig") or it["id"]), size, run.age(it.get("deleted")), "show"))
    if unsized:
        run.complete = False                    # xdgtrash misst die übrigen Größen im Hintergrund weiter
    return _group("system-trash", items, NOTE_SIZES if unsized else "", "empty")


def _read_jobs(state_dir: str) -> list:
    try:
        with open(os.path.join(state_dir, JOBS_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def _archive_parts(path: str) -> list[str]:
    """Alle vorhandenen Teile eines (mehrteiligen) Archivs, nur echte Dateien, keine Verweise. Bei einem
    Installationsprogramm (E114) dessen .bin-Dateien daneben (setup_x-1.bin …)."""
    try:
        if path.lower().endswith((".exe", ".msi")):
            from .installer.setupexe import companions
            parts = [path] + companions(path)
        else:
            from .installer.probe import volume_set
            parts = volume_set(path).get("parts") or [path]
    except Exception:  # noqa: BLE001  (ohne Erkennung der Teile: nur die Datei selbst)
        parts = [path]
    return [p for p in parts if _plain_file(p) is not None]


def _archives(run: _Run, available: dict) -> dict:
    """Aufträge des Archiv-Installers: fertig, das Archiv liegt noch, das Spiel steht verfügbar in der
    Bibliothek. Läuft für dasselbe Archiv noch ein Auftrag, bleibt es weg."""
    ctx = run.ctx
    jobs = _read_jobs(ctx.state_dir)
    busy = {os.path.realpath(j["archive"]) for j in jobs
            if j.get("state") in ACTIVE_JOBS and isinstance(j.get("archive"), str)}
    items, seen = [], set()
    for job in jobs:
        if run.late():
            break
        archive = job.get("archive")
        if job.get("state") != "done" or not isinstance(archive, str) or not os.path.isabs(archive):
            continue
        entry_id = job.get("entry_id") or ("local:" + str(job.get("entry_uuid") or ""))
        if available.get(entry_id) is not True:
            continue                            # nicht (mehr) in der Bibliothek oder gerade nicht verfügbar
        real = os.path.realpath(archive)
        st = _plain_file(archive)
        if real in seen or real in busy or st is None:
            continue
        if not run.safe(os.path.dirname(real)):
            continue                            # FUSE, NTFS, Netz: dort wird nicht gesucht
        found = [(p, _plain_file(p)) for p in _archive_parts(archive)]
        parts = [p for p, s in found if s is not None]
        if not parts or any(os.path.realpath(p) in busy for p in parts):
            continue
        seen.update(os.path.realpath(p) for p in parts)
        size = sum(s.st_blocks * 512 for _p, s in found if s is not None)
        movable = ctx.allowed is None or all(ctx.allowed(p) for p in parts)
        note = "Spiel „%s“ ist installiert." % str(job.get("title") or "")
        if len(parts) > 1:
            note += " %d Teile." % len(parts)
        items.append(_item("archives", str(job.get("id") or real), os.path.basename(archive), archive, size,
                           run.age(st.st_mtime), "trash" if movable else "show", list(parts) if movable else None,
                           note))
    return _group("archives", items, "", None) | {"_paths": seen}


def _downloads(run: _Run, skip: set) -> dict:
    """Große, alte Dateien direkt im Download-Ordner (nur anzeigen und im Explorer öffnen)."""
    ctx = run.ctx
    folder = ctx.downloads
    if not folder or not os.path.isdir(folder):
        return _group("downloads", [])
    if os.path.islink(folder):
        return _group("downloads", [], NOTE_LINK)
    real = os.path.realpath(folder)
    if not run.safe(real):
        return _group("downloads", [], NOTE_UNSAFE)
    items = []
    try:
        listing = os.scandir(folder)
    except OSError:
        return _group("downloads", [])
    with listing:
        for entry in listing:
            if run.late():
                break
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue                        # Verweise, Ordner und Geräte nicht
            size = st.st_blocks * 512
            age = run.age(st.st_mtime)
            if size < ctx.min_download or age is None or age < ctx.min_download_days:
                continue
            if os.path.realpath(entry.path) in skip:
                continue                        # steht schon unter „Archive schon installierter Spiele“
            items.append(_item("downloads", entry.name, entry.name, entry.path, size, age, "open", folder))
    return _group("downloads", items)


def _prefixes(run: _Run, names: set) -> dict:
    """Ordner im eigenen Prefix-Ordner, zu denen kein Eintrag der Bibliothek gehört (nur anzeigen). names:
    die Ordnernamen der Prefixe aller Einträge (launch.prefix.prefix_name). Der eingestellte Ordner selbst
    wird aufgelöst (~/Games kann auf eine Platte zeigen); darin wird keinem Verweis gefolgt."""
    ctx = run.ctx
    root = ctx.prefix_root
    if not root or not os.path.isdir(root):
        return _group("prefixes", [])
    real = os.path.realpath(root)
    if not run.safe(real):
        return _group("prefixes", [], NOTE_UNSAFE)
    try:
        root_dev = os.lstat(real).st_dev
        listing = os.scandir(real)
    except OSError:
        return _group("prefixes", [])
    items = []
    with listing:
        for entry in listing:
            if run.late():
                break
            if entry.name.startswith(".") or entry.name in names:
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode) or st.st_dev != root_dev:
                continue                        # nur echte Ordner, keine Verweise, keine Einhängungen
            items.append(_item("prefixes", entry.name, entry.name, entry.path, tree_size(entry.path, run),
                               run.age(st.st_mtime), "show"))
    return _group("prefixes", items, NOTE_PREFIXES if items else "")


def _manifest_name(library: str, appid: str) -> str:
    try:
        with open(os.path.join(library, "steamapps", "appmanifest_%s.acf" % appid), encoding="utf-8",
                  errors="replace") as fh:
            found = re.search(r'"name"\s+"([^"]*)"', fh.read(65536))
    except OSError:
        return ""
    return found.group(1) if found else ""


def _shadercache(run: _Run) -> dict:
    """Steams Shader-Zwischenspeicher je Spiel (steamapps/shadercache/<appid>), die zehn größten."""
    items = []
    for library in steam_libraries(list(run.ctx.steam_roots or [])):
        cache = os.path.join(library, "steamapps", "shadercache")
        if run.late() or not os.path.isdir(cache) or os.path.islink(cache):
            continue
        if not run.safe(os.path.realpath(cache)):
            continue
        try:
            listing = os.scandir(cache)
        except OSError:
            continue
        with listing:
            for entry in listing:
                if run.late():
                    break
                if not entry.name.isdigit() or not entry.is_dir(follow_symlinks=False):
                    continue
                title = _manifest_name(library, entry.name) or "App %s" % entry.name
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    mtime = None
                items.append(_item("shadercache", "%s:%s" % (library, entry.name), title, entry.path,
                                   tree_size(entry.path, run), run.age(mtime), "show"))
    return _group("shadercache", items, NOTE_SHADERS if items else "", None, SHADER_MAX)


def _free(ctx: Context) -> list:
    """Freier Platz je Datenträger (statvfs, kein Durchsuchen). Liegt der Spiele-Ort auf demselben wie der
    persönliche Ordner (~/Games), steht er nur einmal da, als persönlicher Ordner."""
    found = []
    for vid, label, path in ctx.volumes:
        try:
            st = os.statvfs(path)
            dev = os.stat(path).st_dev
        except OSError:
            continue                            # Platte gerade weg: nichts zu sagen
        found.append((dev, {"id": vid, "label": label, "path": path, "free": st.f_bavail * st.f_frsize,
                            "total": st.f_blocks * st.f_frsize}))
    homes = {dev for dev, v in found if v["id"] == "home"}
    out, devices = [], set()
    for dev, v in found:
        if dev in devices or (dev in homes and v["id"] != "home"):
            continue
        devices.add(dev)
        out.append(v)
    return out


def _step(run: _Run, fn: Callable, *args: Any) -> dict:
    """Eine Gruppe; hat das Budget dabei etwas abgeschnitten, sagt es ihr Vermerk (eine leere Gruppe ist dann
    nicht „nichts gefunden“, sondern nicht fertig durchsucht)."""
    before = run.cut
    group = fn(run, *args)
    if run.cut != before and (not group["items"] or not group["note"]):
        group["note"] = NOTE_LATE
    return group


def scan(ctx: Context) -> dict:
    """Der Suchlauf (blockierend, Arbeits-Thread). Liest nur; siehe Kopf der Datei."""
    run = _Run(ctx)
    available: dict = {}
    names: set = set()
    for entry in (ctx.entries() if ctx.entries else []) or []:
        entry_id = getattr(entry, "id", None)
        if not isinstance(entry_id, str):
            continue
        available[entry_id] = bool(getattr(entry, "available", False)) and bool(getattr(entry, "installed", True))
        if ctx.prefix_name is not None:
            try:
                names.add(ctx.prefix_name(entry))
            except Exception:  # noqa: BLE001
                continue
    groups = [_step(run, _console_trash), _step(run, _system_trash)]
    archives = _step(run, _archives, available)
    skip = archives.pop("_paths")
    groups += [archives, _step(run, _downloads, skip), _step(run, _prefixes, names), _step(run, _shadercache)]
    return {"groups": groups, "total_reclaimable": sum(g["size"] for g in groups if g["id"] in RECLAIMABLE),
            "complete": run.complete, "free": _free(ctx)}
