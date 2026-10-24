"""Ubisoft-Konto: Anmeldung über Ubisofts Anmeldeseite im Browser des PC5 Launchers.

Ablauf wie bei Lutris und dem GOG-Galaxy-Plugin: Der Browser öffnet connect.ubisoft.com/login mit der
appId/genomeId der Shop-Webseite. Nach der Anmeldung (Kennwort, 2FA, Captcha erledigt Ubisofts Seite)
landet er auf LOGIN_DONE; dort liest READ_JS die Sitzung aus dem localStorage der Seite
(PRODloginData: ticket, sessionId, userId …; PRODrememberMe: rememberMeTicket, ~29 Tage) und gibt sie
an take_login(). Gespeichert wird nur, was couch braucht (token_file, 0600). Ein Kennwort sieht couch nie.

    PUT  public-ubiservices.ubi.com/v3/profiles/sessions   Ticket verlängern (Authorization: Ubi_v1 t=…)
    POST public-ubiservices.ubi.com/v3/profiles/sessions   neues Ticket über rememberMeTicket (rm_v1 t=…)
    POST public-ubiservices.ubi.com/v1/profiles/me/uplay/graphql   eigene Spiele (viewer.ownedGames)
Ein Ticket gilt 3 Stunden (Anmeldeseite: ticketExpirationTime 10800000 ms).
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Kennungen der Shop-Webseite (store.ubisoft.com, wa_data; dieselben wie Lutris seit 2026-02).
# Ubisoft tauscht sie alle paar Monate; UbisoftStore.web_ids() liest die aktuellen von der Shop-Seite.
APP_ID = "82b650c0-6cb3-40c0-9f41-25a53b62b206"
GENOME_ID = "42d07c95-9914-4450-8b38-267c4e462b21"
LOGIN_DONE = "https://connect.ubisoft.com/ready"
SESSIONS = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
GRAPHQL = "https://public-ubiservices.ubi.com/v1/profiles/me/uplay/graphql"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TIMEOUT_S = 20.0
MARGIN_S = 10 * 60             # so früh vor Ablauf verlängern
TICKET_S = 3 * 3600
REMEMBER_S = 29 * 24 * 3600
# Liefert einen JSON-Text (oder "null"-Werte, wenn die Seite nichts abgelegt hat)
READ_JS = ('JSON.stringify({PRODloginData: localStorage.getItem("PRODloginData"), '
           'PRODrememberMe: localStorage.getItem("PRODrememberMe")})')
OWNED_QUERY = (
    "query AllGames { viewer { id ...ownedGamesList } }"
    " fragment gameProps on Game { id spaceId name }"
    " fragment ownedGameProps on Game { ...gameProps viewer { meta { id ownedPlatformGroups { id name type } } } }"
    " fragment ownedGamesList on User { ownedGames: games(filterBy: {isOwned: true}) { totalCount nodes { ...ownedGameProps } } }")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def login_url(app_id: str = APP_ID, genome_id: str = GENOME_ID) -> str:
    return "https://connect.ubisoft.com/login?" + urllib.parse.urlencode(
        {"appId": app_id, "genomeId": genome_id, "lang": "de-DE", "nextUrl": LOGIN_DONE})


LOGIN_URL = login_url()


def is_login_done(uri: str) -> bool:
    return str(uri or "").startswith(LOGIN_DONE)


def _send(method: str, url: str, headers: dict, body: dict | None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:     # noqa: S310 (feste https-Adressen)
        return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))


def _post(url: str, headers: dict, body: dict | None = None):
    return _send("POST", url, headers, body)


def _put(url: str, headers: dict, body: dict | None = None):
    return _send("PUT", url, headers, body)


def _write_private(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".neu"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.chmod(tmp, 0o600)        # falls die Datei schon da war (O_CREAT setzt Rechte nur beim Anlegen)
    os.replace(tmp, path)


def _iso(text) -> float | None:
    """„2026-10-22T14:58:49.0123456Z“ -> Sekunden (Ubisoft schreibt bis zu sieben Nachkommastellen)."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)", str(text or ""))
    return float(calendar.timegm(tuple(int(x) for x in m.groups()))) if m else None


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _auth_dead(error: Exception) -> bool:
    """Ubisoft hat die Anmeldung abgelehnt (nicht: kein Netz). Schließt die Antwort des Fehlers."""
    if isinstance(error, urllib.error.HTTPError):
        try:
            error.close()
        except Exception:
            pass
        return error.code in (400, 401, 403)
    return False


