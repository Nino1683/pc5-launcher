"""Proton-Builds prüfen, reparieren und installieren (Bauplan E101, E102).

E101 (Wunsch 2026-10-23: „Wenn eine beschädigte Proton-Version vorliegt, soll sie neu heruntergeladen
und installiert werden“). Anlass am selben Tag: In proton-cachyos-11.0-20261015-slr-x86_64_v3 hatten
gleichzeitige Läufe zweier Proton-Versionen auf einem Prefix 1214 PE-DLLs unter
files/lib/wine/{x86_64,i386}-windows mit denen von 11.0-20260703 überschrieben (system32 des Prefix
besteht aus Verweisen in den Proton-Ordner; Wine schreibt beim Abgleich des Prefix durch sie hindurch,
die Dateien bekommen dabei die Uhrzeit des Schreibens). winevulkan.dll (alt) passte nicht mehr zu
winevulkan.so (neu): Absturz in enum_host_instance_extension_properties, Spiele endeten nach 3 s mit
Code 3.
E102 (Wunsch 2026-10-23: „… unter Einstellungen › System › Updates auch Proton-Versionen herunterladen
können, auch eine Auswahl an Proton-Versionen, die es so gibt und nützlich sind“): CATALOG,
available(), install(), installed(). Entfernen gehört nicht hierher (geprüfter Papierkorb, E17).

Ablauf
  check()        Erkennen, schnell: mit Manifest Größe und Zeit je Datei (Hash nur bei Zweifel), ohne
                 Manifest Zeitstempel (Dateien unter files/lib*/wine/*-windows|*-unix, die deutlich
                 jünger sind als der Release selbst).
  source_for()   Herkunft aus dem Ordnernamen (GitHub-Release), ohne Netz.
  repair()       laden nach <cache>/proton-repair/<name>/ (setzt .part fort) → sha512 bzw. sha256 prüfen →
                 sicher entpacken in <compat_dir>/.couch-proton-<zufall>/ (dasselbe Dateisystem) → erst
                 wenn kein Prozess den Ordner nutzt: alten Ordner nach <backup_dir>/<name>-beschaedigt-
                 <JJJJMMTT-HHMM> (war er heil: -bisherig-) VERSCHIEBEN, neuen per rename einsetzen →
                 Manifest schreiben. Nutzt schon beim Anstoßen ein Prozess den Ordner, wird gar nicht
                 erst geladen und entpackt (E101: sonst bei jedem Spielstart 1,7 GB auf die Platte).
  install()      derselbe Weg für einen Ordner, den es noch nicht gibt.
  healthy_alternative()  Ausweichen, wenn Reparieren nicht geht.
  available(), installed()  Listen für die Einstellungsseite „Proton-Versionen“.

Löschen: Ein Proton-Ordner wird hier nie gelöscht, nur verschoben (SAFETY Regel 13). Eigene Einzeldateien
(.part, Archiv, Sperrdatei, Marker) entfernt dieses Modul selbst; den eigenen Zwischenordner leert nach
einem Fehler ausschließlich trash.remove_own_dir (die eine Löschstelle des Produkts).

Alles blockiert (Netz, Platte, /proc) und gehört in einen Arbeits-Thread. Gemessen am 2026-10-23 an
den Ordnern des Entwicklungsrechners (nur lesend): check() ohne Manifest 8 ms, mit Manifest 5 ms (heil) bzw. 0,16 s (1214
Dateien nachgerechnet), scan_manifest() 0,44 s (1799 Dateien, 900 MB), users()/in_use() 20 ms. Mit
einem Archiv, das aus dem heilen 11.0-20261015 gepackt war, im Scratch (tmpfs): sha512 0,5 s, Entpacken
11,5 s, install() und repair() je 12 s, Ergebnis gleich dem Original (diff -r). Auf der echten Platte
und beim Laden (341 MB) dauert es länger.

Grenzen (unbestätigt, nur mit Attrappen geprüft): echter Download von GitHub über diese Funktionen, die
Heuristik für GE-/UMU-/EM-Builds (nur an CachyOS-Ordnern gemessen), in_use() für Prozesse in Containern
der Steam-Laufzeit (dort sollten dieselben Pfade sichtbar sein), dass Steam versteckte Ordner
(.couch-proton-*) in compatibilitytools.d nicht als Werkzeug liest. Überschreiben mit erhaltenen
Zeitstempeln (cp -a, rsync -t) sieht die Heuristik nicht; dafür gibt es das Manifest.
"""
from __future__ import annotations

import errno
import fcntl
import gzip
import hashlib
import json
import lzma
import os
import posixpath
import re
import secrets
import stat
import tarfile
import time
import urllib.error
import urllib.request
import zlib
from typing import Callable

from . import jsontext, paths
from .installer import trash

USER_AGENT = "PC5-Launcher (Proton)"
GITHUB_DL = "https://github.com/%s/releases/download/%s/%s"
GITHUB_API = "https://api.github.com/repos/%s/releases?per_page=%d"
CHUNK = 1024 * 1024
CACHE_SUB = "proton-repair"            # unter cache_dir: <name>/ (Archiv, .part, Sperre) und releases.json
STAGING_PREFIX = ".couch-proton-"      # Zwischenordner direkt in compat_dir (versteckt: Steam liest nur sichtbare)
STAGING_MARKER = ".couch-proton-staging"
STAGING_RE = re.compile(r"^" + re.escape(STAGING_PREFIX) + r"([0-9a-f]{32})$")
BACKUP_FALLBACK = ".couch-proton-backup"   # liegt backup_dir auf einem anderen Dateisystem: Sicherung hierhin
BACKUP_TAG = "-beschaedigt-"
BACKUP_TAG_OK = "-bisherig-"         # „Neu laden“ einer heilen Version: nichts war beschädigt (E101)
READY_MARKER = ".couch-proton-ready"   # im Zwischenordner: Inhalt geprüft und vollständig, Einsetzen beginnt

SLACK_S = 3600                 # so viel jünger als der Release darf eine Datei sein (Erkennung ohne Manifest)
MIN_CHANGED = 5                # ab so vielen jüngeren Dateien gilt der Ordner als beschädigt
RESERVE = 1 << 30              # so viel bleibt beim Entpacken auf dem Ziel frei
DL_RESERVE = 256 << 20         # so viel bleibt beim Laden im Cache frei
UNPACK_FACTOR = 6              # entpackt / Archiv (gemessen: CachyOS 341 MB xz → 1,69 GB)
MAX_UNPACKED = 16 << 30        # Schutz gegen Archiv-Bomben
BUSY_WAIT_S = 10.0             # so lange wartet repair(), bis ein Wineserver von selbst endet (Vorgabe 3 s)
TTL_S = 6 * 3600               # Zwischenspeicher der Release-Listen
MAX_JSON = 4 * 1024 * 1024
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
# Was zum Release gehört und nie durch einen Prefix beschrieben wird: Bezug für die Zeitstempel
_RELEASE_FILES = ("version", "proton", "toolmanifest.vdf", "compatibilitytool.vdf")
_RELEASE_DIRS = ("files/bin", "files/share/wine")

MSG_STOPPED = "Abgebrochen. Ein neuer Versuch setzt den Download fort."

