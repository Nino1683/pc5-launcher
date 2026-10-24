"""Lesezeichen aus dem Google-Konto (E89): Chrome auf diesem PC hält sie in seinem Profil.

Wunsch 2026-10-21: „Ich hätte auch gerne meine Lesezeichen, die in meinem Google-Konto vorhanden
sind, in diesem Browser.“ Chrome legt die Lesezeichen des Kontos als AccountBookmarks ab (die nur
auf dem Gerät als Bookmarks, beides JSON). Der Browser liest sie nur (nie schreiben: die Dateien
gehören Chrome) und macht daraus eine Seite <data>/browser/bookmarks.html, bei jedem Start und bei
jedem Druck auf den Knopf neu. Neue Lesezeichen im Konto kommen an, sobald Chrome auf dem PC einmal
gelaufen ist und abgeglichen hat. Nur Standardbibliothek (testbar ohne GTK).
"""
from __future__ import annotations

import html
import json
import os
import time

from .prefs import BADGE_COLORS, _web_url, host_of

# Profile in der Reihenfolge der Suche; das erste mit Lesezeichen gilt
PROFILES = (".var/app/com.google.Chrome/config/google-chrome/Default", ".config/google-chrome/Default")
FILES = (("AccountBookmarks", True), ("Bookmarks", False))     # (Datei, aus dem Google-Konto)
ROOTS = ("bookmark_bar", "other", "synced")
MAX_ITEMS = 3000
MAX_DEPTH = 12


def sources(home: str) -> list[tuple[str, bool]]:
    """[(Pfad, aus dem Konto)] des ersten Chrome-Profils, das Lesezeichen hat."""
    for profile in PROFILES:
        found = [(os.path.join(home, profile, name), account) for name, account in FILES
                 if os.path.isfile(os.path.join(home, profile, name))]
        if found:
            return found
    return []


def parse(data: object, prefix: str = "") -> list[dict]:
    """Abschnitte [{title, items: [{title, url}]}] aus einer Lesezeichen-Datei von Chrome, in der
    Reihenfolge von Chrome. Ordner werden flach, ihr Titel ist der Pfad („Auto › Tuning“)."""
    roots = data.get("roots") if isinstance(data, dict) else None
    out: list[dict] = []
    if not isinstance(roots, dict):
        return out
    budget = [MAX_ITEMS]
    for key in ROOTS:
        node = roots.get(key)
        if isinstance(node, dict):
            name = str(node.get("name") or "").strip() or "Lesezeichen"
            # Die Leiste ist die Heimat der Ordner: ihr Name steht nicht vor jedem Unterordner
            _walk(node, prefix + name, "" if key == "bookmark_bar" else name, out, budget, prefix, 0)
    return out


