"""Alle Orte, an denen couch liest und schreibt. Nirgends sonst stehen Pfade ausgeschrieben.

Mit COUCH_TEST_ROOT=<R> liegen alle fünf Laufzeit-Orte unter <R> (config, data, state, run,
cache); so schreibt kein Test in die echten Ordner des Nutzers. Pfadvergleiche laufen immer über
realpath, weil /home auf diesem System ein Verweis auf /var/home ist.
"""
from __future__ import annotations

import os
import pwd
import re
import shutil

_PKG_DIR = os.path.dirname(os.path.realpath(__file__))      # <release>/app/couch


# ---------------------------------------------------------------- Prüfmodus
def test_root() -> str | None:
    return os.environ.get("COUCH_TEST_ROOT") or None


def test_mode() -> bool:
    """Prüfmodus: nichts darf den Rechner verändern (system.run_write protokolliert nur).
    Die eine Definition für das ganze Home; Einstellungs-Backends und Eingabe fragen hier."""
    return bool(test_root()) or os.environ.get("COUCH_TEST") == "1"


def test_mode_incomplete() -> bool:
    """COUCH_TEST=1 ohne COUCH_TEST_ROOT: Prüfmodus, aber alle Orte zeigten auf die echten
    Ordner. Home und Supervisor starten so nicht (main.main, supervisor.main)."""
    return os.environ.get("COUCH_TEST") == "1" and not test_root()


def control_enabled() -> bool:
    """Die Prüfsteuerung nimmt außer power-key nur dann Befehle an."""
    return os.environ.get("COUCH_TEST") == "1"


def fixtures_dir() -> str | None:
    return os.environ.get("COUCH_FIXTURES") or None


# ---------------------------------------------------------------- Grundorte
def home() -> str:
    return os.path.expanduser("~")


def user_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return os.environ.get("USER", "")


def _place(test_sub: str, real: str) -> str:
    root = test_root()
    return os.path.join(root, test_sub) if root else real


def user_place(sub: str) -> str:
    """Ordner im persönlichen Ordner, in den die Konsole schreibt (Downloads, Sicherungen, eigener
    Papierkorb). Im Prüfmodus <COUCH_TEST_ROOT>/home/<sub> (E99): sonst schrieben Tests in die echten
    Ordner des Nutzers, weil home() dem Käfig nicht folgt."""
    return _place(os.path.join("home", sub), os.path.join(home(), sub))


def download_dir() -> str:
    """Download-Ordner der Sitzung (XDG, sonst ~/Downloads) für das Home und den Browser (E59,
    E60); im Prüfmodus der Ordner im Käfig. COUCH_TEST=1 ohne Käfig fragt GLib nicht."""
    if not test_mode():
        try:
            from gi.repository import GLib
            special = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
            if special and os.path.isdir(special):
                return special
        except Exception:
            pass
    return user_place("Downloads")


def config_dir() -> str:
    return _place("config", os.path.join(home(), ".config", "couch"))


def data_dir() -> str:
    return _place("data", os.path.join(home(), ".local", "share", "couch"))


def state_dir() -> str:
    return _place("state", os.path.join(home(), ".local", "state", "couch"))


def run_dir() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return _place("run", os.path.join(runtime, "couch"))


def cache_dir() -> str:
    return _place("cache", os.path.join(home(), ".cache", "couch"))


def ensure_dirs() -> list[str]:
    """Legt die fünf Orte an. Gibt zurück, was sich nicht anlegen ließ (volle Platte o. Ä.):
    Das Home soll auch dann starten, Einstellungen gelten dann nur bis zum Neustart."""
    failed = []
    for d in (config_dir(), data_dir(), state_dir(), run_dir(), cache_dir()):
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError:
            failed.append(d)
    return failed


# ---------------------------------------------------------------- einzelne Dateien
def settings_file() -> str:
    return os.path.join(config_dir(), "settings.json")


def disabled_file() -> str:
    return os.path.join(config_dir(), "disabled")


def start_steam_file() -> str:
    """Weiche Wahl aus den Einstellungen (E26): Die Sitzung beginnt mit Steam Big Picture."""
    return os.path.join(config_dir(), "start-steam")


def library_file() -> str:
    return os.path.join(data_dir(), "library.json")


def library_cache_file() -> str:
    return os.path.join(data_dir(), "library-cache.json")


def art_dir() -> str:
    return os.path.join(data_dir(), "art")


def webkit_data_dir() -> str:
    return os.path.join(data_dir(), "webkit")


