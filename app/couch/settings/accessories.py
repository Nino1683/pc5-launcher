"""Bereich „Zubehör“: Controller, Leuchtleiste, Bluetooth-Geräte, Koppeln.

Controller-Daten kommen vom Eingabedienst über deps.extra["pads"]() -> [{id, name, battery,
charging, type}]; fehlt der Rückruf, zeigt die Seite die verbundenen Bluetooth-Controller.

Einstellungen (config), die der Eingabedienst auswertet:
    input.rumble            bool   Vibration bei der Bedienung der Oberfläche
    input.lightbar          {"color": <Name aus LIGHTBAR_COLORS>, "brightness": 30|60|100}
                            (alte Form True/False wird gelesen); lightbar_rgb() rechnet um
    input.idle_off_minutes  0 (nie) | 10 | 30 | 60. Vorgabe 0 (E22), und die Zeile dazu fehlt
                            auf der Seite, solange niemand den Wert auswertet: Eine Auswahl
                            „10 Minuten“ verspräche ein Abschalten, das nicht passiert (E20).
    input.cable_pair        bool   Kabel-Kopplung (E78): Ein DualSense am USB-Kabel trägt diesen PC
                            als Bluetooth-Gegenüber ein (input/ds_pairing.py). Vorgabe an.

Bluetooth: Lesen über D-Bus, Ein/Aus, Trennen und Entfernen als busctl-Befehle durch run_write,
Koppeln über backends.bluez (eigener Agent). Vibrations- und Trigger-Stärke in Spielen gibt es
nicht: Ohne Steam Input lässt sich das nicht durchsetzen (Befund 9).
"""
from __future__ import annotations

import logging
import os
import time

from ..input import ds_pairing
from . import rows as R
from .backends import TtlCache, bluez, log_write, test_mode
from .rows import AREA_ORDER, TRIAL_KEEP, TRIAL_REVERT, Page, SettingsError

log = logging.getLogger("couch.settings.accessories")

LIGHTBAR_COLORS = {            # Name -> (Anzeige, R, G, B)
    "blue": ("Blau", 0, 64, 255),
    "white": ("Weiß", 255, 255, 255),
    "red": ("Rot", 255, 0, 0),
    "green": ("Grün", 0, 255, 0),
    "yellow": ("Gelb", 255, 190, 0),
    "orange": ("Orange", 255, 90, 0),
    "purple": ("Violett", 150, 0, 255),
    "cyan": ("Türkis", 0, 220, 255),
    "off": ("Aus", 0, 0, 0),
}
LIGHTBAR_LEVELS = [(30, "Schwach"), (60, "Mittel"), (100, "Hell")]
LIGHTBAR_DEFAULT = {"color": "blue", "brightness": 100}
IDLE_CHOICES = [(10, "10 Minuten"), (30, "30 Minuten"), (60, "60 Minuten"), (0, "Nie")]
CAPTURE_CHOICES = [("ps-create", "PS-Taste halten + Create"), ("create", "Create allein")]   # capture.py
CAPTURE_DEFAULT = "ps-create"
IDLE_DEFAULT = 0               # nie (E22), bis das Abschalten am echten Pad gebaut und geprüft ist
IDLE_ROW = False               # Zeile „Controller ausschalten nach“ erst mit einem Backend zeigen
BT_TRIAL_SECONDS = 15
# Marke der laufenden Probezeit „Bluetooth aus“ im Zustandsordner (dieselbe wie paths.bt_trial_file
# und im Supervisor): Stirbt das Home in der Probezeit, schalten der Supervisor bzw. das nächste
# Home Bluetooth wieder ein. Sonst bliebe ein Bluetooth-Pad tot, auch für Steam und den Notgriff.
BT_TRIAL_MARKER = "bt-off-trial"
SCAN_SECONDS = 30
SCAN_POLL = 2.0
# Nach der Kabel-Kopplung so lange nachsehen, ob das Pad per Bluetooth kommt, und es dann
# vertrauenswürdig machen (ohne Trusted darf es sich nicht von selbst verbinden)
CABLE_WATCH_SECONDS = 300
CABLE_POLL = 2.0


