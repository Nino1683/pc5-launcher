"""Installer(deps): Fassade des Archiv-Installers für die RPC-Methoden "install.*".

Der Verdrahter erzeugt genau ein Objekt und hängt die Methoden an den RPC-Bus. Keine Methode wirft
bei erwartbaren Fehlern: Jede liefert ein dict mit "ok" und im Fehlerfall "code" und "message"
(deutscher Satz für die Oberfläche).

deps (SimpleNamespace oder ähnliches Objekt):
    config        get(key, default) – gelesen werden install.games_root, install.reserve_gib, install.trash_days
    paths         state_dir (Wert oder Funktion); die übrigen Orte braucht der Installer nicht
    emit          emit(event, payload) – "install.progress", "install.done", "install.purgeProgress", "install.purgeDone"
    add_local     add_local(entry_dict) – neuen oder geänderten Eintrag übernehmen (gleiche id ersetzt)
    remove_local  remove_local(entry_id)
    get_local     get_local(entry_id) -> entry_dict | None
    scheduler     call_soon(fn, *args), run_in_thread(fn, *args); Vorgabe ThreadScheduler
  optional:
    is_running    is_running(entry_id) -> bool; dann verweigert delete() laufende Spiele
    version       Zeichenkette für die Markerdatei
    browse_roots  Liste (oder Funktion, die sie liefert) aus Pfaden oder {"name", "path", "kind"}: ersetzt die
                  eingebauten Wurzeln der Dateiauswahl (für Tests mit erfundenen Ordnern)
    setup_env     E114: setup_env(entry_id, title, installer) -> {"argv", "env", "log"?} für den stillen Lauf
                  eines Installationsprogramms im Prefix des späteren Eintrags (jobs.JobManager)
    discard_prefix  E114: discard_prefix(entry_id), Prefix eines nie eingetragenen Spiels wegräumen
    rom_dirs, tools, worker_argv, setup_limits   nur für Tests

Blockierend (Platte, Unterprozess; in einen Thread legen): browse, probe, delete, restore, purge,
purge_start, exe_candidates, trash_list, startup, shutdown. Schnell: start, cancel, jobs, jobs_state,
set_exe, busy, purging. rpc_table() liefert dieselbe Einteilung maschinenlesbar. (start, cancel und
set_exe fassen kurz die Platte an; schläft sie, dauert auch das. Der Verdrahter legt sie deshalb
ebenfalls in einen Thread.)

Endgültiges Löschen über die Oberfläche (purge_start) läuft in einem eigenen Thread, einer nach dem
anderen, und meldet sich über emit:
    "install.purgeProgress" {trashId, title, removed, total, percent}   höchstens 2 je Sekunde
    "install.purgeDone"     {trashId, title, ok, message, code?}
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any, Callable

from . import (InstallError, ThreadScheduler, games_root as cfg_games_root, reserve_bytes as cfg_reserve_bytes,
               trash_days as cfg_trash_days)
from . import browse as browse_mod
from . import extract, probe as probe_mod, trash
from .jobs import JobManager, make_executable

PURGE_EVENT_INTERVAL = 0.5       # Sekunden zwischen zwei Fortschrittsmeldungen beim endgültigen Löschen
# Automatisches Leeren des Papierkorbs nicht allein nach der Wanduhr: Ein Eintrag muss außerdem an
# so vielen verschiedenen Tagen beim Start im Papierkorb gesehen worden sein (eine einmal
# vorgestellte Uhr löscht sonst frisch Gelöschtes sofort). Gemerkt in <state>/trash-seen.json.
TRASH_SEEN_FILE = "trash-seen.json"
TRASH_MIN_DAYS_SEEN = 2
MSG_PURGE_STOPPED = "Das endgültige Löschen wurde unterbrochen. Der Rest bleibt im Papierkorb und lässt sich später entfernen."


def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Attribut lesen; ist es eine Funktion, ihr Ergebnis (paths kann Werte oder Funktionen anbieten)."""
    value = getattr(obj, name, default)
    return value() if callable(value) else value


def _package_version() -> str:
    try:
        from .. import VERSION
        return str(VERSION)
    except Exception:           # der Kern ist nicht unser Vertrag; ohne ihn gibt es eben keine Versionsangabe
        return ""


