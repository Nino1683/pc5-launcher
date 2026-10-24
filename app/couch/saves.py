"""Spielstände eines Eintrags (Bauplan E63): Ordner finden, sichern, wiederherstellen.

Wunsch 2026-10-18: „Ich möchte auch die Savegames von einem Spieleintrag sichern können. Und mir
anzeigen lassen, in welchem Ordner diese liegen. Also den ganzen Pfad.“

Windows-Spiele speichern im Prefix (Proton/Wine) unter drive_c/users/<name>/… an je nach Spiel
anderer Stelle. find() sucht an den üblichen Orten (AppData Local/LocalLow/Roaming, Documents,
Documents/My Games, Saved Games, Unterordner „Save…“ im Spielordner) und bewertet jeden Ordner:
Wörter des Titels im Pfad, Speicherdateien darin, Größe, jüngste Änderung. Linux-Spiele: im
Home-Ordner (~/.local/share, ~/.config), nur Ordner mit einem Wort des Titels. Steam-Spiele: dazu
userdata/<Konto>/<AppID> (Steam Cloud). Der beste mit genug Punkten gilt, wenn im Eintrag nichts
anderes gewählt ist (Option "saves").

Emulator-Spiele (E99): Eden legt Stände unter <nand>/user/save/0000000000000000/<Profil>/<TitleID>
ab, Cemu unter <mlc01>/usr/save/<TitleID oben>/<TitleID unten>. Beides wird nur gelesen (Einstellungen,
launched.json, die Ordner); die TitleID kommt aus dem Eintrag oder wird nach einer Sitzung gelernt.

Sicherungen: ZIP-Dateien in ~/Games/backups/couch/<Titel>/<Datum>.zip mit couch-sicherung.json
(Eintrag, Titel, Ordner). Automatische Sicherungen nach dem Spielen heißen „auto-<Datum>“ (E99).
Wiederherstellen packt die Sicherung erst vollständig in einen Zwischenordner aus, legt dann eine
Sicherung des jetzigen Stands an, den Ordner in den Papierkorb (nie endgültig gelöscht) und setzt
den ausgepackten Stand an seine Stelle.
Nur Standardbibliothek, bis auf Gio für den Papierkorb (files.gio_trash).
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile
import zlib

from . import jsontext, log, paths

_log = log.get("saves")

MANIFEST = "couch-sicherung.json"
MIN_SCORE = 6
SCAN_FILES = 600                 # je Ordner höchstens so viele Dateien ansehen
LOCATIONS = (("AppData/Local", 2), ("AppData/LocalLow", 2), ("AppData/Roaming", 2), ("Documents/My Games", 1),
             ("Documents", 2), ("Saved Games", 1))
NOISE = {"microsoft", "temp", "tmp", "cache", "caches", "crashdumps", "d3dscache", "nvidia", "nvidia corporation",
         "amd", "openvr", "vulkan", "packages", "unrealengine", "steam", "valve", "programs", "history",
         "mesa_shader_cache", "fontconfig", "pip", "gtk-3.0", "my games", "documents", "desktop", "downloads",
         "pictures", "music", "videos", "templates", "favorites", "links", "contacts", "searches",
         "application data", "local settings", "start menu", "recent", "sendto", "nethood", "printhood",
         "cookies", "temporary internet files", "comms", "connecteddevicesplatform", "epicgameslauncher",
         "ubisoft game launcher", "electronic arts", "ea desktop", "origin", "gog.com", "battle.net", "blizzard entertainment",
         "unity", "squirreltemp", "crashreportclient", "ms-playwright", "nuget", "wine_gecko", "openal"}
STOP = {"the", "and", "edition", "remastered", "remaster", "definitive", "complete", "game", "goty", "deluxe",
        "ultimate", "enhanced", "directors", "cut", "collection", "of", "der", "die", "das", "und"}
SAVE_NAME = re.compile(r"(sav|save|slot|profile|progress|checkpoint|\.dat$|\.sl2$|\.json$)", re.I)
SAVE_DIRS = {"saves", "save", "savegames", "savegame", "saved", "savedata", "storage"}
# Linux-Spiele: im Home-Ordner; dort zählt ein Ordner nur, wenn ein Wort des Titels im Pfad steht
HOME_LOCATIONS = ((".local/share", 2), (".config", 2), (".config/unity3d", 2))
RECENT = 180 * 86400             # in dieser Zeit geändert: ein Punkt mehr
AUTO_PREFIX = "auto-"            # Name automatischer Sicherungen (E99)
SAFETY_NOTE = "vor-wiederherstellung"
TEXT_BROKEN = "Die Sicherung ist beschädigt."
TEXT_NOT_OURS = "Diese ZIP-Datei ist keine Sicherung des PC5 Launchers."
TEXT_FAILED = "Das hat nicht geklappt. Einzelheiten stehen im Protokoll."
TEXT_NO_SPACE = "Auf dem Laufwerk ist kein Platz mehr frei."
TEXT_LEFTOVER = "Der ausgepackte Stand liegt noch im versteckten Ordner „%s“ daneben."
STALE_PART_S = 86400             # halbe ZIP von Hand („*.zip.neu“), so alt: übrig von einem Absturz
# Was zipfile beim Lesen einer kaputten oder fremden ZIP wirft (Bitfehler, halb kopiert, gepackt
# mit einem Verfahren, das zipfile nicht kennt): wird zu TEXT_BROKEN
_ZIP_ERRORS = (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, NotImplementedError)


def reason(e: BaseException) -> str:
    """Deutscher Satz für die Seite und die Mitteilung (E99). ValueError tragen hier schon einen;
    OSError nie den englischen Text des Systems mit technischem Pfad, Einzelheiten ins Protokoll."""
    if isinstance(e, ValueError):
        return str(e)
    if isinstance(e, OSError):
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            return TEXT_NO_SPACE
        if e.errno == errno.EROFS:
            return "Das Laufwerk ist schreibgeschützt."
        if e.errno in (errno.EACCES, errno.EPERM):
            return "Dafür fehlen dem PC5 Launcher die Rechte."
    return TEXT_FAILED


def tokens(title: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (title or "").lower().replace("ß", "ss"))
    return [w for w in words if len(w) >= 3 and w not in STOP]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def user_dirs(prefix: str) -> list[str]:
    base = os.path.join(prefix, "drive_c", "users")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    # „Public“ zuletzt: Manche Spiele speichern unter C:\Users\Public\Documents
    names.sort(key=lambda n: n.lower() == "public")
    return [os.path.join(base, n) for n in names if os.path.isdir(os.path.join(base, n))
            and not os.path.islink(os.path.join(base, n))]


def _children(path: str) -> list[str]:
    try:
        return [os.path.join(path, n) for n in sorted(os.listdir(path))
                if os.path.isdir(os.path.join(path, n)) and not os.path.islink(os.path.join(path, n))]
    except OSError:
        return []


def stats(path: str) -> dict:
    """Dateien, Bytes, jüngste Änderung, Speicherdateien (höchstens SCAN_FILES Dateien, ohne Verweise)."""
    files = size = hits = 0
    newest = 0.0
    save_dir = False
    for base, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        if any(d.lower() in SAVE_DIRS for d in dirs) or os.path.basename(base).lower() in SAVE_DIRS:
            save_dir = True
        for n in names:
            try:
                st = os.lstat(os.path.join(base, n))
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
            if SAVE_NAME.search(n):
                hits += 1
            if files >= SCAN_FILES:
                return {"files": files, "size": size, "mtime": newest, "hits": hits, "save_dir": save_dir}
    return {"files": files, "size": size, "mtime": newest, "hits": hits, "save_dir": save_dir}


def contents(path: str) -> dict:
    """Alle Dateien eines Ordners ohne Obergrenze (ohne Verweise): {"files", "size", "mtime"}.
    Für die automatische Sicherung (E99): Ob sich seit der letzten Sicherung etwas geändert hat
    und wie groß sie wird, muss für den ganzen Ordner stimmen, nicht nur für die ersten Dateien."""
    files = size = 0
    newest = 0.0
    for base, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        for n in names:
            try:
                st = os.lstat(os.path.join(base, n))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                files += 1
                size += st.st_size
                newest = max(newest, st.st_mtime)
    return {"files": files, "size": size, "mtime": newest}


def score(path: str, rel: str, title_words: list[str], info: dict, now: float | None = None) -> int:
    rel_norm = _norm(rel)
    points = 0
    for w in title_words:
        if w in rel_norm:
            points += 5
    if title_words and _norm("".join(title_words)) in rel_norm:
        points += 3
    if info["hits"]:
        points += 4
    if info["save_dir"]:
        points += 2
    if info["mtime"] and (now if now is not None else time.time()) - info["mtime"] < RECENT:
        points += 1
    if info["files"] == 0:
        points -= 6
    elif info["size"] < 512:
        points -= 2
    return points


def find(title: str, prefix: str = "", game_dir: str = "", home: str = "",
         extra: list | None = None) -> list[dict]:
    """Kandidaten [{path, score, files, size, mtime}], bester zuerst.

    prefix  Wine-/Proton-Prefix (Windows-Spiele)
    home    Home-Ordner (Linux-Spiele: ~/.local/share, ~/.config)
    extra   [(Pfad, Punkte)]: Orte, die die Quelle kennt (Steam: userdata/<Konto>/<AppID>)"""
    words = tokens(title)
    seen: set[str] = set()
    out: list[dict] = []

    def consider(path: str, rel: str, bonus: int = 0, need_word: bool = False) -> None:
        real = os.path.realpath(path)
        if real in seen:
            return
        if need_word and not any(w in _norm(rel) for w in words):
            return
        seen.add(real)
        info = stats(path)
        pts = score(path, rel, words, info) + bonus
        if pts > 0 and info["files"]:
            out.append({"path": path, "score": pts, "files": info["files"], "size": info["size"],
                        "mtime": int(info["mtime"])})

    for user in user_dirs(prefix) if prefix else []:
        for loc, depth in LOCATIONS:
            base = os.path.join(user, loc)
            for child in _children(base):
                if os.path.basename(child).lower() in NOISE:
                    continue
                rel = os.path.relpath(child, user)
                consider(child, rel)
                if depth >= 2:
                    for grand in _children(child):
                        if os.path.basename(grand).lower() in NOISE:
                            continue
                        consider(grand, os.path.relpath(grand, user), bonus=1)
    if home:
        for loc, depth in HOME_LOCATIONS:
            for child in _children(os.path.join(home, loc)):
                if os.path.basename(child).lower() in NOISE:
                    continue
                consider(child, os.path.relpath(child, home), need_word=True)
                if depth >= 2:
                    for grand in _children(child):
                        consider(grand, os.path.relpath(grand, home), bonus=1, need_word=True)
    if game_dir and os.path.isdir(game_dir):
        for child in _children(game_dir):
            if os.path.basename(child).lower() in SAVE_DIRS:
                consider(child, os.path.basename(child), bonus=3)
    for path, bonus in extra or []:
        if os.path.isdir(path):
            consider(path, "", bonus=bonus)
    # Ein Ober- und ein Unterordner mit gleicher Aussage: der genauere (Unterordner) gewinnt nur,
    # wenn er mehr Punkte hat; sonst bleibt der übergeordnete (er enthält alles). Gleich lange
    # Pfade (Edens Profile, E99): der zuletzt geänderte
    out.sort(key=lambda c: (-c["score"], len(c["path"]), -c["mtime"]))
    return out[:12]


def best(candidates: list[dict], chosen: str = "") -> dict | None:
    if chosen:
        hit = next((c for c in candidates if c["path"] == chosen), None)
        if hit:
            return hit
        if os.path.isdir(chosen):
            info = stats(chosen)
            return {"path": chosen, "score": 0, "files": info["files"], "size": info["size"], "mtime": int(info["mtime"])}
    top = candidates[0] if candidates else None
    return top if top and top["score"] >= MIN_SCORE else None


# ---------------------------------------------------------------- Emulatoren (E99)
# Je Ort: (Einstellungsdatei, Vorgabe für den Ordner, launched.json). Zuerst das Flatpak (so
# starten eden-launch und cemu-launch), dann die AppImage-Fassung von EmuDeck. Nur gelesen.
EDEN_PLACES = ((".var/app/dev.eden_emu.eden/config/eden/qt-config.ini", ".var/app/dev.eden_emu.eden/data/eden/nand",
                ".var/app/dev.eden_emu.eden/cache/eden/launched.json"),
               (".config/eden/qt-config.ini", ".local/share/eden/nand", ".cache/eden/launched.json"))
CEMU_PLACES = ((".var/app/info.cemu.Cemu/config/Cemu/settings.xml", ".var/app/info.cemu.Cemu/data/Cemu/mlc01"),
               (".config/Cemu/settings.xml", ".local/share/Cemu/mlc01"))
# Ordner der Emulatoren selbst (Einstellungen, Schlüssel, installierte Spiele): Dorthin und in jeden
# Ordner, der einen davon enthält, holt restore() nie etwas zurück (SAFETY 13, E99)
EMU_APP_DIRS = (".var/app/dev.eden_emu.eden", ".config/eden", ".local/share/eden", ".cache/eden",
                ".var/app/info.cemu.Cemu", ".config/Cemu", ".local/share/Cemu", ".cache/Cemu")
EDEN_SAVE_USER = "0000000000000000"          # Speicherbereich der Spielstände (Benutzer-Saves)
SWITCH_ID = re.compile(r"^01[0-9A-F]{14}$")
WIIU_ID = re.compile(r"^[0-9A-F]{16}$")
LEARN_SLACK_S = 10.0                         # Uhr von Start und Ende: so viel Spiel nach beiden Seiten


def _read_text(path: str, limit: int = 1 << 20) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _unique_dirs(candidates: list[str], existing: bool = True) -> list[str]:
    out, seen = [], set()
    for path in candidates:
        if not path or (existing and not os.path.isdir(path)):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            out.append(path)
    return out


def eden_nands(home: str, existing: bool = True) -> list[str]:
    """NAND-Ordner von Eden: nand_directory aus qt-config.ini, sonst die Vorgabe.
    existing=False: auch die, die es (gerade) nicht gibt (für die Sperre beim Zurückholen)."""
    found = []
    for config, default, _launched in EDEN_PLACES:
        m = re.search(r"^nand_directory=(.*)$", _read_text(os.path.join(home, config)), re.M)
        value = m.group(1).strip().strip('"') if m else ""
        found.append(os.path.expanduser(value) if value else os.path.join(home, default))
        if value and not existing:
            found.append(os.path.join(home, default))   # die Vorgabe bleibt Emulator-Ort
    return _unique_dirs(found, existing)


def cemu_mlcs(home: str, existing: bool = True) -> list[str]:
    """mlc01-Ordner von Cemu: mlc_path aus settings.xml, sonst die Vorgabe (existing wie oben)."""
    found = []
    for config, default in CEMU_PLACES:
        m = re.search(r"<mlc_path>([^<]*)</mlc_path>", _read_text(os.path.join(home, config)))
        value = m.group(1).strip() if m else ""
        found.append(os.path.expanduser(value) if value else os.path.join(home, default))
        if value and not existing:
            found.append(os.path.join(home, default))   # die Vorgabe bleibt Emulator-Ort
    return _unique_dirs(found, existing)


def emulator_save_roots(home: str) -> list[str]:
    """Ordner, unter denen Eden und Cemu Spielstände ablegen (auch die, die es noch nicht gibt)."""
    return ([os.path.join(n, "user", "save") for n in eden_nands(home, existing=False)]
            + [os.path.join(m, "usr", "save") for m in cemu_mlcs(home, existing=False)])


def emulator_data_roots(home: str) -> list[str]:
    """Alle Datenorte von Eden und Cemu: NAND, mlc01 (aus Einstellung und Vorgabe, ob es sie gibt
    oder nicht) und die App-Ordner. Für die Sperre beim Zurückholen (E99)."""
    return (eden_nands(home, existing=False) + cemu_mlcs(home, existing=False)
            + [os.path.join(home, d) for d in EMU_APP_DIRS])


def _rel_parts(path: str, root: str) -> list[str] | None:
    full, base = os.path.realpath(path), os.path.realpath(root).rstrip(os.sep)
    if not full.startswith(base + os.sep):
        return None
    return full[len(base) + 1:].split(os.sep)


def emulator_title_dir(folder: str, home: str) -> bool:
    """Ist folder genau der Spielstand-Ordner eines Spiels (Eden: …/user/save/0000000000000000/
    <Profil>/<TitleID>, Cemu: …/usr/save/<oben>/<unten>), kein Ordner darüber? Nur die Form zählt,
    der Ordner muss nicht bestehen."""
    for nand in eden_nands(home, existing=False):
        parts = _rel_parts(folder, os.path.join(nand, "user", "save"))
        if parts is not None:
            return len(parts) == 3 and parts[0] == EDEN_SAVE_USER and bool(SWITCH_ID.match(parts[2].upper()))
    for mlc in cemu_mlcs(home, existing=False):
        parts = _rel_parts(folder, os.path.join(mlc, "usr", "save"))
        if parts is not None:
            return len(parts) == 2 and bool(WIIU_ID.match((parts[0] + parts[1]).upper()))
    return False


def title_dirs(system: str, home: str) -> list[tuple[str, str]]:
    """[(TitleID, Ordner)] aller Spielstand-Ordner eines Systems; Eden je Profil einer."""
    out: list[tuple[str, str]] = []
    if system == "switch":
        for nand in eden_nands(home):
            for profile in _children(os.path.join(nand, "user", "save", EDEN_SAVE_USER)):
                for folder in _children(profile):
                    tid = os.path.basename(folder).upper()
                    if SWITCH_ID.match(tid):
                        out.append((tid, folder))
    elif system == "wiiu":
        for mlc in cemu_mlcs(home):
            for high in _children(os.path.join(mlc, "usr", "save")):
                for folder in _children(high):
                    tid = (os.path.basename(high) + os.path.basename(folder)).upper()
                    if WIIU_ID.match(tid):
                        out.append((tid, folder))
    return out


def emulator_folders(system: str, home: str, title_id: str) -> list[str]:
    tid = (title_id or "").upper()
    return [folder for t, folder in title_dirs(system, home) if t == tid] if tid else []


def eden_launched(home: str) -> dict[str, float]:
    """{TitleID: Uhrzeit des letzten Starts} aus Edens launched.json (nur gelesen)."""
    out: dict[str, float] = {}
    for _config, _default, launched in EDEN_PLACES:
        try:
            data = json.loads(_read_text(os.path.join(home, launched)) or "{}")
        except ValueError:
            continue
        for tid, item in (data.items() if isinstance(data, dict) else []):
            stamp = item.get("timestamp") if isinstance(item, dict) else None
            if isinstance(stamp, (int, float)) and SWITCH_ID.match(str(tid).upper()):
                out[str(tid).upper()] = max(float(stamp), out.get(str(tid).upper(), 0.0))
    return out


def learn_title(system: str, home: str, started: float, ended: float) -> str:
    """TitleID des Spiels, das zwischen started und ended lief, "" = nicht eindeutig.

    Für Spiele ohne TitleID im Dateinamen (die Kopfdaten einer XCI sind verschlüsselt, prod.keys
    fasst der PC5 Launcher nicht an). Hinweise: Spielstand-Ordner, in denen sich in der Zeit etwas
    geändert hat, und bei Eden die Startzeit aus launched.json. Gilt nur, was genau einen Treffer
    ergibt; zwei Spiele in einer Sitzung lernen nichts. Beide Hinweise zählen nur innerhalb der
    Sitzung (auch nach oben begrenzt: ein später gespieltes Spiel zählt nicht), und widersprechen
    sie sich, wird nichts gelernt (E99)."""
    lo, hi = started - LEARN_SLACK_S, ended + LEARN_SLACK_S
    changed = {tid for tid, folder in title_dirs(system, home) if lo <= contents(folder)["mtime"] <= hi}
    launched = {tid for tid, at in eden_launched(home).items() if lo <= at <= hi} if system == "switch" else set()
    group = (changed & launched) if changed and launched else (changed or launched)
    return next(iter(group)) if len(group) == 1 else ""


# ---------------------------------------------------------------- Sicherungen
def backup_root() -> str:
    """~/Games/backups/couch. Im Prüfmodus <COUCH_TEST_ROOT>/home/Games/backups/couch (E99,
    paths.user_place): Seit der Automatik sichert ein Prüflauf nach jedem längeren Attrappen-Spiel;
    das landete sonst im echten Sicherungsordner des Nutzers."""
    return paths.user_place(os.path.join("Games", "backups", "couch"))


def safe_title(title: str) -> str:
    name = re.sub(r"[^\w .,()+-]+", "_", title or "Spiel", flags=re.U).strip(" .") or "Spiel"
    return name[:80]


def backup_dir(title: str) -> str:
    return os.path.join(backup_root(), safe_title(title))


def kind_of(name: str) -> str:
    """"auto" (nach dem Spielen, E99), "safety" (vor dem Zurückholen) oder "manual"."""
    if name.startswith(AUTO_PREFIX):
        return "auto"
    return "safety" if "_" + SAFETY_NOTE in name else "manual"


def list_backups(title: str) -> list[dict]:
    folder = backup_dir(title)
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    for n in names:
        if not n.endswith(".zip"):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": n[:-4], "path": p, "size": st.st_size, "created": int(st.st_mtime), "kind": kind_of(n)})
    # Neueste zuerst; in derselben Sekunde entscheidet der Name (Zeitstempel, „_2“)
    out.sort(key=lambda b: (b["created"], b["name"]), reverse=True)
    return out


def _arc_name(rel: str) -> str | None:
    """Ersatzname im ZIP für einen Pfad mit kaputt kodierten Bytes (Surrogate aus os.fsdecode,
    E80): ZIP-Namen sind UTF-8, solche Namen ließen die ganze Sicherung scheitern. None = taugt."""
    try:
        rel.encode("utf-8")
        return None
    except UnicodeEncodeError:
        return os.fsencode(rel).decode("utf-8", "backslashreplace")


def make_backup(title: str, entry_id: str, folder: str, note: str = "", clock=time.time,
                auto: bool = False, pause=None) -> dict:
    """ZIP des Ordners. Liefert {path, files, size}.

    auto   automatische Sicherung nach dem Spielen (Name „auto-…“, im Manifest "auto": true)
    pause  wird vor jeder Datei und einmal nach der letzten gerufen; hält an (solange ein Spiel
           vorn ist) oder wirft (Ende des Home, Neustart des Spiels): dann keine ZIP

    Dateinamen, die kein UTF-8 sind, stehen im ZIP unter einem Ersatznamen; die echten Bytes
    stehen im Manifest unter "raw" (Ersatzname -> Bytes als Hex), restore() nimmt sie zurück.

    Als Zeit der Sicherung gilt ihr Beginn (Manifest "created" und Änderungszeit der ZIP, E99):
    Was das Spiel schreibt, während die Sicherung noch läuft, ist dann neuer und löst die nächste
    aus, statt als schon gesichert zu gelten."""
    if not os.path.isdir(folder):
        raise ValueError("Den Spielstand-Ordner gibt es nicht (mehr).")
    target_dir = backup_dir(title)
    os.makedirs(target_dir, exist_ok=True)
    started = clock()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started))
    name = (AUTO_PREFIX if auto else "") + stamp + ("_" + note if note else "")
    path = os.path.join(target_dir, name + ".zip")
    n = 2
    while os.path.exists(path):
        path = os.path.join(target_dir, "%s_%d.zip" % (name, n))
        n += 1
    top = os.path.basename(folder.rstrip("/"))
    files = 0
    raw: dict[str, str] = {}
    used: set[str] = set()

    def member(rel: str, is_dir: bool = False) -> str:
        alt = _arc_name(rel)
        arc = rel if alt is None else alt
        if alt is None and arc not in used:
            used.add(arc)
            return arc + ("/" if is_dir else "")
        base, k = arc, 2
        while arc in used:                       # Ersatzname gibt es schon: eindeutig machen
            arc = "%s~%d" % (base, k)
            k += 1
        used.add(arc)
        raw[arc + ("/" if is_dir else "")] = os.fsencode(rel).hex()
        return arc + ("/" if is_dir else "")

    tmp = path + ".neu"
    try:
        with open(tmp, "wb") as fh:
            # strict_timestamps=False: Dateien von vor 1980 (aus Archiven ohne Zeitstempel) bekommen
            # 1980-01-01, statt die ganze Sicherung scheitern zu lassen (E99)
            with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                                 strict_timestamps=False) as z:
                for base, dirs, names in os.walk(folder, followlinks=False):
                    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
                    rel_base = os.path.relpath(base, folder)
                    arc_base = top if rel_base == "." else os.path.join(top, rel_base)
                    if not names and not dirs:
                        z.writestr(member(arc_base, is_dir=True), "")
                    for n_ in names:
                        full = os.path.join(base, n_)
                        if os.path.islink(full) or not os.path.isfile(full):
                            continue
                        if pause is not None:
                            pause()
                        z.write(full, member(os.path.join(arc_base, n_)))
                        files += 1
                if pause is not None:
                    pause()                      # auch nach der letzten Datei (Neustart dabei, E99)
                manifest = {"entry": entry_id, "title": title, "folder": folder, "top": top, "created": int(started)}
                if auto:
                    manifest["auto"] = True
                if raw:
                    manifest["raw"] = raw
                z.writestr(MANIFEST, jsontext.dumps(manifest, indent=1))
            # Erst auf der Platte, dann unter dem echten Namen (E99): Gleich danach kommen ältere
            # automatische Sicherungen in den Papierkorb; nach einem Stromausfall darf die neue
            # dann nicht leer sein
            fh.flush()
            os.fsync(fh.fileno())
        os.utime(tmp, (started, started))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"path": path, "files": files, "size": os.path.getsize(path)}


def _own(title: str, entry_id: str) -> list[tuple[dict, dict]]:
    """[(Sicherung, Manifest)] dieses Eintrags mit lesbarem Manifest, neueste zuerst. Zwei
    Einträge mit gleichem Titel (Steam und Lutris) teilen sich den Ordner, nicht ihre Sicherungen."""
    out = []
    for b in list_backups(title):
        try:
            info = read_manifest(b["path"])
        except (OSError, ValueError):
            continue
        if info.get("entry") == entry_id:
            out.append((b, info))
    return out


def last_backup(title: str, entry_id: str) -> float:
    """Beginn der jüngsten lesbaren Sicherung dieses Eintrags (Uhrzeit), 0 = keine (E99)."""
    newest = 0.0
    for b, info in _own(title, entry_id):
        at = info.get("created")
        newest = max(newest, float(at) if isinstance(at, (int, float)) else float(b["created"]))
    return newest


def stale_parts(title: str, now: float | None = None) -> list[str]:
    """Halbe ZIP-Dateien („*.zip.neu“), die ein Absturz oder das Ende des Home liegen ließ. Die der
    Automatik schreibt nur ihr eigener Thread, nacheinander: Ruft er das vor seiner Sicherung, ist
    jede „auto-…zip.neu“ übrig. Andere erst nach STALE_PART_S (eine von Hand läuft gerade noch)."""
    folder = backup_dir(title)
    now = time.time() if now is None else now
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    for n in names:
        if not n.endswith(".zip.neu"):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and (n.startswith(AUTO_PREFIX) or now - st.st_mtime > STALE_PART_S):
            out.append(p)
    return out


def prune_auto(title: str, keep: int, trash, entry_id: str) -> list[str]:
    """Behält die jüngsten keep automatischen Sicherungen dieses Eintrags; ältere kommen in den
    Papierkorb (trash, nie endgültig gelöscht). Von Hand gemachte, „vor-wiederherstellung“, fremde
    ZIP-Dateien mit passendem Namen und die eines anderen Eintrags mit gleichem Titel bleiben: Es
    zählt nur, was im Manifest als automatisch und zu diesem Eintrag steht (E99)."""
    autos = [b for b, info in _own(title, entry_id) if b["kind"] == "auto" and info.get("auto") is True]
    gone = []
    for b in autos[max(0, int(keep)):]:
        try:
            trash(b["path"])
            gone.append(b["path"])
        except (OSError, ValueError) as e:
            _log.warning("Alte automatische Sicherung bleibt: %s (%s)", b["path"], e)
    return gone


def _manifest(z: zipfile.ZipFile) -> dict:
    try:
        blob = z.read(MANIFEST)
    except KeyError as e:
        raise ValueError(TEXT_NOT_OURS) from e
    try:
        info = json.loads(blob.decode("utf-8"))
    except ValueError as e:                      # auch UnicodeDecodeError
        raise ValueError(TEXT_BROKEN) from e
    if not isinstance(info, dict):
        raise ValueError(TEXT_BROKEN)
    return info


def read_manifest(zip_path: str) -> dict:
    """Manifest einer Sicherung. Kaputte oder fremde ZIP-Dateien: ValueError mit deutschem Satz."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            return _manifest(z)
    except _ZIP_ERRORS as e:
        raise ValueError(TEXT_BROKEN) from e


