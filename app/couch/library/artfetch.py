"""Cover, Hintergründe, Logos und Kacheln aus dem Netz nachladen (Bauplan E34).

Eigenständig, ohne GLib: alles hier BLOCKIERT (Netz, Sekunden) und gehört in einen
Hintergrund-Thread. Die Quellen stehen unter ``artsources/`` (Steam, Microsoft-Store, GOG, Epic,
Battle.net, Nintendo, SteamGridDB), der Titelvergleich in ``artsources/match.py``.

Ablauf je Eintrag (``fetch_entry``):
  * Die Quellen kommen der Reihe nach dran, solange noch eine Bildart offen ist.
  * Je Bildart gewinnt die erste Quelle mit einem ausreichend großen Bild (Hintergrund: der
    gezeigte 16:9-Ausschnitt mindestens 1920 breit; Cover mindestens 600x900; Kachel mindestens
    512x512). Findet keine eines, bleibt das größte gefundene, aber nur wenn es größer ist als
    das, was schon lokal vorliegt.
  * Übernommen wird nur ein sicherer Treffer (streng, dann tolerant, siehe match.py). Ein
    falsches Bild ist schlimmer als ein Platzhalter.
  * Fällt eine Quelle aus (Zeitlimit, Fehler, kaputte Antwort), machen die anderen weiter; nach
    drei Störungen in Folge ruht die Quelle eine halbe Stunde, nach HTTP 429 eine Stunde.

Regeln: Zeitlimit 6 s je Verbindung, nur https und nur die Rechner der Quellen, Weiterleitungen
nur dorthin, Status und Content-Type werden geprüft, höchstens 15 MB je Bild, jedes Bild wird
vor dem Ablegen mit PIL geprüft, atomar gespeichert, eine Anfrage zur Zeit mit höflichem
Abstand je Dienst. Fehlschläge stehen je Eintrag und je Quelle in ``<cache_dir>/artfetch.json``
und werden frühestens nach 7 Tagen wiederholt (Netzstörungen nach 6 Stunden); ohne Netz wird
nichts vermerkt. Abschaltbar über die Einstellung ``library.fetch_art``.
Heruntergeladenes ist ausschließlich Bildmaterial: JPEG oder PNG, nie ausgeführt.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time

from .. import jsontext
from . import artsources
from .artsources import match
from .artsources.base import (IMAGE_TYPES, MAX_BYTES, MAX_JSON_BYTES, MIN_INTERVAL, TIMEOUT,  # noqa: F401
                              TOTAL_TIMEOUT, USER_AGENT, FetchError, Http, _NET_LOCK, host_of, https_url)
from .artsources.match import MIN_SCORE, norm, score, variants  # noqa: F401  (auch für ältere Aufrufer)
from .artsources.steam import ASSET_KEYS, CDN_URL, ITEMS_URL, LEGACY_NAMES, SEARCH_URL, SteamSource  # noqa: F401

CONFIG_KEY = "library.fetch_art"         # der eine Schalter (E22)
RETRY_MISS = 7 * 86400             # kein Treffer / Bild fehlt: frühestens nach 7 Tagen erneut
RETRY_TRANSIENT = 6 * 3600         # Netzstörung, Zeitüberlauf, Serverfehler
SERVICE_FAILS = 3                  # so viele Störungen in Folge, dann ruht die Quelle
SERVICE_BLOCK = 1800
SERVICE_BLOCK_429 = 3600
STATE_FILE = "artfetch.json"
CHOICES_FILE = "artchoices.json"
FETCH_PREFIX = "fetch-"
THUMB_PREFIX = "thumb-"
META_FILE = "fetch.json"
DEFAULT_KINDS = ("cover", "hero", "logo")
ALL_KINDS = ("cover", "hero", "logo", "tile")
MAX_PIXELS = 50_000_000
MAX_THUMB_BYTES = 6 * 1024 * 1024
THUMB_SIDE = 320
MAX_CANDIDATES = 6                 # je Quelle für „Treffer auswählen“
CANDIDATES_BUDGET_S = 40.0         # „Treffer auswählen“: danach nur noch antworten (RPC wartet 90 s)

MIN_COVER = (600, 900)
MIN_HERO_WIDTH = 1920              # Breite des gezeigten 16:9-Ausschnitts
MIN_TILE = 512
MIN_LOGO_WIDTH = 160

# E100: auch Karten-IDs von itch.io („<benutzer>/<slug>“)
_REF_FORM = re.compile(r"[\w.\-]{1,120}(?:/[\w.\-]{1,128})?")


def default_online() -> bool:
    """Gibt es eine Standardroute? Liest nur /proc (kein Unterprozess, kein Paket ins Netz)."""
    known = False
    try:
        with open("/proc/net/route", encoding="ascii", errors="replace") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                known = True
                if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 0x3 == 0x3:
                    return True
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/ipv6_route", encoding="ascii", errors="replace") as f:
            for line in f:
                parts = line.split()
                known = True
                if (len(parts) >= 10 and set(parts[0]) == {"0"} and parts[1] == "00"
                        and parts[9] != "lo" and int(parts[8], 16) & 0x3 == 0x3):
                    return True
    except (OSError, ValueError):
        pass
    # Lässt sich nichts lesen, entscheidet das Zeitlimit der Anfrage
    return not known


# ------------------------------------------------------------------ Größenregel

def hero_width(dims) -> int:
    """Breite des mittigen 16:9-Ausschnitts: so viel bleibt von einem Hintergrund am Fernseher."""
    return min(int(dims[0]), round(int(dims[1]) * 16 / 9))


def sufficient(kind: str, dims) -> bool:
    """Ist ein Bild dieser Größe gut genug, um nicht weiterzusuchen?"""
    if not dims or not dims[0] or not dims[1]:
        return False
    w, h = int(dims[0]), int(dims[1])
    if kind == "cover":
        return w >= MIN_COVER[0] and h >= MIN_COVER[1]
    if kind == "hero":
        return hero_width((w, h)) >= MIN_HERO_WIDTH
    if kind == "tile":
        return min(w, h) >= MIN_TILE
    if kind == "logo":
        return w >= MIN_LOGO_WIDTH
    return True


def measure(kind: str, dims) -> float:
    """Vergleichsmaß für „das größte gefundene“."""
    if not dims or not dims[0] or not dims[1]:
        return 0.0
    w, h = int(dims[0]), int(dims[1])
    return float(hero_width((w, h))) ** 2 if kind == "hero" else float(w * h)


def shape_problem(kind: str, dims, hero_min: float = 1.3) -> str:
    """Passt die Form zur Bildart? Leer = ja. (Ein Querformat ist kein Cover.) hero_min: kleinstes
    Seitenverhältnis eines Hintergrunds, je Quelle (hero_min_ratio; itch.io-Cover 1,26:1, E100)."""
    ratio = dims[0] / dims[1]
    if kind == "cover" and not 0.5 <= ratio <= 0.85:
        return "not-portrait"
    if kind == "tile" and not 0.9 <= ratio <= 1.1:
        return "not-square"
    if kind == "hero" and ratio < hero_min:
        return "not-landscape"
    return ""


class _Job:
    """Zwischenstand eines Abrufs: was schon liegt, was als Notlösung bereitsteht, was fehlte."""

    def __init__(self, kinds, bonus, have):
        self.kinds = [k for k in kinds if k in ALL_KINDS or k in ("header", "background")]
        self.bonus = [k for k in bonus if k not in self.kinds]
        self.have = dict(have or {})
        self.files = {}            # art -> pfad
        self.origin = {}           # art -> {"source", "ref", "name", "width", "height"}
        self.spare = {}            # art -> {"measure", "dims", "image", "source", "cand", "data", "fmt"}
        self.problems = {}         # art -> grund
        self.tried = {}            # quelle -> ergebnis
        self.first = None          # (quelle, kandidat, wert) des ersten Treffers
        self.failed = ""           # Grund, falls eine Quelle ausfiel (dann früher erneut versuchen)

    def open(self, source=None, with_bonus: bool = True) -> list:
        kinds = self.kinds + (self.bonus if with_bonus else [])
        return [k for k in kinds if k not in self.files and (source is None or k in source.kinds)]


class ArtFetcher:
    """Lädt Bilder aus den Quellen in einen Zielordner. Alle Methoden mit Netz blockieren.

    cache_dir     Ort für artfetch.json (Gedächtnis der Fehlschläge) und, ohne choices_path, die
                  gemerkte Trefferwahl
    config        Objekt mit get(key, default); ``library.fetch_art`` = False schaltet alles ab
    sources       None = nur Steam; "all" = alle Quellen in der vereinbarten Reihenfolge; oder
                  eine Liste von Namen bzw. fertigen Quellen, die mit ``self.http`` gebaut sind
    session       requests.Session-artiges Objekt (für Tests)
    online        Funktion -> bool; Vorgabe: Standardroute vorhanden
    sleep/clock/monotonic  einsteckbar, damit Tests nicht warten
    """

    def __init__(self, cache_dir: str, config=None, *, session=None, online=None, clock=time.time,
                 sleep=time.sleep, monotonic=time.monotonic, timeout: float = TIMEOUT,
                 total_timeout: float = TOTAL_TIMEOUT, max_bytes: int = MAX_BYTES,
                 min_interval: float = MIN_INTERVAL, image_interval: float | None = None,
                 search_url: str = SEARCH_URL, items_url: str = ITEMS_URL, cdn_url: str = CDN_URL,
                 language: str = "german", country: str = "DE", log=None,
                 user_agent: str | None = USER_AGENT, sources=None, source_options: dict | None = None,
                 choices_path: str | None = None):
        self.cache_dir = cache_dir
        self.config = config
        self._online = online or default_online
        self._clock = clock
        self.max_bytes = max_bytes
        self.http = Http(session, timeout=timeout, total_timeout=total_timeout, min_interval=min_interval,
                         image_interval=image_interval, user_agent=user_agent, sleep=sleep, monotonic=monotonic)
        self.steam = SteamSource(self.http, search_url, items_url, cdn_url, language, country)
        self.sources = self._make_sources(sources, source_options)
        self.choices_path = choices_path or os.path.join(cache_dir, CHOICES_FILE)
        self._log = log or (lambda text: None)
        self._state = None
        self._state_lock = threading.RLock()
        self._choices = None
        self._listed = {}                       # eintrag -> {(quelle, ref): kandidat} der letzten Liste

    def _make_sources(self, sources, options) -> list:
        if sources is None:
            return [self.steam]
        if sources == "all":
            sources = artsources.ORDER
        out = []
        for item in sources:
            if isinstance(item, str):
                if item == "steam":
                    out.append(self.steam)
                elif item in artsources.CLASSES:
                    out.extend(artsources.build(self.http, self.config, (item,), overrides=options))
            elif item is not None:
                self.http.allow(getattr(item, "hosts", ()))
                out.append(item)
        return out

    # Ältere Aufrufer und Tests lesen diese Werte am Abrufer
    @property
    def requests_made(self) -> int:
        return self.http.requests_made

    @property
    def timeout(self) -> float:
        return self.http.timeout

    @property
    def search_url(self) -> str:
        return self.steam.search_url

    @property
    def items_url(self) -> str:
        return self.steam.items_url

    @property
    def cdn_url(self) -> str:
        return self.steam.cdn_url

    def cancel(self) -> None:
        """Das Home endet: laufende und künftige Anfragen brechen ab (nichts wird vermerkt)."""
        self.http.cancelled.set()

    # -------------------------------------------------------------- Schalter und Gedächtnis

    def enabled(self) -> bool:
        if self.config is None:
            return True
        try:
            return bool(self.config.get(CONFIG_KEY, True))
        except Exception:                       # eine kaputte Einstellung darf den Abruf nicht sprengen
            return True

    def is_online(self) -> bool:
        try:
            return bool(self._online())
        except Exception:
            return True

    @property
    def state_path(self) -> str:
        return os.path.join(self.cache_dir, STATE_FILE)

    def _load(self) -> dict:
        with self._state_lock:
            if self._state is None:
                try:
                    with open(self.state_path, encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
                        raise ValueError("unerwarteter Aufbau")
                except (OSError, ValueError):
                    data = {"version": 2, "entries": {}}
                if not isinstance(data.get("services"), dict):
                    data["services"] = {}
                self._state = data
            return self._state

    def _save(self) -> None:
        with self._state_lock:
            self._write_json(self.state_path, self._state, sort_keys=True)

    @staticmethod
    def _write_json(path: str, data, sort_keys: bool = False) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps(data, indent=1, sort_keys=sort_keys))
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def status(self, key: str) -> dict | None:
        """Letzter Vermerk zu einem Eintrag: {"status", "reason", "time", "retry_after",
        "sources": {quelle: {"status", "reason", "retry_after"}}, …} oder None."""
        rec = self._load()["entries"].get(key)
        return json.loads(json.dumps(rec)) if rec else None

    def blocked(self, key: str, system=None, kinds=None) -> float:
        """Sekunden, bis für diesen Eintrag wieder ein Versuch erlaubt ist (0 = jetzt). Hatte keine
        Quelle etwas, gilt das nicht für eine zuständige Quelle, die den Eintrag noch nie gesehen hat
        (neu hinzugekommen wie Battle.net, E97) und gerade nicht ruht: Sie darf gleich ran.
        kinds: die gesuchten Bildarten. Eine Quelle, die keine davon führt, zählt dann nicht (E99:
        fetch_entry fragt sie nie, sonst liefe bei jedem Start ein Schein-Abruf)."""
        rec = self._load()["entries"].get(key)
        if not rec:
            return 0.0
        wait = max(0.0, float(rec.get("retry_after", 0)) - self._clock())
        asked = rec.get("sources")
        wanted = set(kinds) if kinds else None
        if wait > 0 and rec.get("status") == "miss" and isinstance(asked, dict) and any(
                src.name not in asked and self.service_blocked(src.name) <= 0
                and (wanted is None or wanted & set(src.kinds)) for src in self.sources_for(system)):
            return 0.0
        return wait

    def source_blocked(self, key: str, source: str) -> float:
        """Sekunden, bis diese Quelle für diesen Eintrag wieder gefragt wird."""
        rec = (self._load()["entries"].get(key) or {}).get("sources") or {}
        return max(0.0, float((rec.get(source) or {}).get("retry_after", 0)) - self._clock())

    def record(self, key: str, status: str, reason: str = "", transient: bool = False, wait: float | None = None,
               **extra) -> None:
        """wait: höchstens so viele Sekunden bis zum nächsten Versuch (Ende der Ruhezeit einer Quelle)."""
        now = self._clock()
        retry = RETRY_TRANSIENT if transient else RETRY_MISS
        if wait is not None:
            retry = min(retry, max(0.0, float(wait)))
        rec = {"status": status, "reason": reason, "time": int(now), "retry_after": int(now + retry)}
        rec.update({k: v for k, v in extra.items() if v is not None})
        with self._state_lock:
            old = self._load()["entries"].get(key) or {}
            if "sources" not in rec and isinstance(old.get("sources"), dict):
                rec["sources"] = old["sources"]
            self._load()["entries"][key] = rec
            self._save()

    def _source_notes(self, key: str, tried: dict) -> dict:
        """Vermerke je Quelle fortschreiben: Wer nichts hatte, wird 7 Tage nicht mehr gefragt."""
        now = self._clock()
        notes = dict((self._load()["entries"].get(key) or {}).get("sources") or {})
        for name, result in tried.items():
            state, _sep, reason = result.partition(":")
            if state in ("backoff", "blocked", "skip"):
                continue
            wait = {"ok": 0, "error": RETRY_TRANSIENT}.get(state, RETRY_MISS)
            notes[name] = {"status": state, "reason": reason, "retry_after": int(now + wait)}
        return notes

    def forget(self, key: str) -> None:
        """Vermerk löschen (z. B. nach „Bilder neu laden“ oder wenn sich der Titel geändert hat)."""
        with self._state_lock:
            if self._load()["entries"].pop(key, None) is not None:
                self._save()

    # -------------------------------------------------------------- Ruhezeit einer Quelle

    def service_blocked(self, name: str) -> float:
        rec = self._load()["services"].get(name) or {}
        return max(0.0, float(rec.get("until", 0)) - self._clock())

    def _service_ok(self, name: str) -> None:
        with self._state_lock:
            if self._load()["services"].pop(name, None) is not None:
                self._save()

    def lift_service(self, name: str, reason: str | None = None) -> bool:
        """Pause einer Quelle aufheben, mit reason nur bei genau diesem Grund (etwa "key-rejected"
        nach einem neuen Schlüssel, E99). True, wenn eine Pause aufgehoben wurde. Kein Netz."""
        with self._state_lock:
            rec = self._load()["services"].get(name)
            if rec is None or (reason is not None and rec.get("reason") != reason):
                return False
            self._service_ok(name)
            return True

    def _service_failed(self, name: str, exc: FetchError) -> None:
        # busy: eine andere Anfrage hielt die Netzsperre zu lange, dieser Dienst wurde gar nicht gefragt (E99)
        if exc.reason in ("cancelled", "test-mode", "no-key", "busy"):
            return
        now = self._clock()
        with self._state_lock:
            rec = dict(self._load()["services"].get(name) or {})
            rec["fails"] = int(rec.get("fails", 0)) + 1
            rec["reason"] = exc.reason
            if exc.reason == "http-429":
                try:
                    asked = min(6 * 3600, max(60, int(exc.detail)))
                except (TypeError, ValueError):
                    asked = SERVICE_BLOCK_429
                rec.update(until=int(now + asked), fails=0)
            elif exc.reason == "key-rejected":
                rec.update(until=int(now + SERVICE_BLOCK_429), fails=0)
            elif rec["fails"] >= SERVICE_FAILS:
                rec.update(until=int(now + SERVICE_BLOCK), fails=0)
            self._load()["services"][name] = rec
            self._save()
        self._log("artfetch: Quelle %s gestört (%s)" % (name, exc.reason))

    # -------------------------------------------------------------- gemerkte Trefferwahl

    def _load_choices(self) -> dict:
        with self._state_lock:
            if self._choices is None:
                try:
                    with open(self.choices_path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._choices = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    self._choices = {}
            return self._choices

    def choice(self, key: str) -> dict | None:
        """Der von Hand gewählte Treffer eines Eintrags: {"source", "ref", "name", "images", "time"}."""
        rec = self._load_choices().get(key)
        return dict(rec) if isinstance(rec, dict) and rec.get("source") and rec.get("ref") else None

    def set_choice(self, key: str, candidate: dict, images: dict | None = None) -> None:
        rec = {"source": str(candidate.get("source") or ""), "ref": str(candidate.get("ref") or ""),
               "name": str(candidate.get("name") or "")[:200], "time": int(self._clock())}
        if images:
            rec["images"] = {k: [{"url": im["url"], "width": im.get("width"), "height": im.get("height")}
                                 for im in v[:3]] for k, v in images.items()}
        with self._state_lock:
            self._load_choices()[key] = rec
            self._write_json(self.choices_path, self._choices, sort_keys=True)

    def forget_choice(self, key: str) -> bool:
        """Zurück zur automatischen Wahl. True = es gab eine gemerkte."""
        with self._state_lock:
            if self._load_choices().pop(key, None) is None:
                return False
            self._write_json(self.choices_path, self._choices, sort_keys=True)
            return True

    # -------------------------------------------------------------- Quellen

    def sources_for(self, system=None) -> list:
        """Die Quellen, die für diese Art Eintrag zuständig und gerade benutzbar sind."""
        out = []
        for src in self.sources:
            wide = "*" in src.systems
            fits = (system in src.systems or wide) if system in ("switch", "wiiu") else (not src.systems or wide)
            searchable = getattr(src, "searchable", None)
            if fits and src.available() and (searchable is None or searchable()):
                out.append(src)
        return out

    def source(self, name: str):
        return next((s for s in self.sources if s.name == name), None)

    # -------------------------------------------------------------- Bilder laden und prüfen

    def _load_image(self, url: str, service: str, kind: str = "", max_bytes: int | None = None) -> tuple:
        """Ein Bild holen und prüfen. Rückgabe (bytes, "JPEG"|"PNG", (breite, höhe)). Blockiert."""
        from PIL import Image
        body, _ctype = self.http.get(url, accept=IMAGE_TYPES, max_bytes=max_bytes or self.max_bytes,
                                     service=service, image=True)
        try:
            with Image.open(io.BytesIO(body)) as im:
                fmt, (w, h), mode, info = im.format, im.size, im.mode, dict(im.info)
                im.verify()
        except Exception:                       # PIL wirft je nach Schaden sehr verschiedene Ausnahmen
            raise FetchError("not-an-image") from None
        if fmt not in ("JPEG", "PNG") or not (16 <= w <= 16384 and 16 <= h <= 16384) or w * h > MAX_PIXELS:
            raise FetchError("not-an-image", detail="%s %dx%d" % (fmt, w, h))
        if kind:
            src = self.source(service)
            problem = shape_problem(kind, (w, h), getattr(src, "hero_min_ratio", 1.3))
            if problem:
                raise FetchError(problem, detail="%dx%d" % (w, h))
        if kind == "logo":
            # Ein deckendes Bild läge als Kasten über dem Hintergrund: nur echte Freisteller
            clear = mode in ("RGBA", "LA", "PA") or "transparency" in info
            if clear:
                try:
                    with Image.open(io.BytesIO(body)) as im:
                        low, _high = im.convert("RGBA").getchannel("A").getextrema()
                    clear = low < 250
                except Exception:
                    clear = False
            if not clear:
                raise FetchError("not-a-logo")
        return body, fmt, (w, h)

    @staticmethod
    def _store(body: bytes, fmt: str, dest_dir: str, stem: str) -> str:
        ext = ".png" if fmt == "PNG" else ".jpg"
        os.makedirs(dest_dir, exist_ok=True)
        final = os.path.join(dest_dir, stem + ext)
        tmp = os.path.join(dest_dir, ".tmp-%d-%d-%s" % (os.getpid(), threading.get_ident(), stem))
        try:
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, final)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        twin = os.path.join(dest_dir, stem + (".jpg" if ext == ".png" else ".png"))
        if os.path.lexists(twin):               # alte Fassung mit anderer Endung: genau diese eine Datei
            try:
                os.unlink(twin)
            except OSError:
                pass
        return final

    # -------------------------------------------------------------- Abruf über App-ID oder Titel (Prüfungen)

    def fetch_app(self, key: str, appid: int, dest_dir: str, kinds=DEFAULT_KINDS,
                  extra_names: dict | None = None, force: bool = False) -> dict:
        """Steam-Spiel über die App-ID, nur aus Steam. Blockiert (mehrere Anfragen mit Abstand).

        extra_names: zusätzliche Dateinamen je Art relativ zu steam/apps/<appid>/ (z. B. der
        2x-Logoname aus appinfo). Rückgabe wie ``fetch_entry``."""
        query = {"title": "", "terms": [], "appid": int(appid), "extra_names": extra_names}
        return self.fetch_entry(key, query, dest_dir, kinds, force=force, only=[self.steam], bonus=())

    def fetch_title(self, key: str, title: str, dest_dir: str, kinds=DEFAULT_KINDS, force: bool = False) -> dict:
        """Spiel über die Titelsuche in allen Quellen dieses Abrufers. Blockiert."""
        return self.fetch_entry(key, {"title": title, "terms": [title]}, dest_dir, kinds, force=force, bonus=())

    # -------------------------------------------------------------- Trefferwahl

    def _settle(self, src, terms, cands, system) -> tuple:
        """Sicheren Treffer wählen; wo die Quelle erst nachsehen muss, was ein Spiel ist, fragt
        sie nur für die Kandidaten, an denen die Entscheidung hängt."""
        info = {"score": 0.0, "level": None, "reason": "kein sicherer Treffer"}
        for _round in range(4):
            hit, info = match.pick(terms, cands, system)
            pending = info.get("unverified") or []
            if hit is not None or not pending:
                return hit, info
            src.details(pending)
            for c in pending:
                if c.get("game") is None:
                    c["game"] = True            # die Quelle kennt keine Arten: der Name entscheidet
        return None, info

    def _find(self, src, query: dict) -> tuple:
        """(kandidat | None, info) für eine Quelle. Blockiert; wirft FetchError."""
        if src is self.steam and query.get("appid"):
            cand = self.steam.by_appid(int(query["appid"]), query.get("extra_names"))
            return cand, {"score": 1.0 if cand else 0.0, "level": "id" if cand else None,
                          "reason": "" if cand else "kein sicherer Treffer"}
        terms = [t for t in query.get("terms") or [] if t][:3]
        system = query.get("system")
        cands, seen, asked = [], set(), set()
        hit, info = None, {"score": 0.0, "level": None, "reason": "kein sicherer Treffer"}
        for term in terms:
            text = match.query_text(term)
            if not text or text.lower() in asked:
                continue
            asked.add(text.lower())
            for c in src.search(text, {"system": system, "terms": terms}):
                if c.get("ref") not in seen:
                    seen.add(c.get("ref"))
                    cands.append(c)
            hit, info = self._settle(src, terms, cands, system)
            if hit is not None and info.get("level") in match.STRICT_LEVELS:
                break                           # sicherer geht es nicht: weitere Suchbegriffe sparen
        return hit, info

    # -------------------------------------------------------------- Abruf je Eintrag

    def _busy(self, job: _Job) -> dict:
        """Die Netzsperre war zu lange belegt (FetchError busy, E99): Keine Quelle ist schuld, nichts wird
        vermerkt; wie ruhende Dienste („Versuche es später noch einmal“), nicht wie „kein Treffer“."""
        return {**self._blank(), "status": "resting", "reason": "andere Anfrage hängt", "retry_s": 0,
                "files": job.files, "ok": bool(job.files), "tried": dict(job.tried)}

    @staticmethod
    def _blank() -> dict:
        return {"ok": False, "status": "", "reason": "", "files": {}, "appid": None, "name": "", "score": 0.0,
                "source": "", "sources": {}, "tried": {}}

    def _gate(self, key: str, force: bool, system=None, kinds=None) -> dict | None:
        out = self._blank()
        if not self.enabled():
            return {**out, "status": "disabled", "reason": "library.fetch_art ist aus"}
        if not self.is_online():
            return {**out, "status": "offline", "reason": "kein Netz"}
        if not force and self.blocked(key, system, kinds) > 0:
            rec = self.status(key) or {}
            return {**out, "status": "backoff", "reason": rec.get("reason", "")}
        return None

    def fetch_entry(self, key: str, query: dict, dest_dir: str, kinds=DEFAULT_KINDS, *, have: dict | None = None,
                    force: bool = False, only=None, bonus=("tile",)) -> dict:
        """Bilder für einen Eintrag aus den Quellen holen. BLOCKIERT (Sekunden bis eine Minute).

        key       Bibliotheks-ID (Schlüssel im Gedächtnis)
        query     {"title", "terms": [suchbegriffe], "system": None|"switch"|"wiiu",
                   "appid": Steam-App-ID oder None, "extra_names": Dateinamen aus appinfo}
        kinds     gesuchte Bildarten aus cover, hero, logo, tile
        have      {art: (breite, höhe)} dessen, was lokal schon vorliegt: Ein kleineres Bild aus
                  dem Netz ersetzt es nicht
        bonus     Bildarten, die mitgenommen werden, wenn eine ohnehin gefragte Quelle sie in
                  guter Größe hat (die quadratische Kachel)
        Rückgabe  {"ok": bool, "status": "ok|partial|miss|error|disabled|offline|backoff|resting|skip",
                   "reason": str, "files": {art: pfad}, "appid": int|None, "name": str,
                   "score": float, "source": str, "sources": {art: quelle}, "tried": {quelle: ergebnis}}
                  ok = mindestens eine Datei wurde neu gespeichert. resting (mit "retry_s"): Jede
                  zuständige Quelle ruht nach Störungen, keine wurde gefragt, nichts vermerkt (E99).
        """
        gate = self._gate(key, force, query.get("system"), kinds)
        if gate:
            return gate
        pinned = self.choice(key) if only is None else None
        if pinned:
            src = self.source(str(pinned.get("source")))
            if src is not None and src.available():
                return self._fetch_choice(key, pinned, dest_dir)
            # die Quelle der gemerkten Wahl gibt es nicht mehr (Schlüssel entfernt): selbst suchen
        sources = list(only) if only is not None else self.sources_for(query.get("system"))
        if not sources:
            return {**self._blank(), "status": "skip", "reason": "keine Quelle für diese Art Eintrag"}
        job = _Job(kinds, bonus, have)
        for src in sources:
            if not job.open(src, with_bonus=False):
                continue
            if self.service_blocked(src.name) > 0:
                job.tried[src.name] = "blocked"
                continue
            if not force and self.source_blocked(key, src.name) > 0:
                job.tried[src.name] = "backoff"
                continue
            try:
                cand, info = self._find(src, query)
                if cand is None:
                    job.tried[src.name] = "miss:" + str(info.get("reason") or "kein sicherer Treffer")
                    self._service_ok(src.name)
                    continue
                if job.first is None:
                    job.first = (src, cand, info.get("score", 0.0))
                took = self._take(job, src, cand, dest_dir)
                job.tried[src.name] = "ok" if took else "nothing"
                self._service_ok(src.name)
            except FetchError as exc:
                if exc.reason in ("cancelled", "test-mode"):
                    return {**self._blank(), "status": "offline", "reason": exc.reason, "files": job.files,
                            "ok": bool(job.files)}
                if exc.reason == "busy":
                    return self._busy(job)
                if exc.reason in ("connection", "timeout") and not self.is_online():
                    # das Netz ist weg: nichts vermerken, später von vorn
                    return {**self._blank(), "status": "offline", "reason": "kein Netz", "files": job.files,
                            "ok": bool(job.files)}
                # Was bis hierher durchschlägt, ist ein Ausfall der Quelle (Störung, kaputte oder
                # abgelehnte Antwort), kein „gibt es nicht“: die anderen Quellen machen weiter
                self._log("artfetch %s: %s: %s" % (key, src.name, exc))
                job.tried[src.name] = "error:" + exc.reason
                job.failed = job.failed or exc.reason
                self._service_failed(src.name, exc)
        if job.tried and all(v in ("blocked", "backoff") for v in job.tried.values()):
            # Keine Quelle wurde wirklich gefragt (E99): Das ist kein „kein Treffer“ (sonst 7 Tage
            # Sperre und „Spiele ohne Treffer“ im Sammellauf), also nichts vermerken
            resting = [self.service_blocked(n) for n, v in job.tried.items() if v == "blocked"]
            if resting:
                return {**self._blank(), "status": "resting", "reason": "die Quellen ruhen nach Störungen",
                        "retry_s": int(min(resting)), "tried": dict(job.tried)}
            return {**self._blank(), "status": "backoff", "reason": "alle Quellen vor Kurzem gefragt",
                    "tried": dict(job.tried)}
        self._use_spares(job, dest_dir)
        return self._finish(key, job, dest_dir)

    def _take(self, job: _Job, src, cand: dict, dest_dir: str) -> bool:
        """Aus einem Treffer alles Offene holen, was er in guter Größe hat. True = etwas gespeichert."""
        wanted = job.open(src)
        images = src.images(cand, wanted)
        took = False
        for kind in wanted:
            alts = [im for im in images.get(kind) or [] if isinstance(im, dict) and im.get("url")]
            if not alts:
                if kind in job.kinds:
                    job.problems.setdefault(kind, "nicht angeboten")
                continue
            for alt in alts[:3]:
                dims = (alt.get("width"), alt.get("height"))
                if all(dims) and not sufficient(kind, dims):
                    # zu klein laut Angabe: erst laden, wenn sich nichts Besseres findet
                    self._offer(job, kind, alt, dims, src, cand)
                    break
                try:
                    body, fmt, real = self._load_image(alt["url"], src.name, kind)
                except FetchError as exc:
                    if exc.transient:
                        raise
                    if kind in job.kinds:
                        job.problems[kind] = exc.reason
                    if exc.reason in ("http-404", "http-403", "http-410"):
                        continue                # diese Fassung gibt es nicht: die nächste versuchen
                    break                       # falscher Typ, zu groß, kaputt: keine zweite Fassung holen
                if sufficient(kind, real):
                    self._commit(job, kind, body, fmt, real, src, cand, dest_dir)
                    took = True
                else:
                    self._offer(job, kind, alt, real, src, cand, body, fmt)
                break
        return took

    @staticmethod
    def _offer(job: _Job, kind: str, alt: dict, dims, src, cand, body=None, fmt=None) -> None:
        if kind in job.bonus:
            return                              # Zugaben nur in guter Größe
        size = measure(kind, dims)
        if kind not in job.spare or size > job.spare[kind]["measure"]:
            job.spare[kind] = {"measure": size, "dims": dims, "image": alt, "source": src, "cand": cand,
                               "body": body, "fmt": fmt}

    def _commit(self, job: _Job, kind: str, body: bytes, fmt: str, dims, src, cand, dest_dir: str) -> None:
        job.files[kind] = self._store(body, fmt, dest_dir, FETCH_PREFIX + kind)
        job.origin[kind] = {"source": src.name, "ref": cand.get("ref"), "name": cand.get("name"),
                            "width": dims[0], "height": dims[1]}
        job.problems.pop(kind, None)

    def _use_spares(self, job: _Job, dest_dir: str) -> None:
        """Keine Quelle hatte ein ausreichend großes Bild: das größte gefundene nehmen, sofern es
        größer ist als das, was schon vorliegt."""
        for kind, sp in job.spare.items():
            if kind in job.files:
                continue
            if job.have.get(kind) and sp["measure"] <= measure(kind, job.have[kind]) * 1.15:
                job.problems[kind] = "nicht größer als vorhanden"
                continue
            try:
                body, fmt, dims = sp["body"], sp["fmt"], sp["dims"]
                if body is None:
                    body, fmt, dims = self._load_image(sp["image"]["url"], sp["source"].name, kind)
                self._commit(job, kind, body, fmt, dims, sp["source"], sp["cand"], dest_dir)
                state = job.tried.get(sp["source"].name, "")
                if state in ("nothing", ""):
                    job.tried[sp["source"].name] = "ok"
            except FetchError as exc:
                job.problems[kind] = exc.reason
                job.failed = job.failed or (exc.reason if exc.transient else "")

    def _finish(self, key: str, job: _Job, dest_dir: str, picked: bool = False) -> dict:
        out = self._blank()
        if job.first:
            src, cand, value = job.first
            out.update(name=str(cand.get("name") or ""), score=round(float(value or 0.0), 3), source=src.name)
            if src.name == "steam":
                try:
                    out["appid"] = int(cand.get("ref"))
                except (TypeError, ValueError):
                    pass
        out["tried"] = dict(job.tried)
        out["sources"] = {k: v["source"] for k, v in job.origin.items()}
        files = job.files
        self._write_meta(dest_dir, out, job, picked)
        notes = self._source_notes(key, job.tried)
        # Ruhte eine Quelle, kommt der Eintrag nach ihrer Ruhezeit wieder dran, nicht erst in 7 Tagen
        rest = [self.service_blocked(n) for n, v in job.tried.items() if v == "blocked"]
        wait = min(rest) if rest else None
        if not files and job.first is None and not job.failed:
            self.record(key, "miss", "kein sicherer Treffer", wait=wait, sources=notes)
            return {**out, "status": "miss", "reason": "kein sicherer Treffer"}
        main = [k for k in job.kinds if k in ("cover", "hero")]
        if files and all(k in files for k in main):
            status = "ok"
        elif files:
            status = "partial"
        else:
            status = "error" if job.failed else "miss"
        reason = job.failed if status == "error" else ", ".join(
            "%s: %s" % kv for kv in sorted(job.problems.items()) if kv[0] not in files)
        self.record(key, status, reason, transient=bool(job.failed) and status != "ok", wait=wait, appid=out["appid"],
                    name=out["name"], score=out["score"], kinds=sorted(files), sources=notes,
                    source=out["source"] or None)
        return {**out, "ok": bool(files), "status": status, "reason": reason, "files": dict(files)}

    def _write_meta(self, dest_dir: str, out: dict, job: _Job, picked: bool = False) -> None:
        """Merkzettel neben den Bildern: woher sie stammen (für „Bild ändern“ und den Bericht)."""
        if not job.files:
            return
        path = os.path.join(dest_dir, META_FILE)
        old = {}
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            pass
        origin = dict(old.get("origin") or {}) if isinstance(old, dict) else {}
        origin.update(job.origin)
        names = dict(old.get("files") or {}) if isinstance(old, dict) else {}
        names.update({k: os.path.basename(v) for k, v in job.files.items()})
        meta = {"appid": out["appid"], "name": out["name"], "score": out["score"], "source": out["source"],
                "time": int(self._clock()), "picked": bool(picked), "files": names, "origin": origin}
        try:
            self._write_json(path, meta)
        except OSError:
            pass

    # -------------------------------------------------------------- Treffer auswählen

    def candidates(self, key: str, query: dict, text: str | None = None, thumb_dir: str | None = None,
                   budget: float | None = None) -> list:
        """Kandidaten aller zuständigen Quellen für „Treffer auswählen“. BLOCKIERT (Netz).

        text ersetzt die Suchbegriffe des Eintrags (eigene Suche). Rückgabe, beste zuerst je
        Quelle: [{"source", "label", "ref", "name", "thumb": pfad | None, "kinds": [..],
        "score": float, "safe": bool}]. thumb ist eine Datei in thumb_dir (kleines JPEG).
        Fällt eine Quelle aus, fehlen nur ihre Kandidaten. Nach budget Sekunden (Vorgabe
        CANDIDATES_BUDGET_S) keine weitere Quelle und keine Vorschaubilder mehr: Bis zu fünf
        Quellen mit Vorschau je Treffer brauchten am Fernseher 154 s, die Seite wartet 90 s."""
        if not self.enabled() or not self.is_online():
            return []
        terms = [text] if text and text.strip() else [t for t in query.get("terms") or [] if t][:2]
        system = query.get("system")
        out, listed = [], {}
        deadline = time.monotonic() + (CANDIDATES_BUDGET_S if budget is None else budget)
        for src in self.sources_for(system):
            if self.service_blocked(src.name) > 0:
                continue
            if time.monotonic() > deadline:
                self._log("artfetch %s: Zeit für „Treffer auswählen“ um, ohne %s" % (key, src.name))
                continue
            try:
                found = self._list(src, terms, system)
                self._service_ok(src.name)
            except FetchError as exc:
                if exc.reason in ("cancelled", "test-mode", "busy"):
                    break                           # busy: die Sperre gilt für alle Quellen, Warten lohnt nicht
                self._log("artfetch %s: %s: %s" % (key, src.name, exc))
                self._service_failed(src.name, exc)
                continue
            for cand, value, safe in found:
                thumb = None
                if thumb_dir and time.monotonic() <= deadline:
                    thumb = self._thumb(src, cand, thumb_dir)
                listed[(src.name, str(cand.get("ref")))] = cand
                out.append({"source": src.name, "label": src.label, "ref": str(cand.get("ref")),
                            "name": str(cand.get("name") or ""), "thumb": thumb,
                            "kinds": [k for k in ALL_KINDS if k in cand.get("offers", src.kinds)],
                            "score": round(float(value), 3), "safe": bool(safe)})
        self._listed = {key: listed}
        return out

    def _list(self, src, terms, system) -> list:
        """[(kandidat, wert, sicher)] einer Quelle: der sichere Treffer zuerst, dann nach Nähe."""
        import difflib
        cands, seen = [], set()
        for term in terms:
            text = match.query_text(term)
            if not text:
                continue
            for c in src.search(text, {"system": system, "terms": terms}):
                if c.get("ref") not in seen:
                    seen.add(c.get("ref"))
                    cands.append(c)
        if not cands:
            return []

        def near(c) -> float:
            best = 0.0
            for term in terms:
                level, value = match.relate(term, c["name"])
                best = max(best, value if level else
                           0.6 * difflib.SequenceMatcher(None, match.norm(term), match.norm(c["name"])).ratio())
            if match._system_tier(c, system) == 2:
                best *= 0.3                      # anderes Gerät: ganz nach hinten
            return best
        ranked = sorted(cands, key=lambda c: -near(c))[:MAX_CANDIDATES + 2]
        unknown = [c for c in ranked if c.get("game") is None]
        if unknown:
            src.details(unknown)
        ranked = [c for c in ranked if c.get("game") is not False][:MAX_CANDIDATES]
        safe, _info = self._settle(src, terms, [c for c in cands if c.get("game") is not None], system)
        out = []
        for c in ranked:
            try:
                offers = sorted(src.images(c).keys()) if src.name != "sgdb" else list(src.kinds)
            except FetchError:
                offers = list(src.kinds)
            c["offers"] = offers
            out.append((c, near(c), safe is not None and c.get("ref") == safe.get("ref")))
        out.sort(key=lambda row: (not row[2], -row[1]))
        return out

    def _thumb(self, src, cand: dict, thumb_dir: str) -> str | None:
        """Kleines Vorschaubild eines Kandidaten im Zwischenspeicher. Scheitert still (None)."""
        from PIL import Image
        url = None
        try:
            url = src.thumb(cand)
            if not url and src.name == "sgdb":
                first = (src.images(cand, ("cover",)).get("cover") or [{}])[0]
                url = first.get("thumb") or first.get("url")
        except FetchError:
            url = None
        url = https_url(url)
        if not url or not self.http.allowed(url):
            return None
        path = os.path.join(thumb_dir, THUMB_PREFIX + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".jpg")
        if os.path.isfile(path):
            return path
        try:
            body, _fmt, _dims = self._load_image(url, src.name, max_bytes=MAX_THUMB_BYTES)
            with Image.open(io.BytesIO(body)) as im:
                im.draft("RGB", (THUMB_SIDE * 2, THUMB_SIDE * 2))
                im = im.convert("RGBA")
                base = Image.new("RGB", im.size, (14, 16, 20))
                base.paste(im, mask=im.getchannel("A"))
                base.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
                buf = io.BytesIO()
                base.save(buf, "JPEG", quality=85)
            return self._store(buf.getvalue(), "JPEG", thumb_dir, os.path.basename(path)[:-4])
        except (FetchError, OSError, ValueError):
            return None

    def fetch_candidate(self, key: str, candidate: dict, dest_dir: str, kinds=None) -> dict:
        """Einen von Hand gewählten Kandidaten übernehmen und merken. BLOCKIERT.

        candidate: {"source", "ref", "name"} aus ``candidates``. Geladen wird alles, was er
        anbietet (auch Kleines: Die Wahl ist bewusst). Rückgabe wie ``fetch_entry``.
        Für eine gezielte Wahl ohne Suche (itch.io nach der Installation, E100) darf candidate
        zusätzlich "data" tragen (Bildadressen für die Quelle); sie gilt nur, wenn die Suche den
        Kandidaten nicht selbst geliefert hat."""
        gate = self._gate(key, True)
        if gate:
            return gate
        name = str((candidate or {}).get("source") or "")
        ref = str((candidate or {}).get("ref") or "")
        src = self.source(name)
        if src is None or not src.available() or not _REF_FORM.fullmatch(ref):
            return {**self._blank(), "status": "skip", "reason": "diesen Kandidaten gibt es nicht"}
        known = (self._listed.get(key) or {}).get((name, ref))
        cand = known or {"source": name, "ref": ref, "name": str(candidate.get("name") or "")[:200], "game": True}
        if known is None and isinstance(candidate.get("data"), dict):
            cand["data"] = candidate["data"]          # E100: Bildadressen der gezielten Wahl (itch.io)
        return self._fetch_choice(key, {"source": name, "ref": ref, "name": cand.get("name", "")}, dest_dir,
                                  cand=cand, kinds=kinds, remember=True)

    def _fetch_choice(self, key: str, choice: dict, dest_dir: str, cand: dict | None = None, kinds=None,
                      remember: bool = False) -> dict:
        src = self.source(str(choice.get("source")))
        if src is None or not src.available():
            return {**self._blank(), "status": "skip", "reason": "Quelle der gemerkten Wahl nicht verfügbar"}
        if self.service_blocked(src.name) > 0:
            return {**self._blank(), "status": "resting", "reason": "Quelle ruht nach Störungen",
                    "retry_s": int(self.service_blocked(src.name))}
        cand = cand or {"source": src.name, "ref": str(choice.get("ref")), "name": str(choice.get("name") or ""),
                        "game": True}
        job = _Job(kinds or src.kinds, (), None)
        job.first = (src, cand, 1.0)
        try:
            images = choice.get("images") if isinstance(choice.get("images"), dict) and cand.get("data") is None \
                else None
            for attempt in (0, 1):
                if images is None:
                    images = src.images(cand, job.open(src))
                stale = False
                for kind in job.open(src):
                    alts = [im for im in images.get(kind) or [] if isinstance(im, dict) and im.get("url")]
                    for alt in alts[:3]:
                        try:
                            body, fmt, real = self._load_image(alt["url"], src.name, kind)
                        except FetchError as exc:
                            if exc.transient:
                                raise
                            job.problems[kind] = exc.reason
                            stale = stale or exc.reason in ("http-404", "http-403", "http-410", "forbidden-url")
                            continue
                        self._commit(job, kind, body, fmt, real, src, cand, dest_dir)
                        break
                    else:
                        if not alts:
                            job.problems.setdefault(kind, "nicht angeboten")
                if not (stale and attempt == 0 and choice.get("images")):
                    break
                images = None                   # gemerkte Adressen sind veraltet: einmal frisch holen
            job.tried[src.name] = "ok" if job.files else "nothing"
            self._service_ok(src.name)
            if job.files:
                self.set_choice(key, cand, images if isinstance(images, dict) else None)
        except FetchError as exc:
            if exc.reason == "busy":
                return self._busy(job)
            if exc.reason in ("cancelled", "test-mode") or (
                    exc.reason in ("connection", "timeout") and not self.is_online()):
                return {**self._blank(), "status": "offline", "reason": "kein Netz", "files": job.files,
                        "ok": bool(job.files)}
            self._log("artfetch %s: %s: %s" % (key, src.name, exc))
            job.tried[src.name] = "error:" + exc.reason
            job.failed = exc.reason
            self._service_failed(src.name, exc)
        if remember and not job.files:
            # Von Hand gewählt, aber kein einziges Bild geladen (E99): weder als feste Wahl merken
            # (sonst fragte jeder spätere Abruf nur noch diesen Treffer) noch den Vermerk des
            # Eintrags überschreiben; die bisherige Wahl bleibt
            return {**self._blank(), "status": "error" if job.failed else "pick-failed",
                    "reason": job.failed or "von diesem Treffer ließ sich kein Bild laden",
                    "tried": dict(job.tried), "name": str(cand.get("name") or ""), "source": src.name}
        return self._finish(key, job, dest_dir, picked=True)
