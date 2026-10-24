"""Bilder der Bibliothek: woher sie kommen und was daraus abgeleitet wird.

Die Oberfläche bekommt je Eintrag ``entry.art = {"tile", "cover", "hero", "logo", "icon"}`` mit
Adressen ``couch://art/<url-kodierter absoluter Pfad>`` oder ``None``:

    tile   quadratisch 512x512 (Kachelreihe, Raster)      immer vorhanden
    cover  hochkant, höchstens 640x960 (fürs Raster)      immer vorhanden
    cover_full  dasselbe Cover in bester Größe (bis 1440x2160); ohne größere Vorlage = cover
    hero   16:9-Hintergrund, 1920 bis 3840 breit          bei Spielen immer, bei Apps None
    logo   freigestelltes Logo (PNG, beschnitten)         oder None -> Titel als Schrift setzen
    icon   klein (bis 256)                                immer vorhanden

Reihenfolge der Quellen, die erste mit brauchbarem Treffer gewinnt je Bildart:
  1. eigene Wahl des Nutzers        <data_dir>/art/<id>/user-<art>.png|jpg
  2. eigener Zwischenspeicher       <data_dir>/art/<id>/fetch-<art>.*  (aus dem Netz, artfetch.py:
                                    Steam, Microsoft-Store, GOG, Epic, Battle.net, Nintendo,
                                    SteamGridDB)
  3. Steams grid-Ordner             über shortcuts.vdf (Name, Startbefehl) bzw. die App-ID
  4. Steams librarycache            Dateinamen aus appinfo (library_assets_full), sonst Muster
  5. Emulator-Quellen               Wii U: meta/iconTex.tga, meta/bootTvTex.tga neben dem Spiel
  6. Lutris                         coverart, banners, Symbol (klein)
  7. erzeugter Platzhalter          placeholder.py

Abgeleitete Bilder (Kachel-Ausschnitt, weichgezeichneter Hintergrund, TGA/ICO -> PNG, beschnittenes
Logo) liegen als ``d-<art>-<hash>.jpg|png`` im Ordner des Eintrags. Der Hash enthält Quelle, mtime
und Größe: Ändert sich die Vorlage, entsteht eine neue Datei und die alte wird entfernt.

Fremde Dateien (Steam, Lutris, Emulatoren) werden nur gelesen. Gelöscht wird ausschließlich über
``ArtResolver._remove_own_file``: einzelne Dateien im eigenen, markierten Ordner, nie rekursiv.

Keine GLib-Schleife, kein Fenster. ``resolve`` blockiert kurz (warm < 20 ms je Eintrag, kalt
0,1–1 s wegen der Ableitungen), ``fetch`` blockiert lange (Netz): beides gehört in einen Thread.

Prüfen:  PYTHONPATH=app COUCH_TEST_ROOT=<scratch> python3 -m couch.library.art --report
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

from .. import jsontext
from . import placeholder
from .model import source_title

KINDS = ("tile", "cover", "hero", "logo", "icon")
# Was resolve() liefert: die Bildarten und dazu das Cover in voller Größe
OUT_KINDS = KINDS + ("cover_full",)
# Bilddienste, deren Hintergründe den Schriftzug des Spiels schon im Bild tragen (am 2026-10-14
# an echten Antworten gesehen: Microsoft-Store-SuperHeroArt von Alan Wake 2, Nintendo-Bilder von
# Zelda und Mario Kart). Die Seite blendet dann Logo bzw. Titel im Hub aus (art.hero_titled).
TITLED_HERO_SOURCES = frozenset({"msstore", "nintendo"})
FETCH_META = "fetch.json"                 # Merkzettel von artfetch: woher jedes geladene Bild stammt
URL_PREFIX = "couch://art/"
# Steckt im Hash jeder Ableitung: hochzählen, wenn sich ein Rezept ändert
RECIPE = 8
MARKER = ".couch-art"
MEMO_FILE = "memo.json"
DERIVED_PREFIX = "d-"
USER_PREFIX = "user-"
FETCH_PREFIX = "fetch-"
TMP_PREFIX = ".tmp-"
THUMB_PREFIX = "thumb-"
# Nur Dateien mit diesen Namensanfängen fasst die Löschfunktion an
OWN_PREFIXES = (DERIVED_PREFIX, FETCH_PREFIX, USER_PREFIX, TMP_PREFIX, THUMB_PREFIX, MEMO_FILE, "fetch.json")
# Vorschaubilder für „Treffer auswählen“ und „Bild ändern“: ein eigener Ordner neben den Einträgen
PREVIEW_DIR = "_vorschau"
PREVIEW_SIDE = 320
PREVIEW_KEEP = 400                 # so viele Vorschaubilder bleiben liegen, die ältesten gehen zuerst
PREVIEW_MAX_PIXELS = 60_000_000
CHOICES_FILE = "choices.json"      # von Hand gewählte Treffer; übersteht „Bilder neu laden“

TILE_SIZE = 512
COVER_SIZE = (600, 900)
COVER_UI_BOX = (640, 960)          # größer wird das Cover im Raster auch in 4K nicht gezeigt
COVER_FULL_BOX = (1440, 2160)
HERO_MIN_W, HERO_MAX_W = 1920, 3840
LOGO_BOX = (1280, 480)             # im Hub höchstens etwa 530x150 Referenz-Pixel, in 4K das Doppelte
ICON_MAX = 256
USER_MAX_BYTES = 40 * 1024 * 1024

BROWSER_EXT = (".jpg", ".jpeg", ".png", ".webp")
PC_SOURCES = ("steam", "proton", "native", "local")
EMU_SOURCES = ("switch", "wiiu")
BADGES = {"steam": "STEAM", "proton": "PROTON", "native": "LINUX", "switch": "SWITCH", "wiiu": "WII U"}

# Andere Namen derselben Bildart (Steam-Prototyp: capsule; Steam-/Lutris-Quelle: wide, coverart, banner)
_ART_ALIASES = {"capsule": "cover", "coverart": "cover", "wide": "header", "banner": "header"}

# Apps: eigenes Liniensymbol und die Namen, unter denen das Programm selbst ein Symbol installiert
APP_STYLE = {
    "steam": ("steam", ("steam",)),
    "youtube": ("play", ("rocks.shy.VacuumTube",)),
    "kodi": ("film", ("tv.kodi.Kodi",)),
    "browser": ("globe", ("com.google.Chrome",)),
    "desktop": ("desktop", ()),
    "emudeck": ("handheld", ()),
    "claude": ("terminal", ()),
}

def art_url(path) -> str | None:
    """Adresse für die Oberfläche. Alles außer Buchstaben/Ziffern/_.-~ wird kodiert (auch '/')."""
    if not path:
        return None
    return URL_PREFIX + quote(os.path.abspath(os.fspath(path)), safe="")


def art_path(url) -> str | None:
    """Umkehrung von art_url (für den couch://-Handler). None, wenn es keine solche Adresse ist."""
    if not isinstance(url, str) or not url.startswith(URL_PREFIX):
        return None
    path = unquote(url[len(URL_PREFIX):].split("?", 1)[0].split("#", 1)[0])
    return path if path.startswith("/") else None


def safe_id(entry_id: str) -> str:
    """Ordnername für eine Bibliotheks-ID: lesbar, ohne Sonderzeichen, eindeutig durch den Hash."""
    readable = re.sub(r"[^A-Za-z0-9.-]+", "_", entry_id).strip("._")[:60] or "eintrag"
    return "%s-%s" % (readable, hashlib.sha1(entry_id.encode("utf-8")).hexdigest()[:8])


def _norm(text: str) -> str:
    # Zeichen wie ™ zuerst entfernen: NFKD würde daraus die Buchstaben "TM" machen
    text = re.sub(r"[™®©℠]", "", text or "").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


# ====================================================================== Umgebung

@dataclass
class ArtContext:
    """Was der Kern hineinreicht. Für eigenständige Läufe: ``ArtContext.from_env()``."""
    data_dir: str
    cache_dir: str
    config: object = None                         # Protokoll Config (get/set/on_change) oder None
    steam_roots: tuple = field(default_factory=lambda: (_home(".local/share/Steam"),))
    lutris_dir: str = field(default_factory=lambda: _home(".local/share/lutris"))
    lutris_icon_dir: str = field(default_factory=lambda: _home(".local/share/icons/hicolor"))
    app_icon_roots: tuple = field(default_factory=lambda: (
        "/var/lib/flatpak/exports/share/icons", _home(".local/share/flatpak/exports/share/icons"),
        "/usr/share/icons"))
    online: object = None                         # Funktion -> bool für den Netz-Abruf
    fetcher: object = None                        # fertiger artfetch.ArtFetcher (sonst entsteht einer)
    fetch_sources: object = "all"                 # Quellen des selbst angelegten Abrufers (artsources.ORDER)
    browse_roots: object = None                   # Liste oder Funktion -> Liste: nur darunter gibt es preview()
    log: object = None                            # Funktion(text)

    @classmethod
    def from_env(cls, env=None, **overrides) -> "ArtContext":
        """Orte wie couch/paths.py: COUCH_TEST_ROOT lenkt Daten und Cache in den Prüfordner."""
        env = os.environ if env is None else env
        root = env.get("COUCH_TEST_ROOT")
        if root:
            data, cache = os.path.join(root, "data"), os.path.join(root, "cache")
        else:
            data = os.path.join(env.get("XDG_DATA_HOME") or _home(".local/share"), "couch")
            cache = os.path.join(env.get("XDG_CACHE_HOME") or _home(".cache"), "couch")
        return cls(data_dir=data, cache_dir=cache, **overrides)


def _ctx_get(ctx, name: str, default=None):
    """Wert aus dem Kontext; ``paths`` darf als Unterobjekt kommen (ctx.paths.data_dir)."""
    for holder in (ctx, getattr(ctx, "paths", None)):
        if holder is None:
            continue
        value = holder.get(name) if isinstance(holder, dict) else getattr(holder, name, None)
        if value is not None:
            return value
    return default


# ====================================================================== Steam lesen

class _AppInfo:
    """Bildnamen aus Steams appcache/appinfo.vdf (nur common/library_assets_full). Nur lesend."""

    MAGIC_V29 = 0x07564429
    MAGIC_V28 = 0x07564428

    def __init__(self, path: str):
        self.path = path
        self._stamp = None
        self._keys = None
        self._offsets = {}
        self._assets = {}

    def assets(self, appid: int, lang: str = "german") -> dict:
        """{"library_capsule": "hash/library_capsule.jpg", "library_hero_2x": …} oder {}."""
        try:
            self._refresh()
            if appid not in self._assets:
                self._assets[appid] = self._read(appid, lang)
            return self._assets[appid]
        except (OSError, ValueError, struct.error, IndexError, KeyError, RecursionError):
            return {}

    def _refresh(self) -> None:
        st = os.stat(self.path)
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        with open(self.path, "rb") as f:
            buf = f.read()
        magic, _universe = struct.unpack_from("<II", buf, 0)
        pos, keys = 8, None
        if magic == self.MAGIC_V29:
            (table_off,) = struct.unpack_from("<q", buf, pos)
            pos += 8
            (count,) = struct.unpack_from("<I", buf, table_off)
            p, keys = table_off + 4, []
            for _ in range(count):
                end = buf.index(b"\0", p)
                keys.append(buf[p:end].decode("utf-8", "replace"))
                p = end + 1
        elif magic != self.MAGIC_V28:
            raise ValueError("appinfo.vdf: unbekanntes Format %#x" % magic)
        offsets = {}
        while pos + 8 <= len(buf):
            appid, size = struct.unpack_from("<II", buf, pos)
            if appid == 0:
                break
            # nach der Größe: Zustand, Zeit, Token, SHA1, Änderungsnummer, SHA1 = 60 Bytes, dann das VDF
            offsets[appid] = (pos + 8 + 60, size - 60)
            pos += 8 + size
        self._stamp, self._keys, self._offsets, self._assets = stamp, keys, offsets, {}

    def _read(self, appid: int, lang: str) -> dict:
        if appid not in self._offsets:
            return {}
        off, size = self._offsets[appid]
        with open(self.path, "rb") as f:
            f.seek(off)
            buf = f.read(size)
        data, _ = self._parse(buf, 0, 0)
        info = data.get("appinfo", data)
        full = (info.get("common") or {}).get("library_assets_full") or {}
        out = {}
        for kind, entry in full.items():
            if not isinstance(entry, dict):
                continue
            for size_key, suffix in (("image", ""), ("image2x", "_2x")):
                imgs = entry.get(size_key)
                if isinstance(imgs, dict) and imgs:
                    name = imgs.get(lang) or imgs.get("english") or next(iter(imgs.values()))
                    if isinstance(name, str):
                        out[kind + suffix] = name
        return out

    def _parse(self, buf: bytes, pos: int, depth: int):
        if depth > 40:
            raise ValueError("appinfo.vdf: zu tief verschachtelt")
        out = {}
        while True:
            t = buf[pos]
            pos += 1
            if t == 0x08:
                return out, pos
            if self._keys is None:
                end = buf.index(b"\0", pos)
                key, pos = buf[pos:end].decode("utf-8", "replace"), end + 1
            else:
                (idx,) = struct.unpack_from("<I", buf, pos)
                pos += 4
                key = self._keys[idx]
            if t == 0x00:
                out[key], pos = self._parse(buf, pos, depth + 1)
            elif t == 0x01:
                end = buf.index(b"\0", pos)
                out[key], pos = buf[pos:end].decode("utf-8", "replace"), end + 1
            elif t in (0x02, 0x03):
                pos += 4
            elif t in (0x07, 0x0A):
                pos += 8
            else:
                raise ValueError("appinfo.vdf: unbekannter Typ %#x" % t)


class _SteamFiles:
    """Steams Bilder und Verknüpfungen von der Platte. Schreibt nichts, fragt den Client nicht."""

    GRID_NAME = re.compile(r"^(\d{1,10})(p|_hero|_logo|_icon)?\.(png|jpe?g|webp|ico|tga)$", re.I)
    GRID_KIND = {"": "header", "p": "cover", "_hero": "hero", "_logo": "logo", "_icon": "icon"}
    CACHE_NAMES = {
        "cover": ("library_capsule", ("library_600x900.jpg", "library_capsule.jpg")),
        "hero": ("library_hero", ("library_hero.jpg",)),
        "logo": ("library_logo", ("logo.png",)),
        "header": ("library_header", ("library_header.jpg", "header.jpg")),
    }

    def __init__(self, roots):
        self.roots = [r for r in roots if r]
        self._lock = threading.RLock()
        self._grid = {}            # Ordner -> (Stempel, {id: {art: pfad}})
        self._shortcuts = {}       # Datei -> (Stempel, [verknüpfung])
        self._appinfo = {}

    def _userdata_dirs(self) -> list:
        out = []
        for root in self.roots:
            base = os.path.join(root, "userdata")
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            out.extend(os.path.join(base, n, "config") for n in names if n.isdigit() and n != "0")
        return out

    def grid_dirs(self) -> list:
        return [os.path.join(d, "grid") for d in self._userdata_dirs()]

    def library_cache_dirs(self) -> list:
        return [os.path.join(r, "appcache", "librarycache") for r in self.roots]

    def grid_art(self, ids) -> dict:
        """Bilder aus dem grid-Ordner für App- bzw. Verknüpfungs-IDs: {art: pfad}, erste ID gewinnt."""
        out = {}
        with self._lock:
            for folder in self.grid_dirs():
                index = self._grid_index(folder)
                for ident in ids:
                    for kind, path in index.get(int(ident), {}).items():
                        out.setdefault(kind, path)
        return out

    def _grid_index(self, folder: str) -> dict:
        try:
            st = os.stat(folder)
        except OSError:
            return {}
        stamp = st.st_mtime_ns
        cached = self._grid.get(folder)
        if cached and cached[0] == stamp:
            return cached[1]
        index = {}
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            names = []
        for name in names:
            m = self.GRID_NAME.match(name)
            if not m:
                continue                              # u. a. <id>.json (Logo-Lage) und 64-Bit-Namen
            kind = self.GRID_KIND[(m.group(2) or "").lower()]
            index.setdefault(int(m.group(1)), {}).setdefault(kind, os.path.join(folder, name))
        self._grid[folder] = (stamp, index)
        return index

    def shortcuts(self) -> list:
        """Nicht-Steam-Verknüpfungen: [{"appid", "name", "norm", "exe", "options"}]."""
        out = []
        with self._lock:
            for conf in self._userdata_dirs():
                out.extend(self._read_shortcuts(os.path.join(conf, "shortcuts.vdf")))
        return out

    def _read_shortcuts(self, path: str) -> list:
        try:
            st = os.stat(path)
        except OSError:
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._shortcuts.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        items = []
        try:
            import vdf
            with open(path, "rb") as f:
                data = vdf.binary_load(f)
            raw = next((v for k, v in data.items() if k.lower() == "shortcuts"), {})
            for sc in raw.values():
                if not isinstance(sc, dict):
                    continue
                low = {k.lower(): v for k, v in sc.items()}
                name = str(low.get("appname") or "")
                exe = str(low.get("exe") or "").strip().strip('"')
                items.append({
                    "appid": int(low.get("appid") or 0) & 0xFFFFFFFF,   # steht vorzeichenbehaftet in der Datei
                    "name": name, "norm": _norm(name), "exe": exe,
                    "options": str(low.get("launchoptions") or ""),
                })
        except Exception:                             # kaputte Datei oder fehlendes Modul: dann eben ohne
            items = []
        self._shortcuts[path] = (stamp, items)
        return items

    def assets(self, appid: int) -> dict:
        """Bildnamen laut appinfo, sonst {}."""
        with self._lock:
            for root in self.roots:
                path = os.path.join(root, "appcache", "appinfo.vdf")
                reader = self._appinfo.get(path)
                if reader is None:
                    reader = self._appinfo[path] = _AppInfo(path)
                found = reader.assets(appid)
                if found:
                    return found
        return {}

    def library_cache(self, appid: int, assets: dict | None = None) -> dict:
        """Bilder aus appcache/librarycache/<appid>/: {art: pfad}."""
        out = {}
        for base in self.library_cache_dirs():
            folder = os.path.join(base, str(int(appid)))
            top, nested = [], []
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            try:
                                with os.scandir(e.path) as sub:
                                    nested.extend((s.name, s.path, s.stat().st_mtime_ns) for s in sub if s.is_file())
                            except OSError:
                                pass
                        elif e.is_file():
                            top.append((e.name, e.path))
            except OSError:
                continue
            for kind, (info_key, fallbacks) in self.CACHE_NAMES.items():
                if kind in out:
                    continue
                wanted = (assets or {}).get(info_key)
                if wanted and os.path.isfile(os.path.join(folder, wanted)):
                    out[kind] = os.path.join(folder, wanted)
                    continue
                for name in fallbacks:
                    hit = next((p for n, p in top if n == name), None)
                    if not hit:
                        # mehrere Prüfsummen-Ordner = alte und neue Fassung: die jüngste nehmen
                        newest = sorted((m, p) for n, p, m in nested if n == name)
                        hit = newest[-1][1] if newest else None
                    if hit:
                        out[kind] = hit
                        break
            if "icon" not in out:
                icon = next((p for n, p in top if re.fullmatch(r"[0-9a-f]{40}\.jpg", n)), None)
                if icon:
                    out["icon"] = icon
        return out


# ====================================================================== Ableitungen (rein, PIL)

def _open(path: str) -> Image.Image:
    """Bild ganz laden; die Datei ist danach wieder zu (ICO und TGA halten sie sonst offen)."""
    with open(path, "rb") as f:
        im = Image.open(f)
        im.load()
    return im


def _rgb(im: Image.Image, background=(14, 16, 20)) -> Image.Image:
    """Ohne Transparenz, auf dunklem Grund."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        base = Image.new("RGB", im.size, background)
        base.paste(im, mask=im.getchannel("A"))
        return base
    return im.convert("RGB")


def _backdrop(im: Image.Image, size, brightness: float = 0.55, blur: float = 0.035) -> Image.Image:
    """Bildfüllende, weichgezeichnete und abgedunkelte Fassung als Grund für ein kleines Bild."""
    w, h = size
    small = ImageOps.fit(im, (max(16, w // 8), max(16, h // 8)), Image.BILINEAR, centering=(0.5, 0.4))
    small = small.filter(ImageFilter.GaussianBlur(max(1.5, blur * max(small.size) * 2)))
    small = ImageEnhance.Brightness(ImageEnhance.Color(small).enhance(1.2)).enhance(brightness)
    return placeholder.smooth_upscale(small, size, noise=0.6)


def _paste_sharp(base: Image.Image, img: Image.Image, center) -> None:
    """Setzt das scharfe Bild mit weichem Schatten auf den Grund."""
    x, y = round(center[0] - img.width / 2), round(center[1] - img.height / 2)
    if img.width < base.width or img.height < base.height:
        shade = Image.new("L", base.size, 0)
        ImageDraw.Draw(shade).rectangle((x, y + 6, x + img.width, y + img.height + 6), fill=150)
        shade = shade.filter(ImageFilter.GaussianBlur(14))
        base.paste((0, 0, 0), mask=shade)
    base.paste(img, (x, y))


def crop_offset(im: Image.Image) -> float:
    """Lage des quadratischen Fensters entlang der langen Seite: 0 = Anfang, 0,5 = Mitte, 1 = Ende.

    Ein starr mittiger Ausschnitt schneidet bei Hochkant-Covern oft den Schriftzug an (er steht
    meist oben oder unten). Deshalb das Fenster dorthin schieben, wo die meisten Kanten liegen
    (Schrift ist kantenreich), nicht durch eine kantenreiche Zeile schneiden, und im Zweifel
    die Mitte nehmen.
    """
    w, h = im.size
    if abs(w - h) <= 2:
        return 0.5
    portrait = h >= w
    n = 96
    gray = im.convert("L")
    gray = gray.resize((max(8, round(n * w / h)), n) if portrait else (n, max(8, round(n * h / w))), Image.BILINEAR)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))       # der Bildrand selbst zählt als Kante
    strip = edges.resize((1, edges.height) if portrait else (edges.width, 1), Image.BOX)
    rows = list(strip.tobytes())
    count = len(rows)
    win = round(count * min(w, h) / max(w, h))
    span = count - win
    total = float(sum(rows))
    if span <= 0 or total <= 0:
        return 0.5
    mean = total / count
    best, best_at = None, span / 2
    for at in range(span + 1):
        inside = sum(rows[at:at + win]) / total
        cut = 0.0
        for edge in (at, at + win):
            if 0 < edge < count:
                band = rows[max(0, edge - 3):min(count, edge + 3)]
                cut += sum(band) / len(band)
        value = inside - 0.10 * cut / (2 * mean) - 0.04 * abs(at - span / 2) / (span / 2)
        if best is None or value > best:
            best, best_at = value, at
    return best_at / span


def make_tile_crop(im: Image.Image, size: int = TILE_SIZE) -> Image.Image:
    """Quadratischer Ausschnitt: mittig, außer ein Schriftzug würde dabei angeschnitten (crop_offset)."""
    src = _rgb(im)
    at = crop_offset(src)
    centering = (0.5, at) if src.height >= src.width else (at, 0.5)
    return ImageOps.fit(src, (size, size), Image.LANCZOS, centering=centering)


def make_composite(im: Image.Image, size, max_upscale: float = 2.0, center=(0.5, 0.5)) -> Image.Image:
    """Zu kleine oder falsch geformte Vorlage: weichgezeichneter Grund, das Bild scharf darauf."""
    w, h = size
    src = _rgb(im)
    base = _backdrop(src, size)
    scale = min(w / src.width, h / src.height, max_upscale)
    sharp = src.resize((max(1, round(src.width * scale)), max(1, round(src.height * scale))), Image.LANCZOS)
    _paste_sharp(base, sharp, (w * center[0], h * center[1]))
    return base


def hero_crop_width(size) -> int:
    """Breite des mittigen 16:9-Ausschnitts einer Vorlage (das Maß für ihre Schärfe am Fernseher)."""
    w, h = size
    return min(w, round(h * 16 / 9))


def make_hero_sharp(im: Image.Image) -> Image.Image:
    """16:9-Hintergrund aus einem echten Hero: Ausschnitt, mindestens 1920 breit, leicht weichgezeichnet.

    Ein 1920x620-Hero würde in 4K dreieinhalbfach vergrößert. Deshalb hier einmal sauber
    hochrechnen und so weit weichzeichnen, dass Blöcke und Kanten der Vorlage nicht auffallen.
    """
    src = _rgb(im)
    cw = hero_crop_width(src.size)
    out_w = max(HERO_MIN_W, min(HERO_MAX_W, cw))
    out_w -= out_w % 16
    out = ImageOps.fit(src, (out_w, out_w * 9 // 16), Image.LANCZOS, centering=(0.5, 0.35))
    to_4k = HERO_MAX_W / cw
    if to_4k > 2.0:
        out = out.filter(ImageFilter.GaussianBlur(min(3.0, 0.9 * (to_4k - 2.0)) * out_w / 1920))
    return out


def make_hero_ambient(im: Image.Image, size=(HERO_MIN_W, 1080)) -> Image.Image:
    """Hintergrund aus kleinem Material (Cover, Header, Symbol): nur noch Farbstimmung.

    So stark weichgezeichnet, dass kein Schriftzug mehr lesbar ist (halb lesbare Schrift wirkt
    kaputt), und auf eine feste, dunkle Grundhelligkeit gebracht: Ein weißes Cover darf den
    Bildschirm nicht grau machen, weiße Schrift muss darauf lesbar bleiben.
    """
    small = ImageOps.fit(_rgb(im), (64, 36), Image.BILINEAR, centering=(0.5, 0.35))
    small = small.filter(ImageFilter.GaussianBlur(5.0))         # entspricht rund 300 px in 4K
    small = ImageEnhance.Color(small).enhance(1.3)
    mean = sum(ImageStat.Stat(small.convert("L")).mean) or 1.0
    small = ImageEnhance.Brightness(small).enhance(max(0.22, min(1.0, 52.0 / mean)))
    return placeholder.smooth_upscale(small, size, noise=1.2)


def make_logo(im: Image.Image, box=LOGO_BOX) -> Image.Image | None:
    """Logo auf seinen sichtbaren Teil beschneiden und verkleinern. None = taugt nicht als Logo."""
    if im.mode != "RGBA":
        if im.mode in ("LA", "PA") or "transparency" in im.info:
            im = im.convert("RGBA")
        else:
            return None                               # deckendes Bild wäre ein Kasten über dem Hintergrund
    alpha = im.getchannel("A")
    low, high = alpha.getextrema()
    if low > 250 or high < 16:
        return None
    bbox = alpha.point(lambda a: 255 if a > 10 else 0).getbbox()
    if not bbox:
        return None
    # schmaler Rand, damit ein weicher Schein um das Logo nicht hart abgeschnitten wird
    pad = max(2, round(0.006 * max(bbox[2] - bbox[0], bbox[3] - bbox[1])))
    bbox = (max(0, bbox[0] - pad), max(0, bbox[1] - pad), min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad))
    out = im.crop(bbox)
    out.thumbnail(box, Image.LANCZOS)
    return out


def make_icon(im: Image.Image, max_side: int = ICON_MAX) -> Image.Image:
    out = im.convert("RGBA")
    out.thumbnail((max_side, max_side), Image.LANCZOS)
    return out


def load_program_icon(path: str, size: int = 512) -> Image.Image | None:
    """Vom Programm installiertes Symbol laden (PNG direkt, SVG über GdkPixbuf ohne Anzeige)."""
    try:
        if not path.lower().endswith((".svg", ".svgz")):
            return _open(path).convert("RGBA")
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")           # gi meldet beim ersten Laden eigene Veraltungen
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        mode = "RGBA" if pb.get_has_alpha() else "RGB"
        im = Image.frombuffer(mode, (pb.get_width(), pb.get_height()), pb.get_pixels(), "raw", mode,
                              pb.get_rowstride(), 1)
        return im.convert("RGBA")
    except Exception:                                 # fehlender Lader, kaputte Datei: dann das eigene Symbol
        return None


# ====================================================================== Auflöser

@dataclass(frozen=True)
class Candidate:
    """Eine gefundene Bilddatei: Art der Vorlage, Quelle, Stempel."""
    kind: str        # tile | cover | hero | background | header | logo | icon
    source: str      # user | cache | steam-grid | steam-cache | emulator | lutris
    path: str
    mtime_ns: int
    size: int


@dataclass
class Choice:
    """Was für eine Bildart gewählt wurde (geht in den Merkzettel und den Bericht)."""
    kind: str
    source: str              # wie Candidate.source, dazu placeholder | app-icon | app-symbol | none
    mode: str                # direct | crop | composite | sharp | ambient | trim | convert | mini | placeholder | app | none
    src: str | None = None   # Vorlage
    out: str | None = None   # fertige Datei (bei direct = Vorlage)
    dims: tuple | None = None
    good: bool = False       # gut genug: kein Anlass, im Netz nach Besserem zu suchen
    pending: bool = False    # nur im Probelauf: Datei würde erst erzeugt


class ArtError(Exception):
    """Fehler bei eigener Wahl oder Aufräumen. message ist ein deutscher Satz für die Oberfläche."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ArtResolver:
    """Löst die Bilder eines Eintrags auf. Siehe Modulkopf.

    ctx: ArtContext oder ein Objekt mit data_dir, cache_dir (auch als ctx.paths.*), optional
    config, steam_roots, lutris_dir, lutris_icon_dir, app_icon_roots, online, fetcher, log.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        if not _ctx_get(ctx, "data_dir") or not _ctx_get(ctx, "cache_dir"):
            raise ValueError("ArtResolver braucht data_dir und cache_dir")
        # Aufgelöste Pfade (/home ist ein Verweis auf /var/home): so sehen alle Adressen gleich aus,
        # egal in welcher Schreibweise eine Quelle ihre Pfade liefert
        self.data_dir = os.path.realpath(os.fspath(_ctx_get(ctx, "data_dir")))
        self.cache_dir = os.path.realpath(os.fspath(_ctx_get(ctx, "cache_dir")))
        self.art_root = os.path.join(self.data_dir, "art")
        self.config = _ctx_get(ctx, "config")
        default = ArtContext(self.data_dir, self.cache_dir)
        self.lutris_dir = _ctx_get(ctx, "lutris_dir", default.lutris_dir)
        self.lutris_icon_dir = _ctx_get(ctx, "lutris_icon_dir", default.lutris_icon_dir)
        self.app_icon_roots = tuple(_ctx_get(ctx, "app_icon_roots", default.app_icon_roots))
        self.steam = _SteamFiles(tuple(_ctx_get(ctx, "steam_roots", default.steam_roots)))
        self._log = _ctx_get(ctx, "log") or (lambda text: None)
        self._fetcher = _ctx_get(ctx, "fetcher")
        self._online = _ctx_get(ctx, "online")
        self._fetch_sources = _ctx_get(ctx, "fetch_sources", "all")
        self._browse_roots = _ctx_get(ctx, "browse_roots")
        self._build_lock = threading.RLock()
        self._probe_cache = {}
        self._choices = {}                 # entry.id -> {art: Choice} vom letzten resolve
        self.stats = {"warm": 0, "cold": 0, "derived": 0, "removed": 0}

    # ------------------------------------------------------------------ öffentlich

    def resolve(self, entry, steam_shortcut_art=None, dry_run: bool = False) -> dict:
        """Bilder eines Eintrags: {"tile", "cover", "cover_full", "hero", "logo", "icon"} ->
        couch://art/… | None. cover ist die Fassung fürs Raster (höchstens 640x960), cover_full
        dasselbe Bild in bester Größe.

        Nur lokale Quellen. BLOCKIEREND: warm (Merkzettel passt) wenige Stat-Aufrufe, kalt werden
        fehlende Ableitungen mit PIL erzeugt (0,1–1 s). Darf für alle Einträge nacheinander
        laufen, aber nicht in der Hauptschleife.

        steam_shortcut_art: None = Verknüpfung selbst suchen (shortcuts.vdf + grid-Ordner);
        ein dict {"cover"|"capsule", "header"|"wide", "hero", "logo", "icon": pfad|None} = genau
        diese Bilder als Quelle „Steam-Grid“ nehmen. Passt auch auf das Ergebnis von
        steam.match_shortcut(entry, shortcuts) (dict mit "grid") bzw. dessen ["grid"].
        Ein leeres dict heißt: keine Verknüpfung, auch nicht selbst suchen.
        dry_run: nichts schreiben, nur planen (für den Bericht).
        """
        choices = self._resolve(entry, steam_shortcut_art, dry_run)
        out = {kind: art_url(choices[kind].out) if choices[kind].out else None for kind in OUT_KINDS}
        # Zwei Merkmale für die Seite (keine Adressen): erzeugte Platzhalter tragen Marke und Titel
        # schon im Bild, die Seite legt dann keine eigene Marke darüber; ein Hintergrund mit
        # Schriftzug braucht kein Logo darüber
        out["generated"] = [kind for kind in OUT_KINDS
                            if choices[kind].out and choices[kind].source == "placeholder"]
        out["hero_titled"] = self._hero_titled(entry, choices["hero"])
        return out

    def _hero_titled(self, entry, choice) -> bool:
        """Stammt der Hintergrund von einem Dienst, dessen Bilder den Schriftzug tragen? Nur für
        geladene Bilder (Quelle cache, Datei fetch-<art>) und nur laut Merkzettel des Abrufs."""
        if choice is None or choice.source != "cache" or not choice.src or choice.mode == "ambient":
            return False
        name = os.path.basename(choice.src)
        if not name.startswith(FETCH_PREFIX):
            return False
        kind = os.path.splitext(name[len(FETCH_PREFIX):])[0]
        try:
            with open(os.path.join(self.entry_dir(entry.id), FETCH_META), encoding="utf-8") as f:
                meta = json.load(f)
            origin = (meta.get("origin") or {}).get(kind) or {}
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(origin, dict) and origin.get("source") in TITLED_HERO_SOURCES

    def describe(self, entry, steam_shortcut_art=None, dry_run: bool = False) -> dict:
        """Wie resolve, aber ausführlich: {art: {"source", "mode", "src", "file", "dims", "good", "pending"}}."""
        choices = self._resolve(entry, steam_shortcut_art, dry_run)
        return {k: {"source": c.source, "mode": c.mode, "src": c.src, "file": c.out,
                    "dims": list(c.dims) if c.dims else None, "good": c.good, "pending": c.pending}
                for k, c in choices.items()}

    def wants_fetch(self, entry) -> bool:
        """Lohnt ein Netz-Abruf? Schnell (nur Merkzettel und artfetch.json), kein Netz."""
        if not self._fetchable(entry):
            return False
        fetcher = self._get_fetcher()
        system = self._system(entry)
        if not fetcher.enabled() or not fetcher.is_online() or fetcher.blocked(entry.id, system) > 0:
            return False
        missing = self.missing_kinds(entry)
        # Die Ausnahme „neue Quelle“ (E97) gilt nur für eine, die eine fehlende Bildart führt (E99)
        if not missing or fetcher.blocked(entry.id, system, missing) > 0:
            return False
        # nur wenn eine zuständige Quelle wenigstens eine der fehlenden Bildarten führt
        return any(set(missing) & set(src.kinds) for src in fetcher.sources_for(self._system(entry)))

    def missing_kinds(self, entry) -> list:
        """Bildarten, für die nur Behelf vorliegt: Teilmenge von ["cover", "hero", "logo", "tile"].

        "tile" nur bei Switch- und Wii-U-Spielen, solange die Kachel ein Ausschnitt aus dem
        Cover ist (Nintendo hat die quadratische Fassung)."""
        choices = self._choices.get(entry.id) or self._resolve(entry, None, False)
        out = [k for k in ("cover", "hero") if not choices[k].good]
        if choices["logo"].out is None:
            out.append("logo")
        if self._system(entry) and choices["tile"].source not in ("user", "cache"):
            out.append("tile")
        return out

    def fetch(self, entry, kinds=None, force: bool = False) -> bool:
        """Bessere Bilder aus dem Netz holen. BLOCKIEREND (mehrere Anfragen, Sekunden bis Minuten).

        Die Quellen kommen der Reihe nach dran (PC: Steam, Microsoft-Store, GOG, Epic,
        SteamGridDB; Switch und Wii U: Nintendo, SteamGridDB); je Bildart gewinnt die erste mit
        einem ausreichend großen Bild. Übernommen wird nur ein sicherer Treffer. Hat der Nutzer
        einen Treffer von Hand gewählt (fetch_candidate), wird genau der wieder geladen.
        Rückgabe True = neue Dateien liegen im Zwischenspeicher; danach ``resolve(entry)`` erneut
        aufrufen und die Oberfläche benachrichtigen. False = nichts geholt (aus, kein Netz,
        Sperrfrist, kein sicherer Treffer, Fehler).
        """
        return bool(self.fetch_detail(entry, kinds, force).get("ok"))

    def fetch_detail(self, entry, kinds=None, force: bool = False) -> dict:
        """Wie fetch, mit dem ganzen Ergebnis des Abrufers: {"ok", "status", "reason", "files",
        "source", "sources", "tried", "name", "score", "appid"}. BLOCKIEREND."""
        blank = {"ok": False, "status": "skip", "reason": "", "files": {}, "source": "", "name": "", "score": 0.0}
        if not self._fetchable(entry):
            return {**blank, "reason": "kein Spiel"}
        fetcher = self._get_fetcher()
        if not fetcher.sources_for(self._system(entry)):
            return {**blank, "reason": "keine Quelle für diese Art Eintrag"}
        kinds = tuple(kinds) if kinds else tuple(self.missing_kinds(entry))
        if not kinds and not fetcher.choice(entry.id):
            return {**blank, "reason": "Bilder sind gut"}
        dest = self._ensure_entry_dir(entry.id)
        if dest is None:
            return {**blank, "status": "error", "reason": "Zwischenspeicher nicht benutzbar"}
        return fetcher.fetch_entry(entry.id, self._query(entry), dest, kinds, have=self._have(entry), force=force)

    def candidates(self, entry, query: str | None = None) -> list:
        """Kandidaten für „Treffer auswählen“. BLOCKIEREND (Netz, einige Sekunden).

        query ersetzt die Suchbegriffe des Eintrags (eigene Suche). Rückgabe je Kandidat:
        {"source", "label", "ref", "name", "thumb": couch://art/… | None, "kinds": [..],
        "score": float, "safe": bool}; der sichere Treffer einer Quelle steht vorn. Die
        Vorschaubilder liegen schon im Zwischenspeicher: Die Oberfläche braucht kein Netz.
        Leere Liste = aus, kein Netz oder nichts gefunden."""
        if not self._fetchable(entry):
            return []
        folder = self._preview_dir()
        rows = self._get_fetcher().candidates(entry.id, self._query(entry), query, thumb_dir=folder)
        for row in rows:
            row["thumb"] = art_url(row["thumb"]) if row.get("thumb") else None
        self._prune_previews()
        return rows

    def fetch_candidate(self, entry, candidate: dict) -> bool:
        """Einen Kandidaten aus ``candidates`` übernehmen. BLOCKIEREND. Die Wahl wird je Eintrag
        gemerkt und gilt auch nach „Bilder neu laden“ (clear_cache + fetch), bis clear_choice
        sie zurücknimmt. True = neue Dateien liegen im Zwischenspeicher."""
        return bool(self.fetch_candidate_detail(entry, candidate).get("ok"))

    def fetch_candidate_detail(self, entry, candidate: dict) -> dict:
        blank = {"ok": False, "status": "skip", "reason": "", "files": {}, "source": "", "name": "", "score": 0.0}
        if not self._fetchable(entry) or not isinstance(candidate, dict):
            return {**blank, "reason": "kein Spiel"}
        dest = self._ensure_entry_dir(entry.id)
        if dest is None:
            return {**blank, "status": "error", "reason": "Zwischenspeicher nicht benutzbar"}
        return self._get_fetcher().fetch_candidate(entry.id, candidate, dest)

    def choice(self, entry_id: str) -> dict | None:
        """Der von Hand gewählte Treffer: {"source", "ref", "name", "time"} oder None. Schnell."""
        got = self._get_fetcher().choice(entry_id)
        return {k: got.get(k) for k in ("source", "ref", "name", "time")} if got else None

    def clear_choice(self, entry_id: str) -> bool:
        """Gemerkte Trefferwahl zurücknehmen (danach sucht fetch wieder selbst)."""
        return self._get_fetcher().forget_choice(entry_id)

    def forget_fetch(self, entry_id: str) -> None:
        """Nur den Vermerk in artfetch.json löschen (Sperrfrist nach „kein Treffer“), geladene Bilder
        und eine gemerkte Trefferwahl bleiben: nach dem Umbenennen (E90/E99) sucht der nächste Abruf
        mit dem neuen Namen. Schreibt artfetch.json: im Bilder-Thread rufen."""
        try:
            self._get_fetcher().forget(entry_id)
        except OSError as exc:
            self._log("art: Vermerk von %s nicht gelöscht: %s" % (entry_id, exc))

    def preview(self, path: str) -> str | None:
        """Kleines Vorschaubild einer eigenen Datei für die Bildauswahl: couch://art/… oder None.

        Nur für Dateien unter den Durchsuchen-Wurzeln (ctx.browse_roots), nur echte Bilder laut
        PIL, höchstens 40 MB. BLOCKIEREND (10 bis 200 ms je Bild; ein zweiter Aufruf ist warm)."""
        try:
            if not path or not os.path.isabs(str(path)):
                return None
            real = os.path.realpath(str(path))
            roots = self._browse_roots() if callable(self._browse_roots) else (self._browse_roots or ())
            if not any(real.startswith(os.path.realpath(str(r)).rstrip(os.sep) + os.sep) for r in roots):
                return None
            st = os.stat(real)
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= USER_MAX_BYTES:
                return None
            folder = self._preview_dir()
            if folder is None:
                return None
            name = "%s%s.jpg" % (THUMB_PREFIX, _digest("%s|%d|%d" % (real, st.st_mtime_ns, st.st_size)))
            out = os.path.join(folder, name)
            if os.path.isfile(out):
                return art_url(out)
            with Image.open(real) as probe:
                w, h = probe.size
                if w < 1 or h < 1 or w * h > PREVIEW_MAX_PIXELS:
                    return None
                probe.verify()
            with open(real, "rb") as f:
                im = Image.open(f)
                im.draft("RGB", (PREVIEW_SIDE * 2, PREVIEW_SIDE * 2))
                small = _rgb(im)
                small.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.LANCZOS)
            placeholder.save_image(small, out, quality=85)
            self._prune_previews()
            return art_url(out)
        except Exception:                             # kein Bild, kein Zugriff, kaputt: dann keine Vorschau
            return None

    def set_user_art(self, entry_id: str, kind: str, source_path: str) -> str:
        """Vom Nutzer gewählte Bilddatei übernehmen (Kopie in den Zwischenspeicher). BLOCKIEREND.

        kind: tile | cover | hero | logo | icon. JPEG und PNG werden unverändert kopiert, anderes
        (WebP, TGA, BMP, ICO …) wird zu PNG. Rückgabe: Pfad der Kopie. ArtError mit deutschem Text.
        """
        if kind not in KINDS:
            raise ArtError("Diese Bildart gibt es nicht.")
        try:
            size = os.path.getsize(source_path)
            if size > USER_MAX_BYTES:
                raise ArtError("Das Bild ist zu groß (höchstens 40 MB).")
            with Image.open(source_path) as probe:
                fmt = probe.format
                probe.verify()
        except ArtError:
            raise
        except Exception:
            raise ArtError("Die Datei ist kein lesbares Bild.") from None
        folder = self._ensure_entry_dir(entry_id)
        if folder is None:
            raise ArtError("Der Bilderordner ist nicht benutzbar.")
        ext = {"JPEG": ".jpg", "PNG": ".png"}.get(fmt, ".png")
        final = os.path.join(folder, USER_PREFIX + kind + ext)
        tmp = os.path.join(folder, "%s%d-user-%s" % (TMP_PREFIX, os.getpid(), kind))
        try:
            if fmt in ("JPEG", "PNG"):
                with open(source_path, "rb") as src, open(tmp, "wb") as dst:
                    while True:
                        block = src.read(1 << 20)
                        if not block:
                            break
                        dst.write(block)
            else:
                _open(source_path).convert("RGBA").save(tmp, "PNG")
            os.replace(tmp, final)
        except OSError:
            raise ArtError("Das Bild konnte nicht gespeichert werden.") from None
        finally:
            self._remove_own_file(folder, os.path.basename(tmp))
        for other in (".jpg", ".png", ".jpeg", ".webp"):
            if other != ext:
                self._remove_own_file(folder, USER_PREFIX + kind + other)
        return final

    def clear_user_art(self, entry_id: str, kind: str | None = None) -> int:
        """Eigene Wahl zurücknehmen (eine Bildart oder alle). Rückgabe: Zahl entfernter Dateien."""
        folder = self.entry_dir(entry_id)
        removed = 0
        for name in self._listdir(folder):
            if name.startswith(USER_PREFIX) and (kind is None or name.startswith(USER_PREFIX + kind + ".")):
                removed += self._remove_own_file(folder, name)
        return removed

    def clear_cache(self, entry_id: str, include_user: bool = False, reset_fetch: bool = True) -> int:
        """Abgeleitete und heruntergeladene Bilder eines Eintrags entfernen.

        Die eigene Wahl des Nutzers bleibt (außer include_user). reset_fetch löscht auch den
        Vermerk in artfetch.json, damit ein neuer Abruf sofort erlaubt ist.
        Rückgabe: Zahl entfernter Dateien. Entfernt nur einzelne Dateien im Ordner des Eintrags.
        """
        folder = self.entry_dir(entry_id)
        removed = 0
        with self._build_lock:
            for name in self._listdir(folder):
                if name.startswith(USER_PREFIX) and not include_user:
                    continue
                removed += self._remove_own_file(folder, name)
            self._choices.pop(entry_id, None)
            try:
                if not os.path.islink(folder):
                    os.rmdir(folder)                  # nur wenn leer; nie rekursiv
            except OSError:
                pass
        if reset_fetch:
            try:
                self._get_fetcher().forget(entry_id)
            except OSError:
                pass
        return removed

    def art_roots(self) -> list:
        """Ordner, aus denen resolve() Dateien unverändert weiterreicht (für paths.art_roots()).

        Alle liegen unter <data_dir>/art, den Steam-Wurzeln und dem Lutris-Ordner. Bilder von
        woanders (Symbolordner, Spielordner) werden nie direkt gemeldet, sondern umgewandelt.
        """
        roots = [self.art_root, os.path.join(self.lutris_dir, "coverart"), os.path.join(self.lutris_dir, "banners")]
        roots += self.steam.grid_dirs() + self.steam.library_cache_dirs()
        seen, out = set(), []
        for root in roots:
            real = os.path.realpath(root)
            if real not in seen:
                seen.add(real)
                out.append(real)
        return out

    def _servable(self, path: str) -> bool:
        real = os.path.realpath(path)
        for root in [self.art_root, self.lutris_dir] + list(self.steam.roots):
            base = os.path.realpath(root).rstrip(os.sep) + os.sep
            if real.startswith(base):
                return True
        return False

    def entry_dir(self, entry_id: str) -> str:
        return os.path.join(self.art_root, safe_id(entry_id))

    # ------------------------------------------------------------------ Ablauf

    def _resolve(self, entry, shortcut_art, dry_run: bool) -> dict:
        folder = self.entry_dir(entry.id)
        cands, extra_key = self._candidates(entry, shortcut_art, folder)
        key = self._memo_key(entry, cands, extra_key)
        memo = self._load_memo(folder)
        if memo and memo.get("key") != key and not getattr(entry, "available", True):
            # Datenträger fehlt: die Vorlagen sind nur gerade nicht da. Letzten guten Stand behalten.
            key = memo.get("key")
        if memo and memo.get("key") == key:
            choices = self._choices_from_memo(memo)
            if choices and all(c.out is None or os.path.isfile(c.out) for c in choices.values()):
                self.stats["warm"] += 1
                self._choices[entry.id] = choices
                return choices
        with self._build_lock:
            self.stats["cold"] += 1
            usable = folder if (dry_run or self._ensure_entry_dir(entry.id)) else None
            choices = self._build(entry, cands, usable, dry_run)
            if not dry_run and usable:
                self._cleanup(folder, choices)
                self._save_memo(folder, key, choices)
            if not dry_run:
                self._choices[entry.id] = choices
        return choices

    def _memo_key(self, entry, cands, extra_key: str) -> str:
        parts = [str(RECIPE), str(placeholder.RECIPE), entry.id, entry.title or "", self._badge(entry),
                 getattr(entry, "kind", "game"), getattr(entry, "source", ""), extra_key]
        parts += ["%s|%s|%s|%d|%d" % (c.kind, c.source, c.path, c.mtime_ns, c.size) for c in cands]
        return hashlib.sha1("\n".join(parts).encode("utf-8", "surrogateescape")).hexdigest()

    def _load_memo(self, folder: str) -> dict | None:
        try:
            with open(os.path.join(folder, MEMO_FILE), encoding="utf-8") as f:
                memo = json.load(f)
            return memo if isinstance(memo, dict) else None
        except (OSError, ValueError):
            return None

    def _save_memo(self, folder: str, key: str, choices: dict) -> None:
        data = {"key": key, "choices": {k: [c.source, c.mode, c.src, c.out, c.dims, c.good] for k, c in choices.items()}}
        tmp = os.path.join(folder, "%s%d-memo" % (TMP_PREFIX, os.getpid()))
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps(data))
            os.replace(tmp, os.path.join(folder, MEMO_FILE))
        except OSError as exc:
            self._log("art: Merkzettel nicht geschrieben: %s" % exc)

    @staticmethod
    def _choices_from_memo(memo: dict) -> dict | None:
        try:
            out = {}
            for kind in OUT_KINDS:
                source, mode, src, dest, dims, good = memo["choices"][kind]
                out[kind] = Choice(kind, source, mode, src, dest, tuple(dims) if dims else None, bool(good))
            return out
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ Vorlagen sammeln

    def _candidates(self, entry, shortcut_art, folder: str):
        """Alle vorhandenen Vorlagen in der Reihenfolge der Quellen. Nur Stat-Aufrufe, kein Dekodieren."""
        out, seen = [], set()

        def add(kind, source, path):
            if not path or not isinstance(path, str):
                return
            path = os.path.realpath(path)             # eine Schreibweise je Datei; Verweise zählen als ihr Ziel
            if (kind, path) in seen:
                return
            seen.add((kind, path))
            try:
                st = os.stat(path)
            except OSError:
                return
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                out.append(Candidate(kind, source, path, st.st_mtime_ns, st.st_size))

        own = set(self._listdir(folder)) if not os.path.islink(folder) else set()
        for kind in KINDS:                                              # 1. eigene Wahl
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                if USER_PREFIX + kind + ext in own:
                    add(kind, "user", os.path.join(folder, USER_PREFIX + kind + ext))
        for kind in ("tile", "cover", "hero", "background", "header", "logo", "icon"):   # 2. Zwischenspeicher
            for ext in (".jpg", ".png"):
                if FETCH_PREFIX + kind + ext in own:
                    add(kind, "cache", os.path.join(folder, FETCH_PREFIX + kind + ext))
        if getattr(entry, "kind", "game") == "app":
            symbol, icon = self._app_style(entry)
            stamp = ""
            if icon:
                try:
                    st = os.stat(icon)
                    stamp = "%s|%d|%d" % (icon, st.st_mtime_ns, st.st_size)
                except OSError:
                    stamp = ""
            return out, "app|%s|%s" % (symbol, stamp)

        appid = self._steam_appid(entry)
        if shortcut_art is not None:                                    # 3. Steam-Grid
            given = dict(shortcut_art)
            given = dict(given["grid"]) if isinstance(given.get("grid"), dict) else given
            grid = {_ART_ALIASES.get(k, k): v for k, v in given.items()}
        elif appid:
            grid = self.steam.grid_art([appid])
        else:
            grid = self.steam.grid_art(self._shortcut_ids(entry))
        for kind in ("cover", "header", "hero", "logo", "icon"):
            add(kind, "steam-grid", grid.get(kind))
        if appid:                                                       # 4. librarycache
            cache = self.steam.library_cache(appid, self._steam_assets(entry, appid))
            for kind in ("cover", "header", "hero", "logo", "icon"):
                add(kind, "steam-cache", cache.get(kind))
            # Was die Steam-Quelle schon gefunden hat (detail.steam_art), gilt als dieselbe Quelle
            hint = self._detail(entry, "steam_art")
            for key, path in (hint.items() if isinstance(hint, dict) else ()):
                if _ART_ALIASES.get(key, key) in ("cover", "header", "hero", "logo", "icon"):
                    add(_ART_ALIASES.get(key, key), "steam-cache", path)
        meta = self._wiiu_meta_dir(entry)                               # 5. Emulator-Quellen
        if meta:
            add("icon", "emulator", os.path.join(meta, "iconTex.tga"))
            add("background", "emulator", os.path.join(meta, "bootTvTex.tga"))
        slug = self._lutris_slug(entry)                                 # 6. Lutris
        if slug:
            for kind, sub in (("cover", "coverart"), ("header", "banners")):
                for ext in (".jpg", ".png"):
                    add(kind, "lutris", os.path.join(self.lutris_dir, sub, slug + ext))
            add("icon", "lutris", os.path.join(self.lutris_icon_dir, "128x128", "apps", "lutris_%s.png" % slug))
        hint = self._detail(entry, "lutris_art")
        for key, path in (hint.items() if isinstance(hint, dict) else ()):
            if _ART_ALIASES.get(key, key) in ("cover", "header", "icon"):
                add(_ART_ALIASES.get(key, key), "lutris", path)
        return out, ""

    @staticmethod
    def _detail(entry, *names):
        detail = getattr(entry, "detail", None) or {}
        for name in names:
            value = detail.get(name)
            if value not in (None, ""):
                return value
        return None

    def _store_appid(self, entry) -> int | None:
        """App-ID im Steam-Store. Verknüpfungen (steam:shortcut:<id>) haben eine eigene Nummer,
        die der Store nicht kennt: Für sie wird wie für jedes fremde Spiel nach dem Titel gesucht."""
        m = re.fullmatch(r"steam:(\d+)", entry.id)
        return int(m.group(1)) if m else None

    def _system(self, entry) -> str | None:
        source = getattr(entry, "source", "")
        return source if source in EMU_SOURCES else None

    def _query(self, entry) -> dict:
        """Was die Quellen über den Eintrag wissen müssen: Suchbegriffe, System, Steam-Nummer."""
        from .artsources import match
        names = [entry.title]
        if source_title(entry) != entry.title:           # umbenannt (E90): der alte Name sucht mit
            names.append(source_title(entry))
        folder = self._detail(entry, "game_dir", "install_dir")
        if folder and not self._system(entry):
            names.append(os.path.basename(str(folder).rstrip(os.sep)))
        rom = self._detail(entry, "rom")
        if rom and self._system(entry) == "switch":
            names.append(os.path.splitext(os.path.basename(str(rom)))[0])
        slug = self._lutris_slug(entry)
        if slug:
            names.append(slug.replace("-", " "))
        # Ordner- und ROM-Namen in kaputter Kodierung: als Suchbegriff ließen sie requests scheitern
        from .sources import clean_text
        names = [clean_text(n) for n in names]
        query = {"title": entry.title or "", "terms": match.search_terms(*names), "system": self._system(entry),
                 "appid": self._store_appid(entry), "extra_names": None}
        if query["appid"]:
            # Namen der doppelt großen Fassungen aus Steams appinfo: fürs Logo (das GetItems nicht
            # nennt) und als Ersatz, falls der Store zu dieser App nichts liefert
            assets = self._steam_assets(entry, query["appid"])
            query["extra_names"] = {
                kind: [n for n in (assets.get(key + "_2x"), assets.get(key)) if n]
                for kind, key in (("logo", "library_logo"), ("cover", "library_capsule"), ("hero", "library_hero"))}
        return query

    def _have(self, entry) -> dict:
        """Größe der Vorlagen, die lokal schon gewählt sind: Kleineres aus dem Netz ersetzt sie nicht."""
        choices = self._choices.get(entry.id) or {}
        return {kind: tuple(c.dims) for kind, c in choices.items()
                if kind in ("cover", "hero", "logo") and c.dims and c.source not in ("placeholder", "none")
                and (kind != "hero" or c.mode != "ambient")}

    def _steam_appid(self, entry) -> int | None:
        if getattr(entry, "source", "") != "steam" and not entry.id.startswith("steam:"):
            return None
        m = re.fullmatch(r"steam:(\d+)", entry.id)
        value = m.group(1) if m else self._detail(entry, "appid")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _steam_assets(self, entry, appid: int) -> dict:
        given = self._detail(entry, "library_assets")
        return given if isinstance(given, dict) else self.steam.assets(appid)

    def _lutris_slug(self, entry) -> str | None:
        if entry.id.startswith("lutris:"):
            return entry.id.split(":", 1)[1] or None
        slug = self._detail(entry, "slug", "lutris_slug")
        return str(slug) if slug else None

    def _wiiu_meta_dir(self, entry) -> str | None:
        if getattr(entry, "source", "") != "wiiu":
            return None
        base = self._detail(entry, "game_dir")
        if not base:
            rom = self._detail(entry, "rom", "file", "path", "rom_path")
            if not rom:
                return None
            rom = str(rom)
            base = rom if os.path.isdir(rom) else os.path.dirname(rom)
            if os.path.basename(base) == "code":       # <Spiel>/code/<name>.rpx
                base = os.path.dirname(base)
        meta = os.path.join(str(base), "meta")
        return meta if os.path.isdir(meta) else None

    def _shortcut_ids(self, entry) -> list:
        """IDs der Steam-Verknüpfungen, die zu diesem Eintrag gehören: sichere Treffer zuerst.

        Sicher = Startbefehl passt (lutris-steam-launch <slug>, lutris:rungameid/<id>, ROM-Pfad,
        dieselbe exe). Sonst zählt nur der exakt gleiche Name nach Normalisierung.
        """
        title = _norm(source_title(entry))      # Steam kennt den Namen der Quelle, nicht den eigenen (E90)
        slug = self._lutris_slug(entry)
        lutris_id = self._detail(entry, "lutris_id", "pga_id")
        rom = self._detail(entry, "rom", "file", "path", "rom_path")
        exe = self._detail(entry, "exe_path", "exe")
        exe_real = os.path.realpath(str(exe)) if exe and os.path.isabs(str(exe)) else None
        strong, by_name = [], []
        for sc in self.steam.shortcuts():
            opts = sc["options"]
            words = opts.split()
            hit = False
            if slug and os.path.basename(sc["exe"]) == "lutris-steam-launch" and words[:1] == [slug]:
                hit = True
            elif slug and re.search(r"lutris:rungame/%s(?![\w-])" % re.escape(slug), opts):
                hit = True
            elif lutris_id is not None and re.search(r"lutris:rungameid/%s(?!\d)" % re.escape(str(lutris_id)), opts):
                hit = True
            elif rom and str(rom) in opts:
                hit = True
            elif exe_real and sc["exe"] and os.path.realpath(sc["exe"]) == exe_real:
                hit = True
            if hit:
                strong.append(sc["appid"])
            elif title and sc["norm"] == title:
                by_name.append(sc["appid"])
        return strong + by_name

    def _app_style(self, entry):
        """(Symbolname, Pfad des vom Programm installierten Symbols oder None)."""
        key = self._detail(entry, "app") or (entry.id.split(":", 1)[1] if ":" in entry.id else entry.id)
        symbol, names = APP_STYLE.get(str(key), ("apps", ()))
        symbol = self._detail(entry, "symbol") or symbol
        given = self._detail(entry, "icon_name", "flatpak", "app_id")
        if given:
            names = tuple(given) if isinstance(given, (list, tuple)) else (str(given),)
        for name in names:
            for root in self.app_icon_roots:
                for sub, ext in (("512x512", ".png"), ("256x256", ".png"), ("scalable", ".svg"), ("128x128", ".png")):
                    path = os.path.join(root, "hicolor", sub, "apps", name + ext)
                    if os.path.isfile(path):
                        return symbol, path
        return symbol, None

    def _badge(self, entry) -> str:
        return getattr(entry, "badge", "") or BADGES.get(getattr(entry, "source", ""), "")

    # ------------------------------------------------------------------ Auswahl und Ableitung

    def _probe(self, cand: Candidate):
        """(breite, höhe) der Vorlage oder None, wenn sie sich nicht öffnen lässt. Liest nur den Kopf."""
        key = (cand.path, cand.mtime_ns, cand.size)
        if key not in self._probe_cache:
            try:
                with Image.open(cand.path) as im:
                    self._probe_cache[key] = im.size
            except Exception:                         # PIL wirft je nach Schaden Verschiedenes
                self._probe_cache[key] = None
            if len(self._probe_cache) > 4000:
                self._probe_cache.clear()
        return self._probe_cache[key]

    def _derive(self, folder, kind, mode, cand, ext, make, dry_run, extra: str = "", quality: int = 92) -> str | None:
        """Erzeugt (falls nötig) die Ableitung einer Vorlage. Rückgabe Pfad oder None bei Fehlschlag."""
        if folder is None:
            return None
        # Hash aus Rezept, Quelle, mtime und Größe: ändert sich die Vorlage, entsteht eine neue Datei
        ident = "|".join((str(RECIPE), kind, mode, cand.path, str(cand.mtime_ns), str(cand.size), extra))
        out = os.path.join(folder, "%s%s-%s%s" % (DERIVED_PREFIX, kind, _digest(ident), ext))
        if os.path.isfile(out) or dry_run:
            return out
        try:
            img = make(_open(cand.path))
            if img is None:
                return None
            placeholder.save_image(img, out, quality=quality)
            self.stats["derived"] += 1
            return out
        except Exception as exc:                      # kaputte Vorlage darf die Bibliothek nicht aufhalten
            self._log("art: Ableitung %s aus %s gescheitert: %s" % (kind, cand.path, exc))
            return None

    def _generated(self, folder, kind, ident: str, ext, make, dry_run) -> str | None:
        """Erzeugt (falls nötig) ein Bild ohne Vorlage (Platzhalter, App-Kachel)."""
        if folder is None:
            return None
        digest = _digest("%d|%d|%s|%s" % (RECIPE, placeholder.RECIPE, kind, ident))
        out = os.path.join(folder, "%s%s-%s%s" % (DERIVED_PREFIX, kind, digest, ext))
        if os.path.isfile(out) or dry_run:
            return out
        try:
            make(out)
            self.stats["derived"] += 1
            return out
        except Exception as exc:
            self._log("art: Platzhalter %s gescheitert: %s" % (kind, exc))
            return None

    def _build(self, entry, cands, folder, dry_run: bool) -> dict:
        sized = [(c, self._probe(c)) for c in cands]
        sized = [(c, d) for c, d in sized if d and min(d) >= 16]
        is_app = getattr(entry, "kind", "game") == "app"
        choices = {}

        def done(kind, source, mode, src, out, dims, good=False):
            pending = bool(dry_run and out and mode != "direct" and not os.path.isfile(out))
            choices[kind] = Choice(kind, source, mode, src, out, dims, good, pending)
            return True

        def of(*kinds):
            return [(c, d) for c, d in sized if c.kind in kinds]

        def direct_ok(c):
            # Unverändert ausliefern nur aus den Ordnern, die couch://art kennt (art_roots);
            # alles andere (Symbolordner, Spielordner) wird in den Zwischenspeicher umgewandelt.
            return c.path.lower().endswith(BROWSER_EXT) and self._servable(c.path)

        def recode_cover(im):
            src = _rgb(im)
            big = src.width > COVER_SIZE[0] or src.height > COVER_SIZE[1]
            return ImageOps.fit(src, COVER_SIZE, Image.LANCZOS) if big else src

        def shrink(box):
            def make(im):
                src = _rgb(im)
                src.thumbnail(box, Image.LANCZOS)
                return src
            return make

        # ---------------- cover (fürs Raster) und cover_full (beste Größe)
        def build_cover():
            for c, (w, h) in of("cover"):
                if w >= 200 and 0.5 <= w / h <= 0.85:
                    good = w >= 500
                    if w > COVER_UI_BOX[0] or h > COVER_UI_BOX[1]:
                        # Große Vorlage: Das Raster bekommt eine verkleinerte Fassung (46 Cover in
                        # voller Größe wären im WebView über 500 MB), die große bleibt daneben
                        small = self._derive(folder, "cover", "small", c, ".jpg", shrink(COVER_UI_BOX), dry_run,
                                             quality=90)
                        if not small:
                            continue
                        fits = w <= COVER_FULL_BOX[0] and h <= COVER_FULL_BOX[1]
                        full = c.path if (fits and direct_ok(c)) else self._derive(
                            folder, "cover_full", "fit", c, ".jpg", shrink(COVER_FULL_BOX), dry_run, quality=90)
                        done("cover", c.source, "small", c.path, small, (w, h), good)
                        return done("cover_full", c.source, "direct" if full == c.path else "fit", c.path,
                                    full or small, (w, h), good)
                    if direct_ok(c):
                        return done("cover", c.source, "direct", c.path, c.path, (w, h), good)
                    out = self._derive(folder, "cover", "convert", c, ".jpg", recode_cover, dry_run)
                    if out:
                        return done("cover", c.source, "convert", c.path, out, (w, h), good)
            if is_app:
                return False
            for c, (w, h) in of("cover", "header", "background", "hero", "tile", "icon"):
                if min(w, h) >= 64:
                    grow = 3.2 if c.kind == "icon" else 2.0
                    out = self._derive(folder, "cover", "composite", c, ".jpg",
                                       lambda im, grow=grow: make_composite(im, COVER_SIZE, grow, center=(0.5, 0.42)),
                                       dry_run)
                    if out:
                        return done("cover", c.source, "composite", c.path, out, (w, h))
            return False

        # ---------------- tile
        def build_tile():
            for c, (w, h) in of("tile"):
                square = 0.97 <= w / h <= 1.03
                if direct_ok(c) and square and 256 <= w <= 640:
                    return done("tile", c.source, "direct", c.path, c.path, (w, h), True)
                # quadratische Vorlage: nur verkleinern, nichts abschneiden (2160x2160 aus dem
                # Microsoft-Store, 1000x1000 von Nintendo)
                out = self._derive(folder, "tile", "square" if square else "crop", c, ".jpg", make_tile_crop, dry_run)
                if out:
                    return done("tile", c.source, "square" if square else "crop", c.path, out, (w, h), True)
            if is_app:
                return False
            options = []
            for c, (w, h) in of("cover"):
                # Ausschnitt nur, wenn er die Fokuskachel (336 px in 4K) ohne starkes Hochrechnen füllt
                options.append((c, (w, h), "crop" if min(w, h) >= 240 else "composite"))
            for c, (w, h) in of("header"):
                options.append((c, (w, h), "composite"))        # Logo steht mittig: nicht anschneiden
            for c, (w, h) in of("background", "hero"):
                options.append((c, (w, h), "crop" if h >= 240 else "composite"))
            for c, (w, h) in of("icon"):
                options.append((c, (w, h), "composite"))
            for c, (w, h), mode in options:
                if min(w, h) < 64:
                    continue
                grow = 2.75 if c.kind == "icon" else 2.0     # Symbole sonst briefmarkenklein
                make = make_tile_crop if mode == "crop" else (
                    lambda im, grow=grow: make_composite(im, (TILE_SIZE, TILE_SIZE), max_upscale=grow))
                out = self._derive(folder, "tile", mode, c, ".jpg", make, dry_run)
                if out:
                    return done("tile", c.source, mode, c.path, out, (w, h), mode == "crop")
            return False

        # ---------------- hero
        def build_hero():
            for c, (w, h) in of("hero", "background"):
                cw = hero_crop_width((w, h))
                if cw < 1000:
                    continue
                good = cw >= 1900
                if (c.path.lower().endswith((".jpg", ".jpeg", ".webp")) and self._servable(c.path)
                        and HERO_MIN_W <= w <= HERO_MAX_W and abs(w / h - 16 / 9) < 0.05):
                    return done("hero", c.source, "direct", c.path, c.path, (w, h), good)
                out = self._derive(folder, "hero", "sharp", c, ".jpg", make_hero_sharp, dry_run, quality=88)
                if out:
                    return done("hero", c.source, "sharp", c.path, out, (w, h), good)
            if is_app:
                return False
            for c, (w, h) in of("hero", "background", "cover", "header", "tile", "icon"):
                if min(w, h) < 64:
                    continue
                out = self._derive(folder, "hero", "ambient", c, ".jpg", make_hero_ambient, dry_run, quality=88)
                if out:
                    return done("hero", c.source, "ambient", c.path, out, (w, h))
            return False

        # ---------------- logo
        def build_logo():
            for c, (w, h) in of("logo"):
                if w < 160:
                    continue
                out = self._derive(folder, "logo", "trim", c, ".png", make_logo, dry_run)
                if out:
                    return done("logo", c.source, "trim", c.path, out, (w, h), True)
            return False

        # ---------------- icon
        def build_icon():
            for c, (w, h) in of("icon"):
                if min(w, h) < 64:
                    continue                          # Steams 32x32-Symbol taugt nicht für 160 px
                if c.path.lower().endswith((".png", ".jpg", ".jpeg")) and max(w, h) <= 512 and self._servable(c.path):
                    return done("icon", c.source, "direct", c.path, c.path, (w, h), True)
                out = self._derive(folder, "icon", "convert", c, ".png", make_icon, dry_run)
                if out:
                    return done("icon", c.source, "convert", c.path, out, (w, h), True)
            return False

        title, badge = entry.title or "", self._badge(entry)
        if not build_cover():
            if is_app:
                self._build_app(entry, "cover", folder, dry_run, done)
            else:
                out = self._generated(folder, "cover", "ph|%s|%s" % (title, badge), ".jpg",
                                      lambda p: placeholder.make_cover(title, badge, p), dry_run)
                done("cover", "placeholder" if out else "none", "placeholder" if out else "none", None, out, None)
        if not build_tile():
            if is_app:
                self._build_app(entry, "tile", folder, dry_run, done)
            else:
                out = self._generated(folder, "tile", "ph|%s|%s" % (title, badge), ".jpg",
                                      lambda p: placeholder.make_tile(title, badge, p), dry_run)
                done("tile", "placeholder" if out else "none", "placeholder" if out else "none", None, out, None)
        if not build_hero():
            if is_app:
                done("hero", "none", "none", None, None, None, True)     # Apps: ruhiger Standardhintergrund
            else:
                out = self._generated(folder, "hero", "ph|%s" % title, ".jpg",
                                      lambda p: placeholder.make_hero(title, p), dry_run)
                done("hero", "placeholder" if out else "none", "placeholder" if out else "none", None, out, None)
        if not build_logo():
            done("logo", "none", "none", None, None, None)
        if not build_icon():
            tile = choices["tile"]
            out = None
            if tile.out:
                # Kleine Fassung der Kachel; hängt am Namen der Kachel, erneuert sich also mit ihr
                def mini(path, src=tile.out):
                    placeholder.save_image(_rgb(_open(src)).resize((192, 192), Image.LANCZOS), path, quality=92)
                out = self._generated(folder, "icon", "mini|" + os.path.basename(tile.out), ".jpg", mini, dry_run)
            done("icon", tile.source if out else "none", "mini" if out else "none", tile.out if out else None, out, None)
        if "cover_full" not in choices:
            c = choices["cover"]
            choices["cover_full"] = Choice("cover_full", c.source, c.mode, c.src, c.out, c.dims, c.good, c.pending)
        return choices

    def _build_app(self, entry, kind, folder, dry_run, done) -> None:
        """App-Kachel bzw. -Cover: Symbol des Programms, sonst das eigene Liniensymbol."""
        symbol, icon_path = self._app_style(entry)
        title = entry.title or ""
        stamp = ""
        if icon_path:
            try:
                st = os.stat(icon_path)
                stamp = "%s|%d|%d" % (os.path.realpath(icon_path), st.st_mtime_ns, st.st_size)
            except OSError:
                icon_path = None
        used = {"icon": bool(icon_path)}

        def make(path):
            icon = load_program_icon(icon_path) if icon_path else None
            used["icon"] = icon is not None
            if kind == "cover":
                placeholder.make_app_cover(symbol, title, path, icon)
            else:
                placeholder.make_app_tile(symbol, title, path, icon)

        out = self._generated(folder, kind, "app|%s|%s|%s" % (symbol, title, stamp), ".jpg", make, dry_run)
        source = ("app-icon" if used["icon"] else "app-symbol") if out else "none"
        done(kind, source, "app" if out else "none", icon_path if used["icon"] else None, out, None, bool(out))

    # ------------------------------------------------------------------ eigener Ordner

    def _listdir(self, folder: str) -> list:
        try:
            return os.listdir(folder)
        except OSError:
            return []

    def _ensure_entry_dir(self, entry_id: str) -> str | None:
        """Legt den Ordner des Eintrags an (und die Marke im Bilderordner). None = nicht benutzbar."""
        folder = self.entry_dir(entry_id)
        try:
            os.makedirs(self.art_root, exist_ok=True)
            marker = os.path.join(self.art_root, MARKER)
            if not os.path.isfile(marker):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("Bilder-Zwischenspeicher von couch. Inhalt darf gelöscht werden.\n")
            if os.path.islink(folder):
                return None                           # ein Verweis an dieser Stelle ist nicht von uns
            os.makedirs(folder, exist_ok=True)
            return folder
        except OSError as exc:
            self._log("art: Ordner %s nicht anlegbar: %s" % (folder, exc))
            return None

    def _remove_own_file(self, folder: str, name: str) -> int:
        """Die einzige Löschfunktion: genau eine eigene Datei im Ordner eines Eintrags.

        Verweigert alles andere: fremde Namen, Unterordner, Ordner außerhalb des markierten
        Bilderordners, Eintragsordner, die selbst ein Verweis sind. Ein Verweis im Ordner wird
        als Verweis entfernt, sein Ziel bleibt. Rückgabe 1 = entfernt, 0 = nicht.
        """
        if not name or os.sep in name or name in (".", "..") or not name.startswith(OWN_PREFIXES):
            return 0
        folder = os.path.abspath(folder)
        if os.path.dirname(folder) != os.path.abspath(self.art_root) or os.path.islink(folder):
            return 0
        if os.path.realpath(os.path.dirname(folder)) != os.path.realpath(self.art_root):
            return 0
        if not os.path.isfile(os.path.join(self.art_root, MARKER)):
            return 0
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode):
                return 0
            os.unlink(path)
            self.stats["removed"] += 1
            return 1
        except OSError:
            return 0

    def _cleanup(self, folder: str, choices: dict) -> None:
        """Alte Ableitungen dieses Eintrags entfernen; liegen gebliebene Zwischendateien auch."""
        keep = {os.path.basename(c.out) for c in choices.values() if c.out}
        now = time.time()
        for name in self._listdir(folder):
            if name.startswith(DERIVED_PREFIX) and name not in keep:
                self._remove_own_file(folder, name)
            elif name.startswith(TMP_PREFIX):
                try:
                    if now - os.lstat(os.path.join(folder, name)).st_mtime > 600:
                        self._remove_own_file(folder, name)
                except OSError:
                    pass

    # ------------------------------------------------------------------ Netz

    def _fetchable(self, entry) -> bool:
        return (getattr(entry, "kind", "game") == "game"
                and getattr(entry, "source", "") in PC_SOURCES + EMU_SOURCES)

    def _get_fetcher(self):
        if self._fetcher is None:
            from .artfetch import ArtFetcher
            self._fetcher = ArtFetcher(self.cache_dir, self.config, online=self._online, log=self._log,
                                       sources=self._fetch_sources, image_interval=0.3,
                                       choices_path=os.path.join(self.art_root, CHOICES_FILE))
        return self._fetcher

    def cancel(self) -> None:
        """Das Home endet: Ein laufender Abruf bricht bei der nächsten Anfrage ab."""
        if self._fetcher is not None and hasattr(self._fetcher, "cancel"):
            self._fetcher.cancel()

    # ------------------------------------------------------------------ Vorschaubilder

    def _preview_dir(self) -> str | None:
        """Ordner der Vorschaubilder (mit der Marke des Bilderordners). None = nicht benutzbar."""
        folder = os.path.join(self.art_root, PREVIEW_DIR)
        try:
            os.makedirs(self.art_root, exist_ok=True)
            marker = os.path.join(self.art_root, MARKER)
            if not os.path.isfile(marker):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("Bilder-Zwischenspeicher von couch. Inhalt darf gelöscht werden.\n")
            if os.path.islink(folder):
                return None
            os.makedirs(folder, exist_ok=True)
            return folder
        except OSError as exc:
            self._log("art: Ordner %s nicht anlegbar: %s" % (folder, exc))
            return None

    def _prune_previews(self) -> None:
        """Hält den Vorschau-Ordner klein: Die ältesten Bilder gehen zuerst (einzeln, nie rekursiv)."""
        folder = os.path.join(self.art_root, PREVIEW_DIR)
        aged = []
        for name in self._listdir(folder):
            if name.startswith(THUMB_PREFIX):
                try:
                    aged.append((os.lstat(os.path.join(folder, name)).st_mtime, name))
                except OSError:
                    pass
        aged.sort()
        for _mtime, name in aged[:max(0, len(aged) - PREVIEW_KEEP)]:
            self._remove_own_file(folder, name)


# ====================================================================== Bericht (Befehlszeile)

def _report_entries() -> list:
    """Einträge für den Bericht: die Quellen der Bibliothek (couch.library.sources) gegen den echten
    Rechner, mit denselben IDs und damit denselben Bildordnern wie im Home. Wirft, wenn sie sich
    nicht lesen lassen (E99: kein eigener Leser mehr, der für Emulator-Spiele andere IDs bildete)."""
    from . import sources
    # home ausdrücklich: sonst läse der Kontext unter COUCH_TEST_ROOT den Attrappen-Baum.
    # cache_dir bleibt leer, die Quellen schreiben dann nichts.
    entries, _warnings, _timings = sources.load_all(sources.SourceContext(home=os.path.expanduser("~")))
    return list(entries)


def report(resolver: ArtResolver, entries: list, dry_run: bool = False) -> dict:
    """Löst alle Einträge auf und zählt, woher welches Bild kommt. BLOCKIEREND."""
    rows, counts, timing = [], {k: {} for k in KINDS}, []
    for entry in entries:
        started = time.monotonic()
        info = resolver.describe(entry, dry_run=dry_run)
        timing.append(time.monotonic() - started)
        for kind in KINDS:
            c = info[kind]
            label = c["source"] if c["mode"] in ("direct", "none", "placeholder", "app") else "%s (%s)" % (c["source"], c["mode"])
            counts[kind][label] = counts[kind].get(label, 0) + 1
        rows.append({"id": entry.id, "title": entry.title, "source": getattr(entry, "source", ""), "art": info,
                     "fetch": resolver.missing_kinds(entry) if (not dry_run and resolver._fetchable(entry)) else []})
    return {"rows": rows, "counts": counts, "seconds": timing}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m couch.library.art",
                                 description="Zeigt je Eintrag der echten Bibliothek, woher welches Bild käme.")
    ap.add_argument("--report", action="store_true", help="Bericht ausgeben")
    ap.add_argument("--json", action="store_true", help="maschinenlesbar")
    ap.add_argument("--dry-run", action="store_true", help="nichts in den Zwischenspeicher schreiben")
    args = ap.parse_args(argv)
    if not args.report:
        ap.print_help()
        return 2
    ctx = ArtContext.from_env()
    dry = args.dry_run
    if not os.environ.get("COUCH_TEST_ROOT") and not dry:
        # Ohne Prüfordner nie in den echten Zwischenspeicher schreiben: dann nur planen
        dry = True
        print("Hinweis: COUCH_TEST_ROOT ist nicht gesetzt, es wird nichts geschrieben (wie --dry-run).", file=sys.stderr)
    resolver = ArtResolver(ctx)
    try:
        entries = _report_entries()
    except Exception as exc:
        print("Fehler: Die Quellen der Bibliothek lassen sich nicht lesen (%s: %s)." % (type(exc).__name__, exc),
              file=sys.stderr)
        return 1
    origin = "couch.library.sources"
    result = report(resolver, entries, dry_run=dry)
    if args.json:
        json.dump({"entries_from": origin, "data_dir": ctx.data_dir, **result}, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    print("Einträge: %d (%s)   Zwischenspeicher: %s%s" % (len(entries), origin, resolver.art_root,
                                                          "   [Probelauf, nichts geschrieben]" if dry else ""))
    for row in result["rows"]:
        print("\n%s   [%s, %s]" % (row["title"], row["id"], row["source"]))
        for kind in KINDS:
            c = row["art"][kind]
            dims = "%dx%d" % tuple(c["dims"]) if c["dims"] else "-"
            name = os.path.basename(c["src"] or c["file"] or "") or "-"
            flag = "" if c["good"] or kind in ("tile", "icon") else "   (Behelf)"
            print("   %-6s %-13s %-11s %-10s %s%s" % (kind, c["source"], c["mode"], dims, name, flag))
        if row["fetch"]:
            print("   Netz-Abruf sinnvoll für: %s" % ", ".join(row["fetch"]))
    print("\nZusammenfassung (Bildart: Quelle = Anzahl)")
    for kind in KINDS:
        parts = ", ".join("%s = %d" % kv for kv in sorted(result["counts"][kind].items(), key=lambda kv: -kv[1]))
        print("   %-6s %s" % (kind, parts))
    secs = result["seconds"]
    if secs:
        print("\nZeit je Eintrag: Mittel %.1f ms, längster %.1f ms, gesamt %.2f s   (warm %d, kalt %d, erzeugt %d)"
              % (1000 * sum(secs) / len(secs), 1000 * max(secs), sum(secs), resolver.stats["warm"],
                 resolver.stats["cold"], resolver.stats["derived"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
