"""Text und Tasten per XTEST in das Gamescope-Xwayland der vorderen App tippen.

Für die eigene Bildschirmtastatur. Es entsteht kein Eingabegerät im Kernel (kein uinput):
Die Eingaben gehen über Xwayland und libei durch Gamescope an das Fenster mit dem
Tastaturfokus und wirken nur in dieser Gamescope-Sitzung.

Was im unsichtbaren Gamescope 3.16 gemessen wurde und den Aufbau bestimmt:

1. Umschalttasten: Im unsichtbaren Gamescope 3.16 kamen Shift und AltGr per XTEST nicht an. In der
   echten Sitzung (Fernseher, 2026-10-22) kommen sie an: xdotool tippte "@", Großbuchstaben und
   "!" in den Rockstar Launcher, die Anmeldung ging durch. Die vorübergehend umgelegte Taste
   (ChangeKeyboardMapping) kommt dort dagegen NICHT an: Aus "Ab@!Z" wurde "b", man konnte keine
   Großbuchstaben und kein "@" tippen. Darum: Zeichen auf der Belegung mit Umschalt- bzw.
   AltGr-Taste (Ebene 2 bzw. 3); nur Zeichen, die gar nicht darauf liegen (€ auf us), bekommen
   als Rückfall vorübergehend einen eigenen, sonst unbenutzten Tastencode.
   Nachtrag 2026-10-24: An GTK-Fenster (eigener Browser, Terminal) reicht Gamescope Umschalt und Strg
   aus XTEST NICHT weiter, auch nicht mit xdotool und Pausen („@“ wurde „2“, Strg+A ein „a“). Text geht
   dorthin deshalb direkt (oskdirect, shell.OSK_DIRECT); nur Tasten ohne Umschalt kommen von hier.
2. Der erste XTEST-Tastendruck schaltet Gamescope auf seine virtuelle Tastatur um; dabei
   ersetzt Xwayland die ganze Belegung (z. B. us -> de) und wirft eigene Zuordnungen weg.
   Deshalb wird vor dem Tippen eine Sondentaste (VoidSymbol, bewirkt nichts) gedrückt und ihre
   Ankunft abgewartet; erst danach wird die Belegung gelesen. Ersetzt später jemand die
   Belegung (MappingNotify über viele Tasten), beginnt das von vorn.
3. Ob eine Taste angekommen ist, zeigt ein XInput2-Rohereignis auf DIESEM X-Server. Bleibt es
   aus, hat hier kein Fenster den Tastaturfokus (die App liegt auf dem anderen Server oder das
   Overlay hält die Eingabe): Dann wird nichts getippt und der Fehler gemeldet, statt Zeichen
   mit falscher Belegung in ein fremdes Fenster zu schicken.

Displays ohne GAMESCOPE_XWAYLAND_SERVER_ID werden verweigert: DISPLAY=:0 ist außerhalb des
Game-Mode der Desktop des Nutzers.
"""
from __future__ import annotations

import logging
import os
import select
import struct
import time
from collections import deque
from typing import Callable

log = logging.getLogger("couch.input")


class NotGamescope(RuntimeError):
    """Das Display ist kein Xwayland von Gamescope. Dorthin wird nie getippt oder geklickt."""


def is_gamescope_display(d) -> bool:
    """Ist das der X-Server eines Gamescope? Erkennung an GAMESCOPE_XWAYLAND_SERVER_ID an der Wurzel.

    Irgendeine GAMESCOPE…-Eigenschaft genügt NICHT: Der Steam-Client setzt auch auf dem
    KDE-Desktop welche (GAMESCOPE_ALLOW_TEARING, GAMESCOPE_COMPOSITE_FORCE …) an die Wurzel
    von KWins Xwayland. Die Server-ID schreibt nur Gamescope selbst.
    """
    try:
        atom = d.get_atom("GAMESCOPE_XWAYLAND_SERVER_ID", only_if_exists=True)
        if not atom:
            return False
        return d.screen().root.get_full_property(atom, 0) is not None      # 0 = AnyPropertyType
    except Exception:
        return False


def open_display(name: str | None):
    """X-Verbindung für XTEST. Wirft NotGamescope, wenn dort kein Gamescope läuft."""
    from Xlib import display           # erst hier: Unit-Tests brauchen kein X
    from .. import xfix
    d = xfix.repair(display.Display(name))
    try:
        if not is_gamescope_display(d):
            raise NotGamescope(f"{d.get_display_name()} ist kein Gamescope-Xwayland")
        if not d.query_extension("XTEST"):
            raise RuntimeError(f"{d.get_display_name()}: X-Server ohne XTEST")
    except Exception:
        d.close()
        raise
    return d


