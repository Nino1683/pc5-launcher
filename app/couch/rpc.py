"""Methoden-Register und Ereignis-Bus zwischen Python und den beiden Seiten.

Seite -> Python: handle(role, {"method", "params"}, reply) liefert genau eine Antwort
    {"ok": true, "result": …}  oder  {"ok": false, "error": "<Klasse>", "message": "<Text>"}.
Python -> Seite: emit(ereignis, payload, target) geht an alle angemeldeten Senken (UiHost.emit).

Eine Methode bekommt params (dict; "_role" nennt die rufende Seite) und gibt ein dict, None
oder ein concurrent.futures.Future zurück. Ohne GTK benutzbar; main übergibt defer, damit
Ergebnisse aus Threads in der Hauptschleife ankommen.
"""
from __future__ import annotations

import threading
from concurrent.futures import CancelledError, Future
from typing import Any, Callable

from . import jsontext, log
from .launch.spec import LaunchError

_log = log.get("rpc")

GENERIC_MESSAGE = "Das hat nicht geklappt."
TIMEOUT_MESSAGE = "Das dauert ungewöhnlich lange. Versuch es bitte gleich noch einmal."

# Zeitgrenze für Antworten, die als Future kommen: Hängt ein Auftrag (schlafende Platte), bekommt
# die Seite nach so vielen Sekunden {error: "Timeout"} statt nie eine Antwort. Der Auftrag läuft
# weiter; seine späte Antwort wird verworfen. None = keine Grenze: Diese Methoden warten auf den
# Nutzer (Rückfrage, Bildschirmtastatur) oder melden ihr Ende selbst (app.launch: app.error).
DEFAULT_TIMEOUT_S = 45.0
TIMEOUTS: dict[str, float | None] = {
    "osk.open": None, "power.do": None, "app.launch": None, "app.adopt": None,
    "settings.action": None,                    # kann auf die Rückfrage im Overlay warten
    "settings.set": 90.0,                       # WLAN verbinden dauert mit DHCP auch einmal länger
    "settings.reset": 90.0,                     # derselbe Weg wie settings.set
    "lib.fetchArt": 90.0, "lib.artCandidates": 90.0,
    # Text-Editor (E99): Sicherung, fsync und os.replace auf einem langsamen USB-Laufwerk. Nach 45 s
    # zeigte die Seite „Zeitüberschreitung“, obwohl die Datei gespeichert war
    "files.textSave": 120.0,
}
TIMEOUT_PREFIXES: dict[str, float | None] = {"install.": 120.0}   # Archive auf USB-Platten



def page_json(obj: Any) -> str:
    """JSON-Text für die Seite. Namen aus dem Dateisystem in kaputter Kodierung (os.fsdecode:
    einzelne Surrogate \\udc80…\\udcff) gehen nicht als UTF-8 an WebKit; dann alles als \\u-Escape.
    JSON.parse zeigt dafür ein Ersatzzeichen, JSON.stringify schickt denselben Namen zurück
    (2026-10-18: eine Ordnerliste mit so einem Namen bekam nie eine Antwort, die Seite wartete)."""
    return jsontext.dumps(obj, default=str)


