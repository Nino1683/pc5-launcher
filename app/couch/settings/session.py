"""Bereich „PC5 Launcher“ (bis 2026-10-19 „Konsolen-Modus“): alles, was diese Oberfläche selbst betrifft.

Version, installierte Fassung und ihr Ordner, Stand der Rückfallebene (Sicherungen und
Fehlstarts des Supervisors aus <state>, nur gelesen), Start mit Steam (<config>/start-steam,
Bauplan E26: Die Sitzung beginnt mit Steam Big Picture, der Supervisor bleibt wach, Steams Ende
und der Notgriff führen ins Home), Wechsel zu Steam Big Picture und zum Desktop, Neustart der
Oberfläche (deps.extra["power"] mit "steam", "desktop" bzw. "restart-session"), Erklärung des
Notgriffs, Orte der Protokolle, Einstellungen zurücksetzen. „Einrichtung“ (E105) und „Einstellungen
sichern und übernehmen“ (E106) öffnen nur eigene Bildschirme der Oberfläche ({"ui": …}).
Geschrieben wird nur im eigenen Einstellungsordner (deps.paths.config_dir): die Datei
start-steam und settings.json – sonst nichts. Der harte Notschalter <config>/disabled ist nur
für die Reparatur vom Desktop aus gedacht und kommt hier bewusst nicht vor: Aus ihm führte
kein Weg am Fernseher zurück.

deps.extra (alles freiwillig):
    "power"(id)          wie power.do; hier "steam", "desktop", "restart-session"
    "busy"()             -> [deutsche Sätze], was ein Wechsel gerade beenden oder stören würde
                         (laufende Spiele, Installationen); sie stehen in den Rückfragen (E31)
    "reset_settings"()   setzt alle Einstellungen zurück und wendet sie an
    "release_dir"        Ordner, aus dem die Oberfläche gerade läuft
"""
from __future__ import annotations

import copy
import json
import logging
import os
import time

from . import rows as R
from .backends import fmt_bytes
from .rows import AREA_ORDER, Page, SettingsError

log = logging.getLogger("couch.settings.session")

try:
    from .. import VERSION
except Exception:  # noqa: BLE001
    VERSION = "unbekannt"


# Zeile -> Aktion des Ein/Aus-Dienstes (power.do)
_POWER = {"session.steam": "steam", "session.desktop": "desktop", "session.restart": "restart-session"}
# Zeile -> eigener Bildschirm der Oberfläche (E103: screens/settings.js öffnet result.ui, wenn es ihn gibt)
_SCREENS = {"session.setup": {"ui": "setup-welcome", "params": {"from": "settings"}},     # E105
            "session.backup": {"ui": "backup"}}                                         # E106


