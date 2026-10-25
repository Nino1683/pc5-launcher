"""GTK-4-Fenster des Terminals mit WebKit und xterm.js (E72). Start über app/bin/couch-terminal.

Die Seite (page/term.html, term.js) zeichnet das Terminal, die Startauswahl, das Befehlsmenü und
das Schreibfeld; dieses Fenster führt das Pseudo-Terminal (session.Pty) und reicht Bytes durch:
    Seite -> Fenster   {t:"ready"|"run"|"in"|"size"|"stop"|"quit"|"font", …} (JSON, Meldungskanal "term")
    Fenster -> Seite   pc5.recv({t:"init"|"out"|"started"|"exited", …})
Ausgaben des Programms gehen gesammelt (alle FLUSH_MS) und base64-kodiert an die Seite; xterm.js
setzt UTF-8 auch über Stückgrenzen richtig zusammen.

Umgebung:
    COUCH_TERM_SIZE    "BxH" statt der Größe des Bildschirms (Tests)
    COUCH_TERM_RUN     Kennung aus session.ACTIONS, die sofort startet (Tests)
    COUCH_OSK_SOCK     Eingang für Text der Bildschirmtastatur (sonst <run>/osk-terminal.sock)
"""
from __future__ import annotations

import base64
import json
import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402

from .. import log, oskdirect, paths  # noqa: E402
from . import session  # noqa: E402

_log = log.get("terminal")

HERE = os.path.dirname(os.path.realpath(__file__))
APP_DIR = os.path.dirname(os.path.dirname(HERE))           # <release>/app
FLUSH_MS = 12
CHUNK = 256 * 1024                 # höchstens so viel je Lesen, damit die Oberfläche flüssig bleibt
FONT_MIN, FONT_MAX, FONT_DEFAULT = 14, 40, 22
STOP_WAIT_MS = 2500                # so lange darf das Programm nach dem Auflegen zum Beenden brauchen


