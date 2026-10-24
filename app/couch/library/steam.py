"""Quelle "steam": installierte Steam-Spiele, gelesen von der Platte, ohne den Client zu fragen.

Nur lesend, blockierend (Platte; auf dem Entwicklungsrechner rund 30 ms ohne FSR-Scan).

Gelesene Dateien (Aufbau: docs/research/02-steam-integration.md, Abschnitt 5):
  config/libraryfolders.vdf                      Bibliotheksordner (auch nicht eingehängte)
  <bibliothek>/steamapps/appmanifest_<id>.acf    Name, installdir, StateFlags, Größe, LastPlayed
  appcache/appinfo.vdf                           Typ (Game/Tool/…), Bildnamen
  userdata/<konto>/config/localconfig.vdf        NUR apps/<id>/LastPlayed und Playtime
  config/config.vdf                              NUR CompatToolMapping/<id>/name
  config/loginusers.vdf                          NUR PersonaName, RememberPassword, AllowAutoLogin,
                                                 WantsOfflineMode, MostRecent, Timestamp
  userdata/<konto>/config/shortcuts.vdf          shortcut_art() (Bilder, zuletzt gespielt) und
                                                 load_shortcuts() (Einträge nach Bauplan E25)
  userdata/<konto>/config/cloudstorage/cloud-storage-namespace-1.json
                                                 NUR die Sammlung „Favoriten“ (favorite_ids, E99)
  appcache/librarycache/<id>/, userdata/<konto>/config/grid/    Bilder

In config.vdf und localconfig.vdf stehen Anmeldedaten. Aus dem geparsten Baum werden sofort nur
die genannten Felder herausgezogen, der Rest wird verworfen; Fehlermeldungen der Parser (sie
zitieren die kaputte Zeile) gehen nie in eine Warnung.

Ergebnis je Spiel:
  Entry(id="steam:<appid>", kind="game", source="steam", badge="STEAM", installed=StateFlags&4,
        available=Bibliothek eingehängt und Spielordner da, last_played, playtime (Sekunden),
        tags=["fsr4"] | ["fsr4-upgrade"] | ["fsr4-needs-cachyos"] | [],
        detail={appid, install_dir, library, compat_tool ("" = Steams Vorgabe), size_on_disk,
                steam_art {cover, wide, hero, logo, icon -> Pfad, nur vorhandene}, type,
                state_flags, update_pending, native_linux, run_url, fsr {verdict, detail, version},
                library_assets {library_capsule, library_capsule_2x, … -> Name in librarycache; nur
                wenn appinfo welche nennt}, steam_favorite (nur wenn in Steams „Favoriten“)})

Fremdspiele, die es nur als Steam-Verknüpfung gibt (E25), liefert load_shortcuts(ctx, andere):
  Entry(id="steam:shortcut:<appid>", kind="game", source="steam", badge="STEAM",
        available=Startdatei vorhanden, last_played aus der Verknüpfung,
        detail={shortcut: True, shortcut_appid, gameid ("<64 Bit>" als Text), run_url
                ("steam://rungameid/<gameid>"), exe, args, start_dir, game_dir, compat_tool,
                steam_art {…aus dem grid-Ordner}, collections, fsr, steam_favorite?})
  Bewusst ohne detail["appid"]: Die Nummer einer Verknüpfung ist keine App-Nummer des Shops.
"""
from __future__ import annotations

import json
import os
import re
import struct

import vdf

from . import fsr as fsr_mod
from .model import Entry, source_title
from .sources import clean_text, err_text, same_path, sort_title, title_key, to_int

STEAMID64_BASE = 76561197960265728
# appinfo common/type, die als Kachel taugen (Werkzeuge, Laufzeiten, DLC, Musik, Video nicht)
PLAYABLE_TYPES = {"game", "demo", "application", "beta", "mod"}
# Nur für den Notfall ohne lesbare appinfo.vdf: was sicher kein Spiel ist
_TOOL_NAME = re.compile(r"^(proton\b|steam linux runtime|steamworks common|steamvr$|steam controller)", re.I)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
_ICON_EXT = _IMAGE_EXT + (".ico",)

MAGIC_V29 = 0x07564429      # Schlüssel als Nummern in eine Tabelle am Dateiende
MAGIC_V28 = 0x07564428      # ältere Clients: Schlüssel als Text


# ---------------------------------------------------------------- kleine VDF-Helfer

def ci(d, *path, default=None):
    """Abstieg ohne Rücksicht auf Groß-/Kleinschreibung (Steam schreibt Schlüssel uneinheitlich)."""
    for key in path:
        if not isinstance(d, dict):
            return default
        hit = next((k for k in d if isinstance(k, str) and k.lower() == key.lower()), None)
        if hit is None:
            return default
        d = d[hit]
    return d


def _load_text_vdf(path: str) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        data = vdf.load(fh)
    return data if isinstance(data, dict) else {}


def supports_fsr4(tool: str) -> bool:
    """Kann dieses Kompatibilitätswerkzeug PROTON_FSR4_UPGRADE? Geprüft ist nur Proton-CachyOS;
    Valves Proton enthält die Zeichenfolge FSR4 nicht (02, Abschnitt 5.5)."""
    return "cachyos" in (tool or "").lower()


# ---------------------------------------------------------------- appinfo.vdf

