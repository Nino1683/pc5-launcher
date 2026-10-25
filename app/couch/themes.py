"""Designs der Oberfläche (E113; Wunsch aus der Community 2026-10-25: „support for community themes“).

Ein Design ändert nur Farben: Es überschreibt Farb- und Verlaufs-Tokens aus app/web/css/tokens.css (Namen
--c-… und --g-…). Maße, Schrift und Zeiten bleiben, so kann kein Design das Layout zerlegen.

  * Eingebaut: BUILTIN (Kennung -> Name, Tokens). "" ist das Standard-Design (tokens.css unverändert).
  * Eigene Designs: <config>/themes/<name>.css (~/.config/couch/themes). Gelesen werden nur Deklarationen
    `--c-…: wert;` bzw. `--g-…: wert;` mit Namen, die tokens.css kennt; Selektoren, Kommentare und alles andere
    werden übergangen. Werte ohne url(), @-Regeln oder Escapes und nur mit den Farbfunktionen aus FUNCS: Ein
    Design aus dem Netz kann so nichts nachladen und nichts außer Farben ändern.

Die Einstellung ist ui.theme ("" | Kennung | "file:<name>"); was die Seite einsetzt, steht in sys.state.ui.themeCss
(wiring_settings.ui_state, js/core/store.js).
"""
from __future__ import annotations

import logging
import os
import re

from . import paths

_log = logging.getLogger("couch.themes")

FILE_PREFIX = "file:"
MAX_BYTES = 32 * 1024            # eine Datei mit allen Farb-Tokens und Kommentaren ist etwa 8 KB groß
MAX_VALUE = 600
_TOKENS_CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "css", "tokens.css")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
_DECL = re.compile(r"(--[a-z0-9-]{1,64})\s*:\s*([^;{}]*)")
_VALUE = re.compile(r"^[#\w\s.,%()+\-/*]+$")
_FUNC = re.compile(r"([a-z-]+)\(")
FUNCS = frozenset({"rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color-mix", "var", "calc",
                   "linear-gradient", "radial-gradient", "conic-gradient", "repeating-linear-gradient",
                   "repeating-radial-gradient"})


def _settings(a, b, c, top, bottom) -> str:
    """Verlauf der Einstellungen in der Form von tokens.css: Licht oben links, Schimmer unten, Grund."""
    return ("radial-gradient(60rem 46rem at 14%% -6%%, %s 0, %s 38%%, rgba(0,0,0,0) 72%%), "
            "radial-gradient(90rem 40rem at 24%% 104%%, %s 0, rgba(0,0,0,0) 70%%), "
            "linear-gradient(to bottom, %s, %s)") % (a, b, c, top, bottom)


def _palette(accent, ring_a, ring_b, bd, art, settings, library, loading, card, bg) -> dict:
    return {
        "--c-accent": accent,
        "--c-ring-a": ring_a, "--c-ring-b": ring_b,
        "--c-bd-a": bd[0], "--c-bd-b": bd[1], "--c-bd-c": bd[2],
        "--g-art-default": "radial-gradient(120%% 90%% at 78%% 38%%, %s 0, %s 38%%, %s 80%%)" % art,
        "--g-settings": _settings(*settings),
        "--g-library": "radial-gradient(70rem 60rem at 80%% 40%%, %s, rgba(0,0,0,0) 70%%), linear-gradient(%s, %s)"
                       % (library[0], library[1], library[1]),
        "--g-loading": "radial-gradient(90rem 70rem at 50%% 42%%, %s, rgba(0,0,0,0) 70%%), linear-gradient(%s, %s)"
                       % (loading[0], loading[1], loading[1]),
        "--g-cc-card": "linear-gradient(160deg, %s, %s)" % card,
        "--c-bg": bg,
    }


