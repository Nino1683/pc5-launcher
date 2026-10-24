"""Betriebssystem-Update über Bazzites Helfer (05 Abschnitt 6).

    /usr/bin/steamos-polkit-helpers/steamos-update check    0 = Update da, 7 = keins
    /usr/bin/steamos-polkit-helpers/steamos-update          lädt und richtet ein, druckt "NN%"

Der Helfer ruft sich selbst über `pkexec --disable-internal-agent` auf (polkit: yes für die
Sitzung), fragt also nie nach einem Kennwort. Fallen, die hier behandelt sind:
- Rückgabe 7 heißt „nichts da“ ODER „kein Netz“ ODER „schon eingerichtet“: Netz und die Marke
  /tmp/upgrade-installed vorher selbst prüfen.
- Der Rückgabewert der Installation ist auch bei einem Fehlschlag 0: Erfolg ist allein die Marke.
- Fehlt /etc/bazzite/fixups/first_steam_gamemode_boostrap, startet das Skript graphical.target
  neu (die ganze Sitzung): dann wird der Helfer gar nicht erst aufgerufen. E104: Das gilt nur auf
  Bazzite (sysdetect, Teil P2; fehlt das Modul, wird wie bisher immer geprüft). Auf SteamOS gibt es
  die Datei nicht, das Protokoll des Helfers ist Steams eigenes (dort unbestätigt).

Die Installation braucht laufende Ausgabe (Fortschritt) und kann deshalb nicht durch run_write
gehen. Sie ist hier gekapselt; der Bereich system schaltet sie im Prüfmodus ab (enabled=False),
außer es ist ausdrücklich ein Attrappen-Skript eingesteckt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time

from . import silent_helper

log = logging.getLogger("couch.settings.osupdate")

HELPER = ["/usr/bin/steamos-polkit-helpers/steamos-update"]
MARKER = "/tmp/upgrade-installed"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
FIXUP = "/etc/bazzite/fixups/first_steam_gamemode_boostrap"
_PERCENT = re.compile(r"^\s*(\d{1,3})\s*%")
_ON_BAZZITE: list = []                # gemerkt je Prozess (sysdetect liest einige Dateien)


def on_bazzite() -> bool:
    """Läuft Bazzite? (E104) Aus couch.sysdetect (Teil P2). Fehlt das Modul oder scheitert es, gilt
    die Prüfung von früher: ja (im Zweifel lieber die Fixup-Datei verlangen)."""
    if not _ON_BAZZITE:
        try:
            from ... import sysdetect
            value = sysdetect.detect()["os"]["id"] == "bazzite"
        except Exception:  # noqa: BLE001 - ImportError (noch nicht da) und jeder Fehler beim Lesen
            value = True
        _ON_BAZZITE.append(bool(value))
    return _ON_BAZZITE[0]

# Zustände: unknown | checking | current | available | offline | installing | installed | failed | blocked
TEXT = {
    "unknown": "Noch nicht geprüft",
    "checking": "Suche läuft …",
    "current": "Auf dem neuesten Stand",
    "available": "Update verfügbar",
    "offline": "Keine Internetverbindung",
    "installed": "Neustart erforderlich",
    "failed": "Fehlgeschlagen",
    "blocked": "Nicht möglich",
    "disabled": "Im Prüfmodus abgeschaltet",
}


def _read_boot_id() -> str:
    """Kennung dieses Hochfahrens; "" wenn unlesbar (dann gilt jeder gespeicherte Stand als alt)."""
    try:
        with open(BOOT_ID, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


class OsUpdater:
    def __init__(self, helper: list[str] | None = None, marker: str = MARKER, fixup: str | None = FIXUP,
                 online=None, game_running=None, on_change=None, state_file: str | None = None,
                 enabled: bool = True, popen=subprocess.Popen, clock=time.time,
                 check_timeout: float = 120.0, boot_id=None, bazzite=None):
        self.helper = list(helper or HELPER)
        self.marker = marker
        self.fixup = fixup                    # None: keine Prüfung (Attrappen-Skripte)
        self._bazzite = bazzite or on_bazzite  # E104: die Fixup-Prüfung gilt nur auf Bazzite
        self.online = online or (lambda: True)
        self.game_running = game_running or (lambda: False)
        self.on_change = on_change
        self.state_file = state_file
        self.enabled = enabled
        self._popen = popen
        self._clock = clock
        self._check_timeout = check_timeout
        self._boot_id = boot_id or _read_boot_id
        self._lock = threading.Lock()
        self.phase = "unknown"
        self.progress: int | None = None
        self.message = ""
        self.checked_at: float | None = None
        self._load()

    # --- Zustand -------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("checked_at"), (int, float)):
                phase = data.get("phase")
                boot = self._boot_id()        # unlesbar (""): Ob seitdem neu gestartet wurde, weiß niemand
                if phase in ("available", "installed") and (not boot or data.get("boot") != boot):
                    # Seit der Suche wurde neu gestartet, vielleicht schon ins neue System (Update
                    # über Steam oder den Desktop): „Update verfügbar“ stimmt dann nicht mehr. Die
                    # Suche ist gleich wieder fällig (E99).
                    log.info("Update-Stand stammt von vor dem Neustart: wird neu gesucht")
                    return
                self.checked_at = float(data["checked_at"])
                if phase in ("current", "available"):
                    self.phase = phase
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"checked_at": self.checked_at, "phase": self.phase, "boot": self._boot_id()}, f)
            os.replace(tmp, self.state_file)
        except OSError as e:
            log.warning("Update-Zustand nicht speicherbar: %s", e)

    def _set(self, phase: str, message: str = "", progress: int | None = None) -> None:
        with self._lock:
            self.phase, self.message, self.progress = phase, message, progress
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                log.exception("on_change")

    def status(self) -> dict:
        """{"phase", "text", "progress", "message", "checked_at", "busy"}; die Marke gewinnt immer."""
        with self._lock:
            phase, progress, message = self.phase, self.progress, self.message
        if phase != "installing" and os.path.exists(self.marker):
            phase = "installed"
        if phase == "installing":
            text = f"Wird installiert … {progress} %" if progress is not None else "Wird installiert …"
        else:
            text = TEXT.get(phase, phase)
        return {"phase": phase, "text": text, "progress": progress, "message": message,
                "checked_at": self.checked_at, "busy": phase in ("checking", "installing")}

    def supported(self) -> bool:
        """Gibt es den Helfer auf diesem System überhaupt? Sonst sind die Zeilen gesperrt."""
        return os.path.isfile(self.helper[0]) and os.access(self.helper[0], os.X_OK) \
            and silent_helper(self.helper[0])

    def _blocked(self) -> str:
        if not self.enabled:
            return "Im Prüfmodus wird das System nicht aktualisiert."
        if not self.supported():
            return "Updates über diese Oberfläche werden auf diesem System nicht unterstützt."
        if self.fixup and self._bazzite() and not os.path.exists(self.fixup):
            # Sonst startet os-update die grafische Sitzung neu (siehe Kopf der Datei).
            return "Starte einmal Steam Big Picture, dann lässt sich hier nach Updates suchen."
        return ""

    # --- Prüfen --------------------------------------------------------------------------

    def check(self) -> dict:
        """Nach einem Update suchen. Blockierend (Sekunden, höchstens check_timeout)."""
        if self.status()["busy"]:
            return self.status()
        if os.path.exists(self.marker):
            self._set("installed")
            return self.status()
        why = self._blocked()
        if why:
            self._set("disabled" if not self.enabled else "blocked", why)
            return self.status()
        self._set("checking")
        if not self.online():
            # Der Helfer meldet ohne Netz dasselbe wie „aktuell“; deshalb hier unterscheiden.
            self._set("offline", "Ohne Internetverbindung lässt sich nicht nach Updates suchen.")
            return self.status()
        try:
            p = subprocess.run(self.helper + ["check"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=self._check_timeout, errors="replace")
            rc = p.returncode
        except subprocess.TimeoutExpired:
            self._set("failed", "Die Suche nach Updates hat zu lange gedauert.")
            return self.status()
        except OSError as e:
            log.warning("Update-Helfer startet nicht: %s", e)
            self._set("failed", "Die Suche nach Updates ist auf diesem System nicht möglich.")
            return self.status()
        self.checked_at = self._clock()
        if rc == 0:
            self._set("available")
        elif rc == 7:
            self._set("current")
        else:
            log.warning("Update-Helfer check: rc=%s %s", rc, (p.stderr or "").strip()[-200:])
            self._set("failed", "Die Suche nach Updates ist fehlgeschlagen.")
        self._save()
        return self.status()

    def check_if_due(self, max_age: float = 86400.0) -> dict | None:
        """Tägliche Prüfung für den Home-Start: nie bei laufendem Spiel, nie doppelt. Blockierend."""
        if self.game_running() or self.status()["busy"]:
            return None
        if self.checked_at is not None and self._clock() - self.checked_at < max_age:
            return None
        return self.check()

    # --- Installieren --------------------------------------------------------------------

    def can_install(self) -> str:
        """Leer = erlaubt, sonst der Grund als Satz."""
        st = self.status()
        if st["busy"]:
            return "Es läuft schon ein Vorgang."
        if st["phase"] == "installed":
            return "Das Update ist schon installiert. Starte die Konsole neu."
        if self.game_running():
            return "Schließe zuerst das laufende Spiel."
        why = self._blocked()
        if why:
            return why
        if st["phase"] != "available":
            return "Suche zuerst nach Updates."
        return ""

    def install(self) -> bool:
        """Update laden und einrichten. Blockierend (Minuten). Fortschritt über on_change/status()."""
        why = self.can_install()
        if why:
            self._set(self.phase, why, None)
            return False
        self._set("installing", "", 0)
        try:
            proc = self._popen(self.helper, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               stdin=subprocess.DEVNULL, text=True, errors="replace")
        except OSError as e:
            log.warning("Update-Helfer startet nicht: %s", e)
            self._set("failed", "Das Update ließ sich nicht starten.")
            return False
        last = 0
        with proc.stdout:
            for line in proc.stdout:
                m = _PERCENT.match(line)
                if not m:
                    continue
                pct = max(0, min(100, int(m.group(1))))
                if pct != last:
                    last = pct
                    self._set("installing", "", pct)
        rc = proc.wait()
        # Der Rückgabewert sagt nichts (0 auch bei Fehlschlag): Nur die Marke zählt.
        if os.path.exists(self.marker):
            self._set("installed", "Das Update ist installiert und wird beim nächsten Start aktiv.")
            self._save()
            return True
        log.warning("Update fehlgeschlagen (rc=%s, keine Marke %s)", rc, self.marker)
        self._set("failed", "Das Update ist fehlgeschlagen. Versuche es später erneut.")
        return False
