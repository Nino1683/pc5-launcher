"""umu-launcher ohne System-Paket (E105, Bauplan 8.9): die ZIP-App aus dem GitHub-Release in den eigenen
Ordner <data>/tools/umu-<VERSION>/, dazu der Verweis <data>/tools/umu auf den Ordner mit umu-run.
paths.tool("umu-run") findet ihn dann (<data>/tools/umu/umu-run).

Nur, wenn paths.tool("umu-run") nichts findet. Fassung und Prüfsumme stehen fest im Code (Muster
store/epic_account.py): Adresse und sha256 aus der GitHub-API der Lese-Runde. Laden mit Teildatei
(<archiv>.part, Fortsetzen per Range), dann sha256, dann tarfile mit Filter „data“ plus eigenen
Prüfungen in einen eigenen Zwischenordner und rename an den Zielort. Aufbau des Archivs unbestätigt:
gesucht wird genau eine Datei namens umu-run. Braucht python3 ≥ 3.10 auf dem System.

Im Prüfmodus nur von COUCH_SETUP_UMU_URL (127.0.0.1); ohne sie lädt nichts.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .. import paths

VERSION = "1.4.4"
ASSET = "umu-launcher-%s-zipapp.tar" % VERSION
URL = "https://github.com/Open-Wine-Components/umu-launcher/releases/download/%s/%s" % (VERSION, ASSET)
SIZE = 430080
SHA256 = "eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851"
CHUNK = 64 * 1024
MAX_MEMBERS = 200
MAX_BYTES = 16 * 1024 * 1024


class UmuError(Exception):
    """Ein deutscher Satz für die Oberfläche."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.message, self.code = message, code


def tools_dir() -> str:
    return os.path.join(paths.data_dir(), "tools")


def target_dir() -> str:
    return os.path.join(tools_dir(), "umu-" + VERSION)


def link_path() -> str:
    return os.path.join(tools_dir(), "umu")


def present() -> str | None:
    return paths.tool("umu-run")


def source_url() -> str | None:
    """Woher geladen wird. Im Prüfmodus nur eine lokale Adresse aus COUCH_SETUP_UMU_URL."""
    if paths.test_mode():
        url = os.environ.get("COUCH_SETUP_UMU_URL") or ""
        host = urllib.parse.urlsplit(url).hostname if url else None
        return url if host in ("127.0.0.1", "localhost") else None
    return URL


def _open(url: str, start: int = 0):
    headers = {"User-Agent": "PC5-Launcher"}
    if start:
        headers["Range"] = "bytes=%d-" % start
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)   # noqa: S310