# Kennung -> (Name, Tokens). Alle dunkel wie das Standard-Design: Weiße Schrift bleibt überall lesbar.
BUILTIN: dict[str, tuple[str, dict]] = {
    "ocean": ("Ozean", _palette(
        "#1fa6e0", "#bfe9ff", "#5fb4d9",
        ("rgba(30,96,170,.60)", "rgba(24,150,170,.40)", "rgba(40,70,150,.40)"),
        ("#1d3a55", "#10202f", "#070b12"),
        ("rgba(80,160,210,.45)", "rgba(50,105,140,.22)", "rgba(40,120,150,.40)", "#08111b", "#0d1822"),
        ("rgba(30,80,120,.35)", "#070b10"), ("rgba(30,90,140,.45)", "#04070b"),
        ("#1b3144", "#0c141c"), "#070b12")),
    "emerald": ("Smaragd", _palette(
        "#23c48a", "#d4f7e6", "#6cc9a0",
        ("rgba(30,140,100,.55)", "rgba(120,170,90,.32)", "rgba(20,110,120,.38)"),
        ("#1c3d33", "#10231d", "#060c0a"),
        ("rgba(110,190,150,.42)", "rgba(60,130,100,.20)", "rgba(40,120,110,.38)", "#08130f", "#0d1a15"),
        ("rgba(40,100,80,.33)", "#070c0a"), ("rgba(30,110,80,.42)", "#040806"),
        ("#1a3a2f", "#0b1612"), "#060c0a")),
    "violet": ("Violett", _palette(
        "#9b6dff", "#ecdcff", "#a68be0",
        ("rgba(110,70,200,.55)", "rgba(190,100,170,.34)", "rgba(70,60,170,.38)"),
        ("#33245a", "#1c1433", "#0a0712"),
        ("rgba(170,130,230,.45)", "rgba(110,80,160,.22)", "rgba(120,90,170,.40)", "#100b1d", "#17112a"),
        ("rgba(80,50,120,.35)", "#09070f"), ("rgba(90,60,150,.45)", "#06040a"),
        ("#2c2244", "#110c1b"), "#0a0712")),
    "ember": ("Glut", _palette(
        "#ff7a3d", "#ffe2cc", "#e39a6c",
        ("rgba(180,70,40,.50)", "rgba(200,130,60,.34)", "rgba(140,40,70,.36)"),
        ("#4a2418", "#26140f", "#0d0706"),
        ("rgba(220,140,100,.45)", "rgba(150,90,70,.22)", "rgba(160,90,80,.38)", "#160c0a", "#1d1210"),
        ("rgba(110,55,40,.35)", "#0d0807"), ("rgba(130,60,40,.42)", "#080504"),
        ("#3a2219", "#170d0a"), "#0d0706")),
    "graphite": ("Graphit", _palette(
        "#d0d4dc", "#ffffff", "#9a9ca3",
        ("rgba(110,115,125,.42)", "rgba(140,140,145,.26)", "rgba(80,85,95,.32)"),
        ("#2a2b2f", "#18191c", "#0a0a0b"),
        ("rgba(170,170,175,.32)", "rgba(110,110,115,.18)", "rgba(110,110,120,.34)", "#101012", "#16161a"),
        ("rgba(70,70,75,.32)", "#0a0a0b"), ("rgba(70,72,80,.40)", "#050506"),
        ("#26272b", "#111214"), "#0a0a0b")),
}

_known: frozenset | None = None


def known_tokens() -> frozenset:
    """Farb- und Verlaufs-Tokens, die tokens.css erklärt (einmal gelesen)."""
    global _known
    if _known is None:
        try:
            with open(_TOKENS_CSS, encoding="utf-8") as fh:
                text = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
            _known = frozenset(n for n, _ in _DECL.findall(text) if n.startswith(("--c-", "--g-")))
        except OSError:
            _log.exception("tokens.css lesen")
            _known = frozenset()
    return _known


def user_dir() -> str:
    return os.path.join(paths.config_dir(), "themes")


def _clean_value(value: str) -> str | None:
    value = " ".join(value.replace("!important", "").split())
    if not value or len(value) > MAX_VALUE or not _VALUE.match(value) or "url" in value.lower():
        return None
    if any(f not in FUNCS for f in _FUNC.findall(value.lower())):
        return None
    if value.count("(") != value.count(")"):
        return None
    return value


def parse(text: str) -> tuple[dict, int]:
    """Text einer Design-Datei -> ({token: wert}, Zahl der übergangenen Deklarationen)."""
    text = re.sub(r"/\*.*?\*/", "", text or "", flags=re.S)
    text = re.sub(r"@[a-z-]+[^;{]*(;|\{[^}]*\})", "", text)        # @import, @media … samt Inhalt
    known, tokens, skipped = known_tokens(), {}, 0
    for name, raw in _DECL.findall(text):
        value = _clean_value(raw)
        if name in known and value is not None:
            tokens[name] = value
        else:
            skipped += 1
    return tokens, skipped


def css_of(tokens: dict) -> str:
    return ":root{%s}" % "".join("%s:%s;" % (n, v) for n, v in sorted(tokens.items())) if tokens else ""


def user_themes() -> list[str]:
    """Namen der eigenen Designs (Dateien <name>.css), sortiert."""
    try:
        names = os.listdir(user_dir())
    except OSError:
        return []
    out = []
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() == ".css" and _NAME.match(stem) and os.path.isfile(os.path.join(user_dir(), name)):
            out.append(stem)
    return sorted(out, key=str.lower)


def read_user(name: str) -> tuple[dict, int] | None:
    """Tokens eines eigenen Designs; None, wenn es die Datei nicht (mehr) gibt oder sie unlesbar ist."""
    if not _NAME.match(name or ""):
        return None
    path = os.path.join(user_dir(), name + ".css")
    try:
        if os.path.getsize(path) > MAX_BYTES:
            _log.warning("Design %s ist zu groß", name)
            return None
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse(fh.read())
    except OSError:
        return None


def valid(theme: str) -> bool:
    theme = str(theme or "")
    if theme == "" or theme in BUILTIN:
        return True
    return theme.startswith(FILE_PREFIX) and theme[len(FILE_PREFIX):] in user_themes()


def css_for(theme) -> str:
    """CSS für die Seite; "" = Standard (auch für Unbekanntes und gelöschte eigene Designs)."""
    theme = str(theme or "")
    if theme in BUILTIN:
        return css_of(BUILTIN[theme][1])
    if theme.startswith(FILE_PREFIX):
        got = read_user(theme[len(FILE_PREFIX):])
        return css_of(got[0]) if got else ""
    return ""
