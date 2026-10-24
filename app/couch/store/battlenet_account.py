"""Was dir bei Battle.net gehört: über die Anmeldung im Browser des PC5 Launchers.

Einen offiziellen Weg gibt es nicht (die öffentlichen OAuth-Scopes von Blizzard reichen nur für
Spielprofile). Playnite (BattleNetLibrary, gepflegt, 2026-01) und das GOG-Galaxy-Plugin lesen
dieselben Stellen wie die Kontoseite selbst, mit den Cookies der Web-Anmeldung:

    account.battle.net/api/                  {"authenticated": bool, "loginUri", "logoutUri", …}
    account.battle.net/api/games-and-subs    {"gameAccounts": [{"titleId", "localizedGameName", …}]}
    account.battle.net/api/classic-games     {"classicGames": [{"localizedGameName", "cdKeys", …}]}

Die Sitzung auf account.battle.net (Cookie SESSIONID) gilt nur 30 Minuten, die Anmeldung bei
oauth.battle.net länger. Wie Playnite frischt owned() darum still auf: REFRESH leitet mit gültiger
Anmeldung ohne Anmeldeseite über oauth.battle.net zurück und setzt eine neue Sitzung (nur in der
eigenen Keksdose; die Datenbank des Browsers bleibt unberührt). Mit einem echten Konto ist das
unbestätigt (Stand 2026-10-22: nur ohne Anmeldung geprüft).

couch kennt nie ein Kennwort; Werte der Cookies kommen nie ins Protokoll. CD-Schlüssel der
Klassiker (classic-games liefert sie mit) verlässt dieses Modul nie.
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request

from .battlenet import code_for
from .steam_account import cookie_rows

ACCOUNT = "https://account.battle.net"
# Anmeldung wie auf der Kontoseite (deren loginUri); ref ist die Seite nach der Anmeldung. Ohne
# Anmeldung führt die Kette über oauth.battle.net zur Anmeldeseite eu.account.battle.net/login/…
# (ohne Anmeldung nachverfolgt am 2026-10-22); ob ref=/overview nach der Anmeldung eingehalten
# wird, ist unbestätigt (Playnite erkennt das Ende an …/overview oder an der Startseite).
LOGIN_URL = ACCOUNT + "/oauth2/authorization/account-settings?ref=/overview"
LOGIN_DONE = ACCOUNT + "/overview"
STATUS = ACCOUNT + "/api/"
GAMES = ACCOUNT + "/api/games-and-subs"
CLASSIC = ACCOUNT + "/api/classic-games"
REFRESH = ACCOUNT + "/oauth2/authorization/account-settings?ref=/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
TIMEOUT_S = 20.0
# Hosts, deren Cookies zur Anmeldung gehören: battle.net, account., eu.account., oauth., eu.
# (die Namen kennt nur Blizzard und sie wechseln; darum alle Cookies dieser Hosts, nicht die des Shops)
_HOST = re.compile(r"^\.?(?:[a-z]{2}\.)?(?:account\.|oauth\.)?battle\.net$")


def is_login_done(uri: str) -> bool:
    """Die Kontoseite nach der Anmeldung (…/overview) oder deren Startseite. Zwischenschritte
    (/oauth2/…, /callback/…, /login/…, Adressen mit code= oder state=) zählen nicht, sonst schlösse
    der Browser, bevor die Sitzung gesetzt ist. Für eine fertig geladene Seite gedacht."""
    uri = str(uri or "")
    if re.search(r"[?&](code|state)=", uri):
        return False
    return uri.startswith(LOGIN_DONE) or uri in (ACCOUNT, ACCOUNT + "/")


def browser_cookies(db: str, clock=time.time) -> dict:
    """{host: {name: wert}} der Battle.net-Cookies aus der Cookie-Datenbank des Browsers ({} ohne
    Anmeldung). Nach Host getrennt, weil dieselben Namen (XSRF-TOKEN, SESSIONID) auf mehreren Hosts
    vorkommen. Liest eine Kopie (steam_account.cookie_rows)."""
    rows = cookie_rows(db, "select name, value, host, expiry from moz_cookies where host like ?", ("%battle.net",))
    if rows is None:
        return {}
    now = clock()
    out: dict = {}
    for name, value, host, expiry in rows:
        host = str(host or "").lower()
        if not name or not _HOST.match(host) or (expiry and int(expiry) < now):
            continue
        out.setdefault(host, {})[str(name)] = str(value or "")
    return out


def remembered(cookies: dict) -> bool:
    """Noch angemeldet im Sinn von „Angemeldet bleiben“: Das Cookie BA-tassadar lebt ein Jahr, die Sitzung auf
    account.battle.net nur 30 Minuten, und ohne echten Browser erneuert Blizzard sie nicht (Prüfung 2026-10-22:
    REFRESH endet auf der Anmeldeseite). Der Store zeigt dann den letzten bekannten Stand."""
    return any(isinstance(v, dict) and v.get("BA-tassadar") for v in (cookies or {}).values())


def make_jar(cookies: dict) -> http.cookiejar.CookieJar:
    """Keksdose aus browser_cookies(): Host mit Punkt gilt für alle Unterhosts, sonst nur für sich."""
    jar = http.cookiejar.CookieJar()
    for host, names in (cookies or {}).items():
        if not isinstance(names, dict):
            continue
        dot = host.startswith(".")
        for name, value in names.items():
            jar.set_cookie(http.cookiejar.Cookie(0, name, value, None, False, host, dot, dot, "/", True, True,
                                                 None, False, None, None, {}))
    return jar


def _get(url: str, jar: http.cookiejar.CookieJar, timeout: float = TIMEOUT_S) -> tuple[int, str]:
    """(HTTP-Status, Text); folgt Umleitungen mit der Keksdose (neue Cookies landen darin)."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8",
                                               "Accept-Language": "de-DE,de;q=0.9"})
    try:
        with opener.open(req, timeout=timeout) as resp:          # noqa: S310 (feste https-Adressen)
            return resp.status, resp.read(8 * 1024 * 1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:                          # 401 ohne Anmeldung: JSON im Rumpf
        try:
            return e.code, e.read(1024 * 1024).decode("utf-8", "replace")
        finally:
            e.close()


def _json(answer) -> dict:
    try:
        _status, text = answer
        data = json.loads(text) if str(text or "").lstrip()[:1] in ("{", "[") else None
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _name(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[®™©]", "", str(text or "")).replace("\xa0", " ")).strip()


def owned(cookies: dict, get=_get) -> dict:
    """{"logged_in": bool, "title_ids": [int], "games": [{"title_id", "name", "code"}]}.
    title_ids ohne Doppelte (WoW hat oft mehrere Spielkonten); Klassiker (Diablo II, Warcraft III
    von früher) stehen mit title_id 0 in games. Ohne Anmeldung oder bei Fehlern logged_in False
    (bei Fehlern zusätzlich "error": True), nie eine Ausnahme."""
    empty = {"logged_in": False, "title_ids": [], "games": []}
    if not any(isinstance(v, dict) and v for v in (cookies or {}).values()):
        return empty
    try:
        jar = make_jar(cookies)
        status = _json(get(STATUS, jar))
        if status.get("authenticated") is not True:
            get(REFRESH, jar)                   # Sitzung abgelaufen: still über die Anmeldung erneuern
            status = _json(get(STATUS, jar))
        if status.get("authenticated") is not True:
            return empty
        code, text = get(GAMES, jar)
        data = _json((code, text))
        if code != 200 or not isinstance(data.get("gameAccounts"), list):
            return dict(empty, error=True)
        games, seen = [], set()
        for acc in data["gameAccounts"]:
            if not isinstance(acc, dict):
                continue
            try:
                tid = int(acc.get("titleId") or 0)
            except (TypeError, ValueError):
                continue
            if tid <= 0 or tid in seen:
                continue
            seen.add(tid)
            games.append({"title_id": tid, "name": _name(acc.get("localizedGameName")), "code": code_for(tid)})
        try:
            classic = _json(get(CLASSIC, jar)).get("classicGames") or []
        except Exception:       # noqa: BLE001 (Klassiker sind Beiwerk; die Liste oben gilt trotzdem)
            classic = []
        names = {g["name"] for g in games}
        for c in classic if isinstance(classic, list) else []:
            name = _name((c or {}).get("localizedGameName")) if isinstance(c, dict) else ""
            if name and name not in names:
                names.add(name)
                games.append({"title_id": 0, "name": name, "code": ""})
        return {"logged_in": True, "title_ids": [g["title_id"] for g in games if g["title_id"]], "games": games}
    except Exception:           # Netz weg, Antwort kaputt: wie abgemeldet, der Store bleibt bedienbar
        return dict(empty, error=True)
