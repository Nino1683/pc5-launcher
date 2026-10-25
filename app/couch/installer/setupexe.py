"""Windows-Installationsprogramme aus eigenen Dateien (Bauplan E114): erkennen, benennen, Platz
schätzen, stille Schalter und Rückgabewerte. Nur lesend.

Ausgeführt wird nichts hier: Den stillen Lauf über Proton macht jobs.py, Befehlsanfang und Umgebung
liefert die Verdrahtung (wiring_install, wie bei GOG und itch.io im Prefix des späteren Eintrags).
Erkannt wird wie beim Store (E100, store/itch_install.installer_type): Inno Setup, NSIS und MSI.
Unbestätigt: die stillen Schalter von NSIS und MSI (nur nach Doku, wie bei itch.io).

Ein Blick in den Dateikopf liest bis zu 8 MB (dort stehen die Kennungen von Inno Setup und NSIS).
Ordnerlisten (Explorer, Dateiauswahl) sehen deshalb nur in wenige Dateien je Liste hinein
(DEEP_LIMIT) und merken sich das Ergebnis je Datei (Pfad, Größe, Änderungszeit).
"""
from __future__ import annotations

import os
import re
import stat
import threading

from . import InstallError

# Geschätzte Größe nach der Installation = Installer samt .bin-Dateien mal INSTALL_FACTOR. Inno Setup,
# NSIS und MSI packen mit LZMA bzw. CAB; Spieldaten (Texturen, Ton, Video) sind meist schon gepackt und
# wachsen beim Installieren nur wenig. Die Store-Warteschlange rechnet für GOG mit 2,2 × Download für
# Download plus Installation, also rund 1,2 × für die Installation selbst (wiring_store, E99). 1,6 lässt
# Luft für stärker gepackte ältere Spiele; die Reserve der Spiele-Platte (install.reserve_gib) kommt dazu,
# und während des Laufs wacht jobs.py wie beim Entpacken über den freien Platz.
INSTALL_FACTOR = 1.6
DEEP_LIMIT = 8                      # so viele .exe je Ordnerliste bekommen den Blick in den Dateikopf (je bis 8 MB)
MIN_DEEP_BYTES = 64 * 1024          # kleinere .exe sind nie ein Installer (spart den Blick)
CACHE_MAX = 512
TYPE_LABELS = {"inno": "Inno Setup", "nsis": "NSIS", "msi": "Windows Installer"}
# Rückgabewerte, die „fertig“ heißen. MSI: 3010 und 1641 = fertig, Neustart empfohlen (unter Wine bedeutungslos)
OK_CODES = {"inno": (0,), "nsis": (0,), "msi": (0, 1641, 3010)}

MSG_UNKNOWN = ("Dieses Installationsprogramm kennt der PC5 Launcher nicht. Still installieren lassen sich nur "
               "Inno Setup, NSIS und MSI.")
MSG_NOT_SETUP = ("Diese Datei ist kein Installationsprogramm. Ist sie das Spiel selbst, starte sie im "
                 "Datei-Explorer oder unter Downloads mit „Starten“.")

_SETUP_SUFFIX = re.compile(r"(?i)setup\.exe$")          # „SpielSetup.exe“: kein Trenner vor setup
_VERSION_CUT = re.compile(r"(?i)[ _-]+v?\d+(\.\d+)+.*$")  # ab der ersten Versionsnummer mit Punkt
# einzelne Wörter, an denen der Titel endet: v12, build123, ein Prüfwert wie „bd1679ce“ (GOG)
_VERSION_WORD = re.compile(r"(?i)^(v\d+[a-z]?|build\d*|(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,})$")
_GENERIC_PARENT = {"downloads", "download", "dokumente", "documents", "desktop", "schreibtisch", "home"}

_cache: dict = {}                   # (Pfad, Größe, Änderungszeit) -> Art ("" = kein bekannter Installer)
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- Erkennen
def is_setup_name(name: str) -> bool:
    """Sagt der Dateiname „Installationsprogramm“? .msi immer; .exe wie setup_*.exe, *-setup.exe,
    *installer.exe (probe.INSTALLER_NAME) oder *Setup.exe. Nur der Name, kein Plattenzugriff."""
    from . import probe
    low = os.path.basename(str(name or "")).lower()
    if low.endswith(".msi"):
        return True
    return low.endswith(".exe") and bool(probe.INSTALLER_NAME.match(low) or _SETUP_SUFFIX.search(low))