class SessionArea:
    id = "session"
    label = "PC5 Launcher"
    icon = "info"
    order = AREA_ORDER["session"]

    def __init__(self, deps):
        self.deps = deps

    # --- Orte ----------------------------------------------------------------------------

    def _start_steam_file(self) -> str:
        return os.path.join(self.deps.paths.config_dir, "start-steam")

    def _settings_file(self) -> str:
        return os.path.join(self.deps.paths.config_dir, "settings.json")

    def _release(self, name: str) -> str | None:
        """Name der Fassung hinter dem Verweis <data>/<name> (current | previous), sonst None."""
        link = os.path.join(self.deps.paths.data_dir, name)
        if not os.path.exists(link):
            return None
        return os.path.basename(os.path.realpath(link))

    def _release_dir(self) -> str:
        """Ordner, aus dem diese Oberfläche läuft (<release> über app/couch/settings/)."""
        given = self.deps.extra.get("release_dir")
        if isinstance(given, str) and given:
            return os.path.realpath(given)
        here = os.path.realpath(__file__)
        for _ in range(4):
            here = os.path.dirname(here)
        return here

    def _running_from_release(self) -> bool:
        cur = os.path.join(self.deps.paths.data_dir, "current")
        if not os.path.exists(cur):
            return False
        return os.path.realpath(cur) == self._release_dir()

    # --- Rückfallebene (nur lesen; die Dateien gehören dem Supervisor) ---------------------

    def _fuses(self) -> list[tuple[str, str]]:
        """Gesetzte Sicherungen: [(Name, Zeile mit Zeit und Grund)]."""
        out = []
        for name in ("fuse-current", "fuse-launcher"):
            path = os.path.join(self.deps.paths.state_dir, name)
            if not os.path.lexists(path):
                continue
            why = ""
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read(2000).splitlines()
                why = lines[1].strip() if len(lines) > 1 else ""
            except OSError:
                pass
            out.append((name, why))
        return out

    def _failed_starts(self) -> list[float]:
        try:
            with open(os.path.join(self.deps.paths.state_dir, "failed-starts"), encoding="utf-8") as f:
                data = json.loads(f.read(20000))
        except (OSError, ValueError):
            return []
        stamps = data.get("fails") if isinstance(data, dict) else None
        return [float(t) for t in stamps or []
                if isinstance(t, (int, float)) and not isinstance(t, bool)]

    def _fallback_rows(self) -> list:
        fuses = self._fuses()
        names = {"fuse-current": "Vorherige Fassung statt der neuen",
                 "fuse-launcher": "Steam Big Picture statt dieser Oberfläche"}
        if fuses:
            value = " · ".join(names[n] for n, _ in fuses)
            hint = " ".join(why for _, why in fuses if why) or \
                "Eine Sicherung gilt, bis eine neue Fassung installiert ist."
        else:
            value, hint = "Keine gesetzt", ""
        rows = [R.info("session.fuses", "Sicherungen", value, hint=hint,
                       note="Rückfallebene aktiv" if fuses else "")]
        fails = self._failed_starts()
        if fuses:
            state = "Hat zuletzt gegriffen"
        elif fails:
            last = time.strftime("%d.%m.%Y, %H:%M", time.localtime(max(fails)))
            n = len(fails)
            state = ("1 Fehlstart" if n == 1 else "%d Fehlstarts" % n) + ", zuletzt am %s Uhr" % last
        else:
            state = "Hat nicht eingegriffen"
        rows.append(R.info("session.fallback", "Rückfallebene", state,
                           hint="Startet diese Oberfläche dreimal nicht, nimmt die Konsole die vorherige "
                                "Fassung und danach Steam Big Picture."))
        return rows

    def _busy_text(self) -> str:
        """Was ein Wechsel beenden würde, als angehängte Sätze (E31)."""
        fn = self.deps.extra.get("busy")
        if not callable(fn):
            return ""
        try:
            lines = [str(t).strip() for t in (fn() or []) if str(t).strip()]
        except Exception:  # noqa: BLE001 - die Rückfrage kommt auch ohne diese Sätze
            log.exception("busy() wirft")
            return ""
        return (" " + " ".join(lines)) if lines else ""

    # --- Seiten --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return ["session", "session.logs"]

    def page(self, page_id: str) -> Page:
        if page_id == "session":
            return Page(page_id, "PC5 Launcher", self._main_rows())
        if page_id == "session.logs":
            return Page(page_id, "Protokolle", self._log_rows())
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        power = callable(self.deps.extra.get("power"))
        off = "" if power else "Nicht verfügbar"
        cur, prev = self._release("current"), self._release("previous")
        busy = self._busy_text()
        rows = [
            R.info("session.version", "Version", VERSION),
            R.info("session.release", "Installierte Fassung", cur or "Nicht installiert",
                   note="" if not cur else ("Aktiv" if self._running_from_release() else "Läuft aus dem Entwicklungsordner")),
            R.info("session.release-dir", "Release-Ordner", self._release_dir()),
            R.info("session.previous", "Vorherige Fassung", prev or "Keine",
                   hint="Startet die installierte Fassung dreimal nicht, nimmt die Konsole von selbst die vorherige."),
        ]
        rows += self._fallback_rows()
        rows += [
            R.toggle("session.use-steam", "Beim nächsten Start mit Steam Big Picture beginnen",
                     os.path.isfile(self._start_steam_file()),
                     hint="Gilt ab dem nächsten Start. Zurück zu dieser Oberfläche: Steam beenden oder "
                          "PS + Create + Options 5 Sekunden halten."),
            # mit Rückfrage wie die Zeile unter „Spiele“: Die Übergabe schließt alles, was läuft (E31)
            R.confirm_action("session.steam", "Zu Steam Big Picture wechseln", title="Zu Steam Big Picture wechseln?",
                             text="Das Home wird beendet, laufende Spiele und Apps werden geschlossen. "
                                  "Zurück zur Konsole: Steam beenden oder PS + Create + Options 5 Sekunden halten."
                                  + busy,
                             ok="Wechseln", enabled=power, note=off,
                             hint="Zurück zur Konsole: PS + Create + Options 5 Sekunden halten."),
            R.confirm_action("session.desktop", "Zum Desktop wechseln", title="Zum Desktop wechseln?",
                             text="Die Konsole wird beendet und der Desktop startet auf den Monitoren am "
                                  "Schreibtisch. Laufende Spiele werden geschlossen. Zurück geht es nur von dort."
                                  + busy,
                             ok="Wechseln", enabled=power, note=off),
            R.confirm_action("session.restart", "Oberfläche neu starten", title="Oberfläche neu starten?",
                             text="Die Oberfläche wird beendet und gleich wieder geladen, der Rechner bleibt an. "
                                  "Laufende Spiele und Apps werden geschlossen." + busy,
                             ok="Neu starten", enabled=power, note=off),
            R.info("session.rescue", "Notgriff", "PS + Create + Options 5 Sekunden halten",
                   hint="Hängt diese Oberfläche, wechselt der Notgriff zu Steam Big Picture – und von dort "
                        "auf dieselbe Weise wieder zurück."),
            # E105/E106: öffnen eigene Bildschirme (settings.action -> {"ui": <bildschirm-id>, "params"})
            R.action("session.setup", "Einrichtung", hint="Emulatoren, Apps und den Spiele-Ort einrichten oder ergänzen."),
            R.action("session.backup", "Einstellungen sichern und übernehmen",
                     hint="Für einen neuen Rechner oder eine Steam Machine."),
            R.link("session.logs-page", "Protokolle", "session.logs"),
            R.confirm_action("session.reset", "Einstellungen zurücksetzen", title="Einstellungen zurücksetzen?",
                             text="Alle Einstellungen dieser Oberfläche gehen auf die Vorgaben zurück, auch ein "
                                  "eingetragener SteamGridDB-Schlüssel. Spiele, Spielstände und die Bibliothek "
                                  "bleiben unberührt.",
                             ok="Zurücksetzen"),
        ]
        return rows

    def _log_rows(self) -> list:
        state = self.deps.paths.state_dir
        rows = []
        for rid, label, rel in (("home", "Oberfläche", "home.log"), ("supervisor", "Sitzung", "supervisor.log"),
                                ("apps", "Spiele und Apps", "apps")):
            path = os.path.join(state, rel)
            if os.path.isfile(path):
                note = fmt_bytes(os.path.getsize(path))
            elif os.path.isdir(path):
                try:
                    note = f"{len(os.listdir(path))} Dateien"
                except OSError:
                    note = ""
            else:
                note = "Noch nicht vorhanden"
            rows.append(R.info(f"session.log.{rid}", label, path, note=note))
        rows.append(R.info("session.log.hint", "Ansehen", "Im Desktop-Modus mit einem Texteditor",
                           hint="Die Protokolle helfen bei der Fehlersuche. Hier lassen sie sich nicht öffnen."))
        return rows

    # --- Schreiben -----------------------------------------------------------------------

    def set(self, row_id: str, value):
        if row_id == "session.use-steam":
            path = self._start_steam_file()
            if value:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("In den Einstellungen gesetzt am %s: Die Sitzung beginnt mit Steam Big Picture.\n"
                            % time.strftime("%Y-%m-%d %H:%M"))
            elif os.path.islink(path) or os.path.isfile(path):
                os.unlink(path)       # nur genau diese Datei; nie ein Ordner, nie rekursiv
            return None
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        if row_id in _POWER:
            fn = self.deps.extra.get("power")
            if not callable(fn):
                raise SettingsError("Das ist gerade nicht verfügbar.")
            try:
                fn(_POWER[row_id])
            except SettingsError:
                raise
            except Exception as e:  # noqa: BLE001
                # der Kern meldet „Es läuft schon ein Vorgang.“ u. Ä. mit einem deutschen Satz
                message = getattr(e, "message", None)
                if isinstance(message, str) and message:
                    raise SettingsError(message) from e
                raise
            return None
        if row_id == "session.reset":
            return self._reset()
        if row_id in _SCREENS:
            return copy.deepcopy(_SCREENS[row_id])
        raise SettingsError("Diese Aktion gibt es nicht.")

    def _reset(self) -> dict:
        cfg = self.deps.config
        fn = self.deps.extra.get("reset_settings")
        if callable(fn):
            fn()
        elif callable(getattr(cfg, "all", None)) and callable(getattr(cfg, "reset", None)):
            # Über das Config-Objekt: Beobachter erfahren jede Änderung, nichts gilt erst nach Neustart.
            for key in list(cfg.all()):
                cfg.reset(key)
        else:
            path = self._settings_file()
            if os.path.islink(path):
                raise SettingsError("Die Einstellungsdatei ist ein Verweis und wird nicht angefasst.")
            if os.path.isfile(path):
                os.replace(path, path + ".bak")     # eine Sicherung, damit nichts endgültig weg ist
            return {"message": "Einstellungen zurückgesetzt. Wirksam nach dem nächsten Start der Oberfläche.",
                    "refresh": True}
        return {"message": "Einstellungen zurückgesetzt.", "refresh": True}


def make_area(deps) -> SessionArea:
    return SessionArea(deps)
