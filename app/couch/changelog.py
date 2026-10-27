"""„Was ist neu“ im PC5 Launcher (E133, Wunsch 2026-10-27: „Für den PC5 Launcher soll es auch ein Changelog geben.
Besonders für neue Updates.“).

Zwei Quellen:
  CHANGELOG.md   deutsch, Abschnitte „## X.Y.Z (JJJJ-MM-TT)“ und „## Nächste Fassung“ (noch nicht veröffentlicht).
                 Im Paket liegt sie neben VERSION (tools/make-dist: packaging/ ist die Wurzel des Pakets, pc5_install
                 kopiert alles aus MANIFEST.sha256), seit E133 auch in Fassungen von tools/install-release; im
                 Arbeitsordner unter packaging/.
  Release-Notiz  Text des GitHub-Release, den selfupdate bei der Suche ohnehin bekommt (<state>/selfupdate.json,
                 „notes“): Markdown, bisher erst Englisch, nach einer Linie „---“ eine deutsche Kurzfassung (bei
                 0.2.0 umgekehrt). Welche Hälfte welche Sprache ist, entscheidet ein Wortvergleich.
Aus Markdown wird lesbarer Text: Aufzählungen mit „•“, ohne Sternchen, Rückstriche und Adressen. Abschnitte zur
Installation („### Install“) entfallen: Im Launcher aktualisiert der Knopf, nicht install.sh.

Die Antwort (payload) geht als {title, version, sections: [{label, value, note}], empty} an die Seite. Die Texte der
Abschnitte stehen unter „value“ neben „label“: So übersetzt der Filter der Brücke (i18n.translate_tree, Zeilen mit
label und value ohne type) sie bei Englisch nicht Satz für Satz; die Sprache wählt diese Datei selbst.
"""
from __future__ import annotations

import os
import re
import stat

MAX_FILE = 512 * 1024           # CHANGELOG.md hat 2026-10 rund 9 KB
MAX_NOTE = 12000                # Zeichen je Release-Notiz im Zustand (0.3.1: 2,6 KB)
MAX_SECTIONS = 12               # so viele Fassungen zeigt „Was ist neu“ höchstens

_HEAD = re.compile(r"^##\s+(.+?)\s*$")
_VERSION_HEAD = re.compile(r"^v?(\d{1,4}\.\d{1,4}\.\d{1,4})\b\s*(?:\((\d{4})-(\d{2})-(\d{2})\))?")
# Umlaute als \u-Folgen: tools/i18n-extract hielte die Muster sonst für Oberflächentext
_NEXT_HEAD = re.compile(r"^(?:n\u00e4chste|next|unreleased)\b", re.I)
_VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})")

