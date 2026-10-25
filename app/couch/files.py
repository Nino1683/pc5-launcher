"""Datei-Explorer der Konsole (Bauplan E62): Orte, Ordner lesen, Dateien kopieren, verschieben,
umbenennen, neue Ordner, in den Papierkorb.

Wunsch 2026-10-18: „Ich möchte auch einen File Explorer haben, der Controller-optimiert
funktioniert.“ Die Seite (screens/files.js) bedient alles mit dem Pad; hier liegt, was die Platte
anfasst. Regeln:
- Nur unter den Orten (roots): persönlicher Ordner, Spiele-Platte, eingehängte Laufwerke unter
  /run/media/<nutzer>. Pfade werden aufgelöst geprüft (realpath), Verweise führen nicht hinaus.
- Gelöscht wird nur in den Papierkorb (Gio.File.trash, auf jedem Laufwerk sein eigener).
  Verschieben über Laufwerksgrenzen = kopieren, danach das Original in den Papierkorb. Endgültig
  erst im Papierkorb selbst (restore/purge/empty, xdgtrash.py), immer nach Rückfrage der Seite.
- Kopieren folgt keinem Verweis (Verweise werden als Verweise kopiert), überspringt keine Fehler
  still und legt bei Namensgleichheit „Name (2)“ an statt zu überschreiben. Rechte (wie cp,
  der Besitzer darf immer schreiben) und Zeitstempel gehen mit. Kopiert wird nur, wenn der Platz
  samt Reserve reicht: auf ein anderes Laufwerk immer, auf demselben ohne Reflink (ext4) (E99).
- Lange Arbeit läuft als Auftrag in einem eigenen Thread; Stand und Ende gehen als Ereignisse
  files.progress / files.done an die Seite (emit), abbrechbar mit cancel.

Nur Standardbibliothek, bis auf Gio für den Papierkorb (im Auftrag geladen).
"""
from __future__ import annotations

import errno
import os
import shutil
import stat
import threading
import time
import uuid
from typing import Any, Callable

from . import log, paths, xdgtrash
from .browser.downloads import split_ext

_log = log.get("files")

MAX_ENTRIES = 3000
TEXT_LIMIT = 64 * 1024
CHUNK = 8 * 1024 * 1024
PROGRESS_GAP = 0.25
# So viel bleibt auf dem Ziel frei, wenn der Explorer dorthin kopiert (E99): Ohne Prüfung lief
# die Platte bis 0 Byte voll, und Home, Steam und das Spiel scheiterten beim Schreiben. Kopieren
# auf demselben Laufwerk prüft nur, wo nicht per Reflink geklont wird (ext4: die Spiele-Platte).
SPACE_RESERVE = 1 << 30
REFLINK_FSTYPES = ("btrfs", "xfs")


