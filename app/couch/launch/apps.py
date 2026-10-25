"""Die festen Apps: YouTube, Kodi, Jellyfin, Moonlight, Browser, Desktop, EmuDeck, Claude Code und die
Emulatoren selbst (Bauplan 4.8, E14, E15, E72, E105; 06).

app:steam gehört nicht hierher: Steam ist eine Übergabe, die der Kern selbst behandelt.

Blockierend: Der Browser fragt vor dem Start "flatpak ps" über ctx.run_read ab (läuft Chrome
schon woanders?). Alles andere sind wenige stat-Aufrufe.
"""
from __future__ import annotations

import os
import time

from . import flatpak
from .spec import LaunchError, LaunchSpec

YOUTUBE = "rocks.shy.VacuumTube"
KODI = "tv.kodi.Kodi"
CHROME = "com.google.Chrome"
JELLYFIN = "org.jellyfin.JellyfinDesktop"
MOONLIGHT = "com.moonlight_stream.Moonlight"
EMU_PREFIX = "app:emu-"

TEXT_CHROME_RUNNING = ("Chrome läuft schon an anderer Stelle, zum Beispiel im Desktop. "
                       "Schließe es dort und öffne den Browser dann noch einmal.")
TEXT_KODI_MISSING = "Kodi ist nicht installiert."
TEXT_UNKNOWN_APP = "Diese App lässt sich hier nicht starten."


def browser_scale(width: int, height: int) -> str:
    """--force-device-scale-factor: 1 bis 1080p, 1.5 bis unter 2160p, 2 ab 2160p."""
    if height <= 1080:
        return "1"
    if height < 2160:
        return "1.5"
    return "2"


def _screen(ctx) -> tuple:
    try:
        width, height = ctx.screen_size() if callable(ctx.screen_size) else ctx.screen_size
        width, height = int(width), int(height)
    except Exception:                      # der Schnappschuss kommt von außen (wiring_launch, E99)
        width, height = 0, 0
    if width < 320 or height < 240:
        width, height = 1920, 1080
    return width, height


def _youtube(entry, ctx, check: bool) -> LaunchSpec:
    # SteamOS=1 mit SteamGamepadUI=1 nicht exportieren: VacuumTube hielte sich für von Steam
    # gestartet, nähme nur noch Steams virtuelles Pad und hängte --no-sandbox an (06 2.1)
    # mouse="keys": couch übersetzt das Pad in Pfeiltasten, Enter, Escape (padmouse.KEYS_TV). VacuumTubes
    # eigene Pad-Abfrage (Gamepad-API im Bildtakt) kam im Game-Mode nicht an, Tasten über XTEST schon
    # (2026-10-15 am Fernseher geprüft). Im Control Center lässt sich weiter der Maus-Modus wählen.
    spec = flatpak.build(entry, ctx, YOUTUBE, ("--fullscreen",), kind="media", label="YouTube",
                         unset=("SteamOS", "SteamGamepadUI"), mouse="keys", check=check)
    # Sonst liest VacuumTube das Pad zusätzlich selbst: jede Richtung kam doppelt an (2026-10-15)
    spec.prelaunch = [[ctx.bin("couch-vacuumtube-prep")]]
    return spec


# Datei, die die nächste Kodi-Sitzung abspielt (Datei-Explorer, E62): id -> (Pfad, Zeitpunkt).
# Gilt OPEN_TTL Sekunden; nicht beim Lesen löschen, sonst verbrauchte schon die Vorab-Prüfung sie
PENDING_OPEN: dict = {}
OPEN_TTL = 30.0


def _kodi(entry, ctx, check: bool) -> LaunchSpec:
    if check and not flatpak.is_installed(KODI, ctx):
        raise LaunchError(TEXT_KODI_MISSING, "Flatpak %s fehlt" % KODI, reason="not-installed")
    # Ohne --standalone: Kodi ist ein Programm unter vielen, "Beenden" soll zum Home führen
    # und nicht den Rechner ausschalten (06 1.2)
    args = ["-fs", "--windowing=x11"]
    path, when, *_rest = PENDING_OPEN.get(str(entry.id), ("", 0.0))
    if path and time.monotonic() - when < OPEN_TTL and os.path.isfile(path) and not path.startswith("-"):
        args.append(path)
    return flatpak.build(entry, ctx, KODI, tuple(args), kind="media", label="Kodi", check=False)


