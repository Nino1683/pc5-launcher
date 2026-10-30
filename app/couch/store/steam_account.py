"""Was dir bei Steam gehört (Bauplan E68): über die Anmeldung im Browser des PC5 Launchers.

Der Store fragt dieselbe Stelle wie die Shop-Seite (store.steampowered.com/dynamicstore/userdata/)
mit den Cookies, die der Browser nach der Anmeldung bei Steam gespeichert hat
(<data>/browser/cookies.sqlite). couch kennt dabei nie ein Kennwort; abmelden heißt im Browser
„Daten löschen“ oder bei Steam abmelden. Werte der Cookies kommen nie ins Protokoll.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
import time
import urllib.parse

from . import net

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


COPY_TRIES = 4
COPY_RETRY_S = 0.25


def _files_state(db: str) -> tuple:
    """(Größe, Änderungszeit, Inode) der Datenbank und ihres Write-Ahead-Protokolls; None für eine fehlende."""
    out = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(db + suffix)
            out.append((st.st_size, st.st_mtime_ns, st.st_ino))
        except OSError:
            out.append(None)
    return tuple(out)


def cookie_rows(db: str, sql: str, params: tuple = (), sleep=time.sleep) -> list | None:
    """Zeilen einer Abfrage auf der Cookie-Datenbank des Browsers, gelesen aus einer Kopie (der
    Browser kann die Datei gerade offen haben); None, wenn sie fehlt oder unlesbar ist. Gemeinsam
    für Steam, Battle.net und Rockstar.

    Schreibt der Browser während des Kopierens (beim Schließen überträgt SQLite das Protokoll in die Datei und
    löscht es), ergäbe die Kopie einen alten Stand: 2026-10-30 zeigte nach der Anmeldung bei Steam der
    Store „nicht angemeldet“. Ändert sich Datei oder Protokoll während des Kopierens, wird es nach kurzer Pause
    wiederholt (höchstens COPY_TRIES Mal)."""
    for attempt in range(COPY_TRIES):
        if not os.path.isfile(db):
            return None
        before = _files_state(db)
        tmp = tempfile.mkdtemp(prefix="couch-cookies-")
        try:
            try:
                for suffix in _COPIED:
                    if os.path.isfile(db + suffix):
                        shutil.copy2(db + suffix, os.path.join(tmp, _COPY + suffix))
            except OSError:                 # Datei verschwand beim Kopieren (Browser schloss gerade)
                if attempt + 1 < COPY_TRIES:
                    sleep(COPY_RETRY_S)
                    continue
                return None
            if _files_state(db) != before and attempt + 1 < COPY_TRIES:
                sleep(COPY_RETRY_S)         # während des Kopierens geschrieben: noch einmal
                continue
            con = sqlite3.connect(os.path.join(tmp, _COPY))
            try:
                return con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        finally:
            _drop_copy(tmp)
    return None


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
    return net.get_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json", "Cookie": net.cookie_header(cookies)},
                        timeout)


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
