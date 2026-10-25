"""Einstellungen › Home-Bildschirm: Design (E113), bewegter Hintergrund (E33), Zeitformat, Cover und
Hintergründe aus dem Netz (E34).

Hier steht nur, was die Oberfläche selbst umsetzt bzw. was die Bilder der Kacheln betrifft. Die
Töne der Oberfläche stehen unter „Ton“ (audio.ui_sounds, audio.ui_volume): Jede Zeile gibt es
genau einmal (E22).

Was der Kern einsteckt (deps.extra, freiwillig):
    "fetch_all_art"()    Cover und Hintergründe für alle Einträge im Hintergrund laden;
                         -> {"started": bool, "total": int, "message": str}. Fehlt der Rückruf,
                         ist die Zeile gesperrt.
    "art_sources"()      Zustand der Bild-Quellen (wiring_art.sources_state): [{"name",
                         "available", "resting_s", …}]. Ruht SteamGridDB nach Störungen, sagt
                         die Schlüssel-Zeile das (E99). Fehlt der Rückruf, steht nur „Eingetragen“.

Der SteamGridDB-Schlüssel (library.sgdb_key) verlässt dieses Modul nie: Die Zeile zeigt nur, ob
einer eingetragen ist, und der Wert steht in keiner Meldung und keinem Protokoll.
"""
from __future__ import annotations

import logging
import re

from .. import themes
from .rows import AREA_ORDER, Page, SettingsError, action, option, select, text, toggle

log = logging.getLogger("couch.settings.home")

AREA_ID = "home"

DEFAULTS: dict = {
    "ui.theme": "",
    "ui.live_background": True,
    "ui.intro": True,
    "ui.clock_24h": True,
    "library.fetch_art": True,
    "library.sgdb_key": "",
}

SGDB_HINT = ("Für Spiele, die es nicht bei Steam gibt. Den Schlüssel bekommst du kostenlos in "
             "deinem SteamGridDB-Konto.")
# SteamGridDB ruht (artfetch: Schlüssel abgelehnt, zu viele Anfragen oder mehrere Störungen in Folge)
SGDB_RESTING_NOTE = "Eingetragen · Pause, noch %s"
SGDB_RESTING_HINT = ("SteamGridDB hat zuletzt nicht geantwortet oder den Schlüssel abgelehnt. In etwa %s "
                     "fragt der PC5 Launcher wieder.")
# SteamGridDB vergibt 32 Hex-Zeichen; etwas großzügiger, aber nie Leerzeichen oder Steuerzeichen
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

THEME_HINT = ("Ändert die Farben der ganzen Oberfläche. Eigene Designs: eine .css-Datei mit Farben aus tokens.css "
              "in den Ordner ~/.config/couch/themes legen.")
THEME_STANDARD = "Standard"
THEME_OWN = "Eigenes Design"

# Schalter: Zeile -> (Schlüssel, Beschriftung, Erklärung)
_TOGGLES = {
    "home.live_background": (
        "ui.live_background", "Bewegter Hintergrund",
        "Der Hintergrund des Home bewegt sich ruhig. Steht still, sobald ein Spiel läuft."),
    "home.intro": (
        "ui.intro", "Startanimation",
        "Beim Einschalten und nach einem Neustart des PC5 Launchers erscheint das Logo mit Ton. "
        "Jede Taste überspringt es."),
    "home.clock_24h": (
        "ui.clock_24h", "24-Stunden-Anzeige",
        "Aus: Die Uhr zeigt die Zeit im 12-Stunden-Format."),
    "home.fetch_art": (
        "library.fetch_art", "Bilder automatisch laden",
        "Fehlende Cover und Hintergründe werden im Hintergrund aus dem Netz geladen."),
}


