"""Was in der Adressleiste steht -> Adresse zum Laden (E59)."""
from __future__ import annotations

import re
import urllib.parse

DEFAULT_SEARCH = "https://www.google.com/search?q=%s"
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_HOSTLIKE = re.compile(r"^(localhost|\[[0-9a-fA-F:]+\]|[^\s/?#]+\.[^\s/?#]{2,}|\d{1,3}(\.\d{1,3}){3})(:\d{1,5})?([/?#].*)?$")
ALLOWED_SCHEMES = ("http", "https", "file", "about")


def to_uri(text: str, search: str = DEFAULT_SEARCH) -> str:
    """Adresse mit Schema bleibt (nur http, https, file, about), Rechnername mit Punkt bekommt
    https://, alles andere wird gesucht. Leer -> ""."""
    text = (text or "").strip()
    if not text:
        return ""
    if _SCHEME.match(text) and not re.match(r"^[^\s/]+:\d", text):
        scheme = text.split(":", 1)[0].lower()
        if scheme in ALLOWED_SCHEMES:
            return text
        return search_uri(text, search)
    if " " not in text and _HOSTLIKE.match(text):
        return "https://" + text
    return search_uri(text, search)


def search_uri(text: str, search: str = DEFAULT_SEARCH) -> str:
    if "%s" not in search or not search.startswith(("https://", "http://")):
        search = DEFAULT_SEARCH
    return search.replace("%s", urllib.parse.quote_plus(text.strip()))


def shown(uri: str) -> str:
    """So steht eine Adresse in der Leiste: ohne Schema bei https, die Startseite leer."""
    if not uri or uri.startswith(("file:", "about:")):
        return ""
    return uri[len("https://"):] if uri.startswith("https://") else uri
