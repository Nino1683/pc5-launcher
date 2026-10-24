"""Pad als Konsolen-Zubehör: Vibration, Lichtleiste, Spieler-LEDs, Akku. Alles ohne Root.

Wege, vom schonendsten zum direktesten:
  1. evdev Force-Feedback auf dem Gamepad-Knoten      -> Vibration (rumble)
  2. sysfs /sys/class/leds/inputN:rgb:indicator       -> Lichtleiste
     sysfs /sys/class/leds/inputN:white:player-K      -> Spieler-LEDs (nur wenn schreibbar)
     sysfs <HID-Gerät>/power_supply/*                 -> Akku
  3. hidraw-Berichte (nur Auf- und Abbau, hier wird kein hidraw geöffnet): build_output_report,
     parse_input_report. Layout: drivers/hid/hid-playstation.c (struct dualsense_output_report_*).

Alles, was das Pad anfasst, scheitert still: Die Aufrufer bekommen False/None, nie eine Ausnahme.
UNBESTÄTIGT: Die sysfs-Namen, Rechte und die Vibration stammen aus dem Treiber-Quelltext; am
wachen Pad noch nicht gemessen (tools/probe-pad).
"""
from __future__ import annotations

import glob
import os
import zlib
from dataclasses import dataclass

# --- Ausgabebericht --------------------------------------------------------------------
REPORT_USB, REPORT_BT = 0x02, 0x31
SIZE_USB, SIZE_BT = 63, 78
BT_TAG = 0x10
CRC_SEED_OUTPUT, CRC_SEED_INPUT = 0xA2, 0xA1

FLAG0_COMPATIBLE_VIBRATION, FLAG0_HAPTICS_SELECT = 0x01, 0x02
FLAG1_MIC_MUTE_LED, FLAG1_LIGHTBAR, FLAG1_PLAYER_LEDS = 0x01, 0x04, 0x10
FLAG2_COMPATIBLE_VIBRATION2 = 0x04

# Offsets im gemeinsamen Teil (47 Bytes)
O_FLAG0, O_FLAG1, O_MOTOR_RIGHT, O_MOTOR_LEFT, O_MUTE_LED = 0, 1, 2, 3, 8
O_FLAG2, O_PLAYER_LEDS, O_RED, O_GREEN, O_BLUE = 38, 43, 44, 45, 46
COMMON_SIZE = 47

# Spieler 1..5 wie auf der Konsole mittig angeordnet (dualsense_set_player_leds)
PLAYER_PATTERNS = {1: 0b00100, 2: 0b01010, 3: 0b10101, 4: 0b11011, 5: 0b11111}


def ps_crc32(seed: int, data: bytes) -> int:
    """CRC der Bluetooth-Berichte: normales CRC-32 über ein Seed-Byte plus die Daten."""
    return zlib.crc32(bytes([seed]) + data) & 0xFFFFFFFF


def build_output_report(bluetooth: bool, seq: int = 0, *,
                        lightbar: tuple[int, int, int] | None = None,
                        player_leds: int | None = None,
                        mic_led: bool | None = None,
                        rumble: tuple[int, int] | None = None,
                        vibration_v2: bool = True) -> bytes:
    """Baut einen Ausgabebericht. rumble = (stark/links, schwach/rechts), je 0..255."""
    common = bytearray(COMMON_SIZE)
    if lightbar is not None:
        common[O_FLAG1] |= FLAG1_LIGHTBAR
        common[O_RED], common[O_GREEN], common[O_BLUE] = (max(0, min(255, v)) for v in lightbar)
    if player_leds is not None:
        common[O_FLAG1] |= FLAG1_PLAYER_LEDS
        common[O_PLAYER_LEDS] = player_leds & 0x1F
    if mic_led is not None:
        common[O_FLAG1] |= FLAG1_MIC_MUTE_LED
        common[O_MUTE_LED] = 1 if mic_led else 0
    if rumble is not None:
        common[O_FLAG0] |= FLAG0_HAPTICS_SELECT
        if vibration_v2:                       # Firmware ab 2.21 (alle aktuellen Pads)
            common[O_FLAG2] |= FLAG2_COMPATIBLE_VIBRATION2
        else:
            common[O_FLAG0] |= FLAG0_COMPATIBLE_VIBRATION
        common[O_MOTOR_LEFT], common[O_MOTOR_RIGHT] = (max(0, min(255, v)) for v in rumble)

    if not bluetooth:
        return bytes([REPORT_USB]) + bytes(common) + bytes(SIZE_USB - 1 - COMMON_SIZE)
    # Bluetooth: Folgenummer im oberen Halbbyte, festes Tag, am Ende CRC über alles davor
    body = bytes([REPORT_BT, (seq & 0x0F) << 4, BT_TAG]) + bytes(common) + bytes(SIZE_BT - 3 - COMMON_SIZE - 4)
    return body + ps_crc32(CRC_SEED_OUTPUT, body).to_bytes(4, "little")


