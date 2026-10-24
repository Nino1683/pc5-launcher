"""Erster Start und Zustand des Einrichtungs-Assistenten (E105, Bauplan 8.2).

Marke <config>/setup.json (atomar, 0600): {state, at, items: [{id, flatpak, installation, at}],
folders: [<angelegt>], gamesDisk, emuRoot}. state ∈ new shown later done existing running interrupted.
Die Marke ist keine Einstellung: „Einstellungen zurücksetzen“ leert settings.json (config.reset_all),
setup.json bleibt, und der Assistent kommt danach nicht wieder.

due() entscheidet einmal beim Einhängen (wiring_setup):
  - Prüfmodus: nur mit COUCH_TEST_SETUP=1 (wie COUCH_TEST_INTRO)
  - Marke da: done, later, existing, shown → nie von selbst (shown: ein Absturz während des
    Assistenten erzeugt keine Schleife); running/interrupted → nicht von selbst, aber Mitteilung (D4)
  - keine Marke, aber Bestand (settings.json, library.json, home-order.json oder library-cache.json
    lag schon da; ältere Installation) → still {"state": "existing"} schreiben, nie zeigen
  - sonst zeigen und sofort {"state": "shown"} schreiben
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .. import jsontext, log, paths, system

_log = log.get("setup.state")

STATES = ("new", "shown", "later", "done", "existing", "running", "interrupted")
NEVER = ("done", "later", "existing", "shown")
RESUME = ("running", "interrupted")
FILE = "setup.json"


def setup_file() -> str:
    return os.path.join(paths.config_dir(), FILE)


def existing_files() -> list:
    """Dateien eines Bestands (dieselbe Erkennung wie portable.migrate, Bauplan 4.6)."""
    return [paths.settings_file(), paths.library_file(), os.path.join(paths.config_dir(), "home-order.json"),
            paths.library_cache_file()]


def is_existing() -> bool:
    """Bestand? portable.existing() (Teil P3) kennt den Stand von vor diesem Start (Schnappschuss früh in
    main, Marke portable.json); ohne das Modul gelten die Dateien, wie sie jetzt liegen."""
    try:
        from .. import portable
        return bool(portable.existing())
    except Exception:  # noqa: BLE001  (fehlt oder scheitert: die Dateien entscheiden)
        return any(os.path.lexists(p) for p in existing_files())


def read() -> dict:
    """Inhalt von setup.json; {} wenn es sie nicht gibt oder sie kaputt ist."""
    try:
        with open(setup_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        _log.warning("setup.json nicht lesbar (%s)", type(e).__name__)
        return {"state": "broken"}
    return data if isinstance(data, dict) else {"state": "broken"}


def write(data: dict) -> dict:
    data = {k: v for k, v in data.items() if v is not None}
    try:
        system.write_atomic(setup_file(), jsontext.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o600)
    except OSError as e:
        _log.error("setup.json nicht schreibbar: %s", e)
    return data


def update(clock=time.time, **fields: Any) -> dict:
    """Felder setzen (state, items, folders, gamesDisk, emuRoot …) und speichern."""
    data = read()
    if data.get("state") == "broken":
        data = {}
    data.update(fields)
    if "state" in fields:
        data["at"] = int(clock())
    return write(data)


def state_of(data: dict | None = None) -> str:
    """Zustand aus der Marke. Ohne Marke (vor dem Einhängen, Prüfmodus ohne COUCH_TEST_SETUP) so, wie due()
    entschiede: Bestand → "existing" (ältere Installation, auch im nachgestellten Lauf), sonst "new"."""
    data = read() if data is None else data
    value = data.get("state") if isinstance(data, dict) else None
    if value in STATES:
        return value
    if not data:
        return "existing" if is_existing() else "new"
    return "existing"


def due(config: Any = None, clock=time.time) -> dict:
    """{"due": bool, "state": str, "resume": bool}; schreibt die Marke dabei wie oben beschrieben.
    resume: eine Installation lief beim letzten Ende des Home noch (Mitteilung D4)."""
    if paths.test_mode() and os.environ.get("COUCH_TEST_SETUP") != "1":
        data = read()                           # Prüfläufe ohne Schalter: nichts schreiben, nie zeigen
        return {"due": False, "state": state_of(data), "resume": data.get("state") in RESUME}
    data = read()
    if data:
        current = state_of(data)
        if data.get("state") == "broken":
            # eine kaputte Marke ist kein neuer Rechner: nie von selbst zeigen
            update(clock, state="existing")
            return {"due": False, "state": "existing", "resume": False}
        return {"due": False, "state": current, "resume": current in RESUME}
    if is_existing():
        update(clock, state="existing")
        _log.info("Einrichtung: Bestand erkannt, der Assistent erscheint nicht von selbst")
        return {"due": False, "state": "existing", "resume": False}
    update(clock, state="shown")
    return {"due": True, "state": "shown", "resume": False}


def record_items(items: list, clock=time.time) -> None:
    """Installierte Flatpaks merken ({id, flatpak, installation, at}); doppelte ersetzt der neue."""
    data = read()
    if data.get("state") == "broken":
        data = {}
    known = {str(i.get("flatpak") or i.get("id")): i for i in data.get("items") or [] if isinstance(i, dict)}
    for item in items:
        known[str(item.get("flatpak") or item.get("id"))] = {**item, "at": int(item.get("at") or clock())}
    data["items"] = list(known.values())
    write(data)


def record_folders(created: list) -> None:
    data = read()
    if data.get("state") == "broken":
        data = {}
    folders = [f for f in data.get("folders") or [] if isinstance(f, str)]
    for f in created:
        if f not in folders:
            folders.append(f)
    data["folders"] = folders
    write(data)
