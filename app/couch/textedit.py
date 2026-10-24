"""Text-Editor des Datei-Explorers (Bauplan E99): Textdateien öffnen und sicher speichern.

Wunsch 2026-10-23: „Im Datei-Manager möchte ich auch txt- und ini-Dateien bearbeiten können.“ Die
Seite (screens/text-editor.js) bearbeitet zeilenweise mit dem Pad; hier liegt, was die Platte anfasst:

- Nur Textdateien bis MAX_BYTES mit bekannter Endung (EXTENSIONS), ohne NUL-Bytes, nur reguläre
  Dateien unter den Orten des Explorers (files.roots). Geprüft wird immer der aufgelöste Pfad
  (realpath, /home ist ein Verweis auf /var/home); ein Verweis führt nicht aus den Orten hinaus,
  gelesen und geschrieben wird die Datei, auf die er zeigt.
- Kodierung, BOM, Zeilenenden und der letzte Zeilenumbruch bleiben genau so, wie sie waren:
  UTF-8 (mit oder ohne BOM), UTF-16 LE/BE mit BOM (viele Windows-Spiele schreiben ihre ini so),
  sonst Windows-1252. Unveränderter Text ergibt Byte für Byte dieselbe Datei; Bytes, die es in
  Windows-1252 nicht gibt, kommen über surrogateescape unverändert zurück. Gemischte Zeilenenden
  behalten unveränderte Zeilen ihr eigenes Ende (Abgleich der Zeilen, difflib).
- Nur ansehen (readonly mit Grund): Steam (auch steamapps/ jeder Bibliothek, nur die Spielordner
  unter steamapps/common/<Spiel>/ nicht), Lutris, EmuDeck und Emulatoren, Flatpak-Apps (~/.var/app),
  die eigenen Ordner des PC5 Launchers (SAFETY.md Regel 13: „Steam-, Lutris-, EmuDeck- und
  Emulator-Dateien nur lesen“), dazu die Sitzung des Game-Mode, umu, Sicherungen, schreibgeschützte
  Dateien, Ordner und Laufwerke, Dateien fremder Benutzer oder mit harten Verknüpfungen und Dateien
  mit mehr als MAX_EDIT_LINES Zeilen (die Seite bliebe sonst nicht flüssig).
- Speichern prüft den Stand vom Öffnen (mtime_ns, Größe, SHA-256): Weicht er ab, heißt es „Die
  Datei wurde inzwischen geändert“. Vorher geht der alte Inhalt als Sicherung nach
  ~/Games/backups/couch/bearbeitet/<JJJJMMTT-HHMMSS>-<Name> (im Prüfmodus im Käfig); je Datei bleiben
  die letzten BACKUP_KEEP, ältere kommen in den Papierkorb (nie endgültig gelöscht). Welche Sicherung
  zu welcher Datei gehört, steht im Verzeichnis INDEX_NAME daneben; was dort nicht steht (von Hand
  abgelegt, ältere Fassung), fasst das Aufräumen nie an. Geschrieben wird atomar: temporäre Datei im
  selben Ordner, Rechte der alten Datei, fsync, os.replace. Scheitert etwas, bleibt die alte Datei,
  wie sie war, und die eben angelegte Sicherung kommt wieder weg.

Ergebnisse sind Wörterbücher wie bei files.py: {"ok": True, …} oder {"ok": False, "code", "message"}
mit einem deutschen Satz. mtime_ns geht als Zeichenkette hinaus: JavaScript-Zahlen sind nur auf
53 Bit genau, eine Nanosekunden-Zeit käme verfälscht zurück und jeder Speicherversuch wäre ein Konflikt.

Nur Standardbibliothek; alles blockiert (Arbeits-Thread, wiring._bg). Die Namen open/save am Ende
sind Kurzformen von open_text/save_text; deshalb nutzt dieses Modul selbst nur io.open und os.open.
"""
from __future__ import annotations

import difflib
import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

from . import jsontext, log, paths

_log = log.get("textedit")

MAX_BYTES = 2 * 1024 * 1024
MAX_EDIT_LINES = 5000
BACKUP_KEEP = 10
# Gleich in screens/text-editor.js (EDITABLE): dort entscheidet sie, ob der Explorer „Bearbeiten“ anbietet
EXTENSIONS = frozenset(("txt", "ini", "cfg", "conf", "config", "log", "json", "xml", "yaml", "yml", "toml", "md",
                        "csv", "properties", "nfo", "lua", "rc"))
INDEX_NAME = ".couch-bearbeitet.json"     # Sicherung -> Datei, zu der sie gehört
TMP_PREFIX = ".couch-edit-"               # temporäre Datei neben der Datei, nur während des Schreibens
SPACE_SLACK = 64 * 1024                   # so viel mehr als den neuen Inhalt muss das Laufwerk frei haben

