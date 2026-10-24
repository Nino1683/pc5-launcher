"""Werbeblocker des Browsers (E61): Regeln für WebKits Inhaltsfilter (Format der Content Blocker).

Gesperrt werden Anfragen fremder Seiten (load-type third-party) an bekannte Werbe- und Tracking-
Netze; dieselbe Seite direkt aufzurufen bleibt möglich. Schwere Seiten werden dadurch vor allem
leichter: weniger Skripte, Videos und Animationen im Hintergrund (2026-10-18: „die Performance
vom Browser lässt nach Benutzung stark nach“, ein Seitenprozess bei 1,2 GB und 59 % CPU).
Bewusst kurze, handgepflegte Liste; im Menü abschaltbar. Nur Standardbibliothek.
"""
from __future__ import annotations

import json
import re
import urllib.parse

VERSION = "1"                # ändern, wenn sich die Liste ändert (WebKit übersetzt sie dann neu)
IDENTIFIER = "couch-adblock-" + VERSION

DOMAINS = """
doubleclick.net googlesyndication.com googleadservices.com google-analytics.com adservice.google.com
pagead2.googlesyndication.com imasdk.googleapis.com adnxs.com adsrvr.org amazon-adsystem.com
taboola.com outbrain.com criteo.com criteo.net pubmatic.com rubiconproject.com openx.net casalemedia.com
scorecardresearch.com quantserve.com quantcount.com moatads.com advertising.com adform.net bidswitch.net
33across.com sharethrough.com smartadserver.com teads.tv yieldmo.com zedo.com indexww.com contextweb.com
lijit.com sovrn.com media.net revcontent.com mgid.com adblade.com triplelift.com gumgum.com
popads.net popcash.net propellerads.com propellerclick.com exoclick.com exosrv.com juicyads.com adcash.com
adsterra.com adsterratools.com hilltopads.net clickadu.com trafficjunky.net trafficfactory.biz
onclickads.net onclkds.com popunder.net adskeeper.com adskeeper.co.uk ad-maven.com admaven.com
hotjar.com mouseflow.com crazyegg.com fullstory.com clarity.ms
yandex.ru/ads an.yandex.ru mc.yandex.ru
facebook.net/tr connect.facebook.net/signals ads.linkedin.com ads.twitter.com static.ads-twitter.com
ads.pinterest.com analytics.tiktok.com ads.tiktok.com
serving-sys.com sizmek.com flashtalking.com adroll.com perfectaudience.com
bluekai.com krxd.net demdex.net omtrdc.net everesttech.net rlcdn.com agkn.com
""".split()


def rules(domains: list[str] | None = None) -> list[dict]:
    """Ein Regelsatz je Domain (mit Unterdomains); Einträge mit "/" sperren nur diesen Pfad."""
    out = []
    for item in domains if domains is not None else DOMAINS:
        host, _, path = item.partition("/")
        pattern = r"^[a-z]+://([^/]*\.)?" + _escape(host) + r"[:/]"
        if path:
            pattern += _escape(path)
        out.append({"trigger": {"url-filter": pattern, "load-type": ["third-party"]},
                    "action": {"type": "block"}})
    return out


def is_ad(uri: str, domains: list[str] | None = None) -> bool:
    """Führt die Adresse zu einem Netz der Liste? Für Fenster, die Seiten öffnen (E89): Die Regeln
    oben greifen nur bei Anfragen fremder Seiten, ein neues Fenster lädt aber als eigene Seite."""
    parts = urllib.parse.urlsplit(uri or "")
    host = (parts.hostname or "").lower()
    if not host:
        return False
    for item in domains if domains is not None else DOMAINS:
        domain, _, path = item.partition("/")
        if (host == domain or host.endswith("." + domain)) and parts.path.lstrip("/").startswith(path):
            return True
    return False


def _escape(text: str) -> str:
    """Nur „.“ ist in Domains und Pfaden der Liste ein Sonderzeichen; WebKits Filter-Regex kennt
    nicht jedes Escape, das re.escape erzeugt."""
    if not re.fullmatch(r"[A-Za-z0-9.\-/_]+", text):
        raise ValueError("unerwartetes Zeichen in %r" % text)
    return text.replace(".", r"\.")


def rules_json() -> str:
    return json.dumps(rules())
