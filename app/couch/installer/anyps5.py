"""AnyPS5 im Installer (E150, Wunsch 2026-11-02: „Baue AnyPS5 in den pc5-launcher ein.“).

AnyPS5 (github.com/boykopovar/AnyPS5, GPL-2.0, nicht Teil des Pakets) wandelt die Programmdatei eines
Konsolenspiels (eboot.bin samt den Modulen in sce_module/) mit dem relinker in ein Linux-Programm um und
bringt die Systembibliotheken dafür mit (prx-linux). Der Installer macht daraus einen Spielordner mit Kachel.

Werkzeug    <data>/tools/anyps5/<tag>/relinker und <tag>/libs/ (aus prx-linux-<tag>.tar.gz), dazu der Verweis
            <data>/tools/anyps5/current auf die zuletzt geladene Fassung. Ältere Fassungen bleiben liegen:
            Jeder Spielordner verweist mit libs auf genau die Fassung, mit der er umgewandelt wurde.
Laden       Ausnahme von „nichts aus dem Netz laden und ausführen“ (Wunsch 2026-11-02, Frage „Wie sollen sie auf
            den Rechner kommen?“: „Ausnahme: Launcher lädt sie“): nur aus dem neuesten Release von REPO, nur die
            Dateien relinker-<tag> und prx-linux-<tag>.tar.gz, nur per https von github.com, geprüft mit der
            sha256 aus der GitHub-API (Feld digest). Ohne passende Prüfsumme wird nichts abgelegt.
Entpacker   E151 (Wunsch 2026-11-02: „baue github pkg to anyps5 in meinen launcher“): pkg-to-anyps5
            (github.com/awake-devel/pkg-to-anyps5, GPL-3.0, nicht Teil des Pakets) nach
            <data>/tools/pkg-to-anyps5/<tag>/pkg-to-anyps5, Regeln wie oben, nur die Datei
            pkg-to-anyps5-<tag>-linux-x86_64.tar.gz und daraus nur das Programm. Es enthält keine Schlüssel und
            liest nur Unverschlüsseltes; verschlüsselte Pakete und Programmdateien lehnt es ab.
Quelle      ein Ordner mit eboot.bin und sce_sys/param.json (is_source). Er wird nur gelesen und bleibt, wo er
            ist: app0/ im Spielordner verweist auf seine Einträge. Ein gelöschtes Spiel löscht nie die Quelle
            (trash._remove_tree entfernt Verweise, folgt ihnen aber nie).
            Oder eine .pkg-Datei (is_pkg, E151): Sie wird nur gelesen, ihr Inhalt landet im Spielordner.
Spielordner (im Zwischenordner, danach wie jedes Archiv-Spiel per rename an den Zielort):
            source/             vom Entpacker: eboot.bin und die Module als ELF, Eingabe des relinkers (bleibt für
                                ein späteres erneutes Umwandeln)
            app.elf             vom relinker (auf Intel-Prozessoren mit --to-intel)
            app0/<modulordner>  die vom relinker umgewandelten Module (*.guest.prx)
            app0/<name>         Ordner: Verweise auf alle übrigen Einträge der Quelle (eboot.bin, sce_sys, Spieldaten)
                                Paket: die Spieldateien selbst, vom Entpacker
            libs                Verweis auf <data>/tools/anyps5/<tag>/libs
Gestartet wird app.elf wie ein Linux-Spiel (launch/native.py setzt dazu ANYPS5_* aus launch_env).

Prüfmodus: geladen wird nur von COUCH_ANYPS5_API bzw. COUCH_PKG2ANY_API auf 127.0.0.1 (Attrappe); ohne sie lädt nichts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .. import paths
from . import human_size

REPO = "boykopovar/AnyPS5"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
PAGE_URL = "https://github.com/%s" % REPO
TEST_ENV = "COUCH_ANYPS5_API"
CURRENT = "current"
RELINKER, LIBS = "relinker", "libs"
EXE = "app.elf"
APP0 = "app0"
MODULE_DIRS = ("sce_module", "sce_modules", "prx")     # Modulordner der Quelle: deren Inhalt liefert der relinker
RUNTIME_MODULE_DIRS = ("Media/Plugins",)                # lädt das Spiel erst zur Laufzeit (Unity): der relinker nur auf Wunsch
SOURCE = "source"                  # Ausgabe des Entpackers: Eingabe des relinkers
TAG_RE = re.compile(r"^v\d{1,4}(\.\d{1,4}){1,3}$")
DIGEST_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
CHUNK = 256 * 1024
API_MAX = 4 << 20
PARAM_MAX = 1 << 20
RELINKER_MAX = 64 << 20
LIBS_MAX = 512 << 20               # gepackt (v0.1.4: 96 MiB)
UNPACKED_MAX = 2 << 30             # entpackt (v0.1.4: 264 MiB)
MAX_MEMBERS = 5000
# Befehle des Zen-2-Prozessors, die umgewandelte Spiele ungeprüft nutzen (AnyPS5 USAGE); LZCNT heißt in
# /proc/cpuinfo „abm“
CPU_FLAGS = {"avx2": "AVX2", "fma": "FMA", "f16c": "F16C", "bmi1": "BMI1", "bmi2": "BMI2", "abm": "LZCNT",
             "movbe": "MOVBE"}
LANGUAGES = {"de": "4", "en": "1"}  # ANYPS5_LANGUAGE: 4 Deutsch, 1 Englisch (USA)

# E151: Entpacker pkg-to-anyps5 und die Kopfdaten eines Pakets (Aufbau wie in dessen src/fih.rs)
UNPACKER = "pkg-to-anyps5"
UNPACKER_REPO = "awake-devel/pkg-to-anyps5"
UNPACKER_API_URL = "https://api.github.com/repos/%s/releases/latest" % UNPACKER_REPO
UNPACKER_TEST_ENV = "COUCH_PKG2ANY_API"
UNPACKER_ASSET = "pkg-to-anyps5-%s-linux-x86_64.tar.gz"
UNPACKER_MAX = 32 << 20            # gepackt (v0.2.5: 0,4 MiB)
LIST_TIMEOUT = 120
PKG_SUFFIX = ".pkg"
FIH_MAGIC, CNT_MAGIC = b"\x7fFIH", b"\x7fCNT"
FIH_SIZE, CNT_HEADER, CNT_ENTRY = 0x100, 0x80, 0x20
CNT_PARAM, CNT_ENCRYPTED = 0x2000, 0x8000_0000
CNT_MAX_ENTRIES = 4096
CONTENT_TITLE_RE = re.compile(r"-([A-Z]{4}\d{5})_")
UNPACK_RE = re.compile(r"^\s*(unwrapping executables|copying app0)\s+(\d+(?:\.\d+)?)%")
PKG_ESTIMATE = (3, 2)              # ohne Entpacker geschätzt: entpackt anderthalbmal so groß wie das Paket
PKG_REFUSED = {
    "retail": "Dieses Paket ist verschlüsselt (Retail-Fassung). Gelesen werden nur unverschlüsselte Debug-Pakete.",
    "other": "Diese Datei ist kein Paket für AnyPS5.",
    "unreadable": "Diese Datei lässt sich nicht lesen.",
}

_install_lock = threading.Lock()   # ein Download zur Zeit (Einstellungen und Installer)


class AnyError(Exception):
    """Ein deutscher Satz für die Oberfläche und ein festes Kürzel."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.message, self.code = message, code


