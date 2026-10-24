"""Papierkorb und Löschen für Archiv-Installationen (Bauplan E17, Kritik Befund 5).

Dies ist die EINZIGE Stelle des Produkts, die Ordner REKURSIV entfernen darf (SAFETY.md
Regel 13: purge, remove_own_dir; der Explorer, der System-Papierkorb, die Prefix-Kopie und die
Updates rufen remove_own_dir auf) und Installationen in den eigenen Papierkorb legt. Einzelne
Dateien entfernen sonst nur Stellen, die genau ihre eigene Datei kennen (E99), etwa files.py
(eigene halbe Kopien), xdgtrash.delete (Papierkorb-Einträge), fetcher._drop_part (eigene .part)
sowie eigene Zwischendateien, Marken und Anmeldedaten.
Am 2026-10-12 hat ein rm -rf durch eine Einhängung hindurch 88 GB Spieldateien vernichtet.
Deshalb gilt hier ohne Ausnahme:

  * Angefasst wird nur ein Ordner, der DIREKT im Spiele-Ordner liegt und unsere Markerdatei
    (.couch-install.json) mit dem Zufallswert des Bibliothekseintrags enthält. Fremde Ordner
    bekommen sie nur nach Bestätigung durch den Nutzer mit vollem Pfad (adopt_folder, E91).
    Einzige Ausnahme (E99): GOG-Spiele aus dem Store liegen eine feste Ebene tiefer, in
    <Spiele-Ordner>/GOG/<Titel> (STORE_LEVELS), ebenso itch.io-Spiele in <Spiele-Ordner>/itch.io/<Titel>
    (E100). Nur für Einträge mit archive "GOG" bzw. "itch.io", nur genau diese Ebene, mit allen
    Prüfungen auch für den Ordner der Ebene selbst und einem Marker, der den Eintrag beim Namen
    nennt (entry_id).
  * "Löschen" (delete_install) ist nur ein rename nach <Spiele-Ordner>/.couch-trash/<id>-<zeit>:
    gleiches Dateisystem, sofort, mit restore() umkehrbar. Es wird dabei nichts entfernt.
  * to_xdg_trash (E102) legt einen Ordner außerhalb des Spiele-Ordners (eine Proton-Version in
    compatibilitytools.d) per rename in den Papierkorb des Systems, mit denselben Prüfungen;
    zurück oder endgültig weg nur über den Papierkorb des Explorers (xdgtrash).
  * Endgültig entfernt nur purge(), und nur aus dem Papierkorb. Es arbeitet über
    Verzeichnis-Deskriptoren: Symlinks werden als Eintrag entfernt und nie verfolgt, jede
    Geräte- oder Einhängungsgrenze bricht ab, der Rest bleibt liegen.
  * Prefixe und Spielstände liegen nicht im Spiele-Ordner und werden hier nie berührt.
  * Jede Abweichung ist eine Verweigerung mit deutschem Grund. Lieber bleibt etwas liegen.

Aufbau der Datei: zuerst kleine Bausteine, von denen jeder genau eine Sache prüft, dann die
öffentlichen Funktionen, die diese Bausteine der Reihe nach aufrufen (nummerierte Schritte).

Nur Standardbibliothek, keine Importe aus dem übrigen Projekt. Alle Funktionen fassen die Platte
an und sind damit blockierend; sie gehören in einen Thread, nicht in die GLib-Schleife.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import time
from typing import Callable

from .. import jsontext

MARKER_NAME = ".couch-install.json"   # liegt in jedem von uns installierten Spielordner
TRASH_DIR = ".couch-trash"            # Papierkorb, direkt im Spiele-Ordner
STAGING_PREFIX = ".couch-staging-"    # Zwischenordner des Entpackers, direkt im Spiele-Ordner
STAGING_MARKER = ".couch-staging"     # Datei im Zwischenordner, Inhalt = sein Zufallswert
MARKER_MAX_BYTES = 256 * 1024
# Feste Store-Ebenen (E99): archive des Bibliothekseintrags -> Ordner direkt im Spiele-Ordner, in dem
# der Store seine Spiele anlegt. Nie tiefer, nie ein anderer Name.
STORE_LEVELS = {"GOG": "GOG", "itch.io": "itch.io"}

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
ENTRY_ID_RE = re.compile(r"^local:([0-9a-f][0-9a-f-]{7,35})$")
TRASH_ID_RE = re.compile(r"^[0-9a-f][0-9a-f-]{7,35}-\d{8}T\d{6}(-[2-9])?$")
STAGING_NAME_RE = re.compile(r"^" + re.escape(STAGING_PREFIX) + r"([0-9a-f]{32})$")

# Ordner öffnen: nie einem Symlink folgen. Dateien öffnen: zusätzlich nie an einer FIFO hängen bleiben.
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_RENAME_NOREPLACE = 1                 # Flag für renameat2(2)

Mounts = Callable[[], list]


class Refuse(Exception):
    """Verweigerung. message ist ein deutscher Satz für die Oberfläche, code ein festes Kürzel."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def result(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message}


# ====================================================================== Bausteine

def read_mounts() -> list:
    """Alle Einhängepunkte, die dieser Prozess sieht (Feld 5 jeder Zeile in /proc/self/mountinfo)."""
    points = []
    with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as fh:
        for line in fh:
            fields = line.split(" ")
            if len(fields) > 4:
                # Leerzeichen und Ähnliches stehen dort als Oktalfolge (\040)
                points.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def _mount_id(fd: int) -> int:
    """Kennung der Einhängung, zu der ein offener Deskriptor gehört.

    Anders als st_dev unterscheidet sie auch Bind-Mounts desselben Dateisystems."""
    with open("/proc/self/fdinfo/%d" % fd, encoding="ascii") as fh:
        for line in fh:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise Refuse("no-mount-id", "Die Einhängung des Ordners lässt sich nicht bestimmen.")


def _refuse_mounts_below(real: str, mounts: Mounts | None) -> None:
    """Verweigert, wenn real selbst ein Einhängepunkt ist oder einer darunter liegt."""
    try:
        points = list((mounts or read_mounts)())
    except OSError:
        raise Refuse("mounts-unknown", "Die Liste der Einhängungen ist nicht lesbar. Es wird nichts verschoben oder gelöscht.")
    for point in points:
        if point == real or str(point).startswith(real + "/"):
            raise Refuse("mount-inside", "In diesem Ordner ist ein Datenträger eingehängt (%s). Er wird nicht angefasst." % point)


def _split_path(path) -> tuple:
    """Zerlegt einen absoluten Pfad rein als Text in (Elternordner, letzter Abschnitt).

    Verweigert leere und relative Pfade, "/" und jeden Pfad mit "..". Fasst die Platte nicht an."""
    if not isinstance(path, str) or path.strip() == "":
        raise Refuse("empty-path", "Es ist kein Ordner angegeben.")
    if "\0" in path or not path.startswith("/"):
        raise Refuse("bad-path", "Der angegebene Ordner ist kein vollständiger Pfad.")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise Refuse("dotdot", "Der Pfad enthält „..“ und wird nicht angefasst.")
    if not parts:
        raise Refuse("is-root", "Das Wurzelverzeichnis „/“ wird nie angefasst.")
    return "/" + "/".join(parts[:-1]), parts[-1]


