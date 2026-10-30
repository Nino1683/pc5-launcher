"""Spielinfos, Neuigkeiten und Trailer aus Steam für die Spielansicht (E123), ohne Anmeldung.

Quellen:
    store.steampowered.com/api/appdetails?appids=<id>&l=<german|english>&filters=…
        Kurzbeschreibung, Genres, Entwickler, Publisher, Erscheinungsdatum, Metacritic, Bilder, Videos
    api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=<id>&count=…&maxlength=…&feeds=steam_community_announcements
        nur die offiziellen Ankündigungen und Patch-Notizen; ohne feeds kam für Cyberpunk 2077 vor allem
        die Presseschau fremder Seiten (2026-10-27: fünf von sechs Meldungen russische Artikel)
    die HLS-Listen eines Trailers auf video.*.steamstatic.com

Videos: appdetails nennt seit 2025 nur noch dash_av1, dash_h264 und hls_h264 (gesehen 2026-10-27 bei
Cyberpunk 2077 und Portal 2, kein mp4/webm mehr). WebKitGTK 2.54 spielt auf diesem Rechner weder HLS direkt
(„Ihrer Installation von GStreamer fehlt ein Plugin“, canPlayType leer) noch H.264 über Media Source
Extensions (vah264dec: „DMABuf caps negotiated without the mandatory support of VideoMeta“). trailer() nennt
darum Anfangsstück und ALLE Stücke einer Fassung (Hintergrund bis 720p, groß bis 1080p) samt Tonliste
(EXT-X-MEDIA TYPE=AUDIO, AAC in fMP4); abspielen lässt sie trailerfeed.py als einen Datenstrom (E132).
movies() nennt alle Trailer eines Spiels (Store, E132).

Zwischenspeicher in <cache>/steaminfo (store/cache.py): Angaben 1 Tag, Neuigkeiten 3 Stunden, Trailer-Listen
1 Tag; ohne Netz gilt der letzte Stand. appdetails drosselt Steam (laut Berichten rund 200 Abrufe in
5 Minuten): höchstens ein Abruf je GAP_S, nach 429/403 PAUSE_S Ruhe. Alles hier blockiert (Netz, Platte)
und gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import cache
from .cache import StoreError
from .steam import NotListed, clip, strip_html

SHOP = "https://store.steampowered.com"
API = "https://api.steampowered.com"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"
TIMEOUT_S = 15.0
LANGS = {"de": "german", "en": "english"}
FILTERS = "basic,genres,developers,publishers,release_date,metacritic,screenshots,movies"
INFO_TTL = 24 * 3600
NEWS_TTL = 3 * 3600
TRAILER_TTL = 24 * 3600
GAP_S = 1.0                     # Abstand zweier appdetails-Abrufe
PAUSE_S = 600.0                 # nach 429/403 so lange kein appdetails
NEWS_COUNT = 6
NEWS_CHARS = 600                # so viel Text je Meldung (Steam kürzt und nimmt dabei BBCode und HTML heraus)
SHOTS_MAX = 12
TRAILER_MAX_H = 720             # Hintergrund: 480p bzw. 720p reicht für ein abgedunkeltes Bild
TRAILER_FULL_H = 1080           # groß mit Ton (Store)
TRAILER_MAX_PARTS = 600         # Stücke zu je rund 3 s: höchstens 30 min; längere Listen gelten als kaputt
MOVIES_MAX = 12
# Woher Bilder, Videos und Links kommen dürfen (die Seite lädt sie selbst bzw. öffnet sie im Browser)
IMAGE_HOSTS = (".steamstatic.com", "steamcdn-a.akamaihd.net")
VIDEO_HOSTS = (".steamstatic.com",)
NEWS_HOSTS = ("steamstore-a.akamaihd.net", "store.steampowered.com", "steamcommunity.com")
MSG_OFFLINE = "Steam ist gerade nicht erreichbar."
MSG_PAUSE = "Steam bremst gerade zu viele Anfragen. Später noch einmal."
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,100}){0,3}")
_BBCODE = re.compile(r"\[/?[A-Za-z*][A-Za-z0-9*]{0,15}(?:=[^\]]{0,300})?\]")
_PLACEHOLDER = re.compile(r"\{STEAM_[A-Z_]+\}\S*")
# Umlaute als \x-Folgen: tools/i18n-extract hielte die Zeichenkette sonst für einen deutschen Text
_GLUED = re.compile(r"(?<=[a-z\xe4\xf6\xfc\xdf0-9)][.!?])(?=[A-Z\xc4\xd6\xdc])")


def _get(url: str, timeout: float = TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (Adressen aus festen Teilen)
        return resp.read(4 * 1024 * 1024)


def lang_param(lang: str) -> str:
    return LANGS.get(str(lang or ""), "english")


def allowed(url: Any, hosts: tuple, loopback: bool = False) -> bool:
    """https zu einem der Rechner (".x" = auch Unterrechner); im Prüfmodus mit Attrappe auch http://127.0.0.1."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if loopback and parts.scheme == "http" and host in ("127.0.0.1", "localhost"):
        return True
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    return any(host.endswith(h) if h.startswith(".") else host == h for h in hosts)


