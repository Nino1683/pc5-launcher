"""Linux-Programme direkt starten (Lutris-"linux"-Einträge, Archiv-Spiele mit Linux-Startdatei).

Hier liegen außerdem die kleinen Prüfhelfer, die alle Starter teilen: fehlende Datei oder
fehlende Platte, Argumente zerlegen. Die Reste eines Steam-Elternprozesses (STEAM_LEFTOVERS)
stehen in spec.py, gemeinsam mit dem AppManager (E99). Alles rechnet und prüft nur; gestartet
wird nichts.

Blockierend: build() mit check=True und preflight() fragen die Platte ab (stat). Auf einer
schlafenden USB-Platte kann das länger als 50 ms dauern, also im Thread aufrufen.
"""
from __future__ import annotations

import os
import re
import shlex

from .spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec, pad_mode

TEXT_NEEDS_STEAM = "Dieses Spiel braucht den laufenden Steam-Client. Starte es über Steam."
TEXT_DISK = "Die Spiele-Platte ist nicht eingehängt."
TEXT_EMUDECK_LAUNCHER = ("EmuDecks eigene Startskripte werden hier nicht benutzt. "
                         "Starte das Spiel über seine Kachel bei den Emulator-Spielen.")
TEXT_STEAM_DETOUR = ("Dieser Eintrag würde den Emulator über Steam aufrufen. "
                     "Starte das Spiel über seine Kachel bei den Emulator-Spielen.")

# Eigene Emulator-Starter des Nutzers: ohne --raw gehen sie im Game-Mode den Umweg über eine
# Steam-Verknüpfung (steam-shortcut-run) und würden Steam mitten in unserer Sitzung starten.
_STEAM_DETOUR_WRAPPERS = ("eden-launch", "cemu-launch")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def expand(path: str, home: str) -> str:
    """"~/x" gegen das eingesteckte Home auflösen (nicht gegen $HOME des Prozesses)."""
    path = str(path or "")
    if path == "~":
        return home
    if path.startswith("~/"):
        return os.path.join(home, path[2:])
    return path


