"""itch.io-Konto (Bauplan E100): Anmeldung über OAuth (Authorization Code + PKCE), nie ein Kennwort.

Einrichtung (einmal): Der Nutzer legt im eigenen itch.io-Konto eine OAuth-App an (APPS_URL), Name
„PC5 Launcher“, Redirect-URL REDIRECT. Ihre öffentliche Kennung (client_id, 32 Hex-Zeichen, kein
Geheimnis) steht in der Einstellung store.itch_client_id. Der Browser liest sie von der Seite der App
(client_id_from_text) und legt sie in <state>/itch-client.json; das Home übernimmt sie (take_client_id).

Anmelden: Das Home erzeugt Verifier/Challenge (S256) und state (<state>/itch-pkce.json, 30 min) und
öffnet login_url() im Browser. Der Browser bricht die Navigation auf REDIRECT ab, legt {code, state, at}
in <state>/itch-login.json (store_code) und schließt sich. Das Home tauscht den Code (take_code) gegen
einen Zugangsschlüssel in <config>/itch-token.json (0600). Abmelden: revoke, Datei weg.
Ein 401 heißt abgemeldet (ItchLoginNeeded), der Schlüssel wird verworfen.

API (api.itch.io, Bearer-Schlüssel): /profile, /profile/owned-keys, /games/<id>/uploads,
/games/<id>/download-sessions, /uploads/<id>/download (Umleitung auf das CDN: Die Adresse wird hier
gelesen, nie verfolgt; der Schlüssel geht nie an das CDN), /search/games. Höflich: höchstens rund vier
Anfragen je Sekunde, bei 429/503 kurz warten und einmal wiederholen. Schlüssel nie ins Protokoll.
Unbestätigt (ohne echtes Konto nur mit Attrappe geprüft): Tausch, owned-keys, uploads, download, search.
Alles hier blockiert und gehört in einen Arbeits-Thread.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import log
from . import itch
from .cache import StoreError

_log = log.get("store.itch")

REDIRECT = "http://127.0.0.1:34711/itch-callback"
SCOPES = "profile:me profile:owned game:view:uploads"
APPS_URL = "https://itch.io/user/settings/oauth-apps"
AUTH_URL = "https://itch.io/user/oauth"
API = "https://api.itch.io"
TOKEN_URL = API + "/oauth/token"
REVOKE_URL = API + "/oauth/revoke"
USER_AGENT = itch.USER_AGENT
TIMEOUT_S = 20.0
MAX_BYTES = 16 * 1024 * 1024
CODE_TTL_S = 600                # der Code von itch.io gilt 10 min
PKCE_TTL_S = 1800              # Anmelden mit der Bildschirmtastatur dauert; der Code selbst gilt 10 min
CLIENT_TTL_S = 24 * 3600
OWNED_TTL_S = 120               # wie GOG (wiring_store.ACCOUNT_TTL_S)
PER_PAGE = 500
MAX_PAGES = 200
API_GAP_S = 0.25                # höchstens rund vier Anfragen je Sekunde
RETRY_S = 2.0                   # nach 429/503 so lange warten, dann einmal wiederholen

TOKEN_FILE = "itch-token.json"      # <config>
CODE_FILE = "itch-login.json"       # <state>, vom Browser
PKCE_FILE = "itch-pkce.json"        # <state>
CLIENT_FILE = "itch-client.json"    # <state>, vom Browser
OWNED_FILE = "itch-owned.json"      # <state>, letzter guter Stand von owned-keys

UNREACHABLE = "itch.io ist gerade nicht erreichbar."
BRAKES = "itch.io bremst gerade, versuch es gleich noch mal."
LOGIN_FIRST = "Melde dich zuerst bei itch.io an."
LOGIN_AGAIN = "Deine Anmeldung bei itch.io gilt nicht mehr. Melde dich neu an."
SETUP_FIRST = "Richte zuerst die itch.io-App ein (Einstellungen › Spiele › itch.io)."
# Fehlersätze der API, die eine ungültige Anmeldung meinen (sonst nur Status 401). Nur genau diese:
# „authentication required for this download“ o. ä. zu einer einzelnen Datei meldet nicht ab (E100)
AUTH_ERRORS = ("invalid key", "invalid token", "invalid api key")

_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")


class ItchError(StoreError):
    """Ein deutscher Satz für die Oberfläche. status: HTTP-Status (0 = ohne); transient: Netz weg,
    Störung oder Bremse, ein neuer Versuch später kann helfen (E99); browser_only: das Spiel läuft
    nur im Browser (itch_install.pick_upload)."""

    def __init__(self, message: str, status: int = 0, browser_only: bool = False, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.browser_only = browser_only
        self.transient = transient


class ItchLoginNeeded(ItchError):
    """Ohne (neue) Anmeldung geht es nicht: nie angemeldet, abgemeldet oder Schlüssel abgelehnt.
    Kein Netzfehler, ein neuer Versuch hilft nicht (E99)."""


# ------------------------------------------------------------------ Dateien

def _write_private(path: str, data: dict) -> None:
    """JSON atomar und nur für dich lesbar (0600). Der Zwischenname ist je Aufruf eindeutig: Browser
    (store_code) und Home (take_code legt den Code nach einem Netzfehler zurück) schreiben dieselbe
    Datei. O_EXCL/O_NOFOLLOW: nie eine liegengebliebene Datei mit fremden Rechten, nie einem Verweis
    folgen (wie wiring_store._write_private, E99)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%s.neu" % (path, secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _take(path: str) -> dict:
    """Lesen und gleich löschen (Codes gelten nur einmal)."""
    data = _read(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    return data


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _age(data: dict, now: float) -> float:
    try:
        return now - float(data.get("at") or 0)
    except (TypeError, ValueError):
        return float("inf")


# ------------------------------------------------------------------ Browser-Seite (reine Funktionen)

def is_login_redirect(uri) -> bool:
    """Ist das die Rückleitung der Anmeldung (http://127.0.0.1:34711/itch-callback…)?"""
    try:
        parts = urllib.parse.urlsplit(str(uri or ""))
        return (parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port == 34711
                and parts.path == "/itch-callback")
    except ValueError:
        return False


def code_from_redirect(uri):
    """(code, state) aus der Rückleitung; ("", state) bei error=… (abgelehnt); None, wenn es nicht
    die Rückleitung ist oder weder Code noch Fehler darin steht."""
    if not is_login_redirect(uri):
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(uri)).query)
    state = (query.get("state") or [""])[0]
    if query.get("error"):
        return "", state
    code = (query.get("code") or [""])[0]
    return (code, state) if code else None


