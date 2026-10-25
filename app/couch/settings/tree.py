"""Einstellungsbaum: lädt die Bereichs-Module und leitet Seiten, Änderungen und Aktionen weiter.

Verwendung durch den Verdrahter (alles hier darf blockieren, also im Thread rufen):

    deps = SimpleNamespace(config=..., run_read=..., run_write=..., gs=..., paths=...,
                           scheduler=..., notify=..., extra={...})
    tree = SettingsTree(deps)
    tree.on_changed(lambda page_id: rpc.emit("settings.changed", {"page": page_id}))
    tree.categories() / tree.page(id) / tree.set(row_id, value) / tree.action(row_id, value)

Eigenständig (lesend, run_write ist eine mitschreibende Attrappe):

    PYTHONPATH=app COUCH_TEST_ROOT=<ordner> python3 -m couch.settings.tree --dump
"""
from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from .rows import TRIAL_KEEP, TRIAL_REVERT, Page, Row, SettingsError

log = logging.getLogger("couch.settings")

GENERIC_ERROR = "Das hat nicht geklappt."

# Zeilen vom Typ confirm-action (ID bis zum ersten ":"): Unumkehrbares, das der Kern nur nach einer
# bestätigten Rückfrage ausführt (settings.action mit confirmed: true, sonst fragt der Kern selbst).
# Maßgeblich ist der Typ der Zeile auf ihrer Seite; diese Liste gilt nur, wenn die Zeile auf keiner
# gezeichneten Seite stand. tests/features/settings-system/test_contract.py hält sie vollständig.
CONFIRM_PREFIXES = frozenset({
    "accessories.bt.remove", "network.wifi.forget", "storage.local.delete", "games.steam.open",
    "session.steam", "session.desktop", "session.restart", "session.reset",
    "system.os.reboot", "system.os.install", "system.power.off", "system.power.reboot",
    "system.power.suspend",
    "games.itch.logout",                # E100: Abmelden widerruft den Schlüssel bei itch.io
    # E102: Proton-Versionen laden (hunderte MB), neu laden, in den Papierkorb legen
    "system.proton.get", "system.proton.build.reload", "system.proton.build.remove",
    # E116: den PC5 Launcher von GitHub aktualisieren, danach neu starten (schließt laufende Spiele)
    "system.launcher.update", "system.launcher.restart",
})
GENERIC_CONFIRM = {"title": "Wirklich ausführen?", "text": "Das lässt sich nicht rückgängig machen.",
                   "ok": "Ausführen", "cancel": "Abbrechen"}
NOT_RESETTABLE = "Diese Einstellung lässt sich nicht einzeln zurücksetzen."


