"""Einrichtungs-Assistent (E105, Wunsch 2026-10-24: „Ein Install-Wizard, der den Nutzer fragt, was er
alles nutzen möchte, und dies dann für ihn installiert, wie die Emulatoren etc.“).

Bausteine (Bauplan docs/research/e103/SPEC.md Abschnitt 8):
  catalog.py   was angeboten wird (Emulatoren, Apps, Werkzeuge, Laufzeiten, ROM-Systeme)
  state.py     Erststart-Marke <config>/setup.json, due()
  detect.py    was auf dem Rechner schon da ist (nur lesend)
  plan.py      Auswahl auflösen, Platz rechnen
  folders.py   Emulator-Ordner bestimmen, roms/<system> und bios nur per mkdir anlegen
  umu.py       umu-launcher als ZIP-App mit fester Prüfsumme nach <data>/tools/umu
  jobs.py      ein Auftrag zugleich: Ordner → Werkzeuge → Flatpaks → Proton
Installiert wird nur über app/bin/couch-setup-flatpak (immer die Nutzer-Installation von Flatpak),
im Prüfmodus nur über tools/fake-flatpak (COUCH_SETUP_FLATPAK).

Einstellungen für Teile ohne eigenen Zugriff auf die Config (Bibliotheksquellen, Archiv-Installer):
wiring_setup bindet die laufende Config über bind_config(); ohne Bindung (eigenständige Läufe) wird
settings.json nur gelesen.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

_config_getter: Callable[[], Any] | None = None


def bind_config(getter: Callable[[], Any] | None) -> None:
    """getter() liefert ein Objekt mit get(key, default). None löst die Bindung (Tests)."""
    global _config_getter
    _config_getter = getter


def setting(key: str, default: Any = None) -> Any:
    """Wert einer Einstellung: aus der gebundenen Config, sonst aus settings.json (nur gelesen; eine
    kaputte Datei gilt als leer), sonst die Vorgabe aus config.DEFAULTS bzw. default."""
    getter = _config_getter
    if getter is not None:
        try:
            cfg = getter()
            if cfg is not None:
                return cfg.get(key, default)
        except Exception:  # noqa: BLE001  (eine tote Bindung darf keine Quelle kosten)
            pass
    from .. import config as config_mod, paths
    value = config_mod.DEFAULTS.get(key, default)
    try:
        with open(paths.settings_file(), encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and key in data:
            value = data[key]
    except (OSError, ValueError):
        pass
    return value


def flag(key: str, default: bool) -> bool:
    value = setting(key, default)
    return value if isinstance(value, bool) else default


def text(key: str) -> str:
    value = setting(key, "")
    return value.strip() if isinstance(value, str) else ""


def abspath_setting(key: str) -> str:
    """Pfad aus einer Einstellung, nur wenn absolut (sonst "" = automatisch)."""
    value = text(key)
    return os.path.normpath(value) if value and os.path.isabs(value) else ""
