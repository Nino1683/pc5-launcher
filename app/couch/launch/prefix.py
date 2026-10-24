"""Prefix-Wahl nach E10 und Übernahme fremder Prefixe als Reflink-Kopie.

Regeln (Bauplan E10, Kritik Befund 18):
- reines Proton-Prefix aus Lutris, nur von einem Spiel benutzt: an Ort und Stelle benutzen;
- wine-staging-, gemischtes oder geteiltes Prefix: einmalig nach <prefix_root>/<slug> kopieren
  und danach nur noch die Kopie benutzen (Lutris' Prefix wird nie angefasst);
- leer, fehlend, Archiv-Spiel: eigenes Prefix <prefix_root>/<name>, Proton legt es an.

decide() und needs_adoption() rechnen nur (ein paar stat-Aufrufe). adopt_prefix() kopiert und
ist BLOCKIEREND (Sekunden bis Minuten): im Thread aufrufen, Fortschritt kommt über progress_cb.

Gelöscht wird in diesem Modul genau eine Art Ordner: der eigene unfertige Zwischenordner einer
abgebrochenen Kopie, und nur über remove_unfinished().
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass

from .. import jsontext
from .spec import LaunchError

MARKER = "couch-prefix.json"              # liegt in jeder fertigen Kopie
PARTIAL = MARKER + ".partial"             # liegt im Zwischenordner, solange kopiert wird
ROOT_MARKER = ".couch-prefixes"           # kennzeichnet den Ordner als unseren Prefix-Ordner
TMP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.tmp-[0-9a-f]{16}$")
RESERVE_BYTES = 2 * 1024 ** 3             # frei bleiben soll mindestens so viel, wenn echt kopiert wird

TEXT_WINESERVER = ("Im Prefix läuft noch ein Wine-Prozess von einem früheren Start. "
                   "Bitte kurz warten oder das Spiel in der Übersicht schließen.")
TEXT_ADOPT_NEEDED = ("Die Spielstände dieses Spiels müssen einmalig übernommen werden, "
                     "bevor es hier startet.")
TEXT_ADOPT_FAILED = "Die Spielstände konnten nicht übernommen werden."
TEXT_ADOPT_CANCELLED = "Die Übernahme der Spielstände wurde abgebrochen."
TEXT_ADOPT_HINT = ("Die Spielstände werden einmalig übernommen. Danach läuft der Spielstand hier "
                   "getrennt von Lutris weiter.")

COPY_STATES = ("wine", "mixed")
OWN_STATES = ("empty", "missing")


class AdoptionRequired(LaunchError):
    """Vor dem Start muss adopt_prefix(src, dst) laufen. Die Oberfläche zeigt TEXT_ADOPT_HINT."""

    def __init__(self, src: str, dst: str):
        super().__init__(TEXT_ADOPT_NEEDED, "%s -> %s" % (src, dst), reason="adoption-required")
        self.src = src
        self.dst = dst
        self.info: dict | None = None     # copy_estimate(), wenn der Aufrufer sie schon hat


class AdoptCancelled(LaunchError):
    def __init__(self):
        super().__init__(TEXT_ADOPT_CANCELLED, reason="cancelled")


@dataclass
class PrefixPlan:
    path: str                 # der WINEPREFIX für den Start
    mode: str                 # "in-place" | "copy" | "own"
    state: str                # prefix_state aus der Bibliothek bzw. "local"
    source: str = ""          # Lutris-Prefix, aus dem (kopiert) wird; leer bei eigenem Prefix
    adopt: tuple | None = None  # (src, dst), solange die Kopie noch aussteht
    note: str = ""            # ein deutscher Satz für Plan und Protokoll


# ---------------------------------------------------------------------------------------------
# Wahl

def prefix_name(entry) -> str:
    """Ordnername des eigenen Prefix: Lutris-Slug bzw. "local-<id>"; nur harmlose Zeichen."""
    ident = str(entry.id).split(":", 1)[-1]
    if entry.source == "local":
        raw = "local-" + ident
    else:
        raw = str(entry.detail.get("slug") or ident)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    name = name.replace(".tmp-", "-tmp-")        # nie mit einem Zwischenordner verwechselbar
    if not name:
        name = "x" + hashlib.sha1(str(entry.id).encode()).hexdigest()[:12]
    return name


def read_marker(prefix: str) -> dict | None:
    """Inhalt von couch-prefix.json einer Kopie, sonst None.

    Liegt nur die .partial-Datei in einem fertig benannten Ordner, ist die Kopie trotzdem
    vollständig (umbenannt wird erst nach erfolgreichem cp); nur der letzte Schritt fehlte.
    """
    for name in (MARKER, PARTIAL):
        try:
            with open(os.path.join(prefix, name), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            continue
    return None


def _has_content(folder: str) -> bool:
    try:
        with os.scandir(folder) as it:
            return any(True for _ in it)
    except OSError:
        return False


def decide(entry, ctx) -> PrefixPlan:
    """Welcher Prefix gilt für diesen Eintrag? Rechnet nur (stat), kopiert nichts."""
    d = entry.detail
    own = os.path.join(ctx.prefix_root_dir(), prefix_name(entry))
    launcher_prefix = str(d.get("launcher_prefix") or "") if isinstance(d, dict) else ""
    if entry.source == "local" and launcher_prefix:
        # Spiele über den Launcher eines Herstellers (E94): der Prefix des Launchers, in dem sie installiert sind
        return PrefixPlan(launcher_prefix, "own", "local", note="Prefix des Launchers %s." % (d.get("launcher") or ""))
    if entry.source == "local":
        note = "Eigener Prefix; Proton legt ihn beim ersten Start an."
        if os.path.isdir(own):
            note = "Eigener Prefix."
        return PrefixPlan(own, "own", "local", note=note)

    state = str(d.get("prefix_state") or "missing")
    src = str(d.get("prefix") or "")
    shared = bool(d.get("shared_prefix"))

    # Gibt es unseren Ordner schon, gilt er immer: nach der Übernahme laufen die Spielstände
    # dort weiter, egal was Lutris seither mit seinem Prefix gemacht hat.
    if os.path.isdir(own):
        marker = read_marker(own)
        if marker:
            return PrefixPlan(own, "copy", state, source=str(marker.get("source") or src),
                              note="Kopie des Lutris-Prefix (übernommen am %s)."
                                   % str(marker.get("adopted") or "?")[:10])
        return PrefixPlan(own, "own", state, note="Eigener Prefix.")

    src_ok = bool(src) and os.path.isdir(src)
    if state == "proton" and not shared and src_ok:
        return PrefixPlan(src, "in-place", state, source=src,
                          note="Reines Proton-Prefix aus Lutris, wird an Ort und Stelle benutzt.")
    # Kopiert wird bei wine/mixed, bei jedem geteilten Prefix und bei unbekanntem Zustand
    # (kopieren ist die sichere Seite: Lutris bleibt unberührt, nichts wird vermischt)
    copy = state not in OWN_STATES and (state != "proton" or shared)
    if copy and src_ok and _has_content(src):
        why = ("Lutris-Prefix, das sich mehrere Spiele teilen" if shared
               else "Lutris-Prefix im Zustand „%s“" % state)
        return PrefixPlan(own, "copy", state, source=src, adopt=(src, own),
                          note="%s: wird beim ersten Start einmalig kopiert." % why)
    return PrefixPlan(own, "own", state,
                      note="Eigener Prefix; Proton legt ihn beim ersten Start an.")


def needs_adoption(entry, ctx) -> tuple | None:
    """(src, dst), wenn vor dem Start noch kopiert werden muss, sonst None."""
    if entry.source not in ("proton", "local"):
        return None
    return decide(entry, ctx).adopt


# ---------------------------------------------------------------------------------------------
# Laufender Wineserver

def _strip_pfx(path: str) -> str:
    path = path.rstrip("/")
    return path[:-4] if path.endswith("/pfx") else path


def wineserver_pids(prefix: str, proc_root: str = "/proc") -> list:
    """PIDs laufender Wineserver dieses Prefix.

    Proton trägt WINEPREFIX=<aufgelöster Pfad>/pfx/ ein, Lutris-Wine den Pfad aus der YAML.
    Deshalb beide Seiten mit realpath und ohne angehängtes /pfx vergleichen (03 Abschnitt 1.6;
    genau an diesem Vergleich scheitert lutris-steam-launch). Liest /proc, kurz blockierend.
    """
    if not prefix:
        return []
    want = {os.path.realpath(prefix), os.path.realpath(_strip_pfx(prefix))}
    found = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        base = os.path.join(proc_root, name)
        try:
            with open(os.path.join(base, "comm"), "rb") as fh:
                comm = fh.read().strip()
            if not comm.startswith(b"wineserver"):
                continue
            with open(os.path.join(base, "environ"), "rb") as fh:
                environ = fh.read().split(b"\0")
        except OSError:
            continue                      # fremder Nutzer oder Prozess schon weg
        for item in environ:
            if item.startswith(b"WINEPREFIX="):
                value = os.fsdecode(item[len(b"WINEPREFIX="):])
                if value and {os.path.realpath(value), os.path.realpath(_strip_pfx(value))} & want:
                    found.append(int(name))
                break
    return sorted(found)


# Wine-Prozesse im Prefix (E101). Anlass 2026-10-23: Zwei Proton-Fassungen liefen zugleich auf einem
# Prefix, und Wines Prefix-Abgleich (wineboot) der einen schrieb ihre DLLs durch die absoluten
# system32-Verweise in den Ordner der anderen (1214 Dateien). Deshalb zählt jeder Prozess, dessen
# Umgebung den Prefix nennt, nicht nur der Wineserver, und auch fremde (Steam, Lutris am Desktop).
_PREFIX_KEYS = (b"WINEPREFIX=", b"STEAM_COMPAT_DATA_PATH=")
_OWN_REAPER = os.sep + os.path.join("couch", "proc", "reaper")    # unser reaper (proc/manager), jede Fassung
_ENV_LIMIT = 1 << 20


def _read_proc(path: str, limit: int = _ENV_LIMIT) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _env_value(environ: list, key: bytes) -> str:
    for item in environ:
        if item.startswith(key):
            return os.fsdecode(item[len(key):])
    return ""


def _proton_build(base: str, environ: list) -> str:
    """Proton-Ordner, aus dem ein Prozess stammt ("" = nicht erkennbar): Programm unter
    <build>/files/bin (wineserver, wine), Protons Skript in der Befehlszeile, PROTONPATH (umu-run)
    oder WINEDLLPATH (setzt Proton für Wine)."""
    try:
        exe = os.readlink(os.path.join(base, "exe"))
    except OSError:
        exe = ""
    exe = exe[:-len(" (deleted)")] if exe.endswith(" (deleted)") else exe
    if "/files/bin/" in exe:
        return exe.split("/files/bin/", 1)[0]
    for arg in (_read_proc(os.path.join(base, "cmdline"), 65536) or b"").split(b"\0")[:8]:
        text = os.fsdecode(arg)
        if os.path.isabs(text) and os.path.basename(text) == "proton":
            return os.path.dirname(text)
    value = _env_value(environ, b"PROTONPATH=")
    if os.path.isabs(value):
        return value.rstrip("/")
    for part in _env_value(environ, b"WINEDLLPATH=").split(":"):
        if "/files/lib" in part:
            return part.split("/files/lib", 1)[0]
    return ""


def _from_home(proc_root: str, pid: int, own_pid: int, limit: int = 64) -> bool | None:
    """Stammt der Prozess von uns ab (Home oder unser reaper, auch einer aus der Zeit vor einem
    Neustart des Home)? None = nicht feststellbar (Eintrag fehlt oder unlesbar)."""
    current = pid
    for _ in range(limit):
        if current == own_pid:
            return True
        if current <= 1:
            return False
        base = os.path.join(proc_root, str(current))
        cmdline = _read_proc(os.path.join(base, "cmdline"), 65536)
        if cmdline and any(os.fsdecode(a).endswith(_OWN_REAPER) for a in cmdline.split(b"\0")[:4]):
            return True
        status = _read_proc(os.path.join(base, "status"), 65536)
        found = re.search(rb"^PPid:\s*(\d+)", status or b"", re.M)
        if not found:
            return None
        current = int(found.group(1))
    return None


def wine_processes(prefix: str, proc_root: str = "/proc", own_pid: int | None = None) -> list:
    """Prozesse, die in diesem Prefix arbeiten (E101): Wineserver und alles, dessen Umgebung den
    Prefix als WINEPREFIX oder STEAM_COMPAT_DATA_PATH nennt (umu-run, Protons Skript, Wine, das
    Spiel), auch außerhalb des eigenen Baums. Vergleich wie bei wineserver_pids (realpath, ohne /pfx).

    Rückgabe je Prozess, nach PID: {"pid", "comm", "wineserver": bool, "build": Proton-Ordner oder
    "", "own": True (stammt vom Home ab) | False (fremd) | None (unbekannt)}. Fremde Nutzer und
    Prozesse, die gerade enden, zählen nicht. Liest /proc (alle Umgebungen), kurz blockierend."""
    if not prefix:
        return []
    own_pid = os.getpid() if own_pid is None else own_pid
    want = {os.path.realpath(prefix), os.path.realpath(_strip_pfx(prefix))}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    found = []
    for name in names:
        if not name.isdigit() or int(name) == own_pid:
            continue
        base = os.path.join(proc_root, name)
        raw = _read_proc(os.path.join(base, "environ"))
        if not raw:
            continue                      # fremder Nutzer, Kernel-Thread oder schon weg
        environ = raw.split(b"\0")
        hit = False
        for key in _PREFIX_KEYS:
            value = _env_value(environ, key)
            if value and {os.path.realpath(value), os.path.realpath(_strip_pfx(value))} & want:
                hit = True
                break
        if not hit:
            continue
        comm = (_read_proc(os.path.join(base, "comm"), 64) or b"").strip()
        found.append({"pid": int(name), "comm": os.fsdecode(comm),
                      "wineserver": comm.startswith(b"wineserver"),
                      "build": _proton_build(base, environ),
                      "own": _from_home(proc_root, int(name), own_pid)})
    return sorted(found, key=lambda item: item["pid"])


# ---------------------------------------------------------------------------------------------
# Geprüftes Löschen des eigenen Zwischenordners

def _mount_points() -> list | None:
    """Alle Einhängepunkte; None, wenn die Liste nicht lesbar ist (dann wird nichts gelöscht)."""
    points = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
            for line in fh:
                fields = line.split(" ")
                if len(fields) > 4:
                    # Leerzeichen usw. stehen dort als \040
                    points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)),
                                         fields[4]))
    except OSError:
        return None
    return points


def removal_refusal(path: str) -> str | None:
    """Grund, warum path NICHT gelöscht werden darf; None = alle Prüfungen bestanden."""
    path = os.path.abspath(path)
    parent, name = os.path.dirname(path), os.path.basename(path)
    if not TMP_RE.match(name):
        return "Name ist kein Zwischenordner einer Übernahme"
    try:
        if not stat.S_ISREG(os.lstat(os.path.join(parent, ROOT_MARKER)).st_mode):
            raise OSError("keine Datei")
    except OSError:
        return "Elternordner ist nicht als couch-Prefix-Ordner markiert"
    try:
        st = os.lstat(path)
        parent_st = os.stat(parent)
    except OSError as err:
        return "nicht prüfbar: %s" % err
    if not stat.S_ISDIR(st.st_mode):
        return "kein Ordner (Symlink oder Datei)"
    if os.path.realpath(path) != os.path.join(os.path.realpath(parent), name):
        return "liegt nicht direkt im couch-Prefix-Ordner"
    if st.st_dev != parent_st.st_dev:
        return "ist selbst eine Einhängung"
    try:
        partial = os.lstat(os.path.join(path, PARTIAL))
    except OSError:
        return "Marke der unfertigen Kopie fehlt"
    if not stat.S_ISREG(partial.st_mode):
        return "Marke der unfertigen Kopie ist keine Datei"
    real = os.path.realpath(path)
    points = _mount_points()
    if points is None:
        return "Einhängungen nicht prüfbar (Liste unlesbar)"
    for point in points:
        if point == real or point.startswith(real + os.sep):
            return "enthält eine Einhängung: %s" % point
    return None


def remove_unfinished(path: str) -> bool:
    """Den eigenen unfertigen Zwischenordner entfernen. Die einzige Löschstelle des Moduls.

    Bedingungen: Name <slug>.tmp-<16 hex>, direkt im markierten couch-Prefix-Ordner, echter
    Ordner (lstat), keine Einhängung darin, Marke der unfertigen Kopie vorhanden. Gelöscht wird
    über die eine Löschstelle des Produkts (installer/trash.remove_own_dir): über
    Verzeichnis-Deskriptoren, Symlinks nur als Eintrag, an jeder Geräte- oder Einhängungsgrenze
    (auch Bind-Mounts desselben Dateisystems) ist Schluss. Blockierend.
    """
    if removal_refusal(path) is not None:
        return False
    from ..installer import trash
    try:
        trash.remove_own_dir(os.path.abspath(path))
    except (trash.Refuse, OSError, RecursionError):
        return False
    return not os.path.lexists(path)


def unfinished(root: str) -> list:
    """Übrig gebliebene Zwischenordner (z. B. nach einem Absturz mitten in der Kopie)."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [os.path.join(root, n) for n in names if TMP_RE.match(n)]


