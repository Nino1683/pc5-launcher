"""Was lib.get über einen Eintrag erzählt: Angaben ("info"), Hinweise ("note") und die möglichen
Aktionen. Rein und schnell (nur der Eintrag und ein paar Werte aus ``env``); nichts hier fasst
die Platte an, deshalb darf lib.get in der Hauptschleife antworten.

env (alles freiwillig, die Verdrahtung füllt es über LibraryIndex.describe_env):
    games_disk      Pfad der Spiele-Platte (für die Anzeige „Spiele-Platte/…“)
    home            Home-Ordner (für „~/…“)
    default_proton  Ordnername des Standard-Proton ("" = unbekannt)
    globals         {"fsr4": bool, "hdr": bool, "pad_auto": bool, "freeze": bool} (E41, E74)
    art             True: Bilder lassen sich ändern (ArtResolver ist da)
    fetch           True: Bilder lassen sich aus dem Netz laden
    delete          True: der Archiv-Installer kann eigene Spiele löschen
    games_root      Spiele-Ordner des Installers (install.games_root): Store-Spiele (GOG, itch.io)
                    außerhalb ihrer Ebene <games_root>/<GOG|itch.io> bekommen kein „Deinstallieren“
                    (E99, E100; fehlt er, keine Prüfung)

Was Platte kostet (Ort der Spielstände), legt die Verdrahtung beim Einlesen in
entry.detail["saves"] ab: {"path", "mode": "in-place"|"copy"|"own", "pending": bool}.
"""
from __future__ import annotations

import os
import time
from typing import Any

from ..installer.trash import STORE_LEVELS
from .model import Entry, source_title

SHORTCUT_PREFIX = "steam:shortcut:"

# Kurze Hinweise zu den Marken der Quellen (tags)
TAG_NOTES = {
    "needs-exe": "Wähle zuerst die Startdatei, dann lässt sich das Spiel starten.",
    "needs-steam": "Braucht den laufenden Steam-Client. Starte es aus Steam Big Picture.",
    "vr": "VR-Spiel: Ohne VR-Brille startet es nicht richtig.",
    "battlenet": "Öffnet den Battle.net-Launcher. Für die Anmeldung brauchst du den Maus-Modus.",
    "fsr4-needs-cachyos": "FSR 4 wirkt erst, wenn du in Steam für dieses Spiel Proton-CachyOS "
                          "als Kompatibilitätswerkzeug wählst.",
}
_TAG_ORDER = ("needs-exe", "needs-steam", "battlenet", "vr", "fsr4-needs-cachyos")

_APP_SOURCES = {"flatpak": "Flatpak", "handover": "Steam", "script": "Konsole", "appimage": "AppImage",
                "browser": "Konsole"}
_EMULATORS = {"eden": "Eden", "cemu": "Cemu"}
_SYSTEMS = {"switch": "Nintendo Switch", "wiiu": "Wii U"}


def system_label(entry: Entry) -> str:
    """E105: Name des Systems eines ROM-Eintrags aus dem Katalog („GameCube (Dolphin)“)."""
    from ..setup import catalog
    d = entry.detail
    system = catalog.system_title(_text(d.get("system"))) or _text(d.get("system"))
    emu = catalog.emulator(_text(d.get("emulator")))
    return "%s (%s)" % (system, emu["title"]) if emu else system

FSR_ACTIVE = "aktiv"
FSR_UPGRADE = "per Upgrade"
FSR_NEEDS = "braucht Proton-CachyOS"
FSR_NONE = "nicht unterstützt"