def _download(url: str, dest: str, get, tick, stop, size: int = SIZE) -> None:
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
        if e.code == 416 and have:
            os.replace(part, dest)
            return
        raise UmuError("GitHub lieferte umu-launcher nicht (HTTP %d)." % e.code, "http") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UmuError("GitHub ist nicht erreichbar. Besteht eine Verbindung zum Internet?", "no-network") from e
    with resp:
        resumed = bool(have) and getattr(resp, "status", 200) == 206
        if not resumed:
            have = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_APPEND if resumed else os.O_TRUNC)
        with os.fdopen(os.open(part, flags, 0o600), "wb") as out:
            while True:
                if stop is not None and stop.is_set():
                    raise UmuError("Abgebrochen.", "cancelled")
                try:
                    chunk = resp.read(CHUNK)
                except OSError as e:
                    raise UmuError("Der Download brach ab. Ein neuer Versuch setzt fort.", "interrupted") from e
                if not chunk:
                    break
                have += len(chunk)
                if have > size:
                    raise UmuError("umu-launcher kam größer an als erwartet.", "damaged")
                out.write(chunk)
                if tick:
                    tick(have, size)
    os.replace(part, dest)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: str, staging: str, known: list) -> str:
    """Entpacken mit Filter „data“ und eigenen Prüfungen. known bekommt die Namen aller Einträge (für das
    Abräumen Datei für Datei). Rückgabe: Ordner (relativ) mit umu-run."""
    found = []
    with tarfile.open(archive, "r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS or sum(m.size for m in members) > MAX_BYTES:
            raise UmuError("Das Archiv von umu-launcher sieht anders aus als erwartet.", "damaged")
        for m in members:
            name = os.path.normpath(m.name)
            if name.startswith(("/", "..")) or "/../" in "/" + name + "/" or m.isdev() or m.islnk():
                raise UmuError("Das Archiv von umu-launcher enthält unerlaubte Einträge.", "damaged")
            if m.issym():
                target = os.path.normpath(os.path.join(os.path.dirname(name), m.linkname))
                if os.path.isabs(m.linkname) or target.startswith(".."):
                    raise UmuError("Das Archiv von umu-launcher enthält unerlaubte Einträge.", "damaged")
            if m.isfile() and os.path.basename(name) == "umu-run":
                found.append(name)
        if len(found) != 1:
            raise UmuError("Im Archiv von umu-launcher steckt kein eindeutiges umu-run.", "damaged")
        known.extend(os.path.normpath(m.name) for m in members if os.path.normpath(m.name) != ".")
        tar.extractall(staging, filter="data")
    path = os.path.join(staging, found[0])
    os.chmod(path, 0o755)
    return os.path.dirname(found[0])


def ensure(get: Callable | None = None, tick=None, stop=None, *, sha256: str = SHA256, size: int = SIZE) -> str:
    """umu-run bereitstellen. Blockierend (Arbeits-Thread). Liefert den Pfad; UmuError sonst. sha256 und size
    nur für Tests anders (Archiv vom lokalen Prüfserver)."""
    have = present()
    if have:
        return have
    url = source_url()
    if not url:
        raise UmuError("Im Prüfmodus lädt der Assistent nichts aus dem Netz.", "no-network")
    get = get or _open
    tools = tools_dir()
    os.makedirs(tools, mode=0o700, exist_ok=True)
    archive = os.path.join(tools, ASSET)
    staging = os.path.join(tools, ".umu-neu-%d" % os.getpid())
    known: list = []
    try:
        _download(url, archive, get, tick, stop, size)
        if _sha256(archive) != sha256:
            raise UmuError("umu-launcher kam beschädigt an. Versuche es noch einmal.", "damaged")
        os.mkdir(staging, 0o700)
        sub = _extract(archive, staging, known)
        target = target_dir()
        if os.path.lexists(target):
            raise UmuError("Der Ordner %s gibt es schon. Er bleibt, wie er ist." % target, "exists")
        os.rename(staging, target)
        staging = ""
        link = link_path()
        if os.path.islink(link):
            os.unlink(link)                     # nur ein eigener Verweis, nie ein Ordner
        if os.path.lexists(link):
            raise UmuError("%s gibt es schon und ist kein Verweis." % link, "exists")
        os.symlink(os.path.join(os.path.basename(target), sub) if sub else os.path.basename(target), link)
    except OSError as e:
        raise UmuError("umu-launcher ließ sich nicht ablegen: %s" % (e.strerror or e), "failed") from e
    finally:
        for leftover in (archive, archive + ".part"):
            try:
                if os.path.isfile(leftover) and not os.path.islink(leftover):
                    os.unlink(leftover)
            except OSError:
                pass
        if staging:
            _drop_staging(staging, known)
    found = present()
    if not found:
        raise UmuError("umu-launcher ist abgelegt, lässt sich aber nicht finden.", "failed")
    return found


def _drop_staging(staging: str, known: list) -> None:
    """Eigenen Zwischenordner abräumen (kein rekursives Löschen, SAFETY 13/E99): nur die Einträge aus dem
    eigenen Archiv (known), Dateien und Verweise per unlink, Ordner per rmdir, tiefste zuerst, dann der
    leere Zwischenordner. Liegt dort sonst etwas, bleibt der Ordner samt Inhalt liegen."""
    if not os.path.basename(staging).startswith(".umu-neu-"):
        return
    for name in sorted(set(known), key=lambda n: n.count(os.sep), reverse=True):
        path = os.path.join(staging, name)
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
        except OSError:
            pass
    try:
        os.rmdir(staging)
    except OSError:
        pass
