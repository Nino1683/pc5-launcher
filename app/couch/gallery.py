"""Mediengalerie (E120) und eigener Hintergrund des Home (E121).

Wunsch 2026-10-27: die Bildschirmfotos (capture.py) als eigene Galerie wie an der Konsole, und das eigene Bild
bzw. eine Diashow als Hintergrund des Home. Hier liegt, was die Platte anfasst; die Seite steht in
web/js/screens/gallery.js und web/js/components/backdrop.js, angemeldet wird in wiring_gallery.py.

Quellen (nur diese beiden Ordner, nie Unterordner):
    screenshots  der Ordner der Bildschirmfotos (capture.folder): „<Titel> <JJJJ-MM-TT HH-MM-SS>.png“,
                 Titel und Zeit stehen im Dateinamen (parse_name)
    wallpapers   „Hintergründe“ im Bilder-Ordner nach XDG (bzw. „Wallpapers“, wenn der Bilder-Ordner
                 englisch „Pictures“ heißt), wie capture.folder seinen Ordner findet; angelegt nur auf
                 Wunsch (Einstellungen › Home-Bildschirm › Ordner „Hintergründe“ öffnen)
Angenommen wird nur eine echte Datei (kein Verweis) mit Bild-Endung direkt in einem der Ordner (checked).

Vorschaubilder und Hintergrund-Fassungen liegen im Zwischenspeicher <cache>/gallery (Cache): JPEG,
klein (640x360, für das Raster) bzw. höchstens 2560x1440 (Hintergrund: ein 4K-PNG dekodiert die Seite
sonst bei jedem Wechsel neu, E121). Der Name enthält Pfad, mtime und Größe der Vorlage: Ändert sie sich,
entsteht eine neue Datei. Der Ordner trägt eine Marke; gelöscht werden dort nur einzelne eigene Dateien
(t-, b-, .tmp-), nie rekursiv (SAFETY 13). Ausgeliefert wird über couch://art (wiring_gallery meldet den
Ordner vor dem ersten Fenster als Wurzel an), die Bilder selbst in voller Größe über couch://files wie
im Datei-Explorer (ui/host.py prüft dort bei jedem Abruf files.allowed).

Löschen (delete) nur in den Papierkorb und nur über textedit.default_trash (Gio-Papierkorb, im Prüfmodus
der im Käfig): derselbe Weg wie der Datei-Explorer, kein eigenes Löschen.

PIL wird erst beim ersten Vorschaubild geladen. Alles mit PIL blockiert (100 bis 400 ms je 4K-PNG):
nur im eigenen Arbeits-Thread rufen (wiring_gallery, Lane „couch-gallery“).
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import time
import urllib.parse
from typing import Any, Callable

from . import capture, log, paths

_log = log.get("gallery")

SCREENSHOTS = "screenshots"
WALLPAPERS = "wallpapers"
SOURCES = (SCREENSHOTS, WALLPAPERS)
EXTS = (".png", ".jpg", ".jpeg", ".webp")
MAX_ITEMS = 5000                    # so viele Bilder je Ordner listet die Galerie höchstens
MAX_BYTES = 80 * 1024 * 1024        # größere Dateien bekommen kein Vorschaubild
MAX_PIXELS = 60_000_000             # 8K hat 33 Mio.; darüber ist es kein Bildschirmfoto mehr
THUMB_BOX = (640, 360)              # Raster: in 4K höchstens rund 600 Pixel breit
BACKDROP_BOX = (2560, 1440)         # Hintergrund: wird ohnehin abgedunkelt und leicht vergrößert
THUMB_QUALITY, BACKDROP_QUALITY = 82, 88
RECIPE = 1                          # steckt im Namen jeder Ableitung: hochzählen, wenn sich ein Rezept ändert
THUMB_PREFIX, BACKDROP_PREFIX, TMP_PREFIX = "t-", "b-", ".tmp-"
OWN_PREFIXES = (THUMB_PREFIX, BACKDROP_PREFIX, TMP_PREFIX)
KEEP = {THUMB_PREFIX: 3000, BACKDROP_PREFIX: 40}     # so viele bleiben liegen, die ältesten gehen zuerst
MARKER = ".couch-gallery"
CACHE_SUB = "gallery"

# Hintergrund des Home (E121): ui.background, ui.background_image, ui.background_slides, ui.background_interval
MODES = ("game", "image", "slideshow")
SLIDES = ("all", WALLPAPERS, SCREENSHOTS)
INTERVALS = (30, 60, 300, 900)
DEFAULTS = {"ui.background": "game", "ui.background_image": "", "ui.background_slides": "all",
            "ui.background_interval": 60}

TEXT_GONE = "Dieses Bild gibt es nicht mehr."
TEXT_OUTSIDE = "Dieses Bild liegt nicht in der Mediengalerie."
TEXT_TRASHED = "In den Papierkorb gelegt: %s"
TEXT_BACKGROUND = "Das Bild ist jetzt der Hintergrund des Home."

# „Hollow Knight 2026-10-21 13-05-12.png“, bei gleicher Sekunde „… 13-05-12 (2).png“ (capture.take)
_NAME = re.compile(r"^(?P<title>.+?) (?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}-\d{2}-\d{2})(?: \(\d+\))?$")


# ---------------------------------------------------------------- Ordner und Namen
def wallpaper_folder() -> str:
    """„Hintergründe“ im Bilder-Ordner (Prüfmodus: im Käfig, wie capture.folder). Legt nichts an."""
    pictures = paths.user_dir_at("PICTURES", missing="de")
    sub = "Wallpapers" if os.path.basename(pictures.rstrip(os.sep)) == "Pictures" else "Hintergründe"
    return os.path.join(pictures, sub)


def folder_of(source: str) -> str:
    return capture.folder() if source == SCREENSHOTS else wallpaper_folder()


def ensure_wallpaper_folder() -> str:
    """Auf ausdrücklichen Wunsch: den Ordner „Hintergründe“ anlegen (nur mkdir). Liefert den Pfad."""
    folder = wallpaper_folder()
    os.makedirs(folder, exist_ok=True)
    return folder


def parse_name(name: str) -> tuple[str, float | None]:
    """Titel und Aufnahmezeit aus dem Dateinamen der Konsole; sonst ("", None)."""
    stem = os.path.splitext(name)[0]
    m = _NAME.match(stem)
    if not m:
        return "", None
    try:
        when = time.mktime(time.strptime(m.group("date") + " " + m.group("time"), "%Y-%m-%d %H-%M-%S"))
    except (ValueError, OverflowError):
        return "", None
    return m.group("title").strip(), when


def key_of(path: str) -> str:
    """Kurze Kennung eines Bildes für die Seite (geht durch die Übersetzung, ein Pfad nicht unbedingt)."""
    return hashlib.sha1(os.path.realpath(path).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def files_url(path: str) -> str:
    """Bild in voller Größe, wie der Datei-Explorer es zeigt (wiring_files.image)."""
    return "couch://files" + urllib.parse.quote(os.path.abspath(path))


def checked(path: Any, sources: tuple = SOURCES) -> tuple[str, str, os.stat_result] | None:
    """(Pfad, Quelle, stat), wenn path eine echte Bilddatei direkt in einem der Ordner ist; sonst None.
    Verweise zählen nicht (lstat), Unterordner und versteckte Dateien auch nicht."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return None
    path = os.path.normpath(path)
    name = os.path.basename(path)
    if not name or name.startswith(".") or not name.lower().endswith(EXTS):
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    parent = os.path.realpath(os.path.dirname(path))
    for source in sources:
        if source in SOURCES and parent == os.path.realpath(folder_of(source)):
            return path, source, st
    return None


