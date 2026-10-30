"""Quelle "flatpaks" (E140): jede installierte Flatpak-App, die noch keine feste Kachel hat.

Wunsch 2026-10-29: „Bazaar möchte ich auch im pc5 Launcher wenn ich darin etwas installiere soll es auch im pc5
launcher erscheinen.“ Gelesen wird je Installation (ctx.flatpak_dirs: /var/lib/flatpak und ~/.local/share/flatpak,
im Prüfmodus der Attrappen-Baum) nur:
  <root>/app/<id>/current/active                         ist die App eingerichtet (wie apps.flatpak_installed)
  <root>/exports/share/applications/<id>.desktop         Name, Comment, Categories, NoDisplay, Terminal, Exec, Icon
  <root>/exports/share/icons/hicolor/<größe>/apps/<Icon>.png|.svg   Bild der Kachel
Nur lesend und blockierend (je App ein paar stat und eine kleine Datei); wirft nie.

Ausgelassen: Apps ohne Desktop-Datei, NoDisplay=true, Hidden=true, Terminal=true, Type ≠ Application; alles mit
fester Kachel (apps.APPS: YouTube, Kodi, Jellyfin, Moonlight, Discord; Chrome, solange es die Kachel „Browser“ ist);
die Emulatoren aus dem Katalog des Assistenten (die haben ihre ausgeblendeten Einträge app:emu-*); Steam als Flatpak
(die Kachel „Steam“ übergibt an Steam selbst); eigene Hilfs-Flatpaks (OWN_PREFIXES). Runtimes liegen unter runtime/
und werden gar nicht gelesen.

Einordnung nach Categories: „Game“ ohne „Emulator“, ohne Werkzeug-Kategorie (NOT_GAMES) und kein bekannter Launcher
(LAUNCHERS) → Spiel (kind "game": Spiele-Reihe, Bibliothek, Spielzeit, Bilder aus dem Netz); AudioVideo, Audio,
Video, Player, TV → Medien; alles andere (auch Emulatoren) → Apps.

Ergebnis: Entry(id="flatpak:<id>", source="flatpak", kind "game" | "app", tags ["apps-row" | "media-row"] bei Apps,
  detail={"flatpak", "installations": ["user", "system"] (die erste startet), "command": Wert von --command oder "",
          "args": Argumente aus Exec ohne Feldcodes, "description": Comment, "categories", "row", "launcher": "flatpak",
          "icon": größtes PNG ab 256 px, sonst SVG, sonst größtes PNG, "icon_png": größtes PNG, "icon_name", "created"})
"""
from __future__ import annotations

import os
import re
import shlex

from .model import Entry
from .sources import clean_text, sort_title

PREFIX = "flatpak:"
SYSTEM_ROOT = "/var/lib/flatpak"
STEAM = "com.valvesoftware.Steam"
# Eigene Hilfs-Flatpaks des PC5 Launchers (es gibt noch keine; sie bekämen nie eine Kachel)
OWN_PREFIXES = ("org.couch.", "io.github.pc5launcher.")
# Launcher und Werkzeuge mit der Kategorie Game: unter „Apps“, nicht als Spiel
LAUNCHERS = ("net.lutris.Lutris", "com.heroicgameslauncher.hgl", "com.usebottles.bottles", "com.vysp3r.ProtonPlus",
             "net.davidotek.pupgui2", "com.github.Matoking.protontricks")
NOT_GAMES = {"Emulator", "Utility", "System", "Settings", "Development", "PackageManager"}
MEDIA = {"AudioVideo", "Audio", "Video", "Player", "TV"}
APP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
_FIELD = re.compile(r"%[a-zA-Z]")
MAX_DESKTOP = 65536
MAX_ARGS = 16


def valid_id(app_id) -> bool:
    return isinstance(app_id, str) and len(app_id) <= 255 and bool(APP_ID.match(app_id))


def installation_kind(ctx, root: str) -> str:
    """"system" für /var/lib/flatpak (und was ctx.flatpak_system_dirs nennt, Tests), sonst "user"."""
    systems = [SYSTEM_ROOT] + list(getattr(ctx, "flatpak_system_dirs", None) or ())
    real = os.path.realpath(root)
    return "system" if any(real == os.path.realpath(s) for s in systems) else "user"


