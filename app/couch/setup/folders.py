"""Emulator-Ordner (E105, Bauplan 4.6 und 8.8): wo er liegt und wie roms/<system> und bios entstehen.

emu_root(): Einstellung emu.root (absolut), sonst der EmuDeck-Ordner (Elternordner von romsDirectory des
Steam ROM Managers, sonst emulationPath aus ~/.config/EmuDeck/settings.sh; beides nur gelesen), sonst
<Spiele-Ort>/Emulation.

make(): nur mkdir, Stufe für Stufe, nur unter dem gewählten Ort (realpath-Vergleich), ohne Verweisen
zu folgen; Ergebnis ist die Liste der selbst angelegten Ordner (kommt nach setup.json). undo(): nur
rmdir leerer, selbst angelegter Ordner, tiefste zuerst, nie rekursiv (SAFETY 13). Bei einem
EmuDeck-Ordner legt der Assistent dort nichts an; Hinweisdateien in ROM-Ordnern gibt es nicht.
"""
from __future__ import annotations

import json
import os
import re
import stat

from . import catalog

_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def emudeck_settings(home: str) -> dict:
    """Schlüssel=Wert aus ~/.config/EmuDeck/settings.sh (nur gelesen, nichts wird ausgeführt)."""
    out: dict = {}
    try:
        with open(os.path.join(home, ".config/EmuDeck/settings.sh"), encoding="utf-8", errors="replace") as fh:
            for line in fh.read(256 * 1024).splitlines():
                m = _LINE.match(line)
                if m:
                    out[m.group(1)] = m.group(2).strip().strip("\"'")
    except OSError:
        pass
    return out


def emudeck_path(home: str) -> str:
    """emulationPath von EmuDeck ("" = kein EmuDeck oder kein absoluter Pfad)."""
    value = emudeck_settings(home).get("emulationPath") or ""
    return os.path.normpath(value) if value and os.path.isabs(value) else ""


def srm_root(home: str, srm_dir: str | None = None) -> str:
    """Emulator-Ordner aus Steam ROM Manager: Elternordner von romsDirectory, wenn der „roms“ heißt."""
    path = os.path.join(srm_dir or os.path.join(home, ".config/steam-rom-manager/userData"), "userSettings.json")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            roms = (json.load(fh).get("environmentVariables") or {}).get("romsDirectory")
    except (OSError, ValueError, AttributeError):
        return ""
    if isinstance(roms, str) and os.path.isabs(roms) and os.path.basename(roms.rstrip("/")) == catalog.ROMS_DIR:
        return os.path.dirname(os.path.normpath(roms))
    return ""


def emudeck_root(home: str, srm_dir: str | None = None) -> str:
    """Vorhandener EmuDeck-Ordner: aus der SRM-Konfiguration (romsDirectory; so liest ihn auch die Quelle
    „emulators“), sonst emulationPath aus settings.sh. Auf dem Entwicklungsrechner nennt settings.sh
    noch einen alten Ort, die SRM-Konfiguration den aktuellen (gelesen 2026-10-24)."""
    return srm_root(home, srm_dir) or emudeck_path(home)


def emu_root(setting: str, home: str, games_disk: str | None, srm_dir: str | None = None) -> str:
    """Der Emulator-Ordner nach 4.6 ("" nur, wenn es gar keinen Anhaltspunkt gibt)."""
    if setting and os.path.isabs(setting):
        return os.path.normpath(setting)
    found = emudeck_root(home, srm_dir)
    if found:
        return found
    return os.path.join(games_disk, "Emulation") if games_disk else ""


def roms_dir(root: str) -> str:
    return os.path.join(root, catalog.ROMS_DIR) if root else ""


def wanted(root: str, systems) -> list:
    """Ordner, die für diese Systeme da sein sollen (roms, roms/<system> …, bios), ohne Prüfung."""
    if not root:
        return []
    out = [roms_dir(root)]
    for system in systems:
        if system in catalog.SYSTEMS and os.path.join(roms_dir(root), system) not in out:
            out.append(os.path.join(roms_dir(root), system))
    out.append(os.path.join(root, catalog.BIOS_DIR))
    return out


