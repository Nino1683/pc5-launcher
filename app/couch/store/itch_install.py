"""itch.io-Spiele laden und installieren (Bauplan E100): reine Hilfen für die Warteschlange
(wiring_itch.ItchDownloads), Muster store/gog_install.py.

1. Datei wählen (pick_upload): nur Spiele (type default), keine Demos, keine Pakete; Windows zuerst
   (wie GOG, E68), sonst Linux; 64 vor 32 Bit; Archiv vor Einzeldatei; bei Gleichstand die größere.
   Spiele, die nur im Browser laufen (html, flash, unity, java), ergeben ItchError mit browser_only.
2. Laden (download) nach <ziel>/.itch-setup/<datei>.part mit Range-Fortsetzen (gog_install.resumable);
   die signierte Adresse kommt je Versuch frisch von itch_account.download_url. Kein Schlüssel ans CDN.
3. Art der Datei (kind_of) und eines Windows-Installers (installer_type: Inno Setup, NSIS, MSI);
   stille Schalter (silent_command). Unbestätigt: NSIS und MSI (nur nach Doku, nicht mit echten
   itch.io-Dateien geprüft).
4. Hinweis auf eine neuere Datei (update_hint), nur Hinweis, keine Automatik.
Alles blockiert (Platte, Netz) und gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import os
import re
import urllib.request
from datetime import datetime, timezone

from . import gog_install
from .gog_install import InstallError, Interrupted, windows_dir  # noqa: F401 (die Warteschlange nimmt sie von hier)
from .itch import USER_AGENT
from .itch_account import ItchError, ItchLoginNeeded

SETUP_DIR = ".itch-setup"
PART = ".part"
HEAD_BYTES = 8 * 1024 * 1024          # so weit vorn suchen Inno Setup und NSIS ihre Kennung
BROWSER_TYPES = ("html", "flash", "unity", "java")
ARCHIVES = (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz", ".txz", ".tbz2", ".zip", ".7z", ".rar", ".tar")
PACKAGES = (".deb", ".rpm", ".dmg", ".apk", ".pkg", ".aab", ".ipa", ".snap", ".flatpak", ".flatpakref")
# Dateien, die als „default“ hochgeladen, aber kein Spiel sind (wie Lutris)
NOT_GAMES = ("wallpaper", "soundtrack", "artbook", "art book", "artwork", "poster", "manual", "source code")
ARM = ("arm64", "aarch64", "armhf", "armv7", "arm32")
INNO_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS"]
RETRY_CODES = (403, 408, 410, 429)     # abgelaufene CDN-Adresse oder Störung: neuer Versuch, frische Adresse

MSG_BROWSER = "Dieses Spiel läuft nur im Browser."
MSG_NOTHING = "Für dieses Spiel gibt es keine Datei für Windows oder Linux."
MSG_DEMO = "Von diesem Spiel gibt es bei itch.io nur eine Demo für Windows oder Linux."


# ------------------------------------------------------------------ Dateiarten

def kind_of(filename: str, platform: str = "") -> str:
    """archive | exe | msi | appimage | linux-bin | unsupported."""
    name = os.path.basename(str(filename or "")).strip().lower()
    if not name:
        return "unsupported"
    if name.endswith(ARCHIVES):
        return "archive"
    if name.endswith(".exe"):
        return "exe"
    if name.endswith(".msi"):
        return "msi"
    if name.endswith(".appimage"):
        return "appimage"
    if name.endswith((".x86_64", ".x86")):
        return "linux-bin"
    if "." not in name and platform == "linux":
        return "linux-bin"
    return "unsupported"                       # Pakete, .jar, .sh, .bin, .apk, .dmg …


def _bits(upload: dict, platform: str) -> int:
    """0 = 64 Bit, 1 = unbekannt bzw. beides, 2 = 32 Bit (Rangfolge)."""
    arch = str((upload.get("arch") or {}).get(platform) or "")
    if arch == "amd64":
        return 0
    if arch == "386":
        return 2
    name = str(upload.get("filename") or "").lower() + " " + str(upload.get("display_name") or "").lower()
    # nur deutliche Zeichen: „v1.32“ im Dateinamen ist eine Fassung, keine Bitbreite
    # Grenzen ohne \b: „Game_x64.zip“ (der Unterstrich zählt für \b als Buchstabe)
    if re.search(r"x86[_-]?64|amd64|(?<![a-z0-9])x64(?![a-z0-9])|win64|64[ _-]?bit", name):
        return 0
    if re.search(r"(?<![a-z0-9])(x86|i[36]86|386)(?![a-z0-9])|win32|32[ _-]?bit", name):
        return 2
    return 1


def _usable(upload: dict, platform: str) -> str:
    """Grund, warum die Datei nicht taugt ("" = taugt)."""
    name = str(upload.get("filename") or "").lower()
    label = name + " " + str(upload.get("display_name") or "").lower()
    if not (upload.get("platforms") or {}).get(platform):
        return "platform"
    if name.endswith(PACKAGES) or kind_of(name, platform) == "unsupported":
        return "format"
    if any(word in label for word in NOT_GAMES) or any(word in label for word in ARM):
        return "format"
    if upload.get("preorder"):
        return "preorder"
    if upload.get("demo"):
        return "demo"
    return ""


def _ranked(uploads, platform: str) -> list:
    """Taugliche Dateien einer Plattform, die beste zuerst: 64 vor 32 Bit, Archiv vor Einzeldatei,
    bei Gleichstand die größere."""
    found = [u for u in uploads if str(u.get("type") or "default") == "default" and not _usable(u, platform)]
    found.sort(key=lambda u: (_bits(u, platform), 0 if kind_of(u.get("filename"), platform) == "archive" else 1,
                              -int(u.get("size") or 0)))
    return found


def pick_upload(uploads) -> tuple:
    """(upload, platform) mit platform "windows" oder "linux"; Windows zuerst (wie GOG, E68).
    ItchError: nur im Browser spielbar (browser_only=True), nur Demos oder gar nichts Passendes."""
    rows = [u for u in uploads or [] if isinstance(u, dict)]
    for platform in ("windows", "linux"):
        found = _ranked(rows, platform)
        if found:
            return found[0], platform
    if any(str(u.get("type") or "") in BROWSER_TYPES for u in rows):
        raise ItchError(MSG_BROWSER, browser_only=True)
    demo = any(str(u.get("type") or "default") == "default" and _usable(u, p) == "demo"
               for u in rows for p in ("windows", "linux"))
    raise ItchError(MSG_DEMO if demo else MSG_NOTHING)


def is_browser_only(uploads) -> bool:
    """Gibt es nur Fassungen für den Browser (kein herunterladbares Spiel für Windows/Linux)?"""
    try:
        pick_upload(uploads)
    except ItchError as e:
        return e.browser_only
    return False


# ------------------------------------------------------------------ Windows-Installer

def installer_type(path: str) -> str:
    """inno | nsis | msi | "" (kein bekannter Installer: dann ist die .exe das Spiel)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
    except OSError:
        return ""
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or str(path).lower().endswith(".msi"):
        return "msi"                            # OLE-Verbunddatei: Windows Installer
    if not head.startswith(b"MZ"):
        return ""
    # Kennungen stehen als ASCII im Lader bzw. als UTF-16 in den Versionsangaben der .exe
    for marks, kind in (((b"Inno Setup Setup Data", b"Inno Setup"), "inno"), ((b"NullsoftInst", b"Nullsoft"), "nsis")):
        for mark in marks:
            if mark in head or mark.decode("ascii").encode("utf-16-le") in head:
                return kind
    return ""