def webkit_cache_dir() -> str:
    return os.path.join(cache_dir(), "webkit")


def home_log() -> str:
    return os.path.join(state_dir(), "home.log")


def writes_log() -> str:
    return os.path.join(state_dir(), "writes.log")


def deliberate_exit_file() -> str:
    return os.path.join(state_dir(), "deliberate-exit")


def sink_before_file() -> str:
    """Merkzettel für die Ton-Ausgabe von vor der Sitzung (couch/sinknote.py)."""
    return os.path.join(state_dir(), "sink-before")


def apps_log_dir() -> str:
    return os.path.join(state_dir(), "apps")


def downloads_file() -> str:
    """Downloads (E59, E60): schreibt allein das Home (fetcher.py), der Browser liest."""
    return os.path.join(state_dir(), "downloads.json")


def open_url_dir() -> str:
    """Adressen aus Programmen unter Wine (bin/couch-open-url, E94); das Home zeigt sie im Browser."""
    return os.path.join(state_dir(), "open-url")


def osk_socket(app: str) -> str:
    """Eingang für Text der Bildschirmtastatur an eine eigene App („browser“, „terminal“), am
    Gamescope vorbei: Dort gehen Umschalt und AltGr per XTEST verloren (Wunsch 2026-10-24: „@“ wurde „2“)."""
    return os.path.join(run_dir(), "osk-%s.sock" % app)


def browser_open_file() -> str:
    """Adresse, die der laufende Browser öffnen soll (E68: Kaufen, Anmelden im Store); schreibt das Home."""
    return os.path.join(state_dir(), "browser-open.json")


def download_requests_dir() -> str:
    """Download-Aufträge des Browsers an das Home (E60), je Download eine Datei."""
    return os.path.join(state_dir(), "download-requests")


def browser_downloads_file() -> str:
    """Downloads, die WebKit selbst lädt (blob:, data:, POST; E99): schreibt allein der Browser,
    das Home zeigt sie nur an. Liegt neben downloads.json (der Browser leitet den Ort daraus ab)."""
    return os.path.join(state_dir(), "browser-downloads.json")


def app_log(entry_id: str) -> str:
    """Ausgabe einer gestarteten App. ":" und "/" aus der Bibliotheks-ID werden ersetzt. Legt den
    Ordner an: Installer (GOG, Epic, Launcher) schreiben hinein, auch wenn noch nie eine App lief."""
    try:
        os.makedirs(apps_log_dir(), mode=0o700, exist_ok=True)
    except OSError:
        pass
    return os.path.join(apps_log_dir(), re.sub(r"[^A-Za-z0-9._-]", "_", entry_id) + ".log")


def ready_file() -> str:
    return os.path.join(run_dir(), "ready")


def beat_file() -> str:
    return os.path.join(run_dir(), "beat")


def handover_file() -> str:
    return os.path.join(run_dir(), "handover.json")


def exiting_file() -> str:
    """Das Home endet gewollt: "<code> <pid> <zeit>". Der Supervisor wertet den Code aus, auch
    wenn das Home danach beim Beenden hängt und hart beendet werden muss."""
    return os.path.join(run_dir(), "exiting")


def bt_trial_file() -> str:
    """Probezeit „Bluetooth aus“ läuft: Adapterpfad und Uhrzeit. Liegt die Datei beim Start (oder
    beim Supervisor) noch, hat niemand die Probezeit beendet: Bluetooth wieder einschalten."""
    return os.path.join(state_dir(), "bt-off-trial")


def running_file() -> str:
    return os.path.join(run_dir(), "running.json")


def control_socket() -> str:
    return os.path.join(run_dir(), "control.sock")


# ---------------------------------------------------------------- Programmdateien
def app_dir() -> str:
    """<release>/app"""
    return os.path.dirname(_PKG_DIR)


def release_dir() -> str:
    return os.path.dirname(app_dir())


def web_dir() -> str:
    """Ordner der Oberfläche. Nur im Prüfmodus über COUCH_WEB_DIR umstellbar (Prüfseite)."""
    override = os.environ.get("COUCH_WEB_DIR")
    if override and test_mode():
        return os.path.realpath(override)
    return os.path.join(app_dir(), "web")