def _regular(path: str):
    """os.stat einer gewöhnlichen Datei oder None (nie eine FIFO öffnen: das Lesen hinge)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def installer_type(path: str) -> str:
    """inno | nsis | msi | "" (kein bekannter Installer). Liest bis zu 8 MB; gemerkt je Datei."""
    st = _regular(path)
    if st is None:
        return ""
    key = (os.path.realpath(path), st.st_size, st.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    from ..store.itch_install import installer_type as detect     # dieselbe Erkennung wie der Store (E100)
    kind = detect(path)
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = kind
    return kind


def known_type(path: str) -> str | None:
    """Art aus dem Gedächtnis, ohne zu lesen; None = noch nie angesehen."""
    st = _regular(path)
    if st is None:
        return None
    with _cache_lock:
        return _cache.get((os.path.realpath(path), st.st_size, st.st_mtime_ns))


class Budget:
    """Wie viele Dateien einer Ordnerliste noch den Blick in den Dateikopf bekommen."""

    def __init__(self, n: int = DEEP_LIMIT):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def listing_flag(path: str, name: str, size: int, budget: Budget | None = None) -> bool:
    """Für Ordnerlisten: ist diese Datei ein Installationsprogramm, das „Installieren“ anbieten soll?
    .msi immer; eine .exe, wenn der Dateikopf Inno Setup oder NSIS sagt. Ist das Budget verbraucht (oder
    die Platte langsam: budget None), entscheidet ein früherer Blick, sonst der Name."""
    low = str(name or "").lower()
    if low.endswith(".msi"):
        return True
    if not low.endswith(".exe"):
        return False
    seen = known_type(path)
    if seen is not None:
        return bool(seen)
    if budget is not None and isinstance(size, int) and size >= MIN_DEEP_BYTES and budget.take():
        return bool(installer_type(path))
    return is_setup_name(name)


def succeeded(kind: str, rc) -> bool:
    return isinstance(rc, int) and rc in OK_CODES.get(kind, (0,))


def silent_command(kind: str, installer: str, target: str) -> list:
    """Befehl ohne umu-run davor für einen unsichtbaren Lauf nach target (wie itch.io, E100)."""
    from ..store import gog_install, itch_install
    try:
        return itch_install.silent_command(kind, installer, target)
    except gog_install.InstallError as e:
        raise InstallError("unknown-installer", MSG_UNKNOWN) from e


# ---------------------------------------------------------------- Titel und Größe
def companions(path: str) -> list:
    """Daten neben dem Installer: <name>-1.bin, <name>-2.bin … (Inno Setup, GOG-Offline-Installer). Nur lesend."""
    folder, name = os.path.split(os.path.abspath(path))
    stem = os.path.splitext(name)[0]
    rx = re.compile(re.escape(stem) + r"-\d+\.bin$", re.I)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(os.path.join(folder, n) for n in names if rx.match(n) and _regular(os.path.join(folder, n)))


def estimate(path: str) -> dict:
    """{files: [Namen], setup_bytes, installed_bytes} für die Platzprüfung (Annahme: INSTALL_FACTOR)."""
    parts = [os.path.abspath(path)] + companions(path)
    total = 0
    for part in parts:
        st = _regular(part)
        total += st.st_size if st is not None else 0
    return {"files": [os.path.basename(p) for p in parts], "setup_bytes": total,
            "installed_bytes": int(total * INSTALL_FACTOR)}


def title_of(path: str) -> str:
    """Dateiname eines Installers -> Spieltitel: ohne setup_/install vorn und -setup/installer hinten,
    ohne Klammern (GOG-Build), Versionsnummern, Prüfwerte und Unterstriche. GOG schreibt alles klein:
    dann bekommt jedes Wort einen großen Anfangsbuchstaben. setup.exe allein: der Ordner darüber."""
    from . import probe
    stem = os.path.splitext(os.path.basename(str(path or "")))[0]
    t = re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", " ", stem).strip(" _.-")
    t = re.sub(r"(?i)^(setup|install(er)?)(?=[ _.-]|$)[ _.-]*", "", t)
    t = re.sub(r"(?i)[ _.-]*(setup|installer|install)$", "", t).strip(" _.-")
    t = _VERSION_CUT.sub("", t)
    words = []
    for word in [w for w in re.split(r"[ _]+", t) if w]:
        if words and _VERSION_WORD.match(word):
            break
        words.append(word)
    t = probe.clean_name(" ".join(words)) if words else ""
    t = re.sub(r"\s+", " ", t).strip(" -._")
    if t and t == t.lower():
        t = " ".join(w[:1].upper() + w[1:] for w in t.split(" "))
    if t:
        return t
    parent = os.path.basename(os.path.dirname(os.path.abspath(str(path or ""))))
    if parent and probe.norm(parent) not in _GENERIC_PARENT | probe.GENERIC_TOP:
        return probe.clean_name(parent)
    return "Spiel"
