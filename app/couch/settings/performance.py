"""Einstellungen › Leistung: Leistungsanzeige (mangoapp) und Bildratenbegrenzung.

Die Arbeit macht couch.perf.PerfService. Der Kern steckt seinen Dienst als deps.extra["perf"] ein,
damit Einstellungen, Control Center (RPC perf.get/perf.set) und die Werte je Spiel denselben
Zustand sehen; ohne ihn legt der Bereich einen eigenen an.
"""
from __future__ import annotations

from .. import perf as perf_mod
from .rows import AREA_ORDER, Page, SettingsError, info, option, select

AREA_ID = "performance"

DEFAULTS = {k: v for k, v in perf_mod.DEFAULTS.items() if k.startswith("perf.")}

_OVERLAY_NOTES = {"bar": "Bildrate, CPU, Grafikkarte und Speicher in einer Zeile",
                  "detailed": "Kasten mit Auslastung, Temperaturen und Verlauf"}


class PerformanceArea:
    id = AREA_ID
    label = "Leistung"
    icon = "speed"
    order = AREA_ORDER.get(AREA_ID, 60)

    def __init__(self, deps):
        self.deps = deps
        extra = getattr(deps, "extra", None) or {}
        self.perf = extra.get("perf") or perf_mod.PerfService(
            deps.config, deps.gs, run_read=getattr(deps, "run_read", None),
            run_write=getattr(deps, "run_write", None), apply_display=extra.get("apply_display"))

    def pages(self) -> list[str]:
        return [AREA_ID]

    def page(self, page_id: str) -> Page:
        """Blockiert nicht (Einstellungen und zwei X-Eigenschaften)."""
        if page_id != AREA_ID:
            raise KeyError(page_id)
        st = self.perf.get()
        available = st["overlayAvailable"]
        auto = perf_mod.auto_font_scale(self._root_height())
        auto_name = next((perf_mod.FONT_SCALE_LABELS[k] for k, v in perf_mod.FONT_SCALES.items()
                          if abs(v - auto) < 0.01), "")
        rows = [
            select("performance.overlay", "Leistungsanzeige", st["overlay"],
                   [option(k, perf_mod.OVERLAY_LABELS[k], _OVERLAY_NOTES.get(k, ""))
                    for k in perf_mod.OVERLAY_LEVELS],
                   enabled=available, note="" if available else "Nicht verfügbar",
                   hint="Blendet Messwerte über Spielen und dem Home ein.", default=DEFAULTS["perf.overlay"]),
            select("performance.font_scale", "Schriftgröße der Anzeige", st["fontScale"],
                   [option(k, label, auto_name if k == "auto" else "")
                    for k, label in perf_mod.FONT_SCALE_LABELS.items()],
                   enabled=available, note="" if available else "Nicht verfügbar",
                   hint="„Automatisch“ richtet sich nach der Auflösung des Bildschirms.",
                   default=DEFAULTS["perf.font_scale"]),
            select("performance.fps_limit", "Bildratenbegrenzung", st["fpsLimit"],
                   [option(n, "%d Bilder pro Sekunde" % n if n else "Aus") for n in st["fpsLimits"]],
                   enabled=st["limitAvailable"], note="" if st["limitAvailable"] else "Nicht verfügbar",
                   default=DEFAULTS["perf.fps_limit"],
                   hint="Gilt für Spiele; das Home läuft immer mit voller Bildrate. Möglich sind "
                        "nur Werte, durch die sich die Bildwiederholrate des Bildschirms teilen lässt."),
            info("performance.refresh", "Bildschirm", "%d Hz" % st["refresh"]),
        ]
        return Page(AREA_ID, self.label, rows)

    def set(self, row_id: str, value):
        try:
            if row_id == "performance.overlay":
                self.perf.set(overlay=value)
            elif row_id == "performance.font_scale":
                self.perf.set(fontScale=value)
            elif row_id == "performance.fps_limit":
                self.perf.set(fpsLimit=value)
            else:
                raise SettingsError("Diese Einstellung lässt sich nicht ändern.")
        except ValueError as e:
            raise SettingsError(str(e)) from None
        return None

    def action(self, row_id: str, value=None):
        raise SettingsError("Diese Aktion gibt es nicht.")

    def _root_height(self) -> int:
        try:
            return int(self.deps.gs.root_size()[1])
        except Exception:  # noqa: BLE001
            return 0


def make_area(deps) -> PerformanceArea:
    return PerformanceArea(deps)
