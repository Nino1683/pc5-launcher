"""Quelle "lutris" (E134): die Spiele, die der PC5 Launcher einmal aus Lutris übernommen hat.

Lutris wird nicht mehr gebraucht (Wunsch 2026-10-27): Beim ersten Einlesen nach dem Update bzw. nach der ersten
Installation liest library/lutris_import.py Lutris genau einmal (pga.db, YAML, Bilder). Danach liest der Launcher
weder pga.db noch Lutris' YAML noch dessen Bilderordner je wieder, auch nicht, wenn dort später Spiele dazukommen.
Gestartet wurden diese Spiele schon vorher ohne Lutris (E10: umu und Proton, der Prefix des Spiels an Ort und
Stelle oder einmal kopiert, launch/prefix.py).

Der Name der Quelle bleibt "lutris" und die IDs bleiben "lutris:<slug>" bzw. "lutris:<slug>~<zeile>": Spielzeit,
Optionen und alles andere in library.json "meta", Reihenfolge im Home, Favoriten, kopierte Prefixe (Ordner nach dem
Slug), Spielstand-Sicherungen, Bilder, Rückblick und Tastenbelegungen hängen an der ID und bleiben ohne Umschreiben
dran; library-cache.json und seen_sources sehen dieselbe Quelle, nichts zählt als neu installiert.

load(ctx), blockierend wie jede Quelle, wirft nicht:
  erledigt (<state_dir>/lutris-import.json)
      -> Schnappschuss aus library.json, Abschnitt "imported" -> "lutris" (nur gelesen; library.json schreibt
         allein der LibraryIndex). Fehlt er dort (library.json war kaputt und begann neu), gilt die Sicherung
         in der Zustandsdatei, und der Index bekommt ihn wieder (ctx.keep_imported).
  noch nicht erledigt
      -> lutris_import.read(ctx): keine Datenbank = erledigt mit 0 Spielen, für immer. Datenbank gesperrt oder
         kaputt = nicht erledigt, ctx.fail("lutris") (der letzte Stand der Bibliothek bleibt), Warnung, beim
         nächsten Start wieder. Gelesen: Bilder einmal nach <data_dir>/lutris-import/<slug>/ kopieren und den
         Schnappschuss über ctx.keep_imported an den Index geben. Erst wenn er in library.json auf der Platte
         steht, schreibt finish() die Zustandsdatei (stirbt das Home dazwischen, wird beim nächsten Start noch
         einmal übernommen, mit demselben Ergebnis).
      Ohne ctx.keep_imported (Befehlszeile, Startplan, Prüfungen der Quellen) wird nur gelesen: nichts kopiert,
      nichts gemerkt.
  Jeder Lauf frischt die Einträge auf wie bei eigenen Spielen: available (Startdatei da), FSR-Scan des
  Spielordners, Marken vr und battlenet, Zustand des Prefix. needs-steam (E25) und „zuletzt gespielt“ bzw.
  Favoriten aus Steams Verknüpfungen ergänzt sources.run_source wie bisher.

Zustandsdatei: {"version": 1, "done": true, "time": unix, "count": n, "ids": [...], "backup": [Einträge]}.

Hier liegen auch die Helfer, die andere Quellen brauchen (Spielordner einer exe, Emulator-Starter): lutris_import
lädt nur dieses Modul, nie umgekehrt außer in _take_over (SAFETY: nur die Übernahme liest Lutris).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from .. import log
from . import fsr as fsr_mod
from .model import Entry
from .sources import err_text, title_key

_log = log.get("library.import")

SOURCE = "lutris"                       # Name der Quelle (Register, library-cache.json, seen_sources)
STATE_FILE = "lutris-import.json"       # in <state_dir>
ART_DIR = "lutris-import"               # in <data_dir>: die einmal kopierten Bilder, je Slug ein Ordner
LIBRARY_FILE = "library.json"
SECTION = "imported"

# Lutris-Einträge, die nur eigene Emulator-Starter des Nutzers aufrufen: Diese Spiele liefert die Emulator-Quelle.
EMULATOR_STARTERS = {"eden-launch", "cemu-launch"}
NEEDS_STEAM = {"rocksmith-launch"}

# Unterordner, in denen die exe liegt, während das Spiel weiter oben beginnt (bin/x64, Binaries/Win64)
_BIN_DIRS = {"binaries", "bin", "bin64", "bin32", "win64", "win32", "x64", "x86", "x86_64", "wingdk", "retail"}

# Reine VR-Titel ohne Bildschirm-Modus, deren Name das nicht verrät (Vergleich über title_key).
KNOWN_VR_ONLY = {
    "beatsaber", "halflifealyx", "gunmancontracts", "boneworks", "bonelab", "bladeandsorcery",
    "pistolwhip", "jobsimulator", "thewalkingdeadsaintssinners", "intotheradius", "pavlovvr",
}
_VR_WORD = re.compile(r"(?<![A-Za-z0-9])VR(?![A-Za-z0-9])", re.I)
# Marken, die jeder Lauf neu bestimmt (die übrigen, z. B. needs-steam, kommen aus dem Schnappschuss)
_FRESH_TAGS = {"fsr4", "fsr4-upgrade", "vr", "battlenet"}

_finish_lock = threading.Lock()


# ---------------------------------------------------------------- Helfer (auch für andere Quellen)

def prefix_state(prefix: str) -> str:
    """Zustand eines Wine-Prefix (03, Abschnitt 4.2). Nur lstat/readlink im Prefix des Spiels, schnell.

    "missing"  Ordner fehlt oder kein Prefix eingetragen
    "empty"    Ordner da, aber nie gestartet (keine system.reg)
    "proton"   Protons Marke (Datei version oder tracked_files), DXVK von Proton
    "mixed"    Protons Marke, aber system32/dxgi.dll ist ein Verweis in Lutris' DXVK (ein wine-staging-Start
               hat Protons Dateien ersetzt; gelesen wird nur das Ziel des Verweises, nie Lutris' Datei)
    "wine"     kein Proton-Zeichen (Lutris-Wine, z. B. wine-staging)
    """
    if not prefix or not os.path.isdir(prefix):
        return "missing"
    # Lutris nennt den Ordner mit drive_c; Steam-artige Prefixe haben darunter noch pfx/.
    roots = [prefix, os.path.join(prefix, "pfx")]
    if not any(os.path.isfile(os.path.join(r, "system.reg")) for r in roots):
        return "empty"
    proton_mark = False
    for name in ("version", "tracked_files"):
        try:
            proton_mark = proton_mark or os.path.getsize(os.path.join(prefix, name)) > 0
        except OSError:
            pass
    lutris_dxvk = False
    for r in roots:
        dxgi = os.path.join(r, "drive_c/windows/system32/dxgi.dll")
        try:
            if os.path.islink(dxgi) and "/lutris/runtime/" in os.readlink(dxgi).replace("\\", "/"):
                lutris_dxvk = True
        except OSError:
            pass
    if proton_mark:
        return "mixed" if lutris_dxvk else "proton"
    return "wine"


def game_root(exe: str, directory: str = "", mounts=None, home: str = "") -> str:
    """Spielordner zu einer exe (das ist der Ordner, den der FSR-Scan durchsucht).

    Ausgang ist der Ordner der exe. Heißt er wie ein Programm-Unterordner (bin/x64,
    Binaries/Win64), geht es bis zu drei Ebenen hoch; bei Unreal-Spielen (Ordner Engine neben dem
    Projektordner) noch eine. Nie bis auf einen Einhängepunkt oder das Home. Liegt "directory"
    (bei übernommenen Spielen der Spielordner aus Lutris, bei Verknüpfungen der Startordner) darüber,
    gilt das.
    """
    mounts = mounts or {}
    home_real = os.path.realpath(home) if home else ""

    def forbidden(path: str) -> bool:
        real = os.path.realpath(path)
        return not path or real == "/" or real in mounts or (home_real and real == home_real)

    base = os.path.dirname(exe)
    cur = base
    for _ in range(3):
        parent = os.path.dirname(cur)
        if os.path.basename(cur).lower() in _BIN_DIRS and not forbidden(parent):
            cur = parent
        else:
            break
    parent = os.path.dirname(cur)
    if cur != base and not forbidden(parent) and os.path.isdir(os.path.join(parent, "Engine")):
        cur = parent
    if directory and not forbidden(directory):
        d = os.path.realpath(directory)
        c = os.path.realpath(cur)
        if c != d and c.startswith(d.rstrip("/") + "/"):
            cur = directory
    return cur


def is_vr_title(title: str, slug: str, root: str, scan: dict | None) -> bool:
    """Vorsichtige VR-Erkennung: lieber keine Marke als eine falsche.

    Bedingung 1: Im Spielordner liegt eine VR-Laufzeit (openvr_api.dll, OVRPlugin.dll,
    openxr_loader.dll …) oder der Ordner game/hlvr (Source 2 VR). Das allein reicht nicht:
    Cyberpunk 2077, Resident Evil Village, Superliminal und jedes Unreal-Spiel bringen solche
    DLLs mit, ohne VR-Spiele zu sein.
    Bedingung 2: Der Titel ist erkennbar kein Bildschirmspiel: game/hlvr vorhanden, oder „VR“
    steht als eigenes Wort im Titel, im Slug oder im Namen des Spielordners, oder der Titel steht
    in der kleinen Liste KNOWN_VR_ONLY.
    """
    files = (scan or {}).get("files") or {}
    if files.get("vr_marker"):
        return True
    if not files.get("vr"):
        return False
    words = "%s %s %s" % (title, (slug or "").replace("-", " "), os.path.basename(root.rstrip("/")))
    return bool(_VR_WORD.search(words)) or title_key(title) in KNOWN_VR_ONLY


def art_folder(slug: str) -> str:
    """Ordnername der kopierten Bilder eines Slugs unter <data_dir>/lutris-import: nur harmlose Zeichen."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", str(slug or "")).strip("-.")
    return name or "spiel"


