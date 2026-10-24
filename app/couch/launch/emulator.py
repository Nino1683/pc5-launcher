"""Emulator-Spiele (Bauplan E13, E105).

Switch (Eden) und Wii U (Cemu) aus dem Steam ROM Manager: über eigene Starter des Nutzers (~/.local/bin/eden-launch
und cemu-launch) mit --raw (sie setzen die Controller-Zuordnung und starten das Flatpak; E13 gilt dort weiter).
EmuDecks Startskripte unter Emulation/tools/launchers werden nie benutzt (zwei git pull je Start,
überschreiben sich selbst, können Emulatoren nachinstallieren; 06 Abschnitt 4.4). Fehlen diese Starter,
startet derselbe Flatpak-Weg wie bei der Quelle "rom".

Quelle "rom" (E105, ROM-Ordner ohne SRM): direkt über das Flatpak des Katalog-Emulators,
    flatpak run --socket=x11 --nosocket=wayland --filesystem=<emu.root>:ro <flatpak> <args>
mit den Katalog-Argumenten ({rom}, {core}). Der Emulator darf den Emulator-Ordner nur lesen, nichts bleibt
zurück (keine dauerhafte Freigabe). Nie --file-forwarding: das geht über das Dokumenten-Portal (FUSE), der Schaden vom
2026-10-12 lief über so eine Einhängung. RetroArch: Kern aus
~/.var/app/org.libretro.RetroArch/config/retroarch/cores/<kern>_libretro.so (nur gelesen; geladen wird er in
RetroArch selbst). Startargumente sind unbestätigt (am Fernseher je Emulator zu prüfen).

Blockierend: build() mit check=True und preflight() fragen die Platte ab (stat).
"""
from __future__ import annotations

import os

from . import flatpak
from .native import clean_env, expand, missing, split_args
from .spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec

EMULATORS = {
    "eden": {"starter": ".local/bin/eden-launch", "flatpak": "dev.eden_emu.eden", "label": "Eden"},
    "cemu": {"starter": ".local/bin/cemu-launch", "flatpak": "info.cemu.Cemu", "label": "Cemu"},
}
_BY_SOURCE = {"switch": "eden", "wiiu": "cemu"}
RA_CORES = ".var/app/org.libretro.RetroArch/config/retroarch/cores"
FORBIDDEN_FLAGS = ("--file-forwarding",)
TEXT_NO_EMULATOR = "Für dieses System ist kein Emulator eingerichtet."


def _emulator(entry) -> tuple:
    name = str(entry.detail.get("emulator") or _BY_SOURCE.get(entry.source) or "").lower()
    if name not in EMULATORS:
        raise LaunchError(TEXT_NO_EMULATOR, "emulator=%r system=%r" % (name, entry.detail.get("system")),
                          reason="no-launcher")
    return name, EMULATORS[name]


def _rom(entry, ctx) -> str:
    rom = expand(entry.detail.get("rom") or "", ctx.home)
    if not rom:
        raise LaunchError("Für dieses Spiel ist keine Spieldatei festgelegt.", reason="no-exe")
    return rom


def _rpx_in(folder: str) -> str | None:
    """Wii-U-Spiele sind Ordner; Cemu will die .rpx unter code/."""
    code = os.path.join(folder, "code")
    try:
        found = sorted(n for n in os.listdir(code) if n.lower().endswith(".rpx"))
    except OSError:
        return None
    return os.path.join(code, found[0]) if len(found) == 1 else None


def _starter_ok(ctx, emu: dict) -> bool:
    starter = os.path.join(ctx.home, emu["starter"])
    return os.path.isfile(starter) and os.access(starter, os.X_OK)


# ---------------------------------------------------------------- Flatpak-Weg (E105)
def _catalog_emu(entry) -> dict:
    """Katalog-Emulator eines Eintrags: detail.emulator, bei switch/wiiu Eden bzw. Cemu."""
    from ..setup import catalog
    name = str(entry.detail.get("emulator") or _BY_SOURCE.get(entry.source) or "").lower()
    emu = catalog.emulator(name)
    if emu is None or not emu["args"]:
        raise LaunchError(TEXT_NO_EMULATOR, "emulator=%r system=%r" % (name, entry.detail.get("system")),
                          reason="no-launcher")
    return emu


def _fs_root(entry, ctx, rom: str) -> str:
    """Ordner, den der Emulator lesen darf: der Emulator-Ordner, wenn die Spieldatei darunter liegt, sonst
    der Ordner des Spiels (Wii U: Titelordner) bzw. der Datei."""
    candidates = [expand(str(entry.detail.get("root") or ""), ctx.home)]
    if entry.source in _BY_SOURCE:
        from .. import setup
        from ..setup import folders
        candidates.append(folders.emu_root(setup.abspath_setting("emu.root"), ctx.home, None))
    real = os.path.realpath(rom)
    for root in candidates:
        if root and os.path.isabs(root):
            top = os.path.realpath(root)
            if real.startswith(top.rstrip(os.sep) + os.sep):
                return os.path.normpath(root)
    game_dir = expand(str(entry.detail.get("game_dir") or ""), ctx.home)
    if game_dir and os.path.isabs(game_dir):
        return os.path.normpath(game_dir)
    return os.path.dirname(os.path.normpath(rom))


