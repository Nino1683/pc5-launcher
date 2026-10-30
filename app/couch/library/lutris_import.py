"""Einmalige Übernahme der Spiele aus Lutris (E134). Nur library/imported.py lädt dieses Modul, und nur solange die
Übernahme nicht erledigt ist (<state_dir>/lutris-import.json): Danach liest der Launcher Lutris nie wieder.

Nur lesend, blockierend (SQLite mit mode=ro, YAML-Dateien, ein paar lstat je Prefix, dazu der FSR-Scan der
Spielordner). Geschrieben wird nur in den eigenen Ordner <data_dir>/lutris-import (copy_art): Lutris' Bilder
werden einmal kopiert, weil Lutris danach nicht mehr gelesen wird (und vielleicht nicht mehr installiert ist).

Gelesen werden <lutris_db> (pga.db, Tabelle games, nur installed = 1), die Vorgaben
<lutris_config>/system.yml und runners/<runner>.yml und je Spiel games/<configpath>.yml.

Regeln je Runner (dieselben wie bis 0.4.0 bei jedem Einlesen):
  wine    -> Entry(id="lutris:<slug>", source="proton", badge="PROTON")
  steam   -> ausgelassen (kommt aus der Steam-Quelle)
  linux   -> ausgelassen, wenn das Programm eden-launch oder cemu-launch ist (Emulator-Quelle);
             sonst Entry(id="lutris:<slug>", source="native", badge=""); rocksmith-launch bekommt
             die Marke "needs-steam" (braucht den laufenden Steam-Client) und, wenn das Skript
             eine Steam-App nennt, detail.steam_appid. Gibt es dieses Steam-Spiel in der
             Bibliothek, lässt sources.run_source den Eintrag weg (Bauplan E25).
  andere  -> ausgelassen, mit Warnung
Derselbe Slug zweimal: der zweite heißt "lutris:<slug>~<zeile>".

detail bei wine:
  slug, lutris_id, exe, args (Liste), working_dir, prefix (realpath oder ""), wine_version,
  is_proton, env (system.env), dll_overrides ({dll: modus} wie in der YAML), esync, fsync, dxvk,
  locale, prelaunch_command (Zeichenkette), prelaunch_wait, lutris_art {coverart, banner, icon ->
  Pfad oder None}, prefix_state, shared_prefix, store ("battlenet" oder ""), game_dir
  (durchsuchter Spielordner), fsr {verdict, detail, version, dir, stale}
detail bei linux: slug, lutris_id, exe, args (Liste), working_dir, env, lutris_art, steam_appid (nur needs-steam)
wine_version und is_proton beschreiben nur, was in Lutris eingestellt war: Gestartet wird mit der Proton-Wahl des
Launchers (launch/proton.py), nie mit einem Runner aus Lutris. copy_art ersetzt lutris_art durch imported_art
{cover, banner, icon} mit den Kopien.

available = exe vorhanden (fehlt z. B., wenn die Spiele-Platte nicht eingehängt ist).
Marken: "fsr4" / "fsr4-upgrade" (FSR-Scan), "vr" (siehe imported.is_vr_title), "battlenet".
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import sqlite3

import yaml

from . import fsr as fsr_mod
from .imported import (ART_DIR, EMULATOR_STARTERS, NEEDS_STEAM, art_folder, game_root, is_vr_title,
                       prefix_state)
from .model import Entry
from .sources import clean_text, err_text, expand_home, sort_title, title_key, to_float

_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

ART_MAX_BYTES = 40 * 1024 * 1024        # größer ist kein Cover (wie art.USER_MAX_BYTES)
_ART_EXT = (".jpg", ".png")


# ---------------------------------------------------------------- Helfer

def is_proton_version(version: str, steam_root: str, lutris_data: str, libraries=()) -> bool:
    """War die in Lutris gewählte Wine-Fassung ein Proton-Build? (nur zur Beschreibung, detail.is_proton)

    Ja, wenn es dazu ein Skript "proton" gibt (compatibilitytools.d oder steamapps/common). Nein,
    wenn der Build unter Lutris' eigenen Wine-Fassungen liegt. Liegt er nirgends, entscheidet der
    Name ("Proton 10.0", "GE-Proton…", "proton-cachyos…").
    """
    if not version:
        return False
    roots = [os.path.join(steam_root, "compatibilitytools.d"), "/usr/share/steam/compatibilitytools.d",
             os.path.join(steam_root, "steamapps/common")]
    roots += [os.path.join(lib, "steamapps/common") for lib in libraries]
    if any(os.path.isfile(os.path.join(r, version, "proton")) for r in roots):
        return True
    if os.path.isdir(os.path.join(lutris_data, "runners/wine", version)):
        return False
    return bool(re.match(r"(ge-)?proton", version, re.I))


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        data = yaml.load(fh, Loader=_YamlLoader)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("kein Schlüssel-Wert-Dokument")
    return data


def _section(cfg, key: str) -> dict:
    value = cfg.get(key) if isinstance(cfg, dict) else None
    return value if isinstance(value, dict) else {}


def _split_args(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if value in (None, ""):
        return []
    try:
        return shlex.split(str(value))
    except ValueError:                               # offene Anführungszeichen: wörtlich trennen
        return str(value).split()


def _str_map(value) -> dict:
    return {str(k): "" if v is None else str(v) for k, v in value.items()} if isinstance(value, dict) else {}


_STEAM_APP_HINTS = (
    re.compile(r"compatdata/(\d{2,10})\b"), re.compile(r"\bumu-(\d{2,10})\b"),
    re.compile(r"rungameid/(\d{2,10})\b"), re.compile(r"-applaunch\s+(\d{2,10})\b"),
    re.compile(r"\bSteamAppId=[\"']?(\d{2,10})\b"),
)


def steam_appid_of_script(path: str) -> int | None:
    """Die Steam-App, die ein Startskript nennt (compatdata/<id>, umu-<id>, rungameid/<id> …),
    oder None. Liest höchstens 64 KiB; wirft nicht."""
    try:
        if os.path.getsize(path) > 256 * 1024:
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return None
    if not text.startswith("#!"):
        return None
    for rx in _STEAM_APP_HINTS:
        m = rx.search(text)
        if m:
            return int(m.group(1))
    return None


def _rows(ctx) -> list[dict] | None:
    """Installierte Spiele aus pga.db. [] und Warnung, wenn es keine Datenbank gibt; None (dazu
    ctx.fail), wenn sie da, aber nicht lesbar ist (gesperrt, halb geschrieben, kaputt)."""
    if not os.path.isfile(ctx.lutris_db):
        ctx.warn("lutris: Datenbank fehlt (%s)" % ctx.lutris_db)
        return []
    con = None
    try:
        # as_uri() maskiert ?, # und % im Pfad; mode=ro öffnet garantiert nur lesend
        con = sqlite3.connect(pathlib.Path(os.path.abspath(ctx.lutris_db)).as_uri() + "?mode=ro",
                              uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("select * from games where installed = 1 order by id")]
    except (sqlite3.Error, OSError, ValueError) as e:
        ctx.warn("lutris: Datenbank nicht lesbar (%s), die Übernahme versucht es beim nächsten Start wieder"
                 % type(e).__name__)
        fail = getattr(ctx, "fail", None)
        if fail:
            fail("lutris")          # gesperrt oder halb geschrieben: kein „es gibt keine Spiele mehr“
        return None
    finally:
        if con is not None:
            con.close()


def _defaults(ctx, name: str) -> dict:
    path = os.path.join(ctx.lutris_config, name)
    if not os.path.isfile(path):
        return {}
    try:
        return _load_yaml(path)
    except Exception as e:
        ctx.warn("lutris: %s nicht lesbar (%s); Vorgaben fehlen" % (name, err_text(e)))
        return {}


def _art(ctx, slug: str) -> dict:
    data = os.path.dirname(ctx.lutris_db)
    cand = {
        "coverart": os.path.join(data, "coverart", slug + ".jpg"),
        "banner": os.path.join(data, "banners", slug + ".jpg"),
        "icon": os.path.join(ctx.home, ".local/share/icons/hicolor/128x128/apps", "lutris_%s.png" % slug),
    }
    return {k: (p if os.path.isfile(p) else None) for k, p in cand.items()}


# ---------------------------------------------------------------- Lesen

def read(ctx) -> tuple[str, list[Entry]]:
    """("ok", Einträge) | ("none", []) ohne Datenbank | ("failed", []) wenn sie nicht lesbar ist (ctx.fail ist
    dann gesetzt). Blockierend; wirft nicht."""
    rows = _rows(ctx)
    if rows is None:
        return "failed", []
    if not os.path.isfile(ctx.lutris_db):
        return "none", []
    if any(str(r.get("runner") or "") in ("wine", "linux") for r in rows) and not os.path.isdir(ctx.lutris_config):
        # Datenbank da, Konfiguration nicht erreichbar: Das wären „keine Spiele“ für immer. Lieber später wieder
        ctx.warn("lutris: Konfiguration fehlt (%s), die Übernahme versucht es beim nächsten Start wieder"
                 % ctx.lutris_config)
        ctx.fail("lutris")
        return "failed", []
    return "ok", _entries(ctx, rows)


def load(ctx) -> list[Entry]:
    """Lutris-Spiele als Einträge (wie read, nur die Liste). Blockierend; wirft nicht."""
    return read(ctx)[1]


def _entries(ctx, rows: list[dict]) -> list[Entry]:
    if not rows:
        return []
    system_defaults = _section(_defaults(ctx, "system.yml"), "system")
    runner_cfg: dict[str, dict] = {}
    lutris_data = os.path.dirname(ctx.lutris_db)
    mounts = ctx.fsr.mounts()
    out: list[Entry] = []
    used_ids: set[str] = set()

    for row in rows:
        name = clean_text(row.get("name"), "")
        runner = str(row.get("runner") or "")
        slug = str(row.get("slug") or "").strip() or "game-%s" % row.get("id")
        if runner == "steam":
            continue
        if runner not in ("wine", "linux"):
            ctx.warn("lutris: %s übersprungen (Runner „%s“ wird nicht unterstützt)" % (name or slug, runner))
            continue
        try:
            entry = _entry(ctx, row, name or slug, slug, runner, system_defaults, runner_cfg, lutris_data, mounts)
        except Exception as e:
            ctx.warn("lutris: %s ausgelassen (%s)" % (name or slug, err_text(e)))
            continue
        if entry is None:
            continue
        if entry.id in used_ids:
            # Lutris erlaubt denselben Slug zweimal; der erste (kleinere Nummer) behält die schlichte ID
            entry.id = "%s~%s" % (entry.id, row.get("id"))
            ctx.warn("lutris: Slug %s kommt mehrfach vor; zweiter Eintrag heißt %s" % (slug, entry.id))
        used_ids.add(entry.id)
        out.append(entry)

    counts: dict[str, int] = {}
    for e in out:
        p = e.detail.get("prefix")
        if e.source == "proton" and p:
            counts[p] = counts.get(p, 0) + 1
    for e in out:
        if e.source == "proton":
            e.detail["shared_prefix"] = counts.get(e.detail.get("prefix"), 0) > 1
    return out


def _entry(ctx, row, name, slug, runner, system_defaults, runner_cfg, lutris_data, mounts) -> Entry | None:
    cfg_name = "games/%s.yml" % row.get("configpath")
    cfg_path = os.path.join(ctx.lutris_config, cfg_name)
    try:
        cfg = _load_yaml(cfg_path)
    except FileNotFoundError:
        ctx.warn("lutris: %s ausgelassen (Konfiguration %s fehlt)" % (name, cfg_name))
        return None
    except Exception as e:
        ctx.warn("lutris: %s ausgelassen (%s nicht lesbar: %s)" % (name, cfg_name, err_text(e)))
        return None
    if runner not in runner_cfg:
        runner_cfg[runner] = _defaults(ctx, "runners/%s.yml" % runner)
    rdef = runner_cfg[runner]

    game = _section(cfg, "game")
    # Wie Lutris: Vorgaben, dann Runner, dann Spiel; env wird schlüsselweise gemischt
    layers = [system_defaults, _section(rdef, "system"), _section(cfg, "system")]
    system: dict = {}
    env: dict = {}
    for layer in layers:
        system.update(layer)
        env.update(_str_map(layer.get("env")))

    directory = str(row.get("directory") or "")
    exe = expand_home(str(game.get("exe") or "").strip(), ctx.home)
    if not exe:
        ctx.warn("lutris: %s ausgelassen (keine Startdatei eingetragen)" % name)
        return None
    working_dir = expand_home(str(game.get("working_dir") or "").strip(), ctx.home)
    if not os.path.isabs(exe):
        exe = os.path.join(directory or working_dir or expand_home(str(game.get("prefix") or ""), ctx.home), exe)
    working_dir = working_dir or os.path.dirname(exe)
    args = _split_args(game.get("args"))
    base = {
        "slug": slug, "lutris_id": row.get("id"), "exe": exe, "args": args,
        "working_dir": working_dir, "env": env, "lutris_art": _art(ctx, slug),
    }
    if getattr(ctx, "lutris_flatpak", False):
        base["lutris_flatpak"] = True       # E104: aus Lutris als Flatpak gelesen (nur zur Beschreibung)
    title = name
    common = dict(
        id="lutris:%s" % slug, title=title, kind="game",
        sort_title=sort_title(clean_text(row.get("sortname")) or title),
        available=os.path.isfile(exe), installed=True,
        last_played=to_float(row.get("lastplayed")) or None,
        playtime=max(0, int(round(to_float(row.get("playtime")) * 3600))),       # Lutris zählt Stunden
    )

    if runner == "linux":
        prog = os.path.basename(exe)
        if prog in EMULATOR_STARTERS:
            return None
        tags = []
        if prog in NEEDS_STEAM:
            tags.append("needs-steam")
            appid = steam_appid_of_script(exe)
            if appid:
                base["steam_appid"] = appid
        return Entry(source="native", badge="", tags=tags, detail=base, **common)

    wine = {**_section(rdef, "wine"), **_section(cfg, "wine")}
    prefix = expand_home(str(game.get("prefix") or "").strip(), ctx.home)
    prefix = os.path.realpath(prefix) if prefix else ""
    version = str(wine.get("version") or "")
    root = game_root(exe, directory, mounts, ctx.home)
    # Fehlt die exe (Platte weg), liefert der Scanner sein letztes bekanntes Ergebnis oder "missing".
    scan = ctx.fsr.scan(root)
    tags = fsr_mod.tags_for(scan)
    if is_vr_title(title, slug, root, scan):
        tags.append("vr")
    store = ""
    if title_key(title) == "battlenet" or os.path.basename(exe).lower() in ("battle.net launcher.exe", "battle.net.exe"):
        tags.append("battlenet")
        store = "battlenet"                          # der Proton-Starter setzt damit STORE=battlenet
    detail = dict(base)
    detail.update({
        "prefix": prefix,
        "wine_version": version,
        "is_proton": is_proton_version(version, ctx.steam_root, lutris_data),
        "dll_overrides": _str_map(wine.get("overrides")),
        "esync": bool(wine.get("esync", True)),
        "fsync": bool(wine.get("fsync", True)),
        "dxvk": bool(wine.get("dxvk", True)),
        "locale": str(system.get("locale") or ""),
        "prelaunch_command": expand_home(str(system.get("prelaunch_command") or "").strip(), ctx.home),
        "prelaunch_wait": bool(system.get("prelaunch_wait")),
        "prefix_state": prefix_state(prefix),
        "shared_prefix": False,
        "store": store,
        "game_dir": root,
        "fsr": fsr_mod.summary(scan),
    })
    return Entry(source="proton", badge="PROTON", tags=tags, detail=detail, **common)


# ---------------------------------------------------------------- Bilder einmal kopieren

def _art_sources(ctx, slug: str, found: dict) -> dict:
    """Wo Lutris Bilder zu diesem Slug hat, je Art die erste vorhandene Datei. Gesucht wird wie bisher beim
    Auflösen (art.py bis 0.4.0): coverart/ und banners/ neben pga.db, unter ~/.local/share/lutris und
    ~/.cache/lutris (ältere Lutris-Fassungen), .jpg vor .png; das Symbol unter hicolor/128x128 (auch das von
    Lutris als Flatpak)."""
    dirs = []
    for base in (os.path.dirname(ctx.lutris_db), os.path.join(ctx.home, ".local/share/lutris"),
                 os.path.join(ctx.home, ".cache/lutris")):
        if base not in dirs:
            dirs.append(base)
    icon_dirs = [os.path.join(ctx.home, ".local/share/icons/hicolor/128x128/apps"),
                 os.path.join(ctx.home, ".var/app/net.lutris.Lutris/data/icons/hicolor/128x128/apps")]
    cand = {
        "cover": [found.get("coverart")] + [os.path.join(d, "coverart", slug + x) for d in dirs for x in _ART_EXT],
        "banner": [found.get("banner")] + [os.path.join(d, "banners", slug + x) for d in dirs for x in _ART_EXT],
        "icon": [found.get("icon")] + [os.path.join(d, "lutris_%s.png" % slug) for d in icon_dirs],
    }
    out = {}
    for kind, paths in cand.items():
        out[kind] = next((p for p in paths if isinstance(p, str) and p and os.path.isfile(p)), None)
    return out


def _copy_file(src: str, folder: str, name: str) -> str | None:
    """src nach <folder>/<name> kopieren (Zwischendatei, dann rename). Nur gewöhnliche Dateien bis ART_MAX_BYTES;
    gelesen wird nur src. Rückgabe: Pfad der Kopie oder None."""
    tmp = os.path.join(folder, ".%s.%d.tmp" % (name, os.getpid()))
    try:
        with open(src, "rb") as fin:
            st = os.fstat(fin.fileno())
            if not (st.st_mode & 0o170000 == 0o100000) or not 0 < st.st_size <= ART_MAX_BYTES:
                return None
            data = fin.read(ART_MAX_BYTES + 1)
        if len(data) > ART_MAX_BYTES:
            return None
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as fout:
            fout.write(data)
        os.replace(tmp, os.path.join(folder, name))
        return os.path.join(folder, name)
    except OSError:
        try:
            os.unlink(tmp)                  # nur die eigene Zwischendatei
        except OSError:
            pass
        return None


def copy_art(ctx, records: list) -> None:
    """Die Bilder aus Lutris einmal in den eigenen Ordner <data_dir>/lutris-import/<slug>/ (cover, banner, icon mit
    der Endung der Vorlage) und detail.imported_art auf die Kopien. detail.lutris_art (Pfade in Lutris' Ordnern)
    entfällt. Geschrieben wird nur dort; ein Ordner, der ein Verweis ist, wird nicht benutzt. Wirft nicht."""
    root = os.path.join(ctx.data_dir, ART_DIR)
    data_real = os.path.realpath(ctx.data_dir)
    for record in records:
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        record["detail"] = detail
        found = detail.pop("lutris_art", None)
        slug = str(detail.get("slug") or str(record.get("id", "")).split(":", 1)[-1])
        copies = {"cover": None, "banner": None, "icon": None}
        sources = _art_sources(ctx, slug, found if isinstance(found, dict) else {})
        if any(sources.values()):
            folder = os.path.join(root, art_folder(slug))
            try:
                os.makedirs(folder, mode=0o755, exist_ok=True)
                usable = not os.path.islink(folder) and not os.path.islink(root) \
                    and os.path.realpath(folder) == os.path.join(data_real, ART_DIR, art_folder(slug))
            except OSError as e:
                ctx.warn("lutris: Ordner für die Bilder von %s nicht anlegbar (%s)" % (slug, err_text(e)))
                usable = False
            for kind, src in sources.items():
                if usable and src:
                    ext = os.path.splitext(src)[1].lower() if kind != "icon" else ".png"
                    copies[kind] = _copy_file(src, folder, kind + (ext if ext in _ART_EXT else ".jpg"))
        detail["imported_art"] = copies
