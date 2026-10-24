"""Quelle "lutris": Spiele aus der Lutris-Bibliothek, nur gelesen (E10: gestartet wird ohne Lutris).

Nur lesend, blockierend (SQLite mit mode=ro, YAML-Dateien, ein paar lstat je Prefix, dazu der
FSR-Scan der Spielordner).

Gelesen werden <lutris_db> (pga.db, Tabelle games, nur installed = 1), die Vorgaben
<lutris_config>/system.yml und runners/<runner>.yml und je Spiel games/<configpath>.yml.

Regeln je Runner:
  wine    -> Entry(id="lutris:<slug>", source="proton", badge="PROTON")
  steam   -> ausgelassen (kommt aus der Steam-Quelle)
  linux   -> ausgelassen, wenn das Programm eden-launch oder cemu-launch ist (Emulator-Quelle);
             sonst Entry(id="lutris:<slug>", source="native", badge=""); rocksmith-launch bekommt
             die Marke "needs-steam" (braucht den laufenden Steam-Client) und, wenn das Skript
             eine Steam-App nennt, detail.steam_appid. Gibt es dieses Steam-Spiel in der
             Bibliothek, lässt sources.run_source den Lutris-Eintrag weg (Bauplan E25).
  andere  -> ausgelassen, mit Warnung

detail bei wine:
  slug, lutris_id, exe, args (Liste), working_dir, prefix (realpath oder ""), wine_version,
  is_proton, env (system.env), dll_overrides ({dll: modus} wie in der YAML), esync, fsync, dxvk,
  locale, prelaunch_command (Zeichenkette), prelaunch_wait, lutris_art {coverart, banner, icon ->
  Pfad oder None}, prefix_state, shared_prefix, store ("battlenet" oder ""), game_dir
  (durchsuchter Spielordner), fsr {verdict, detail, version, dir, stale}
detail bei linux: slug, lutris_id, exe, args (Liste), working_dir, env, lutris_art, steam_appid (nur needs-steam)

available = exe vorhanden (fehlt z. B., wenn die Spiele-Platte nicht eingehängt ist).
Marken: "fsr4" / "fsr4-upgrade" (FSR-Scan), "vr" (siehe is_vr_title), "battlenet".
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import sqlite3

import yaml

from . import fsr as fsr_mod
from .model import Entry
from .sources import clean_text, err_text, expand_home, sort_title, title_key, to_float

_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Lutris-Einträge, die nur die eigenen Emulator-Starter des Nutzers aufrufen: Diese Spiele liefert die Emulator-Quelle.
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


# ---------------------------------------------------------------- Helfer

def overrides_env(overrides) -> str:
    """Lutris-Schreibweise {dll: disabled|native|builtin|…} -> Wert für WINEDLLOVERRIDES (für den Starter)."""
    short = {"disabled": "", "native": "n", "builtin": "b", "n,b": "n,b", "b,n": "b,n"}
    parts = []
    for dll, mode in (overrides.items() if isinstance(overrides, dict) else []):
        parts.append("%s=%s" % (dll, short.get(str(mode), str(mode))))
    return ";".join(parts)


def prefix_state(prefix: str) -> str:
    """Zustand eines Wine-Prefix (03, Abschnitt 4.2). Nur lstat/readlink, schnell.

    "missing"  Ordner fehlt oder kein Prefix eingetragen
    "empty"    Ordner da, aber nie gestartet (keine system.reg)
    "proton"   Protons Marke (Datei version oder tracked_files), DXVK von Proton
    "mixed"    Protons Marke, aber system32/dxgi.dll ist ein Symlink in Lutris' DXVK (ein
               wine-staging-Start hat Protons Dateien ersetzt)
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


def is_proton_version(version: str, steam_root: str, lutris_data: str, libraries=()) -> bool:
    """Ist die in Lutris gewählte Wine-Fassung ein Proton-Build?

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


def game_root(exe: str, directory: str = "", mounts=None, home: str = "") -> str:
    """Spielordner zu einer exe (das ist der Ordner, den der FSR-Scan durchsucht).

    Ausgang ist der Ordner der exe. Heißt er wie ein Programm-Unterordner (bin/x64,
    Binaries/Win64), geht es bis zu drei Ebenen hoch; bei Unreal-Spielen (Ordner Engine neben dem
    Projektordner) noch eine. Nie bis auf einen Einhängepunkt oder das Home. Liegt Lutris'
    "directory" darüber, gilt das.
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


def _rows(ctx) -> list[dict]:
    """Installierte Spiele aus pga.db. Leere Liste und Warnung, wenn die Datenbank nicht lesbar ist."""
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
        ctx.warn("lutris: Datenbank nicht lesbar (%s)" % type(e).__name__)
        fail = getattr(ctx, "fail", None)
        if fail:
            fail("lutris")          # gesperrt oder halb geschrieben: kein „es gibt keine Spiele mehr“
        return []
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


# ---------------------------------------------------------------- Quelle

def load(ctx) -> list[Entry]:
    """Lutris-Spiele als Einträge. Blockierend; wirft nicht, Probleme stehen in ctx.warnings."""
    rows = _rows(ctx)
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
        base["lutris_flatpak"] = True       # E104: aus Lutris als Flatpak gelesen; „wie in Lutris“ (E55) nur nativ
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