class RpcError(Exception):
    """Fehler mit einem deutschen Satz für die Oberfläche."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code


class UnknownMethod(RpcError):
    pass


def error_object(exc: BaseException) -> dict:
    """Ausnahme -> Fehlerobjekt für die Seite. Nur gewollte Texte gehen wörtlich durch."""
    if isinstance(exc, (RpcError, LaunchError)):
        out = {"ok": False, "error": getattr(exc, "code", "") or type(exc).__name__,
               "message": exc.message}
        detail = getattr(exc, "detail", "")
        if detail:
            out["detail"] = detail
        return out
    return {"ok": False, "error": type(exc).__name__, "message": GENERIC_MESSAGE,
            "detail": str(exc)}


class Rpc:
    def __init__(self, defer: Callable[[Callable[[], None]], None] | None = None,
                 later: Callable[[float, Callable[[], None]], Callable[[], None]] | None = None):
        """defer(fn): fn in der Hauptschleife ausführen. later(sekunden, fn) -> abbrechen():
        Zeitgeber in der Hauptschleife (für die Zeitgrenze der Antworten; ohne later keine)."""
        self._methods: dict[str, Callable[[dict], Any]] = {}
        self._sinks: list[Callable[[str | None, str, dict | None], None]] = []
        # Alles, was zur Seite geht (Antworten, Fehler, Ereignisse), läuft hier durch: Englisch übersetzt es
        # (i18n.translate_tree, 2026-10-25); None = unverändert (Deutsch)
        self.out_filter: Callable[[Any], Any] | None = None
        self._defer = defer or (lambda fn: fn())
        self._later = later
        self._main = threading.get_ident()
        self.timeouts: dict[str, float | None] = dict(TIMEOUTS)
        self.default_timeout: float | None = DEFAULT_TIMEOUT_S
        self.timed_out = 0                      # Zähler für Prüfläufe

    def timeout_for(self, name: str) -> float | None:
        if name in self.timeouts:
            return self.timeouts[name]
        for prefix, seconds in TIMEOUT_PREFIXES.items():
            if name.startswith(prefix):
                return seconds
        return self.default_timeout

    # ------------------------------------------------------------ Register
    def register(self, name: str, fn: Callable[[dict], Any]) -> None:
        if name in self._methods:
            _log.info("Methode %s wird ersetzt", name)
        self._methods[name] = fn

    def method(self, name: str) -> Callable:
        def deco(fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
            self.register(name, fn)
            return fn
        return deco

    def names(self) -> list[str]:
        return sorted(self._methods)

    # ------------------------------------------------------------ Aufruf
    def dispatch(self, role: str, name: str, params: dict | None) -> Any:
        """Ruft die Methode. Ausnahmen gehen an den Aufrufer (handle macht daraus die Antwort)."""
        fn = self._methods.get(name)
        if fn is None:
            raise UnknownMethod("Unbekannte Methode: %s" % name)
        p = dict(params) if isinstance(params, dict) else {}
        p["_role"] = role
        return fn(p)

    def handle(self, role: str, message: Any, reply: Callable[[dict], None],
               meta: dict | None = None) -> None:
        """Eingang der Brücke: ruft die Methode und antwortet genau einmal, bei Futures später.
        meta (nur Prüfsteuerung): bekommt "deferred" = die Methode hat ein Future geliefert."""
        try:
            if not isinstance(message, dict) or not isinstance(message.get("method"), str):
                raise RpcError("Ungültiger Aufruf.", "BadRequest")
            result = self.dispatch(role, message["method"], message.get("params"))
        except Exception as e:
            self._log_failure(message, e)
            reply(self._out(error_object(e)))
            return
        if meta is not None:
            meta["deferred"] = isinstance(result, Future)
        if isinstance(result, Future):
            name = message["method"]
            # reply steht nur in box (keine Closure darf es direkt fassen): Nach der Antwort lässt
            # answer es los. Die Rückrufe des Futures leert CPython nie, und das Future stirbt oft
            # erst in einem Arbeits-Thread; die Senke soll dann nichts mehr festhalten (E99).
            # Auch der Rahmen hier nicht (del): Ein Traceback kann ihn über f_back festhalten.
            box: dict = {"answered": False, "cancel": None, "reply": reply}
            del reply

            def answer(obj: dict) -> bool:
                """Genau eine Antwort (Hauptschleife): die erste gilt, eine späte wird verworfen."""
                if box["answered"]:
                    return False
                box["answered"] = True
                cancel, box["cancel"] = box["cancel"], None
                if cancel is not None:
                    try:
                        cancel()
                    except Exception:
                        pass
                send, box["reply"] = box["reply"], None
                send(self._out(obj))
                return True

            def done(fut: Future) -> None:
                def finish() -> None:
                    # Nicht fut.result(): Das wirft die gespeicherte Ausnahme erneut und hängt dabei die
                    # Rahmen dieses Threads (finish … handle, _on_message) an ihren Traceback; das Future
                    # hielte sie dann in einem Zyklus, den der GC in irgendeinem Thread auflöst (E99)
                    failure = CancelledError() if fut.cancelled() else fut.exception()
                    if failure is None:
                        obj = {"ok": True, "result": fut.result()}
                    else:
                        if not box["answered"]:
                            self._log_failure(message, failure)
                        obj = error_object(failure)
                    if not answer(obj):
                        _log.info("%s: Antwort kam nach der Zeitgrenze und wird verworfen", name)
                # Futures werden meist in einem Thread fertig; die Antwort gehört in die Hauptschleife
                if threading.get_ident() == self._main:
                    finish()
                else:
                    self._defer(finish)

            limit = self.timeout_for(name)
            if limit and self._later is not None and not result.done():
                def expired() -> None:
                    box["cancel"] = None                # der Zeitgeber ist schon abgelaufen
                    if answer({"ok": False, "error": "Timeout", "message": TIMEOUT_MESSAGE}):
                        self.timed_out += 1
                        _log.warning("%s: keine Antwort nach %.0f s, die Seite bekommt Timeout", name, limit)
                box["cancel"] = self._later(limit, expired)
            result.add_done_callback(done)
            return
        reply(self._out({"ok": True, "result": result}))

    def _out(self, obj: Any) -> Any:
        fn = self.out_filter
        if fn is None or obj is None:
            return obj
        try:
            return fn(obj)
        except Exception:
            _log.exception("Übersetzen der Ausgabe")
            return obj

    @staticmethod
    def _log_failure(message: Any, exc: BaseException) -> None:
        name = message.get("method") if isinstance(message, dict) else "?"
        if isinstance(exc, (RpcError, LaunchError)):
            _log.info("%s: %s", name, getattr(exc, "message", exc))
        else:
            _log.error("%s: %s: %s", name, type(exc).__name__, exc, exc_info=exc)

    # ------------------------------------------------------------ Ereignisse
    def add_sink(self, fn: Callable[[str | None, str, dict | None], None]) -> None:
        """fn(target, ereignis, payload); target None = beide Seiten."""
        self._sinks.append(fn)

    def emit(self, event: str, payload: dict | None = None, target: str | None = None) -> None:
        def send() -> None:
            out = self._out(payload)
            for sink in list(self._sinks):
                try:
                    sink(target, event, out)
                except Exception:
                    _log.exception("Ereignis %s", event)
        if threading.get_ident() == self._main:
            send()
        else:
            self._defer(send)


def resolved(value: Any) -> Future:
    """Ein schon fertiges Future (für Methoden, die mal sofort, mal später antworten)."""
    fut: Future = Future()
    fut.set_result(value)
    return fut
