"""Bereich „Netzwerk“: Zustand, WLAN ein/aus, Netze suchen und verbinden, Verbindung testen.

Lesen über nmcli (backends/nm.py). Schreiben über run_write:
    nmcli radio wifi on|off
    nmcli device wifi rescan
    nmcli -w 30 connection up uuid <uuid>          gemerktes Netz verbinden
    nmcli connection delete uuid <uuid>            gemerktes Netz entfernen (nur WLAN-Profile)
Ein neues Netz (mit oder ohne Kennwort) geht über libnm (nm.wifi_connect), damit weder Kennwort
noch SSID auf einer Befehlszeile stehen; im Prüfmodus wird das nur protokolliert.
Geschwindigkeit messen (E111, hebt den Ausschluss aus Befund 9 auf): backends/speedtest.py im eigenen
Thread, abbrechbar; nicht, solange Downloads oder Installationen laufen; Ergebnis in
<state>/speedtest.json. Im Prüfmodus nur gegen einen lokalen Server (COUCH_SPEEDTEST_URL).
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from . import rows as R
from .backends import TtlCache, log_write, nm, speedtest, test_mode
from .rows import AREA_ORDER, Page, SettingsError

log = logging.getLogger("couch.settings.network")

# E111: Geschwindigkeit messen
SPEED_HINT = "Lädt bis zu 250 MB von speed.cloudflare.com, etwa 10 Sekunden. Nichts wird hochgeladen."
SPEED_BUSY = "Gerade laufen Downloads oder Installationen. Miss danach, sonst stimmt das Ergebnis nicht."
SPEED_RUNNING = "Wird gemessen … %d %%"
SPEED_TEST_MODE = "Im Prüfmodus nicht verfügbar"
SPEED_REFRESH_S = 1.0               # so oft zeichnet die Seite den Stand neu (nicht je Prozent)

_CONNECTIVITY = {
    "full": "Verbunden",
    "limited": "Verbunden, kein Internet",
    "portal": "Anmeldung im Netz erforderlich",
    "none": "Kein Internet",
}


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def _unhex(code: str) -> str:
    try:
        return bytes.fromhex(code).decode("utf-8")
    except ValueError:
        raise SettingsError("Dieses Netz gibt es nicht mehr.") from None


def _probe_internet() -> bool:
    """Rückfall, wenn NetworkManager die Erreichbarkeit nicht prüft: eine TCP-Verbindung."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=3).close()
        return True
    except OSError:
        return False


