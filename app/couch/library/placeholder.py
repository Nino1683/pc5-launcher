"""Erzeugte Bilder für Einträge ohne eigenes Bild und Kacheln für die Apps.

Eigenständig: nur PIL, keine GLib-Schleife, kein Netz, kein Unterprozess. Alle ``render_*``
liefern ein PIL-Bild (rein, für Tests), alle ``make_*`` schreiben es atomar in eine Datei.
Blockierend: ein Bild kostet 5–60 ms (Hintergrund 1920x1080 am längsten).

Aussehen: ruhiger, dunkler Farbverlauf, dessen Farbton aus dem Titel abgeleitet ist (derselbe
Titel bekommt immer dieselbe Farbe), Titel in Noto Sans unten links, Marke oben links.
Apps bekommen eine neutrale Systemkachel mit einem selbst gezeichneten Liniensymbol oder dem
Symbol, das das Programm selbst installiert hat. Fremde Logos werden hier nicht nachgezeichnet.
"""
from __future__ import annotations

import colorsys
import functools
import hashlib
import math
import os
import re
import unicodedata
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageMath

# Steckt im Dateinamen-Hash der erzeugten Bilder: bei jeder Änderung am Aussehen hochzählen,
# dann erneuern sich die Platzhalter von selbst.
RECIPE = 3

TILE_SIZE = (512, 512)
COVER_SIZE = (600, 900)
HERO_SIZE = (1920, 1080)

APP_SYMBOLS = ("steam", "play", "film", "globe", "desktop", "handheld", "terminal", "apps")

# E104: dazu die Schriften des Rechners im Flatpak-Sandkasten (/run/host/fonts) und, wenn nirgends
# Noto liegt, die mitgelieferte variable Noto Sans (app/web/fonts, OFL) vor den Ersatzschriften
_FONT_DIRS = (
    "/usr/share/fonts/google-noto",
    "/usr/share/fonts/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
    os.path.expanduser("~/.local/share/fonts"),
    "/run/host/fonts/google-noto",
    "/run/host/fonts/noto",
    "/run/host/fonts/truetype/noto",
    "/run/host/fonts/opentype/noto",
)
_FONT_FILES = {
    "light": "NotoSans-Light.ttf",
    "regular": "NotoSans-Regular.ttf",
    "medium": "NotoSans-Medium.ttf",
    "semibold": "NotoSans-SemiBold.ttf",
    "bold": "NotoSans-Bold.ttf",
}
_OWN_FONT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                         "web", "fonts", "NotoSans-VF.ttf")      # <release>/app/web/fonts (E103)
_VARIABLE_FONTS = (
    "/usr/share/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/usr/share/fonts/noto/NotoSans[wght].ttf",
    "/run/host/fonts/google-noto-vf/NotoSans[wght].ttf",
    "/run/host/fonts/noto/NotoSans[wght].ttf",
    _OWN_FONT,
)
_VARIABLE_WEIGHT = {"light": 300, "regular": 400, "medium": 500, "semibold": 600, "bold": 700}
_FALLBACK_FONTS = (
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
)


# ------------------------------------------------------------------ Schrift

@functools.lru_cache(maxsize=None)
def _font_path(weight: str) -> tuple[str, int]:
    """(Datei, Achsenwert). Achsenwert 0 = feste Schnittdatei, sonst variable Schrift."""
    name = _FONT_FILES.get(weight, _FONT_FILES["regular"])
    for folder in _FONT_DIRS:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path, 0
    for path in _VARIABLE_FONTS:
        if os.path.isfile(path):
            return path, _VARIABLE_WEIGHT.get(weight, 400)
    for path in _FALLBACK_FONTS:
        if os.path.isfile(path):
            return path, 0
    return "", 0


@functools.lru_cache(maxsize=256)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    """Noto Sans im gewünschten Schnitt; fehlt sie, eine Ersatzschrift (nie eine Ausnahme)."""
    path, axis = _font_path(weight)
    if path:
        try:
            f = ImageFont.truetype(path, size)
            if axis:
                try:
                    f.set_variation_by_axes([axis])
                except (OSError, ValueError, AttributeError):
                    pass
            return f
        except OSError:
            pass
    return ImageFont.load_default(size)


