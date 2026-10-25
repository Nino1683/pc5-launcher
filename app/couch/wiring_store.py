"""Store am Kern (Bauplan E68): RPC store.* und die Downloads über Steam im Hintergrund.

Lesen (Katalog, Konto, Einträge auf der Platte) läuft im Arbeits-Thread. Die Downloads führt
ein eigener Thread (SteamDownloads), höchstens alle STEP_S Sekunden:
  1. Für neue Aufträge muss Steam aus sein: das eigene Hintergrund-Steam beenden; ein fremdes
     Steam ohne laufendes Spiel mit „steam -shutdown“ beenden; läuft ein Steam-Spiel, warten.
  2. Bei beendetem Steam die Einträge schreiben (store.steam_install.write_manifest).
  3. Steam unsichtbar starten (couch-steam-game --download) und den Fortschritt aus den
     Einträgen lesen; fertig = StateFlags 4. Sind alle fertig, Steam wieder beenden.
Beendet ein Spielstart das Hintergrund-Steam (E57 räumt fremdes Steam weg), geht es danach
weiter. Die Warteschlange liegt in <state>/store-steam.json und überlebt einen Neustart; die von
GOG und Epic liegen in store-gog.json bzw. store-epic.json (E99, 0600) und laufen nach dem
Bereit-Signal weiter. GOG und itch.io (wiring_itch.py, E100) teilen sich _FolderQueue: eigener
Download in einen markierten Ordner der Store-Ebene <Spiele-Ordner>/<GOG|itch.io>/<Titel>.
Fortschritt: sys.state.store_jobs [{store, id, title, percent, state, text, kind?, done, total,
speed, eta}] (Control Center › Downloads; Bytes, Bytes/s, Sekunden), eine Sammelstelle für alle
Shops (publish_jobs). Ergebnis als Mitteilung, danach liest die Bibliothek neu.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import threading
import time
import uuid
from typing import Any

from . import APP_ID_BASE, log, paths
from .store import epic as epic_store
from .store import epic_account
from .store import gog as gog_store
from .store import gog_account, gog_install
from .store import steam as steam_store
from .store import steam_account, steam_install

_log = log.get("wiring.store")
STEP_S = 2.0
ACCOUNT_TTL_S = 120.0
ERROR_TTL_S = 10.0              # Konto-Abfrage gescheitert (Netz): so bald wieder fragen (E99)
STALL_S = 15 * 60               # so lange läuft das Download-Steam ohne jeden Fortschritt: es lädt das Spiel wohl nicht
IDLE_STEP_MAX_S = 60.0          # höchstens so viel zählt ein Durchgang dazu (Ruhezustand, hängender Faden)
RUNNER_MIN_S = 60.0             # endet das Download-Steam früher von selbst, ist das ein Fehlstart (E99)
RUNNER_FAILS = 5                # so viele Fehlstarts in RUNNER_FAILS_WINDOW_S: Aufträge mit Mitteilung abbrechen
RUNNER_FAILS_WINDOW_S = 10 * 60
PURCHASE_WATCH_S = 20 * 60      # so lange nach „Kaufen“ nachsehen, ob das Spiel jetzt dir gehört
PURCHASE_STEP_S = 10.0
APP_ID_MAX = 0x7FFFFFFF
MSG_NO_SHOP = "Dieser Shop ist gerade nicht verfügbar."     # store.* mit einem Shop ohne eigenen Teil
STATES = {"queued": "Wartet", "waiting": "Wartet, bis das Steam-Spiel beendet ist", "starting": "Steam startet …",
          "downloading": "Lädt", "cancelling": "Wird abgebrochen …", "removing": "Wird deinstalliert …"}
# Reihenfolge unter Downloads; Launcher-Shops danach (E100: itch.io nach Epic)
STORE_ORDER = {"steam": 0, "gog": 1, "epic": 2, "itch": 3}


def publish_jobs(app: Any, stores: set, jobs: list) -> None:
    """Hauptschleife: die Aufträge dieser Shops in sys.state.store_jobs ersetzen (E99). Eine Sammelstelle
    für alle Warteschlangen (Steam, GOG, Epic; die Launcher über wiring.store_publish_jobs): Der alte
    Stand wird erst hier gelesen, nie in einem anderen Faden (sonst Geister-Downloads)."""
    state = app.state
    old = (state.value("store_jobs") or []) if hasattr(state, "value") else []
    merged = [j for j in old if j.get("store") not in stores] + list(jobs)
    merged.sort(key=lambda j: STORE_ORDER.get(j.get("store"), len(STORE_ORDER)))   # stabil: je Shop wie geliefert
    state.update(store_jobs=merged)


class _Rate:
    """Geschwindigkeit aus Byte-Ständen, geglättet, für „12 MB/s · noch etwa 9 Min.“ (E99)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.at: float | None = None
        self.base = 0
        self.speed = 0.0

    def update(self, done: int) -> None:
        now = self.clock()
        if self.at is None or done < self.base:
            self.at, self.base = now, done
            return
        dt = now - self.at
        if dt < 2.0:
            return
        now_speed = (done - self.base) / dt
        self.speed = now_speed if self.speed <= 0 else 0.7 * self.speed + 0.3 * now_speed
        self.at, self.base = now, done

    def view(self, done, total) -> dict:
        out = {"done": int(done or 0), "total": int(total or 0), "speed": int(self.speed), "eta": 0}
        if self.speed > 0 and out["total"] > out["done"]:
            out["eta"] = int((out["total"] - out["done"]) / self.speed)
        return out


def store_root(app: Any, name: str) -> str:
    """<Spiele-Ordner>/<name> für GOG-, Epic- und itch.io-Spiele (E99, E100): derselbe Ordner wie für
    Archiv-Installer und Launcher (Einstellung install.games_root, leer = Spiele-Platte), damit der
    Papierkorb des Installers GOG- und itch.io-Spiele findet. Ist er nicht da (Platte nicht
    eingehängt), wie bisher ~/Games/<name>."""
    from . import wiring_install
    root = wiring_install.games_root(app)
    if os.path.realpath(root) == os.path.realpath(paths.games_disk()):
        there = os.path.ismount(root)           # ein leerer Ordner unter /run/media ist keine Platte
    else:
        there = os.path.isdir(root)
    return os.path.join(root, name) if there else os.path.join(paths.home(), "Games", name)


def target_view(app: Any, name: str) -> dict | None:
    """{path, free_gb} für „Installiert nach“ auf der Store-Seite."""
    import shutil
    root = store_root(app, name)
    probe = root
    while probe and not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    try:
        return {"path": root, "free_gb": round(shutil.disk_usage(probe).free / 2**30)}
    except OSError:
        return None


def _steam_processes() -> tuple[bool, bool]:
    """(läuft ein Steam-Client, läuft ein Steam-Spiel). Nur eigene Prozesse."""
    from .proc import tree
    uid = os.getuid()
    client = game = False
    for pid, info in tree.snapshot().items():
        try:
            if os.stat("/proc/%d" % pid).st_uid != uid or info.state in ("Z", "X"):
                continue
        except OSError:
            continue
        if info.comm == "steam":
            client = True
        elif info.comm == tree.REAPER_COMM:
            n = tree.reaper_app_id(pid)
            if n is not None and not (APP_ID_BASE < n <= APP_ID_MAX):
                game = True
    return client, game


