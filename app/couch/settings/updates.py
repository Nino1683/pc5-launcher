"""Bereich „Updates“ (E133, Wunsch 2026-10-27: „In den Einstellungen vom PC5 Launcher soll es einen eigenen
Update-Bereich geben, nicht mehr unter System. Hier soll es übersichtlicher werden, ich muss in zu viele Untermenüs,
um alle Updates sehen zu können. Für den PC5 Launcher soll es auch ein Changelog geben.“).

Eine Seite für alles, in der linken Leiste unter „Spiele“ (AREA_ORDER 55, Wunsch 2026-10-27; ohne Rollen zu sehen,
samt Punkt). Je Bereich eine Zeile; steht etwas an, ist die Zeile selbst der Knopf:

  Alles aktualisieren          updates.all     Apps und Epic-Spiele (was heute ohne Rückfrage geht)
  Jetzt nach Updates suchen    updates.check   Systemsoftware, Apps, Proton, Spiele und PC5 Launcher auf einmal
  Systemsoftware               system.os.install | system.os.reboot | Verweis auf system.software
  PC5 Launcher                 system.launcher.update | system.launcher.restart | Verweis auf system.launcher
  Was ist neu                  updates.notes   Changelog im Blätter-Dialog (Ergebnis {"notes": …}, changelog.py)
  Apps und Laufzeiten          system.apps.update | Verweis auf system.apps (Liste)
  Proton-CachyOS               system.updates.proton.install | Zustand; darunter „Proton-Versionen“ (system.proton)
  Spiele                       Zustand, je Epic-Spiel mit Update system.updates.game:<app>

Zustand, Abläufe, Rückfragen und Sperren gehören weiter dem Bereich system (settings/system.py, Bausteine
update_*): Zeilen mit „system.“ leitet der Baum nach ihrem Vorsatz dorthin, auch von dieser Seite aus; ihre IDs und
Rückfragen sind dieselben wie auf den Unterseiten. Die Unterseiten bleiben für Einzelheiten (Liste der Apps, der
Proton-Versionen, Fassung des Systems). Den Bereich system holt sich diese Seite über den Baum (attach).
"""
from __future__ import annotations

import logging

from . import rows as R
from .rows import AREA_ORDER, Page, SettingsError
from .system import ASK_HINT, NOTES_HINT, NOTES_HINT_NEW

log = logging.getLogger("couch.settings.updates")

PAGE = "updates"
UNAVAILABLE = "Das ist gerade nicht verfügbar."


class UpdatesArea:
    id = PAGE
    label = "Updates"
    icon = "update"
    order = AREA_ORDER["updates"]

    def __init__(self, deps):
        self.deps = deps
        self._tree = None

    def attach(self, tree) -> None:
        """Vom Baum nach dem Laden aller Bereiche: Zustand und Aktionen liegen im Bereich system."""
        self._tree = tree

    def _system(self):
        area = self._tree.area("system") if self._tree is not None else None
        return area if callable(getattr(area, "update_os_row", None)) else None

    # --- Seite ---------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return [PAGE]

    def page(self, page_id: str) -> Page:
        if page_id != PAGE:
            raise KeyError(page_id)
        return Page(PAGE, "Updates", self._rows())

    def _rows(self) -> list:
        sysa = self._system()
        if sysa is None:
            return [R.info("updates.gone", UNAVAILABLE, enabled=False)]
        rows = []
        rows += self._head_rows(sysa)
        rows.append(self._safe(sysa.update_os_row, "updates.os.failed", "Systemsoftware"))
        launcher = self._safe(sysa.update_launcher_row, "updates.launcher.failed", "PC5 Launcher")
        if launcher is not None:
            rows.append(launcher)
            rows.append(self._notes_row(sysa))
        rows.append(self._safe(sysa.update_apps_row, "updates.apps.failed", "Apps und Laufzeiten"))
        rows += self._safe_list(sysa.update_proton_rows, "updates.proton.failed", "Proton")
        rows += self._safe_list(sysa.update_game_rows, "updates.games.failed", "Spiele")
        return rows

    def _head_rows(self, sysa) -> list:
        try:
            s = sysa.update_summary()
        except Exception:  # noqa: BLE001  (eine kaputte Quelle darf die Übersicht nicht kosten)
            log.exception("Übersicht der Updates")
            s = {"auto": 0, "ask": [], "checking": False}
        n = s["auto"]
        if n:
            value, hint = ("%d Updates" % n if n != 1 else "1 Update"), ASK_HINT
        else:
            value = ""
            hint = ("Ohne Rückfrage gibt es gerade nichts zu aktualisieren. Systemsoftware, PC5 Launcher und Proton "
                    "aktualisierst du mit dem Knopf in ihrer Zeile." if s["ask"] else "Gerade gibt es nichts zu aktualisieren.")
        return [
            R.action("updates.all", "Alles aktualisieren", value=value, enabled=bool(n), hint=hint),
            R.action("updates.check", "Jetzt nach Updates suchen", value="Suche läuft …" if s["checking"] else "",
                     hint="Fragt alles auf einmal: Systemsoftware, PC5 Launcher, Apps, Proton und Spiele. Von selbst "
                          "geschieht das alle 12 Stunden, für den PC5 Launcher einmal am Tag."),
        ]

    def _notes_row(self, sysa) -> R.Row:
        s = sysa.launcher_status() or {}
        new = s.get("pending") or s.get("available") or ""
        return R.action("updates.notes", "Was ist neu", value=new or str(s.get("own") or ""),
                        hint=NOTES_HINT_NEW if new else NOTES_HINT)

    @staticmethod
    def _safe(fn, fail_id: str, label: str):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.exception("Updates: Zeile %s", label)
            return R.info(fail_id, label, "Gerade nicht lesbar", enabled=False)

    @classmethod
    def _safe_list(cls, fn, fail_id: str, label: str) -> list:
        out = cls._safe(fn, fail_id, label)
        return out if isinstance(out, list) else [out]

    # --- Aktionen ------------------------------------------------------------------------

    def set(self, row_id: str, value):
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        sysa = self._system()
        if sysa is None:
            raise SettingsError(UNAVAILABLE)
        if row_id == "updates.all":
            return sysa.update_all()
        if row_id == "updates.check":
            return sysa.check_all()
        if row_id == "updates.notes":
            return sysa.launcher_notes()
        raise SettingsError("Diese Aktion gibt es nicht.")


def make_area(deps) -> UpdatesArea:
    return UpdatesArea(deps)
