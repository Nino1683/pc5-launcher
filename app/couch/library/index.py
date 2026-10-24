"""Die Bibliothek: führt die Einträge aller Quellen zusammen und hält, was couch selbst über
ein Spiel weiß (Optionen, Spielzeit, zuletzt gespielt, ausgeblendet, eigene Einträge).

Quellen sind einsteckbar: register_source(name, fn) mit fn() -> list[Entry]. Sie laufen im
Arbeits-Thread und schreiben nichts. Ihr letztes Ergebnis liegt in library-cache.json, damit
das Home sofort Kacheln zeigt und damit Einträge einer abgezogenen Platte als "nicht
verfügbar" stehen bleiben, statt zu verschwinden.

Platte weg: Was eine Quelle nicht mehr meldet, bleibt als available=False stehen, wenn sein
Ort auf einem fehlenden Datenträger liegt (volume_missing) oder solange ein angemeldeter
Datenträger (add_volume, z. B. die Spiele-Platte) fehlt. Eine Quelle, die gerade nicht lesen
kann, wirft SourceSkipped: Dann bleibt ihr letzter Stand unverändert.
"""
from __future__ import annotations

import calendar
import copy
import dataclasses
import json
import os
import re
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from .. import jsontext, log, paths, system
from . import describe
from .model import OPTION_DEFAULTS, Entry, pad_spots_value, source_title

_log = log.get("library")

SourceFn = Callable[[], list]

# Reihenfolge = Vorrang bei doppelten IDs (die zuerst angemeldete Quelle gewinnt)
_SOURCES: dict[str, SourceFn] = {}

_ENTRY_FIELDS = {f.name for f in dataclasses.fields(Entry)}
# Felder von detail, in denen Quellen den Ort eines Spiels ablegen (für "Platte fehlt")
_PATH_KEYS = ("volume", "path", "dir", "install_dir", "exe", "rom", "file", "prefix")


class SourceSkipped(Exception):
    """Eine Quelle kann gerade nicht lesen (Datenbank gesperrt, Datei wird neu geschrieben).
    Ihr letzter Stand bleibt stehen; im Protokoll steht eine Zeile, kein Traceback."""


# Zuletzt gespielte Spiele für die Reihe des Home (lib.list filter=recent ohne limit)
RECENT_LIMIT = 12
TITLE_MAX = 120                 # eigener Name (E90), Zeichen
# Sitzungsverlauf (E99): <data>/sessions.json neben library.json, die ältesten gehen zuerst
SESSIONS_FILE = "sessions.json"
SESSIONS_MAX = 1000
# Spiele aus Steams Bibliothek: Steam zählt ihre Spielzeit selbst (localconfig.vdf)
_STEAM_APP = re.compile(r"steam:\d+")
# Allererster Start ohne Zwischenspeicher: So lange wartet lib.list höchstens auf den ersten Lauf
# der Quellen, statt der Seite eine leere Bibliothek zu zeigen
FIRST_LIST_WAIT_S = 1.5
# E106: Kennungen, die auf einem anderen Rechner dasselbe Spiel meinen (Steam-AppID, Lutris-Slug,
# feste App-Kachel). local:, emu:, rom:, Verknüpfungen (steam:shortcut:…) und lutris:<slug>~<zeile>
# (Zeilennummer der Lutris-Datenbank) hängen am Rechner und werden nie übertragen
TRANSFER_ID = re.compile(r"steam:\d{1,10}|lutris:[A-Za-z0-9][A-Za-z0-9._-]{0,199}|app:[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# E106: Optionen mit Pfaden dieses Rechners (Spielstand-Ordner, Startdatei) reisen nicht mit. Ebenso
# Umgebungsvariablen und Startargumente: Aus einer fremden Sicherung (ZIP von einem Laufwerk) könnten sie
# beim nächsten Start Code nachladen (LD_PRELOAD auf eine Datei des Sticks, Nachbesserung E106)
LOCAL_OPTIONS = ("saves", "exe", "env", "args")
# E106: längste Sitzung, die eine Sicherung mitbringen darf (mehr ist ein Fehler in der Datei)
SESSION_MAX_S = 7 * 86400
# E108: Rückblick, Listen höchstens so lang
STATS_TOP = 5
STATS_ADDED = 10
STATS_RANGES = ("week", "month", "all")
STATS_OFFSET_MAX = 520                  # zehn Jahre Wochen zurück
_MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember")
_MONTHS_SHORT = ("Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez.")


def transferable(entry_id: Any) -> bool:
    """E106: Meint diese Kennung auf jedem Rechner dasselbe (Sichern und Übernehmen)?"""
    return isinstance(entry_id, str) and TRANSFER_ID.fullmatch(entry_id) is not None


def is_steam_app(entry_id: Any) -> bool:
    """steam:<appid>: ein Spiel aus Steams Bibliothek (keine Verknüpfung). E106: dessen Meta bleibt beim
    Übernehmen, auch wenn es hier noch nicht installiert ist."""
    return isinstance(entry_id, str) and _STEAM_APP.fullmatch(entry_id) is not None


def _midnight(year: int, month: int, day: int) -> float:
    """0 Uhr Ortszeit dieses Tages; mktime rechnet Tage über das Monatsende selbst um (32. = 1. des
    Folgemonats, 0. = letzter des Vormonats) und kennt die Zeitumstellung (tm_isdst = -1)."""
    return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))


def _date_of(stamp: float) -> time.struct_time:
    return time.localtime(stamp)


def _overlap(a_from: float, a_to: float, b_from: float, b_to: float) -> float:
    return max(0.0, min(a_to, b_to) - max(a_from, b_from))


def _period(kind: str, offset: int, now: float) -> tuple[list[float], list[str], str]:
    """Grenzen des Zeitraums (Beginn jedes Tages bzw. Monats und das Ende), die Namen der Abschnitte
    ("2026-10-21" bzw. "2026-10") und die Überschrift (E108)."""
    today = time.localtime(now)
    if kind == "week":
        first = today.tm_mday - today.tm_wday - 7 * offset
        bounds = [_midnight(today.tm_year, today.tm_mon, first + i) for i in range(8)]
        days = [_date_of(b) for b in bounds[:7]]
        dates = [time.strftime("%Y-%m-%d", d) for d in days]
        a, b = days[0], days[6]
        if (a.tm_year, a.tm_mon) == (b.tm_year, b.tm_mon):
            label = "%d.–%d. %s" % (a.tm_mday, b.tm_mday, _MONTHS_SHORT[b.tm_mon - 1])
        elif a.tm_year == b.tm_year:
            label = "%d. %s – %d. %s" % (a.tm_mday, _MONTHS_SHORT[a.tm_mon - 1], b.tm_mday, _MONTHS_SHORT[b.tm_mon - 1])
        else:
            label = "%d. %s %d – %d. %s %d" % (a.tm_mday, _MONTHS_SHORT[a.tm_mon - 1], a.tm_year,
                                               b.tm_mday, _MONTHS_SHORT[b.tm_mon - 1], b.tm_year)
        return bounds, dates, label
    if kind == "month":
        year, month = divmod(today.tm_year * 12 + today.tm_mon - 1 - offset, 12)
        month += 1
        length = calendar.monthrange(year, month)[1]
        bounds = [_midnight(year, month, 1 + i) for i in range(length + 1)]
        dates = ["%04d-%02d-%02d" % (year, month, 1 + i) for i in range(length)]
        return bounds, dates, "%s %d" % (_MONTHS[month - 1], year)
    # all: die letzten zwölf Monate (der laufende zuletzt) als Abschnitte
    months = []
    for back in range(11, -2, -1):
        year, month = divmod(today.tm_year * 12 + today.tm_mon - 1 - back, 12)
        months.append((year, month + 1))
    bounds = [_midnight(y, m, 1) for y, m in months]
    return bounds, ["%04d-%02d" % ym for ym in months[:12]], "Insgesamt"


def register_source(name: str, fn: SourceFn) -> None:
    """Meldet eine Quelle für alle künftigen LibraryIndex-Objekte an."""
    _SOURCES[name] = fn


def unregister_source(name: str) -> None:
    _SOURCES.pop(name, None)


