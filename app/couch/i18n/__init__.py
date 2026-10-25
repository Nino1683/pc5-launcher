"""Sprache der Oberfläche: Deutsch oder Englisch (Wunsch 2026-10-25: „Erstelle auch eine englische Fassung der Oberfläche“).

Die Seite (app/web) hat ihre Texte in strings.js bzw. strings-en.js und bekommt die Sprache über ?lang= (ui/host.py).
Der Kern schreibt seine Texte weiter deutsch; was er an die Seite schickt (Antworten und Ereignisse der Brücke),
übersetzt bei Englisch Rpc.out_filter = translate_tree an einer Stelle:

  * en.json: {deutsche Vorlage: englische Vorlage}, gesammelt mit tools/i18n-extract. Vorlagen mit Platzhaltern
    (%s, %d, %.1f, {0}, {name}) passen auf den fertig eingesetzten Text; die eingesetzten Werte (Titel, Zahlen)
    bleiben, wie sie sind.
  * Passt ein ganzer Text nicht, wird Satz für Satz übersetzt (zusammengesetzte Meldungen); was dann noch fehlt,
    bleibt deutsch.
  * Nie übersetzt: Kennungen, Pfade, Adressen und Werte von Auswahlzeilen (SKIP_KEYS, _skip_value), damit die Seite
    nichts Übersetztes zurückschickt.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

LANGS = ("de", "en")
NAMES = {"de": "Deutsch", "en": "English"}
_HERE = os.path.dirname(os.path.abspath(__file__))
# Echte Platzhalter (ohne Leerzeichen als Formatkennzeichen: „50 % sparen“ ist keiner) und die Verdopplungen
_MARK = re.compile(r"%(?:\([^)]*\))?[-+#0]*\d*(?:\.\d+)?[sdifr]|\{[A-Za-z0-9_]*\}")
_PH = re.compile(r"%%|\{\{|\}\}|" + _MARK.pattern)
_MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember")
_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")
_DATE = re.compile(r"\b(\d{1,2})\. (%s)(?: (\d{4}))?" % "|".join(_MONTHS))
# settings/system.german_datetime: „Samstag, 25. Oktober 2026, 10:30 Uhr“ (0.3.1: Wochentag und „Uhr“ blieben deutsch)
_DAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_DAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAY = re.compile(r"\b(%s)\b" % "|".join(_DAYS))
_WEEKDAY_LEAD = re.compile(r"^(?:%s), " % "|".join(_DAYS))
_CLOCK = re.compile(r"\b(\d{1,2}:\d{2}) Uhr\b")
# Deutsche Zahlen („87,4“, „1.234,6“) auf Englisch mit Dezimalpunkt; Versionen und Uhrzeiten haben kein Komma
_DECIMAL = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d+),(\d+)(?![\d,])")
_SENTENCE = re.compile(r"(?<=[.!?…“])\s+(?=[A-ZÄÖÜ„])")
SKIP_KEYS = frozenset({"id", "ids", "path", "paths", "url", "uri", "href", "src", "appId", "app_id", "entryId",
                       "entry_id", "exe", "dir", "file", "files", "icon", "art", "cover", "tile", "hero", "logo",
                       "key", "kind", "type", "page", "rows_page", "role", "state", "source", "store", "sid",
                       "slug", "code", "reason", "phase", "action", "event", "method", "name_id", "uuid", "mac",
                       "address", "ssid", "sink", "profile", "connector", "lang", "language", "version",
                       "error", "exts", "flatpak", "game_id", "gameId",
                       "theme", "themeCss"})       # Design (E113): CSS bleibt, wie es ist („hsl(200,50%)“ ist keine Dezimalzahl)


def language(config: Any = None, env: Any = None) -> str:
    """Gewählte Sprache (ui.language); leer = nach der Systemsprache (de* -> Deutsch, sonst Englisch)."""
    try:
        value = str(config.get("ui.language", "") or "") if config is not None else ""
    except Exception:
        value = ""
    if value in LANGS:
        return value
    env = os.environ if env is None else env
    loc = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    return "de" if loc.lower().startswith("de") else "en"


def _literal(text: str) -> str:
    return text.replace("%%", "%").replace("{{", "{").replace("}}", "}")


def _unescape_marks(text: str) -> str:
    """Verdopplungen ausblenden, damit „%%s“ oder „{{x}}“ nicht als Platzhalter zählen."""
    return text.replace("%%", "\0").replace("{{", "\1").replace("}}", "\2")


def _split(text: str) -> list[str]:
    """Vorlage an den echten Platzhaltern teilen; Verdopplungen bleiben im Text (später _literal)."""
    hidden = _unescape_marks(text)
    parts = _MARK.split(hidden)
    return [p.replace("\0", "%%").replace("\1", "{{").replace("\2", "}}") for p in parts]


class Catalog:
    def __init__(self, pairs: dict):
        self.exact: dict[str, str] = {}
        self.buckets: dict[str, list] = {}
        self.prefixes: list = []                # „Zuletzt geprüft: “ + Wert (zusammengesetzt im Code)
        for de, en in pairs.items():
            if not isinstance(de, str) or not isinstance(en, str) or not de or de == en:
                continue
            marks = _MARK.findall(_unescape_marks(de))
            if not marks:
                self.exact.setdefault(_literal(de), _literal(en))
                if de.endswith((": ", " · ", " – ")) and len(de) > 3:
                    self.prefixes.append((_literal(de), _literal(en)))
                continue
            parts = _split(de)
            literals = [_literal(p) for p in parts]
            if not "".join(literals).strip():
                continue                            # nur Platzhalter: nichts zu übersetzen
            rx = "".join(re.escape(lit) + ("(.+?)" if i < len(marks) else "") for i, lit in enumerate(literals))
            self.buckets.setdefault(literals[0][:3], []).append((re.compile(rx + r"\Z", re.S), marks, en))

    def _template(self, text: str) -> str | None:
        for key in (text[:3], text[:2], text[:1], ""):
            for rx, marks, en in self.buckets.get(key, ()):
                m = rx.match(text)
                if m:
                    # Eingesetzte Teile, die selbst Text der Konsole sind („wird installiert“), auch übersetzen;
                    # Titel und Zahlen stehen nicht in der Liste und bleiben
                    return _fill(en, marks, tuple(self.exact.get(g, g) for g in m.groups()))
        return None

    def translate(self, text: str) -> str:
        out = self._translate(text)
        return _DECIMAL.sub(lambda m: m.group(1).replace(".", ",") + "." + m.group(2), out) if "," in out else out

    def _translate(self, text: str) -> str:
        hit = self.exact.get(text)
        if hit is not None:
            return hit
        if len(text) < 2 or not any(c.isalpha() for c in text):
            return text
        hit = self._template(text)
        if hit is not None:
            return hit
        parts = _SENTENCE.split(text)
        if len(parts) > 1:
            out = [self.exact.get(p) or self._template(p) or p for p in parts]
            if out != parts:
                return _dates(" ".join(out))
        for de, en in self.prefixes:
            if text.startswith(de):
                rest = text[len(de):]
                return en + _dates(self.exact.get(rest) or self._template(rest) or rest)
        # Sonst nur ein reines Datum („24. Oktober 2026, 21:14“): kein halb übersetzter deutscher Satz
        return _dates(text) if _DATE.fullmatch(_WEEKDAY_LEAD.sub("", text).split(",")[0].strip()) else text


def _dates(text: str) -> str:
    """„24. Oktober 2026“ -> „October 24, 2026“ (Datumsangaben, die der Kern deutsch zusammensetzt)."""
    if not any(m in text for m in _MONTHS):
        return text

    def put(m: re.Match) -> str:
        month = _MONTHS_EN[_MONTHS.index(m.group(2))]
        return "%s %s, %s" % (month, m.group(1), m.group(3)) if m.group(3) else "%s %s" % (month, m.group(1))
    text = _DATE.sub(put, text)
    text = _WEEKDAY.sub(lambda m: _DAYS_EN[_DAYS.index(m.group(1))], text)
    return _CLOCK.sub(lambda m: m.group(1), text)


def _fill(en: str, marks: list, values: tuple) -> str:
    """Englische Vorlage mit den Werten des deutschen Texts: %-Platzhalter der Reihe nach, {x} nach Namen."""
    named = {m: v for m, v in zip(marks, values) if m.startswith("{")}
    pos = iter(v for m, v in zip(marks, values) if m.startswith("%"))

    def put(m: re.Match) -> str:
        tok = m.group(0)
        if tok == "%%":
            return "%"
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        if tok.startswith("{"):
            return named.get(tok, tok)
        return next(pos, tok)
    return _PH.sub(put, en)


_lock = threading.Lock()
_catalogs: dict[str, Catalog] = {}


def catalog(lang: str = "en") -> Catalog:
    with _lock:
        if lang not in _catalogs:
            try:
                with open(os.path.join(_HERE, lang + ".json"), encoding="utf-8") as f:
                    pairs = json.load(f)
            except (OSError, ValueError):
                pairs = {}
            _catalogs[lang] = Catalog(pairs if isinstance(pairs, dict) else {})
        return _catalogs[lang]


def _skip_value(obj: dict) -> bool:
    """Werte von Auswahlzeilen und ihren Optionen gehen an den Kern zurück: nie übersetzen."""
    # Jede Zeile hat ein Feld options (meist None): nur echte Auswahllisten zählen, sonst blieben auch
    # Info-Werte („Verbunden“) deutsch
    return bool(obj.get("options")) or ("label" in obj and "value" in obj and "type" not in obj) or \
        obj.get("type") in ("select", "toggle", "slider")


def translate_tree(obj: Any, cat: Catalog | None = None) -> Any:
    """Kopie von obj mit übersetzten Texten (Wörterbücher, Listen, Zeichenketten)."""
    cat = cat or catalog("en")
    if isinstance(obj, str):
        return cat.translate(obj)
    if isinstance(obj, list):
        return [translate_tree(v, cat) for v in obj]
    if isinstance(obj, tuple):
        return tuple(translate_tree(v, cat) for v in obj)
    if isinstance(obj, dict):
        keep_value = _skip_value(obj)
        out = {}
        for k, v in obj.items():
            if k in SKIP_KEYS or (k == "value" and keep_value) or (isinstance(k, str) and k.endswith(("Id", "_id", "Path", "_path"))):
                out[k] = v
            else:
                out[k] = translate_tree(v, cat)
        return out
    return obj


def t(text: str, lang: str) -> str:
    """Ein Text für Fenster außerhalb der Seite (Browser, Terminal)."""
    return catalog("en").translate(text) if lang == "en" else text