def lightbar_value(raw) -> dict:
    """Einstellung input.lightbar in die Form {"color", "brightness"} bringen (auch True/False)."""
    if isinstance(raw, dict):
        color = raw.get("color") if raw.get("color") in LIGHTBAR_COLORS else LIGHTBAR_DEFAULT["color"]
        level = raw.get("brightness") if raw.get("brightness") in (30, 60, 100) else LIGHTBAR_DEFAULT["brightness"]
        return {"color": color, "brightness": level}
    if raw is False:
        return {"color": "off", "brightness": LIGHTBAR_DEFAULT["brightness"]}
    return dict(LIGHTBAR_DEFAULT)


def lightbar_rgb(raw) -> tuple[int, int, int]:
    """Farbe für die Leuchtleiste aus der Einstellung input.lightbar, Helligkeit eingerechnet.
    (0, 0, 0) heißt aus. Für den Eingabedienst (dualsense_fx.set_lightbar)."""
    v = lightbar_value(raw)
    _, r, g, b = LIGHTBAR_COLORS[v["color"]]
    f = v["brightness"] / 100
    return (round(r * f), round(g * f), round(b * f))


def _battery_text(pad: dict) -> str:
    level = pad.get("battery")
    if not isinstance(level, (int, float)):
        return "Verbunden"
    text = f"Akku {int(level)} %"
    if pad.get("charging"):
        text += " · wird geladen"
    return text


