"""Einhängungen aus /proc/self/mountinfo: der eine Leser für alle (Datei-Explorer, Speicher, FSR, Sicherung,
Dateiauswahl des Installers, flatpaklock). Nur lesend, blockiert kurz (eine Datei unter /proc).

Vorher gab es sechs fast gleiche Leser, die sich in Kleinigkeiten unterschieden: Einer entmaskierte nur „\\040“, einer
nahm bei zwei Einhängungen am selben Ort die verdeckte statt der obenauf, einer las nicht lesbare Namen anders als
os.path.realpath sie liefert. Hier gilt überall dasselbe:

  - Feld 5 (Einhängepunkt), Feld 4 (Wurzel) und die Felder nach „ - “ stehen bei Leerzeichen, Tab, Zeilenumbruch und
    Backslash als Oktalfolge da (\\040 \\011 \\012 \\134, allgemein \\ooo). Entmaskiert wird auf Bytes und dann wie
    jeder Pfadname dekodiert (os.fsdecode, surrogateescape): So gleicht der Name dem, was os.path.realpath liefert,
    auch bei Bytes, die kein UTF-8 sind.
  - Für einen Pfad zählt der längste passende Einhängepunkt; bei gleich langen (zwei Einhängungen am selben Ort)
    die spätere Zeile, denn die liegt obenauf.
  - Zeilen ohne Trenner „ - “, ohne Dateisystemtyp oder mit weniger als fünf Feldern davor zählen nicht.

Prüfmodus: Nennt der Aufrufer keine Datei, gilt im Prüfmodus COUCH_TEST_MOUNTINFO, falls gesetzt (dieselbe Variable,
die flatpaklock nimmt), sonst wie im Betrieb /proc/self/mountinfo. Die Haken der Aufrufer (ihr Argument mountinfo,
flatpaklock.probe) bleiben, wie sie sind; eine ausdrücklich genannte Datei geht immer vor.

Kein Importkreis: nur Standardbibliothek und couch.paths (das selbst nichts aus couch lädt). installer/browse und
flatpaklock laden das hier; es darf nie files, installer oder settings laden.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, NamedTuple, TypeVar

from . import paths

MOUNTINFO = "/proc/self/mountinfo"
TEST_ENV = "COUCH_TEST_MOUNTINFO"
_OCTAL = re.compile(rb"\\([0-3][0-7]{2})")      # \ooo bis \377: genau ein Byte

V = TypeVar("V")


class Mount(NamedTuple):
    """Eine Zeile aus mountinfo. Texte entmaskiert, fstype wie in der Datei (Groß/klein unverändert)."""
    point: str                              # Feld 5: Einhängepunkt
    fstype: str                             # erstes Feld nach „ - “
    device: tuple[int, int] | None          # Feld 3 major:minor (dezimal); None, wenn nicht lesbar
    root: str = ""                          # Feld 4: Wurzel im Dateisystem (btrfs-Subvolume, Bind-Mount)
    source: str = ""                        # Feld nach dem Typ (/dev/…, „portal“, „nas:/export“)
    options: str = ""                       # Feld 6: Optionen der Einhängung (rw,nosuid,…)
    super_options: str = ""                 # letztes Feld: Optionen des Dateisystems


def unescape(field: bytes | str) -> str:
    """Oktalfolgen (\\040, \\011, \\012, \\134, allgemein \\ooo) zurück in Zeichen; Ergebnis wie ein Pfadname."""
    raw = os.fsencode(field) if isinstance(field, str) else field
    return os.fsdecode(_OCTAL.sub(lambda m: bytes((int(m.group(1), 8),)), raw))


def _device(field: bytes) -> tuple[int, int] | None:
    major, sep, minor = field.partition(b":")
    if not sep or not major.isdigit() or not minor.isdigit():
        return None
    return int(major), int(minor)


def parse(data: bytes | str) -> list[Mount]:
    """Alle gültigen Zeilen, in der Reihenfolge der Datei. Nimmt Bytes oder Text (Text wie ein Pfadname kodiert)."""
    raw = os.fsencode(data) if isinstance(data, str) else (data or b"")
    out: list[Mount] = []
    for line in raw.split(b"\n"):
        left, sep, right = line.partition(b" - ")
        fields, tail = left.split(b" "), right.split(b" ")
        if not sep or len(fields) < 5 or not tail[0]:
            continue
        out.append(Mount(point=unescape(fields[4]), fstype=unescape(tail[0]), device=_device(fields[2]),
                         root=unescape(fields[3]), source=unescape(tail[1]) if len(tail) > 1 else "",
                         options=os.fsdecode(fields[5]) if len(fields) > 5 else "",
                         super_options=os.fsdecode(tail[2]) if len(tail) > 2 else ""))
    return out


def source() -> str:
    """Welche Datei gelesen wird, wenn der Aufrufer keine nennt (siehe Modulkopf)."""
    if paths.test_mode():
        return os.environ.get(TEST_ENV) or MOUNTINFO
    return MOUNTINFO


def read(mountinfo: str | None = None) -> list[Mount]:
    """Liest und zerlegt mountinfo (None: source()). Wirft OSError, wenn die Datei nicht lesbar ist: Was dann gilt,
    entscheidet der Aufrufer (unbekannt, „im Zweifel langsam“, leere Tabelle)."""
    with open(mountinfo or source(), "rb") as fh:
        return parse(fh.read())


def contains(point: str, real: str) -> bool:
    """Liegt der (aufgelöste) Pfad real unter dem Einhängepunkt point oder ist er es selbst?"""
    return real == point or real.startswith(point.rstrip("/") + "/")


def longest(real: str, pairs: Iterable[tuple[str, V]]) -> V | None:
    """Wert zum längsten Einhängepunkt, unter dem real liegt; bei gleich langen der spätere. None ohne Treffer.
    pairs: (einhängepunkt, wert) in der Reihenfolge der Datei."""
    best: V | None = None
    best_len = -1
    for point, value in pairs:
        if contains(point, real) and len(point) >= best_len:
            best, best_len = value, len(point)
    return best


def find(real: str, mounts: Iterable[Mount]) -> Mount | None:
    """Die Einhängung, in der der schon aufgelöste Pfad real liegt (ohne realpath: kein Zugriff auf die Platte)."""
    return longest(real, ((m.point, m) for m in mounts))


def mount_of(path: str, mountinfo: str | None = None) -> Mount | None:
    """Die Einhängung, in der path liegt (realpath aufgelöst). Wirft OSError wie read()."""
    real = os.path.realpath(path)
    return find(real, read(mountinfo))


def fstype(path: str, mountinfo: str | None = None) -> str:
    """Dateisystemtyp, auf dem path liegt, wie in mountinfo geschrieben; "" wenn unbekannt oder nicht lesbar."""
    try:
        mount = mount_of(path, mountinfo)
    except OSError:
        return ""
    return mount.fstype if mount else ""


def table(mounts: Iterable[Mount]) -> dict[str, str]:
    """Einhängepunkt -> Dateisystemtyp. Spätere Zeilen überdecken frühere (wie der Kern: obenauf gilt)."""
    return {m.point: m.fstype for m in mounts}
