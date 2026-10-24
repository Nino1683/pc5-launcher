"""Einstellungen sichern und übernehmen (E106, Teil P11; Bauplan docs/research/e103/SPEC.md 9.1).

Für einen neuen Rechner oder eine Steam Machine: „PC5 Launcher Einstellungen <JJJJ-MM-TT HH-MM>.zip“
in ~/Games/backups/couch/einstellungen (wie die Spielstände, E63) oder auf einem eingehängten Laufwerk.

Im ZIP (nur diese Namen, alle in couch-einstellungen.json mit sha256 und Größe):
  settings.json        gesetzte Einstellungen ohne Geheimnisse und ohne Werte dieses Rechners (BLOCKED)
  library-meta.json    LibraryIndex.export_meta(): eigene Namen, Favoriten, Ausgeblendetes, Optionen,
                       Spielzeiten der übertragbaren IDs (index.transferable)
  home-order.json      Reihenfolge des Home, nur übertragbare IDs
  sessions.json        Sitzungsverlauf, nur übertragbare IDs
  art/choices.json     von Hand gewählte Bildtreffer, art/images.json + art/user/<n>-<art>.<jpg|png>: eigene Bilder
  browser-prefs.json   Vorlieben des Browsers (Favoriten, Startseite, Suche, Zoom, Werbeblocker)
  terminal-prefs.json  Schriftgröße des Terminals
Nie: SteamGridDB-Schlüssel, Anmeldungen (*-token.json, Epic, Cookies), WebKit-Daten, <state>,
<config>/sicherung. Es wird nur gelesen, was hier ausdrücklich genannt ist; nichts wird durchsucht.

Übernehmen prüft das ZIP vollständig, bevor es etwas ändert (read_backup: höchstens 50 MB und 2000
Einträge, keine „..“, keine absoluten Namen, keine Verweise, keine Ordner, nur bekannte Namen, Format 1,
jede Prüfsumme), sichert dann den eigenen Stand („… vor-übernahme.zip“) und wendet die gewählten Teile
über die laufenden Dienste an (Host, gefüllt von wiring_backup.py): Einstellungen über config.set (nur
Schlüssel aus config.DEFAULTS, Wert wie dort typisiert und im erlaubten Bereich), Bibliothek und
Sitzungen über den Index (library.json schreibt weiter nur er, E28), Reihenfolge über
wiring_home.write_order, eigene Bilder über ArtResolver.set_user_art (prüft das Bild). Die Bildwahl
(art/choices.json) und die Vorlieben werden als neue Dateien atomar geschrieben; sie gelten nach dem
Neustart des PC5 Launchers bzw. beim nächsten Öffnen von Browser und Terminal. Gelöscht wird nichts;
was hier schon gewählt ist (Bild, Bildwahl), bleibt. Zweimal übernehmen ergibt dasselbe.
Alles hier blockiert: nur im Arbeits-Thread aufrufen.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Callable

from . import PRODUCT, VERSION, config as config_mod, jsontext, log, paths, system
from .library.index import transferable

_log = log.get("backup")

FORMAT = 1
MANIFEST = "couch-einstellungen.json"
PREFIX = "PC5 Launcher Einstellungen "
SAFETY_NOTE = "vor-übernahme"
MAX_ZIP = 50 * 1024 * 1024              # Größe der Datei und Summe der entpackten Einträge
MAX_FILES = 2000
MAX_JSON = 8 * 1024 * 1024              # je JSON-Datei im ZIP
MAX_IMAGE = 8 * 1024 * 1024             # je eigenes Bild; größere bleiben auf dem alten Rechner
IMAGE_BUDGET = 40 * 1024 * 1024         # alle Bilder zusammen (das ZIP bleibt unter MAX_ZIP)
PARTS = ("settings", "games", "order", "sessions", "art", "prefs")
ART_KINDS = ("tile", "cover", "hero", "logo", "icon")          # wie library/art.py KINDS
USER_PREFIX = "user-"                                           # wie library/art.py USER_PREFIX
TERMINAL_FONT = (14, 40)                                        # wie terminal/window.py FONT_MIN, FONT_MAX

# Einstellungen, die nie mitreisen: Geheimnisse und Werte, die an diesem Rechner hängen (Fernseher,
# Platten, Adapter). library.roms setzt die Migration im Bestand bzw. der Assistent (E105) je Rechner.
BLOCKED_KEYS = frozenset((
    "library.sgdb_key", "input.bt_adapter", "audio.sink", "display.hdr", "display.hdr_confirmed",
    "display.sdr_brightness", "display.vrr", "display.area_scale", "library.games_disk", "emu.root",
    "install.games_root", "games.prefix_root", "library.roms"))
BLOCKED_PREFIXES = ("hints.",)
# Erlaubte Werte über den Typ hinaus. Was hier fehlt, prüfen die Dienste beim Lesen selbst (perf,
# accessories): Zahlen von, bis …
RANGES: dict[str, tuple] = {
    "audio.ui_volume": (0.0, 1.0), "notify.seconds": (2, 30), "shell.launch_timeout": (10, 600),
    "shell.resume_delay_ms": (0, 5000), "install.reserve_gib": (0, 1000), "install.trash_days": (1, 365),
    "ui.saver_after": (0, 1440), "power.sleep_after": (0, 1440), "power.sleep_after_media": (0, 1440),
    "input.idle_off_minutes": (0, 1440), "input.ps_long_ms": (200, 5000), "input.ps_quit_ms": (1000, 10000),
    "input.mouse_speed": (0.25, 4.0), "perf.fps_limit": (0, 1000),
}
# … und Auswahlen
CHOICES: dict[str, tuple] = {
    "ui.scale": (1.0, 1.1, 1.2), "audio.ui_pack": ("couch", "pc5"), "saves.keep": (5, 10, 20),
    "browser.app": ("couch", "chrome"), "input.capture_button": ("ps-create", "create"),
    "display.game_resolution": ("native", "1440p", "1080p"),
    "display.scaling_filter": ("linear", "nearest", "fsr", "nis"),
}
_NAMES = re.compile(r"(settings|library-meta|home-order|sessions|browser-prefs|terminal-prefs)\.json"
                    r"|art/(choices|images)\.json|art/user/\d{1,5}-(tile|cover|hero|logo|icon)\.(jpg|png)")
_URL = re.compile(r"https?://[^\s\x00-\x1f]{1,2040}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
DAMAGED = "Diese Sicherung ist beschädigt oder stammt nicht vom PC5 Launcher."


class BackupError(Exception):
    """Fehler mit einem deutschen Satz für die Oberfläche; geändert wurde nichts."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Host:
    """Was backup.py vom laufenden Home braucht (wiring_backup.py füllt es; Tests mit echten Diensten im
    Käfig). Lesen und Schreiben des Index und der Einstellungen läuft dort in der Hauptschleife."""
    settings: Callable[[], dict]                        # config.all()
    set_setting: Callable[[str, Any], None]             # config.set
    export_meta: Callable[[], dict]
    import_meta: Callable[[dict], dict]
    export_sessions: Callable[[], list]
    import_sessions: Callable[[list], dict]
    known: Callable[[str], bool]                        # gibt es den Eintrag hier (steam:<appid> immer)?
    entry_ids: Callable[[], list]                       # IDs der Bibliothek (für eigene Bilder)
    read_order: Callable[[], list]
    write_order: Callable[[list], Any]
    art_dir: Callable[[], str]                          # <data>/art
    entry_art_dir: Callable[[str], str]                 # <data>/art/<safe_id>
    set_user_art: Callable[[str, str, str], Any] | None = None
    proton_exists: Callable[[str], bool] = lambda name: False
    running_apps: Callable[[], list] = lambda: []       # laufende Apps, deren Vorlieben übernommen werden
    after_apply: Callable[[], Any] = lambda: None       # Bibliothek neu einlesen (Bilder)
    browser_prefs: str = ""
    terminal_prefs: str = ""


