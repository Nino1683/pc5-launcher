"""Downloads des Browsers (E59, E60): Dateinamen, Aufträge an das Home, Lesen der Liste.

Seit E60 lädt das Home selbst (couch/fetcher.py): Der Browser schreibt je Download einen Auftrag
(write_request) und liest die Liste <state>/downloads.json nur noch für seine Anzeige (read).
state: queued | running | paused | done | failed | cancelled. Nur Standardbibliothek.

Was WebKit selbst lädt (blob:, data:, POST), führt LocalLog in <state>/browser-downloads.json
(E99); das Home liest sie mit read_local und zeigt die Einträge im Control Center.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

KEEP = 30
STATES = ("queued", "running", "paused", "done", "failed", "cancelled")
LOCAL_FILE = "browser-downloads.json"
LOCAL_STATES = ("running", "done", "failed", "cancelled")
LOCAL_SAVE_GAP = 1.0            # Stand eines laufenden Downloads höchstens so oft schreiben (Sekunden)
TEXT_CLOSED = "Abgebrochen: Der Browser wurde geschlossen."
TEXT_ENDED = "Abgebrochen: Der Browser wurde unerwartet beendet."
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2",
                    ".tar.zst", ".iso")
_BAD = re.compile(r"[\x00-\x1f/\\:*?\"<>|]+")


def safe_name(name: str) -> str:
    """Dateiname aus dem Vorschlag der Seite: ohne Pfad, ohne Steuerzeichen, nicht leer."""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = _BAD.sub("_", name).strip(" .")
    if not name or name in (".", ".."):
        name = "Download"
    if len(name.encode("utf-8")) > 200:
        stem, ext = split_ext(name)
        while len((stem + ext).encode("utf-8")) > 200 and stem:
            stem = stem[:-1]
        name = (stem or "Download") + ext
    return name


def split_ext(name: str) -> tuple[str, str]:
    low = name.lower()
    for ext in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if low.endswith(ext) and len(name) > len(ext):
            return name[:-len(ext)], name[-len(ext):]
    stem, ext = os.path.splitext(name)
    return (stem, ext) if stem else (name, "")


def unique_path(folder: str, name: str) -> str:
    """Freier Pfad im Ordner: "spiel.zip", sonst "spiel (2).zip", "spiel (3).zip" …"""
    name = safe_name(name)
    path = os.path.join(folder, name)
    if not os.path.lexists(path):
        return path
    stem, ext = split_ext(name)
    n = 2
    while True:
        path = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
        if not os.path.lexists(path):
            return path
        n += 1


def is_archive(name: str) -> bool:
    return str(name or "").lower().endswith(ARCHIVE_SUFFIXES)


def read(path: str) -> list[dict]:
    """Die Einträge der Datei; fehlt sie oder ist sie kaputt: []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") and item.get("state") in STATES:
            out.append(item)
    return out[:KEEP]


def write_request(spool_dir: str, url: str, name: str, headers: dict | None = None) -> str:
    """Download-Auftrag für das Home (E60): eine Datei je Download in <state>/download-requests/.
    Erst unter anderem Namen geschrieben, dann umbenannt: Das Home liest nie eine halbe Datei.
    Rechte 0600, die Kopfzeilen können Cookies enthalten."""
    os.makedirs(spool_dir, exist_ok=True)
    path = os.path.join(spool_dir, "%d-%s.json" % (time.time_ns(), uuid.uuid4().hex[:8]))
    tmp = path + ".neu"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"url": url, "name": name, "headers": dict(headers or {})}, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def proc_start(pid: int) -> str | None:
    """Startzeit des Prozesses (Feld 22 von /proc/<pid>/stat, Takte seit dem Hochfahren) oder
    None, wenn es ihn nicht gibt. Mit der PID zusammen erkennt sie eine wiederverwendete PID."""
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as f:
            blob = f.read(4096)
    except (OSError, ValueError):
        return None
    fields = blob.rpartition(b")")[2].split()       # der Name in Klammern kann Leerzeichen haben
    return fields[19].decode("ascii", "replace") if len(fields) > 19 else ""