def font_family() -> str:
    """Name der tatsächlich benutzten Schrift (für den Bericht)."""
    try:
        return " ".join(font("semibold", 20).getname())
    except (AttributeError, OSError):
        return "Ersatzschrift"


# ------------------------------------------------------------------ Farbe

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_hue(title: str) -> float:
    """Farbton 0…1, fest an den (normalisierten) Titel gebunden."""
    digest = hashlib.sha1(_norm(title).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 2 ** 32


def _rgb(h: float, lightness: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, lightness, s)
    return int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)


def palette(title: str) -> dict:
    """Farben eines Platzhalters: gedeckt und dunkel, damit weiße Schrift überall lesbar bleibt."""
    h = title_hue(title)
    # Gelb/Grün wirkt bei gleicher Helligkeit greller als Blau: dort etwas zurücknehmen
    glare = 0.5 + 0.5 * math.cos((h - 0.22) * 2 * math.pi)
    sat = 0.50 - 0.16 * glare
    return {
        "hue": h,
        "top": _rgb(h, 0.34 - 0.05 * glare, sat),
        "bottom": _rgb(h + 0.07, 0.10, sat + 0.06),
        "glow": _rgb(h - 0.04, 0.56, sat + 0.08),
        "accent": _rgb(h - 0.02, 0.74, 0.60),
    }


def _mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def smooth_upscale(small: Image.Image, size, noise: float = 0.6) -> Image.Image:
    """Vergrößert ein kleines, weiches RGB-Bild stufenlos.

    In Gleitkomma vergrößert und erst zuletzt mit feinem Rauschen auf 8 Bit gebracht: sonst
    zeigt der Fernseher in dunklen Verläufen Stufen und ein Gittermuster.
    """
    return _upscale_planes([chan.convert("F") for chan in small.convert("RGB").split()], size, noise)


def _backdrop(size, top, bottom, glow, glow_at=(0.18, 0.10), glow_strength=0.42, noise=0.6) -> Image.Image:
    """Schräger Verlauf mit weichem Lichtfleck; klein gerechnet, dann stufenlos vergrößert."""
    w, h = size
    gw = 48
    gh = max(8, round(gw * h / w))
    chans = ([], [], [])
    gx, gy = glow_at[0] * (gw - 1), glow_at[1] * (gh - 1)
    reach = 0.75 * math.hypot(gw, gh)
    for y in range(gh):
        ty = y / (gh - 1)
        for x in range(gw):
            tx = x / (gw - 1)
            t = min(1.0, max(0.0, 0.30 * tx + 0.80 * ty))
            t = t * t * (3 - 2 * t)                     # weicher Übergang statt linearer Rampe
            c = _mix(top, bottom, t)
            d = math.hypot(x - gx, y - gy) / reach
            c = _mix(c, glow, glow_strength * max(0.0, 1.0 - d) ** 2.2)
            for i in range(3):
                chans[i].append(c[i])
    planes = []
    for values in chans:
        plane = Image.new("F", (gw, gh))
        plane.putdata(values)
        planes.append(plane)
    return _upscale_planes(planes, size, noise)


def _upscale_planes(planes, size, noise: float) -> Image.Image:
    w, h = size
    grain = Image.effect_noise((w, h), noise).convert("F") if noise > 0 else None
    out = []
    for plane in planes:
        big = plane.resize((w, h), Image.BICUBIC)
        if grain is not None:
            big = ImageMath.lambda_eval(lambda a: a["x"] + a["n"] - 127.5, x=big, n=grain)
        else:
            big = ImageMath.lambda_eval(lambda a: a["x"] + 0.5, x=big)
        out.append(big.convert("L"))
    return Image.merge("RGB", out)


# ------------------------------------------------------------------ Textsatz

@dataclass
class TextLayout:
    lines: list
    font_size: int
    line_height: int
    width: int            # breiteste Zeile (Vorschub)
    height: int           # Zeilen * Zeilenhöhe
    truncated: bool       # Text musste mit „…“ gekürzt werden
    split: bool           # ein Wort musste getrennt werden


