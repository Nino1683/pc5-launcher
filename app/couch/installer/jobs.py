"""JobManager: Installationsaufträge der Reihe nach ausführen.

  * Ein Auftrag zur Zeit, weitere warten.
  * Jeder Auftrag läuft als eigener Unterprozess (extract.py) in eigener Prozessgruppe. Abbruch
    schickt SIGTERM nur an diese Gruppe; der Arbeiter räumt dann selbst auf. Antwortet er 10 s
    nicht, folgt SIGKILL an dieselbe Gruppe und der JobManager räumt den Zwischenordner auf.
  * Der Arbeiter schont ein laufendes Spiel (E30): Platten-Priorität "idle" (ionice, wenn vorhanden),
    nice 10, 7z mit höchstens acht Threads.
  * Ereignisse gehen über den eingesteckten Rückruf emit(event, payload):
        "install.progress" {jobId, percent, file, state, title, elapsed}  höchstens 4 je Sekunde
        "install.done"     {jobId, ok, entry?, entryId?, message, state}
  * Zustand der Aufträge steht in <state_dir>/install-jobs.json (ohne Kennwörter).
  * startup(): verwaiste Zwischenordner entfernen (nur über trash.remove_staging), unterbrochene
    Aufträge als "abgebrochen" melden.

Zustände eines Auftrags: "queued" → "running" → "finishing" → "done" | "needs-choice" | "failed" |
"cancelled". "needs-choice": Das Spiel liegt am Zielort, aber die Startdatei ist nicht eindeutig;
erst set_exe() legt den Bibliothekseintrag an.

Der JobManager selbst hängt nicht an der GLib-Schleife. Hintergrundarbeit und Rückrufe laufen über
den eingesteckten Planer (scheduler.run_in_thread, scheduler.call_soon).
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable

from .. import jsontext
from . import InstallError, ThreadScheduler, games_root as cfg_games_root, reserve_bytes as cfg_reserve_bytes
from . import extract, probe, trash

JOBS_FILE = "install-jobs.json"
LOG_FILE = "installer.log"
PROGRESS_MIN_INTERVAL = 0.25     # höchstens 4 Fortschritts-Ereignisse pro Sekunde
KILL_AFTER = 10.0                # Sekunden zwischen SIGTERM und SIGKILL an die Gruppe des Arbeiters
KEEP_FINISHED = 20               # so viele abgeschlossene Aufträge bleiben in der Liste
ACTIVE = ("queued", "running", "finishing")
MSG_INTERRUPTED = "Die Installation wurde unterbrochen, weil die Konsole beendet wurde."


def io_idle_prefix() -> list:
    """Befehlsanfang, der einem Prozess die Platten-Priorität "idle" gibt (E30: die Platte gehört
    zuerst dem laufenden Spiel). Leer, wenn es ionice auf diesem System nicht gibt.

    -t: Scheitert das Setzen (Dateisystem oder Kern ohne Unterstützung), läuft der Befehl trotzdem."""
    path = shutil.which("ionice")
    return [path, "-t", "-c", "3"] if path else []


def default_worker_argv() -> list:
    """Befehl des Arbeiters: isoliertes Python (-I), ohne Bytecode neben die Quellen zu schreiben (-B),
    mit Platten-Priorität "idle". nice 10 setzt der Arbeiter selbst (extract.main); beides vererbt
    sich an bubblewrap und 7z/unrar. ionice ersetzt sich durch den Befehl: Die PID bleibt die des Arbeiters."""
    return io_idle_prefix() + [sys.executable, "-I", "-B",
                               os.path.join(os.path.dirname(os.path.realpath(__file__)), "extract.py")]


def make_executable(install_dir: str, rel: str) -> None:
    """Setzt das Ausführungsrecht auf die Startdatei eines Linux-Spiels (zip verliert es oft)."""
    clean = extract.verify_start_file(install_dir, rel)
    if not clean:
        return
    fd = os.open(os.path.join(os.path.realpath(install_dir), clean), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o100 | ((st.st_mode & 0o044) >> 2))
    finally:
        os.close(fd)


class JobManager:
    def __init__(self, *, config, state_dir: str, emit: Callable[[str, dict], None],
                 add_local: Callable[[dict], None], scheduler=None, rom_dirs: Callable[[], dict] | None = None,
                 tools: dict | None = None, worker_argv: list | None = None, version: str = "",
                 clock: Callable[[], float] = time.time):
        """Legt nur Felder an und fasst die Platte nicht an (schnell).

        config: Einstellungen (get). state_dir: Ordner für install-jobs.json und installer.log.
        emit(event, payload), add_local(entry_dict): Rückrufe, laufen über scheduler.call_soon.
        scheduler: Objekt mit call_soon(fn, *args) und run_in_thread(fn, *args); Vorgabe ThreadScheduler.
        rom_dirs: liefert {"switch": ordner, "wiiu": ordner}; Vorgabe probe.default_rom_dirs.
        tools, worker_argv: nur für Tests (Werkzeug-Attrappen, eigener Arbeiter-Befehl)."""
        self._config = config
        self._state_dir = state_dir
        self._emit_cb = emit
        self._add_local = add_local
        self._scheduler = scheduler or ThreadScheduler()
        self._rom_dirs = rom_dirs or probe.default_rom_dirs
        self._tools = tools
        self._worker_argv = list(worker_argv) if worker_argv else default_worker_argv()
        self._version = version
        self._clock = clock
        self._lock = threading.RLock()            # schützt alle folgenden Felder
        self._jobs: dict[str, dict] = {}
        self._queue: list[str] = []
        self._active: str | None = None
        self._procs: dict[str, subprocess.Popen] = {}
        self._passwords: dict[str, str] = {}      # nur im Speicher, nie auf der Platte
        self._emitted: dict[str, tuple] = {}      # je Auftrag: (zeitpunkt, zuletzt gemeldeter Stand)
        self._loaded = False                      # install-jobs.json schon eingelesen?
        self._startup_lock = threading.Lock()
        self._startup_result: dict | None = None

    # ------------------------------------------------------------ öffentlich

    def start(self, plan: dict, choices: dict | None = None) -> str:
        """Reiht einen Auftrag ein. Schnell (ein paar lstat, eine kleine Datei schreiben). Rückgabe: Auftrags-ID.

        plan: der Plan aus probe() (gebraucht werden archive, kind, system, rom_layout, rom_items, title, candidates).
        choices: {"title"?, "folder"?, "exe"?, "password"?}. Der Arbeiter prüft das Archiv selbst erneut.
        Wirft InstallError (code z. B. "target-exists" mit extra["suggestion"], "no-target", "unsupported")."""
        choices = choices or {}
        if not isinstance(plan, dict) or not isinstance(plan.get("archive"), str) or not isinstance(choices, dict):
            raise InstallError("bad-plan", "Der Installationsplan ist ungültig.")
        archive = plan["archive"]
        if not os.path.isabs(archive) or not os.path.isfile(archive):
            raise InstallError("not-found", "Das Archiv wurde nicht gefunden.")
        games_root = cfg_games_root(self._config)
        problem = probe.check_games_root(games_root)
        if problem:
            raise InstallError("no-target", problem)
        title = str(choices.get("title") or plan.get("title") or probe.clean_name(os.path.basename(archive))).strip() or "Spiel"
        content = {"kind": plan.get("kind"), "system": plan.get("system"), "rom_layout": plan.get("rom_layout"),
                   "rom_items": [i for i in (plan.get("rom_items") or []) if isinstance(i, str)], "title": title}
        rom_dirs = dict(self._rom_dirs() or {})
        folder = choices.get("folder") if isinstance(choices.get("folder"), str) and choices.get("folder") else title
        target = probe.resolve_target(content, folder, games_root, rom_dirs)
        if target["problem"]:
            raise InstallError(*target["problem"])
        if target["mode"] is None:
            raise InstallError("unsupported", str(plan.get("message") or "Dieses Archiv kann nicht installiert werden."))
        if target["exists"]:
            if target["mode"] == "rom-files":
                raise InstallError("target-exists", "Im ROM-Ordner liegt schon eine Datei mit diesem Namen: %s." % ", ".join(target["existing"]))
            raise InstallError("target-exists", "Auf der Spiele-Platte gibt es schon einen Ordner „%s“." % target["name"],
                               suggestion=target["suggestion"])
        candidates = [{"archive_path": c.get("archive_path"), "path": c.get("path"), "score": c.get("score", 0)}
                      for c in (plan.get("candidates") or []) if isinstance(c, dict)][:12]
        password = choices.get("password") if isinstance(choices.get("password"), str) and choices.get("password") else None
        with self._lock:
            self._ensure_loaded()
            for other in self._live_jobs():
                if other["archive"] == archive:
                    raise InstallError("already-queued", "Dieses Archiv wird schon installiert.")
                if target["mode"] != "rom-files" and other.get("final_dir") == target["dir"]:
                    raise InstallError("already-queued", "Für diesen Ordner läuft schon eine Installation.")
            job_id = "job-" + uuid.uuid4().hex[:12]
            job = {"id": job_id, "state": "queued", "percent": 0, "file": None, "message": "", "title": title,
                   "archive": archive, "kind": content["kind"], "platform": probe.PLATFORMS.get(content["kind"]),
                   "system": content["system"], "mode": target["mode"], "games_root": games_root,
                   "final_dir": target["dir"], "folder": target["name"], "token": uuid.uuid4().hex,
                   "entry_uuid": str(uuid.uuid4()), "wanted_exe": choices.get("exe") if isinstance(choices.get("exe"), str) else None,
                   "plan_candidates": candidates, "created": self._clock(), "started": None, "finished": None,
                   "staging": None, "staging_token": None}
            self._jobs[job_id] = job
            self._queue.append(job_id)
            if password:
                self._passwords[job_id] = password
            self._save()
        self._progress(job, force=True)
        self._pump()
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Bricht einen Auftrag ab. Kehrt sofort zurück; das Ergebnis kommt als "install.done".

        Wartend: wird gestrichen. Laufend: SIGTERM an die Prozessgruppe des Arbeiters.
        "needs-choice": Der fertig entpackte Ordner wandert in den Papierkorb (ein rename, umkehrbar).
        Rückgabe: False, wenn es nichts abzubrechen gab."""
        with self._lock:
            self._ensure_loaded()
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["state"] in ACTIVE and job not in self._live_jobs():
                self._close(job, "cancelled", MSG_INTERRUPTED)      # Rest aus einer früheren Sitzung
                return True
            if job["state"] == "queued":
                if job_id in self._queue:
                    self._queue.remove(job_id)
                self._passwords.pop(job_id, None)
                self._close(job, "cancelled", extract.MSG_CANCELLED)
                return True
            if job["state"] in ("running", "finishing"):
                job["cancel_requested"] = time.monotonic()
                self._signal_worker(job_id, signal.SIGTERM)
                return True
            if job["state"] != "needs-choice":
                return False
            result = trash.delete_install(self._entry_stub(job), cfg_games_root(self._config))
            if not result["ok"]:
                raise InstallError(result["code"], result["message"])
            job["trash_id"] = result["trash_id"]
            self._close(job, "cancelled", "Die Installation wurde verworfen. Der Ordner liegt im Papierkorb.")
            return True

    def list(self) -> list:
        """Alle bekannten Aufträge, älteste zuerst. Schnell (nur Speicher).

        Je Auftrag: {id, jobId, title, percent, state, file, message, archive, kind, platform, system,
        created, finished, installDir, entryId, candidates (nur bei "needs-choice")}."""
        with self._lock:
            self._ensure_loaded()
            return [self._view(job) for job in sorted(self._jobs.values(), key=lambda j: j["created"])]

    def open_jobs(self) -> list:
        """Aufträge, die in diesem Prozess warten oder laufen, und alle, die noch auf die Wahl der
        Startdatei warten (auch aus einer früheren Sitzung). Schnell. Ansicht wie list().

        Ein Auftrag, der beim letzten Ende der Konsole noch lief, gehört nicht dazu: Den schließt
        startup() als unterbrochen ab; bis dahin soll ihn niemand als laufend anzeigen."""
        with self._lock:
            self._ensure_loaded()
            live = {job["id"] for job in self._live_jobs()}
            return [self._view(job) for job in sorted(self._jobs.values(), key=lambda j: j["created"])
                    if job["id"] in live or job["state"] == "needs-choice"]

    def busy(self) -> bool:
        """Wartet oder läuft gerade ein Auftrag? (Für die Rückfrage vor dem Ausschalten.)"""
        with self._lock:
            return bool(self._live_jobs())

    def set_exe(self, job_id: str, path: str) -> dict:
        """Wählt die Startdatei eines Auftrags im Zustand "needs-choice" und schließt ihn ab.

        path: relativ zum Spielordner. Kurz blockierend (liest den Marker). Rückgabe: der Bibliothekseintrag.
        Danach kommt "install.done" mit ok=True."""
        with self._lock:
            self._ensure_loaded()
            job = self._jobs.get(job_id)
            if job is None or job["state"] != "needs-choice":
                raise InstallError("no-choice", "Für diesen Auftrag wird keine Startdatei erwartet.")
            exe = extract.verify_start_file(job["install_dir"], path)
            if exe is None:
                raise InstallError("bad-exe", "Diese Datei liegt nicht im Spielordner.")
            try:
                marker = trash.read_marker(job["install_dir"])
            except trash.Refuse as refuse:
                raise InstallError(refuse.code, refuse.message)
            if marker["token"] != job["token"]:
                raise InstallError("marker-mismatch", "Der Spielordner gehört nicht mehr zu diesem Auftrag.")
            return self._complete(job, exe)

    def startup(self) -> dict:
        """Einmal beim Start: Auftragsliste laden, Unterbrochenes melden, verwaiste Zwischenordner entfernen.

        Blockierend (Platte; das Entfernen kann bei großen Resten dauern): in einem Thread aufrufen.
        Läuft höchstens einmal; start() holt es vor dem ersten Auftrag nach.
        Rückgabe: {"interrupted": [ids], "recovered": [ids], "removed": [pfade], "refused": [{path, reason}], "skipped": [pfade]}."""
        with self._startup_lock:
            if self._startup_result is not None:
                return self._startup_result
            result: dict[str, list] = {"interrupted": [], "recovered": [], "removed": [], "refused": [], "skipped": []}
            self._rotate_log()
            with self._lock:
                self._ensure_loaded()
                live = self._live_jobs()
                unfinished = [job for job in self._jobs.values() if job["state"] in ACTIVE and job not in live]
            for job in unfinished:
                if self._recover(job):
                    result["recovered"].append(job["id"])
                else:
                    with self._lock:
                        self._close(job, "cancelled", MSG_INTERRUPTED)
                    result["interrupted"].append(job["id"])
            games_root = cfg_games_root(self._config)
            if probe.check_games_root(games_root) is None:      # im Prüfmodus nie auf der echten Platte
                for item in trash.list_staging(games_root):
                    if self._staging_of_active(item["path"]) or extract.staging_in_use(item["path"]):
                        result["skipped"].append(item["path"])  # dort arbeitet noch ein Entpacker
                        continue
                    try:
                        trash.remove_staging(item["path"], games_root, item["token"])
                        result["removed"].append(item["path"])
                    except (trash.Refuse, OSError, RecursionError) as exc:
                        result["refused"].append({"path": item["path"], "reason": str(exc)})
            with self._lock:
                self._save()
            self._log("start: %s" % jsontext.dumps(result))
            self._startup_result = result
            return result

    def reannounce(self) -> list:
        """Aufträge, die noch auf die Wahl der Startdatei warten ("needs-choice", auch aus einer früheren
        Sitzung), erneut als "install.progress" melden, damit die Oberfläche sie wieder anbietet.

        Schnell (nur Speicher). Rückgabe: [{id, title}] der gemeldeten Aufträge."""
        with self._lock:
            self._ensure_loaded()
            waiting = [job for job in sorted(self._jobs.values(), key=lambda j: j["created"])
                       if job["state"] == "needs-choice"]
            for job in waiting:
                self._emitted.pop(job["id"], None)      # sonst verschluckt die Drosselung den gleichen Stand
        for job in waiting:
            self._progress(job, force=True)
        return [{"id": job["id"], "title": job["title"]} for job in waiting]

    def finished_records(self) -> list:
        """Bibliotheks-Datensätze aller fertig installierten PC-Spiele, die die Auftragsliste noch kennt
        (höchstens KEEP_FINISHED), als [(Auftrags-ID, Datensatz)]. Schnell (nur Speicher).

        Damit findet Installer.startup() Spiele wieder, deren Datensatz die Bibliothek nie erreicht hat
        (add_local gescheitert oder die Konsole endete dazwischen)."""
        with self._lock:
            self._ensure_loaded()
            return [(job["id"], self._record(job, job["exe"], job.get("finished"))) for job in self._jobs.values()
                    if job["state"] == "done" and job.get("mode") == "game" and isinstance(job.get("exe"), str)
                    and job["exe"] and isinstance(job.get("install_dir"), str) and job.get("token")]

    def shutdown(self, timeout: float = 20.0) -> None:
        """Vor dem Ende des Home-Prozesses: alles abbrechen und warten, bis der Arbeiter aufgeräumt hat. Blockierend."""
        with self._lock:
            ids = [job["id"] for job in self._live_jobs()]
        for job_id in ids:
            try:
                self.cancel(job_id)
            except InstallError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._active is None:
                    return
            time.sleep(0.05)

    # ------------------------------------------------------------ Warteschlange und Arbeiter

    def _ensure_loaded(self) -> None:
        """Liest install-jobs.json einmal ein, bevor irgendetwas gespeichert wird. Unter self._lock aufrufen."""
        if not self._loaded:
            self._loaded = True
            self._load()

    def _live_jobs(self) -> list:
        """Aufträge, die in diesem Prozess warten oder laufen (nicht: Reste aus einer früheren Sitzung)."""
        ids = list(self._queue) + ([self._active] if self._active else [])
        return [self._jobs[i] for i in ids if i in self._jobs and self._jobs[i]["state"] in ACTIVE]

    def _pump(self) -> None:
        with self._lock:
            if self._active is not None or not self._queue:
                return
            job_id = self._queue.pop(0)
            self._active = job_id
        self._scheduler.run_in_thread(self._run_job, job_id)

    def _run_job(self, job_id: str) -> None:
        """Läuft im Hintergrund-Thread: ein Auftrag von Anfang bis Ende."""
        job = self._jobs[job_id]
        try:
            self.startup()
            self._work(job)
        except Exception as exc:                 # ein Fehler hier darf die Warteschlange nicht anhalten
            self._log("job %s: interner Fehler %r" % (job_id, exc))
            with self._lock:
                if job["state"] in ACTIVE:
                    self._close(job, "failed", "Die Installation ist an einem internen Fehler gescheitert.")
        finally:
            with self._lock:
                self._active = None
                self._passwords.pop(job_id, None)
            self._pump()

    def _work(self, job: dict) -> None:
        job_id = job["id"]
        with self._lock:
            if job["state"] != "queued":
                return                           # inzwischen abgebrochen
            job.update(state="running", started=self._clock())
            self._save()
            spec = {"job_id": job_id, "archive": job["archive"], "password": self._passwords.get(job_id),
                    "games_root": job["games_root"], "reserve_bytes": cfg_reserve_bytes(self._config),
                    "folder": job["folder"], "title": job["title"], "final_dir": job["final_dir"],
                    "token": job["token"], "entry_id": "local:" + job["entry_uuid"],
                    "rom_dirs": dict(self._rom_dirs() or {}), "tools": self._tools, "version": self._version,
                    "parent_pid": os.getpid()}
        self._progress(job, force=True)
        os.makedirs(self._state_dir, exist_ok=True)
        with open(os.path.join(self._state_dir, LOG_FILE), "ab") as log:
            # Eigene Sitzung = eigene Prozessgruppe: Abbruch trifft nur den Arbeiter und seine Kinder
            proc = subprocess.Popen(self._worker_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                    start_new_session=True, cwd="/")
        with self._lock:
            self._procs[job_id] = proc
            job["worker_pid"] = proc.pid
            if job.get("cancel_requested"):      # Abbruch kam, bevor der Arbeiter lief
                self._signal_worker(job_id, signal.SIGTERM)
        try:
            proc.stdin.write(json.dumps(spec).encode("utf-8"))    # über stdin: das Kennwort steht auf keiner Befehlszeile
            proc.stdin.close()
        except OSError:
            pass
        del spec
        final = self._read_events(job, proc)
        with self._lock:
            proc.wait()
            self._procs.pop(job_id, None)
        proc.stdout.close()

        if final is None:
            # Arbeiter ohne Abschlussmeldung beendet (abgestürzt oder hart beendet): selbst aufräumen
            self._cleanup_staging(job)
            with self._lock:
                if job.get("cancel_requested"):
                    self._close(job, "cancelled", extract.MSG_CANCELLED)
                else:
                    self._close(job, "failed", "Die Installation wurde unerwartet beendet.")
        elif final["event"] == "done":
            self._on_done(job, final)
        elif final["event"] == "cancelled":
            with self._lock:
                self._close(job, "cancelled", str(final.get("message") or extract.MSG_CANCELLED))
        else:
            self._log("job %s: fehlgeschlagen %s %s" % (job_id, final.get("code"), final.get("detail")))
            with self._lock:
                job["code"] = final.get("code")
                self._close(job, "failed", str(final.get("message") or "Die Installation ist fehlgeschlagen."))

    def _read_events(self, job: dict, proc: subprocess.Popen) -> dict | None:
        """Liest die JSON-Zeilen des Arbeiters bis zu seinem Ende. Rückgabe: Abschlussmeldung oder None."""
        final = None
        buf = b""
        fd = proc.stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        try:
            while True:
                if selector.select(PROGRESS_MIN_INTERVAL):
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and self._on_event(job, event):
                            final = event
                self._progress(job)
                with self._lock:
                    asked = job.get("cancel_requested")
                    if asked and not job.get("killed") and time.monotonic() - asked > KILL_AFTER:
                        job["killed"] = True     # der Arbeiter reagiert nicht: die ganze Gruppe hart beenden
                        self._signal_worker(job["id"], signal.SIGKILL)
        finally:
            selector.close()
        return final

    def _on_event(self, job: dict, event: dict) -> bool:
        """Verarbeitet ein Ereignis des Arbeiters. True bei einer Abschlussmeldung."""
        kind = event.get("event")
        if kind in ("done", "cancelled", "failed"):
            return True
        with self._lock:
            if kind == "progress":
                percent = event.get("percent")
                if isinstance(percent, int) and not isinstance(percent, bool):
                    job["percent"] = max(0, min(99, percent))
                job["file"] = event.get("file") if isinstance(event.get("file"), str) else None
            elif kind == "staging":
                job.update(staging=event.get("path"), staging_token=event.get("token"))
                self._save()
            elif kind == "state" and event.get("state") == "finishing":
                job.update(state="finishing", percent=99, file=None)
            elif kind == "cleanup-refused":
                self._log("job %s: Zwischenordner bleibt liegen: %s" % (job["id"], event.get("reason")))
            else:
                return False
        if kind == "state":
            self._progress(job, force=True)
        return False

    def _signal_worker(self, job_id: str, sig: int) -> None:
        """Signal an die Prozessgruppe des Arbeiters. Nur unter self._lock aufrufen.

        Die Gruppe gehört uns: Der Arbeiter wurde mit eigener Sitzung gestartet (Gruppe = seine PID)
        und wird nur unter demselben Schloss eingesammelt, die PID kann also nicht neu vergeben sein."""
        proc = self._procs.get(job_id)
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def _cleanup_staging(self, job: dict) -> None:
        """Zwischenordner eines hart beendeten Arbeiters entfernen (nur über die geprüfte Funktion)."""
        staging, token = job.get("staging"), job.get("staging_token")
        if not staging or not token:
            return
        try:
            trash.remove_staging(staging, job["games_root"], token)
        except (trash.Refuse, OSError, RecursionError) as exc:
            self._log("job %s: Zwischenordner bleibt liegen: %s" % (job["id"], exc))

    def _staging_of_active(self, path: str) -> bool:
        with self._lock:
            return any(job.get("staging") == path and job["state"] in ACTIVE for job in self._jobs.values())

    # ------------------------------------------------------------ Abschluss

    def _on_done(self, job: dict, final: dict) -> None:
        if final.get("mode") != "game":
            # ROM: kein eigener Bibliothekseintrag; die Emulator-Quelle findet das Spiel beim nächsten Einlesen
            with self._lock:
                job.update(install_dir=final.get("path"), files=final.get("files") or [])
                self._close(job, "done", "„%s“ wurde installiert." % job["title"], ok=True,
                            extra={"kind": "rom", "system": job["system"], "path": final.get("path"),
                                   "files": final.get("files") or [], "rescan": True})
            return
        candidates = [c for c in (final.get("candidates") or []) if isinstance(c, dict) and isinstance(c.get("path"), str)]
        with self._lock:
            job.update(install_dir=final.get("path"), candidates=candidates)
            wanted = job.get("wanted_exe")
            exe = extract.verify_start_file(job["install_dir"], wanted) if wanted else None
            if exe is None and candidates and not final.get("needs_choice"):
                exe = candidates[0]["path"]
            if exe is None:
                job.update(state="needs-choice", percent=100, file=None, finished=self._clock(),
                           message="Bitte wähle die Startdatei für „%s“." % job["title"])
                self._save()
            else:
                self._complete(job, exe)
                return
        self._progress(job, force=True)

    def _record(self, job: dict, exe: str, created: float | None = None) -> dict:
        """Der Datensatz eines installierten Spiels, wie ihn add_local bekommt (library/local.py)."""
        candidates = job.get("candidates") or []
        return {"id": "local:" + job["entry_uuid"], "title": job["title"], "platform": job["platform"],
                "install_dir": job["install_dir"], "exe": exe,
                "exe_candidates": [{"path": c["path"], "score": c.get("score", 0)} for c in candidates
                                   if isinstance(c, dict) and isinstance(c.get("path"), str)],
                "args": "", "created": created or self._clock(), "archive": job["archive"], "marker": job["token"]}

    def _complete(self, job: dict, exe: str) -> dict:
        """Bibliothekseintrag bauen, abgeben und den Auftrag als erledigt melden. Unter self._lock aufrufen."""
        candidates = job.get("candidates") or []
        entry = self._record(job, exe)
        if job["platform"] == "linux":
            # zip verliert das Ausführungsrecht oft: Startdatei und die übrigen erkannten Programme ausführbar machen
            for rel in [exe] + [c["path"] for c in candidates]:
                try:
                    make_executable(job["install_dir"], rel)
                except OSError as exc:
                    self._log("job %s: Ausführungsrecht nicht gesetzt: %s" % (job["id"], exc))
        job.update(exe=exe, entry_id=entry["id"])
        self._scheduler.call_soon(self._call, self._add_local, entry)
        self._close(job, "done", "„%s“ wurde installiert und ist spielbereit." % job["title"], ok=True,
                    extra={"entry": entry, "entryId": entry["id"]})
        return entry

    def _recover(self, job: dict) -> bool:
        """Nach einem Neustart: Lag das Spiel schon fertig am Zielort (Marker mit unserem Zufallswert)?"""
        if job.get("mode") != "game" or job["state"] == "queued" or not job.get("final_dir"):
            return False
        try:
            if trash.read_marker(job["final_dir"])["token"] != job["token"]:
                return False
        except (trash.Refuse, OSError):
            return False
        candidates = []
        for cand in job.get("plan_candidates") or []:
            clean = extract.verify_start_file(job["final_dir"], cand.get("path"))
            if clean:
                candidates.append({"path": clean, "score": cand.get("score", 0), "why": []})
        candidates = candidates or extract.scan_start_files(job["final_dir"], job.get("platform") or "windows")
        self._on_done(job, {"mode": "game", "path": job["final_dir"], "candidates": candidates,
                            "needs_choice": probe.needs_choice(candidates)})
        return True

    def _entry_stub(self, job: dict) -> dict:
        """Eintrag für ein Spiel, das noch keine Startdatei hat (für den Papierkorb und restore())."""
        candidates = job.get("candidates") or []
        return {"id": "local:" + job["entry_uuid"], "title": job["title"], "platform": job["platform"],
                "install_dir": job["install_dir"], "exe": "",
                "exe_candidates": [{"path": c["path"], "score": c.get("score", 0)} for c in candidates],
                "args": "", "created": job.get("finished") or self._clock(), "archive": job["archive"],
                "marker": job["token"]}

    def _close(self, job: dict, state: str, message: str, ok: bool = False, extra: dict | None = None) -> None:
        """Auftrag abschließen, speichern, "install.done" melden. Unter self._lock aufrufen."""
        job.update(state=state, message=message, file=None, finished=self._clock())
        if ok:
            job["percent"] = 100
        self._emitted.pop(job["id"], None)
        self._save()
        payload = {"jobId": job["id"], "ok": ok, "message": message, "state": state, "title": job["title"]}
        if job.get("code") and not ok:
            payload["code"] = job["code"]
        payload.update(extra or {})
        self._emit("install.done", payload)

    # ------------------------------------------------------------ Ereignisse, Ansicht, Ablage

    def _progress(self, job: dict, force: bool = False) -> None:
        """Meldet den Stand eines Auftrags, gedrosselt auf PROGRESS_MIN_INTERVAL (außer force)."""
        with self._lock:
            if job["state"] not in ("queued", "running", "finishing", "needs-choice"):
                return
            stand = (job["state"], job["percent"], job["file"])
            last_time, last_stand = self._emitted.get(job["id"], (0.0, None))
            now = time.monotonic()
            if stand == last_stand or (not force and now - last_time < PROGRESS_MIN_INTERVAL):
                return
            self._emitted[job["id"]] = (now, stand)
            elapsed = max(0.0, self._clock() - job["started"]) if job.get("started") else 0.0
            payload = {"jobId": job["id"], "percent": job["percent"], "file": job["file"], "state": job["state"],
                       "title": job["title"], "elapsed": round(elapsed, 1)}    # elapsed: Sekunden seit dem Start, für die Restzeit
            if job["state"] == "needs-choice":
                payload["candidates"] = job.get("candidates") or []
                payload["message"] = job["message"]
        self._emit("install.progress", payload)

    def _emit(self, event: str, payload: dict) -> None:
        self._scheduler.call_soon(self._call, self._emit_cb, event, payload)

    def _call(self, fn: Callable, *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:                 # ein Fehler im Empfänger darf keinen Auftrag abbrechen
            self._log("Rückruf %s gescheitert: %r" % (getattr(fn, "__name__", fn), exc))

    def _view(self, job: dict) -> dict:
        view = {"id": job["id"], "jobId": job["id"], "title": job["title"], "percent": job["percent"],
                "state": job["state"], "file": job["file"], "message": job["message"], "archive": job["archive"],
                "kind": job["kind"], "platform": job["platform"], "system": job["system"], "created": job["created"],
                "finished": job["finished"], "installDir": job.get("install_dir"), "entryId": job.get("entry_id")}
        if job["state"] == "needs-choice":
            view["candidates"] = job.get("candidates") or []
        return view

    def _load(self) -> None:
        try:
            with open(os.path.join(self._state_dir, JOBS_FILE), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        stored = data.get("jobs") if isinstance(data, dict) else None
        for job in stored if isinstance(stored, list) else []:
            needed = ("id", "state", "title", "archive", "token", "entry_uuid", "created", "games_root")
            if isinstance(job, dict) and all(key in job for key in needed) and job["id"] not in self._jobs:
                for key, default in (("percent", 0), ("file", None), ("message", ""), ("kind", None), ("platform", None),
                                     ("system", None), ("finished", None)):
                    job.setdefault(key, default)
                self._jobs[job["id"]] = job

    def _save(self) -> None:
        """Auftragsliste atomar schreiben (ohne Kennwörter, ohne flüchtige Felder). Unter self._lock aufrufen."""
        finished = sorted((j for j in self._jobs.values() if j["state"] in ("done", "failed", "cancelled")),
                          key=lambda j: j["finished"] or 0)
        for old in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            del self._jobs[old["id"]]
        volatile = ("cancel_requested", "killed", "worker_pid")
        jobs = [{k: v for k, v in job.items() if k not in volatile} for job in self._jobs.values()]
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            path = os.path.join(self._state_dir, JOBS_FILE)
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(jsontext.dumps({"version": 1, "jobs": jobs}, indent=1))
            os.replace(tmp, path)
        except OSError as exc:
            self._log("Auftragsliste nicht gespeichert: %s" % exc)

    def _rotate_log(self) -> None:
        path = os.path.join(self._state_dir, LOG_FILE)
        try:
            if os.path.getsize(path) > 1024 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass

    def _log(self, text: str) -> None:
        """Eine Zeile ins eigene Protokoll. Hier landet nie ein Kennwort und nie eine Befehlszeile."""
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            with open(os.path.join(self._state_dir, LOG_FILE), "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass
