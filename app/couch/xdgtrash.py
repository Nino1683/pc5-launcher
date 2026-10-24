"""Papierkorb des Systems für den Datei-Explorer: auflisten, zurücklegen, endgültig löschen, leeren.

Wunsch 2026-10-21: „Im Dateimanager gibt es keinen Papierkorb, den ich leeren könnte.“ Der Explorer
legt Gelöschtes mit Gio.File.trash ab, nach der XDG-Regel: für den eigenen Ordner unter
~/.local/share/Trash, auf anderen Laufwerken in <Laufwerk>/.Trash-<uid> (oder .Trash/<uid>). Je
Eintrag liegen files/<name> und info/<name>.trashinfo (Path=, DeletionDate=) nebeneinander.

- Zurücklegen ist ein rename auf demselben Laufwerk, an den alten Ort (fehlt der Ordner, wird er
  angelegt; ist der Name belegt, heißt es „Name (2)“). Überschrieben wird nie.
- Endgültig löscht nur installer/trash.remove_own_dir (Ordner: kein Symlink, gleiches Gerät,
  keine Einhängung darin) bzw. os.unlink (Datei, Verweis): die eine Löschstelle des Produkts.
- Ein Eintrag wird nur angefasst, wenn er direkt in files/ eines der gefundenen Papierkörbe liegt.

Nur Standardbibliothek; alles blockiert und läuft in den Aufträgen von files.FileOps bzw. im
Arbeits-Thread. Nur Größen, die nicht in die Frist passen, misst ein eigener Thread (nur lesend).
"""
from __future__ import annotations

import os
import stat
import threading
import time
import urllib.parse
import uuid

from . import paths

INFO_SUFFIX = ".trashinfo"
SIZE_BUDGET_S = 10.0            # Größen der Einträge: danach ohne Größe weiter (große Spielordner)
TEXT_GONE = "Diesen Eintrag gibt es im Papierkorb nicht mehr."

# Gemessene Größen (E99): (Pfad, Gerät, Inode, mtime_ns) -> Bytes. Im Papierkorb ändert sich ein
# Eintrag nicht mehr; ohne Zwischenspeicher maß jedes Öffnen alle Bäume neu (ein verschobener
# Spielordner mit 100 000 Dateien belegte dabei jedes Mal einen Arbeits-Thread).
_sizes: dict[tuple, int] = {}
_sizes_lock = threading.Lock()
# Was nicht in die Frist passt, misst ein Thread ohne Frist zu Ende (E99), einer nach dem anderen
# und je Eintrag nur einmal. Sonst lief ein großer Baum bei jedem Öffnen wieder die ganze Frist
# lang, die Einträge dahinter wurden nie gemessen, und die Gesamtgröße erschien nie.
_later: list[tuple[tuple, str]] = []
_later_keys: set[tuple] = set()
_later_running = False


def home_trash() -> str:
    """Der eigene Papierkorb (XDG). Im Prüfmodus nur im Käfig (E99): XDG_DATA_HOME gilt dort nur,
    wenn es im Käfig liegt, sonst <COUCH_TEST_ROOT>/home/.local/share/Trash. Ein Test mit
    „Papierkorb leeren“ leerte sonst den echten Papierkorb des Nutzers."""
    given = os.environ.get("XDG_DATA_HOME") or ""
    root = paths.test_root()
    if root and not (given and paths.is_under(given, [root])):
        given = ""
    base = given or paths.user_place(os.path.join(".local", "share"))
    return os.path.join(base, "Trash")


def trash_dirs(places: list[dict]) -> list[tuple[str, str | None]]:
    """[(papierkorb, oberster Ordner des Laufwerks | None)]: der eigene und die der Laufwerke
    unter den Orten, nur vorhandene. Der oberste Ordner gilt für relative Pfade in .trashinfo."""
    uid = os.getuid()
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(folder: str, top: str | None) -> None:
        if os.path.islink(folder) or not os.path.isdir(os.path.join(folder, "files")):
            return
        real = os.path.realpath(folder)
        if real not in seen:
            seen.add(real)
            out.append((folder, top))

    add(home_trash(), None)
    for place in places:
        if place.get("kind") != "storage":
            continue
        top = str(place.get("path") or "")
        add(os.path.join(top, ".Trash-%d" % uid), top)
        shared = os.path.join(top, ".Trash")
        if os.path.isdir(shared) and not os.path.islink(shared):
            add(os.path.join(shared, str(uid)), top)
    return out