def _cstr(buf: bytes, pos: int):
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _parse_kv(buf: bytes, pos: int, keys, depth: int = 0):
    """Binäres VDF ab pos. keys=None: Schlüssel stehen als Text da (v28)."""
    if depth > 64:
        raise ValueError("zu tief verschachtelt")
    out = {}
    while True:
        t = buf[pos]
        pos += 1
        if t == 0x08:
            return out, pos
        if keys is None:
            key, pos = _cstr(buf, pos)
        else:
            (idx,) = struct.unpack_from("<I", buf, pos)
            pos += 4
            key = keys[idx]
        if t == 0x00:
            out[key], pos = _parse_kv(buf, pos, keys, depth + 1)
        elif t == 0x01:
            out[key], pos = _cstr(buf, pos)
        elif t == 0x02:
            (out[key],) = struct.unpack_from("<i", buf, pos)
            pos += 4
        elif t == 0x07:
            (out[key],) = struct.unpack_from("<Q", buf, pos)
            pos += 8
        elif t == 0x0A:
            # int64; kennt auch der Leser in art.py (E99: sonst fiel der ganze App-Block weg)
            (out[key],) = struct.unpack_from("<q", buf, pos)
            pos += 8
        elif t == 0x03:
            (out[key],) = struct.unpack_from("<f", buf, pos)
            pos += 4
        else:
            raise ValueError("unbekannter VDF-Typ %#x" % t)


def load_appinfo(path: str, only=None) -> dict:
    """Liest appcache/appinfo.vdf: {appid: {"appinfo": {...}, "change_number": n}}. Blockierend.

    Aufbau v29: uint32 magic, uint32 universe, int64 Offset der Schlüsseltabelle; je App
    uint32 appid (0 = Ende), uint32 size, uint32 info_state, uint32 last_updated, uint64 token,
    20 Byte sha1, uint32 change_number, 20 Byte sha1, binäres VDF. Tabelle: uint32 Anzahl, dann
    nullterminierte Zeichenketten.

    only: nur diese App-IDs auswerten. Wirft OSError/ValueError bei unlesbarem Kopf; ein einzelner
    kaputter App-Block wird übersprungen (Steam schreibt die Datei im laufenden Betrieb neu).
    """
    with open(path, "rb") as fh:
        buf = fh.read()
    try:
        magic, _universe = struct.unpack_from("<II", buf, 0)
        pos = 8
        keys = None
        if magic == MAGIC_V29:
            (table_off,) = struct.unpack_from("<q", buf, pos)
            pos += 8
            (count,) = struct.unpack_from("<I", buf, table_off)
            if count > 5_000_000:
                raise ValueError("Schlüsseltabelle unplausibel")
            p = table_off + 4
            keys = []
            for _ in range(count):
                s, p = _cstr(buf, p)
                keys.append(s)
        elif magic != MAGIC_V28:
            raise ValueError("unbekanntes Format %#x" % magic)
    except (struct.error, IndexError) as e:
        raise ValueError("Kopf unlesbar") from e
    apps = {}
    while True:
        try:
            (appid,) = struct.unpack_from("<I", buf, pos)
            if appid == 0:
                break
            (size,) = struct.unpack_from("<I", buf, pos + 4)
        except struct.error:
            break                                    # abgeschnittene Datei: nehmen, was da ist
        body = pos + 8                               # size zählt ab hier
        if only is None or appid in only:
            try:
                (change,) = struct.unpack_from("<I", buf, body + 36)
                data, _ = _parse_kv(buf, body + 60, keys)
                info = data.get("appinfo", data)
                if isinstance(info, dict):
                    apps[appid] = {"appinfo": info, "change_number": change}
            except (struct.error, IndexError, ValueError, RecursionError):
                pass
        if size <= 0:
            break
        pos = body + size
    return apps


def library_assets(appinfo: dict, lang: str = "german") -> dict:
    """Dateinamen der Bibliotheksbilder aus common/library_assets_full, relativ zu
    appcache/librarycache/<appid>/: {"library_capsule": "<hash>/library_capsule.jpg",
    "library_capsule_2x": …, …}. Die doppelt großen Fassungen (image2x) als <art>_2x: Der
    Bilder-Abruf nimmt sie fürs Logo und als Ersatz (art.py liest sie aus detail.library_assets)."""
    full = ci(appinfo, "common", "library_assets_full", default={})
    out = {}
    for kind, entry in (full.items() if isinstance(full, dict) else []):
        for size_key, suffix in (("image", ""), ("image2x", "_2x")):
            imgs = ci(entry, size_key) if isinstance(entry, dict) else None
            if isinstance(imgs, dict) and imgs:
                name = imgs.get(lang) or imgs.get("english") or next(iter(imgs.values()))
                if isinstance(name, str):
                    out[kind + suffix] = name
    return out


# ---------------------------------------------------------------- Konto, Bibliotheken

def _login_user(ctx):
    """(steamid64, felder) des zuletzt angemeldeten Kontos aus loginusers.vdf oder (None, {})."""
    try:
        users = ci(_load_text_vdf(os.path.join(ctx.steam_root, "config/loginusers.vdf")), "users", default={})
    except Exception:
        return None, {}
    best = None
    for sid, rec in (users.items() if isinstance(users, dict) else []):
        if not isinstance(rec, dict) or not str(sid).isdigit():
            continue
        rank = (to_int(ci(rec, "MostRecent")), to_int(ci(rec, "Timestamp")))
        if best is None or rank > best[0]:
            best = (rank, int(sid), rec)
    return (best[1], best[2]) if best else (None, {})


def account_id(ctx) -> int | None:
    """Kontonummer (Ordnername unter userdata): loginusers.vdf, sonst der jüngste userdata-Ordner."""
    sid, _rec = _login_user(ctx)
    if sid and sid > STEAMID64_BASE:
        acct = sid - STEAMID64_BASE
        if os.path.isdir(os.path.join(ctx.steam_root, "userdata", str(acct))):
            return acct
    best = None
    try:
        names = os.listdir(os.path.join(ctx.steam_root, "userdata"))
    except OSError:
        return None
    for name in names:
        if not name.isdigit() or name == "0":
            continue
        try:
            mtime = os.stat(os.path.join(ctx.steam_root, "userdata", name, "config/localconfig.vdf")).st_mtime
        except OSError:
            mtime = 0.0
        if best is None or mtime > best[0]:
            best = (mtime, int(name))
    return best[1] if best else None