def load_prefs(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    font = data.get("font") if isinstance(data, dict) else None
    return {"font": font if isinstance(font, int) and FONT_MIN <= font <= FONT_MAX else FONT_DEFAULT}


def save_prefs(path: str, prefs: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f)
    os.replace(tmp, path)


class Terminal:
    def __init__(self, app: Gtk.Application):
        self.app = app
        self.home = paths.home()
        self.prefs_file = os.path.join(paths.data_dir(), "terminal", "prefs.json")
        self.prefs = load_prefs(self.prefs_file)
        self.env = session.child_env(dict(os.environ), self.home, APP_DIR)
        self.pty: session.Pty | None = None
        self.action = ""
        self.size = (120, 36)
        self._out = bytearray()
        self._flush_id = 0
        self._watch_id = 0
        self._child_id = 0
        self._ready = False
        self._quitting = False

        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler("term", None)
        ucm.connect("script-message-received::term", self.on_message)
        self.view = WebKit.WebView(user_content_manager=ucm)
        st = self.view.get_settings()
        st.set_enable_developer_extras(False)
        st.set_enable_page_cache(False)
        st.set_enable_back_forward_navigation_gestures(False)
        st.set_enable_write_console_messages_to_stdout(True)
        color = Gdk.RGBA()
        color.parse("#0b0c10")
        self.view.set_background_color(color)
        self.view.connect("context-menu", lambda *a: True)
        self.view.connect("decide-policy", self.on_policy)
        self.view.connect("web-process-terminated", self.on_crash)

        self.win = Gtk.ApplicationWindow(application=app, title="Claude Code")
        self.win.set_decorated(False)
        self._size_window()
        self.win.set_child(self.view)
        self.win.connect("close-request", lambda *_: self.quit() or True)
        self.win.present()
        uri = GLib.filename_to_uri(os.path.join(HERE, "page", "term.html"), None)
        if os.environ.get("COUCH_LANG") == "en":           # Englisch (2026-10-25), aus dem Start
            uri += "?lang=en"
        self.view.load_uri(uri)
        self.view.grab_focus()
        self.osk = None
        try:                                    # Text der Bildschirmtastatur direkt (oskdirect)
            self.osk = oskdirect.Server(os.environ.get("COUCH_OSK_SOCK") or paths.osk_socket("terminal"),
                                        self.osk_text)
        except OSError as e:
            _log.warning("Eingang der Bildschirmtastatur: %s", e)

    def osk_text(self, text: str) -> None:
        """Wie Einfügen in das Feld mit dem Fokus (xterm.js oder das Schreibfeld der Seite)."""
        self.view.execute_editing_command_with_argument("InsertText", text)

    def _size_window(self) -> None:
        size = os.environ.get("COUCH_TERM_SIZE", "")
        try:
            w, h = (int(v) for v in size.lower().split("x"))
        except ValueError:
            w = h = 0
        if w <= 0 or h <= 0:
            display = Gdk.Display.get_default()
            monitors = display.get_monitors() if display else None
            mon = monitors.get_item(0) if monitors is not None and monitors.get_n_items() else None
            geo = mon.get_geometry() if mon else None
            w, h = (geo.width, geo.height) if geo else (1920, 1080)
        self.win.set_default_size(w, h)

    # ------------------------------------------------------------------ Seite
    def send(self, msg: dict) -> None:
        js = "window.pc5&&pc5.recv(%s)" % json.dumps(msg, ensure_ascii=False)
        self.view.evaluate_javascript(js, -1, None, None, None, None)

    def on_policy(self, view, decision, kind) -> bool:
        # Nur die eigene Seite; Verweise aus dem Terminal öffnen nichts
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not uri.startswith("file://"):
                decision.ignore()
                return True
        elif kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        return False

    def on_crash(self, view, reason) -> None:
        _log.warning("Seite abgestürzt (%s), lade neu", reason)
        self._ready = False
        GLib.timeout_add(500, lambda: self.view.reload() and False)

    def on_message(self, ucm, value) -> None:
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        t = msg.get("t")
        if t == "in":
            data = msg.get("d")
            if isinstance(data, str) and self.pty is not None:
                if not self.pty.write(data.encode("utf-8", "surrogateescape")):
                    GLib.timeout_add(20, self._retry_write)
        elif t == "size":
            self.size = session.clamp_size(msg.get("cols"), msg.get("rows"))
            if self.pty is not None:
                self.pty.resize(*self.size)
        elif t == "ready":
            self.size = session.clamp_size(msg.get("cols"), msg.get("rows"))
            self._ready = True
            self.send({"t": "init", "font": self.prefs["font"], "fontMin": FONT_MIN, "fontMax": FONT_MAX,
                       "claude": session.argv_for("new", self.env) is not None,
                       "running": self.action if self.pty is not None else ""})
            first = os.environ.get("COUCH_TERM_RUN", "")
            if first and self.pty is None and not self.action:
                self.run(first)
        elif t == "run":
            self.run(str(msg.get("what", "")))
        elif t == "stop":
            if self.pty is not None:
                self.pty.signal(signal.SIGHUP)
        elif t == "font":
            font = msg.get("size")
            if isinstance(font, int) and FONT_MIN <= font <= FONT_MAX:
                self.prefs["font"] = font
                try:
                    save_prefs(self.prefs_file, self.prefs)
                except OSError as e:
                    _log.warning("Einstellungen nicht gespeichert: %s", e)
        elif t == "quit":
            self.quit()

    def _retry_write(self) -> bool:
        return self.pty is not None and not self.pty.flush()

    # ------------------------------------------------------------------ Programm
    def run(self, action: str) -> None:
        if self.pty is not None:
            return
        argv = session.argv_for(action, self.env)
        if argv is None:
            self.send({"t": "exited", "what": action, "code": -1, "missing": True})
            return
        try:
            self.pty = session.Pty(argv, self.home, self.env, *self.size)
        except OSError as e:
            _log.error("Start von %s gescheitert: %s", argv, e)
            self.send({"t": "exited", "what": action, "code": -1, "failed": True})
            return
        self.action = action
        _log.info("Gestartet: %s (PID %d)", " ".join(argv), self.pty.pid)
        self._watch_id = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.pty.fd,
                                               GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                                               self.on_readable)
        self._child_id = GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.pty.pid, self.on_exit)
        self.send({"t": "started", "what": action})

    def on_readable(self, fd, cond) -> bool:
        pty = self.pty
        if pty is None:
            return False
        data = pty.read(CHUNK)
        if data is None:
            self._watch_id = 0                          # Ende; das Prozessende meldet on_exit
            return False
        if data:
            self._out += data
            if not self._flush_id:
                self._flush_id = GLib.timeout_add(FLUSH_MS, self.flush_out)
        return True

    def flush_out(self) -> bool:
        self._flush_id = 0
        if self._out:
            self.send({"t": "out", "b": base64.b64encode(bytes(self._out)).decode("ascii")})
            self._out.clear()
        return False

    def on_exit(self, pid, status) -> None:
        pty = self.pty
        if pty is None:
            return
        while True:                                     # Rest der Ausgabe noch zeigen
            data = pty.read(CHUNK)
            if not data:
                break
            self._out += data
        if self._watch_id:
            GLib.source_remove(self._watch_id)
            self._watch_id = 0
        pty.close()
        self.pty = None
        code = os.waitstatus_to_exitcode(status)       # negativ: durch ein Signal beendet (Auflegen)
        _log.info("Beendet: %s (Code %d)", self.action, code)
        if self._flush_id:
            GLib.source_remove(self._flush_id)
        self.flush_out()
        self.send({"t": "exited", "what": self.action, "code": code})
        self.action = ""
        if self._quitting:
            self.app.quit()

    def quit(self) -> None:
        """Fenster zu: das Programm auflegen lassen (Claude Code speichert das Gespräch laufend),
        höchstens STOP_WAIT_MS warten."""
        if self._quitting:
            return
        self._quitting = True
        if self.osk is not None:
            self.osk.close()
            self.osk = None
        if self.pty is None:
            self.app.quit()
            return
        self.pty.signal(signal.SIGHUP)
        GLib.timeout_add(STOP_WAIT_MS, lambda: self.app.quit() or False)


def main(argv: list[str] | None = None) -> int:
    log.setup()                      # stderr: der AppManager schreibt es nach apps/app_claude.log
    app = Gtk.Application(application_id="org.couch.Terminal", flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder: dict = {}

    def activate(gtk_app) -> None:
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        holder["t"] = Terminal(gtk_app)

    def stop() -> bool:
        t = holder.get("t")
        if t is not None:
            t.quit()
        else:
            app.quit()
        return False

    app.connect("activate", activate)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop)
    return app.run([sys.argv[0]] if argv is None else argv)