def _tokens(title: str) -> list:
    """Wörter; an Binde- und Schrägstrichen darf zusätzlich umbrochen werden."""
    out = []
    for wi, word in enumerate((title or "").split()):
        parts = re.findall(r"[^-/–—]+[-/–—]*|[-/–—]+", word)
        for pi, part in enumerate(parts):
            out.append((part, " " if (pi == 0 and wi > 0) else ""))
    return out


def _wrap(tokens, fnt, max_w: float, allow_split: bool):
    """Füllt Zeilen von links. Rückgabe (Zeilen, getrennt?) oder None, wenn ein Wort nicht passt."""
    lines, line, did_split = [], "", False
    for tok, glue in tokens:
        cand = (line + glue + tok) if line else tok
        if fnt.getlength(cand) <= max_w:
            line = cand
            continue
        if line:
            lines.append(line)
        line = tok
        while fnt.getlength(line) > max_w:
            if not allow_split:
                return None
            # Zu langes Wort (deutsche Zusammensetzung): so viele Zeichen wie passen, mit Trennstrich
            k = len(line) - 1
            while k > 1 and fnt.getlength(line[:k] + "-") > max_w:
                k -= 1
            k = max(1, k)
            head = line[:k]
            lines.append(head + ("-" if head[-1].isalnum() else ""))
            line = line[k:]
            did_split = True
    if line:
        lines.append(line)
    return lines, did_split


def _ellipsize(text: str, fnt, max_w: float) -> str:
    text = text.rstrip(" -–—:,;/")
    while text and fnt.getlength(text + "…") > max_w:
        text = text[:-1].rstrip(" -–—:,;/")
    return (text + "…") if text else "…"