def steam_account(ctx) -> dict:
    """Für die Einstellungsseite „Steam“. Blockierend (eine kleine Datei). Nur harmlose Felder:

    {"name": "Nutzer", "logged_in_hint": True, "logged_in": True, "offline_mode": False,
     "account_id": 12345678, "last_login": 1791915366.0}
    logged_in_hint: Steam hat eine gemerkte Anmeldung (ob sie noch gilt, weiß nur der Client).
    logged_in ist derselbe Wert unter dem Namen, den die Einstellungsseite erwartet.
    Ohne loginusers.vdf: name "", logged_in_hint False, account_id None, last_login None.
    """
    sid, rec = _login_user(ctx)
    remember = str(ci(rec, "RememberPassword", default="0")) == "1"
    allow = str(ci(rec, "AllowAutoLogin", default="1")) != "0"
    stamp = to_int(ci(rec, "Timestamp"))
    hint = bool(sid) and remember and allow
    return {
        "name": clean_text(ci(rec, "PersonaName", default="")),
        "logged_in_hint": hint,
        "logged_in": hint,
        "offline_mode": str(ci(rec, "WantsOfflineMode", default="0")) == "1",
        "account_id": (sid - STEAMID64_BASE) if sid and sid > STEAMID64_BASE else None,
        "last_login": float(stamp) if stamp else None,
    }


def library_folders(ctx) -> list[dict]:
    """[{"path", "mounted": bool, "apps": {appid: bytes}}] aus libraryfolders.vdf, ohne Dubletten."""
    libs, seen = [], set()

    def add(path, apps):
        if not isinstance(path, str) or not path:
            return
        key = os.path.realpath(path)
        if key in seen:
            return
        seen.add(key)
        sizes = {}
        for k, v in (apps.items() if isinstance(apps, dict) else []):
            if str(k).isdigit():
                sizes[int(k)] = to_int(v)
        libs.append({"path": path, "mounted": os.path.isdir(os.path.join(path, "steamapps")), "apps": sizes})

    for rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
        path = os.path.join(ctx.steam_root, rel)
        if not os.path.isfile(path):
            continue
        try:
            folders = ci(_load_text_vdf(path), "libraryfolders", default=None)
            if not isinstance(folders, dict):        # vdf liest Müll auch als leeres Dokument
                raise ValueError("kein Abschnitt libraryfolders")
        except Exception as e:
            ctx.warn("steam: %s nicht lesbar (%s)" % (rel, err_text(e)))
            continue
        for _idx, rec in sorted(folders.items(), key=lambda kv: str(kv[0])) if isinstance(folders, dict) else []:
            if isinstance(rec, dict):
                add(ci(rec, "path"), ci(rec, "apps", default={}))
        if libs:
            break
    add(ctx.steam_root, {})                           # die Hauptbibliothek gilt auch ohne Liste
    return libs