def browser_args(ctx) -> list:
    width, height = _screen(ctx)
    args = [
        "--window-position=0,0",
        "--window-size=%d,%d" % (width, height),
        "--force-device-scale-factor=%s" % browser_scale(width, height),
        "--no-first-run",
        "--no-default-browser-check",
        # Reagiert Chrome nicht auf die Bitte (couch-flatpak-stop), kommt nach der Schonfrist
        # SIGKILL; ohne den Schalter fragt es dann beim nächsten Start, ob die Seiten
        # wiederhergestellt werden sollen (E29)
        "--hide-crash-restore-bubble",
    ]
    url = str(ctx.config.get("browser.home", "") or "").strip()
    if url and not url.startswith("-"):    # nie als Schalter an Chrome durchreichen
        args.append(url)
    return args


def _lang(ctx) -> str:
    from .. import i18n
    return i18n.language(getattr(ctx, "config", None))


def _browser(entry, ctx, check: bool) -> LaunchSpec:
    """Browser der Konsole (E59, Wunsch 2026-10-18: „ein für eine Spielekonsole optimierter Browser
    … Der jetzige ist einfach Chrome im Desktop-Modus“). Chrome bleibt mit browser.app="chrome"."""
    if str(ctx.config.get("browser.app", "couch") or "couch") == "chrome":
        return _chrome(entry, ctx, check)
    width, height = _screen(ctx)
    env = {"COUCH_BROWSER_SCALE": "2" if height >= 2000 else "1", "COUCH_LANG": _lang(ctx),
           "COUCH_BROWSER_DOWNLOADS": os.path.join(ctx.state_dir, "downloads.json")}
    for key, name in (("browser.home", "COUCH_BROWSER_HOME"), ("browser.search", "COUCH_BROWSER_SEARCH")):
        value = str(ctx.config.get(key, "") or "").strip()
        if value:
            env[name] = value
    url, when, *rest = PENDING_OPEN.get(str(entry.id), ("", 0.0))
    if url and time.monotonic() - when < OPEN_TTL and url.startswith("https://"):
        env["COUCH_BROWSER_OPEN"] = url           # Store (E68): Kaufen, Anmelden
        if rest and rest[0]:
            env["COUCH_BROWSER_CLOSE_AFTER"] = str(rest[0])   # nach der Anmeldung zu (E69)
    exe = ctx.bin("couch-browser")
    if check and not os.access(exe, os.X_OK):
        raise LaunchError("Der Browser der Konsole fehlt in dieser Fassung.", exe, reason="not-installed")
    return LaunchSpec(entry_id=entry.id, argv=[exe], env=env, cwd=ctx.home, kind="browser", freeze=False,
                      mouse=True, title=entry.title)


def _chrome(entry, ctx, check: bool) -> LaunchSpec:
    spec = flatpak.build(entry, ctx, CHROME, browser_args(ctx), kind="browser", label="Chrome",
                         mouse=True, check=check)
    if check:
        # Chrome benutzt das normale Profil des Nutzers, und ein Profil verträgt nur eine Instanz: Läuft
        # Chrome schon (etwa im verschachtelten Desktop), ginge das neue Fenster dort auf und
        # die Kachel wirkte tot (Kritik Befund 15)
        instances = flatpak.running(ctx, CHROME)
        if instances:
            raise LaunchError(TEXT_CHROME_RUNNING, "Instanzen: %s"
                              % ", ".join(r["instance"] for r in instances), reason="already-running")
    return spec


def desktop_stop_argv(ctx) -> list:
    """Befehl für LaunchSpec.polite_stop des Desktops. Rückgabewerte von couch-desktop --logout:
    0 = Plasma abgemeldet (bzw. die Abmeldung läuft), 3 = keine eigene Sitzung gefunden (dann
    genügt SIGTERM ans Hauptkind), 1 = der Aufruf ist gescheitert, 2 = Aufruffehler."""
    return [ctx.bin("couch-desktop"), "--logout"]


def _desktop(entry, ctx, check: bool) -> LaunchSpec:
    script = ctx.bin("couch-desktop")
    if check:
        if not os.path.isfile(script) or not os.access(script, os.X_OK):
            raise LaunchError("Der Desktop lässt sich nicht starten: Das Startskript fehlt.", script,
                              reason="not-installed")
        if not ctx.which("startplasma-wayland"):
            raise LaunchError("Der Desktop lässt sich nicht starten: Plasma wurde nicht gefunden.",
                              reason="not-installed")
    return LaunchSpec(
        entry_id=entry.id,
        argv=[script],
        # Nur hier läuft ein verschachtelter Compositor: Gamescopes Vulkan-Schicht fände darin
        # keine Gamescope-Oberfläche und löste eine Lawine aus zenity-Prozessen aus (06 7.2)
        env={"DISABLE_GAMESCOPE_WSI": "1"},
        unset=["ENABLE_GAMESCOPE_WSI"],
        cwd=ctx.home,
        kind="desktop",
        freeze=False,
        mouse=True,
        # Plasma über den Bus seiner eigenen Sitzung abmelden (E29); ein SIGTERM ans Skript
        # ließe dessen Aufräumen laufen, während Plasma noch lebt
        polite_stop=desktop_stop_argv(ctx),
        title=entry.title,
    )


