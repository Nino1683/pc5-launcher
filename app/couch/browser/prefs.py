"""Optionen des Browsers (E61): Favoriten, Startseite, Suchmaschine, Werbeblocker, Zoom.

Stehen in <data>/browser/prefs.json; nur der Browser liest und schreibt sie. Die Startseite ist
eine erzeugte Datei (<data>/browser/start.html) mit den Favoriten als Kacheln; sie wird bei jeder
Änderung neu geschrieben und als file:// geladen. Nur Standardbibliothek (testbar ohne GTK).
"""
from __future__ import annotations

import html
import json
import os
import urllib.parse

from .. import jsontext

ZOOM_MIN, ZOOM_MAX, ZOOM_DEFAULT = 0.5, 3.0, 1.25
MAX_FAVORITES = 48
SEARCH_ENGINES = {           # id -> (Name, Adresse mit %s)
    "google": ("Google", "https://www.google.com/search?q=%s"),
    "duckduckgo": ("DuckDuckGo", "https://duckduckgo.com/?q=%s"),
    "bing": ("Bing", "https://www.bing.com/search?q=%s"),
    "startpage": ("Startpage", "https://www.startpage.com/do/search?q=%s"),
    "ecosia": ("Ecosia", "https://www.ecosia.org/search?q=%s"),
}
DEFAULT_FAVORITES = [
    {"title": "Google", "url": "https://www.google.com/"},
    {"title": "YouTube", "url": "https://www.youtube.com/"},
    {"title": "Wikipedia", "url": "https://de.wikipedia.org/"},
    {"title": "DuckDuckGo", "url": "https://duckduckgo.com/"},
    {"title": "Twitch", "url": "https://www.twitch.tv/"},
    {"title": "ProtonDB", "url": "https://www.protondb.com/"},
]
BADGE_COLORS = ("#4285f4", "#e62117", "#5a5f6b", "#de5833", "#9146ff", "#e7414b", "#1f9d7a", "#c47f12")


def _web_url(url: object) -> str:
    url = str(url or "").strip()
    return url if urllib.parse.urlsplit(url).scheme in ("http", "https") and len(url) <= 4096 else ""


def host_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


class Prefs:
    def __init__(self, path: str, home_default: str = "", search_default: str = ""):
        self.path = path
        self.zoom = ZOOM_DEFAULT
        self.home = _web_url(home_default)            # "" = Startseite mit den Favoriten
        self.search = "google"
        for key, (_name, pattern) in SEARCH_ENGINES.items():
            if search_default and search_default == pattern:
                self.search = key
        self.custom_search = search_default if search_default and search_default not in \
            [p for _n, p in SEARCH_ENGINES.values()] and "%s" in search_default else ""
        self.adblock = True
        self.favorites = [dict(f) for f in DEFAULT_FAVORITES]
        self.load()

    # ------------------------------------------------------------ Datei
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            z = float(data.get("zoom", self.zoom))
            if ZOOM_MIN <= z <= ZOOM_MAX:
                self.zoom = z
        except (TypeError, ValueError):
            pass
        if "home" in data:
            self.home = _web_url(data.get("home"))
        if data.get("search") in SEARCH_ENGINES:
            self.search = data["search"]
            self.custom_search = ""
        if isinstance(data.get("adblock"), bool):
            self.adblock = data["adblock"]
        favs = data.get("favorites")
        if isinstance(favs, list):
            out = []
            for f in favs:
                if isinstance(f, dict) and _web_url(f.get("url")) and not any(o["url"] == f["url"] for o in out):
                    out.append({"title": str(f.get("title") or host_of(f["url"]))[:80], "url": f["url"]})
            self.favorites = out[:MAX_FAVORITES]

    def save(self) -> bool:
        data = {"zoom": self.zoom, "home": self.home, "search": self.search, "adblock": self.adblock,
                "favorites": self.favorites}
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".neu"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps(data, indent=1))
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ Favoriten
    def is_favorite(self, url: str) -> bool:
        return any(f["url"] == url for f in self.favorites)

    def add_favorite(self, url: str, title: str = "") -> bool:
        url = _web_url(url)
        if not url or self.is_favorite(url) or len(self.favorites) >= MAX_FAVORITES:
            return False
        self.favorites.append({"title": (title or host_of(url)).strip()[:80] or url, "url": url})
        return True

    def remove_favorite(self, url: str) -> bool:
        before = len(self.favorites)
        self.favorites = [f for f in self.favorites if f["url"] != url]
        return len(self.favorites) != before

    def move_favorite(self, url: str, step: int) -> bool:
        i = next((n for n, f in enumerate(self.favorites) if f["url"] == url), -1)
        j = i + step
        if i < 0 or not 0 <= j < len(self.favorites):
            return False
        self.favorites[i], self.favorites[j] = self.favorites[j], self.favorites[i]
        return True

    # ------------------------------------------------------------ Startseite und Suche
    def set_home(self, url: str) -> bool:
        url = _web_url(url)
        if not url:
            return False
        self.home = url
        return True

    def reset_home(self) -> None:
        self.home = ""

    def search_pattern(self) -> str:
        return self.custom_search or SEARCH_ENGINES[self.search][1]

    def search_name(self) -> str:
        return "eigene" if self.custom_search else SEARCH_ENGINES[self.search][0]

    def next_search(self) -> str:
        keys = list(SEARCH_ENGINES)
        self.search = keys[(keys.index(self.search) + 1) % len(keys)] if self.search in keys else keys[0]
        self.custom_search = ""
        return self.search


