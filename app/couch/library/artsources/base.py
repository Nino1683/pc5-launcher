"""Gemeinsames der Bilder-Quellen: der eine HTTP-Zugang, Fehlerarten, die Form einer Quelle.

Regeln des HTTP-Zugangs (für alle Quellen gleich, nirgends sonst wird ins Netz gegriffen):
  * nur https und nur Rechnernamen, die eine Quelle angemeldet hat; http ausschließlich zu
    127.0.0.0/8 bzw. localhost (dort laufen die Attrappen der Tests)
  * Weiterleitungen werden nicht blind verfolgt: Das Ziel muss wieder ein erlaubter Rechner sein
  * Zeitlimit je Verbindung und je Antwort, Größenbegrenzung schon beim Lesen
  * eine Anfrage zur Zeit im ganzen Prozess, je Dienst ein höflicher Abstand
  * im Prüfmodus (COUCH_TEST_ROOT) keine Anfrage an echte Rechner, außer COUCH_ART_NET=1
  * Kopfzeilen (Schlüssel!) stehen nie in Fehlertexten oder im Protokoll

Alles hier BLOCKIERT und gehört in einen Hintergrund-Thread.
"""
from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from urllib.parse import urljoin, urlsplit

USER_AGENT = "couch-konsolenmodus/0.1 (privates Heimprojekt; Cover-Abruf fuer die eigene Spielebibliothek)"

TIMEOUT = 6.0                      # Sekunden für Verbindungsaufbau und jede Lesepause
TOTAL_TIMEOUT = 45.0               # Obergrenze für eine ganze Antwort (tröpfelnde Verbindung)
MAX_BYTES = 15 * 1024 * 1024       # je Bild
MAX_JSON_BYTES = 2 * 1024 * 1024
MIN_INTERVAL = 1.0                 # höflicher Abstand zwischen zwei Anfragen an denselben Dienst
MAX_REDIRECTS = 3

JSON_TYPES = ("application/json", "text/json", "text/javascript")
IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png")

# Eine Anfrage zur Zeit, auch wenn mehrere Abrufer existieren
_NET_LOCK = threading.Lock()


class FetchError(Exception):
    """Abruf gescheitert. transient = später noch einmal versuchen (Netz, Zeitlimit, Serverfehler)."""

    def __init__(self, reason: str, transient: bool = False, detail: str = ""):
        super().__init__(reason + (": " + detail if detail else ""))
        self.reason = reason
        self.transient = transient
        self.detail = detail


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def real_net_allowed() -> bool:
    """Im Prüfmodus bleiben echte Dienste unberührt (Prüfläufe mit erfundenen Titeln sollen
    keinen Shop abfragen). COUCH_ART_NET=1 hebt das für bewusste Läufe auf."""
    return not os.environ.get("COUCH_TEST_ROOT") or os.environ.get("COUCH_ART_NET") == "1"


