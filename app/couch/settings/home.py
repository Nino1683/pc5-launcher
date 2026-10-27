"""Einstellungen › Home-Bildschirm: Design (E113), Hintergrund (E121), bewegter Hintergrund (E33),
Zeitformat, Cover und Hintergründe aus dem Netz (E34).

Hintergrund (E121, gallery.py): Spielbilder (wie bisher), ein eigenes Bild oder eine Diashow aus den
Bildschirmfotos und dem Ordner „Hintergründe“ im Bilder-Ordner. Gewählt wird das Bild am Bildschirm in der
Mediengalerie (Bildschirm gallery mit params.pick = "background"); die Zeilen darunter hängen von der Wahl ab.

Startanimation (E130): Eigene (E103) · Video · Aus (ui.intro, ui.intro_style), dazu das Ruhemodus-Video (ui.suspend_video).
Die Videos kommen aus steamdeckrepo.com, gewählt am Bildschirm boot-videos {kind}; laden und den Zustand für die Seite
macht wiring_videos.py, hier werden nur die Angaben der Ablage gelesen und auf Wunsch die eigenen Dateien entfernt.

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
import os
import re

from .. import bootvideos, gallery, paths, themes
from .rows import AREA_ORDER, Page, SettingsError, action, confirm_action, link, option, select, text, toggle

log = logging.getLogger("couch.settings.home")


def _video_folder() -> str:
    """E130: Ablage der Videos (wie wiring_videos.folder; hier nur Angaben lesen und entfernen, nie Netz)."""
    return os.path.join(paths.data_dir(), "videos")


def _video_note(meta: dict | None) -> str:
    """„Titel · 9 s“ des gespeicherten Videos, sonst „Kein Video gewählt“."""
    if not meta:
        return NO_VIDEO
    seconds = meta.get("duration") or 0
    return "%s · %d s" % (meta.get("title") or meta.get("id") or "Video", round(seconds)) if seconds else \
        (meta.get("title") or "Video")

AREA_ID = "home"

DEFAULTS: dict = {
    "ui.theme": "",
    "ui.background": "game",
    "ui.background_image": "",
    "ui.background_slides": "all",
    "ui.background_interval": 60,
    "ui.live_background": True,
    "ui.intro": True,
    "ui.intro_style": "own",           # E130: Startanimation „Eigene“ | „Video“ (steamdeckrepo.com)
    "ui.suspend_video": "off",         # E130: Ruhemodus-Video „Aus“ | „Video“
    "ui.clock_24h": True,
    "library.fetch_art": True,
    "library.sgdb_key": "",
    # E126: Widget-Leiste im Home, je Widget ein Schalter (wiring_widgets.KEYS)
    "ui.widget_storage": True,
    "ui.widget_trophies": True,
    "ui.widget_deals": True,
    "ui.widget_recent": True,
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

# Hintergrund (E121): Werte wie gallery.MODES, SLIDES, INTERVALS
BACKGROUND_CHOICES = (("game", "Spielbilder"), ("image", "Eigenes Bild"), ("slideshow", "Diashow"))
BACKGROUND_HINT = ("Spielbilder: das Bild des gewählten Spiels. Ein eigenes Bild und die Diashow gelten immer, "
                   "auch wenn ein Spiel gewählt ist.")
SLIDES_CHOICES = (("all", "Hintergründe und Bildschirmfotos"), ("wallpapers", "Nur Hintergründe"),
                  ("screenshots", "Nur Bildschirmfotos"))
INTERVAL_CHOICES = ((30, "Alle 30 Sekunden"), (60, "Jede Minute"), (300, "Alle 5 Minuten"), (900, "Alle 15 Minuten"))
PICK_HINT = "Aus deinen Bildschirmfotos oder dem Ordner „Hintergründe“ im Bilder-Ordner."
FOLDER_HINT = "Eigene Bilder hierher kopieren, etwa von einem USB-Stick über den Datei-Explorer."
NO_IMAGE = "Kein Bild gewählt"
IMAGE_GONE = "Bild fehlt"

# Schalter: Zeile -> (Schlüssel, Beschriftung, Erklärung)
_TOGGLES = {
    "home.live_background": (
        "ui.live_background", "Bewegter Hintergrund",
        "Der Hintergrund des Home bewegt sich ruhig. Steht still, sobald ein Spiel läuft."),
    # E130: als Auswahl Eigene · Video · Aus gebaut (_intro_rows), hier nur Platz in der Reihenfolge und Erklärung
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

# Startanimation und Ruhemodus-Video (E130, bootvideos.py, wiring_videos.py): Videos aus steamdeckrepo.com, gewählt am
# Bildschirm boot-videos {kind}; die Zeilen darunter hängen von der Wahl ab
INTRO_CHOICES = (("own", "Eigene"), ("video", "Video"), ("off", "Aus"))
INTRO_VIDEO_HINT = ("Eigene: das Logo des PC5 Launchers mit Ton. Video: ein Startvideo aus der Sammlung von "
                    "steamdeckrepo.com. Jede Taste überspringt es.")
SUSPEND_CHOICES = (("off", "Aus"), ("video", "Video"))
SUSPEND_HINT = ("Ein kurzes Video aus der Sammlung von steamdeckrepo.com, bevor du den Ruhemodus im Ein/Aus-Menü "
                "wählst. Beim Ruhemodus nach Zeit nie.")
VIDEO_PICK_HINT = "Videos der Community von steamdeckrepo.com; die Rechte liegen bei den Erstellern."
NO_VIDEO = "Kein Video gewählt"
VIDEO_REMOVE_TEXT = "Die Datei wird gelöscht. Du kannst jederzeit ein anderes Video wählen."
_VIDEO_ROWS = {"home.intro": "boot", "home.intro_video": "boot", "home.intro_video_remove": "boot",
               "home.suspend_video": "suspend", "home.suspend_video_pick": "suspend",
               "home.suspend_video_remove": "suspend"}

# Widget-Leiste unten im Home (E126): Unterseite „Widgets“, Zeile -> (Schlüssel, Beschriftung, Erklärung)
WIDGETS_PAGE = "home.widgets"
WIDGETS_HINT = "Karten ganz unten im Home, unter dem Hub. Runter führt hinein."
_WIDGETS = {
    "home.widget_storage": ("ui.widget_storage", "Speicherplatz",
                            "Freier Platz auf der Spiele-Platte. Kreuz öffnet „Platz schaffen“."),
    "home.widget_trophies": ("ui.widget_trophies", "Trophäen",
                             "Deine Trophäen aus allen Steam-Spielen. Kreuz öffnet die Übersicht."),
    "home.widget_deals": ("ui.widget_deals", "Angebote der Wunschliste",
                          "Wie viele Spiele deiner Steam-Wunschliste gerade im Angebot sind. Kreuz öffnet den Store."),
    "home.widget_recent": ("ui.widget_recent", "Zuletzt gespielt",
                           "Die letzten Spiele. Kreuz startet das Spiel bzw. holt es nach vorn."),
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
        return [AREA_ID, WIDGETS_PAGE]

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
        if page_id == WIDGETS_PAGE:
            return self._widgets_page()
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
        rows += self._background_rows()
        for row_id, (key, label, hint) in _TOGGLES.items():
            # E130: die Startanimation als Auswahl samt Video-Zeilen an ihrem Platz
            rows += self._intro_rows() if row_id == "home.intro" else [
                toggle(row_id, label, self._cfg(key), hint=hint, default=DEFAULTS[key])]
        rows.append(self._widgets_link())
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

    def _background_rows(self) -> list:
        """Hintergrund (E121): die Wahl und, je nach Wahl, Bild bzw. Diashow und der Ordner."""
        mode = gallery.mode_of(self.config)
        rows = [select("home.background", "Hintergrund", mode, [option(v, l) for v, l in BACKGROUND_CHOICES],
                       hint=BACKGROUND_HINT, default=DEFAULTS["ui.background"])]
        if mode == "image":
            image = gallery.image_of(self.config)
            hit = gallery.checked(image) if image else None
            note = gallery.describe(hit[0]) if hit else (IMAGE_GONE if image else NO_IMAGE)
            rows.append(action("home.background_pick", "Bild auswählen", note=note, hint=PICK_HINT))
        elif mode == "slideshow":
            slides = gallery.slides_of(self.config)
            n = gallery.count(gallery.slide_sources(slides))
            rows.append(select("home.background_slides", "Bilder der Diashow", slides,
                               [option(v, l) for v, l in SLIDES_CHOICES], default=DEFAULTS["ui.background_slides"],
                               note="Keine Bilder" if not n else "1 Bild" if n == 1 else "%d Bilder" % n))
            rows.append(select("home.background_interval", "Bildwechsel", gallery.interval_of(self.config),
                               [option(v, l) for v, l in INTERVAL_CHOICES], default=DEFAULTS["ui.background_interval"]))
        if mode != "game":
            folder = os.path.basename(gallery.wallpaper_folder())
            rows.append(action("home.background_folder", "Ordner „%s“ öffnen" % folder, hint=FOLDER_HINT))
        return rows

    def _set_background(self, row_id: str, value):
        if row_id == "home.background":
            if value not in gallery.MODES:
                raise SettingsError("Diesen Hintergrund gibt es nicht.")
            self.config.set("ui.background", value)
            out = {"refresh": True}
            if value == "image" and gallery.checked(gallery.image_of(self.config)) is None:
                out.update(ui="gallery", params={"pick": "background"})     # gleich ein Bild wählen
            return out
        if row_id == "home.background_slides":
            if value not in gallery.SLIDES:
                raise SettingsError("Diese Auswahl gibt es nicht.")
            self.config.set("ui.background_slides", value)
            return {"refresh": True}
        if isinstance(value, bool) or value not in gallery.INTERVALS:
            raise SettingsError("Diese Zeit gibt es nicht.")
        self.config.set("ui.background_interval", int(value))
        return None

    def set(self, row_id: str, value):
        if row_id in ("home.background", "home.background_slides", "home.background_interval"):
            return self._set_background(row_id, value)
        if row_id == "home.theme":
            if not themes.valid(value):
                raise SettingsError("Dieses Design gibt es nicht (mehr).")
            self.config.set("ui.theme", str(value or ""))
            return None
        if row_id in ("home.intro", "home.suspend_video"):
            return self._set_video_choice(row_id, value)        # E130
        if row_id in _TOGGLES:
            self.config.set(_TOGGLES[row_id][0], bool(value))
            return None
        if row_id in _WIDGETS:
            self.config.set(_WIDGETS[row_id][0], bool(value))
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
        if row_id == "home.background_pick":
            return {"ui": "gallery", "params": {"pick": "background"}}
        if row_id == "home.background_folder":
            try:
                folder = gallery.ensure_wallpaper_folder()
            except OSError as e:
                raise SettingsError("Der Ordner ließ sich nicht anlegen.") from e
            return {"ui": "explorer", "params": {"start": folder}}
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
        if row_id in ("home.intro_video", "home.suspend_video_pick"):
            return {"ui": "boot-videos", "params": {"kind": _VIDEO_ROWS[row_id]}}     # E130: Auswahl am Bildschirm
        if row_id in ("home.intro_video_remove", "home.suspend_video_remove"):
            return self._remove_video(_VIDEO_ROWS[row_id])
        raise SettingsError("Diese Aktion gibt es nicht.")

    # ------------------------------------------------------------ Startanimation und Ruhemodus-Video (E130)
    def _intro_style(self) -> str:
        if self._cfg("ui.intro") is False:
            return "off"
        style = self._cfg("ui.intro_style")
        return style if style in ("own", "video") else "own"

    def _intro_rows(self) -> list:
        """Startanimation: Eigene · Video · Aus; mit „Video“ das gewählte Video (Auswahl am Bildschirm, entfernen).
        Darunter das Ruhemodus-Video: Aus · Video, ebenso."""
        style = self._intro_style()
        rows = [select("home.intro", "Startanimation", style, [option(v, l) for v, l in INTRO_CHOICES],
                       hint=INTRO_VIDEO_HINT, default="own")]
        boot = bootvideos.Repo(_video_folder(), "", base=None).meta("boot")
        if style == "video":
            rows.append(action("home.intro_video", "Startvideo", note=_video_note(boot), hint=VIDEO_PICK_HINT))
            if boot:
                rows.append(confirm_action("home.intro_video_remove", "Startvideo entfernen", "Startvideo entfernen?",
                                           VIDEO_REMOVE_TEXT, ok="Entfernen"))
        on = self._cfg("ui.suspend_video") == "video"
        rows.append(select("home.suspend_video", "Ruhemodus-Video", "video" if on else "off",
                           [option(v, l) for v, l in SUSPEND_CHOICES], hint=SUSPEND_HINT,
                           default=DEFAULTS["ui.suspend_video"]))
        if on:
            sleep = bootvideos.Repo(_video_folder(), "", base=None).meta("suspend")
            rows.append(action("home.suspend_video_pick", "Video vor dem Ruhemodus", note=_video_note(sleep),
                               hint=VIDEO_PICK_HINT))
            if sleep:
                rows.append(confirm_action("home.suspend_video_remove", "Ruhemodus-Video entfernen",
                                           "Ruhemodus-Video entfernen?", VIDEO_REMOVE_TEXT, ok="Entfernen"))
        return rows

    def _set_video_choice(self, row_id: str, value):
        """Auswahl der Startanimation bzw. des Ruhemodus-Videos. „Video“ ohne gespeichertes Video öffnet gleich die
        Auswahl am Bildschirm (wie „Eigenes Bild“ beim Hintergrund)."""
        kind = _VIDEO_ROWS[row_id]
        if kind == "boot":
            if value not in ("own", "video", "off"):
                raise SettingsError("Diese Startanimation gibt es nicht.")
            self.config.set("ui.intro", value != "off")
            if value != "off":
                self.config.set("ui.intro_style", value)
        else:
            if value not in ("off", "video"):
                raise SettingsError("Diese Auswahl gibt es nicht.")
            self.config.set("ui.suspend_video", value)
        out = {"refresh": True}
        if value == "video" and bootvideos.Repo(_video_folder(), "", base=None).meta(kind) is None:
            out.update(ui="boot-videos", params={"kind": kind})
        return out

    def _remove_video(self, kind: str) -> dict:
        gone = bootvideos.Repo(_video_folder(), "", base=None).remove(kind)
        if kind == "boot":
            self.config.set("ui.intro_style", "own")
        else:
            self.config.set("ui.suspend_video", "off")
        message = ("Das Startvideo wurde entfernt." if kind == "boot" else "Das Ruhemodus-Video wurde entfernt.") \
            if gone else "Es ist kein Video gespeichert."
        return {"message": message, "refresh": True}

    # ------------------------------------------------------------ Widgets (E126)
    def _widgets_link(self):
        on = sum(1 for key, _label, _hint in _WIDGETS.values() if self._cfg(key))
        note = "Alle an" if on == len(_WIDGETS) else "Alle aus" if not on else "%d von %d an" % (on, len(_WIDGETS))
        return link(WIDGETS_PAGE, "Widgets", WIDGETS_PAGE, note=note, hint=WIDGETS_HINT)

    def _widgets_page(self) -> Page:
        rows = [toggle(row_id, label, self._cfg(key), hint=hint, default=DEFAULTS[key])
                for row_id, (key, label, hint) in _WIDGETS.items()]
        return Page(WIDGETS_PAGE, "Widgets", rows)


def make_area(deps) -> HomeArea:
    return HomeArea(deps)
