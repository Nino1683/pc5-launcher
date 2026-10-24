"""Prozessbaum eines reaper über /proc: Nachkommen finden und ihnen Signale schicken.

Warum nicht cgroups (E5, Befund 2): Flatpak-Programme verlassen die cgroup des Starters,
bleiben aber Nachkommen des reaper. Warum nicht über Namen: SAFETY 13, es darf nur treffen,
was nachweislich aus dem eigenen reaper-Baum stammt.

Schutz gegen wiederverwendete PIDs: Jede PID gilt nur zusammen mit ihrer Startzeit
(/proc/<pid>/stat, Feld 22). Vor jedem Signal wird die PID als pidfd festgehalten, dann
Startzeit und Elternkette noch einmal gelesen; das Signal geht über den pidfd.
Nur Standardbibliothek: Der Supervisor darf dieses Modul mitbenutzen.
"""
from __future__ import annotations

import os
import signal
from typing import NamedTuple

REAPER_COMM = "reaper"
_MAX_DEPTH = 256          # Elternkette: Schutz gegen Schleifen bei Wettläufen


class ProcInfo(NamedTuple):
    pid: int
    ppid: int
    state: str            # R S D T (angehalten) t Z (Zombie) ...
    starttime: int        # Ticks seit dem Booten; zusammen mit der PID eindeutig
    comm: str