def cleanup_unfinished(root: str, max_age: float = 3600.0) -> list:
    """Alte Zwischenordner entfernen (beim Start des Home). Liefert die entfernten Pfade.

    max_age schützt eine Kopie, die gerade in einem anderen Prozess läuft. Blockierend.
    """
    removed = []
    now = time.time()
    for path in unfinished(root):
        try:
            age = now - os.lstat(os.path.join(path, PARTIAL)).st_mtime
        except OSError:
            continue
        if age >= max_age and remove_unfinished(path):
            removed.append(path)
    return removed


# ---------------------------------------------------------------------------------------------
# Übernahme

def ensure_root(root: str) -> None:
    """Den couch-Prefix-Ordner anlegen und markieren."""
    os.makedirs(root, mode=0o755, exist_ok=True)
    marker = os.path.join(root, ROOT_MARKER)
    if not os.path.lexists(marker):
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("Prefixe des Konsolen-Modus couch. Nicht von Hand verschieben.\n")


def _count_entries(folder: str, cancel) -> tuple:
    """(Einträge, Bytes) ohne Symlinks zu folgen und ohne das Dateisystem zu verlassen."""
    try:
        dev = os.lstat(folder).st_dev
    except OSError:
        return 1, 0
    count, size = 1, 0
    stack = [folder]
    while stack:
        if cancel is not None and cancel.is_set():
            break
        try:
            with os.scandir(stack.pop()) as it:
                for item in it:
                    try:
                        st = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    count += 1
                    if stat.S_ISDIR(st.st_mode):
                        if st.st_dev == dev:
                            stack.append(item.path)
                    elif stat.S_ISREG(st.st_mode):
                        size += st.st_size
        except OSError:
            continue
    return count, size