def news_text(text: Any, limit: int = NEWS_CHARS) -> str:
    """Text einer Meldung ohne BBCode, HTML und Platzhalter; Sätze, die Steam beim Kürzen aneinanderklebt
    („talk.Join us“), bekommen ihr Leerzeichen zurück."""
    plain = _BBCODE.sub(" ", str(text or ""))
    plain = _PLACEHOLDER.sub(" ", plain)
    plain = strip_html(plain)
    plain = _GLUED.sub(" ", plain)
    return clip(re.sub(r"\s+", " ", plain).strip(), limit)


def _plain(text: Any, limit: int) -> str:
    return clip(html.unescape(re.sub(r"<[^>]+>", "", str(text or ""))), limit)


def parse_details(appid: int, raw: Any, shop: str = SHOP, loopback: bool = False) -> dict:
    """Antwort von appdetails -> Angaben für „Über das Spiel“. Wirft NotListed (success:false)."""
    node = raw.get(str(appid)) if isinstance(raw, dict) else None
    if not isinstance(node, dict) or not node.get("success") or not isinstance(node.get("data"), dict):
        raise NotListed("Zu diesem Spiel hat Steam keine Angaben.")
    d = node["data"]
    shots = []
    for s in d.get("screenshots") or []:
        if not isinstance(s, dict):
            continue
        thumb, full = s.get("path_thumbnail"), s.get("path_full")
        if allowed(thumb, IMAGE_HOSTS, loopback) and allowed(full, IMAGE_HOSTS, loopback):
            shots.append({"thumb": str(thumb), "full": str(full)})
    release = d.get("release_date") if isinstance(d.get("release_date"), dict) else {}
    meta = d.get("metacritic") if isinstance(d.get("metacritic"), dict) else {}
    try:
        score = int(meta.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "appid": int(appid), "title": _plain(d.get("name"), 200),
        "short": clip(strip_html(str(d.get("short_description") or "")), 600),
        "about": strip_html(str(d.get("about_the_game") or d.get("detailed_description") or ""))[:6000],
        "genres": [_plain(g.get("description"), 60) for g in d.get("genres") or [] if isinstance(g, dict)][:4],
        "developers": [_plain(x, 80) for x in d.get("developers") or [] if isinstance(x, str)][:3],
        "publishers": [_plain(x, 80) for x in d.get("publishers") or [] if isinstance(x, str)][:3],
        "release": _plain(release.get("date"), 40), "coming_soon": bool(release.get("coming_soon")),
        "metacritic": score if 0 < score <= 100 else 0,
        "screenshots": shots[:SHOTS_MAX],
        "url": "%s/app/%d/" % (shop, int(appid)),
    }


def _movies(raw: Any, appid: int, loopback: bool = False) -> list[dict]:
    """Trailer mit HLS (H.264) aus appdetails, die hervorgehobenen zuerst (sonst in Steams Reihenfolge)."""
    node = raw.get(str(appid)) if isinstance(raw, dict) else None
    data = node.get("data") if isinstance(node, dict) and isinstance(node.get("data"), dict) else {}
    movies = [m for m in data.get("movies") or [] if isinstance(m, dict) and allowed(m.get("hls_h264"), VIDEO_HOSTS, loopback)]
    movies.sort(key=lambda m: not m.get("highlight"))
    return movies[:MOVIES_MAX]


def movie_id(movie: dict) -> str:
    """Kennung eines Trailers für die Seite: Steams id, sonst ein Hash der Adresse."""
    raw = movie.get("id")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return str(raw)
    return hashlib.sha1(str(movie.get("hls_h264") or "").encode("utf-8")).hexdigest()[:12]


