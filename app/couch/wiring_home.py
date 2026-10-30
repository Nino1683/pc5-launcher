"""Reihenfolge der Kacheln im Home (Bauplan E69, E140): „Kachel verschieben“ im Optionsmenü.

Wunsch 2026-10-19: „verschieben, also sortieren sollte dazukommen“; 2026-10-29 (E140): „Die karten sollen auch
verschiebbar sein zum sortieren“ – seitdem je Reihe eine eigene Liste. Gespeichert werden die Kennungen in
<config>/home-order.json ({"games": [...], "apps": [...], "media": [...]}); eine ältere Datei nur mit "games" bleibt
gültig. Leer heißt: wie vorgegeben (Spiele nach zuletzt gespielt, Apps und Medien in der Reihenfolge des Kerns).
Spiele, die nicht in der Liste stehen (neu installiert), zeigt das Home vorn, neue Apps und Medien hinten.
RPC home.order -> {games, apps, media, hidden}, home.setOrder {games?, apps?, media?} -> {ok}: Nur die genannten Reihen
werden ersetzt.

E141 (Wunsch 2026-10-30: „Ja“ auf „Soll man Apps ausblenden können? … Weg zurück“): Kacheln, die keine Einträge der
Bibliothek sind (die eigenen Bildschirme „Dateien“ und „Mediengalerie“, TILES), blendet „Ausblenden“ über diese Datei
aus ("hidden": [...], nur Kennungen aus TILES). Apps der Bibliothek blendet lib.hide aus (meta.hidden im Index).
RPC home.setHidden {id, hidden} -> {ok}; danach das Ereignis home.changed (das Home liest neu, auch wenn
Einstellungen › Apps › Ausgeblendete Apps die Kachel zurückholt).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from . import log, paths

_log = log.get("wiring.home")
MAX_IDS = 5000
ROWS = ("games", "apps", "media")
HIDDEN = "hidden"
# E141: Kacheln der Seite selbst (home.js FILES_TILE, GALLERY_TILE), die sich ausblenden lassen
TILES = ("sys:files", "sys:gallery")


def order_file() -> str:
    return os.path.join(paths.config_dir(), "home-order.json")


def _clean(ids: Any) -> list[str]:
    clean, seen = [], set()
    for i in ids if isinstance(ids, list) else []:
        if isinstance(i, str) and i and len(i) < 300 and i not in seen:
            seen.add(i)
            clean.append(i)
    return clean[:MAX_IDS]


def _hidden(ids: Any) -> list[str]:
    return [i for i in _clean(ids) if i in TILES]


def read_orders() -> dict[str, list[str]]:
    """{games, apps, media, hidden}: je Reihe die gespeicherte Liste (fehlt eine, ist sie leer), dazu die
    ausgeblendeten Kacheln der Seite (E141)."""
    try:
        with open(order_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else {}
    out = {row: _clean(data.get(row)) for row in ROWS}
    out[HIDDEN] = _hidden(data.get(HIDDEN))
    return out


def read_order() -> list[str]:
    """Nur die Spiele (wie vor E140)."""
    return read_orders()["games"]


# Lesen, Ändern, Schreiben in einem Zug: Es schreiben die Hauptschleife (home.setOrder, setHidden) und Arbeits-Threads
# (Übernehmen einer Sicherung, Apps wieder einblenden), alle über dieselbe .neu-Datei
_orders_lock = threading.RLock()


def write_orders(changes: dict) -> dict[str, list[str]]:
    """Ersetzt die Listen der genannten Reihen (und "hidden"), die anderen bleiben. Liefert den neuen Stand. OSError
    beim Schreiben."""
    with _orders_lock:
        orders = read_orders()
        for row in ROWS:
            if row in changes:
                orders[row] = _clean(changes[row])
        if HIDDEN in changes:
            orders[HIDDEN] = _hidden(changes[HIDDEN])
        os.makedirs(paths.config_dir(), exist_ok=True)
        tmp = order_file() + ".neu"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(orders, fh, indent=1)
        os.replace(tmp, order_file())
        return orders


def write_order(ids: list) -> list[str]:
    """Nur die Spiele (wie vor E140)."""
    return write_orders({"games": ids})["games"]


def set_tile_hidden(tile: str, hidden: bool) -> list[str]:
    """E141: eine Kachel aus TILES aus- bzw. wieder einblenden. Liefert die ausgeblendeten. ValueError, OSError."""
    if tile not in TILES:
        raise ValueError(tile)
    with _orders_lock:
        now = [i for i in read_orders()[HIDDEN] if i != tile]
        if hidden:
            now.append(tile)
        return write_orders({HIDDEN: now})[HIDDEN]


def extend(wiring: Any) -> None:
    rpc = wiring.app.rpc

    def changed() -> None:
        emit = getattr(rpc, "emit", None)
        if callable(emit):
            emit("home.changed", {})
    wiring.home_changed = changed               # E141: auch Einstellungen › Apps › Ausgeblendete Apps meldet es

    def get(params: dict) -> dict:
        return read_orders()

    def set_hidden(params: dict) -> dict:
        tile = str((params or {}).get("id") or "")
        if tile not in TILES:
            return {"ok": False, "message": "Diese Kachel lässt sich hier nicht ausblenden."}
        try:
            set_tile_hidden(tile, (params or {}).get("hidden", True) is not False)
        except OSError as e:
            return {"ok": False, "message": "Das ließ sich nicht speichern: %s" % e}
        _log.info("Kachel %s %s", tile, "ausgeblendet" if (params or {}).get("hidden", True) is not False else "eingeblendet")
        changed()
        return {"ok": True}

    def set_order(params: dict) -> dict:
        changes = {row: (params or {}).get(row) for row in ROWS if row in (params or {})}
        if not changes:
            return {"ok": False, "message": "Es fehlt die Reihe, deren Reihenfolge gespeichert werden soll."}
        try:
            orders = write_orders(changes)
        except OSError as e:
            return {"ok": False, "message": "Die Reihenfolge ließ sich nicht speichern: %s" % e}
        _log.info("Reihenfolge der Kacheln: %s", ", ".join("%s %d" % (row, len(orders[row])) for row in changes))
        return {"ok": True}

    rpc.register("home.order", get)
    rpc.register("home.setOrder", set_order)
    rpc.register("home.setHidden", set_hidden)
