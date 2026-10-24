"""Fenster, die eine Seite selbst öffnet (window.open), E89.

Wunsch 2026-10-21: „Der Browser macht Probleme, wenn es Pop-ups oder Werbeseiten gibt. Ich sehe sie
zwar nicht, aber die eigentliche Seite ist dann auch nicht mehr zu steuern.“ Jedes Fenster lag bis
dahin als zweite Ansicht über der Seite (gebraucht für Anmeldungen mit window.opener); ein leeres
oder vom Werbeblocker gesperrtes Werbefenster verdeckte sie und nahm jeden Klick.

Jetzt entscheidet verdict() über die Adresse, die das Fenster lädt:
    ALLOW  dieselbe Seite wie die öffnende oder eine bekannte Anmeldung: Fenster mit Rahmen zeigen
    WAIT   noch leer (about:blank): unsichtbar warten, bis es eine Adresse lädt, dann neu urteilen
    BLOCK  alles andere: kein Fenster, Hinweis; die Leiste bietet „Pop-up öffnen“ an
Nur Standardbibliothek (testbar ohne GTK).
"""
from __future__ import annotations

import urllib.parse

from . import adblock

ALLOW, WAIT, BLOCK = "allow", "wait", "block"

# Anmeldungen und Bezahlungen, die als eigenes Fenster kommen („Mit Google anmelden“ usw.); itch.io
# (E100) meldet auch über GitHub, Google, X/Twitter und Facebook an, die stehen schon hier
LOGIN_SITES = frozenset("""
google.com apple.com microsoftonline.com live.com microsoft.com steampowered.com steamcommunity.com
gog.com epicgames.com facebook.com discord.com twitch.tv github.com paypal.com amazon.de amazon.com
x.com twitter.com klarna.com itch.io
""".split())
# Zweite Ebene unter Länderkennungen: example.co.uk ist eine Seite, co.uk nicht
_SECOND_LEVEL = frozenset("co com org net gov ac edu or ne go".split())


def site(uri: str) -> str:
    """Grobe Seite einer Adresse (letzte zwei Teile des Namens, bei co.uk & Co. drei)."""
    host = (urllib.parse.urlsplit(uri or "").hostname or "").lower().rstrip(".")
    labels = host.split(".")
    if len(labels) < 3 or host.replace(".", "").isdigit() or ":" in host:
        return host
    n = 3 if labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    return ".".join(labels[-n:])


def verdict(uri: str, opener: str) -> str:
    uri = (uri or "").strip()
    if not uri or uri == "about:blank":
        return WAIT
    if uri.startswith("blob:"):                     # blob:https://seite/… gehört zur Seite dahinter
        uri = uri[len("blob:"):]
    if urllib.parse.urlsplit(uri).scheme not in ("http", "https") or adblock.is_ad(uri):
        return BLOCK
    own = site(uri)
    if own in LOGIN_SITES or (own and own == site(opener)):
        return ALLOW
    return BLOCK