# ---------------------------------------------------------------- Zustand

def state_path(ctx) -> str:
    return os.path.join(ctx.state_dir, STATE_FILE) if getattr(ctx, "state_dir", None) else ""


def read_state(ctx) -> dict | None:
    """Die Zustandsdatei, wenn die Übernahme erledigt ist; sonst None (fehlt, kaputt, nicht erledigt).
    Eine kaputte Datei zählt als „nicht erledigt“: Dann wird noch einmal übernommen, das schadet nicht."""
    path = state_path(ctx)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        ctx.warn("lutris: %s nicht lesbar (%s), die Übernahme läuft noch einmal" % (STATE_FILE, err_text(e)))
        return None
    return data if isinstance(data, dict) and data.get("done") is True else None


def write_state(ctx, records: list) -> None:
    """Übernahme als erledigt vermerken (atomar, im eigenen Zustandsordner). Wirft OSError."""
    from .. import jsontext, system
    data = {"version": 1, "done": True, "time": round(time.time(), 1), "count": len(records),
            "ids": [r.get("id") for r in records], "backup": records}
    system.write_atomic(state_path(ctx), jsontext.dumps(data, indent=1, sort_keys=True) + "\n")


def _usable(records) -> list | None:
    """Liste von Einträgen als Objekt (id und title Text), sonst None."""
    if not isinstance(records, list):
        return None
    return [r for r in records if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]
            and isinstance(r.get("title"), str)]