@dataclass
class Backup:
    path: str
    manifest: dict
    files: dict = field(default_factory=dict)           # Name -> Bytes (geprüft)

    def json(self, name: str, default: Any = None) -> Any:
        blob = self.files.get(name)
        if blob is None:
            return default
        try:
            return json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise BackupError(DAMAGED + " (%s)" % name) from None


# ---------------------------------------------------------------- Orte
def default_dir() -> str:
    """~/Games/backups/couch/einstellungen; im Prüfmodus unter <COUCH_TEST_ROOT>/home (paths.user_place)."""
    return paths.user_place(os.path.join("Games", "backups", "couch", "einstellungen"))


_NET_FS = ("cifs", "smb", "nfs", "9p", "sshfs", "fuse.sshfs", "davfs", "fuse.davfs", "afs", "ceph", "fuse.rclone")


def _mount_types(mountinfo: str = "/proc/self/mountinfo") -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                left, _sep, right = line.partition(" - ")
                fields = left.split()
                if len(fields) >= 5 and right:
                    out[fields[4].replace("\\040", " ")] = right.split()[0]
    except OSError:
        pass
    return out


def drives() -> list[str]:
    """Eingehängte Laufwerke (oberste Ebene): /run/media/<nutzer>/*, /run/media/* (ältere SteamOS-
    Fassungen), /media/<nutzer>/*; nur echte Einhängepunkte, keine Netzlaufwerke (die können hängen).
    Nie selbst eingehängt. Im Prüfmodus die Ordner unter <COUCH_TEST_ROOT>/media."""
    root = paths.test_root()
    if paths.test_mode():
        base = os.path.join(root, "media") if root else ""
        try:
            return sorted(os.path.join(base, n) for n in os.listdir(base)
                          if os.path.isdir(os.path.join(base, n)) and not os.path.islink(os.path.join(base, n)))
        except OSError:
            return []
    user = paths.user_name()
    types = _mount_types()
    found: list[str] = []
    for base in (os.path.join("/run/media", user), "/run/media", os.path.join("/media", user)):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            fs = types.get(p, "")
            if p not in found and fs and not fs.startswith(_NET_FS) and os.path.ismount(p):
                found.append(p)
    return found


