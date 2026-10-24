"""Datei-Explorer am Kern (Bauplan E62): RPC files.* und die Ereignisse files.progress/files.done.
Dazu der Text-Editor (E99): files.textOpen/files.textSave über textedit.py.

Die Platte fasst files.py an; hier nur Anmelden und der Weg in andere Teile (Bibliothek, Kodi).
Alles, was liest oder schreibt, läuft im Arbeits-Thread (wiring._bg) bzw. in den Aufträgen von
files.FileOps; an die Seite geht es über die Hauptschleife.
"""
from __future__ import annotations

import os
import time
import urllib.parse
import uuid
from typing import Any

from . import files, log, textedit, xdgtrash

_log = log.get("wiring.files")


def extend(wiring: Any) -> None:
    app = wiring.app
    rpc = app.rpc
    ops = files.FileOps(emit=lambda event, payload: rpc.emit(event, payload, "home"),
                        post=wiring.main.post, notify=getattr(app, "notify", None))
    wiring.file_ops = ops

    def bg(fn):
        return lambda params: wiring._bg(fn, params or {})

    def list_(params: dict) -> dict:
        return files.listing(params.get("path") or None, hidden=bool(params.get("hidden")))

    def text(params: dict) -> dict:
        return files.read_text(str(params.get("path") or ""))

    def image(params: dict) -> dict:
        path = str(params.get("path") or "")
        if files.kind_of(path) != "image" or not files.allowed(path):
            return {"ok": False, "message": files.TEXT_OUTSIDE}
        return {"ok": True, "url": "couch://files" + urllib.parse.quote(os.path.abspath(path))}

    def op(params: dict) -> dict:
        return ops.start(str(params.get("op") or ""), params.get("paths"), str(params.get("dest") or ""),
                         str(params.get("name") or ""))

    def cancel(params: dict) -> dict:
        return ops.cancel(str(params.get("jobId") or ""))

    def trash_list(params: dict) -> dict:
        # {sizes: false} liefert sofort nur die Einträge (E99): Die Seite zeigt sie gleich und
        # holt die Größen danach; complete sagt, ob alle Größen in die Frist passten
        sizes = params.get("sizes") is not False
        items = xdgtrash.list_items(xdgtrash.trash_dirs(files.roots()), sizes=sizes)
        return {"ok": True, "items": items, "size": sum(i["size"] or 0 for i in items),
                "complete": sizes and all(i["size"] is not None for i in items)}

    def add(params: dict) -> dict:
        return add_to_library(app, str(params.get("path") or ""))

    def run(params: dict) -> dict:
        return entry_for_file(app, str(params.get("path") or ""))

    def play(params: dict) -> dict:
        return play_with_kodi(app, str(params.get("path") or ""))

    # Text-Editor (E99, textedit.py): öffnen und speichern nur an den Orten des Explorers. Alte
    # Sicherungen räumt textedit.default_trash weg (Papierkorb; im Prüfmodus der im Käfig, deshalb
    # hier kein trash=files.gio_trash: Gio legte auf /tmp einen eigenen Papierkorb an)
    def text_open(params: dict) -> dict:
        return textedit.open_text(params.get("path"))

    def text_save(params: dict) -> dict:
        return textedit.save_text(params.get("path"), params.get("text"), params.get("expect"))

    for name, fn in (("files.list", bg(list_)), ("files.text", bg(text)), ("files.image", image),
                     ("files.op", bg(op)), ("files.cancel", cancel), ("files.add", bg(add)), ("files.run", bg(run)),
                     ("files.play", play), ("files.trashList", bg(trash_list)),
                     ("files.textOpen", bg(text_open)), ("files.textSave", bg(text_save))):
        rpc.register(name, fn)


def add_to_library(app: Any, path: str) -> dict:
    """Startdatei (.exe, .sh …) als eigener Eintrag der Bibliothek (wie bei P.T. und Superliminal
    von Hand am 2026-10-18). Windows-Dateien laufen über Proton mit eigenem Prefix."""
    from . import wiring_library
    kind = files.kind_of(path)
    if kind not in ("windows", "linux") or not files.allowed(path) or not os.path.isfile(path):
        return {"ok": False, "message": "Nur Startdateien (.exe, .sh …) lassen sich hinzufügen."}
    folder = os.path.dirname(path)
    places = [os.path.realpath(r["path"]) for r in files.roots()]
    title = os.path.splitext(os.path.basename(path))[0] if os.path.realpath(folder) in places \
        else os.path.basename(folder)
    record = {"id": "local:" + uuid.uuid4().hex, "title": title, "platform": kind, "install_dir": folder,
              "exe": os.path.basename(path), "args": "", "archive": "aus dem Datei-Explorer", "marker": ""}
    try:
        entry_id = wiring_library.add_local_record(app, record)
    except Exception as e:  # noqa: BLE001
        _log.exception("Zur Bibliothek hinzufügen")
        return {"ok": False, "message": "Das hat nicht geklappt: %s" % e}
    if not entry_id:
        return {"ok": False, "message": "Das hat nicht geklappt."}
    return {"ok": True, "id": entry_id, "message": "„%s“ steht jetzt in der Bibliothek." % title}


def entry_for_file(app: Any, path: str) -> dict:
    """„Starten“ einer Startdatei aus den Downloads oder dem Datei-Explorer (E93): der Eintrag mit
    genau dieser Startdatei, sonst ein neuer wie bei „Zur Bibliothek hinzufügen“ (eigener Prefix,
    beim nächsten Mal auch in der Bibliothek). Gestartet wird über den gewohnten Weg der Seite
    (app.launch). Blockierend (Arbeits-Thread). → {"ok", "id", "title", "new"} oder {"ok": False, "message"}."""
    if files.kind_of(path) not in ("windows", "linux") or not files.allowed(path) or not os.path.isfile(path):
        return {"ok": False, "message": "Nur Startdateien (.exe, .sh …) lassen sich starten."}
    real = os.path.realpath(path)
    for entry in app.library.entries():
        if entry.source != "local" or not isinstance(entry.detail, dict):
            continue
        exe = entry.detail.get("exe_path") or os.path.join(str(entry.detail.get("install_dir") or ""),
                                                           str(entry.detail.get("exe") or ""))
        if isinstance(exe, str) and os.path.isabs(exe) and os.path.realpath(exe) == real:
            return {"ok": True, "id": entry.id, "title": entry.title, "new": False}
    added = add_to_library(app, path)
    if not added.get("ok"):
        return added
    entry = app.library.get(added["id"])
    return {"ok": True, "id": added["id"], "title": entry.title if entry else os.path.basename(path), "new": True}


def play_with_kodi(app: Any, path: str) -> dict:
    """Video oder Musik mit Kodi (Flatpak, darf /run/media und die Medienordner lesen)."""
    from .launch import apps
    if files.kind_of(path) not in ("video", "audio") or not files.allowed(path):
        return {"ok": False, "message": "Das lässt sich nicht abspielen."}
    if app.procs.by_entry("app:kodi") is not None:
        return {"ok": False, "message": "Kodi läuft schon. Schließe es zuerst, dann startet es mit dieser Datei."}
    apps.PENDING_OPEN["app:kodi"] = (path, time.monotonic())
    try:
        return app.shell.launch("app:kodi")
    except Exception as e:  # noqa: BLE001
        apps.PENDING_OPEN.pop("app:kodi", None)
        return {"ok": False, "message": getattr(e, "message", str(e))}
