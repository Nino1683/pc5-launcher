"""Fernseher steuern (E142, Wunsch 2026-10-30: „Ich wollte die Lautstärke regeln, ferner aus- und Eingang wechseln“ – „Und es
muss bei anderen funktionieren es hat ja nicht jeder mein setup“).

Nur für Fernseher mit Google TV bzw. Android TV, nur über das Heimnetz (am PC gibt es kein HDMI-CEC; der Fernseher
reicht Lautstärketasten selbst per CEC an einen Verstärker weiter):
  remote.py   Fernbedienungs-Protokoll v2 der Android-TV-Fernbedienung: Koppeln (TLS, Port 6467, Code am Fernseher)
              und Steuern (TLS, Port 6466) mit eigenem Client-Zertifikat
  adb.py      wahlfrei: ADB über das Netz (Port 5555), nur die feste Liste in adb.COMMANDS, um Eingänge, ihre Namen und
              den aktiven Eingang zu lesen; der Nutzer schaltet dafür am Fernseher „Netzwerk-Debugging“ ein und bestätigt dort
  inputs.py   dumpsys tv_input lesen (AOSP-Aufbau), Eingang des PCs aus der EDID (CEC-Adresse im HDMI-Herstellerblock)
  net.py      Suche (mDNS _androidtvremote2._tcp über avahi-browse), MAC aus der Nachbartabelle, Wake-on-LAN
  keys.py     eigene Schlüssel in <config>/tv (Ordner 0700, Dateien 0600); fehlt python3-cryptography, ein Hinweis
  store.py    der gekoppelte Fernseher (tv.json): Adresse, Name, MAC, gelernte Eingänge, Eingang des PCs
  service.py  alles zusammen, in Arbeits-Threads mit kurzen Zeitgrenzen; nie in der Hauptschleife

Vorgabe aus (tv.enabled). Vor dem Einschalten und Koppeln geht nichts ins Netz, auch keine Suche. Gesprochen wird nur
mit der Adresse des gekoppelten Fernsehers (dazu die Suche beim Koppeln und das Wake-on-LAN-Paket an die Broadcast-
Adressen). Alles, was der Rechner dieses Nutzers nicht kann, zeigt eine Zeile mit Grund statt eines Fehlers.

Dieses Modul: feste Werte und kleine Prüfungen, ohne Netz und ohne Dateien.
"""
from __future__ import annotations

import ipaddress
import re
import urllib.parse

TITLE = "Fernseher"

# Vorgaben der eigenen Schlüssel (E22; die Tabelle in config.py führt sie ebenso)
DEFAULTS = {
    "tv.enabled": False,            # Fernseher steuern (alles andere wirkt nur damit)
    "tv.off_with_pc": "never",      # beim Ruhemodus und Ausschalten: never | pc (nur wenn er den PC zeigt) | always
    "tv.wake_with_pc": False,       # beim Aufwachen und nach dem Einschalten des PCs: einschalten, auf den PC umschalten
}
OFF_CHOICES = ("never", "pc", "always")

REMOTE_PORT = 6466
PAIR_PORT = 6467
ADB_PORT = 5555
SERVICE = "_androidtvremote2._tcp"

# Android-Tastencodes (KeyEvent). Lautstärke geht über CEC an einen Verstärker weiter, wenn der Ton dort läuft.
KEYS = {
    "volume_up": 24,
    "volume_down": 25,
    "mute": 164,
    "power": 26,            # schaltet um: nur senden, wenn der Fernseher „an“ meldet
    "wakeup": 224,          # weckt nur, schaltet nie aus
    "input_menu": 178,      # KEYCODE_TV_INPUT: Auswahl der Eingänge am Fernseher (letzter Rückfall)
}
VOLUME_KEYS = ("volume_up", "volume_down", "mute")
# KEYCODE_TV_INPUT_HDMI_1..4: Manche Fernseher übergehen sie (gesehen), andere schalten damit um
HDMI_KEYS = {1: 243, 2: 244, 3: 245, 4: 246}
MAX_PORT = 8

PASSTHROUGH = "content://android.media.tv/passthrough/"
# Kennung eines Eingangs aus dumpsys tv_input, z. B. „com.vendor.tvinput/.PassThrough/HW7“ (Paket/Dienst/Gerät)
_INPUT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.]*/[A-Za-z0-9_.$]+(?:/[A-Za-z0-9_.$-]+)?")
_HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                       r"[A-Za-z0-9])?)*\.?")
_CODE = re.compile(r"[0-9A-F]{6}")

