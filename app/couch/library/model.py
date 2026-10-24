"""Ein Eintrag der Bibliothek. Vertrag zwischen Quellen, Index, Startern und Oberfläche."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

KINDS = ("game", "app")
SOURCES = ("steam", "proton", "native", "switch", "wiiu", "app", "local", "fixture",
           "rom")                    # E105: ROM-Ordner ohne Steam ROM Manager (library/roms.py)

# Vorgaben der Einstellungen je Spiel; gespeichert wird nur, was davon abweicht
OPTION_DEFAULTS = {
    "proton": "",            # Ordnername des Proton-Builds, leer = Standard aus den Einstellungen
    "fsr4": "auto",          # "auto" | "off"
    "hdr": "auto",           # "auto" | "off"
    "optiscaler": "off",     # "off" | "on": PROTON_USE_OPTISCALER (Wunsch 2026-10-24, je Spiel ein und aus)
    "freeze": True,          # anhalten, solange unsere Oberfläche vorn ist
    "prefer_sdl": True,      # PROTON_PREFER_SDL
    "gameid": "umu-default",
    "env": {},
    "args": "",
    "exe": "",               # nur Archiv-Spiele: gewählte Startdatei
    "resolution": "native",  # "native" | "1440p" | "1080p"
    "fps_limit": 0,
    "perf_overlay": "",      # leer = globale Einstellung
    "launcher": "auto",      # nur Lutris-Spiele: "auto" (Proton) | "lutris" (wie in Lutris, E55)
    "saves": "",             # Spielstand-Ordner (E63), leer = automatisch gefunden
    "pad": "auto",           # "auto" | "xbox" | "direct" (Controller fürs Spiel, E74) | "kbm" (Tastatur und Maus, E64) | "mouse" (Zeiger, E97)
    "pad_look": 1.0,         # Spiel-Modus: Tempo des Umschauens (Faktor, PAD_LOOKS)
    "pad_spots": {},         # Zeiger-Modus: eigene Sprungstellen {taste: {x, y, w, h}} (E99, pad_spots_value)
    "pad_keys": {},          # Spiel-Modus: eigene Tastenbelegung {taste: ziel} (E109, pad_keys_value); {} = E64
}

# E109: Tastenbelegung je Spiel für „Als Tastatur und Maus“ (Profil game). Tasten des Pads nach Lage wie beim
# DualSense (padmouse.PAD_KEY_CODES), Ziele nach Lage der Taste auf der Tastatur (padmouse.TARGET_CODES, E64:
# „y“ ist die Taste KEY_Y, auf QWERTZ beschriftet mit „Z“; die Beschriftung macht erst die Seite)
PAD_KEY_BUTTONS = ("cross", "circle", "square", "triangle", "l1", "r1", "l2", "r2", "l3", "r3", "options", "create",
                   "dpad_up", "dpad_down", "dpad_left", "dpad_right")
PAD_KEY_TARGETS = (tuple("abcdefghijklmnopqrstuvwxyz") + tuple("0123456789")
                   + tuple("f%d" % n for n in range(1, 13))
                   + ("space", "enter", "esc", "tab", "backspace", "shift", "ctrl", "alt", "up", "down", "left", "right",
                      "mouse_left", "mouse_right", "mouse_middle", "wheel_up", "wheel_down", "none"))
# Belegung von E64 (padmouse.KEYS_GAME, BUTTONS_GAME, Auslöser als Maustasten, Steuerkreuz als Pfeile)
PAD_KEYS_DEFAULT = {"cross": "space", "circle": "c", "square": "r", "triangle": "e", "l1": "q", "r1": "f",
                    "l2": "mouse_right", "r2": "mouse_left", "l3": "shift", "r3": "mouse_middle", "options": "esc",
                    "create": "tab", "dpad_up": "up", "dpad_down": "down", "dpad_left": "left", "dpad_right": "right"}
# Vorlagen (Vorschläge, offene Frage 7). Was eine Vorlage nicht nennt, bleibt wie bei E64. Die Seite
# (screens/game-settings.js) hat dieselben Tabellen; tests/unit/input/test_padmouse_keys.py vergleicht sie
PAD_KEY_PRESETS = {
    "standard": {},
    "shooter": {"cross": "space", "circle": "ctrl", "square": "r", "triangle": "e", "l1": "q", "r1": "f", "l3": "shift",
                "r3": "v", "dpad_up": "1", "dpad_right": "2", "dpad_down": "3", "dpad_left": "4", "options": "esc",
                "create": "tab", "r2": "mouse_left", "l2": "mouse_right"},
    "racing": {"r2": "w", "l2": "s", "cross": "space", "circle": "c", "square": "r", "triangle": "enter", "l1": "q",
               "r1": "e", "options": "esc", "create": "tab"},
    "adventure": {"cross": "space", "circle": "esc", "square": "e", "triangle": "i", "l1": "q", "r1": "tab",
                  "dpad_up": "m", "dpad_down": "j", "dpad_left": "left", "dpad_right": "right", "options": "esc",
                  "create": "tab", "r2": "mouse_left", "l2": "mouse_right"},
}


def pad_keys_value(value) -> dict:
    """Option pad_keys prüfen und in ihre gespeicherte Form bringen (E109): je Taste aus PAD_KEY_BUTTONS ein
    Ziel aus PAD_KEY_TARGETS. Unbekannte Tasten und Ziele fallen weg, ebenso Paare, die der Belegung von E64
    entsprechen ({} = Standard; so ergibt „Standard“ und „Zurücksetzen“ dasselbe, und zweimal übernehmen
    ändert nichts). Reihenfolge wie PAD_KEY_BUTTONS. ValueError, wenn value gar keine Zuordnung ist."""
    if not isinstance(value, dict):
        raise ValueError("Die Tastenbelegung besteht aus Paaren aus Taste und Ziel.")
    out = {}
    for button in PAD_KEY_BUTTONS:
        target = value.get(button)
        if isinstance(target, str) and target in PAD_KEY_TARGETS and target != PAD_KEYS_DEFAULT[button]:
            out[button] = target
    return out

# Tasten mit eigener Sprungstelle (padmouse.SPOT_BUTTONS): Steuerkreuz, L1, R1
PAD_SPOT_KEYS = ("up", "down", "left", "right", "l1", "r1")
PAD_SPOT_MAX_SIZE = 16384    # Pixel; größer ist kein Fenster


def pad_spots_value(value) -> dict:
    """Option pad_spots prüfen und in ihre gespeicherte Form bringen: je Taste die Stelle als Anteil
    des Fensters (x, y: 0…1, vier Nachkommastellen) und dessen Größe beim Merken (w, h in Pixeln;
    padmouse nimmt die Stelle nur bei gleichem Seitenverhältnis). ValueError, wenn es nicht passt."""
    if not isinstance(value, dict):
        raise ValueError("Sprungstellen sind Paare aus Taste und Stelle.")
    out = {}
    for key, spot in value.items():
        if key not in PAD_SPOT_KEYS or not isinstance(spot, dict):
            raise ValueError("Diese Sprungstelle gibt es nicht.")
        x, y, w, h = (spot.get(k) for k in ("x", "y", "w", "h"))
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (x, y, w, h)) \
                or not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) \
                or not (1 <= w <= PAD_SPOT_MAX_SIZE and 1 <= h <= PAD_SPOT_MAX_SIZE) or w != int(w) or h != int(h):
            raise ValueError("Diese Sprungstelle liegt nicht im Bild.")
        out[key] = {"x": round(float(x), 4), "y": round(float(y), 4), "w": int(w), "h": int(h)}
    return out


@dataclass
class Entry:
    id: str                              # "steam:1091500", "lutris:alan-wake-2", "emu:switch:<hash>", "app:youtube", "local:<uuid>"
    title: str
    kind: str = "game"
    source: str = "local"
    badge: str = ""                      # Marke auf der Kachel: "STEAM", "PROTON", "SWITCH", "WII U"
    sort_title: str = ""
    # {"tile","cover","cover_full","hero","logo","icon"} -> couch://art/... oder None. tile =
    # quadratisch, fertig zugeschnitten (E27); cover = Fassung fürs Raster (höchstens 640x960),
    # cover_full = dasselbe Cover in bester Größe für eine große Einzelansicht (E34)
    art: dict = field(default_factory=dict)
    available: bool = True               # Dateien/Datenträger vorhanden
    installed: bool = True               # False z. B. bei Kodi, solange das Flatpak fehlt
    tags: list = field(default_factory=list)      # "fsr4", "fsr4-upgrade", "hdr" ...
    last_played: float | None = None
    # Wann das Home das Spiel zuerst installiert gesehen hat (Unix-Zeit; None = schon immer da):
    # Neu Installiertes steht im Home vorn (Wunsch 2026-10-21), dann gilt „zuletzt gespielt“
    added: float | None = None
    playtime: int = 0                    # Sekunden
    hidden: bool = False
    # Favorit (E99): meta.favorite, sonst Steams Sammlung „Favoriten“ (detail.steam_favorite, nur gelesen)
    favorite: bool = False
    options: dict = field(default_factory=dict)   # nur Abweichungen von OPTION_DEFAULTS
    detail: dict = field(default_factory=dict)    # quellenspezifisch; geht NICHT an die Seite

    def option(self, key: str):
        return self.options.get(key, OPTION_DEFAULTS.get(key))

    def view(self, running: bool = False) -> dict:
        """Fassung für die Oberfläche (ohne detail, mit aufgefüllten Optionen)."""
        d = asdict(self)
        d.pop("detail")
        d["options"] = {**OPTION_DEFAULTS, **self.options}
        d["sort_title"] = self.sort_title or self.title.casefold()
        d["running"] = running
        d["origin"] = origin(self)
        return d


# Herkunft für den Filter der Bibliothek (E99): source "local" umfasst Store-, Launcher- und eigene
# Spiele, die Seite sieht detail nicht. Werte: steam, proton, native (Lutris), switch, wiiu, epic, gog,
# itch (E100), battlenet, ubisoft, rockstar, own-windows, own-linux (Archiv und Datei-Explorer), sonst die Quelle.
_LAUNCHER_ORIGINS = ("battlenet", "ubisoft", "rockstar")
_STORE_ORIGINS = {"GOG": "gog", "itch.io": "itch"}     # archive der Store-Spiele mit eigenem Ordner


def origin(entry) -> str:
    source = str(getattr(entry, "source", "") or "")
    if source != "local":
        return source
    detail = getattr(entry, "detail", None)
    detail = detail if isinstance(detail, dict) else {}
    launcher = detail.get("launcher")
    if launcher in _LAUNCHER_ORIGINS:
        return launcher
    if detail.get("epic"):
        return "epic"
    archive = detail.get("archive")
    if isinstance(archive, str) and archive in _STORE_ORIGINS:
        return _STORE_ORIGINS[archive]
    return "own-linux" if detail.get("platform") == "linux" else "own-windows"


def source_title(entry) -> str:
    """Titel, wie ihn die Quelle liefert. Hat der Nutzer den Eintrag umbenannt (meta.title, E90), steht
    der eigene Name in title und der der Quelle in detail.original_title. Spielstände, ihre
    Sicherungen und der Abgleich mit Steam-Verknüpfungen gehen nach diesem Titel."""
    detail = getattr(entry, "detail", None)
    original = detail.get("original_title") if isinstance(detail, dict) else None
    return original if isinstance(original, str) and original else str(getattr(entry, "title", "") or "")
