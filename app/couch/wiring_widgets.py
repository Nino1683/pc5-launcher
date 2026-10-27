"""Widget-Leiste im Home (E126): Verdrahtung, eingehängt über wiring.PARTS (nach "trophies").

Unten im Home stehen Karten (web/js/components/widgets.js): Speicherplatz der Spiele-Platte, Trophäen gesamt
(trophies.summary, E124), Angebote der Wunschliste (E122) und Zuletzt gespielt (die Reihe des Home, ohne Kern).
Jedes Widget lässt sich unter Einstellungen › Home-Bildschirm › Widgets ausblenden (settings/home.py).

    widgets.get {}           -> {storage: {path, label, free, total} | null, deals: {count, best, checked} | null}
                                im Pool (statvfs kann an einer schlafenden Platte hängen, nie in der Hauptschleife)
    sys.state.widgets        {storage, trophies, deals, recent}: welche Widgets an sind (ui.widget_*)

deals liest nur <state>/wishlist-deals.json (store/wishlist.py schreibt sie): Zahl der gerade gemeldeten Angebote, der
größte Rabatt und wann zuletzt geprüft wurde; null, solange nie geprüft wurde (keine Steam-Anmeldung im Store).
"""
from __future__ import annotations

import json
import os
from typing import Any

from . import log, paths

_log = log.get("wiring.widgets")

KEYS = {"storage": "ui.widget_storage", "trophies": "ui.widget_trophies", "deals": "ui.widget_deals",
        "recent": "ui.widget_recent"}
DEFAULTS = {key: True for key in KEYS.values()}
DEALS_FILE = "wishlist-deals.json"


def storage(path: str | None = None) -> dict | None:
    """Frei und gesamt auf der Spiele-Platte; None, wenn sie gerade fehlt."""
    path = path or paths.games_disk()
    try:
        if not os.path.isdir(path):
            return None
        st = os.statvfs(path)
    except OSError:
        return None
    return {"path": path, "label": os.path.basename(os.path.normpath(path)) or path,
            "free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}


def deals(state_dir: str | None = None) -> dict | None:
    """Angebote der Wunschliste aus dem Stand von E122; None = nie geprüft."""
    try:
        with open(os.path.join(state_dir or paths.state_dir(), DEALS_FILE), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    checked = data.get("checked_at")
    reported = data.get("reported") if isinstance(data.get("reported"), dict) else {}
    found = [v for k, v in reported.items() if str(k).isdigit() and isinstance(v, int) and not isinstance(v, bool) and v > 0]
    if not isinstance(checked, (int, float)) or isinstance(checked, bool):
        return None if not found else {"count": len(found), "best": max(found), "checked": 0}
    return {"count": len(found), "best": max(found, default=0), "checked": int(checked)}


def flags(config: Any) -> dict:
    out = {}
    for name, key in KEYS.items():
        try:
            out[name] = bool(config.get(key, True))
        except Exception:  # noqa: BLE001 (kaputte Einstellung: Vorgabe an)
            out[name] = True
    return out


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(dict(DEFAULTS))

    def get(params: dict) -> Any:
        return wiring._bg(lambda: {"storage": storage(), "deals": deals()})

    app.rpc.register("widgets.get", get)

    def push() -> None:
        app.state.update(widgets=flags(wiring.config))
    push()
    on_change = getattr(wiring.config, "on_change", None)
    if callable(on_change):
        on_change("ui.widget_", lambda _key, _value: wiring.main.post(push))
