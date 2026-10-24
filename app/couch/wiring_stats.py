"""Spielzeit-Rückblick „Deine Woche“ (E108, Teil P9): Verdrahtung, eingehängt über wiring.PARTS.

RPC lib.stats {range: week|month|all, offset} -> LibraryIndex.stats() (Bauplan 9.3). Dazu je Spiel in
top, added und longest das EntryView als entry (die Seite öffnet damit die Spielseite) und in top
dessen Bilder als art (Kachelbild wie in der Reihe des Home). Schnell und nur aus dem Speicher: läuft
in der Hauptschleife wie lib.get.

Wochenmitteilung „Deine Woche: 12 Std. 30 Min. gespielt, am meisten „Alan Wake 2“.“ (notify.weekly,
Einstellungen › Mitteilungen): nach dem Bereit-Signal und alle 6 Stunden im Arbeits-Thread. Einmal je
Kalenderwoche (ab Montag 0 Uhr), nur wenn die Vorwoche Spielzeit hat; die Woche, für die entschieden
ist, steht in <state>/weekly-shown.json {"week": "2026-W44"}. Ist der Schalter aus, wird nichts
gemerkt: Wer ihn mitten in der Woche einschaltet, bekommt den Rückblick noch.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from . import jsontext, log, paths, system
from .library.describe import human_time

_log = log.get("wiring.stats")

WEEKLY_FILE = "weekly-shown.json"
WEEKLY_AGAIN_S = 6 * 3600
WEEKLY_TITLE = "Deine Woche"
CFG_WEEKLY = "notify.weekly"


def weekly_file() -> str:
    return os.path.join(paths.state_dir(), WEEKLY_FILE)


def week_key(now: float) -> str:
    """Kalenderwoche nach ISO 8601 ("2026-W44"); sie beginnt wie der Rückblick am Montag."""
    return time.strftime("%G-W%V", time.localtime(now))


def shown_week() -> str:
    try:
        with open(weekly_file(), encoding="utf-8") as f:
            data = json.load(f)
        week = data.get("week") if isinstance(data, dict) else None
        return week if isinstance(week, str) else ""
    except (OSError, ValueError):
        return ""


def weekly_text(stats: dict) -> str | None:
    """Text der Mitteilung zur Vorwoche oder None (nichts gespielt)."""
    total = int(stats.get("total") or 0)
    if total <= 0:
        return None
    named = [t for t in stats.get("top") or [] if isinstance(t, dict) and t.get("title")]
    if named:
        return "%s gespielt, am meisten „%s“." % (human_time(total), named[0]["title"])
    return "%s gespielt." % human_time(total)


def check_weekly(wiring: Any, now: float | None = None) -> bool:
    """Arbeits-Thread: Mitteilung zur Vorwoche, falls fällig. Liefert, ob eine kam."""
    now = time.time() if now is None else now
    app = wiring.app
    if not bool(wiring.config.get(CFG_WEEKLY, True)):
        return False
    key = week_key(now)
    if shown_week() == key:
        return False
    library = getattr(app, "library", None)
    if library is None:
        return False
    stats = wiring.main.call(library.stats, "week", 1, now)
    text = weekly_text(stats)
    try:
        system.write_atomic(weekly_file(), jsontext.dumps({"week": key}) + "\n")
    except OSError as e:
        # ohne Merkzettel lieber keine Mitteilung: Sie käme sonst alle 6 Stunden wieder
        _log.warning("Wochenrückblick: %s nicht speicherbar (%s)", WEEKLY_FILE, e)
        return False
    if text is None:
        return False
    app.notify.push(WEEKLY_TITLE, text, icon="clock")
    _log.info("Wochenrückblick %s gezeigt", key)
    return True


def decorate(library: Any, out: dict, procs: Any = None) -> dict:
    """Ergänzt die Antwort von stats() um entry (EntryView) und art (nur top)."""
    views: dict[str, dict | None] = {}

    def view(entry_id: str) -> dict | None:
        if entry_id not in views:
            entry = library.get(entry_id)
            running = bool(procs and procs.by_entry(entry_id)) if entry is not None else False
            views[entry_id] = entry.view(running) if entry is not None else None
        return views[entry_id]
    for item in out.get("top") or []:
        v = view(item["id"])
        if v is not None:
            item["entry"] = v
            item["art"] = v.get("art")
    for item in (out.get("added") or []) + ([out["longest"]] if out.get("longest") else []):
        v = view(item["id"])
        if v is not None:
            item["entry"] = v
    return out


def extend(wiring: Any) -> None:
    from .rpc import RpcError
    app = wiring.app
    library = app.library

    def lib_stats(params: dict) -> dict:
        """{range: week|month|all, offset: Zeiträume zurück (0 = laufender)} -> Rückblick (E108)."""
        params = params or {}
        rng = params.get("range", "week")
        offset = params.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset < 0:
            raise RpcError("offset ist die Zahl der Zeiträume zurück (0 = der laufende).", "BadValue")
        try:
            out = library.stats(rng if isinstance(rng, str) else "", int(offset))
        except ValueError as e:
            raise RpcError(str(e), "BadValue") from None
        return decorate(library, out, getattr(app, "procs", None))

    app.rpc.register("lib.stats", lib_stats)

    timer = {"id": None}

    def run() -> None:
        try:
            check_weekly(wiring)
        except Exception:
            _log.exception("Wochenrückblick")

    def tick() -> bool:
        wiring._bg(run)
        return True                                  # GLib: weiter alle 6 Stunden

    def arm() -> None:
        wiring._bg(run)
        from gi.repository import GLib
        timer["id"] = GLib.timeout_add_seconds(WEEKLY_AGAIN_S, tick)

    def disarm() -> None:
        if timer["id"] is not None:
            from gi.repository import GLib
            GLib.source_remove(timer["id"])
            timer["id"] = None

    wiring.after_ready.append(arm)
    wiring.shutdown_hooks.append(disarm)
