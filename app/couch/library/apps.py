"""Quelle "app": die festen Programme der Konsole (Reiter „Apps“ und „Medien“, E140; Steam steht unter „Spiele“).

Nur lesend, blockierend. Im Normalfall nur ein paar Verzeichnisabfragen; ``flatpak info <id>``
über ctx.run_read wird nur gefragt, wenn es keinen der Flatpak-Ordner gibt (ungewohnte Installation).

Reihenfolge und IDs sind fest (die Reihenfolge ist die Vorgabe der Reihen im Home, E140):
  app:steam      Steam      Übergabe an Steam Big Picture (kein eigener Prozess, siehe launch/steam.py)
  app:bazaar     App Store  Bazaar, der Flathub-Store des Systems (/usr/bin/bazaar-daemon bzw. bazaar, E140) – nur, wenn es da ist
  app:browser    Browser    der Browser der Konsole (<app>/bin/couch-browser, E59), solange browser.app
                            "couch" ist (dann immer installiert); mit "chrome" das Flatpak com.google.Chrome
  app:desktop    Desktop    <app>/bin/couch-desktop – nur, wenn es startplasma-wayland gibt (E105)
  app:claude     Claude Code  <app>/bin/couch-terminal (E72) – nur, wenn claude im PATH oder in ~/.local/bin liegt
  app:discord    Discord    Flatpak com.discordapp.Discord (E139) – nur mit discord.enabled und nur, wenn installiert
  app:emudeck    EmuDeck    ~/Applications/EmuDeck.AppImage – nur in der Liste, wenn die Datei da ist
  app:youtube    YouTube    Flatpak rocks.shy.VacuumTube
  app:kodi       Kodi       Flatpak tv.kodi.Kodi
  app:jellyfin   Jellyfin   Flatpak org.jellyfin.JellyfinDesktop – nur, wenn installiert (E105)
  app:moonlight  Moonlight  Flatpak com.moonlight_stream.Moonlight – nur, wenn installiert (E105)
Dahinter, ausgeblendet (hidden): app:emu-<katalog-id> je installiertem Emulator aus dem Katalog des
Assistenten (E105). Sie erscheinen nirgends als Kachel; „<Name> öffnen“ unter Einstellungen › Spiele ›
Emulatoren startet sie über den normalen Weg (Maus-Modus, für Kerne, BIOS und Einstellungen).

E140: Jede feste App trägt die Marke ihrer Reihe im Home (ROWS): "games-row" (Steam), "apps-row" oder "media-row".
Die Seite ordnet nur danach, eine Liste von Namen gibt es dort nicht. Weitere Flatpaks liest library/flatpaks.py.

Ergebnis: Entry(kind="app", source="app", badge="", installed, available=installed,
  detail={"app": "youtube", "launcher": "flatpak"|"handover"|"script"|"appimage"|"browser"|"program",
          "flatpak": "<id>" bzw. "path": "<pfad>", "description": "<deutscher Satz>",
          "hint": "Kodi ist nicht installiert." (nur bei installed=False),
          "setup_id": "<katalog-id>" (nur fehlende YouTube/Kodi: „Installieren“ öffnet den Assistenten)})
Fehlende Apps mit setup_id tragen zusätzlich die Marke "setup:<katalog-id>" (tags gehen an die Seite).
"""
from __future__ import annotations

import os
import shutil

from .flatpaks import installed as flatpak_set_up
from .model import Entry
from .sources import sort_title

