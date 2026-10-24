"""Gamepads unter Linux: Codes, Wertebereiche und Geräte-Erkennung (Vorlage: DualSense).

Quelle der Zuordnung: drivers/hid/hid-playstation.c (ps_gamepad_buttons, ps_gamepad_create,
dualsense_parse_report). Die Zahlen sind bewusst ausgeschrieben, damit das Modul ohne
python-evdev importierbar bleibt (Tests, Werkzeuge).
"""
from __future__ import annotations

# --- Ereignistypen ---------------------------------------------------------------------
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW, EV_FF = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x15
SYN_REPORT, SYN_DROPPED = 0, 3

# --- Tasten des Gamepad-Geräts ---------------------------------------------------------
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR = 0x13A, 0x13B, 0x13C, 0x13D, 0x13E
BTN_TRIGGER_HAPPY1 = 0x2C0  # nur DualSense Edge: Fn1, Fn2, Paddle links, Paddle rechts (…HAPPY4)
# Manche Pads melden das Steuerkreuz als Tasten statt als Achse
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223

# Achtung: Der Kernel benennt nach Lage, nicht nach Symbol. BTN_NORTH ist Dreieck,
# BTN_WEST ist Quadrat (bei Xbox-Pads ist es umgekehrt, siehe button_name).
CROSS, CIRCLE, TRIANGLE, SQUARE = BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST
L1, R1, L2, R2 = BTN_TL, BTN_TR, BTN_TL2, BTN_TR2
CREATE, OPTIONS, PS, L3, R3 = BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR

# --- Touchpad-Gerät ("… Touchpad") -----------------------------------------------------
BTN_LEFT = 0x110                      # Klick aufs Touchpad
TOUCHPAD_CLICK = BTN_LEFT             # kommt vom Touchpad-Knoten, nie vom Gamepad-Knoten
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
TOUCHPAD_W, TOUCHPAD_H = 1920, 1080   # Achsen 0..1919 / 0..1079, zwei Finger
INPUT_PROP_POINTER, INPUT_PROP_BUTTONPAD, INPUT_PROP_ACCELEROMETER = 0x00, 0x02, 0x06

# Namen, wie sie die Oberfläche bekommt (Lage wie beim DualSense)
BUTTON_NAMES = {
    CROSS: "cross", CIRCLE: "circle", TRIANGLE: "triangle", SQUARE: "square",
    L1: "l1", R1: "r1", L2: "l2", R2: "r2",
    CREATE: "create", OPTIONS: "options", PS: "ps", L3: "l3", R3: "r3",
    TOUCHPAD_CLICK: "touchpad",
}
BUTTON_CODES = {name: code for code, name in BUTTON_NAMES.items()}

# --- Achsen des Gamepad-Geräts ---------------------------------------------------------
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
LEFT_X, LEFT_Y, RIGHT_X, RIGHT_Y = ABS_X, ABS_Y, ABS_RX, ABS_RY   # DualSense: 0..255, Ruhe 128, oben/links = 0
L2_ANALOG, R2_ANALOG = ABS_Z, ABS_RZ                                # DualSense: 0..255, Ruhe 0
DPAD_X, DPAD_Y = ABS_HAT0X, ABS_HAT0Y                               # -1/0/1, oben = -1, links = -1

STICK_MIN, STICK_MAX, STICK_CENTER = 0, 255, 128
TRIGGER_MAX = 255
# Bereiche des DualSense; andere Pads melden ihre eigenen (PadDevice.absinfo)
DUALSENSE_RANGES = {
    LEFT_X: (0, 255), LEFT_Y: (0, 255), RIGHT_X: (0, 255), RIGHT_Y: (0, 255),
    L2_ANALOG: (0, 255), R2_ANALOG: (0, 255), DPAD_X: (-1, 1), DPAD_Y: (-1, 1),
}

