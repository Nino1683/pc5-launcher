"""Quelle "local": Spiele, die der Archiv-Installer angelegt hat.

Dieses Modul liest nur (blockierend: eine JSON-Datei, ein paar stat, der FSR-Scan). Geschrieben
wird <data_dir>/library.json allein vom LibraryIndex.

Der Datensatz eines Archiv-Spiels (das gibt der Installer an add_local weiter):

  {"id": "local:6f1c0a52c6f04d0d9c3d2d0f5a7b8e91",   Pflicht. "local:" + uuid4().hex, bleibt für immer
   "title": "Cool Game",                              Pflicht. Anzeigename
   "platform": "windows",                             Pflicht. "windows" | "linux" | ROM-System ("switch", "wiiu", …)
   "install_dir": "/run/media/nutzer/Spiele/Cool Game", Pflicht. absoluter Pfad des angelegten Ordners
   "exe": "bin/CoolGame.exe",                         Pflicht bei windows/linux. Startdatei RELATIV zu install_dir, mit "/"
   "exe_candidates": [                                 weitere Startdateien für „Andere Startdatei wählen“
      {"path": "bin/CoolGame.exe", "score": 106, "why": "Name = Spieltitel"},
      {"path": "Launcher.exe", "score": 41}],         (einfache Zeichenketten gehen auch)
   "args": "",                                        Argumente als Zeichenkette (Shell-Schreibweise) oder Liste
   "created": 1791900000.0,                           Unix-Zeit der Installation
   "archive": "/home/nutzer/Downloads/Cool Game.zip", Herkunft, nur zur Anzeige
   "marker": "9b0e6c1f52a84c0e",                      Zufallswert des Auftrags; steht auch als Feld "token" in
                                                      <install_dir>/.couch-install.json (Löschschutz nach E17)
   "epic": "Fortnite",                                nur Epic-Spiele aus dem Store (E73): App-Name in legendary;
                                                      gestartet wird dann über bin/couch-epic-game
   "itch": {"id": "user/slug", "game_id": 341190,     nur itch.io-Spiele aus dem Store (E100, archive "itch.io"):
            "upload_id": 1, "build_id": 0,            Herkunft der installierten Datei für den Hinweis auf eine
            "updated_at": "…", "key_id": 2,           neuere Fassung; detail.store wird "itchio" (STORE für umu)
            "page": "https://user.itch.io/slug"}}

Abschnitt "local" in library.json – gelesen werden alle drei Formen:
  a) Liste solcher Datensätze                         [ {datensatz}, … ]
  b) Objekt ID -> Datensatz                           {"local:…": {datensatz}}
  c) Objekt ID -> Entry als Objekt, die Felder des    {"local:…": {"id", "title", "source": "local", …,
     Datensatzes unter "detail" (so legt                          "detail": {"platform", "install_dir", "exe", …}}}
     LibraryIndex.add_local(entry) sie heute ab)
Für c) baut record_to_entry(ctx, datensatz) den passenden Entry; entry_to_record() ist der Rückweg.

Regeln beim Lesen:
  windows -> Entry(source="local", badge="PROTON"), linux -> badge "".
  ROM-Installationen (jede andere platform) werden hier ausgelassen: Die Spiele erscheinen über
  die Emulator-Quelle; ihr Datensatz bleibt für das Löschen in der Datei.
  available = install_dir und die Startdatei sind vorhanden (bei jedem Einlesen neu geprüft).
  Eine Startdatei, die aus install_dir hinauszeigt („../…“, absoluter Pfad, Symlink), wird nicht
  übernommen. Fehlt die Startdatei noch (der Nutzer muss erst wählen), bleibt der Eintrag mit
  available=False und der Marke "needs-exe" stehen.
  Die vom Nutzer gewählte Startdatei steht in options["exe"]; das wertet der Starter aus.

Ergebnis: detail={platform, install_dir, exe (relativ), exe_path (absolut oder ""), exe_candidates
  [{path, score, why}], args (Liste), created, archive, marker, marker_ok, fsr {…}}; dazu bei Epic
  epic/store, bei Launcher-Spielen launcher…/store und bei itch.io (E100) itch/store "itchio".
"""
from __future__ import annotations

import json
import os
import shlex
import time
import uuid

from . import fsr as fsr_mod
from .model import Entry
from .sources import clean_text, err_text, read_json, sort_title, to_float

LIBRARY_FILE = "library.json"
SECTION = "local"
MARKER_FILE = ".couch-install.json"      # schreibt der Installer (installer/trash.py MARKER_NAME)
RECORD_KEYS = ("platform", "install_dir", "exe", "exe_candidates", "args", "created", "archive", "marker", "epic",
               "launcher", "launcher_code", "folder_win", "launcher_prefix", "itch")
