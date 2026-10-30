"""Fernseher steuern (E142): Verdrahtung, eingehängt über wiring.PARTS (nach discord).

Wunsch 2026-10-30: „Ich wollte die Lautstärke regeln, ferner aus- und Eingang wechseln“ – „Ok aber bedenke beim committen
auf github. Dass keine persönlichen daten mitgenommen werden. Und es muss bei anderen funktionieren es hat ja nicht
jeder mein setup“. Was das Paket couch/tv kann und darf, steht dort; hier hängt es am Kern.

Vorgabe aus (tv.enabled). Aus heißt: keine Suche, keine Verbindung, kein Symbol im Control Center, nichts beim
Ruhemodus oder Aufwachen. Erst eingeschaltet und gekoppelt spricht der Teil mit dem Fernseher, und nur über
Arbeits-Threads mit kurzen Zeitgrenzen (TvService); die Hauptschleife wartet nie.

  Einstellungen   settings/tvctl.py (Bildschirm und Video › Fernseher steuern) ruft die Methoden unten über
                  deps.extra["late"]("tv_part"): status, discover, pick, pick_address, submit_code, new_code,
                  learn_inputs, forget (alle blockierend, im Thread der Einstellungen)
  Control Center  RPC tv.state, tv.key, tv.input, tv.power (Pool); Symbol „Fernseher“ nur mit sys.state.tv
                  {enabled: true, paired: true}
  Ruhemodus       Power.before_system (Arbeits-Thread von power.py, vor logind; nicht vor dem Neustart, da
                  bleibt er an, Wunsch 2026-10-30): tv.off_with_pc „pc“ (nur wenn er
                  den PC zeigt; braucht die gelernten Eingänge) bzw. „always“; höchstens OFF_BUDGET_S, danach wird
                  nichts mehr gesendet und der Ruhemodus wartet nicht länger
  Aufwachen       wiring.resume_hooks (ResumeWatch, Hauptschleife) und nach dem Bereit-Signal einmal je echtem Start
                  des PCs (boot_id, Laufzeit unter BOOT_WINDOW_S): tv.wake_with_pc -> Wake-on-LAN, KEYCODE_WAKEUP, auf
                  den Eingang des PCs; bis WAKE_BUDGET_S lang (das Netz braucht nach dem Aufwachen ein paar Sekunden)

Prüfmodus: nie das echte Netz. Verbindungen nur zu 127.0.0.1 (Ports aus COUCH_TEST_TV_PORTS „fernbedienung,koppeln,
adb“), Suche nur aus COUCH_TEST_TV_FIND, Nachbartabelle nur aus COUCH_TEST_TV_NEIGH, Wake-on-LAN nur an
127.0.0.1:COUCH_TEST_TV_WOL, EDID nur aus COUCH_TEST_TV_EDID (Datei), Start des PCs nur aus COUCH_TEST_TV_BOOT
(„boot_id:sekunden“); Tests stecken dasselbe über wiring.tv_overrides ein.
"""
from __future__ import annotations

import os
import threading
from typing import Any

from . import log, paths, tv
from .rpc import RpcError
from .tv import TvError, keys, net
from .tv.service import TvService

_log = log.get("wiring.tv")

OFF_BUDGET_S = 3.0              # vor dem Ruhemodus bzw. Ausschalten höchstens so lange
WAKE_BUDGET_S = 20.0            # nach dem Aufwachen so lange versuchen
BOOT_WINDOW_S = 600.0           # „nach einem echten Start“: der PC läuft noch nicht länger als das
TEXT_PROMPT = "Bestätige am Fernseher „Debugging zulassen?“, am besten mit „Immer von diesem Computer zulassen“."


