"""Archiv ansehen, ohne es zu entpacken, und einen Installationsplan liefern (nur lesend).

Aus docs/research/prototypes/apps/archive_probe.py. probe() ruft `7z l` bzw. `unrar lt` in
bubblewrap auf (alles nur lesbar, kein Netz), mit Kennwort zusätzlich einen Test der kleinsten
verschlüsselten Datei. Es wird nichts angelegt, entpackt oder gelöscht.
E114: Auch ein Windows-Installationsprogramm (setup.exe, .msi) bekommt einen Plan; erkannt werden
Inno Setup, NSIS und MSI (setupexe.py), ein Archiv mit einem Installer darin ist ebenso installierbar.

probe() blockiert (Unterprozess, Platte): gehört in einen Thread.

Kennwörter stehen notgedrungen auf der Befehlszeile von 7z/unrar (-p…); sie werden nirgends
protokolliert und stehen in keinem Ergebnis. stdin der Werkzeuge ist immer /dev/null, -p ist immer
gesetzt (sonst fragen sie am Terminal nach).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess

from . import human_size, test_mode_violation, thread_limit
from . import setupexe, trash
from ..setup import catalog as _catalog

DUMMY_PW = "\x7fcouch-kein-kennwort\x7f"   # verhindert, dass 7z nach einem Kennwort fragt
SUPPORTED_FORMATS = ("zip", "7z", "rar")
SETUP_FORMATS = ("exe", "msi")             # E114: ein Installationsprogramm als Datei (kein Archiv)
LIST_TIMEOUT = 120
PASSWORD_TEST_TIMEOUT = 300
SPACE_FACTOR = 1.05                        # Aufschlag für Verwaltungsdaten des Dateisystems

# ---------------------------------------------------------------- Format und Teile

MAGIC = [
    (0, b"PK\x03\x04", "zip"), (0, b"PK\x05\x06", "zip"), (0, b"PK\x07\x08", "zip"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"Rar!\x1a\x07\x01\x00", "rar"), (0, b"Rar!\x1a\x07\x00", "rar"),
    (0, b"\x1f\x8b", "gz"), (0, b"\xfd7zXZ\x00", "xz"), (0, b"BZh", "bz2"), (0, b"\x28\xb5\x2f\xfd", "zst"),
    (257, b"ustar", "tar"), (32769, b"CD001", "iso"), (0, b"MZ", "exe"),
    # OLE-Verbunddatei: Windows Installer (.msi), aber auch alte Office-Dateien; als MSI zählt sie nur mit .msi
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
]


def sniff(path: str, deep: bool = True) -> str | None:
    """Format am Dateikopf erkennen. deep=False liest nur 8 Bytes (billig, für die Dateiauswahl)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(34000 if deep else 8)
    except OSError:
        return None
    for off, sig, kind in MAGIC:
        if head[off:off + len(sig)] == sig:
            return kind
    return None


VOLUME_PATTERNS = [
    (re.compile(r"^(?P<base>.+)\.part(?P<n>\d+)\.rar$", re.I), "rar-part"),
    (re.compile(r"^(?P<base>.+)\.(?P<ext>7z|zip|rar)\.(?P<n>\d{3,})$", re.I), "split"),
    (re.compile(r"^(?P<base>.+)\.r(?P<n>\d{2})$", re.I), "rar-old"),
    (re.compile(r"^(?P<base>.+)\.z(?P<n>\d{2})$", re.I), "zip-old"),
]


def volume_set(path: str, siblings: list | None = None) -> dict:
    """Findet alle Teile eines mehrteiligen Archivs, egal von welchem Teil aus.

    Rückgabe: {"kind", "base", "first", "parts", "missing"}. siblings: Namen im selben Ordner
    (wenn der Aufrufer sie schon hat, sonst wird der Ordner gelistet)."""
    folder, name = os.path.split(os.path.abspath(path))
    if siblings is None:
        try:
            siblings = os.listdir(folder)
        except OSError:
            siblings = [name]
    for rx, kind in VOLUME_PATTERNS:
        m = rx.match(name)
        if not m:
            continue
        base = m.group("base")
        found = {}
        for s in siblings:
            sm = rx.match(s)
            if sm and sm.group("base").lower() == base.lower() and \
               (kind != "split" or sm.group("ext").lower() == m.group("ext").lower()):
                found[int(sm.group("n"))] = s
        if kind == "rar-old":      # name.rar ist Teil 1, .r00 Teil 2 ...
            head = next((s for s in siblings if s.lower() == (base + ".rar").lower()), None)
            parts = ([head] if head else []) + [found[k] for k in sorted(found)]
            missing = ([] if head else [base + ".rar"]) + \
                      [("%s.r%02d" % (base, k)) for k in range(0, max(found) + 1) if k not in found]
            first = head
        elif kind == "zip-old":    # name.z01 ... und name.zip als letzter Teil; 7z bekommt name.zip
            tail = next((s for s in siblings if s.lower() == (base + ".zip").lower()), None)
            parts = [found[k] for k in sorted(found)] + ([tail] if tail else [])
            missing = [("%s.z%02d" % (base, k)) for k in range(1, max(found) + 1) if k not in found] + \
                      ([] if tail else [base + ".zip"])
            first = tail
        else:
            parts = [found[k] for k in sorted(found)]
            width = len(m.group("n"))
            if kind == "rar-part":
                missing = ["%s.part%0*d.rar" % (base, width, k) for k in range(1, max(found) + 1) if k not in found]
            else:
                missing = ["%s.%s.%0*d" % (base, m.group("ext"), width, k) for k in range(1, max(found) + 1) if k not in found]
            first = found.get(1)
        return {"kind": kind, "base": base, "first": os.path.join(folder, first) if first else None,
                "parts": [os.path.join(folder, p) for p in parts], "missing": missing}
    # name.rar mit .r00-Geschwistern oder name.zip mit .z01-Geschwistern
    stem, ext = os.path.splitext(name)
    for own_ext, sibling_rx in ((".rar", r"\.r\d{2}$"), (".zip", r"\.z\d{2}$")):
        if ext.lower() == own_ext:
            other = next((s for s in siblings if re.match(re.escape(stem) + sibling_rx, s, re.I)), None)
            if other:
                return volume_set(os.path.join(folder, other), siblings)
    return {"kind": "single", "base": os.path.splitext(name)[0], "first": os.path.abspath(path),
            "parts": [os.path.abspath(path)], "missing": []}


# ---------------------------------------------------------------- Werkzeuge im Käfig

HIDDEN_IN_SANDBOX = ("/tmp", "/run")   # im Käfig leer: dort liegen die Sockets von X11, D-Bus, Wayland, PipeWire