def pick_movie(raw: Any, appid: int, loopback: bool = False, movie: str | None = None) -> dict | None:
    """Der Trailer: der genannte (movie, Kennung wie movie_id), sonst der erste hervorgehobene, sonst der erste."""
    movies = _movies(raw, appid, loopback)
    if movie is not None:
        return next((m for m in movies if movie_id(m) == str(movie)), None)
    return movies[0] if movies else None


def _attrs(line: str) -> dict:
    return {k.upper(): v.strip('"') for k, v in re.findall(r'([A-Za-z-]+)=("[^"]*"|[^,]*)', line)}


def parse_audio(text: str) -> dict[str, str]:
    """Tonspuren einer HLS-Hauptliste: {GROUP-ID: Liste}; je Gruppe die Vorgabe (DEFAULT=YES), sonst die erste."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        a = _attrs(line.split(":", 1)[1])
        uri, group = a.get("URI", ""), a.get("GROUP-ID", "")
        if a.get("TYPE") != "AUDIO" or not uri or not _NAME.fullmatch(uri) or ".." in uri:
            continue
        if group not in out or a.get("DEFAULT") == "YES":
            out[group] = uri
    return out


def parse_master(text: str) -> list[dict]:
    """Fassungen einer HLS-Hauptliste: [{"name", "width", "height", "codecs", "bandwidth", "audio"}]."""
    out, pending = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attrs(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending is not None:
            m = re.fullmatch(r"(\d+)x(\d+)", pending.get("RESOLUTION", ""))
            try:
                bandwidth = int(pending.get("BANDWIDTH") or 0)
            except ValueError:
                bandwidth = 0
            if m and _NAME.fullmatch(line) and ".." not in line:
                out.append({"name": line, "width": int(m.group(1)), "height": int(m.group(2)),
                            "codecs": pending.get("CODECS", ""), "bandwidth": bandwidth, "audio": pending.get("AUDIO", "")})
            pending = None
    return out


def pick_variant(variants: list[dict], max_height: int = TRAILER_MAX_H) -> dict | None:
    """Die größte Fassung bis max_height; gibt es keine, die kleinste."""
    small = [v for v in variants if v["height"] <= max_height]
    if small:
        return max(small, key=lambda v: (v["height"], -v["bandwidth"]))
    return min(variants, key=lambda v: v["height"]) if variants else None


def parse_media(text: str) -> tuple[str, list[tuple[str, float]]]:
    """Medienliste: (Anfangsstück aus EXT-X-MAP, [(Stück, Sekunden)])."""
    init, parts, seconds = "", [], 0.0
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            init = _attrs(line.split(":", 1)[1]).get("URI", "")
        elif line.startswith("#EXTINF:"):
            try:
                seconds = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                seconds = 0.0
        elif line and not line.startswith("#"):
            parts.append((line, seconds))
            seconds = 0.0
    return init, parts


def video_codec(codecs: str) -> str:
    """„avc1.640029,mp4a.40.2“ -> „avc1.640029“ (die Liste nennt Bild und Ton; Bild und Ton kommen getrennt)."""
    return next((c.strip() for c in codecs.split(",") if c.strip().startswith(("avc1", "avc3"))), "avc1.640029")


class SteamInfo:
    """Angaben, Neuigkeiten und Trailer je Steam-App-ID. get(url) liefert Bytes (Netz-Attrappe in Tests)."""

    def __init__(self, cache_dir: str, get: Callable[[str], bytes] = _get, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 shop: str = SHOP, api: str = API, loopback: bool = False):
        self.cache_dir = cache_dir
        self._get = get
        self._clock = clock
        self._mono = monotonic
        self._sleep = sleep
        self.shop = shop.rstrip("/")
        self.api = api.rstrip("/")
        self.loopback = loopback                # Prüfmodus mit Attrappe auf 127.0.0.1
        self._gate_lock = threading.Lock()
        self._last = -1e9
        self.paused_until = -1e9
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # ------------------------------------------------------------ Netz
    def _lock(self, key: str) -> threading.Lock:
        """Ein Abruf je Schlüssel: Zwei Rufe für dasselbe Spiel (Home und Spielseite) fragen Steam einmal."""
        with self._locks_lock:
            return self._locks.setdefault(key, threading.Lock())

    def _gate(self) -> None:
        """appdetails sparsam: höchstens ein Abruf je GAP_S, nach einer Drosselung PAUSE_S gar keiner."""
        with self._gate_lock:
            now = self._mono()
            if now < self.paused_until:
                raise StoreError(MSG_PAUSE)
            wait = self._last + GAP_S - now
            if wait > 0:
                self._sleep(wait)
            self._last = self._mono()

    def _throttled(self, error: BaseException) -> None:
        if not isinstance(error, urllib.error.HTTPError):
            return
        if error.code in (403, 429):
            self.paused_until = self._mono() + PAUSE_S
        try:
            error.close()                       # die Antwort hängt sonst offen (ResourceWarning)
        except Exception:  # noqa: BLE001
            pass

    def _json(self, url: str) -> Any:
        return json.loads(self._get(url).decode("utf-8"))

    def _cached(self, key: str, fetch: Callable[[], Any], ttl: float) -> Any:
        with self._lock(key):
            try:
                return cache.cached(self.cache_dir, key, fetch, ttl, clock=self._clock, unreachable=MSG_OFFLINE,
                                    on_error=self._throttled)
            except StoreError as e:
                # cache.cached macht aus jedem Fehler „nicht erreichbar“; in der Pause nach 403/429 stimmt das nicht
                if self._mono() < self.paused_until:
                    raise StoreError(MSG_PAUSE) from e
                raise

    def _raw(self, appid: int, lang: str) -> Any:
        url = "%s/api/appdetails?appids=%d&l=%s&filters=%s" % (self.shop, appid, lang_param(lang), FILTERS)

        def fetch() -> Any:
            self._gate()
            return self._json(url)
        return self._cached("app-%d-%s" % (appid, lang_param(lang)), fetch, INFO_TTL)

    # ------------------------------------------------------------ Angaben
    def details(self, appid: int, lang: str = "de") -> dict:
        """„Über das Spiel“. Wirft NotListed (Steam führt das Spiel nicht) oder StoreError (kein Netz)."""
        return parse_details(int(appid), self._raw(int(appid), lang), self.shop, self.loopback)

    # ------------------------------------------------------------ Neuigkeiten
    def news(self, appid: int, count: int = NEWS_COUNT) -> list[dict]:
        """Die letzten offiziellen Meldungen: [{"gid", "title", "date", "label", "patch", "text", "url"}]."""
        appid = int(appid)
        url = ("%s/ISteamNews/GetNewsForApp/v2/?appid=%d&count=%d&maxlength=%d&feeds=steam_community_announcements"
               "&format=json" % (self.api, appid, count, NEWS_CHARS))
        raw = self._cached("news-%d" % appid, lambda: self._json(url), NEWS_TTL)
        items = ((raw or {}).get("appnews") or {}).get("newsitems") if isinstance(raw, dict) else None
        out = []
        for it in items or []:
            if not isinstance(it, dict) or not str(it.get("gid") or "").isdigit():
                continue
            title = news_text(it.get("title"), 200)
            if not title:
                continue
            try:
                date = int(it.get("date") or 0)
            except (TypeError, ValueError):
                date = 0
            link = str(it.get("url") or "")
            if not allowed(link, NEWS_HOSTS, self.loopback):
                link = "%s/news/app/%d/view/%s" % (self.shop, appid, it["gid"])
            tags = it.get("tags") if isinstance(it.get("tags"), list) else []
            out.append({"gid": str(it["gid"]), "title": title, "date": date,
                        "label": _plain(it.get("feedlabel"), 80), "patch": "patchnotes" in tags,
                        "text": news_text(it.get("contents")), "url": link})
        return out[:count]

    def news_url(self, appid: int, gid: str) -> str | None:
        """Adresse einer Meldung aus dem Zwischenspeicher (nie eine, die die Seite schickt)."""
        try:
            items = self.news(appid)
        except StoreError:
            return None
        return next((n["url"] for n in items if n["gid"] == str(gid)), None)

    # ------------------------------------------------------------ Trailer
    def movies(self, appid: int, lang: str = "de") -> list[dict]:
        """Alle Trailer eines Spiels: [{"movie", "name", "poster", "highlight"}]. Wirft StoreError ohne Netz."""
        appid = int(appid)
        out = []
        for m in _movies(self._raw(appid, lang), appid, self.loopback):
            poster = str(m.get("thumbnail") or "")
            out.append({"movie": movie_id(m), "name": _plain(m.get("name"), 160), "highlight": bool(m.get("highlight")),
                        "poster": poster if allowed(poster, IMAGE_HOSTS, self.loopback) else ""})
        return out

    def trailer(self, appid: int, lang: str = "de", movie: str | None = None, max_height: int = TRAILER_MAX_H) -> dict | None:
        """{"movie", "name", "poster", "width", "height", "seconds", "bandwidth", "key",
        "video": {"init", "parts": [[Adresse, Sekunden] …], "seconds"}, "audio": {…} | None} oder None (kein Trailer).
        key: Name für eine abgelegte Fassung (App-ID, Hash der Hauptliste, Höhe). Wirft StoreError ohne Netz."""
        appid = int(appid)
        found_movie = pick_movie(self._raw(appid, lang), appid, self.loopback, movie)
        if found_movie is None:
            return None
        master = str(found_movie["hls_h264"])
        max_height = TRAILER_FULL_H if int(max_height) >= TRAILER_FULL_H else TRAILER_MAX_H

        def fetch() -> dict:
            text = self._get(master).decode("utf-8", "replace")
            var = pick_variant(parse_master(text), max_height)
            if var is None:
                return {"none": True}
            listing = urllib.parse.urljoin(master, var["name"])
            init, parts = parse_media(self._get(listing).decode("utf-8", "replace"))
            out = {"master": master, "listing": listing, "init": init, "parts": parts[:TRAILER_MAX_PARTS + 1],
                   "width": var["width"], "height": var["height"], "codecs": var["codecs"], "bandwidth": var["bandwidth"]}
            # Ton der Fassung (eigene Liste); fehlt er oder klemmt er, gibt es eben nur das Bild
            groups = parse_audio(text)
            sound = groups.get(var.get("audio") or "") or next(iter(groups.values()), "")
            if sound:
                try:
                    audio_listing = urllib.parse.urljoin(master, sound)
                    a_init, a_parts = parse_media(self._get(audio_listing).decode("utf-8", "replace"))
                    out.update(audio_listing=audio_listing, audio_init=a_init, audio_parts=a_parts[:TRAILER_MAX_PARTS + 1])
                except Exception as e:  # noqa: BLE001
                    if isinstance(e, urllib.error.HTTPError):
                        e.close()
            return out
        # Die Adresse der Hauptliste trägt einen Zeitstempel (?t=…): Neue Fassung, neuer Schlüssel. „trailer2“: Die
        # Listen von E123 waren auf 16 Stücke gekürzt und dürfen nicht weitergelten.
        digest = hashlib.sha1(master.encode("utf-8")).hexdigest()[:12]
        key = "trailer2-%d-%s-%d" % (appid, digest, max_height)
        found = self._cached(key, fetch, TRAILER_TTL)
        if not isinstance(found, dict) or found.get("none") or found.get("master") != master:
            return None
        video = self._track(found.get("listing"), found.get("init"), found.get("parts"))
        if video is None:
            return None
        audio = self._track(found.get("audio_listing"), found.get("audio_init"), found.get("audio_parts"))
        poster = str(found_movie.get("thumbnail") or "")
        try:
            bandwidth = max(0, int(found.get("bandwidth") or 0))
        except (TypeError, ValueError):
            bandwidth = 0
        return {"movie": movie_id(found_movie), "name": _plain(found_movie.get("name"), 160),
                "poster": poster if allowed(poster, IMAGE_HOSTS, self.loopback) else "",
                "width": int(found.get("width") or 0), "height": int(found.get("height") or 0),
                "seconds": video["seconds"], "bandwidth": bandwidth,
                "key": "%d-%s-%d" % (appid, digest, int(found.get("height") or 0)),
                "video": video, "audio": audio}

    def _track(self, listing: Any, init: Any, parts: Any) -> dict | None:
        """{"init": Adresse, "parts": [[Adresse, Sekunden] …], "seconds"} einer Medienliste; None, wenn eine Adresse
        nicht neben der Liste auf einem Rechner des Video-CDN läge oder die Liste leer bzw. zu lang ist."""
        if not listing or not init or not isinstance(parts, list) or not parts or len(parts) > TRAILER_MAX_PARTS:
            return None
        names = [(str(init), 0.0)]
        for p in parts:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                return None
            try:
                secs = float(p[1])
            except (TypeError, ValueError):
                return None
            if not 0.0 <= secs <= 60.0:
                return None
            names.append((str(p[0]), secs))
        out = []
        for name, secs in names:
            url = urllib.parse.urljoin(str(listing), name)
            if not _NAME.fullmatch(name) or ".." in name or not allowed(url, VIDEO_HOSTS, self.loopback):
                return None
            out.append([url, secs])
        return {"init": out[0][0], "parts": out[1:], "seconds": round(sum(s for _u, s in out[1:]), 2)}
