"""Mediengalerie (E120) und eigener Hintergrund des Home (E121): Verdrahtung, eingehängt über wiring.PARTS.

  gallery.list {sources?: ["screenshots", "wallpapers"], game?, all?, limit?}
                                                           -> {ok, items, truncated, folders, current, game?}
               game: nur die Bildschirmfotos dieses Spiels (Titel des Eintrags; mit all: true bleiben alle, die
               Antwort nennt nur den Titel wie im Dateinamen); limit: die neuesten n (1..500)
  gallery.thumbs {paths: [...]}                            -> {thumbs: {pfad: couch://art/… | null}}
  gallery.delete {path}                                    -> {ok, message}   (Papierkorb, nach Rückfrage der Seite)
  gallery.setBackground {path}                             -> {ok, message}   (setzt „Eigenes Bild“)
  gallery.backdrop {after?}                                -> {mode, interval, url, key, count?}
  sys.state.background {mode, interval, slides, key}       bei jeder Änderung von ui.background*
  Ereignis gallery.changed {reason: "capture" | "delete"}  an die Home-Seite: die Galerie lädt neu

Alles mit PIL (Vorschaubilder, Hintergrund-Fassungen) läuft der Reihe nach in einem eigenen Arbeits-Thread
(Lane „couch-gallery“): Eine volle Galerie belegt so höchstens einen Thread und nie den Pool, aus dem
Einstellungen und Ein/Aus antworten (E99). Listen und Löschen laufen im Pool (wiring._bg).

Der Zwischenspeicher <cache>/gallery wird vor dem ersten Fenster als Wurzel für couch://art angemeldet
(paths.add_art_root, ui/host.py liest die Liste einmal).
"""
from __future__ import annotations

from typing import Any

from . import gallery, log, paths
from .rpc import RpcError

_log = log.get("wiring.gallery")


def extend(wiring: Any) -> None:
    app = wiring.app
    rpc = app.rpc
    config = wiring.config
    cache = gallery.Cache()
    if cache.ensure() and not paths.add_art_root(cache.folder):
        _log.warning("Mediengalerie: %s lässt sich nicht als Bilder-Wurzel anmelden", cache.folder)
    service = gallery.Gallery(config, cache)
    wiring.gallery = service
    from .wiring_settings import Lane
    lane = (getattr(wiring, "lane_factory", None) or Lane)("couch-gallery")

    def sources(params: dict) -> Any:
        raw = (params or {}).get("sources")
        if raw is not None and not (isinstance(raw, list) and all(isinstance(s, str) for s in raw)):
            raise RpcError("sources ist eine Liste aus „screenshots“ und „wallpapers“.", "BadValue")
        return raw

    def list_(params: dict) -> Any:
        params = params or {}
        wanted = sources(params)
        game, limit = params.get("game"), params.get("limit")
        if game is not None and not isinstance(game, str):
            raise RpcError("game ist der Titel eines Spiels.", "BadValue")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500):
            raise RpcError("limit ist eine Zahl von 1 bis 500.", "BadValue")
        keep_all = params.get("all") is True
        return wiring._bg(lambda: service.list(wanted, game, limit, keep_all))

    def thumbs(params: dict) -> Any:
        return lane.submit(service.thumbs, (params or {}).get("paths"))

    def delete(params: dict) -> Any:
        path = (params or {}).get("path")

        def run() -> dict:
            out = service.delete(path)
            if out.get("ok"):
                rpc.emit("gallery.changed", {"reason": "delete"}, "home")
            return out
        return wiring._bg(run)

    def set_background(params: dict) -> Any:
        return wiring._bg(service.set_background, (params or {}).get("path"))

    def backdrop(params: dict) -> Any:
        return lane.submit(service.backdrop, (params or {}).get("after") or "")

    for name, fn in (("gallery.list", list_), ("gallery.thumbs", thumbs), ("gallery.delete", delete),
                     ("gallery.setBackground", set_background), ("gallery.backdrop", backdrop)):
        rpc.register(name, fn)

    # Hintergrund für die Seite: nur die Einstellungen, ohne Dateizugriff (die Seite fragt das Bild nach)
    def push() -> None:
        wiring.main.post(lambda: app.state.update(background=gallery.ui_background(config)))
    app.state.update(background=gallery.ui_background(config))
    config.on_change("ui.background", lambda _key, _value: push())

    # Neues Bildschirmfoto: eine offene Galerie lädt neu. capture.Capture entsteht erst in
    # wiring_settings.started(), also nach dem Einhängen der Teile
    def attach() -> None:
        cap = getattr(wiring, "capture", None)
        if cap is not None:
            cap.on_saved = lambda path: rpc.emit("gallery.changed", {"reason": "capture"}, "home")
    wiring.after_ready.append(attach)
    wiring.shutdown_hooks.append(lane.close)