def tool_argv(tools: dict | None, name: str) -> list:
    """Befehl für ein Werkzeug ("7z", "unrar", "bwrap"). tools kann es ersetzen (Tests: Attrappe)."""
    value = (tools or {}).get(name) or name
    return list(value) if isinstance(value, (list, tuple)) else [str(value)]


def _hidden(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in HIDDEN_IN_SANDBOX)


def sandbox_argv(argv: list, *, read_only: list, writable: str | None = None, tools: dict | None = None) -> list:
    """Verpackt einen Befehl in bubblewrap: alles nur lesbar, kein Netz, eigene Prozessnummern.

    read_only: Ordner, die der Befehl lesen muss (Ordner des Archivs). Liegen sie unter /tmp oder
    /run, werden sie eigens eingebunden, weil diese beiden im Käfig leer sind.
    writable: der eine Ordner, in den geschrieben werden darf (Inhalt des Zwischenordners), oder None."""
    box = tool_argv(tools, "bwrap") + ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                                      "--tmpfs", "/tmp", "--tmpfs", "/run"]
    needed = [os.path.realpath(p) for p in read_only]
    # Auch ein ersetztes Werkzeug (Attrappe im Test) muss im Käfig lesbar bleiben
    needed += [os.path.dirname(os.path.realpath(a)) for a in argv[:3] if isinstance(a, str) and a.startswith("/")]
    seen = set()
    for real in needed:
        if _hidden(real) and real not in seen:
            seen.add(real)
            box += ["--ro-bind", real, real]
    if writable:
        box += ["--bind", writable, writable]
    box += ["--unshare-net", "--unshare-ipc", "--unshare-pid", "--die-with-parent", "--cap-drop", "ALL",
            "--chdir", "/", "--"]
    return box + list(argv)


TOOL_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "HOME": "/tmp"}


class ToolFailure(Exception):
    """Ein Werkzeug ließ sich nicht starten oder lief zu lange."""


