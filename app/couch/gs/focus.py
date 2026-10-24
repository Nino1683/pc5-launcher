"""FocusManager: wer vorn ist, Overlay-Zustand, Vorgaben der Sitzung (Bauplan 4.4, E2, E6, E12).

Fokus geht nur über GAMESCOPECTRL_BASELAYER_APPID an der Wurzel von Server 0: die Liste
[vorn, HOME]. Gamescope zeigt die erste App der Liste, die ein Fenster hat; verschwindet das
Fenster der vorderen (sie wird geschlossen oder endet), ist ohne Zutun das Home zu sehen
(Bauplan 4.5: „App endet: Gamescope zeigt von selbst das Home“). Die Fenster von Home und
Overlay bleiben dafür immer eingeblendet.

Apps im Hintergrund stehen bewusst NICHT in der Liste. Sie sind dort meist angehalten; stünden
sie drin, zeigte Gamescope nach dem Schließen der vorderen App ihr eingefrorenes Bild, während
das Home die schließende App noch für vorn hält und das Pad tot ist (Befund der Prüfer vom
2026-10-14). Eine Hintergrund-App kommt nur über set_foreground wieder nach vorn.
"""
from __future__ import annotations

from typing import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .. import APP_ID_BASE, HOME_APPID, log  # noqa: E402
from . import GamescopeError  # noqa: E402

_log = log.get("focus")

APP_ID_MAX = 0x7FFFFFFF
STEAM_CLIENT_APPID = 769            # Steams eigene Fenster (Client, Meldungen): nie umbenennen
# Fenster eines Launchers, während ein Spiel über ihn startet (E94): diese Nummer gehört zu keiner App,
# Gamescope zeigt sie nie (aus dem Bereich unterhalb unserer App-IDs)
HIDDEN_APPID = 0x7EFFFFFE
# Zeigt das Spiel so lange kein eigenes Fenster, will der Launcher etwas (Anmeldung, Update, Kauf):
# dann werden seine Fenster sichtbar, sonst sähe man nur das Ladebild
REVEAL_AFTER_S = 60
# Programme von Wine/Proton selbst (Desktop, Controller-Hilfe, Bildschirmtastatur): Ihre kleinen
# Fenster bleiben mit dem Launcher verborgen und zählen nie als „das Spiel ist da“
WINE_HELPERS = frozenset({"explorer.exe", "xalia.exe", "tabtip.exe", "conhost.exe", "services.exe", "winedevice.exe",
                          "plugplay.exe", "svchost.exe", "rpcss.exe", "start.exe"})
# Installer und Fehlerhelfer, die ein Launcher beim Anmelden oder Aktualisieren startet (E99): Ihr Fenster
# zählt nie als „das Spiel ist da“, sonst endete das Ladebild, die Launcher-Maus ginge aus, und der
# Launcher bliebe für immer verborgen. Bis zum Spiel bleiben sie mit ihm verborgen, danach sichtbar
# (z. B. der Fehlerdialog von winedbg, wenn das Spiel abstürzt)
SETUP_HELPERS = frozenset({"vc_redist.x64.exe", "vc_redist.x86.exe", "vcredist_x64.exe", "vcredist_x86.exe",
                           "dxsetup.exe", "dxwebsetup.exe", "winedbg.exe", "crashpad_handler.exe"})
OPAQUE = 0xFFFFFFFF
MODES = ("hidden", "passive", "active")
SETTLE_SECONDS = 3.0            # so lange muss die Wurzelgröße stehen (Befund 19)
NET_DELAY_MS = 200              # Gamescope vergibt die App-ID selbst; erst danach nachsehen
NET_RECHECK_MS = 2000           # und noch einmal, falls Steam das Fenster erst später kennzeichnet
# Ein fremder Schreiber der Liste (Steam) wird überschrieben, aber kein Dauerfeuer:
REWRITE_LIMIT, REWRITE_WINDOW = 5, 10.0
RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440), "2160p": (3840, 2160)}


def is_app_id(value: int) -> bool:
    """Eine von uns vergebene App-ID (E3)?"""
    return APP_ID_BASE < value <= APP_ID_MAX


def parse_resolution(text, native: tuple[int, int]) -> tuple[int, int]:
    """"native" | "1440p" | "1080p" (auch "720p", "2160p", "BxH") -> Breite, Höhe.
    Mehr als die Ausgabe hergibt, gibt es nicht (kein Supersampling): dann nativ."""
    size = None
    if isinstance(text, str):
        size = RESOLUTIONS.get(text.strip().lower())
        if size is None and "x" in text:
            try:
                w, h = (int(v) for v in text.lower().split("x", 1))
                size = (w, h) if w >= 320 and h >= 200 else None
            except ValueError:
                size = None
    if size is None or size[0] > native[0] or size[1] > native[1]:
        return native
    return size


