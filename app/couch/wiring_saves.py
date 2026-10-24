"""Spielstände am Kern (Bauplan E63): RPC saves.*, ein Haken für lib.get und die Automatik nach
dem Spielen (E99).

Suchen, Sichern und Wiederherstellen macht saves.py, immer im Arbeits-Thread (wiring._bg). Der
Haken für lib.get läuft in der Hauptschleife und fasst die Platte nicht an: Er trägt den Menüpunkt
„Spielstände“ und den vollen Pfad aus der letzten Suche ein (Zwischenspeicher je Eintrag) und
stößt sonst eine Suche an. Die Seite holt alles Weitere mit saves.info.

Wo gesucht wird, hängt am Start des Eintrags: der Prefix, mit dem das Spiel wirklich läuft (Lutris-
Weg E55: der Lutris-Prefix; sonst der von prefix.decide), bei Steam compatdata/<AppID>/pfx und
userdata/<Konto>/<AppID>, bei Linux-Spielen der Home-Ordner. Switch und Wii U (E99): Edens bzw.
Cemus Spielstand-Ordner zur TitleID des Eintrags (detail.title_id) oder zur gelernten (nach einer
Sitzung aus Edens bzw. Cemus Ordnern, gemerkt in <state>/saves-learned.json). Zurückholen in
Emulator-Ordner ist gesperrt, bis der Nutzer es freigibt (EMU_RESTORE, SAFETY 13).

Automatik (E99): Endet ein Spiel (Ereignis "exited" der AppManager, nach mindestens AUTO_MIN_S
gespielt, Einstellung saves.auto), sichert ein eigener Thread mit nice 19 und Platten-Priorität
„idle“ den Ordner als „auto-<Datum>“, wenn sich seit der letzten Sicherung etwas geändert hat.
Solange ein Spiel vorn ist, hält er zwischen zwei Dateien an; startet dasselbe Spiel wieder, bricht
er ab und sichert nach dieser Sitzung (keine Mischung aus altem und neuem Stand). Es bleiben die
letzten saves.keep automatischen Sicherungen je Eintrag, ältere kommen in den Papierkorb. Eine
Mitteilung gibt es nur, wenn es nicht klappt. Die TitleID lernt der Arbeits-Thread gleich beim
Ende der Sitzung, nicht erst hinter wartenden Sicherungen.
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any

from . import files, jsontext, log, paths, saves, system
from .library.model import source_title
from .settings.games import KEEP_CHOICES, SAVES_DEFAULTS   # eine Tabelle für Auswahl und Vorgabe (E22)

_log = log.get("wiring.saves")

FRESH_S = 120.0          # so lange gilt eine Suche für lib.get
LABEL = "Spielstände"
STEAM_USERDATA_BONUS = 5
EMU_BONUS = 8            # Ordner zur TitleID: gilt auch ohne Titelwort im Pfad (MIN_SCORE 6)
EMU_SOURCES = ("switch", "wiiu")
GAME_KINDS = ("game", "emulator")          # LaunchSpec.kind, die als Spiel zählen (wie shell.py)
AUTO_MIN_S = 120.0       # kürzer gespielt: keine automatische Sicherung
AUTO_MAX_BYTES = 1 << 30                   # größere Stände nur von Hand (Platz, Plattenzeit)
AUTO_RESERVE_BYTES = 1 << 30               # so viel bleibt auf dem Laufwerk der Sicherungen frei
SHUTDOWN_WAIT_S = 2.0    # so lange wartet das Ende des Home auf eine laufende Sicherung
# Zurückholen in Edens/Cemus Spielstand-Ordner schreibt in Emulator-Dateien (SAFETY 13: nur lesen).
# Erst nach ausdrücklicher Freigabe hier auf True stellen.
EMU_RESTORE = False
LEARNED_FILE = "saves-learned.json"
TEXT_GONE = "Diesen Eintrag gibt es nicht."
TEXT_BUSY = "Gerade wird schon gesichert oder wiederhergestellt."
TEXT_RUNNING = "Schließe zuerst das Spiel, dann lässt sich der Spielstand zurückholen."
TEXT_NONE = "Kein Spielstand-Ordner bekannt. Wähle ihn im Datei-Explorer."
TEXT_FAILED = saves.TEXT_FAILED
TEXT_EMU_RESTORE = ("Spielstände von Emulatoren holt der PC5 Launcher noch nicht selbst zurück. Die Sicherung "
                    "liegt im Sicherungsordner, du kannst sie im Datei-Explorer öffnen.")
TEXT_EMU_OTHER = ("In diesen Ordner holt der PC5 Launcher nichts zurück: Er enthält Dateien eines Emulators. "
                  "Wähle den Spielstand-Ordner des Spiels selbst.")
TEXT_NO_ROOM = ("Auf dem Laufwerk der Sicherungen ist zu wenig Platz frei. Auch ältere Sicherungen im "
                "Papierkorb belegen Platz.")
TEXT_EMU_LEARN = "Spiel es einmal, dann erkennt der PC5 Launcher den Ordner von selbst."
TEXT_AUTO_FAILED = "Spielstand nicht gesichert"


class _Stopped(Exception):
    """Das Home endet: die laufende automatische Sicherung bricht ab (saves räumt die halbe ZIP weg)."""


class _Restarted(Exception):
    """Dasselbe Spiel läuft wieder: Die Sicherung bricht ab, statt alte und neue Dateien zu mischen;
    sie kommt nach dieser Sitzung nach (E99)."""


class _Part:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.found: dict[str, dict] = {}     # Eintrag -> {"at", "folder", "chosen", "prefix", "candidates"}
        self.pending: set[str] = set()       # Suche läuft
        self.busy: set[str] = set()          # Sicherung oder Wiederherstellung läuft
        self.learned: dict | None = None     # Eintrag -> {"title_id", "at"} (saves-learned.json)
        self.too_big: set[str] = set()       # schon gemeldet: zu groß für die Automatik
        self.auto: _Auto | None = None


def supported(entry: Any) -> bool:
    return getattr(entry, "kind", "") == "game"


def _first_dir(paths_: list) -> str:
    named = [p for p in paths_ if isinstance(p, str) and p]
    return next((p for p in named if os.path.isdir(p)), named[0] if named else "")


def _home(wiring: Any) -> str:
    return getattr(getattr(wiring, "sources", None), "home", None) or paths.home()


def _cfg(wiring: Any, key: str, default: Any) -> Any:
    config = getattr(wiring.app, "config", None)
    return config.get(key, default) if config is not None else default


def _keep(wiring: Any) -> int:
    keep = _cfg(wiring, "saves.keep", SAVES_DEFAULTS["saves.keep"])
    return keep if keep in KEEP_CHOICES else SAVES_DEFAULTS["saves.keep"]


# ---------------------------------------------------------------- gelernte TitleIDs (E99)
def learned_path() -> str:
    return os.path.join(paths.state_dir(), LEARNED_FILE)


def _learned(wiring: Any) -> dict:
    """Gelernte TitleIDs (unter p.lock rufen)."""
    p = part(wiring)
    if p.learned is None:
        p.learned = {}
        try:
            with open(learned_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                p.learned = {str(k): v for k, v in data["entries"].items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            _log.warning("Gelernte Spielstand-Ordner nicht lesbar: %s", e)
    return p.learned


def title_id(wiring: Any, entry: Any) -> str:
    """TitleID eines Emulator-Spiels: aus dem Eintrag, sonst die gelernte, sonst ""."""
    tid = str((entry.detail or {}).get("title_id") or "")
    if tid:
        return tid.upper()
    with part(wiring).lock:
        item = _learned(wiring).get(entry.id) or {}
    return str(item.get("title_id") or "").upper()


def learn(wiring: Any, entry: Any, started: float, ended: float) -> str:
    """Nach einer Sitzung ohne bekannte TitleID: die eine, die sich in der Zeit geändert hat,
    merken. Liest nur die Ordner der Emulatoren; geschrieben wird nur die eigene Datei."""
    if entry.source not in EMU_SOURCES or (entry.detail or {}).get("title_id"):
        return ""
    tid = saves.learn_title(entry.source, _home(wiring), started, ended)
    if not tid:
        _log.info("Spielstände %s: keine eindeutige TitleID nach dieser Sitzung", entry.id)
        return ""
    p = part(wiring)
    with p.lock:
        known = _learned(wiring)
        if (known.get(entry.id) or {}).get("title_id") == tid:
            return tid
        known[entry.id] = {"title_id": tid, "at": int(time.time())}
        text = jsontext.dumps({"version": 1, "entries": known}, indent=1, sort_keys=True) + "\n"
        p.found.pop(entry.id, None)
    try:
        system.write_atomic(learned_path(), text)
    except OSError as e:
        _log.warning("Gelernte TitleID nicht gespeichert: %s", e)
    _log.info("Spielstände %s: TitleID %s gelernt", entry.id, tid)
    return tid


def emu_refuse(wiring: Any):
    """refuse() für saves.restore (SAFETY 13). Gesperrt ist jedes Ziel in einem Datenort von Eden
    oder Cemu (NAND, mlc01, App-Ordner; auch die, die es gerade nicht gibt) und jedes, das einen
    davon enthält: Ein von Hand gewählter Ordner darüber (nand/user, mlc01, ~/.local/share) legte
    sonst beim Zurückholen die Stände aller Spiele samt Emulator in den Papierkorb. Mit EMU_RESTORE
    geht es nur in genau den Spielstand-Ordner eines Spiels."""
    def refuse(folder: str) -> str:
        home = _home(wiring)
        roots = saves.emulator_data_roots(home)
        holds = any(paths.is_under(r, [folder]) for r in roots)
        inside = paths.is_under(folder, roots)
        if not holds and not inside:
            return ""
        if not EMU_RESTORE:
            return TEXT_EMU_RESTORE
        return "" if not holds and saves.emulator_title_dir(folder, home) else TEXT_EMU_OTHER
    return refuse


def places(entry: Any, wiring: Any) -> dict:
    """Wo gesucht wird (Arbeits-Thread, ein paar stat): {"prefix", "home", "game_dir", "extra"}."""
    from .launch import builders, lutris_way
    from .library import describe
    d = entry.detail
    src = getattr(wiring, "sources", None)
    home = _home(wiring)
    out: dict = {"prefix": "", "home": "", "game_dir": describe.location(entry), "extra": []}
    if describe.windows_game(entry):
        if entry.source == "steam":
            if not describe.is_shortcut(entry) and d.get("library") and d.get("appid"):
                out["prefix"] = os.path.join(str(d["library"]), "steamapps", "compatdata", str(d["appid"]), "pfx")
        elif lutris_way.wanted(entry):
            out["prefix"] = str(d.get("prefix") or "")
        else:
            info = None
            if getattr(wiring, "launch", None) is not None:
                try:
                    info = builders.prefix_info(entry, wiring.launch)
                except Exception:
                    _log.exception("Prefix von %s", entry.id)
            saved = d.get("saves") if isinstance(d.get("saves"), dict) else {}
            # Steht die Übernahme aus Lutris noch aus, liegen die Stände noch im Lutris-Prefix
            out["prefix"] = _first_dir([(info or {}).get("path"), saved.get("path"), d.get("prefix")])
    elif entry.source in ("steam", "native", "local"):
        out["home"] = home
    elif entry.source in EMU_SOURCES:
        # E99: Edens bzw. Cemus Ordner zur TitleID; Eden mit mehreren Profilen liefert mehrere
        tid = title_id(wiring, entry)
        for folder in saves.emulator_folders(entry.source, home, tid) if tid else []:
            out["extra"].append((folder, EMU_BONUS))
    if entry.source == "steam" and d.get("appid") and not describe.is_shortcut(entry):
        root = getattr(src, "steam_root", None) or os.path.join(home, ".local/share/Steam")
        base = os.path.join(root, "userdata")
        try:
            accounts = [n for n in sorted(os.listdir(base)) if n.isdigit() and n != "0"]
        except OSError:
            accounts = []
        for acct in accounts:
            folder = os.path.join(base, acct, str(d["appid"]))
            remote = os.path.join(folder, "remote")
            out["extra"].append((remote if os.path.isdir(remote) else folder, STEAM_USERDATA_BONUS))
    return out


def scan(wiring: Any, entry: Any) -> dict:
    """Sucht neu und merkt sich das Ergebnis (Arbeits-Thread)."""
    where = places(entry, wiring)
    found = saves.find(source_title(entry), where["prefix"], where["game_dir"], where["home"], where["extra"])
    chosen = str(entry.option("saves") or "")
    top = saves.best(found, chosen)
    result = {"at": time.monotonic(), "folder": top["path"] if top else "", "current": top,
              "chosen": bool(chosen) and bool(top) and top["path"] == chosen, "prefix": where["prefix"],
              "candidates": found}
    p = part(wiring)
    with p.lock:
        p.found[entry.id] = result
        p.pending.discard(entry.id)
    return result


def part(wiring: Any) -> _Part:
    p = getattr(wiring, "_saves_part", None)
    if p is None:
        p = wiring._saves_part = _Part()
    return p


# ---------------------------------------------------------------- lib.get (Hauptschleife)
def _hook(wiring: Any):
    def hook(entry: Any, out: dict) -> None:
        if not supported(entry):
            return
        acts = out.get("actions")
        if isinstance(acts, list):
            at = next((n for n, a in enumerate(acts) if isinstance(a, dict) and a.get("id") == "info"), len(acts) - 1)
            acts.insert(at + 1, {"id": "saves", "label": LABEL})
        p = part(wiring)
        with p.lock:
            found = p.found.get(entry.id)
            start = (found is None or time.monotonic() - found["at"] > FRESH_S) and entry.id not in p.pending
            if start:
                p.pending.add(entry.id)
        if start:
            wiring._bg(_scan_quiet, wiring, entry)
        rows = out.get("info")
        if not isinstance(rows, list):
            return
        value = ""
        if found and found["folder"]:
            value = found["folder"]
        elif found and found["prefix"]:
            value = "kein Ordner erkannt (Prefix: %s)" % found["prefix"]
        if not value:
            return                       # bleibt, was describe.saves_text sagt
        row = next((r for r in rows if isinstance(r, dict) and r.get("label") == LABEL), None)
        if row is not None:
            row["value"] = value
            return
        # Ans Ende (nach „Speicherort“): Die ersten drei Zeilen stehen als Karten im Hub
        at = next((n + 1 for n, r in enumerate(rows) if isinstance(r, dict) and r.get("label") == "Speicherort"), len(rows))
        rows.insert(at, {"label": LABEL, "value": value})
    return hook


def _scan_quiet(wiring: Any, entry: Any) -> None:
    try:
        scan(wiring, entry)
    except Exception:
        _log.exception("Spielstände suchen: %s", entry.id)
        with part(wiring).lock:
            part(wiring).pending.discard(entry.id)


# ---------------------------------------------------------------- Automatik nach dem Spielen (E99)
def lower_priority() -> None:
    """Nur der eigene Thread: nice 19 und Platten-Priorität „idle“ (ionice wirkt mit einer
    Thread-ID nur auf diesen Thread). Fehlt ionice, folgt die Platten-Priorität dem nice-Wert."""
    tid = threading.get_native_id()
    try:
        os.setpriority(os.PRIO_PROCESS, tid, 19)
    except (OSError, AttributeError):
        pass
    tool = shutil.which("ionice")
    if tool:
        try:
            subprocess.run([tool, "-c", "3", "-p", str(tid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


class _Auto:
    """Eigener Thread für die Automatik: Aufträge nacheinander; er endet, wenn nichts mehr ansteht.
    free ist gesetzt, solange kein Spiel vorn ist; pause() wartet darauf."""

    def __init__(self, run_job) -> None:
        self._run_job = run_job
        self._cond = threading.Condition()
        self._jobs: collections.deque = collections.deque()
        self._thread: threading.Thread | None = None
        self.free = threading.Event()
        self.free.set()
        self.stopped = False
        self.current = ""                    # Titel der Sicherung, die gerade läuft
        self._starts: dict[str, int] = {}    # Eintrag -> Zahl der Starts (Ereignis "started")
        self.owed: set[str] = set()          # abgebrochen, weil das Spiel wieder lief: nachholen

    def started(self, entry_id: str) -> None:
        with self._cond:
            self._starts[entry_id] = self._starts.get(entry_id, 0) + 1

    def generation(self, entry_id: str) -> int:
        with self._cond:
            return self._starts.get(entry_id, 0)

    def submit(self, job: dict) -> None:
        with self._cond:
            if self.stopped:
                return
            self._jobs.append(job)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="couch-saves-auto", daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        lower_priority()
        while True:
            with self._cond:
                if not self._jobs or self.stopped:
                    self._jobs.clear()
                    self._thread = None
                    self._cond.notify_all()
                    return
                job = self._jobs.popleft()
            try:
                self._run_job(job)
            except _Stopped:
                pass
            except Exception:
                _log.exception("Automatische Sicherung: %s", job.get("id"))

    def pause(self) -> None:
        while not self.free.wait(1.0):
            if self.stopped:
                raise _Stopped()
        if self.stopped:
            raise _Stopped()

    def stop(self, wait: float = 0.0) -> None:
        """Ende des Home. Wartet kurz, bis die laufende Sicherung an der nächsten Datei abbricht
        und ihre halbe ZIP wegräumt; dauert eine einzelne Datei länger, räumt die nächste
        automatische Sicherung den Rest weg (saves.stale_parts)."""
        self.stopped = True
        self.free.set()
        if wait > 0 and self._thread is not threading.current_thread():
            self.wait_idle(wait)

    def wait_idle(self, timeout: float) -> bool:
        """Wartet, bis alle Aufträge durch sind (Ende des Home, Prüfläufe)."""
        end = time.monotonic() + timeout
        with self._cond:
            while self._thread is not None:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True


def _game_in_front(app: Any) -> bool:
    """Hauptschleife. Eine unbekannte App-ID vorn (Fenster, das Steam selbst gestartet hat) zählt
    als Spiel, solange eins läuft; sonst wartete die Sicherung auf eine veraltete ID ewig."""
    front = getattr(getattr(app, "focus", None), "foreground", None)
    if front is None:
        return False
    procs = getattr(app, "procs", None)
    find = getattr(procs, "by_app_id", None)
    running = find(front) if callable(find) else None
    if running is not None:
        return getattr(getattr(running, "spec", None), "kind", "game") in GAME_KINDS
    every = getattr(procs, "running", None)
    apps = every() if callable(every) else []
    return any(getattr(getattr(a, "spec", None), "kind", "game") in GAME_KINDS for a in apps)


def _gb(n: int) -> str:
    return ("%.1f GB" % (n / (1024 ** 3))).replace(".", ",")


def _free_bytes(path: str) -> int | None:
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def auto_backup(wiring: Any, entry: Any, pause=None) -> dict | None:
    """Eine automatische Sicherung (Thread der Automatik). None = nichts zu tun (kein Ordner,
    nichts geändert, zu groß). Fehlschläge werfen ValueError/OSError."""
    title = source_title(entry)
    folder = scan(wiring, entry)["folder"]
    if not folder:
        _log.info("Automatische Sicherung %s: kein Spielstand-Ordner bekannt", entry.id)
        return None
    have = saves.contents(folder)
    if not have["files"]:
        return None
    # Nur Sicherungen dieses Eintrags mit lesbarem Manifest zählen, und zwar ab ihrem Beginn (E99)
    last = saves.last_backup(title, entry.id)
    if last and have["mtime"] <= last:
        _log.info("Automatische Sicherung %s: nichts geändert seit %s", entry.id,
                  time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)))
        return None
    p = part(wiring)
    if have["size"] > AUTO_MAX_BYTES:
        _log.info("Automatische Sicherung %s: %s sind zu groß", entry.id, _gb(have["size"]))
        with p.lock:
            told = entry.id in p.too_big
            p.too_big.add(entry.id)
        if not told:
            _tell(wiring, "„%s“: Der Spielstand ist mit %s zu groß für die automatische Sicherung. "
                          "„Jetzt sichern“ unter Spielstände geht weiterhin." % (entry.title, _gb(have["size"])))
        return None
    # Halbe ZIP-Dateien vom letzten Ende des Home (oder Absturz): eigene Dateien, in den Papierkorb
    for stale in saves.stale_parts(title):
        try:
            files.gio_trash(stale)
            _log.info("Halbe Sicherung in den Papierkorb: %s", stale)
        except (OSError, ValueError) as e:
            _log.warning("Halbe Sicherung bleibt: %s (%s)", stale, e)
    free = _free_bytes(saves.backup_root())
    if free is not None and free < have["size"] + AUTO_RESERVE_BYTES:
        raise ValueError(TEXT_NO_ROOM)
    made = saves.make_backup(title, entry.id, folder, auto=True, pause=pause)
    gone = saves.prune_auto(title, _keep(wiring), files.gio_trash, entry.id)
    _log.info("Automatisch gesichert: %s -> %s (%d Dateien; %d alte in den Papierkorb)",
              folder, made["path"], made["files"], len(gone))
    return made


def _tell(wiring: Any, body: str) -> None:
    app = wiring.app
    notify = getattr(app, "notify", None)
    if notify is None:
        return
    wiring.main.post(lambda: notify.push(TEXT_AUTO_FAILED, body, icon="warn"))


def _run_auto(wiring: Any, job: dict) -> None:
    """Ein Auftrag der Automatik: eine Sicherung (das Lernen der TitleID lief schon beim Ende)."""
    entry, auto = job["entry"], part(wiring).auto
    p = part(wiring)

    def pause() -> None:
        auto.pause()                         # hält an, solange ein Spiel vorn ist
        if auto.generation(entry.id) != job["gen"]:
            raise _Restarted()               # dasselbe Spiel lief inzwischen wieder

    took = False
    try:
        pause()                              # nicht anfangen, solange ein Spiel vorn ist
        # Erst jetzt gilt sie als laufend: Beim Warten ist nichts halb geschrieben, und „Jetzt
        # sichern“ und „Zurückholen“ bleiben solange frei (E99)
        with p.lock:
            if entry.id in p.busy:
                _log.info("Automatische Sicherung %s: läuft schon eine von Hand", entry.id)
                return
            p.busy.add(entry.id)
            took = True
        auto.owed.discard(entry.id)
        auto.current = entry.title
        auto_backup(wiring, entry, pause=pause)
    except _Stopped:
        raise
    except _Restarted:
        auto.owed.add(entry.id)
        _log.info("Automatische Sicherung %s: das Spiel läuft wieder; kommt nach dieser Sitzung", entry.id)
    except (OSError, ValueError) as e:
        _log.warning("Automatische Sicherung %s: %s", entry.id, e)
        _tell(wiring, "„%s“: %s" % (entry.title, saves.reason(e)))
    except Exception:
        _log.exception("Automatische Sicherung %s", entry.id)
        _tell(wiring, "„%s“: %s" % (entry.title, TEXT_FAILED))
    finally:
        if took:
            auto.current = ""
            with p.lock:
                p.busy.discard(entry.id)
                p.found.pop(entry.id, None)


def _learn_then(wiring: Any, entry: Any, started: float, ended: float, job: dict | None) -> None:
    """Arbeits-Thread, gleich beim Ende der Sitzung: TitleID lernen, dann erst die Sicherung
    einreihen (sie braucht den gelernten Ordner). Nicht hinter wartenden Sicherungen: Später
    hätte sich womöglich schon ein anderes Spiel geändert (E99)."""
    try:
        learn(wiring, entry, started, ended)
    except Exception:
        _log.exception("TitleID lernen: %s", entry.id)
    auto = part(wiring).auto
    if job is not None and auto is not None:
        auto.submit(job)


def _watch(wiring: Any) -> None:
    """Hängt die Automatik an die Ereignisse der AppManager und des Vordergrunds (kein Eingriff in
    die Shell). Läuft in extend(), also in der Hauptschleife."""
    app = wiring.app
    p = part(wiring)
    auto = p.auto = _Auto(lambda job: _run_auto(wiring, job))

    def front_changed() -> None:
        try:
            game = _game_in_front(app)
        except Exception:
            _log.exception("Vordergrund prüfen")
            game = False
        if game:
            auto.free.clear()
        else:
            auto.free.set()

    def proc_changed(running: Any, what: str) -> None:
        if what not in ("started", "exited"):
            return
        entry = app.library.get(running.entry_id) if getattr(app, "library", None) is not None else None
        if entry is None or not supported(entry):
            return
        if getattr(getattr(running, "spec", None), "kind", "game") not in GAME_KINDS:
            return
        if what == "started":
            auto.started(entry.id)           # bricht eine laufende Sicherung dieses Spiels ab
            return
        ended = time.time()
        started = float(getattr(running, "started", 0) or 0)
        learn_now = entry.source in EMU_SOURCES and not (entry.detail or {}).get("title_id")
        # Eine wegen Neustart abgebrochene Sicherung kommt auch nach einer kurzen Sitzung nach
        long_enough = float(getattr(running, "runtime", 0) or 0) >= AUTO_MIN_S or entry.id in auto.owed
        backup = bool(_cfg(wiring, "saves.auto", SAVES_DEFAULTS["saves.auto"])) and long_enough
        job = {"id": entry.id, "entry": entry, "gen": auto.generation(entry.id)} if backup else None
        if job is not None:
            front_changed()
        if learn_now:
            wiring._bg(_learn_then, wiring, entry, started, ended, job)
        elif job is not None:
            auto.submit(job)

    procs_on = getattr(getattr(app, "procs", None), "on_change", None)
    if callable(procs_on):
        procs_on(proc_changed)
    focus_on = getattr(getattr(app, "focus", None), "on_change", None)
    if callable(focus_on):
        focus_on(front_changed)
    if isinstance(getattr(wiring, "shutdown_hooks", None), list):
        wiring.shutdown_hooks.append(lambda: auto.stop(wait=SHUTDOWN_WAIT_S))
    if isinstance(getattr(wiring, "busy_checks", None), list):
        wiring.busy_checks.append(
            lambda: "Der Spielstand von „%s“ wird gerade gesichert." % auto.current if auto.current else None)


# ---------------------------------------------------------------- RPC
def extend(wiring: Any) -> None:
    app = wiring.app
    rpc = app.rpc
    p = part(wiring)

    hooks = getattr(getattr(app, "library", None), "detail_hooks", None)
    if isinstance(hooks, list):
        hooks.append(_hook(wiring))
    _watch(wiring)

    def entry_of(params: dict) -> Any:
        entry_id = str((params or {}).get("id") or "")
        entry = wiring.main.call(app.library.get, entry_id) if entry_id else None
        return entry if entry is not None and supported(entry) else None

    def running(entry_id: str) -> bool:
        return wiring.main.call(lambda: app.procs.by_entry(entry_id) is not None)

    def info(params: dict) -> dict:
        entry = entry_of(params)
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}
        res = scan(wiring, entry)
        note = ""
        if not res["folder"] and entry.source in EMU_SOURCES and not title_id(wiring, entry):
            note = TEXT_EMU_LEARN
        blocked = emu_refuse(wiring)(res["folder"]) if res["folder"] else ""
        return {"ok": True, "title": entry.title, "folder": res["folder"], "current": res["current"],
                "chosen": res["chosen"], "prefix": res["prefix"], "candidates": res["candidates"],
                "backups": saves.list_backups(source_title(entry)), "backupDir": saves.backup_dir(source_title(entry)),
                "running": running(entry.id), "note": note, "restoreBlocked": blocked,
                "auto": bool(_cfg(wiring, "saves.auto", SAVES_DEFAULTS["saves.auto"])), "keep": _keep(wiring)}

    def guarded(entry: Any, fn) -> dict:
        with p.lock:
            if entry.id in p.busy:
                return {"ok": False, "message": TEXT_BUSY}
            p.busy.add(entry.id)
        try:
            return fn()
        except ValueError as e:
            _log.warning("Spielstände %s: %s", entry.id, e)
            return {"ok": False, "message": str(e)}
        except OSError as e:
            # deutsch an die Seite (volle Platte: kein englischer Satz mit technischem Pfad, E99)
            _log.warning("Spielstände %s: %s", entry.id, e)
            return {"ok": False, "message": saves.reason(e)}
        except Exception:
            # nie einen englischen Satz an die Seite (E99): Einzelheiten ins Protokoll
            _log.exception("Spielstände %s", entry.id)
            return {"ok": False, "message": TEXT_FAILED}
        finally:
            with p.lock:
                p.busy.discard(entry.id)
                p.found.pop(entry.id, None)

    def backup(params: dict) -> dict:
        entry = entry_of(params)
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}

        def run() -> dict:
            folder = scan(wiring, entry)["folder"]
            if not folder:
                return {"ok": False, "message": TEXT_NONE}
            made = saves.make_backup(source_title(entry), entry.id, folder)
            _log.info("Spielstand gesichert: %s -> %s (%d Dateien)", folder, made["path"], made["files"])
            return {"ok": True, "backup": made, "message": "Gesichert: %d Dateien in „%s“."
                    % (made["files"], os.path.basename(made["path"]))}
        return guarded(entry, run)

    def known_backup(entry: Any, path: str) -> bool:
        return any(b["path"] == path for b in saves.list_backups(source_title(entry)))

    def restore(params: dict) -> dict:
        entry = entry_of(params)
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}
        path = str(params.get("path") or "")
        if running(entry.id):
            return {"ok": False, "message": TEXT_RUNNING}
        if not known_backup(entry, path):
            return {"ok": False, "message": "Diese Sicherung gibt es nicht."}

        def run() -> dict:
            current = scan(wiring, entry)["folder"]
            if current and entry.source in EMU_SOURCES:
                # Eden: In jedem Profil heißt der Ordner gleich (TitleID), und die Suche nimmt das
                # zuletzt geänderte. Eine Sicherung aus Profil A ginge sonst still nach Profil B;
                # gibt es den Ort von damals noch, gilt er (E99, erst mit EMU_RESTORE von Belang)
                then = str(saves.read_manifest(path).get("folder") or "")
                if then and os.path.isdir(then) and \
                        os.path.dirname(os.path.realpath(then)) != os.path.dirname(os.path.realpath(current)):
                    current = ""
            done = saves.restore(path, source_title(entry), entry.id, trash=files.gio_trash,
                                 current=current, refuse=emu_refuse(wiring))
            _log.info("Spielstand zurückgeholt: %s -> %s (vorher gesichert: %s)", path, done["folder"], done["safety"])
            text = "Der Spielstand ist zurück."
            if done["safety"]:
                text += " Der vorige Stand liegt als Sicherung „%s“ bereit." % os.path.basename(done["safety"])[:-4]
            return {"ok": True, "folder": done["folder"], "message": text}
        return guarded(entry, run)

    def remove(params: dict) -> dict:
        entry = entry_of(params)
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}
        path = str(params.get("path") or "")
        if not known_backup(entry, path):
            return {"ok": False, "message": "Diese Sicherung gibt es nicht."}

        def run() -> dict:
            files.gio_trash(path)
            return {"ok": True, "message": "Die Sicherung liegt im Papierkorb."}
        return guarded(entry, run)

    def choose(params: dict) -> dict:
        entry = entry_of(params)
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}
        path = str(params.get("path") or "")
        if path:
            path = os.path.normpath(path)
            if not files.allowed(path) or not os.path.isdir(path):
                return {"ok": False, "message": "Diesen Ordner kann der PC5 Launcher nicht verwenden."}
        wiring.main.call(app.library.set_option, entry.id, "saves", path)
        with p.lock:
            p.found.pop(entry.id, None)
        res = scan(wiring, entry_of(params) or entry)
        return {"ok": True, "folder": res["folder"],
                "message": "Spielstände liegen jetzt in „%s“." % path if path else "Der Ordner wird wieder selbst gesucht."}

    for name, fn in (("saves.info", info), ("saves.backup", backup), ("saves.restore", restore),
                     ("saves.remove", remove), ("saves.choose", choose)):
        rpc.register(name, lambda params, fn=fn: wiring._bg(fn, params or {}))