def _read_umask() -> int:
    """Umask des Prozesses aus /proc (os.umask zum Lesen änderte sie kurz für alle Threads)."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8) & 0o777
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


_UMASK = _read_umask()

KINDS = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".svg"),
    "video": (".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv"),
    "audio": (".mp3", ".flac", ".ogg", ".opus", ".wav", ".m4a", ".aac"),
    "archive": (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".txz", ".bz2", ".zst", ".iso"),
    "text": (".txt", ".md", ".log", ".ini", ".cfg", ".conf", ".json", ".xml", ".yml", ".yaml", ".nfo", ".csv"),
    "pdf": (".pdf",),
    "windows": (".exe", ".bat", ".msi"),
    "linux": (".sh", ".x86_64", ".appimage"),
    "rom": (".xci", ".nsp", ".wua", ".rpx", ".wud", ".iso.wbfs", ".nds", ".3ds", ".gba"),
}

TEXT_OUTSIDE = "Dieser Ort liegt außerhalb der Konsole."
TEXT_GONE = "Das gibt es nicht mehr."
TEXT_BAD_NAME = "Dieser Name geht nicht."
TEXT_EXISTS = "Hier gibt es schon etwas mit diesem Namen."
TEXT_NO_TRASH = "Auf diesem Laufwerk gibt es keinen Papierkorb. Gelöscht wird nichts."
TEXT_INTO_ITSELF = "Ein Ordner lässt sich nicht in sich selbst kopieren."
TEXT_NO_SPACE = "Auf dem Ziel fehlen %s."


def kind_of(name: str, is_dir: bool = False) -> str:
    if is_dir:
        return "dir"
    low = name.lower()
    for kind, exts in KINDS.items():
        if low.endswith(exts):
            return kind
    return "file"


def roots() -> list[dict]:
    """Die Orte: [{name, path, kind}] in fester Reihenfolge, nur vorhandene."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(name: str, path: str, kind: str) -> None:
        real = os.path.realpath(path)
        if real in seen or not os.path.isdir(real):
            return
        seen.add(real)
        out.append({"name": name, "path": path, "kind": kind})

    if paths.test_mode():
        for p in [p for p in os.environ.get("COUCH_FILES_ROOTS", "").split(":") if p]:
            add(os.path.basename(p.rstrip("/")) or p, p, "folder")
        return out
    home = paths.home()
    # E104: Ordner nach XDG (user-dirs.dirs, sonst deutsche bzw. englische Namen); beschriftet bleiben sie deutsch
    from . import capture
    for name, path, kind in (("Downloads", paths.user_dir("DOWNLOAD"), "folder"), ("Persönlicher Ordner", home, "home"),
                             ("Bildschirmfotos", capture.folder(), "folder"), ("Bilder", paths.user_dir("PICTURES"), "folder"),
                             ("Videos", paths.user_dir("VIDEOS"), "folder"), ("Musik", paths.user_dir("MUSIC"), "folder"),
                             ("Dokumente", paths.user_dir("DOCUMENTS"), "folder")):
        add(name, path, kind)
    disk = paths.games_disk()
    # Spiele-Ort: eine eingehängte Platte wie bisher; ein gewöhnlicher Ordner außerhalb von ~ (E104) auch
    if os.path.ismount(disk) or (os.path.isdir(disk) and not paths.is_under(disk, [home])):
        add("Spiele-Platte", disk, "storage")
    for p in paths.media_roots():          # /run/media/<nutzer>/…, /run/media/…, /media/<nutzer>/… (E104)
        add(os.path.basename(p), p, "storage")
    return out


def allowed(path: str, places: list[dict] | None = None) -> bool:
    return bool(path) and os.path.isabs(path) and paths.is_under(path, [r["path"] for r in (places or roots())])


def entry_of(path: str, name: str | None = None) -> dict | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    link = stat.S_ISLNK(st.st_mode)
    is_dir = stat.S_ISDIR(st.st_mode) or (link and os.path.isdir(path))
    name = name or os.path.basename(path)
    return {"name": name, "path": path, "type": "dir" if is_dir else "file", "kind": kind_of(name, is_dir),
            "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime), "link": link,
            "hidden": name.startswith(".")}


def _setup_budget(path: str):
    """Wie viele .exe dieser Liste in den Dateikopf schauen dürfen (setupexe.Budget); None auf langsamen
    Platten (NTFS, FUSE, Netz: dort entscheidet nur der Name, wie in der Dateiauswahl des Installers)."""
    try:
        from .installer import browse, setupexe
        return None if browse._slow_filesystem(os.path.realpath(path)) else setupexe.Budget()
    except Exception:  # noqa: BLE001
        return None


def _is_setup(e: dict, budget) -> bool:
    try:
        from .installer import setupexe
        return setupexe.listing_flag(e["path"], e["name"], int(e.get("size") or 0), budget)
    except Exception:  # noqa: BLE001  (ohne Erkennung bleibt es eine gewöhnliche Startdatei)
        return False


