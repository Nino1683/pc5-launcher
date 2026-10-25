"""Selbst-Update von GitHub (E116, Fassung 0.3.1; Wunsch: „eine Update-Funktion, die das Release von GitHub
installiert und eine Benachrichtigung meldet, wenn es eine neue Version gibt“).

Ablauf (alles Blockierende gehört in einen Arbeits-Thread):
  check()     GET API_URL höchstens alle CHECK_EVERY_S nach der Wanduhr, mit If-None-Match (ETag) und
              eigenem User-Agent: Ohne Anmeldung erlaubt GitHub 60 Anfragen je Stunde und Adresse. Die
              Releases sind Vorab-Fassungen (prerelease), /releases/latest liefert dafür nichts: deshalb die
              Liste, Entwürfe übersprungen, höchstes Tag vX.Y.Z mit Paket und .sha256 (pick_release).
              Fehler (kein Netz, 403/429, Unsinn) kommen nur ins Protokoll und als Satz an die Zeile.
  download()  pc5-launcher-X.Y.Z.tar.gz und .sha256 nach <cache>/selfupdate/X.Y.Z/ (erst .part, dann
              umbenennen). Die Adressen baut der Code aus REPO, Tag und Dateiname, nie aus der API-Antwort.
              Nur https, Umleitungen nur zu github.com bzw. *.githubusercontent.com (_GitHubOnly), höchstens
              MAX_PACKAGE, sha256 aus der .sha256-Datei; nennt die API einen digest, muss er gleich sein.
  extract()   in einen markierten Zwischenordner unpack-<zufall>: tarfile-Filter „data“ plus eigene Prüfung
              (nur Ordner und Dateien; kein absoluter Pfad, kein „..“, keine Verweise, Geräte, FIFOs), genau
              ein Oberordner pc5-launcher-X.Y.Z/, dessen VERSION X.Y.Z sagt.
  install()   <python> -I <ordner>/installer/pc5_install.py install --yes mit Zeitgrenze, Ausgabe nach
              <state>/selfupdate-install.log. pc5_install prüft MANIFEST.sha256 selbst, legt die Fassung
              unter <data>/releases ab, stellt current/previous um, behält den Sitzungshaken und lässt
              Fassungen in Benutzung stehen; es ruft nie sudo. Die neue Fassung gilt nach „PC5 Launcher neu
              starten“. Danach räumt cleanup() den Ordner Datei für Datei ab, den entpackten Baum nur über
              trash.remove_own_dir (die eine Löschstelle, SAFETY 13).

Selbst installiert wird nur eine Paket-Installation (install.sh legt <data>/install-manifest.json an, und das
Home läuft aus <data>/releases). Ohne Manifest (tools/install-release, Entwicklungsrechner) oder mit einer Fassung
„-dirty“ wird nur gemeldet: Sonst ersetzte das öffentliche Paket den privaten, neueren Stand.

Prüfmodus: Ohne eingesteckte Netzfunktion gibt es kein Netz; der Installer läuft nur als Attrappe
(proc.manager.test_mode_allows) und mit HOME und den XDG-Orten im Käfig.

Zustand <state>/selfupdate.json: checked_at, etag, release {version, size, digest, name}, announced (schon
gemeldete Fassung), installed (abgelegt, wartet auf den Neustart), error, error_at.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Callable

from . import jsontext, log, paths

_log = log.get("selfupdate")

REPO = "Nino1683/pc5-launcher"
API_URL = "https://api.github.com/repos/%s/releases?per_page=10" % REPO
DOWNLOAD_URL = "https://github.com/%s/releases/download/%s/%s"
USER_AGENT = "PC5-Launcher (Selbst-Update)"
CHECK_EVERY_S = 24 * 3600.0
DUE_SLACK_S = 600.0             # so viel früher gilt die Suche schon als fällig (Takt des Zeitgebers)
RETRY_AFTER_S = 3 * 3600.0      # nach einem Fehler (kein Netz, Grenze von GitHub) erst dann wieder von selbst
FORCE_GAP_S = 30.0              # „Jetzt suchen“ kurz hintereinander fragt GitHub nur einmal
TIMEOUT_S = 20.0
MAX_JSON = 2 << 20              # zehn Releases samt Texten sind rund 30 KB
MAX_SUM = 4096
# 0.3.0: 3,0 MB gepackt, 7,8 MB entpackt, 364 Einträge. Die Grenzen lassen Luft für Schriften und Töne,
# halten aber eine Archiv-Bombe oder eine falsche Datei auf, bevor sie den Cache füllt.
MAX_PACKAGE = 100 << 20
MAX_UNPACKED = 400 << 20
MAX_MEMBERS = 20000
# pc5_install: Kopie, bash -n, Übersetzen (bis 300 s), Selbsttest des Supervisors, couch-home --check (bis 180 s)
INSTALL_TIMEOUT_S = 900.0
LEFTOVER_AGE_S = 2 * 3600.0     # entpackte Reste erst so alt abräumen: ein Installer vom letzten Home läuft vielleicht noch
CHUNK = 256 * 1024
UNPACK_PREFIX = "unpack-"
UNPACK_RE = re.compile(r"^unpack-([0-9a-f]{32})$")
MARKER = ".couch-selfupdate"
STATE_NAME = "selfupdate.json"
LOG_NAME = "selfupdate-install.log"
_TAG = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
_PLAIN = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:[-+~]([0-9A-Za-z.+~-]{1,40}))?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HOSTS = ("github.com", "api.github.com")
_HOST_SUFFIX = ".githubusercontent.com"     # Objektspeicher der Release-Dateien (release-assets.…)

MSG_NET = "GitHub ist nicht erreichbar. Besteht eine Verbindung zum Internet?"
MSG_RATE = "GitHub lässt gerade keine Abfragen zu (zu viele in kurzer Zeit). Später noch einmal."
MSG_BAD = "GitHub lieferte eine unerwartete Antwort."
MSG_STOPPED = "Abgebrochen."
MSG_DEV = ("Diese Installation stammt nicht aus dem Paket von GitHub. Der PC5 Launcher aktualisiert sie deshalb "
           "nicht selbst.")
MSG_BUSY = "Der PC5 Launcher wird gerade aktualisiert."


class UpdateError(Exception):
    """Fehler mit deutschem Satz für die Oberfläche; code für Tests und das Protokoll."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ================================================================ Versionen und Releases

