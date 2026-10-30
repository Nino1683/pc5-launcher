"""Discord im Hintergrund (E139, Wunsch 2026-10-29: „Könnte man discord in den pc5 launcher einbinden? Als Nachrichten mit
freunden sprachchat etc so wie auf der ps5 nur als discord im Hintergrund?“ – „Ja aber Discord soll optional bleiben und
deaktivierbar“).

Nur Wege, die Discord selbst anbietet (Nutzungsbedingungen): der offizielle Client als Flatpak com.discordapp.Discord,
seine lokale RPC-Schnittstelle für „Spielt gerade“ (ipc.py), Mitlesen der Desktop-Mitteilungen auf dem Sitzungsbus
(notes.py) und Tastendrücke für Tastenbelegungen, die der Nutzer in Discord selbst festlegt. Kein inoffizielles API, kein
Nutzer-Token, kein Bot, kein veränderter oder fremder Client.

Vorgabe aus: Ohne discord.enabled startet, beobachtet und zeigt nichts davon etwas (wiring_discord.py).

Dieses Modul: feste Werte und kleine Prüfungen, ohne GTK und ohne Prozesse.
"""
from __future__ import annotations

import re

FLATPAK = "com.discordapp.Discord"
ENTRY_ID = "app:discord"
TITLE = "Discord"

# Vorgaben der eigenen Schlüssel (E22; die Tabelle in config.py führt sie ebenso)
DEFAULTS = {
    "discord.enabled": False,           # Discord verwenden (alles andere wirkt nur damit)
    "discord.autostart": True,          # mit der Sitzung im Hintergrund starten
    "discord.notify": True,             # Nachrichten aus Discord als Mitteilung des PC5 Launchers
    "discord.presence": True,           # „Spielt gerade …“ über die lokale RPC-Schnittstelle
    "discord.app_id": "",               # Application ID aus dem Developer Portal; leer = keine Anzeige
}

# Hintergrund-Start: Discord legt sein Fenster gar nicht erst an (sonst sein eigener Autostart-Schalter)
BACKGROUND_ARGS = ("--start-minimized",)

# Tastenbelegungen, die der Nutzer in Discord unter Einstellungen › System › Benutzerdefinierte Hotkeys einträgt (Discord 1.0.167). Strg+Umschalt+Alt mit F11/F12:
# legt kein Spiel und kein Programm von sich aus auf; mit Umschalt liefert die F-Taste auch nie die Taste „Konsole
# wechseln“ (Strg+Alt+F11 ergäbe in der XKB-Belegung XF86Switch_VT_11). Namen wie in input/xtest.KEYCODES.
KEYS = {
    "mute": ("Control_L", "Shift_L", "Alt_L", "F11"),       # Discord: „Mikrofon ein-/ausschalten“ (TOGGLE_MUTE)
    "deafen": ("Control_L", "Shift_L", "Alt_L", "F12"),     # Discord: „Ein- und Ausgabe (de)aktivieren“ (TOGGLE_DEAFEN)
}
KEY_LABELS = {"mute": "Strg+Umschalt+Alt+F11", "deafen": "Strg+Umschalt+Alt+F12"}

# Application ID (Snowflake): 17 bis 20 Ziffern
_APP_ID = re.compile(r"[0-9]{17,20}")


def clean_app_id(value) -> str:
    """Application ID ohne Leerzeichen; "" wenn leer. ValueError bei allem, was keine ist."""
    text = re.sub(r"\s+", "", value) if isinstance(value, str) else ""
    if not text:
        return ""
    if not _APP_ID.fullmatch(text):
        raise ValueError(text)
    return text


def app_id_of(config) -> str:
    """Die gespeicherte Application ID, nur wenn sie gültig ist (sonst "": keine Anzeige)."""
    try:
        return clean_app_id(config.get("discord.app_id", ""))
    except (ValueError, AttributeError):
        return ""


def enabled(config) -> bool:
    try:
        return config.get("discord.enabled", False) is True
    except AttributeError:
        return False


def flag(config, key: str) -> bool:
    """Ein Unterschalter (autostart, notify, presence): nur mit discord.enabled, sonst immer aus."""
    if not enabled(config):
        return False
    value = config.get(key, DEFAULTS[key])
    return value if isinstance(value, bool) else DEFAULTS[key]
