"""GOG-Installer ohne Wine auspacken, wenn der stille Lauf unter Proton scheitert (E131).

Befund 2026-10-27 (Pizza Connection, Inno Setup 5.5.9, GOG-Installer von 2018): Mit /VERYSILENT wie mit /SILENT
endet der Installer nach 3 s mit Rückgabewert 1, sein Protokoll sagt „Current Page: Welcome – Failed to proceed
to next wizard page; aborting“. Das Skript des Installers lässt die Willkommensseite unter Wine nicht weiter,
egal mit welchem Ziel und welcher Sprache. Linux-Programme für GOG (Lutris, Minigalaxy) packen solche Installer
deshalb mit innoextract aus; das macht der Launcher dann auch.

innoextract kommt nur vom System: aus dem PATH oder aus Homebrew (/home/linuxbrew/.linuxbrew/bin, Bazzite bringt
Homebrew mit; der PATH der Game-Mode-Sitzung nennt es oft nicht). Lutris' Laufzeit wird seit E134 nicht mehr
benutzt. Der Launcher lädt selbst kein Programm aus dem Netz. Im Prüfmodus gilt nur die Attrappe aus
COUCH_TEST_INNOEXTRACT.

Ablauf: in einen Zwischenordner im eigenen Spielordner auspacken (eine Sprache: Deutsch, sonst Englisch), alles
außer den Hilfsordnern des Installers in den Spielordner verschieben, dazu __support/app (Einstellungen, z. B.
dosbox_*.conf) und __support/save (Anfangsstand der Spielstände), die sonst GOGs Skript an diese Stelle kopiert.
Der Rest des Zwischenordners kommt in den Papierkorb, gelöscht wird nichts.

Käfig: innoextract liest einen fremden Installer (GOG, E114 auch aus fremden Archiven) und läuft deshalb wie 7z,
unrar und tar des Archiv-Installers in bubblewrap (installer/probe.sandbox_argv): das System nur lesbar, /tmp und
/run leer, kein Netz, eigene Prozessnummern, keine Fähigkeiten, Umgebung nur PATH/Sprache/HOME. Eigens lesbar
eingebunden ist der Ordner des Installers (dort liegen auch seine .bin-Teile), beschreibbar allein der
Zwischenordner. Fehlt bubblewrap oder startet es nicht, wird nicht ausgepackt (wie beim Archiv-Installer): nie
ohne Käfig. Bevor etwas wandert, wird der Zwischenordner geprüft: innoextract legt nur Ordner und Dateien an,
ein Verweis oder eine Sonderdatei lässt das Auspacken scheitern. Marker, Zwischenordner und Download-Ordner des
Launchers werden nie aus dem Installer übernommen.
"""
from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
from typing import Any, Callable

from .. import log, paths
from ..installer import probe, trash as _trash
from .gog_install import SETUP_DIR

_log = log.get("store.gog")

STAGING = ".gog-extract"
SKIP = frozenset({"tmp", "commonappdata", "__redist", "__support"})    # Hilfsordner des Installers
SUPPORT = ("__support/app", "__support/save")
# Namen des Launchers im Spielordner: Ein Installer, der so etwas mitbringt, ersetzte sonst den Marker
OWN_NAMES = frozenset({_trash.MARKER_NAME, STAGING, SETUP_DIR})
TIMEOUT_S = 2 * 3600
LIST_TIMEOUT_S = 120
# E134: außer dem PATH nur noch Homebrew (Bazzite); fest, damit kein Ordner aus der Umgebung hineinkommt
EXTRA_DIRS = ("/home/linuxbrew/.linuxbrew/bin",)

# Dieselben Sätze wie beim Archiv-Installer (installer/extract.py)
MSG_NO_SANDBOX = "Der geschützte Entpacker (bubblewrap) fehlt auf diesem System."
MSG_SANDBOX_FAILED = "Der geschützte Entpacker (bubblewrap) konnte nicht starten."
MSG_UNSAFE = ("Beim Auspacken des Installers sind Verweise oder Sonderdateien entstanden. Aus Sicherheitsgründen "
              "wird nichts davon übernommen.")
MSG_STAGING = "%s ist kein echter Ordner (Verweis?) und wird nicht angefasst."


