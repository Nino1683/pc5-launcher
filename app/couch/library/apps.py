"""Quelle "app": die festen Programme der Konsole (Reiter „Medien“).

Nur lesend, blockierend. Im Normalfall nur ein paar Verzeichnisabfragen; ``flatpak info <id>``
über ctx.run_read wird nur gefragt, wenn es keinen der Flatpak-Ordner gibt (ungewohnte Installation).

Reihenfolge und IDs sind fest:
  app:steam      Steam      Übergabe an Steam Big Picture (kein eigener Prozess, siehe launch/steam.py)
  app:youtube    YouTube    Flatpak rocks.shy.VacuumTube
  app:kodi       Kodi       Flatpak tv.kodi.Kodi
  app:jellyfin   Jellyfin   Flatpak org.jellyfin.JellyfinDesktop – nur, wenn installiert (E105)
  app:moonlight  Moonlight  Flatpak com.moonlight_stream.Moonlight – nur, wenn installiert (E105)
  app:browser    Browser    der Browser der Konsole (<app>/bin/couch-browser, E59), solange browser.app
                            "couch" ist (dann immer installiert); mit "chrome" das Flatpak com.google.Chrome
  app:desktop    Desktop    <app>/bin/couch-desktop – nur, wenn es startplasma-wayland gibt (E105)
  app:emudeck    EmuDeck    ~/Applications/EmuDeck.AppImage – nur in der Liste, wenn die Datei da ist
  app:claude     Claude Code  <app>/bin/couch-terminal (E72) – nur, wenn claude im PATH oder in ~/.local/bin liegt
Dahinter, ausgeblendet (hidden): app:emu-<katalog-id> je installiertem Emulator aus dem Katalog des
Assistenten (E105). Sie erscheinen nirgends als Kachel; „<Name> öffnen“ unter Einstellungen › Spiele ›
Emulatoren startet sie über den normalen Weg (Maus-Modus, für Kerne, BIOS und Einstellungen).

Ergebnis: Entry(kind="app", source="app", badge="", installed, available=installed,
  detail={"app": "youtube", "launcher": "flatpak"|"handover"|"script"|"appimage"|"browser",
          "flatpak": "<id>" bzw. "path": "<pfad>", "description": "<deutscher Satz>",
          "hint": "Kodi ist nicht installiert." (nur bei installed=False),
          "setup_id": "<katalog-id>" (nur fehlende YouTube/Kodi: „Installieren“ öffnet den Assistenten)})
Fehlende Apps mit setup_id tragen zusätzlich die Marke "setup:<katalog-id>" (tags gehen an die Seite).
"""
from __future__ import annotations

import os
import shutil

from .model import Entry
from .sources import sort_title

# (kennung, titel, art, ziel, beschreibung). library/steam.py, settings/system.py und wiring_settings.py
# lesen davon nur die ersten vier Felder (Verknüpfungen auf eigene Apps, Namen für Updates). Die Kachel
# „Browser“ steht hier mit dem Chrome-Flatpak; solange browser.app "couch" ist, startet der eigene Browser.
APPS = (
    ("steam", "Steam", "handover", "", "Steam Big Picture mit Shop, Freunden und deiner Steam-Bibliothek."),
    ("youtube", "YouTube", "flatpak", "rocks.shy.VacuumTube", "YouTube in der Ansicht für den Fernseher."),
    ("kodi", "Kodi", "flatpak", "tv.kodi.Kodi", "Mediacenter für Filme, Serien und Musik."),
    ("jellyfin", "Jellyfin", "flatpak", "org.jellyfin.JellyfinDesktop",
     "Filme und Serien von deinem eigenen Jellyfin-Server."),
    ("moonlight", "Moonlight", "flatpak", "com.moonlight_stream.Moonlight",
     "Spiele von einem anderen PC auf diesen Fernseher streamen."),
    ("browser", "Browser", "flatpak", "com.google.Chrome", "Google Chrome, bedient mit dem Controller als Maus."),
    ("desktop", "Desktop", "script", "bin/couch-desktop", "Der KDE-Desktop, bedient mit dem Controller als Maus."),
    ("emudeck", "EmuDeck", "appimage", "Applications/EmuDeck.AppImage", "Emulatoren einrichten und verwalten."),
    ("claude", "Claude Code", "script", "bin/couch-terminal", "Claude Code im Terminal, bedient mit dem Controller."),
)
CHROME = "com.google.Chrome"
OWN_BROWSER = ("bin/couch-browser", "Der Browser der Konsole, bedient mit dem Controller.")
# E105: Kacheln, die nur erscheinen, wenn sie da sind; fehlende mit Katalog-ID zeigen „Installieren“
ONLY_INSTALLED = ("jellyfin", "moonlight", "desktop", "emudeck", "claude")
SETUP_IDS = {"youtube": "youtube", "kodi": "kodi"}


def flatpak_installed(ctx, app_id: str) -> bool:
    """Ist das Flatpak eingerichtet? Erst die Ordner (schnell), notfalls ``flatpak info``. Blockierend."""
    roots = [d for d in ctx.flatpak_dirs if os.path.isdir(os.path.join(d, "app"))]
    for root in roots:
        # current -> <arch>/<zweig>, darin active -> <commit>; ein halb entferntes Flatpak hat das nicht
        if os.path.isdir(os.path.join(root, "app", app_id, "current", "active")):
            return True
    if roots:
        return False
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


def load(ctx) -> list[Entry]:
    """Die festen Apps mit installed/available. Blockierend; wirft nicht."""
    out = []
    for key, title, launcher, target, description in APPS:
        if key == "browser" and browser_app(ctx) != "chrome":
            launcher, (target, description) = "browser", OWN_BROWSER      # E59: der Browser der Konsole
        detail = {"app": key, "launcher": launcher, "description": description}
        if launcher == "flatpak":
            detail["flatpak"] = target
            installed = flatpak_installed(ctx, target)
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
        tags = []
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
        if not any(os.path.isdir(os.path.join(r, "app", emu["flatpak"], "current", "active")) for r in roots):
            continue
        out.append(Entry(id="app:emu-" + emu["id"], title=emu["title"], kind="app", source="app", badge="",
                         sort_title=sort_title(emu["title"]), hidden=True,
                         detail={"app": "emu-" + emu["id"], "launcher": "flatpak", "flatpak": emu["flatpak"],
                                 "description": "Emulator für Kerne, BIOS und Einstellungen, bedient mit dem "
                                                "Controller als Maus.", "emulator": emu["id"]}))
    return out