def _flat_entry(entry: Any) -> dict | None:
    """Bibliothekseintrag als flaches dict. Liegen install_dir/marker unter "detail", werden sie hochgezogen."""
    if not isinstance(entry, dict):
        return None
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    flat = dict(detail)
    flat.update({k: v for k, v in entry.items() if k != "detail"})
    return flat


_REMOVABLE = ("/run/media/", "/media/", "/mnt/")
EXPLORER_ARCHIVE = "aus dem Datei-Explorer"      # Spiele, die wiring_files.add_to_library anlegt (ohne Marker)


def folder_gone(install_dir: Any, mounts: Callable[[], list] | None = None) -> bool:
    """Ist der Spielordner nachweislich weg, während sein Datenträger da ist? Nur lesend.

    Fehlt er nur, weil die Platte nicht eingehängt ist, zählt das nicht: Dann bleibt der
    Bibliothekseintrag stehen (die Bibliothek zeigt ihn als nicht verfügbar). Deshalb muss der
    Elternordner vorhanden sein und, liegt er an einem Ort für Wechseldatenträger, wirklich auf
    einer Einhängung liegen (dort bleibt manchmal ein leerer Ordner zurück)."""
    if not isinstance(install_dir, str) or not os.path.isabs(install_dir) or "\0" in install_dir:
        return False
    path = os.path.normpath(install_dir)
    parent = os.path.dirname(path)
    if path == "/" or os.path.lexists(path) or not os.path.isdir(parent):
        return False
    real = os.path.realpath(parent)
    if real.startswith(_REMOVABLE):
        try:
            points = list((mounts or trash.read_mounts)())
        except OSError:
            return False
        if not any(p.startswith(_REMOVABLE) and (real == p or real.startswith(p.rstrip("/") + "/")) for p in points):
            return False
    return True