def silent_command(kind: str, installer: str, target: str) -> list:
    """Befehl (ohne umu-run davor) für einen unsichtbaren Durchlauf in target (Linux-Pfade).
    NSIS: /D= muss der letzte Schalter sein, ohne Anführungszeichen (auch mit Leerzeichen).
    MSI: msiexec aus dem Prefix; TARGETDIR/INSTALLDIR nach Doku (unbestätigt)."""
    win = windows_dir(target)
    if kind == "inno":
        return [installer] + INNO_ARGS + ["/DIR=" + win]
    if kind == "nsis":
        return [installer, "/S", "/D=" + win]
    if kind == "msi":
        return ["msiexec", "/i", windows_dir(installer), "/qn", "/norestart", "TARGETDIR=" + win, "INSTALLDIR=" + win]
    raise InstallError("Diesen Installer kennt der PC5 Launcher nicht.")


# ------------------------------------------------------------------ Laden

def safe_name(filename: str) -> str:
    """Dateiname für die Platte (ohne Pfadteile, ohne seltsame Zeichen)."""
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    name = re.sub(r"[^\w .,()+-]", "_", name).strip(" .")
    return name[:180] or "download.bin"


def part_path(setup_dir: str, filename: str) -> str:
    return os.path.join(setup_dir, safe_name(filename) + PART)


