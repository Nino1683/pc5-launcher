"""Einstellungen › Mitteilungen: welche Einblendungen erscheinen dürfen.

Reine Einstellungen; der Mitteilungsdienst des Kerns (couch/notify.py) liest sie vor jeder
Einblendung: notify.popups (aus = nur noch die Liste), notify.in_game (aus = nichts über einem
Spiel oder einer App, außer der Akkuwarnung), notify.battery (aus = keine Akkuwarnung).
E108: notify.weekly (Wochenrückblick „Deine Woche …“, wiring_stats.py) liest die Verdrahtung selbst.
"""
from __future__ import annotations

from .rows import AREA_ORDER, Page, SettingsError, toggle

AREA_ID = "notifications"

DEFAULTS = {
    "notify.popups": True,
    "notify.in_game": True,
    "notify.battery": True,
    "notify.weekly": True,                  # E108
}

# Zeile -> (Schlüssel, Beschriftung, Erklärung)
_TOGGLES = {
    "notifications.popups": ("notify.popups", "Einblendungen anzeigen", ""),
    "notifications.in_game": ("notify.in_game", "Während Spielen anzeigen",
                              "Aus: Solange ein Spiel oder eine App vorn ist, stehen Mitteilungen "
                              "nur in der Liste. Die Akkuwarnung kommt trotzdem."),
    "notifications.battery": ("notify.battery", "Akkuwarnung des Controllers",
                              "Meldet sich, wenn der Akku des Controllers unter 15 % fällt."),
    # E108: steht auch ohne Einblendungen in der Liste, darum nie gesperrt
    "notifications.weekly": ("notify.weekly", "Wochenrückblick",
                             "Zu Beginn jeder Woche eine Mitteilung mit deiner Spielzeit der Vorwoche."),
}
_ALWAYS = ("notifications.popups", "notifications.weekly")


class NotificationsArea:
    id = AREA_ID
    label = "Mitteilungen"
    icon = "bell"
    order = AREA_ORDER.get(AREA_ID, 90)

    def __init__(self, deps):
        self.config = deps.config

    def pages(self) -> list[str]:
        return [AREA_ID]

    def page(self, page_id: str) -> Page:
        if page_id != AREA_ID:
            raise KeyError(page_id)
        popups = bool(self.config.get("notify.popups", DEFAULTS["notify.popups"]))
        rows = []
        for row_id, (key, label, text) in _TOGGLES.items():
            # Ohne Einblendungen haben die beiden anderen Schalter nichts zu schalten.
            rows.append(toggle(row_id, label, self.config.get(key, DEFAULTS[key]), hint=text,
                               enabled=popups or row_id in _ALWAYS, default=DEFAULTS[key]))
        return Page(AREA_ID, self.label, rows)

    def set(self, row_id: str, value):
        if row_id not in _TOGGLES:
            raise SettingsError("Diese Einstellung lässt sich nicht ändern.")
        self.config.set(_TOGGLES[row_id][0], bool(value))
        return {"refresh": True} if row_id == "notifications.popups" else None

    def action(self, row_id: str, value=None):
        raise SettingsError("Diese Aktion gibt es nicht.")


def make_area(deps) -> NotificationsArea:
    return NotificationsArea(deps)
