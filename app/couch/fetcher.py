"""Downloads der Konsole (Bauplan E60): anhalten, fortsetzen, abbrechen, über Neustarts hinweg.

Wunsch 2026-10-18: „Der Download ist stehen geblieben, ich möchte stoppen, fortsetzen und
pausieren haben, damit es einen Neustart überlebt.“ WebKit kann einen Download weder anhalten
noch fortsetzen. Darum übergibt der Browser jeden http(s)-Download an das Home (Auftragsdatei in
<state>/download-requests/, browser/downloads.write_request), und das Home lädt selbst: HTTP mit
Range in <ziel>.part, fertig wird umbenannt. Den Stand schreibt allein das Home nach
<state>/downloads.json (Rechte 0600: die Kopfzeilen können Cookies enthalten).

Zustände: queued (wartet auf einen freien Platz), running, paused, done, failed, cancelled.
- Beim Start des Home laufen queued und running weiter; paused bleibt angehalten.
- Kommen STALL_S keine Daten, wird die Verbindung neu aufgebaut und ab dem Stand fortgesetzt
  (RETRIES Versuche mit Pause dazwischen), danach failed; „Fortsetzen“ geht auch dann weiter.
- Antwortet der Server auf Range nicht mit 206, beginnt der Download von vorn (resumable=False).
- Abbrechen löscht die eigene .part-Datei; Entfernen nimmt nur den Eintrag aus der Liste.

Nur Standardbibliothek (urllib); ein Thread je laufendem Download, höchstens MAX_PARALLEL. In die
Hauptschleife geht nur der Stand (post). Einträge aus dem Browser vor E60 (ohne Kopfzeilen, ohne
.part) lassen sich nicht fortsetzen: Sie werden als „fehlgeschlagen“ übernommen.

Cookies gehen bei einer Umleitung nur an denselben Rechnernamen mit und nie von https auf http,
der Referer ebenfalls nicht von https auf http (E99): urllib reicht sonst alle Kopfzeilen an
jedes Umleitungsziel weiter.

Was WebKit selbst lädt (blob:, data:, POST), führt der Browser in <state>/browser-downloads.json
(E99). Das Home liest die Datei nur und hängt die Einträge an sys.state.downloads an: ohne
Anhalten und Fortsetzen ("local": True), „Entfernen“ blendet sie nur aus. Lebt der Browser
eines laufenden Eintrags nicht mehr (PID samt Startzeit), gilt der Eintrag als abgebrochen.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.client import HTTPException
from typing import Any, Callable

from . import jsontext, log
from .browser import downloads as names

_log = log.get("fetcher")

STATES = ("queued", "running", "paused", "done", "failed", "cancelled")
ACTIVE = ("queued", "running")
MAX_PARALLEL = 2
CHUNK = 1 << 20
STALL_S = 45.0                  # so lange ohne Daten gilt eine Verbindung als hängend
RETRIES = 6
RETRY_WAIT = (5, 15, 30, 60, 120, 300)
SAVE_GAP = 1.0                  # Stand höchstens so oft schreiben und melden (Sekunden)
POLL_S = 1.0                    # Auftragsordner des Browsers
KEEP = 30
HEADER_NAMES = ("Cookie", "Referer", "User-Agent", "Accept-Language")
HEADER_VALUE = re.compile(r"^[\x20-\x7e\x80-\xff]{0,8192}$")

TEXT_LEGACY = "Vom alten Browser begonnen; bitte im Browser neu laden."
TEXT_NO_SPACE = "Zu wenig Platz im Download-Ordner."
TEXT_REFUSED = "Der Server gibt die Datei nicht mehr heraus (%s). Der Link ist vielleicht abgelaufen."
TEXT_GAVE_UP = "Die Verbindung bricht immer wieder ab. Mit „Fortsetzen“ geht es an derselben Stelle weiter."
TEXT_LOCAL = "Diesen Download lädt der Browser selbst. Anhalten und Fortsetzen geht nur, wenn die Konsole lädt."
TEXT_LOCAL_RUNNING = "Dieser Download läuft noch im Browser."


def _origin(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    scheme = (parts.scheme or "").lower()
    return scheme, (parts.hostname or "").lower(), port or (443 if scheme == "https" else 80)


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Umleitung ohne fremde Cookies (E99): Der Browser gibt die Cookies der ersten Adresse mit;
    urllib kopierte sie an jedes Ziel, auch an einen anderen Anbieter und von https auf http.
    Wie im Browser hängen Cookies am Rechnernamen, nicht am Port: http -> https auf demselben
    Rechner behält sie (sonst wurde aus dem Upgrade ein endgültiges 403)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_origin, new_origin = _origin(req.full_url), _origin(new.full_url)
            downgrade = old_origin[0] == "https" and new_origin[0] != "https"
            if old_origin[1] != new_origin[1] or downgrade:
                new.remove_header("Cookie")
            if downgrade:
                new.remove_header("Referer")
        return new


_OPENER = urllib.request.build_opener(_RedirectHandler)


def clean_headers(raw: Any) -> dict:
    out = {}
    if isinstance(raw, dict):
        for key in HEADER_NAMES:
            value = raw.get(key)
            if isinstance(value, str) and value and HEADER_VALUE.match(value) and "\n" not in value and "\r" not in value:
                out[key] = value
    return out


def parse_total(headers: Any, offset: int, status: int) -> int:
    """Gesamtgröße aus Content-Range (206) bzw. Content-Length; 0 = unbekannt."""
    rng = headers.get("Content-Range") if headers is not None else None
    if status == 206 and rng:
        m = re.match(r"bytes\s+\d+-\d+/(\d+)", rng)
        if m:
            return int(m.group(1))
    try:
        length = int(headers.get("Content-Length") or 0) if headers is not None else 0
    except (TypeError, ValueError):
        length = 0
    return offset + length if length > 0 else 0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _setup_name(name: Any) -> bool:
    """E114: Sagt der Name „Installationsprogramm“ (setup_*.exe, *-setup.exe, .msi …)? Nur der Name: view()
    läuft unter dem Schloss und oft; ob es wirklich Inno Setup, NSIS oder MSI ist, sagt install.probe."""
    try:
        from .installer.setupexe import is_setup_name
        return is_setup_name(str(name or ""))
    except Exception:  # noqa: BLE001
        return False


class _Job:
    def __init__(self):
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.why = ""                  # "pause" | "cancel" | "close"


class Fetcher:
    def __init__(self, list_path: str, spool_dir: str, download_dir: str,
                 later: Callable[[float, Callable[[], None]], Any], post: Callable[..., None],
                 state: Any = None, notify: Any = None, opener: Callable | None = None,
                 clock: Callable[[], float] = time.time, retry_wait: tuple = RETRY_WAIT,
                 stall_s: float = STALL_S, max_parallel: int = MAX_PARALLEL, local_path: str | None = None):
        self.list_path = list_path
        self.spool_dir = spool_dir
        self.download_dir = download_dir
        self.later = later
        self.post = post
        self.state = state
        self.notify = notify
        self.open = opener or _OPENER.open
        self.clock = clock
        self.retry_wait = tuple(retry_wait) or RETRY_WAIT
        self.stall_s = stall_s
        self.max_parallel = max_parallel
        self.items: list[dict] = []
        self.jobs: dict[str, _Job] = {}
        self.lock = threading.RLock()
        self.closed = False
        self._saved = 0.0
        self._speed: dict[str, tuple[float, int, float]] = {}   # id -> (zeit, bytes, bytes/s)
        self.local_path = local_path            # Downloads, die WebKit selbst lädt (nur lesen)
        self.local: list[dict] = []
        self.hidden: set[str] = set()           # ausgeblendete Einträge des Browsers
        self._local_sig: tuple | None = None

    # ------------------------------------------------------------ Start und Ende
    def start(self) -> None:
        with self.lock:
            self.items = self._load()
            for item in self.items:
                if item["state"] in ACTIVE:
                    if not self._resumable_record(item):
                        item.update(state="failed", error=TEXT_LEGACY, finished=self.clock())
                    else:
                        item["state"] = "queued"     # nach dem Neustart weiter
            self._save(force=True)
        self.watch_local(tell=False)            # nach _load: dort steht, was ausgeblendet ist
        self._pump()
        self._tick()

    def stop(self) -> None:
        """Ende des Home: Verbindungen schließen, Zustand bleibt (läuft beim nächsten Start weiter)."""
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
            for job in jobs:
                job.why = "close"
                job.stop.set()
        for job in jobs:
            if job.thread is not None:
                job.thread.join(timeout=3)
        with self.lock:
            self._save(force=True)

    def _tick(self) -> None:
        if self.closed:
            return
        try:
            self.take_requests()
            self.watch_local()
        except Exception:
            _log.exception("Download-Aufträge")
        finally:
            if not self.closed:
                self.later(POLL_S, self._tick)

    # ------------------------------------------------------------ Aufträge des Browsers
    def take_requests(self) -> int:
        try:
            files = sorted(f for f in os.listdir(self.spool_dir) if f.endswith(".json"))
        except OSError:
            return 0
        n = 0
        for name in files:
            path = os.path.join(self.spool_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
            except (OSError, ValueError):
                req = None
            try:
                os.unlink(path)
            except OSError:
                pass
            if isinstance(req, dict) and self.add(req.get("url"), req.get("name"), req.get("headers")):
                n += 1
        return n

    def add(self, url: Any, name: Any = "", headers: Any = None) -> str | None:
        url = str(url or "")
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            _log.warning("Download-Auftrag ohne http(s)-Adresse übergangen")
            return None
        if not name:
            name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)) or "Download"
        with self.lock:
            try:
                os.makedirs(self.download_dir, exist_ok=True)
            except OSError as e:
                _log.warning("Download-Ordner %s: %s", self.download_dir, e)
            path = self._free_path(str(name))
            item = {"id": uuid.uuid4().hex[:12], "name": os.path.basename(path), "path": path, "url": url,
                    "headers": clean_headers(headers), "received": 0, "total": 0, "state": "queued",
                    "error": "", "started": self.clock(), "finished": 0, "resumable": True}
            self.items.insert(0, item)
            self._trim()
            self._save(force=True)
        _log.info("Download übernommen: %s", item["name"])
        self._pump()
        return item["id"]

    def _free_path(self, name: str) -> str:
        """Freier Name im Download-Ordner: weder die Datei noch ihre .part noch ein Eintrag. Auch
        abgebrochene und fehlgeschlagene Einträge belegen ihren Pfad (E99), denn „Neu laden“ und
        „Erneut versuchen“ schreiben wieder dorthin; vorher teilten sich zwei Downloads eine .part."""
        taken = {i["path"] for i in self.items if i["state"] != "done"}
        stem, ext = names.split_ext(names.safe_name(name))
        n = 1
        while True:
            candidate = os.path.join(self.download_dir, (stem if n == 1 else "%s (%d)" % (stem, n)) + ext)
            if candidate not in taken and not os.path.lexists(candidate) and not os.path.lexists(candidate + ".part"):
                return candidate
            n += 1

    # ------------------------------------------------------------ Bedienung (RPC)
    def pause(self, item_id: str) -> dict:
        with self.lock:
            if self._local_item(item_id) is not None:
                return {"ok": False, "message": TEXT_LOCAL}
            item = self._get(item_id)
            if item is None or item["state"] not in ACTIVE:
                return {"ok": False, "message": "Dieser Download läuft nicht."}
            item["state"] = "paused"
            self._stop_job(item_id, "pause")
            self._save(force=True)
        self._pump()
        return {"ok": True}

    def resume(self, item_id: str) -> dict:
        with self.lock:
            if self._local_item(item_id) is not None:
                return {"ok": False, "message": TEXT_LOCAL}
            item = self._get(item_id)
            if item is None or item["state"] in ACTIVE or item["state"] == "done":
                return {"ok": False, "message": "Dieser Download lässt sich nicht fortsetzen."}
            if not self._resumable_record(item):
                return {"ok": False, "message": TEXT_LEGACY}
            if self._shared(item):
                # Liste von vor E99: Ein anderer, nicht fertiger Eintrag schreibt in denselben Pfad.
                # Eigener Pfad, von vorn (wem die .part gehört, ist nicht zu sagen)
                path = self._free_path(item["name"])
                item.update(path=path, name=os.path.basename(path), received=0)
            if item["state"] == "cancelled":
                item["received"] = 0
            item.update(state="queued", error="", finished=0)
            self._save(force=True)
        self._pump()
        return {"ok": True}

    def cancel(self, item_id: str) -> dict:
        with self.lock:
            if self._local_item(item_id) is not None:
                return {"ok": False, "message": TEXT_LOCAL}
            item = self._get(item_id)
            if item is None or item["state"] in ("done", "cancelled"):
                return {"ok": False, "message": "Dieser Download ist schon zu Ende."}
            was_running = item_id in self.jobs
            item.update(state="cancelled", error="Abgebrochen.", finished=self.clock(), received=0)
            self._stop_job(item_id, "cancel")
            if not was_running:
                self._drop_part(item)
            self._save(force=True)
        self._pump()
        return {"ok": True}

    def remove(self, item_id: str) -> dict:
        with self.lock:
            local = self._local_item(item_id)
            if local is not None:
                if local["state"] == "running":
                    return {"ok": False, "message": TEXT_LOCAL_RUNNING}
                self.hidden.add(item_id)        # die Datei des Browsers schreibt nur der Browser
                self._save(force=True)
                return {"ok": True}
            item = self._get(item_id)
            if item is None:
                return {"ok": False, "message": "Diesen Download gibt es nicht mehr."}
            if item["state"] in ACTIVE or item_id in self.jobs:
                return {"ok": False, "message": "Erst anhalten oder abbrechen."}
            if item["state"] in ("paused", "failed"):
                self._drop_part(item)
            self.items.remove(item)
            self._save(force=True)
        return {"ok": True}

    # ------------------------------------------------------------ Abarbeiten
    def _pump(self) -> None:
        with self.lock:
            if self.closed:
                return
            running = sum(1 for i in self.items if i["id"] in self.jobs)
            for item in reversed(self.items):          # älteste zuerst
                if running >= self.max_parallel:
                    break
                if item["state"] == "queued" and item["id"] not in self.jobs:
                    job = _Job()
                    self.jobs[item["id"]] = job
                    item["state"] = "running"
                    job.thread = threading.Thread(target=self._run, args=(item["id"], job),
                                                  name="couch-download", daemon=True)
                    job.thread.start()
                    running += 1
            self._save(force=True)

    def _stop_job(self, item_id: str, why: str) -> None:
        job = self.jobs.get(item_id)
        if job is not None:
            job.why = why
            job.stop.set()

    def _run(self, item_id: str, job: _Job) -> None:
        attempt = 0
        try:
            while not job.stop.is_set():
                try:
                    if self._fetch(item_id, job):
                        return
                    attempt = 0                         # es kam etwas an: neue Versuche
                except _Fatal as e:
                    self._end(item_id, "failed", str(e))
                    return
                except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                        HTTPException, OSError) as e:
                    if job.stop.is_set():
                        break
                    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
                        self._end(item_id, "failed", TEXT_NO_SPACE)
                        return
                    attempt += 1
                    _log.info("Download %s: Verbindung weg (%s), Versuch %d", item_id, e, attempt)
                    if attempt > RETRIES:
                        self._end(item_id, "failed", TEXT_GAVE_UP)
                        return
                    job.stop.wait(self.retry_wait[min(attempt, len(self.retry_wait)) - 1])
        finally:
            self._job_ended(item_id, job)

    def _fetch(self, item_id: str, job: _Job) -> bool:
        """Ein Verbindungsversuch. True: fertig. False: Verbindung lieferte Daten und endete zu früh."""
        with self.lock:
            item = self._get(item_id)
            if item is None:
                return True
            url, final, headers = item["url"], item["path"], dict(item.get("headers") or {})
        part = final + ".part"
        offset = os.path.getsize(part) if os.path.isfile(part) else 0
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        request = urllib.request.Request(url, headers=headers)
        try:
            resp = self.open(request, timeout=self.stall_s)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 416 and offset:
                with self.lock:
                    item = self._get(item_id)
                    total = item.get("total", 0) if item else 0
                if total and offset >= total:
                    return self._complete(item_id, part, final, offset, total)
                os.truncate(part, 0)                     # Stand passt nicht zur Datei: von vorn
                raise ConnectionError("416 ohne passende Größe, beginne von vorn")
            if e.code in (401, 403, 404, 410):
                raise _Fatal(TEXT_REFUSED % e.code)
            raise
        with resp:
            status = getattr(resp, "status", 200)
            if offset and status != 206:
                offset = 0                               # kein Range: von vorn
                with self.lock:
                    item = self._get(item_id)
                    if item is not None:
                        item["resumable"] = False
            total = parse_total(resp.headers, offset, status)
            self._space(final, total - offset if total else 0)
            with self.lock:
                item = self._get(item_id)
                if item is not None:
                    item.update(received=offset, total=total or item.get("total", 0))
                    self._save(force=True)
            got_any = False
            with open(part, "ab" if offset else "wb") as f:
                while not job.stop.is_set():
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    offset += len(chunk)
                    got_any = True
                    self._progress(item_id, offset)
            if job.stop.is_set():
                self._progress(item_id, offset, force=True)
                return True
        if total and offset < total:
            self._progress(item_id, offset, force=True)
            if got_any:
                return False
            raise ConnectionError("Verbindung endete ohne Daten")
        return self._complete(item_id, part, final, offset, total)

    def _space(self, final: str, need: int) -> None:
        if need <= 0:
            return
        try:
            free = shutil.disk_usage(os.path.dirname(final) or ".").free
        except OSError:
            return
        if free < need + 256 * 1024 * 1024:
            raise _Fatal(TEXT_NO_SPACE)

    def _complete(self, item_id: str, part: str, final: str, size: int, total: int) -> bool:
        with self.lock:
            item = self._get(item_id)
            if item is None:
                return True
            target = final
            if os.path.lexists(target):
                stem, ext = names.split_ext(os.path.basename(final))
                n = 2
                while os.path.lexists(target):
                    target = os.path.join(os.path.dirname(final), "%s (%d)%s" % (stem, n, ext))
                    n += 1
            os.replace(part, target)
            item.update(path=target, name=os.path.basename(target), received=size, total=total or size)
        self._end(item_id, "done", "")
        return True

    def _progress(self, item_id: str, received: int, force: bool = False) -> None:
        with self.lock:
            item = self._get(item_id)
            if item is None or item["state"] != "running":
                return                                  # angehalten oder abgebrochen: Stand bleibt
            item["received"] = received
            now = time.monotonic()
            last = self._speed.get(item_id)
            if last is None:
                self._speed[item_id] = (now, received, 0.0)
            elif now - last[0] >= 2.0:
                rate = max(0.0, (received - last[1]) / (now - last[0]))
                self._speed[item_id] = (now, received, rate if last[2] == 0 else 0.6 * rate + 0.4 * last[2])
            self._save(force=force)

    def _end(self, item_id: str, state: str, error: str) -> None:
        with self.lock:
            item = self._get(item_id)
            if item is None or item["state"] not in ACTIVE:
                return
            item.update(state=state, error=error, finished=self.clock())
            self._speed.pop(item_id, None)
            self._save(force=True)
        _log.info("Download %s: %s %s", item["name"], state, error)
        if self.notify is not None:
            def tell() -> None:
                try:
                    if state == "done":
                        body = ("Im Control Center unter Downloads lässt es sich installieren."
                                if names.is_archive(item["name"]) else "Liegt im Download-Ordner.")
                        self.notify.push("Download fertig: %s" % item["name"], body, icon="download")
                    else:
                        self.notify.push("Download fehlgeschlagen: %s" % item["name"], error, icon="warn")
                except Exception:
                    _log.exception("Mitteilung zum Download")
            self.post(tell)

    def _job_ended(self, item_id: str, job: _Job) -> None:
        with self.lock:
            if self.jobs.get(item_id) is job:
                del self.jobs[item_id]
            item = self._get(item_id)
            if item is not None and job.why == "cancel":
                self._drop_part(item)
            elif item is not None and item["state"] in ("paused", "failed", "running"):
                part = item["path"] + ".part"
                if os.path.isfile(part):
                    item["received"] = os.path.getsize(part)    # genauer Stand für die Anzeige
            self._speed.pop(item_id, None)
            self._save(force=True)
        if job.why != "close":
            self._pump()

    # ------------------------------------------------------------ Liste
    def _get(self, item_id: str) -> dict | None:
        return next((i for i in self.items if i["id"] == item_id), None)

    @staticmethod
    def _resumable_record(item: dict) -> bool:
        """Einträge aus dem Browser vor E60 haben keine Kopfzeilen und keine .part-Datei."""
        return "headers" in item and bool(item.get("url"))

    def _shared(self, item: dict) -> bool:
        """Schreibt ein anderer, nicht fertiger Eintrag in denselben Pfad? (nur Listen von vor E99)
        Fertige zählen nicht: Ihre .part ist umbenannt, und _free_path gibt ihren Pfad wieder her,
        sobald die Datei weg ist (verschoben, gelöscht). Sonst lud „Fortsetzen“ von vorn unter
        „Name (2)“, und Abbrechen ließ GB-große .part-Dateien liegen."""
        return any(o is not item and o.get("state") != "done" and o.get("path") == item.get("path")
                   for o in self.items)

    def _drop_part(self, item: dict) -> None:
        """Löscht nur die eigene Teildatei <ziel>.part, und nur im Download-Ordner. Teilt sich der
        Pfad mit einem anderen Eintrag (Liste von vor E99), bleibt sie: Sie kann dessen sein."""
        if self._shared(item):
            return
        part = str(item.get("path") or "") + ".part"
        inside = os.path.realpath(os.path.dirname(part)) == os.path.realpath(self.download_dir)
        try:
            if inside and os.path.isfile(part) and not os.path.islink(part):
                os.unlink(part)
        except OSError as e:
            _log.warning("%s nicht gelöscht: %s", part, e)

    def _trim(self) -> None:
        keep, n = [], 0
        for item in self.items:
            if item["state"] in ("done", "failed", "cancelled"):
                n += 1
                if n > KEEP:
                    continue
            keep.append(item)
        self.items = keep

    def _load(self) -> list[dict]:
        try:
            with open(self.list_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        hidden = data.get("hidden_local") if isinstance(data, dict) else None
        self.hidden = {h for h in hidden if isinstance(h, str)} if isinstance(hidden, list) else set()
        out = []
        for item in data.get("items") if isinstance(data, dict) and isinstance(data.get("items"), list) else []:
            if isinstance(item, dict) and item.get("id") and item.get("state") in STATES and item.get("path"):
                item.setdefault("received", 0)
                item.setdefault("total", 0)
                item.setdefault("error", "")
                out.append(item)
        return out

    # ------------------------------------------------------------ Downloads des Browsers (E99)
    def _local_item(self, item_id: str) -> dict | None:
        return next((i for i in self.local if i["id"] == item_id and i["id"] not in self.hidden), None)

    def watch_local(self, tell: bool = True) -> bool:
        """Liest browser-downloads.json neu, wenn sie sich geändert hat, und meldet den Stand.
        tell: Mitteilung, wenn ein Download des Browsers fertig wird oder scheitert (nicht beim Start)."""
        if not self.local_path:
            return False
        try:
            st = os.stat(self.local_path)
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig == self._local_sig:
            with self.lock:
                if not self._reap_local():
                    return False
                self._publish()
            return True
        self._local_sig = sig
        fresh = names.read_local(self.local_path) if sig is not None else []
        with self.lock:
            before = {i["id"]: i["state"] for i in self.local}
            self.local = fresh
            self._reap_local()
            if fresh:                           # Vergessenes nicht ewig mitführen (leer: Datei kaputt?)
                self.hidden &= {i["id"] for i in fresh}
            ended = [i for i in fresh if i["state"] in ("done", "failed") and before.get(i["id"]) != i["state"]
                     and i["id"] not in self.hidden]
            self._publish()
        if tell and self.notify is not None:
            for item in ended:
                self.post(self._tell_local, item)
        return True

    def _reap_local(self) -> bool:
        """Unter self.lock. „running“ eines Browsers, der nicht mehr lebt (Absturz, SIGKILL, Strom
        weg), gilt in der eigenen Sicht als abgebrochen (E99); die Datei schreibt weiter nur der
        Browser. Sonst sperrte der Eintrag bis zum nächsten Browserstart Ruhemodus und
        Bildschirmschoner („Ein Download läuft.“) und ließ sich nicht entfernen."""
        changed = False
        for item in self.local:
            if item["state"] == "running" and names.owner_gone(item):
                item.update(state="cancelled", error=names.TEXT_ENDED, finished=self.clock())
                changed = True
        return changed

    def _tell_local(self, item: dict) -> None:
        try:
            if item["state"] == "done":
                body = ("Im Control Center unter Downloads lässt es sich installieren."
                        if names.is_archive(item["name"]) else "Liegt im Download-Ordner.")
                self.notify.push("Download fertig: %s" % item["name"], body, icon="download")
            else:
                self.notify.push("Download fehlgeschlagen: %s" % item["name"], item.get("error") or "", icon="warn")
        except Exception:
            _log.exception("Mitteilung zum Download des Browsers")

    def view(self) -> list[dict]:
        with self.lock:
            out = []
            for item in self.local:
                if item["id"] in self.hidden:
                    continue
                total, received = item["total"], item["received"]
                percent = 100 if item["state"] == "done" else (min(100, received * 100 // total) if total > 0 else 0)
                out.append({"id": item["id"], "name": item["name"], "path": item["path"], "state": item["state"],
                            "received": received, "total": total, "percent": int(percent), "speed": 0,
                            "error": item["error"], "finished": item["finished"], "resumable": False,
                            "archive": names.is_archive(item["name"]), "installer": _setup_name(item["name"]),
                            "local": True, "started": item["started"]})
            for item in self.items:
                total, received = int(item.get("total") or 0), int(item.get("received") or 0)
                percent = 100 if item["state"] == "done" else (min(100, received * 100 // total) if total > 0 else 0)
                speed = self._speed.get(item["id"], (0, 0, 0.0))[2] if item["state"] == "running" else 0.0
                out.append({"id": item["id"], "name": item["name"], "path": item["path"], "state": item["state"],
                            "received": received, "total": total, "percent": int(percent),
                            "speed": int(speed), "error": item.get("error") or "",
                            "finished": item.get("finished") or 0, "resumable": item.get("resumable", True) is not False,
                            "archive": names.is_archive(item["name"]), "installer": _setup_name(item["name"]),
                            "local": False,
                            "started": _number(item.get("started"))})
            out.sort(key=lambda i: i["started"], reverse=True)          # neueste zuerst, beide Quellen
            return out

    def _save(self, force: bool = False) -> None:
        """Unter self.lock. Schreibt die Liste und meldet den Stand (gedrosselt)."""
        now = time.monotonic()
        if not force and now - self._saved < SAVE_GAP:
            return
        self._saved = now
        try:
            os.makedirs(os.path.dirname(self.list_path) or ".", exist_ok=True)
            tmp = "%s.%d.neu" % (self.list_path, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps({"version": 2, "items": self.items, "hidden_local": sorted(self.hidden)},
                                       indent=1))
                f.write("\n")
            os.replace(tmp, self.list_path)
        except OSError as e:
            _log.error("downloads.json nicht schreibbar: %s", e)
        self._publish()

    def _publish(self) -> None:
        """Unter self.lock: Stand nach sys.state.downloads (eigene und die des Browsers)."""
        if self.state is not None:
            view = self.view()
            self.post(lambda: self.state.update(downloads=view))


class _Fatal(Exception):
    """Hier hilft kein neuer Versuch (Server lehnt ab, Platte voll)."""
