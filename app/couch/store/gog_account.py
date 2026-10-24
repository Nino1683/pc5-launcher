"""GOG-Konto (Bauplan E68, Teil 2): Anmeldung über GOGs Anmeldeseite im Browser des PC5 Launchers.

Ablauf wie bei anderen freien GOG-Programmen (Heroic, lgogdownloader, minigalaxy): Der Browser
öffnet auth.gog.com mit der öffentlichen Kennung des GOG-Galaxy-Clients. Nach der Anmeldung leitet
GOG auf embed.gog.com/on_login_success?…&code=… um; der Browser fängt diese Adresse ab und legt den
Code in <state>/gog-login.json (nur für das Home, 0600). Das Home tauscht ihn gegen Schlüssel
(<config>/gog-token.json, 0600) und erneuert sie selbst. Ein Kennwort sieht couch nie.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT_ID = "46899977096215655"
CLIENT_SECRET = "9d85c43b1482497dbbce61f6e4aa173a433796eeae2ca8c5f6129f2dc4de46d9"
REDIRECT = "https://embed.gog.com/on_login_success?origin=client"
LOGIN_URL = ("https://auth.gog.com/auth?client_id=%s&redirect_uri=%s&response_type=code&layout=client2"
             % (CLIENT_ID, urllib.parse.quote(REDIRECT, safe="")))
TOKEN = "https://auth.gog.com/token"
OWNED = "https://embed.gog.com/user/data/games"
USER_AGENT = "PC5-Launcher (GOG)"
CODE_TTL_S = 600


class GogError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


class GogLoginNeeded(GogError):
    """Ohne (neue) Anmeldung geht es nicht: nie angemeldet, oder GOG lehnt den Schlüssel endgültig ab
    (abgemeldet, widerrufen). Kein Netzfehler, ein neuer Versuch hilft nicht (E99)."""


def is_login_redirect(uri: str) -> bool:
    return str(uri or "").startswith("https://embed.gog.com/on_login_success")


def code_from_redirect(uri: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
    return (query.get("code") or [""])[0]


def _write_private(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".neu"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def store_code(path: str, code: str, clock=time.time) -> None:
    """Vom Browser gerufen: den Code für das Home ablegen."""
    _write_private(path, {"code": code, "at": clock()})


def _request(url: str, token: str = "", timeout: float = 20.0) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310 (feste https-Adressen)
        return json.loads(resp.read(16 * 1024 * 1024).decode("utf-8"))


class GogAccount:
    def __init__(self, token_file: str, code_file: str, request=_request, clock=time.time):
        self.token_file = token_file
        self.code_file = code_file
        self._request = request
        self._clock = clock

    def _tokens(self) -> dict:
        try:
            with open(self.token_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, answer: dict) -> dict:
        if not answer.get("access_token") or not answer.get("refresh_token"):
            raise GogError("GOG hat die Anmeldung nicht bestätigt.")
        data = {"access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
                "expires_at": self._clock() + float(answer.get("expires_in") or 3600) - 60,
                "user_id": str(answer.get("user_id") or "")}
        _write_private(self.token_file, data)
        return data

    def _token_call(self, **params) -> dict:
        query = urllib.parse.urlencode({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **params})
        try:
            return self._request(TOKEN + "?" + query)
        except urllib.error.HTTPError as e:
            e.close()
            # OAuth: 400 invalid_grant bzw. 401 heißt „Schlüssel ungültig“, alles andere eher Störung
            # (Wortlaut bei GOG unbestätigt)
            if e.code in (400, 401):
                raise GogLoginNeeded("Deine Anmeldung bei GOG ist abgelaufen. Melde dich neu an.") from e
            raise GogError("GOG ist gerade nicht erreichbar.") from e
        except Exception as e:      # urllib wirft je nach Lage sehr verschiedene Ausnahmen
            raise GogError("GOG ist gerade nicht erreichbar.") from e

    def take_code(self) -> bool:
        """Liegt ein frischer Code vom Browser bereit, gegen Schlüssel tauschen. True: neu angemeldet."""
        try:
            with open(self.code_file, encoding="utf-8") as fh:
                data = json.load(fh)
            os.unlink(self.code_file)
        except (OSError, ValueError):
            return False
        code = str((data or {}).get("code") or "") if isinstance(data, dict) else ""
        try:
            fresh = self._clock() - float(data.get("at") or 0) < CODE_TTL_S
        except (TypeError, ValueError, AttributeError):
            fresh = False
        if not code or not fresh:
            return False
        self._save(self._token_call(grant_type="authorization_code", code=code, redirect_uri=REDIRECT))
        return True

    def token(self) -> str:
        """Gültiger Zugangsschlüssel oder "" (nicht angemeldet). Erneuert bei Bedarf."""
        data = self._tokens()
        if not data.get("refresh_token"):
            return ""
        if data.get("access_token") and self._clock() < float(data.get("expires_at") or 0):
            return data["access_token"]
        try:
            return self._save(self._token_call(grant_type="refresh_token", refresh_token=data["refresh_token"]))["access_token"]
        except GogLoginNeeded:
            # E99: endgültig abgelehnt, also abgemeldet; sonst hieße es auf Dauer „nicht erreichbar“
            self.logout()
            return ""
        except GogError:
            return ""

    def logged_in(self) -> bool:
        return bool(self._tokens().get("refresh_token"))

    def owned(self) -> set[int]:
        """Eigene Produkt-Nummern; set() ohne Anmeldung. GogError, wenn GOG nicht antwortet (E99: ein
        kurzer Netzfehler hieß sonst zwei Minuten lang „gehört dir nicht“)."""
        if not self.logged_in():
            return set()
        token = self.token()
        if not token:
            if not self.logged_in():
                return set()                    # eben abgemeldet (Schlüssel abgelehnt)
            raise GogError("GOG ist gerade nicht erreichbar.")
        try:
            data = self._request(OWNED, token)
        except Exception as e:
            raise GogError("GOG ist gerade nicht erreichbar.") from e
        return {int(x) for x in (data.get("owned") if isinstance(data, dict) else None) or [] if str(x).isdigit()}

    def get(self, url: str) -> dict:
        """Abfrage mit Anmeldung (Downloads). GogError ohne Anmeldung oder Netz."""
        token = self.token()
        if not token:
            if self.logged_in():                # Schlüssel ließ sich gerade nicht erneuern (Netz)
                raise GogError("GOG ist gerade nicht erreichbar.")
            raise GogLoginNeeded("Melde dich zuerst bei GOG an.")
        try:
            return self._request(url, token)
        except Exception as e:
            raise GogError("GOG ist gerade nicht erreichbar.") from e

    def logout(self) -> None:
        try:
            os.unlink(self.token_file)
        except OSError:
            pass
