"""Quellen der Bibliothek: Kontext, gemeinsame Helfer, Register und Sammellauf.

Jede Quelle ist eine Funktion ``load(ctx) -> list[Entry]``. Sie liest nur, blockiert (Platte,
bei den Apps notfalls ein ``flatpak info``) und gehört deshalb in einen Thread. Probleme landen
als deutsche Sätze in ``ctx.warnings``; eine Quelle wirft nie.

    from couch.library import sources
    ctx = sources.SourceContext(data_dir=paths.data_dir, cache_dir=paths.cache_dir,
                                run_read=system.run_read)
    entries, warnings, timings = sources.load_all(ctx)          # blockierend, alles auf einmal
    for name, fn in sources.bound_sources(ctx).items():         # oder je Quelle: fn() -> list[Entry]
        register_source(name, fn)
    for name, fn in sources.bound_derived(ctx).items():         # danach: Quellen, die auf den
        register_source(name, fn)                               # anderen aufbauen (Verknüpfungen)

Abgeleitete Quellen (``derived_sources``): "shortcuts" macht aus Steam-Verknüpfungen, die zu
keinem anderen Eintrag gehören, eigene Einträge ``steam:shortcut:<appid>`` (Bauplan E25). Sie
läuft nach den anderen und sieht deren Ergebnis des laufenden Laufs (``ctx.last``).

Befehl zum Nachsehen (liest nur, schreibt auch keinen Zwischenspeicher):

    PYTHONPATH=app python3 -m couch.library.sources --table

Schutz für Tests: Ohne ausdrückliches ``home`` liest der Kontext die echte Bibliothek nur, wenn
weder COUCH_FIXTURES noch COUCH_TEST_ROOT gesetzt ist. Sonst zeigt alles in einen Attrappen-Baum
(Aufbau siehe ``SourceContext``); so sieht kein Testlauf versehentlich die echten Spiele des Nutzers.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from typing import Callable

from .model import Entry

# E105: "roms" (ROM-Ordner ohne SRM) direkt nach "emulators": Bei doppelter ID gewinnt die frühere Quelle
SOURCE_NAMES = ("steam", "lutris", "emulators", "roms", "apps", "local")
DERIVED_NAMES = ("shortcuts",)
# E104: Ordner von Lutris als Flatpak unter dem persönlichen Ordner (nur gelesen)
FLATPAK_LUTRIS = ".var/app/net.lutris.Lutris"


# ---------------------------------------------------------------- Helfer für alle Quellen

_MARKS = re.compile(r"[™®©℠]")


def clean_text(value, fallback: str = "") -> str:
    """Titel für die Oberfläche: eine Zeile, keine Steuerzeichen, keine halben Unicode-Paare.

    Dateinamen mit kaputter Kodierung kommen aus os.listdir als Ersatzzeichen (surrogateescape)
    an; die ließen später json.dumps für die Seite scheitern.
    """
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = value.encode("utf-8", "replace").decode("utf-8", "replace")
    value = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or fallback


def sort_title(title: str) -> str:
    """Sortierschlüssel: ohne Marken-Zeichen, ohne führendes „The“, ohne Akzente, klein."""
    t = unicodedata.normalize("NFKD", _MARKS.sub("", title))     # vorher: NFKD macht aus ™ sonst „TM“
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def title_key(title: str) -> str:
    """Vergleichsschlüssel für Titel: nur Buchstaben und Ziffern („Rocksmith® 2014“ = „rocksmith2014“)."""
    t = unicodedata.normalize("NFKD", _MARKS.sub("", title))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", t.casefold())


def err_text(exc: BaseException) -> str:
    """Kurzbeschreibung eines Fehlers für Warnungen, bewusst OHNE Dateiinhalt.

    Die Parser (vdf, yaml, json) zitieren in ihren Meldungen die kaputte Zeile; in config.vdf und
    localconfig.vdf können das Anmeldedaten sein. Deshalb nur Art und Zeilennummer.
    """
    name = type(exc).__name__
    if isinstance(exc, OSError):
        return "%s: %s" % (name, exc.strerror or "Fehler beim Lesen")
    line = getattr(exc, "lineno", None)
    mark = getattr(exc, "problem_mark", None)          # yaml
    if line is None and mark is not None:
        line = getattr(mark, "line", -1) + 1
    return "%s, Zeile %s" % (name, line) if isinstance(line, int) and line > 0 else name


def same_path(a: str, b: str) -> bool:
    """Pfadvergleich über realpath (/home ist hier ein Verweis auf /var/home)."""
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)


def expand_home(path: str, home: str) -> str:
    if path == "~":
        return home
    return os.path.join(home, path[2:]) if path.startswith("~/") else path


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def read_json(path: str):
    """JSON-Datei lesen; wirft OSError/ValueError (der Aufrufer macht daraus eine Warnung)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def write_json_atomic(path: str, data) -> bool:
    """Nur für die eigenen Zwischenspeicher unter cache_dir. Scheitert still (False)."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def default_run_read(argv, timeout: float = 10):
    """Vorgabe für run_read im eigenständigen Lauf: (returncode, stdout, stderr). Blockierend."""
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitüberschreitung"
    except OSError as e:
        return 126, "", str(e)


def no_run_read(argv, timeout: float = 10):
    """run_read für Attrappen-Kontexte: führt nichts aus und meldet „nicht gefunden“."""
    return 127, "", "kein run_read im Attrappen-Kontext"


# ---------------------------------------------------------------- Kontext

def fixtures_root() -> str | None:
    """Attrappen-Baum für Tests: COUCH_FIXTURES, sonst <COUCH_TEST_ROOT>/fixtures, sonst keiner."""
    fx = os.environ.get("COUCH_FIXTURES")
    if fx:
        return fx
    root = os.environ.get("COUCH_TEST_ROOT")
    return os.path.join(root, "fixtures") if root else None


class SourceContext:
    """Woher die Quellen lesen. Alle Werte sind einfache Pfade; None heißt Vorgabe.

    Vorgaben für den echten Rechner (weder COUCH_FIXTURES noch COUCH_TEST_ROOT gesetzt):
        home           ~
        steam_root     ~/.steam/root (aufgelöst), sonst ~/.local/share/Steam
        lutris_db      ~/.local/share/lutris/pga.db; fehlt sie, die von Lutris als Flatpak
                       (~/.var/app/net.lutris.Lutris/data/lutris/pga.db, nur gelesen, E104)
        lutris_config  ~/.config/lutris (beim Flatpak ~/.var/app/net.lutris.Lutris/config/lutris)
        srm_dir        ~/.config/steam-rom-manager/userData
        games_disk     paths.games_disk() (E104: library.games_disk, im Bestand /run/media/<nutzer>/Spiele,
                       sonst ~/Games)
        data_dir       ~/.local/share/couch        (nur gelesen: library.json)
        cache_dir      None = kein Zwischenspeicher, es wird gar nichts geschrieben
        run_read       subprocess (default_run_read); in Attrappen-Kontexten no_run_read
        flatpak_dirs   /var/lib/flatpak, ~/.local/share/flatpak

    Mit ausdrücklichem ``home`` (Tests) leitet sich alles aus diesem Ordner ab und nichts
    außerhalb wird gelesen: games_disk = None, flatpak_dirs = [<home>/.local/share/flatpak].

    Ohne ``home``, aber mit COUCH_FIXTURES=<ordner> (bzw. <COUCH_TEST_ROOT>/fixtures):
        <ordner>/home/…          wie oben unter ~
        <ordner>/games           games_disk
        <ordner>/flatpak         flatpak_dirs
    data_dir ist dann <COUCH_TEST_ROOT>/data. (Die Prüf-Quelle "fixture" des Kerns mit
    <ordner>/library.json ist davon unabhängig.)

    Der Kontext darf über mehrere Läufe leben (der FSR-Zwischenspeicher bleibt dann im Speicher).
    """

    def __init__(self, home=None, steam_root=None, lutris_db=None, lutris_config=None,
                 srm_dir=None, games_disk=None, data_dir=None, cache_dir=None, run_read=None, *,
                 flatpak_dirs=None, app_dir=None, fsr_depth=None, mountinfo=None):
        real_home = os.path.expanduser("~")
        fx = fixtures_root() if home is None else None
        test_root = os.environ.get("COUCH_TEST_ROOT")
        if home is None:
            home = os.path.join(fx, "home") if fx else real_home
        is_real = fx is None and os.path.realpath(home) == os.path.realpath(real_home)
        self.home = home
        self.is_real = is_real

        if steam_root is None:
            link = os.path.join(home, ".steam", "root")
            steam_root = os.path.realpath(link) if os.path.isdir(os.path.join(link, "steamapps")) \
                else os.path.join(home, ".local/share/Steam")
        self.steam_root = steam_root
        # E104: Lutris als Flatpak nur lesen, wenn es kein natives gibt. Gestartet wird wie bisher direkt
        # mit umu (E10); „wie in Lutris“ (E55) kennt nur das native Lutris (lutris_flatpak)
        self.lutris_flatpak = False
        if not lutris_db:
            lutris_db = os.path.join(home, ".local/share/lutris/pga.db")
            flat = os.path.join(home, FLATPAK_LUTRIS)
            if not os.path.isfile(lutris_db) and os.path.isfile(os.path.join(flat, "data/lutris/pga.db")):
                lutris_db = os.path.join(flat, "data/lutris/pga.db")
                self.lutris_flatpak = True
                if not lutris_config:
                    conf = os.path.join(flat, "config/lutris")
                    lutris_config = conf if os.path.isdir(conf) else os.path.join(flat, "data/lutris")
        self.lutris_db = lutris_db
        self.lutris_config = lutris_config or os.path.join(home, ".config/lutris")
        self.srm_dir = srm_dir or os.path.join(home, ".config/steam-rom-manager/userData")
        if games_disk is None:
            if fx:
                games_disk = os.path.join(fx, "games")
            elif is_real:
                from .. import paths            # E104: kein fester Ort mehr (library.games_disk, Bestand)
                games_disk = paths.games_disk()
        self.games_disk = games_disk
        if data_dir is None:
            if test_root:
                data_dir = os.path.join(test_root, "data")
            elif is_real:
                data_dir = os.path.join(os.environ.get("XDG_DATA_HOME")
                                        or os.path.join(real_home, ".local/share"), "couch")
            else:
                data_dir = os.path.join(home, ".local/share/couch")
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        # Attrappen-Kontext ohne eigenes run_read: nichts vom echten Rechner erfragen (flatpak info)
        self.run_read: Callable = run_read or (default_run_read if is_real else no_run_read)
        if flatpak_dirs is None:
            if fx:
                flatpak_dirs = [os.path.join(fx, "flatpak")]
            else:
                flatpak_dirs = [os.path.join(home, ".local/share/flatpak")]
                if is_real:
                    flatpak_dirs.insert(0, "/var/lib/flatpak")
        self.flatpak_dirs = list(flatpak_dirs)
        # app/ des laufenden Release (für app/bin/couch-desktop): …/app/couch/library/sources.py
        self.app_dir = app_dir or os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__))))
        self.fsr_depth = fsr_depth
        self.mountinfo = mountinfo
        self.warnings: list[str] = []
        self.last: dict[str, list[Entry]] = {}       # Quelle -> Einträge ihres letzten Laufs
        self.failed: set[str] = set()                # Quellen, die zuletzt nicht lesen konnten
        self._fsr = None

    def fail(self, source: str) -> None:
        """Die Quelle konnte gerade nicht lesen (gesperrte Datenbank, Absturz): Ihr Ergebnis
        ist kein „es gibt nichts mehr“. Der Index lässt dann ihren letzten Stand stehen."""
        self.failed.add(source)

    def warn(self, text: str) -> None:
        """Warnung merken. Jede beginnt mit dem Namen ihrer Quelle („steam: …“)."""
        if text not in self.warnings:
            self.warnings.append(text)

    def drop_warnings(self, source: str) -> None:
        """Warnungen einer Quelle verwerfen (vor ihrem nächsten Lauf)."""
        self.warnings = [w for w in self.warnings if not w.startswith(source + ": ")]

    @property
    def fsr(self):
        """Gemeinsamer FSR-Scanner aller Quellen (ein Zwischenspeicher, einmal geladen)."""
        if self._fsr is None:
            from . import fsr as fsr_mod
            kwargs = {}
            if self.fsr_depth is not None:
                kwargs["max_depth"] = self.fsr_depth
            if self.mountinfo is not None:
                kwargs["mountinfo"] = self.mountinfo
            cache = os.path.join(self.cache_dir, "fsr.json") if self.cache_dir else None
            self._fsr = fsr_mod.FsrScanner(cache_path=cache, home=self.home, **kwargs)
        return self._fsr

    def cache_file(self, name: str) -> str | None:
        return os.path.join(self.cache_dir, name) if self.cache_dir else None

    def games_disk_present(self) -> bool:
        return bool(self.games_disk) and os.path.isdir(self.games_disk)


# ---------------------------------------------------------------- Register und Sammellauf

def all_sources() -> dict[str, Callable[[SourceContext], list[Entry]]]:
    """Name -> Ladefunktion load(ctx), in der Reihenfolge des Einlesens. Jede blockiert und
    schreibt nichts (Ausnahme: die Zwischenspeicher fsr.json und emu_titles.json unter cache_dir)."""
    from . import apps, emulators, local, lutris, roms, steam      # hier, damit kein Importkreis entsteht
    return {
        "steam": steam.load,
        "lutris": lutris.load,
        "emulators": emulators.load,
        "roms": roms.load,                  # E105
        "apps": apps.load,
        "local": local.load,
    }


def _load_shortcuts(ctx: SourceContext) -> list[Entry]:
    """Abgeleitete Quelle "shortcuts": braucht die Einträge aller anderen Quellen. Was in diesem
    Kontext noch nicht gelaufen ist, läuft hier nach (eigenständiger Aufruf)."""
    from . import steam
    others: list[Entry] = []
    for name in all_sources():
        if name not in ctx.last:
            run_source(ctx, name)
        others += ctx.last.get(name, [])
    return steam.load_shortcuts(ctx, others)


def derived_sources() -> dict[str, Callable[[SourceContext], list[Entry]]]:
    """Quellen, die auf den Ergebnissen der anderen aufbauen. Sie laufen nach all_sources()."""
    return {"shortcuts": _load_shortcuts}


# Quellen, deren Spiele bisher über Steam-Verknüpfungen gestartet wurden
_SHORTCUT_TIMES = ("lutris", "emulators", "local")


def _drop_needs_steam(ctx: SourceContext, entries: list[Entry]) -> list[Entry]:
    """E25: Ein Lutris-Eintrag, der den laufenden Steam-Client braucht (Marke needs-steam),
    entfällt, wenn es dasselbe Spiel als Steam-Spiel gibt oder als Verknüpfung, die couch zeigt
    (dann startet es per Übergabe, und Steam läuft). Erkannt wird das Steam-Spiel an der
    App-Nummer aus dem Startskript (detail.steam_appid) oder am gleichen Titel, die Verknüpfung
    am selben Programm oder am gleichen Titel. Nie über ähnliche Namen."""
    if not any("needs-steam" in e.tags for e in entries):
        return entries
    from . import steam
    games = [e for e in ctx.last.get("steam", []) if e.source == "steam"]
    appids = {to_int(e.detail.get("appid")) for e in games if e.detail.get("appid")}
    titles = {title_key(e.title) for e in games}
    shown = [s for s in steam.shortcut_art(ctx) if steam.shortcut_exclusion(ctx, s) is None]
    out = []
    for entry in entries:
        if "needs-steam" in entry.tags:
            key = title_key(entry.title)
            exe = entry.detail.get("exe") or ""
            appid = to_int(entry.detail.get("steam_appid"))
            if (appid and appid in appids) or key in titles or any(
                    title_key(s["name"]) == key or (os.path.isabs(exe) and os.path.isabs(s["exe"])
                                                     and same_path(exe, s["exe"])) for s in shown):
                continue
        out.append(entry)
    return out


def run_source(ctx: SourceContext, name: str, fn=None) -> list[Entry]:
    """Eine Quelle vollständig laufen lassen. Blockierend; wirft nie.

    Verwirft die alten Warnungen dieser Quelle, fängt einen Absturz ab (leere Liste, Warnung und
    ctx.failed), ergänzt bei Lutris-, Emulator- und Archiv-Spielen „zuletzt gespielt“ aus Steams
    Verknüpfungen (steam.apply_shortcut_last_played) und die Marke steam_favorite, wenn die
    Verknüpfung in Steams „Favoriten“ steht (E99), lässt Lutris-Einträge mit needs-steam weg,
    die es als Steam-Spiel gibt (E25), merkt das Ergebnis in ctx.last und sichert den
    FSR-Zwischenspeicher.
    """
    fn = fn or {**all_sources(), **derived_sources()}[name]
    ctx.drop_warnings(name)
    ctx.failed.discard(name)
    try:
        entries = [e for e in (fn(ctx) or []) if isinstance(e, Entry)]
    except Exception as e:                          # nie abstürzen: Quelle auslassen, weitermachen
        ctx.warn("%s: Quelle abgebrochen (%s)" % (name, err_text(e)))
        ctx.fail(name)
        entries = []
    if name in _SHORTCUT_TIMES and entries:
        try:
            from . import steam
            shortcuts = steam.shortcut_art(ctx)
            steam.apply_shortcut_last_played(entries, shortcuts)
            steam.apply_shortcut_favorites(entries, shortcuts, steam.favorite_ids(ctx))   # E99
        except Exception as e:
            ctx.warn("%s: Verknüpfungen nicht ausgewertet (%s)" % (name, err_text(e)))
    if name == "lutris" and entries:
        try:
            entries = _drop_needs_steam(ctx, entries)
        except Exception as e:
            ctx.warn("lutris: Abgleich mit Steam nicht möglich (%s)" % err_text(e))
    ctx.last[name] = entries
    try:
        ctx.fsr.save()
    except Exception as e:
        ctx.warn("%s: FSR-Zwischenspeicher nicht geschrieben (%s)" % (name, err_text(e)))
    return entries


def bound_sources(ctx: SourceContext) -> dict[str, Callable[[], list[Entry]]]:
    """Name -> fn() ohne Parameter, fertig für ein Register wie register_source(name, fn).

    Jede fn ist run_source(ctx, name): blockierend, wirft nie; die Warnungen des letzten Laufs
    stehen danach in ctx.warnings (je Quelle ersetzt, nicht angehäuft).
    """
    def bind(name: str):
        return lambda: run_source(ctx, name)
    return {name: bind(name) for name in all_sources()}


def bound_derived(ctx: SourceContext) -> dict[str, Callable[[], list[Entry]]]:
    """Wie bound_sources für die abgeleiteten Quellen ("shortcuts"); NACH den anderen anmelden,
    damit sie im selben Lauf deren frisches Ergebnis sehen."""
    def bind(name: str):
        return lambda: run_source(ctx, name)
    return {name: bind(name) for name in derived_sources()}


def load_all(ctx: SourceContext | None = None, only=None, derived: bool = False):
    """Alle Quellen einlesen. Blockierend (auf dem Entwicklungsrechner ohne Zwischenspeicher rund 0,7 bis
    1,5 s, mit Zwischenspeicher rund 0,1 s). Rückgabe: (entries, warnings, timings).

    timings: {"steam": s, "lutris": s, "emulators": s, "roms": s, "apps": s, "local": s, "total": s}.
    Eine abgestürzte Quelle liefert nichts und eine Warnung; die anderen laufen weiter. Doppelte
    IDs über Quellen hinweg: der erste Eintrag bleibt.
    derived=True nimmt die abgeleiteten Quellen dazu (Verknüpfungen nach E25; timings bekommt
    dann auch "shortcuts").
    """
    ctx = ctx or SourceContext()
    ctx.warnings = []
    entries: list[Entry] = []
    timings: dict[str, float] = {}
    seen: dict[str, str] = {}
    t_all = time.monotonic()
    names = dict(all_sources())
    if derived:
        names.update(derived_sources())
    for name, fn in names.items():
        if only and name not in only:
            continue
        t0 = time.monotonic()
        got = run_source(ctx, name, fn)
        timings[name] = round(time.monotonic() - t0, 4)
        for entry in got:
            if entry.id in seen:
                ctx.warn("%s: doppelte ID %s (schon aus %s), ausgelassen" % (name, entry.id, seen[entry.id]))
                continue
            seen[entry.id] = name
            entries.append(entry)
    timings["total"] = round(time.monotonic() - t_all, 4)
    return entries, list(ctx.warnings), timings


def find_duplicates(entries) -> list[list[Entry]]:
    """Gruppen von Einträgen mit gleichem Titel-Schlüssel (nur zur Anzeige; nichts wird entfernt)."""
    groups: dict[tuple, list[Entry]] = {}
    for e in entries:
        groups.setdefault((e.kind, title_key(e.title)), []).append(e)
    return [g for g in groups.values() if len(g) > 1]


# ---------------------------------------------------------------- Befehl

def _detail_column(e: Entry) -> str:
    d = e.detail
    if e.source == "steam":
        if d.get("shortcut"):
            return "Verknüpfung | %s | %s" % (d.get("compat_tool") or "-", d.get("exe", ""))
        return "%s | %s" % (d.get("compat_tool") or "Steams Vorgabe", d.get("library", ""))
    if e.source == "proton":
        return "%s | Prefix %s%s | %s" % (d.get("wine_version", ""), d.get("prefix_state", ""),
                                         " (geteilt)" if d.get("shared_prefix") else "", d.get("exe", ""))
    if e.source in ("switch", "wiiu"):
        return "%s | %s" % (d.get("emulator", ""), d.get("rom", ""))
    if e.source == "app":
        return d.get("flatpak") or d.get("path") or d.get("hint") or ""
    return d.get("exe_path") or d.get("exe") or ""


def print_table(entries, warnings, timings, out=None) -> None:
    out = out or sys.stdout
    rows = [("ID", "Quelle", "Marke", "Titel", "da", "inst", "Tags", "zuletzt", "Std", "Einzelheit")]
    for e in entries:
        last = time.strftime("%Y-%m-%d", time.localtime(e.last_played)) if e.last_played else "-"
        rows.append((e.id, e.source, e.badge or "-", e.title, "ja" if e.available else "NEIN",
                     "ja" if e.installed else "NEIN", ",".join(e.tags) or "-", last,
                     "%.1f" % (e.playtime / 3600) if e.playtime else "-", _detail_column(e)))
    widths = [min(max(len(r[i]) for r in rows), 46) for i in range(len(rows[0]) - 1)]
    for r in rows:
        cells = [r[i][:widths[i]].ljust(widths[i]) for i in range(len(widths))]
        print("  ".join(cells + [r[-1]]).rstrip(), file=out)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1
    print("\n%d Einträge: %s" % (len(entries), ", ".join("%s %d" % kv for kv in sorted(counts.items()))), file=out)
    print("Laufzeiten (s): " + ", ".join("%s %.3f" % kv for kv in timings.items()), file=out)
    for group in find_duplicates(entries):
        print("Gleicher Titel: " + " / ".join("%s (%s)" % (e.id, e.title) for e in group), file=out)
    for w in warnings:
        print("Warnung: " + w, file=out)


def main(argv=None) -> int:
    import argparse
    from dataclasses import asdict
    ap = argparse.ArgumentParser(prog="python3 -m couch.library.sources",
                                 description="Bibliothek einlesen und ausgeben (nur lesend).")
    ap.add_argument("--table", action="store_true", help="Tabelle (Vorgabe)")
    ap.add_argument("--json", action="store_true", help="vollständige Einträge als JSON")
    ap.add_argument("--source", action="append", choices=SOURCE_NAMES + DERIVED_NAMES,
                    help="nur diese Quelle(n)")
    ap.add_argument("--cache-dir", help="Ordner für fsr.json/emu_titles.json (Vorgabe: keiner, nichts wird geschrieben)")
    ap.add_argument("--repeat", type=int, default=1, help="mehrmals einlesen (Laufzeit mit warmem Zwischenspeicher)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # Dateinamen mit kaputter Kodierung nicht zum Absturz werden lassen
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ctx = SourceContext(cache_dir=args.cache_dir)
    entries, warnings, timings = [], [], {}
    for _ in range(max(1, args.repeat)):
        entries, warnings, timings = load_all(ctx, only=args.source, derived=True)
    if args.json:
        json.dump({"entries": [asdict(e) for e in entries], "warnings": warnings, "timings": timings},
                  sys.stdout, ensure_ascii=True, indent=1)
        print()
    else:
        print_table(entries, warnings, timings)
    return 0


if __name__ == "__main__":
    # Über den Paketnamen laufen lassen; sonst gäbe es dieses Modul zweimal (__main__ und couch.library.sources).
    from couch.library.sources import main as _main
    sys.exit(_main())
