"""Ablaufregeln des Home (Bauplan 4.5): Wer ist vorn, was zeigt das Overlay, wohin gehen die
Tasten, wann wird ein Spiel angehalten.

Die Shell kennt die Dienste nur über interfaces.py und Zeit nur über einen Scheduler; deshalb
läuft sie in tests/unit/test_shell.py vollständig mit Attrappen.

Drei Dinge werden aus dem Zustand berechnet (_apply), nie einzeln von Hand gesetzt:
  Overlay-Modus  = Modus der obersten Ansicht, sonst "passive" bei einer Mitteilung, sonst "hidden"
  angehalten     = jede anhaltbare App, die nicht vorn ist, und die vordere unter einem aktiven Overlay
  Eingabe-Route  = "overlay", wenn die oberste Ansicht Tasten braucht; sonst "app"/"mouse" bei
                   einer App vorn; sonst "home"
Weiterlaufen und Maus-Route gibt es erst, wenn alle Pads neutral sind und 150 ms vergangen
sind: Der Druck, der ein Menü schließt, darf nicht im Spiel ankommen.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from . import HOME_APPID, log, oskdirect, paths, system
from .launch import build_spec
from .launch.prefix import TEXT_ADOPT_HINT, AdoptCancelled, AdoptionRequired, size_text
from .launch.spec import LaunchError, LaunchSpec, apply_pad_keys
from .rpc import RpcError

_log = log.get("shell")

HIDDEN, PASSIVE, ACTIVE = "hidden", "passive", "active"
GAME_KINDS = ("game", "emulator")

SETTLE_POLL_MS = 30         # Takt, in dem auf ein neutrales Pad gewartet wird
SETTLE_MAX_S = 3.0          # hängt eine Taste, läuft das Spiel danach trotzdem weiter
CLOSE_GRACE_MS = 250        # so lange darf die Seite ihr Schließen zeigen, bevor das Fenster verschwindet
# Nur diese darf der Schalter je Spiel anhalten (4.5). Heute dieselben Arten wie GAME_KINDS, aber
# eine andere Frage („darf stehen“ statt „zählt als Spiel“); deshalb eigener Name, eine Quelle (E99)
FREEZABLE_KINDS = GAME_KINDS
_SCREEN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")     # ui.open: Name eines Bildschirms der Seite
DETACH_MS = 1500            # Home hat so lange den Fokus, obwohl eine App vorn sein sollte
OWN_WORDS = "MELDUNG: "        # so beginnt der Satz eines Starthelfers im Protokoll (E57)
CLOSE_OTHER_S = 15.0        # Wartezeit auf das Ende des anderen Spiels, danach SIGKILL
CLOSE_KILL_S = 5.0
AFTER_GAME_EXTRA_S = 10.0   # Nachlauf eines Launcher-Spiels (E99): so viel länger als dessen Frist warten
SHUTDOWN_S = 12.0           # Wartezeit auf das Ende aller Apps vor Sitzungsende
EARLY_EXIT_S = 15.0         # Ende mit Fehler so kurz nach dem Start gilt als Absturz
# SIGILL, SIGABRT, SIGBUS, SIGFPE, SIGSEGV: als Signal (negativ) oder als 128 + Nummer
CRASH_CODES = frozenset({-4, -6, -7, -8, -11, 132, 134, 135, 136, 139})

ADOPT_TITLE = "Spielstände übernehmen?"
ADOPT_LEAD = "„%s“ lief bisher über Lutris."         # davor; der Hinweis selbst ist prefix.TEXT_ADOPT_HINT
ADOPT_SIZE = "Die Kopie ist rund %s groß."
ADOPT_SMALL = "Die Kopie ist kleiner als 1 MB."
ADOPT_COPYING = "Spielstände werden übernommen …"
# Eigene Apps, die Text der Bildschirmtastatur direkt annehmen (oskdirect): Eintrag -> Name des Eingangs
OSK_DIRECT = {"app:browser": "browser", "app:claude": "terminal"}
OSK_DIRECT_WAIT_MS = 15     # Takt, solange vorher getippte XTEST-Tasten noch unterwegs sind
HINT_SECONDS = 8.0          # so lange bleibt das Overlay für einen Bedienhinweis sichtbar
HINT_MOUSE = "mouse-mode"
HINT_CARDS = "cards-mode"
HINT_CARDS_SECONDS = 12.0   # Belegung des Zeiger-Modus: mehr Zeilen als beim Maus-Modus
HINT_OPTI = "opti-mode"     # Belegung im Menü von OptiScaler (E118) …
OPTI_HINT_TIMES = 3         # … so oft insgesamt
CARDS_HINT_STARTS = 3       # so oft je Spiel
OWN_PROFILES = ("keys", "game", "term", "cards")     # eigene Belegung einer App (spec.mouse), nicht der Maus-Modus
# Eigene Sprungstellen (E99): so heißen die Tasten in der Mitteilung „Sprungstelle für R1 gemerkt“
SPOT_LABELS = {"up": "Steuerkreuz ↑", "down": "Steuerkreuz ↓", "left": "Steuerkreuz ←", "right": "Steuerkreuz →",
               "l1": "L1", "r1": "R1"}
# Menü von OptiScaler (E118): Taste (Vorgabe von OptiScaler, ShortcutKey=auto), wie lange nach dem Control
# Center sie noch gilt und wie lange nach dem Weiterlaufen des Spiels sie kommt
OPTISCALER_KEY = "Insert"
OPTISCALER_WAIT_S = 10.0
OPTISCALER_DELAY_MS = 250
# Controller fällt kurz nach dem Fortsetzen eines angehaltenen Spiels ab (E119; Wunsch 2026-10-25, am Fernseher
# bestätigt: ohne Anhalten blieb er verbunden). Das Spiel spricht den DualSense selbst an und reißt nach dem
# Weiterlaufen die Bluetooth-Verbindung ab. So lange nach dem Fortsetzen zählt ein Abfall, ab so vielen je Spiel
# (in dieser Sitzung) wird es nicht mehr angehalten
PAD_DROP_WINDOW_S = 30.0
PAD_DROP_LIMIT = 2

# Kennungen im Ereignis app.error (Feld code), soweit die Shell sie selbst vergibt. Die der
# Starter stehen als LaunchError.reason in couch/launch (needs-exe, missing-file, disk-missing,
# umu-missing, proton-missing, wineserver, not-installed, already-running …).
CODE_GENERIC = "launch-failed"      # Start gescheitert, Ursache ohne eigene Kennung
CODE_NO_WINDOW = "no-window"        # beendet, bevor ein Bild kam
CODE_TIMEOUT = "timeout"            # kein Bild in der Wartezeit
CODE_CRASHED = "crashed"            # lief schon und endete mit einem Fehler
PHASE_START, PHASE_RUN = "start", "run"


class _Timer:
    __slots__ = ("source", "done")

    def __init__(self) -> None:
        self.source = 0
        self.done = False


class GLibScheduler:
    """Zeit und Wecker aus der GLib-Hauptschleife. Tests benutzen stattdessen eine Handuhr."""

    def now(self) -> float:
        return time.monotonic()

    def later(self, ms: float, fn: Callable[[], None]) -> _Timer:
        from gi.repository import GLib
        timer = _Timer()

        def fire() -> bool:
            timer.done = True
            fn()
            return False
        timer.source = GLib.timeout_add(max(0, int(ms)), fire)
        return timer

    def cancel(self, timer: _Timer | None) -> None:
        if timer is not None and not timer.done:
            from gi.repository import GLib
            timer.done = True
            GLib.source_remove(timer.source)

    def soon(self, fn: Callable[[], None]) -> None:
        from gi.repository import GLib
        GLib.idle_add(lambda: fn() and False, priority=GLib.PRIORITY_DEFAULT)   # wie main.on_main


@dataclass
class View:
    """Eine Ansicht im Overlay-Fenster. Die oberste bestimmt Modus und Tastenziel."""
    name: str                               # "cc" | "osk" | "confirm" | "disconnected" | "quit-progress" | "saver"
    params: dict = field(default_factory=dict)
    mode: str = ACTIVE                      # ACTIVE hält die App an, PASSIVE lässt sie laufen
    takes_input: bool = True                # Pad-Tasten gehen an die Overlay-Seite
    app_id: int | None = None               # gehört zu dieser App und endet mit ihr
    on_close: Callable[[Any], None] | None = None
    timer: Any = None
    serial: int = 0                         # die Seite schickt sie mit overlay.close zurück


@dataclass
class _Launch:
    entry_id: str
    title: str
    timer: Any = None


class Shell:
    def __init__(self, app: Any, scheduler: Any = None):
        self.app = app
        self.rpc = app.rpc
        self.state = app.state
        self.config = app.config
        self.gs = app.gs
        self.focus = app.focus
        self.procs = app.procs
        self.input = app.input
        self.library = app.library
        self.sched = scheduler or GLibScheduler()
        self._background = getattr(app, "background", None) or system.background

        self._front_id: int | None = None       # App, die vorn sein soll (None = Home)
        self._return_to: dict[str, int] = {}    # Eintrag (Browser) -> App, die nach seinem Ende wieder vorn ist (E94)
        self._detached = False                  # sie soll vorn sein, aber Gamescope zeigt das Home
        self._detach_timer: Any = None
        self._views: list[View] = []
        self._hold_mode: str | None = None      # Fenster bleibt noch kurz, während die Seite schließt
        self._hold_timer: Any = None
        self._toast_until = 0.0
        self._toast_timer: Any = None
        self._launching: dict[int, _Launch] = {}
        self._preparing: set[str] = set()       # Einträge, deren Startauftrag gerade gebaut wird
        self._cancel_wanted: set[str] = set()   # davon: Start abgebrochen, bevor er so weit war
        self._tail_waits: dict[str, dict] = {}  # Eintrag -> Warten auf den Nachlauf eines Launcher-Spiels (E99)
        self._adopt_questions: dict[str, str] = {}      # Eintrag -> Kennung der Rückfrage (E24)
        self._adopting: dict[str, Any] = {}     # Eintrag -> Abbruch-Marke der laufenden Prefix-Kopie
        self._adopt_percent: dict[str, int] = {}        # Eintrag -> Stand der Kopie in Prozent
        # Von der Verdrahtung gesetzt: fn(entry, cancel) kopiert das Prefix (blockierend, im
        # Thread). Ohne sie scheitert ein Start, der eine Übernahme braucht, mit deren Hinweis.
        # Den Fortschritt meldet sie über adopt_progress() (aus der Hauptschleife).
        self.adopt_prefix: Callable[[Any, Any], Any] | None = None
        self._closing: set[int] = set()         # Apps, deren Ende wir selbst verlangt haben
        self._repairing: set[str] = set()       # Einträge im Reparaturversuch „wie in Lutris“ (E55)
        self._shown: set[int] = set()           # Apps, die schon ein Bild hatten (zählt als gespielt)
        self._mouse: dict[int, bool] = {}       # Maus-Modus je App, falls umgeschaltet
        # Maus vom sichtbar gewordenen Launcher (E94) -> Stand davor (None = nie umgeschaltet); mit
        # dem ersten Spielfenster gilt wieder dieser Stand, außer der Nutzer hat inzwischen selbst geschaltet
        self._reveal_mouse: dict[int, bool | None] = {}
        self._mouse_hinted = False              # Hinweis zur Belegung schon gezeigt (oder früher gesehen)
        self._cards_hinted: set[int] = set()    # Zeiger-Modus: Hinweis für diesen Start schon gezeigt
        self._key: tuple | None = None          # Lage, für die zuletzt auf ein neutrales Pad gewartet wurde
        self._settled = False
        self._settle_timer: Any = None
        self._settle_started = 0.0
        self._neutral_since: float | None = None
        self._waiters: list[dict] = []          # "wenn diese Apps weg sind, dann …"
        self._had_pads = False
        self._known_pads: dict[Any, str] = {}   # id -> Name, für "verbunden"/"getrennt"
        self._low_battery: set = set()          # Pads, für die schon gewarnt wurde
        self._applying = False
        self._dirty = False
        self._last_running: list | None = None
        self._ids = itertools.count(1)
        self._serials = itertools.count(1)
        self._osk: View | None = None
        self._osk_direct: list = []             # (Eingang, Text), wartet hinter XTEST-Tasten
        self._osk_direct_timer: Any = None
        self.osk_send = oskdirect.send          # Tests: Attrappe
        self.ending = False
        self.capture: Callable[[str], Any] | None = None     # Bildschirmfoto (wiring_settings.start_capture)
        self._resume_until = 0.0                    # bis wann ein wegfallendes Pad zum Aufwachen gehört
        # Schalter „Im Hintergrund anhalten“ je Spiel (lib.setOption freeze), für laufende Apps:
        self._freeze_opts: dict[int, bool] = {}     # zuletzt gesehener Wert je App
        self._freeze_wait: dict[int, tuple] = {}    # eingeschaltet: erst ab dem nächsten Wechsel
        self._key_after: tuple | None = None        # (App, Taste, bis wann): Taste, sobald die App weiterläuft (E118)
        self._opti: set[int] = set()                # Apps, deren OptiScaler-Menü wir geöffnet haben: Pad bedient es
        self._thawed_at: dict[int, float] = {}      # App -> wann sie zuletzt nach dem Overlay weiterlief (E119)
        self._pad_drops: dict[str, int] = {}        # Eintrag -> Controller-Abfälle kurz danach

    # ================================================================ Start
    def start(self) -> None:
        """Rückrufe anmelden, Overlay versteckt, Home vorn. Übernommene Apps (Neustart des Home
        im Betrieb, Befund 23) bleiben im Hintergrund und werden wieder angehalten."""
        self.procs.on_change(self._on_proc)
        self.focus.on_change(self._on_focus)
        self.gs.watch(self._on_gs)
        self.input.on_button = self.on_button
        self.input.on_ps = self.on_ps
        self.input.on_pads_changed = self.on_pads_changed
        self.input.on_spot = self.on_spot
        # Der Schalter „Spiele im Hintergrund anhalten“ wirkt sofort, auch auf ein Spiel, das
        # gerade angehalten im Hintergrund liegt
        self.config.on_change("games.freeze", lambda key, value: self._apply())
        # … und ebenso der Schalter je Spiel (Spieleinstellungen), auch für ein laufendes Spiel
        on_change = getattr(self.library, "on_change", None)
        if on_change is not None:
            on_change(lambda: self.sched.soon(self._freeze_options_changed))
            # … und eigene Sprungstellen, die jemand in den Spieleinstellungen zurückgesetzt hat (E99)
            on_change(lambda: self.sched.soon(self._spots_changed))
        pads = self._pads()
        self._had_pads = bool(pads)
        self._known_pads = {p.get("id"): str(p.get("name", "")) for p in pads}
        self.state.update(pads=pads)
        self._shown = {a.app_id for a in self.procs.running() if a.visible}
        self.focus.overlay(HIDDEN)
        self.focus.set_foreground(None)
        self._apply()
        self._emit_running(force=True)

    # ================================================================ kleine Helfer
    def _pads(self) -> list:
        try:
            return list(self.input.pads())
        except Exception:
            _log.exception("input.pads")
            return []

    def _neutral(self) -> bool:
        try:
            return bool(self.input.all_neutral())
        except Exception:
            _log.exception("input.all_neutral")
            return True

    def _front_app(self) -> Any:
        return self.procs.by_app_id(self._front_id) if self._front_id is not None else None

    def _effective_front(self) -> Any:
        """Die App, die wirklich zu sehen ist (None, solange Gamescope das Home zeigt)."""
        return None if self._detached else self._front_app()

    def _top(self) -> View | None:
        return self._views[-1] if self._views else None

    def _title(self, entry_id: str, fallback: str = "") -> str:
        entry = self.library.get(entry_id)
        return entry.title if entry else (fallback or entry_id)

    def _app_title(self, app: Any) -> str:
        return self._title(app.entry_id, getattr(app.spec, "title", ""))

    def _may_freeze(self, app: Any) -> bool:
        # games.freeze ist der Schalter „Spiele im Hintergrund anhalten“ (Einstellungen › Spiele, E22);
        # dazu der Schalter je Spiel, wie er JETZT steht (nicht nur beim Start, Befund 2026-10-14)
        if not app.spec.freeze or not self.config.get("games.freeze", True):
            return False
        if self._entry_freeze(app) is False or app.app_id in self._freeze_wait:
            return False
        return True

    def _has_window(self, app: Any) -> bool:
        try:
            return app.app_id in set(self.gs.focusable_apps())
        except Exception:
            return True

    def _game_seen(self, app: Any) -> bool | None:
        """Spiel über einen Launcher (spec.hide_windows, E94): Hatte das Spiel selbst schon ein Fenster?
        None, wenn es kein Launcher-Spiel ist oder der Fokus den Start nicht kennt (Neustart des Home):
        Dann gilt, was ohne diese Frage gälte (E99)."""
        if not getattr(app.spec, "hide_windows", ()):
            return None
        seen = getattr(self.focus, "game_seen", None)
        if seen is None:
            return None
        try:
            return seen(app.app_id)
        except Exception:
            _log.exception("focus.game_seen")
            return None

    def _after_game(self, app: Any) -> bool:
        """Launcher-Spiel im Nachlauf (E99): Das Spiel hatte ein Fenster, jetzt hat die App keines mehr,
        der Helfer gleicht noch Spielstände ab (Ubisoft, Rockstar). Zählt nicht als laufendes Spiel
        für „… läuft noch – schließen?“: Ein Ja bräche genau den Abgleich ab."""
        return self._game_seen(app) is True and not self._has_window(app)

    @staticmethod
    def _after_game_seconds(apps: list) -> float:
        """So lange höchstens auf das Ende des Nachlaufs warten: der des Launchers (COUCH_LAUNCHER_AFTER,
        proton._launcher_spec) plus AFTER_GAME_EXTRA_S."""
        longest = 0.0
        for app in apps:
            try:
                longest = max(longest, float((getattr(app.spec, "env", None) or {}).get("COUCH_LAUNCHER_AFTER") or 0))
            except (TypeError, ValueError):
                pass
        return longest + AFTER_GAME_EXTRA_S

    def _entry_freeze(self, app: Any) -> bool | None:
        """Option freeze des Eintrags (Spieleinstellungen); None, wenn es ihn nicht gibt."""
        try:
            entry = self.library.get(app.entry_id)
        except Exception:
            _log.exception("library.get")
            return None
        option = getattr(entry, "option", None) if entry is not None else None
        return bool(option("freeze")) if option is not None else None

    def _switch_sig(self) -> tuple:
        """Was sich bei einem Wechsel ändert: App vorn, Home statt App, oberste Ansicht."""
        top = self._top()
        return (self._front_id, self._detached, top.serial if top is not None else None)

    def _freeze_options_changed(self) -> None:
        """Die Bibliothek hat sich geändert: Hat jemand den Schalter je Spiel einer laufenden App
        umgelegt? Aus: wie der globale Schalter sofort auftauen. Ein: erst ab dem nächsten
        Wechsel anhalten (nicht mitten in den Spieleinstellungen)."""
        changed = False
        for app in list(self.procs.running()):
            now = self._entry_freeze(app)
            if now is None:
                continue
            before = self._freeze_opts.get(app.app_id)
            self._freeze_opts[app.app_id] = now
            if before is None or before == now:
                continue
            changed = True
            if now:
                if getattr(app.spec, "kind", "") in FREEZABLE_KINDS:
                    app.spec.freeze = True      # mit „nie anhalten“ gestartet: ab jetzt erlaubt
                    self._freeze_wait[app.app_id] = self._switch_sig()
            else:
                self._freeze_wait.pop(app.app_id, None)
        if changed:
            self._apply()

    def _mouse_on(self, app: Any) -> bool:
        return app.app_id in self._opti or self._mouse.get(app.app_id, bool(app.spec.mouse))

    def _pad_profile(self, app: Any) -> str:
        """Was das Pad in der Route "mouse" tut: "keys" (nur Pfeile, Enter, Escape) für Apps mit
        Fernseh-Oberfläche (spec.mouse == "keys", z. B. YouTube), "game" (Tastatur und Maus, E64)
        für Spiele ohne Controller (Spieleinstellung „Controller“), "cards" (Zeiger, E97) für Spiele,
        die man nur mit der Maus spielt, jeweils solange niemand den Maus-Modus im Control Center
        eingeschaltet hat; sonst "mouse". Vor allem anderen "opti" (E118), solange das Menü von OptiScaler
        offen ist, das optiscaler_menu geöffnet hat."""
        if app.app_id in self._opti:
            return "opti"
        own = app.spec.mouse if app.spec.mouse in OWN_PROFILES else None
        return own if own and app.app_id not in self._mouse else "mouse"

    def _pad_look(self, app: Any) -> float:
        """Spiel-Modus: Tempo des Umschauens aus den Spieleinstellungen (Option pad_look)."""
        entry = self.library.get(app.entry_id) if app.entry_id else None
        try:
            return float(entry.option("pad_look")) if entry is not None else 1.0
        except (TypeError, ValueError):
            return 1.0

    def _pad_layout(self, app: Any) -> str:
        """Zeiger-Modus: Sprungstellen des Spiels (launch.spec.pad_layout, z. B. Hearthstone)."""
        from .launch.spec import pad_layout
        entry = self.library.get(app.entry_id) if app.entry_id else None
        try:
            return pad_layout(entry) if entry is not None else ""
        except Exception:
            _log.exception("Sprungstellen für %s", app.entry_id)
            return ""

    def _pad_spots(self, app: Any) -> dict:
        """Zeiger-Modus: eigene Sprungstellen des Spiels (Option pad_spots, E99), über _pad_layout."""
        from .launch.spec import pad_spots
        entry = self.library.get(app.entry_id) if app.entry_id else None
        try:
            return pad_spots(entry) if entry is not None else {}
        except Exception:
            _log.exception("Eigene Sprungstellen für %s", app.entry_id)
            return {}

    @staticmethod
    def _pad_keys(app: Any) -> dict:
        """Spiel-Modus: Tastenbelegung des Spiels (E109), beim Start in spec.pad_keys festgehalten
        (launch.spec.apply_pad_keys): Eine Änderung in den Spieleinstellungen gilt ab dem nächsten Start."""
        keys = getattr(getattr(app, "spec", None), "pad_keys", None)
        return dict(keys) if isinstance(keys, dict) else {}

    def _spots_changed(self) -> None:
        """Die Bibliothek hat sich geändert: Gelten für das Spiel vorn noch dieselben eigenen Sprungstellen?"""
        if self.input.route != "mouse" or getattr(self.input, "mouse_profile", "") != "cards":
            return
        front = self._front_app()
        held = getattr(self.input, "mouse_spots", None)
        if front is not None and held is not None and held != self._pad_spots(front):
            self._apply()

    @staticmethod
    def _optiscaler(app: Any) -> bool:
        """Lief der Start mit OptiScaler (E118)? Proton lädt ihn nur bei PROTON_USE_OPTISCALER=1 (launch/proton.py,
        steam_game_env); der Schalter in den Spieleinstellungen gilt erst ab dem nächsten Start."""
        env = getattr(getattr(app, "spec", None), "env", None)
        return isinstance(env, dict) and env.get("PROTON_USE_OPTISCALER") == "1"

    def _real_mouse(self, app: Any) -> bool:
        """Maus-Modus im Sinn des Control Centers (der Tasten-Modus zählt nicht als Maus)."""
        return self._mouse_on(app) and self._pad_profile(app) == "mouse"

    def _entry_view(self, app: Any) -> dict:
        entry = self.library.get(app.entry_id)
        if entry:
            return entry.view(True)
        return {"id": app.entry_id, "title": self._app_title(app), "running": True}

    def running_view(self) -> list[dict]:
        return [{"entryId": a.entry_id, "appId": a.app_id, "title": self._app_title(a),
                 "kind": a.spec.kind, "frozen": bool(a.frozen), "started": a.started,
                 "launching": a.app_id in self._launching,
                 # wird gerade beendet: nicht mehr „Fortsetzen“ anbieten
                 "closing": a.app_id in self._closing,
                 "optiscaler": self._optiscaler(a)}
                for a in self.procs.running()]

    def _emit_running(self, force: bool = False) -> None:
        running = self.running_view()
        if force or running != self._last_running:
            self._last_running = running
            self.rpc.emit("app.changed", {"running": running})
        self.state.update(running=running)

    def _emit_launching(self, entry_id: str, phase: str, text: str = "",
                        percent: int | None = None) -> None:
        payload: dict = {"id": entry_id, "phase": phase, "text": text}
        if percent is not None:
            payload["percent"] = int(percent)       # nur bei "prepare" mit messbarem Fortschritt
        self.rpc.emit("app.launching", payload)

    def _emit_error(self, app: Any, title: str, message: str, code: str = CODE_GENERIC,
                    phase: str = PHASE_RUN) -> None:
        """app.error für eine App, die es gab: mit dem Ende ihres Protokolls. phase sagt der
        Seite, ob der Start gescheitert ist ("start") oder ein laufendes Spiel endete ("run")."""
        path = getattr(app, "log_path", None) or paths.app_log(app.entry_id)
        self.rpc.emit("app.error", {"id": app.entry_id, "title": title, "message": message,
                                    "log": log.tail(path, 20), "code": code, "phase": phase})

    def report_launch_error(self, entry_id: str, exc: BaseException, title: str = "") -> None:
        """app.error für einen Start, der gar nicht erst zustande kam (Vorab-Prüfungen der
        Starter, Platte weg, Startdatei fehlt …). code ist LaunchError.reason; bei "needs-exe"
        öffnet die Oberfläche die Auswahl der Startdatei. Ein Protokoll gibt es hier nicht."""
        if isinstance(exc, LaunchError):
            message = exc.message
            code = getattr(exc, "reason", "") or CODE_GENERIC
        else:
            _log.error("Start von %s: %s: %s", entry_id, type(exc).__name__, exc, exc_info=exc)
            message = "Beim Vorbereiten des Starts ist ein Fehler aufgetreten."
            code = CODE_GENERIC
        self.rpc.emit("app.error", {"id": entry_id, "title": title or self._title(entry_id),
                                    "message": message, "log": "", "code": code,
                                    "phase": PHASE_START})

    # ================================================================ Berechnen und Anwenden
    def _apply(self) -> None:
        """Bringt Overlay, Anhalten, Route und sys.state mit dem inneren Zustand in Deckung."""
        if self._applying:              # Rückrufe der Dienste landen wieder hier
            self._dirty = True
            return
        self._applying = True
        try:
            for _ in range(8):
                self._dirty = False
                self._apply_once()
                if not self._dirty:
                    break
        finally:
            self._applying = False

    def _apply_once(self) -> None:
        top = self._top()
        if top is not None:
            mode = top.mode
        elif self._hold_mode is not None:
            mode = self._hold_mode
        else:
            mode = PASSIVE if self.sched.now() < self._toast_until else HIDDEN
        if self.focus.overlay_mode != mode:
            self.focus.overlay(mode)

        intended = self._front_app()
        front = self._effective_front()
        blocked = mode == ACTIVE
        takes_input = bool(top and top.takes_input)

        if self._freeze_wait:
            sig = self._switch_sig()
            for app_id, at in list(self._freeze_wait.items()):
                if at != sig:
                    del self._freeze_wait[app_id]   # gewechselt: der Schalter je Spiel gilt jetzt
        for app in list(self.procs.running()):
            if app.app_id not in self._freeze_opts:
                opt = self._entry_freeze(app)
                if opt is not None:
                    self._freeze_opts[app.app_id] = opt
            if (app.app_id in self._launching or app.app_id in self._closing
                    or app.entry_id in self._preparing):
                continue                # lädt noch bzw. soll sich gerade beenden
            if not self._may_freeze(app):
                if app.frozen:
                    self.procs.thaw(app)    # darf nicht (mehr) angehalten sein: Schalter aus
                continue
            if app.frozen:
                continue
            if getattr(app.spec, "freeze_needs_window", False) and not self._has_window(app):
                continue                # Steam ohne Spielfenster (E57): beendet sich gerade, nicht festhalten
            if self._game_seen(app) is False:
                continue                # Launcher aufgedeckt, Spiel noch nicht da: Anmeldung, Patch laufen weiter (E99)
            is_intended = intended is not None and app.app_id == intended.app_id
            is_front = front is not None and app.app_id == front.app_id
            if not is_intended or (blocked and is_front):
                self._freeze(app)

        key = (front.app_id, blocked, takes_input) if front is not None else None
        if key != self._key:
            self._key = key
            self._settled = False
            self.sched.cancel(self._settle_timer)
            self._settle_timer = None
            if front is not None and not blocked:
                self._begin_settle()

        if takes_input:
            route = "overlay"
        elif front is not None:
            route = "mouse" if (self._settled and not blocked and self._mouse_on(front)) else "app"
        else:
            route = "home"
        profile = self._pad_profile(front) if route == "mouse" else "mouse"
        # Liegt das Home oder das Control Center über einer laufenden App, liest nur couch das Pad
        # (Kodi, YouTube, Browser lasen sonst mit und reagierten auf die Bedienung des Overlays); ebenso im
        # Menü von OptiScaler (E118), soweit das Spiel das Pad nicht über hidraw liest
        exclusive = (route in ("overlay", "home") and any(True for _ in self.procs.running())) or profile == "opti"
        set_exclusive = getattr(self.input, "set_exclusive", None)
        if set_exclusive is not None:
            set_exclusive(exclusive)
        look = self._pad_look(front) if profile == "game" else 1.0
        layout = self._pad_layout(front) if profile == "cards" else ""
        spots = self._pad_spots(front) if route == "mouse" and profile == "cards" else {}
        keys = self._pad_keys(front) if profile == "game" else {}      # E109; der Maus-Modus hat keine
        if self.input.route != route or (route == "mouse" and (getattr(self.input, "mouse_profile", profile) != profile
                                                               or getattr(self.input, "mouse_look", look) != look
                                                               or getattr(self.input, "mouse_layout", layout) != layout
                                                               or getattr(self.input, "mouse_keys", keys) != keys)):
            self.input.set_route(route, self.gs.game_display() if route == "mouse" else None, profile=profile, look=look,
                                 layout=layout, keys=keys)
            if route == "mouse" and profile == "mouse" and front.spec.mouse is True:
                self._mouse_hint(front)
            elif route == "mouse" and profile == "cards":
                self._cards_hint(front, layout, spots)
            elif route == "mouse" and profile == "opti":
                self._opti_hint(front)
        # Eigene Sprungstellen (E99) eigens: Sie ändern sich im Spiel (gemerkt, zurückgesetzt), und das
        # darf die Maus nicht neu aufbauen (ein gehaltener Klick fiele ab)
        set_spots = getattr(self.input, "set_spots", None)
        if set_spots is not None and route == "mouse" and getattr(self.input, "mouse_spots", spots) != spots:
            set_spots(spots)

        self.state.update(
            foreground=front.entry_id if front is not None else None,
            overlay=mode,
            # Der gewollte Maus-Modus der App vorn, nicht die gerade geltende Route: Unter dem
            # Control Center ist die Route "overlay", und sein Schalter muss den Stand kennen
            mouseMode=bool(front is not None and self._real_mouse(front)),
            visible=front is None or mode != HIDDEN,
            # Schoner offen (E99): Er ist passiv wie eine Mitteilung, deckt das Home aber ganz zu;
            # die Home-Seite gilt dann als unsichtbar und ruht (core/store.js)
            saver=self.saver_open(),
        )
        self._emit_running()

    def _freeze(self, app: Any) -> None:
        try:
            self.input.rumble("stop")   # sonst brummt das Pad weiter, während das Spiel steht
        except Exception:
            _log.exception("rumble stop")
        self.procs.freeze(app)

    # ---------------------------------------------------------------- neutral + 150 ms
    def _begin_settle(self) -> None:
        now = self.sched.now()
        self._settle_started = now
        self._neutral_since = now if self._neutral() else None
        self._settle_wait(now)

    def _settle_delay(self) -> float:
        return float(self.config.get("shell.resume_delay_ms", 150)) / 1000.0

    def _settle_wait(self, now: float) -> None:
        # im Takt nachsehen; die letzte Prüfung liegt genau am Ende der Wartezeit
        wait = float(SETTLE_POLL_MS)
        if self._neutral_since is not None:
            left = (self._neutral_since + self._settle_delay() - now) * 1000.0
            wait = min(wait, max(1.0, left))
        self._settle_timer = self.sched.later(wait, self._settle_tick)

    def _settle_tick(self) -> None:
        self._settle_timer = None
        if self._key is None or self._settled:
            return
        now = self.sched.now()
        if self._neutral():
            if self._neutral_since is None:
                self._neutral_since = now
            if now - self._neutral_since >= self._settle_delay() - 1e-6:
                self._settle_done()
                return
        else:
            self._neutral_since = None
        if now - self._settle_started >= SETTLE_MAX_S:
            _log.warning("Pad wird nicht neutral; die App läuft trotzdem weiter")
            self._settle_done()
            return
        self._settle_wait(now)

    def _settle_done(self) -> None:
        self._settled = True
        front = self._effective_front()
        if front is not None and front.frozen and self.focus.overlay_mode != ACTIVE:
            self.procs.thaw(front)
            self._thawed_at[front.app_id] = self.sched.now()
        self._apply()
        self._key_when_running()

    # ================================================================ Ansichten im Overlay
    def open_view(self, name: str, params: dict | None = None, mode: str = ACTIVE,
                  takes_input: bool = True, app_id: int | None = None,
                  on_close: Callable[[Any], None] | None = None) -> View:
        top = self._top()
        if top is not None and top.name == name and name != "confirm":
            top.params = dict(params or {})         # dieselbe Ansicht: nur neue Angaben
            view = top
        else:
            view = View(name, dict(params or {}), mode, takes_input, app_id, on_close,
                        serial=next(self._serials))
            self._views.append(view)
        self._end_hold()
        self._emit_open(view)
        self._apply()
        return view

    def _emit_open(self, view: View) -> None:
        self.rpc.emit("overlay.open", {"view": view.name, "params": view.params,
                                       "serial": view.serial}, target="overlay")

    def close_view(self, view: View | None = None, result: Any = None, hold: bool = True) -> None:
        """Schließt eine Ansicht (Vorgabe: die oberste). hold=False, wenn die Seite selbst
        gemeldet hat, dass sie fertig ist."""
        view = view or self._top()
        if view is None or view not in self._views:
            return
        self._remove_views([view], result, hold)
        self._apply()

    def _remove_views(self, views: list[View], result: Any = None, hold: bool = True) -> None:
        if not views:
            return
        old_top = self._top()
        for view in views:
            if view not in self._views:
                continue
            self._views.remove(view)
            self.sched.cancel(view.timer)
            if view is self._osk:
                self._osk = None
            if view.on_close:
                try:
                    view.on_close(result)
                except Exception:
                    _log.exception("on_close von %s", view.name)
        top = self._top()
        if top is old_top:
            return
        if top is not None:
            self._emit_open(top)
        else:
            self.rpc.emit("overlay.close", {}, target="overlay")
            if hold and old_top is not None:
                self._hold_mode = old_top.mode
                self.sched.cancel(self._hold_timer)
                self._hold_timer = self.sched.later(CLOSE_GRACE_MS, self._hold_over)

    def _drop_views(self, keep: Callable[[View], bool] = lambda v: False, hold: bool = True) -> None:
        self._remove_views([v for v in self._views if not keep(v)], None, hold)

    def _end_hold(self) -> None:
        self.sched.cancel(self._hold_timer)
        self._hold_timer = None
        self._hold_mode = None

    def _hold_over(self) -> None:
        self._hold_timer = None
        self._hold_mode = None
        self._apply()

    def page_ready(self, role: str) -> None:
        """Eine Seite hat (wieder) ihr erstes Bild gemeldet. Nach einem Absturz des
        Web-Prozesses weiß die Overlay-Seite nicht mehr, was sie zeigen soll."""
        top = self._top()
        if role == "overlay" and top is not None:
            self._emit_open(top)

    def page_closed(self, name: str | None = None, serial: Any = None) -> None:
        """RPC overlay.close: Die Seite meldet, dass sie fertig ist. Sie nennt Namen und
        laufende Nummer der Ansicht: Eine verspätete Meldung zu einer schon geschlossenen
        Ansicht darf keine gleichnamige, inzwischen neu geöffnete schließen."""
        if not self._views and self._hold_mode is not None:
            self._end_hold()            # Antwort auf unser overlay.close: Fenster jetzt verstecken
            self._apply()
            return
        top = self._top()
        if top is None or (name and name != top.name):
            return
        if serial is not None and serial != top.serial:
            return
        self.close_view(top, None, hold=False)

    # ---------------------------------------------------------------- Rückfragen
    def confirm(self, title: str, text: str = "", buttons: list[dict] | None = None,
                default: Any = "no", timeout: float = 0, app_id: int | None = None,
                extra: dict | None = None) -> Future:
        """Rückfrage im Overlay. Das Future liefert den Wert des gewählten Knopfs; Kreis,
        PS-Taste und Zeitablauf liefern default. extra: zusätzliche Angaben für die Seite
        (z. B. powerKey: die Rückfrage des Einschaltknopfs, E38)."""
        fut: Future = Future()
        cid = "c%d" % next(self._ids)
        buttons = buttons or [{"value": "yes", "label": "Ja"}, {"value": "no", "label": "Nein"}]

        def closed(result: Any) -> None:
            if not fut.done():
                fut.set_result(default if result is None else result)
        params = {**(extra or {}), "id": cid, "title": title, "text": text, "buttons": buttons,
                  "default": default, "timeout": timeout}
        view = self.open_view("confirm", params, mode=ACTIVE, takes_input=True, app_id=app_id,
                              on_close=closed)
        if timeout and timeout > 0:
            view.timer = self.sched.later(timeout * 1000, lambda: self.close_view(view, default))
        fut.cid = cid                               # type: ignore[attr-defined]
        return fut

    def confirm_result(self, cid: str, value: Any) -> None:
        for view in self._views:
            if view.name == "confirm" and view.params.get("id") == cid:
                self.close_view(view, value, hold=False)
                return

    # ---------------------------------------------------------------- Mitteilungen
    def on_toast(self, item: dict | None = None) -> None:
        """Mitteilung: Overlay passiv zeigen, solange sie steht (keine Eingabe, App läuft weiter)."""
        self._show_passive(float(self.config.get("notify.seconds", 5)) + 0.8)   # plus Aus-Animation

    def _show_passive(self, seconds: float) -> None:
        """Overlay für seconds sichtbar halten, ohne Eingabe und ohne die App anzuhalten."""
        until = self.sched.now() + seconds
        if until <= self._toast_until:
            self._apply()                   # eine längere Einblendung läuft schon
            return
        self._toast_until = until
        self.sched.cancel(self._toast_timer)
        self._toast_timer = self.sched.later(seconds * 1000 + 20, self._toast_over)
        self._apply()

    def _toast_over(self) -> None:
        self._toast_timer = None
        self._apply()

    def _mouse_hint(self, app: Any) -> None:
        """Beim allerersten Mal im Maus-Modus (Browser, Desktop, EmuDeck) erfährt die Seite, dass
        sie die Belegung erklären soll: Ereignis hint {id: "mouse-mode"}. Gemerkt wird das in
        den Einstellungen, damit der Hinweis nicht bei jedem Start wiederkommt."""
        if self._mouse_hinted:
            return
        self._mouse_hinted = True
        try:
            if self.config.get("hints.mouse_mode_seen", False):
                return
            self.rpc.emit("hint", {"id": HINT_MOUSE, "entryId": app.entry_id,
                                   "seconds": HINT_SECONDS})
            self.config.set("hints.mouse_mode_seen", True)
            self._show_passive(HINT_SECONDS + 0.8)
        except Exception:
            _log.exception("Hinweis zum Maus-Modus")

    def _cards_hint(self, app: Any, layout: str, spots: dict | None = None) -> None:
        """Zeiger-Modus (E97): Bei den ersten Starts eines Spiels zeigt die Seite die Belegung
        (Ereignis hint {id: "cards-mode", layout, spots?}); gezählt je Spiel in hints.cards_seen.
        spots: Tasten mit eigener Sprungstelle (E99), nur wenn es welche gibt."""
        if app.app_id in self._cards_hinted:
            return
        self._cards_hinted.add(app.app_id)
        try:
            seen = self.config.get("hints.cards_seen", {})
            seen = seen if isinstance(seen, dict) else {}
            count = int(seen.get(app.entry_id, 0) or 0)
            if count >= CARDS_HINT_STARTS:
                return
            hint = {"id": HINT_CARDS, "entryId": app.entry_id, "layout": layout, "seconds": HINT_CARDS_SECONDS}
            if spots:
                hint["spots"] = sorted(spots)
            self.rpc.emit("hint", hint)
            self.config.set("hints.cards_seen", {**seen, app.entry_id: count + 1})
            self._show_passive(HINT_CARDS_SECONDS + 0.8)
        except Exception:
            _log.exception("Hinweis zum Zeiger-Modus")

    def _opti_hint(self, app: Any) -> None:
        """OptiScaler-Menü (E118): Die Seite zeigt die Belegung (Ereignis hint {id: "opti-mode"}), die ersten
        OPTI_HINT_TIMES Male; gezählt in hints.opti_seen."""
        try:
            count = int(self.config.get("hints.opti_seen", 0) or 0)
            if count >= OPTI_HINT_TIMES:
                return
            self.rpc.emit("hint", {"id": HINT_OPTI, "entryId": app.entry_id, "seconds": HINT_SECONDS})
            self.config.set("hints.opti_seen", count + 1)
            self._show_passive(HINT_SECONDS + 0.8)
        except Exception:
            _log.exception("Hinweis zum OptiScaler-Menü")

    # ================================================================ Eingabe
    def on_spot(self, name: str, spot: dict) -> None:
        """Zeiger-Modus (E99): Create gehalten + Sprungtaste hat die Zeigerstelle gemerkt. Je Spiel als
        Option pad_spots speichern (gilt ab sofort und bei jedem Start) und kurz bestätigen."""
        from .library.model import pad_spots_value
        front = self._front_app()
        if front is None or not front.entry_id or self.input.route != "mouse" or self._pad_profile(front) != "cards":
            self._spots_back()
            return
        try:
            value = pad_spots_value({**self._pad_spots(front), name: spot})
            self.library.set_option(front.entry_id, "pad_spots", value)
        except (KeyError, ValueError) as e:
            _log.warning("Sprungstelle %s für %s nicht gespeichert: %s", name, front.entry_id, e)
            self._spots_back()
            return
        notify = getattr(self.app, "notify", None)
        if notify is not None:
            try:
                # important: Antwort auf einen eigenen Griff, auch wenn Mitteilungen im Spiel aus sind
                notify.push("Sprungstelle für %s gemerkt" % SPOT_LABELS.get(name, name), self._app_title(front),
                            icon="gamepad", important=True)
            except Exception:
                _log.exception("Mitteilung zur Sprungstelle")

    def _spots_back(self) -> None:
        """Nicht gespeichert: Der Eingabedienst hat die Stelle schon übernommen und bekommt den Stand
        zurück, den _apply_once ihm gäbe (die Bibliothek bzw. keine). Sonst spränge R1 an eine Stelle,
        die nirgends steht und beim nächsten Start fehlt (E99)."""
        set_spots = getattr(self.input, "set_spots", None)
        if set_spots is None:
            return
        front = self._effective_front()
        cards = front is not None and self.input.route == "mouse" and self._pad_profile(front) == "cards"
        try:
            set_spots(self._pad_spots(front) if cards else {})
        except Exception:
            _log.exception("Sprungstellen zurückgeben")

    def on_button(self, button: str, repeat: bool = False) -> None:
        route = self.input.route
        if route in ("home", "overlay"):
            self.rpc.emit("pad", {"button": button, "repeat": bool(repeat)}, target=route)
        elif route == "mouse" and button == "triangle" and not repeat:
            # Im Maus-Modus reicht der Eingabedienst nur Dreieck durch: Bildschirmtastatur
            # für die App vorn (Browser, Desktop; Bauplan E14)
            try:
                self.osk_open("app")
            except RpcError as e:
                _log.info("Bildschirmtastatur: %s", e.message)
        elif route == "mouse" and button == "options" and not repeat:
            # OptiScaler-Menü (E118): Options hat Einfg gedrückt und das Menü geschlossen
            front = self._effective_front()
            if front is not None and front.app_id in self._opti:
                self._opti.discard(front.app_id)
                self._apply()

    def on_ps(self, kind: str, progress: float = 0.0) -> None:
        if kind in ("short", "long") and self.ending:
            # Die Tafel „Die Konsole wird ausgeschaltet …“ sperrt die Eingabe: PS schloss sonst das
            # Control Center samt Tafel bzw. führte das Home darunter zurück (es wurde bedienbar)
            _log.info("PS %s übergangen: die Konsole wird beendet", kind)
            return
        if kind == "short":
            self.rpc.emit("ps", {"kind": "short"})
            self._ps_short()
        elif kind == "long":
            self.rpc.emit("ps", {"kind": "long"})
            self._ps_long()
        elif kind == "options":
            self._ps_options(float(progress))
        elif kind == "options-done":            # der Eingabedienst meldet das Ende der 3 s eigens
            self._ps_options(1.0)
        elif kind == "options-cancel":
            self._ps_options_cancel()
        elif kind in ("create", "create-solo"):
            # Bildschirmfoto (capture.py): PS + Create bzw. Create allein, in jeder Route
            if self.capture is not None:
                self.capture(kind)

    def front_title(self) -> str | None:
        """Titel der App vorn (für Dateinamen), None im Home."""
        front = self._front_app()
        return self._app_title(front) if front is not None else None

    def _ps_short(self) -> None:
        top = self._top()
        if top is not None and top.takes_input:
            self.close_view(top)                    # PS schließt, was offen ist (Rückfrage = Vorgabe)
            return
        self.open_control_center()

    def open_control_center(self, section: str = "", by_power_key: bool = False) -> None:
        front = self._effective_front()
        self._drop_views(lambda v: v.name != "quit-progress", hold=False)
        params: dict = {"entryId": front.entry_id if front is not None else None}
        if section:
            params["section"] = section
        if by_power_key:
            params["byPowerKey"] = True
        self.open_view("cc", params, mode=ACTIVE, takes_input=True,
                       app_id=front.app_id if front is not None else None)

    # ------------------------------------------------------------ Bildschirmschoner (idle.py)
    def open_saver(self) -> bool:
        """Passiv über Home oder Spiel (das Spiel läuft weiter, E82), nimmt aber das Pad: Der erste
        Druck schließt ihn. evdev-/joydev-Leser bekommen ihn nicht (Griff), hidraw-Leser wie Proton
        im Controller-Modus „direkt“ schon (E56). Nie über Rückfragen, Tastatur, Ein/Aus."""
        top = self._top()
        if self.ending or (top is not None and top.name not in ("cc", "saver")):
            return False
        front = self._effective_front()
        # Über dem Control Center bleibt es aktiv: Sonst taute der Schoner das Spiel auf, das
        # unter dem Control Center steht, und es liefe unbeaufsichtigt weiter (E99)
        mode = ACTIVE if top is not None and top.mode == ACTIVE else PASSIVE
        self.open_view("saver", {}, mode=mode, takes_input=True,
                       app_id=front.app_id if front is not None else None)
        return True

    def close_saver(self) -> None:
        views = [v for v in self._views if v.name == "saver"]
        if views:
            self._remove_views(views, None, False)
            self._apply()

    def saver_open(self) -> bool:
        return any(v.name == "saver" for v in self._views)

    def front_kind(self) -> str | None:
        """Art der App vorn (LaunchSpec.kind), None im Home."""
        front = self._front_app()
        return getattr(getattr(front, "spec", None), "kind", None) if front is not None else None

    def key_menu_open(self) -> bool:
        """Steht das Ein/Aus-Menü, das der Einschaltknopf geöffnet hat, oben (E38)?"""
        top = self._top()
        return top is not None and top.name == "cc" and bool(top.params.get("byPowerKey"))

    def _ps_long(self) -> None:
        if self._front_app() is not None:
            self.go_home()
        else:
            self._drop_views()                      # Home ist schon vorn: nur Menüs schließen
            self._apply()

    def _ps_options(self, progress: float) -> None:
        front = self._effective_front()
        top = self._top()
        if front is None or (top is not None and top.takes_input):
            return
        progress = max(0.0, min(1.0, progress))
        if top is None or top.name != "quit-progress":
            self.open_view("quit-progress", {"entryId": front.entry_id,
                                             "title": self._app_title(front), "progress": progress},
                           mode=PASSIVE, takes_input=False, app_id=front.app_id)
        self.rpc.emit("ps", {"kind": "options", "progress": progress})
        if progress >= 1.0 and front.app_id not in self._closing:
            self.close_app(front)

    def _ps_options_cancel(self) -> None:
        self.rpc.emit("ps", {"kind": "options", "progress": 0.0, "cancelled": True})
        self._remove_views([v for v in self._views if v.name == "quit-progress"
                            and v.app_id not in self._closing])
        self._apply()

    # ------------------------------------------------------------ nach dem Aufwachen
    RESUME_WINDOW_MS = 30_000
    RESUME_HINT_MS = 90_000

    def after_resume(self) -> None:
        """Aufgewacht (idle.ResumeWatch). Beim Aufwachen startet der PC alle USB-Geräte neu, auch
        den Bluetooth-Chip; die Verbindung, mit der die PS-Taste geweckt hat, reißt dabei ab und
        der Controller geht aus (2026-10-21). Ohne Controller deshalb ein Hinweis, PS noch einmal
        zu drücken; das Pad kann auch erst ein paar Sekunden nach dem Aufwachen wegfallen."""
        self._resume_until = self.sched.now() + self.RESUME_WINDOW_MS / 1000
        self.sched.later(4000, self._resume_check)

    def _resume_check(self) -> None:
        if self._pads() or self.ending or any(v.name == "disconnected" for v in self._views):
            return
        top = self._top()
        if top is not None and top.takes_input:
            return
        self.open_view("disconnected", {"resume": True, "connected": False}, mode=PASSIVE, takes_input=False)
        self.sched.later(self.RESUME_HINT_MS, self._resume_hint_done)

    def _resume_hint_done(self) -> None:
        views = [v for v in self._views if v.name == "disconnected" and v.params.get("resume")]
        if views:
            self._remove_views(views, None, False)
            self._apply()

    def on_pads_changed(self) -> None:
        pads = self._pads()
        self.state.update(pads=pads)
        front = self._effective_front()
        if front is not None and set(self._known_pads) - {p.get("id") for p in pads}:
            self._pad_dropped(front)
        shown = next((v for v in self._views if v.name == "disconnected"), None)
        if shown is not None and shown.params.get("resume") and pads:
            # nach dem Aufwachen wieder verbunden: Hinweis weg, die Mitteilung „Controller verbunden“ genügt
            self._remove_views([shown], None, False)
            self._apply()
            shown = None
        elif not pads and self.sched.now() < self._resume_until and shown is None:
            self.sched.later(1500, self._resume_check)       # erst nach dem Aufwachen weggefallen
        if not pads and self._had_pads and front is not None and front.spec.kind in GAME_KINDS:
            # letztes Pad weg, Spiel vorn (Befund 24): anhalten und Hinweis zeigen
            if shown is None and (self._top() is None or not self._top().takes_input):
                self._drop_views(hold=False)
                self.open_view("disconnected", {"entryId": front.entry_id, "connected": False},
                               mode=ACTIVE, takes_input=True, app_id=front.app_id)
        elif pads and shown is not None:
            shown.params = {**shown.params, "connected": True}
            if shown is self._top():
                self._emit_open(shown)
        self._had_pads = bool(pads)
        self._pad_toasts(pads, quiet=any(v.name == "disconnected" for v in self._views))

    def _pad_dropped(self, app: Any) -> None:
        """Ein Pad ist weg, während app vorn ist (E119). Lief sie gerade erst nach dem Anhalten weiter (nicht nach
        dem Aufwachen: Dann fallen die Pads ohnehin ab), zählt das für ihr Spiel; ab PAD_DROP_LIMIT schaltet die
        Konsole „Im Hintergrund anhalten“ dafür aus (Option freeze, wie in den Spieleinstellungen) und sagt es."""
        now = self.sched.now()
        at = self._thawed_at.pop(app.app_id, None)
        if (at is None or now - at > PAD_DROP_WINDOW_S or now < self._resume_until
                or app.spec.kind not in GAME_KINDS or not self._may_freeze(app)):
            return
        count = self._pad_drops.get(app.entry_id, 0) + 1
        self._pad_drops[app.entry_id] = count
        _log.info("Controller %.1f s nach dem Fortsetzen von %s getrennt (%d. Mal)", now - at, app.entry_id, count)
        if count < PAD_DROP_LIMIT:
            return
        try:
            self.library.set_option(app.entry_id, "freeze", False)
        except (KeyError, ValueError) as e:
            _log.warning("Anhalten für %s nicht abgeschaltet: %s", app.entry_id, e)
            return
        notify = getattr(self.app, "notify", None)
        if notify is not None:
            notify.push("Spiel wird nicht mehr angehalten",
                        "Nach dem Fortsetzen von „%s“ hat sich der Controller mehrmals getrennt. Es läuft deshalb unter "
                        "dem Overlay weiter (Spieleinstellungen › Im Hintergrund anhalten)." % self._app_title(app),
                        icon="gamepad", important=True)

    def _pad_toasts(self, pads: list, quiet: bool) -> None:
        """Mitteilungen zu den Pads: verbunden, getrennt, Akku unter 15 % (Befund 24).
        quiet: Der Hinweis "Controller getrennt" steht schon groß im Overlay."""
        notify = getattr(self.app, "notify", None)
        now = {p.get("id"): p for p in pads}
        before = self._known_pads
        self._known_pads = {pid: str(p.get("name", "")) for pid, p in now.items()}
        if notify is None:
            return
        for pid, pad in now.items():
            name = str(pad.get("name", ""))
            if pid not in before and not quiet:
                notify.push("Controller verbunden", name, icon="gamepad")
            battery = pad.get("battery")
            if isinstance(battery, (int, float)) and not pad.get("charging"):
                if battery <= 15 and pid not in self._low_battery:
                    self._low_battery.add(pid)
                    notify.push("Akku des Controllers fast leer", "%s: noch %d %%" % (name, battery),
                                icon="battery")
                elif battery >= 25:
                    self._low_battery.discard(pid)
            else:
                self._low_battery.discard(pid)
        for pid, name in before.items():
            if pid not in now:
                self._low_battery.discard(pid)
                if not quiet:
                    notify.push("Controller getrennt", name, icon="gamepad")

    # ================================================================ Vordergrund
    def go_home(self) -> None:
        """Zurück zum Home; die App bleibt (angehalten) im Hintergrund."""
        self._front_id = None
        self._set_detached(False)
        self._drop_views()
        self.focus.set_foreground(None)
        self._apply()

    def focus_app(self, app: Any) -> None:
        """Kachel einer laufenden App: nach vorn, weiter erst bei neutralem Pad."""
        if app.app_id in self._launching:
            return                          # lädt noch: kommt von selbst nach vorn
        if app.app_id in self._closing:
            # Sonst liefe das Spiel noch ein paar Sekunden weiter und würde dann hart beendet
            raise RpcError("„%s“ wird gerade beendet." % self._app_title(app), "Closing")
        self._bring_front(app)

    def _bring_front(self, app: Any) -> None:
        self._front_id = app.app_id
        self._set_detached(False)
        self._shown.add(app.app_id)
        if app.entry_id in self._repairing:
            self._repairing.discard(app.entry_id)       # Reparatur geglückt: der Weg bleibt gemerkt
            _log.info("Startreparatur: %s läuft wie in Lutris, der Weg bleibt eingestellt", app.entry_id)
        self._drop_views(lambda v: v.name == "confirm")
        self.focus.set_foreground(app.app_id)
        self._key = None                    # auch bei derselben App neu auf ein neutrales Pad warten
        self._apply()

    def _set_detached(self, value: bool) -> None:
        self.sched.cancel(self._detach_timer)
        self._detach_timer = None
        self._detached = value

    def _check_reality(self) -> None:
        """Zeigt Gamescope das Home, obwohl eine App vorn sein sollte (ihr Fenster ist weg, sie
        lebt aber noch), wird das Home bedienbar. Die Reihenfolge an der Wurzel bleibt: Kommt
        ein neues Fenster der App, ist sie von selbst wieder vorn."""
        app = self._front_app()
        if app is None:
            if self._detached or self._detach_timer:
                self._set_detached(False)
            return
        try:
            focused = self.gs.focused_app()
        except Exception:
            _log.exception("gs.focused_app")
            return
        home_focused = focused == HOME_APPID
        if home_focused and self.focus.overlay_mode != ACTIVE:
            if self._detached:
                pass
            elif app.app_id in self._closing:
                # Wir haben ihr Ende verlangt und ihr Fenster ist weg: Da kommt kein neues
                # mehr, auf das zu warten wäre. Das Home ist sofort bedienbar, auch wenn der
                # Prozess noch aufräumt. (Was beim Wechsel noch gedrückt ist, gilt im Home
                # erst nach dem Loslassen: Durchrutsch-Schutz des Eingabedienstes.)
                _log.info("%s wird beendet und hat kein Fenster mehr; Home ist bedienbar", app.entry_id)
                self._set_detached(True)
                self._apply()
            elif self._detach_timer is None:
                self._detach_timer = self.sched.later(DETACH_MS, self._detach_now)
        elif focused and self.focus.overlay_mode != ACTIVE:
            # Ein Fenster der App ist wieder vorn. Unter einem aktiven Overlay steht FOCUSED_APP auf
            # 0 (STEAM_INPUT_FOCUS=1, docs/research/01): das heißt nichts, sonst bände das Control
            # Center eine losgelöste App wieder an, und nach dem Schließen wäre das Home 1,5 s taub (E99)
            was = self._detached
            self._set_detached(False)
            if was:
                self._key = None
                self._apply()

    def _detach_now(self) -> None:
        self._detach_timer = None
        app = self._front_app()
        try:
            still = app is not None and self.gs.focused_app() == HOME_APPID
        except Exception:
            still = False
        if still and self.focus.overlay_mode != ACTIVE:
            _log.info("%s hat kein Fenster mehr; Home ist bedienbar", app.entry_id)
            self._detached = True
            self._apply()

    # ================================================================ Dienste melden sich
    def _on_focus(self) -> None:
        self._check_reality()
        self._apply()

    def _on_gs(self, what: str) -> None:
        if what == "apps":
            visible = set(self.gs.focusable_apps())
            for app_id in list(self._launching):
                app = self.procs.by_app_id(app_id)
                if app is not None and app_id in visible:
                    self._on_visible(app)
        if what in ("apps", "focus"):
            self._check_reality()

    def _on_proc(self, app: Any, what: str) -> None:
        if what == "visible":
            self._on_visible(app)
        elif what == "exited":
            self._on_exited(app)
        else:
            self._apply()

    def _on_visible(self, app: Any) -> None:
        waits = getattr(self.focus, "waits_for_game", None)
        if app.app_id in self._launching and waits is not None and waits(app.app_id):
            return                          # nur Fenster des Launchers (verborgen): das Ladebild bleibt
        launch = self._launching.pop(app.app_id, None)
        if launch is None:
            return                          # weiteres Fenster einer laufenden App: nichts umschalten
        self.sched.cancel(launch.timer)
        if app.app_id in self._closing:
            self._emit_launching(launch.entry_id, "cancelled")
            return                          # wurde während des Ladens abgebrochen
        self._emit_launching(launch.entry_id, "done")
        self._bring_front(app)

    def _on_exited(self, app: Any) -> None:
        launch = self._launching.pop(app.app_id, None)
        self._freeze_opts.pop(app.app_id, None)
        self._freeze_wait.pop(app.app_id, None)
        # Ein Ende, das wir selbst verlangt haben, ist nie ein Fehler, egal mit welchem
        # Rückgabewert und nach welcher Laufzeit der reaper endet (gesehen: 241 nach SIGTERM
        # an umu-run, 143, 137). app.closing setzt der AppManager bei close() und kill(): So
        # zählt auch ein Schließen, das an der Shell vorbei direkt dort verlangt wurde.
        requested = app.app_id in self._closing or bool(getattr(app, "closing", False))
        self._closing.discard(app.app_id)
        title = self._app_title(app)
        if launch is not None:
            self.sched.cancel(launch.timer)
            if requested:
                self._emit_launching(launch.entry_id, "cancelled")
            elif self._try_repair(app, title):
                pass                                    # neuer Start „wie in Lutris“ läuft gleich an
            else:
                self._emit_launching(launch.entry_id, "failed")
                self._emit_error(app, title, self._own_words(app) or "„%s“ wurde beendet, bevor ein Bild kam." % title,
                                 CODE_NO_WINDOW, PHASE_START)
        elif not requested and getattr(app, "exit_code", None) not in (0, None):
            # Das Ladebild endete schon (aufgedeckter Launcher, Steams Fenster), der Starthelfer gab
            # danach auf: Sein Satz betrifft den Start (couch-launcher-game nach 15 Minuten ohne Spiel,
            # E99). Ohne Satz gilt wie bisher nur ein Absturz als Fehler
            words = self._own_words(app)
            if words:
                self._emit_error(app, title, words, CODE_GENERIC, PHASE_START)
            elif self._looks_crashed(app):
                self._emit_error(app, title, "„%s“ wurde unerwartet beendet." % title,
                                 CODE_CRASHED, PHASE_RUN)
        if app.app_id in self._shown:
            self._shown.discard(app.app_id)
            try:
                self.library.record_play(app.entry_id, int(self._runtime(app)))
            except Exception:
                _log.exception("record_play")
        self._mouse.pop(app.app_id, None)
        self._reveal_mouse.pop(app.app_id, None)
        self._opti.discard(app.app_id)
        self._thawed_at.pop(app.app_id, None)
        self._remove_views([v for v in self._views if v.app_id == app.app_id], None, hold=False)
        back = self.procs.by_app_id(self._return_to.pop(app.entry_id, 0) or 0)
        if self._front_id == app.app_id and back is not None and back.app_id not in self._closing:
            self._bring_front(back)         # Browser einer Launcher-Anmeldung zu: zurück in den Launcher
        elif self._front_id == app.app_id:
            self._front_id = None           # Gamescope zeigt von selbst das Home
            self._set_detached(False)
        # Liste an der Wurzel neu schreiben, damit die beendete App nicht darin stehen bleibt
        self.focus.set_foreground(self._front_id)
        self._apply()
        for waiter in list(self._waiters):
            waiter["ids"].discard(app.app_id)
            if not waiter["ids"]:
                self._finish_waiter(waiter)

    @staticmethod
    def _own_words(app: Any) -> str:
        """Der Satz, mit dem ein Starthelfer sein Aufgeben erklärt (Zeile "MELDUNG: …" am Ende
        seines Protokolls, couch-steam-game), sonst ""."""
        path = getattr(app, "log_path", None) or paths.app_log(app.entry_id)
        for line in reversed(log.tail(path, 20).splitlines()):
            if line.startswith(OWN_WORDS):
                return line[len(OWN_WORDS):].strip()[:300]
        return ""

    def _try_repair(self, app: Any, title: str) -> bool:
        """Startreparatur (E55): Ein Lutris-Spiel, das unter Proton endet, bevor ein Bild kam,
        startet sofort noch einmal „wie in Lutris“ (Lutris' Runner und Prefix). Der Weg wird
        gleich gespeichert und gilt ab dann; scheitert auch er, geht die Option zurück und der
        Fehler kommt wie sonst. True = ein neuer Start ist angestoßen."""
        from .launch import lutris_way
        entry = self.library.get(app.entry_id) if self.library is not None else None
        if entry is None or not lutris_way.possible(entry):
            return False
        if entry.option(lutris_way.OPTION) == "lutris":
            # Auch der Lutris-Weg kam ohne Bild: zurück auf Proton, auch wenn die Reparatur aus einer
            # früheren Sitzung des Home stammt (sonst blieb das Spiel für immer auf Lutris, 2026-10-24)
            self._undo_repair(app.entry_id)
            _log.warning("Startreparatur: %s startete auch wie in Lutris nicht, wieder Proton", app.entry_id)
            return False
        if lutris_way.lutris_command() is None:
            return False
        try:
            self.library.set_option(app.entry_id, lutris_way.OPTION, "lutris")
        except Exception:
            _log.exception("Startreparatur: Option setzen")
            return False
        self._repairing.add(app.entry_id)
        _log.warning("Startreparatur: %s endete unter Proton ohne Bild (Rückgabewert %s, %.1f s); "
                     "neuer Start wie in Lutris", app.entry_id, getattr(app, "exit_code", None), self._runtime(app))
        notify = getattr(self.app, "notify", None)
        if notify is not None:
            try:
                notify.push("Startreparatur", "„%s“ startete mit Proton nicht. couch startet es jetzt "
                            "so wie Lutris und merkt sich das." % title, icon="info")
            except Exception:
                _log.exception("Mitteilung der Startreparatur")
        entry_id = app.entry_id

        def again() -> None:
            try:
                self.launch(entry_id, confirmed=True)
            except Exception as e:                    # z. B. Lutris lieferte kein Skript
                self._undo_repair(entry_id)
                self._emit_launching(entry_id, "failed")
                self.report_launch_error(entry_id, e)
        self.sched.later(300, again)
        return True

    def _undo_lutris_way(self, entry_id: str) -> None:
        """Kein Bild in der Frist auf dem Lutris-Weg: beim nächsten Start wieder Proton (E55)."""
        from .launch import lutris_way
        entry = self.library.get(entry_id) if self.library is not None else None
        if entry is not None and lutris_way.wanted(entry):
            _log.warning("Startreparatur: %s kam auch wie in Lutris nicht in der Frist, wieder Proton", entry_id)
            self._undo_repair(entry_id)

    def _undo_repair(self, entry_id: str) -> None:
        """Startreparatur zurücknehmen (E55): Auch der Lutris-Weg gab keinen Start, die Option geht
        auf "auto" zurück. Sonst liefe jeder weitere Start dieses Spiels wieder über Lutris."""
        from .launch import lutris_way
        self._repairing.discard(entry_id)
        try:
            self.library.set_option(entry_id, lutris_way.OPTION, "auto")
        except Exception:
            _log.exception("Startreparatur zurücknehmen: %s", entry_id)

    @staticmethod
    def _runtime(app: Any) -> float:
        runtime = getattr(app, "runtime", 0) or 0
        return float(runtime) if runtime > 0 else max(0.0, time.time() - app.started)

    def _looks_crashed(self, app: Any) -> bool:
        """Viele Spiele enden auch beim normalen Verlassen mit einem Rückgabewert ungleich 0.
        Als Absturz gilt deshalb nur ein Ende durch ein Fehlersignal oder ein Ende mit Fehler
        gleich nach dem Start."""
        code = getattr(app, "exit_code", None)
        if code in (0, None):
            return False
        if code in CRASH_CODES:
            return True
        return self._runtime(app) < EARLY_EXIT_S

    # ================================================================ Starten
    def _games_running(self, except_entry: str = "") -> list:
        return [a for a in self.procs.running()
                if a.spec.kind in GAME_KINDS and a.entry_id != except_entry
                and a.app_id not in self._closing]

    def launch(self, entry_id: str, close_other: bool = False, confirmed: bool = False) -> Any:
        """Startet einen Eintrag oder holt ihn nach vorn. Liefert {ok, appId},
        {needsConfirm: "close-other", other} oder ein Future darauf; LaunchError trägt den Text
        für die Oberfläche (und in .reason die Kennung der Ursache).

        Fehler, die sich erst beim Vorbereiten zeigen (Future), gehen zusätzlich als Ereignis
        app.error an die Seite. Was hier sofort geworfen wird, meldet der Aufrufer selbst: die
        RPC app.launch als app.error, andere Aufrufer (Einstellungen) auf ihre Weise."""
        entry = self.library.get(entry_id)
        if entry is None:
            raise LaunchError("Diesen Eintrag gibt es nicht mehr.", entry_id, reason="not-found")
        if entry_id in self._preparing:
            return {"ok": True, "pending": True}
        running = self.procs.by_entry(entry_id)
        if running is not None and running.app_id in self._closing:
            if self.ending:
                raise LaunchError("Die Konsole wird gerade beendet.", reason="ending")
            return self._launch_after_close(entry, running, close_other, confirmed)
        if running is not None:
            self.focus_app(running)
            return {"ok": True, "appId": running.app_id}
        if self.ending:
            raise LaunchError("Die Konsole wird gerade beendet.", reason="ending")
        if not entry.installed:
            raise LaunchError("„%s“ ist nicht installiert." % entry.title, reason="not-installed")
        if not entry.available:
            if "needs-exe" not in (entry.tags or ()):
                raise LaunchError("Die Dateien von „%s“ sind gerade nicht erreichbar. "
                                  "Ist die Spiele-Platte angeschlossen?" % entry.title,
                                  reason="unavailable")
            # Archiv-Spiel, bei dem der Installer sich nicht für eine Startdatei entscheiden
            # konnte: Das liegt nicht an der Platte. Ist inzwischen eine gewählt, prüft sie
            # der Starter; sonst öffnet die Seite auf diese Kennung hin die Auswahl.
            if not entry.option("exe"):
                raise LaunchError("Für „%s“ ist noch keine Startdatei gewählt." % entry.title,
                                  reason="needs-exe")
        # Steam-Spiele übergeben an Steam und schließen dabei ohnehin alles (eigene Rückfrage). Ein
        # Launcher-Spiel im Nachlauf fragt nicht: Auf sein Ende wartet _spec_ready (E99)
        others = [a for a in self._games_running(entry_id) if not self._after_game(a)] \
            if entry.kind == "game" and entry.source != "steam" else []
        if others and not close_other:
            return {"needsConfirm": "close-other", "other": self._entry_view(others[0])}

        result: Future = Future()
        self._preparing.add(entry_id)
        self._emit_launching(entry_id, "prepare", "„%s“ wird vorbereitet …" % entry.title)
        work = self._background(build_spec, entry, self.app)
        work.add_done_callback(lambda f: self.sched.soon(
            lambda: self._spec_ready(entry, f, close_other, confirmed, result)))
        return result

    def _launch_after_close(self, entry: Any, old: Any, close_other: bool, confirmed: bool) -> Future:
        """Der Eintrag wird gerade beendet (Schonfrist läuft) und soll neu starten: auf sein
        Ende warten, dann frisch starten. Die sterbende App wird nie nach vorn geholt."""
        result: Future = Future()
        self._preparing.add(entry.id)
        self._emit_launching(entry.id, "closing-other", "„%s“ wird noch beendet …" % entry.title)

        def again() -> None:
            self._preparing.discard(entry.id)
            if entry.id in self._cancel_wanted or self.ending:
                self._launch_cancelled(entry, result)
                return
            try:
                value = self.launch(entry.id, close_other, confirmed)
            except BaseException as e:
                self._launch_failed(entry, result, e)
                return
            if isinstance(value, Future):
                value.add_done_callback(lambda f: result.set_exception(f.exception())
                                        if f.exception() else result.set_result(f.result()))
            else:
                if not (isinstance(value, dict) and value.get("ok")):
                    # z. B. die Rückfrage „anderes Spiel schließen?“: Das Ladebild von eben
                    # („wird noch beendet“) hat sich damit erledigt
                    self._emit_launching(entry.id, "cancelled")
                result.set_result(value)
        self._when_gone([old.app_id], CLOSE_OTHER_S, then=again,
                        failed=lambda: self._launch_failed(
                            entry, result, LaunchError("„%s“ ließ sich nicht beenden." % entry.title,
                                                       reason="close-failed")))
        return result

    def _launch_failed(self, entry: Any, result: Future, exc: BaseException) -> None:
        if entry.id in self._repairing:               # Startreparatur: Lutris lieferte keinen Start
            self._undo_repair(entry.id)
        self._preparing.discard(entry.id)
        self._cancel_wanted.discard(entry.id)
        self._emit_launching(entry.id, "failed")
        # Der Aufrufer von eben ist längst zurück (Future): Die Seite erfährt den Grund als
        # Ereignis, mit Kennung (z. B. needs-exe -> Auswahl der Startdatei)
        self.report_launch_error(entry.id, exc, entry.title)
        if not result.done():
            result.set_exception(exc)

    def _launch_cancelled(self, entry: Any, result: Future) -> None:
        self._preparing.discard(entry.id)
        self._cancel_wanted.discard(entry.id)
        self._emit_launching(entry.id, "cancelled")
        if not result.done():
            result.set_result({"ok": False, "cancelled": True})

    def cancel_launch(self, entry_id: str) -> bool:
        """„Abbrechen“ im Ladebild, solange es noch keine App gibt: Rückfrage zur Übernahme
        verneinen, laufende Prefix-Kopie abbrechen, sonst den Start vormerken als abgebrochen.
        False, wenn für den Eintrag gerade nichts vorbereitet wird."""
        if entry_id not in self._preparing:
            return False
        cid = self._adopt_questions.get(entry_id)
        if cid:
            self.confirm_result(cid, "no")
            return True
        cancel = self._adopting.get(entry_id)
        if cancel is not None:
            cancel.set()                    # die Kopie räumt selbst auf und meldet AdoptCancelled
            self._emit_launching(entry_id, "prepare", "Die Übernahme wird abgebrochen …")
            return True
        self._cancel_wanted.add(entry_id)
        waiter = self._tail_waits.pop(entry_id, None)
        if waiter is not None:
            self._finish_waiter(waiter)     # wartet auf einen Nachlauf: gleich abbrechen, nicht erst danach
        return True

    def cancel_preparing(self) -> int:
        """Bricht alles ab, was gerade einen Start vorbereitet (Rückfragen, Prefix-Kopien,
        Starter im Arbeits-Thread). Vor Ausschalten und Übergabe und am Ende des Home: Eine
        Kopie darf das Prozessende nicht um Minuten aufhalten. Liefert, wie viele es waren."""
        entries = list(self._preparing)
        for cancel in list(self._adopting.values()):
            cancel.set()                    # zuerst und ohne Umweg: die Kopien
        for entry_id in entries:
            try:
                self.cancel_launch(entry_id)
            except Exception as e:
                # z. B. Gamescope ist schon weg und die Rückfrage lässt sich nicht mehr
                # schließen: Die übrigen Abbrüche dürfen daran nicht hängen bleiben
                _log.warning("Start von %s ließ sich nicht sauber abbrechen: %s", entry_id, e)
        return len(entries)

    def is_preparing(self, entry_id: str) -> bool:
        """Wird für diesen Eintrag gerade ein Start vorbereitet (Auftrag bauen, Rückfrage, Prefix-Kopie,
        Warten auf ein anderes Spiel)? Für die Verdrahtung statt des Felds _preparing (E99)."""
        return entry_id in self._preparing

    def adopting_count(self) -> int:
        """Prefix-Übernahmen, die gerade laufen oder auf ihre Rückfrage warten (für die
        Rückfrage vor Ausschalten, Neustart und Übergabe, E31)."""
        return len(set(self._adopting) | set(self._adopt_questions))

    def adopt_progress(self, entry_id: str, done: int, total: int) -> None:
        """Stand einer laufenden Prefix-Kopie, von der Verdrahtung aus der Hauptschleife
        gemeldet: geht als app.launching {phase: "prepare", text, percent} an das Ladebild."""
        cancel = self._adopting.get(entry_id)
        if cancel is None or cancel.is_set() or not total or total <= 0:
            return                          # keine Kopie (mehr) oder schon abgebrochen
        percent = max(0, min(100, int(100 * done / total)))
        if self._adopt_percent.get(entry_id) == percent:
            return
        self._adopt_percent[entry_id] = percent
        self._emit_launching(entry_id, "prepare", "%s %d %%" % (ADOPT_COPYING, percent), percent)

    def copy_sentence(self) -> str | None:
        """Satz für wiring.busy_checks: Läuft eine Prefix-Kopie, würde Ausschalten, Abmelden
        oder die Übergabe an Steam sie abbrechen."""
        if not self._adopting:
            return None
        entry_id = next(iter(self._adopting))
        percent = self._adopt_percent.get(entry_id)
        text = "Die Spielstände von „%s“ werden noch übernommen" % self._title(entry_id)
        if percent is not None:
            text += " (%d %%)" % percent
        more = len(self._adopting) - 1
        if more > 0:
            text += " und %d weitere" % more
        return text + "."

    def busy_sentences(self) -> list[str] | None:
        """Sätze aller busy_checks der Verdrahtung: was gerade läuft und durch Ausschalten,
        Abmelden oder die Übergabe an Steam unterbrochen würde (E31). None, wenn es keine
        Verdrahtung gibt (dann gilt, was die Shell selbst weiß)."""
        busy = getattr(getattr(self.app, "wiring", None), "busy", None)
        if not callable(busy):
            return None
        try:
            return [str(text) for text in busy() if text]
        except Exception:
            _log.exception("wiring.busy")
            return []

    def _spec_ready(self, entry: Any, work: Future, close_other: bool, confirmed: bool,
                    result: Future, adopted: bool = False, waited: bool = False) -> None:
        cancelled = entry.id in self._cancel_wanted or self.ending
        try:
            spec: LaunchSpec = work.result()
        except AdoptionRequired as e:
            # E24: nie ohne Rückfrage kopieren; nach einer Kopie nicht ein zweites Mal fragen
            if cancelled:
                self._launch_cancelled(entry, result)
            elif adopted or self.adopt_prefix is None:
                self._launch_failed(entry, result, e)
            else:
                self._ask_adoption(entry, e, close_other, confirmed, result)
            return
        except BaseException as e:
            self._launch_failed(entry, result, e)
            return
        if cancelled:
            self._launch_cancelled(entry, result)
            return
        if spec.handover is not None:
            self._preparing.discard(entry.id)
            self._handover_asked(entry, spec, confirmed, result)
            return
        others = self._games_running(entry.id) if spec.kind in GAME_KINDS else []
        # Launcher-Spiel im Nachlauf (E99): nicht fragen, sein Ende abwarten. Ohne SIGKILL: Ist es
        # danach noch da (oder hat es wieder ein Fenster), zählt es beim zweiten Durchgang wie jedes Spiel
        tail = [] if waited else [a for a in others if self._after_game(a)]
        others = [a for a in others if a not in tail]
        if others and not close_other:
            self._preparing.discard(entry.id)
            self._emit_launching(entry.id, "cancelled")
            result.set_result({"needsConfirm": "close-other", "other": self._entry_view(others[0])})
            return
        if tail:
            names = ", ".join("„%s“" % self._app_title(a) for a in tail)
            self._emit_launching(entry.id, "closing-other", "%s wird noch beendet …" % names)

            def again() -> None:
                self._tail_waits.pop(entry.id, None)
                self._spec_ready(entry, work, close_other, confirmed, result, adopted, waited=True)
            waiter = self._when_gone([a.app_id for a in tail], self._after_game_seconds(tail), kill=False, then=again)
            if waiter is not None:
                self._tail_waits[entry.id] = waiter     # „Abbrechen“ im Ladebild wartet nicht bis zum Ende
            return
        if others:
            names = ", ".join("„%s“" % self._app_title(a) for a in others)
            self._emit_launching(entry.id, "closing-other", "%s wird geschlossen …" % names)
            for other in others:
                self.close_app(other)
            self._when_gone(
                [a.app_id for a in others], CLOSE_OTHER_S,
                then=lambda: self._start(entry, spec, result),
                failed=lambda: self._launch_failed(
                    entry, result, LaunchError("%s ließ sich nicht schließen." % names,
                                               reason="close-failed")))
            return
        self._start(entry, spec, result)

    # ---------------------------------------------------------------- Prefix-Übernahme (E24)
    @staticmethod
    def adoption_text(title: str, info: Any = None) -> str:
        """Text der Rückfrage vor der Übernahme: Spielname, der Hinweis der Starter
        (prefix.TEXT_ADOPT_HINT) und die Größe der Kopie, wenn sie sich schnell messen ließ
        (info = prefix.copy_estimate, vom Starter an AdoptionRequired gehängt)."""
        text = "%s %s" % (ADOPT_LEAD % title, TEXT_ADOPT_HINT)
        if isinstance(info, dict) and info.get("complete") and info.get("bytes"):
            try:
                size = int(info["bytes"])
                text += "\n" + (ADOPT_SIZE % size_text(size) if size >= 1000 ** 2 else ADOPT_SMALL)
            except (TypeError, ValueError):
                pass
        return text

    def _ask_adoption(self, entry: Any, needed: Any, close_other: bool, confirmed: bool,
                      result: Future) -> None:
        self._emit_launching(entry.id, "confirm")
        asked = self.confirm(ADOPT_TITLE, self.adoption_text(entry.title, getattr(needed, "info", None)),
                             [{"value": "yes", "label": "Kopieren und starten"},
                              {"value": "no", "label": "Abbrechen"}], default="no")
        self._adopt_questions[entry.id] = getattr(asked, "cid", "")

        def answered(fut: Future) -> None:
            self._adopt_questions.pop(entry.id, None)
            if fut.result() != "yes" or self.ending or self.adopt_prefix is None:
                self._launch_cancelled(entry, result)
                return
            cancel = threading.Event()
            self._adopting[entry.id] = cancel
            self._adopt_percent.pop(entry.id, None)
            self._emit_launching(entry.id, "prepare", ADOPT_COPYING)
            work = self._background(self.adopt_prefix, entry, cancel)
            work.add_done_callback(lambda f: self.sched.soon(
                lambda: self._adopted(entry, f, close_other, confirmed, result)))
        asked.add_done_callback(answered)

    def adopt_answer(self, entry_id: str, ok: bool) -> dict:
        """RPC app.adopt {id, ok} (E27): Antwort der Seite auf die Rückfrage zur Übernahme."""
        cid = self._adopt_questions.get(entry_id)
        if not cid:
            raise RpcError("Dazu gibt es gerade keine Rückfrage.", "NoQuestion")
        self.confirm_result(cid, "yes" if ok else "no")
        return {"ok": True}

    def _adopted(self, entry: Any, work: Future, close_other: bool, confirmed: bool,
                 result: Future) -> None:
        self._adopting.pop(entry.id, None)
        self._adopt_percent.pop(entry.id, None)
        try:
            work.result()
        except AdoptCancelled:
            self._launch_cancelled(entry, result)
            return
        except BaseException as e:
            self._launch_failed(entry, result, e)
            return
        if self.ending or entry.id in self._cancel_wanted:
            self._launch_cancelled(entry, result)
            return
        # Die Kopie liegt: Der Startauftrag wird neu gebaut und zeigt jetzt auf sie
        self._emit_launching(entry.id, "prepare", "„%s“ wird vorbereitet …" % entry.title)
        again = self._background(build_spec, entry, self.app)
        again.add_done_callback(lambda f: self.sched.soon(
            lambda: self._spec_ready(entry, f, close_other, confirmed, result, adopted=True)))

    def _start(self, entry: Any, spec: LaunchSpec, result: Future) -> None:
        if entry.id in self._cancel_wanted or self.ending:
            self._launch_cancelled(entry, result)
            return
        try:
            # E109: Tastenbelegung des Spiel-Modus aus der Option festhalten (nur spec.mouse == "game")
            apply_pad_keys(spec, entry)
            # entry bleibt in _preparing, bis die App als "lädt" eingetragen ist: Der Manager
            # meldet "started" schon während launch(), und sie darf dabei nicht angehalten werden
            app = self.procs.launch(spec)
        except BaseException as e:
            self._launch_failed(entry, result, e)
            return
        seconds = self._start_seconds(spec)
        app_id = app.app_id
        self._launching[app_id] = _Launch(
            entry.id, spec.title or entry.title,
            self.sched.later(seconds * 1000, lambda: self._launch_timeout(app_id)))
        self._preparing.discard(entry.id)
        touch = getattr(self.library, "touch", None)
        if touch:
            touch(entry.id)                 # Kachel rückt sofort nach vorn
        self._emit_launching(entry.id, "starting", "„%s“ wird gestartet …" % (spec.title or entry.title))
        self._apply()
        result.set_result({"ok": True, "appId": app_id})
        if app.visible:
            self._on_visible(app)

    def _start_seconds(self, spec: Any) -> float:
        """Frist bis zum ersten Bild: die des Starters (Steam-Spiele, E57), sonst die Einstellung."""
        try:
            own = float(getattr(spec, "start_timeout", None) or 0)
        except (TypeError, ValueError):
            own = 0.0
        return own if own > 0 else float(self.config.get("shell.launch_timeout", 90))

    def _launch_timeout(self, app_id: int) -> None:
        launch = self._launching.pop(app_id, None)
        app = self.procs.by_app_id(app_id)
        if launch is None or app is None:
            return
        seconds = int(self._start_seconds(getattr(app, "spec", None)))
        self._undo_lutris_way(launch.entry_id)
        self._emit_launching(launch.entry_id, "failed")
        self._emit_error(app, launch.title, "„%s“ hat nach %d Sekunden kein Bild gezeigt und "
                                            "wurde beendet." % (launch.title, seconds),
                         CODE_TIMEOUT, PHASE_START)
        self.close_app(app)

    def close_app(self, app: Any, kill: bool = False) -> None:
        self._closing.add(app.app_id)
        if app.frozen:
            self.procs.thaw(app)            # ein angehaltener Prozess kann nicht auf SIGTERM antworten
        (self.procs.kill if kill else self.procs.close)(app)
        if self.procs.by_app_id(app.app_id) is not None:
            self._check_reality()           # ihr Fenster ist vielleicht schon weg: Home sofort bedienbar
        self._apply()

    # ---------------------------------------------------------------- warten, bis Apps weg sind
    def _when_gone(self, app_ids: list[int], seconds: float, then: Callable[[], None],
                   failed: Callable[[], None] | None = None, kill: bool = True) -> dict | None:
        """then(), sobald alle weg sind. Nach seconds: mit kill SIGKILL und noch CLOSE_KILL_S, dann
        failed(); ohne kill (nur abwarten, E99) gleich failed() bzw. then(), wenn failed fehlt.
        Liefert den Wartenden (für _finish_waiter), None, wenn then() schon lief."""
        alive = {i for i in app_ids if self.procs.by_app_id(i) is not None}
        if not alive:
            then()
            return None
        waiter = {"ids": alive, "then": then, "failed": failed, "killed": not kill, "timer": None}
        waiter["timer"] = self.sched.later(seconds * 1000, lambda: self._waiter_timeout(waiter))
        self._waiters.append(waiter)
        return waiter

    def _finish_waiter(self, waiter: dict, ok: bool = True) -> None:
        if waiter not in self._waiters:
            return
        self._waiters.remove(waiter)
        self.sched.cancel(waiter["timer"])
        fn = waiter["then"] if ok or waiter["failed"] is None else waiter["failed"]
        fn()

    def _waiter_timeout(self, waiter: dict) -> None:
        waiter["timer"] = None
        if waiter not in self._waiters:
            return
        waiter["ids"] = {i for i in waiter["ids"] if self.procs.by_app_id(i) is not None}
        if not waiter["ids"]:
            self._finish_waiter(waiter)
        elif not waiter["killed"]:
            waiter["killed"] = True         # höflich hat nicht gereicht
            for app_id in list(waiter["ids"]):
                app = self.procs.by_app_id(app_id)
                if app is not None:
                    self.close_app(app, kill=True)
            waiter["timer"] = self.sched.later(CLOSE_KILL_S * 1000,
                                               lambda: self._waiter_timeout(waiter))
        else:
            self._finish_waiter(waiter, ok=False)

    # ================================================================ Sitzungsende und Übergabe
    def after_shutdown(self, then: Callable[[], None]) -> None:
        """Schließt alle laufenden Apps und ruft danach then() (auch wenn eine hängen bleibt:
        Ausschalten darf nicht an einem Spiel scheitern). Kehrt sofort zurück: Die Stufen des
        Beendens laufen über Zeitgeber, die Hauptschleife bleibt frei (Bild, Pad, RPC)."""
        self.ending = True
        try:
            self.cancel_preparing()                 # auch laufende Prefix-Kopien enden
            apps = list(self.procs.running())
            for app in apps:
                if app.app_id not in self._closing:
                    self.close_app(app)
            self._when_gone([a.app_id for a in apps], SHUTDOWN_S, then=then, failed=then)
        except BaseException:
            self.ending = False                     # sonst startete danach nie wieder etwas
            raise

    def handover(self, content: dict, done: Callable[[], None] | None = None,
                 failed: Callable[[BaseException], None] | None = None) -> None:
        """Übergabe an Steam: Apps schließen, handover.json schreiben, Home endet mit 10.
        Lässt sich die Datei nicht schreiben, läuft das Home weiter und failed(e) erfährt es."""
        def go() -> None:
            try:
                system.write_atomic(paths.handover_file(), json.dumps(content) + "\n")
            except Exception as e:
                _log.error("handover.json nicht schreibbar: %s", e)
                self.ending = False
                if failed:
                    failed(e)
                return
            if done:
                done()
            self.app.quit(10)
        self.after_shutdown(go)

    def _handover_asked(self, entry: Any, spec: LaunchSpec, confirmed: bool, result: Future) -> None:
        def go() -> None:
            self._emit_launching(entry.id, "handover", "Steam wird gestartet …")
            self.handover(dict(spec.handover or {}),
                          done=lambda: result.set_result({"ok": True, "handover": True}),
                          failed=lambda e: self._launch_failed(entry, result, LaunchError(
                              "Die Übergabe an Steam hat nicht geklappt.", str(e),
                              reason="handover-failed")))
        if confirmed:
            # Die Seite hat schon selbst gefragt. Die busy_checks werden trotzdem befragt:
            # Was sie melden, steht dann wenigstens im Protokoll.
            self.interruptions()
            go()
            return
        if (spec.handover or {}).get("uri"):
            title, ok = "Mit Steam starten?", "Starten"
            lead = ("„%s“ läuft über Steam. Die Konsole übergibt an Steam und ist wieder da, "
                    "sobald du das Spiel beendest." % entry.title)
        else:
            title, ok = "Steam Big Picture öffnen?", "Öffnen"
            lead = ("Die Konsole übergibt an Steam Big Picture. Zurück geht es über „Steam "
                    "beenden“ oder mit PS + Create + Options, 5 Sekunden gehalten.")
        closing, busy = self.interruptions()
        text = " ".join([lead] + closing)
        if busy:
            text += "\n" + "\n".join(busy)
        self._emit_launching(entry.id, "confirm")
        asked = self.confirm(title, text, [{"value": "yes", "label": ok},
                                           {"value": "no", "label": "Abbrechen"}], default="no")

        def answered(fut: Future) -> None:
            if fut.result() == "yes":
                go()
            else:
                self._emit_launching(entry.id, "cancelled")
                result.set_result({"ok": False, "cancelled": True})
        asked.add_done_callback(answered)

    def interruptions(self) -> tuple[list[str], list[str]]:
        """Was eine Aktion beendet, die alles schließt (Ausschalten, Neustart, Abmelden,
        Desktop, Übergabe an Steam; E31): (Sätze zu laufenden Spielen und Apps, Sätze der
        busy_checks). Befragt dabei jeden busy_check der Verdrahtung; ohne Verdrahtung zählt,
        was die Shell selbst weiß (Installationen laut sys.state, Prefix-Kopien)."""
        closing: list[str] = []
        apps = [a for a in self.procs.running() if a.app_id not in self._closing]
        if apps:
            names = ", ".join("„%s“" % self._app_title(a) for a in apps[:3])
            if len(apps) > 3:
                names += " und %d weitere" % (len(apps) - 3)
            closing.append("Laufende Spiele und Apps werden vorher geschlossen: %s." % names)
        busy = self.busy_sentences()
        if busy is None:
            busy = []
            if self.jobs_running():
                busy.append("Eine laufende Installation wird abgebrochen.")
            copy = self.copy_sentence()
            if copy:
                busy.append(copy)
        elif busy:
            _log.info("läuft noch: %s", " | ".join(busy))
        return closing, busy

    def jobs_running(self) -> int:
        """Archiv-Installationen laut sys.state (die Verdrahtung führt die Liste), die ein
        Ausschalten abbräche: wartende, laufende, solche im Abschluss und laufende Installationsprogramme
        (E114, "installing"). Eine offene Frage
        nach der Startdatei (needs-choice) zählt nicht, sie übersteht einen Neustart."""
        try:
            jobs = self.state.get().get("jobs") or []
            return sum(1 for j in jobs if isinstance(j, dict)
                       and j.get("state") in ("queued", "running", "finishing", "installing"))
        except Exception:
            return 0

    # ================================================================ Bildschirmtastatur, Maus
    def osk_open(self, purpose: str, initial: str = "", title: str = "", secret: bool = False,
                 learn: bool = True) -> Any:
        """purpose "app": tippt per XTEST in die vordere App (sie läuft dabei weiter).
        purpose "field": liefert den Text an den Aufrufer zurück (Future). secret: die Tastatur
        verdeckt die Eingabe (Kennwort, Schlüssel); der Text steht nie im Protokoll. learn=False:
        Die Tastatur merkt sich keine Wörter (Zeilen im Text-Editor, E99); Vorschläge bleiben."""
        if self._osk is not None:
            self.close_view(self._osk, None, hold=False)
        front = self._effective_front()
        params = {"purpose": purpose, "initial": initial, "title": title, "secret": secret is True,
                  "learn": learn is not False}
        if purpose == "app":
            if front is None:
                raise RpcError("Es ist keine App vorn, in die getippt werden könnte.", "NoApp")
            self._drop_views(lambda v: v.name == "confirm", hold=False)
            self._osk = self.open_view("osk", params, mode=PASSIVE, takes_input=True,
                                       app_id=front.app_id)
            return {"ok": True}
        fut: Future = Future()
        top = self._top()
        mode = ACTIVE if (top is not None and top.mode == ACTIVE) else PASSIVE

        def closed(result: Any) -> None:
            if not fut.done():
                fut.set_result({"text": initial, "cancelled": True} if result is None
                               else {"text": str(result), "cancelled": False})
        self._osk = self.open_view("osk", params, mode=mode, takes_input=True, on_close=closed)
        return fut

    def ui_open(self, screen: Any, params: Any = None) -> dict:
        """RPC ui.open {screen, params?}: Das Overlay (z. B. das Control Center) öffnet einen
        Bildschirm der Home-Seite. Das Home kommt nach vorn wie mit „Home“ im Control Center (eine
        App bleibt angehalten im Hintergrund), dann geht das Ereignis ui.open an die Home-Seite."""
        if not isinstance(screen, str) or not _SCREEN.match(screen):
            raise RpcError("Diesen Bildschirm gibt es nicht.", "BadRequest")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError("Ungültiger Aufruf.", "BadRequest")
        self.go_home()
        self.rpc.emit("ui.open", {"screen": screen, "params": params}, target="home")
        return {}

    def osk_done(self, text: str | None) -> None:
        if self._osk is not None:
            self.close_view(self._osk, text, hold=False)

    def _osk_for_app(self) -> bool:
        return self._osk is not None and self._osk.params.get("purpose") == "app"

    def type_for_app(self, text: str) -> None:
        """Text der Bildschirmtastatur an die App vorn. Eigene Apps (OSK_DIRECT) bekommen ihn direkt,
        weil Gamescope Umschalt/AltGr aus XTEST an GTK-Fenster nicht weiterreicht („@“ wurde „2“)."""
        if not text:
            return
        front = self._front_app()
        name = OSK_DIRECT.get(front.entry_id) if front is not None else None
        if name is None:
            self.input.type_text(text)
            return
        self._osk_direct.append((name, text))
        if self._osk_direct_timer is None:
            self._osk_direct_flush()

    def _osk_direct_flush(self) -> None:
        self._osk_direct_timer = None
        if self.input.typing_busy():        # erst die Rücktaste davor (XTEST), dann der Text
            self._osk_direct_timer = self.sched.later(OSK_DIRECT_WAIT_MS, self._osk_direct_flush)
            return
        while self._osk_direct:
            name, text = self._osk_direct.pop(0)
            if not self.osk_send(paths.osk_socket(name), text):
                self.input.type_text(text)  # Eingang fehlt (Chrome, ältere Fassung): wie bisher

    def return_after(self, entry_id: str) -> None:
        """Die App, die jetzt vorn ist, kommt wieder nach vorn, wenn entry_id endet (Browser für die
        Anmeldung eines Launchers, E94: danach zurück in den Launcher, nicht ins Home)."""
        front = self._front_app()
        if front is not None and front.entry_id != entry_id:
            self._return_to[entry_id] = front.app_id

    def launcher_progress(self, app_id: int, revealed: bool) -> None:
        """Spiel über einen Launcher (E94): Das Spiel zeigt sein erstes Fenster, oder der Launcher
        wird sichtbar, weil er etwas will (dann mit Maus). Erst jetzt endet das Ladebild."""
        app = self.procs.by_app_id(app_id)
        if app is None:
            return
        if revealed:
            if app_id not in self._reveal_mouse:
                self._reveal_mouse[app_id] = self._mouse.get(app_id)
            self._mouse[app_id] = True
        elif app_id in self._reveal_mouse:
            # Die Maus galt nur dem Launcher (Anmeldung, Update). Bliebe sie an, steuerte das Pad
            # das Spiel zusätzlich per XTEST: Stick = Maus, Kreis = Escape (E99)
            before = self._reveal_mouse.pop(app_id)
            if before is None:
                self._mouse.pop(app_id, None)
            else:
                self._mouse[app_id] = before
        if app_id in self._launching and self._has_window(app):
            self._on_visible(app)
        self._apply()

    def optiscaler_menu(self) -> dict:
        """Control Center › Leistung › „OptiScaler-Menü“ (E118). Das Menü von OptiScaler öffnet und schließt
        nur eine Taste (Einfg); offen lässt es sich mit dem Controller bedienen (OptiScaler schaltet dafür die
        Gamepad-Steuerung von ImGui ein, die den DualSense unter Proton aber nicht sieht). Die Seite schließt danach
        das Control Center; die Taste geht an das Spiel, sobald es weiterläuft (_settle_done): Unter dem Overlay ist
        es angehalten und ohne Fokus. Mit dem Öffnen bedient das Pad das Menü (padmouse profile "opti": Steuerkreuz,
        Kreuz = Leertaste, Kreis = Escape), Options oder dieser Eintrag schließen es wieder."""
        front = self._front_app()
        if front is None or not self._optiscaler(front):
            raise RpcError("Dieses Spiel läuft ohne OptiScaler.", "NoOptiScaler")
        self._key_after = (front.app_id, OPTISCALER_KEY, self.sched.now() + OPTISCALER_WAIT_S)
        return {}

    def _key_when_running(self) -> None:
        pending, self._key_after = self._key_after, None
        if pending is None:
            return
        app_id, key, until = pending
        front = self._effective_front()
        if self.sched.now() > until or front is None or front.app_id != app_id or self._top() is not None:
            return
        self.sched.later(OPTISCALER_DELAY_MS, lambda: self._press_for(app_id, key))

    def _press_for(self, app_id: int, key: str) -> None:
        front = self._effective_front()
        if front is None or front.app_id != app_id or front.frozen or self._top() is not None:
            return                          # inzwischen gewechselt oder wieder im Overlay
        try:
            self.input.press_key(key)
        except Exception:
            _log.exception("Taste %s für %s", key, front.entry_id)
            return
        # Dieselbe Taste öffnet und schließt das Menü: Mit ihr wechselt das Pad zwischen Menü und Spiel
        if app_id in self._opti:
            self._opti.discard(app_id)
        else:
            self._opti.add(app_id)
        self._apply()

    def set_mouse(self, on: bool) -> dict:
        front = self._front_app()
        if front is None:
            raise RpcError("Der Maus-Modus geht nur, wenn eine App vorn ist.", "NoApp")
        self._reveal_mouse.pop(front.app_id, None)     # der Nutzer hat selbst entschieden: das gilt, auch im Spiel
        self._opti.discard(front.app_id)               # auch gegenüber dem OptiScaler-Menü (E118)
        if not on and front.spec.mouse in OWN_PROFILES:
            self._mouse.pop(front.app_id, None)        # zurück zum Tasten- bzw. Spiel-Modus der App, nicht „gar nichts“
        else:
            self._mouse[front.app_id] = bool(on)
        self._apply()
        return {"mouseMode": self._real_mouse(front)}

    # ================================================================ RPC
    def register_rpc(self, rpc: Any) -> None:
        def need_app(params: dict) -> Any:
            app = self.procs.by_entry(str(params.get("id", "")))
            if app is None:
                raise RpcError("Das läuft gerade nicht.", "NotRunning")
            return app

        def app_focus(params: dict) -> dict:
            app = need_app(params)
            self.focus_app(app)
            return {"ok": True, "appId": app.app_id}

        def app_close(params: dict) -> dict:
            self.close_app(need_app(params))
            return {}

        def app_kill(params: dict) -> dict:
            # „Abbrechen“ im Ladebild: Solange noch vorbereitet wird (Auftrag bauen, Prefix
            # kopieren), gibt es keine App, die man beenden könnte
            cancelled = self.cancel_launch(str(params.get("id", "")))
            app = self.procs.by_entry(str(params.get("id", "")))
            if app is not None:
                self.close_app(app, kill=True)
            elif not cancelled:
                need_app(params)
            return {"cancelled": True} if cancelled else {}

        def osk_type(params: dict) -> dict:
            if self._osk_for_app():
                self.type_for_app(str(params.get("text", "")))
            return {}

        def osk_key(params: dict) -> dict:
            if self._osk_for_app():
                self.input.press_key(str(params.get("name", "")))
            return {}

        def app_launch(params: dict) -> Any:
            entry_id = str(params.get("id", ""))
            try:
                return self.launch(entry_id, bool(params.get("closeOther")),
                                   bool(params.get("confirmed")))
            except LaunchError as e:
                # sofort abgelehnt (nicht installiert, Platte weg, Startdatei fehlt): Die
                # Seite bekommt dieselbe Meldung wie bei Fehlern, die sich erst später zeigen
                self.report_launch_error(entry_id, e)
                raise

        rpc.register("app.launch", app_launch)
        rpc.register("app.focus", app_focus)
        rpc.register("app.home", lambda p: self.go_home() or {})
        rpc.register("app.close", app_close)
        rpc.register("app.kill", app_kill)
        rpc.register("app.adopt", lambda p: self.adopt_answer(str(p.get("id", "")), bool(p.get("ok"))))
        rpc.register("app.optiscaler", lambda p: self.optiscaler_menu())
        rpc.register("overlay.close", lambda p: self.page_closed(p.get("view"), p.get("serial")) or {})
        rpc.register("overlay.confirmResult",
                     lambda p: self.confirm_result(str(p.get("id", "")), p.get("value")) or {})
        rpc.register("osk.open", lambda p: self.osk_open(
            str(p.get("purpose") or "field"), str(p.get("initial") or ""), str(p.get("title") or ""),
            p.get("secret") is True, p.get("learn") is not False))
        rpc.register("ui.open", lambda p: self.ui_open(p.get("screen"), p.get("params")))
        rpc.register("osk.type", osk_type)
        rpc.register("osk.key", osk_key)
        rpc.register("osk.done", lambda p: self.osk_done(
            None if p.get("cancelled") else str(p.get("text", ""))) or {})
        rpc.register("mouse.set", lambda p: self.set_mouse(bool(p.get("on"))))