_RULE = re.compile(r"^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:\*\s*){3,}$|^\s{0,3}(?:_\s*){3,}$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)\s]*(?:\s+\"[^\"]*\")?\)")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?![\s*])(.+?)(?<![\s*])\*(?![\w*])|(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")
_CODE = re.compile(r"`([^`]*)`")
# Abschnitte zur Installation: im Launcher ohne Sinn (er aktualisiert mit einem Knopf)
_DROP = frozenset("install/installation/installing/update/updating/installieren/aktualisieren".split("/"))

# Wortvergleich für die Sprache einer Release-Notiz (als Liste mit „/“, damit tools/i18n-extract sie nicht für
# Oberflächentext hält)
_DE = frozenset(("der/die/das/und/nicht/ist/mit/für/ein/eine/einen/auf/wird/werden/bei/oder/auch/jetzt/nach/dem/den/"
                 "sich/von/zum/zur/im/kein/keine/über/unter/wenn/es/als/du/dein/deine/neu/neue/nur/noch").split("/"))
_EN = frozenset(("the/and/not/is/with/for/a/an/on/will/be/when/or/also/now/after/from/to/no/over/under/if/it/as/you/"
                 "your/this/that/are/new/only/still/of/by/can/its").split("/"))
_WORD = re.compile(r"[A-Za-z\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df]+")
_UMLAUT = re.compile(r"[\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df]")


# ================================================================ Lesen
def _read(path: str, limit: int = MAX_FILE) -> str:
    """Eigene Textdatei lesen, nie über einen Verweis; "" wenn es sie nicht gibt."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def read_text(release_dir: str) -> str:
    """CHANGELOG.md der laufenden Fassung: im Paket bzw. von install-release neben VERSION, im Arbeitsordner unter
    packaging/. "" wenn keine da ist."""
    for rel in ("CHANGELOG.md", os.path.join("packaging", "CHANGELOG.md")):
        text = _read(os.path.join(release_dir or "", rel))
        if text.strip():
            return text
    return ""


def version_key(text) -> tuple[int, int, int] | None:
    m = _VERSION.match(str(text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse(text: str) -> list[dict]:
    """Abschnitte der CHANGELOG.md in der Reihenfolge der Datei: [{version ("" bei „Nächste Fassung“), date
    (JJJJ-MM-TT oder ""), next, body (Markdown)}]. Andere Abschnitte (ohne Fassung) werden übergangen."""
    out: list[dict] = []
    cur: dict | None = None
    for line in str(text or "").splitlines():
        m = _HEAD.match(line)
        if m:
            head = m.group(1)
            v = _VERSION_HEAD.match(head)
            if v:
                date = "%s-%s-%s" % v.group(2, 3, 4) if v.group(2) else ""
                cur = {"version": v.group(1), "date": date, "next": False, "lines": []}
            elif _NEXT_HEAD.match(head):
                cur = {"version": "", "date": "", "next": True, "lines": []}
            else:
                cur = None
            if cur is not None:
                out.append(cur)
            continue
        if cur is not None:
            cur["lines"].append(line)
    return [{"version": s["version"], "date": s["date"], "next": s["next"], "body": "\n".join(s["lines"]).strip()}
            for s in out]


# ================================================================ Markdown zu Text
def _inline(text: str) -> str:
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _AUTOLINK.sub(lambda m: m.group(1), text)
    text = _CODE.sub(lambda m: m.group(1), text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2) or "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def to_text(md: str) -> str:
    """Markdown (CHANGELOG, Release-Notiz) als lesbarer Text: Absätze mit Leerzeile, Aufzählungen mit „•“,
    Überschriften als eigene Zeile, Code eingerückt. Abschnitte zur Installation entfallen."""
    blocks: list[list] = []          # [art, text]: head | para | item | code
    cur: list | None = None
    fence = False
    dropping = False
    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            fence = not fence
            cur = None
            continue
        if fence:
            if not dropping:
                blocks.append(["code", line.strip()])
            continue
        if not line.strip():
            cur = None
            continue
        h = _HEADING.match(line)
        if h:
            title = _inline(h.group(1))
            dropping = title.strip(" :.").lower() in _DROP
            cur = None
            if not dropping and title:
                blocks.append(["head", title])
            continue
        if _RULE.match(line):
            dropping = False
            cur = None
            continue
        if dropping:
            continue
        b = _BULLET.match(line) or _NUMBERED.match(line)
        if b:
            text = b.group(b.lastindex)
            mark = "•" if b.re is _BULLET else b.group(2) + "."
            cur = ["item", "%s %s" % (mark, _inline(text)), len(b.group(1))]
            blocks.append(cur)
            continue
        indent = len(line) - len(line.lstrip())
        if cur is not None and cur[0] in ("item", "para"):
            cur[1] += " " + _inline(line)        # weicher Zeilenumbruch im Absatz bzw. Fortsetzung des Punkts
            continue
        if indent >= 4:
            blocks.append(["code", line.strip()])
            continue
        cur = ["para", _inline(line)]
        blocks.append(cur)
    lines: list[str] = []
    last = ""
    for block in blocks:
        kind, text = block[0], block[1]
        if not text:
            continue
        # Aufzählungen und Code stehen dicht untereinander, alles andere mit Leerzeile
        if lines and not (kind == last and kind in ("item", "code")):
            lines.append("")
        lines.append(("  " if kind == "code" else "") + text)
        last = kind
    return "\n".join(lines).strip()


# ================================================================ Sprache
def guess_language(text: str) -> str:
    """"de" oder "en" nach häufigen Wörtern (und Umlauten); bei Gleichstand Deutsch (die Sprache der Datei)."""
    words = [w.lower() for w in _WORD.findall(str(text or ""))]
    de = sum(1 for w in words if w in _DE) + 2 * len(_UMLAUT.findall(str(text or "")))
    en = sum(1 for w in words if w in _EN)
    return "en" if en > de else "de"


def split_languages(md: str) -> dict[str, str]:
    """Teile einer Release-Notiz an Linien „---“, je Sprache zusammengefasst: {"en": …, "de": …} (Markdown)."""
    parts = [p.strip() for p in re.split(r"(?m)^\s{0,3}-{3,}\s*$", str(md or "")) if p.strip()]
    out: dict[str, str] = {}
    for part in parts:
        lang = guess_language(part)
        out[lang] = (out[lang] + "\n\n" + part) if lang in out else part
    return out


# ================================================================ Antwort für die Seite
def _date_text(date: str, lang: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", date or "")
    if not m:
        return ""
    return "%s-%s-%s" % m.groups() if lang == "en" else "%s.%s.%s" % (m.group(3), m.group(2), m.group(1))


def _pick(lang: str, changelog_md: str, note_md: str) -> tuple[str, str]:
    """(Text, Sprache des Texts) einer Fassung. Deutsch: die CHANGELOG.md (ausführlicher als die deutsche Kurzfassung
    der Release-Notiz), sonst der deutsche Teil der Notiz. Englisch: der englische Teil der Notiz, sonst deutsch."""
    parts = split_languages(note_md) if note_md else {}
    if lang == "en":
        order = [("en", parts.get("en", "")), ("de", changelog_md), ("de", parts.get("de", ""))]
    else:
        order = [("de", changelog_md), ("de", parts.get("de", "")), ("en", parts.get("en", ""))]
    for text_lang, md in order:
        text = to_text(md) if md else ""
        if text:
            return text, text_lang
    return "", ""


def _section(label: str, text: str, text_lang: str, lang: str) -> dict:
    note = ""
    if text_lang and text_lang != lang:
        note = "Nur auf Deutsch" if text_lang == "de" else "Nur auf Englisch"
    return {"label": label, "value": text, "note": note}


def payload(*, own: str, kind: str = "dev", lang: str = "de", changelog_text: str = "", notes: dict | None = None,
            available: str = "", pending: str = "", which: str = "all", since: str = "") -> dict:
    """Inhalt für „Was ist neu“.

    which "all": erst ein gefundenes Update (available bzw. pending, mit allen Fassungen dazwischen, aus den
                 Release-Notizen), bei einer eigenen Installation (kind "dev") „Nächste Fassung“ der Datei, dann die
                 installierte und die früheren Fassungen.
    which "new": was seit since bis zur laufenden Fassung dazukam (nach einem Update; ohne since nur diese).
    notes: {Fassung: {"body": Markdown, "date": JJJJ-MM-TT}} aus selfupdate; lang: "de" | "en"."""
    lang = "en" if lang == "en" else "de"
    notes = notes if isinstance(notes, dict) else {}
    own_key = version_key(own)
    own_plain = "%d.%d.%d" % own_key if own_key else str(own or "")
    log = parse(changelog_text)
    by_version = {s["version"]: s for s in log if s["version"]}

    def note_md(version: str) -> str:
        item = notes.get(version)
        return str(item.get("body") or "")[:MAX_NOTE] if isinstance(item, dict) else ""

    def note_date(version: str) -> str:
        item = notes.get(version)
        return str(item.get("date") or "") if isinstance(item, dict) else ""

    sections: list[dict] = []
    if which == "new":
        low = version_key(since)
        versions = [s["version"] for s in log if s["version"]] + [v for v in notes if version_key(v)]
        wanted = sorted({v for v in versions if version_key(v) and own_key and version_key(v) <= own_key
                         and (version_key(v) == own_key if low is None else version_key(v) > low)},
                        key=version_key, reverse=True)
        if own_key and own_plain not in wanted:
            wanted.insert(0, own_plain)
        for v in wanted:
            entry = by_version.get(v) or {}
            text, text_lang = _pick(lang, entry.get("body", ""), note_md(v))
            if text:
                date = _date_text(entry.get("date") or note_date(v), lang)
                label = "%s · installiert" % v if v == own_plain else (v + (" · " + date if date else ""))
                sections.append(_section(label, text, text_lang, lang))
        title = "Neu im PC5 Launcher %s" % own_plain
    else:
        target = pending or available
        target_key = version_key(target)
        if target_key and own_key and target_key > own_key:
            ahead = sorted({v for v in notes if version_key(v) and own_key < version_key(v) <= target_key} | {target},
                           key=version_key, reverse=True)
            for v in ahead:
                text, text_lang = _pick(lang, "", note_md(v))
                if not text:
                    continue
                label = ("%s · installiert, gilt nach dem Neustart" % v if v == pending else "%s · verfügbar" % v)
                sections.append(_section(label, text, text_lang, lang))
        if kind != "package":
            nxt = next((s for s in log if s["next"]), None)
            if nxt is not None:
                text = to_text(nxt["body"])
                if text:
                    sections.append(_section("Nach %s · noch nicht veröffentlicht" % own_plain, text, "de", lang))
        for s in log:
            key = version_key(s["version"])
            if not key or not own_key or key > own_key:
                continue
            text, text_lang = _pick(lang, s["body"], note_md(s["version"]))
            if not text:
                continue
            if key == own_key:
                label = "%s · installiert" % s["version"]
            else:
                date = _date_text(s["date"] or note_date(s["version"]), lang)
                label = s["version"] + (" · " + date if date else "")
            sections.append(_section(label, text, text_lang, lang))
        title = "Was ist neu im PC5 Launcher"
    return {"title": title, "version": own_plain, "sections": sections[:MAX_SECTIONS],
            "empty": "Zu dieser Fassung sind keine Änderungen verzeichnet."}


def release_notes(data, limit: int = 10) -> dict:
    """Texte der Releases aus GET /releases: {X.Y.Z: {"body", "date"}} für Releases mit Tag vX.Y.Z (keine Entwürfe),
    höchstens limit, Texte gekürzt. Pakete braucht es dafür nicht (auch ältere Fassungen haben Notizen)."""
    out: dict = {}
    for rel in data if isinstance(data, list) else []:
        if not isinstance(rel, dict) or rel.get("draft") is not False:
            continue
        m = re.match(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$", str(rel.get("tag_name") or ""))
        if not m:
            continue
        version = "%d.%d.%d" % tuple(int(x) for x in m.groups())
        if version in out:
            continue
        body = rel.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        date = str(rel.get("published_at") or "")[:10]
        out[version] = {"body": body[:MAX_NOTE], "date": date if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else ""}
        if len(out) >= limit:
            break
    return out
