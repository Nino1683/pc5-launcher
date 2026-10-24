"""Bilder der Bibliothek an den Kern hängen (Bauplan E34, E42).

Hier entsteht der ArtResolver (wiring._make_art ruft make_art) mit allem, was er vom Kern
braucht: Orte, Einstellungen, die Durchsuchen-Wurzeln für Vorschaubilder und die Auskunft, ob ins
Netz gegriffen werden darf. Was mit den Bildern geschieht (Thread, RPC, automatisch bei neuer
Kachel), verdrahtet der Bibliotheks-Teil über wiring.art:

    resolve(entry) · wants_fetch(entry) · fetch(entry, kinds=None, force=False) · fetch_detail(…)
    candidates(entry, query=None) · fetch_candidate(entry, candidate) · clear_choice(entry_id)
    preview(path) · set_user_art / clear_user_art / clear_cache

Alles davon außer wants_fetch blockiert und gehört in einen Arbeits-Thread (wiring._bg).

Prüfmodus: Mit COUCH_TEST_ROOT fragt das Home keinen echten Dienst ab (Prüfläufe mit erfundenen
Titeln sollen keinen Shop behelligen). COUCH_ART_NET=1 hebt das für einen bewussten Lauf auf.
COUCH_ART_FAKE_STORE=http://127.0.0.1:<port> lenkt den Abruf im Prüfmodus auf eine Attrappe des
Steam-Stores um (tests/features/art/fake_store.py); andere Quellen gibt es dann nicht.
"""
from __future__ import annotations

import os
from typing import Any

from . import log, paths

_log = log.get("wiring.art")

SGDB_KEY = "library.sgdb_key"
# Vorgaben der eigenen Schlüssel (E22/E34). library.fetch_art steht schon in config.py.
DEFAULTS = {
    SGDB_KEY: "",                      # eigener SteamGridDB-Schlüssel; leer = die Quelle bleibt aus
}


def fake_store() -> str | None:
    """Adresse einer Store-Attrappe für Prüfläufe: nur im Prüfmodus und nur auf diesem Rechner."""
    from .library.artsources.base import host_of, is_loopback
    base = os.environ.get("COUCH_ART_FAKE_STORE") or ""
    if not paths.test_mode() or not base.startswith("http://") or not is_loopback(host_of(base)):
        return None
    return base.rstrip("/")


def net_allowed() -> bool:
    """Darf das Home für Bilder ins Netz? Im Prüfmodus nur mit COUCH_ART_NET=1 (echte Dienste)
    oder gegen eine Attrappe auf diesem Rechner."""
    return (not paths.test_mode() or os.environ.get("COUCH_ART_NET") == "1" or fake_store() is not None)


def make_art(wiring: Any) -> Any:
    """Legt den ArtResolver an. Läuft beim Start in der Hauptschleife: nur Objekte bauen, keine
    Platte, kein Netz."""
    from .library import art as art_mod
    from .library import artfetch
    src = wiring.sources
    home = src.home if src is not None else paths.home()

    def online() -> bool:
        return net_allowed() and (fake is not None or artfetch.default_online())

    fake = fake_store()
    fetcher = None
    if fake is not None:
        # Prüflauf gegen die Attrappe: nur die Steam-Quelle, ohne Wartezeiten
        fetcher = artfetch.ArtFetcher(
            paths.cache_dir(), wiring.config, online=online, min_interval=0.0,
            search_url=fake + "/search", items_url=fake + "/items", cdn_url=fake + "/cdn/",
            choices_path=os.path.join(paths.data_dir(), "art", art_mod.CHOICES_FILE),
            log=lambda text: _log.info("Bilder: %s", text))
    ctx = art_mod.ArtContext(
        data_dir=paths.data_dir(), cache_dir=paths.cache_dir(), config=wiring.config,
        steam_roots=(src.steam_root,) if src is not None else (os.path.join(home, ".local/share/Steam"),),
        lutris_dir=os.path.join(home, ".local/share/lutris"),
        lutris_icon_dir=os.path.join(home, ".local/share/icons/hicolor"),
        online=online, fetcher=fetcher,
        # nur was die Dateiauswahl zeigen darf, bekommt ein Vorschaubild
        browse_roots=paths.browse_roots,
        log=lambda text: _log.info("Bilder: %s", text))
    if src is not None and not src.is_real:
        ctx.app_icon_roots = tuple(os.path.join(d, "exports/share/icons") for d in src.flatpak_dirs)
    return art_mod.ArtResolver(ctx)


def sources_state(app: Any) -> list[dict]:
    """Welche Bild-Quellen es gibt und ob sie gerade benutzbar sind (für eine Einstellungsseite
    oder den Bericht): [{"name", "label", "available", "resting_s"}]. Schnell, kein Netz."""
    resolver = getattr(getattr(app, "wiring", None), "art", None)
    if resolver is None:
        return []
    fetcher = resolver._get_fetcher()
    return [{"name": s.name, "label": s.label, "available": bool(s.available()),
             "resting_s": int(fetcher.service_blocked(s.name))} for s in fetcher.sources]


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(dict(DEFAULTS))
    if wiring.art is None:
        _log.warning("Bilder der Bibliothek fehlen: kein Abruf aus dem Netz, keine Vorschaubilder")
        return
    if not net_allowed():
        _log.info("Prüfmodus: Bilder werden nicht aus dem Netz geladen (COUCH_ART_NET=1 erlaubt es)")

    def stop() -> None:
        # ein laufender Abruf bricht bei der nächsten Anfrage ab; nichts davon wird vermerkt
        if wiring.art is not None:
            wiring.art.cancel()
    wiring.shutdown_hooks.append(stop)
    on_change = getattr(app.config, "on_change", None)
    if on_change is not None:
        on_change(SGDB_KEY, lambda key, value: sgdb_key_changed(wiring, value))


def sgdb_key_changed(wiring: Any, value: Any) -> None:
    """Ein neuer SteamGridDB-Schlüssel hebt die Pause nach „Schlüssel abgelehnt“ auf (E99). Sonst
    bliebe die Quelle bis zu 60 Minuten aus, und die Einstellungen, die gleich nach dem Eintragen neu
    fragen, zeigten weiter „Pause, noch N Min.“. Andere Pausen (zu viele Anfragen, Netz) bleiben.
    Läuft im Beobachter von config.set: nur die kleine Merkdatei des Abrufers, kein Netz. Der
    Schlüssel selbst kommt nie ins Protokoll."""
    resolver = wiring.art
    if resolver is None or not (isinstance(value, str) and value.strip()):
        return
    try:
        if resolver._get_fetcher().lift_service("sgdb", "key-rejected"):
            _log.info("Bilder: neuer SteamGridDB-Schlüssel, die Pause ist aufgehoben")
    except Exception:  # noqa: BLE001 (der Schlüssel ist trotzdem gespeichert)
        _log.exception("Pause von SteamGridDB aufheben")
