"""Titelvergleich für die Bildersuche: lieber kein Treffer als ein falscher.

Zwei Durchgänge je Quelle:

  streng     derselbe Titel, höchstens mit Tippfehler-Abstand (``score`` >= 0,9), oder ein Kürzel
             im Suchbegriff steht für die Anfangsbuchstaben mehrerer Wörter („SotT“).
  tolerant   nur wenn der strenge nichts findet:
             Ausgabe     gleicher Titel, sobald man hinten die Ausgaben-Wörter weglässt
                         („Horizon Zero Dawn“ / „Horizon Zero Dawn™ Remastered“)
             Untertitel  der Suchbegriff endet auf eine Nummer, der Store-Titel beginnt mit ihm
                         und danach folgt ein Trenner („The Witcher 3“ / „The Witcher 3: Wild
                         Hunt“), nie eine Zahl oder ein weiteres Titelwort
             Einschub    der Store-Titel schiebt zwischen zwei Trennern einen Untertitel ein
                         („Broken Sword - Reforged“ / „Broken Sword - Shadow of the Templars: Reforged“)

Sperren im toleranten Durchgang: Zubehör (DLC, Upgrade, Soundtrack, Demo, Werkzeug) zählt nie;
mehrere gleich gute Kandidaten ergeben keinen Treffer; nennt die Trefferliste noch ein anderes
Spiel mit dem Suchbegriff im Namen („Shadow of the Tomb Raider“), ist der Suchbegriff ein
Reihenname und es gibt nichts. Bei Konsolenspielen zählt die Systemangabe.

Alles hier ist rein (keine Platte, kein Netz) und schnell.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

MIN_SCORE = 0.9                    # streng: darunter gilt ein Name nicht als derselbe
SCORE_ACRONYM = 0.95
SCORE_EDITION = 0.88
SCORE_SUBTITLE = 0.8
SCORE_INFIX = 0.75

STRICT_LEVELS = ("strict", "acronym")
LEVEL_ORDER = ("strict", "acronym", "edition", "subtitle", "infix")

_MARKS = re.compile(r"[™®©℠]")
# Klammerzusätze, die nur die Plattform nennen. Andere bleiben stehen: „Resident Evil 2 (1998)“
# ist ein anderes Spiel als „Resident Evil 2“, das Jahr gehört dort zum Namen.
_BRACKETS = re.compile(
    r"[(\[][^()\[\]]*\b(?:xbox|pc|windows|win ?1[01]|playstation|ps ?[45]|switch|steam|mac|linux)\b[^()\[\]]*[)\]]",
    re.I)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# Trenner zwischen zwei Wörtern: Doppelpunkt, Gedankenstrich, Bindestrich mit Leerzeichen
_SEP_GAP = re.compile(r":|[–—]|\s-+\s")
_ROMAN = frozenset("i ii iii iv v vi vii viii ix x xi xii xiii xiv xv xvi xvii xviii xix xx".split())

# Ausgaben-Zusätze: nur am ENDE eines Titels gelten sie als Zusatz („Ultimate Chicken Horse“
# bleibt). Eigenschaftswörter zählen nur zusammen mit „Edition“: „Tomb Raider: Anniversary“,
# „Mario Kart 8 Deluxe“ und „Super Smash Bros. Ultimate“ sind eigene Spiele.
_EDITION_KINDS = ("definitive", "complete", "enhanced", "deluxe", "ultimate", "gold", "premium", "standard",
                  "digital", "legendary", "anniversary", "special", "collectors", "collector s", "royal",
                  "game of the year", "goty")
EDITION_PHRASES = tuple(sorted((tuple(p.split()) for p in (
    "game of the year", "goty", "director s cut", "directors cut", "master collection", "stand alone",
    "standalone", "the final cut", "final cut", "for windows", "remastered", "remaster", "enhanced",
    "hd", "edition", "version", "pc", "windows",
) + tuple(k + " edition" for k in _EDITION_KINDS)), key=len, reverse=True))

# Zubehör und Werkzeuge: kein Spiel, außer der Suchbegriff nennt dasselbe Wort
NONGAME_PHRASES = tuple(sorted((tuple(p.split()) for p in (
    "dlc", "soundtrack", "original soundtrack", "ost", "upgrade", "season pass", "expansion pass",
    "demo", "artbook", "art book", "bonus content", "pack", "bundle", "add on", "addon", "sdk",
    "redkit", "modkit", "toolkit", "devkit", "editor", "mod tools", "modding tools",
    "creation kit", "dedicated server", "benchmark", "playtest", "trailer", "wallpaper",
    "wallpapers", "erweiterung", "erweiterungspass", "saisonpass", "zusatzinhalt",
    "zusatzinhalte", "zusatzszenario", "paket", "sprachpaket", "language pack", "kostum", "kostume",
    "costume", "costumes", "skin", "skins", "trial", "testversion", "cashcard",
)), key=len, reverse=True))


# ------------------------------------------------------------------ Grundformen

def norm(text: str) -> str:
    """Kleinbuchstaben, nur a–z und 0–9, einfache Leerzeichen."""
    # Zeichen wie ™ zuerst entfernen: NFKD würde daraus die Buchstaben "TM" machen
    text = _MARKS.sub("", text or "").replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def strip_brackets(text: str) -> str:
    """Klammerzusätze mit der Plattform entfernen („(Xbox One)“, „[PC]“)."""
    out = _BRACKETS.sub(" ", text or "")
    return out if _WORD.search(out) else (text or "")


def parts(text: str) -> list:
    """[(wort, trenner_davor)] in Kleinbuchstaben. Anders als norm() bleiben fremde Buchstaben
    als Wort erhalten („Δ“): Sie sind ein weiteres Titelwort, kein Trenner."""
    text = _MARKS.sub("", strip_brackets(text)).replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    out, pos = [], 0
    for m in _WORD.finditer(text):
        gap = text[pos:m.start()]
        out.append((m.group(), bool(out) and bool(_SEP_GAP.search(gap))))
        pos = m.end()
    return out


def words(text: str) -> list:
    return [w for w, _sep in parts(text)]


def _numbers(tokens: list) -> list:
    return sorted(t for t in tokens if t.isdigit() or t in _ROMAN)


def _same_word(a: str, b: str) -> bool:
    """Gleiches Wort bis auf einen Tippfehler? Kurze Wörter und Mehrzahl zählen als verschieden."""
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5:
        return False                                   # „Doom“ / „Room“
    if a.rstrip("s") == b.rstrip("s"):
        return False                                   # „Alien“ / „Aliens“ sind verschiedene Spiele
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def score(query: str, name: str) -> float:
    """Ähnlichkeit 0…1. Sicher (>= 0,9) ist nur derselbe Titel, höchstens mit Tippfehler-Abstand.

    Die reine Zeichen-Ähnlichkeit reicht nicht: „Resident Evil 2“ und „Resident Evil 3“ kämen
    auf 0,93, „Alan Wake 2“ und „Alan Wake“ auf 0,90. Deshalb Sperren: ein Wort mehr oder
    weniger (Fortsetzung, Untertitel, DLC), abweichende Zahlen (auch römische), ein anderes
    Wort an gleicher Stelle. Der Store ist voll von Spielen mit fast gleichem Namen.
    """
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q.replace(" ", "") == n.replace(" ", ""):
        return 0.98                                    # „Spider-Man“ / „Spiderman“
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    qt, nt = q.split(), n.split()
    if len(qt) != len(nt):
        return min(ratio, 0.85)
    if _numbers(qt) != _numbers(nt):
        return min(ratio, 0.80)
    if not all(_same_word(a, b) for a, b in zip(qt, nt)):
        return min(ratio, 0.85)
    return ratio


def variants(title: str) -> list:
    """Schreibweisen eines Titels für Dienste, deren Suche an ' - ' scheitert."""
    seen, out = set(), []
    for v in (title, title.replace(" - ", ": "), title.replace(" - ", " "),
              re.sub(r"\(.*?\)|\[.*?\]", "", title).strip()):
        v = re.sub(r"\s+", " ", v).strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


def query_text(term: str) -> str:
    """Suchbegriff, wie er an einen Dienst geht: ohne Marken-Zeichen, Klammern und
    Trennstriche (die Steam-Suche findet „A - B“ nicht, „A B“ schon)."""
    text = _MARKS.sub("", strip_brackets(term or ""))
    text = re.sub(r"\s[-–—]+\s|[–—_()\[\]]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def slugify(term: str) -> str:
    """„Alan Wake 2“ -> „alan-wake-2“ (Adressform der Shops)."""
    return "-".join(norm(strip_brackets(term)).split())


def search_terms(*names) -> list:
    """Suchbegriffe eines Eintrags ohne Doppelte (verglichen nach norm): Titel zuerst, dann
    Ordnername, dann der Slug als Wörter."""
    out, seen = [], set()
    for name in names:
        if not isinstance(name, str):
            continue
        text = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
        key = norm(strip_brackets(text))
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# ------------------------------------------------------------------ Bausteine des Vergleichs

def _ends_with(tokens: list, phrase: tuple) -> bool:
    return len(tokens) >= len(phrase) and tuple(tokens[-len(phrase):]) == phrase


def strip_editions(tokens: list) -> tuple:
    """(Titel ohne Ausgaben-Wörter am Ende, Zahl der entfernten Wörter). Nie bis auf nichts."""
    base, removed = list(tokens), 0
    changed = True
    while changed:
        changed = False
        for phrase in EDITION_PHRASES:
            if len(base) > len(phrase) and _ends_with(base, phrase):
                del base[-len(phrase):]
                removed += len(phrase)
                changed = True
                break
    return base, removed


def _contains(tokens: list, phrase) -> bool:
    n = len(phrase)
    return n > 0 and any(tuple(tokens[i:i + n]) == tuple(phrase) for i in range(len(tokens) - n + 1))


def is_accessory(name: str, term: str = "") -> bool:
    """Zubehör statt Spiel (DLC, Upgrade, Soundtrack, Demo, Werkzeug)? Nennt der Suchbegriff
    dasselbe Wort selbst („… Demo“, „… Party Pack“), ist es keins."""
    tokens, mine = words(name), words(term)
    return any(_contains(tokens, p) and not _contains(mine, p) for p in NONGAME_PHRASES)


def _acronym(short: list, full: list, used: bool = False) -> bool:
    """Passt short auf full, wenn ein Wort von short die Anfangsbuchstaben mehrerer Wörter ist?"""
    if not short:
        return used and not full
    if not full:
        return False
    if short[0] == full[0] and _acronym(short[1:], full[1:], used):
        return True
    w = short[0]
    if len(w) >= 3 and w.isalpha() and len(full) >= len(w) and all(full[i][:1] == w[i] for i in range(len(w))):
        return _acronym(short[1:], full[len(w):], True)
    return False


def relate(term: str, name: str) -> tuple:
    """Wie steht ein Store-Titel zum Suchbegriff? (stufe, wert): stufe aus LEVEL_ORDER oder None."""
    sc = score(strip_brackets(term), strip_brackets(name))
    if sc >= MIN_SCORE:
        return "strict", sc
    cparts = parts(name)
    tt, ct = words(term), [w for w, _s in cparts]
    if not tt or not ct:
        return None, 0.0
    if len(tt) <= 12 and len(ct) <= 12 and (_acronym(tt, ct) or _acronym(ct, tt)):
        return "acronym", SCORE_ACRONYM
    tb, _tn = strip_editions(tt)
    cb, _cn = strip_editions(ct)
    if tb == cb or score(" ".join(tb), " ".join(cb)) >= MIN_SCORE:
        return "edition", SCORE_EDITION
    n = len(tb)
    # Untertitel nur hinter einer Nummer: „The Witcher 3“ meint genau einen Teil, „Tomb Raider“
    # dagegen eine ganze Reihe („Tomb Raider: Legend“ wäre ein anderes Spiel)
    numbered = n >= 2 and (tb[-1].isdigit() or tb[-1] in _ROMAN)
    if numbered and len(cb) > n and cb[:n] == tb and cparts[n][1]:
        # genau EIN Untertitel: „X 3: Wild Hunt — Songs of the Past“ ist ein Zusatz zu
        # „X 3: Wild Hunt“, kein anderer Name für „X 3“
        if sum(1 for _w, sep in cparts[n:len(cb)] if sep) == 1:
            return "subtitle", SCORE_SUBTITLE
    if len(cb) > n >= 3:
        for k in range(2, n):                          # Anfang mindestens zwei Wörter, Ende mindestens eins
            tail = tb[k:]
            cut = len(cb) - len(tail)
            if cb[:k] == tb[:k] and cb[cut:] == tail and cparts[k][1] and cparts[cut][1]:
                return "infix", SCORE_INFIX
    return None, 0.0


# ------------------------------------------------------------------ Auswahl

def _system_tier(cand: dict, system) -> int:
    """0 = passt zum System (oder keine Angabe), 1 = Schwester-System, 2 = fremd."""
    if not system:
        return 0
    have = cand.get("systems")
    if not have:
        return 0
    if system in have:
        return 0
    return 1 if set(have) & {"switch", "wiiu"} else 2


def pick(terms, candidates, system=None) -> tuple:
    """Den sicheren Treffer aus einer Kandidatenliste wählen.

    terms       Suchbegriffe des Eintrags (Titel, Ordnername, Slug als Wörter)
    candidates  [{"name", "ref", "game": True|False|None, "systems"?: [..]}, …]; game None =
                noch unbekannt (die Quelle klärt das mit details())
    system      "switch" | "wiiu" | None

    Rückgabe (kandidat | None, info) mit info = {"score", "level", "term", "reason",
    "unverified": [kandidaten, deren Art die Entscheidung trägt und noch unbekannt ist]}.
    """
    terms = [t for t in terms if t]
    usable = [c for c in candidates if isinstance(c, dict) and c.get("name") and c.get("game") is not False]
    for tier in (0, 1):
        pool = [c for c in usable if _system_tier(c, system) == tier]
        if not pool:
            continue
        hit, info = _pick_in(terms, pool, usable)
        if hit is not None or info.get("unverified") or info.get("reason") == "mehrdeutig":
            return hit, info
    return None, {"score": 0.0, "level": None, "term": "", "reason": "kein sicherer Treffer", "unverified": []}


def _rate(terms, pool) -> list:
    rated = []
    for c in pool:
        best = None
        for term in terms:
            level, sc = relate(term, c["name"])
            if level is None:
                continue
            if level not in STRICT_LEVELS and is_accessory(c["name"], term):
                continue
            rank = (LEVEL_ORDER.index(level), -sc)
            if best is None or rank < best[0]:
                best = (rank, level, sc, term)
        if best:
            rated.append({"cand": c, "level": best[1], "score": best[2], "term": best[3]})
    return rated


def _same_ref(a: dict, b: dict) -> bool:
    return a.get("ref") is not None and a.get("ref") == b.get("ref") and a.get("source") == b.get("source")


def _result(rate: dict, reason: str = "") -> tuple:
    c = rate["cand"]
    info = {"score": round(float(rate["score"]), 3), "level": rate["level"], "term": rate["term"],
            "reason": reason, "unverified": [c] if c.get("game") is None else []}
    return (None if info["unverified"] else c), info


def _none(reason: str, unverified=()) -> tuple:
    return None, {"score": 0.0, "level": None, "term": "", "reason": reason,
                  "unverified": [c for c in unverified if c.get("game") is None]}


def _pick_in(terms, pool, everything) -> tuple:
    rated = _rate(terms, pool)
    if not rated:
        return _none("kein sicherer Treffer")
    # ---- streng: der beste Wert gewinnt; zwei verschiedene Spiele mit gleich gutem Namen = nichts
    strict = [r for r in rated if r["level"] in STRICT_LEVELS]
    if strict:
        unknown = [r["cand"] for r in strict if r["cand"].get("game") is None]
        if unknown:
            # Erst klären, was davon ein Spiel ist (die Suche nennt auch DLC und Musik)
            return _none("ungeprüft", unknown[:5])
        top = max(r["score"] for r in strict)
        best = [r for r in strict if r["score"] == top]
        if len(best) > 1 and not all(_same_ref(best[0]["cand"], r["cand"]) for r in best[1:]):
            return _none("mehrdeutig")
        return _result(best[0])
    # ---- tolerant, Stufe „Ausgabe“: derselbe Titel in anderer Ausgabe
    editions = [r for r in rated if r["level"] == "edition"]
    if editions:
        for r in editions:
            r["extra"] = strip_editions(words(r["cand"]["name"]))[1]
        least = min(r["extra"] for r in editions)
        best = [r for r in editions if r["extra"] == least]
        unknown = [r["cand"] for r in best if r["cand"].get("game") is None]
        if unknown:
            return _none("ungeprüft", unknown[:5])
        if len(best) > 1:
            return _none("mehrdeutig")
        return _result(best[0])
    # ---- tolerant, Stufen „Untertitel“ und „Einschub“
    loose = [r for r in rated if r["level"] in ("subtitle", "infix")]
    for r in loose:
        r["parts"] = parts(r["cand"]["name"])
        r["base"], r["extra"] = strip_editions([w for w, _s in r["parts"]])
    roots = []
    for r in loose:
        # „X: Untertitel - Zusatz“ gehört zu „X: Untertitel“, wenn es den auch gibt
        owned = any(o is not r and len(o["base"]) < len(r["base"]) and r["base"][:len(o["base"])] == o["base"]
                    for o in loose)
        if not owned:
            roots.append(r)
    bases = {tuple(r["base"]) for r in roots}
    if len(bases) != 1:
        return _none("mehrdeutig")
    least = min(r["extra"] for r in roots)
    best = [r for r in roots if r["extra"] == least]
    unknown = [r["cand"] for r in best if r["cand"].get("game") is None]
    if unknown:
        return _none("ungeprüft", unknown[:5])
    if len(best) > 1:
        return _none("mehrdeutig")
    winner = best[0]
    # Reihen-Sperre: Nennt die Liste ein weiteres Spiel mit dem Suchbegriff im Namen, ist der
    # Suchbegriff ein Reihenname („Tomb Raider“) und der Untertitel-Treffer nur einer von vielen
    phrase = strip_editions(words(winner["term"]))[0]
    family = winner["base"]
    for c in everything:
        if c is winner["cand"] or is_accessory(c["name"], winner["term"]):
            continue
        tokens = strip_editions(words(c["name"]))[0]
        if tokens[:len(family)] == family:
            continue                                   # Ausgabe oder Zusatz desselben Spiels
        if _contains(tokens, phrase):
            return _none("Reihenname")
    return _result(winner)