# X-Tastencode = evdev-Code + 8, unabhängig von der Tastaturbelegung
KEYCODES = {
    "Return": 36, "BackSpace": 22, "Escape": 9, "Tab": 23, "space": 65, "Delete": 119,
    "Up": 111, "Down": 116, "Left": 113, "Right": 114,
    "Home": 110, "End": 115, "Page_Up": 112, "Page_Down": 117,
    "Insert": 118,                      # Einfg: öffnet das Menü von OptiScaler (E118)
}
_ALIASES = {"enter": "Return", "esc": "Escape", "backspace": "BackSpace", "del": "Delete", "ins": "Insert",
            "pageup": "Page_Up", "pagedown": "Page_Down", "page_up": "Page_Up", "page_down": "Page_Down"}
_BY_LOWER = {name.lower(): name for name in KEYCODES}


def keycode_for(name: str) -> int:
    """Tastencode zu einem Tastennamen ("Return", "BackSpace", "Escape", "Tab", Pfeile …)."""
    key = _ALIASES.get(name.lower()) or _BY_LOWER.get(name.lower())
    if key is None:
        raise ValueError(f"unbekannte Taste: {name!r}")
    return KEYCODES[key]


def keysym_to_char(keysym: int) -> str | None:
    if 0x20 <= keysym <= 0x7E or 0xA0 <= keysym <= 0xFF:
        return chr(keysym)                      # Latin-1: Keysym = Codepunkt
    if 0x01000100 <= keysym <= 0x0110FFFF:
        return chr(keysym - 0x01000000)         # Unicode-Keysym
    return None                                 # Funktions-, Tot- und Alttasten


def char_to_keysym(ch: str) -> int | None:
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    if cp >= 0x100:
        return 0x01000000 | cp
    return None                                 # Steuerzeichen


_VOID = 0xFFFFFF                # VoidSymbol: Taste ohne Wirkung
_SHIFT_L, _LEVEL3 = 0xFFE1, 0xFE03      # Shift_L, ISO_Level3_Shift (AltGr)
_GENERIC_EVENT = 35
_RAW_KEY_RELEASE = 14           # XI_RawKeyRelease
_CONTROL = {"\n": "Return", "\r": "Return", "\t": "Tab", "\b": "BackSpace", "\x1b": "Escape"}