def listing(path: str | None, hidden: bool = False) -> dict:
    """Inhalt eines Ordners. Ohne path: die Orte."""
    places = roots()
    if not path:
        return {"ok": True, "path": None, "parent": None, "name": "",
                "entries": [dict(entry_of(r["path"], r["name"]) or {}, kind=r["kind"]) for r in places
                            if entry_of(r["path"])]}
    if not allowed(path, places):
        return {"ok": False, "code": "outside", "message": TEXT_OUTSIDE, "path": path, "entries": []}
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return {"ok": False, "code": "gone", "message": TEXT_GONE, "path": path, "entries": []}
    except OSError as e:
        return {"ok": False, "code": "error", "message": "Ordner nicht lesbar: %s" % e.strerror, "path": path, "entries": []}
    entries = []
    budget = _setup_budget(path)
    for name in names:
        if not hidden and name.startswith("."):
            continue
        e = entry_of(os.path.join(path, name), name)
        if e is not None:
            if e["kind"] == "windows" and e["type"] == "file":
                # E114: Installationsprogramm (Inno Setup, NSIS, MSI): Kreuz heißt „Installieren“, nicht „Starten“
                e["installer"] = _is_setup(e, budget)
            entries.append(e)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold()))
    total = len(entries)
    top = next((r for r in sorted(places, key=lambda r: -len(r["path"])) if paths.is_under(path, [r["path"]])), None)
    at_root = top is not None and os.path.realpath(top["path"]) == os.path.realpath(path)
    return {"ok": True, "path": path, "parent": None if at_root else os.path.dirname(path.rstrip("/")) or "/",
            "name": top["name"] if at_root and top else os.path.basename(path.rstrip("/")),
            "entries": entries[:MAX_ENTRIES], "truncated": total > MAX_ENTRIES, "total": total}


def read_text(path: str) -> dict:
    if not allowed(path):
        return {"ok": False, "message": TEXT_OUTSIDE}
    try:
        with open(path, "rb") as f:
            raw = f.read(TEXT_LIMIT + 1)
    except OSError as e:
        return {"ok": False, "message": "Datei nicht lesbar: %s" % e.strerror}
    return {"ok": True, "text": raw[:TEXT_LIMIT].decode("utf-8", "replace"), "truncated": len(raw) > TEXT_LIMIT}


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\0" in name or len(name.encode()) > 255:
        raise ValueError(TEXT_BAD_NAME)
    return name


def free_target(folder: str, name: str) -> str:
    """„name“, sonst „name (2)“, „name (3)“ … im Ordner (nichts wird überschrieben)."""
    path = os.path.join(folder, name)
    if not os.path.lexists(path):
        return path
    stem, ext = split_ext(name) if not os.path.isdir(path) else (name, "")
    n = 2
    while True:
        path = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
        if not os.path.lexists(path):
            return path
        n += 1


class SizeTimeout(Exception):
    """walk_size: die Frist ist um."""