# (kennung, titel, art, ziel, beschreibung). library/steam.py, settings/system.py und wiring_settings.py
# lesen davon nur die ersten vier Felder (Verknüpfungen auf eigene Apps, Namen für Updates). Die Kachel
# „Browser“ steht hier mit dem Chrome-Flatpak; solange browser.app "couch" ist, startet der eigene Browser.
# E140: Reihenfolge wie im Home: „Apps“ (Bazaar vorn), dann „Medien“; „Dateien“ setzt die Seite hinter den Browser.
APPS = (
    ("steam", "Steam", "handover", "", "Steam Big Picture mit Shop, Freunden und deiner Steam-Bibliothek."),
    ("bazaar", "App Store", "program", "bazaar", "Apps und Spiele von Flathub installieren und aktualisieren (Bazaar)."),
    ("browser", "Browser", "flatpak", "com.google.Chrome", "Google Chrome, bedient mit dem Controller als Maus."),
    ("desktop", "Desktop", "script", "bin/couch-desktop", "Der KDE-Desktop, bedient mit dem Controller als Maus."),
    ("claude", "Claude Code", "script", "bin/couch-terminal", "Claude Code im Terminal, bedient mit dem Controller."),
    ("discord", "Discord", "flatpak", "com.discordapp.Discord",
     "Nachrichten und Sprachchat mit Freunden; läuft auf Wunsch im Hintergrund weiter."),
    ("emudeck", "EmuDeck", "appimage", "Applications/EmuDeck.AppImage", "Emulatoren einrichten und verwalten."),
    ("youtube", "YouTube", "flatpak", "rocks.shy.VacuumTube", "YouTube in der Ansicht für den Fernseher."),
    ("kodi", "Kodi", "flatpak", "tv.kodi.Kodi", "Mediacenter für Filme, Serien und Musik."),
    ("jellyfin", "Jellyfin", "flatpak", "org.jellyfin.JellyfinDesktop",
     "Filme und Serien von deinem eigenen Jellyfin-Server."),
    ("moonlight", "Moonlight", "flatpak", "com.moonlight_stream.Moonlight",
     "Spiele von einem anderen PC auf diesen Fernseher streamen."),
)
# E140: Reihe im Home je feste App (Marke "<reihe>-row"); was hier fehlt, steht unter „Apps“
ROWS = {"steam": "games", "youtube": "media", "kodi": "media", "jellyfin": "media", "moonlight": "media"}
# Bazaar (E140): Programm des Systems (RPM auf Bazzite). Der Eintrag der Desktop-Datei startet bazaar-daemon, der
# das Fenster als Kindprozess öffnet; fehlt der, das Programm selbst. Symbol aus /usr/share/icons (nur gelesen)
BAZAAR = ("bazaar-daemon", "bazaar")
BAZAAR_ICON = "io.github.kolunmi.Bazaar"
CHROME = "com.google.Chrome"
OWN_BROWSER = ("bin/couch-browser", "Der Browser der Konsole, bedient mit dem Controller.")
# E105: Kacheln, die nur erscheinen, wenn sie da sind; fehlende mit Katalog-ID zeigen „Installieren“
ONLY_INSTALLED = ("bazaar", "jellyfin", "moonlight", "desktop", "emudeck", "claude", "discord")
# E139: Kacheln, die nur mit ihrem Schalter erscheinen (aus: kein Eintrag, auch wenn das Programm installiert ist)
SWITCHED = {"discord": "discord.enabled"}
SETUP_IDS = {"youtube": "youtube", "kodi": "kodi"}


def flatpak_installed(ctx, app_id: str) -> bool:
    """Ist das Flatpak eingerichtet? Erst die Ordner (schnell), notfalls ``flatpak info``. Blockierend."""
    roots = [d for d in ctx.flatpak_dirs if os.path.isdir(os.path.join(d, "app"))]
    if roots:
        return flatpak_set_up(roots, app_id)     # current/active, wie überall (flatpaks.installed_at)
    try:
        code, _out, _err = ctx.run_read(["flatpak", "info", app_id], timeout=10)
    except Exception:
        return False
    return code == 0


def browser_app(ctx) -> str:
    """browser.app: "couch" (Vorgabe) oder "chrome"; am Kontext übersteuerbar (Tests)."""
    value = getattr(ctx, "browser_app", None)
    if not isinstance(value, str):
        from .. import setup
        value = setup.text("browser.app") or "couch"
    return "chrome" if value == "chrome" else "couch"


def _which(ctx, name: str) -> str | None:
    fn = getattr(ctx, "which", None)
    return (fn if callable(fn) else shutil.which)(name)


def has_claude(ctx) -> bool:
    if _which(ctx, "claude"):
        return True
    own = os.path.join(ctx.home, ".local/bin/claude")
    return os.path.isfile(own) and os.access(own, os.X_OK)


