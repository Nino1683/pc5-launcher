"""Startweg „wie in Lutris“ (Bauplan E55) für Lutris-Spiele, die mit Proton nicht starten.

couch startet Windows-Spiele aus Lutris sonst mit umu-run und Proton-CachyOS in einer Kopie des
Prefix (E15). Manche laufen so nicht, wohl aber in Lutris (gesehen 2026-10-15: „Tomb Raider“
endete unter Proton nach 0,3 s ohne Fehler, mit wine-11.16-staging aus Lutris lief es). Dann
startet couch das Spiel genau so, wie Lutris es tut: Lutris schreibt dafür sein Startskript
(`lutris -b <slug>`: Umgebung, Arbeitsordner, Wine-Aufruf mit seinem Runner und seinem Prefix).
couch ändert an Lutris nichts; das Skript entsteht in einem eigenen Ordner neu bei jedem Start
(so gelten Änderungen in Lutris gleich), mit dem zuletzt erzeugten als Rückfall.

Im Prüfmodus läuft nie das echte Lutris: nur COUCH_TEST_LUTRIS_CMD (Attrappe), sonst Fehler.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile

from .spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec, pad_mode

OPTION = "launcher"                 # Option je Spiel: "auto" (Proton) | "lutris"
TEXT_NOT_POSSIBLE = "Dieses Spiel lässt sich nicht „wie in Lutris“ starten."
TEXT_NO_LUTRIS = "Lutris ist nicht installiert; „wie in Lutris“ lässt sich nicht starten."
TEXT_NO_SCRIPT = "Lutris hat keinen Startbefehl für „%s“ geliefert."
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_PREFIX = re.compile(r'^export WINEPREFIX="([^"]+)"\s*$', re.M)
SCRIPT_TIMEOUT = 45.0
# Nichts aus der Sitzung, was Wine oder dem Spiel einen anderen Weg vorgibt (Steam, Proton, umu): die
# gemeinsame Liste der Steam-Reste (spec.py, E99) und die Schalter des Proton-Wegs
UNSET = list(dict.fromkeys([*STEAM_LEFTOVERS, "PROTONPATH", "GAMEID", "STORE", "PROTON_FSR4_UPGRADE",
                            "FSR4_UPGRADE", "PROTON_PREFER_SDL", "DXVK_HDR", "PROTON_USE_OPTISCALER"]))


def possible(entry) -> bool:
    """Windows-Spiel aus dem nativen Lutris (Wine-Runner) mit bekanntem Slug. Aus Lutris als Flatpak
    (detail.lutris_flatpak, E104) nie: Dessen Spiele kennt ein natives ``lutris -b`` nicht."""
    return (entry.source == "proton" and str(entry.id).startswith("lutris:")
            and not entry.detail.get("lutris_flatpak")
            and bool(_SLUG.match(str(entry.detail.get("slug") or ""))))


def wanted(entry) -> bool:
    return possible(entry) and entry.option(OPTION) == "lutris"


def lutris_command() -> list | None:
    """argv-Anfang für "lutris -b". Im Prüfmodus nur die Attrappe."""
    fake = os.environ.get("COUCH_TEST_LUTRIS_CMD", "")
    if fake:
        return shlex.split(fake)
    if os.environ.get("COUCH_TEST_ROOT"):
        return None
    found = shutil.which("lutris")
    return [found] if found else None


def script_dir(ctx) -> str:
    return os.path.join(ctx.data_dir, "lutris-scripts")


def script_path(entry, ctx) -> str:
    return os.path.join(script_dir(ctx), str(entry.detail.get("slug")) + ".sh")


def _valid(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return bool(_PREFIX.search(text)) and bool(lines) and not lines[-1].startswith(("export ", "cd "))


def _drop_work(work: str, slug: str) -> None:
    """Wegwerf-Ordner von `lutris -b` abräumen: nur das erwartete Skript, dann den leeren Ordner.
    Keine zweite rekursive Löschstelle (SAFETY 13, E99): Liegt sonst etwas darin, bleibt er liegen."""
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(work, slug + ".sh"))
    with contextlib.suppress(OSError):
        os.rmdir(work)


def ensure_script(entry, ctx) -> str:
    """Lässt Lutris das Startskript schreiben (blockierend, wenige Sekunden). Scheitert das, gilt
    das zuletzt erzeugte. Wirft LaunchError, wenn es keins gibt."""
    slug = str(entry.detail.get("slug") or "")
    if not _SLUG.match(slug):
        raise LaunchError(TEXT_NOT_POSSIBLE, "slug=%r" % slug, reason="no-launcher")
    path = script_path(entry, ctx)
    cmd = lutris_command()
    problem = ""
    if cmd is None:
        problem = "lutris fehlt"
    else:
        work = tempfile.mkdtemp(prefix="couch-lutris-")
        try:
            # Mit DISPLAY: Lutris richtet beim Start sein Farbschema über GTK ein und stürzt ohne
            # Anzeige ab (2026-10-15 in der echten Sitzung); ein Fenster öffnet es mit -b nicht
            env = dict(os.environ)
            done = subprocess.run(cmd + ["-b", slug], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=SCRIPT_TIMEOUT, check=False)
            made = os.path.join(work, slug + ".sh")
            text = ""
            if os.path.isfile(made):
                with open(made, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            if done.returncode == 0 and _valid(text):
                os.makedirs(script_dir(ctx), exist_ok=True)
                tmp = path + ".neu"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.chmod(tmp, 0o700)
                os.replace(tmp, path)
                return path
            said = (done.stderr or done.stdout or "").strip().splitlines()
            problem = "lutris -b: Rückgabewert %s, %s" % (done.returncode, said[-1] if said else "kein Skript")
        except (OSError, subprocess.TimeoutExpired) as err:
            problem = "lutris -b: %s" % err
        finally:
            _drop_work(work, slug)
    if os.path.isfile(path):
        return path                                    # Rückfall: das zuletzt erzeugte Skript
    if cmd is None:
        raise LaunchError(TEXT_NO_LUTRIS, problem, reason="not-installed")
    raise LaunchError(TEXT_NO_SCRIPT % entry.title, problem, reason="no-launcher")


def build(entry, ctx, *, check: bool = True) -> LaunchSpec:
    if not possible(entry):
        raise LaunchError(TEXT_NOT_POSSIBLE, str(entry.id), reason="no-launcher")
    path = ensure_script(entry, ctx) if check else script_path(entry, ctx)
    prefix = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = _PREFIX.search(f.read())
        prefix = m.group(1) if m else ""
    except OSError:
        pass
    prelaunch = []
    if prefix:
        from . import proton             # Vorab-Befehl aus Lutris wie beim Proton-Weg, mit dem Lutris-Prefix
        prelaunch = proton._prelaunch(entry, ctx, prefix, [])
    cwd = str(entry.detail.get("working_dir") or "") or None
    return LaunchSpec(entry_id=entry.id, argv=["/usr/bin/bash", path], unset=list(UNSET),
                      cwd=cwd if cwd and os.path.isdir(cwd) else None, kind="game", freeze=True,
                      mouse=pad_mode(entry), prelaunch=prelaunch, title=entry.title)
