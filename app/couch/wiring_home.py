"""Reihenfolge der Spiele-Kacheln im Home (Bauplan E69): „Kachel verschieben“ im Optionsmenü.

Wunsch 2026-10-19: „verschieben, also sortieren sollte dazukommen“. Gespeichert wird die Liste der
Kennungen in <config>/home-order.json ({"games": [...]}). Leer heißt: wie bisher nach zuletzt
gespielt. Spiele, die nicht in der Liste stehen (neu installiert), zeigt das Home vorn.
RPC home.order -> {games: [...]}, home.setOrder {games: [...]} -> {ok}.
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import log, paths

_log = log.get("wiring.home")
MAX_IDS = 5000


def order_file() -> str:
    return os.path.join(paths.config_dir(), "home-order.json")


def read_order() -> list[str]:
    try:
        with open(order_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        games = data.get("games") if isinstance(data, dict) else None
        return [str(i) for i in games if isinstance(i, str)][:MAX_IDS] if isinstance(games, list) else []
    except (OSError, ValueError):
        return []


def write_order(ids: list) -> list[str]:
    clean, seen = [], set()
    for i in ids if isinstance(ids, list) else []:
        if isinstance(i, str) and i and len(i) < 300 and i not in seen:
            seen.add(i)
            clean.append(i)
    clean = clean[:MAX_IDS]
    os.makedirs(paths.config_dir(), exist_ok=True)
    tmp = order_file() + ".neu"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"games": clean}, fh, indent=1)
    os.replace(tmp, order_file())
    return clean


def extend(wiring: Any) -> None:
    rpc = wiring.app.rpc

    def get(params: dict) -> dict:
        return {"games": read_order()}

    def set_order(params: dict) -> dict:
        try:
            ids = write_order((params or {}).get("games"))
        except OSError as e:
            return {"ok": False, "message": "Die Reihenfolge ließ sich nicht speichern: %s" % e}
        _log.info("Reihenfolge der Spiele-Kacheln: %d Einträge", len(ids))
        return {"ok": True}

    rpc.register("home.order", get)
    rpc.register("home.setOrder", set_order)