# --- Kennungen -------------------------------------------------------------------------
VENDOR_SONY, VENDOR_NINTENDO, VENDOR_MICROSOFT, VENDOR_VALVE = 0x054C, 0x057E, 0x045E, 0x28DE
PRODUCT_DUALSENSE, PRODUCT_DUALSENSE_EDGE = 0x0CE6, 0x0DF2
PRODUCT_STEAM_VIRTUAL_PAD = 0x11FF      # Steams "X-Box 360 pad" aus Steam Input
PRODUCT_STEAM_DECK = 0x1205             # auch InputPlumbers "deck-uhid"
BUS_USB, BUS_BLUETOOTH = 0x03, 0x05

GAMEPAD, TOUCHPAD, MOTION, JACK = "gamepad", "touchpad", "motion", "jack"


def classify(name: str, keys: set[int], abs_axes: set[int], props: set[int]) -> str | None:
    """Ordnet ein Eingabegerät einer der vier Rollen eines Pads zu (oder None).

    Entscheidet nach Fähigkeiten statt nach Namen: per USB heißt das Pad
    "Sony Interactive Entertainment DualSense Wireless Controller", per Bluetooth nur
    "DualSense Wireless Controller", und fremde Pads sollen ebenfalls funktionieren.
    """
    if INPUT_PROP_ACCELEROMETER in props:
        return MOTION                      # hat ABS_X..ABS_RZ wie ein Gamepad, aber keine Tasten
    if ABS_MT_POSITION_X in abs_axes and BTN_LEFT in keys:
        return TOUCHPAD                    # gehört libinput; als Gamepad fassen wir es nie an
    if BTN_SOUTH in keys and (ABS_X in abs_axes or ABS_HAT0X in abs_axes):
        return GAMEPAD
    if name.endswith("Headset Jack"):
        return JACK
    return None


def is_dualsense(vendor: int, product: int) -> bool:
    return vendor == VENDOR_SONY and product in (PRODUCT_DUALSENSE, PRODUCT_DUALSENSE_EDGE)


def is_excluded(vendor: int, product: int) -> bool:
    """Nur Steams virtuelles Pad bleibt draußen (es spiegelt ein echtes: sonst doppelte Eingabe).

    Nicht der ganze Hersteller 28de: InputPlumber kann das echte Pad hinter einem Gerät
    28de:1205 verstecken, und das muss dann bedienbar bleiben (Befund 8).
    """
    return (vendor, product) == (VENDOR_VALVE, PRODUCT_STEAM_VIRTUAL_PAD)


def is_positional(vendor: int) -> bool:
    """Meldet der Treiber BTN_NORTH/BTN_WEST nach Lage? Sony und Nintendo ja, xpad und hid-steam nein."""
    return vendor in (VENDOR_SONY, VENDOR_NINTENDO)


def button_name(code: int, positional: bool = True) -> str | None:
    """Name für die Oberfläche. Maßgeblich ist die Lage der Taste, nicht ihre Beschriftung.

    Der xpad-Treiber meldet X (liegt links) als BTN_NORTH und Y (liegt oben) als BTN_WEST. Für
    solche Pads wird getauscht, damit "oben" überall Dreieck und "links" überall Quadrat ist.
    """
    if not positional:
        if code == BTN_NORTH:
            return "square"
        if code == BTN_WEST:
            return "triangle"
    return BUTTON_NAMES.get(code)


def pad_type(vendor: int, product: int) -> str:
    """Grobe Art des Pads für die Anzeige (Symbol, Tastenbilder)."""
    if vendor == VENDOR_SONY:
        if product == PRODUCT_DUALSENSE_EDGE:
            return "dualsense-edge"
        return "dualsense" if product == PRODUCT_DUALSENSE else "playstation"
    if vendor == VENDOR_NINTENDO:
        return "switch"
    if vendor == VENDOR_MICROSOFT:
        return "xbox"
    if vendor == VENDOR_VALVE:
        return "steamdeck"
    return "gamepad"
