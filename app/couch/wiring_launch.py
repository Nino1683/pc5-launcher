"""Verdrahtung des Teils „Spiel starten“ (Bauplan E21, E24, E25, E31, E42).

Hängt die Starter (couch/launch) an das Register des Kerns und gibt der Shell, was sie für die
Prefix-Übernahme braucht. Die Starter selbst bleiben ohne GTK und ohne RPC; die Abläufe (Rückfrage,
Ladebild, Fehlerereignisse) stehen in couch/shell.py.

    setup_launchers(wiring)   von Wiring._launchers gerufen: Starter anmelden, wiring.launch setzen
    refresh_snapshot(wiring)  von Wiring._refresh_snapshot gerufen
    extend(wiring)            Haken: busy_check, Aufräumen nach dem Start, Ende des Home
    launch_entry(app, id)     für andere Teile („EmuDeck öffnen“ in den Einstellungen)

Starter laufen im Arbeits-Thread (Shell: app.background) und dürfen X nicht anfassen. HDR-Zustand
und Größe des Spiele-Bildschirms bekommen sie deshalb als Schnappschuss, den die Hauptschleife
nachführt.
"""
from __future__ import annotations

import time
from concurrent.futures import Future
from typing import Any, Callable

from . import launch as launch_mod
from . import log, paths, system
from .launch import builders, prefix
from .launch.spec import LaunchError, LaunchSpec

_log = log.get("wiring.launch")

ESTIMATE_BUDGET_S = 1.0         # so lange darf das Messen der Kopie die Rückfrage aufhalten
PROGRESS_STEP_S = 0.25          # höchstens so oft geht der Fortschritt der Kopie in die Hauptschleife
CLEANUP_AGE_S = 3600.0          # ältere Zwischenordner abgebrochener Kopien entfernt der Start

# Vorgaben der eigenen Schlüssel (E22); die Tabelle in config.py führt der Einstellungs-Teil
DEFAULTS = {
    "hints.mouse_mode_seen": False,     # Hinweis zur Belegung im Maus-Modus schon gezeigt
    "hints.cards_seen": {},             # Zeiger-Modus (E97): Hinweis zur Belegung gezeigt, je Spiel wie oft
    "hints.opti_seen": 0,               # OptiScaler-Menü (E118): Hinweis zur Belegung so oft gezeigt
}


# ---------------------------------------------------------------- Schnappschuss für die Starter
def refresh_snapshot(wiring: Any) -> None:
    """HDR-Ausgabe und Größe von Xwayland 1 festhalten (Hauptschleife). Dorthin gehen die
    Spiele und der Browser; nur wenn es keinen Spiel-Server gibt, zählt die Wurzel von Server 0."""
    gs = wiring.app.gs
    try:
        size = getattr(gs, "game_root_size", gs.root_size)()
        wiring._snapshot = {"hdr": bool(gs.hdr_output_active()),
                            "size": (int(size[0]), int(size[1]))}
    except Exception:
        _log.exception("Bildschirmdaten für die Starter")


def _default_proton(app: Any) -> str:
    """Standard-Proton der Einstellungen (wiring_settings.resolve_proton), späte Bindung:
    Fehlt der Teil oder scheitert er, sucht launch/proton.py selbst."""
    try:
        from . import wiring_settings
        resolve = getattr(wiring_settings, "resolve_proton", None)
        return str(resolve(app) or "") if callable(resolve) else ""
    except ImportError:
        return ""
    except Exception:
        _log.exception("Standard-Proton der Einstellungen")
        return ""


# ---------------------------------------------------------------- Starter anmelden
def make_context(wiring: Any) -> Any:
    app = wiring.app
    sources = wiring.sources
    return builders.LaunchContext(
        config=wiring.config,
        hdr_active=lambda: wiring._snapshot["hdr"],
        screen_size=lambda: wiring._snapshot["size"],
        data_dir=paths.data_dir(), state_dir=paths.state_dir(), app_dir=paths.app_dir(),
        # Im Prüfmodus zeigt das Home der Bibliothek in den Attrappen-Baum: Dort suchen dann
        # auch die Starter (eden-launch, Proton-Builds), nie im echten Home des Nutzers
        home=sources.home if sources is not None else paths.home(),
        run_read=read3,
        default_proton=lambda: _default_proton(app),
        # dieselben Flatpak-Ordner wie die Bibliothek: Sonst hielte der Starter im Prüfmodus für
        # installiert, was nur auf dem echten Rechner liegt (und umgekehrt)
        flatpak_roots=tuple(getattr(sources, "flatpak_dirs", None) or ()))


def read3(argv: Any, timeout: float = 10) -> tuple[int, str, str]:
    """Lesender Systemaufruf in der Form, die die Starter erwarten: (rc, stdout, stderr)."""
    r = system.run_read([str(a) for a in argv], timeout=float(timeout))
    return r.returncode, r.stdout or "", r.stderr or ""


