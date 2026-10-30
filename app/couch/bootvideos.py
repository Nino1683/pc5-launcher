"""Start- und Ruhemodus-Videos aus der Community-Sammlung steamdeckrepo.com (E130).

Die Seite zeigt die Sammlung am Bildschirm (screens/boot-videos.js); geladen wird nur das Video, das man dort wählt.

API (öffentlich, ohne Anmeldung; gesehen 2026-10-27):
  GET https://steamdeckrepo.com/api/posts?type=<boot_video|suspend_video>&sort=<downloads-desc|created_at-desc|…>&page=<n>
  -> {posts: [{id, slug, title, user {steam_name}, thumbnail, video (.webm), video_duration, video_preview (.mp4),
     likes, downloads, type, devices, created_at}], sortOptions, currentSort}; zwölf je Seite, X-RateLimit-Limit 60.
  Die Videos sind VP9 1280x800 (16:10, fürs Steam Deck), meist mit Vorbis-Ton; die Vorschau ist H.264.
Nutzungsbedingungen (steamdeckrepo.com/terms-of-service, gelesen 2026-10-27): Wer die Seite und ihre Dienste nutzt,
stimmt ihnen zu; alles „as is“ ohne Gewähr; Nutzung ab 13 Jahren; wer hochlädt, verantwortet die Rechte am Inhalt
und räumt dem Betreiber (waylaidwanderer) eine Lizenz ein; der Betreiber darf Inhalte entfernen und Nutzer sperren;
Recht von British Columbia. Zu Abrufen über die API, zu Grenzen oder zum Weitergeben steht dort nichts. Deshalb hier:
abgefragt wird nur, solange die Auswahl offen ist; Seiten eine Stunde zwischengespeichert, mindestens MIN_GAP_S
Abstand, nach 429/403 REST_S Pause; eigene Kennung im User-Agent; geladen wird nur das gewählte Video für diesen
Rechner, nichts wird weitergegeben; Titel und Ersteller bleiben in der Auswahl und in den Einstellungen sichtbar.

Ablage <data>/videos/: boot.webm und suspend.webm (je höchstens MAX_VIDEO, Dateikopf WebM/Matroska geprüft), daneben
boot.json und suspend.json (Kennung, Titel, Ersteller, Länge, Größe). Ersetzt wird per rename aus einer Teildatei im
selben Ordner; entfernt werden nur diese eigenen Dateien einzeln (lstat: echte Datei, kein Verweis), nie ein Ordner.
Die Adresse eines Videos kommt nie von der Seite: Die Seite nennt die Kennung, der Kern nimmt die Adresse aus seiner
eigenen, geprüften Liste (nur https zu steamdeckrepo.com bzw. cdn.steamdeckrepo.com, feste Pfade), Umleitungen nur
dorthin.

Prüfmodus: kein echtes Netz. Mit COUCH_BOOTVIDEO_FAKE=http://127.0.0.1:<port> gehen API und Videos an eine Attrappe
(tests/features/bootvideos/fake_repo.py); ohne sie antwortet list mit reason "test-mode".
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import VERSION, log, paths, system

_log = log.get("bootvideos")

API_BASE = "https://steamdeckrepo.com"
API_PATH = "/api/posts"
HOSTS = ("steamdeckrepo.com", "cdn.steamdeckrepo.com")
PAGE_HOST = "https://steamdeckrepo.com/post/%s/%s"
USER_AGENT = "PC5-Launcher/%s (+https://github.com/Nino1683/pc5-launcher)" % VERSION
FAKE_ENV = "COUCH_BOOTVIDEO_FAKE"

KINDS = {"boot": "boot_video", "suspend": "suspend_video"}
SORTS = {"popular": "downloads-desc", "new": "created_at-desc"}
# Längen: Startvideos wie die Sammlung selbst (bis 30 s); vor dem Ruhemodus nur kurze (E130: „wenige Sekunden“)
MAX_SECONDS = {"boot": 30.0, "suspend": 6.0}
MAX_PAGE = 10                   # zwölf je Seite: höchstens 120 Videos je Sortierung
MAX_JSON = 1 << 20              # eine Seite sind rund 10 KB
MAX_VIDEO = 60 << 20            # die Sammlung hat meist 1–10 MB
MAX_META = 64 << 10
CHUNK = 256 << 10
TIMEOUT_S = 20.0
CACHE_S = 3600.0                # Seiten der Sammlung: eine Stunde
MIN_GAP_S = 1.0                 # Abstand zwischen zwei Abrufen
REST_S = 600.0                  # nach 429/403 so lange keine Abrufe
MAX_KNOWN = 600                 # gemerkte Videos (Kennung -> geprüfte Angaben)
WEBM_MAGIC = b"\x1a\x45\xdf\xa3"   # EBML: WebM und Matroska

_ID = re.compile(r"^[A-Za-z0-9]{1,16}$")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_MEDIA = {
    "video": re.compile(r"^/videos/[A-Za-z0-9_-]{1,100}\.webm$"),
    "thumb": re.compile(r"^/thumbnails/[A-Za-z0-9_-]{1,100}\.(?:png|jpe?g|webp)$"),
    "preview": re.compile(r"^/previews/[A-Za-z0-9_-]{1,100}\.(?:mp4|webm)$"),
}
_PART = re.compile(r"^\.(?:boot|suspend)-[0-9a-f]{8}\.part$")
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")

MSG_TEST = "Im Prüfmodus nicht verfügbar."
MSG_NET = "steamdeckrepo.com ist gerade nicht erreichbar. Ist das Netz da?"
MSG_REST = "steamdeckrepo.com bittet um eine Pause. In ein paar Minuten geht es wieder."
MSG_UNKNOWN = "Dieses Video kennt die Liste nicht mehr. Lade die Auswahl neu."
MSG_BIG = "Das Video ist größer als 60 MB und wird nicht geladen."
MSG_NOT_WEBM = "Die geladene Datei ist kein WebM-Video. Sie wurde verworfen."
MSG_FOREIGN = "Die Sammlung nannte eine fremde Adresse; es wird nichts geladen."
MSG_STOPPED = "Das Laden wurde abgebrochen."
MSG_FOLDER = "Der Ordner für die Videos ist kein echter Ordner."
MSG_DISK = "Das Video ließ sich nicht speichern. Ist auf der Platte noch Platz?"


class VideoError(Exception):
    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


def fake_base() -> str | None:
    """Adresse einer Attrappe für Prüfläufe: nur im Prüfmodus und nur auf diesem Rechner."""
    from .library.artsources.base import host_of, is_loopback
    base = os.environ.get(FAKE_ENV) or ""
    if not paths.test_mode() or not base.startswith("http://") or not is_loopback(host_of(base)):
        return None
    return base.rstrip("/")


def clean_text(value: Any, limit: int) -> str:
    """Text aus der Sammlung: ohne Steuer- und Richtungszeichen, Leerraum zusammengezogen, gekürzt."""
    if not isinstance(value, str):
        return ""
    text = " ".join(_CTRL.sub(" ", value).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return float(value)


class Repo:
    """Abrufer der Sammlung und Ablage der gewählten Videos. Alles hier blockiert: nur im Arbeits-Thread rufen
    (meta und path lesen nur kleine eigene Dateien)."""

    def __init__(self, folder: str, cache: str, *, base: str | None = API_BASE, open_url: Callable | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        """base None: nur die Ablage (Lesen, Entfernen), kein Netz (Prüfmodus ohne Attrappe)."""
        self.folder = folder
        self.cache = cache
        self.offline = base is None
        self.base = (base or API_BASE).rstrip("/")
        self.fake = self.base != API_BASE
        parts = urllib.parse.urlsplit(self.base)
        self._scheme, self._netloc = parts.scheme, parts.netloc.lower()
        self._open_url = open_url or self._default_open
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()           # Abstand der Abrufe, Pause, gemerkte Videos
        self._last = 0.0
        self._rest_until = 0.0
        self._known: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------ Adressen
    def allowed(self, url: Any, what: str = "") -> bool:
        """Nur https ohne Anmeldedaten zu steamdeckrepo.com bzw. cdn.steamdeckrepo.com (Attrappe: genau ihre Adresse),
        mit what ("video", "thumb", "preview") zusätzlich nur der feste Pfad dieser Art."""
        try:
            p = urllib.parse.urlsplit(str(url or ""))
            port = p.port
        except ValueError:
            return False
        # Abfragen nur an die API selbst (what leer), nie an einer Datei
        if p.username or p.password or p.fragment or (what and p.query):
            return False
        if self.fake:
            if p.scheme != self._scheme or p.netloc.lower() != self._netloc:
                return False
        else:
            host = (p.hostname or "").lower().rstrip(".")
            if p.scheme != "https" or port not in (None, 443) or host not in HOSTS:
                return False
        pattern = _MEDIA.get(what)
        return pattern is None or bool(pattern.match(p.path))

    def _default_open(self, url: str, headers: dict) -> Any:
        repo = self

        class _Only(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                if not repo.allowed(newurl):
                    raise VideoError(MSG_FOREIGN, "redirect")
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)

        opener = urllib.request.build_opener(_Only)
        return opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_S)   # noqa: S310

    def _open(self, url: str, what: str = "") -> Any:
        if self.offline:
            raise VideoError(MSG_TEST, "test-mode")
        if not self.allowed(url, what):
            raise VideoError(MSG_FOREIGN, "redirect")
        with self._lock:
            now = self._clock()
            if now < self._rest_until:
                raise VideoError(MSG_REST, "rate-limit")
            wait = self._last + MIN_GAP_S - now
            self._last = max(now, self._last + MIN_GAP_S)
        if wait > 0:
            self._sleep(wait)
        try:
            resp = self._open_url(url, {"User-Agent": USER_AGENT, "Accept": "application/json, video/webm;q=0.9, */*;q=0.5"})
        except VideoError:
            raise
        except urllib.error.HTTPError as e:
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass
            if e.code in (403, 429):
                with self._lock:
                    self._rest_until = self._clock() + REST_S
                raise VideoError(MSG_REST, "rate-limit") from None
            raise VideoError("steamdeckrepo.com antwortete mit einem Fehler (HTTP %d)." % e.code, "http") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VideoError(MSG_NET, "offline") from e
        where = getattr(resp, "geturl", lambda: url)()
        if not self.allowed(where, what):
            resp.close()
            raise VideoError(MSG_FOREIGN, "redirect")
        return resp

    # ------------------------------------------------------------ Liste
    def clean_post(self, raw: Any, kind: str) -> dict | None:
        """Ein Eintrag der API in geprüfter Form, sonst None (falsche Art, fremde Adresse, zu lang …)."""
        if not isinstance(raw, dict) or raw.get("type") != KINDS[kind]:
            return None
        post_id, slug = raw.get("id"), raw.get("slug")
        if not isinstance(post_id, str) or not _ID.match(post_id):
            return None
        video, thumb, preview = raw.get("video"), raw.get("thumbnail"), raw.get("video_preview")
        if not self.allowed(video, "video") or not self.allowed(thumb, "thumb"):
            return None
        duration = _number(raw.get("video_duration"))
        if duration is None or not 0 < duration <= MAX_SECONDS[kind]:
            return None
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        count = lambda key: max(0, int(_number(raw.get(key)) or 0))    # noqa: E731
        created = raw.get("created_at")
        return {
            "id": post_id,
            "title": clean_text(raw.get("title"), 120) or post_id,
            "author": clean_text(user.get("steam_name"), 64),
            "duration": round(duration, 1),
            "downloads": count("downloads"),
            "likes": count("likes"),
            "thumb": thumb,
            "preview": preview if self.allowed(preview, "preview") else "",
            "created": created[:10] if isinstance(created, str) and re.match(r"^\d{4}-\d\d-\d\d", created) else "",
            "page": PAGE_HOST % (post_id, slug) if isinstance(slug, str) and _SLUG.match(slug) else "",
            "video": video,
        }

    def _cache_file(self, kind: str, sort: str, page: int) -> str:
        return os.path.join(self.cache, "%s-%s-%d.json" % (kind, sort, page))

    def _read_cache(self, path: str) -> dict | None:
        try:
            with open(path, "rb") as f:
                data = json.loads(f.read(MAX_JSON + 1))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("posts"), list) else None

    def _remember(self, kind: str, posts: list) -> None:
        with self._lock:
            for post in posts:
                self._known.pop((kind, post["id"]), None)
                self._known[(kind, post["id"])] = post
            while len(self._known) > MAX_KNOWN:
                self._known.pop(next(iter(self._known)))

    def list(self, kind: str, sort: str = "popular", page: int = 1) -> dict:
        """Eine Seite der Sammlung: {ok, posts (ohne Video-Adresse), page, more, stale?}. VideoError ohne Netz und
        ohne Zwischenspeicher."""
        if kind not in KINDS or sort not in SORTS or isinstance(page, bool) or not isinstance(page, int) \
                or not 1 <= page <= MAX_PAGE:
            raise VideoError("Diese Auswahl gibt es nicht.", "bad-request")
        path = self._cache_file(kind, sort, page)
        cached = self._read_cache(path)
        fresh = cached is not None and 0 <= self._clock() - float(cached.get("at") or 0) < CACHE_S
        stale = False
        if fresh:
            raw_posts, more = cached["posts"], bool(cached.get("more"))
        else:
            query = urllib.parse.urlencode({"type": KINDS[kind], "sort": SORTS[sort], "page": page})
            try:
                with self._open("%s%s?%s" % (self.base, API_PATH, query)) as resp:
                    body = resp.read(MAX_JSON + 1)
                if len(body) > MAX_JSON:
                    raise VideoError("Die Antwort von steamdeckrepo.com ist unerwartet groß.", "too-big")
                data = json.loads(body.decode("utf-8"))
                raw_posts = data.get("posts") if isinstance(data, dict) else None
                if not isinstance(raw_posts, list):
                    raise VideoError("Die Antwort von steamdeckrepo.com ist unlesbar.", "bad-answer")
            except (ValueError, UnicodeDecodeError) as e:
                if cached is None:
                    raise VideoError("Die Antwort von steamdeckrepo.com ist unlesbar.", "bad-answer") from e
                raw_posts, more, stale = cached["posts"], bool(cached.get("more")), True
            except VideoError:
                if cached is None:
                    raise
                raw_posts, more, stale = cached["posts"], bool(cached.get("more")), True
            except (OSError, http.client.HTTPException) as e:
                # Abbruch beim Lesen der Antwort (Zeitüberlauf, Verbindung weg): wie offline, alter Stand bleibt
                if cached is None:
                    raise VideoError(MSG_NET, "offline") from e
                raw_posts, more, stale = cached["posts"], bool(cached.get("more")), True
            else:
                more = len(raw_posts) >= 12 and page < MAX_PAGE
                try:
                    system.write_atomic(path, json.dumps({"at": self._clock(), "posts": raw_posts[:50], "more": more},
                                                         ensure_ascii=False))
                except OSError as e:
                    _log.warning("Zwischenspeicher der Videos nicht schreibbar: %s", e)
        posts = [p for p in (self.clean_post(r, kind) for r in raw_posts[:50]) if p]
        self._remember(kind, posts)
        out = {"ok": True, "kind": kind, "sort": sort, "page": page, "more": more,
               "posts": [{k: v for k, v in p.items() if k != "video"} for p in posts]}
        if stale:
            out["stale"] = True
        return out

    def find(self, kind: str, post_id: str) -> dict | None:
        """Geprüfte Angaben eines Videos aus den zuletzt gelisteten Seiten (Speicher, sonst Zwischenspeicher)."""
        with self._lock:
            hit = self._known.get((kind, post_id))
        if hit is not None:
            return hit
        try:
            names = sorted(os.listdir(self.cache))
        except OSError:
            return None
        for name in names:
            if not name.startswith(kind + "-") or not name.endswith(".json"):
                continue
            data = self._read_cache(os.path.join(self.cache, name)) or {}
            for raw in data.get("posts", [])[:50]:
                post = self.clean_post(raw, kind)
                if post and post["id"] == post_id:
                    self._remember(kind, [post])
                    return post
        return None

    # ------------------------------------------------------------ Ablage
    def path(self, kind: str) -> str:
        return os.path.join(self.folder, "%s.webm" % kind)

    def _meta_path(self, kind: str) -> str:
        return os.path.join(self.folder, "%s.json" % kind)

    def ensure_folder(self) -> str:
        """Eigener Ordner <data>/videos: anlegen, aber nie über einen Verweis hinweg benutzen."""
        try:
            os.makedirs(self.folder, mode=0o700, exist_ok=True)
        except OSError as e:
            raise VideoError(MSG_FOLDER, "bad-dir") from e
        if os.path.islink(self.folder) or not os.path.isdir(self.folder):
            raise VideoError(MSG_FOLDER, "bad-dir")
        return self.folder

    @staticmethod
    def _regular(path: str) -> os.stat_result | None:
        try:
            st = os.lstat(path)
        except OSError:
            return None
        return st if stat.S_ISREG(st.st_mode) else None

    @classmethod
    def _unlink_own(cls, path: str) -> bool:
        """Eine eigene Datei entfernen: nur eine echte Datei, nie ein Verweis oder Ordner."""
        if cls._regular(path) is None:
            return False
        try:
            os.unlink(path)
            return True
        except OSError as e:
            _log.warning("%s nicht entfernt: %s", path, e)
            return False

    def meta(self, kind: str) -> dict | None:
        """Angaben zum abgelegten Video dieser Art, None ohne (gültige) Datei."""
        if kind not in KINDS:
            return None
        st = self._regular(self.path(kind))
        if st is None or st.st_size <= 0 or os.path.islink(self.folder):
            return None
        data: dict = {}
        try:
            fd = os.open(self._meta_path(kind), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as f:
                raw = json.loads(f.read(MAX_META + 1))
            data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            data = {}
        return {"id": str(data.get("id") or ""), "title": clean_text(data.get("title"), 120),
                "author": clean_text(data.get("author"), 64),
                "duration": _number(data.get("duration")) or 0.0, "size": st.st_size, "mtime": int(st.st_mtime),
                "page": data.get("page") if isinstance(data.get("page"), str) and data["page"].startswith(
                    "https://steamdeckrepo.com/post/") else ""}

    def cleanup_parts(self) -> int:
        """Teildateien eines abgebrochenen Ladens (nur die eigenen Namen, einzeln)."""
        n = 0
        try:
            names = os.listdir(self.folder)
        except OSError:
            return 0
        for name in names:
            if _PART.match(name) and self._unlink_own(os.path.join(self.folder, name)):
                n += 1
        return n

    def download(self, kind: str, post_id: str, progress: Callable[[int, int], None] | None = None,
                 stop: threading.Event | None = None) -> dict:
        """Lädt das Video mit dieser Kennung nach <data>/videos/<kind>.webm (ersetzt ein vorhandenes erst, wenn das neue
        ganz da und geprüft ist) und schreibt die Angaben daneben. Rückgabe: meta(kind). VideoError bei jedem Fehler;
        eine verworfene Teildatei bleibt nicht liegen."""
        if kind not in KINDS or not isinstance(post_id, str) or not _ID.match(post_id):
            raise VideoError("Dieses Video gibt es nicht.", "bad-request")
        post = self.find(kind, post_id)
        if post is None:
            raise VideoError(MSG_UNKNOWN, "unknown")
        folder = self.ensure_folder()
        part = os.path.join(folder, ".%s-%s.part" % (kind, secrets.token_hex(4)))
        done = 0
        try:
            with self._open(post["video"], "video") as resp:
                headers = getattr(resp, "headers", None) or {}
                try:
                    total = int(headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
                if total > MAX_VIDEO:
                    raise VideoError(MSG_BIG, "too-big")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                with os.fdopen(os.open(part, flags, 0o600), "wb") as out:
                    if progress:
                        progress(0, total)
                    head = b""
                    while True:
                        if stop is not None and stop.is_set():
                            raise VideoError(MSG_STOPPED, "stopped")
                        try:
                            chunk = resp.read(CHUNK)
                        except (OSError, http.client.HTTPException) as e:
                            raise VideoError("Das Laden brach ab. Versuch es noch einmal.", "interrupted") from e
                        if not chunk:
                            break
                        if len(head) < 4:
                            head = (head + chunk)[:4]
                        done += len(chunk)
                        if done > MAX_VIDEO:
                            raise VideoError(MSG_BIG, "too-big")
                        try:
                            out.write(chunk)
                        except OSError as e:
                            raise VideoError(MSG_DISK, "disk") from e
                        if progress:
                            progress(done, total)
                    try:
                        out.flush()
                        os.fsync(out.fileno())
                    except OSError as e:
                        raise VideoError(MSG_DISK, "disk") from e
            if head != WEBM_MAGIC:
                raise VideoError(MSG_NOT_WEBM, "not-webm")
            if total and done != total:
                raise VideoError("Das Video kam nicht vollständig an. Versuch es noch einmal.", "interrupted")
            os.replace(part, self.path(kind))
        except BaseException:
            self._unlink_own(part)
            raise
        meta = {k: post[k] for k in ("id", "title", "author", "duration", "page")}
        meta["fetched"] = int(self._clock())
        try:
            system.write_atomic(self._meta_path(kind), json.dumps(meta, ensure_ascii=False))
        except OSError as e:
            _log.warning("Angaben zum Video nicht schreibbar: %s", e)
        _log.info("Video %s geladen: %s (%d Bytes)", kind, post_id, done)
        return self.meta(kind) or {}

    def remove(self, kind: str) -> bool:
        """Das abgelegte Video dieser Art und seine Angaben entfernen (nur diese zwei eigenen Dateien)."""
        if kind not in KINDS:
            return False
        gone = self._unlink_own(self.path(kind))
        self._unlink_own(self._meta_path(kind))
        return gone