def roots() -> list[str]:
    """Wo Sicherungen liegen und hin dürfen: der Vorgabeordner (auch wenn ~/Games ein Verweis auf eine
    andere Platte ist), das eigene Home und die Laufwerke; im Prüfmodus nur der Käfig."""
    if paths.test_mode():
        root = paths.test_root()
        return [os.path.realpath(root)] if root else []
    return [os.path.realpath(default_dir()), os.path.realpath(paths.home())] + [os.path.realpath(d) for d in drives()]


def allowed(path: str) -> bool:
    return isinstance(path, str) and os.path.isabs(path) and paths.is_under(path, roots())


def system_label() -> str:
    """Bezeichnung des Systems ohne Rechnernamen („Bazzite 43“, „SteamOS 3.8 · Steam Machine“)."""
    try:
        from . import sysdetect                 # E104 (Teil P2); fehlt es, gilt os-release
        d = sysdetect.detect()
        name = " ".join(str(x) for x in (d["os"].get("name"), d["os"].get("version")) if x)
        device = {"steam-machine": "Steam Machine", "steam-deck": "Steam Deck"}.get(d.get("device") or "", "")
        if name:
            return name + (" · " + device if device else "")
    except Exception:
        pass
    try:
        with open("/etc/os-release", encoding="utf-8", errors="replace") as f:
            values = dict(line.strip().split("=", 1) for line in f if "=" in line)
        return (values.get("PRETTY_NAME") or values.get("NAME") or "").strip().strip('"')[:80]
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------- Einstellungen
def blocked(key: str) -> bool:
    return key in BLOCKED_KEYS or key.startswith(BLOCKED_PREFIXES)


def settings_of(values: dict) -> dict:
    """Was aus config.all() in die Sicherung kommt: bekannte Schlüssel mit eigenem Wert, ohne BLOCKED."""
    out = {}
    for key, default in config_mod.DEFAULTS.items():
        if key in values and not blocked(key) and values[key] != default:
            out[key] = copy.deepcopy(values[key])
    return out


def check_setting(key: str, value: Any, proton_exists: Callable[[str], bool] = lambda n: False) -> Any:
    """Wert aus einer Sicherung in die Form von config.DEFAULTS bringen; ValueError, wenn er nicht passt."""
    if not isinstance(key, str) or key not in config_mod.DEFAULTS or blocked(key):
        raise ValueError("unbekannter oder gesperrter Schlüssel")
    default = config_mod.DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError("Ein/Aus erwartet")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError("ganze Zahl erwartet")
        value = int(value)
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise ValueError("Zahl erwartet")
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
            raise ValueError("Text erwartet")
    elif isinstance(default, dict):
        if not isinstance(value, dict) or len(jsontext.dumps(value)) > 16384:
            raise ValueError("Objekt erwartet")
        value = copy.deepcopy(value)
    else:
        raise ValueError("Art des Werts unbekannt")
    if key in RANGES and not RANGES[key][0] <= value <= RANGES[key][1]:
        raise ValueError("außerhalb des Bereichs")
    if key in CHOICES and value not in CHOICES[key]:
        raise ValueError("diese Auswahl gibt es nicht")
    if key == "games.proton" and value and not proton_exists(value):
        raise ValueError("diese Proton-Fassung gibt es hier nicht")
    if key == "store.itch_client_id" and value and not _HEX32.fullmatch(value):
        raise ValueError("keine App-Kennung")
    if key == "browser.home" and value and not _URL.fullmatch(value):
        raise ValueError("keine Adresse")
    if key == "browser.search" and value and ("%s" not in value or not _URL.fullmatch(value.replace("%s", "x"))):
        raise ValueError("keine Suchadresse")
    if key == "input.lightbar" and not (set(value) <= {"color", "brightness"}
                                        and isinstance(value.get("color", ""), str)
                                        and isinstance(value.get("brightness", 100), (int, float))):
        raise ValueError("keine Lichtleiste")
    return value


