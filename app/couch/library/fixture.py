"""Prüf-Quelle "fixture": Einträge aus <COUCH_FIXTURES>/library.json statt aus Steam, Lutris
und Co. Nur aktiv, wenn COUCH_FIXTURES gesetzt ist UND das Home im Prüfmodus läuft (Tests; es
wird nie ein echtes Spiel gelesen). Im echten Betrieb liefert sie nichts, auch wenn die Variable
versehentlich gesetzt ist. Fehlt die Datei (Fixtures-Ordner nur mit Attrappen-Baum für die
echten Quellen), gibt es still keine Einträge.

Form der Datei:
  {"entries": [
     {"id": "fixture:rot", "title": "Rotes Spiel", "kind": "game", "badge": "PROTON",
      "art": {"cover": "rot.png"},                      relative Pfade zählen ab COUCH_FIXTURES
      "launch": {"argv": ["{fake_app}", "--title", "Rot"], "env": {}, "kind": "game",
                 "freeze": true, "mouse": false, "error": null, "handover": null}},
     …]}
Alle übrigen Felder von Entry (source, tags, available, installed, options, detail …) sind
erlaubt. "launch" landet in entry.detail["launch"] und wird von launch/fixture.py ausgewertet.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from .. import log, paths
from .index import _ENTRY_FIELDS, register_source
from .model import Entry

_log = log.get("library.fixture")


def art_url(path: str) -> str:
    """couch://art/<url-kodierter absoluter Pfad>"""
    return "couch://art" + urllib.parse.quote(os.path.abspath(path))


def load(directory: str | None = None) -> list[Entry]:
    directory = directory or paths.fixtures_dir()
    if not directory or not paths.test_mode():
        return []
    path = os.path.join(directory, "library.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []                               # Ordner ohne Prüf-Bibliothek: kein Fehler
    except (OSError, ValueError) as e:
        _log.warning("Prüf-Bibliothek %s nicht lesbar: %s", path, e)
        return []
    items = data.get("entries", []) if isinstance(data, dict) else []
    out: list[Entry] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) \
                or not isinstance(raw.get("title"), str):
            _log.warning("Prüf-Bibliothek: Eintrag ohne id/title übergangen")
            continue
        fields = {k: v for k, v in raw.items() if k in _ENTRY_FIELDS}
        fields.setdefault("source", "fixture")
        entry = Entry(**fields)
        art = {}
        for key, value in (entry.art or {}).items():
            if isinstance(value, str) and value and "://" not in value:
                value = art_url(value if os.path.isabs(value) else os.path.join(directory, value))
            art[key] = value
        entry.art = art
        if isinstance(raw.get("launch"), dict):
            entry.detail = {**entry.detail, "launch": raw["launch"]}
        out.append(entry)
    return out


def setup() -> bool:
    """Meldet die Quelle an, wenn COUCH_FIXTURES gesetzt ist (nur im Prüfmodus)."""
    if not paths.fixtures_dir() or not paths.test_mode():
        return False
    register_source("fixture", load)
    _log.info("Prüf-Quelle aktiv: %s", paths.fixtures_dir())
    return True