def _items_of(source: str, limit: int = MAX_ITEMS) -> tuple[list[dict], bool]:
    folder = folder_of(source)
    try:
        names = os.listdir(folder)
    except OSError:
        return [], False
    out: list[dict] = []
    for name in names:
        if name.startswith(".") or not name.lower().endswith(EXTS):
            continue
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            continue                    # Name ist kein UTF-8 (altes Archiv): sonst scheiterte die ganze Liste
        path = os.path.join(folder, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        title, when = parse_name(name) if source == SCREENSHOTS else (os.path.splitext(name)[0], None)
        out.append({"path": path, "name": name, "title": title, "time": int(when if when is not None else st.st_mtime),
                    "size": st.st_size, "source": source, "key": key_of(path), "_st": st})
    out.sort(key=lambda i: (-i["time"], i["name"]))
    return out[:limit], len(out) > limit


def list_images(sources: tuple = (SCREENSHOTS,), cache: "Cache | None" = None) -> dict:
    """Bilder der Quellen, neueste zuerst. Mit cache: schon vorhandene Vorschaubilder gleich dabei
    (thumb), sonst None (die Seite fragt sie über gallery.thumbs nach). Liest nur."""
    items: list[dict] = []
    truncated = False
    for source in sources:
        if source not in SOURCES:
            continue
        got, cut = _items_of(source)
        items += got
        truncated = truncated or cut
    items.sort(key=lambda i: (-i["time"], i["name"]))
    for item in items:
        st = item.pop("_st")
        item["url"] = files_url(item["path"])
        item["thumb"] = cache.cached(THUMB_PREFIX, item["path"], st) if cache is not None else None
    return {"ok": True, "items": items, "truncated": truncated,
            "folders": {s: folder_of(s) for s in SOURCES}}


def slide_sources(choice: Any) -> tuple:
    return {WALLPAPERS: (WALLPAPERS,), SCREENSHOTS: (SCREENSHOTS,)}.get(choice, (WALLPAPERS, SCREENSHOTS))


def count(sources: tuple) -> int:
    return sum(len(_items_of(s)[0]) for s in sources if s in SOURCES)


# ---------------------------------------------------------------- Einstellungen des Hintergrunds
def _cfg(config: Any, key: str) -> Any:
    try:
        return config.get(key, DEFAULTS[key])
    except Exception:  # noqa: BLE001 - eine kaputte Einstellung gilt als Vorgabe
        return DEFAULTS[key]


def mode_of(config: Any) -> str:
    value = _cfg(config, "ui.background")
    return value if value in MODES else "game"


def interval_of(config: Any) -> int:
    value = _cfg(config, "ui.background_interval")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULTS["ui.background_interval"]
    return min(INTERVALS, key=lambda v: abs(v - value))


def slides_of(config: Any) -> str:
    value = _cfg(config, "ui.background_slides")
    return value if value in SLIDES else "all"


def image_of(config: Any) -> str:
    value = _cfg(config, "ui.background_image")
    return value if isinstance(value, str) else ""


def ui_background(config: Any) -> dict:
    """sys.state.background für die Seite: ohne Dateizugriff (läuft bei jeder Änderung in der Hauptschleife).
    key wechselt mit dem gewählten Bild; die Seite holt das Bild dann über gallery.backdrop."""
    image = image_of(config)
    return {"mode": mode_of(config), "interval": interval_of(config), "slides": slides_of(config),
            "key": key_of(image) if image else ""}


_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def describe(path: str, lang: str = "de") -> str:
    """Kurze Angabe zum gewählten Bild für die Zeile in den Einstellungen. Das Datum nach der Sprache: Die
    Übersetzung an der Brücke erkennt „21.10.2026“ nicht (stand bei Englisch so da)."""
    title, when = parse_name(os.path.basename(path))
    if title and when is not None:
        t = time.localtime(when)
        day = "%s %d, %d" % (_MONTHS_EN[t.tm_mon - 1], t.tm_mday, t.tm_year) if lang == "en" else \
            time.strftime("%d.%m.%Y", t)
        return "%s · %s" % (title, day)
    return os.path.splitext(os.path.basename(path))[0]


# ---------------------------------------------------------------- Zwischenspeicher
def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:24]


