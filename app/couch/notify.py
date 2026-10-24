"""Mitteilungen: eine kurze Liste im Speicher und das Ereignis "toast" für die Einblendung.
Die Shell zeigt dafür das Overlay passiv an (ohne Eingabefokus).

Einstellungen (Bereich „Mitteilungen“, Bauplan E22):
    notify.popups    aus: nichts wird eingeblendet, alles steht nur in der Liste
    notify.in_game   aus: solange ein Spiel oder eine App vorn ist, wird nichts eingeblendet
                     (die Mitteilung steht in der Liste und zählt als ungelesen). Die Akkuwarnung
                     und alles mit important=True kommen trotzdem: Ein leeres Pad mitten im
                     Spiel ist genau der Fall, für den die Warnung da ist.
    notify.battery   aus: Die Akkuwarnung des Controllers entfällt ganz (auch in der Liste)
    notify.seconds   wie lange eine Einblendung steht; geht als "seconds" mit dem Ereignis mit

Die Liste übersteht einen Neustart des Home (E99: Absturz, neue Fassung, „Neu starten“; sonst
war weg, was kurz davor kam, etwa ein fehlgeschlagener Download). Mit store gesetzt steht sie in
<state>/notifications.json: höchstens MAX_ITEMS Einträge, keiner älter als MAX_AGE_S, Texte
gekürzt, ohne Ton und ohne das, was in Adressen ein Geheimnis sein kann (Anmeldedaten,
Abfrageteil mit Schlüsseln). Geschrieben wird über den WriteBehind des Home (kein fsync in der
Hauptschleife).
"""
from __future__ import annotations

import itertools
import json
import os
import re
import threading
import time
from typing import Any, Callable

from . import jsontext, log, paths

