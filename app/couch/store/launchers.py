"""Spiele über die Launcher der Hersteller: Battle.net, Ubisoft Connect, Rockstar Games Launcher (Bauplan E94).

Diese Shops haben keine Schnittstelle zum Herunterladen für fremde Programme; laden und starten kann
nur ihr eigener Launcher (Windows, über Proton). Die Konsole hält ihn so weit wie möglich verborgen:

  * Jeder Launcher bekommt einen eigenen Prefix auf der Spiele-Platte (<Spiele>/Launcher/<Name>);
    seine Spiele landen darin (C:\\Program Files …) und damit auf der großen Platte.
  * Der Launcher wird beim ersten Gebrauch vom offiziellen Server geladen und still installiert.
  * Installieren: Rockstar lädt unsichtbar (hidden_install_args, E96). Battle.net und Ubisoft öffnen
    ihren Dialog für genau dieses Spiel (Battle.net --exec="install CODE", Ubisoft
    uplay://install/ID); einmal bestätigen. Danach lädt der Launcher im Hintergrund weiter, die
    Konsole sieht in der Registry des Prefix nach.
  * Starten: bin/couch-launcher-game startet den Launcher, löst den Start aus und wartet auf das
    Spiel; seine Fenster zeigt die Konsole dabei nicht (gs/focus.py, spec.hide_windows). Was der
    Helfer je Launcher braucht (Programme, Wartezeit, Startbefehl, Nachlauf), kommt aus dieser
    Tabelle über die Umgebung (launch/proton.py, E99).

Dieses Modul rechnet nur und liest die Platte; Prozesse startet die Verdrahtung (wiring_launchers).
Nur Standardbibliothek.
"""
from __future__ import annotations

import os
import re
import time
import urllib.request
from dataclasses import dataclass

from . import winreg

UNINSTALL = "software\\wow6432node\\microsoft\\windows\\currentversion\\uninstall"
UNINSTALL64 = "software\\microsoft\\windows\\currentversion\\uninstall"
UBI_INSTALLS = "software\\wow6432node\\ubisoft\\launcher\\installs"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"


@dataclass(frozen=True)
class Launcher:
    id: str
    name: str                 # sichtbarer Name (auch Ordnername des Prefix)
    url: str                  # offizieller Installer
    installer: str            # Dateiname des Installers
    installer_args: tuple     # still installieren
    exe: str                  # Windows-Pfad des Launchers nach der Installation
    store: str                # STORE für umu/Proton ("" = keiner)
    procs: tuple              # Programme des Launchers (klein); ihre Fenster bleiben beim Spielstart verborgen
    min_size: int             # kleinere Installer gelten als kaputt
    # Für bin/couch-launcher-game (über die Umgebung, launch/proton.py; E99: vorher führte der Helfer
    # eine eigene Kopie dieser Werte, und Änderungen hier wirkten beim echten Start nicht)
    ready_procs: tuple        # an diesen Programmen erkennt der Helfer, dass er läuft
    ready_wait: int           # danach noch so viele Sekunden, bevor der Start ausgelöst wird
    # Nachlauf nach dem Spiel, bevor der Prefix endet (wineserver -k): Ubisoft Connect und der Rockstar
    # Games Launcher gleichen Cloud-Spielstände erst nach dem Ende des Spiels ab (laut Berichten von
    # Spielern, am Gerät unbestätigt). Blizzard-Spiele speichern auf dem Server
    after_game: int = 0