def split_args(value) -> list:
    """Argumente aus Lutris/Optionen: Zeichenkette (Shell-Schreibweise) oder fertige Liste."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    try:
        return shlex.split(str(value))
    except ValueError as err:
        raise LaunchError("Die Startargumente sind fehlerhaft geschrieben.",
                          "%s: %r" % (err, value), reason="bad-args") from None


def clean_env(mapping) -> dict:
    """Nur gültige Variablennamen, alle Werte als Text."""
    out = {}
    for key, val in (mapping or {}).items():
        key = str(key)
        if _ENV_NAME.match(key) and val is not None:
            out[key] = str(val)
    return out


def mount_root(path: str, media_roots) -> str | None:
    """Einhängepunkt eines Wechseldatenträgers, unter dem path liegt, sonst None.

    media_roots: Paare (wurzel, tiefe), z. B. ("/run/media", 2) für /run/media/<nutzer>/<name>.
    """
    path = os.path.abspath(path)
    for root, depth in media_roots:
        root = os.path.abspath(root)
        if not path.startswith(root + os.sep):
            continue
        parts = path[len(root) + 1:].split(os.sep)
        if len(parts) > depth:
            return os.path.join(root, *parts[:depth])
    return None


def missing(path: str, ctx, what: str = "Die Startdatei") -> LaunchError:
    """Passender Fehler für eine fehlende Datei: Platte weg oder nur die Datei."""
    mount = mount_root(path, ctx.media_roots)
    if mount and not os.path.ismount(mount):
        return LaunchError(TEXT_DISK, "nicht eingehängt: %s (gesucht: %s)" % (mount, path),
                           reason="disk-missing")
    return LaunchError("%s fehlt: %s" % (what, path), reason="missing-file")


def require_file(path: str, ctx, what: str = "Die Startdatei", executable: bool = False) -> None:
    """LaunchError, wenn die Datei fehlt (oder nicht ausführbar ist). Blockierend (Platte)."""
    if not path or not os.path.isfile(path):
        raise missing(path, ctx, what)
    if executable and not os.access(path, os.X_OK):
        raise LaunchError("%s ist nicht ausführbar: %s" % (what, path), reason="not-executable")


def require_exe(entry, path: str, ctx, executable: bool = False) -> None:
    """Wie require_file für die Startdatei eines Spiels. Bei einem Archiv-Spiel, dessen Ordner
    da ist, dessen Startdatei aber fehlt, heißt die Ursache "needs-exe": Dann hilft die Auswahl
    der Startdatei (die Oberfläche öffnet sie), nicht der Blick auf die Platte."""
    try:
        require_file(path, ctx, executable=executable)
    except LaunchError as err:
        folder = expand(entry.detail.get("install_dir") or "", ctx.home) if entry.source == "local" else ""
        if err.reason == "missing-file" and folder and os.path.isdir(folder):
            err.reason = "needs-exe"
        raise


def inside(path: str, folder: str) -> bool:
    """Liegt path (aufgelöst) in folder? /home ist ein Verweis auf /var/home, daher realpath."""
    real, base = os.path.realpath(path), os.path.realpath(folder)
    return real == base or real.startswith(base + os.sep)


def is_emudeck_launcher(path: str) -> bool:
    """E13: Skripte unter Emulation/tools/launchers ziehen bei jedem Start per git nach."""
    parts = os.path.normpath(path).split(os.sep)
    for i in range(len(parts) - 3):
        if parts[i:i + 3] == ["Emulation", "tools", "launchers"]:
            return True
    return False


def local_exe(entry, ctx) -> tuple:
    """(exe, args, ordner) eines Archiv-Spiels; die Startdatei darf den Spielordner nicht verlassen."""
    d = entry.detail
    folder = expand(d.get("install_dir") or "", ctx.home)
    rel = str(entry.option("exe") or d.get("exe") or "")
    if not folder or not rel:
        # Der Installer konnte sich nicht entscheiden (Marke needs-exe): Der Nutzer wählt
        raise LaunchError("Für dieses Spiel ist keine Startdatei festgelegt.",
                          "install_dir=%r exe=%r" % (folder, rel), reason="needs-exe")
    exe = rel if os.path.isabs(rel) else os.path.join(folder, rel)
    exe = os.path.normpath(exe)
    if not inside(exe, folder):
        raise LaunchError("Die Startdatei liegt außerhalb des Spielordners: %s" % exe,
                          reason="needs-exe")
    return exe, split_args(d.get("args")), folder


def _paths(entry, ctx) -> tuple:
    d = entry.detail
    if entry.source == "local":
        exe, args, folder = local_exe(entry, ctx)
        return exe, args, os.path.dirname(exe) or folder
    exe = expand(d.get("exe") or "", ctx.home)
    if not exe:
        raise LaunchError("Für diesen Eintrag ist keine Startdatei festgelegt.", reason="no-exe")
    cwd = expand(d.get("working_dir") or "", ctx.home) or os.path.dirname(exe)
    return exe, split_args(d.get("args")), cwd


def _plan(entry, ctx) -> tuple:
    if "needs-steam" in (entry.tags or []):
        raise LaunchError(TEXT_NEEDS_STEAM, reason="needs-steam")
    exe, args, cwd = _paths(entry, ctx)
    if is_emudeck_launcher(exe):
        raise LaunchError(TEXT_EMUDECK_LAUNCHER, exe, reason="wrong-launcher")
    if os.path.basename(exe) in _STEAM_DETOUR_WRAPPERS and args[:1] != ["--raw"]:
        raise LaunchError(TEXT_STEAM_DETOUR, "%s %s" % (exe, " ".join(args)), reason="wrong-launcher")
    args = args + split_args(entry.option("args"))
    return exe, args, cwd


def preflight(entry, ctx) -> list:
    """Harte Fehler als LaunchError, Hinweise als Liste deutscher Sätze. Blockierend (Platte)."""
    exe, _args, cwd = _plan(entry, ctx)
    require_exe(entry, exe, ctx, executable=True)
    warnings = []
    if cwd and not os.path.isdir(cwd):
        warnings.append("Der Arbeitsordner fehlt (%s); gestartet wird im Ordner der Startdatei." % cwd)
    return warnings


def build(entry, ctx, *, check: bool = True) -> LaunchSpec:
    """Entry (Quelle "native" oder "local" mit platform "linux") -> LaunchSpec.

    check=True prüft zusätzlich die Platte (blockierend) und wirft LaunchError.
    """
    exe, args, cwd = _plan(entry, ctx)
    if check:
        preflight(entry, ctx)
    if cwd and not os.path.isdir(cwd) and os.path.isdir(os.path.dirname(exe)):
        cwd = os.path.dirname(exe)
    # Lutris-Linux-Spiele bekommen die Umgebung aus Lutris (Vorgaben, Runner, Spiel: z. B.
    # LD_PRELOAD oder LD_LIBRARY_PATH auf mitgelieferte Bibliotheken), darüber die Option env (E99)
    env = clean_env(entry.detail.get("env")) if entry.source == "native" else {}
    env.update(clean_env(entry.option("env")))
    return LaunchSpec(
        entry_id=entry.id,
        argv=[exe, *args],
        env=env,
        unset=list(STEAM_LEFTOVERS),
        cwd=cwd or None,
        kind="game",
        freeze=bool(entry.option("freeze")),
        mouse=pad_mode(entry),
        polite_stop=None,
        title=entry.title,
    )