def render_start(prefs: Prefs, bookmarks: tuple[str, int] | None = None, t=lambda text: text) -> str:
    """Startseite mit den Favoriten als Kacheln; bookmarks = (Adresse der Lesezeichen-Seite, Anzahl)
    zeigt darunter den Weg zu den Lesezeichen aus dem Google-Konto (E89)."""
    tiles = []
    for n, fav in enumerate(prefs.favorites):
        title = html.escape(fav["title"])
        host = html.escape(host_of(fav["url"]))
        letter = html.escape((fav["title"].strip() or host or "?")[0].upper())
        color = BADGE_COLORS[n % len(BADGE_COLORS)]
        tiles.append('<a class="tile" href="%s"><div class="badge" style="background:%s">%s</div>%s<span>%s</span></a>'
                     % (html.escape(fav["url"], quote=True), color, letter, title, host))
    body = "\n    ".join(tiles) if tiles else t('<p class="empty">Noch keine Favoriten. Im Menü (⋯) oben rechts: „Zu den Favoriten“.</p>')
    link = ""
    if bookmarks and bookmarks[1] > 0:
        link = t('<a class="bmlink" href="%s">★  Lesezeichen <span>%d</span></a>') % (
            html.escape(bookmarks[0], quote=True), bookmarks[1])
    return (t(START_TEMPLATE).replace("{{TILES}}", body).replace("{{SEARCH}}", html.escape(t(prefs.search_name())))
            .replace("{{BOOKMARKS}}", link))


START_TEMPLATE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Startseite</title>
<style>
  :root { --bg: #0b0c10; --tile: #1b1e26; --tile-hover: #2b3140; --text: #eef1f6; --muted: #9aa1b0; --accent: #3d8bfd; }
  * { box-sizing: border-box; }
  html, body { margin: 0; min-height: 100%; background: var(--bg); color: var(--text); font-family: "Noto Sans", sans-serif; }
  main { min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 36px; padding: 40px 24px; }
  h1 { margin: 0; font-size: 44px; font-weight: 600; }
  p.hint, p.empty { margin: 0; font-size: 22px; color: var(--muted); text-align: center; max-width: 960px; line-height: 1.5; }
  .tiles { display: grid; grid-template-columns: repeat(4, 240px); gap: 26px; }
  a.tile { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; height: 168px;
           border-radius: 22px; background: var(--tile); color: var(--text); text-decoration: none; font-size: 24px;
           font-weight: 600; border: 3px solid transparent; text-align: center; padding: 0 12px; overflow: hidden; }
  a.tile:hover, a.tile:focus { background: var(--tile-hover); border-color: var(--accent); outline: none; }
  a.tile span { font-size: 17px; font-weight: 400; color: var(--muted); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge { width: 60px; height: 60px; border-radius: 18px; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: 700; color: #fff; }
  a.bmlink { padding: 16px 30px; border-radius: 32px; background: var(--tile); color: var(--text); text-decoration: none;
             font-size: 23px; font-weight: 600; border: 3px solid transparent; }
  a.bmlink span { color: var(--muted); font-weight: 400; margin-left: 8px; }
  a.bmlink:hover, a.bmlink:focus { background: var(--tile-hover); border-color: var(--accent); outline: none; }
  @media (max-width: 1100px) { .tiles { grid-template-columns: repeat(3, 230px); } }
</style>
</head>
<body>
<main>
  <h1>Browser</h1>
  <p class="hint">Oben in die Leiste tippen (Kreuz) und mit Dreieck die Tastatur öffnen, um mit {{SEARCH}} zu suchen
    oder eine Adresse einzugeben. Favoriten und Startseite im Menü (⋯) oben rechts.</p>
  <div class="tiles">
    {{TILES}}
  </div>
  {{BOOKMARKS}}
</main>
</body>
</html>
"""