class Installer:
    def __init__(self, deps: Any):
        """Schnell; fasst die Platte nicht an. Danach einmal startup() in einem Thread aufrufen."""
        self._deps = deps
        self._config = deps.config
        self._scheduler = getattr(deps, "scheduler", None) or ThreadScheduler()
        self.manager = JobManager(
            config=self._config, state_dir=_value(deps.paths, "state_dir"), emit=deps.emit,
            add_local=deps.add_local, scheduler=self._scheduler, rom_dirs=self._rom_dirs,
            tools=getattr(deps, "tools", None), worker_argv=getattr(deps, "worker_argv", None),
            version=getattr(deps, "version", None) or _package_version(),
            setup_env=getattr(deps, "setup_env", None), discard_prefix=getattr(deps, "discard_prefix", None),
            setup_limits=getattr(deps, "setup_limits", None))
        # Endgültiges Löschen: eine Warteschlange, ein Thread. _purge_lock hält außerdem das
        # Aufräumen beim Start (purge_expired) und einen Lauf über die Oberfläche auseinander.
        self._purge_guard = threading.Lock()     # schützt die drei folgenden Felder
        self._purge_queue: list[dict] = []
        self._purge_now: dict | None = None
        self._purge_running = False
        self._purge_idle = threading.Event()
        self._purge_idle.set()
        self._purge_lock = threading.Lock()
        self._stopping = threading.Event()       # die Konsole endet: nichts Neues beginnen
        self._clock: Callable[[], float] = getattr(deps, "clock", None) or time.time
        self._sizes: dict[str, int | None] = {}  # trash_id -> belegter Platz (Einträge ändern sich nicht)

    # ------------------------------------------------------------ Hilfen

    def _games_root(self) -> str:
        return cfg_games_root(self._config)

    def _rom_dirs(self) -> dict:
        given = getattr(self._deps, "rom_dirs", None)
        if given is None:
            return probe_mod.default_rom_dirs()
        return dict(given() if callable(given) else given)

    def _roots(self) -> list:
        given = getattr(self._deps, "browse_roots", None)
        if given is None:
            return browse_mod.default_roots()   # ~/Downloads, ~/Dokumente, eingehängte Platten unter /run/media/<nutzer>
        roots = []
        for item in (given() if callable(given) else given):
            if isinstance(item, dict):
                roots.append({"name": item.get("name") or os.path.basename(item["path"]),
                              "path": os.path.realpath(item["path"]), "kind": item.get("kind", "disk")})
            elif isinstance(item, str) and os.path.isdir(item):
                roots.append({"name": os.path.basename(item.rstrip("/")) or item, "path": os.path.realpath(item),
                              "kind": "disk" if item.startswith("/run/media/") else "folder"})
        return roots

    def _later(self, fn: Callable, *args: Any) -> None:
        """Rückruf an den Kern über den Planer (kommt dort in der Hauptschleife an)."""
        self._scheduler.call_soon(fn, *args)

    # ------------------------------------------------------------ Dateiauswahl und Plan

    def browse(self, path: str | None = None) -> dict:
        """install.browse. Blockierend.

        → {"ok", "path", "parent", "name", "entries": [{"name", "path", "type", "size"}], "truncated", "total", "message"}."""
        try:
            listing = browse_mod.list_dir(path, roots=self._roots())
        except InstallError as error:
            return {"ok": False, "code": error.code, "message": error.message, "path": path, "parent": None,
                    "name": "", "entries": [], "truncated": False, "total": 0}
        listing["ok"] = True
        return listing

    def probe(self, path: str, password: str | None = None) -> dict:
        """install.probe. Blockierend (Sekunden bei großen Archiven). → Plan (siehe probe.probe); plan["ready"] sagt, ob start() geht."""
        if not browse_mod.is_allowed_archive(path, self._roots()):
            return {"archive": path, "name": os.path.basename(str(path or "")), "format": None, "volumes": None,
                    "supported": False, "ready": False, "status": "outside-roots", "kind": None,
                    "message": "Diese Datei liegt außerhalb der erlaubten Orte.",
                    "problems": [{"code": "outside-roots", "message": "Diese Datei liegt außerhalb der erlaubten Orte."}]}
        return probe_mod.probe(path, password, games_root=self._games_root(),
                               reserve_bytes=cfg_reserve_bytes(self._config), rom_dirs=self._rom_dirs(),
                               tools=getattr(self._deps, "tools", None))

    # ------------------------------------------------------------ Aufträge

    def start(self, plan: dict, choices: dict | None = None) -> dict:
        """install.start. Schnell. → {"ok": True, "jobId"} oder {"ok": False, "code", "message", "suggestion"?}.

        choices: {"title"?, "folder"?, "exe"?, "password"?}. Bei "target-exists" nennt "suggestion" einen freien Ordnernamen."""
        try:
            if not isinstance(plan, dict) or not browse_mod.is_allowed_archive(plan.get("archive"), self._roots()):
                raise InstallError("outside-roots", "Diese Datei liegt außerhalb der erlaubten Orte.")
            return {"ok": True, "jobId": self.manager.start(plan, choices)}
        except InstallError as error:
            return error.as_dict()

    def cancel(self, job_id: str) -> dict:
        """install.cancel. Schnell. → {"ok": bool, "message"?}. Das Ergebnis des Abbruchs kommt als "install.done"."""
        try:
            return {"ok": isinstance(job_id, str) and self.manager.cancel(job_id)}
        except InstallError as error:
            return error.as_dict()

    def jobs(self) -> dict:
        """Aufträge für die Anzeige. Schnell. → {"jobs": [{id, jobId, title, percent, state, file, message, …}]}."""
        return {"jobs": self.manager.list()}

    def jobs_state(self) -> list:
        """Kurzfassung für sys.state: [{id, title, percent, state}] aller Aufträge, die noch etwas vom Nutzer wollen
        oder laufen (queued, running, finishing, needs-choice). Schnell. Reste einer früheren Sitzung, die
        startup() erst noch als unterbrochen abschließt, stehen nicht darin."""
        return [{"id": j["id"], "title": j["title"], "percent": j["percent"], "state": j["state"]}
                for j in self.manager.open_jobs()]

    def busy(self) -> bool:
        """Wartet oder läuft eine Installation? (Rückfrage vor Ausschalten, Neustart, Übergabe an Steam.)"""
        return self.manager.busy()

    def rpc_table(self) -> dict:
        """Für den Verdrahter: RPC-Name -> (Funktion(params: dict) -> dict, blockierend: bool).

        Blockierende Einträge gehören in einen Thread (Future an die Brücke), die anderen dürfen
        direkt in der Hauptschleife laufen. Fehlende Parameter führen zu einem Ergebnis mit ok=False."""
        return {
            "install.browse": (lambda p: self.browse(p.get("path")), True),
            "install.probe": (lambda p: self.probe(p.get("path"), p.get("password")), True),
            "install.start": (lambda p: self.start(p.get("plan"), p.get("choices")), False),
            "install.cancel": (lambda p: self.cancel(p.get("jobId")), False),
            "install.jobs": (lambda p: self.jobs(), False),
            # E27: install.setExe {jobId, exe}; die älteren Namen id/path gelten weiter
            "install.setExe": (lambda p: self.set_exe(p.get("jobId") or p.get("id"),
                                                      p.get("exe") or p.get("path")), False),
            # forget=true: Fehlt der Spielordner (delete meldet dann can_forget), nur den Eintrag entfernen
            # folder: der Ordner, den der Nutzer für ein von Hand hinzugefügtes Spiel bestätigt hat (E91)
            "install.delete": (lambda p: self.delete(p.get("id"), forget=p.get("forget") is True,
                                                     folder=p.get("folder")), True),
            "install.trashList": (lambda p: self.trash_list(), True),
            "install.restore": (lambda p: self.restore(p.get("trashId")), True),
            # antwortet sofort; Verlauf und Ergebnis kommen als install.purgeProgress / install.purgeDone
            "install.purge": (lambda p: self.purge_start(p.get("trashId")), True),
        }

    def set_exe(self, ref: str, path: str) -> dict:
        """Startdatei wählen. ref ist eine Auftrags-ID ("job-…", Zustand "needs-choice") oder eine Eintrags-ID ("local:…").

        Kurz blockierend. → {"ok": True, "entry"} oder {"ok": False, "code", "message"}."""
        try:
            if isinstance(ref, str) and ref.startswith("local:"):
                return {"ok": True, "entry": self._set_entry_exe(ref, path)}
            if not isinstance(ref, str):
                raise InstallError("no-choice", "Für diesen Auftrag wird keine Startdatei erwartet.")
            return {"ok": True, "entry": self.manager.set_exe(ref, path)}
        except InstallError as error:
            return error.as_dict()

    def _set_entry_exe(self, entry_id: str, path: str) -> dict:
        entry = _flat_entry(self._deps.get_local(entry_id))
        if entry is None or not isinstance(entry.get("install_dir"), str):
            raise InstallError("unknown-entry", "Dieses Spiel ist nicht in der Bibliothek.")
        exe = extract.verify_start_file(entry["install_dir"], path)
        if exe is None:
            raise InstallError("bad-exe", "Diese Datei liegt nicht im Spielordner.")
        try:
            marker = trash.read_marker(entry["install_dir"])
        except trash.Refuse as refuse:
            raise InstallError(refuse.code, refuse.message)
        if marker["token"] != entry.get("marker"):
            raise InstallError("marker-mismatch", "Der Spielordner gehört nicht zu diesem Bibliothekseintrag.")
        updated = dict(entry, exe=exe)
        if updated.get("platform") == "linux":
            try:
                make_executable(entry["install_dir"], exe)
            except OSError:
                pass
        self._later(self._deps.add_local, updated)
        return updated

    # ------------------------------------------------------------ Löschen und Papierkorb

    def delete(self, entry_id: str, forget: bool = False, folder: Any = None) -> dict:
        """Archiv-Installation in den Papierkorb verschieben (ein rename). Blockierend, aber kurz.

        → {"ok": True, "trash_id", "message"} oder {"ok": False, "code", "message"}. Bei Erfolg wird
        remove_local(entry_id) aufgerufen. Prefixe und Spielstände bleiben unberührt.

        Ist der Spielordner nicht mehr da, obwohl sein Datenträger eingehängt ist (von Hand gelöscht),
        gibt es nichts zu verschieben: → {"ok": False, "code": "missing", "can_forget": True, "message"}.
        Mit forget=True wird dann nur der Bibliothekseintrag entfernt (auf der Platte geschieht nichts):
        → {"ok": True, "forgotten": True, "message"}.

        Spiele aus dem Datei-Explorer (ohne Marker, E91) gehen über delete_foreign."""
        entry = _flat_entry(self._deps.get_local(entry_id)) if isinstance(entry_id, str) else None
        if entry is None:
            return {"ok": False, "code": "unknown-entry", "message": "Dieses Spiel ist nicht in der Bibliothek."}
        is_running = getattr(self._deps, "is_running", None)
        if callable(is_running) and is_running(entry_id):
            return {"ok": False, "code": "running", "message": "Das Spiel läuft gerade. Schließe es zuerst."}
        if entry.get("archive") == EXPLORER_ARCHIVE and not entry.get("marker") \
                and not folder_gone(entry.get("install_dir")):
            return self.delete_foreign(entry_id, entry, folder)
        if entry.get("archive") in trash.STORE_LEVELS and not folder_gone(entry.get("install_dir")):
            # Store-Spiel in GOG/<Titel> (E99): auch hier nie ein Ordner mit einer Steam-Bibliothek darin
            shared = getattr(self._deps, "shared_folder", None)
            other = shared(entry_id, entry.get("install_dir"), entry.get("install_dir")) if callable(shared) else None
            if other:
                return {"ok": False, "code": "shared-folder",
                        "message": "Im Ordner %s liegt auch %s. Er wird nicht angefasst." % (entry.get("install_dir"), other)}
        result = trash.delete_install(entry, self._games_root())
        if result["ok"]:
            self._later(self._deps.remove_local, entry_id)
        elif folder_gone(entry.get("install_dir")):
            title = str(entry.get("title") or "Das Spiel")
            if forget:
                self._later(self._deps.remove_local, entry_id)
                return {"ok": True, "forgotten": True,
                        "message": "„%s“ wurde aus der Bibliothek entfernt. Der Spielordner war nicht mehr vorhanden." % title}
            return {"ok": False, "code": "missing", "can_forget": True,
                    "message": "Der Spielordner von „%s“ ist nicht mehr vorhanden. "
                               "Du kannst den Eintrag aus der Bibliothek entfernen." % title}
        return result

    def delete_foreign(self, entry_id: str, entry: dict, folder: Any = None) -> dict:
        """Von Hand hinzugefügtes Spiel (Datei-Explorer, ohne Marker; E91) in den Papierkorb.

        Ohne folder: nur nennen, welcher Ordner es wäre → {"ok": False, "code": "confirm-folder",
        "folder", "message"}. Mit folder = genau diesem Pfad (am Bildschirm bestätigt): Marker
        hinein (trash.adopt_folder), dann derselbe Weg wie bei eigenen Installationen. Liegt im
        Ordner noch ein anderes Spiel der Bibliothek (deps.shared_folder), wird nichts angefasst."""
        root = self._games_root()
        title = str(entry.get("title") or "Das Spiel")
        try:
            target = trash.foreign_folder(entry.get("install_dir"), root)
        except trash.Refuse as refuse:
            return refuse.result()
        shared = getattr(self._deps, "shared_folder", None)
        other = shared(entry_id, target, entry.get("install_dir")) if callable(shared) else None
        if other:
            return {"ok": False, "code": "shared-folder",
                    "message": "Im Ordner %s liegt auch %s. Er wird nicht angefasst." % (target, other)}
        if folder != target:
            return {"ok": False, "code": "confirm-folder", "folder": target, "title": title,
                    "message": "„%s“ wurde nicht über die Konsole installiert. In den Papierkorb käme der Ordner %s."
                               % (title, target)}
        exe_path = os.path.join(os.path.realpath(str(entry.get("install_dir"))), str(entry.get("exe") or ""))
        token = secrets.token_hex(16)
        adopted = dict(entry, install_dir=target, exe=os.path.relpath(exe_path, target), marker=token,
                       marker_ok=True, exe_candidates=[])
        made = trash.adopt_folder(target, root, token, {"entry_id": entry_id, "title": title})
        if not made["ok"]:
            return made
        result = trash.delete_install(adopted, root)
        if result["ok"]:
            self._later(self._deps.remove_local, entry_id)
        else:
            trash.drop_adopted_marker(target, token)
        return result

    def exe_candidates(self, entry_id: str) -> dict:
        """Mögliche Startdateien eines Archiv-Spiels, gegen die Platte geprüft. Blockierend (kurz).

        Zuerst die bei der Installation bewerteten Kandidaten, soweit sie noch im Spielordner liegen,
        danach weitere Programme aus dem Ordner (höchstens vier Ebenen tief, ohne Bewertung).
        → {"ok": True, "id", "exe" (die geltende, relativ), "available", "candidates": [{path, score, why[], current}]}
        oder {"ok": False, "code": "unknown-entry", "message", "candidates": []}."""
        entry = _flat_entry(self._deps.get_local(entry_id)) if isinstance(entry_id, str) else None
        if entry is None or not isinstance(entry.get("install_dir"), str):
            return {"ok": False, "code": "unknown-entry", "message": "Dieses Spiel ist nicht in der Bibliothek.",
                    "candidates": []}
        install_dir = entry["install_dir"]
        if not os.path.isdir(install_dir):
            return {"ok": True, "id": entry_id, "exe": str(entry.get("exe") or ""), "available": False, "candidates": [],
                    "message": "Der Spielordner ist gerade nicht erreichbar."}
        current = extract.verify_start_file(install_dir, entry.get("exe")) or ""
        found: list[dict] = []
        seen: set[str] = set()

        def take(path: Any, score: Any = 0, why: Any = None) -> None:
            clean = extract.verify_start_file(install_dir, path)
            if clean and clean not in seen:
                seen.add(clean)
                reasons = [str(w) for w in why] if isinstance(why, (list, tuple)) else [str(why)] if why else []
                number = score if isinstance(score, (int, float)) and not isinstance(score, bool) else 0
                found.append({"path": clean, "score": number, "why": reasons})

        for cand in entry.get("exe_candidates") if isinstance(entry.get("exe_candidates"), (list, tuple)) else []:
            if isinstance(cand, dict):
                take(cand.get("path"), cand.get("score"), cand.get("why"))
            else:
                take(cand)
        if current:
            take(current)
        platform = "linux" if entry.get("platform") == "linux" else "windows"
        for cand in extract.scan_start_files(install_dir, platform):
            take(cand["path"])
        for cand in found:
            cand["current"] = cand["path"] == current
        return {"ok": True, "id": entry_id, "exe": current, "available": True, "candidates": found}

    def trash_list(self) -> dict:
        """Inhalt des Papierkorbs. Blockierend (liest kleine Dateien).

        → {"items": [{trash_id, title, deleted, expires, purging, percent?, …}], "days"}. purging ist auch
        für Einträge wahr, deren endgültiges Löschen erst in der Warteschlange steht."""
        days = cfg_trash_days(self._config)
        root = self._games_root()
        items = trash.list_trash(root)
        active = {p["trash_id"]: p for p in self.purging()}
        for item in items:
            deleted = item.get("deleted")
            item["expires"] = deleted + days * 86400 if days >= 1 and isinstance(deleted, (int, float)) else None
            # Für die Rückfrage „Endgültig löschen“ (wie viel frei wird); einmal je Eintrag gemessen
            trash_id = item["trash_id"]
            if trash_id not in self._sizes or self._sizes[trash_id] is None:
                self._sizes[trash_id] = trash.item_size(trash_id, root) if item.get("present") else None
            item["size_bytes"] = self._sizes[trash_id]
            if item["trash_id"] in active:
                item["purging"] = True
                item["percent"] = active[item["trash_id"]]["percent"]
        return {"items": items, "days": days}

    def restore(self, trash_id: str) -> dict:
        """Aus dem Papierkorb zurückholen. Blockierend, kurz. Bei Erfolg wird add_local(entry) aufgerufen."""
        if isinstance(trash_id, str) and any(p["trash_id"] == trash_id for p in self.purging()):
            return {"ok": False, "code": "purging", "message": "Dieser Eintrag wird gerade endgültig gelöscht."}
        result = trash.restore(trash_id, self._games_root())
        if result["ok"] and isinstance(result.get("entry"), dict):
            self._later(self._deps.add_local, result["entry"])
        return result

    def purge(self, trash_id: str) -> dict:
        """Papierkorb-Eintrag endgültig löschen und auf das Ende warten. Nur nach ausdrücklicher
        Bestätigung aufrufen. Blockierend (lange); die Oberfläche nimmt purge_start()."""
        with self._purge_lock:
            if self._stopping.is_set():      # gar nicht erst beginnen: Ein angefangener Eintrag ließe sich nicht mehr zurückholen
                return {"ok": False, "code": "cancelled", "message": MSG_PURGE_STOPPED}
            return trash.purge(trash_id, self._games_root(), tick=self._stop_tick)

    # ------------------------------------------------------------ endgültiges Löschen im Hintergrund

    def purge_start(self, trash_id: str) -> dict:
        """install.purge: Papierkorb-Eintrag endgültig löschen, ohne darauf zu warten. Nur nach
        ausdrücklicher Bestätigung aufrufen. Kurz blockierend (liest die Angaben des Eintrags).

        → {"ok": True, "started": True, "trash_id", "title", "waiting", "message"}; waiting = Einträge davor.
        Danach "install.purgeProgress" und zum Schluss "install.purgeDone" {trashId, title, ok, message, code?}.
        → {"ok": False, "code": "not-in-trash"|"purging"|"stopping", "message"}, wenn gar nicht erst begonnen wird."""
        item = None
        if isinstance(trash_id, str):
            item = next((i for i in trash.list_trash(self._games_root()) if i["trash_id"] == trash_id), None)
        if item is None:
            return {"ok": False, "code": "not-in-trash", "message": "Dieser Eintrag ist nicht im Papierkorb."}
        title = str(item.get("title") or item.get("name") or "")
        with self._purge_guard:
            if self._stopping.is_set():
                return {"ok": False, "code": "stopping", "message": "Die Konsole wird gerade beendet."}
            if any(p["trash_id"] == trash_id for p in self._purge_queue) or \
                    (self._purge_now or {}).get("trash_id") == trash_id:
                return {"ok": False, "code": "purging", "message": "„%s“ wird schon endgültig gelöscht." % title}
            waiting = len(self._purge_queue) + (1 if self._purge_now else 0)
            self._purge_queue.append({"trash_id": trash_id, "title": title})
            start = not self._purge_running
            if start:
                self._purge_running = True
                self._purge_idle.clear()
        if start:
            # eigener Thread: Minutenlanges Löschen soll keinen Arbeiter des Kerns belegen
            threading.Thread(target=self._purge_worker, name="couch-install-purge", daemon=True).start()
        return {"ok": True, "started": True, "trash_id": trash_id, "title": title, "waiting": waiting,
                "message": "„%s“ wird endgültig gelöscht." % title}

    def purging(self) -> list:
        """Was gerade endgültig gelöscht wird oder darauf wartet. Schnell.
        → [{trash_id, title, percent (None = wartet oder Umfang unbekannt), removed, total}], der laufende zuerst."""
        with self._purge_guard:
            items = [dict(self._purge_now)] if self._purge_now else []
            items += [dict(p, percent=None, removed=0, total=None) for p in self._purge_queue]
        return items

    def stop_purge(self, timeout: float = 2.0) -> bool:
        """Endgültiges Löschen anhalten (die Konsole endet): Warteschlange leeren, der laufende Eintrag
        hört beim nächsten Dateieintrag auf; sein Rest bleibt im Papierkorb. Wartet höchstens timeout
        Sekunden. Rückgabe: True, wenn danach nichts mehr läuft."""
        self._stopping.set()
        with self._purge_guard:
            self._purge_queue.clear()
        return self._purge_idle.wait(max(0.0, timeout))

    def _stop_tick(self) -> None:
        if self._stopping.is_set():
            raise trash.Refuse("cancelled", MSG_PURGE_STOPPED)

    def _purge_worker(self) -> None:
        while True:
            with self._purge_guard:
                if not self._purge_queue or self._stopping.is_set():
                    self._purge_now = None
                    self._purge_running = False
                    self._purge_idle.set()
                    return
                item = self._purge_queue.pop(0)
                state = self._purge_now = dict(item, removed=0, total=None, percent=None)
            try:
                result = self._purge_one(state)
            except Exception as exc:        # nie den Thread verlieren: Die Warteschlange liefe sonst nie weiter
                result = {"ok": False, "code": "os-error", "message":
                          "Der Papierkorb-Eintrag konnte nicht vollständig entfernt werden. Der Rest bleibt liegen. (%s)"
                          % type(exc).__name__}
            with self._purge_guard:
                self._purge_now = None
            payload = {"trashId": state["trash_id"], "title": state["title"], "ok": bool(result.get("ok")),
                       "message": str(result.get("message") or "")}
            if not payload["ok"] and result.get("code"):
                payload["code"] = result["code"]
            self._emit("install.purgeDone", payload)

    def _purge_one(self, state: dict) -> dict:
        root = self._games_root()
        last = [0.0]

        def report(force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last[0] < PURGE_EVENT_INTERVAL:
                return
            last[0] = now
            with self._purge_guard:
                total, removed = state["total"], state["removed"]
                state["percent"] = min(99, removed * 100 // total) if total else None
                payload = {"trashId": state["trash_id"], "title": state["title"], "removed": removed,
                           "total": total, "percent": state["percent"]}
            self._emit("install.purgeProgress", payload)

        def tick() -> None:
            self._stop_tick()
            state["removed"] += 1
            report()

        with self._purge_lock:
            if self._stopping.is_set():
                return {"ok": False, "code": "cancelled", "message": MSG_PURGE_STOPPED}
            state["total"] = trash.count_item(state["trash_id"], root)
            report(force=True)
            return trash.purge(state["trash_id"], root, tick=tick)

    def _emit(self, event: str, payload: dict) -> None:
        def send() -> None:
            try:
                self._deps.emit(event, payload)
            except Exception:               # ein Fehler im Empfänger darf das Löschen nicht anhalten
                pass
        self._later(send)

    # ------------------------------------------------------------ Start und Ende

    def startup(self) -> dict:
        """Einmal nach dem Start in einem Thread: verwaiste Zwischenordner entfernen, unterbrochene
        Aufträge melden, Papierkorb-Einträge älter als install.trash_days endgültig entfernen, fertige
        Spiele ohne Bibliothekseintrag nachtragen, offene Fragen nach der Startdatei erneut stellen. Blockierend.

        → {"jobs": {interrupted, recovered, removed, refused, skipped}, "purged": [{trash_id, title, ok, message}],
           "readded": [Eintrags-IDs], "pending": [{id, title}]}."""
        # Offene Fragen zuerst: Sie brauchen die Platte nicht, und die Oberfläche soll sie sofort wieder
        # zeigen, auch wenn das Aufräumen danach dauert (schlafende Platte, großer Papierkorb).
        pending = self.manager.reannounce()
        report = {"jobs": self.manager.startup(), "purged": [], "readded": [], "pending": pending}
        if probe_mod.check_games_root(self._games_root()) is None and not self._stopping.is_set():
            with self._purge_lock:
                seen = self._trash_seen()
                report["purged"] = trash.purge_expired(self._games_root(), cfg_trash_days(self._config),
                                                       tick=self._stop_tick, now=self._clock, seen=seen,
                                                       min_days_seen=TRASH_MIN_DAYS_SEEN)
        report["readded"] = self._readd_lost(skip=report["jobs"].get("recovered") or [])
        return report

    def _trash_seen(self) -> dict:
        """Vermerkt den heutigen Tag (Wanduhr) für jeden Eintrag im Papierkorb und liefert je
        trash_id die Tage, an denen ein Start ihn gesehen hat. Blockierend, klein."""
        state = _value(self._deps.paths, "state_dir")
        path = os.path.join(state, TRASH_SEEN_FILE) if state else None
        old: dict = {}
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    old = {str(k): [d for d in v if isinstance(d, str)] for k, v in data.items() if isinstance(v, list)}
            except (OSError, ValueError):
                old = {}
        today = time.strftime("%Y-%m-%d", time.localtime(self._clock()))
        seen = {}
        for item in trash.list_trash(self._games_root()):
            days = old.get(item["trash_id"], [])
            if today not in days:
                days = days + [today]
            seen[item["trash_id"]] = days[-10:]
        if path and seen != old:
            tmp = "%s.tmp-%d" % (path, os.getpid())
            try:
                os.makedirs(state, mode=0o700, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(seen, fh)
                os.replace(tmp, path)
            except OSError:
                pass                            # ohne Gedächtnis wird eben nichts automatisch gelöscht
        return seen

    def _readd_lost(self, skip: list) -> list:
        """Fertig installierte Spiele, deren Datensatz die Bibliothek nie erreicht hat (add_local im Kern
        gescheitert, oder die Konsole endete zwischen Installation und Eintrag): noch einmal abgeben.

        Nur, wenn der Ordner mit dem Marker dieses Auftrags an seinem Platz liegt. Liegt das Spiel im
        Papierkorb oder wurde es von Hand entfernt, bleibt es dabei."""
        readded = []
        for job_id, record in self.manager.finished_records():
            if job_id in skip:
                continue                    # eben erst wiedergefunden: add_local ist schon unterwegs
            try:
                if self._deps.get_local(record["id"]) is not None:
                    continue
                if trash.read_marker(record["install_dir"])["token"] != record["marker"]:
                    continue
            except Exception:               # Platte fehlt, Marker weg, Bibliothek nicht erreichbar: nichts tun
                continue
            self._later(self._deps.add_local, record)
            readded.append(record["id"])
        return readded

    def shutdown(self, timeout: float = 20.0) -> None:
        """Vor dem Ende des Home-Prozesses: laufende Installation abbrechen und aufräumen lassen,
        endgültiges Löschen anhalten. Blockierend (höchstens timeout Sekunden und zwei für das Löschen)."""
        self._stopping.set()
        self.manager.shutdown(timeout)
        self.stop_purge(min(2.0, max(0.0, timeout)))