_log = log.get("notify")
MAX_ITEMS = 50
MAX_AGE_S = 7 * 86400.0             # Älteres fällt beim Laden weg
MAX_TITLE, MAX_BODY = 160, 400      # so viel Text je Eintrag wird gespeichert
BATTERY_ICON = "battery"            # so kennzeichnet die Shell die Akkuwarnung (shell._pad_toasts)
DEFAULT_SECONDS = 5.0
_ICON = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
# In Adressen stehen Geheimnisse in der Anmeldung (user:pass@) und im Abfrageteil (?token=…)
_URL = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)(?:[^\s/@]*@)?([^\s?#]*)(?:[?#]\S*)?")


def store_file() -> str:
    return os.path.join(paths.state_dir(), "notifications.json")


def scrub(text: Any, limit: int) -> str:
    """Text für die Datei: ohne Anmeldedaten und Abfrageteil in Adressen, gekürzt."""
    text = _URL.sub(lambda m: m.group(1) + m.group(2), str(text or ""))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _stored(item: Any) -> dict | None:
    """Ein gespeicherter (oder zu speichernder) Eintrag in sicherer Form; None, wenn unbrauchbar."""
    if not isinstance(item, dict):
        return None
    ident, stamp = item.get("id"), item.get("time")
    if isinstance(ident, bool) or not isinstance(ident, int) or ident <= 0:
        return None
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    icon = item.get("icon")
    return {"id": ident, "title": scrub(item.get("title"), MAX_TITLE), "body": scrub(item.get("body"), MAX_BODY),
            "icon": icon if isinstance(icon, str) and _ICON.match(icon) else "info", "time": float(stamp)}


class Notifications:
    def __init__(self, rpc: Any, state: Any = None, config: Any = None,
                 defer: Callable[[Callable[[], None]], None] | None = None,
                 store: str | None = None, writer: Any = None, clock: Callable[[], float] = time.time):
        self._rpc = rpc
        self._state = state
        self._config = config
        self._defer = defer                     # bringt Aufrufe aus Threads in die Hauptschleife
        self._main = threading.get_ident()
        self._items: list[dict] = []
        self._ids = itertools.count(1)
        self._unread = 0
        self._store = store                     # Datei der Liste (None: nur im Speicher)
        self._writer = writer                   # system.WriteBehind; ohne ihn direkt geschrieben
        self._clock = clock
        self.on_toast: Callable[[dict], None] | None = None     # von der Shell gesetzt
        if store:
            self._load()

    # ------------------------------------------------------------ Datei
    def _load(self) -> None:
        try:
            with open(self._store, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _log.warning("Mitteilungen von vorher nicht lesbar: %s", e)
            return
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return
        now = self._clock()
        items = [i for i in (_stored(raw) for raw in data["items"][-MAX_ITEMS:])
                 if i is not None and now - i["time"] < MAX_AGE_S]
        unread = data.get("unread")
        self._items = items
        self._unread = unread if isinstance(unread, int) and not isinstance(unread, bool) and unread >= 0 else 0
        self._unread = min(self._unread, len(items))
        self._ids = itertools.count(max((i["id"] for i in items), default=0) + 1)
        if items:
            _log.info("%d Mitteilungen von vorher übernommen (%d ungelesen)", len(items), self._unread)
        self._sync(save=False)

    def _save(self) -> None:
        if not self._store:
            return
        text = jsontext.dumps({"items": [_stored(i) for i in self._items], "unread": self._unread})
        try:
            if self._writer is not None:
                self._writer.write(self._store, text)
            else:
                from . import system
                system.write_atomic(self._store, text)
        except Exception as e:  # noqa: BLE001 - die Liste gilt im Speicher weiter
            _log.warning("Mitteilungen nicht speicherbar: %s", e)

    # ------------------------------------------------------------ Einstellungen
    def _cfg(self, key: str, default: Any) -> Any:
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            _log.exception("Einstellung %s", key)
            return default
        return default if value is None else value

    def seconds(self) -> float:
        """Dauer einer Einblendung; Unsinn in den Einstellungen fällt auf die Vorgabe."""
        raw = self._cfg("notify.seconds", DEFAULT_SECONDS)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return DEFAULT_SECONDS
        return float(min(30.0, max(2.0, raw)))

    def _app_in_front(self) -> bool:
        if self._state is None:
            return False
        try:
            return self._state.value("foreground") is not None
        except Exception:
            return False

    def _shows(self, icon: str, important: bool) -> bool:
        """Wird diese Mitteilung eingeblendet (sonst steht sie nur in der Liste)?"""
        if not self._cfg("notify.popups", True):
            return False
        if important or icon == BATTERY_ICON:
            return True
        return bool(self._cfg("notify.in_game", True)) or not self._app_in_front()

    # ------------------------------------------------------------ Schnittstelle
    def push(self, title: str, body: str = "", icon: str = "info", sound: str | None = None,
             silent: bool = False, important: bool = False) -> int:
        """Neue Mitteilung. silent: nur in die Liste, keine Einblendung. important: auch über
        einem Spiel einblenden, wenn notify.in_game aus ist. Aus einem Arbeits-Thread gerufen,
        läuft sie in der Hauptschleife nach (Rückgabe dann 0)."""
        if self._defer is not None and threading.get_ident() != self._main:
            self._defer(lambda: self.push(title, body, icon, sound, silent, important))
            return 0
        if icon == BATTERY_ICON and not self._cfg("notify.battery", True):
            return 0                            # abbestellt: weder Einblendung noch Listeneintrag
        item = {"id": next(self._ids), "title": title, "body": body, "icon": icon,
                "time": self._clock()}
        if sound:
            item["sound"] = sound
        self._items.append(item)
        del self._items[:-MAX_ITEMS]
        self._unread += 1
        self._sync()
        if not silent and self._shows(icon, important):
            self._rpc.emit("toast", {**item, "seconds": self.seconds()})
            if self.on_toast:
                try:
                    self.on_toast(item)
                except Exception:
                    _log.exception("on_toast")
        return item["id"]

    def list(self) -> list[dict]:
        return [dict(i) for i in reversed(self._items)]      # neueste zuerst

    def mark_read(self) -> None:
        changed = self._unread != 0
        self._unread = 0
        self._sync(save=changed)                # jedes Öffnen der Liste ruft das: nur Neues schreiben

    def clear(self) -> None:
        self._items.clear()
        self._unread = 0
        self._sync()

    def remove(self, item_id: int) -> bool:
        """Eine einzelne Mitteilung löschen (07 6.4, Quadrat). Unbekannt: nichts passiert."""
        before = len(self._items)
        self._items = [i for i in self._items if i["id"] != item_id]
        if len(self._items) == before:
            return False
        # Ungelesen können höchstens so viele sein, wie noch in der Liste stehen
        self._unread = min(self._unread, len(self._items))
        self._sync()
        return True

    def _sync(self, save: bool = True) -> None:
        if self._state:
            self._state.update(notifications=self._unread)
        if save:
            self._save()

    def register_rpc(self, rpc: Any) -> None:
        def notify_list(params: dict) -> dict:
            items = self.list()
            self.mark_read()                    # wer die Liste öffnet, hat sie gesehen
            return {"items": items}

        def notify_clear(params: dict) -> dict:
            self.clear()
            return {}

        def notify_remove(params: dict) -> dict:
            from .rpc import RpcError
            item_id = params.get("id")
            if isinstance(item_id, bool) or not isinstance(item_id, int):
                raise RpcError("Ungültiger Aufruf.", "BadRequest")
            self.remove(item_id)
            return {}

        rpc.register("notify.list", notify_list)
        rpc.register("notify.clear", notify_clear)
        rpc.register("notify.remove", notify_remove)
