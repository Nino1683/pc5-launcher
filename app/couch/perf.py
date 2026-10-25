"""Leistungsanzeige (mangoapp), Bildratenbegrenzung und Spielauflösung.

Eigenständiger Dienst ohne GLib. Der Verdrahter erzeugt ihn einmal:

    perf = PerfService(config, gs, run_write=wiring.write3)    # (rc, out, err); mangoapp-Datei aus $MANGOHUD_CONFIGFILE
    perf.apply_on_start()                     # beim Home-Start
    perf.apply_for_app(entry)                 # eine App kommt nach vorn
    perf.restore_global()                     # das Home ist wieder vorn / die App ist beendet
    perf.get() / perf.set(overlay=..., fpsLimit=..., resolution=...)     # RPC perf.get / perf.set

Blockiert höchstens kurz: eine kleine Datei auf tmpfs, X-Eigenschaften über gs und, nur wenn sich
das Limit ändert, ein Aufruf von gamescopectl (3 ms gemessen, aber ein Unterprozess). Im Home
laufen deshalb alle Aufrufe in EINEM eigenen Arbeits-Thread der Verdrahtung (wiring_settings,
Reihenfolge bleibt erhalten); gs und config sind dort Stellvertreter, die in die Hauptschleife
reichen. Die Hauptschleife selbst ruft nur fps_choices()/refresh_rate() (ohne Sperre).

Wie es zusammenhängt:
- mangoapp läuft in der Sitzung in einer Schleife und liest $MANGOHUD_CONFIGFILE neu, sobald die
  Datei überschrieben wird. Die Sitzung setzt sie bei jedem Start auf "no_display" zurück (05, 9).
- Bildratenbegrenzung: Die Wurzel-Eigenschaft GAMESCOPE_FPS_LIMIT (01, 2.7) begrenzt in diesem
  Gamescope (3.16.31-ogc2+) NICHT, sobald eine App vorn ist: Im unsichtbaren Gamescope lief ein
  Vulkan-Client damit unverändert mit 60 Bildern. Es wirkt `gamescopectl debug_set_fps_limit N`:
  30 -> 30 Bilder, 20 -> 20, 40 bei 60 Hz -> keine Wirkung (nur ganzzahlige Teiler). Deshalb
  geht das Limit über run_write an gamescopectl; ohne run_write gibt es kein Limit. Gemessen in
  tests/features/settings-display/e2e_headless.py; am Fernseher noch nicht.
- Das Limit bremst alle Fenster außer Overlays, also auch unser Home. Deshalb gilt es nur,
  solange eine App vorn ist.
"""
from __future__ import annotations

import logging
import os
import stat
import threading
from typing import Any, Callable

log = logging.getLogger("couch.perf")

# Stufe -> Inhalt der mangoapp-Datei (alle vier im unsichtbaren Gamescope im fertigen Bild gesehen)
MANGO_LEVELS = {
    "off": "no_display\n",
    "fps": "preset=1\n",          # nur die Bildrate
    "bar": "preset=2\n",          # eine Zeile: Bildrate, Frametime, CPU, GPU, RAM, VRAM
    "detailed": "preset=3\n",     # Kasten mit GPU/CPU/VRAM/RAM/Bildrate/Frametime
}
OVERLAY_LEVELS = tuple(MANGO_LEVELS)
OVERLAY_LABELS = {"off": "Aus", "fps": "Nur Bildrate", "bar": "Leiste", "detailed": "Ausführlich"}

# Schriftgröße der Anzeige: mangoapps Vorgabe ist für einen Monitor gemacht, in 4K zu klein
FONT_SCALES = {"small": 1.0, "medium": 1.5, "large": 2.0, "xlarge": 2.5}
FONT_SCALE_LABELS = {"auto": "Automatisch", "small": "Klein", "medium": "Mittel", "large": "Groß",
                     "xlarge": "Sehr groß"}

FPS_MIN = 20                      # kleinste angebotene Grenze; angeboten wird jeder ganzzahlige Teiler der Bildwiederholrate ab hier
RESOLUTIONS = {"native": None, "1440p": (2560, 1440), "1080p": (1920, 1080)}
RESOLUTION_LABELS = {"native": "Wie der Bildschirm", "1440p": "1440p", "1080p": "1080p"}

GAMESCOPECTL = "gamescopectl"
FPS_LIMIT_COMMAND = "debug_set_fps_limit"
ATOM_LIMITER_FEEDBACK = "GAMESCOPE_LIMITER_FEEDBACK"     # 1, solange Gamescope begrenzt (nur lesen)
ATOM_REFRESH = "GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK"
ATOM_MODE_CONTROL = "GAMESCOPE_XWAYLAND_MODE_CONTROL"
GAME_SERVER = 1                   # Xwayland 1 trägt die Spiele (E4)