# Wo nie ein Spiele-Ordner liegen darf (aufgelöste Pfade): Systemordner, Laufzeit- und
# Arbeitsspeicher-Ordner (Lehre vom 2026-10-12), Temp-Ordner. /run/media/<nutzer>/… bleibt erlaubt.
_SYSTEM_ROOTS = ("/proc", "/sys", "/dev", "/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib64",
                 "/var/lib", "/var/log", "/var/cache")
_TEMP_ROOTS = ("/tmp", "/var/tmp")


def root_refusal(games_root) -> str | None:
    """Die EINE Regel, welcher Ordner Spiele-Ordner sein darf (Installer und Papierkorb fragen
    beide hier, damit der Installer nie etwas anlegt, was der Papierkorb nicht wieder entfernen
    darf). None = zulässig, sonst ein deutscher Satz. Fasst die Platte nur über realpath an."""
    if not isinstance(games_root, str) or not games_root.startswith("/") or "\0" in games_root:
        return "Es ist kein gültiger Spiele-Ordner eingestellt."
    real = os.path.realpath(games_root)
    bad = "Der eingestellte Spiele-Ordner (%s) ist dafür nicht zulässig." % real
    home = os.path.realpath(os.path.expanduser("~"))
    test_root = os.environ.get("COUCH_TEST_ROOT")

    def under(roots) -> bool:
        return any(real == r or real.startswith(r + "/") for r in roots)

    if real == "/" or real.count("/") < 2:             # "/" und Ordner direkt darunter
        return bad
    if real == home or home.startswith(real + "/"):    # das Home oder etwas darüber
        return bad
    if real.startswith(home + "/") and any(p.startswith(".") for p in real[len(home) + 1:].split("/")):
        return bad                                     # ~/.config, ~/.local/share/Steam …
    if under(_SYSTEM_ROOTS):
        return bad
    if under(("/run",)) and not under(("/run/media",)):
        return bad                                     # /run/user/<uid> (Laufzeitordner, tmpfs)
    if under(_TEMP_ROOTS) and not test_root:
        # Arbeitsspeicher bzw. wird aufgeräumt. Im Prüfmodus liegt alles im Käfig unter /tmp;
        # was dort außerhalb liegt, lehnt die Regel des Prüfmodus mit eigenem Satz ab
        return bad
    return None


def _open_root(games_root) -> tuple:
    """Öffnet den Spiele-Ordner. Rückgabe: (aufgelöster Pfad, Deskriptor, fstat).

    Der Aufrufer schließt den Deskriptor. Verweigert Wurzeln, die nie ein Spiele-Ordner sein können."""
    if not isinstance(games_root, str) or not games_root.startswith("/") or "\0" in games_root:
        raise Refuse("no-root", "Es ist kein gültiger Spiele-Ordner eingestellt.")
    real = os.path.realpath(games_root)
    refusal = root_refusal(games_root)
    if refusal is not None:
        raise Refuse("bad-root", refusal)
    test_root = os.environ.get("COUCH_TEST_ROOT")
    if test_root and not real.startswith(os.path.realpath(test_root) + "/"):
        # Prüfmodus: nie die echte Platte des Nutzers anfassen, auch nicht mit Vorgabe-Einstellungen
        raise Refuse("test-mode", "Prüfmodus: Der Spiele-Ordner (%s) liegt außerhalb des Testordners." % real)
    try:
        fd = os.open(real, _DIR_FLAGS)
    except OSError:
        raise Refuse("root-missing", "Die Spiele-Platte ist nicht eingehängt (%s)." % games_root)
    return real, fd, os.fstat(fd)


def _open_child_dir(parent_fd: int, name: str, parent_st: os.stat_result) -> tuple:
    """Öffnet den Unterordner name des Ordners hinter parent_fd. Rückgabe: (Deskriptor, fstat).

    Verweigert Symlinks, alles, was kein eigener echter Ordner ist, und jede Geräte- oder
    Einhängungsgrenze zwischen Eltern- und Unterordner."""
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)   # lstat: der Eintrag selbst
    except FileNotFoundError:
        raise Refuse("missing", "Der Ordner „%s“ ist nicht vorhanden." % name)
    if stat.S_ISLNK(st.st_mode):
        raise Refuse("symlink", "„%s“ ist ein Verweis (Symlink) und kein echter Ordner. Er wird nicht angefasst." % name)
    if not stat.S_ISDIR(st.st_mode):
        raise Refuse("not-a-dir", "„%s“ ist kein Ordner." % name)
    if st.st_uid != os.getuid():
        raise Refuse("foreign-owner", "„%s“ gehört einem anderen Benutzer." % name)
    if st.st_dev != parent_st.st_dev:
        raise Refuse("other-filesystem", "„%s“ liegt auf einem anderen Dateisystem als der Spiele-Ordner." % name)
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            raise Refuse("changed", "„%s“ hat sich während der Prüfung geändert." % name)
        if _mount_id(fd) != _mount_id(parent_fd):
            raise Refuse("mount-inside", "„%s“ ist selbst eine Einhängung und wird nicht angefasst." % name)
    except BaseException:
        os.close(fd)
        raise
    return fd, opened