def _info_path(trash: str, name: str) -> str:
    return os.path.join(trash, "info", name + INFO_SUFFIX)


def parse_info(path: str, top: str | None) -> tuple[str, float]:
    """(alter Ort, Zeitpunkt des Löschens) aus einer .trashinfo; ("", 0.0), wenn unlesbar."""
    orig, deleted = "", 0.0
    try:
        with open(path, "rb") as f:
            blob = f.read(64 * 1024)
    except OSError:
        return orig, deleted
    for line in blob.splitlines():
        line = line.strip()
        if line.startswith(b"Path="):
            orig = os.fsdecode(urllib.parse.unquote_to_bytes(line[5:]))
            if orig and not os.path.isabs(orig) and top:
                orig = os.path.join(top, orig)
        elif line.startswith(b"DeletionDate="):
            try:
                deleted = time.mktime(time.strptime(line[13:].decode("ascii", "replace")[:19], "%Y-%m-%dT%H:%M:%S"))
            except (ValueError, OverflowError):
                deleted = 0.0
    return (orig if os.path.isabs(orig) else ""), deleted


def list_items(dirs: list[tuple[str, str | None]], sizes: bool = True) -> list[dict]:
    """Alle Einträge, zuletzt Gelöschtes zuerst: [{id, name, orig, deleted, size, dir}].
    id ist der Pfad unter files/; size None, wenn die Zeit für Größen um war (dann misst ein
    Thread weiter, und ein späterer Aufruf liefert die Größe aus dem Zwischenspeicher)."""
    from .files import SizeTimeout, walk_size
    items: list[dict] = []
    deadline = time.monotonic() + SIZE_BUDGET_S
    measured: dict[tuple, int] = {}
    seen: set[tuple] = set()
    for trash, top in dirs:
        try:
            names = os.listdir(os.path.join(trash, "files"))
        except OSError:
            continue
        for name in names:
            path = os.path.join(trash, "files", name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            orig, deleted = parse_info(_info_path(trash, name), top)
            size = None
            if sizes:
                key = (path, st.st_dev, st.st_ino, st.st_mtime_ns)
                seen.add(key)
                with _sizes_lock:
                    size = _sizes.get(key)
                    later = key in _later_keys
                if size is None and not later and time.monotonic() < deadline:
                    try:
                        # Die Frist gilt auch innerhalb eines Baums (E99), nicht nur vor jedem Eintrag
                        size = walk_size(path, deadline=deadline)[0]
                    except SizeTimeout:
                        size = None
                if size is not None:
                    measured[key] = size
                elif not later:
                    _measure_later(key, path)
            items.append({"id": path, "name": os.path.basename(orig) if orig else name, "orig": orig,
                          "deleted": deleted or st.st_mtime, "size": size, "dir": stat.S_ISDIR(st.st_mode)})
    if sizes:
        with _sizes_lock:                       # nur behalten, was es noch gibt
            kept = {k: v for k, v in _sizes.items() if k in seen}
            kept.update(measured)
            _sizes.clear()
            _sizes.update(kept)
    items.sort(key=lambda i: i["deleted"], reverse=True)
    return items


def _measure_later(key: tuple, path: str) -> None:
    """Größe von path ohne Frist im Hintergrund messen (nur lesend, folgt keinem Verweis)."""
    global _later_running
    with _sizes_lock:
        if key in _later_keys:
            return
        _later_keys.add(key)
        _later.append((key, path))
        if _later_running:                      # der laufende Thread nimmt es mit
            return
        _later_running = True
    try:
        threading.Thread(target=_measure_queue, name="couch-trash-size", daemon=True).start()
    except RuntimeError:                        # kein Thread mehr möglich: beim nächsten Öffnen wieder
        with _sizes_lock:
            _later_running = False
            _later.clear()
            _later_keys.clear()


def _measure_queue() -> None:
    global _later_running
    from .files import walk_size
    while True:
        with _sizes_lock:
            if not _later:
                _later_running = False          # unter der Sperre: nichts bleibt ungemessen liegen
                return
            key, path = _later.pop(0)
        size = None
        try:
            st = os.lstat(path)
            if (path, st.st_dev, st.st_ino, st.st_mtime_ns) == key:     # inzwischen weg oder ersetzt: nichts
                size = walk_size(path)[0]
        except Exception:  # noqa: BLE001 – der Thread darf nie mit gesetztem _later_running sterben
            size = None
        with _sizes_lock:
            if size is not None:
                _sizes[key] = size
            _later_keys.discard(key)


def _forget_size(path: str) -> None:
    with _sizes_lock:
        for key in [k for k in _sizes if k[0] == path]:
            del _sizes[key]


def locate(item_id: object, dirs: list[tuple[str, str | None]]) -> tuple[str, str | None, str] | None:
    """(papierkorb, oberster Ordner, name), wenn item_id direkt in files/ eines Papierkorbs liegt."""
    if not isinstance(item_id, str) or not os.path.isabs(item_id):
        return None
    folder, name = os.path.split(item_id)
    if not name or name in (".", ".."):
        return None
    real = os.path.realpath(folder)
    for trash, top in dirs:
        if real == os.path.realpath(os.path.join(trash, "files")):
            return trash, top, name
    return None


def restore(item_id: str, dirs: list[tuple[str, str | None]]) -> str:
    """Legt den Eintrag an seinen alten Ort zurück. Liefert den neuen Pfad; wirft ValueError/OSError."""
    from .files import free_target
    where = locate(item_id, dirs)
    if where is None or not os.path.lexists(item_id):
        raise ValueError(TEXT_GONE)
    trash, top, name = where
    orig, _deleted = parse_info(_info_path(trash, name), top)
    if not orig:
        raise ValueError("Der alte Ort dieses Eintrags ist nicht bekannt.")
    parent = os.path.dirname(orig)
    os.makedirs(parent, exist_ok=True)
    target = free_target(parent, os.path.basename(orig))
    os.rename(item_id, target)                  # derselbe Papierkorb liegt auf demselben Laufwerk
    _unlink_quiet(_info_path(trash, name))
    return target


def delete(item_id: str, dirs: list[tuple[str, str | None]]) -> None:
    """Löscht den Eintrag endgültig. Wirft ValueError/OSError; was bis dahin nicht weg ist, bleibt."""
    from .installer import trash as own
    where = locate(item_id, dirs)
    if where is None:
        raise ValueError(TEXT_GONE)
    trash, _top, name = where
    info = _info_path(trash, name)
    _forget_size(item_id)                       # auch ein halber Versuch ändert die Größe
    try:
        st = os.lstat(item_id)
    except FileNotFoundError:
        _unlink_quiet(info)                     # nur die Beschreibung war noch da
        return
    if stat.S_ISDIR(st.st_mode):
        path = item_id
        if name.startswith("."):
            # remove_own_dir fasst versteckte Ordner nicht an: erst sichtbar umbenennen. Die
            # .trashinfo wandert mit (E99): Bleibt der Ordner halb liegen, steht er weiter unter
            # seinem Namen in der Liste und lässt sich zurücklegen (vorher „alter Ort unbekannt“)
            hidden = "pc5-loeschen-" + uuid.uuid4().hex[:10]
            moved_info = _rename_quiet(info, _info_path(trash, hidden))
            path = os.path.join(trash, "files", hidden)
            try:
                os.rename(item_id, path)
            except OSError:
                if moved_info:
                    _rename_quiet(_info_path(trash, hidden), info)
                raise
            if moved_info:
                info = _info_path(trash, hidden)
        try:
            own.remove_own_dir(path)
        except own.Refuse as e:
            raise ValueError(e.message) from None
    else:
        os.unlink(item_id)
    _unlink_quiet(info)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _rename_quiet(src: str, dst: str) -> bool:
    try:
        os.rename(src, dst)
        return True
    except OSError:
        return False
