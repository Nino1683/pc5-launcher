"""Die zwei Fenster des Home und die Brücke zu ihren Seiten (Bauplan 4.3).

  home     Vollbild, deckend, couch://app/index.html?role=home
  overlay  ARGB, durchsichtig, so groß wie die Wurzel, …?role=overlay; liegt als STEAM_OVERLAY
           über allem und wird nur über seine Deckkraft gezeigt (das macht der FocusManager)

Außer den beiden WebViews gibt es keine GTK-Widgets: das GTK-Thema des Nutzers wirkt auf alles, was GTK
selbst zeichnet. Geprüfte Teile stammen aus docs/research/prototypes/uitech/skeleton.

Seite -> Python: window.webkit.messageHandlers.couch.postMessage(JSON-Text {method, params})
                 -> Promise mit JSON-Text {"ok": true, "result"} / {"ok": false, "error", "message"}
Python -> Seite: window.__couch.dispatch(ereignis, payload)

WebKit- und JSC-Objekte (JSC.Context, JSC.Value, WebKit.ScriptMessageReply) fasst nur der
Hauptthread an, auch beim Freigeben: JSCs Kontext-Tabelle ist nicht threadsicher. Am 2026-10-23
starb das Home in JSValueUnprotect, weil ein Arbeits-Thread mit einem alten Future den letzten
Verweis auf den Kontext einer Antwort losließ (E99). Deshalb gehen an rpc nur Nummern (_pending).
"""
from __future__ import annotations

import functools
import itertools
import json
import mimetypes
import os
import threading
import time
import urllib.parse
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkX11", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk, Soup, WebKit  # noqa: E402
from gi.repository import JavaScriptCore as JSC  # noqa: E402

from .. import log, paths  # noqa: E402
from ..rpc import page_json  # noqa: E402
from . import DisplayError, require_gamescope_display  # noqa: E402,F401
from .crashwatch import CrashWatch  # noqa: E402

_log = log.get("ui")

ROLES = ("home", "overlay")
PAGE = "couch://app/index.html?role=%s"
BEAT_TIMEOUT = 15.0          # so lange darf der Herzschlag einer Seite ausbleiben
RANGE_MAX = 4 * 1024 * 1024  # größtes Stück einer Range-Antwort
FONT = "Noto Sans"

# Was couch://art ausliefert: Bilder, Töne, Schriften. Die Wurzeln enthalten auch Steams
# Konfiguration; die soll über dieses Schema nicht lesbar sein.
ART_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".bmp": "image/bmp",
    ".avif": "image/avif", ".tga": "image/x-tga",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
}
APP_TYPES = {
    **ART_TYPES, ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
    ".mjs": "text/javascript", ".json": "application/json", ".txt": "text/plain",
}

CSS = """
window.couch-home { background: #000; }
window.couch-overlay, window.couch-overlay > * { background: transparent; box-shadow: none; }
"""


