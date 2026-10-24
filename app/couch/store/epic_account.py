"""Epic-Konto, eigene Spiele, Installieren und Starten über legendary (Bauplan E73).

legendary (github.com/legendary-gl/legendary, GPLv3) ist ein freies Programm für Epics Schnittstellen;
damit laufen Epic-Spiele ohne den Epic-Launcher. Der PC5 Launcher lädt eine feste Fassung beim ersten
Gebrauch nach <data>/tools (VERSION, Prüfsumme SHA256 wie im GitHub-Release) und gibt ihr einen eigenen
Ordner für Anmeldung und Spielliste (LEGENDARY_CONFIG_PATH = <data>/epic, 0700).

Anmeldung: Der Browser des PC5 Launchers öffnet LOGIN_URL; nach der Anmeldung zeigt Epic unter
REDIRECT ein JSON mit "authorizationCode". Der Browser legt den Code in <state>/epic-login.json ab
(0600, wie bei GOG), das Home tauscht ihn mit „legendary auth --code“ gegen Schlüssel. Ein Kennwort
sieht der PC5 Launcher nie.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
import urllib.parse
import urllib.request

from .. import log

VERSION = "0.21.1"
URL = "https://github.com/legendary-gl/legendary/releases/download/%s/legendary_linux_x64" % VERSION
SHA256 = "dbed33bbe96031e65858233e4badf0a1d401bb6cb0cc995d1237cf3a0c826a45"
CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"           # der des Epic-Launchers, wie in legendary
REDIRECT = "https://www.epicgames.com/id/api/redirect?clientId=%s&responseType=code" % CLIENT_ID
LOGIN_URL = "https://www.epicgames.com/id/login?redirectUrl=" + urllib.parse.quote(REDIRECT, safe="")
CODE_TTL_S = 600
PROGRESS = re.compile(r"Progress:\s*([0-9.]+)%")
TARGET = re.compile(r"Install path:\s*(/.+?)\s*$")
# Weitere Zeilen von legendary für Geschwindigkeit, Restzeit und Menge (E99; Wortlaut wie in legendary
# 0.20/0.21, am echten Download unbestätigt): „Download size: 1234.56 MiB“, „ETA: 00:12:34“,
# „ - Downloaded: 123.45 MiB, …“, „ + Download - 12.34 MiB/s (raw) / …“
SIZE = re.compile(r"Download size:\s*([0-9.]+)\s*MiB")
ETA = re.compile(r"ETA:\s*(\d+):(\d{2}):(\d{2})")
DONE = re.compile(r"Downloaded:\s*([0-9.]+)\s*MiB")
SPEED = re.compile(r"\+ Download\s*-\s*([0-9.]+)\s*MiB/s")
MIB = 1024 * 1024
THIRD_PARTY = ("origin", "the ea app", "ubisoftconnect", "uplay")


class EpicError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


def is_login_redirect(uri: str) -> bool:
    return str(uri or "").startswith("https://www.epicgames.com/id/api/redirect")


def code_from_page(text: str) -> str:
    """authorizationCode aus der Antwort der Umleitungsseite (JSON, als Text angezeigt)."""
    try:
        data = json.loads((text or "").strip())
    except ValueError:
        m = re.search(r'"authorizationCode"\s*:\s*"([0-9a-f]{16,64})"', text or "")
        return m.group(1) if m else ""
    code = data.get("authorizationCode") if isinstance(data, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[0-9a-f]{16,64}", code) else ""


def _write_private(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".neu"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def store_code(path: str, code: str, clock=time.time) -> None:
    """Vom Browser gerufen: den Code für das Home ablegen."""
    _write_private(path, {"code": code, "at": clock()})


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "PC5-Launcher"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as fh:   # noqa: S310 (feste Adresse)
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def percent(line: str) -> int | None:
    """Fortschritt aus einer Zeile von „legendary install“ („= Progress: 12.34% (…)“)."""
    m = PROGRESS.search(line or "")
    return min(100, int(float(m.group(1)))) if m else None


def stats(line: str, into: dict) -> bool:
    """Menge, Restzeit und Geschwindigkeit aus einer Zeile in into übernehmen ({total, done, eta,
    speed}; Bytes, Sekunden, Bytes/s). True, wenn sich etwas geändert hat."""
    line = line or ""
    found = {}
    m = SIZE.search(line)
    if m:
        found["total"] = int(float(m.group(1)) * MIB)
    m = ETA.search(line)
    if m:
        found["eta"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = DONE.search(line)
    if m:
        found["done"] = int(float(m.group(1)) * MIB)
    m = SPEED.search(line)
    if m:
        found["speed"] = int(float(m.group(1)) * MIB)
    changed = any(into.get(k) != v for k, v in found.items())
    into.update(found)
    return changed


def parse_updates(text: str) -> list[dict]:
    """`legendary list-installed --check-updates --tsv`: Kopfzeile, dann je Spiel App-Name, Titel,
    installierte Fassung, verfügbare Fassung, Update available (True/False), Größe, Ordner, Plattform."""
    import csv
    import io
    out = []
    for row in csv.reader(io.StringIO(text), dialect="excel-tab"):
        if len(row) >= 5 and row[0] != "App name" and row[4].strip() == "True":
            out.append({"app": row[0], "title": row[1], "installed": row[2], "latest": row[3]})
    return out


def card_of(game: dict) -> dict | None:
    """Ein Spiel aus „legendary list --json“ als Karte (Namensraum ohne Angebot, siehe store.epic)."""
    meta = game.get("metadata") if isinstance(game.get("metadata"), dict) else {}
    app = str(game.get("app_name") or "")
    namespace = str(meta.get("namespace") or "")
    if not app or not namespace or "mainGameItem" in meta:
        return None
    images = {str(k.get("type")): str(k.get("url")) for k in meta.get("keyImages") or []
              if isinstance(k, dict) and k.get("url")}
    attrs = meta.get("customAttributes") if isinstance(meta.get("customAttributes"), dict) else {}
    third = str(((attrs.get("ThirdPartyManagedApp") or attrs.get("ThirdPartyManagedProvider") or {}).get("value")) or "")
    return {"store": "epic", "id": namespace + ":", "app": app, "title": str(game.get("app_title") or meta.get("title") or app),
            "slug": "", "image": images.get("DieselGameBox") or images.get("OfferImageWide") or images.get("DieselGameBoxTall") or "",
            "price": "", "original": "", "discount": 0, "third_party": third}


class Legendary:
    def __init__(self, tool_dir: str, config_dir: str, run=subprocess.run, popen=subprocess.Popen,
                 fetch=_download, clock=time.time):
        self.tool_dir = tool_dir
        self.config_dir = config_dir
        self.exe = os.environ.get("COUCH_LEGENDARY") or os.path.join(tool_dir, "legendary-" + VERSION)
        self._run = run
        self._popen = popen
        self._fetch = fetch
        self._clock = clock
        self.last_app = ""                  # zuletzt begonnene Installation und ihr Ordner (für Abbruch)
        self.last_target = ""

    # ------------------------------------------------------------------ Programm
    def ready(self) -> bool:
        return os.path.isfile(self.exe) and os.access(self.exe, os.X_OK)

    def ensure(self) -> str:
        """legendary bereitstellen (einmal ~3 MB laden, Prüfsumme). Blockierend."""
        if self.ready():
            return self.exe
        os.makedirs(self.tool_dir, exist_ok=True)
        part = self.exe + ".part"
        try:
            self._fetch(URL, part)
            with open(part, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            if digest != SHA256:
                raise EpicError("Das Epic-Werkzeug (legendary) kam beschädigt an. Versuche es noch einmal.")
            os.chmod(part, 0o755)
            os.replace(part, self.exe)
        except OSError as e:
            raise EpicError("Das Epic-Werkzeug (legendary) ließ sich nicht laden: %s" % e) from e
        finally:
            if os.path.exists(part):
                os.unlink(part)
        return self.exe

    def env(self) -> dict:
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        return {**os.environ, "LEGENDARY_CONFIG_PATH": self.config_dir}

    def call(self, args: list, timeout: float = 120.0) -> tuple[int, str, str]:
        self.ensure()
        try:
            r = self._run([self.exe, *args], env=self.env(), capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise EpicError("Epic antwortet gerade nicht.") from e
        return r.returncode, r.stdout or "", r.stderr or ""

    # ------------------------------------------------------------------ Konto
    def logged_in(self) -> bool:
        return os.path.isfile(os.path.join(self.config_dir, "user.json"))

    def user_name(self) -> str:
        try:
            with open(os.path.join(self.config_dir, "user.json"), encoding="utf-8") as fh:
                return str(json.load(fh).get("displayName") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def take_code(self, code_file: str) -> bool:
        """Liegt ein frischer Code vom Browser bereit, anmelden. True: neu angemeldet."""
        try:
            with open(code_file, encoding="utf-8") as fh:
                data = json.load(fh)
            os.unlink(code_file)
        except (OSError, ValueError):
            return False
        code = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
        try:
            fresh = self._clock() - float(data.get("at") or 0) < CODE_TTL_S
        except (TypeError, ValueError, AttributeError):
            fresh = False
        if not code or not fresh:
            return False
        rc, _out, err = self.call(["auth", "--code", code], timeout=60)
        if rc != 0 or not self.logged_in():
            raise EpicError("Die Anmeldung bei Epic hat nicht geklappt. Versuche es noch einmal.")
        return True

    def logout(self) -> None:
        self.call(["auth", "--delete"], timeout=30)

    def owned(self, refresh: bool = False) -> list[dict]:
        """Eigene Spiele (Karten, nach Titel). Blockierend, braucht das Netz."""
        if not self.logged_in():
            return []
        rc, out, err = self.call(["list", "--json"] + (["--force-refresh"] if refresh else []), timeout=300)
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if rc != 0 or not isinstance(data, list):
            raise EpicError("Deine Epic-Spiele ließen sich nicht abrufen%s." % (
                " (bitte neu anmelden)" if "Login failed" in err or "login" in err.lower() else ""))
        cards = [c for c in (card_of(g) for g in data if isinstance(g, dict)) if c]
        return sorted(cards, key=lambda c: c["title"].casefold())

    def installed(self) -> dict:
        """{app_name: {install_path, executable, title, …}} aus legendarys installed.json (ohne Netz)."""
        try:
            with open(os.path.join(self.config_dir, "installed.json"), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ Installieren
    def install(self, app: str, base_path: str, on_progress, cancel, log_path: str = "", on_stats=None) -> dict:
        """Spiel laden und installieren (Windows-Fassung, mit eigenen Zusatzinhalten). Blockierend.
        on_progress(prozent); on_stats({total, done, eta, speed}); cancel: threading.Event. Ergebnis:
        Eintrag aus installed.json."""
        self.ensure()
        known = self.installed().get(app)
        if known and not os.path.isdir(str(known.get("install_path") or "")):
            self.call(["-y", "uninstall", app, "--keep-files"], timeout=60)   # Ordner von Hand gelöscht
        argv = [self.exe, "-y", "install", app, "--base-path", base_path, "--platform", "Windows", "--skip-sdl",
                "--with-dlcs"]
        self.last_app, self.last_target = app, ""
        rc, failures = self._stream(argv, on_progress, cancel, log_path, on_stats)
        if cancel.is_set():
            raise EpicError("Abgebrochen.")
        info = self.installed().get(app)
        if rc != 0 or not info:
            if any("disk space" in f for f in failures):
                raise EpicError("Auf %s ist nicht genug Platz für dieses Spiel." % base_path)
            raise EpicError("legendary konnte das Spiel nicht installieren (Rückgabewert %s%s)."
                            % (rc, ": " + failures[0] if failures else ""))
        return info

    def _stream(self, argv: list, on_progress, cancel, log_path: str, on_stats=None) -> tuple[int, list]:
        """legendary mit Fortschritt laufen lassen. (Rückgabewert, Fehlerzeilen). Blockierend."""
        # angehängt: hier selbst kappen (E99), auch wenn ein Aufrufer es vergisst (log.capped ist doppelt harmlos)
        log_fh = open(log.capped(log_path), "a", encoding="utf-8") if log_path else None
        failures = []
        numbers: dict = {}
        try:
            proc = self._popen(argv, env=self.env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
            for line in proc.stdout:
                if log_fh:
                    log_fh.write(line)
                m = TARGET.search(line)
                if m:
                    self.last_target = m.group(1)
                if "! Failure:" in line or "Not enough available disk space" in line:
                    failures.append(line.split("Failure:", 1)[-1].strip())
                if stats(line, numbers) and on_stats is not None:
                    on_stats(dict(numbers))
                p = percent(line)
                if p is not None:
                    on_progress(p)
                if cancel.is_set() and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGINT)     # legendary merkt sich den Stand zum Fortsetzen
            rc = proc.wait()
        finally:
            if log_fh:
                log_fh.close()
        return rc, failures

    # ------------------------------------------------------------------ Updates
    def check_updates(self) -> list[dict]:
        """Installierte Spiele mit neuerer Fassung bei Epic: [{app, title, installed, latest}].
        Fragt Epic (Netz, Anmeldung nötig). Blockierend; ohne Anmeldung leer."""
        if not self.logged_in():
            return []
        rc, out, _err = self.call(["list-installed", "--check-updates", "--tsv"], timeout=120)
        if rc != 0:
            raise EpicError("Epic-Updates ließen sich nicht prüfen.")
        return parse_updates(out)

    def update(self, app: str, on_progress, cancel, log_path: str = "", on_stats=None) -> dict:
        """Ein installiertes Spiel aktualisieren (legendary update). Blockierend."""
        self.ensure()
        self.last_app, self.last_target = app, ""
        rc, failures = self._stream([self.exe, "-y", "update", app], on_progress, cancel, log_path, on_stats)
        if cancel.is_set():
            raise EpicError("Abgebrochen.")
        info = self.installed().get(app)
        if rc != 0 or not info:
            if any("disk space" in f for f in failures):
                raise EpicError("Für das Update ist nicht genug Platz.")
            raise EpicError("legendary konnte das Spiel nicht aktualisieren (Rückgabewert %s%s)."
                            % (rc, ": " + failures[0] if failures else ""))
        return info

    def uninstall(self, app: str) -> None:
        rc, _out, err = self.call(["-y", "uninstall", app], timeout=1800)
        if rc != 0 and app in self.installed():
            raise EpicError("Das Spiel ließ sich nicht deinstallieren.")