# ---------------------------------------------------------------- kleine Helfer
def human_time(seconds: int) -> str:
    if seconds < 60:
        return "weniger als eine Minute"
    hours, minutes = divmod(seconds // 60, 60)
    if hours == 0:
        return "%d Min." % minutes
    return "%d Std. %d Min." % (hours, minutes) if minutes else "%d Std." % hours


def human_date(stamp: float | None) -> str:
    if not stamp:
        return "noch nie"
    days = (time.time() - stamp) // 86400
    if days < 1:
        return "heute"
    if days < 2:
        return "gestern"
    return time.strftime("%d.%m.%Y", time.localtime(stamp))


def human_size(size: Any) -> str:
    """Bytes als deutsche Angabe („89,5 GB“). "" bei 0 oder Unsinn."""
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            text = "%d" % value if unit in ("B", "KB") else ("%.1f" % value).replace(".", ",")
            return "%s %s" % (text, unit)
        value /= 1024
    return ""


def is_shortcut(entry: Entry) -> bool:
    return entry.id.startswith(SHORTCUT_PREFIX)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _under(path: str, root: str) -> str | None:
    """Der Teil von path unterhalb von root ("" = root selbst), None wenn er woanders liegt.
    Nur Textvergleich der aufgelösten Schreibweisen, kein Zugriff auf die Platte."""
    if not path or not root:
        return None
    for a, b in ((path, root), (path, _var_home(root)), (_var_home(path), root)):
        a, b = os.path.normpath(a), os.path.normpath(b)
        if a == b:
            return ""
        if a.startswith(b.rstrip(os.sep) + os.sep):
            return a[len(b.rstrip(os.sep)) + 1:]
    return None


def _var_home(path: str) -> str:
    # /home ist hier ein Verweis auf /var/home; beide Schreibweisen kommen vor
    if path.startswith("/var/home/"):
        return path[4:]
    if path.startswith("/home/"):
        return "/var" + path
    return path


def place(path: str, env: dict) -> str:
    """Pfad für die Anzeige: „Spiele-Platte/Alan Wake 2“, „~/Games/…“, sonst wie er ist."""
    if not path:
        return ""
    rel = _under(path, _text(env.get("games_disk")))
    if rel is not None:
        return "Spiele-Platte/" + rel if rel else "Spiele-Platte"
    rel = _under(path, _text(env.get("home")))
    if rel is not None:
        return "~/" + rel if rel else "~"
    return path


def supports_fsr4(tool: str) -> bool:
    # wie steam.supports_fsr4; hier noch einmal, damit dieses Modul ohne vdf auskommt
    return "cachyos" in (tool or "").lower()


# ---------------------------------------------------------------- Angaben
def windows_game(entry: Entry) -> bool:
    """Läuft dieses Spiel über Proton? (Nur dort gibt es Proton-Fassung und FSR 4.)"""
    d = entry.detail
    if entry.kind != "game":
        return False
    if entry.source == "proton":
        return True
    if entry.source == "local":
        return _text(d.get("platform")) != "linux"
    if entry.source == "steam":
        if is_shortcut(entry):
            return bool(d.get("compat_tool")) or _text(d.get("exe")).lower().endswith(".exe")
        return bool(d.get("compat_tool")) or not d.get("native_linux")
    return False


_LAUNCHER_SOURCES = {"battlenet": "Battle.net", "ubisoft": "Ubisoft Connect", "rockstar": "Rockstar Games Launcher"}


def source_label(entry: Entry) -> str:
    d = entry.detail
    if entry.kind == "app":
        return _APP_SOURCES.get(_text(d.get("launcher")), "App")
    if entry.source == "steam":
        return "Steam-Verknüpfung" if is_shortcut(entry) else "Steam"
    if entry.source == "proton":
        return "Lutris (Windows)"
    if entry.source == "native":
        return "Lutris (Linux)"
    if entry.source in _SYSTEMS:
        emulator = _EMULATORS.get(_text(d.get("emulator")), _text(d.get("emulator")).capitalize())
        return "%s (%s)" % (_SYSTEMS[entry.source], emulator) if emulator else _SYSTEMS[entry.source]
    if entry.source == "rom":
        return system_label(entry)
    if entry.source == "local":
        # Aus dem Store (E68, E73, E94) oder von Hand: sagen, woher; sonst das Archiv
        launcher = _LAUNCHER_SOURCES.get(_text(d.get("launcher")))
        if launcher:
            return launcher
        if _text(d.get("epic")):
            return "Epic Games Store"
        if _text(d.get("archive")) == "GOG":
            return "GOG"
        if _text(d.get("archive")) == "itch.io":
            return "itch.io"
        if _text(d.get("archive")) == "aus dem Datei-Explorer":
            return "Von Hand hinzugefügt"
        return "Archiv (Linux)" if _text(d.get("platform")) == "linux" else "Archiv (Windows)"
    if entry.source == "fixture":
        return "Prüf-Eintrag"
    return entry.badge or entry.source


def chosen_proton(entry: Entry, env: dict) -> str:
    """Der Build, mit dem couch dieses Spiel startet ("" = unbekannt)."""
    return _text(entry.option("proton")) or _text(env.get("default_proton"))


def proton_text(entry: Entry, env: dict) -> str:
    d = entry.detail
    if entry.source == "steam":
        tool = _text(d.get("compat_tool"))
        if tool:
            return tool
        if is_shortcut(entry):
            return "Steams Vorgabe"
        return "Linux-Fassung (ohne Proton)" if d.get("native_linux") else "Steams Vorgabe"
    own = _text(entry.option("proton"))
    if own:
        return own
    default = _text(env.get("default_proton"))
    return "Standard (%s)" % default if default else "Standard"


def fsr_text(entry: Entry, env: dict) -> str:
    """„aktiv“ (das Spiel bringt FSR 4 mit), „per Upgrade“ (Proton-CachyOS hebt FSR 3.1 an),
    „braucht Proton-CachyOS“ oder „nicht unterstützt“; dazu „aus“, wenn ein Schalter es abstellt."""
    tags = entry.tags if isinstance(entry.tags, list) else []
    if "fsr4-needs-cachyos" in tags:
        return FSR_NEEDS
    if "fsr4" not in tags and "fsr4-upgrade" not in tags:
        return FSR_NONE
    if entry.source in ("proton", "local"):
        switches = env.get("globals") if isinstance(env.get("globals"), dict) else {}
        if switches.get("fsr4") is False:
            return "aus (Einstellungen › Spiele)"
        if entry.option("fsr4") == "off":
            return "aus (für dieses Spiel)"
        chosen = chosen_proton(entry, env)
        if chosen and not supports_fsr4(chosen):
            return FSR_NEEDS
    return FSR_ACTIVE if "fsr4" in tags else FSR_UPGRADE


def saves_text(entry: Entry, env: dict) -> str:
    """Wo die Spielstände eines Proton-Spiels liegen (der Prefix). "" = keine Angabe."""
    d = entry.detail
    saves = d.get("saves") if isinstance(d.get("saves"), dict) else None
    if saves and _text(saves.get("path")):
        where = place(saves["path"], env)
        mode = saves.get("mode")
        if saves.get("pending"):
            return "werden beim ersten Start aus Lutris übernommen"
        if mode == "in-place":
            return "%s (gemeinsam mit Lutris)" % where
        return where
    if entry.source == "proton" and _text(d.get("prefix")):
        return "%s (Lutris)" % place(d["prefix"], env)
    return ""


def location(entry: Entry) -> str:
    """Der Ordner (oder die Datei) des Spiels, soweit die Quelle ihn kennt."""
    d = entry.detail
    if entry.kind == "app":
        return _text(d.get("flatpak")) or _text(d.get("path"))
    if entry.source in _SYSTEMS or entry.source == "rom":
        return _text(d.get("game_dir")) or _text(d.get("rom"))
    if entry.source == "native" or is_shortcut(entry):
        # Hier ist das Programm oft nur ein Startskript (~/.local/bin): kein Spielordner
        return _text(d.get("game_dir"))
    for key in ("install_dir", "game_dir"):
        if _text(d.get(key)):
            return d[key]
    exe = _text(d.get("exe_path")) or _text(d.get("exe"))
    return os.path.dirname(exe) if os.path.isabs(exe) else ""


def info_rows(entry: Entry, env: dict | None = None) -> list[dict]:
    """Zeilen für „Informationen“ und die Karten im Hub (die ersten drei stehen dort)."""
    env = env or {}
    d = entry.detail
    rows: list[dict] = []

    def add(label: str, value: str) -> None:
        if value:
            rows.append({"label": label, "value": value})

    # Was den Start verhindert, steht ganz vorn
    if not entry.available and entry.installed:
        add("Status", "Dateien nicht erreichbar")
    elif not entry.installed:
        add("Status", "Nicht installiert")
    add("Quelle", source_label(entry))
    if entry.kind == "app":
        add("Zuletzt geöffnet", human_date(entry.last_played) if entry.last_played else "")
        return rows
    if windows_game(entry):
        add("Proton-Fassung", proton_text(entry, env))
        add("FSR 4", fsr_text(entry, env))
        if entry.source in ("proton", "local"):
            add("Spielstände", saves_text(entry, env))
    elif entry.source == "steam" and not is_shortcut(entry):
        add("Proton-Fassung", proton_text(entry, env))
    add("Spielzeit", human_time(entry.playtime) if entry.playtime else "–")
    add("Zuletzt gespielt", human_date(entry.last_played))
    add("Speicherort", place(location(entry), env))
    add("Größe", human_size(d.get("size_on_disk")))
    if entry.source == "steam" and d.get("update_pending"):
        add("Status", "Steam lädt vor dem Start ein Update")
    if source_title(entry) != entry.title:
        add("Ursprünglicher Name", source_title(entry))
    return rows


# ---------------------------------------------------------------- Hinweise
def notes(entry: Entry, env: dict | None = None) -> list[dict]:
    """[{"tag", "text"}]: kurze deutsche Sätze zu den Marken des Eintrags."""
    out = []
    tags = entry.tags if isinstance(entry.tags, list) else []
    for tag in _TAG_ORDER:
        if tag in tags:
            out.append({"tag": tag, "text": TAG_NOTES[tag]})
    hint = _text(entry.detail.get("hint"))
    if hint and not entry.installed:
        out.append({"tag": "not-installed", "text": hint})
    return out


def note(entry: Entry, env: dict | None = None) -> str:
    """Eine Zeile für den Hub: bei Apps die Beschreibung, bei Spielen die Hinweise."""
    texts = [n["text"] for n in notes(entry, env)]
    if entry.kind == "app" and not texts:
        return _text(entry.detail.get("description"))
    return " ".join(texts)


# ---------------------------------------------------------------- Aktionen
def own_install(entry: Entry) -> bool:
    """Ein Spiel, das der Archiv-Installer angelegt hat (nur die darf couch löschen, E17)."""
    return entry.source == "local" and entry.id.startswith("local:") \
        and bool(_text(entry.detail.get("install_dir")))


def deletable(entry: Entry, env: dict | None = None) -> bool:
    """Darf der Papierkorb des Installers den Ordner nehmen? Spiele aus einem Store mit eigener Ebene
    (trash.STORE_LEVELS: GOG E99, itch.io E100) nur mit Marker im Ordner; ältere Installationen ohne
    Marker bekommen den Menüpunkt nicht (er scheiterte sonst immer mit „kein Marker gespeichert“, E91).
    Ebenso Store-Spiele, die mangels Spiele-Platte in ~/Games/<Ebene> landeten: Der Papierkorb nimmt nur
    <Spiele-Ordner>/<Ebene>/<Titel>. Nur ein Vergleich der Zeichenketten (hier wird die Platte nicht
    gefragt); er blendet nur aus, erlauben tut der Papierkorb."""
    level = STORE_LEVELS.get(_text(entry.detail.get("archive")))
    if level is not None:
        if not (_text(entry.detail.get("marker")) and entry.detail.get("marker_ok") is True):
            return False
        root = _text((env or {}).get("games_root"))
        install_dir = _text(entry.detail.get("install_dir"))
        if root and os.path.dirname(os.path.normpath(install_dir)) != os.path.join(os.path.normpath(root), level):
            return False
    return True


def fetchable(entry: Entry) -> bool:
    return entry.kind == "game" and entry.source in ("steam", "proton", "native", "local",
                                                     "switch", "wiiu", "rom")


def actions(entry: Entry, running: bool = False, env: dict | None = None) -> list[dict]:
    """Kennungen für Hauptknopf und Optionsmenü, in der Reihenfolge der Anzeige:
    launch | focus + close, settings, exe, art, art-fetch, rename, rename-reset, info, forget, hide | unhide,
    delete | uninstall (nicht, wenn der Ordner fehlt: env["gone"], E91). settings bekommt jedes Spiel,
    seit E99 auch Steam-Spiele und Steam-Verknüpfungen (die Seite zeigt dort nur, was die Konsole regelt)."""
    env = env or {}
    game = entry.kind == "game"
    tags = entry.tags if isinstance(entry.tags, list) else []
    if running:
        out = [{"id": "focus", "label": "Fortsetzen"},
               {"id": "close", "label": "Spiel schließen" if game else "Schließen"}]
    else:
        out = [{"id": "launch", "label": "Spielen" if game else "Öffnen"}]
    if game:
        out.append({"id": "settings", "label": "Spieleinstellungen"})
    if "needs-exe" in tags and own_install(entry):
        out.append({"id": "exe", "label": "Startdatei wählen"})
    if env.get("art"):
        out.append({"id": "art", "label": "Bild ändern"})
        if env.get("fetch") and fetchable(entry):
            out.append({"id": "art-fetch", "label": "Bilder neu laden"})
    if game:
        out.append({"id": "rename", "label": "Umbenennen"})      # E90
        if source_title(entry) != entry.title:
            out.append({"id": "rename-reset", "label": "Namen zurücksetzen"})
    out.append({"id": "info", "label": "Informationen"})
    if env.get("gone"):
        # Ordner von Hand gelöscht (E91): Es gibt nichts mehr zu deinstallieren, nur den Eintrag
        out.append({"id": "forget", "label": "Aus der Liste entfernen"})
    out.append({"id": "unhide" if entry.hidden else "hide",
                "label": "Wieder einblenden" if entry.hidden else "Aus der Bibliothek ausblenden"})
    if env.get("gone"):
        return out
    if own_install(entry) and entry.detail.get("launcher") and not running:
        # Battle.net, Ubisoft, Rockstar (E94): deinstalliert wird im Launcher, die Konsole räumt danach auf
        out.append({"id": "uninstall", "label": "Deinstallieren", "store": str(entry.detail["launcher"])})
    elif own_install(entry) and entry.detail.get("epic") and not running:
        # Epic-Spiel aus dem Store (E73): legendary räumt Spielordner und Spielliste auf
        out.append({"id": "uninstall", "label": "Deinstallieren"})
    elif env.get("delete") and own_install(entry) and deletable(entry, env) and not running:
        out.append({"id": "delete", "label": "Deinstallieren"})
    elif entry.source == "steam" and not is_shortcut(entry) and entry.installed and game and not running:
        # Steam-Spiele (E69): Steam wird dafür kurz beendet, die Dateien kommen in den Papierkorb
        out.append({"id": "uninstall", "label": "Deinstallieren"})
    return out


def detail(entry: Entry, running: bool = False, env: dict | None = None) -> dict:
    """Alles, was lib.get außer dem Eintrag selbst liefert."""
    env = env or {}
    out = {"info": info_rows(entry, env), "actions": actions(entry, running, env),
           "notes": notes(entry, env), "note": note(entry, env)}
    if isinstance(env.get("globals"), dict):
        # E41: ein globales Aus ist je Spiel nicht übersteuerbar; die Seite zeigt das an
        out["globals"] = dict(env["globals"])
    if not entry.installed and _text(entry.detail.get("setup_id")):
        # E105: „Installieren“ statt „Nicht installiert“ öffnet den Assistenten mit genau diesem Eintrag
        out["setupId"] = _text(entry.detail["setup_id"])
    return out