def _same_filesystem(a: str, b: str) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


class _Deadline:
    """Abbruch-Marke nach Zeit (wie threading.Event, nur mit is_set)."""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + max(0.0, seconds)
        self.hit = False

    def is_set(self) -> bool:
        if not self.hit and time.monotonic() >= self.end:
            self.hit = True
        return self.hit


def copy_estimate(src: str, dst: str, budget: float = 1.0) -> dict:
    """Wie groß wird die Kopie von src? Für die Rückfrage vor der Übernahme (E24).

    BLOCKIEREND, aber höchstens etwa budget Sekunden: Reicht die Zeit nicht (schlafende Platte,
    sehr großer Prefix), ist "complete" False und die Zahlen sind nur eine Untergrenze; die
    Rückfrage nennt dann keine Größe. Liest nur.

    Rückgabe: {"entries": int, "bytes": int, "complete": bool, "same_filesystem": bool}
    same_filesystem: Quelle und Zielordner liegen auf demselben Dateisystem (dann kann cp
    Reflinks anlegen, falls das Dateisystem sie kennt)."""
    limit = _Deadline(budget)
    entries, size = _count_entries(os.path.realpath(src), limit)
    probe = os.path.abspath(os.path.dirname(dst) or ".")
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    try:
        same = _same_filesystem(os.path.realpath(src), probe)
    except OSError:
        same = False
    return {"entries": entries, "bytes": size, "complete": not limit.hit, "same_filesystem": same}