def _members(z: zipfile.ZipFile, info: dict, top: str) -> list[tuple[str, str, bool]]:
    """[(Eintrag im ZIP, Pfad unter dem Zwischenordner, ist Ordner)]. Alles muss unter top liegen,
    sonst gilt die Sicherung als beschädigt und nichts geschieht."""
    raw = info.get("raw") if isinstance(info.get("raw"), dict) else {}
    out = []
    for m in z.namelist():
        if m == MANIFEST:
            continue
        is_dir = m.endswith("/")
        rel = m
        if m in raw:
            try:
                rel = os.fsdecode(bytes.fromhex(str(raw[m])))
            except ValueError as e:
                raise ValueError(TEXT_BROKEN) from e
        norm = os.path.normpath(rel)
        if "\0" in norm or norm.startswith(("..", "/")) or not (norm == top or norm.startswith(top + os.sep)) \
                or (norm == top and not is_dir):
            raise ValueError(TEXT_BROKEN)
        out.append((m, norm, is_dir))
    return out


def _unpack(z: zipfile.ZipFile, members: list[tuple[str, str, bool]], tmp: str) -> None:
    """Packt in den frischen Zwischenordner aus (dort gibt es keine Verweise, die irgendwohin zeigen)."""
    for m, rel, is_dir in members:
        target = os.path.join(tmp, rel)
        if is_dir:
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(m) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)