# ---------------------------------------------------------------- Sichern
def _dumps(data: Any) -> bytes:
    return (jsontext.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8")


def _read_json_file(path: str) -> Any:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_JSON:
            return None
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError):
        return None


def browser_prefs_of(path: str) -> dict | None:
    """Vorlieben des Browsers in der Form, die er selbst speichert (über seine eigene Prüfung)."""
    if not path or not os.path.isfile(path) or os.path.islink(path):
        return None
    from .browser.prefs import Prefs
    p = Prefs(path)
    return {"zoom": p.zoom, "home": p.home, "search": p.search, "adblock": p.adblock, "favorites": p.favorites}


def terminal_prefs_of(path: str) -> dict | None:
    data = _read_json_file(path) if path else None
    font = data.get("font") if isinstance(data, dict) else None
    if isinstance(font, bool) or not isinstance(font, int) or not TERMINAL_FONT[0] <= font <= TERMINAL_FONT[1]:
        return None
    return {"font": font}


def _user_images(host: Host) -> list[tuple[str, str, str]]:
    """(ID, Art, Pfad) der eigenen Bilder übertragbarer Einträge; nur echte Dateien, keine Verweise."""
    out = []
    budget = IMAGE_BUDGET
    for entry_id in sorted({i for i in host.entry_ids() if transferable(i)}):
        folder = host.entry_art_dir(entry_id)
        if not folder or os.path.islink(folder):
            continue
        for kind in ART_KINDS:
            for ext in (".jpg", ".png"):
                p = os.path.join(folder, USER_PREFIX + kind + ext)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and 0 < st.st_size <= min(MAX_IMAGE, budget):
                    budget -= st.st_size
                    out.append((entry_id, kind, p))
                    break
    return out


def collect(host: Host) -> tuple[dict[str, bytes], dict]:
    """Inhalt der Sicherung: (Name -> Bytes, Zählung je Teil)."""
    files: dict[str, bytes] = {}
    counts = {part: 0 for part in PARTS}
    settings = settings_of(host.settings())
    files["settings.json"] = _dumps(settings)
    counts["settings"] = len(settings)
    meta = {k: v for k, v in host.export_meta().items() if transferable(k)}
    files["library-meta.json"] = _dumps(meta)
    counts["games"] = len(meta)
    order = [i for i in host.read_order() if transferable(i)]
    files["home-order.json"] = _dumps({"games": order})
    counts["order"] = len(order)
    sessions = [s for s in host.export_sessions() if isinstance(s, dict) and transferable(s.get("id"))]
    files["sessions.json"] = _dumps({"version": 1, "sessions": sessions})
    counts["sessions"] = len(sessions)
    choices = _read_json_file(os.path.join(host.art_dir(), "choices.json"))
    choices = {k: v for k, v in choices.items() if transferable(k) and isinstance(v, dict)} \
        if isinstance(choices, dict) else {}
    files["art/choices.json"] = _dumps(choices)
    images = []
    for n, (entry_id, kind, path) in enumerate(_user_images(host), 1):
        name = "art/user/%d-%s%s" % (n, kind, os.path.splitext(path)[1])
        try:
            with open(path, "rb") as f:
                files[name] = f.read(MAX_IMAGE + 1)
        except OSError:
            continue
        images.append({"id": entry_id, "kind": kind, "file": name})
    files["art/images.json"] = _dumps(images)
    counts["art"] = len(choices) + len(images)
    for name, value in (("browser-prefs.json", browser_prefs_of(host.browser_prefs)),
                        ("terminal-prefs.json", terminal_prefs_of(host.terminal_prefs))):
        if value is not None:
            files[name] = _dumps(value)
            counts["prefs"] += 1
    return files, counts


def file_name(now: float, note: str = "") -> str:
    stamp = time.strftime("%Y-%m-%d %H-%M", time.localtime(now))
    return PREFIX + stamp + (" " + note if note else "") + ".zip"