DEFAULTS = {
    "perf.overlay": "off",
    "perf.font_scale": "auto",
    "perf.fps_limit": 0,
    "display.game_resolution": "native",
}


def mango_text(level: str, font_scale: float | None = None) -> str:
    """Inhalt der mangoapp-Datei für eine Stufe. Unbekannte Stufe: ValueError."""
    if level not in MANGO_LEVELS:
        raise ValueError("Unbekannte Stufe der Leistungsanzeige: %r" % (level,))
    text = MANGO_LEVELS[level]
    if level != "off" and font_scale and abs(font_scale - 1.0) > 0.01:
        text += "font_scale=%s\n" % (("%.2f" % font_scale).rstrip("0").rstrip("."))
    return text


def auto_font_scale(root_height: int) -> float:
    """Schrift passend zur Bildhöhe: 1080p = 1, 4K = 2 (in halben Schritten, 1 … 3)."""
    if not root_height:
        return 1.0
    return min(3.0, max(1.0, round(root_height / 1080.0 * 2) / 2))


def fps_choices(refresh_hz: int) -> list[int]:
    """Erlaubte Grenzen: 0 (aus) und jeder ganzzahlige Teiler der Bildwiederholrate ab FPS_MIN.

    Ohne VRR lässt Gamescope nur jedes n-te Bild durch; 40 bei 60 Hz bliebe wirkungslos. Bis 0.3.0 gab es nur
    30, 40 und 60, bei 144 Hz also gar nichts; seit 0.3.1 etwa 120 Hz -> 20, 24, 30, 40, 60, 120 und
    144 Hz -> 24, 36, 48, 72, 144.
    """
    hz = int(refresh_hz or 60)
    return [0] + [n for n in range(FPS_MIN, hz + 1) if hz % n == 0]


def resolution_size(choice: str, root: tuple[int, int]) -> tuple[int, int]:
    """Auswahl -> (Breite, Höhe) für das Spiel-Xwayland; "native" = Größe der Ausgabe."""
    size = RESOLUTIONS.get(choice)
    return tuple(size) if size else (int(root[0]), int(root[1]))


