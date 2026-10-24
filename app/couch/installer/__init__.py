"""Archiv-Installer (Bauplan 4.10, E16, E17).

Installiert Spiele aus Archiven, die schon auf dem Rechner liegen (zip, 7z, rar, auch mehrteilig
und mit Kennwort). Kein Herunterladen, keine Shop-Anbindung.

Module:
  browse.py   Dateiauswahl unter festen Wurzeln (nur lesend)
  probe.py    Archiv ansehen und einen Plan liefern (nur lesend)
  extract.py  Entpacker: Zwischenordner, bubblewrap, Platzwächter, Marker, rename an den Zielort
  jobs.py     JobManager: Warteschlange, Unterprozess, Ereignisse, Aufräumen beim Start
  trash.py    Papierkorb und das einzige Löschen des Produkts
  service.py  Installer(deps): Fassade für die RPC-Methoden

Geschrieben wird ausschließlich unterhalb des Spiele-Ordners (Einstellung "install.games_root")
und in <state_dir> (Auftragsliste, Protokoll). Dieses Modul hält nur gemeinsame Kleinigkeiten und
importiert keine Untermodule (kein Importkreis).
"""
from __future__ import annotations

import os
import pwd
import threading
from typing import Any, Callable

CFG_GAMES_ROOT = "install.games_root"     # Zielwurzel für PC-Spiele (Version 1: nur die Spiele-Platte)
CFG_RESERVE_GIB = "install.reserve_gib"   # so viel bleibt auf der Spiele-Platte immer frei
CFG_TRASH_DAYS = "install.trash_days"     # Papierkorb-Einträge älter als das werden beim Start entfernt

DEFAULT_RESERVE_GIB = 5.0
DEFAULT_TRASH_DAYS = 14
GIB = 1 << 30
MAX_THREADS = 8                           # E30: 7z lässt einem laufenden Spiel die übrigen Kerne


def thread_limit() -> int:
    """Zahl der Threads für 7z (-mmt): höchstens MAX_THREADS, auf kleinen Rechnern nicht mehr als Kerne."""
    return max(1, min(MAX_THREADS, os.cpu_count() or MAX_THREADS))


class InstallError(Exception):
    """Fehler mit deutschem Satz für die Oberfläche (message) und festem Kürzel (code)."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message, **self.extra}


def user_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER") or "user"


def default_games_root() -> str:
    """E104: der Spiele-Ort (paths.games_disk: library.games_disk, im Bestand /run/media/<nutzer>/Spiele,
    sonst ~/Games) statt eines festen Pfads."""
    from .. import paths
    return paths.games_disk()


def games_root(config) -> str:
    value = config.get(CFG_GAMES_ROOT, None) if config is not None else None
    return value if isinstance(value, str) and value else default_games_root()


def reserve_bytes(config) -> int:
    value = config.get(CFG_RESERVE_GIB, DEFAULT_RESERVE_GIB) if config is not None else DEFAULT_RESERVE_GIB
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = DEFAULT_RESERVE_GIB          # Unsinn in den Einstellungen schaltet den Wächter nicht ab
    return int(value * GIB)


def trash_days(config) -> int:
    value = config.get(CFG_TRASH_DAYS, DEFAULT_TRASH_DAYS) if config is not None else DEFAULT_TRASH_DAYS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = DEFAULT_TRASH_DAYS
    return int(value)                        # < 1 heißt: nie von selbst endgültig löschen


def test_mode_violation(path: str) -> str | None:
    """Im Prüfmodus (COUCH_TEST_ROOT gesetzt) darf nur unterhalb des Testordners geschrieben werden.

    Liefert einen deutschen Grund, wenn path außerhalb liegt, sonst None. So kann ein Test mit
    Vorgabe-Einstellungen nie auf die echte Spiele-Platte des Nutzers schreiben."""
    root = os.environ.get("COUCH_TEST_ROOT")
    if not root:
        return None
    allowed = os.path.realpath(root)
    real = os.path.realpath(path)
    if real == allowed or real.startswith(allowed + os.sep):
        return None
    return "Prüfmodus: %s liegt außerhalb des Testordners und wird nicht beschrieben." % real


def human_size(n: float) -> str:
    """Größe für die Oberfläche: „1,7 TB“, „12 GB“ (deutsches Komma, 1024er-Schritte)."""
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % n
            text = ("%.0f" if n >= 100 else "%.1f") % n
            return text.replace(".", ",") + " " + unit
        n /= 1024.0
    return ""


class DictConfig:
    """Einfache Einstellungen nach dem Protokoll Config (interfaces.py) für eigenständige Läufe und Tests."""

    def __init__(self, values: dict | None = None):
        self._values = dict(values or {})
        self._watchers: list[tuple[str, Callable[[str, Any], None]]] = []

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value
        for prefix, fn in list(self._watchers):
            if key.startswith(prefix):
                fn(key, value)

    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        self._watchers.append((prefix, fn))


class ThreadScheduler:
    """Planer ohne GLib: Hintergrundarbeit in Threads, Rückrufe direkt im aufrufenden Thread.

    Für Tests und eigenständige Läufe. Rückrufe (emit, add_local) kommen dann aus dem
    Arbeits-Thread des Auftrags."""

    def call_soon(self, fn: Callable, *args: Any) -> None:
        fn(*args)

    def run_in_thread(self, fn: Callable, *args: Any) -> None:
        threading.Thread(target=fn, args=args, name="couch-install", daemon=True).start()


class GLibScheduler:
    """Planer für den Home-Prozess: Hintergrundarbeit in Threads, Rückrufe in der GLib-Hauptschleife."""

    def __init__(self):
        from gi.repository import GLib      # erst hier, damit Tests ohne gi laufen
        self._glib = GLib

    def call_soon(self, fn: Callable, *args: Any) -> None:
        def once():
            fn(*args)
            return False                    # idle_add: nicht wiederholen
        self._glib.idle_add(once, priority=self._glib.PRIORITY_DEFAULT)   # wie main.on_main

    def run_in_thread(self, fn: Callable, *args: Any) -> None:
        threading.Thread(target=fn, args=args, name="couch-install", daemon=True).start()


def __getattr__(name: str):
    # Installer erst bei Bedarf laden: service importiert alle Untermodule
    if name == "Installer":
        from .service import Installer
        return Installer
    raise AttributeError(name)
