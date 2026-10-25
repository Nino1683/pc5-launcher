"""Einstellungen in settings.json: flach, Schlüssel mit Punkten ("audio.ui_volume").

Gespeichert wird nur, was jemand gesetzt hat; alles andere kommt aus der Tabelle DEFAULTS.
Schreiben ist atomar. Eine kaputte Datei wird beiseitegelegt und mit Vorgaben gestartet:
Das Home muss auch mit zerschossenen Einstellungen hochkommen.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Callable

from . import jsontext, log, paths, system

_log = log.get("config")

# Vorgaben; die Namen nach Bauplan E22: EINE Tabelle, jeder Schlüssel genau einmal. Die Bereiche
# der Einstellungen führen ihre Vorgaben zusätzlich als DEFAULTS in ihrem Modul (damit sie auch
# ohne den Kern laufen); tests/wiring/settings prüft, dass beide übereinstimmen. Wer einen
# Baustein baut, meldet seine Schlüssel mit add_defaults() an; was hier steht, gewinnt.
# Die Namen aus der Zeit davor gibt es nicht mehr (tests/wiring/settings/test_keys.py sucht danach).
DEFAULTS: dict[str, Any] = {
    # --- Oberfläche (gehen als sys.state.ui an die Seite, die sie auswertet)
    "ui.reduce_motion": False,
    "ui.clock_24h": True,
    "ui.scale": 1.0,                   # Textgröße: 1.0 | 1.1 | 1.2
    "ui.live_background": True,        # bewegter Hintergrund des Home (E33)
    "ui.language": "",                 # "de" | "en"; leer = nach der Systemsprache (i18n.language, 2026-10-25)
    "ui.intro": True,                  # Startanimation mit Ton beim Start des Home (E92)
    "ui.theme": "",                    # Design (E113): "" Standard | eingebaute Kennung | "file:<name>" (themes.py)
    # --- Ton
    "audio.ui_sounds": True,
    "audio.ui_volume": 0.6,            # Anteil 0..1
    # Klangpaket der Oberfläche: "couch" (eigene Töne) | "pc5" (web/sounds/pc5, E58; nur wenn der
    # Ordner da ist, nie im Paket). E104: Vorgabe "couch" statt "pc5"
    "audio.ui_pack": "couch",
    "audio.sink": "",                  # "" = Fernseher, sobald er da ist; sonst Name der gewählten Senke
    "audio.controller_mic": False,     # Mikrofon des Controllers wird zur Eingabe, sobald es da ist (E75; GuliKit-Dongle: nur Stille)
    "audio.controller_output": False,  # Ton und Vibration über den Ton-Ausgang des Controllers (E75; aus = WirePlumber-Regel)
    # --- Mitteilungen (notify.py)
    "notify.popups": True,             # Mitteilungen einblenden (sonst nur in die Liste)
    "notify.in_game": True,            # auch einblenden, solange ein Spiel oder eine App vorn ist
    "notify.battery": True,            # Akkuwarnung des Controllers
    "notify.seconds": 5,
    "notify.weekly": True,             # E108: Wochenrückblick „Deine Woche …“ montags
    # --- Selbst-Update (E116, selfupdate.py)
    "update.check": True,              # einmal am Tag bei GitHub nach einer neuen Fassung fragen und sie melden
    # --- Shell
    "shell.launch_timeout": 90,        # Sekunden bis "kein Bild" (Bauplan 4.5)
    "shell.resume_delay_ms": 150,      # Wartezeit nach neutralem Pad, bevor ein Spiel weiterläuft
    # --- Spiele und Starter
    "games.freeze": True,              # Spiele anhalten, solange unsere Oberfläche vorn ist
    "games.fsr4": True,                # FSR 4 für Proton-Starts; je Spiel abschaltbar
    "games.hdr": True,                 # DXVK_HDR, solange die Sitzung HDR ausgibt
    "games.pad_auto": True,            # Controller je Spiel als Xbox-Pad oder direkt (E74, launch/proton.controller)
    "games.proton": "",                # "" = automatisch (wiring_settings.resolve_proton)
    "games.prefix_root": "",           # "" = ~/Games/prefixes/couch
    "games.locale": "",                # "" = nichts setzen
    "saves.auto": True,                # Spielstände nach jedem Spielen automatisch sichern (E99, wiring_saves)
    "saves.keep": 5,                   # so viele automatische Sicherungen je Spiel bleiben: 5 | 10 | 20
    "browser.home": "",                # Startseite des Browsers, "" = die eigene (Kacheln)
    "browser.app": "couch",            # "couch" = Browser der Konsole (E59) | "chrome" = Chrome wie bisher
    "browser.search": "",              # Suchadresse mit %s, "" = Google
    # --- Bibliothek und Bilder
    "library.fetch_art": True,         # Cover und Hintergründe im Hintergrund aus dem Netz laden (E34)
    "library.sgdb_key": "",            # eigener SteamGridDB-Schlüssel; steht nie im Protokoll
    "library.games_disk": "",          # E104: Spiele-Ort; "" = automatisch (paths.games_disk: Bestand
                                       # /run/media/<nutzer>/Spiele, neu ~/Games)
    "library.roms": True,              # E105: ROM-Ordner ohne Steam ROM Manager einlesen (Quelle "rom");
                                       # im Bestand setzt portable.migrate einmal False
    # --- Emulatoren (E105)
    "emu.root": "",                    # Emulator-Ordner; "" = EmuDeck-Ordner (emulationPath), sonst <Spiele-Ort>/Emulation
    # --- Store
    "store.itch_client_id": "",        # itch.io (E100): öffentliche Kennung der eigenen OAuth-App, 32 Hex-Zeichen, kein Geheimnis
    # --- Archiv-Installer
    "install.games_root": "",          # "" = die Spiele-Platte
    "install.reserve_gib": 5,
    "install.trash_days": 14,
    # --- Leerlauf (idle.py): Minuten ohne Bedienung am Controller, 0 = nie
    "ui.saver_after": 10,              # Bildschirmschoner im Home und in Spielen
    "power.sleep_after": 60,           # Ruhemodus im Home und in Spielen
    "power.sleep_after_media": 180,    # Ruhemodus, solange YouTube, Kodi oder der Browser vorn ist
    # --- Eingabe (input/service.py)
    "input.idle_off_minutes": 0,       # 0 = nie; sonst trennt das Home ein unbenutztes Bluetooth-Pad
    "input.rumble": True,
    "input.cable_pair": True,          # USB-DualSense koppelt sich per Bluetooth mit diesem PC (E78)
    "input.bt_adapter": "",            # Adresse des Bluetooth-Adapters; "" = der mit einem gekoppelten Controller
    "input.capture_button": "ps-create",   # Bildschirmfoto: "ps-create" (PS halten + Create) | "create" (capture.py)
    "input.lightbar": {"color": "blue", "brightness": 100},     # Form des Bereichs „Zubehör“
    "input.ps_long_ms": 800,           # PS lang
    "input.ps_quit_ms": 3000,          # PS + Options bis zum Schließen
    "input.mouse_speed": 1.0,          # Faktor 0.25 … 4 für den Maus-Modus
    # --- Bild (settings/display.py, gs/focus.py)
    "display.game_resolution": "native",
    "display.scaling_filter": "linear",
    "display.overlay_blur": True,      # Unschärfe hinter dem aktiven Overlay
    "display.force_composition": False,  # Komposition erzwingen, für Sunshine-Streaming (0.3.2)
    "display.hdr": False,
    "display.hdr_confirmed": False,    # HDR wurde einmal am Bildschirm bestätigt (E12)
    "display.sdr_brightness": 200,     # Nits
    "display.vrr": False,
    "display.area_scale": 100,         # Anzeigebereich: Prozent der Bildfläche für Home und Overlay (90..100)
    # --- Leistung (perf.py)
    "perf.overlay": "off",
    "perf.font_scale": "auto",
    "perf.fps_limit": 0,               # nur Teiler der Bildwiederholrate, nur solange eine App vorn ist (E23)
}



class Config:
    def __init__(self, path: str | None = None, defaults: dict[str, Any] | None = None,
                 writer: Any = None):
        self.path = path or paths.settings_file()
        # writer (system.WriteBehind): Das Home schreibt die Datei in einem eigenen Thread, damit
        # das fsync nie die Hauptschleife aufhält. Ohne writer wird sofort geschrieben (Tests).
        self._writer = writer
        self._defaults = dict(DEFAULTS if defaults is None else defaults)
        self._values: dict[str, Any] = {}
        self._observers: list[tuple[str, Callable[[str, Any], None]]] = []
        self._lock = threading.RLock()
        self.broken_copy: str | None = None      # Pfad der Sicherung, falls die Datei kaputt war
        # Die Datei war beim Start da, aber nicht lesbar (Rechte, Plattenfehler): Dann gelten die
        # Vorgaben nur im Speicher und es wird nie gespeichert. Sonst ersetzte das erste Speichern
        # die Einstellungen des Nutzers ohne Sicherung durch die Vorgaben.
        self.read_failed = False
        self._refused_logged = False
        self._load()

    # ------------------------------------------------------------ laden / speichern
    def _load(self) -> None:
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            return
        except OSError as e:
            self.read_failed = True
            _log.error("Einstellungen nicht lesbar (%s): Vorgaben gelten nur bis zum Neustart, "
                       "die Datei bleibt unverändert", e)
            return
        raw, bad_bytes = decode_lenient(blob)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("oberste Ebene ist kein Objekt")
        except (ValueError, RecursionError) as e:
            self._keep_broken(blob)
            _log.error("Einstellungen kaputt (%s); Sicherung: %s; starte mit Vorgaben",
                       e, self.broken_copy)
            return
        if bad_bytes:
            # Einzelne Bytes sind kein UTF-8 (Plattenfehler, mit Latin-1 bearbeitet): Der Rest
            # gilt weiter, die Urfassung bleibt als Sicherung liegen
            self._keep_broken(blob)
            _log.error("Einstellungen enthalten ungültige Zeichen; Sicherung: %s", self.broken_copy)
        self._values = {str(k): v for k, v in data.items()}

    def _keep_broken(self, blob: bytes) -> None:
        self.broken_copy = "%s.kaputt-%s" % (self.path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            system.write_atomic(self.broken_copy, blob)
        except OSError:
            self.broken_copy = None

    def _save(self) -> None:
        if self.read_failed:
            if not self._refused_logged:
                self._refused_logged = True
                _log.error("Einstellungen werden nicht gespeichert: %s war beim Start nicht lesbar", self.path)
            return
        text = jsontext.dumps(self._values, indent=1, sort_keys=True) + "\n"
        if self._writer is not None:
            self._writer.write(self.path, text)
            return
        try:
            system.write_atomic(self.path, text)
        except OSError as e:
            # volle Platte o. Ä.: Der Wert gilt bis zum Neustart weiter, das Home läuft
            _log.error("Einstellungen nicht speicherbar: %s", e)

    # ------------------------------------------------------------ Schnittstelle (interfaces.Config)
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                return copy.deepcopy(self._values[key])
            if key in self._defaults:
                return copy.deepcopy(self._defaults[key])
        return default

    def set(self, key: str, value: Any) -> None:
        json.dumps(value)                       # nicht speicherbare Werte sofort ablehnen
        with self._lock:
            old = self.get(key, _MISSING)
            if key in self._values and self._values[key] == value:
                return
            self._values[key] = copy.deepcopy(value)
            self._save()
            observers = list(self._observers)
        if old == value:
            return                              # gleicher Wert wie die Vorgabe: nichts zu melden
        for prefix, fn in observers:
            if key.startswith(prefix):
                try:
                    fn(key, copy.deepcopy(value))
                except Exception:
                    _log.exception("Beobachter für %s", key)

    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None:
        with self._lock:
            self._observers.append((prefix, fn))

    # ------------------------------------------------------------ Zusätze
    def reset(self, key: str) -> None:
        """Zurück zur Vorgabe."""
        with self._lock:
            if key not in self._values:
                return
            old = self._values.pop(key)
            self._save()
            observers = list(self._observers)
        new = self.get(key)
        if new != old:
            for prefix, fn in observers:
                if key.startswith(prefix):
                    try:
                        fn(key, new)
                    except Exception:
                        _log.exception("Beobachter für %s", key)

    def reset_all(self) -> list[str]:
        """Alles zurück auf die Vorgaben, mit einem einzigen Schreibvorgang. Beobachter erfahren
        jeden Schlüssel, dessen Wert sich dadurch ändert; diese Schlüssel kommen zurück."""
        with self._lock:
            old = self._values
            if not old:
                return []
            self._values = {}
            self._save()
            observers = list(self._observers)
        changed = []
        for key, was in old.items():
            new = self.get(key)
            if new == was:
                continue
            changed.append(key)
            for prefix, fn in observers:
                if key.startswith(prefix):
                    try:
                        fn(key, new)
                    except Exception:
                        _log.exception("Beobachter für %s", key)
        return changed

    def add_defaults(self, table: dict[str, Any]) -> None:
        with self._lock:
            for k, v in table.items():
                self._defaults.setdefault(k, v)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy({**self._defaults, **self._values})


_MISSING = object()


def decode_lenient(blob: bytes) -> tuple[str, bool]:
    """UTF-8 lesen, ohne an einzelnen kaputten Bytes zu scheitern: (text, hatte_kaputte_bytes).
    Eine Datei mit einem falschen Byte darf das Home nicht am Start hindern."""
    try:
        return blob.decode("utf-8"), False
    except UnicodeDecodeError:
        return blob.decode("utf-8", errors="replace"), True
