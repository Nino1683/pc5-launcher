"""BlueZ über D-Bus: Adapter und Geräte lesen, Controller koppeln.

Lesen: ObjectManager.GetManagedObjects (unter 3 ms). Einfaches Schreiben (Bluetooth ein/aus,
Trennen, Entfernen) sind busctl-Befehle, die der Bereich accessories durch run_write schickt
(cmd_*). Nur das Koppeln braucht eine eigene Verbindung zum Bus: Der Agent muss in unserem
Prozess leben, und die Suche gehört dem, der sie gestartet hat. Diese Wege stecken in GioBluez
und sind im Prüfmodus durch DryBluez ersetzt.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time

log = logging.getLogger("couch.settings.bluez")

BUS_NAME = "org.bluez"
AGENT_PATH = "/de/couch/agent"
_DEVICE_PATH = re.compile(r"^/org/bluez/hci\d+/dev_[0-9A-F]{2}(_[0-9A-F]{2}){5}$")
_ADAPTER_PATH = re.compile(r"^/org/bluez/hci\d+$")
_MAC = re.compile(r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})$", re.I)

AGENT_XML = """<node><interface name="org.bluez.Agent1">
<method name="Release"/>
<method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
<method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
<method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
<method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
<method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
<method name="RequestAuthorization"><arg type="o" direction="in"/></method>
<method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
<method name="Cancel"/>
</interface></node>"""


class BluezError(Exception):
    """Fehler mit deutschem Satz für die Oberfläche."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def valid_device_path(path: str) -> bool:
    return bool(_DEVICE_PATH.match(str(path)))


def valid_adapter_path(path: str) -> bool:
    return bool(_ADAPTER_PATH.match(str(path)))


# --- Lesen ------------------------------------------------------------------------------------

