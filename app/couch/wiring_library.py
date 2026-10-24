"""Verdrahtung der Bibliothek (Bauplan E21, E25, E28, E34, E42): Quellen am Index, Bilder,
der eine Hintergrund-Thread für alles, was Bilder kostet, und die RPC lib.setArt, lib.resetArt,
lib.fetchArt, lib.artCandidates, lib.artPreview, lib.artBrowse.

Aufbau:
  setup(wiring)     ruft Wiring._library: SourceContext anlegen, Bilder-Auflöser holen
                    (wiring._make_art), Quellen am Index anmelden.
  extend(wiring)    ruft Wiring._extend am Ende: Vorgaben, Beobachter, RPC, Bilder-Thread.
  Funktionen mit dem App-Objekt als erstem Parameter für die anderen Teile: add_local_record,
  get_local_record, remove_local, local_entries, game_dirs, steam_account, emudeck_info, rescan,
  fetch_all_art.

Bilder: Die Quellen laufen im Thread des Einlesens und bekommen dort ihre Bilder
(ArtResolver.resolve, mit den Grid-Bildern der passenden Steam-Verknüpfung). Was dabei erst
erzeugt werden müsste (kalte Ableitung, 0,1 bis 0,3 s je Eintrag), wartet nicht: Nach einem
kleinen Zeitbudget gehen die übrigen Einträge mit ihrem letzten bekannten Bild hinaus und der
Bilder-Thread trägt nach, zuletzt Gespieltes zuerst. Die Seite erfährt es als
lib.changed {ids, reason: "art"}: lokal Aufgelöstes in Schüben von etwa einer Sekunde, nach
Abrufen aus dem Netz gesammelt höchstens alle 2 s. Derselbe Thread holt Bilder aus dem Netz:
automatisch nach dem Bereit-Signal und sofort für jeden neuen Eintrag, gedrosselt, nie
solange eine App vorn ist, nur mit library.fetch_art. Im Prüfmodus nie, außer es ist eine
Attrappe eingesteckt: wiring_art.net_allowed() (COUCH_ART_FAKE_STORE=http://127.0.0.1:<port>
oder für einen bewussten Lauf COUCH_ART_NET=1) bzw. in Tests mit einem Attrappen-Auflöser
COUCH_TEST_ART_NET=1.
"""
from __future__ import annotations

import os
import threading
import time
import weakref
from concurrent.futures import Future
from typing import Any, Callable

from . import log, paths, system
from .rpc import RpcError

_log = log.get("wiring.library")

CFG_FETCH = "library.fetch_art"
CFG_SGDB = "library.sgdb_key"
DEFAULTS = {CFG_FETCH: True, CFG_SGDB: ""}

ART_BUDGET_S = 0.25         # so lange darf ein Lauf der Quellen kalte Bilder selbst erzeugen
ART_WARM_S = 0.02           # schneller = lag schon fertig vor und zählt nicht aufs Budget
FETCH_KINDS = ("cover", "hero", "logo")

# Reihenfolge im Bilder-Thread (kleiner = früher)
PRIO_MANUAL, PRIO_RESOLVE, PRIO_NEW, PRIO_BULK, PRIO_AUTO = 0, 10, 20, 30, 40

TEXT_NO_ART = "Die Bilder der Bibliothek sind gerade nicht verfügbar."
TEXT_NO_ENTRY = "Diesen Eintrag gibt es nicht mehr."
TEXT_FETCH_OFF = "Das Laden von Bildern aus dem Netz ist ausgeschaltet (Einstellungen › Spiele)."
TEXT_TEST_MODE = "Im Prüfmodus werden keine Bilder aus dem Netz geladen."
TEXT_OFFLINE = "Keine Verbindung zum Internet."
_STATUS_TEXT = {
    "ok": "Neue Bilder geladen.",
    "partial": "Neue Bilder geladen.",
    "miss": "Zu diesem Spiel wurde kein passendes Bild gefunden.",
    "offline": TEXT_OFFLINE,
    "disabled": TEXT_FETCH_OFF,
    "backoff": "Für dieses Spiel wurde vor Kurzem schon gesucht. Versuche es später noch einmal.",
    "skip": "Für diesen Eintrag gibt es keine Bilder im Netz.",
    "error": "Die Bilder konnten nicht geladen werden.",
    "cancelled": "Abgebrochen.",
    # E99: Alle zuständigen Bilderdienste ruhen nach Störungen (nichts gefragt, nichts vermerkt)
    "resting": "Die Bilderdienste antworten gerade nicht. Versuche es später noch einmal.",
    # E99: von Hand gewählter Treffer ohne ein einziges ladbares Bild (wird nicht gemerkt)
    "pick-failed": "Von diesem Treffer ließ sich kein Bild laden. Die bisherige Wahl bleibt.",
}
# Ergebnisse ohne Abfrage im Netz: danach keine Pause vor dem nächsten Auftrag
_NO_NET_STATUS = ("skip", "disabled", "backoff", "offline", "resting")

_PARTS: "weakref.WeakValueDictionary[int, LibraryPart]" = weakref.WeakValueDictionary()


def _read3(argv: Any, timeout: float = 10) -> tuple[int, str, str]:
    """run_read der Quellen: (rc, stdout, stderr) über couch.system."""
    r = system.run_read([str(a) for a in argv], timeout=float(timeout))
    return r.returncode, r.stdout or "", r.stderr or ""


def net_allowed() -> bool:
    """Darf der Bilder-Thread ins Netz? Im echten Betrieb ja. Im Prüfmodus nur, wenn der
    Bilder-Teil es erlaubt (eingesteckte Store-Attrappe oder COUCH_ART_NET=1) oder ein Test mit
    Attrappen-Auflöser COUCH_TEST_ART_NET=1 setzt."""
    if not paths.test_mode() or os.environ.get("COUCH_TEST_ART_NET") == "1":
        return True
    try:
        from . import wiring_art
        return bool(wiring_art.net_allowed())
    except Exception:           # Teil fehlt oder lädt nicht: im Prüfmodus dann lieber kein Netz
        return os.environ.get("COUCH_ART_NET") == "1"


def _plural(n: int, one: str, many: str) -> str:
    return "%d %s" % (n, one if n == 1 else many)


# ====================================================================== Bilder-Thread
class _Job:
    __slots__ = ("kind", "entry_id", "entry", "prio", "order", "seq", "force", "kinds", "forget",
                 "candidate", "future", "bulk")

    def __init__(self, kind: str, entry_id: str, prio: int, order: float = 0.0, **kw: Any):
        self.kind = kind                    # "resolve" | "fetch" | "candidate"
        self.entry_id = entry_id
        self.prio = prio
        self.order = order
        self.seq = 0
        self.entry = kw.get("entry")        # Eintrag der Quelle, mit dem eigenen Namen (LibraryPart._titled)
        self.force = bool(kw.get("force"))
        self.kinds = kw.get("kinds")
        # vor dem Abruf den Vermerk in artfetch.json löschen (nach dem Umbenennen, E99)
        self.forget = bool(kw.get("forget"))
        self.candidate = kw.get("candidate")
        self.future: Future | None = kw.get("future")
        self.bulk: _Bulk | None = kw.get("bulk")

    def key(self) -> tuple:
        return (self.prio, self.order, self.seq)