def _run(argv: list, timeout: int) -> tuple:
    """Lesender Werkzeugaufruf. Nie protokollieren: argv kann ein Kennwort enthalten."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, env=TOOL_ENV, cwd="/")
    except subprocess.TimeoutExpired:
        raise ToolFailure("Das Archiv konnte nicht rechtzeitig gelesen werden.")
    except OSError as exc:
        raise ToolFailure("Ein Werkzeug zum Lesen des Archivs fehlt (%s)." % (exc.filename or exc.strerror))
    return p.returncode, p.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------- Auflisten

def list_7z(path: str, password: str | None, tools: dict | None = None, run=_run) -> dict:
    argv = tool_argv(tools, "7z") + ["l", "-slt", "-ba", "-sccUTF-8", "-p" + (password or DUMMY_PW), "--", path]
    rc, out = run(sandbox_argv(argv, read_only=[os.path.dirname(path)], tools=tools), LIST_TIMEOUT)
    if "Wrong password" in out or "Cannot open encrypted archive" in out:
        return {"status": "password-wrong" if password else "password-headers", "entries": [], "log": ""}
    if rc not in (0, 1) and "Path = " not in out:
        return {"status": "error", "entries": [], "log": out[-600:]}
    entries = []
    for block in re.split(r"\n\s*\n", out):
        f = dict(re.findall(r"^([A-Za-z ]+?) = (.*)$", block, re.M))
        if "Path" not in f:
            continue
        attr = f.get("Attributes", "")
        entries.append({
            "path": f["Path"].replace("\\", "/"),
            "size": int(f.get("Size") or 0) if (f.get("Size") or "0").isdigit() else 0,
            "dir": f.get("Folder") == "+" or attr.startswith("D"),
            "encrypted": f.get("Encrypted") == "+",
            "link": bool(re.search(r"(^|\s)l[rwx-]{9}", attr)) or bool(f.get("Symbolic Link")),
        })
    # 7z: 0 gut, 1 Warnung (z. B. Daten hinter dem Archivende), ab 2 Fehler (z. B. abgebrochener Download)
    status = "ok" if rc == 0 else "warnings" if rc == 1 else "damaged"
    return {"status": status, "entries": entries, "log": "" if rc == 0 else out[-600:]}


def list_rar(path: str, password: str | None, tools: dict | None = None, run=_run) -> dict:
    # -v: alle Teile eines mehrteiligen Archivs auflisten; -p-: nie nachfragen
    argv = tool_argv(tools, "unrar") + ["lt", "-v", "-cfg-", "-p" + (password or "-"), "--", path]
    rc, out = run(sandbox_argv(argv, read_only=[os.path.dirname(path)], tools=tools), LIST_TIMEOUT)
    if rc == 11 or "Incorrect password" in out:
        return {"status": "password-wrong" if password else "password-headers", "entries": [], "log": ""}
    entries, seen = [], set()
    for block in re.split(r"\n\s*\n", out):
        f = dict((k.strip(), v) for k, v in re.findall(r"^\s*([A-Za-z0-9 -]+?): (.*)$", block, re.M))
        if "Name" not in f or "Type" not in f:
            continue
        if f["Name"] in seen:          # Datei über mehrere Teile: nur einmal zählen
            continue
        seen.add(f["Name"])
        entries.append({
            "path": f["Name"].replace("\\", "/"),
            "size": int(f.get("Size") or 0) if (f.get("Size") or "0").isdigit() else 0,
            "dir": f["Type"].lower().startswith("dir"),
            "encrypted": "encrypted" in f.get("Flags", ""),
            "link": "link" in f["Type"].lower(),
        })
    if rc == 6 or "Cannot find volume" in out:
        return {"status": "incomplete", "entries": entries, "log": ""}
    if rc != 0 and not entries:
        return {"status": "error", "entries": [], "log": out[-600:]}
    # unrar meldet auch ein abgeschnittenes Archiv nur mit Rückgabe 1: jede Abweichung gilt als beschädigt
    return {"status": "ok" if rc == 0 else "damaged", "entries": entries, "log": "" if rc == 0 else out[-600:]}


def check_password(kind: str, path: str, password: str, entries: list, tools: dict | None = None, run=_run) -> bool | None:
    """Prüft das Kennwort an der kleinsten verschlüsselten Datei. None: nicht prüfbar (dauert zu lange)."""
    enc = sorted((e for e in entries if e["encrypted"] and not e["dir"]), key=lambda e: e["size"])
    if not enc:
        return True
    target = enc[0]["path"]
    if kind == "rar":
        argv = tool_argv(tools, "unrar") + ["t", "-cfg-", "-idq", "-p" + password, "--", path, target]
    else:
        # -spd: Dateiname wörtlich nehmen. Der Name steht als Argument hinter dem Archiv, nicht als -i!…
        argv = tool_argv(tools, "7z") + ["t", "-bso0", "-bsp0", "-spd", "-mmt=%d" % thread_limit(), "-p" + password,
                                         "--", path, target]
    try:
        rc, _ = run(sandbox_argv(argv, read_only=[os.path.dirname(path)], tools=tools), PASSWORD_TEST_TIMEOUT)
    except ToolFailure:
        return None
    return rc == 0


# ---------------------------------------------------------------- Sicherheit der Pfade

def path_issues(entries: list) -> list:
    issues = []
    for e in entries:
        p = e["path"]
        parts = p.split("/")
        if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
            issues.append(("absoluter Pfad", p))
        if ".." in parts:
            issues.append(("Pfad verlässt den Zielordner (..)", p))
        if e["link"]:
            issues.append(("symbolischer Link", p))
        if any(len(x.encode("utf-8", "replace")) > 255 for x in parts):
            issues.append(("Name länger als 255 Bytes", p[:80] + "…"))
        if re.search(r"[\x00-\x1f]", p):
            issues.append(("Steuerzeichen im Namen", repr(p)))
    return issues


# ---------------------------------------------------------------- Titel und Ordnername

NOISE = r"(?i)\b(gog|repack|fitgirl|dodi|elamigos|ankergames|portable|multi\d*|x64|x86|win64|win32|windows|linux|" \
        r"steamrip|rip|iso|goty|proper|incl|dlcs?|update|build|unity|unreal|drm[ -]?free|eur|usa|pal|ntsc)\b"


def clean_name(raw: str) -> str:
    """Archiv- oder Ordnername -> lesbarer Spieltitel."""
    t = raw
    t = re.sub(r"(?i)\.(part\d+\.rar|7z\.\d+|zip\.\d+|rar\.\d+|rar|zip|7z|r\d\d|z\d\d|tar\.(gz|xz|zst|bz2)|tgz)$", "", t)
    t = re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", " ", t)
    if " " not in t.strip():                       # Szene-Schreibweise: Punkte/Unterstriche/Bindestriche statt Leerzeichen
        if re.search(r"[._]", t):
            t = re.sub(r"-[A-Za-z0-9]{2,12}$", "", t)  # -GRUPPE am Ende
        t = re.sub(r"(?i)(?<![A-Za-z0-9])v\d+(\.\d+){1,4}[a-z]?\b.*$", "", t)   # v1.2.3 abschneiden, solange die Punkte noch da sind
        t = re.sub(r"[._]+", " ", t)
        if " " not in t.strip() and t.count("-") >= 2:
            t = t.replace("-", " ")                # Wörter-mit-Bindestrichen
    t = t.replace("_", " ")
    t = re.sub(r"(?i)\bv?\d+(\.\d+){1,4}[a-z]?\b.*$", "", t)   # ab der Versionsnummer abschneiden
    t = re.sub(r"(?i)\bbuild\s*\d+.*$", "", t)
    t = re.sub(NOISE, " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -._")
    return t or raw


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def safe_folder_name(title: str) -> str:
    """Titel -> ein einzelner, sichtbarer Ordnername (kein Pfad, nicht versteckt, nie "." oder "..")."""
    name = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', "", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    while len(os.fsencode(name)) > 120:           # Bytes auf der Platte (auch bei kaputter Kodierung)
        name = name[:-1]
    return name.strip(" .") or "Spiel"


def free_name(parent: str, name: str) -> str | None:
    """Erster freier Ordnername „<name> (2)“, „<name> (3)“ … in parent. Nur lesend."""
    for n in range(2, 100):
        candidate = "%s (%d)" % (name, n)
        if not os.path.lexists(os.path.join(parent, candidate)):
            return candidate
    return None


# ---------------------------------------------------------------- Einordnung

ROM_EXT = {   # nur Endungen, die eindeutig zu einem System gehören
    "xci": "switch", "nsp": "switch", "nsz": "switch", "xcz": "switch",
    "wud": "wiiu", "wux": "wiiu", "wua": "wiiu",
    "rvz": "gc", "gcz": "gc", "gcm": "gc", "wbfs": "wii", "wad": "wii",
    "3ds": "n3ds", "cia": "n3ds", "cci": "n3ds", "nds": "nds", "dsi": "nds",
    "gba": "gba", "gbc": "gbc", "gb": "gb", "sfc": "snes", "smc": "snes",
    "nes": "nes", "fds": "nes", "n64": "n64", "z64": "n64", "v64": "n64",
    "md": "genesis", "gen": "genesis", "smd": "genesis", "sms": "mastersystem", "gg": "gamegear",
    "32x": "sega32x", "pce": "pcengine", "ngp": "ngp", "ngc": "ngpc", "ws": "wonderswan", "wsc": "wonderswancolor",
    "lnx": "atarilynx", "a26": "atari2600", "j64": "atarijaguar", "vb": "virtualboy",
    "cso": "psp", "pbp": "psp", "gdi": "dreamcast", "cdi": "dreamcast",
}
ROM_AMBIGUOUS = {"iso", "bin", "cue", "chd", "img", "ccd", "mdf", "mds", "nrg", "elf", "m3u"}
# Systeme, die der Installer einordnet: Unterordner der ROM-Wurzel und der Starter, der vorhanden sein muss.
# E13: Switch und Wii U aus der SRM-Konfiguration mit den eigenen Startern des Nutzers. E105: dazu jedes System, dessen Emulator
# die ROM-Quelle liest (Katalog des Assistenten), Ziel <emu.root>/roms/<system>, Starter "" = das Flatpak des
# Emulators muss installiert sein. Wii U nur über SRM (Ordner-Spiele liest die ROM-Quelle nicht).
ROM_SYSTEMS = {"switch": ("switch", "eden-launch"), "wiiu": ("wiiu/roms", "cemu-launch")}
ROM_SYSTEMS.update({f: (f, "") for f in _catalog.library_systems(srm=False) if f not in ROM_SYSTEMS})
SYSTEM_LABELS = {k: v[0] for k, v in _catalog.SYSTEMS.items()}
# Endungen aus dem Katalog, die genau zu einem System gehören (.rpx nur in Wii-U-Ordnern)
for _ext, _folder in _catalog.unique_extensions().items():
    if _ext not in ROM_AMBIGUOUS and _ext != "rpx":
        ROM_EXT.setdefault(_ext, _folder)

EXE_HARD_EXCLUDE = re.compile(
    r"(?i)^(unins\d*|uninst.*|uninstall.*|vc_?redist.*|vcredist.*|dxsetup|dxwebsetup|directx.*|dotnet.*|ndp\d.*|"
    r"dotnetfx.*|windowsdesktop-runtime.*|oalinst|physx.*|ue4prereqsetup.*|ueprereqsetup.*|"
    r"unitycrashhandler(32|64)?|crashreportclient|crashpad_handler|crashsender\d*|bssndrpt(64)?|crs-handler|"
    r"easyanticheat.*|eac_?launcher|beservice.*|battleye.*|quicksfv|cefsharp\.browsersubprocess|"
    r"qtwebengineprocess|notification_helper|7z(a|g|r)?|python(w)?|cleanup|touchup|activation|"
    r"steam_?setup|gamelaunchhelper|createdump|sendrpt|dxdiag|.*subprocess.*)\.exe$")
EXE_BAD_DIRS = re.compile(
    r"(?i)(^|/)(_commonredist|__?redist|redist|redistributables?|__installer|_installer|directx|prerequisites?|"
    r"support|easyanticheat|battleye|extras|bonus|tools?|sdk|engine/extras|engine/binaries/thirdparty|"
    r"__macosx|dotnet|vcredist|soundtrack|manual)(/|$)")
EXE_SOFT_BAD = re.compile(
    r"(?i)(setup|install|config|settings|server|editor|crash|report|helper|updat|patch|redist|benchmark|"
    r"diag|register|trainer|keygen|language|launcher|modtool|dedicated|test)")
GENERIC_TOP = {"game", "games", "installer", "install", "setup", "bin", "app", "data", "release", "files",
               "windows", "win64", "linux", "build", "disc1", "cd1", "dvd", "iso", "crack", "newfolder", "neuerordner"}
INSTALLER_NAME = re.compile(r"(?i)^(setup.*|install(er)?.*|.*_setup|.*-setup|.*installer)\.(exe|msi)$")

# Beigaben neben dem eigentlichen Spielordner (Lies-mich, Verknüpfung zur Download-Seite, Start-Batch):
# Sie machen aus "ein Oberordner" sonst "lose Dateien" und das Spiel läge eine Ebene zu tief.
STRAY_EXT = {".txt", ".nfo", ".diz", ".md", ".url", ".lnk", ".html", ".htm", ".pdf", ".rtf", ".bat", ".cmd",
             ".jpg", ".jpeg", ".png", ".ico", ".webp"}
STRAY_MAX_FILES = 10
STRAY_MAX_BYTES = 10 * 1024 * 1024


def only_strays(items: list) -> bool:
    """items: [(name, größe)] der Dateien auf oberster Ebene. True, wenn alle nur Beigaben sind (auch: keine)."""
    return (len(items) <= STRAY_MAX_FILES and sum(size for _name, size in items) <= STRAY_MAX_BYTES
            and all(os.path.splitext(name)[1].lower() in STRAY_EXT for name, _size in items))


CHOICE_MIN_SCORE = 30    # darunter fragt die Oberfläche nach der Startdatei
CHOICE_MIN_GAP = 15      # ebenso, wenn der zweite Kandidat näher dran ist


def needs_choice(candidates: list) -> bool:
    """Ist die beste Startdatei nicht eindeutig? candidates: nach Punktzahl absteigend, je {"score"}."""
    if not candidates:
        return True
    if candidates[0]["score"] < CHOICE_MIN_SCORE:
        return True
    return len(candidates) > 1 and candidates[0]["score"] - candidates[1]["score"] < CHOICE_MIN_GAP


def classify(entries: list, archive_title: str) -> dict:
    """Ordnet den Inhalt ein. Rückgabe: {"kind", "title", "layout", "top_folder", …} je nach Art.

    kind: "windows-game" | "linux-game" | "rom" | "windows-installer" | "disc-image" | "unknown".
    Kandidaten: {"archive_path" (Pfad im Archiv), "path" (im fertigen Spielordner), "score", "why", "size"}."""
    files = [e for e in entries if not e["dir"]]
    tops = sorted({e["path"].split("/")[0] for e in entries if e["path"]})
    top_files = [e for e in files if "/" not in e["path"]]
    top_dirs = sorted({e["path"].split("/")[0] for e in entries if "/" in e["path"] or e["dir"]} - {""})
    # Ein Oberordner; daneben höchstens ein paar Beigaben, die beim Installieren wegfallen
    single_top = len(top_dirs) == 1 and len(tops) == 1 + len(top_files) and \
        only_strays([(e["path"], e["size"]) for e in top_files])
    strays = [e["path"] for e in top_files] if single_top else []
    if single_top:
        files = [e for e in files if "/" in e["path"]]
        tops = top_dirs
    prefix = tops[0] + "/" if single_top else ""

    def rel(e):
        return e["path"][len(prefix):] if prefix else e["path"]

    # Oberordner wie "Game" oder "Installer" sagen nichts über den Titel
    generic = single_top and norm(tops[0]) in GENERIC_TOP
    title = clean_name(tops[0]) if single_top and not generic else archive_title
    lower = [(rel(e).lower(), e) for e in files]
    result = {"layout": "single-folder" if single_top else "loose", "top_folder": tops[0] if single_top else None,
              "title": title, "candidates": [], "excluded": [], "exe": None, "needs_choice": False,
              "system": None, "rom_layout": None, "rom_items": [], "note": "", "stray": strays}

    # --- Wii U (Ordner mit code/*.rpx und meta/meta.xml), auch eine Ebene tiefer
    all_paths = {e["path"].lower() for e in files}
    for e in files:
        m = re.match(r"(?i)^(?:(.*)/)?code/[^/]+\.rpx$", e["path"])
        if not m:
            continue
        inner = m.group(1) or ""               # Ordner im Archiv, der code/ und meta/ enthält ("" = oberste Ebene)
        if ((inner + "/" if inner else "") + "meta/meta.xml").lower() in all_paths:
            result.update(kind="rom", system="wiiu", rom_layout="folder", rom_items=[inner or "."])
            if inner:
                result["title"] = clean_name(os.path.basename(inner))
            return result

    # --- ROM-Dateien mit eindeutiger Endung
    roms = [(r, e) for r, e in lower if r.rsplit(".", 1)[-1] in ROM_EXT]
    big = sum(e["size"] for _, e in lower) or 1
    if roms and sum(e["size"] for _, e in roms) / big > 0.8:
        systems = sorted({ROM_EXT[r.rsplit(".", 1)[-1]] for r, _ in roms})
        result.update(kind="rom", system=systems[0] if len(systems) == 1 else None,
                      rom_layout="files", rom_items=[e["path"] for _, e in roms])
        if len(roms) == 1:
            result["title"] = clean_name(os.path.splitext(os.path.basename(roms[0][1]["path"]))[0])
        return result

    exes = [(r, e) for r, e in lower if r.endswith(".exe")]
    msis = [(r, e) for r, e in lower if r.endswith(".msi")]

    # --- Linux
    linux = []
    for r, e in lower:
        b = os.path.basename(r)
        depth = r.count("/")
        if b.endswith(".appimage"):
            linux.append((90 - depth * 10, e, "AppImage"))
        elif re.match(r"^(start|run|launch|play)[^/]*\.sh$", b) or b == norm(title) + ".sh":
            linux.append((85 - depth * 10, e, "Startskript"))
        elif b.endswith((".x86_64", ".x64")):
            linux.append((65 - depth * 10, e, "Linux-Programm"))
        elif b.endswith(".x86"):
            linux.append((40 - depth * 10, e, "Linux-Programm (32 Bit)"))
        elif b.endswith(".sh") and depth == 0:
            linux.append((30, e, "Shell-Skript"))
    linux.sort(key=lambda x: (-x[0], rel(x[1]).lower()))
    linux_candidates = [{"archive_path": e["path"], "path": rel(e), "score": s, "why": [w], "size": e["size"]}
                        for s, e, w in linux[:12]]
    if linux and not exes:
        result.update(kind="linux-game", candidates=linux_candidates, exe=linux_candidates[0]["path"],
                      needs_choice=needs_choice(linux_candidates))
        return result

    # --- Installer oder Datenträgerabbild
    game_markers = any(re.search(r"(_data/|/binaries/win64/|/content/paks/|\.pak$|unityplayer\.dll$|\.pck$)", r) for r, _ in lower)
    top_installers = [(r, e) for r, e in exes + msis if "/" not in r and INSTALLER_NAME.match(os.path.basename(r))]
    # Fertig installierte Spiele bringen oft noch ein setup_*.exe mit (z. B. den Starter des Herstellers).
    # Liegt daneben ein überzeugendes Spielprogramm, ist das Archiv kein Installer.
    scored = score_exes(exes, lower, title, prefix) if exes else []
    strong_game_exe = any(c["score"] >= CHOICE_MIN_SCORE and not INSTALLER_NAME.match(os.path.basename(c["path"]))
                          for c in scored)
    if (top_installers and not game_markers and not strong_game_exe) or (msis and not exes):
        pick = sorted(top_installers or msis, key=lambda x: (not x[0].endswith(".exe"), -x[1]["size"]))[0]
        result.update(kind="windows-installer", exe=rel(pick[1]))
        return result
    discs = [(r, e) for r, e in lower if r.rsplit(".", 1)[-1] in ("iso", "mdf", "nrg", "cue", "bin", "img")]
    if discs and not exes and sum(e["size"] for _, e in discs) / big > 0.8:
        result.update(kind="disc-image", rom_items=[e["path"] for _, e in discs])
        return result

    # --- Windows-Spiel: Exe-Kandidaten bewerten
    if exes:
        usable = [c for c in scored if c["score"] > -50]
        result.update(kind="windows-game", candidates=usable[:12],
                      excluded=[c["path"] for c in scored if c["score"] <= -50][:12],
                      exe=usable[0]["path"] if usable else None, needs_choice=needs_choice(usable))
        return result

    if linux:
        result.update(kind="linux-game", candidates=linux_candidates, exe=linux_candidates[0]["path"],
                      needs_choice=needs_choice(linux_candidates))
        return result

    amb = [e["path"] for r, e in lower if r.rsplit(".", 1)[-1] in ROM_AMBIGUOUS]
    result.update(kind="unknown", note=("Enthält %d Abbild-/ROM-Dateien mit mehrdeutiger Endung." % len(amb)) if amb else "")
    return result


def score_exes(exes: list, lower: list, title: str, prefix: str = "") -> list:
    """Bewertet .exe-Dateien (Tabelle in docs/research/06, Abschnitt 5.9). Beste zuerst."""
    names = {r for r, _ in lower}
    dirs = set()
    for r in names:
        parts = r.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    max_size = max(e["size"] for _, e in exes) or 1
    tnorm = norm(title)
    has64 = any(re.search(r"(win64|x64|bin64|64\.exe$)", r) for r, _ in exes)
    out = []
    for r, e in exes:
        base = os.path.basename(r)
        stem = base[:-4]
        folder = os.path.dirname(r)
        depth = r.count("/")
        score, why = 0, []
        if EXE_HARD_EXCLUDE.match(base):
            score -= 100
            why.append("Hilfsprogramm (Ausschlussliste)")
        if EXE_BAD_DIRS.search(folder + "/"):
            score -= 80
            why.append("liegt in einem Redist-/Tool-Ordner")
        snorm = norm(re.sub(r"(?i)[-_ ]?(win64|win32|x64|x86|64|shipping|dx11|dx12|vulkan|final|game)\b", "", stem))
        if tnorm and tnorm in (snorm, norm(stem)):
            score += 50
            why.append("Name = Spieltitel")
        elif tnorm and snorm and (snorm in tnorm or tnorm in snorm) and len(snorm) >= 4:
            score += 30
            why.append("Name ähnelt dem Spieltitel")
        elif tnorm and snorm and _initials(title) == snorm and len(snorm) >= 3:
            score += 20
            why.append("Name = Anfangsbuchstaben des Titels")
        if (folder + "/" if folder else "") + stem + "_data" in dirs:
            score += 35
            why.append("Unity: Ordner %s_Data daneben" % os.path.basename(e["path"])[:-4])
        if re.search(r"-win64-shipping\.exe$", base):
            score += 20
            why.append("Unreal: Shipping-Programm")
        if depth == 0 and any(re.match(re.escape(stem) + r"/binaries/win64/.+\.exe$", n) for n in names):
            score += 45
            why.append("Unreal: Starter im Hauptordner")
        score += max(0, 12 - depth * 6)
        if depth == 0:
            why.append("im Hauptordner")
        size_part = int(20 * math.log10(max(e["size"], 1)) / math.log10(max_size)) if max_size > 1 else 0
        score += size_part - 10
        if e["size"] == max_size:
            score += 8
            why.append("größte Exe")
        if e["size"] < 150_000:
            score -= 10
            why.append("sehr klein")
        if re.search(r"(win64|x64|bin64|64\.exe$)", r):
            score += 6
        elif has64 and re.search(r"(win32|x86|bin32|32\.exe$)", r):
            score -= 15
            why.append("32-Bit-Variante")
        m = EXE_SOFT_BAD.search(stem)
        if m and score > -50:
            score -= 15 if m.group(1).lower() == "launcher" else 35
            why.append("Name enthält »%s«" % m.group(1))
        # archive_path = Pfad im Archiv, path = Pfad im fertigen Spielordner (Oberordner entfernt)
        out.append({"archive_path": e["path"], "path": e["path"][len(prefix):], "size": e["size"],
                    "score": score, "why": why})
    out.sort(key=lambda c: (-c["score"], c["path"].count("/"), c["path"].lower()))
    return out


def _initials(title: str) -> str:
    return "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", title)).lower()


# ---------------------------------------------------------------- Ziel und Platz

def default_rom_dirs(home: str | None = None) -> dict:
    """ROM-Ordner der Systeme, für die auf diesem Rechner ein Emulator eingerichtet ist. Nur lesend.

    E13: Die ROM-Wurzel steht in der Konfiguration des Steam ROM Managers (EmuDeck); Switch und Wii U zählen
    nur, wenn ihr Ordner und ihr Starter (~/.local/bin/eden-launch, cemu-launch) vorhanden sind.
    E105: Ist library.roms an, zählt dazu <emu.root>/roms/<system> jedes Katalog-Systems, wenn der Ordner da ist
    (kein Verweis; angelegt wird hier nichts) und das Flatpak eines passenden Emulators installiert ist."""
    home = home or os.path.expanduser("~")
    found = {}
    srm = os.path.isfile(os.path.join(home, ".config/steam-rom-manager/userData/userConfigurations.json"))
    try:
        with open(os.path.join(home, ".config/steam-rom-manager/userData/userSettings.json"), encoding="utf-8") as fh:
            roms = json.load(fh)["environmentVariables"]["romsDirectory"]
    except (OSError, ValueError, KeyError, TypeError):
        roms = None
    if isinstance(roms, str) and os.path.isabs(roms):
        for system, (sub, launcher) in ROM_SYSTEMS.items():
            if not launcher:
                continue
            folder = os.path.join(roms, sub)
            if os.path.isdir(folder) and os.path.exists(os.path.join(home, ".local/bin", launcher)):
                found[system] = folder
    try:
        found.update({k: v for k, v in _catalog_rom_dirs(home, srm).items() if k not in found})
    except Exception:  # noqa: BLE001  (nur ein Zusatz: die SRM-Ordner von oben gelten weiter)
        pass
    return found


def _catalog_rom_dirs(home: str, srm: bool) -> dict:
    """E105: {system: <emu.root>/roms/<system>} der Katalog-Systeme mit installiertem Emulator."""
    import stat as stat_mod
    from .. import paths, setup
    from ..library import sources
    from ..setup import folders
    if not setup.flag("library.roms", True):
        return {}
    fx = sources.fixtures_root()
    disk = setup.abspath_setting("library.games_disk") or (os.path.join(fx, "games") if fx else paths.games_disk())
    root = folders.emu_root(setup.abspath_setting("emu.root"), home, disk)
    if not root:
        return {}
    dirs = [os.path.join(fx, "flatpak")] if fx else ["/var/lib/flatpak", os.path.join(home, ".local/share/flatpak")]
    out = {}
    for system, emus in _catalog.library_systems(srm).items():
        if system == "wiiu" or ROM_SYSTEMS.get(system, ("", ""))[1]:
            continue                            # Wii U und SRM-Systeme: oben
        folder = folders.roms_dir(root) and os.path.join(folders.roms_dir(root), system)
        try:
            if not stat_mod.S_ISDIR(os.lstat(folder).st_mode):
                continue
        except OSError:
            continue
        if any(os.path.isdir(os.path.join(d, "app", e["flatpak"], "current", "active")) for d in dirs for e in emus):
            out[system] = folder
    return out


def check_games_root(games_root: str, mounts=None) -> str | None:
    """Ist der Spiele-Ordner benutzbar? None = ja, sonst ein deutscher Grund. Nur lesend.

    Der Ordner wird nie angelegt: Fehlt die Platte, wird nicht auf die Systemplatte geschrieben."""
    if not isinstance(games_root, str) or not os.path.isabs(games_root):
        return "Es ist kein gültiger Spiele-Ordner eingestellt."
    # Dieselbe Regel wie der Papierkorb: nie etwas anlegen, was sich nicht wieder entfernen ließe
    refusal = trash.root_refusal(games_root)
    if refusal is not None:
        return refusal
    real = os.path.realpath(games_root)
    if not os.path.isdir(real):
        return "Die Spiele-Platte ist nicht eingehängt (%s)." % games_root
    if real.startswith("/run/media/"):
        # Unter /run/media bleibt manchmal ein leerer Ordner zurück, obwohl nichts eingehängt ist
        try:
            mounted = real in set((mounts or trash.read_mounts)())
        except OSError:
            mounted = False
        if not mounted:
            return "Die Spiele-Platte ist nicht eingehängt (%s)." % games_root
    if not os.access(real, os.W_OK | os.X_OK):
        return "Auf die Spiele-Platte kann nicht geschrieben werden (%s)." % games_root
    return test_mode_violation(real)


def resolve_target(content: dict, folder: str | None, games_root: str, rom_dirs: dict | None) -> dict:
    """Wohin kommt der Inhalt? Rein rechnerisch plus lexists-Abfragen, nichts wird angelegt.

    Rückgabe: {"mode": "game"|"rom-folder"|"rom-files"|None, "root", "parent", "name", "dir",
               "files", "exists", "existing", "suggestion", "problem": (code, text) | None}."""
    kind = content.get("kind")
    target = {"mode": None, "root": games_root, "parent": None, "name": None, "dir": None, "files": [],
              "exists": False, "existing": [], "suggestion": None, "problem": None}
    if kind in ("windows-game", "linux-game", "windows-installer"):
        # E114: Ein Installationsprogramm installiert in einen eigenen Spielordner wie ein Archiv-Spiel
        target.update(mode="game", parent=games_root)
    elif kind == "rom":
        system = content.get("system")
        if system not in ROM_SYSTEMS or system not in (rom_dirs or {}):
            label = SYSTEM_LABELS.get(system) or system or "dieses System"
            target["problem"] = ("unsupported", "Für %s ist auf dieser Konsole kein Emulator eingerichtet." % label)
            return target
        rom_dir = rom_dirs[system]
        real_rom, real_root = os.path.realpath(rom_dir), os.path.realpath(games_root)
        try:
            same_device = os.stat(real_rom).st_dev == os.stat(real_root).st_dev
        except OSError:
            same_device = False
        if not real_rom.startswith(real_root + os.sep) or not same_device:
            # Version 1 schreibt ausschließlich unterhalb des Spiele-Ordners (E16), und der
            # Zwischenordner muss auf demselben Dateisystem liegen wie das Ziel (rename)
            target["problem"] = ("unsupported", "Der ROM-Ordner (%s) liegt nicht auf der Spiele-Platte." % rom_dir)
            return target
        target.update(mode="rom-folder" if content.get("rom_layout") == "folder" else "rom-files", parent=rom_dir)
    else:
        return target
    if target["mode"] == "rom-files":
        target["dir"] = target["parent"]
        target["files"] = [safe_folder_name(os.path.basename(item)) for item in content.get("rom_items", [])]
        target["existing"] = [n for n in target["files"] if os.path.lexists(os.path.join(target["parent"], n))]
        target["exists"] = bool(target["existing"])
        if len(set(target["files"])) != len(target["files"]):
            target["problem"] = ("unsupported", "Das Archiv enthält mehrere ROM-Dateien mit demselben Namen.")
    else:
        name = safe_folder_name(folder or content.get("title") or "")
        target.update(name=name, dir=os.path.join(target["parent"], name))
        target["exists"] = os.path.lexists(target["dir"])
        if target["exists"]:
            target["existing"] = [name]
            target["suggestion"] = free_name(target["parent"], name)
    return target


def free_bytes(path: str, statvfs=os.statvfs) -> int:
    st = statvfs(path)
    return st.f_bavail * st.f_frsize


# ---------------------------------------------------------------- Plan

KIND_LABELS = {"windows-game": "PC-Spiel (Windows)", "linux-game": "PC-Spiel (Linux)", "rom": "Emulator-Spiel",
               "windows-installer": "Installationsprogramm (Windows)", "disc-image": "Datenträgerabbild",
               "unknown": "Unbekannter Inhalt", "windows-exe": "Programm (Windows)"}
SETUP_LABEL = "Installationsprogramm (%s)"     # mit setupexe.TYPE_LABELS, wenn die Art bekannt ist
PLATFORMS = {"windows-game": "windows", "linux-game": "linux", "windows-installer": "windows"}


def _problem(plan: dict, code: str, message: str) -> None:
    plan["problems"].append({"code": code, "message": message})


def _finish(plan: dict) -> dict:
    """Setzt status, message, supported und ready aus der Problemliste."""
    codes = [p["code"] for p in plan["problems"]]
    # supported=False nur, wenn Version 1 mit dieser Datei grundsätzlich nichts anfangen kann
    plan["supported"] = not any(c in codes for c in ("unsupported", "unknown-format", "not-found", "error", "damaged"))
    plan["ready"] = not plan["problems"]
    if plan["problems"]:
        plan["status"] = plan["problems"][0]["code"]
        plan["message"] = plan["problems"][0]["message"]
    else:
        plan["status"] = "ok-with-warnings" if plan.get("issues") else "ok"
        plan["message"] = ""
    return plan


def probe(path: str, password: str | None = None, *, games_root: str, reserve_bytes: int,
          rom_dirs: dict | None = None, folder: str | None = None, verify_password: bool = True,
          tools: dict | None = None, statvfs=os.statvfs, mounts=None, run=_run) -> dict:
    """Sieht in ein Archiv und liefert den Plan. Blockierend (Unterprozess, Platte). Nur lesend.

    path: ein Archiv oder irgendein Teil eines mehrteiligen Satzes.
    games_root, reserve_bytes: aus den Einstellungen. rom_dirs: {"switch": <ordner>, "wiiu": <ordner>}.
    folder: gewünschter Ordnername (sonst aus dem Titel). verify_password=False spart den Kennworttest.

    Plan (JSON-tauglich), immer vorhanden:
      archive, name, format, volumes{kind,count,missing,first,packed_bytes}, supported, ready,
      status, message, problems[{code,message}]
    nach erfolgreichem Auflisten zusätzlich:
      kind, kind_label, platform, system, title, files, unpacked_bytes, password ("none"|"content"|
      "headers"), needs_password, issues[{problem,path}], paths_safe, layout, top_folder,
      stray[Beigaben neben dem Spielordner, die nicht mitinstalliert werden],
      candidates[{archive_path,path,score,why,size}], excluded, exe, needs_choice, rom_layout, rom_items,
      target{mode,root,parent,name,dir,files,exists,existing,suggestion}, space{needed_bytes,
      free_bytes,reserve_bytes,ok,missing_bytes,checked_at}
    E114, Installationsprogramme: im Archiv zusätzlich installed_bytes (Schätzung); als Datei (format
      "exe"/"msi") installer_type ("inno"|"nsis"|"msi"|None), setup_files, setup_bytes, installed_bytes,
      kind "windows-installer" bzw. "windows-exe" (gewöhnliches Programm, nicht unterstützt)
    ready=True heißt: kann so installiert werden. supported=False heißt: Version 1 kann das nicht."""
    password = password or None
    plan = {"archive": os.path.abspath(path), "name": os.path.basename(path), "format": None, "volumes": None,
            "supported": False, "ready": False, "status": "", "message": "", "problems": [], "kind": None}
    if not os.path.isfile(path):
        _problem(plan, "not-found", "Die Datei wurde nicht gefunden.")
        return _finish(plan)
    vols = volume_set(path)
    plan["volumes"] = {"kind": vols["kind"], "count": len(vols["parts"]), "missing": vols["missing"],
                       "first": vols["first"],
                       "packed_bytes": sum(os.path.getsize(p) for p in vols["parts"] if os.path.isfile(p))}
    plan["format"] = next((k for k in (sniff(p) for p in vols["parts"][:1]) if k), None)
    if vols["missing"] or not vols["first"]:
        _problem(plan, "incomplete", "Es fehlen Teile des Archivs: " + ", ".join(vols["missing"] or ["der erste Teil"]) + ".")
        return _finish(plan)
    first = vols["first"]
    plan["archive"] = first
    plan["name"] = os.path.basename(first)
    kind = sniff(first)
    if kind == "ole":
        kind = "msi" if first.lower().endswith(".msi") else None
    plan["format"] = kind
    if kind is None:
        _problem(plan, "unknown-format", "Diese Datei ist kein Archiv, das die Konsole entpacken kann.")
        return _finish(plan)
    if kind in SETUP_FORMATS:
        return _probe_setup(plan, first, folder=folder, games_root=games_root, reserve_bytes=reserve_bytes,
                            statvfs=statvfs, mounts=mounts)
    if kind not in SUPPORTED_FORMATS:
        plan["kind"] = "unknown"
        _problem(plan, "unsupported", "Dieses Archivformat (%s) wird noch nicht unterstützt." % kind)
        return _finish(plan)

    lister = list_rar if kind == "rar" else list_7z
    try:
        # Erst ohne Kennwort: so ist klar, ob schon die Dateinamen verschlüsselt sind
        listing = lister(first, None, tools, run)
        names_hidden = listing["status"] == "password-headers"
        if names_hidden and password:
            listing = lister(first, password, tools, run)
    except ToolFailure as exc:
        _problem(plan, "error", str(exc))
        return _finish(plan)
    plan["password"] = "headers" if names_hidden else "none"
    plan["needs_password"] = names_hidden
    if listing["status"] == "password-headers":
        _problem(plan, "password-needed", "Dieses Archiv ist mit einem Kennwort geschützt.")
        return _finish(plan)
    if listing["status"] == "password-wrong":
        _problem(plan, "password-wrong", "Das Kennwort stimmt nicht.")
        return _finish(plan)
    if listing["status"] == "incomplete":
        _problem(plan, "incomplete", "Es fehlen Teile des Archivs.")
        return _finish(plan)
    if listing["status"] == "error":
        _problem(plan, "error", "Das Archiv lässt sich nicht lesen. Es ist beschädigt oder unvollständig.")
        return _finish(plan)

    entries = listing["entries"]
    files = [e for e in entries if not e["dir"]]
    total = sum(e["size"] for e in files)
    plan["files"] = len(files)
    plan["unpacked_bytes"] = total
    plan["issues"] = [{"problem": a, "path": b} for a, b in path_issues(entries)][:20]
    plan["paths_safe"] = not plan["issues"]
    if listing["status"] == "warnings":
        plan["issues"].append({"problem": "Das Archiv meldet Warnungen beim Lesen", "path": ""})
    elif listing["status"] == "damaged":
        # Die Auflistung ist unvollständig: nicht erst nach vielen Gigabyte beim Entpacken scheitern
        _problem(plan, "damaged", "Das Archiv ist beschädigt oder unvollständig (zum Beispiel ein abgebrochener Download).")

    content = classify(entries, clean_name(os.path.basename(vols["base"] if vols["kind"] != "single" else first)))
    for key in ("kind", "title", "layout", "top_folder", "candidates", "excluded", "exe", "needs_choice",
                "system", "rom_layout", "rom_items", "stray"):
        plan[key] = content[key]
    plan["platform"] = PLATFORMS.get(content["kind"])
    plan["kind_label"] = KIND_LABELS[content["kind"]]
    if content["kind"] == "rom" and content["system"] in SYSTEM_LABELS:
        plan["kind_label"] = "Spiel für " + SYSTEM_LABELS[content["system"]]
    if not files:
        _problem(plan, "unsupported", "Dieses Archiv ist leer.")
    elif content["kind"] == "disc-image":
        _problem(plan, "unsupported", "Datenträgerabbilder werden noch nicht unterstützt.")
    elif content["kind"] == "unknown":
        _problem(plan, "unsupported", "In diesem Archiv wurde kein Spiel gefunden."
                 + (" " + content["note"] if content["note"] else ""))
    elif content["kind"] == "rom" and content["system"] is None:
        _problem(plan, "unsupported", "Das Archiv enthält Spiele für mehrere Systeme.")

    if any(e["encrypted"] for e in entries) and not names_hidden:
        plan["password"] = "content"
        plan["needs_password"] = True
        # Sind die Namen lesbar und ist kein Spiel darin, wird gar nicht erst nach dem Kennwort gefragt
        if not plan["problems"]:
            if not password:
                _problem(plan, "password-needed", "Dieses Archiv ist mit einem Kennwort geschützt.")
            elif verify_password and check_password(kind, first, password, entries, tools, run) is False:
                _problem(plan, "password-wrong", "Das Kennwort stimmt nicht.")

    need = int(total * SPACE_FACTOR)
    if content["kind"] == "windows-installer":
        # E114: Die Setup-Dateien liegen entpackt im Zwischenordner, bis der Installer in den Spielordner
        # installiert hat; beides zugleich auf der Spiele-Platte (Annahme wie setupexe.INSTALL_FACTOR)
        plan["installed_bytes"] = int(total * setupexe.INSTALL_FACTOR)
        need += plan["installed_bytes"]
    _target_and_space(plan, content, folder, games_root, rom_dirs, need + int(reserve_bytes), int(reserve_bytes),
                      statvfs, mounts)
    return _finish(plan)


def _target_and_space(plan: dict, content: dict, folder: str | None, games_root: str, rom_dirs: dict | None,
                      need: int, reserve: int, statvfs, mounts) -> None:
    """Ziel (plan["target"]) und Platz (plan["space"]) eintragen, samt Problemen. need: Bytes inklusive Reserve."""
    target = resolve_target(content, folder, games_root, rom_dirs)
    problem = target.pop("problem")
    plan["target"] = target
    root_problem = check_games_root(games_root, mounts)
    target["available"] = root_problem is None
    if problem and not any(p["code"] == "unsupported" for p in plan["problems"]):
        _problem(plan, *problem)
    if target["mode"] is not None:
        if root_problem:
            _problem(plan, "no-target", root_problem)
        elif target["exists"]:
            if target["mode"] == "rom-files":
                _problem(plan, "target-exists", "Im ROM-Ordner liegt schon eine Datei mit diesem Namen: %s." % ", ".join(target["existing"]))
            else:
                _problem(plan, "target-exists", "Auf der Spiele-Platte gibt es schon einen Ordner „%s“." % target["name"])

    plan["space"] = {"needed_bytes": need, "free_bytes": None, "reserve_bytes": reserve, "ok": False,
                     "missing_bytes": None, "checked_at": games_root}
    if target["available"]:
        try:
            free = free_bytes(os.path.realpath(games_root), statvfs)
        except OSError:
            free = None
        if free is not None:
            plan["space"].update(free_bytes=free, ok=free >= need, missing_bytes=max(0, need - free))
            if free < need and target["mode"] is not None:
                _problem(plan, "no-space", "Nicht genug Speicherplatz. Es fehlen %s." % human_size(need - free))


def _probe_setup(plan: dict, path: str, *, folder: str | None, games_root: str, reserve_bytes: int,
                 statvfs, mounts) -> dict:
    """E114: Plan für ein Installationsprogramm als Datei (setup.exe samt .bin daneben, .msi). Nur lesend.

    Bekannt (Inno Setup, NSIS, MSI): kind "windows-installer", installer_type, title aus dem Dateinamen,
    setup_files, setup_bytes, installed_bytes (Schätzung), Ziel im Spiele-Ordner. Sonst nicht unterstützt:
    ein unbekannter Installer (der Name sagt „Setup“) oder ein gewöhnliches Programm (kind "windows-exe";
    das startet man mit „Starten“, E93). Die Datei selbst bleibt liegen."""
    itype = setupexe.installer_type(path)
    plan.update(platform="windows" if itype else None, password="none", needs_password=False, issues=[], paths_safe=True,
                layout=None, top_folder=None, candidates=[], excluded=[], exe=None, needs_choice=False, system=None,
                rom_layout=None, rom_items=[], stray=[], unpacked_bytes=None, installer_type=itype or None,
                title=setupexe.title_of(path))
    need = 0
    if itype:
        found = setupexe.estimate(path)
        plan.update(kind="windows-installer", kind_label=SETUP_LABEL % setupexe.TYPE_LABELS[itype],
                    files=len(found["files"]), setup_files=found["files"], setup_bytes=found["setup_bytes"],
                    installed_bytes=found["installed_bytes"])
        need = found["installed_bytes"]
    else:
        named = setupexe.is_setup_name(plan["name"])
        plan["kind"] = "windows-installer" if named else "windows-exe"
        plan["kind_label"] = KIND_LABELS[plan["kind"]]
        plan["files"] = 1
        _problem(plan, "unsupported", setupexe.MSG_UNKNOWN if named else setupexe.MSG_NOT_SETUP)
    content = {"kind": plan["kind"] if itype else "unknown", "title": plan["title"]}
    _target_and_space(plan, content, folder, games_root, None, need + int(reserve_bytes), int(reserve_bytes),
                      statvfs, mounts)
    return _finish(plan)
