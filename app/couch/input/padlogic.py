"""Reine Eingabelogik ohne Geräte: Totzone, Wiederholung, PS-Taste, Pad-Zustand, Tasten für die Oberfläche.

Alles hier bekommt die Zeit als Parameter (monotone Sekunden) und öffnet nichts. Dadurch
läuft es im Test mit erfundenen Ereignissen und im Home mit echten.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import ds_codes as c


# --- Totzone ---------------------------------------------------------------------------
def radial_deadzone(x: float, y: float, inner: float = 0.18, outer: float = 0.96) -> tuple[float, float]:
    """Vektor -1..1 je Achse -> Vektor mit Betrag 0..1, radiale Totzone.

    Radial statt je Achse, weil eine Achsen-Totzone diagonale Bewegungen ausbremst und der
    Zeiger dann an den Achsen "klebt". Der Rest wird auf 0..1 gestreckt, damit die Bewegung
    am Rand der Totzone bei null beginnt und nicht springt.
    """
    mag = math.hypot(x, y)
    if mag <= inner:
        return 0.0, 0.0
    scaled = min(1.0, (mag - inner) / (outer - inner))
    return x / mag * scaled, y / mag * scaled


def stick_vector(raw_x: int, raw_y: int, inner: float = 0.18, outer: float = 0.96) -> tuple[float, float]:
    """Rohwerte des DualSense (0..255, Ruhe 128) -> Vektor mit Betrag 0..1."""
    return radial_deadzone((raw_x - c.STICK_CENTER) / 127.0, (raw_y - c.STICK_CENTER) / 127.0, inner, outer)


def curve(magnitude: float, exponent: float) -> float:
    """Beschleunigungskurve: kleine Auslenkung fein, volle Auslenkung schnell."""
    return max(0.0, min(1.0, magnitude)) ** exponent


# --- Navigation mit Tastenwiederholung -------------------------------------------------
@dataclass
class NavRepeater:
    """Macht aus Steuerkreuz und Stick Richtungs-Schritte für Menüs.

    Erster Schritt sofort, dann Pause (delay), danach Wiederholung, die von `interval` auf
    `fast_interval` schneller wird: lange Listen lassen sich durchlaufen, ein kurzer Druck
    springt trotzdem genau einen Eintrag.
    """
    delay: float = 0.38
    interval: float = 0.13
    fast_interval: float = 0.055
    fast_after: int = 8            # so viele Wiederholungen bis zur vollen Geschwindigkeit
    stick_on: float = 0.60         # Stick zählt ab dieser Auslenkung als gedrückt …
    stick_off: float = 0.40        # … und erst darunter wieder als losgelassen (kein Flattern)

    _dir: tuple[int, int] = (0, 0)
    _next: float = 0.0
    _count: int = 0
    _stick: tuple[int, int] = (0, 0)

    def _stick_dir(self, sx: float, sy: float) -> tuple[int, int]:
        def axis(v: float, old: int) -> int:
            thr = self.stick_off if old else self.stick_on
            return (1 if v > 0 else -1) if abs(v) >= thr else 0
        # nur die stärkere Achse: schräge Auslenkung soll nicht zwei Schritte auslösen
        if abs(sx) >= abs(sy):
            return axis(sx, self._stick[0]), 0
        return 0, axis(sy, self._stick[1])

    @property
    def active(self) -> bool:
        """Eine Richtung wird gehalten: Die Wiederholung braucht den Takt."""
        return self._dir != (0, 0)

    def reset(self) -> None:
        self._dir, self._stick, self._count = (0, 0), (0, 0), 0

    def poll(self, hat: tuple[int, int], stick: tuple[float, float], now: float) -> list[tuple[int, int, bool]]:
        """Liefert die jetzt fälligen Schritte als (dx, dy, wiederholung), oben = (0, -1)."""
        self._stick = self._stick_dir(*stick)
        want = hat if hat != (0, 0) else self._stick
        if want[0] and want[1]:
            want = (want[0], 0)            # Diagonale am Steuerkreuz: waagrecht gewinnt
        if want == (0, 0):
            self._dir = (0, 0)
            return []
        if want != self._dir:
            self._dir, self._count, self._next = want, 0, now + self.delay
            return [(want[0], want[1], False)]
        if now < self._next:
            return []
        self._count += 1
        f = min(1.0, self._count / self.fast_after)
        step = self.interval + (self.fast_interval - self.interval) * f
        self._next += step
        if now >= self._next:
            # Hänger der Hauptschleife (E99): verpasste Fristen nicht nachholen, das wären Dutzende
            # Schritte auf einmal und der Fokus spränge 20–30 Einträge. Ein Schritt, Takt ab jetzt.
            self._next = now + step
        return [(want[0], want[1], True)]

    def update(self, hat: tuple[int, int], stick: tuple[float, float], now: float) -> list[tuple[int, int]]:
        """Wie poll(), ohne die Kennzeichnung der Wiederholung."""
        return [(dx, dy) for dx, dy, _ in self.poll(hat, stick, now)]


# --- PS-Taste --------------------------------------------------------------------------
PS_SHORT, PS_LONG = "short", "long"
PS_OPTIONS, PS_OPTIONS_DONE, PS_OPTIONS_CANCEL = "options", "options-done", "options-cancel"
# Bildschirmfoto (2026-10-21): PS gehalten + Create, bzw. Create allein (je nach Einstellung). Beide
# kommen in jeder Route; im Spiel sieht das Spiel die Create-Taste trotzdem (kein Greifen)
PS_CREATE, CREATE_SOLO = "create", "create-solo"


@dataclass
class PsButton:
    """Kurz = Control Center, lang = Home, PS + Options gehalten = App beenden.

    Kurz wird erst beim Loslassen gemeldet, sonst käme vor jedem langen Druck ein kurzer.
    Lang wird beim Erreichen der Schwelle gemeldet (nicht erst beim Loslassen), damit die
    Oberfläche sofort reagiert. Die Kombination braucht bewusst 3 s und zwei weit
    auseinanderliegende Tasten, weil sie ungespeicherte Spielstände kostet.

    PS + Create + Options ist der Notgriff des Supervisors: Sobald alle drei gedrückt sind,
    meldet dieser Druck nichts mehr (eine schon gezeigte Fortschrittsanzeige wird abgesagt).
    """
    long_after: float = 0.80
    quit_after: float = 3.0
    # Die Kombination meldet sich erst nach dieser Frist: Beim Notgriff landen drei Finger nie
    # im selben Augenblick, und Options kommt oft vor Create an.
    grace: float = 0.12

    _down_at: float | None = None
    _long_sent: bool = False
    _spoiled: bool = False          # PS war Teil einer Kombination: weder "kurz" noch "lang"
    _dead: bool = False             # Notgriff oder Aufwecken: bis zum Loslassen gar nichts
    _chord_at: float | None = None
    _chord_shown: bool = False
    _chord_done: bool = False
    _create_at: float | None = None # PS + Create: seit wann (das Foto kommt nach grace oder beim Loslassen)
    _create_sent: bool = False      # PS + Create hat in diesem Druck schon ein Bildschirmfoto gemeldet

    def prime(self, pressed: set[int], now: float) -> None:
        """Beim Öffnen eines Pads: eine schon gehaltene PS-Taste (Aufwecken!) löst nichts aus."""
        if c.PS in pressed:
            self._down_at, self._dead = now, True

    @property
    def pending(self) -> bool:
        """PS wird gehalten und es kann noch etwas fällig werden: Der Takt muss laufen."""
        if self._down_at is None or self._dead:
            return False
        if self._create_at is not None and not self._create_sent:
            return True
        if self._chord_at is not None:
            return not self._chord_done
        return not self._spoiled and not self._long_sent

    def update(self, pressed: set[int], now: float) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        if c.PS not in pressed:
            if self._down_at is not None:
                if self._chord_shown and not self._chord_done:
                    out.append((PS_OPTIONS_CANCEL, 0.0))
                elif self._create_at is not None and not self._create_sent and not self._dead:
                    out.append((PS_CREATE, 0.0))            # PS und Create zugleich losgelassen
                elif not self._spoiled and not self._long_sent and not self._dead:
                    out.append((PS_SHORT, 0.0))
                self._down_at = self._chord_at = self._create_at = None
                self._long_sent = self._spoiled = self._dead = False
                self._chord_shown = self._chord_done = self._create_sent = False
            return out

        if self._down_at is None:
            self._down_at = now
        if self._dead:
            return out
        options = c.OPTIONS in pressed
        if options and c.CREATE in pressed:
            if self._chord_shown and not self._chord_done:
                out.append((PS_OPTIONS_CANCEL, 0.0))
            self._dead, self._chord_at, self._chord_shown = True, None, False
            return out
        # PS + Create: Bildschirmfoto, und dieser PS-Druck öffnet danach kein Control Center. Erst
        # nach grace (kommt Options dazu, ist es der Notgriff) oder sobald Create losgelassen ist
        if c.CREATE in pressed:
            if self._create_at is None:
                self._create_at, self._spoiled = now, True
            elif not self._create_sent and now - self._create_at >= self.grace:
                self._create_sent = True
                out.append((PS_CREATE, 0.0))
        elif self._create_at is not None and not self._create_sent:
            self._create_sent = True
            out.append((PS_CREATE, 0.0))

        if options and not self._long_sent and not self._chord_done:
            if self._chord_at is None:
                self._chord_at, self._spoiled = now, True
            held = now - self._chord_at
            if held >= self.quit_after:
                self._chord_done = True
                out.append((PS_OPTIONS_DONE, 1.0))
            elif held >= self.grace:
                self._chord_shown = True
                out.append((PS_OPTIONS, held / self.quit_after))
        elif self._chord_at is not None and not self._chord_done:
            # Options losgelassen, PS noch gehalten: abgebrochen
            if self._chord_shown:
                out.append((PS_OPTIONS_CANCEL, 0.0))
            self._chord_at, self._chord_shown = None, False

        if not self._spoiled and not self._long_sent and now - self._down_at >= self.long_after:
            self._long_sent = True
            out.append((PS_LONG, 0.0))
        return out


# --- Zustand eines Pads ----------------------------------------------------------------
def _sign(v: int) -> int:
    return (v > 0) - (v < 0)


@dataclass
class PadState:
    """Letzter bekannter Stand eines Gamepad-Geräts, gefüttert mit rohen evdev-Ereignissen.

    Achsen werden mit ihrem gemeldeten Bereich normiert: Der DualSense liefert 0..255, ein
    Xbox-Pad -32768..32767, und beide sollen gleich reagieren.
    """
    buttons: set[int] = field(default_factory=set)
    axes: dict[int, int] = field(default_factory=lambda: {
        c.LEFT_X: c.STICK_CENTER, c.LEFT_Y: c.STICK_CENTER,
        c.RIGHT_X: c.STICK_CENTER, c.RIGHT_Y: c.STICK_CENTER,
        c.L2_ANALOG: 0, c.R2_ANALOG: 0, c.DPAD_X: 0, c.DPAD_Y: 0,
    })
    ranges: dict[int, tuple[int, int]] = field(default_factory=lambda: dict(c.DUALSENSE_RANGES))
    right_axes: tuple[int, int] | None = (c.RIGHT_X, c.RIGHT_Y)
    trigger_axes: tuple[int, int] | None = (c.L2_ANALOG, c.R2_ANALOG)     # (L2, R2)

    def configure(self, ranges: dict[int, tuple[int, int]]) -> None:
        """Achsen des geöffneten Geräts übernehmen: Bereiche und welche Achse was ist."""
        if not ranges:
            return
        self.ranges = {code: (lo, hi) for code, (lo, hi) in ranges.items() if hi > lo}
        have = set(self.ranges)
        if {c.ABS_RX, c.ABS_RY} <= have:
            self.right_axes = (c.ABS_RX, c.ABS_RY)
            self.trigger_axes = (c.ABS_Z, c.ABS_RZ) if {c.ABS_Z, c.ABS_RZ} <= have else None
        elif {c.ABS_Z, c.ABS_RZ} <= have:
            # Einfache HID-Pads legen den rechten Stick auf Z/RZ und haben keine analogen
            # Auslöser. Als Auslöser gelesen stünde er in Ruhe auf "halb gedrückt".
            self.right_axes, self.trigger_axes = (c.ABS_Z, c.ABS_RZ), None
        else:
            self.right_axes = self.trigger_axes = None
        sticks = {c.LEFT_X, c.LEFT_Y, *(self.right_axes or ())}
        self.axes = {code: self._center(code) if code in sticks else (0 if lo <= 0 <= hi else lo)
                     for code, (lo, hi) in self.ranges.items()}

    def _center(self, code: int) -> int:
        lo, hi = self.ranges[code]
        return (lo + hi + 1) // 2

    def feed(self, etype: int, code: int, value: int) -> tuple[str, int, int] | None:
        """Verarbeitet ein Ereignis; liefert ("button", code, 0/1) bei Tastenwechsel."""
        if etype == c.EV_KEY:
            if value == 2:                      # Autorepeat des Kernels: uninteressant
                return None
            was = code in self.buttons
            (self.buttons.add if value else self.buttons.discard)(code)
            if was != bool(value):
                return ("button", code, 1 if value else 0)
        elif etype == c.EV_ABS:
            self.axes[code] = value
        return None

    def resync(self, active_keys: set[int], abs_values: dict[int, int]) -> list[tuple[str, int, int]]:
        """Nach SYN_DROPPED oder beim Öffnen: Stand neu setzen, Unterschiede als Ereignisse."""
        changes = [("button", k, 0) for k in sorted(self.buttons - active_keys)]
        changes += [("button", k, 1) for k in sorted(active_keys - self.buttons)]
        self.buttons = set(active_keys)
        self.axes.update(abs_values)
        return changes

    def _norm(self, code: int) -> float:
        rng = self.ranges.get(code)
        if rng is None:
            return 0.0
        center = (rng[0] + rng[1] + 1) // 2
        return (self.axes.get(code, center) - center) / ((rng[1] - center) or 1)

    @property
    def hat(self) -> tuple[int, int]:
        x, y = _sign(self.axes.get(c.DPAD_X, 0)), _sign(self.axes.get(c.DPAD_Y, 0))
        b = self.buttons
        if not x:
            x = (c.BTN_DPAD_RIGHT in b) - (c.BTN_DPAD_LEFT in b)
        if not y:
            y = (c.BTN_DPAD_DOWN in b) - (c.BTN_DPAD_UP in b)
        return x, y

    def left(self, inner: float = 0.18) -> tuple[float, float]:
        return radial_deadzone(self._norm(c.LEFT_X), self._norm(c.LEFT_Y), inner)

    def right(self, inner: float = 0.18) -> tuple[float, float]:
        if self.right_axes is None:
            return 0.0, 0.0
        return radial_deadzone(self._norm(self.right_axes[0]), self._norm(self.right_axes[1]), inner)

    def trigger(self, code: int) -> float:
        """Analoger Auslöser 0..1; 0, wenn das Pad keine analogen Auslöser hat."""
        if self.trigger_axes is None or code not in self.trigger_axes:
            return 0.0
        lo, hi = self.ranges.get(code, (0, c.TRIGGER_MAX))
        return max(0.0, min(1.0, (self.axes.get(code, lo) - lo) / (hi - lo)))

    def is_neutral(self) -> bool:
        """Nichts gedrückt, nichts ausgelenkt. Erst dann darf ein angehaltenes Spiel weiterlaufen."""
        return (not self.buttons and self.hat == (0, 0) and self.left() == (0.0, 0.0)
                and self.right() == (0.0, 0.0)
                and self.trigger(c.L2_ANALOG) < 0.1 and self.trigger(c.R2_ANALOG) < 0.1)


# --- Tasten für die Oberfläche ---------------------------------------------------------
DIRECTIONS = {(0, -1): "up", (0, 1): "down", (-1, 0): "left", (1, 0): "right"}
TRIGGER_PRESS, TRIGGER_RELEASE = 0.55, 0.35     # L2/R2 als Taste; Abstand gegen Flattern


class PadLogic:
    """Was die Oberfläche von EINEM Pad sieht: Tasten, Richtungen mit Wiederholung, PS-Gesten.

    update() läuft nach jedem Bericht des Pads und im Takt und liefert
        ("press", name, wiederholung)   name aus cross circle square triangle l1 r1 l2 r2 l3 r3
                                        options create touchpad up down left right rs-up rs-down
                                        rs-left rs-right
        ("ps", art, fortschritt)        art aus short long options options-done options-cancel
                                        create (PS + Create) create-solo (Create ohne PS)
    Gemeldet wird nur der Druck. Solange PS gehalten wird, gehört das Pad der Systemebene.
    """

    def __init__(self, state: PadState, positional: bool = True, ps_long: float = 0.8, ps_quit: float = 3.0):
        self.state = state
        self.positional = positional
        self.ps = PsButton(long_after=ps_long, quit_after=ps_quit)
        self.nav = NavRepeater()
        # rechter Stick rollt Listen und lange Texte: etwas zügiger als die Fokus-Schritte
        self.rs = NavRepeater(delay=0.28, interval=0.10, fast_interval=0.045)
        self.enabled = True
        self._seen: set[str] = set()
        self._blocked: set[str] = set()
        self._nav_blocked = self._rs_blocked = False
        self._trig = {"l2": False, "r2": False}
        self._create_down = False

    def prime(self, now: float) -> None:
        """Nach dem Öffnen: Was schon gehalten wird, zählt nicht als Druck."""
        self.ps.prime(self.state.buttons, now)
        self._create_down = c.CREATE in self.state.buttons
        self.hold_off()

    def set_enabled(self, on: bool) -> None:
        """Aus = die Oberfläche bekommt keine Tasten (ein Spiel ist vorn); PS läuft weiter."""
        self.enabled = on
        self.hold_off()

    @property
    def needs_tick(self) -> bool:
        return self.ps.pending or (self.enabled and (self.nav.active or self.rs.active))

    def _down(self) -> set[str]:
        st = self.state
        names = set()
        for code in st.buttons:
            name = c.button_name(code, self.positional)
            # PS läuft über PsButton; die digitalen Auslöser kämen doppelt (digital und analog)
            if name is None or name == "ps" or (st.trigger_axes and name in ("l2", "r2")):
                continue
            names.add(name)
        if st.trigger_axes:
            for name, axis in zip(("l2", "r2"), st.trigger_axes):
                value = st.trigger(axis)
                down = value > TRIGGER_RELEASE if self._trig[name] else value >= TRIGGER_PRESS
                self._trig[name] = down
                if down:
                    names.add(name)
        return names

    @staticmethod
    def _held(hat: tuple[int, int], stick: tuple[float, float], rep: NavRepeater) -> bool:
        return hat != (0, 0) or max(abs(stick[0]), abs(stick[1])) >= rep.stick_off

    def hold_off(self) -> None:
        """Alles, was jetzt gedrückt oder ausgelenkt ist, gilt erst nach dem Loslassen wieder.

        Nach einem Wechsel des Ziels (Spiel -> Overlay, Overlay -> Home) darf die Taste, die den
        Wechsel ausgelöst hat oder nebenbei gehalten wird, im neuen Ziel nichts auslösen.
        """
        st = self.state
        self._seen = self._down()
        self._blocked = set(self._seen)
        self._nav_blocked = self._held(st.hat, st.left(), self.nav)
        self._rs_blocked = self._held((0, 0), st.right(), self.rs)
        self.nav.reset()
        self.rs.reset()

    def _steps(self, rep: NavRepeater, hat, stick, blocked: bool, prefix: str, now: float, out: list) -> bool:
        if blocked:
            if self._held(hat, stick, rep):
                return True
            rep.reset()
        for dx, dy, repeat in rep.poll(hat, stick, now):
            out.append(("press", prefix + DIRECTIONS[(dx, dy)], repeat))
        return False

    def update(self, now: float) -> list[tuple[str, str, float]]:
        st = self.state
        out: list = [("ps", kind, value) for kind, value in self.ps.update(st.buttons, now)]
        # Create ohne PS, auch wenn die Tasten gerade nicht der Oberfläche gehören (Spiel vorn)
        create = c.CREATE in st.buttons
        if create and not self._create_down and c.PS not in st.buttons:
            out.append(("ps", CREATE_SOLO, 0.0))
        self._create_down = create
        if c.PS in st.buttons:
            self.hold_off()
            return out
        if not self.enabled:
            return out
        down = self._down()
        self._blocked &= down
        for name in sorted(down - self._seen - self._blocked):
            out.append(("press", name, False))
        self._seen = down
        self._nav_blocked = self._steps(self.nav, st.hat, st.left(), self._nav_blocked, "", now, out)
        self._rs_blocked = self._steps(self.rs, (0, 0), st.right(), self._rs_blocked, "rs-", now, out)
        return out