def _walk(node: dict, title: str, path: str, out: list, budget: list, prefix: str, depth: int) -> None:
    items, folders = [], []
    for child in node.get("children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "url" and budget[0] > 0:
            url = _web_url(child.get("url"))            # javascript: und chrome:// gehen hier nicht
            if url:
                budget[0] -= 1
                items.append({"title": (str(child.get("name") or "").strip() or host_of(url))[:120], "url": url})
        elif child.get("type") == "folder" and depth < MAX_DEPTH:
            folders.append(child)
    if items:
        out.append({"title": title, "items": items})
    for folder in folders:
        name = str(folder.get("name") or "").strip() or "Ordner"
        sub = path + " › " + name if path else name
        _walk(folder, prefix + sub, sub, out, budget, prefix, depth + 1)


def load(home: str) -> dict:
    """{sections, count, stamp, account}: alle Lesezeichen des Profils, die des Kontos zuerst."""
    sections: list[dict] = []
    stamp, account = 0.0, False
    for path, from_account in sources(home):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            stamp = max(stamp, os.path.getmtime(path))
        except (OSError, ValueError):
            continue
        part = parse(data, "" if from_account else "Auf diesem Gerät · ")
        account = account or (from_account and bool(part))
        sections += part
    return {"sections": sections, "count": sum(len(s["items"]) for s in sections), "stamp": stamp,
            "account": account}


def render(book: dict) -> str:
    sections = book.get("sections") or []
    if not sections:
        body = ('<p class="empty">Keine Lesezeichen gefunden. Melde dich im Desktop-Modus in Chrome bei '
                'deinem Google-Konto an; danach stehen sie hier.</p>')
        return PAGE.replace("{{NAV}}", "").replace("{{SOURCE}}", "").replace("{{BODY}}", body)
    nav, parts, n = [], [], 0
    for i, sec in enumerate(sections):
        title = html.escape(sec["title"])
        nav.append('<a href="#s%d">%s <b>%d</b></a>' % (i, title, len(sec["items"])))
        tiles = []
        for item in sec["items"]:
            host = host_of(item["url"])
            letter = html.escape(next((c for c in item["title"] + host if c.isalnum()), "?").upper())
            tiles.append('<a class="bm" href="%s"><i style="background:%s">%s</i><div><b>%s</b><span>%s</span></div></a>'
                         % (html.escape(item["url"], quote=True), BADGE_COLORS[n % len(BADGE_COLORS)], letter,
                            html.escape(item["title"]), html.escape(host)))
            n += 1
        parts.append('<section id="s%d"><h2>%s</h2><div class="grid">%s</div></section>' % (i, title, "".join(tiles)))
    origin = "aus deinem Google-Konto" if book.get("account") else "aus Chrome auf diesem PC"
    source = "%d Lesezeichen %s" % (book.get("count") or 0, origin)
    if book.get("stamp"):
        source += ", Stand %s (zuletzt mit Chrome abgeglichen)" % time.strftime("%d.%m.%Y, %H:%M",
                                                                              time.localtime(book["stamp"]))
    return (PAGE.replace("{{NAV}}", '<nav>%s</nav>' % "".join(nav)).replace("{{SOURCE}}", html.escape(source))
            .replace("{{BODY}}", "\n".join(parts)))


PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Lesezeichen</title>
<style>
  :root { --bg: #0b0c10; --tile: #1b1e26; --tile-hover: #2b3140; --text: #eef1f6; --muted: #9aa1b0; --accent: #3d8bfd; }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--text); font-family: "Noto Sans", sans-serif; }
  main { max-width: 1480px; margin: 0 auto; padding: 36px 28px 80px; }
  h1 { margin: 0 0 6px; font-size: 40px; font-weight: 600; }
  p.source, p.empty { margin: 0 0 24px; font-size: 19px; color: var(--muted); }
  p.empty { font-size: 22px; margin-top: 40px; }
  nav { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 12px; }
  nav a { padding: 12px 20px; border-radius: 26px; background: var(--tile); color: var(--text); text-decoration: none;
          font-size: 20px; border: 3px solid transparent; }
  nav a b { color: var(--muted); font-weight: 400; margin-left: 6px; }
  nav a:hover, nav a:focus { background: var(--tile-hover); border-color: var(--accent); outline: none; }
  h2 { font-size: 26px; font-weight: 600; margin: 36px 0 14px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; }
  a.bm { display: flex; align-items: center; gap: 16px; padding: 14px 18px; min-height: 84px; border-radius: 18px;
         background: var(--tile); color: var(--text); text-decoration: none; border: 3px solid transparent; overflow: hidden; }
  a.bm:hover, a.bm:focus { background: var(--tile-hover); border-color: var(--accent); outline: none; }
  a.bm i { flex: none; width: 48px; height: 48px; border-radius: 14px; display: flex; align-items: center;
           justify-content: center; font-style: normal; font-size: 24px; font-weight: 700; color: #fff; }
  a.bm div { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  a.bm b, a.bm span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  a.bm b { font-size: 20px; font-weight: 600; }
  a.bm span { font-size: 16px; color: var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Lesezeichen</h1>
  <p class="source">{{SOURCE}}</p>
  {{NAV}}
  {{BODY}}
</main>
</body>
</html>
"""
