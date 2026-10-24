"""Internet-Geschwindigkeit messen (E111, Teil P10; Bauplan docs/research/e103/SPEC.md 9.6).

Antwortzeit: Median aus PINGS TCP-Verbindungsaufbauten zum Messdienst. Download: STREAMS parallele
GET auf DOWN_URL (je 25 MiB, nach dem Ende neu), bis MAX_SECONDS oder MAX_BYTES; die erste
Sekunde zählt nicht (Anlauf, TCP-Fenster). STALL_S ohne Daten = „Die Verbindung ist abgerissen.“
Nichts wird hochgeladen. Alles im eigenen Thread (nie in der Hauptschleife), jederzeit abbrechbar.

Der Messdienst ist der offizielle von Cloudflare (offene Frage 11, Vorgabe). Im Prüfmodus gibt es
nur COUCH_SPEEDTEST_URL auf 127.0.0.1 (ein lokaler Testserver), sonst ist die Messung nicht
verfügbar; jede Verbindung prüft das noch einmal selbst (_guard), auch nach einer Weiterleitung
(die es ohnehin nicht gibt: Weiterleitungen gelten als Fehler). Keine Proxys aus der Umgebung.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ... import system
from . import test_mode

log = logging.getLogger("couch.settings.speedtest")

SERVER = "speed.cloudflare.com"
DOWN_URL = "https://speed.cloudflare.com/__down?bytes=26214400"
TEST_ENV = "COUCH_SPEEDTEST_URL"
TEST_HOST = "127.0.0.1"

MAX_SECONDS = 10.0
MAX_BYTES = 250 * 1000 * 1000
WARMUP_S = 1.0
STALL_S = 5.0
STREAMS = 2
PINGS = 3
PING_TIMEOUT_S = 3.0
CHUNK = 64 * 1024
TICK_S = 0.1
PING_SHARE = 5                       # Prozent der Anzeige für die Antwortzeit

FILE_NAME = "speedtest.json"

TEXT_TORN = "Die Verbindung ist abgerissen."
TEXT_UNREACHABLE = "Der Messdienst ist nicht erreichbar."
TEXT_TEST_MODE = "Im Prüfmodus nicht verfügbar."


class Refused(Exception):
    """Verbindung, die der Prüfmodus nicht erlaubt."""


def target() -> tuple[str, str, int] | None:
    """(Download-Adresse, Rechner, Port) der Messung; im Prüfmodus nur COUCH_SPEEDTEST_URL mit
    127.0.0.1 (http oder https), sonst None."""
    if not test_mode():
        return DOWN_URL, SERVER, 443
    raw = os.environ.get(TEST_ENV, "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.hostname != TEST_HOST or parts.username or parts.password:
        return None
    return raw, TEST_HOST, port or (443 if parts.scheme == "https" else 80)


def _guard(host: str | None) -> None:
    """Im Prüfmodus nur 127.0.0.1: zweite Sperre unter target(), an jeder Verbindung."""
    if not test_mode():
        return
    try:
        ok = host is not None and ipaddress.ip_address(host) == ipaddress.ip_address(TEST_HOST)
    except ValueError:
        ok = False
    if not ok:
        raise Refused("Prüfmodus: keine Verbindung zu %r" % host)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib-Schnittstelle
        raise urllib.error.HTTPError(req.full_url, code, "Weiterleitung abgelehnt", headers, fp)


class _GuardedHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        _guard(req.host.rsplit(":", 1)[0] if req.host else None)
        return super().http_open(req)


class _GuardedHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        _guard(req.host.rsplit(":", 1)[0] if req.host else None)
        return super().https_open(req)


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       _GuardedHTTP(), _GuardedHTTPS())


class Measurement:
    """Eine Messung. start() startet den eigenen Thread; on_progress(prozent) und on_done(ergebnis)
    kommen aus ihm. Ergebnis: {"ok": True, "down_mbps", "ping_ms", "bytes", "seconds", "at"},
    {"ok": False, "message"} oder {"ok": False, "cancelled": True}."""

    def __init__(self, url: str, host: str, port: int,
                 on_progress: Callable[[int], None] | None = None,
                 on_done: Callable[[dict], None] | None = None,
                 max_seconds: float | None = None, max_bytes: int | None = None,
                 warmup: float | None = None, stall: float | None = None,
                 streams: int | None = None, pings: int | None = None,
                 wall: Callable[[], float] = time.time):
        self.url, self.host, self.port = url, host, int(port)
        self.on_progress, self.on_done = on_progress, on_done
        self.max_seconds = MAX_SECONDS if max_seconds is None else float(max_seconds)
        self.max_bytes = MAX_BYTES if max_bytes is None else int(max_bytes)
        self.warmup = WARMUP_S if warmup is None else float(warmup)
        self.stall = STALL_S if stall is None else float(stall)
        self.streams = STREAMS if streams is None else int(streams)
        self.pings = PINGS if pings is None else int(pings)
        self.wall = wall
        self.percent = 0
        self.result: dict | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bytes = 0
        self._reserved = 0                  # Bytes, die die Ströme gerade lesen dürfen (Mengengrenze)
        self._full = threading.Event()      # max_bytes erreicht: die Messschleife wacht sofort auf
        self._full_at: float | None = None
        self._last_data: float | None = None
        self._alive = 0
        self._thread: threading.Thread | None = None
        self._opener = _opener()

    # ------------------------------------------------------------ Steuerung
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="couch-speedtest", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread is not None:
            self._thread.join(timeout)
        return self.result is not None

    # ------------------------------------------------------------ Ablauf (eigener Thread)
    def _run(self) -> None:
        try:
            result = self._measure()
        except Refused:
            result = {"ok": False, "message": TEXT_TEST_MODE}
        except Exception:
            log.exception("Geschwindigkeit messen")
            result = {"ok": False, "message": TEXT_UNREACHABLE}
        self._stop.set()                    # die Download-Threads enden mit dem nächsten Block
        self.result = result
        if result.get("ok"):
            log.info("Geschwindigkeit: %.1f Mbit/s, Antwortzeit %s ms, %d Bytes in %.1f s", result["down_mbps"],
                     result["ping_ms"], result["bytes"], result["seconds"])
        else:
            log.info("Geschwindigkeit messen: %s", result.get("message") or "abgebrochen")
        if self.on_done is not None:
            try:
                self.on_done(result)
            except Exception:
                log.exception("Ende der Messung melden")

    def _progress(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent == self.percent:
            return
        self.percent = percent
        if self.on_progress is not None:
            try:
                self.on_progress(percent)
            except Exception:
                log.exception("Stand der Messung melden")

    def _ping(self) -> float | None:
        """Median der Verbindungsaufbauten in ms; None, wenn keiner gelang."""
        times = []
        for _ in range(max(1, self.pings)):
            if self._stop.is_set():
                return None
            _guard(self.host)
            began = time.monotonic()
            try:
                socket.create_connection((self.host, self.port), timeout=PING_TIMEOUT_S).close()
            except OSError:
                continue
            times.append((time.monotonic() - began) * 1000.0)
        return statistics.median(times) if times else None

    def _stream(self) -> None:
        """Ein Download-Strom: GET, lesen, nach dem Ende von vorn; endet bei Stopp oder Fehler."""
        req = urllib.request.Request(self.url, headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"})
        try:
            while not self._stop.is_set():
                with self._opener.open(req, timeout=self.stall) as resp:
                    if getattr(resp, "status", 200) != 200:
                        return
                    while not self._stop.is_set():
                        # Nachbesserung E111: nie mehr als max_bytes lesen, auch zwischen zwei Takten der
                        # Messschleife (sonst bis zu TICK_S Durchsatz zu viel, bei 2,5 Gbit/s rund 30 MB)
                        with self._lock:
                            want = min(CHUNK, self.max_bytes - self._bytes - self._reserved)
                            self._reserved += max(0, want)
                        if want <= 0:
                            return
                        try:
                            chunk = resp.read(want)
                        finally:
                            with self._lock:
                                self._reserved -= want
                        if not chunk:
                            break
                        with self._lock:
                            self._bytes += len(chunk)
                            self._last_data = time.monotonic()
                            if self._bytes >= self.max_bytes and not self._full.is_set():
                                self._full_at = self._last_data
                                self._full.set()
        except Refused:
            log.warning("Prüfmodus: Download zu %s abgelehnt", self.url)
        except (OSError, urllib.error.URLError, ValueError) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()                       # die Antwort des Fehlers gehört uns
            if not self._stop.is_set():
                log.info("Download-Strom endet: %s", e)
        finally:
            with self._lock:
                self._alive -= 1

    def _measure(self) -> dict:
        _guard(self.host)
        ping = self._ping()
        if self._stop.is_set():
            return {"ok": False, "cancelled": True}
        if ping is None:
            return {"ok": False, "message": TEXT_UNREACHABLE}
        self._progress(PING_SHARE)
        began = time.monotonic()
        with self._lock:
            self._alive = max(1, self.streams)
        for i in range(max(1, self.streams)):
            threading.Thread(target=self._stream, name="couch-speedtest-%d" % i, daemon=True).start()
        warm: tuple[float, int] | None = None
        while True:
            self._full.wait(TICK_S)
            if self._stop.is_set():
                return {"ok": False, "cancelled": True}
            now = time.monotonic()
            with self._lock:
                total, last, alive = self._bytes, self._last_data, self._alive
                if self._full_at is not None:
                    now = self._full_at             # Ende der Messung = der letzte erlaubte Block
            elapsed = now - began
            if warm is None and elapsed >= self.warmup:
                warm = (now, total)
            share = max(elapsed / self.max_seconds, total / self.max_bytes) if self.max_bytes else 1.0
            self._progress(PING_SHARE + int((99 - PING_SHARE) * min(1.0, share)))     # 100 erst am Ende
            if total >= self.max_bytes or elapsed >= self.max_seconds:
                break
            if now - (last if last is not None else began) >= self.stall:
                return {"ok": False, "message": TEXT_TORN}
            if alive <= 0:
                return {"ok": False, "message": TEXT_TORN if total else TEXT_UNREACHABLE}
        self._stop.set()
        if total <= 0:
            return {"ok": False, "message": TEXT_UNREACHABLE}
        # Ohne Anlauf: ab der ersten Sekunde; war die Messung so kurz, dass danach kaum etwas bleibt
        # (250 MB in gut einer Sekunde), zählt die ganze Zeit
        if warm is not None and now - warm[0] >= 2 * TICK_S and total > warm[1]:
            spent, counted = now - warm[0], total - warm[1]
        else:
            spent, counted = max(elapsed, TICK_S), total
        mbps = counted * 8 / spent / 1e6
        self._progress(100)
        return {"ok": True, "down_mbps": round(mbps, 2), "ping_ms": round(ping, 1), "bytes": total,
                "seconds": round(elapsed, 2), "at": round(self.wall(), 1)}


# ------------------------------------------------------------ Ablage und Texte
def load(state_dir: str) -> dict | None:
    """Letzte gelungene Messung aus <state>/speedtest.json (oder None)."""
    try:
        with open(os.path.join(state_dir, FILE_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("down_mbps", "at"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return None
    ping = data.get("ping_ms")
    if ping is not None and (isinstance(ping, bool) or not isinstance(ping, (int, float)) or ping < 0):
        return None
    return {"down_mbps": float(data["down_mbps"]), "ping_ms": None if ping is None else float(ping),
            "at": float(data["at"])}


def save(state_dir: str, result: dict) -> None:
    """Ergebnis ablegen (Mess-Thread). Gemerkt wird nur, was die Seite zeigt."""
    data = {"down_mbps": result["down_mbps"], "ping_ms": result.get("ping_ms"), "at": result["at"],
            "bytes": result.get("bytes"), "seconds": result.get("seconds")}
    system.write_atomic(os.path.join(state_dir, FILE_NAME), json.dumps(data) + "\n")


def _de(value: float, digits: int) -> str:
    """Zahl mit deutschem Komma und Tausenderpunkt."""
    text = f"{value:,.{digits}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def mbps_text(value: float) -> str:
    return _de(value, 1) + " Mbit/s"


def ping_text(value: float | None) -> str:
    if value is None:
        return "Unbekannt"
    return "%d ms" % max(1, round(value))


def when_text(at: float, now: float | None = None) -> str:
    """„heute, 21:14“, „gestern, 21:14“, sonst „12.10.2026, 21:14“ (Ortszeit)."""
    now = time.time() if now is None else now
    day = time.localtime(at)
    today = time.localtime(now)
    clock = time.strftime("%H:%M", day)
    if day[:3] == today[:3]:
        return "heute, " + clock
    yesterday = time.localtime(time.mktime((today.tm_year, today.tm_mon, today.tm_mday - 1, 12, 0, 0, 0, 0, -1)))
    if day[:3] == yesterday[:3]:
        return "gestern, " + clock
    return time.strftime("%d.%m.%Y", day) + ", " + clock
