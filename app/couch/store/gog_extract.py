"""GOG-Installer ohne Wine auspacken, wenn der stille Lauf unter Proton scheitert (E131).

Befund 2026-10-27 (Pizza Connection, Inno Setup 5.5.9, GOG-Installer von 2018): Mit /VERYSILENT wie mit /SILENT
endet der Installer nach 3 s mit Rückgabewert 1, sein Protokoll sagt „Current Page: Welcome – Failed to proceed
to next wizard page; aborting“. Das Skript des Installers lässt die Willkommensseite unter Wine nicht weiter,
egal mit welchem Ziel und welcher Sprache. Linux-Programme für GOG (Lutris, Minigalaxy) packen solche Installer
deshalb mit innoextract aus; das macht der Launcher dann auch.

innoextract kommt nur vom System: aus dem PATH oder aus der Laufzeit von Lutris (nativ und als Flatpak), die es
ohnehin für GOG-Spiele lädt. Der Launcher lädt selbst kein Programm aus dem Netz. Im Prüfmodus gilt nur die
Attrappe aus COUCH_TEST_INNOEXTRACT.

Ablauf: in einen Zwischenordner im eigenen Spielordner auspacken (eine Sprache: Deutsch, sonst Englisch), alles
außer den Hilfsordnern des Installers in den Spielordner verschieben, dazu __support/app (Einstellungen, z. B.
dosbox_*.conf) und __support/save (Anfangsstand der Spielstände), die sonst GOGs Skript an diese Stelle kopiert.
Der Rest des Zwischenordners kommt in den Papierkorb, gelöscht wird nichts.
"""
from __future__ import annotations

import glob
import os
import shutil
import signal
import subprocess
from typing import Any, Callable

from .. import log, paths

_log = log.get("store.gog")

STAGING = ".gog-extract"
SKIP = frozenset({"tmp", "commonappdata", "__redist", "__support"})    # Hilfsordner des Installers
SUPPORT = ("__support/app", "__support/save")
TIMEOUT_S = 2 * 3600
LUTRIS_RUNTIMES = ("~/.local/share/lutris/runtime", "~/.var/app/net.lutris.Lutris/data/lutris/runtime")


def find_tool(environ: dict | None = None) -> str | None:
    """Pfad zu innoextract oder None. Im Prüfmodus nur die Attrappe."""
    env = os.environ if environ is None else environ
    if paths.test_mode():
        fake = env.get("COUCH_TEST_INNOEXTRACT") or ""
        return fake if fake and os.access(fake, os.X_OK) else None
    found = shutil.which("innoextract", path=env.get("PATH"))
    if found:
        return found
    for root in LUTRIS_RUNTIMES:
        # neueste Fassung zuerst (innoextract-1.9-linux vor innoextract-1.8-linux)
        for folder in sorted(glob.glob(os.path.join(os.path.expanduser(root), "innoextract-*")), reverse=True):
            tool = os.path.join(folder, "innoextract")
            if os.path.isfile(tool) and os.access(tool, os.X_OK):
                return tool
    return None


NAMES = {"de": ("german", "deutsch"), "en": ("english",)}     # ältere Installer nennen die Sprache beim Namen


def pick_language(listing: str, wanted=("de", "en")) -> str:
    """Sprache aus `innoextract --list-languages` („ - en-US“ bzw. „ - german“); "" = alle auspacken."""
    langs = [line.strip()[2:].split(":")[0].strip() for line in listing.splitlines() if line.strip().startswith("- ")]
    for want in wanted:
        for lang in langs:
            low = lang.lower()
            if low == want or low.startswith((want + "-", want + "_")) or low in NAMES.get(want, ()):
                return lang
    return ""


def _run(argv: list, log_fh: Any, cancel: Any, timeout: float, on_proc: Callable[[Any], None] | None) -> int:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh, start_new_session=True)
    if on_proc:
        on_proc(proc)
    try:
        waited = 0.0
        while True:
            try:
                return proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                waited += 1.0
                if (cancel is not None and cancel.is_set()) or waited >= timeout:
                    os.killpg(proc.pid, signal.SIGKILL)      # eigene Prozessgruppe (start_new_session)
                    proc.wait()
                    return -1
    finally:
        if on_proc:
            on_proc(None)


def _merge(src: str, dst: str) -> None:
    """src in dst verschieben (gleiches Laufwerk: rename). Ordner werden zusammengeführt, gleichnamige
    Dateien ersetzt (die ausgepackte Fassung gilt); es wird nichts gelöscht."""
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.islink(s) and os.path.isdir(d) and not os.path.islink(d):
            _merge(s, d)
        elif os.path.isdir(d) and not os.path.islink(d):
            _log.warning("GOG: %s bleibt im Zwischenordner (dort liegt schon ein Ordner)", name)
        else:
            os.replace(s, d)


def extract(tool: str, installer: str, target: str, log_fh: Any, cancel: Any = None,
            on_proc: Callable[[Any], None] | None = None, trash: Callable[[str], None] | None = None) -> str:
    """installer nach target auspacken. Rückgabe: die gewählte Sprache ("" = alle). Fehler: RuntimeError mit
    deutschem Satz. target ist der eigene, markierte Spielordner (wiring_store._new_target)."""
    listing = subprocess.run([tool, "--list-languages", installer], capture_output=True, text=True,
                             timeout=120, stdin=subprocess.DEVNULL)
    if listing.returncode != 0:
        raise RuntimeError("innoextract kann diesen Installer nicht lesen.")
    lang = pick_language(listing.stdout)
    staging = os.path.join(target, STAGING)
    os.makedirs(staging, exist_ok=True)
    argv = [tool, "--gog", "--exclude-temp", "--silent", "--progress=0", "-d", staging]
    if lang:
        argv += ["--language", lang]
    rc = _run(argv + [installer], log_fh, cancel, TIMEOUT_S, on_proc)
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Abgebrochen.")
    if rc != 0:
        raise RuntimeError("innoextract ist nicht durchgelaufen (Rückgabewert %s)." % rc)
    for name in sorted(os.listdir(staging)):
        if name in SKIP:
            continue
        src, dst = os.path.join(staging, name), os.path.join(target, name)
        if os.path.isdir(src) and not os.path.islink(src) and os.path.isdir(dst) and not os.path.islink(dst):
            _merge(src, dst)                # Reste eines halben Laufs unter Wine: zusammenführen
        elif not (os.path.isdir(dst) and not os.path.islink(dst)):
            os.replace(src, dst)
    for rel in SUPPORT:
        src = os.path.join(staging, rel)
        if os.path.isdir(src) and not os.path.islink(src):
            _merge(src, target)
    if trash is not None:
        try:
            trash(staging)                  # Reste (tmp, __redist …) in den Papierkorb
        except (OSError, ValueError) as e:
            _log.warning("GOG: Zwischenordner nicht in den Papierkorb: %s", e)
    return lang