class XTestTyper:
    """Tippt Text und Tasten. Arbeitet eine Warteschlange ab, ohne zu blockieren.

        typer = XTestTyper(":1")
        typer.type_text("Grüße"); typer.press_key("Return")
        wait = typer.pump()        # Sekunden bis zum nächsten nötigen Aufruf, None = fertig
    Der Dienst ruft pump(), wenn die X-Verbindung lesbar ist (fileno()) und nach der gemeldeten
    Frist. Für Werkzeuge und Tests wartet run() blockierend, bis alles getippt ist.
    """
    ACK_TIMEOUT = 0.30      # so lange darf die Quittung einer Taste ausbleiben
    REUSE_AFTER = 0.15      # frühestens dann bekommt ein Ersatz-Tastencode ein anderes Zeichen:
    #                         Der Empfänger liest die Belegung erst, wenn er die Taste verarbeitet
    RELEASE_AFTER = 2.0     # Ruhezeit, nach der die vorübergehenden Zuordnungen verschwinden
    CONFIRM_FOR = 1.0       # so lange gilt eine Quittung als Beleg, dass der Tastaturfokus stimmt
    BLIND_SETTLE = 0.08     # Ersatz für die Quittung, wenn XInput2 fehlt

    def __init__(self, display_name: str | None = None, *, on_error: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, blind: bool | None = None):
        from Xlib import X
        from Xlib.ext import xtest
        self._X, self._fake = X, xtest.fake_input
        self._d = open_display(display_name)
        self.display_name = self._d.get_display_name()
        self._d.set_error_handler(lambda err, _req=None: log.warning("xtest: X-Fehler %s", err))
        self._clock = clock
        self.on_error = on_error
        self.errors: list[str] = []
        self.broken = False                     # X-Verbindung verloren: Der Besitzer muss schließen
        if blind is None:
            blind = os.environ.get("COUCH_XTEST_BLIND") == "1"
        self._xi_opcode = None if blind else self._init_xi()
        self._listening = False
        self._queue: deque[tuple[str, object]] = deque()
        self._primed = False
        self._priming_at: float | None = None
        self._probe: int | None = None
        self._width = 1
        self._table: dict[str, int] = {}        # Zeichen ohne Umschalttaste -> Tastencode
        self._shifted: dict[str, tuple[int, tuple[int, ...]]] = {}   # Zeichen -> (Tastencode, Umschalttasten)
        self._free: list[int] = []              # unbenutzte Tastencodes
        self._slots: dict[str, int] = {}        # Zeichen -> vorübergehend zugeordneter Tastencode
        self._slot_time: dict[int, float] = {}
        self._pending: dict[int, int] = {}      # Tastencode -> gesendet, aber noch nicht quittiert
        self._sent: dict[int, float] = {}
        self._timed_out: set[int] = set()
        self._own_maps: dict[int, int] = {}     # Tastencode -> eigene Zuordnungen, deren Echo noch aussteht
        self._last_send = 0.0
        self._confirmed_at = float("-inf")      # Zeit der letzten Quittung

    # --- Aufbau ------------------------------------------------------------------------
    def _init_xi(self) -> int | None:
        """XInput 2.2 anmelden. Mit 2.0 bekäme man Rohereignisse nicht, solange ein Spiel greift."""
        try:
            from Xlib.ext import xinput
            if not self._d.has_extension(xinput.extname):
                return None
            opcode = self._d.display.get_extension_major(xinput.extname)
            xinput.XIQueryVersion(display=self._d.display, opcode=opcode, major_version=2, minor_version=2)
            return opcode
        except Exception as exc:
            log.warning("xtest: XInput2 nicht nutzbar (%s): tippe ohne Quittung", exc)
            return None

    def _listen(self, on: bool) -> None:
        """Rohereignisse nur während des Tippens abonnieren: sonst weckt jede Taste des Spielers."""
        if self._xi_opcode is None or on == self._listening:
            return
        from Xlib.ext import xinput
        self._d.screen().root.xinput_select_events(
            [(xinput.AllMasterDevices, xinput.RawKeyReleaseMask if on else 0)])
        self._listening = on

    def fileno(self) -> int:
        return self._d.fileno()

    @property
    def busy(self) -> bool:
        return bool(self._queue) or any(self._pending.values())

    # --- Aufträge ----------------------------------------------------------------------
    def type_text(self, text: str) -> None:
        for ch in text:
            if ch in _CONTROL:
                self._queue.append(("key", KEYCODES[_CONTROL[ch]]))
            else:
                self._queue.append(("char", ch))

    def press_key(self, name: str) -> None:
        self._queue.append(("key", keycode_for(name)))

    # --- Ablauf ------------------------------------------------------------------------
    def pump(self, now: float | None = None) -> float | None:
        """Tippt, was gerade geht. Liefert die Wartezeit bis zum nächsten Aufruf oder None."""
        now = self._clock() if now is None else now
        try:
            return self._pump(now)
        except Exception as exc:       # X-Verbindung weg: nichts davon darf den Aufrufer treffen
            self.broken = True
            self._fail(f"Tippen abgebrochen: {exc!r}")
            return None

    def run(self, timeout: float = 5.0) -> bool:
        """Blockierend für Werkzeuge und Tests: True, wenn alles ohne Fehler getippt wurde."""
        seen = len(self.errors)
        end = self._clock() + timeout
        while True:
            wait = self.pump()
            if not self.busy:
                return len(self.errors) == seen
            if self._clock() >= end:
                self._fail("Tippen abgebrochen: Zeit überschritten")
                return False
            select.select([self._d.fileno()], [], [], min(wait or 0.02, 0.05))

    def _pump(self, now: float) -> float | None:
        self._drain(now)
        if self._queue:
            self._listen(True)
        while self._queue:
            if not self._primed:
                wait = self._prime(now)
                if wait is not None:
                    self._d.flush()
                    return wait
                continue
            kind, value = self._queue[0]
            mods: tuple[int, ...] = ()
            if kind == "key":
                code = value
            elif value not in self._table and value in self._shifted:
                code, mods = self._shifted[value]
            else:
                code = self._keycode_for_char(value, now)
                if code is None:                   # alle Ersatz-Tastencodes sind noch warm
                    self._d.flush()
                    return 0.02
                if code == 0:
                    self._queue.popleft()
                    self._error(f"Zeichen {value!r} lässt sich nicht tippen")
                    continue
            if (self._xi_opcode is not None and now - self._confirmed_at > self.CONFIRM_FOR
                    and any(self._pending.values())):
                # Nach einer Pause erst EINE Taste schicken und ihre Quittung abwarten: Hat inzwischen
                # ein anderes Fenster den Tastaturfokus, landet höchstens diese eine dort.
                self._d.flush()
                return 0.02
            self._queue.popleft()
            self._tap(code, now, mods)
        self._d.flush()
        if any(self._pending.values()):
            return 0.05
        idle = now - self._last_send
        if self._slots or self._listening:
            if idle < self.RELEASE_AFTER:
                return self.RELEASE_AFTER - idle
            self._release_slots()
            self._listen(False)
            self._d.flush()
        return None

    def _drain(self, now: float) -> None:
        X = self._X
        while self._d.pending_events():
            ev = self._d.next_event()
            if ev.type == X.MappingNotify:
                if ev.request != X.MappingKeyboard:
                    continue
                if ev.count > 1:
                    # viele Tasten auf einmal = neue Belegung (während der Sonde erwartet)
                    if self._priming_at is None:
                        self._forget_keymap()
                    self._own_maps.clear()
                elif self._own_maps.get(ev.first_keycode):
                    self._own_maps[ev.first_keycode] -= 1          # Echo einer eigenen Zuordnung
                else:
                    self._foreign_change(ev.first_keycode)
            elif (ev.type == _GENERIC_EVENT and self._xi_opcode is not None
                  and ev.extension == self._xi_opcode and ev.evtype == _RAW_KEY_RELEASE):
                code = struct.unpack_from("=I", ev.data, 6)[0]     # hinter deviceid (2) und time (4)
                if self._pending.get(code):
                    self._pending[code] -= 1
                    self._confirmed_at = now
        limit = self.BLIND_SETTLE if self._xi_opcode is None else self.ACK_TIMEOUT
        lost = False
        for code, count in self._pending.items():
            if count and now - self._sent.get(code, now) >= limit:
                self._pending[code] = 0
                if self._xi_opcode is not None:
                    self._timed_out.add(code)
                    lost = lost or code != self._probe
        if lost:
            self._fail("Tippen abgebrochen: Die Tasten kommen nicht mehr an (Tastaturfokus verloren?)")

    def _foreign_change(self, code: int) -> None:
        """Jemand anderes hat einen Tastencode umgelegt (zweites Programm, das so tippt wie wir)."""
        for table in (self._table, self._slots):
            for ch in [ch for ch, mapped in table.items() if mapped == code]:
                del table[ch]
        for ch in [ch for ch, (mapped, keys) in self._shifted.items() if code == mapped or code in keys]:
            del self._shifted[ch]
        self._slot_time.pop(code, None)
        if code in self._free:
            self._free.remove(code)
        if code == self._probe:
            self._primed = False               # ohne Sonde lässt sich ein Belegungswechsel nicht abwarten

    def _forget_keymap(self) -> None:
        self._primed = False
        self._slots.clear()
        self._slot_time.clear()
        self._free = []
        self._probe = None

    def _load_keymap(self) -> None:
        lo, hi = self._d.display.info.min_keycode, self._d.display.info.max_keycode
        rows = self._d.get_keyboard_mapping(lo, hi - lo + 1)
        self._width = len(rows[0]) if rows else 1
        table: dict[str, int] = {}
        shifted: dict[str, tuple[int, tuple[int, ...]]] = {}
        free: list[int] = []
        mods = {sym: lo + i for i, syms in reversed(list(enumerate(rows))) for sym in syms[:1] if sym in (_SHIFT_L, _LEVEL3)}
        shift, level3 = mods.get(_SHIFT_L), mods.get(_LEVEL3)
        # Spalten der Kernbelegung: 0 ohne, 1 Shift, 4 AltGr, 5 AltGr+Shift (2/3 = zweite Gruppe)
        levels = [(1, (shift,)), (4, (level3,)), (5, (shift, level3))]
        for i, syms in enumerate(rows):
            code = lo + i
            if all(sym in (0, _VOID) for sym in syms):
                # unbenutzt (oder nur unsere Sonde aus einer früheren Runde): als Ersatz brauchbar
                if code > 8:                   # 8 wäre KEY_RESERVED
                    free.append(code)
                continue
            ch = keysym_to_char(syms[0])
            if ch is not None and ch not in table:
                table[ch] = code
            for col, keys in levels:
                ch = keysym_to_char(syms[col]) if col < len(syms) and all(keys) else None
                if ch is not None and ch not in shifted:
                    shifted[ch] = (code, keys)
        self._table, self._shifted, self._free = table, shifted, free
        self._slots.clear()
        self._slot_time.clear()

    def _map(self, code: int, keysym: int) -> None:
        self._d.change_keyboard_mapping(code, [(keysym,) * self._width])
        self._own_maps[code] = self._own_maps.get(code, 0) + 1     # das MappingNotify dazu ist unseres

    def _prime(self, now: float) -> float | None:
        """Sondentaste drücken und ihre Ankunft abwarten. None = bereit (oder aufgegeben)."""
        if self._priming_at is None:
            if self._slots:
                self._release_slots()          # Reste einer früheren Runde: nicht als feste Tasten übernehmen
            self._load_keymap()
            if not self._free:
                # keine freie Taste: ohne Sonde weiter; Zeichen mit Umschalttaste gehen dann nicht
                log.warning("xtest: kein freier Tastencode auf %s", self.display_name)
                self._primed = True
                return None
            self._probe = self._free.pop()
            self._map(self._probe, _VOID)
            self._timed_out.discard(self._probe)
            self._priming_at = now
            self._tap(self._probe, now)
            return 0.02
        if self._pending.get(self._probe):
            return 0.02
        self._priming_at = None
        if self._probe in self._timed_out:
            self._timed_out.discard(self._probe)
            self._fail("Tippen nicht möglich: Auf diesem X-Server hat kein Fenster den Tastaturfokus")
            return None
        # Die Sonde ist durch: Ein Wechsel der Belegung (falls es einen gab) ist jetzt abgeschlossen
        self._load_keymap()
        if self._probe in self._free:
            self._free.remove(self._probe)
        else:                                  # die neue Belegung benutzt diesen Code selbst
            self._probe = self._free.pop() if self._free else None
        if self._probe is not None:
            self._map(self._probe, _VOID)      # der Wechsel hat die Zuordnung der Sonde gelöscht
        self._primed = True
        return None

    def _keycode_for_char(self, ch: str, now: float) -> int | None:
        """Tastencode für ein Zeichen; 0 = nicht tippbar, None = später noch einmal fragen."""
        code = self._table.get(ch) or self._slots.get(ch)
        if code:
            return code
        keysym = char_to_keysym(ch)
        if keysym is None:
            return 0
        if self._free:
            code = self._free.pop()
        elif self._slots:
            old, code = min(self._slots.items(), key=lambda item: self._slot_time.get(item[1], 0.0))
            if self._pending.get(code) or now - self._slot_time.get(code, 0.0) < self.REUSE_AFTER:
                return None
            del self._slots[old]
        else:
            return 0
        self._map(code, keysym)
        self._slots[ch] = code
        self._slot_time[code] = now
        return code

    def _tap(self, code: int, now: float, mods: tuple[int, ...] = ()) -> None:
        X = self._X
        for mod in mods:
            self._fake(self._d, X.KeyPress, mod)
        self._fake(self._d, X.KeyPress, code)
        self._fake(self._d, X.KeyRelease, code)
        for mod in reversed(mods):
            self._fake(self._d, X.KeyRelease, mod)
        self._pending[code] = self._pending.get(code, 0) + 1
        self._sent[code] = now
        if code in self._slot_time:
            self._slot_time[code] = now
        self._last_send = now

    def _release_slots(self) -> None:
        for code in self._slots.values():
            self._map(code, 0)
            self._free.append(code)
        self._free.sort()
        self._slots.clear()
        self._slot_time.clear()

    # --- Fehler ------------------------------------------------------------------------
    def _error(self, message: str) -> None:
        """Nie stumm verschlucken: protokollieren und dem Aufrufer melden."""
        log.warning("xtest %s: %s", self.display_name, message)
        self.errors.append(message)
        del self.errors[:-20]
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                log.exception("xtest: on_error")

    def _fail(self, message: str) -> None:
        dropped = len(self._queue)
        self._queue.clear()
        try:
            self._release_slots()              # sonst gingen die Ersatz-Tastencodes nach und nach verloren
        except Exception:
            self._slots.clear()
            self._slot_time.clear()
        self._pending.clear()
        self._timed_out.clear()
        self._priming_at = None
        self._primed = False
        self._error(f"{message} ({dropped} Eingaben verworfen)" if dropped else message)

    def close(self) -> None:
        try:
            self._release_slots()
            if self._probe is not None:
                self._map(self._probe, 0)
            self._listen(False)
            self._d.sync()
        except Exception:
            pass
        try:
            self._d.close()
        except Exception:
            pass