# ---------------------------------------------------------------- Familien (E102)
# Geprüft am 2026-10-23 über die GitHub-API und die ersten 128 KB je eines Archivs: Der Ordner im Archiv
# heißt bei allen vier wie das Archiv ohne Endung (EM mit führendem „./“). GitHub nennt seit 2025 je
# Datei zusätzlich einen sha256 („digest“); ohne eigene Prüfsummen-Datei gilt der.
CATALOG = (
    {"family": "proton-cachyos", "label": "Proton-CachyOS", "repo": "CachyOS/proton-cachyos",
     "asset": r"^(proton-cachyos-[0-9A-Za-z.]+-\d{8}-slr-%(arch)s)\.tar\.xz$", "sum": ".sha512sum", "algo": "sha512",
     "listed": True, "default": True,
     # E104: ohne Bezug auf einen bestimmten Rechner; family_text() setzt auf Prozessoren ohne v3 die passende Bauart ein
     "text": "Empfohlen. Auf Leistung getrimmt, für Prozessoren mit x86-64-v3 gebaut, kann FSR 4 nachladen. "
             "Erscheint alle paar Wochen.", "arch_text": "x86-64-v3"},
    {"family": "ge-proton", "label": "GE-Proton", "repo": "GloriousEggroll/proton-ge-custom",
     "asset": r"^(GE-Proton\d+-\d+(?:-x86_64)?)\.tar\.gz$", "sum": ".sha512sum", "algo": "sha512",
     "listed": True, "default": False,
     "text": "Von GloriousEggroll: viele Korrekturen für einzelne Spiele und Codecs für Videos in Spielen. "
             "Gut, wenn ein Spiel mit CachyOS hakt. Erscheint oft."},
    {"family": "umu-proton", "label": "UMU-Proton", "repo": "Open-Wine-Components/umu-proton",
     "asset": r"^(UMU-Proton-[0-9A-Za-z.-]+)\.tar\.gz$", "sum": ".sha512sum", "algo": "sha512",
     "listed": True, "default": False,
     "text": "Vorgabe von umu-run, nah an Valves Proton. Ändert sich selten."},
    {"family": "proton-em", "label": "Proton-EM", "repo": "BananaWorks07/Proton",
     "asset": r"^(proton-EM-[0-9A-Za-z.-]+)\.tar\.xz$", "sum": ".sha256sum", "algo": "sha256",
     "listed": False, "default": False,
     "text": "Experimentell, Schwerpunkt FSR 4 und HDR. Seit April 2026 keine neue Fassung; nur auf Wunsch."},
)
_FAMILY = {c["family"]: c for c in CATALOG}

# Ordnername → Herkunft. proton-cachyos-<ver>-<datum>[-<art>]-<arch>  →  Tag cachyos-<ver>-<datum>[-<art>]
_CACHY = re.compile(r"^proton-(cachyos-\d+(?:\.\d+)*-\d{8}(?:-[a-z]+)?)-(x86_64(?:_v[234])?|arm64)$")
_GE = re.compile(r"^(GE-Proton\d+-\d+)(-x86_64|-aarch64)?$")
_UMU = re.compile(r"^UMU-Proton-\d+(?:\.\d+)*-[0-9A-Za-z.]+$")
_EM = re.compile(r"^proton-(EM-\d+(?:\.\d+)*-[0-9A-Za-z.-]+)$")


class ProtonError(Exception):
    """Ein deutscher Satz für die Oberfläche; code ein festes Kürzel."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.message, self.code = message, code


def family_of(name: str) -> str:
    """Familie eines Ordnernamens ("" = unbekannt)."""
    if _CACHY.match(name or ""):
        return "proton-cachyos"
    if _GE.match(name or ""):
        return "ge-proton"
    if _UMU.match(name or ""):
        return "umu-proton"
    if _EM.match(name or ""):
        return "proton-em"
    return ""


def version_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name or ""))


def valid_name(name) -> bool:
    """Taugt name als Ordnername einer Proton-Version (kein Pfad, nicht versteckt)? Für die
    Verdrahtung, bevor sie einen Auftrag annimmt (E101/E102)."""
    return isinstance(name, str) and bool(_NAME.match(name))


def label(name: str) -> str:
    """Name für die Oberfläche: „Proton-CachyOS 11.0-20261015“, „GE-Proton 11-11“; unbekannt = Ordnername."""
    return _label(name)


# ---------------------------------------------------------------- vorgeschlagene Orte
def manifest_dir() -> str:
    """Manifeste: <data>/proton-manifests/<name>.json (klein, ~200 KB je Build)."""
    return os.path.join(paths.data_dir(), "proton-manifests")


def backup_dir() -> str:
    """Sicherungen beschädigter Ordner (wie bei der Sicherung von Hand am 2026-10-23); im Prüfmodus im Käfig."""
    return paths.user_place(os.path.join("Games", "backups", "proton"))


def cache_root() -> str:
    """Basis für Downloads (darunter proton-repair/<name>/)."""
    return paths.cache_dir()


# ================================================================ 1 Erkennen

def _wine_roots(tool_dir: str) -> list[str]:
    files = os.path.join(tool_dir, "files")
    try:
        names = sorted(n for n in os.listdir(files) if n.startswith("lib"))
    except OSError:
        return []
    return [os.path.join(files, n, "wine") for n in names if os.path.isdir(os.path.join(files, n, "wine"))]


def _walk_files(root: str, base: str) -> list[tuple[str, os.stat_result]]:
    """Alle Einträge außer Ordnern unter root (lstat, folgt keinem Verweis), Pfad relativ zu base."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames:                          # Verweise auf Ordner zählen als Eintrag
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                out.append((os.path.relpath(p, base), os.lstat(p)))
        for n in filenames:
            p = os.path.join(dirpath, n)
            try:
                out.append((os.path.relpath(p, base), os.lstat(p)))
            except OSError:
                continue
    return out


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _when(ts: float) -> str:
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def _read_small(path: str, limit: int = 4096) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def manifest_path(manifest_dir_: str, name: str) -> str:
    return os.path.join(manifest_dir_, name + ".json")


def load_manifest(manifest_dir_: str | None, name: str) -> dict | None:
    """{"name", "version", "made", "files": {rel: [größe, sha256, mtime]}} oder None.
    Ein reines {rel: [größe, sha256]} (ohne Hülle) wird ebenso angenommen."""
    if not manifest_dir_ or not _NAME.match(name or ""):
        return None
    try:
        with open(manifest_path(manifest_dir_, name), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files") if isinstance(data.get("files"), dict) else data
    files = {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, list) and len(v) >= 2}
    if not files:
        return None
    return {"name": data.get("name", name), "version": data.get("version"), "made": data.get("made"), "files": files}


def scan_manifest(tool_dir: str) -> dict:
    """Manifest eines Ordners berechnen (liest alle Dateien unter files/lib*/wine: ~1–3 s)."""
    files = {}
    for root in _wine_roots(tool_dir):
        for rel, st in _walk_files(root, tool_dir):
            if stat.S_ISLNK(st.st_mode):
                files[rel] = [-1, "link:" + os.readlink(os.path.join(tool_dir, rel)), int(st.st_mtime)]
            elif stat.S_ISREG(st.st_mode):
                files[rel] = [st.st_size, _sha256(os.path.join(tool_dir, rel)), int(st.st_mtime)]
    return {"name": os.path.basename(os.path.normpath(tool_dir)),
            "version": _read_small(os.path.join(tool_dir, "version")).strip(),
            "made": int(time.time()), "files": files}


