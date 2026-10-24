"""GOG-Spiele laden und installieren (Bauplan E68, Teil 2).

1. Installer wählen (api.gog.com/products/<id>?expand=downloads, angemeldet): Windows auf Deutsch,
   sonst Englisch, sonst die erste Sprache; nur ohne Windows-Fassung die Linux-Fassung.
2. Teile laden: Jede Datei hat einen downlink (angemeldet) auf eine signierte Adresse des CDN.
   Geladen wird nach <ziel>/.gog-setup/ auf der Spiele-Platte (setzt nach Abbruch fort, auch nach
   einem Neustart des Home: wiring_store.GogDownloads, E99), geprüft gegen die MD5 aus GOGs Prüfliste.
   Das Fortsetzen selbst (resumable) benutzt auch itch.io (store/itch_install.py, E100).
3. Installieren: Windows-Installer (Inno Setup) unsichtbar über Proton in den Prefix, den das Spiel
   später auch benutzt (/VERYSILENT … /DIR=<ziel>); Linux-Installer per unzip (data/noarch).
4. Startdatei aus goggame-<id>.info (playTasks, isPrimary); .gog-setup kommt in den Papierkorb.
Alles blockiert und gehört in einen Arbeits-Thread (wiring_store.GogDownloads).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DOWNLOADS = "https://api.gog.com/products/%d?expand=downloads"
CHUNK = 1024 * 1024
SETUP_DIR = ".gog-setup"
USER_AGENT = "PC5-Launcher (GOG)"
INNO_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS"]


class InstallError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


class Interrupted(InstallError):
    """Der Download brach ab oder der Server war kurz gestört: Ein neuer Versuch setzt fort (E99)."""


def pick_installer(product: dict, langs=("de", "en")) -> dict | None:
    """{os, language, version, files: [{id, size, downlink}], total} oder None."""
    installers = [i for i in ((product or {}).get("downloads") or {}).get("installers") or [] if isinstance(i, dict)]
    for system in ("windows", "linux"):
        mine = [i for i in installers if i.get("os") == system and i.get("files")]
        if not mine:
            continue
        best = next((i for lang in langs for i in mine if i.get("language") == lang), mine[0])
        files = [{"id": str(f.get("id") or ""), "size": int(f.get("size") or 0), "downlink": str(f.get("downlink") or "")}
                 for f in best.get("files") or [] if isinstance(f, dict) and f.get("downlink")]
        if files:
            return {"os": system, "language": str(best.get("language") or ""), "version": str(best.get("version") or ""),
                    "files": files, "total": sum(f["size"] for f in files)}
    return None


def safe_folder(title: str) -> str:
    name = re.sub(r"[^\w .,'()&+-]", "", title or "", flags=re.U).strip(" .")
    return name[:80] or "GOG-Spiel"


def free_target(root: str, title: str) -> str:
    base = os.path.join(root, safe_folder(title))
    path, n = base, 2
    while os.path.lexists(path):
        path = "%s (%d)" % (base, n)
        n += 1
    return path


def folder_bytes(folder: str) -> int:
    """Größe der gewöhnlichen Dateien direkt in folder (schon geladene Teile; Verweise zählen nicht)."""
    total = 0
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
    except OSError:
        return 0
    return total


def _open(url: str, start: int = 0):
    headers = {"User-Agent": USER_AGENT}
    if start:
        headers["Range"] = "bytes=%d-" % start
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)   # noqa: S310


def file_name(url: str) -> str:
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
    return re.sub(r"[^\w .,()+-]", "_", name).strip(" .") or "setup.bin"


def expected_md5(checksum_url: str, opener=_open) -> str:
    """MD5 aus GOGs Prüfliste (XML: <file md5="…">), "" ohne Liste."""
    if not checksum_url:
        return ""
    try:
        with opener(checksum_url) as resp:
            root = ET.fromstring(resp.read(4 * 1024 * 1024))
        return str(root.get("md5") or "").lower()
    except Exception:
        return ""


def download(url: str, dest: str, size: int, progress, cancel, opener=_open) -> None:
    """Lädt nach dest (setzt eine vorhandene Datei fort). progress(neue_bytes).
    size aus GOGs Liste ist nur ungefähr (in MB gerundet): Maßgeblich ist, was der Server meldet
    (Content-Length bzw. Content-Range); die MD5 prüft danach den Inhalt."""
    resumable(url, dest, progress, cancel, opener)


def resumable(url: str, dest: str, progress, cancel, opener=_open, shop: str = "GOG",
              retry_codes: tuple = (403, 408, 429)) -> None:
    """Datei mit Range fortsetzen (E68; E100: auch für itch.io). opener(url, start) liefert die
    Antwort; shop steht im Fehlersatz; retry_codes (und 5xx) ergeben Interrupted (neuer Versuch)."""
    import urllib.error
    have = os.path.getsize(dest) if os.path.isfile(dest) else 0
    try:
        resp = opener(url, have)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 416 and have:              # schon vollständig geladen
            return
        # Störung beim Server oder abgelaufene Download-Adresse: ein neuer Versuch holt eine frische
        error = Interrupted if e.code >= 500 or e.code in retry_codes else InstallError
        raise error("%s lieferte die Datei nicht (HTTP %d)." % (shop, e.code)) from e
    with resp:
        resumed = bool(have) and getattr(resp, "status", 200) == 206
        length = int(resp.headers.get("Content-Length") or 0) if getattr(resp, "headers", None) else 0
        total = (have if resumed else 0) + length if length else 0
        if not resumed:
            have = 0
        with open(dest, "ab" if resumed else "wb") as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise InstallError("Abgebrochen.")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                have += len(chunk)
                progress(len(chunk))
    if total and os.path.getsize(dest) != total:
        raise Interrupted("Der Download ist unvollständig. Versuche es später noch einmal.")


def md5_of(path: str) -> str:
    h = hashlib.md5()      # noqa: S324 (GOG prüft mit MD5)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def play_task(target: str, product_id: int) -> dict | None:
    """Startdatei aus goggame-<id>.info: {exe (relativ zu target), args, working_dir}."""
    candidates = [os.path.join(target, "goggame-%d.info" % product_id)]
    try:
        candidates += [os.path.join(target, n) for n in sorted(os.listdir(target)) if re.fullmatch(r"goggame-\d+\.info", n)]
    except OSError:
        pass
    for path in candidates:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue
        tasks = [t for t in info.get("playTasks") or [] if isinstance(t, dict) and t.get("type") == "FileTask" and t.get("path")]
        task = next((t for t in tasks if t.get("isPrimary")), tasks[0] if tasks else None)
        if task:
            rel = str(task["path"]).replace("\\", "/")
            return {"exe": rel, "args": str(task.get("arguments") or ""), "working_dir": str(task.get("workingDir") or "").replace("\\", "/")}
    return None


def windows_dir(path: str) -> str:
    """Linux-Pfad als Windows-Pfad über das Laufwerk Z: (Wine bildet / darauf ab)."""
    return "Z:" + path.replace("/", "\\")


def installer_args(target: str) -> list[str]:
    return INNO_ARGS + ["/DIR=" + windows_dir(target)]
