"""Verteiler der Starter: aus einem Bibliothekseintrag wird eine LaunchSpec (Bauplan 4.8).

Die Starter starten nichts; sie rechnen und prüfen nur. Für den Verdrahter:

    ctx = LaunchContext(config=app.config, hdr_active=lambda: snapshot["hdr"],
                        screen_size=lambda: snapshot["size"], data_dir=…, state_dir=…,
                        app_dir=…, home=…, run_read=system.run_read)
    register_all(register_launcher)                     # oder build(entry, ctx) direkt

    pair = needs_adoption(entry, ctx)                   # (src, dst) | None, schnell
    if pair: adopt(entry, ctx, progress_cb, cancel)     # BLOCKIEREND, im Thread; vorher Hinweis zeigen
    warnings = preflight(entry, ctx)                    # blockierend (Platte, /proc, flatpak ps)
    spec = build(entry, ctx)                            # blockierend wie preflight

Die Starter laufen im Arbeits-Thread und fragen Gamescope nie selbst (python-xlib hat ohne
Xlib.threaded keine Sperren): HDR und Bildschirmgröße kommen aus dem Schnappschuss, den
wiring_launch.refresh_snapshot in der Hauptschleife nachführt. Die Umgebung des Starts rechnet
der AppManager (proc/manager.build_env, Muster in spec.unset über spec.strip_env).

Fehler sind immer LaunchError mit einem deutschen Satz in .message.

Lesender Lauf gegen die echte Bibliothek (couch.library.sources), ohne etwas zu starten oder zu kopieren:

    PYTHONPATH=app python3 -m couch.launch.builders --plan [--json] [--only TEXT] [--hdr]

Er schreibt nichts außer höchstens dem eigenen Protokollordner der Apps (paths.app_log über
steam_launcher, ohne COUCH_TEST_ROOT unter ~/.local/state/couch); Agenten lassen ihn im Käfig laufen.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from . import apps, emulator, lutris_way, native, prefix, proton
from .prefix import AdoptCancelled, AdoptionRequired
from .spec import APP_KINDS, LaunchError, LaunchSpec

TEXT_NO_LAUNCHER = "Für diesen Eintrag gibt es keinen Starter."

# Quellen, für die register_all einen Starter einträgt. "steam" und app:steam sind Übergaben
# und gehören dem Kern. "rom" (E105, ROM-Ordner ohne SRM) startet wie switch/wiiu über emulator.py.
SOURCES = ("proton", "local", "native", "switch", "wiiu", "app", "rom")


class DictConfig:
    """Einfachste Einstellungen für eigenständige Läufe und Tests (Protokoll Config)."""

    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def on_change(self, prefix, fn):
        return None


def default_run_read(argv, timeout=10):
    """Lesender Systemaufruf: (returncode, stdout, stderr). Blockierend."""
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Zeit abgelaufen"
    except OSError as err:
        return 127, "", str(err)


def _no_hdr() -> bool:
    return False


def _full_hd() -> tuple:
    return (1920, 1080)


def _default_app_dir() -> str:
    # …/app/couch/launch/builders.py -> …/app
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


@dataclass
class LaunchContext:
    """Was die Starter von außen brauchen. Alles einsteckbar, nichts kommt aus Kern-Dateien.

    Leere Zeichenketten werden in __post_init__ mit den Vorgaben gefüllt. Ist COUCH_TEST_ROOT
    gesetzt, zeigen data_dir, state_dir und der Prefix-Ordner in diesen Käfig.
    """
    config: Any = field(default_factory=DictConfig)     # get(key, default) / set / on_change
    hdr_active: Callable[[], bool] = _no_hdr            # gibt die Sitzung gerade HDR aus?
    screen_size: Callable[[], tuple] = _full_hd         # (Breite, Höhe) des Spiele-Bildschirms
    data_dir: str = ""
    state_dir: str = ""
    app_dir: str = ""                                   # Ordner mit bin/ und couch/
    home: str = ""
    run_read: Callable = default_run_read               # (argv, timeout=10) -> (rc, out, err)
    # Zusätze, vor allem für Tests:
    prefix_root: str = ""                               # leer: config "games.prefix_root" bzw. ~/Games/prefixes/couch
    compat_dir: str = ""                                # leer: ~/.local/share/Steam/compatibilitytools.d
    proc_root: str = "/proc"
    # Standard-Proton der Einstellungen als Ordnername ("" = keins): steckt die Verdrahtung ein
    # (wiring_settings.resolve_proton). Ohne die Abfrage sucht launch/proton.py selbst.
    default_proton: Callable[[], str] | None = None
    flatpak_roots: tuple = ()                           # leer: /var/lib/flatpak und ~/.local/share/flatpak
    media_roots: tuple = (("/run/media", 2), ("/media", 1), ("/mnt", 1))
    which: Callable = shutil.which

    def __post_init__(self):
        cage = os.environ.get("COUCH_TEST_ROOT") or ""
        if not self.home:
            self.home = os.path.expanduser("~")
        if not self.app_dir:
            self.app_dir = _default_app_dir()
        if not self.data_dir:
            self.data_dir = os.path.join(cage, "data") if cage else os.path.join(
                os.environ.get("XDG_DATA_HOME") or os.path.join(self.home, ".local/share"), "couch")
        if not self.state_dir:
            self.state_dir = os.path.join(cage, "state") if cage else os.path.join(
                os.environ.get("XDG_STATE_HOME") or os.path.join(self.home, ".local/state"), "couch")

    def bin(self, name: str) -> str:
        return os.path.join(self.app_dir, "bin", name)

    def prefix_root_dir(self) -> str:
        """Ordner für eigene und übernommene Prefixe. Im Prüfmodus nie der echte Ordner des Nutzers."""
        if self.prefix_root:
            return self.prefix_root
        configured = str(self.config.get("games.prefix_root", "") or "")
        if configured:
            return native.expand(configured, self.home)
        cage = os.environ.get("COUCH_TEST_ROOT") or ""
        if cage:
            return os.path.join(cage, "data", "prefixes", "couch")
        return os.path.join(self.home, "Games", "prefixes", "couch")

    def compat_tools_dir(self) -> str:
        return self.compat_dir or os.path.join(self.home, ".local/share/Steam/compatibilitytools.d")

    def flatpak_dirs(self) -> tuple:
        return tuple(self.flatpak_roots) or (
            "/var/lib/flatpak", os.path.join(self.home, ".local/share/flatpak"))


def default_context(**overrides) -> LaunchContext:
    return LaunchContext(**overrides)


# ---------------------------------------------------------------------------------------------
# Verteiler

def _module(entry):
    source = entry.source
    if source == "proton":
        return proton
    if source == "local":
        platform = str(entry.detail.get("platform") or "")
        if platform == "windows":
            return proton
        if platform == "linux":
            return native
        raise LaunchError("Für dieses Spiel ist nicht festgelegt, ob es ein Windows- oder "
                          "Linux-Spiel ist.", "platform=%r" % platform, reason="no-launcher")
    if source == "native":
        return native
    if source in ("switch", "wiiu", "rom"):
        return emulator
    if source == "app":
        return apps
    raise LaunchError(TEXT_NO_LAUNCHER, "Quelle %r, Eintrag %s" % (source, entry.id),
                      reason="no-launcher")


def build(entry, ctx: LaunchContext | None = None, *, check: bool = True,
          planning: bool = False) -> LaunchSpec:
    """Entry -> LaunchSpec. Wirft LaunchError (deutscher Satz) bei allem, was den Start hindert.

    check=True     führt die Vorab-Prüfungen des Starters aus. BLOCKIEREND (Platte, /proc, beim
                   Browser "flatpak ps"): im Thread aufrufen.
    planning=True  nur für Plan und Tests: liefert die Spec auch, wenn die Übernahme des Prefix
                   noch aussteht (sonst AdoptionRequired mit .src und .dst).
    """
    ctx = ctx or default_context()
    module = _module(entry)
    if module is proton and lutris_way.wanted(entry):
        # Startweg „wie in Lutris“ (E55): gewählt oder von der Startreparatur gesetzt
        spec = lutris_way.build(entry, ctx, check=check)
    elif module is proton:
        spec = proton.build(entry, ctx, check=check, planning=planning)
    else:
        spec = module.build(entry, ctx, check=check)
    if spec.kind not in APP_KINDS:
        raise LaunchError(TEXT_NO_LAUNCHER, "unbekannte Art %r" % spec.kind, reason="no-launcher")
    return spec


def preflight(entry, ctx: LaunchContext | None = None, *, planning: bool = False) -> list:
    """Vorab-Prüfungen ohne Spec. Rückgabe: Hinweise als deutsche Sätze (für das Ladebild).
    Harte Fehler als LaunchError. BLOCKIEREND wie build(check=True)."""
    ctx = ctx or default_context()
    module = _module(entry)
    if module is proton and lutris_way.wanted(entry):
        return []                          # Lutris prüft selbst; der Prefix wird nicht übernommen
    if module is proton:
        return proton.preflight(entry, ctx, planning=planning)
    return module.preflight(entry, ctx)


def needs_adoption(entry, ctx: LaunchContext | None = None) -> tuple | None:
    """(src, dst), wenn vor dem ersten Start der Lutris-Prefix kopiert werden muss, sonst None.

    Die Oberfläche zeigt dann vorher prefix.TEXT_ADOPT_HINT und ruft adopt() im Thread.
    Schnell (einige stat-Aufrufe)."""
    ctx = ctx or default_context()
    if _module_or_none(entry) is not proton or lutris_way.wanted(entry):
        return None                        # „wie in Lutris“ startet in Lutris' eigenem Prefix
    return prefix.needs_adoption(entry, ctx)


def _module_or_none(entry):
    try:
        return _module(entry)
    except LaunchError:
        return None


def prefix_info(entry, ctx: LaunchContext | None = None) -> dict | None:
    """Prefix-Entscheidung für Anzeige und Plan:
    {"path", "mode": "in-place"|"copy"|"own", "state", "source", "adopt": [src, dst]|None, "note"}"""
    ctx = ctx or default_context()
    if _module_or_none(entry) is not proton:
        return None
    plan = prefix.decide(entry, ctx)
    return {"path": plan.path, "mode": plan.mode, "state": plan.state, "source": plan.source,
            "adopt": list(plan.adopt) if plan.adopt else None, "note": plan.note}


def adopt(entry, ctx: LaunchContext | None = None, progress_cb=None, cancel=None) -> dict:
    """Die ausstehende Übernahme für diesen Eintrag ausführen. BLOCKIEREND (Sekunden bis Minuten).

    progress_cb(erledigt, gesamt) kommt aus dem aufrufenden Thread; cancel ist ein
    threading.Event. Rückgabe wie prefix.adopt_prefix; {"copied": False, …}, wenn nichts zu tun war.
    """
    ctx = ctx or default_context()
    pair = needs_adoption(entry, ctx)
    if not pair:
        plan = prefix.decide(entry, ctx) if _module_or_none(entry) is proton else None
        return {"copied": False, "dst": plan.path if plan else "", "src": "", "entries": 0,
                "seconds": 0.0, "marker": prefix.read_marker(plan.path) if plan else None}
    src, dst = pair
    return prefix.adopt_prefix(src, dst, progress_cb, cancel=cancel,
                               state=str(entry.detail.get("prefix_state") or ""),
                               entry_id=str(entry.id), proc_root=ctx.proc_root)


def register_all(register_launcher: Callable, ctx: LaunchContext | None = None, *,
                 auto_adopt: bool = False, on_progress: Callable | None = None) -> None:
    """Trägt die Starter in ein fremdes Register ein: register_launcher(source, fn).

    fn(entry, zweites=None) -> LaunchSpec. Ist das zweite Argument ein LaunchContext, gilt es;
    sonst (das Register des Kerns reicht sein App-Objekt durch) der hier übergebene ctx bzw.
    ein Vorgabe-Kontext. fn prüft vollständig (check=True) und ist damit BLOCKIEREND.

    auto_adopt=False  steht eine Prefix-Übernahme aus, wirft fn AdoptionRequired (.src, .dst);
                      der Aufrufer zeigt den Hinweis, ruft adopt() und versucht es erneut.
    auto_adopt=True   fn kopiert selbst (blockierend, Sekunden bis Minuten) und meldet über
                      on_progress(entry, erledigt, gesamt); der Hinweis vorab entfällt dann.
    """
    bound = ctx

    def launcher(entry, second=None) -> LaunchSpec:
        use = second if isinstance(second, LaunchContext) else (bound or default_context())
        if auto_adopt and needs_adoption(entry, use):
            # Erst prüfen (Platte da? Startdatei da?), dann kopieren
            preflight(entry, use, planning=True)
            report = (lambda done, total: on_progress(entry, done, total)) if on_progress else None
            adopt(entry, use, report)
        return build(entry, use)

    for source in SOURCES:
        register_launcher(source, launcher)


# ---------------------------------------------------------------------------------------------
# Plan-Lauf (nur lesend)

_SECRET = re.compile(r"(TOKEN|PASSWORD|PASSWD|SECRET|AUTH|COOKIE|KEY$)", re.I)


def _mask(key: str, value: str) -> str:
    """Werte, deren Name nach Zugangsdaten aussieht, nie ausgeben."""
    return "***" if _SECRET.search(key) else value


def is_handover(entry) -> bool:
    """Einträge, die der Kern startet (launch/__init__.py): Steam-Spiele und Steam-Verknüpfungen
    über couch-steam-game (E25, E57), die Kachel „Steam“ als Übergabe (E9)."""
    return entry.source == "steam" or str(entry.id) == "app:steam"


def plan_entry(entry, ctx: LaunchContext) -> dict:
    """Was für einen Eintrag geplant würde. Startet und kopiert nichts. Blockierend."""
    out = {"id": entry.id, "title": entry.title, "source": entry.source, "error": None,
           "error_detail": "", "error_reason": "", "warnings": [], "argv": None, "cwd": None,
           "env": {}, "unset": [], "kind": None, "freeze": None, "mouse": None, "polite_stop": None,
           "prelaunch": [], "prefix": None, "handover": None}
    if is_handover(entry):
        # Steam-Spiele: der Befehl für couch-steam-game (seit E57 ohne Übergabe; vorher brach hier
        # dict(None) den ganzen Plan ab, E99). Die Kachel „Steam“: nur die Übergabe
        from types import SimpleNamespace
        from .. import launch as core
        # Ersatz für das App-Objekt wie beim echten Start: Einstellungen aus ctx.config, HDR aus
        # app.wiring.launch (dort der LaunchContext mit dem Schnappschuss, wiring_launch). Ohne
        # gs: Gamescope gibt es im Plan nicht, und Starter fragen es nie (E99)
        app = SimpleNamespace(config=ctx.config, wiring=SimpleNamespace(launch=ctx))
        try:
            spec = core.steam_client_spec(entry) if entry.source != "steam" else core.steam_launcher(entry, app)
            out.update(kind=spec.kind, freeze=spec.freeze, mouse=spec.mouse)
            if spec.handover is not None:
                out["handover"] = dict(spec.handover)
            else:
                out.update(argv=spec.argv, env={k: _mask(k, v) for k, v in sorted(spec.env.items())})
        except LaunchError as err:
            out.update(error=err.message, error_detail=err.detail, error_reason=err.reason)
        return out
    try:
        out["prefix"] = prefix_info(entry, ctx)
        spec = build(entry, ctx, check=False, planning=True)
        out.update(argv=spec.argv, cwd=spec.cwd, unset=list(spec.unset), kind=spec.kind,
                   freeze=spec.freeze, mouse=spec.mouse, polite_stop=spec.polite_stop,
                   prelaunch=spec.prelaunch,
                   env={k: _mask(k, v) for k, v in sorted(spec.env.items())})
        out["warnings"] = preflight(entry, ctx, planning=True)
    except LaunchError as err:
        out["error"] = err.message
        out["error_detail"] = err.detail
        out["error_reason"] = err.reason
    return out


def load_entries(home: str) -> tuple:
    """(Einträge, Herkunft, Warnungen) aus couch.library.sources, wie sie die Bibliothek sieht. Ausdrückliches
    home: sonst läse die Bibliothek im Prüfmodus ihre Attrappen-Daten. Ohne cache_dir schreibt sie
    nichts. Blockierend. Einen eigenen Leser gibt es nicht mehr (E99): Er wich von der Bibliothek ab
    (Prefix-Zustand, Lutris-Umgebung), und der Plan führte die Fehlersuche in die falsche Richtung."""
    from ..library import sources
    found, warnings, _timings = sources.load_all(sources.SourceContext(home=home))
    # Steam-Spiele, Steam-Verknüpfungen und app:steam sind Übergaben: Der Plan nennt für sie nur
    # die Adresse (plan_entry)
    found = [e for e in found if e.source in SOURCES or is_handover(e)]
    return found, "couch.library.sources.load_all()", list(warnings)


def _print_plan(item: dict) -> None:
    import shlex
    print("== %s  [%s, %s]" % (item["title"], item["id"], item["source"]))
    if item["prefix"]:
        info = item["prefix"]
        print("   Prefix:   %s (%s, Zustand %s)" % (info["path"], info["mode"], info["state"]))
        print("             %s" % info["note"])
        if info["adopt"]:
            print("   Übernahme: %s -> %s" % tuple(info["adopt"]))
    if item["error"]:
        print("   FEHLER:   %s%s" % (item["error"], " [%s]" % item["error_reason"] if item.get("error_reason") else ""))
        if item["error_detail"]:
            print("             (%s)" % item["error_detail"])
    if item.get("handover"):
        print("   Übergabe: %s (zurück: %s)" % (item["handover"].get("uri") or "Steam Big Picture ohne Spiel",
                                              item["handover"].get("return")))
    if item["argv"] is None:
        print()
        return
    print("   Befehl:   %s" % " ".join(shlex.quote(str(a)) for a in item["argv"]))
    print("   cwd:      %s" % item["cwd"])
    print("   Art:      %s, anhalten=%s, maus=%s" % (item["kind"], item["freeze"], item["mouse"]))
    for key, val in item["env"].items():
        print("   %s=%s" % (key, shlex.quote(val)))
    if item["unset"]:
        print("   unset:    %s" % " ".join(item["unset"]))
    for command in item["prelaunch"]:
        print("   vorab:    %s" % " ".join(shlex.quote(str(a)) for a in command))
    if item["polite_stop"]:
        print("   beenden:  %s" % " ".join(shlex.quote(str(a)) for a in item["polite_stop"]))
    for text in item["warnings"]:
        print("   Hinweis:  %s" % text)
    print()


def main(argv: list | None = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python3 -m couch.launch.builders",
                                 description="Zeigt je Eintrag, was gestartet würde. Startet und kopiert nichts.")
    ap.add_argument("--plan", action="store_true", help="Plan für die echte Bibliothek ausgeben")
    ap.add_argument("--json", action="store_true", help="als JSON")
    ap.add_argument("--only", default="", help="nur Einträge, deren Titel oder ID diesen Text enthält")
    ap.add_argument("--hdr", action="store_true", help="so rechnen, als gäbe die Sitzung HDR aus")
    ap.add_argument("--screen", default="3840x2160", help="angenommene Bildschirmgröße BxH")
    ap.add_argument("--home", default="", help="anderes Home (Tests)")
    ap.add_argument("--prefix-root", default="", help="Ordner für eigene Prefixe (nur Anzeige)")
    args = ap.parse_args(argv)
    if not args.plan:
        ap.print_help()
        return 2
    try:
        width, height = (int(n) for n in args.screen.lower().split("x", 1))
    except ValueError:
        ap.error("--screen erwartet BxH, z. B. 3840x2160")
    ctx = LaunchContext(home=args.home, prefix_root=args.prefix_root,
                        hdr_active=lambda: args.hdr, screen_size=lambda: (width, height))
    entries, origin, warnings = load_entries(ctx.home)
    needle = args.only.casefold()
    if needle:
        entries = [e for e in entries if needle in e.title.casefold() or needle in e.id.casefold()]
    plans = [plan_entry(e, ctx) for e in entries]
    if args.json:
        json.dump({"origin": origin, "prefix_root": ctx.prefix_root_dir(), "warnings": warnings,
                   "plans": plans}, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return 0
    print("# Quelle der Einträge: %s" % origin)
    for text in warnings:
        print("# Bibliothek: %s" % text)
    print("# Prefix-Ordner: %s   HDR angenommen: %s   Bildschirm: %dx%d\n"
          % (ctx.prefix_root_dir(), "ja" if args.hdr else "nein", width, height))
    for item in plans:
        _print_plan(item)
    groups = {}
    for item in plans:
        if item["error"]:
            key = "Fehler"
        elif item.get("handover"):
            key = "Übergabe an Steam"
        elif item["source"] == "steam":
            key = "Steam (couch-steam-game)"
        elif item["prefix"]:
            key = {"in-place": "Proton an Ort und Stelle", "copy": "Proton mit Kopie",
                   "own": "Proton mit eigenem Prefix"}[item["prefix"]["mode"]]
        else:
            key = item["kind"] or item["source"]
        groups.setdefault(key, []).append(item["title"])
    print("# Zusammenfassung")
    for key in sorted(groups):
        print("#   %-28s %2d  %s" % (key, len(groups[key]), ", ".join(groups[key])))
    failed = [item for item in plans if item["error"]]
    pending = [item for item in plans if item["prefix"] and item["prefix"]["adopt"] and not item["error"]]
    print("#   Pläne: %d von %d Einträgen, Übernahmen ausstehend: %d, Fehler: %d"
          % (len(plans) - len(failed), len(plans), len(pending), len(failed)))
    for item in failed:
        print("#   scheitert: %s [%s]: %s" % (item["title"], item["id"], item["error"]))
    return 0


__all__ = ["AdoptCancelled", "AdoptionRequired", "DictConfig", "LaunchContext", "LaunchError",
           "LaunchSpec", "SOURCES", "adopt", "build", "default_context",
           "default_run_read", "needs_adoption", "plan_entry", "prefix_info", "preflight",
           "register_all"]

if __name__ == "__main__":
    sys.exit(main())