def _emudeck(entry, ctx, check: bool) -> LaunchSpec:
    image = os.path.join(ctx.home, "Applications", "EmuDeck.AppImage")
    if check and (not os.path.isfile(image) or not os.access(image, os.X_OK)):
        raise LaunchError("EmuDeck ist nicht installiert.", image, reason="not-installed")
    return LaunchSpec(
        entry_id=entry.id,
        argv=[image],
        env={},
        unset=[],
        cwd=ctx.home,
        kind="tool",
        freeze=False,
        mouse=True,
        polite_stop=None,
        title=entry.title,
    )


def _claude(entry, ctx, check: bool) -> LaunchSpec:
    """Terminal für Claude Code (E72, Wunsch 2026-10-19: „… damit ich nicht immer in den Desktop muss,
    um dort das Terminal zu öffnen. Angepasst an Controller“). Das Pad kommt als feste Tasten
    (padmouse, profile "term"); was sie tun, entscheidet die Seite des Terminals."""
    exe = ctx.bin("couch-terminal")
    if check and not os.access(exe, os.X_OK):
        raise LaunchError("Das Terminal fehlt in dieser Fassung.", exe, reason="not-installed")
    _width, height = _screen(ctx)
    return LaunchSpec(entry_id=entry.id, argv=[exe], env={"COUCH_TERM_SCALE": "2" if height >= 2000 else "1",
                                                                    "COUCH_LANG": _lang(ctx)},
                      cwd=ctx.home, kind="tool", freeze=False, mouse="term", title=entry.title)


def _jellyfin(entry, ctx, check: bool) -> LaunchSpec:
    """E105: Jellyfin-Desktop, mit dem Pad als Maus (eigene Pad-Abfrage unbestätigt), nie angehalten."""
    return flatpak.build(entry, ctx, JELLYFIN, (), kind="media", label="Jellyfin", mouse=True, check=check)


def _moonlight(entry, ctx, check: bool) -> LaunchSpec:
    """E105: Moonlight streamt Spiele von einem anderen PC; Pad als Maus für die Menüs, nie angehalten."""
    return flatpak.build(entry, ctx, MOONLIGHT, (), kind="media", label="Moonlight", mouse=True, check=check)


def _emulator_app(entry, ctx, check: bool) -> LaunchSpec:
    """E105: ein Emulator selbst (Einstellungen › Spiele › Emulatoren › „<Name> öffnen“): für Kerne, BIOS und
    Einstellungen, mit dem Pad als Maus, nie angehalten. Nur Flatpaks aus dem Katalog des Assistenten."""
    from ..setup import catalog
    emu = catalog.emulator(str(entry.id)[len(EMU_PREFIX):])
    if emu is None:
        raise LaunchError(TEXT_UNKNOWN_APP, "kein Emulator für %s" % entry.id, reason="no-launcher")
    return flatpak.build(entry, ctx, emu["flatpak"], (), kind="tool", label=emu["title"], mouse=True, check=check)


_APPS = {
    "app:youtube": _youtube,
    "app:kodi": _kodi,
    "app:jellyfin": _jellyfin,
    "app:moonlight": _moonlight,
    "app:browser": _browser,
    "app:desktop": _desktop,
    "app:emudeck": _emudeck,
    "app:claude": _claude,
}


def build(entry, ctx, *, check: bool = True) -> LaunchSpec:
    """Entry der Quelle "app" -> LaunchSpec. Medien, Browser, Desktop und EmuDeck werden nie
    angehalten (freeze=False)."""
    fn = _APPS.get(str(entry.id)) or (_emulator_app if str(entry.id).startswith(EMU_PREFIX) else None)
    if fn is None:
        raise LaunchError(TEXT_UNKNOWN_APP, "kein Starter für %s" % entry.id, reason="no-launcher")
    return fn(entry, ctx, check)


def preflight(entry, ctx) -> list:
    """Wie build(check=True); Apps haben keine Hinweise, nur harte Fehler."""
    build(entry, ctx, check=True)
    return []
