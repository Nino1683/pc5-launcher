"""Windows-Spiele mit wine-run starten statt mit Proton (Wunsch 2026-10-28: „Dann mache es bitte möglich, Spiele
auch mit wine-run zu starten im PC5“).

wine-run ist ein eigenes Skript des Nutzers (~/.local/bin/wine-run; im Desktop dasselbe wie der Doppelklick auf eine exe
bzw. „Mit Wine ausführen“): ein Wine-Staging-Build mit DXVK und VKD3D in einem gemeinsamen Prefix, Vorgabe
~/Games/prefixes/windows. Es gehört nicht zum PC5 Launcher; die Option runner = "wine-run" bietet die Seite nur an,
wenn es das Skript gibt (script()). Ohne Skript startet ein so eingestelltes Spiel nicht (LaunchError mit Satz),
statt still auf Proton zu wechseln.

Gestartet wird wie im Desktop: Startdatei als erstes Argument, Arbeitsordner der Ordner der Startdatei, ohne
WINEPREFIX (das Skript nimmt seinen eigenen Prefix). Nie im Prefix des Spiels unter Proton: Der besteht aus
Verweisen in den Proton-Ordner, ein anderes Wine schriebe durch sie hindurch (E101, 2026-10-28). Proton-Schalter
(Proton-Version, FSR 4, HDR, OptiScaler, DXVK) und die Übernahme eines Prefix gelten hier nicht; Startoptionen
(Option args), Option env, Anhalten und die Pad-Modi Tastatur/Maus und Zeiger schon.

Nur Rechnen und stat; blockierend wie native.py (Platte), im Arbeits-Thread aufrufen.
"""
from __future__ import annotations

import os
import shutil

from .. import paths
from .native import clean_env, require_exe, require_file
from .spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec, pad_mode

RUNNER = "wine-run"
SCRIPT = os.path.join(".local", "bin", "wine-run")      # unter dem Home
TEXT_MISSING = ("Dieses Spiel ist auf „Starten mit: wine-run“ gestellt, aber ~/.local/bin/wine-run fehlt. "
                "Stell es in den Spieleinstellungen auf Proton.")
# Würden das Skript von seinem Prefix und seinem Wine abbringen (geerbt aus Steam, Lutris oder einer Sitzung)
UNSET = ("WINEPREFIX", "WINEARCH", "WINELOADER", "WINESERVER", "WINEDLLPATH", "PROTONPATH", "STEAM_COMPAT_DATA_PATH")


def script(home: str) -> str | None:
    """Pfad zu wine-run oder None: <home>/.local/bin/wine-run, sonst aus dem PATH (nicht im Prüfmodus; dort nur
    das Skript im Attrappen-Home). Schnell (stat)."""
    path = os.path.join(home, SCRIPT)
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    if paths.test_mode():
        return None
    return shutil.which("wine-run")


def wanted(entry) -> bool:
    """Ist dieses Spiel auf wine-run gestellt? Nur eigene Windows-Spiele (Quelle proton bzw. local mit platform
    windows), nicht Epic und nicht Spiele über einen Hersteller-Launcher: Die brauchen ihren Prefix."""
    if entry.option("runner") != RUNNER:
        return False
    d = entry.detail
    if entry.source == "proton":
        return True
    return (entry.source == "local" and str(d.get("platform") or "") == "windows"
            and not d.get("epic") and not d.get("launcher") and not d.get("launcher_app"))


def _plan(entry, ctx) -> tuple:
    from .proton import game_paths           # dieselbe Startdatei samt Startoptionen (Option args) wie unter Proton
    return game_paths(entry, ctx)


def preflight(entry, ctx) -> list:
    """Harte Fehler als LaunchError (Skript fehlt, Startdatei fehlt bzw. Platte weg). Blockierend (Platte)."""
    tool = script(ctx.home)
    if not tool:
        raise LaunchError(TEXT_MISSING, os.path.join(ctx.home, SCRIPT), reason="tool-missing")
    require_file(tool, ctx, "wine-run", executable=True)
    exe, _args, cwd = _plan(entry, ctx)
    require_exe(entry, exe, ctx)
    if cwd and not os.path.isdir(cwd):
        return ["Der Arbeitsordner fehlt (%s); gestartet wird im Ordner der Startdatei." % cwd]
    return []


def build(entry, ctx, *, check: bool = True) -> LaunchSpec:
    """Entry -> LaunchSpec: [wine-run, startdatei, *argumente]. check=True prüft vorher (blockierend)."""
    if check:
        preflight(entry, ctx)
    exe, args, cwd = _plan(entry, ctx)
    if not cwd or (check and not os.path.isdir(cwd)):
        cwd = os.path.dirname(exe)
    tool = script(ctx.home) or os.path.join(ctx.home, SCRIPT)
    return LaunchSpec(
        entry_id=entry.id,
        argv=[tool, exe, *args],
        env=clean_env(entry.option("env")),
        unset=list(STEAM_LEFTOVERS) + [n for n in UNSET if n not in STEAM_LEFTOVERS],
        cwd=cwd or None,
        kind="game",
        freeze=bool(entry.option("freeze")),
        mouse=pad_mode(entry),
        polite_stop=None,
        title=entry.title,
    )