class SteamDownloads:
    def __init__(self, wiring: Any, ctx: Any, processes=_steam_processes, clock=time.time):
        self.wiring = wiring
        self.app = wiring.app
        self.ctx = ctx
        self.processes = processes
        self.clock = clock
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closed = False
        self.file = os.path.join(paths.state_dir(), "store-steam.json")
        self.queue: list[dict] = self._load()
        self.runner = None                  # Popen von couch-steam-game --download
        self.runner_started_at = 0.0
        self.runner_stopped = False         # das SIGTERM kam von uns: kein Fehlstart
        self.runner_failed_at = 0.0
        self.fails: list[float] = []        # Zeitpunkte der Fehlstarts (E99)
        self.shutdown_sent_at = 0.0         # „steam -shutdown“ an ein fremdes Steam geschickt
        # Laufzeit des Download-Steams ohne jeden Fortschritt (E99). Zählt nur, solange es läuft: Ein
        # Neustart setzt sie nicht zurück (sonst griff die Stillstandsregel bei einem Steam, das immer
        # wieder endet, nie), und die Pause während eines Steam-Spiels zählt nicht mit
        self.idle_s = 0.0
        self.last_step_at: float | None = None
        self.rates: dict[int, _Rate] = {}
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------ Warteschlange
    def _load(self) -> list[dict]:
        try:
            with open(self.file, encoding="utf-8") as fh:
                data = json.load(fh)
            return [j for j in data.get("queue", []) if isinstance(j, dict) and isinstance(j.get("appid"), int)]
        except (OSError, ValueError, AttributeError):
            return []

    def _save(self) -> None:
        tmp = self.file + ".neu"
        try:
            os.makedirs(os.path.dirname(self.file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"queue": self.queue}, fh, indent=1)
            os.replace(tmp, self.file)
        except OSError:
            _log.exception("Warteschlange des Stores speichern")

    def jobs(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in self.queue]

    def find(self, appid: int) -> dict | None:
        return next((j for j in self.queue if j["appid"] == appid), None)

    def add(self, appid: int, name: str, lib: str, installdir: str) -> None:
        with self.lock:
            if self.find(appid) is None:
                if not self.queue:
                    self.idle_s = 0.0
                self.queue.append({"appid": appid, "name": name, "lib": lib, "installdir": installdir,
                                   "written": False, "state": "queued", "percent": 0, "added": int(self.clock()),
                                   "done": 0})
                self._save()
        self.start()
        self.wake.set()

    def uninstall(self, appid: int, name: str, lib: str) -> None:
        """Spiel deinstallieren: wie Abbrechen, aber auch der fertige Spielordner (Steam muss aus)."""
        with self.lock:
            job = self.find(appid)
            if job is None:
                job = {"appid": appid, "name": name, "lib": lib, "installdir": "", "written": True, "percent": 0,
                       "added": int(self.clock()), "done": 0}
                self.queue.append(job)
            job.update(cancel=True, uninstall=True, state="removing")
            self._save()
        self.start()
        self.wake.set()

    def cancel(self, appid: int) -> bool:
        with self.lock:
            job = self.find(appid)
            if job is None or job.get("uninstall"):
                return False                    # eine Deinstallation läuft zu Ende (E99: sie lief sonst trotz „Wird abgebrochen“ weiter)
            job["cancel"] = True
            job["state"] = "cancelling"
            self._save()
        self.wake.set()
        return True

    # ------------------------------------------------------------ Faden
    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="couch-store-steam", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.closed = True
        self.wake.set()
        self._stop_runner()

    def _run(self) -> None:
        while not self.closed:
            try:
                busy = self.step()
            except Exception:
                _log.exception("Downloads des Stores")
                busy = True
            self.wiring.main.post(self.publish)
            if not busy:
                return                          # nichts mehr zu tun; add() startet neu
            self.wake.wait(STEP_S)
            self.wake.clear()

    def publish(self) -> None:
        jobs = []
        for j in self.jobs():
            state = j.get("state") or "queued"
            view = {"store": "steam", "id": str(j["appid"]), "title": j["name"], "percent": int(j.get("percent") or 0),
                    "state": state, "text": STATES.get(state, "")}
            rate = self.rates.get(j["appid"])
            view.update(rate.view(j.get("dl_done"), j.get("dl_total")) if rate and state == "downloading"
                        else {"done": int(j.get("dl_done") or 0), "total": int(j.get("dl_total") or 0), "speed": 0, "eta": 0})
            jobs.append(view)
        publish_jobs(self.app, {"steam"}, jobs)

    def step(self) -> bool:
        """Ein Durchgang. False: nichts mehr zu tun (Steam ist dann aus)."""
        with self.lock:
            queue = [dict(j) for j in self.queue]
        now = self.clock()
        dt = 0.0 if self.last_step_at is None else max(0.0, min(IDLE_STEP_MAX_S, now - self.last_step_at))
        self.last_step_at = now
        runner_alive = self.runner is not None and self.runner.poll() is None
        if runner_alive:
            self.idle_s += dt
        client = game = None
        if self.runner is not None and not runner_alive:
            client, game = self.processes()
            self._runner_ended(game)
        if not queue:
            if runner_alive:
                self._stop_runner()
            return False
        info = steam_install.manifests(self.ctx)
        # Abbrüche zuerst: brauchen ein beendetes Steam
        cancels = [j for j in queue if j.get("cancel")]
        need_off = cancels or [j for j in queue if not j.get("written")]
        if client is None:
            client, game = self.processes()
        if need_off:
            if runner_alive:
                self._stop_runner()
                return True
            if client:
                if game:
                    self._set_state([j["appid"] for j in need_off], "waiting")
                    return True
                if self.clock() - self.shutdown_sent_at > 30:
                    self.shutdown_sent_at = self.clock()
                    self._shutdown_foreign()
                return True
            for job in cancels:
                self._cancel_now(job, info.get(job["appid"]))
            for job in [j for j in queue if not j.get("written") and not j.get("cancel")]:
                self._write(job, info.get(job["appid"]))
            return True
        # Fortschritt und Ende
        finished = []
        for job in queue:
            m = info.get(job["appid"])
            if steam_install.installed(m):
                finished.append(job)
                continue
            self._progress(job, m)
        for job in finished:
            self._finished(job)
        with self.lock:
            left = [j for j in self.queue if not j.get("cancel")]
            # Lädt Steam gar nichts (kein Download hat je angefangen), ist etwas faul: meist gehört
            # das Spiel nicht dem angemeldeten Konto. Dann abbrechen statt ewig zu warten.
            stalled = self.idle_s > STALL_S and all(not (info.get(j["appid"]) or {}).get("total") for j in left)
            # Startet das Download-Steam immer wieder nicht (E99): Aufträge mit Mitteilung abbrechen
            # statt endlos neu zu starten
            broken = len(self.fails) >= RUNNER_FAILS
            if left and (stalled or broken):
                for job in left:
                    job["cancel"] = True
                    job["failed"] = "start" if broken else True
                    job["state"] = "cancelling"
                self.fails = []
                self.idle_s = 0.0
                self._save()
                return True                     # der nächste Durchgang bricht ab (braucht Steam aus)
        if not left:
            if runner_alive:
                self._stop_runner()
            return False
        if not runner_alive and not client:
            if self.clock() - self.runner_failed_at < 30:
                return True
            self.runner_started_at = self.clock()
            self.runner_stopped = False
            self._start_runner()
            self._set_state([j["appid"] for j in left], "starting")
        elif client and not runner_alive and game:
            self._set_state([j["appid"] for j in left], "waiting")
        return True

    # ------------------------------------------------------------ Teilschritte
    def _set_state(self, appids: list[int], state: str) -> None:
        with self.lock:
            for job in self.queue:
                if job["appid"] in appids and job.get("state") not in ("cancelling", "removing"):
                    job["state"] = state

    def _runner_ended(self, game: bool) -> None:
        """Das Download-Steam ist beendet. Fehlstart (E99): Es endete von selbst mit Fehler oder nach
        weniger als RUNNER_MIN_S, ohne SIGTERM von uns und ohne laufendes Steam-Spiel (E57 beendet es
        für jeden Spielstart; das ist kein Fehler)."""
        runner, self.runner = self.runner, None
        rc = runner.returncode
        now = self.clock()
        ran = now - self.runner_started_at
        _log.info("Hintergrund-Steam endete (%s nach %d s)", rc, ran)
        if self.runner_stopped or game:
            return
        if rc not in (0, -signal.SIGTERM) or (rc == 0 and ran < RUNNER_MIN_S):
            self.runner_failed_at = now
            self.fails = [t for t in self.fails if now - t < RUNNER_FAILS_WINDOW_S] + [now]

    def _write(self, job: dict, info: dict | None) -> None:
        appid = job["appid"]
        if info is None:
            try:
                owner = self._owner()
                steam_install.write_manifest(job["lib"], appid, job["name"], job["installdir"], owner)
                _log.info("Store: Eintrag für %s (%d) in %s geschrieben", job["name"], appid, job["lib"])
            except FileExistsError:
                pass
            except OSError as e:
                _log.warning("Store: Eintrag für %d nicht geschrieben: %s", appid, e)
                self._drop(job, "„%s“ ließ sich nicht installieren" % job["name"],
                           "Die Steam-Bibliothek ist nicht beschreibbar: %s" % e, ok=False)
                return
        with self.lock:
            live = self.find(appid)
            if live is not None:
                live["written"] = True
                live["state"] = "queued"
                self.idle_s = 0.0               # ein neuer Eintrag: Steam bekommt wieder die volle Frist
                self._save()

    def _owner(self) -> int | None:
        try:
            from .library import steam as steam_lib
            sid, _rec = steam_lib._login_user(self.ctx)
            return sid
        except Exception:
            return None

    def _progress(self, job: dict, info: dict | None) -> None:
        with self.lock:
            live = self.find(job["appid"])
            if live is None:
                return
            done = info["done"] if info else 0
            if done != live.get("done"):
                live["done"] = done
                self.idle_s = 0.0
            live["total"] = info["total"] if info else 0
            # Anzeige und Geschwindigkeit nur aus dem Laden (E99), Stillstand und Prozent aus allem
            live["dl_done"] = int(info.get("dl_done") or 0) if info else 0
            live["dl_total"] = int(info.get("dl_total") or 0) if info else 0
            self.rates.setdefault(job["appid"], _Rate(self.clock)).update(live["dl_done"])
            live["percent"] = steam_install.percent(info) if info else 0
            if self.runner is not None:
                live["state"] = "downloading" if info and info["total"] > 0 else "queued"
            self._save()

    def _finished(self, job: dict) -> None:
        self._drop(job, "„%s“ ist installiert" % job["name"], "Du findest es jetzt in der Bibliothek.", ok=True)
        self.wiring.main.post(self._rescan)

    def _cancel_now(self, job: dict, info: dict | None) -> None:
        """Bei beendetem Steam: Eintrag und Teil-Download in den Papierkorb. Ist das Spiel installiert
        (etwa mit offenem Update, E99), bleiben Eintrag und Spielordner; scheiterte der Auftrag, wird
        dann gar nichts weggeräumt."""
        from . import files
        moved = 0
        kept = not job.get("uninstall") and steam_install.present(info)
        trash = [] if kept and job.get("failed") else \
            steam_install.leftovers(info, job["appid"], uninstall=bool(job.get("uninstall"))) if info else []
        for path in trash:
            try:
                files.gio_trash(path)
                moved += 1
            except (OSError, ValueError) as e:
                _log.warning("Store: %s nicht in den Papierkorb: %s", path, e)
        if job.get("uninstall") and not moved:
            # Platte schläft oder fehlt, Papierkorb verweigert: nicht „deinstalliert“ melden, das
            # Spiel stünde nach dem Neueinlesen wieder da
            self._drop(job, "„%s“ wurde nicht deinstalliert" % job["name"],
                       "Die Spieldateien ließen sich nicht entfernen. Ist die Platte mit dem Spiel angeschlossen?", ok=False)
        elif job.get("uninstall"):
            self._drop(job, "„%s“ ist deinstalliert" % job["name"],
                       "Die Spieldateien liegen im Papierkorb. Spielstände bleiben erhalten.", ok=True)
        elif job.get("failed") == "start":
            self._drop(job, "„%s“ wurde nicht %s" % (job["name"], "aktualisiert" if kept else "installiert"),
                       "Steam ließ sich für den Download nicht starten. Versuche es später noch einmal."
                       + (" Das Spiel bleibt installiert." if kept else ""), ok=False)
        elif job.get("failed") and kept:
            self._drop(job, "„%s“ wurde nicht aktualisiert" % job["name"],
                       "Steam hat das Update nicht geladen. Das Spiel bleibt installiert; Steam lädt das Update "
                       "vor dem nächsten Start.", ok=False)
        elif job.get("failed"):
            self._drop(job, "„%s“ wurde nicht installiert" % job["name"],
                       "Steam hat nicht angefangen zu laden. Gehört dir das Spiel im angemeldeten Steam-Konto?", ok=False)
        elif kept:
            self._drop(job, "Download abgebrochen", "„%s“ bleibt installiert." % job["name"], ok=True)
        else:
            self._drop(job, "Download abgebrochen", "„%s“ wird nicht installiert." % job["name"], ok=True)
        self.wiring.main.post(self._rescan)

    def _drop(self, job: dict, title: str, body: str, ok: bool) -> None:
        with self.lock:
            self.queue = [j for j in self.queue if j["appid"] != job["appid"]]
            self._save()
        self.rates.pop(job["appid"], None)
        self.wiring.main.post(self._notify, title, body, ok)

    def _notify(self, title: str, body: str, ok: bool) -> None:
        try:
            self.app.notify.push(title, body, icon="download" if ok else "warn", sound=None if ok else "error")
        except Exception:
            _log.exception("Mitteilung")

    def _rescan(self) -> None:
        try:
            self.app.library.rescan()
        except Exception:
            _log.exception("Bibliothek neu lesen")

    # ------------------------------------------------------------ Steam
    def _start_runner(self) -> None:
        helper = os.path.join(paths.app_dir(), "bin", "couch-steam-game")
        env = dict(os.environ)
        try:
            display = self.app.gs.game_display()
        except Exception:
            display = None
        if display:
            env["DISPLAY"] = display
        if os.environ.get("COUCH_SESSION_ENV"):
            env["COUCH_STEAM_SESSION_ENV"] = os.environ["COUCH_SESSION_ENV"]
        env["COUCH_STEAM_LOG"] = paths.app_log("steam-download")     # kappt couch-steam-game selbst (E99)
        try:
            # angehängt: vor jedem Download-Steam gekappt (log.capped, E99)
            log_fh = open(log.capped(paths.app_log("steam-download-helper")), "a")
        except OSError:
            log_fh = None
        try:
            from .wiring import _popen_bound
            self.runner = _popen_bound([helper, "--download"], env=env, stdin=open(os.devnull),
                                       stdout=log_fh or open(os.devnull, "w"), stderr=log_fh or None)
            _log.info("Hintergrund-Steam für Downloads gestartet (PID %d)", self.runner.pid)
        except OSError as e:
            _log.warning("Hintergrund-Steam startet nicht: %s", e)
            now = self.clock()
            self.runner_failed_at = now
            self.fails = [t for t in self.fails if now - t < RUNNER_FAILS_WINDOW_S] + [now]

    def _stop_runner(self) -> None:
        runner = self.runner
        if runner is None or runner.poll() is not None:
            return
        self.runner_stopped = True
        try:
            runner.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def _shutdown_foreign(self) -> None:
        """Ein Steam ohne Spiel (etwa aus der Desktop-App) beenden, damit Einträge geschrieben
        werden dürfen. Läuft im Faden; die Wirkung sieht der nächste Durchgang."""
        from . import system
        _log.info("Store: fremdes Steam wird für neue Downloads beendet")
        system.run_write(["steam", "-shutdown"], timeout=30.0, note="store")


GOG_STATES = {"queued": "Wartet", "downloading": "Lädt", "installing": "Wird installiert …", "cancelling": "Wird abgebrochen …",
              "waiting": "Wartet auf GOG …"}
# Netz oder GOG kurz weg (E99): so lange warten, bevor es noch einmal versucht wird (zusammen gut 15 min)
GOG_RETRY_S = (30, 60, 120, 240, 480)
EPIC_STATES = {"queued": "Wartet", "preparing": "Wird vorbereitet …", "downloading": "Lädt",
               "installing": "Wird eingetragen …", "cancelling": "Wird abgebrochen …", "removing": "Wird deinstalliert …",
               "login": "Wartet auf die Anmeldung bei Epic"}


