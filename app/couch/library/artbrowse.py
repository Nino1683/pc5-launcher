"""Dateiauswahl für „Bild ändern“ (lib.artBrowse): ein Ordner, darin Unterordner und Bilddateien.

Wie die Dateiauswahl des Archiv-Installers (install.browse), aber für Bilder: gleiche Form der
Antwort, Einträge vom Typ "dir" und "image", die Bilder mit dem neuesten zuerst (das eben
heruntergeladene Cover steht oben). Andere Dateien werden nicht aufgeführt.

Nur lesend, nie rekursiv, nur unter den übergebenen Wurzeln (paths.browse_roots()); versteckte
Einträge bleiben aus, Verweise, die aus den Wurzeln hinausführen, werden nicht verfolgt.
list_images() blockiert (Platte) und gehört in einen Thread.
"""
from __future__ import annotations

import os

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga")
ENTRY_LIMIT = 400        # so viele Einträge gehen höchstens an die Oberfläche
SCAN_LIMIT = 5000        # so viele Verzeichniseinträge werden höchstens gelesen


def _roots(roots) -> list[dict]:
    out = []
    for item in roots or []:
        if isinstance(item, dict):
            path, name = item.get("path"), item.get("name")
        else:
            path, name = item, None
        if not isinstance(path, str) or not os.path.isabs(path):
            continue
        real = os.path.realpath(path)
        if not os.path.isdir(real) or any(r["path"] == real for r in out):
            continue
        out.append({"name": str(name or os.path.basename(real.rstrip("/")) or real), "path": real})
    return out


def _root_of(real: str, roots: list[dict]) -> dict | None:
    best = None
    for root in roots:
        base = root["path"]
        if (real == base or real.startswith(base.rstrip("/") + "/")) \
                and (best is None or len(base) > len(best["path"])):
            best = root
    return best


def start_dir(roots) -> str | None:
    """Der Ordner, in dem die Auswahl beginnt: der Download-Ordner, wenn er unter den Wurzeln ist."""
    for root in _roots(roots):
        if os.path.basename(root["path"]).lower() in ("downloads", "download"):
            return root["path"]
    return None


def allowed_image(path, roots) -> bool:
    """Liegt die Datei (aufgelöst) unter einer Wurzel, nicht versteckt, mit Bild-Endung?"""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return False
    real = os.path.realpath(path)
    root = _root_of(real, _roots(roots))
    if root is None or not os.path.isfile(real) or not real.lower().endswith(IMAGE_SUFFIXES):
        return False
    inside = real[len(root["path"].rstrip("/")):].strip("/")
    return not any(part.startswith(".") for part in inside.split("/") if part)


def _fail(code: str, message: str, path) -> dict:
    return {"ok": False, "code": code, "message": message, "path": path, "parent": None, "name": "",
            "entries": [], "truncated": False, "total": 0}


def list_images(path: str | None, roots, limit: int = ENTRY_LIMIT) -> dict:
    """Ein Ordner der Bildauswahl. path None oder "" = die Liste der Wurzeln.

    -> {"ok", "path", "parent", "name", "entries": [{"name", "path", "type": "dir"|"image",
        "size", "mtime"}], "truncated", "total", "message"}; im Fehlerfall "ok": False mit
    "code" und einem deutschen Satz in "message". parent ist None an einer Wurzel."""
    roots = _roots(roots)
    if path is None or path == "":
        entries = [{"name": r["name"], "path": r["path"], "type": "dir", "size": None, "mtime": None}
                   for r in roots]
        return {"ok": True, "path": None, "parent": None, "name": "", "entries": entries,
                "truncated": False, "total": len(entries), "message": ""}
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return _fail("bad-path", "Dieser Ordner kann nicht angezeigt werden.", path)
    real = os.path.realpath(path)
    root = _root_of(real, roots)
    if root is None:
        return _fail("outside-roots", "Dieser Ordner liegt außerhalb der erlaubten Orte.", path)
    inside = real[len(root["path"].rstrip("/")):].strip("/")
    if any(part.startswith(".") for part in inside.split("/") if part):
        return _fail("hidden", "Versteckte Ordner werden nicht angezeigt.", path)
    if not os.path.isdir(real):
        return _fail("not-a-dir", "Dieser Ordner ist nicht vorhanden.", path)
    dirs, images, scanned, truncated = [], [], 0, False
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
                    target = os.path.realpath(entry.path) if entry.is_symlink() else entry.path
                    if entry.is_symlink() and _root_of(target, roots) is None:
                        continue                    # führt hinaus: nicht zeigen, nicht verfolgen
                    if os.path.isdir(target):
                        dirs.append(entry.name)
                    elif entry.name.lower().endswith(IMAGE_SUFFIXES) and os.path.isfile(target):
                        st = os.stat(target)
                        images.append((st.st_mtime, entry.name, st.st_size))
                except OSError:
                    continue
    except OSError:
        return _fail("unreadable", "Dieser Ordner kann nicht gelesen werden.", path)
    dirs.sort(key=str.casefold)
    images.sort(key=lambda item: (-item[0], item[1].casefold()))
    total = len(dirs) + len(images)
    if total > limit:
        truncated = True
    dirs = dirs[:limit]
    images = images[:max(0, limit - len(dirs))]
    entries = [{"name": name, "path": os.path.join(real, name), "type": "dir", "size": None, "mtime": None}
               for name in dirs]
    entries += [{"name": name, "path": os.path.join(real, name), "type": "image", "size": size, "mtime": mtime}
                for mtime, name, size in images]
    parent = None if real == root["path"] else os.path.dirname(real)
    message = "Es werden nur die ersten %d Einträge angezeigt." % len(entries) if truncated else ""
    return {"ok": True, "path": real, "parent": parent,
            "name": root["name"] if parent is None else os.path.basename(real),
            "entries": entries, "truncated": truncated, "total": total, "message": message}
