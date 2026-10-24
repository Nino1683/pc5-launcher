"""Was dir bei Rockstar gehört und was installiert ist.

Besitz, Weg 1 (Shop): Der Browser des PC5 Launchers meldet sich bei Rockstar an (LOGIN_URL); danach
liefert der Shop seine Seiten mit den Cookies dieser Anmeldung aus, und jede SKU trägt
purchaseState 2 („besessen“) statt 1 („kaufbar“). Das Feld und seine Werte stehen im Programmcode
des Shops (2026-10-22); mit einem echten Konto ist das unbestätigt. couch kennt nie ein Kennwort,
Werte der Cookies kommen nie ins Protokoll.

Besitz, Weg 2 (Launcher): Sobald der Rockstar Games Launcher im Prefix angemeldet ist, schreibt er in
Documents/Rockstar Games/Launcher/launcher.log, für welche Titel er einen Zweig (Download) bekommt.

Installiert: die Uninstall-Einträge des Launchers in der Registry des Prefix (system.reg):
UninstallString „…\\Launcher.exe" -enableFullMode -uninstall=<kürzel>“ und InstallLocation
(so liest es auch Playnite), ersatzweise HKLM\\SOFTWARE\\WOW6432Node\\Rockstar Games\\<Name>\\InstallFolder.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import rockstar
from .steam_account import cookie_rows

STORE = rockstar.STORE
# Rückkehr auf eine Shop-Seite mit Marke; die Anmeldung des Shops selbst nutzt dieselbe Adresse
# (appSettings.loginUrl, Parameter lang und returnUrl relativ zum Shop)
LOGIN_URL = ("https://signin.rockstargames.com/connect/authorize/t2gp-rs?lang=de-DE&returnUrl="
             + urllib.parse.quote("/de?pc5=angemeldet", safe=""))
LOGIN_DONE = "pc5=angemeldet"
# Cookies, die ein Browser an store.rockstargames.com schickt: eigene des Shops (u. a.
# __Host-AccessToken) und solche für die ganze Domain (Anmeldung). Die Namen wechseln teils
# (TS…, rsso…), darum alle dieser Hosts.
HOSTS = ("store.rockstargames.com", ".store.rockstargames.com", ".rockstargames.com")
TOKEN = "__Host-AccessToken"
REFRESH = STORE + "/api/refreshtoken"
USER_AGENT = rockstar.USER_AGENT
LOG = "drive_c/users/steamuser/Documents/Rockstar Games/Launcher/launcher.log"
# Untertitel und Bündel aus title_metadata.json, keine eigenen Spiele im Launcher
NOT_TITLES = {"launcher", "rdr2_sp", "rdr2_rdo", "rdr2_sp_rgl", "rdr2_sp_steam", "rdr2_sp_epic", "gtatrilogy"}


def is_login_done(uri: str) -> bool:
    """Nach LOGIN_URL wieder auf einer Shop-Seite: angemeldet (auch falls Rockstar die Marke LOGIN_DONE
    nicht durchreicht, wie bei Steam). Zwischenschritte des Shops (/api/…, Adressen mit code= oder
    state=) zählen nicht, sonst schlösse der Browser, bevor die Cookies gesetzt sind. Für eine
    fertig geladene Seite gedacht; unbestätigt, ob Rockstar genau so zurückleitet."""
    uri = str(uri or "")
    if not uri.startswith(STORE + "/"):
        return False
    parts = urllib.parse.urlsplit(uri)
    return not parts.path.startswith("/api/") and not re.search(r"(^|&)(code|state)=", parts.query)


def browser_cookies(db: str, clock=time.time) -> dict:
    """{name: wert} der Cookies für den Rockstar-Shop aus der Cookie-Datenbank des Browsers ({} ohne
    Anmeldung). Liest eine Kopie (steam_account.cookie_rows)."""
    rows = cookie_rows(db, "select name, value, host, expiry from moz_cookies where host in (%s)"
                       % ",".join("?" * len(HOSTS)), HOSTS)
    if rows is None:
        return {}
    now = clock()
    out, rank = {}, {}
    for name, value, host, expiry in rows:
        if not name or (expiry and int(expiry) < now):
            continue
        r = HOSTS.index(host) if host in HOSTS else 9      # der genauere Host gewinnt
        if name not in out or r < rank[name]:
            out[name], rank[name] = str(value or ""), r
    return out


def _get(url: str, cookies: dict, timeout: float = 20.0) -> str:
    jar = "; ".join("%s=%s" % kv for kv in sorted(cookies.items()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html",
                                               "Cookie": jar})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (feste https-Adresse)
        return resp.read(16 * 1024 * 1024).decode("utf-8", "replace")


def _expiry(token: str) -> float:
    """Ablauf (exp) des Zugangs-Schlüssels (JWT); 0 wenn unlesbar."""
    try:
        payload = urllib.parse.unquote(token).split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data.get("exp") or 0)
    except (IndexError, ValueError, TypeError, AttributeError):
        return 0.0


def _fresh(cookies: dict, get, clock) -> dict:
    """Abgelaufenen Zugangs-Schlüssel erneuern, wie es die Shop-Seite selbst tut (/api/refreshtoken
    liefert {"accessToken"}); die Datenbank des Browsers bleibt unberührt."""
    token = cookies.get(TOKEN) or ""
    exp = _expiry(token) if token else 0.0
    if token and exp and exp - 60 > clock():
        return cookies
    try:
        data = json.loads(get(REFRESH, cookies))
        new = data.get("accessToken") if isinstance(data, dict) else None
    except Exception:       # noqa: BLE001 (ohne neuen Schlüssel geht es mit dem alten weiter)
        return cookies
    return dict(cookies, **{TOKEN: new}) if isinstance(new, str) and new.count(".") == 2 else cookies


def _props(text: str) -> dict:
    data = json.loads(text)
    return data.get("pageProps") if isinstance(data, dict) and isinstance(data.get("pageProps"), dict) else {}


def owned(cookies: dict, get=_get, clock=time.time) -> dict:
    """{"logged_in", "slugs", "codes"} über den Shop mit den Cookies der Anmeldung (purchaseState 2).
    Ohne Anmeldung oder bei Fehlern logged_in False (dann mit "error": True); wirft nie."""
    empty = {"logged_in": False, "slugs": [], "codes": []}
    if not cookies:
        return empty
    try:
        cookies = _fresh(dict(cookies), get, clock)
        home = rockstar.next_data(get(rockstar.HOME, cookies))
        build = str(home.get("buildId") or "")
        profile = ((home.get("props") or {}).get("pageProps") or {}).get("profile")
        if not build:
            return dict(empty, error=True)
        if not (isinstance(profile, dict) and profile):
            return empty
        games = rockstar.products(_props(get(rockstar.DATA % (build, "view-all-games"), cookies)))
        slugs = []
        for p in games:
            mine = any(s["state"] == rockstar.OWNED for s in p["skus"])
            if not mine:
                # eine andere Edition (etwa Ultimate) steht nur auf der Seite des Spiels
                try:
                    pp = _props(get(rockstar.DATA % (build, "game/" + p["slug"]), cookies))
                    mine = any(s["state"] == rockstar.OWNED and not re.search(r"GOLDBAR|SHARKCARD|CASHCARD", s["external"])
                               for s in rockstar.pc_skus(pp.get("skus")))
                except Exception as e:  # noqa: BLE001 (eine Seite fehlt: die übrigen zählen trotzdem)
                    if hasattr(e, "close"):
                        e.close()           # HTTPError hält sonst die Antwort offen
                    mine = False
            if mine:
                slugs.append(p["slug"])
    except Exception:       # Netz weg, Antwort kaputt: wie abgemeldet, der Store bleibt bedienbar
        return dict(empty, error=True)
    codes = [c for s in slugs for c in rockstar.SLUG_CODES.get(s, ())]
    return {"logged_in": True, "slugs": slugs, "codes": codes}


# Zeilen des Launcher-Protokolls. Beide Fassungen sind nicht mit einem echten Protokoll geprüft:
# alt (bis 2022, so liest es das GOG-Galaxy-Plugin): „<kürzel>: … on branch …“ / „… no branches!“
# neu (Texte in Launcher.exe 1.0.109): „Selecting first available branch for <kürzel>: <zweig>“ /
# „Unflagging presumable ownership of <kürzel> due to lack of default branch.“
_NEW = ((re.compile(r"Selecting first available branch for ([a-z0-9_]+):"), True),
        (re.compile(r"Unflagging presumable ownership of ([a-z0-9_]+)\b"), False))
# die alte Form nennt nur „<wort>:“; damit keine anderen Meldungen mitzählen, nur bekannte Kürzel
_OLD = ((re.compile(r"(?:^|[\]\s])([a-z0-9_]+):\s.*\bon branch\b"), True),
        (re.compile(r"(?:^|[\]\s])([a-z0-9_]+):\s.*\bno branches!"), False))


def owned_from_log(text: str) -> list[str]:
    """Kürzel der Titel, die der angemeldete Launcher laut launcher.log besitzt (die letzte Zeile zu
    einem Titel gilt), sortiert."""
    state: dict[str, bool] = {}
    for line in (text or "").splitlines():
        for pat, mine in _NEW + _OLD:
            m = pat.search(line)
            code = m.group(1) if m else ""
            if not code or code in NOT_TITLES or ((pat, mine) in _OLD and code not in rockstar.TITLES):
                continue
            state[code] = mine
            break
    return sorted(code for code, mine in state.items() if mine)


def _unescape(value: str) -> str:
    """Zeichenketten aus Wines .reg-Dateien: \\\\, \\", \\n …, Oktal und \\x<hex> für Nicht-ASCII-Zeichen
    (Wine schreibt bis zu vier Ziffern, vier, wenn danach eine Hex-Ziffer folgt: „Gr\\xfc\\x00dfe“)."""
    def one(m):
        c = m.group(1)
        if c[0] == "x":
            return chr(int(c[1:], 16))
        if c[0] in "01234567":
            return chr(int(c, 8))
        return {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b"}.get(c, c)
    return re.sub(r"\\(x[0-9a-fA-F]{1,4}|[0-7]{1,3}|.)", one, value)


def _reg_sections(text: str) -> dict:
    """{schlüssel (klein, mit einfachem \\): {wertname (klein): wert}} aus einer Wine-.reg-Datei."""
    out, current = {}, None
    value = re.compile(r'^"((?:[^"\\]|\\.)*)"=(?:str\(\d\):)?"((?:[^"\\]|\\.)*)"\s*$')
    for line in (text or "").splitlines():
        if line.startswith("["):
            m = re.match(r"^\[(.*?)\](?:\s+\d+)?\s*$", line)
            current = out.setdefault(_unescape(m.group(1)).lower(), {}) if m else None
            continue
        if current is None:
            continue
        m = value.match(line)
        if m:
            current[_unescape(m.group(1)).lower()] = _unescape(m.group(2))
    return out


def windows_to_linux(path: str, prefix: str) -> str:
    """„C:\\Program Files\\…“ -> „<prefix>/drive_c/Program Files/…“ (Z: ist bei Wine das Wurzelverzeichnis)."""
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", path or "")
    if not m or not prefix:
        return path
    rest = m.group(2).replace("\\", "/").rstrip("/")
    if m.group(1).lower() == "z":
        return "/" + rest
    return os.path.join(prefix, "drive_%s" % m.group(1).lower(), rest)


def installed_from_registry(system_reg_text: str, prefix: str = "") -> dict:
    """{kürzel: Spielordner} aus system.reg eines Prefix. Mit prefix (Pfad zum pfx-Ordner) als
    Linux-Pfad, sonst als Windows-Pfad. Uninstall-Einträge des Launchers zuerst (sie nennen das
    Kürzel selbst), dann InstallFolder unter Rockstar Games\\<Name> für bekannte Titel."""
    sections = _reg_sections(system_reg_text)
    out: dict[str, str] = {}
    for key, values in sections.items():
        if "\\currentversion\\uninstall\\" not in key:
            continue
        m = re.search(r"(?:launcher|uninstall)\.exe\"?.*?-uninstall=([A-Za-z0-9_]+)", values.get("uninstallstring", ""), re.I)
        folder = values.get("installlocation", "").strip().strip('"').rstrip("\\/")
        if m and folder and m.group(1).lower() not in NOT_TITLES:
            out.setdefault(m.group(1).lower(), folder)
    by_registry = {t["registry"].lower(): code for code, t in rockstar.TITLES.items()}
    for key, values in sections.items():
        m = re.search(r"software\\(?:wow6432node\\)?rockstar games\\([^\\]+)$", key)
        code = by_registry.get(m.group(1)) if m else None
        folder = values.get("installfolder", "").strip().strip('"').rstrip("\\/")
        if code and folder:
            out.setdefault(code, folder)
    return {code: windows_to_linux(folder, prefix) for code, folder in sorted(out.items())}
