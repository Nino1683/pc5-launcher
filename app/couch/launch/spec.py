"""Startauftrag. Starter bauen ihn aus einem Bibliothekseintrag, der AppManager führt ihn aus."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

APP_KINDS = ("game", "emulator", "media", "browser", "desktop", "tool")

# Reste eines Steam- oder Lutris-Elternprozesses (03 Abschnitt 5.3). Sie kippen Proton in den Steam-Modus,
# lassen Starter und Spiele glauben, sie liefen unter Steam (eden-launch nimmt dann Steams virtuelles Pad,
# SDL blendet echte Pads aus) oder schleppen alte Wine-Einstellungen ein. Eine Liste für alle Starter und
# den AppManager (E99). "*" = Muster (strip_env). Die Einzelnamen neben "STEAM_COMPAT_*" sind Absicht:
# proton.build_env lässt das Muster weg, wenn es eine selbst gesetzte Variable träfe.
STEAM_LEFTOVERS = (
    "SteamAppId", "SteamGameId", "SteamOverlayGameId", "SteamClientLaunch", "SteamEnv",
    "STEAM_COMPAT_*",
    "STEAM_COMPAT_APP_ID", "STEAM_COMPAT_CLIENT_INSTALL_PATH", "STEAM_COMPAT_CONFIG",
    "STEAM_COMPAT_DATA_PATH", "STEAM_COMPAT_FLAGS", "STEAM_COMPAT_INSTALL_PATH",
    "STEAM_COMPAT_LAUNCHER_SERVICE", "STEAM_COMPAT_LIBRARY_PATHS", "STEAM_COMPAT_MEDIA_PATH",
    "STEAM_COMPAT_MOUNTS", "STEAM_COMPAT_SHADER_PATH", "STEAM_COMPAT_TOOL_PATHS",
    "STEAM_COMPAT_TRANSCODED_MEDIA_PATH",
    "STEAM_FOSSILIZE_DUMP_PATH", "DXVK_STATE_CACHE_PATH",
    "SteamVirtualGamepadInfo*", "SteamVirtualGamepadInfo", "SteamVirtualGamepadInfo_Proton",
    "SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", "SDL_GAMECONTROLLER_IGNORE_DEVICES",
    "ENABLE_VK_LAYER_VALVE_steam_overlay_1", "ENABLE_VK_LAYER_VALVE_steam_fossilize_1",
    "LD_PRELOAD", "WINEPREFIX", "WINEARCH", "WINEDLLOVERRIDES", "PROTON_ENABLE_HDR",
    "PROTON_VERB", "UMU_ID", "EXE",
    # Shell-Funktionen der Spielmodus-Sitzung (gamescope-session-plus exportiert sie): gehören in kein Spiel
    # und füllten umus Protokoll und das Fehlerfenster (2026-10-24)
    "BASH_FUNC_*",
)


def strip_env(base: dict, names) -> dict:
    """base ohne die Variablen aus names (spec.unset); Namen mit "*" sind Muster (fnmatch, Groß- und
    Kleinschreibung zählt). Eine Regel für jeden, der spec.unset anwendet (E99): vorher wertete nur
    eine ungenutzte Hilfsfunktion Muster aus, der AppManager verglich wörtlich."""
    names = [str(n) for n in (names or ())]
    exact = {n for n in names if "*" not in n}
    patterns = [n for n in names if "*" in n]
    return {key: val for key, val in base.items()
            if key not in exact and not any(fnmatch.fnmatchcase(key, p) for p in patterns)}


class LaunchError(Exception):
    """Start nicht möglich. message ist ein deutscher Satz für die Oberfläche.

    reason ist eine kurze Kennung der Ursache für die Oberfläche (Ereignis app.error, Feld
    code), z. B. "needs-exe": Sie öffnet dann die Auswahl der Startdatei. Leer = allgemein.
    Bewusst nicht "code": So heißt das Feld, aus dem rpc.error_object den Namen des Fehlers
    für die Antwort nimmt, und der bleibt "LaunchError"."""

    def __init__(self, message: str, detail: str = "", reason: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.reason = reason


@dataclass
class LaunchSpec:
    entry_id: str
    argv: list
    env: dict = field(default_factory=dict)        # Zusätze zur bereinigten Grundumgebung
    unset: list = field(default_factory=list)      # zusätzlich zu entfernende Variablen
    cwd: str | None = None
    kind: str = "game"                             # einer aus APP_KINDS
    freeze: bool = True                            # darf angehalten werden, wenn unsere Oberfläche vorn ist
    mouse: bool | str = False                      # padmouse aktiv, solange die App vorn ist; "keys" = nur Tasten (KEYS_TV);
                                                   # "game" = Tastatur und Maus für Spiele ohne Controller (E64);
                                                   # "term" = Terminal „Claude Code“ (E72); "cards" = Zeiger (E97)
    polite_stop: list | None = None                # Befehl für höfliches Beenden, sonst SIGTERM ans Hauptkind
    prelaunch: list = field(default_factory=list)  # Liste von Befehlen (je eine argv-Liste), vor dem Start
    handover: dict | None = None                   # nur die Kachel „Steam“: Inhalt für handover.json; dann wird nichts gestartet
    title: str = ""                                # für Ladebild und Fehlermeldungen
    # Keine eigene Nummer im Bild: Jede App, auch ein Steam-Spiel, trägt die AppId ihres reaper (E57,
    # Korrektur 2026-10-18). Fenster, die Steam mit STEAM_GAME umbenennt, setzt gs/focus.py zurück (E71).
    start_timeout: float | None = None             # Sekunden bis zum ersten Bild, sonst shell.launch_timeout
    # nur anhalten, solange sie ein Fenster hat: Steam-Spiele (Steam räumt sonst gerade auf) und seit E99
    # auch Spiele über einen Launcher (ohne Fenster des Spiels nicht anhalten: Start und Nachlauf)
    freeze_needs_window: bool = False
    kill_grace: float | None = None                # Sekunden zwischen Bitte und SIGKILL, sonst COUCH_KILL_GRACE
    # Spiele über den Launcher eines Herstellers (E94): Fenster dieser Programme (klein, z. B.
    # "battle.net.exe") zeigt die Konsole nicht; man sieht das Ladebild, bis das Spiel erscheint
    hide_windows: tuple = ()
    # E109: Tastenbelegung im Spiel-Modus {taste: ziel} (Option pad_keys, nur mouse == "game"; {} = E64). Beim
    # Start festgehalten (apply_pad_keys): wirkt ab dem nächsten Start und kommt mit running.json mit
    pad_keys: dict = field(default_factory=dict)


# Spiele, die man nur mit der Maus spielt (E97): Mit „Controller › Automatisch“ wird das Pad zum Zeiger
# (padmouse profile "cards"), sonst passiert mit dem Controller nichts. Wert = Sprungstellen
# (padmouse.LAYOUTS), "" = nur Zeiger und Maustasten. Schlüssel: (Launcher, Code des Launchers).
POINTER_GAMES = {
    ("battlenet", "WTCG"): "hearthstone",      # Hearthstone: am PC nur Maus und Touch
    ("battlenet", "S1"): "",                   # StarCraft: Remastered
    ("battlenet", "S2"): "",                   # StarCraft II
    ("battlenet", "W3"): "",                   # Warcraft III: Reforged
    ("battlenet", "Hero"): "",                 # Heroes of the Storm
    ("battlenet", "D3"): "",                   # Diablo III (Controller nur auf Konsolen)
}


def _pointer_key(entry) -> tuple:
    d = entry.detail if entry.source == "local" else {}
    return (str(d.get("launcher") or ""), str(d.get("launcher_code") or ""))


def pad_mode(entry) -> bool | str:
    """spec.mouse eines Spiels aus den Spieleinstellungen „Controller“: "game" (Als Tastatur und
    Maus, E64), "cards" (Als Maus, E97; auch bei Automatisch für POINTER_GAMES), sonst False."""
    pad = entry.option("pad")
    if pad == "kbm":
        return "game"
    if pad == "mouse" or (pad == "auto" and _pointer_key(entry) in POINTER_GAMES):
        return "cards"
    return False


def pad_layout(entry) -> str:
    """Sprungstellen des Zeiger-Modus für dieses Spiel (padmouse.LAYOUTS) oder ""."""
    return POINTER_GAMES.get(_pointer_key(entry), "") if pad_mode(entry) == "cards" else ""


def pad_spots(entry) -> dict:
    """Eigene Sprungstellen des Zeiger-Modus (Option pad_spots, E99): {taste: {x, y, w, h}}. Sie liegen
    über denen aus pad_layout und gelten für jedes Spiel im Zeiger-Modus. Unbrauchbares gilt als leer."""
    from ..library.model import pad_spots_value
    try:
        return pad_spots_value(entry.option("pad_spots") or {})
    except (ValueError, TypeError, AttributeError):
        return {}


def pad_keys(entry) -> dict:
    """Eigene Tastenbelegung des Spiel-Modus (Option pad_keys, E109): {taste: ziel}, geprüft wie in
    library.model.pad_keys_value. Unbrauchbares gilt als leer, also als Belegung von E64."""
    from ..library.model import pad_keys_value
    try:
        return pad_keys_value(entry.option("pad_keys") or {})
    except (ValueError, TypeError, AttributeError):
        return {}


def apply_pad_keys(spec: LaunchSpec, entry) -> LaunchSpec:
    """spec.pad_keys aus der Option des Eintrags, nur wenn das Pad als Tastatur und Maus läuft
    (spec.mouse == "game", E64); sonst {}. Die Shell ruft das beim Start, nach build_spec (E109)."""
    spec.pad_keys = pad_keys(entry) if getattr(spec, "mouse", False) == "game" else {}
    return spec