# Spiele über den Launcher eines Herstellers (E94): Marke auf der Kachel und STORE für umu/Proton
LAUNCHER_BADGES = {"battlenet": ("BATTLE.NET", "battlenet"), "ubisoft": ("UBISOFT", "ubisoft"), "rockstar": ("ROCKSTAR", "")}
PC_PLATFORMS = ("windows", "linux")
ITCH_ARCHIVE = "itch.io"                 # archive der itch.io-Spiele aus dem Store (E100)
ITCH_KEYS = {"id": str, "game_id": int, "upload_id": int, "build_id": int, "updated_at": str, "key_id": int, "page": str}


# ---------------------------------------------------------------- reine Helfer (für Installer und Index)

def new_id() -> str:
    return "local:" + uuid.uuid4().hex


def make_record(title: str, platform: str, install_dir: str, exe: str = "", exe_candidates=(),
                args="", archive: str = "", marker: str = "", created: float | None = None,
                entry_id: str | None = None, epic: str = "", itch: dict | None = None) -> dict:
    """Baut einen gültigen Datensatz für den Abschnitt "local". Rein, schreibt nichts."""
    rec = {
        "id": entry_id or new_id(), "title": title, "platform": platform,
        "install_dir": install_dir, "exe": exe.replace(os.sep, "/"),
        "exe_candidates": [dict(c) if isinstance(c, dict) else {"path": str(c)} for c in exe_candidates],
        "args": args, "created": time.time() if created is None else created,
        "archive": archive, "marker": marker,
    }
    if epic:
        rec["epic"] = epic
    if itch:
        rec["itch"] = itch_detail(itch)
    return rec


def itch_detail(value) -> dict:
    """Herkunft eines itch.io-Spiels (E100) in fester Form: nur bekannte Felder mit passendem Typ. Rein."""
    out = {}
    for key, kind in ITCH_KEYS.items():
        raw = value.get(key) if isinstance(value, dict) else None
        if kind is int:
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                out[key] = raw
        elif isinstance(raw, str) and raw:
            out[key] = raw[:300]
    return out


def entry_to_record(entry: Entry) -> dict:
    """Der Datensatz zu einem Entry dieser Quelle (für LibraryIndex.add_local). Rein."""
    d = entry.detail
    rec = make_record(entry.title, d.get("platform", "windows"), d.get("install_dir", ""),
                      d.get("exe", ""), d.get("exe_candidates") or (), d.get("args") or "",
                      d.get("archive", ""), d.get("marker", ""), d.get("created"), entry.id, d.get("epic", ""),
                      d.get("itch") if isinstance(d.get("itch"), dict) else None)
    if d.get("launcher"):
        rec.update({k: d[k] for k in ("launcher", "launcher_code", "folder_win", "launcher_prefix") if k in d})
    return rec


def marker_valid(install_dir: str, marker: str) -> bool:
    """Trägt der Ordner die Marke dieses Datensatzes (Feld "token" in .couch-install.json)?
    Nur lesend und nur zur Anzeige; die Löschfunktion des Installers prüft selbst und strenger."""
    if not marker or not install_dir:
        return False
    path = os.path.join(install_dir, MARKER_FILE)
    try:
        if os.path.islink(path) or os.path.getsize(path) > 256 * 1024:
            return False
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("token") == marker


def resolve_exe(install_dir: str, exe: str) -> str | None:
    """Absoluter Pfad der Startdatei, wenn er (aufgelöst) in install_dir bleibt; sonst None."""
    if not install_dir or not exe or os.path.isabs(exe):
        return None
    path = os.path.normpath(os.path.join(install_dir, exe.replace("/", os.sep)))
    root = os.path.realpath(install_dir)
    real = os.path.realpath(path)
    if real != root and real.startswith(root.rstrip(os.sep) + os.sep):
        return path
    return None


def _args_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if not value:
        return []
    try:
        return shlex.split(str(value))
    except ValueError:
        return str(value).split()


def _candidates(value) -> list[dict]:
    out = []
    for c in value if isinstance(value, (list, tuple)) else []:
        if isinstance(c, str) and c:
            out.append({"path": c, "score": None, "why": ""})
        elif isinstance(c, dict) and isinstance(c.get("path"), str) and c["path"]:
            out.append({"path": c["path"], "score": c.get("score"), "why": str(c.get("why") or "")})
    return out


# ---------------------------------------------------------------- Quelle

def read_records(ctx) -> list:
    """Rohdatensätze des Abschnitts "local" (auch ROM-Installationen). Blockierend; wirft nicht."""
    path = os.path.join(ctx.data_dir, LIBRARY_FILE) if ctx.data_dir else ""
    if not path or not os.path.isfile(path):
        return []
    try:
        data = read_json(path)
    except (OSError, ValueError) as e:
        ctx.warn("local: %s nicht lesbar (%s)" % (LIBRARY_FILE, err_text(e)))
        return []
    records = data.get(SECTION) if isinstance(data, dict) else None
    if records is None:
        return []
    if isinstance(records, dict):
        return [as_record(value, key) for key, value in records.items()]
    if isinstance(records, list):
        return [as_record(value) for value in records]
    ctx.warn("local: Abschnitt „local“ in %s ist weder Liste noch Objekt" % LIBRARY_FILE)
    return []


