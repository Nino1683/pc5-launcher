"""Verdrahtung, Teil „proton“: Proton-Versionen prüfen, reparieren, installieren und entfernen
(Bauplan E101, E102).

E101 (Wunsch 2026-10-23: „Wenn eine beschädigte Proton-Version vorliegt, soll sie neu heruntergeladen
und installiert werden“), E102 („… unter Einstellungen › System › Updates auch Proton-Versionen
herunterladen können, auch eine Auswahl an Proton-Versionen, die es so gibt und nützlich sind“).
Die eigentliche Arbeit macht proton_repair.py; hier hängt sie am Home:

  wiring.proton_jobs   eine Warteschlange für Proton-Aufträge (repair, install): einer zugleich, in
                       einem eigenen Arbeits-Thread mit niedrigem Vorrang (couch-proton), Abbrechen über
                       threading.Event. Ereignisse proton.progress (höchstens 4/s), proton.done und
                       proton.changed, dazu eine Mitteilung. Unter Downloads im Control Center stehen die
                       Aufträge in sys.state.store_jobs mit store "proton" (Kreuz: store.cancel).
  PROTON_DAMAGED       launch/proton.py meldet bei jedem echten Lauf einen beschädigten Build: Die Reparatur
                       kommt in die Warteschlange (origin "launch", nie doppelt), dazu eine Mitteilung mit
                       dem Satz „Dieses Mal startet das Spiel mit …“, auch wenn die Reparatur schon lief.
                       Der Haken kehrt sofort zurück: True, wenn eine Reparatur ansteht oder läuft.
  nach dem Bereit-Signal  im Arbeits-Thread: Reste abgebrochener Läufe (cleanup_staging), für heile
                       Builds ohne Manifest einmal eins (nur bei 0 jüngeren Dateien), alle prüfen,
                       beschädigte reparieren lassen, wenn kein Prozess sie nutzt, mit Mitteilung.
  nach einem Steam-Spiel  (procs.on_change, E57) dieselbe Prüfung nach STEAM_RECHECK_S: Steam startet seine
                       Spiele mit eigener Proton-Zuordnung, auch mit zwei Fassungen auf einem Prefix
                       (compatdata/0); so steht eine Reparatur vor dem nächsten Start an. Schon gemeldete
                       Schäden kommen dabei nicht noch einmal als Mitteilung.
  busy_checks          solange ein Auftrag läuft: ein Satz für Ausschalten, Abmelden und Ruhemodus.
  Einstellungen        settings/system.py zeichnet „Proton-Versionen“ aus overview() und ruft für ihre
                       Aktionen die Methoden hier (deps.extra["proton"]). Die Quelle eines Downloads
                       kommt immer aus der eigenen Liste (available() bzw. source_for), nie von der Seite.

Im Prüfmodus geht nichts ins Netz: get verweigert dann (Tests stecken über wiring.proton_get eine
Attrappe ein), und ohne Attrappe stößt der Start keine Reparatur an. Einsteckbar für Tests außerdem
wiring.proton_thread (startet den Arbeits-Thread), wiring.proton_busy (statt proton_repair.in_use)
und wiring.proton_arch (Bauart für Proton-CachyOS).
"""
from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
from typing import Any, Callable

from . import log, paths, proton_repair

_log = log.get("wiring.proton")

STORE = "proton"                 # Kennung unter sys.state.store_jobs und für store.cancel
PROGRESS_S = 0.25                # proton.progress höchstens viermal je Sekunde
VIEW_S = 1.0                     # Downloads und offene Einstellungsseite höchstens einmal je Sekunde
CATALOG_LIMIT = 3                # je Familie so viele neueste Fassungen
CATALOG_REREAD_S = 60.0          # so lange gilt die gelesene Liste, bevor die Seite sie neu liest
GENERIC = "Das hat nicht geklappt."
PAGE = "system.proton"
BUILD_PAGE = "system.proton.build:"
PAGES = (PAGE, "system.apps")    # zeigen Aufträge und Stand
PHASES = {"queued": "Wartet", "download": "Lädt", "verify": "Prüft", "extract": "Entpackt", "swap": "Setzt ein",
          "done": "Fertig"}
DOING = {"repair": "wird neu geladen", "install": "wird installiert"}
ALREADY = {"repair": "wird schon neu geladen", "install": "wird schon installiert"}
NOTE_REPEAT_S = 30.0             # dieselbe Mitteilung zum Ausweichen nicht zweimal kurz nacheinander (Übernahme)
STEAM_RECHECK_S = 20.0           # nach dem Ende eines Steam-Spiels: so lange enden Steams Reste, dann prüfen
STATES = {"queued": "queued", "download": "downloading", "verify": "verifying", "extract": "extracting",
          "swap": "installing", "done": "installing"}
WHERE = "Einstellungen › System › Systemsoftware › Updates › Proton-Versionen"


def _no_net(url: str, start: int = 0):
    """get im Prüfmodus ohne Attrappe: nie ins echte Netz (SAFETY Regel 11)."""
    raise urllib.error.URLError("Prüfmodus: kein Netz")


_IOPRIO_SET = {"x86_64": 251, "aarch64": 30}     # Nummer des Systemaufrufs ioprio_set
IOPRIO_WHO_PROCESS, IOPRIO_CLASS_IDLE, IOPRIO_CLASS_SHIFT = 1, 3, 13


def _lower_priority() -> None:
    """Nur der eigene Thread: Entpacken und Prüfen sollen ein laufendes Spiel nicht stören, weder beim
    Rechnen (nice 10) noch auf der Platte (E/A-Klasse „idle“: Manifeste lesen etwa 1 GB je Build, Entpacken
    schreibt 1,7 GB). Best effort: Was nicht geht, bleibt still beim Alten."""
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, 10)
    except (OSError, AttributeError):
        pass
    number = _IOPRIO_SET.get(os.uname().machine)
    if number is None:
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall(number, IOPRIO_WHO_PROCESS, tid, IOPRIO_CLASS_IDLE << IOPRIO_CLASS_SHIFT)
    except Exception:  # noqa: BLE001  (best effort)
        pass


