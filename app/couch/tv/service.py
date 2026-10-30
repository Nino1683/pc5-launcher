"""Den Fernseher steuern (E142): alles, was der Teil tv (wiring_tv.py) und die Einstellungen brauchen.

Jede Methode blockiert (Netz mit kurzen Zeitgrenzen) und läuft nur in Arbeits-Threads, nie in der Hauptschleife. Ein
Schloss hält die Gespräche mit dem Fernseher auseinander. Die Verbindung zur Fernbedienung entsteht erst bei Bedarf und
bleibt nach dem letzten Befehl KEEP_S offen (so reagieren Lauter/Leiser im Control Center sofort), dann geht sie zu.

Gesprochen wird nur mit der Adresse des gekoppelten Fernsehers (bzw. beim Koppeln mit der gewählten), dazu die Suche
beim Koppeln und Wake-on-LAN. Antwortet die gemerkte Adresse nicht mehr (neue Adresse per DHCP), sucht der Dienst den
Fernseher unter seinem Namen aus der Suche neu. Im Prüfmodus nur 127.0.0.1 (_check).

Eingang wechseln: Mit gelernten Eingängen (ADB, „Eingänge genauer erkennen“) per App-Link
content://android.media.tv/passthrough/<Kennung>; ohne sie die Tasten HDMI 1 bis 4 (manche Fernseher übergehen sie)
bzw. das Eingangsmenü. Nach dem Wechsel auf den PC prüft der Dienst mit ADB nach VERIFY_S, ob es dabei blieb (ein
Gerät an einem anderen Eingang kann den Fernseher per CEC zurückholen), und versucht es bis zu VERIFY_TRIES mal neu.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .. import system
from . import (ADB_PORT, HDMI_KEYS, KEYS, PAIR_PORT, REMOTE_PORT, SERVICE, TEXT_CODE_FORMAT, TEXT_NOT_PAIRED,
               TEXT_TEST_HOST, TEXT_UNREACHABLE, TvError, VOLUME_KEYS, clean_code, clean_host, inputs, keys, net,
               passthrough_uri, valid_input_id)
from .adb import AdbClient
from .remote import Pairing, RemoteSession
from .store import Store

log = logging.getLogger("couch.tv")

KEEP_S = 30.0               # so lange bleibt die Fernbedienung nach dem letzten Befehl verbunden
FOUND_S = 15.0              # Ergebnis der Suche gilt so lange (die Seite zeichnet sich öfter neu)
POWER_S = 1.5               # so lange auf die Meldung „an/aus“ warten
VERIFY_S = 3.0              # nach dem Wechsel auf den PC: so spät nachsehen
VERIFY_TRIES = 2
WOL_EVERY_S = 4.0
OFF_ADB_S = 1.2             # ADB vor dem Ruhemodus: Zeitgrenze je Schritt
TEXT_BAD_HOST = ("Das ist keine gültige Adresse. Gib die IP-Adresse aus den Netzwerk-Einstellungen des Fernsehers ein "
                 "(vier Zahlen mit Punkten) oder seinen Namen im Netz.")


class TvService:
    def __init__(self, folder: str, ports: tuple[int, int, int] = (REMOTE_PORT, PAIR_PORT, ADB_PORT),
                 run_read: Callable | None = None, edid: Callable[[], bytes] | None = None, test_mode: bool = False,
                 neigh_path: str | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.folder = folder
        self.store = Store(folder)
        self.remote_port, self.pair_port, self.adb_port = ports
        self._run_read = run_read or system.run_read
        self._edid = edid or (lambda: b"")
        self._test = test_mode
        self._neigh = neigh_path
        self._sleep, self._clock = sleep, clock
        self._lock = threading.RLock()
        self._remote: RemoteSession | None = None
        self._idle: threading.Timer | None = None
        self._pairing: tuple | None = None          # (Pairing, host, name, hostname)
        self._found: tuple[list, float] = ([], -1e9)
        self._closed = False

    # ------------------------------------------------------------ Zustand (ohne Netz)
    @staticmethod
    def available() -> bool:
        return keys.available()

    def paired(self) -> bool:
        return self.store.paired()

    def pc_port(self) -> tuple[int | None, str]:
        """(Eingang des PCs, Quelle): „adb“ (lief beim Erkennen der Eingänge) vor „edid“ (CEC-Adresse)."""
        port, source = self.store.get("pc_port"), self.store.get("pc_from") or ""
        if isinstance(port, int) and source == "adb":
            return port, "adb"
        try:
            found = inputs.pc_port_from_edid(self._edid() or b"")
        except Exception:  # noqa: BLE001 (nur ein Hinweis)
            log.exception("EDID lesen")
            found = None
        if found:
            return found, "edid"
        return (port, source) if isinstance(port, int) else (None, "")

    def info(self) -> dict:
        data = self.store.all()
        port, source = self.pc_port()
        return {"paired": self.paired(), "name": data.get("name") or data.get("host") or "", "host": data.get("host", ""),
                "adb": bool(data.get("adb")), "ports": list(data.get("ports") or []), "pc_port": port, "pc_from": source,
                "wol": bool(data.get("mac")), "pairing": self._pairing is not None}

    # ------------------------------------------------------------ Grenzen
    def _check(self, host: str) -> str:
        if self._closed:
            raise TvError("Das Home endet gerade.", "Unavailable")
        if self._test and host != "127.0.0.1":
            raise TvError(TEXT_TEST_HOST, "BadRequest")
        return host

    def _client(self) -> tuple[str, str]:
        return keys.client_files(self.folder)

    # ------------------------------------------------------------ Suche und Koppeln
    def discover(self, force: bool = False) -> list[dict]:
        items, at = self._found
        if not force and self._clock() - at < FOUND_S:
            return list(items)
        found = net.discover(self._run_read, SERVICE)
        self._found = (found, self._clock())
        return list(found)

    def pick(self, host: str, name: str = "", hostname: str = "") -> str:
        """Fernseher wählen: „paired“, wenn er unser Zertifikat schon kennt (kein Code nötig), sonst „code“ (er zeigt
        jetzt einen Code, submit_code). TvError mit Satz, wenn er nicht antwortet."""
        try:
            host = clean_host(host, self._test)
        except ValueError:
            raise TvError(TEXT_TEST_HOST if self._test else TEXT_BAD_HOST, "BadRequest") from None
        self._check(host)
        cert, key = self._client()
        shown = name or host
        with self._lock:
            self._cancel_pairing()
            self._drop_remote()
            try:
                session = RemoteSession(host, self.remote_port, cert, key).open()
            except TvError as e:
                if e.code == "Unreachable":
                    raise TvError("„%s“ antwortet nicht. Ist der Fernseher an und im selben Netz wie der PC?" % shown,
                                  "Unreachable") from None
                session = None
            if session is not None:
                self._remote = session
                self._paired(host, name, hostname)
                self._touch()
                log.info("Fernseher kennt dieses Zertifikat schon: gekoppelt ohne Code")
                return "paired"
            pairing = Pairing(host, self.pair_port, cert, key, keys.cert_pem(self.folder))
            pairing.start()
            self._pairing = (pairing, host, name, hostname)
            return "code"

    def pairing_open(self) -> bool:
        p = self._pairing
        return p is not None and not p[0].expired()

    def pairing_name(self) -> str:
        p = self._pairing
        return (p[2] or p[1]) if p else ""

    def submit_code(self, text: str) -> str:
        """Code vom Fernseher eingeben. Liefert den Namen des Fernsehers. BadCode (Format, Tippfehler) lässt die
        Kopplung offen; alles andere schließt sie."""
        try:
            code = clean_code(text)
        except ValueError:
            raise TvError(TEXT_CODE_FORMAT, "BadCode") from None
        with self._lock:
            if self._pairing is None:
                raise TvError("Gerade läuft keine Kopplung. Wähle den Fernseher noch einmal aus.", "Expired")
            pairing, host, name, hostname = self._pairing
            try:
                pairing.finish(code)
            except TvError as e:
                if e.code != "BadCode":
                    self._pairing = None
                raise
            self._pairing = None
            self._paired(host, name, hostname)
            try:                                    # gleich einmal verbinden: MAC lernen, Verbindung steht
                self._open_locked(rediscover=False)
                self._touch()
            except TvError as e:
                log.info("Nach dem Koppeln keine Verbindung: %s", e.code)
            return name or host

    def new_code(self) -> None:
        """Neuen Code anfordern (die Kopplung beginnt neu)."""
        p = self._pairing
        if p is None:
            raise TvError("Gerade läuft keine Kopplung. Wähle den Fernseher noch einmal aus.", "Expired")
        self.pick(p[1], p[2], p[3])

    def _cancel_pairing(self) -> None:
        p, self._pairing = self._pairing, None
        if p is not None:
            p[0].close()

    def _paired(self, host: str, name: str, hostname: str) -> None:
        old = self.store.all()
        same = old.get("host") == host or (hostname and old.get("hostname") == hostname)
        values: dict[str, Any] = {"host": host,
                                  "name": (name or old.get("name")) if same else (name or None),
                                  "hostname": hostname or (old.get("hostname") if same else None),
                                  "paired_at": int(time.time())}
        if not same:                        # ein anderer Fernseher: Gelerntes gilt nicht mehr
            values.update(mac=None, iface=None, adb=None, ports=None, pc_port=None, pc_from=None)
        self.store.update(**values)
        self._learn_mac(host)

    def _learn_mac(self, host: str) -> None:
        found = net.neighbour(host, self._neigh)
        if found and (found[0] != self.store.get("mac") or found[1] != self.store.get("iface")):
            self.store.update(mac=found[0], iface=found[1])

    def forget(self) -> None:
        """Kopplung vergessen: tv.json leer, Verbindung zu. Die Schlüssel bleiben."""
        with self._lock:
            self._cancel_pairing()
            self._drop_remote()
            self.store.clear()
            self._found = ([], -1e9)

    # ------------------------------------------------------------ Fernbedienung
    def _open_locked(self, timeout: float = 4.0, rediscover: bool = True) -> RemoteSession:
        if self._remote is not None and self._remote.alive:
            return self._remote
        self._drop_remote()
        host = self.store.get("host")
        if not host or not self.paired():
            raise TvError(TEXT_NOT_PAIRED, "NotPaired")
        self._check(host)
        cert, key = self._client()
        try:
            session = RemoteSession(host, self.remote_port, cert, key).open(timeout)
        except TvError as e:
            moved = self._moved(host) if e.code == "Unreachable" and rediscover else None
            if not moved:
                raise
            session = RemoteSession(moved, self.remote_port, cert, key).open(timeout)
            host = moved
        self._remote = session
        if not self.store.get("mac"):
            self._learn_mac(host)
        return session

    def _moved(self, host: str) -> str | None:
        """Neue Adresse desselben Fernsehers (Suche nach Rechner- bzw. Anzeigenamen)."""
        hostname, name = self.store.get("hostname") or "", self.store.get("name") or ""
        if not hostname and not name:
            return None
        for item in self.discover(force=True):
            if item.get("host") and item["host"] != host and (
                    (hostname and item.get("hostname") == hostname) or (not hostname and item.get("name") == name)):
                try:
                    new = self._check(clean_host(item["host"], self._test))
                except (ValueError, TvError):
                    continue
                log.info("Fernseher hat eine neue Adresse")
                self.store.update(host=new)
                return new
        return None

    def _touch(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
        timer = threading.Timer(KEEP_S, self._idle_close)
        timer.daemon = True
        self._idle = timer
        timer.start()

    def _idle_close(self) -> None:
        if self._lock.acquire(timeout=5.0):
            try:
                # Nur der neueste Zeitgeber schließt: cancel() hält einen schon laufenden nicht auf. Wartete er hier,
                # während ein Befehl das Schloss hielt, schloss er sonst gleich danach dessen frische Verbindung.
                if self._idle is threading.current_thread():
                    self._idle = None
                    self._drop_remote()
            finally:
                self._lock.release()

    def _drop_remote(self) -> None:
        remote, self._remote = self._remote, None
        if remote is not None:
            remote.close()

    def close(self) -> None:
        """Am Ende des Home bzw. beim Ausschalten des Schalters. Wirft nie."""
        self._closed = True
        if self._idle is not None:
            self._idle.cancel()
        # Hält ein Gespräch das Schloss (Rückfrage am Fernseher bis 60 s), nicht warten: Die Fäden sind Daemons
        if self._lock.acquire(timeout=0.5):
            try:
                self._cancel_pairing()
                self._drop_remote()
            finally:
                self._lock.release()

    def reopen(self) -> None:
        """Nach dem Wiedereinschalten des Schalters."""
        self._closed = False

    def reset(self) -> None:
        """Verbindung zur Fernbedienung verwerfen (nach dem Aufwachen: Sie sieht lebendig aus, ist aber tot)."""
        if self._lock.acquire(timeout=2.0):
            try:
                self._drop_remote()
            finally:
                self._lock.release()

    # ------------------------------------------------------------ ADB (wahlfrei)
    def _adb(self, allow_prompt: bool = False, on_prompt: Callable[[], None] | None = None,
             step_s: float | None = None) -> AdbClient:
        host = self.store.get("host") or ""
        if not host:
            raise TvError(TEXT_NOT_PAIRED, "NotPaired")      # vor _check: ohne Adresse ist nichts gekoppelt
        self._check(host)
        kw = {"connect_s": step_s, "step_s": step_s} if step_s else {}
        client = AdbClient(host, self.adb_port, keys.adb_key(self.folder), **kw)
        client.connect(allow_prompt=allow_prompt, on_prompt=on_prompt)
        return client

    def _dump(self, allow_prompt: bool = False, on_prompt: Callable[[], None] | None = None,
              step_s: float | None = None) -> dict:
        client = self._adb(allow_prompt, on_prompt, step_s)
        try:
            return inputs.parse_tv_input(client.run("tv_input"))
        finally:
            client.close()

    def learn_inputs(self, on_prompt: Callable[[], None] | None = None) -> dict:
        """„Eingänge genauer erkennen“: über ADB (fragt beim ersten Mal am Fernseher) Eingänge, Namen und den
        laufenden Eingang lesen. Der läuft gerade, also ist er der Eingang des PCs (der Nutzer sieht ja diese Seite)."""
        with self._lock:
            parsed = self._dump(allow_prompt=True, on_prompt=on_prompt)
            if not parsed["ports"]:
                self.store.update(adb=None, ports=None)
                raise TvError("Der Fernseher nennt keine HDMI-Eingänge. Der PC5 Launcher schaltet weiter mit den "
                              "Tasten HDMI 1 bis 4.", "NoInputs")
            current = (parsed.get("current") or {}).get("port")
            values: dict[str, Any] = {"adb": True, "ports": parsed["ports"]}
            if current:
                values.update(pc_port=current, pc_from="adb")
            self.store.update(**values)
            return {"ports": parsed["ports"], "current": current}

    def current_port(self, step_s: float | None = None) -> int | None:
        """Laufender Eingang über ADB (ohne Rückfrage am Fernseher); None, wenn unbekannt."""
        if not self.store.get("adb"):
            return None
        with self._lock:
            try:
                parsed = self._dump(step_s=step_s)
            except TvError as e:
                log.info("Eingang nicht lesbar: %s", e.code)
                return None
            if parsed["ports"]:
                self.store.update(ports=parsed["ports"])
            return (parsed.get("current") or {}).get("port")

    # ------------------------------------------------------------ Control Center
    def panel(self) -> dict:
        """Zustand für die Tafel „Fernseher“: {reachable, on, inputs, current, pc_port, adb, wol, error?}."""
        info = self.info()
        out = {"reachable": False, "on": None, "pc_port": info["pc_port"], "adb": info["adb"], "wol": info["wol"],
               "current": None, "inputs": self._input_rows(info)}
        with self._lock:
            try:
                session = self._open_locked()
            except TvError as e:
                out["error"], out["code"] = e.message, e.code
                return out
            out["reachable"] = True
            out["on"] = session.wait_power(POWER_S)
            self._touch()
        if info["adb"] and out["on"] is not False:
            out["current"] = self.current_port(step_s=2.0)
            out["inputs"] = self._input_rows(self.info())
        return out

    def _input_rows(self, info: dict) -> list[dict]:
        learned = [p for p in info.get("ports") or [] if isinstance(p, dict) and isinstance(p.get("port"), int)]
        if learned:
            return [{"port": p["port"], "name": str(p.get("name") or ""), "state": p.get("state") or "connected",
                     "pc": p["port"] == info.get("pc_port")} for p in learned]
        return [{"port": n, "name": "", "state": "", "pc": n == info.get("pc_port")} for n in sorted(HDMI_KEYS)]

    def volume(self, what: str) -> None:
        if what not in VOLUME_KEYS:
            raise TvError("Ungültiger Aufruf.", "BadRequest")
        with self._lock:
            self._open_locked().key(KEYS[what])
            self._touch()

    def power(self, on: bool) -> str:
        """Aus: nur, wenn der Fernseher „an“ meldet (die Taste schaltet um). An: Wake-on-LAN und KEYCODE_WAKEUP."""
        if on:
            return self.wake(budget=10.0, switch=False)
        with self._lock:
            session = self._open_locked()
            state = session.wait_power(POWER_S)
            if state is False:
                raise TvError("Der Fernseher ist schon aus.", "AlreadyOff")
            if state is None:
                raise TvError("Der Fernseher meldet nicht, ob er an ist. Damit er nicht versehentlich angeht, schickt "
                              "der PC5 Launcher nichts.", "Unknown")
            session.key(KEYS["power"])
            self._touch()
        return "off"

    def switch(self, port: int | None, menu: bool = False) -> str:
        """Eingang wählen (port) bzw. das Eingangsmenü öffnen. Auf den PC: Nachsehen im Hintergrund (ADB)."""
        with self._lock:
            session = self._open_locked()
            if menu:
                session.key(KEYS["input_menu"])
                self._touch()
                return "menu"
            how = self._send_port(session, port)
            self._touch()
        if port == self.pc_port()[0] and self.store.get("adb"):
            threading.Thread(target=self._verify_pc, args=(port,), name="tv-verify", daemon=True).start()
        return how

    def _send_port(self, session: RemoteSession, port: Any, adb_last: bool = False) -> str:
        if isinstance(port, bool) or not isinstance(port, int):
            raise TvError("Ungültiger Aufruf.", "BadRequest")
        entry = next((p for p in self.store.get("ports") or [] if isinstance(p, dict) and p.get("port") == port
                      and valid_input_id(p.get("id"))), None)
        if entry is not None:
            if adb_last:
                try:
                    client = self._adb(step_s=2.0)
                    try:
                        client.switch(entry["id"])
                    finally:
                        client.close()
                    return "adb"
                except TvError as e:
                    log.info("Umschalten über ADB: %s", e.code)
            session.app_link(passthrough_uri(entry["id"]))
            return "link"
        if port in HDMI_KEYS:
            session.key(HDMI_KEYS[port])
            return "key"
        raise TvError("Diesen Eingang kann der PC5 Launcher nicht wählen.", "BadRequest")

    def _verify_pc(self, port: int, deadline: float | None = None) -> bool | None:
        """Nach dem Wechsel auf den PC: blieb es dabei? Sonst bis zu VERIFY_TRIES mal neu (zuletzt über ADB)."""
        for attempt in range(VERIFY_TRIES + 1):
            self._sleep(VERIFY_S)
            if self._closed or (deadline is not None and self._clock() > deadline):
                return None
            current = self.current_port(step_s=2.0)
            if current is None:
                return None
            if current == port:
                return True
            if attempt == VERIFY_TRIES:
                log.warning("Fernseher bleibt nicht auf dem Eingang des PCs")
                return False
            log.info("Fernseher zeigt einen anderen Eingang: neuer Versuch")
            try:
                with self._lock:
                    self._send_port(self._open_locked(rediscover=False), port, adb_last=attempt == VERIFY_TRIES - 1)
                    self._touch()
            except TvError as e:
                log.info("Neuer Versuch: %s", e.code)
                return None
        return None

    # ------------------------------------------------------------ automatisch
    def off_for_sleep(self, mode: str, budget: float, cancelled: threading.Event | None = None) -> str:
        """Vor Ruhemodus und Ausschalten (höchstens budget Sekunden): ausschalten, wenn er an ist und (mode „pc“) den
        PC zeigt. Liefert, was geschah. Nach cancelled bzw. budget wird nichts mehr gesendet."""
        if mode not in ("pc", "always") or not self.paired():
            return "skip"
        end = self._clock() + budget

        def late() -> bool:
            return (cancelled is not None and cancelled.is_set()) or self._clock() > end
        if not self._lock.acquire(timeout=min(1.0, budget)):
            return "busy"
        try:
            try:
                session = self._open_locked(timeout=max(0.5, min(2.0, end - self._clock())), rediscover=False)
            except TvError as e:
                return "unreachable" if e.code == "Unreachable" else "rejected"
            state = session.wait_power(max(0.1, min(1.0, end - self._clock())))
            if state is not True:
                return "not-on" if state is False else "unknown"
            if mode == "pc":
                port = self.pc_port()[0]
                if port is None or not self.store.get("adb"):
                    return "no-adb"
                current = self.current_port(step_s=max(0.3, min(OFF_ADB_S, end - self._clock())))
                if current != port:
                    return "other-input"
            if late():
                return "late"
            session.key(KEYS["power"])
            log.info("Fernseher ausgeschaltet (vor dem Ruhemodus bzw. Ausschalten)")
            return "sent"
        finally:
            self._lock.release()

    def wake(self, budget: float = 20.0, switch: bool = True, cancelled: threading.Event | None = None) -> str:
        """Wecken (Wake-on-LAN, KEYCODE_WAKEUP) und, mit switch, auf den Eingang des PCs. Versucht es budget Sekunden
        lang (nach dem Aufwachen braucht das Netz ein paar Sekunden)."""
        if not self.paired():
            raise TvError(TEXT_NOT_PAIRED, "NotPaired")
        self.reset()                            # eine Verbindung von vor dem Ruhemodus ist tot, auch wenn sie lebt
        end = self._clock() + budget
        mac, iface = self.store.get("mac") or "", self.store.get("iface") or ""
        sent_at: float | None = None
        session = None
        attempt = 0
        last = TvError(TEXT_UNREACHABLE, "Unreachable")
        while not self._closed and self._clock() < end and not (cancelled is not None and cancelled.is_set()):
            if mac and (sent_at is None or self._clock() - sent_at >= WOL_EVERY_S):
                net.wake_on_lan(mac, iface)
                sent_at = self._clock()
            attempt += 1
            with self._lock:
                try:
                    session = self._open_locked(timeout=max(0.5, min(3.0, end - self._clock())),
                                                rediscover=attempt == 3)
                except TvError as e:
                    # Gerade aufgewacht, kann die Fernbedienung noch Verbindungen abweisen: weiter versuchen
                    session, last = None, e
                    if e.code not in ("Unreachable", "NotPaired"):
                        raise
            if session is not None:
                break
            self._sleep(1.0)
        if session is None:
            raise last
        with self._lock:
            state = session.wait_power(POWER_S)
            if state is not True:
                session.key(KEYS["wakeup"])       # weckt nur, schaltet nie aus
            self._touch()
        port = self.pc_port()[0]
        if not switch or port is None:
            return "woke"
        if self.store.get("adb") and self.current_port(step_s=2.0) == port:
            return "on-pc"
        with self._lock:
            try:
                self._send_port(self._open_locked(rediscover=False), port)
                self._touch()
            except TvError as e:
                log.info("Umschalten nach dem Wecken: %s", e.code)
                return "woke"
        if self.store.get("adb"):
            self._verify_pc(port, deadline=end + VERIFY_S * (VERIFY_TRIES + 2))
        return "switched"