class HomeArea:
    id = AREA_ID
    label = "Home-Bildschirm"
    icon = "home"
    order = AREA_ORDER.get(AREA_ID, 80)

    def __init__(self, deps):
        self.config = deps.config
        self.extra = getattr(deps, "extra", None) or {}

    def pages(self) -> list[str]:
        return [AREA_ID]

    def _cfg(self, key: str):
        return self.config.get(key, DEFAULTS[key])

    def _has_key(self) -> bool:
        value = self._cfg("library.sgdb_key")
        return isinstance(value, str) and bool(value.strip())

    def _sgdb_resting(self) -> int:
        """Sekunden, die SteamGridDB nach Störungen noch ruht; 0 = bereit oder unbekannt (E99)."""
        fn = self.extra.get("art_sources")
        if not callable(fn):
            return 0
        try:
            for source in fn() or []:
                if isinstance(source, dict) and source.get("name") == "sgdb":
                    rest = source.get("resting_s")
                    ok = isinstance(rest, (int, float)) and not isinstance(rest, bool)
                    return int(rest) if ok and rest > 0 else 0
        except Exception:  # noqa: BLE001 - die Seite steht auch ohne diese Angabe
            log.exception("Zustand der Bild-Quellen")
        return 0

    def page(self, page_id: str) -> Page:
        if page_id != AREA_ID:
            raise KeyError(page_id)
        can_fetch = callable(self.extra.get("fetch_all_art"))
        has_key = self._has_key()
        key_note, key_hint = ("Eingetragen" if has_key else "Nicht eingetragen"), SGDB_HINT
        resting = self._sgdb_resting() if has_key else 0
        if resting:
            minutes = -(-resting // 60)                 # aufgerundet: nie „0 Minuten“
            key_note = SGDB_RESTING_NOTE % ("%d Min." % minutes)
            key_hint += " " + SGDB_RESTING_HINT % ("1 Minute" if minutes == 1 else "%d Minuten" % minutes)
        rows = [self._theme_row()]
        rows += [toggle(row_id, label, self._cfg(key), hint=hint, default=DEFAULTS[key])
                 for row_id, (key, label, hint) in _TOGGLES.items()]
        rows += [
            action("home.fetch_all_art", "Cover und Hintergründe laden",
                   enabled=can_fetch, note="" if can_fetch else "Nicht verfügbar",
                   hint="Lädt jetzt die Bilder für alle Spiele und Apps. Eigene Bilder bleiben."),
            # value bleibt leer: Der Schlüssel geht nie an die Oberfläche zurück
            text("home.sgdb_key", "SteamGridDB-Schlüssel", "", secret=True, note=key_note, hint=key_hint),
        ]
        return Page(AREA_ID, self.label, rows)

    def _theme_row(self):
        current = str(self._cfg("ui.theme") or "")
        options = [option("", THEME_STANDARD)]
        options += [option(key, name) for key, (name, _) in themes.BUILTIN.items()]
        options += [option(themes.FILE_PREFIX + name, name, note=THEME_OWN) for name in themes.user_themes()]
        if not any(o["value"] == current for o in options):
            current = ""                                # gelöschtes eigenes Design: es gilt wieder Standard
        return select("home.theme", "Design", current, options, hint=THEME_HINT, default=DEFAULTS["ui.theme"])

    def set(self, row_id: str, value):
        if row_id == "home.theme":
            if not themes.valid(value):
                raise SettingsError("Dieses Design gibt es nicht (mehr).")
            self.config.set("ui.theme", str(value or ""))
            return None
        if row_id in _TOGGLES:
            self.config.set(_TOGGLES[row_id][0], bool(value))
            return None
        if row_id == "home.sgdb_key":
            return self._set_key(value)
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def _set_key(self, value) -> dict:
        key = "".join(str(value or "").split())         # Leerzeichen und Zeilenenden vom Abtippen
        if not key:
            # leere Eingabe = Schlüssel entfernen
            had = self._has_key()
            self.config.set("library.sgdb_key", "")
            return {"message": "Der Schlüssel wurde entfernt." if had else "Kein Schlüssel eingetragen.",
                    "refresh": True}
        if not _KEY.match(key):
            # die Eingabe selbst nie zurückgeben oder protokollieren
            raise SettingsError("Das sieht nicht nach einem SteamGridDB-Schlüssel aus.")
        self.config.set("library.sgdb_key", key)
        return {"message": "Der Schlüssel ist eingetragen.", "refresh": True}

    def action(self, row_id: str, value=None):
        if row_id == "home.fetch_all_art":
            fn = self.extra.get("fetch_all_art")
            if not callable(fn):
                raise SettingsError("Das ist gerade nicht verfügbar.")
            try:
                result = fn()
            except SettingsError:
                raise
            except Exception as e:  # noqa: BLE001
                message = getattr(e, "message", None)
                if isinstance(message, str) and message:
                    raise SettingsError(message) from e
                raise
            result = result if isinstance(result, dict) else {}
            message = str(result.get("message") or "")
            if not message:
                total = result.get("total")
                if result.get("started") is False:
                    message = "Es gibt gerade nichts zu laden."
                elif isinstance(total, int) and not isinstance(total, bool) and total > 0:
                    message = "Bilder für %d Einträge werden geladen." % total
                else:
                    message = "Die Bilder werden geladen."
            return {"message": message}
        if row_id == "home.sgdb_key":
            # Textzeilen dürfen ihre Eingabe auch als Aktion schicken (Vertrag in rows.py)
            return self._set_key(value)
        raise SettingsError("Diese Aktion gibt es nicht.")


def make_area(deps) -> HomeArea:
    return HomeArea(deps)
