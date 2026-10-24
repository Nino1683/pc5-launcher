"""Systemfreigaben: die wenigen Schritte, die Root brauchen, mit dem Controller einrichten.

Wunsch 2026-10-24: „Um den Launcher auf anderen Geräten zu installieren, sollen alle sudo-Befehle, die nötig
sind, im Launcher gestartet werden können, da alles mit dem Controller eingerichtet wird.“ Das hebt für
genau diese Liste die Regel „nie sudo, nie ein Kennwort“ auf (SAFETY 13, Ausnahme Systemfreigaben):

  * Nur die Aktionen aus ACTIONS, keine freien Befehle. Was kopiert wird, liegt in der installierten
    Fassung unter app/system/ (realpath geprüft); Ziele sind feste Pfade.
  * Nur auf ausdrücklichen Wunsch am Bildschirm (Einstellungen › System › Systemfreigaben). Das Kennwort
    tippt man selbst (verdeckte Zeile, Bildschirmtastatur ohne Lernen). Es geht nur über stdin an
    `sudo -S -k`, nie in argv, Umgebung, Protokoll oder eine Datei, und sudo merkt es sich nicht (-k).
  * Jede Aktion hat einen Stand (status) und lässt sich wieder entfernen.
  * Ein Aufruf von sudo je Aktion: ein festes Skript für /bin/sh, dessen Teile mit shlex.quote gesetzt sind.

Im Prüfmodus nie das echte sudo: runner einstecken (Tests) oder COUCH_TEST_SUDO (Attrappe), sonst Fehler.
Alles hier blockiert (sudo bis TIMEOUT_S): nur aus einem Arbeits-Thread rufen.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from . import log, paths

_log = log.get("sysadmin")

TIMEOUT_S = 60.0
APP_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))        # <fassung>/app
SYSTEM_DIR = os.path.join(APP_DIR, "system")

TEXT_WRONG = ("Das Kennwort stimmt nicht. Nach drei Fehlversuchen sperrt das System die Anmeldung "
              "für einige Minuten.")
TEXT_NOT_ALLOWED = "Dein Benutzer darf auf diesem System keine Systemänderungen vornehmen (sudo)."
TEXT_NO_SUDO = "Auf diesem System gibt es kein sudo."
TEXT_EMPTY = "Gib dein Systemkennwort ein."
TEXT_FAILED = ("Das hat nicht geklappt. Hat dein Benutzer noch kein Kennwort (etwa auf einem Steam Deck), "
               "lege es einmal im Desktop-Modus mit „passwd“ fest.")
TEXT_TEST = "Im Prüfmodus läuft kein echtes sudo."


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    purpose: str
    files: tuple = ()                       # ((quelle relativ zu app/system, ziel), …)
    after: tuple = ()                       # Befehle nach dem Kopieren bzw. Entfernen (argv)
    extra: dict = field(default_factory=dict)


UDEV_RELOAD = (("udevadm", "control", "--reload"),
               ("udevadm", "trigger", "--subsystem-match=input", "--action=change"))

ACTIONS = {
    "touchpad": Action(
        id="touchpad",
        title="Touchpad des Controllers",
        purpose=("Damit das Touchpad von DualSense und DualShock 4 in Spielen nicht zugleich die Maus "
                 "bewegt und klickt. Legt eine udev-Regel an; danach den Controller einmal neu verbinden."),
        files=(("udev/70-pc5-dualsense-touchpad.rules", "/etc/udev/rules.d/70-pc5-dualsense-touchpad.rules"),),
        after=UDEV_RELOAD),
}


def _rules(text: str) -> list[str]:
    """Wirksame Zeilen (ohne Kommentare und Leerzeilen): Kommentare dürfen sich ändern."""
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _source(rel: str) -> str:
    src = os.path.realpath(os.path.join(SYSTEM_DIR, rel))
    if not src.startswith(os.path.realpath(SYSTEM_DIR) + os.sep) or not os.path.isfile(src):
        raise ValueError("Quelle fehlt in dieser Fassung: %s" % rel)
    return src


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def status(action: Action, root: str = "/") -> str:
    """"on" (eingerichtet), "off" (fehlt), "changed" (anderer Inhalt: neu einrichten). Nur lesend."""
    states = []
    for rel, dest in action.files:
        have = _read(os.path.join(root, dest.lstrip("/")))
        want = _read(_source(rel))
        if have is None:
            states.append("off")
        elif want is not None and _rules(have) == _rules(want):
            states.append("on")
        else:
            states.append("changed")
    if not states or all(s == "off" for s in states):
        return "off"
    return "on" if all(s == "on" for s in states) else "changed"


def script(action: Action, mode: str) -> str:
    """Das Skript für /bin/sh, das sudo als Root ausführt (nur feste, gequotete Teile)."""
    if mode not in ("install", "remove"):
        raise ValueError(mode)
    q = shlex.quote
    steps = ["exec </dev/null", "set -e"]       # stdin zu: das Kennwort erreicht nie einen Befehl
    for rel, dest in action.files:
        if mode == "install":
            steps.append("install -D -m 0644 -o root -g root -- %s %s" % (q(_source(rel)), q(dest)))
        else:
            steps.append("rm -f -- %s" % q(dest))
    steps += [" ".join(q(a) for a in argv) for argv in action.after]
    return "\n".join(steps)


def _sudo_argv() -> list[str] | None:
    fake = os.environ.get("COUCH_TEST_SUDO", "")
    if fake:
        return shlex.split(fake)
    if paths.test_mode():
        return None
    found = shutil.which("sudo")
    return [found] if found else []


def run(action: Action, mode: str, password: str, runner: Callable | None = None) -> tuple[bool, str]:
    """(ok, Satz für die Oberfläche). Blockiert. password nur über stdin an sudo."""
    if not password:
        return False, TEXT_EMPTY
    if runner is None:
        base = _sudo_argv()
        if base is None:
            return False, TEXT_TEST
        if not base:
            return False, TEXT_NO_SUDO
        runner = subprocess.run
    else:
        base = ["sudo"]
    try:
        body = script(action, mode)
    except ValueError as e:
        _log.warning("Systemfreigabe %s: %s", action.id, e)
        return False, "Die Datei dafür fehlt in dieser Fassung des PC5 Launchers."
    argv = [*base, "-S", "-k", "-p", "", "--", "/bin/sh", "-c", body]
    env = {k: v for k, v in os.environ.items() if k not in ("SUDO_ASKPASS",)}
    env["LC_ALL"] = "C"                       # Meldungen von sudo auswerten können
    try:
        done = runner(argv, input=password + "\n", capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        _log.warning("Systemfreigabe %s (%s): Zeitüberschreitung", action.id, mode)
        return False, TEXT_FAILED
    except OSError as e:
        _log.warning("Systemfreigabe %s (%s): %s", action.id, mode, type(e).__name__)
        return False, TEXT_NO_SUDO
    err = (done.stderr or "").lower()
    if done.returncode == 0:
        _log.info("Systemfreigabe %s: %s", action.id, "eingerichtet" if mode == "install" else "entfernt")
        return True, ("„%s“ ist eingerichtet." if mode == "install" else "„%s“ ist entfernt.") % action.title
    # stderr enthält nie das Kennwort (sudo -p '' und -S lesen es nur); trotzdem nur kurz protokollieren
    _log.warning("Systemfreigabe %s (%s): rc=%s %s", action.id, mode, done.returncode, err.strip()[-200:])
    if "incorrect password" in err or "sorry, try again" in err:
        return False, TEXT_WRONG
    if "not in the sudoers" in err or "not allowed to" in err:
        return False, TEXT_NOT_ALLOWED
    return False, TEXT_FAILED
