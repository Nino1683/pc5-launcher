"""Vertrag der Einstellungen: Zeilen, Seiten, Bereiche.

Die Oberfläche kennt keine einzelne Einstellung. Ein Bereich (Modul couch.settings.<name>) liefert
Seiten aus Zeilen; die Seite zeichnet sie allgemein nach ihrem Typ. Diese Datei ist der gemeinsame
Vertrag aller Bereichs-Module: nur verträglich erweitern (neue Felder mit Vorgabe), nie umbauen.

Regeln für Bereiche:
- Seiten- und Zeilen-IDs beginnen immer mit "<bereich>." bzw. sind gleich der Bereichs-ID
  ("audio", "audio.output", "audio.devices", "network.wifi.connect:<ssid>"). Der Baum leitet
  page/set/action allein über diesen Vorsatz an den Bereich.
- page() darf blockieren (der Verdrahter ruft im Thread), Hauptseiten sollen bei warmem
  Zwischenspeicher unter 100 ms bleiben.
- Jede Zeile hat ein Backend oder entfällt; was hier nichts bewirkt, ist gesperrt
  (enabled=False) mit note, z. B. "Nicht unterstützt".
- Alles Systemverändernde geht durch deps.run_write, nie direkt durch subprocess. Was so nicht
  geht (Kennwort über D-Bus, eigener BlueZ-Agent, Update mit laufender Ausgabe), ist in
  backends/ gekapselt (nm.wifi_connect, bluez.GioBluez, osupdate.OsUpdater) und im Prüfmodus
  abgeschaltet.
- Fehler mit Text für die Oberfläche: SettingsError("deutscher Satz") werfen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

ROW_TYPES = ("toggle", "select", "slider", "action", "info", "page", "text", "confirm-action")

# Wer was aufruft: toggle/select/slider/text -> set(row_id, wert) (text: Eingabe der
# Bildschirmtastatur); action -> action(row_id); confirm-action -> action(row_id) nach bestätigter
# Rückfrage; page -> die Oberfläche öffnet Row.page selbst. settings.reset -> set(row_id,
# Row.default) für Zeilen mit Vorgabe (der Baum schlägt sie auf der zuletzt gezeichneten Seite nach).

# Empfohlene Werte für Area.order (Reihenfolge der Kategorien wie in tree.AREA_MODULES)
AREA_ORDER = {"audio": 10, "display": 20, "network": 30, "accessories": 40, "games": 50,
              "performance": 60, "storage": 70, "home": 80, "notifications": 90,
              "accessibility": 100, "system": 110, "session": 120}

# Probezeit (Row.trial): set() wendet an und liefert {"trial": {"row", "seconds", "text"}}. Die
# Oberfläche fragt nach und meldet die Antwort über action(row_id, TRIAL_KEEP | TRIAL_REVERT).
# Kommt binnen "seconds" nichts, nimmt der Bereich die Änderung selbst zurück (deps.scheduler).
TRIAL_KEEP = "trial-keep"
TRIAL_REVERT = "trial-revert"


class SettingsError(Exception):
    """Fehler mit deutschem Satz für die Oberfläche. Der Baum macht daraus {"error", "message"}."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Row:
    id: str                         # "bereich.name", z. B. "audio.output"
    type: str                       # einer aus ROW_TYPES
    label: str
    value: Any = None               # toggle: bool; select: value; slider: Zahl; info: Text; text: Text
    options: list | None = None     # select: [{"value", "label", "note"?}]
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    enabled: bool = True
    hint: str = ""                  # Erklärzeile unter der Zeile
    note: str = ""                  # kurzer Zustand rechts, z. B. "Nicht unterstützt"
    page: str | None = None         # type "page": Ziel-Seite
    confirm: dict | None = None     # confirm-action: {"title", "text", "ok", "cancel"}
    trial: dict | None = None       # Probezeit: {"seconds": 15, "text": "Bild behalten?"}
    secret: bool = False            # text: Eingabe verdecken (WLAN-Kennwort)
    learn: bool = True              # text: False = die Bildschirmtastatur merkt sich keine Wörter (Kennungen, E100)
    # Vorgabe in der Form der Zeile (Prozent beim Regler, Wert der Auswahl …) für „Auf Standard
    # zurücksetzen“ (settings.reset, 07 §8). None = lässt sich nicht einzeln zurücksetzen.
    default: Any = None

    def to_dict(self) -> dict:
        """Fassung für die Seite: immer dieselben Schlüssel, nur JSON-Werte."""
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "value": self.value,
            "options": [dict(o) for o in self.options] if self.options is not None else None,
            "min": self.min,
            "max": self.max,
            "step": self.step,
            "unit": self.unit,
            "enabled": bool(self.enabled),
            "hint": self.hint,
            "note": self.note,
            "page": self.page,
            "confirm": dict(self.confirm) if self.confirm is not None else None,
            "trial": dict(self.trial) if self.trial is not None else None,
            "secret": bool(self.secret),
            "learn": bool(self.learn),
            "default": self.default,
        }


