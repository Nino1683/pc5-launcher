"""Archiv-Installer am Kern (Bauplan 4.10, E16, E17, E27, E28, E30, E31, E114).

Wiring._installer ruft setup(wiring): genau ein Installer, seine RPC-Methoden install.* und der
Weg seiner Ereignisse an die Oberfläche. E114: Für Installationsprogramme (setup.exe, .msi) liefert
_Hub.setup_env Befehlsanfang und Umgebung im Prefix des späteren Eintrags (wie GOG und itch.io) und
_Hub.discard_prefix räumt den Prefix eines nie eingetragenen Spiels weg. extend(wiring) hängt am Ende von Wiring.run() ein, was
die Haken der Verdrahtung braucht: Aufräumen nach dem Bereit-Signal, Ende, Sätze für die
Rückfrage vor dem Ausschalten. Dort wird auch lib.exeCandidates ersetzt.

Für die anderen Teile (App-Objekt als erster Parameter, späte Bindung über "from . import
wiring_install" in der aufrufenden Funktion):

    delete_local(app, entry_id) -> {ok, message, …}   blockierend; Archiv-Spiel in den Papierkorb
    busy(app) -> bool                                   Installation oder endgültiges Löschen läuft
    games_root(app) -> str                              wohin installiert wird (Einstellung oder Vorgabe)

Wer wo läuft:
- Die Rückrufe des Installers (Ereignisse, add_local, remove_local) kommen über seinen
  GLibScheduler in der Hauptschleife an.
- RPC-Methoden mit Plattenzugriff laufen im Arbeits-Thread (wiring._bg) und fragen die
  Bibliothek und die laufenden Apps über wiring.main in der Hauptschleife ab.
- Aufräumen beim Start und endgültiges Löschen haben eigene Threads: Beides kann Minuten
  dauern und soll keinen der wenigen Arbeiter des Kerns belegen.

Die Bibliothek erreicht dieses Modul nur über wiring_library (add_local_record,
get_local_record, remove_local, rescan). Fehlt das Modul oder eine der Funktionen, gilt der
einfache Weg über library/local.py und den LibraryIndex: Ein installiertes Spiel soll nie
daran scheitern, dass ein anderer Teil nicht geladen ist.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, Callable

from . import log, paths
from .rpc import RpcError

_log = log.get("wiring.install")

DEFAULT_RESERVE_GIB = 5
DEFAULT_TRASH_DAYS = 14
ADD_WAIT_S = 30.0               # so lange wartet „fertig“ höchstens auf den Bibliothekseintrag (FSR-Scan, Bilder)
NOT_AVAILABLE = "Der Archiv-Installer ist gerade nicht verfügbar."
NOT_LOCAL = "Nur Spiele, die die Konsole selbst aus einem Archiv installiert hat, lassen sich löschen."
RPC_NAMES = ("install.browse", "install.probe", "install.start", "install.cancel", "install.jobs",
             "install.setExe", "install.delete", "install.trashList", "install.restore", "install.purge")
OPEN_STATES = ("queued", "running", "finishing", "installing")   # hier arbeitet oder wartet der Installer
SETUP_LOG_NAME = "setup-install"   # E114: Ausgabe der Installationsprogramme (paths.app_log, wie gog-install)
# Nur im Prüfmodus beachtet: Zusätze für deps (tools, worker_argv, rom_dirs, setup_env, setup_limits), damit
# Tests einen langsamen Entpacker, eine volle Platte oder ein Attrappen-Installationsprogramm nachstellen können.
TEST_DEPS: dict = {}


# ---------------------------------------------------------------- Einstellungen
def default_games_root() -> str:
    """Ziel der Installationen ohne eigene Einstellung: die Spiele-Platte; im Prüfmodus ein
    Ordner im Käfig (alles andere verweigert der Installer dort ohnehin)."""
    root = paths.test_root()
    return os.path.join(root, "games") if root else paths.games_disk()


def defaults() -> dict:
    # "" = automatisch (so steht es auch in der Tabelle von config.py): default_games_root()
    return {"install.games_root": "", "install.reserve_gib": DEFAULT_RESERVE_GIB,
            "install.trash_days": DEFAULT_TRASH_DAYS}


def _add_defaults(app: Any) -> None:
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(defaults())


class _Config:
    """Einstellungen, wie der Installer sie liest. Ein leerer Spiele-Ordner (so steht „automatisch“
    in manchen Tabellen) wird zur Vorgabe; sonst nähme der Installer seine eigene eingebaute."""

    def __init__(self, config: Any):
        self._config = config

    def get(self, key: str, default: Any = None) -> Any:
        value = self._config.get(key, default)
        if key == "install.games_root" and not (isinstance(value, str) and value.strip()):
            return default_games_root()
        return value


# ---------------------------------------------------------------- Bibliothek (späte Bindung)
def _library_fn(name: str) -> Callable | None:
    """Funktion aus wiring_library oder None (Modul fehlt, lädt nicht oder kennt sie nicht)."""
    try:
        from . import wiring_library
    except ImportError:
        return None
    except Exception:
        _log.exception("wiring_library lässt sich nicht laden")
        return None
    fn = getattr(wiring_library, name, None)
    return fn if callable(fn) else None


def _hub(app: Any) -> Any:
    return getattr(getattr(app, "wiring", None), "install_hub", None)


# ---------------------------------------------------------------- Verbindung Installer <-> Kern
class _Hub:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.config = _Config(wiring.config)
        self.installer: Any = None
        self.started: Future | None = None      # Aufräumen nach dem Start (für Tests und Protokoll)
        self.extended = False
        self._adding: dict[str, Future] = {}    # Eintrags-ID -> laufendes add_local (nur Hauptschleife)
        self._add_failed: set[str] = set()      # Einträge, die die Bibliothek nicht angenommen hat
        self._asked: set[str] = set()           # Aufträge, deren Frage nach der Startdatei schon gemeldet ist
        # Offene Aufträge, wie die Ereignisse sie melden (geschrieben nur in der Hauptschleife).
        # Daraus entstehen sys.state.jobs und die Sätze für busy_checks: Die Hauptschleife fragt
        # den JobManager nie selbst, denn dessen Schloss hält ein Arbeits-Thread auch einmal über
        # einen Plattenzugriff hinweg (die Spiele-Platte hängt an USB und kann schlafen).
        self._jobs: dict[str, dict] = {}
        self._closing = False

    # ------------------------------------------------------------ RPC
    def register_rpc(self) -> None:
        for name, (fn, _blocking) in self.installer.rpc_table().items():
            self.app.rpc.register(name, self._method(name, fn))

    def _method(self, name: str, fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
        """Jede Methode läuft im Arbeits-Thread, auch die, die der Installer „schnell“ nennt: start,
        cancel und setExe fassen kurz die Platte an, und alle warten auf das Schloss des JobManagers."""
        def work(params: dict) -> Any:
            if name == "install.delete" and not str(params.get("id") or "").startswith("local:"):
                # Fremde Spiele (Steam, Lutris, Emulator) löscht die Konsole nie; der Installer
                # kennte sie nur als „nicht in der Bibliothek“
                return {"ok": False, "code": "not-local", "message": NOT_LOCAL}
            result = fn(params)
            if name in ("install.restore", "install.setExe") and isinstance(result, dict) \
                    and result.get("ok") and isinstance(result.get("entry"), dict):
                # erst antworten, wenn die Bibliothek den Eintrag hat: Die Seite fragt gleich danach
                self._wait_added(result["entry"].get("id"))
            return result
        return lambda params: self.wiring._bg(work, params)

    def replace_exe_candidates(self) -> None:
        """lib.exeCandidates {id}: bei Archiv-Spielen die Kandidaten, die wirklich im Spielordner
        liegen; bei allen anderen wie bisher, was der Eintrag mitbringt."""
        def exe_candidates(params: dict) -> dict:
            entry_id = str(params.get("id") or "")
            result = self.installer.exe_candidates(entry_id)
            if result.get("ok"):
                return result
            entry = self.wiring.main.call(self.app.library.get, entry_id)
            if entry is None:
                raise RpcError("Diesen Eintrag gibt es nicht mehr.", "NotFound")
            found = entry.detail.get("exe_candidates") if isinstance(entry.detail, dict) else None
            return {"ok": True, "id": entry_id, "candidates": list(found) if isinstance(found, list) else []}
        self.app.rpc.register("lib.exeCandidates", lambda params: self.wiring._bg(exe_candidates, params))

    # ------------------------------------------------------------ Rückrufe des Installers
    def emit(self, event: str, payload: dict | None = None) -> None:
        """Hauptschleife. „Fertig“ mit Bibliothekseintrag wartet, bis der Eintrag angelegt ist."""
        payload = payload or {}
        if event == "install.done" and payload.get("ok"):
            pending = self._adding.get(str(payload.get("entryId") or ""))
            if pending is not None and not pending.done():
                self._hold_done(payload, pending)
                return
        self._deliver(event, payload)

    def _hold_done(self, payload: dict, pending: Future) -> None:
        sent: list[bool] = []

        def deliver() -> None:
            if not sent:
                sent.append(True)
                timer.cancel()
                self._deliver("install.done", payload)
        # Hängt die Bibliothek, erfährt die Oberfläche trotzdem vom Ende der Installation
        timer = threading.Timer(ADD_WAIT_S, lambda: self.wiring.main.post(deliver))
        timer.daemon = True
        timer.start()
        # reiht sich hinter dem Rückruf aus add_local ein: _add_failed stimmt dann schon
        pending.add_done_callback(lambda fut: self.wiring.main.post(deliver))

    def _deliver(self, event: str, payload: dict) -> None:
        self.app.rpc.emit(event, payload)
        try:
            if event in ("install.progress", "install.done"):
                self._track(event, payload)
                self.push_jobs()
            if event == "install.done":
                self._done(payload)
            elif event == "install.progress" and payload.get("state") == "needs-choice":
                self._ask_exe(payload)
            elif event == "install.purgeDone":
                self._purged(payload)
        except Exception:
            _log.exception("Ereignis %s des Installers", event)

    def _track(self, event: str, payload: dict) -> None:
        job_id = str(payload.get("jobId") or "")
        if not job_id:
            return
        if event == "install.done":
            self._jobs.pop(job_id, None)
        else:
            percent = payload.get("percent")
            self._jobs[job_id] = {"id": job_id, "title": str(payload.get("title") or ""),
                                  "percent": percent if isinstance(percent, int) else 0,
                                  "state": str(payload.get("state") or "")}

    def jobs_view(self) -> list:
        """[{id, title, percent, state}] der offenen Aufträge in der Reihenfolge ihres Erscheinens:
        queued, running, finishing und needs-choice. Aus jedem Thread lesbar."""
        return [dict(job) for job in list(self._jobs.values())]

    def push_jobs(self) -> None:
        self.app.state.update(jobs=self.jobs_view())

    def _notify(self, title: str, body: str, icon: str = "download", **more: Any) -> None:
        if self._closing:
            return
        try:
            self.app.notify.push(title, body, icon=icon, **more)
        except Exception:
            _log.exception("Mitteilung „%s“", title)

    def _done(self, payload: dict) -> None:
        title = str(payload.get("title") or "Das Spiel")
        message = str(payload.get("message") or "")
        state = payload.get("state")
        self._asked.discard(str(payload.get("jobId") or ""))
        _log.info("Installation %s: %s (%s) %s", payload.get("jobId"), state, title, message)
        if payload.get("ok"):
            if payload.get("rescan"):
                # ROM: kein eigener Datensatz, das Spiel kommt über die Emulator-Quelle
                self._rescan()
                self._notify("„%s“ ist installiert" % title, "Es erscheint gleich in der Bibliothek.")
            elif str(payload.get("entryId") or "") in self._add_failed:
                self._notify("Bibliothek nicht aktualisiert",
                             "„%s“ ist installiert, ließ sich aber nicht in die Bibliothek eintragen. "
                             "Beim nächsten Start der Konsole wird es erneut versucht." % title, icon="warn")
            else:
                self._notify("„%s“ ist installiert" % title, "Das Spiel steht jetzt in der Bibliothek.")
        elif state == "cancelled":
            from .installer import jobs as jobs_mod
            if message == jobs_mod.MSG_INTERRUPTED:
                self._notify("Installation unterbrochen",
                             "„%s“ wurde nicht fertig installiert, weil die Konsole beendet wurde. "
                             "Starte die Installation noch einmal." % title, icon="warn")
            else:
                self._notify("Installation abgebrochen", "„%s“: %s" % (title, message))
        else:
            self._notify("Installation fehlgeschlagen", "„%s“: %s" % (title, message), icon="warn",
                         sound="error")

    def _ask_exe(self, payload: dict) -> None:
        job_id = str(payload.get("jobId") or "")
        if job_id in self._asked:
            return
        self._asked.add(job_id)
        self._notify("Startdatei wählen",
                     "„%s“ ist entpackt. Wähle noch die Startdatei, dann steht das Spiel in der Bibliothek."
                     % str(payload.get("title") or "Das Spiel"), icon="archive")

    def _purged(self, payload: dict) -> None:
        message = str(payload.get("message") or "")
        _log.info("Papierkorb %s: %s", payload.get("trashId"), message)
        if payload.get("ok"):
            self._notify("Endgültig gelöscht", message, icon="trash")
        elif payload.get("code") != "cancelled":
            self._notify("Löschen nicht abgeschlossen", message, icon="warn", sound="error")

    def add_local(self, record: dict) -> None:
        """Hauptschleife: Datensatz an die Bibliothek (neu oder ersetzt). Das Anlegen blockiert
        (FSR-Scan des Spielordners) und läuft deshalb im Arbeits-Thread."""
        entry_id = str(record.get("id") or "")
        fut = self.wiring._bg(self._add_record, dict(record))
        self._adding[entry_id] = fut

        def finished(done: Future) -> None:
            if self._adding.get(entry_id) is done:
                del self._adding[entry_id]
            error = done.exception()
            if error is None:
                self._add_failed.discard(entry_id)
            else:
                # Der Auftrag bleibt in der Liste des Installers: Der nächste Start trägt nach
                self._add_failed.add(entry_id)
                _log.error("Bibliothekseintrag %s nicht angelegt: %s: %s", entry_id,
                           type(error).__name__, error)
        fut.add_done_callback(lambda done: self.wiring.main.post(finished, done))

    def _add_record(self, record: dict) -> str | None:
        """Arbeits-Thread. Liefert die Eintrags-ID; None, wenn es keinen eigenen Eintrag gibt
        (dann liest die Bibliothek nur neu ein)."""
        fn = _library_fn("add_local_record")
        if fn is not None:
            return fn(self.app, record)
        return self._plain_add(record)

    def _plain_add(self, record: dict) -> str | None:
        from .library import local as local_mod
        sources = self.wiring.sources
        entry = local_mod.record_to_entry(sources, record) if sources is not None else None
        if entry is not None:
            self.wiring.main.call(self.app.library.add_local, entry)
        self._rescan()                          # Bilder und Marken kommen erst mit dem Einlesen
        return entry.id if entry is not None else None

    def _rescan(self) -> None:
        fn = _library_fn("rescan")

        def run() -> None:
            try:
                if fn is not None:
                    fn(self.app)
                else:
                    self.app.library.rescan()
            except Exception:
                _log.exception("Bibliothek neu einlesen")
        self.wiring.main.post(run)

    def _wait_added(self, entry_id: Any) -> None:
        """Arbeits-Thread: wartet auf ein gerade laufendes add_local für diesen Eintrag. Die Frage
        an die Hauptschleife reiht sich hinter dem add_local-Rückruf des Installers ein."""
        try:
            pending = self.wiring.main.call(self._adding.get, str(entry_id or ""))
            if pending is not None:
                pending.result(timeout=ADD_WAIT_S)
        except Exception:
            pass                                # der Fehler steht schon im Protokoll (add_local)

    def remove_local(self, entry_id: str) -> None:
        """Hauptschleife."""
        try:
            fn = _library_fn("remove_local")
            if fn is not None:
                fn(self.app, entry_id)
            else:
                self.app.library.remove_local(entry_id)
        except Exception:
            _log.exception("Bibliothekseintrag %s entfernen", entry_id)

    def get_local(self, entry_id: str) -> dict | None:
        """Aus jedem Thread: der Datensatz eines Archiv-Spiels, None für alles andere."""
        fn = _library_fn("get_local_record")

        def read() -> dict | None:
            if fn is not None:
                return fn(self.app, entry_id)
            from .library import local as local_mod
            entry = self.app.library.get(entry_id)
            if entry is None or entry.source != "local":
                return None
            return local_mod.entry_to_record(entry)
        try:
            return self.wiring.main.call(read)
        except RuntimeError:
            return None                         # das Home endet gerade

    def shared_folder(self, entry_id: str, folder: str, own_dir: Any = None) -> str | None:
        """Aus jedem Thread (E91): „Titel“ eines anderen Spiels der Bibliothek, dessen Ordner in folder
        liegt, aber nicht im Ordner dieses Spiels (own_dir; ein Lutris-Eintrag auf denselben Ordner
        ist dasselbe Spiel). Auch eine Steam-Bibliothek in folder zählt. None = nur dieses Spiel."""
        from .library import describe
        try:
            places = self.wiring.main.call(lambda: [(e.id, e.title, describe.location(e))
                                                    for e in self.app.library.entries()])
        except RuntimeError:
            return "ein anderes Spiel"          # das Home endet gerade: lieber nichts anfassen
        real = os.path.realpath(folder)
        own = os.path.realpath(own_dir) if isinstance(own_dir, str) and own_dir else real
        if os.path.isdir(os.path.join(real, "steamapps")):
            return "eine Steam-Bibliothek"
        for other_id, title, where in places:
            if other_id == entry_id or not isinstance(where, str) or not os.path.isabs(where):
                continue
            path = os.path.realpath(where)
            if (path == real or path.startswith(real + "/")) and not (path == own or path.startswith(own + "/")):
                return "„%s“" % title
        return None

    # ------------------------------------------------------------ Installationsprogramme (E114)
    def setup_env(self, entry_id: str, title: str, installer: str) -> dict:
        """Arbeits-Thread des JobManagers: Befehlsanfang (umu-run) und Umgebung für ein
        Installationsprogramm im Prefix des späteren Eintrags entry_id, genau wie bei GOG und itch.io
        (wiring_store.installer_launch); so startet das Spiel später im selben Prefix (Registry,
        Abhängigkeiten). Wirft InstallError mit deutschem Satz (Proton nicht bereit, beschädigt, Prefix belegt)."""
        from . import wiring_store
        from .installer import InstallError
        from .store import gog_install
        try:
            umu, env = wiring_store.installer_launch(self.wiring, entry_id, title, installer)
        except gog_install.InstallError as error:
            raise InstallError("no-proton", str(error)) from error
        return {"argv": [umu], "env": env, "log": paths.app_log(SETUP_LOG_NAME)}

    def discard_prefix(self, entry_id: str) -> None:
        """Arbeits-Thread: Prefix eines nie eingetragenen Spiels nach Abbruch oder Fehler in den Papierkorb,
        wie die Store-Warteschlange (wiring_store._FolderQueue._cleanup): nur <prefixe>/local-<id>, ein echter
        Ordner, die Bibliothek kennt den Eintrag nicht, und in ihm läuft kein Wine. Sonst bleibt er liegen."""
        from .installer import trash
        match = trash.ENTRY_ID_RE.match(entry_id) if isinstance(entry_id, str) else None
        ctx = getattr(self.wiring, "launch", None)
        if match is None or ctx is None:
            return
        try:
            if self.wiring.main.call(self.app.library.get, entry_id) is not None:
                return                          # eingetragen: dort liegen Spielstände
        except Exception:
            return                              # nicht zu klären (das Home endet): liegen lassen
        from .launch import prefix as prefix_mod
        from .library.model import Entry
        folder = os.path.join(ctx.prefix_root_dir(), prefix_mod.prefix_name(Entry(id=entry_id, title="", source="local")))
        if os.path.islink(folder) or not os.path.isdir(folder):
            return
        from .launch import proton
        from .launch.spec import LaunchError
        try:
            proton.prefix_free(folder, settle=False)
        except LaunchError:
            _log.info("Prefix %s bleibt liegen: darin läuft noch Wine", folder)
            return
        from . import textedit
        try:
            textedit.default_trash(folder)      # Papierkorb des Systems, im Prüfmodus der im Käfig
        except (OSError, ValueError) as error:
            _log.warning("Prefix %s nicht in den Papierkorb: %s", folder, error)

    def is_running(self, entry_id: str) -> bool:
        """Aus jedem Thread. Ein laufendes oder gerade startendes Spiel wandert nie in den
        Papierkorb; lässt es sich nicht klären, gilt es als laufend."""
        def check() -> bool:
            if self.app.procs.by_entry(entry_id) is not None:
                return True
            # Start in Vorbereitung (Auftrag, Rückfrage, Prefix-Kopie): öffentlicher Zugriff der Shell (E99)
            is_preparing = getattr(getattr(self.app, "shell", None), "is_preparing", None)
            return is_preparing is not None and bool(is_preparing(entry_id))
        try:
            return bool(self.wiring.main.call(check))
        except Exception:
            _log.exception("Läuft %s? Nicht zu klären, also nicht löschen", entry_id)
            return True

    # ------------------------------------------------------------ Start, Ende, Rückfrage
    def start(self) -> Future:
        """Nach dem Bereit-Signal (Hauptschleife): verwaiste Zwischenordner entfernen,
        Unterbrochenes melden, alten Papierkorb leeren, offene Fragen erneut stellen."""
        if self.started is not None:
            return self.started
        fut: Future = Future()
        self.started = fut

        def work() -> None:
            try:
                report = self.installer.startup()
            except BaseException as error:
                _log.exception("Aufräumen des Archiv-Installers nach dem Start")
                fut.set_exception(error)
                return
            fut.set_result(report)
            if not self._closing:
                self.wiring.main.post(self._started, report)
        threading.Thread(target=work, name="couch-install-start", daemon=True).start()
        return fut

    def _started(self, report: dict) -> None:
        jobs = report.get("jobs") or {}
        _log.info("Archiv-Installer: %d unterbrochen, %d wiedergefunden, %d Zwischenordner entfernt, "
                  "%d nachgetragen, %d warten auf die Startdatei",
                  len(jobs.get("interrupted") or []), len(jobs.get("recovered") or []),
                  len(jobs.get("removed") or []), len(report.get("readded") or []),
                  len(report.get("pending") or []))
        for item in jobs.get("refused") or []:
            _log.warning("Zwischenordner bleibt liegen: %s (%s)", item.get("path"), item.get("reason"))
        purged = [r for r in report.get("purged") or [] if r.get("ok")]
        failed = [r for r in report.get("purged") or [] if not r.get("ok") and r.get("code") != "cancelled"]
        for item in failed:
            _log.warning("Papierkorb-Eintrag %s bleibt liegen: %s", item.get("trash_id"), item.get("message"))
        if purged:
            from .installer import trash_days
            names = ", ".join("„%s“" % (r.get("title") or r.get("trash_id")) for r in purged)
            # still: Aufräumen im Hintergrund, aber in der Liste nachlesbar
            self._notify("Papierkorb aufgeräumt", "Nach %d Tagen endgültig gelöscht: %s."
                         % (trash_days(self.config), names), icon="trash", silent=True)
        try:
            self.push_jobs()
        except Exception:
            _log.exception("Auftragsliste an die Oberfläche")

    def closing(self) -> None:
        """Haken für wiring.stop(): nichts mehr melden, endgültiges Löschen anhalten. Laufende
        Installationen bricht wiring.stop() danach selbst ab (installer.shutdown)."""
        self._closing = True
        if self.installer is not None:
            self.installer.stop_purge(1.0)

    def busy_install(self) -> str | None:
        jobs = [j for j in self.jobs_view() if j["state"] in OPEN_STATES]
        if not jobs:
            return None
        active = next((j for j in jobs if j["state"] != "queued"), None)
        waiting = len(jobs) - (1 if active else 0)
        if active is None:
            if waiting == 1:
                return "Eine Installation wartet noch (%s)." % jobs[0]["title"]
            return "%d Installationen warten noch." % waiting
        if active["state"] == "finishing":
            text = "Eine Installation wird gerade abgeschlossen (%s)" % active["title"]
        elif active["state"] == "installing":
            text = "Ein Installationsprogramm läuft noch (%s)" % active["title"]
        else:
            text = "Eine Installation läuft noch (%s, %d %%)" % (active["title"], int(active["percent"] or 0))
        if waiting == 1:
            text += ", eine weitere wartet"
        elif waiting > 1:
            text += ", %d weitere warten" % waiting
        return text + "."

    def busy_purge(self) -> str | None:
        items = self.installer.purging()
        if not items:
            return None
        first = items[0]
        text = "Der Papierkorb wird noch geleert (%s" % first["title"]
        if first.get("percent") is not None:
            text += ", %d %%" % int(first["percent"])
        return text + ")."


def install_roots() -> list:
    """paths.install_roots für den Installer; ~ heißt dort wie im Explorer (sonst stand in der
    Dateiauswahl nur der Benutzername)."""
    home = os.path.realpath(paths.home())
    return [{"name": "Persönlicher Ordner", "path": p, "kind": "folder"} if p == home else p
            for p in paths.install_roots()]


def _rom_dirs(wiring: Any) -> dict:
    """ROM-Ordner für Emulator-Spiele aus der SRM-Konfiguration des Home, das auch die
    Bibliothek liest: im Prüfmodus der Attrappen-Baum, nie die echte Konfiguration des Nutzers."""
    from .installer import probe as probe_mod
    sources = getattr(wiring, "sources", None)
    home = getattr(sources, "home", None)
    if not home and paths.test_mode():
        return {}
    return probe_mod.default_rom_dirs(home or None)


# ---------------------------------------------------------------- Einstieg
def setup(wiring: Any) -> Any:
    """Von Wiring._installer gerufen: den einen Installer anlegen und install.* anmelden.
    Schnell, fasst die Platte nicht an (im Prüfmodus entsteht der Spiele-Ordner im Käfig)."""
    from . import installer as installer_mod
    from .installer.service import Installer
    app = wiring.app
    _add_defaults(app)
    test_root = paths.test_root()
    if test_root:
        try:
            os.makedirs(os.path.join(test_root, "games"), mode=0o700, exist_ok=True)
        except OSError as error:
            _log.warning("Prüf-Spieleordner lässt sich nicht anlegen: %s", error)
    hub = _Hub(wiring)
    deps = SimpleNamespace(
        config=hub.config,
        paths=SimpleNamespace(state_dir=paths.state_dir, data_dir=paths.data_dir, cache_dir=paths.cache_dir),
        emit=hub.emit, add_local=hub.add_local, remove_local=hub.remove_local, get_local=hub.get_local,
        scheduler=installer_mod.GLibScheduler(), is_running=hub.is_running, shared_folder=hub.shared_folder,
        # Orte des Explorers (E99), damit dessen Archive die Prüfung bestehen; im Prüfmodus nie die Ordner des Nutzers
        browse_roots=install_roots,
        rom_dirs=lambda: _rom_dirs(wiring),
        # E114: Installationsprogramme still über Proton im Prefix des späteren Eintrags
        setup_env=hub.setup_env, discard_prefix=hub.discard_prefix)
    if paths.test_mode():
        slow = os.environ.get("COUCH_TEST_INSTALL_7Z") or ""
        if slow and os.path.basename(slow).startswith("fake") and os.path.isfile(slow):
            # Prüfläufe der ganzen Sitzung: ein langsamer Entpacker (tests/features/installer/
            # fake7z.py), damit „Installation läuft“ lange genug steht, um es zu prüfen
            import sys
            deps.tools = {"7z": [sys.executable, "-I", "-B", slow]}
        for key, value in TEST_DEPS.items():
            setattr(deps, key, value)
    hub.installer = Installer(deps)
    wiring.installer = hub.installer
    wiring.install_hub = hub
    app.jobs = hub.installer.manager
    hub.register_rpc()
    return hub


def _register_stubs(app: Any) -> None:
    """Ohne Installer antworten install.* mit einem deutschen Satz statt „unbekannte Methode“."""
    def unavailable(params: dict) -> dict:
        raise RpcError(NOT_AVAILABLE, "NoInstaller")
    for name in RPC_NAMES:
        app.rpc.register(name, unavailable)


def extend(wiring: Any) -> None:
    """Am Ende von Wiring.run(): Haken eintragen und lib.exeCandidates ersetzen."""
    hub = getattr(wiring, "install_hub", None)
    if hub is None or hub.installer is None:
        # Wiring._installer ist gescheitert: ein zweiter Versuch; klappt auch der nicht, bleibt
        # die Konsole ohne Archiv-Installer (steht dann in wiring.failed)
        try:
            hub = setup(wiring)
        except Exception:
            _register_stubs(wiring.app)
            raise
    if hub.extended:
        return
    hub.extended = True
    _add_defaults(wiring.app)
    hub.replace_exe_candidates()
    wiring.after_ready.append(hub.start)
    wiring.shutdown_hooks.append(hub.closing)
    wiring.busy_checks.append(hub.busy_install)
    wiring.busy_checks.append(hub.busy_purge)
    _downloads(wiring)


DOWNLOAD_RPC = ("downloads.pause", "downloads.resume", "downloads.cancel", "downloads.remove")


def _downloads(wiring: Any) -> None:
    """Downloads der Konsole (E60): Das Home lädt, was der Browser übergibt, und führt
    sys.state.downloads. Braucht den Planer des Einstellungsteils (Rückrufe im Arbeits-Thread);
    ohne ihn antworten die Methoden mit einem Satz und es wird nichts geladen."""
    if getattr(wiring, "fetcher", None) is not None:
        return
    sched = getattr(wiring, "_scheduler", None)
    rpc = wiring.app.rpc
    if sched is None:
        for name in DOWNLOAD_RPC:
            rpc.register(name, lambda params: {"ok": False, "message": "Downloads sind gerade nicht verfügbar."})
        return
    from .fetcher import Fetcher
    fetcher = Fetcher(paths.downloads_file(), paths.download_requests_dir(), paths.download_dir(),
                      later=sched.later, post=wiring.main.post, state=wiring.app.state,
                      notify=getattr(wiring.app, "notify", None),
                      local_path=paths.browser_downloads_file())     # was WebKit selbst lädt (E99)
    wiring.fetcher = fetcher
    for name in DOWNLOAD_RPC:
        fn = getattr(fetcher, name.split(".", 1)[1])
        rpc.register(name, lambda params, fn=fn: fn(str((params or {}).get("id") or "")))
    wiring.after_ready.append(fetcher.start)
    wiring.shutdown_hooks.append(fetcher.stop)


def push_jobs(wiring: Any) -> None:
    """Offene Aufträge nach sys.state.jobs ([{id, title, percent, state}]). Hauptschleife; liest
    nur, was die Ereignisse des Installers gemeldet haben. Aufträge einer früheren Sitzung, die
    noch auf die Startdatei warten, kommen deshalb erst mit dem Aufräumen nach dem Bereit-Signal."""
    hub = getattr(wiring, "install_hub", None)
    if hub is not None and hub.installer is not None:
        hub.push_jobs()


# ---------------------------------------------------------------- für andere Teile
def games_root(app: Any) -> str:
    """Der Ordner, in den installiert wird (Einstellung install.games_root, leer = Spiele-Platte)."""
    return str(_Config(app.config).get("install.games_root"))


def delete_local(app: Any, entry_id: str) -> dict:
    """Ein eigenes Archiv-Spiel in den Papierkorb verschieben. Blockierend (Arbeits-Thread).

    → {"ok": True, "message", "trash_id"?, "forgotten"?} oder {"ok": False, "code", "message"}.
    Ein laufendes Spiel wird nie verschoben. Ist der Spielordner schon weg (von Hand gelöscht,
    Platte eingehängt), verschwindet nur der Bibliothekseintrag; auf der Platte geschieht nichts.
    ROM-Installationen und fremde Spiele haben keinen Marker und werden abgelehnt."""
    hub = _hub(app)
    if hub is None or hub.installer is None:
        return {"ok": False, "code": "no-installer", "message": NOT_AVAILABLE}
    if not isinstance(entry_id, str) or not entry_id.startswith("local:"):
        return {"ok": False, "code": "not-local", "message": NOT_LOCAL}
    result = hub.installer.delete(entry_id)
    if not result.get("ok") and result.get("can_forget"):
        result = hub.installer.delete(entry_id, forget=True)
    return result


def busy(app: Any) -> bool:
    """Wartet oder läuft eine Installation, oder wird gerade endgültig gelöscht? Schnell, aus
    jedem Thread (eine offene Frage nach der Startdatei zählt nicht)."""
    hub = _hub(app)
    if hub is None or hub.installer is None:
        return False
    try:
        return any(j["state"] in OPEN_STATES for j in hub.jobs_view()) or bool(hub.installer.purging())
    except Exception:
        _log.exception("Zustand des Archiv-Installers")
        return False