def _number(value: Any) -> float | None:
    """Zahl oder None. bool, Texte, Listen und NaN/unendlich zählen nicht."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) < 1e15 else None


_STR_FIELDS = ("kind", "source", "badge", "sort_title")
_CONTAINERS = {"art": dict, "options": dict, "detail": dict, "tags": list}
_BOOL_FIELDS = ("available", "installed", "hidden", "favorite")


def entry_from_dict(data: dict) -> Entry:
    """Unbekannte Felder (ältere/neuere Fassung der Datei) werden überlesen. Die Datei kann von
    Hand bearbeitet oder von einer anderen Fassung geschrieben sein: Ohne brauchbare id und
    title gibt es keinen Eintrag (ValueError); alle anderen Felder mit falschem Typ fallen auf
    ihre Vorgabe, damit ein einzelner Wert nie die ganze Bibliothek leert."""
    if not isinstance(data, dict):
        raise ValueError("Eintrag ist kein Objekt")
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ValueError("Eintrag ohne brauchbare id")
    if not isinstance(data.get("title"), str):
        raise ValueError("Eintrag %s ohne brauchbaren Titel" % data["id"])
    clean: dict = {}
    for key, value in data.items():
        if key not in _ENTRY_FIELDS:
            continue
        if key in _STR_FIELDS and not isinstance(value, str):
            continue
        if key in _CONTAINERS and not isinstance(value, _CONTAINERS[key]):
            continue
        if key in _BOOL_FIELDS and not isinstance(value, bool):
            continue
        if key == "playtime":
            number = _number(value)
            if number is None or number < 0:
                continue
            value = int(number)
        elif key in ("last_played", "added"):
            number = _number(value)
            value = number if number is not None and number > 0 else None
        clean[key] = copy.deepcopy(value)
    return Entry(**clean)


def sane_entry(entry: Any) -> bool:
    """Was eine Quelle liefert, muss wenigstens id und title als Text haben (sonst scheitert
    schon das Sortieren, und lib.list bliebe für immer leer)."""
    return isinstance(entry, Entry) and isinstance(entry.id, str) and bool(entry.id) \
        and isinstance(entry.title, str) and isinstance(entry.sort_title, str)


def clean_title(value: Any) -> str:
    """Eigener Name (E90), bereinigt wie die Titel der Quellen. sources erst hier laden: Das Paket
    lädt index, und `python -m couch.library.sources` darf sources vorher nicht im Speicher finden."""
    from .sources import clean_text
    return clean_text(value)[:TITLE_MAX] if isinstance(value, str) else ""


def clean_meta(record: dict) -> tuple[dict, bool]:
    """Datensatz aus library.json "meta" mit geprüften Typen: (datensatz, etwas verworfen).
    Unbekannte Schlüssel (künftige Fassung) bleiben stehen."""
    out: dict = {}
    dropped = False
    for key, value in record.items():
        if key == "options":
            if not isinstance(value, dict):
                dropped = True
                continue
            value = {str(k): v for k, v in value.items()}
        elif key == "playtime":
            number = _number(value)
            if number is None or number < 0:
                dropped = True
                continue
            value = int(number)
        elif key == "last_played":
            number = _number(value)
            if number is None or number <= 0:
                dropped = dropped or value is not None
                continue
            value = number
        elif key == "hidden":
            if not isinstance(value, bool):
                dropped = True
                continue
        elif key in ("forgotten", "favorite"):
            if not isinstance(value, bool):
                dropped = True
                continue
        elif key == "title":
            value = clean_title(value)
            if not value:
                dropped = True
                continue
        out[str(key)] = value
    return out, dropped


def entry_to_dict(entry: Entry) -> dict:
    return dataclasses.asdict(entry)


def local_form(key: str, record: dict) -> dict:
    """Eigener Eintrag aus library.json in der Form, die der Index führt (Entry als Objekt).
    Steht dort der flache Datensatz des Installers (library/local.py: install_dir, exe … auf
    oberster Ebene), wandern dessen Felder nach detail; sonst bliebe bis zum ersten Lauf der
    Quelle "local" ein Eintrag ohne Ordner und Startdatei stehen."""
    if isinstance(record.get("detail"), dict) or "install_dir" not in record:
        return record
    return {"id": record.get("id") or key, "title": record.get("title"), "kind": "game", "source": "local",
            "detail": {k: v for k, v in record.items() if k not in ("id", "title")}}


def volume_of(path: str) -> str | None:
    """Einhängepunkt eines Wechseldatenträgers, auf dem path liegt (sonst None)."""
    parts = path.split(os.sep)
    if path.startswith("/run/media/") and len(parts) >= 5:
        return os.sep.join(parts[:5])
    if path.startswith(("/media/", "/mnt/")) and len(parts) >= 3:
        return os.sep.join(parts[:3])
    return None


def volume_missing(entry: Entry) -> bool:
    """True, wenn der Eintrag auf einem Datenträger liegt, der gerade nicht eingehängt ist."""
    for key in _PATH_KEYS:
        value = entry.detail.get(key)
        if isinstance(value, str) and value.startswith(os.sep):
            vol = value if key == "volume" else volume_of(value)
            if vol and not os.path.ismount(vol):
                return True
    return False


class LibraryIndex:
    def __init__(self, path: str | None = None, cache_path: str | None = None,
                 sources: dict[str, SourceFn] | None = None,
                 on_main: Callable[[Callable[[], None]], None] | None = None,
                 missing: Callable[[Entry], bool] = volume_missing, writer: Any = None):
        self.path = path or paths.library_file()
        # writer (system.WriteBehind): library.json und der Zwischenspeicher werden in einem
        # eigenen Thread geschrieben (kein fsync in der Hauptschleife). Ohne writer sofort (Tests).
        self._writer = writer
        self.cache_path = cache_path or paths.library_cache_file()
        self._own_sources = sources                 # None = die global angemeldeten
        self._on_main = on_main or (lambda fn: fn())
        self._missing = missing
        self._lock = threading.RLock()
        self._observers: list[Callable[[], None]] = []
        self._meta: dict[str, dict] = {}            # id -> {options, playtime, last_played, hidden}
        self._local: dict[str, dict] = {}           # id -> Entry als dict (eigene Einträge)
        self._scanned: dict[str, list[Entry]] = {}  # Quelle -> Einträge des letzten Laufs
        self._entries: dict[str, Entry] = {}
        self._order: dict[str, int] = {}            # id -> Platz in der Reihenfolge der Quellen
        self._volumes: list[tuple[str, bool]] = []  # (Wurzel, muss Einhängepunkt sein)
        # Bilder, die nach dem Lesen der Quellen neu aufgelöst wurden: id -> (Zeit, art)
        self._art_patch: dict[str, tuple[float, dict]] = {}
        self._scan: Future | None = None
        self._scan_again = False
        self.broken_copy: str | None = None
        # library.json war beim Start da, aber nicht lesbar (Rechte, Plattenfehler): nie speichern,
        # sonst ersetzte das erste Speichern Spielzeiten, Optionen und die Datensätze der
        # Archiv-Spiele ohne Sicherung
        self.read_failed = False
        self._seen_since: float | None = None       # seit wann neu Installiertes erkannt wird (_mark_seen)
        # E108 (E85): Quellen, die schon einmal vollständig gelaufen sind (auch leer). Liefert eine Quelle
        # zum ersten Mal etwas (ROM-Ordner eingeschaltet), zählt ihr Bestand nicht als „neu“
        self._seen_sources: set[str] | None = None
        # Namen der Quellen, die gerade ausgeschaltet sind (ROM-Ordner mit library.roms aus; die
        # Verdrahtung setzt die Funktion). Ihr leeres Ergebnis trägt sie nicht in seen_sources ein.
        self.inactive_sources: Callable[[], Any] | None = None
        self._refused_logged = False
        self.last_scan: float = 0.0
        self.errors: dict[str, str] = {}            # Quelle -> Fehlertext des letzten Laufs
        self._warned: set[str] = set()
        self.option_validators: dict[str, Callable[[Any], Any]] = {}    # siehe register_rpc
        # Was lib.get außer dem Eintrag braucht (describe.py): die Verdrahtung setzt eine
        # Funktion -> dict ein; detail_hooks(entry, ergebnis) dürfen das Ergebnis ergänzen
        self.describe_env: Callable[[], dict] | None = None
        self.detail_hooks: list[Callable[[Entry, dict], None]] = []
        self._rename_hooks: list[Callable[[Entry], None]] = []
        self.sessions_path = os.path.join(os.path.dirname(self.path), SESSIONS_FILE)
        self._sessions: list[dict] | None = None    # erst beim ersten Gebrauch gelesen
        self._sessions_failed = False
        self._load()
        self._load_cache()
        self._rebuild()

    # ------------------------------------------------------------ Dateien
    def _load(self) -> None:
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return
        except OSError as e:
            self.read_failed = True
            _log.error("library.json nicht lesbar (%s): Änderungen gelten nur bis zum Neustart, "
                       "die Datei bleibt unverändert", e)
            return
        # einzelne Bytes, die kein UTF-8 sind, dürfen das Home nicht am Start hindern
        try:
            raw, damaged = blob.decode("utf-8"), False
        except UnicodeDecodeError:
            raw, damaged = blob.decode("utf-8", errors="replace"), True
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("oberste Ebene ist kein Objekt")
            meta = data.get("meta", {})
            local = data.get("local", {})
            if not isinstance(meta, dict) or not isinstance(local, dict):
                raise ValueError("meta/local haben die falsche Form")
        except (ValueError, RecursionError) as e:
            # Spielzeiten und eigene Einträge sind Daten des Nutzers: nie stumm überschreiben
            self._keep_broken(blob)
            _log.error("library.json kaputt (%s); Sicherung: %s", e, self.broken_copy)
            return
        since = _number(data.get("seen_since"))
        self._seen_since = since if since is not None and since > 0 else None
        listed = data.get("seen_sources")
        if isinstance(listed, list):
            self._seen_sources = {str(n) for n in listed if isinstance(n, str) and n}
        elif listed is not None:
            damaged = True
        # Gültiges JSON mit falschen Typen (von Hand bearbeitet, andere Fassung): einzelne
        # Werte verwerfen statt den Start scheitern zu lassen; die Urfassung bleibt liegen
        self._meta = {}
        for key, record in meta.items():
            if not isinstance(record, dict):
                damaged = True
                continue
            self._meta[str(key)], dropped = clean_meta(record)
            damaged = damaged or dropped
        self._local = {}
        for key, record in local.items():
            if isinstance(record, dict):
                self._local[str(key)] = local_form(str(key), record)
            else:
                damaged = True
        if damaged:
            self._keep_broken(blob)
            _log.error("library.json enthält unbrauchbare Werte, sie werden übergangen; "
                       "Sicherung der Urfassung: %s", self.broken_copy)

    def _keep_broken(self, blob: bytes) -> None:
        self.broken_copy = "%s.kaputt-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            system.write_atomic(self.broken_copy, blob)
        except OSError as e:
            # Ohne Sicherung nie überschreiben: Spielzeiten, Optionen und Archiv-Spiele wären weg
            _log.error("Sicherung von library.json gescheitert (%s); die Datei bleibt bis zum nächsten Start unberührt", e)
            self.broken_copy = None
            self.read_failed = True

    def _save(self) -> None:
        if self.read_failed:
            if not self._refused_logged:
                self._refused_logged = True
                _log.error("library.json wird nicht gespeichert: Sie war beim Start nicht lesbar (%s)", self.path)
            return
        data = {"version": 1, "meta": self._meta, "local": self._local}
        if self._seen_since is not None:
            data["seen_since"] = self._seen_since
        if self._seen_sources is not None:
            data["seen_sources"] = sorted(self._seen_sources)
        text = jsontext.dumps(data, indent=1, sort_keys=True) + "\n"
        if self._writer is not None:
            self._writer.write(self.path, text)
            return
        try:
            system.write_atomic(self.path, text)
        except OSError as e:
            _log.error("library.json nicht speicherbar: %s", e)

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            for name, items in data.get("sources", {}).items():
                entries = []
                for item in items:
                    try:
                        entries.append(entry_from_dict(item))
                    except (ValueError, TypeError) as e:
                        _log.warning("library-cache.json: Eintrag aus %s übergangen (%s)", name, e)
                self._scanned[str(name)] = entries
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError, RecursionError) as e:
            _log.warning("library-cache.json verworfen: %s", e)     # nur ein Zwischenspeicher
            self._scanned = {}

    def _save_cache(self) -> None:
        data = {"version": 1, "time": time.time(),
                "sources": {n: [entry_to_dict(e) for e in items]
                            for n, items in self._scanned.items()}}
        text = jsontext.dumps(data) + "\n"
        if self._writer is not None:
            self._writer.write(self.cache_path, text)
            return
        try:
            system.write_atomic(self.cache_path, text)
        except OSError as e:
            _log.warning("library-cache.json nicht speicherbar: %s", e)

    # ------------------------------------------------------------ Zusammenführen
    def _sources(self) -> dict[str, SourceFn]:
        return dict(_SOURCES if self._own_sources is None else self._own_sources)

    def _apply_meta(self, entry: Entry) -> Entry:
        meta = self._meta.get(entry.id)
        # Installiert seit: was das Home gesehen hat (_mark_seen), sonst der Zeitpunkt aus dem
        # Datensatz des Installers (Archiv, Epic, GOG)
        seen = _number(meta.get("seen")) if isinstance(meta, dict) else None
        created = _number(entry.detail.get("created")) if isinstance(entry.detail, dict) else None
        entry.added = seen if seen is not None else (created if created and created > 0 else None)
        entry.added = entry.added if entry.added and entry.added > 0 else None
        # Favorit (E99): Steams Sammlung „Favoriten“ gilt, bis der Nutzer es hier selbst festlegt
        entry.favorite = isinstance(entry.detail, dict) and entry.detail.get("steam_favorite") is True
        if not isinstance(meta, dict) or not meta:
            return entry
        if isinstance(meta.get("favorite"), bool):
            entry.favorite = meta["favorite"]
        options = meta.get("options")
        if isinstance(options, dict):
            entry.options = {**(entry.options if isinstance(entry.options, dict) else {}), **options}
        # Steam zählt seine Spielzeit selbst; unsere kommt für eigene Starts dazu. Bei Spielen aus
        # Steams Bibliothek zählt record_play seit E99 nichts mehr dazu (sonst doppelt); was bis
        # dahin in meta.playtime steht, bleibt und zählt weiter mit.
        # Nur Zahlen zählen: Ein Text an dieser Stelle darf den Start nicht kosten.
        entry.playtime = int(max(0.0, _number(entry.playtime) or 0.0)
                             + max(0.0, _number(meta.get("playtime")) or 0.0))
        stamps = [t for t in (_number(entry.last_played), _number(meta.get("last_played"))) if t and t > 0]
        entry.last_played = max(stamps) if stamps else None
        hidden = meta.get("hidden", entry.hidden)
        entry.hidden = hidden if isinstance(hidden, bool) else bool(entry.hidden is True)
        # Eigener Name (E90): Kachel, Suche und Sortierung nehmen ihn, der der Quelle bleibt in detail
        title = meta.get("title")
        if isinstance(title, str) and title and title != entry.title:
            from .sources import sort_title
            entry.detail = {**(entry.detail if isinstance(entry.detail, dict) else {}), "original_title": entry.title}
            entry.title = title
            entry.sort_title = sort_title(title)
        return entry

    def _rebuild(self) -> None:
        try:
            merged = self._merge()
        except Exception:
            # Letztes Netz: lieber eine leere Bibliothek als ein Home, das nicht startet
            _log.exception("Bibliothek ließ sich nicht zusammenführen, sie bleibt vorerst leer")
            merged = {}
        with self._lock:
            self._entries = merged
            self._order = {eid: n for n, eid in enumerate(merged)}

    def _merge(self) -> dict[str, Entry]:
        merged: dict[str, Entry] = {}
        order = list(self._sources()) + [n for n in self._scanned if n not in self._sources()]
        for name in order:
            for entry in self._scanned.get(name, []):
                if not sane_entry(entry):
                    self._warn_once("Eintrag %r aus Quelle %s hat keinen brauchbaren Titel und "
                                    "wird übergangen" % (getattr(entry, "id", "?"), name))
                    continue
                if entry.id in merged:
                    _log.debug("doppelte ID %s aus Quelle %s übergangen", entry.id, name)
                    continue
                if self._forgotten(entry):
                    continue
                try:
                    merged[entry.id] = self._apply_meta(copy.deepcopy(entry))
                except Exception as e:      # ein kaputter Datensatz darf die anderen nicht mitreißen
                    self._warn_once("Eintrag %s übergangen: %s: %s" % (entry.id, type(e).__name__, e))
        for eid, data in self._local.items():
            if eid not in merged:
                try:
                    merged[eid] = self._apply_meta(entry_from_dict(data))
                except Exception as e:
                    self._warn_once("eigener Eintrag %s unlesbar: %s" % (eid, e))
        return merged

    def _forgotten(self, entry: Entry) -> bool:
        """Aus der Liste entfernt (E91), solange das Spiel nicht wieder verfügbar ist."""
        meta = self._meta.get(getattr(entry, "id", None))
        return isinstance(meta, dict) and meta.get("forgotten") is True and getattr(entry, "available", True) is False

    def _mark_seen(self, results: dict[str, list[Entry]] | None = None) -> bool:
        """Nach einem vollständigen Lauf: Wann ist ein Spiel neu installiert aufgetaucht? Merkt die
        Zeit in meta.seen. Beim allerersten Mal (noch kein seen_since) zählt alles Vorhandene als
        schon immer da, sonst stünde die ganze Bibliothek als „neu“ vorn. Deinstalliert (installed
        False) vergisst es die Zeit: Wieder installiert ist es wieder neu. Liefert, ob sich etwas änderte.

        E108 (E85): results (Quelle -> gelieferte Einträge dieses Laufs) führt seen_sources. Eine Quelle, die
        vollständig gelaufen ist, steht danach darin, auch wenn sie leer war: Ihr erstes Spiel ist dann
        wirklich neu installiert (neues System: erstes Store-, Steam- oder Lutris-Spiel). Liefert eine Quelle
        Spiele, die nie darin stand (ausgeschaltet, gescheitert oder neu, etwa der ROM-Ordner nach dem
        Einschalten), gilt ihr ganzer Bestand als schon immer da (seen = 0) und flutet die Reihe des Home
        nicht. Ausgeschaltete Quellen (inactive_sources) bleiben mit ihrem leeren Ergebnis draußen. Fehlt
        seen_sources noch (Fassung davor, allererster Lauf), werden die gelaufenen Quellen still
        eingetragen: Dort gilt die Regel von bisher."""
        now = time.time()
        first = self._seen_since is None
        changed = first
        ran = set(results or {})
        try:
            off = set(self.inactive_sources() or ()) if callable(self.inactive_sources) else set()
        except Exception:
            _log.exception("ausgeschaltete Quellen")
            off = set(ran)                  # im Zweifel keine leere Quelle eintragen
        delivering = {name for name, items in (results or {}).items()
                      if any(isinstance(e, Entry) and e.kind == "game" for e in items)}
        if self._seen_sources is None:
            self._seen_sources = set(delivering) | (ran - off)
            changed = True
        fresh_sources = delivering - self._seen_sources
        # Woher ein Eintrag in diesem Lauf kam (bei doppelter ID gewinnt die frühere Quelle wie in _merge)
        came_from: dict[str, str] = {}
        for name, items in (results or {}).items():
            for e in items:
                if isinstance(e, Entry):
                    came_from.setdefault(e.id, name)
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
            if e.kind != "game":
                continue
            meta = self._meta.get(e.id)
            if isinstance(meta, dict) and meta.get("forgotten") is True and e.available:
                meta.pop("forgotten")                   # wieder da (neu installiert): wieder in der Liste
                changed = True
            if e.installed:
                if not isinstance(meta, dict) or "seen" not in meta:
                    created = _number(e.detail.get("created")) if isinstance(e.detail, dict) else None
                    old = first or came_from.get(e.id) in fresh_sources
                    self._meta.setdefault(e.id, {})["seen"] = 0 if old else (created if created and created > 0 else now)
                    changed = True
            elif isinstance(meta, dict) and "seen" in meta:
                meta.pop("seen")
                changed = True
        if fresh_sources:
            _log.info("Quellen liefern zum ersten Mal Spiele, ihr Bestand zählt nicht als neu: %s",
                      ", ".join(sorted(fresh_sources)))
        newly = (fresh_sources | (ran - off)) - self._seen_sources
        if newly:
            self._seen_sources |= newly
            changed = True
        if first:
            self._seen_since = now
        if changed:
            self._save()
        return changed

    def _warn_once(self, text: str) -> None:
        # _rebuild läuft nach jeder Änderung: dieselbe Zeile nicht jedes Mal wiederholen
        if text not in self._warned:
            self._warned.add(text)
            _log.warning("%s", text)

    def _notify(self) -> None:
        for fn in list(self._observers):
            try:
                fn()
            except Exception:
                _log.exception("Beobachter der Bibliothek")

    # ------------------------------------------------------------ Schnittstelle (interfaces.LibraryIndex)
    def entries(self) -> list[Entry]:
        with self._lock:
            items = list(self._entries.values())
        return sorted(items, key=lambda e: str(e.sort_title or e.title or "").casefold())

    def get(self, entry_id: str) -> Entry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def on_change(self, fn: Callable[[], None]) -> None:
        self._observers.append(fn)

    def on_rename(self, fn: Callable[[Entry], None]) -> None:
        """fn(eintrag) nach lib.rename, wenn sich der geltende Name wirklich geändert hat (E99:
        Bilder neu auflösen, im Netz mit dem neuen Namen suchen)."""
        self._rename_hooks.append(fn)

    def own_title(self, entry_id: str) -> str | None:
        """Eigener Name (E90) oder None. Liest nur meta: auch im Thread des Einlesens."""
        meta = self._meta.get(entry_id)
        title = meta.get("title") if isinstance(meta, dict) else None
        return title if isinstance(title, str) and title else None

    def with_own_title(self, entry: Entry) -> Entry:
        """Eintrag einer Quelle mit dem eigenen Namen, wie _apply_meta ihn setzt (E90). Für die
        Bilder beim Einlesen: Platzhalter und Merkzettel hängen am Titel; mit dem Namen der Quelle
        entstünde dort eine andere Fassung als im Bilder-Thread, und jede löschte die andere (E99).
        Rückgabe: eine Kopie (detail flach kopiert) oder, ohne eigenen Namen, der Eintrag selbst."""
        title = self.own_title(entry.id)
        if title is None or title == entry.title:
            return entry
        from .sources import sort_title
        detail = entry.detail if isinstance(entry.detail, dict) else {}
        return dataclasses.replace(entry, title=title, sort_title=sort_title(title),
                                   detail={**detail, "original_title": entry.title})

    def first_scan_pending(self) -> Future | None:
        """Der allererste Lauf der Quellen läuft noch und aus dem Zwischenspeicher kam nichts
        (kalter Start): sein Future, sonst None."""
        with self._lock:
            if self.last_scan or any(self._scanned.values()) or self._scan is None \
                    or self._scan.done():
                return None
            return self._scan

    def position(self, entry_id: str) -> int:
        """Platz in der Reihenfolge der Quellen (Apps stehen so fest: Steam, YouTube, Kodi …)."""
        with self._lock:
            return self._order.get(entry_id, len(self._order))

    # ------------------------------------------------------------ Datenträger
    def add_volume(self, path: str, mount: bool | None = None) -> None:
        """Meldet einen Datenträger an, auf dem Spiele liegen (die Spiele-Platte). Fehlt er,
        verschwindet beim Einlesen nichts. mount: Die Wurzel muss ein Einhängepunkt sein
        (Vorgabe: ja, wenn sie wie einer aussieht, /run/media/<nutzer>/<name>)."""
        if not path or not os.path.isabs(path):
            return
        if mount is None:
            mount = volume_of(path) == path.rstrip(os.sep)
        item = (path.rstrip(os.sep) or os.sep, bool(mount))
        if item not in self._volumes:
            self._volumes.append(item)

    def missing_volumes(self) -> list[str]:
        """Angemeldete Datenträger, die gerade nicht da sind."""
        gone = []
        for root, mount in self._volumes:
            try:
                there = os.path.isdir(root) and (os.path.ismount(root) if mount else True)
            except OSError:
                there = False
            if not there:
                gone.append(root)
        return gone

    def set_option(self, entry_id: str, key: str, value: Any) -> None:
        if key not in OPTION_DEFAULTS:
            raise KeyError("unbekannte Option: %s" % key)
        if self.get(entry_id) is None:
            raise KeyError("unbekannter Eintrag: %s" % entry_id)
        json.dumps(value)
        meta = self._meta.setdefault(entry_id, {})
        options = meta.setdefault("options", {})
        if value == OPTION_DEFAULTS[key]:
            options.pop(key, None)              # nur Abweichungen speichern
        else:
            options[key] = copy.deepcopy(value)
        if key == "proton" and options.get("launcher") == "lutris":
            # Wer eine Proton-Fassung wählt, will mit Proton starten: „wie in Lutris“ (E55, nur von der
            # Startreparatur gesetzt) überginge die Wahl (Wunsch 2026-10-24: „Selbst wenn ich manuell eine
            # Proton-Version auswähle, geht es nicht“)
            options.pop("launcher", None)
        self._changed()

    def set_hidden(self, entry_id: str, hidden: bool) -> None:
        if self.get(entry_id) is None:
            raise KeyError("unbekannter Eintrag: %s" % entry_id)
        self._meta.setdefault(entry_id, {})["hidden"] = bool(hidden)
        self._changed()

    def files_gone(self, entry: Entry) -> bool:
        """Spielordner von Hand gelöscht (E91): nicht verfügbar, obwohl sein Datenträger da ist.
        Steam-Spiele zählen nicht (dort deinstalliert Steam), Apps auch nicht."""
        if entry.kind != "game" or entry.available or not entry.installed or entry.source == "steam":
            return False
        if self._missing(entry):
            return False
        where = describe.location(entry)
        if not where or not os.path.isabs(where):
            return True
        vol = volume_of(where)
        if vol and not os.path.ismount(vol):
            return False
        return not os.path.lexists(where) and os.path.isdir(os.path.dirname(os.path.normpath(where)))

    def forget(self, entry_id: str) -> bool:
        """„Aus der Liste entfernen“ (E91), nur wenn files_gone. Eigene Einträge verschwinden ganz,
        die anderer Quellen (Lutris …) merkt sich meta.forgotten; deren Dateien bleiben unberührt.
        Taucht das Spiel wieder auf (neu installiert), steht es wieder in der Liste."""
        entry = self.get(entry_id)
        if entry is None or not self.files_gone(entry):
            return False
        if entry.source == "local" and entry_id in self._local:
            return self.remove_local(entry_id)
        self._meta.setdefault(entry_id, {})["forgotten"] = True
        self._changed()
        return True

    def set_title(self, entry_id: str, title: Any) -> str:
        """Eigener Name (E90). Leer oder gleich dem der Quelle: zurück zum Namen der Quelle.
        Rückgabe: der Name, der jetzt gilt."""
        entry = self.get(entry_id)
        if entry is None:
            raise KeyError("unbekannter Eintrag: %s" % entry_id)
        title = clean_title(title)
        original = source_title(entry)
        before = entry.title
        meta = self._meta.setdefault(entry_id, {})
        if not title or title == original:
            meta.pop("title", None)
            title = original
        else:
            meta["title"] = title
        self._changed()
        after = self.get(entry_id)
        if after is not None and after.title != before:
            for fn in list(self._rename_hooks):
                try:
                    fn(after)
                except Exception:
                    _log.exception("Haken nach dem Umbenennen")
        return title

    def set_favorite(self, entry_id: str, favorite: bool) -> None:
        """Favorit (E99). Die Wahl gilt auch gegen Steams Sammlung „Favoriten“ (die nur gelesen wird)."""
        if self.get(entry_id) is None:
            raise KeyError("unbekannter Eintrag: %s" % entry_id)
        self._meta.setdefault(entry_id, {})["favorite"] = bool(favorite)
        self._changed()

    def record_play(self, entry_id: str, seconds: int) -> None:
        seconds = max(0, int(seconds))
        meta = self._meta.setdefault(entry_id, {})
        # Spiele aus Steams Bibliothek zählt Steam selbst (localconfig.vdf); seit sie unter unserem
        # reaper laufen (E57), zählte jede Sitzung hier ein zweites Mal (E99). Was schon in
        # meta.playtime steht, bleibt unverändert. Verknüpfungen (steam:shortcut:…) zählen weiter:
        # Für sie steht in localconfig.vdf keine Spielzeit.
        if not _STEAM_APP.fullmatch(entry_id):
            meta["playtime"] = int(meta.get("playtime") or 0) + seconds
        meta["last_played"] = time.time()
        entry = self.get(entry_id)
        if seconds and (entry is None or entry.kind == "game"):
            self._add_session(entry_id, seconds)
        self._changed()

    # ------------------------------------------------------------ Sitzungsverlauf (E99)
    def sessions(self) -> list[dict]:
        """[{id, start, seconds}] alter Sitzungen zuerst; höchstens SESSIONS_MAX."""
        if self._sessions is None:
            self._sessions = self._load_sessions()
        return self._sessions

    def _load_sessions(self) -> list[dict]:
        try:
            with open(self.sessions_path, "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return []
        except OSError as e:
            # Rechte, Plattenfehler: nie überschreiben, was man nicht lesen konnte
            self._sessions_failed = True
            _log.error("sessions.json nicht lesbar (%s): Sitzungen werden bis zum Neustart nicht gespeichert", e)
            return []
        try:
            data = json.loads(blob.decode("utf-8", errors="replace"))
            items = data.get("sessions") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("kein Abschnitt sessions")
        except (ValueError, RecursionError) as e:
            # Kaputt: wie library.json eine Sicherung daneben, dann leer weiter (E99; vorher blieb der
            # Verlauf für immer aus). Nur ohne Sicherung bleibt die Datei unberührt
            backup = "%s.kaputt-%s" % (self.sessions_path, time.strftime("%Y%m%d-%H%M%S"))
            try:
                system.write_atomic(backup, blob)
            except OSError as e2:
                self._sessions_failed = True
                _log.error("sessions.json kaputt (%s), Sicherung gescheitert (%s): Sitzungen werden bis zum "
                           "Neustart nicht gespeichert", e, e2)
                return []
            _log.error("sessions.json kaputt (%s); Sicherung: %s; der Verlauf beginnt neu", e, backup)
            return []
        out = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            start, seconds = _number(item.get("start")), _number(item.get("seconds"))
            if start and start > 0 and seconds is not None and seconds > 0:
                out.append({"id": item["id"], "start": start, "seconds": int(seconds)})
        return out[-SESSIONS_MAX:]

    def _add_session(self, entry_id: str, seconds: int) -> None:
        items = self.sessions()
        items.append({"id": entry_id, "start": round(time.time() - seconds, 1), "seconds": int(seconds)})
        del items[:-SESSIONS_MAX]
        self._save_sessions()

    def _save_sessions(self) -> None:
        if self._sessions_failed:
            return
        items = self.sessions()
        text = jsontext.dumps({"version": 1, "sessions": items}) + "\n"
        if self._writer is not None:
            self._writer.write(self.sessions_path, text)
            return
        try:
            system.write_atomic(self.sessions_path, text)
        except OSError as e:
            _log.error("sessions.json nicht speicherbar: %s", e)

    def week_playtime(self, entry_id: str, now: float | None = None) -> int:
        """Sekunden, die dieses Spiel in der laufenden Kalenderwoche (ab Montag 0 Uhr, Ortszeit)
        gespielt wurde, laut Sitzungsverlauf. Schnell, nur aus dem Speicher."""
        now = time.time() if now is None else now
        day = time.localtime(now)
        monday = time.mktime((day.tm_year, day.tm_mon, day.tm_mday - day.tm_wday, 0, 0, 0, 0, 0, -1))
        total = 0.0
        for item in self.sessions():
            if item["id"] == entry_id:
                end = item["start"] + item["seconds"]
                total += max(0.0, min(end, now) - max(item["start"], monday))
        return int(total)

    # ------------------------------------------------------------ Rückblick (E108)
    def stats(self, range: str = "week", offset: int = 0, now: float | None = None) -> dict:
        """Spielzeit-Rückblick „Deine Woche“ (E108) rein aus dem Speicher (Sitzungsverlauf E99, alle Quellen).

        range: week (ab Montag 0 Uhr Ortszeit) | month (Kalendermonat) | all (alles; days = die letzten
        12 Monate als Monate). offset: so viele Zeiträume zurück (0 = der laufende; bei all ohne Wirkung).
        Tagesgrenzen über time.mktime je Tag: Die Woche der Zeitumstellung hat einen Tag mit 23 bzw. 25
        Stunden. Sitzungen über eine Grenze werden geteilt, gezählt wird bis now. Ausgeblendete Spiele
        zählen mit, werden aber nicht genannt (top, longest, added). ValueError bei unbekanntem range.
        {range, offset, from, to, label, total, previous_total (None bei all), days: [{date, seconds}],
         top: [{id, title, seconds, sessions}], sessions, longest: {id, title, seconds, start} | None,
         added: [{id, title, added}], older: gibt es davor noch Sitzungen}; Sekunden als ganze Zahlen,
        title "" für ein Spiel, das die Bibliothek nicht mehr kennt."""
        if range not in STATS_RANGES:
            raise ValueError("Diesen Zeitraum gibt es nicht.")
        now = time.time() if now is None else float(now)
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset != offset:
            offset = 0
        offset = 0 if range == "all" else max(0, min(STATS_OFFSET_MAX, int(offset)))
        bounds, dates, label = _period(range, offset, now)
        items = list(self.sessions())
        start = bounds[0]
        if range == "all":
            start = min([bounds[0]] + [s["start"] for s in items])
        end = bounds[-1]
        days = [{"date": d, "seconds": 0.0} for d in dates]
        per_game: dict[str, list] = {}          # id -> [Sekunden, Sitzungen]
        longest: tuple[float, dict] | None = None
        total = count = 0.0
        for s in items:
            s_from, s_to = s["start"], min(s["start"] + s["seconds"], now)
            part = _overlap(s_from, s_to, start, end)
            if part <= 0:
                continue
            total += part
            count += 1
            for i, day in enumerate(days):         # range ist hier der Zeitraum (Vertrag 4.8)
                day["seconds"] += _overlap(s_from, s_to, bounds[i], bounds[i + 1])
            game = per_game.setdefault(s["id"], [0.0, 0])
            game[0] += part
            game[1] += 1
            if self._nameable(s["id"]) and (longest is None or part > longest[0]):
                longest = (part, {"id": s["id"], "start": max(s_from, start)})
        previous = None
        if range != "all":
            p_bounds, _dates, _label = _period(range, offset + 1, now)
            previous = int(round(sum(_overlap(s["start"], min(s["start"] + s["seconds"], now), p_bounds[0], p_bounds[-1])
                                     for s in items)))
        top = sorted(((sec, n, gid) for gid, (sec, n) in per_game.items() if self._nameable(gid)),
                     key=lambda t: (-t[0], self._stats_title(t[2]).casefold(), t[2]))[:STATS_TOP]
        added = []
        for e in self.entries():
            stamp = _number(e.added)
            if e.kind == "game" and not e.hidden and stamp and start <= stamp < end:
                added.append({"id": e.id, "title": e.title, "added": stamp})
        added.sort(key=lambda a: (-a["added"], a["title"].casefold()))
        return {
            "range": range, "offset": offset, "from": start, "to": end, "label": label,
            "total": int(round(total)), "previous_total": previous,
            "days": [{"date": d["date"], "seconds": int(round(d["seconds"]))} for d in days],
            "top": [{"id": gid, "title": self._stats_title(gid), "seconds": int(round(sec)), "sessions": n}
                    for sec, n, gid in top],
            "sessions": int(count),
            "longest": None if longest is None else {
                "id": longest[1]["id"], "title": self._stats_title(longest[1]["id"]),
                "seconds": int(round(longest[0])), "start": longest[1]["start"]},
            "added": added[:STATS_ADDED],
            # gibt es weiter zurück noch Sitzungen? (die Seite blättert nur so weit)
            "older": range != "all" and any(s["start"] < start for s in items),
        }

    def _nameable(self, entry_id: str) -> bool:
        """Darf der Rückblick dieses Spiel nennen? Ausgeblendete nicht (sie zählen nur mit)."""
        entry = self.get(entry_id)
        if entry is not None:
            return not entry.hidden
        meta = self._meta.get(entry_id)
        return not (isinstance(meta, dict) and meta.get("hidden") is True)

    def _stats_title(self, entry_id: str) -> str:
        entry = self.get(entry_id)
        return entry.title if entry is not None else (self.own_title(entry_id) or "")

    # ------------------------------------------------------------ Sichern und Übernehmen (E106)
    def export_meta(self) -> dict:
        """Was der Index über übertragbare Einträge weiß (transferable): je ID {title?, favorite?,
        hidden?, options?, playtime?, last_played?}. Ohne seen, forgotten, eigene Einträge (local),
        detail und Optionen mit Pfaden dieses Rechners (LOCAL_OPTIONS). Eine Kopie; Hauptschleife."""
        out: dict[str, dict] = {}
        for entry_id, meta in self._meta.items():
            if not transferable(entry_id) or not isinstance(meta, dict):
                continue
            rec: dict = {}
            title = meta.get("title")
            if isinstance(title, str) and title:
                rec["title"] = title
            for key in ("favorite", "hidden"):
                if isinstance(meta.get(key), bool):
                    rec[key] = meta[key]
            options = meta.get("options")
            if isinstance(options, dict):
                kept = {k: copy.deepcopy(v) for k, v in options.items()
                        if k in OPTION_DEFAULTS and k not in LOCAL_OPTIONS}
                if kept:
                    rec["options"] = kept
            playtime = _number(meta.get("playtime"))
            if playtime and playtime > 0:
                rec["playtime"] = int(playtime)
            last = _number(meta.get("last_played"))
            if last and last > 0:
                rec["last_played"] = last
            if rec:
                out[entry_id] = rec
        return out

    def import_meta(self, data: Any, now: float | None = None) -> dict:
        """Übernimmt export_meta() eines anderen Rechners (E106). Zugeordnet wird über die ID: Was hier
        unbekannt oder nicht übertragbar ist, wird übersprungen, außer steam:<appid> (bleibt als
        Meta, falls das Spiel später installiert wird). title bereinigt wie lib.rename (gleich dem der
        Quelle = keiner), favorite und hidden nur als Ein/Aus, options einzeln geprüft (check_option
        und die Prüfungen der Verdrahtung; Ungültiges und LOCAL_OPTIONS fallen weg), playtime und
        last_played = Maximum aus Bestand und Sicherung (zweimal übernehmen verdoppelt nichts;
        last_played höchstens now). Schreibt library.json einmal. Hauptschleife.
        Ergebnis {applied: Zahl der übernommenen IDs, skipped: [IDs]}."""
        now = time.time() if now is None else float(now)
        applied, skipped = 0, []
        if not isinstance(data, dict):
            return {"applied": 0, "skipped": []}
        for raw_id, record in data.items():
            entry_id = str(raw_id)
            entry = self.get(entry_id) if transferable(entry_id) else None
            if not isinstance(record, dict) or not transferable(entry_id) \
                    or (entry is None and not _STEAM_APP.fullmatch(entry_id)):
                skipped.append(entry_id)
                continue
            meta = copy.deepcopy(self._meta.get(entry_id)) if isinstance(self._meta.get(entry_id), dict) else {}
            if "title" in record:
                title = clean_title(record.get("title"))
                original = source_title(entry) if entry is not None else ""
                if title and title != original:
                    meta["title"] = title
                elif title:
                    meta.pop("title", None)
            for key in ("favorite", "hidden"):
                if isinstance(record.get(key), bool):
                    meta[key] = record[key]
            options = record.get("options")
            if isinstance(options, dict):
                mine = dict(meta.get("options")) if isinstance(meta.get("options"), dict) else {}
                for key, value in options.items():
                    if key not in OPTION_DEFAULTS or key in LOCAL_OPTIONS:
                        continue
                    try:
                        value = check_option(key, copy.deepcopy(value))
                        extra = self.option_validators.get(key)
                        if extra is not None:
                            value = extra(value)
                        json.dumps(value)
                    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                        continue
                    if value == OPTION_DEFAULTS[key]:
                        mine.pop(key, None)
                    else:
                        mine[key] = value
                if mine:
                    meta["options"] = mine
                else:
                    meta.pop("options", None)
            playtime = _number(record.get("playtime"))
            if playtime is not None and playtime > 0:
                meta["playtime"] = max(int(_number(meta.get("playtime")) or 0), int(playtime))
            last = _number(record.get("last_played"))
            if last is not None and last > 0:
                meta["last_played"] = max(_number(meta.get("last_played")) or 0.0, min(last, now))
            if meta:
                self._meta[entry_id] = meta
            applied += 1
        if applied:
            self._changed()
        return {"applied": applied, "skipped": skipped}

    def export_sessions(self) -> list[dict]:
        """Sitzungsverlauf der übertragbaren Einträge (E106), älteste zuerst. Eine Kopie."""
        return [dict(s) for s in self.sessions() if transferable(s.get("id"))]

    def import_sessions(self, items: Any) -> dict:
        """Sitzungen aus einer Sicherung dazunehmen (E106). Nur übertragbare IDs, die hier bekannt sind
        (steam:<appid> immer, wie import_meta); eine Sitzung mit gleicher ID und Startzeit gibt es nur
        einmal (zweimal übernehmen ändert nichts), höchstens SESSIONS_MAX. sessions.json schreibt
        weiter nur der Index. Hauptschleife. Ergebnis {applied, skipped}."""
        have = self.sessions()
        keys = {(s["id"], round(float(s["start"]), 1)) for s in have}
        added = skipped = 0
        for item in items if isinstance(items, list) else []:
            entry_id = item.get("id") if isinstance(item, dict) else None
            start = _number(item.get("start")) if isinstance(item, dict) else None
            seconds = _number(item.get("seconds")) if isinstance(item, dict) else None
            if not transferable(entry_id) or start is None or start <= 0 or seconds is None \
                    or not 0 < seconds <= SESSION_MAX_S \
                    or (self.get(entry_id) is None and not _STEAM_APP.fullmatch(entry_id)):
                skipped += 1
                continue
            key = (entry_id, round(start, 1))
            if key in keys:
                continue
            keys.add(key)
            have.append({"id": entry_id, "start": round(start, 1), "seconds": int(seconds)})
            added += 1
        if added:
            have.sort(key=lambda s: s["start"])
            del have[:-SESSIONS_MAX]
            self._save_sessions()
        return {"applied": added, "skipped": skipped}

    def touch(self, entry_id: str) -> None:
        """Nur "zuletzt gespielt" setzen (beim Start, damit die Kachel sofort vorn steht)."""
        self._meta.setdefault(entry_id, {})["last_played"] = time.time()
        self._changed()

    def add_local(self, entry: Entry) -> None:
        """Eigenen Eintrag einfügen oder (gleiche id) ersetzen."""
        self._local[entry.id] = entry_to_dict(entry)
        # Was die Quelle "local" beim letzten Lauf gelesen hat, ist damit überholt: Sonst
        # zeigte die Bibliothek bis zum nächsten Einlesen noch die alte Startdatei
        self._drop_scanned(entry.id)
        self._changed()

    def remove_local(self, entry_id: str) -> bool:
        """Nimmt einen eigenen Eintrag aus der Liste. Dateien fasst das nicht an."""
        if self._local.pop(entry_id, None) is None:
            return False
        self._drop_scanned(entry_id)
        self._art_patch.pop(entry_id, None)
        self._changed()
        return True

    def local_ids(self) -> list[str]:
        return list(self._local)

    def _drop_scanned(self, entry_id: str) -> None:
        for name, items in self._scanned.items():
            if any(e.id == entry_id and e.source == "local" for e in items):
                self._scanned[name] = [e for e in items
                                       if not (e.id == entry_id and e.source == "local")]

    # ------------------------------------------------------------ Bilder nachtragen
    def update_art(self, arts: dict[str, dict], notify: bool = False) -> list[str]:
        """Ersetzt die Bilder einzelner Einträge, ohne die Quellen neu zu lesen (Hauptschleife).

        Für Bilder, die erst nach dem Einlesen fertig werden (Ableitung, Abruf aus dem Netz).
        Ein Lauf der Quellen, der vor dieser Änderung begonnen hat, überschreibt sie nicht.
        Rückgabe: IDs, bei denen sich für die Oberfläche etwas geändert hat."""
        now = time.monotonic()
        changed: list[str] = []
        with self._lock:
            for entry_id, art in arts.items():
                if not isinstance(entry_id, str) or not isinstance(art, dict):
                    continue
                self._art_patch[entry_id] = (now, dict(art))
                for items in self._scanned.values():
                    for entry in items:
                        if entry.id == entry_id and entry.art != art:
                            entry.art = dict(art)
                local = self._local.get(entry_id)
                if isinstance(local, dict) and local.get("art") != art:
                    local["art"] = dict(art)
                merged = self._entries.get(entry_id)
                if merged is not None and merged.art != art:
                    merged.art = dict(art)
                    changed.append(entry_id)
        # Der Zwischenspeicher wird hier bewusst nicht geschrieben (das läuft in der
        # Hauptschleife und käme alle paar Sekunden): Der nächste Lauf der Quellen sichert ihn.
        if notify and changed:
            self._notify()
        return changed

    def _changed(self) -> None:
        self._save()
        self._rebuild()
        self._notify()

    # ------------------------------------------------------------ Einlesen
    def rescan(self) -> Future:
        """Alle Quellen im Hintergrund neu lesen. Läuft schon ein Lauf, folgt danach genau einer."""
        with self._lock:
            if self._scan is not None and not self._scan.done():
                self._scan_again = True
                return self._scan
            fut: Future = Future()
            self._scan = fut
        sources = self._sources()
        threading.Thread(target=self._scan_thread, args=(sources, fut), name="couch-libscan",
                         daemon=True).start()
        return fut

    def rescan_now(self) -> None:
        """Dasselbe im aufrufenden Thread (Tests, Werkzeuge)."""
        started = time.monotonic()
        results, errors = self._run_sources(self._sources())
        self._apply_scan(results, errors, started)

    def _run_sources(self, sources: dict[str, SourceFn]) -> tuple[dict, dict]:
        results: dict[str, list[Entry]] = {}
        errors: dict[str, str] = {}
        for name, fn in sources.items():
            try:
                items = fn()
                results[name] = [e for e in items if sane_entry(e)]
            except SourceSkipped as e:  # kann gerade nicht lesen: letzter Stand bleibt
                _log.warning("Quelle %s ausgelassen, ihr letzter Stand bleibt: %s", name, e)
                errors[name] = str(e) or "ausgelassen"
            except Exception as e:      # eine kaputte Quelle darf die anderen nicht mitreißen
                _log.exception("Quelle %s", name)
                errors[name] = "%s: %s" % (type(e).__name__, e)
        return results, errors

    def _scan_thread(self, sources: dict[str, SourceFn], fut: Future) -> None:
        started = time.monotonic()
        results, errors = self._run_sources(sources)

        def finish() -> None:
            try:
                self._apply_scan(results, errors, started)
                fut.set_result({"entries": len(self._entries), "errors": dict(errors)})
            except BaseException as e:
                fut.set_exception(e)
            again = False
            with self._lock:
                if self._scan_again:
                    self._scan_again = False
                    again = True
            if again:
                self.rescan()
        self._on_main(finish)

    def _apply_scan(self, results: dict[str, list[Entry]], errors: dict[str, str],
                    started: float | None = None) -> None:
        before = {e.id: entry_to_dict(e) for e in self.entries()}
        gone = self.missing_volumes()
        delivered = {e.id for fresh in results.values() for e in fresh}
        for name, fresh in results.items():
            ids = {e.id for e in fresh}
            kept = []
            for old in self._scanned.get(name, []):
                if old.id in ids:
                    continue
                if old.id in delivered:
                    continue            # kommt jetzt aus einer anderen Quelle
                # Nicht mehr gemeldet, aber der Datenträger fehlt: stehen lassen, nicht löschen.
                # Fehlt ein angemeldeter Datenträger, gilt das für alles, was verschwindet:
                # Manche Quelle (Emulatoren) liefert dann gar nichts mehr, und nicht jeder
                # Eintrag verrät, wo er liegt.
                if gone or self._missing(old):
                    old = copy.deepcopy(old)
                    old.available = False
                    kept.append(old)
            self._scanned[name] = fresh + kept
        # Bilder, die während dieses Laufs nachgetragen wurden, gelten weiter; ältere hat
        # der Lauf selbst schon gesehen
        for entry_id, (stamp, art) in list(self._art_patch.items()):
            if started is None or stamp < started:
                del self._art_patch[entry_id]
                continue
            for name in results:
                for entry in self._scanned.get(name, []):
                    if entry.id == entry_id:
                        entry.art = dict(art)
        for name in list(self._scanned):
            if name not in self._sources():
                del self._scanned[name]         # Quelle gibt es nicht mehr (z. B. Prüf-Quelle)
        self.errors = errors
        self.last_scan = time.time()
        self._save_cache()
        self._rebuild()
        if self._mark_seen(results):
            self._rebuild()
        if before != {e.id: entry_to_dict(e) for e in self.entries()}:
            self._notify()


# ---------------------------------------------------------------- RPC lib.*
_human_time = describe.human_time
_human_date = describe.human_date


_RESOLUTIONS = ("native", "1440p", "1080p")          # wie model.OPTION_DEFAULTS und perf.RESOLUTIONS
PAD_LOOKS = (0.5, 0.75, 1.0, 1.5, 2.0)                 # Spiel-Modus des Pads (E64): Tempo des Umschauens
_PERF_LEVELS = ("", "off", "fps", "bar", "detailed")


def check_option(key: str, value: Any) -> Any:
    """Wert einer Einstellung je Spiel prüfen und in seine gespeicherte Form bringen.
    ValueError mit einem deutschen Satz, wenn er nicht passt (die Seite schickt, was ihre
    Auswahl hergibt; ein falscher Wert würde sonst stumm gespeichert und nie wirken)."""
    default = OPTION_DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError("Hier geht nur Ein oder Aus.")
        return value
    if key == "fps_limit":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) \
                or not 0 <= value <= 1000:
            raise ValueError("Die Bildratenbegrenzung muss eine ganze Zahl sein (0 = aus).")
        return int(value)
    if key in ("fsr4", "hdr"):
        if value not in ("auto", "off"):
            raise ValueError("Hier geht nur „Automatisch“ oder „Aus“.")
        return value
    if key == "optiscaler":
        if value not in ("off", "on"):
            raise ValueError("Hier geht nur „Aus“ oder „Ein“.")
        return value
    if key == "pad":
        if value not in ("auto", "xbox", "direct", "kbm", "mouse"):
            raise ValueError("Diese Controller-Einstellung gibt es nicht.")
        return value
    if key == "pad_look":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) not in PAD_LOOKS:
            raise ValueError("Dieses Tempo gibt es nicht.")
        return float(value)
    if key == "pad_spots":
        return pad_spots_value(value)       # {} = zurücksetzen (Spieleinstellungen › Controller, E99)
    if key == "saves":
        if not isinstance(value, str) or (value and (not os.path.isabs(value) or len(value) > 4096)):
            raise ValueError("Das ist kein Ordner.")
        return value
    if key == "launcher":
        if value not in ("auto", "lutris"):
            raise ValueError("Hier geht nur „Automatisch“ oder „Wie in Lutris“.")
        return value
    if key == "resolution":
        if value not in _RESOLUTIONS:
            raise ValueError("Diese Auflösung gibt es nicht.")
        return value
    if key == "perf_overlay":
        if value not in _PERF_LEVELS:
            raise ValueError("Diese Stufe der Leistungsanzeige gibt es nicht.")
        return value
    if key == "env":
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in value.items()):
            raise ValueError("Umgebungsvariablen müssen Paare aus Name und Text sein.")
        return value
    if key == "pad_keys":
        # E109 (Teil P12): Tastenbelegung je Spiel, geprüft in model.pad_keys_value ({} = Standard)
        from . import model
        check = getattr(model, "pad_keys_value", None)
        if check is None:
            raise ValueError("Die Tastenbelegung gibt es in dieser Fassung noch nicht.")
        return check(value)
    if not isinstance(value, str):
        raise ValueError("Hier wird ein Text erwartet.")
    return value


def _favorite_action(entry: Entry, out: dict) -> None:
    """„Zu den Favoriten“/„Aus den Favoriten“ (E99) vor „Ausblenden“ ins Optionsmenü. Hier statt in
    describe.actions: Der Favorit gehört dem Index (meta.favorite, lib.setFavorite)."""
    actions = out.get("actions")
    if not isinstance(actions, list):
        return
    item = {"id": "unfavorite" if entry.favorite else "favorite",
            "label": "Aus den Favoriten" if entry.favorite else "Zu den Favoriten"}
    at = next((n for n, a in enumerate(actions) if isinstance(a, dict) and a.get("id") in ("hide", "unhide")),
              len(actions))
    actions.insert(at, item)


def register_rpc(rpc: Any, library: LibraryIndex, procs: Any = None,
                 validators: dict[str, Callable[[Any], Any]] | None = None) -> None:
    """Meldet lib.* an. procs (AppManager) liefert, was gerade läuft. validators: zusätzliche
    Prüfungen je Option (fn(wert) -> wert oder ValueError); die Verdrahtung hängt dort z. B.
    die erlaubten Bildraten ein. Das dict darf später gefüllt werden."""
    validators = validators if validators is not None else {}
    library.option_validators = validators

    def running(entry_id: str) -> bool:
        return bool(procs and procs.by_entry(entry_id))

    def need(params: dict) -> Entry:
        from ..rpc import RpcError
        entry = library.get(str(params.get("id", "")))
        if entry is None:
            raise RpcError("Diesen Eintrag gibt es nicht mehr.", "NotFound")
        return entry

    def lib_list(params: dict) -> Any:
        """{kind?, filter?: all|installed|hidden|recent|favorites, query?,
        sort?: title|recent|playtime|added, limit?}.
        recent: Laufendes zuerst, dann nach letztem Start über alle Quellen, nie Gespieltes
        hinten in Namensfolge; ohne limit die ersten RECENT_LIMIT (limit 0 = alle).
        playtime: meistgespielt zuerst; added: zuletzt installiert zuerst, schon immer Vorhandenes
        hinten (E99), beide sonst in Namensfolge.
        Apps (kind "app") stehen immer in der festen Reihenfolge ihrer Quelle.
        Kalter Start: Läuft der allererste Lauf der Quellen noch, kommt die Antwort mit ihm
        (spätestens nach FIRST_LIST_WAIT_S), damit die Seite nicht erst eine leere Bibliothek
        aufbaut und den Fokus auf der falschen Kachel behält."""
        scan = library.first_scan_pending()
        if scan is None:
            return list_now(params)
        answer: Future = Future()
        once = threading.Lock()

        def finish(*_ignored: Any) -> None:
            with once:
                if answer.done():
                    return
                try:
                    answer.set_result(list_now(params))
                except Exception as e:
                    answer.set_exception(e)
        timer = threading.Timer(FIRST_LIST_WAIT_S, finish)
        timer.daemon = True
        timer.start()
        scan.add_done_callback(finish)
        return answer

    def list_now(params: dict) -> dict:
        kind = params.get("kind")
        flt = params.get("filter") or "all"
        query = str(params.get("query") or "").casefold().strip()
        items = library.entries()
        if kind:
            items = [e for e in items if e.kind == kind]
        if flt == "hidden":
            items = [e for e in items if e.hidden]
        else:
            items = [e for e in items if not e.hidden]
        if flt == "installed":
            items = [e for e in items if e.installed and e.available]
        elif flt == "favorites":
            items = [e for e in items if e.favorite]
        if query:
            # Nach dem Umbenennen (E90) findet die Suche auch den alten Namen
            items = [e for e in items if query in e.title.casefold() or query in source_title(e).casefold()]
        sort = params.get("sort") or ("recent" if flt == "recent" else "title")
        if kind == "app":
            items.sort(key=lambda e: library.position(e.id))
        elif sort == "recent":
            # Neu installiert zählt wie gerade gespielt (Wunsch 2026-10-21): vorn steht, was zuletzt
            # dazukam oder gespielt wurde; nie Gespieltes und schon immer Vorhandenes hinten
            items.sort(key=lambda e: (not running(e.id), -max(_number(e.last_played) or 0, _number(e.added) or 0),
                                      -(_number(e.last_played) or 0)))
        elif sort == "playtime":
            items.sort(key=lambda e: -(_number(e.playtime) or 0))
        elif sort == "added":
            items.sort(key=lambda e: -(_number(e.added) or 0))
        limit = _number(params.get("limit"))
        if limit is None:
            limit = RECENT_LIMIT if flt == "recent" and kind != "app" else 0
        if limit and limit > 0:
            items = items[:int(limit)]
        return {"entries": [e.view(running(e.id)) for e in items]}

    def lib_get(params: dict) -> dict:
        """{id} -> {entry, info: [{label, value}], actions: [{id, label}], notes: [{tag, text}],
        note, globals?, week?}; Einzelheiten in describe.py. Kennungen der Aktionen: launch, focus,
        close, settings, exe, art, art-fetch, rename (E90), info, saves (E63, Haken in wiring_saves),
        favorite | unfavorite (E99, vor hide), hide, unhide, delete. week: bei Spielen die Sekunden
        der laufenden Kalenderwoche aus dem Sitzungsverlauf (E99)."""
        entry = need(params)
        is_running = running(entry.id)
        env: dict = {}
        if library.describe_env is not None:
            try:
                env = library.describe_env() or {}
            except Exception:
                _log.exception("describe_env")
        out = {"entry": entry.view(is_running)}
        env = {**env, "gone": library.files_gone(entry)}
        out.update(describe.detail(entry, is_running, env))
        if entry.kind == "game":
            _favorite_action(entry, out)
            out["week"] = library.week_playtime(entry.id)
        # Zeiger-Modus (E97), auch über „Automatisch“: Die Spieleinstellungen bieten dann
        # „Sprungstellen zurücksetzen“ an (E99)
        try:
            from ..launch.spec import pad_mode
            out["pointer"] = pad_mode(entry) == "cards"
        except Exception:
            _log.exception("pad_mode %s", entry.id)
        for hook in list(library.detail_hooks):
            try:
                hook(entry, out)
            except Exception:
                _log.exception("detail_hook %r", hook)
        return out

    def lib_set_option(params: dict) -> dict:
        from ..rpc import RpcError
        entry = need(params)
        key = str(params.get("key"))
        if key not in OPTION_DEFAULTS:
            raise RpcError("Diese Einstellung gibt es nicht.", "BadOption")
        try:
            value = check_option(key, params.get("value"))
            extra = validators.get(key)
            if extra is not None:
                value = extra(value)
        except ValueError as e:
            raise RpcError(str(e) or "Dieser Wert passt nicht.", "BadValue") from None
        try:
            library.set_option(entry.id, key, value)
        except KeyError:
            raise RpcError("Diese Einstellung gibt es nicht.", "BadOption") from None
        return {"entry": library.get(entry.id).view(running(entry.id))}

    def lib_hide(params: dict) -> dict:
        entry = need(params)
        library.set_hidden(entry.id, bool(params.get("hidden", True)))
        return {}

    def lib_rename(params: dict) -> dict:
        """{id, title} -> {entry}. Leerer Titel: wieder der Name der Quelle (E90)."""
        from ..rpc import RpcError
        entry = need(params)
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            raise RpcError("Hier wird ein Text erwartet.", "BadValue")
        library.set_title(entry.id, title or "")
        return {"entry": library.get(entry.id).view(running(entry.id))}

    def lib_set_favorite(params: dict) -> dict:
        """{id, favorite: bool} -> {entry} (E99). In Steam wird dabei nichts geändert."""
        from ..rpc import RpcError
        entry = need(params)
        value = params.get("favorite", True)
        if not isinstance(value, bool):
            raise RpcError("Hier geht nur Ein oder Aus.", "BadValue")
        library.set_favorite(entry.id, value)
        return {"entry": library.get(entry.id).view(running(entry.id))}

    def lib_forget(params: dict) -> dict:
        """{id} -> {ok, message}: „Aus der Liste entfernen“ für Spiele, deren Ordner fehlt (E91)."""
        entry = need(params)
        if not library.forget(entry.id):
            return {"ok": False, "message": "„%s“ ist noch vorhanden. Zum Entfernen „Deinstallieren“ oder "
                                            "„Ausblenden“ nehmen." % entry.title}
        return {"ok": True, "message": "„%s“ wurde aus der Liste entfernt." % entry.title}

    def lib_rescan(params: dict) -> Future:
        return library.rescan()

    def lib_exe_candidates(params: dict) -> dict:
        # Die Auswahl liefert später der Archiv-Installer; bis dahin, was der Eintrag mitbringt
        entry = need(params)
        found = entry.detail.get("exe_candidates")
        return {"candidates": list(found) if isinstance(found, list) else []}

    rpc.register("lib.list", lib_list)
    rpc.register("lib.get", lib_get)
    rpc.register("lib.setOption", lib_set_option)
    rpc.register("lib.hide", lib_hide)
    rpc.register("lib.rename", lib_rename)
    rpc.register("lib.setFavorite", lib_set_favorite)
    rpc.register("lib.forget", lib_forget)
    rpc.register("lib.rescan", lib_rescan)
    rpc.register("lib.exeCandidates", lib_exe_candidates)
    library.on_change(lambda: rpc.emit("lib.changed", {}))