class PerfService:
    def __init__(self, config, gs, mango_config_path: str | None = None, run_read: Callable | None = None,
                 run_write: Callable | None = None, apply_display: Callable[[], Any] | None = None,
                 environ: dict | None = None):
        """config: get/set nach interfaces.Config. gs: get/set nach interfaces.Gamescope.

        mango_config_path: Datei, die mangoapp liest. None = $MANGOHUD_CONFIGFILE der Sitzung;
        fehlt auch die, bleibt die Anzeige still unberührt.
        run_write(argv, timeout) -> (rc, stdout, stderr): führt gamescopectl für das Limit aus
        (im Prüfmodus des Kerns nur protokolliert). Ohne run_write gibt es kein Limit.
        apply_display: Rückruf des Kerns, der die gespeicherte Spielauflösung anwendet; ohne ihn
        schreibt der Dienst GAMESCOPE_XWAYLAND_MODE_CONTROL selbst.
        run_read: wird derzeit nicht gebraucht (nur damit der Aufruf des Verdrahters passt).
        """
        env = os.environ if environ is None else environ
        self.config = config
        self.gs = gs
        self.mango_path = mango_config_path or env.get("MANGOHUD_CONFIGFILE") or None
        self.run_read = run_read
        self._run_write = run_write
        self._apply_display = apply_display
        self._lock = threading.RLock()
        self._app: dict | None = None     # {"fps", "overlay"} solange eine App vorn ist
        self._limit_applied: int | None = None    # zuletzt an Gamescope gemeldetes Limit

    # --- lesen ----------------------------------------------------------------------------

    def _cfg(self, key: str):
        return self.config.get(key, DEFAULTS[key])

    def refresh_rate(self) -> int:
        """Bildwiederholrate der Ausgabe laut Gamescope; 60, wenn nichts gemeldet ist."""
        try:
            v = self.gs.get(ATOM_REFRESH)
            return int(v[0]) if v and v[0] else 60
        except Exception:  # noqa: BLE001 - eine fehlende Rückmeldung darf nichts kippen
            return 60

    def fps_choices(self) -> list[int]:
        return fps_choices(self.refresh_rate())

    def overlay_available(self) -> bool:
        """True, wenn es eine mangoapp-Datei gibt, in die geschrieben werden kann."""
        return self._mango_file_ok()

    def limit_available(self) -> bool:
        """True, wenn das Limit an Gamescope gemeldet werden kann (run_write eingesteckt)."""
        return self._run_write is not None

    def limiter_active(self) -> bool:
        """Gamescopes eigene Rückmeldung, ob gerade begrenzt wird."""
        try:
            v = self.gs.get(ATOM_LIMITER_FEEDBACK)
            return bool(v and v[0])
        except Exception:  # noqa: BLE001
            return False

    def _global_overlay(self) -> str:
        level = self._cfg("perf.overlay")
        return level if level in MANGO_LEVELS else DEFAULTS["perf.overlay"]

    def _valid_limit(self, value) -> int:
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return n if n in self.fps_choices() else 0

    def _global_limit(self) -> int:
        return self._valid_limit(self._cfg("perf.fps_limit"))

    def _resolution(self) -> str:
        r = self._cfg("display.game_resolution")
        return r if r in RESOLUTIONS else DEFAULTS["display.game_resolution"]

    def font_scale(self) -> float:
        choice = self._cfg("perf.font_scale")
        if choice in FONT_SCALES:
            return FONT_SCALES[choice]
        try:
            return auto_font_scale(self.gs.root_size()[1])
        except Exception:  # noqa: BLE001
            return 1.0

    def get(self) -> dict:
        """{"overlay", "fpsLimit", "resolution"} plus Auswahllisten für die Oberfläche.

        Ist eine App vorn, sind overlay/fpsLimit die gerade wirksamen Werte (mit den Werten je
        Spiel), sonst die gespeicherten globalen.
        """
        with self._lock:
            app = self._app
            choice = self._cfg("perf.font_scale")
            return {
                "overlay": app["overlay"] if app else self._global_overlay(),
                "fpsLimit": app["fps"] if app else self._global_limit(),
                "resolution": self._resolution(),
                "fontScale": choice if choice in FONT_SCALE_LABELS else DEFAULTS["perf.font_scale"],
                "refresh": self.refresh_rate(),
                "overlays": list(OVERLAY_LEVELS),
                "fpsLimits": self.fps_choices(),
                "resolutions": list(RESOLUTIONS),
                "overlayAvailable": self.overlay_available(),
                "limitAvailable": self.limit_available(),
                "limiterActive": self.limiter_active(),
                "appActive": app is not None,
            }

    # --- schreiben ------------------------------------------------------------------------

    def set(self, overlay: str | None = None, fpsLimit: int | None = None,
            resolution: str | None = None, fontScale: str | None = None) -> dict:
        """Speichert die globalen Werte und wendet sie an. Liefert get(). ValueError bei Unsinn.

        Ist gerade eine App vorn, gilt der neue Wert sofort auch für sie (er übersteuert ihren
        eigenen Wert, bis sie das nächste Mal nach vorn kommt).
        """
        with self._lock:
            if overlay is not None and overlay not in MANGO_LEVELS:
                raise ValueError("Diese Stufe der Leistungsanzeige gibt es nicht.")
            if fontScale is not None and fontScale not in FONT_SCALE_LABELS:
                raise ValueError("Diese Schriftgröße gibt es nicht.")
            if resolution is not None and resolution not in RESOLUTIONS:
                raise ValueError("Diese Auflösung gibt es nicht.")
            if fpsLimit is not None:
                try:
                    fpsLimit = int(fpsLimit)
                except (TypeError, ValueError):
                    raise ValueError("Diese Bildratenbegrenzung gibt es nicht.") from None
                if fpsLimit not in self.fps_choices():
                    raise ValueError("Diese Bildratenbegrenzung passt nicht zu %d Hz." % self.refresh_rate())

            if fontScale is not None:
                self.config.set("perf.font_scale", fontScale)
            if overlay is not None:
                self.config.set("perf.overlay", overlay)
                if self._app is not None:
                    self._app["overlay"] = overlay
            if overlay is not None or fontScale is not None:
                self._write_overlay(self._app["overlay"] if self._app else self._global_overlay())
            if fpsLimit is not None:
                self.config.set("perf.fps_limit", fpsLimit)
                if self._app is not None:
                    self._app["fps"] = fpsLimit
                    self._write_limit(fpsLimit)
            if resolution is not None:
                self.config.set("display.game_resolution", resolution)
                self.apply_resolution()
            return self.get()

    def apply_on_start(self) -> None:
        """Beim Home-Start: gespeicherte Stufe in die mangoapp-Datei (die Sitzung hat sie auf
        "no_display" zurückgesetzt), Limit aus, weil das Home vorn ist."""
        with self._lock:
            self._limit_applied = None        # Zustand von Gamescope unbekannt (z. B. nach Steam)
            self.restore_global()

    def apply_for_app(self, entry) -> None:
        """Eine App ist vorn: ihre Werte aus entry.options (fps_limit, perf_overlay), sonst die globalen."""
        with self._lock:
            fps = self._valid_limit(_option(entry, "fps_limit")) or self._global_limit()
            overlay = _option(entry, "perf_overlay")
            if overlay not in MANGO_LEVELS:
                overlay = self._global_overlay()
            self._app = {"fps": fps, "overlay": overlay}
            self._write_overlay(overlay)
            self._write_limit(fps)

    def restore_global(self) -> None:
        """Keine App mehr vorn: globale Stufe der Anzeige, Limit aus (es bremste sonst das Home)."""
        with self._lock:
            self._app = None
            self._write_overlay(self._global_overlay())
            self._write_limit(0)

    def refresh(self) -> None:
        """Nach einem Größenwechsel der Ausgabe: die Anzeige mit der dazu passenden Schrift neu
        schreiben („Automatisch“ richtet sich nach der Bildhöhe) und das Limit auf die neue
        Bildwiederholrate abstimmen (ein Wert, der sie nicht mehr teilt, fällt auf 0)."""
        with self._lock:
            app = self._app
            self._write_overlay(app["overlay"] if app else self._global_overlay())
            if app is not None:
                app["fps"] = self._valid_limit(app["fps"])
                self._write_limit(app["fps"])

    def app_active(self) -> bool:
        """True, solange die Werte einer App gelten (zwischen apply_for_app und restore_global)."""
        return self._app is not None

    def apply_resolution(self) -> None:
        """Gespeicherte Spielauflösung anwenden: über den Kern, sonst direkt an Xwayland 1."""
        if self._apply_display is not None:
            self._apply_display()
            return
        try:
            w, h = resolution_size(self._resolution(), self.gs.root_size())
            if w > 0 and h > 0:
                self.gs.set(ATOM_MODE_CONTROL, [GAME_SERVER, w, h, 0])
        except Exception:  # noqa: BLE001
            log.exception("Spielauflösung ließ sich nicht setzen")

    # --- intern ---------------------------------------------------------------------------

    def _write_limit(self, fps: int) -> bool:
        fps = int(fps)
        if self._run_write is None:
            return False
        if self._limit_applied == fps:
            return True                       # nichts zu tun: kein Unterprozess
        try:
            rc, _out, err = self._run_write([GAMESCOPECTL, FPS_LIMIT_COMMAND, str(fps)], timeout=5)
        except Exception:  # noqa: BLE001
            log.exception("Bildratenbegrenzung ließ sich nicht setzen")
            return False
        if rc != 0:
            # nicht merken: beim nächsten Wechsel noch einmal versuchen
            log.warning("gamescopectl %s %d: Rückgabe %s %s", FPS_LIMIT_COMMAND, fps, rc, (err or "").strip())
            return False
        self._limit_applied = fps
        return True

    def _mango_file_ok(self) -> bool:
        if not self.mango_path:
            return False
        try:
            st = os.lstat(self.mango_path)
        except OSError:
            return False
        # Nur die Datei der Sitzung überschreiben: nie neu anlegen, nie einem Verweis folgen.
        return stat.S_ISREG(st.st_mode)

    def _write_overlay(self, level: str) -> bool:
        if not self._mango_file_ok():
            return False
        text = mango_text(level, self.font_scale())
        try:
            with open(self.mango_path, "r", encoding="utf-8", errors="replace") as f:
                if f.read() == text:
                    return True               # unverändert: mangoapp nicht unnötig neu laden lassen
            # An Ort und Stelle überschreiben, nicht umbenennen: mangoapp beobachtet diese Datei.
            with open(self.mango_path, "w", encoding="utf-8") as f:
                f.write(text)
            return True
        except OSError:
            log.exception("mangoapp-Datei %s ließ sich nicht schreiben", self.mango_path)
            return False


def _option(entry, key: str):
    """Option eines Bibliothekseintrags; verträgt Entry, dict und None."""
    if entry is None:
        return None
    if hasattr(entry, "option"):
        return entry.option(key)
    opts = entry.get("options", {}) if isinstance(entry, dict) else getattr(entry, "options", {})
    return (opts or {}).get(key)
