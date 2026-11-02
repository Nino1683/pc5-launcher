"""Verdrahtung, Teil „anyps5“ (E150, Wunsch 2026-11-02: „Baue AnyPS5 in den pc5-launcher ein.“).

Hier nur die Werkzeuge selbst, für Einstellungen › Spiele › AnyPS5 (settings/games.py über
deps.extra["late"]("anyps5")): AnyPS5 und der Entpacker pkg-to-anyps5 (E151). Ein Spiel umwandeln geht über den
Installer (installer/jobs.py, kind "anyps5"), der beide auch selbst lädt, wenn sie fehlen.

  wiring.anyps5   AnyPS5Part:
    status()      schnell: {installed: tag|None, latest: {tag, bytes}|None, unpacker: {installed, latest},
                  checked_at, error, checking, job: {name, tag, percent}|None, games, older}; stößt höchstens alle
                  CHECK_TTL eine Suche an
    install(what) neueste Fassung laden, what "anyps5" oder "unpacker" (eigener Thread, ein Download zur Zeit);
                  Fortschritt als settings.changed der Seite (höchstens 2/s), am Ende eine Mitteilung
    cancel()      laufenden Download abbrechen (die Teildatei bleibt fürs Fortsetzen)
    busy()        Satz für Ausschalten und Ruhemodus, solange geladen wird

Im Prüfmodus lädt nichts aus dem Netz (installer/anyps5.api_url).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import log
from .installer import anyps5

_log = log.get("wiring.anyps5")

PAGE = "games.anyps5"
CHECK_TTL = 6 * 3600.0           # so lange gilt eine Antwort von GitHub
RETRY_S = 300.0                  # nach einem Fehler frühestens wieder
EMIT_S = 0.5
NAMES = {"anyps5": "AnyPS5", "unpacker": anyps5.UNPACKER}


class AnyPS5Part:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.lock = threading.Lock()
        self.latest: dict | None = None
        self.latest_unpacker: dict | None = None
        self.checked_at = 0.0
        self.error = ""
        self.checking = False
        self.job: dict | None = None
        self._stop = threading.Event()
        self._emitted = 0.0

    # ------------------------------------------------------------ Stand
    def status(self) -> dict:
        tool = anyps5.installed()
        self._check_later()
        games = [e for e in self._entries() if isinstance(e.detail.get("anyps5"), dict)]
        current = tool["tag"] if tool else ""
        with self.lock:
            latest = {"tag": self.latest["tag"], "bytes": self.latest["bytes"]} if self.latest else None
            newest = self.latest_unpacker
            unpack = anyps5.unpacker()
            return {"installed": current or None, "latest": latest, "checked_at": self.checked_at, "error": self.error,
                    "unpacker": {"installed": unpack["tag"] if unpack else None,
                                 "latest": {"tag": newest["tag"], "bytes": newest["bytes"]} if newest else None},
                    "checking": self.checking, "job": dict(self.job) if self.job else None, "games": len(games),
                    "older": sum(1 for e in games if current and e.detail["anyps5"].get("tag") not in ("", current))}

    def _entries(self) -> list:
        library = getattr(self.app, "library", None)
        try:
            return list(library.entries()) if library is not None else []
        except Exception:
            _log.exception("Bibliothek nicht lesbar")
            return []

    def _check_later(self, force: bool = False) -> None:
        now = time.time()
        with self.lock:
            fresh = now - self.checked_at < (RETRY_S if self.error else CHECK_TTL)
            if self.checking or (fresh and not force):
                return
            self.checking = True
        threading.Thread(target=self._check, name="couch-anyps5", daemon=True).start()

    def _check(self) -> None:
        latest, newest, error = None, None, ""
        try:
            latest = anyps5.fetch_latest()
            newest = anyps5.fetch_unpacker()
        except anyps5.AnyError as e:
            error = e.message
        except Exception:
            _log.exception("Suche nach AnyPS5")
            error = "Die Suche nach AnyPS5 ist gescheitert."
        with self.lock:
            self.checking, self.checked_at, self.error = False, time.time(), error
            if latest is not None:
                self.latest = latest
            if newest is not None:
                self.latest_unpacker = newest
        self._changed(force=True)

    def refresh(self) -> None:
        self._check_later(force=True)

    # ------------------------------------------------------------ Laden
    def install(self, what: str = "anyps5") -> dict:
        name = NAMES[what]
        with self.lock:
            if self.job is not None:
                return {"ok": False, "message": "%s wird schon geladen." % self.job["name"]}
            release = self.latest if what == "anyps5" else self.latest_unpacker
            if release is None:
                return {"ok": False, "message": self.error or "Die neueste Fassung ist noch nicht bekannt."}
            self.job = {"name": name, "tag": release["tag"], "percent": 0}
            self._stop.clear()
        target = anyps5.install if what == "anyps5" else anyps5.install_unpacker
        threading.Thread(target=self._install, args=(release, name, target), name="couch-anyps5", daemon=True).start()
        self._changed(force=True)
        return {"ok": True, "message": "%s %s wird geladen." % (name, release["tag"])}

    def _install(self, release: dict, name: str, install) -> None:
        def progress(done: int, total: int) -> None:
            with self.lock:
                if self.job is not None:
                    self.job["percent"] = int(done * 100 / max(1, total))
            self._changed()

        try:
            tool = install(release, progress=progress, stop=self._stop.is_set)
        except anyps5.AnyError as e:
            with self.lock:
                self.job = None
            if e.code != "cancelled":
                self._notify("%s nicht geladen" % name, e.message, "warn")
            self._changed(force=True)
            return
        except Exception:
            _log.exception("%s laden", name)
            with self.lock:
                self.job = None
            self._notify("%s nicht geladen" % name, "Ein interner Fehler ist aufgetreten.", "warn")
            self._changed(force=True)
            return
        with self.lock:
            self.job = None
        self._notify("%s %s ist bereit" % (name, tool["tag"]),
                     "Spiele wandelst du im Installer um: Paket oder Spieleordner wählen, Installieren.", "download")
        self._changed(force=True)

    def cancel(self) -> dict:
        self._stop.set()
        return {"ok": True}

    def busy(self) -> str | None:
        with self.lock:
            return "%s wird gerade geladen." % self.job["name"] if self.job is not None else None

    # ------------------------------------------------------------ Anzeige
    def _changed(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._emitted < EMIT_S:
            return
        self._emitted = now
        rpc = getattr(self.app, "rpc", None)
        if rpc is not None:
            self.wiring.main.post(lambda: rpc.emit("settings.changed", {"page": PAGE}))

    def _notify(self, title: str, body: str, icon: str) -> None:
        try:
            self.app.notify.push(title, body, icon=icon)
        except Exception:
            _log.exception("Mitteilung „%s“", title)


def extend(wiring: Any) -> None:
    part = AnyPS5Part(wiring)
    wiring.anyps5 = part
    busy = getattr(wiring, "busy_checks", None)
    if isinstance(busy, list):
        busy.append(part.busy)
