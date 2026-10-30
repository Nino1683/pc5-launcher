"""Spielinfos, Neuigkeiten und Trailer in der Spielansicht (E123): Verdrahtung, eingehängt über wiring.PARTS.

RPC (die Arbeit läuft im Arbeits-Thread, nie in der Hauptschleife):
    game.info {id}      -> {ok, appid, steam: {title, short, about, genres, developers, publishers, release,
                           coming_soon, metacritic, screenshots: [{thumb, full}], url}}
    game.news {id}      -> {ok, appid, steam: {news: [{gid, title, date, label, patch, text, url}]}}
    game.trailer {id | appid, movie?, full?}
                        -> {ok, appid, trailer: {movie, name, poster, width, height, seconds, video, av, audio, saved}
                           | null}: video/av/audio sind Adressen des Trailer-Dienstes (trailerfeed.py, E132) auf
                           127.0.0.1, ein Datenstrom ohne Datei; av = Bild mit Ton, audio = nur Ton (null ohne Ton).
                           full: bis 1080p (groß im Store), sonst bis 720p. saved: liegt schon im eigenen Spielordner.
                           ohne Steam-Angaben: {ok: false, reason: "no-steam" | "not-listed" | "offline" | "test-mode"
                           | "blocked", message?}
    game.trailers {id | appid} -> {ok, appid, trailers: [{movie, name, poster, highlight}]} (Store, E132)
                        Was Steam schreibt (Beschreibung, Meldungen), steht immer unter "steam": Die Übersetzung an der
                        Brücke (i18n.translate_tree) darf es nicht anfassen (machte aus „1,000 players“ „1.000 players“).
    game.openNews {id, gid, closeOther?} -> wie store.buy: Meldung im Browser der Konsole (needsConfirm "close-other")
sys.state.trailers      Einstellung games.trailers (Einstellungen › Spiele › „Trailer im Hintergrund“); ob der
                        Trailer wirklich läuft, entscheidet die Seite (components/trailer.js)
sys.state.game_music    Einstellung games.music („Spielmusik im Home“, Vorgabe aus): der Ton desselben Trailers, leise

Steam-App-ID: steam:<appid> direkt. Andere Einträge nur mit einem sicheren Treffer der Bildsuche (artfetch): von
Hand gewählt (artchoices.json, Quelle steam) oder automatisch mit Quelle steam ab SURE_SCORE (Stufen „streng“ und
„Ausgabe“, library/artsources/match.py; „Untertitel“ und „Einschub“ zählen nicht, dort ist ein Reihenname leicht
ein anderes Spiel).

Sprache der Angaben nach der Oberfläche (i18n.language: german | english). Neuigkeiten haben bei Steam keine
Sprache; sie kommen, wie der Hersteller sie schreibt.

Trailer (E132): Der Dienst holt die Stücke selbst und reicht sie als Strom weiter. Nur für eigene Spiele (Eintrag
local:…, Ordner mit Marker, installer/trash.open_own_dir) schreibt er zugleich eine Datei in <Spielordner>/.pc5-trailer
(danach spielt die Datei); Steam-, Lutris- und alle anderen Ordner fasst er nie an. Hänger-Schutz: Lädt die Seite des
Home neu, weil sie nicht mehr schlug oder abstürzte, während ein Trailer lief oder noch laufen konnte (Beginn des Stroms bis
Länge + 30 s, trailerfeed.Feed.active; ui.on_page_reset), gibt es für dieses
Spiel in dieser Sitzung keinen Trailer mehr; nach HANGS_OFF solchen Fällen für keines mehr (reason "blocked").

Prüfmodus: kein echtes Netz. COUCH_STEAMINFO_FAKE=http://127.0.0.1:<port> lenkt Store, API und Videos auf eine
Attrappe (tests/features/steaminfo/fake_steam.py); Tests stecken auch wiring.steaminfo selbst ein. Sonst
antworten die Methoden mit reason "test-mode".
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any

from . import i18n, log, paths, trailerfeed
from .store import steam_info
from .store.cache import StoreError
from .store.steam import NotListed

_log = log.get("wiring.gameinfo")

SURE_SCORE = 0.88               # match.SCORE_EDITION: dieselbe Ausgabe oder strenger
DEFAULTS = {"games.trailers": True, "games.music": False}
FAKE_ENV = "COUCH_STEAMINFO_FAKE"
MSG_TEST = "Im Prüfmodus nicht verfügbar."
MSG_NO_STEAM = "Zu diesem Spiel kennt der PC5 Launcher keine Steam-Seite."
MSG_BLOCKED = "Der Trailer wurde ausgelassen, weil die Oberfläche beim letzten Mal hängen blieb."
HANGS_OFF = 2                   # so viele Hänger mit Trailer, dann keiner mehr in dieser Sitzung


def fake_base() -> str | None:
    """Adresse einer Attrappe für Prüfläufe: nur im Prüfmodus und nur auf diesem Rechner (fakenet.base_url)."""
    from . import fakenet
    return fakenet.base_url(FAKE_ENV)


def make_info(wiring: Any) -> steam_info.SteamInfo | None:
    """Der eine Abrufer; None im Prüfmodus ohne Attrappe (dann gibt es kein Netz)."""
    given = getattr(wiring, "steaminfo", None)
    if given is not None:
        return given
    folder = os.path.join(paths.cache_dir(), "steaminfo")
    fake = fake_base()
    if fake is not None:
        return steam_info.SteamInfo(folder, shop=fake, api=fake, loopback=True)
    if paths.test_mode():
        return None
    return steam_info.SteamInfo(folder)


def steam_appid(wiring: Any, entry: Any) -> int | None:
    """Steam-App-ID eines Eintrags oder None. Liest höchstens die kleinen Merkdateien der Bildsuche
    (artfetch.json, artchoices.json): im Arbeits-Thread rufen."""
    m = re.fullmatch(r"steam:(\d+)", str(getattr(entry, "id", "")))
    if m:
        return int(m.group(1))
    if getattr(entry, "kind", "game") != "game":
        return None
    resolver = getattr(wiring, "art", None)
    get_fetcher = getattr(resolver, "_get_fetcher", None)
    if not callable(get_fetcher):
        return None
    try:
        fetcher = get_fetcher()
        choice = fetcher.choice(entry.id)
        if choice:
            # Von Hand gewählt („Bild ändern“ › Treffer): sicher, aber nur, wenn es ein Steam-Treffer war
            ref = str(choice.get("ref") or "")
            return int(ref) if choice.get("source") == "steam" and ref.isdigit() and 0 < int(ref) < 2**31 else None
        rec = fetcher.status(entry.id) or {}
    except Exception:  # noqa: BLE001 (Merkdatei kaputt: dann eben keine Angaben)
        _log.exception("Steam-Nummer von %s", getattr(entry, "id", "?"))
        return None
    appid, score = rec.get("appid"), rec.get("score")
    if rec.get("source") != "steam" or rec.get("status") not in ("ok", "partial"):
        return None
    if isinstance(appid, bool) or not isinstance(appid, int) or not 0 < appid < 2**31:
        return None
    return appid if isinstance(score, (int, float)) and score >= SURE_SCORE else None


class GameInfoPart:
    def __init__(self, wiring: Any, info: steam_info.SteamInfo | None):
        self.wiring = wiring
        self.app = wiring.app
        self.info = info
        self.lock = threading.Lock()
        self.appids: dict[str, int] = {}            # Eintrag -> App-ID (für game.openNews in der Hauptschleife)
        self.feed = trailerfeed.Feed(allowed=lambda url: steam_info.allowed(url, steam_info.VIDEO_HOSTS,
                                                                            bool(info is not None and info.loopback)))
        self.keys: dict[str, int] = {}              # key eines Trailers -> App-ID (Hänger-Schutz)
        self.blocked: set[int] = set()              # App-IDs, deren Trailer die Seite hängen ließ
        self.hangs = 0
        self.links: dict[tuple[int, str], str] = {}  # (App-ID, gid) -> Adresse aus der letzten Antwort von game.news

    # ------------------------------------------------------------ gemeinsam
    def _lang(self) -> str:
        return i18n.language(self.wiring.config)

    def _resolve(self, entry_id: str, appid: Any = None) -> tuple[int | None, dict | None]:
        """Arbeits-Thread: (App-ID, None) oder (None, Antwort ohne Angaben). appid: Store-Seite (Steam-App-ID direkt)."""
        if self.info is None:
            return None, {"ok": False, "reason": "test-mode", "message": MSG_TEST}
        if not entry_id and appid is not None:
            text = str(appid)
            if text.isdigit() and 0 < int(text) < 2**31:
                return int(text), None
            return None, {"ok": False, "reason": "no-steam", "message": MSG_NO_STEAM}
        entry = self.app.library.get(entry_id) if entry_id else None
        appid = steam_appid(self.wiring, entry) if entry is not None else None
        if appid is None:
            return None, {"ok": False, "reason": "no-steam", "message": MSG_NO_STEAM}
        with self.lock:
            self.appids[entry_id] = appid
        return appid, None

    def _fail(self, appid: int, e: BaseException) -> dict:
        if isinstance(e, NotListed):
            return {"ok": False, "appid": appid, "reason": "not-listed", "message": str(e)}
        return {"ok": False, "appid": appid, "reason": "offline", "message": str(e) or steam_info.MSG_OFFLINE}

    # ------------------------------------------------------------ RPC (Arbeits-Thread)
    def game_info(self, params: dict) -> dict:
        appid, out = self._resolve(str(params.get("id") or ""))
        if appid is None:
            return out
        try:
            return {"ok": True, "appid": appid, "steam": self.info.details(appid, self._lang())}
        except StoreError as e:
            return self._fail(appid, e)

    def game_news(self, params: dict) -> dict:
        appid, out = self._resolve(str(params.get("id") or ""))
        if appid is None:
            return out
        try:
            items = self.info.news(appid)
        except StoreError as e:
            return self._fail(appid, e)
        with self.lock:
            for it in items:
                self.links[(appid, it["gid"])] = it["url"]
        return {"ok": True, "appid": appid, "steam": {"news": items}}

    def game_trailer(self, params: dict) -> dict:
        entry_id = str(params.get("id") or "")
        appid, out = self._resolve(entry_id, params.get("appid"))
        if appid is None:
            return out
        with self.lock:
            blocked = self.hangs >= HANGS_OFF or appid in self.blocked
        if blocked:
            return {"ok": False, "appid": appid, "reason": "blocked", "message": MSG_BLOCKED}
        full = bool(params.get("full"))
        movie = params.get("movie")
        try:
            t = self.info.trailer(appid, self._lang(), str(movie) if movie is not None else None,
                                  steam_info.TRAILER_FULL_H if full else steam_info.TRAILER_MAX_H)
        except StoreError as e:
            return self._fail(appid, e)
        if t is None:
            return {"ok": True, "appid": appid, "trailer": None}
        # Eigenes Spiel (Ordner mit Marker): zugleich als Datei im Spielordner; groß (Store) nie
        own = None if full or not entry_id else self._own_dir(entry_id)
        spec = {"key": t["key"], "seconds": t["seconds"], "video": t["video"], "audio": t["audio"], "own": own,
                "estimate": int(t.get("bandwidth") or 0) * float(t["seconds"]) / 8.0}
        try:
            token = self.feed.register(spec)
        except trailerfeed.FeedError as e:
            _log.warning("Trailer %s nicht abspielbar: %s", t["key"], e)
            return {"ok": True, "appid": appid, "trailer": None}
        with self.lock:
            self.keys[t["key"]] = appid
        saved = self._saved(own, t["key"]) if own is not None else {"video": False, "audio": False}
        audio = t["audio"] is not None
        return {"ok": True, "appid": appid, "trailer": {
            "movie": t["movie"], "name": t["name"], "poster": t["poster"], "width": t["width"], "height": t["height"],
            "seconds": t["seconds"], "video": self.feed.url(token, "video"),
            "av": self.feed.url(token, "av") if audio else None, "audio": self.feed.url(token, "audio") if audio else None,
            "saved": bool(saved["video"])}}

    def game_trailers(self, params: dict) -> dict:
        appid, out = self._resolve(str(params.get("id") or ""), params.get("appid"))
        if appid is None:
            return out
        try:
            return {"ok": True, "appid": appid, "trailers": self.info.movies(appid, self._lang())}
        except StoreError as e:
            return self._fail(appid, e)

    # ------------------------------------------------------------ Trailer: eigener Spielordner, Hänger-Schutz
    def _own_dir(self, entry_id: str) -> trailerfeed.OwnDir | None:
        """Nur ein Eintrag local:… mit Marker, dessen Ordner trash.open_own_dir besteht (geprüft bei jedem Strom)."""
        if not entry_id.startswith("local:"):
            return None
        try:
            from . import wiring_install, wiring_library
            from .installer import trash
            from .installer.service import _flat_entry
            record = _flat_entry(wiring_library.get_local_record(self.app, entry_id))
            root = wiring_install.games_root(self.app)
        except Exception:  # noqa: BLE001 (ohne Installer kein eigener Ordner: nur streamen)
            _log.exception("Spielordner von %s", entry_id)
            return None
        if not record or not record.get("marker") or not record.get("install_dir"):
            return None
        try:
            fd, _real = trash.open_own_dir(record, root)
            os.close(fd)
        except (trash.Refuse, OSError) as e:
            _log.info("Trailer von %s nur gestreamt: %s", entry_id, e)
            return None
        return trailerfeed.OwnDir(lambda: trash.open_own_dir(record, root)[0], label=entry_id)

    def _saved(self, own: trailerfeed.OwnDir, key: str) -> dict:
        try:
            fd = own.open()
        except Exception:  # noqa: BLE001
            return {"video": False, "audio": False}
        try:
            trailerfeed.prune(fd, key)                  # ältere Fassungen dieses Spiels einzeln weg
            return trailerfeed.own_files(fd, key)
        finally:
            os.close(fd)

    def page_reset(self, role: str, reason: str) -> None:
        """Die Seite des Home wurde neu geladen (kein Herzschlag, Absturz): lief dabei ein Trailer, ihn verwerfen."""
        if role != "home" or reason not in ("no-beat", "crashed", "exceeded-memory-limit", "exceeded-cpu-limit"):
            return
        keys = self.feed.active()
        if not keys:
            return
        with self.lock:
            appids = {self.keys[k] for k in keys if k in self.keys}
            self.blocked |= appids
            self.hangs += 1
            hangs = self.hangs
        _log.warning("Seite neu geladen (%s), während ein Trailer lief: Trailer von %s ausgelassen%s", reason,
                     ", ".join(str(a) for a in sorted(appids)) or "?",
                     "; ab jetzt keine Trailer mehr in dieser Sitzung" if hangs >= HANGS_OFF else "")

    # ------------------------------------------------------------ RPC (Hauptschleife)
    def open_news(self, params: dict) -> Any:
        """Die Meldung im Browser der Konsole. Die Adresse kommt aus der letzten Antwort von game.news (Hauptschleife,
        nur der Speicher, kein Netz), nie von der Seite; sonst die feste Seite der Meldung im Steam-Store."""
        entry_id, gid = str(params.get("id") or ""), str(params.get("gid") or "")
        m = re.fullmatch(r"steam:(\d+)", entry_id)
        with self.lock:
            appid = int(m.group(1)) if m else self.appids.get(entry_id)
            url = self.links.get((appid, gid)) if appid is not None else None
        if appid is None or not gid.isdigit() or len(gid) > 24:
            return {"ok": False, "message": "Diese Meldung gibt es nicht."}
        url = url or "%s/news/app/%d/view/%s" % (steam_info.SHOP, appid, gid)
        opener = getattr(self.wiring, "store_open_url", None)
        if not callable(opener):
            return {"ok": False, "message": "Der Browser ist gerade nicht verfügbar."}
        return opener(url, bool(params.get("closeOther")))

    # ------------------------------------------------------------ Zustand
    def push_trailers(self) -> None:
        cfg = self.wiring.config
        self.app.state.update(trailers=bool(cfg.get("games.trailers", True)), game_music=bool(cfg.get("games.music", False)))


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(dict(DEFAULTS))
    info = make_info(wiring)
    if info is None:
        _log.info("Prüfmodus: Spielinfos werden nicht aus dem Netz geladen (%s für eine Attrappe)", FAKE_ENV)
    part = GameInfoPart(wiring, info)
    wiring.gameinfo = part

    from .wiring import pooled
    app.rpc.register("game.info", pooled(wiring, part.game_info))
    app.rpc.register("game.news", pooled(wiring, part.game_news))
    app.rpc.register("game.trailer", pooled(wiring, part.game_trailer))
    app.rpc.register("game.trailers", pooled(wiring, part.game_trailers))
    app.rpc.register("game.openNews", lambda params: part.open_news(dict(params or {})))
    part.push_trailers()
    ui = getattr(app, "ui", None)
    hooks = getattr(ui, "on_page_reset", None)
    if isinstance(hooks, list):
        hooks.append(part.page_reset)
    shutdown = getattr(wiring, "shutdown_hooks", None)
    if isinstance(shutdown, list):
        shutdown.append(part.feed.stop)
    on_change = getattr(wiring.config, "on_change", None)
    if callable(on_change):
        for key in DEFAULTS:
            on_change(key, lambda key, value: wiring.main.post(part.push_trailers))