def _playtimes(ctx, acct) -> dict:
    """{appid: (last_played, minuten)} aus localconfig.vdf. Nur diese zwei Felder verlassen die Funktion."""
    if acct is None:
        return {}
    path = os.path.join(ctx.steam_root, "userdata", str(acct), "config/localconfig.vdf")
    try:
        tree = _load_text_vdf(path)
    except FileNotFoundError:
        return {}
    except Exception as e:
        ctx.warn("steam: localconfig.vdf nicht lesbar (%s); Spielzeiten nur aus den appmanifest-Dateien" % err_text(e))
        return {}
    if ci(tree, "UserLocalConfigStore") is None:
        ctx.warn("steam: localconfig.vdf hat keinen erwarteten Inhalt; Spielzeiten nur aus den appmanifest-Dateien")
    apps = ci(tree, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", default={})
    out = {}
    for appid, rec in (apps.items() if isinstance(apps, dict) else []):
        if str(appid).isdigit() and isinstance(rec, dict):
            out[int(appid)] = (to_int(ci(rec, "LastPlayed")), to_int(ci(rec, "Playtime")))
    del tree, apps
    return out


def _compat_tools(ctx) -> dict:
    """{appid oder 0: werkzeugname} aus config.vdf (nur CompatToolMapping/<id>/name)."""
    path = os.path.join(ctx.steam_root, "config/config.vdf")
    try:
        tree = _load_text_vdf(path)
    except FileNotFoundError:
        return {}
    except Exception as e:
        ctx.warn("steam: config.vdf nicht lesbar (%s); Proton-Zuordnung unbekannt" % err_text(e))
        return {}
    if ci(tree, "InstallConfigStore") is None:
        ctx.warn("steam: config.vdf hat keinen erwarteten Inhalt; Proton-Zuordnung unbekannt")
    mapping = ci(tree, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", default={})
    out = {}
    for appid, rec in (mapping.items() if isinstance(mapping, dict) else []):
        name = ci(rec, "name")
        if str(appid).isdigit() and isinstance(name, str) and name:
            out[int(appid)] = name
    del tree, mapping
    return out


def compat_tool_mapping(steam_root: str) -> dict:
    """{appid oder 0: werkzeugname} aus <steam_root>/config/config.vdf, nur CompatToolMapping/<id>/name
    (E102: Eine Proton-Version, die Steam einem Spiel zuordnet, wird nicht entfernt). Fehlt die Datei: {}.
    Unlesbar oder ohne erwarteten Aufbau: OSError bzw. ValueError ohne die Meldung des Parsers (sie zitiert
    Zeilen, und in config.vdf stehen Anmeldedaten); der Aufrufer zählt das als belegt."""
    path = os.path.join(steam_root, "config/config.vdf")
    try:
        tree = _load_text_vdf(path)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise OSError(e.errno, "config.vdf nicht lesbar") from None
    except Exception as e:
        raise ValueError("config.vdf nicht lesbar (%s)" % type(e).__name__) from None
    if ci(tree, "InstallConfigStore") is None:
        raise ValueError("config.vdf hat keinen erwarteten Inhalt")
    mapping = ci(tree, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping", default={})
    out = {}
    for appid, rec in (mapping.items() if isinstance(mapping, dict) else []):
        name = ci(rec, "name")
        if str(appid).isdigit() and isinstance(name, str) and name:
            out[int(appid)] = name
    del tree, mapping
    return out


# ---------------------------------------------------------------- Sammlung „Favoriten“ (E99)

FAVORITES_KEY = "user-collections.favorite"


def favorite_ids(ctx, acct=None) -> set[int]:
    """App-Nummern in Steams Sammlung „Favoriten“ (Steam-Spiele und Verknüpfungen, vorzeichenlos).
    Nur gelesen; das Format ist nicht dokumentiert: Liste aus [schlüssel, {value: JSON-Text, …}],
    der Wert {"id": "favorite", "added": [...], "removed": [...]}. Fehler: leere Menge, still.
    Gemerkt am Kontext, neu gelesen nur bei geänderter Datei."""
    acct = account_id(ctx) if acct is None else acct
    if acct is None:
        return set()
    path = os.path.join(ctx.steam_root, "userdata", str(acct), "config/cloudstorage/cloud-storage-namespace-1.json")
    try:
        st = os.stat(path)
    except OSError:
        return set()
    stamp = (path, st.st_mtime_ns, st.st_size)
    cached = getattr(ctx, "_favorites_cache", None)
    if cached and cached[0] == stamp:
        return set(cached[1])
    found: set[int] = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        for item in (data if isinstance(data, list) else []):
            if not (isinstance(item, list) and len(item) == 2 and item[0] == FAVORITES_KEY
                    and isinstance(item[1], dict)) or item[1].get("is_deleted"):
                continue
            value = json.loads(item[1].get("value") or "{}")
            if isinstance(value, dict):
                removed = {to_int(v) & 0xFFFFFFFF for v in value.get("removed") or [] if to_int(v)}
                found = {to_int(v) & 0xFFFFFFFF for v in value.get("added") or [] if to_int(v)} - removed
    except (OSError, ValueError, TypeError, RecursionError):
        found = set()
    try:
        ctx._favorites_cache = (stamp, frozenset(found))
    except AttributeError:
        pass
    return found


# ---------------------------------------------------------------- Bilder

def _grid_index(ctx, acct) -> dict:
    """Dateien im grid-Ordner nach Stamm: {"3208209731p": pfad, "3208209731_hero": pfad, …}."""
    index: dict[str, str] = {}
    if acct is None:
        return index
    grid = os.path.join(ctx.steam_root, "userdata", str(acct), "config/grid")
    try:
        names = sorted(os.listdir(grid))
    except OSError:
        return index
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() in _ICON_EXT and (stem not in index or ext.lower() == ".png"):
            index[stem] = os.path.join(grid, name)
    return index


def _grid_art(index: dict, appid: int) -> dict:
    out = {}
    for kind, stem in (("cover", "%dp" % appid), ("wide", "%d" % appid), ("hero", "%d_hero" % appid),
                       ("logo", "%d_logo" % appid), ("icon", "%d_icon" % appid)):
        path = index.get(stem)
        if path and (kind == "icon" or path.lower().endswith(_IMAGE_EXT)):
            out[kind] = path
    return out


def _cache_art(ctx, appid: int, assets: dict, icon_hash: str = "") -> dict:
    """Bilder aus appcache/librarycache/<appid>/: erst der Name aus appinfo, dann alte und neue Muster."""
    base = os.path.join(ctx.steam_root, "appcache/librarycache", str(appid))
    files: dict[str, str] = {}                       # relativer Pfad klein -> echter Pfad
    try:
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if os.path.isdir(full):
                try:
                    for sub in sorted(os.listdir(full)):
                        files[("%s/%s" % (name, sub)).lower()] = os.path.join(full, sub)
                except OSError:
                    pass
            else:
                files[name.lower()] = full
    except OSError:
        return {}

    def pick(asset_key, *names):
        named = assets.get(asset_key)
        if isinstance(named, str) and named.lower() in files:
            return files[named.lower()]
        for want in names:
            if want in files:
                return files[want]
            hit = next((p for rel, p in files.items() if rel.endswith("/" + want)), None)
            if hit:
                return hit
        return None

    out = {
        "cover": pick("library_capsule", "library_600x900.jpg", "library_capsule.jpg"),
        "wide": pick("library_header", "library_header.jpg", "header.jpg"),
        "hero": pick("library_hero", "library_hero.jpg"),
        "logo": pick("library_logo", "logo.png"),
        "icon": files.get(("%s.jpg" % icon_hash).lower()) if icon_hash else None,
    }
    if not out["icon"]:                              # 32x32-Symbol: <sha1>.jpg direkt im App-Ordner
        out["icon"] = next((p for rel, p in files.items()
                            if "/" not in rel and re.fullmatch(r"[0-9a-f]{40}\.jpg", rel)), None)
    return {k: v for k, v in out.items() if v}


# ---------------------------------------------------------------- Quelle

def _manifests(ctx, libs) -> dict:
    """{appid: (bibliothek, AppState)} aller appmanifest_<id>.acf der eingehängten Bibliotheken."""
    found: dict[int, tuple] = {}
    for lib in libs:
        if not lib["mounted"]:
            continue
        steamapps = os.path.join(lib["path"], "steamapps")
        try:
            names = sorted(os.listdir(steamapps))
        except OSError as e:
            ctx.warn("steam: %s nicht lesbar (%s)" % (steamapps, err_text(e)))
            continue
        for name in names:
            m = re.fullmatch(r"appmanifest_(\d+)\.acf", name)       # Reste wie *.acf.123.tmp nicht
            if not m:
                continue
            try:
                state = ci(_load_text_vdf(os.path.join(steamapps, name)), "AppState", default=None)
                if not isinstance(state, dict):
                    raise ValueError("kein AppState")
            except Exception as e:
                ctx.warn("steam: %s nicht lesbar (%s), ausgelassen" % (name, err_text(e)))
                continue
            appid = int(m.group(1))
            old = found.get(appid)
            # Dieselbe App in zwei Bibliotheken (Rest einer Verschiebung): die vollständige gewinnt
            if old is None or (to_int(ci(state, "StateFlags")) & 4 and not to_int(ci(old[1], "StateFlags")) & 4):
                found[appid] = (lib, state)
    return found


def _install_dir(lib: dict, name) -> str:
    """Spielordner unter steamapps/common; "" bei leerem oder krummem Namen (sonst würde der
    FSR-Scan den ganzen Ordner common mit allen Spielen durchsuchen)."""
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return ""
    return os.path.join(lib["path"], "steamapps/common", name)


def load(ctx) -> list[Entry]:
    """Installierte, spielbare Steam-Apps. Blockierend; wirft nicht, Probleme stehen in ctx.warnings."""
    if not os.path.isdir(ctx.steam_root):
        ctx.warn("steam: Steam-Ordner fehlt (%s)" % ctx.steam_root)
        return []
    libs = library_folders(ctx)
    manifests = _manifests(ctx, libs)
    offline: dict[int, tuple] = {}                   # Apps in nicht eingehängten Bibliotheken
    for lib in libs:
        if not lib["mounted"]:
            for appid, size in lib["apps"].items():
                if appid not in manifests:
                    offline.setdefault(appid, (lib, size))

    info, typed = {}, True
    appinfo_path = os.path.join(ctx.steam_root, "appcache/appinfo.vdf")
    try:
        info = load_appinfo(appinfo_path, only=set(manifests) | set(offline))
    except Exception as e:
        typed = False
        ctx.warn("steam: appinfo.vdf nicht lesbar (%s); Werkzeuge werden nur am Namen erkannt" % err_text(e))

    acct = account_id(ctx)
    played = _playtimes(ctx, acct)
    compat = _compat_tools(ctx)
    grid = _grid_index(ctx, acct)
    favorites = favorite_ids(ctx, acct)
    out: list[Entry] = []

    def build(appid, lib, name, typ, install_dir, mounted, flags, size, manifest_last, ai):
        last, minutes = played.get(appid, (0, 0))
        last = last or manifest_last
        tool = compat.get(appid) or compat.get(0) or ""
        available = mounted and bool(install_dir) and os.path.isdir(install_dir)
        # Fehlt der Ordner (Platte weg), liefert der Scanner sein letztes bekanntes Ergebnis.
        scan = ctx.fsr.scan(install_dir) if install_dir else None
        tags = fsr_mod.tags_for(scan)
        if tags and not supports_fsr4(tool):
            tags = ["fsr4-needs-cachyos"]            # Spiel könnte FSR 4, das eingestellte Proton nicht
        assets = library_assets(ai)
        art = _cache_art(ctx, appid, assets, str(ci(ai, "common", "icon", default="") or ""))
        art.update(_grid_art(grid, appid))           # eigene Bilder des Nutzers gehen vor
        oslist = str(ci(ai, "common", "oslist", default="") or "")
        title = clean_text(name, "App %d" % appid)
        # Für art.py (E99): appinfo nur hier lesen; ohne Namen sucht art.py selbst in appinfo.vdf
        extra = {"library_assets": assets} if assets else {}
        if appid in favorites:
            extra["steam_favorite"] = True
        return Entry(
            id="steam:%d" % appid, title=title, kind="game", source="steam", badge="STEAM",
            sort_title=sort_title(title), available=available, installed=bool(flags & 4),
            tags=tags, last_played=float(last) if last else None, playtime=max(0, minutes) * 60,
            detail={
                "appid": appid, "install_dir": install_dir, "library": lib["path"],
                "compat_tool": tool, "size_on_disk": size, "steam_art": art,
                "type": typ, "state_flags": flags, "update_pending": bool(flags & 2),
                "native_linux": "linux" in [p.strip() for p in oslist.split(",")],
                "run_url": "steam://rungameid/%d" % appid,
                "fsr": fsr_mod.summary(scan), **extra,
            })

    for appid, (lib, state) in sorted(manifests.items()):
        ai = info.get(appid, {}).get("appinfo", {})
        name = ci(ai, "common", "name") or ci(state, "name") or ""
        typ = str(ci(ai, "common", "type", default="") or "").lower()
        if typ:
            if typ not in PLAYABLE_TYPES:
                continue
        elif _TOOL_NAME.search(str(name)):
            continue
        elif typed and info:
            ctx.warn("steam: App %d steht nicht in appinfo.vdf; als Spiel übernommen" % appid)
        install_dir = _install_dir(lib, ci(state, "installdir"))
        try:
            out.append(build(appid, lib, name, typ, install_dir, True, to_int(ci(state, "StateFlags")),
                             to_int(ci(state, "SizeOnDisk")), to_int(ci(state, "LastPlayed")), ai))
        except Exception as e:
            ctx.warn("steam: App %d ausgelassen (%s)" % (appid, err_text(e)))

    missing_libs = set()
    for appid, (lib, size) in sorted(offline.items()):
        ai = info.get(appid, {}).get("appinfo", {})
        typ = str(ci(ai, "common", "type", default="") or "").lower()
        name = ci(ai, "common", "name")
        if typ not in PLAYABLE_TYPES or not name:
            continue                                 # ohne Typ und Namen lieber kein Eintrag
        install_dir = _install_dir(lib, ci(ai, "config", "installdir"))
        try:
            out.append(build(appid, lib, name, typ, install_dir, False, 4, size, 0, ai))
            missing_libs.add(lib["path"])
        except Exception as e:
            ctx.warn("steam: App %d ausgelassen (%s)" % (appid, err_text(e)))
    for path in sorted(missing_libs):
        ctx.warn("steam: Bibliothek %s ist nicht eingehängt; ihre Spiele sind als nicht verfügbar markiert" % path)
    return out


# ---------------------------------------------------------------- Verknüpfungen (nur Bilder)

def _mtime_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _unquote(value) -> str:
    value = value if isinstance(value, str) else ""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def shortcut_art(ctx) -> list[dict]:
    """Nicht-Steam-Verknüpfungen mit ihren Bildern aus dem grid-Ordner. Blockierend.

    Bildquelle und „zuletzt gespielt“ für die Emulator- und Lutris-Spiele; was davon ein eigener
    Eintrag wird, entscheidet load_shortcuts(). Die Liste wird am Kontext gemerkt und nur neu
    gelesen, wenn sich shortcuts.vdf oder der grid-Ordner ändern; Aufrufer verändern sie nicht.
    Rückgabe je Verknüpfung:
      {"name": "The Legend of Zelda: Tears of the Kingdom", "exe": "/home/nutzer/.local/bin/eden-launch",
       "args": "totk", "appid": 3208209731, "last_played": 1791820527.0 | None,
       "grid": {"cover": pfad|None, "wide": …, "hero": …, "logo": …, "icon": …},
       "start_dir": "/home/nutzer/.local/bin/", "hidden": False, "collections": ["Lutris"],
       "flatpak": ""}
    """
    acct = account_id(ctx)
    if acct is None:
        return []
    path = os.path.join(ctx.steam_root, "userdata", str(acct), "config/shortcuts.vdf")
    # Mehrere Quellen fragen im selben Lauf: nur neu lesen, wenn sich Datei oder grid-Ordner ändern
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size, _mtime_ns(os.path.join(os.path.dirname(path), "grid")))
    except OSError:
        return []
    cached = getattr(ctx, "_shortcut_cache", None)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            data = vdf.binary_load(fh)
    except FileNotFoundError:
        return []
    except Exception as e:
        ctx.warn("steam: shortcuts.vdf nicht lesbar (%s); keine Bilder aus Verknüpfungen" % err_text(e))
        return []
    grid = _grid_index(ctx, acct)
    shortcuts = ci(data, "shortcuts", default={})
    out = []
    for _idx, sc in (shortcuts.items() if isinstance(shortcuts, dict) else []):
        if not isinstance(sc, dict):
            continue
        appid = to_int(ci(sc, "appid")) & 0xFFFFFFFF          # steht vorzeichenbehaftet in der Datei
        art = dict.fromkeys(("cover", "wide", "hero", "logo", "icon"))
        art.update(_grid_art(grid, appid))
        icon = _unquote(ci(sc, "icon"))
        if not art["icon"] and icon.lower().endswith(_ICON_EXT) and os.path.isfile(icon):
            art["icon"] = icon
        last = to_int(ci(sc, "LastPlayTime"))
        tags = ci(sc, "tags", default={})
        out.append({"name": clean_text(ci(sc, "AppName")), "exe": _unquote(ci(sc, "Exe")),
                    "args": ci(sc, "LaunchOptions") if isinstance(ci(sc, "LaunchOptions"), str) else "",
                    "appid": appid, "grid": art, "last_played": float(last) if last > 0 else None,
                    "start_dir": _unquote(ci(sc, "StartDir")), "hidden": to_int(ci(sc, "IsHidden")) != 0,
                    "collections": [clean_text(v) for v in tags.values() if isinstance(v, str)]
                    if isinstance(tags, dict) else [],
                    "flatpak": clean_text(ci(sc, "FlatpakAppID"))})
    try:
        ctx._shortcut_cache = (stamp, out)
    except AttributeError:
        pass
    return out


def _shortcut_matches(entry: Entry, shortcuts: list[dict]) -> list[dict]:
    """Alle Verknüpfungen, die zu einem Eintrag gehören, die sicherste Zuordnung zuerst:
    Lutris-Slug oder -Nummer im Aufruf, dieselbe exe (realpath), gleicher Titel. Der Titel ist der
    der Quelle: Steam kennt den eigenen Namen nicht (E90; der Bilder-Thread fragt mit umbenannten
    Einträgen, E99)."""
    d = entry.detail
    slug, lutris_id = d.get("slug"), d.get("lutris_id")
    exe = d.get("exe_path") or d.get("exe") or ""
    key = title_key(source_title(entry))
    by_call, by_exe, by_name = [], [], []
    for s in shortcuts:
        base, args = os.path.basename(s["exe"]), s["args"].strip()
        if slug and ((base == "lutris-steam-launch" and args == slug) or (
                base == "lutris" and args in ("lutris:rungame/%s" % slug, "lutris:rungameid/%s" % lutris_id))):
            by_call.append(s)
        elif exe and os.path.isabs(exe) and os.path.isabs(s["exe"]) and not args and same_path(s["exe"], exe):
            by_exe.append(s)
        elif key and title_key(s["name"]) == key:
            by_name.append(s)
    return by_call + by_exe + by_name


def match_shortcut(entry: Entry, shortcuts: list[dict]) -> dict | None:
    """Die Verknüpfung, deren Bilder zu einem Bibliothekseintrag passen, oder None. Rein, schnell.
    Verknüpfungen ganz ohne Bilder zählen nicht."""
    return next((s for s in _shortcut_matches(entry, shortcuts) if any(s["grid"].values())), None)


def apply_shortcut_favorites(entries: list[Entry], shortcuts: list[dict], favorites: set[int]) -> int:
    """Lutris-, Emulator- und Archiv-Spiele, deren Steam-Verknüpfung in Steams „Favoriten“ steht,
    bekommen detail.steam_favorite (E99). Rein, schnell. Rückgabe: Anzahl markierter Einträge."""
    if not favorites:
        return 0
    marked = 0
    for entry in entries:
        if entry.source not in ("proton", "native", "switch", "wiiu", "local"):
            continue
        if any(s["appid"] in favorites for s in _shortcut_matches(entry, shortcuts)):
            entry.detail["steam_favorite"] = True
            marked += 1
    return marked


def apply_shortcut_last_played(entries: list[Entry], shortcuts: list[dict]) -> int:
    """Übernimmt „zuletzt gespielt“ aus den Verknüpfungen, über die Emulator- und Lutris-Spiele
    bisher gestartet wurden (das weiß sonst niemand). Ändert nur last_played, und nur nach oben.
    Rückgabe: Anzahl geänderter Einträge. Rein, schnell."""
    changed = 0
    for entry in entries:
        if entry.source not in ("proton", "native", "switch", "wiiu", "local"):
            continue
        stamps = [s["last_played"] for s in _shortcut_matches(entry, shortcuts) if s.get("last_played")]
        if stamps and max(stamps) > (entry.last_played or 0):
            entry.last_played = max(stamps)
            changed += 1
    return changed


# ---------------------------------------------------------------- Verknüpfungen als Einträge (E25)

SHORTCUT_PREFIX = "steam:shortcut:"
SHORTCUT_FLAG = 0x02000000          # untere Hälfte der Spielkennung einer Verknüpfung

# Gründe, aus denen eine Verknüpfung KEIN eigener Eintrag wird (shortcut_exclusion)
EXCLUDE_EMPTY = "empty"             # ohne Namen oder ohne Programm
EXCLUDE_HIDDEN = "hidden"           # in Steam ausgeblendet
EXCLUDE_DESKTOP = "desktop"         # nested-desktop: dafür gibt es die App „Desktop“
EXCLUDE_OWN_APP = "own-app"         # unsere eigenen Apps (YouTube, Kodi, Browser, EmuDeck, Steam, couch)
EXCLUDE_EMULATOR = "emulator"       # Emulator-Starter: die Spiele liefert die Emulator-Quelle
EXCLUDE_LUTRIS = "lutris"           # Aufruf eines Lutris-Spiels: das liefert die Lutris-Quelle
EXCLUDE_ASSIGNED = "assigned"       # gehört schon zu einem anderen Eintrag (Aufruf, exe, Titel)
EXCLUDE_SYSTEM = "system-tool"      # Programm des Systems (Dateiverwaltung …), kein Spiel

_DESKTOP_STARTERS = {"nested-desktop", "steamos-nested-desktop", "couch-desktop"}
_OWN_PROGRAMS = {"steam", "steam.sh", "couch-session", "couch-home", "couch-desktop", "emudeck.appimage"}
_LUTRIS_PROGRAMS = {"lutris", "lutris-steam-launch", "lutris-wrapper"}
_LUTRIS_FLATPAK = "net.lutris.Lutris"
# Emulatoren und ihre Werkzeuge, am Anfang des Programm- bzw. Flatpak-Namens erkannt
_EMULATOR_NAMES = (
    "eden", "cemu", "yuzu", "suyu", "sudachi", "citron", "torzu", "ryujinx", "ryubing", "dolphin-emu",
    "primehack", "rpcs3", "retroarch", "pcsx2", "ppsspp", "duckstation", "citra", "lime3ds", "azahar",
    "melonds", "xemu", "xenia", "mame", "mgba", "flycast", "shadps4", "vita3k", "scummvm", "es-de",
    "emulationstation", "steam-rom-manager", "srm", "emudeck", "bigpemu", "ares", "simple64", "rmg",
)
_EMULATOR_FLATPAK = re.compile(
    r"(?i)(^|\.)(eden|cemu|yuzu|suyu|ryujinx|dolphin[-_]?emu|primehack|rpcs3|retroarch|pcsx2|ppsspp|"
    r"duckstation|citra|lime3ds|azahar|melonds|xemu|xenia|mame|mgba|flycast|shadps4|vita3k|scummvm|"
    r"es[-_]de|steam[-_]rom[-_]manager|emudeck|rmg|ares)$")
_SYSTEM_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/libexec/", "/usr/local/bin/",
                "/usr/lib/", "/usr/lib64/")
# Hüllen, hinter denen das eigentliche Programm in den Startoptionen steht
_WRAPPERS = {"env", "bash", "sh", "python", "python3", "gamemoderun", "mangohud", "gamescope",
             "umu-run", "wine", "proton", "flatpak"}


def shortcut_gameid(appid: int) -> int:
    """Die Nummer für steam://rungameid/… einer Verknüpfung: (appid << 32) | 0x02000000."""
    return ((int(appid) & 0xFFFFFFFF) << 32) | SHORTCUT_FLAG


def _program(sc: dict) -> str:
    return os.path.basename(sc.get("exe") or "").lower()


def _flatpak_ids(sc: dict) -> list[str]:
    """Flatpak-Kennungen, die diese Verknüpfung startet (Feld FlatpakAppID oder „flatpak run <id>“)."""
    ids = [sc["flatpak"]] if sc.get("flatpak") else []
    if _program(sc) == "flatpak":
        words = (sc.get("args") or "").split()
        if "run" in words:
            ids += [w for w in words[words.index("run") + 1:] if not w.startswith("-") and w.count(".") >= 2]
    return ids


def _is_emulator_name(name: str) -> bool:
    name = name.lower()
    if name.endswith((".exe", ".bat")):
        return False            # ein Windows-Spiel darf „Ares.exe“ heißen; Emulatoren hier nie
    name = re.sub(r"\.(appimage|sh)$", "", name)
    return any(name == n or name.startswith((n + "-", n + "_", n + ".")) for n in _EMULATOR_NAMES)


def _lutris_call(sc: dict) -> bool:
    return (_program(sc) in _LUTRIS_PROGRAMS or "lutris:rungame" in (sc.get("args") or "")
            or _LUTRIS_FLATPAK in _flatpak_ids(sc))


def shortcut_exclusion(ctx, sc: dict, others=()) -> str | None:
    """Warum diese Verknüpfung kein eigener Eintrag wird (eine der EXCLUDE_-Kennungen), oder None.

    others: die Einträge aller anderen Quellen dieses Laufs. Rein bis auf realpath beim Vergleich
    zweier Programmpfade. Die Regeln stehen in Bauplan E25; dazu kommen in Steam ausgeblendete
    Verknüpfungen und Programme des Systems (/usr/bin/dolphin ist kein Spiel).
    """
    from . import apps as apps_mod
    from . import lutris as lutris_mod
    exe = sc.get("exe") or ""
    prog = _program(sc)
    if not sc.get("name") or not exe:
        return EXCLUDE_EMPTY
    if sc.get("hidden"):
        return EXCLUDE_HIDDEN
    if prog in _DESKTOP_STARTERS:
        return EXCLUDE_DESKTOP
    flatpaks = _flatpak_ids(sc)
    # nur die ersten vier Felder (id, Titel, Art, Ziel): die Tabelle darf hinten wachsen (E105)
    own_flatpaks = {a[3] for a in apps_mod.APPS if a[2] == "flatpak"}
    own_paths = [os.path.join(ctx.home, a[3]) for a in apps_mod.APPS if a[2] == "appimage"]
    app_dir = getattr(ctx, "app_dir", "") or ""
    if prog in _OWN_PROGRAMS or any(f in own_flatpaks for f in flatpaks) \
            or any(os.path.isabs(exe) and same_path(exe, p) for p in own_paths) \
            or (app_dir and os.path.isabs(exe) and os.path.realpath(exe).startswith(
                os.path.realpath(app_dir).rstrip(os.sep) + os.sep)):
        return EXCLUDE_OWN_APP
    if prog in lutris_mod.EMULATOR_STARTERS or _is_emulator_name(prog) \
            or any(_EMULATOR_FLATPAK.search(f) for f in flatpaks) \
            or "/Emulation/tools/" in exe or "/Emulation/roms/" in (sc.get("args") or ""):
        return EXCLUDE_EMULATOR
    if _lutris_call(sc):
        return EXCLUDE_LUTRIS
    for entry in others:
        if getattr(entry, "id", "").startswith(SHORTCUT_PREFIX):
            continue
        if entry.source in ("switch", "wiiu"):
            # Starter und ROM im Aufruf: gehört zum Emulator-Spiel
            starter, rom = entry.detail.get("starter"), entry.detail.get("rom")
            if rom and rom in (sc.get("args") or ""):
                return EXCLUDE_ASSIGNED
            if starter and os.path.isabs(exe) and os.path.isabs(str(starter)) and same_path(exe, starter):
                return EXCLUDE_EMULATOR
        if _shortcut_matches(entry, [sc]):
            return EXCLUDE_ASSIGNED
    if os.path.isabs(exe) and exe.startswith(_SYSTEM_DIRS) and prog not in _WRAPPERS:
        return EXCLUDE_SYSTEM
    return None


def load_shortcuts(ctx, others=()) -> list[Entry]:
    """Verknüpfungen, die zu keinem anderen Eintrag gehören, als Einträge (E25). Blockierend
    (shortcuts.vdf, config.vdf, je Windows-Spiel der FSR-Scan); wirft nicht.

    others: Einträge der übrigen Quellen (sources.run_source gibt die des laufenden Laufs)."""
    from . import lutris as lutris_mod
    shortcuts = shortcut_art(ctx)
    if not shortcuts:
        return []
    others = list(others)
    favorites = favorite_ids(ctx)
    compat = None
    mounts = None
    out: list[Entry] = []
    seen: set[int] = set()
    for sc in shortcuts:
        try:
            if sc["appid"] in seen or shortcut_exclusion(ctx, sc, others):
                continue
            seen.add(sc["appid"])
            if compat is None:
                compat = _compat_tools(ctx)
                mounts = ctx.fsr.mounts()
            exe, start_dir = sc["exe"], sc.get("start_dir") or ""
            windows = exe.lower().endswith((".exe", ".bat", ".msi"))
            # Steam führt die Zuordnung unter der vorzeichenlosen Nummer der Verknüpfung
            tool = compat.get(sc["appid"]) or (compat.get(0) if windows else "") or ""
            game_dir = ""
            if os.path.isabs(exe):
                game_dir = lutris_mod.game_root(exe, start_dir if os.path.isabs(start_dir) else "",
                                                mounts, ctx.home)
                if game_dir.startswith(_SYSTEM_DIRS) or same_path(game_dir, ctx.home):
                    game_dir = ""
            scan = ctx.fsr.scan(game_dir) if (windows and game_dir) else None
            tags = fsr_mod.tags_for(scan)
            if tags and not supports_fsr4(tool):
                tags = ["fsr4-needs-cachyos"]
            gameid = shortcut_gameid(sc["appid"])
            title = clean_text(sc["name"], "Verknüpfung %d" % sc["appid"])
            out.append(Entry(
                id="%s%d" % (SHORTCUT_PREFIX, sc["appid"]), title=title, kind="game", source="steam",
                badge="STEAM", sort_title=sort_title(title),
                available=os.path.exists(exe) if os.path.isabs(exe) else True, installed=True,
                tags=tags, last_played=sc.get("last_played"),
                detail={
                    "shortcut": True, "shortcut_appid": sc["appid"], "gameid": str(gameid),
                    "run_url": "steam://rungameid/%d" % gameid, "exe": exe, "args": sc.get("args") or "",
                    "start_dir": start_dir, "game_dir": game_dir, "compat_tool": tool,
                    "steam_art": {k: v for k, v in sc["grid"].items() if v},
                    "collections": list(sc.get("collections") or []), "fsr": fsr_mod.summary(scan),
                    **({"steam_favorite": True} if sc["appid"] in favorites else {}),
                }))
        except Exception as e:
            ctx.warn("shortcuts: Verknüpfung %s ausgelassen (%s)" % (sc.get("appid"), err_text(e)))
    return out
