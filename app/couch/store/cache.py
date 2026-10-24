"""Zwischenspeicher der Shops (E99): eine Fassung statt fünf fast gleicher Kopien (Steam, GOG, Epic,
Battle.net, Ubisoft, Rockstar).

Antworten liegen als JSON in <cache>/store/<shop>/<schlüssel>.json. Jünger als ttl: aus der Datei.
Sonst neu holen; scheitert das, gilt ohne Netz der letzte Stand, sonst StoreError mit einem deutschen
Satz. Blockierend (Platte, Netz), gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import json
import os
import re
import time


class StoreError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


def path_for(cache_dir: str, key: str) -> str:
    return os.path.join(cache_dir, re.sub(r"[^\w.-]", "_", key)[:150] + ".json")


def _read(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write(cache_dir: str, path: str, data) -> None:
    """Atomar ablegen; ein Fehler der Platte ist hier kein Fehler (dann eben ohne Zwischenspeicher)."""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + ".neu"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def cached(cache_dir: str, key: str, fetch, ttl: float, *, clock=time.time, force: bool = False, check=None,
           unreachable: str = "Der Shop ist gerade nicht erreichbar.", on_error=None, keep: tuple = ()):
    """fetch() liefert die Antwort. check(data) darf werfen: eine Fehlerantwort gilt wie kein Netz.
    on_error(e) wird bei jedem Fehler gerufen (etwa um eine HTTP-Antwort zu schließen). keep: Ausnahmen,
    die unverändert durchgehen, ohne Rückfall auf den letzten Stand (schon ein Satz für die Oberfläche)."""
    path = path_for(cache_dir, key)
    try:
        age = clock() - os.path.getmtime(path)
    except OSError:
        age = None
    if age is not None and age < ttl and not force:
        try:
            return _read(path)
        except (OSError, ValueError):
            pass
    try:
        data = fetch()
        if check is not None:
            check(data)
    except keep:
        raise
    except Exception as e:      # urllib wirft je nach Lage sehr verschiedene Ausnahmen
        if on_error is not None:
            on_error(e)
        if age is not None:
            try:
                return _read(path)      # ohne Netz: der letzte Stand
            except (OSError, ValueError):
                pass
        raise StoreError(unreachable) from e
    write(cache_dir, path, data)
    return data
