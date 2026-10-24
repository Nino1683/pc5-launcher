"""FSR-Erkennung im Spielordner, nur lesend, mit Zwischenspeicher.

Regel (Herleitung: docs/research/03-proton-fsr4-hdr.md, Abschnitt 2.5), Dateinamen ohne
Rücksicht auf Groß-/Kleinschreibung irgendwo unter dem Spielordner:

  fsr4-native   amd_fidelityfx_upscaler_dx12.dll (FidelityFX SDK 2.x): Das Spiel fragt selbst
                nach FSR 4. Die Dateiversion der DLL ist die FSR-Version.
  fsr4-upgrade  amd_fidelityfx_dx12.dll (SDK 1.1.x = FSR 3.1, DX12): Proton-CachyOS stuft das
                über den Treiberweg auf FSR 4 hoch. Im Spiel muss FSR 3.1 gewählt sein.
  fsr31-vulkan  nur amd_fidelityfx_vk.dll: FSR 3.1 über Vulkan, kein FSR 4.
  optiscaler    nur DLSS/XeSS/FSR-2-DLLs: FSR 4 ginge nur über OptiScaler.
  none          nichts gefunden.
  skipped       Ordner nicht durchsucht (FUSE/NTFS/Netz, Einhängepunkt, Home); Grund in "reason".
  missing       Ordner fehlt (z. B. Spiele-Platte nicht eingehängt).

Marken für die Kachel: tags_for(ergebnis) -> ["fsr4"] | ["fsr4-upgrade"] | [].

Nebenbei merkt sich der Durchlauf VR-Laufzeit-DLLs (files["vr"]) und den Ordner game/hlvr
(files["vr_marker"]), damit die Lutris-Quelle für ihre VR-Heuristik nicht noch einmal sucht.

Schutz: folgt keinen Symlinks, verlässt das Dateisystem des Spielordners nicht (st_dev und
Einhängepunkte aus /proc/self/mountinfo), betritt keine FUSE-/NTFS-/Netz-Einhängungen, begrenzt
Tiefe und Dateizahl. Alles hier blockiert (Platte) und gehört in einen Thread.

Zwischenspeicher <cache_dir>/fsr.json:
  {"version": 1, "entries": {"<realpath des Spielordners>":
      {"dir_mtime": ns, "depth": n, "scanned": unix, "dlls": {"<relativer pfad>": mtime_ns},
       "result": {…}}}}
Ein Eintrag gilt, solange mtime des Spielordners und aller gefundenen DLLs unverändert sind und
er nicht älter als ttl ist (eine tief im Baum neu abgelegte DLL ändert die mtime des Spielordners
nicht; die Frist fängt das auf). Fehlt der Ordner, wird das letzte bekannte Ergebnis geliefert
("stale": True), damit die Marke bei abgezogener Platte nicht flackert.
"""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time

MOUNTINFO = "/proc/self/mountinfo"
DEFAULT_DEPTH = 8            # Ordnerebenen unter dem Spielordner (Wert des Prototyps, siehe Bericht 03)
DEFAULT_TTL = 7 * 86400
MAX_FILES = 300_000          # Notbremse gegen riesige Bäume
MAX_LISTED = 16              # so viele Funde je Art merken
CACHE_VERSION = 1

# Dateinamen klein. Quelle: Ersetzungstabelle WINE_UPSCALER_REPLACE in proton-cachyos-11.0-20260703.
FSR4_UPSCALER = "amd_fidelityfx_upscaler_dx12.dll"
FSR4_NATIVE = {FSR4_UPSCALER, "amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_framegeneration_dx12.dll"}
FSR31_DX12 = {"amd_fidelityfx_dx12.dll"}
FSR31_VK = {"amd_fidelityfx_vk.dll"}
DLSS = {"nvngx_dlss.dll", "nvngx_dlssd.dll", "nvngx_dlssg.dll"}
XESS = {"libxess.dll", "libxess_dx11.dll", "libxess_fg.dll", "libxell.dll"}
OPTISCALER = {"optiscaler.ini", "optiscaler.dll"}
D3D12_HINT = {"d3d12core.dll"}
VR_RUNTIME = {"openvr_api.dll", "ovrplugin.dll", "openxr_loader.dll",
              "libovrplatform64_1.dll", "libovrplatform32_1.dll"}

# Dateiversion der amd_fidelityfx_dx12.dll -> FSR-Version
# (https://loathingkernel.github.io/proton-upscalers/manifest.json, Stand 2026-10-13)
FSR31_VERSIONS = {
    (1, 0, 0): "3.1.0",
    (1, 0, 1, 37507): "3.1.1",
    (1, 0, 1, 38338): "3.1.2",
    (1, 0, 2, 38022): "3.1.2",
    (1, 0, 1, 39157): "3.1.3",
    (1, 0, 1, 40157): "3.1.3",
    (1, 0, 1, 41314): "3.1.4",
}