class SettingsTree:
    AREA_MODULES = ["audio", "display", "network", "accessories", "games", "performance",
                    "storage", "home", "notifications", "accessibility", "system", "session"]

    def __init__(self, deps, modules: list[str] | None = None, package: str = "couch.settings"):
        """deps: SimpleNamespace(config, run_read, run_write, gs, paths, scheduler, notify, extra).

        Fehlende Teile werden mit harmlosen Vorgaben aufgefüllt (siehe make_deps). Der Baum hängt
        deps.changed(page_id) ein; darüber melden Bereiche Änderungen von außen.
        modules/package nur für Tests (andere Modulnamen bzw. ein Paket mit Attrappen).
        """
        self.deps = fill_deps(deps)
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.Lock()
        self._row_page: dict[str, str] = {}      # Zeile -> Seite, auf der sie zuletzt stand
        self.areas: list = []
        self.skipped: dict[str, str] = {}        # Modulname -> Grund (für Protokoll und Tests)
        self.deps.changed = self._emit_changed
        for name in (modules if modules is not None else self.AREA_MODULES):
            self._load(package, name)
        self.areas.sort(key=lambda a: (getattr(a, "order", 1000), a.id))

    # --- Laden ---------------------------------------------------------------------------

    def _load(self, package: str, name: str) -> None:
        # Ein kaputter Bereich darf die übrigen nie mitreißen: alles abfangen, vermerken, weiter.
        try:
            mod = importlib.import_module(f"{package}.{name}")
            area = mod.make_area(self.deps)
            for attr in ("id", "label", "icon", "order", "pages", "page", "set", "action"):
                if not hasattr(area, attr):
                    raise TypeError(f"Bereich ohne '{attr}'")
            if any(a.id == area.id for a in self.areas):
                raise ValueError(f"Bereichs-ID '{area.id}' doppelt")
        except ModuleNotFoundError as e:
            # Nur das Bereichs-Modul selbst darf still fehlen; fehlt etwas darin, ist das ein Fehler.
            if e.name == f"{package}.{name}":
                self.skipped[name] = "fehlt"
                log.info("Einstellungsbereich %s fehlt, übersprungen", name)
            else:
                self.skipped[name] = f"ModuleNotFoundError: {e}"
                log.exception("Einstellungsbereich %s lädt nicht", name)
            return
        except Exception as e:  # noqa: BLE001 - bewusst breit, siehe oben
            self.skipped[name] = f"{type(e).__name__}: {e}"
            log.exception("Einstellungsbereich %s lädt nicht", name)
            return
        self.areas.append(area)

    # --- Zuordnung -----------------------------------------------------------------------

    def _area_for(self, ident: str):
        """Bereich zu einer Seiten- oder Zeilen-ID: erst genaue Seite, dann Vorsatz vor dem Punkt."""
        ident = str(ident)
        for a in self.areas:
            if ident == a.id:
                return a
        for a in self.areas:
            try:
                if ident in a.pages():
                    return a
            except Exception:  # noqa: BLE001
                log.exception("pages() von %s wirft", a.id)
        head = ident.split(".", 1)[0].split(":", 1)[0]
        for a in self.areas:
            if a.id == head:
                return a
        return None

    # --- Schnittstelle -------------------------------------------------------------------

    def categories(self) -> list[dict]:
        return [{"id": a.id, "label": a.label, "icon": a.icon} for a in self.areas]

    def page(self, page_id: str) -> dict:
        """{"id", "title", "rows": [Row.to_dict()]}; bei Fehlern eine Seite mit Hinweiszeile und "error"."""
        area = self._area_for(page_id)
        if area is None:
            return _error_page(page_id, "Einstellungen", "Diese Seite gibt es nicht.")
        try:
            page = area.page(page_id)
            if not isinstance(page, Page):
                raise TypeError(f"page() liefert {type(page).__name__}")
            out = page.to_dict()
        except KeyError:
            return _error_page(page_id, area.label, "Diese Seite gibt es nicht.")
        except SettingsError as e:
            return _error_page(page_id, area.label, e.message)
        except Exception:  # noqa: BLE001
            log.exception("Seite %s wirft", page_id)
            return _error_page(page_id, area.label, "Dieser Bereich ist gerade nicht verfügbar.")
        with self._lock:
            for r in out["rows"]:
                self._row_page[r["id"]] = out["id"]
        return out

    def set(self, row_id: str, value: Any) -> dict:
        """Leitet an den Bereich. Liefert {"message"?, "trial"?, "page"?, "rows"?, "error"?}."""
        return self._forward("set", row_id, value)

    def action(self, row_id: str, value: Any = None) -> dict:
        return self._forward("action", row_id, value)

    def reset(self, row_id: str) -> dict:
        """settings.reset: eine Zeile auf ihre Vorgabe (Row.default) zurücksetzen, über denselben
        Weg wie set(). Die Zeile wird auf der Seite nachgeschlagen, auf der sie zuletzt stand
        (frisch gezeichnet); ohne Vorgabe ein Fehler im Ergebnis. Ergebnis wie set(), immer mit
        der frischen Seite."""
        row_id = str(row_id)
        area = self._area_for(row_id) if row_id else None
        if area is None:
            return {"error": True, "message": "Diese Einstellung gibt es nicht."}
        with self._lock:
            page_id = self._row_page.get(row_id)
        row = None
        for pid in ([page_id] if page_id else []) + [area.id]:
            row = next((r for r in self.page(pid).get("rows", []) if r.get("id") == row_id), None)
            if row is not None:
                break
        if row is None or row.get("default") is None or row.get("type") not in ("toggle", "select", "slider", "action"):
            return {"error": True, "message": NOT_RESETTABLE}
        if row.get("value") == row["default"]:
            fresh = self.page(page_id or area.id)
            return {"rows": fresh["rows"], "rows_page": fresh["id"]}
        log.info("Einstellung %s auf die Vorgabe zurückgesetzt", row_id)
        out = self._forward("set", row_id, row["default"])
        if "rows" not in out:
            fresh = self.page(page_id or area.id)
            out["rows"], out["rows_page"] = fresh["rows"], fresh["id"]
        return out

    def confirm_for(self, row_id: str, value: Any = None) -> dict | None:
        """Die Rückfrage {title, text, ok, cancel} vor action(row_id), oder None, wenn die Zeile
        keine braucht. Blockierend (zeichnet die Seite, auf der die Zeile zuletzt stand, neu: so
        stehen im Text die Sätze von jetzt, z. B. laufende Spiele)."""
        row_id = str(row_id)
        if value in (TRIAL_KEEP, TRIAL_REVERT):
            return None                         # Antwort auf eine Probezeit, keine eigene Aktion
        with self._lock:
            page_id = self._row_page.get(row_id)
        if page_id is not None:
            for row in self.page(page_id).get("rows", []):
                if row.get("id") == row_id:
                    if row.get("type") != "confirm-action":
                        return None
                    return {**GENERIC_CONFIRM, **{k: v for k, v in (row.get("confirm") or {}).items() if v}}
        if row_id.split(":", 1)[0] in CONFIRM_PREFIXES:
            return dict(GENERIC_CONFIRM)        # nie gezeichnet oder verschwunden: trotzdem fragen
        return None

    def on_changed(self, fn: Callable[[str], None]) -> None:
        """fn(page_id) bei Änderungen von außen. Kann aus einem Thread kommen: Der Verdrahter
        reicht in die Hauptschleife weiter (GLib.idle_add)."""
        self._listeners.append(fn)

    def changed(self, page_id: str) -> None:
        """Für den Verdrahter: eine Seite von außen als geändert melden (z. B. "accessories" bei
        pads.changed). Dasselbe wie deps.changed aus einem Bereich."""
        self._emit_changed(page_id)

    def area(self, area_id: str):
        for a in self.areas:
            if a.id == area_id:
                return a
        return None

    # --- intern --------------------------------------------------------------------------

    def _forward(self, kind: str, row_id: str, value: Any) -> dict:
        area = self._area_for(row_id)
        if area is None:
            return {"error": True, "message": "Diese Einstellung gibt es nicht."}
        try:
            res = area.set(row_id, value) if kind == "set" else area.action(row_id, value)
        except SettingsError as e:
            res = {"error": True, "message": e.message, "refresh": True}
        except Exception:  # noqa: BLE001
            log.exception("%s(%s) wirft", kind, row_id)
            res = {"error": True, "message": GENERIC_ERROR, "refresh": True}
        out = dict(res) if isinstance(res, dict) else {}
        if out.pop("refresh", False):
            # Die Seite, auf der die Zeile zuletzt stand, gleich mitliefern (spart einen Umlauf).
            with self._lock:
                page_id = self._row_page.get(row_id)
            if page_id is None:
                page_id = area.id
            fresh = self.page(page_id)
            out["rows"] = fresh["rows"]
            out["rows_page"] = fresh["id"]
        return out

    def _emit_changed(self, page_id: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(page_id)
            except Exception:  # noqa: BLE001
                log.exception("Beobachter für settings.changed wirft")


def _error_page(page_id: str, title: str, message: str) -> dict:
    row = Row(id=f"{page_id}.unavailable", type="info", label=message, enabled=False)
    return {"id": page_id, "title": title, "rows": [row.to_dict()], "error": True}


# =============================================================================================
# Vorgaben für eigenständige Läufe und Tests. Der Kern steckt seine echten Dienste ein.
# =============================================================================================

class MemoryConfig:
    """Config nach interfaces.Config, nur im Speicher."""

    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})
        self._watch: list[tuple[str, Callable[[str, Any], None]]] = []

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        for prefix, fn in list(self._watch):
            if key.startswith(prefix):
                fn(key, value)

    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        self._watch.append((prefix, fn))


