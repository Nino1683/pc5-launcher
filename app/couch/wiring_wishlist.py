"""Angebote auf der Steam-Wunschliste (E122): Verdrahtung, eingehängt über wiring.PARTS (nach "store").

  Zeitplan    FIRST_S nach dem Bereit-Signal, danach stündlich nachsehen, ob 24 h um sind (store/wishlist.py,
              nach der Wanduhr; der Zeitgeber läuft auf der monotonen Uhr und steht im Ruhemodus). Nie, solange ein
              Spiel startet (wiring_selfupdate.launching), und nur mit notify.wishlist an. Derselbe Zeitplan wie das
              Selbst-Update (wiring_selfupdate.DailyTimer, E116), nur zehn Minuten später: nicht beides zugleich kurz
              nach dem Start.
  Konto       wiring.store_steam_account (wiring_store.get_account): ohne Steam-Anmeldung im Store passiert nichts,
              auch kein Abruf; die Prüfung gilt dann nicht als erledigt.
  Mitteilung  je neuem Angebot eine (mehr als drei auf einmal: zwei einzeln, der Rest zusammen), Symbol „store“.
              Mitteilungen haben keine Aktion (notify.py): Die Store-Seite öffnet man im Store selbst.
  Prüfmodus   ohne eingesteckte Netzfunktion (wiring.wishlist_get) gar nichts.
"""
from __future__ import annotations

from typing import Any

from . import i18n, log, paths
from .store import wishlist
from .wiring_selfupdate import LAUNCH_WAIT_S, TICK_S, DailyTimer  # noqa: F401  (TICK_S, LAUNCH_WAIT_S: dieselben)

_log = log.get("wiring.wishlist")

FIRST_S = 900.0          # erste Prüfung eine Viertelstunde nach dem Start
DEFAULTS = {"notify.wishlist": True}
ICON = "store"


def _real_get(url: str) -> bytes:
    from .store import steam_info
    return steam_info._get(url)


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if add is not None:
        add(dict(DEFAULTS))
    injected = getattr(wiring, "wishlist_get", None)        # Tests und Prüfläufe: Netz-Attrappe
    deals = wishlist.WishlistDeals(paths.state_dir(), injected or _real_get,
                                   **(getattr(wiring, "wishlist_options", None) or {}))
    wiring.wishlist = deals

    def account() -> dict:
        fn = getattr(wiring, "store_steam_account", None)
        if not callable(fn):
            return {}
        try:
            return fn() or {}
        except Exception:  # noqa: BLE001 (Konto nicht lesbar: heute nicht)
            _log.exception("Steam-Konto für die Wunschliste")
            return {}

    def run_check() -> None:
        """Arbeits-Thread. Ohne Anmeldung kein Abruf und kein Vermerk: Nach der Anmeldung kommt die Prüfung bald."""
        if not deals.due():
            return
        acct = account()
        if not acct.get("logged_in") or acct.get("error"):
            return
        order = list(acct.get("wishlist_order") or sorted(acct.get("wishlist") or ()))
        found = deals.check(order, str(acct.get("country") or "de"), i18n.language(wiring.config))
        for title, body in wishlist.messages(found):
            app.notify.push(title, body, icon=ICON)

    timer = DailyTimer(wiring, "notify.wishlist", run_check, "der Wunschliste")

    def start() -> None:
        if paths.test_mode() and injected is None:
            return                              # Prüfmodus: kein Netz, keine Prüfung
        timer.start(FIRST_S)

    wiring.after_ready.append(start)
    wiring.shutdown_hooks.append(timer.close)
