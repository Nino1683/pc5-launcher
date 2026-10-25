"""padmouse: das Gamepad als Maus und Tastatur für Desktop, Browser und Dialoge in Spielen.

Belegung:
    linker Stick      Zeiger, mit Beschleunigungskurve; L3 gehalten = Feinmodus
    rechter Stick     Rollen, senkrecht und waagrecht
    R2 / Kreuz        linke Maustaste (halten = ziehen)
    L2                rechte Maustaste
    R3                mittlere Maustaste
    Steuerkreuz       Pfeiltasten
    Kreis             Escape
    Quadrat           Rücktaste
    Dreieck           Bildschirmtastatur (Rückruf; die Shell öffnet sie)
    L1 / R1           zurück / vor (Maustasten 8/9, verstehen Browser und Dolphin)
    PS, Touchpad      fasst padmouse nicht an: PS gehört der Shell, das Touchpad ist
                      durch den Kernel schon ein Zeigegerät (libinput) – sonst doppelte Eingabe
    Create, Options   unbelegt (Options ist Teil von PS + Options)

Spiel-Modus (profile="game", E64) für Spiele ohne Controller-Unterstützung (Wunsch 2026-10-18:
„Spiele, die keinen Controller unterstützen, sollen trotzdem mit dem Controller funktionieren“):
    linker Stick      W A S D
    rechter Stick     Maus (Umschauen, Zielen; in Menüs der Zeiger)
    R2 / L2           linke / rechte Maustaste;  R3 mittlere Maustaste
    Kreuz             Leertaste      Kreis   C      Quadrat  R      Dreieck  E
    L1 / R1           Q / F          L3      Umschalt links (Rennen)
    Steuerkreuz       Pfeiltasten    Options Escape  Create  Tab
  Je Spiel lässt sich das umbelegen (E109, Option pad_keys über keys=…, game_keymap): jede Taste außer
  den Sticks und PS auf ein Ziel aus TARGET_CODES (Taste nach Lage, Maustaste, Mausrad oder nichts).
  Ohne keys gilt genau die Tabelle oben (GAME_DEFAULT).

Zeiger-Modus (profile="cards", E97) für Spiele, die man nur mit der Maus spielt (Wunsch 2026-10-23:
„Könnte man Hearthstone auch ohne Maus-Tastatur-Modus spielen? Wenn ich den Controller-Modus
anlasse, passiert nichts“):
    linker Stick      Zeiger wie im Maus-Modus;  L3 gehalten = Feinmodus
    rechter Stick     Rollen
    Kreuz / R2        linke Maustaste (auswählen, ausspielen; halten = ziehen)
    Kreis / L2        rechte Maustaste (abbrechen)
    R3                mittlere Maustaste       Options   Escape (Menü des Spiels)
    Dreieck           Bildschirmtastatur
    Steuerkreuz, L1, R1  Sprung des Zeigers an feste Stellen des Spiels (layout, LAYOUTS), ohne
                      Klick; ohne Sprungstelle Pfeiltasten bzw. nichts
    Create halten + Steuerkreuz/L1/R1  jetzige Zeigerstelle für diese Taste merken (E99, eigene
                      Stellen, Option pad_spots; liegen über LAYOUTS); dieser Create-Druck ist dann
                      kein Bildschirmfoto (die Zurückhaltung macht service.py)

OptiScaler-Menü (profile="opti", E118), solange das Menü von OptiScaler offen ist, das die Shell geöffnet hat
(Wunsch 2026-10-25: „Kann man das nicht komplett mit dem Controller steuern?“). Das ImGui-Menü von OptiScaler
lässt sich mit der Tastatur bedienen, seine Gamepad-Steuerung sieht den DualSense unter Proton aber nicht:
    Steuerkreuz       Pfeiltasten (Eintrag wählen; nach Kreuz auf einem Regler: Wert ändern)
    Kreuz             Leertaste (auswählen, umschalten, Regler bearbeiten)
    Kreis             Escape (zurück)       L1 / R1   Bild auf / Bild ab
    Options           Einfg (schließt das Menü), danach meldet on_leave: Das Pad gehört wieder dem Spiel
    sonst wie im Maus-Modus: linker Stick Zeiger, rechter Stick Rollen, R2 / L2 linke / rechte Maustaste

Terminal-Modus (profile="term", E72) für die App „Claude Code“ (couch-terminal). Was die Tasten
im Terminal tun, entscheidet die App; padmouse schickt nur feste Tasten:
    Steuerkreuz, linker Stick  Pfeiltasten        rechter Stick  Rollen
    Kreuz Enter    Kreis Escape    Quadrat Rücktaste    R1 Tab
    Dreieck        F9 (die App öffnet ihr Schreibfeld), dann die Bildschirmtastatur
    Options        F12 (Befehle)   L1  F10 (Umschalt+Tab: Modus)   L2 / R2  F7 / F8 (Blättern)

Aufbau: PadMouse ist reine Logik und schreibt in eine Senke (Sink):
    XTestSink    XTEST ins Xwayland von Gamescope (kein Gerät im Kernel)
    LogSink      nur mitschreiben, zum Abstimmen der Kurven am echten Pad
Virtuelle Eingabegeräte (uinput) gibt es hier bewusst nicht: Steam greift sich jedes neue
Gerät und macht daraus Eingaben auf dem Desktop.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Protocol

from . import ds_codes as c
from .padlogic import PadState, curve

# Ausgabe-Codes (linux/input-event-codes.h)
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
KEY_PAGEUP, KEY_PAGEDOWN, KEY_INSERT = 104, 109, 110
WHEEL_NOTCH = 120                      # eine Raste in hochauflösenden Rolleinheiten

KEYS = {c.CIRCLE: KEY_ESC, c.SQUARE: KEY_BACKSPACE}
# Tasten-Modus (profile="keys") für Apps mit Fernseh-Oberfläche (YouTube/VacuumTube): Steuerkreuz
# und linker Stick sind Pfeiltasten, Kreuz Enter, Kreis Escape, Quadrat Rücktaste; kein Zeiger,
# kein Rollen, keine Maustasten. VacuumTubes eigene Pad-Abfrage kam im Game-Mode nicht an
# (2026-10-15 am Fernseher), Pfeiltasten über XTEST schon.
KEYS_TV = {c.CROSS: KEY_ENTER, c.CIRCLE: KEY_ESC, c.SQUARE: KEY_BACKSPACE}
STICK_KEY_ON, STICK_KEY_OFF = 0.6, 0.4    # Stick als Pfeiltaste: ab hier gedrückt, darunter los
BUTTONS = {c.CROSS: BTN_LEFT, c.L1: BTN_SIDE, c.R1: BTN_EXTRA, c.R3: BTN_MIDDLE}
# Spiel-Modus (profile="game", E64): Tastencodes nach Lage, nicht nach Belegung (W bleibt W, auch bei QWERTZ)
KEY_Q, KEY_W, KEY_E, KEY_R, KEY_A, KEY_S, KEY_D, KEY_F, KEY_C = 16, 17, 18, 19, 30, 31, 32, 33, 46
KEY_SPACE, KEY_LEFTSHIFT = 57, 42
KEY_F7, KEY_F8, KEY_F9, KEY_F10, KEY_F12 = 65, 66, 67, 68, 88
KEYS_GAME = {c.CROSS: KEY_SPACE, c.CIRCLE: KEY_C, c.SQUARE: KEY_R, c.TRIANGLE: KEY_E, c.L1: KEY_Q, c.R1: KEY_F,
             c.L3: KEY_LEFTSHIFT, c.OPTIONS: KEY_ESC, c.CREATE: KEY_TAB}
BUTTONS_GAME = {c.R3: BTN_MIDDLE}
# E109: Tastenbelegung je Spiel im Spiel-Modus. Namen der Pad-Tasten und Ziele wie in library.model
# (PAD_KEY_BUTTONS, PAD_KEY_TARGETS); ein Ziel ist ("key", Code nach Lage), ("button", Maustaste),
# ("wheel", +1 hoch / -1 runter) oder None (nichts). Das Steuerkreuz zählt je Richtung als Taste.
KEY_LEFTCTRL, KEY_LEFTALT = 29, 56
PAD_KEY_CODES = {"cross": c.CROSS, "circle": c.CIRCLE, "square": c.SQUARE, "triangle": c.TRIANGLE,
                 "l1": c.L1, "r1": c.R1, "l2": c.L2, "r2": c.R2, "l3": c.L3, "r3": c.R3,
                 "options": c.OPTIONS, "create": c.CREATE, "dpad_up": c.BTN_DPAD_UP, "dpad_down": c.BTN_DPAD_DOWN,
                 "dpad_left": c.BTN_DPAD_LEFT, "dpad_right": c.BTN_DPAD_RIGHT}
TARGET_CODES = {
    **{ch: ("key", code) for ch, code in zip("qwertyuiop", range(16, 26))},
    **{ch: ("key", code) for ch, code in zip("asdfghjkl", range(30, 39))},
    **{ch: ("key", code) for ch, code in zip("zxcvbnm", range(44, 51))},
    **{str(n % 10): ("key", 1 + n) for n in range(1, 11)},              # 1 … 9 = 2 … 10, 0 = 11
    **{"f%d" % n: ("key", 58 + n) for n in range(1, 11)}, "f11": ("key", 87), "f12": ("key", KEY_F12),
    "space": ("key", KEY_SPACE), "enter": ("key", KEY_ENTER), "esc": ("key", KEY_ESC), "tab": ("key", KEY_TAB),
    "backspace": ("key", KEY_BACKSPACE), "shift": ("key", KEY_LEFTSHIFT), "ctrl": ("key", KEY_LEFTCTRL),
    "alt": ("key", KEY_LEFTALT), "up": ("key", KEY_UP), "down": ("key", KEY_DOWN), "left": ("key", KEY_LEFT),
    "right": ("key", KEY_RIGHT), "mouse_left": ("button", BTN_LEFT), "mouse_right": ("button", BTN_RIGHT),
    "mouse_middle": ("button", BTN_MIDDLE), "wheel_up": ("wheel", 1), "wheel_down": ("wheel", -1), "none": None,
}
# Die Belegung von E64 als solche Tabelle: KEYS_GAME, BUTTONS_GAME, R2/L2 linke/rechte Maustaste, Steuerkreuz Pfeile
GAME_DEFAULT = {**{b: ("key", k) for b, k in KEYS_GAME.items()}, **{b: ("button", k) for b, k in BUTTONS_GAME.items()},
                c.R2: ("button", BTN_LEFT), c.L2: ("button", BTN_RIGHT),
                c.BTN_DPAD_UP: ("key", KEY_UP), c.BTN_DPAD_DOWN: ("key", KEY_DOWN),
                c.BTN_DPAD_LEFT: ("key", KEY_LEFT), c.BTN_DPAD_RIGHT: ("key", KEY_RIGHT)}


def game_keymap(keys: dict | None = None) -> dict:
    """Wirksame Belegung des Spiel-Modus: GAME_DEFAULT (E64), darüber keys {taste: ziel} (Option pad_keys,
    E109). Unbekannte Tasten und Ziele fallen weg; geprüft wird schon in library.model.pad_keys_value."""
    table = dict(GAME_DEFAULT)
    for name, target in (keys.items() if isinstance(keys, dict) else ()):
        code = PAD_KEY_CODES.get(name)
        if code is not None and isinstance(target, str) and target in TARGET_CODES:
            table[code] = TARGET_CODES[target]
    return table

# Terminal-Modus (profile="term", E72): Dreieck (F9) öffnet zusätzlich die Bildschirmtastatur
KEYS_TERM = {c.CROSS: KEY_ENTER, c.CIRCLE: KEY_ESC, c.SQUARE: KEY_BACKSPACE, c.TRIANGLE: KEY_F9,
             c.OPTIONS: KEY_F12, c.L1: KEY_F10, c.R1: KEY_TAB, c.L2: KEY_F7, c.R2: KEY_F8}
TRIGGERS_TERM = {c.L2_ANALOG: KEY_F7, c.R2_ANALOG: KEY_F8}
# OptiScaler-Menü (profile="opti", E118): Kreuz ist hier die Leertaste (Auswahl in ImGui), geklickt wird mit R2
KEYS_OPTI = {c.CROSS: KEY_SPACE, c.L1: KEY_PAGEUP, c.R1: KEY_PAGEDOWN, c.OPTIONS: KEY_INSERT}
# Zeiger-Modus (profile="cards", E97)
KEYS_CARDS = {c.OPTIONS: KEY_ESC}
BUTTONS_CARDS = {c.CROSS: BTN_LEFT, c.CIRCLE: BTN_RIGHT, c.R3: BTN_MIDDLE}
DPAD_SPOTS = {(0, -1): c.BTN_DPAD_UP, (0, 1): c.BTN_DPAD_DOWN, (-1, 0): c.BTN_DPAD_LEFT, (1, 0): c.BTN_DPAD_RIGHT}
DPAD_BUTTONS = frozenset(DPAD_SPOTS.values())      # Steuerkreuz als Tasten (hid-steam u. a.), PadState.hat
# Sprungstellen je Spiel als Anteil des Bildschirms (0…1, links oben 0/0). Hearthstone (16:9; das Brett
# skaliert mit der Bildhöhe): eigene Hand unten, eigener Held und Heldenfähigkeit darüber, gegnerischer
# Held oben, „Zug beenden“ rechts in der Mitte.
LAYOUTS = {
    "hearthstone": {
        c.BTN_DPAD_UP: (0.500, 0.190),      # gegnerischer Held
        c.BTN_DPAD_DOWN: (0.500, 0.930),    # eigene Hand (Mitte)
        c.BTN_DPAD_LEFT: (0.500, 0.760),    # eigener Held
        c.BTN_DPAD_RIGHT: (0.592, 0.755),   # Heldenfähigkeit
        c.L1: (0.500, 0.550),               # eigenes Spielfeld: Diener ablegen
        c.R1: (0.812, 0.455),               # Zug beenden
    },
}
WARP_MIN_WINDOW = 200        # kleinere Fenster mit Fokus (Helfer, 1×1) zählen nicht als Spielfenster
# Eigene Sprungstellen (E99): Create halten + Taste merkt die Zeigerstelle. Namen wie in der Option
# pad_spots (library.model.PAD_SPOT_KEYS); gespeichert als Anteil des Fensters samt seiner Größe
SPOT_BUTTONS = {"up": c.BTN_DPAD_UP, "down": c.BTN_DPAD_DOWN, "left": c.BTN_DPAD_LEFT, "right": c.BTN_DPAD_RIGHT,
                "l1": c.L1, "r1": c.R1}
SPOT_NAMES = {code: name for name, code in SPOT_BUTTONS.items()}
SPOT_HOLD = c.CREATE
# Eine eigene Stelle gilt nur bei (fast) gleichem Seitenverhältnis wie beim Merken: Mit einer anderen
# Auflösung je Spiel verschiebt sich das Bild, der Anteil träfe daneben (dann die eingebaute Stelle)
ASPECT_TOLERANCE = 0.03
ARROWS = (KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN)
WASD = (KEY_A, KEY_D, KEY_W, KEY_S)
PROFILES = ("mouse", "keys", "game", "term", "cards", "opti")
OSK_BUTTON, PRECISION_BUTTON = c.TRIANGLE, c.L3


class Sink(Protocol):
    def move(self, dx: int, dy: int) -> None: ...
    def warp(self, fx: float, fy: float, aspect: float | None = None) -> bool | None: ...   # an eine Stelle, als Anteil
    # (fx, fy, Breite, Höhe) der Zeigerstelle wie warp sie meint, None = unbekannt (eigene Sprungstellen, E99)
    def where(self) -> tuple | None: ...
    def scroll(self, v_hires: int, v_steps: int, h_hires: int, h_steps: int) -> None: ...
    def button(self, code: int, down: bool) -> None: ...
    def key(self, code: int, down: bool) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


@dataclass
class Config:
    deadzone: float = 0.18
    pointer_min: float = 45.0          # Pixel/s direkt hinter der Totzone
    pointer_max: float = 1500.0        # Pixel/s bei voller Auslenkung
    pointer_exponent: float = 2.2      # >1: viel Weg im langsamen Bereich
    pointer_boost: float = 1.6         # nach boost_after s Vollausschlag: Faktor für weite Wege
    boost_after: float = 0.45
    precision: float = 0.30            # Faktor, solange L3 gehalten wird
    scroll_max: float = 16.0           # Rasten/s bei voller Auslenkung
    scroll_exponent: float = 1.8
    look_min: float = 60.0             # Spiel-Modus: rechter Stick als Maus, Pixel/s hinter der Totzone …
    look_max: float = 1400.0           # … und bei voller Auslenkung (mal Faktor look aus den Spieleinstellungen)
    look_exponent: float = 1.6         # flacher als beim Zeiger: Zielen braucht gleichmäßiges Tempo
    walk_on: float = 0.45              # Spiel-Modus: linker Stick als W A S D, ab hier gedrückt …
    walk_off: float = 0.30             # … und erst darunter los
    trigger_press: float = 0.55        # Auslöser zählt ab hier als gedrückt …
    trigger_release: float = 0.35      # … und erst darunter als los (sonst flattert die Maustaste)
    analog_triggers: bool = True       # False für Pads ohne analoge Auslöser: dann BTN_TL2/BTN_TR2


class PadMouse:
    """Übersetzt die Ereignisse EINES Gamepad-Geräts. Mehrere Pads teilen sich eine Senke.

    Mit eigenem Zustand (feed) oder auf dem Zustand des Lesers (state=…, handle): Im Dienst
    führt der PadManager den Zustand ohnehin, dann wird er nicht doppelt gepflegt.
    """

    def __init__(self, sink: Sink, config: Config | None = None, osk_toggle: Callable[[], None] | None = None,
                 state: PadState | None = None, profile: str = "mouse", look: float = 1.0, layout: str = "",
                 spots: dict | None = None, on_spot: Callable[[str, dict], None] | None = None,
                 keys: dict | None = None, on_leave: Callable[[], None] | None = None):
        self.sink = sink
        self.on_leave = on_leave                # OptiScaler-Menü: Options hat es geschlossen (E118)
        self.profile = profile if profile in PROFILES else "mouse"
        # Spiel-Modus: wirksame Belegung je Pad-Taste (E64, mit keys = Option pad_keys umbelegt, E109)
        self._game = game_keymap(keys) if self.profile == "game" else {}
        # Zeiger-Modus: eingebaute Sprungstellen (fx, fy) und darüber eigene (fx, fy, w, h; E99)
        self._base = dict(LAYOUTS.get(layout, {})) if self.profile == "cards" else {}
        self._own: dict[int, tuple] = {}
        self.on_spot = on_spot                  # (name, {x, y, w, h}): eine Stelle wurde gemerkt
        self.spot_taken = False                 # dieser Create-Druck hat eine Stelle gemerkt (kein Bildschirmfoto)
        self.set_spots(spots)
        self.look = min(4.0, max(0.25, float(look or 1.0)))     # Spiel-Modus: Tempo des Umschauens
        self._stick = (0, 0)                    # Tasten- und Spiel-Modus: linker Stick als Richtung
        self.cfg = config or Config()
        self.osk_toggle = osk_toggle
        self.state = state if state is not None else PadState()
        self.enabled = False
        # nur was wir selbst gedrückt haben, lassen wir los: Taste -> Quellen (E109: zwei Pad-Tasten mit
        # demselben Ziel teilen sich die Taste wie Kreuz und R2 die linke Maustaste)
        self._held_keys: dict[int, set] = {}
        self._held_buttons: dict[int, set] = {}  # Maustaste -> Quellen (Kreuz und R2 teilen sich die linke)
        self._hat = (0, 0)
        self._hat_due = False                   # Zeiger-Modus: Steuerkreuz am Ende des Berichts auswerten (E99)
        self._trig: dict[int, bool] = {}        # Auslöser logisch gedrückt (auch im stummen Zustand geführt)
        self._acc = [0.0, 0.0, 0.0, 0.0]        # Reste: Zeiger x/y, Rollen senkrecht/waagrecht (hi-res)
        self._steps = [0, 0]                    # hi-res-Reste für ganze Rasten (alte Programme)
        self._last_tick: float | None = None
        self._full_since: float | None = None

    # --- ein/aus -----------------------------------------------------------------------
    def set_enabled(self, on: bool) -> None:
        if on == self.enabled:
            return
        self.enabled = on
        self._acc = [0.0, 0.0, 0.0, 0.0]
        self._steps = [0, 0]
        self._last_tick = None
        self._full_since = None
        if not on:
            self.release_all()
        else:
            self.adopt_state()

    def adopt_state(self) -> None:
        """Was jetzt schon gehalten wird, gilt als bekannt: nur neue Tastendrücke lösen etwas aus.

        Wer mit Kreuz den Desktop startet und R2 noch hält, soll dort nicht sofort klicken.
        """
        self._hat = self.state.hat
        self._trig = {axis: self.state.trigger(axis) >= self.cfg.trigger_press
                      for axis in (c.R2_ANALOG, c.L2_ANALOG)}

    def release_all(self) -> None:
        """Hängende Tasten wären fatal (Maustaste dauerhaft gedrückt): alles loslassen."""
        for code in sorted(self._held_keys):
            self.sink.key(code, False)
        for code in sorted(self._held_buttons):
            self.sink.button(code, False)
        self._held_keys.clear()
        self._held_buttons.clear()
        self._hat = (0, 0)
        self._stick = (0, 0)
        self.sink.flush()

    def reset_clock(self) -> None:
        """Nach einer Pause des Takts: Der erste Schritt misst nur die Zeit, bewegt nichts."""
        self._last_tick = None

    @property
    def wants_tick(self) -> bool:
        """Ein Stick ist ausgelenkt: Zeiger oder Rollen brauchen den Takt."""
        if not self.enabled:
            return False
        dz = self.cfg.deadzone
        if self.profile == "keys":
            return self.state.left(dz) != (0.0, 0.0) or self._stick != (0, 0)
        if self.profile in ("game", "term"):
            return self.state.left(dz) != (0.0, 0.0) or self._stick != (0, 0) or self.state.right(dz) != (0.0, 0.0)
        return self.state.left(dz) != (0.0, 0.0) or self.state.right(dz) != (0.0, 0.0)

    @property
    def spots(self) -> dict:
        """Sprungstellen je Taste: eingebaute, darüber die eigenen."""
        return {**self._base, **self._own}

    def set_spots(self, spots: dict | None) -> None:
        """Eigene Sprungstellen {name: {x, y, w, h}} (Option pad_spots, E99); nur im Zeiger-Modus."""
        own: dict[int, tuple] = {}
        if self.profile == "cards":
            for name, spot in (spots or {}).items():
                code = SPOT_BUTTONS.get(name)
                try:
                    if code is not None:
                        own[code] = (float(spot["x"]), float(spot["y"]), int(spot["w"]), int(spot["h"]))
                except (KeyError, TypeError, ValueError):
                    continue
        self._own = own

    @property
    def _analog(self) -> bool:
        return self.cfg.analog_triggers and self.state.trigger_axes is not None

    # --- Ausgabe mit Buchführung -------------------------------------------------------
    def _key(self, code: int, down: bool, source: int | None = None) -> None:
        """Taste drücken bzw. loslassen. source: welche Pad-Taste sie hält (E109); los geht sie erst, wenn
        keine Quelle sie mehr hält. Ohne source (Sticks, die festen Belegungen) wie bisher eine Quelle."""
        sources = self._held_keys.get(code)
        if down and self.enabled:
            if sources is None:
                self._held_keys[code] = {source}
                self.sink.key(code, True)
            else:
                sources.add(source)
        elif not down and sources is not None and source in sources:   # loslassen auch im stummen Zustand
            sources.discard(source)
            if not sources:
                del self._held_keys[code]
                self.sink.key(code, False)

    def _press(self, target: tuple | None, down: bool, source: int) -> None:
        """Spiel-Modus (E109): ein Ziel aus game_keymap drücken bzw. loslassen. Das Mausrad rastet je Druck
        einmal; None (nichts) tut nichts."""
        if target is None:
            return
        kind, code = target
        if kind == "key":
            self._key(code, down, source)
        elif kind == "button":
            self._button(code, down, source)
        elif kind == "wheel" and down and self.enabled:
            self.sink.scroll(code * WHEEL_NOTCH, code, 0, 0)

    def _button(self, code: int, down: bool, source: int) -> None:
        sources = self._held_buttons.get(code)
        if down and self.enabled:
            if sources is None:
                self._held_buttons[code] = {source}
                self.sink.button(code, True)
            else:
                sources.add(source)
        elif not down and sources is not None:          # loslassen auch im stummen Zustand
            sources.discard(source)
            if not sources:
                del self._held_buttons[code]
                self.sink.button(code, False)

    # --- Eingabe -----------------------------------------------------------------------
    def feed(self, etype: int, code: int, value: int) -> None:
        """Rohes Ereignis bei eigenem Zustand."""
        self.handle(etype, code, self.state.feed(etype, code, value))

    def handle(self, etype: int, code: int, change: tuple | None) -> None:
        """Rohes Ereignis, nachdem der Zustand es schon aufgenommen hat (change = Tastenwechsel)."""
        if etype == c.EV_SYN:
            if self._hat_due:
                self._hat_due = False
                self._dpad_spots()
            self.sink.flush()
            return
        if change and change[1] in DPAD_BUTTONS:
            # Meldet das Pad sein Steuerkreuz als Tasten statt als Achsen, gilt dasselbe wie für die
            # Achsen; beide Wege lesen state.hat (E99). Sonst kämen in keinem Profil Pfeiltasten an.
            if self.profile == "cards":
                self._hat_due = True
            elif self.profile == "game":
                self._dpad_game()
            else:
                self._dpad()
            return
        if self.profile == "keys":
            if change:
                _, button, down = change
                if button in KEYS_TV:
                    self._key(KEYS_TV[button], bool(down))
            elif etype == c.EV_ABS and code in (c.DPAD_X, c.DPAD_Y):
                self._dpad()
            return
        if self.profile == "term":
            if change:
                _, button, down = change
                if button in (c.L2, c.R2) and self._analog:
                    return                              # kommt über die Achse (_trigger_key)
                if button in KEYS_TERM:
                    self._key(KEYS_TERM[button], bool(down))
                    if button == OSK_BUTTON and down and self.enabled and self.osk_toggle:
                        self.sink.flush()               # erst das Schreibfeld der App, dann die Tastatur
                        self.osk_toggle()
            elif etype == c.EV_ABS:
                if code in (c.DPAD_X, c.DPAD_Y):
                    self._dpad()
                elif self._analog and code in TRIGGERS_TERM:
                    self._trigger_key(code, TRIGGERS_TERM[code])
            return
        if self.profile == "cards":
            if change:
                _, button, down = change
                if button == SPOT_HOLD:
                    if down:
                        self.spot_taken = False
                elif button in SPOT_NAMES and down and self.enabled and SPOT_HOLD in self.state.buttons:
                    self._remember(button)
                elif button in self.spots:
                    if down and self.enabled:
                        self._jump(button)
                elif button in KEYS_CARDS:
                    self._key(KEYS_CARDS[button], bool(down))
                elif button in BUTTONS_CARDS:
                    self._button(BUTTONS_CARDS[button], bool(down), button)
                elif button == OSK_BUTTON and down and self.enabled and self.osk_toggle:
                    self.osk_toggle()
                elif not self._analog and button in (c.R2, c.L2):
                    self._button(BTN_LEFT if button == c.R2 else BTN_RIGHT, bool(down), button)
            elif etype == c.EV_ABS:
                if code in (c.DPAD_X, c.DPAD_Y):
                    # erst beim Ende des Berichts (EV_SYN): Kommen beide Achsen in einem Bericht, ist
                    # das ein schräger Druck und kein Druck nach rechts, gefolgt von einem nach oben
                    self._hat_due = True
                elif self._analog and code in (c.R2_ANALOG, c.L2_ANALOG):
                    self._trigger(code, BTN_LEFT if code == c.R2_ANALOG else BTN_RIGHT)
            return
        if self.profile == "game":
            # Belegung aus game_keymap (E64, umbelegt nach E109); jede Pad-Taste ist ihre eigene Quelle
            if change:
                _, button, down = change
                if button in (c.R2, c.L2) and self._analog:
                    return                              # kommt über die Achse (_trigger_target)
                if button in self._game:
                    self._press(self._game[button], bool(down), button)
            elif etype == c.EV_ABS:
                if code in (c.DPAD_X, c.DPAD_Y):
                    self._dpad_game()
                elif self._analog and code in (c.R2_ANALOG, c.L2_ANALOG):
                    self._trigger_target(code, self._game.get(c.R2 if code == c.R2_ANALOG else c.L2))
            return
        if change:
            _, button, down = change
            if self.profile == "opti" and button in KEYS_OPTI:
                self._key(KEYS_OPTI[button], bool(down))
                if button == c.OPTIONS and not down and self.enabled and self.on_leave is not None:
                    self.sink.flush()                   # erst Einfg, dann zurück zum Spiel
                    self.on_leave()
            elif button in KEYS:
                self._key(KEYS[button], bool(down))
            elif button in BUTTONS:
                self._button(BUTTONS[button], bool(down), button)
            elif button == OSK_BUTTON and down and self.enabled and self.osk_toggle:
                self.osk_toggle()
            elif not self._analog and button in (c.R2, c.L2):
                self._button(BTN_LEFT if button == c.R2 else BTN_RIGHT, bool(down), button)
        elif etype == c.EV_ABS:
            if code in (c.DPAD_X, c.DPAD_Y):
                self._dpad()
            elif self._analog and code in (c.R2_ANALOG, c.L2_ANALOG):
                self._trigger(code, BTN_LEFT if code == c.R2_ANALOG else BTN_RIGHT)

    def _trigger(self, axis: int, out: int) -> None:
        v = self.state.trigger(axis)
        was = self._trig.get(axis, False)
        down = v > self.cfg.trigger_release if was else v >= self.cfg.trigger_press
        if down != was:
            self._trig[axis] = down
            self._button(out, down, axis)

    def _trigger_target(self, axis: int, target: tuple | None) -> None:
        """Spiel-Modus: Auslöser auf sein Ziel (E109; ohne eigene Belegung R2/L2 linke/rechte Maustaste),
        mit derselben Schwelle und Quelle (die Achse) wie _trigger."""
        v = self.state.trigger(axis)
        was = self._trig.get(axis, False)
        down = v > self.cfg.trigger_release if was else v >= self.cfg.trigger_press
        if down != was:
            self._trig[axis] = down
            self._press(target, down, axis)

    def _trigger_key(self, axis: int, key: int) -> None:
        """Auslöser als Taste (Terminal-Modus), mit derselben Schwelle wie die Maustasten."""
        v = self.state.trigger(axis)
        was = self._trig.get(axis, False)
        down = v > self.cfg.trigger_release if was else v >= self.cfg.trigger_press
        if down != was:
            self._trig[axis] = down
            self._key(key, down)

    def _arrows(self, old: tuple, new: tuple, keys: tuple = ARROWS) -> None:
        for old_v, new_v, neg, pos in ((old[0], new[0], keys[0], keys[1]),
                                       (old[1], new[1], keys[2], keys[3])):
            if old_v == new_v:
                continue
            if old_v:
                self._key(neg if old_v < 0 else pos, False)
            if new_v:
                self._key(neg if new_v < 0 else pos, True)

    def _stick_keys(self, keys: tuple = ARROWS, on: float = STICK_KEY_ON, off: float = STICK_KEY_OFF) -> None:
        """Linker Stick als Tasten (Pfeile bzw. W A S D), je Achse mit Schwelle und Rückfall."""
        lx, ly = self.state.left(self.cfg.deadzone)
        new = []
        for v, old in ((lx, self._stick[0]), (ly, self._stick[1])):
            if old and abs(v) > off and (v > 0) == (old > 0):
                new.append(old)                         # bleibt gedrückt, bis er zurückgeht
            elif abs(v) >= on:
                new.append(1 if v > 0 else -1)
            else:
                new.append(0)
        new = tuple(new)
        if new != self._stick:
            self._arrows(self._stick, new, keys)
            self._stick = new
            self.sink.flush()

    def _look(self, dt: float) -> bool:
        """Spiel-Modus: rechter Stick als relative Mausbewegung. True, wenn sich etwas bewegt hat."""
        cfg = self.cfg
        rx, ry = self.state.right(cfg.deadzone)
        mag = (rx * rx + ry * ry) ** 0.5
        if mag <= 0.0:
            self._acc[0] = self._acc[1] = 0.0
            return False
        speed = (cfg.look_min + (cfg.look_max - cfg.look_min) * curve(mag, cfg.look_exponent)) * self.look
        self._acc[0] += rx / mag * speed * dt
        self._acc[1] += ry / mag * speed * dt
        dx, dy = int(self._acc[0]), int(self._acc[1])
        if not (dx or dy):
            return False
        self._acc[0] -= dx
        self._acc[1] -= dy
        self.sink.move(dx, dy)
        return True

    def _dpad(self) -> None:
        new = self.state.hat
        for old_v, new_v, neg, pos in ((self._hat[0], new[0], KEY_LEFT, KEY_RIGHT),
                                       (self._hat[1], new[1], KEY_UP, KEY_DOWN)):
            if old_v == new_v:
                continue
            if old_v:
                self._key(neg if old_v < 0 else pos, False)
            if new_v:
                self._key(neg if new_v < 0 else pos, True)
        self._hat = new

    def _dpad_game(self) -> None:
        """Spiel-Modus: das Steuerkreuz je Richtung auf ihr Ziel (E109), in derselben Reihenfolge wie _dpad
        (je Achse erst die alte Richtung los, dann die neue); ohne eigene Belegung also Pfeiltasten."""
        new = self.state.hat
        for old_v, new_v, neg, pos in ((self._hat[0], new[0], c.BTN_DPAD_LEFT, c.BTN_DPAD_RIGHT),
                                       (self._hat[1], new[1], c.BTN_DPAD_UP, c.BTN_DPAD_DOWN)):
            if old_v == new_v:
                continue
            if old_v:
                way = neg if old_v < 0 else pos
                self._press(self._game.get(way), False, way)
            if new_v:
                way = neg if new_v < 0 else pos
                self._press(self._game.get(way), True, way)
        self._hat = new

    def _dpad_spots(self) -> None:
        """Zeiger-Modus, einmal je Bericht: Das Steuerkreuz springt zur Stelle der neu gedrückten
        Richtung; eine Richtung ohne Stelle bleibt Pfeiltaste (eine einzelne eigene Stelle nimmt den
        anderen nichts, E99). Mit gehaltenem Create merkt die Richtung stattdessen die Zeigerstelle,
        aber nie schräg: Beide Richtungen bekämen sonst dieselbe Stelle (E99)."""
        old, new = self._hat, self.state.hat
        self._hat = new
        spots = self.spots
        for axis, (neg, pos) in enumerate(((KEY_LEFT, KEY_RIGHT), (KEY_UP, KEY_DOWN))):
            if old[axis] == new[axis]:
                continue
            if old[axis]:
                self._key(neg if old[axis] < 0 else pos, False)     # nur, wenn wir sie gedrückt haben
            if not new[axis]:
                continue
            button = DPAD_SPOTS[(new[axis], 0) if axis == 0 else (0, new[axis])]
            if SPOT_HOLD in self.state.buttons:
                if self.enabled and new[0] and new[1]:
                    self.spot_taken = True      # schräg: nichts merken, aber auch kein Bildschirmfoto
                elif self.enabled:
                    self._remember(button)
            elif button in spots:
                if self.enabled:
                    self._jump(button)
            else:
                self._key(neg if new[axis] < 0 else pos, True)

    def _jump(self, button: int) -> None:
        """Sprung zur Stelle der Taste, nie mit Klick. Eine eigene Stelle passt nicht mehr, wenn sich
        das Seitenverhältnis des Fensters geändert hat: dann gilt die eingebaute, sonst keine."""
        own = self._own.get(button)
        if own is not None and own[3] > 0 and self.sink.warp(own[0], own[1], own[2] / own[3]) is not False:
            return
        base = self._base.get(button)
        if base is not None:
            self.sink.warp(*base)

    def _remember(self, button: int) -> None:
        """Create gehalten + Sprungtaste (E99): jetzige Zeigerstelle als eigene Stelle dieser Taste."""
        self.spot_taken = True                  # auch ohne Ergebnis: dieser Create-Druck ist kein Foto
        where = getattr(self.sink, "where", None)
        pos = where() if where is not None else None
        if not pos:
            return
        fx, fy, w, h = pos
        self._own[button] = (fx, fy, w, h)
        if self.on_spot is not None:
            self.on_spot(SPOT_NAMES[button], {"x": fx, "y": fy, "w": w, "h": h})

    # --- Bewegung ----------------------------------------------------------------------
    def tick(self, now: float) -> None:
        """Zeiger und Rollen aus dem aktuellen Stick-Stand. Im Takt aufrufen, solange wants_tick."""
        last, self._last_tick = self._last_tick, now
        if self.profile == "keys":
            if self.enabled:
                self._stick_keys()
            return
        if self.profile == "game":
            if self.enabled:
                self._stick_keys(WASD, self.cfg.walk_on, self.cfg.walk_off)
                if last is not None and self._look(min(now - last, 0.05)):
                    self.sink.flush()
            return
        if self.profile == "term":
            if self.enabled:
                self._stick_keys()
                if last is not None and self._scroll(min(now - last, 0.05)):
                    self.sink.flush()
            return
        if not self.enabled or last is None:
            return
        dt = min(now - last, 0.05)             # nach einem Hänger keinen Sprung machen
        cfg = self.cfg
        moved = False

        lx, ly = self.state.left(cfg.deadzone)
        mag = (lx * lx + ly * ly) ** 0.5
        if mag > 0.0:
            speed = cfg.pointer_min + (cfg.pointer_max - cfg.pointer_min) * curve(mag, cfg.pointer_exponent)
            if mag >= 0.98:
                self._full_since = self._full_since if self._full_since is not None else now
                if now - self._full_since >= cfg.boost_after:
                    speed *= cfg.pointer_boost
            else:
                self._full_since = None
            if PRECISION_BUTTON in self.state.buttons:
                speed *= cfg.precision
            self._acc[0] += lx / mag * speed * dt
            self._acc[1] += ly / mag * speed * dt
            dx, dy = int(self._acc[0]), int(self._acc[1])      # Rest behalten: langsam = gleichmäßig
            if dx or dy:
                self._acc[0] -= dx
                self._acc[1] -= dy
                self.sink.move(dx, dy)
                moved = True
        else:
            self._full_since = None
            self._acc[0] = self._acc[1] = 0.0

        if self._scroll(dt):
            moved = True
        if moved:
            self.sink.flush()

    def _scroll(self, dt: float) -> bool:
        """Rechter Stick als Mausrad. True, wenn gerollt wurde."""
        cfg = self.cfg
        rx, ry = self.state.right(cfg.deadzone)
        rmag = (rx * rx + ry * ry) ** 0.5
        if rmag <= 0.0:
            self._acc[2] = self._acc[3] = 0.0
            self._steps = [0, 0]
            return False
        rate = cfg.scroll_max * curve(rmag, cfg.scroll_exponent) * WHEEL_NOTCH * dt
        # Stick nach unten (ry > 0) rollt nach unten = negative Rollrichtung
        self._acc[2] += -ry / rmag * rate
        self._acc[3] += rx / rmag * rate
        v, h = int(self._acc[2]), int(self._acc[3])
        if not (v or h):
            return False
        self._acc[2] -= v
        self._acc[3] -= h
        self._steps[0] += v
        self._steps[1] += h
        vs, hs = int(self._steps[0] / WHEEL_NOTCH), int(self._steps[1] / WHEEL_NOTCH)
        self._steps[0] -= vs * WHEEL_NOTCH
        self._steps[1] -= hs * WHEEL_NOTCH
        self.sink.scroll(v, vs, h, hs)
        return True


# --- Senken ----------------------------------------------------------------------------
class LogSink:
    """Schreibt nur mit. Zum Abstimmen der Kurven am echten Pad, ohne jede Wirkung."""

    def __init__(self, out=sys.stdout):
        self.out = out

    def move(self, dx, dy): print(f"move {dx:+d} {dy:+d}", file=self.out)
    def warp(self, fx, fy, aspect=None): print(f"warp {fx:.3f} {fy:.3f}", file=self.out)
    def where(self): return None
    def scroll(self, v, vs, h, hs): print(f"scroll v={v:+d} ({vs:+d}) h={h:+d} ({hs:+d})", file=self.out)
    def button(self, code, down): print(f"button {code:#x} {'down' if down else 'up'}", file=self.out)
    def key(self, code, down): print(f"key {code} {'down' if down else 'up'}", file=self.out)
    def flush(self): self.out.flush()
    def close(self): pass


class XTestSink:
    """Eingaben über die X-Erweiterung XTEST ins Xwayland von Gamescope.

    Es entsteht kein Gerät im Kernel: nichts, was Steam als Controller greifen könnte, nichts,
    was nach einem Absturz übrig bleibt, und die Eingaben landen nur in dieser Gamescope-Sitzung.
    Verweigert jedes Display, das kein Gamescope-Xwayland ist (xtest.open_display).

    Grenzen: Rollen nur in ganzen Rasten (X-Tasten 4-7). Tasten gehen als Tastencode
    (evdev-Code + 8) und brauchen deshalb keine Belegung. Umschalttasten kommen in der echten
    Sitzung an (xtest.py, Punkt 1; KEYS_GAME schickt L3 als Shift), nur im unsichtbaren Gamescope nicht.
    UNBESTÄTIGT: nur im unsichtbaren Gamescope geprüft, nicht im echten Game-Mode am Fernseher.
    """
    X_BUTTON = {BTN_LEFT: 1, BTN_MIDDLE: 2, BTN_RIGHT: 3, BTN_SIDE: 8, BTN_EXTRA: 9}

    def __init__(self, display_name: str | None = None):
        from Xlib import X             # erst hier: Unit-Tests brauchen kein X
        from Xlib.ext import xtest as xt
        from .xtest import open_display
        self._X, self._fake = X, xt.fake_input
        self._d = open_display(display_name)
        self.display_name = self._d.get_display_name()

    def move(self, dx, dy):
        self._fake(self._d, self._X.MotionNotify, detail=1, x=dx, y=dy)      # detail=1: relativ

    def _area(self) -> tuple[int, int, int, int]:
        """Worauf sich Anteile beziehen: das Fenster mit dem Fokus (ein Spiel kann kleiner sein als die
        Wurzel; Gamescope streckt es nur im Bild), ohne brauchbares Fenster der ganze Bildschirm."""
        s = self._d.screen()
        area = (0, 0, s.width_in_pixels, s.height_in_pixels)
        try:
            win = self._d.get_input_focus().focus
            g = win.get_geometry()
            if g.width >= WARP_MIN_WINDOW and g.height >= WARP_MIN_WINDOW:
                t = s.root.translate_coords(win, 0, 0)
                area = (t.x, t.y, g.width, g.height)
        except Exception:
            pass                        # Fokus PointerRoot/None oder Fenster eben verschwunden
        return area

    def warp(self, fx, fy, aspect=None):
        """Sprung an eine Stelle (Anteil von _area). aspect: Seitenverhältnis beim Merken einer eigenen
        Stelle (E99); weicht das Fenster davon ab, springt nichts und es kommt False zurück."""
        s = self._d.screen()
        x0, y0, w, h = self._area()
        if aspect and abs(w / h - aspect) > ASPECT_TOLERANCE * aspect:
            return False
        x = x0 + round(min(1.0, max(0.0, fx)) * (w - 1))
        y = y0 + round(min(1.0, max(0.0, fy)) * (h - 1))
        self._fake(self._d, self._X.MotionNotify, detail=0, root=s.root, x=x, y=y)   # detail=0: absolut
        return True

    def where(self):
        """Zeigerstelle als Anteil von _area (die Umkehrung von warp) samt dessen Größe: (fx, fy, w, h).
        None, wenn der Zeiger neben dem Fenster mit dem Fokus steht: Eine an den Rand geschobene
        Stelle wäre nicht die, die man gezeigt hat (E99)."""
        p = self._d.screen().root.query_pointer()
        x0, y0, w, h = self._area()
        if not (x0 <= p.root_x < x0 + w and y0 <= p.root_y < y0 + h):
            return None
        fx = min(1.0, max(0.0, (p.root_x - x0) / max(1, w - 1)))
        fy = min(1.0, max(0.0, (p.root_y - y0) / max(1, h - 1)))
        return round(fx, 4), round(fy, 4), int(w), int(h)

    def _click(self, button, times):
        for _ in range(times):
            self._fake(self._d, self._X.ButtonPress, button)
            self._fake(self._d, self._X.ButtonRelease, button)

    def scroll(self, v, vs, h, hs):
        # positiv heißt nach oben = X-Taste 4 bzw. nach rechts = X-Taste 7
        if vs:
            self._click(4 if vs > 0 else 5, abs(vs))
        if hs:
            self._click(7 if hs > 0 else 6, abs(hs))

    def button(self, code, down):
        self._fake(self._d, self._X.ButtonPress if down else self._X.ButtonRelease, self.X_BUTTON[code])

    def key(self, code, down):
        self._fake(self._d, self._X.KeyPress if down else self._X.KeyRelease, code + 8)

    def flush(self):
        self._d.flush()

    def pointer(self) -> tuple[int, int]:
        p = self._d.screen().root.query_pointer()
        return p.root_x, p.root_y

    def close(self):
        self._d.close()