def export_zip(host: Host, folder: str | None = None, now: float | None = None, note: str = "") -> dict:
    """Baut die Sicherung in folder (Vorgabe default_dir()). Atomar: erst eine Teildatei daneben, dann
    rename ohne Ersetzen einer vorhandenen Sicherung (gleiche Minute: „ (2)“). {path, size, counts}."""
    now = time.time() if now is None else now
    if folder is None:
        folder = default_dir()
        os.makedirs(folder, mode=0o700, exist_ok=True)
    if not allowed(folder) or not os.path.isdir(folder):
        raise BackupError("Dorthin kann der PC5 Launcher nicht sichern.")
    files, counts = collect(host)
    manifest = {"format": FORMAT, "product": PRODUCT, "version": VERSION, "created": int(now),
                "system": system_label(), "counts": counts,
                "files": [{"name": n, "sha256": hashlib.sha256(b).hexdigest(), "size": len(b)}
                          for n, b in sorted(files.items())]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        stamp = time.localtime(now)[:6]
        for name, blob in [(MANIFEST, _dumps(manifest))] + sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=max(stamp, (1980, 1, 1, 0, 0, 0)))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            zf.writestr(info, blob)
    blob = buf.getvalue()
    if len(blob) > MAX_ZIP:
        raise BackupError("Die Sicherung wäre größer als 50 MB. Entferne ein paar eigene Bilder und versuch es noch einmal.")
    base = file_name(now, note)
    target = os.path.join(folder, base)
    for n in range(2, 100):
        if not os.path.lexists(target):
            break
        target = os.path.join(folder, base[:-4] + " (%d).zip" % n)
    try:
        system.write_atomic(target, blob)
    except OSError as e:
        raise BackupError("Die Sicherung ließ sich nicht schreiben: %s" % (e.strerror or e)) from None
    _log.info("Einstellungen gesichert: %s (%d Bytes, %s)", target, len(blob), counts)
    return {"path": target, "size": len(blob), "counts": counts}


# ---------------------------------------------------------------- Prüfen
def _check_name(name: str) -> bool:
    if not name or len(name) > 255 or name.startswith("/") or "\\" in name or ":" in name:
        return False
    if any(ord(c) < 32 for c in name):
        return False
    parts = name.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def read_backup(path: str) -> Backup:
    """Öffnet und prüft eine Sicherung vollständig; BackupError mit Grund, wenn etwas nicht stimmt."""
    if not isinstance(path, str) or not path.endswith(".zip") or not allowed(path):
        raise BackupError("Diese Datei liegt an keinem erlaubten Ort.")
    try:
        st = os.lstat(path)
    except OSError:
        raise BackupError("Die Sicherung gibt es nicht mehr.") from None
    if stat.S_ISLNK(st.st_mode):
        raise BackupError("Diese Sicherung ist ein Verweis. Der PC5 Launcher öffnet nur echte Dateien.")
    if not stat.S_ISREG(st.st_mode):
        raise BackupError(DAMAGED)
    if st.st_size > MAX_ZIP:
        raise BackupError("Die Sicherung ist größer als 50 MB. So große Sicherungen legt der PC5 Launcher nicht an.")
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError):
        raise BackupError(DAMAGED) from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise BackupError(DAMAGED + " (zu viele Einträge)")
        names: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            if not _check_name(name):
                raise BackupError(DAMAGED + " (Name „%s“)" % name[:80])
            if info.is_dir() or stat.S_ISDIR(info.external_attr >> 16):
                raise BackupError(DAMAGED + " (Ordner „%s“)" % name[:80])
            if stat.S_ISLNK(info.external_attr >> 16):
                raise BackupError(DAMAGED + " (Verweis „%s“)" % name[:80])
            if name in names:
                raise BackupError(DAMAGED + " (doppelt: „%s“)" % name[:80])
            if name != MANIFEST and not _NAMES.fullmatch(name):
                raise BackupError(DAMAGED + " (unbekannt: „%s“)" % name[:80])
            names.add(name)
            total += info.file_size
            if info.file_size > MAX_ZIP or total > MAX_ZIP:
                raise BackupError("Die Sicherung ist entpackt größer als 50 MB.")
        if MANIFEST not in names:
            raise BackupError(DAMAGED)
        try:
            manifest = json.loads(_read_member(zf, MANIFEST, MAX_JSON).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise BackupError(DAMAGED) from None
        if not isinstance(manifest, dict) or manifest.get("product") != PRODUCT:
            raise BackupError(DAMAGED)
        if manifest.get("format") != FORMAT:
            raise BackupError("Diese Sicherung stammt von einer neueren Fassung des PC5 Launchers. Aktualisiere zuerst.")
        listed = manifest.get("files")
        if not isinstance(listed, list) or not all(isinstance(f, dict) for f in listed):
            raise BackupError(DAMAGED)
        by_name = {f.get("name"): f for f in listed}
        if len(by_name) != len(listed) or set(by_name) != names - {MANIFEST}:
            raise BackupError(DAMAGED + " (Inhaltsverzeichnis)")
        files: dict[str, bytes] = {}
        for name, f in sorted(by_name.items()):
            limit = MAX_IMAGE if name.startswith("art/user/") else MAX_JSON
            blob = _read_member(zf, name, limit)
            if len(blob) != f.get("size") or hashlib.sha256(blob).hexdigest() != f.get("sha256"):
                raise BackupError(DAMAGED + " (Prüfsumme von „%s“)" % name)
            files[name] = blob
    return Backup(path=path, manifest=manifest, files=files)


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        with zf.open(name) as f:
            blob = f.read(limit + 1)
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError, EOFError, NotImplementedError):
        raise BackupError(DAMAGED + " („%s“ lässt sich nicht lesen)" % name) from None
    if len(blob) > limit:
        raise BackupError(DAMAGED + " („%s“ ist zu groß)" % name)
    return blob