def as_record(value, key: str | None = None):
    """Bringt die drei erlaubten Formen auf den flachen Datensatz. Unbrauchbares bleibt, wie es ist
    (record_to_entry meldet es dann). Rein."""
    if not isinstance(value, dict):
        return value
    detail = value.get("detail")
    if isinstance(detail, dict) and "install_dir" not in value:
        # Form c): Entry als Objekt, die Felder des Datensatzes liegen unter detail
        rec = {k: detail[k] for k in RECORD_KEYS if k in detail}
        rec["id"] = value.get("id") or key
        rec["title"] = value.get("title")
        return rec
    if key and not value.get("id"):
        return {**value, "id": key}
    return value


def record_to_entry(ctx, rec) -> Entry | None:
    """Ein Datensatz -> Entry; None (mit Warnung), wenn er unbrauchbar ist, None ohne Warnung bei ROMs."""
    if not isinstance(rec, dict):
        ctx.warn("local: Eintrag ausgelassen (kein Objekt)")
        return None
    entry_id = rec.get("id")
    title = clean_text(rec.get("title"))
    if not isinstance(entry_id, str) or not entry_id.startswith("local:") or len(entry_id) <= 6:
        ctx.warn("local: Eintrag „%s“ ausgelassen (ID fehlt oder beginnt nicht mit local:)" % (title or "?"))
        return None
    platform = str(rec.get("platform") or "").lower()
    if platform not in PC_PLATFORMS:
        return None                                  # ROM: erscheint über die Emulator-Quelle
    install_dir = rec.get("install_dir")
    if not title or not isinstance(install_dir, str) or not os.path.isabs(install_dir):
        ctx.warn("local: Eintrag %s ausgelassen (Titel oder Ordner fehlt)" % entry_id)
        return None
    exe = str(rec.get("exe") or "").replace("\\", "/").strip()
    tags = []
    if not exe:
        # Der Installer konnte sich nicht entscheiden; der Nutzer wählt über „Startdatei wählen“
        exe_path = ""
        tags.append("needs-exe")
    else:
        exe_path = resolve_exe(install_dir, exe)
        if exe_path is None:
            ctx.warn("local: %s ausgelassen (Startdatei „%s“ liegt nicht im Spielordner)" % (title, clean_text(exe)))
            return None
    available = bool(exe_path) and os.path.isdir(install_dir) and os.path.isfile(exe_path)
    scan = ctx.fsr.scan(install_dir) if platform == "windows" else None
    marker = str(rec.get("marker") or "")
    epic = clean_text(rec.get("epic")) if platform == "windows" else ""
    detail_epic = {"epic": epic, "store": "egs"} if epic else {}
    launcher = str(rec.get("launcher") or "") if platform == "windows" else ""
    badge = "EPIC" if epic else "PROTON" if platform == "windows" else ""
    if launcher in LAUNCHER_BADGES:
        badge, store = LAUNCHER_BADGES[launcher]
        detail_epic = {"launcher": launcher, "launcher_code": rec.get("launcher_code"),
                       "folder_win": str(rec.get("folder_win") or ""),
                       "launcher_prefix": str(rec.get("launcher_prefix") or ""), "store": store}
    elif not epic and str(rec.get("archive") or "") == ITCH_ARCHIVE:
        # itch.io aus dem Store (E100): STORE für umu; die Marke bleibt PROTON bzw. leer (Startweg)
        detail_epic = {"store": "itchio", "itch": itch_detail(rec.get("itch"))}
    return Entry(
        id=entry_id, title=title, kind="game", source="local",
        badge=badge,
        sort_title=sort_title(title), available=available, installed=True,
        tags=fsr_mod.tags_for(scan) + tags,
        detail={
            "platform": platform, "install_dir": install_dir, "exe": exe, "exe_path": exe_path,
            "exe_candidates": _candidates(rec.get("exe_candidates")), "args": _args_list(rec.get("args")),
            "created": to_float(rec.get("created")) or None, "archive": str(rec.get("archive") or ""),
            "marker": marker, "marker_ok": marker_valid(install_dir, marker),
            "fsr": fsr_mod.summary(scan), **detail_epic,
        })


def load(ctx) -> list[Entry]:
    """Archiv-Spiele als Einträge. Blockierend; wirft nicht, Probleme stehen in ctx.warnings."""
    out, seen = [], set()
    for rec in read_records(ctx):
        try:
            entry = record_to_entry(ctx, rec)
        except Exception as e:
            ctx.warn("local: Eintrag ausgelassen (%s)" % err_text(e))
            continue
        if entry is None:
            continue
        if entry.id in seen:
            ctx.warn("local: doppelte ID %s, zweiter Eintrag ausgelassen" % entry.id)
            continue
        seen.add(entry.id)
        out.append(entry)
    return out