def snapshot(ctx) -> list | None:
    """Der Schnappschuss aus library.json (Abschnitt "imported" -> "lutris"), nur gelesen. None, wenn die Datei
    oder der Abschnitt fehlt bzw. nicht lesbar ist. Gelesen wird nur bei geänderter Datei (mtime, Größe)."""
    path = os.path.join(ctx.data_dir, LIBRARY_FILE) if getattr(ctx, "data_dir", None) else ""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    memo = getattr(ctx, "_imported_memo", None)
    if memo and memo[0] == (path, key):
        return memo[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        ctx.warn("lutris: %s nicht lesbar (%s)" % (LIBRARY_FILE, err_text(e)))
        return None
    section = data.get(SECTION) if isinstance(data, dict) else None
    records = _usable(section.get(SOURCE)) if isinstance(section, dict) else None
    try:
        ctx._imported_memo = ((path, key), records)
    except AttributeError:
        pass
    return records


def finish(ctx, records: list, flushed: bool = True) -> bool:
    """Nach dem Speichern des Schnappschusses (der Index hat library.json geschrieben): Steht er wirklich auf der
    Platte, wird die Übernahme vermerkt. True nur beim ersten Mal (dann kommt die Mitteilung); False, wenn sie
    schon vermerkt war oder library.json den Abschnitt nicht hat (dann gilt sie beim nächsten Start als offen)."""
    # Läuft nach dem Lauf der Quelle (Rückruf des Index): Warnungen deshalb ins Protokoll, nicht in ctx.warnings
    if not flushed:
        _log.warning("Übernahme aus Lutris: library.json ist noch nicht geschrieben, sie bleibt offen")
        return False
    with _finish_lock:
        if read_state(ctx) is not None:
            return False
        on_disk = snapshot(ctx) or []
        have = {r["id"] for r in on_disk}
        if any(r.get("id") not in have for r in records):
            _log.warning("Übernahme aus Lutris: Die Spiele stehen nicht in library.json; sie läuft beim nächsten "
                         "Start noch einmal")
            return False
        try:
            write_state(ctx, records)
        except OSError as e:
            _log.warning("Übernahme aus Lutris: %s nicht schreibbar (%s); sie läuft beim nächsten Start noch "
                         "einmal", STATE_FILE, err_text(e))
            return False
    ctx._pending_import = None
    _log.info("Übernahme aus Lutris erledigt: %d Spiele, Lutris wird nicht mehr gelesen", len(records))
    return True


# ---------------------------------------------------------------- Quelle

def load(ctx) -> list[Entry]:
    """Die übernommenen Spiele als Einträge. Blockierend; wirft nicht, Probleme stehen in ctx.warnings."""
    state = read_state(ctx)
    if state is not None:
        records = snapshot(ctx)
        if records is None:
            # library.json ohne den Abschnitt (kaputt und neu begonnen): die Sicherung der Zustandsdatei, und
            # der Index bekommt sie wieder. Lutris wird nicht noch einmal gelesen
            records = _usable(state.get("backup")) or []
            keep = getattr(ctx, "keep_imported", None)
            if records and keep is not None:
                ctx.warn("lutris: library.json kennt die übernommenen Spiele nicht mehr, sie kommen aus %s"
                         % STATE_FILE)
                keep(SOURCE, records, None)
    else:
        records = _take_over(ctx)
        if records is None:
            return []
    out: list[Entry] = []
    for record in records:
        try:
            out.append(refresh(ctx, record))
        except Exception as e:
            ctx.warn("lutris: %s ausgelassen (%s)" % (record.get("title") or record.get("id"), err_text(e)))
    return out


def _take_over(ctx) -> list | None:
    """Die Übernahme (einmal je Rechner): Lutris lesen, Bilder kopieren, dem Index geben. None = gescheitert."""
    pending = getattr(ctx, "_pending_import", None)
    if pending is not None:
        return pending                      # in diesem Lauf schon gelesen, der Index schreibt noch
    from . import lutris_import             # die einzige Stelle, an der Lutris gelesen wird
    status, entries = lutris_import.read(ctx)
    if status == "failed":
        return None
    from .index import entry_to_dict
    records = [entry_to_dict(e) for e in entries]
    keep = getattr(ctx, "keep_imported", None)
    if keep is None:
        return records                      # nur lesen: Befehlszeile, Startplan, Prüfungen der Quellen
    if status == "none" or not records:
        try:
            write_state(ctx, [])            # kein Lutris (bzw. keine Spiele): erledigt, für immer
        except OSError as e:
            ctx.warn("lutris: %s nicht schreibbar (%s)" % (STATE_FILE, err_text(e)))
        return []
    lutris_import.copy_art(ctx, records)
    ctx._pending_import = records
    # Für die Mitteilung zählt, was die Bibliothek zeigt: needs-steam-Einträge mit Steam-Spiel entfallen (E25);
    # vorher aus der Liste entfernte Spiele (E91) zieht die Verdrahtung ab, sie kennt library.json "meta"
    try:
        from .sources import _drop_needs_steam
        shown = [e.id for e in _drop_needs_steam(ctx, list(entries))]
    except Exception:
        shown = [r["id"] for r in records]
    keep(SOURCE, records, lambda flushed: finish(ctx, records, flushed), shown)
    return records


def refresh(ctx, record: dict) -> Entry:
    """Ein Eintrag des Schnappschusses, aufgefrischt wie ein eigenes Spiel bei jedem Einlesen."""
    from .index import entry_from_dict
    entry = entry_from_dict(record)                     # tiefe Kopie, der Schnappschuss bleibt unverändert
    d = entry.detail
    exe = d.get("exe") if isinstance(d.get("exe"), str) else ""
    entry.available = bool(exe) and os.path.isfile(exe)
    entry.installed = True
    if entry.source != "proton":
        return entry                                    # Linux-Spiele: Marken (needs-steam) wie übernommen
    root = d.get("game_dir") if isinstance(d.get("game_dir"), str) and d.get("game_dir") else ""
    if not root and exe:
        root = game_root(exe, "", ctx.fsr.mounts(), ctx.home)
        d["game_dir"] = root
    # Fehlt die exe (Platte weg), liefert der Scanner sein letztes bekanntes Ergebnis oder "missing"
    scan = ctx.fsr.scan(root) if root else None
    tags = fsr_mod.tags_for(scan)
    if is_vr_title(entry.title, str(d.get("slug") or ""), root, scan):
        tags.append("vr")
    if d.get("store") == "battlenet":
        tags.append("battlenet")
    entry.tags = tags + [t for t in entry.tags if t not in _FRESH_TAGS and t not in tags]
    d["fsr"] = fsr_mod.summary(scan)
    d["prefix_state"] = prefix_state(str(d.get("prefix") or ""))
    return entry
