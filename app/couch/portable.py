"""Bestand erkennen und einmal auf die Schlüssel von E104/E105 heben (Bauplan E103, Abschnitt 4.6).

„Bestand“ heißt: Diese Installation hatte schon Einstellungen oder eine Bibliothek, bevor die erste
Fassung mit E104 startete (etwa der Entwicklungsrechner). Dort bleibt alles wie bisher:

    library.games_disk = /run/media/<nutzer>/Spiele    (egal ob die Platte gerade eingehängt ist)
    library.roms       = False                         (ROM-Ordner ohne SRM erst auf Wunsch, E105)

Geprüft wird an den vier Dateien aus EXISTING_FILES, und zwar so, wie sie vor diesem Start lagen
(snapshot(), früh in main.main). Das Ergebnis steht danach für immer in <config>/portable.json:
„Einstellungen zurücksetzen“ leert nur settings.json und macht aus einem Bestand keine Neuinstallation
(paths.games_disk fragt existing()). Geschrieben wird settings.json nur über config.set.

    from couch import portable
    portable.snapshot()             # vor allem anderen, nur lesend
    portable.migrate(config)        # einmal beim Start des Home, vor der Verdrahtung; wirft nie
    portable.existing()             # Bestand? (auch für den Einrichtungs-Assistenten, E105)
"""
from __future__ import annotations

import json
import os
import time

from . import jsontext, log, paths, system

_log = log.get("portable")

FORMAT = 1
MARKER_NAME = "portable.json"
# Ort (paths-Funktion) und Name der Dateien, an denen ein Bestand zu erkennen ist
EXISTING_FILES = (("config", "settings.json"), ("data", "library.json"), ("config", "home-order.json"),
                  ("data", "library-cache.json"))
# Zustände in <config>/setup.json (Einrichtungs-Assistent, E105), die eine neue Installation belegen
_SETUP_NEW = ("new", "shown", "later", "done", "running", "interrupted")

# Je Ort (config- und data-Ordner; Tests wechseln den Käfig im selben Prozess):
_snapshot: dict = {}                    # Dateien, die beim ersten Blick dieses Prozesses schon lagen
_existing: dict = {}                    # entschieden (Marke, migrate oder Dateien)


def _where() -> tuple:
    return (paths.config_dir(), paths.data_dir())


def _dir(kind: str) -> str:
    return paths.config_dir() if kind == "config" else paths.data_dir()


def found_files() -> list[str]:
    """Die Dateien aus EXISTING_FILES, die gerade da sind (Namen). Liest nur."""
    return [name for kind, name in EXISTING_FILES if os.path.lexists(os.path.join(_dir(kind), name))]


def snapshot() -> list[str]:
    """Hält einmal je Prozess fest, welche der Dateien vor dem Start schon lagen (nur lesend)."""
    where = _where()
    if where not in _snapshot:
        try:
            _snapshot[where] = found_files()
        except Exception:                   # noqa: BLE001 - nie am Start scheitern
            _log.exception("Bestand: Dateien nicht prüfbar")
            _snapshot[where] = []
    return list(_snapshot[where])


def marker_file() -> str:
    return os.path.join(paths.config_dir(), MARKER_NAME)


def read_marker() -> dict | None:
    try:
        with open(marker_file(), encoding="utf-8") as f:
            data = json.loads(f.read(64 * 1024))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("existing"), bool) else None


def _setup_state() -> str:
    """Zustand aus <config>/setup.json des Assistenten (nur gelesen; die Datei gehört setup/state.py)."""
    try:
        with open(os.path.join(paths.config_dir(), "setup.json"), encoding="utf-8") as f:
            data = json.loads(f.read(256 * 1024))
    except (OSError, ValueError):
        return ""
    state = data.get("state") if isinstance(data, dict) else ""
    return state if isinstance(state, str) else ""


def _decide() -> bool:
    """Ohne Marke: Hat der Assistent hier schon als neue Installation gelaufen, ist es kein Bestand
    (sonst machte eine verlorene Marke aus einem neuen Rechner einen Bestand); sonst die Dateien von
    vor dem Start."""
    if _setup_state() in _SETUP_NEW:
        return False
    return bool(snapshot())


def existing() -> bool:
    """Ist das ein Bestand (E104/E105)? Auf dem Entwicklungsrechner: ja. Liest nur; das Ergebnis gilt für den Prozess."""
    where = _where()
    if where not in _existing:
        marker = read_marker()
        _existing[where] = bool(marker["existing"]) if marker is not None else _decide()
    return _existing[where]


def migrate(config, writer=None) -> dict:
    """Einmal beim Start des Home, vor der Verdrahtung (E104). Bestand mit leerem library.games_disk:
    Spiele-Ort /run/media/<nutzer>/Spiele und library.roms = False, beides über config.set. Danach nie
    wieder (Marke <config>/portable.json, 0600). writer (system.WriteBehind): die Marke schreibt dann
    dessen Thread, kein fsync in der Hauptschleife. Liefert {existing, first, changed}; wirft nie."""
    out = {"existing": False, "first": False, "changed": []}
    try:
        paths.use_config(config)            # paths.games_disk liest ab jetzt die laufenden Einstellungen
        marker = read_marker()
        if marker is not None:
            out["existing"] = _existing[_where()] = bool(marker["existing"])
            return out
        existing_now = _decide()
        out.update(existing=existing_now, first=True)
        if existing_now and not str(config.get("library.games_disk", "") or "").strip():
            config.set("library.games_disk", paths.legacy_games_disk())
            config.set("library.roms", False)
            out["changed"] = ["library.games_disk", "library.roms"]
        _existing[_where()] = existing_now
        if getattr(config, "read_failed", False):
            # settings.json war nicht lesbar: nichts ist gespeichert, also beim nächsten Start noch einmal
            _log.warning("Bestand: Einstellungen nicht lesbar, Übernahme beim nächsten Start erneut")
            return out
        data = {"format": FORMAT, "existing": existing_now, "at": round(time.time(), 3),
                "files": snapshot(), "changed": out["changed"]}
        text = jsontext.dumps(data, indent=1, sort_keys=True) + "\n"
        if writer is not None:
            writer.write(marker_file(), text, 0o600)
        else:
            system.write_atomic(marker_file(), text, 0o600)
        if existing_now:
            _log.info("Bestand erkannt (%s): Spiele-Ort %s%s", ", ".join(data["files"]) or "-",
                      paths.games_disk(), ", ROM-Ordner ohne SRM aus" if out["changed"] else "")
        else:
            _log.info("Neue Installation: Spiele-Ort %s", paths.games_disk())
    except Exception:                       # noqa: BLE001 - das Home startet trotzdem
        _log.exception("Bestand übernehmen")
    return out


def _reset_for_tests() -> None:
    """Nur für Tests: vergessen, was dieser Prozess schon entschieden hat."""
    _snapshot.clear()
    _existing.clear()
    paths.use_config(None)