class _Bulk:
    """Ein Lauf „Cover und Hintergründe laden“ über alle Einträge."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.images = 0
        self.misses = 0
        self.errors = 0
        self.offline = False
        self.cancelled = False
        self.last_progress = 0.0

    def summary(self) -> str:
        if self.offline:
            head = TEXT_OFFLINE
            return head if not self.images else "%s %s." % (
                head, _plural(self.images, "Bild geladen", "Bilder geladen"))
        parts = [_plural(self.images, "Bild geladen", "Bilder geladen") if self.images
                 else "Keine neuen Bilder gefunden"]
        if self.misses:
            parts.append(_plural(self.misses, "Spiel ohne Treffer", "Spiele ohne Treffer"))
        if self.errors:
            parts.append(_plural(self.errors, "Abruf gescheitert", "Abrufe gescheitert"))
        return ", ".join(parts)


class ArtWorker:
    """EIN Thread mit Warteschlange für lokale Ableitungen und den Abruf aus dem Netz."""

    AUTO_PAUSE_S = 2.0          # Pause nach einem automatischen Abruf
    BULK_PAUSE_S = 0.3          # Pause zwischen den Spielen bei „alle laden“
    EMIT_EVERY_S = 2.0          # lib.changed nach Abrufen aus dem Netz höchstens so oft
    COLLECT_S = 1.0             # lokal aufgelöste Bilder (erster Start) in Schüben dieser Länge
    PROGRESS_EVERY_S = 0.5      # art.progress höchstens so oft
    OFFLINE_RETRY_S = 300.0     # ohne Netz: so lange ruhen die automatischen Abrufe
    NICE = 10                   # der Thread soll weder die Oberfläche noch ein Spiel bremsen

    def __init__(self, part: "LibraryPart"):
        self.part = part
        self._cond = threading.Condition()
        self._jobs: list[_Job] = []
        self._auto: dict[tuple, _Job] = {}      # (art, id) -> wartender Auftrag ohne Besteller
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._stop = False
        self._busy = False
        self.auto_open = False                  # erst nach dem Bereit-Signal ins Netz
        self.quiet = False                      # eine App ist vorn: automatische Abrufe ruhen
        self.allow_net = net_allowed()          # im Prüfmodus nur mit eingesteckter Attrappe
        self._next_net = 0.0
        self._offline_until = 0.0
        self._changed: dict[str, dict] = {}
        self._changed_since = 0.0
        self._changed_net = False
        self._last_emit = -1e9
        self.bulk: _Bulk | None = None

    # ------------------------------------------------------------ Aufträge
    def start(self) -> None:
        """Startet den Thread (spätestens der erste Auftrag tut das von selbst)."""
        with self._cond:
            if self._thread is not None or self._stop:
                return
            self._thread = threading.Thread(target=self._run, name="couch-art", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Beim Ende des Home: nichts Neues mehr anfangen. Kehrt sofort zurück."""
        with self._cond:
            self._stop = True
            jobs, self._jobs = self._jobs, []
            self._auto.clear()
            if self.bulk is not None:
                self.bulk.cancelled = True
            self._cond.notify_all()
        for job in jobs:
            job.bulk = None                         # keine Mitteilung mehr beim Beenden
            self._finish(job, {"ok": False, "status": "cancelled", "message": _STATUS_TEXT["cancelled"]})

    def wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def idle(self) -> bool:
        """Nichts in Arbeit und nichts, was ohne ein Ereignis von außen (Bereit-Signal, Schalter,
        App nicht mehr vorn) von selbst anliefe."""
        with self._cond:
            if self._busy or self._changed:
                return False
            job, wait = self._pick(time.monotonic())
            return job is None and wait is None

    def pending(self, kind: str | None = None) -> list[str]:
        with self._cond:
            return [j.entry_id for j in sorted(self._jobs, key=_Job.key) if kind in (None, j.kind)]

    def _put(self, job: _Job, auto: bool = False) -> _Job:
        with self._cond:
            if self._stop:
                self._finish(job, {"ok": False, "status": "cancelled",
                                   "message": _STATUS_TEXT["cancelled"]})
                return job
            if auto:
                old = self._auto.get((job.kind, job.entry_id))
                if old is not None:
                    # schon vorgemerkt: höchstens nach vorn ziehen, nie doppelt
                    if job.key()[:2] < old.key()[:2]:
                        old.prio, old.order = job.prio, job.order
                    if job.entry is not None:
                        old.entry = job.entry
                    old.forget = old.forget or job.forget
                    self._cond.notify_all()
                    return old
                self._auto[(job.kind, job.entry_id)] = job
            self._seq += 1
            job.seq = self._seq
            self._jobs.append(job)
            self._cond.notify_all()
        self.start()
        return job

    def resolve_later(self, entry: Any, order: float = 0.0) -> None:
        """Bilder dieses Eintrags lokal auflösen und nachtragen."""
        self._put(_Job("resolve", entry.id, PRIO_RESOLVE, order, entry=entry), auto=True)

    def fetch_auto(self, entry_id: str, new: bool = False, order: float = 0.0, forget: bool = False) -> None:
        """Automatischer Abruf, wenn der Auflöser ihn für lohnend hält (wants_fetch). forget: vorher
        den Vermerk der letzten Suche löschen (der Name hat sich geändert)."""
        self._put(_Job("fetch", entry_id, PRIO_NEW if new else PRIO_AUTO, order, forget=forget), auto=True)

    def fetch_now(self, entry_id: str) -> Future:
        """„Bilder neu laden“ für ein Spiel: vor allen anderen Aufträgen."""
        fut: Future = Future()
        self._put(_Job("fetch", entry_id, PRIO_MANUAL, force=True, kinds=FETCH_KINDS, future=fut))
        return fut

    def fetch_candidate(self, entry_id: str, candidate: dict) -> Future:
        fut: Future = Future()
        self._put(_Job("candidate", entry_id, PRIO_MANUAL, candidate=candidate, future=fut))
        return fut

    def fetch_all(self, entry_ids: list[str], force: bool = False) -> _Bulk | None:
        """Alle Einträge in die Warteschlange. None, wenn schon ein solcher Lauf läuft."""
        with self._cond:
            if self.bulk is not None or self._stop:
                return None
            bulk = self.bulk = _Bulk(len(entry_ids))
            for n, entry_id in enumerate(entry_ids):
                old = self._auto.pop(("fetch", entry_id), None)
                forget = False
                if old is not None and old in self._jobs:
                    self._jobs.remove(old)          # der Lauf deckt den automatischen Abruf ab
                    forget = old.forget             # … auch den Vermerk nach dem Umbenennen (E99)
                self._seq += 1
                job = _Job("fetch", entry_id, PRIO_BULK, float(n), force=force, bulk=bulk, forget=forget)
                job.seq = self._seq
                self._jobs.append(job)
            self._cond.notify_all()
        self._progress(bulk, "", force=True)
        return bulk

    # ------------------------------------------------------------ Schleife
    def _fetch_enabled(self) -> bool:
        try:
            return bool(self.part.app.config.get(CFG_FETCH, True))
        except Exception:
            return False

    def _pick(self, now: float) -> tuple[_Job | None, float | None]:
        """(nächster Auftrag, der jetzt laufen darf; sonst Wartezeit bis einer dürfte)."""
        wait: float | None = None
        enabled = None
        for job in sorted(self._jobs, key=_Job.key):
            if job.kind != "fetch" or job.prio == PRIO_MANUAL:
                return job, None
            if job.prio == PRIO_BULK:
                ready_at = self._next_net
            else:
                if enabled is None:
                    enabled = self._fetch_enabled()
                if not (self.auto_open and self.allow_net and enabled) or self.quiet:
                    continue                        # wartet auf ein Ereignis, nicht auf die Uhr
                ready_at = max(self._next_net, self._offline_until)
            if ready_at <= now:
                return job, None
            wait = ready_at - now if wait is None else min(wait, ready_at - now)
        return None, wait

    def _run(self) -> None:
        try:
            # nur dieser Thread (unter Linux gilt der Wert je Thread)
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), self.NICE)
        except (OSError, AttributeError):
            pass
        while True:
            with self._cond:
                while True:
                    if self._stop:
                        return
                    now = time.monotonic()
                    self._flush(now)
                    job, wait = self._pick(now)
                    if job is not None:
                        self._jobs.remove(job)
                        if self._auto.get((job.kind, job.entry_id)) is job:
                            del self._auto[(job.kind, job.entry_id)]
                        self._busy = True
                        break
                    if self._changed:
                        due = max(0.0, self._last_emit + self._gap() - now)
                        wait = due if wait is None else min(wait, due)
                    self._cond.wait(None if wait is None else max(0.01, wait))
            try:
                result = self._execute(job)
            except Exception:
                _log.exception("Bilder-Thread: %s für %s", job.kind, job.entry_id)
                result = {"ok": False, "status": "error", "message": _STATUS_TEXT["error"]}
            self._finish(job, result)
            with self._cond:
                self._busy = False
                self._flush(time.monotonic())
                self._cond.notify_all()

    def _entry(self, job: _Job) -> Any:
        library = self.part.app.library
        current = library.get(job.entry_id) if library is not None else None
        return current if current is not None else job.entry

    def _execute(self, job: _Job) -> dict:
        part = self.part
        resolver = part.wiring.art
        if job.kind == "resolve":
            entry = job.entry if job.entry is not None else self._entry(job)
            if entry is None or resolver is None:
                return {"ok": False, "status": "skip"}
            self._patch(entry.id, part.resolve_art(entry), net=False)
            return {"ok": True, "status": "ok"}
        entry = self._entry(job)
        if entry is None:
            return {"ok": False, "status": "skip", "message": TEXT_NO_ENTRY}
        if resolver is None:
            return {"ok": False, "status": "error", "message": TEXT_NO_ART}
        if job.kind == "candidate":
            detailed = getattr(resolver, "fetch_candidate_detail", None)
            fetch = getattr(resolver, "fetch_candidate", None)
            if detailed is not None:
                got = detailed(entry, job.candidate)
                got = got if isinstance(got, dict) else {"ok": bool(got)}
            elif fetch is not None:
                got = {"ok": bool(fetch(entry, job.candidate))}
            else:
                return {"ok": False, "status": "error", "message": TEXT_NO_ART}
            ok = bool(got.get("ok"))
            status = str(got.get("status") or ("ok" if ok else "error"))
            if status not in _NO_NET_STATUS:
                self._next_net = time.monotonic() + self.BULK_PAUSE_S
            if ok:
                self._patch(entry.id, part.resolve_art(entry))
                return {"ok": True, "status": status, "message": "Das Bild wurde übernommen."}
            text = _STATUS_TEXT.get(status) if status in ("offline", "disabled", "resting", "pick-failed") else None
            return {"ok": False, "status": status,
                    "message": text or "Dieses Bild konnte nicht geladen werden."}
        manual = job.prio == PRIO_MANUAL
        if job.forget:
            forget = getattr(resolver, "forget_fetch", None)
            if forget is not None:
                forget(entry.id)
        if not manual and job.bulk is None:
            # automatisch: nur, wenn sich ein Abruf lohnt (schnell, ohne Netz)
            if not resolver.wants_fetch(entry):
                return {"ok": False, "status": "skip"}
        if job.bulk is not None and (job.bulk.cancelled or job.bulk.offline):
            return {"ok": False, "status": "cancelled"}
        if job.bulk is not None and not job.force and not resolver.wants_fetch(entry):
            return {"ok": False, "status": "skip"}
        detail = self._fetch(resolver, entry, job)
        status = str(detail.get("status") or ("ok" if detail.get("ok") else "miss"))
        now = time.monotonic()
        if status not in _NO_NET_STATUS:
            self._next_net = now + (self.AUTO_PAUSE_S if job.bulk is None and not manual
                                    else self.BULK_PAUSE_S)
        if status == "offline" and not manual:
            self._offline_until = now + self.OFFLINE_RETRY_S
            if job.bulk is None:
                # später noch einmal: Der Eintrag soll sein Bild bekommen, sobald das Netz da ist
                self.fetch_auto(job.entry_id, new=job.prio == PRIO_NEW, order=job.order)
        if detail.get("ok"):
            self._patch(entry.id, part.resolve_art(entry))
        files = detail.get("files") if isinstance(detail.get("files"), dict) else {}
        return {"ok": bool(detail.get("ok")), "status": status, "images": len(files) or int(bool(detail.get("ok"))),
                "message": _STATUS_TEXT.get(status, _STATUS_TEXT["error"]), "title": entry.title}

    @staticmethod
    def _fetch(resolver: Any, entry: Any, job: _Job) -> dict:
        fetch_detail = getattr(resolver, "fetch_detail", None)
        if fetch_detail is not None:
            detail = fetch_detail(entry, job.kinds, job.force)
            return detail if isinstance(detail, dict) else {"ok": bool(detail)}
        return {"ok": bool(resolver.fetch(entry, job.kinds, job.force))}

    def _finish(self, job: _Job, result: dict) -> None:
        if job.future is not None and not job.future.done():
            job.future.set_result({k: v for k, v in result.items() if k not in ("images", "title")})
        bulk = job.bulk
        if bulk is None or bulk.cancelled:
            return
        status = result.get("status")
        bulk.done += 1
        if result.get("ok"):
            bulk.images += int(result.get("images") or 1)
        elif status in ("miss", "backoff"):
            bulk.misses += 1
        elif status in ("error", "resting"):
            bulk.errors += 1                        # ruhende Dienste sind eine Störung, kein Fehltreffer (E99)
        elif status == "offline":
            bulk.offline = True                     # der Rest des Laufs wird übersprungen
        last = bulk.done >= bulk.total
        self._progress(bulk, str(result.get("title") or ""), force=last)
        if last:
            with self._cond:
                if self.bulk is bulk:
                    self.bulk = None
            self.part.bulk_finished(bulk)

    def _progress(self, bulk: _Bulk, title: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - bulk.last_progress < self.PROGRESS_EVERY_S:
            return
        bulk.last_progress = now
        self.part.post(self.part.show_progress, bulk.done, bulk.total, title, bulk.done >= bulk.total)

    # ------------------------------------------------------------ Änderungen melden
    def _patch(self, entry_id: str, art: Any, net: bool = True) -> None:
        if not isinstance(art, dict):
            return
        with self._cond:
            if not self._changed:
                self._changed_since = time.monotonic()
                self._changed_net = False
            self._changed[entry_id] = art
            self._changed_net = self._changed_net or net

    def _gap(self) -> float:
        """Mindestabstand zur letzten Meldung: nach Abrufen 2 s, für nur lokal Aufgelöstes kürzer."""
        return self.EMIT_EVERY_S if self._changed_net else min(self.COLLECT_S, self.EMIT_EVERY_S)

    def _flush(self, now: float) -> None:
        """Mit gehaltener Sperre: gesammelte Bilder an den Index, nie öfter als _gap()."""
        if not self._changed or now - self._last_emit < self._gap():
            return
        if now - self._changed_since < self.COLLECT_S and any(j.kind == "resolve" for j in self._jobs):
            # Erster Start: Die Kacheln sollen in Schüben erscheinen, nicht eine vorweg.
            # Auflösungen laufen ohne Pause durch, also kommt diese Stelle gleich wieder dran.
            return
        arts, self._changed = self._changed, {}
        self._last_emit = now
        self.part.post(self.part.apply_art, arts)


# ====================================================================== der Teil
class LibraryPart:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.worker = ArtWorker(self)
        self.default_proton = ""
        self._seen: set[str] = set()
        self._ready = False
        self._warned: dict[str, tuple] = {}
        self._budget = ART_BUDGET_S
        self._launch_ctx: Any = None
        self._launch_ctx_failed = False
        self._candidates: dict[str, list] = {}      # id -> Treffer der letzten Suche
        self._outside_warned = False
        self._art_roots: list[str] | None = None
        self._art_rpc_done = False
        self._first_source = ""
        self._places: dict[str, str] = {}           # aufgelöste Orte für die Anzeige (describe_env)

    # ------------------------------------------------------------ Hauptschleife
    def call(self, fn: Callable, *args: Any) -> Any:
        main = getattr(self.wiring, "main", None)
        return main.call(fn, *args) if main is not None else fn(*args)

    def post(self, fn: Callable, *args: Any) -> None:
        main = getattr(self.wiring, "main", None)
        if main is not None:
            main.post(fn, *args)
        else:
            fn(*args)

    def bg(self, fn: Callable, *args: Any) -> Future:
        run = getattr(self.wiring, "_bg", None) or self.app.background
        return run(fn, *args)

    # ------------------------------------------------------------ Quellen
    def make_context(self) -> Any:
        from .library import sources as lib_sources
        if paths.test_mode():
            # Prüfmodus: Der Kontext zeigt von selbst in den Attrappen-Baum
            # (<COUCH_FIXTURES>/home, /games, /flatpak) und fragt den echten Rechner nichts.
            return lib_sources.SourceContext(data_dir=paths.data_dir(), cache_dir=paths.cache_dir())
        return lib_sources.SourceContext(games_disk=paths.games_disk(), data_dir=paths.data_dir(),
                                         cache_dir=paths.cache_dir(), run_read=_read3)

    def allow_steam_art(self) -> None:
        """Liegt Steam nicht unter ~/.local/share/Steam (anderer Ort, Flatpak-Steam), lieferte
        ui/host.py die Bilder aus seinem Zwischenspeicher und dem Grid-Ordner nicht aus: Die
        Wurzel der Bibliothek wird deshalb in die Erlaubnisliste aufgenommen (vor dem Start der
        Fenster). Im Prüfmodus deckt COUCH_FIXTURES den Attrappen-Baum schon ab."""
        root = getattr(self.wiring.sources, "steam_root", None)
        if not root or paths.test_mode():
            return
        try:
            if os.path.isdir(root) and not paths.is_under(root, paths.art_roots()):
                if paths.add_art_root(root):
                    _log.info("Steam liegt unter %s: als Wurzel für Bilder angemeldet", root)
                else:
                    _log.warning("Steam liegt unter %s: Bilder von dort werden nicht ausgeliefert", root)
        except OSError:
            _log.exception("Steam-Wurzel für Bilder")

    def register_sources(self) -> None:
        from .library import register_source
        from .library import sources as lib_sources
        ctx = self.wiring.sources
        bound = dict(lib_sources.bound_sources(ctx))
        bound.update(lib_sources.bound_derived(ctx))    # Verknüpfungen (E25), nach den anderen
        self._first_source = next(iter(bound), "")
        for name, fn in bound.items():
            register_source(name, self.wrap(fn, name))
        library = getattr(self.app, "library", None)
        add_volume = getattr(library, "add_volume", None)
        if add_volume is not None and getattr(ctx, "games_disk", None):
            # im Attrappen-Baum ist die „Platte“ ein gewöhnlicher Ordner
            add_volume(ctx.games_disk, mount=bool(getattr(ctx, "is_real", False)))

    def inactive_sources(self) -> set:
        """Für den Index (seen_sources, E108): Quellen, die gerade ausgeschaltet sind. Ihr leeres Ergebnis
        zählt nicht als gesehen; sonst flutete ihr Bestand beim Einschalten das Home (ROM-Ordner)."""
        from .library import roms
        ctx = self.wiring.sources
        return set() if ctx is None or roms.enabled(ctx) else {"roms"}

    def wrap(self, fn: Callable[[], list], name: str = "") -> Callable[[], list]:
        """Eine Quelle für den Index: einlesen, Warnungen ins Protokoll, Bilder dazu."""
        def load() -> list:
            if name and name == self._first_source:
                # Ein Budget je Lauf über alle Quellen. Allererster Start (noch kein Eintrag):
                # gar keins, erst die Kacheln, die Bilder trägt der Bilder-Thread sofort nach.
                pending = getattr(getattr(self.app, "library", None), "first_scan_pending", None)
                cold = pending is not None and pending() is not None
                self._budget = 0.0 if cold else ART_BUDGET_S
            entries = fn()
            ctx = self.wiring.sources
            if name and ctx is not None:
                if name == self._first_source:
                    self._note_places(ctx)
                self._log_warnings(ctx, name)
                if name in getattr(ctx, "failed", ()):
                    from .library.index import SourceSkipped
                    raise SourceSkipped("%s konnte gerade nicht lesen" % name)
            self.dress(entries)
            return entries
        return load

    def _note_places(self, ctx: Any) -> None:
        """Spiele-Platte und Home in aufgelöster Schreibweise (im Thread des Einlesens: realpath
        fasst die Platte an). lib.get kürzt damit Pfade zu „Spiele-Platte/…“ und „~/…“."""
        places = {}
        for key in ("games_disk", "home"):
            value = getattr(ctx, key, None)
            if isinstance(value, str) and value:
                try:
                    places[key] = os.path.realpath(value)
                except OSError:
                    places[key] = value
        self._places = places

    def _log_warnings(self, ctx: Any, name: str) -> None:
        mine = tuple(w for w in list(getattr(ctx, "warnings", ())) if w.startswith(name + ": "))
        if mine == self._warned.get(name, ()):
            return                                  # dieselben wie beim letzten Lauf: nicht wiederholen
        self._warned[name] = mine
        for text in mine:
            _log.warning("Bibliothek: %s", text)
        if not mine:
            _log.info("Bibliothek: Quelle %s meldet keine Warnungen mehr", name)

    def dress(self, entries: list) -> None:
        """Im Thread des Einlesens: Bilder und Ort der Spielstände je Eintrag. Aufgelöst wird mit
        dem eigenen Namen (_titled), das Bild landet am Eintrag der Quelle."""
        library = getattr(self.app, "library", None)
        for entry in entries:
            self._saves(entry)
            if self.wiring.art is None:
                continue
            known = library.get(entry.id) if library is not None else None
            if self._budget <= 0:
                # Budget verbraucht: letztes bekanntes Bild zeigen, der Bilder-Thread trägt nach
                if known is not None and isinstance(known.art, dict):
                    entry.art = dict(known.art)
                stamp = (known.last_played if known is not None else entry.last_played) or 0
                self.worker.resolve_later(self._titled(entry), order=-float(stamp))
                continue
            started = time.monotonic()
            try:
                entry.art = self.resolve_art(self._titled(entry))
            except Exception:
                _log.exception("Bilder für %s", entry.id)
            spent = time.monotonic() - started
            if spent > ART_WARM_S:
                self._budget -= spent

    def dress_one(self, entry: Any) -> None:
        """Ein einzelner neuer Eintrag (Archiv-Installation): sofort mit Bildern, ohne Budget."""
        self._saves(entry)
        if self.wiring.art is None:
            return
        try:
            entry.art = self.resolve_art(self._titled(entry))
        except Exception:
            _log.exception("Bilder für %s", entry.id)

    def _titled(self, entry: Any) -> Any:
        """Der Eintrag einer Quelle mit dem eigenen Namen (E90), wie ihn der Bilder-Thread aus dem
        Index bekommt: Platzhalter und Merkzettel der Bilder hängen am Titel. Mit dem Namen der Quelle
        zeigte die Kachel den alten Namen, und jeder Wechsel zwischen Einlesen und Bilder-Thread
        erzeugte die Bilder neu und löschte die der anderen Fassung (E99)."""
        titled = getattr(getattr(self.app, "library", None), "with_own_title", None)
        if titled is None:
            return entry
        try:
            return titled(entry)
        except Exception:
            _log.exception("eigener Name für %s", getattr(entry, "id", "?"))
            return entry

    def renamed(self, entry: Any) -> None:
        """Nach lib.rename (Hauptschleife, Haken LibraryIndex.on_rename): die Bilder mit dem neuen
        Namen auflösen (Platzhalter tragen ihn) und, wenn das Spiel abrufbar ist, im Netz mit dem
        neuen Namen suchen. Der Vermerk der letzten Suche (nach „kein Treffer“ 7 Tage Sperre) galt
        dem alten Namen; der Bilder-Thread löscht ihn vor dem Abruf, eine gemerkte Trefferwahl bleibt
        (E90/E99)."""
        if self.wiring.art is None:
            return
        self.worker.resolve_later(entry, order=float("-inf"))
        from .library import describe
        if describe.fetchable(entry):
            self.worker.fetch_auto(entry.id, new=True, order=float("-inf"), forget=True)

    def resolve_art(self, entry: Any) -> dict:
        """Bilder eines Eintrags aus lokalen Quellen. Blockierend (kalt 0,1 bis 0,3 s)."""
        resolver = self.wiring.art
        grid = self._grid(entry)
        art = resolver.resolve(entry) if grid is None else resolver.resolve(entry, grid)
        self._check_roots(art)
        return _stamped(art)

    def _grid(self, entry: Any) -> dict | None:
        """Bilder aus Steams grid-Ordner für diesen Eintrag (None = der Auflöser sucht selbst)."""
        if entry.kind == "app":
            return None
        if entry.source == "steam":
            if entry.detail.get("shortcut"):
                art = entry.detail.get("steam_art")
                return dict(art) if isinstance(art, dict) else {}
            return None
        ctx = self.wiring.sources
        if ctx is None:
            return None
        try:
            from .library import steam
            match = steam.match_shortcut(entry, steam.shortcut_art(ctx))
        except Exception:
            _log.exception("Verknüpfung für %s", entry.id)
            return None
        return dict(match["grid"]) if match else None

    def _check_roots(self, art: Any) -> None:
        """Was die Seite laden soll, muss unter paths.art_roots() liegen (ui/host.py liefert
        sonst nicht aus). Einmal ins Protokoll, damit es beim Zusammenführen auffällt."""
        if self._outside_warned or not isinstance(art, dict):
            return
        if self._art_roots is None:
            self._art_roots = [r.rstrip(os.sep) + os.sep for r in paths.art_roots()]
        for url in art.values():
            path = _art_path(url)
            if not path or path.startswith(tuple(self._art_roots)):
                continue                            # der Auflöser liefert aufgelöste Pfade
            if not paths.is_under(path, paths.art_roots()):
                self._outside_warned = True
                _log.warning("Bild außerhalb der Erlaubnisliste paths.art_roots(): %s", path)
                return

    def _saves(self, entry: Any) -> None:
        """Wo liegen die Spielstände (der Prefix)? Ein paar stat-Aufrufe über die Starter."""
        if entry.source not in ("proton", "local") or self._launch_ctx_failed:
            return
        try:
            from .launch import builders
            # Der Kontext der Starter (wiring.launch), damit hier derselbe Ordner herauskommt
            # wie beim Start; gibt es ihn (noch) nicht, ein eigener mit denselben Orten
            launch_ctx = getattr(self.wiring, "launch", None)
            if launch_ctx is None:
                if self._launch_ctx is None:
                    ctx = self.wiring.sources
                    self._launch_ctx = builders.LaunchContext(
                        config=getattr(self.wiring, "config", None) or self.app.config,
                        data_dir=paths.data_dir(), state_dir=paths.state_dir(), app_dir=paths.app_dir(),
                        home=ctx.home if ctx is not None else paths.home(), run_read=_read3)
                launch_ctx = self._launch_ctx
            info = builders.prefix_info(entry, launch_ctx)
        except Exception:
            self._launch_ctx_failed = True          # kein zweiter Versuch je Eintrag
            _log.exception("Ort der Spielstände nicht bestimmbar")
            return
        if isinstance(info, dict) and info.get("path"):
            entry.detail["saves"] = {"path": str(info["path"]), "mode": str(info.get("mode") or ""),
                                     "pending": bool(info.get("adopt"))}

    # ------------------------------------------------------------ Hauptschleife: Änderungen
    def apply_art(self, arts: dict) -> None:
        library = self.app.library
        try:
            changed = library.update_art(arts)
        except Exception:
            _log.exception("Bilder nachtragen")
            return
        if changed:
            self.app.rpc.emit("lib.changed", {"ids": changed, "reason": "art"})

    def show_progress(self, done: int, total: int, title: str, finished: bool) -> None:
        self.app.rpc.emit("art.progress", {"done": done, "total": total, "title": title,
                                           "finished": finished})
        self.app.state.update(art_job=None if finished else {"done": done, "total": total})

    def bulk_finished(self, bulk: _Bulk) -> None:
        text = bulk.summary()
        _log.info("Bilder laden: %s (%d Einträge)", text, bulk.total)
        self.post(lambda: self.app.notify.push("Bilder geladen" if not bulk.offline
                                               else "Bilder nicht geladen", text, icon="info"))

    def library_changed(self) -> None:
        """Beobachter des Index (Hauptschleife): jeder neue Eintrag sofort in die Warteschlange."""
        try:
            entries = self.app.library.entries()
        except Exception:
            _log.exception("Bibliothek lesen")
            return
        for entry in entries:
            if entry.id in self._seen:
                continue
            self._seen.add(entry.id)
            if entry.kind == "game":
                # nach dem Start entstandene Einträge zuerst (E34: ohne Warten auf den nächsten Start)
                self.worker.fetch_auto(entry.id, new=self._ready, order=-float(entry.last_played or 0))

    def front_changed(self) -> None:
        try:
            quiet = self.app.focus.foreground is not None
        except Exception:
            quiet = False
        if quiet != self.worker.quiet:
            self.worker.quiet = quiet
            self.worker.wake()

    def proc_changed(self, running: Any, what: str) -> None:
        if what == "exited":
            # Prefix-Übernahme, Spielzeit: was sich mit einem Start ändert, neu lesen
            try:
                self.app.library.rescan()
            except Exception:
                _log.exception("Neu einlesen nach dem Spielende")

    def ready(self) -> None:
        """Nach dem Bereit-Signal (Hauptschleife): Jetzt darf der Bilder-Thread ins Netz."""
        self._ready = True
        self.worker.auto_open = True
        self.worker.wake()
        self.bg(self._read_default_proton)

    def stop(self) -> None:
        """Haken für wiring.stop(): kehrt sofort zurück."""
        self.worker.stop()
        cancel = getattr(self.wiring.art, "cancel", None)
        if cancel is not None:
            try:
                cancel()                            # ein Abruf, der gerade im Netz hängt, bricht ab
            except Exception:
                _log.exception("Bilder-Abruf abbrechen")

    def _read_default_proton(self, *_args: Any) -> None:
        try:
            from . import wiring_settings
            value = str(wiring_settings.resolve_proton(self.app) or "")
        except Exception:
            return                                  # Teil fehlt oder kann es (noch) nicht
        self.default_proton = value

    def describe_env(self) -> dict:
        """Für lib.get (library/describe.py). Nur Werte aus dem Speicher."""
        cfg = self.app.config
        ctx = self.wiring.sources
        has_art = self.wiring.art is not None
        return {
            "games_disk": self._places.get("games_disk") or getattr(ctx, "games_disk", "") or "",
            "home": self._places.get("home") or getattr(ctx, "home", "") or "",
            "default_proton": self.default_proton,
            "globals": {"fsr4": bool(cfg.get("games.fsr4", True)), "hdr": bool(cfg.get("games.hdr", True)),
                        "pad_auto": bool(cfg.get("games.pad_auto", True)),
                        "freeze": bool(cfg.get("games.freeze", True))},
            "art": has_art,
            "fetch": has_art and self.worker.allow_net and bool(cfg.get(CFG_FETCH, True)),
            "delete": getattr(self.wiring, "installer", None) is not None,
            "games_root": self._games_root(),
        }

    def _games_root(self) -> str:
        """Spiele-Ordner des Installers, nur aus der Einstellung (E99, describe.deletable); "" = unbekannt."""
        try:
            from . import wiring_install
            return str(wiring_install.games_root(self.app) or "")
        except Exception:
            return ""

    # ------------------------------------------------------------ RPC
    def need(self, params: dict, art: bool = True) -> Any:
        entry = self.app.library.get(str(params.get("id", "")))
        if entry is None:
            raise RpcError(TEXT_NO_ENTRY, "NotFound")
        if art and self.wiring.art is None:
            raise RpcError(TEXT_NO_ART, "NoArt")
        return entry

    def refresh_art(self, entry: Any) -> None:
        """Nach einer eigenen Bildwahl: sofort neu auflösen (im Arbeits-Thread gerufen)."""
        if entry.source == "fixture":
            # Prüf-Einträge bringen ihre Bilder aus ihrer Datei mit: neu einlesen
            self.post(lambda: self.app.library.rescan())
            return
        try:
            art = self.resolve_art(entry)
        except Exception:
            _log.exception("Bilder für %s", entry.id)
            return
        self.post(self.apply_art, {entry.id: art})

    def register_art_rpc(self) -> None:
        """lib.setArt {id, kind, path} und lib.resetArt {id} (E27)."""
        app = self.app
        if self._art_rpc_done:
            return
        self._art_rpc_done = True

        def set_art(params: dict) -> dict:
            from .library import art as art_mod
            entry = self.need(params)
            path = str(params.get("path") or "")
            # nur Dateien von dort, wo auch die Dateiauswahl hinkommt (und schon bekannte Bilder)
            if not path or not os.path.isabs(path) or not os.path.isfile(path) \
                    or not paths.is_under(path, paths.browse_roots() + paths.art_roots()):
                raise RpcError("Dieses Bild liegt an keinem erlaubten Ort.", "BadPath")
            try:
                self.wiring.art.set_user_art(entry.id, str(params.get("kind") or ""), path)
            except art_mod.ArtError as e:
                raise RpcError(str(e), "ArtFailed") from None
            self.refresh_art(entry)
            return {"ok": True}

        def reset_art(params: dict) -> dict:
            entry = self.need(params)
            resolver = self.wiring.art
            removed = int(resolver.clear_user_art(entry.id))
            forget = getattr(resolver, "clear_choice", None)
            if forget is not None:
                forget(entry.id)                    # auch die von Hand gewählte Fundstelle
            cleared = int(resolver.clear_cache(entry.id))
            self.refresh_art(entry)
            # danach frisch aus dem Netz holen, wenn das erlaubt ist (Ergebnis kommt als lib.changed)
            fetching = self.fetch_possible() is None and entry.kind == "game"
            if fetching:
                self.worker.fetch_now(entry.id)
            return {"ok": True, "removed": removed, "cleared": cleared, "fetching": fetching}

        app.rpc.register("lib.setArt", lambda p: self.bg(set_art, p))
        app.rpc.register("lib.resetArt", lambda p: self.bg(reset_art, p))

    def fetch_possible(self) -> str | None:
        """None, wenn Bilder aus dem Netz geladen werden dürfen; sonst der Grund als Satz."""
        if self.wiring.art is None:
            return TEXT_NO_ART
        if not self.worker.allow_net:
            return TEXT_TEST_MODE
        if not bool(self.app.config.get(CFG_FETCH, True)):
            return TEXT_FETCH_OFF
        return None

    def register_fetch_rpc(self) -> None:
        """lib.fetchArt, lib.artCandidates, lib.artPreview, lib.artBrowse (E34)."""
        app = self.app

        def fetch_art(params: dict) -> Any:
            if not params.get("id"):
                return fetch_all_art(app, force=bool(params.get("force")))
            entry = self.need(params)
            why = self.fetch_possible()
            if why is not None:
                return {"ok": False, "status": "disabled", "message": why}
            candidate = params.get("candidate")
            if candidate is None:
                return self.worker.fetch_now(entry.id)
            if not isinstance(candidate, dict):
                raise RpcError("Dieser Treffer ist ungültig.", "BadCandidate")
            # Nur was die letzte Suche für diesen Eintrag geliefert hat, und zwar deren
            # eigenes Objekt: Die Seite gibt nur an, welcher Treffer gemeint ist.
            known = next((c for c in self._candidates.get(entry.id, [])
                          if isinstance(c, dict) and c.get("source") == candidate.get("source")
                          and c.get("ref") == candidate.get("ref")), None)
            if known is None:
                raise RpcError("Dieser Treffer ist nicht mehr gültig. Suche bitte noch einmal.",
                               "BadCandidate")
            return self.worker.fetch_candidate(entry.id, known)

        def candidates(params: dict) -> dict:
            entry = self.need(params)
            why = self.fetch_possible()
            if why is not None:
                raise RpcError(why, "NoNet")
            search = getattr(self.wiring.art, "candidates", None)
            if search is None:
                raise RpcError(TEXT_NO_ART, "NoArt")
            query = str(params.get("query") or "").strip() or None
            found = search(entry, query=query)
            found = [c for c in (found or []) if isinstance(c, dict)]
            self._candidates[entry.id] = found
            for cand in found:
                self._check_roots({"thumb": cand.get("thumb")})
            return {"candidates": found}

        def preview(params: dict) -> dict:
            from .library import artbrowse
            if self.wiring.art is None:
                raise RpcError(TEXT_NO_ART, "NoArt")
            path = str(params.get("path") or "")
            if not artbrowse.allowed_image(path, paths.browse_roots()):
                raise RpcError("Dieses Bild liegt an keinem erlaubten Ort.", "BadPath")
            make = getattr(self.wiring.art, "preview", None)
            url = make(path) if make is not None else None
            if not url:
                raise RpcError("Die Datei ist kein lesbares Bild.", "ArtFailed")
            self._check_roots({"preview": url})
            return {"url": url}

        def browse(params: dict) -> dict:
            from .library import artbrowse
            roots = paths.browse_roots()
            if params.get("roots") or ("path" in params and not params.get("path")):
                return artbrowse.list_images(None, roots)
            path = params.get("path") or artbrowse.start_dir(roots)
            return artbrowse.list_images(path, roots)

        app.rpc.register("lib.fetchArt", fetch_art)
        app.rpc.register("lib.artCandidates", lambda p: self.bg(candidates, p))
        app.rpc.register("lib.artPreview", lambda p: self.bg(preview, p))
        app.rpc.register("lib.artBrowse", lambda p: self.bg(browse, p))


# Eigene und geladene Bilder behalten ihren Dateinamen, wenn sie ersetzt werden
_SAME_NAME = ("user-", "fetch-")


def _stamped(art: Any) -> Any:
    """Bilder, die unter gleichbleibendem Namen ersetzt werden (user-<art>, fetch-<art>) und die
    der Auflöser unverändert durchreicht, bekommen ihren Zeitstempel als ?v= angehängt: Der
    WebView hielte sonst bis zu zehn Minuten das alte Bild im Zwischenspeicher (ui/host.py
    schneidet die Abfrage beim Ausliefern ab)."""
    if not isinstance(art, dict):
        return art
    for key, url in list(art.items()):
        path = _art_path(url)
        if not path or "?" in url or not os.path.basename(path).startswith(_SAME_NAME):
            continue
        try:
            art[key] = "%s?v=%x" % (url, os.stat(path).st_mtime_ns)
        except OSError:
            pass
    return art


def _art_path(url: Any) -> str | None:
    """Pfad hinter einer couch://art/-Adresse (wie library/art.art_path), sonst None."""
    prefix = "couch://art/"
    if not isinstance(url, str) or not url.startswith(prefix):
        return None
    from urllib.parse import unquote
    path = unquote(url[len(prefix):].split("?", 1)[0].split("#", 1)[0])
    return path if path.startswith("/") else None


# ====================================================================== Einhängen
def _part(app: Any) -> LibraryPart | None:
    wiring = getattr(app, "wiring", None)
    part = getattr(wiring, "library_part", None)
    return part if part is not None else _PARTS.get(id(app))


def setup(wiring: Any) -> LibraryPart:
    """Von Wiring._library: Kontext, Bilder-Auflöser, Quellen. Wirft, wenn die Quellen sich
    nicht anmelden lassen (Wiring.run vermerkt das und lässt den Rest starten)."""
    part = LibraryPart(wiring)
    wiring.library_part = part
    _PARTS[id(wiring.app)] = part
    wiring.sources = part.make_context()
    part.allow_steam_art()
    try:
        wiring._make_art()
    except Exception:
        _log.exception("Bilder der Bibliothek: Auflöser startet nicht, Kacheln bleiben ohne Bild")
    part.register_sources()
    return part


def register_art_rpc(wiring: Any) -> None:
    """Für Wiring._art_rpc: lib.setArt und lib.resetArt."""
    part = getattr(wiring, "library_part", None)
    if part is not None:
        part.register_art_rpc()


def with_art(wiring: Any, fn: Callable[[], list]) -> Callable[[], list]:
    """Eine Quellen-Funktion, die ihren Einträgen die Bilder mitgibt (LibraryPart.wrap); ohne den
    Bibliotheks-Teil fn selbst."""
    part = getattr(wiring, "library_part", None)
    if part is None:
        return fn
    return part.wrap(fn)


def extend(wiring: Any) -> None:
    """Hängt alles ein, was über das Anmelden der Quellen hinausgeht. Jeder Schritt für sich:
    Ein Fehler hier darf weder das Home noch die anderen Teile am Start hindern."""
    app = wiring.app
    part = getattr(wiring, "library_part", None)
    if part is None:
        # Wiring._library ist gescheitert oder lief nicht: so viel wie möglich nachholen
        try:
            part = setup(wiring)
        except Exception:
            _log.exception("Bibliothek: Quellen lassen sich nicht anmelden")
            part = getattr(wiring, "library_part", None)
            if part is None:
                try:
                    part = LibraryPart(wiring)
                    wiring.library_part = part
                    _PARTS[id(app)] = part
                except Exception:
                    _log.exception("Bibliothek: Teil lässt sich nicht anlegen")
                    return

    def step(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            _log.exception("Bibliothek: %s", name)

    library = getattr(app, "library", None)
    step("Vorgaben", lambda: app.config.add_defaults(dict(DEFAULTS)))
    step("Angaben für lib.get", lambda: setattr(library, "describe_env", part.describe_env))
    step("ausgeschaltete Quellen", lambda: setattr(library, "inactive_sources", part.inactive_sources))
    step("RPC lib.setArt", part.register_art_rpc)
    step("RPC lib.fetchArt", part.register_fetch_rpc)
    step("Beobachter Bibliothek", lambda: library.on_change(part.library_changed))
    step("Beobachter Umbenennen", lambda: library.on_rename(part.renamed))
    step("Beobachter Vordergrund", lambda: app.focus.on_change(part.front_changed))
    step("Beobachter Apps", lambda: app.procs.on_change(part.proc_changed))
    step("Beobachter Einstellungen", lambda: (
        app.config.on_change(CFG_FETCH, lambda key, value: part.worker.wake()),
        app.config.on_change("games.proton", lambda key, value: part.bg(part._read_default_proton))))
    step("Zustand", lambda: app.state.update(art_job=None))
    step("bekannte Einträge", part.library_changed)
    step("Haken", lambda: (wiring.after_ready.append(part.ready),
                           wiring.shutdown_hooks.append(part.stop)))
    step("Bilder-Thread", part.worker.start)


# ====================================================================== für die anderen Teile
def _need_part(app: Any) -> LibraryPart:
    part = _part(app)
    if part is None:
        raise RuntimeError("Die Bibliothek ist nicht eingehängt")
    return part


def add_local_record(app: Any, record: dict) -> str | None:
    """Datensatz des Archiv-Installers einfügen ODER ersetzen (gleiche id). Liefert die
    Eintrags-ID; None bei ROM-Installationen und unbrauchbaren Datensätzen (dann wird nur neu
    eingelesen: Das Spiel erscheint über die Emulator-Quelle). Blockierend (FSR-Scan, Bilder):
    im Arbeits-Thread rufen. library.json schreibt dabei nur der Index (E28)."""
    from .library import local as local_mod
    part = _need_part(app)
    ctx = part.wiring.sources
    entry = local_mod.record_to_entry(ctx, record) if ctx is not None and isinstance(record, dict) else None
    if entry is None:
        _log.info("Installation ohne eigenen Eintrag (%s), lese die Bibliothek neu",
                  record.get("platform") if isinstance(record, dict) else "?")
        part.post(lambda: app.library.rescan())
        return None
    part.dress_one(entry)
    part.call(app.library.add_local, entry)
    return entry.id


def get_local_record(app: Any, entry_id: str) -> dict | None:
    """Der Datensatz (library/local.py) eines eigenen Archiv-Spiels oder None."""
    from .library import local as local_mod
    entry = app.library.get(entry_id) if isinstance(entry_id, str) else None
    if entry is None or entry.source != "local":
        return None
    return local_mod.entry_to_record(entry)


def remove_local(app: Any, entry_id: str) -> bool:
    """Eigenen Eintrag aus der Bibliothek nehmen (Dateien bleiben; die löscht nur der Installer)."""
    part = _part(app)
    if part is None:
        return bool(app.library.remove_local(entry_id))
    return bool(part.call(app.library.remove_local, entry_id))


def local_entries(app: Any) -> list[dict]:
    """[{id, title, install_dir, exe, path}] der Archiv-Spiele (path = install_dir, so erwartet
    es die Speicher-Seite)."""
    out = []
    for entry in app.library.entries():
        if entry.source != "local" or not entry.id.startswith("local:"):
            continue
        folder = str(entry.detail.get("install_dir") or "")
        out.append({"id": entry.id, "title": entry.title, "install_dir": folder,
                    "exe": str(entry.detail.get("exe") or ""), "path": folder})
    return out


def game_dirs(app: Any) -> list[dict]:
    """[{id, title, path, source}] aller Spiele mit bekanntem Ordner (Emulator-Spiele ohne
    eigenen Ordner mit ihrer Datei). Rein aus dem Speicher."""
    from .library import describe
    out = []
    for entry in app.library.entries():
        if entry.kind != "game":
            continue
        path = describe.location(entry)
        if not path or not os.path.isabs(path):
            continue
        out.append({"id": entry.id, "title": entry.title, "path": path, "source": entry.source})
    return out


def steam_account(app: Any) -> dict:
    """{name, logged_in, logged_in_hint, offline_mode, account_id, last_login}. Blockierend."""
    from .library import steam
    part = _part(app)
    ctx = part.wiring.sources if part is not None else None
    return steam.steam_account(ctx) if ctx is not None else {}


def emudeck_info(app: Any) -> dict:
    """{installed, path, emulation_dir, roms_dir, available, systems: [{id, label, count, …}]}.
    Blockierend."""
    from .library import emulators
    part = _part(app)
    ctx = part.wiring.sources if part is not None else None
    return emulators.emudeck_info(ctx) if ctx is not None else {}


def rescan(app: Any) -> Future:
    """Bibliothek neu einlesen. Aus jedem Thread; das Future wird in der Hauptschleife fertig."""
    part = _part(app)
    if part is None:
        return app.library.rescan()
    return part.call(app.library.rescan)


def fetch_all_art(app: Any, force: bool = False) -> dict:
    """Cover und Hintergründe für alle Spiele im Hintergrund laden (E34).
    -> {started, total, message}. Fortschritt: Ereignis art.progress {done, total, title,
    finished} und sys.state art_job {done, total} | None; am Ende eine Mitteilung."""
    part = _part(app)
    if part is None:
        return {"started": False, "total": 0, "message": "Die Bibliothek ist noch nicht bereit."}
    why = part.fetch_possible()
    if why is not None:
        return {"started": False, "total": 0, "message": why}
    from .library import describe
    entries = [e for e in app.library.entries() if describe.fetchable(e)]
    entries.sort(key=lambda e: -(e.last_played or 0))
    if not entries:
        return {"started": False, "total": 0, "message": "Es gibt keine Spiele, für die Bilder geladen werden könnten."}
    part.worker.start()
    bulk = part.worker.fetch_all([e.id for e in entries], force=bool(force))
    if bulk is None:
        running = part.worker.bulk
        return {"started": False, "total": running.total if running else 0,
                "message": "Die Bilder werden schon geladen."}
    return {"started": True, "total": bulk.total,
            "message": "Bilder für %s werden geladen." % _plural(bulk.total, "Spiel", "Spiele")}