class Http:
    """Der einzige Weg ins Netz. Ein Objekt je Abrufer; die Sperre gilt prozessweit."""

    def __init__(self, session=None, *, timeout: float = TIMEOUT, total_timeout: float = TOTAL_TIMEOUT,
                 min_interval: float = MIN_INTERVAL, image_interval: float | None = None,
                 user_agent: str | None = USER_AGENT, sleep=time.sleep, monotonic=time.monotonic):
        self._session = session
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.min_interval = min_interval
        self.image_interval = min_interval if image_interval is None else image_interval
        self.user_agent = user_agent
        self._sleep = sleep
        self._monotonic = monotonic
        self._hosts = set()                 # genaue Namen
        self._suffixes = set()              # ".steamstatic.com"
        self._last_end = {}                 # dienst -> Zeitpunkt
        self.cancelled = threading.Event()  # das Home endet: laufende Abrufe brechen ab
        self.requests_made = 0              # Zähler für Tests und Berichte

    # -------------------------------------------------------------- Erlaubnisliste

    def allow(self, hosts) -> None:
        for host in hosts or ():
            host = (host or "").strip().lower()
            if host.startswith("."):
                self._suffixes.add(host)
            elif host:
                self._hosts.add(host)

    def allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return False
        if not host or parts.username or parts.password:
            return False
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback(host)):
            return False
        return host in self._hosts or any(host.endswith(s) for s in self._suffixes)

    # -------------------------------------------------------------- Anfrage

    def _get_session(self):
        if self._session is None:
            import requests                     # erst hier: der Import kostet beim Start sonst Zeit
            self._session = requests.Session()
        return self._session

    def get(self, url: str, params=None, *, accept: tuple, max_bytes: int, headers=None,
            service: str = "", image: bool = False) -> tuple:
        """Eine Anfrage (samt erlaubter Weiterleitungen). Rückgabe (bytes, content_type).
        Blockiert; wirft FetchError. headers gehen nur an den ersten Rechner."""
        import requests
        if not self.allowed(url):
            raise FetchError("forbidden-url", detail=host_of(url) or "ohne Rechnernamen")
        if not is_loopback(host_of(url)) and not real_net_allowed():
            raise FetchError("test-mode", transient=True, detail="im Prüfmodus kein echtes Netz")
        gap = self.image_interval if image else self.min_interval
        service = service or host_of(url)
        # Nicht unbegrenzt hinter einer anderen Anfrage warten („Treffer auswählen“ hat eine Frist).
        # Eigener Grund "busy" (E99): Das ist keine Störung dieses Dienstes, sondern eine andere Anfrage
        if not _NET_LOCK.acquire(timeout=max(0.1, self.total_timeout)):
            raise FetchError("busy", transient=True, detail="andere Anfrage hängt")
        try:
            hops = 0
            started = None
            while True:
                if self.cancelled.is_set():
                    raise FetchError("cancelled", transient=True)
                last = self._last_end.get(service)
                if last is not None:
                    wait = gap - (self._monotonic() - last)
                    if wait > 0:
                        self._sleep(wait)
                # Die Gesamtfrist gilt für die Anfrage samt Weiterleitungen, nicht je Sprung neu (E99)
                if started is None:
                    started = self._monotonic()
                elif self._monotonic() - started > self.total_timeout:
                    raise FetchError("timeout", transient=True, detail="Weiterleitungen zu langsam")
                self.requests_made += 1
                resp = None
                try:
                    # „*/*“ nur als Nachsatz, damit kein Dienst mit 406 antwortet; geprüft wird der
                    # Content-Type der Antwort unten trotzdem streng
                    send = {"Accept": ", ".join(accept) + ", */*;q=0.1"}
                    if self.user_agent:
                        send["User-Agent"] = self.user_agent
                    if headers and hops == 0:
                        send.update(headers)
                    resp = self._get_session().get(
                        url, params=params if hops == 0 else None, timeout=(self.timeout, self.timeout),
                        stream=True, headers=send, allow_redirects=False)
                    code = resp.status_code
                    if code in (301, 302, 303, 307, 308):
                        target = urljoin(url, resp.headers.get("Location", "") or "")
                        hops += 1
                        if hops > MAX_REDIRECTS:
                            raise FetchError("redirect-loop")
                        if not self.allowed(target):
                            # fremder Rechner: nie folgen (auch nicht „nur für das Bild“)
                            raise FetchError("redirect-foreign", detail=host_of(target) or "ohne Ziel")
                        url = target
                        continue
                    if code != 200:
                        raise FetchError("http-%d" % code, transient=(code == 429 or code >= 500),
                                         detail=str(resp.headers.get("Retry-After", ""))[:12] if code == 429 else "")
                    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    if not any(ctype == a or (a.endswith("/") and ctype.startswith(a)) for a in accept):
                        raise FetchError("content-type", detail=ctype or "fehlt")
                    try:
                        declared = int(resp.headers.get("Content-Length", "0") or 0)
                    except ValueError:
                        declared = 0
                    if declared > max_bytes:
                        raise FetchError("too-large", detail="%d Bytes angekündigt" % declared)
                    chunks, total = [], 0
                    for chunk in _pieces(resp):
                        if self.cancelled.is_set():
                            raise FetchError("cancelled", transient=True)     # ArtWorker.stop/cancel
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError("too-large", detail="mehr als %d Bytes" % max_bytes)
                        if self._monotonic() - started > self.total_timeout:
                            raise FetchError("timeout", transient=True, detail="Antwort tröpfelt")
                        chunks.append(chunk)
                    return b"".join(chunks), ctype
                except requests.Timeout as exc:
                    raise FetchError("timeout", transient=True, detail=type(exc).__name__) from None
                except requests.ConnectionError as exc:
                    raise FetchError("connection", transient=True, detail=type(exc).__name__) from None
                except requests.RequestException as exc:
                    raise FetchError("request", transient=True, detail=type(exc).__name__) from None
                finally:
                    if resp is not None:
                        resp.close()
                    self._last_end[service] = self._monotonic()
        finally:
            _NET_LOCK.release()

    def get_json(self, url: str, params=None, *, headers=None, service: str = ""):
        """JSON-Antwort (dict oder list). Blockiert; wirft FetchError."""
        body, _ctype = self.get(url, params, accept=JSON_TYPES, max_bytes=MAX_JSON_BYTES,
                                headers=headers, service=service)
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise FetchError("bad-json") from None