class WriteRecorder:
    """run_write-Attrappe: schreibt nur mit, führt nichts aus, meldet Erfolg."""

    def __init__(self, result: tuple[int, str, str] = (0, "", "")):
        self.calls: list[list[str]] = []
        self.result = result
        self.results: dict[str, tuple[int, str, str]] = {}   # argv[0] oder ganzer Befehl -> Ergebnis

    def __call__(self, argv, timeout: float = 30):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        return self.results.get(" ".join(argv), self.results.get(argv[0], self.result))

    def clear(self) -> None:
        self.calls.clear()


def subprocess_read(argv, timeout: float = 10) -> tuple[int, str, str]:
    """run_read über subprocess. Blockierend. Nie für Befehle benutzen, die etwas verändern."""
    try:
        p = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: nicht gefunden"
    except subprocess.TimeoutExpired:
        return 124, "", "Zeit abgelaufen"
    except OSError as e:
        return 126, "", str(e)


class NullGamescope:
    """Gamescope-Attrappe: keine Sitzung, Eigenschaften nur im Speicher."""

    def __init__(self):
        self.props: dict[str, list[int]] = {}

    def get(self, name, window=None):
        return self.props.get(name)

    def set(self, name, values, window=None):
        self.props[name] = list(values)

    def delete(self, name, window=None):
        self.props.pop(name, None)

    def focused_app(self): return 0
    def focusable_apps(self): return []
    def focusable_windows(self): return []
    def root_size(self): return (3840, 2160)
    def game_display(self): return ""
    def hdr_output_active(self): return False
    def watch(self, callback): pass