def games_disk() -> str:
    """E104: der Spiele-Ort. library.games_disk (absolut), sonst im Bestand (Installation von vor E104, portable.existing)
    /run/media/<nutzer>/Spiele, sonst ~/Games (im Prüfmodus <COUCH_TEST_ROOT>/home/Games). Gibt immer einen
    Pfad zurück, auch wenn er gerade fehlt (Z12: Platte weg = Einträge „nicht verfügbar“, nie gelöscht).
    Liest nur (Einstellung bzw. settings.json, die Marke von portable)."""
    value = _games_disk_setting()
    if value:
        return value
    from . import portable                      # hier: portable baut auf paths auf
    if portable.existing():
        return legacy_games_disk()
    return user_place("Games")


def legacy_games_disk() -> str:
    """Spiele-Ort eines Bestands (bis E104 fest): /run/media/<nutzer>/Spiele, egal ob eingehängt."""
    return os.path.join("/run/media", user_name(), "Spiele")


# Laufende Einstellungen des Home (portable.migrate meldet sie an); ohne sie liest games_disk
# settings.json selbst (Werkzeuge, eigenständige Module), gemerkt nach Zeitstempel und Größe
_config = None
_settings_seen: tuple = (None, None)


def use_config(config) -> None:
    global _config
    _config = config


def _games_disk_setting() -> str:
    global _settings_seen
    value = None
    if _config is not None:
        try:
            value = _config.get("library.games_disk", "")
        except Exception:
            value = None
    else:
        path = settings_file()
        try:
            st = os.stat(path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None
        if key is not None and _settings_seen[0] == key:
            value = _settings_seen[1]
        elif key is not None:
            try:
                import json
                with open(path, encoding="utf-8", errors="replace") as f:
                    data = json.loads(f.read(1024 * 1024))
                value = data.get("library.games_disk") if isinstance(data, dict) else None
            except (OSError, ValueError):
                value = None
            _settings_seen = (key, value)
    if not isinstance(value, str) or not value.strip():
        return ""
    value = os.path.expanduser(value.strip())
    return os.path.normpath(value) if os.path.isabs(value) else ""


# ---------------------------------------------------------------- Wurzeln
def _real_unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for p in items:
        r = os.path.realpath(p)
        if r not in out:
            out.append(r)
    return out


_extra_art_roots: list[str] = []


def add_art_root(path: str) -> bool:
    """Meldet eine weitere Wurzel für couch://art an: die Steam-Installation, wenn sie nicht
    unter ~/.local/share/Steam liegt (die Bibliothek kennt ihren Ort, wiring_library.setup).
    Vor dem Start der Fenster rufen; ui/host.py liest die Liste einmal. Angenommen wird nur ein
    vorhandener Ordner, der tief genug liegt (nie /, das Home selbst oder ein Ordner direkt
    unter der Wurzel)."""
    if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path):
        return False
    real = os.path.realpath(path)
    if real.count(os.sep) < 3 or real == os.path.realpath(home()):
        return False
    if real not in _extra_art_roots:
        _extra_art_roots.append(real)
    return True


def art_roots() -> list[str]:
    """Erlaubnisliste für couch://art/…: nur darunter liefert der Home-Prozess Dateien aus."""
    h = home()
    roots = [
        art_dir(),
        os.path.join(h, ".local", "share", "Steam"),
        os.path.join(h, ".local", "share", "lutris"),
        os.path.join(h, ".cache", "lutris"),
        # E104: Lutris als Flatpak (nur gelesen, library/sources.py): Cover und Banner liegen dort
        os.path.join(h, ".var", "app", "net.lutris.Lutris", "data", "lutris"),
        os.path.join(h, ".var", "app", "net.lutris.Lutris", "cache", "lutris"),
        games_disk(),
        web_dir(),
    ]
    roots += _extra_art_roots
    fx = fixtures_dir()
    if fx and test_mode():
        roots.append(fx)
    return _real_unique(roots)


def browse_roots() -> list[str]:
    """Wurzeln für die Dateiauswahl des Archiv-Installers: nur schon Eingehängtes.

    Im Prüfmodus nie die echten Ordner des Nutzers, sondern COUCH_BROWSE_ROOTS (mit ":" getrennt) oder
    <COUCH_FIXTURES>/browse.
    """
    if test_mode():
        listed = [p for p in os.environ.get("COUCH_BROWSE_ROOTS", "").split(":") if p]
        fx = fixtures_dir()
        if not listed and fx:
            listed = [os.path.join(fx, "browse")]
        return [p for p in _real_unique(listed) if os.path.isdir(p)]
    # E104: der Download-Ordner nach XDG; Laufwerke auch direkt unter /run/media und unter /media/<nutzer>
    roots = [user_dir("DOWNLOAD")] + media_roots()
    return [p for p in _real_unique(roots) if os.path.isdir(p)]