class UbisoftError(Exception):
    """Ubisoft antwortet nicht (kein Netz, Störung): nicht dasselbe wie „besitzt nichts“ (E99)."""


class UbisoftAccount:
    def __init__(self, token_file: str, post=_post, put=_put, clock=time.time, app_id: str = APP_ID,
                 genome_id: str = GENOME_ID, resolve=None):
        """resolve(space_ids) -> {spaceId: Ubisoft-Connect-ID}, z. B. UbisoftStore.uplay_ids;
        ohne resolve heißt uplay_id in owned() 0."""
        self.token_file = token_file
        self._post = post
        self._put = put
        self._clock = clock
        self.app_id = app_id
        self.genome_id = genome_id
        self._resolve = resolve

    # ------------------------------------------------------------------ Datei
    def _load(self) -> dict:
        try:
            with open(self.token_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _headers(self, auth: str = "", session: str = "") -> dict:
        out = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT,
               "Ubi-AppId": self.app_id, "Ubi-RequestedPlatformType": "uplay", "GenomeId": self.genome_id,
               "Origin": "https://connect.ubisoft.com", "Referer": "https://connect.ubisoft.com/"}
        if auth:
            out["Authorization"] = auth
        if session:
            out["Ubi-SessionId"] = session
        return out

    def _store(self, session: dict, previous: dict | None = None, remember_until: float | None = None) -> dict | None:
        """Antwort der Sitzungs-Schnittstelle (oder PRODloginData) prüfen und ablegen."""
        previous = previous or {}
        ticket, sid = str(session.get("ticket") or ""), str(session.get("sessionId") or "")
        user = str(session.get("userId") or session.get("profileId") or previous.get("user_id") or "")
        if len(ticket) < 20 or not _UUID.fullmatch(sid) or not _UUID.fullmatch(user):
            return None
        now = self._clock()
        exp, server = _iso(session.get("expiration")), _iso(session.get("serverTime"))
        if str(session.get("expirationDate") or "").isdigit():
            hard = int(session["expirationDate"]) / 1000.0     # von der Anmeldeseite (ms, unsere Uhr)
        elif exp is not None and server is not None and exp > server:
            hard = now + (exp - server)                 # Laufzeit nach der Uhr des Servers
        else:
            hard = now + TICKET_S
        own = str(session.get("rememberMeTicket") or "")
        remember = own or str(previous.get("remember_me") or "")
        if remember_until is not None:
            until = remember_until
        elif own and own != previous.get("remember_me"):
            until = now + REMEMBER_S                    # neues rememberMeTicket: läuft neu
        else:
            until = float(previous.get("remember_until") or 0)
        data = {"ticket": ticket, "session_id": sid, "user_id": user,
                "hard_expiry": hard, "expires_at": hard - MARGIN_S,
                "remember_me": remember, "remember_until": until if remember else 0.0}
        _write_private(self.token_file, data)
        return data

    # ------------------------------------------------------------------ Anmeldung
    def take_login(self, raw_json: str) -> bool:
        """Ergebnis von READ_JS aus dem Browser übernehmen. True: angemeldet."""
        try:
            outer = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        except ValueError:
            return False
        if not isinstance(outer, dict):
            return False
        login, remember = _maybe_json(outer.get("PRODloginData")), _maybe_json(outer.get("PRODrememberMe"))
        if not isinstance(login, dict):
            return False
        session = dict(login)
        until = None
        if isinstance(remember, dict) and remember.get("rememberMeTicket"):
            session["rememberMeTicket"] = remember["rememberMeTicket"]
            if str(remember.get("expirationDate") or "").isdigit():
                until = int(remember["expirationDate"]) / 1000.0
        return self._store(session, remember_until=until) is not None

    def logged_in(self) -> bool:
        data = self._load()
        if not data.get("ticket"):
            return False
        now = self._clock()
        return now < float(data.get("hard_expiry") or 0) or \
            (bool(data.get("remember_me")) and now < float(data.get("remember_until") or 0))

    def ticket(self) -> str | None:
        """Gültiges Ticket oder None. Verlängert bei Bedarf (PUT), sonst neu über rememberMeTicket."""
        data = self._load()
        if not data.get("ticket"):
            return None
        now = self._clock()
        if now < float(data.get("expires_at") or 0):
            return data["ticket"]
        if now < float(data.get("hard_expiry") or 0):
            try:
                answer = self._put(SESSIONS, self._headers("Ubi_v1 t=" + data["ticket"], data.get("session_id", "")),
                                   {"rememberMe": False})
                fresh = self._store(answer if isinstance(answer, dict) else {}, data)
                if fresh:
                    return fresh["ticket"]
            except Exception as e:  # abgelaufen oder kein Netz: weiter mit rememberMe
                _auth_dead(e)
        if data.get("remember_me") and now < float(data.get("remember_until") or 0):
            try:
                answer = self._post(SESSIONS, self._headers("rm_v1 t=" + data["remember_me"]), {"rememberMe": True})
            except Exception as e:
                if _auth_dead(e):
                    self.logout()
                return None
            fresh = self._store(answer if isinstance(answer, dict) else {}, data)
            return fresh["ticket"] if fresh else None
        if now >= float(data.get("hard_expiry") or 0):
            self.logout()           # nichts mehr, womit man ein Ticket bekäme
        return None

    # ------------------------------------------------------------------ eigene Spiele
    def owned(self) -> list[dict]:
        """[{"uplay_id", "space_id", "name"}] der PC-Spiele im Konto, nach Name. [] ohne Anmeldung;
        UbisoftError, wenn die Liste wegen Netz oder Störung nicht kam (der alte Stand soll dann gelten)."""
        body = {"operationName": "AllGames", "variables": {"owned": True}, "query": OWNED_QUERY}
        answer = None
        for attempt in (1, 2):
            ticket = self.ticket()
            if not ticket:
                # E99: Abgelaufenes Ticket und kein Netz beim Erneuern heißt nicht „besitzt nichts“.
                # Eine echte Ablehnung meldet ticket() schon ab, dann ist logged_in() False.
                if self.logged_in():
                    raise UbisoftError("Ubisoft ist gerade nicht erreichbar.")
                return []
            data = self._load()
            try:
                answer = self._post(GRAPHQL, self._headers("Ubi_v1 t=" + ticket, data.get("session_id", "")), body)
                break
            except Exception as e:
                dead = _auth_dead(e)
                if not dead:
                    raise UbisoftError("Ubisoft ist gerade nicht erreichbar.") from e
                if attempt == 2:
                    return []
                # Ticket vor der Zeit abgelehnt: einmal neu holen (PUT bzw. rememberMe)
                _write_private(self.token_file, {**data, "expires_at": 0.0, "hard_expiry": 0.0})
        viewer = ((answer or {}).get("data") or {}).get("viewer") if isinstance(answer, dict) else None
        nodes = (((viewer or {}).get("ownedGames") or {}).get("nodes") or []) if isinstance(viewer, dict) else []
        games = []
        for n in nodes:
            if not isinstance(n, dict) or not n.get("name"):
                continue
            groups = (((n.get("viewer") or {}).get("meta") or {}).get("ownedPlatformGroups") or [])
            types = {str(g.get("type") or "").upper() for g in groups if isinstance(g, dict)}
            if types and "PC" not in types:
                continue        # nur auf Konsolen gekauft
            games.append({"space_id": str(n.get("spaceId") or ""), "name": str(n["name"]).strip(),
                          "_id": str(n.get("id") or "")})
        ids = {}
        if self._resolve is not None:
            try:
                ids = self._resolve([g["space_id"] for g in games if g["space_id"]]) or {}
            except Exception:
                ids = {}
        out = []
        for g in games:
            uplay = ids.get(g["space_id"]) or (int(g["_id"]) if g["_id"].isdigit() else 0)
            out.append({"uplay_id": int(uplay), "space_id": g["space_id"], "name": g["name"]})
        return sorted(out, key=lambda g: g["name"].casefold())

    def logout(self) -> None:
        try:
            os.unlink(self.token_file)
        except OSError:
            pass