def _core(entry, ctx, emu: dict) -> str:
    """RetroArch-Kern für das System (erster vorhandene aus dem Katalog). LaunchError, wenn keiner da ist."""
    from ..setup import catalog
    system = str(entry.detail.get("system") or "")
    names = emu["cores"].get(system) or ()
    folder = os.path.join(ctx.home, RA_CORES)
    for name in names:
        path = os.path.join(folder, "%s_libretro.so" % name)
        if os.path.isfile(path):
            return path
    raise LaunchError("RetroArch hat noch keinen Kern für %s. Öffne RetroArch unter Einstellungen › Spiele › "
                      "Emulatoren und lade ihn über Online-Aktualisierung › Kerne." % catalog.system_title(system),
                      "Kerne %s fehlen in %s" % ("/".join(names) or "?", folder), reason="no-core")


def _flatpak_plan(entry, ctx, check: bool) -> tuple:
    emu = _catalog_emu(entry)
    rom = _rom(entry, ctx)
    if emu["id"] == "cemu" and os.path.isdir(rom):
        rom = _rpx_in(rom) or rom
    core = ""
    if "{core}" in emu["args"]:
        try:
            core = _core(entry, ctx, emu)
        except LaunchError:
            if check:
                raise
            core = os.path.join(ctx.home, RA_CORES, "%s_libretro.so" % ((emu["cores"].get(
                str(entry.detail.get("system") or "")) or ("?",))[0]))
    args = [a.replace("{rom}", rom).replace("{core}", core) for a in emu["args"]]
    args += split_args(entry.option("args"))
    fs = "--filesystem=%s:ro" % _fs_root(entry, ctx, rom)
    argv = flatpak.run_argv(emu["flatpak"], *args, flatpak_args=(fs,))
    if any(a in FORBIDDEN_FLAGS or str(a).startswith(FORBIDDEN_FLAGS) for a in argv):
        raise LaunchError("Dieser Start ist nicht erlaubt.", "--file-forwarding", reason="no-launcher")
    return emu, rom, argv


def _flatpak_preflight(entry, ctx) -> list:
    emu, rom, _argv = _flatpak_plan(entry, ctx, check=True)
    if not flatpak.is_installed(emu["flatpak"], ctx):
        raise LaunchError("%s ist nicht installiert." % emu["title"], "Flatpak %s fehlt" % emu["flatpak"],
                          reason="not-installed")
    if not os.path.exists(rom):
        raise missing(rom, ctx, "Die Spieldatei")
    return []


def _uses_flatpak(entry, ctx) -> bool:
    """Quelle rom immer; switch/wiiu nur ohne eigene Starter des Nutzers, und nur wenn das Flatpak da ist."""
    if entry.source == "rom":
        return True
    name, emu = _emulator(entry)
    return not _starter_ok(ctx, emu) and flatpak.is_installed(emu["flatpak"], ctx)


# ---------------------------------------------------------------- Eigene Starter (E13)
def _plan(entry, ctx) -> tuple:
    name, emu = _emulator(entry)
    starter = os.path.join(ctx.home, emu["starter"])
    rom = _rom(entry, ctx)
    if name == "cemu" and os.path.isdir(rom):
        rom = _rpx_in(rom) or rom
    argv = [starter, "--raw", "-f", "-g", rom, *split_args(entry.option("args"))]
    return name, emu, starter, rom, argv


def preflight(entry, ctx) -> list:
    """Harte Fehler als LaunchError: Starter fehlt (und kein Flatpak), Emulator nicht installiert, Kern
    fehlt, Spieldatei fehlt bzw. Platte nicht eingehängt. Rückgabe: Hinweise. Blockierend (Platte)."""
    if _uses_flatpak(entry, ctx):
        return _flatpak_preflight(entry, ctx)
    _name, emu, starter, rom, _argv = _plan(entry, ctx)
    if not os.path.isfile(starter) or not os.access(starter, os.X_OK):
        raise LaunchError("Der Emulator-Starter fehlt: %s" % starter, reason="not-installed")
    if not flatpak.is_installed(emu["flatpak"], ctx):
        raise LaunchError("%s ist nicht installiert." % emu["label"], "Flatpak %s fehlt" % emu["flatpak"],
                          reason="not-installed")
    if not os.path.isfile(rom):
        raise missing(rom, ctx, "Die Spieldatei")
    return []


def build(entry, ctx, *, check: bool = True) -> LaunchSpec:
    """Entry (Quelle "switch", "wiiu" oder "rom") -> LaunchSpec.

    switch/wiiu mit Starter: argv [~/.local/bin/eden-launch, "--raw", "-f", "-g", <rom>] bzw. cemu-launch … <rpx>.
    Sonst: flatpak run … --filesystem=<emu.root>:ro <flatpak> <Katalog-Argumente>.
    """
    if _uses_flatpak(entry, ctx):
        _emu, _rom_path, argv = _flatpak_plan(entry, ctx, check)
    else:
        _name, _emu, _starter, _rom_path, argv = _plan(entry, ctx)
    if check:
        preflight(entry, ctx)
    return LaunchSpec(
        entry_id=entry.id,
        argv=argv,
        env=clean_env(entry.option("env")),
        # Mit SteamGameId/SteamAppId in der Umgebung stellen die Starter auf Steams virtuelles
        # Pad um, das es in unserer Sitzung nicht gibt
        unset=list(STEAM_LEFTOVERS),
        cwd=ctx.home,
        kind="emulator",
        freeze=bool(entry.option("freeze")),
        mouse=False,
        polite_stop=flatpak.stop_argv(entry.id, ctx),
        title=entry.title,
    )
