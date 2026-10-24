"""Flatpak-Programme starten und höflich beenden.

Start immer mit --socket=x11 --nosocket=wayland: Flatpaks mit fallback-x11 landen sonst auf
einem zufällig vorhandenen Wayland-Socket (im Test: auf dem Desktop des Nutzers), siehe 06 Abschnitt 7.1.

Beenden: Flatpak-Programme liegen nicht in unserer cgroup, und _NET_WM_PID ist die PID in der
Sandbox. Deshalb bittet app/bin/couch-flatpak-stop genau die Instanzen, deren Prozesse
Nachkommen unseres reaper für diesen Eintrag sind: SIGTERM an die Programme in der Sandbox, dann
höchstens die halbe Schonfrist warten (E99; "flatpak kill" war SIGKILL und ließ Kodi, Eden und Cemu
nichts mehr speichern). Nie über den App-Namen: das träfe auch eine Instanz im
verschachtelten Desktop des Nutzers (Kritik Befund 25).

Blockierend: running() ruft "flatpak ps" über das eingesteckte run_read auf (um 50 bis 100 ms).
is_installed() sieht nur in zwei Ordner und ist schnell.
"""
from __future__ import annotations

import os

from .spec import LaunchError, LaunchSpec

X11_ONLY = ("--socket=x11", "--nosocket=wayland")
PS_ARGV = ("flatpak", "ps", "--columns=instance,pid,child-pid,application")


def run_argv(app_id: str, *app_args: str, flatpak_args=()) -> list:
    """["flatpak", "run", "--socket=x11", "--nosocket=wayland", *flatpak_args, app, *args]

    E105: nie --file-forwarding (Dokumenten-Portal = FUSE-Einhängung; der Schaden vom 2026-10-12 lief über
    so eine). Zugriff auf Spieldateien nur über --filesystem=<ordner>:ro je Start."""
    if any(str(a).startswith("--file-forwarding") for a in flatpak_args):
        raise ValueError("--file-forwarding ist nicht erlaubt")
    return ["flatpak", "run", *X11_ONLY, *flatpak_args, app_id, *[str(a) for a in app_args]]


def is_installed(app_id: str, ctx) -> bool:
    """Liegt die App in einer der Flatpak-Installationen? Kein Unterprozess."""
    return any(os.path.isdir(os.path.join(root, "app", app_id, "current"))
               for root in ctx.flatpak_dirs())


def parse_ps(text: str) -> list:
    """Ausgabe von "flatpak ps --columns=instance,pid,child-pid,application" zerlegen.

    Rückgabe: [{"instance": str, "pid": int, "child_pid": int, "application": str}]
    Kopfzeilen und Unlesbares werden übersprungen.
    """
    rows = []
    for line in (text or "").splitlines():
        # flatpak trennt mit Tabulatoren; eine leere Spalte (child-pid) bleibt so erhalten
        parts = [p.strip() for p in (line.split("\t") if "\t" in line else line.split())]
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append({"instance": parts[0], "pid": int(parts[1]),
                     "child_pid": int(parts[2]) if parts[2].isdigit() else 0,
                     "application": parts[3]})
    return rows


def running(ctx, app_id: str | None = None) -> list | None:
    """Laufende Flatpak-Instanzen (optional nur einer App). None, wenn flatpak ps scheitert.
    Blockierend (Unterprozess über ctx.run_read)."""
    try:
        code, out, _err = ctx.run_read(list(PS_ARGV), timeout=5)
    except Exception:
        return None
    if code != 0:
        return None
    rows = parse_ps(out)
    if app_id is not None:
        rows = [r for r in rows if r["application"] == app_id]
    return rows


def stop_argv(entry_id: str, ctx) -> list:
    """Befehl für LaunchSpec.polite_stop. Rückgabewerte von couch-flatpak-stop:
    0 = Bitte (SIGTERM) an die Programme der eigenen Sandbox(en) zugestellt, 3 = keine eigene
    Instanz bzw. kein Programm darin gefunden (dann genügt SIGTERM ans Hauptkind), 2 = Aufruffehler,
    1 = flatpak ps gescheitert oder die Bitte erreichte niemanden."""
    return [ctx.bin("couch-flatpak-stop"), str(entry_id)]


def build(entry, ctx, app_id: str, app_args=(), *, kind: str, label: str = "",
          freeze: bool = False, mouse: bool | str = False, env=None, unset=(), check: bool = True
          ) -> LaunchSpec:
    """LaunchSpec für eine Flatpak-App. check=True prüft, ob sie installiert ist."""
    if check and not is_installed(app_id, ctx):
        raise LaunchError("%s ist nicht installiert." % (label or entry.title or app_id),
                          "Flatpak %s fehlt" % app_id, reason="not-installed")
    return LaunchSpec(
        entry_id=entry.id,
        argv=run_argv(app_id, *app_args),
        env=dict(env or {}),
        unset=list(unset),
        cwd=ctx.home,
        kind=kind,
        freeze=freeze,
        mouse=mouse,
        polite_stop=stop_argv(entry.id, ctx),
        title=entry.title,
    )
