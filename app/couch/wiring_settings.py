"""Verdrahtung, Teil „settings“: Einstellungen, Ton, Leistung, Bild, Netz, System-Update und
alles, was davon in sys.state steht (Bauplan E21, E22, E23, E32, E42).

wiring.py ruft die Funktionen hier aus seinen Methoden (_ui_settings, _perf, _audio, _settings,
_state, started …; jede einzeln abgesichert) und am Ende extend(wiring). Andere Teile rufen
resolve_proton(app).

Was hier gilt:
- Nichts Blockierendes in der Hauptschleife. Ton und Leistung haben je einen eigenen
  Arbeits-Thread (Lane): Ihre Aufträge laufen der Reihe nach und warten nie hinter langen
  Aufträgen des gemeinsamen Pools (Bilder laden, Update suchen). So reagieren die
  Lautstärketasten sofort, und „App vorn / Home vorn“ kommt bei Gamescope in der richtigen
  Reihenfolge an.
- Code der Einstellungsbereiche läuft NIE in der Hauptschleife, auch nicht aus Zeitgebern
  (OffMainScheduler). Die Bereiche halten Sperren, während sie über die Stellvertreter
  (wiring.gs, wiring.config) auf die Hauptschleife warten; liefe derselbe Code auch dort,
  stünden beide, bis der Supervisor das Home für tot hält.
- Auf andere Teile (wiring_library, wiring_install, wiring_launch) wird erst zur Aufrufzeit
  zugegriffen. Fehlen sie, gibt es einen Ersatz oder einen deutschen Satz, nie einen Absturz.
- Alles Schreibende geht durch system.run_write (wiring.write3); im Prüfmodus läuft nichts
  gegen das echte System, auch nicht die Suche nach Updates.
"""
from __future__ import annotations

import importlib
import os
import queue
import re
import subprocess
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, Callable

from . import log, paths, sinknote
from . import wiring as wiring_mod
from .rpc import RpcError

_log = log.get("wiring.settings")

STATE_TICK_S = 60.0             # HDR-Zustand nachlesen; Ton und Netz nur, wenn ihr Beobachter fehlt
SIZE_SETTLE_S = 3.0             # so lange muss die Wurzelgröße stehen (wie gs/focus.py, Befund 19)
NET_DEBOUNCE_S = 1.0            # nmcli monitor meldet einen Wechsel in mehreren Zeilen
UPDATE_FIRST_S = 45.0           # tägliche Update-Suche: erst, wenn der Start vorbei ist
UPDATE_AGAIN_S = 6 * 3600.0     # danach alle paar Stunden nachsehen, ob wieder ein Tag um ist
UPDATE_RESUME_S = 60.0          # nach dem Aufwachen: dann ist das Netz wieder da (E99)
LOGIND_FRESH_S = 30.0           # Sperren bei logind: so alt darf der gelesene Stand sein, dann neu lesen
LOGIND_STALE_S = 120.0          # älter: unbekannt, der Ruhemodus wartet einen Takt auf den neuen Stand
PROTON_CACHE_S = 30.0
GENERIC_ERROR = "Das hat nicht geklappt."
UNAVAILABLE = "Das ist gerade nicht verfügbar."
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

read3 = wiring_mod.read3
write3 = wiring_mod.write3


# ================================================================ Werkzeug
class Lane:
    """Ein eigener Arbeits-Thread, der seine Aufträge der Reihe nach erledigt. submit() liefert
    ein Future (wie app.background), der Thread entsteht beim ersten Auftrag und ruht sonst."""

    def __init__(self, name: str):
        self.name = name
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        fut: Future = Future()
        with self._lock:
            if self._closed:
                fut.set_exception(RuntimeError("Das Home endet gerade"))
                return fut
            self._queue.put((fut, fn, args, kwargs))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
        return fut

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fut, fn, args, kwargs = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as e:      # geht an den, der auf das Future wartet
                    fut.set_exception(e)
            # Der ruhende Thread hielte sonst Auftrag und Future samt Rückrufen bis zum nächsten
            # Auftrag fest (wie system.Pool, Absturzanalyse H1, E99)
            item = fut = fn = args = kwargs = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)