@dataclass
class Page:
    id: str
    title: str
    rows: list[Row] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "rows": [r.to_dict() for r in self.rows]}


class Area(Protocol):
    """Ein Bereich = ein Modul couch.settings.<name> mit make_area(deps) -> Area."""
    id: str        # zugleich ID der Hauptseite und Vorsatz aller Zeilen-IDs
    label: str     # Name der Kategorie in der linken Spalte
    icon: str      # Name des Symbols in icons.svg
    order: int     # Reihenfolge der Kategorien (aufsteigend)

    def pages(self) -> list[str]: ...
    # Seiten-IDs dieses Bereichs; die erste ist die Hauptseite. Unterseiten mit veränderlichem
    # Namen ("network.wifi:<ssid>") müssen hier nicht stehen, der Vorsatz genügt.

    def page(self, page_id: str) -> Page: ...
    # Blockierend erlaubt. Unbekannte Seite: KeyError.

    def set(self, row_id: str, value: Any) -> dict | None: ...
    # toggle/select/slider/text. Rückgabe {"message"?: str, "refresh"?: bool, "trial"?: {...}};
    # refresh=True heißt: andere Zeilen der Seite haben sich geändert, Seite neu holen.

    def action(self, row_id: str, value: Any = None) -> dict | None: ...
    # action/confirm-action (nach bestätigter Rückfrage) und die Antwort auf eine Probezeit.
    # Rückgabe wie set(); zusätzlich "page"?: Seiten-ID, die die Oberfläche öffnen soll.


class Scheduler(Protocol):
    """Zeitgeber und Hintergrundarbeit, einsteckbar (GLib im Home, Attrappe im Test)."""

    def later(self, seconds: float, fn: Callable[[], None]) -> Any: ...   # einmalig; liefert Griff
    def cancel(self, handle: Any) -> None: ...                           # unbekannter Griff: nichts tun
    def background(self, fn: Callable[[], None]) -> None: ...            # fn in einem Thread ausführen


# --- Kurzformen, damit Bereiche lesbar bleiben ---------------------------------------------

def toggle(id: str, label: str, value: bool, **kw) -> Row:
    return Row(id=id, type="toggle", label=label, value=bool(value), **kw)


def select(id: str, label: str, value: Any, options: list, **kw) -> Row:
    return Row(id=id, type="select", label=label, value=value, options=list(options), **kw)


def slider(id: str, label: str, value: float, min: float = 0, max: float = 100, step: float = 1,
           unit: str = "", **kw) -> Row:
    return Row(id=id, type="slider", label=label, value=value, min=min, max=max, step=step,
               unit=unit, **kw)


def action(id: str, label: str, **kw) -> Row:
    return Row(id=id, type="action", label=label, **kw)


def info(id: str, label: str, value: Any = "", **kw) -> Row:
    return Row(id=id, type="info", label=label, value=value, **kw)


def link(id: str, label: str, page: str, **kw) -> Row:
    """Zeile, die eine Unterseite öffnet."""
    return Row(id=id, type="page", label=label, page=page, **kw)


def text(id: str, label: str, value: str = "", secret: bool = False, **kw) -> Row:
    return Row(id=id, type="text", label=label, value=value, secret=secret, **kw)


def confirm_action(id: str, label: str, title: str, text: str, ok: str = "OK",
                   cancel: str = "Abbrechen", **kw) -> Row:
    return Row(id=id, type="confirm-action", label=label,
               confirm={"title": title, "text": text, "ok": ok, "cancel": cancel}, **kw)


def option(value: Any, label: str, note: str = "") -> dict:
    o = {"value": value, "label": label}
    if note:
        o["note"] = note
    return o
