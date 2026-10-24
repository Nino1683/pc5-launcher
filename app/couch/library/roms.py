"""Quelle "rom" (E105): Emulator-Spiele aus den ROM-Ordnern <emu.root>/roms/<system>, ohne Steam ROM Manager.

Nur lesend, blockierend (je System ein Verzeichnis lesen). Liest nur, wenn library.roms an ist (im Bestand
setzt portable.migrate es einmal auf False), und nur die Systeme, deren Emulator im Katalog
(setup/catalog.py) library hat. Eine Ebene tief, nie rekursiv (also nie durch FUSE/NTFS hindurch), keine
Verweise: weder ein verlinkter System-Ordner noch verlinkte Dateien. Switch und Wii U nur, wenn es keine
SRM-Konfiguration gibt; sonst liefert die Quelle "emulators" sie (keine Dubletten).

Ergebnis je Datei:
  Entry(id="rom:<system>:<10 Hex-Zeichen sha1 des realpath>", source="rom", kind="game",
        badge=Kurzname des Systems („GAMECUBE“, „PS2“ …), installed=Emulator-Flatpak installiert,
        detail={system, rom, emulator (Katalog-ID), flatpak, root (Emulator-Ordner), alternatives})
Titel aus dem Dateinamen (Klammerzusätze wie „(Europe)“ weg). Eine .m3u verdeckt die Dateien, die sie nennt,
und .cue/.chd desselben Namens; Mehrteiliges („(Disc 1)“, „(Disc 2)“) erscheint einmal.

Einstellungen (library.roms, emu.root, library.games_disk) kommen über couch.setup.setting (gebunden von
wiring_setup); Tests können am Kontext roms_enabled, emu_root und flatpak_installed setzen.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat

from .model import Entry
from .sources import clean_text, sort_title

MAX_FILES = 5000                      # je System; mehr liest die Quelle nicht (Zeitbudget)
_DISC = re.compile(r"\s*[\(\[]\s*(disc|disk|cd|dvd)\s*\d+\s*(of\s*\d+)?\s*[\)\]]", re.I)


def rom_id(system: str, path: str) -> str:
    real = os.path.realpath(path)
    return "rom:%s:%s" % (system, hashlib.sha1(real.encode("utf-8", "surrogateescape")).hexdigest()[:10])


def enabled(ctx) -> bool:
    """Liest die Quelle gerade (library.roms; am Kontext übersteuerbar)? Auch für den Index: Eine
    ausgeschaltete Quelle zählt nicht als gesehen (seen_sources, E108)."""
    from .. import setup
    value = getattr(ctx, "roms_enabled", None)
    return value if isinstance(value, bool) else bool(setup.flag("library.roms", True))


def _settings(ctx) -> tuple:
    """(an?, emu.root-Einstellung, library.games_disk-Einstellung)."""
    from .. import setup
    root = getattr(ctx, "emu_root", None)
    if not isinstance(root, str):
        root = setup.abspath_setting("emu.root")
    disk = setup.abspath_setting("library.games_disk")
    return enabled(ctx), root, disk


def has_srm(ctx) -> bool:
    return os.path.isfile(os.path.join(ctx.srm_dir, "userConfigurations.json"))


def emu_root(ctx) -> str:
    """Der Emulator-Ordner dieses Kontexts (setup/folders.emu_root); "" = keiner bekannt."""
    from ..setup import folders
    _enabled, setting, disk = _settings(ctx)
    return folders.emu_root(setting, ctx.home, disk or ctx.games_disk, ctx.srm_dir)


def flatpak_installed(ctx, app_id: str) -> bool:
    fn = getattr(ctx, "flatpak_installed", None)
    if callable(fn):
        return bool(fn(app_id))
    return any(os.path.isdir(os.path.join(root, "app", app_id, "current", "active")) for root in ctx.flatpak_dirs)


def clean_title(name: str) -> str:
    from .emulators import clean_title as clean
    return clean(_DISC.sub("", name))


def _files(folder: str, exts: tuple) -> list:
    """Reguläre Dateien direkt in folder mit passender Endung, sortiert. Verweise zählen nicht."""
    out = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if len(out) >= MAX_FILES:
                    break
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                low = entry.name.lower()
                if any(low.endswith(e) for e in exts):
                    out.append(entry.name)
    except OSError:
        return []
    return sorted(out, key=str.casefold)


def _m3u_hidden(folder: str, names: list) -> set:
    """Was eine .m3u im selben Ordner nennt (nur Dateinamen dort) und .cue/.chd mit ihrem Namen."""
    hidden: set = set()
    for name in names:
        if not name.lower().endswith(".m3u"):
            continue
        stem = name[:-4]
        for other in names:
            if other != name and other.rsplit(".", 1)[0] == stem and other.lower().endswith((".cue", ".chd")):
                hidden.add(other)
        try:
            with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as fh:
                for line in fh.read(64 * 1024).splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "/" not in line:
                        hidden.add(line)
        except OSError:
            continue
    return hidden


def _system_dir(roms: str, system: str) -> str | None:
    path = os.path.join(roms, system)
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return path if stat.S_ISDIR(st.st_mode) else None          # kein Verweis, kein Datei-Name


def load(ctx) -> list:
    """Einträge der ROM-Ordner. Blockierend; wirft nie, Probleme stehen in ctx.warnings."""
    from ..setup import catalog
    enabled, _setting, _disk = _settings(ctx)
    if not enabled:
        return []
    root = emu_root(ctx)
    if not root:
        return []
    roms = os.path.join(root, catalog.ROMS_DIR)
    try:
        st = os.lstat(roms)
    except OSError:
        return []
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        ctx.warn("roms: %s ist kein Ordner (Verweise liest die Quelle nicht)" % roms)
        return []
    srm = has_srm(ctx)
    out = []
    for system, emus in catalog.library_systems(srm).items():
        folder = _system_dir(roms, system)
        if folder is None:
            continue
        exts = catalog.extensions(system, srm)
        names = _files(folder, exts)
        if len(names) >= MAX_FILES:
            ctx.warn("roms: %s hat mehr als %d Dateien; nur die ersten erscheinen" % (folder, MAX_FILES))
        hidden = _m3u_hidden(folder, names) if any(n.lower().endswith(".m3u") for n in names) else set()
        emu = next((e for e in emus if flatpak_installed(ctx, e["flatpak"])), emus[0])
        installed = flatpak_installed(ctx, emu["flatpak"])
        seen: dict = {}
        badge = catalog.system_badge(system)
        for name in names:
            if name in hidden:
                continue
            path = os.path.join(folder, name)
            raw = name.rsplit(".", 1)[0]
            title = clean_text(clean_title(raw), raw)
            key = title.casefold()
            if key in seen:
                seen[key].detail["alternatives"].append(path)       # weitere Scheibe, gleiche Fassung
                continue
            entry = Entry(id=rom_id(system, path), title=title, kind="game", source="rom", badge=badge,
                          sort_title=sort_title(title), available=True, installed=installed,
                          detail={"system": system, "rom": path, "emulator": emu["id"], "flatpak": emu["flatpak"],
                                  "root": root, "title_raw": raw, "alternatives": []})
            if not installed:
                entry.detail["hint"] = "%s ist nicht installiert." % emu["title"]
                if emu.get("offer"):          # nur Angebotenes; Eden/Ryujinx (Frage 3) nur mit Hinweis
                    entry.detail["setup_id"] = emu["id"]
                    entry.tags.append("setup:" + emu["id"])
            seen[key] = entry
            out.append(entry)
    out.sort(key=lambda e: (e.detail["system"], e.sort_title))
    return out