# ---------------------------------------------------------------- Werkzeug

def tools_dir() -> str:
    return os.path.join(paths.data_dir(), "tools", "anyps5")


def version_dir(tag: str, root: str | None = None) -> str:
    if not TAG_RE.match(str(tag or "")):
        raise AnyError("Unbekannte Fassung von AnyPS5.", "bad-tag")
    return os.path.join(root or tools_dir(), tag)


def check_dir(path: str) -> dict | None:
    """Eine Fassung, wenn darin ein ausführbarer relinker und der Ordner libs liegen, sonst None. Nur lesend."""
    tag = os.path.basename(path)
    relinker, libs = os.path.join(path, RELINKER), os.path.join(path, LIBS)
    try:
        st = os.stat(relinker)
    except OSError:
        return None
    if not TAG_RE.match(tag) or not stat.S_ISREG(st.st_mode) or not os.access(relinker, os.X_OK) \
            or not os.path.isdir(libs):
        return None
    return {"tag": tag, "dir": path, "relinker": relinker, "libs": libs}


def installed(root: str | None = None) -> dict | None:
    """Die aktuelle Fassung ({tag, dir, relinker, libs}) oder None. Nur lesend."""
    path = _current(root or tools_dir())
    return check_dir(path) if path else None


def _current(root: str) -> str | None:
    """Der Ordner, auf den der Verweis current zeigt. Nur lesend."""
    try:
        target = os.readlink(os.path.join(root, CURRENT))
    except OSError:
        return None
    if "/" in target:
        return None                     # der eigene Verweis nennt nur den Namen des Ordners daneben
    return os.path.join(root, target)


def api_url(env: str = TEST_ENV, live: str = API_URL) -> str | None:
    """Woher die Liste kommt. Im Prüfmodus nur eine lokale Adresse aus env."""
    if paths.test_mode():
        url = os.environ.get(env) or ""
        return url if _local(url) else None
    return live


def _local(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost")
    except ValueError:
        return False


def _allowed_download(url, repo: str = REPO) -> bool:
    if not isinstance(url, str):
        return False
    if paths.test_mode():
        return _local(url)
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "github.com" and not parts.username \
        and parts.path.startswith("/%s/releases/download/" % repo)


def _open(url: str, start: int = 0):
    headers = {"User-Agent": "PC5-Launcher"}
    if start:
        headers["Range"] = "bytes=%d-" % start
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)   # noqa: S310


def parse_release(data) -> dict:
    """Die Antwort der GitHub-API -> {tag, relinker: {name, url, size, sha256}, libs: {…}, bytes}. Rein.
    Wirft AnyError, wenn eine der beiden Dateien, ihre Größe oder ihre Prüfsumme fehlt."""
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise AnyError("GitHub nennt keine gültige Fassung von AnyPS5.", "bad-release")
    wanted = {RELINKER: ("relinker-%s" % tag, RELINKER_MAX), LIBS: ("prx-linux-%s.tar.gz" % tag, LIBS_MAX)}
    out = {"tag": tag}
    for key, (name, limit) in wanted.items():
        out[key] = _asset(data, tag, name, limit, REPO, "AnyPS5")
    out["bytes"] = out[RELINKER]["size"] + out[LIBS]["size"]
    return out


