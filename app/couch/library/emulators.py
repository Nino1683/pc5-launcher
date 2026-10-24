"""Quellen "switch" und "wiiu": Emulator-Spiele aus der Parser-Liste des Steam ROM Managers (E13).

Nur lesend, blockierend (Platte: ROM-Ordner durchsuchen; beim ersten Mal zusätzlich die
6 MB große Namensliste des SRM, danach aus dem Zwischenspeicher <cache_dir>/emu_titles.json).

Gelesen werden <srm_dir>/userConfigurations.json (Parser: ROM-Ordner, Suchmuster, Startskript),
<srm_dir>/userSettings.json (romsDirectory) und <srm_dir>/fuzzyList.json (Schreibweise der Titel).
Die Suche bildet den Steam ROM Manager nach (glob.parser.ts, file-parser.ts): ${title} wird zu *,
gesucht wird im ROM-Ordner, der Titel ist der Pfadabschnitt, in dem ${title} stand.

Bewusst anders als der SRM:
  * Parser, deren Startskript fehlt, werden übersprungen. Sonst erschiene jedes Switch-Spiel
    doppelt („Switch - Ryujinx“ ist aktiv, ryujinx.sh gibt es nicht).
  * Es zählen nur Parser, deren Startskript wir direkt ersetzen können: eden.sh -> eden-launch,
    cemu.sh -> cemu-launch. EmuDecks eigene Skripte werden nie aufgerufen.
  * Titel: Klammerzusätze weg, „X, The“ umstellen, dann nur ein exakter Treffer auf dem
    normalisierten Namen in der SRM-Liste (liefert „Zelda: Tears of the Kingdom“ mit Doppelpunkt).
  * Dieselbe Datei (realpath) aus zwei Parsern ergibt einen Eintrag.

Ergebnis je Spiel:
  Entry(id="emu:<system>:<10 Hex-Zeichen sha1 des realpath>", source="switch"|"wiiu",
        badge="SWITCH"|"WII U", available=Datei vorhanden,
        detail={system, rom (Pfad), emulator ("eden"|"cemu"), starter (Pfad zu eden-launch bzw.
                cemu-launch), starter_exists, parser, title_raw, game_dir (nur Wii U: Titelordner),
                alternatives (weitere Treffer zum selben Titel), title_id (16 Hex-Zeichen, groß; ""
                = unbekannt): Switch aus dem Dateinamen „…[0100F2C0115B6000]…“, Wii U aus
                meta/meta.xml des Titelordners. Damit findet saves.py die Spielstände (E99).})

Unterstützter Glob-Dialekt (minimatch-Teilmenge): * ? [abc] [!abc] ** {a,b} (auch mit /),
@(a|b) ?(a|b) +(a|b) *(a|b) !(a|b), verschachtelt. Nicht: {1..9}, [[:alpha:]], Backslash-Escapes,
Parser-Typ „Glob-regex“.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata

from .. import log
from .model import Entry
from .sources import clean_text, err_text, read_json, sort_title, write_json_atomic

_log = log.get("library.emulators")

# Parser, die keine Spiele liefern, sondern Programme
APP_PARSERS = ("Emulators - Emulators", "Cloud Services", "Generic Applications",
               "Remote Play Clients", "Desktop Applications")

# EmuDeck-Startskript -> (Emulator, System, Marke, eigener Starter unter ~/.local/bin)
DIRECT = {
    "eden.sh": ("eden", "switch", "SWITCH", "eden-launch"),
    "cemu.sh": ("cemu", "wiiu", "WII U", "cemu-launch"),
}
SYSTEM_NAMES = {"switch": "Nintendo Switch", "wiiu": "Wii U"}
# Flatpak je Emulator: ohne eigene Starter des Nutzers der Weg über flatpak run (E105)
FLATPAKS = {"eden": "dev.eden_emu.eden", "cemu": "info.cemu.Cemu"}
_IGNORED_FILES = {"systeminfo.txt", "metadata.txt"}
_TITLE_CACHE_VERSION = 1
_SWITCH_ID = re.compile(r"\[(01[0-9A-Fa-f]{14})\]")
_WIIU_ID = re.compile(r"<title_id[^>]*>\s*([0-9A-Fa-f]{16})\s*</title_id>")


# ---------------------------------------------------------------- Variablen ${...}

def _find_close(s: str, start: int) -> int:
    """Index der schließenden } zu einem ${ (start zeigt hinter '${')."""
    depth, i = 1, start
    while i < len(s):
        if s.startswith("${", i):
            depth += 1
            i += 2
            continue
        if s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _exec_special(body: str):
    """SRM-Sonderformen: ${/regex/flags|text|ersatz}, ${uc|x}, ${lc|x}, ${os:linux|a|b}."""
    m = re.match(r"^/(.*?)/([giu]{0,3})\|(.*?)(?:\|(.*?))?$", body, re.S)
    if m:
        try:
            rx = re.compile(m.group(1), re.I if "i" in m.group(2) else 0)
        except re.error:
            return body
        if m.group(4) is not None:
            return rx.sub(m.group(4).replace("$", "\\"), m.group(3), count=0 if "g" in m.group(2) else 1)
        im = rx.search(m.group(3))
        if not im:
            return ""
        return "".join(g for g in im.groups() if g) or im.group(0)
    m = re.match(r"^uc\|(.*)$", body, re.I | re.S)
    if m:
        return m.group(1).upper()
    m = re.match(r"^lc\|(.*)$", body, re.I | re.S)
    if m:
        return m.group(1).lower()
    m = re.match(r"^os:(.+?)\|(.*?)(?:\|(.*?))?$", body, re.I | re.S)
    if m and m.group(1).lower() in ("win", "mac", "linux"):
        return (m.group(2) if m.group(1).lower() == "linux" else m.group(3)) or ""
    return None


def expand(text: str, variables: dict, keep=()) -> str:
    """Ersetzt ${...} von innen nach außen; Namen ohne Groß/Klein-Unterschied. keep bleibt stehen."""
    out, i = [], 0
    while True:
        j = text.find("${", i)
        if j < 0:
            out.append(text[i:])
            break
        k = _find_close(text, j + 2)
        if k < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        body = expand(text[j + 2:k], variables, keep)
        low = body.lower()
        if low in keep:
            out.append("${" + body + "}")
        elif low in variables:
            out.append(variables[low])
        else:
            special = _exec_special(body)
            out.append(body if special is None else special)     # Unbekanntes bleibt wie beim SRM stehen
        i = k + 1
    return "".join(out)


# ---------------------------------------------------------------- Glob -> Regex

def brace_expand(pat: str) -> list[str]:
    """{a,b{c,d}} -> alle Varianten. Klammern ohne Komma bleiben wörtlich."""
    depth, start = 0, -1
    for i, ch in enumerate(pat):
        if ch == "{" and (i == 0 or pat[i - 1] != "$"):
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                inner = pat[start + 1:i]
                parts, d, last = [], 0, 0
                for n, c in enumerate(inner):
                    if c == "{":
                        d += 1
                    elif c == "}":
                        d -= 1
                    elif c == "," and d == 0:
                        parts.append(inner[last:n])
                        last = n + 1
                parts.append(inner[last:])
                if len(parts) == 1:
                    continue
                res = []
                for p in parts:
                    res.extend(brace_expand(pat[:start] + p + pat[i + 1:]))
                return res
    return [pat]


def _split_alt(s: str) -> list[str]:
    parts, depth, last = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            parts.append(s[last:i])
            last = i + 1
    parts.append(s[last:])
    return parts


def seg_regex(seg: str) -> str:
    """Ein Pfadabschnitt (ohne /) als Regex-Quelltext, ohne Anker."""
    out, i = [], 0
    while i < len(seg):
        ch = seg[i]
        if ch in "@?+*!" and i + 1 < len(seg) and seg[i + 1] == "(":
            depth, j = 1, i + 2
            while j < len(seg) and depth:
                depth += seg[j] == "("
                depth -= seg[j] == ")"
                j += 1
            if depth == 0:
                alts = "|".join(seg_regex(a) for a in _split_alt(seg[i + 2:j - 1]))
                rest = seg_regex(seg[j:])
                if ch == "@":
                    out.append("(?:%s)" % alts)
                elif ch == "?":
                    out.append("(?:%s)?" % alts)
                elif ch == "+":
                    out.append("(?:%s)+" % alts)
                elif ch == "*":
                    out.append("(?:%s)*" % alts)
                else:                                   # !(a|b): alles, was nicht (a|b)+Rest ist
                    out.append("(?:(?!(?:%s)%s$)[^/]*?)" % (alts, rest))
                out.append(rest)
                return "".join(out)
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            j = seg.find("]", i + 2)
            if j < 0:
                out.append(re.escape(ch))
            else:
                body = seg[i + 1:j]
                if body[:1] in "!^":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def has_magic(seg: str) -> bool:
    return bool(re.search(r"[*?\[]|[@+!]\(", seg))


class DirCache:
    """Verzeichnislisten eines Suchlaufs: einmal gelesen, von allen Parsern geteilt.

    Die Wii-U-Spiele sind Ordner mit Zehntausenden Dateien, und zwei Parser durchsuchen sie mit
    „**“. os.scandir kennt die Art eines Eintrags ohne eigenes stat je Datei.
    """

    def __init__(self):
        self._lists: dict[str, list[tuple[str, bool]]] = {}

    def entries(self, path: str) -> list[tuple[str, bool]]:
        """[(name, ist_ordner)] sortiert; Symlinks auf Ordner zählen als Ordner (node-glob follow:true)."""
        hit = self._lists.get(path)
        if hit is None:
            hit = []
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir()
                        except OSError:
                            is_dir = False
                        hit.append((entry.name, is_dir))
            except OSError:
                pass
            hit.sort()
            self._lists[path] = hit
        return hit


def glob_walk(root: str, pattern: str, dirs: DirCache | None = None) -> list[str]:
    """Relative Pfade (mit /) aller Treffer unter root. Wie node-glob mit dot:true und follow:true;
    Schleifen über Symlinks werden erkannt."""
    dirs = dirs or DirCache()
    results: dict[str, bool] = {}
    for pat in brace_expand(pattern):
        segs = [s for s in pat.split("/") if s != ""]
        compiled = [None if s == "**" else (re.compile("^" + seg_regex(s) + "$", re.S) if has_magic(s) else s)
                    for s in segs]
        _walk(root, "", compiled, 0, results, set(), dirs)
    return sorted(results)


def _walk(root, rel, segs, i, results, seen, dirs) -> None:
    here = os.path.join(root, rel) if rel else root
    if i == len(segs):
        if rel:
            results[rel] = True
        return
    seg = segs[i]
    if seg is None:                                     # **
        _walk(root, rel, segs, i + 1, results, seen, dirs)
        for name, is_dir in dirs.entries(here):
            if not is_dir:
                continue
            try:
                st = os.stat(os.path.join(here, name))
            except OSError:
                continue
            key = (st.st_dev, st.st_ino, i)
            if key in seen:
                continue
            seen.add(key)
            _walk(root, (rel + "/" + name) if rel else name, segs, i, results, seen, dirs)
        return
    last = i == len(segs) - 1
    if isinstance(seg, str):
        path = os.path.join(here, seg)
        names = [(seg, os.path.isdir(path))] if os.path.lexists(path) else []
    else:
        names = [(n, d) for n, d in dirs.entries(here) if seg.match(n)]
    for name, is_dir in names:
        nrel = (rel + "/" + name) if rel else name
        if last:
            results[nrel] = True
        elif is_dir:
            _walk(root, nrel, segs, i + 1, results, seen, dirs)


# ---------------------------------------------------------------- Titel

class TitleRule:
    """Wie SRM getTitleDepth/getTitleRegex: wo im Pfad steht der Titel, und was ist Rest."""

    def __init__(self, glob: str):
        self.glob = glob
        if re.sub(r"\$\{title\}", "", glob, count=1, flags=re.I) == "":
            self.level, self.direction = None, None
        elif re.search(r".*(?:\*\*|\{.*?/+.*?\}).+\$\{title\}.*", glob, re.I):
            self.direction = "right"
            self.level = re.sub(r".*\$\{title\}", "", glob, flags=re.I).count("/")
        else:
            self.direction = "left"
            self.level = re.sub(r"\$\{title\}.*", "", glob, flags=re.I).count("/")
        m = re.match(r".*/(.*\$\{title\}.*?)/|.*/(.*\$\{title\}.*)|(.*\$\{title\}.*?)/|(.*\$\{title\}.*)",
                     glob, re.I)
        seg = next(g for g in m.groups() if g) if m else "${title}"
        left, right = re.split(r"\$\{title\}", seg, maxsplit=1, flags=re.I)
        lefts = "|".join(seg_regex(x) for x in brace_expand(left))
        rights = "|".join(seg_regex(x) for x in brace_expand(right))
        self.regex = re.compile("^(?:%s)(?P<title>.*?)(?:%s)$" % (lefts, rights), re.S)
        self.search_glob = re.sub(r"\$\{.*?\}", "*", glob, count=1) if self.level is not None else "*"

    def title_of(self, relpath: str):
        part = relpath
        if self.level is not None:
            parts = relpath.split("/")
            idx = len(parts) - (self.level + 1) if self.direction == "right" else self.level
            if idx < 0 or idx >= len(parts):
                return None
            part = parts[idx]
        m = self.regex.match(part)
        if not m or not m.group("title"):
            return None
        return m.group("title").strip()


def norm_key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def clean_title(raw: str) -> str:
    """Dateiname -> lesbarer Titel: Klammerzusätze weg, „X, The“ -> „The X“, _ -> Leerzeichen."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", raw).replace("_", " ")
    t = re.sub(r"\s+", " ", t).strip(" -")
    m = re.match(r"^(.*?),\s*(The|A|An|Der|Die|Das)\b(.*)$", t)
    if m:
        t = (m.group(2) + " " + m.group(1) + m.group(3)).strip()
    return t or raw.strip()