# --- Eingabebericht (nur was wir brauchen) ---------------------------------------------
@dataclass
class RawState:
    ps: bool
    touchpad_click: bool
    mute: bool
    battery_percent: int
    charging: bool


def parse_input_report(data: bytes) -> RawState | None:
    """Liest PS-, Touchpad- und Stumm-Taste sowie den Akku direkt aus einem hidraw-Bericht.

    Die Stumm-Taste gibt es NUR hier: Der Kernel schaltet mit ihr selbst das Mikrofon um und
    reicht sie nicht als evdev-Taste weiter.
    """
    if len(data) == 64 and data[0] == 0x01:            # USB
        base = 1
    elif len(data) == 78 and data[0] == 0x31:          # Bluetooth, voller Bericht
        if ps_crc32(CRC_SEED_INPUT, data[:74]) != int.from_bytes(data[74:78], "little"):
            return None                                 # Funkstörung: Bericht verwerfen wie der Kernel
        base = 2
    else:
        return None
    buttons2 = data[base + 9]                           # x,y,rx,ry,z,rz,seq, buttons[0..3]
    status0 = data[base + 52]
    level, charge = status0 & 0x0F, status0 >> 4
    return RawState(ps=bool(buttons2 & 0x01), touchpad_click=bool(buttons2 & 0x02), mute=bool(buttons2 & 0x04),
                    battery_percent=100 if charge == 2 else min(level * 10 + 5, 100), charging=charge == 1)


# --- sysfs -----------------------------------------------------------------------------
@dataclass
class PadSysfs:
    """Pfade eines Pads, abgeleitet aus seinem Gamepad-Knoten (/dev/input/eventN).

    Nie zwischenspeichern: inputN und damit die Namen der LEDs ändern sich bei jeder
    Neuverbindung.
    """
    input_name: str            # "input57"
    hid_dir: str               # …/0005:054C:0CE6.000C
    sys_root: str = "/sys"

    @classmethod
    def from_event_node(cls, event_path: str, sys_root: str = "/sys") -> "PadSysfs | None":
        dev = os.path.realpath(os.path.join(sys_root, "class/input", os.path.basename(event_path), "device"))
        hid = os.path.realpath(os.path.join(dev, "device"))
        if not os.path.isdir(dev):
            return None
        return cls(input_name=os.path.basename(dev), hid_dir=hid, sys_root=sys_root)

    @property
    def lightbar_dir(self) -> str:
        return os.path.join(self.sys_root, "class/leds", f"{self.input_name}:rgb:indicator")

    def player_led(self, n: int) -> str:
        return os.path.join(self.sys_root, "class/leds", f"{self.input_name}:white:player-{n}", "brightness")

    def hidraw_node(self) -> str | None:
        nodes = glob.glob(os.path.join(self.hid_dir, "hidraw", "hidraw*"))
        return "/dev/" + os.path.basename(nodes[0]) if nodes else None

    def battery_dir(self) -> str | None:
        # DualSense: ps-controller-battery-<mac>; andere Treiber nennen ihren Akku anders
        dirs = sorted(glob.glob(os.path.join(self.hid_dir, "power_supply", "*")))
        named = [d for d in dirs if os.path.basename(d).startswith("ps-controller-battery-")]
        return (named or dirs or [None])[0]

    def is_bluetooth(self) -> bool:
        return os.path.basename(self.hid_dir).startswith("0005:")     # 0003 = USB, 0005 = Bluetooth


