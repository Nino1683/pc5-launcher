"""Läuft gerade eine Flatpak-Übertragung (Download, Update, Installation)? E141, nur lesend.

Wunsch 2026-10-30: „Wenn man ihn schließt soll er beendet werden nur wenn ein download und update läuft soll gefragt
werden“. Bazaar (App Store, E140) hat dafür keine Schnittstelle (nur GApplication-Aktionen auf dem Sitzungsbus). Aber
libflatpak bzw. OSTree hält während jeder Übertragung eine Sperre auf <installation>/repo/.lock (im Probelauf am
2026-10-30: `OFDLCK ADVISORY READ -1 00:39:<inode> 0 EOF` in /proc/locks für die ganze Dauer, vorher und nachher
nichts; im Leerlauf, auch mit offenem Bazaar, hält niemand eine). Geprüft wird deshalb:

  Sperre    eine Zeile in /proc/locks, deren Gerät:Inode zu repo/.lock einer Installation gehört (dazu
            <cache>/flatpak/system-cache/repo/.lock, falls vorhanden: dorthin zieht libflatpak für System-Installationen
            vor, wenn der Nutzer nicht root ist); auch wartende Sperren („->“) zählen
  Helfer    ein Prozess revokefs-fuse (System-Installation über den flatpak-system-helper; nur /proc/<pid>/comm gelesen)

Gerät: /proc/locks nennt das Gerät des Dateisystems (super_block->s_dev, hexadezimal major:minor). Auf btrfs-
Subvolumes (etwa /var und /var/home) meldet stat() aber das Gerät des Subvolumes (gesehen 2026-10-30: stat 00:3b,
/proc/locks 00:23 für dieselbe Datei). Gezählt wird deshalb beides: st_dev und das Gerät der Einhängung, in der die
Datei liegt (Feld 3 aus /proc/self/mountinfo, dezimal). Der Inode muss immer stimmen.

Auch Bazaars Abgleich der Metadaten (appstream) hält die Sperre kurz; dann fragt es eben einmal unnötig.

Prüfmodus: nie das echte /proc. Sperren nur aus COUCH_TEST_PROC_LOCKS (Datei), Prozesse nur aus COUCH_TEST_PROC
(Ordner mit <pid>/comm), Einhängungen nur aus COUCH_TEST_MOUNTINFO; fehlt eine Angabe, entfällt diese Prüfung.
"""
from __future__ import annotations

import os
import re
from typing import Iterable

from . import mounts as _mounts, paths

LOCKS = "/proc/locks"
PROC = "/proc"
MOUNTINFO = _mounts.MOUNTINFO
HELPER = "revokefs-fuse"
_DEV_INODE = re.compile(r"^([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)$")


def probe() -> dict:
    """Woher gelesen wird: im Betrieb /proc, im Prüfmodus nur, was die Umgebung nennt (sonst None)."""
    if paths.test_mode():
        return {"locks": os.environ.get("COUCH_TEST_PROC_LOCKS") or None,
                "proc": os.environ.get("COUCH_TEST_PROC") or None,
                "mountinfo": os.environ.get(_mounts.TEST_ENV) or None}
    return {"locks": LOCKS, "proc": PROC, "mountinfo": MOUNTINFO}


def lock_files(roots: Iterable[str], cache_home: str | None) -> list[str]:
    """repo/.lock je Installation, dazu der Zwischenspeicher für System-Installationen (nur, wenn es ihn gibt)."""
    out = [os.path.join(r, "repo", ".lock") for r in roots if r]
    if cache_home:
        out.append(os.path.join(cache_home, "flatpak", "system-cache", "repo", ".lock"))
    return out


def cache_home(home: str) -> str:
    """Wo libflatpak den Zwischenspeicher sucht (g_get_user_cache_dir). Im Prüfmodus nur unter dem Attrappen-Home."""
    if paths.test_mode():
        return os.path.join(home, ".cache")
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache")


def parse_locks(text: str) -> set[tuple[int, int, int]]:
    """(major, minor, inode) jeder Zeile von /proc/locks (Gerät hexadezimal)."""
    out = set()
    for line in (text or "").splitlines():
        for field in line.split():
            m = _DEV_INODE.match(field)
            if m:
                out.add((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3))))
                break
    return out


def parse_mounts(text: str) -> list[tuple[str, int, int]]:
    """(Einhängepunkt, major, minor) aus /proc/self/mountinfo, in der Reihenfolge der Datei (couch.mounts).
    Zeilen ohne lesbares Gerät fehlen."""
    return [(m.point, m.device[0], m.device[1]) for m in _mounts.parse(text or "") if m.device is not None]


def mount_device(path: str, mounts: list[tuple[str, int, int]]) -> tuple[int, int] | None:
    """Gerät der Einhängung, in der path liegt (längster passender Einhängepunkt, bei Gleichstand der spätere)."""
    return _mounts.longest(os.path.realpath(path), ((point, (major, minor)) for point, major, minor in mounts))


def held(files: Iterable[str], locks: set, mounts: list) -> list[str]:
    """Die Dateien aus files, auf denen gerade eine Sperre liegt."""
    out = []
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue                            # gibt es (noch) nicht: dann sperrt dort auch niemand
        devices = {(os.major(st.st_dev), os.minor(st.st_dev))}
        dev = mount_device(path, mounts)
        if dev is not None:
            devices.add(dev)
        if any((major, minor, st.st_ino) in locks for major, minor in devices):
            out.append(path)
    return out


def helper_running(proc: str) -> bool:
    """Läuft revokefs-fuse? Liest nur /proc/<pid>/comm (Name bis 15 Zeichen)."""
    try:
        names = [n for n in os.listdir(proc) if n.isdigit()]
    except OSError:
        return False
    for pid in names:
        try:
            with open(os.path.join(proc, pid, "comm"), encoding="utf-8", errors="replace") as fh:
                if fh.read(64).strip() == HELPER:
                    return True
        except OSError:
            continue                            # inzwischen beendet oder nicht lesbar
    return False


def _read(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(4 * 1024 * 1024)
    except OSError:
        return ""


def transfer_running(roots: Iterable[str], home: str, where: dict | None = None) -> bool:
    """True, solange eine Flatpak-Übertragung läuft (Sperre auf einem repo/.lock oder revokefs-fuse). Wirft nicht."""
    where = probe() if where is None else where
    if where.get("locks"):
        locks = parse_locks(_read(where["locks"]))
        if locks:
            mounts = parse_mounts(_read(where.get("mountinfo")))
            if held(lock_files(list(roots), cache_home(home)), locks, mounts):
                return True
    return bool(where.get("proc")) and helper_running(where["proc"])