class NetworkArea:
    id = "network"
    label = "Netzwerk"
    icon = "network"
    order = AREA_ORDER["network"]

    def __init__(self, deps):
        self.deps = deps
        self._cache = TtlCache(2.0)
        # E111: laufende Messung, letzter Fehler (nur im Speicher), letztes Ergebnis (aus der Ablage)
        self._speed_lock = threading.Lock()
        self._speed: speedtest.Measurement | None = None
        self._speed_error = ""
        self._speed_last: dict | None = None
        self._speed_loaded = False
        self._speed_drawn = 0.0

    # --- Lesen ---------------------------------------------------------------------------

    def state(self) -> dict:
        return self._cache.get("state", lambda: nm.read_state(self.deps.run_read))

    def invalidate(self) -> None:
        """Für den Verdrahter: Der Netz-Beobachter hat eine Änderung gemeldet, neu lesen."""
        self._cache.drop()

    def summary(self) -> dict:
        """Für sys.state: {"state": "connected"|"limited"|"offline", "type": "ethernet"|"wifi"|None, "name"}."""
        st = self.state()
        p = st.get("primary")
        if not p:
            return {"state": "offline", "type": None, "name": ""}
        return {"state": "connected" if st["connectivity"] in ("full", "unknown") else "limited",
                "type": p["type"], "name": p["connection"]}

    def pages(self) -> list[str]:
        return ["network", "network.details", "network.wifi"]

    def page(self, page_id: str) -> Page:
        if page_id == "network":
            return Page("network", "Netzwerk", self._main_rows())
        if page_id == "network.details":
            return Page("network.details", "Verbindungsstatus", self._detail_rows())
        if page_id == "network.wifi":
            return Page("network.wifi", "WLAN-Netze", self._wifi_rows())
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        st = self.state()
        if st["error"]:
            return [R.info("network.error", "Der Netzwerkdienst antwortet nicht.", enabled=False)]
        p = st["primary"]
        rows = []
        if p:
            rows.append(R.info("network.status", "Verbindungsstatus", _CONNECTIVITY.get(st["connectivity"], "Verbunden")))
            rows.append(R.info("network.type", "Verbindungsart", "WLAN" if p["type"] == "wifi" else "Kabel (LAN)"))
            rows.append(R.info("network.name", "Netzwerk", p["connection"]))
            rows.append(R.info("network.ip", "IP-Adresse", (p["ip4"][0].split("/")[0] if p["ip4"] else "Keine")))
            rows.append(R.link("network.details-page", "Verbindungsstatus anzeigen", "network.details"))
        else:
            rows.append(R.info("network.status", "Verbindungsstatus", "Nicht verbunden"))
        rows.append(R.action("network.test", "Internetverbindung testen",
                             hint="Prüft, ob das Internet erreichbar ist."))
        rows += self._speed_rows()
        if st["wifi_device"] is None:
            rows.append(R.toggle("network.wifi", "WLAN", False, enabled=False, note="Kein WLAN-Adapter"))
        elif not st["wifi_hw"]:
            rows.append(R.toggle("network.wifi", "WLAN", False, enabled=False, note="Am Gerät ausgeschaltet"))
        else:
            rows.append(R.toggle("network.wifi", "WLAN", st["wifi"]))
            wifi_dev = next((d for d in st["devices"] if d["type"] == "wifi"), None)
            rows.append(R.link("network.wifi-page", "WLAN-Netz einrichten", "network.wifi", enabled=st["wifi"],
                               note=(wifi_dev["connection"] if wifi_dev and wifi_dev["connected"] else "")
                               if st["wifi"] else "WLAN ist aus"))
        return rows

    def _detail_rows(self) -> list:
        st = self.state()
        p = st.get("primary")
        if not p:
            return [R.info("network.d.status", "Verbindungsstatus", "Nicht verbunden")]
        rows = [
            R.info("network.d.status", "Internet", _CONNECTIVITY.get(st["connectivity"], "Unbekannt")),
            R.info("network.d.type", "Verbindungsart", "WLAN" if p["type"] == "wifi" else "Kabel (LAN)"),
            R.info("network.d.name", "Netzwerk", p["connection"]),
            R.info("network.d.ip", "IP-Adresse", ", ".join(p["ip4"]) or "Keine"),
            R.info("network.d.gateway", "Standard-Gateway", p["gateway"] or "Keines"),
            R.info("network.d.dns", "DNS", ", ".join(p["dns"]) or "Keiner"),
            R.info("network.d.mac", "MAC-Adresse", p["hwaddr"]),
        ]
        if p["speed"]:
            rows.append(R.info("network.d.speed", "Verbindungsgeschwindigkeit", p["speed"].replace("Mb/s", "Mbit/s")))
        return rows

    def _wifi_rows(self) -> list:
        st = self.state()
        if st["error"] or st["wifi_device"] is None:
            return [R.info("network.wifi.none", "Kein WLAN-Adapter gefunden.", enabled=False)]
        if not st["wifi"]:
            return [R.info("network.wifi.off", "WLAN ist ausgeschaltet.", enabled=False,
                           hint="Schalte WLAN unter Netzwerk ein, um Netze zu suchen.")]
        saved = nm.read_saved_wifi(self.deps.run_read)
        by_name = {c["name"]: c for c in saved}
        rows = [R.action("network.wifi.scan", "Netze suchen")]
        aps = nm.read_wifi_list(self.deps.run_read)
        if not aps:
            rows.append(R.info("network.wifi.empty", "Keine Netze gefunden.", enabled=False))
        for ap in aps:
            strength = f"Signal {ap['signal']} %"
            known = by_name.get(ap["ssid"])
            if ap["in_use"]:
                rows.append(R.info(f"network.wifi.ap:{_hex(ap['ssid'])}", ap["ssid"], "Verbunden", note=strength))
            elif known:
                rows.append(R.action(f"network.wifi.up:{known['uuid']}", ap["ssid"], note=f"Gemerkt · {strength}"))
            elif ap["enterprise"]:
                rows.append(R.info(f"network.wifi.ap:{_hex(ap['ssid'])}", ap["ssid"], enabled=False,
                                   note="Nicht unterstützt", hint="Netze mit Benutzeranmeldung (802.1X) gehen hier nicht."))
            elif ap["secured"]:
                rows.append(R.text(f"network.wifi.connect:{_hex(ap['ssid'])}", ap["ssid"], "", secret=True,
                                   note=f"Gesichert · {strength}", hint="Kennwort eingeben, um zu verbinden."))
            else:
                rows.append(R.action(f"network.wifi.open:{_hex(ap['ssid'])}", ap["ssid"], note=f"Offen · {strength}"))
        for c in saved:
            rows.append(R.confirm_action(
                f"network.wifi.forget:{c['uuid']}", f"Gemerktes Netz entfernen: {c['name']}",
                title=f"„{c['name']}“ entfernen?",
                text="Das Kennwort wird gelöscht. Du kannst das Netz später neu einrichten.",
                ok="Entfernen"))
        return rows

    # --- Schreiben -----------------------------------------------------------------------

    def _write(self, argv: list[str], failure: str, timeout: float = 30) -> None:
        rc, _out, err = self.deps.run_write(argv, timeout=timeout)
        self._cache.drop()
        if rc != 0:
            log.warning("%s: rc=%s %s", " ".join(argv[:6]), rc, (err or "").strip())
            raise SettingsError(failure)

    def _connect_new(self, ssid: str, password: str | None) -> dict:
        ap = next((a for a in nm.read_wifi_list(self.deps.run_read) if a["ssid"] == ssid), None)
        if ap is None:
            raise SettingsError("Dieses Netz ist nicht mehr in Reichweite.")
        if ap["enterprise"]:
            raise SettingsError("Netze mit Benutzeranmeldung werden nicht unterstützt.")
        if ap["secured"]:
            password = password or ""
            if not (8 <= len(password) <= 63 or (len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password))):
                raise SettingsError("Das Kennwort muss 8 bis 63 Zeichen lang sein.")
        else:
            password = None
        fn = self.deps.extra.get("wifi_connect")
        if fn is not None:
            ok, message = fn(ssid, password, ap["security"])
        elif test_mode():
            # Prüfmodus: nichts am Netz ändern. Das Kennwort steht bewusst nicht im Protokoll.
            log_write(self.deps.paths, f"nm wifi-connect ssid={ssid!r} security={ap['security']!r}")
            ok, message = True, ""
        else:
            ok, message = nm.wifi_connect(ssid, password, ap["security"])
        self._cache.drop()
        if not ok:
            raise SettingsError(message or "Verbindung fehlgeschlagen.")
        return {"message": f"Verbunden mit „{ssid}“.", "refresh": True}

    def set(self, row_id: str, value):
        if row_id == "network.wifi":
            self._write(["nmcli", "radio", "wifi", "on" if value else "off"],
                        "WLAN ließ sich nicht umschalten.")
            return {"refresh": True}
        if row_id.startswith("network.wifi.connect:"):
            return self._connect_new(_unhex(row_id.split(":", 1)[1]), str(value or ""))
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        if row_id == "network.test":
            return {"message": self._test(), "refresh": True}
        if row_id == "network.speed":
            return self._speed_action()
        if row_id == "network.wifi.scan":
            self._write(["nmcli", "device", "wifi", "rescan"], "Die Suche ließ sich nicht starten.")
            # Die Ergebnisse brauchen ein paar Sekunden; dann die Seite neu zeichnen lassen.
            self.deps.scheduler.later(4.0, lambda: (self._cache.drop(), self.deps.changed("network.wifi")))
            return {"message": "Netze werden gesucht …"}
        head, _, arg = row_id.partition(":")
        if head == "network.wifi.connect":
            return self._connect_new(_unhex(arg), str(value or ""))
        if head == "network.wifi.open":
            return self._connect_new(_unhex(arg), None)
        if head in ("network.wifi.up", "network.wifi.forget"):
            # Nur WLAN-Profile, die es wirklich gibt: nie ein Kabel-Profil anfassen.
            known = next((c for c in nm.read_saved_wifi(self.deps.run_read) if c["uuid"] == arg), None)
            if known is None:
                raise SettingsError("Dieses Netz ist nicht mehr gemerkt.")
            if head == "network.wifi.up":
                self._write(["nmcli", "-w", "30", "connection", "up", "uuid", arg],
                            "Verbindung fehlgeschlagen.", timeout=40)
                return {"message": f"Verbunden mit „{known['name']}“.", "refresh": True}
            self._write(["nmcli", "connection", "delete", "uuid", arg], "Das Netz ließ sich nicht entfernen.")
            return {"message": f"„{known['name']}“ wurde entfernt.", "refresh": True}
        raise SettingsError("Diese Aktion gibt es nicht.")

    # --- Geschwindigkeit (E111) --------------------------------------------------------

    def _last_speed(self) -> dict | None:
        with self._speed_lock:
            if not self._speed_loaded:
                self._speed_loaded = True
                self._speed_last = speedtest.load(self.deps.paths.state_dir)
            return self._speed_last

    def _speed_rows(self) -> list:
        with self._speed_lock:
            run, error = self._speed, self._speed_error
        if run is not None:
            # dieselbe Zeile: Kreuz bricht ab, der Fokus bleibt stehen
            return [R.action("network.speed", "Abbrechen", note=SPEED_RUNNING % run.percent, hint=SPEED_HINT)]
        if speedtest.target() is None:
            rows = [R.action("network.speed", "Geschwindigkeit messen", enabled=False, note=SPEED_TEST_MODE,
                             hint=SPEED_HINT)]
        else:
            rows = [R.action("network.speed", "Geschwindigkeit messen", note=error, hint=SPEED_HINT)]
        last = self._last_speed()
        if last:
            rows += [R.info("network.speed.down", "Download", speedtest.mbps_text(last["down_mbps"])),
                     R.info("network.speed.ping", "Antwortzeit", speedtest.ping_text(last["ping_ms"])),
                     R.info("network.speed.at", "Gemessen", speedtest.when_text(last["at"]))]
        return rows

    def _transfers(self) -> list[str]:
        """Was eine Messung verfälschen würde: die busy_checks der Verdrahtung (Installationen,
        Store-Downloads, Proton, Einrichtung …) und Browser-Downloads. Offene Spiele zählen nicht
        (sie sind angehalten, solange die Einstellungen offen sind). Ohne Verdrahtung (Tests):
        deps.extra["busy"]."""
        extra = self.deps.extra
        late = extra.get("late")
        busy = late("busy") if callable(late) else None
        main = late("main") if callable(late) else None
        if callable(busy) and callable(getattr(main, "call", None)):
            reasons = list(main.call(busy) or [])
        else:
            fn = extra.get("busy")
            reasons = list(fn() or []) if callable(fn) else []
        app = late("app") if callable(late) else None
        state = getattr(app, "state", None)
        downloads = state.value("downloads") if callable(getattr(state, "value", None)) else None
        if any(isinstance(d, dict) and d.get("state") in ("running", "queued") for d in downloads or []):
            reasons.append("Ein Download läuft.")
        return [str(r) for r in reasons if r]

    def _speed_action(self) -> dict:
        with self._speed_lock:
            run = self._speed
            if run is not None:
                self._speed = None
                run.cancel()
                log.info("Geschwindigkeit messen: abgebrochen")
                return {"message": "Messung abgebrochen.", "refresh": True}
        found = speedtest.target()
        if found is None:
            raise SettingsError(SPEED_TEST_MODE + ".")
        reasons = self._transfers()
        if reasons:
            log.info("Geschwindigkeit messen abgelehnt: %s", " | ".join(reasons))
            raise SettingsError(SPEED_BUSY)
        holder: dict = {}

        def progress(_percent: int) -> None:
            now = time.monotonic()
            with self._speed_lock:
                if self._speed is not holder.get("run") or now - self._speed_drawn < SPEED_REFRESH_S:
                    return
                self._speed_drawn = now
            self.deps.changed("network")

        def done(result: dict) -> None:
            with self._speed_lock:
                if self._speed is not holder.get("run"):
                    return                      # abgebrochen oder durch eine neue Messung ersetzt
                self._speed = None
                if result.get("ok"):
                    self._speed_last = {k: result.get(k) for k in ("down_mbps", "ping_ms", "at")}
                    self._speed_loaded = True
                    self._speed_error = ""
                elif not result.get("cancelled"):
                    self._speed_error = str(result.get("message") or speedtest.TEXT_UNREACHABLE)
            if result.get("ok"):
                try:
                    speedtest.save(self.deps.paths.state_dir, result)
                except OSError as e:
                    log.warning("speedtest.json nicht schreibbar: %s", e)
            self.deps.changed("network")

        run = speedtest.Measurement(*found, on_progress=progress, on_done=done)
        holder["run"] = run
        with self._speed_lock:
            if self._speed is not None:
                return {"refresh": True}        # gleichzeitig gedrückt: es misst schon
            self._speed = run
            self._speed_error = ""
            self._speed_drawn = time.monotonic()
        run.start()
        return {"refresh": True}

    def _test(self) -> str:
        self._cache.drop()
        st = self.state()
        if st["error"] or not st["primary"]:
            return "Nicht mit einem Netzwerk verbunden."
        word = nm.check_connectivity(self.deps.run_read)
        if word == "unknown":
            probe = self.deps.extra.get("internet_probe") or _probe_internet
            word = "full" if probe() else "none"
        self._cache.drop()
        if word == "full":
            return "Internetverbindung: erfolgreich."
        if word == "portal":
            return "Das Netz verlangt eine Anmeldung im Browser."
        return "Mit dem Netzwerk verbunden, aber das Internet ist nicht erreichbar."


def make_area(deps) -> NetworkArea:
    return NetworkArea(deps)
