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
import stat
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DOWNLOADS = "https://api.gog.com/products/%d?expand=downloads"
CHUNK = 1024 * 1024
SETUP_DIR = ".gog-setup"
USER_AGENT = "PC5-Launcher (GOG)"
INNO_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS"]
INFO_MAX = 1024 * 1024          # goggame-*.info hat ein paar KB; mehr gilt als kaputt


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


def on_disk(root: str, rel: str) -> str:
    """rel (mit /) in der Schreibweise, die auf der Platte steht. GOG schreibt Pfade wie Windows ohne Rücksicht auf
    Groß- und Kleinschreibung (Pizza Connection: „DOSBOX\\dosbox.exe“, die Datei heißt DOSBox.exe); unter Linux
    fände der Launcher sie sonst nicht. Teile, die es nicht gibt, bleiben, wie sie sind."""
    cur, out = root, []
    for part in [p for p in rel.split("/") if p]:
        if part not in (".", "..") and not os.path.lexists(os.path.join(cur, part)):
            try:
                part = next((n for n in sorted(os.listdir(cur)) if n.lower() == part.lower()), part)
            except OSError:
                pass
        out.append(part)
        cur = os.path.join(cur, part)
    return "/".join(out)


def read_info(path: str):
    """JSON einer goggame-*.info oder None. Die Datei kommt aus einem Installer unter Wine (auch aus fremden
    Archiven, E114): nur eine echte Datei, nie über einen Verweis (O_NOFOLLOW; einer auf /dev/zero füllte sonst den
    Speicher), keine FIFO (O_NONBLOCK, dann S_ISREG: sie ließe den Auftrag hängen), höchstens INFO_MAX Bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > INFO_MAX:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1                             # gehört jetzt fh
            raw = fh.read(INFO_MAX + 1)
    except OSError:
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > INFO_MAX:                     # wuchs beim Lesen
        return None
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:                          # auch kein UTF-8
        return None


def play_task(target: str, product_id: int) -> dict | None:
    """Startdatei aus goggame-<id>.info: {exe (relativ zu target, Schreibweise wie auf der Platte), args, working_dir}."""
    candidates = [os.path.join(target, "goggame-%d.info" % product_id)]
    try:
        candidates += [os.path.join(target, n) for n in sorted(os.listdir(target)) if re.fullmatch(r"goggame-\d+\.info", n)]
    except OSError:
        pass
    for path in candidates:
        info = read_info(path)
        # Die Datei kommt aus dem Installer (auch aus fremden Archiven, E114): falsche Typen wie eine kaputte Datei
        listed = info.get("playTasks") if isinstance(info, dict) else None
        if not isinstance(listed, list):
            continue
        tasks = [t for t in listed if isinstance(t, dict) and t.get("type") == "FileTask" and t.get("path")]
        task = next((t for t in tasks if t.get("isPrimary")), tasks[0] if tasks else None)
        if task:
            rel = on_disk(target, str(task["path"]).replace("\\", "/"))
            work = on_disk(target, str(task.get("workingDir") or "").replace("\\", "/"))
            return {"exe": rel, "args": str(task.get("arguments") or ""), "working_dir": work}
    return None


def windows_dir(path: str) -> str:
    """Linux-Pfad als Windows-Pfad über das Laufwerk Z: (Wine bildet / darauf ab)."""
    return "Z:" + path.replace("/", "\\")


def installer_args(target: str) -> list[str]:
    return INNO_ARGS + ["/DIR=" + windows_dir(target)]
