"""Einstellungen › Barrierefreiheit.

Nur was die Oberfläche umsetzt: Bewegung reduzieren und Textgröße. Beides sind reine
Einstellungen; die Seite liest sie über sys.state bzw. das Änderungs-Ereignis der Config.
"""
from __future__ import annotations

from .rows import AREA_ORDER, Page, SettingsError, option, select, toggle

AREA_ID = "accessibility"

DEFAULTS = {
    "ui.reduce_motion": False,
    "ui.scale": 1.0,
}

TEXT_SIZES = ((1.0, "Normal"), (1.1, "Groß"), (1.2, "Sehr groß"))


def nearest_scale(value) -> float:
    """Gespeicherten Wert auf eine der angebotenen Stufen legen (JSON macht aus 1.0 eine 1)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = DEFAULTS["ui.scale"]
    return min((s for s, _ in TEXT_SIZES), key=lambda s: abs(s - v))


class AccessibilityArea:
    id = AREA_ID
    label = "Barrierefreiheit"
    icon = "access"
    order = AREA_ORDER.get(AREA_ID, 100)

    def __init__(self, deps):
        self.config = deps.config

    def pages(self) -> list[str]:
        return [AREA_ID]

    def page(self, page_id: str) -> Page:
        if page_id != AREA_ID:
            raise KeyError(page_id)
        return Page(AREA_ID, self.label, [
            toggle("accessibility.reduce_motion", "Bewegung reduzieren",
                   self.config.get("ui.reduce_motion", DEFAULTS["ui.reduce_motion"]),
                   hint="Schaltet Übergänge und Bewegungen der Oberfläche ab.",
                   default=DEFAULTS["ui.reduce_motion"]),
            select("accessibility.text_size", "Textgröße",
                   nearest_scale(self.config.get("ui.scale", DEFAULTS["ui.scale"])),
                   [option(s, label) for s, label in TEXT_SIZES], default=DEFAULTS["ui.scale"]),
        ])

    def set(self, row_id: str, value):
        if row_id == "accessibility.reduce_motion":
            self.config.set("ui.reduce_motion", bool(value))
            return None
        if row_id == "accessibility.text_size":
            try:
                v = float(value)
            except (TypeError, ValueError):
                raise SettingsError("Diese Textgröße gibt es nicht.") from None
            if min(abs(s - v) for s, _ in TEXT_SIZES) > 0.005:
                raise SettingsError("Diese Textgröße gibt es nicht.")
            self.config.set("ui.scale", nearest_scale(v))
            return None
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def action(self, row_id: str, value=None):
        raise SettingsError("Diese Aktion gibt es nicht.")


def make_area(deps) -> AccessibilityArea:
    return AccessibilityArea(deps)