SKIP_DIRS = {"lost+found", "$recycle.bin", "system volume information"}
# Dateisysteme, die nie durchsucht werden: FUSE (NTFS über lowntfs-3g, Portale, sshfs) und Netz.
SKIP_FS = {"ntfs", "ntfs3", "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs", "ceph", "autofs",
           "proc", "sysfs", "devtmpfs", "devpts", "cgroup2", "tracefs", "debugfs", "securityfs"}

_PE_VERSION_SIG = b"\xbd\x04\xef\xfe"


# ---------------------------------------------------------------- Einhängungen

def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def mount_table(mountinfo: str = MOUNTINFO) -> dict[str, str]:
    """Einhängepunkt -> Dateisystemtyp. Spätere Zeilen überdecken frühere (so wie der Kern)."""
    table: dict[str, str] = {}
    try:
        with open(mountinfo, encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                left, sep, right = line.rstrip("\n").partition(" - ")
                fields, tail = left.split(" "), right.split(" ")
                if sep and len(fields) >= 5 and tail and tail[0]:
                    table[_unescape(fields[4])] = tail[0]
    except OSError:
        pass
    return table


def fs_type(path: str, mounts: dict[str, str] | None = None) -> str:
    """Dateisystemtyp des Pfads (längster passender Einhängepunkt); "" wenn unbekannt."""
    mounts = mount_table() if mounts is None else mounts
    real = os.path.realpath(path)
    best, best_len = "", -1
    for point, fstype in mounts.items():
        if real == point or real.startswith(point.rstrip("/") + "/"):
            if len(point) > best_len:
                best, best_len = fstype, len(point)
    return best


def skip_fs(fstype: str) -> bool:
    return fstype.startswith("fuse") or fstype in SKIP_FS


def is_mount_point(path: str, mounts: dict[str, str] | None = None) -> bool:
    mounts = mount_table() if mounts is None else mounts
    return os.path.realpath(path) in mounts


# ---------------------------------------------------------------- Durchlauf

def _entry_dev(entry) -> int:
    """Gerätenummer eines Unterordners (eigene Funktion, damit Tests sie ersetzen können)."""
    return entry.stat(follow_symlinks=False).st_dev


def walk(root: str, max_depth: int = DEFAULT_DEPTH, mounts: dict[str, str] | None = None,
         max_files: int = MAX_FILES):
    """Liefert (pfad, name, ist_ordner) für alles unter root. Ohne Symlinks, ohne Wechsel des
    Dateisystems, bis max_depth Ordnerebenen tief. Bricht nach max_files Einträgen ab."""
    mounts = {} if mounts is None else mounts
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return
    seen = 0
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > max_files:
                return
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield entry.path, entry.name, True
                    if depth >= max_depth or entry.name.lower() in SKIP_DIRS:
                        continue
                    if entry.path in mounts or _entry_dev(entry) != root_dev:
                        continue                      # fremde Einhängung: nicht betreten
                    stack.append((entry.path, depth + 1))
                else:
                    yield entry.path, entry.name, False
            except OSError:
                continue


def pe_file_version(path: str, limit: int = 64 * 1024 * 1024):
    """Dateiversion aus VS_FIXEDFILEINFO; None, wenn nicht lesbar.

    Die Ressourcen stehen fast immer am Dateiende: erst das letzte MiB lesen, dann mehr. So kostet
    die Abfrage wenige Seiten statt der ganzen DLL (auf der Platte spielt vielleicht gerade jemand).
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            for span in (1 << 20, 8 << 20, limit):
                span = min(span, size, limit)
                fh.seek(size - span)
                data = fh.read(span)
                pos = data.rfind(_PE_VERSION_SIG)
                if pos >= 0 and pos + 16 <= len(data):
                    _sig, _struc, ms, ls = struct.unpack_from("<IIII", data, pos)
                    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
                if span >= size or span >= limit:
                    break
    except (OSError, struct.error):
        pass
    return None


def fsr31_name(version):
    if not version:
        return None
    return FSR31_VERSIONS.get(tuple(version)) or FSR31_VERSIONS.get(tuple(version[:3]))


def _empty(real: str, verdict: str, reason: str = "") -> dict:
    return {"dir": real, "verdict": verdict, "fsr4": False, "detail": "", "version": None,
            "files": {}, "reason": reason}


def refuse_reason(real: str, mounts: dict[str, str], home: str | None = None) -> str:
    """Warum dieser Ordner nicht durchsucht wird ("" = wird durchsucht)."""
    fstype = fs_type(real, mounts)
    if skip_fs(fstype):
        return "Dateisystem %s wird nicht durchsucht" % fstype
    if real == "/" or real in mounts:
        return "Einhängepunkt (ganze Platte) wird nicht durchsucht"
    for h in {os.path.expanduser("~"), home or os.path.expanduser("~")}:
        h = os.path.realpath(h)
        if real == h or real == os.path.dirname(h):
            return "Home-Ordner wird nicht durchsucht"
    return ""


def scan(game_dir: str, max_depth: int = DEFAULT_DEPTH, mounts: dict[str, str] | None = None,
         home: str | None = None) -> dict:
    """Einen Spielordner durchsuchen. Blockierend. Rückgabe (JSON-tauglich):

    {"dir": realpath, "verdict": "fsr4-native"|"fsr4-upgrade"|"fsr31-vulkan"|"optiscaler"|"none"|
     "skipped"|"missing", "fsr4": bool, "detail": "SDK 2.x, FSR 4.1.0", "version": "4.1.0"|None,
     "files": {"fsr4": [rel…], "fsr31_dx12": […], "fsr31_vk", "fsr_legacy", "dlss", "xess",
               "optiscaler", "d3d12", "vr", "vr_marker"}, "reason": ""}
    """
    real = os.path.realpath(game_dir) if game_dir else ""
    if not real or not os.path.isdir(real):
        return _empty(real, "missing", "Ordner fehlt")
    mounts = mount_table() if mounts is None else mounts
    reason = refuse_reason(real, mounts, home)
    if reason:
        return _empty(real, "skipped", reason)

    found: dict[str, list[str]] = {}

    def add(kind: str, path: str) -> None:
        bucket = found.setdefault(kind, [])
        if len(bucket) < MAX_LISTED:
            bucket.append(os.path.relpath(path, real))

    for path, name, is_dir in walk(real, max_depth, mounts):
        low = name.lower()
        if is_dir:
            # Source 2 in der VR-Fassung (Half-Life: Alyx und Ableger): <spiel>/game/hlvr
            if low == "hlvr" and os.path.basename(os.path.dirname(path)).lower() == "game":
                add("vr_marker", path)
            continue
        if not (low.endswith(".dll") or low.endswith(".ini")):
            continue
        if low in FSR4_NATIVE:
            add("fsr4", path)
        elif low in FSR31_DX12:
            add("fsr31_dx12", path)
        elif low in FSR31_VK:
            add("fsr31_vk", path)
        elif low in DLSS:
            add("dlss", path)
        elif low in XESS:
            add("xess", path)
        elif low in OPTISCALER:
            add("optiscaler", path)
        elif low in D3D12_HINT:
            add("d3d12", path)
        elif low in VR_RUNTIME:
            add("vr", path)
        elif low.startswith("ffx_") and low.endswith(".dll"):
            add("fsr_legacy", path)                    # FSR 2.x / 3.0, z. B. ffx_fsr2_api_x64.dll

    res = _empty(real, "none")
    res["files"] = {k: sorted(v) for k, v in found.items()}
    upscalers = [r for r in found.get("fsr4", []) if os.path.basename(r).lower() == FSR4_UPSCALER]
    if upscalers:
        versions = set()
        for rel in upscalers:
            ver = pe_file_version(os.path.join(real, rel))
            versions.add(".".join(map(str, ver[:3])) if ver else "?")
        known = sorted(v for v in versions if v != "?")
        res.update(verdict="fsr4-native", fsr4=True, version=known[-1] if known else None,
                   detail="SDK 2.x, FSR " + ", ".join(sorted(versions)))
    elif found.get("fsr31_dx12"):
        names = []
        for rel in found["fsr31_dx12"]:
            ver = pe_file_version(os.path.join(real, rel))
            names.append((fsr31_name(ver) or "?", ".".join(map(str, ver)) if ver else "Version unlesbar"))
        known = sorted(n for n, _ in names if n != "?")
        res.update(verdict="fsr4-upgrade", fsr4=True, version=known[-1] if known else None,
                   detail="FSR " + ", ".join(sorted({"%s (%s)" % n for n in names})))
    elif found.get("fsr31_vk"):
        res.update(verdict="fsr31-vulkan", version="3.1", detail="FSR 3.1 über Vulkan")
    elif found.get("dlss") or found.get("xess") or found.get("fsr_legacy"):
        res.update(verdict="optiscaler",
                   detail="+".join(k for k in ("dlss", "xess", "fsr_legacy") if found.get(k)))
    return res


def tags_for(result: dict | None) -> list[str]:
    """Marken aus einem Scan-Ergebnis: ["fsr4"], ["fsr4-upgrade"] oder []."""
    verdict = (result or {}).get("verdict")
    if verdict == "fsr4-native":
        return ["fsr4"]
    if verdict == "fsr4-upgrade":
        return ["fsr4-upgrade"]
    return []


def summary(result: dict | None) -> dict:
    """Kurzfassung für Entry.detail["fsr"]."""
    r = result or {}
    return {"verdict": r.get("verdict", "none"), "detail": r.get("detail", ""),
            "version": r.get("version"), "dir": r.get("dir", ""), "stale": bool(r.get("stale"))}


# ---------------------------------------------------------------- Zwischenspeicher

_TRACKED = ("fsr4", "fsr31_dx12", "fsr31_vk", "fsr_legacy", "dlss", "xess", "optiscaler")


class FsrScanner:
    """scan() mit Zwischenspeicher. Threadsicher; scan() und save() blockieren (Platte).

    cache_path=None: nur im Speicher, es wird nichts geschrieben.
    """

    def __init__(self, cache_path: str | None = None, max_depth: int = DEFAULT_DEPTH,
                 ttl: float = DEFAULT_TTL, mountinfo: str = MOUNTINFO, home: str | None = None):
        self.cache_path = cache_path
        self.max_depth = max_depth
        self.ttl = ttl
        self.mountinfo = mountinfo
        self.home = home
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._mounts: tuple[float, dict[str, str]] = (0.0, {})
        self._load()

    def _load(self) -> None:
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("entries") if isinstance(data, dict) and data.get("version") == CACHE_VERSION else None
            if isinstance(entries, dict):
                self._entries = {k: v for k, v in entries.items()
                                 if isinstance(v, dict) and isinstance(v.get("result"), dict)}
        except (OSError, ValueError):
            self._entries = {}                       # kaputter Zwischenspeicher: neu aufbauen

    def mounts(self) -> dict[str, str]:
        stamp, table = self._mounts
        if time.monotonic() - stamp > 2.0 or not table:
            table = mount_table(self.mountinfo)
            self._mounts = (time.monotonic(), table)
        return table

    def _fresh(self, real: str, entry: dict) -> bool:
        try:
            if os.stat(real).st_mtime_ns != entry.get("dir_mtime"):
                return False
            if entry.get("depth") != self.max_depth:
                return False
            if not 0 <= time.time() - float(entry.get("scanned", 0)) < self.ttl:
                return False
            for rel, mtime in (entry.get("dlls") or {}).items():
                if os.lstat(os.path.join(real, rel)).st_mtime_ns != mtime:
                    return False
        except (OSError, TypeError, ValueError):
            return False
        return True

    def scan(self, game_dir: str, force: bool = False) -> dict:
        real = os.path.realpath(game_dir) if game_dir else ""
        with self._lock:
            entry = self._entries.get(real)
        if not real or not os.path.isdir(real):
            if not entry and real:
                # Ohne Platte errät game_root() bei Unreal-Spielen den Projektordner statt des
                # Spielordners darüber; dessen gemerktes Ergebnis gilt dann auch.
                with self._lock:
                    entry = self._entries.get(os.path.dirname(real))
            if entry:                                 # Platte weg: letztes bekanntes Ergebnis
                return {**entry["result"], "stale": True}
            return _empty(real, "missing", "Ordner fehlt")
        if entry and not force and self._fresh(real, entry):
            self.hits += 1
            return dict(entry["result"])
        self.misses += 1
        try:
            dir_mtime = os.stat(real).st_mtime_ns
        except OSError:
            return _empty(real, "missing", "Ordner fehlt")
        result = scan(real, self.max_depth, self.mounts(), self.home)
        if result["verdict"] in ("skipped", "missing"):
            return result                             # nicht merken: der Grund kann sich ändern
        dlls = {}
        for kind in _TRACKED:
            for rel in result["files"].get(kind, []):
                try:
                    dlls[rel] = os.lstat(os.path.join(real, rel)).st_mtime_ns
                except OSError:
                    pass
        with self._lock:
            self._entries[real] = {"dir_mtime": dir_mtime, "depth": self.max_depth,
                                   "scanned": time.time(), "dlls": dlls, "result": result}
            self._dirty = True
        return dict(result)

    def save(self) -> bool:
        """Schreibt den Zwischenspeicher, falls sich etwas geändert hat. False = nichts geschrieben."""
        if not self.cache_path:
            return False
        with self._lock:
            if not self._dirty:
                return False
            data = {"version": CACHE_VERSION, "entries": dict(self._entries)}
            self._dirty = False
        tmp = "%s.%d.tmp" % (self.cache_path, os.getpid())
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))     # ensure_ascii: auch kaputte Dateinamen überleben
            os.replace(tmp, self.cache_path)
            return True
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            with self._lock:
                self._dirty = True
            return False