def size_text(size: int) -> str:
    """Größe für einen deutschen Satz: "687 MB", "1,1 GB"."""
    size = max(0, int(size))
    if size >= 1000 ** 3:
        return ("%.1f GB" % (size / 1000 ** 3)).replace(".", ",")
    if size >= 1000 ** 2:
        return "%d MB" % round(size / 1000 ** 2)
    return "weniger als 1 MB"


def _rename_noreplace(src: str, dst: str) -> bool:
    """Umbenennen, ohne je etwas Vorhandenes zu ersetzen. False, wenn dst schon existiert."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        if fn(-100, os.fsencode(src), -100, os.fsencode(dst), 1) == 0:   # AT_FDCWD, RENAME_NOREPLACE
            return True
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            return False
        if code not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(code, os.strerror(code), dst)
    except AttributeError:
        pass
    # Dateisystem ohne RENAME_NOREPLACE: prüfen und umbenennen (kleines Zeitfenster bleibt)
    if os.path.lexists(dst):
        return False
    os.rename(src, dst)
    return True


def _write_json(path: str, data: dict) -> None:
    """Schreibt die Marke, ohne je einem Verweis zu folgen: neue Datei (O_EXCL, O_NOFOLLOW)
    daneben, dann rename. Bringt cp -a aus der Quelle einen Verweis gleichen Namens mit, ersetzt
    das rename nur den Eintrag; sein Ziel (irgendeine Datei außerhalb) bleibt unberührt."""
    tmp = "%s.new-%s" % (path, secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(jsontext.dumps(data, indent=1))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stop_child(proc) -> None:
    # Nur der eigene Kindprozess, über seine PID
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def adopt_prefix(src: str, dst: str, progress_cb=None, *, cancel=None, state: str = "",
                 entry_id: str = "", proc_root: str = "/proc", copy_argv=None) -> dict:
    """Einen Lutris-Prefix einmalig nach dst kopieren (cp -a --reflink=auto). BLOCKIEREND.

    src           Quelle; wird nur gelesen und nie verändert.
    dst           Ziel <prefix_root>/<slug>. Existiert es schon, wird NICHT kopiert.
    progress_cb   fn(erledigt, gesamt) in Einträgen; kommt aus dem aufrufenden Thread.
    cancel        Objekt mit is_set() (threading.Event): bricht ab und räumt auf.
    state         ursprünglicher prefix_state, landet in couch-prefix.json.
    copy_argv     nur für Tests: fn(src, tmp) -> argv statt des echten cp.

    Rückgabe: {"copied": bool, "dst": str, "src": str, "entries": int, "seconds": float,
               "marker": dict | None}
    Fehler: LaunchError (deutscher Satz), AdoptCancelled bei Abbruch. In beiden Fällen bleibt
    kein halber Zielordner zurück.

    Kopiert wird in einen Zwischenordner dst.tmp-<zufall> und erst der fertige Ordner wird
    umbenannt. Symlinks im Prefix (dosdevices, Benutzerordner, pfx) bleiben Symlinks.
    """
    src_real = os.path.realpath(src)
    dst = os.path.abspath(dst)
    root = os.path.dirname(dst)
    if os.path.lexists(dst):
        return {"copied": False, "dst": dst, "src": src_real, "entries": 0, "seconds": 0.0,
                "marker": read_marker(dst)}
    if not os.path.isdir(src_real):
        raise LaunchError(TEXT_ADOPT_FAILED, "Quelle fehlt: %s" % src, reason="adoption-failed")
    dst_real = os.path.join(os.path.realpath(root), os.path.basename(dst))
    if (dst_real == src_real or dst_real.startswith(src_real + os.sep)
            or src_real.startswith(dst_real + os.sep)):
        raise LaunchError(TEXT_ADOPT_FAILED, "Quelle und Ziel überschneiden sich: %s / %s"
                          % (src_real, dst_real), reason="adoption-failed")
    if wineserver_pids(src_real, proc_root):
        # Eine laufende Registry zu kopieren ergäbe einen halben Stand
        raise LaunchError(TEXT_WINESERVER, "Wineserver in %s" % src_real, reason="wineserver")

    ensure_root(root)
    started = time.monotonic()
    total, size = _count_entries(src_real, cancel)
    if not _same_filesystem(src_real, root):
        # Anderes Dateisystem: kein Reflink, die Kopie kostet den vollen Platz
        free = shutil.disk_usage(root).free
        if free < size + RESERVE_BYTES:
            raise LaunchError("Für die Übernahme der Spielstände fehlt Speicherplatz.",
                              "nötig %d MiB, frei %d MiB in %s" % (size >> 20, free >> 20, root),
                              reason="no-space")

    while True:
        tmp = "%s.tmp-%s" % (dst, secrets.token_hex(8))
        try:
            os.mkdir(tmp, 0o700)          # schlägt fehl, wenn es den Namen schon gibt
            break
        except FileExistsError:
            continue
    info = {
        "version": 1,
        "source": src_real,
        "source_given": src,
        "original_state": state or "unknown",
        "entry": entry_id,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "adopted": None,
    }
    _write_json(os.path.join(tmp, PARTIAL), info)

    def give_up(error):
        # Der Zwischenordner ist unser eigener; alles andere bleibt, wie es ist
        remove_unfinished(tmp)
        raise error

    argv = copy_argv(src_real, tmp) if copy_argv else [
        "cp", "-a", "-x", "--reflink=auto", "-v", "-T", "--", src_real, tmp]
    done = 0
    last_report = 0.0
    last_touch = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors,
                                    stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C"))
        except OSError as err:
            give_up(LaunchError(TEXT_ADOPT_FAILED, str(err), reason="adoption-failed"))
        fd = proc.stdout.fileno()
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    _stop_child(proc)
                    give_up(AdoptCancelled())
                if time.monotonic() - last_touch > 15:
                    # Lebenszeichen, damit cleanup_unfinished eine laufende Kopie in Ruhe lässt
                    last_touch = time.monotonic()
                    try:
                        os.utime(os.path.join(tmp, PARTIAL))
                    except OSError:
                        pass
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                done += chunk.count(b"\n")
                now = time.monotonic()
                if progress_cb and now - last_report >= 0.1:
                    last_report = now
                    progress_cb(min(done, total), total)
            code = proc.wait()
        except BaseException:
            _stop_child(proc)
            remove_unfinished(tmp)
            raise
        finally:
            proc.stdout.close()
        if code != 0:
            errors.seek(0)
            tail = errors.read()[-2000:].decode("utf-8", "replace").strip()
            give_up(LaunchError(TEXT_ADOPT_FAILED, "cp endete mit %d: %s" % (code, tail),
                                reason="adoption-failed"))

    info["adopted"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    info["entries"] = total
    _write_json(os.path.join(tmp, PARTIAL), info)
    try:
        renamed = _rename_noreplace(tmp, dst)
    except OSError as err:
        give_up(LaunchError(TEXT_ADOPT_FAILED, "Umbenennen: %s" % err, reason="adoption-failed"))
    if not renamed:
        # Jemand war schneller (zweiter Aufruf): dessen Ordner gilt, unserer verschwindet
        remove_unfinished(tmp)
        return {"copied": False, "dst": dst, "src": src_real, "entries": 0,
                "seconds": time.monotonic() - started, "marker": read_marker(dst)}
    os.replace(os.path.join(dst, PARTIAL), os.path.join(dst, MARKER))
    if progress_cb:
        progress_cb(total, total)
    return {"copied": True, "dst": dst, "src": src_real, "entries": total,
            "seconds": time.monotonic() - started, "marker": info}