def owner_gone(item: dict) -> bool:
    """Lebt der Browser nicht mehr, der diesen Eintrag schreibt? (E99) Ohne PID: nicht zu sagen."""
    pid = _int(item.get("pid"))
    if not pid:
        return False
    start = proc_start(pid)
    if start is None:
        return True
    return bool(item.get("pid_start")) and bool(start) and start != item["pid_start"]


def read_local(path: str) -> list[dict]:
    """Die Downloads, die WebKit selbst lädt (Datei von LocalLog), geprüft und mit festen Feldern:
    [{id, name, path, received, total, state, error, started, finished, pid, pid_start}]. Fehlt sie: []."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not (isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("state") in LOCAL_STATES
                and isinstance(item.get("path"), str) and os.path.isabs(item["path"])):
            continue
        out.append({"id": item["id"], "name": str(item.get("name") or os.path.basename(item["path"])),
                    "path": item["path"], "received": _int(item.get("received")), "total": _int(item.get("total")),
                    "state": item["state"], "error": str(item.get("error") or ""),
                    "started": float(_int(item.get("started"))), "finished": float(_int(item.get("finished"))),
                    "pid": _int(item.get("pid")), "pid_start": str(item.get("pid_start") or "")})
    return out[:KEEP]


class LocalLog:
    """Liste der Downloads, die WebKit selbst lädt (E99). Schreibt allein der Browser, atomar und
    mit Rechten 0600; die neuesten zuerst, höchstens KEEP. Was beim Start noch „läuft“, ist mit
    dem vorigen Browser gestorben und gilt als abgebrochen. Jeder Eintrag trägt PID und Startzeit
    des Browsers: Stirbt er ohne quit() (Absturz, SIGKILL, Strom weg), sieht das Home daran, dass
    „running“ nicht mehr stimmt (sonst sperrte der Eintrag Ruhemodus und Bildschirmschoner)."""

    def __init__(self, path: str, clock=time.time, monotonic=time.monotonic):
        self.path = path
        self.clock = clock
        self.monotonic = monotonic
        self._written = 0.0
        self.pid = os.getpid()
        self.pid_start = proc_start(self.pid) or ""
        self.items = read_local(path)
        stale = False
        for item in self.items:
            if item["state"] == "running":
                item.update(state="cancelled", error=TEXT_CLOSED, finished=self.clock())
                stale = True
        if stale:
            self.save()

    def add(self, name: str, path: str, total: int = 0) -> str:
        item_id = "b-" + uuid.uuid4().hex[:12]
        self.items.insert(0, {"id": item_id, "name": name, "path": path, "received": 0, "total": _int(total),
                              "state": "running", "error": "", "started": self.clock(), "finished": 0,
                              "pid": self.pid, "pid_start": self.pid_start})
        del self.items[KEEP:]
        self.save()
        return item_id

    def get(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i["id"] == item_id), None)

    def progress(self, item_id: str, received: int, total: int = 0) -> None:
        """Gedrosselt: höchstens alle LOCAL_SAVE_GAP Sekunden auf die Platte."""
        item = self.get(item_id)
        if item is None or item["state"] != "running":
            return
        item["received"] = _int(received)
        if total:
            item["total"] = _int(total)
        if self.monotonic() - self._written >= LOCAL_SAVE_GAP:
            self.save()

    def end(self, item_id: str, state: str, error: str = "", path: str = "") -> None:
        item = self.get(item_id)
        if item is None or item["state"] != "running" or state not in LOCAL_STATES:
            return
        item.update(state=state, error=error, finished=self.clock())
        if path:
            item.update(path=path, name=os.path.basename(path))
        if state == "done" and item["total"] < item["received"]:
            item["total"] = item["received"]
        self.save()

    def close_running(self) -> None:
        """Ende des Browsers: Was noch läuft, ist abgebrochen (WebKit setzt nicht fort)."""
        changed = False
        for item in self.items:
            if item["state"] == "running":
                item.update(state="cancelled", error=TEXT_CLOSED, finished=self.clock())
                changed = True
        if changed:
            self.save()

    def save(self) -> bool:
        self._written = self.monotonic()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = "%s.%d.neu" % (self.path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": self.items}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False