def battery_levels(sys_root: str = "/sys/class/power_supply") -> dict[str, dict]:
    """Akkustände nach MAC (Großbuchstaben). hid_playstation legt
    ps-controller-battery-<mac> an; BlueZ' Battery1 gibt es für den DualSense nicht."""
    out: dict[str, dict] = {}
    for p in glob.glob(os.path.join(sys_root, "*")):
        ue: dict[str, str] = {}
        try:
            with open(os.path.join(p, "uevent"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    k, _, v = line.strip().partition("=")
                    ue[k] = v
        except OSError:
            continue
        if ue.get("POWER_SUPPLY_SCOPE") != "Device":
            continue
        m = _MAC.search(os.path.basename(p))
        try:
            percent = int(ue["POWER_SUPPLY_CAPACITY"])
        except (KeyError, ValueError):
            continue
        if m:
            out[m.group(1).upper()] = {"percent": max(0, min(100, percent)),
                                       "status": ue.get("POWER_SUPPLY_STATUS", "")}
    return out


def parse_objects(objs: dict, batteries: dict | None = None, prefer: str = "") -> dict:
    """Aus GetManagedObjects: {"adapter": {...}|None, "adapters": [...], "devices": [...]}.
    Kaputte Einträge entfallen. Mehrere Adapter (2026-10-21: TP-Link-Dongle neben dem Intel-Chip):
    prefer (Adresse, input.bt_adapter) gilt, wenn es ihn gibt; sonst der Adapter mit einem
    gekoppelten Controller, sonst der erste eingeschaltete, sonst der erste."""
    batteries = batteries or {}
    adapters, devices = [], []
    if not isinstance(objs, dict):
        return {"adapter": None, "adapters": [], "devices": []}
    for path in sorted(objs, key=str):
        ifs = objs[path]
        if not isinstance(ifs, dict):
            continue
        a = ifs.get("org.bluez.Adapter1")
        if isinstance(a, dict) and valid_adapter_path(path):
            adapters.append({"path": str(path), "powered": bool(a.get("Powered")),
                             "discovering": bool(a.get("Discovering")),
                             "alias": str(a.get("Alias") or a.get("Name") or ""),
                             "address": str(a.get("Address") or "").upper()})
        d = ifs.get("org.bluez.Device1")
        if isinstance(d, dict) and valid_device_path(path) and d.get("Address"):
            mac = str(d["Address"]).upper()
            icon = str(d.get("Icon") or "")
            b1 = ifs.get("org.bluez.Battery1")
            batt = batteries.get(mac) or {}
            percent = batt.get("percent")
            if isinstance(b1, dict) and isinstance(b1.get("Percentage"), int):
                percent = int(b1["Percentage"])
            rssi = d.get("RSSI")
            devices.append({
                "path": str(path), "mac": mac,
                "name": str(d.get("Alias") or d.get("Name") or mac),
                "named": bool(d.get("Name") or d.get("Alias") and d.get("Alias") != mac.replace(":", "-")),
                "icon": icon, "paired": bool(d.get("Paired")), "trusted": bool(d.get("Trusted")),
                "connected": bool(d.get("Connected")),
                "is_gamepad": icon == "input-gaming",
                "rssi": int(rssi) if isinstance(rssi, int) else None,
                "battery": percent, "charging": batt.get("status") == "Charging",
                "adapter": str(d.get("Adapter") or os.path.dirname(str(path)))})
    devices.sort(key=lambda x: (not x["connected"], x["name"].casefold(), x["mac"]))
    return {"adapter": pick_adapter(adapters, devices, prefer), "adapters": adapters, "devices": devices}


def pick_adapter(adapters: list, devices: list, prefer: str = "") -> dict | None:
    want = str(prefer or "").upper()
    with_pad = {d["adapter"] for d in devices if d["paired"] and d["is_gamepad"]}
    return (next((a for a in adapters if want and a["address"] == want), None)
            or next((a for a in adapters if a["path"] in with_pad), None)
            or next((a for a in adapters if a["powered"]), None)
            or (adapters[0] if adapters else None))


_VENDORS = {"8087": "Intel", "0bda": "Realtek", "37ad": "TP-Link", "2357": "TP-Link", "0a12": "CSR",
            "0b05": "ASUS", "13d3": "AzureWave", "0489": "Foxconn", "0cf3": "Qualcomm", "050d": "Belkin"}


def adapter_label(path: str, sys_root: str = "/sys") -> str:
    """„TP-Link Bluetooth USB Adapter“ bzw. „Intel Bluetooth“: Name des USB-Geräts unter
    /sys/class/bluetooth/<hciN> (ohne eigenen Namen der Hersteller); leer, wenn unbekannt."""
    dev = os.path.join(sys_root, "class", "bluetooth", os.path.basename(str(path)), "device")
    d = os.path.realpath(dev) if os.path.exists(dev) else ""
    while d and d not in ("/", os.path.realpath(sys_root)):
        if os.path.isfile(os.path.join(d, "idVendor")):         # das USB-Gerät selbst, nicht der Hub
            def read(name: str) -> str:
                try:
                    with open(os.path.join(d, name), encoding="utf-8", errors="replace") as fh:
                        return fh.read().strip()
                except OSError:
                    return ""
            vendor = _VENDORS.get(read("idVendor"), "")
            return read("product") or (vendor + " Bluetooth" if vendor else "")
        d = os.path.dirname(d)
    return ""


# --- einfache Schreibwege als Befehle (laufen durch run_write) ----------------------------------

def cmd_set_powered(adapter_path: str, on: bool) -> list[str]:
    if not valid_adapter_path(adapter_path):
        raise BluezError("Kein Bluetooth-Adapter gefunden.")
    return ["busctl", "--system", "set-property", BUS_NAME, adapter_path, "org.bluez.Adapter1",
            "Powered", "b", "true" if on else "false"]


def cmd_disconnect(device_path: str) -> list[str]:
    if not valid_device_path(device_path):
        raise BluezError("Dieses Gerät gibt es nicht mehr.")
    return ["busctl", "--system", "call", BUS_NAME, device_path, "org.bluez.Device1", "Disconnect"]


def cmd_set_trusted(device_path: str) -> list[str]:
    if not valid_device_path(device_path):
        raise BluezError("Dieses Gerät gibt es nicht mehr.")
    return ["busctl", "--system", "set-property", BUS_NAME, device_path, "org.bluez.Device1",
            "Trusted", "b", "true"]


def cmd_remove(adapter_path: str, device_path: str) -> list[str]:
    if not valid_adapter_path(adapter_path) or not valid_device_path(device_path):
        raise BluezError("Dieses Gerät gibt es nicht mehr.")
    return ["busctl", "--system", "call", BUS_NAME, adapter_path, "org.bluez.Adapter1",
            "RemoveDevice", "o", device_path]


# --- Koppeln ------------------------------------------------------------------------------------

_PAIR_ERRORS = {
    "org.bluez.Error.AuthenticationFailed": "Kopplung fehlgeschlagen. Versetze den Controller erneut in den Kopplungsmodus.",
    "org.bluez.Error.AuthenticationCanceled": "Die Kopplung wurde abgebrochen.",
    "org.bluez.Error.AuthenticationRejected": "Der Controller hat die Kopplung abgelehnt.",
    "org.bluez.Error.AuthenticationTimeout": "Der Controller hat nicht geantwortet.",
    "org.bluez.Error.ConnectionAttemptFailed": "Der Controller ist nicht erreichbar. Ist er noch im Kopplungsmodus?",
    "org.bluez.Error.InProgress": "Es läuft schon eine Kopplung.",
    "org.bluez.Error.NotReady": "Bluetooth ist ausgeschaltet.",
}


class GioBluez:
    """Echte Verbindung zu BlueZ über Gio. Alle Methoden blockieren; pair() läuft in einem eigenen
    GLib-Kontext (braucht keine Hauptschleife, gehört in einen Thread).

    connection: nur für Tests (privater Bus mit BlueZ-Attrappe); sonst der Systembus.
    """

    def __init__(self, connection=None, bus_name: str = BUS_NAME):
        self._conn = connection
        self._name = bus_name

    def _c(self):
        if self._conn is None:
            from gi.repository import Gio
            self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._conn

    def _call(self, path: str, iface: str, method: str, params=None, reply=None, timeout_ms: int = 5000):
        from gi.repository import Gio, GLib
        return self._c().call_sync(self._name, path, iface, method, params,
                                   GLib.VariantType(reply) if reply else None,
                                   Gio.DBusCallFlags.NONE, timeout_ms, None)

    # Lesen
    def managed_objects(self) -> dict:
        r = self._call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                       reply="(a{oa{sa{sv}}})", timeout_ms=3000)
        return r.unpack()[0]

    # SCHREIBT
    def start_discovery(self, adapter_path: str) -> None:
        """Suche starten. Sie läuft, bis stop_discovery kommt oder unsere Bus-Verbindung endet."""
        from gi.repository import GLib
        try:
            # Nur klassisches Bluetooth: Der DualSense ist kein LE-Gerät, und LE-Baken füllen sonst die Liste.
            self._call(adapter_path, "org.bluez.Adapter1", "SetDiscoveryFilter",
                       GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "bredr")},)))
            self._call(adapter_path, "org.bluez.Adapter1", "StartDiscovery")
        except GLib.Error as e:
            if "InProgress" in (e.message or ""):
                return
            raise self._error(e, "Die Suche ließ sich nicht starten.") from None

    # SCHREIBT
    def stop_discovery(self, adapter_path: str) -> None:
        from gi.repository import GLib
        try:
            self._call(adapter_path, "org.bluez.Adapter1", "StopDiscovery")
        except GLib.Error:
            pass     # lief nicht (mehr): kein Fehler

    # SCHREIBT
    def pair(self, device_path: str, timeout: float = 45.0) -> None:
        """Koppeln, vertrauen, verbinden. Wirft BluezError mit deutschem Satz.

        Eigener Agent „NoInputNoOutput“: Im Game-Mode läuft kein Agent des Desktops, und der
        DualSense koppelt ohne PIN. Der Agent nickt nur das Gerät ab, das gerade gekoppelt wird.
        Trusted=true ist Pflicht: Ohne das darf sich das Pad später nicht von selbst verbinden.
        """
        from gi.repository import Gio, GLib
        conn = self._c()
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()      # Agent-Aufrufe und Antworten landen in diesem Kontext
        reg_id = 0
        registered = False
        try:
            node = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
            reg_id = conn.register_object(AGENT_PATH, node.interfaces[0],
                                          lambda *a: self._agent_call(device_path, *a), None, None)
            self._call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                       GLib.Variant("(os)", (AGENT_PATH, "NoInputNoOutput")))
            registered = True
            try:
                self._call_wait(ctx, device_path, "org.bluez.Device1", "Pair", None, timeout)
            except GLib.Error as e:
                if Gio.DBusError.get_remote_error(e) != "org.bluez.Error.AlreadyExists":
                    raise
            self._call(device_path, "org.freedesktop.DBus.Properties", "Set",
                       GLib.Variant("(ssv)", ("org.bluez.Device1", "Trusted", GLib.Variant("b", True))))
            try:
                self._call_wait(ctx, device_path, "org.bluez.Device1", "Connect", None, 20.0)
            except GLib.Error as e:
                # Gekoppelt ist es; verbinden kann das Pad danach selbst (PS-Taste).
                log.info("Connect nach dem Koppeln: %s", e.message)
        except GLib.Error as e:
            raise self._error(e, "Kopplung fehlgeschlagen.") from None
        finally:
            if registered:
                try:
                    self._call("/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                               GLib.Variant("(o)", (AGENT_PATH,)))
                except GLib.Error:
                    pass
            if reg_id:
                conn.unregister_object(reg_id)
            ctx.pop_thread_default()

    def _call_wait(self, ctx, path: str, iface: str, method: str, params, timeout: float) -> None:
        """Asynchron rufen und den eigenen Kontext drehen, damit der Agent währenddessen antwortet."""
        from gi.repository import Gio, GLib
        box: dict = {}

        def done(conn, res):
            try:
                conn.call_finish(res)
                box["ok"] = True
            except GLib.Error as e:
                box["error"] = e

        self._c().call(self._name, path, iface, method, params, None, Gio.DBusCallFlags.NONE,
                       int(timeout * 1000), None, done)
        tick = GLib.timeout_source_new(200)
        tick.set_callback(lambda *_: True)
        tick.attach(ctx)
        try:
            deadline = time.monotonic() + timeout + 2
            while not box and time.monotonic() < deadline:
                ctx.iteration(True)
        finally:
            tick.destroy()
        if "error" in box:
            raise box["error"]
        if not box:
            raise BluezError("Der Controller hat nicht geantwortet.")

    @staticmethod
    def _agent_call(target, _conn, _sender, _path, _iface, method, params, invocation) -> None:
        args = params.unpack() if params is not None else ()
        if method in ("Release", "Cancel", "DisplayPinCode", "DisplayPasskey"):
            invocation.return_value(None)
        elif method in ("RequestConfirmation", "RequestAuthorization", "AuthorizeService") \
                and args and args[0] == target:
            invocation.return_value(None)
        else:
            # PIN-Abfragen und fremde Geräte: ablehnen. Hier fragt nie etwas nach einer Eingabe.
            invocation.return_dbus_error("org.bluez.Error.Rejected", "abgelehnt")

    @staticmethod
    def _error(e, fallback: str) -> BluezError:
        from gi.repository import Gio
        name = Gio.DBusError.get_remote_error(e) or ""
        return BluezError(_PAIR_ERRORS.get(name, fallback), f"{name} {e.message}".strip())


class DryBluez:
    """Prüfmodus: liest wie das Original, schreibt nie. Schreibversuche stehen in writes.log."""

    def __init__(self, inner, record):
        self._inner = inner
        self._record = record          # fn(text)

    def managed_objects(self) -> dict:
        return self._inner.managed_objects()

    def start_discovery(self, adapter_path: str) -> None:
        self._record(f"bluez StartDiscovery {adapter_path}")

    def stop_discovery(self, adapter_path: str) -> None:
        self._record(f"bluez StopDiscovery {adapter_path}")

    def pair(self, device_path: str, timeout: float = 45.0) -> None:
        self._record(f"bluez Pair+Trust+Connect {device_path}")
