"""Start- und Ruhemodus-Videos aus steamdeckrepo.com (E130): Verdrahtung, eingehängt über wiring.PARTS.

RPC (Netz und Platte im Arbeits-Thread, nie in der Hauptschleife):
    video.list {kind: "boot"|"suspend", sort: "popular"|"new", page}
                        -> {ok, kind, sort, page, more, posts: [{id, title, author, duration, downloads, likes, thumb,
                           preview, created, page}], chosen, stale?}; ohne Netz {ok: false, reason, message}
    video.choose {kind, id} -> lädt dieses Video (Adresse aus der eigenen Liste, bootvideos.Repo.download) und stellt
                           die Einstellung auf „Video“: {ok, title, message} bzw. {ok: false, message}. Während des Ladens
                           Ereignis video.progress {kind, id, done, total} an das Home (höchstens alle PROGRESS_S).
    video.played {}      -> die Seite meldet: Ruhemodus-Video zu Ende (oder übersprungen, oder ging nicht)
sys.state.videos     {style: "own"|"video"|"off", boot: {url, id, title, author, duration} | null, suspend: … | null,
                      suspendOn: bool}. url ist couch://art/… auf <data>/videos/<art>.webm (der Ordner ist als Wurzel
                      angemeldet; ui/host.py muss dafür .webm ausliefern, siehe ARCHITECTURE E130).
Ereignis video.suspend {url, maxMs} an die Seite, die den Ruhemodus gewählt hat (power.do aus dem Ein/Aus-Menü):
                      Sie spielt das Video und meldet video.played; der Kern wartet höchstens maxMs + SUSPEND_GRACE_MS.

Einstellungen (Home-Bildschirm, settings/home.py): ui.intro (an/aus wie bisher), ui.intro_style ("own" | "video"),
ui.suspend_video ("off" | "video"). Entfernt wird ein Video nur dort („Startvideo entfernen“, settings/home._remove_video;
das frühere RPC video.remove nutzte nur die Attrappe). Prüfmodus: kein echtes Netz (bootvideos.FAKE_ENV für eine
Attrappe).
"""
from __future__ import annotations

import os
import threading
import time
import urllib.parse
from concurrent.futures import Future
from typing import Any

from . import bootvideos, log, paths

_log = log.get("wiring.videos")

DEFAULTS = {"ui.intro_style": "own", "ui.suspend_video": "off"}
STYLES = ("own", "video")
SUSPEND = ("off", "video")
PROGRESS_S = 0.25
SUSPEND_MAX_MS = int(bootvideos.MAX_SECONDS["suspend"] * 1000)
SUSPEND_GRACE_MS = 1500             # Laden und erstes Bild; danach geht es ohne Rückmeldung der Seite weiter
MSG_BUSY = "Es wird schon ein Video geladen."
MSG_SAVED = {"boot": "„%s“ ist jetzt deine Startanimation.", "suspend": "„%s“ läuft jetzt vor dem Ruhemodus."}


def folder() -> str:
    """Ablage der gewählten Videos im Datenordner des PC5 Launchers."""
    return os.path.join(paths.data_dir(), "videos")


def make_repo(wiring: Any) -> bootvideos.Repo | None:
    """Der eine Abrufer; None im Prüfmodus ohne Attrappe (dann gibt es kein Netz)."""
    given = getattr(wiring, "bootvideo_repo", None)
    if given is not None:
        return given
    cache = os.path.join(paths.cache_dir(), "bootvideos")
    fake = bootvideos.fake_base()
    if fake is not None:
        return bootvideos.Repo(folder(), cache, base=fake)
    if paths.test_mode():
        return None
    return bootvideos.Repo(folder(), cache)


def art_url(path: str, mtime: int) -> str:
    """couch://art/<Pfad> mit dem Stand der Datei (eine ersetzte Datei kommt nicht aus dem Zwischenspeicher)."""
    return "couch://art" + urllib.parse.quote(os.path.abspath(path)) + "?v=%d" % int(mtime)


