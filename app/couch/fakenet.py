"""Netz-Attrappen für Prüfläufe: die eine Prüfung ihrer Adresse. Genutzt für den Steam-Store der Bilder
(COUCH_ART_FAKE_STORE, wiring_art), Spielinfos und Trophäen (COUCH_STEAMINFO_FAKE, wiring_gameinfo) und die Videos
(COUCH_BOOTVIDEO_FAKE, bootvideos)."""
from __future__ import annotations

import os

from . import paths


def base_url(env: str) -> str | None:
    """Adresse aus der Umgebungsvariablen env, ohne „/“ am Ende: nur im Prüfmodus, nur http:// und nur auf diesem
    Rechner (Loopback). Sonst None (dann gibt es im Prüfmodus kein Netz)."""
    from .library.artsources.base import host_of, is_loopback
    base = os.environ.get(env) or ""
    if not paths.test_mode() or not base.startswith("http://") or not is_loopback(host_of(base)):
        return None
    return base.rstrip("/")
