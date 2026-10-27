"""Steam-Trophäen (E124): Errungenschaften aus Steams Zwischenspeicher, ihre Seltenheit und Symbole.

Wunsch 2026-10-27: Fortschritt je Spiel, Seltenheit, Stufen und
eine Mitteilung beim Freischalten. Steam legt die Errungenschaften selbst ab; dieses Modul liest sie nur (SAFETY 5
und 13: Steam-Dateien nie schreiben). Nicht-Steam-Spiele haben keine.

Dateien unter <steam>/appcache/stats, binäres KeyValues (parse_kv):
    UserGameStatsSchema_<appid>.bin      <appid> → stats → <statid> → type (4 bzw. "ACHIEVEMENTS") → bits → <bit> →
                                         {name (API-Name), display → {name {english, german, …}, desc {…}, hidden,
                                         icon, icon_gray}}
    UserGameStats_<konto>_<appid>.bin    cache → <statid> → {data (Bitfeld der freigeschalteten), AchievementTimes
                                         {<bit>: Unixzeit}}; bei Zahl-Werten (Typ INT) ist data kein Bitfeld
Konto = SteamID64 − 76561197960265728 aus loginusers.vdf (MostRecent, library.steam.account_id), sonst die zuletzt
geschriebene Nutzerdatei. Geprüft am 2026-10-27 an 47 Spielen dieses Rechners (Resident Evil Village: 25 von 56).

Seltenheit: GetGlobalAchievementPercentagesForApp (öffentlich, ohne Schlüssel; percent als Text), je Spiel 7 Tage im
Zwischenspeicher <cache>/trophies (store/cache.py; ohne Netz gilt der letzte Stand, 403 für unbekannte Spiele zählt
als „keine Angaben“). Stufen (eigene Festlegung): unter 10 % Gold, unter 35 % Silber, sonst Bronze; alle eines
Spiels = Platin. Ohne Seltenheit keine Stufe (tier None).

Symbole: Steams CDN (ICON_URL; 2026-10-27 geprüft: 64x64 JPEG, Namen als sha1 oder wie bei Portal 2 „WAKE_UP.jpg“).
Geladen erst auf Nachfrage der Seite (icons) nach <cache>/trophies/icons/<appid>/<name>, ausgeliefert über
couch://art (wiring_trophies meldet den Ordner als Wurzel an). Verborgene, gesperrte Trophäen verraten weder Namen
noch Beschreibung noch Symbol, bis sie frei sind.

Gemerkter Stand für die Mitteilungen: <state>/trophies-seen.json (Seen). Alles hier blockiert (Platte, Netz): im
Arbeits-Thread rufen (wiring_trophies, Lane „couch-trophies“).
"""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable

from . import jsontext, log
from .store import cache as store_cache
from .store.cache import StoreError

_log = log.get("trophies")

STEAMID64_BASE = 76561197960265728
API = "https://api.steampowered.com"
CDN = "https://cdn.cloudflare.steamstatic.com"
PCT_PATH = "/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v0002/?gameid=%d&format=json"
ICON_PATH = "/steamcommunity/public/images/apps/%d/%s"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"
TIMEOUT_S = 10.0
PCT_TTL = 7 * 86400.0
OFFLINE_PAUSE_S = 300.0         # nach einem Abruf ohne Netz so lange nicht weiter fragen
FETCH_GAP_S = 0.25              # zwischen zwei Abrufen der Seltenheit (Übersicht mit vielen Spielen)
GOLD_BELOW, SILVER_BELOW = 10.0, 35.0
TIERS = ("bronze", "silver", "gold")
LANGS = {"de": "german", "en": "english"}
MAX_FILE = 16 * 1024 * 1024     # größte Datei bisher 0,55 MB (Company of Heroes 2, 452 Errungenschaften)
MAX_ICON = 256 * 1024
MAX_DEPTH = 32
ICONS_PER_CALL = 24
SINCE_SLACK_S = 120.0           # Uhr von Steam und Start der Sitzung liegen nicht genau gleich
MARKER = ".couch-trophies"
SEEN_FILE = "trophies-seen.json"
_ICON_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,120}\.(?:jpg|jpeg|png)")
_USER_FILE = re.compile(r"UserGameStats_(\d{1,10})_(\d{1,10})\.bin")
_SCHEMA_FILE = re.compile(r"UserGameStatsSchema_(\d{1,10})\.bin")