class Refused(RuntimeError):
    """Ausgepackt wird nicht, weil der Käfig fehlt oder das Ergebnis verdächtig ist. code: "no-sandbox" |
    "unsafe-staging" | "unsafe-content". Bleibt ein RuntimeError (wiring_store zeigt dessen Satz)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def find_tool(environ: dict | None = None, extra_dirs=EXTRA_DIRS) -> str | None:
    """Pfad zu innoextract oder None: zuerst der PATH, dann Homebrew (extra_dirs). Im Prüfmodus nur die
    Attrappe aus COUCH_TEST_INNOEXTRACT."""
    env = os.environ if environ is None else environ
    if paths.test_mode():
        fake = env.get("COUCH_TEST_INNOEXTRACT") or ""
        return fake if fake and os.access(fake, os.X_OK) else None
    found = shutil.which("innoextract", path=env.get("PATH"))
    if found:
        return found
    for folder in extra_dirs:
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


def tool_env(tool: str) -> dict:
    """Umgebung von innoextract im Käfig: die der Werkzeuge des Archiv-Installers (probe.TOOL_ENV); dazu der
    Ordner von innoextract im PATH, falls es nicht vom System kommt (Homebrew: innoextract --gog ruft unrar bzw.
    unar beim Namen auf). Nichts aus der Sitzung (Bus, Anzeige, Steam)."""
    env = dict(probe.TOOL_ENV)
    folder = os.path.dirname(os.path.abspath(tool))
    if folder not in env["PATH"].split(":"):
        env["PATH"] = env["PATH"] + ":" + folder
    return env


def sandbox_argv(argv: list, installer: str, writable: str | None = None, tools: dict | None = None) -> list:
    """innoextract-Befehl im Käfig (probe.sandbox_argv): lesbar der Ordner des Installers, beschreibbar nur
    writable (der Zwischenordner) bzw. nichts. tools: nur für Tests (bwrap ersetzen)."""
    return probe.sandbox_argv(argv, read_only=[os.path.dirname(os.path.realpath(installer))], writable=writable,
                              tools=tools)


def run_sandboxed(argv: list, log_fh: Any, cancel: Any, timeout: float, on_proc: Callable[[Any], None] | None,
         env: dict | None = None) -> int:
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh, start_new_session=True,
                                env=env, cwd="/")
    except OSError:
        raise Refused("no-sandbox", MSG_NO_SANDBOX) from None   # argv[0] ist bwrap
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
                    # eigene Prozessgruppe (start_new_session): bwrap, sein Init im Käfig und innoextract
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    return -1
    finally:
        if on_proc:
            on_proc(None)


def _list_languages(tool: str, installer: str, tools: dict | None) -> str:
    """`innoextract --list-languages` im Käfig (nichts beschreibbar). Rückgabe: Ausgabe; RuntimeError sonst."""
    try:
        listing = subprocess.run(sandbox_argv([tool, "--list-languages", installer], installer, tools=tools),
                                 capture_output=True, encoding="utf-8", errors="replace", timeout=LIST_TIMEOUT_S,
                                 stdin=subprocess.DEVNULL, env=tool_env(tool), cwd="/")
    except OSError:
        raise Refused("no-sandbox", MSG_NO_SANDBOX) from None
    if listing.returncode != 0:
        if "bwrap:" in (listing.stderr or ""):
            raise Refused("no-sandbox", MSG_SANDBOX_FAILED)
        raise RuntimeError("innoextract kann diesen Installer nicht lesen.")
    return listing.stdout or ""


def fresh_staging(target: str) -> str:
    """Der Zwischenordner <target>/.gog-extract als echter Ordner, aufgelöster Pfad. Ein Rest eines unterbrochenen
    Laufs darf bleiben; ein Verweis (etwa vom Installer unter Wine angelegt) ließe innoextract woandershin schreiben."""
    staging = os.path.join(os.path.realpath(target), STAGING)
    try:
        os.mkdir(staging, 0o755)
    except FileExistsError:
        pass
    try:
        real_dir = stat.S_ISDIR(os.lstat(staging).st_mode)
    except OSError:
        real_dir = False
    if not real_dir or os.path.realpath(staging) != staging:
        raise Refused("unsafe-staging", MSG_STAGING % staging)
    return staging


def unsafe_entries(staging: str, limit: int = 5) -> list:
    """Verweise und Sonderdateien (FIFO, Gerät, Socket) im Zwischenordner, als relative Pfade, höchstens limit.
    innoextract legt nur Ordner und Dateien an. Ein Ordner, der sich nicht lesen lässt, zählt mit: In ihm
    könnte ein Verweis stecken, der beim Verschieben mitwanderte. Nur lesend."""
    bad: list = []

    def unreadable(err: OSError) -> None:
        bad.append(os.path.relpath(err.filename or staging, staging))

    for dirpath, dirnames, filenames in os.walk(staging, onerror=unreadable, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                mode = 0
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                bad.append(os.path.relpath(path, staging))
        if len(bad) >= limit:
            break
    return bad[:limit]


def _own(name: str) -> bool:
    return name in OWN_NAMES or name.startswith(_trash.STAGING_PREFIX)


def merge(src: str, dst: str, top: bool = False) -> None:
    """src in dst verschieben (gleiches Laufwerk: rename). Ordner werden zusammengeführt, gleichnamige
    Dateien ersetzt (die ausgepackte Fassung gilt); es wird nichts gelöscht. top: dst ist der Spielordner,
    die Namen des Launchers (OWN_NAMES) bleiben dann im Zwischenordner."""
    for name in os.listdir(src):
        if top and _own(name):
            _log.warning("GOG: %s aus dem Installer wird nicht übernommen", name)
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.isdir(s) and not os.path.islink(s) and os.path.isdir(d) and not os.path.islink(d):
            merge(s, d)
        elif os.path.isdir(d) and not os.path.islink(d):
            _log.warning("GOG: %s bleibt im Zwischenordner (dort liegt schon ein Ordner)", name)
        else:
            os.replace(s, d)


def extract(tool: str, installer: str, target: str, log_fh: Any, cancel: Any = None,
            on_proc: Callable[[Any], None] | None = None, trash: Callable[[str], None] | None = None,
            wanted: tuple = ("de", "en"), tools: dict | None = None) -> str:
    """installer nach target auspacken, innoextract im Käfig. Rückgabe: die gewählte Sprache ("" = alle).
    Fehler: RuntimeError mit deutschem Satz (Refused, wenn der Käfig fehlt oder das Ergebnis verdächtig ist).
    target ist der eigene, markierte Spielordner (wiring_store._new_target). wanted: Sprachen in der
    Reihenfolge der Oberfläche. on_proc bekommt den Prozess von bwrap (Gruppe = seine PID).
    tools: nur für Tests ({"bwrap": …})."""
    installer = os.path.realpath(installer)
    lang = pick_language(_list_languages(tool, installer, tools), wanted)
    staging = fresh_staging(target)
    argv = [tool, "--gog", "--exclude-temp", "--silent", "--progress=0", "-d", staging]
    if lang:
        argv += ["--language", lang]
    rc = run_sandboxed(sandbox_argv(argv + [installer], installer, writable=staging, tools=tools), log_fh, cancel, TIMEOUT_S,
              on_proc, env=tool_env(tool))
    if cancel is not None and cancel.is_set():
        raise RuntimeError("Abgebrochen.")
    if rc != 0:
        raise RuntimeError("innoextract ist nicht durchgelaufen (Rückgabewert %s)." % rc)
    bad = unsafe_entries(staging)
    if bad:
        _log.warning("GOG: Verweise oder Sonderdateien im Zwischenordner, nichts wird übernommen: %s", ", ".join(bad))
        raise Refused("unsafe-content", MSG_UNSAFE)
    target = os.path.dirname(staging)
    for name in sorted(os.listdir(staging)):
        if _own(name):
            _log.warning("GOG: %s aus dem Installer wird nicht übernommen", name)
            continue
        if name in SKIP:
            continue
        src, dst = os.path.join(staging, name), os.path.join(target, name)
        if os.path.isdir(src) and not os.path.islink(src) and os.path.isdir(dst) and not os.path.islink(dst):
            merge(src, dst)                # Reste eines halben Laufs unter Wine: zusammenführen
        elif not (os.path.isdir(dst) and not os.path.islink(dst)):
            os.replace(src, dst)
    for rel in SUPPORT:
        src = os.path.join(staging, rel)
        if os.path.isdir(src) and not os.path.islink(src):
            merge(src, target, top=True)
    if trash is not None:
        try:
            trash(staging)                  # Reste (tmp, __redist …) in den Papierkorb
        except (OSError, ValueError) as e:
            _log.warning("GOG: Zwischenordner nicht in den Papierkorb: %s", e)
    return lang