class Cache:
    """Vorschaubilder (t-) und Hintergrund-Fassungen (b-) im eigenen, markierten Ordner. make()
    blockiert (PIL); cached() liest nur. Ein Lock hält zwei Threads davon ab, dieselbe Datei zu bauen."""

    def __init__(self, folder: str | None = None,
                 art_url: Callable[[str], str] | None = None):
        self.folder = folder or os.path.join(paths.cache_dir(), CACHE_SUB)
        self._lock = threading.Lock()
        self._art_url = art_url or (lambda p: "couch://art/" + urllib.parse.quote(os.path.abspath(p), safe=""))

    def ensure(self) -> bool:
        """Ordner und Marke anlegen. False = nicht benutzbar (Verweis an seiner Stelle, kein Platz)."""
        try:
            if os.path.islink(self.folder):
                return False
            os.makedirs(self.folder, exist_ok=True)
            marker = os.path.join(self.folder, MARKER)
            if not os.path.isfile(marker):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write("Vorschaubilder der Mediengalerie (PC5 Launcher). Inhalt darf gelöscht werden.\n")
            return True
        except OSError as e:
            _log.warning("Mediengalerie: Zwischenspeicher %s nicht anlegbar: %s", self.folder, e)
            return False

    def name(self, prefix: str, path: str, st: os.stat_result) -> str:
        return "%s%s.jpg" % (prefix, _digest("%d|%s|%s|%d|%d" % (RECIPE, prefix, os.path.realpath(path),
                                                                  st.st_mtime_ns, st.st_size)))

    def cached(self, prefix: str, path: str, st: os.stat_result) -> str | None:
        out = os.path.join(self.folder, self.name(prefix, path, st))
        return self._art_url(out) if os.path.isfile(out) else None

    def thumb(self, path: str) -> str | None:
        return self.make(THUMB_PREFIX, path)

    def backdrop(self, path: str) -> str | None:
        return self.make(BACKDROP_PREFIX, path)

    def make(self, prefix: str, path: str) -> str | None:
        """couch://art-Adresse der Ableitung oder None (kein Bild, zu groß, kaputt). BLOCKIEREND."""
        hit = checked(path)
        if hit is None:
            return None
        path, _source, st = hit
        if not 0 < st.st_size <= MAX_BYTES:
            return None
        out = os.path.join(self.folder, self.name(prefix, path, st))
        with self._lock:
            if os.path.isfile(out):
                try:
                    os.utime(out)               # zuletzt benutzt: bleibt beim Aufräumen länger
                except OSError:
                    pass
                return self._art_url(out)
            if not self.ensure():
                return None
            try:
                img = render(path, THUMB_BOX if prefix == THUMB_PREFIX else BACKDROP_BOX, crop=prefix == THUMB_PREFIX)
                self._save(img, out, THUMB_QUALITY if prefix == THUMB_PREFIX else BACKDROP_QUALITY)
            except Exception as e:  # noqa: BLE001 - kein Bild, kaputt, zu groß: dann ohne Ableitung
                _log.info("Mediengalerie: kein %s für %s (%s)", "Vorschaubild" if prefix == THUMB_PREFIX
                          else "Hintergrund", os.path.basename(path), e)
                return None
            self.prune(prefix)
        return self._art_url(out)

    def _save(self, img: Any, out: str, quality: int) -> None:
        tmp = os.path.join(self.folder, "%s%d-%d-%s" % (TMP_PREFIX, os.getpid(), threading.get_ident(),
                                                        os.path.basename(out)))
        try:
            img.save(tmp, "JPEG", quality=quality)
            os.replace(tmp, out)
        finally:
            self._remove_own(os.path.basename(tmp))

    def forget(self, path: str, st: os.stat_result) -> int:
        """Die Ableitungen eines Bildes entfernen (vor dem Löschen der Vorlage). Zahl der entfernten."""
        return sum(self._remove_own(self.name(p, path, st)) for p in (THUMB_PREFIX, BACKDROP_PREFIX))

    def prune(self, prefix: str) -> None:
        """Hält den Ordner klein: über KEEP gehen die am längsten unbenutzten zuerst (einzeln)."""
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        aged = []
        now = time.time()
        for name in names:
            full = os.path.join(self.folder, name)
            try:
                mtime = os.lstat(full).st_mtime
            except OSError:
                continue
            if name.startswith(prefix):
                aged.append((mtime, name))
            elif name.startswith(TMP_PREFIX) and now - mtime > 600:
                self._remove_own(name)          # liegen gebliebene Zwischendatei (Absturz beim Speichern)
        aged.sort()
        for _mtime, name in aged[:max(0, len(aged) - KEEP.get(prefix, 0))]:
            self._remove_own(name)

    def _remove_own(self, name: str) -> int:
        """Die einzige Löschstelle hier: genau eine eigene Datei direkt im markierten Ordner.
        Verweigert fremde Namen, Unterordner und einen Ordner, der selbst ein Verweis ist."""
        if not name or os.sep in name or name in (".", "..") or not name.startswith(OWN_PREFIXES):
            return 0
        if os.path.islink(self.folder) or not os.path.isfile(os.path.join(self.folder, MARKER)):
            return 0
        full = os.path.join(self.folder, name)
        try:
            if stat.S_ISDIR(os.lstat(full).st_mode):
                return 0
            os.unlink(full)
            return 1
        except OSError:
            return 0