def _write_json_atomic(path: str, obj) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    tmp = os.path.join(folder, ".%s.%s.tmp" % (os.path.basename(path), secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(jsontext.dumps(obj, separators=(",", ":")).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)                          # eigene Zwischendatei
        except OSError:
            pass
        raise


def make_manifest(tool_dir: str, manifest_dir_: str, manifest: dict | None = None) -> dict:
    """Schreibt <manifest_dir>/<name>.json atomar. Nur für einen Ordner aufrufen, der frisch aus einem
    geprüften Archiv kommt (repair/install tun das selbst); ein Manifest eines schon beschädigten
    Ordners schriebe den Schaden fest. Rückgabe: {path, files}."""
    data = manifest or scan_manifest(tool_dir)
    name = data.get("name") or os.path.basename(os.path.normpath(tool_dir))
    if not _NAME.match(name):
        raise ProtonError("Unerwarteter Ordnername: %s" % name, "bad-name")
    path = manifest_path(manifest_dir_, name)
    _write_json_atomic(path, dict(data, name=name))
    return {"path": path, "files": len(data.get("files") or {})}


def _result(ok: bool, reason: str, files: int, checked: int, method: str, sample=None, newest=None) -> dict:
    return {"ok": ok, "reason": reason, "files": files, "checked": checked, "method": method,
            "sample": list(sample or [])[:5], "newest": newest}


def _check_manifest(tool_dir: str, man: dict) -> dict:
    files = man["files"]
    bad, missing, hashed = [], [], 0
    for rel, rec in files.items():
        parts = rel.split("/")
        if rel.startswith("/") or ".." in parts:
            continue                                  # einem Manifest wird nicht blind geglaubt
        path = os.path.join(tool_dir, rel)
        try:
            st = os.lstat(path)
        except OSError:
            missing.append(rel)
            continue
        size, digest = rec[0], rec[1]
        mtime = rec[2] if len(rec) > 2 else None
        if size == -1:                                # Verweis
            if not stat.S_ISLNK(st.st_mode) or "link:" + os.readlink(path) != digest:
                bad.append(rel)
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            bad.append(rel)
            continue
        if mtime is not None and int(st.st_mtime) == int(mtime):
            continue
        hashed += 1                                   # gleiche Größe, andere Zeit: nur hier rechnen
        try:
            if _sha256(path) != digest:
                bad.append(rel)
        except OSError:
            bad.append(rel)
    if missing or bad:
        parts = []
        if bad:
            parts.append("%d Dateien weichen vom Auslieferungszustand ab" % len(bad))
        if missing:
            parts.append("%d fehlen" % len(missing))
        return _result(False, "Proton-Ordner ist beschädigt: %s (z. B. %s)." % (", ".join(parts), (bad + missing)[0]),
                       len(bad) + len(missing), len(files), "manifest", bad + missing)
    extra = " (%d per Prüfsumme nachgerechnet)" % hashed if hashed else ""
    return _result(True, "Proton-Ordner ist unversehrt: %d Dateien geprüft%s." % (len(files), extra),
                   0, len(files), "manifest")


def _release_time(tool_dir: str) -> float | None:
    """Neueste Zeit der Dateien, die zum Release gehören und nie durch einen Prefix beschrieben werden."""
    times = []
    for n in _RELEASE_FILES:
        try:
            times.append(os.lstat(os.path.join(tool_dir, n)).st_mtime)
        except OSError:
            pass
    for d in _RELEASE_DIRS:
        try:
            with os.scandir(os.path.join(tool_dir, d)) as it:
                times += [e.stat(follow_symlinks=False).st_mtime for e in it if e.is_file(follow_symlinks=False)]
        except OSError:
            pass
    return max(times) if times else None


def _check_times(tool_dir: str) -> dict:
    entries = []
    for root in _wine_roots(tool_dir):
        try:
            subs = sorted(n for n in os.listdir(root) if n.endswith("-windows") or n.endswith("-unix"))
        except OSError:
            continue
        for sub in subs:
            folder = os.path.join(root, sub)
            if os.path.islink(folder) or not os.path.isdir(folder):
                continue
            entries += _walk_files(folder, tool_dir)
    if not entries:
        return _result(False, "Im Proton-Ordner fehlen die Wine-Bibliotheken (files/lib/wine).", 0, 0, "zeitstempel")
    ref = _release_time(tool_dir)
    if ref is None:                                   # ohne Bezug: Median der Bibliotheken selbst
        times = sorted(st.st_mtime for _, st in entries)
        ref = times[len(times) // 2]
    young = sorted(((st.st_mtime, rel) for rel, st in entries if st.st_mtime > ref + SLACK_S), reverse=True)
    if len(young) >= MIN_CHANGED:
        newest = young[0][0]
        return _result(False, "Proton-Ordner ist beschädigt: %d Dateien unter files/lib/wine wurden nach der "
                              "Auslieferung überschrieben (zuletzt am %s), z. B. %s."
                       % (len(young), _when(newest), young[0][1]),
                       len(young), len(entries), "zeitstempel", [r for _, r in young], newest)
    note = " %d Datei(en) jünger, unter der Schwelle." % len(young) if young else ""
    return _result(True, "Keine Auffälligkeiten: %d Dateien geprüft (nur Zeitstempel).%s" % (len(entries), note),
                   len(young), len(entries), "zeitstempel", [r for _, r in young])


def check(tool_dir: str, manifest_dir=None) -> dict:
    """Ist der Proton-Ordner heil? Nur lesend, schnell (stat; Hash nur bei Zweifel).

    Rückgabe {ok, reason (deutscher Satz), files (beanstandete Dateien), checked, method
    ("manifest" | "zeitstempel"), sample (bis 5 relative Pfade), newest (Zeit der jüngsten oder None)}."""
    real = os.path.realpath(tool_dir)
    name = os.path.basename(real)
    if not os.path.isdir(real):
        return _result(False, "Der Proton-Ordner fehlt (%s)." % tool_dir, 0, 0, "")
    if not os.path.isfile(os.path.join(real, "proton")):
        return _result(False, "Im Proton-Ordner fehlt das Startskript „proton“.", 0, 0, "")
    if not _wine_roots(real):
        return _result(False, "Im Proton-Ordner fehlen die Wine-Bibliotheken (files/lib/wine).", 0, 0, "")
    man = load_manifest(manifest_dir, name)
    if man is not None:
        version = _read_small(os.path.join(real, "version")).strip()
        if man.get("version") and version and man["version"] != version:
            res = _check_times(real)              # Manifest gehört zu einer anderen Fassung
            res["reason"] += " Das Manifest gehört zu einer anderen Fassung und wurde nicht benutzt."
            return res
        return _check_manifest(real, man)
    return _check_times(real)


# ================================================================ 2 Quelle

def _src(family: str, tag: str, asset: str, top: str) -> dict:
    fam = _FAMILY[family]
    url = GITHUB_DL % (fam["repo"], tag, asset)
    stem = re.sub(r"\.tar\.(xz|gz)$", "", asset)
    sha_url = GITHUB_DL % (fam["repo"], tag, stem + fam["sum"])
    return {"url": url, "sha512_url": sha_url if fam["algo"] == "sha512" else "", "sha_url": sha_url,
            "sha_algo": fam["algo"], "sha_value": "", "kind": family, "repo": fam["repo"], "tag": tag,
            "asset": asset, "top": top, "size": 0}


def source_for(name: str) -> dict | None:
    """Herkunft eines Proton-Ordners, nur aus dem Namen (kein Netz):
    {url, sha512_url, sha_url, sha_algo, kind (Familie), repo, tag, asset, top (Ordner im Archiv),
    alt: [weitere Kandidaten]} oder None (unbekannt: dann bleibt nur healthy_alternative)."""
    if not _NAME.match(name or ""):
        return None
    m = _CACHY.match(name)
    if m:
        src = _src("proton-cachyos", m.group(1), name + ".tar.xz", name)
        src["alt"] = []
        return src
    m = _GE.match(name)
    if m:
        tag = m.group(1)
        src = _src("ge-proton", tag, name + ".tar.gz", name)
        # Seit GE-Proton11-4 tragen die Archive die Bauart im Namen (Ordner darin ebenso)
        src["alt"] = [] if m.group(2) else [_src("ge-proton", tag, name + "-x86_64.tar.gz", name + "-x86_64")]
        return src
    if _UMU.match(name):
        src = _src("umu-proton", name, name + ".tar.gz", name)
        src["alt"] = []
        return src
    m = _EM.match(name)
    if m:
        src = _src("proton-em", m.group(1), name + ".tar.xz", name)
        src["alt"] = []
        return src
    return None


# ================================================================ Netz

def _open(url: str, start: int = 0):
    headers = {"User-Agent": USER_AGENT}
    if url.startswith("https://api.github.com/"):
        headers["Accept"] = "application/vnd.github+json"
    if start:
        headers["Range"] = "bytes=%d-" % start
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)   # noqa: S310


def _net_error(e: Exception, what: str = "GitHub") -> ProtonError:
    if isinstance(e, urllib.error.HTTPError):
        try:
            e.close()                               # hält sonst die Verbindung offen
        except Exception:  # noqa: BLE001
            pass
        if e.code == 404:
            return ProtonError("%s kennt diese Datei nicht (HTTP 404)." % what, "not-found")
        if e.code in (403, 429):
            return ProtonError("%s lässt gerade keine Abfragen zu (zu viele in kurzer Zeit). Später noch einmal."
                               % what, "rate-limit")
        return ProtonError("%s lieferte die Datei nicht (HTTP %d)." % (what, e.code), "http")
    return ProtonError("%s ist nicht erreichbar. Besteht eine Verbindung zum Internet?" % what, "network")


def _fetch(url: str, get, limit: int) -> bytes:
    try:
        with get(url) as resp:
            data = resp.read(limit + 1)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _net_error(e) from e
    if len(data) > limit:
        raise ProtonError("GitHub lieferte eine unerwartet große Antwort.", "bad-reply")
    return data


def _parse_sum(text: str, asset: str, algo: str) -> str:
    """Hash aus einer .sha512sum/.sha256sum („<hex>  <datei>“); "" wenn keiner zur Datei passt."""
    width = 128 if algo == "sha512" else 64
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts or len(parts[0]) != width or not re.fullmatch(r"[0-9a-fA-F]+", parts[0]):
            continue
        fname = parts[1].lstrip("*") if len(parts) > 1 else ""
        if not fname or os.path.basename(fname) == asset:
            return parts[0].lower()
    return ""


def _free(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _size(path: str) -> int:
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def _download(url: str, dest: str, get, tick: Callable[[int, int], None], stop) -> None:
    """Lädt nach dest; setzt <dest>.part per Range fort. Erst vollständig wird umbenannt."""
    part = dest + ".part"
    have = _size(part)
    try:
        resp = get(url, have)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:                  # schon vollständig (die Prüfsumme sagt, ob es stimmt)
            _net_error(e)
            os.replace(part, dest)
            return
        raise _net_error(e) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise _net_error(e) from e
    with resp:
        status = getattr(resp, "status", 200)
        headers = getattr(resp, "headers", None) or {}
        length = int(headers.get("Content-Length") or 0)
        resumed = bool(have) and status == 206
        total = ((have if resumed else 0) + length) if length else 0
        if not resumed:
            have = 0
        folder = os.path.dirname(dest)
        if total and _free(folder) < total - have + DL_RESERVE:
            raise ProtonError("Zu wenig Platz für den Download (%d MB nötig)." % ((total - have) >> 20), "space")
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_APPEND if resumed else os.O_TRUNC)
        with os.fdopen(os.open(part, flags, 0o600), "wb") as out:
            tick(have, total)
            while True:
                if stop is not None and stop.is_set():
                    raise ProtonError(MSG_STOPPED, "stopped")
                try:
                    chunk = resp.read(CHUNK)
                except OSError as e:
                    raise ProtonError("Der Download brach ab. Ein neuer Versuch setzt fort.", "interrupted") from e
                if not chunk:
                    break
                out.write(chunk)
                have += len(chunk)
                tick(have, total)
    if total and _size(part) != total:
        raise ProtonError("Der Download ist unvollständig. Ein neuer Versuch setzt fort.", "interrupted")
    os.replace(part, dest)


def _hash_file(path: str, algo: str, stop=None) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            if stop is not None and stop.is_set():
                raise ProtonError("Abgebrochen.", "stopped")
            h.update(block)
    return h.hexdigest()


# ================================================================ Zwischenordner und Entpacken

def _make_staging(compat_dir: str) -> tuple:
    """<compat_dir>/.couch-proton-<zufall>/ mit Marker (Inhalt = Zufallswert), gesperrt solange wir arbeiten."""
    token = secrets.token_hex(16)
    staging = os.path.join(compat_dir, STAGING_PREFIX + token)
    os.mkdir(staging, 0o700)                      # scheitert, falls es den Namen schon gibt
    fd = os.open(os.path.join(staging, STAGING_MARKER),
                 os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.write(fd, token.encode("ascii"))
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return staging, token, fd


def _drop_staging(staging: str, token: str, lock_fd: int | None) -> str:
    """Eigenen Zwischenordner wegräumen: Unterordner nur über trash.remove_own_dir (die eine Löschstelle),
    dann Marker und leeren Ordner. Rückgabe "" oder ein Satz, warum etwas liegen blieb."""
    left = ""
    try:
        if _read_small(os.path.join(staging, STAGING_MARKER), 64) != token:
            return "Der Zwischenordner %s gehört nicht zu diesem Auftrag und bleibt liegen." % staging
        for n in os.listdir(staging):
            if n == STAGING_MARKER:
                continue
            p = os.path.join(staging, n)
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                trash.remove_own_dir(p)
            elif n == READY_MARKER and stat.S_ISREG(st.st_mode):
                os.unlink(p)                        # eigene Marke
            else:
                left = "Im Zwischenordner %s liegt Unerwartetes; er bleibt liegen." % staging
        if not left:
            os.unlink(os.path.join(staging, STAGING_MARKER))
            os.rmdir(staging)                       # nur leer möglich
    except Exception as e:  # noqa: BLE001  (Refuse, OSError: dann bleibt er liegen)
        left = "Der Zwischenordner %s blieb liegen (%s)." % (staging, getattr(e, "message", None) or e)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
    return left


def _mark_ready(staging: str, name: str) -> None:
    """Vor dem Einsetzen: Der Inhalt von staging/<name> ist geprüft und vollständig entpackt."""
    fd = os.open(os.path.join(staging, READY_MARKER), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    try:
        os.write(fd, name.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish_swap(compat_dir: str, staging: str) -> str:
    """Endete ein Lauf zwischen den beiden rename in _swap (alter Ordner schon in der Sicherung, der neue
    noch im Zwischenordner), fehlt compatibilitytools.d/<name>: den geprüften neuen Ordner einsetzen statt
    ihn wegzuräumen. Nur mit READY_MARKER (vollständig entpackt), nie über etwas Vorhandenes. Rückgabe:
    eingesetzter Pfad oder ""."""
    name = _read_small(os.path.join(staging, READY_MARKER), 256).strip()
    if not _NAME.match(name or "") or name.startswith("."):
        return ""
    fresh, target = os.path.join(staging, name), os.path.join(compat_dir, name)
    if os.path.islink(fresh) or not os.path.isdir(fresh) or not os.path.isfile(os.path.join(fresh, "proton")) \
            or os.path.lexists(target):
        return ""
    try:
        _rename(fresh, target)
    except OSError:
        return ""
    return target


def cleanup_staging(compat_dir: str) -> list[str]:
    """Zwischenordner eines abgebrochenen Laufs (Absturz, Ausschalten) wegräumen: nur .couch-proton-<zufall>
    mit passendem Marker, den gerade niemand sperrt. War der neue Ordner schon fertig und fehlt der alte an
    seinem Platz, wird er erst eingesetzt (_finish_swap). Rückgabe: entfernte Pfade."""
    gone = []
    try:
        names = sorted(os.listdir(compat_dir))
    except OSError:
        return gone
    for n in names:
        m = STAGING_RE.match(n)
        path = os.path.join(compat_dir, n)
        if not m or os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            fd = os.open(os.path.join(path, STAGING_MARKER), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue                                # ein Auftrag läuft noch
        if _read_small(os.path.join(path, STAGING_MARKER), 64) == m.group(1):
            _finish_swap(compat_dir, path)
        if not _drop_staging(path, m.group(1), fd):
            gone.append(path)
    return gone


def _norm_member(name: str) -> str | None:
    """Name ohne führendes „./“; None für den Wurzeleintrag „.“. Absolutes und „..“ werfen."""
    if name.startswith("/") or "\0" in name:
        raise ProtonError("Das Archiv enthält einen absoluten Pfad (%s) und wird nicht entpackt." % name[:80],
                          "bad-archive")
    if ".." in name.split("/"):
        raise ProtonError("Das Archiv enthält „..“ im Pfad (%s) und wird nicht entpackt." % name[:80], "bad-archive")
    norm = posixpath.normpath(name)
    return None if norm in (".", "") else norm


def _extract(archive: str, staging: str, top: str, tick: Callable[[int, int], None], stop) -> str:
    """Entpackt archive nach staging. Erlaubt ist genau ein Oberordner namens top; keine absoluten
    Pfade, kein „..“, keine Geräte, Verweise nur innerhalb von top (tarfile-Filter "data" plus eigene
    Prüfungen). Rückgabe: Pfad des entpackten Ordners."""
    dest = os.path.realpath(staging)
    top_root = os.path.join(dest, top)
    budget = [0]

    def vet(member: tarfile.TarInfo, path: str):
        name = _norm_member(member.name)
        if name is None:
            return None
        first = name.split("/", 1)[0]
        if first != top or (name == top and not member.isdir()):
            raise ProtonError("Das Archiv enthält mehr als den Ordner „%s“ (%s) und wird nicht entpackt."
                              % (top, name[:80]), "bad-archive")
        changes = {"name": name}
        if member.islnk():
            link = _norm_member(member.linkname)
            if link is None or link.split("/", 1)[0] != top:
                raise ProtonError("Das Archiv verweist aus dem Ordner hinaus (%s)." % name[:80], "bad-archive")
            changes["linkname"] = link
        member = member.replace(**changes, deep=False)
        try:
            member = tarfile.data_filter(member, path)
        except tarfile.FilterError as e:
            raise ProtonError("Das Archiv enthält Unzulässiges (%s) und wird nicht entpackt." % e, "bad-archive") from e
        if member.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(os.path.join(dest, name)), member.linkname))
            if target != top_root and not target.startswith(top_root + "/"):
                raise ProtonError("Das Archiv verweist aus dem Ordner hinaus (%s)." % name[:80], "bad-archive")
        if member.isreg():
            budget[0] += member.size
            if budget[0] > MAX_UNPACKED:
                raise ProtonError("Das Archiv wäre entpackt unplausibel groß und wird nicht entpackt.", "bad-archive")
        return member

    size = os.path.getsize(archive)
    with open(archive, "rb") as raw, tarfile.open(fileobj=raw, mode="r:*") as tar:
        def members():
            for i, m in enumerate(tar):
                if stop is not None and stop.is_set():
                    raise ProtonError("Abgebrochen.", "stopped")
                if i % 200 == 0:
                    if _free(dest) < RESERVE:
                        raise ProtonError("Kein Platz mehr beim Entpacken; es wurde abgebrochen.", "space")
                    tick(raw.tell(), size)
                yield m
        try:
            tar.extractall(dest, members=members(), filter=vet)
        except (tarfile.TarError, EOFError, lzma.LZMAError, zlib.error, gzip.BadGzipFile) as e:
            raise ProtonError("Das Archiv ist beschädigt (%s)." % e, "bad-archive") from e
        except OSError as e:
            if e.errno == errno.ENOSPC:
                raise ProtonError("Kein Platz mehr beim Entpacken; es wurde abgebrochen.", "space") from e
            raise ProtonError("Entpacken gescheitert: %s" % e, "extract") from e
    if os.path.islink(top_root) or not os.path.isfile(os.path.join(top_root, "proton")):
        raise ProtonError("Im Archiv fehlt das Startskript „proton“.", "bad-archive")
    return top_root


# ================================================================ 3 In Benutzung?

def _needles(tool_dir: str) -> list[bytes]:
    """Pfad in allen Schreibweisen (/home ist ein Verweis auf /var/home)."""
    forms = {os.path.abspath(tool_dir), os.path.realpath(tool_dir)}
    for f in list(forms):
        if f.startswith("/var/home/"):
            forms.add(f[len("/var"):])
        elif f.startswith("/home/"):
            forms.add("/var" + f)
    return sorted((os.fsencode(f) for f in forms), key=len, reverse=True)


def users(tool_dir: str, proc_root: str = "/proc", limit: int = 16 << 20) -> list[int]:
    """PIDs, deren Programm, Arbeitsordner, Befehlszeile, Umgebung, offene Dateien oder Speicherabbild
    (maps) den Ordner nennen. Nur lesend; fremde Prozesse ohne Leserecht zählen nicht."""
    pattern = re.compile(b"(?:" + b"|".join(re.escape(n) for n in _needles(tool_dir)) + b")(?![A-Za-z0-9._+-])")
    found = []
    try:
        pids = sorted((int(n) for n in os.listdir(proc_root) if n.isdigit()))
    except OSError:
        return found
    for pid in pids:
        base = os.path.join(proc_root, str(pid))
        hit = False
        for link in ("exe", "cwd"):
            try:
                if pattern.search(os.fsencode(os.readlink(os.path.join(base, link)))):
                    hit = True
                    break
            except OSError:
                pass
        for fname in ("cmdline", "environ", "maps"):
            if hit:
                break
            try:
                with open(os.path.join(base, fname), "rb") as fh:
                    hit = bool(pattern.search(fh.read(limit)))
            except OSError:
                pass
        if not hit:
            try:
                fds = os.listdir(os.path.join(base, "fd"))[:4096]
            except OSError:
                fds = []
            for fd in fds:
                try:
                    if pattern.search(os.fsencode(os.readlink(os.path.join(base, "fd", fd)))):
                        hit = True
                        break
                except OSError:
                    pass
        if hit:
            found.append(pid)
    return found


def in_use(tool_dir: str, proc_root: str = "/proc") -> bool:
    """Nutzt gerade ein Prozess den Proton-Ordner? (Wineserver, Spiel, umu-run, pressure-vessel …)"""
    return bool(users(tool_dir, proc_root))


# ================================================================ 4 Reparieren und Installieren

class _Progress:
    """progress({"phase", "done", "total", "percent"}) höchstens viermal je Sekunde.
    Phasen: download 0–80 %, verify 80–84, extract 84–97, swap 97–100, done."""
    SPANS = {"download": (0, 80), "verify": (80, 84), "extract": (84, 97), "swap": (97, 100), "done": (100, 100)}

    def __init__(self, fn):
        self.fn, self.last = fn, 0.0

    def __call__(self, phase: str, done: int = 0, total: int = 0, force: bool = False) -> None:
        if self.fn is None:
            return
        now = time.monotonic()
        if not force and now - self.last < 0.25:
            return
        self.last = now
        lo, hi = self.SPANS.get(phase, (0, 100))
        frac = min(1.0, done / total) if total else 0.0
        try:
            self.fn({"phase": phase, "done": int(done), "total": int(total), "percent": int(lo + (hi - lo) * frac)})
        except Exception:  # noqa: BLE001  (Rückruf der Oberfläche darf den Auftrag nicht kippen)
            pass


def _guard(*dirs: str) -> None:
    """Prüfmodus: nie außerhalb des Käfigs schreiben (SAFETY Regel 11)."""
    if paths.test_mode_incomplete():
        raise ProtonError("Prüfmodus ohne COUCH_TEST_ROOT: Es wird nichts geschrieben.", "test-mode")
    root = paths.test_root()
    if not root:
        return
    real_root = os.path.realpath(root)
    for d in dirs:
        real = os.path.realpath(d)
        if real != real_root and not real.startswith(real_root + "/"):
            raise ProtonError("Prüfmodus: %s liegt außerhalb des Testordners." % real, "test-mode")


def _lock(folder: str) -> int:
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(folder, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ProtonError("Für diese Proton-Version läuft schon ein Auftrag.", "running")
    return fd


def _unlink_own(path: str) -> None:
    """Eigene Einzeldatei (Archiv, .part, Sperre) entfernen; nie einen Ordner."""
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            os.unlink(path)
    except OSError:
        pass


def _fetch_archive(src: dict, work: str, get, tick: _Progress, stop) -> tuple:
    """Prüfsumme holen, Archiv laden (fortsetzen) und prüfen. Rückgabe (archiv, quelle).
    Probiert src und src["alt"] der Reihe nach (nur bei 404)."""
    candidates = [src] + list(src.get("alt") or [])
    last = None
    for cand in candidates:
        want = (cand.get("sha_value") or "").lower()
        if not want:
            if not cand.get("sha_url"):
                raise ProtonError("Für diese Fassung gibt es keine Prüfsumme; ohne sie wird nichts installiert.",
                                  "no-sum")
            try:
                text = _fetch(cand["sha_url"], get, 64 * 1024).decode("utf-8", "replace")
            except ProtonError as e:
                last = e
                if e.code == "not-found":
                    continue
                raise
            want = _parse_sum(text, cand["asset"], cand["sha_algo"])
            if not want:
                raise ProtonError("Die Prüfsummen-Datei von GitHub ist unlesbar; es wird nichts installiert.", "no-sum")
        archive = os.path.join(work, cand["asset"])
        if not os.path.isfile(archive):
            try:
                _download(cand["url"], archive, get, lambda d, t: tick("download", d, t or cand.get("size") or 0), stop)
            except ProtonError as e:
                last = e
                if e.code == "not-found" and cand is not candidates[-1]:
                    continue
                raise
        tick("verify", 0, 1, force=True)
        if _hash_file(archive, cand["sha_algo"], stop) != want:
            _unlink_own(archive)                    # verworfen: ein neuer Versuch lädt neu
            raise ProtonError("Die geladene Datei ist beschädigt (Prüfsumme stimmt nicht). Sie wurde verworfen; "
                              "ein neuer Versuch lädt sie neu.", "checksum")
        tick("verify", 1, 1, force=True)
        return archive, cand
    raise last or ProtonError("Keine Quelle für diese Proton-Version gefunden.", "no-source")


def _backup_target(tool_dir: str, compat_dir: str, backup_dir_: str, now: float, tag: str = BACKUP_TAG) -> str:
    """Zielpfad der Sicherung auf DEMSELBEN Dateisystem (rename statt Kopie). Liegt backup_dir woanders,
    dann <compat_dir>/.couch-proton-backup/. tag: BACKUP_TAG (beschädigt) oder BACKUP_TAG_OK (war heil)."""
    name = os.path.basename(tool_dir)
    folder = backup_dir_
    try:
        os.makedirs(folder, mode=0o755, exist_ok=True)
        if os.stat(folder).st_dev != os.stat(compat_dir).st_dev:
            raise OSError(errno.EXDEV, "anderes Dateisystem")
    except OSError:
        folder = os.path.join(compat_dir, BACKUP_FALLBACK)
        os.makedirs(folder, mode=0o700, exist_ok=True)
    base = os.path.join(folder, name + tag + time.strftime("%Y%m%d-%H%M", time.localtime(now)))
    path, n = base, 2
    while os.path.lexists(path):
        path = "%s-%d" % (base, n)
        n += 1
    return path


_rename = trash.rename_noreplace         # ersetzt nie etwas; in Tests austauschbar


def _swap(tool_dir: str, fresh: str, backup: str) -> None:
    """Alten Ordner nach backup verschieben, frischen einsetzen. Scheitert das Einsetzen: zurück."""
    try:
        _rename(tool_dir, backup)
    except OSError as e:
        raise ProtonError("Der beschädigte Ordner ließ sich nicht beiseitelegen (%s). Es wurde nichts verändert."
                          % (e.strerror or e), "swap") from e
    try:
        _rename(fresh, tool_dir)
    except OSError as e:
        try:
            _rename(backup, tool_dir)
        except OSError as e2:
            raise ProtonError("Der neue Ordner ließ sich nicht einsetzen (%s), und der alte nicht zurücklegen (%s). "
                              "Er liegt unter %s." % (e.strerror or e, e2.strerror or e2, backup), "swap-stuck") from e2
        raise ProtonError("Der neue Ordner ließ sich nicht einsetzen (%s). Der alte ist wieder an seinem Platz."
                          % (e.strerror or e), "swap") from e


def _wait_free(tool_dir: str, busy, wait_s: float, stop) -> bool:
    """True, sobald busy(tool_dir) False liefert (bis wait_s). Abbrechen: ProtonError „stopped“."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if not busy(tool_dir):
                return True
        except Exception:  # noqa: BLE001  (Zweifel = belegt)
            pass
        if stop is not None and stop.is_set():
            raise ProtonError(MSG_STOPPED, "stopped")
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def _gb(n: int) -> str:
    return ("%.1f GB" % (n / 2 ** 30)).replace(".", ",")


def _backups_note(folder: str, name: str) -> str:
    """Wie viele Sicherungen dieser Version liegen dort und wie groß sind sie zusammen (E101: jede Reparatur
    lässt etwa 1,7 GB liegen; gelöscht wird hier nichts)."""
    total, count = 0, 0
    try:
        names = os.listdir(folder)
    except OSError:
        return ""
    for n in names:
        if n.startswith(name + BACKUP_TAG) or n.startswith(name + BACKUP_TAG_OK):
            path = os.path.join(folder, n)
            if os.path.isdir(path) and not os.path.islink(path):
                count += 1
                try:
                    total += _tree_size(path)
                except OSError:
                    pass
    if count < 2:
        return ""
    return (" Dort liegen jetzt %d Sicherungen dieser Version (zusammen %s); nicht mehr gebrauchte kannst du im "
            "Datei-Explorer in den Papierkorb legen." % (count, _gb(total)))


def _label(name: str) -> str:
    fam = _FAMILY.get(family_of(name))
    return "%s %s" % (fam["label"], _version_of(name)) if fam else name


def _run(name: str, compat_dir: str, cache_dir: str, *, mode: str, backup_dir_: str | None, progress, stop, get,
         busy, manifest_dir_: str | None, source: dict | None, wait_s: float, clock) -> dict:
    tick = _Progress(progress)
    if not _NAME.match(name or "") or name.startswith("."):
        raise ProtonError("Unerwarteter Name: %s" % name, "bad-name")
    compat_dir = os.path.realpath(compat_dir)
    if not os.path.isdir(compat_dir):
        raise ProtonError("Der Ordner für Proton-Versionen fehlt (%s)." % compat_dir, "no-compat")
    tool_dir = os.path.join(compat_dir, name)
    mdir = manifest_dir_ or manifest_dir()
    _guard(compat_dir, cache_dir, mdir, *([backup_dir_] if backup_dir_ else []))
    if mode == "repair":
        st = os.lstat(tool_dir) if os.path.lexists(tool_dir) else None
        if st is None:
            raise ProtonError("%s ist nicht installiert; dafür gibt es „Installieren“." % name, "missing")
        if not stat.S_ISDIR(st.st_mode):
            raise ProtonError("%s ist kein echter Ordner (Verweis?) und wird nicht angefasst." % tool_dir, "not-a-dir")
        if st.st_dev != os.stat(compat_dir).st_dev:
            raise ProtonError("%s liegt auf einem anderen Dateisystem und wird nicht angefasst." % tool_dir, "other-fs")
    elif os.path.lexists(tool_dir):
        raise ProtonError("%s ist schon installiert; dafür gibt es „Reparieren“." % name, "exists")
    src = source or source_for(name)
    if not src:
        raise ProtonError("Für %s ist keine Quelle bekannt; es lässt sich nur auf eine andere Version ausweichen."
                          % name, "no-source")
    work = os.path.join(cache_dir, CACHE_SUB, name)
    lock_fd = _lock(work)
    staging = token = staging_fd = None
    try:
        if mode == "repair" and not _wait_free(tool_dir, busy, 0.0, stop):
            # E101: nicht erst laden und 1,7 GB entpacken, um dann doch nicht tauschen zu dürfen (der Haken des
            # Starters stellt bei jedem Start eine Reparatur ein, auch während Desktop-Steam den Ordner hält)
            raise ProtonError("%s wird gerade benutzt (ein Spiel oder Wine läuft noch). Beende es und versuche es "
                              "noch einmal." % _label(name), "busy")
        archive, used = _fetch_archive(src, work, get, tick, stop)
        need = os.path.getsize(archive) * UNPACK_FACTOR + RESERVE
        if _free(compat_dir) < need:
            raise ProtonError("Zu wenig Platz zum Entpacken (etwa %.1f GB nötig)." % (need / 1e9), "space")
        cleanup_staging(compat_dir)
        staging, token, staging_fd = _make_staging(compat_dir)
        tick("extract", 0, 1, force=True)
        try:
            fresh = _extract(archive, staging, used.get("top") or name, lambda d, t: tick("extract", d, t), stop)
        except ProtonError as e:
            if e.code == "bad-archive":
                _unlink_own(archive)                # nicht behalten: ein neuer Versuch lädt neu
            raise
        if os.path.basename(fresh) != name:              # GE ohne Bauart im Namen: Ordner im Archiv heißt anders
            os.rename(fresh, os.path.join(staging, name))
            fresh = os.path.join(staging, name)
        man = scan_manifest(fresh)
        man["name"] = name
        tick("swap", 0, 1, force=True)
        backup = ""
        was_ok = False
        if mode == "repair":
            if not _wait_free(tool_dir, busy, wait_s, stop):
                raise ProtonError("%s wird gerade benutzt (ein Spiel oder Wine läuft noch). Beende es und versuche "
                                  "es noch einmal; das Archiv bleibt geladen." % _label(name), "busy")
            # „Neu laden“ einer heilen Version: dann heißt die Sicherung nicht „beschädigt“
            was_ok = bool(check(tool_dir, mdir).get("ok"))
            backup = _backup_target(tool_dir, compat_dir, backup_dir_ or backup_dir(), clock(),
                                    BACKUP_TAG_OK if was_ok else BACKUP_TAG)
            _mark_ready(staging, name)
            _swap(tool_dir, fresh, backup)
        else:
            _mark_ready(staging, name)
            try:
                _rename(fresh, tool_dir)
            except OSError as e:
                raise ProtonError("Der neue Ordner ließ sich nicht einsetzen (%s)." % (e.strerror or e), "swap") from e
        note = ""
        try:
            make_manifest(tool_dir, mdir, man)
        except (OSError, ProtonError) as e:
            note = " Das Manifest ließ sich nicht schreiben (%s)." % e
        left = _drop_staging(staging, token, staging_fd)
        staging_fd = None
        staging = None
        _unlink_own(archive)
        tick("done", 1, 1, force=True)
        if mode == "repair":
            msg = "%s wurde neu geladen und eingesetzt. Der %s Ordner liegt unter %s.%s" % (
                _label(name), "bisherige" if was_ok else "beschädigte", backup,
                _backups_note(os.path.dirname(backup), name))
        else:
            msg = "%s ist installiert." % _label(name)
        return {"ok": True, "message": (msg + note + (" " + left if left else "")).strip(), "path": tool_dir,
                "backup": backup, "code": "ok"}
    finally:
        if staging is not None:
            _drop_staging(staging, token, staging_fd)
        try:
            os.close(lock_fd)
        except OSError:
            pass


def _wrap(fn) -> dict:
    try:
        return fn()
    except ProtonError as e:
        return {"ok": False, "message": e.message, "code": e.code}
    except OSError as e:
        return {"ok": False, "message": "Das hat nicht geklappt: %s" % (e.strerror or e), "code": "os"}


def repair(name: str, compat_dir: str, cache_dir: str, backup_dir: str, progress=None, stop=None, get=_open,
           busy=in_use, *, manifest_dir: str | None = None, source: dict | None = None,
           wait_s: float = BUSY_WAIT_S, clock=time.time) -> dict:
    """Beschädigten Proton-Ordner <compat_dir>/<name> durch eine frisch geladene, geprüfte Kopie ersetzen.

    Der alte Ordner wird nach <backup_dir>/<name>-beschaedigt-<JJJJMMTT-HHMM> verschoben (nie gelöscht; war er
    heil, „Neu laden“: -bisherig-). busy(tool_dir) muss False liefern: schon vor dem Laden (sonst sofort „busy“,
    ohne Download) und vor dem Tausch (Vorgabe in_use; bis wait_s gewartet). manifest_dir None =
    manifest_dir(). Rückgabe {ok, message, code, path, backup}; nie eine Ausnahme."""
    return _wrap(lambda: _run(name, compat_dir, cache_dir, mode="repair", backup_dir_=backup_dir, progress=progress,
                              stop=stop, get=get, busy=busy, manifest_dir_=manifest_dir, source=source,
                              wait_s=wait_s, clock=clock))


def install(name: str, compat_dir: str, cache_dir: str, progress=None, stop=None, get=_open, *,
            manifest_dir: str | None = None, source: dict | None = None, clock=time.time) -> dict:
    """Noch nicht vorhandene Proton-Version <compat_dir>/<name> laden und einsetzen (E102). Derselbe sichere
    Weg wie repair(). source: ein Eintrag aus available() (sonst source_for(name)).
    Rückgabe {ok, message, code, path}; nie eine Ausnahme."""
    return _wrap(lambda: _run(name, compat_dir, cache_dir, mode="install", backup_dir_=None, progress=progress,
                              stop=stop, get=get, busy=lambda d: False, manifest_dir_=manifest_dir, source=source,
                              wait_s=0.0, clock=clock))


# ================================================================ 5 Ausweichen, installierte Liste

def _display_name(tool_dir: str) -> str:
    text = _read_small(os.path.join(tool_dir, "compatibilitytool.vdf"), 8192)
    m = re.search(r'"display_name"\s+"([^"]{1,120})"', text)
    return m.group(1) if m else ""


def _tree_size(folder: str) -> int:
    dev = os.lstat(folder).st_dev
    total = 0
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames[:] = [d for d in dirnames if os.lstat(os.path.join(dirpath, d)).st_dev == dev]
        for n in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_blocks * 512
            except OSError:
                pass
    return total


def installed(compat_dir: str, manifest_dir=None, quick: bool = True) -> list[dict]:
    """Proton-Ordner in compat_dir: [{name, family, label, display_name, size_bytes (None bei quick),
    ok, reason, method, files, manifest: bool}]. Nur lesend; quick=False rechnet die Belegung (~0,1–1 s je Build)."""
    out = []
    try:
        names = sorted(os.listdir(compat_dir))
    except OSError:
        return out
    for n in names:
        path = os.path.join(compat_dir, n)
        if n.startswith(".") or n.startswith("pc5-") or not os.path.isdir(path):
            continue
        fam = family_of(n)
        if not fam and not os.path.isfile(os.path.join(path, "proton")):
            continue                                  # anderes Werkzeug (Boxtron, Luxtorpeda …)
        res = check(path, manifest_dir)
        out.append({"name": n, "family": fam, "label": _label(n), "display_name": _display_name(path),
                    "size_bytes": None if quick else _tree_size(path), "ok": res["ok"], "reason": res["reason"],
                    "method": res["method"], "files": int(res.get("files") or 0),
                    "manifest": load_manifest(manifest_dir, n) is not None})
    return out


def healthy_alternative(compat_dir: str, broken_name: str, manifest_dir=None, prefer_family: str | None = None) -> str | None:
    """Neueste andere heile Version derselben Familie (prefer_family, Vorgabe family_of(broken_name): ist bei
    einem Spiel absichtlich GE-Proton eingestellt, weil es mit CachyOS hakt, bleibt es bei GE), sonst
    proton-cachyos, sonst GE, sonst irgendeine heile Proton-Version (Ordnername)."""
    healthy = [e for e in installed(compat_dir, manifest_dir) if e["name"] != broken_name and e["ok"]]
    prefer = family_of(broken_name) if prefer_family is None else prefer_family
    for fam in ([prefer] if prefer else []) + ["proton-cachyos", "ge-proton"]:
        mine = [e["name"] for e in healthy if e["family"] == fam]
        if mine:
            return max(mine, key=version_key)
    rest = [e["name"] for e in healthy]
    return max(rest, key=version_key) if rest else None


# ================================================================ 6 Verfügbare Versionen (E102)

_ARCH_LABEL = {"x86_64_v3": "x86-64-v3", "x86_64": "x86-64", "arm64": "ARM64"}
_ARCH_SEEN: list = []                   # cpu_arch() je Prozess einmal (/proc/cpuinfo ist langsam zu lesen)


def this_arch() -> str:
    """cpu_arch() dieses Rechners, einmal je Prozess gelesen (E104: Proton-Bauart für updates.py und die Texte)."""
    if not _ARCH_SEEN:
        try:
            _ARCH_SEEN.append(cpu_arch())
        except Exception:  # noqa: BLE001 - im Zweifel die bisherige feste Bauart
            _ARCH_SEEN.append("x86_64_v3")
    return _ARCH_SEEN[0]


def family_text(family, arch: str | None = None) -> str:
    """Beschreibung einer Familie für diesen Rechner (E104): Nennt der Text eine Bauart (arch_text), steht dort
    die, die hier geladen wird (arch, Vorgabe this_arch()). family: Name oder Eintrag aus CATALOG."""
    fam = _FAMILY.get(family) if isinstance(family, str) else family
    if not isinstance(fam, dict):
        return ""
    text = str(fam.get("text") or "")
    old = fam.get("arch_text")
    if old:
        label = _ARCH_LABEL.get(arch or this_arch(), str(arch or ""))
        if label and label != old:
            text = text.replace(old, label)
    return text


def cpu_arch(cpuinfo: str = "/proc/cpuinfo") -> str:
    """Bauart für Proton-CachyOS: x86_64_v3, wenn die CPU alle v3-Befehle kann, sonst x86_64 (arm64 auf ARM)."""
    if os.uname().machine in ("aarch64", "arm64"):
        return "arm64"
    flags = set()
    for line in _read_small(cpuinfo, 256 * 1024).splitlines():
        if line.startswith("flags"):
            flags = set(line.split(":", 1)[-1].split())
            break
    need = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}
    return "x86_64_v3" if need <= flags else "x86_64"


def _version_of(name: str) -> str:
    m = _CACHY.match(name)
    if m:
        return re.sub(r"-slr$", "", m.group(1)[len("cachyos-"):])     # slr ist heute die einzige Art
    m = _GE.match(name)
    if m:
        return m.group(1)[len("GE-Proton"):]
    if _UMU.match(name):
        return name[len("UMU-Proton-"):]
    m = _EM.match(name)
    if m:
        return m.group(1)[len("EM-"):]
    return name


def parse_releases(family: str, releases, arch: str = "x86_64_v3", limit: int = 5) -> list[dict]:
    """GitHub-Release-Liste → Einträge {name, family, label, version, tag, date, size, url, sha_url,
    sha_algo, sha_value, asset, top, notes, prerelease}. Ohne verlässliche Prüfsumme kein Eintrag."""
    fam = _FAMILY[family]
    pattern = re.compile(fam["asset"] % {"arch": re.escape(arch)})
    items = []
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        assets = {str(a.get("name") or ""): a for a in rel.get("assets") or [] if isinstance(a, dict)}
        for aname, asset in sorted(assets.items()):
            m = pattern.match(aname)
            if not m:
                continue
            stem = m.group(1)
            sum_asset = assets.get(stem + fam["sum"]) or {}
            digest = str(asset.get("digest") or "")
            algo, sha_url, value = fam["algo"], str(sum_asset.get("browser_download_url") or ""), ""
            if not sha_url:
                if not digest.startswith("sha256:"):
                    continue                          # keine Prüfsumme: nicht anbieten
                algo, value = "sha256", digest[len("sha256:"):].lower()
            notes = family_text(fam, arch) + (" Vorabfassung." if rel.get("prerelease") else "")
            items.append({"name": stem, "family": family, "label": "%s %s" % (fam["label"], _version_of(stem)),
                          "version": _version_of(stem), "tag": str(rel.get("tag_name") or ""),
                          "date": str(rel.get("published_at") or "")[:10], "size": int(asset.get("size") or 0),
                          "url": str(asset.get("browser_download_url") or ""), "sha_url": sha_url,
                          "sha512_url": sha_url if algo == "sha512" else "", "sha_algo": algo, "sha_value": value,
                          "asset": aname, "top": stem, "kind": family, "repo": fam["repo"], "alt": [],
                          "notes": notes, "prerelease": bool(rel.get("prerelease"))})
            break                                     # eine Datei je Release (passende Bauart)
        if len(items) >= limit:
            break
    return items


def trusted_source(item: dict) -> dict | None:
    """Quelle eines Eintrags aus available() nur so, wie sie zum Katalog passt (E102): Familie bekannt,
    Datei passt zum Muster der Familie und zum Namen, Adressen neu gebaut aus Repo, Tag und Datei
    (GitHub-Release des Katalog-Repos), nie die Adressen aus der API-Antwort oder dem Zwischenspeicher.
    None = passt nicht."""
    if not isinstance(item, dict):
        return None
    fam = _FAMILY.get(str(item.get("family") or item.get("kind") or ""))
    name, tag, asset = str(item.get("name") or ""), str(item.get("tag") or ""), str(item.get("asset") or "")
    if fam is None or not _NAME.match(name) or not re.fullmatch(r"[0-9A-Za-z._+-]{1,128}", tag) or tag.startswith("."):
        return None
    m = re.match(fam["asset"] % {"arch": r"[0-9A-Za-z_]+"}, asset)
    if not m or m.group(1) != name or family_of(name) != fam["family"]:
        return None
    src = _src(fam["family"], tag, asset, str(item.get("top") or name))
    if not _NAME.match(src["top"]):
        return None
    value = str(item.get("sha_value") or "").lower()
    if value:
        # nur GitHubs sha256 („digest“) ohne eigene Prüfsummen-Datei, siehe parse_releases
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            return None
        src.update(sha_value=value, sha_algo="sha256", sha_url="", sha512_url="")
    src.update(size=int(item.get("size") or 0), alt=[])
    return src


def _cache_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, CACHE_SUB, "releases.json")


def available(family=None, limit: int = 5, get=_open, cache_dir: str | None = None, ttl: float = TTL_S,
              compat_dir: str | None = None, *, status: bool = False, arch: str | None = None, now=time.time):
    """Verfügbare Versionen je Familie über die GitHub-Releases-API, zwischengespeichert (ttl). Ohne Netz gilt
    der letzte Stand (Einträge mit stale=True). family None = alle mit listed=True; sonst ein Name oder eine Liste.
    Rückgabe: Liste der Einträge (installed: bool je compat_dir). status=True: {items, error, checked_at};
    ohne status wirft es ProtonError, wenn gar nichts bekannt ist."""
    fams = [c["family"] for c in CATALOG if c["listed"]] if family is None else \
        ([family] if isinstance(family, str) else list(family))
    fams = [f for f in fams if f in _FAMILY]
    cdir = cache_dir or cache_root()
    path = _cache_file(cdir)
    try:
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    arch = arch or this_arch()
    items, errors, changed, checked = [], [], False, 0.0
    for fam in fams:
        entry = cache.get(fam) if isinstance(cache.get(fam), dict) else None
        key_ok = entry is not None and entry.get("arch") == arch and entry.get("limit", 0) >= limit
        fresh = key_ok and now() - float(entry.get("at") or 0) < ttl
        stale = False
        if not fresh:
            try:
                url = GITHUB_API % (_FAMILY[fam]["repo"], min(30, max(limit * 2, 6)))
                data = json.loads(_fetch(url, get, MAX_JSON))
                if not isinstance(data, list):
                    raise ProtonError("GitHub lieferte eine unerwartete Antwort.", "bad-reply")
                entry = {"at": now(), "arch": arch, "limit": limit, "items": parse_releases(fam, data, arch, limit)}
                cache[fam] = entry
                changed = True
            except (ProtonError, ValueError) as e:
                why = getattr(e, "message", "GitHub lieferte eine unerwartete Antwort.")
                errors.append("%s: %s" % (_FAMILY[fam]["label"], why))
                stale = True
                if not key_ok:
                    entry = None                      # alter Stand für eine andere Bauart: nicht anbieten
        if entry:
            checked = max(checked, float(entry.get("at") or 0))
            for it in (entry.get("items") or [])[:limit]:
                name = str(it.get("name") or "")
                inst = bool(compat_dir and name) and os.path.isdir(os.path.join(compat_dir, name))
                items.append(dict(it, installed=inst, stale=stale))
    if changed:
        try:
            _guard(cdir)
            _write_json_atomic(path, cache)
        except (OSError, ProtonError):
            pass                                      # dann nur ohne Zwischenspeicher
    error = " ".join(errors)
    if status:
        return {"items": items, "error": error, "checked_at": checked}
    if not items and error:
        raise ProtonError(error, "unavailable")
    return items
