"""Merkzettel für die Ton-Ausgabe von vor der Sitzung: <state>/sink-before.

Das Home macht beim Start den Fernseher zur Standard-Ausgabe (pactl set-default-sink), und
WirePlumber merkt sich das dauerhaft. Zurückstellen soll der Supervisor am Sitzungsende; beim
Ausschalten und Neustarten wird er aber hart beendet, bevor er dazu kommt. Deshalb:

- Vor jedem Umstellen notiert das Home hier, was vorher galt (Zeile 1, bleibt für die Sitzung
  stehen) und worauf umgestellt wird (Zeile 2).
- Vor Ausschalten, Neustart und Desktop-Wechsel stellt das Home selbst zurück (restore).
- Bleibt der Zettel doch liegen (Sitzung von außen beendet), wertet ihn der Supervisor beim
  nächsten Start aus (session/supervisor.py, sink_at_start) und löscht ihn.

Nur Dateien im eigenen Zustandsordner; der Befehl selbst geht durch run_write.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from . import log, paths, system

_log = log.get("sinknote")


def _usable(name: Any) -> bool:
    # Der Name landet als Argument bei pactl: nie etwas, das wie ein Schalter aussieht
    return (isinstance(name, str) and 0 < len(name) < 300 and name.isprintable()
            and not name.startswith("-") and "\n" not in name)


def read(path: str | None = None) -> tuple[str, str] | None:
    """(vorher, umgestellt auf) oder None, wenn es keinen brauchbaren Zettel gibt."""
    try:
        with open(path or paths.sink_before_file(), encoding="utf-8", errors="replace") as f:
            lines = [line.strip() for line in f.read().splitlines()]
    except OSError:
        return None
    before = lines[0] if lines else ""
    during = lines[1] if len(lines) > 1 else ""
    return (before, during) if _usable(before) else None


def record(old: Any, new: Any, path: str | None = None) -> None:
    """Vor einem Umstellen der Standard-Ausgabe aufrufen. old = was gerade gilt. Der erste
    Eintrag der Sitzung bleibt als "vorher" stehen; spätere ändern nur das Ziel."""
    if not _usable(new):
        return
    path = path or paths.sink_before_file()
    known = read(path)
    if known is not None:
        before = known[0]
    elif _usable(old) and old != new:
        before = old
    else:
        return                              # nichts umzustellen bzw. unbekannt, was vorher galt
    try:
        system.write_atomic(path, "%s\n%s\n" % (before, new))
    except OSError as e:
        _log.warning("Merkzettel der Tonausgabe nicht schreibbar: %s", e)


def restore(write: Callable[..., Any] = system.run_write, path: str | None = None) -> bool:
    """Die Ausgabe von vor der Sitzung wieder zum Standard machen und den Zettel löschen.
    Blockiert kurz (pactl): im Arbeits-Thread aufrufen. True, wenn zurückgestellt wurde."""
    path = path or paths.sink_before_file()
    known = read(path)
    if known is None:
        return False
    before, during = known
    ok = True
    if before != during:
        result = write(["pactl", "set-default-sink", before], timeout=5.0,
                       note="Ton-Ausgabe von vor der Sitzung")
        ok = getattr(result, "returncode", 1) == 0
    if ok:
        try:
            os.unlink(path)                 # genau diese eine Datei
        except OSError:
            pass
    else:
        _log.warning("Tonausgang ließ sich nicht auf %s zurückstellen", before)
    return ok