class VideoPart:
    def __init__(self, wiring: Any, repo: bootvideos.Repo | None):
        self.wiring = wiring
        self.app = wiring.app
        self.repo = repo
        # Lesen und Entfernen der Ablage gehen auch ohne Netz (Prüfmodus): eigener Repo nur für die Platte, derselbe Ordner
        self.store = bootvideos.Repo(folder(), os.path.join(paths.cache_dir(), "bootvideos"), base=None)
        self._lock = threading.Lock()
        self._busy = ""                 # Art, die gerade lädt
        self._suspend: Future | None = None
        self._suspend_timer: Any = None

    # ------------------------------------------------------------ Zustand
    def _cfg(self, key: str) -> Any:
        return self.wiring.config.get(key, DEFAULTS.get(key))

    def style(self) -> str:
        if self.wiring.config.get("ui.intro", True) is False:
            return "off"
        value = self._cfg("ui.intro_style")
        return value if value in STYLES else "own"

    def suspend_on(self) -> bool:
        return self._cfg("ui.suspend_video") == "video"

    def _view(self, kind: str) -> dict | None:
        meta = self.store.meta(kind)
        if meta is None:
            return None
        return {"url": art_url(self.store.path(kind), meta["mtime"]), "id": meta["id"], "title": meta["title"],
                "author": meta["author"], "duration": meta["duration"]}

    def state(self) -> dict:
        return {"style": self.style(), "boot": self._view("boot"), "suspend": self._view("suspend"),
                "suspendOn": self.suspend_on()}

    def push(self) -> None:
        self.app.state.update(videos=self.state())

    # ------------------------------------------------------------ RPC (Arbeits-Thread)
    def video_list(self, params: dict) -> dict:
        kind = str(params.get("kind") or "")
        if kind not in bootvideos.KINDS:
            return {"ok": False, "reason": "bad-request", "message": "Diese Auswahl gibt es nicht."}
        if self.repo is None:
            return {"ok": False, "reason": "test-mode", "message": bootvideos.MSG_TEST}
        page = params.get("page", 1)
        try:
            out = self.repo.list(kind, str(params.get("sort") or "popular"), page if isinstance(page, int) else 0)
        except bootvideos.VideoError as e:
            return {"ok": False, "reason": e.code, "message": e.message}
        meta = self.store.meta(kind)
        out["chosen"] = meta["id"] if meta else ""
        return out

    def video_choose(self, params: dict) -> dict:
        kind, post_id = str(params.get("kind") or ""), str(params.get("id") or "")
        if kind not in bootvideos.KINDS:
            return {"ok": False, "message": "Diese Auswahl gibt es nicht."}
        if self.repo is None:
            return {"ok": False, "message": bootvideos.MSG_TEST}
        with self._lock:
            if self._busy:
                return {"ok": False, "message": MSG_BUSY}
            self._busy = kind
        last = [0.0]

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last[0] >= PROGRESS_S or (total and done >= total):
                last[0] = now
                self.app.rpc.emit("video.progress", {"kind": kind, "id": post_id, "done": done, "total": total}, "home")
        try:
            meta = self.repo.download(kind, post_id, progress)
        except bootvideos.VideoError as e:
            return {"ok": False, "message": e.message}
        finally:
            with self._lock:
                self._busy = ""
        if kind == "boot":
            self.wiring.config.set("ui.intro", True)
            self.wiring.config.set("ui.intro_style", "video")
        else:
            self.wiring.config.set("ui.suspend_video", "video")
        self.wiring.main.post(self.push)
        title = meta.get("title") or post_id
        return {"ok": True, "title": title, "message": MSG_SAVED[kind] % title}

    # ------------------------------------------------------------ Ruhemodus-Video (Hauptschleife)
    def suspend_video(self, role: str) -> Future | None:
        """Für power.Power.suspend_video: spielt das Ruhemodus-Video in der Seite role und liefert ein Future, das nach
        dem Video (Meldung video.played) oder spätestens nach SUSPEND_MAX_MS + SUSPEND_GRACE_MS erfüllt ist; None, wenn
        es keins gibt (aus, Datei fehlt, unbekannte Seite)."""
        if role not in ("home", "overlay") or not self.suspend_on():
            return None
        view = self._view("suspend")
        if view is None:
            return None
        self._finish_suspend()
        fut: Future = Future()
        self._suspend = fut
        sched = self.app.shell.sched
        self._suspend_timer = sched.later(SUSPEND_MAX_MS + SUSPEND_GRACE_MS, self._finish_suspend)
        self.app.rpc.emit("video.suspend", {"url": view["url"], "maxMs": SUSPEND_MAX_MS, "title": view["title"]}, role)
        _log.info("Ruhemodus-Video in %s: %s", role, view["id"])
        return fut

    def _finish_suspend(self) -> None:
        fut, self._suspend = self._suspend, None
        if self._suspend_timer is not None:
            try:
                self.app.shell.sched.cancel(self._suspend_timer)
            except Exception:  # noqa: BLE001
                pass
            self._suspend_timer = None
        if fut is not None and not fut.done():
            fut.set_result(True)

    def video_played(self, params: dict) -> dict:
        self._finish_suspend()
        return {"ok": True}


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(dict(DEFAULTS))
    repo = make_repo(wiring)
    if repo is None:
        _log.info("Prüfmodus: Videos werden nicht aus dem Netz geladen (%s für eine Attrappe)", bootvideos.FAKE_ENV)
    part = VideoPart(wiring, repo)
    wiring.videos = part
    # Die Seite holt die Videos über couch://art: Ordner anlegen und als Wurzel anmelden (vor den Fenstern)
    try:
        part.store.ensure_folder()
        if not paths.add_art_root(folder()):
            _log.warning("Ordner der Videos nicht als Bildwurzel angemeldet: %s", folder())
    except bootvideos.VideoError as e:
        _log.warning("%s (%s)", e.message, folder())

    from .wiring import pooled
    app.rpc.register("video.list", pooled(wiring, part.video_list))
    app.rpc.register("video.choose", pooled(wiring, part.video_choose))
    app.rpc.register("video.played", lambda params: part.video_played(dict(params or {})))
    power = getattr(app, "power", None)
    if power is not None and hasattr(power, "suspend_video"):
        power.suspend_video = part.suspend_video
    part.push()
    on_change = getattr(wiring.config, "on_change", None)
    if callable(on_change):
        for key in ("ui.intro", "ui.suspend_video"):         # Vorsilbe: ui.intro umfasst ui.intro_style
            on_change(key, lambda key, value: wiring.main.post(part.push))
    wiring.after_ready.append(lambda: wiring._bg(part.store.cleanup_parts))
