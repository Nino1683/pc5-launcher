"""Einstellungen › Bildschirm und Video.

Der Ausgabemodus des Fernsehers ist in Version 1 fest (E20): Er wird nur angezeigt. Geschaltet wird
zur Laufzeit über Eigenschaften an der Gamescope-Wurzel (01 Abschnitt 2.7, Namen stehen auch im
installierten gamescope): HDR, Helligkeit von SDR-Inhalten, VRR, Skalierungsfilter. ITM bleibt
immer aus. Eine Zeile für die Schärfe gibt es nicht: Dafür meldet Gamescope nichts zurück.

Stand der Belege (tests/features/settings-display/e2e_headless.py): Skalierungsfilter und
Spielauflösung wirken im unsichtbaren Gamescope nachweislich. HDR, SDR-Helligkeit und VRR nimmt
Gamescope dort nur an; ob sie wirken, zeigt erst der Fernseher.

HDR und VRR laufen über die HDMI-over-LAN-Strecke, die noch niemand damit gesehen hat. Beide
schalten deshalb mit Probezeit ein: Das Modul wendet an, stellt selbst eine Uhr und nimmt die
Änderung zurück, wenn keine Bestätigung kommt. Das hängt nicht an der Oberfläche: Ist das Bild
schwarz, sieht niemand eine Rückfrage.

Für den Verdrahter, zusätzlich zum Vertrag in rows.py:
    area = make_area(deps)
    area.apply_on_start()     # beim Home-Start und nach einem Wechsel der Ausgabe
    area.hdr_state()          # {"supported", "enabled", "active"} für sys.state
    area.refresh()            # Fernseher-Daten neu lesen (blockiert einige Millisekunden)
deps.extra: "apply_display" (Rückruf: gespeicherte Spielauflösung anwenden), "perf" (PerfService),
"connector" (Anschluss des Fernsehers; fehlt er, drm_modes.tv_connector(), E104; "" = kein Fernseher
erkannt), "drm_summary" (Rückruf statt drm_modes.summary, für Tests).
"""
from __future__ import annotations

import logging
import math
import struct
import threading
import time

from .. import perf as perf_mod
from . import drm_modes
from .rows import (AREA_ORDER, TRIAL_KEEP, TRIAL_REVERT, Page, SettingsError, action, info, option,
                   select, slider, toggle)

log = logging.getLogger("couch.settings.display")

AREA_ID = "display"

ATOM_HDR_ENABLED = "GAMESCOPE_DISPLAY_HDR_ENABLED"
ATOM_HDR_SUPPORTED = "GAMESCOPE_DISPLAY_SUPPORTS_HDR"
ATOM_SDR_BRIGHTNESS = "GAMESCOPE_SDR_ON_HDR_CONTENT_BRIGHTNESS"   # float-Bits in einem CARDINAL
ATOM_ITM = "GAMESCOPE_HDR_ITM_ENABLE"
ATOM_VRR_ENABLED = "GAMESCOPE_VRR_ENABLED"
ATOM_VRR_CAPABLE = "GAMESCOPE_VRR_CAPABLE"
ATOM_REFRESH = "GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK"
ATOM_SCALING_FILTER = "GAMESCOPE_NEW_SCALING_FILTER"
# Komposition erzwingen (0.3.2, Rückmeldung auf Reddit: Sunshine streamt sonst nur Schwarz, mit Ton und Steuerung;
# in Steam Big Picture half „Force composition“ in den Entwickler-Einstellungen). Dieselbe Wurzel-Eigenschaft
# schreibt Steam; Gamescope zeichnet dann jedes Bild selbst, statt Ebenen direkt auszugeben (kein Scanout).
ATOM_COMPOSITE_FORCE = "GAMESCOPE_COMPOSITE_FORCE"

# Auswahl -> Wert der Eigenschaft (0 linear, 2 FSR). Im unsichtbaren Gamescope gesehen: Mit 2
# meldet GAMESCOPE_FSR_FEEDBACK 1, sobald ein Spiel kleiner als die Ausgabe gerechnet wird.
# Vorgabe ist Gamescopes eigene (linear): ungefragt ändert das Home am Bild der Spiele nichts.
SCALING_FILTERS = {"linear": 0, "fsr": 2}
SCALING_LABELS = {"linear": "Weich (linear)", "fsr": "Scharf (FSR 1)"}