def _write_private(path: str, data: Any) -> None:
    """JSON atomar und nur für dich lesbar (0600) ablegen."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".neu"
    # Eine liegengebliebene .neu behielte sonst ihre Rechte; ein Verweis wird nie verfolgt (E99)
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())               # nach einem Stromausfall keine leere Warteschlange
    os.replace(tmp, path)


class _StoreQueue:
    """Gemeinsames der Warteschlangen von GOG und Epic (E99): Aufträge einer nach dem anderen in einem
    Faden, gespeichert in <state>/<FILE> (0600, atomar). Nach einem Neustart warten sie wieder und
    laufen nach dem Bereit-Signal weiter (extend: after_ready); ein Auftrag, der gerade abgebrochen
    wurde, wird zu Ende abgebrochen. Beim Ende des Home (stop) bleibt die Datei, wie sie ist."""
    STORE = ""
    FILE = ""
    KEY = ""                                # Feld, das einen Auftrag benennt
    SAVED: tuple = ()                       # Felder, die in der Datei stehen
    STATES: dict = {}

    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.running = False                # ein Faden arbeitet die Warteschlange ab (unter lock)
        self.current: dict | None = None    # der Auftrag, an dem der Faden gerade arbeitet
        self.closing = False                # das Home endet: nichts wegräumen, nichts melden
        self.file = os.path.join(paths.state_dir(), self.FILE)
        self.queue: list[dict] = self._load()

    # ------------------------------------------------------------ Datei
    def _load(self) -> list[dict]:
        try:
            with open(self.file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        out = []
        for raw in (data.get("queue") if isinstance(data, dict) else None) or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("title"), str) or not self._valid(raw):
                continue
            job = self._restored({k: raw[k] for k in self.SAVED if k in raw})
            if job is not None:
                out.append(job)
        return out

    def _valid(self, raw: dict) -> bool:
        return bool(raw.get(self.KEY))

    def _restored(self, job: dict) -> dict | None:
        """Auftrag aus der Datei: ein abgebrochener bleibt abgebrochen, alles andere wartet wieder."""
        job["cancel"] = threading.Event()
        if job.get("state") == "cancelling":
            job["cancel"].set()
        else:
            job["state"] = "queued"
        job["percent"] = int(job.get("percent") or 0) if str(job.get("percent") or 0).isdigit() else 0
        return job

    def _save(self) -> None:
        """Nur mit gehaltenem self.lock rufen."""
        try:
            _write_private(self.file, {"queue": [{k: j[k] for k in self.SAVED if k in j} for j in self.queue]})
        except OSError:
            _log.exception("Warteschlange %s speichern", self.STORE)

    # ------------------------------------------------------------ Aufträge
    def jobs(self) -> list[dict]:
        with self.lock:
            return [dict(j, cancel=None, rate=None) for j in self.queue]

    def start(self) -> None:
        with self.lock:
            go = bool(self.queue) and not self.closing and not self.running
            if go:
                self.running = True
                self.thread = threading.Thread(target=self._loop, name="couch-store-" + self.STORE, daemon=True)
                thread = self.thread
        if go:
            thread.start()
        self.wiring.main.post(self.publish)

    def _loop(self) -> None:
        """Rahmen des Fadens (E99): Wirft _run unerwartet (etwa beim Aufräumen), bliebe running sonst
        gesetzt, und bis zum Neustart liefe kein Auftrag mehr. Der Auftrag kommt dann mit Mitteilung weg."""
        crashed = False
        try:
            self._run()
        except Exception:  # noqa: BLE001
            _log.exception("Warteschlange %s", self.STORE)
            crashed = True
        finally:
            with self.lock:
                if self.thread is threading.current_thread():   # nicht den Merker eines neueren Fadens
                    self.running = False
        if crashed and not self.closing:
            with self.lock:
                job = self.current if any(j is self.current for j in self.queue) else None
            if job is not None:
                try:
                    self._done(job, "„%s“: das hat nicht geklappt" % job["title"],
                               "Ein unerwarteter Fehler ist aufgetreten. Einzelheiten stehen im Protokoll.", False)
                except Exception:  # noqa: BLE001
                    _log.exception("Warteschlange %s: Auftrag entfernen", self.STORE)
            self.start()

    def _next(self) -> dict | None:
        """Der nächste Auftrag; None beendet den Faden (nichts mehr zu tun, oder das Home endet)."""
        with self.lock:
            job = self.queue[0] if self.queue else None
            if job is None or self.closing:
                self.running = False
                self.current = None
                return None
            self.current = job
            return job

    def _halt(self) -> None:
        with self.lock:
            self.running = False

    def _set(self, job: dict, save: bool = False, **kw) -> None:
        with self.lock:
            changed = "state" in kw and kw["state"] != job.get("state")
            job.update(kw)
            if save or changed:
                self._save()
        self.wiring.main.post(self.publish)

    def _done(self, job: dict, title: str, body: str, ok: bool) -> None:
        with self.lock:
            self.queue = [j for j in self.queue if j is not job]
            self._save()
        self.wiring.main.post(self.publish)
        self.wiring.main.post(self._notify, title, body, ok)

    def _notify(self, title: str, body: str, ok: bool) -> None:
        try:
            self.app.notify.push(title, body, icon="download" if ok else "warn", sound=None if ok else "error")
        except Exception:
            _log.exception("Mitteilung")

    # ------------------------------------------------------------ Anzeige
    def view(self, job: dict) -> dict | None:
        state = job.get("state") or "queued"
        out = {"store": self.STORE, "id": str(job[self.KEY]), "title": job["title"], "percent": int(job.get("percent") or 0),
               "state": state, "text": self.STATES.get(state, ""), "done": 0, "total": 0, "speed": 0, "eta": 0}
        if state == "downloading" and job.get("rate") is not None:
            out.update(job["rate"].view(job.get("done"), job.get("total")))
        return out

    def publish(self) -> None:
        with self.lock:
            snapshot = [dict(j) for j in self.queue]
        publish_jobs(self.app, {self.STORE}, [v for v in (self.view(j) for j in snapshot) if v is not None])

    def _run(self) -> None:
        raise NotImplementedError


def _installer_running(path: str) -> bool:
    """Läuft ein eigener Prozess, der diesen Installer in der Befehlszeile hat (Linux-Pfad oder Z:\\…)?
    Nur lesend (/proc)."""
    plain = os.fsencode(path)
    windows = os.fsencode(gog_install.windows_dir(path)).lower()
    uid, me = os.getuid(), os.getpid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return False
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            if os.stat("/proc/" + name).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmd = fh.read()
        except OSError:
            continue
        if plain in cmd or windows in cmd.lower():
            return True
    return False


def installer_launch(wiring: Any, entry_id: str, title: str, installer: str, detail: dict | None = None,
                     proc_root: str = "/proc") -> tuple:
    """(umu-run, Umgebung) für einen Installer, der unsichtbar im Prefix des späteren Eintrags entry_id
    läuft (GOG E99, itch.io E100, eigene setup.exe E114): derselbe Prefix wie beim späteren Start, weil
    proton.build ihn aus der Eintrags-ID bildet (<prefixe>/local-<id>). Blockierend (Platte, /proc).
    E101: Ist der einzige Proton-Build beschädigt, oder arbeitet im Prefix schon Wine, wirft es
    gog_install.InstallError mit dem Satz des Starters; dann startet nichts."""
    from .launch import proton
    from .launch.spec import LaunchError, strip_env
    from .library.model import Entry
    from .proc.manager import SCRUB
    ctx = getattr(wiring, "launch", None)
    if ctx is None:
        raise gog_install.InstallError("Proton ist gerade nicht bereit. Versuche es gleich noch einmal.")
    temp = Entry(id=entry_id, title=title, source="local",
                 detail=dict({"platform": "windows", "install_dir": os.path.dirname(installer),
                              "exe": os.path.basename(installer)}, **(detail or {})))
    try:
        spec = proton.build(temp, ctx, check=False)
        prefix = str((spec.env or {}).get("WINEPREFIX") or "")
        if prefix:
            proton.prefix_free(prefix, str((spec.env or {}).get("PROTONPATH") or ""), proc_root)
    except LaunchError as e:
        raise gog_install.InstallError(e.message) from e
    # Wie beim AppManager (E99): Reste der Sitzung weg (SCRUB) und spec.unset samt Mustern ("LUTRIS_*");
    # vorher wurde wörtlich verglichen, und Muster blieben stehen
    env = strip_env(dict(os.environ), SCRUB + tuple(spec.unset or ()))
    env.update(spec.env or {})
    try:
        display = wiring.app.gs.game_display()
    except Exception:
        display = None
    if display:
        env["DISPLAY"] = display
    return spec.argv[0], env


def _net_error(e: BaseException) -> bool:
    """Netz oder Server kurz gestört (urllib, Verbindung, Zeitlimit, TLS, HTTP-Protokoll)."""
    import http.client
    import ssl
    import urllib.error
    return isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError, http.client.HTTPException))


def _transient(e: BaseException) -> bool:
    """Netz oder Server kurz gestört: Ein neuer Versuch lohnt (E99). Nicht: abgemeldet, kein Platz,
    kaputte Datei, kein Installer."""
    if isinstance(e, gog_account.GogLoginNeeded):
        return False
    if isinstance(e, (gog_account.GogError, gog_install.Interrupted)):
        return True
    return _net_error(e)


class _FolderQueue(_StoreQueue):
    """Gemeinsames der Warteschlangen, die selbst laden und in einen eigenen Ordner der festen
    Store-Ebene <Spiele-Ordner>/<LEVEL>/<Titel> installieren (GOG E99, itch.io E100). Der Ordner
    bekommt beim Anlegen unseren Marker mit entry_id und Zufallswert: Daran erkennen ihn der Papierkorb
    des Installers (Deinstallieren, trash.STORE_LEVELS) und nach einem Neustart das Fortsetzen.
    Fortsetzen und Aufräumen nur im nachweislich eigenen Ordner (SAFETY 13)."""
    LEVEL = ""                              # Store-Ebene im Spiele-Ordner (trash.STORE_LEVELS)
    SETUP_DIR = ""                          # Unterordner der Downloads; kommt nach der Installation weg
    NAME = ""                               # Shop in Protokoll und Sätzen
    MARKER_STORE = ""                       # Feld store im Marker
    LOG_NAME = ""                           # Protokoll des Installers (paths.app_log)
    REGISTRY = ""                           # <state>/<datei>: über den Store installierte Spiele
    EXPECTED: tuple = (OSError,)            # erwartete Fehler: ohne Traceback ins Protokoll

    def __init__(self, wiring: Any):
        self.proc = None                    # laufender Installer
        super().__init__(wiring)

    def _restored(self, job: dict) -> dict | None:
        """Wie oben; Felder mit falscher Form (Datei beschädigt oder von Hand geändert) fallen weg,
        dann gibt es eben einen neuen Ordner (E99)."""
        from .installer import trash
        job = super()._restored(job)
        checks = {"entry_id": trash.ENTRY_ID_RE, "token": trash.TOKEN_RE}
        for key, pattern in checks.items():
            if not (isinstance(job.get(key), str) and pattern.match(job[key])):
                job.pop(key, None)
        if not (isinstance(job.get("target"), str) and os.path.isabs(job["target"])):
            job.pop("target", None)
        return job

    # ------------------------------------------------------------ Aufträge
    def cancel(self, key: Any) -> bool:
        with self.lock:
            job = next((j for j in self.queue if j[self.KEY] == key), None)
            if job is None:
                return False
            job["cancel"].set()
            job["state"] = "cancelling"
            self._save()
            proc = self.proc if job is (self.queue[0] if self.queue else None) else None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
        self.start()                            # ein wartender Auftrag (etwa nach einem Neustart) wird so aufgeräumt
        return True

    def stop(self) -> None:
        """Ende des Home: anhalten, nichts in den Papierkorb, nichts melden. Die Datei behält den Stand
        von vorher; nach dem Neustart geht es weiter (E99)."""
        self.closing = True
        with self.lock:
            for job in self.queue:
                job["cancel"].set()
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def _transient(self, e: BaseException) -> bool:
        return _net_error(e)

    def _retries(self) -> tuple:
        return GOG_RETRY_S

    def _install(self, job: dict) -> str:
        raise NotImplementedError

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            try:
                if self._known_entry(job.get("entry_id"), doubt=False):
                    # E99: Das Home endete zwischen Eintragen und Austragen des Auftrags. Das Spiel ist
                    # da; nicht noch einmal laden und nie seinen Ordner oder Prefix wegräumen
                    self._done(job, "„%s“ ist installiert" % job["title"], "Du findest es jetzt in der Bibliothek.", True)
                    continue
                if job["cancel"].is_set():
                    raise gog_install.InstallError("Abgebrochen.")      # schon vor dem Neustart abgebrochen
                self._install(job)
                self._done(job, "„%s“ ist installiert" % job["title"], "Du findest es jetzt in der Bibliothek.", True)
            except Exception as e:  # noqa: BLE001
                if self.closing:
                    self._halt()
                    return
                if not job["cancel"].is_set() and job.get("state") != "installing" and self._transient(e) \
                        and self._wait_retry(job, e):
                    continue                    # derselbe Auftrag noch einmal; der Ordner bleibt zum Fortsetzen
                if isinstance(e, (gog_install.InstallError,) + tuple(self.EXPECTED)):
                    _log.warning("%s: %s nicht installiert: %s", self.NAME, job["title"], e)
                else:
                    _log.exception("%s-Installation %s", self.NAME, job["title"])
                self._cleanup(job)
                if job["cancel"].is_set():
                    self._done(job, "Download abgebrochen", "„%s“ wird nicht installiert." % job["title"], True)
                else:
                    self._done(job, "„%s“ wurde nicht installiert" % job["title"], str(e), False)

    def _wait_retry(self, job: dict, error: BaseException) -> bool:
        """Netz oder Shop kurz weg, etwa gleich nach dem Hochfahren (E99): warten und noch einmal,
        statt den Teil-Download wegzuwerfen. False, wenn die Versuche aufgebraucht sind."""
        delays = self._retries()
        tries = int(job.get("tries") or 0)
        if tries >= len(delays):
            return False
        _log.info("%s: %s: %s; neuer Versuch in %d s", self.NAME, job["title"], error, delays[tries])
        self._set(job, state="waiting", tries=tries + 1)
        job["cancel"].wait(delays[tries])       # Abbrechen und das Ende des Home wecken sofort
        return True

    # ------------------------------------------------------------ Verzeichnis der Store-Spiele
    @classmethod
    def registry_file(cls) -> str:
        return os.path.join(paths.state_dir(), cls.REGISTRY)

    @classmethod
    def installed(cls) -> dict:
        raise NotImplementedError

    def _entry_ids(self) -> set:
        """Einträge der Bibliothek, die über diesen Store installiert sind."""
        return {str(v) for v in self.installed().values()}

    def _known_entry(self, entry_id: Any, doubt: bool = True) -> bool:
        """Kennt die Bibliothek (oder unser Verzeichnis der Store-Spiele) diesen Eintrag schon? Lässt sich
        die Bibliothek nicht fragen, gilt doubt (beim Aufräumen ja: dann wird nichts weggeräumt)."""
        if not entry_id:
            return False
        if entry_id in self._entry_ids():
            return True
        library = getattr(self.app, "library", None)
        try:
            return library is not None and library.get(entry_id) is not None
        except Exception:  # noqa: BLE001
            _log.exception("%s: Bibliothek fragen", self.NAME)
            return doubt

    # ------------------------------------------------------------ Spielordner
    @staticmethod
    def _own_target(job: dict, root: str) -> tuple[str, dict]:
        """(Ordner, Marker) des gespeicherten Spielordners, aber nur, wenn er sicher zu diesem Auftrag
        gehört (E99, SAFETY 13): direkt in der Store-Ebene root, echter Ordner (kein Verweis), mit
        unserem Marker und dem Zufallswert des Auftrags. Sonst ("", {}). Ziel und Zufallswert können aus
        der Datei von vor einem Neustart stammen; ohne diese Prüfung fasst die Warteschlange keinen Ordner an."""
        from .installer import trash
        target, token = str(job.get("target") or ""), str(job.get("token") or "")
        if not target or not token or not os.path.isabs(target):
            return "", {}
        real_root = os.path.realpath(root)
        real = os.path.realpath(target)
        if os.path.islink(target) or os.path.dirname(real) != real_root or os.path.basename(real).startswith("."):
            return "", {}
        if not os.path.isdir(real):
            return "", {}
        try:
            marker = trash.read_marker(real)
        except trash.Refuse:
            return "", {}
        if marker.get("token") != token:
            return "", {}
        return real, marker

    @classmethod
    def _resume_target(cls, job: dict, root: str) -> str:
        """Der gespeicherte Spielordner zum Fortsetzen nach einem Neustart: unserer (siehe _own_target),
        mit dem Download-Ordner (SETUP_DIR) und mit der entry_id des Auftrags im Marker. Sonst "" (dann
        ein neuer Ordner)."""
        real, marker = cls._own_target(job, root)
        if not real or not job.get("entry_id") or marker.get("entry_id") != job["entry_id"]:
            return ""
        setup = os.path.join(real, cls.SETUP_DIR)
        if os.path.islink(setup) or not os.path.isdir(setup):
            return ""
        return real

    def _free_target(self, root: str, title: str) -> str:
        return gog_install.free_target(root, title)

    def _marker_extra(self, job: dict) -> dict:
        """Weitere Felder des Markers (Kennung beim Shop)."""
        return {}

    def _new_target(self, job: dict, root: str) -> str:
        """Neuer Spielordner <LEVEL>/<Titel> mit unserem Marker (E99), gleich beim Anlegen."""
        from .installer import extract
        target = self._free_target(root, job["title"])
        os.makedirs(target)
        entry_id = str(job.get("entry_id") or "") or "local:" + uuid.uuid4().hex
        token = secrets.token_hex(16)
        fields = {"version": 1, "token": token, "entry_id": entry_id, "title": job["title"], "store": self.MARKER_STORE}
        fields.update(self._marker_extra(job))
        fields.update({"games_root": os.path.realpath(os.path.dirname(root)),
                       "folder": self.LEVEL + "/" + os.path.basename(target), "created": time.time()})
        extract.write_marker(target, fields)
        self._set(job, save=True, target=target, entry_id=entry_id, token=token)
        return target

    # ------------------------------------------------------------ Installer über Proton
    PROC_ROOT = "/proc"                  # für die Prüfung auf Wine im Prefix (Tests: erfundenes /proc)

    def _run_proton(self, job: dict, entry_id: str, installer: str, command: list, detail: dict | None = None) -> int:
        """Installer unsichtbar über Proton, im Prefix, den das Spiel später benutzt. command: der Befehl
        ohne umu-run davor (Installer oder msiexec samt stillen Schaltern). Rückgabe: Rückgabewert.
        E101: Ist der einzige Proton-Build beschädigt, oder arbeitet im Prefix schon Wine (auch fremdes,
        auch eine andere Proton-Fassung), startet nichts: InstallError mit dem Satz des Starters."""
        ctx = getattr(self.wiring, "launch", None)
        if ctx is None:
            raise gog_install.InstallError("Proton ist gerade nicht bereit. Versuche es gleich noch einmal.")
        # Endete das Home mitten in der Installation, kann der alte Installer noch laufen (er hat eine
        # eigene Sitzung): erst warten, zwei Installer im selben Prefix verderben sich (E99)
        deadline = time.monotonic() + 3 * 3600
        while _installer_running(installer) and time.monotonic() < deadline:
            if job["cancel"].is_set():
                raise gog_install.InstallError("Abgebrochen.")
            time.sleep(5)
        umu, env = installer_launch(self.wiring, entry_id, job["title"], installer, detail, self.PROC_ROOT)
        argv = [umu] + [str(a) for a in command]
        _log.info("%s: Installer läuft: %s", self.NAME, " ".join(argv))
        log_path = log.capped(paths.app_log(self.LOG_NAME))     # angehängt: vorher kappen (E99)
        import subprocess
        with open(log_path, "a") as log_fh:
            self.proc = subprocess.Popen(argv, env=env, cwd=os.path.dirname(installer), stdin=subprocess.DEVNULL,
                                         stdout=log_fh, stderr=log_fh, start_new_session=True)
            try:
                rc = self.proc.wait(timeout=3 * 3600)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                raise gog_install.InstallError("Der Installer ist nach drei Stunden nicht fertig geworden.")
            finally:
                self.proc = None
        if job["cancel"].is_set():
            raise gog_install.InstallError("Abgebrochen.")
        return rc

    # ------------------------------------------------------------ Aufräumen
    def _cleanup(self, job: dict) -> None:
        """Nach Abbruch oder Fehler: unseren Spielordner und den Prefix des Auftrags in den Papierkorb.
        Nur, was sicher zu diesem Auftrag gehört (E99, SAFETY 13): der Ordner nach _own_target (auch
        ohne Download-Ordner), der Prefix nur, wenn derselbe Marker die entry_id nennt und die Bibliothek
        den Eintrag nicht kennt (sonst lägen dort Spielstände eines installierten Spiels)."""
        from . import files
        from .installer import trash as trash_mod
        if self._known_entry(job.get("entry_id")):
            return
        real, marker = self._own_target(job, store_root(self.app, self.LEVEL))
        if not real:
            if job.get("target"):
                _log.warning("%s: %s bleibt liegen (nicht sicher unserer)", self.NAME, job["target"])
            return
        trash = [real]
        entry_id = job.get("entry_id")
        match = trash_mod.ENTRY_ID_RE.match(entry_id) if isinstance(entry_id, str) else None
        ctx = getattr(self.wiring, "launch", None)
        if match and ctx is not None and marker.get("entry_id") == entry_id:
            prefix_dir = os.path.join(ctx.prefix_root_dir(), "local-" + match.group(1))
            if os.path.isdir(prefix_dir) and not os.path.islink(prefix_dir):
                trash.append(prefix_dir)
        for path in trash:
            try:
                files.gio_trash(path)
            except (OSError, ValueError) as e:
                _log.warning("%s: %s nicht in den Papierkorb: %s", self.NAME, path, e)


class GogDownloads(_FolderQueue):
    """GOG-Spiele laden und installieren, eines nach dem anderen (store/gog_install.py). Der Spielordner
    GOG/<Titel> bekommt beim Anlegen unseren Marker (E99): Daran erkennt ihn der Papierkorb des
    Installers (Deinstallieren) und nach einem Neustart das Fortsetzen."""
    STORE, FILE, KEY = "gog", "store-gog.json", "id"
    SAVED = ("id", "title", "state", "percent", "target", "entry_id", "token")
    STATES = GOG_STATES
    LEVEL, SETUP_DIR, NAME, MARKER_STORE = "GOG", gog_install.SETUP_DIR, "GOG", "gog"
    LOG_NAME, REGISTRY = "gog-install", "gog-installed.json"
    EXPECTED = (gog_account.GogError, OSError)

    def __init__(self, wiring: Any, account: "gog_account.GogAccount", clock=time.time):
        self.account = account
        self.clock = clock
        super().__init__(wiring)

    def _valid(self, raw: dict) -> bool:
        return isinstance(raw.get("id"), int) and not isinstance(raw.get("id"), bool) and raw["id"] > 0

    def _transient(self, e: BaseException) -> bool:
        return _transient(e)

    def _retries(self) -> tuple:
        return GOG_RETRY_S

    def _marker_extra(self, job: dict) -> dict:
        return {"product": job["id"]}

    def add(self, product_id: int, title: str) -> None:
        with self.lock:
            if not any(j["id"] == product_id for j in self.queue):
                self.queue.append({"id": product_id, "title": title, "state": "queued", "percent": 0,
                                   "cancel": threading.Event()})
                self._save()
        self.start()

    @classmethod
    def installed(cls) -> dict:
        """{GOG-Produkt: Eintrag der Bibliothek} der über den Store installierten Spiele."""
        try:
            with open(cls.registry_file(), encoding="utf-8") as fh:
                data = json.load(fh)
            return {int(k): str(v) for k, v in data.items() if str(k).isdigit()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, product_id: int, entry_id: str) -> None:
        data = {str(k): v for k, v in self.installed().items()}
        data[str(product_id)] = entry_id
        tmp = self.registry_file() + ".neu"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.registry_file())

    def _install(self, job: dict) -> str:
        cancel = job["cancel"]
        product = self.account.get(gog_install.DOWNLOADS % job["id"])
        pick = gog_install.pick_installer(product)
        if pick is None:
            raise gog_install.InstallError("Zu diesem Spiel gibt es keinen Installer für Windows oder Linux.")
        root = store_root(self.app, self.LEVEL)
        os.makedirs(root, exist_ok=True)
        target = self._resume_target(job, root)
        have = gog_install.folder_bytes(os.path.join(target, gog_install.SETUP_DIR)) if target else 0
        import shutil
        if shutil.disk_usage(root).free < pick["total"] * 2.2 - have + 2 * 10**9:
            raise gog_install.InstallError("Auf %s ist nicht genug Platz (%d GB nötig)."
                                           % (root, round((pick["total"] * 2.2 - have) / 1e9)))
        if target:
            _log.info("GOG: %s setzt fort in %s (%d MB schon da)", job["title"], target, have // 2**20)
            self._set(job, target=target)
        else:
            target = self._new_target(job, root)
        setup = os.path.join(target, gog_install.SETUP_DIR)
        os.makedirs(setup, exist_ok=True)
        total = max(1, pick["total"])
        done = [0]
        last = [0.0]
        rate = _Rate()
        job["rate"] = rate

        def progress(n: int) -> None:
            done[0] += n
            job["tries"] = 0                    # es kommt wieder etwas: die Wartezeiten fangen neu an
            if time.monotonic() - last[0] > 0.5:
                last[0] = time.monotonic()
                rate.update(done[0])
                self._set(job, state="downloading", percent=min(99, int(done[0] * 100 / total)),
                          done=done[0], total=pick["total"])
        first = ""
        for f in pick["files"]:
            if cancel.is_set():
                raise gog_install.InstallError("Abgebrochen.")
            link = self.account.get("https://api.gog.com" + f["downlink"] if f["downlink"].startswith("/") else f["downlink"])
            url = str(link.get("downlink") or "")
            if not url.startswith("https://"):
                raise gog_install.InstallError("GOG hat keine Download-Adresse geliefert.")
            dest = os.path.join(setup, gog_install.file_name(url))
            if os.path.isfile(dest):
                done[0] += os.path.getsize(dest)        # schon geladen (Fortsetzen nach einem Neustart)
            gog_install.download(url, dest, f["size"], progress, cancel)
            want = gog_install.expected_md5(str(link.get("checksum") or ""))
            if want and gog_install.md5_of(dest) != want:
                raise gog_install.InstallError("Eine geladene Datei ist beschädigt (Prüfsumme). Versuche es noch einmal.")
            first = first or dest
        self._set(job, state="installing", percent=100)
        entry_id = job["entry_id"]
        if pick["os"] == "windows":
            self._run_windows_installer(job, entry_id, first, target)
            task = gog_install.play_task(target, job["id"])
            platform = "windows"
        else:
            self._unpack_linux(first, target)
            task = {"exe": "data/noarch/start.sh", "args": "", "working_dir": ""}
            platform = "linux"
        if cancel.is_set():
            raise gog_install.InstallError("Abgebrochen.")
        from . import files, wiring_library
        try:
            files.gio_trash(setup)
        except (OSError, ValueError) as e:
            _log.warning("GOG: Installer-Dateien nicht in den Papierkorb: %s", e)
        record = {"id": entry_id, "title": job["title"], "platform": platform, "install_dir": target,
                  "exe": (task or {}).get("exe", ""), "args": (task or {}).get("args", ""),
                  "archive": "GOG", "marker": job["token"]}
        added = wiring_library.add_local_record(self.app, record)
        if added:
            self._remember(job["id"], added)
        _log.info("GOG: %s installiert nach %s (%s)", job["title"], target, record["exe"] or "Startdatei offen")
        return target

    def _run_windows_installer(self, job: dict, entry_id: str, installer: str, target: str) -> None:
        """Installer unsichtbar über Proton, im Prefix, den das Spiel später benutzt."""
        rc = self._run_proton(job, entry_id, installer, [installer] + gog_install.installer_args(target))
        if rc != 0 or gog_install.play_task(target, job["id"]) is None and not any(
                n.lower().endswith(".exe") for n in os.listdir(target)):
            raise gog_install.InstallError("Der GOG-Installer ist nicht durchgelaufen (Rückgabewert %s)." % rc)

    def _unpack_linux(self, installer: str, target: str) -> None:
        from . import system
        r = system.run_write(["unzip", "-o", "-q", installer, "data/noarch/*", "-d", target], timeout=3600.0, note="gog")
        if r.returncode not in (0, 1) or not os.path.isfile(os.path.join(target, "data/noarch/start.sh")):
            raise gog_install.InstallError("Der Linux-Installer ließ sich nicht entpacken.")


class EpicDownloads(_StoreQueue):
    """Epic-Spiele über legendary laden und installieren, eines nach dem anderen (E73). Fertige
    Spiele werden eigene Einträge der Bibliothek (Quelle local, detail.epic = App-Name in legendary).
    Die Warteschlange liegt in <state>/store-epic.json; legendary setzt einen Download selbst fort (E99).
    Ist die Anmeldung inzwischen abgelaufen, wartet der Auftrag („login“), bis du dich anmeldest."""
    STORE, FILE, KEY = "epic", "store-epic.json", "app"
    SAVED = ("app", "title", "ns", "kind", "entry", "state", "percent", "target")
    STATES = EPIC_STATES

    def __init__(self, wiring: Any, legendary: "epic_account.Legendary"):
        self.legendary = legendary
        super().__init__(wiring)

    def _valid(self, raw: dict) -> bool:
        return isinstance(raw.get("app"), str) and bool(raw["app"]) and raw.get("kind") in ("install", "update", "uninstall")

    def _restored(self, job: dict) -> dict | None:
        job = super()._restored(job)
        job.setdefault("ns", "")
        job.setdefault("entry", "")
        if job["kind"] == "uninstall":
            job["state"] = "removing"
        return job

    def add(self, app_name: str, title: str, namespace: str, kind: str = "install", entry_id: str = "") -> None:
        with self.lock:
            if not any(j["app"] == app_name for j in self.queue):
                self.queue.append({"app": app_name, "title": title, "ns": namespace, "kind": kind, "entry": entry_id,
                                   "state": "removing" if kind == "uninstall" else "queued", "percent": 0,
                                   "cancel": threading.Event()})
                self._save()
        self.start()

    def cancel(self, app_name: str) -> bool:
        with self.lock:
            job = next((j for j in self.queue if j["app"] == app_name and j["kind"] in ("install", "update")), None)
            if job is None:
                return False
            job["cancel"].set()
            job["state"] = "cancelling"
            self._save()
        self.start()                            # auch ein Auftrag, der auf die Anmeldung wartet, wird so beendet
        return True

    def stop(self) -> None:
        """Ende des Home: legendary anhalten (es merkt sich den Stand), nichts wegräumen, die Datei
        behält den Stand von vorher (E99)."""
        self.closing = True
        with self.lock:
            for job in self.queue:
                job["cancel"].set()

    def view(self, job: dict) -> dict | None:
        if job["kind"] not in ("install", "update"):
            return None
        out = super().view(job)
        out.update(id=job["ns"] + ":", app=job["app"], kind=job["kind"],
                   text=("Update · " if job["kind"] == "update" else "") + EPIC_STATES.get(out["state"], ""))
        stats = job.get("stats") or {}
        if out["state"] == "downloading" and stats:
            out.update(done=int(stats.get("done") or 0), total=int(stats.get("total") or 0),
                       speed=int(stats.get("speed") or 0), eta=int(stats.get("eta") or 0))
        return out

    @staticmethod
    def _needs_login(job: dict) -> bool:
        return job["kind"] != "uninstall" and not job["cancel"].is_set()

    def _next(self) -> dict | None:
        """Der nächste Auftrag. Ist die Anmeldung abgelaufen (etwa nach einem Neustart), warten Laden und
        Aktualisieren („login“) statt zu scheitern; Deinstallieren und Abbrechen brauchen keine Anmeldung
        und laufen trotzdem (E99). Die Anmeldung im Store startet die Warteschlange wieder
        (extend: epic_take_code), dann warten alle wieder normal."""
        with self.lock:
            asks = any(self._needs_login(j) for j in self.queue)
        logged = self.legendary.logged_in() if asks else True       # liest nur eine Datei
        with self.lock:
            if not self.queue or self.closing:
                self.running = False
                self.current = None
                return None
            changed = False
            for j in self.queue:
                state = j.get("state")
                if logged and state == "login":
                    j["state"] = "queued"
                    changed = True
                elif not logged and self._needs_login(j) and state != "login":
                    j["state"] = "login"
                    changed = True
            job = self.queue[0] if logged else next((j for j in self.queue if not self._needs_login(j)), None)
            if changed:
                self._save()
            if job is None:
                self.running = False
            self.current = job
        if changed:
            self.wiring.main.post(self.publish)
        return job

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            try:
                if job["cancel"].is_set():
                    raise epic_account.EpicError("Abgebrochen.")       # abgebrochen, bevor er lief
                if job["kind"] == "uninstall":
                    self._uninstall(job)
                    self._done(job, "„%s“ ist deinstalliert" % job["title"], "Spielstände im Prefix bleiben erhalten.", True)
                elif job["kind"] == "update":
                    self._update(job)
                    self._done(job, "„%s“ ist aktuell" % job["title"], "Das Update ist installiert.", True)
                    watch = getattr(self.wiring, "update_watch", None)
                    if watch is not None:
                        self.wiring._bg(watch.check)    # Zeile und Punkt am Zahnrad gleich aktualisieren
                else:
                    self._install(job)
                    self._done(job, "„%s“ ist installiert" % job["title"], "Du findest es jetzt in der Bibliothek.", True)
            except epic_account.EpicError as e:
                if self.closing:
                    self._halt()
                    return
                cancelled = job["cancel"].is_set()
                _log.warning("Epic: %s: %s", job["title"], e)
                if cancelled and job["kind"] == "install":
                    self._cleanup(job)
                if cancelled and job["kind"] == "update":
                    # das Spiel bleibt in der alten Fassung, legendary setzt das Update später fort
                    self._done(job, "Update abgebrochen", "„%s“ bleibt in der bisherigen Fassung installiert." % job["title"], True)
                elif cancelled:
                    self._done(job, "Download abgebrochen", "„%s“ wird nicht installiert." % job["title"], True)
                else:
                    self._done(job, "„%s“: das hat nicht geklappt" % job["title"], str(e), False)
            except Exception as e:  # noqa: BLE001
                if self.closing:
                    self._halt()
                    return
                _log.exception("Epic: %s", job["title"])
                self._done(job, "„%s“: das hat nicht geklappt" % job["title"], str(e), False)

    @staticmethod
    def registry_file() -> str:
        return os.path.join(paths.state_dir(), "epic-installed.json")

    @classmethod
    def installed(cls) -> dict:
        """{App-Name: {entry, namespace, title}} der über den Store installierten Epic-Spiele."""
        try:
            with open(cls.registry_file(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def _write_registry(self, data: dict) -> None:
        tmp = self.registry_file() + ".neu"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.registry_file())

    def _watchers(self, job: dict):
        """(progress, stats) für legendary: Prozent, Ziel und Zahlen in den Auftrag."""
        last = [0.0]

        def progress(p: int) -> None:
            if time.monotonic() - last[0] > 0.5 or p >= 100:
                last[0] = time.monotonic()
                target = self.legendary.last_target if getattr(self.legendary, "last_app", "") == job["app"] else ""
                more = {"target": target} if target and target != job.get("target") else {}
                self._set(job, save=bool(more), state="downloading", percent=p, **more)

        def stats(numbers: dict) -> None:
            with self.lock:
                job["stats"] = numbers
        return progress, stats

    def _install(self, job: dict) -> None:
        self._set(job, state="preparing")
        root = store_root(self.app, "Epic")
        os.makedirs(root, exist_ok=True)
        progress, stats = self._watchers(job)
        # legendary hängt an das Protokoll an (epic_account._stream): hier gekappt (E99)
        info = self.legendary.install(job["app"], root, progress, job["cancel"], log.capped(paths.app_log("epic-install")),
                                      on_stats=stats)
        if job["cancel"].is_set():
            raise epic_account.EpicError("Abgebrochen.")
        self._set(job, state="installing", percent=100)
        from . import wiring_library
        from .library import local as local_mod
        install_dir = str(info.get("install_path") or "")
        exe = str(info.get("executable") or "").replace("\\", "/")
        old = self.installed().get(job["app"]) or {}
        record = local_mod.make_record(job["title"], "windows", install_dir, exe, archive="Epic",
                                       entry_id=old.get("entry") or None, epic=job["app"])
        added = wiring_library.add_local_record(self.app, record)
        if not added:
            raise epic_account.EpicError("Das Spiel ist installiert, ließ sich aber nicht in die Bibliothek eintragen.")
        data = self.installed()
        data[job["app"]] = {"entry": added, "namespace": job["ns"], "title": job["title"]}
        self._write_registry(data)
        _log.info("Epic: %s installiert nach %s (%s)", job["title"], install_dir, exe)

    def _update(self, job: dict) -> None:
        self._set(job, state="preparing")
        progress, stats = self._watchers(job)
        self.legendary.update(job["app"], progress, job["cancel"], log.capped(paths.app_log("epic-install")), on_stats=stats)
        _log.info("Epic: %s aktualisiert", job["title"])

    def _uninstall(self, job: dict) -> None:
        self.legendary.uninstall(job["app"])
        from . import wiring_library
        entry_id = job.get("entry") or (self.installed().get(job["app"]) or {}).get("entry") or ""
        if entry_id:
            wiring_library.remove_local(self.app, entry_id)
        data = self.installed()
        data.pop(job["app"], None)
        self._write_registry(data)
        _log.info("Epic: %s deinstalliert", job["title"])

    def _cleanup(self, job: dict) -> None:
        """Nach „Abbrechen“: halb geladenen Spielordner in den Papierkorb (legendary vergisst ihn dann).
        Nur einen echten Ordner direkt im Epic-Ordner des Stores: Das Ziel kann aus der Datei von vor
        einem Neustart stammen (E99)."""
        from . import files
        if self.legendary.installed().get(job["app"]) is not None:
            return
        target = str(job.get("target") or "") or (
            self.legendary.last_target if getattr(self.legendary, "last_app", "") == job["app"] else "")
        root = os.path.realpath(store_root(self.app, "Epic"))
        if not target or os.path.islink(target) or not os.path.isdir(target) \
                or os.path.dirname(os.path.realpath(target)) != root:
            return
        try:
            files.gio_trash(target)
        except (OSError, ValueError) as e:
            _log.warning("Epic: %s nicht in den Papierkorb: %s", target, e)


# ---------------------------------------------------------------- RPC
def extend(wiring: Any) -> None:
    app = wiring.app
    rpc = app.rpc
    ctx = getattr(wiring, "sources", None)
    if ctx is None:
        from .library.sources import SourceContext
        ctx = SourceContext()
    shop = steam_store.SteamStore(os.path.join(paths.cache_dir(), "store", "steam"))
    downloads = SteamDownloads(wiring, ctx)
    wiring.store_downloads = downloads
    wiring.store_publish_jobs = lambda stores, jobs: publish_jobs(app, set(stores), jobs)    # Hauptschleife
    account = {"at": 0.0, "data": None, "good": None}
    account_lock = threading.Lock()

    def get_account(force: bool = False) -> dict:
        with account_lock:
            data = account["data"]
            ttl = ERROR_TTL_S if data is not None and data.get("error") else ACCOUNT_TTL_S
            if not force and data is not None and time.monotonic() - account["at"] < ttl:
                return data
            good = account["good"]
        cookies = steam_account.browser_cookies(os.path.join(paths.data_dir(), "browser", "cookies.sqlite"))
        data = steam_account.userdata(cookies)
        if data.get("error") and good is not None:
            # Netz kurz weg (E99): der letzte gute Stand gilt, nach ERROR_TTL_S wird wieder gefragt
            data = dict(good, error=True)
        with account_lock:
            account.update(at=time.monotonic(), data=data)
            if not data.get("error"):
                account["good"] = data
        return data

    def annotate(cards: list[dict]) -> list[dict]:
        acct = get_account()
        info = steam_install.manifests(ctx)
        queued = {j["appid"] for j in downloads.jobs()}
        for c in cards:
            appid = int(c["id"])
            m = info.get(appid)
            # installiert = Bit 4 wie in der Bibliothek (E99): ein offenes Update macht daraus keinen Download
            c["installed"] = steam_install.present(m)
            c["update_pending"] = steam_install.update_pending(m)
            c["downloading"] = appid in queued or (bool(m) and not c["installed"])
            c["owned"] = appid in acct["owned"] or c["installed"]
            c["wishlist"] = appid in acct["wishlist"]
        return cards

    def steam_owned() -> dict:
        """Reiter „Meine Spiele“: was dir gehört (nur Spiele), Namen aus Steams Zwischenspeicher."""
        acct = get_account()
        cards = [steam_store.SteamStore.card(appid, name) for appid, name in steam_install.owned_games(ctx, acct["owned"])]
        for c in cards:
            c["price"] = ""
        return {"id": "owned", "title": "Meine Spiele", "items": annotate(cards)}

    def steam_front(params: dict) -> dict:
        """Anmeldung, Genres und „Meine Spiele“; die Listen holt die Seite mit store.list (E70)."""
        acct = get_account()
        return {"ok": True, "sections": [steam_owned()], "loggedIn": acct["logged_in"], "genres": steam_store.genres()}

    def steam_list(params: dict) -> dict:
        """Eine Seite eines Reiters oder der Suche (E70): {items, total, more}."""
        try:
            page = shop.browse(str(params.get("tab") or ""), int(params.get("page") or 0),
                               str(params.get("genre") or ""), str(params.get("query") or ""))
        except (steam_store.StoreError, ValueError) as e:
            return {"ok": False, "message": str(e), "items": [], "total": 0, "more": False}
        annotate(page["items"])
        return {"ok": True, **page}

    def steam_delisted(appid: int, info: dict | None) -> dict:
        """Eigenes Spiel, das der Shop nicht (mehr) führt (E99): eine knappe Seite aus Steams appinfo,
        installierbar wie jedes andere (wie bei Epic)."""
        name = steam_install.app_name(ctx, appid) or str((info or {}).get("name") or "") or "Steam-Spiel %d" % appid
        d = steam_store.SteamStore.card(appid, name)
        d.update(type="game", short="", about="", hero=steam_store.CDN % (appid, "library_hero.jpg"),
                 cover=steam_store.CDN % (appid, "library_600x900.jpg"), screenshots=[], free=False, buyable=False,
                 release="", coming_soon=False, developers=[], publishers=[], genres=[], metacritic=0, controller="",
                 linux=False, size_gb=0.0, url="%s/app/%d/" % (steam_store.SHOP, appid), deck="", protondb="",
                 hint="Der Steam-Shop führt dieses Spiel nicht mehr. Installieren geht trotzdem.")
        try:
            real = shop.assets([appid]).get(appid) or {}
        except steam_store.StoreError:
            real = {}
        d.update({k: real[k] for k in ("hero", "cover") if real.get(k)})
        if real.get("header"):
            d["image"] = real["header"]
        return d

    def steam_item(params: dict) -> dict:
        appid = _appid(params)
        if appid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        try:
            d = shop.details(appid)
            d["deck"] = shop.deck(appid)
            d["protondb"] = shop.protondb(appid)
        except steam_store.NotListed as e:
            # nur, wenn der Shop wirklich nichts liefert; ohne Netz bleibt es bei der Meldung (E99)
            info = steam_install.manifests(ctx).get(appid)
            if appid not in get_account()["owned"] and not steam_install.present(info):
                return {"ok": False, "message": str(e)}
            d = steam_delisted(appid, info)
        except steam_store.StoreError as e:
            return {"ok": False, "message": str(e)}
        annotate([d])
        job = next((j for j in downloads.jobs() if j["appid"] == appid), None)
        d["job"] = {"percent": job.get("percent", 0), "state": job.get("state"), "text": STATES.get(job.get("state"), "")} if job else None
        d["entryId"] = "steam:%d" % appid
        d["loggedIn"] = get_account()["logged_in"]
        libs = steam_install.libraries(ctx)
        lib = steam_install.pick_library(libs, str(app.config.get("store.steam_library", "") or ""), paths.games_disk())
        d["library"] = {"path": lib["path"], "free_gb": round(lib["free"] / 2**30)} if lib else None
        return {"ok": True, "item": d}

    def steam_install_rpc(params: dict) -> dict:
        appid = _appid(params)
        if appid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        acct_data = get_account()
        info = steam_install.manifests(ctx).get(appid)
        if steam_install.present(info):
            # E99: auch mit offenem Update; ein Auftrag dafür räumte beim Abbrechen den Eintrag weg
            return {"ok": False, "message": "Das Spiel ist schon installiert."
                    + (" Steam lädt das Update vor dem nächsten Start." if steam_install.update_pending(info) else "")}
        if appid not in acct_data["owned"]:
            if acct_data.get("error"):
                return {"ok": False, "message": "Steam ist gerade nicht erreichbar. Versuche es gleich noch einmal."}
            return {"ok": False, "message": "Dieses Spiel gehört dir (noch) nicht. Kaufe es zuerst."}
        try:
            d = shop.details(appid)
            name, need = d["title"], float(d.get("size_gb") or 0) * 1e9
        except steam_store.NotListed as e:
            # Eigenes Spiel, das der Shop nicht mehr führt (E99): Name aus appinfo, Größe unbekannt
            name, need = steam_install.app_name(ctx, appid), 0.0
            if not name:
                return {"ok": False, "message": str(e)}
        except steam_store.StoreError as e:
            return {"ok": False, "message": str(e)}             # Netz: nicht ohne Platzprüfung
        libs = steam_install.libraries(ctx)
        lib = steam_install.pick_library(libs, str(app.config.get("store.steam_library", "") or ""), paths.games_disk())
        if lib is None:
            return {"ok": False, "message": "Keine Steam-Bibliothek, in die installiert werden kann."}
        if need and lib["free"] < need * 1.1:
            return {"ok": False, "message": "Auf %s ist nicht genug Platz (%d GB frei, %d GB nötig)."
                    % (lib["path"], lib["free"] // 2**30, round(need / 1e9))}
        downloads.add(appid, name, lib["path"], steam_install.install_dir_name(ctx, appid, name))
        _log.info("Store: %s (%d) wird installiert nach %s", name, appid, lib["path"])
        return {"ok": True, "message": "„%s“ wird im Hintergrund installiert. Den Fortschritt siehst du unter Downloads." % name}

    def steam_uninstall(params: dict) -> dict:
        appid = _appid(params)
        if appid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        if wiring.main.call(app.procs.by_entry, "steam:%d" % appid) is not None:     # läuft im Arbeits-Thread
            return {"ok": False, "message": "Schließe das Spiel zuerst, dann lässt es sich deinstallieren."}
        info = steam_install.manifests(ctx).get(appid)
        if info is None:
            return {"ok": False, "message": "Das Spiel ist nicht installiert."}
        name = info["name"] or str(params.get("title") or "Das Spiel")
        downloads.uninstall(appid, name, info["lib"])
        _log.info("Store: %s (%d) wird deinstalliert", name, appid)
        return {"ok": True, "message": "„%s“ wird deinstalliert. Steam wird dafür kurz beendet." % name}

    def steam_cancel(params: dict) -> dict:
        appid = _appid(params)
        job = next((j for j in downloads.jobs() if j["appid"] == appid), None) if appid is not None else None
        if job is not None and job.get("uninstall"):
            return {"ok": False, "message": "Die Deinstallation läuft schon und lässt sich nicht abbrechen."}
        if appid is None or not downloads.cancel(appid):
            return {"ok": False, "message": "Dieser Download läuft nicht."}
        return {"ok": True, "message": "Der Download wird abgebrochen."}

    def open_url(url: str, close_other: bool = False, close_after: str = "") -> Any:
        """Adresse im Browser des PC5 Launchers. close_after "steam-login"/"gog-login": Der
        Browser schließt sich nach der Anmeldung selbst (zurück in den Store)."""
        from .launch import apps
        running = app.procs.by_entry("app:browser")
        if running is not None:
            try:
                tmp = paths.browser_open_file() + ".neu"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"url": url, "at": time.time(), "close": close_after}, fh)
                os.replace(tmp, paths.browser_open_file())
            except OSError as e:
                return {"ok": False, "message": "Der Browser lässt sich gerade nicht ansteuern: %s" % e}
            app.shell.focus_app(running)
            return {"ok": True}
        apps.PENDING_OPEN["app:browser"] = (url, time.monotonic(), close_after)
        try:
            # Läuft etwas anderes vorn, fragt die Seite wie bei jedem Start (needsConfirm, closeOther)
            return app.shell.launch("app:browser", close_other=close_other)
        except Exception as e:  # noqa: BLE001
            apps.PENDING_OPEN.pop("app:browser", None)
            return {"ok": False, "message": getattr(e, "message", str(e))}

    # ------------------------------------------------------------ Kauf beobachten (E71)
    watching: dict = {}                         # (shop, Nummer) -> Ende der Beobachtung

    extra_owned: dict = {}                      # weitere Shops (wiring_launchers, E94): shop -> fn(pid) -> bool

    def owned_now(store_id: str, pid: Any) -> bool:
        try:
            if store_id in extra_owned:
                return bool(extra_owned[store_id](pid))
            if store_id == "gog":
                return pid in gog_owned(force=True)
            if store_id == "epic":
                return pid in {c["id"].split(":")[0] for c in epic_owned(force=True)}
            return pid in get_account(force=True)["owned"]
        except Exception as e:                  # Netz weg: beim nächsten Schritt wieder
            _log.debug("Store: Besitz von %s %s unbekannt: %s", store_id, pid, e)
            return False

    def watch_purchase(store_id: str, pid: Any, title: str) -> None:
        """Nach „Kaufen“: alle PURCHASE_STEP_S nachsehen, ob das Spiel jetzt dir gehört. Dann den
        Browser schließen, melden und den Store neu zeigen lassen (Wunsch 2026-10-19: Browser blieb
        nach dem Kauf offen, das Spiel stand erst nach einem Umweg unter „Meine Spiele“)."""
        key = (store_id, pid)
        if key in watching:
            watching[key] = time.monotonic() + PURCHASE_WATCH_S
            return
        watching[key] = time.monotonic() + PURCHASE_WATCH_S
        if owned_now(store_id, pid):
            watching.pop(key, None)             # gehörte schon (etwa nur angesehen): nichts zu tun
            return
        away_since = None
        try:
            while time.monotonic() < watching.get(key, 0):
                time.sleep(PURCHASE_STEP_S)
                try:
                    running = wiring.main.call(lambda: app.procs.by_entry("app:browser"))
                except RuntimeError:            # das Home endet
                    return
                if owned_now(store_id, pid):
                    _log.info("Store: %s (%s %s) gekauft", title, store_id, pid)

                    def done() -> None:
                        browser = app.procs.by_entry("app:browser")
                        if browser is not None:
                            app.shell.close_app(browser)
                        app.notify.push("„%s“ gehört jetzt dir" % title, "Du kannst es jetzt installieren.", icon="check")
                        rpc.emit("store.owned", {"store": store_id, "id": str(pid)})
                    wiring.main.post(done)
                    return
                # Browser zu (ohne Kauf): noch eine Minute weiter nachsehen, dann aufhören
                away_since = None if running is not None else (away_since or time.monotonic())
                if away_since and time.monotonic() - away_since > 60:
                    return
        finally:
            watching.pop(key, None)

    def steam_buy(params: dict) -> Any:
        appid = _appid(params)
        if appid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        with account_lock:
            account["at"] = 0.0                 # nach dem Kauf neu fragen
        threading.Thread(target=watch_purchase, args=("steam", appid, str(params.get("title") or "Das Spiel")),
                         name="couch-store-kauf", daemon=True).start()
        return open_url("%s/app/%d/" % (steam_store.SHOP, appid), bool(params.get("closeOther")))

    def steam_login(params: dict) -> Any:
        with account_lock:
            account["at"] = 0.0
        return open_url(steam_account.LOGIN, bool((params or {}).get("closeOther")), "steam-login")

    # ------------------------------------------------------------ GOG (Teil 2)
    gog = gog_store.GogStore(os.path.join(paths.cache_dir(), "store", "gog"))
    gog_acct = gog_account.GogAccount(os.path.join(paths.config_dir(), "gog-token.json"),
                                      os.path.join(paths.state_dir(), "gog-login.json"))
    gog_dl = GogDownloads(wiring, gog_acct)
    owned_gog = {"at": 0.0, "ids": set(), "error": False}

    def gog_owned(force: bool = False) -> set:
        try:
            if gog_acct.take_code():
                force = True
                _log.info("GOG: angemeldet")
        except gog_account.GogError as e:
            _log.warning("GOG-Anmeldung: %s", e)
        ttl = ERROR_TTL_S if owned_gog["error"] else ACCOUNT_TTL_S
        if force or time.monotonic() - owned_gog["at"] > ttl:
            try:
                owned_gog.update(at=time.monotonic(), ids=gog_acct.owned(), error=False)
            except gog_account.GogError as e:
                # Netz kurz weg (E99): der letzte gute Stand gilt, gleich wieder fragen
                _log.warning("GOG: eigene Spiele unbekannt: %s", e)
                owned_gog.update(at=time.monotonic(), error=True)
        return owned_gog["ids"]

    def gog_annotate(cards: list[dict]) -> list[dict]:
        owned = gog_owned()
        done = GogDownloads.installed()
        busy = {j["id"] for j in gog_dl.jobs()}
        for c in cards:
            pid = int(c["id"])
            entry_id = done.get(pid)
            c["installed"] = bool(entry_id) and app.library.get(entry_id) is not None
            c["downloading"] = pid in busy
            c["owned"] = pid in owned or c["installed"]
            c["wishlist"] = False
            c["entryId"] = entry_id or ""
        return cards

    def gog_front(params: dict) -> dict:
        owned = gog.products(sorted(gog_owned())) if gog_acct.logged_in() else []
        return {"ok": True, "sections": [{"id": "owned", "title": "Meine Spiele", "items": gog_annotate(owned)}],
                "loggedIn": gog_acct.logged_in(), "genres": gog_store.genres()}

    def gog_list(params: dict) -> dict:
        try:
            page = gog.browse(str(params.get("tab") or ""), int(params.get("page") or 0),
                              str(params.get("genre") or ""), str(params.get("query") or ""))
        except (steam_store.StoreError, ValueError) as e:
            return {"ok": False, "message": str(e), "items": [], "total": 0, "more": False}
        gog_annotate(page["items"])
        return {"ok": True, **page}

    def gog_item(params: dict) -> dict:
        pid = _appid(params)
        if pid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        try:
            d = gog.details(pid, params.get("card") if isinstance(params.get("card"), dict) else None)
        except steam_store.StoreError as e:
            return {"ok": False, "message": str(e)}
        gog_annotate([d])
        job = next((j for j in gog_dl.jobs() if j["id"] == pid), None)
        d["job"] = {"percent": job.get("percent", 0), "state": job["state"], "text": GOG_STATES.get(job["state"], "")} if job else None
        d["loggedIn"] = gog_acct.logged_in()
        d["library"] = target_view(app, "GOG")
        return {"ok": True, "item": d}

    def gog_install_rpc(params: dict) -> dict:
        pid = _appid(params)
        if pid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        if not gog_acct.logged_in():
            return {"ok": False, "message": "Melde dich zuerst bei GOG an."}
        if pid not in gog_owned(force=True):
            if not gog_acct.logged_in():                # GOG hat die Anmeldung eben abgelehnt (E99)
                return {"ok": False, "message": "Deine Anmeldung bei GOG ist abgelaufen. Melde dich neu an."}
            if owned_gog["error"]:
                return {"ok": False, "message": "GOG ist gerade nicht erreichbar. Versuche es gleich noch einmal."}
            return {"ok": False, "message": "Dieses Spiel gehört dir (noch) nicht. Kaufe es zuerst."}
        try:
            title = gog.details(pid)["title"]
        except steam_store.StoreError as e:
            return {"ok": False, "message": str(e)}
        gog_dl.add(pid, title)
        _log.info("Store: GOG %s (%d) wird installiert", title, pid)
        return {"ok": True, "message": "„%s“ wird im Hintergrund geladen und installiert. Den Fortschritt siehst du unter Downloads." % title}

    def gog_cancel(params: dict) -> dict:
        pid = _appid(params)
        if pid is None or not gog_dl.cancel(pid):
            return {"ok": False, "message": "Dieser Download läuft nicht."}
        return {"ok": True, "message": "Der Download wird abgebrochen."}

    def gog_buy(params: dict) -> Any:
        pid = _appid(params)
        if pid is None:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        owned_gog["at"] = 0.0
        slug = str(params.get("slug") or "")
        if not slug:
            try:
                slug = gog.details(pid)["slug"]
            except steam_store.StoreError:
                slug = ""
        url = gog_store.SHOP % slug if slug else "https://www.gog.com/de/"
        threading.Thread(target=watch_purchase, args=("gog", pid, str(params.get("title") or "Das Spiel")),
                         name="couch-store-kauf", daemon=True).start()
        return open_url(url, bool(params.get("closeOther")))

    def gog_login(params: dict) -> Any:
        owned_gog["at"] = 0.0
        return open_url(gog_account.LOGIN_URL, bool((params or {}).get("closeOther")), "gog-login")

    # ------------------------------------------------------------ Epic (E73)
    epic = epic_store.EpicStore(os.path.join(paths.cache_dir(), "store", "epic"))
    legendary = epic_account.Legendary(os.path.join(paths.data_dir(), "tools"), os.path.join(paths.data_dir(), "epic"))
    epic_dl = EpicDownloads(wiring, legendary)
    wiring.epic_legendary, wiring.epic_downloads = legendary, epic_dl     # für die Updates (updates.py)
    epic_code = os.path.join(paths.state_dir(), "epic-login.json")
    owned_epic: dict = {"at": 0.0, "cards": [], "error": "", "busy": False}
    owned_epic_lock = threading.Lock()

    def epic_take_code() -> None:
        try:
            if legendary.take_code(epic_code):
                _log.info("Epic: angemeldet")
                owned_epic["at"] = 0.0
                epic_dl.start()                 # Aufträge, die auf die Anmeldung warten (E99)
        except epic_account.EpicError as e:
            _log.warning("Epic-Anmeldung: %s", e)
            owned_epic["error"] = str(e)

    def epic_refresh(announce: bool = False) -> None:
        with owned_epic_lock:
            try:
                owned_epic.update(at=time.monotonic(), cards=legendary.owned(), error="")
            except epic_account.EpicError as e:
                _log.warning("Epic: eigene Spiele: %s", e)
                owned_epic.update(at=time.monotonic(), error=str(e))
            finally:
                owned_epic["busy"] = False
        if announce:
            wiring.main.post(lambda: rpc.emit("store.owned", {"store": "epic", "id": ""}))

    def epic_owned(force: bool = False, wait: bool = True) -> list[dict]:
        """Eigene Spiele als Karten. wait=False blockiert nicht: Ist die Liste alt, holt ein Faden sie
        (legendary braucht beim ersten Mal lange) und meldet danach store.owned; die Seite lädt neu."""
        epic_take_code()
        if not legendary.logged_in():
            return []
        stale = force or time.monotonic() - owned_epic["at"] > ACCOUNT_TTL_S
        if stale and wait:
            epic_refresh()
        elif stale and not owned_epic["busy"]:
            owned_epic["busy"] = True
            threading.Thread(target=epic_refresh, args=(True,), name="couch-store-epic-liste", daemon=True).start()
        return list(owned_epic["cards"])

    def epic_mine(namespace: str) -> dict | None:
        return next((c for c in owned_epic["cards"] if c["id"].split(":")[0] == namespace), None)

    def epic_annotate(cards: list[dict]) -> list[dict]:
        owned = {c["id"].split(":")[0] for c in owned_epic["cards"]}
        done = {v.get("namespace"): v.get("entry") for v in EpicDownloads.installed().values()
                if v.get("entry") and app.library.get(v["entry"]) is not None}
        busy = {j["ns"] for j in epic_dl.jobs() if j["kind"] == "install"}
        for c in cards:
            ns = c["id"].split(":")[0]
            c["installed"] = ns in done
            c["entryId"] = done.get(ns) or ""
            c["downloading"] = ns in busy
            c["owned"] = ns in owned or c["installed"]
            c["wishlist"] = False
        return cards

    def epic_front(params: dict) -> dict:
        owned = epic_owned(wait=False)
        return {"ok": True, "sections": [{"id": "owned", "title": "Meine Spiele", "items": epic_annotate(owned)}],
                "loggedIn": legendary.logged_in(), "genres": epic_store.genres(), "ownedLoading": owned_epic["busy"]}

    def epic_list(params: dict) -> dict:
        epic_owned(wait=False)
        try:
            page = epic.browse(str(params.get("tab") or ""), int(params.get("page") or 0),
                               str(params.get("genre") or ""), str(params.get("query") or ""))
        except (steam_store.StoreError, ValueError) as e:
            return {"ok": False, "message": str(e), "items": [], "total": 0, "more": False}
        epic_annotate(page["items"])
        return {"ok": True, **page}

    def epic_item(params: dict) -> dict:
        item_id = str(params.get("id") or "")
        ns = item_id.split(":")[0]
        if not ns:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        epic_owned(wait=False)
        mine = epic_mine(ns)
        card = params.get("card") if isinstance(params.get("card"), dict) else None
        try:
            d = epic.details(item_id, card)
        except steam_store.StoreError as e:
            if mine is None:
                return {"ok": False, "message": str(e)}
            # Eigenes Spiel, das der Shop nicht (mehr) führt: was legendary weiß
            d = {**mine, "type": "game", "short": "", "about": "", "hero": mine["image"], "screenshots": [],
                 "release": "", "developers": [], "genres": [], "metacritic": 0, "controller": "", "deck": "",
                 "protondb": "", "linux": False, "windows": True, "installable": True, "coming_soon": False,
                 "size_gb": 0.0, "url": "", "buyable": False, "free": False}
        epic_annotate([d])
        d["loggedIn"] = legendary.logged_in()
        if mine is not None:
            d["app"] = mine["app"]
            if mine.get("third_party"):
                d["installable"] = False
                d["hint"] = "Dieses Spiel läuft über %s und lässt sich hier nicht installieren." % mine["third_party"]
        job = next((j for j in epic_dl.jobs() if j["ns"] == ns and j["kind"] == "install"), None)
        d["job"] = {"percent": job.get("percent", 0), "state": job["state"], "text": EPIC_STATES.get(job["state"], "")} if job else None
        d["library"] = target_view(app, "Epic")
        return {"ok": True, "item": d}

    def epic_install_rpc(params: dict) -> dict:
        ns = str(params.get("id") or "").split(":")[0]
        if not legendary.logged_in():
            return {"ok": False, "message": "Melde dich zuerst bei Epic an."}
        mine = epic_mine(ns)
        if mine is None:
            epic_owned(force=True)
            mine = epic_mine(ns)
        if mine is None:
            return {"ok": False, "message": "Dieses Spiel gehört dir (noch) nicht. Hole oder kaufe es zuerst."}
        if mine.get("third_party"):
            return {"ok": False, "message": "Dieses Spiel läuft über %s und lässt sich hier nicht installieren." % mine["third_party"]}
        epic_dl.add(mine["app"], mine["title"], ns)
        _log.info("Store: Epic %s (%s) wird installiert", mine["title"], mine["app"])
        return {"ok": True, "message": "„%s“ wird im Hintergrund geladen und installiert. Den Fortschritt siehst du unter Downloads." % mine["title"]}

    def epic_cancel(params: dict) -> dict:
        ns = str(params.get("id") or "").split(":")[0]
        # auch Updates (E99): Downloads zeigt sie, abbrechen ließen sie sich bisher nicht
        job = next((j for j in epic_dl.jobs() if j["ns"] == ns and j["kind"] in ("install", "update")), None)
        if job is None or not epic_dl.cancel(job["app"]):
            return {"ok": False, "message": "Dieser Download läuft nicht."}
        return {"ok": True, "message": "Das Update wird abgebrochen." if job["kind"] == "update" else "Der Download wird abgebrochen."}

    def epic_buy(params: dict) -> Any:
        item_id = str(params.get("id") or "")
        ns = item_id.split(":")[0]
        if not ns:
            return {"ok": False, "message": "Dieses Spiel gibt es nicht."}
        slug = str(params.get("slug") or "")
        if not slug:
            try:
                slug = epic.details(item_id)["slug"]
            except steam_store.StoreError:
                slug = ""
        url = epic_store.SHOP % slug if slug else "https://store.epicgames.com/de/"
        threading.Thread(target=watch_purchase, args=("epic", ns, str(params.get("title") or "Das Spiel")),
                         name="couch-store-kauf", daemon=True).start()
        return open_url(url, bool(params.get("closeOther")))

    def epic_login(params: dict) -> Any:
        owned_epic["at"] = 0.0
        return open_url(epic_account.LOGIN_URL, bool((params or {}).get("closeOther")), "epic-login")

    def epic_uninstall(params: dict) -> dict:
        reg = EpicDownloads.installed()
        entry_id = str(params.get("entry") or "")
        ns = str(params.get("id") or "").split(":")[0]
        app_name = next((a for a, v in reg.items() if (entry_id and v.get("entry") == entry_id) or (ns and v.get("namespace") == ns)), "")
        if not app_name and entry_id:
            entry = app.library.get(entry_id)
            app_name = str(entry.detail.get("epic") or "") if entry is not None else ""
        if not app_name:
            return {"ok": False, "message": "Dieses Spiel ist nicht über den Store installiert."}
        info = reg.get(app_name) or {}
        entry_id = entry_id or str(info.get("entry") or "")
        if entry_id and wiring.main.call(app.procs.by_entry, entry_id) is not None:
            return {"ok": False, "message": "Schließe das Spiel zuerst, dann lässt es sich deinstallieren."}
        title = str(info.get("title") or params.get("title") or "Das Spiel")
        epic_dl.add(app_name, title, str(info.get("namespace") or ns), kind="uninstall", entry_id=entry_id)
        return {"ok": True, "message": "„%s“ wird deinstalliert." % title}

    # ------------------------------------------------------------ nach Shop verteilen
    routes = {
        # store.search und store.account sind entfallen (E99): Die Suche läuft über store.list (E70)
        "store.front": ({"steam": steam_front, "gog": gog_front, "epic": epic_front}, True),
        "store.list": ({"steam": steam_list, "gog": gog_list, "epic": epic_list}, True),
        "store.item": ({"steam": steam_item, "gog": gog_item, "epic": epic_item}, True),
        "store.install": ({"steam": steam_install_rpc, "gog": gog_install_rpc, "epic": epic_install_rpc}, True),
        "store.cancel": ({"steam": steam_cancel, "gog": gog_cancel, "epic": epic_cancel}, False),
        "store.buy": ({"steam": steam_buy, "gog": gog_buy, "epic": epic_buy}, False),
        "store.login": ({"steam": steam_login, "gog": gog_login, "epic": epic_login}, False),
        "store.uninstall": ({"steam": steam_uninstall, "epic": epic_uninstall,
                             "gog": lambda p: {"ok": False, "message": "GOG-Spiele deinstallierst du in der Bibliothek."}}, True),
    }

    def route(fns: dict, in_bg: bool):
        def call(params):
            params = params or {}
            # Steam nur ohne Angabe; ein Shop, dessen Teil fehlt (wiring_itch darf scheitern), landete
            # sonst still bei Steam: Reiter „itch.io“ mit dem Steam-Store, Kaufen über steam_buy (E100)
            fn = fns.get(str(params.get("store") or "steam"))
            if fn is None:
                return {"ok": False, "message": MSG_NO_SHOP}
            return wiring._bg(fn, params) if in_bg else fn(params)
        return call

    for name, (fns, in_bg) in routes.items():
        rpc.register(name, route(fns, in_bg))
    # Weitere Shops hängen sich hier ein (wiring_launchers, E94): Die Routen lesen ihre Tabellen bei
    # jedem Aufruf, Einträge, die später dazukommen, gelten sofort
    wiring.store_routes = {name: fns for name, (fns, _bg) in routes.items()}
    wiring.store_open_url = open_url
    wiring.store_watch_purchase = watch_purchase
    wiring.store_extra_owned = extra_owned

    art_memo: dict = {}

    def home_art(params: dict) -> dict:
        """Hintergrund der Store-Kachel im Home (Wunsch 2026-10-21): das breite Bild eines der ersten
        zehn Steam-Topseller, jeden Tag ein anderes. Die echte Adresse kommt aus GetItems (neuere
        Spiele: Unterordner mit Prüfkennung). Für den Tag gemerkt. {ok, hero, title}."""
        if paths.test_mode():
            return {"ok": False}                # im Prüfmodus kein echtes Netz (Bilder vom Steam-CDN)
        day = int(time.time() // 86400)
        if art_memo.get("day") == day:
            return art_memo["result"]
        try:
            items = [c for c in shop.browse("top_sellers", 0)["items"] if str(c.get("id") or "").isdigit()][:10]
            heroes = shop.assets([int(c["id"]) for c in items])
        except (steam_store.StoreError, ValueError, KeyError):
            return {"ok": False}
        with_hero = [c for c in items if (heroes.get(int(c["id"])) or {}).get("hero")]
        if not with_hero:
            return {"ok": False}
        pick = with_hero[day % len(with_hero)]
        result = {"ok": True, "hero": heroes[int(pick["id"])]["hero"], "title": str(pick.get("title") or "")}
        art_memo.update(day=day, result=result)
        return result
    rpc.register("store.homeArt", lambda params: wiring._bg(home_art, params or {}))

    cards_memo: dict = {}

    def home_cards(params: dict) -> dict:
        """Karten unter der Store-Kachel im Home (Wunsch 2026-10-21: „mehr Infos als nur Öffnen,
        z. B. die Gratis-Spiele von Epic, besondere Angebote“): jetzt gratis bei Epic (höchstens
        zwei), das nächste Gratis-Spiel, das stärkste Angebot bei Steam und bei GOG; höchstens vier.
        Jede Quelle darf fehlen. {ok, cards: [{store, id, title, image, label, value, card}]}."""
        if paths.test_mode():
            return {"ok": True, "cards": []}    # im Prüfmodus kein echtes Netz
        now = time.time()
        if cards_memo.get("at", 0) > now - 1800:
            return cards_memo["result"]
        out: list = []

        def add(c: dict, label: str, value: str) -> None:
            if c.get("id") and c.get("image"):
                out.append({"store": c["store"], "id": str(c["id"]), "title": str(c.get("title") or ""),
                            "image": c["image"], "label": label, "value": value, "card": c})
        try:
            free = epic.free()
            for c in [c for c in free if c.get("free_now")][:2]:
                add(c, "Gratis bei Epic", c.get("price") or "")
            soon = next((c for c in free if not c.get("free_now")), None)
            if soon:
                add(soon, "Bald gratis bei Epic", soon.get("price") or "")
        except Exception as e:  # noqa: BLE001
            _log.warning("Store-Karten Epic: %s", e)

        def best(items: list) -> dict | None:
            # stärkster Rabatt unter den ersten zwanzig (die Listen sind nach Beliebtheit geordnet)
            deals = [c for c in items[:20] if int(c.get("discount") or 0) > 0 and c.get("price")]
            return max(deals, key=lambda c: int(c["discount"])) if deals else None
        try:
            specials = next((sec["items"] for sec in shop.front() if sec["id"] == "specials"), [])
            deal = best(specials)
            if deal:
                add(deal, "Angebot bei Steam · −%d %%" % deal["discount"], deal["price"])
        except Exception as e:  # noqa: BLE001
            _log.warning("Store-Karten Steam: %s", e)
        try:
            deal = best(gog.browse("gog_bestsellers", 0)["items"])
            if deal:
                add(deal, "Angebot bei GOG · −%d %%" % deal["discount"], deal["price"])
        except Exception as e:  # noqa: BLE001
            _log.warning("Store-Karten GOG: %s", e)
        result = {"ok": True, "cards": out[:4]}
        if out:
            cards_memo.update(at=now, result=result)
        return result
    rpc.register("store.homeCards", lambda params: wiring._bg(home_cards, params or {}))

    def jobs_busy() -> str | None:
        jobs = downloads.jobs()
        if jobs:
            return "Steam lädt gerade „%s“ für den Store." % jobs[0]["name"]
        gjobs = gog_dl.jobs()
        if gjobs:
            return "Der Store lädt gerade „%s“ von GOG." % gjobs[0]["title"]
        ejobs = [j for j in epic_dl.jobs() if j.get("state") != "login"]   # wartet nur auf die Anmeldung
        return "Der Store lädt gerade „%s“ von Epic." % ejobs[0]["title"] if ejobs else None
    wiring.busy_checks.append(jobs_busy)
    wiring.shutdown_hooks.append(downloads.stop)
    wiring.shutdown_hooks.append(gog_dl.stop)
    wiring.shutdown_hooks.append(epic_dl.stop)
    for queue in (downloads, gog_dl, epic_dl):  # Aufträge von vor dem Neustart (GOG und Epic: E99)
        if queue.queue:
            wiring.after_ready.append(queue.start)


def _appid(params: dict) -> int | None:
    raw = str((params or {}).get("id") or "")
    return int(raw) if raw.isdigit() and 0 < int(raw) < 2**31 else None
