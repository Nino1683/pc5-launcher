"""Was dir bei Steam gehört (Bauplan E68): über die Anmeldung im Browser des PC5 Launchers.

Der Store fragt dieselbe Stelle wie die Shop-Seite (store.steampowered.com/dynamicstore/userdata/)
mit den Cookies, die der Browser nach der Anmeldung bei Steam gespeichert hat
(<data>/browser/cookies.sqlite). couch kennt dabei nie ein Kennwort; abmelden heißt im Browser
„Daten löschen“ oder bei Steam abmelden. Werte der Cookies kommen nie ins Protokoll.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request

USERDATA = "https://store.steampowered.com/dynamicstore/userdata/"
# Nach der Anmeldung leitet Steam auf redir um; die Marke pc5=angemeldet erkennt der Browser und
# schließt sich (zurück in den Store)
LOGIN = "https://store.steampowered.com/login/?redir=account%2F%3Fpc5%3Dangemeldet&redir_ssl=1"
LOGIN_DONE = "pc5=angemeldet"
HOSTS = ("store.steampowered.com", ".store.steampowered.com", "steampowered.com", ".steampowered.com")
NAMES = ("steamLoginSecure", "sessionid", "browserid", "steamCountry")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) PC5-Launcher"


# Kopie der Cookie-Datenbank: die Datei, ihr Write-Ahead-Protokoll und dessen Index; beim
# Abräumen auch ein -journal, falls SQLite selbst eins anlegt
_COPY = "c.sqlite"
_COPIED = ("", "-wal", "-shm")
_DROPPED = _COPIED + ("-journal",)


def _drop_copy(tmp: str) -> None:
    """Kopie der Cookie-Datenbank abräumen: nur ihre eigenen Dateien, dann den leeren Ordner. Keine
    zweite rekursive Löschstelle (SAFETY 13, E99): Liegt sonst etwas darin, bleibt er liegen."""
    for suffix in _DROPPED:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(tmp, _COPY + suffix))
    with contextlib.suppress(OSError):
        os.rmdir(tmp)


def cookie_rows(db: str, sql: str, params: tuple = ()) -> list | None:
    """Zeilen einer Abfrage auf der Cookie-Datenbank des Browsers, gelesen aus einer Kopie (der
    Browser kann die Datei gerade offen haben); None, wenn sie fehlt oder unlesbar ist. Gemeinsam
    für Steam, Battle.net und Rockstar."""
    if not os.path.isfile(db):
        return None
    tmp = tempfile.mkdtemp(prefix="couch-cookies-")
    try:
        for suffix in _COPIED:
            if os.path.isfile(db + suffix):
                shutil.copy2(db + suffix, os.path.join(tmp, _COPY + suffix))
        con = sqlite3.connect(os.path.join(tmp, _COPY))
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):       # OSError: Datei verschwand beim Kopieren (Browser schloss gerade)
        return None
    finally:
        _drop_copy(tmp)


def browser_cookies(db: str, clock=time.time) -> dict:
    """{name: wert} der Steam-Cookies aus der Cookie-Datenbank des Browsers ({} ohne Anmeldung)."""
    rows = cookie_rows(db, "select name, value, host, expiry from moz_cookies where name in (%s) and host in (%s)"
                       % (",".join("?" * len(NAMES)), ",".join("?" * len(HOSTS))), NAMES + HOSTS)
    if rows is None:
        return {}
    now = clock()
    out = {}
    for name, value, host, expiry in rows:
        if expiry and int(expiry) < now:
            continue
        if name not in out or host.lstrip(".") == "store.steampowered.com":
            out[name] = value
    return out


def _get(url: str, cookies: dict, timeout: float = 15.0) -> dict:
    jar = "; ".join("%s=%s" % kv for kv in sorted(cookies.items()))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Cookie": jar})
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (feste https-Adresse)
        return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))


def country(cookies: dict) -> str:
    """Land des Steam-Shops aus dem Cookie steamCountry („DE%7C<prüfsumme>“), klein; "" ohne Cookie. Für
    die Preise der Wunschliste (E122): Steam rechnet in der Währung dieses Landes."""
    raw = urllib.parse.unquote(str(cookies.get("steamCountry") or "")).split("|")[0].strip().lower()
    return raw if len(raw) == 2 and raw.isalpha() else ""


def userdata(cookies: dict, get=_get, clock=time.time) -> dict:
    """{"logged_in", "owned": set, "wishlist": set, "ignored": set, "country"}. Ohne Anmeldung alles leer.
    rgWishlist kommt in der Reihenfolge der Wunschliste; "wishlist_order" behält sie (E122 prüft bei sehr
    langen Listen die vorderen zuerst)."""
    empty = {"logged_in": False, "owned": set(), "wishlist": set(), "ignored": set(), "wishlist_order": [],
             "country": country(cookies)}
    if not cookies.get("steamLoginSecure"):
        return empty
    try:
        data = get("%s?t=%d" % (USERDATA, int(clock())), cookies)
    except Exception:       # Netz weg, Antwort kaputt: wie abgemeldet, der Store bleibt bedienbar
        return dict(empty, error=True)
    if not isinstance(data, dict):
        return dict(empty, error=True)
    owned = {int(x) for x in data.get("rgOwnedApps") or [] if str(x).isdigit()}
    packages = data.get("rgOwnedPackages") or []
    order = list(dict.fromkeys(int(x) for x in data.get("rgWishlist") or [] if str(x).isdigit()))
    return {"logged_in": bool(owned or packages), "owned": owned,
            "wishlist": set(order), "wishlist_order": order,
            "ignored": {int(x) for x in (data.get("rgIgnoredApps") or {}) if str(x).isdigit()},
            "country": country(cookies)}