class AccessoriesArea:
    id = "accessories"
    label = "Zubehör"
    icon = "gamepad"
    order = AREA_ORDER["accessories"]

    def __init__(self, deps):
        self.deps = deps
        self._cache = TtlCache(1.0)
        self._bus = None
        self._bt_timer = None          # Probezeit „Bluetooth aus“
        self._bt_adapter = None
        self._scan_left = 0.0
        self._scan_timer = None
        self._scan_adapter = None
        self._cable_ignored: set[str] = set()      # Geräte, die die Kabel-Kopplung nicht übernehmen
        self._cable_left: dict[str, float] = {}    # MAC -> Sekunden Nachschau, die noch bleiben

    # --- Quellen -------------------------------------------------------------------------

    def _bluez(self):
        if self._bus is None:
            bus = self.deps.extra.get("bluez")
            if bus is None:
                bus = bluez.GioBluez()
                if test_mode():
                    bus = bluez.DryBluez(bus, lambda text: log_write(self.deps.paths, text))
            self._bus = bus
        return self._bus

    def bt_state(self) -> dict:
        """{"adapter": {...}|None, "devices": [...], "error": str}. Blockierend (wenige ms)."""
        def load():
            try:
                objs = self._bluez().managed_objects()
            except Exception as e:  # noqa: BLE001 - bluetoothd fehlt, Bus weg: Seite zeigt den Zustand
                log.info("BlueZ nicht erreichbar: %s", e)
                return {"adapter": None, "devices": [], "error": "Bluetooth ist nicht verfügbar."}
            root = self.deps.extra.get("power_supply_root", "/sys/class/power_supply")
            st = bluez.parse_objects(objs, bluez.battery_levels(root), str(self.deps.config.get("input.bt_adapter", "") or ""))
            st["error"] = "" if st["adapter"] else "Kein Bluetooth-Adapter gefunden."
            return st
        return self._cache.get("bt", load)

    def _pads(self) -> list[dict]:
        fn = self.deps.extra.get("pads")
        if callable(fn):
            try:
                return [p for p in (fn() or []) if isinstance(p, dict)]
            except Exception:  # noqa: BLE001
                log.exception("pads() wirft")
                return []
        return [{"id": d["mac"], "name": d["name"], "battery": d["battery"], "charging": d["charging"],
                 "type": "bluetooth"} for d in self.bt_state()["devices"] if d["connected"] and d["is_gamepad"]]

    def _device(self, mac: str) -> dict:
        d = next((d for d in self.bt_state()["devices"] if d["mac"] == mac.upper()), None)
        if d is None:
            raise SettingsError("Dieses Gerät gibt es nicht mehr.")
        return d

    # --- Seiten --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return ["accessories", "accessories.bluetooth", "accessories.pair"]

    def page(self, page_id: str) -> Page:
        if page_id == "accessories":
            return Page(page_id, "Zubehör", self._main_rows())
        if page_id == "accessories.bluetooth":
            return Page(page_id, "Bluetooth-Zubehör", self._bt_rows())
        if page_id == "accessories.pair":
            return Page(page_id, "Neuen Controller koppeln", self._pair_rows())
        if page_id.startswith("accessories.bt.device:"):
            d = self._device(page_id.split(":", 1)[1])
            return Page(page_id, d["name"], self._device_rows(d))
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        cfg = self.deps.config
        rows = []
        pads = self._pads()
        if not pads:
            rows.append(R.info("accessories.pads", "Controller", "Kein Controller verbunden"))
        for p in pads:
            kind = {"bluetooth": "Bluetooth", "usb": "USB"}.get(str(p.get("type") or ""), "")
            rows.append(R.info(f"accessories.pad:{p.get('id')}", str(p.get("name") or "Controller"),
                               _battery_text(p), note=kind))
        rows.append(R.toggle("accessories.rumble", "Vibration bei der Bedienung", bool(cfg.get("input.rumble", True)),
                             hint="Gilt für die Oberfläche. In Spielen bestimmt das Spiel die Vibration.",
                             default=True))
        capture = cfg.get("input.capture_button", CAPTURE_DEFAULT)
        rows.append(R.select("accessories.capture", "Bildschirmfoto mit",
                             capture if capture in [v for v, _ in CAPTURE_CHOICES] else CAPTURE_DEFAULT,
                             [R.option(v, label) for v, label in CAPTURE_CHOICES], default=CAPTURE_DEFAULT,
                             hint="Fotos liegen unter Dateien › Bildschirmfotos. Mit Create allein sieht ein "
                                  "Spiel den Druck auch (oft Karte oder Menü)."))
        lb = lightbar_value(cfg.get("input.lightbar", None))
        rows.append(R.select("accessories.lightbar-color", "Farbe der Leuchtleiste", lb["color"],
                             [R.option(k, v[0]) for k, v in LIGHTBAR_COLORS.items()],
                             hint="Spiele mit eigener Unterstützung für den Controller setzen die Farbe selbst.",
                             default=LIGHTBAR_DEFAULT["color"]))
        rows.append(R.select("accessories.lightbar-brightness", "Helligkeit der Leuchtleiste", lb["brightness"],
                             [R.option(v, label) for v, label in LIGHTBAR_LEVELS], enabled=lb["color"] != "off",
                             default=LIGHTBAR_DEFAULT["brightness"]))
        if IDLE_ROW:
            idle = cfg.get("input.idle_off_minutes", IDLE_DEFAULT)
            if idle not in [v for v, _ in IDLE_CHOICES]:
                idle = IDLE_DEFAULT
            rows.append(R.select("accessories.idle-off", "Controller ausschalten nach", idle,
                                 [R.option(v, label) for v, label in IDLE_CHOICES],
                                 hint="Schaltet einen unbenutzten Bluetooth-Controller aus und schont den Akku."))
        bt = self.bt_state()
        note = bt["error"] or ("Ein" if bt["adapter"]["powered"] else "Aus")
        rows.append(R.link("accessories.bluetooth-page", "Bluetooth-Zubehör", "accessories.bluetooth", note=note))
        return rows

    def _adapter_rows(self, bt: dict) -> list:
        """Mehrere Adapter (z. B. ein Dongle am Fernseher neben dem eingebauten): welcher gilt für
        Controller, Koppeln und Kabel-Kopplung (input.bt_adapter)."""
        adapters = bt.get("adapters") or []
        if len(adapters) < 2:
            return []
        sys_root = self.deps.extra.get("sys_root", "/sys")
        names = {a["address"]: (bluez.adapter_label(a["path"], sys_root) or "Bluetooth") + " · " + a["address"][-8:]
                 for a in adapters}
        want = str(self.deps.config.get("input.bt_adapter", "") or "").upper()
        options = [R.option("", "Automatisch", note=names.get(bt["adapter"]["address"], ""))]
        options += [R.option(a["address"], names[a["address"]]) for a in adapters]
        return [R.select("accessories.bt.adapter", "Bluetooth-Adapter", want if want in names else "", options,
                         default="", hint="Automatisch nimmt den Adapter, an dem dein Controller gekoppelt ist. "
                                          "Ein anderer Adapter braucht eine neue Kopplung.")]

    def _bt_rows(self) -> list:
        bt = self.bt_state()
        if bt["error"]:
            return [R.info("accessories.bt.none", bt["error"], enabled=False)]
        on = bt["adapter"]["powered"]
        rows = [R.toggle("accessories.bt.power", "Bluetooth", on,
                         trial={"seconds": BT_TRIAL_SECONDS, "text": "Bluetooth ausgeschaltet lassen?"},
                         hint="Beim Ausschalten werden kabellose Controller getrennt. Ohne Bestätigung "
                              f"schaltet sich Bluetooth nach {BT_TRIAL_SECONDS} Sekunden wieder ein."),
                *self._adapter_rows(bt),
                R.toggle("accessories.bt.cable", "Per Kabel koppeln",
                         bool(self.deps.config.get("input.cable_pair", True)), default=True,
                         hint="Steckst du einen Controller per USB-Kabel an, koppelt er sich mit diesem PC. "
                              "Danach Kabel abziehen und die PS-Taste drücken.")]
        for d in bt["devices"]:
            if not d["paired"]:
                continue
            if d["connected"]:
                note = "Verbunden" + (f" · Akku {d['battery']} %" if d["battery"] is not None else "")
            else:
                note = "Nicht verbunden"
            rows.append(R.link(f"accessories.bt.dev:{d['mac']}", d["name"], f"accessories.bt.device:{d['mac']}", note=note))
        rows.append(R.link("accessories.pair-page", "Neuen Controller koppeln", "accessories.pair",
                           enabled=on, note="" if on else "Bluetooth ist aus"))
        return rows

    def _device_rows(self, d: dict) -> list:
        mac = d["mac"]
        rows = [R.info(f"accessories.bt.status:{mac}", "Status", "Verbunden" if d["connected"] else "Nicht verbunden")]
        if d["battery"] is not None:
            rows.append(R.info(f"accessories.bt.battery:{mac}", "Akku",
                               f"{d['battery']} %" + (" · wird geladen" if d["charging"] else "")))
        rows.append(R.info(f"accessories.bt.mac:{mac}", "Adresse", mac))
        if d["connected"]:
            rows.append(R.action(f"accessories.bt.disconnect:{mac}", "Trennen",
                                 hint="Schaltet den Controller aus. Mit der PS-Taste verbindet er sich wieder."))
        rows.append(R.confirm_action(
            f"accessories.bt.remove:{mac}", "Gerät entfernen",
            title=f"„{d['name']}“ entfernen?",
            text="Das Gerät wird getrennt und vergessen. Zum erneuten Koppeln brauchst du einen "
                 "zweiten Controller oder ein USB-Kabel.",
            ok="Entfernen"))
        return rows

    def _pair_rows(self) -> list:
        bt = self.bt_state()
        if bt["error"]:
            return [R.info("accessories.pair.none", bt["error"], enabled=False)]
        rows = [R.info("accessories.pair.howto", "Kopplungsmodus", "Create + PS-Taste 5 Sekunden halten",
                       hint="Halte am ausgeschalteten Controller die Create-Taste (links vom Touchpad) und die "
                            "PS-Taste gedrückt, bis die Leuchtleiste schnell doppelt blinkt.")]
        if not bt["adapter"]["powered"]:
            rows.append(R.info("accessories.pair.off", "Bluetooth ist ausgeschaltet.", enabled=False))
            return rows
        scanning = bt["adapter"]["discovering"] or self._scan_left > 0
        if scanning:
            rows.append(R.action("accessories.pair.stop", "Suche beenden", note="Suche läuft …"))
        else:
            rows.append(R.action("accessories.pair.scan", "Suche starten"))
        found = [d for d in bt["devices"] if not d["paired"] and d["is_gamepad"]]
        found.sort(key=lambda d: -(d["rssi"] if d["rssi"] is not None else -200))
        for d in found:
            rows.append(R.action(f"accessories.pair.device:{d['mac']}", d["name"], note="Gefunden",
                                 hint="Auswählen, um zu koppeln."))
        if scanning and not found:
            rows.append(R.info("accessories.pair.empty", "Noch kein Controller gefunden.", enabled=False))
        return rows

    # --- Schreiben -----------------------------------------------------------------------

    def _write(self, argv: list[str], failure: str) -> None:
        rc, _out, err = self.deps.run_write(argv, timeout=15)
        self._cache.drop()
        if rc != 0:
            log.warning("%s: rc=%s %s", " ".join(argv), rc, (err or "").strip())
            raise SettingsError(failure)

    def set(self, row_id: str, value):
        cfg = self.deps.config
        if row_id == "accessories.bt.adapter":
            known = {a["address"] for a in self.bt_state().get("adapters") or []}
            if value not in known and value != "":
                raise SettingsError("Diesen Adapter gibt es nicht.")
            cfg.set("input.bt_adapter", value)
            self._cache.drop("bt")
            return {"refresh": True}
        if row_id == "accessories.capture":
            if value not in [v for v, _ in CAPTURE_CHOICES]:
                raise SettingsError("Ungültiger Wert.")
            cfg.set("input.capture_button", value)
            return None
        if row_id == "accessories.rumble":
            cfg.set("input.rumble", bool(value))
            return None
        if row_id == "accessories.lightbar-color":
            if value not in LIGHTBAR_COLORS:
                raise SettingsError("Diese Farbe gibt es nicht.")
            cfg.set("input.lightbar", {**lightbar_value(cfg.get("input.lightbar", None)), "color": value})
            return {"refresh": True}
        if row_id == "accessories.lightbar-brightness":
            if value not in [v for v, _ in LIGHTBAR_LEVELS]:
                raise SettingsError("Ungültiger Wert.")
            cfg.set("input.lightbar", {**lightbar_value(cfg.get("input.lightbar", None)), "brightness": value})
            return None
        if row_id == "accessories.idle-off" and IDLE_ROW:
            if value not in [v for v, _ in IDLE_CHOICES]:
                raise SettingsError("Ungültiger Wert.")
            cfg.set("input.idle_off_minutes", value)
            return None
        if row_id == "accessories.bt.power":
            return self._set_bt_power(bool(value))
        if row_id == "accessories.bt.cable":
            cfg.set("input.cable_pair", bool(value))
            return None
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def _set_bt_power(self, on: bool):
        adapter = self.bt_state()["adapter"]
        if adapter is None:
            raise SettingsError("Kein Bluetooth-Adapter gefunden.")
        self._cancel_bt_trial()
        if not on:
            # Die Marke zuerst: Stirbt das Home gleich nach dem Ausschalten, weiß der Nächste Bescheid
            self._mark_trial(adapter["path"])
        try:
            self._write(bluez.cmd_set_powered(adapter["path"], on), "Bluetooth ließ sich nicht umschalten.")
        except SettingsError:
            if not on:
                self._clear_trial()
            raise
        if on:
            self._bt_adapter = None
            self._clear_trial()                 # von Hand eingeschaltet: keine Probezeit mehr
            return {"refresh": True}
        # Mit Bluetooth geht auch der Controller aus, mit dem man es wieder einschalten könnte.
        # Deshalb Probezeit: Ohne Bestätigung (z. B. per USB-Controller) kommt Bluetooth zurück.
        self._bt_adapter = adapter["path"]
        # Der Zeitgeber läuft in der Hauptschleife, das Einschalten blockiert: also im Hintergrund.
        self._bt_timer = self.deps.scheduler.later(
            BT_TRIAL_SECONDS, lambda: self.deps.scheduler.background(self._bt_revert))
        return {"refresh": True, "trial": {"row": "accessories.bt.power", "seconds": BT_TRIAL_SECONDS,
                                           "text": "Bluetooth ausgeschaltet lassen?"}}

    def _cancel_bt_trial(self) -> None:
        if self._bt_timer is not None:
            self.deps.scheduler.cancel(self._bt_timer)
            self._bt_timer = None

    def _bt_revert(self) -> None:
        self._bt_timer = None
        path, self._bt_adapter = self._bt_adapter, None
        if not path:
            return
        try:
            self._write(bluez.cmd_set_powered(path, True), "Bluetooth ließ sich nicht einschalten.")
        except SettingsError:
            # Die Marke bleibt: Supervisor bzw. nächster Start versuchen es noch einmal
            log.error("Bluetooth ließ sich nach der Probezeit nicht wieder einschalten")
        else:
            self._clear_trial()
        self.deps.changed("accessories.bluetooth")

    # --- Marke der Probezeit -------------------------------------------------------------

    def _trial_file(self) -> str | None:
        state = getattr(self.deps.paths, "state_dir", None)
        return os.path.join(state, BT_TRIAL_MARKER) if state else None

    def _mark_trial(self, adapter_path: str) -> None:
        path = self._trial_file()
        if not path:
            return
        tmp = "%s.tmp-%d" % (path, os.getpid())
        try:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("%s %.3f\n" % (adapter_path, time.time()))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Marke der Bluetooth-Probezeit nicht schreibbar: %s", e)

    def _clear_trial(self) -> None:
        path = self._trial_file()
        if not path:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Marke der Bluetooth-Probezeit nicht entfernbar: %s", e)

    def restore_after_restart(self) -> bool:
        """Nach dem Start (Arbeits-Thread): Liegt noch die Marke eines früheren Home, hat dessen
        Probezeit nie geendet. Bluetooth wieder einschalten. True = etwas getan. Blockierend."""
        path = self._trial_file()
        if not path or self._bt_adapter is not None:
            return False                        # keine Marke, oder eine eigene Probezeit läuft
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                adapter = (f.readline().split() or [""])[0]
        except FileNotFoundError:
            return False
        except OSError as e:
            log.warning("Marke der Bluetooth-Probezeit nicht lesbar: %s", e)
            return False
        if not bluez.valid_adapter_path(adapter):
            log.warning("Marke der Bluetooth-Probezeit unbrauchbar, verworfen: %r", adapter[:80])
            self._clear_trial()
            return True
        log.warning("Probezeit „Bluetooth aus“ eines früheren Starts nie beendet: Bluetooth wieder ein (%s)", adapter)
        for _ in range(3):
            try:
                self._write(bluez.cmd_set_powered(adapter, True), "Bluetooth ließ sich nicht einschalten.")
            except SettingsError:
                time.sleep(1.0)
                continue
            self._clear_trial()
            self.deps.changed("accessories.bluetooth")
            return True
        log.error("Bluetooth ließ sich nicht wieder einschalten; die Marke bleibt für den Supervisor")
        return True

    def end_trial(self) -> None:
        """Am Ende des Home: Eine laufende Probezeit kann niemand mehr bestätigen, also zurück.
        Gelingt das nicht, bleibt die Marke für den Supervisor."""
        if self._bt_adapter is None:
            return
        self._cancel_bt_trial()
        self._bt_revert()

    def action(self, row_id: str, value=None):
        if row_id == "accessories.bt.power":
            if value == TRIAL_KEEP:
                self._cancel_bt_trial()
                self._bt_adapter = None
                self._clear_trial()             # bestätigt: Bluetooth bleibt gewollt aus
                return {"refresh": True}
            if value == TRIAL_REVERT:
                self._cancel_bt_trial()
                self._bt_revert()
                return {"refresh": True}
            raise SettingsError("Diese Aktion gibt es nicht.")
        if row_id == "accessories.pair.scan":
            return self._scan_start()
        if row_id == "accessories.pair.stop":
            self._scan_stop()
            return {"refresh": True}
        head, _, mac = row_id.partition(":")
        if head == "accessories.pair.device":
            return self._pair(mac)
        if head == "accessories.bt.disconnect":
            d = self._device(mac)
            self._write(bluez.cmd_disconnect(d["path"]), "Das Gerät ließ sich nicht trennen.")
            return {"message": f"„{d['name']}“ wurde getrennt.", "refresh": True}
        if head == "accessories.bt.remove":
            d = self._device(mac)
            self._refuse_last_pad(d)
            self._write(bluez.cmd_remove(d["adapter"], d["path"]), "Das Gerät ließ sich nicht entfernen.")
            return {"message": f"„{d['name']}“ wurde entfernt.", "page": "accessories.bluetooth"}
        raise SettingsError("Diese Aktion gibt es nicht.")

    def _refuse_last_pad(self, d: dict) -> None:
        """Den gerade verbundenen und einzigen Controller nicht entfernen: Danach ließe sich die
        Konsole nicht mehr bedienen, und neu koppeln geht nur mit einem zweiten Controller oder
        einem USB-Kabel."""
        if not (d.get("connected") and d.get("is_gamepad")):
            return
        pads = self.deps.extra.get("pads")
        count = None
        if callable(pads):
            try:
                count = len([p for p in (pads() or []) if isinstance(p, dict)])
            except Exception:  # noqa: BLE001 - ohne Liste zählen die verbundenen Bluetooth-Controller
                log.exception("Pads zählen")
        if count is None:
            count = sum(1 for x in self.bt_state()["devices"] if x.get("connected") and x.get("is_gamepad"))
        if count <= 1:
            raise SettingsError(f"„{d['name']}“ ist gerade dein einziger Controller. Schließ zuerst einen "
                                "zweiten Controller oder ein USB-Kabel an, sonst lässt sich die Konsole "
                                "nicht mehr bedienen.")

    # --- Koppeln -------------------------------------------------------------------------

    def _scan_start(self):
        bt = self.bt_state()
        if bt["error"] or not bt["adapter"]["powered"]:
            raise SettingsError(bt["error"] or "Bluetooth ist ausgeschaltet.")
        try:
            self._bluez().start_discovery(bt["adapter"]["path"])
        except bluez.BluezError as e:
            raise SettingsError(e.message) from None
        self._scan_adapter = bt["adapter"]["path"]
        self._scan_left = float(SCAN_SECONDS)
        self._cache.drop()
        self._scan_tick_arm()
        return {"message": "Suche läuft …", "refresh": True}

    def _scan_tick_arm(self) -> None:
        if self._scan_timer is not None:
            self.deps.scheduler.cancel(self._scan_timer)
        self._scan_timer = self.deps.scheduler.later(SCAN_POLL, self._scan_tick)

    def _scan_tick(self) -> None:
        # Während der Suche die Seite regelmäßig neu zeichnen lassen; nach SCAN_SECONDS endet sie
        # von selbst, damit der Funk nicht dauernd sucht.
        self._scan_timer = None
        if self._scan_left <= 0:
            return
        self._scan_left -= SCAN_POLL
        self._cache.drop()
        if self._scan_left <= 0:
            # StopDiscovery ist ein D-Bus-Aufruf: nicht in der Hauptschleife warten.
            self.deps.scheduler.background(lambda: (self._scan_stop(), self.deps.changed("accessories.pair")))
        else:
            self._scan_tick_arm()
            self.deps.changed("accessories.pair")

    def _scan_stop(self) -> None:
        self._scan_left = 0.0
        if self._scan_timer is not None:
            self.deps.scheduler.cancel(self._scan_timer)
            self._scan_timer = None
        path, self._scan_adapter = self._scan_adapter, None
        if path is None:
            adapter = self.bt_state()["adapter"]
            path = adapter["path"] if adapter else None
        if path:
            try:
                self._bluez().stop_discovery(path)
            except Exception:  # noqa: BLE001
                log.exception("StopDiscovery")
        self._cache.drop()

    def _pair(self, mac: str):
        d = self._device(mac)
        if not d["is_gamepad"]:
            raise SettingsError("Hier lassen sich nur Controller koppeln.")
        try:
            self._bluez().pair(d["path"])     # blockiert bis zu rund 45 s
        except bluez.BluezError as e:
            self._cache.drop()
            raise SettingsError(e.message) from None
        self._scan_stop()
        return {"message": f"„{d['name']}“ wurde gekoppelt.", "page": "accessories.bluetooth"}

    # --- Kabel-Kopplung (E78) -------------------------------------------------------------

    def cable_pair(self, pad: dict) -> str:
        """Ein DualSense steckt per USB (Eingabedienst; Arbeits-Thread): diesen PC als sein
        Bluetooth-Gegenüber eintragen. pad = {id: MAC laut Kernel, name, hidraw: /dev/hidrawN}.
        Liefert den Ausgang fürs Protokoll: off, ignored, no-bluetooth, no-hidraw, unreadable,
        already, paired."""
        mac = str(pad.get("id") or "").upper()
        if not self.deps.config.get("input.cable_pair", True):
            return "off"
        if mac in self._cable_ignored:
            return "ignored"
        adapter = self.bt_state()["adapter"]
        if adapter is None or not adapter["powered"] or not ds_pairing.valid_mac(adapter["address"]):
            log.info("Kabel-Kopplung %s: Bluetooth ist aus oder fehlt", mac or "?")
            return "no-bluetooth"
        opener = self.deps.extra.get("hidraw_open")
        if opener is None:
            if test_mode():
                return "no-hidraw"              # Prüflauf: nie ein echtes Pad beschreiben
            opener = ds_pairing.Hidraw
        node = str(pad.get("hidraw") or "")
        try:
            dev = opener(node)
        except OSError as e:
            log.info("Kabel-Kopplung %s: %s nicht zu öffnen: %s", mac or "?", node or "kein hidraw", e)
            return "no-hidraw"
        try:
            res = ds_pairing.pair_over_cable(dev, adapter["address"])
        finally:
            dev.close()
        status = res["status"]
        log.info("Kabel-Kopplung %s: %s (Gegenüber vorher %s)%s", res.get("pad") or mac, status,
                 res.get("host") or "?", f" {res['error']}" if res.get("error") else "")
        if status == "ignored":
            self._cable_ignored.add(mac)
        elif status == "paired":
            self.deps.notify("Controller gekoppelt", "Kabel abziehen und die PS-Taste drücken.", "gamepad")
            self._cable_left[res["pad"]] = float(CABLE_WATCH_SECONDS)
            self._cable_watch(res["pad"])
        return status

    def _cable_watch(self, mac: str) -> None:
        self.deps.scheduler.later(CABLE_POLL, lambda: self.deps.scheduler.background(lambda: self._cable_check(mac)))

    def _cable_check(self, mac: str) -> None:
        """Arbeits-Thread: Ist das Pad per Bluetooth gekoppelt, vertrauen; ist es verbunden, fertig."""
        self._cache.drop()
        d = next((d for d in self.bt_state()["devices"] if d["mac"] == mac), None)
        if d is not None and d["paired"] and not d["trusted"]:
            try:
                self._write(bluez.cmd_set_trusted(d["path"]), "Vertrauen ließ sich nicht setzen.")
                log.info("Kabel-Kopplung %s: als vertrauenswürdig eingetragen", mac)
            except (SettingsError, bluez.BluezError):
                pass
        if d is not None and d["paired"] and d["connected"]:
            log.info("Kabel-Kopplung %s: per Bluetooth verbunden", mac)
            self._cable_left.pop(mac, None)
            self.deps.changed("accessories.bluetooth")
            return
        left = self._cable_left.get(mac, 0.0) - CABLE_POLL
        if left > 0:
            self._cable_left[mac] = left
            self._cable_watch(mac)
        else:
            self._cable_left.pop(mac, None)
            log.info("Kabel-Kopplung %s: kam in %d s nicht per Bluetooth", mac, CABLE_WATCH_SECONDS)


def make_area(deps) -> AccessoriesArea:
    return AccessoriesArea(deps)
