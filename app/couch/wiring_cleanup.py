"""Platz schaffen (E110, Teil P13): Verdrahtung, eingehängt über wiring.PARTS.

RPC storage.cleanupScan {} -> cleanup.scan() im Arbeits-Thread (Bauplan 4.3, 9.5): Gruppen, Größen, Aktionen.
wiring.cleanup ist der Dienst; die Seite Einstellungen › Speicher zeigt die Zeile „Platz schaffen“ nur, wenn es
ihn gibt (deps.extra["late"]("cleanup"), settings/storage.py), und öffnet damit den Bildschirm cleanup.

Gelöscht wird hier nichts und es gibt keine neue Löschstelle (SAFETY 13): Die Seite ruft danach die
vorhandenen Wege install.purge, files.op empty und files.op trash (Papierkorb, wiederherstellbar), jeweils
nach einer Rückfrage mit Vorgabe Abbrechen. Läuft schon ein Suchlauf, bekommt ein zweiter Aufruf dessen Ergebnis.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future
from typing import Any

from . import cleanup, log, paths

_log = log.get("wiring.cleanup")


class CleanupService:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self._lock = threading.Lock()           # für _running
        self._running: Any = None               # Future des laufenden Suchlaufs
        self._trash_lock = threading.Lock()     # eigenes Schloss: der Suchlauf selbst fragt danach
        self._trash: Future | None = None       # laufende Liste des Konsolen-Papierkorbs (mit Größen)

    # ------------------------------------------------------------ was der Suchlauf liest
    def _prefix_root(self) -> str:
        ctx = getattr(self.wiring, "launch", None)
        fn = getattr(ctx, "prefix_root_dir", None)
        try:
            return str(fn()) if callable(fn) else ""
        except Exception:  # noqa: BLE001
            _log.exception("Prefix-Ordner")
            return ""

    def _games_root(self) -> str:
        try:
            from . import wiring_install
            return wiring_install.games_root(self.wiring.app)
        except Exception:  # noqa: BLE001
            _log.exception("Spiele-Ordner des Installers")
            return ""

    def _installer(self) -> Any:
        return getattr(getattr(self.wiring, "install_hub", None), "installer", None)

    def _console_trash(self):
        """installer.trash_list (misst die Größen einmal, dann aus seinem Gedächtnis). Läuft die Messung eines
        früheren Suchlaufs noch, wartet dieser auf dieselbe, statt den Baum ein zweites Mal zu durchlaufen."""
        installer = self._installer()
        if installer is None:
            return None

        def listing() -> dict:
            with self._trash_lock:
                running = self._trash
                mine = running is None or running.done()
                if mine:
                    running = self._trash = Future()
            if mine:
                try:
                    running.set_result(installer.trash_list())
                except Exception as e:  # noqa: BLE001
                    running.set_exception(e)
            return running.result()
        return listing

    def _console_trash_quick(self):
        """Nur die Einträge, ohne Größen (installer/trash.list_trash liest die kleinen Begleitdateien)."""
        if self._installer() is None:
            return None
        root = self._games_root()

        def quick() -> dict:
            from .installer import trash
            return {"items": trash.list_trash(root)}
        return quick

    @staticmethod
    def _system_trash() -> list:
        from . import files, xdgtrash
        return xdgtrash.list_items(xdgtrash.trash_dirs(files.roots()), sizes=True)

    @staticmethod
    def _allowed(path: str) -> bool:
        from . import files
        return files.allowed(path)

    def context(self) -> cleanup.Context:
        app = self.wiring.app
        library = getattr(app, "library", None)
        src = getattr(self.wiring, "sources", None)
        steam_root = getattr(src, "steam_root", None)
        from .launch.prefix import prefix_name
        # Spiele-Ort und persönlicher Ordner wie die Speicher-Seite (Quellen-Kontext; im Prüfmodus die Prüf-Welt)
        games = getattr(src, "games_disk", None) or paths.games_disk()
        home = getattr(src, "home", None) or paths.user_place("")
        return cleanup.Context(
            state_dir=paths.state_dir(), games_root=self._games_root(), downloads=paths.user_dir("DOWNLOAD"),
            prefix_root=self._prefix_root(), steam_roots=[steam_root] if steam_root else [],
            entries=library.entries if library is not None else None, prefix_name=prefix_name,
            console_trash=self._console_trash(), console_trash_quick=self._console_trash_quick(),
            system_trash=self._system_trash, allowed=self._allowed,
            # „Frei: 910 GB auf „Spiele“ · 230 GB im persönlichen Ordner“ (im Prüfmodus der Käfig)
            volumes=[("games", os.path.basename(games.rstrip("/")) or games, games), ("home", "", os.path.realpath(home))])

    # ------------------------------------------------------------ RPC
    def scan(self, params: dict | None = None) -> Any:
        """storage.cleanupScan: Future aus dem Arbeits-Thread (höchstens rund 20 s)."""
        with self._lock:
            if self._running is not None and not self._running.done():
                return self._running
            self._running = self.wiring._bg(self._work)
            return self._running

    def _work(self) -> dict:
        return cleanup.scan(self.context())


def extend(wiring: Any) -> None:
    service = CleanupService(wiring)
    wiring.cleanup = service
    wiring.app.rpc.register("storage.cleanupScan", service.scan)