def _pieces(resp):
    """Der Inhalt einer Antwort in Stücken, so wie er ankommt. iter_content(65536) gibt erst einen
    vollen Block zurück: Bei einer tröpfelnden Antwort (40 Byte/s) prüfte Http.get seine Frist erst
    nach 27 Minuten (E99). read1 liest genau einmal vom Netz und gibt zurück, was da ist; das
    Lese-Zeitlimit gilt dabei für jede Pause. Ohne read1 (ältere urllib3, Attrappen) kleine Blöcke."""
    raw = getattr(resp, "raw", None)
    read1 = getattr(raw, "read1", None)
    if not callable(read1):
        yield from resp.iter_content(1024)
        return
    while True:
        try:
            chunk = read1(65536, decode_content=True)
        except Exception as exc:
            # urllib3 wirft hier seine eigenen Fehler (requests übersetzt sie nur in iter_content)
            raise _read_error(exc) from None
        if not chunk:
            return
        yield chunk


def _read_error(exc: Exception) -> FetchError:
    """Fehler beim Lesen des Inhalts als FetchError: Zeitüberlauf, sonst Verbindung (beide später erneut)."""
    try:
        from urllib3.exceptions import DecodeError, ReadTimeoutError
    except ImportError:                         # pragma: no cover - urllib3 kommt mit requests
        DecodeError = ReadTimeoutError = ()     # type: ignore[assignment,misc]
    if isinstance(exc, (ReadTimeoutError, TimeoutError)):
        return FetchError("timeout", transient=True, detail=type(exc).__name__)
    if isinstance(exc, DecodeError):
        return FetchError("bad-encoding", detail=type(exc).__name__)
    return FetchError("connection", transient=True, detail=type(exc).__name__)


def https_url(value) -> str | None:
    """Adresse aus einer Antwort in Ordnung bringen („//rechner/pfad“ -> https)."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    return value if value.startswith(("https://", "http://")) else None


def image(url, width=None, height=None) -> dict | None:
    """Eine Bild-Angabe, wie images() sie liefert: {"url", "width", "height"} (Größe None = unbekannt)."""
    url = https_url(url)
    if not url:
        return None
    try:
        width, height = (int(width) or None), (int(height) or None)
    except (TypeError, ValueError):
        width = height = None
    return {"url": url, "width": width, "height": height}


class Source:
    """Form jeder Quelle. Alle Methoden mit Netz blockieren und werfen FetchError.

    search(title, hints)  -> Kandidaten [{"source", "ref", "name", "game", "thumb_url", …}]
                             game: True = Spiel, False = Zubehör, None = erst details() weiß es
    details(candidates)   -> dieselben Kandidaten, um Art und Bilddaten ergänzt (eine Anfrage)
    images(candidate)     -> {"cover"|"hero"|"logo"|"tile": [{"url", "width", "height"}, …]},
                             je Bildart die beste Fassung zuerst; fehlen dem Kandidaten die
                             Daten (nur "ref" bekannt), holt die Quelle sie selbst
    """
    name = ""
    label = ""
    systems: tuple = ()                 # für welche Bibliotheksquellen: () = PC-Spiele
    kinds: tuple = ("cover", "hero", "logo")
    hosts: tuple = ()

    def __init__(self, http: Http):
        self.http = http
        http.allow(self.hosts)

    def available(self) -> bool:
        return True

    def search(self, title: str, hints: dict | None = None) -> list:
        raise NotImplementedError

    def details(self, candidates: list) -> list:
        return candidates

    def images(self, candidate: dict, kinds=None) -> dict:
        raise NotImplementedError

    def thumb(self, candidate: dict) -> str | None:
        """Adresse eines kleinen Vorschaubilds für „Treffer auswählen“ (oder None)."""
        return candidate.get("thumb_url")

    def _candidate(self, ref, name: str, game=True, thumb=None, **more) -> dict:
        out = {"source": self.name, "ref": str(ref), "name": str(name or ""), "game": game,
               "thumb_url": https_url(thumb)}
        out.update(more)
        return out