def store_code(state_dir: str, code: str, state: str, clock=time.time) -> None:
    """Vom Browser gerufen: Code und state für das Home ablegen (0600)."""
    _write_private(os.path.join(state_dir, CODE_FILE), {"code": str(code or ""), "state": str(state or ""),
                                                         "at": clock()})


def is_apps_page(uri) -> bool:
    """Seite der OAuth-Apps im eigenen itch.io-Konto (dort steht die client_id)?"""
    try:
        parts = urllib.parse.urlsplit(str(uri or ""))
    except ValueError:
        return False
    path = parts.path.rstrip("/")
    return (parts.scheme == "https" and parts.hostname == "itch.io"
            and (path == "/user/settings/oauth-apps" or path.startswith("/user/settings/oauth-apps/")))


def valid_client_id(cid) -> bool:
    return isinstance(cid, str) and bool(re.fullmatch(r"[0-9a-f]{32}", cid))


def client_id_from_text(text) -> str:
    """Die client_id aus dem sichtbaren Text der App-Seite: genau eine 32-Hex-Kette, sonst ""."""
    found = {m.lower() for m in _HEX32.findall(str(text or ""))}
    return found.pop() if len(found) == 1 else ""


def store_client_id(state_dir: str, cid: str, clock=time.time) -> bool:
    """Vom Browser gerufen: die gelesene client_id für das Home ablegen (0600). False: ungültig."""
    cid = str(cid or "").strip().lower()
    if not valid_client_id(cid):
        return False
    _write_private(os.path.join(state_dir, CLIENT_FILE), {"client_id": cid, "at": clock()})
    return True