def _thread(fn: Callable[[], None]) -> None:
    """Eigener Thread mit niedrigem Vorrang (Aufträge, Prüfung nach dem Start), nicht der gemeinsame
    Pool: Manifeste lesen fast 1 GB je Build, Entpacken dauert 15–30 s."""
    def run() -> None:
        _lower_priority()
        try:
            fn()
        except Exception:
            _log.exception("Proton-Thread")
    threading.Thread(target=run, name="couch-proton", daemon=True).start()


def _quote(label: str) -> str:
    return "„%s“" % label


class ProtonJobs:
    """Warteschlange und Zustand der Proton-Versionen (siehe Modulkopf). Alle öffentlichen Methoden
    dürfen aus jedem Thread kommen und kehren schnell zurück, außer overview(), remove(), check(),
    can_remove() und install_wait() (lesen die Platte bzw. warten; nur im Arbeits-Thread rufen)."""

    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        injected = getattr(wiring, "proton_get", None)
        self.net_ok = injected is not None or not paths.test_mode()
        self.get: Callable = injected or (_no_net if paths.test_mode() else proton_repair._open)
        self.in_use: Callable[[str], bool] = getattr(wiring, "proton_busy", None) or proton_repair.in_use
        self.start_thread: Callable = getattr(wiring, "proton_thread", None) or _thread
        self.arch = getattr(wiring, "proton_arch", None)
        self.clock: Callable[[], float] = time.monotonic
        self.lock = threading.RLock()
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.working = False
        self.closed = False
        self.removing: set[str] = set()
        self.serial = 0
        self.show_em = False             # Proton-EM nur auf Wunsch (ruht seit April 2026)
        self.catalog_state: dict = {"items": [], "error": "", "checked_at": 0.0, "loading": False,
                                    "fetched": False, "families": (), "at": -1e9}
        self.sizes: dict[str, tuple] = {}        # Name -> ((Inode, mtime_ns), Bytes)
        self.sizing = False
        self.names: list[str] = []               # zuletzt gesehene installierte Builds (Seiten neu zeichnen)
        self.damaged: set[str] = set()
        self.noted: dict[str, tuple] = {}         # Name -> (Ausweich-Version, Zeitpunkt) der letzten Mitteilung
        # Aufräumen der Zwischenordner (startup) nie, während ein Auftrag einen anlegt (_execute)
        self.staging_lock = threading.Lock()
        self.recheck_delay = STEAM_RECHECK_S
        self._last_progress = -1e9
        self._last_view = -1e9

    # ------------------------------------------------------------ Orte
    def compat_dir(self) -> str:
        from . import wiring_settings
        dirs = wiring_settings._compat_dirs(self.app, self.wiring)
        return os.path.realpath(dirs[0]) if dirs else ""

    # ------------------------------------------------------------ Warteschlange
    def _job_for(self, name: str) -> dict | None:
        for job in ([self.current] if self.current else []) + self.queue:
            if job["name"] == name:
                return job
        return None

    def _enqueue(self, name: str, action: str, origin: str, source: dict | None = None, quiet: bool = False,
                 progress: Callable[[int], None] | None = None) -> tuple[dict, dict | None, bool]:
        """(Ergebnis {ok, message, queued}, Auftrag, neu angelegt). Ein Auftrag für denselben Namen
        kommt nie doppelt in die Schlange (E101: eine laufende oder anstehende Reparatur genügt)."""
        label = proton_repair.label(name)
        with self.lock:
            if self.closed:
                return {"ok": False, "message": "Das Home endet gerade.", "queued": False}, None, False
            if name in self.removing:
                return {"ok": False, "message": "%s wird gerade entfernt." % _quote(label), "queued": False}, None, False
            same = self._job_for(name)
            if same is not None:
                if same["action"] != action:
                    return ({"ok": False, "message": "Für %s läuft schon ein Auftrag." % _quote(label), "queued": False},
                            None, False)
                if progress is not None:
                    same["listeners"].append(progress)
                return ({"ok": True, "message": "%s %s." % (_quote(label), ALREADY[action]),
                         "queued": same is not self.current}, same, False)
            self.serial += 1
            job = {"id": self.serial, "name": name, "label": label, "action": action, "origin": origin,
                   "phase": "queued", "percent": 0, "done": 0, "total": 0, "stop": threading.Event(),
                   "event": threading.Event(), "source": source, "quiet": quiet, "result": None,
                   "listeners": [progress] if progress is not None else []}
            queued = self.current is not None or bool(self.queue)
            self.queue.append(job)
            start = not self.working
            self.working = True
        _log.info("Proton %s: %s (%s)%s", action, name, origin, ", wartet" if queued else "")
        self._view(force=True)
        if start:
            try:
                self.start_thread(self._work)
            except Exception:
                _log.exception("Proton-Thread startet nicht")
                with self.lock:
                    self.working = False
                    if job in self.queue:
                        self.queue.remove(job)
                job["result"] = {"ok": False, "code": "os", "message": GENERIC}
                job["event"].set()          # niemand wartet ewig (install_wait)
                self._view(force=True)
                return {"ok": False, "message": GENERIC, "queued": False}, None, False
        if queued:
            text = "%s %s, sobald der laufende Proton-Auftrag fertig ist." % (_quote(label), DOING[action])
        else:
            text = "%s %s. Den Fortschritt siehst du unter Downloads." % (_quote(label), DOING[action])
        return {"ok": True, "message": text, "queued": queued}, job, True

    def _work(self) -> None:
        """Arbeits-Thread: ein Auftrag nach dem anderen. Was auch passiert (etwa main.post beim Beenden),
        der Auftrag endet mit Ergebnis und Ereignis, und am Ende steht working wieder auf False: Sonst
        warteten alle späteren Aufträge für immer, und busy() hielte Ruhemodus und Ausschalten auf."""
        clean = False
        try:
            while True:
                with self.lock:
                    if self.closed or not self.queue:
                        self.working = False
                        self.current = None
                        clean = True
                        return
                    job = self.queue.pop(0)
                    self.current = job
                result = None
                try:
                    self._view(force=True)
                    result = self._execute(job)
                except Exception:
                    _log.exception("Proton-Auftrag %s %s", job["action"], job["name"])
                finally:
                    with self.lock:
                        self.current = None
                    try:
                        self._finish(job, result)
                    except Exception:
                        _log.exception("Proton-Auftrag %s abschließen", job["name"])
                    finally:
                        job["event"].set()          # niemand wartet ewig (install_wait)
        finally:
            if not clean:
                with self.lock:
                    self.working = False
                    self.current = None

    def _execute(self, job: dict) -> dict:
        compat = self.compat_dir()
        if not compat:
            return {"ok": False, "code": "no-compat", "message": "Der Ordner für Proton-Versionen fehlt."}

        def progress(p: dict) -> None:
            self._progress(job, p)
        try:
            with self.staging_lock:
                if job["action"] == "repair":
                    return proton_repair.repair(job["name"], compat, proton_repair.cache_root(),
                                                proton_repair.backup_dir(), progress=progress, stop=job["stop"],
                                                get=self.get, busy=self.in_use, manifest_dir=proton_repair.manifest_dir())
                return proton_repair.install(job["name"], compat, proton_repair.cache_root(), progress=progress,
                                             stop=job["stop"], get=self.get, manifest_dir=proton_repair.manifest_dir(),
                                             source=job["source"])
        except Exception:                       # repair/install werfen nie; zur Sicherheit
            _log.exception("Proton %s %s", job["action"], job["name"])
            return {"ok": False, "code": "os", "message": GENERIC}

    def _progress(self, job: dict, p: dict) -> None:
        """Arbeits-Thread: Stand merken, gedrosselt weitergeben."""
        phase = str(p.get("phase") or job["phase"])
        new_phase = phase != job["phase"]
        job.update(phase=phase, percent=int(p.get("percent") or 0),
                   done=int(p.get("done") or 0) if phase == "download" else 0,
                   total=int(p.get("total") or 0) if phase == "download" else 0)
        for fn in list(job["listeners"]):
            try:
                fn(job["percent"])
            except Exception:  # noqa: BLE001  (ein Zuhörer darf den Auftrag nicht kippen)
                _log.exception("Proton-Fortschritt weitergeben")
        now = self.clock()
        if new_phase or phase == "done" or now - self._last_progress >= PROGRESS_S:
            self._last_progress = now
            self._emit("proton.progress", {"name": job["name"], "action": job["action"], "phase": phase,
                                           "percent": job["percent"], "done": job["done"], "total": job["total"]})
        if new_phase or now - self._last_view >= VIEW_S:
            self._view()

    def _finish(self, job: dict, result: dict) -> None:
        result = result if isinstance(result, dict) else {"ok": False, "code": "os", "message": GENERIC}
        job["result"] = result
        ok, code = bool(result.get("ok")), str(result.get("code") or ("ok" if result.get("ok") else "failed"))
        message = str(result.get("message") or ("" if ok else GENERIC))
        _log.info("Proton %s %s: %s (%s)", job["action"], job["name"], "fertig" if ok else "nicht fertig", code)
        self.sizes.pop(job["name"], None)
        if ok:
            self.damaged.discard(job["name"])
            self.noted.pop(job["name"], None)
            self._caches_stale()
            try:
                from .launch import proton
                proton.health_clear()               # nicht nötig (Schlüssel mit Inode), aber sofort sicher
            except Exception:
                _log.exception("Proton-Prüfungen vergessen")
            watch = getattr(self.wiring, "update_watch", None)
            if watch is not None and hasattr(watch, "proton_installed"):
                try:
                    watch.proton_installed(job["name"])
                except Exception:
                    _log.exception("Update-Stand nach Proton")
        self._view(force=True)
        self._emit("proton.done", {"name": job["name"], "action": job["action"], "origin": job["origin"], "ok": ok,
                                   "message": message, "code": code})
        self._changed([job["name"]])
        if not job["quiet"]:
            self._tell_done(job, ok, code, message)
        job["event"].set()

    def _tell_done(self, job: dict, ok: bool, code: str, message: str) -> None:
        label = job["label"]
        if ok and job["action"] == "repair":
            self._notify("%s ist repariert" % label, message, "update")
        elif ok:
            self._notify("%s ist installiert" % label,
                         "Wählen kannst du sie unter Spiele › Standard-Proton oder je Spiel.", "update")
        elif code == "stopped":
            self._notify("%s: abgebrochen" % label, message, "info")
        else:
            self._notify("%s: das hat nicht geklappt" % label, message, "warn")

    # ------------------------------------------------------------ Ereignisse und Anzeige
    def _emit(self, event: str, payload: dict) -> None:
        rpc = getattr(self.app, "rpc", None)
        if rpc is not None:
            self.wiring.main.post(lambda: rpc.emit(event, payload))

    def _notify(self, title: str, body: str, icon: str) -> None:
        try:
            self.app.notify.push(title, body, icon=icon)
        except Exception:
            _log.exception("Mitteilung „%s“", title)

    def _pages(self, names: list[str] | None = None, every: bool = True) -> list[str]:
        """Seiten, die neu zeichnen sollen: die Liste, Updates und die Unterseiten der genannten
        (every: aller installierten) Versionen. Die Seite lädt nur bei ihrer eigenen Kennung neu."""
        out = list(PAGES)
        for n in list(names or []) + (list(self.names) if every else []):
            if BUILD_PAGE + n not in out:
                out.append(BUILD_PAGE + n)
        return out

    def _changed(self, names: list[str] | None = None) -> None:
        """Nach jeder Änderung: proton.changed und die offenen Einstellungsseiten neu zeichnen lassen.
        Direkt als settings.changed (nicht über den Baum: der zöge bei jeder Seite „system.*“ die
        Update-Marke nach, und das liest ostree)."""
        self._emit("proton.changed", {})
        for page in self._pages(names):
            self._emit("settings.changed", {"page": page})

    def job_views(self) -> list[dict]:
        """Aufträge für sys.state.store_jobs (Downloads im Control Center)."""
        with self.lock:
            jobs = ([self.current] if self.current else []) + list(self.queue)
        out = []
        for job in jobs:
            view = {"store": STORE, "id": job["name"], "title": job["label"], "kind": job["action"],
                    "state": STATES.get(job["phase"], "queued"), "text": PHASES.get(job["phase"], "Wartet"),
                    "percent": job["percent"]}
            if job["total"]:
                view.update(done=job["done"], total=job["total"])
            out.append(view)
        return out

    def _view(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_view < VIEW_S:
            return
        self._last_view = now
        self.wiring.main.post(self._publish_jobs)
        with self.lock:
            names = [j["name"] for j in ([self.current] if self.current else []) + self.queue]
        for page in self._pages(names, every=False):
            self._emit("settings.changed", {"page": page})

    def _publish_jobs(self) -> None:
        """Hauptschleife: über die Sammelstelle des Stores (wiring_store.publish_jobs), sonst selbst."""
        views = self.job_views()
        collect = getattr(self.wiring, "store_publish_jobs", None)
        if callable(collect):
            collect({STORE}, views)
            return
        state = self.app.state
        others = [j for j in (state.value("store_jobs") or []) if j.get("store") != STORE] \
            if hasattr(state, "value") else []
        state.update(store_jobs=others + views)

    def _caches_stale(self) -> None:
        """Auswahl je Spiel und „Automatisch“ neu lesen lassen (wiring_settings)."""
        p = getattr(self.wiring, "settings_part", None)
        if p is not None:
            p.proton_cache = None
            p.proton_builds_at = -1e9

    # ------------------------------------------------------------ Schnittstelle (Vertrag mit L, W)
    def repair(self, name: str, origin: str = "settings") -> dict:
        """Reparatur anstoßen. {ok, message, queued}; kehrt sofort zurück."""
        name = str(name or "")
        label = proton_repair.label(name)
        compat = self.compat_dir()
        if not proton_repair.valid_name(name) or not compat:
            return {"ok": False, "message": "Diese Proton-Version gibt es nicht.", "queued": False}
        if not os.path.isdir(os.path.join(compat, name)):
            return {"ok": False, "message": "%s ist nicht installiert." % _quote(label), "queued": False}
        if proton_repair.source_for(name) is None:
            return {"ok": False, "message": "Für %s ist keine Quelle bekannt; sie lässt sich nicht neu laden." % _quote(label),
                    "queued": False}
        return self._enqueue(name, "repair", origin)[0]

    def install(self, name: str, origin: str = "settings") -> dict:
        """Eine Version aus der eigenen Liste (available) installieren. {ok, message, queued}."""
        name = str(name or "")
        label = proton_repair.label(name)
        compat = self.compat_dir()
        if not proton_repair.valid_name(name) or not compat:
            return {"ok": False, "message": "Diese Proton-Version gibt es nicht.", "queued": False}
        if os.path.lexists(os.path.join(compat, name)):
            return {"ok": False, "message": "%s ist schon installiert." % _quote(label), "queued": False}
        with self.lock:
            item = next((dict(i) for i in self.catalog_state["items"] if i.get("name") == name), None)
        if item is None:
            return {"ok": False, "message": "%s steht nicht in der Liste der verfügbaren Versionen. Such neu." % _quote(label),
                    "queued": False}
        # Herkunft nur so, wie sie zum Katalog passt: Adressen neu gebaut aus Repo, Tag und Datei (E102),
        # nie die aus der API-Antwort bzw. dem Zwischenspeicher
        source = proton_repair.trusted_source(item)
        if source is None:
            _log.warning("Proton-Liste: %s passt nicht zum Katalog und wird nicht geladen", name)
            return {"ok": False, "message": "%s passt nicht zum Katalog und wird nicht geladen. Such neu." % _quote(label),
                    "queued": False}
        return self._enqueue(name, "install", origin, source=source)[0]

    def install_wait(self, name: str, progress: Callable[[int], None], size: int = 0) -> str:
        """Für die Updates-Zeile „Proton-CachyOS aktualisieren“ (UpdateWatch, Arbeits-Thread): dieselbe
        Warteschlange, wartet bis zum Ende. Meldet sich nicht selbst (die UpdateWatch tut es).
        Liefert den Ordner; wirft ValueError mit deutschem Satz."""
        source = proton_repair.source_for(str(name or ""))
        if source is None:
            raise ValueError("Für %s ist keine Quelle bekannt." % name)
        source["size"] = int(size or 0)
        result, job, _new = self._enqueue(name, "install", "updates", source=source, quiet=True, progress=progress)
        if job is None:
            raise ValueError(result["message"])
        job["event"].wait()
        done = job["result"] or {}
        if done.get("ok") or done.get("code") == "exists":
            return os.path.join(self.compat_dir(), name)
        raise ValueError(str(done.get("message") or GENERIC))

    def cancel(self, name: str | None = None) -> dict:
        """Den laufenden Auftrag (oder den für name) abbrechen. {ok, message}."""
        with self.lock:
            job = self._job_for(name) if name else (self.current or (self.queue[0] if self.queue else None))
            if job is None:
                return {"ok": False, "message": "Es läuft kein Proton-Auftrag."}
            if job is self.current:
                job["stop"].set()
                return {"ok": True, "message": "%s wird abgebrochen …" % _quote(job["label"])}
            self.queue.remove(job)
        self._finish(job, {"ok": False, "code": "stopped", "message": "Abgebrochen, bevor es losging."})
        return {"ok": True, "message": "%s wird nicht geladen." % _quote(job["label"])}

    def running(self) -> dict | None:
        with self.lock:
            job = self.current
            return None if job is None else {"name": job["name"], "action": job["action"], "phase": job["phase"],
                                             "percent": job["percent"]}

    def pending(self) -> list[dict]:
        with self.lock:
            return [{"name": j["name"], "label": j["label"], "action": j["action"]} for j in self.queue]

    def busy(self) -> str | None:
        """Für busy_checks und den Ruhemodus (Muster UpdateWatch.busy_text). Aufträge, die ihr
        Auftraggeber selbst meldet (quiet, die Updates-Zeile), zählen hier nicht doppelt."""
        with self.lock:
            jobs = [j for j in ([self.current] if self.current else []) + self.queue if not j["quiet"]]
        if not jobs:
            return None
        job = jobs[0]
        if job is self.current:
            return "%s %s (%d %%)." % (job["label"], DOING[job["action"]], job["percent"])
        return "%s wartet aufs Laden." % job["label"]

    def on_damaged(self, name: str, tool_dir: str, result: dict) -> bool:
        """launch.proton.PROTON_DAMAGED (E101): bei einem echten Lauf beschädigt gefunden. Reparatur
        anstoßen (nie doppelt) und sofort zurückkehren; wirft nie. result["note"] („Dieses Mal startet das
        Spiel mit …“) kommt in die Mitteilung, auch wenn die Reparatur schon lief (sonst startete das Spiel
        still mit einer anderen Version). Rückgabe True, wenn eine Reparatur ansteht oder läuft: Nur dann
        sagt der Start „wird neu geladen“ (Vertrag mit launch/proton.py)."""
        try:
            name = str(name or "")
            compat = self.compat_dir()
            real = os.path.realpath(str(tool_dir or ""))
            if not proton_repair.valid_name(name) or not compat or os.path.basename(real) != name \
                    or os.path.dirname(real) != compat:
                _log.info("Proton beschädigt, aber nicht in compatibilitytools.d: %s", name)
                return False
            self.damaged.add(name)
            label = proton_repair.label(name)
            result = result if isinstance(result, dict) else {}
            reason = str(result.get("reason") or "")
            alt = str(result.get("alternative") or "")
            note = str(result.get("note") or "")
            if not self.net_ok:
                _log.info("Prüfmodus ohne Netz-Attrappe: %s wird nicht neu geladen", name)
                return False
            if proton_repair.source_for(name) is None:
                self._notify_damaged(name, alt, "%s ist beschädigt" % label, " ".join(x for x in (
                    reason, "Für diese Version ist keine Quelle bekannt; entfernen kannst du sie unter %s." % WHERE,
                    note) if x), "warn")
                return False
            _res, job, new = self._enqueue(name, "repair", "launch")
            if job is None:                       # Home endet, wird entfernt, anderer Auftrag für den Namen
                return False
            if new:
                self._notify_damaged(name, alt, "%s ist beschädigt und wird neu geladen" % label,
                                     " ".join(x for x in (note, "Den Fortschritt siehst du unter Downloads.") if x),
                                     "update", force=True)
            elif alt:
                self._notify_damaged(name, alt, "Dieses Mal mit %s" % proton_repair.label(alt),
                                     " ".join(x for x in ("%s wird gerade neu geladen." % label, note) if x), "update")
            return True
        except Exception:
            _log.exception("Proton beschädigt melden")
            return False

    def _notify_damaged(self, name: str, alt: str, title: str, body: str, icon: str, force: bool = False) -> None:
        """Mitteilung zu einem beschädigten Build, aber nicht zweimal binnen NOTE_REPEAT_S mit derselben
        Ausweich-Version (eine Prefix-Übernahme ruft den Haken zweimal für einen Start)."""
        now = self.clock()
        with self.lock:
            last = self.noted.get(name)
            if not force and last is not None and last[0] == alt and now - last[1] < NOTE_REPEAT_S:
                return
            self.noted[name] = (alt, now)
        self._notify(title, body, icon)

    # ------------------------------------------------------------ nach dem Start
    def after_app(self, running: Any, what: str) -> None:
        """procs.on_change (Hauptschleife): Endet ein Steam-Spiel, nach STEAM_RECHECK_S alle Builds prüfen."""
        if what != "exited" or self.closed or not str(getattr(running, "entry_id", "") or "").startswith("steam:"):
            return

        def later() -> None:
            time.sleep(self.recheck_delay)
            if not self.closed:
                self.startup(recheck=True)
        try:
            self.start_thread(later)
        except Exception:
            _log.exception("Proton nach Steam-Spiel prüfen")

    def startup(self, recheck: bool = False) -> None:
        """Arbeits-Thread nach dem Bereit-Signal (siehe Modulkopf). recheck: nach einem Steam-Spiel; dann
        kommen schon gemeldete Schäden nicht noch einmal als Mitteilung."""
        compat = self.compat_dir()
        if not compat or not os.path.isdir(compat):
            return
        # Nur, solange kein Auftrag einen Zwischenordner anlegt (staging_lock): sonst räumte das hier im Spalt
        # zwischen mkdir und flock einen frischen weg. Läuft einer, räumt er vorher selbst auf (_run)
        if self.staging_lock.acquire(blocking=False):
            try:
                gone = proton_repair.cleanup_staging(compat)
                if gone:
                    _log.info("Proton: Zwischenordner abgebrochener Läufe entfernt: %s", ", ".join(gone))
            except Exception:
                _log.exception("Proton-Zwischenordner aufräumen")
            finally:
                self.staging_lock.release()
        mdir = proton_repair.manifest_dir()
        try:
            entries = proton_repair.installed(compat, mdir)
        except Exception:
            _log.exception("Proton-Versionen prüfen")
            return
        self.names = [e["name"] for e in entries]
        known = set(self.damaged) if recheck else set()
        made = 0
        for e in entries:
            path = os.path.join(compat, e["name"])
            if not e["ok"]:
                # wie der Starter (proton.is_damaged): nur ein echter Befund ist ein Schaden, ein fremder Aufbau
                # ohne files/lib/wine nicht
                if e.get("method") in ("manifest", "zeitstempel") and e.get("files", 0) > 0:
                    self._damaged_at_start(e, path, told=e["name"] in known)
                else:
                    _log.info("Proton %s nicht prüfbar: %s", e["name"], e.get("reason"))
                continue
            if e["manifest"] or e["method"] != "zeitstempel":
                continue
            try:
                # einmalig, nur ganz ohne jüngere Dateien (ein Manifest schriebe Schaden fest) und
                # nur, solange niemand den Ordner benutzt
                res = proton_repair.check(path, mdir)
                if res["ok"] and res["files"] == 0 and not self.in_use(path):
                    proton_repair.make_manifest(path, mdir)
                    made += 1
                    _log.info("Proton-Manifest angelegt: %s", e["name"])
            except Exception:
                _log.exception("Proton-Manifest für %s", e["name"])
        if made or self.damaged:
            self._changed()

    def _damaged_at_start(self, e: dict, path: str, told: bool = False) -> None:
        """told: schon gemeldet (Prüfung nach einem Steam-Spiel); dann nur noch eine neue Reparatur melden."""
        name, label = e["name"], e["label"]
        self.damaged.add(name)
        _log.warning("Proton beschädigt: %s (%s)", name, e.get("reason"))
        if not self.net_ok:
            return                              # Prüfmodus ohne Netz-Attrappe: nur im Protokoll
        reason = str(e.get("reason") or "")
        if proton_repair.source_for(name) is None:
            if not told:
                self._notify("%s ist beschädigt" % label,
                             (reason + " Für diese Version ist keine Quelle bekannt; entfernen kannst du sie unter %s."
                              % WHERE).strip(), "warn")
            return
        try:
            used = bool(self.in_use(path))
        except Exception:  # noqa: BLE001  (Zweifel = belegt)
            used = True
        if used:
            if not told:
                self._notify("%s ist beschädigt" % label,
                             "Die Version wird gerade benutzt. Reparieren kannst du sie danach unter %s." % WHERE, "warn")
            return
        _res, _job, new = self._enqueue(name, "repair", "start")
        if new:
            self._notify("%s ist beschädigt und wird neu geladen" % label,
                         "Den Fortschritt siehst du unter Downloads.", "update")

    # ------------------------------------------------------------ für die Seite „Proton-Versionen“
    def summary(self) -> dict:
        """Schnell (nur listdir): {installed, damaged, job} für die Zeile unter Updates."""
        compat = self.compat_dir()
        try:
            names = [n for n in os.listdir(compat) if not n.startswith(".") and not n.startswith("pc5-")
                     and os.path.isfile(os.path.join(compat, n, "proton"))] if compat else []
        except OSError:
            names = []
        return {"installed": len(names), "damaged": len(self.damaged & set(names)), "job": self.running()}

    def overview(self) -> dict:
        """Alles für die Seite (Arbeits-Thread; liest die Ordner, ~10 ms je Build). Größen und Liste
        der verfügbaren Versionen kommen aus dem Zwischenspeicher und werden im Hintergrund nachgeladen;
        danach kommt settings.changed."""
        compat = self.compat_dir()
        mdir = proton_repair.manifest_dir()
        entries = proton_repair.installed(compat, mdir) if compat else []
        # je Familie in der Reihenfolge des Katalogs, darin die neueste zuerst
        order = {c["family"]: i for i, c in enumerate(proton_repair.CATALOG)}
        entries.sort(key=lambda e: (order.get(e["family"], len(order)),
                                    tuple(-n for n in proton_repair.version_key(e["name"]))))
        self.names = [e["name"] for e in entries]
        self.damaged = {e["name"] for e in entries if not e["ok"]}
        sizes = self._sizes(compat, entries)
        out = []
        for e in entries:
            out.append(dict(e, size_bytes=sizes.get(e["name"]), source=proton_repair.source_for(e["name"]) is not None))
        with self.lock:
            job = self.current
            jobs = {j["name"]: {"name": j["name"], "label": j["label"], "action": j["action"], "phase": j["phase"],
                                "percent": j["percent"], "running": j is job}
                    for j in ([job] if job else []) + self.queue}
        return {"compat": compat, "installed": out, "jobs": jobs,
                "job": next((v for v in jobs.values() if v["running"]), None),
                "queue": [v for v in jobs.values() if not v["running"]],
                "catalog": self.catalog(compat), "show_em": self.show_em, "net": self.net_ok}

    def _families(self) -> tuple:
        return tuple(c["family"] for c in proton_repair.CATALOG
                     if c["listed"] or (self.show_em and c["family"] == "proton-em"))

    def catalog(self, compat: str | None = None) -> dict:
        """Verfügbare Versionen aus dem Zwischenspeicher; liest im Hintergrund nach, wenn nötig."""
        families = self._families()
        with self.lock:
            st = self.catalog_state
            need = not st["loading"] and (not st["fetched"] or st["families"] != families
                                          or self.clock() - st["at"] > CATALOG_REREAD_S)
            if need:
                st["loading"] = True
            snap = dict(st, items=[dict(i) for i in st["items"]])
        if need:
            self._fetch_later(families, proton_repair.TTL_S)
            with self.lock:
                snap = dict(self.catalog_state, items=[dict(i) for i in self.catalog_state["items"]])
        compat = self.compat_dir() if compat is None else compat
        for it in snap["items"]:
            it["installed"] = bool(compat) and os.path.isdir(os.path.join(compat, str(it.get("name") or "")))
        return snap

    def _fetch_later(self, families: tuple, ttl: float) -> None:
        try:
            self.wiring._bg(self._fetch, families, ttl)
        except Exception:
            _log.exception("Proton-Liste nachladen")
            with self.lock:
                self.catalog_state["loading"] = False

    def _fetch(self, families: tuple, ttl: float) -> None:
        """Arbeits-Thread: available(status=True) über die GitHub-API (6 h zwischengespeichert)."""
        try:
            got = proton_repair.available(family=list(families), limit=CATALOG_LIMIT, get=self.get,
                                          cache_dir=proton_repair.cache_root(), ttl=ttl,
                                          compat_dir=self.compat_dir() or None, status=True, arch=self.arch)
        except Exception as e:  # noqa: BLE001
            _log.warning("Proton-Liste: %s", e)
            got = {"items": [], "error": getattr(e, "message", "") or "Die Liste ließ sich nicht lesen.", "checked_at": 0.0}
        with self.lock:
            self.catalog_state.update(items=list(got.get("items") or []), error=str(got.get("error") or ""),
                                      checked_at=float(got.get("checked_at") or 0.0), loading=False, fetched=True,
                                      families=families, at=self.clock())
        self._changed()

    def refresh(self) -> dict:
        """„Neu suchen“: an GitHub vorbei am Zwischenspeicher (ttl 0)."""
        with self.lock:
            if self.catalog_state["loading"]:
                return {"ok": True, "message": "Die Suche läuft schon."}
            self.catalog_state["loading"] = True
        self._fetch_later(self._families(), 0.0)
        return {"ok": True, "message": "Es wird nach neuen Versionen gesucht …"}

    def set_show_em(self, on: bool) -> None:
        self.show_em = bool(on)
        self._changed()

    def _sizes(self, compat: str, entries: list[dict]) -> dict:
        """Gemessene Belegung je Build; Fehlendes misst ein Arbeits-Thread (danach settings.changed).
        Gilt je (Inode, mtime) des Ordners; was sich nicht messen ließ, bleibt None und wird nicht bei
        jedem Neuzeichnen wieder versucht (sonst zeichnete die Seite endlos neu)."""
        out, missing = {}, []
        for e in entries:
            try:
                st = os.lstat(os.path.join(compat, e["name"]))
            except OSError:
                continue
            key = (st.st_ino, st.st_mtime_ns)
            hit = self.sizes.get(e["name"])
            if hit is not None and hit[0] == key:
                if hit[1] is not None:
                    out[e["name"]] = hit[1]
            else:
                missing.append((e["name"], key))
        with self.lock:
            start = bool(missing) and not self.sizing
            if start:
                self.sizing = True
        if start:
            try:
                self.wiring._bg(self._measure, compat, missing)
            except Exception:
                _log.exception("Proton-Größen")
                with self.lock:
                    self.sizing = False
        return out

    def _measure(self, compat: str, missing: list[tuple]) -> None:
        """Arbeits-Thread: Belegung der fehlenden Builds (installed(quick=False), ~0,1–1 s je Build)."""
        got: dict[str, int] = {}
        try:
            for e in proton_repair.installed(compat, proton_repair.manifest_dir(), quick=False):
                if e.get("size_bytes") is not None:
                    got[e["name"]] = int(e["size_bytes"])
        except Exception:
            _log.exception("Proton-Größen")
        finally:
            for name, key in missing:
                self.sizes[name] = (key, got.get(name))
            with self.lock:
                self.sizing = False
        if got:
            self._changed()

    def check(self, name: str) -> dict:
        compat = self.compat_dir()
        if not proton_repair.valid_name(name) or not compat:
            return {"ok": False, "reason": "Diese Proton-Version gibt es nicht."}
        return proton_repair.check(os.path.join(compat, name), proton_repair.manifest_dir())

    def _games_using(self, name: str, path: str) -> list[str] | None:
        """Titel der Spiele, bei denen genau diese Version fest eingestellt ist (Option „proton“).
        None = die Bibliothek ließ sich nicht lesen (Zweifel heißt belegt)."""
        titles = []
        try:
            entries = list(self.app.library.entries() or [])
        except Exception:
            _log.exception("Bibliothek lesen")
            return None
        real = os.path.realpath(path)
        for entry in entries:
            try:
                value = str(entry.option("proton") or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if value and (value == name or (os.path.isabs(value) and os.path.realpath(value) == real)):
                titles.append(str(getattr(entry, "title", "") or entry.id))
        return titles

    def _steam_using(self, name: str, path: str) -> str:
        """Satz, wenn Steam diese Version selbst einem Spiel zuordnet (config.vdf, CompatToolMapping; nur
        lesend, E57: Steam-Spiele startet Steam), sonst "". Unlesbar zählt wie belegt."""
        from .library import steam
        label = _quote(proton_repair.label(name))
        names = {name} | set(_tool_names(path))
        try:
            mapping = steam.compat_tool_mapping(os.path.dirname(self.compat_dir()))
        except Exception as e:  # noqa: BLE001  (Zweifel = belegt; die Meldung nennt nur die Art)
            _log.warning("Steams Proton-Zuordnung nicht lesbar (%s)", type(e).__name__)
            return "Ob Steam %s nutzt, lässt sich gerade nicht prüfen (config.vdf unlesbar)." % label
        apps = sorted(a for a, tool in mapping.items() if tool in names)
        if 0 in apps:
            return ("Steam nutzt %s als Standard für alle Spiele. Stell das in Steam zuerst um (Einstellungen › "
                    "Kompatibilität)." % label)
        if apps:
            return "Steam nutzt %s für %s. Stell sie in Steam zuerst um." % (
                label, "1 Spiel" if len(apps) == 1 else "%d Spiele" % len(apps))
        return ""

    def can_remove(self, name: str) -> str:
        """"" oder der Satz, warum diese Version gerade nicht entfernt werden darf (Arbeits-Thread)."""
        label = _quote(proton_repair.label(name))
        compat = self.compat_dir()
        if not proton_repair.valid_name(name) or not compat:
            return "Diese Proton-Version gibt es nicht."
        with self.lock:
            if self._job_for(name) is not None or name in self.removing:
                return "Für %s läuft gerade ein Auftrag. Warte, bis er fertig ist." % label
        entries = proton_repair.installed(compat, proton_repair.manifest_dir())
        me = next((e for e in entries if e["name"] == name), None)
        if me is None:
            return "%s ist nicht installiert." % label
        path = os.path.join(compat, name)
        wanted = str(self.wiring.config.get("games.proton", "") or "").strip()
        if wanted and (wanted == name or (os.path.isabs(wanted) and os.path.realpath(wanted) == os.path.realpath(path))):
            return ("%s ist dein Standard-Proton. Stell zuerst einen anderen Standard ein (oder Automatisch), dann "
                    "lässt sie sich entfernen." % label)
        fixed = self._games_using(name, path)
        if fixed is None:
            return "Welche Spiele %s nutzen, lässt sich gerade nicht prüfen. Versuch es gleich noch mal." % label
        steam = self._steam_using(name, path)
        if steam:
            return steam
        if fixed:
            shown = ", ".join(_quote(t) for t in fixed[:3]) + (" und %d weitere" % (len(fixed) - 3) if len(fixed) > 3 else "")
            return "%s ist bei %s fest eingestellt. Stell das Spiel zuerst auf eine andere Version um." % (label, shown)
        if me["ok"] and not any(e["ok"] for e in entries if e["name"] != name):
            return "%s ist die einzige heile Proton-Version. Ohne sie starten keine Windows-Spiele." % label
        try:
            used = bool(self.in_use(path))
        except Exception:  # noqa: BLE001  (Zweifel = belegt)
            used = True
        if used:
            return "%s wird gerade benutzt (ein Spiel oder Wine läuft noch). Beende es zuerst." % label
        return ""

    def remove(self, name: str) -> dict:
        """Entfernen = in den Papierkorb des Systems (E102), nur über trash.to_xdg_trash: realpath
        direkt in compatibilitytools.d, kein Symlink, keine Einhängung, nicht in Benutzung, nie der
        letzte heile Build, nie der Standard, nie einer, den ein Spiel der Konsole oder Steam selbst
        (CompatToolMapping) fest nutzt; im Zweifel nicht. Arbeits-Thread. {ok, message}."""
        from . import xdgtrash
        from .installer import trash
        name = str(name or "")
        why = self.can_remove(name)
        if why:
            return {"ok": False, "message": why}
        label = _quote(proton_repair.label(name))
        with self.lock:
            if self._job_for(name) is not None or name in self.removing:
                return {"ok": False, "message": "Für %s läuft gerade ein Auftrag. Warte, bis er fertig ist." % label}
            self.removing.add(name)
        try:
            result = trash.to_xdg_trash(os.path.join(self.compat_dir(), name), xdgtrash.home_trash())
        finally:
            with self.lock:
                self.removing.discard(name)
        if not result.get("ok"):
            _log.warning("Proton entfernen %s: %s", name, result.get("code"))
            return {"ok": False, "message": str(result.get("message") or GENERIC)}
        _log.info("Proton in den Papierkorb: %s -> %s", name, result.get("path"))
        self.sizes.pop(name, None)
        self.damaged.discard(name)
        self._caches_stale()
        self._changed([name])
        return {"ok": True, "message": "%s liegt jetzt im Papierkorb. Zurücklegen kannst du sie im Datei-Explorer "
                                       "unter Papierkorb." % label}

    def set_default(self, name: str) -> dict:
        """Standard-Proton setzen: der eine Schlüssel games.proton (E22); "" = Automatisch."""
        from .settings import games
        name = "" if name is None else str(name)
        if name:
            label = _quote(proton_repair.label(name))
            dirs = [self.compat_dir()] if self.compat_dir() else []
            if not proton_repair.valid_name(name) or not any(b["id"] == name for b in games.list_proton_builds(dirs)):
                return {"ok": False, "message": "Diese Proton-Version gibt es nicht mehr."}
            if not self.check(name).get("ok"):
                return {"ok": False, "message": "%s ist beschädigt. Repariere sie zuerst." % label}
        self.wiring.config.set("games.proton", name)
        self._caches_stale()
        self._changed([name] if name else None)
        if name:
            return {"ok": True, "message": "%s ist jetzt dein Standard-Proton." % _quote(proton_repair.label(name))}
        return {"ok": True, "message": "Standard-Proton: Automatisch."}

    # ------------------------------------------------------------ Ende
    def stop(self) -> None:
        """shutdown_hooks: den laufenden Auftrag abbrechen (proton_repair räumt seinen Zwischenordner
        selbst ab; was ein hartes Ende liegen lässt, nimmt cleanup_staging beim nächsten Start)."""
        with self.lock:
            self.closed = True
            current, waiting = self.current, list(self.queue)
            self.queue.clear()
        if current is not None:
            current["stop"].set()
        for job in waiting:
            job["result"] = {"ok": False, "code": "stopped", "message": "Das Home endete vorher."}
            job["event"].set()
        try:
            from .launch import proton
            if getattr(getattr(proton, "PROTON_DAMAGED", None), "__self__", None) is self:
                proton.PROTON_DAMAGED = None
        except Exception:
            _log.exception("Proton-Haken lösen")


def _tool_names(path: str) -> list[str]:
    """Interne Namen aus compatibilitytool.vdf (so nennt Steam das Werkzeug in CompatToolMapping; meist
    gleich dem Ordnernamen)."""
    try:
        with open(os.path.join(path, "compatibilitytool.vdf"), encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return []
    m = re.search(r'"compat_tools"\s*\{\s*"([^"]{1,128})"', text)
    return [m.group(1)] if m else []


def extend(wiring: Any) -> None:
    jobs = ProtonJobs(wiring)
    wiring.proton_jobs = jobs
    # Haken des Starters (E101, Vertrag mit launch/proton.py): nur beim echten Start gerufen
    from .launch import proton
    proton.PROTON_DAMAGED = jobs.on_damaged
    # Kreuz unter Downloads: store.cancel {store: "proton", id: <Ordnername>} (Routen des Store-Teils)
    routes = getattr(wiring, "store_routes", None)
    if isinstance(routes, dict) and isinstance(routes.get("store.cancel"), dict):
        routes["store.cancel"][STORE] = lambda params: jobs.cancel(str((params or {}).get("id") or "") or None)
    procs_on = getattr(getattr(wiring.app, "procs", None), "on_change", None)
    if callable(procs_on):
        procs_on(jobs.after_app)                # nach jedem Steam-Spiel prüfen (E57, compatdata/0)
    wiring.busy_checks.append(jobs.busy)
    wiring.shutdown_hooks.append(jobs.stop)
    wiring.after_ready.append(lambda: jobs.start_thread(jobs.startup))