class TitleNames:
    """Schreibweise eines Titels aus der SGDB-Namensliste des SRM (fuzzyList.json, 246.000 Namen).

    Nur exakter Treffer auf dem normalisierten Schlüssel. Die Liste zu laden kostet rund 0,5 s;
    deshalb merkt sich <cache_dir>/emu_titles.json die schon nachgeschlagenen Titel, gültig,
    solange Größe und mtime der Liste gleich bleiben.
    """

    def __init__(self, list_path: str, cache_path: str | None = None):
        self.list_path = list_path
        self.cache_path = cache_path
        self.loaded_list = False
        self._index: dict[str, str] | None = None
        self._known: dict[str, str | None] = {}
        self._dirty = False
        try:
            st = os.stat(list_path)
            self._sig = [st.st_size, st.st_mtime_ns]
        except OSError:
            self._sig = None
        if cache_path and self._sig:
            try:
                data = read_json(cache_path)
                if isinstance(data, dict) and data.get("version") == _TITLE_CACHE_VERSION \
                        and data.get("list") == self._sig and isinstance(data.get("titles"), dict):
                    self._known = data["titles"]
            except (OSError, ValueError):
                pass

    def lookup(self, title: str) -> str | None:
        key = norm_key(title)
        if not key or self._sig is None:
            return None
        if key in self._known:
            return self._known[key]
        if self._index is None:
            self._index = {}
            self.loaded_list = True
            try:
                for name in read_json(self.list_path).get("games", []):
                    if isinstance(name, str):
                        self._index.setdefault(norm_key(name), name)
            except (OSError, ValueError, AttributeError):
                self._sig = None                         # Liste kaputt: Titel aus dem Dateinamen
                return None
        self._known[key] = self._index.get(key)
        self._dirty = True
        return self._known[key]

    def save(self) -> None:
        if self._dirty and self.cache_path and self._sig:
            write_json_atomic(self.cache_path, {"version": _TITLE_CACHE_VERSION, "list": self._sig,
                                                "titles": self._known})
            self._dirty = False