# Ordner des Datei-Explorers (files.roots), aus denen auch der Installer Archive annimmt (E99).
# Zuletzt der persönliche Ordner selbst: Der Explorer bietet für jedes Archiv in seinen Orten
# „Installieren“ an, auch in ~ und ~/Games. Versteckte Ordner bleiben in der Dateiauswahl und
# bei der Prüfung ausgeschlossen (installer/browse.py), ~/.local und ~/.steam also nie.
# E104: Arten der XDG-Benutzerordner (user_dir) statt fester deutscher Namen
INSTALL_HOME_KINDS = ("DOCUMENTS", "PICTURES", "VIDEOS", "MUSIC")


def install_roots() -> list[str]:
    """Wurzeln des Archiv-Installers (Prüfung und Dateiauswahl): browse_roots und dazu die Orte
    des Explorers unter ~ samt ~ selbst (E99: ein Archiv in Dokumente oder direkt in ~ hieß sonst
    „außerhalb der erlaubten Orte“). Im Prüfmodus genau browse_roots, nie die Ordner des Nutzers."""
    roots = browse_roots()
    if test_mode():
        return roots
    first = [user_dir(kind) for kind in ("DOWNLOAD",) + INSTALL_HOME_KINDS]
    return [p for p in _real_unique(first + [home()] + roots) if os.path.isdir(p)]


def is_under(path: str, roots: list[str]) -> bool:
    """Liegt path (nach Auflösen aller Verweise) in einer der Wurzeln?"""
    full = os.path.realpath(path)
    for root in roots:
        r = os.path.realpath(root)
        if full == r or full.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


# ---------------------------------------------------------------- Sitzungsumgebung
def session_env() -> dict[str, str]:
    """Die unveränderte Umgebung der Sitzung (für gestartete Apps und für Steam).

    Der Supervisor legt sie als `env -0` in die Datei aus COUCH_SESSION_ENV, weil er das Home
    selbst mit bereinigter Umgebung startet. Ohne Datei gilt die eigene Umgebung.
    """
    path = os.environ.get("COUCH_SESSION_ENV")
    if path:
        try:
            with open(path, "rb") as f:
                raw = f.read()
            env: dict[str, str] = {}
            for item in raw.split(b"\0"):
                if b"=" in item:
                    k, _, v = item.partition(b"=")
                    env[k.decode("utf-8", "surrogateescape")] = v.decode("utf-8", "surrogateescape")
            if env:
                return env
        except OSError:
            pass
    return dict(os.environ)


# ---------------------------------------------------------------- E104: Benutzerordner und Werkzeuge (Teil P0)
# Arten der XDG-Benutzerordner (~/.config/user-dirs.dirs) und ihre Namen, falls dort nichts steht:
# zuerst der deutsche Name, wenn es den Ordner gibt, sonst der englische (Vorgabe von xdg-user-dirs)
USER_DIR_NAMES = {
    "DOWNLOAD": ("Downloads", "Downloads"),
    "DOCUMENTS": ("Dokumente", "Documents"),
    "PICTURES": ("Bilder", "Pictures"),
    "VIDEOS": ("Videos", "Videos"),
    "MUSIC": ("Musik", "Music"),
}
_USER_DIR_LINE = re.compile(r'^\s*XDG_([A-Z]+)_DIR\s*=\s*"([^"]*)"\s*$')


def _user_dirs_file(base: str) -> str:
    """user-dirs.dirs des Nutzers; im Prüfmodus nur die im Käfig (<COUCH_TEST_ROOT>/home/.config)."""
    if test_root():
        return os.path.join(base, ".config", "user-dirs.dirs")
    conf = os.environ.get("XDG_CONFIG_HOME") or os.path.join(base, ".config")
    return os.path.join(conf, "user-dirs.dirs")