class TvPart:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.config = wiring.config
        self._service: TvService | None = None
        self._make_lock = threading.Lock()
        self._wake_lock = threading.Lock()

    # ------------------------------------------------------------ Dienst
    def _overrides(self) -> dict:
        return dict(getattr(self.wiring, "tv_overrides", None) or {})

    def _ports(self) -> tuple[int, int, int]:
        over = self._overrides().get("ports")
        if over:
            return tuple(over)
        if paths.test_mode():
            raw = (os.environ.get("COUCH_TEST_TV_PORTS") or "").split(",")
            if len(raw) == 3 and all(p.strip().isdigit() for p in raw):
                return tuple(int(p) for p in raw)
        return (tv.REMOTE_PORT, tv.PAIR_PORT, tv.ADB_PORT)

    def _edid(self) -> bytes:
        over = self._overrides().get("edid")
        if callable(over):
            return over() or b""
        if paths.test_mode():
            path = os.environ.get("COUCH_TEST_TV_EDID") or ""
            try:
                with open(path, "rb") as f:
                    return f.read(4096)
            except OSError:
                return b""
        from .settings import drm_modes
        try:
            env = paths.session_env()
        except Exception:  # noqa: BLE001
            env = {}
        connector = drm_modes.tv_connector(env)
        return drm_modes.read_edid(connector) if connector else b""

    def service(self) -> TvService:
        with self._make_lock:
            if self._service is None:
                over = self._overrides()
                self._service = TvService(paths.tv_dir(), ports=self._ports(), edid=self._edid,
                                          test_mode=paths.test_mode(),
                                          **{k: over[k] for k in ("sleep", "clock", "neigh_path") if k in over})
            return self._service

    def enabled(self) -> bool:
        return tv.enabled(self.config)

    def paired(self) -> bool:
        try:
            return self.service().paired()
        except Exception:  # noqa: BLE001 (unlesbare Datei: dann eben nicht gekoppelt)
            _log.exception("Kopplung lesen")
            return False

    def _need(self) -> TvService:
        if not self.enabled():
            raise TvError(tv.TEXT_OFF, "TvOff")
        if not keys.available():
            raise TvError(keys.MISSING, "Unavailable")
        return self.service()

    # ------------------------------------------------------------ Zustand
    def publish(self) -> None:
        """sys.state.tv (Hauptschleife): aus {enabled: false}, sonst {enabled, paired}."""
        value: dict = {"enabled": False}
        if self.enabled():
            # nur tv.json lesen: cryptography zu laden kostete in der Hauptschleife Zeit (ohne es sagt es die Tafel)
            value = {"enabled": True, "paired": self.paired()}
        try:
            self.app.state.update(tv=value)
        except Exception:  # noqa: BLE001
            _log.exception("sys.state.tv")

    def apply(self) -> None:
        """Ein Schlüssel tv.* hat sich geändert (Hauptschleife)."""
        svc = self._service
        if svc is not None:
            if self.enabled():
                svc.reopen()
            else:
                threading.Thread(target=svc.close, name="tv-close", daemon=True).start()
        self.publish()

    def status(self) -> dict:
        """Für die Einstellungen (ohne Netz)."""
        out = {"available": keys.available(), "enabled": self.enabled(), "can_discover": net.can_discover(),
               "paired": False, "name": "", "adb": False, "ports": [], "pc_port": None, "pc_from": "", "wol": False,
               "pairing": False, "pairing_name": ""}
        if not out["available"] or not out["enabled"]:
            return out                          # aus: nicht einmal tv.json lesen
        try:
            svc = self.service()
            out.update(svc.info())
            out["pairing"] = svc.pairing_open()
            out["pairing_name"] = svc.pairing_name()
        except Exception:  # noqa: BLE001
            _log.exception("Zustand des Fernsehers")
        return out

    # ------------------------------------------------------------ Einstellungen (blockierend)
    def discover(self, force: bool = False) -> list[dict]:
        return self._need().discover(force)

    def pick(self, host: str, name: str = "", hostname: str = "") -> str:
        result = self._need().pick(host, name, hostname)
        self.wiring.main.post(self.publish)
        return result

    def pick_address(self, text: str) -> str:
        return self.pick(text, "", "")

    def submit_code(self, text: str) -> str:
        name = self._need().submit_code(text)
        self.wiring.main.post(self.publish)
        return name

    def new_code(self) -> None:
        self._need().new_code()

    def learn_inputs(self) -> dict:
        return self._need().learn_inputs(on_prompt=lambda: self._toast(tv.TITLE, TEXT_PROMPT))

    def forget(self) -> None:
        if keys.available():
            self.service().forget()
        self.wiring.main.post(self.publish)

    # ------------------------------------------------------------ Control Center (Pool)
    def _rpc(self, fn, *args):
        def run():
            try:
                return fn(*args)
            except TvError as e:
                raise RpcError(e.message, e.code) from None
        return self.wiring._bg(run)

    def panel(self) -> dict:
        svc = self._need()
        if not svc.paired():
            raise TvError(tv.TEXT_NOT_PAIRED, "NotPaired")
        info = svc.info()
        out = svc.panel()
        out.update(name=info["name"], hdmi_keys=not info["adb"])
        return out

    def key(self, what: str) -> dict:
        self._need().volume(what)
        return {"ok": True}

    def switch(self, params: dict) -> dict:
        port = params.get("port")
        menu = params.get("menu") is True
        if not menu and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= tv.MAX_PORT):
            raise TvError("Ungültiger Aufruf.", "BadRequest")
        how = self._need().switch(None if menu else port, menu=menu)
        return {"ok": True, "how": how}

    def power(self, params: dict) -> dict:
        on = params.get("on")
        if not isinstance(on, bool):
            raise TvError("Ungültiger Aufruf.", "BadRequest")
        return {"ok": True, "result": self._need().power(on)}

    def _toast(self, title: str, body: str) -> None:
        """Kurze Einblendung ohne Eintrag in der Liste (wiring.toast). Aus jedem Thread."""
        from . import wiring as wiring_mod
        wiring_mod.toast(self.wiring, title, body, "tv", "tv")

    # ------------------------------------------------------------ Ruhemodus und Ausschalten
    def before_power(self, action: str) -> None:
        """Power.before_system (Arbeits-Thread): vor Ruhemodus und Ausschalten höchstens OFF_BUDGET_S (der Neustart lässt ihn an)."""
        if action not in ("off", "suspend"):
            return
        mode = tv.off_mode(self.config)
        if mode == "never" or not keys.available() or not self.paired():
            return
        svc = self.service()
        cancelled, done, box = threading.Event(), threading.Event(), {}

        def run() -> None:
            try:
                box["result"] = svc.off_for_sleep(mode, OFF_BUDGET_S, cancelled)
            except Exception as e:  # noqa: BLE001 (der Ruhemodus kommt in jedem Fall)
                box["result"] = "fehler: %s" % type(e).__name__
            finally:
                done.set()
        threading.Thread(target=run, name="tv-off", daemon=True).start()
        if not done.wait(OFF_BUDGET_S + 0.3):
            cancelled.set()
            _log.info("Fernseher vor %s: keine Antwort binnen %.0f s, es geht ohne weiter", action, OFF_BUDGET_S)
            return
        _log.info("Fernseher vor %s: %s", action, box.get("result"))

    # ------------------------------------------------------------ Aufwachen und Start
    def on_resume(self) -> None:
        """ResumeWatch (Hauptschleife): eigener Thread, die Hauptschleife wartet nicht. Eine Verbindung von vor dem
        Ruhemodus gilt nicht mehr (sie sähe lebendig aus, Tasten gingen ins Leere). keys.available() lädt beim ersten
        Mal cryptography: erst im Thread fragen, nie in der Hauptschleife."""
        if tv.wake_wanted(self.config):
            threading.Thread(target=self._resume_work, name="tv-wake", daemon=True).start()
        elif self._service is not None:
            threading.Thread(target=self._service.reset, name="tv-reset", daemon=True).start()

    def _resume_work(self) -> None:
        if keys.available():
            # wake() verwirft die alte Verbindung selbst (ein eigener Thread dafür könnte die frische treffen)
            self._wake("Aufwachen")
        elif self._service is not None:
            self._service.reset()

    def _boot(self) -> tuple[str, float] | None:
        """(boot_id, Sekunden seit dem Start) oder None."""
        over = self._overrides().get("boot")
        if callable(over):
            return over()
        if paths.test_mode():
            raw = os.environ.get("COUCH_TEST_TV_BOOT") or ""
            ident, _, secs = raw.partition(":")
            try:
                return (ident, float(secs)) if ident else None
            except ValueError:
                return None
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
                ident = f.read(64).strip()
            with open("/proc/uptime", encoding="ascii") as f:
                up = float(f.read(64).split()[0])
        except (OSError, ValueError, IndexError):
            return None
        return ident, up

    def after_ready(self) -> None:
        if tv.wake_wanted(self.config):         # keys.available() erst im Thread (lädt cryptography)
            threading.Thread(target=self._boot_wake, name="tv-boot", daemon=True).start()

    def _boot_wake(self) -> None:
        """Einmal je echtem Start des PCs (nicht bei jedem Neustart des PC5 Launchers)."""
        if not keys.available():
            return
        boot = self._boot()
        if not boot or not self.paired():
            return
        ident, uptime = boot
        svc = self.service()
        if svc.store.get("boot") == ident:
            return
        svc.store.update(boot=ident)
        if uptime > BOOT_WINDOW_S:
            return
        self._wake("Start des PCs")

    def _wake(self, why: str) -> None:
        if not self._wake_lock.acquire(blocking=False):
            return                              # weckt schon
        try:
            if not tv.wake_wanted(self.config) or not self.paired():
                return
            result = self.service().wake(budget=WAKE_BUDGET_S, switch=True)
            _log.info("Fernseher nach %s: %s", why, result)
        except TvError as e:
            _log.info("Fernseher nach %s: %s", why, e.code)
        except Exception:  # noqa: BLE001
            _log.exception("Fernseher wecken")
        finally:
            self._wake_lock.release()

    # ------------------------------------------------------------ Ende
    def stop(self) -> None:
        svc = self._service
        if svc is not None:
            svc.close()


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if callable(add):
        add(dict(tv.DEFAULTS))
    part = TvPart(wiring)
    wiring.tv_part = part                   # Einstellungen › Bildschirm und Video › Fernseher steuern (settings/tvctl.py)

    rpc = app.rpc
    rpc.register("tv.state", lambda params: part._rpc(part.panel))
    rpc.register("tv.key", lambda params: part._rpc(part.key, str((params or {}).get("what") or "")))
    rpc.register("tv.input", lambda params: part._rpc(part.switch, dict(params or {})))
    rpc.register("tv.power", lambda params: part._rpc(part.power, dict(params or {})))

    on_change = getattr(wiring.config, "on_change", None)
    if callable(on_change):
        on_change("tv.", lambda _key, _value: wiring.main.post(part.apply))
    power = getattr(app, "power", None)
    hooks = getattr(power, "before_system", None)
    if isinstance(hooks, list):
        hooks.append(part.before_power)
    resume = getattr(wiring, "resume_hooks", None)
    if isinstance(resume, list):
        resume.append(part.on_resume)
    wiring.after_ready.append(part.after_ready)
    wiring.shutdown_hooks.append(part.stop)
    part.publish()