class UiHost:
    def __init__(self, gtk_app: Gtk.Application, rpc: Any,
                 root_size: Callable[[], tuple[int, int]] | None = None):
        self.gtk_app = gtk_app
        self.rpc = rpc
        self.root_size = root_size
        self.on_realize: Callable[[str, int], None] | None = None
        self.on_first_frame: Callable[[str], None] | None = None
        self.on_beat: Callable[[str], None] | None = None
        # fn(rolle, adresse, text): Die Seite selbst ließ sich nicht laden (app/web fehlt oder
        # ist kaputt). main beendet das Home dann sofort, statt die Bereit-Frist abzuwarten.
        self.on_page_failed: Callable[[str, str, str], None] | None = None
        # fn(rolle): Der Web-Prozess dieser Seite stirbt immer wieder, ohne dass sie dazwischen
        # schlägt (CrashWatch). main beendet das Home dann als Absturz (Rückfallebene).
        self.on_crash_loop: Callable[[str], None] | None = None
        # E92: Startanimation? Gefragt nur beim ersten Laden des Home in diesem Prozess
        self.want_intro: Callable[[], bool] | None = None
        self.lang: Callable[[], str] | None = None             # Sprache der Seite (?lang=, i18n)
        self.crash_watch = CrashWatch()
        self._windows: dict[str, Gtk.Window] = {}
        self._views: dict[str, WebKit.WebView] = {}
        self._xids: dict[str, int] = {}
        self._frames = {role: 0 for role in ROLES}
        self._frames_since = time.monotonic()
        self._alive: dict[str, float] = {}           # letzter Herzschlag bzw. Ladebeginn je Rolle
        self._first: dict[str, int] = {role: 0 for role in ROLES}
        self._beating: set[str] = set()              # Rollen, deren Seite seit dem Laden geschlagen hat
        # Nur für die Pause vor dem Neuladen, bewusst getrennt von CrashWatch: Die Pause wächst
        # über 30 s auch dann, wenn die Seite zwischen zwei Abstürzen kurz geschlagen hat (CrashWatch
        # vergisst beim Herzschlag alles und zählt nur die Schleife ohne Leben für die Rückfallebene)
        self._crashes: dict[str, list[float]] = {role: [] for role in ROLES}
        # Offene Antworten an die Seiten (E99): Nummer -> (rolle, reply, value). Nur der Hauptthread
        # liest, füllt und leert die Tabelle; rpc bekommt allein die Nummer (siehe Kopf der Datei)
        self._pending: dict[int, tuple[str, Any, Any]] = {}
        self._tokens = itertools.count(1)
        self._main = threading.get_ident()
        self._warned_thread = False
        self.reloads = {role: 0 for role in ROLES}
        self._web_dir = ""
        self._art_roots: list[str] = []
        self._started = False
        try:
            self.beat_timeout = float(os.environ.get("COUCH_BEAT_TIMEOUT", "")) \
                if paths.test_mode() else BEAT_TIMEOUT
        except ValueError:
            self.beat_timeout = BEAT_TIMEOUT
        rpc.register("sys.firstFrame", self._rpc_first_frame)
        rpc.register("sys.beat", self._rpc_beat)
        rpc.add_sink(self.emit)

    # ================================================================ Aufbau
    def start(self) -> None:
        require_gamescope_display()
        self._web_dir = os.path.realpath(paths.web_dir())
        self._art_roots = paths.art_roots()
        # Ohne die Seite gibt es nie ein Bild: gleich scheitern (Fehlstart nach Sekunden), statt
        # dreimal die Bereit-Frist des Supervisors mit schwarzem Bildschirm abzuwarten
        index = os.path.join(self._web_dir, "index.html")
        if not (os.path.isfile(index) and os.access(index, os.R_OK)):
            raise FileNotFoundError("Die Oberfläche fehlt oder ist nicht lesbar: %s" % index)

        self._context = WebKit.WebContext()
        # Der Handler läuft in diesem Prozess (ohne Sandbox) und entscheidet über die
        # Erlaubnisliste; der Web-Prozess selbst sieht die Dateien des Nutzers nicht.
        self._context.register_uri_scheme("couch", self._on_uri)
        sm = self._context.get_security_manager()
        sm.register_uri_scheme_as_secure("couch")
        sm.register_uri_scheme_as_cors_enabled("couch")
        # Ohne eigene Sitzung schreibt WebKit nach ~/.local/share/python3 und ~/.cache/python3
        self._session = self._network_session()

        # Priorität über USER: ~/.config/gtk-4.0/gtk.css (WhiteSur) überstimmt sonst unsere
        # Regeln, das Overlay wäre dunkelgrau statt durchsichtig (08, Fallstrick 10).
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 100)
        for role in ROLES:
            self._make_window(role)
        self._started = True
        # Alle 2 s nachsehen: Eine hängende Home-Seite soll neu geladen sein, bevor der
        # Supervisor nach 25 s ohne Herzschlag das ganze Home beendet (15 s + 2 s + Laden).
        interval = max(1, int(min(2.0, self.beat_timeout / 3)))
        GLib.timeout_add_seconds(interval, self._watch_beats)

    @staticmethod
    def _network_session() -> WebKit.NetworkSession:
        """Eigene Sitzung mit den Orten aus paths. Lassen sich die Ordner nicht anlegen (Platte
        voll, Rechte), eine flüchtige Sitzung im Speicher: Die Seite braucht nichts davon, und
        ein unwichtiger Ordner darf das Home nie am Start hindern."""
        try:
            os.makedirs(paths.webkit_data_dir(), mode=0o700, exist_ok=True)
            os.makedirs(paths.webkit_cache_dir(), mode=0o700, exist_ok=True)
            return WebKit.NetworkSession.new(paths.webkit_data_dir(), paths.webkit_cache_dir())
        except OSError as e:
            _log.warning("WebKit-Ordner nicht anlegbar (%s): flüchtige Sitzung ohne Ordner", e)
            return WebKit.NetworkSession.new_ephemeral()

    def _settings(self) -> WebKit.Settings:
        st = WebKit.Settings()
        st.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
        st.set_enable_write_console_messages_to_stdout(True)    # Meldungen der Seite ins home.log
        st.set_enable_developer_extras(paths.test_mode())
        st.set_media_playback_requires_user_gesture(False)      # Pad-Tasten sind keine "Geste"
        st.set_enable_page_cache(False)
        st.set_enable_smooth_scrolling(False)
        st.set_enable_back_forward_navigation_gestures(False)
        st.set_sans_serif_font_family(FONT)
        st.set_default_font_family(FONT)
        return st

    def _make_window(self, role: str) -> None:
        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler_with_reply("couch", None)
        ucm.connect("script-message-with-reply-received::couch", self._on_message, role)
        view = WebKit.WebView(
            web_context=self._context, user_content_manager=ucm, settings=self._settings(),
            network_session=self._session,
            website_policies=WebKit.WebsitePolicies(autoplay=WebKit.AutoplayPolicy.ALLOW))
        color = Gdk.RGBA()
        color.parse("rgba(0,0,0,0)" if role == "overlay" else "#000")
        view.set_background_color(color)        # home: kein weißer Blitz; overlay: Alpha je Pixel
        view.connect("web-process-terminated", self._on_terminated, role)
        view.connect("load-failed", self._on_load_failed, role)
        view.connect("decide-policy", self._on_policy)
        view.connect("context-menu", lambda *a: True)

        win = Gtk.ApplicationWindow(application=self.gtk_app,
                                    title="couch" if role == "home" else "couch-overlay")
        win.set_decorated(False)
        if role == "overlay":
            win.remove_css_class("background")  # sonst malt das Thema einen deckenden Hintergrund
            win.add_css_class("couch-overlay")
            # Overlays skaliert Gamescope nicht und führt sie bei Größenwechseln nicht nach:
            # Größe der Wurzel selbst setzen (resize() bei jedem Wechsel).
            width, height = self._root_size()
            win.set_default_size(width, height)
        else:
            win.add_css_class("couch-home")
            win.fullscreen()                    # mit App-ID führt Gamescope Vollbildfenster selbst nach
        win.set_child(view)
        win.connect("realize", self._on_realize, role)
        win.connect("close-request", lambda w: True)
        self._windows[role] = win
        self._views[role] = view
        win.present()
        self._load(role)

    def _root_size(self) -> tuple[int, int]:
        if self.root_size:
            try:
                w, h = self.root_size()
                if w > 0 and h > 0:
                    return int(w), int(h)
            except Exception:
                _log.exception("root_size")
        return 1920, 1080

    def _load(self, role: str) -> None:
        self._alive[role] = time.monotonic()
        self._beating.discard(role)
        uri = PAGE % role
        try:                                    # Sprache der Seite (strings.js, 2026-10-25)
            lang = self.lang() if self.lang is not None else "de"
        except Exception:
            lang = "de"
        if lang != "de":
            uri += "&lang=" + lang
        if paths.test_mode() and os.environ.get("COUCH_TEST_SOUND") != "1":
            # Prüfläufe dürfen nichts hörbar machen (die Töne gingen sonst auf die echten
            # Lautsprecher): Mit mute=1 legt die Seite gar keinen AudioContext an.
            uri += "&mute=1"
        if role == "home" and self.reloads[role] == 0 and self.want_intro is not None:
            try:
                if self.want_intro():
                    uri += "&intro=1"
            except Exception:
                _log.exception("want_intro")
        self._views[role].load_uri(uri)

    # ================================================================ Fenster
    def _on_realize(self, win: Gtk.Window, role: str) -> None:
        surface = win.get_surface()
        xid = GdkX11.X11Surface.get_xid(surface) if isinstance(surface, GdkX11.X11Surface) else 0
        self._xids[role] = xid
        if not xid:
            _log.error("%s: kein X11-Fenster (GDK_BACKEND=%s)", role, os.environ.get("GDK_BACKEND"))
        # Zählt die Bilder, die GTK wirklich abgibt (rAF der Seite sagt darüber nichts, 08 5.3)
        win.get_frame_clock().connect("after-paint", self._after_paint, role)
        # realize läuft vor dem Einblenden: Die Markierung steht am Server, bevor Gamescope
        # das Fenster zum ersten Mal sieht (sonst blitzt das Overlay als normales Fenster auf).
        if xid and self.on_realize:
            try:
                self.on_realize(role, xid)
            except Exception:
                _log.exception("on_realize(%s)", role)

    def _after_paint(self, clock: Any, role: str) -> None:
        self._frames[role] += 1

    def xid(self, role: str) -> int:
        return self._xids.get(role, 0)

    def frames(self, reset: bool = False) -> dict:
        out: dict = dict(self._frames)
        out["seconds"] = round(time.monotonic() - self._frames_since, 3)
        if reset:
            self._frames = {role: 0 for role in ROLES}
            self._frames_since = time.monotonic()
        return out

    def resize(self, width: int | None = None, height: int | None = None) -> None:
        """Die Wurzel hat eine neue Größe (Fernseher-Modus, Sprung auf einen anderen Anschluss)."""
        if width is None or height is None:
            width, height = self._root_size()
        win = self._windows.get("overlay")
        if win is None or width <= 0 or height <= 0:
            return
        _log.info("Overlay folgt der Wurzel: %dx%d", width, height)
        win.set_default_size(int(width), int(height))

    # ================================================================ Brücke
    def _on_message(self, ucm: Any, value: Any, reply: Any, role: str) -> bool:
        try:
            message = json.loads(value.to_string() if value.is_string() else value.to_json(0))
        except Exception:
            message = None
        # reply bleibt gültig, bis geantwortet ist; so können Methoden ein Future liefern. reply und
        # value bleiben hier im Hauptthread; die Nummer darf jeder Thread festhalten und loslassen.
        token = next(self._tokens)
        self._pending[token] = (role, reply, value)
        # Auch dieser Rahmen hält sie nicht: Ein Traceback kann ihn über f_back in einem Zyklus
        # festhalten, und den löst der GC in dem Thread auf, der ihn gerade auslöst (E99)
        del ucm, reply, value
        try:
            self.rpc.handle(role, message, functools.partial(self._answer, token))
        except Exception:
            _log.exception("Brücke: %r", message)
            self._answer(token, {"ok": False, "error": "BridgeError", "message": "Das hat nicht geklappt."})
        return True

    def _answer(self, token: int, obj: dict) -> None:
        """Antwort auf die Nachricht token: höchstens einmal, immer im Hauptthread (E99)."""
        if threading.get_ident() != self._main:
            # Absicherung: rpc liefert Antworten schon in der Hauptschleife ab. Einmal ins Protokoll,
            # damit ein künftiger Aufrufer, der das nicht tut, nicht still durchrutscht
            if not self._warned_thread:
                self._warned_thread = True
                _log.warning("Antwort %d aus Thread %s, in den Hauptthread verschoben",
                             token, threading.current_thread().name)
            GLib.idle_add(self._answer_later, token, obj, priority=GLib.PRIORITY_DEFAULT)
            return
        entry = self._pending.pop(token, None)
        if entry is None:
            return              # Seite neu geladen oder Web-Prozess tot: niemand wartet mehr
        _role, reply, value = entry
        try:
            text = page_json(obj)
        except (TypeError, ValueError) as e:
            text = json.dumps({"ok": False, "error": "BadResult",
                               "message": "Das hat nicht geklappt.", "detail": str(e)})
        reply.return_value(JSC.Value.new_string(value.get_context(), text))

    def _answer_later(self, token: int, obj: dict) -> bool:
        self._answer(token, obj)
        return False

    def _drop_answers(self, role: str) -> None:
        """Die Seite dieser Rolle ist weg: ihre offenen Antworten hier im Hauptthread verwerfen.
        Laut WebKit-Quelltext 2.54.1 (~_WebKitScriptMessageReply) bekommt die alte Seite dann
        undefined; mit echtem WebKit unbestätigt. Ohne das wüchse die Tabelle mit jedem Future ohne
        Zeitgrenze, das nie fertig wird (osk.open, app.launch …)."""
        stale = [t for t, (r, _reply, _value) in self._pending.items() if r == role]
        for t in stale:
            del self._pending[t]
        if stale:
            _log.info("%s: %d offene Antworten verworfen", role, len(stale))

    def emit(self, role: str | None, event: str, payload: dict | None = None) -> None:
        if not self._started:
            return
        js = "window.__couch&&window.__couch.dispatch(%s,%s)" % (
            json.dumps(event), page_json({} if payload is None else payload))
        # Auch ohne Rückruf legt WebKit für das Ergebnis einen GTask mit JSC.Value an und gibt ihn
        # im Hauptthread frei; hier zeigte sich der Absturz vom 2026-10-23, verursacht hat ihn _on_message
        for r in ((role,) if role in ROLES else ROLES):
            self._views[r].evaluate_javascript(js, -1, None, None, None, None)

    def eval(self, role: str, js: str, callback: Callable[[Any], None] | None = None) -> None:
        self.eval_full(role, js, (lambda ok, value: callback(value if ok else None)) if callback else None)

    def eval_full(self, role: str, js: str, callback: Callable[[bool, Any], None] | None = None,
                  asynchronous: bool = False) -> None:
        """callback(ok, wert bzw. fehlertext). asynchronous: js ist der Rumpf einer
        async-Funktion ("return await …"); evaluate_javascript kann keine Promises liefern."""
        view = self._views[role]

        def done(v: WebKit.WebView, res: Gio.AsyncResult) -> None:
            try:
                value = (v.call_async_javascript_function_finish(res) if asynchronous
                         else v.evaluate_javascript_finish(res))
                out = None if value.is_undefined() else json.loads(value.to_json(0))
                ok = True
            except GLib.Error as e:
                ok, out = False, e.message
            if callback:
                callback(ok, out)
        if asynchronous:
            view.call_async_javascript_function(js, -1, None, None, None, None, done)
        else:
            view.evaluate_javascript(js, -1, None, None, None, done)

    def snapshot(self, role: str, path: str, callback: Callable[[bool], None] | None = None) -> None:
        """PNG des WebViews (nur die Seite; mit Alpha beim Overlay)."""
        def done(v: WebKit.WebView, res: Gio.AsyncResult) -> None:
            ok = False
            try:
                texture = v.get_snapshot_finish(res)
                ok = bool(texture.save_to_png(path))
            except GLib.Error as e:
                _log.warning("Snapshot %s: %s", role, e.message)
            if callback:
                callback(ok)
        options = (WebKit.SnapshotOptions.TRANSPARENT_BACKGROUND if role == "overlay"
                   else WebKit.SnapshotOptions.NONE)
        self._views[role].get_snapshot(WebKit.SnapshotRegion.VISIBLE, options, None, done)

    def reload(self, role: str) -> None:
        self.reloads[role] += 1
        self._drop_answers(role)
        self._load(role)

    def crash(self, role: str) -> None:
        """Beendet den Web-Prozess einer Seite (nur für die Prüfung der Wiederherstellung)."""
        self._views[role].terminate_web_process()

    # ================================================================ Lebenszeichen
    def _role_of(self, params: dict) -> str | None:
        role = params.get("_role")
        return role if role in ROLES else None

    def _rpc_first_frame(self, params: dict) -> dict:
        role = self._role_of(params)
        if role:
            self._alive[role] = time.monotonic()
            self._first[role] += 1
            _log.info("erstes Bild: %s (%d.)", role, self._first[role])
            if self.on_first_frame:
                self.on_first_frame(role)
        return {}

    def _rpc_beat(self, params: dict) -> dict:
        role = self._role_of(params)
        if role:
            self._alive[role] = time.monotonic()
            self._beating.add(role)
            self.crash_watch.beat(role)
            if self.on_beat:
                self.on_beat(role)
        return {}

    def first_frames(self) -> dict:
        return dict(self._first)

    def _watch_beats(self) -> bool:
        now = time.monotonic()
        for role in ROLES:
            if now - self._alive.get(role, now) <= self.beat_timeout:
                continue
            # Das Home wird immer neu geladen (ohne Bild ist die Konsole tot). Das Overlay nur,
            # wenn seine Seite schon einmal geschlagen hat: Eine Seite, die versteckt gar nicht
            # schlägt, würde sonst alle 15 s neu geladen und verlöre dabei ihre Ansicht.
            if role != "home" and role not in self._beating:
                continue
            _log.warning("%s: kein Herzschlag seit %.0f s, Web-Prozess wird beendet und die "
                         "Seite neu geladen", role, now - self._alive[role])
            self._restart_page(role)
        return True

    def _restart_page(self, role: str) -> None:
        """Eine Seite ohne Herzschlag hängt meist in ihrem Web-Prozess (Endlosschleife); der
        nimmt dann auch kein load_uri mehr an. Also den Prozess beenden: web-process-terminated
        lädt mit einem frischen neu, ohne dass der Supervisor das ganze Home beenden muss."""
        before = self.reloads[role]
        self._alive[role] = time.monotonic()
        try:
            self._views[role].terminate_web_process()
        except Exception:
            _log.exception("%s: Web-Prozess ließ sich nicht beenden", role)
            self.reload(role)
            return

        def check() -> bool:
            if self.reloads[role] == before:    # das Signal blieb aus: wenigstens neu laden
                _log.warning("%s: kein Neuladen nach dem Beenden des Web-Prozesses, lade selbst", role)
                self.reload(role)
            return False
        GLib.timeout_add(3000, check)

    def _on_terminated(self, view: WebKit.WebView, reason: Any, role: str) -> None:
        # Ohne Neuladen bliebe ein schwarzes Fenster. Der Zustand liegt in Python, die Seite
        # holt ihn beim Laden über sys.state. Stirbt der Prozess immer wieder, wächst die Pause.
        now = time.monotonic()
        recent = [t for t in self._crashes[role] if now - t < 30.0] + [now]
        self._crashes[role] = recent
        delay = min(300 * 2 ** (len(recent) - 1), 5000)
        _log.error("%s: Web-Prozess beendet (%s), lade in %d ms neu", role, reason.value_nick, delay)
        self._alive[role] = now
        self._drop_answers(role)
        if self.crash_watch.crashed(role) and self.on_crash_loop:
            # Immer wieder tot, ohne je zu schlagen: Neuladen hilft nicht, die Rückfallebene schon
            try:
                self.on_crash_loop(role)
            except Exception:
                _log.exception("on_crash_loop(%s)", role)

        def again() -> bool:
            self.reload(role)
            return False
        GLib.timeout_add(delay, again)

    def _on_load_failed(self, view: WebKit.WebView, event: Any, uri: str, error: GLib.Error,
                        role: str) -> bool:
        _log.error("%s: Laden fehlgeschlagen: %s (%s)", role, uri, error.message)
        # Nur der eindeutige Fall zählt: Unser eigener couch://-Dienst konnte die Seite nicht
        # liefern (Datei fehlt oder liegt außerhalb, siehe _on_uri). Ein abgebrochenes Laden
        # (neues load_uri mitten im alten, beendeter Web-Prozess) ist kein Fehler der Seite.
        try:
            missing = (error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND)
                       or error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.PERMISSION_DENIED))
        except Exception:
            missing = False
        if missing and (uri or "").startswith(PAGE.split("?")[0]) and self.on_page_failed:
            try:
                self.on_page_failed(role, uri, error.message)
            except Exception:
                _log.exception("on_page_failed(%s)", role)
        return False

    def _on_policy(self, view: WebKit.WebView, decision: Any, kind: Any) -> bool:
        # Die Oberfläche bleibt bei couch://; nichts navigiert ins Netz oder öffnet Fenster
        if kind in (WebKit.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not uri.startswith("couch://") and uri != "about:blank":
                _log.warning("Navigation verweigert: %s", uri)
                decision.ignore()
                return True
        return False

    # ================================================================ couch://
    def resolve_uri(self, uri: str) -> tuple[str, str]:
        """couch://-Adresse -> (Dateipfad, Inhaltstyp). Wirft PermissionError/FileNotFoundError."""
        parts = urllib.parse.urlsplit(uri)
        path = urllib.parse.unquote(parts.path)
        if "\0" in path:
            raise PermissionError("ungültiger Pfad")
        if parts.netloc == "app":
            full = os.path.realpath(os.path.join(self._web_dir, path.lstrip("/") or "index.html"))
            if not paths.is_under(full, [self._web_dir]) or full == self._web_dir:
                raise PermissionError("außerhalb der Oberfläche")
            types = APP_TYPES
        elif parts.netloc == "files":
            # Bilder im Datei-Explorer (E62): nur an den Orten des Explorers, bei jedem Abruf geprüft
            from .. import files
            full = os.path.realpath(path)
            if not os.path.isabs(path) or not files.allowed(full):
                raise PermissionError("nicht an einem Ort des Datei-Explorers")
            types = ART_TYPES
        elif parts.netloc == "art":
            full = os.path.realpath(path)
            if not os.path.isabs(path) or not paths.is_under(full, self._art_roots):
                raise PermissionError("nicht in den erlaubten Bildordnern")
            types = ART_TYPES
        else:
            raise FileNotFoundError(parts.netloc)
        ext = os.path.splitext(full)[1].lower()
        ctype = types.get(ext)
        if ctype is None:
            if types is ART_TYPES:
                raise PermissionError("Dateityp nicht erlaubt")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if not os.path.isfile(full):
            raise FileNotFoundError(full)
        return full, ctype

    def _on_uri(self, request: Any) -> None:
        uri = request.get_uri()
        try:
            full, ctype = self.resolve_uri(uri)
            f = Gio.File.new_for_path(full)
            size = f.query_info("standard::size", Gio.FileQueryInfoFlags.NONE, None).get_size()
            headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
            # couch://art ist für die Seite (couch://app) ein fremder Ursprung: ohne diese
            # Kopfzeile scheitern fetch(), Canvas und Schriften.
            headers.append("Access-Control-Allow-Origin", "*")
            headers.append("Accept-Ranges", "bytes")
            # fetch() sieht nur diese Kopfzeilen, nicht den Typ der Antwort selbst
            text = ctype.startswith("text/") or ctype == "application/json"
            headers.append("Content-Type", ctype + "; charset=utf-8" if text else ctype)
            incoming = request.get_http_headers()
            rng = incoming.get_one("Range") if incoming else None
            if rng and rng.startswith("bytes="):
                # Medien-Elemente fragen "bytes=0-1445" und verlangen 206 mit genau diesem Stück.
                # WebKit liest einen Strom immer bis zum Ende, deshalb kommt das Stück aus dem
                # Speicher (höchstens RANGE_MAX je Antwort, der Rest über die nächste Anfrage).
                first, _, last = rng[6:].split(",")[0].partition("-")
                start, end = 0, size - 1
                if first == "":
                    start = max(0, size - int(last))
                else:
                    start = int(first)
                    if last:
                        end = min(int(last), size - 1)
                if start > end or start >= size:
                    raise ValueError("Range nicht erfüllbar: %s" % rng)
                end = min(end, start + RANGE_MAX - 1)
                with open(full, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(end - start + 1)
                end = start + len(data) - 1
                stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
                length, status = len(data), 206
                headers.append("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                stream = f.read(None)       # öffnet nur; WebKit liest den Strom asynchron
                length, status = size, 200
                art = urllib.parse.urlsplit(uri).netloc == "art"
                headers.append("Cache-Control", "max-age=600" if art else "no-cache")
            response = WebKit.URISchemeResponse.new(stream, length)
            response.set_content_type(ctype)
            response.set_status(status, None)
            response.set_http_headers(headers)
            request.finish_with_response(response)
        except Exception as e:
            _log.warning("couch:// verweigert: %s (%s: %s)", uri, type(e).__name__, e)
            code = Gio.IOErrorEnum.PERMISSION_DENIED if isinstance(e, PermissionError) \
                else Gio.IOErrorEnum.NOT_FOUND
            request.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), str(e), int(code)))