def has_plasma(ctx) -> bool:
    return bool(_which(ctx, "startplasma-wayland"))


def bazaar_program(ctx) -> str | None:
    """E140: Pfad von bazaar-daemon, sonst bazaar, sonst None (im Prüfmodus über ctx.which steuerbar)."""
    for name in BAZAAR:
        found = _which(ctx, name)
        if found:
            return found
    return None


def row_tag(key: str) -> str:
    """E140: Marke der Reihe im Home für eine feste App ("games-row", "apps-row", "media-row")."""
    return ROWS.get(key, "apps") + "-row"


def switched_on(ctx, key: str) -> bool:
    """E139: Ist der Schalter einer App aus SWITCHED an? Am Kontext übersteuerbar (ctx.switches, Tests), sonst die
    Einstellung über setup.flag (gebundene Config bzw. settings.json)."""
    setting = SWITCHED.get(key)
    if setting is None:
        return True
    switches = getattr(ctx, "switches", None)
    if isinstance(switches, dict) and setting in switches:
        return switches[setting] is True
    from .. import setup
    return setup.flag(setting, False)


def load(ctx) -> list[Entry]:
    """Die festen Apps mit installed/available. Blockierend; wirft nicht."""
    out = []
    for key, title, launcher, target, description in APPS:
        if not switched_on(ctx, key):
            continue                             # E139: ausgeschaltet, es gibt keine Kachel
        if key == "browser" and browser_app(ctx) != "chrome":
            launcher, (target, description) = "browser", OWN_BROWSER      # E59: der Browser der Konsole
        detail = {"app": key, "launcher": launcher, "description": description}
        if launcher == "flatpak":
            detail["flatpak"] = target
            installed = flatpak_installed(ctx, target)
        elif launcher == "program":
            detail["path"] = bazaar_program(ctx) or ""
            detail["icon_name"] = BAZAAR_ICON
            installed = bool(detail["path"])
        elif launcher == "handover":
            detail["path"] = os.path.join(ctx.steam_root, "steam.sh")
            installed = os.path.isfile(detail["path"])
        elif launcher in ("script", "browser"):
            # Das Skript gehört zum eigenen Release; ob es da ist, prüft der Starter (LaunchError).
            detail["path"] = os.path.join(ctx.app_dir, target)
            installed = (key != "desktop" or has_plasma(ctx)) and (key != "claude" or has_claude(ctx))
        else:
            detail["path"] = os.path.join(ctx.home, target)
            installed = os.path.isfile(detail["path"])
        if not installed and key in ONLY_INSTALLED:
            continue                             # erscheint nur, wenn es eingerichtet ist
        tags = [row_tag(key)]
        if not installed:
            detail["hint"] = "%s ist nicht installiert." % title
            if key in SETUP_IDS:
                detail["setup_id"] = SETUP_IDS[key]
                tags.append("setup:" + SETUP_IDS[key])
        out.append(Entry(id="app:" + key, title=title, kind="app", source="app", badge="",
                         sort_title=sort_title(title), available=installed, installed=installed,
                         tags=tags, detail=detail))
    out.extend(emulator_apps(ctx))
    return out


def emulator_apps(ctx) -> list[Entry]:
    """Ausgeblendete Einträge app:emu-<id> der installierten Emulatoren (E105, siehe oben)."""
    from ..setup import catalog
    out = []
    roots = [d for d in ctx.flatpak_dirs if os.path.isdir(os.path.join(d, "app"))]
    for emu in catalog.EMULATORS:
        if not flatpak_set_up(roots, emu["flatpak"]):
            continue
        out.append(Entry(id="app:emu-" + emu["id"], title=emu["title"], kind="app", source="app", badge="",
                         sort_title=sort_title(emu["title"]), hidden=True,
                         detail={"app": "emu-" + emu["id"], "launcher": "flatpak", "flatpak": emu["flatpak"],
                                 "description": "Emulator für Kerne, BIOS und Einstellungen, bedient mit dem "
                                                "Controller als Maus.", "emulator": emu["id"]}))
    return out