def _asset(data: dict, tag: str, name: str, limit: int, repo: str, tool: str) -> dict:
    """Eine Datei eines Release: {name, url, size, sha256}. Wirft AnyError, wenn Datei, Größe oder Prüfsumme fehlen."""
    asset = next((a for a in data.get("assets") or [] if isinstance(a, dict) and a.get("name") == name), None)
    if asset is None:
        raise AnyError("Im Release %s von %s fehlt die Datei %s." % (tag, tool, name), "bad-release")
    size, url = asset.get("size"), asset.get("browser_download_url")
    digest = DIGEST_RE.match(str(asset.get("digest") or ""))
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= limit:
        raise AnyError("GitHub nennt für %s eine unerwartete Größe." % name, "bad-release")
    if digest is None:
        raise AnyError("GitHub nennt für %s keine Prüfsumme. Ohne sie lädt der Launcher nichts." % name, "no-digest")
    if not _allowed_download(url, repo):
        raise AnyError("Die Adresse von %s zeigt nicht auf GitHub." % name, "bad-release")
    return {"name": name, "url": url, "size": size, "sha256": digest.group(1)}


def fetch_latest(get: Callable | None = None) -> dict:
    """Die neueste Fassung laut GitHub (parse_release). Blockierend (Netz). Wirft AnyError."""
    return parse_release(_release_json(api_url(), get))


def _release_json(url: str | None, get: Callable | None) -> dict:
    """Die Antwort der GitHub-API. Blockierend (Netz). Wirft AnyError."""
    if not url:
        raise AnyError("Im Prüfmodus lädt der Launcher nichts aus dem Netz.", "no-network")
    try:
        with (get or _open)(url) as resp:
            raw = resp.read(API_MAX + 1)
    except urllib.error.HTTPError as e:
        raise AnyError("GitHub antwortet nicht wie erwartet (HTTP %d)." % e.code, "http") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AnyError("GitHub ist nicht erreichbar. Besteht eine Verbindung zum Internet?", "no-network") from e
    if len(raw) > API_MAX:
        raise AnyError("Die Antwort von GitHub ist zu groß.", "bad-release")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise AnyError("Die Antwort von GitHub ist nicht lesbar.", "bad-release") from e
    return data