LAUNCHERS = {
    "battlenet": Launcher(
        "battlenet", "Battle.net",
        "https://downloader.battle.net/download/getInstaller?os=win&installer=Battle.net-Setup.exe",
        "Battle.net-Setup.exe",
        # am 2026-10-22 im Wegwerf-Prefix geprüft: installiert ohne Klick, danach erscheint die Anmeldung
        ("--lang=deDE", "--installpath=C:\\Program Files (x86)\\Battle.net"),
        "C:\\Program Files (x86)\\Battle.net\\Battle.net.exe", "battlenet",
        ("battle.net.exe", "battle.net launcher.exe", "agent.exe", "agenthelper.exe", "blizzarderror.exe",
         "battle.net helper.exe", "blizzard uninstaller.exe"),
        1_000_000, ("battle.net.exe", "agent.exe"), 8),
    "ubisoft": Launcher(
        "ubisoft", "Ubisoft Connect",
        "https://static3.cdn.ubi.com/orbit/launcher_installer/UbisoftConnectInstaller.exe",
        "UbisoftConnectInstaller.exe", ("/S",),
        # upc.exe ist das Programm selbst; UbisoftConnect.exe startet es nur und endet nach einer Sekunde
        "C:\\Program Files (x86)\\Ubisoft\\Ubisoft Game Launcher\\upc.exe", "ubisoft",
        ("upc.exe", "ubisoftconnect.exe", "uplaywebcore.exe", "ubisoftgamelauncher.exe", "uplayservice.exe",
         "uplaycrashreporter.exe", "upc_installer.exe"),
        50_000_000, ("upc.exe",), 6, 30),
    "rockstar": Launcher(
        "rockstar", "Rockstar Games Launcher",
        "https://gamedownloads.rockstargames.com/public/installer/Rockstar-Games-Launcher.exe",
        "Rockstar-Games-Launcher.exe", ("/s", "/f"),
        "C:\\Program Files\\Rockstar Games\\Launcher\\Launcher.exe", "",
        ("launcher.exe", "launcherpatcher.exe", "socialclubhelper.exe", "rockstarservice.exe",
         "rockstarerrorhandler.exe", "rockstarsteamhelper.exe"),
        20_000_000, ("launcher.exe",), 10, 30),
}


# Argumente für den nächsten sichtbaren Start eines Launchers (Eintrag launcher:<id>): etwa
# --exec="install Fen"; launch/proton.py nimmt sie beim Bau des Startauftrags (wie apps.PENDING_OPEN)
PENDING_ARGS: dict = {}

# Vor jedem Start im Prefix eines Launchers (ein Spiel darüber oder der Launcher selbst) macht die
# Verdrahtung dort Platz oder lehnt mit LaunchError ab (wiring_launchers.LauncherStores.make_room, E99):
# fn(launcher_id, entry_id, title) -> True, wenn ein anderes Spiel der Konsole den Prefix hält (die
# Shell schließt es vor dem Start). launch/proton.py ruft es im Arbeits-Thread; None = keine Verdrahtung.
before_start = None


class LauncherError(Exception):
    """Deutscher Satz für die Oberfläche."""


# ---------------------------------------------------------------- Orte
def prefix_dir(games_root: str, launcher_id: str) -> str:
    """Prefix des Launchers auf der Spiele-Platte: <Spiele>/Launcher/<Name>."""
    return os.path.join(games_root, "Launcher", LAUNCHERS[launcher_id].name)


def exe_path(prefix: str, launcher_id: str) -> str:
    return winreg.to_linux(LAUNCHERS[launcher_id].exe, prefix)


def installed(prefix: str, launcher_id: str) -> bool:
    return os.path.isfile(exe_path(prefix, launcher_id))


def tree_usage(folder: str) -> tuple:
    """(belegt, angelegt) in Bytes: Der Rockstar Games Launcher legt die Dateien eines Spiels sofort in voller
    Größe an und füllt sie beim Laden (dünn belegte Dateien, 2026-10-22: 96,0 GB angelegt, 5,6 GB belegt)."""
    used = size = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            used += st.st_blocks * 512
            size += st.st_size
    return used, size


def tree_size(folder: str) -> tuple:
    """(Dateien, Bytes) unter folder: ändert sich das nicht mehr, ist ein Installer fertig."""
    files = total = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                pass
    return files, total


_BROWSER_KEY = re.compile(r"(?m)^\[Software\\\\Wine\\\\WineBrowser\][^\n]*\n")


def route_urls(prefix: str, handler: str) -> bool:
    """Links aus dem Prefix (ShellExecute → winebrowser) gehen an handler statt an xdg-open: In der
    Sitzung des PC5 Launchers fängt xdg-open nichts auf (Steams Link-Handler, Steam läuft nicht), und
    „Mit Google anmelden“ in Battle.net wartet ins Leere (2026-10-22). Schreibt user.reg (Schlüssel
    Software/Wine/WineBrowser, Wert "Browsers"), nur wenn der Prefix nicht läuft: der Wineserver schreibt die Datei
    beim Beenden neu. True, wenn der Eintrag stimmt."""
    from ..launch.prefix import wineserver_pids
    path = os.path.join(prefix, "user.reg")
    line = '"Browsers"="%s"' % handler.replace("\\", "\\\\").replace('"', '\\"')
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            text = fh.read()
    except OSError:
        return False
    m = _BROWSER_KEY.search(text)
    if m:
        end = text.find("\n\n", m.end())
        end = len(text) if end < 0 else end + 1
        body = text[m.end():end]
        if line + "\n" in body:
            return True
        body = re.sub(r'(?m)^"Browsers"=.*\n', "", body)
        new = text[:m.end()] + line + "\n" + body + text[end:]
    else:
        new = text + ("" if text.endswith("\n") else "\n") + "\n[Software\\\\Wine\\\\WineBrowser] %d\n%s\n" % (int(time.time()), line)
    if wineserver_pids(prefix):
        return False
    tmp = path + ".couch"
    try:
        with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(new)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