BOM_UTF8 = b"\xef\xbb\xbf"
BOMS = {"utf-8": BOM_UTF8, "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}
# Rückweg der Zeichen, die beim Lesen nicht passten (surrogateescape: Bytes 0x80…0xff, die es in der
# Kodierung nicht gibt; surrogatepass: einzelne Surrogate in UTF-16)
ERRORS = {"utf-8": "surrogateescape", "cp1252": "surrogateescape", "utf-16-le": "surrogatepass", "utf-16-be": "surrogatepass"}
LABELS = {"utf-8": "UTF-8", "cp1252": "Windows-1252", "utf-16-le": "UTF-16 LE", "utf-16-be": "UTF-16 BE"}
EOL_NAMES = {"\r\n": "crlf", "\n": "lf", "\r": "cr"}
_EOL_SPLIT = re.compile(r"(\r\n|\r|\n)")
_EOL_ANY = re.compile(r"\r\n|\r|\n")
# Steuerzeichen, die in Text nicht vorkommen (Tab, Zeilenvorschub, Seitenvorschub, ESC und das alte
# DOS-Dateiende 0x1a schon); viele davon heißen: Das ist eine umbenannte Binärdatei
_CONTROL = re.compile(r"[\x01-\x08\x0e-\x19\x1c-\x1f]")
_SETTING = re.compile(r"^\s*(emulationPath|romsPath|toolsPath|biosPath|savesPath|storagePath)\s*=\s*[\"']?([^\"'\n]*)", re.M)
_SHA = re.compile(r"^[0-9a-f]{64}$")

TEXT_OUTSIDE = "Dieser Ort liegt außerhalb der Konsole."
TEXT_GONE = "Das gibt es nicht mehr."
TEXT_NOT_FILE = "Das ist keine gewöhnliche Datei."
TEXT_TYPE = "Dieser Dateityp lässt sich nicht bearbeiten. Möglich sind Textdateien wie .txt, .ini, .cfg oder .json."
TEXT_TOO_BIG = "Die Datei ist zu groß (%s). Bearbeiten lassen sich Textdateien bis 2 MB."
TEXT_BINARY = "Das ist keine Textdatei: Sie enthält Binärdaten."
TEXT_UTF32 = "Diese Datei ist in UTF-32 gespeichert. Das lässt sich hier nicht bearbeiten."
TEXT_BAD_UTF16 = "Die Datei ist als UTF-16 gekennzeichnet, aber beschädigt."
TEXT_UNREADABLE = "Die Datei lässt sich nicht lesen."
TEXT_NO_READ = "Die Datei lässt sich nicht lesen: Dafür fehlen die Rechte."
TEXT_READ_IO = "Die Datei lässt sich nicht lesen: Lesefehler auf dem Laufwerk."
TEXT_CONFLICT = "Die Datei wurde inzwischen geändert."
TEXT_NO_EXPECT = "Der Stand der Datei vom Öffnen fehlt. Lade sie neu."
TEXT_BAD_TEXT = "Der Text fehlt."
TEXT_NEW_TOO_BIG = "Der Text ist zu groß (höchstens 2 MB). Gespeichert wurde nichts."
TEXT_CHAR = "Das Zeichen „%s“ gibt es in der Kodierung %s nicht. Gespeichert wurde nichts."
TEXT_NO_SPACE = "Auf dem Laufwerk ist kein Platz mehr frei. Die Datei bleibt, wie sie war."
TEXT_ROFS = "Das Laufwerk ist schreibgeschützt. Die Datei bleibt, wie sie war."
TEXT_NO_RIGHTS = "Dafür fehlen die Schreibrechte. Die Datei bleibt, wie sie war."
TEXT_IO = "Schreibfehler auf dem Laufwerk. Die Datei bleibt, wie sie war."
TEXT_FAILED = "Das Speichern hat nicht geklappt. Die Datei bleibt, wie sie war."
TEXT_BACKUP = "Die Sicherung ließ sich nicht anlegen (%s). Gespeichert wurde nichts."
TEXT_SAVED = "Gespeichert."
TEXT_UNCHANGED = "Es gab nichts zu speichern."

RO_STEAM = "Gehört zu Steam. Steam-Dateien lassen sich hier nur ansehen."
RO_LUTRIS = "Gehört zu Lutris. Lutris-Dateien lassen sich hier nur ansehen."
RO_EMUDECK = "Gehört zu EmuDeck. EmuDeck-Dateien lassen sich hier nur ansehen."
RO_EMULATOR = "Gehört zu einem Emulator. Seine Dateien lassen sich hier nur ansehen."
RO_FLATPAK = "Gehört zu einer Flatpak-App (Emulator, Browser …). Ihre Dateien lassen sich hier nur ansehen."
RO_OWN = "Gehört zum PC5 Launcher selbst. Seine Dateien lassen sich hier nur ansehen."
RO_SESSION = "Gehört zur Sitzung des Game-Mode. Diese Dateien lassen sich hier nur ansehen."
RO_UMU = "Gehört zu umu (Proton-Laufzeit). Diese Dateien lassen sich hier nur ansehen."
RO_BACKUP = "Das ist eine Sicherung. Sicherungen bleiben, wie sie sind."
RO_MOUNT = "Das Laufwerk ist schreibgeschützt eingehängt."
RO_FILE = "Die Datei ist schreibgeschützt."
RO_FOLDER = "Der Ordner ist schreibgeschützt. Die Datei lässt sich dort nicht sicher speichern."
RO_OWNER = "Die Datei gehört einem anderen Benutzer."
RO_LINKS = "Die Datei hat mehrere harte Verknüpfungen. Speichern würde sie voneinander trennen."
RO_LINES = "Die Datei hat mehr als %d Zeilen und lässt sich hier nur ansehen." % MAX_EDIT_LINES
RO_TRASH = "Die Datei liegt im Papierkorb. Lege sie zuerst zurück."

# Orte im persönlichen Ordner, nur lesen: (Abschnitte ab ~, klein geschrieben, Grund)
HOME_ZONES = (
    ((".local", "share", "steam"), RO_STEAM),
    ((".steam",), RO_STEAM),
    ((".config", "lutris"), RO_LUTRIS),
    ((".local", "share", "lutris"), RO_LUTRIS),
    ((".cache", "lutris"), RO_LUTRIS),
    ((".var", "app"), RO_FLATPAK),
    ((".config", "emudeck"), RO_EMUDECK),
    (("emudeck",), RO_EMUDECK),
    (("es-de",), RO_EMUDECK),                      # EmulationStation, von EmuDeck eingerichtet
    ((".emulationstation",), RO_EMUDECK),
    ((".config", "couch"), RO_OWN),
    ((".local", "share", "couch"), RO_OWN),
    ((".local", "state", "couch"), RO_OWN),
    ((".cache", "couch"), RO_OWN),
    # Ein Fehler hier ließe den Game-Mode nicht mehr starten, und am Fernseher gäbe es keinen Ausweg
    ((".config", "gamescope-session-plus"), RO_SESSION),
    ((".config", "gamescope"), RO_SESSION),
    ((".local", "share", "umu"), RO_UMU),
    (("games", "umu"), RO_UMU),
    (("games", "backups"), RO_BACKUP),
)
# Ordner von Emulatoren unter ~/.config und ~/.local/share (klein geschrieben). Nicht "dolphin":
# so heißt auch der Dateimanager von KDE (~/.local/share/dolphin), der Emulator heißt dolphin-emu.
EMULATOR_DIRS = frozenset((
    "cemu", "ryujinx", "yuzu", "eden", "suyu", "sudachi", "citron", "citra-emu", "azahar-emu", "lime3ds-emu",
    "pcsx2", "rpcs3", "vita3k", "duckstation", "ppsspp", "retroarch", "dolphin-emu", "primehack", "xemu",
    "xenia", "melonds", "shadps4", "mame", "flycast", "scummvm", "mgba", "redream", "mednafen", "supermodel",
    "steam-rom-manager", "es-de",
))
HOME_EMULATOR_DIRS = frozenset((".supermodel", ".vita3k", ".citra-emu", ".yuzu"))
# Eigene Ordner des Installers auf der Spiele-Platte (installer/trash.py) und Papierkörbe der Laufwerke
OWN_PARTS = (".couch-trash",)
OWN_PREFIXES = (".couch-staging-",)
TRASH_PARTS = re.compile(r"^\.trash(-\d+)?$")
# Steams eigene Werkzeuge unter steamapps/common (keine Spiele)
STEAM_TOOLS = frozenset(("steam controller configs", "steamworks shared", "steamvr"))
_STEAM_TOOL = re.compile(r"^(steamlinuxruntime|proton( |-|$))")


class Refuse(Exception):
    """Ablehnung mit festem Kürzel und deutschem Satz für die Oberfläche."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def result(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message}


# ====================================================================== Name und Ort
def extension(name: str) -> str:
    """Endung ohne Punkt, klein. ".bashrc" (versteckt, ohne Endung) und "datei." haben keine."""
    base = os.path.basename(name or "")
    stem, dot, ext = base.lstrip(".").rpartition(".")
    return ext.lower() if dot and stem else ""


def editable_name(name: str) -> bool:
    return extension(name) in EXTENSIONS


def _explorer_roots() -> list[str]:
    from . import files                        # spät: files zieht mehr nach, als dieses Modul braucht
    return [r["path"] for r in files.roots()]


def _zone_home(home: str | None) -> str:
    """Bezug der Zonen im persönlichen Ordner. Im Prüfmodus <COUCH_TEST_ROOT>/home (wie
    paths.user_place); die echten Ordner des PC5 Launchers gelten zusätzlich immer."""
    if home:
        return os.path.realpath(home)
    root = paths.test_root()
    return os.path.realpath(os.path.join(root, "home") if root else paths.home())


def _parts_below(real: str, base: str) -> list[str] | None:
    if real == base:
        return []
    prefix = base.rstrip("/") + "/"
    return real[len(prefix):].split("/") if real.startswith(prefix) else None


def _under(real: str, base: str) -> bool:
    return _parts_below(real, os.path.realpath(base)) is not None


def _own_dirs() -> list[str]:
    home = paths.home()
    own = [paths.config_dir(), paths.data_dir(), paths.state_dir(), paths.cache_dir(), paths.run_dir(),
           os.path.join(home, ".config", "couch"), os.path.join(home, ".local", "share", "couch"),
           os.path.join(home, ".local", "state", "couch"), os.path.join(home, ".cache", "couch")]
    return [os.path.realpath(p) for p in own]


def _steam_game_file(parts: list[str]) -> bool:
    """Liegt der Pfad (Abschnitte, klein) in einem Spielordner steamapps/common/<Spiel>/…?"""
    if "steamapps" not in parts:
        return False
    rest = parts[parts.index("steamapps") + 1:]
    return len(rest) >= 3 and rest[0] == "common" and rest[1] not in STEAM_TOOLS and not _STEAM_TOOL.match(rest[1])


def _emudeck_paths(home: str) -> list[str]:
    """Ordner, die EmuDeck in seiner settings.sh nennt (emulationPath, romsPath …)."""
    out = []
    for rel in (("emudeck", "settings.sh"), (".config", "EmuDeck", "settings.sh")):
        try:
            with io.open(os.path.join(home, *rel), encoding="utf-8", errors="replace") as f:
                text = f.read(256 * 1024)
        except OSError:
            continue
        for _key, value in _SETTING.findall(text):
            value = value.strip()
            if value.startswith("/") and value.count("/") >= 2:
                out.append(os.path.realpath(value))
    return out


def _ancestor_reason(real: str, game_file: bool) -> str:
    """Grund aus den Ordnern über dem Pfad, ohne dass hier ein Ort fest steht:
    - ein Ordner „Emulation“ mit dem Gerüst von EmuDeck (roms und tools/saves/storage/bios), so
      wie auf der Spiele-Platte (/run/media/<nutzer>/Spiele/Emulation, auch das alte Switch/Emulation);
    - eine Steam-Installation (steamapps neben steam.sh, steam.exe oder ubuntu12_32), auch eine
      von Windows auf einem NTFS-Laufwerk; ihre Spielordner (game_file) ausgenommen."""
    folder = os.path.dirname(real)
    while folder and folder != "/":
        if os.path.basename(folder).lower() == "emulation" and os.path.isdir(os.path.join(folder, "roms")) \
                and any(os.path.isdir(os.path.join(folder, sub)) for sub in ("tools", "saves", "storage", "bios")):
            return RO_EMUDECK
        if not game_file and os.path.isdir(os.path.join(folder, "steamapps")) \
                and any(os.path.exists(os.path.join(folder, n)) for n in ("steam.sh", "steam.exe", "ubuntu12_32")):
            return RO_STEAM
        folder = os.path.dirname(folder)
    return ""


def zone_reason(real: str, home: str | None = None) -> str:
    """Grund, warum dieser (aufgelöste) Pfad nur gelesen werden darf, oder "" (Ordnerregeln)."""
    parts = [p.lower() for p in real.strip("/").split("/")]
    game_file = _steam_game_file(parts)
    if any(_under(real, d) for d in _own_dirs()) or any(p in OWN_PARTS or p.startswith(OWN_PREFIXES) for p in parts):
        return RO_OWN
    if any(TRASH_PARTS.match(p) for p in parts[:-1]):
        return RO_TRASH
    base = _zone_home(home)
    below = _parts_below(real, base)
    if below is not None:
        low = [p.lower() for p in below]
        if low[:3] == [".local", "share", "trash"]:
            return RO_TRASH
        for zone, reason in HOME_ZONES:
            if tuple(low[:len(zone)]) == zone:
                if reason == RO_STEAM and game_file:
                    continue                    # Spieldateien in Steams eigener Bibliothek
                return reason
        if low and low[0] in HOME_EMULATOR_DIRS:
            return RO_EMULATOR
        if len(low) > 1 and low[0] == ".config" and low[1] in EMULATOR_DIRS:
            return RO_EMULATOR
        if len(low) > 2 and low[0] == ".local" and low[1] == "share" and low[2] in EMULATOR_DIRS:
            return RO_EMULATOR
    if "steamapps" in parts and not game_file:
        return RO_STEAM                         # Manifeste, compatdata, workshop … jeder Bibliothek
    if any(_under(real, p) for p in _emudeck_paths(base)):
        return RO_EMUDECK
    return _ancestor_reason(real, game_file)


def access_reason(real: str, st: os.stat_result) -> str:
    """Grund aus Rechten und Laufwerk, oder "" (die Datei lässt sich sicher ersetzen)."""
    try:
        if os.statvfs(real).f_flag & os.ST_RDONLY:
            return RO_MOUNT
    except OSError:
        pass
    if st.st_uid != os.getuid():
        return RO_OWNER                         # die neue Datei gehörte sonst uns
    if not os.access(real, os.W_OK):
        return RO_FILE
    if not os.access(os.path.dirname(real) or "/", os.W_OK | os.X_OK):
        return RO_FOLDER                        # die temporäre Datei muss daneben entstehen
    if st.st_nlink > 1:
        return RO_LINKS                         # os.replace setzt eine neue Datei an nur einen Namen
    return ""


def readonly_reason(real: str, st: os.stat_result, home: str | None = None) -> str:
    return zone_reason(real, home) or access_reason(real, st)


# ====================================================================== Lesen
def _resolve(path: Any, roots: list[str] | None) -> tuple[str, os.stat_result]:
    if not isinstance(path, str) or not path or "\0" in path or not os.path.isabs(path):
        raise Refuse("outside", TEXT_OUTSIDE)
    places = roots if roots is not None else _explorer_roots()
    real = os.path.realpath(path)
    if not places or not paths.is_under(real, places):
        raise Refuse("outside", TEXT_OUTSIDE)
    if not editable_name(real):
        raise Refuse("type", TEXT_TYPE)
    try:
        st = os.stat(real, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        raise Refuse("gone", TEXT_GONE)
    except OSError as e:
        raise Refuse("unreadable", _read_reason(e))
    if not stat.S_ISREG(st.st_mode):
        raise Refuse("not-file", TEXT_NOT_FILE)
    if st.st_size > MAX_BYTES:
        raise Refuse("too-big", TEXT_TOO_BIG % _size_text(st.st_size))
    return real, st


def _read(real: str, st: os.stat_result) -> tuple[bytes, os.stat_result]:
    """Inhalt und Stand über einen Deskriptor: kein Verweis (O_NOFOLLOW), keine FIFO (O_NONBLOCK,
    S_ISREG), dieselbe Datei wie geprüft (Gerät und Inode)."""
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise Refuse("outside", TEXT_OUTSIDE)
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            raise Refuse("gone", TEXT_GONE)
        raise Refuse("unreadable", _read_reason(e))
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise Refuse("conflict", TEXT_CONFLICT)
        chunks, total = [], 0
        while True:
            try:
                chunk = os.read(fd, 1 << 20)
            except OSError as e:
                raise Refuse("unreadable", _read_reason(e))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise Refuse("too-big", TEXT_TOO_BIG % _size_text(total))
            chunks.append(chunk)
        return b"".join(chunks), fst
    finally:
        os.close(fd)


def decode(raw: bytes) -> dict:
    """Bytes -> Text mit Format. Der Text hat "\\n" als Zeilenende und keinen letzten Zeilenumbruch
    (final_newline sagt, ob es einen gab); _lines/_ends/_eol braucht encode für den Rückweg."""
    if raw.startswith(b"\xff\xfe\x00\x00") or raw.startswith(b"\x00\x00\xfe\xff"):
        raise Refuse("encoding", TEXT_UTF32)
    bom = True
    if raw.startswith(BOM_UTF8):
        enc, body = "utf-8", raw[3:]
    elif raw.startswith(b"\xff\xfe"):
        enc, body = "utf-16-le", raw[2:]
    elif raw.startswith(b"\xfe\xff"):
        enc, body = "utf-16-be", raw[2:]
    else:
        enc, body, bom = "", raw, False
    if not enc:
        if b"\0" in body:
            raise Refuse("binary", TEXT_BINARY)
        try:
            text, enc = body.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, enc = body.decode("cp1252", ERRORS["cp1252"]), "cp1252"
    elif enc == "utf-8":
        text = body.decode("utf-8", ERRORS["utf-8"])
    else:
        if len(body) % 2:
            raise Refuse("encoding", TEXT_BAD_UTF16)
        text = body.decode(enc, ERRORS[enc])
    if "\0" in text:
        raise Refuse("binary", TEXT_BINARY)
    controls = len(_CONTROL.findall(text))
    if controls > 8 and controls * 100 > len(text):
        raise Refuse("binary", TEXT_BINARY)
    pieces = _EOL_SPLIT.split(text)
    lines, ends = pieces[0::2], pieces[1::2]
    final = bool(ends) and lines[-1] == ""
    if final:
        lines.pop()
    counts = {e: ends.count(e) for e in EOL_NAMES}
    if ends:
        # Gleichstand: CRLF vor LF vor CR (Windows-Spiele sind der häufigste Fall)
        eol = max(EOL_NAMES, key=lambda e: (counts[e], -list(EOL_NAMES).index(e)))
    else:
        eol = "\r\n" if bom or enc != "utf-8" else "\n"
    return {"text": "\n".join(lines), "encoding": enc, "bom": bom, "eol": EOL_NAMES[eol],
            "eol_mixed": sum(1 for n in counts.values() if n) > 1, "final_newline": final, "lines": len(lines),
            "_lines": lines, "_ends": ends, "_eol": eol}


def encode(text: Any, doc: dict) -> bytes:
    """Text der Seite -> Bytes im Format von doc (aus decode des alten Inhalts)."""
    if not isinstance(text, str):
        raise Refuse("bad-text", TEXT_BAD_TEXT)
    new = _EOL_ANY.split(text)
    eol = doc["_eol"]
    if doc["eol_mixed"]:
        ends = _carry_endings(doc["_lines"], doc["_ends"], doc["final_newline"], new, eol)
    else:
        ends = [eol] * (len(new) - 1) + ([eol] if doc["final_newline"] else [])
    body = "".join(line + (ends[i] if i < len(ends) else "") for i, line in enumerate(new))
    enc = doc["encoding"]
    try:
        data = body.encode(enc, ERRORS[enc])
    except UnicodeEncodeError as e:
        raise Refuse("encoding", TEXT_CHAR % (e.object[e.start:e.end][:1], LABELS[enc]))
    return (BOMS[enc] if doc["bom"] else b"") + data


def _carry_endings(old: list[str], old_ends: list[str], final: bool, new: list[str], eol: str) -> list[str]:
    """Gemischte Zeilenenden: Unveränderte Zeilen behalten ihr eigenes Ende, neue und geänderte
    bekommen das häufigste. Ergebnis: Enden nach jeder neuen Zeile (die letzte nur bei final)."""
    after_old = list(old_ends) + ([] if final else [None])
    mine: list[str | None] = [None] * len(new)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, _j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mine[j1 + k] = after_old[i1 + k]
    ends = [e or eol for e in mine[:-1]]
    if final:
        ends.append(mine[-1] or (old_ends[-1] if old_ends else eol))
    return ends


def _stamp(st: os.stat_result, raw: bytes) -> dict:
    return {"mtime_ns": str(st.st_mtime_ns), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _public(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def open_text(path: Any, *, roots: list[str] | None = None, home: str | None = None) -> dict:
    """RPC files.textOpen. roots: erlaubte Orte (Vorgabe: die des Explorers); home: Bezug der
    Zonen im persönlichen Ordner (Vorgabe: ~, im Prüfmodus <COUCH_TEST_ROOT>/home)."""
    try:
        real, st = _resolve(path, roots)
        raw, fst = _read(real, st)
        doc = decode(raw)
    except Refuse as e:
        return e.result()
    reason = readonly_reason(real, fst, home) or (RO_LINES if doc["lines"] > MAX_EDIT_LINES else "")
    stamp = _stamp(fst, raw)
    return dict(_public(doc), ok=True, path=path, real=real, name=os.path.basename(real),
                folder=os.path.dirname(path), label=LABELS[doc["encoding"]], expect=stamp, **stamp,
                readonly=bool(reason), readonly_reason=reason)


# ====================================================================== Speichern
_save_lock = threading.Lock()


def _parse_expect(expect: Any) -> tuple[int, int, str | None]:
    if isinstance(expect, dict):
        mtime, size, sha = expect.get("mtime_ns"), expect.get("size"), expect.get("sha256")
    elif isinstance(expect, (list, tuple)) and len(expect) in (2, 3):
        mtime, size, sha = (list(expect) + [None])[:3]
    else:
        raise Refuse("bad-expect", TEXT_NO_EXPECT)
    out = []
    for value in (mtime, size):
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
            raise Refuse("bad-expect", TEXT_NO_EXPECT)
        out.append(int(value))
    return out[0], out[1], sha if isinstance(sha, str) and _SHA.match(sha) else None


def save_text(path: Any, text: Any, expect: Any = None, *, roots: list[str] | None = None,
              home: str | None = None, trash: Callable[[str], None] | None = None) -> dict:
    """RPC files.textSave. expect: {mtime_ns, size, sha256?} aus open_text (oder [mtime_ns, size]).
    Kodierung, BOM und Zeilenenden kommen aus der Datei selbst, die laut expect genau die vom
    Öffnen ist. trash(pfad) legt alte Sicherungen in den Papierkorb (Vorgabe files.gio_trash)."""
    with _save_lock:
        try:
            return _save(path, text, expect, roots, home, trash)
        except Refuse as e:
            if e.code not in ("conflict", "readonly"):
                _log.info("Speichern %s: %s", path, e.message)
            return e.result()
        except OSError as e:
            _log.warning("Speichern %s: %s", path, e)
            return {"ok": False, "code": _write_code(e), "message": _write_reason(e)}
        except Exception:  # noqa: BLE001 - die Seite bekommt einen Satz, Einzelheiten stehen im Protokoll
            _log.exception("Speichern %s", path)
            return {"ok": False, "code": "failed", "message": TEXT_FAILED}


def _save(path: Any, text: Any, expect: Any, roots: list[str] | None, home: str | None,
          trash: Callable[[str], None] | None) -> dict:
    want_mtime, want_size, want_sha = _parse_expect(expect)
    if not isinstance(text, str):
        raise Refuse("bad-text", TEXT_BAD_TEXT)
    real, st = _resolve(path, roots)
    raw, fst = _read(real, st)
    if (fst.st_mtime_ns, len(raw)) != (want_mtime, want_size) \
            or (want_sha and hashlib.sha256(raw).hexdigest() != want_sha):
        raise Refuse("conflict", TEXT_CONFLICT)
    reason = readonly_reason(real, fst, home)
    if reason:
        raise Refuse("readonly", reason)
    doc = decode(raw)
    if doc["lines"] > MAX_EDIT_LINES:
        raise Refuse("readonly", RO_LINES)
    data = encode(text, doc)
    if len(data) > MAX_BYTES:
        raise Refuse("too-big", TEXT_NEW_TOO_BIG)
    if data == raw:
        return _saved(doc, _stamp(fst, raw), unchanged=True, backup="", message=TEXT_UNCHANGED)
    _check_space(os.path.dirname(real), len(data))
    backup = _make_backup(real, raw)
    try:
        _write_atomic(real, data, fst)
    except BaseException:
        _drop_backup(backup)
        raise
    try:
        prune(real, trash)
    except Exception:  # noqa: BLE001 - gespeichert ist; alte Sicherungen bleiben dann eben liegen
        _log.exception("Alte Sicherungen von %s", real)
    _log.info("Gespeichert: %s (%d Bytes, Sicherung %s)", real, len(data), backup)
    return _saved(decode(data), _stamp(os.stat(real), data), unchanged=False, backup=backup, message=TEXT_SAVED)


def _saved(doc: dict, stamp: dict, **extra: Any) -> dict:
    """Neuer Stand nach dem Speichern: Format und expect für das nächste Speichern, ohne den Text."""
    out = _public(doc)
    out.pop("text", None)
    return dict(out, ok=True, label=LABELS[doc["encoding"]], expect=stamp, **stamp, **extra)


def _check_space(folder: str, need: int) -> None:
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    if free < need + SPACE_SLACK:
        raise Refuse("no-space", TEXT_NO_SPACE)


def _fsync_dir(folder: str) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                                    # manche Dateisysteme (vfat, FUSE) können das nicht
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _write_atomic(real: str, data: bytes, st: os.stat_result) -> None:
    """Neuer Inhalt über eine temporäre Datei im selben Ordner: erst ganz geschrieben und auf der
    Platte (fsync), dann per os.replace an die Stelle der alten. Ein Absturz mittendrin lässt die
    alte Datei stehen; die halbe temporäre (eigene) wird entfernt."""
    folder = os.path.dirname(real)
    tmp = os.path.join(folder, TMP_PREFIX + uuid.uuid4().hex[:12] + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    placed = False
    try:
        try:
            os.fchmod(fd, stat.S_IMODE(st.st_mode))
        except OSError as e:                    # NTFS/vfat: Rechte kommen aus den Einhängeoptionen
            _log.info("Rechte für %s nicht übernommen: %s", real, e)
        if st.st_gid != os.getgid():
            try:
                os.fchown(fd, -1, st.st_gid)    # Gruppe wie vorher, soweit wir Mitglied sind
            except OSError:
                pass
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        # Letzter Blick: Hat ein anderes Programm (ein Spiel beim Beenden) die Datei während des
        # Schreibens ersetzt oder geändert, gilt sein Stand
        now = os.stat(real, follow_symlinks=False)
        if (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size) != (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size):
            raise Refuse("conflict", TEXT_CONFLICT)
        os.replace(tmp, real)
        placed = True
        _fsync_dir(folder)
    finally:
        if fd >= 0:
            os.close(fd)
        if not placed:
            try:
                os.unlink(tmp)                  # nur die eigene temporäre Datei
            except OSError:
                pass


# ====================================================================== Sicherungen
def backup_dir() -> str:
    """~/Games/backups/couch/bearbeitet; im Prüfmodus <COUCH_TEST_ROOT>/home/… (paths.user_place)."""
    return paths.user_place(os.path.join("Games", "backups", "couch", "bearbeitet"))


def backup_name(name: str, stamp: str, n: int = 1) -> str:
    """<stamp>-<name>, ab der zweiten Sicherung derselben Sekunde <stamp>-<n>-<name>; höchstens
    255 Bytes (der Name wird vor der Endung gekürzt)."""
    head = stamp + ("-%d" % n if n > 1 else "") + "-"
    room = 255 - len(os.fsencode(head))
    if len(os.fsencode(name)) > room:
        stem, ext = os.path.splitext(name)
        if len(os.fsencode(ext)) > 16:
            ext = ""
        cut = os.fsencode(stem)[:room - len(os.fsencode(ext))]
        # ein am Ende zerschnittenes UTF-8-Zeichen fällt weg; kaputt kodierte Namen bleiben Bytes
        broken = any(0xdc80 <= ord(c) <= 0xdcff for c in stem)
        name = (os.fsdecode(cut) if broken else cut.decode("utf-8", "ignore")) + ext
    return head + name


def _make_backup(real: str, raw: bytes) -> str:
    folder = backup_dir()
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1, 1000):
            name = backup_name(os.path.basename(real), stamp, n)
            target = os.path.join(folder, name)
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except FileExistsError:
                continue
            break
        else:
            raise Refuse("backup", TEXT_BACKUP % "kein freier Name")
        done = False
        try:
            _write_all(fd, raw)
            os.fsync(fd)
            done = True
        finally:
            os.close(fd)
            if not done:
                try:
                    os.unlink(target)           # die eigene halbe Sicherung
                except OSError:
                    pass
        _fsync_dir(folder)
    except OSError as e:
        raise Refuse("backup", TEXT_BACKUP % _short_reason(e))
    try:
        items = _index_read(folder)
        items[name] = {"source": real, "at": time.time_ns()}
        _index_write(folder, items)
    except OSError as e:                        # die Sicherung gibt es; sie wird nur nie aufgeräumt
        _log.warning("Verzeichnis der Sicherungen nicht geschrieben: %s", e)
    return target


def _drop_backup(target: str) -> None:
    """Speichern gescheitert: Die eben angelegte Sicherung gleicht der Datei, die stehen bleibt."""
    folder, name = os.path.split(target)
    try:
        os.unlink(target)
    except OSError:
        return
    try:
        items = _index_read(folder)
        if items.pop(name, None) is not None:
            _index_write(folder, items)
    except OSError:
        pass


def _index_read(folder: str) -> dict:
    try:
        with io.open(os.path.join(folder, INDEX_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        # Kaputt: neu beginnen. Was dann nicht mehr verzeichnet ist, räumt niemand weg (sicher)
        _log.warning("Verzeichnis der Sicherungen unlesbar, beginnt neu: %s", e)
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, dict)}


def _index_write(folder: str, items: dict) -> None:
    path = os.path.join(folder, INDEX_NAME)
    tmp = path + ".neu"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(jsontext.dumps({"version": 1, "items": items}, indent=1, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backups_of(real: str) -> list[str]:
    """Sicherungen dieser Datei laut Verzeichnis, die jüngste zuerst (nur vorhandene)."""
    folder = backup_dir()
    items = _index_read(folder)
    mine = sorted(((v.get("at") or 0, k) for k, v in items.items() if v.get("source") == real), reverse=True)
    return [os.path.join(folder, k) for _, k in mine if os.path.isfile(os.path.join(folder, k))]


def prune(real: str, trash: Callable[[str], None] | None = None, keep: int = BACKUP_KEEP) -> list[str]:
    """Behält die jüngsten keep Sicherungen dieser Datei; ältere kommen in den Papierkorb (nie
    endgültig gelöscht). Angefasst wird nur, was im Verzeichnis zu genau dieser Datei steht und als
    gewöhnliche Datei direkt im Sicherungsordner liegt."""
    folder = backup_dir()
    items = _index_read(folder)
    mine = sorted(((v.get("at") or 0, k) for k, v in items.items() if v.get("source") == real), reverse=True)
    put_away = trash or default_trash
    gone, changed = [], False
    for _at, name in mine[max(0, int(keep)):]:
        if not name or "/" in name or name.startswith(".") or name in (INDEX_NAME,):
            continue
        target = os.path.join(folder, name)
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            items.pop(name, None)               # schon weg (von Hand): nur aus dem Verzeichnis
            changed = True
            continue
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        try:
            put_away(target)
        except (OSError, ValueError) as e:
            _log.warning("Alte Sicherung bleibt: %s (%s)", target, e)
            continue
        items.pop(name, None)
        changed = True
        gone.append(target)
    if changed:
        _index_write(folder, items)
    return gone


def default_trash(path: str) -> None:
    """Papierkorb des Systems (files.gio_trash). Im Prüfmodus der Papierkorb im Käfig
    (xdgtrash.home_trash), damit nie etwas außerhalb landet (Gio legte auf /tmp einen eigenen an)."""
    if not paths.test_mode():
        from . import files
        files.gio_trash(path)
        return
    from . import xdgtrash
    if not paths.test_root():
        raise ValueError("Prüfmodus ohne Käfig: nichts in den Papierkorb gelegt.")
    base = xdgtrash.home_trash()
    os.makedirs(os.path.join(base, "files"), exist_ok=True)
    os.makedirs(os.path.join(base, "info"), exist_ok=True)
    name = os.path.basename(path)
    target = os.path.join(base, "files", name)
    while os.path.lexists(target) or os.path.lexists(os.path.join(base, "info", os.path.basename(target) + ".trashinfo")):
        target = os.path.join(base, "files", "%s.%s" % (name, uuid.uuid4().hex[:6]))
    info = os.path.join(base, "info", os.path.basename(target) + ".trashinfo")
    with io.open(info, "x", encoding="utf-8") as f:
        f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
            urllib.parse.quote(os.fsencode(path)), time.strftime("%Y-%m-%dT%H:%M:%S")))
    os.rename(path, target)


# ====================================================================== Meldungen
def _size_text(n: int) -> str:
    return ("%.1f MB" % (n / 1048576)).replace(".", ",")


def _read_reason(e: OSError) -> str:
    if e.errno in (errno.EACCES, errno.EPERM):
        return TEXT_NO_READ
    if e.errno == errno.EIO:
        return TEXT_READ_IO
    return TEXT_UNREADABLE


def _write_code(e: OSError) -> str:
    return {errno.ENOSPC: "no-space", errno.EDQUOT: "no-space", errno.EROFS: "readonly", errno.EACCES: "no-rights",
            errno.EPERM: "no-rights", errno.EIO: "io", errno.ENOENT: "gone"}.get(e.errno, "failed")


def _write_reason(e: OSError) -> str:
    return {"no-space": TEXT_NO_SPACE, "readonly": TEXT_ROFS, "no-rights": TEXT_NO_RIGHTS, "io": TEXT_IO,
            "gone": TEXT_GONE}.get(_write_code(e), TEXT_FAILED)


def _short_reason(e: OSError) -> str:
    return {"no-space": "kein Platz frei", "readonly": "Laufwerk schreibgeschützt", "no-rights": "keine Schreibrechte",
            "io": "Schreibfehler"}.get(_write_code(e), e.strerror or "Fehler")


# Kurzformen, wie im Auftrag benannt (dieses Modul selbst ruft nie das eingebaute open)
open = open_text  # noqa: A001
save = save_text