TEXT_OFF = "Fernseher steuern ist ausgeschaltet. Einschalten unter Einstellungen › Bildschirm und Video › Fernseher steuern."
TEXT_NOT_PAIRED = ("Der Fernseher ist nicht gekoppelt. Koppeln unter Einstellungen › Bildschirm und Video › Fernseher "
                   "steuern.")
TEXT_UNREACHABLE = ("Der Fernseher antwortet nicht. Ist er im Standby, muss in seinen Einstellungen „Netzwerk-Standby“ "
                    "(je nach Hersteller „Wake on LAN“ oder „Über Netzwerk einschalten“) an sein.")
TEXT_REJECTED = ("Der Fernseher kennt diesen PC nicht mehr (Kopplung aufgehoben oder Fernseher neu eingerichtet). Koppel "
                 "ihn neu unter Einstellungen › Bildschirm und Video › Fernseher steuern.")
TEXT_CODE_FORMAT = "Der Code hat 6 Zeichen: Ziffern 0 bis 9 und Buchstaben A bis F."
TEXT_CODE_TYPO = "Der Code passt nicht. Vertippt? Vergleiche ihn noch einmal mit dem Code am Fernseher."
TEXT_TEST_HOST = "Im Prüfmodus spricht der PC5 Launcher nur mit 127.0.0.1."


class TvError(Exception):
    """Fehler mit deutschem Satz für die Oberfläche und kurzer Kennung (Unreachable, NotPaired, Rejected, BadCode,
    BadRequest, NoAdb, AdbDenied, Unavailable, Busy, TvOff)."""

    def __init__(self, message: str, code: str = "TvError"):
        super().__init__(message)
        self.message = message
        self.code = code


def enabled(config) -> bool:
    try:
        return config.get("tv.enabled", False) is True
    except AttributeError:
        return False


def off_mode(config) -> str:
    """never | pc | always; ohne tv.enabled immer never."""
    if not enabled(config):
        return "never"
    value = config.get("tv.off_with_pc", DEFAULTS["tv.off_with_pc"])
    return value if value in OFF_CHOICES else DEFAULTS["tv.off_with_pc"]


def wake_wanted(config) -> bool:
    return enabled(config) and config.get("tv.wake_with_pc", DEFAULTS["tv.wake_with_pc"]) is True


def clean_host(value, test_mode: bool = False) -> str:
    """Eingegebene Adresse: IPv4 oder Rechnername (z. B. „fernseher.local“), ohne Schema, Port und Leerzeichen.
    ValueError bei allem anderen. Im Prüfmodus nur 127.0.0.1 (nie das echte Netz)."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 253:
        raise ValueError(text)
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.version != 4 or addr.is_multicast or addr.is_unspecified or str(addr).endswith(".255"):
            raise ValueError(text)
        text = str(addr)
    elif not _HOSTNAME.fullmatch(text) or text.replace(".", "").isdigit():
        raise ValueError(text)
    if test_mode and text != "127.0.0.1":
        raise ValueError(text)
    return text.rstrip(".")


def clean_code(value) -> str:
    """Code vom Fernseher: 6 Hex-Zeichen, Leerzeichen und Kleinschreibung erlaubt. ValueError sonst."""
    text = re.sub(r"\s+", "", value).upper() if isinstance(value, str) else ""
    if not _CODE.fullmatch(text):
        raise ValueError(text)
    return text


def valid_input_id(value) -> bool:
    return isinstance(value, str) and len(value) <= 200 and bool(_INPUT_ID.fullmatch(value))


def passthrough_uri(input_id: str) -> str:
    """App-Link für einen Eingang: content://android.media.tv/passthrough/<Kennung, "/" als %2F>. Nur geprüfte
    Kennungen (die Adresse geht auch als Argument an „am start“): quote ohne sichere Zeichen lässt nur
    Buchstaben, Ziffern, _.-~ und %XX übrig."""
    if not valid_input_id(input_id):
        raise ValueError(input_id)
    return PASSTHROUGH + urllib.parse.quote(input_id, safe="")


def port_label(port: int) -> str:
    return "HDMI %d" % int(port)