# ------------------------------------------------------------------ Netz

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None             # nie folgen: der Bearer-Schlüssel darf nicht an das CDN (E100)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, headers: dict | None = None, data: dict | None = None,
             timeout: float = TIMEOUT_S) -> tuple:
    """Eine Anfrage ohne Umleitungen: (status, kopfzeilen, inhalt). Wirft nur bei Netzfehlern."""
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:         # noqa: S310 (feste https-Adressen)
            return resp.status, dict(resp.headers.items()), resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers.items()) if e.headers else {}, e.read(MAX_BYTES)
        finally:
            e.close()


def _header(headers: dict, name: str) -> str:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value or "")
    return ""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def challenge_of(verifier: str) -> str:
    """PKCE S256 (RFC 7636)."""
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


_API_LOCK = threading.Lock()
_API_LAST = [0.0]


def _upload(u: dict) -> dict:
    """Ein Upload der API in fester Form (v1 mit traits, v2 mit platforms {windows: "amd64"})."""
    traits = u.get("traits") if isinstance(u.get("traits"), list) else []
    plats = u.get("platforms") if isinstance(u.get("platforms"), dict) else {}
    arch = {}
    for key in ("windows", "linux", "osx"):
        value = plats.get(key)
        arch[key] = str(value) if value else ("all" if "p_" + key in traits else "")
    build = u.get("build") if isinstance(u.get("build"), dict) else {}

    def num(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    return {"id": num(u.get("id")), "filename": str(u.get("filename") or ""),
            "display_name": str(u.get("display_name") or ""), "size": num(u.get("size")),
            "type": str(u.get("type") or "default"), "storage": str(u.get("storage") or ""),
            "host": str(u.get("host") or ""), "demo": bool(u.get("demo")) or "demo" in traits,
            "preorder": bool(u.get("preorder")) or "preorder" in traits,
            "platforms": {k: bool(v) for k, v in arch.items()}, "arch": arch,
            "channel_name": str(u.get("channel_name") or ""), "build_id": num(u.get("build_id") or build.get("id")),
            "version": str(build.get("user_version") or build.get("version") or ""),
            "created_at": str(u.get("created_at") or ""), "updated_at": str(u.get("updated_at") or "")}


def owned_key(k) -> dict | None:
    """Ein Schlüssel aus owned-keys in fester Form; None ohne Spiel auf <benutzer>.itch.io."""
    if not isinstance(k, dict) or not isinstance(k.get("game"), dict):
        return None
    game = k["game"]
    gid = itch.id_from_url(game.get("url"))
    try:
        key_id, game_id = int(k.get("id") or 0), int(game.get("id") or k.get("game_id") or 0)
        min_price = int(game.get("min_price") or 0)
    except (TypeError, ValueError):
        return None
    if not gid or not key_id or not game_id:
        return None
    return {"key_id": key_id, "game_id": game_id, "id": gid, "title": str(game.get("title") or ""),
            "cover": itch.cover_of(game.get("cover_url"), game.get("still_cover_url")), "min_price": min_price,
            "platforms": itch.platforms_of(game), "classification": str(game.get("classification") or "game"),
            "type": str(game.get("type") or "default"), "created_at": str(k.get("created_at") or ""),
            "item": itch.item_from_game(game)}


class ItchAccount:
    """Konto bei itch.io. get_client/set_client lesen und schreiben die Einstellung
    store.itch_client_id (ohne sie gilt ein Wert nur im Speicher)."""

    def __init__(self, config_dir: str, state_dir: str, request=None, clock=time.time, *,
                 get_client=None, set_client=None, sleep=time.sleep, monotonic=time.monotonic):
        self.config_dir = config_dir
        self.state_dir = state_dir
        self._request = request or _request
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._memory = ""
        self._get_client = get_client or (lambda: self._memory)
        self._set_client = set_client or (lambda cid: setattr(self, "_memory", cid))
        self._lock = threading.RLock()
        self._owned = None
        self._owned_at = 0.0
        self._search_denied = False

    # ------------------------------------------------------------ Pfade
    @property
    def token_file(self) -> str:
        return os.path.join(self.config_dir, TOKEN_FILE)

    def _state(self, name: str) -> str:
        return os.path.join(self.state_dir, name)

    # ------------------------------------------------------------ App-Kennung
    def client_id(self) -> str:
        try:
            cid = str(self._get_client() or "").strip().lower()
        except Exception:       # die Einstellung ist nicht lesbar: dann eben nicht eingerichtet
            return ""
        return cid if valid_client_id(cid) else ""

    def configured(self) -> bool:
        return bool(self.client_id())

    def set_client_id(self, cid) -> bool:
        """Kennung übernehmen (geprüft: 32 Hex-Zeichen). "" löscht sie. False: ungültig."""
        cid = str(cid or "").strip().lower()
        if cid and not valid_client_id(cid):
            return False
        self._set_client(cid)
        return True

    def take_client_id(self) -> bool:
        """Liegt eine vom Browser gelesene Kennung bereit, in die Einstellung übernehmen."""
        data = _take(self._state(CLIENT_FILE))
        cid = str(data.get("client_id") or "").lower()
        if not valid_client_id(cid) or _age(data, self._clock()) > CLIENT_TTL_S:
            return False
        return self.set_client_id(cid)

    # ------------------------------------------------------------ Anmelden
    def login_url(self) -> str:
        """Adresse der Anmeldung (merkt Verifier und state für take_code). ItchError ohne Kennung."""
        cid = self.client_id()
        if not cid:
            raise ItchError(SETUP_FIRST)
        verifier, state = _b64(os.urandom(32)), _b64(os.urandom(16))
        _write_private(self._state(PKCE_FILE), {"verifier": verifier, "state": state, "client_id": cid,
                                                 "at": self._clock()})
        query = urllib.parse.urlencode(
            {"client_id": cid, "scope": SCOPES, "response_type": "code", "redirect_uri": REDIRECT, "state": state,
             "code_challenge": challenge_of(verifier), "code_challenge_method": "S256"},
            quote_via=urllib.parse.quote, safe=":")
        return AUTH_URL + "?" + query

    def take_code(self) -> bool:
        """Liegt ein frischer Code vom Browser bereit, gegen einen Schlüssel tauschen. True: neu
        angemeldet. ItchLoginNeeded, wenn itch.io den Code ablehnt; ItchError ohne Netz (der Code
        bleibt dann für den nächsten Versuch liegen, solange er gilt)."""
        with self._lock:
            path = self._state(CODE_FILE)
            data = _take(path)
            if not data:
                return False
            now = self._clock()
            pkce = _read(self._state(PKCE_FILE))
            code, state = str(data.get("code") or ""), str(data.get("state") or "")
            # Als Bytes vergleichen: compare_digest wirft bei Nicht-ASCII-Text (jede Seite kann die
            # Rückleitung mit state=%C3%A4 aufrufen)
            if (not pkce or _age(data, now) >= CODE_TTL_S or _age(pkce, now) >= PKCE_TTL_S
                    or not hmac.compare_digest(state.encode("utf-8"),
                                               str(pkce.get("state") or "").encode("utf-8"))):
                return False                    # alt, fremd oder ohne Gegenstück: nie tauschen
            if not code:                        # abgelehnt (error=access_denied)
                _remove(self._state(PKCE_FILE))
                return False
            try:
                answer = self._token_call(grant_type="authorization_code", code=code,
                                          code_verifier=str(pkce.get("verifier") or ""), redirect_uri=REDIRECT,
                                          client_id=str(pkce.get("client_id") or ""))
            except ItchLoginNeeded:
                _remove(self._state(PKCE_FILE))
                raise
            except ItchError:
                try:
                    _write_private(path, data)  # Netz weg: später noch einmal (der Code gilt 10 min)
                except OSError as e:
                    _log.warning("itch.io: Anmelde-Code nicht zurückgelegt (%s)", type(e).__name__)
                raise
            _remove(self._state(PKCE_FILE))
            self._save(answer, str(pkce.get("client_id") or ""))
            self._forget_owned()
            self._search_denied = False
        try:
            self.profile(force=True)
        except (ItchError, OSError):
            pass                                # Name kommt beim nächsten Mal
        return True

    def _token_call(self, **form) -> dict:
        status, _headers, body = self._send("POST", TOKEN_URL, {"User-Agent": USER_AGENT,
                                                                "Accept": "application/json"}, form)
        answer = self._json(body)
        if status == 200 and isinstance(answer, dict) and answer.get("access_token"):
            return answer
        if status in (400, 401):
            raise ItchLoginNeeded("itch.io hat die Anmeldung nicht bestätigt. Melde dich neu an.", status)
        raise ItchError(BRAKES if status == 429 else UNREACHABLE, status, transient=True)

    def _save(self, answer: dict, client_id: str, keep_user: bool = False) -> None:
        data = {"access_token": str(answer["access_token"]), "client_id": client_id,
                "scope": str(answer.get("scope") or SCOPES), "at": self._clock()}
        old = self._tokens() if keep_user else {}
        for key in ("name", "user"):                # beim Erneuern bleibt das Konto dasselbe
            if old.get(key):
                data[key] = old[key]
        # Laut Doku läuft der Schlüssel nie ab; go-itchio kennt aber auch refresh_token/expires_in
        if answer.get("refresh_token"):
            data["refresh_token"] = str(answer["refresh_token"])
        try:
            expires = float(answer.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires = 0.0
        if expires > 0:
            data["expires_at"] = self._clock() + expires - 60
        _write_private(self.token_file, data)

    def _tokens(self) -> dict:
        return _read(self.token_file)

    def _refresh(self, data: dict) -> str:
        answer = self._token_call(grant_type="refresh_token", refresh_token=str(data.get("refresh_token") or ""),
                                  client_id=str(data.get("client_id") or self.client_id()))
        self._save(answer, str(data.get("client_id") or ""), keep_user=True)
        return str(answer["access_token"])

    def token(self) -> str:
        """Gültiger Zugangsschlüssel oder "" (nicht angemeldet bzw. gerade nicht erneuerbar)."""
        with self._lock:
            data = self._tokens()
            tok = str(data.get("access_token") or "")
            if not tok:
                return ""
            try:
                expired = bool(data.get("expires_at")) and self._clock() >= float(data["expires_at"])
            except (TypeError, ValueError):
                expired = False
            if not expired:
                return tok
            if not data.get("refresh_token"):
                self._drop()
                return ""
            try:
                return self._refresh(data)
            except ItchLoginNeeded:
                self._drop()                    # E99: endgültig abgelehnt, also abgemeldet
                return ""
            except ItchError:
                return ""

    def logged_in(self) -> bool:
        return bool(self._tokens().get("access_token"))

    def name(self) -> str:
        """Anzeigename des Kontos ("" unbekannt)."""
        return str(self._tokens().get("name") or "")

    def _drop(self) -> None:
        """Schlüssel verwerfen, ohne itch.io zu fragen (abgelehnt)."""
        _remove(self.token_file)
        _remove(self._state(OWNED_FILE))
        self._forget_owned()

    def _forget_owned(self) -> None:
        self._owned, self._owned_at = None, 0.0

    def logout(self) -> None:
        """Abmelden: Schlüssel bei itch.io widerrufen (ohne Netz egal), Dateien und Stände weg."""
        data = self._tokens()
        tok = str(data.get("access_token") or "")
        if tok:
            try:
                self._send("POST", REVOKE_URL, {"User-Agent": USER_AGENT, "Accept": "application/json"},
                           {"token": tok, "client_id": str(data.get("client_id") or self.client_id())})
            except ItchError:
                pass
        with self._lock:
            self._drop()
            _remove(self._state(PKCE_FILE))
            _remove(self._state(CODE_FILE))
            self._search_denied = False

    # ------------------------------------------------------------ API
    def _pause(self) -> None:
        with _API_LOCK:
            wait = API_GAP_S - (self._monotonic() - _API_LAST[0])
            if wait > 0:
                self._sleep(wait)
            _API_LAST[0] = self._monotonic()

    def _send(self, method: str, url: str, headers: dict, data: dict | None = None) -> tuple:
        """Eine Anfrage, bei 429/503 einmal wiederholt. ItchError bei Netzfehlern (ohne Einzelheiten:
        die Adresse oder der Schlüssel gehören in keinen Satz)."""
        attempt = 0
        while True:
            self._pause()
            try:
                status, out_headers, body = self._request(method, url, headers, data)
            except Exception:           # urllib wirft je nach Lage sehr verschiedene Ausnahmen
                raise ItchError(UNREACHABLE, transient=True) from None      # ohne Kette: keine Adresse im Protokoll
            if status in (429, 503) and attempt == 0:
                attempt += 1
                self._sleep(RETRY_S)
                continue
            return int(status), out_headers or {}, body or b""

    @staticmethod
    def _json(body):
        try:
            return json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body or "null")
        except (ValueError, UnicodeDecodeError):
            return None

    def _raw(self, method: str, path: str, params: dict | None = None) -> tuple:
        """Anfrage mit Schlüssel: (status, kopfzeilen, inhalt). ItchLoginNeeded ohne Anmeldung."""
        tok = self.token()
        if not tok:
            raise ItchLoginNeeded(LOGIN_FIRST)
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self._send(method, url, {"User-Agent": USER_AGENT, "Accept": "application/vnd.itch.v2",
                                        "Authorization": "Bearer " + tok})

    def _call(self, method: str, path: str, params: dict | None = None, *, denied_none: bool = False):
        """JSON-Antwort der API (dict). Fehler als ItchError mit Satz; 401 meldet ab (ItchLoginNeeded).
        denied_none: 401/403 heißt „nicht erlaubt“ und ergibt None, ohne abzumelden (Suche)."""
        status, _headers, body = self._raw(method, path, params)
        data = self._json(body)
        errors = data.get("errors") if isinstance(data, dict) else None
        auth_failed = status == 401 or (isinstance(errors, list) and any(
            str(m).strip().lower() in AUTH_ERRORS for m in errors))
        if denied_none and (auth_failed or status == 403):
            return None
        if auth_failed:
            with self._lock:
                self._drop()
            raise ItchLoginNeeded(LOGIN_AGAIN, 401)
        if status == 403:
            raise ItchError("itch.io erlaubt das deinem Konto nicht.", 403)
        if status == 404:
            raise ItchError("Das gibt es bei itch.io nicht (mehr).", 404)
        if status == 429:
            raise ItchError(BRAKES, 429, transient=True)
        if status in (400, 422) or (status == 200 and errors):
            raise ItchError("itch.io hat die Anfrage abgelehnt.", status)
        if status != 200 or not isinstance(data, dict):
            raise ItchError(UNREACHABLE, status, transient=True)
        return data

    def profile(self, force: bool = False) -> dict:
        """{id, username, display_name, url}; merkt den Anzeigenamen in der Schlüssel-Datei."""
        data = self._tokens()
        if not force and data.get("name") and data.get("user"):
            return dict(data["user"]) if isinstance(data.get("user"), dict) else {}
        answer = self._call("GET", "/profile")
        user = answer.get("user") if isinstance(answer.get("user"), dict) else {}
        out = {"id": user.get("id"), "username": str(user.get("username") or ""),
               "display_name": str(user.get("display_name") or ""), "url": str(user.get("url") or "")}
        with self._lock:
            data = self._tokens()
            if data.get("access_token"):
                data.update(name=out["display_name"] or out["username"], user=out)
                try:
                    _write_private(self.token_file, data)
                except OSError as e:            # Platte voll o. ä.: der Name kommt beim nächsten Mal
                    _log.warning("itch.io: Kontoname nicht gespeichert (%s)", type(e).__name__)
        return out

    def owned(self, force: bool = False) -> list[dict]:
        """Eigene Spiele aus owned-keys: alle Seiten bis zur LEEREN Seite (Seiten können kürzer sein,
        obwohl weitere folgen). Je Spiel ein Schlüssel (der neueste). [] ohne Anmeldung; ItchError,
        wenn itch.io nicht antwortet (E99: ein Netzfehler heißt nie „gehört dir nicht“; last_owned()
        hat den letzten guten Stand)."""
        if not self.logged_in():
            return []
        now = self._clock()
        if not force and self._owned is not None and now - self._owned_at < OWNED_TTL_S:
            return list(self._owned)
        keys, seen, key_ids = [], set(), set()
        for page in range(1, MAX_PAGES + 1):
            data = self._call("GET", "/profile/owned-keys", {"page": page, "per_page": PER_PAGE})
            rows = data.get("owned_keys")
            if rows == {}:
                rows = []                       # Konto ohne Spiele: itch.io schickt {} statt [] (gesehen 2026-10-24)
            if not isinstance(rows, list):
                raise ItchError(UNREACHABLE, transient=True)
            if not rows:
                break
            # Übergeht itch.io page und liefert immer dieselbe Seite, wären es sonst MAX_PAGES Anfragen
            # je Aufruf (die Kaufbeobachtung fragt alle 10 s): Schluss, sobald nichts Neues kommt
            page_ids = {str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")}
            if not page_ids - key_ids:
                break
            key_ids |= page_ids
            for row in rows:
                key = owned_key(row)
                if key and key["game_id"] not in seen:      # neueste zuerst: der erste Schlüssel gilt
                    seen.add(key["game_id"])
                    keys.append(key)
        with self._lock:
            if self.logged_in():
                self._owned, self._owned_at = keys, now
                try:
                    _write_private(self._state(OWNED_FILE), {"at": now, "keys": keys})
                except OSError as e:            # Platte voll o. ä.: der Stand im Speicher gilt
                    _log.warning("itch.io: eigene Spiele nicht auf der Platte gemerkt (%s)", type(e).__name__)
        return list(keys)

    def last_owned(self) -> list[dict]:
        """Letzter guter Stand von owned() (Speicher, sonst Platte); [] ohne Anmeldung."""
        if not self.logged_in():
            return []
        if self._owned is not None:
            return list(self._owned)
        keys = _read(self._state(OWNED_FILE)).get("keys")
        return [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []

    def key_for(self, game_id: int) -> dict | None:
        """Schlüssel eines Spiels aus dem letzten Stand (ohne Netz)."""
        return next((k for k in self.last_owned() if k.get("game_id") == game_id), None)

    def uploads(self, game_id: int, key_id: int | None = None) -> list[dict]:
        """Dateien eines Spiels (bezahlte Spiele brauchen den Download-Schlüssel)."""
        params = {"download_key_id": int(key_id)} if key_id else None
        data = self._call("GET", "/games/%d/uploads" % int(game_id), params)
        rows = data.get("uploads")
        if not isinstance(rows, list):
            raise ItchError(UNREACHABLE, transient=True)
        return [_upload(u) for u in rows if isinstance(u, dict) and u.get("id")]

    def download_session(self, game_id: int, key_id: int | None = None) -> str:
        """uuid einer Download-Sitzung ("" wenn itch.io keine gibt; sie ist nur eine Zählhilfe)."""
        params = {"download_key_id": int(key_id)} if key_id else None
        try:
            data = self._call("POST", "/games/%d/download-sessions" % int(game_id), params)
        except ItchLoginNeeded:
            raise
        except ItchError:
            return ""
        uuid = str(data.get("uuid") or "")
        return uuid if re.fullmatch(r"[0-9a-fA-F-]{8,64}", uuid) else ""

    def download_url(self, upload_id: int, key_id: int | None = None, uuid: str = "") -> str:
        """Signierte Adresse der Datei. Die Umleitung der API wird gelesen, nicht verfolgt: der
        Schlüssel geht nie an das CDN. Je Versuch neu holen (die Adresse läuft ab)."""
        params = {}
        if key_id:
            params["download_key_id"] = int(key_id)
        if uuid:
            params["uuid"] = str(uuid)
        status, headers, body = self._raw("GET", "/uploads/%d/download" % int(upload_id), params or None)
        if status in (301, 302, 303, 307, 308):
            target = _header(headers, "Location")
            if target.startswith("https://"):
                return target
            raise ItchError("itch.io hat keine sichere Download-Adresse geliefert.", status)
        if status == 200:
            data = self._json(body)
            target = str(data.get("url") or "") if isinstance(data, dict) else ""
            if target.startswith("https://"):
                return target
        if status == 401:
            with self._lock:
                self._drop()
            raise ItchLoginNeeded(LOGIN_AGAIN, 401)
        if status == 403:
            raise ItchError("itch.io erlaubt dir diese Datei nicht.", 403)
        if status == 404:
            raise ItchError("Diese Datei gibt es bei itch.io nicht mehr.", 404)
        raise ItchError(BRAKES if status == 429 else UNREACHABLE, status, transient=True)

    def search_games(self, query: str) -> list:
        """Rohe Treffer der API-Suche für die Bilder der Bibliothek (artsources.itch): [{title, url,
        cover_url, still_cover_url …}]; [] ohne Anmeldung oder wenn die Suche nicht erlaubt ist."""
        text = re.sub(r"\s+", " ", str(query or "")).strip()[:100]
        if not text or self._search_denied or not self.logged_in():
            return []
        data = self._call("GET", "/search/games", {"query": text}, denied_none=True)
        if data is None:
            self._search_denied = True
            return []
        games = data.get("games") if isinstance(data.get("games"), list) else []
        return [g for g in games if isinstance(g, dict) and str(g.get("classification") or "game") == "game"]

    def can_search(self) -> bool:
        return self.logged_in() and not self._search_denied

    def search(self, query: str, page: int = 0) -> dict | None:
        """Suche über die API: {items: [karte], entries: [eintrag für ItchStore.remember], total, more};
        page zählt ab 0 wie store.list.
        None: Die API-Suche ist für diesen Schlüssel nicht erlaubt (401/403; unbestätigt, ob die
        Scopes sie abdecken) – dann sucht der Browser. Meldet dabei nicht ab."""
        text = re.sub(r"\s+", " ", str(query or "")).strip()[:100]
        if self._search_denied:
            return None
        if not text:
            return {"items": [], "total": 0, "more": False}
        page = max(0, int(page))
        data = self._call("GET", "/search/games", {"query": text, "page": page + 1}, denied_none=True)
        if data is None:
            self._search_denied = True
            return None
        games = data.get("games") if isinstance(data.get("games"), list) else []
        items = []
        for g in games:
            if isinstance(g, dict) and str(g.get("classification") or "game") == "game":
                item = itch.item_from_game(g)
                if item:
                    items.append(item)
        try:
            per_page = int(data.get("per_page") or 0)
        except (TypeError, ValueError):
            per_page = 0
        more = bool(games) and len(games) >= (per_page or len(games) + 1)
        return {"items": [itch.ItchStore.card(i) for i in items], "entries": items,
                "total": page * max(per_page, len(games)) + len(items) + (1 if more else 0), "more": more}