def parse_version(text: Any) -> tuple[int, int, int] | None:
    """(X, Y, Z) aus „X.Y.Z“, „vX.Y.Z“ oder „X.Y.Z-zusatz“; None für alles andere."""
    m = _VERSION.match(str(text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_plain(text: Any) -> bool:
    """Genau X.Y.Z, ohne Zusatz (eine veröffentlichte Fassung; „0.3.1-dirty“ ist keine)."""
    return bool(_PLAIN.match(str(text or "")))


def newer(candidate: Any, own: Any) -> bool:
    """Ist candidate eine höhere Fassung als own? Gezählt wird nur X.Y.Z: „0.3.1-dirty“ steht auf 0.3.1,
    eine veröffentlichte 0.3.1 ist also nicht neuer. Unlesbares ist nie neuer."""
    a, b = parse_version(candidate), parse_version(own)
    return a is not None and b is not None and is_plain(candidate) and a > b


def asset_name(version: str) -> str:
    return "pc5-launcher-%s.tar.gz" % version


def download_url(version: str, name: str) -> str:
    """Adresse einer Release-Datei, gebaut aus REPO, Tag und Dateiname (nie aus der API-Antwort)."""
    return DOWNLOAD_URL % (REPO, "v" + version, name)


def pick_release(data: Any) -> dict | None:
    """Höchste brauchbare Fassung aus GET /releases: kein Entwurf, Tag vX.Y.Z, Paket und .sha256 vorhanden
    (hochgeladen), Paket nicht größer als MAX_PACKAGE. Vorab-Fassungen zählen (alle sind es)."""
    best = None
    for rel in data if isinstance(data, list) else []:
        if not isinstance(rel, dict) or rel.get("draft") is not False:
            continue
        m = _TAG.match(str(rel.get("tag_name") or ""))
        if not m:
            continue
        version = "%d.%d.%d" % tuple(int(x) for x in m.groups())
        if "v" + version != rel.get("tag_name"):
            continue                    # „v0.03.1“ o. Ä.: nicht neu gebaut erreichbar
        assets = {str(a.get("name") or ""): a for a in rel.get("assets") or [] if isinstance(a, dict)
                  and a.get("state", "uploaded") == "uploaded"}
        pkg, summ = assets.get(asset_name(version)), assets.get(asset_name(version) + ".sha256")
        if pkg is None or summ is None:
            continue
        size = pkg.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_PACKAGE:
            _log.warning("Release %s: Paketgröße %r außerhalb der Grenze, übersprungen", version, size)
            continue
        digest = str(pkg.get("digest") or "").lower()
        if not (digest.startswith("sha256:") and _HEX64.match(digest[7:])):
            digest = ""
        item = {"version": version, "size": size, "digest": digest, "name": str(rel.get("name") or "")[:120]}
        if best is None or parse_version(version) > parse_version(best["version"]):
            best = item
    return best


def parse_sum(text: str, asset: str) -> str:
    """sha256 aus der Zeile „<hex>  <datei>“ zu genau dieser Datei; "" sonst."""
    for line in str(text or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and _HEX64.match(parts[0].lower()) and parts[1].lstrip("*") == asset:
            return parts[0].lower()
    return ""


# ================================================================ Netz

def allowed_url(url: Any) -> bool:
    """Nur https ohne Anmeldedaten zu github.com, api.github.com oder *.githubusercontent.com."""
    try:
        p = urllib.parse.urlsplit(str(url or ""))
        port = p.port
    except ValueError:
        return False
    if p.scheme != "https" or p.username or p.password or port not in (None, 443):
        return False
    host = (p.hostname or "").lower().rstrip(".")
    return host in _HOSTS or (host.endswith(_HOST_SUFFIX) and len(host) > len(_HOST_SUFFIX))


class _GitHubOnly(urllib.request.HTTPRedirectHandler):
    """Umleitungen nur zu GitHub und seinem Objektspeicher, nur https: Das Paket wird danach installiert,
    eine Umleitung auf http:// oder einen fremden Ort ließe es unterwegs austauschen (wie E100)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise UpdateError("GitHub hat auf eine fremde oder unsichere Adresse umgeleitet; es wird nichts geladen.",
                              "redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GitHubOnly)


def default_open(url: str, headers: dict) -> Any:
    return _OPENER.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_S)   # noqa: S310


def _no_net(url: str, headers: dict) -> Any:
    """Im Prüfmodus ohne eingesteckte Netzfunktion: nie ins echte Netz (SAFETY Regel 11)."""
    raise urllib.error.URLError("Prüfmodus: kein Netz")


def _net_error(e: BaseException) -> UpdateError:
    if isinstance(e, UpdateError):
        return e
    if isinstance(e, urllib.error.HTTPError):
        try:
            e.close()
        except Exception:  # noqa: BLE001
            pass
        if e.code in (403, 429):
            return UpdateError(MSG_RATE, "rate-limit")
        if e.code == 404:
            return UpdateError("GitHub kennt diese Datei nicht (HTTP 404).", "not-found")
        return UpdateError("GitHub antwortete mit einem Fehler (HTTP %d)." % e.code, "http")
    return UpdateError(MSG_NET, "network")


# ================================================================ Installation erkennen

def read_small(path: str, limit: int = 256) -> str:
    """Kurze eigene Textdatei lesen, nie über einen Verweis; "" wenn es sie nicht gibt."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def install_kind(own: str, data_dir: str, release_dir: str) -> tuple[str, str]:
    """("package", "") für eine Installation aus dem Paket, sonst ("dev", Grund). Paket heißt: Fassung X.Y.Z
    ohne Zusatz, <data>/install-manifest.json lesbar (pc5_install, Format 1) und das Home läuft aus
    <data>/releases. Im Zweifel "dev": dann wird nur gemeldet."""
    if not is_plain(own):
        return "dev", "Diese Fassung (%s) ist ein eigener Stand und wird nicht selbst aktualisiert." % own
    text = read_small(os.path.join(data_dir, "install-manifest.json"), 1 << 20)
    try:
        data = json.loads(text) if text else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("format") != 1:
        return "dev", MSG_DEV
    if not paths.is_under(release_dir, [os.path.join(data_dir, "releases")]):
        return "dev", "Dieser PC5 Launcher läuft nicht aus einer installierten Fassung und wird nicht selbst aktualisiert."
    return "package", ""


def current_version(data_dir: str) -> str:
    """VERSION der Fassung, auf die <data>/current zeigt ("" wenn keine)."""
    cur = os.path.join(data_dir, "current")
    real = os.path.realpath(cur)
    if not os.path.lexists(cur) or not paths.is_under(real, [os.path.join(data_dir, "releases")]):
        return ""
    return read_small(os.path.join(real, "VERSION"), 64).strip()


def python_for_installer() -> str:
    """Das Python des Home (couch-home nimmt COUCH_PYTHON oder /usr/bin/python3, sys.executable ist dasselbe):
    Mit ihm übersetzt und prüft pc5_install die neue Fassung, und mit ihm läuft sie danach."""
    exe = sys.executable
    if exe and os.path.isabs(exe) and os.access(exe, os.X_OK):
        return exe
    if os.access("/usr/bin/python3", os.X_OK):
        return "/usr/bin/python3"
    return shutil.which("python3") or "python3"


def installer_env() -> dict:
    """Umgebung des Installers: die des Home ohne PYTHON*-Variablen (-I übergeht sie, couch-home --check
    nicht). Im Prüfmodus liegen HOME, die XDG-Orte und das Protokoll der Systembefehle im Käfig, und
    pc5_install führt Systembefehle nur zum Schein aus (PC5_DRY_SYSTEM)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    root = paths.test_root()
    if paths.test_mode() and root:
        home = os.path.join(root, "installer-home")
        env.update(HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"),
                   XDG_DATA_HOME=os.path.join(home, ".local", "share"),
                   XDG_RUNTIME_DIR=os.path.join(root, "installer-run"), PC5_DRY_SYSTEM="1",
                   PC5_DRY_LOG=os.path.join(root, "installer-dry.log"))
    return env


# ================================================================ Entpacken

def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def safe_extract(archive: str, dest: str, version: str) -> str:
    """Entpackt das Paket nach dest (eigener, frischer Ordner). Erlaubt: genau ein Oberordner
    pc5-launcher-<version>, darin nur Ordner und Dateien, nichts Absolutes, kein „..“, nicht mehr als
    MAX_MEMBERS Einträge und MAX_UNPACKED Bytes (tarfile-Filter „data“ plus diese Prüfungen). VERSION im
    Oberordner muss version sagen, installer/pc5_install.py muss da sein. Rückgabe: Pfad des Oberordners."""
    top = "pc5-launcher-" + version
    real = os.path.realpath(dest)
    seen = {"n": 0, "bytes": 0}

    def vet(member: tarfile.TarInfo, path: str):
        seen["n"] += 1
        if seen["n"] > MAX_MEMBERS:
            raise UpdateError("Das Paket hat unplausibel viele Einträge und wird nicht entpackt.", "bad-archive")
        name = member.name
        if not name or name.startswith("/") or "\0" in name or "\\" in name:
            raise UpdateError("Das Paket enthält einen absoluten oder unzulässigen Pfad und wird nicht entpackt.",
                              "bad-archive")
        if ".." in name.split("/"):
            raise UpdateError("Das Paket enthält „..“ im Pfad und wird nicht entpackt.", "bad-archive")
        norm = posixpath.normpath(name)
        if norm in (".", ""):
            return None                             # Wurzeleintrag „./“
        if norm.split("/", 1)[0] != top or (norm == top and not member.isdir()):
            raise UpdateError("Das Paket enthält mehr als den Ordner „%s“ und wird nicht entpackt." % top, "bad-archive")
        if not (member.isdir() or member.isreg()):
            # das Paket hat keine Verweise (0.3.0: nur Ordner und Dateien); Geräte und FIFOs ohnehin nicht
            raise UpdateError("Das Paket enthält Verweise oder Sonderdateien und wird nicht entpackt.", "bad-archive")
        if member.isreg():
            seen["bytes"] += member.size
            if seen["bytes"] > MAX_UNPACKED:
                raise UpdateError("Das Paket wäre entpackt unplausibel groß und wird nicht entpackt.", "bad-archive")
        member = member.replace(name=norm, deep=False)
        try:
            return tarfile.data_filter(member, path)
        except tarfile.FilterError as e:
            raise UpdateError("Das Paket enthält Unzulässiges und wird nicht entpackt (%s)." % e, "bad-archive") from e

    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            tar.extractall(real, filter=vet)
    except UpdateError:
        raise
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile, OSError) as e:
        raise UpdateError("Das Paket ist beschädigt und wird nicht installiert (%s)." % e, "bad-archive") from e
    root = os.path.join(real, top)
    if os.path.islink(root) or not os.path.isdir(root):
        raise UpdateError("Im Paket fehlt der Ordner „%s“." % top, "bad-archive")
    said = read_small(os.path.join(root, "VERSION"), 64).strip()
    if said != version:
        raise UpdateError("Das Paket nennt eine andere Fassung (%s statt %s) und wird nicht installiert."
                          % (said or "keine", version), "bad-version")
    script = os.path.join(root, "installer", "pc5_install.py")
    if os.path.islink(os.path.join(root, "installer")) or os.path.islink(script) or not os.path.isfile(script):
        raise UpdateError("Im Paket fehlt der Installer.", "bad-archive")
    return root


# ================================================================ Dienst

class SelfUpdate:
    """Suche, Laden und Installieren (siehe Modulkopf). check(), download(), install() blockieren; status()
    und busy_text() sind schnell und dürfen aus jedem Thread kommen. start_update() lädt im Hintergrund.

    notify(title, body): Mitteilung bei einer neuen Fassung (einmal je Fassung). changed(): Seiten neu zeichnen
    (gedrosselt). background(fn, *args): Arbeits-Thread. Alles Übrige steckt Tests ein."""

    def __init__(self, *, own: str | None = None, open_url: Callable | None = None,
                 notify: Callable[[str, str], None] | None = None, background: Callable | None = None,
                 changed: Callable[[], None] | None = None, wall: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic, runner: Callable | None = None,
                 python: str | None = None, data_dir: str | None = None, state_dir: str | None = None,
                 cache_dir: str | None = None, release_dir: str | None = None, timeout_s: float = INSTALL_TIMEOUT_S):
        if own is None:
            from . import VERSION as own
        self.own = str(own)
        self.open_url = open_url or (_no_net if paths.test_mode() else default_open)
        self.notify = notify
        self.background = background
        self.changed = changed or (lambda: None)
        self.wall, self.clock = wall, clock
        self.runner = runner
        self.python = python or python_for_installer()
        self.data_dir = data_dir or paths.data_dir()
        self.state_dir = state_dir or paths.state_dir()
        self.cache_dir = cache_dir or paths.cache_dir()
        self.release_dir = release_dir or paths.release_dir()
        self.timeout_s = float(timeout_s)
        self.lock = threading.RLock()
        self.checking = False
        self.job: dict = {"phase": "idle", "percent": 0, "error": "", "version": ""}
        self.stop = threading.Event()
        self._shown_at = -1e9

    # ------------------------------------------------------------ Orte und Zustand
    @property
    def state_file(self) -> str:
        return os.path.join(self.state_dir, STATE_NAME)

    @property
    def log_file(self) -> str:
        return os.path.join(self.state_dir, LOG_NAME)

    def _load(self) -> dict:
        text = read_small(self.state_file, 1 << 20)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            _log.warning("%s unlesbar, beginne neu", self.state_file)
            data = {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            from . import system
            system.write_atomic(self.state_file, jsontext.dumps(data, indent=1, sort_keys=True) + "\n")
        except OSError as e:
            _log.warning("%s nicht schreibbar: %s", self.state_file, e)

    def kind(self) -> tuple[str, str]:
        return install_kind(self.own, self.data_dir, self.release_dir)

    def _release(self, st: dict) -> dict | None:
        rel = st.get("release")
        if isinstance(rel, dict) and is_plain(rel.get("version")):
            return rel
        return None

    def available(self, st: dict | None = None) -> str:
        """Gefundene höhere Fassung ("" wenn keine)."""
        rel = self._release(self._load() if st is None else st)
        return rel["version"] if rel and newer(rel["version"], self.own) else ""

    def pending(self, st: dict | None = None) -> str:
        """Abgelegte Fassung, die auf den Neustart wartet: installiert über diesen Weg, höher als die laufende,
        und current zeigt noch darauf (sonst hat jemand zurückgestellt oder anders installiert)."""
        st = self._load() if st is None else st
        version = str(st.get("installed") or "")
        if not newer(version, self.own) or current_version(self.data_dir) != version:
            return ""
        return version

    def status(self) -> dict:
        """{own, kind, why, phase, available, pending, percent, error, checked_at, checking, size, name}. phase:
        unknown | current | available | downloading | installing | installed | failed."""
        st = self._load()
        kind, why = self.kind()
        job = dict(self.job)
        avail, pending = self.available(st), self.pending(st)
        rel = self._release(st) or {}
        if job["phase"] in ("downloading", "installing"):
            phase = job["phase"]
        elif pending:
            phase = "installed"
        elif job["phase"] == "failed":
            phase = "failed"
        elif avail:
            phase = "available"
        elif isinstance(st.get("checked_at"), (int, float)) and not st.get("error"):
            phase = "current"
        elif rel:
            phase = "current"           # letzter bekannter Stand; der Fehler der letzten Suche steht in error
        else:
            phase = "unknown"
        return {"own": self.own, "kind": kind, "why": why, "phase": phase, "available": avail, "pending": pending,
                "percent": int(job.get("percent") or 0), "job_error": str(job.get("error") or ""),
                "error": str(st.get("error") or ""), "checked_at": st.get("checked_at"),
                "checking": bool(self.checking), "size": int(rel.get("size") or 0) if avail else 0,
                "version": job.get("version") or avail}

    def busy(self) -> bool:
        return self.job["phase"] in ("downloading", "installing")

    def busy_text(self) -> str | None:
        """Für wiring.busy_checks: Ausschalten und Neustart fragen nach, solange geladen oder installiert wird."""
        return MSG_BUSY if self.busy() else None

    # ------------------------------------------------------------ Suchen
    def _due(self, st: dict, now: float, force: bool) -> bool:
        last = st.get("checked_at")
        last = last if isinstance(last, (int, float)) and not isinstance(last, bool) else None
        err = st.get("error_at")
        err = err if isinstance(err, (int, float)) and not isinstance(err, bool) else None
        if force:
            recent = max([t for t in (last, err) if t is not None], default=None)
            return recent is None or not 0 <= now - recent < FORCE_GAP_S
        if err is not None and 0 <= now - err < RETRY_AFTER_S:
            return False
        if last is None:
            return True
        passed = now - last
        # Ging die Uhr weit zurück (vor die letzte Suche), gilt sie als fällig; sonst käme lange keine mehr
        return passed < -DUE_SLACK_S or passed >= CHECK_EVERY_S - DUE_SLACK_S

    def check(self, force: bool = False, quiet: bool = False) -> dict:
        """Arbeits-Thread: GitHub fragen, wenn fällig (force: „Jetzt suchen“). Neue Fassung: eine Mitteilung,
        einmal je Fassung (quiet: nur merken, die Seite zeigt es selbst). Wirft nie; Rückgabe status()."""
        with self.lock:
            if self.checking:
                return self.status()
            st = self._load()
            now = self.wall()
            if not self._due(st, now, force):
                return self.status()
            self.checking = True
        try:
            try:
                self._ask_github(st)
                st["checked_at"] = now
                st.pop("error", None)
                st.pop("error_at", None)
            except Exception as e:  # noqa: BLE001  (nie abstürzen: alles Unerwartete gilt als Störung)
                err = e if isinstance(e, UpdateError) else UpdateError(MSG_BAD, "bad-reply")
                if not isinstance(e, UpdateError):
                    _log.exception("Suche nach einer neuen Fassung")
                _log.warning("Suche nach einer neuen Fassung: %s (%s)", err.message, err.code)
                st["error"], st["error_at"] = err.message, now
            self._announce(st, quiet)
            self._save(st)
        finally:
            with self.lock:
                self.checking = False
        self.changed()
        return self.status()

    def _ask_github(self, st: dict) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        etag = str(st.get("etag") or "")
        if etag and "release" in st:
            headers["If-None-Match"] = etag             # 304 zählt nicht gegen die Grenze von 60 je Stunde
        try:
            resp = self.open_url(API_URL, headers)
            with resp:
                where = getattr(resp, "geturl", lambda: API_URL)()
                if not allowed_url(where):
                    raise UpdateError(MSG_BAD, "redirect")
                body = resp.read(MAX_JSON + 1)
                new_tag = str((getattr(resp, "headers", None) or {}).get("ETag") or "")
        except urllib.error.HTTPError as e:
            if e.code == 304 and "release" in st:
                _net_error(e)                           # schließt die Antwort
                _log.info("Selbst-Update: unverändert (ETag)")
                return
            raise _net_error(e) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise _net_error(e) from e
        if len(body) > MAX_JSON:
            raise UpdateError("GitHub lieferte eine unerwartet große Antwort.", "bad-reply")
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError as e:
            raise UpdateError(MSG_BAD, "bad-reply") from e
        if not isinstance(data, list):
            raise UpdateError(MSG_BAD, "bad-reply")
        rel = pick_release(data)
        st["release"] = rel
        st["etag"] = new_tag[:200]
        _log.info("Selbst-Update: neueste Fassung bei GitHub %s, laufend %s", rel["version"] if rel else "keine", self.own)

    def _announce(self, st: dict, quiet: bool) -> None:
        avail = self.available(st)
        if not avail or st.get("announced") == avail:
            return
        st["announced"] = avail
        if quiet or self.notify is None:
            return
        kind, _why = self.kind()
        body = ("Aktualisieren unter Einstellungen › System › Systemsoftware › Updates." if kind == "package" else
                "Diese Installation stammt nicht aus dem Paket und wird nicht selbst aktualisiert.")
        try:
            self.notify("PC5 Launcher %s ist da" % avail, body)
        except Exception:  # noqa: BLE001
            _log.exception("Mitteilung zur neuen Fassung")

    # ------------------------------------------------------------ Laden
    def _version_dir(self, version: str) -> str:
        """<cache>/selfupdate/<version>, echte Ordner (kein Verweis), mit 0700 angelegt."""
        if not is_plain(version):
            raise UpdateError("Unerwartete Versionsangabe.", "bad-version")
        if paths.test_mode_incomplete():
            raise UpdateError("Prüfmodus ohne COUCH_TEST_ROOT: Es wird nichts geschrieben.", "test-mode")
        base = os.path.join(self.cache_dir, "selfupdate")
        folder = os.path.join(base, version)
        for d in (self.cache_dir, base, folder):
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
            if os.path.islink(d) or not os.path.isdir(d):
                raise UpdateError("Der Zwischenordner für das Update ist kein echter Ordner.", "bad-dir")
        return folder

    def _open(self, url: str) -> Any:
        if not allowed_url(url):
            raise UpdateError("Unerwartete Adresse; es wird nichts geladen.", "redirect")
        try:
            resp = self.open_url(url, {"User-Agent": USER_AGENT})
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise _net_error(e) from e
        where = getattr(resp, "geturl", lambda: url)()
        if not allowed_url(where):
            resp.close()
            raise UpdateError("GitHub hat auf eine fremde oder unsichere Adresse umgeleitet; es wird nichts geladen.",
                              "redirect")
        return resp

    def _write_new(self, path: str):
        """Eigene Datei frisch anlegen, nie über einen Verweis hinweg."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
        return os.fdopen(os.open(path, flags, 0o600), "wb")

    @staticmethod
    def _unlink_own(path: str) -> None:
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass

    def download(self, version: str, progress: Callable[[int, int], None] | None = None,
                 stop: threading.Event | None = None) -> str:
        """Lädt Paket und Prüfsumme nach <cache>/selfupdate/<version>/ und prüft sha256. Rückgabe: Pfad des
        Pakets. UpdateError mit deutschem Satz bei jedem Fehler; eine verworfene Teildatei bleibt nicht liegen."""
        folder = self._version_dir(version)
        name = asset_name(version)
        try:
            with self._open(download_url(version, name + ".sha256")) as resp:
                raw = resp.read(MAX_SUM + 1)
        except UpdateError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise _net_error(e) from e
        want = parse_sum(raw[:MAX_SUM].decode("utf-8", "replace"), name) if len(raw) <= MAX_SUM else ""
        if not want:
            raise UpdateError("Die Prüfsummen-Datei von GitHub ist unlesbar; es wird nichts installiert.", "no-sum")
        rel = self._release(self._load()) or {}
        digest = rel.get("digest") if rel.get("version") == version else ""
        if digest and digest != "sha256:" + want:
            raise UpdateError("Die Prüfsummen von GitHub passen nicht zueinander; es wird nichts installiert.", "checksum")
        with self._write_new(os.path.join(folder, name + ".sha256")) as fh:
            fh.write(raw)
        archive = os.path.join(folder, name)
        part = archive + ".part"
        self._unlink_own(archive)               # nie ein altes Paket ungeprüft übernehmen
        h = hashlib.sha256()
        done = 0
        try:
            with self._open(download_url(version, name)) as resp, self._write_new(part) as out:
                headers = getattr(resp, "headers", None) or {}
                try:
                    total = int(headers.get("Content-Length") or 0)
                except ValueError:
                    total = 0
                if total > MAX_PACKAGE:
                    raise UpdateError("Das Paket ist unerwartet groß und wird nicht geladen.", "too-big")
                total = total or int(rel.get("size") or 0)
                if progress:
                    progress(0, total)
                while True:
                    if stop is not None and stop.is_set():
                        raise UpdateError(MSG_STOPPED, "stopped")
                    try:
                        chunk = resp.read(CHUNK)
                    except OSError as e:
                        raise UpdateError("Der Download brach ab. Versuch es noch einmal.", "interrupted") from e
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_PACKAGE:
                        raise UpdateError("Das Paket ist unerwartet groß und wird nicht geladen.", "too-big")
                    h.update(chunk)
                    out.write(chunk)
                    if progress:
                        progress(done, total)
            if h.hexdigest() != want:
                raise UpdateError("Die geladene Datei ist beschädigt (Prüfsumme stimmt nicht). Sie wurde verworfen.",
                                  "checksum")
            os.replace(part, archive)
        except BaseException:
            self._unlink_own(part)
            raise
        _log.info("Selbst-Update: Paket %s geladen und geprüft (%d Bytes)", version, done)
        return archive

    def extract(self, archive: str, version: str) -> tuple[str, str]:
        """Entpackt in <cache>/selfupdate/<version>/unpack-<zufall>/ (Marke mit dem Zufallswert). Rückgabe
        (Zwischenordner, Oberordner des Pakets)."""
        folder = self._version_dir(version)
        token = secrets.token_hex(16)
        unpack = os.path.join(folder, UNPACK_PREFIX + token)
        os.mkdir(unpack, 0o700)
        with self._write_new(os.path.join(unpack, MARKER)) as fh:
            fh.write(token.encode("ascii"))
        return unpack, safe_extract(archive, unpack, version)

    # ------------------------------------------------------------ Installieren
    def run_installer(self, top: str) -> int:
        """pc5_install.py install --yes aus dem entpackten Paket; Ausgabe ins eigene Protokoll. Im Prüfmodus
        nur eine Attrappe im Käfig. Rückgabe: Rückgabewert des Installers."""
        script = os.path.join(top, "installer", "pc5_install.py")
        argv = [self.python, "-I", script, "install", "--yes"]
        env = installer_env()
        if self.runner is not None:
            return int(self.runner(argv, env, self.log_file, self.timeout_s))
        if paths.test_mode():
            from .proc.manager import test_mode_allows
            if paths.test_mode_incomplete() or not test_mode_allows(script):
                raise UpdateError("Prüfmodus: Ein echter Installer startet hier nicht.", "test-mode")
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        with self._write_new(self.log_file) as out:
            out.write(("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv))).encode("utf-8"))
            out.flush()
            # eigene Sitzung: bei einer Zeitüberschreitung trifft das Beenden nur den Installer und seine Kinder.
            # Ohne pdeathsig: Endet das Home mittendrin, darf der Installer fertig werden (er stellt current
            # erst ganz am Ende um); entpackte Reste räumt cleanup_leftovers erst nach LEFTOVER_AGE_S ab.
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    cwd=os.path.dirname(top), env=env, start_new_session=True, close_fds=True)
            try:
                return proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                _log.error("Selbst-Update: Installer nach %.0f s nicht fertig, wird beendet", self.timeout_s)
                for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
                    try:
                        os.killpg(proc.pid, sig)        # Gruppe des eigenen Kindes (start_new_session)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=wait)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                raise UpdateError("Die Installation dauerte zu lange und wurde abgebrochen.", "timeout") from None

    def _log_tail(self, limit: int = 600) -> str:
        try:
            with open(self.log_file, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - limit))
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    def install(self, version: str, progress: Callable[[int, int], None] | None = None,
                stop: threading.Event | None = None, on_phase: Callable[[str], None] | None = None) -> str:
        """Laden, prüfen, entpacken, pc5_install laufen lassen, aufräumen. Nur für eine Paket-Installation und
        nur eine höhere Fassung. Rückgabe: Satz für die Oberfläche; UpdateError sonst."""
        kind, why = self.kind()
        if kind != "package":
            raise UpdateError(why or MSG_DEV, "dev")
        if not newer(version, self.own):
            raise UpdateError("Diese Fassung ist nicht neuer als die laufende.", "not-newer")
        try:
            archive = self.download(version, progress, stop)
            if stop is not None and stop.is_set():
                raise UpdateError(MSG_STOPPED, "stopped")
            if on_phase:
                on_phase("installing")
            _unpack, top = self.extract(archive, version)
            rc = self.run_installer(top)
            if rc != 0:
                _log.warning("Selbst-Update: Installer endete mit %s: %s", rc, self._log_tail())
                if rc == 3:
                    raise UpdateError("Der Installer hat nichts geändert: Diese Einrichtung stammt nicht aus dem Paket.",
                                      "foreign")
                raise UpdateError("Die Installation ist gescheitert (Rückgabewert %d). Einzelheiten stehen in %s."
                                  % (rc, self.log_file), "installer")
            placed = current_version(self.data_dir)
            if placed != version:
                _log.warning("Selbst-Update: current zeigt auf %r statt %s", placed, version)
                raise UpdateError("Der Installer meldete Erfolg, aber die neue Fassung ist nicht eingestellt. "
                                  "Einzelheiten stehen in %s." % self.log_file, "not-placed")
            with self.lock:
                st = self._load()
                st["installed"] = version
                self._save(st)
            _log.info("Selbst-Update: %s abgelegt, gilt nach dem Neustart", version)
            return "PC5 Launcher %s ist installiert. Er gilt nach einem Neustart des PC5 Launchers." % version
        finally:
            self.cleanup(version)

    # ------------------------------------------------------------ im Hintergrund (Einstellungen)
    def start_update(self) -> str:
        """Lädt und installiert die gefundene Fassung im Hintergrund. Satz für die Seite; UpdateError sonst."""
        kind, why = self.kind()
        if kind != "package":
            raise UpdateError(why or MSG_DEV, "dev")
        version = self.available()
        if not version:
            raise UpdateError("Der PC5 Launcher ist schon aktuell.", "current")
        if self.pending():
            raise UpdateError("Eine neue Fassung ist schon installiert. Starte den PC5 Launcher neu.", "pending")
        if self.background is None:
            raise UpdateError("Das ist gerade nicht verfügbar.", "unavailable")
        with self.lock:
            if self.busy():
                raise UpdateError(MSG_BUSY, "busy")
            self.stop = threading.Event()
            self.job = {"phase": "downloading", "percent": 0, "error": "", "version": version}
        self.background(self._update_job, version, self.stop)
        self.changed()
        return "PC5 Launcher %s wird geladen." % version

    def cancel(self) -> str:
        if self.job["phase"] != "downloading":
            raise UpdateError("Gerade wird nichts geladen." if not self.busy() else
                              "Die Installation läuft schon und lässt sich nicht mehr abbrechen.", "not-loading")
        self.stop.set()
        return "Wird abgebrochen …"

    def _progress(self, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total > 0 else 0
        self.job = {**self.job, "percent": max(0, min(100, pct))}
        now = self.clock()
        if now - self._shown_at >= 1.0:
            self._shown_at = now
            self.changed()

    def _phase(self, phase: str) -> None:
        self.job = {**self.job, "phase": phase, "percent": 100}
        self.changed()

    def _update_job(self, version: str, stop: threading.Event) -> None:
        try:
            text = self.install(version, self._progress, stop, on_phase=self._phase)
            self.job = {"phase": "idle", "percent": 100, "error": "", "version": version}
            if self.notify is not None:
                self.notify("PC5 Launcher %s installiert" % version,
                            "Gilt nach dem Neustart: Einstellungen › System › Systemsoftware › Updates › PC5 Launcher.")
            _log.info("%s", text)
        except Exception as e:  # noqa: BLE001
            err = e if isinstance(e, UpdateError) else UpdateError("Das Update hat nicht geklappt.", "error")
            if not isinstance(e, UpdateError):
                _log.exception("Selbst-Update")
            _log.warning("Selbst-Update %s: %s (%s)", version, err.message, err.code)
            stopped = err.code == "stopped"
            self.job = {"phase": "idle" if stopped else "failed", "percent": 0,
                        "error": "" if stopped else err.message, "version": version}
            if not stopped and self.notify is not None:
                self.notify("PC5 Launcher: Das Update hat nicht geklappt", err.message)
        finally:
            self.changed()

    # ------------------------------------------------------------ Aufräumen
    def cleanup(self, version: str, min_age: float = 0.0) -> list[str]:
        """Räumt <cache>/selfupdate/<version>/ ab: nur die eigenen Dateien (Paket, Teildatei, Prüfsumme) und
        Zwischenordner unpack-<zufall> mit passender Marke (über trash.remove_own_dir, nie durch Verweise oder
        Einhängungen). Unbekanntes bleibt liegen, dann auch der Ordner. min_age: Zwischenordner erst ab diesem
        Alter (Reste eines Laufs, dessen Installer vielleicht noch arbeitet). Rückgabe: entfernte Namen."""
        if not is_plain(version):
            return []
        folder = os.path.join(self.cache_dir, "selfupdate", version)
        if os.path.islink(folder) or not os.path.isdir(folder):
            return []
        name = asset_name(version)
        own_files = {name, name + ".part", name + ".sha256"}
        gone = []
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if entry in own_files and stat.S_ISREG(st.st_mode):
                self._unlink_own(path)
            elif UNPACK_RE.match(entry) and stat.S_ISDIR(st.st_mode):
                mark = os.path.join(path, MARKER)
                if read_small(mark, 64) != UNPACK_RE.match(entry).group(1):
                    _log.info("Selbst-Update: %s ohne passende Marke, bleibt liegen", path)
                    continue
                try:
                    age = self.wall() - os.lstat(mark).st_mtime
                except OSError:
                    continue
                if age < min_age:
                    continue
                try:
                    from .installer import trash
                    trash.remove_own_dir(path)
                except Exception as e:  # noqa: BLE001  (Refuse, OSError: dann bleibt er liegen)
                    _log.warning("Selbst-Update: %s bleibt liegen: %s", path, getattr(e, "message", None) or e)
                    continue
            else:
                _log.info("Selbst-Update: Unbekanntes in %s bleibt liegen: %s", folder, entry)
                continue
            if not os.path.lexists(path):
                gone.append(entry)
        try:
            os.rmdir(folder)                    # nur leer möglich
        except OSError:
            pass
        return gone

    def cleanup_leftovers(self) -> list[str]:
        """Nach dem Start (Arbeits-Thread): Reste abgebrochener Läufe (Absturz, Ausschalten) abräumen, nie
        während eines eigenen Auftrags."""
        if self.busy():
            return []
        base = os.path.join(self.cache_dir, "selfupdate")
        try:
            names = sorted(os.listdir(base)) if not os.path.islink(base) else []
        except OSError:
            return []
        gone = []
        for version in names:
            if is_plain(version):
                gone += ["%s/%s" % (version, n) for n in self.cleanup(version, min_age=LEFTOVER_AGE_S)]
        if gone:
            _log.info("Selbst-Update: Reste entfernt: %s", ", ".join(gone))
        return gone