UBI_SETTINGS = os.path.join("drive_c", "users", "steamuser", "AppData", "Local", "Ubisoft Game Launcher", "settings.yaml")


def german(prefix: str, launcher_id: str) -> None:
    """Nach dem Einrichten: Ubisoft Connect auf Deutsch (es nimmt sonst en-US; Battle.net bekommt
    --lang=deDE beim Installieren, Rockstar folgt der Sprache des Systems)."""
    if launcher_id != "ubisoft":
        return
    path = os.path.join(prefix, UBI_SETTINGS)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    except OSError:
        return
    if re.search(r"(?m)^language:\s*\n\s+code:", text):
        new = re.sub(r"(?m)^(language:\s*\n\s+code:)\s*\S*", r"\1 de-DE", text, count=1)
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + "language:\n  code: de-DE\n"
    if new != text:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)


def installer_path(cache_dir: str, launcher_id: str) -> str:
    return os.path.join(cache_dir, "launchers", LAUNCHERS[launcher_id].installer)


def download_installer(launcher_id: str, cache_dir: str, progress=None, stop=None,
                       opener=urllib.request.urlopen) -> str:
    """Lädt den offiziellen Installer (neu, wenn der vorhandene älter als eine Woche ist).
    progress(bytes, total); stop: threading.Event. Rückgabe: Pfad. Wirft LauncherError."""
    launcher = LAUNCHERS[launcher_id]
    path = installer_path(cache_dir, launcher_id)
    try:
        if os.path.getsize(path) >= launcher.min_size and time.time() - os.path.getmtime(path) < 7 * 86400:
            return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = path + ".part"
    try:
        req = urllib.request.Request(launcher.url, headers={"User-Agent": USER_AGENT})
        with opener(req, timeout=60) as resp, open(part, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if stop is not None and stop.is_set():
                    raise LauncherError("Abgebrochen.")
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except LauncherError:
        _drop(part)
        raise
    except Exception as e:  # noqa: BLE001 (Netz, Platte)
        _drop(part)
        raise LauncherError("%s ließ sich nicht herunterladen: %s" % (launcher.name, e)) from e
    with open(part, "rb") as fh:
        head = fh.read(2)
    if head != b"MZ" or os.path.getsize(part) < launcher.min_size:
        _drop(part)
        raise LauncherError("Der Download von %s ist unvollständig. Bitte noch einmal versuchen." % launcher.name)
    os.replace(part, path)
    return path


def _drop(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- Befehle (Windows-Seite)
def login_args(launcher_id: str) -> list:
    """Der Launcher selbst, sichtbar (Anmeldung, Einstellungen)."""
    return [exe_win(launcher_id)]


BNET_LOGS = os.path.join("drive_c", "users", "steamuser", "AppData", "Local", "Battle.net", "Logs")
_BNET_LINE = re.compile(r"^[IDWE] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def login_after(prefix: str, launcher_id: str, since: float) -> bool:
    """Hat sich jemand im Launcher angemeldet, nachdem er seit since (Unix-Zeit) die Anmeldung zeigte?
    Dann ist ein Auftrag von vorher (--exec=install …) verloren: Battle.net nimmt ihn beim Start an,
    führt ihn ohne Anmeldung aber nie aus (2026-10-22). Nur Battle.net (sein Protokoll, Zeiten in UTC)."""
    if launcher_id != "battlenet":
        return False
    asked = False
    for line in _bnet_log_lines(prefix, since):
        if "UAuth: browser state changed: Login" in line:
            asked = True
        elif asked and ("hosted login complete" in line or "New auth token received. tokenType=Default" in line):
            return True
    return False


def _bnet_log_lines(prefix: str, since: float) -> list:
    """Zeilen des jüngsten Battle.net-Protokolls ab since (Unix-Zeit; die Zeiten darin sind UTC)."""
    import calendar
    folder = os.path.join(prefix, BNET_LOGS)
    try:
        logs = sorted((n for n in os.listdir(folder) if n.startswith("battle.net-") and n.endswith(".log")),
                      key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        if not logs or os.path.getmtime(os.path.join(folder, logs[-1])) < since:
            return []
        with open(os.path.join(folder, logs[-1]), "rb") as fh:
            fh.seek(max(0, os.path.getsize(fh.name) - 4 * 1024 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        m = _BNET_LINE.match(line)
        if m and calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + 1 >= since:
            out.append(line)
    return out


def bnet_install(prefix: str, uid: str, since: float) -> dict | None:
    """Lädt Battle.net dieses Spiel (seit since)? {"total": Bytes, "folder": Linux-Pfad} oder None.
    In die Registry trägt es das Spiel erst ganz am Ende ein; bis dahin steht nur im Protokoll
    „Install started: <uid>“ und einmal die Größe samt Zielordner (2026-10-22, Hearthstone)."""
    started, total, folder = False, 0, ""
    tag = "InstallState (%s):" % uid
    for line in _bnet_log_lines(prefix, since):
        if line.endswith("Install started: %s" % uid):
            started = True
        elif tag in line:
            m = re.search(r"\bbytesToDownload=(\d+)", line)
            if m:
                total = int(m.group(1))
            m = re.search(r"\binstallPath=(.+?)(?: [a-zA-Z]+=|$)", line)
            if m:
                folder = winreg.to_linux(m.group(1).strip().replace("/", "\\"), prefix)
    return {"total": total, "folder": folder} if started else None


RGL_LOG = os.path.join("drive_c", "users", "steamuser", "Documents", "Rockstar Games", "Launcher", "launcher.log")
_RGL_RESUME = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\] .*\[updatequeue\] Updating (.+?) to build")
_RGL_REQUEST = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\] .*Install requested: (\S+) at (.+?)\s*$")


def rockstar_install(prefix: str, code: str, since: float) -> dict | None:
    """Lädt der Rockstar Games Launcher dieses Spiel (seit since)? {"folder": Linux-Ordner} oder None.
    Ins Protokoll schreibt er beim neuen Download „Install requested: <code> at <Ordner>“, beim
    Fortsetzen „[updatequeue] Updating <Titel> to build …“ (Ortszeit), die Größe nicht; in die Registry
    kommt das Spiel erst am Ende (2026-10-22, GTA V Enhanced)."""
    from .rockstar import TITLES
    info = TITLES.get(str(code)) or {}
    sub, title = str(info.get("folder") or ""), str(info.get("title") or "")
    try:
        with open(os.path.join(prefix, RGL_LOG), "rb") as fh:
            fh.seek(max(0, os.path.getsize(fh.name) - 2 * 1024 * 1024))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    default = winreg.to_linux("C:\\Program Files\\Rockstar Games" + ("\\" + sub if sub else ""), prefix)
    for line in reversed(lines):
        m = _RGL_REQUEST.match(line)
        if m and m.group(2) == str(code):
            if time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + 1 < since:
                return None
            return {"folder": winreg.to_linux(m.group(3).rstrip("\\") + ("\\" + sub if sub else ""), prefix)}
        m = _RGL_RESUME.match(line)
        if m and title and m.group(2) == title:
            if time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + 1 < since:
                return None
            return {"folder": default}
    return None


def hidden_install_args(launcher_id: str, code) -> list | None:
    """Installieren, ohne dass man den Launcher sieht: [exe, args …] oder None (nur mit Dialog).
    Rockstar: „-install=<code>“ meldet an (automatisch) und lädt ohne Fenster, auch ein angefangener
    Download geht weiter (2026-10-22, live mit GTA V Enhanced). Battle.net und Ubisoft brauchen einen Klick."""
    if launcher_id == "rockstar" and re.fullmatch(r"[a-z0-9_]{2,40}", str(code)):
        return [exe_win(launcher_id), "-enableFullMode", "-install=%s" % code]
    return None


def install_args(launcher_id: str, code) -> list:
    """Der Installationsdialog des Launchers für dieses Spiel (einmal bestätigen)."""
    exe = exe_win(launcher_id)
    if launcher_id == "battlenet":
        return [exe, "--exec=install %s" % code]
    if launcher_id == "ubisoft" and str(code).isdigit():
        return [exe, "uplay://install/%s" % code]
    return [exe]          # Rockstar: kein Befehl zum Installieren, nur das Fenster


def launch_args(launcher_id: str, code, folder_win: str = "") -> list:
    """Spiel starten über den laufenden Launcher (bin/couch-launcher-game bekommt es als
    COUCH_LAUNCHER_START, launch/proton.py)."""
    exe = exe_win(launcher_id)
    if launcher_id == "battlenet":
        return [exe, "--exec=launch %s" % code]
    if launcher_id == "ubisoft":
        return [exe, "uplay://launch/%s/0" % code]
    return [exe, "-launchTitleInFolder", folder_win]


def exe_win(launcher_id: str) -> str:
    return LAUNCHERS[launcher_id].exe


# ---------------------------------------------------------------- Installierte Spiele
ROCKSTAR_SELF = frozenset({"launcher", "socialclub"})   # Einträge des Launchers selbst, keine Spiele


def installed_games(prefix: str, launcher_id: str, codes: dict | None = None) -> list[dict]:
    """Was der Launcher im Prefix installiert hat: [{code, title, folder (Linux), folder_win, complete}].
    codes: Battle.net-uid → Launcher-Code (aus battlenet.CODES); complete None = unbekannt."""
    reg = winreg.read(os.path.join(prefix, "system.reg"), (UNINSTALL, UNINSTALL64, UBI_INSTALLS))
    uninstall = {**winreg.subkeys(reg, UNINSTALL64), **winreg.subkeys(reg, UNINSTALL)}
    out: list[dict] = []
    if launcher_id == "battlenet":
        for values in uninstall.values():
            m = re.search(r"--uid=(\S+)", str(values.get("uninstallstring") or ""))
            uid = m.group(1).strip('"') if m else ""
            if not uid or uid.startswith("battle.net") or uid == "agent":
                continue
            folder_win = str(values.get("installlocation") or "")
            folder = winreg.to_linux(folder_win, prefix)
            code = (codes or {}).get(uid) or (codes or {}).get(uid.split("_")[0], "")
            out.append({"code": code or uid, "uid": uid, "title": str(values.get("displayname") or uid),
                        "folder": folder, "folder_win": folder_win,
                        "complete": os.path.isfile(os.path.join(folder, ".build.info")) or None})
    elif launcher_id == "ubisoft":
        names = {}
        for key, values in uninstall.items():
            m = re.match(r"uplay install (\d+)$", key)
            if m:
                names[m.group(1)] = str(values.get("displayname") or "")
        for game_id, values in winreg.subkeys(reg, UBI_INSTALLS).items():
            folder_win = str(values.get("installdir") or "").replace("/", "\\")
            folder = winreg.to_linux(folder_win, prefix)
            if not game_id.isdigit() or not folder:
                continue
            out.append({"code": int(game_id), "title": names.get(game_id) or "", "folder": folder,
                        "folder_win": folder_win.rstrip("\\"), "complete": _ubi_complete(folder)})
    elif launcher_id == "rockstar":
        for values in uninstall.values():
            m = re.search(r"-uninstall=(\S+)", str(values.get("uninstallstring") or ""))
            # der Launcher trägt sich nach dem ersten Start selbst so ein (-uninstall=launcher, 2026-10-22)
            if not m or m.group(1).strip('"').lower() in ROCKSTAR_SELF:
                continue
            folder_win = str(values.get("installlocation") or "").rstrip("\\")
            out.append({"code": m.group(1).strip('"'), "title": str(values.get("displayname") or m.group(1)),
                        "folder": winreg.to_linux(folder_win, prefix), "folder_win": folder_win, "complete": None})
    return out


def _ubi_complete(folder: str):
    """Ubisoft Connect setzt das erste Byte von uplay_install.state auf 0x0A, wenn das Spiel fertig ist."""
    try:
        with open(os.path.join(folder, "uplay_install.state"), "rb") as fh:
            return fh.read(1) == b"\x0a"
    except OSError:
        return None


def main_exe(folder: str, hint: str = "") -> str:
    """Startdatei relativ zum Spielordner: hint, wenn es sie gibt, sonst die größte .exe (ohne
    Hilfsprogramme) in den oberen zwei Ebenen; "" wenn nichts da ist."""
    if hint and os.path.isfile(os.path.join(folder, hint)):
        return hint
    best, size = "", -1
    skip = re.compile(r"(?i)(unins|setup|crash|report|helper|launcher|redist|vc_|dxsetup|install|update|easyanticheat|battleye)")
    for root, dirs, files in os.walk(folder):
        depth = root[len(folder):].count(os.sep)
        if depth >= 2:
            dirs[:] = []
        for name in files:
            if not name.lower().endswith(".exe") or skip.search(name):
                continue
            try:
                s = os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
            if s > size:
                best, size = os.path.relpath(os.path.join(root, name), folder), s
    return best