# ---------------------------------------------------------------- Liste und Vorschau
def describe(path: str) -> dict:
    """Ein Listeneintrag; liest nur das Inhaltsverzeichnis (ok False, wenn es nicht taugt)."""
    st = os.lstat(path)
    item = {"path": path, "name": os.path.basename(path)[:-4], "size": st.st_size, "created": int(st.st_mtime),
            "version": "", "system": "", "counts": {}, "safety": SAFETY_NOTE in os.path.basename(path), "ok": False}
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(_read_member(zf, MANIFEST, 1024 * 1024).decode("utf-8"))
        if isinstance(manifest, dict) and manifest.get("product") == PRODUCT:
            created = manifest.get("created")
            if isinstance(created, int) and not isinstance(created, bool) and created > 0:
                item["created"] = created
            item["version"] = str(manifest.get("version") or "")[:40]
            item["system"] = str(manifest.get("system") or "")[:80]
            counts = manifest.get("counts")
            item["counts"] = {k: int(v) for k, v in counts.items() if k in PARTS and isinstance(v, int)} \
                if isinstance(counts, dict) else {}
            item["ok"] = manifest.get("format") == FORMAT
    except (zipfile.BadZipFile, OSError, ValueError, KeyError, UnicodeDecodeError, BackupError, RecursionError):
        pass
    return item


def list_backups(extra: str | None = None) -> list[dict]:
    """Sicherungen im Vorgabeordner, auf der obersten Ebene jedes Laufwerks und in extra; neueste zuerst."""
    mounted = drives()
    folders = [default_dir()] + mounted + ([extra] if extra and allowed(extra) else [])
    seen: set[str] = set()
    out = []
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not (name.startswith(PREFIX) and name.endswith(".zip")):
                continue
            path = os.path.join(folder, name)
            real = os.path.realpath(path)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if real in seen or not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ZIP:
                continue
            seen.add(real)
            item = describe(path)
            item["drive"] = os.path.basename(folder) if folder in mounted else ""
            out.append(item)
    out.sort(key=lambda i: (i["created"], i["name"]), reverse=True)
    return out


def _proton_filtered(meta: dict, proton_exists: Callable[[str], bool]) -> dict:
    """Eigene Proton-Fassung je Spiel nur, wenn es sie hier gibt (sonst startete das Spiel nicht)."""
    out = {}
    for entry_id, rec in meta.items():
        if isinstance(rec, dict) and isinstance(rec.get("options"), dict) and rec["options"].get("proton"):
            name = rec["options"]["proton"]
            if not (isinstance(name, str) and proton_exists(name)):
                rec = {**rec, "options": {k: v for k, v in rec["options"].items() if k != "proton"}}
        out[entry_id] = rec
    return out


def _parts(backup: Backup) -> dict:
    """Die Teile einer geprüften Sicherung in ihrer Form (fehlende Teile leer)."""
    settings = backup.json("settings.json", {})
    meta = backup.json("library-meta.json", {})
    order = backup.json("home-order.json", {})
    sessions = backup.json("sessions.json", {})
    choices = backup.json("art/choices.json", {})
    images = backup.json("art/images.json", [])
    return {
        "settings": settings if isinstance(settings, dict) else {},
        "meta": meta if isinstance(meta, dict) else {},
        "order": [i for i in (order.get("games") if isinstance(order, dict) else None) or [] if transferable(i)],
        "sessions": (sessions.get("sessions") if isinstance(sessions, dict) else None) or [],
        "choices": choices if isinstance(choices, dict) else {},
        "images": [i for i in images if isinstance(i, dict) and i.get("file") in backup.files
                   and i.get("kind") in ART_KINDS and transferable(i.get("id"))] if isinstance(images, list) else [],
        "browser": backup.json("browser-prefs.json"),
        "terminal": backup.json("terminal-prefs.json"),
    }