def _read_user_dirs(path: str, base: str) -> dict[str, str]:
    """XDG_<ART>_DIR="$HOME/…" bzw. "/…" (nur diese beiden Formen erlaubt xdg-user-dirs). Ein Eintrag,
    der auf den persönlichen Ordner selbst zeigt, heißt „abgeschaltet“ und zählt nicht."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(64 * 1024).splitlines()
    except OSError:
        return out
    for line in lines:
        m = _USER_DIR_LINE.match(line)
        if not m:
            continue
        kind, value = m.group(1), m.group(2)
        if value == "$HOME" or value.startswith("$HOME/"):
            value = base + value[len("$HOME"):]
        elif not value.startswith("/"):
            continue
        value = os.path.normpath(value)
        if value.rstrip(os.sep) and os.path.realpath(value) != os.path.realpath(base):
            out[kind] = value
    return out


def user_dir(kind: str) -> str:
    """Benutzerordner einer Art ("DOWNLOAD", "DOCUMENTS", "PICTURES", "VIDEOS", "MUSIC"; E104 statt
    fester deutscher Namen): aus user-dirs.dirs, sonst der deutsche Name, falls es den Ordner gibt,
    sonst der englische. Gibt immer einen Pfad zurück, auch wenn er fehlt; legt nichts an.
    Im Prüfmodus unter <COUCH_TEST_ROOT>/home (liest nie die echte Datei des Nutzers)."""
    kind = str(kind or "").upper()
    if kind not in USER_DIR_NAMES:
        raise ValueError("unbekannte Art eines Benutzerordners: %r" % kind)
    root = test_root()
    base = os.path.join(root, "home") if root else home()
    configured = _read_user_dirs(_user_dirs_file(base), base).get(kind)
    if configured:
        return configured
    german, english = USER_DIR_NAMES[kind]
    if os.path.isdir(os.path.join(base, german)):
        return os.path.join(base, german)
    return os.path.join(base, english)


def tool(name: str) -> str | None:
    """Ein Hilfsprogramm (E104/E105): aus dem PATH, sonst die eigene Kopie unter <data>/tools/<name>
    bzw. für umu-run <data>/tools/umu/umu-run (ZIP-App des Assistenten, E105). Voller Pfad oder None."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        return None
    found = shutil.which(name)
    if found:
        return found
    if name == "umu-run":
        own = os.path.join(data_dir(), "tools", "umu", "umu-run")
    else:
        own = os.path.join(data_dir(), "tools", name)
    if os.path.isfile(own) and os.access(own, os.X_OK):
        return own
    return None


# ---------------------------------------------------------------- E104: Benutzerordner und Laufwerke (Teil P3)
def user_dir_at(kind: str, base: str | None = None, missing: str = "en") -> str:
    """Wie user_dir, aber für einen ausdrücklich genannten persönlichen Ordner base (installer/browse,
    Tests; gelesen wird dann nur <base>/.config/user-dirs.dirs) und mit Wahl des Namens, wenn weder
    user-dirs.dirs etwas nennt noch einer der beiden Ordner da ist: missing="en" wie user_dir, "de"
    für Ordner, die die Konsole selbst anlegt (Oberfläche deutsch, z. B. Bildschirmfotos)."""
    if base is None and missing == "en":
        return user_dir(kind)
    kind = str(kind or "").upper()
    if kind not in USER_DIR_NAMES:
        raise ValueError("unbekannte Art eines Benutzerordners: %r" % kind)
    if base is None:
        root = test_root()
        base = os.path.join(root, "home") if root else home()
        conf = _user_dirs_file(base)
    else:
        conf = os.path.join(base, ".config", "user-dirs.dirs")
    configured = _read_user_dirs(conf, base).get(kind)
    if configured:
        return configured
    german, english = USER_DIR_NAMES[kind]
    for name in (german, english):
        if os.path.isdir(os.path.join(base, name)):
            return os.path.join(base, name)
    return os.path.join(base, german if missing == "de" else english)


def media_roots(run_media: str = "/run/media", media: str = "/media", user: str | None = None,
                ismount=os.path.ismount) -> list[str]:
    """Eingehängte Laufwerke (E104), nur was wirklich eingehängt ist, nie selbst einhängen:
    /run/media/<nutzer>/<name> (udisks), direkt /run/media/<name> (ältere SteamOS-Fassungen:
    /run/media/mmcblk0p1, unbestätigt) und /media/<nutzer>/<name> (Debian, Ubuntu). Verweise und
    versteckte Namen zählen nicht. Liest nur (listdir, ismount)."""
    user = user if user is not None else user_name()
    out: list[str] = []
    bases = [os.path.join(run_media, user)] if user else []
    bases += [run_media] + ([os.path.join(media, user)] if user else [])
    for base in bases:
        try:
            names = sorted(os.listdir(base))   # Reihenfolge wie bisher in browse_roots und files.roots
        except OSError:
            continue
        for name in names:
            p = os.path.join(base, name)
            if name.startswith(".") or os.path.islink(p) or p in out:
                continue
            if ismount(p):
                out.append(p)
    return out