def setup_launchers(wiring: Any) -> None:
    """Starter für proton, local, native, switch, wiiu und app anmelden; Steam-Spiele und
    Steam-Verknüpfungen bleiben beim Starter des Kerns (Übergabe, launch.steam_launcher)."""
    app = wiring.app
    refresh_snapshot(wiring)
    app.gs.watch(lambda what: wiring._refresh_snapshot())
    app.config.on_change("display.", lambda key, value: wiring._refresh_snapshot())

    ctx = make_context(wiring)
    wiring.launch = ctx

    # E24: nie ohne Rückfrage kopieren. Die Starter melden eine ausstehende Übernahme
    # (AdoptionRequired); die Shell fragt, kopiert dann über adopt() im Arbeits-Thread und baut
    # den Auftrag danach neu.
    found: dict[str, Callable] = {}
    builders.register_all(lambda source, fn: found.__setitem__(source, fn), ctx, auto_adopt=False)
    for source, fn in found.items():
        launch_mod.register_launcher(source, _wrap(source, fn))

    app.shell.adopt_prefix = lambda entry, cancel: adopt(wiring, entry, cancel)


def _wrap(source: str, fn: Callable) -> Callable:
    def launcher(entry: Any, second: Any = None) -> LaunchSpec:
        if source == "app" and entry.id == launch_mod.STEAM_CLIENT_ID:
            # Steam Big Picture ist kein Prozess unter uns, sondern die Übergabe (E9)
            return launch_mod.steam_client_spec(entry)
        try:
            return fn(entry, second)
        except prefix.AdoptionRequired as needed:
            # Wir sind schon im Arbeits-Thread: Die Größe der Kopie für die Rückfrage gleich
            # mitmessen, aber nur kurz (schlafende Platte, riesiger Prefix -> dann ohne Größe)
            try:
                needed.info = prefix.copy_estimate(needed.src, needed.dst, ESTIMATE_BUDGET_S)
            except Exception:
                _log.exception("Größe der Kopie für %s", entry.id)
            raise
    return launcher


def adopt(wiring: Any, entry: Any, cancel: Any) -> Any:
    """Prefix-Übernahme für die Shell. BLOCKIEREND (Sekunden bis Minuten), läuft im
    Arbeits-Thread. Der Fortschritt geht gebremst in die Hauptschleife (shell.adopt_progress)."""
    shell = wiring.app.shell
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - last[0] < PROGRESS_STEP_S:
            return
        last[0] = now
        try:
            wiring.main.post(shell.adopt_progress, entry.id, done, total)
        except Exception:               # das Home endet gerade: die Kopie bricht gleich selbst ab
            pass
    return builders.adopt(entry, wiring.launch, progress, cancel)


# ---------------------------------------------------------------- Haken (E42)
def cleanup_prefixes(wiring: Any) -> list:
    """Zwischenordner abgebrochener Übernahmen entfernen. BLOCKIEREND, im Arbeits-Thread."""
    ctx = getattr(wiring, "launch", None)
    if ctx is None:
        return []
    try:
        removed = prefix.cleanup_unfinished(ctx.prefix_root_dir(), CLEANUP_AGE_S)
    except Exception:
        _log.exception("Aufräumen unfertiger Prefix-Kopien")
        return []
    for path in removed:
        _log.info("unfertige Prefix-Kopie entfernt: %s", path)
    return removed


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if callable(add):
        add(dict(DEFAULTS))
    shell = getattr(app, "shell", None)
    if shell is None:
        return

    # Läuft eine Prefix-Kopie, würde Ausschalten, Abmelden oder die Übergabe sie abbrechen
    wiring.busy_checks.append(shell.copy_sentence)

    # Nach dem Bereit-Signal, nicht davor: Der Start wartet auf keine Platte
    wiring.after_ready.append(lambda: wiring._bg(cleanup_prefixes, wiring))

    def stop_preparing() -> None:
        # Am Ende des Home darf keine Kopie weiterlaufen: Sonst hielte ihr Arbeits-Thread das
        # Prozessende auf, bis der Supervisor hart beendet. Die Kopie räumt nach dem Abbruch
        # ihren Zwischenordner selbst weg.
        count = shell.cancel_preparing()
        if count:
            _log.info("Home endet: %d Start(s) in Vorbereitung abgebrochen", count)
    wiring.shutdown_hooks.append(stop_preparing)


# ---------------------------------------------------------------- für andere Teile
def launch_entry(app: Any, entry_id: str) -> None:
    """Startet einen Eintrag über die Shell, so als hätte man seine Kachel gedrückt (für
    „EmuDeck öffnen“ u. Ä.). Aus jedem Thread rufbar; kehrt zurück, sobald der Start angenommen
    ist, und wartet nicht auf das Fenster.

    Wirft LaunchError (deutscher Satz), wenn der Start sofort abgelehnt wird: Eintrag fehlt,
    nicht installiert, Platte weg, die Konsole wird beendet, oder ein anderes Spiel läuft noch
    (hier wird nie ungefragt eines geschlossen). Was sich erst beim Vorbereiten zeigt, kommt
    wie bei jeder Kachel als Ereignis app.error bei der Oberfläche an."""
    shell = getattr(app, "shell", None)
    if shell is None:
        raise LaunchError("Starten ist gerade nicht möglich.", "keine Shell", reason="unavailable")

    def start() -> Any:
        return shell.launch(str(entry_id))

    wiring = getattr(app, "wiring", None)
    main = getattr(wiring, "main", None)
    result = main.call(start) if main is not None else start()
    if isinstance(result, Future):
        return
    if isinstance(result, dict) and result.get("needsConfirm"):
        other = (result.get("other") or {}).get("title") or "Ein anderes Spiel"
        raise LaunchError("„%s“ läuft noch. Schließe es zuerst." % other, reason="other-running")
