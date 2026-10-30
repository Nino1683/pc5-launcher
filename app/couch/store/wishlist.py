"""Angebote auf der Steam-Wunschliste (E122): einmal am Tag die Preise prüfen, je Spiel und Rabatt einmal melden.

Die Wunschliste liest der Store über die Anmeldung im Browser (steam_account.userdata, rgWishlist). Die Preise
kommen gebündelt und ohne Anmeldung aus appdetails?appids=a,b,…&filters=price_overview&cc=<land> (mehrere
Nummern gehen dort nur mit genau diesem Filter), die Namen der neuen Angebote aus GetItems (ein Abruf für alle).

Gemerkt wird in <state>/wishlist-deals.json: wann zuletzt geprüft wurde und welcher Rabatt je Spiel schon
gemeldet ist. Dasselbe Spiel mit demselben Rabatt kommt nicht noch einmal; ein anderer Rabatt schon. Endet das
Angebot (Steam meldet den Normalpreis), wird das Spiel vergessen, damit das nächste Angebot wieder kommt; fehlt
ein Spiel nur in einer Antwort, bleibt es gemerkt. Alles hier blockiert (Netz, Platte): Arbeits-Thread.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
from typing import Any, Callable

from .. import jsontext, log
from .steam import ITEMS_URL, LANGS, SHOP

_log = log.get("store.wishlist")
STATE_NAME = "wishlist-deals.json"
CHECK_EVERY_S = 24 * 3600.0
DUE_SLACK_S = 600.0             # so viel früher gilt die Prüfung schon als fällig (Takt des Zeitgebers)
RETRY_AFTER_S = 3 * 3600.0      # nach einem Fehler erst dann wieder von selbst
CHUNK = 50                      # Nummern je Preisabfrage
MAX_APPS = 500                  # sehr lange Wunschlisten: die vorderen 500 (zehn Abfragen)
GAP_S = 1.5                     # zwischen zwei Preisabfragen (appdetails drosselt)
SINGLE = 3                      # so viele Angebote einzeln; mehr auf einmal (Steam-Sale) als eine Sammelmeldung


def price_text(formatted: Any) -> str:
    """„19,99€“ -> „19,99 €“ (Schreibweise des Stores)."""
    text = str(formatted or "").strip()
    return text.replace("€", " €").replace("  ", " ").strip() if "€" in text and " €" not in text else text


def parse_prices(raw: Any) -> dict[int, dict]:
    """Antwort von appdetails (price_overview) -> {appid: {"discount", "price"}}; nur Spiele mit Preis."""
    out: dict[int, dict] = {}
    if not isinstance(raw, dict):
        return out
    for key, node in raw.items():
        if not str(key).isdigit() or not isinstance(node, dict) or not node.get("success"):
            continue
        data = node.get("data")
        po = data.get("price_overview") if isinstance(data, dict) else None
        if not isinstance(po, dict):
            # Ohne Preis (kostenlos, noch nicht erschienen): kein Angebot. Steam liefert dann [] statt {}
            out[int(key)] = {"discount": 0, "price": ""}
            continue
        try:
            discount = int(po.get("discount_percent") or 0)
        except (TypeError, ValueError):
            discount = 0
        out[int(key)] = {"discount": max(0, min(100, discount)), "price": price_text(po.get("final_formatted"))}
    return out


def messages(deals: list[dict]) -> list[tuple[str, str]]:
    """(Titel, Text) je Mitteilung: bis SINGLE einzeln, darüber die ersten SINGLE - 1 einzeln und der Rest
    zusammen (im Steam-Sale sind oft Dutzende Spiele auf einmal im Angebot)."""
    def one(d: dict) -> tuple[str, str]:
        # Der Titel der Einblendung ist kurz („von deiner Wunschliste …“ schnitt den Rabatt ab, Befund 2026-10-27)
        if d.get("price"):
            body = "Deine Wunschliste: −%d %% (%s)" % (d["discount"], d["price"])
        else:
            body = "Deine Wunschliste: −%d %%" % d["discount"]
        return ("„%s“ im Angebot" % d["title"], body)
    if len(deals) <= SINGLE:
        return [one(d) for d in deals]
    head, rest = deals[:SINGLE - 1], deals[SINGLE - 1:]
    names = ", ".join("%s (−%d %%)" % (d["title"], d["discount"]) for d in rest)
    return [one(d) for d in head] + [("Weitere %d Spiele deiner Wunschliste sind im Angebot" % len(rest), names)]


class WishlistDeals:
    def __init__(self, state_dir: str, get: Callable[[str], bytes], clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, shop: str = SHOP, items_url: str = ITEMS_URL):
        self.state_dir = state_dir
        self._get = get
        self._clock = clock
        self._sleep = sleep
        self.shop = shop.rstrip("/")
        self.items_url = items_url
        self.lock = threading.Lock()
        self.checking = False

    @property
    def state_file(self) -> str:
        return os.path.join(self.state_dir, STATE_NAME)

    def _load(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            _log.warning("%s unlesbar, beginne neu: %s", self.state_file, e)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            from .. import system
            system.write_atomic(self.state_file, jsontext.dumps(data, indent=1, sort_keys=True) + "\n")
        except OSError as e:
            _log.warning("%s nicht schreibbar: %s", self.state_file, e)

    def due(self, now: float | None = None) -> bool:
        st = self._load()
        now = self._clock() if now is None else now
        last, err = st.get("checked_at"), st.get("error_at")
        if isinstance(err, (int, float)) and not isinstance(err, bool) and 0 <= now - err < RETRY_AFTER_S:
            return False
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            return True
        passed = now - last
        # Ging die Uhr weit zurück (vor die letzte Prüfung), gilt sie als fällig
        return passed < -DUE_SLACK_S or passed >= CHECK_EVERY_S - DUE_SLACK_S

    def _json(self, url: str) -> Any:
        return json.loads(self._get(url).decode("utf-8"))

    def prices(self, appids: list[int], cc: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for n in range(0, len(appids), CHUNK):
            if n:
                self._sleep(GAP_S)
            ids = ",".join(str(a) for a in appids[n:n + CHUNK])
            out.update(parse_prices(self._json("%s/api/appdetails?appids=%s&filters=price_overview&cc=%s"
                                               % (self.shop, ids, urllib.parse.quote(cc)))))
        return out

    def names(self, appids: list[int], cc: str, lang: str) -> dict[int, str]:
        """Namen aus GetItems; fehlt einer, bleibt die Nummer (das Angebot wird trotzdem gemeldet)."""
        if not appids:
            return {}
        req = {"ids": [{"appid": a} for a in appids], "context": {"language": LANGS.get(lang, "english"),
                                                                 "country_code": cc.upper()}, "data_request": {}}
        try:
            data = self._json(self.items_url + "?input_json=" + urllib.parse.quote(json.dumps(req, separators=(",", ":"))))
        except Exception as e:  # noqa: BLE001 (Namen sind Beiwerk)
            _log.warning("Wunschliste: Namen nicht lesbar: %s", e)
            return {}
        items = ((data.get("response") if isinstance(data, dict) else None) or {}).get("store_items") or []
        out = {}
        for it in items:
            if isinstance(it, dict) and str(it.get("appid") or "").isdigit() and it.get("name"):
                out[int(it["appid"])] = str(it["name"])[:120]
        return out

    def check(self, wishlist: list[int], cc: str = "de", lang: str = "de", force: bool = False) -> list[dict]:
        """Arbeits-Thread: die Preise prüfen, wenn fällig. Liefert die neu zu meldenden Angebote
        [{"appid", "title", "discount", "price"}]; wirft nie (ein Fehler wird gemerkt, Rückgabe [])."""
        with self.lock:
            if self.checking:
                return []
            now = self._clock()
            if not force and not self.due(now):
                return []
            self.checking = True
        try:
            st = self._load()
            ids = list(dict.fromkeys(int(a) for a in wishlist if (isinstance(a, int) and not isinstance(a, bool) and a > 0)
                                     or (isinstance(a, str) and a.isdigit())))[:MAX_APPS]
            reported = {str(k): int(v) for k, v in (st.get("reported") or {}).items()
                        if str(k).isdigit() and isinstance(v, int) and not isinstance(v, bool)}
            try:
                prices = self.prices(ids, (cc or "de").lower())
            except Exception as e:  # noqa: BLE001 (Netz, Drosselung, kaputte Antwort: in drei Stunden wieder)
                _log.warning("Wunschliste: Preise nicht lesbar: %s", e)
                if isinstance(e, urllib.error.HTTPError):
                    e.close()
                st["error_at"] = now
                self._save(st)
                return []
            new = []
            for appid in ids:
                p = prices.get(appid)
                if p is None:
                    continue                    # diesmal keine Antwort: nichts vergessen, nichts melden
                if p["discount"] <= 0:
                    reported.pop(str(appid), None)     # Angebot vorbei: das nächste wird wieder gemeldet
                elif reported.get(str(appid)) != p["discount"]:
                    new.append({"appid": appid, "discount": p["discount"], "price": p["price"]})
            # Von der Wunschliste genommen: nichts mehr merken
            keep = {str(a) for a in ids}
            reported = {k: v for k, v in reported.items() if k in keep}
            names = self.names([d["appid"] for d in new], (cc or "de").lower(), lang)
            for d in new:
                d["title"] = names.get(d["appid"]) or "Steam-Spiel %d" % d["appid"]
                reported[str(d["appid"])] = d["discount"]
            st.update(checked_at=now, reported=reported)
            st.pop("error_at", None)
            self._save(st)
            if new:
                _log.info("Wunschliste: %d neue Angebote", len(new))
            return new
        finally:
            with self.lock:
                self.checking = False