def preview(backup: Backup, host: Host) -> dict:
    """Was Übernehmen bewirken würde (Bauplan 4.3 backup.inspect: apply {settings, games, skipped, order,
    art, sessions}, dazu prefs und je Teil, ob es etwas gibt)."""
    p = _parts(backup)
    settings = 0
    for key, value in p["settings"].items():
        try:
            check_setting(key, value, host.proton_exists)
            settings += 1
        except ValueError:
            pass
    games = [i for i, rec in p["meta"].items() if transferable(i) and isinstance(rec, dict) and host.known(i)]
    skipped = len(p["meta"]) - len(games)
    sessions = sum(1 for s in p["sessions"] if isinstance(s, dict) and transferable(s.get("id")) and host.known(s["id"]))
    art = sum(1 for i, rec in p["choices"].items() if transferable(i) and isinstance(rec, dict) and host.known(i)) \
        + sum(1 for i in p["images"] if host.known(i["id"]))
    prefs = int(isinstance(p["browser"], dict)) + int(isinstance(p["terminal"], dict))
    apply = {"settings": settings, "games": len(games), "skipped": skipped, "order": len(p["order"]),
             "art": art, "sessions": sessions, "prefs": prefs}
    available = {"settings": settings, "games": len(games), "order": len(p["order"]), "sessions": sessions,
                 "art": art, "prefs": prefs}
    return {"apply": apply, "parts": [{"id": part, "count": available[part]} for part in PARTS if available[part]]}


# ---------------------------------------------------------------- Übernehmen
def _clean_choice(rec: Any) -> dict | None:
    """Gemerkter Bildtreffer (library/artfetch.py set_choice) in geprüfter Form."""
    if not isinstance(rec, dict) or not isinstance(rec.get("source"), str) or not isinstance(rec.get("ref"), str):
        return None
    if not rec["source"] or not rec["ref"] or len(rec["source"]) > 40 or len(rec["ref"]) > 200:
        return None
    out = {"source": rec["source"], "ref": rec["ref"], "name": str(rec.get("name") or "")[:200]}
    t = rec.get("time")
    out["time"] = int(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else 0
    images = rec.get("images")
    if isinstance(images, dict):
        clean = {}
        for kind, items in images.items():
            if kind in ART_KINDS and isinstance(items, list):
                good = [{"url": i["url"], "width": i.get("width") if isinstance(i.get("width"), int) else None,
                         "height": i.get("height") if isinstance(i.get("height"), int) else None}
                        for i in items[:3] if isinstance(i, dict) and isinstance(i.get("url"), str) and _URL.fullmatch(i["url"])]
                if good:
                    clean[kind] = good
        if clean:
            out["images"] = clean
    return out


def _has_user_image(folder: str, kind: str) -> bool:
    try:
        return any(n.startswith(USER_PREFIX + kind + ".") for n in os.listdir(folder))
    except OSError:
        return False


def _apply_art(p: dict, backup: Backup, host: Host) -> tuple[int, int]:
    applied = skipped = 0
    path = os.path.join(host.art_dir(), "choices.json")
    have = _read_json_file(path)
    have = have if isinstance(have, dict) else {}
    changed = False
    for entry_id, rec in sorted(p["choices"].items()):
        clean = _clean_choice(rec)
        if clean is None or not transferable(entry_id) or not host.known(entry_id):
            skipped += 1
            continue
        if entry_id in have:                                    # hier schon gewählt: das bleibt
            continue
        have[entry_id] = clean
        changed = True
        applied += 1
    if changed:
        if os.path.islink(host.art_dir()) or os.path.islink(path):
            raise BackupError("Der Bilderordner ist ein Verweis; die Bildwahl wurde nicht übernommen.")
        system.write_atomic(path, jsontext.dumps(have, sort_keys=True))
    if not p["images"]:
        return applied, skipped
    if host.set_user_art is None:
        return applied, skipped + len(p["images"])
    work = tempfile.mkdtemp(prefix="couch-backup-")              # eigener Wegwerf-Ordner (E99)
    own: list[str] = []
    try:
        for n, item in enumerate(p["images"]):
            if not host.known(item["id"]):
                skipped += 1
                continue
            folder = host.entry_art_dir(item["id"])
            if folder and _has_user_image(folder, item["kind"]):
                continue                                        # eigenes Bild hier bleibt
            tmp = os.path.join(work, "%d%s" % (n, os.path.splitext(item["file"])[1]))
            with open(tmp, "wb") as f:
                f.write(backup.files[item["file"]])
            own.append(tmp)
            try:
                host.set_user_art(item["id"], item["kind"], tmp)
                applied += 1
            except Exception as e:                              # kein lesbares Bild: übersprungen
                _log.warning("Bild für %s (%s) nicht übernommen: %s", item["id"], item["kind"], e)
                skipped += 1
    finally:
        for f in own:                                           # nur die eigenen Dateien, dann der leere Ordner
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            _log.warning("Wegwerf-Ordner %s ist nicht leer und bleibt liegen", work)
    return applied, skipped


def _apply_prefs(p: dict, host: Host) -> tuple[int, int]:
    applied = skipped = 0
    if isinstance(p["browser"], dict) and host.browser_prefs:
        from .browser.prefs import Prefs
        work = tempfile.mkdtemp(prefix="couch-backup-")
        tmp = os.path.join(work, "prefs.json")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps(p["browser"]))
            prefs = Prefs(tmp)                                  # die eigene Prüfung des Browsers
        finally:
            try:                                                # die eigene Datei, dann der leere Ordner (E99)
                os.unlink(tmp)
            except OSError:
                pass
            try:
                os.rmdir(work)
            except OSError:
                pass
        data = {"zoom": prefs.zoom, "home": prefs.home, "search": prefs.search, "adblock": prefs.adblock,
                "favorites": prefs.favorites}
        system.write_atomic(host.browser_prefs, jsontext.dumps(data, indent=1))
        applied += 1
    elif p["browser"] is not None:
        skipped += 1
    terminal = p["terminal"]
    font = terminal.get("font") if isinstance(terminal, dict) else None
    if isinstance(font, int) and not isinstance(font, bool) and TERMINAL_FONT[0] <= font <= TERMINAL_FONT[1] \
            and host.terminal_prefs:
        system.write_atomic(host.terminal_prefs, json.dumps({"font": font}))
        applied += 1
    elif terminal is not None:
        skipped += 1
    return applied, skipped