def walk_size(path: str, deadline: float | None = None) -> tuple[int, int]:
    """(Bytes, Anzahl) ohne Verweisen zu folgen. Mit deadline (time.monotonic) wirft es
    SizeTimeout, sobald die Frist um ist, auch mitten in einem großen Baum (E99)."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return (st.st_size if stat.S_ISREG(st.st_mode) else 0), 1
    size, count = 0, 1
    for base, dirs, files in os.walk(path, followlinks=False):
        if deadline is not None and time.monotonic() > deadline:
            raise SizeTimeout()
        for i, name in enumerate(files + [d for d in dirs if os.path.islink(os.path.join(base, d))]):
            if deadline is not None and i % 512 == 511 and time.monotonic() > deadline:
                raise SizeTimeout()
            try:
                s = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            size += s.st_size if stat.S_ISREG(s.st_mode) else 0
            count += 1
        count += sum(1 for d in dirs if not os.path.islink(os.path.join(base, d)))
    return size, count


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, op: str, sources: list[str], dest: str = "", name: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.op, self.sources, self.dest, self.name = op, sources, dest, name
        self.total = 0
        self.done = 0
        self.current = ""
        self.cancel = threading.Event()
        self.result: dict | None = None


class FileOps:
    """Aufträge mit Fortschritt. emit(event, payload) geht an die Seite (aus dem Arbeits-Thread
    über post in die Hauptschleife)."""

    def __init__(self, emit: Callable[[str, dict], None], post: Callable[..., None] = lambda fn, *a: fn(*a),
                 notify: Any = None, trash: Callable[[str], None] | None = None):
        self.emit = emit
        self.post = post
        self.notify = notify
        self.trash_fn = trash or gio_trash
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    # ------------------------------------------------------------ Annahme
    def start(self, op: str, sources: Any, dest: str = "", name: str = "") -> dict:
        places = roots()
        sources = [str(s) for s in (sources or []) if isinstance(s, str)]
        if op in ("restore", "purge", "empty"):
            return self._start_trash(op, sources, places)
        if op not in ("copy", "move", "trash", "rename", "mkdir"):
            return {"ok": False, "message": "Unbekannte Aktion."}
        if op in ("copy", "move", "trash", "rename") and not sources:
            return {"ok": False, "message": "Nichts gewählt."}
        for p in sources:
            if not allowed(p, places) or os.path.realpath(p) in [os.path.realpath(r["path"]) for r in places]:
                return {"ok": False, "message": TEXT_OUTSIDE if not allowed(p, places) else "Einen Ort selbst lässt sich nicht ändern."}
            if not os.path.lexists(p):
                return {"ok": False, "message": TEXT_GONE}
        if op in ("copy", "move", "mkdir"):
            if not allowed(dest, places) or not os.path.isdir(dest):
                return {"ok": False, "message": TEXT_OUTSIDE}
        if op in ("copy", "move"):
            for p in sources:
                if os.path.isdir(p) and not os.path.islink(p) and paths.is_under(dest, [p]):
                    return {"ok": False, "message": TEXT_INTO_ITSELF}
        if op in ("rename", "mkdir"):
            try:
                name = check_name(name)
            except ValueError as e:
                return {"ok": False, "message": str(e)}
        job = Job(op, sources, dest, name)
        with self.lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), name="couch-files", daemon=True).start()
        return {"ok": True, "jobId": job.id}

    def _start_trash(self, op: str, sources: list[str], places: list[dict]) -> dict:
        """Aufträge im Papierkorb: zurücklegen, endgültig löschen, alles leeren."""
        dirs = xdgtrash.trash_dirs(places)
        if op == "empty":
            sources = [i["id"] for i in xdgtrash.list_items(dirs, sizes=False)]
            if not sources:
                return {"ok": False, "message": "Der Papierkorb ist schon leer."}
        elif not sources:
            return {"ok": False, "message": "Nichts gewählt."}
        elif any(xdgtrash.locate(p, dirs) is None for p in sources):
            return {"ok": False, "message": xdgtrash.TEXT_GONE}
        job = Job(op, sources)
        job.dirs = dirs
        with self.lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._run, args=(job,), name="couch-files", daemon=True).start()
        return {"ok": True, "jobId": job.id}

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            return {"ok": False}
        job.cancel.set()
        return {"ok": True}

    # ------------------------------------------------------------ Arbeit
    def _run(self, job: Job) -> None:
        started = time.monotonic()
        try:
            result = getattr(self, "_" + job.op)(job)
            result.setdefault("ok", True)
        except Cancelled:
            result = {"ok": False, "cancelled": True, "message": "Abgebrochen."}
        except ValueError as e:
            result = {"ok": False, "message": str(e)}
        except OSError as e:
            if e.errno == errno.ENOSPC:
                msg = "Zu wenig Platz auf dem Ziel."
            else:
                msg = "%s: %s" % (os.path.basename(e.filename or "") or "Datei", e.strerror or e)
            result = {"ok": False, "message": msg}
        except Exception as e:  # noqa: BLE001
            _log.exception("Dateiauftrag %s", job.op)
            result = {"ok": False, "message": "Das hat nicht geklappt: %s" % e}
        job.result = result
        with self.lock:
            self.jobs.pop(job.id, None)
        payload = dict(result, jobId=job.id, op=job.op)
        _log.info("Dateiauftrag %s: %s", job.op, payload.get("message") or "fertig")
        self.post(self.emit, "files.done", payload)
        # Lange Aufträge melden sich auch, wenn der Explorer schon zu ist: Erfolg wie Fehlschlag
        # (vorher erfuhr man von „Zu wenig Platz“ nach dem Verlassen nichts)
        if self.notify is not None and time.monotonic() - started > 10 and not result.get("cancelled"):
            if result.get("ok"):
                self.post(self.notify.push, "Dateien: fertig", result.get("message") or "", "folder")
            else:
                self.post(self.notify.push, "Dateien: hat nicht geklappt", result.get("message") or "", "warn")

    def _progress(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - getattr(job, "_shown", 0) < PROGRESS_GAP:
            return
        job._shown = now
        self.post(self.emit, "files.progress", {"jobId": job.id, "op": job.op, "done": job.done,
                                                "total": job.total, "current": job.current})

    def _copy(self, job: Job) -> dict:
        sizes = {p: walk_size(p)[0] for p in job.sources}
        job.total = sum(sizes.values())
        # Auf demselben Laufwerk belegt nur eine Kopie ohne Reflink neuen Platz (ext4)
        clones = reflinks(job.dest)
        self._check_space(job.dest, [p for p in job.sources if not (clones and same_device(p, job.dest))], sizes)
        made = []
        for src in job.sources:
            target = free_target(job.dest, os.path.basename(src.rstrip("/")))
            self._copy_whole(job, src, target)
            made.append(target)
        self._progress(job, force=True)
        return {"message": "%d kopiert." % len(made) if len(made) != 1 else "Kopiert: %s" % os.path.basename(made[0]),
                "paths": made}

    @staticmethod
    def _check_space(dest: str, far: list[str], sizes: dict[str, int]) -> None:
        """Vor dem Kopieren der Quellen far: Reicht der Platz samt SPACE_RESERVE? Sonst gleich
        ablehnen, nicht erst nach Minuten mit ENOSPC (E99)."""
        need = sum(sizes.get(p, 0) for p in far)
        if need <= 0:
            return
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            return
        if free < need + SPACE_RESERVE:
            from .installer import human_size
            raise ValueError(TEXT_NO_SPACE % human_size(need + SPACE_RESERVE - free))

    def _copy_whole(self, job: Job, src: str, target: str) -> None:
        """Kopiert src nach target (frei gewählt, gab es vorher nicht). Bei Abbruch oder Fehler
        kommt die halbe Kopie wieder weg: Ein halb leerer Ordner unter dem Originalnamen sah
        fertig aus und verleitete dazu, das Original zu löschen."""
        try:
            self._copy_any(job, src, target)
        except BaseException:
            try:
                st = os.lstat(target)
            except OSError:
                st = None                       # nichts angelegt
            if st is None:
                raise
            try:
                if stat.S_ISDIR(st.st_mode):
                    from .installer import trash
                    trash.remove_own_dir(target)    # die eine Löschstelle des Produkts (E17)
                else:
                    os.unlink(target)
                _log.info("Dateiauftrag %s: halbe Kopie %s entfernt", job.op, target)
            except Exception as e:  # noqa: BLE001
                _log.warning("Dateiauftrag %s: halbe Kopie %s bleibt liegen: %s", job.op, target, e)
            raise

    def _copy_any(self, job: Job, src: str, dst: str) -> None:
        if job.cancel.is_set():
            raise Cancelled()
        st = os.lstat(src)
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(src), dst)
        elif stat.S_ISDIR(st.st_mode):
            os.mkdir(dst)
            for name in sorted(os.listdir(src)):
                self._copy_any(job, os.path.join(src, name), os.path.join(dst, name))
            _copystat(src, dst)
        elif stat.S_ISREG(st.st_mode):
            self._copy_file(job, src, dst)
        # Geräte, Pipes, Sockets: nicht kopieren

    def _copy_file(self, job: Job, src: str, dst: str) -> None:
        job.current = os.path.basename(src)
        ok = False
        try:
            with open(src, "rb") as fin, open(dst, "xb") as fout:
                while True:
                    if job.cancel.is_set():
                        raise Cancelled()
                    try:
                        n = os.copy_file_range(fin.fileno(), fout.fileno(), CHUNK)
                    except OSError as e:
                        if e.errno not in (errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                            raise
                        data = fin.read(CHUNK)
                        n = len(data)
                        fout.write(data)
                    if n == 0:
                        break
                    job.done += n
                    self._progress(job)
            _copystat(src, dst)
            ok = True
        finally:
            if not ok:
                try:
                    os.unlink(dst)              # eigene halbe Kopie
                except OSError:
                    pass

    def _move(self, job: Job) -> dict:
        moved, via_copy = [], 0
        # Geht etwas auf ein anderes Laufwerk, zählt der Fortschritt dessen Bytes (sonst stand
        # bei 50 GB minutenlang nur „Verschieben …“), sonst die Einträge
        far = [src for src in job.sources if not same_device(src, job.dest)]
        sizes = {src: walk_size(src)[0] for src in far}
        self._check_space(job.dest, far, sizes)
        job.total = sum(sizes.values()) if far else len(job.sources)
        for src in job.sources:
            if job.cancel.is_set():
                raise Cancelled()
            target = free_target(job.dest, os.path.basename(src.rstrip("/")))
            if src not in far:
                os.rename(src, target)
                if not far:
                    job.done += 1
            else:
                # anderes Laufwerk: kopieren, dann das Original in den Papierkorb (rückholbar)
                self._copy_whole(job, src, target)
                self.trash_fn(src)
                via_copy += 1
            moved.append(target)
            self._progress(job, force=True)
        msg = "%d verschoben." % len(moved) if len(moved) != 1 else "Verschoben: %s" % os.path.basename(moved[0])
        if via_copy:
            msg += " Die Originale auf dem anderen Laufwerk liegen im Papierkorb."
        return {"message": msg, "paths": moved}

    def _trash(self, job: Job) -> dict:
        job.total = len(job.sources)
        for src in job.sources:
            if job.cancel.is_set():
                raise Cancelled()
            self.trash_fn(src)
            job.done += 1
            self._progress(job)
        return {"message": "%d in den Papierkorb gelegt." % job.total if job.total != 1
                else "In den Papierkorb gelegt: %s" % os.path.basename(job.sources[0].rstrip("/"))}

    def _restore(self, job: Job) -> dict:
        job.total = len(job.sources)
        made = []
        for src in job.sources:
            if job.cancel.is_set():
                raise Cancelled()
            job.current = os.path.basename(src)
            made.append(xdgtrash.restore(src, job.dirs))
            job.done += 1
            self._progress(job)
        return {"message": "%d zurückgelegt." % len(made) if len(made) != 1 else "Zurückgelegt nach %s" % os.path.dirname(made[0]),
                "paths": made}

    def _purge(self, job: Job) -> dict:
        """Je Eintrag weiter, auch wenn einer nicht weggeht (E99): Vorher hielt der erste
        unlöschbare Eintrag (immer der neueste) jedes „Leeren“ an, alles danach blieb liegen."""
        job.total = len(job.sources)
        gone, left = 0, []
        for src in job.sources:
            if job.cancel.is_set():
                raise Cancelled()
            job.current = os.path.basename(src)
            try:
                xdgtrash.delete(src, job.dirs)
                gone += 1
            except (ValueError, OSError) as e:
                _log.warning("Papierkorb: %s bleibt liegen: %s", src, e)
                left.append("%s: %s" % (job.current, _reason(e)))
            job.done += 1
            self._progress(job)
        if left:
            if not gone and len(left) == 1:
                return {"ok": False, "message": left[0]}
            shown = "; ".join(left[:3]) + (" …" if len(left) > 3 else "")
            return {"ok": False, "message": "%d gelöscht, %d %s liegen: %s" % (
                gone, len(left), "blieb" if len(left) == 1 else "blieben", shown)}
        return {"message": "%d endgültig gelöscht." % job.total if job.total != 1 else "Endgültig gelöscht."}

    def _empty(self, job: Job) -> dict:
        result = self._purge(job)
        if result.get("ok") is False:
            return result
        return {"message": "Der Papierkorb ist leer."}

    def _rename(self, job: Job) -> dict:
        src = job.sources[0]
        target = os.path.join(os.path.dirname(src.rstrip("/")), job.name)
        if os.path.lexists(target):
            raise ValueError(TEXT_EXISTS)
        os.rename(src, target)
        return {"message": "Umbenannt.", "paths": [target]}

    def _mkdir(self, job: Job) -> dict:
        target = free_target(job.dest, job.name)
        os.mkdir(target)
        return {"message": "Ordner angelegt: %s" % os.path.basename(target), "paths": [target]}


def same_device(src: str, dest_dir: str) -> bool:
    return os.lstat(src).st_dev == os.stat(dest_dir).st_dev


def fs_type(path: str, mountinfo: str = "/proc/self/mountinfo") -> str:
    """Dateisystem, auf dem path liegt (längster passender Einhängepunkt), "" wenn unbekannt."""
    real = os.path.realpath(path)
    best, kind = "", ""
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                fields = left.split(" ")
                if len(fields) < 5 or not right:
                    continue
                point = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= len(best):
                    best, kind = point, right.split(" ")[0].lower()
    except OSError:
        return ""
    return kind


def reflinks(dest_dir: str) -> bool:
    """Klont copy_file_range auf diesem Laufwerk (btrfs, xfs), statt Daten zu schreiben? Unbekannt:
    nein, dann wird der Platz eben geprüft."""
    return fs_type(dest_dir) in REFLINK_FSTYPES


def _reason(e: Exception) -> str:
    if isinstance(e, OSError):
        if e.errno == errno.ENOSPC:
            return "Zu wenig Platz."
        name = os.path.basename(e.filename or "")
        return "%s: %s" % (name, e.strerror or e) if name else str(e.strerror or e)
    return str(e)


def copy_mode(mode: int, is_dir: bool) -> int:
    """Rechte der Kopie wie bei cp: Bits der Quelle ohne setuid/setgid, durch die Umask, und der
    Besitzer darf immer schreiben (Ordner: auch lesen und betreten). Aus einer ISO/DVD (0555/0444)
    wurde sonst eine Kopie, die sich weder verschieben noch in den Papierkorb legen ließ, und
    aus NTFS/exFAT (0777) eine, in die jeder schreiben durfte."""
    mode = stat.S_IMODE(mode) & 0o777 & ~_UMASK | stat.S_IWUSR
    return mode | stat.S_IRWXU if is_dir else mode


def _copystat(src: str, dst: str) -> None:
    """Zeitstempel und Rechte (E99: ohne Rechte wurde aus start.sh 0755 eine 0644, und ein
    verschobenes Linux-Spiel startete nicht mehr); Rechte über copy_mode. Auf vfat/NTFS still nicht."""
    try:
        st = os.lstat(src)
    except OSError:
        return
    if not stat.S_ISLNK(st.st_mode):
        try:
            os.chmod(dst, copy_mode(st.st_mode, stat.S_ISDIR(st.st_mode)))
        except (OSError, NotImplementedError):
            pass
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
    except (OSError, NotImplementedError):
        pass


def gio_trash(path: str) -> None:
    """In den Papierkorb des jeweiligen Laufwerks. Ohne Papierkorb: Fehler, nichts gelöscht."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    try:
        Gio.File.new_for_path(path).trash(None)
    except GLib.Error as e:
        if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
            raise ValueError(TEXT_NO_TRASH) from e
        raise ValueError("Nicht in den Papierkorb: %s" % e.message) from e