class OffMainScheduler:
    """Planer für Ton-Dienst und Einstellungsbereiche: Die Uhr gehört dem Grund-Planer (GLib im
    Home, eine vorstellbare Uhr im Test), der Rückruf aber läuft immer in einem Arbeits-Thread
    (siehe Modulkopf). Fehler eines Rückrufs stehen im Protokoll."""

    def __init__(self, base: Any):
        self.base = base
        self.closed = False             # das Home endet: abgelaufene Zeitgeber tun nichts mehr

    def later(self, seconds: float, fn: Callable[[], None]) -> Any:
        return self.base.later(seconds, lambda: None if self.closed else self.background(fn))

    def cancel(self, handle: Any) -> None:
        if handle is not None:
            self.base.cancel(handle)

    def background(self, fn: Callable[[], None]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception:
                _log.exception("Hintergrundarbeit %r", fn)
        self.base.background(run)


class NetWatch:
    """`nmcli monitor` als eigenes Kind (nur lesend): Jede Zeile heißt, am Netz hat sich etwas
    getan. Der Verdrahter liest dann den Zustand neu (entprellt)."""

    ARGV = ["env", "LC_ALL=C", "nmcli", "monitor"]

    def __init__(self, on_event: Callable[[], None], popen: Callable = subprocess.Popen):
        self._on_event = on_event
        self._popen = popen
        self._proc: Any = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    def start(self) -> bool:
        if self._proc is not None or self._stopped:
            return self._proc is not None
        try:
            self._proc = self._popen(list(self.ARGV), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True)
        except OSError as e:
            _log.warning("nmcli monitor startet nicht (%s): Netz wird regelmäßig abgefragt", e)
            return False
        self._thread = threading.Thread(target=self._loop, args=(self._proc,), name="couch-net", daemon=True)
        self._thread.start()
        return True

    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def stop(self) -> None:
        self._stopped = True
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()                # nur das eigene Kind, über sein Popen-Objekt
            except OSError:
                pass

    def _loop(self, proc: Any) -> None:
        try:
            for line in proc.stdout:
                if line.strip() and not self._stopped:
                    self._on_event()
        except Exception:
            _log.exception("nmcli monitor")


# ================================================================ Zustand dieses Teils
class Part:
    """Was dieser Teil über die Laufzeit hält; hängt als wiring.settings_part am Wiring.

    Für Tests lassen sich am Wiring-Objekt vorab einstecken: _scheduler (Grund-Planer),
    lane_factory (statt Lane), popen (statt wiring._popen_bound), extra_overrides (dict, das
    deps.extra der Einstellungsbereiche ergänzt)."""

    def __init__(self, wiring: Any):
        self.wiring = wiring
        base = getattr(wiring, "_scheduler", None)
        if isinstance(base, OffMainScheduler):
            self.scheduler = base
        else:
            if base is None:
                from .settings import tree as settings_tree
                base = settings_tree.GLibScheduler()
            self.scheduler = OffMainScheduler(base)
        wiring._scheduler = self.scheduler       # ein Planer für alles in diesem Teil
        make_lane = getattr(wiring, "lane_factory", None) or Lane
        self.audio_lane = make_lane("couch-audio")
        self.perf_lane = make_lane("couch-perf")
        self.popen: Callable = getattr(wiring, "popen", None) or wiring_mod._popen_bound
        self.volume = {"level": 0, "muted": False, "sink": ""}
        self.volume_pending = False
        self.res_apps: set[int] = set()         # Apps, für die eine eigene Spielauflösung gilt
        self.net: NetWatch | None = None
        self.net_timer: Any = None
        self.size_timer: Any = None
        self.tick_timer: Any = None
        self.update_timer: Any = None
        self.update_armed = False               # tägliche Systemsuche läuft (update_arm nach dem Bereit-Signal)
        self.resume_timer: Any = None
        self.lock = threading.Lock()
        self.closed = False
        self.proton_cache: tuple | None = None
        self.proton_builds: list[dict] = []     # Auswahl je Spiel (lib.get.choices.proton)
        self.proton_builds_at = -1e9
        self.proton_builds_pending = False

    def close(self) -> None:
        """Am Ende des Home: Zeitgeber, Beobachter und die eigenen Threads loslassen."""
        self.closed = True
        for name in ("net_timer", "size_timer", "tick_timer", "update_timer", "resume_timer"):
            handle = getattr(self, name)
            setattr(self, name, None)
            try:
                self.scheduler.cancel(handle)
            except Exception:
                _log.exception("Zeitgeber %s", name)
        # Eine laufende HDR-/VRR-Probezeit kann niemand mehr bestätigen: gleich zurücknehmen,
        # bevor die Zeitgeber verstummen (sonst bliebe es bis zum nächsten Start eingeschaltet)
        tree = getattr(self.wiring, "tree", None)
        display = tree.area("display") if tree is not None else None
        if display is not None and hasattr(display, "cancel_trials"):
            try:
                display.cancel_trials()
            except Exception:
                _log.exception("Probezeiten beenden")
        # Ebenso „Bluetooth aus“: Sonst bliebe ein Bluetooth-Pad tot (Marke bleibt bei Misserfolg)
        accessories = tree.area("accessories") if tree is not None else None
        if accessories is not None and hasattr(accessories, "end_trial"):
            try:
                accessories.end_trial()
            except Exception:
                _log.exception("Probezeit Bluetooth beenden")
        self.scheduler.closed = True
        if self.net is not None:
            self.net.stop()
        self.audio_lane.close()
        self.perf_lane.close()


def part(wiring: Any) -> Part:
    p = getattr(wiring, "settings_part", None)
    if p is None:
        p = Part(wiring)
        wiring.settings_part = p
    return p


def _late(module: str, name: str) -> Callable | None:
    """Funktion eines anderen Teils (wiring_<module>.<name>), erst zur Aufrufzeit geholt.
    None, wenn es den Teil oder die Funktion (noch) nicht gibt."""
    full = "%s.wiring_%s" % (__package__, module)
    try:
        mod = importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name == full:
            return None
        _log.exception("Teil %s lässt sich nicht laden", module)
        return None
    except Exception:
        _log.exception("Teil %s lässt sich nicht laden", module)
        return None
    fn = getattr(mod, name, None)
    return fn if callable(fn) else None


def _settings_error(text: str) -> Exception:
    from .settings.rows import SettingsError
    return SettingsError(text)


# ================================================================ Einstellungen für die Seite
def ui_percent(raw: Any) -> int:
    """audio.ui_volume ist als Anteil 0..1 gespeichert; ältere Prozentwerte verkraften."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 60
    return int(round(max(0.0, min(100.0, raw * 100 if raw <= 1 else raw))))


def ui_state(config: Any) -> dict:
    """sys.state.ui: was die Seite selbst auswertet (Töne, Bewegung, Textgröße, Uhr, Hintergrund,
    Anzeigebereich, Design)."""
    from .settings.accessibility import nearest_scale
    from .settings.display import area_percent
    from .settings.audio import ui_pack
    from . import themes
    return {"sounds": bool(config.get("audio.ui_sounds", True)),
            "soundVolume": ui_percent(config.get("audio.ui_volume", 0.6)),
            "soundPack": ui_pack(config),
            "reduceMotion": bool(config.get("ui.reduce_motion", False)),
            "scale": nearest_scale(config.get("ui.scale", 1.0)),
            "clock24h": bool(config.get("ui.clock_24h", True)),
            "liveBackground": bool(config.get("ui.live_background", True)),
            "intro": bool(config.get("ui.intro", True)),
            "theme": str(config.get("ui.theme", "") or ""),
            "themeCss": themes.css_for(config.get("ui.theme", "")),
            "areaScale": area_percent(config.get("display.area_scale", 100))}


def push_ui(wiring: Any) -> None:
    app = wiring.app
    wiring.main.post(lambda: app.state.update(ui=ui_state(app.config)))


def ui_settings(wiring: Any) -> None:
    """Jede dieser Einstellungen hat genau einen Schlüssel (E22); bei jeder Änderung geht
    sys.state.ui neu hinaus."""
    cfg = wiring.app.config
    for prefix in ("audio.ui_", "ui.", "display.area_scale"):
        cfg.on_change(prefix, lambda key, value: push_ui(wiring))


# ================================================================ Leistung
def setup_perf(wiring: Any) -> None:
    from .perf import PerfService
    app, p = wiring.app, part(wiring)
    test = paths.test_mode()
    # Die Datei der Leistungsanzeige gehört der Sitzung. Im Prüfmodus nie die echte anfassen:
    # dort gilt nur eine Datei im eigenen Laufzeitordner (falls ein Test sie anlegt).
    mango = os.path.join(paths.run_dir(), "mangoapp.conf") if test else None
    perf = PerfService(wiring.config, wiring.gs, mango_config_path=mango, run_write=write3,
                       apply_display=lambda: wiring.main.call(app.focus.apply_session_defaults),
                       environ={} if test else None)
    wiring.perf = perf

    def perf_set(params: dict) -> dict:
        wanted = {k: params[k] for k in ("overlay", "fpsLimit", "resolution", "fontScale")
                  if params.get(k) is not None}
        try:
            return perf.set(**wanted)
        except ValueError as e:
            raise RpcError(str(e), "BadValue") from None

    # beide über den Leistungs-Thread: gamescopectl ist ein Unterprozess, und die Reihenfolge
    # gegenüber den Fokuswechseln muss stimmen
    app.rpc.register("perf.get", lambda params: p.perf_lane.submit(perf.get))
    app.rpc.register("perf.set", lambda params: p.perf_lane.submit(perf_set, params))

    def check_fps(value: int) -> int:
        # je Spiel gilt dieselbe Liste wie im Control Center: nur Teiler der Bildwiederholrate (E23)
        if value and value not in perf.fps_choices():
            raise ValueError("Diese Bildratenbegrenzung passt nicht zu %d Hz." % perf.refresh_rate())
        return value
    validators = getattr(app.library, "option_validators", None)
    if isinstance(validators, dict):
        validators["fps_limit"] = check_fps
    app.focus.on_change(getattr(wiring, "_front_changed", None) or (lambda: front_changed(wiring)))
    app.procs.on_change(getattr(wiring, "_proc_changed", None)
                        or (lambda running, what: proc_changed(wiring, running, what)))
    wiring.after_ready.append(lambda: perf_start(wiring))


def _perf_job(wiring: Any, fn: Callable, *args: Any) -> Future:
    def run() -> None:
        try:
            fn(*args)
        except Exception:
            _log.exception("Leistungswerte anwenden")
    return part(wiring).perf_lane.submit(run)


def perf_start(wiring: Any) -> None:
    """Nach dem Bereit-Signal: gespeicherte Stufe der Anzeige schreiben, Limit aus (das Home ist
    vorn); danach gilt, was gerade vorn ist (eine übernommene App nach einem Home-Neustart)."""
    perf = wiring.perf
    if perf is None:
        return
    app = wiring.app
    _perf_job(wiring, perf.apply_on_start)
    wiring._front = None
    front_changed(wiring)
    for running in list(app.procs.running() or []):
        _app_resolution(wiring, running)


def front_changed(wiring: Any) -> None:
    """Bildraten-Limit und Leistungsanzeige je Spiel gelten nur, solange es vorn ist; ist das
    Home vorn, gelten die globale Stufe und kein Limit. Läuft in der Hauptschleife und gibt
    die Arbeit an den Leistungs-Thread."""
    perf = wiring.perf
    if perf is None:
        return
    app = wiring.app
    front = app.focus.foreground
    if front == getattr(wiring, "_front", None):
        return
    wiring._front = front
    running = app.procs.by_app_id(front) if front is not None else None
    if running is None:
        _perf_job(wiring, perf.restore_global)
    else:
        _perf_job(wiring, perf.apply_for_app, app.library.get(running.entry_id))


def _app_resolution(wiring: Any, running: Any) -> None:
    """Spielauflösung je Spiel (Option "resolution"): Der eine Weg ist focus.set_app_options.
    Der FocusManager wendet sie an, sobald die App nach vorn kommt; alles andere folgt der
    Einstellung display.game_resolution."""
    app = wiring.app
    entry = app.library.get(running.entry_id)
    resolution = entry.option("resolution") if entry is not None else None
    set_options = getattr(app.focus, "set_app_options", None)
    if resolution and resolution != "native" and set_options is not None:
        set_options(running.app_id, resolution=str(resolution))
        part(wiring).res_apps.add(running.app_id)     # erst danach: nur was gesetzt ist, wird zurückgestellt


def proc_changed(wiring: Any, running: Any, what: str) -> None:
    if what == "started":
        _app_resolution(wiring, running)
        return
    if what != "exited":
        return
    p = part(wiring)
    if running.app_id in p.res_apps:
        p.res_apps.discard(running.app_id)
        # zurückstellen: Ohne eigenen Wert gilt wieder display.game_resolution. Sonst bekäme
        # die nächste App (Browser) die Größe des beendeten Spiels genannt.
        wiring.app.focus.set_app_options(running.app_id, resolution=None)
    if wiring.perf is not None and getattr(wiring, "_front", None) == running.app_id:
        # nicht auf den Fokuswechsel warten: Das Limit darf nie am Home hängen bleiben
        wiring._front = None
        _perf_job(wiring, wiring.perf.restore_global)


# ================================================================ Ton
def setup_audio(wiring: Any) -> None:
    from .settings.audio import AudioService
    from .settings.rows import SettingsError
    app, p = wiring.app, part(wiring)
    audio = AudioService(read3, write3, config=wiring.config, scheduler=p.scheduler,
                         on_change=getattr(wiring, "_audio_changed", None) or (lambda: audio_changed(wiring)),
                         # merkt sich, was vor dem Umstellen galt (<state>/sink-before)
                         on_default_sink=sinknote.record)
    wiring.audio = audio
    power = getattr(app, "power", None)
    if power is not None:
        # Ausschalten stellt die Ausgabe vorher zurück; scheitert es, wieder zum Fernseher
        power.on_stay = audio.request_tv

    def on_lane(fn: Callable[[dict], Any]) -> Callable[[dict], Future]:
        def run(params: dict) -> Any:
            try:
                return fn(params)
            except SettingsError as e:
                raise RpcError(e.message, "AudioFailed") from None
        return lambda params: p.audio_lane.submit(run, params)

    def audio_get(params: dict) -> dict:
        got = audio.get()
        push_volume(wiring, got)
        return got

    # Lautstärke und Stumm lesen nichts nach (kein pw-dump je Tastendruck): Der Zustand der
    # Statusleiste wird aus dem Ergebnis nachgezogen, der Beobachter liest gebündelt nach.
    def set_volume(params: dict) -> dict:
        result = audio.set_volume(params.get("level"))
        push_volume(wiring, {**p.volume, "level": result["level"]})
        return result

    def set_muted(params: dict) -> dict:
        result = audio.set_muted(bool(params.get("muted")))
        push_volume(wiring, {**p.volume, "muted": result["muted"]})
        return result

    def set_sink(params: dict) -> dict:
        name = audio.set_sink(str(params.get("id", "")))
        try:
            push_volume(wiring, audio.get())
        except Exception:
            _log.exception("Ton nach dem Wechsel der Ausgabe lesen")
        return {"sink": name}

    app.rpc.register("audio.get", on_lane(audio_get))
    app.rpc.register("audio.setVolume", on_lane(set_volume))
    app.rpc.register("audio.setMuted", on_lane(set_muted))
    app.rpc.register("audio.setSink", on_lane(set_sink))
    wiring.after_ready.append(lambda: audio_start(wiring))


def audio_start(wiring: Any) -> None:
    """Nach dem Bereit-Signal: Beobachter (pactl subscribe, nur lesend) starten und den Fernseher
    zur Ausgabe machen (entprellt, im Hintergrund; hat der Nutzer ein anderes Gerät gewählt, bleibt
    es dabei). Den alten Standard stellt der Supervisor am Sitzungsende wieder her, vor
    Ausschalten, Neustart und Desktop-Wechsel das Home selbst (power.py, sinknote.py)."""
    audio = wiring.audio
    if audio is None:
        return
    p = part(wiring)

    def start() -> None:
        try:
            audio.start_watch(popen=p.popen)
            if wiring.main.closed.is_set():
                audio.stop_watch()              # das Home endete, während der Beobachter startete
                return
            # Controller per Kabel/Dongle (E75): Ton-Ausgang nach Einstellung, Mikrofon zur Eingabe
            for step in (audio.apply_controller_output, audio.prefer_controller_mic):
                try:
                    step()
                except Exception as e:  # noqa: BLE001
                    _log.warning("Ton, Controller: %s", e)
        except Exception:
            _log.exception("Ton-Beobachter")
    wiring._bg(start)
    audio.request_tv()
    request_volume(wiring)


def push_volume(wiring: Any, got: dict) -> None:
    app, p = wiring.app, part(wiring)
    volume = {"level": got.get("level", 0), "muted": bool(got.get("muted")), "sink": got.get("sink") or ""}
    p.volume = volume
    wiring.main.post(lambda: app.state.update(volume=volume))


def request_volume(wiring: Any) -> None:
    """Ton-Zustand im Ton-Thread nachlesen und nach sys.state schreiben; mehrere Wünsche in
    kurzer Folge werden ein Lesen."""
    audio = wiring.audio
    if audio is None:
        return
    p = part(wiring)
    with p.lock:
        if p.volume_pending:
            return
        p.volume_pending = True

    def read() -> None:
        with p.lock:
            p.volume_pending = False
        try:
            push_volume(wiring, audio.get())
        except Exception:
            _log.exception("Ton nachlesen")
    p.audio_lane.submit(read)


def audio_changed(wiring: Any) -> None:
    """Der Beobachter meldet eine Änderung von außen (gebündelt, aus einem Arbeits-Thread)."""
    request_volume(wiring)
    _emit_changed(wiring, "audio")


# ================================================================ Einstellungsbaum
def _emit_changed(wiring: Any, page_id: str) -> None:
    app = wiring.app
    wiring.main.post(lambda: app.rpc.emit("settings.changed", {"page": page_id}))


def settings_changed(wiring: Any, page_id: str) -> None:
    """Ein Bereich meldet eine Änderung (kann aus jedem Thread kommen): der Seite Bescheid
    geben und nachziehen, was davon in sys.state steht."""
    _emit_changed(wiring, page_id)
    _follow(wiring, str(page_id).split(".", 1)[0].split(":", 1)[0], inline=False)


def _follow(wiring: Any, area: str, inline: bool) -> None:
    """Nach einer Änderung im Bereich area den zugehörigen Teil von sys.state erneuern.
    inline: Der Aufrufer ist schon ein Arbeits-Thread und darf selbst lesen."""
    if area == "audio":
        request_volume(wiring)
        return
    fn = {"network": lambda: push_network(wiring, fresh=True), "display": lambda: push_hdr(wiring),
          "system": lambda: push_update_badge(wiring)}.get(area)
    if fn is None:
        return
    if inline:
        try:
            fn()
        except Exception:
            _log.exception("Zustand nach einer Änderung in %s", area)
    else:
        wiring._bg(fn)


def pads_for_settings(wiring: Any) -> list[dict]:
    """Pads für die Seite „Zubehör“; "type" nennt dort die Verbindung (bluetooth | usb)."""
    pads = [dict(p) for p in (wiring.main.call(wiring.app.input.pads) or []) if isinstance(p, dict)]
    tree = wiring.tree
    accessories = tree.area("accessories") if tree is not None else None
    if accessories is None or not pads:
        return pads
    try:
        connected = {str(d.get("mac", "")).upper() for d in accessories.bt_state()["devices"] if d.get("connected")}
    except Exception:
        _log.exception("Bluetooth-Geräte für die Pad-Liste")
        return pads
    for pad in pads:
        ident = str(pad.get("id") or "")
        if _MAC.match(ident):
            pad["type"] = "bluetooth" if ident.upper() in connected else "usb"
    return pads


def busy_sentences(wiring: Any) -> list[str]:
    """Sätze für die Rückfragen vor Ausschalten, Desktop-Wechsel und Steam-Übergabe (E31): was
    noch offen ist und was die anderen Teile melden (Installationen, Kopien)."""
    app = wiring.app

    def collect() -> list[str]:
        out: list[str] = []
        titles = []
        for running in app.procs.running() or []:
            title = str(getattr(getattr(running, "spec", None), "title", "") or "")
            if not title:
                entry = app.library.get(running.entry_id)
                title = entry.title if entry is not None else str(running.entry_id)
            titles.append("„%s“" % title)
        if titles:
            out.append("Noch offen: %s." % ", ".join(titles))
        busy = getattr(wiring, "busy", None)
        if callable(busy):
            out += [str(t) for t in busy()]
        return out
    return wiring.main.call(collect)


def backup_settings(app: Any) -> str | None:
    """Vor „Einstellungen zurücksetzen“: settings.json, wie sie jetzt ist, daneben sichern
    (settings.json.bak-<zeit>). Arbeits-Thread. Ohne Datei gibt es nichts zu sichern."""
    from . import system
    path = getattr(app.config, "path", None) or paths.settings_file()
    writer = getattr(app, "writer", None)
    if writer is not None and hasattr(writer, "flush"):
        writer.flush(2.0)                       # was noch im Schreib-Thread steht, gehört dazu
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        _log.warning("Einstellungen vor dem Zurücksetzen nicht lesbar: %s", e)
        return None
    backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        system.write_atomic(backup, data)
    except OSError as e:
        _log.warning("Sicherung der Einstellungen nicht schreibbar: %s", e)
        return None
    _log.info("Einstellungen vor dem Zurücksetzen gesichert: %s", backup)
    return backup


def reset_settings(app: Any) -> None:
    """„Einstellungen zurücksetzen“: alles auf die Vorgaben (ein Schreibvorgang, Beobachter
    erfahren jede Änderung) und gleich anwenden, was sonst erst der nächste Start täte.
    Blockierend, aus einem Arbeits-Thread rufen."""
    wiring = app.wiring
    backup_settings(app)
    reset_all = getattr(app.config, "reset_all", None)
    if reset_all is not None:
        wiring.main.call(reset_all)
    else:
        for key in list(app.config.all()):
            wiring.config.reset(key)
    tree = wiring.tree
    display = tree.area("display") if tree is not None else None
    if display is not None and hasattr(display, "apply_on_start"):
        display.apply_on_start()                # schreibt auch „HDR aus“ ausdrücklich
        push_hdr(wiring)
    if wiring.perf is not None:
        _perf_job(wiring, wiring.perf.refresh)
    if wiring.audio is not None:
        wiring.audio.request_tv()               # audio.sink ist wieder leer: der Fernseher
    push_ui(wiring)


def _connector() -> str:
    """Anschluss des Fernsehers (E104, settings/drm_modes.tv_connector): zuerst OUTPUT_CONNECTOR der Sitzung
    (z. B. "HDMI-A-1,*" ergibt HDMI-A-1), sonst der erste verbundene Ausgang aus /sys/class/drm (HDMI vor DP
    vor eDP), sonst "" („Kein Fernseher erkannt“). Liest nur ein paar kleine Dateien."""
    from .settings import drm_modes
    try:
        env = paths.session_env()
    except Exception:
        env = {}
    try:
        return drm_modes.tv_connector(env)
    except Exception:
        _log.exception("Anschluss des Fernsehers")
        return ""


# ================================================================ itch.io-Konto (E100)
# Einstellungen › Spiele › itch.io. wiring_itch (Teil B1) hängt itch_status/itch_logout/itch_set_client
# an das Wiring; fehlt der Teil (oder lädt er nicht), gilt das Konto aus store/itch_account direkt.
ITCH_CLIENT_KEY = "store.itch_client_id"


def _itch_account(wiring: Any) -> Any:
    """Rückfall ohne wiring_itch: ein Konto je Wiring, App-Kennung in der Einstellung (E22)."""
    acc = getattr(wiring, "settings_itch_account", None)
    if acc is None:
        from .store import itch_account
        config = wiring.config
        acc = itch_account.ItchAccount(paths.config_dir(), paths.state_dir(),
                                       get_client=lambda: config.get(ITCH_CLIENT_KEY, ""),
                                       set_client=lambda cid: config.set(ITCH_CLIENT_KEY, cid))
        wiring.settings_itch_account = acc
    return acc


def itch_status(wiring: Any) -> dict | None:
    """{configured, loggedIn, name}; None, wenn nicht lesbar (dann nie „abgemeldet“ zeigen, E99)."""
    try:
        fn = getattr(wiring, "itch_status", None)
        if callable(fn):
            got = fn()
        else:
            acc = _itch_account(wiring)
            acc.take_client_id()            # vom Browser gelesene Kennung übernehmen (wie vor jedem Abruf)
            got = {"configured": acc.configured(), "loggedIn": acc.logged_in(), "name": acc.name()}
    except Exception:
        _log.exception("itch.io-Konto lesen")
        return None
    if not isinstance(got, dict):
        return None
    return {"configured": bool(got.get("configured")),
            "loggedIn": bool(got.get("loggedIn", got.get("logged_in", False))),
            "name": str(got.get("name") or "")}


def _itch_result(got: Any, fallback: str) -> dict:
    if not isinstance(got, dict):
        return {"ok": bool(got), "message": "" if got else fallback}
    ok = got.get("ok") is not False
    return {"ok": ok, "message": str(got.get("message") or ("" if ok else fallback))}


def itch_logout(wiring: Any) -> dict:
    """{ok, message}. Blockiert (widerruft den Schlüssel bei itch.io); nur im Thread rufen."""
    failed = "Das Abmelden bei itch.io hat nicht geklappt."
    try:
        fn = getattr(wiring, "itch_logout", None)
        if callable(fn):
            return _itch_result(fn(), failed)
        _itch_account(wiring).logout()
        return {"ok": True, "message": "Du bist bei itch.io abgemeldet."}
    except Exception as e:
        from .store.cache import StoreError
        _log.warning("itch.io abmelden: %s", type(e).__name__)      # nur die Art: nie Schlüssel ins Protokoll
        return {"ok": False, "message": (str(e) if isinstance(e, StoreError) else "") or failed}


def itch_set_client(wiring: Any, cid: str) -> dict:
    """{ok, message}: App-Kennung übernehmen (geprüft: 32 Hex-Zeichen)."""
    bad = "Die App-Kennung hat 32 Zeichen aus 0–9 und a–f."
    try:
        fn = getattr(wiring, "itch_set_client", None)
        if callable(fn):
            return _itch_result(fn(cid), bad)
        ok = _itch_account(wiring).set_client_id(cid)
        return {"ok": bool(ok), "message": "App-Kennung übernommen." if ok else bad}
    except Exception:
        _log.exception("itch.io-App-Kennung übernehmen")
        return {"ok": False, "message": "Die App-Kennung ließ sich nicht übernehmen."}


def build_extra(wiring: Any) -> dict:
    """deps.extra für die Bereiche. Alles, was andere Teile liefern, ist spät gebunden."""
    app, main = wiring.app, wiring.main
    src = wiring.sources

    def power(action: str) -> None:
        # Die Zeilen der Einstellungen fragen selbst (confirm-action), auch nach laufenden Spielen:
        # Der Kern fragt dann nicht noch einmal (confirmed=True). Nur wenn ein busy_check etwas
        # meldet (Installation, Prefix-Kopie, System-Update), stellt er seine Rückfrage mit genau
        # diesen Sätzen, denn nicht jede Zeile nennt sie (E31).
        def run() -> Any:
            busy = getattr(wiring, "busy", None)
            return app.power.do(action, not (callable(busy) and busy()))
        try:
            work = main.call(run)
        except RpcError as e:                   # „Es läuft schon ein Vorgang.“
            raise _settings_error(e.message) from None
        if isinstance(work, Future):
            work.add_done_callback(app.power.tell_failed)

    def rescan() -> None:
        fn = _late("library", "rescan")
        if fn is not None:
            fn(app)
        else:
            main.call(app.library.rescan)

    def launch(entry_id: str) -> None:
        fn = _late("launch", "launch_entry")
        if fn is not None:
            fn(app, entry_id)                   # wirft LaunchError mit deutschem Satz
        else:
            main.call(app.shell.launch, entry_id)

    def has_entry(entry_id: str) -> bool:
        return main.call(app.library.get, entry_id) is not None

    def games() -> list[dict]:
        fn = _late("library", "game_dirs")
        return list(fn(app) or []) if fn is not None else []

    def local_entries() -> list[dict]:
        fn = _late("library", "local_entries")
        out = []
        for e in (fn(app) or []) if fn is not None else []:
            if isinstance(e, dict) and e.get("id"):
                # die Speicher-Seite misst den Ordner unter "path"
                out.append({**e, "path": e.get("path") or e.get("install_dir") or ""})
        return out

    def delete_local(entry_id: str) -> dict:
        fn = _late("install", "delete_local")
        if fn is not None:
            result = fn(app, entry_id)
        elif wiring.installer is not None:
            result = wiring.installer.delete(entry_id)
        else:
            raise _settings_error("Löschen ist gerade nicht verfügbar.")
        if isinstance(result, dict) and result.get("ok") is False:
            raise _settings_error(str(result.get("message") or "Das Spiel ließ sich nicht löschen."))
        return result if isinstance(result, dict) else {}

    def steam_account() -> dict:
        # nur Anzeige: Ein Fehler beim Lesen der Steam-Dateien darf die Seite nicht kosten
        try:
            fn = _late("library", "steam_account")
            if fn is not None:
                return fn(app) or {}
            if src is None:
                return {}
            from .library import steam
            return steam.steam_account(src) or {}
        except Exception:
            _log.exception("Steam-Konto lesen")
            return {}

    def emudeck_info() -> dict:
        try:
            fn = _late("library", "emudeck_info")
            if fn is not None:
                return fn(app) or {}
            if src is None:
                return {}
            from .library import emulators
            return emulators.emudeck_info(src) or {}
        except Exception:
            _log.exception("EmuDeck-Angaben lesen")
            return {}

    def running_kinds() -> list[str]:
        def collect() -> list[str]:
            return [str(getattr(getattr(a, "spec", None), "kind", "") or "game") for a in app.procs.running()]
        return main.call(collect)

    def fetch_all_art() -> dict:
        fn = _late("library", "fetch_all_art")
        if fn is None:
            raise _settings_error("Das Laden der Bilder ist gerade nicht verfügbar.")
        return fn(app)

    def art_sources() -> list:
        # Ruht SteamGridDB nach Störungen? Die Schlüssel-Zeile unter Home-Bildschirm sagt es (E99)
        fn = _late("art", "sources_state")
        return (fn(app) or []) if fn is not None else []

    extra: dict = {
        "perf": wiring.perf,
        "audio": wiring.audio,
        "apply_display": lambda: main.call(app.focus.apply_session_defaults),
        "pads": lambda: pads_for_settings(wiring),
        "power": power,
        # Spiel = game/emulator; Medien-Apps, Browser und Desktop sind keine (Befund 2026-10-14:
        # die Rückfrage sprach sonst vom „laufenden Spiel“, wenn nur YouTube lief)
        "game_running": lambda: any(k in ("game", "emulator") for k in running_kinds()),
        "running_kinds": running_kinds,
        "busy": lambda: busy_sentences(wiring),
        "rescan": rescan,
        "launch": launch,
        "has_entry": has_entry,
        "games": games,
        "local_entries": local_entries,
        "delete_local": delete_local,
        "steam_account": steam_account,
        "emudeck_info": emudeck_info,
        "fetch_all_art": fetch_all_art,
        "art_sources": art_sources,
        "reset_settings": lambda: reset_settings(app),
        "release_dir": paths.release_dir(),
        "connector": _connector(),
        "games_disk": (src.games_disk if src is not None and src.games_disk else None) or paths.games_disk(),
        # Updates (updates.py): Suche, Proton, Epic-Spiele; spät gebunden, die Suche startet erst nach dem Start
        "updates": lambda: getattr(wiring, "update_watch", None),
        "bt_hci": lambda: bt_hci(wiring),
        "epic_update": lambda app_name: epic_update(wiring, app_name),
        "epic_jobs": lambda: (wiring.epic_downloads.jobs() if getattr(wiring, "epic_downloads", None) else []),
        # Proton-Versionen (E101/E102): die Warteschlange aus wiring_proton, spät gebunden (Teil „proton“)
        "proton": lambda: getattr(wiring, "proton_jobs", None),
        # itch.io-Konto (E100): spät gebunden, wiring_itch hängt sich erst nach diesem Teil ein
        "itch_status": lambda: itch_status(wiring),
        "itch_logout": lambda: itch_logout(wiring),
        "itch_set_client": lambda cid: itch_set_client(wiring, cid),
        # E103: Dienste der Teile, die sich erst nach diesem einhängen (wiring.PARTS), spät gebunden:
        # deps.extra["late"]("<name>") -> getattr(wiring, name) oder None (nichts Privates)
        "late": lambda name: getattr(wiring, name, None) if isinstance(name, str) and not name.startswith("_") else None,
    }
    if src is not None:
        extra["steam_roots"] = [src.steam_root]
        extra["compat_tools_dirs"] = _compat_dirs(app, wiring)
    if paths.test_mode():
        # Der Fernseher am echten Anschluss geht einen Prüflauf nichts an (Anschluss aus _connector, E104)
        from .settings import drm_modes
        extra["drm_summary"] = lambda: drm_modes.summarize(extra.get("connector") or "", None, None)
    # Für Tests und Prüfläufe: Attrappen einstecken (bluez, os_update_helper, ostree_lister …)
    extra.update(getattr(wiring, "extra_overrides", None) or {})
    return {k: v for k, v in extra.items() if v is not None}


def setup_settings(wiring: Any) -> None:
    from .settings import tree as settings_tree
    app, p = wiring.app, part(wiring)
    deps = SimpleNamespace(
        config=wiring.config, run_read=read3, run_write=write3, gs=wiring.gs,
        paths=SimpleNamespace(config_dir=paths.config_dir(), data_dir=paths.data_dir(),
                              state_dir=paths.state_dir(), run_dir=paths.run_dir(),
                              cache_dir=paths.cache_dir()),
        scheduler=p.scheduler,
        notify=lambda title, body="", icon="": app.notify.push(title, body, icon or "info"),
        extra=build_extra(wiring))
    tree = settings_tree.SettingsTree(deps)
    wiring.tree = tree
    app.settings = tree
    for name, why in tree.skipped.items():
        _log.warning("Einstellungsbereich %s fehlt: %s", name, why)
    tree.on_changed(lambda page_id: settings_changed(wiring, page_id))

    def forward(kind: str, params: dict) -> dict:
        """settings.set / settings.action / settings.reset: Das Ergebnis des Bereichs geht
        unverändert an die Seite ({rows?, rows_page?, message?, error?, page?, trial?, ui?}); nie
        eine Ausnahme."""
        row_id = str(params.get("id", ""))
        try:
            if kind == "reset":
                out = tree.reset(row_id)            # settings.reset {id}: auf die Vorgabe der Zeile
            else:
                call = tree.set if kind == "set" else tree.action
                out = call(row_id, params.get("value"))
        except Exception:
            _log.exception("settings.%s %s", kind, row_id)
            out = {"error": True, "message": GENERIC_ERROR}
        out = out if isinstance(out, dict) else {}
        try:
            _follow(wiring, row_id.split(".", 1)[0].split(":", 1)[0], inline=True)
        except Exception:
            _log.exception("Zustand nach settings.%s %s", kind, row_id)
        return out

    def page(params: dict) -> dict:
        return tree.page(str(params.get("id", "")))        # liefert bei Fehlern eine Hinweis-Seite

    def action(params: dict) -> Future:
        """settings.action {id, value?, confirmed?}. Zeilen vom Typ confirm-action (Unumkehrbares:
        Gerät entfernen, WLAN vergessen, zurücksetzen, ausschalten …) führt der Kern nur aus, wenn
        die Seite mit dem Satz aus Row.confirm gefragt hat (confirmed: true). Sonst fragt er selbst
        im Overlay (Vorgabe Abbrechen); abgelehnt: {cancelled: true}, nichts ist passiert. So
        löst auch eine Seite ohne eigene Rückfrage nie etwas Unumkehrbares aus."""
        if params.get("confirmed") is True:
            return wiring._bg(forward, "action", params)
        row_id, value = str(params.get("id", "")), params.get("value")
        out: Future = Future()

        def prepare() -> tuple:
            try:
                question = tree.confirm_for(row_id, value)
            except Exception:
                _log.exception("Rückfrage zu %s", row_id)
                question = dict(getattr(settings_tree, "GENERIC_CONFIRM", {}))
            if question is None:
                return ("done", forward("action", params))
            return ("ask", question)

        def settle(fut: Future) -> None:
            try:
                out.set_result(fut.result())
            except BaseException:               # forward wirft nie; zur Sicherheit
                _log.exception("settings.action %s", row_id)
                out.set_result({"error": True, "message": GENERIC_ERROR})

        def answered(asked: Future) -> None:
            try:
                yes = asked.result() == "yes"
            except BaseException:
                yes = False
            if not yes:
                _log.info("settings.action %s: Rückfrage abgelehnt", row_id)
                out.set_result({"cancelled": True})
                return
            wiring._bg(forward, "action", params).add_done_callback(settle)

        def step(first: Future) -> None:        # Hauptschleife (Rückfrage gehört der Shell)
            try:
                kind, value_or_question = first.result()
            except BaseException:
                _log.exception("settings.action %s", row_id)
                out.set_result({"error": True, "message": GENERIC_ERROR})
                return
            if kind == "done":
                out.set_result(value_or_question)
                return
            q = value_or_question
            confirm = getattr(app.shell, "confirm", None)
            if not callable(confirm):
                # niemand kann fragen: lieber nichts tun und den Satz zurückgeben
                out.set_result({"error": True, "message": "Das muss erst bestätigt werden.", "confirm": q})
                return
            _log.info("settings.action %s: Rückfrage im Overlay (%s)", row_id, q.get("title"))
            try:
                asked = confirm(q.get("title") or "", q.get("text") or "",
                                [{"value": "yes", "label": q.get("ok") or "OK"},
                                 {"value": "no", "label": q.get("cancel") or "Abbrechen"}], default="no")
            except Exception:
                _log.exception("Rückfrage zu %s", row_id)
                out.set_result({"error": True, "message": GENERIC_ERROR})
                return
            asked.add_done_callback(lambda a: wiring.main.post(answered, a))

        wiring._bg(prepare).add_done_callback(lambda f: wiring.main.post(step, f))
        return out

    app.rpc.register("settings.categories", lambda params: wiring._bg(lambda: {"categories": tree.categories()}))
    app.rpc.register("settings.page", lambda params: wiring._bg(page, params))
    app.rpc.register("settings.set", lambda params: wiring._bg(forward, "set", params))
    app.rpc.register("settings.reset", lambda params: wiring._bg(forward, "reset", params))
    app.rpc.register("settings.action", action)


def started(wiring: Any) -> None:
    """Von main gerufen, nachdem die Shell ihre Rückrufe eingehängt hat."""
    app = wiring.app
    inp = app.input
    if hasattr(inp, "on_type_error"):
        # Scheitert das Tippen der Bildschirmtastatur (kein Fenster mit Tastaturfokus), soll
        # man das am Bildschirm sehen und nicht nur im Protokoll
        inp.on_type_error = lambda text: app.notify.push("Tippen hat nicht geklappt", str(text), icon="warn")
    if hasattr(inp, "on_idle_off"):
        inp.on_idle_off = lambda pad: idle_off(wiring, pad)
    if hasattr(inp, "on_cable_pad"):
        inp.on_cable_pad = lambda pad: cable_pad(wiring, pad)
    shell_cb = inp.on_pads_changed

    def pads_changed() -> None:
        if shell_cb:
            shell_cb()
        if wiring.tree is not None:
            wiring.tree.changed("accessories")  # Seite „Zubehör“ zeigt die Pads
    inp.on_pads_changed = pads_changed
    start_idle(wiring)
    start_capture(wiring)
    start_updates(wiring)


def _http_json(url: str) -> Any:
    import json
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "PC5-Launcher", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read(4 * 1024 * 1024).decode("utf-8"))


def bt_hci(wiring: Any) -> str:
    """hciN des Bluetooth-Adapters, den Zubehör gerade nutzt (der mit dem Controller)."""
    tree = wiring.tree
    accessories = tree.area("accessories") if tree is not None else None
    try:
        adapter = accessories.bt_state()["adapter"] if accessories is not None else None
    except Exception:  # noqa: BLE001
        adapter = None
    return os.path.basename(adapter["path"]) if adapter else "hci0"


def epic_update(wiring: Any, app_name: str) -> str:
    """Ein Epic-Spiel über die Warteschlange des Stores aktualisieren (Fortschritt unter Downloads)."""
    dl = getattr(wiring, "epic_downloads", None)
    if dl is None:
        raise _settings_error("Das ist gerade nicht verfügbar.")
    reg = dl.installed().get(app_name) or {}
    game = next((g for g in (getattr(getattr(wiring, "update_watch", None), "found", {}) or {}).get("games") or []
                 if g.get("app") == app_name), {})
    title = str(reg.get("title") or game.get("title") or "Das Spiel")
    entry_id = str(reg.get("entry") or "")
    if entry_id and wiring.main.call(wiring.app.procs.by_entry, entry_id) is not None:
        raise _settings_error("Schließe das Spiel zuerst, dann lässt es sich aktualisieren.")
    dl.add(app_name, title, str(reg.get("namespace") or ""), kind="update", entry_id=entry_id)
    return "„%s“ wird aktualisiert. Den Fortschritt siehst du unter Downloads." % title


UPDATE_PAGES = ("system.apps", "system.software", "system")


def show_updates(wiring: Any) -> None:
    """Hauptschleife: Seiten mit Update-Stand neu zeichnen lassen. Die offene Seite lädt nur neu,
    wenn ihre eigene Kennung kommt (screens/settings.js): „Updates“ ist system.apps, die Zahl steht
    auf system.software (2026-10-21: der Fortschritt von Proton stand still, bis man die Seite neu öffnete)."""
    if wiring.tree is not None:
        for page in UPDATE_PAGES:
            wiring.tree.changed(page)


def start_updates(wiring: Any) -> None:
    """Suche nach Updates für Apps, Proton und Epic-Spiele (updates.py); die Systemsoftware sucht
    ihr eigener täglicher Updater. Im Prüfmodus nichts (kein Netz, nicht die Ordner des Nutzers)."""
    if paths.test_mode():
        return
    setup_updates(wiring).start()


def setup_updates(wiring: Any) -> Any:
    """Die UpdateWatch samt Haken, ohne zu suchen (fragt erst ihr Zeitgeber; Tests rufen nur dies)."""
    from . import updates
    from .library.apps import APPS, CHROME
    app = wiring.app
    names = {app[3]: app[1] for app in APPS if app[2] == "flatpak"}    # (id, Titel, Art, Ziel, …): E105 hängt Felder an
    names[CHROME] = "Google Chrome"             # die Kachel „Browser“ ist seit E59 der eigene Browser

    def os_state() -> tuple[bool, str]:
        system_area, updater = _updater(wiring)
        if updater is None:
            return False, ""
        st = updater.status()
        return st.get("phase") == "available", str(st.get("checked_at") or "")

    def apps() -> list:
        from .settings.backends import flatpak
        found, error = flatpak.read_updates(read3)
        if error:
            raise RuntimeError(error)
        return [{**u, "name": names.get(u["id"], u["name"])} for u in found]

    def compat_dir() -> str:
        dirs = _compat_dirs(app, wiring)
        return dirs[0] if dirs else ""

    def proton() -> dict | None:
        d = compat_dir()
        return updates.proton_update(d, _http_json) if d else None

    def games() -> list:
        leg = getattr(wiring, "epic_legendary", None)
        if leg is None or not leg.ready():
            return []
        return leg.check_updates()

    def publish(found: dict) -> None:
        apps_n = [a for a in found.get("apps") or [] if not a.get("runtime")]
        value = {"os": bool(found.get("os")), "apps": len(apps_n),
                 "runtimes": len(found.get("apps") or []) - len(apps_n),
                 "proton": (found.get("proton") or {}).get("name") or "", "games": len(found.get("games") or [])}

        def apply() -> None:
            app.state.update(updates=value)
            wiring._bg(push_update_badge, wiring)
            show()
        wiring.main.post(apply)

    def show() -> None:
        show_updates(wiring)

    def changed() -> None:
        wiring.main.post(show)

    def install(rel: dict, progress: Callable[[int], None]) -> str:
        # Ein Weg (E101/E102): über die Proton-Warteschlange, wenn der Teil „proton“ da ist (einer zugleich),
        # sonst direkt; beides lädt über proton_repair.install
        jobs = getattr(wiring, "proton_jobs", None)
        if jobs is not None and callable(getattr(jobs, "install_wait", None)):
            return jobs.install_wait(rel["name"], progress, size=int(rel.get("size") or 0))
        return updates.install_proton(rel, compat_dir(), progress)

    def leftovers() -> None:
        # Reste eines Ladens, das mit dem letzten Home abbrach (E99); nie während eines Ladens
        d = compat_dir()
        if d and watch.busy_text() is None:
            updates.cleanup_proton(d)

    sched = app.shell.sched
    watch = updates.UpdateWatch(
        {"os": os_state, "apps": apps, "proton": proton, "games": games}, publish,
        lambda title, body: app.notify.push(title, body, icon="update"),
        lambda seconds, fn: sched.later(seconds * 1000, fn), wiring._bg, proton_installer=install, changed=changed)
    wiring.update_watch = watch
    # Ausschalten, Abmelden und Ruhemodus fragen nach, solange Proton lädt (sonst bräche es ab)
    wiring.busy_checks.append(watch.busy_text)
    wiring.after_ready.append(lambda: wiring._bg(leftovers))
    return watch


def after_resume(wiring: Any) -> None:
    """Hauptschleife, nach dem Aufwachen (ResumeWatch). Die Zeitgeber der Update-Suchen laufen auf
    der monotonen Uhr und stehen im Ruhemodus: fällige Suchen nach der Wanduhr nachholen (E99)."""
    watch = getattr(wiring, "update_watch", None)
    if watch is not None:
        try:
            watch.resumed()
        except Exception:
            _log.exception("Update-Suche nach dem Aufwachen")
    p = part(wiring)
    if p.closed or not p.update_armed:
        return
    # check_if_due vergleicht mit der Wanduhr: Ist kein Tag um, fragt es den Helfer gar nicht
    p.scheduler.cancel(p.resume_timer)
    p.resume_timer = p.scheduler.later(UPDATE_RESUME_S, lambda: None if p.closed else update_check(wiring))


def start_capture(wiring: Any) -> None:
    """Bildschirmfoto per PS + Create bzw. Create (capture.py); die Shell reicht die Geste durch."""
    from . import capture, system
    app = wiring.app
    shell = app.shell
    if not hasattr(shell, "front_title"):
        return
    cap = capture.Capture(app.config, shell.front_title, system.run_write,
                          lambda title, body, ok: app.notify.push(title, body, icon="screen" if ok else "warn",
                                                                  important=True),
                          wiring._bg)
    wiring.capture = cap
    shell.capture = cap.on_gesture


def start_idle(wiring: Any) -> None:
    """Bildschirmschoner und Ruhemodus nach Leerlauf (idle.py), in der Hauptschleife."""
    from . import idle, system
    app = wiring.app
    shell, inp = app.shell, app.input
    if not hasattr(shell, "open_saver") or not callable(getattr(inp, "idle_seconds", None)):
        return

    # Sperren bei logind liest ein Arbeits-Thread (busctl, bis 5 s): Die Prüfung läuft in der
    # Hauptschleife, in der letzten Minute alle 10 s und, solange ein Download den Schlaf
    # aufhält, stundenlang (E21, E99)
    inhibit: dict = {"at": None, "value": [], "pending": False}

    def read_inhibitors() -> None:
        try:
            inhibit["value"] = idle.logind_blockers(system.run_read)
        finally:
            inhibit["at"] = time.monotonic()
            inhibit["pending"] = False

    def inhibitors() -> list:
        at = inhibit["at"]
        age = None if at is None else time.monotonic() - at
        if (age is None or age >= LOGIND_FRESH_S) and not inhibit["pending"]:
            inhibit["pending"] = True
            wiring._bg(read_inhibitors)
            at = inhibit["at"]                  # ein Arbeits-Thread im Test erledigt es sofort
            age = None if at is None else time.monotonic() - at
        if age is None or age >= LOGIND_STALE_S:
            return ["Sperren des Ruhemodus werden gelesen."]    # einen Takt warten statt raten
        return list(inhibit["value"])

    def blockers() -> list:
        out = list(wiring.busy())               # Installation, Prefix-Kopie, Store, System-Update
        if any(d.get("state") in ("running", "queued") for d in (app.state.value("downloads") or []) if isinstance(d, dict)):
            out.append("Ein Download läuft.")
        ops = getattr(wiring, "file_ops", None)
        if ops is not None and ops.jobs:
            out.append("Ein Dateiauftrag läuft.")
        if getattr(app.power, "busy", None) or shell.ending:
            out.append("Ein/Aus läuft schon.")
        # logind nur fragen, wenn sonst nichts den Schlaf aufhält
        return out or inhibitors()

    def suspend() -> None:
        try:
            work = app.power.do("suspend", confirmed=True)
        except RpcError as e:
            _log.warning("Ruhemodus nach Leerlauf: %s", e.message)
            return
        if isinstance(work, Future):
            work.add_done_callback(app.power.tell_failed)

    saver = SimpleNamespace(open=shell.open_saver, close=shell.close_saver, is_open=shell.saver_open)
    wiring.idle = idle.IdleWatch(
        shell.sched, app.config, inp.idle_seconds, shell.front_kind, saver, blockers,
        lambda title, body: app.notify.push(title, body, icon="power", important=True), suspend)
    wiring.idle.start()
    # Aufgewacht: Leerlauf zählt neu, ohne Controller ein Hinweis (Shell.after_resume), fällige
    # Update-Suchen kommen nach (after_resume)
    def on_resume() -> None:
        for fn in (wiring.idle.resumed, shell.after_resume, lambda: after_resume(wiring)):
            try:
                fn()
            except Exception:
                _log.exception("Nach dem Aufwachen: %r", fn)
    wiring.resume_watch = idle.ResumeWatch(shell.sched, on_resume)
    wiring.resume_watch.start()


def bt_trial_restore(wiring: Any) -> None:
    """Arbeits-Thread nach dem Bereit-Signal: Hat ein früheres Home Bluetooth auf Probe
    ausgeschaltet und ist dann abgestürzt, wieder einschalten (settings/accessories.py)."""
    tree = wiring.tree
    accessories = tree.area("accessories") if tree is not None else None
    fn = getattr(accessories, "restore_after_restart", None)
    if fn is not None:
        try:
            fn()
        except Exception:
            _log.exception("Probezeit Bluetooth nachholen")


def idle_off(wiring: Any, pad: dict) -> None:
    """Der Eingabedienst meldet ein Bluetooth-Pad, das input.idle_off_minutes lang unbenutzt
    war (Hauptschleife). Getrennt wird im Arbeits-Thread über run_write; nie, solange eine App
    vorn ist."""
    app = wiring.app
    if app.focus.foreground is not None:
        return
    ident = str((pad or {}).get("id") or "")
    if not _MAC.match(ident):
        return

    def work() -> None:
        from .settings.backends import bluez
        tree = wiring.tree
        accessories = tree.area("accessories") if tree is not None else None
        if accessories is None:
            return
        try:
            device = next((d for d in accessories.bt_state()["devices"]
                           if str(d.get("mac", "")).upper() == ident.upper() and d.get("connected")), None)
            if device is None:
                return                          # am Kabel oder schon weg
            rc, _out, err = write3(bluez.cmd_disconnect(device["path"]), timeout=15)
        except Exception:
            _log.exception("Pad %s trennen", ident)
            return
        if rc != 0:
            _log.warning("Pad %s ließ sich nicht trennen: %s", ident, (err or "").strip())
        else:
            _log.info("Pad %s nach Leerlauf getrennt", ident)
    wiring._bg(work)


def cable_pad(wiring: Any, pad: dict) -> None:
    """Der Eingabedienst meldet einen DualSense am USB-Kabel (Hauptschleife). Die Kabel-Kopplung
    (settings/accessories.py, E78) liest und schreibt das Pad und fragt BlueZ: Arbeits-Thread."""
    def work() -> None:
        tree = wiring.tree
        accessories = tree.area("accessories") if tree is not None else None
        fn = getattr(accessories, "cable_pair", None)
        if fn is None:
            return
        try:
            fn(pad)
        except Exception:
            _log.exception("Kabel-Kopplung %s", (pad or {}).get("id"))
    wiring._bg(work)


# ================================================================ sys.state
def push_network(wiring: Any, fresh: bool = False) -> None:
    """Arbeits-Thread: Netz für Statusleiste und Control Center."""
    tree = wiring.tree
    network = tree.area("network") if tree is not None else None
    if network is None or not hasattr(network, "summary"):
        return
    app = wiring.app
    try:
        if fresh and hasattr(network, "invalidate"):
            network.invalidate()
        s = network.summary()
        value = {"state": s.get("state") or "unknown", "type": s.get("type") or "", "name": s.get("name") or ""}
    except Exception:
        _log.exception("Netz lesen")
        return
    wiring.main.post(lambda: app.state.update(network=value))


def push_hdr(wiring: Any) -> None:
    """Arbeits-Thread: HDR-Zustand {supported, enabled, active}."""
    tree = wiring.tree
    display = tree.area("display") if tree is not None else None
    if display is None or not hasattr(display, "hdr_state"):
        return
    app = wiring.app
    try:
        value = {k: bool(v) for k, v in dict(display.hdr_state()).items()}
    except Exception:
        _log.exception("HDR-Zustand lesen")
        return
    wiring.main.post(lambda: app.state.update(hdr=value))


def _updater(wiring: Any) -> tuple[Any, Any]:
    tree = wiring.tree
    system = tree.area("system") if tree is not None else None
    updater = getattr(system, "updater", None)
    # Im Prüfmodus ist der Updater abgeschaltet (außer mit eingestecktem Attrappen-Skript):
    # Dann wird hier weder gesucht noch der Zustand des echten Systems gelesen.
    if updater is None or not getattr(updater, "enabled", False):
        return None, None
    return system, updater


def _launcher_new(wiring: Any) -> bool:
    """E116: Gibt es eine neue Fassung des PC5 Launchers, die noch nicht installiert ist (wiring_selfupdate)?"""
    svc = getattr(wiring, "self_update", None)
    try:
        return bool(svc is not None and svc.available() and not svc.pending())
    except Exception:
        _log.exception("Selbst-Update: Stand")
        return False


def push_update_badge(wiring: Any) -> None:
    """Arbeits-Thread: Marke am Zahnrad (sys.state.update_available)."""
    system, _updater_obj = _updater(wiring)
    if system is None:
        return
    app = wiring.app
    try:
        available = system.update_badge().get("state") == "available"
    except Exception:
        _log.exception("Update-Marke")
        return
    # dazu Apps, Proton und Spiele (updates.py; Laufzeiten allein machen keinen Punkt)
    found = getattr(getattr(wiring, "update_watch", None), "found", None) or {}
    others = any(not a.get("runtime") for a in found.get("apps") or []) or bool(found.get("proton")) \
        or bool(found.get("games")) or _launcher_new(wiring)
    wiring.main.post(lambda: app.state.update(update_available=bool(available or others)))


def update_check(wiring: Any) -> dict | None:
    """Arbeits-Thread: die tägliche Suche nach System-Updates (E32). Der Updater sucht höchstens
    einmal je Tag und nie, solange ein Spiel läuft. Findet er etwas: eine Mitteilung und die
    Marke am Zahnrad. Installiert wird nur auf Knopfdruck in den Einstellungen."""
    system, updater = _updater(wiring)
    if updater is None:
        return None
    try:
        result = updater.check_if_due()
    except Exception:
        _log.exception("Suche nach System-Updates")
        return None
    push_update_badge(wiring)
    if result and result.get("phase") == "available":
        _log.info("System-Update verfügbar")
        wiring.app.notify.push("Systemsoftware-Update verfügbar",
                               "Installieren unter Einstellungen › System › Systemsoftware.", icon="update")
    return result


def refresh_state(wiring: Any, periodic: bool = False) -> None:
    """Arbeits-Thread: Ton, Netz und HDR für Statusleiste und Control Center. periodic: der
    regelmäßige Durchgang liest Ton und Netz nur, wenn ihr Beobachter nicht läuft."""
    p = part(wiring)
    audio = wiring.audio
    watching = audio is not None and audio.watching()
    if not (periodic and watching):
        request_volume(wiring)
    if not (periodic and p.net is not None and p.net.alive()):
        push_network(wiring, fresh=periodic)
    push_hdr(wiring)


def setup_state(wiring: Any) -> None:
    app, p = wiring.app, part(wiring)
    app.state.update(ui=ui_state(app.config))
    push_jobs = getattr(wiring, "_push_jobs", None)
    if push_jobs is not None:
        push_jobs()
    wiring._bg(refresh_state, wiring)

    def tick() -> None:
        if p.closed or wiring.main.closed.is_set():
            return
        try:
            refresh_state(wiring, periodic=True)
        finally:
            p.tick_timer = p.scheduler.later(STATE_TICK_S, tick)
    p.tick_timer = p.scheduler.later(STATE_TICK_S, tick)


# ================================================================ Bild
def display_apply(wiring: Any) -> None:
    """Arbeits-Thread: gespeicherten Bild-Zustand an Gamescope schreiben (Start, nach einem
    Wechsel der Ausgabe) und den HDR-Zustand melden. Beendet auch eine Probezeit, die ein
    Absturz des Home überlebt hat (HDR nur, wenn je bestätigt)."""
    tree = wiring.tree
    display = tree.area("display") if tree is not None else None
    if display is None:
        return
    try:
        display.apply_on_start()
    except Exception:
        _log.exception("Bild-Einstellungen anwenden")
    push_hdr(wiring)


def _size_changed(wiring: Any) -> None:
    """Hauptschleife: Die Wurzel hat die Größe gewechselt. Erst wenn sie steht, neu anwenden
    (Gamescope springt bei HDMI-Aussetzern über andere Anschlüsse)."""
    p = part(wiring)
    if p.closed:
        return

    def settled() -> None:
        p.size_timer = None
        tree = wiring.tree
        display = tree.area("display") if tree is not None else None
        if display is not None and hasattr(display, "refresh"):
            try:
                display.refresh()               # Fernseher-Daten neu lesen, Seite neu zeichnen lassen
            except Exception:
                _log.exception("Bildschirmdaten neu lesen")
        display_apply(wiring)
        if wiring.perf is not None:
            _perf_job(wiring, wiring.perf.refresh)    # Schriftgröße „Automatisch“ folgt der Bildhöhe
    p.scheduler.cancel(p.size_timer)
    p.size_timer = p.scheduler.later(SIZE_SETTLE_S, settled)


# ================================================================ Netz
def net_start(wiring: Any) -> None:
    """Arbeits-Thread, nach dem Bereit-Signal: Beobachter für das Netz starten."""
    p = part(wiring)
    if p.net is not None or p.closed:
        return

    def event() -> None:
        if p.closed:
            return
        p.scheduler.cancel(p.net_timer)
        p.net_timer = p.scheduler.later(NET_DEBOUNCE_S, changed)

    def changed() -> None:
        p.net_timer = None
        push_network(wiring, fresh=True)
        _emit_changed(wiring, "network")        # eine offene Netzwerk-Seite zeichnet neu
    p.net = NetWatch(event, popen=p.popen)
    p.net.start()
    if p.closed:
        p.net.stop()


# ================================================================ Proton
def _compat_dirs(app: Any, wiring: Any = None) -> list[str]:
    # wiring ausdrücklich: Beim Einhängen steht app.wiring noch nicht (setup() setzt es am Ende)
    src = getattr(wiring if wiring is not None else getattr(app, "wiring", None), "sources", None)
    if src is not None and getattr(src, "steam_root", None):
        return [os.path.join(src.steam_root, "compatibilitytools.d")]
    if paths.test_mode():
        return []                               # im Prüfmodus nie der echte Ordner des Nutzers
    return [os.path.join(paths.home(), ".local", "share", "Steam", "compatibilitytools.d")]


def resolve_proton(app: Any) -> str:
    """Ordnername des Standard-Proton: der in den Einstellungen gewählte (games.proton), wenn
    es ihn gibt, sonst der, den „Automatisch“ meint (Builds mit FSR 4 zuerst, darin Proton-CachyOS,
    dann der neueste; games.list_proton_builds). "" = keiner gefunden. Ein als voller Pfad eingetragener Build kommt unverändert
    zurück. Blockierend (liest die Ordner der Builds): im Arbeits-Thread rufen; das Ergebnis
    gilt 30 s."""
    from .settings import games
    wanted = str(app.config.get("games.proton", "") or "").strip()
    if os.path.isabs(wanted):
        return wanted if os.path.isfile(os.path.join(wanted, "proton")) else ""
    dirs = _compat_dirs(app)
    stamp = []
    for d in dirs:
        try:
            stamp.append(os.stat(d).st_mtime_ns)
        except OSError:
            stamp.append(0)
    key = (wanted, tuple(dirs), tuple(stamp))
    wiring = getattr(app, "wiring", None)
    holder = part(wiring) if wiring is not None and hasattr(wiring, "settings_part") else None
    now = time.monotonic()
    if holder is not None and holder.proton_cache and holder.proton_cache[0] == key \
            and now - holder.proton_cache[1] < PROTON_CACHE_S:
        return holder.proton_cache[2]
    build = games.resolve_proton(wanted, dirs=dirs)
    name = str(build["id"]) if build else ""
    if holder is not None:
        holder.proton_cache = (key, now, name)
    return name


PROTON_CHOICES_S = 60.0          # so lange gilt die gelesene Liste der Proton-Builds


def proton_choices(wiring: Any) -> None:
    """Arbeits-Thread: die Proton-Builds für die Auswahl je Spiel lesen (lib.get.choices.proton)."""
    from .settings import games
    p = part(wiring)
    try:
        builds = games.list_proton_builds(_compat_dirs(wiring.app, wiring))
        p.proton_builds = [{"value": b["id"], "label": b["label"], "fsr4": bool(b.get("fsr4"))} for b in builds]
    except Exception:
        _log.exception("Proton-Builds lesen")
    p.proton_builds_at = time.monotonic()
    p.proton_builds_pending = False


def _proton_hook(wiring: Any) -> Callable[[Any, dict], None]:
    """Haken für lib.get (LibraryIndex.detail_hooks, Hauptschleife): Windows-Spiele bekommen
    choices.proton = [{value: <Ordnername>, label, fsr4}]. Gelesen wird nie hier, sondern im
    Arbeits-Thread; die Antwort trägt den letzten bekannten Stand."""
    def hook(entry: Any, out: dict) -> None:
        windows = entry.source == "proton" or (entry.source == "local"
                                               and entry.detail.get("platform") == "windows")
        if not windows:
            return
        p = part(wiring)
        if not p.proton_builds_pending and time.monotonic() - p.proton_builds_at > PROTON_CHOICES_S:
            p.proton_builds_pending = True
            wiring._bg(proton_choices, wiring)
        choices = out.setdefault("choices", {})
        if isinstance(choices, dict):
            choices["proton"] = [dict(b) for b in p.proton_builds]
    return hook


# ================================================================ Einhängen
def _defaults() -> dict:
    """Vorgaben aller Schlüssel dieses Teils (die Tabelle in config.py führt sie auch; sie gewinnt)."""
    from . import perf
    from .settings import accessibility, display, games, home, notifications
    out: dict = {}
    for table in (perf.DEFAULTS, display.DEFAULTS, games.DEFAULTS, games.SAVES_DEFAULTS, home.DEFAULTS,
                  notifications.DEFAULTS, accessibility.DEFAULTS):     # SAVES_DEFAULTS: Spielstände (E99)
        out.update(table)
    out.update({"audio.ui_sounds": True, "audio.ui_volume": 0.6, "audio.ui_pack": "couch", "audio.sink": "", "notify.seconds": 5,
                "input.idle_off_minutes": 0, "input.rumble": True,
                "input.lightbar": {"color": "blue", "brightness": 100}})
    return out


def extend(wiring: Any) -> None:
    """Was über die Methoden in wiring.py hinausgeht: Vorgaben, alles Langsame nach dem
    Bereit-Signal, Größenwechsel der Ausgabe, tägliche Update-Suche, Haken zum Ende."""
    app, p = wiring.app, part(wiring)
    try:
        app.config.add_defaults(_defaults())
    except Exception:
        _log.exception("Vorgaben der Einstellungen anmelden")

    # --- Bild: beim Start und nach jedem Größenwechsel der Wurzel
    wiring.after_ready.append(lambda: wiring._bg(display_apply, wiring))
    app.gs.watch(lambda what: _size_changed(wiring) if what == "size" else None)

    # --- Netz: Beobachter statt Abfragen
    wiring.after_ready.append(lambda: wiring._bg(net_start, wiring))

    # --- Bluetooth: Probezeit eines abgestürzten Home nachholen (Marke im Zustandsordner)
    wiring.after_ready.append(lambda: wiring._bg(bt_trial_restore, wiring))

    # --- Auswahl der Proton-Fassung je Spiel: Liste nach dem Start lesen, lib.get reicht sie durch
    hooks = getattr(getattr(app, "library", None), "detail_hooks", None)
    if isinstance(hooks, list):
        hooks.append(_proton_hook(wiring))
        wiring.after_ready.append(lambda: wiring._bg(proton_choices, wiring))

    # --- System-Update: einmal je Tag nach dem Start (E32); im Prüfmodus nichts
    def update_run() -> None:
        if p.closed:
            return
        try:
            update_check(wiring)
        finally:
            if not p.closed:
                p.update_timer = p.scheduler.later(UPDATE_AGAIN_S, update_run)

    def update_arm() -> None:
        if _updater(wiring)[1] is None:
            return
        wiring._bg(push_update_badge, wiring)   # was die letzte Suche ergab, gleich zeigen
        p.update_timer = p.scheduler.later(UPDATE_FIRST_S, update_run)
        p.update_armed = True
    wiring.after_ready.append(update_arm)

    # --- Ausschalten/Abmelden: nicht mitten in einem System-Update
    def update_busy() -> str | None:
        tree = wiring.tree
        system = tree.area("system") if tree is not None else None
        text = getattr(system, "busy_text", None)
        return text() if callable(text) else None
    wiring.busy_checks.append(update_busy)

    wiring.shutdown_hooks.append(p.close)
