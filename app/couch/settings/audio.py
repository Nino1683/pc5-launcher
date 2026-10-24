"""Bereich „Ton“ und der AudioService für das Control Center.

Lesen über pw-dump (backends/pipewire.py), Schreiben ausschließlich über run_write:
    wpctl set-volume --limit 1.0 @DEFAULT_AUDIO_SINK@ 0.60     Lautstärke
    wpctl set-mute @DEFAULT_AUDIO_SINK@ 1|0                    Stumm
    pactl set-card-profile <karte> <profil>                    HDMI-Port der Grafikkarte wählen
    pactl set-default-sink|set-default-source <node.name>      Standardgerät
    paplay --device=<senke> --channel-map=… <wav>              Testton
    systemctl --user restart wireplumber                       nach Änderung der Regel für den Controller

Controller (E75, Wunsch 2026-10-20: „Baue auch ein, dass das Mikrofon vom Controller erkannt wird als
Mic-Input“): Per Kabel oder Dongle (GuliKit) bringt der DualSense eine eigene Soundkarte mit. Sein
Mikrofon steht in der Auswahl und wird automatisch zur Eingabe (audio.controller_mic, Vorgabe aus: Der GuliKit-Dongle liefert nur Stille). Seinen Ton-Ausgang
(vier Kanäle, Vibration über Ton) schaltet eine WirePlumber-Regel ab, solange audio.controller_output aus
ist (CONTROLLER_RULE): Silent Hill Townfall stürzte sonst in XAudio2 ab.

Der Fernseher wird über seinen EDID-Namen gefunden, nie über eine feste Portnummer. pactl
set-default-sink schreibt auch WirePlumbers gemerkten Standard um; deshalb setzt der Supervisor
den vorherigen Standard am Sitzungsende zurück (Befund 14) – hier wird nichts zurückgestellt.
Damit dabei nichts verloren geht, meldet der Dienst jedes Umstellen vorher an on_default_sink
(alter Standard, neuer Standard); der Verdrahter notiert es in <state>/sink-before.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time

from . import rows as R
from .backends import pipewire
from .rows import AREA_ORDER, Page, SettingsError

log = logging.getLogger("couch.settings.audio")

UI_SOUNDS_DEFAULT = True          # wie config.DEFAULTS["audio.ui_sounds"]
UI_VOLUME_DEFAULT = 60            # Prozent; gespeichert als Anteil 0.6 (config.DEFAULTS["audio.ui_volume"])
# Klangpakete der Oberfläche (E58). "pc5": Kopie aus einem Decky-Paket in web/sounds/pc5; ihr fehlt
# nur der Fehlerton, dafür gilt der eigene. Fehlt der Ordner (andere Rechner, das Paket nie, E104),
# wird es nicht angeboten und es gilt "couch".
UI_PACKS = (("pc5", "PC5 Sounds"), ("couch", "Eigene Töne"))   # so benannt (Wunsch 2026-10-18); keine Markennamen (Bauplan, Sprache)
UI_PACK_DEFAULT = "couch"         # E104: wie config.DEFAULTS["audio.ui_pack"] (bis 2026-10-24 "pc5")


def ui_packs() -> list:
    """Die Klangpakete, die es hier gibt, als [(id, Name)]: "couch" immer, jedes andere nur, wenn
    sein Ordner web/sounds/<id> da ist."""
    from .. import paths
    web = paths.web_dir()
    return [(k, label) for k, label in UI_PACKS
            if k == "couch" or os.path.isdir(os.path.join(web, "sounds", k))]


def ui_pack(config) -> str:
    """Das wirksame Klangpaket für state.ui.soundPack: das gewählte, wenn es hier da ist, sonst "couch"."""
    value = str(config.get("audio.ui_pack", UI_PACK_DEFAULT) or UI_PACK_DEFAULT)
    return value if value in dict(ui_packs()) else "couch"

TV_MONITOR = "Beyond TV"
PORT_PREFIX = "port:"          # ID einer Ausgabe, deren Senke erst nach einem Profilwechsel existiert
STEP = 5
TEST_LEFT = "/usr/share/sounds/alsa/Front_Left.wav"
TEST_RIGHT = "/usr/share/sounds/alsa/Front_Right.wav"
_EVENT = re.compile(r"Event '(\w+)' on ([\w-]+) #(\d+)")
CONTROLLER_MIC_LABEL = "Mikrofon des Controllers"
CONTROLLER_RULE_NAME = "90-pc5-controller-audio.conf"
CONTROLLER_RULE = """# PC5 Launcher: Ton-Ausgang des Controllers aus (Lautsprecher und Vibration über Ton). Über USB bzw.
# einen Dongle schicken Spiele sonst die Vibration als Ton dorthin; Silent Hill Townfall stürzte dabei
# in XAudio2 ab (2026-10-20). Das Mikrofon des Controllers bleibt. Schalter: Einstellungen › Ton.
monitor.alsa.rules = [
  {
    matches = [
      { node.name = "~alsa_output.usb-Sony_Interactive_Entertainment_DualSense*" }
      { node.name = "~alsa_output.usb-Sony_Interactive_Entertainment_Wireless_Controller*" }
    ]
    actions = {
      update-props = {
        node.disabled = true
      }
    }
  }
]
"""


def controller_rule_path() -> str:
    from .. import paths
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(paths.home(), ".config")
    return os.path.join(base, "wireplumber", "wireplumber.conf.d", CONTROLLER_RULE_NAME)


def _clamp(level) -> int:
    try:
        return max(0, min(100, int(round(float(level)))))
    except (TypeError, ValueError):
        raise SettingsError("Ungültiger Wert.") from None


class AudioService:
    """Ton für Einstellungen und Control Center. Alle Methoden blockieren kurz (pw-dump rund
    20 ms, wpctl/pactl rund 10 ms) und gehören deshalb in einen Thread, nicht in die Hauptschleife.
    """

    def __init__(self, run_read, run_write, config=None, scheduler=None,
                 monitor_name: str = TV_MONITOR, on_change=None,
                 clock=time.monotonic, sleep=time.sleep, cache_seconds: float = 1.5,
                 on_default_sink=None):
        self.run_read = run_read
        self.run_write = run_write
        self.config = config
        self.scheduler = scheduler
        self.monitor_name = monitor_name
        self.on_change = on_change            # fn() nach Änderungen von außen (Beobachter, Fernseher gewählt)
        self.on_default_sink = on_default_sink  # fn(alt, neu) VOR jedem Umstellen der Standard-Ausgabe
        self._clock = clock
        self._sleep = sleep
        self._ttl = cache_seconds
        self._state: dict | None = None
        self._state_at = 0.0
        self._lock = threading.Lock()
        self._tv_timer = None
        self._tv_tries = 0
        self._notify_timer = None
        self._watch_proc = None
        self._watch_thread = None

    # --- Lesen ---------------------------------------------------------------------------

    def state(self, fresh: bool = False) -> dict:
        """Ton-Zustand, höchstens cache_seconds alt. Blockierend."""
        with self._lock:
            if not fresh and self._state is not None and self._clock() - self._state_at < self._ttl:
                return self._state
        st = pipewire.read_state(self.run_read)
        with self._lock:
            self._state, self._state_at = st, self._clock()
        return st

    def invalidate(self) -> None:
        with self._lock:
            self._state = None

    def outputs(self, st: dict | None = None) -> list[dict]:
        """[{id, label, active, tv}]: vorhandene Senken und verfügbare HDMI-Ports ohne Senke."""
        st = st or self.state()
        out, names = [], set()
        for s in st["sinks"]:
            if s["controller"]:
                continue
            names.add(s["name"])
            tv = s["is_hdmi"] and (s["product_name"] or s["nick"]) == self.monitor_name
            out.append({"id": s["name"], "label": self._tv_label() if tv else s["label"],
                        "active": bool(s["is_default"]), "tv": tv})
        for p in st["hdmi_ports"]:
            # Die Karte führt nur ein Stereo-Profil gleichzeitig: weitere Bildschirme stehen als
            # Ports da und bekommen ihre Senke erst beim Auswählen.
            if p["available"] == "no" or not p["stereo_sink"] or p["stereo_sink"] in names \
                    or not p["product_name"]:
                continue
            tv = p["product_name"] == self.monitor_name
            out.append({"id": f"{PORT_PREFIX}{p['card']}|{p['port']}",
                        "label": self._tv_label() if tv else str(p["product_name"]),
                        "active": False, "tv": tv})
        out.sort(key=lambda o: (not o["tv"], o["label"].casefold()))
        return out

    def inputs(self, st: dict | None = None) -> list[dict]:
        """Mikrofone; das des Controllers unter eigenem Namen (E75)."""
        st = st or self.state()
        return [{"id": s["name"], "label": CONTROLLER_MIC_LABEL if s["controller"] else s["label"],
                 "active": bool(s["is_default"]), "controller": bool(s["controller"])}
                for s in st["sources"]]

    def prefer_controller_mic(self) -> str | None:
        """Ist ein Controller-Mikrofon da und audio.controller_mic an, wird es zur Eingabe. Gibt den
        Namen zurück, wenn umgestellt wurde. Verschwindet der Controller, wählt WirePlumber selbst."""
        if self.config is not None and not self.config.get("audio.controller_mic", False):
            return None
        st = self.state(fresh=True)
        pads = [s for s in st["sources"] if s["controller"]]
        if not pads or any(s["is_default"] for s in pads):
            return None
        name = pads[0]["name"]
        self._write(["pactl", "set-default-source", name], "Das Mikrofon des Controllers ließ sich nicht wählen.")
        self.invalidate()
        log.info("Mikrofon des Controllers ist jetzt die Eingabe: %s", name)
        return name

    def apply_controller_output(self, restart: bool = True) -> bool:
        """WirePlumber-Regel passend zu audio.controller_output anlegen bzw. entfernen; True, wenn
        sich etwas geändert hat (dann WirePlumber neu starten, der Ton setzt kurz aus)."""
        from .. import paths, system
        wanted = bool(self.config.get("audio.controller_output", False)) if self.config is not None else False
        path = controller_rule_path()
        try:
            with open(path, encoding="utf-8") as fh:
                exact = fh.read() == CONTROLLER_RULE
            exists = True
        except OSError:
            exact = exists = False
        if (wanted and not exists) or (not wanted and exact):
            return False
        if paths.test_mode():
            pass                                    # Prüfmodus: nie den WirePlumber-Ordner des Nutzers anfassen
        elif wanted:
            os.unlink(path)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            system.write_atomic(path, CONTROLLER_RULE, mode=0o644)
        if restart:
            self._write(["systemctl", "--user", "restart", "wireplumber"], "Der Ton-Dienst ließ sich nicht neu starten.",
                        timeout=20)
            self.invalidate()
        log.info("Ton-Ausgang des Controllers %s", "an" if wanted else "aus (WirePlumber-Regel)")
        return True

    def get(self) -> dict:
        """{level: 0..100, muted, sink: id|None, sinks: [{id, label, active}], available}.
        Zeigt immer die tatsächliche Standard-Senke, nicht die gewünschte."""
        st = self.state()
        cur = pipewire.sink_by_name(st, st["default_sink"])
        return {"level": int(round(cur["volume"] * 100)) if cur else 0,
                "muted": bool(cur["mute"]) if cur else False,
                "sink": cur["name"] if cur else None,
                "sinks": [{"id": o["id"], "label": o["label"], "active": o["active"]} for o in self.outputs(st)],
                "available": cur is not None}

    def get_mic(self) -> dict:
        st = self.state()
        cur = pipewire.source_by_name(st, st["default_source"])
        return {"level": int(round(cur["volume"] * 100)) if cur else 0,
                "muted": bool(cur["mute"]) if cur else False,
                "source": cur["name"] if cur else None,
                "sources": self.inputs(st), "available": cur is not None}

    def _tv_label(self) -> str:
        return f"Fernseher ({self.monitor_name})"

    # --- Schreiben (nur über run_write) ----------------------------------------------------

    def _write(self, argv: list[str], failure: str, timeout: float = 10) -> None:
        rc, _out, err = self.run_write(argv, timeout=timeout)
        if rc != 0:
            log.warning("%s: rc=%s %s", " ".join(argv), rc, (err or "").strip())
            raise SettingsError(failure)

    def _patch(self, kind: str, **changes) -> None:
        # Den gemerkten Zustand gleich nachziehen: Der Regler soll nicht zurückspringen, bis
        # pw-dump den neuen Wert zeigt.
        with self._lock:
            st = self._state
            if not st:
                return
            key = "default_sink" if kind == "sinks" else "default_source"
            for s in st[kind]:
                if s["name"] == st[key]:
                    s.update(changes)

    def set_volume(self, level) -> dict:
        level = _clamp(level)
        self._write(["wpctl", "set-volume", "--limit", "1.0", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}"],
                    "Die Lautstärke ließ sich nicht ändern.")
        self._patch("sinks", volume=level / 100)
        return {"level": level}

    def volume_step(self, direction: int) -> dict:
        """direction > 0 lauter, < 0 leiser, in Schritten von 5 (auf Fünfer gerundet)."""
        cur = self.get()["level"]
        if direction > 0:
            target = (cur // STEP + 1) * STEP
        else:
            target = (-(-cur // STEP) - 1) * STEP
        return self.set_volume(target)

    def set_muted(self, muted: bool) -> dict:
        self._write(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if muted else "0"],
                    "Die Stummschaltung ließ sich nicht ändern.")
        self._patch("sinks", mute=bool(muted))
        return {"muted": bool(muted)}

    def set_sink(self, sink_id: str, remember: bool = True) -> str:
        """Ausgabegerät wählen. sink_id stammt aus outputs(); Unbekanntes wird abgelehnt (nie
        beliebigen Text an pactl geben). remember: Wahl in den Einstellungen festhalten.
        Liefert den Namen der Senke."""
        st = self.state(fresh=True)
        choice = next((o for o in self.outputs(st) if o["id"] == sink_id), None)
        if choice is None:
            raise SettingsError("Dieses Ausgabegerät ist nicht mehr verfügbar.")
        if sink_id.startswith(PORT_PREFIX):
            card, _, port_name = sink_id[len(PORT_PREFIX):].partition("|")
            port = next(p for p in st["hdmi_ports"] if p["card"] == card and p["port"] == port_name)
            name = self._activate_port(port, st["default_sink"])
        else:
            name = sink_id
            self._set_default_sink(name, st["default_sink"])
        if remember and self.config is not None:
            # Leer heißt: Fernseher, sobald er da ist. Ein anderes Gerät bleibt gewählt.
            self.config.set("audio.sink", "" if choice["tv"] else name)
        self.invalidate()
        return name

    def _set_default_sink(self, name: str, old) -> None:
        if self.on_default_sink is not None:
            try:
                self.on_default_sink(old, name)     # erst notieren, dann umstellen
            except Exception:  # noqa: BLE001
                log.exception("Merkzettel der Tonausgabe")
        self._write(["pactl", "set-default-sink", name], "Das Ausgabegerät ließ sich nicht wählen.")
        self.invalidate()
        self.follow_own_streams(name)

    def follow_own_streams(self, sink: str | None = None) -> int:
        """Tonströme der Oberfläche (WebKit von Home und Overlay) auf die Ausgabe sink, Regler auf
        100 % (2026-10-18: Sie hingen am USB-Gerät, das beim Sitzungsstart kurz Standard war, und
        erbten von „python3“ gespeicherte 0 %). Die Lautstärke der Töne regelt die Seite selbst
        (audio.ui_volume). Liefert die Zahl der nachgezogenen Ströme; Fehler nur ins Protokoll."""
        try:
            st = self.state(fresh=True)
        except Exception:  # noqa: BLE001
            log.exception("Tonströme der Oberfläche")
            return 0
        sink = sink or st.get("default_sink")
        done = 0
        for s in st.get("own_streams") or []:
            serial = str(s.get("serial") or "")
            if not serial.isdigit() or not sink:
                continue
            try:
                changed = False
                if s.get("target") != sink:
                    self._write(["pactl", "move-sink-input", serial, sink], "Tonstrom nicht verschiebbar.")
                    changed = True
                if float(s.get("volume", 1.0)) < 0.99:
                    self._write(["pactl", "set-sink-input-volume", serial, "100%"], "Tonstrom nicht einstellbar.")
                    changed = True
                if s.get("mute"):
                    self._write(["pactl", "set-sink-input-mute", serial, "0"], "Tonstrom nicht einstellbar.")
                    changed = True
                done += int(changed)
            except SettingsError as e:
                log.warning("Tonstrom %s der Oberfläche: %s", serial, e)
        if done:
            log.info("%d Tonströme der Oberfläche auf %s gezogen", done, sink)
        return done

    def _activate_port(self, port: dict, old=None) -> str:
        if self.on_default_sink is not None:
            try:
                # schon vor dem Profilwechsel: Er kann die bisherige Senke verschwinden lassen
                self.on_default_sink(old, port["stereo_sink"])
            except Exception:  # noqa: BLE001
                log.exception("Merkzettel der Tonausgabe")
        self._write(["pactl", "set-card-profile", port["card"], port["stereo_profile"]],
                    "Das Ausgabegerät ließ sich nicht wählen.")
        # Die Senke entsteht erst kurz nach dem Profilwechsel; pactl lehnt unbekannte Namen ab.
        for _ in range(10):
            if pipewire.sink_by_name(self.state(fresh=True), port["stereo_sink"]):
                break
            self._sleep(0.2)
        self._set_default_sink(port["stereo_sink"], old)
        return port["stereo_sink"]

    def select_tv(self, monitor_name: str | None = None) -> str | None:
        """Den Fernseher zur Standard-Ausgabe machen (blockierend, ein Durchgang, bis rund 2 s).

        Aufrufen nach dem Sitzungsstart und nach Anschluss-Ereignissen (dafür request_tv mit
        Entprellung). Hat der Nutzer in den Einstellungen ein anderes Gerät gewählt und ist es da,
        bleibt es dabei. Liefert den Namen der Senke oder None (Fernseher-Ausgang dunkel).
        """
        monitor = monitor_name or self.monitor_name
        st = self.state(fresh=True)
        wanted = self.config.get("audio.sink", "") if self.config is not None else ""
        if wanted:
            s = pipewire.sink_by_name(st, wanted)
            if s and not s["controller"]:
                if st["default_sink"] != wanted:
                    self._set_default_sink(wanted, st["default_sink"])
                    self.invalidate()
                else:
                    self.follow_own_streams(wanted)
                return wanted
        port = pipewire.find_monitor_port(st, monitor)
        if port is None:
            return None
        if pipewire.sink_by_name(st, port["stereo_sink"]) is None:
            name = self._activate_port(port, st["default_sink"])
        else:
            name = port["stereo_sink"]
            if st["default_sink"] != name:
                self._set_default_sink(name, st["default_sink"])
            else:
                self.follow_own_streams(name)       # Standard stimmt, die Ströme der Oberfläche vielleicht nicht
        self.invalidate()
        return name

    def request_tv(self, delay: float = 2.5, retries: int = 4) -> None:
        """select_tv mit Entprellung: Die HDMI-Strecke meldet sich beim Wiederkommen mehrfach.
        Ist der Port noch nicht da, wird bis zu retries-mal im selben Abstand neu versucht."""
        if self.scheduler is None:
            return
        self._tv_tries = retries
        self._arm_tv(delay)

    def _arm_tv(self, delay: float) -> None:
        if self._tv_timer is not None:
            self.scheduler.cancel(self._tv_timer)
        self._tv_timer = self.scheduler.later(delay, lambda: self.scheduler.background(lambda: self._tv_job(delay)))

    def _tv_job(self, delay: float) -> None:
        self._tv_timer = None
        try:
            name = self.select_tv()
        except SettingsError:
            name = None
        if name is None and self._tv_tries > 0:
            self._tv_tries -= 1
            self._arm_tv(delay)
        elif name is not None and self.on_change:
            self.on_change()

    def set_source(self, source_id: str) -> str:
        st = self.state(fresh=True)
        if not any(s["id"] == source_id for s in self.inputs(st)):
            raise SettingsError("Dieses Mikrofon ist nicht mehr verfügbar.")
        self._write(["pactl", "set-default-source", source_id], "Das Mikrofon ließ sich nicht wählen.")
        self.invalidate()
        return source_id

    def set_mic_volume(self, level) -> dict:
        level = _clamp(level)
        self._write(["wpctl", "set-volume", "--limit", "1.0", "@DEFAULT_AUDIO_SOURCE@", f"{level / 100:.2f}"],
                    "Der Mikrofonpegel ließ sich nicht ändern.")
        self._patch("sources", volume=level / 100)
        return {"level": level}

    def set_mic_muted(self, muted: bool) -> dict:
        self._write(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if muted else "0"],
                    "Das Mikrofon ließ sich nicht stummschalten.")
        self._patch("sources", mute=bool(muted))
        return {"muted": bool(muted)}

    @staticmethod
    def test_tone_available() -> bool:
        return os.path.isfile(TEST_LEFT) and os.path.isfile(TEST_RIGHT)

    def test_tone(self) -> None:
        """Ton links, dann rechts auf der Standard-Senke (blockierend, rund 3 s). Unverändert
        laut abspielen: WirePlumber merkt sich die Lautstärke je Programm, eine leisere bliebe
        für jeden späteren Testton hängen."""
        sink = self.get()["sink"]
        if not sink:
            raise SettingsError("Kein Ausgabegerät vorhanden.")
        for chan, path in (("front-left", TEST_LEFT), ("front-right", TEST_RIGHT)):
            self._write(["paplay", f"--device={sink}", f"--channel-map={chan}", path],
                        "Der Testton ließ sich nicht abspielen.", timeout=10)

    # --- Beobachter (lesend): Änderungen von außen ----------------------------------------

    def start_watch(self, popen=subprocess.Popen) -> bool:
        """Startet `pactl subscribe` als eigenes Kind und meldet Änderungen über on_change;
        neue oder verschwundene Senken stoßen request_tv an. Der Verdrahter ruft das einmal beim
        Start und stop_watch() am Ende. Nur lesend."""
        if self._watch_proc is not None:
            return True
        try:
            self._watch_proc = popen(["env", "LC_ALL=C", "pactl", "subscribe"], stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True)
        except OSError as e:
            log.warning("pactl subscribe startet nicht: %s", e)
            return False
        self._watch_thread = threading.Thread(target=self._watch_loop, args=(self._watch_proc,), daemon=True)
        self._watch_thread.start()
        return True

    def stop_watch(self) -> None:
        """Beobachter beenden und ausstehende Zeitgeber (Fernseher-Wahl, gebündelte Meldung)
        abstellen: Nach dem Ende des Home soll nichts mehr lesen oder umstellen."""
        proc, self._watch_proc = self._watch_proc, None
        if proc is not None:
            try:
                proc.terminate()      # nur das eigene Kind, über sein Popen-Objekt
            except OSError:
                pass
        self._tv_tries = 0
        if self.scheduler is not None:
            for name in ("_tv_timer", "_notify_timer"):
                handle = getattr(self, name)
                setattr(self, name, None)
                if handle is not None:
                    self.scheduler.cancel(handle)

    def watching(self) -> bool:
        """Läuft der Beobachter? (Sonst liest der Verdrahter den Zustand regelmäßig nach.)"""
        proc = self._watch_proc
        try:
            return proc is not None and proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def _watch_loop(self, proc) -> None:
        for line in proc.stdout:
            self.handle_event(line)

    def handle_event(self, line: str) -> None:
        """Eine Zeile von `pactl subscribe` auswerten (getrennt, damit es ohne Prozess prüfbar ist)."""
        m = _EVENT.search(line)
        if not m:
            return
        kind, what = m.group(1), m.group(2)
        if what not in ("sink", "source", "card", "server"):
            return
        self.invalidate()
        # Senke gekommen/gegangen oder Karte geändert (HDMI-Port bekommt seine ELD): Fernseher
        # neu wählen. Endet von selbst, weil select_tv nichts mehr schreibt, sobald alles stimmt.
        if what == "card" or (what == "sink" and kind in ("new", "remove")):
            self.request_tv()
        if what == "source" and kind == "new" and self.scheduler is not None:
            self.scheduler.later(1.0, self._controller_mic_job)       # Controller angesteckt (E75)
        if self.scheduler is not None and self.on_change is not None:
            # Lautstärketasten erzeugen Ereignisse im Dutzend: bündeln.
            if self._notify_timer is not None:
                self.scheduler.cancel(self._notify_timer)
            self._notify_timer = self.scheduler.later(0.3, self._fire_change)

    def _controller_mic_job(self) -> None:
        from .. import system
        system.background(self._safe_prefer_mic)

    def _safe_prefer_mic(self) -> None:
        try:
            if self.prefer_controller_mic():
                self._fire_change()
        except Exception as e:  # noqa: BLE001
            log.warning("Mikrofon des Controllers: %s", e)

    def _fire_change(self) -> None:
        self._notify_timer = None
        if self.on_change:
            self.on_change()


class AudioArea:
    id = "audio"
    label = "Ton"
    icon = "sound"
    order = AREA_ORDER["audio"]

    def __init__(self, deps):
        self.deps = deps
        shared = deps.extra.get("audio")
        # Der Verdrahter kann seinen AudioService hereinreichen, damit Control Center und
        # Einstellungen denselben Zwischenspeicher benutzen.
        self.service: AudioService = shared or AudioService(
            deps.run_read, deps.run_write, config=deps.config, scheduler=deps.scheduler,
            on_change=lambda: deps.changed("audio"))

    def pages(self) -> list[str]:
        return ["audio", "audio.mic"]

    def page(self, page_id: str) -> Page:
        if page_id == "audio":
            return Page("audio", "Ton", self._main_rows())
        if page_id == "audio.mic":
            return Page("audio.mic", "Mikrofon", self._mic_rows())
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        svc = self.service
        st = svc.state()
        if st["error"]:
            return [R.info("audio.error", "Der Ton-Dienst antwortet nicht.", enabled=False)]
        cur = svc.get()
        outs = svc.outputs(st)
        rows = []
        if outs:
            hint = "" if any(o["tv"] for o in outs) else \
                "Der Fernseher erscheint hier, sobald sein HDMI-Anschluss aktiv ist."
            rows.append(R.select("audio.output", "Ausgabegerät", cur["sink"],
                                 [R.option(o["id"], o["label"]) for o in outs], hint=hint))
        else:
            rows.append(R.info("audio.output", "Ausgabegerät", "Kein Gerät gefunden", enabled=False))
        ok = cur["available"]
        rows.append(R.slider("audio.volume", "Lautstärke", cur["level"], 0, 100, STEP, "%", enabled=ok))
        rows.append(R.toggle("audio.muted", "Stummschalten", cur["muted"], enabled=ok))
        rows.append(R.info("audio.channels", "Anzahl Kanäle", "Stereo",
                           hint="Der Fernseher nimmt unkomprimierten Ton nur in Stereo an."))
        rows.append(R.toggle("audio.controller_output", "Ton und Vibration über den Controller",
                             bool(self.deps.config.get("audio.controller_output", False)), default=False,
                             hint="Nur per Kabel oder Dongle. Aus: Spiele schicken keinen Ton und keine Vibration "
                                  "über die Soundkarte des Controllers (Silent Hill Townfall stürzte damit ab). "
                                  "Das Mikrofon bleibt. Beim Umschalten setzt der Ton kurz aus."))
        if svc.test_tone_available():
            rows.append(R.action("audio.test", "Audio testen", enabled=ok,
                                 hint="Spielt einen Ton erst links, dann rechts ab."))
        mic = svc.get_mic()
        cur_mic = next((s["label"] for s in mic["sources"] if s["active"]), "")
        rows.append(R.link("audio.mic-page", "Mikrofon", "audio.mic",
                           note=cur_mic if mic["sources"] else "Kein Mikrofon"))
        sounds = bool(self.deps.config.get("audio.ui_sounds", True))
        # default: Vorgabe für „Auf Standard zurücksetzen“ (E22: audio.ui_sounds True, audio.ui_volume 0.6)
        rows.append(R.toggle("audio.ui_sounds", "Töne der Oberfläche", sounds, default=UI_SOUNDS_DEFAULT))
        rows.append(R.slider("audio.ui_volume", "Lautstärke der Töne", self._ui_volume(), 0, 100, 10, "%",
                             enabled=sounds, default=UI_VOLUME_DEFAULT))
        rows.append(R.select("audio.ui_pack", "Klangpaket", ui_pack(self.deps.config),
                             [R.option(k, label) for k, label in ui_packs()], enabled=sounds,
                             default=UI_PACK_DEFAULT))
        return rows

    def _ui_volume(self) -> int:
        # Gespeichert als Anteil 0..1 (so liest ihn die Oberfläche); ältere Prozentwerte verkraften.
        raw = self.deps.config.get("audio.ui_volume", 0.6)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 60
        return _clamp(raw * 100 if raw <= 1 else raw)

    def _mic_rows(self) -> list:
        mic = self.service.get_mic()
        if not mic["sources"]:
            return [R.info("audio.input", "Eingabegerät", "Kein Mikrofon gefunden", enabled=False)]
        ok = mic["available"]
        return [
            R.select("audio.input", "Eingabegerät", mic["source"],
                     [R.option(s["id"], s["label"]) for s in mic["sources"]]),
            R.toggle("audio.controller_mic", "Mikrofon des Controllers bevorzugen",
                     bool(self.deps.config.get("audio.controller_mic", False)), default=False,
                     hint="Ist der Controller per USB-Kabel verbunden, wird sein Mikrofon automatisch zur Eingabe. "
                          "Über Bluetooth gibt es kein Mikrofon, über manche Dongles (GuliKit) nur Stille."),
            R.slider("audio.mic-volume", "Mikrofonpegel", mic["level"], 0, 100, STEP, "%", enabled=ok),
            R.toggle("audio.mic-muted", "Mikrofon stummschalten", mic["muted"], enabled=ok),
        ]

    def set(self, row_id: str, value):
        svc = self.service
        if row_id == "audio.output":
            svc.set_sink(str(value))
            return {"refresh": True}
        if row_id == "audio.volume":
            svc.set_volume(value)
            return None
        if row_id == "audio.muted":
            svc.set_muted(bool(value))
            return None
        if row_id == "audio.input":
            svc.set_source(str(value))
            return {"refresh": True}
        if row_id == "audio.mic-volume":
            svc.set_mic_volume(value)
            return None
        if row_id == "audio.mic-muted":
            svc.set_mic_muted(bool(value))
            return None
        if row_id == "audio.controller_mic":
            self.deps.config.set("audio.controller_mic", bool(value))
            if value:
                svc.prefer_controller_mic()
            return {"refresh": True}
        if row_id == "audio.controller_output":
            self.deps.config.set("audio.controller_output", bool(value))
            svc.apply_controller_output()
            return {"refresh": True}
        if row_id == "audio.ui_sounds":
            self.deps.config.set("audio.ui_sounds", bool(value))
            return {"refresh": True}
        if row_id == "audio.ui_volume":
            self.deps.config.set("audio.ui_volume", _clamp(value) / 100)
            return None
        if row_id == "audio.ui_pack":
            if value not in dict(ui_packs()):        # auch "pc5" ohne seinen Ordner (E104)
                raise SettingsError("Dieses Klangpaket gibt es nicht.")
            self.deps.config.set("audio.ui_pack", value)
            return None
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        if row_id == "audio.test":
            self.service.test_tone()
            return None
        raise SettingsError("Diese Aktion gibt es nicht.")


def make_area(deps) -> AudioArea:
    return AudioArea(deps)