def layout_title(title: str, box_w: int, box_h: int, weight: str = "semibold", max_size: int = 60,
                 min_size: int = 30, max_lines: int = 5, leading: float = 1.18) -> TextLayout:
    """Setzt einen Titel in ein Feld: erst kleiner werden, dann Wörter trennen, zuletzt kürzen.

    Garantie: keine Zeile ist breiter als box_w, der Block nicht höher als box_h.
    """
    title = " ".join((title or "").split()) or "?"
    tokens = _tokens(title)
    safe_w = box_w * 0.985                        # Luft für Überhänge einzelner Buchstaben
    size = max_size
    while size >= min_size:
        fnt = font(weight, size)
        line_h = round(size * leading)
        fit = _wrap(tokens, fnt, safe_w, allow_split=False)
        if fit and len(fit[0]) <= max_lines and len(fit[0]) * line_h <= box_h:
            return _finish(_balance(tokens, fnt, safe_w, fit[0]), fnt, size, line_h, False, False)
        size -= 2
    size = min_size
    fnt = font(weight, size)
    line_h = round(size * leading)
    lines, did_split = _wrap(tokens, fnt, safe_w, allow_split=True)
    allowed = max(1, min(max_lines, box_h // line_h))
    truncated = len(lines) > allowed
    if truncated:
        lines = lines[:allowed]
        lines[-1] = _ellipsize(lines[-1], fnt, safe_w)
    return _finish(lines, fnt, size, line_h, truncated, did_split)


def _balance(tokens, fnt, max_w: float, lines: list) -> list:
    """Gleich viele Zeilen, aber möglichst gleich lang (keine einsame Silbe in der letzten Zeile)."""
    n = len(lines)
    if n < 2:
        return lines
    best = lines
    w = max_w * 0.55
    while w < max_w:
        fit = _wrap(tokens, fnt, w, allow_split=False)
        if fit and len(fit[0]) == n:
            best = fit[0]
            break
        w += max_w * 0.05
    return best


def _finish(lines, fnt, size, line_h, truncated, did_split) -> TextLayout:
    width = max((math.ceil(fnt.getlength(s)) for s in lines), default=0)
    return TextLayout(list(lines), size, line_h, width, len(lines) * line_h, truncated, did_split)


def _draw_lines(draw: ImageDraw.ImageDraw, layout: TextLayout, x: int, bottom: int, weight: str, fill) -> None:
    """Zeilen linksbündig, die letzte Grundlinie so, dass Unterlängen über ``bottom`` bleiben."""
    fnt = font(weight, layout.font_size)
    descent = round(layout.font_size * 0.26)
    baseline = bottom - descent - (len(layout.lines) - 1) * layout.line_height
    for i, line in enumerate(layout.lines):
        draw.text((x, baseline + i * layout.line_height), line, font=fnt, fill=fill, anchor="ls")


# Maße je Format: Rand, Schriftgrößen, Feld für den Titel (alles in Pixeln des fertigen Bildes)
_SPECS = {
    "tile": {"margin": 40, "max": 62, "min": 32, "lines": 5, "badge": 23, "top_reserved": 108},
    "cover": {"margin": 48, "max": 74, "min": 36, "lines": 7, "badge": 26, "top_reserved": 150},
}


def title_box(kind: str, size) -> tuple[int, int, int, int]:
    """Feld (links, oben, rechts, unten), in dem der Titel stehen darf."""
    spec = _SPECS[kind]
    m = spec["margin"]
    return m, spec["top_reserved"], size[0] - m, size[1] - m


def title_layer(kind: str, title: str, size=None) -> tuple[Image.Image, TextLayout]:
    """Nur die Schrift auf durchsichtigem Grund (so lässt sich der Überlauf im Test messen)."""
    size = size or (TILE_SIZE if kind == "tile" else COVER_SIZE)
    spec = _SPECS[kind]
    left, top, right, bottom = title_box(kind, size)
    layout = layout_title(title, right - left, bottom - top, max_size=spec["max"],
                          min_size=spec["min"], max_lines=spec["lines"])
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    _draw_lines(ImageDraw.Draw(layer), layout, left, bottom, "semibold", (255, 255, 255, 255))
    return layer, layout


def _badge_layer(size, text: str, margin: int, font_px: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    text = (text or "").strip().upper()
    if not text:
        return layer
    fnt = font("semibold", font_px)
    draw = ImageDraw.Draw(layer)
    spacing = font_px * 0.08                      # Versalien leicht gesperrt
    width = sum(fnt.getlength(c) for c in text) + spacing * (len(text) - 1)
    pad_x, pad_y = round(font_px * 0.62), round(font_px * 0.34)
    box_h = font_px + 2 * pad_y
    box = (margin, margin, margin + math.ceil(width) + 2 * pad_x, margin + box_h)
    draw.rounded_rectangle(box, radius=round(font_px * 0.36), fill=(255, 255, 255, 46))
    x = margin + pad_x
    baseline = margin + pad_y + round(font_px * 0.86)
    for c in text:
        draw.text((x, baseline), c, font=fnt, fill=(255, 255, 255, 235), anchor="ls")
        x += fnt.getlength(c) + spacing
    return layer


def _with_shadow(base: Image.Image, layer: Image.Image, offset=(0, 3), blur=7, strength=0.45) -> Image.Image:
    """Legt eine Ebene mit weichem Schatten auf (Lesbarkeit auf hellen Stellen des Verlaufs)."""
    alpha = layer.getchannel("A")
    shadow_a = alpha.filter(ImageFilter.GaussianBlur(blur)).point(lambda a: int(a * strength))
    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow.putalpha(shadow_a)
    out = base.convert("RGBA")
    out.alpha_composite(ImageChops.offset(shadow, offset[0], offset[1]))
    out.alpha_composite(layer)
    return out


def _render_titled(kind: str, title: str, badge: str, size) -> Image.Image:
    pal = palette(title)
    spec = _SPECS[kind]
    img = _backdrop(size, pal["top"], pal["bottom"], pal["glow"])
    text, layout = title_layer(kind, title, size)
    left, _top, _right, bottom = title_box(kind, size)
    # Kurzer Akzentstrich über dem Titel: gibt der Fläche Halt, ohne ein Motiv vorzutäuschen
    marks = Image.new("RGBA", size, (0, 0, 0, 0))
    bar_y = bottom - layout.height - round(spec["margin"] * 0.55)
    bar_h = max(4, round(size[0] * 0.009))
    ImageDraw.Draw(marks).rounded_rectangle(
        (left, bar_y - bar_h, left + round(size[0] * 0.11), bar_y), radius=bar_h // 2, fill=pal["accent"] + (230,))
    marks.alpha_composite(_badge_layer(size, badge, spec["margin"], spec["badge"]))
    marks.alpha_composite(text)
    return _with_shadow(img, marks).convert("RGB")


def render_tile(title: str, badge: str = "", size=TILE_SIZE) -> Image.Image:
    """Quadratische Kachel: Verlauf, Marke oben links, Titel unten links."""
    return _render_titled("tile", title, badge, size)


def render_cover(title: str, badge: str = "", size=COVER_SIZE) -> Image.Image:
    """Hochkant-Bild für das Raster der Bibliothek."""
    return _render_titled("cover", title, badge, size)


def render_hero(title: str, size=HERO_SIZE) -> Image.Image:
    """Bildschirmfüllender Hintergrund ohne Schrift (den Titel setzt die Oberfläche selbst)."""
    pal = palette(title)
    top = _mix(pal["top"], (10, 11, 16), 0.35)
    bottom = _mix(pal["bottom"], (6, 7, 10), 0.45)
    return _backdrop(size, top, bottom, pal["glow"], glow_at=(0.72, 0.08), glow_strength=0.30, noise=1.2)


# ------------------------------------------------------------------ App-Kacheln

def _stroke(draw, pts, width, fill=(255, 255, 255, 255)) -> None:
    """Linienzug mit runden Enden und Ecken."""
    draw.line(pts, fill=fill, width=width, joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _draw_symbol(name: str, box: int) -> Image.Image:
    """Eigenes Liniensymbol, weiß auf durchsichtig. Raster 100x100, vierfach überabgetastet."""
    scale = 4
    s = box * scale / 100.0
    layer = Image.new("RGBA", (box * scale, box * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw = max(2, round(5.2 * s))
    white = (255, 255, 255, 255)

    def P(*xy):
        return [(xy[i] * s, xy[i + 1] * s) for i in range(0, len(xy), 2)]

    def rrect(x0, y0, x1, y1, r):
        d.rounded_rectangle((x0 * s, y0 * s, x1 * s, y1 * s), radius=r * s, outline=white, width=lw)

    def dot(x, y, r):
        d.ellipse(((x - r) * s, (y - r) * s, (x + r) * s, (y + r) * s), fill=white)

    if name == "play":                       # Bildschirm mit Wiedergabe-Dreieck
        rrect(10, 22, 90, 78, 12)
        d.polygon(P(43, 38, 43, 62, 63, 50), fill=white)
    elif name == "film":                     # Filmstreifen
        rrect(14, 16, 86, 84, 9)
        # Linien enden innerhalb des Rahmens, sonst stehen die runden Enden über
        d.line(P(31, 18, 31, 82), fill=white, width=lw)
        d.line(P(69, 18, 69, 82), fill=white, width=lw)
        for y in (33, 50, 67):
            d.line(P(16, y, 31, y), fill=white, width=lw)
            d.line(P(69, y, 84, y), fill=white, width=lw)
    elif name == "globe":                    # Weltkugel
        d.ellipse((14 * s, 14 * s, 86 * s, 86 * s), outline=white, width=lw)
        d.ellipse((35 * s, 14 * s, 65 * s, 86 * s), outline=white, width=lw)
        d.line(P(16, 50, 84, 50), fill=white, width=lw)
        d.line(P(21, 31, 79, 31), fill=white, width=lw)
        d.line(P(21, 69, 79, 69), fill=white, width=lw)
    elif name == "desktop":                  # Bildschirm mit Fuß
        rrect(10, 18, 90, 68, 8)
        _stroke(d, P(50, 69, 50, 82), lw)
        _stroke(d, P(32, 83, 68, 83), lw)
    elif name == "terminal":                 # Fenster mit Eingabezeichen und Schreibmarke
        rrect(8, 16, 92, 84, 9)
        _stroke(d, P(24, 38, 38, 50, 24, 62), lw)
        _stroke(d, P(46, 64, 70, 64), lw)
    elif name == "handheld":                 # Handheld: Bildschirm, Steuerkreuz, zwei Knöpfe
        rrect(6, 27, 94, 73, 15)
        rrect(33, 36, 67, 64, 4)
        _stroke(d, P(14, 50, 26, 50), lw)
        _stroke(d, P(20, 44, 20, 56), lw)
        dot(77, 55, 3.6)
        dot(85, 46, 3.6)
    elif name == "steam":                    # aufsteigender Dampf (kein Firmenzeichen)
        for x0 in (30, 50, 70):
            pts = []
            for i in range(0, 41):
                y = 80 - i * 1.5
                pts.extend((x0 + 6.5 * math.sin(i / 40 * 2 * math.pi), y))
            _stroke(d, P(*pts), lw)
    else:                                    # "apps": vier Felder
        for x0, y0 in ((16, 16), (56, 16), (16, 56), (56, 56)):
            rrect(x0, y0, x0 + 28, y0 + 28, 7)
    return layer.resize((box, box), Image.LANCZOS)


def _fit_label(text: str, max_w: int, max_size: int, min_size: int, weight: str = "regular"):
    text = " ".join((text or "").split())
    size = max_size
    while size > min_size and font(weight, size).getlength(text) > max_w:
        size -= 2
    fnt = font(weight, size)
    if fnt.getlength(text) > max_w:
        text = _ellipsize(text, fnt, max_w)
    return text, fnt


def render_app_tile(symbol: str, title: str = "", icon: Image.Image | None = None, size=TILE_SIZE,
                    portrait: bool = False) -> Image.Image:
    """Neutrale Systemkachel. ``icon`` = vom Programm selbst installiertes Symbol (sonst Liniensymbol)."""
    w, h = size
    img = _backdrop(size, (40, 47, 54), (19, 22, 26), (92, 104, 118), glow_at=(0.15, 0.05), glow_strength=0.34)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    has_label = bool((title or "").strip())
    box = round(w * (0.46 if has_label else 0.52))
    cx = w // 2
    cy = round(h * (0.40 if portrait else (0.42 if has_label else 0.50)))
    if icon is not None:
        art = icon.convert("RGBA")
        art.thumbnail((box, box), Image.LANCZOS)
    else:
        art = _draw_symbol(symbol if symbol in APP_SYMBOLS else "apps", box)
    layer.alpha_composite(art, (cx - art.width // 2, cy - art.height // 2))
    if has_label:
        text, fnt = _fit_label(title, w - round(w * 0.16), round(w * 0.088), round(w * 0.05))
        base_y = cy + box // 2 + round(w * 0.135)
        ImageDraw.Draw(layer).text((cx, base_y), text, font=fnt, fill=(255, 255, 255, 240), anchor="ms")
    return _with_shadow(img, layer, offset=(0, 4), blur=9, strength=0.5).convert("RGB")


# ------------------------------------------------------------------ Dateien

def save_image(img: Image.Image, out_path: str, quality: int = 90) -> str:
    """Atomar speichern (erst daneben schreiben, dann umbenennen). JPEG oder PNG nach Endung."""
    folder = os.path.dirname(out_path)
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".tmp-%d-%s" % (os.getpid(), os.path.basename(out_path)))
    try:
        if out_path.lower().endswith(".png"):
            img.save(tmp, "PNG", compress_level=4)
        else:
            img.convert("RGB").save(tmp, "JPEG", quality=quality, subsampling=0 if quality >= 92 else 2)
        os.replace(tmp, out_path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return out_path


def make_tile(title: str, badge: str, out_path: str) -> str:
    """Blockierend (~15 ms)."""
    return save_image(render_tile(title, badge), out_path, quality=92)


def make_cover(title: str, badge: str, out_path: str) -> str:
    """Blockierend (~25 ms)."""
    return save_image(render_cover(title, badge), out_path, quality=92)


def make_hero(title: str, out_path: str) -> str:
    """Blockierend (~60 ms)."""
    return save_image(render_hero(title), out_path, quality=90)


def make_app_tile(symbol: str, title: str, out_path: str, icon: Image.Image | None = None) -> str:
    return save_image(render_app_tile(symbol, title, icon), out_path, quality=93)


def make_app_cover(symbol: str, title: str, out_path: str, icon: Image.Image | None = None) -> str:
    return save_image(render_app_tile(symbol, title, icon, size=COVER_SIZE, portrait=True), out_path, quality=93)