def _download(item: dict, dest: str, get, tick, stop, repo: str = REPO) -> None:
    """Eine Datei nach dest (über dest.part, Fortsetzen per Range). tick(geladen) nach jedem Stück."""
    url, size = item["url"], item["size"]
    if not _allowed_download(url, repo):
        raise AnyError("Die Adresse von %s zeigt nicht auf GitHub." % item["name"], "bad-release")
    part = dest + ".part"
    try:
        have = os.lstat(part).st_size if stat.S_ISREG(os.lstat(part).st_mode) else 0
    except OSError:
        have = 0
    if have > size:
        os.unlink(part)
        have = 0
    try:
        resp = get(url, have)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have == size:
            os.replace(part, dest)
            return
        raise AnyError("GitHub lieferte %s nicht (HTTP %d)." % (item["name"], e.code), "http") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AnyError("GitHub ist nicht erreichbar. Besteht eine Verbindung zum Internet?", "no-network") from e
    with resp:
        resumed = bool(have) and getattr(resp, "status", 200) == 206
        if not resumed:
            have = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_APPEND if resumed else os.O_TRUNC)
        with os.fdopen(os.open(part, flags, 0o600), "wb") as out:
            while True:
                if stop is not None and stop():
                    raise AnyError("Abgebrochen.", "cancelled")
                try:
                    chunk = resp.read(CHUNK)
                except OSError as e:
                    raise AnyError("Der Download brach ab. Ein neuer Versuch setzt fort.", "interrupted") from e
                if not chunk:
                    break
                have += len(chunk)
                if have > size:
                    raise AnyError("%s kam größer an als erwartet." % item["name"], "damaged")
                out.write(chunk)
                if tick:
                    tick(len(chunk))
    if have < size:
        raise AnyError("Der Download ist unvollständig. Ein neuer Versuch setzt fort.", "interrupted")
    os.replace(part, dest)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_libs(archive: str, staging: str) -> None:
    """prx-linux-<tag>.tar.gz nach staging/libs: nur Ordner und gewöhnliche Dateien unter libs/, Filter „data“."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS or sum(m.size for m in members) > UNPACKED_MAX:
            raise AnyError("Das Archiv von AnyPS5 ist größer als erwartet.", "damaged")
        files = 0
        for m in members:
            name = os.path.normpath(m.name)
            if name == LIBS and m.isdir():
                continue
            if not name.startswith(LIBS + "/") or "/../" in "/" + name + "/" or not (m.isdir() or m.isfile()):
                raise AnyError("Das Archiv von AnyPS5 enthält unerwartete Einträge.", "damaged")
            files += m.isfile()
        if not files:
            raise AnyError("Im Archiv von AnyPS5 liegen keine Bibliotheken.", "damaged")
        tar.extractall(staging, filter="data")


def _switch_current(root: str, tag: str) -> None:
    """current zeigt danach auf tag (ein neuer Verweis, dann rename: nie ohne gültigen Verweis)."""
    link = os.path.join(root, CURRENT)
    if os.path.lexists(link) and not os.path.islink(link):
        raise AnyError("%s gibt es schon und ist kein Verweis." % link, "exists")
    tmp = os.path.join(root, ".%s-%d" % (CURRENT, os.getpid()))
    if os.path.islink(tmp):
        os.unlink(tmp)                  # nur der eigene Verweis eines früheren Laufs
    os.symlink(tag, tmp)
    os.replace(tmp, link)


def install(release: dict, get: Callable | None = None, progress: Callable[[int, int], None] | None = None,
            stop: Callable[[], bool] | None = None, root: str | None = None) -> dict:
    """Lädt die Fassung release (aus fetch_latest), prüft sie und macht sie zur aktuellen. Blockierend (Netz,
    Platte). progress(geladen, gesamt) in Bytes; stop() -> True bricht ab. Liefert installed(); wirft AnyError.
    Eine schon vollständige Fassung wird nicht neu geladen. Abgebrochene Downloads bleiben als .part liegen."""
    root = root or tools_dir()
    tag = release["tag"]
    with _install_lock:
        final = version_dir(tag, root)
        have = check_dir(final)
        if have is None:
            if os.path.lexists(final):
                raise AnyError("Der Ordner %s gibt es schon, aber ohne vollständiges AnyPS5. Er bleibt, wie er ist."
                               % final, "exists")
            _fetch_into(release, final, root, get or _open, progress, stop)
            have = check_dir(final)
            if have is None:
                raise AnyError("AnyPS5 ist abgelegt, lässt sich aber nicht finden.", "failed")
        try:
            _switch_current(root, tag)
        except OSError as e:
            raise AnyError("AnyPS5 ließ sich nicht einrichten: %s" % (e.strerror or e), "failed") from e
        return have


def _fetch_into(release: dict, final: str, root: str, get, progress, stop) -> None:
    os.makedirs(root, mode=0o755, exist_ok=True)
    total, done = release["bytes"], [0]
    downloads = os.path.join(root, ".laden-" + release["tag"])     # Teildateien, bleiben fürs Fortsetzen
    staging = os.path.join(root, ".neu-%s-%d" % (release["tag"], os.getpid()))

    def tick(n: int) -> None:
        done[0] += n
        if progress:
            progress(min(done[0], total), total)

    try:
        os.makedirs(downloads, mode=0o700, exist_ok=True)
        files = {}
        for key in (RELINKER, LIBS):
            item = release[key]
            dest = os.path.join(downloads, item["name"])
            _download(item, dest, get, tick, stop)
            if _sha256(dest) != item["sha256"]:
                os.unlink(dest)
                raise AnyError("%s kam beschädigt an (Prüfsumme passt nicht). Versuche es noch einmal." % item["name"],
                               "damaged")
            files[key] = dest
        os.mkdir(staging, 0o755)
        _extract_libs(files[LIBS], staging)
        os.rename(files[RELINKER], os.path.join(staging, RELINKER))
        os.chmod(os.path.join(staging, RELINKER), 0o755)
        os.rename(staging, final)
        staging = ""
        os.unlink(files[LIBS])
        os.rmdir(downloads)
    except AnyError:
        raise
    except (OSError, tarfile.TarError) as e:
        raise AnyError("AnyPS5 ließ sich nicht ablegen: %s" % (getattr(e, "strerror", None) or e), "failed") from e
    finally:
        if staging and os.path.isdir(staging):
            _drop_staging(staging)


def _drop_staging(staging: str) -> None:
    """Eigenen, abgebrochenen Zwischenordner leeren (kein rekursives Löschen fremder Dinge, SAFETY 13): nur
    Dateien und Ordner aus dem eigenen Entpacken, tiefste zuerst. Verweise gibt es darin nicht (_extract_libs)."""
    if not os.path.basename(staging).startswith(".neu-"):
        return
    for dirpath, dirnames, filenames in os.walk(staging, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
            except OSError:
                pass
        for name in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, name))
            except OSError:
                pass
    try:
        os.rmdir(staging)
    except OSError:
        pass


# ---------------------------------------------------------------- Entpacker pkg-to-anyps5 (E151)

def unpacker_dir() -> str:
    return os.path.join(paths.data_dir(), "tools", UNPACKER)


def _check_unpacker(path: str) -> dict | None:
    """Eine Fassung des Entpackers ({tag, dir, exe}), wenn darin das ausführbare Programm liegt. Nur lesend."""
    tag, exe = os.path.basename(path), os.path.join(path, UNPACKER)
    try:
        st = os.stat(exe)
    except OSError:
        return None
    if not TAG_RE.match(tag) or not stat.S_ISREG(st.st_mode) or not os.access(exe, os.X_OK):
        return None
    return {"tag": tag, "dir": path, "exe": exe}


def unpacker(root: str | None = None) -> dict | None:
    """Die aktuelle Fassung des Entpackers oder None. Nur lesend."""
    path = _current(root or unpacker_dir())
    return _check_unpacker(path) if path else None


def parse_unpacker_release(data) -> dict:
    """Die Antwort der GitHub-API -> {tag, archive: {name, url, size, sha256}, bytes}. Rein. Wirft AnyError."""
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise AnyError("GitHub nennt keine gültige Fassung von %s." % UNPACKER, "bad-release")
    archive = _asset(data, tag, UNPACKER_ASSET % tag, UNPACKER_MAX, UNPACKER_REPO, UNPACKER)
    return {"tag": tag, "archive": archive, "bytes": archive["size"]}


def fetch_unpacker(get: Callable | None = None) -> dict:
    """Die neueste Fassung des Entpackers laut GitHub. Blockierend (Netz). Wirft AnyError."""
    return parse_unpacker_release(_release_json(api_url(UNPACKER_TEST_ENV, UNPACKER_API_URL), get))


def install_unpacker(release: dict, get: Callable | None = None, progress: Callable[[int, int], None] | None = None,
                     stop: Callable[[], bool] | None = None, root: str | None = None) -> dict:
    """Wie install(), für den Entpacker. Liefert {tag, dir, exe}; wirft AnyError."""
    root = root or unpacker_dir()
    tag = release["tag"]
    with _install_lock:
        final = version_dir(tag, root)
        have = _check_unpacker(final)
        if have is None:
            if os.path.lexists(final):
                raise AnyError("Der Ordner %s gibt es schon, aber ohne vollständiges %s. Er bleibt, wie er ist."
                               % (final, UNPACKER), "exists")
            _fetch_unpacker_into(release, final, root, get or _open, progress, stop)
            have = _check_unpacker(final)
            if have is None:
                raise AnyError("%s ist abgelegt, lässt sich aber nicht finden." % UNPACKER, "failed")
        try:
            _switch_current(root, tag)
        except OSError as e:
            raise AnyError("%s ließ sich nicht einrichten: %s" % (UNPACKER, e.strerror or e), "failed") from e
        return have


def _fetch_unpacker_into(release: dict, final: str, root: str, get, progress, stop) -> None:
    item, done = release["archive"], [0]
    downloads = os.path.join(root, ".laden-" + release["tag"])     # Teildatei, bleibt fürs Fortsetzen
    staging = os.path.join(root, ".neu-%s-%d" % (release["tag"], os.getpid()))

    def tick(n: int) -> None:
        done[0] += n
        if progress:
            progress(min(done[0], item["size"]), item["size"])

    try:
        os.makedirs(downloads, mode=0o700, exist_ok=True)
        dest = os.path.join(downloads, item["name"])
        _download(item, dest, get, tick, stop, UNPACKER_REPO)
        if _sha256(dest) != item["sha256"]:
            os.unlink(dest)
            raise AnyError("%s kam beschädigt an (Prüfsumme passt nicht). Versuche es noch einmal." % item["name"],
                           "damaged")
        os.mkdir(staging, 0o755)
        _extract_unpacker(dest, staging)
        os.rename(staging, final)
        staging = ""
        os.unlink(dest)
        os.rmdir(downloads)
    except AnyError:
        raise
    except (OSError, tarfile.TarError) as e:
        raise AnyError("%s ließ sich nicht ablegen: %s" % (UNPACKER, getattr(e, "strerror", None) or e), "failed") from e
    finally:
        if staging and os.path.isdir(staging):
            _drop_staging(staging)


def _extract_unpacker(archive: str, staging: str) -> None:
    """Aus dem Archiv nur das Programm, unter festem Namen (der Pfad im Archiv zählt nicht)."""
    with tarfile.open(archive, "r:gz") as tar:
        hits = [m for m in tar.getmembers() if m.isfile() and os.path.basename(m.name) == UNPACKER]
        if len(hits) != 1 or hits[0].size > UNPACKER_MAX * 4:
            raise AnyError("Im Archiv von %s liegt das Programm nicht wie erwartet." % UNPACKER, "damaged")
        out = os.path.join(staging, UNPACKER)
        with tar.extractfile(hits[0]) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    os.chmod(out, 0o755)


def is_pkg(path) -> bool:
    """Eine Paketdatei (.pkg)? Der Inhalt zählt erst in read_pkg. Nur lesend (ein stat)."""
    return isinstance(path, str) and os.path.isabs(path) and path.lower().endswith(PKG_SUFFIX) and os.path.isfile(path)


def is_input(path) -> bool:
    """Etwas, das der Installer an AnyPS5 geben kann: ein Spieleordner oder eine Paketdatei?"""
    return is_source(path) or is_pkg(path)


def _be32(raw: bytes, at: int) -> int:
    return int.from_bytes(raw[at:at + 4], "big")


def read_pkg(path: str, lang: str = "de") -> dict:
    """Blick in den Kopf eines Pakets: {"state": "debug"|"retail"|"other"|"unreadable", "title", "title_id"}.
    Nur lesend, wenige Bytes. Titel und Kennung stehen unverschlüsselt im eingebetteten CNT (param.json)."""
    out = {"state": "other", "title": "", "title_id": ""}
    try:
        with open(path, "rb") as fh:
            head = fh.read(FIH_SIZE)
            if len(head) < FIH_SIZE or head[:4] != FIH_MAGIC:
                return out
            # Byte 5: 0 = Debug oder selbst signiert; alles andere ist eine verschlüsselte Retail-Fassung
            out["state"] = "debug" if head[5] == 0 else "retail"
            if out["state"] == "debug":
                try:
                    raw, content_id = _cnt_param(fh, int.from_bytes(head[0x58:0x60], "little"))
                except (OverflowError, ValueError):    # Unsinn im Kopf, zu groß für seek(): ob es taugt, sagt der Entpacker
                    raw, content_id = b"", ""
                out.update(_param_info(raw, lang))
                found = CONTENT_TITLE_RE.search(content_id)
                if not out["title_id"] and found:
                    out["title_id"] = found.group(1)
    except OSError:
        out["state"] = "unreadable"
    return out


def _cnt_param(fh, offset: int) -> tuple:
    """(Inhalt von param.json oder b"", Content-ID) aus dem CNT ab offset. Unstimmiges gilt als „steht nicht drin“:
    Ob das Paket taugt, entscheidet der Entpacker."""
    fh.seek(offset)
    header = fh.read(CNT_HEADER)
    if len(header) < CNT_HEADER or header[:4] != CNT_MAGIC:
        return b"", ""
    content_id = header[0x40:0x64].split(b"\0", 1)[0].decode("ascii", "replace")
    count, table = _be32(header, 16), _be32(header, 24)
    if not 0 < count <= CNT_MAX_ENTRIES:
        return b"", content_id
    fh.seek(offset + table)
    raw = fh.read(count * CNT_ENTRY)
    for at in range(0, len(raw) - CNT_ENTRY + 1, CNT_ENTRY):
        if _be32(raw, at) == CNT_PARAM and not _be32(raw, at + 8) & CNT_ENCRYPTED:
            size = _be32(raw, at + 20)
            if size > PARAM_MAX:
                break
            fh.seek(offset + _be32(raw, at + 16))
            return fh.read(size), content_id
    return b"", content_id


def list_pkg(tool: dict, path: str) -> dict:
    """Inhalt eines Pakets laut Entpacker: {"files", "bytes", "exe_bytes", "eboot"}. Blockierend, nur lesend.
    Wirft AnyError mit dem Grund des Entpackers."""
    try:
        done = subprocess.run([tool["exe"], path, "--list"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=LIST_TIMEOUT, start_new_session=True, check=False)
    except subprocess.TimeoutExpired as e:
        raise AnyError("Das Paket antwortet nicht (zu langsame Platte?).", "damaged") from e
    except OSError as e:
        raise AnyError("%s startet nicht: %s" % (UNPACKER, e.strerror or e), "no-tool") from e
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AnyError(refusal(lines[0] if lines else ""), "damaged")
    out = {"files": 0, "bytes": 0, "exe_bytes": 0, "eboot": False}
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        size, _, name = line.strip().partition("  ")
        if not size.isdigit() or not name:
            continue                    # verschlüsselte Einträge (Lizenzen) nennt er mit „-“: die bleiben weg
        out["files"] += 1
        out["bytes"] += int(size)
        if name == "eboot.bin" or name.endswith((".prx", ".sprx")):
            out["exe_bytes"] += int(size)
        out["eboot"] = out["eboot"] or name == "eboot.bin"
    return out


def refusal(line: str) -> str:
    """Der Grund des Entpackers als Satz für die Oberfläche."""
    text = line.strip()
    text = text[len("error:"):].strip() if text.startswith("error:") else text
    if "encrypted" in text or "retail package" in text:
        return "Die Dateien sind verschlüsselt. Gelesen werden nur unverschlüsselte Pakete und Ordner."
    return "Der Inhalt ließ sich nicht lesen%s." % (": " + text[:200] if text else "")


def unpack_argv(tool: dict, source: str, out_dir: str) -> list:
    """Befehl des Entpackers. Paket: alles nach out_dir. Ordner: nur die Programmdateien (die übrigen Dateien bleiben
    in der Quelle). --verbose schreibt ohne Terminal alle 5 s eine Fortschrittszeile (unpack_fraction)."""
    return [tool["exe"], source, out_dir, "--verbose"] + ([] if is_pkg(source) else ["--executables-only"])


def unpack_fraction(line: str) -> float | None:
    """Fortschritt aus einer Zeile des Entpackers (0 bis 1), None bei anderen Zeilen. Programmdateien zählen 10 %."""
    found = UNPACK_RE.match(line)
    if found is None:
        return None
    part = min(float(found.group(2)), 100.0) / 100
    return part / 10 if found.group(1).startswith("unwrapping") else 0.1 + part * 0.9


# ---------------------------------------------------------------- Quelle und Prozessor

def is_source(path) -> bool:
    """Ein Ordner mit eboot.bin und sce_sys/param.json? Nur lesend (zwei stat)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    return os.path.isfile(os.path.join(path, "eboot.bin")) and os.path.isfile(os.path.join(path, "sce_sys", "param.json"))


