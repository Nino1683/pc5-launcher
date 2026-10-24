"""Einstellungen sichern und übernehmen (E106, Teil P11): Verdrahtung, eingehängt über wiring.PARTS.

RPC (Bauplan 4.3), alle im Arbeits-Thread (Platte, ZIP):
  backup.export  {dir? | path?} -> {ok, path, size, counts, message}   path: der Wahlmodus des Explorers
                 („Sicherung auf ein Laufwerk“, screens/explorer.js pick) schickt den Ordner als path
  backup.list    {dir?} -> {items: [{path, name, created, version, system, counts, size, safety, ok, drive}],
                 dir: Vorgabeordner}
  backup.inspect {path} -> {ok, manifest, apply: {settings, games, skipped, order, art, sessions, prefs},
                 parts: [{id, count}], message?}
  backup.apply   {path, parts} -> {ok, applied, skipped, safety: <pfad>, restart: true, message}
Fehler kommen als {ok: false, message} (geändert wurde dann nichts, außer die Antwort sagt es).
Ein Übernehmen zugleich; solange es läuft, meldet busy_checks den Grund. Den Neustart danach fragt die
Seite (power.do restart-session).
"""
from __future__ import annotations

import os
import threading
from typing import Any

from . import backup, log, paths, wiring_home
from .library.index import is_steam_app

_log = log.get("wiring.backup")
BUSY_TEXT = "Die Einstellungen werden gerade übernommen."
# Apps, deren Vorlieben übernommen werden (library/apps.py): läuft eine, gilt es beim nächsten Öffnen
PREF_APPS = (("app:browser", "Der Browser"), ("app:claude", "Das Terminal"))


def make_host(wiring: Any) -> backup.Host:
    app = wiring.app
    library = app.library
    main = wiring.main

    def known(entry_id: str) -> bool:
        return isinstance(entry_id, str) and (library.get(entry_id) is not None or is_steam_app(entry_id))

    def entry_ids() -> list:
        return [e.id for e in library.entries()]

    def art_dir() -> str:
        return os.path.join(paths.data_dir(), "art")

    def entry_art_dir(entry_id: str) -> str:
        art = getattr(wiring, "art", None)
        if art is not None and hasattr(art, "entry_dir"):
            return art.entry_dir(entry_id)
        from .library.art import safe_id
        return os.path.join(art_dir(), safe_id(entry_id))

    art = getattr(wiring, "art", None)
    set_user_art = getattr(art, "set_user_art", None) if art is not None else None

    def compat_dir() -> str:
        launch = getattr(wiring, "launch", None)
        fn = getattr(launch, "compat_tools_dir", None)
        if callable(fn):
            return fn()
        # ohne Starter: der Ort wie in launch/builders.py, im Prüfmodus im Käfig (paths.user_place)
        return paths.user_place(os.path.join(".local", "share", "Steam", "compatibilitytools.d"))

    def proton_exists(name: str) -> bool:
        if not isinstance(name, str) or not name or os.sep in name or name in (".", ".."):
            return False
        return os.path.isfile(os.path.join(compat_dir(), name, "proton"))

    def running_apps() -> list:
        procs = getattr(app, "procs", None)
        return [label for entry_id, label in PREF_APPS if procs is not None and procs.by_entry(entry_id)]

    def after_apply() -> None:
        main.post(library.rescan)

    return backup.Host(
        settings=lambda: wiring.config.all(),
        set_setting=lambda key, value: wiring.config.set(key, value),
        export_meta=lambda: main.call(library.export_meta),
        import_meta=lambda data: main.call(library.import_meta, data),
        export_sessions=lambda: main.call(library.export_sessions),
        import_sessions=lambda items: main.call(library.import_sessions, items),
        known=known, entry_ids=entry_ids,
        read_order=wiring_home.read_order, write_order=wiring_home.write_order,
        art_dir=art_dir, entry_art_dir=entry_art_dir, set_user_art=set_user_art,
        proton_exists=proton_exists, running_apps=running_apps, after_apply=after_apply,
        browser_prefs=os.path.join(paths.data_dir(), "browser", "prefs.json"),
        terminal_prefs=os.path.join(paths.data_dir(), "terminal", "prefs.json"),
    )


def extend(wiring: Any) -> None:
    app = wiring.app
    lock = threading.Lock()
    state = {"applying": False}

    def host() -> backup.Host:
        return make_host(wiring)

    def failed(e: Exception) -> dict:
        if isinstance(e, backup.BackupError):
            return {"ok": False, "message": e.message}
        _log.exception("Sichern/Übernehmen")
        return {"ok": False, "message": "Das hat nicht geklappt (%s)." % type(e).__name__}

    def export(params: dict) -> dict:
        folder = params.get("dir") or params.get("path") or None
        if folder is not None and not isinstance(folder, str):
            return {"ok": False, "message": "Das ist kein Ordner."}
        try:
            out = backup.export_zip(host(), folder)
        except Exception as e:
            return failed(e)
        return {"ok": True, **out, "message": "Gesichert: %s" % os.path.basename(out["path"])}

    def listing(params: dict) -> dict:
        extra = params.get("dir")
        try:
            items = backup.list_backups(extra if isinstance(extra, str) else None)
        except Exception as e:
            out = failed(e)
            return {"items": [], "dir": backup.default_dir(), "message": out["message"]}
        return {"items": items, "dir": backup.default_dir()}

    def inspect(params: dict) -> dict:
        try:
            b = backup.read_backup(params.get("path"))
            h = host()
            out = backup.preview(b, h)
        except Exception as e:
            return failed(e)
        m = b.manifest
        manifest = {k: m.get(k) for k in ("format", "product", "version", "created", "system", "counts")}
        return {"ok": True, "manifest": manifest, **out}

    def apply(params: dict) -> dict:
        with lock:
            if state["applying"]:
                return {"ok": False, "message": BUSY_TEXT}
            state["applying"] = True
        try:
            return backup.apply(params.get("path"), params.get("parts"), host())
        except Exception as e:
            return failed(e)
        finally:
            with lock:
                state["applying"] = False

    app.rpc.register("backup.export", lambda p: wiring._bg(export, p or {}))
    app.rpc.register("backup.list", lambda p: wiring._bg(listing, p or {}))
    app.rpc.register("backup.inspect", lambda p: wiring._bg(inspect, p or {}))
    app.rpc.register("backup.apply", lambda p: wiring._bg(apply, p or {}))
    wiring.busy_checks.append(lambda: BUSY_TEXT if state["applying"] else None)