def read_stat(pid: int) -> ProcInfo | None:
    """Eine Zeile aus /proc/<pid>/stat. None, wenn es den Prozess nicht (mehr) gibt."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    try:
        # comm darf Leerzeichen und Klammern enthalten: von der ersten "(" bis zur letzten ")"
        left, right = raw.index(b"("), raw.rindex(b")")
        rest = raw[right + 2:].split()
        return ProcInfo(pid, int(rest[1]), rest[0].decode("ascii", "replace"), int(rest[19]),
                        raw[left + 1:right].decode("utf-8", "replace"))
    except (ValueError, IndexError):
        return None


def starttime(pid: int) -> int | None:
    info = read_stat(pid)
    return info.starttime if info else None


def alive(pid: int, start: int | None = None) -> bool:
    """Lebt genau dieser Prozess noch (kein Zombie, gleiche Startzeit)?"""
    info = read_stat(pid)
    if info is None or info.state in ("Z", "X"):
        return False
    return start is None or info.starttime == start


def cmdline(pid: int) -> list[str]:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "surrogateescape") for p in parts]


def snapshot() -> dict[int, ProcInfo]:
    """Alle Prozesse des Rechners in einem Durchgang. Wer dabei verschwindet, fehlt einfach."""
    out: dict[int, ProcInfo] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for name in names:
        if name.isdigit():
            info = read_stat(int(name))
            if info is not None:
                out[info.pid] = info
    return out


def descendants(root_pid: int, snap: dict[int, ProcInfo] | None = None) -> list[int]:
    """Alle Nachkommen von root_pid (ohne root_pid), Eltern vor Kindern."""
    snap = snapshot() if snap is None else snap
    kids: dict[int, list[int]] = {}
    for info in snap.values():
        kids.setdefault(info.ppid, []).append(info.pid)
    out: list[int] = []
    seen = {root_pid}
    todo = [root_pid]
    while todo:
        for kid in sorted(kids.get(todo.pop(0), ())):
            if kid not in seen:
                seen.add(kid)
                out.append(kid)
                todo.append(kid)
    return out


def children(pid: int, snap: dict[int, ProcInfo] | None = None) -> list[ProcInfo]:
    """Direkte Kinder, das älteste zuerst."""
    snap = snapshot() if snap is None else snap
    return sorted((i for i in snap.values() if i.ppid == pid), key=lambda i: (i.starttime, i.pid))


def has_ancestor(pid: int, ancestor: int) -> bool:
    """Läuft die Elternkette von pid jetzt gerade über ancestor? (frisch gelesen)"""
    cur = pid
    for _ in range(_MAX_DEPTH):
        info = read_stat(cur)
        if info is None:
            return False
        if info.ppid == ancestor:
            return True
        if info.ppid <= 1:
            return False
        cur = info.ppid
    return False


def send_checked(pid: int, start: int, root_pid: int, sig: int) -> bool:
    """sig an genau diesen Prozess (PID mit Startzeit), solange er noch Nachkomme von root_pid ist.
    Auch für couch-flatpak-stop (E99): SIGTERM an die Programme in einer eigenen Sandbox."""
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        return False
    try:
        # Erst festhalten, dann prüfen: Passt die Startzeit jetzt noch, zeigt der pidfd auf
        # genau diesen Prozess, auch wenn er gleich danach endet und die PID neu vergeben wird.
        info = read_stat(pid)
        if info is None or info.starttime != start or info.state in ("Z", "X"):
            return False
        if not has_ancestor(pid, root_pid):
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _send_root(root_pid: int, start: int | None, sig: int) -> bool:
    try:
        fd = os.pidfd_open(root_pid)
    except OSError:
        return False
    try:
        info = read_stat(root_pid)
        if info is None or info.state in ("Z", "X") or (start is not None and info.starttime != start):
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def signal_tree(root_pid: int, sig: int, include_root: bool = False,
                root_starttime: int | None = None, max_rounds: int = 20) -> set[int]:
    """Schickt sig an alle Nachkommen von root_pid und wiederholt, bis keine neuen mehr
    auftauchen (ein Prozess kann zwischen Lesen und Signal noch ein Kind starten).

    root_starttime: Stimmt die Startzeit von root_pid nicht, ist die PID neu vergeben
    worden; dann passiert nichts. include_root schickt das Signal zum Schluss auch an
    root_pid selbst (vorher wären die Nachkommen umgehängt und nicht mehr nachweisbar).
    Liefert die PIDs, die das Signal bekommen haben.
    """
    done: dict[int, int] = {}                 # pid -> Startzeit, schon versucht
    sent: set[int] = set()
    if include_root and sig == signal.SIGKILL:
        # Erst anhalten: Ein reaper, der gerade startet, würde sonst zwischen dem Lesen
        # des Baums und seinem eigenen Ende noch sein Kind abspalten, und das bliebe übrig.
        _send_root(root_pid, root_starttime, signal.SIGSTOP)
    for _ in range(max_rounds):
        snap = snapshot()
        root = snap.get(root_pid)
        if root is None or (root_starttime is not None and root.starttime != root_starttime):
            return sent
        fresh = [p for p in descendants(root_pid, snap)
                 if done.get(p) != snap[p].starttime and snap[p].state not in ("Z", "X")]
        if not fresh:
            break
        for pid in fresh:
            start = snap[pid].starttime
            done[pid] = start                 # auch bei Fehlschlag nicht endlos wiederholen
            if send_checked(pid, start, root_pid, sig):
                sent.add(pid)
    if include_root and _send_root(root_pid, root_starttime, sig):
        sent.add(root_pid)
    return sent


def states(root_pid: int) -> dict[int, str]:
    """Zustand je Nachkomme ("T" = angehalten). Für Tests und zur Kontrolle nach dem Anhalten."""
    snap = snapshot()
    return {p: snap[p].state for p in descendants(root_pid, snap)}


def reaper_app_id(pid: int) -> int | None:
    """Ist pid ein reaper (`reaper SteamLaunch AppId=<N> -- …`)? Dann N, sonst None.

    Dieselbe Regel wie in Gamescope (src/Utils/Process.cpp): Prozessname genau "reaper",
    vor "--" erst "SteamLaunch", danach "AppId=<N>".
    """
    info = read_stat(pid)
    if info is None or info.comm != REAPER_COMM:
        return None
    launch = False
    for arg in cmdline(pid):
        if arg == "--":
            break
        if arg == "SteamLaunch":
            launch = True
        elif launch and arg.startswith("AppId="):
            try:
                return int(arg[6:], 10)
            except ValueError:
                return None
    return None


def find_reaper(pid: int) -> tuple[int, int] | None:
    """Der nächste reaper in der Elternkette von pid: (reaper_pid, app_id) oder None."""
    cur = pid
    for _ in range(_MAX_DEPTH):
        info = read_stat(cur)
        if info is None:
            return None
        app_id = reaper_app_id(cur) if info.comm == REAPER_COMM else None
        if app_id is not None and cur != pid:
            return cur, app_id
        if info.ppid <= 1:
            return None
        cur = info.ppid
    return None