def finish_part(path: str) -> str:
    """<datei>.part nach dem vollständigen Laden in <datei> umbenennen; Rückgabe der neue Pfad."""
    if not path.endswith(PART):
        return path
    final = path[:-len(PART)]
    os.replace(path, final)
    return final


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """Umleitungen des CDN nur auf https:// (E100): Die geladene Datei wird später ausgeführt
    (Installer oder Spiel), eine Umleitung auf http:// ließe sie unterwegs austauschen."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl or "").lower().startswith("https://"):
            raise InstallError("itch.io hat den Download auf eine unsichere Adresse umgeleitet.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


def _open(url: str, start: int = 0, ua: str = USER_AGENT):
    headers = {"User-Agent": ua}             # nie ein Authorization-Kopf: das ist das CDN
    if start:
        headers["Range"] = "bytes=%d-" % start
    return _OPENER.open(urllib.request.Request(url, headers=headers), timeout=30)   # noqa: S310


def download(url: str, part: str, progress, stop, ua: str = USER_AGENT, opener=None) -> int:
    """Lädt nach part (setzt eine vorhandene Datei fort, Range). progress(neue_bytes); stop ist ein
    threading.Event. Rückgabe: Größe der Datei. Interrupted bei Störung oder abgelaufener Adresse
    (neuer Versuch mit frischer Adresse), InstallError sonst."""
    if not str(url or "").startswith("https://"):
        raise InstallError("itch.io hat keine sichere Download-Adresse geliefert.")
    os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
    gog_install.resumable(url, part, progress, stop, opener or (lambda u, start=0: _open(u, start, ua)),
                          shop="itch.io", retry_codes=RETRY_CODES)
    return os.path.getsize(part)


def transient(e: BaseException) -> bool:
    """Lohnt ein neuer Versuch später (Netz weg, Störung, Bremse, abgelaufene Adresse; E99)? Nie bei
    fehlender Anmeldung oder einem Satz, der sich nicht von selbst ändert („nur im Browser“)."""
    if isinstance(e, Interrupted):
        return True
    return isinstance(e, ItchError) and not isinstance(e, ItchLoginNeeded) and bool(getattr(e, "transient", False))


# ------------------------------------------------------------------ Hinweis auf neuere Dateien

def _when(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    try:
        stamp = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def newer_upload(installed: dict, uploads) -> dict | None:
    """Neuere Datei für ein installiertes Spiel oder None. installed: {upload_id, build_id, updated_at,
    installed_at, platform}. Neuer heißt: dieselbe Datei mit größerer build_id oder späterem updated_at
    (sonst später als die Installation), oder die Datei ist weg und eine andere gleicher Plattform da."""
    installed = installed if isinstance(installed, dict) else {}
    rows = [u for u in uploads or [] if isinstance(u, dict)]
    try:
        _best, platform = pick_upload(rows)
    except ItchError:
        return None
    want = str(installed.get("platform") or platform)
    same = next((u for u in rows if int(u.get("id") or 0) == int(installed.get("upload_id") or -1)), None)
    if same is not None:
        if int(same.get("build_id") or 0) > int(installed.get("build_id") or 0) > 0:
            return same
        before = _when(installed.get("updated_at")) or _when(installed.get("installed_at"))
        return same if before and _when(same.get("updated_at")) > before else None
    found = _ranked(rows, want)
    return found[0] if found else None


def update_hint(installed: dict, uploads) -> str:
    """Text für die Spielseite, wenn es eine neuere Datei gibt ("" sonst)."""
    newer = newer_upload(installed, uploads)
    if newer is None:
        return ""
    version = str(newer.get("version") or "").strip()
    return ("Bei itch.io gibt es eine neuere Fassung (%s)." % version) if version else \
        "Bei itch.io gibt es eine neuere Fassung."