class FocusManager:
    def __init__(self, gs, config, settle_seconds: float = SETTLE_SECONDS):
        self._gs = gs
        self._cfg = config
        self._settle = settle_seconds
        self.overlay_mode = "hidden"
        self.requested: int | None = None      # App, die vorn sein soll (None = Home)
        self._foreground: int | None = None    # App, die laut Gamescope vorn ist
        self._mru: list[int] = []              # zuletzt nach vorn geholte zuerst
        self._xid = {"home": 0, "overlay": 0}
        self._listeners: list[Callable[[], None]] = []
        # app_id -> Programme, deren Fenster verborgen bleiben (spec.hide_windows, E94); setzt der Kern
        self.hide_windows: Callable[[int], tuple] | None = None
        # (app_id, revealed): das Spiel zeigt sein erstes Fenster (False) oder der Launcher wird sichtbar (True)
        self.on_launcher: Callable[[int, bool], None] | None = None
        self._hidden: dict[int, dict] = {}     # app_id -> {"xids": {xid: server}, "game": bool, "revealed": bool}
        self._app_options: dict[int, dict] = {}
        self._started = False
        self._settle_timer = 0
        self._net_timer = 0
        self._net_pending: set[int] = set()
        self._rewrites: list[float] = []
        self._rewrite_muted = False
        self._window_takes: list[float] = []    # fremd erzwungene Fenster (E98), wie _rewrites
        self._window_muted = False

    # ------------------------------------------------------------ Schnittstelle
    @property
    def foreground(self) -> int | None:
        """Unsere App-ID auf der Basisebene, None = Home. Bleibt beim Spiel, auch solange
        das Overlay die Eingabe hat (GAMESCOPE_FOCUSED_APP ist dann leer)."""
        return self._foreground

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def tag_window(self, role: str, xid: int) -> None:
        """Markiert ein eigenes Fenster, bevor es eingeblendet wird (UiHost.on_realize)."""
        if role == "home":
            self._xid["home"] = xid
            self._gs.set("STEAM_GAME", [HOME_APPID], window=xid)
        elif role == "overlay":
            self._xid["overlay"] = xid
            self._gs.set("STEAM_OVERLAY", [1], window=xid)
            self._apply_overlay(blur=False)     # beginnt versteckt (bzw. im aktuellen Zustand)
        else:
            raise ValueError("unbekannte Rolle: %r" % (role,))
        sync = getattr(self._gs, "sync", None)
        if sync:
            sync(0)                             # steht am Server, bevor GTK das Fenster einblendet

    def start(self) -> None:
        """Beobachter an, Liste [HOME] schreiben, Vorgaben der Sitzung setzen."""
        if not self._started:
            self._started = True
            self._gs.watch(self._on_gs)
            watch_windows = getattr(self._gs, "watch_windows", None)
            if watch_windows:
                watch_windows(self._on_map)
            self._cfg.on_change("display.", self._on_config)
        # Ein erzwungenes Fenster schlägt die Liste (01 2.4); Reste davon dürfen nicht bleiben
        if self._gs.get("GAMESCOPECTRL_BASELAYER_WINDOW") is not None:
            self._gs.delete("GAMESCOPECTRL_BASELAYER_WINDOW")
        self.requested = None
        self._rewrites.clear()
        self._rewrite_muted = False
        self._window_takes.clear()
        self._window_muted = False
        self._write_list()
        self._apply_blur()
        self._refresh_foreground(notify=False)
        self.apply_session_defaults()
        self._hdr_at_start()

    def set_foreground(self, app_id: int | None) -> None:
        if app_id == HOME_APPID or not app_id:
            app_id = None
        self.requested = app_id
        if app_id is not None:
            if app_id in self._mru:
                self._mru.remove(app_id)
            self._mru.insert(0, app_id)
            del self._mru[32:]
        self._rewrites.clear()
        self._rewrite_muted = False
        self._window_takes.clear()
        self._window_muted = False
        self._write_list()
        self._foreign_window()                  # ein eigener Wunsch schlägt ein fremd erzwungenes Fenster
        self._apply_resolution()

    def overlay(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError("Overlay-Zustand %r gibt es nicht" % (mode,))
        changed = mode != self.overlay_mode
        self.overlay_mode = mode
        self._apply_overlay()
        if changed:
            self._notify()

    def apply_session_defaults(self) -> None:
        """Beim Start und 3 s nach jedem Größenwechsel der Wurzel (Gamescope springt bei
        HDMI-Aussetzern über andere Anschlüsse; Server 1 folgt der Ausgabe nicht von selbst)."""
        # ITM hebt SDR-Inhalt auf HDR-Helligkeit: für eine Oberfläche unerwünscht (01 5.3)
        self._set_if("GAMESCOPE_HDR_ITM_ENABLE", [0])
        # Kein Bildraten-Limit von hier (E23): GAMESCOPE_FPS_LIMIT begrenzt in diesem Gamescope
        # nicht. Das Limit setzt allein couch.perf über gamescopectl, nur solange eine App vorn ist.
        self._apply_resolution()

    # ------------------------------------------------------------ Zusätze
    def set_app_options(self, app_id: int, fps_limit: int | None = None,
                        resolution: str | None = None) -> None:
        """Werte je App (aus den Optionen des Bibliothekseintrags). resolution None = es gilt
        die Einstellung display.game_resolution. fps_limit wird nur gemerkt: Das Limit je Spiel
        wendet couch.perf an (PerfService.apply_for_app, E23), nicht mehr dieser Dienst."""
        self._app_options[app_id] = {"fps_limit": fps_limit, "resolution": resolution}
        while len(self._app_options) > 64:
            self._app_options.pop(next(iter(self._app_options)))
        self._apply_resolution()

    def set_hdr(self, on: bool) -> None:
        """HDR-Ausgabe zur Laufzeit (E12). Ob der Bildschirm mitmacht, zeigt
        gs.hdr_output_active(); die Bestätigung am Bildschirm führt, wer das aufruft."""
        self._gs.set("GAMESCOPE_DISPLAY_HDR_ENABLED", [1 if on else 0])

    # ------------------------------------------------------------ Liste
    def _desired(self) -> list[int]:
        if self.requested is None:
            return [HOME_APPID]
        # nur vorn und Home; Hintergrund-Apps bleiben draußen (siehe Modulkopf)
        return [self.requested, HOME_APPID]

    def _write_list(self) -> bool:
        want = self._desired()
        if self._gs.get("GAMESCOPECTRL_BASELAYER_APPID") == want:
            return False                        # gleich: nicht schreiben, kein neues Ereignis
        self._gs.set("GAMESCOPECTRL_BASELAYER_APPID", want)
        return True

    def _foreign_write(self) -> None:
        want = self._desired()
        have = self._gs.get("GAMESCOPECTRL_BASELAYER_APPID")
        if have == want or self._rewrite_muted:
            return
        now = GLib.get_monotonic_time() / 1e6
        self._rewrites = [t for t in self._rewrites if now - t < REWRITE_WINDOW]
        if len(self._rewrites) >= REWRITE_LIMIT:
            self._rewrite_muted = True
            _log.error("Fokusliste wird laufend von jemand anderem überschrieben (%s), gebe nach", have)
            return
        self._rewrites.append(now)
        _log.warning("Fokusliste fremd geändert: %s statt %s, schreibe neu", have, want)
        self._gs.set("GAMESCOPECTRL_BASELAYER_APPID", want)

    def _foreign_window(self) -> None:
        """Steam in Big Picture (E98, 2026-10-23) erzwingt mit GAMESCOPECTRL_BASELAYER_WINDOW sein Fenster
        und schlägt damit die Liste: Man kam weder mit PS noch mit PS + Options zurück, und nach dem
        Ende von Steam blieb dessen letztes Bild stehen. Zurücknehmen, aber auch hier kein Dauerfeuer."""
        have = self._gs.get("GAMESCOPECTRL_BASELAYER_WINDOW")
        if have is None or self._window_muted:
            return
        now = GLib.get_monotonic_time() / 1e6
        self._window_takes = [t for t in self._window_takes if now - t < REWRITE_WINDOW]
        if len(self._window_takes) >= REWRITE_LIMIT:
            self._window_muted = True
            _log.error("Fenster %s wird laufend erzwungen, gebe nach", have)
            return
        self._window_takes.append(now)
        _log.warning("Fremd erzwungenes Fenster %s (GAMESCOPECTRL_BASELAYER_WINDOW) zurückgenommen", have)
        self._gs.delete("GAMESCOPECTRL_BASELAYER_WINDOW")

    # ------------------------------------------------------------ Ereignisse
    def _guard(fn):                             # noqa: N805
        def wrapper(self, *args):
            try:
                return fn(self, *args)
            except GamescopeError as e:
                _log.error("%s: %s", fn.__name__, e)
                return False
        return wrapper

    @_guard
    def _on_gs(self, kind: str) -> None:
        if kind == "focus":
            self._refresh_foreground()
        elif kind == "apps":
            self._write_list()                  # schreibt nur, falls die Liste nicht mehr stimmt
        elif kind == "baselayer":
            self._foreign_write()
        elif kind == "basewindow":
            self._foreign_window()
        elif kind == "size":
            self._follow_size()
            if self._settle_timer:
                GLib.source_remove(self._settle_timer)
            self._settle_timer = GLib.timeout_add(max(1, int(self._settle * 1000)), self._on_settled)

    @_guard
    def _on_settled(self) -> bool:
        self._settle_timer = 0
        self.apply_session_defaults()
        return False

    def _on_config(self, key: str, _value) -> None:
        try:
            if key == "display.game_resolution":
                self._apply_resolution()
            elif key == "display.overlay_blur":
                self._apply_blur()
        except GamescopeError as e:
            _log.error("Einstellung %s: %s", key, e)

    def _refresh_foreground(self, notify: bool = True) -> None:
        gfx = self._gs.get("GAMESCOPE_FOCUSED_APP_GFX")
        app = gfx[0] if gfx else self._gs.focused_app()
        new = None if app in (0, HOME_APPID) else app
        if new == self._foreground:
            return
        self._foreground = new
        if notify:
            self._notify()

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                _log.exception("Beobachter %r", fn)

    # ------------------------------------------------------------ Overlay
    def _apply_overlay(self, blur: bool = True) -> None:
        xid = self._xid["overlay"]
        if xid:
            shown = self.overlay_mode != "hidden"
            grab = self.overlay_mode == "active"
            # Beides immer zusammen: Deckkraft 0 allein lässt die Tastatur beim unsichtbaren
            # Overlay (01 3.1). Reihenfolge so, dass es nie unsichtbar die Eingabe hält.
            if shown:
                self._gs.set("_NET_WM_WINDOW_OPACITY", [OPAQUE], window=xid)
                self._gs.set("STEAM_INPUT_FOCUS", [1 if grab else 0], window=xid)
            else:
                self._gs.set("STEAM_INPUT_FOCUS", [0], window=xid)
                self._gs.set("_NET_WM_WINDOW_OPACITY", [0], window=xid)
        if blur:
            self._apply_blur()

    def _apply_blur(self) -> None:
        on = self.overlay_mode == "active" and bool(self._cfg.get("display.overlay_blur", True))
        if on:
            height = self._gs.root_size()[1]
            self._set_if("GAMESCOPE_BLUR_RADIUS", [max(8, round(height / 36))])
        # 2 = immer; "bedingt" (1) spränge schon beim Mauszeiger an (01 2.7)
        self._set_if("GAMESCOPE_BLUR_MODE", [2 if on else 0], absent=None if on else [0])

    def _follow_size(self) -> None:
        # Gamescope zieht das Overlay nur beim Einblenden auf Wurzelgröße, danach nicht mehr (E6)
        xid = self._xid["overlay"]
        resize = getattr(self._gs, "resize_window", None)
        if xid and resize:
            width, height = self._gs.root_size()
            resize(xid, width, height)

    # ------------------------------------------------------------ Vorgaben
    def _set_if(self, name: str, values: list[int], absent: list[int] | None = None) -> None:
        """Schreibt nur bei Abweichung. absent: Wert, dem eine fehlende Eigenschaft entspricht."""
        have = self._gs.get(name)
        if have == values or (have is None and absent == values):
            return
        self._gs.set(name, values)

    def _apply_resolution(self) -> None:
        if not getattr(self._gs, "has_game_server", True):
            return                              # nur ein Xwayland: das Home säße mit drin
        native = self._gs.root_size()
        app = self.requested if self.requested is not None else (self._mru[0] if self._mru else None)
        text = self._app_options.get(app, {}).get("resolution") if app is not None else None
        if text is None:
            text = self._cfg.get("display.game_resolution", "native")
        target = parse_resolution(text, native)
        current = getattr(self._gs, "game_root_size", None)
        if current is not None and tuple(current()) == target:
            return                              # Gamescope löscht die Eigenschaft nach dem Lesen:
        #                                         verglichen wird deshalb mit der Wurzel von Server 1
        self._gs.set("GAMESCOPE_XWAYLAND_MODE_CONTROL", [1, target[0], target[1], 0])

    def _hdr_at_start(self) -> None:
        want = bool(self._cfg.get("display.hdr", False)) and bool(self._cfg.get("display.hdr_confirmed", False))
        have = self._gs.get("GAMESCOPE_DISPLAY_HDR_ENABLED")
        if want and have != [1]:
            self.set_hdr(True)
        elif not want and have and have[0] != 0:
            # an, aber nie am Bildschirm bestätigt (Absturz mitten in der Rückfrage): zurück
            self.set_hdr(False)

    # ------------------------------------------------------------ Netz für Fenster ohne App-ID
    def _on_map(self, server: int, xid: int) -> None:
        if server != (1 if getattr(self._gs, "has_game_server", True) else 0):
            return
        self._net_pending.add(xid)
        if not self._net_timer:
            self._net_timer = GLib.timeout_add(NET_DELAY_MS, self._net_check)
        # Steams Overlay setzt seine Nummer manchmal erst nach dem Erscheinen: noch einmal nachsehen
        GLib.timeout_add(NET_RECHECK_MS, self._recheck, xid)

    def _recheck(self, xid: int) -> bool:
        self._net_pending.add(xid)
        if not self._net_timer:
            self._net_timer = GLib.timeout_add(NET_DELAY_MS, self._net_check)
        return False

    @staticmethod
    def _own_reaper(pid: int):
        """Unser reaper weiter oben in der Elternkette, über Steams eigenem reaper, und nur wenn er
        ein Steam-Spiel der Konsole ist (couch-steam-game, E57). Sonst None: Ein Steam in der
        Desktop-App gehört nicht zur App „Desktop“."""
        from ..proc import tree
        cur = pid
        for _ in range(16):
            found = tree.find_reaper(cur)
            if found is None:
                return None
            if is_app_id(found[1]):
                try:
                    with open("/proc/%d/cmdline" % found[0], "rb") as fh:
                        return found if b"couch-steam-game" in fh.read() else None
                except OSError:
                    return None
            cur = found[0]
        return None

    @_guard
    def _net_check(self) -> bool:
        """Fenster, die am reaper vorbei keine App-ID bekommen haben, aber über ihre PID und
        die Elternkette zu einem unserer reaper gehören, bekommen STEAM_GAME nachgesetzt.
        Ebenso Spielfenster, denen Steam (Overlay) seine Spielnummer gibt, obwohl das Spiel als
        App der Konsole läuft (E57): Sonst ordnet Gamescope das Fenster nicht unserer App zu, das
        Home bleibt vorn und man hört das Spiel nur (2026-10-19, Worms Revolution)."""
        from ..proc import tree
        self._net_timer = 0
        pending, self._net_pending = self._net_pending, set()
        server = 1 if getattr(self._gs, "has_game_server", True) else 0
        listed = self._gs.focusable_windows()
        for xid in pending:
            state = self._gs.window_state(xid, server=server)
            if not state or not state["viewable"] or state["input_only"]:
                continue
            pid = state["pid"]
            if not pid:
                continue
            tag = state["steam_game"]
            if self._hide_launcher(xid, pid, tag, server):
                continue
            if tag:
                if is_app_id(tag) or tag == STEAM_CLIENT_APPID:
                    continue                    # schon unsere Nummer, oder ein Fenster von Steam selbst
                # Nur ein Spiel unter Steams eigenem reaper mit genau dessen Nummer, und darüber
                # unser reaper (Steam-Spiel als App der Konsole); alles andere bleibt, wie es ist
                from ..proc import tree
                steam = tree.find_reaper(pid)
                own = self._own_reaper(steam[0]) if steam and steam[1] == tag and not is_app_id(steam[1]) else None
                if own is not None:
                    _log.info("Fenster 0x%x (PID %d) trägt Steams Nummer %d, setze STEAM_GAME=%d", xid, pid, tag, own[1])
                    self._gs.set("STEAM_GAME", [own[1]], window=xid, server=server)
                continue
            if any(x == xid and app and p == pid for x, app, p in listed):
                continue                        # Gamescope kennt es schon
            found = tree.find_reaper(pid)
            if found is not None and not is_app_id(found[1]):
                found = self._own_reaper(found[0])  # Spiel unter Steams reaper, darüber unser reaper
            if found is None or not is_app_id(found[1]):
                continue
            _log.info("Fenster 0x%x (PID %d) ohne App-ID, setze STEAM_GAME=%d", xid, pid, found[1])
            self._gs.set("STEAM_GAME", [found[1]], window=xid, server=server)
        return False

    def _hide_launcher(self, xid: int, pid: int, tag: int, server: int) -> bool:
        """Fenster eines Launchers (Battle.net, Ubisoft Connect, Rockstar), während über ihn ein Spiel
        startet (spec.hide_windows, E94): Es bekommt eine Nummer, die keine App hat, und bleibt
        unsichtbar; man sieht das Ladebild, bis das Spiel selbst ein Fenster zeigt."""
        if tag == HIDDEN_APPID or self.hide_windows is None:
            return tag == HIDDEN_APPID
        from ..proc import tree
        app_id = tag if tag and is_app_id(tag) else None
        if app_id is None:
            found = tree.find_reaper(pid)
            app_id = found[1] if found is not None and is_app_id(found[1]) else None
        if app_id is None:
            return False
        try:
            names = self.hide_windows(app_id) or ()
        except Exception:
            _log.exception("hide_windows")
            return False
        if not names:
            return False
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                argv0 = fh.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            return False
        program = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if app_id not in self._hidden and len(self._hidden) >= 16:
            self._hidden.pop(next(iter(self._hidden)))      # längst beendete Starts
        state = self._hidden.setdefault(app_id, {"xids": {}, "game": False, "revealed": False})
        setup = program in SETUP_HELPERS
        if program not in names and program not in WINE_HELPERS and not setup:
            if not state["game"]:
                state["game"] = True            # das Spiel selbst: Launcher-Fenster bleiben verborgen
                self._launcher_event(app_id, False)
            return False
        if state["revealed"]:
            return False                        # der Launcher will etwas von dir: bleibt sichtbar
        if setup and state["game"]:
            return False                        # nach dem Spielstart: Fehlerdialog sichtbar lassen
        if not state["xids"] and not state["game"]:
            GLib.timeout_add_seconds(REVEAL_AFTER_S, self._reveal_launcher, app_id)
        state["xids"][xid] = server
        _log.info("Fenster 0x%x (%s) gehört zum Launcher, bleibt verborgen", xid, program)
        self._gs.set("STEAM_GAME", [HIDDEN_APPID], window=xid, server=server)
        return True

    @_guard
    def _reveal_launcher(self, app_id: int) -> bool:
        """Nach REVEAL_AFTER_S ohne Spielfenster: Die verborgenen Fenster bekommen die Nummer der App."""
        state = self._hidden.get(app_id)
        if state is None or state["game"] or state["revealed"]:
            return False
        state["revealed"] = True
        _log.info("App %d: nach %d s kein Spielfenster, Launcher wird sichtbar", app_id, REVEAL_AFTER_S)
        for xid, server in state["xids"].items():
            try:
                self._gs.set("STEAM_GAME", [app_id], window=xid, server=server)
            except GamescopeError as e:          # Fenster schon zu
                _log.debug("Fenster 0x%x: %s", xid, e)
        self._launcher_event(app_id, True)
        return False

    def _launcher_event(self, app_id: int, revealed: bool) -> None:
        if self.on_launcher is not None:
            try:
                self.on_launcher(app_id, revealed)
            except Exception:
                _log.exception("on_launcher")

    def waits_for_game(self, app_id: int) -> bool:
        """Startet die App ein Spiel über einen Launcher, dessen Fenster noch verborgen sind? Dann
        gilt ein Fenster in Gamescopes Liste noch nicht als „die App ist da“ (das Ladebild bleibt)."""
        if self.hide_windows is None:
            return False
        try:
            if not self.hide_windows(app_id):
                return False
        except Exception:
            return False
        state = self._hidden.get(app_id)
        return not (state and (state["game"] or state["revealed"]))

    def game_seen(self, app_id: int) -> bool | None:
        """Hat das Spiel eines Launcher-Starts schon ein eigenes Fenster gezeigt (E99)? None, wenn es zu
        dieser App keinen verborgenen Launcher gab (kein Launcher-Spiel, Start vor einem Neustart des Home).
        Die Shell hält ein Launcher-Spiel nicht an, solange hier False steht (aufgedeckter Launcher), und
        wartet nach dem Spiel ohne Rückfrage auf den Nachlauf."""
        state = self._hidden.get(app_id)
        return None if state is None else bool(state["game"])

    del _guard
