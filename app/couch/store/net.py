"""Netz der Shops: eine Fassung für Abfragen mit Zeitgrenze, Lesegrenze und den Kopfzeilen des Shops.

Vorher hatte fast jeder Shop ein eigenes _get/_post, mit verschiedenen Lesegrenzen (still abgeschnitten),
eigener Dekodierung und Fehlerantworten, die offen hängen blieben. Jetzt:
    call()        eine Anfrage -> Reply(status, headers, body); mehr als limit Bytes -> TooLarge
    get_json()    GET, Antwort als JSON (UTF-8, streng)
    get_text()    GET, Antwort als Text (UTF-8, Unlesbares ersetzt)
    get_bytes()   GET, Rohdaten
    send_json()   POST bzw. PUT mit JSON-Rumpf, Antwort als JSON
Kopfzeilen (User-Agent, Accept, Cookies …), Zeitgrenze und Lesegrenze nennt jeder Shop selbst: Manche
Schnittstellen antworten nur einem Browser-Kennzeichen, andere sollen das eigene sehen.

Fehler bleiben die von urllib; welche Ausnahme und welcher deutsche Satz daraus wird, entscheidet wie bisher
der Shop bzw. cache.cached:
    urllib.error.HTTPError   Fehlerstatus. Die Antwort ist schon gelesen (höchstens error_limit Bytes) und
                             die Verbindung zu; e.read() liefert den gelesenen Anfang (404 mit „notFound“,
                             401 mit JSON), schließen muss sie niemand mehr. keep_errors=True: statt der
                             Ausnahme ein Reply mit dem Status.
    URLError, OSError        kein Netz, Zeitgrenze
    ValueError               kaputtes JSON oder UTF-8, TooLarge
Tests stecken wie bisher get=/post= in die Shops, ersetzen urllib.request.urlopen (wird bei jedem Aufruf
nachgeschlagen) oder sprechen mit einem eigenen Server auf 127.0.0.1. Blockierend, gehört in einen
Arbeits-Thread. Nur Standardbibliothek.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_BYTES = 16 * 1024 * 1024
ERROR_BYTES = 1024 * 1024       # so viel einer Fehlerantwort bleibt lesbar


class TooLarge(ValueError):
    """Antwort größer als die Lesegrenze: lieber keine als eine abgeschnittene (gilt wie eine kaputte)."""


class Reply:
    """Gelesene Antwort. headers: die Kopfzeilen von urllib (oder ein dict einer Attrappe)."""
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: Any, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name: str) -> str:
        """Eine Kopfzeile ohne Rücksicht auf Groß- und Kleinschreibung ("" wenn sie fehlt)."""
        items = getattr(self.headers, "items", None)
        for key, value in (items() if items else ()):
            if str(key).lower() == name.lower():
                return str(value or "")
        return ""

    def text(self, errors: str = "replace") -> str:
        return self.body.decode("utf-8", errors)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def cookie_header(cookies: dict) -> str:
    """Kopfzeile Cookie aus {name: wert}, nach Namen sortiert. Werte nie ins Protokoll."""
    return "; ".join("%s=%s" % kv for kv in sorted((cookies or {}).items()))


class ReadHTTPError(urllib.error.HTTPError):
    """HTTPError mit schon gelesenem Anfang der Antwort und ohne offene Verbindung: read() liefert den Anfang,
    close() schadet nicht, und niemand muss sie mehr schließen (sonst ResourceWarning beim Aufräumen)."""

    def __init__(self, error: urllib.error.HTTPError, body: bytes):
        super().__init__(error.filename, error.code, error.msg, error.hdrs, None)
        self.close()
        self._head = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._head.read(n)


def _buffered(error: urllib.error.HTTPError, limit: int) -> ReadHTTPError:
    """Fehlerantwort lesen und schließen (sie hielte sonst die Verbindung offen)."""
    try:
        body = error.read(limit) or b""
    except Exception:  # noqa: BLE001 (Antwort ohne Inhalt oder schon zu)
        body = b""
    finally:
        try:
            error.close()
        except Exception:  # noqa: BLE001
            pass
    return ReadHTTPError(error, body)


def call(url: str, *, timeout: float, headers: dict | None = None, limit: int = MAX_BYTES, method: str | None = None,
         data: bytes | None = None, json_body: Any = None, form: dict | None = None, opener: Any = None,
         keep_errors: bool = False, error_limit: int = ERROR_BYTES) -> Reply:
    """Eine Anfrage. json_body: Rumpf als JSON (Content-Type, falls der Shop keinen nennt); form: Rumpf als
    Formular; data: roh. opener: eigener urllib-Öffner (Keksdose, keine Umleitungen). keep_errors: ein
    Fehlerstatus kommt als Reply zurück (Inhalt höchstens error_limit Bytes) statt als HTTPError."""
    heads = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        if not any(str(k).lower() == "content-type" for k in heads):
            heads["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(url, data=data, headers=heads, method=method)
    # bei jedem Aufruf nachgeschlagen: Tests ersetzen urllib.request.urlopen
    open_url = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_url(req, timeout=timeout) as resp:      # noqa: S310 (Adressen aus festen Teilen der Shops)
            body = resp.read(limit + 1)
            status = int(getattr(resp, "status", 0) or 200)
            got = getattr(resp, "headers", None)
    except urllib.error.HTTPError as e:
        error = _buffered(e, error_limit)
        if keep_errors:
            return Reply(error.code, error.hdrs if error.hdrs is not None else {}, error.read())
        raise error from e
    if len(body) > limit:
        raise TooLarge("Antwort größer als %d Bytes" % limit)
    return Reply(status, got if got is not None else {}, body)


def get_json(url: str, headers: dict, timeout: float, limit: int = MAX_BYTES, opener: Any = None) -> Any:
    return call(url, headers=headers, timeout=timeout, limit=limit, opener=opener).json()


def get_text(url: str, headers: dict, timeout: float, limit: int = MAX_BYTES, opener: Any = None) -> str:
    return call(url, headers=headers, timeout=timeout, limit=limit, opener=opener).text()


def get_bytes(url: str, headers: dict, timeout: float, limit: int = MAX_BYTES, opener: Any = None) -> bytes:
    return call(url, headers=headers, timeout=timeout, limit=limit, opener=opener).body


def send_json(url: str, body: Any, headers: dict, timeout: float, limit: int = MAX_BYTES, method: str = "POST") -> Any:
    """POST (oder PUT) mit JSON-Rumpf (None: ohne Rumpf), Antwort als JSON."""
    return call(url, headers=headers, timeout=timeout, limit=limit, method=method, json_body=body).json()