def _read_file(dir_fd: int, name: str, limit: int) -> bytes:
    """Liest eine kleine gewöhnliche Datei aus dem Ordner hinter dir_fd (kein Symlink, keine FIFO)."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise Refuse("marker-invalid", "„%s“ ist keine gültige Markerdatei." % name)
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
        return data
    finally:
        os.close(fd)


def _read_marker_fd(dir_fd: int) -> dict:
    """Liest die Markerdatei des Ordners hinter dir_fd. Verweigert, wenn sie fehlt oder ungültig ist."""
    try:
        raw = _read_file(dir_fd, MARKER_NAME, MARKER_MAX_BYTES)
    except OSError:
        raise Refuse("marker-missing", "In diesem Ordner fehlt die Markerdatei der Konsole. "
                                       "Er wurde nicht von der Konsole installiert und wird nicht angefasst.")
    try:
        marker = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Refuse("marker-invalid", "Die Markerdatei in diesem Ordner ist beschädigt.")
    if not isinstance(marker, dict) or not _is_token(marker.get("token")):
        raise Refuse("marker-invalid", "Die Markerdatei in diesem Ordner ist unvollständig.")
    return marker


def read_marker(folder: str) -> dict:
    """Markerdatei eines Spielordners lesen (nur lesend). Wirft Refuse, wenn es keine gültige gibt."""
    try:
        fd = os.open(folder, _DIR_FLAGS)
    except OSError:
        raise Refuse("missing", "Der Ordner ist nicht vorhanden oder kein echter Ordner.")
    try:
        return _read_marker_fd(fd)
    finally:
        os.close(fd)


def _is_token(value) -> bool:
    return isinstance(value, str) and TOKEN_RE.match(value) is not None


def _refuse_other_filesystem(marker: dict, dir_fd: int, dir_st: os.stat_result) -> None:
    """Der Ordner muss auf dem Dateisystem liegen, auf dem er angelegt wurde.

    Der Marker hält st_dev und die Kennung des Dateisystems (statvfs f_fsid) fest. Die Gerätenummer
    eines USB-/LVM-Laufwerks kann sich nach einem Neustart ändern, die Kennung (bei ext4 und btrfs
    aus der UUID abgeleitet) nicht. Deshalb: Ist eine Kennung festgehalten, muss sie stimmen;
    nur wenn es keine gibt (0, etwa bei NTFS über FUSE), entscheidet die Gerätenummer."""
    recorded_fsid, recorded_dev = marker.get("fsid"), marker.get("st_dev")
    current_fsid = os.fstatvfs(dir_fd).f_fsid
    if isinstance(recorded_fsid, int) and not isinstance(recorded_fsid, bool) and recorded_fsid != 0 and current_fsid != 0:
        same = recorded_fsid == current_fsid
    else:
        same = isinstance(recorded_dev, int) and not isinstance(recorded_dev, bool) and recorded_dev == dir_st.st_dev
    if not same:
        raise Refuse("filesystem-changed", "Dieser Ordner liegt nicht mehr auf dem Dateisystem, auf dem er installiert wurde. "
                                           "Er wird nicht angefasst.")


def _open_trash(root_fd: int, root_st: os.stat_result, create: bool) -> tuple:
    """Öffnet <Spiele-Ordner>/.couch-trash, legt ihn bei create=True an. Rückgabe: (Deskriptor, fstat)."""
    try:
        os.stat(TRASH_DIR, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise Refuse("not-in-trash", "Der Papierkorb ist leer.")
        os.mkdir(TRASH_DIR, 0o700, dir_fd=root_fd)
    return _open_child_dir(root_fd, TRASH_DIR, root_st)


def _write_new_file(dir_fd: int, name: str, data: bytes) -> None:
    """Legt eine neue Datei an. Scheitert, wenn es den Namen schon gibt (nichts wird überschrieben)."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _unlink_file(dir_fd: int, name: str) -> None:
    """Entfernt eine einzelne gewöhnliche Datei (unsere Begleitdateien im Papierkorb). Nie einen Ordner."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode):
        raise Refuse("not-a-file", "„%s“ ist keine gewöhnliche Datei und bleibt liegen." % name)
    os.unlink(name, dir_fd=dir_fd)


def _exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def rename_noreplace(src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
    """rename, das nie etwas ersetzt: renameat2(2) mit RENAME_NOREPLACE.

    Gibt es das Ziel schon (auch als leeren Ordner oder Symlink), wirft es FileExistsError.
    Ein rename verschiebt nur den Verzeichniseintrag; es kopiert und löscht keine Daten."""
    at_fdcwd = -100
    src_fd = at_fdcwd if src_dir_fd is None else src_dir_fd
    dst_fd = at_fdcwd if dst_dir_fd is None else dst_dir_fd
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        err = ctypes.get_errno()
        if err not in (errno.EINVAL, errno.ENOSYS):
            raise OSError(err, os.strerror(err), dst)
    # Dieses Dateisystem kennt das Flag nicht (z. B. FUSE): erst prüfen, dann gewöhnlich umbenennen.
    try:
        os.stat(dst, dir_fd=dst_dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst)


def _remove_tree(dir_fd: int, dev: int, mount_id: int, keep: str | None = None,
                 tick: Callable[[], None] | None = None) -> None:
    """Leert den Ordner hinter dir_fd. Der Ordner selbst bleibt stehen.

    Regeln: Alles, was kein Ordner ist (Datei, Symlink, FIFO), wird als Eintrag entfernt; ein
    Symlink wird also nie verfolgt, sein Ziel bleibt unberührt. In einen Unterordner geht es nur,
    wenn er auf demselben Gerät UND in derselben Einhängung liegt wie die Wurzel. Jede Abweichung
    wirft Refuse bzw. OSError; was bis dahin nicht entfernt ist, bleibt liegen.
    keep: ein Name auf oberster Ebene, der stehen bleibt (der Marker, damit ein zweiter Versuch
    den Ordner noch als unseren erkennt).
    tick: wird VOR jedem entfernten Eintrag gerufen (Fortschritt). Wirft es Refuse, hört das
    Entfernen dort auf (Abbruch beim Beenden der Konsole); der Rest bleibt liegen."""
    for name in os.listdir(dir_fd):
        if name == keep:
            continue
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)      # lstat
        if not stat.S_ISDIR(st.st_mode):
            if tick is not None:
                tick()
            os.unlink(name, dir_fd=dir_fd)                            # entfernt den Eintrag, nie ein Ziel
            continue
        if st.st_dev != dev:
            raise Refuse("mount-inside", "Unterhalb liegt ein anderes Dateisystem (%s). Abbruch, der Rest bleibt liegen." % name)
        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                raise Refuse("changed", "„%s“ hat sich während des Löschens geändert. Abbruch." % name)
            if _mount_id(fd) != mount_id:
                raise Refuse("mount-inside", "Unterhalb liegt eine Einhängung (%s). Abbruch, der Rest bleibt liegen." % name)
            os.fchmod(fd, 0o700)        # Archive können schreibgeschützte Ordner enthalten
            _remove_tree(fd, dev, mount_id, tick=tick)
        finally:
            os.close(fd)
        if tick is not None:
            tick()
        os.rmdir(name, dir_fd=dir_fd)   # scheitert, wenn doch noch etwas darin liegt


def _count_tree(dir_fd: int, dev: int) -> int:
    """Zählt die Einträge unterhalb von dir_fd (Dateien, Verweise und Ordner). Nur lesend; folgt
    keinem Symlink und verlässt das Gerät nicht (dieselben Grenzen wie _remove_tree)."""
    total = 0
    for name in os.listdir(dir_fd):
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        total += 1
        if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                total += _count_tree(fd, dev)
            finally:
                os.close(fd)
    return total


def _size_tree(dir_fd: int, dev: int) -> int:
    """Belegter Platz unterhalb von dir_fd in Bytes (st_blocks, wie du). Nur lesend, dieselben
    Grenzen wie _remove_tree: kein Symlink wird verfolgt, das Gerät nie verlassen."""
    total = 0
    for name in os.listdir(dir_fd):
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        total += st.st_blocks * 512
        if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
            try:
                fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError:
                continue
            try:
                total += _size_tree(fd, dev)
            finally:
                os.close(fd)
    return total


def _store_level(entry: dict, parent: str, real_root: str) -> str | None:
    """Wo der Ordner liegen darf: None = direkt im Spiele-Ordner, sonst der Name der festen Store-Ebene
    (E99), in der er liegt. Rein als Text über realpath; geöffnet wird erst danach über Deskriptoren.
    Verweigert jeden anderen Ort."""
    real_parent = os.path.realpath(parent)
    if real_parent == real_root:
        return None
    archive = entry.get("archive")
    level = STORE_LEVELS.get(archive) if isinstance(archive, str) else None
    if level is not None and real_parent == real_root + "/" + level:
        return level
    raise Refuse("not-direct-child", "Der Ordner liegt nicht direkt im Spiele-Ordner (%s) und wird nicht angefasst." % real_root)


def _guarded(fn: Callable[[], dict], failure: str) -> dict:
    """Führt fn aus und macht aus jeder Verweigerung und jedem Plattenfehler ein Ergebnis mit Grund."""
    try:
        return fn()
    except Refuse as refuse:
        return refuse.result()
    except (OSError, RecursionError, TypeError, ValueError) as exc:
        # Plattenfehler, zu tiefer Baum oder ein Eintrag, der sich nicht als JSON ablegen lässt
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        return {"ok": False, "code": "os-error", "message": "%s (%s)" % (failure, detail)}


# ====================================================================== Löschen = in den Papierkorb

def delete_install(entry: dict, games_root: str, *, mounts: Mounts | None = None,
                   now: Callable[[], float] | None = None) -> dict:
    """Verschiebt eine Archiv-Installation in den Papierkorb. Blockierend.

    entry: Bibliothekseintrag {"id": "local:<uuid>", "install_dir": ..., "marker": <Zufallswert>, ...}.
    Rückgabe: {"ok": True, "trash_id", "path", "message"} oder {"ok": False, "code", "message"}.
    Es wird nichts gelöscht: genau ein rename innerhalb des Spiele-Ordners."""
    return _guarded(lambda: _delete_install(entry, games_root, mounts, now or time.time),
                    "Das Spiel konnte nicht in den Papierkorb verschoben werden.")


def _delete_install(entry, games_root, mounts, now) -> dict:
    # 1 Der Eintrag: nur eigene Archiv-Installationen, mit Zufallswert und Ordner
    if not isinstance(entry, dict):
        raise Refuse("bad-entry", "Der Bibliothekseintrag ist ungültig.")
    match = ENTRY_ID_RE.match(entry.get("id")) if isinstance(entry.get("id"), str) else None
    if match is None:
        raise Refuse("not-local", "Nur Spiele, die die Konsole selbst aus einem Archiv installiert hat, lassen sich löschen.")
    token = entry.get("marker")
    if not _is_token(token):
        raise Refuse("no-token", "Zu diesem Spiel ist kein Marker gespeichert. Es wird nicht angefasst.")

    # 2 Der Pfad, rein als Text: nicht leer, nicht "/", absolut, ohne ".."; kein versteckter Name
    parent, name = _split_path(entry.get("install_dir"))
    if name.startswith("."):
        raise Refuse("hidden", "Versteckte Ordner werden nicht angefasst.")

    # 3 Der Spiele-Ordner (aufgelöst, geöffnet)
    real_root, root_fd, root_st = _open_root(games_root)
    level_fd = None
    try:
        # 4 Der Ordner ist nicht der Spiele-Ordner selbst und liegt direkt darin (oder in der festen
        #   Store-Ebene seines Shops, E99)
        if os.path.realpath(parent + "/" + name) == real_root:
            raise Refuse("is-games-root", "Der Spiele-Ordner selbst wird nie gelöscht.")
        level = _store_level(entry, parent, real_root)

        # 4a Die Store-Ebene selbst: echter eigener Ordner, kein Symlink, gleiches Gerät, gleiche
        #    Einhängung wie der Spiele-Ordner (dieselbe Prüfung wie für jeden Spielordner)
        parent_fd, parent_st = root_fd, root_st
        if level is not None:
            level_fd, level_st = _open_child_dir(root_fd, level, root_st)
            parent_fd, parent_st = level_fd, level_st
        rel = (level + "/" + name) if level is not None else name

        # 5 lstat und Öffnen über den Deskriptor des Elternordners: echter eigener Ordner,
        #   kein Symlink, gleiches Gerät, gleiche Einhängung. Ab hier gilt nur noch (parent_fd, name).
        item_fd, item_st = _open_child_dir(parent_fd, name, parent_st)
        try:
            # 6 Der aufgelöste Pfad bleibt unter dem Spiele-Ordner (doppelte Naht zu Schritt 4 und 5)
            real_item = os.path.realpath(real_root + "/" + rel)
            if real_item != real_root + "/" + rel:
                raise Refuse("escapes-root", "Der Ordner führt aus dem Spiele-Ordner hinaus und wird nicht angefasst.")

            # 7 Markerdatei im Ordner: vorhanden, gültig, Zufallswert gleich dem des Eintrags;
            #   in einer Store-Ebene muss sie außerdem genau diesen Eintrag nennen
            marker = _read_marker_fd(item_fd)
            if marker["token"] != token:
                raise Refuse("marker-mismatch", "Die Markerdatei im Ordner gehört nicht zu diesem Bibliothekseintrag. "
                                                "Er wird nicht angefasst.")
            if level is not None and marker.get("entry_id") != entry.get("id"):
                raise Refuse("marker-mismatch", "Die Markerdatei im Ordner nennt ein anderes Spiel. Er wird nicht angefasst.")

            # 8 Gleiches Dateisystem wie beim Anlegen
            _refuse_other_filesystem(marker, item_fd, item_st)

            # 9 Keine Einhängung im oder unter dem Ordner
            _refuse_mounts_below(real_item, mounts)

            # 10 Papierkorb öffnen (bei Bedarf anlegen) und einen freien Namen wählen
            trash_fd, _trash_st = _open_trash(root_fd, root_st, create=True)
            try:
                stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now()))
                trash_id = "%s-%s" % (match.group(1), stamp)
                for suffix in ("", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"):
                    if not _exists(trash_fd, trash_id + suffix) and not _exists(trash_fd, trash_id + suffix + ".json"):
                        trash_id += suffix
                        break
                else:
                    raise Refuse("trash-busy", "Im Papierkorb liegt schon ein Eintrag mit diesem Namen. Bitte gleich noch einmal versuchen.")

                # 11 Begleitdatei mit allem, was restore() braucht (zuerst, damit nie ein Ordner ohne sie im Papierkorb liegt)
                info = {"version": 1, "trash_id": trash_id, "name": name, "token": token,
                        "title": str(entry.get("title") or name), "deleted": now(), "entry": entry}
                if level is not None:
                    info["level"] = level           # restore() legt ihn dorthin zurück
                _write_new_file(trash_fd, trash_id + ".json", jsontext.dumps(info, indent=1).encode("utf-8"))

                # 12 Der einzige Eingriff: rename im selben Dateisystem, ersetzt nie etwas
                try:
                    rename_noreplace(name, trash_id, src_dir_fd=parent_fd, dst_dir_fd=trash_fd)
                except BaseException:
                    _unlink_file(trash_fd, trash_id + ".json")
                    raise

                # 13 Nachkontrolle: Im Papierkorb liegt genau der Ordner, der geprüft wurde. Sonst zurück.
                moved = os.stat(trash_id, dir_fd=trash_fd, follow_symlinks=False)
                if (moved.st_dev, moved.st_ino) != (item_st.st_dev, item_st.st_ino):
                    rename_noreplace(trash_id, name, src_dir_fd=trash_fd, dst_dir_fd=parent_fd)
                    _unlink_file(trash_fd, trash_id + ".json")
                    raise Refuse("changed", "Der Ordner hat sich während des Verschiebens geändert. Es wurde nichts verändert.")
            finally:
                os.close(trash_fd)
        finally:
            os.close(item_fd)
    finally:
        if level_fd is not None:
            os.close(level_fd)
        os.close(root_fd)
    return {"ok": True, "trash_id": trash_id, "path": "%s/%s/%s" % (real_root, TRASH_DIR, trash_id),
            "message": "„%s“ wurde in den Papierkorb verschoben." % info["title"]}


# ====================================================================== Von Hand hinzugefügte Spiele (E91)
# Spiele aus dem Datei-Explorer haben keinen Marker: Die Konsole hat ihren Ordner nicht angelegt.
# Gelöscht wird ein solcher Ordner nur, nachdem der Nutzer ihn am Bildschirm mit vollem Pfad bestätigt
# hat. Dann bekommt er unseren Marker (adopt_folder), und es gilt derselbe Weg wie für eigene
# Installationen (delete_install). Ein Ordner mit fremdem Marker wird nie übernommen.

def foreign_folder(install_dir, games_root) -> str:
    """Der Ordner direkt im Spiele-Ordner, in dem install_dir liegt (aufgelöst). Nur lesend.

    Wirft Refuse, wenn install_dir nicht im Spiele-Ordner liegt, der Spiele-Ordner selbst ist
    oder der Ordner versteckt ist (.couch-trash, Zwischenordner des Entpackers …)."""
    _split_path(install_dir)
    real_root, root_fd, _root_st = _open_root(games_root)
    os.close(root_fd)
    real = os.path.realpath(install_dir)
    if real == real_root:
        raise Refuse("is-games-root", "Das Spiel liegt direkt im Spiele-Ordner. Der wird nie gelöscht.")
    if not real.startswith(real_root + "/"):
        raise Refuse("outside-root", "Das Spiel liegt nicht im Spiele-Ordner (%s). Sein Ordner wird nicht angefasst." % real_root)
    name = real[len(real_root) + 1:].split("/", 1)[0]
    if name.startswith("."):
        raise Refuse("hidden", "Versteckte Ordner werden nicht angefasst.")
    return real_root + "/" + name


def adopt_folder(folder: str, games_root: str, token: str, fields: dict | None = None, *,
                 mounts: Mounts | None = None) -> dict:
    """Legt unseren Marker in einen fremden Spielordner direkt im Spiele-Ordner. Blockierend.

    Nur nach Bestätigung durch den Nutzer mit genau diesem Pfad aufrufen. Prüft wie delete_install: echter
    eigener Ordner, kein Symlink, gleiches Dateisystem, keine Einhängung darin. Liegt dort schon
    eine Markerdatei, wird nichts geschrieben.
    → {"ok": True, "marker"} oder {"ok": False, "code", "message"}."""
    return _guarded(lambda: _adopt_folder(folder, games_root, token, fields or {}, mounts),
                    "Der Ordner konnte nicht vorbereitet werden.")


def _adopt_folder(folder, games_root, token, fields, mounts) -> dict:
    if not _is_token(token):
        raise Refuse("no-token", "Ohne Zufallswert wird kein Marker geschrieben.")
    parent, name = _split_path(folder)
    if name.startswith("."):
        raise Refuse("hidden", "Versteckte Ordner werden nicht angefasst.")
    real_root, root_fd, root_st = _open_root(games_root)
    try:
        if os.path.realpath(parent) != real_root or real_root + "/" + name == real_root:
            raise Refuse("not-direct-child", "Der Ordner liegt nicht direkt im Spiele-Ordner (%s) und wird nicht angefasst." % real_root)
        item_fd, _item_st = _open_child_dir(root_fd, name, root_st)
        try:
            if os.path.realpath(real_root + "/" + name) != real_root + "/" + name:
                raise Refuse("escapes-root", "Der Ordner führt aus dem Spiele-Ordner hinaus und wird nicht angefasst.")
            if _exists(item_fd, MARKER_NAME):
                raise Refuse("marker-present", "In diesem Ordner liegt schon eine Markerdatei. Er wird nicht angefasst.")
            _refuse_mounts_below(real_root + "/" + name, mounts)
            st = os.fstat(item_fd)
            marker = dict(fields, token=token, adopted=True, st_dev=st.st_dev, fsid=os.fstatvfs(item_fd).f_fsid)
            _write_new_file(item_fd, MARKER_NAME, jsontext.dumps(marker, indent=1).encode("utf-8"))
        finally:
            os.close(item_fd)
    finally:
        os.close(root_fd)
    return {"ok": True, "marker": marker}


def drop_adopted_marker(folder: str, token: str) -> None:
    """Nimmt einen Marker von adopt_folder wieder heraus (das Verschieben ist danach gescheitert).
    Nur unseren, mit genau diesem Zufallswert; alles andere bleibt liegen."""
    try:
        fd = os.open(folder, _DIR_FLAGS)
    except OSError:
        return
    try:
        marker = _read_marker_fd(fd)
        if marker.get("token") == token and marker.get("adopted") is True:
            _unlink_file(fd, MARKER_NAME)
    except (Refuse, OSError):
        pass
    finally:
        os.close(fd)


# ====================================================================== Papierkorb: ansehen, zurückholen

def list_trash(games_root: str) -> list:
    """Einträge des Papierkorbs, neueste zuerst. Nur lesend, blockierend.

    Je Eintrag: {"trash_id", "title", "name", "deleted", "entry_id", "present", "purging"}."""
    try:
        _real_root, root_fd, root_st = _open_root(games_root)
    except Refuse:
        return []
    try:
        try:
            trash_fd, _trash_st = _open_trash(root_fd, root_st, create=False)
        except (Refuse, OSError):
            return []
        try:
            items = []
            for filename in os.listdir(trash_fd):
                trash_id = filename[:-5] if filename.endswith(".json") else ""
                if not TRASH_ID_RE.match(trash_id):
                    continue
                try:
                    info = _read_sidecar(trash_fd, trash_id)
                except (Refuse, OSError):
                    continue
                entry = info.get("entry") if isinstance(info.get("entry"), dict) else {}
                items.append({"trash_id": trash_id, "title": str(info.get("title") or info["name"]), "name": info["name"],
                              "deleted": info.get("deleted"), "entry_id": entry.get("id"),
                              "present": _exists(trash_fd, trash_id),
                              "purging": _exists(trash_fd, trash_id + ".purging")})
            items.sort(key=lambda item: item["deleted"] if isinstance(item["deleted"], (int, float)) else 0, reverse=True)
            return items
        finally:
            os.close(trash_fd)
    finally:
        os.close(root_fd)


def _read_sidecar(trash_fd: int, trash_id: str) -> dict:
    """Liest die Begleitdatei <trash_id>.json. Verweigert, wenn sie fehlt oder nicht zu uns passt."""
    try:
        raw = _read_file(trash_fd, trash_id + ".json", MARKER_MAX_BYTES)
        info = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        raise Refuse("not-in-trash", "Dieser Eintrag ist nicht im Papierkorb.")
    if (not isinstance(info, dict) or info.get("trash_id") != trash_id or not _is_token(info.get("token"))
            or not isinstance(info.get("name"), str)):
        raise Refuse("sidecar-invalid", "Die Angaben zu diesem Papierkorb-Eintrag sind beschädigt. Er bleibt liegen.")
    return info


def _check_trash_id(trash_id) -> str:
    if not isinstance(trash_id, str) or not TRASH_ID_RE.match(trash_id):
        raise Refuse("bad-trash-id", "Das ist kein gültiger Papierkorb-Eintrag.")
    return trash_id


def restore(trash_id: str, games_root: str, *, mounts: Mounts | None = None) -> dict:
    """Holt einen Ordner aus dem Papierkorb an seinen alten Platz zurück. Blockierend.

    Rückgabe: {"ok": True, "entry": <Bibliothekseintrag>, "install_dir", "message"} oder Verweigerung."""
    return _guarded(lambda: _restore(trash_id, games_root, mounts),
                    "Das Spiel konnte nicht wiederhergestellt werden.")


def _restore(trash_id, games_root, mounts) -> dict:
    # 1 Kennung des Papierkorb-Eintrags: festes Muster, also nie ein Pfad
    _check_trash_id(trash_id)
    # 2 Spiele-Ordner und Papierkorb öffnen
    real_root, root_fd, root_st = _open_root(games_root)
    try:
        trash_fd, trash_st = _open_trash(root_fd, root_st, create=False)
        try:
            # 3 Begleitdatei: alter Name und Zufallswert
            info = _read_sidecar(trash_fd, trash_id)
            name = info["name"]
            if "/" in name or name in ("", ".", "..") or name.startswith(".") or "\0" in name:
                raise Refuse("sidecar-invalid", "Der gespeicherte Ordnername ist ungültig. Der Eintrag bleibt im Papierkorb.")
            if _exists(trash_fd, trash_id + ".purging"):
                raise Refuse("purging", "Dieser Eintrag wurde schon teilweise endgültig gelöscht und lässt sich nicht wiederherstellen.")
            # 4 Der Ordner im Papierkorb: echter eigener Ordner mit passendem Marker, ohne Einhängung
            item_fd, _item_st = _open_child_dir(trash_fd, trash_id, trash_st)
            try:
                if _read_marker_fd(item_fd)["token"] != info["token"]:
                    raise Refuse("marker-mismatch", "Der Ordner im Papierkorb passt nicht zu seinen Angaben. Er bleibt liegen.")
            finally:
                os.close(item_fd)
            _refuse_mounts_below("%s/%s/%s" % (real_root, TRASH_DIR, trash_id), mounts)
            # 5 Zurück an den alten Platz: direkt in den Spiele-Ordner oder in seine Store-Ebene (E99),
            #   die wie jeder Ordner geprüft geöffnet wird. Dort darf nichts liegen; rename ersetzt nie etwas
            level = info.get("level")
            if level is not None and level not in STORE_LEVELS.values():
                raise Refuse("sidecar-invalid", "Der gespeicherte Ort ist ungültig. Der Eintrag bleibt im Papierkorb.")
            dest_fd = root_fd
            if level is not None:
                try:
                    dest_fd, _level_st = _open_child_dir(root_fd, level, root_st)
                except Refuse as refuse:
                    raise Refuse("level-missing", "Der Ordner %s im Spiele-Ordner fehlt oder ist kein echter Ordner (%s). "
                                                  "Der Eintrag bleibt im Papierkorb." % (level, refuse.message))
            try:
                rename_noreplace(trash_id, name, src_dir_fd=trash_fd, dst_dir_fd=dest_fd)
            except FileExistsError:
                raise Refuse("target-exists", "Im Spiele-Ordner liegt inzwischen ein anderer Ordner „%s“. "
                                              "Der Eintrag bleibt im Papierkorb." % name)
            finally:
                if dest_fd != root_fd:
                    os.close(dest_fd)
            # 6 Begleitdatei entfernen (eine einzelne eigene Datei)
            _unlink_file(trash_fd, trash_id + ".json")
        finally:
            os.close(trash_fd)
    finally:
        os.close(root_fd)
    entry = info.get("entry") if isinstance(info.get("entry"), dict) else None
    place = real_root + "/" + (level + "/" if level is not None else "") + name
    return {"ok": True, "entry": entry, "install_dir": place,
            "message": "„%s“ wurde wiederhergestellt." % (info.get("title") or name)}


# ====================================================================== endgültig entfernen

def purge(trash_id: str, games_root: str, *, mounts: Mounts | None = None,
          tick: Callable[[], None] | None = None) -> dict:
    """Entfernt einen Papierkorb-Eintrag endgültig. Blockierend, kann bei großen Spielen dauern.

    Läuft nie von selbst beim Löschen, nur auf ausdrückliche Bestätigung oder über purge_expired().
    Rückgabe: {"ok": True, "message"} oder {"ok": False, "code", "message"}; bei einer Verweigerung
    mitten im Lauf bleibt der Rest im Papierkorb liegen.
    tick: wird vor jedem entfernten Eintrag gerufen (Fortschritt, siehe count_item). Wirft es
    Refuse, bricht der Lauf dort ab; ein späterer purge() macht an der Stelle weiter."""
    return _guarded(lambda: _purge(trash_id, games_root, mounts, tick),
                    "Der Papierkorb-Eintrag konnte nicht vollständig entfernt werden. Der Rest bleibt liegen.")


def count_item(trash_id: str, games_root: str) -> int | None:
    """Zahl der Einträge, die purge() für diesen Papierkorb-Eintrag entfernen wird (für die
    Fortschrittsanzeige). Nur lesend, blockierend (ein Durchgang durch den Baum). None, wenn sich
    der Eintrag nicht zweifelsfrei als unserer öffnen lässt (purge() nennt dann den Grund)."""
    try:
        _check_trash_id(trash_id)
        _real_root, root_fd, root_st = _open_root(games_root)
        try:
            trash_fd, trash_st = _open_trash(root_fd, root_st, create=False)
            try:
                _read_sidecar(trash_fd, trash_id)
                item_fd, item_st = _open_child_dir(trash_fd, trash_id, trash_st)
                try:
                    return _count_tree(item_fd, item_st.st_dev)
                finally:
                    os.close(item_fd)
            finally:
                os.close(trash_fd)
        finally:
            os.close(root_fd)
    except (Refuse, OSError, RecursionError):
        return None


def item_size(trash_id: str, games_root: str) -> int | None:
    """Platz in Bytes, den purge() für diesen Papierkorb-Eintrag frei macht (für die Rückfrage
    „Endgültig löschen“). Nur lesend, blockierend (ein Durchgang durch den Baum). None, wenn sich
    der Eintrag nicht zweifelsfrei als unserer öffnen lässt."""
    try:
        _check_trash_id(trash_id)
        _real_root, root_fd, root_st = _open_root(games_root)
        try:
            trash_fd, trash_st = _open_trash(root_fd, root_st, create=False)
            try:
                _read_sidecar(trash_fd, trash_id)
                item_fd, item_st = _open_child_dir(trash_fd, trash_id, trash_st)
                try:
                    return item_st.st_blocks * 512 + _size_tree(item_fd, item_st.st_dev)
                finally:
                    os.close(item_fd)
            finally:
                os.close(trash_fd)
        finally:
            os.close(root_fd)
    except (Refuse, OSError, RecursionError):
        return None


def _purge(trash_id, games_root, mounts, tick=None) -> dict:
    # 1 Kennung: festes Muster, nie ein Pfad
    _check_trash_id(trash_id)
    # 2 Spiele-Ordner und Papierkorb (beides echte eigene Ordner auf demselben Gerät)
    real_root, root_fd, root_st = _open_root(games_root)
    try:
        trash_fd, trash_st = _open_trash(root_fd, root_st, create=False)
        try:
            # 3 Begleitdatei muss da sein und den Zufallswert nennen
            info = _read_sidecar(trash_fd, trash_id)
            if not _exists(trash_fd, trash_id):
                # Der Ordner ist schon weg (von Hand entfernt): nur noch die eigenen Begleitdateien aufräumen
                _unlink_file(trash_fd, trash_id + ".purging")
                _unlink_file(trash_fd, trash_id + ".json")
                return {"ok": True, "trash_id": trash_id, "message": "Der Eintrag war schon gelöscht."}
            # 4 Der Ordner: direkt im Papierkorb, echter eigener Ordner, kein Symlink, gleiche Einhängung
            item_fd, item_st = _open_child_dir(trash_fd, trash_id, trash_st)
            try:
                # 5 Marker im Ordner gehört zur Begleitdatei
                if _read_marker_fd(item_fd)["token"] != info["token"]:
                    raise Refuse("marker-mismatch", "Der Ordner im Papierkorb passt nicht zu seinen Angaben. Er bleibt liegen.")
                # 6 Keine Einhängung im oder unter dem Ordner
                _refuse_mounts_below("%s/%s/%s" % (real_root, TRASH_DIR, trash_id), mounts)
                # 7 Ab jetzt ist der Ordner unvollständig: restore() sperren
                if not _exists(trash_fd, trash_id + ".purging"):
                    _write_new_file(trash_fd, trash_id + ".purging", b"")
                # 8 Inhalt entfernen; der Marker bleibt bis zuletzt stehen
                _remove_tree(item_fd, item_st.st_dev, _mount_id(item_fd), keep=MARKER_NAME, tick=tick)
                _unlink_file(item_fd, MARKER_NAME)
            finally:
                os.close(item_fd)
            # 9 Den nun leeren Ordner und die beiden Begleitdateien entfernen
            os.rmdir(trash_id, dir_fd=trash_fd)
            _unlink_file(trash_fd, trash_id + ".purging")
            _unlink_file(trash_fd, trash_id + ".json")
        finally:
            os.close(trash_fd)
    finally:
        os.close(root_fd)
    return {"ok": True, "trash_id": trash_id, "message": "„%s“ wurde endgültig gelöscht." % (info.get("title") or info["name"])}


# Vor diesem Zeitpunkt (2026-01-01) gab es couch nicht: Ein so alter Löschzeitpunkt stammt aus einer
# falsch gehenden Uhr (leere CMOS-Batterie) und wird nie von selbst endgültig gelöscht.
EARLIEST_DELETED = 1767225600.0


def expired(deleted, current: float, days: int) -> bool:
    """Ist ein Eintrag nach der Wanduhr abgelaufen? Nur bei glaubwürdigem Löschzeitpunkt: nicht vor
    EARLIEST_DELETED und nicht in der Zukunft (die Uhr ging beim Löschen vor oder geht jetzt nach)."""
    if not isinstance(deleted, (int, float)) or isinstance(deleted, bool):
        return False
    if deleted < EARLIEST_DELETED or deleted > current:
        return False
    return current - deleted > days * 86400


def purge_expired(games_root: str, days: int, *, mounts: Mounts | None = None,
                  now: Callable[[], float] | None = None, tick: Callable[[], None] | None = None,
                  seen: dict | None = None, min_days_seen: int = 1) -> list:
    """Entfernt Papierkorb-Einträge, die älter als days Tage sind. days < 1: nichts. Blockierend.

    Die Wanduhr allein reicht nicht (vorgestellte Uhr = sofortiger Verlust): seen nennt je
    trash_id die verschiedenen Tage, an denen ein Start den Eintrag im Papierkorb gesehen hat
    (führt der Aufrufer, siehe service.py); gelöscht wird nur, wenn es mindestens min_days_seen
    sind. Löschzeitpunkte vor EARLIEST_DELETED oder in der Zukunft gelten nie als abgelaufen.
    Rückgabe: Liste der purge()-Ergebnisse, jeweils mit "trash_id" und "title".
    tick: wie bei purge() (Abbruch beim Beenden der Konsole); nach einem Abbruch wird kein
    weiterer Eintrag mehr begonnen."""
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        return []
    current = (now or time.time)()
    results = []
    for item in list_trash(games_root):
        if not expired(item["deleted"], current, days):
            continue
        if seen is not None and len(set(seen.get(item["trash_id"]) or ())) < min_days_seen:
            continue
        result = purge(item["trash_id"], games_root, mounts=mounts, tick=tick)
        result["trash_id"] = item["trash_id"]
        result["title"] = item["title"]
        results.append(result)
        if result.get("code") == "cancelled":
            break
    return results


# ====================================================================== In den Papierkorb des Systems (E102)

XDG_INFO_SUFFIX = ".trashinfo"


def to_xdg_trash(path: str, trash_dir: str, *, mounts: Mounts | None = None,
                 now: Callable[[], float] | None = None) -> dict:
    """Verschiebt einen Ordner in einen XDG-Papierkorb: <trash_dir>/files/<name>, daneben
    info/<name>.trashinfo mit altem Ort und Zeit (E102: eine Proton-Version aus compatibilitytools.d
    entfernen). Nur ein rename auf demselben Dateisystem, es wird nichts kopiert oder gelöscht; der
    Datei-Explorer legt es von dort zurück oder leert es (xdgtrash.restore/delete). Blockierend.

    Geprüft wie beim Löschen: echter Ordner des eigenen Nutzers (kein Symlink), nicht versteckt,
    gleiches Gerät und gleiche Einhängung wie sein Elternordner, keine Einhängung darin, Papierkorb
    auf demselben Dateisystem. Im Prüfmodus liegen Ordner und Papierkorb im Käfig.
    Rückgabe {"ok": True, "path", "info", "message"} oder {"ok": False, "code", "message"}."""
    return _guarded(lambda: _to_xdg_trash(path, trash_dir, mounts, now or time.time),
                    "Der Ordner ließ sich nicht in den Papierkorb verschieben.")


def _open_dir_in(parent_fd: int, name: str, parent_st: os.stat_result) -> tuple:
    """Unterordner öffnen, fehlt er: anlegen (0700). Prüfungen wie _open_child_dir."""
    if not _exists(parent_fd, name):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    return _open_child_dir(parent_fd, name, parent_st)


def _to_xdg_trash(path, trash_dir, mounts, now) -> dict:
    import urllib.parse
    # 1 Pfade erst als Text, dann aufgelöst; im Prüfmodus nur im Käfig
    parent, name = _split_path(path)
    if name.startswith("."):
        raise Refuse("hidden", "Versteckte Ordner werden nicht angefasst.")
    _split_path(trash_dir)
    real_parent = os.path.realpath(parent)
    real_trash = os.path.realpath(trash_dir)
    test_root = os.environ.get("COUCH_TEST_ROOT")
    if test_root:
        cage = os.path.realpath(test_root)
        for p in (real_parent, real_trash):
            if not p.startswith(cage + "/"):
                raise Refuse("test-mode", "Prüfmodus: %s liegt außerhalb des Testordners." % p)
    try:
        parent_fd = os.open(real_parent, _DIR_FLAGS)
    except OSError:
        raise Refuse("missing", "Der Elternordner ist nicht vorhanden oder kein echter Ordner.")
    fds: list = []
    try:
        parent_st = os.fstat(parent_fd)
        # 2 Der Ordner selbst: echt, eigen, gleiches Gerät und gleiche Einhängung, nichts darin eingehängt
        item_fd, item_st = _open_child_dir(parent_fd, name, parent_st)
        os.close(item_fd)
        _refuse_mounts_below(real_parent + "/" + name, mounts)
        # 3 Der Papierkorb: files/ und info/ als echte Ordner auf demselben Dateisystem
        os.makedirs(real_trash, mode=0o700, exist_ok=True)
        trash_fd = os.open(real_trash, _DIR_FLAGS)
        fds.append(trash_fd)
        trash_st = os.fstat(trash_fd)
        files_fd, files_st = _open_dir_in(trash_fd, "files", trash_st)
        fds.append(files_fd)
        info_fd, _info_st = _open_dir_in(trash_fd, "info", trash_st)
        fds.append(info_fd)
        if files_st.st_dev != item_st.st_dev:
            raise Refuse("other-filesystem", "Der Papierkorb liegt auf einem anderen Laufwerk als „%s“. "
                                             "Es wird nichts kopiert; der Ordner bleibt, wo er ist." % name)
        # 4 Freier Name (nie überschreiben), erst die Beschreibung, dann das Verschieben
        target, n = name, 2
        while _exists(files_fd, target) or _exists(info_fd, target + XDG_INFO_SUFFIX):
            target = "%s.%d" % (name, n)
            n += 1
        info = "[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
            urllib.parse.quote(os.fsencode(real_parent + "/" + name)),
            time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now())))
        _write_new_file(info_fd, target + XDG_INFO_SUFFIX, info.encode("utf-8"))
        try:
            rename_noreplace(name, target, src_dir_fd=parent_fd, dst_dir_fd=files_fd)
        except OSError:
            _unlink_file(info_fd, target + XDG_INFO_SUFFIX)
            raise
        moved = os.stat(target, dir_fd=files_fd, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != (item_st.st_dev, item_st.st_ino):
            # zwischen Prüfung und Verschieben ausgetauscht: zurück an seinen Platz
            rename_noreplace(target, name, src_dir_fd=files_fd, dst_dir_fd=parent_fd)
            _unlink_file(info_fd, target + XDG_INFO_SUFFIX)
            raise Refuse("changed", "„%s“ hat sich während der Prüfung geändert." % name)
        return {"ok": True, "path": real_trash + "/files/" + target,
                "info": real_trash + "/info/" + target + XDG_INFO_SUFFIX, "message": ""}
    finally:
        for fd in reversed(fds):
            os.close(fd)
        os.close(parent_fd)


# ====================================================================== Zwischenordner des Entpackers

def list_staging(dest_root: str) -> list:
    """Zwischenordner (.couch-staging-<zufall>) direkt im Spiele-Ordner. Nur lesend.

    Rückgabe: [{"path", "token"}]. Ob einer entfernt werden darf, entscheidet erst remove_staging()."""
    try:
        real_root, root_fd, _root_st = _open_root(dest_root)
    except Refuse:
        return []
    try:
        found = []
        for name in sorted(os.listdir(root_fd)):
            match = STAGING_NAME_RE.match(name)
            if match:
                found.append({"path": real_root + "/" + name, "token": match.group(1)})
        return found
    finally:
        os.close(root_fd)


def remove_staging(staging: str, dest_root: str, token: str, *, mounts: Mounts | None = None) -> None:
    """Entfernt einen Zwischenordner des Entpackers samt Inhalt. Blockierend.

    Wirft Refuse (bzw. OSError), wenn der Ordner nicht zweifelsfrei unser eigener ist; dann bleibt
    alles liegen. Der Aufrufer meldet das, er versucht nichts anderes."""
    # 1 Zufallswert und Name gehören zusammen
    if not _is_token(token):
        raise Refuse("bad-token", "Der Zufallswert des Zwischenordners ist ungültig.")
    parent, name = _split_path(staging)
    if name != STAGING_PREFIX + token:
        raise Refuse("name-mismatch", "Der Name des Zwischenordners passt nicht zu seinem Zufallswert.")
    # 2 Er liegt direkt im Spiele-Ordner
    real_root, root_fd, root_st = _open_root(dest_root)
    try:
        if os.path.realpath(parent) != real_root:
            raise Refuse("not-direct-child", "Der Zwischenordner liegt nicht direkt im Spiele-Ordner.")
        # 3 Echter eigener Ordner, kein Symlink, gleiches Gerät, gleiche Einhängung
        item_fd, item_st = _open_child_dir(root_fd, name, root_st)
        try:
            # 4 Markerdatei darin nennt denselben Zufallswert
            try:
                content = _read_file(item_fd, STAGING_MARKER, 64).decode("ascii", "replace")
            except OSError:
                raise Refuse("marker-missing", "Im Zwischenordner fehlt die Markerdatei. Er bleibt liegen.")
            if content != token:
                raise Refuse("marker-mismatch", "Die Markerdatei des Zwischenordners nennt einen anderen Zufallswert. Er bleibt liegen.")
            # 5 Keine Einhängung im oder unter dem Ordner
            _refuse_mounts_below(real_root + "/" + name, mounts)
            # 6 Inhalt entfernen (Marker zuletzt), dann den leeren Ordner
            _remove_tree(item_fd, item_st.st_dev, _mount_id(item_fd), keep=STAGING_MARKER)
            _unlink_file(item_fd, STAGING_MARKER)
        finally:
            os.close(item_fd)
        os.rmdir(name, dir_fd=root_fd)
    finally:
        os.close(root_fd)


def remove_own_dir(path: str, *, mounts: Mounts | None = None) -> None:
    """Entfernt einen eigenen Ordner samt Inhalt mit denselben Grenzen wie purge(): echter Ordner
    des eigenen Nutzers (kein Symlink), gleiches Gerät und gleiche Einhängung wie sein
    Elternordner, keine Einhängung darin (auch nicht bei unlesbarer Liste), Symlinks nur als
    Eintrag. Für Zwischenordner anderer Teile (Prefix-Kopie), die ihre eigenen Prüfungen schon
    gemacht haben; so bleibt es bei dieser einen Löschstelle im Produkt. Blockierend.

    Wirft Refuse bzw. OSError; was bis dahin nicht entfernt ist, bleibt liegen."""
    parent, name = _split_path(path)
    if name.startswith("."):
        raise Refuse("hidden", "Versteckte Ordner werden nicht angefasst.")
    try:
        parent_fd = os.open(parent, _DIR_FLAGS)
    except OSError:
        raise Refuse("missing", "Der Elternordner ist nicht vorhanden oder kein echter Ordner.")
    try:
        parent_st = os.fstat(parent_fd)
        item_fd, item_st = _open_child_dir(parent_fd, name, parent_st)
        try:
            _refuse_mounts_below(os.path.realpath(parent) + "/" + name, mounts)
            # wie jeder Unterordner in _remove_tree: Ein schreibgeschützter Ordner (0555, etwa von
            # einer DVD kopiert) ließe sich sonst nicht leeren (E99)
            os.fchmod(item_fd, 0o700)
            _remove_tree(item_fd, item_st.st_dev, _mount_id(item_fd))
        finally:
            os.close(item_fd)
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def remove_escaping_links(payload: str, within: str | None = None) -> list:
    """Entfernt im frisch entpackten Inhalt eines Zwischenordners alle Symlinks, die absolut sind
    oder aus dem Inhalt hinauszeigen. Entfernt wird nur der Verweis, nie sein Ziel. Blockierend.

    payload muss <Spiele-Ordner>/.couch-staging-<zufall>/<unterordner> sein; alles andere wird verweigert.
    within: der Ordner unterhalb von payload, der wirklich an den Zielort wandert (der Spielordner).
    Gemessen wird dann gegen ihn: Ein Verweis „../Nachbar“ bliebe sonst im Zwischenordner gültig
    und zeigte nach dem Verschieben auf das Nachbarspiel im Spiele-Ordner.
    Rückgabe: Liste der entfernten Verweise (Pfade relativ zu payload bzw. within)."""
    base = os.path.realpath(payload)
    if not STAGING_NAME_RE.match(os.path.basename(os.path.dirname(base))) or not os.path.isdir(base):
        raise Refuse("not-staging", "Verweise werden nur in einem Zwischenordner des Entpackers bereinigt.")
    root = base
    if within is not None:
        root = os.path.realpath(within)
        if not (root == base or root.startswith(base + os.sep)) or not os.path.isdir(root) or os.path.islink(within):
            raise Refuse("not-staging", "Verweise werden nur in einem Zwischenordner des Entpackers bereinigt.")
    removed = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            if os.path.isabs(os.readlink(path)) or not (target == root or target.startswith(root + os.sep)):
                os.unlink(path)         # entfernt den Verweis selbst
                removed.append(os.path.relpath(path, root))
    return removed