# Typ-Bytes des binären KeyValues
T_NODE, T_STRING, T_INT32, T_FLOAT, T_UINT64, T_END, T_INT64 = 0, 1, 2, 3, 7, 8, 10


class KvError(ValueError):
    """Datei nicht lesbar: unbekannter Typ (4, 5, 6 …), zu kurz, zu tief."""


# ---------------------------------------------------------------- binäres KeyValues
def _cstr(data: bytes, pos: int) -> tuple[str, int]:
    end = data.find(b"\0", pos)
    if end < 0:
        raise KvError("Zeichenkette ohne Ende bei Byte %d" % pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def _node(data: bytes, pos: int, depth: int) -> tuple[dict, int]:
    if depth > MAX_DEPTH:
        raise KvError("zu tief verschachtelt")
    out: dict = {}
    size = len(data)
    while pos < size:
        kind = data[pos]
        pos += 1
        if kind == T_END:
            return out, pos
        key, pos = _cstr(data, pos)
        if kind == T_NODE:
            value, pos = _node(data, pos, depth + 1)
        elif kind == T_STRING:
            value, pos = _cstr(data, pos)
        elif kind in (T_INT32, T_FLOAT, T_UINT64, T_INT64):
            fmt = {T_INT32: "<i", T_FLOAT: "<f", T_UINT64: "<Q", T_INT64: "<q"}[kind]
            width = struct.calcsize(fmt)
            if pos + width > size:
                raise KvError("Datei endet in einem Wert")
            (value,) = struct.unpack_from(fmt, data, pos)
            pos += width
        else:
            # 4 (wstring), 5 (Farbe), 6 (Zeiger) und Unbekanntes: lieber nichts als geraten
            raise KvError("Typ %d bei Byte %d" % (kind, pos - 1))
        out[key] = value
    if depth:
        raise KvError("Datei endet mitten in einem Unterbaum")
    return out, pos


def parse_kv(data: bytes) -> dict:
    """Eine ganze Datei; die oberste Ebene endet mit 0x08 oder am Dateiende."""
    return _node(bytes(data), 0, 0)[0]


def read_kv(path: str) -> dict:
    with open(path, "rb") as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise KvError("Datei größer als %d Bytes" % MAX_FILE)
    return parse_kv(data)


# ---------------------------------------------------------------- Schema und Stand
def _texts(value: Any) -> dict[str, str]:
    """{sprache: text} ohne das Kürzel „token“; ein einzelner Text gilt als Englisch."""
    if isinstance(value, str):
        return {"english": value} if value else {}
    if not isinstance(value, dict):
        return {}
    return {str(k): v.strip() for k, v in value.items() if isinstance(v, str) and v.strip() and k != "token"}


def pick(texts: dict, lang: str) -> str:
    """Text in der Sprache der Oberfläche, sonst Englisch, sonst irgendeiner."""
    want = LANGS.get(lang, "english")
    return texts.get(want) or texts.get("english") or next(iter(texts.values()), "")


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip() not in ("", "0", "false")


def _icon(value: Any) -> str:
    name = str(value or "")
    return name if _ICON_NAME.fullmatch(name) and ".." not in name else ""


def _num(text: Any) -> tuple:
    return (int(text), "") if str(text).isdigit() else (1 << 40, str(text))


def achievements(tree: dict, appid: int) -> list[dict]:
    """Errungenschaften eines Schemas in Steams Reihenfolge (Stat, dann Bit)."""
    root = tree.get(str(appid)) if isinstance(tree, dict) else None
    if not isinstance(root, dict):
        root = next((v for v in (tree or {}).values() if isinstance(v, dict)), {})
    stats = root.get("stats")
    out: list[dict] = []
    for sid, st in sorted((stats or {}).items() if isinstance(stats, dict) else (), key=lambda kv: _num(kv[0])):
        if not isinstance(st, dict) or str(st.get("type")) not in ("4", "ACHIEVEMENTS"):
            continue
        bits = st.get("bits")
        for bit, b in sorted(bits.items() if isinstance(bits, dict) else (), key=lambda kv: _num(kv[0])):
            if not str(bit).isdigit() or int(bit) > 31 or not isinstance(b, dict):
                continue
            disp = b.get("display") if isinstance(b.get("display"), dict) else {}
            out.append({"key": "%s:%d" % (sid, int(bit)), "stat": str(sid), "bit": int(bit),
                        "api": str(b.get("name") or ""), "names": _texts(disp.get("name")),
                        "descs": _texts(disp.get("desc")), "hidden": _flag(disp.get("hidden")),
                        "icon": _icon(disp.get("icon")), "icon_gray": _icon(disp.get("icon_gray"))})
    return out


def game_name(tree: dict, appid: int) -> str:
    """„gamename“ des Schemas; Steams Platzhalter („ValveTestApp…“) zählen nicht."""
    root = tree.get(str(appid)) if isinstance(tree, dict) else None
    name = str((root or {}).get("gamename") or "") if isinstance(root, dict) else ""
    return "" if not name or name.startswith("ValveTestApp") or name.startswith("StudioSFApp") else name


def unlocked(tree: dict, items: list[dict]) -> dict[str, int]:
    """{key: Unixzeit (0 = unbekannt)} der freigeschalteten. Das Bitfeld data entscheidet; fehlt es, zählt eine Zeit."""
    cache = tree.get("cache") if isinstance(tree, dict) else None
    if not isinstance(cache, dict):
        return {}
    out: dict[str, int] = {}
    for item in items:
        st = cache.get(item["stat"])
        if not isinstance(st, dict):
            continue
        data = st.get("data")
        times = st.get("AchievementTimes") if isinstance(st.get("AchievementTimes"), dict) else {}
        stamp = times.get(str(item["bit"]))
        stamp = int(stamp) if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp > 0 else 0
        if isinstance(data, int) and not isinstance(data, bool):
            got = (data & 0xFFFFFFFF) >> item["bit"] & 1
        else:
            got = stamp > 0
        if got:
            out[item["key"]] = stamp
    return out


def tier_of(percent: float | None) -> str | None:
    if percent is None:
        return None
    return "gold" if percent < GOLD_BELOW else "silver" if percent < SILVER_BELOW else "bronze"


def parse_percentages(raw: Any) -> dict[str, float]:
    """Antwort von GetGlobalAchievementPercentagesForApp -> {API-Name: Prozent}."""
    node = raw.get("achievementpercentages") if isinstance(raw, dict) else None
    items = node.get("achievements") if isinstance(node, dict) else None
    out: dict[str, float] = {}
    for it in items if isinstance(items, list) else ():
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            continue
        try:
            value = float(it.get("percent"))
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 100.0:
            out[it["name"]] = value
    return out


# ---------------------------------------------------------------- Orte
def stats_dir(root: str) -> str:
    return os.path.join(root, "appcache", "stats")


def schema_path(root: str, appid: int) -> str:
    return os.path.join(stats_dir(root), "UserGameStatsSchema_%d.bin" % appid)


def user_path(root: str, account: int, appid: int) -> str:
    return os.path.join(stats_dir(root), "UserGameStats_%d_%d.bin" % (account, appid))


def account_of(root: str) -> int | None:
    """Kontonummer der Nutzerdateien: das zuletzt angemeldete Konto (loginusers.vdf), wenn es Dateien hat, sonst das
    Konto der zuletzt geschriebenen Datei."""
    try:
        names = os.listdir(stats_dir(root))
    except OSError:
        return None
    newest: dict[int, float] = {}
    for name in names:
        m = _USER_FILE.fullmatch(name)
        if not m:
            continue
        try:
            mtime = os.stat(os.path.join(stats_dir(root), name)).st_mtime
        except OSError:
            continue
        acct = int(m.group(1))
        newest[acct] = max(newest.get(acct, 0.0), mtime)
    if not newest:
        return None
    try:
        from .library import steam
        login = steam.account_id(SimpleNamespace(steam_root=root))
    except Exception:  # noqa: BLE001 (loginusers.vdf kaputt: dann nach den Dateien)
        login = None
    if login in newest:
        return login
    return max(newest, key=lambda a: newest[a])


def schema_appids(root: str) -> list[int]:
    try:
        names = os.listdir(stats_dir(root))
    except OSError:
        return []
    return sorted(int(m.group(1)) for m in (_SCHEMA_FILE.fullmatch(n) for n in names) if m and int(m.group(1)) > 0)


def _http_get(url: str, timeout: float = TIMEOUT_S, limit: int = 4 * 1024 * 1024) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, image/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (Adressen aus festen Teilen)
        return resp.read(limit + 1)


def _image_kind(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return ""


# ---------------------------------------------------------------- gemerkter Stand
class Seen:
    """Was schon gemeldet ist: {"apps": {"<appid>": {"keys": [...], "all": bool}}}. Ein Spiel ohne Eintrag hat noch
    keinen Stand (beim ersten Blick wird er nur gemerkt, nicht gemeldet)."""

    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, SEEN_FILE)
        self.lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            _log.warning("%s unlesbar, beginne neu: %s", self.path, e)
            return {}
        apps = data.get("apps") if isinstance(data, dict) else None
        return apps if isinstance(apps, dict) else {}

    def get(self, appid: int) -> tuple[set, bool] | None:
        node = self._load().get(str(appid))
        if not isinstance(node, dict) or not isinstance(node.get("keys"), list):
            return None
        return {k for k in node["keys"] if isinstance(k, str)}, bool(node.get("all"))

    def put(self, appid: int, keys: set, done: bool) -> None:
        apps = self._load()
        apps[str(appid)] = {"keys": sorted(keys), "all": bool(done)}
        try:
            from . import system
            system.write_atomic(self.path, jsontext.dumps({"apps": apps}, indent=1, sort_keys=True) + "\n")
        except OSError as e:
            _log.warning("%s nicht schreibbar: %s", self.path, e)


# ---------------------------------------------------------------- Dienst
class Trophies:
    """Lesen, Seltenheit, Symbole, Stand für Mitteilungen. steam_root: Pfad oder Funktion, die ihn liefert (None =
    kein Steam). get(url) -> Bytes; None = kein Netz (Prüfmodus ohne Attrappe). api/cdn: Basis der Adressen (im
    Prüfmodus die Attrappe auf 127.0.0.1)."""

    def __init__(self, steam_root: Any, cache_dir: str, state_dir: str, get: Callable[[str], bytes] | None = _http_get,
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, api: str = API, cdn: str = CDN,
                 art_url: Callable[[str], str] | None = None):
        self._root = steam_root
        self.cache_dir = cache_dir
        self.icon_dir = os.path.join(cache_dir, "icons")
        self._get = get
        self._clock = clock
        self._mono = monotonic
        self._sleep = sleep
        self.api = api.rstrip("/")
        self.cdn = cdn.rstrip("/")
        self._art_url = art_url or (lambda p: "couch://art/" + urllib.parse.quote(os.path.abspath(p), safe=""))
        self.seen = Seen(state_dir)
        self._lock = threading.Lock()
        self._memo: dict[str, tuple] = {}       # Pfad -> (Kennung der Datei, Ergebnis)
        self._offline_until = -1e9
        self._last_fetch = -1e9

    # ------------------------------------------------------------ Orte und Dateien
    def root(self) -> str | None:
        value = self._root() if callable(self._root) else self._root
        return value if isinstance(value, str) and value and os.path.isdir(stats_dir(value)) else None

    def ensure_cache(self) -> bool:
        """Zwischenspeicher samt Marke anlegen (vor dem Anmelden als Bilder-Wurzel). False = nicht benutzbar."""
        try:
            if os.path.islink(self.cache_dir):
                return False
            os.makedirs(self.icon_dir, exist_ok=True)
            marker = os.path.join(self.cache_dir, MARKER)
            if not os.path.isfile(marker):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("Seltenheit und Symbole der Steam-Trophäen (PC5 Launcher). Inhalt darf gelöscht werden.\n")
            return True
        except OSError as e:
            _log.warning("Trophäen: Zwischenspeicher %s nicht anlegbar: %s", self.cache_dir, e)
            return False

    def _parsed(self, path: str, fn: Callable[[dict], Any]) -> Any:
        """fn(Baum) einer Datei, gemerkt nach mtime und Größe; None, wenn sie fehlt oder kaputt ist."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._memo.get(path)
        if hit and hit[0] == sig:
            return hit[1]
        try:
            value = fn(read_kv(path))
        except (OSError, KvError) as e:
            _log.info("Trophäen: %s nicht lesbar (%s)", os.path.basename(path), e)
            value = None
        with self._lock:
            self._memo[path] = (sig, value)
        return value

    def _schema(self, root: str, appid: int) -> dict | None:
        return self._parsed(schema_path(root, appid), lambda t: {"items": achievements(t, appid), "name": game_name(t, appid)})

    def _account(self, root: str) -> int | None:
        """account_of, gemerkt bis sich der Ordner ändert (die Übersicht fragt es sonst je Spiel neu)."""
        try:
            sig = os.stat(stats_dir(root)).st_mtime_ns
        except OSError:
            return None
        key = "account:" + root
        with self._lock:
            hit = self._memo.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        acct = account_of(root)
        with self._lock:
            self._memo[key] = (sig, acct)
        return acct

    def user_file(self, appid: int) -> str | None:
        root = self.root()
        acct = self._account(root) if root else None
        return user_path(root, acct, appid) if acct is not None else None

    def state(self, appid: int) -> tuple[list[dict], dict[str, int], str] | None:
        """(Errungenschaften, freigeschaltete {key: zeit}, Name aus dem Schema) oder None (keine Errungenschaften)."""
        root = self.root()
        if root is None or not isinstance(appid, int) or appid <= 0:
            return None
        schema = self._schema(root, appid)
        if not schema or not schema["items"]:
            return None
        items = schema["items"]
        path = self.user_file(appid)
        got = (self._parsed(path, lambda t: unlocked(t, items)) if path else None) or {}
        return items, got, schema["name"]

    # ------------------------------------------------------------ Netz
    def _net(self, url: str) -> bytes:
        if self._get is None:
            raise StoreError("kein Netz")
        wait = self._last_fetch + FETCH_GAP_S - self._mono()
        if wait > 0:
            self._sleep(wait)
        self._last_fetch = self._mono()
        return self._get(url)

    def rarity(self, appid: int, fetch: bool = True) -> dict[str, float]:
        """{API-Name: Prozent}; ohne Netz der letzte Stand, sonst {}. fetch False: nur der Zwischenspeicher."""
        key = "pct-%d" % appid
        path = store_cache.path_for(self.cache_dir, key)

        def stored() -> dict[str, float]:
            try:
                with open(path, encoding="utf-8") as f:
                    return parse_percentages(json.load(f))
            except (OSError, ValueError):
                return {}

        if not fetch or self._get is None or self._mono() < self._offline_until:
            return stored()

        def get() -> Any:
            try:
                return json.loads(self._net(self.api + PCT_PATH % appid).decode("utf-8"))
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
                if code in (400, 403, 404):
                    return {"achievementpercentages": {"achievements": []}}     # Steam kennt das Spiel nicht
                raise

        def failed(_error: BaseException) -> None:
            # Kein Netz oder Steam bremst: eine Weile gar nicht fragen (die Übersicht hat sonst je Spiel eine Wartezeit)
            self._offline_until = self._mono() + OFFLINE_PAUSE_S

        try:
            return parse_percentages(store_cache.cached(self.cache_dir, key, get, PCT_TTL, clock=self._clock,
                                                        unreachable="Steam ist gerade nicht erreichbar.", on_error=failed))
        except StoreError:
            return {}

    def _icon_file(self, appid: int, name: str) -> str:
        return os.path.join(self.icon_dir, str(appid), name)

    def icon_url(self, appid: int, name: str) -> str | None:
        if not name:
            return None
        path = self._icon_file(appid, name)
        return self._art_url(path) if os.path.isfile(path) else None

    def _load_icon(self, appid: int, name: str) -> str | None:
        """Ein Symbol vom CDN in den Zwischenspeicher; nur JPEG/PNG bis MAX_ICON."""
        path = self._icon_file(appid, name)
        if os.path.isfile(path):
            return self._art_url(path)
        if self._get is None or self._mono() < self._offline_until or not self.ensure_cache():
            return None
        try:
            data = self._net(self.cdn + ICON_PATH % (appid, urllib.parse.quote(name)))
        except urllib.error.HTTPError as e:
            e.close()
            return None
        except Exception as e:  # noqa: BLE001 (kein Netz: eine Weile nicht weiter fragen)
            _log.info("Trophäen: Symbol %s/%s nicht geladen (%s)", appid, name, e)
            self._offline_until = self._mono() + OFFLINE_PAUSE_S
            return None
        if len(data) > MAX_ICON or not _image_kind(data):
            return None
        tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError as e:
            _log.warning("Trophäen: Symbol nicht speicherbar: %s", e)
            try:
                os.unlink(tmp)                  # nur die eigene halbe Datei
            except OSError:
                pass
            return None
        return self._art_url(path)

    # ------------------------------------------------------------ Sicht für die Seite
    @staticmethod
    def _shown_icon(item: dict, got: bool) -> str:
        if got:
            return item["icon"] or item["icon_gray"]
        return "" if item["hidden"] else (item["icon_gray"] or item["icon"])

    def game(self, appid: int, lang: str = "de", fetch: bool = True) -> dict | None:
        """Fortschritt und Liste eines Spiels: freigeschaltete zuerst (neueste zuerst), dann gesperrte (Steams
        Reihenfolge). Verborgene gesperrte ohne Namen, Text und Symbol. None = keine Errungenschaften."""
        found = self.state(appid)
        if found is None:
            return None
        items, got, name = found
        pct = self.rarity(appid, fetch)
        rows = []
        for n, item in enumerate(items):
            on = item["key"] in got
            secret = item["hidden"] and not on
            rarity = pct.get(item["api"]) if item["api"] else None
            rows.append({"key": item["key"], "unlocked": on, "time": got.get(item["key"], 0), "hidden": secret,
                         "name": "" if secret else pick(item["names"], lang), "desc": "" if secret else pick(item["descs"], lang),
                         "rarity": rarity, "tier": tier_of(rarity), "icon": self.icon_url(appid, self._shown_icon(item, on)),
                         "_n": n})
        rows.sort(key=lambda r: (not r["unlocked"], -r["time"] if r["unlocked"] else 0, r["_n"]))
        for r in rows:
            del r["_n"]
        return {"appid": appid, "name": name, "progress": progress(rows), "items": rows}

    def icons(self, appid: int, keys: Any) -> dict[str, str | None]:
        """Symbole der genannten Trophäen (höchstens ICONS_PER_CALL) laden: {key: couch://art/… | None}."""
        found = self.state(appid)
        if found is None or not isinstance(keys, list):
            return {}
        items, got, _name = found
        by_key = {i["key"]: i for i in items}
        out: dict[str, str | None] = {}
        for key in [k for k in keys if isinstance(k, str)][:ICONS_PER_CALL]:
            item = by_key.get(key)
            if item is None:
                continue
            file = self._shown_icon(item, key in got)
            out[key] = self._load_icon(appid, file) if file else None
        return out

    def summary(self, titles: Callable[[int], str] | None = None, fetch: bool = True) -> dict:
        """Übersicht aller Spiele mit Errungenschaften: Summen je Stufe und die Spiele mit Fortschritt (zuletzt
        freigeschaltet zuerst). Seltenheit nur für Spiele mit Fortschritt (sonst gibt es nichts einzustufen)."""
        root = self.root()
        totals = {"platinum": 0, "gold": 0, "silver": 0, "bronze": 0, "unknown": 0, "unlocked": 0, "total": 0, "games": 0}
        games = []
        for appid in schema_appids(root) if root else []:
            found = self.state(appid)
            if found is None:
                continue
            items, got, name = found
            totals["total"] += len(items)
            totals["games"] += 1
            if not got:
                continue
            pct = self.rarity(appid, fetch)
            rows = [{"unlocked": i["key"] in got, "time": got.get(i["key"], 0), "tier": tier_of(pct.get(i["api"]))}
                    for i in items]
            prog = progress(rows)
            for tier in ("platinum", "gold", "silver", "bronze", "unknown"):
                totals[tier] += prog["tiers"][tier]
            totals["unlocked"] += prog["unlocked"]
            title = (titles(appid) if titles else "") or name or "Steam-Spiel %d" % appid
            games.append({"appid": appid, "title": title, **prog})
        games.sort(key=lambda g: (-g["last"], g["title"].casefold()))
        return {"totals": totals, "games": games}

    # ------------------------------------------------------------ Mitteilungen
    def check(self, appid: int, lang: str = "de", since: float | None = None, start: bool = False) -> list[dict]:
        """Neu freigeschaltete seit dem gemerkten Stand: [{"key", "name", "tier", "rarity"}], dazu {"platinum": True},
        wenn das Spiel dadurch vollständig wurde. Alles Freigeschaltete wird danach gemerkt (nie doppelt).

        Neu ist, was Steam mit einer Zeit ab since (Start der Sitzung, SINCE_SLACK_S Spielraum) eingetragen hat; ohne
        Zeit nur, wenn es nicht die Grundlinie beim Start ist (start=True). So kommen weder Trophäen aus Steams Wolke
        (alte Zeiten, etwa nach einer Neuinstallation) noch solche, die außerhalb der Konsole entstanden, als Flut."""
        found = self.state(appid)
        if found is None:
            return []
        items, got, _name = found
        floor = (since or 0.0) - SINCE_SLACK_S
        with self.seen.lock:
            before = self.seen.get(appid)
            keys, was_done = before if before is not None else (set(), False)
            now = set(got)
            done = len(now) == len(items)

            def fresh_one(key: str) -> bool:
                stamp = got.get(key, 0)
                return stamp >= floor if stamp else not start and before is not None
            fresh = [i for i in items if i["key"] in now and i["key"] not in keys and fresh_one(i["key"])]
            if before is not None and not (now - keys):
                return []
            self.seen.put(appid, keys | now, done or was_done)
        if not fresh:
            return []
        pct = self.rarity(appid)
        fresh.sort(key=lambda i: (got.get(i["key"], 0), i["key"]))
        out = [{"key": i["key"], "name": pick(i["names"], lang) or i["api"], "tier": tier_of(pct.get(i["api"])),
                "rarity": pct.get(i["api"])} for i in fresh]
        if done and not was_done:
            out.append({"platinum": True})
        return out


def progress(rows: list[dict]) -> dict:
    """Zahlen eines Spiels aus seinen Zeilen ({unlocked, time, tier}): Platin = alle frei."""
    total = len(rows)
    on = [r for r in rows if r["unlocked"]]
    tiers = {"platinum": 1 if total and len(on) == total else 0, "gold": 0, "silver": 0, "bronze": 0, "unknown": 0}
    for r in on:
        tiers[r["tier"] or "unknown"] += 1
    return {"total": total, "unlocked": len(on), "percent": (len(on) * 100) // total if total else 0,
            "tiers": tiers, "platinum": bool(tiers["platinum"]), "last": max((r["time"] for r in on), default=0)}


# ---------------------------------------------------------------- Texte der Mitteilungen
TITLES = {"bronze": "Bronze-Trophäe freigeschaltet", "silver": "Silber-Trophäe freigeschaltet",
          "gold": "Gold-Trophäe freigeschaltet", None: "Trophäe freigeschaltet"}
PLATINUM_TITLE = "Platin-Trophäe freigeschaltet"
SINGLE = 3                      # so viele einzeln; mehr auf einmal (Steam schrieb nach dem Spiel) zusammen


def icon_of(tier: str | None) -> str:
    """Symbol in icons.svg: farbiger Pokal je Stufe, sonst der neutrale."""
    return "trophy-" + tier if tier in TIERS or tier == "platinum" else "trophy"


def messages(game: str, found: list[dict]) -> list[tuple[str, str, str]]:
    """(Titel, Text, Symbol) je Mitteilung. Platin kommt immer einzeln und zuletzt."""
    plain = [f for f in found if not f.get("platinum")]
    out: list[tuple[str, str, str]] = []
    shown = plain if len(plain) <= SINGLE else plain[:SINGLE - 1]
    for f in shown:
        out.append((TITLES.get(f.get("tier"), TITLES[None]), "„%s“ · %s" % (f["name"], game), icon_of(f.get("tier"))))
    rest = plain[len(shown):]
    if rest:
        out.append(("%d weitere Trophäen freigeschaltet" % len(rest), game, "trophy"))
    if any(f.get("platinum") for f in found):
        out.append((PLATINUM_TITLE, "Alle Trophäen in „%s“" % game, icon_of("platinum")))
    return out