def read_param(folder: str, lang: str = "de") -> dict:
    """{"title", "title_id"} aus sce_sys/param.json; fehlt etwas, leere Texte. Nur lesend."""
    path = os.path.join(folder, "sce_sys", "param.json")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(PARAM_MAX + 1)
    except OSError:
        raw = b""
    return _param_info(raw, lang)


def _param_info(raw: bytes, lang: str) -> dict:
    """{"title", "title_id"} aus dem Inhalt einer param.json. Rein."""
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw and len(raw) <= PARAM_MAX else {}
    except (ValueError, RecursionError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    names = data.get("localizedParameters")
    names = names if isinstance(names, dict) else {}
    default = names.get("defaultLanguage") or data.get("defaultLanguage")
    order = ["de-DE"] if lang == "de" else []
    order += [default, "en-US", "en-GB"] + sorted(k for k in names if isinstance(names.get(k), dict))
    title = ""
    for key in order:
        entry = names.get(key) if isinstance(key, str) else None
        if isinstance(entry, dict) and isinstance(entry.get("titleName"), str) and entry["titleName"].strip():
            title = entry["titleName"].strip()
            break
    title_id = data.get("titleId") if isinstance(data.get("titleId"), str) else ""
    return {"title": " ".join(title.split())[:200], "title_id": re.sub(r"[^A-Z0-9_]", "", title_id.upper())[:16]}


def _cpuinfo(path: str = "/proc/cpuinfo") -> tuple:
    """(Hersteller, Merkmale) des ersten Kerns. Nur lesend."""
    vendor, flags = "", set()
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                key = key.strip()
                if key == "vendor_id" and not vendor:
                    vendor = value.strip()
                elif key == "flags":
                    flags = set(value.split())
                    break
    except OSError:
        pass
    return vendor, flags


def needs_intel(path: str = "/proc/cpuinfo") -> bool:
    """Intel-Prozessor? Dann wandelt der relinker AMD-eigene Befehle um (--to-intel)."""
    return _cpuinfo(path)[0] == "GenuineIntel"


def missing_cpu(path: str = "/proc/cpuinfo") -> list:
    """Namen der Befehlssätze, die diesem Prozessor fehlen (leer, wenn /proc/cpuinfo nichts sagt)."""
    flags = _cpuinfo(path)[1]
    return [label for flag, label in CPU_FLAGS.items() if flags and flag not in flags]


def relinker_argv(tool: dict, source: str, out_exe: str, intel: bool) -> list:
    """Befehl des relinkers: Quelle eboot.bin, Module aus den Modulordnern daneben (Vorgabe des relinkers), dazu die
    Ordner, deren Module das Spiel erst zur Laufzeit lädt."""
    extra = [arg for name in RUNTIME_MODULE_DIRS if os.path.isdir(os.path.join(source, name)) for arg in ("--module-dir", name)]
    return [tool["relinker"]] + (["--to-intel"] if intel else []) + extra + [os.path.join(source, "eboot.bin"), out_exe]


def link_resources(source: str, out_dir: str, tool: dict) -> int:
    """Nach dem relinker: app0/ bekommt Verweise auf alle Einträge der Quelle außer den Modulordnern und dem,
    was der relinker schon dorthin geschrieben hat; libs verweist auf die Bibliotheken der Fassung. Liefert die
    Zahl der Verweise in app0. Wirft OSError."""
    app0 = os.path.join(out_dir, APP0)
    os.makedirs(app0, mode=0o755, exist_ok=True)
    count = _link_tree(source, app0, MODULE_DIRS)
    link_libs(out_dir, tool)
    return count


def _link_tree(source: str, into: str, skip: tuple = ()) -> int:
    """Verweise auf die Einträge von source. Liegt schon ein echter Ordner da (der relinker hat Module hineingeschrieben,
    z. B. Media/Modules), kommen die Verweise in ihn hinein."""
    count = 0
    for name in sorted(os.listdir(source)):
        here, there = os.path.join(source, name), os.path.join(into, name)
        if name in skip:
            continue
        if not os.path.lexists(there):
            os.symlink(here, there)
            count += 1
        elif os.path.isdir(there) and not os.path.islink(there) and os.path.isdir(here):
            count += _link_tree(here, there)
    return count


def link_libs(out_dir: str, tool: dict) -> None:
    """libs verweist auf die Bibliotheken der Fassung. Wirft OSError."""
    os.symlink(tool["libs"], os.path.join(out_dir, LIBS))


def launch_env(lang: str) -> dict:
    """Umgebung eines AnyPS5-Spiels: Vollbild, Sprache der Oberfläche, GameMode, wenn vorhanden."""
    return {"ANYPS5_FULLSCREEN": "1", "ANYPS5_LANGUAGE": LANGUAGES.get(lang, LANGUAGES["en"]),
            "APS5_GAME_MODE": "auto"}


# ---------------------------------------------------------------- Plan für den Installer

def plan(path: str, *, games_root: str, reserve_bytes: int, folder: str | None = None, lang: str = "de",
         latest: Callable[[], dict] | None = None, latest_unpacker: Callable[[], dict] | None = None,
         statvfs=os.statvfs, mounts=None, cpuinfo: str = "/proc/cpuinfo") -> dict:
    """Plan für einen Spieleordner oder eine Paketdatei (Form wie probe.probe, kind "anyps5", dazu tool und unpacker
    {tag, download, bytes} und notes: Hinweise, je einer eine Zeile). estimated: Der Platzbedarf eines Pakets ist nur
    geschätzt, weil der Entpacker noch fehlt. Blockierend: Platte und, nur wenn ein Werkzeug fehlt, das Netz (latest,
    latest_unpacker; Vorgabe fetch_latest, fetch_unpacker). Nur lesend."""
    from . import probe
    real = os.path.realpath(path) if isinstance(path, str) else ""
    package = is_pkg(real)
    out = {"archive": real, "name": os.path.basename(real), "format": "pkg" if package else "folder", "volumes": None,
           "supported": False, "ready": False, "status": "", "message": "", "problems": [], "kind": None}
    if not real or not (package or is_source(real)):
        out["problems"].append({"code": "unsupported", "message": "In diesem Ordner liegt kein Spiel für AnyPS5 "
                                "(es fehlen eboot.bin oder sce_sys/param.json)."})
        return probe._finish(out)
    info = read_pkg(real, lang) if package else read_param(real, lang)
    if package and info["state"] != "debug":
        out["problems"].append({"code": "unsupported", "message": PKG_REFUSED[info["state"]]})
        return probe._finish(out)
    name = os.path.basename(real)[:-len(PKG_SUFFIX)] if package else os.path.basename(real)
    title = info["title"] or probe.clean_name(name) or "Spiel"
    out.update(kind="anyps5", kind_label=probe.KIND_LABELS["anyps5"], platform="linux", system=None, title=title,
               title_id=info["title_id"], exe=EXE, needs_choice=False,
               candidates=[{"archive_path": EXE, "path": EXE, "score": 100, "why": []}])
    missing = missing_cpu(cpuinfo)
    if missing:
        out["problems"].append({"code": "unsupported", "message": "Diesem Prozessor fehlen Befehle, die AnyPS5-Spiele "
                                "brauchen: %s." % ", ".join(missing)})
    notes = ["Die Spieldateien werden auf die Spiele-Platte entpackt. Das Paket selbst wird danach nicht mehr gebraucht."
             if package else "Die Spieldateien bleiben im Spieleordner. Nicht verschieben oder löschen."]
    out["tool"] = _tool_plan(out, notes, "AnyPS5", installed(), latest or fetch_latest)
    tool = unpacker()
    out["unpacker"] = _tool_plan(out, notes, UNPACKER, tool, latest_unpacker or fetch_unpacker)
    if package:
        need = _pkg_space(out, real, tool)
    else:
        need = 2 * _folder_exe_bytes(real)      # einmal als ELF in source/, einmal umgewandelt
    # Ins Spielordner kommen sonst nur das Programm und die umgewandelten Module
    probe._target_and_space(out, {"kind": "anyps5", "title": title}, folder, games_root, None,
                            need + reserve_bytes + RELINKER_MAX, reserve_bytes, statvfs, mounts)
    out["notes"] = notes if out["kind"] == "anyps5" else []       # Hinweise je eine Zeile, auch neben Problemen
    return probe._finish(out)


def _tool_plan(out: dict, notes: list, name: str, tool: dict | None, latest: Callable[[], dict]) -> dict | None:
    """Stand eines Werkzeugs für den Plan: {tag, download, bytes}; fehlt es, ein Hinweis auf das Laden oder ein Problem."""
    if tool is not None:
        return {"tag": tool["tag"], "download": False, "bytes": 0}
    try:
        release = latest()
    except AnyError as e:
        out["problems"].append({"code": "no-tool", "message": "%s ist noch nicht eingerichtet. %s" % (name, e.message)})
        return None
    notes.append("Vorher wird %s %s (%s) von GitHub geladen." % (name, release["tag"], human_size(release["bytes"])))
    return {"tag": release["tag"], "download": True, "bytes": release["bytes"]}


def _pkg_space(out: dict, path: str, tool: dict | None) -> int:
    """Platz für den Inhalt eines Pakets: laut Entpacker, ohne ihn geschätzt (estimated, der Auftrag prüft dann erneut)."""
    if tool is None:
        out["estimated"] = True
        try:
            size = os.stat(path).st_size
        except OSError:
            size = 0
        return size * PKG_ESTIMATE[0] // PKG_ESTIMATE[1]
    try:
        content = list_pkg(tool, path)
    except AnyError as e:
        out["problems"].append({"code": e.code, "message": e.message})
        return 0
    if not content["eboot"]:
        out["problems"].append({"code": "unsupported", "message": "In diesem Paket liegt kein Spiel (es fehlt eboot.bin)."})
    out.update(files=content["files"], unpacked_bytes=content["bytes"])
    return content["bytes"] + 2 * content["exe_bytes"]      # die Programmdateien zusätzlich in source/ und umgewandelt


def _folder_exe_bytes(folder: str) -> int:
    """Größe von eboot.bin und der Module aus den Modulordnern eines Spieleordners. Nur lesend."""
    total = 0
    items = [os.path.join(folder, "eboot.bin")]
    for name in MODULE_DIRS:
        try:
            items += [os.path.join(folder, name, item) for item in os.listdir(os.path.join(folder, name))]
        except OSError:
            continue
    for item in items:
        try:
            total += os.stat(item).st_size
        except OSError:
            continue
    return total