def render(path: str, box: tuple[int, int], crop: bool) -> Any:
    """Bild als RGB in der Größe für box. crop: genau box (Mitte, wie object-fit: cover); sonst nur
    auf 16:9 zugeschnitten und höchstens box groß, nie vergrößert. BLOCKIEREND."""
    from PIL import Image, ImageOps
    bw, bh = box
    with Image.open(path) as im:
        w, h = im.size
        if w < 1 or h < 1 or w * h > MAX_PIXELS:
            raise ValueError("%dx%d" % (w, h))
        if im.format == "JPEG":
            im.draft("RGB", (bw, bh))           # JPEG gleich verkleinert dekodieren (bleibt >= box)
        im = ImageOps.exif_transpose(im)        # Fotos vom Handy stehen sonst quer
        if im.mode in ("RGBA", "LA", "P", "PA"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            im = flat
        elif im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        # Ausschnitt im Seitenverhältnis von box, mittig
        ratio = bw / bh
        cw, ch = (w, w / ratio) if w / h < ratio else (h * ratio, h)
        x0, y0 = (w - cw) / 2, (h - ch) / 2
        area = (x0, y0, x0 + cw, y0 + ch)
        if crop:
            size = (bw, bh)
        else:
            scale = min(1.0, bw / cw)
            size = (max(1, round(cw * scale)), max(1, round(ch * scale)))
        return im.resize(size, Image.LANCZOS, box=area, reducing_gap=2.0)


# ---------------------------------------------------------------- Dienst
class Gallery:
    """Was die RPC-Methoden tun (wiring_gallery). config: get/set; trash(path) legt eine Datei in den
    Papierkorb (Vorgabe textedit.default_trash). Alles blockiert: in Arbeits-Threads rufen."""

    def __init__(self, config: Any, cache: Cache | None = None, trash: Callable[[str], None] | None = None):
        self.config = config
        self.cache = cache or Cache()
        self._trash = trash

    def list(self, sources: Any = None, game: str | None = None, limit: int | None = None, keep_all: bool = False) -> dict:
        """game: nur die Bildschirmfotos dieses Spiels (Titel wie im Dateinamen, capture.clean_title, ohne
        Groß-/Kleinschreibung); mit keep_all bleiben alle Bilder, out["game"] nennt dann nur den Titel (die
        Galerie öffnet mit diesem Filter). limit: höchstens so viele, die neuesten zuerst."""
        wanted = tuple(s for s in (sources or [SCREENSHOTS]) if s in SOURCES) or (SCREENSHOTS,)
        out = list_images(wanted, self.cache)
        if game:
            name = capture.clean_title(game)
            out["game"] = name
            if not keep_all:
                out["items"] = [i for i in out["items"] if i["source"] == SCREENSHOTS
                                and i["title"].casefold() == name.casefold()]
        if limit:
            out["truncated"] = out["truncated"] or len(out["items"]) > limit
            out["items"] = out["items"][:limit]
        image = image_of(self.config)
        out["current"] = key_of(image) if image and mode_of(self.config) == "image" else ""
        return out

    def thumbs(self, paths_: Any, limit: int = 12) -> dict:
        got: dict[str, str | None] = {}
        for p in (paths_ if isinstance(paths_, list) else [])[:limit]:
            if isinstance(p, str):
                got[p] = self.cache.thumb(p)
        return {"thumbs": got}

    def delete(self, path: Any) -> dict:
        hit = checked(path)
        if hit is None:
            exists = isinstance(path, str) and os.path.lexists(path)
            return {"ok": False, "message": TEXT_OUTSIDE if exists else TEXT_GONE}
        path, _source, st = hit
        self.cache.forget(path, st)
        trash = self._trash
        if trash is None:
            from .textedit import default_trash
            trash = default_trash
        try:
            trash(path)
        except (OSError, ValueError) as e:
            message = str(e) if isinstance(e, ValueError) else "Nicht in den Papierkorb: %s" % (e.strerror or e)
            return {"ok": False, "message": message}
        _log.info("Mediengalerie: %s in den Papierkorb", os.path.basename(path))
        if image_of(self.config) and os.path.realpath(image_of(self.config)) == os.path.realpath(path):
            self.config.set("ui.background_image", "")      # der Hintergrund zeigt dann wieder Spielbilder
        return {"ok": True, "message": TEXT_TRASHED % os.path.basename(path)}

    def set_background(self, path: Any) -> dict:
        hit = checked(path)
        if hit is None:
            return {"ok": False, "message": TEXT_GONE}
        self.config.set("ui.background_image", hit[0])
        self.config.set("ui.background", "image")
        return {"ok": True, "message": TEXT_BACKGROUND}

    def backdrop(self, after: Any = "") -> dict:
        """Bild für den Hintergrund des Home: bei „Eigenes Bild“ das gewählte, bei „Diashow“ das nach
        after (Kennung des gezeigten). url None = Spielbilder wie bisher. BLOCKIEREND (PIL)."""
        mode = mode_of(self.config)
        base = {"mode": mode, "interval": interval_of(self.config), "url": None, "key": ""}
        if mode == "image":
            image = image_of(self.config)
            url = self.cache.backdrop(image) if image else None
            return {**base, "url": url, "key": key_of(image) if url else ""}
        if mode != "slideshow":
            return base
        items = list_images(slide_sources(slides_of(self.config)))["items"]
        # Feste, gemischte Reihenfolge (nach einem Hash des Namens): ohne gespeicherten Stand kommt jedes
        # Bild einmal dran, bevor sich eines wiederholt
        items.sort(key=lambda i: _digest(i["name"]))
        keys = [i["key"] for i in items]
        start = keys.index(after) + 1 if isinstance(after, str) and after in keys else 0
        for step in range(min(len(items), 5)):             # kaputte Bilder überspringen, nicht ewig suchen
            item = items[(start + step) % len(items)]
            url = self.cache.backdrop(item["path"])
            if url:
                return {**base, "url": url, "key": item["key"], "count": len(items)}
        return {**base, "count": len(items)}