def _drop(tmp: str, trash) -> bool:
    """Zwischenordner nach einem Fehlschlag in den Papierkorb (keine zweite Löschstelle, E99).
    False = er liegt noch da (Laufwerk ohne Papierkorb)."""
    if not os.path.lexists(tmp):
        return True
    try:
        trash(tmp)
    except Exception as e:  # noqa: BLE001 – der eigentliche Fehler zählt, nicht dieser
        _log.warning("Zwischenordner bleibt liegen: %s (%s)", tmp, e)
    return not os.path.lexists(tmp)


def restore(zip_path: str, title: str, entry_id: str, trash, clock=time.time, current: str = "",
            refuse=None) -> dict:
    """Stellt eine Sicherung an ihren Ort zurück. Liefert {folder, safety}.

    Reihenfolge (E99): Sicherung prüfen und vollständig in einen Zwischenordner neben dem Ziel
    auspacken; erst dann den jetzigen Stand sichern („vor-wiederherstellung“), in den Papierkorb
    legen und den ausgepackten Stand an seine Stelle setzen. Scheitert das Auspacken (Bitfehler,
    volle Platte), bleibt der Spielstand, wie er war; der Zwischenordner kommt in den Papierkorb.

    current: wo die Suche den Spielstand heute findet. Heißt der Ordner wie in der Sicherung,
    gilt er statt des Orts von damals: Nach der Übernahme eines Lutris-Prefix (E10/E24) schrieb
    das Wiederherstellen sonst in den alten Prefix, und im Spiel änderte sich nichts.
    refuse(folder) -> "" oder ein Satz, warum dorthin nicht zurückgeholt wird (vor jeder Änderung)."""
    if not paths.is_under(zip_path, [backup_root()]):
        raise ValueError("Diese Sicherung liegt nicht im Sicherungsordner.")
    try:
        with zipfile.ZipFile(zip_path) as z:
            info = _manifest(z)
            folder, top = str(info.get("folder") or ""), str(info.get("top") or "")
            if info.get("entry") != entry_id or not folder or not top or "/" in top:
                raise ValueError("Diese Sicherung gehört zu einem anderen Spiel.")
            if current and os.path.basename(current.rstrip("/")) == top:
                folder = current
            why = refuse(folder) if refuse is not None else ""
            if why:
                raise ValueError(why)
            members = _members(z, info, top)
            if z.testzip() is not None:
                raise ValueError(TEXT_BROKEN)
            parent = os.path.dirname(folder.rstrip("/"))
            os.makedirs(parent, exist_ok=True)
            tmp = tempfile.mkdtemp(prefix=".couch-wiederherstellung-", dir=parent)
            try:
                _unpack(z, members, tmp)
            except BaseException:
                _drop(tmp, trash)
                raise
    except _ZIP_ERRORS as e:
        raise ValueError(TEXT_BROKEN) from e
    # Erst jetzt, mit vollständig ausgepackter Sicherung, wird der jetzige Stand angefasst
    safety = None
    try:
        if os.path.isdir(folder):
            safety = make_backup(title, entry_id, folder, note=SAFETY_NOTE, clock=clock)["path"]
            trash(folder)
            if os.path.lexists(folder):
                raise ValueError("Der alte Spielstand ließ sich nicht in den Papierkorb legen.")
        unpacked = os.path.join(tmp, top)
        if os.path.isdir(unpacked):
            os.rename(unpacked, folder)
        else:
            os.makedirs(folder)                  # leerer Ordner in der Sicherung
    except Exception as e:
        # Kann das Laufwerk keinen Papierkorb, scheitern trash(folder) und _drop beide: Dann soll
        # der Nutzer wissen, wo die ausgepackte Kopie liegt (früher entstand sie erst nach trash)
        if not _drop(tmp, trash):
            raise ValueError("%s %s" % (reason(e), TEXT_LEFTOVER % os.path.basename(tmp))) from e
        raise
    except BaseException:
        _drop(tmp, trash)
        raise
    try:
        os.rmdir(tmp)
    except OSError:
        _drop(tmp, trash)
    return {"folder": folder, "safety": safety}