class ThreadScheduler:
    """Planer ohne GLib: threading.Timer und Threads."""

    def later(self, seconds: float, fn: Callable[[], None]):
        t = threading.Timer(max(0.0, seconds), fn)
        t.daemon = True
        t.start()
        return t

    def cancel(self, handle) -> None:
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001
            pass

    def background(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()


class _GTimer:
    """Griff eines Zeitgebers von GLibScheduler. cancelled darf jeder Thread setzen; done setzt
    nur die Hauptschleife, und die GLib-Quelle selbst fasst auch nur sie an."""
    __slots__ = ("source", "done", "cancelled")

    def __init__(self) -> None:
        self.source = 0
        self.done = False           # abgelaufen oder aufgehoben
        self.cancelled = False


class GLibScheduler:
    """Planer für das Home: Zeitgeber laufen in der GLib-Hauptschleife, Arbeit in Threads.

    later() und cancel() dürfen aus jedem Thread kommen (Ton-Beobachter, Probezeiten,
    OffMainScheduler). Aufgehoben wird eine Quelle aber nur im Thread der Hauptschleife: Holt ein
    Arbeits-Thread sie über ihre Nummer (find_source_by_id) und gibt sie frei, während die
    Hauptschleife sie gerade ausführt oder abräumt, stürzt das Home mit SIGSEGV ab (Befund vom
    2026-10-14, nachgestellt in tests/unit/test_timer_threads.py). Aus einem anderen Thread wird
    deshalb nur der Merker gesetzt (der Rückruf tut dann nichts mehr) und das Aufheben in die
    Hauptschleife gereicht."""

    def __init__(self):
        from gi.repository import GLib   # erst hier, damit Tests ohne gi auskommen
        self._glib = GLib

    def later(self, seconds: float, fn: Callable[[], None]):
        timer = _GTimer()

        def once():
            timer.done = True
            if not timer.cancelled:
                fn()
            return False
        timer.source = self._glib.timeout_add(int(max(0.0, seconds) * 1000), once)
        return timer

    def cancel(self, handle) -> None:
        if not isinstance(handle, _GTimer):
            return                      # None oder fremder Griff: nichts tun (rows.Scheduler)
        handle.cancelled = True
        if handle.done:
            return
        try:
            if self._glib.MainContext.default().is_owner():
                self._remove(handle)
            else:
                self._glib.idle_add(lambda: self._remove(handle))
        except Exception:  # noqa: BLE001
            log.exception("Zeitgeber aufheben")

    def _remove(self, timer: _GTimer) -> bool:
        """Nur im Thread der Hauptschleife: Dort läuft auch once(); eine Quelle ohne done-Merker
        ist also noch nicht abgelaufen und darf über ihre Nummer aufgehoben werden."""
        if not timer.done and timer.source:
            timer.done = True
            self._glib.source_remove(timer.source)
        return False

    def background(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()


class ManualScheduler:
    """Planer für Tests: Zeit läuft nur über advance(); background() läuft sofort (oder gesammelt)."""

    def __init__(self, defer_background: bool = False):
        self.now = 0.0
        self.timers: list[list] = []          # [fällig, fn, aktiv]
        self.defer_background = defer_background
        self.jobs: list[Callable[[], None]] = []

    def later(self, seconds: float, fn: Callable[[], None]):
        entry = [self.now + max(0.0, seconds), fn, True]
        self.timers.append(entry)
        return entry

    def cancel(self, handle) -> None:
        if isinstance(handle, list) and len(handle) == 3:
            handle[2] = False

    def background(self, fn: Callable[[], None]) -> None:
        if self.defer_background:
            self.jobs.append(fn)
        else:
            fn()

    def run_background(self) -> None:
        jobs, self.jobs = self.jobs, []
        for fn in jobs:
            fn()

    def advance(self, seconds: float) -> None:
        """Zeit vorstellen; fällige Zeitgeber laufen in ihrer Reihenfolge, die Uhr steht dabei
        jeweils auf ihrem Fälligkeitszeitpunkt (so stimmen auch Zeitgeber, die neue stellen)."""
        target = self.now + seconds
        while True:
            due = [t for t in self.timers if t[2] and t[0] <= target]
            if not due:
                break
            nxt = min(due, key=lambda t: t[0])
            nxt[2] = False
            self.now = max(self.now, nxt[0])
            nxt[1]()
        self.now = target
        self.timers = [t for t in self.timers if t[2]]

    def pending(self) -> int:
        return sum(1 for t in self.timers if t[2])


def default_paths(root: str | None = None) -> SimpleNamespace:
    """Orte nach Bauplan Abschnitt 2. COUCH_TEST_ROOT (oder root) lenkt alle fünf um."""
    root = root or os.environ.get("COUCH_TEST_ROOT")
    if root:
        root = os.path.abspath(root)
        return SimpleNamespace(config_dir=os.path.join(root, "config"), data_dir=os.path.join(root, "data"),
                               state_dir=os.path.join(root, "state"), run_dir=os.path.join(root, "run"),
                               cache_dir=os.path.join(root, "cache"))
    home = os.path.expanduser("~")
    xdg = lambda var, rel: os.environ.get(var) or os.path.join(home, rel)   # noqa: E731
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return SimpleNamespace(config_dir=os.path.join(xdg("XDG_CONFIG_HOME", ".config"), "couch"),
                           data_dir=os.path.join(xdg("XDG_DATA_HOME", ".local/share"), "couch"),
                           state_dir=os.path.join(xdg("XDG_STATE_HOME", ".local/state"), "couch"),
                           run_dir=os.path.join(run, "couch"),
                           cache_dir=os.path.join(xdg("XDG_CACHE_HOME", ".cache"), "couch"))


def fill_deps(deps=None) -> SimpleNamespace:
    """Ergänzt fehlende Abhängigkeiten durch harmlose Vorgaben. run_write ist dann IMMER eine
    Attrappe: Wer wirklich schreiben will, muss es ausdrücklich einstecken."""
    d = deps if deps is not None else SimpleNamespace()
    defaults = {
        "config": MemoryConfig,
        "run_read": lambda: subprocess_read,
        "run_write": WriteRecorder,
        "gs": NullGamescope,
        "paths": default_paths,
        "scheduler": ThreadScheduler,
        "notify": lambda: (lambda title, body="", icon="": None),
        "extra": dict,
        "changed": lambda: (lambda page_id: None),
    }
    for name, factory in defaults.items():
        if getattr(d, name, None) is None:
            setattr(d, name, factory())
    return d


def make_deps(**overrides) -> SimpleNamespace:
    """Vollständiges deps-Objekt für Tests und eigenständige Läufe."""
    return fill_deps(SimpleNamespace(**overrides))


# =============================================================================================
# Lesender Lauf gegen den Rechner
# =============================================================================================

def _format_row(r: dict) -> str:
    val = r["value"]
    if r["type"] == "toggle":
        shown = "Ein" if val else "Aus"
    elif r["type"] == "select":
        shown = next((o["label"] for o in (r["options"] or []) if o["value"] == val), repr(val))
        shown += f"   ({len(r['options'] or [])} Optionen)"
    elif r["type"] == "slider":
        shown = f"{val}{r['unit']}  [{r['min']}..{r['max']} / {r['step']}]"
    elif r["type"] == "page":
        shown = f"-> {r['page']}"
    elif r["type"] == "text":
        shown = "•••" if r["secret"] and val else (val or "")
    else:
        shown = "" if val is None else str(val)
    flags = "" if r["enabled"] else "  [gesperrt]"
    note = f"  ({r['note']})" if r["note"] else ""
    line = f"  {r['type']:<14} {r['id']:<44} {r['label']}: {shown}{note}{flags}"
    if r["hint"]:
        line += f"\n{'':17}· {r['hint']}"
    return line


def dump(tree: SettingsTree, out=None, follow: bool = True) -> list[tuple[str, float]]:
    """Erzeugt jede Seite einmal und gibt sie als Text aus. Liefert [(Seite, Sekunden)]."""
    out = out or sys.stdout
    timings: list[tuple[str, float]] = []
    seen: set[str] = set()
    for cat in tree.categories():
        area = tree.area(cat["id"])
        queue = list(area.pages())
        print(f"\n=== {cat['label']}  [{cat['id']}, Symbol {cat['icon']}] ===", file=out)
        while queue:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            t0 = time.monotonic()
            page = tree.page(pid)
            dt = time.monotonic() - t0
            timings.append((pid, dt))
            mark = "  FEHLER" if page.get("error") else ""
            print(f"\n--- {page['id']}: {page['title']}  ({dt * 1000:.0f} ms){mark}", file=out)
            for r in page["rows"]:
                print(_format_row(r), file=out)
                if follow and r["type"] == "page" and r["page"] and r["page"] not in seen:
                    queue.append(r["page"])
    if tree.skipped:
        print("\nÜbersprungene Bereiche:", file=out)
        for name, why in tree.skipped.items():
            print(f"  {name}: {why}", file=out)
    return timings


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Einstellungsseiten lesend gegen diesen Rechner erzeugen")
    ap.add_argument("--dump", action="store_true", help="jede Seite einmal als Text ausgeben")
    ap.add_argument("--twice", action="store_true", help="zweiter Durchlauf mit warmem Zwischenspeicher")
    ap.add_argument("--only", default="", help="nur diese Bereiche (Komma-getrennt)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.dump:
        ap.print_help()
        return 2
    if not os.environ.get("COUCH_TEST_ROOT"):
        # Ohne Käfig lägen Zwischenspeicher in den echten Ordnern des Nutzers.
        print("COUCH_TEST_ROOT ist nicht gesetzt (Ordner für Zwischenspeicher).", file=sys.stderr)
        return 2
    writes = WriteRecorder()
    deps = make_deps(run_write=writes)
    modules = [m.strip() for m in args.only.split(",") if m.strip()] or None
    t0 = time.monotonic()
    tree = SettingsTree(deps, modules=modules)
    print(f"Bereiche geladen in {(time.monotonic() - t0) * 1000:.0f} ms: "
          f"{', '.join(a.id for a in tree.areas)}")
    timings = dump(tree)
    if args.twice:
        time.sleep(1.0)   # Hintergrundarbeit des ersten Durchlaufs abwarten
        print("\n\n########## zweiter Durchlauf (warm) ##########")
        warm = dump(tree, out=open(os.devnull, "w"))
        print("\nLaufzeiten kalt/warm:")
        cold = dict(timings)
        for pid, dt in warm:
            print(f"  {pid:<40} {cold.get(pid, 0) * 1000:7.0f} ms {dt * 1000:7.0f} ms")
    else:
        print("\nLaufzeiten:")
        for pid, dt in timings:
            print(f"  {pid:<40} {dt * 1000:7.0f} ms")
    print(f"\nrun_write-Aufrufe (nur mitgeschrieben, nichts ausgeführt): {len(writes.calls)}")
    for c in writes.calls:
        print("  ", " ".join(c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