def set_lightbar(pad: PadSysfs, r: int, g: int, b: int) -> bool:
    """Farbe über die Multicolor-LED. True, wenn geschrieben."""
    try:
        # Reihenfolge laut multi_index: red green blue. Das Schreiben von multi_intensity wendet
        # die Farbe mit der aktuellen Helligkeit sofort an; der Treiber startet mit 255.
        # brightness nur anfassen, wenn jemand sie heruntergedreht hat (0 = Lichtleiste aus).
        with open(os.path.join(pad.lightbar_dir, "multi_intensity"), "w") as f:
            f.write(f"{max(0, min(255, r))} {max(0, min(255, g))} {max(0, min(255, b))}")
        with open(os.path.join(pad.lightbar_dir, "brightness")) as f:
            dimmed = f.read().strip() != "255"
        if dimmed:
            with open(os.path.join(pad.lightbar_dir, "brightness"), "w") as f:
                f.write("255")
        return True
    except OSError:
        return False


def set_player_leds(pad: PadSysfs, player: int) -> bool:
    """Spieler-LEDs über sysfs. Ohne eigene udev-Regel darf das nur root: dann False."""
    pattern = PLAYER_PATTERNS.get(player, 0)
    try:
        for n in range(1, 6):
            with open(pad.player_led(n), "w") as f:
                f.write("1" if pattern & (1 << (n - 1)) else "0")
        return True
    except OSError:
        return False


def read_battery(pad: PadSysfs) -> tuple[int, str] | None:
    """(Prozent, Status). Prozent kommt in Zehnerstufen (5, 15 … 95, 100); Status wie im Kernel."""
    d = pad.battery_dir()
    if not d:
        return None
    try:
        with open(os.path.join(d, "capacity")) as f:
            cap = int(f.read())
        with open(os.path.join(d, "status")) as f:
            return cap, f.read().strip()        # Charging, Discharging, Full, Not charging, Unknown
    except (OSError, ValueError):
        return None


def parse_color(text: str) -> tuple[int, int, int] | None:
    """"#rrggbb" -> (r, g, b); alles andere None (= Lichtleiste nicht anfassen)."""
    text = (text or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


# --- Vibration über evdev --------------------------------------------------------------
class Rumble:
    """Kurze Vibrationsstöße als Rückmeldung der Oberfläche.

    Braucht Schreibrecht auf dem Gamepad-Knoten (hat der angemeldete Benutzer über die uaccess-ACL).
    `device` ist ein evdev.InputDevice; die Effekte liegen im Kernel und müssen wieder
    gelöscht werden, sonst sind die 16 Plätze irgendwann voll.
    """

    def __init__(self, device):
        from evdev import ecodes, ff
        self._dev, self._e, self._ff = device, ecodes, ff
        self._effect: int | None = None

    def pulse(self, strong: float, weak: float, ms: int) -> None:
        """strong = linker, schwerer Motor; weak = rechter, leichter. Werte 0..1."""
        e, ff = self._e, self._ff
        self.stop()
        rumble = ff.Rumble(strong_magnitude=int(max(0.0, min(1.0, strong)) * 0xFFFF),
                           weak_magnitude=int(max(0.0, min(1.0, weak)) * 0xFFFF))
        effect = ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(ms, 0), ff.EffectType(ff_rumble_effect=rumble))
        try:
            self._effect = self._dev.upload_effect(effect)
            self._dev.write(e.EV_FF, self._effect, 1)       # 1 = einmal abspielen; Dauer steht im Effekt
        except OSError:
            self._effect = None                             # Pad weg oder nur lesend geöffnet

    def silence(self) -> None:
        """Motoren aus, auch wenn ein anderes Programm sie angestellt hat.

        Ein angehaltenes Spiel kann seine Vibration nicht mehr abstellen. Ein Effekt mit Stärke
        null lässt den Treiber einen Bericht mit ruhenden Motoren senden.
        UNBESTÄTIGT (Annahme aus dem Treiber-Quelltext; am Pad prüfen).
        """
        self.pulse(0.0, 0.0, 20)

    def stop(self) -> None:
        if self._effect is not None:
            try:
                self._dev.erase_effect(self._effect)
            except OSError:
                pass
            self._effect = None


# Fertige Muster für die Oberfläche: (stark, schwach, Millisekunden)
FEEDBACK = {
    "tick": (0.0, 0.25, 18),        # Fokus wandert
    "confirm": (0.0, 0.5, 35),      # Auswahl
    "boundary": (0.45, 0.0, 60),    # Ende einer Liste
    "error": (0.8, 0.4, 160),       # geht nicht
    "home": (0.3, 0.3, 45),         # PS-Taste angenommen
}