# ---------------------------------------------------------------- EmuDeck-Einstellungen

def _emudeck_settings(ctx) -> dict:
    """Schlüssel=Wert aus ~/.config/EmuDeck/settings.sh (nur gelesen, nichts wird ausgeführt)."""
    out = {}
    try:
        with open(os.path.join(ctx.home, ".config/EmuDeck/settings.sh"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    out[m.group(1)] = m.group(2).strip().strip("\"'")
    except OSError:
        pass
    return out


def _load_srm(ctx):
    """(parser-liste, variablen) oder (None, None), wenn es keine SRM-Konfiguration gibt."""
    config = os.path.join(ctx.srm_dir, "userConfigurations.json")
    if not os.path.isfile(config):
        return None, None
    try:
        parsers = read_json(config)
        if not isinstance(parsers, list):
            raise ValueError("keine Liste")
    except (OSError, ValueError) as e:
        ctx.warn("emulators: userConfigurations.json nicht lesbar (%s)" % err_text(e))
        return None, None
    env = {}
    try:
        settings = read_json(os.path.join(ctx.srm_dir, "userSettings.json"))
        env = settings.get("environmentVariables") or {} if isinstance(settings, dict) else {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        ctx.warn("emulators: userSettings.json nicht lesbar (%s); ROM-Ordner aus EmuDeck geraten" % err_text(e))
    if not isinstance(env, dict):
        env = {}
    roms = env.get("romsDirectory") or _emudeck_settings(ctx).get("romsPath") or \
        (os.path.join(ctx.games_disk, "Emulation/roms") if ctx.games_disk else "")
    base = {
        "/": os.sep,
        "steamdirglobal": str(env.get("steamDirectory") or ""),
        "romsdirglobal": str(roms or ""),
        "retroarchpath": str(env.get("retroarchPath") or ""),
        "racores": str(env.get("raCoresDirectory") or ""),
        "localimagesdir": str(env.get("localImagesDirectory") or ""),
    }
    return parsers, base


# ---------------------------------------------------------------- Quelle

def _flatpak_installed(ctx, app_id: str) -> bool:
    """Wie library/apps.py: current/active in einer der Flatpak-Installationen des Kontexts."""
    return any(os.path.isdir(os.path.join(root, "app", app_id, "current", "active"))
               for root in getattr(ctx, "flatpak_dirs", None) or [])


def title_id(system: str, rom: str, game_dir: str = "") -> str:
    """TitleID eines Spiels ohne die verschlüsselten Kopfdaten zu lesen (E99), "" = unbekannt.
    Switch: „[0100…]“ im Dateinamen (ein Update-Kennzeichen …800 gilt als das Grundspiel).
    Wii U: <title_id> in meta/meta.xml des Titelordners (eine Datei, nur gelesen)."""
    if system == "switch":
        m = _SWITCH_ID.search(os.path.basename(rom))
        if not m:
            return ""
        tid = m.group(1).upper()
        return tid[:13] + "000" if tid.endswith("800") else tid
    if system == "wiiu" and game_dir:
        try:
            with open(os.path.join(game_dir, "meta", "meta.xml"), encoding="utf-8", errors="replace") as fh:
                m = _WIIU_ID.search(fh.read(65536))
        except OSError:
            return ""
        return m.group(1).upper() if m else ""
    return ""


def rom_id(system: str, path: str) -> str:
    """Stabile ID aus dem aufgelösten Pfad der Spieldatei."""
    real = os.path.realpath(path)
    return "emu:%s:%s" % (system, hashlib.sha1(real.encode("utf-8", "surrogateescape")).hexdigest()[:10])


def scan_parsers(ctx, parsers, base, names: TitleNames | None = None):
    """Spiele aller brauchbaren Parser. Blockierend.
    Rückgabe: (liste von dicts, hinweise, anzahl der Parser mit vorhandenem, bekanntem Startskript)."""
    games, notes = [], []
    by_real: dict[str, dict] = {}
    usable = 0
    dirs = DirCache()
    no_starter: list[tuple[str, str, str, str]] = []
    for p in parsers:
        if not isinstance(p, dict):
            continue
        ptitle = str(p.get("configTitle") or "?")
        if p.get("disabled") or ptitle.startswith(APP_PARSERS):
            continue
        executable = p.get("executable") if isinstance(p.get("executable"), dict) else {}
        launcher = expand(str(executable.get("path") or ""), base)
        direct = DIRECT.get(os.path.basename(launcher))
        if p.get("parserType") != "Glob":
            if direct:
                notes.append("Parser „%s“ übersprungen (Typ %s wird nicht unterstützt)" % (ptitle, p.get("parserType")))
            continue
        inputs = p.get("parserInputs") if isinstance(p.get("parserInputs"), dict) else {}
        glob = str(inputs.get("glob") or "")
        if not re.search(r"\$\{title\}", glob, re.I):
            continue
        if not launcher or not os.path.exists(launcher):
            continue                                     # Emulator nicht eingerichtet (oder Platte weg)
        if not direct:
            # Erst nach allen Parsern entscheiden, ob das eine Warnung wert ist (E99)
            no_starter.append((ptitle, os.path.basename(launcher), glob,
                               os.path.normpath(expand(str(p.get("romDirectory") or ""), base))))
            continue
        usable += 1
        emulator, system, badge, starter_name = direct
        starter = os.path.join(ctx.home, ".local/bin", starter_name)
        rom_dir = os.path.normpath(expand(str(p.get("romDirectory") or ""), base))
        if not os.path.isdir(rom_dir):
            notes.append("ROM-Ordner fehlt: %s (Parser „%s“)" % (rom_dir, ptitle))
            continue
        try:
            rule = TitleRule(glob)
            hits = glob_walk(rom_dir, rule.search_glob, dirs)
        except (re.error, RecursionError, StopIteration) as e:
            notes.append("Parser „%s“ übersprungen (Suchmuster nicht verstanden: %s)" % (ptitle, type(e).__name__))
            continue
        # Ordner-Spiele (Wii U) liefern mehrere Treffer je Titel: der flachste, kürzeste Pfad gewinnt
        hits.sort(key=lambda rel: (rel.count("/"), len(rel), rel))
        seen_titles: dict[str, dict] = {}
        for rel in hits:
            raw = rule.title_of(rel)
            if raw is None:
                continue
            path = os.path.join(rom_dir, rel.replace("/", os.sep))
            if os.path.basename(path) in _IGNORED_FILES:
                continue
            cleaned = clean_title(raw)
            final = clean_text((names.lookup(cleaned) if names else None) or cleaned, raw)
            if final in seen_titles:
                seen_titles[final]["alternatives"].append(path)
                continue
            real = os.path.realpath(path)
            if real in by_real:                          # dieselbe Datei aus einem zweiten Parser
                continue
            # Ordner-Spiel (Muster „${title}/**/*.rpx“): der Titelordner, dort liegen meta/*.tga und meta.xml
            game_dir = ""
            if rule.direction == "left" and rule.level == 0 and "/" in rel:
                game_dir = os.path.join(rom_dir, rel.split("/")[0])
            game = {
                "id": rom_id(system, path), "title": final, "title_raw": raw, "system": system,
                "badge": badge, "emulator": emulator, "rom": path, "parser": ptitle,
                "starter": starter, "starter_exists": os.path.isfile(starter), "alternatives": [],
                "game_dir": game_dir,
            }
            seen_titles[final] = game
            by_real[real] = game
            games.append(game)
    notes += _no_starter_notes(no_starter, by_real, dirs)
    return games, notes, usable


def _no_starter_notes(skipped, by_real: dict, dirs: DirCache) -> list[str]:
    """Warnungen für Parser ohne eigenen Starter, aber nur, wenn in ihrem ROM-Ordner Spiele liegen,
    die sonst kein Parser liefert (E99: EmuDeck legt rund 70 aktive Parser an, fast alle ohne Spiele;
    jeder Home-Start schrieb 69 solche Zeilen ins Protokoll). Mit demselben DirCache: Was die
    anderen Parser schon gelesen haben, kostet nichts mehr (auf dem Entwicklungsrechner zusammen rund 30 ms)."""
    out = []
    for ptitle, script, glob, rom_dir in skipped:
        # Nur ein Hilfsmittel für Warnungen: Was hier schiefgeht, darf nie die Spiele kosten (E99)
        try:
            if not os.path.isdir(rom_dir):
                continue
            rule = TitleRule(glob)
            hits = glob_walk(rom_dir, rule.search_glob, dirs)
            own = sum(1 for rel in hits if rule.title_of(rel) is not None
                      and rel.rsplit("/", 1)[-1] not in _IGNORED_FILES
                      and os.path.realpath(os.path.join(rom_dir, rel.replace("/", os.sep))) not in by_real)
        except Exception as e:  # noqa: BLE001
            _log.debug("Parser %s: Prüfung der Spieldateien gescheitert: %s", ptitle, e)
            continue
        if own:
            files = "1 Spieldatei erscheint" if own == 1 else "%d Spieldateien erscheinen" % own
            out.append("Parser „%s“ übersprungen (kein eigener Starter für %s; %s deshalb nicht)"
                       % (ptitle, script, files))
    return out


def load(ctx) -> list[Entry]:
    """Emulator-Spiele als Einträge. Blockierend; wirft nicht, Probleme stehen in ctx.warnings."""
    parsers, base = _load_srm(ctx)
    if parsers is None:
        return []
    names = TitleNames(os.path.join(ctx.srm_dir, "fuzzyList.json"), ctx.cache_file("emu_titles.json"))
    games, notes, usable = scan_parsers(ctx, parsers, base, names)
    names.save()
    for note in notes:
        ctx.warn("emulators: " + note)
    if not usable and base.get("romsdirglobal") and not os.path.isdir(base["romsdirglobal"]):
        # Startskripte und ROMs liegen auf derselben Platte: ohne sie sieht alles „nicht eingerichtet“ aus
        ctx.warn("emulators: EmuDeck-Ordner nicht erreichbar (%s); Spiele-Platte nicht eingehängt?"
                 % base["romsdirglobal"])
    # E105: Ohne eigene Starter des Nutzers startet das Spiel über das Flatpak des Emulators (launch/emulator.py);
    # installiert ist es, wenn eins von beiden da ist
    flatpak_ok = {emu: _flatpak_installed(ctx, app_id) for emu, app_id in FLATPAKS.items()}
    missing = sorted({g["starter"] for g in games if not g["starter_exists"] and not flatpak_ok.get(g["emulator"])})
    for starter in missing:
        ctx.warn("emulators: Starter fehlt (%s); die Spiele lassen sich so nicht starten" % starter)
    out = []
    for g in sorted(games, key=lambda g: (g["system"], sort_title(g["title"]))):
        out.append(Entry(
            id=g["id"], title=g["title"], kind="game", source=g["system"], badge=g["badge"],
            sort_title=sort_title(g["title"]), available=os.path.exists(g["rom"]),
            installed=bool(g["starter_exists"] or flatpak_ok.get(g["emulator"])),
            detail={"system": g["system"], "rom": g["rom"], "emulator": g["emulator"],
                    "starter": g["starter"], "starter_exists": g["starter_exists"], "parser": g["parser"],
                    "title_raw": g["title_raw"], "game_dir": g["game_dir"], "alternatives": g["alternatives"],
                    "title_id": title_id(g["system"], g["rom"], g["game_dir"])}))
    return out


def emudeck_info(ctx) -> dict:
    """Für die Einstellungsseite „Emulatoren“. Blockierend (liest die Spiele wie load()).

    {"installed": True, "emulation_dir": "/run/media/nutzer/Spiele/Emulation",
     "path": "/run/media/nutzer/Spiele/Emulation",
     "roms_dir": "/run/media/nutzer/Spiele/Emulation/roms", "available": True,
     "systems": [{"name": "Nintendo Switch", "label": "Nintendo Switch", "id": "switch",
                  "emulator": "eden", "count": 7},
                 {"name": "Wii U", "label": "Wii U", "id": "wiiu", "emulator": "cemu", "count": 2}]}
    installed: EmuDeck ist eingerichtet (Backend-Ordner, AppImage oder SRM-Parser-Liste vorhanden).
    available: der ROM-Ordner ist gerade erreichbar. systems: nur Systeme mit spielbaren Titeln.
    path und label sind dieselben Werte unter den Namen, die die Einstellungsseite erwartet.
    Die Warnungen dieses Laufs landen NICHT in ctx.warnings.
    """
    saved = list(ctx.warnings)
    try:
        try:
            entries = load(ctx)
        except Exception:
            entries = []
        settings = _emudeck_settings(ctx)
        _parsers, base = _load_srm(ctx)
    finally:
        ctx.warnings = saved                             # eine Abfrage für die Anzeige hinterlässt keine Warnungen
    roms = settings.get("romsPath") or (base or {}).get("romsdirglobal") or \
        (os.path.join(ctx.games_disk, "Emulation/roms") if ctx.games_disk else "")
    emulation = settings.get("emulationPath") or (os.path.dirname(roms.rstrip("/")) if roms else "")
    installed = any(os.path.exists(p) for p in (
        os.path.join(ctx.home, ".config/EmuDeck/backend"),
        os.path.join(ctx.home, "Applications/EmuDeck.AppImage"),
        os.path.join(ctx.srm_dir, "userConfigurations.json")))
    counts: dict[str, list] = {}
    for e in entries:
        counts.setdefault(e.source, [e.detail.get("emulator", ""), 0])[1] += 1
    systems = [{"name": SYSTEM_NAMES.get(sid, sid), "label": SYSTEM_NAMES.get(sid, sid), "id": sid,
                "emulator": emu, "count": n} for sid, (emu, n) in sorted(counts.items())]
    return {"installed": installed, "emulation_dir": emulation, "path": emulation, "roms_dir": roms,
            "available": bool(roms) and os.path.isdir(roms), "systems": systems}