def roots(ctx) -> list:
    """[(Wurzel, "user"|"system")]: die Nutzer-Installation zuerst (flatpak run nimmt sie vor der des Systems)."""
    found = [(r, installation_kind(ctx, r)) for r in ctx.flatpak_dirs if os.path.isdir(os.path.join(r, "app"))]
    return sorted(found, key=lambda item: item[1] != "user")


def installed_in(ctx, app_id: str) -> list:
    """Installationen ("user", "system"), in denen die App eingerichtet ist; Nutzer zuerst. Nur stat."""
    if not valid_id(app_id):
        return []
    return [kind for root, kind in roots(ctx)
            if os.path.isdir(os.path.join(root, "app", app_id, "current", "active"))]


# ---------------------------------------------------------------- Desktop-Datei
def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append({"s": " ", "n": " ", "t": " ", "r": "", "\\": "\\"}.get(value[i + 1], value[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def read_desktop(path: str) -> dict | None:
    """Schlüssel der Gruppe [Desktop Entry] (Name[de] bleibt als eigener Schlüssel). None, wenn unlesbar."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_DESKTOP)
    except OSError:
        return None
    out: dict = {}
    group = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            group = line[1:-1]
            continue
        if group != "Desktop Entry" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(key.strip(), _unescape(value.strip()))
    return out if out else None


def localized(data: dict, key: str, lang: str) -> str:
    """Name bzw. Comment in der Sprache der Oberfläche (de: Name[de_DE], Name[de], sonst Name)."""
    region = {"de": "de_DE", "en": "en_US"}.get(lang, lang)
    for k in ("%s[%s]" % (key, region), "%s[%s]" % (key, lang), key):
        value = data.get(k)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _flag(data: dict, key: str) -> bool:
    return str(data.get(key, "")).strip().lower() == "true"


def split_list(value) -> list:
    return [p.strip() for p in str(value or "").split(";") if p.strip()]


def parse_exec(line: str, app_id: str) -> tuple:
    """(--command, Argumente) aus der Exec-Zeile einer exportierten Desktop-Datei:
    "/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=haruna --file-forwarding org.kde.haruna @@u %U @@"
    -> ("haruna", []). Von den Schaltern vor der Kennung zählt nur --command (nie --file-forwarding, E105); dahinter
    fallen Feldcodes (%U, %f …) und die Marken @@/@@u weg. Unlesbares -> ("", [])."""
    try:
        tokens = shlex.split(line or "")
    except ValueError:
        return "", []
    if len(tokens) < 3 or os.path.basename(tokens[0]) != "flatpak" or "run" not in tokens:
        return "", []
    at = tokens.index("run") + 1
    command = ""
    while at < len(tokens) and tokens[at] != app_id:
        if tokens[at].startswith("--command="):
            command = tokens[at][len("--command="):]
        at += 1
    if at >= len(tokens):
        return "", []
    if "\0" in command or len(command) > 512 or command.startswith("-"):
        command = ""
    args = [t for t in tokens[at + 1:] if t not in ("@@", "@@u") and not _FIELD.search(t)]
    args = [a.replace("%%", "%") for a in args if "\0" not in a and len(a) < 512][:MAX_ARGS]
    return command, args


def classify(categories, app_id: str = "") -> str:
    """Reihe nach den Kategorien: "games" | "media" | "apps" (siehe oben)."""
    cats = set(categories or ())
    if "Game" in cats and not cats & NOT_GAMES and app_id not in LAUNCHERS:
        return "games"
    if cats & MEDIA:
        return "media"
    return "apps"


def find_icons(root: str, name: str) -> tuple:
    """(bestes Bild, größtes PNG) unter <root>/exports/share/icons/hicolor; "" = keins. Bestes: PNG ab 256 px,
    sonst SVG (wird groß gerendert), sonst das größte PNG."""
    base = os.path.join(root, "exports", "share", "icons", "hicolor")
    try:
        sizes = os.listdir(base)
    except OSError:
        return "", ""
    pngs, svg = [], ""
    for size in sizes:
        if size == "scalable":
            for ext in (".svg", ".svgz"):
                path = os.path.join(base, size, "apps", name + ext)
                if not svg and os.path.isfile(path):
                    svg = path
            continue
        m = re.fullmatch(r"(\d+)x(\d+)(@\d+)?", size)
        path = os.path.join(base, size, "apps", name + ".png")
        if m and os.path.isfile(path):
            pngs.append((int(m.group(1)) * (int(m.group(3)[1:]) if m.group(3) else 1), path))
    pngs.sort()
    big = pngs[-1][1] if pngs else ""
    best = big if pngs and pngs[-1][0] >= 256 else (svg or big)
    return best, big


# ---------------------------------------------------------------- Quelle
def _language(ctx) -> str:
    value = getattr(ctx, "language", None)
    if isinstance(value, str) and value:
        return value
    from .. import i18n, setup

    class _Config:
        @staticmethod
        def get(key, default=None):
            return setup.setting(key, default)
    return i18n.language(_Config)


def skipped_ids(ctx) -> set:
    """Kennungen, die keine eigene Kachel bekommen (feste Kacheln, Emulatoren des Katalogs, Steam)."""
    from ..setup import catalog
    from . import apps
    out = {a[3] for a in apps.APPS if a[2] == "flatpak" and a[0] != "browser"}
    if apps.browser_app(ctx) == "chrome":
        out.add(apps.CHROME)
    out.update(e["flatpak"] for e in catalog.EMULATORS)
    out.add(STEAM)
    return out


def describe_app(ctx, root: str, app_id: str, lang: str) -> dict | None:
    """Was die Desktop-Datei über eine App sagt, oder None, wenn sie keine Kachel bekommt."""
    data = read_desktop(os.path.join(root, "exports", "share", "applications", app_id + ".desktop"))
    if not data or data.get("Type", "Application") != "Application":
        return None
    if _flag(data, "NoDisplay") or _flag(data, "Hidden") or _flag(data, "Terminal"):
        return None
    title = clean_text(localized(data, "Name", lang))
    if not title:
        return None
    command, args = parse_exec(data.get("Exec", ""), app_id)
    icon_name = str(data.get("Icon") or "").strip()
    if not icon_name.startswith(app_id) or os.sep in icon_name:
        icon_name = app_id                      # Flatpak exportiert nur Symbole mit der Kennung vorn
    icon, icon_png = find_icons(root, icon_name)
    if not icon and icon_name != app_id:
        icon_name = app_id
        icon, icon_png = find_icons(root, app_id)
    categories = split_list(data.get("Categories"))
    return {"title": title, "description": clean_text(localized(data, "Comment", lang)), "command": command,
            "args": args, "categories": categories, "row": classify(categories, app_id), "icon": icon,
            "icon_png": icon_png, "icon_name": icon_name}


def load(ctx) -> list[Entry]:
    """Alle erkannten Flatpak-Apps (siehe oben), nach Titel. Blockierend; wirft nicht."""
    skip = skipped_ids(ctx)
    lang = _language(ctx)
    seen: dict[str, dict] = {}
    for root, kind in roots(ctx):
        try:
            ids = sorted(os.listdir(os.path.join(root, "app")))
        except OSError as e:
            ctx.warn("flatpaks: %s nicht lesbar (%s)" % (root, e.strerror or "Fehler"))
            continue
        for app_id in ids:
            if not valid_id(app_id) or app_id in skip or app_id.startswith(OWN_PREFIXES):
                continue
            active = os.path.join(root, "app", app_id, "current", "active")
            if not os.path.isdir(active):
                continue                        # halb entfernt oder nur ein Zweig ohne Auswahl
            if app_id in seen:
                seen[app_id]["installations"].append(kind)
                continue
            info = describe_app(ctx, root, app_id, lang)
            if info is None:
                seen[app_id] = {"installations": [kind], "skip": True}
                continue
            try:
                info["created"] = os.stat(active).st_mtime
            except OSError:
                info["created"] = None
            info["installations"] = [kind]
            seen[app_id] = info
    out = []
    for app_id, info in seen.items():
        if info.get("skip"):
            continue
        game = info["row"] == "games"
        detail = {"flatpak": app_id, "launcher": "flatpak", "installations": info["installations"],
                  "command": info["command"], "args": info["args"], "description": info["description"],
                  "categories": info["categories"], "row": info["row"], "icon": info["icon"],
                  "icon_png": info["icon_png"], "icon_name": info["icon_name"], "created": info["created"]}
        out.append(Entry(id=PREFIX + app_id, title=info["title"], kind="game" if game else "app", source="flatpak",
                         badge="FLATPAK" if game else "", sort_title=sort_title(info["title"]), available=True,
                         installed=True, tags=[] if game else [info["row"] + "-row"], detail=detail))
    out.sort(key=lambda e: (e.sort_title, e.id))
    return out
