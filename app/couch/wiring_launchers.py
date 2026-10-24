"""Store, Teil 4 (Bauplan E94): Battle.net, Ubisoft und Rockstar im Store des PC5 Launchers.

Lesen, Kaufen und „Meine Spiele“ wie bei den anderen Shops (store/battlenet.py, ubisoft.py,
rockstar.py und ihre *_account.py; Anmeldung im Browser des PC5 Launchers). Installieren und Starten
gehen nur über den Launcher des Herstellers (store/launchers.py):

  1. „Installieren“ legt einen Auftrag an. Fehlt der Launcher, wird er vom offiziellen Server geladen
     und still in seinen Prefix auf der Spiele-Platte installiert (<Spiele>/Launcher/<Name>). Läuft
     gerade ein Spiel über diesen Launcher, wartet der Auftrag, bis es beendet ist (E99).
  2. Rockstar lädt unsichtbar (E96, launchers.hidden_install_args): der Launcher als Hintergrundprozess
     der Konsole; ohne Fortschritt nach HIDDEN_WAIT_S kommt er sichtbar nach vorn. Battle.net und
     Ubisoft öffnen sichtbar ihren Installationsdialog für genau dieses Spiel (Eintrag launcher:<id>,
     Pad als Maus). Beim ersten Mal meldet man sich dort auch einmal an.
  3. Danach lädt er im Hintergrund weiter (die App wird nie angehalten). Alle POLL_S Sekunden sieht
     die Konsole in der Registry des Prefix nach; ist das Spiel da und fertig, wird es ein Eintrag der
     Bibliothek (Quelle local, detail.launcher) mit Mitteilung, und der Launcher wird beendet, wenn
     sonst nichts mehr lädt.
  4. Gestartet wird über bin/couch-launcher-game (launch/proton.py), der Launcher bleibt verborgen.
     Vorher macht make_room im Prefix Platz (E99): Ein Launcher, den die Konsole für eine fertige
     Installation geöffnet hat, geht zu, ein unsichtbarer ohne Auftrag endet. Lädt er gerade oder hat
     der Nutzer ihn selbst geöffnet, wird der Start mit einem Satz abgelehnt.

Auch Spiele, die man im Launcher selbst installiert oder deinstalliert, gleicht die Konsole einmal je
Minute ab. Was hier nicht geht: Battle.net und Ubisoft ganz ohne Klick installieren (beide zeigen einen
Dialog; nur Rockstar lädt unsichtbar).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import threading
import time
from typing import Any

from . import log, paths
from .library.model import Entry
from .store import launchers
from .store.steam import StoreError

_log = log.get("wiring.launchers")

URL_MAX_AGE_S = 120
LOGIN_GRACE_S = 120.0           # nach einer Store-Anmeldung: „abgemeldet“ nicht zwischenspeichern
MAX_URL = 8192
STORE_IDS = ("battlenet", "ubisoft", "rockstar")
NAMES = {"battlenet": "Battle.net", "ubisoft": "Ubisoft Connect", "rockstar": "Rockstar Games Launcher"}
SHOP_NAMES = {"battlenet": "Battle.net", "ubisoft": "Ubisoft", "rockstar": "Rockstar"}
POLL_S = 5.0
HIDDEN_WAIT_S = 45.0             # so lange darf ein unsichtbarer Auftrag ohne Fortschritt bleiben, dann sichtbar
PROGRESS_S = 15.0                # Fortschritt eines Battle.net-Downloads (Ordnergröße) so oft
RATE_MIN_S = 2.0                 # Geschwindigkeit eines Downloads erst über mindestens so viele Sekunden (E99)
SYNC_S = 60.0
ACCOUNT_TTL_S = 120.0
ERROR_TTL_S = 10.0                   # Konto-Abfrage gescheitert (Netz) und kein alter Stand: so bald wieder fragen (E99)
CONFIRM_TIMEOUT_S = 45 * 60          # so lange wartet die Konsole, bis das Spiel im Launcher auftaucht
STARTING_S = 60.0                    # so lange gilt ein Spielstart im Prefix als „läuft“, bis seine App da ist
CLOSE_WAIT_S = 30.0                  # make_room: so lange auf das Ende des sichtbaren Launchers warten
ROOM_WAIT_S = 15.0                   # … und danach auf das Ende seines Wineservers
OPEN_GRACE_S = 60.0                  # so lange darf ein sichtbarer Launcher für einen Auftrag zum Öffnen brauchen
SOME_GAME = "das Spiel"              # Titel unbekannt (ohne Anführungszeichen, _game_text)
JOB_TEXT = {"queued": "Wartet", "launcher": "%s wird eingerichtet …", "confirm": "Bestätige in %s",
            "installing": "%s lädt …", "hidden": "%s startet im Hintergrund …",
            "waiting": "Wartet, bis das Spiel beendet ist"}
# Aufträge, die den Launcher noch nicht gestartet haben: Ein Spielstart darf dann vor (E99)
NOT_STARTED = ("queued", "waiting")
MARK = ".couch-installing"           # im Prefix, solange etwas lädt (bin/couch-launcher-game lässt einen fremden Launcher dann laufen)


def _module(name: str):
    try:
        return importlib.import_module("couch.store." + name)
    except ImportError as e:
        _log.warning("Store: Modul %s fehlt: %s", name, e)
        return None


def _umu() -> str:
    """argv[0] für umu-run (E104/E105, launch/proton.umu_run): aus dem PATH als Name wie bisher, sonst die
    eigene ZIP-App des Assistenten mit vollem Pfad. Fehlt beides, bleibt es beim Namen: Popen scheitert
    dann wie bisher (OSError, gleiche Meldung)."""
    from .launch import proton
    return proton.umu_run() or proton.UMU_RUN


def _game_text(title: str) -> str:
    """Spieltitel für einen Satz: in Anführungszeichen, der Rückfall SOME_GAME ohne (E99)."""
    return "„%s“" % title if title and title != SOME_GAME else SOME_GAME


def entry_id_for(launcher_id: str, code: Any) -> str:
    """Feste ID je Launcher und Spiel: Nach einer Neuinstallation ist es derselbe Eintrag."""
    return "local:" + hashlib.sha1(("%s:%s" % (launcher_id, code)).encode()).hexdigest()[:32]


class LauncherStores:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.rpc = self.app.rpc
        self.lock = threading.RLock()
        self.jobs: list[dict] = []
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.shops: dict = {}
        self.owned: dict = {sid: {"at": 0.0, "data": None} for sid in STORE_IDS}
        self.ubi_account = None
        self._last_sync = 0.0
        self.hidden: dict[str, Any] = {}       # Launcher, die die Konsole unsichtbar laufen lässt (Popen)
        # Unsichtbare Launcher aus der Zeit vor einem Neustart des Home (kein eigener Popen): Sie enden wie
        # eigene, wenn nichts mehr lädt (E96, E99)
        self.adopted: set[str] = set()
        self._starting: dict[str, tuple] = {}  # Store -> (monotonic, Titel, Eintrag): ein Spiel startet im Prefix
        # Wofür die Konsole den sichtbaren Launcher geöffnet hat ("install", "uninstall"; "" = der Nutzer selbst):
        # Nur nach einer eigenen Installation geht er für einen Spielstart still zu (E99)
        self._opening: dict[str, str] = {}     # open_launcher -> make_room des Launcher-Starts
        self._shown_for: dict[str, str] = {}
        self._save_lock = threading.Lock()
        self.proc_root = "/proc"               # für wineserver_pids (Tests: erfundenes /proc)
        self._saved_jobs: list | None = None
        self._login_at: dict[str, float] = {}   # Store-Anmeldung im Browser verlangt (monotonic)
        self._routed: set[str] = set()          # Prefixe, deren Links schon in den Browser der Konsole gehen
        self._url_monitor = None
        self.modules = {sid: (_module(sid), _module(sid + "_account")) for sid in STORE_IDS}

    # ------------------------------------------------------------ Orte
    def games_root(self) -> str:
        from . import wiring_install
        return wiring_install.games_root(self.app)

    def prefix(self, sid: str) -> str:
        return launchers.prefix_dir(self.games_root(), sid)

    def launcher_ready(self, sid: str) -> bool:
        return launchers.installed(self.prefix(sid), sid)

    @staticmethod
    def url_handler() -> str:
        """bin/couch-open-url; über den Verweis current, damit der Eintrag im Prefix neue Fassungen überlebt."""
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "couch-open-url")
        current = os.path.join(paths.data_dir(), "current", "app", "bin", "couch-open-url")
        return current if os.path.realpath(current) == os.path.realpath(here) else here

    def route_urls(self, sid: str) -> None:
        prefix = self.prefix(sid)
        if prefix not in self._routed and launchers.route_urls(prefix, self.url_handler()):
            self._routed.add(prefix)
            _log.info("Launcher %s: Links gehen in den Browser der Konsole", sid)

    def registry_file(self) -> str:
        return os.path.join(paths.state_dir(), "launcher-games.json")

    def known(self) -> dict:
        try:
            with open(self.registry_file(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_known(self, data: dict) -> None:
        tmp = self.registry_file() + ".neu"
        try:
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(tmp, self.registry_file())
        except OSError as e:
            _log.warning("launcher-games.json: %s", e)

    # ------------------------------------------------------------ Shops
    def shop(self, sid: str):
        if sid not in self.shops:
            mod = self.modules[sid][0]
            if mod is None:
                raise StoreError("Dieser Shop ist gerade nicht verfügbar.")
            cls = {"battlenet": "BattlenetStore", "ubisoft": "UbisoftStore", "rockstar": "RockstarStore"}[sid]
            self.shops[sid] = getattr(mod, cls)(os.path.join(paths.cache_dir(), "store", sid))
        return self.shops[sid]

    def tabs(self, sid: str) -> list:
        mod = self.modules[sid][0]
        return [list(t) for t in getattr(mod, "TABS", [])] if mod else []

    def genres(self, sid: str) -> list:
        mod = self.modules[sid][0]
        try:
            return list(mod.genres()) if mod else []
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def code_of(sid: str, card: dict):
        """Kennung des Spiels im Launcher: Battle.net-Code, Ubisoft-Spielnummer, Rockstar-Kürzel."""
        if sid == "ubisoft":
            try:
                return int(card.get("uplay_id") or 0) or None
            except (TypeError, ValueError):
                return None
        return str(card.get("code") or "") or None

    # ------------------------------------------------------------ Konto
    def cookies(self, sid: str) -> dict:
        acct = self.modules[sid][1]
        if acct is None or not hasattr(acct, "browser_cookies"):
            return {}
        try:
            return acct.browser_cookies(os.path.join(paths.data_dir(), "browser", "cookies.sqlite")) or {}
        except Exception as e:  # noqa: BLE001
            _log.warning("Store %s: Cookies nicht lesbar: %s", sid, e)
            return {}

    def ubi(self):
        if self.ubi_account is None:
            acct = self.modules["ubisoft"][1]
            if acct is None:
                return None
            ids = {}
            try:
                ids = self.shop("ubisoft").web_ids() or {}       # dieselben IDs wie die Anmeldeseite
            except Exception as e:  # noqa: BLE001
                _log.warning("Ubisoft: Anmelde-IDs nicht lesbar: %s", e)
            extra = {k: ids[k] for k in ("app_id", "genome_id") if ids.get(k)}
            self.ubi_account = acct.UbisoftAccount(os.path.join(paths.config_dir(), "ubisoft-token.json"),
                                                   resolve=self.shop("ubisoft").uplay_ids, **extra)
        login = os.path.join(paths.state_dir(), "ubisoft-login.json")
        if os.path.isfile(login):
            try:
                with open(login, encoding="utf-8") as fh:
                    raw = fh.read()
                os.unlink(login)
                if self.ubi_account.take_login(raw):
                    _log.info("Ubisoft: angemeldet")
                    self.owned["ubisoft"]["at"] = 0.0
            except OSError as e:
                _log.warning("Ubisoft-Anmeldung: %s", e)
        return self.ubi_account

    def account(self, sid: str, force: bool = False) -> dict:
        """{logged_in, ids (set: title_id | uplay_id | code/slug), games [{id, name}]}; zwischengespeichert."""
        memo = self.owned[sid]
        if not force and memo["data"] is not None and time.monotonic() - memo["at"] < ACCOUNT_TTL_S:
            return memo["data"]
        data = {"logged_in": False, "ids": set(), "games": []}
        acct = self.modules[sid][1]
        failed = False                  # Netz oder Störung: der Stand ist unbekannt, nicht „abgemeldet“
        try:
            if sid == "ubisoft":
                ubi = self.ubi()
                if ubi is not None and ubi.logged_in():
                    games = ubi.owned() or []
                    data = {"logged_in": True, "ids": {int(g.get("uplay_id") or 0) for g in games} - {0},
                            "games": [{"id": int(g.get("uplay_id") or 0), "name": str(g.get("name") or "")} for g in games]}
            elif acct is not None:
                cookies = self.cookies(sid)
                r = acct.owned(cookies) or {}
                if r.get("error") and memo["data"] is not None:     # Netz kurz weg: alter Stand gilt
                    return memo["data"]
                failed = bool(r.get("error"))
                if sid == "battlenet":
                    ids = {int(t) for t in r.get("title_ids") or [] if str(t).isdigit()}
                    games = [{"id": int(g.get("title_id") or 0), "name": str(g.get("name") or "")} for g in r.get("games") or []]
                else:
                    ids = {str(s) for s in (r.get("slugs") or []) + (r.get("codes") or [])}
                    games = [{"id": s, "name": ""} for s in r.get("slugs") or []]
                data = {"logged_in": bool(r.get("logged_in")), "ids": ids, "games": games}
                data = self._remember(sid, data, bool(getattr(acct, "remembered", lambda c: False)(cookies)))
        except Exception as e:  # noqa: BLE001 (Netz, Konto; Ubisoft wirft UbisoftError, E99)
            _log.warning("Store %s: eigene Spiele unbekannt: %s", sid, e)
            if memo["data"] is not None:
                return memo["data"]
            failed = True
        # Gleich nach einer Anmeldung kann der Browser die Cookies noch nicht geschrieben haben: „abgemeldet“
        # dann nur 5 s merken, danach prüft die nächste Frage neu (sonst zweimal anmelden, 2026-10-22).
        # Kam die Liste nicht (Netz) und gibt es keinen alten Stand, nur ERROR_TTL_S (E99)
        recent = time.monotonic() - self._login_at.get(sid, -1e9) < LOGIN_GRACE_S
        now = time.monotonic()
        if recent and not data["logged_in"]:
            at = now - ACCOUNT_TTL_S + 5
        elif failed and not data["logged_in"]:
            at = now - ACCOUNT_TTL_S + ERROR_TTL_S
        else:
            at = now
        memo.update(at=at, data=data)
        return data

    def _remember(self, sid: str, data: dict, remembered: bool) -> dict:
        """Letzter Stand eines angemeldeten Kontos auf der Platte; gilt weiter, solange das Konto im Browser
        angemeldet bleibt (langlebiges Cookie), auch wenn die kurze Sitzung abgelaufen ist."""
        path = os.path.join(paths.state_dir(), "store-owned-%s.json" % sid)
        if data["logged_in"]:
            try:
                tmp = path + ".neu"
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"at": time.time(), "ids": sorted(data["ids"], key=str), "games": data["games"]}, fh)
                os.replace(tmp, path)
            except OSError as e:
                _log.warning("Store %s: Stand nicht gemerkt: %s", sid, e)
            return data
        if not remembered:
            return data
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
            return {"logged_in": True, "ids": set(old.get("ids") or []), "games": list(old.get("games") or []), "stale": True}
        except (OSError, ValueError, AttributeError):
            return data

    def _after_login(self, sid: str) -> None:
        """Store-Anmeldung im Browser: Ist der Browser wieder zu, ein paar Mal nachfragen und die Seite
        neu laden lassen, sobald das Konto angemeldet ist."""
        end = time.monotonic() + 15 * 60
        while not self.stopped.wait(2) and time.monotonic() < end:
            if self.wiring.main.call(self.app.procs.by_entry, "app:browser") is None:
                break
        for delay in (1, 3, 6, 10):
            if self.stopped.wait(delay):
                return
            if self.account(sid, force=True)["logged_in"]:
                self.wiring.main.post(lambda: self.rpc.emit("store.owned", {"store": sid, "id": ""}))
                return

    def is_owned(self, sid: str, card: dict, acct: dict) -> bool:
        ids = acct["ids"]
        if sid == "battlenet":
            return int(card.get("title_id") or 0) in ids
        if sid == "ubisoft":
            return int(card.get("uplay_id") or 0) in ids
        return str(card.get("id") or "") in ids or str(card.get("code") or "") in ids

    # ------------------------------------------------------------ Karten markieren
    def annotate(self, sid: str, cards: list[dict]) -> list[dict]:
        acct = self.account(sid)
        known = self.known().get(sid) or {}
        with self.lock:
            busy = {str(j["code"]) for j in self.jobs if j["store"] == sid}
        for c in cards:
            code = self.code_of(sid, c)
            entry = known.get(str(code)) if code is not None else None
            c["installed"] = bool(entry) and self.app.library.get(entry) is not None
            c["entryId"] = entry if c["installed"] else ""
            c["downloading"] = code is not None and str(code) in busy
            c["owned"] = self.is_owned(sid, c, acct) or c["installed"]
            c["wishlist"] = False
        return cards

    def owned_cards(self, sid: str) -> list[dict]:
        """Reiter „Meine Spiele“: was dir gehört, als Karten aus dem Katalog (sonst nur mit Namen)."""
        acct = self.account(sid)
        if not acct["logged_in"]:
            return []
        try:
            catalog = []
            for tab, _label in self.tabs(sid)[:1]:
                page, more = 0, True
                while more and page < 6:
                    r = self.shop(sid).browse(tab, page)
                    catalog += r.get("items") or []
                    more = bool(r.get("more"))
                    page += 1
        except StoreError:
            catalog = []
        out, seen, covered = [], set(), set()
        for c in catalog:
            if self.is_owned(sid, c, acct) and c["id"] not in seen:
                seen.add(c["id"])
                covered.add(self._game_key(sid, c))
                out.append(dict(c, price=""))
        # Eigene Spiele, die der Katalog nicht (mehr) führt oder die jenseits der ersten Seiten liegen:
        # nur mit Namen (E99; die Liste kam schon mit dem Konto, wurde aber nie gezeigt)
        for game in acct.get("games") or []:
            card = self._fallback_card(sid, game)
            if card is not None and self._game_key(sid, card) not in covered and card["id"] not in seen:
                seen.add(card["id"])
                out.append(card)
        return self.annotate(sid, out)

    @staticmethod
    def _game_key(sid: str, card: dict):
        """Kennung, mit der das Konto ein Spiel nennt (Battle.net title_id, Ubisoft uplay_id)."""
        try:
            return int(card.get("title_id" if sid == "battlenet" else "uplay_id") or 0) or None
        except (TypeError, ValueError):
            return None

    def _fallback_card(self, sid: str, game: dict) -> dict | None:
        """Karte „nur mit Namen“ (id "owned:<Kennung>") für ein eigenes Spiel aus der Liste des Kontos;
        None, wenn sich daraus nichts installieren ließe. Rockstar braucht das nicht: Dort kommt der
        Besitz aus den Daten des Shops selbst."""
        gid, name = game.get("id"), str(game.get("name") or "").strip()
        if not gid or not name or sid == "rockstar":
            return None
        card = {"store": sid, "id": "owned:%s" % gid, "title": name, "slug": "", "image": "", "price": "",
                "discount": 0, "original": ""}
        if sid == "ubisoft":
            card["uplay_id"] = gid
            return card
        mod = self.modules["battlenet"][0]
        code = next((c for c, v in (getattr(mod, "CODES", {}) if mod else {}).items()
                     if isinstance(v, dict) and str(v.get("title_id") or "") == str(gid)), "")
        return dict(card, title_id=gid, code=code) if code else None

    def _owned_detail(self, sid: str, item_id: str) -> dict | None:
        """Seite eines Spiels „nur mit Namen“ (owned_cards): ohne Shop, aber installierbar."""
        if not item_id.startswith("owned:"):
            return None
        for game in self.account(sid).get("games") or []:
            card = self._fallback_card(sid, game)
            if card is not None and card["id"] == item_id:
                return {**card, "type": "game", "short": "", "about": "", "hero": "", "screenshots": [], "release": "",
                        "developers": [], "genres": [], "metacritic": 0, "controller": "", "deck": "", "protondb": "",
                        "linux": False, "windows": True, "installable": True, "coming_soon": False, "size_gb": 0.0,
                        "url": "", "buyable": False, "free": False}
        return None

    def _details(self, sid: str, params: dict) -> dict:
        """Shop-Seite eines Spiels; für Karten „nur mit Namen“ das, was das Konto weiß. Wirft StoreError."""
        item_id = str(params.get("id") or "")
        d = self._owned_detail(sid, item_id)
        if d is not None:
            return d
        return self.shop(sid).details(item_id, params.get("card") if isinstance(params.get("card"), dict) else None)

    # ------------------------------------------------------------ RPC des Stores
    def routes(self) -> dict:
        # store.search und store.account sind entfallen (E99): Die Suche läuft über store.list (E70)
        names = ("store.front", "store.list", "store.item", "store.install",
                 "store.cancel", "store.buy", "store.login", "store.uninstall")
        out: dict = {n: {} for n in names}
        for sid in STORE_IDS:
            out["store.front"][sid] = lambda p, s=sid: self.front(s, p)
            out["store.list"][sid] = lambda p, s=sid: self.list(s, p)
            out["store.item"][sid] = lambda p, s=sid: self.item(s, p)
            out["store.install"][sid] = lambda p, s=sid: self.install(s, p)
            out["store.cancel"][sid] = lambda p, s=sid: self.cancel(s, p)
            out["store.buy"][sid] = lambda p, s=sid: self.buy(s, p)
            out["store.login"][sid] = lambda p, s=sid: self.login(s, p)
            out["store.uninstall"][sid] = lambda p, s=sid: self.uninstall(s, p)
        return out

    def front(self, sid: str, params: dict) -> dict:
        acct = self.account(sid)
        return {"ok": True, "sections": [{"id": "owned", "title": "Meine Spiele", "items": self.owned_cards(sid)}],
                "loggedIn": acct["logged_in"], "genres": self.genres(sid), "tabs": self.tabs(sid),
                "launcher": {"name": NAMES[sid], "installed": self.launcher_ready(sid)}}

    def list(self, sid: str, params: dict) -> dict:
        try:
            page = self.shop(sid).browse(str(params.get("tab") or ""), int(params.get("page") or 0),
                                         str(params.get("genre") or ""), str(params.get("query") or ""))
        except (StoreError, ValueError) as e:
            return {"ok": False, "message": str(e), "items": [], "total": 0, "more": False}
        self.annotate(sid, page["items"])
        return {"ok": True, **page}

    def item(self, sid: str, params: dict) -> dict:
        try:
            d = self._details(sid, params)
        except StoreError as e:
            return {"ok": False, "message": str(e)}
        self.annotate(sid, [d])
        d["loggedIn"] = self.account(sid)["logged_in"]
        code = self.code_of(sid, d)
        if code is None:
            d["installable"] = False
            d["hint"] = "Dieses Produkt lässt sich nicht über %s installieren." % NAMES[sid]
        with self.lock:
            job = next((j for j in self.jobs if j["store"] == sid and str(j["code"]) == str(code)), None)
        d["job"] = self.job_view(job) if job else None
        d["launcher"] = {"name": NAMES[sid], "installed": self.launcher_ready(sid)}
        try:
            root = self.games_root()
            d["library"] = {"path": self.prefix(sid), "free_gb": round(shutil.disk_usage(root).free / 2**30)} \
                if os.path.isdir(root) else None
        except OSError:
            d["library"] = None
        if d["owned"] and not d["installed"] and not d.get("hint"):
            if launchers.hidden_install_args(sid, code):
                # E96: Rockstar lädt unsichtbar; ein Fenster kommt nur, wenn er dich braucht (E99: der
                # Hinweis versprach vorher einen Dialog, der nie kam)
                d["hint"] = ("Die Installation läuft über %s im Hintergrund: Du musst nichts bestätigen, den Stand "
                             "siehst du unter Downloads." % NAMES[sid])
            else:
                d["hint"] = ("Die Installation läuft über %s: Er öffnet sich gleich mit diesem Spiel, bestätige dort "
                             "mit dem Pad als Maus. Danach lädt er im Hintergrund." % NAMES[sid])
        return {"ok": True, "item": d}

    def buy(self, sid: str, params: dict) -> Any:
        item_id = str(params.get("id") or "")
        url = ""
        try:
            d = self.shop(sid).details(item_id, params.get("card") if isinstance(params.get("card"), dict) else None)
            url = d.get("url") or ""
            pid = d.get("title_id") if sid == "battlenet" else d.get("uplay_id") if sid == "ubisoft" else d.get("id")
        except StoreError:
            pid = None
        if not url:
            url = {"battlenet": "https://eu.shop.battle.net/de-de", "ubisoft": "https://store.ubisoft.com/de/home",
                   "rockstar": "https://store.rockstargames.com/de"}[sid]
        if pid:
            self.owned[sid]["at"] = 0.0
            threading.Thread(target=self.wiring.store_watch_purchase, args=(sid, pid, str(params.get("title") or "Das Spiel")),
                             name="couch-store-kauf", daemon=True).start()
        return self.wiring.store_open_url(url, bool(params.get("closeOther")))

    def owned_check(self, sid: str):
        def check(pid) -> bool:
            acct = self.account(sid, force=True)
            if sid == "rockstar":
                return str(pid) in acct["ids"]
            try:
                return int(pid) in acct["ids"]
            except (TypeError, ValueError):
                return False
        return check

    def login(self, sid: str, params: dict) -> Any:
        acct = self.modules[sid][1]
        if acct is None:
            return {"ok": False, "message": "Die Anmeldung bei %s ist gerade nicht verfügbar." % SHOP_NAMES[sid]}
        self.owned[sid]["at"] = 0.0
        self._login_at[sid] = time.monotonic()
        threading.Thread(target=self._after_login, args=(sid,), name="couch-store-anmeldung", daemon=True).start()
        url = acct.LOGIN_URL
        if sid == "ubisoft" and hasattr(acct, "login_url"):
            try:
                ids = self.shop("ubisoft").web_ids() or {}
                if ids.get("app_id") and ids.get("genome_id"):
                    url = acct.login_url(ids["app_id"], ids["genome_id"])
            except Exception as e:  # noqa: BLE001
                _log.warning("Ubisoft: Anmelde-IDs nicht lesbar: %s", e)
        return self.wiring.store_open_url(url, bool((params or {}).get("closeOther")), "%s-login" % sid)

    # ------------------------------------------------------------ Installieren
    def job_view(self, job: dict) -> dict:
        text = JOB_TEXT.get("hidden" if job.get("hidden") and job["state"] == "confirm" else job["state"], "")
        text = text % NAMES[job["store"]] if "%s" in text else text
        if job["state"] == "waiting" and job.get("waits_for") and job["waits_for"] != SOME_GAME:
            text = "Wartet, bis „%s“ beendet ist" % job["waits_for"]
        if job["state"] == "installing" and job.get("done_gb") and not job.get("percent"):
            text = "%s %s GB" % (text, ("%.1f" % job["done_gb"]).replace(".", ","))
        # Bytes für die Zeile unter dem Download (E99, overlay/panels.js): wie viel, wie schnell, wie lange noch
        done, total, speed = int(job.get("done_b") or 0), int(job.get("total_b") or 0), int(job.get("speed") or 0)
        eta = int((total - done) / speed) if speed > 0 and total > done else 0
        return {"store": job["store"], "id": job["id"], "title": job["title"], "percent": int(job.get("percent") or 0),
                "state": job["state"], "text": text, "hidden": bool(job.get("hidden")),
                "done": done, "total": total, "speed": speed, "eta": eta}

    @staticmethod
    def _note_bytes(job: dict, done: int, total: int) -> None:
        """Stand eines Downloads in Bytes (total 0 = unbekannt) und daraus die geglättete Geschwindigkeit;
        ein Wert über mindestens RATE_MIN_S, damit zwei Blicke kurz hintereinander nichts verfälschen."""
        now = time.monotonic()
        at, base = job.get("rate_at") or (None, 0)
        if at is None or done < base:
            job["rate_at"] = (now, done)
        elif now - at >= RATE_MIN_S:
            cur = (done - base) / (now - at)
            job["speed"] = cur if not job.get("speed") else 0.7 * job["speed"] + 0.3 * cur
            job["rate_at"] = (now, done)
        job["done_b"], job["total_b"] = int(done), int(total or 0)

    def publish(self) -> None:
        with self.lock:
            keep = [{k: j[k] for k in ("store", "id", "code", "title", "exe_hint")} for j in self.jobs if j.get("hidden")]
        self._save_jobs(keep)
        state = self.app.state

        def apply() -> None:
            # Beides erst in der Hauptschleife lesen (E99): Steam, GOG und Epic schreiben store_jobs dort;
            # ein hier im Thread gelesener Stand stellte deren schon erledigte Einträge wieder her
            # (Geister-Downloads), und Stände aus zwei Threads kamen in falscher Reihenfolge an
            with self.lock:
                mine = [self.job_view(j) for j in self.jobs]
            collect = getattr(self.wiring, "store_publish_jobs", None)
            if callable(collect):
                collect(STORE_IDS, mine)        # die gemeinsame Sammelstelle des Stores (wiring_store.publish_jobs)
                return
            others = [j for j in (state.value("store_jobs") or []) if j.get("store") not in STORE_IDS] \
                if hasattr(state, "value") else []
            state.update(store_jobs=others + mine)
        self.wiring.main.post(apply)

    def install(self, sid: str, params: dict) -> dict:
        item_id = str(params.get("id") or "")
        try:
            d = self._details(sid, params)
        except StoreError as e:
            return {"ok": False, "message": str(e)}
        code = self.code_of(sid, d)
        if code is None:
            return {"ok": False, "message": "Dieses Produkt lässt sich nicht über %s installieren." % NAMES[sid]}
        root = self.games_root()
        if not os.path.isdir(root):
            return {"ok": False, "message": "Die Spiele-Platte ist nicht eingehängt (%s)." % root}
        with self.lock:
            job = next((j for j in self.jobs if j["store"] == sid and str(j["code"]) == str(code)), None)
        if job is not None:
            # Knopf „Bestätige in …“ bzw. „… lädt“: den Launcher nach vorn holen (beim Bestätigen samt Dialog)
            if job["state"] in ("confirm", "installing") and self.launcher_ready(sid) and not job.get("hidden"):
                self.open_launcher(sid, launchers.install_args(sid, code)[1:] if job["state"] == "confirm" else [],
                                   why="install")
                return {"ok": True, "message": "%s ist vorn." % NAMES[sid]}
            if job["state"] == "waiting":
                return {"ok": True, "message": "„%s“ wird installiert, sobald %s beendet ist."
                                               % (d["title"], _game_text(job.get("waits_for") or SOME_GAME))}
            return {"ok": True, "message": "„%s“ wird schon installiert." % d["title"]}
        with self.lock:
            if any(j["store"] == sid and str(j["code"]) == str(code) for j in self.jobs):
                return {"ok": True, "message": "„%s“ wird schon installiert." % d["title"]}
            self.jobs.append({"store": sid, "id": item_id, "code": code, "title": d["title"], "state": "queued",
                              "percent": 0, "since": time.time(), "exe_hint": str(d.get("exe") or ""),
                              "cancel": threading.Event()})
        self.publish()
        self.wake.set()
        _log.info("Store: %s (%s %s) wird über den Launcher installiert", d["title"], sid, code)
        first = "" if self.launcher_ready(sid) else "%s wird zuerst eingerichtet (einmalig). " % NAMES[sid]
        if launchers.hidden_install_args(sid, code):
            # E96: unsichtbar (E99: die Meldung versprach vorher ein Fenster zum Bestätigen)
            text = ("%s%s lädt „%s“ im Hintergrund, du musst nichts tun. Den Stand siehst du unter Downloads."
                    % (first, NAMES[sid], d["title"]))
        else:
            text = ("%s%s öffnet sich gleich mit „%s“: bestätige dort die Installation. Danach lädt er im "
                    "Hintergrund, den Stand siehst du unter Downloads." % (first, NAMES[sid], d["title"]))
        game = self._game_in_prefix(sid)
        if game:
            text += " Los geht es, sobald %s beendet ist." % _game_text(game)
        return {"ok": True, "message": text}

    def cancel(self, sid: str, params: dict) -> dict:
        item_id = str(params.get("id") or "")
        with self.lock:
            job = next((j for j in self.jobs if j["store"] == sid and j["id"] == item_id), None)
            if job is None:
                return {"ok": False, "message": "Dieser Download läuft nicht."}
            job["cancel"].set()
            self.jobs.remove(job)
        self.publish()
        self._unmark(sid)
        if job.get("hidden"):
            self._stop_hidden_if_idle(sid)
            return {"ok": True, "message": "Abgebrochen. Was schon geladen ist, bleibt liegen; „Installieren“ lädt weiter."}
        if job["state"] == "installing":
            return {"ok": True, "message": "Die Konsole wartet nicht mehr auf „%s“. Den Download selbst brichst du in "
                                           "%s ab (Bibliothek › Optionen › %s)." % (job["title"], NAMES[sid], NAMES[sid])}
        return {"ok": True, "message": "Abgebrochen."}

    def uninstall(self, sid: str, params: dict) -> dict:
        """Deinstallieren geht nur im Launcher: Er öffnet sich, die Konsole räumt danach selbst auf."""
        if not self.launcher_ready(sid):
            return {"ok": False, "message": "%s ist nicht eingerichtet." % NAMES[sid]}
        entry_id = str(params.get("entry") or "")
        if entry_id and self.wiring.main.call(self.app.procs.by_entry, entry_id) is not None:
            return {"ok": False, "message": "Schließe das Spiel zuerst, dann lässt es sich deinstallieren."}
        refusal = self._busy_text(sid, app_start=True)
        if refusal:
            return {"ok": False, "message": refusal}
        self.open_launcher(sid, launchers.login_args(sid)[1:], why="uninstall")
        return {"ok": True, "message": "Deinstalliere „%s“ in %s (beim Spiel das Zahnrad › Deinstallieren). "
                                       "Die Konsole nimmt es danach von selbst aus der Bibliothek."
                                       % (params.get("title") or "das Spiel", NAMES[sid])}

    def open_launcher(self, sid: str, args: list, job: dict | None = None, why: str = "install") -> None:
        """Den Launcher sichtbar öffnen (Eintrag launcher:<id>): mit args für genau diesen Aufruf. job: der
        Auftrag, für den er sich öffnet; scheitert das Öffnen, fällt er weg (sonst stünde er 45 min da).
        why: wofür ("install", "uninstall"); entscheidet, ob make_room ihn später still schließt."""
        entry_id = "launcher:%s" % sid
        launchers.PENDING_ARGS[entry_id] = list(args)

        def failed(e: BaseException | None) -> None:
            # Die Argumente gelten nur für diesen Start; der Grund kommt als app.error bzw. Mitteilung
            launchers.PENDING_ARGS.pop(entry_id, None)
            with self.lock:
                self._opening.pop(sid, None)
            _log.warning("Launcher %s lässt sich nicht öffnen: %s", sid, getattr(e, "message", e))
            if job is not None and self._live(job):
                # im Thread: _fail nimmt die Marke von der Spiele-Platte, hier ist die Hauptschleife (E99)
                threading.Thread(target=lambda: self._fail(job, "", quiet=True), name="couch-launcher-auftrag",
                                 daemon=True).start()

        def done(fut) -> None:
            try:
                value = fut.result()
            except BaseException as e:  # noqa: BLE001 (LaunchError aus dem Arbeits-Thread der Shell)
                failed(e)
                return
            if not (isinstance(value, dict) and value.get("ok")):
                failed(None)                    # abgebrochen (Ladebild „Abbrechen“)

        def go(tries: int = 3) -> None:
            if self.app.library.get(entry_id) is None and tries:
                # gerade erst eingerichtet: Der Eintrag kommt mit dem nächsten Bibliothekslauf
                fut = self.app.library.rescan()
                fut.add_done_callback(lambda _f: self.wiring.main.post(lambda: go(tries - 1)))
                return
            running = self.app.procs.by_entry(entry_id)
            if running is not None and not getattr(running, "closing", False):
                # läuft schon: Der zweite Aufruf geht an das laufende Programm (Proton „runinprefix“). Die
                # Argumente holt dann kein Start ab (E99: sonst galten sie für das nächste Öffnen)
                launchers.PENDING_ARGS.pop(entry_id, None)
                if why != "install":
                    with self.lock:
                        self._shown_for[sid] = why  # vorsichtiger Zweck gewinnt (Deinstallieren läuft)
                self._send_to_running(sid, args)
                self.app.shell.focus_app(running)
                return
            # Ein Launcher, der gerade endet, startet über die Shell neu (sie wartet auf sein Ende)
            with self.lock:
                self._opening[sid] = why
            try:
                result = self.app.shell.launch(entry_id, close_other=True, confirmed=True)
            except Exception as e:  # noqa: BLE001
                failed(e)
                self.app.notify.push("%s lässt sich nicht öffnen" % NAMES[sid], getattr(e, "message", str(e)), icon="warn")
                return
            if hasattr(result, "add_done_callback"):
                # Fehler beim Vorbereiten (make_room, Proton) kommen erst hier an (E99: vorher blieben die
                # Argumente liegen und galten für den nächsten Start)
                result.add_done_callback(done)
        self.wiring.main.post(go)

    def _send_to_running(self, sid: str, args: list) -> None:
        """Aufruf an den laufenden Launcher (Proton „runinprefix“). Liest /proc und die Proton-Ordner, deshalb
        im eigenen Thread: Gerufen wird auch aus der Hauptschleife (open_launcher)."""
        if not args:
            return
        threading.Thread(target=self._send_now, args=(sid, list(args)), name="couch-launcher-aufruf",
                         daemon=True).start()

    def _send_now(self, sid: str, args: list) -> bool:
        """E101: mit genau der Proton-Fassung, mit der der Launcher im Prefix läuft, nicht mit der eines frischen
        Plans (der wechselt bei einem beschädigten Build oder nach einer Installation unter „Automatisch“). Zwei
        Fassungen auf einem Prefix haben am 2026-10-23 einen Build zerstört. Ist die laufende Fassung nicht
        erkennbar, geht nichts hinaus (Protokoll und Mitteilung). True = gesendet."""
        import subprocess
        from .launch.spec import LaunchError
        state, build, found = self._running_build(sid)
        if state == "unknown":
            _log.warning("Launcher %s: Proton im Prefix nicht erkennbar (%s); Aufruf nicht gesendet", sid,
                         _describe(found))
            self._tell_unknown(sid)
            return False
        try:
            env = self._proton_env(sid)
        except (LaunchError, launchers.LauncherError) as e:
            _log.warning("Launcher %s: Aufruf nicht gesendet: %s", sid, getattr(e, "message", e))
            self.wiring.main.post(lambda: self.app.notify.push(
                "%s nimmt den Auftrag gerade nicht an" % NAMES[sid], str(getattr(e, "message", "") or e), icon="warn"))
            return False
        if state == "known":
            planned = os.path.realpath(str(env.get("PROTONPATH") or ""))
            if planned != build:
                _log.info("Launcher %s läuft mit %s, der Plan nennt %s: Aufruf mit der laufenden Fassung", sid,
                          build, planned)
            env["PROTONPATH"] = build
        env["PROTON_VERB"] = "runinprefix"
        try:
            subprocess.Popen([_umu(), launchers.exe_path(self.prefix(sid), sid), *args], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError as e:
            _log.warning("Launcher %s: Aufruf gescheitert: %s", sid, e)
            return False
        return True

    def _tell_unknown(self, sid: str) -> None:
        self.wiring.main.post(lambda: self.app.notify.push(
            "%s nimmt den Auftrag gerade nicht an" % NAMES[sid],
            "Im Prefix läuft Wine mit einer Proton-Version, die die Konsole nicht erkennt. Schließe %s in der "
            "Übersicht und versuche es noch einmal." % NAMES[sid], icon="warn"))

    def _running_build(self, sid: str) -> tuple:
        """Womit im Prefix gerade Wine läuft (E101), als (Zustand, Proton-Ordner, Prozesse): "none" (nichts
        läuft), "known" (genau ein Proton-Ordner, den es hier gibt: der des Wineservers, ohne Wineserver der
        übrigen Prozesse), "unknown" (nicht erkennbar, mehrere, oder ein Pfad, wie ihn nur ein Container
        sieht). Liest /proc, blockierend."""
        from .launch import prefix as prefixes
        found = prefixes.wine_processes(self.prefix(sid), self.proc_root)
        if not found:
            return "none", "", []
        servers = [x for x in found if x["wineserver"]]
        pool = servers or found
        builds = {os.path.realpath(x["build"]) for x in pool if x["build"]}
        if len(builds) != 1 or any(not x["build"] for x in servers):
            return "unknown", "", found
        build = builds.pop()
        if not os.path.isfile(os.path.join(build, "proton")):
            return "unknown", "", found
        return "known", build, found

    def _bare_env(self, sid: str) -> dict:
        """Umgebung nur für „wineserver -k“: Sitzungsreste weg, WINEPREFIX dieses Launchers. Ohne Plan, damit
        das Beenden weder an einem beschädigten Build scheitert noch eine Reparatur anstößt."""
        from .launch.spec import strip_env
        from .proc.manager import SCRUB
        env = strip_env(dict(os.environ), SCRUB)
        env["WINEPREFIX"] = self.prefix(sid)
        return env

    def _proton_env(self, sid: str) -> dict:
        from .launch import proton
        ctx = getattr(self.wiring, "launch", None)
        if ctx is None:
            raise launchers.LauncherError("Proton ist gerade nicht bereit. Versuche es gleich noch einmal.")
        temp = self.launcher_entry(sid)
        spec = proton.build(temp, ctx, check=False)
        # Wie der AppManager (E99): Muster in spec.unset ("STEAM_COMPAT_*") gelten, und was nur das Home
        # braucht (SCRUB: DISABLE_GAMESCOPE_WSI, GSK_RENDERER, COUCH_LOG_FILE …), erbt der Launcher nicht
        from .launch.spec import strip_env
        from .proc.manager import SCRUB
        env = strip_env(dict(os.environ), SCRUB + tuple(spec.unset or ()))
        for key, value in (spec.env or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        try:
            display = self.app.gs.game_display()
        except Exception:  # noqa: BLE001
            display = None
        if display:
            env["DISPLAY"] = display
        return env

    # ------------------------------------------------------------ Platz im Prefix (E99)
    def make_room(self, sid: str, entry_id: str, title: str = "") -> bool:
        """Vor einem Start im Prefix dieses Launchers (launchers.before_start, Arbeits-Thread der Shell).
        Vorher scheiterte jeder Start, solange dort irgendetwas lief (Wineserver-Prüfung), auch die
        Kachel eines frisch installierten Spiels, wenn der Launcher noch offen war. Jetzt:
          * lädt der Launcher für einen Auftrag, Ablehnung mit Satz (reason "launcher-busy"); schließen
            bräche den Download ab;
          * ein sichtbarer Launcher (App launcher:<id>), den die Konsole für eine inzwischen fertige
            Installation geöffnet hat, geht zu; einen, den der Nutzer selbst oder zum Deinstallieren geöffnet
            hat, schließt der Nutzer selbst (Ablehnung mit Satz: die Konsole weiß nicht, was er gerade tut);
          * ein unsichtbarer Launcher ohne Auftrag endet;
          * Apps, die die Konsole gerade beendet, werden abgewartet (die Shell wartet nicht auf sie);
          * ein Spiel startet hier nie in einen Launcher, der nicht zum Baum seiner App gehört (falsche
            App-Nummer, kein Anhalten und Beenden).
        True: Den Prefix hält ein anderes Spiel der Konsole; die Shell schließt es vor dem Start (Rückfrage
        „… schließen?“), dessen Wineserver ist also kein Rest. Wirft LaunchError, auch mit der
        Wineserver-Meldung des Starters (dann wartet kein Auftrag auf einen Start, der nicht kommt)."""
        from .launch.prefix import wineserver_pids
        from .launch.spec import LaunchError
        if sid not in STORE_IDS:
            return False
        own_app = "launcher:%s" % sid
        app_start = entry_id == own_app
        # Läuft im Prefix überhaupt etwas? Vor der Sperre (Hauptschleife, /proc): Ein sichtbarer Auftrag,
        # dessen Launcher zu ist, lädt nicht (E99: der Satz sagte sonst „wird gerade installiert“)
        alive = app_start or bool(wineserver_pids(self.prefix(sid), self.proc_root)) or self._prefix_in_use(sid)
        with self.lock:
            if app_start:
                self._shown_for[sid] = self._opening.pop(sid, "")   # "" = der Nutzer öffnet ihn selbst
            refusal = self._busy_text(sid, app_start, alive)
            if not refusal and not app_start:
                # Aufträge warten ab jetzt, bis das Spiel beendet ist (_begin prüft unter derselben Sperre,
                # damit nicht beide zugleich den Prefix nehmen)
                self._starting[sid] = (time.monotonic(), title or SOME_GAME, entry_id)
        if refusal:
            raise LaunchError(refusal, "Auftrag im Prefix %s" % self.prefix(sid), reason="launcher-busy")
        try:
            held = self._clear_prefix(sid, entry_id, app_start)
        except Exception:
            with self.lock:
                self._starting.pop(sid, None)   # der Start kommt nicht: Aufträge müssen nicht warten
            raise
        if not app_start:
            with self.lock:
                # STARTING_S ab jetzt: Schließen und Warten eben dauerten bis zu CLOSE_WAIT_S + ROOM_WAIT_S
                self._starting[sid] = (time.monotonic(), title or SOME_GAME, entry_id)
        return held

    def _clear_prefix(self, sid: str, entry_id: str, app_start: bool) -> bool:
        """Teil von make_room: andere Apps im Prefix, sichtbarer und unsichtbarer Launcher."""
        from .launch import prefix as prefixes
        from .launch.proton import TEXT_FOREIGN_WINE, TEXT_WINESERVER
        from .launch.spec import LaunchError
        own_app = "launcher:%s" % sid
        users = self._users(sid)
        # Was die Konsole schon beendet, hält den Prefix nicht mehr, ist aber noch nicht weg: abwarten.
        # Die Shell wartet nicht darauf (sie schließt nur, was nicht schon endet); mit True fiele zudem die
        # Wineserver-Prüfung weg, und ein schneller Wechsel zwischen zwei Spielen desselben Launchers
        # startete in den sterbenden Prefix (E99)
        leaving = [a for a in users if getattr(a, "closing", False)]
        users = [a for a in users if a not in leaving]
        others = [a for a in users if a.entry_id not in (own_app, entry_id)]
        if others and app_start:
            raise LaunchError("Schließe zuerst „%s“: Es läuft über %s." % (self._app_title(others[0]), NAMES[sid]),
                              reason="launcher-busy")
        shown = next((a for a in users if a.entry_id == own_app), None)
        if shown is not None and not app_start:
            refusal = self._shown_text(sid, shown)
            if refusal:
                raise LaunchError(refusal, "Launcher %s offen" % sid, reason="launcher-busy")
            _log.info("Launcher %s: sichtbar nach einer fertigen Installation, geht für den Start von %s zu",
                      sid, entry_id)
            self.wiring.main.call(self.app.shell.close_app, shown)
            leaving.append(shown)
        until = time.monotonic() + CLOSE_WAIT_S
        for app in leaving:
            while self.wiring.main.call(self.app.procs.by_app_id, app.app_id) is not None:
                if time.monotonic() > until:
                    raise LaunchError("„%s“ ließ sich nicht beenden." % self._app_title(app), reason="close-failed")
                time.sleep(0.2)
        if others:
            return True                         # wineserver -k träfe das andere Spiel; die Shell schließt es
        with self.lock:
            proc = self.hidden.pop(sid, None)
            adopted = sid in self.adopted
            self.adopted.discard(sid)
        if (proc is not None and proc.poll() is None) or adopted:
            _log.info("Launcher %s: unsichtbar ohne Auftrag, endet für den Start von %s", sid, entry_id)
            self._kill_prefix(sid, proc)
        # E101: nicht nur auf den Wineserver warten, sondern auf alles, was im Prefix arbeitet (umu-run, Protons
        # Skript, auch Fremdes): Der Starter prüft danach genauso und hätte sonst nur 3 s Geduld
        until = time.monotonic() + ROOM_WAIT_S
        while True:
            found = prefixes.wine_processes(self.prefix(sid), self.proc_root)
            if not found:
                return False
            if time.monotonic() > until:
                # dieselbe Meldung wie beim Starter, aber hier: make_room nimmt den Start dann zurück
                detail = "Wine-Prozesse in %s: %s" % (self.prefix(sid), _describe(found))
                foreign = any(x["own"] is False for x in found)
                raise LaunchError(TEXT_FOREIGN_WINE if foreign else TEXT_WINESERVER, detail, reason="wineserver")
            time.sleep(0.2)

    def _shown_text(self, sid: str, shown: Any) -> str:
        """Satz, wenn der sichtbare Launcher für einen Spielstart nicht still zugehen darf, sonst ""."""
        tail = "Schließe ihn in der Übersicht, dann starte das Spiel noch einmal."
        if getattr(shown, "adopted", False):
            # lief schon vor dem Neustart des Home: Ob er noch lädt, weiß die Konsole nicht
            return "%s lief schon vor dem Neustart des PC5 Launchers und lädt vielleicht noch. %s" % (NAMES[sid], tail)
        with self.lock:
            why = self._shown_for.get(sid, "")
        if why == "install":
            return ""                           # für eine eigene Installation geöffnet, die ist fertig
        if why == "uninstall":
            return "%s ist zum Deinstallieren offen. Wenn er damit fertig ist: %s" % (NAMES[sid], tail)
        # selbst geöffnet: der Nutzer installiert dort vielleicht etwas, patcht oder meldet sich an (E94)
        return "%s ist offen und lädt vielleicht gerade etwas. %s" % (NAMES[sid], tail)

    def _busy_text(self, sid: str, app_start: bool, alive: bool = True) -> str:
        """Satz, wenn ein Start im Prefix jetzt einen Download abbräche, sonst "". Ein Spiel: jeder
        laufende Auftrag dieses Launchers. Der Launcher selbst (Anmelden, Deinstallieren): nur, solange
        er unsichtbar lädt; sichtbar nimmt er den Aufruf selbst an. alive: Im Prefix läuft etwas."""
        with self.lock:
            busy = [j for j in self.jobs if j["store"] == sid and j["state"] not in NOT_STARTED]
            hidden = self._hidden_running(sid) or sid in self.adopted
        if not busy or (app_start and not hidden):
            return ""
        if app_start:
            return ("%s lädt gerade „%s“ im Hintergrund. Das geht, wenn der Download fertig ist (Stand unter "
                    "Downloads)." % (NAMES[sid], busy[0]["title"]))
        if not alive and all(j["state"] in ("confirm", "installing") and not j.get("hidden")
                             and time.time() - j.get("since", 0) > OPEN_GRACE_S for j in busy):
            # sichtbarer Auftrag, Launcher zu (in der Übersicht geschlossen): Er lädt gerade nicht (E99)
            title = busy[0]["title"]
            return ("„%s“ wartet auf %s, der gerade geschlossen ist. Öffne „%s“ im Store, damit er weiterlädt, "
                    "oder brich es unter Downloads ab. Dann starte das Spiel noch einmal." % (title, NAMES[sid], title))
        return ("Über %s wird gerade „%s“ installiert. Starte das Spiel, wenn das fertig ist, oder brich den "
                "Download unter Downloads ab." % (NAMES[sid], busy[0]["title"]))

    @staticmethod
    def _app_title(app: Any) -> str:
        return str(getattr(app.spec, "title", "") or app.entry_id)

    def _users(self, sid: str) -> list:
        """Apps der Konsole, die gerade in diesem Prefix laufen (der sichtbare Launcher, Spiele darüber)."""
        prefix = os.path.realpath(self.prefix(sid))

        def apps() -> list:
            return [a for a in self.app.procs.running()
                    if os.path.realpath(str((getattr(a.spec, "env", None) or {}).get("WINEPREFIX") or "")) == prefix]
        return list(self.wiring.main.call(apps) or [])

    def _game_in_prefix(self, sid: str) -> str:
        """Titel des Spiels, das gerade über diesen Launcher läuft oder startet, sonst "". Ein Auftrag
        wartet dann (E99): Sonst ginge er per runinprefix in einen Launcher unter dem reaper des Spiels,
        und dessen App endete nicht mehr mit dem Spiel."""
        title = self._starting_title(sid)
        if title and not self._start_dropped(sid):
            return title
        try:
            # auch ein Spiel, das gerade endet: Sein sterbender Prefix nähme den Auftrag sonst mit
            users = [a for a in self._users(sid) if a.entry_id != "launcher:%s" % sid]
        except Exception:  # noqa: BLE001 (Hauptschleife zu: Ende der Konsole, lieber warten)
            return SOME_GAME
        return self._app_title(users[0]) if users else ""

    def _start_dropped(self, sid: str) -> bool:
        """Kam der Start, für den make_room Platz gemacht hat, nicht zustande (im Ladebild abgebrochen,
        Rückfrage verneint, Fehler danach)? Dann wartet kein Auftrag mehr auf ihn (E99). Die Shell meldet
        den Eintrag über is_preparing, bis seine App läuft (wie in wiring_install.is_running); ohne
        diese Angabe gilt nur die Frist STARTING_S."""
        with self.lock:
            got = self._starting.get(sid)
        if not got:
            return False
        entry_id = got[2]

        def pending() -> bool:
            is_preparing = getattr(getattr(self.app, "shell", None), "is_preparing", None)
            if is_preparing is None:
                return True
            return bool(is_preparing(entry_id)) or self.app.procs.by_entry(entry_id) is not None
        try:
            if self.wiring.main.call(pending):
                return False
        except Exception:  # noqa: BLE001 (Hauptschleife zu: lieber warten)
            return False
        with self.lock:
            if self._starting.get(sid) is got:
                self._starting.pop(sid, None)
        return True

    def epic_tool(self) -> str:
        """launch/proton.EPIC_TOOL: legendary bereitstellen (lädt es beim ersten Mal, Arbeits-Thread).
        Ohne den Epic-Teil des Stores prüft der Starter nichts nach."""
        legendary = getattr(self.wiring, "epic_legendary", None)
        return str(legendary.ensure()) if legendary is not None else ""

    def _starting_title(self, sid: str) -> str:
        """Titel des Spiels, für das make_room gerade Platz gemacht hat (bis seine App da ist), sonst ""."""
        with self.lock:
            at, title = self._starting.get(sid, (None, "", ""))[:2]
            if at is not None and time.monotonic() - at < STARTING_S:
                return title
            self._starting.pop(sid, None)
            return ""

    def _live(self, job: dict) -> bool:
        """Steht der Auftrag noch an (nicht abgebrochen, nicht entfernt)? Abläufe mit älteren Listen
        prüfen das vor jedem Schritt (E99: sonst lud ein abgebrochenes Spiel unsichtbar weiter)."""
        with self.lock:
            return job in self.jobs and not job["cancel"].is_set()

    def _mark(self, sid: str) -> None:
        try:
            with open(os.path.join(self.prefix(sid), MARK), "w") as fh:
                fh.write("1\n")
        except OSError:
            pass

    def _unmark(self, sid: str) -> None:
        with self.lock:
            busy = any(j["store"] == sid for j in self.jobs)
        if not busy:
            try:
                os.unlink(os.path.join(self.prefix(sid), MARK))
            except OSError:
                pass

    # ------------------------------------------------------------ Launcher einrichten
    def setup_launcher(self, sid: str, job: dict) -> None:
        """Lädt den Launcher und installiert ihn still in seinen Prefix (blockierend, Arbeits-Thread)."""
        import subprocess
        prefix = self.prefix(sid)
        os.makedirs(prefix, exist_ok=True)

        def progress(got, total):
            job["percent"] = int(got * 100 / total) if total else 0
            self.publish()
        installer = launchers.download_installer(sid, paths.cache_dir(), progress, job["cancel"])
        job["percent"] = 0
        self.publish()
        from .launch import proton
        from .launch.spec import LaunchError
        try:
            env = self._proton_env(sid)
            # E101: nie in einen Prefix, in dem schon Wine arbeitet (auch fremdes)
            proton.prefix_free(prefix, str(env.get("PROTONPATH") or ""), self.proc_root)
        except LaunchError as e:
            raise launchers.LauncherError(e.message) from e
        argv = [_umu(), installer, *launchers.LAUNCHERS[sid].installer_args]
        _log.info("Launcher %s wird installiert: %s", sid, " ".join(argv))
        with open(log.capped(paths.app_log("launcher-%s" % sid)), "a") as log_fh:
            proc = subprocess.Popen(argv, env=env, cwd=os.path.dirname(installer), stdin=subprocess.DEVNULL,
                                    stdout=log_fh, stderr=log_fh, start_new_session=True)
            until = time.monotonic() + 20 * 60
            # Fertig ist er, sobald seine Programmdatei da ist; manche Installer starten ihn danach
            # selbst und enden nicht (Battle.net zeigt dann die Anmeldung): dann wird der Prefix beendet
            while not launchers.installed(prefix, sid):
                if job["cancel"].is_set() or self.stopped.is_set():
                    self._kill_prefix(sid, proc)
                    raise launchers.LauncherError("Abgebrochen.")
                if time.monotonic() > until:
                    self._kill_prefix(sid, proc)
                    raise launchers.LauncherError("%s ließ sich nicht einrichten (keine Rückmeldung nach 20 Minuten)." % NAMES[sid])
                if proc.poll() is not None and not launchers.installed(prefix, sid):
                    time.sleep(10)          # manche Installer starten einen zweiten Schritt und enden
                    if not launchers.installed(prefix, sid) and proc.poll() is not None:
                        raise launchers.LauncherError("Der Installer von %s ist nicht durchgelaufen (Rückgabewert %s)."
                                                      % (NAMES[sid], proc.returncode))
                time.sleep(2)
            # Die Programmdatei ist da, aber der Installer packt vielleicht noch aus (die Spiele-Platte ist
            # langsamer als ein Test im Speicher): fertig, wenn er endet oder sich im Ordner des Launchers
            # 10 s lang nichts mehr ändert (manche starten den Launcher danach selbst und enden nie)
            folder = os.path.dirname(launchers.exe_path(prefix, sid))
            last, calm, settle_until = None, 0, time.monotonic() + 10 * 60
            while proc.poll() is None and calm < 5 and time.monotonic() < settle_until:
                if job["cancel"].is_set() or self.stopped.is_set():
                    self._kill_prefix(sid, proc)
                    raise launchers.LauncherError("Abgebrochen.")
                size = launchers.tree_size(folder)
                calm = calm + 1 if size == last else 0
                last = size
                time.sleep(2)
            self._kill_prefix(sid, proc)
        launchers.german(prefix, sid)
        self.route_urls(sid)
        _log.info("Launcher %s eingerichtet in %s", sid, prefix)
        self.wiring.main.post(lambda: self.app.library.rescan())

    def _kill_prefix(self, sid: str, proc=None) -> None:
        """Den Prefix des Launchers beenden: eigene Prozessgruppe, dann „wineserver -k“ mit der Fassung des
        laufenden Wineservers (E101: der eines frischen Plans scheitert am Protokoll, wenn der Plan inzwischen
        ausweicht; dann bliebe der Prefix belegt und jeder Start dort endete mit TEXT_WINESERVER)."""
        import signal
        import subprocess
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            state, build, _found = self._running_build(sid)
            if state == "none":
                return                          # nichts mehr im Prefix
            if state == "known":
                env = self._bare_env(sid)
                env["PROTONPATH"] = build
            else:
                env = self._proton_env(sid)     # nicht erkennbar: wie bisher die Fassung des Plans
            server = os.path.join(env.get("PROTONPATH", ""), "files", "bin", "wineserver")
            if os.path.isfile(server):
                subprocess.run([server, "-k"], env=env, timeout=30, check=False)
        except Exception as e:  # noqa: BLE001
            _log.warning("Launcher %s: Prefix nicht beendet: %s", sid, e)

    # ------------------------------------------------------------ Bibliothek: Launcher als verborgene Apps
    def launcher_entry(self, sid: str) -> Entry:
        prefix = self.prefix(sid)
        exe = launchers.exe_path(prefix, sid)
        return Entry(id="launcher:%s" % sid, title=NAMES[sid], kind="app", source="local", hidden=True,
                     installed=True, available=os.path.isfile(exe),
                     detail={"platform": "windows", "launcher": sid, "launcher_app": True, "launcher_prefix": prefix,
                             "store": launchers.LAUNCHERS[sid].store, "install_dir": os.path.dirname(exe),
                             "exe": os.path.basename(exe)})

    def library_source(self) -> list[Entry]:
        """Quelle „launchers“: eingerichtete Launcher als verborgene Einträge (Anmeldung, Dialoge)."""
        try:
            return [self.launcher_entry(sid) for sid in STORE_IDS if self.launcher_ready(sid)]
        except Exception:  # noqa: BLE001
            _log.exception("Launcher-Einträge")
            return []

    # ------------------------------------------------------------ Abgleich mit dem Launcher
    def codes_map(self, sid: str) -> dict:
        if sid != "battlenet":
            return {}
        mod = self.modules["battlenet"][0]
        table = getattr(mod, "CODES", {}) if mod else {}
        return {str(v.get("agent_uid")): code for code, v in table.items() if isinstance(v, dict) and v.get("agent_uid")}

    def title_for(self, sid: str, code, fallback: str) -> str:
        mod = self.modules[sid][0]
        table = (getattr(mod, "CODES", None) if sid == "battlenet" else getattr(mod, "TITLES", None)) if mod else None
        info = table.get(code) if isinstance(table, dict) else None
        return str((info or {}).get("title") or fallback or code)

    def exe_hint(self, sid: str, code) -> str:
        mod = self.modules[sid][0]
        if sid == "rockstar" and mod is not None:
            return str((getattr(mod, "TITLES", {}).get(code) or {}).get("exe") or "")
        return ""

    def sync(self, sid: str) -> None:
        """Was im Launcher installiert ist ↔ Einträge der Bibliothek; Aufträge abschließen."""
        prefix = self.prefix(sid)
        if not os.path.isdir(prefix):
            return
        self.route_urls(sid)
        found = launchers.installed_games(prefix, sid, self.codes_map(sid))
        known = self.known()
        mine = dict(known.get(sid) or {})
        changed = False
        with self.lock:
            jobs = [j for j in self.jobs if j["store"] == sid]
        for game in found:
            code = game["code"]
            job = next((j for j in jobs if str(j["code"]) == str(code)), None)
            exe = launchers.main_exe(game["folder"], self.exe_hint(sid, code) or (job or {}).get("exe_hint", ""))
            complete = game["complete"] if game["complete"] is not None else bool(exe)
            if job is not None and job["state"] in ("confirm", "launcher", "queued"):
                job["state"] = "installing"
                self.publish()
            if not complete or not exe:
                continue
            if str(code) not in mine:
                title = self.title_for(sid, code, (job or {}).get("title") or game["title"])
                entry_id = entry_id_for(sid, code)
                record = {"id": entry_id, "title": title, "platform": "windows", "install_dir": game["folder"],
                          "exe": exe, "args": "", "archive": SHOP_NAMES[sid], "marker": "", "created": time.time(),
                          "launcher": sid, "launcher_code": code, "folder_win": game["folder_win"],
                          "launcher_prefix": prefix}
                try:
                    from . import wiring_library
                    added = wiring_library.add_local_record(self.app, record)
                except Exception:  # noqa: BLE001
                    _log.exception("Launcher-Spiel %s eintragen", title)
                    continue
                if added:
                    mine[str(code)] = added
                    changed = True
                    _log.info("Launcher %s: %s ist installiert (%s)", sid, title, game["folder"])
                    self.wiring.main.post(lambda t=title, s=sid: self.app.notify.push(
                        "„%s“ ist installiert" % t, "Gestartet wird über %s, ohne dass du ihn siehst." % NAMES[s], icon="check"))
            if job is not None:
                with self.lock:
                    if job in self.jobs:
                        self.jobs.remove(job)
                self.publish()
                self._unmark(sid)
                self._close_if_idle(sid)
        # Im Launcher deinstalliert: Eintrag weg
        present = {str(g["code"]) for g in found}
        for code, entry_id in list(mine.items()):
            if code not in present:
                mine.pop(code)
                changed = True
                _log.info("Launcher %s: %s ist nicht mehr installiert", sid, code)
                from . import wiring_library
                self.wiring.main.post(lambda e=entry_id: wiring_library.remove_local(self.app, e))
        if changed:
            known[sid] = mine
            self._write_known(known)

    def _close_if_idle(self, sid: str) -> None:
        """Nichts lädt mehr über diesen Launcher: die sichtbare Sitzung beenden, wenn sie hinten liegt."""
        with self.lock:
            if any(j["store"] == sid for j in self.jobs):
                return

        def go() -> None:
            running = self.app.procs.by_entry("launcher:%s" % sid)
            if running is not None and self.app.focus.foreground != running.app_id:
                self.app.shell.close_app(running)
        self.wiring.main.post(go)
        self._stop_hidden_if_idle(sid)

    # ------------------------------------------------------------ Unsichtbar installieren (E96)
    def _hidden_running(self, sid: str) -> bool:
        proc = self.hidden.get(sid)
        return proc is not None and proc.poll() is None

    def _start_hidden(self, sid: str, args: list) -> None:
        """Der Launcher als Hintergrundprozess der Konsole: ohne App-Nummer zeigt Gamescope seine Fenster nie.
        Läuft im Prefix schon etwas (der unsichtbare Launcher, auch der von vor einem Neustart), geht der
        Auftrag dorthin, mit dessen Proton-Fassung (E101). Sonst nur in einen freien Prefix (prefix_free).
        Solange ein Spiel darüber läuft, kommt _begin nicht hierher (E99: der Auftrag wartet)."""
        import subprocess
        from .launch import proton
        from .launch.prefix import wineserver_pids
        from .launch.spec import LaunchError
        try:
            env = self._proton_env(sid)
        except LaunchError as e:                # z. B. der einzige Build ist beschädigt (proton-damaged)
            raise launchers.LauncherError(e.message) from e
        exe = launchers.exe_path(self.prefix(sid), sid)
        own = self._hidden_running(sid)
        busy = own or bool(wineserver_pids(self.prefix(sid), self.proc_root))
        if busy:
            state, build, found = self._running_build(sid)
            if state == "unknown":
                _log.warning("Launcher %s: Proton im Prefix nicht erkennbar (%s)", sid, _describe(found))
                raise launchers.LauncherError("Im Prefix von %s läuft Wine mit einer Proton-Version, die die Konsole "
                                              "nicht erkennt. Versuche es noch einmal, wenn es beendet ist." % NAMES[sid])
            if state == "known":
                env["PROTONPATH"] = build
            env["PROTON_VERB"] = "runinprefix"
        else:
            try:
                proton.prefix_free(self.prefix(sid), str(env.get("PROTONPATH") or ""), self.proc_root)
            except LaunchError as e:
                raise launchers.LauncherError(e.message) from e
        _log.info("Launcher %s unsichtbar: %s", sid, " ".join(args))
        with open(log.capped(paths.app_log("launcher-%s-hidden" % sid)), "a") as log_fh:
            proc = subprocess.Popen([_umu(), exe, *args], env=env, cwd=os.path.dirname(exe), stdin=subprocess.DEVNULL,
                                    stdout=log_fh, stderr=log_fh, start_new_session=True)
        adopt = busy and not own and not self._prefix_in_use(sid)
        with self.lock:
            if not busy:
                self.hidden[sid] = proc
            elif adopt:
                # Läuft schon, ohne eigenen Popen und ohne App der Konsole: der unsichtbare Launcher von vor
                # dem Neustart (start_new_session überlebt ihn). Er endet wie ein eigener (E96, E99)
                self.adopted.add(sid)
        if adopt:
            _log.info("Launcher %s: läuft schon von vorher, der Auftrag geht an ihn", sid)

    def _prefix_in_use(self, sid: str) -> bool:
        """Läuft eine App der Konsole in diesem Prefix (der sichtbare Launcher, ein Spiel darüber)?"""
        try:
            return bool(self._users(sid))
        except Exception:  # noqa: BLE001 (Hauptschleife zu: Ende der Konsole)
            return True

    def _stop_hidden_if_idle(self, sid: str) -> None:
        from .launch.prefix import wineserver_pids
        with self.lock:
            if any(j["store"] == sid for j in self.jobs):
                return
            proc = self.hidden.pop(sid, None)
            adopted = sid in self.adopted
            self.adopted.discard(sid)
        if adopted:
            # übernommen: kein Popen; nur, wenn im Prefix noch Wine läuft (E99: lief sonst bis Sitzungsende)
            if not wineserver_pids(self.prefix(sid), self.proc_root):
                return
        elif proc is None or proc.poll() is not None:
            return
        if self._prefix_in_use(sid):
            return                              # ein Spiel läuft darüber: es beendet den Prefix selbst
        _log.info("Launcher %s: nichts lädt mehr, der unsichtbare Launcher endet", sid)
        threading.Thread(target=self._kill_prefix, args=(sid, proc), name="couch-launcher-ende", daemon=True).start()

    def _reveal(self, job: dict, why: str) -> None:
        """Unsichtbar ging es nicht weiter (Anmeldung, Bestätigung, Captcha): den Launcher zeigen."""
        if not self._live(job):
            return                              # inzwischen abgebrochen (E99: öffnete sonst den Dialog dafür)
        sid = job["store"]
        _log.info("Launcher %s: %s, wird sichtbar", sid, why)
        with self.lock:
            proc = self.hidden.pop(sid, None)
            adopted = sid in self.adopted
            self.adopted.discard(sid)
        if ((proc is not None and proc.poll() is None) or adopted) and not self._prefix_in_use(sid):
            self._kill_prefix(sid, proc)        # sonst gäbe der sichtbare Start den Auftrag nur an ihn weiter
        job.update(hidden=False, since=time.time())
        self.publish()
        self.wiring.main.post(lambda: self.app.notify.push(
            "%s braucht dich einmal" % NAMES[sid], "Melde dich dort an oder bestätige die Installation von „%s“." % job["title"],
            icon="info"))
        self.open_launcher(sid, (launchers.hidden_install_args(sid, job["code"]) or launchers.install_args(sid, job["code"]))[1:],
                           job)

    def _save_jobs(self, jobs: list) -> None:
        """Unsichtbare Aufträge überleben einen Neustart des PC5 Launchers: Danach lädt er weiter.
        Unter eigener Sperre: publish kommt aus mehreren Threads, alle schreiben dieselbe .neu-Datei."""
        path = os.path.join(paths.state_dir(), "launcher-jobs.json")
        with self._save_lock:
            if self._saved_jobs == jobs:
                return
            self._saved_jobs = jobs
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".neu", "w", encoding="utf-8") as fh:
                    json.dump(jobs, fh)
                os.replace(path + ".neu", path)
            except OSError as e:
                _log.warning("launcher-jobs.json: %s", e)

    def _resume(self) -> None:
        """Beim Start: gemerkte unsichtbare Aufträge wieder aufnehmen (der Launcher setzt den Download fort)."""
        try:
            with open(os.path.join(paths.state_dir(), "launcher-jobs.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return
        known = self.known()
        for raw in saved if isinstance(saved, list) else []:
            if not isinstance(raw, dict) or raw.get("store") not in STORE_IDS or not raw.get("code"):
                continue
            if str(raw["code"]) in (known.get(raw["store"]) or {}):
                continue                        # inzwischen fertig
            with self.lock:
                if any(j["store"] == raw["store"] and str(j["code"]) == str(raw["code"]) for j in self.jobs):
                    continue
                self.jobs.append({"store": raw["store"], "id": str(raw.get("id") or ""), "code": raw["code"],
                                  "title": str(raw.get("title") or ""), "state": "queued", "percent": 0, "since": time.time(),
                                  "exe_hint": str(raw.get("exe_hint") or ""), "cancel": threading.Event()})
            _log.info("Launcher %s: %s lädt nach dem Neustart weiter", raw["store"], raw.get("title"))
        self.publish()
        self.wake.set()

    def _clear_stale_marks(self) -> None:
        """Beim Start: Die Marke eines Prefix ohne wieder aufgenommenen Auftrag stammt aus der Zeit vor dem
        Neustart (sichtbare Aufträge werden nicht gemerkt). Sie hielte sonst für immer einen fremd
        laufenden Launcher nach jedem Spiel am Leben (E99, bin/couch-launcher-game)."""
        for sid in STORE_IDS:
            if os.path.exists(os.path.join(self.prefix(sid), MARK)):
                self._unmark(sid)

    # ------------------------------------------------------------ Arbeits-Thread
    def start(self) -> None:
        if self.thread is None:
            self._resume()
            self.thread = threading.Thread(target=self._run, name="couch-launchers", daemon=True)
            self.thread.start()
        self.wiring.main.post(self._watch_urls)

    # ------------------------------------------------------------ Links aus den Launchern
    def _watch_urls(self) -> None:
        """bin/couch-open-url legt Adressen als Datei ab (Wine im Container erreicht das Home sonst nicht)."""
        from gi.repository import Gio
        folder = paths.open_url_dir()
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            self._url_monitor = Gio.File.new_for_path(folder).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._url_monitor.connect("changed", lambda *_a: self.take_urls())
        except Exception as e:  # noqa: BLE001 (GLib.Error, OSError)
            _log.warning("Links aus den Launchern: kein Ordner %s: %s", folder, e)
        self.take_urls()

    def take_urls(self) -> None:
        """Abgelegte Adressen im Browser zeigen (Anmeldung mit Google, Apple … im Launcher). Ältere als
        2 Minuten fallen weg (abgelegt, während das Home nicht lief)."""
        folder = paths.open_url_dir()
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".url"))
        except OSError:
            return
        for name in names:
            path = os.path.join(folder, name)
            try:
                fresh = time.time() - os.path.getmtime(path) < URL_MAX_AGE_S
                with open(path, encoding="utf-8", errors="replace") as fh:
                    url = fh.read(MAX_URL + 1).strip()
                os.unlink(path)
            except OSError:
                continue
            if not fresh or len(url) > MAX_URL or not url.startswith(("https://", "http://")) or any(c.isspace() for c in url):
                continue
            _log.info("Launcher öffnet eine Adresse im Browser: %s", url.split("?")[0])
            self.app.shell.return_after("app:browser")
            r = self.wiring.store_open_url(url, False, "app-login")
            if isinstance(r, dict) and not r.get("ok", True):
                _log.warning("Browser: %s", r.get("message"))

    def stop(self) -> None:
        self.stopped.set()
        self.wake.set()
        from .launch import proton
        if launchers.before_start == self.make_room:
            launchers.before_start = None
        if getattr(proton.EPIC_TOOL, "__self__", None) is self:
            proton.EPIC_TOOL = None

    def busy(self) -> str | None:
        with self.lock:
            job = next(iter(self.jobs), None)
        return "%s lädt gerade „%s“ für den Store." % (NAMES[job["store"]], job["title"]) if job else None

    def _run(self) -> None:
        # Erst hier, nicht in start(): Das läuft in der Hauptschleife, und die Marken liegen auf der
        # Spiele-Platte, die vielleicht erst anlaufen muss (E99). _resume lief schon
        try:
            self._clear_stale_marks()
        except Exception:  # noqa: BLE001
            _log.exception("Alte Marken")
        while not self.stopped.is_set():
            self.wake.wait(POLL_S)
            self.wake.clear()
            if self.stopped.is_set():
                return
            with self.lock:
                queued = [j for j in self.jobs if j["state"] in NOT_STARTED]
                watched = [j for j in self.jobs if j["state"] in ("confirm", "installing")]
            # Die Listen sind Momentaufnahmen, und _begin dauert (Launcher einrichten): vor jedem Schritt
            # prüfen, ob der Auftrag noch ansteht (E99)
            for job in queued:
                if self._live(job):
                    self._begin(job)
            stores = {j["store"] for j in watched}
            if time.monotonic() - self._last_sync > SYNC_S:
                self._last_sync = time.monotonic()
                stores |= {sid for sid in STORE_IDS if os.path.isdir(self.prefix(sid))}
            for sid in stores:
                try:
                    self.sync(sid)
                except Exception:  # noqa: BLE001
                    _log.exception("Abgleich %s", sid)
            for job in watched:
                if not self._live(job):
                    continue
                self._progress(job)
                if job["state"] == "confirm" and not job.get("resent") and self._live(job) \
                        and launchers.login_after(self.prefix(job["store"]), job["store"], job["since"]):
                    # erst jetzt angemeldet: Den Auftrag von vorher führt der Launcher nicht mehr aus
                    job["resent"] = True
                    _log.info("%s: angemeldet, Installation von %s noch einmal angestoßen", NAMES[job["store"]], job["title"])
                    self._send_to_running(job["store"], launchers.install_args(job["store"], job["code"])[1:])
                if job.get("hidden") and job["state"] == "confirm" and time.time() - job["since"] > HIDDEN_WAIT_S \
                        and self._live(job):
                    self._reveal(job, "nach %d s kein Download" % HIDDEN_WAIT_S)
                if job["state"] == "confirm" and time.time() - job["since"] > CONFIRM_TIMEOUT_S and self._live(job):
                    self._fail(job, "In %s wurde die Installation nicht bestätigt." % NAMES[job["store"]])

    def _progress(self, job: dict) -> None:
        """Battle.net trägt ein Spiel erst am Ende in die Registry ein: Ob es lädt und wie weit, steht in
        seinem Protokoll (Größe, Zielordner) und im wachsenden Ordner. Höchstens alle PROGRESS_S Sekunden."""
        if job["store"] not in ("battlenet", "rockstar") or time.monotonic() - job.get("checked", 0.0) < PROGRESS_S:
            return
        job["checked"] = time.monotonic()
        if job["store"] == "rockstar":
            # Rockstar legt die Dateien gleich in voller Größe an und füllt sie: belegt / angelegt
            info = launchers.rockstar_install(self.prefix("rockstar"), str(job["code"]), job["since"] - 5)
            if info is None:
                return
            used, size = launchers.tree_usage(info["folder"])
            percent = max(job.get("percent", 0), min(99, int(used * 100 / size))) if size else 0
            gb = round(used / 2**30, 1)
            speed = job.get("speed")
            self._note_bytes(job, used, size)
            if job["state"] != "installing" or percent != job.get("percent") or (not size and gb != job.get("done_gb")) \
                    or speed != job.get("speed"):
                job.update(state="installing", percent=percent, done_gb=gb)
                self.publish()
            return
        mod = self.modules["battlenet"][0]
        uid = str(((getattr(mod, "CODES", {}) if mod else {}).get(str(job["code"])) or {}).get("agent_uid") or "")
        info = launchers.bnet_install(self.prefix("battlenet"), uid, job["since"] - 5) if uid else None
        if info is None:
            return
        percent = job.get("percent", 0)
        speed = job.get("speed")
        if info["total"] and info["folder"]:
            got = launchers.tree_size(info["folder"])[1]
            percent = max(percent, min(99, int(got * 100 / info["total"])))
            self._note_bytes(job, got, info["total"])
        if job["state"] != "installing" or percent != job.get("percent") or speed != job.get("speed"):
            job.update(state="installing", percent=percent)
            self.publish()

    def _begin(self, job: dict) -> None:
        sid = job["store"]
        try:
            # Läuft ein Spiel über diesen Launcher (oder startet gerade), wartet der Auftrag: Er ginge sonst
            # in den Launcher unter dem reaper des Spiels, dessen App dann nicht mehr mit dem Spiel endete
            # und beim Schließen den Download mitnähme (E99). Erst danach Marke und Start
            game = self._game_in_prefix(sid)
            with self.lock:
                if not self._live(job):
                    return
                game = game or self._starting_title(sid)
                if game:
                    changed = job["state"] != "waiting" or job.get("waits_for") != game
                    job.update(state="waiting", waits_for=game)
                else:
                    changed = False
                    # den Prefix nehmen, solange niemand sonst prüft (make_room unter derselben Sperre)
                    job["state"] = "confirm" if self.launcher_ready(sid) else "launcher"
            if game:
                if changed:
                    _log.info("Launcher %s: %s wartet, bis %s beendet ist", sid, job["title"], game)
                    self.publish()
                return
            if job["state"] == "launcher":
                self.publish()
                self.setup_launcher(sid, job)
            if not self._live(job):
                # abgebrochen, während der Launcher eingerichtet wurde (die letzten Schritte prüfen nicht)
                raise launchers.LauncherError("Abgebrochen.")
            self._mark(sid)
            job.update(state="confirm", since=time.time(), percent=0)
            hidden = launchers.hidden_install_args(sid, job["code"])
            shown = self.wiring.main.call(self.app.procs.by_entry, "launcher:%s" % sid) is not None
            if hidden and not shown:
                # unsichtbar (E96): Der Launcher lädt im Hintergrund; braucht er einen, kommt er nach vorn
                job["hidden"] = True
                self.publish()
                self._start_hidden(sid, hidden[1:])
                if not self._live(job):
                    self._stop_hidden_if_idle(sid)      # während des Starts abgebrochen
            else:
                self.publish()
                self.open_launcher(sid, (hidden or launchers.install_args(sid, job["code"]))[1:], job)
        except launchers.LauncherError as e:
            self._fail(job, str(e))
        except Exception as e:  # noqa: BLE001
            _log.exception("Launcher-Auftrag %s", job["title"])
            self._fail(job, "Das hat nicht geklappt: %s" % e)

    def _fail(self, job: dict, text: str, quiet: bool = False) -> None:
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
        self.publish()
        self._unmark(job["store"])
        if text != "Abgebrochen." and not quiet:
            self.wiring.main.post(lambda: self.app.notify.push("„%s“ wurde nicht installiert" % job["title"], text, icon="warn"))


def _describe(found: list) -> str:
    return ", ".join("%d %s%s" % (x["pid"], x["comm"], " (fremd)" if x["own"] is False else "") for x in found)


def extend(wiring: Any) -> None:
    routes = getattr(wiring, "store_routes", None)
    if routes is None:
        return                                  # ohne den Store-Teil gibt es nichts zu erweitern
    part = LauncherStores(wiring)
    wiring.launcher_stores = part
    for name, fns in part.routes().items():
        if name in routes:
            routes[name].update(fns)
    for sid in STORE_IDS:
        wiring.store_extra_owned[sid] = part.owned_check(sid)
    # Haken der Starter (Arbeits-Thread, E99): Platz im Prefix eines Launchers; legendary vor einem
    # Epic-Spiel (gehört zum Store wie diese Shops; wiring.epic_legendary legt der Store-Teil an)
    from .launch import proton
    launchers.before_start = part.make_room
    proton.EPIC_TOOL = part.epic_tool
    from .library import register_source
    register_source("launchers", part.library_source)
    wiring.busy_checks.append(part.busy)
    wiring.shutdown_hooks.append(part.stop)
    wiring.after_ready.append(part.start)