class FolderError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


def _under(path: str, base: str) -> bool:
    real, top = os.path.realpath(path), os.path.realpath(base)
    return real == top or real.startswith(top.rstrip(os.sep) + os.sep)


def _mkdir_steps(target: str, base: str, created: list) -> None:
    """target unter base anlegen, jede fehlende Stufe einzeln, nie durch einen Verweis."""
    base = os.path.normpath(base)
    rel = os.path.relpath(os.path.normpath(target), base)
    if rel == "." or rel.startswith(".."):
        raise FolderError("Der Ordner %s liegt nicht unter %s." % (target, base))
    current = base
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current, 0o755)
            except FileExistsError:
                st = os.lstat(current)
            else:
                created.append(current)
                continue
        if stat.S_ISLNK(st.st_mode):
            raise FolderError("%s ist ein Verweis. Der Assistent folgt keinen Verweisen." % current)
        if not stat.S_ISDIR(st.st_mode):
            raise FolderError("%s ist kein Ordner." % current)
        if not _under(current, base):
            raise FolderError("Der Ordner %s liegt nicht unter %s." % (current, base))


def make(root: str, systems, base: str, emudeck: str = "") -> list:
    """roms/<system> und bios unter root anlegen. base: Wurzel des gewählten Orts (Einhängepunkt bzw.
    für den internen Speicher der persönliche Ordner), unter der root liegen muss; sie selbst muss
    schon da sein (eingehängt) und wird nie angelegt, Zwischenstufen wie ~/Games schon. emudeck: der
    EmuDeck-Ordner, dort entsteht nichts. Rückgabe: angelegte Ordner (in Reihenfolge). FolderError
    mit deutschem Satz, wenn etwas nicht passt; was bis dahin entstand, steht in err.created."""
    created: list = []
    if not root or not base:
        raise FolderError("Es ist kein Ort für die Emulator-Spiele gewählt.")
    if emudeck and os.path.realpath(root) == os.path.realpath(emudeck):
        return created                          # EmuDeck-Ordner bleiben, wie sie sind
    try:
        st = os.lstat(base)
    except OSError:
        raise FolderError("Der gewählte Ort ist nicht erreichbar (%s)." % base) from None
    if stat.S_ISLNK(st.st_mode) and not os.path.isdir(base):
        raise FolderError("Der gewählte Ort ist nicht erreichbar (%s)." % base)
    if not os.path.isdir(base):
        raise FolderError("Der gewählte Ort ist kein Ordner (%s)." % base)
    # erst nach dem Namen (root gibt es oft noch nicht); jede vorhandene Stufe prüft _mkdir_steps
    # zusätzlich über realpath und lstat
    nroot, nbase = os.path.normpath(root), os.path.normpath(base)
    if nroot != nbase and not nroot.startswith(nbase.rstrip(os.sep) + os.sep):
        raise FolderError("Der Emulator-Ordner muss am gewählten Ort liegen.")
    try:
        if os.path.normpath(root) != os.path.normpath(base):
            _mkdir_steps(root, base, created)
        for folder in wanted(root, systems):
            _mkdir_steps(folder, root, created)
    except FolderError as e:
        e.created = list(created)               # type: ignore[attr-defined]
        raise
    except OSError as e:
        err = FolderError("Der Ordner ließ sich nicht anlegen: %s" % (e.strerror or e))
        err.created = list(created)             # type: ignore[attr-defined]
        raise err from e
    return created


def undo(created: list) -> list:
    """Selbst angelegte Ordner wieder entfernen, nur wenn leer und kein Verweis (rmdir, tiefste zuerst).
    Rückgabe: was entfernt wurde. Was nicht leer ist, bleibt."""
    gone = []
    for path in sorted({os.path.normpath(p) for p in created if isinstance(p, str) and os.path.isabs(p)},
                       key=lambda p: p.count(os.sep), reverse=True):
        try:
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                continue
            os.rmdir(path)
            gone.append(path)
        except OSError:
            continue
    return gone
