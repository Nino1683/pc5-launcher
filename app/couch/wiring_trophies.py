"""Steam-Trophäen (E124): Verdrahtung, eingehängt über wiring.PARTS (nach "gameinfo").

RPC (alles im eigenen Arbeits-Thread „couch-trophies“: Platte und Netz, nie die Hauptschleife, nie der Pool):
    trophies.summary {}          -> {ok, totals: {platinum, gold, silver, bronze, unknown, unlocked, total, games},
                                     games: [{id, appid, title, total, unlocked, percent, tiers, platinum, last, entry?}]}
                                    nur Spiele mit Fortschritt, zuletzt freigeschaltet zuerst
    trophies.game {id}           -> {ok, id, appid, progress: {total, unlocked, percent, tiers, platinum, last},
                                     steam: {title, items: [{key, unlocked, time, hidden, name, desc, rarity, tier, icon}]}}
    trophies.icons {id, keys}    -> {icons: {key: "couch://art/…" | null}} (lädt fehlende Symbole, höchstens 24)
    ohne Trophäen {ok: false, reason: "no-steam" (kein steam:<appid>, Nicht-Steam-Spiele still weglassen) | "none"}
    Ereignis trophies.changed {id, appid} an die Home-Seite, sobald neue Trophäen gemeldet sind; {id: "", appid: 0},
    wenn die Seltenheiten der Übersicht im Hintergrund neu geholt wurden (summary antwortet aus dem Zwischenspeicher).
    Was Steam schreibt (Namen, Beschreibungen), steht unter "steam": Die Übersetzung an der Brücke fasst es nicht an.

Mitteilung beim Freischalten: Startet die Konsole ein Steam-Spiel (procs „started“, steam:<appid>), wird sein Stand
gemerkt (stille Grundlinie, trophies.Trophies.check mit start=True) und die Nutzerdatei alle WATCH_S Sekunden nur per
stat beobachtet; ändert sie sich, liest der Arbeits-Thread sie und meldet neue Bits (Stufe, Name, Ton der Mitteilung,
auch im Spiel nach notify.in_game). Ob Steam die Datei sofort beim Freischalten schreibt, ist unbestätigt: Nach dem
Ende des Spiels wird AFTER_S später noch einmal nachgesehen. Gemeldet wird je Trophäe nur einmal (<state>/
trophies-seen.json); als neu zählt, was seit dem Start dieser Sitzung freigeschaltet wurde (Zeit von Steam) bzw. ohne
Zeit erst nach der Grundlinie auftauchte.

Prüfmodus: Steam-Dateien nur aus dem Käfig (wiring.sources.steam_root zeigt in die Attrappen-Welt), Netz nur über
COUCH_STEAMINFO_FAKE (wiring_gameinfo.fake_base) bzw. eingesteckt (wiring.trophies_get), sonst gar keins.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from . import i18n, log, paths, trophies

_log = log.get("wiring.trophies")

WATCH_S = 20.0                  # stat der Nutzerdatei, solange ein Steam-Spiel läuft
AFTER_S = (5.0, 30.0, 90.0)     # nach dem Ende noch so oft nachsehen
CACHE_SUB = "trophies"
REFRESH_GAP_S = 600.0           # Seltenheiten der Übersicht höchstens so oft im Hintergrund nachholen
_STEAM = re.compile(r"steam:(\d{1,10})")
MSG_NONE = "Zu diesem Spiel gibt es keine Trophäen."
MSG_NO_STEAM = "Trophäen gibt es nur für Steam-Spiele."


def appid_of(entry_id: Any) -> int | None:
    m = _STEAM.fullmatch(str(entry_id or ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def steam_root(wiring: Any) -> str | None:
    root = getattr(getattr(wiring, "sources", None), "steam_root", None)
    if root:
        return root
    return None if paths.test_mode() else os.path.join(paths.home(), ".local", "share", "Steam")


def make_service(wiring: Any) -> trophies.Trophies:
    """Der eine Dienst. Netz: eingesteckt (Tests), die Attrappe (Prüfläufe) oder Steam; im Prüfmodus sonst keins."""
    folder = os.path.join(paths.cache_dir(), CACHE_SUB)
    injected = getattr(wiring, "trophies_get", None)
    kw: dict = {}
    if injected is not None:
        kw["get"] = injected
    else:
        from .wiring_gameinfo import fake_base
        fake = fake_base()
        if fake is not None:
            kw.update(api=fake, cdn=fake)
        elif paths.test_mode():
            kw["get"] = None
    for key in ("api", "cdn"):
        given = getattr(wiring, "trophies_" + key, None)
        if given:
            kw[key] = given
    return trophies.Trophies(lambda: steam_root(wiring), folder, paths.state_dir(), **kw)


class TrophyPart:
    def __init__(self, wiring: Any, service: trophies.Trophies, lane: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.service = service
        self.lane = lane
        self.sched = getattr(getattr(self.app, "shell", None), "sched", None) or getattr(wiring, "sched", None)
        self.watching: dict[int, dict] = {}     # appid -> {"since", "path", "sig"} (Hauptschleife)
        self.timer: Any = None
        self.closed = False
        self._refresh_at = -1e9

    # ------------------------------------------------------------ gemeinsam
    def _lang(self) -> str:
        return i18n.language(self.wiring.config)

    def _entry(self, appid: int) -> Any:
        try:
            return self.app.library.get("steam:%d" % appid)
        except Exception:  # noqa: BLE001 (Bibliothek ausgefallen: dann ohne Titel und Bild)
            return None

    def _title(self, appid: int) -> str:
        entry = self._entry(appid)
        return str(getattr(entry, "title", "") or "")

    # ------------------------------------------------------------ RPC (Arbeits-Thread)
    def summary(self, params: dict | None = None) -> dict:
        if self.service.root() is None:
            return {"ok": False, "reason": "no-steam", "message": MSG_NO_STEAM}
        # Seltenheit nur aus dem Zwischenspeicher: Sie für jedes Spiel nacheinander zu holen dauerte im RPC Sekunden
        # (beim ersten Öffnen und wenn alle zugleich veralten); das holt _refresh danach
        out = self.service.summary(self._title, fetch=False)
        self._refresh_later([g["appid"] for g in out["games"]])
        for game in out["games"]:
            game["id"] = "steam:%d" % game["appid"]
            entry = self._entry(game["appid"])
            if entry is not None and callable(getattr(entry, "view", None)):
                game["entry"] = entry.view(False)
        return {"ok": True, **out}

    def _refresh_later(self, appids: list[int]) -> None:
        now = time.monotonic()
        if not appids or now - self._refresh_at < REFRESH_GAP_S:
            return
        self._refresh_at = now
        self.lane.submit(self._refresh, list(appids))   # läuft nach dieser Antwort (eine Lane, der Reihe nach)

    def _refresh(self, appids: list[int], changed: bool = False) -> None:
        """Arbeits-Thread: veraltete oder fehlende Seltenheiten holen (frische kommen aus dem Zwischenspeicher). Ein
        Spiel je Auftrag, der Rest kommt wieder hinten in die Lane: Sonst wartete ein trophies.game, das während des
        Nachholens kommt, hinter den Abrufen aller Spiele (Sekunden). trophies.changed einmal am Ende."""
        if self.closed or not appids:
            return
        appid, rest = appids[0], appids[1:]
        try:
            before = self.service.rarity(appid, fetch=False)
            changed = self.service.rarity(appid) != before or changed
        except Exception:  # noqa: BLE001
            _log.exception("Seltenheit von %d", appid)
        if rest:
            if not self.closed:
                self.lane.submit(self._refresh, rest, changed)
            return
        if changed and not self.closed:
            try:
                self.app.rpc.emit("trophies.changed", {"id": "", "appid": 0}, "home")
            except Exception:  # noqa: BLE001
                _log.exception("trophies.changed")

    def game(self, params: dict | None = None) -> dict:
        entry_id = str((params or {}).get("id") or "")
        appid = appid_of(entry_id)
        if appid is None:
            return {"ok": False, "reason": "no-steam", "message": MSG_NO_STEAM}
        data = self.service.game(appid, self._lang())
        if data is None:
            return {"ok": False, "reason": "none", "appid": appid, "message": MSG_NONE}
        return {"ok": True, "id": entry_id, "appid": appid, "progress": data["progress"],
                "steam": {"title": self._title(appid) or data["name"], "items": data["items"]}}

    def icons(self, params: dict | None = None) -> dict:
        params = params or {}
        appid = appid_of(params.get("id"))
        keys = params.get("keys")
        if appid is None or not isinstance(keys, list):
            return {"icons": {}}
        return {"icons": self.service.icons(appid, keys)}

    # ------------------------------------------------------------ Mitteilungen (Arbeits-Thread)
    def _check(self, appid: int, since: float, start: bool = False) -> None:
        try:
            found = self.service.check(appid, self._lang(), since=since, start=start)
        except Exception:  # noqa: BLE001 (eine kaputte Datei darf das Beobachten nicht beenden)
            _log.exception("Trophäen von %d prüfen", appid)
            return
        if not found:
            return
        title = self._title(appid) or "Steam-Spiel %d" % appid
        _log.info("Trophäen: %d neu in %s", len(found), title)
        for head, body, icon in trophies.messages(title, found):
            self.app.notify.push(head, body, icon=icon)
        try:
            self.app.rpc.emit("trophies.changed", {"id": "steam:%d" % appid, "appid": appid}, "home")
        except Exception:  # noqa: BLE001
            _log.exception("trophies.changed")

    def _baseline(self, appid: int, since: float) -> None:
        """Arbeits-Thread: Grundlinie und Pfad der Nutzerdatei für das Beobachten."""
        self._check(appid, since, start=True)
        path = self.service.user_file(appid)
        sig = _sig(path)
        self.wiring.main.post(lambda: self._watched(appid, path, sig))

    # ------------------------------------------------------------ Beobachten (Hauptschleife)
    def on_proc(self, running: Any, what: str) -> None:
        appid = appid_of(getattr(running, "entry_id", ""))
        if appid is None or self.closed:
            return
        since = float(getattr(running, "started", 0.0) or 0.0)
        if what == "started" and appid not in self.watching:
            self.watching[appid] = {"since": since, "path": None, "sig": None}
            self.lane.submit(self._baseline, appid, since)
            self._arm()
        elif what == "exited":
            self.watching.pop(appid, None)
            self.lane.submit(self._check, appid, since)
            for delay in AFTER_S:
                self._later(delay, lambda: self.closed or self.lane.submit(self._check, appid, since))

    def _watched(self, appid: int, path: str | None, sig: Any) -> None:
        w = self.watching.get(appid)
        if w is not None:
            w["path"], w["sig"] = path, sig

    def _later(self, seconds: float, fn: Any) -> Any:
        if self.sched is None:
            return None
        return self.sched.later(seconds * 1000, fn)

    def _arm(self) -> None:
        if self.timer is None and self.watching and not self.closed:
            self.timer = self._later(WATCH_S, self._tick)

    def _tick(self) -> None:
        self.timer = None
        for appid, w in list(self.watching.items()):
            if w["path"] is None:
                continue                        # die Grundlinie ist noch nicht fertig
            sig = _sig(w["path"])
            if sig != w["sig"]:
                w["sig"] = sig
                self.lane.submit(self._check, appid, w["since"])
        self._arm()

    def close(self) -> None:
        self.closed = True
        timer, self.timer = self.timer, None
        if timer is not None and self.sched is not None:
            try:
                self.sched.cancel(timer)
            except Exception:  # noqa: BLE001
                _log.exception("Zeitgeber der Trophäen")


def _sig(path: str | None) -> Any:
    """Kennung einer Datei für das Beobachten: (mtime_ns, Größe) oder None (fehlt). Nur stat, kein Lesen."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def extend(wiring: Any) -> None:
    app = wiring.app
    service = make_service(wiring)
    # Symbole über couch://art: vor dem ersten Fenster anmelden (ui/host.py liest die Liste einmal)
    if service.ensure_cache() and not paths.add_art_root(service.cache_dir):
        _log.warning("Trophäen: %s lässt sich nicht als Bilder-Wurzel anmelden", service.cache_dir)
    from .wiring_settings import Lane
    lane = (getattr(wiring, "lane_factory", None) or Lane)("couch-trophies")
    part = TrophyPart(wiring, service, lane)
    wiring.trophies = part

    def route(fn):
        return lambda params: lane.submit(fn, dict(params or {}))

    app.rpc.register("trophies.summary", route(part.summary))
    app.rpc.register("trophies.game", route(part.game))
    app.rpc.register("trophies.icons", route(part.icons))
    procs_on = getattr(getattr(app, "procs", None), "on_change", None)
    if callable(procs_on):
        procs_on(part.on_proc)
    wiring.shutdown_hooks.append(part.close)
    wiring.shutdown_hooks.append(lane.close)