TRIAL_SECONDS = 15
OFF_RETRIES = 5              # Versuche, nach einer Probezeit auszuschalten
OFF_RETRY_SECONDS = 2.0
SDR_MIN, SDR_MAX, SDR_STEP = 100, 400, 10
DRM_CACHE_SECONDS = 30.0

DEFAULTS = {
    "display.hdr": False,
    "display.hdr_confirmed": False,      # HDR wurde auf diesem Rechner einmal am Bildschirm bestätigt
    "display.sdr_brightness": 200,       # Nits; Gamescopes eigene Vorgabe ist 203
    "display.vrr": False,
    "display.game_resolution": perf_mod.DEFAULTS["display.game_resolution"],
    "display.scaling_filter": "linear",
    "display.overlay_blur": True,
    "display.force_composition": False,
    "display.area_scale": 100,
}
AREA_MIN, AREA_MAX = 90, 100        # Anzeigebereich in Prozent der Bildfläche (Home und Overlay)


def area_percent(raw) -> int:
    """Gespeicherten Anzeigebereich lesen: ganze Prozent in AREA_MIN..AREA_MAX, Unsinn = 100."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return AREA_MAX
    return int(min(AREA_MAX, max(AREA_MIN, round(raw))))

# Zeilen mit Probezeit: Eigenschaft, Schlüssel und Texte
_TRIALS = {
    "display.hdr": {
        "atom": ATOM_HDR_ENABLED, "key": "display.hdr", "confirmed": "display.hdr_confirmed",
        "name": "HDR",
    },
    "display.vrr": {
        "atom": ATOM_VRR_ENABLED, "key": "display.vrr", "confirmed": None,
        "name": "VRR",
    },
}


def f2u(value: float) -> int:
    """Gamescope liest manche Werte als float-Bits in einem CARDINAL (400.0 -> 1137180672)."""
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


def _later(scheduler, seconds: float, fn):
    # Der Vertrag in rows.py nennt die Methode later(); ältere Beschreibungen call_later().
    call = getattr(scheduler, "later", None) or getattr(scheduler, "call_later")
    return call(seconds, fn)


class DisplayArea:
    id = AREA_ID
    label = "Bildschirm und Video"
    icon = "screen"
    order = AREA_ORDER.get(AREA_ID, 20)

    def __init__(self, deps):
        self.deps = deps
        self.config = deps.config
        self.gs = deps.gs
        extra = getattr(deps, "extra", None) or {}
        # E104: kein fester Anschluss mehr; "" heißt „Kein Fernseher erkannt“ (dann wird nichts gelesen)
        self._connector = str(extra["connector"] or "") if "connector" in extra else drm_modes.tv_connector()
        self._read_drm = extra.get("drm_summary") or (lambda: drm_modes.summary(self._connector))
        self._perf = extra.get("perf") or perf_mod.PerfService(
            deps.config, deps.gs, apply_display=extra.get("apply_display"))
        self._lock = threading.RLock()
        self._pending: dict[str, object] = {}     # Zeile -> Griff der laufenden Probezeit
        self._drm: dict | None = None
        self._drm_at = 0.0

    # --- Vertrag --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return [AREA_ID]

    def page(self, page_id: str) -> Page:
        """Blockiert beim ersten Aufruf einige Millisekunden (Fernseher-Daten), danach aus dem Zwischenspeicher."""
        if page_id != AREA_ID:
            raise KeyError(page_id)
        s = self._summary()
        rows = self._info_rows(s)
        rows += self._hdr_rows(s)
        rows.append(self._vrr_row(s))
        rows.append(select(
            "display.game_resolution", "Spielauflösung", self._game_resolution(),
            [option(k, perf_mod.RESOLUTION_LABELS[k], "%d × %d" % v if v else "")
             for k, v in perf_mod.RESOLUTIONS.items()],
            hint="Niedrigere Auflösungen werden auf den Bildschirm hochgerechnet; das bringt mehr "
                 "Bilder pro Sekunde.", default=DEFAULTS["display.game_resolution"]))
        rows.append(select(
            "display.scaling_filter", "Skalierung kleinerer Auflösungen", self._scaling_filter(),
            [option(k, SCALING_LABELS[k]) for k in SCALING_FILTERS],
            hint="Wirkt, wenn ein Spiel mit weniger Bildpunkten rechnet, als der Bildschirm hat.",
            default=DEFAULTS["display.scaling_filter"]))
        rows.append(toggle(
            "display.overlay_blur", "Unschärfe hinter dem Control Center",
            self._cfg("display.overlay_blur"),
            hint="Zeichnet das Spiel weich, solange das Control Center darüber liegt.",
            default=DEFAULTS["display.overlay_blur"]))
        rows.append(toggle(
            "display.force_composition", "Komposition erzwingen",
            bool(self._cfg("display.force_composition")),
            hint="Für Streaming mit Sunshine und ähnlichen Programmen: Kommt dort nur ein schwarzes Bild an, "
                 "hilft das meist. Kostet etwas Leistung.",
            default=DEFAULTS["display.force_composition"]))
        # Der Anzeigebereich wirkt nur auf die Oberfläche der Konsole (die Seite zeichnet sich so
        # viel kleiner, settings.set {id: display.area, value: Prozent}); Spiele sehen nichts davon
        percent = area_percent(self._cfg("display.area_scale"))
        rows.append(action(
            "display.area", "Anzeigebereich", value=percent, min=AREA_MIN, max=AREA_MAX, step=1, unit="%",
            default=DEFAULTS["display.area_scale"], note="" if percent == AREA_MAX else "%d %%" % percent,
            hint="Zeigt vier Marken in den Ecken. Siehst du nicht alle vollständig, schalte am "
                 "Fernseher den Overscan aus (Bildformat „Original“ oder „Punkt für Punkt“) oder "
                 "verkleinere hier die Oberfläche der Konsole."))
        return Page(AREA_ID, self.label, rows)

    def set(self, row_id: str, value):
        if row_id in _TRIALS:
            return self._set_trial_row(row_id, bool(value))
        if row_id == "display.sdr_brightness":
            try:
                nits = int(round(float(value)))
            except (TypeError, ValueError):
                raise SettingsError("Dieser Wert ist keine Helligkeit.") from None
            nits = min(SDR_MAX, max(SDR_MIN, nits))
            self._gs_set(ATOM_SDR_BRIGHTNESS, [f2u(nits)])
            self.config.set("display.sdr_brightness", nits)
            return None
        if row_id == "display.game_resolution":
            if value not in perf_mod.RESOLUTIONS:
                raise SettingsError("Diese Auflösung gibt es nicht.")
            self._perf.set(resolution=value)
            return None
        if row_id == "display.scaling_filter":
            if value not in SCALING_FILTERS:
                raise SettingsError("Diesen Filter gibt es nicht.")
            self._gs_set(ATOM_SCALING_FILTER, [SCALING_FILTERS[value]])
            self.config.set("display.scaling_filter", value)
            return None
        if row_id == "display.overlay_blur":
            self.config.set("display.overlay_blur", bool(value))
            return None
        if row_id == "display.force_composition":
            self._gs_set(ATOM_COMPOSITE_FORCE, [1 if value else 0])
            self.config.set("display.force_composition", bool(value))
            return None
        if row_id == "display.area":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SettingsError("Dieser Wert ist kein Anzeigebereich.")
            self.config.set("display.area_scale", area_percent(value))
            return {"refresh": True}
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def action(self, row_id: str, value=None):
        if row_id == "display.area":
            return {"ui": "display-area"}
        # Antwort auf eine Probezeit: action(zeile, TRIAL_KEEP|TRIAL_REVERT) oder zeile.confirm/.revert
        base, _, verb = row_id.rpartition(".")
        if row_id in _TRIALS and value in (TRIAL_KEEP, TRIAL_REVERT):
            base, verb = row_id, "confirm" if value == TRIAL_KEEP else "revert"
        if base in _TRIALS and verb == "confirm":
            return self._trial_keep(base)
        if base in _TRIALS and verb == "revert":
            self._trial_revert(base, "user")
            return {"refresh": True}
        raise SettingsError("Diese Aktion gibt es nicht.")

    # --- zusätzlich für den Verdrahter ------------------------------------------------------

    def apply_on_start(self) -> None:
        """Gespeicherten Zustand an Gamescope schreiben (Home-Start, nach Wechsel der Ausgabe).

        Schreibt auch das Ausschalten ausdrücklich: So endet eine Probezeit, die ein Absturz des
        Home überlebt hat, spätestens beim Neustart. HDR nur, wenn es je bestätigt wurde (E12).
        """
        with self._lock:
            self._gs_set(ATOM_ITM, [0], quiet=True)
            self._gs_set(ATOM_SDR_BRIGHTNESS, [f2u(self._sdr_nits())], quiet=True)
            self._gs_set(ATOM_SCALING_FILTER, [SCALING_FILTERS[self._scaling_filter()]], quiet=True)
            self._gs_set(ATOM_COMPOSITE_FORCE, [1 if self._cfg("display.force_composition") else 0], quiet=True)
            if "display.hdr" not in self._pending:
                on = bool(self._cfg("display.hdr")) and bool(self._cfg("display.hdr_confirmed"))
                self._gs_set(ATOM_HDR_ENABLED, [1 if on else 0], quiet=True)
            if "display.vrr" not in self._pending:
                self._gs_set(ATOM_VRR_ENABLED, [1 if self._cfg("display.vrr") else 0], quiet=True)

    def hdr_state(self) -> dict:
        """{"supported", "enabled", "active"} für sys.state."""
        return {"supported": self._hdr_supported(self._summary()),
                "enabled": bool(self._cfg("display.hdr")) or "display.hdr" in self._pending,
                "active": self._hdr_active()}

    def refresh(self) -> None:
        """Fernseher-Daten neu lesen. Blockierend (einige Millisekunden)."""
        self._summary(force=True)
        self._changed()

    def trial_pending(self, row_id: str) -> bool:
        return row_id in self._pending

    def cancel_trials(self) -> list[str]:
        """Für den Verdrahter, am Ende des Home: jede laufende Probezeit sofort zurücknehmen
        (niemand kann mehr bestätigen). Liefert die betroffenen Zeilen; wirft nie."""
        rows = list(self._pending)
        for row_id in rows:
            self._trial_revert(row_id, "shutdown")
        return rows

    # --- Zeilen ---------------------------------------------------------------------------

    def _info_rows(self, s: dict) -> list:
        if not self._connector:
            return [info("display.tv", "Fernseher", "Kein Fernseher erkannt")]
        if not s.get("present") or not s.get("connected"):
            return [info("display.tv", "Fernseher", "Nicht verbunden", note=self._connector)]
        texts = s.get("texts", {})
        rows = [info("display.tv", "Fernseher", texts.get("name") or "Unbekannt", note=self._connector),
                info("display.output", "Ausgabe", self._mode_text(s),
                     hint="Auflösung und Bildwiederholrate der Ausgabe sind in dieser Version fest.")]
        if texts.get("modes"):
            rows.append(info("display.modes", "Der Fernseher kann", texts["modes"]))
        caps = []
        if s.get("hdr"):
            caps.append(texts.get("hdr") or "HDR")
        if s.get("vrr"):
            caps.append("VRR" + (" " + texts["vrr"] if s.get("vrr_range") else ""))
        if caps:
            rows.append(info("display.caps", "HDR und VRR", " · ".join(caps)))
        return rows

    def _hdr_rows(self, s: dict) -> list:
        supported = self._hdr_supported(s)
        pending = "display.hdr" in self._pending
        on = bool(self._cfg("display.hdr")) or pending
        rows = [toggle(
            "display.hdr", "HDR", on and supported, enabled=supported,
            note="" if supported else "Nicht unterstützt",
            hint="Beim Einschalten fragt die Konsole, ob das Bild in Ordnung ist. Ohne Antwort "
                 "schaltet sie nach %d Sekunden zurück." % TRIAL_SECONDS,
            trial=self._trial_info("display.hdr"), default=DEFAULTS["display.hdr"])]
        rows.append(info("display.hdr_active", "HDR-Ausgabe aktiv", "Ja" if self._hdr_active() else "Nein"))
        rows.append(slider(
            "display.sdr_brightness", "Helligkeit von SDR-Inhalten", self._sdr_nits(),
            min=SDR_MIN, max=SDR_MAX, step=SDR_STEP, unit=" Nits",
            enabled=on and supported, note="" if on and supported else "Nur mit HDR",
            hint="Wie hell Menüs und Spiele ohne HDR auf der HDR-Ausgabe erscheinen.",
            default=DEFAULTS["display.sdr_brightness"]))
        return rows

    def _vrr_row(self, s: dict):
        capable = self._vrr_capable(s)
        on = bool(self._cfg("display.vrr")) or "display.vrr" in self._pending
        return toggle(
            "display.vrr", "Variable Bildwiederholrate (VRR)", on and capable, enabled=capable,
            note="" if capable else "Nicht unterstützt",
            hint="Über die HDMI-Strecke ungetestet. Beim Einschalten fragt die Konsole, ob das "
                 "Bild in Ordnung ist.",
            trial=self._trial_info("display.vrr"), default=DEFAULTS["display.vrr"])

    def _trial_info(self, row_id: str) -> dict:
        name = _TRIALS[row_id]["name"]
        return {"row": row_id, "seconds": TRIAL_SECONDS, "text": "Bild behalten?",
                "detail": "Ohne Bestätigung wird %s nach %d Sekunden wieder ausgeschaltet."
                          % (name, TRIAL_SECONDS)}

    # --- Probezeit ------------------------------------------------------------------------

    def _set_trial_row(self, row_id: str, on: bool):
        spec = _TRIALS[row_id]
        with self._lock:
            if not on:
                self._cancel_timer(row_id)
                self._gs_set(spec["atom"], [0])
                self.config.set(spec["key"], False)
                return {"refresh": True}
            s = self._summary()
            supported = self._hdr_supported(s) if row_id == "display.hdr" else self._vrr_capable(s)
            if not supported:
                raise SettingsError("Dieser Bildschirm unterstützt kein %s." % spec["name"])
            if self._cfg(spec["key"]):
                # schon bestätigt und eingeschaltet: nur noch einmal schreiben
                self._gs_set(spec["atom"], [1])
                return None
            self._cancel_timer(row_id)
            # Erst die Uhr stellen, dann schalten: Der Rückweg steht, bevor das Bild weg sein kann.
            self._pending[row_id] = _later(self.deps.scheduler, TRIAL_SECONDS,
                                           lambda: self._trial_revert(row_id, "timeout"))
            try:
                if row_id == "display.hdr":
                    self._gs_set(ATOM_ITM, [0])
                    self._gs_set(ATOM_SDR_BRIGHTNESS, [f2u(self._sdr_nits())])
                self._gs_set(spec["atom"], [1])
            except SettingsError:
                self._trial_revert(row_id, "error")
                raise
            return {"trial": self._trial_info(row_id), "refresh": True}

    def _trial_keep(self, row_id: str):
        spec = _TRIALS[row_id]
        with self._lock:
            if row_id not in self._pending:
                if self._cfg(spec["key"]):
                    return {"refresh": True}          # doppelte Bestätigung
                return {"message": "Die Zeit ist abgelaufen. %s ist wieder ausgeschaltet." % spec["name"],
                        "refresh": True}
            self._cancel_timer(row_id)
            self.config.set(spec["key"], True)
            if spec["confirmed"]:
                self.config.set(spec["confirmed"], True)
            return {"refresh": True}

    def _trial_revert(self, row_id: str, why: str) -> None:
        """Probezeit beenden und zurückschalten. Läuft auch aus dem Zeitgeber; wirft nie."""
        spec = _TRIALS[row_id]
        with self._lock:
            if row_id not in self._pending:
                return                                # schon bestätigt oder schon zurückgenommen
            self._cancel_timer(row_id)
            # Nach einem gescheiterten Einschalten gibt es nichts nachzuholen, sonst schon. Am
            # Ende des Home gibt es keinen zweiten Versuch (der nächste Start schreibt „aus“).
            self._switch_off(row_id, tries=1 if why in ("error", "shutdown") else OFF_RETRIES)
        if why == "timeout":
            log.warning("%s nicht bestätigt, wieder ausgeschaltet", spec["name"])
            try:
                self.deps.notify("%s wieder ausgeschaltet" % spec["name"],
                                 "Die Änderung wurde nicht bestätigt.")
            except Exception:  # noqa: BLE001 - eine Mitteilung darf den Rückweg nicht stören
                log.exception("Mitteilung zum Zurückschalten fehlgeschlagen")
        if why not in ("user", "shutdown"):
            self._changed()

    def _switch_off(self, row_id: str, tries: int) -> None:
        """Ausschalten nach einer Probezeit. Scheitert das Schreiben, kurz darauf noch einmal:
        Genau dieser Schritt darf nicht an einem einzelnen X-Fehler hängen bleiben."""
        if self._gs_set(_TRIALS[row_id]["atom"], [0], quiet=True) or tries <= 1:
            return
        try:
            _later(self.deps.scheduler, OFF_RETRY_SECONDS, lambda: self._retry_off(row_id, tries - 1))
        except Exception:  # noqa: BLE001
            log.exception("Zeitgeber für den zweiten Versuch ließ sich nicht stellen")

    def _retry_off(self, row_id: str, tries: int) -> None:
        with self._lock:
            if row_id in self._pending or self._cfg(_TRIALS[row_id]["key"]):
                return                                # inzwischen neu eingeschaltet
            self._switch_off(row_id, tries)

    def _cancel_timer(self, row_id: str) -> None:
        handle = self._pending.pop(row_id, None)
        if handle is not None:
            try:
                self.deps.scheduler.cancel(handle)
            except Exception:  # noqa: BLE001
                log.exception("Zeitgeber ließ sich nicht abbrechen")

    # --- Zustand --------------------------------------------------------------------------

    def _cfg(self, key: str):
        return self.config.get(key, DEFAULTS[key])

    def _sdr_nits(self) -> int:
        try:
            return min(SDR_MAX, max(SDR_MIN, int(self._cfg("display.sdr_brightness"))))
        except (TypeError, ValueError):
            return DEFAULTS["display.sdr_brightness"]

    def _game_resolution(self) -> str:
        v = self._cfg("display.game_resolution")
        return v if v in perf_mod.RESOLUTIONS else DEFAULTS["display.game_resolution"]

    def _scaling_filter(self) -> str:
        v = self._cfg("display.scaling_filter")
        return v if v in SCALING_FILTERS else DEFAULTS["display.scaling_filter"]

    def _summary(self, force: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            if force or self._drm is None or now - self._drm_at > DRM_CACHE_SECONDS:
                try:
                    self._drm = self._read_drm() or {}
                except Exception:  # noqa: BLE001 - ohne Fernseher-Daten bleibt die Seite benutzbar
                    log.exception("Bildschirmdaten nicht lesbar")
                    self._drm = {}
                self._drm_at = now
            return self._drm

    def _gs_get(self, name: str):
        try:
            return self.gs.get(name)
        except Exception:  # noqa: BLE001
            log.exception("Eigenschaft %s nicht lesbar", name)
            return None

    def _gs_set(self, name: str, values: list, quiet: bool = False) -> bool:
        try:
            self.gs.set(name, values)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Eigenschaft %s ließ sich nicht setzen", name)
            if quiet:
                return False
            raise SettingsError("Gamescope hat die Änderung nicht angenommen.") from None

    def _hdr_supported(self, s: dict) -> bool:
        # Gamescopes eigenes Urteil gilt; ohne Sitzung (Attrappe, Test) das der Karte und der EDID.
        v = self._gs_get(ATOM_HDR_SUPPORTED)
        if v:
            return bool(v[0])
        return bool(s.get("hdr"))

    def _vrr_capable(self, s: dict) -> bool:
        v = self._gs_get(ATOM_VRR_CAPABLE)
        if v:
            return bool(v[0])
        return bool(s.get("vrr"))

    def _hdr_active(self) -> bool:
        try:
            return bool(self.gs.hdr_output_active())
        except Exception:  # noqa: BLE001
            return False

    def _mode_text(self, s: dict) -> str:
        if s.get("current"):
            return drm_modes.mode_text(s["current"])
        try:
            w, h = self.gs.root_size()
        except Exception:  # noqa: BLE001
            w = h = 0
        hz = self._gs_get(ATOM_REFRESH)
        if w and h and hz and hz[0]:
            return "%d × %d, %d Hz" % (w, h, hz[0])
        return drm_modes.mode_text(s.get("preferred")) or (s.get("texts") or {}).get("mode", "")

    def _changed(self) -> None:
        try:
            self.deps.changed(AREA_ID)
        except Exception:  # noqa: BLE001
            log.exception("Änderungsmeldung fehlgeschlagen")


def make_area(deps) -> DisplayArea:
    return DisplayArea(deps)