def merge_order(saved: list, current: list) -> list:
    """Reihenfolge der Sicherung, danach die Kacheln dieses Rechners, die sie nicht kennt (in ihrer Folge)."""
    first = [i for i in saved if isinstance(i, str) and transferable(i)]
    known = set(first)
    return first + [i for i in current if isinstance(i, str) and i not in known]


def apply(path: str, parts: Any, host: Host, now: float | None = None) -> dict:
    """Übernimmt eine Sicherung. Erst prüfen, dann den eigenen Stand sichern (… vor-übernahme.zip im
    Vorgabeordner), dann die gewählten Teile (Vorgabe alle). BackupError, solange nichts geändert ist."""
    now = time.time() if now is None else now
    backup = read_backup(path)
    wanted = [x for x in (parts if isinstance(parts, list) else PARTS) if x in PARTS]
    if not wanted:
        raise BackupError("Wähle mindestens einen Teil aus.")
    safety = export_zip(host, None, now, note=SAFETY_NOTE)["path"]
    p = _parts(backup)
    applied = {part: 0 for part in wanted}
    skipped = {part: 0 for part in wanted}
    try:
        if "settings" in wanted:
            for key, value in sorted(p["settings"].items()):
                try:
                    value = check_setting(key, value, host.proton_exists)
                except ValueError:
                    skipped["settings"] += 1
                    continue
                host.set_setting(key, value)
                applied["settings"] += 1
        if "games" in wanted:
            result = host.import_meta(_proton_filtered(p["meta"], host.proton_exists))
            applied["games"] = int(result.get("applied") or 0)
            skipped["games"] = len(result.get("skipped") or [])
        if "order" in wanted and p["order"]:
            merged = merge_order(p["order"], host.read_order())
            host.write_order(merged)
            applied["order"] = len(p["order"])
        if "sessions" in wanted:
            result = host.import_sessions(p["sessions"])
            applied["sessions"] = int(result.get("applied") or 0)
            skipped["sessions"] = int(result.get("skipped") or 0)
        if "art" in wanted:
            applied["art"], skipped["art"] = _apply_art(p, backup, host)
        if "prefs" in wanted:
            applied["prefs"], skipped["prefs"] = _apply_prefs(p, host)
    except BackupError as e:
        raise BackupError("%s Dein vorheriger Stand ist gesichert: %s" % (e.message, os.path.basename(safety))) from None
    except Exception as e:
        _log.exception("Übernehmen von %s", path)
        raise BackupError("Das Übernehmen hat mittendrin nicht geklappt (%s). Dein vorheriger Stand ist gesichert: %s"
                          % (type(e).__name__, os.path.basename(safety))) from None
    try:
        host.after_apply()
    except Exception:
        _log.exception("Nach dem Übernehmen")
    later = [str(t) for t in host.running_apps() if t] if "prefs" in wanted and applied.get("prefs") else []
    message = "Übernommen. Einige Teile gelten erst nach dem Neustart des PC5 Launchers."
    if later:
        message += " %s übernimmt die Vorlieben beim nächsten Öffnen." % " und ".join(later)
    _log.info("Sicherung %s übernommen: %s, übersprungen %s", path, applied, skipped)
    return {"ok": True, "applied": applied, "skipped": skipped, "safety": safety, "restart": True, "message": message}
