"""Aufträge des Einrichtungs-Assistenten (E105, Bauplan 8.7). Ein Auftrag zugleich, in einem eigenen
Arbeits-Thread mit niedrigem Vorrang; Reihenfolge: Ordner → Werkzeuge (umu, legendary) → Flatpaks (eine
Transaktion über das Hilfsprogramm) → Proton (Warteschlange aus E102, wiring.proton_jobs).

Stand liegt in <state>/setup-jobs.json, solange ein Auftrag läuft: Findet ein neues Home die Datei, war der
Auftrag unterbrochen (D4). Ereignisse setup.progress (höchstens 4/s) und setup.done; unter Downloads im
Control Center steht der Auftrag in sys.state.store_jobs mit store "setup" (Kreuz: store.cancel).

Das Hilfsprogramm: app/bin/couch-setup-flatpak; im Prüfmodus nur die Attrappe aus COUCH_SETUP_FLATPAK
(Dateiname beginnt mit „fake-“), sonst gar keins. Beendet wird nur der eigene Kindprozess über seine PID.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from .. import jsontext, log, paths, system
from . import catalog, folders, plan as plan_mod, state as state_mod, umu

_log = log.get("setup.jobs")

STORE = "setup"
PROGRESS_S = 0.25
VIEW_S = 1.0
HELPER_WAIT_S = 15.0
PLAN_TIMEOUT_S = 90.0
PROTON_LIST_WAIT_S = 90.0
ITEM_TEXT = {"waiting": "Wartet", "running": "", "done": "Fertig", "failed": "Fehlgeschlagen",
             "skipped": "Schon installiert", "cancelled": "Abgebrochen"}
FINAL = ("done", "failed", "skipped", "cancelled")
BUSY_TEXT = "Die Einrichtung installiert gerade Emulatoren und Apps."
JOBS_FILE = "setup-jobs.json"


def jobs_file() -> str:
    return os.path.join(paths.state_dir(), JOBS_FILE)


def helper_argv() -> list | None:
    """Befehl des Hilfsprogramms. Im Prüfmodus nur eine Attrappe (fake-…) aus COUCH_SETUP_FLATPAK."""
    if paths.test_mode():
        fake = os.environ.get("COUCH_SETUP_FLATPAK") or ""
        if fake and os.path.basename(fake).startswith("fake-") and os.access(fake, os.X_OK):
            return [os.path.realpath(fake)]
        return None
    return [os.path.join(paths.app_dir(), "bin", "couch-setup-flatpak")]


def _lower_priority() -> None:
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (OSError, AttributeError):
        pass


def _thread(fn: Callable[[], None]) -> None:
    def run() -> None:
        _lower_priority()
        try:
            fn()
        except Exception:
            _log.exception("Einrichtungs-Thread")
    threading.Thread(target=run, name="couch-setup", daemon=True).start()


def in_cage(path: str) -> bool:
    """Prüfmodus: nur Orte im Käfig (aufgelöst), nie die echten Platten des Nutzers."""
    root = paths.test_root()
    if not root:
        return False
    probe = os.path.abspath(path)
    while not os.path.lexists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    real, top = os.path.realpath(probe), os.path.realpath(root)
    return real == top or real.startswith(top + os.sep)


class Helper:
    """Ein Lauf des Hilfsprogramms: Zeilen lesen, Rückgabewert, Abbruch über SIGTERM an die eigene PID."""

    def __init__(self, argv: list, popen: Callable | None = None, env: dict | None = None):
        self.argv = list(argv)
        self.popen = popen or subprocess.Popen
        self.env = env
        self.proc: Any = None
        self.lock = threading.Lock()

    def start(self, args: list) -> None:
        self.proc = self.popen(self.argv + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, errors="replace", env=self.env)

    def lines(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                _log.info("Hilfsprogramm: %s", line[:200])
                continue
            if isinstance(data, dict):
                yield data

    def finish(self, timeout: float = HELPER_WAIT_S) -> int:
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.terminate()
            return self.proc.wait(timeout)
        finally:
            out = getattr(self.proc, "stdout", None)     # Rohr schließen (sonst ResourceWarning je Lauf)
            if out is not None:
                try:
                    out.close()
                except (OSError, ValueError):
                    pass

    def signal_term(self) -> None:
        """Nur bitten (SIGTERM an die eigene PID), nicht warten: für das Ende des Home."""
        with self.lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def terminate(self) -> None:
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(HELPER_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass

    def run(self, args: list, timeout: float = PLAN_TIMEOUT_S) -> tuple:
        """(Rückgabewert, Ereignisse) für kurze Aufrufe (remote-ensure, plan)."""
        self.start(args)
        events: list = []
        timer = threading.Timer(timeout, self.terminate)
        timer.daemon = True
        timer.start()
        try:
            events = list(self.lines())
            rc = self.finish()
        finally:
            timer.cancel()
        return rc, events


class SetupJobs:
    """Warteschlange mit höchstens einem Auftrag. Öffentliche Methoden kehren schnell zurück, außer
    probe_plan() (Arbeits-Thread)."""

    def __init__(self, part: Any):
        self.part = part
        self.wiring = part.wiring
        self.app = part.wiring.app
        self.start_thread: Callable = getattr(self.wiring, "setup_thread", None) or _thread
        self.popen: Callable | None = getattr(self.wiring, "setup_popen", None)
        self.umu_get: Callable | None = getattr(self.wiring, "setup_umu_get", None)
        self.clock: Callable[[], float] = time.monotonic
        self.lock = threading.RLock()
        self.job: dict | None = None
        self.serial = 0
        self.closed = False
        self.helper: Helper | None = None
        self._last_progress = -1e9
        self._last_view = -1e9

    # ------------------------------------------------------------ Hilfsprogramm
    def _helper(self) -> Helper | None:
        argv = helper_argv()
        if argv is None:
            return None
        if self.popen is None:
            # endet mit dem Home, auch wenn es hart beendet wird (setpriv --pdeathsig, wie wiring._popen_bound)
            setpriv = shutil.which("setpriv")
            if setpriv:
                argv = [setpriv, "--pdeathsig", "TERM"] + argv
        return Helper(argv, popen=self.popen, env=dict(os.environ))

    def probe_plan(self, ids: list) -> dict | None:
        """Probelauf des Hilfsprogramms (Arbeits-Thread). None, wenn es keins gibt oder es scheitert."""
        if not ids:
            return {"ev": "plan", "ops": [], "skipped": [], "exact": True}
        helper = self._helper()
        if helper is None:
            return None
        try:
            rc, events = helper.run(["plan", *ids])
        except OSError as e:
            _log.warning("Probelauf: %s", e)
            return None
        got = next((e for e in events if e.get("ev") == "plan"), None)
        if got is None:
            _log.warning("Probelauf ohne Ergebnis (Rückgabewert %s)", rc)
        return got

    # ------------------------------------------------------------ Auftrag
    def running(self) -> bool:
        with self.lock:
            return self.job is not None

    def start(self, selection: dict, where: dict) -> dict:
        with self.lock:
            if self.closed:
                return {"ok": False, "message": "Das Home endet gerade."}
            if self.job is not None:
                return {"ok": False, "message": "Die Einrichtung läuft schon."}
            self.serial += 1
            job_id = "setup-%d-%d" % (int(time.time()), self.serial)
            job = {"id": job_id, "selection": selection, "where": where, "items": self._items(selection),
                   "stop": threading.Event(), "started": self.clock(), "bytes": 0, "speed": 0.0,
                   "total_bytes": int(where.get("download") or 0), "rate": None, "failed": []}
            self.job = job
        self._save(job)
        self.part.set_state("running")
        self._view(force=True)
        try:
            self.start_thread(lambda: self._work(job))
        except Exception:
            _log.exception("Einrichtungs-Thread startet nicht")
            with self.lock:
                self.job = None
            self._drop_file()
            self.part.set_state("later")
            return {"ok": False, "message": "Die Einrichtung ließ sich nicht starten."}
        _log.info("Einrichtung %s: %s", job_id, ", ".join(i["id"] for i in job["items"]))
        return {"ok": True, "jobId": job_id}

    def _items(self, selection: dict) -> list:
        items = [{"id": "folders", "title": "Ordner anlegen", "kind": "folders"}]
        for tool_id in selection["tools"]:
            items.append({"id": tool_id, "title": catalog.get(tool_id)["title"], "kind": tool_id})
        for item_id in selection["emulators"] + selection["apps"]:
            item = catalog.get(item_id)
            items.append({"id": item_id, "title": item["title"], "kind": "flatpak", "flatpak": item["flatpak"]})
        if selection["proton"]:
            items.append({"id": selection["proton"], "title": catalog.get(selection["proton"])["title"],
                          "kind": "proton"})
        for it in items:
            it.update(state="waiting", percent=0, bytes=0, speed=0, message="")
        return items

    def cancel(self, job_id: str | None = None) -> dict:
        with self.lock:
            job = self.job
            if job is None or (job_id and job_id != job["id"]):
                return {"ok": False, "message": "Es läuft keine Einrichtung."}
            job["stop"].set()
            helper = self.helper
        if helper is not None:
            threading.Thread(target=helper.terminate, daemon=True).start()
        pj = getattr(self.wiring, "proton_jobs", None)
        name = job.get("proton_name")
        if pj is not None and name:
            try:
                pj.cancel(name)
            except Exception:
                _log.exception("Proton-Auftrag der Einrichtung abbrechen")
        return {"ok": True, "message": "Die Einrichtung wird abgebrochen …"}

    def busy(self) -> str | None:
        return BUSY_TEXT if self.running() else None

    def stop(self) -> None:
        """shutdown_hooks: laufenden Auftrag beenden; setup-jobs.json bleibt (nächster Start: D4)."""
        with self.lock:
            self.closed = True
            job, helper = self.job, self.helper
        if job is not None:
            job["stop"].set()
            job["shutdown"] = True
        if helper is not None:
            helper.signal_term()                # nicht warten: Ende-Haken sind kurz (der Arbeits-Thread wartet)

    # ------------------------------------------------------------ Ablauf (Arbeits-Thread)
    def _work(self, job: dict) -> None:
        try:
            for step in (self._folders, self._tools, self._flatpaks, self._proton):
                if job["stop"].is_set():
                    break
                try:
                    step(job)
                except Exception:
                    _log.exception("Einrichtung: Schritt %s", step.__name__)
                    for it in job["items"]:
                        if it["state"] in ("waiting", "running") and self._belongs(it, step.__name__):
                            self._set(job, it, "failed", message="Das hat nicht geklappt.")
        finally:
            self._finish(job)

    @staticmethod
    def _belongs(item: dict, step: str) -> bool:
        kind = item["kind"]
        return {"_folders": kind == "folders", "_tools": kind in ("umu", "legendary"), "_flatpaks": kind == "flatpak"
                or kind == "runtime", "_proton": kind == "proton"}.get(step, False)

    def _item(self, job: dict, item_id: str) -> dict | None:
        return next((i for i in job["items"] if i["id"] == item_id), None)

    def _set(self, job: dict, item: dict, st: str, **fields: Any) -> None:
        with self.lock:
            item["state"] = st
            item.update(fields)
            if st in FINAL:
                item["speed"] = 0
                if st == "done":
                    item["percent"] = 100
                if st == "failed":
                    # code für S9 („Nicht installiert: … (keine Verbindung)“, S.setupWhy)
                    item["code"] = str(item.get("code") or "failed")
                    job["failed"].append({"id": item["id"], "title": item["title"], "message": item.get("message") or "",
                                          "code": item["code"]})
        self._save(job)
        self._progress(job, force=True)

    def _folders(self, job: dict) -> None:
        item = self._item(job, "folders")
        where, systems = job["where"], job["selection"]["systems"]
        self._set(job, item, "running")
        root = where["emuRoot"]
        if paths.test_mode() and not in_cage(root):
            self._set(job, item, "failed", message="Im Prüfmodus legt der Assistent nur Ordner im Käfig an.")
            return
        try:
            created = folders.make(root, systems, where["base"], where.get("emudeck") or "")
        except folders.FolderError as e:
            state_mod.record_folders(getattr(e, "created", []))
            self._set(job, item, "failed", message=str(e))
            return
        state_mod.record_folders(created)
        job["folders"] = created
        self._set(job, item, "done", message="")

    def _tools(self, job: dict) -> None:
        for tool_id in job["selection"]["tools"]:
            if job["stop"].is_set():
                return
            item = self._item(job, tool_id)
            self._set(job, item, "running")
            if tool_id == "umu":
                self._umu(job, item)
            elif tool_id == "legendary":
                self._legendary(job, item)
            else:
                self._set(job, item, "failed", message="Unbekanntes Werkzeug.")

    def _umu(self, job: dict, item: dict) -> None:
        if umu.present():
            self._set(job, item, "skipped")
            return

        def tick(done: int, total: int) -> None:
            item["bytes"] = done
            item["percent"] = int(done * 100 / total) if total else 0
            self._progress(job)
        try:
            umu.ensure(get=self.umu_get, tick=tick, stop=job["stop"])
        except umu.UmuError as e:
            self._set(job, item, "cancelled" if e.code == "cancelled" else "failed", message=e.message,
                      code=str(e.code or "failed"))
            return
        self._set(job, item, "done")

    def _legendary(self, job: dict, item: dict) -> None:
        legendary = getattr(self.wiring, "epic_legendary", None)
        if legendary is None:
            self._set(job, item, "failed", message="Der Store ist gerade nicht verfügbar.")
            return
        try:
            if legendary.ready():
                self._set(job, item, "skipped")
                return
            if paths.test_mode():
                self._set(job, item, "failed", message="Im Prüfmodus lädt der Assistent nichts aus dem Netz.")
                return
            legendary.ensure()                  # ~3 MB, Prüfsumme fest im Code (E73)
        except Exception as e:  # noqa: BLE001  (EpicError trägt einen deutschen Satz)
            self._set(job, item, "failed", message=str(getattr(e, "args", [""])[0] or "Das hat nicht geklappt."))
            return
        self._set(job, item, "done")

    def _flatpaks(self, job: dict) -> None:
        wanted = [i for i in job["items"] if i["kind"] == "flatpak"]
        if not wanted:
            return
        helper = self._helper()
        if helper is None:
            for it in wanted:
                self._set(job, it, "failed", message="Ohne Flatpak lassen sich keine Emulatoren und Apps installieren."
                          if not paths.test_mode() else "Im Prüfmodus fehlt die Flatpak-Attrappe.")
            return
        rc, events = helper.run(["remote-ensure"], timeout=150)
        remote = next((e for e in events if e.get("ev") == "remote"), {})
        if not remote.get("ok"):
            text = remote.get("message") or "Flathub ist gerade nicht erreichbar."
            for it in wanted:
                self._set(job, it, "failed", message=text, code=remote.get("code") or "failed")
            return
        if job["stop"].is_set():
            return
        helper = self._helper()
        with self.lock:
            self.helper = helper
        by_flatpak = {it["flatpak"]: it for it in wanted}
        saw_end = {"ok": None, "cancelled": False}
        try:
            helper.start(["install", *[it["flatpak"] for it in wanted]])
            for ev in helper.lines():
                self._helper_event(job, ev, by_flatpak, saw_end)
            rc = helper.finish()
        except OSError as e:
            _log.warning("Hilfsprogramm startet nicht: %s", e)
            rc = 1
        finally:
            with self.lock:
                self.helper = None
        cancelled = job["stop"].is_set() or saw_end["cancelled"] or rc == 3
        for it in job["items"]:
            if it["kind"] in ("flatpak", "runtime") and it["state"] in ("waiting", "running"):
                if cancelled:
                    self._set(job, it, "cancelled", message="Abgebrochen.")
                elif saw_end["ok"] is None:
                    self._set(job, it, "failed", message="Das Hilfsprogramm hat unerwartet aufgehört.")
                else:
                    self._set(job, it, "failed", message="Das hat nicht geklappt.")
        done = [{"id": it["id"], "flatpak": it["flatpak"], "installation": it.get("installation") or "user"}
                for it in wanted if it["state"] in ("done", "skipped")]
        if done:
            state_mod.record_items(done)
            job["installed"] = done

    def _helper_event(self, job: dict, ev: dict, by_flatpak: dict, saw_end: dict) -> None:
        kind = ev.get("ev")
        ref = str(ev.get("ref") or "")
        target = self._target(job, ref, by_flatpak, create=kind == "op-start")
        if kind == "skip":
            item = by_flatpak.get(str(ev.get("id") or ""))
            if item is not None:
                item["installation"] = str(ev.get("installation") or "user")
                self._set(job, item, "skipped", message="Schon auf diesem Gerät (systemweit)."
                          if item["installation"] == "system" else "Schon installiert.")
        elif kind == "op-start" and target is not None:
            self._set(job, target, "running", percent=0)
        elif kind == "progress" and target is not None:
            with self.lock:
                target["percent"] = max(0, min(100, int(ev.get("percent") or 0)))
                new = int(ev.get("bytes") or 0)
                job["bytes"] += max(0, new - int(target.get("bytes") or 0))
                target["bytes"] = new
            self._rate(job, target)
            self._progress(job)
        elif kind == "op-done" and target is not None:
            self._set(job, target, "skipped" if ev.get("skipped") else "done")
        elif kind == "op-error":
            text = str(ev.get("message") or "Das hat nicht geklappt.")
            if target is not None:
                self._set(job, target, "failed", message=text, code=str(ev.get("code") or "failed"))
            else:
                for it in by_flatpak.values():
                    if it["state"] in ("waiting", "running"):
                        self._set(job, it, "failed", message=text, code=str(ev.get("code") or "failed"))
        elif kind == "end":
            saw_end["ok"] = bool(ev.get("ok"))
            saw_end["cancelled"] = bool(ev.get("cancelled"))

    def _target(self, job: dict, ref: str, by_flatpak: dict, create: bool) -> dict | None:
        parts = ref.split("/")
        if len(parts) >= 2 and parts[0] == "app":
            return by_flatpak.get(parts[1])
        if len(parts) >= 2 and parts[0] == "runtime":
            item_id = "runtime:" + ref
            found = self._item(job, item_id)
            if found is None and create:
                found = {"id": item_id, "title": catalog.ref_title(ref), "kind": "runtime", "state": "waiting",
                         "percent": 0, "bytes": 0, "speed": 0, "message": ""}
                with self.lock:
                    # Laufzeiten stehen vor den Apps (sie kommen zuerst)
                    first = next((n for n, i in enumerate(job["items"]) if i["kind"] == "flatpak"), len(job["items"]))
                    job["items"].insert(first, found)
            return found
        return None

    def _rate(self, job: dict, item: dict) -> None:
        now = self.clock()
        rate = job.get("rate")
        if rate is None or rate[1] > job["bytes"]:
            job["rate"] = (now, job["bytes"])
            return
        dt = now - rate[0]
        if dt >= 2.0:
            speed = (job["bytes"] - rate[1]) / dt
            job["speed"] = speed if job["speed"] <= 0 else 0.7 * job["speed"] + 0.3 * speed
            job["rate"] = (now, job["bytes"])
            item["speed"] = int(job["speed"])

    def _proton(self, job: dict) -> None:
        family = job["selection"]["proton"]
        if not family:
            return
        item = self._item(job, family)
        self._set(job, item, "running")
        pj = getattr(self.wiring, "proton_jobs", None)
        if pj is None:
            self._set(job, item, "failed", message="Proton-Versionen sind gerade nicht verfügbar.")
            return
        name = self._proton_name(pj, family, job["stop"])
        if job["stop"].is_set():
            self._set(job, item, "cancelled", message="Abgebrochen.")
            return
        if not name:
            self._set(job, item, "failed", message="Für %s ist gerade keine Fassung zu finden. Später unter Einstellungen › "
                                                   "System › Updates › Proton-Versionen." % item["title"])
            return
        compat = pj.compat_dir()
        if compat and os.path.isdir(os.path.join(compat, name)):
            self._set(job, item, "skipped")
            return
        result = pj.install(name, origin="setup") or {}
        if not result.get("ok"):
            self._set(job, item, "failed", message=str(result.get("message") or "Das hat nicht geklappt."))
            return
        job["proton_name"] = name
        while not job["stop"].is_set():
            current = pj.running() or {}
            waiting = [p.get("name") for p in pj.pending() or []]
            if current.get("name") != name and name not in waiting:
                break
            if current.get("name") == name:
                item["percent"] = int(current.get("percent") or 0)
                self._progress(job)
            job["stop"].wait(0.5)
        if job["stop"].is_set():
            self._set(job, item, "cancelled", message="Abgebrochen.")
        elif compat and os.path.isdir(os.path.join(compat, name)):
            self._set(job, item, "done")
        else:
            self._set(job, item, "failed", message="%s ließ sich nicht installieren. Einzelheiten stehen unter Downloads."
                      % item["title"])

    @staticmethod
    def _proton_name(pj: Any, family: str, stop: threading.Event) -> str:
        """Neueste Fassung der Familie aus der Liste der Proton-Versionen (E102); wartet, bis sie gelesen ist."""
        from .. import proton_repair
        waited = 0.0
        while True:
            snap = pj.catalog() or {}
            items = [i for i in snap.get("items") or [] if i.get("family") == family and proton_repair.valid_name(
                str(i.get("name") or ""))]
            if items or (snap.get("fetched") and not snap.get("loading")) or waited >= PROTON_LIST_WAIT_S:
                break
            if stop.wait(1.0):
                return ""
            waited += 1.0
        items.sort(key=lambda i: proton_repair.version_key(str(i["name"])), reverse=True)
        return str(items[0]["name"]) if items else ""

    def _finish(self, job: dict) -> None:
        cancelled = job["stop"].is_set() and not job.get("shutdown")
        failed = list(job["failed"])
        ok = not failed and not cancelled
        with self.lock:
            if self.job is job:
                self.job = None
        if job.get("shutdown"):
            return                              # das Home endet: setup-jobs.json bleibt für D4
        self._drop_file()
        self.part.set_state("later" if cancelled else "done", last=True)
        self._view(force=True)
        self.part.emit("setup.done", {"jobId": job["id"], "ok": ok, "cancelled": cancelled, "failed": failed})
        self._progress(job, force=True)
        if any(it["state"] == "done" for it in job["items"] if it["kind"] in ("flatpak", "folders", "umu")):
            self.part.rescan()
        if cancelled:
            self.part.notify("Einrichtung abgebrochen", "Was schon fertig ist, bleibt installiert. Den Rest kannst du "
                             "später unter Einstellungen › PC5 Launcher › Einrichtung nachholen.", "info")
        elif failed:
            names = ", ".join(f["title"] for f in failed[:3])
            self.part.notify("Einrichtung fast fertig", "Nicht installiert: %s. Nachholen unter Einstellungen › PC5 "
                             "Launcher › Einrichtung." % names, "warn")
        else:
            self.part.notify("Einrichtung fertig", "Emulatoren und Apps sind installiert.", "update")
        _log.info("Einrichtung %s: %s", job["id"], "abgebrochen" if cancelled else ("fertig" if ok else
                                                                                       "mit Fehlern fertig"))

    # ------------------------------------------------------------ Stand und Anzeige
    def _save(self, job: dict) -> None:
        data = {"jobId": job["id"], "state": "running", "at": int(time.time()),
                "params": job["where"].get("params") or {},
                "items": [{k: it.get(k) for k in ("id", "title", "kind", "state", "message")} for it in job["items"]]}
        try:
            system.write_atomic(jobs_file(), jsontext.dumps(data, indent=1) + "\n", mode=0o600)
        except OSError as e:
            _log.warning("setup-jobs.json nicht schreibbar: %s", e)

    @staticmethod
    def _drop_file() -> None:
        try:
            os.unlink(jobs_file())
        except FileNotFoundError:
            pass
        except OSError as e:
            _log.warning("setup-jobs.json nicht entfernt: %s", e)

    def payload(self, job: dict) -> dict:
        with self.lock:
            items = [{k: it.get(k) for k in ("id", "title", "state", "percent", "bytes", "speed", "message")}
                     for it in job["items"]]
        done = sum(1 for it in items if it["state"] in FINAL)
        eta = 0
        elapsed = self.clock() - job["started"]
        if elapsed >= 10 and job["speed"] > 0 and job["total_bytes"] > job["bytes"]:
            eta = int((job["total_bytes"] - job["bytes"]) / job["speed"])
        return {"jobId": job["id"], "items": items, "done": done, "total": len(items), "eta": eta}

    def _progress(self, job: dict, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_progress < PROGRESS_S:
            return
        self._last_progress = now
        self.part.emit("setup.progress", self.payload(job))
        if force or now - self._last_view >= VIEW_S:
            self._view(force=True)

    def view(self) -> list:
        with self.lock:
            job = self.job
        if job is None:
            return []
        p = self.payload(job)
        current = next((it for it in p["items"] if it["state"] == "running"), None)
        percent = int(p["done"] * 100 / p["total"]) if p["total"] else 0
        text = "%d von %d fertig" % (p["done"], p["total"])
        if current is not None:
            text = "%s · %s" % (current["title"], text)
        # Titel „Emulatoren und Apps“: darunter steht die Notiz „Einrichtung“ (panels.js DL_NOTES.setup)
        out = {"store": STORE, "id": job["id"], "title": "Emulatoren und Apps", "kind": "install",
               "state": "downloading" if current is not None else "queued", "text": text, "percent": percent,
               "speed": int(job["speed"]), "eta": p["eta"]}
        if job["total_bytes"]:
            out.update(done=min(job["bytes"], job["total_bytes"]), total=job["total_bytes"])
        return [out]

    def _view(self, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_view < VIEW_S:
            return
        self._last_view = now
        self.part.publish_jobs(self.view())
