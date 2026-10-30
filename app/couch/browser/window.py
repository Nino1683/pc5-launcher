"""GTK-4-Fenster des Browsers mit WebKit (E59). Start über app/bin/couch-browser.

Umgebung (vom Starter, launch/apps.py):
    COUCH_BROWSER_HOME        Startseite (sonst start.html daneben)
    COUCH_BROWSER_SEARCH      Suchadresse mit %s (sonst address.DEFAULT_SEARCH)
    COUCH_BROWSER_DOWNLOADS   Liste der Downloads, die das Home führt (sonst <state>/downloads.json)
    COUCH_BROWSER_REQUESTS    Ordner für Download-Aufträge an das Home (sonst <state>/download-requests)
    COUCH_BROWSER_DOWNLOAD_DIR  Ziel der Downloads (sonst der Download-Ordner, paths.download_dir)
    COUCH_BROWSER_LOCAL_DOWNLOADS  Liste der Downloads, die WebKit selbst lädt (sonst browser-downloads.json
                              neben der Liste des Home)
    COUCH_BROWSER_SIZE        "BxH" statt der Größe des Bildschirms (Tests)
    COUCH_BROWSER_OPEN        Adresse, die statt der Startseite geladen wird (Store: Kaufen, Anmelden)
    COUCH_BROWSER_CLOSE_AFTER "steam-login" | "gog-login" | "epic-login" | "battlenet-login" | "rockstar-login" |
                              "ubisoft-login" | "app-login" | "itch-login" | "itch-app": nach der Anmeldung
                              schließt sich der Browser selbst (Store, E69); kommt auch mit dem Auftrag ("close").
                              "itch-app" (E100): Einrichtung der itch.io-App; der Browser liest die App-Kennung
                              von ihrer Seite unter /user/settings/oauth-apps/ und schließt sich dann
    COUCH_BROWSER_OPEN_FILE   Auftrag {url, at, close} des Home an den laufenden Browser (sonst
                              <state>/browser-open.json, paths.browser_open_file); alle 2 s gelesen
    COUCH_OSK_SOCK            Eingang für Text der Bildschirmtastatur (sonst <run>/osk-browser.sock)
"""
from __future__ import annotations

import itertools
import json
import os
import re
import signal
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit  # noqa: E402

from .. import i18n, log, oskdirect, paths, system  # noqa: E402
from . import adblock, address, bookmarks, downloads, popups  # noqa: E402
from .prefs import Prefs, ZOOM_MAX, ZOOM_MIN, render_start  # noqa: E402
from ..store import epic_account, gog_account, itch_account, steam_account  # noqa: E402

GOG_DONE = ("<!doctype html><meta charset=utf-8><body style='background:#0a0b10;color:#fff;font:32px sans-serif;"
            "display:flex;align-items:center;justify-content:center;height:90vh;text-align:center'>"
            "<div><p>Du bist bei GOG angemeldet.</p><p style='color:#aaa'>Drücke die PS-Taste und geh zurück in den Store.</p></div>")
EPIC_DONE = GOG_DONE.replace("bei GOG", "bei Epic")
# E100: itch.io (Anmeldung, abgelehnte Anmeldung, eingerichtete App)
ITCH_DONE = GOG_DONE.replace("bei GOG", "bei itch.io")
ITCH_DENIED = GOG_DONE.replace("Du bist bei GOG angemeldet.", "Anmeldung bei itch.io abgebrochen.")
ITCH_APP_DONE = GOG_DONE.replace("Du bist bei GOG angemeldet.", "Die itch.io-App ist eingerichtet.").replace(
    "Drücke die PS-Taste und geh zurück in den Store.",
    "Drücke die PS-Taste, geh zurück in den Store und wähle dort noch einmal „Anmelden“.")
# Text der App-Seite: sichtbarer Text plus sichtbare Felder (die Kennung kann in einem schreibgeschützten
# Feld stehen, innerText enthält keine Feldwerte). Nie Kennwort- oder versteckte Felder.
ITCH_APP_JS = ("const parts = [document.body ? document.body.innerText : ''];"
               "for (const el of document.querySelectorAll('input, textarea')) {"
               "  const type = (el.getAttribute('type') || '').toLowerCase();"
               "  if (type !== 'hidden' && type !== 'password' && el.offsetParent !== null) parts.push(el.value || '');"
               "}"
               "return parts.join('\\n');")

_log = log.get("browser")
# Sprache der Bedienelemente (2026-10-25): COUCH_LANG aus dem Start (launch/apps.py, i18n.language)
LANG = os.environ.get("COUCH_LANG", "de")


def _t(text: str) -> str:
    return i18n.t(text, LANG)


ZOOM_STEP = 0.1
MEMORY_LIMIT_MB = 2048             # je Seitenprozess; darüber räumt WebKit seine Zwischenspeicher
# E138: kein eigener Seitenprozess je Website. Mit dem Wechsel behielt WebKit jede besuchte Seite als
# eigenen Prozess (300–600 MB) im Vorrat: nach 8 Seiten 13 Prozesse und 2,7 GB statt 3 und 0,7 GB,
# bei gleichen Ladezeiten (gemessen 2026-10-28, 4K)
FEATURES_OFF = ("ProcessSwapOnCrossSiteNavigation",)
TOAST_MS = 4500
CRASH_LIMIT = 3                    # so oft je Minute lädt der Browser eine abgestürzte Seite neu
POPUP_WAIT_MS = 10000              # so lange darf ein Fenster der Seite leer bleiben (E89)
SINK_MS = 5000                     # so lange lebt das Schein-Fenster eines gesperrten Pop-ups
BLOCKED_OFFER_S = 15               # so lange bietet die Leiste ein gesperrtes Pop-up an
# Rückweg einer Anmeldung an ein Programm auf diesem Rechner (OAuth mit Loopback, E94)
# (Battle.net: http://127.0.0.1:22885/login-callback oder https://localbattle.net:22885/…, das auf 127.0.0.1 zeigt)
# Launcher unter Wine nehmen den Rückweg über einen eigenen HTTPS-Server auf diesem Rechner, mit einem
# Zertifikat, das ihr Installer nur im Zertifikatsspeicher von Wine einträgt (Battle.net: selbst
# ausgestellt, „Blizzard Battle.net Local Cert“, 2026-10-22). Dem vertraut der Browser genau für diesen
# Namen, wenn der Name auf diesen Rechner zeigt und das Zertifikat so heißt.
LOCAL_CERTS = {"localbattle.net": "CN=Blizzard Battle.net Local Cert"}
# Abschlussseiten einer Launcher-Anmeldung, die den Rückruf selbst im Hintergrund schicken (Battle.net:
# „Sie können jetzt zu Ihrem Spiel oder der Battle.net-App zurückkehren“, 2026-10-22)
_APP_LOGIN_END = re.compile(r"^https://([a-z]{2}\.)?account\.battle\.net/login/flow/end")
APP_LOGIN_END_MS = 2500             # so lange bleibt die Abschlussseite stehen (ihr Rückruf läuft noch)
_LOOPBACK = re.compile(r"^(http://(127\.0\.0\.1|localhost|\[::1\])|https?://localbattle\.net)(:\d+)?/")
ITCH_APP_DONE_MS = 1500             # E100: so lange steht „App eingerichtet“, bevor der Browser zugeht

CSS = """
* { font-family: "Noto Sans", sans-serif; }
window.couch-browser { background: #0b0c10; }
.bar { background: #15171d; padding: 10px 18px; }
.bar button { min-width: 56px; min-height: 56px; padding: 0; border-radius: 28px; background: #23262e;
              color: #e9ecf2; border: none; box-shadow: none; }
.bar button:hover { background: #3a4150; }
.bar button:disabled { color: #50545e; background: #1b1d23; }
.bar button image { -gtk-icon-size: 28px; }
.bar entry { min-height: 56px; font-size: 22px; border-radius: 28px; padding: 0 24px; background: #23262e;
             color: #ffffff; border: 2px solid transparent; box-shadow: none; }
.bar entry:focus-within { border-color: #3d8bfd; background: #1c2333; }
.zoom { font-size: 19px; color: #a9afbd; min-width: 76px; }
.dl { font-size: 20px; color: #8fc3ff; padding: 0 8px; }
progressbar.loading trough { min-height: 4px; background: transparent; border: none; }
progressbar.loading progress { min-height: 4px; background: #3d8bfd; border: none; }
.toast { background: rgba(21, 23, 29, 0.94); color: #ffffff; font-size: 22px; padding: 16px 28px;
         border-radius: 18px; margin-bottom: 48px; }
popover.menu > contents { background: #1b1e26; border-radius: 20px; padding: 14px; }
.menu button { min-height: 58px; font-size: 22px; padding: 0 22px; border-radius: 14px; background: #262a34;
               color: #eef1f6; border: none; box-shadow: none; }
.menu button:hover { background: #3a4150; }
.menu button.danger { color: #ffb4b4; }
.menu .favrow label { font-size: 21px; color: #eef1f6; }
.menu .favrow button { min-width: 58px; padding: 0; }
.menu .title { font-size: 19px; color: #9aa1b0; margin: 4px 6px 8px 6px; }
.bar button.wide { padding: 0 22px; font-size: 19px; background: #3a2f1c; color: #ffd38a; }
.popup { background: #15171d; border: 3px solid #3d8bfd; border-radius: 18px; }
.popup-head { padding: 8px 10px 8px 24px; }
.popup-head label { font-size: 20px; color: #c9cfdb; }
.popup-head button { min-height: 52px; padding: 0 24px; font-size: 20px; border-radius: 26px; background: #23262e;
                     color: #ffffff; border: none; box-shadow: none; }
.popup-head button:hover { background: #3a4150; }
"""


def _features_off(settings, names) -> None:
    """Schaltet WebKit-Funktionen ab, die diese WebKit-Fassung kennt; unbekannte Namen zählen nicht."""
    features = WebKit.Settings.get_all_features()
    for i in range(features.get_length()):
        feature = features.get(i)
        if feature.get_identifier() in names:
            settings.set_feature_enabled(feature, False)


def _env_path(name: str, default: str) -> str:
    value = os.environ.get(name, "")
    return value if value else default


def download_dir() -> str:
    # derselbe Ort wie beim Home (E99): im Prüfmodus im Käfig, nicht im echten ~/Downloads
    return os.environ.get("COUCH_BROWSER_DOWNLOAD_DIR") or paths.download_dir()


def _is_local(host: str) -> bool:
    """Zeigt der Name nur auf diesen Rechner (127.0.0.0/8, ::1)? Blockiert (Namensauflösung): nie im
    GTK-Hauptthread, sondern über Browser._in_thread."""
    import ipaddress
    import socket
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in addrs)


def _local_server_cert(host: str, port: int) -> tuple[bool, str]:
    """(zeigt host nur auf diesen Rechner, Zertifikat des Servers dort als PEM oder ""): ohne Prüfung geholt,
    geprüft wird danach der Name. Blockiert (Namensauflösung, TLS bis zu 3 s): nur im eigenen Faden."""
    import ssl
    if not _is_local(host):
        return False, ""
    try:
        return True, ssl.get_server_certificate(("127.0.0.1", port), timeout=3)
    except (OSError, ValueError) as e:
        _log.info("Zertifikat von %s:%d nicht zu holen: %s", host, port, e)
        return True, ""


def _cert_from_pem(pem: str):
    return Gio.TlsCertificate.new_from_pem(pem, -1)

def is_itch_app_page(uri: str) -> bool:
    """Seite einer einzelnen itch.io-App (unter /user/settings/oauth-apps/…), E100. Die Liste selbst
    nicht: Dort könnte die Kennung einer anderen App stehen, bevor der Nutzer die neue angelegt hat."""
    from urllib.parse import urlsplit
    if not itch_account.is_apps_page(uri):
        return False
    try:
        path = urlsplit(uri).path.rstrip("/")
    except ValueError:
        return False
    return path != "/user/settings/oauth-apps" and not path.endswith("/new")


class Browser:
    def __init__(self, app: Gtk.Application):
        self.app = app
        data = os.path.join(paths.data_dir(), "browser")
        cache = os.path.join(paths.cache_dir(), "browser")
        for d in (data, cache):
            os.makedirs(d, exist_ok=True)
        # Vorgaben aus den Einstellungen der Konsole gelten nur, bis im Menü etwas gewählt ist
        self.close_after = os.environ.get("COUCH_BROWSER_CLOSE_AFTER", "")   # Store-Anmeldung (E69)
        self._login_page = ""            # zuletzt protokollierte Seite einer Store-Anmeldung (store_login_check)
        self.prefs = Prefs(os.path.join(data, "prefs.json"), os.environ.get("COUCH_BROWSER_HOME", ""),
                           os.environ.get("COUCH_BROWSER_SEARCH", ""))
        self.start_file = os.path.join(data, "start.html")
        self.bookmarks_file = os.path.join(data, "bookmarks.html")
        self.bookmark_count = self.write_bookmarks()
        self.write_start()
        # Eigener Ordner neben dem Zwischenspeicher (den verwaltet WebKits Netzwerkprozess). Fürs
        # Übersetzen braucht WebKit ein gültiges TMPDIR, sonst „Internal I/O error“ (im Probelauf gesehen)
        self.filter_store = WebKit.UserContentFilterStore.new(os.path.join(paths.cache_dir(), "browser-filters"))
        self.filter = None
        self._filter_source = None
        self.dl_dir = download_dir()
        self.dl_list = _env_path("COUCH_BROWSER_DOWNLOADS", paths.downloads_file())
        self.requests = _env_path("COUCH_BROWSER_REQUESTS", paths.download_requests_dir())
        self.handed: dict = {}                     # Downloads, die das Home übernimmt
        self.local: dict = {}                      # Downloads, die WebKit selbst lädt (blob:, POST …) -> Eintrag
        # E99: Sie stehen auch im Control Center (das Home liest die Datei); vorher nur ein Toast
        self.local_log = downloads.LocalLog(_env_path(
            "COUCH_BROWSER_LOCAL_DOWNLOADS", os.path.join(os.path.dirname(self.dl_list), downloads.LOCAL_FILE)))
        self.lang = os.environ.get("LANG", "").split(".")[0].replace("_", "-")
        self.crashes: list[float] = []
        self._toast_timer = 0

        self.session = WebKit.NetworkSession.new(data, cache)
        self.session.get_cookie_manager().set_persistent_storage(
            os.path.join(data, "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        self.session.connect("download-started", self.on_download)
        # Speicher (E61): Wird ein Seitenprozess größer als MEMORY_LIMIT_MB, räumt WebKit seine
        # Zwischenspeicher; vorbereitete Seiten für „Zurück“ (Page Cache) hält es keine vor
        pressure = WebKit.MemoryPressureSettings.new()
        pressure.set_memory_limit(MEMORY_LIMIT_MB)
        self.context = WebKit.WebContext(memory_pressure_settings=pressure)
        self.view = WebKit.WebView(web_context=self.context, network_session=self.session)
        st = self.view.get_settings()
        st.set_enable_page_cache(False)
        st.set_enable_developer_extras(False)
        st.set_javascript_can_open_windows_automatically(False)
        st.set_enable_smooth_scrolling(True)
        st.set_media_playback_requires_user_gesture(False)
        st.set_enable_back_forward_navigation_gestures(False)
        _features_off(st, FEATURES_OFF)
        self.view.set_zoom_level(self.prefs.zoom)
        self.apply_adblock()
        self.view.set_vexpand(True)
        self.view.set_hexpand(True)
        for name, fn in (("notify::uri", self.on_uri), ("notify::estimated-progress", self.on_progress),
                         ("load-changed", self.on_load), ("load-failed", self.on_load_failed),
                         ("load-failed-with-tls-errors", self.on_tls_error),
                         ("decide-policy", self.on_policy),
                         ("create", self.on_create), ("permission-request", self.on_permission),
                         ("enter-fullscreen", self.on_fullscreen), ("leave-fullscreen", self.on_unfullscreen),
                         ("web-process-terminated", self.on_crash), ("close", lambda *_: None)):
            self.view.connect(name, fn)

        self.win = Gtk.ApplicationWindow(application=app, title="Browser")
        self.win.add_css_class("couch-browser")
        self.win.set_decorated(False)
        self._size_window()
        self.bar = self._bar()
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("loading")
        self.progress.set_visible(False)
        self.toast = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END, wrap=True, max_width_chars=70)
        self.toast.add_css_class("toast")
        self.toast.set_visible(False)
        self.toast.set_can_target(False)
        stage = Gtk.Overlay()
        self.stage = stage
        self.popup = None               # Fenster, das eine Seite mit window.open öffnet (Anmeldungen)
        self.popup_frame = None         # sein Rahmen über der Seite, erst wenn es gezeigt wird (E89)
        self.popup_uri = ""
        self.popup_allowed = False
        self.popup_ready = False
        self._popup_timer = 0
        self.blocked_uri = ""
        self._blocked_timer = 0
        self._blocked_at = 0.0
        self._sinks: list = []
        stage.set_child(self.view)
        stage.add_overlay(self.toast)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.bar)
        box.append(self.progress)
        box.append(stage)
        self.win.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.win.add_controller(keys)
        clicks = Gtk.GestureClick(button=0)          # Maustasten 8/9 (L1/R1 in padmouse)
        clicks.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        clicks.connect("pressed", self.on_click)
        self.win.add_controller(clicks)
        self.win.connect("close-request", lambda *_: self.quit() or True)

        self.update_buttons()
        self.update_downloads()
        self.osk = None
        try:                                    # Text der Bildschirmtastatur direkt (oskdirect)
            self.osk = oskdirect.Server(_env_path("COUCH_OSK_SOCK", paths.osk_socket("browser")), self.osk_text)
        except OSError as e:
            _log.warning("Eingang der Bildschirmtastatur: %s", e)
        if self.close_after == "app-login":
            self.trust_local_server(os.environ.get("COUCH_BROWSER_OPEN") or "")
        self.view.load_uri(os.environ.get("COUCH_BROWSER_OPEN") or self.home_uri)
        self.win.present()
        self.win.fullscreen()
        self.view.grab_focus()

    # ------------------------------------------------------------ Aufbau
    def _size_window(self) -> None:
        size = os.environ.get("COUCH_BROWSER_SIZE", "")
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

    def _button(self, icon: str, tip: str, fn) -> Gtk.Button:
        b = Gtk.Button.new_from_icon_name(icon)
        b.set_tooltip_text(_t(tip))
        b.set_focus_on_click(False)
        b.connect("clicked", lambda *_: fn())
        return b

    def _bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("bar")
        self.back = self._button("go-previous-symbolic", "Zurück (L1)", self.go_back)
        self.fwd = self._button("go-next-symbolic", "Vor (R1)", self.go_forward)
        self.reload = self._button("view-refresh-symbolic", "Neu laden", self.reload_or_stop)
        self.homeb = self._button("go-home-symbolic", "Startseite", self.go_home)
        self.marks = self._button("user-bookmarks-symbolic", "Lesezeichen", self.open_bookmarks)
        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Suchen oder Adresse eingeben")
        self.entry.set_input_purpose(Gtk.InputPurpose.URL)
        self.entry.connect("activate", self.on_go)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: GLib.idle_add(self._select_all))
        self.entry.add_controller(focus)
        self.zoom_out = self._button("zoom-out-symbolic", "Kleiner", lambda: self.zoom(-ZOOM_STEP))
        self.zoom_label = Gtk.Label()
        self.zoom_label.add_css_class("zoom")
        self.zoom_in = self._button("zoom-in-symbolic", "Größer", lambda: self.zoom(+ZOOM_STEP))
        self.dl_label = Gtk.Label()
        self.dl_label.add_css_class("dl")
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menü")
        self.menu_button.set_focus_on_click(False)
        self.menu = self._menu()
        self.menu_button.set_popover(self.menu)
        # Gesperrtes Pop-up doch öffnen (E89): erscheint nur kurz nach dem Sperren
        self.blocked_btn = Gtk.Button(label=_t("Pop-up öffnen"))
        self.blocked_btn.add_css_class("wide")
        self.blocked_btn.set_focus_on_click(False)
        self.blocked_btn.set_visible(False)
        self.blocked_btn.connect("clicked", lambda *_: self.open_blocked())
        for w in (self.back, self.fwd, self.reload, self.homeb, self.marks, self.entry, self.blocked_btn, self.dl_label,
                  self.zoom_out, self.zoom_label, self.zoom_in, self.menu_button):
            bar.append(w)
        self._zoom_text()
        return bar

    def _select_all(self) -> bool:
        self.entry.select_region(0, -1)
        return False

    # ------------------------------------------------------------ Optionen (E61)
    @property
    def home_uri(self) -> str:
        return self.prefs.home or GLib.filename_to_uri(self.start_file, None)

    @property
    def search(self) -> str:
        return self.prefs.search_pattern()

    def write_start(self) -> None:
        marks = (GLib.filename_to_uri(self.bookmarks_file, None), self.bookmark_count)
        try:
            system.write_atomic(self.start_file, render_start(self.prefs, marks, _t), 0o600)   # nennt die Favoriten
        except OSError as e:
            _log.warning("Startseite nicht schreibbar: %s", e)

    def write_bookmarks(self) -> int:
        """Lesezeichen aus Chrome (Google-Konto) als Seite (E89); gibt ihre Anzahl zurück."""
        book = bookmarks.load(paths.home())
        try:
            system.write_atomic(self.bookmarks_file, bookmarks.render(book), 0o600)   # Lesezeichen des Kontos
        except OSError as e:
            _log.warning("Lesezeichen nicht schreibbar: %s", e)
        return book["count"]

    def open_bookmarks(self) -> None:
        count = self.write_bookmarks()             # frisch: Chrome kann inzwischen abgeglichen haben
        if count != self.bookmark_count:
            self.bookmark_count = count
            self.write_start()
        self.view.load_uri(GLib.filename_to_uri(self.bookmarks_file, None))
        self.view.grab_focus()

    def changed(self) -> None:
        """Optionen gespeichert, Startseite neu; zeigt der Browser sie gerade, neu laden."""
        self.prefs.save()
        self.write_start()
        if (self.view.get_uri() or "") == GLib.filename_to_uri(self.start_file, None):
            self.view.reload()

    def apply_adblock(self) -> None:
        ucm = self.view.get_user_content_manager()
        if not self.prefs.adblock:
            ucm.remove_all_filters()
            return
        if self.filter is not None:
            ucm.add_filter(self.filter)
            return

        def saved(store, result) -> None:
            self._filter_source = None
            try:
                self.filter = store.save_finish(result)
            except GLib.Error as e:
                _log.error("Werbeblocker nicht übersetzbar: %s", e.message)
                return
            if self.prefs.adblock:
                ucm.add_filter(self.filter)

        def loaded(store, result) -> None:
            try:
                self.filter = store.load_finish(result)
            except GLib.Error:
                # Die Regeln festhalten, bis WebKit fertig ist: Es liest sie im Hintergrund, und
                # ein freigegebener Puffer endete im Browser als „Internal I/O error“
                self._filter_source = GLib.Bytes.new(adblock.rules_json().encode())
                store.save(adblock.IDENTIFIER, self._filter_source, None, saved)
                return
            if self.prefs.adblock:
                ucm.add_filter(self.filter)
        self.filter_store.load(adblock.IDENTIFIER, None, loaded)

    # ------------------------------------------------------------ Menü
    def _menu(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("menu")
        self.menu_stack = Gtk.Stack()
        self.menu_stack.set_vhomogeneous(False)      # so hoch wie die gezeigte Seite, nicht wie die längste
        self.menu_stack.set_hhomogeneous(False)
        pop.set_child(self.menu_stack)
        pop.connect("show", lambda *_: self._fill_menu())
        return pop

    def _menu_button(self, text: str, fn, css: str = "") -> Gtk.Button:
        b = Gtk.Button(label=_t(text))
        b.set_focus_on_click(False)
        if css:
            b.add_css_class(css)
        b.get_child().set_xalign(0)
        b.connect("clicked", lambda *_: fn())
        return b

    def _fill_menu(self, page: str = "main") -> None:
        for child in list(self.menu_stack):
            self.menu_stack.remove(child)
        uri = self.view.get_uri() or ""
        web = uri.startswith(("http://", "https://"))
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        if web and self.prefs.is_favorite(uri):
            main.append(self._menu_button("★  Aus den Favoriten entfernen", self.menu_unfavorite))
        elif web:
            main.append(self._menu_button("☆  Zu den Favoriten", self.menu_favorite))
        main.append(self._menu_button("Favoriten bearbeiten …", lambda: self._fill_menu("favs")))
        if web and self.prefs.home != uri:
            main.append(self._menu_button("Diese Seite als Startseite", self.menu_set_home))
        if self.prefs.home:
            main.append(self._menu_button("Startseite: Favoriten-Kacheln", self.menu_reset_home))
        main.append(self._menu_button("Suchen mit: %s" % self.prefs.search_name(), self.menu_next_search))
        main.append(self._menu_button("Werbeblocker: %s" % ("an" if self.prefs.adblock else "aus"), self.menu_adblock))
        main.append(self._menu_button("Browserdaten löschen …", lambda: self._fill_menu("clear"), "danger"))
        self.menu_stack.add_named(main, "main")

        favs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        title = Gtk.Label(label=_t("Favoriten (Reihenfolge auf der Startseite)"), xalign=0)
        title.add_css_class("title")
        favs.append(title)
        if not self.prefs.favorites:
            favs.append(Gtk.Label(label=_t("Noch keine Favoriten."), xalign=0))
        for fav in self.prefs.favorites:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("favrow")
            label = Gtk.Label(label=fav["title"], xalign=0, hexpand=True, ellipsize=3, max_width_chars=28)
            row.append(label)
            row.append(self._button("go-up-symbolic", "Nach vorn", lambda u=fav["url"]: self.menu_move(u)))
            row.append(self._button("user-trash-symbolic", "Entfernen", lambda u=fav["url"]: self.menu_remove(u)))
            favs.append(row)
        favs.append(self._menu_button("‹  Zurück", lambda: self._fill_menu("main")))
        self.menu_stack.add_named(favs, "favs")

        clear = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        note = Gtk.Label(label=_t("Verlauf, Cookies, Anmeldungen und Zwischenspeicher löschen?"), xalign=0, wrap=True,
                         max_width_chars=34)
        note.add_css_class("title")
        clear.append(note)
        clear.append(self._menu_button("Löschen", self.menu_clear, "danger"))
        clear.append(self._menu_button("Abbrechen", lambda: self._fill_menu("main")))
        self.menu_stack.add_named(clear, "clear")
        self.menu_stack.set_visible_child_name(page)

    def menu_favorite(self) -> None:
        uri = self.view.get_uri() or ""
        if self.prefs.add_favorite(uri, self.view.get_title() or ""):
            self.changed()
            self.show_toast("Zu den Favoriten hinzugefügt.")
        self.menu.popdown()

    def menu_unfavorite(self) -> None:
        if self.prefs.remove_favorite(self.view.get_uri() or ""):
            self.changed()
            self.show_toast("Aus den Favoriten entfernt.")
        self.menu.popdown()

    def menu_move(self, url: str) -> None:
        if self.prefs.move_favorite(url, -1):
            self.changed()
        self._fill_menu("favs")

    def menu_remove(self, url: str) -> None:
        if self.prefs.remove_favorite(url):
            self.changed()
        self._fill_menu("favs")

    def menu_set_home(self) -> None:
        if self.prefs.set_home(self.view.get_uri() or ""):
            self.changed()
            self.show_toast("Startseite festgelegt.")
        self.menu.popdown()

    def menu_reset_home(self) -> None:
        self.prefs.reset_home()
        self.changed()
        self.show_toast("Startseite: deine Favoriten.")
        self.menu.popdown()

    def menu_next_search(self) -> None:
        self.prefs.next_search()
        self.changed()
        self._fill_menu("main")

    def menu_adblock(self) -> None:
        self.prefs.adblock = not self.prefs.adblock
        self.prefs.save()
        self.apply_adblock()
        self.view.reload()
        self._fill_menu("main")

    def menu_clear(self) -> None:
        manager = self.session.get_website_data_manager()

        def done(mgr, result) -> None:
            try:
                mgr.clear_finish(result)
                self.show_toast("Browserdaten gelöscht.")
            except GLib.Error as e:
                self.show_toast("Löschen hat nicht geklappt: %s" % e.message)
        manager.clear(WebKit.WebsiteDataTypes.ALL, 0, None, done)
        self.menu.popdown()

    # ------------------------------------------------------------ Bedienung
    def go_back(self) -> None:
        if self.popup_frame is not None:           # zurück (L1) schließt erst das Fenster der Seite
            self.close_popup()
        elif self.view.can_go_back():
            self.view.go_back()

    def go_forward(self) -> None:
        if self.view.can_go_forward():
            self.view.go_forward()

    def go_home(self) -> None:
        self.view.load_uri(self.home_uri)
        self.view.grab_focus()

    def reload_or_stop(self) -> None:
        if self.view.is_loading():
            self.view.stop_loading()
        else:
            self.view.reload()

    def on_go(self, *_a) -> None:
        uri = address.to_uri(self.entry.get_text(), self.search)
        if uri:
            self.view.load_uri(uri)
            self.view.grab_focus()

    def zoom(self, step: float) -> None:
        z = round(min(ZOOM_MAX, max(ZOOM_MIN, self.view.get_zoom_level() + step)), 2)
        self.view.set_zoom_level(z)
        self.prefs.zoom = z
        self.prefs.save()
        self._zoom_text()

    def _zoom_text(self) -> None:
        self.zoom_label.set_text("%d %%" % round(self.view.get_zoom_level() * 100))

    def on_key(self, _ctl, keyval, _code, state) -> bool:
        if keyval == Gdk.KEY_Escape and self.popup_frame is not None:
            # Kreis schließt das gezeigte Anmeldefenster, nicht die Seite (wie go_back; ein noch nicht
            # gezeigtes Fenster, E89, bleibt und schluckt die Taste nicht)
            self.close_popup()
            return True
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if keyval == Gdk.KEY_Escape and self.entry.has_focus():
            self.on_uri()
            self.view.grab_focus()
            return True
        if keyval == Gdk.KEY_F5 or (ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R)):
            self.view.reload()
            return True
        if ctrl and keyval in (Gdk.KEY_l, Gdk.KEY_L):
            self.entry.grab_focus()
            return True
        if alt and keyval == Gdk.KEY_Left:
            self.go_back()
            return True
        if alt and keyval == Gdk.KEY_Right:
            self.go_forward()
            return True
        if ctrl and keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.zoom(+ZOOM_STEP)
            return True
        if ctrl and keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.zoom(-ZOOM_STEP)
            return True
        return False

    def on_click(self, gesture, _n, _x, _y) -> None:
        button = gesture.get_current_button()
        if button == 8:
            self.go_back()
        elif button == 9:
            self.go_forward()
        else:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    # ------------------------------------------------------------ Seite
    def on_uri(self, *_a) -> None:
        if not self.entry.has_focus():
            self.entry.set_text(address.shown(self.view.get_uri() or ""))
        self.update_buttons()
        # Shops, die nach der Anmeldung die Adresse nur per Skript umschreiben (history.pushState, Rockstar), laden
        # nicht neu: on_load sähe die Schlussadresse nie (2026-10-30: Browser blieb nach der Anmeldung offen)
        if self.close_after and not self.view.is_loading():
            self.store_login_check(self.view.get_uri() or "")

    def on_progress(self, *_a) -> None:
        self.progress.set_fraction(self.view.get_estimated_progress())

    def on_load(self, _view, event) -> None:
        loading = event != WebKit.LoadEvent.FINISHED
        uri = self.view.get_uri() or ""
        if self.close_after == "app-login" and event in (WebKit.LoadEvent.COMMITTED, WebKit.LoadEvent.FINISHED):
            # Anmeldung für einen Launcher: Weg ohne Anfrageteil ins Protokoll (darin stehen Einmal-Codes)
            _log.info("Anmeldung für einen Launcher: %s %s", "fertig" if not loading else "lädt", uri.split("?")[0][:200])
        if event in (WebKit.LoadEvent.STARTED, WebKit.LoadEvent.REDIRECTED) and gog_account.is_login_redirect(uri):
            # Sicherheitsnetz zu on_policy (so machen es auch andere GOG-Programme)
            self.take_gog_code(uri)
            return
        if event in (WebKit.LoadEvent.STARTED, WebKit.LoadEvent.REDIRECTED) and itch_account.is_login_redirect(uri):
            self.take_itch_code(uri)             # E100: Sicherheitsnetz zu on_policy, wie GOG
            return
        if event == WebKit.LoadEvent.FINISHED and epic_account.is_login_redirect(uri):
            self.take_epic_code()
        if event == WebKit.LoadEvent.FINISHED and self.close_after == "itch-app" and is_itch_app_page(uri):
            self.take_itch_client()
        if not loading and self.store_login_check(self.view.get_uri() or ""):
            return
        if not loading and self.close_after == "app-login" and _APP_LOGIN_END.match(uri):
            self.login_done(APP_LOGIN_END_MS)
            return
        if not loading and self.close_after == "app-login" and _LOOPBACK.match(uri):
            # Anmeldung für einen Launcher unter Wine (E94, „Mit Google anmelden“ in Battle.net): Die
            # Seite gibt das Ergebnis an den Launcher auf diesem Rechner zurück, dann zurück zu ihm
            self.login_done()
            return
        self.progress.set_visible(loading)
        self.reload.set_icon_name("process-stop-symbolic" if loading else "view-refresh-symbolic")
        self.reload.set_tooltip_text(_t("Laden abbrechen" if loading else "Neu laden"))
        self.update_buttons()

    def trust_local_server(self, url: str) -> None:
        """Vor der Anmeldung für einen Launcher: Nennt die Adresse einen Rückweg über HTTPS auf diesem
        Rechner (Battle.net: returnHttps=https://localbattle.net:22885/…), holt der Browser das Zertifikat
        des Launchers von dort und vertraut ihm für diesen einen Namen."""
        from urllib.parse import parse_qs, urlsplit
        try:
            query = parse_qs(urlsplit(url).query)
        except ValueError:
            return
        for value in [v for values in query.values() for v in values]:
            try:
                target = urlsplit(value)
                host, port = (target.hostname or "").lower(), target.port or 443
            except ValueError:
                continue
            if target.scheme == "https" and host in LOCAL_CERTS:
                # Namensauflösung und TLS im eigenen Faden (vorher im GTK-Hauptthread, auch aus dem 2-s-Zeitgeber
                # _poll_downloads): Die Seite lädt derweil schon. Der Rückweg kommt erst nach der Anmeldung
                self._in_thread(lambda h=host, p=port: _local_server_cert(h, p),
                                lambda found, h=host: self._trust_found(h, *found))

    def _trust_found(self, host: str, local: bool, pem: str) -> None:
        """GTK-Hauptthread: Ergebnis von _local_server_cert für trust_local_server."""
        if not pem:
            return
        try:
            cert = _cert_from_pem(pem)
        except GLib.Error as e:
            _log.info("Zertifikat von %s nicht lesbar: %s", host, e.message)
            return
        self.allow_local_cert(host, cert, local)

    @staticmethod
    def _launcher_cert(host: str, cert) -> bool:
        """Trägt cert den Namen des Launcher-Zertifikats für host (LOCAL_CERTS)?"""
        return host in LOCAL_CERTS and LOCAL_CERTS[host] in (cert.get_subject_name() or "")

    def allow_local_cert(self, host: str, cert, local: bool) -> bool:
        """GTK-Hauptthread. local: Der Name zeigt nur auf diesen Rechner (_is_local, im eigenen Faden geklärt)."""
        if not local or not self._launcher_cert(host, cert):
            return False
        self.session.allow_tls_certificate_for_host(cert, host)
        _log.info("Zertifikat des Launchers für %s anerkannt (%s)", host, LOCAL_CERTS[host])
        return True

    def on_tls_error(self, _view, uri, cert, _errors) -> bool:
        """Rückfall, falls das Zertifikat vorab nicht zu holen war: nur für die Launcher-Namen. Ob der Name auf
        diesen Rechner zeigt, klärt ein eigener Faden; bis dahin bleibt die Seite stehen. Danach lädt die Adresse
        noch einmal: mit dem anerkannten Zertifikat, sonst scheitert sie wie vorher (WebKits Fehlerseite)."""
        from urllib.parse import urlsplit
        host = (urlsplit(uri or "").hostname or "").lower()
        if self.close_after != "app-login" or not self._launcher_cert(host, cert):
            return False
        answers = self._thread_state()["answers"]
        if host in answers:                     # eben im Faden geklärt: kein Launcher auf diesem Rechner
            del answers[host]
            return False

        def resolved(local: bool) -> None:
            if not self.allow_local_cert(host, cert, local):
                answers[host] = False           # der nächste Fehler geht an WebKit
            self.view.load_uri(uri)
        self._in_thread(lambda: _is_local(host), resolved)
        return True

    def _thread_state(self) -> dict:
        state = self.__dict__.get("_threads")
        if state is None:
            state = self.__dict__["_threads"] = {"tokens": itertools.count(1), "waiting": {}, "answers": {}}
        return state

    def _in_thread(self, work, done) -> None:
        """work() in eigenem Faden (Namensauflösung, TLS: blockieren), danach done(ergebnis) im GTK-Hauptthread
        über GLib.idle_add. done (und was es festhält, etwa ein Gio.TlsCertificate) bleibt in einer Tabelle des
        Hauptthreads; der Faden kennt nur die Nummer und lässt so nie den letzten Verweis darauf los."""
        state = self._thread_state()
        token = next(state["tokens"])
        state["waiting"][token] = done

        def run() -> None:
            try:
                result, ok = work(), True
            except Exception:  # noqa: BLE001
                _log.exception("Prüfung im Hintergrund")
                result, ok = None, False
            GLib.idle_add(self._thread_done, token, result, ok)
        threading.Thread(target=run, name="couch-browser-lokal", daemon=True).start()

    def _thread_done(self, token: int, result, ok: bool) -> bool:
        done = self._thread_state()["waiting"].pop(token, None)
        if done is not None and ok:
            try:
                done(result)
            except Exception:  # noqa: BLE001
                _log.exception("Ergebnis einer Prüfung im Hintergrund")
        return False

    def on_load_failed(self, _view, _event, uri, error) -> bool:
        if itch_account.is_login_redirect(uri or ""):
            # E100: Kam die Rückleitung an on_policy vorbei, scheitert ihr Laden (dort hört niemand):
            # Code trotzdem an das Home
            self.take_itch_code(uri)
            return True
        if self.close_after == "app-login":
            _log.info("Anmeldung für einen Launcher: nicht geladen %s (%s)", (uri or "").split("?")[0][:200],
                      getattr(error, "message", error))
            if _LOOPBACK.match(uri or ""):
                self.login_done()           # Rückweg erreicht (der Launcher wartete nicht mehr): zurück zu ihm
        return False

    def update_buttons(self) -> None:
        self.back.set_sensitive(self.popup_frame is not None or self.view.can_go_back())
        self.fwd.set_sensitive(self.view.can_go_forward())

    def take_gog_code(self, uri: str) -> None:
        """Anmeldung bei GOG für den Store (E68): Code an das Home, nicht an die Seite."""
        code = gog_account.code_from_redirect(uri)
        if not code or getattr(self, "_gog_taken", "") == code:
            return
        if self.close_after != "gog-login":
            # Nur während einer Anmeldung aus dem Store (wie itch.io): Sonst könnte jede Seite diese Adresse mit
            # dem Code eines fremden Kontos aufrufen, und die Konsole wäre damit angemeldet. Die Navigation ist ignoriert.
            _log.info("GOG-Rückleitung ohne laufende Anmeldung: nicht übernommen")
            return
        self._gog_taken = code
        _log.info("GOG-Anmeldung: Umleitung mit Code erkannt")
        gog_account.store_code(os.path.join(paths.state_dir(), "gog-login.json"), code)
        self.view.load_html(_t(GOG_DONE), "about:blank")
        self.login_done()

    def take_epic_code(self) -> None:
        """Anmeldung bei Epic für den Store (E73): Die Umleitungsseite zeigt ein JSON mit dem Code;
        er geht an das Home (legendary auth), nicht an die Seite. Nur während einer Anmeldung aus dem Store
        (close_after "epic-login"), sonst schlösse jede Seite mit dieser Adresse den Browser."""
        if self.close_after != "epic-login":
            return

        def done(view, res) -> None:
            try:
                value = view.call_async_javascript_function_finish(res)
                text = value.to_string() if value is not None and value.is_string() else ""
            except GLib.Error as e:
                _log.warning("Epic-Anmeldung: Seite nicht lesbar: %s", e.message)
                return
            code = epic_account.code_from_page(text)
            if not code or getattr(self, "_epic_taken", "") == code:
                return
            self._epic_taken = code
            _log.info("Epic-Anmeldung: Code erkannt")
            epic_account.store_code(os.path.join(paths.state_dir(), "epic-login.json"), code)
            self.view.load_html(_t(EPIC_DONE), "about:blank")
            self.login_done()
        self.view.call_async_javascript_function("return document.body ? document.body.innerText : '';", -1,
                                                 None, None, None, None, done)

    def take_itch_code(self, uri: str) -> None:
        """Anmeldung bei itch.io für den Store (E100, OAuth mit PKCE): Code und state an das Home
        (<state>/itch-login.json, 0600), nicht an die Seite; das Home tauscht ihn. Abgelehnt
        (error=…): nichts ablegen, nur schließen. Nur bei close_after "itch-login" (sonst bleibt die
        Navigation ignoriert, der Browser offen). Code und state nie ins Protokoll."""
        found = itch_account.code_from_redirect(uri)
        # Dieselbe Rückleitung kommt oft mehrmals an (on_policy, on_load, load-failed nach dem Abbruch)
        if found is None or getattr(self, "_itch_taken", "") == uri:
            return
        if self.close_after != "itch-login":
            # Nur während einer Anmeldung aus dem Store: Sonst könnte jede Seite mit dieser Adresse den
            # Browser schließen oder einen wartenden Code überschreiben. Die Navigation ist ignoriert.
            _log.info("itch.io-Rückleitung ohne laufende Anmeldung: nicht übernommen")
            return
        code, state = found
        if not code:
            self._itch_taken = uri
            _log.info("itch.io-Anmeldung abgelehnt oder abgebrochen")
            self.view.load_html(_t(ITCH_DENIED), "about:blank")
            self.login_done()
            return
        _log.info("itch.io-Anmeldung: Umleitung mit Code erkannt")
        try:
            itch_account.store_code(paths.state_dir(), code, state)
        except OSError as e:
            _log.warning("itch.io-Anmeldung nicht gespeichert: %s", e)
            return
        self._itch_taken = uri
        self.view.load_html(_t(ITCH_DONE), "about:blank")
        self.login_done()

    def take_itch_client(self) -> None:
        """Einrichtung der itch.io-App (E100, close_after "itch-app"): Zeigt die Seite der App genau eine
        Kennung (32 Hex-Zeichen) und die Redirect-URL der Konsole, geht die Kennung an das Home
        (<state>/itch-client.json, 0600), dann schließt sich der Browser. Zwei oder keine Kennung oder
        eine fremde App: weiter warten (der Nutzer kann sie in den Einstellungen eintippen)."""
        def done(view, res) -> None:
            try:
                value = view.call_async_javascript_function_finish(res)
                text = value.to_string() if value is not None and value.is_string() else ""
            except GLib.Error as e:
                _log.warning("itch.io-App: Seite nicht lesbar: %s", e.message)
                return
            if self.close_after != "itch-app" or not is_itch_app_page(view.get_uri() or ""):
                return                      # inzwischen woanders: nichts übernehmen
            cid = itch_account.client_id_from_text(text)
            if not cid or getattr(self, "_itch_client_taken", "") == cid:
                return
            if itch_account.REDIRECT not in text:
                # Eine andere, schon vorhandene App (ohne unsere Redirect-URL): ihre Kennung taugt nicht
                # für die Anmeldung, weiter warten (E100)
                return
            try:
                stored = itch_account.store_client_id(paths.state_dir(), cid)
            except OSError as e:
                _log.warning("itch.io-App-Kennung nicht gespeichert: %s", e)
                return
            if not stored:
                return
            self._itch_client_taken = cid
            _log.info("itch.io-App-Kennung erkannt")
            self.view.load_html(_t(ITCH_APP_DONE), "about:blank")
            self.login_done(ITCH_APP_DONE_MS)
        self.view.call_async_javascript_function(ITCH_APP_JS, -1, None, None, None, None, done)

    def store_login_check(self, uri: str) -> bool:
        """Fertig geladene Seite bzw. neue Adresse während einer Store-Anmeldung: Ist sie fertig, schließt der Browser.
        Für Steam und die Launcher-Shops (Battle.net, Rockstar, Ubisoft); aus on_load und on_uri."""
        if self.close_after == "steam-login":
            # Angemeldet, auch falls Steam die Marke im redir nicht durchreicht: jede Shop-Seite außer der Anmeldung
            if uri.startswith("https://store.steampowered.com/") and "/login" not in uri:
                self.login_done()
                return True
            return False
        if self.close_after not in ("battlenet-login", "rockstar-login", "ubisoft-login"):
            return False
        # Weg ohne Anfrageteil ins Protokoll (darin stehen Einmal-Codes): so lässt sich am Gerät sehen, wo ein Shop
        # nach der Anmeldung landet, falls der Browser einmal nicht schließt
        page = uri.split("?")[0].split("#")[0][:200]
        if page and page != self._login_page:
            self._login_page = page
            _log.info("Anmeldung (%s): %s", self.close_after, page)
        return self.launcher_login_done(self.close_after.split("-")[0], uri)

    def launcher_login_done(self, store: str, uri: str) -> bool:
        """Anmeldung bei Battle.net, Rockstar oder Ubisoft für den Store (E94). Battle.net und Rockstar:
        Die Cookies liegen danach im Browser, das Home liest sie selbst. Ubisoft: Die Seite hält die
        Anmeldung im localStorage; sie geht als Datei (0600) an das Home, nicht ins Protokoll."""
        import importlib
        try:
            mod = importlib.import_module("couch.store.%s_account" % store)
        except ImportError:
            return False
        check = getattr(mod, "is_login_done", None)
        done = str(getattr(mod, "LOGIN_DONE", "") or "")
        if not (check(uri) if callable(check) else bool(done) and done in uri):
            return False
        if store != "ubisoft":
            self.login_done()
            return True

        def got(view, res) -> None:
            if not self.close_after:
                return                          # schon erkannt (on_load und on_uri fragen beide)
            try:
                value = view.call_async_javascript_function_finish(res)
                text = value.to_string() if value is not None and value.is_string() else ""
            except GLib.Error as e:
                _log.warning("Ubisoft-Anmeldung: Seite nicht lesbar: %s", e.message)
                return
            if not text or text in ("null", "{}"):
                return
            path = os.path.join(paths.state_dir(), "ubisoft-login.json")
            try:
                # eigene Zwischendatei mit 0600: eine liegengebliebene ältere bestimmt nie die Rechte (E99)
                system.write_atomic(path, text, 0o600)
            except OSError as e:
                _log.warning("Ubisoft-Anmeldung nicht gespeichert: %s", e)
                return
            _log.info("Ubisoft-Anmeldung erkannt")
            self.login_done()
        self.view.call_async_javascript_function("return " + str(mod.READ_JS) + ";", -1, None, None, None, None, got)
        return True

    def login_done(self, delay_ms: int = 300) -> None:
        """Anmeldung für den Store fertig: Browser zu, das Home zeigt wieder den Store (E69)."""
        _log.info("Anmeldung fertig (%s), Browser schließt", self.close_after or "Store")
        self.close_after = ""
        GLib.timeout_add(delay_ms, self.quit)

    def on_policy(self, _view, decision, kind) -> bool:
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if gog_account.is_login_redirect(uri):
                decision.ignore()
                self.take_gog_code(uri)
                return True
            if itch_account.is_login_redirect(uri):
                decision.ignore()               # E100: auf 127.0.0.1:34711 hört niemand, der Code geht an das Home
                self.take_itch_code(uri)
                return True
            if steam_account.LOGIN_DONE in uri and uri.startswith("https://store.steampowered.com/"):
                decision.ignore()
                self.login_done()
                return True
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri() or ""
            if action.get_navigation_type() != WebKit.NavigationType.LINK_CLICKED:
                # Von einer Seite geöffnetes Fenster (window.open, etwa „Mit Steam/Google anmelden“
                # bei GOG): Es braucht die Verbindung zu seiner Seite (window.opener), sonst
                # kommt die Anmeldung nie zurück. Also ein echtes Fenster über der Seite; ob es
                # gezeigt wird, entscheidet on_create (E89)
                decision.use()
                return True
            # Links mit target=_blank: im selben Fenster, außer sie führen zu Werbung
            decision.ignore()
            if self.prefs.adblock and adblock.is_ad(uri):
                self.popup_blocked(uri)
            elif uri:
                self.view.load_uri(uri)
            return True
        if kind == WebKit.PolicyDecisionType.RESPONSE and epic_account.is_login_redirect(
                decision.get_request().get_uri() or ""):
            decision.use()                  # JSON mit dem Code anzeigen, nicht laden (take_epic_code)
            return True
        if kind == WebKit.PolicyDecisionType.RESPONSE and not decision.is_mime_type_supported():
            decision.download()
            return True
        return False

    # ------------------------------------------------------------ Fenster der Seite (E89)
    # Bis E88 lag jedes Fenster sofort über der Seite. Werbefenster blieben dabei leer oder waren
    # gesperrt, verdeckten die Seite aber und nahmen jeden Klick (Wunsch 2026-10-21). Jetzt urteilt
    # popups.verdict: gezeigt wird nur dieselbe Seite oder eine Anmeldung, mit Rahmen und
    # „Schließen“; ein leeres Fenster wartet unsichtbar auf seine Adresse und ist nach
    # POPUP_WAIT_MS weg. Kreis, L1 und der Zurück-Knopf schließen es.
    def on_create(self, view, action):
        """Neues Fenster einer Seite (window.open): über der Seite, mit Verbindung zu ihr."""
        uri = (action.get_request().get_uri() if action else "") or ""
        verdict = popups.verdict(uri, view.get_uri() or "")
        if verdict == popups.BLOCK:
            self.popup_blocked(uri)
            return self._sink(view)
        if self.popup is not None:
            self.close_popup()
        popup = WebKit.WebView(related_view=view)
        popup.set_vexpand(True)
        popup.set_hexpand(True)
        popup.connect("ready-to-show", self.on_popup_ready)
        popup.connect("close", lambda w: self.close_popup(w))
        popup.connect("decide-policy", self.on_popup_policy)
        popup.connect("create", self.on_create)            # ein Fenster im Fenster: ersetzt es
        self.popup = popup
        self.popup_uri = uri
        self.popup_allowed = verdict == popups.ALLOW
        self.popup_ready = False
        self._popup_timer = GLib.timeout_add(POPUP_WAIT_MS, self._popup_timeout, popup)
        _log.info("Fenster der Seite geöffnet (%s): %s", verdict, uri[:80])
        return popup

    def _sink(self, view):
        """Gesperrtes Pop-up: ein unsichtbares Fenster, das nichts lädt. Werbeskripte legen gern eine
        durchsichtige Fläche über die Seite, die erst verschwindet, wenn window.open ein Fenster
        liefert; ohne Fenster blieb die Seite tot (Wunsch 2026-10-21, nachgestellt)."""
        sink = WebKit.WebView(related_view=view)
        sink.connect("decide-policy", lambda _w, decision, _k: decision.ignore() or True)
        sink.connect("create", lambda *_a: None)
        self._sinks.append(sink)
        GLib.timeout_add(SINK_MS, self._drop_sink, sink)
        return sink

    def _drop_sink(self, sink) -> bool:
        if sink in self._sinks:
            self._sinks.remove(sink)
        return False

    def on_popup_ready(self, popup) -> None:
        if popup is self.popup:
            self.popup_ready = True
            if self.popup_allowed:
                self.show_popup()

    def show_popup(self) -> None:
        popup = self.popup
        if popup is None or self.popup_frame is not None:
            return
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("popup")
        frame.set_overflow(Gtk.Overflow.HIDDEN)    # WebKit zeichnet anfangs größer als sein Platz
        for side in ("top", "bottom", "start", "end"):
            getattr(frame, "set_margin_" + side)(28)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.add_css_class("popup-head")
        label = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        close = Gtk.Button(label=_t("Schließen (Kreis)"))
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_: self.close_popup(popup))
        head.append(label)
        head.append(close)
        frame.append(head)
        frame.append(popup)

        def title(*_a) -> None:
            host = popups.site(popup.get_uri() or self.popup_uri) or "einer Seite"
            label.set_text(_t("Fenster von %s" % host))
        popup.connect("notify::uri", title)
        title()
        self.popup_frame = frame
        self.stage.add_overlay(frame)
        popup.grab_focus()
        self.update_buttons()

    def _popup_timeout(self, popup) -> bool:
        if popup is self.popup:
            self._popup_timer = 0
            if self.popup_frame is None:
                _log.info("Fenster der Seite blieb leer: geschlossen")
                self.close_popup(popup)
        return False

    def close_popup(self, popup=None) -> None:
        popup = popup or self.popup
        if popup is None or popup is not self.popup:
            return
        if self._popup_timer:
            GLib.source_remove(self._popup_timer)
            self._popup_timer = 0
        frame, self.popup_frame, self.popup = self.popup_frame, None, None
        if frame is not None:
            self.stage.remove_overlay(frame)
            frame.remove(popup)
        self.view.grab_focus()
        self.update_buttons()

    def on_popup_policy(self, popup, decision, kind) -> bool:
        """Anmeldungen, die im Fenster selbst enden (GOG leitet manchmal dort um). Ein Fenster,
        das noch nicht gezeigt werden darf, wird an seiner ersten Adresse gemessen (E89)."""
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if gog_account.is_login_redirect(uri) or steam_account.LOGIN_DONE in uri:
                decision.ignore()
                self.close_popup(popup)
                self.view.load_uri(uri)                     # die Hauptseite fängt es ab (on_policy)
                return True
            if itch_account.is_login_redirect(uri):
                decision.ignore()                           # E100: gleich hier, ohne die Adresse zu laden
                self.close_popup(popup)
                self.take_itch_code(uri)
                return True
            if popup is self.popup and not self.popup_allowed:
                verdict = popups.verdict(uri, self.view.get_uri() or "")
                if verdict == popups.BLOCK:
                    decision.ignore()
                    self.close_popup(popup)
                    self.popup_blocked(uri)
                    return True
                if verdict == popups.ALLOW:
                    self.popup_allowed = True
                    self.popup_uri = uri
                    if self.popup_ready:
                        self.show_popup()
        if kind == WebKit.PolicyDecisionType.RESPONSE and not decision.is_mime_type_supported() \
                and not epic_account.is_login_redirect(decision.get_request().get_uri() or ""):
            # Wie in der Hauptansicht (E59, E99): Nicht Darstellbares wird ein Download. WebKits
            # Vorgabe lud nur Antworten mit „attachment“ und ließ sonst ein leeres Fenster stehen.
            # Der Download gehört der gemeinsamen Sitzung (on_download); das Fenster geht zu.
            # Ausgenommen das JSON mit Epics Anmelde-Code: Das soll nie als Datei im Download-Ordner landen
            decision.download()
            main = getattr(decision, "is_main_frame_main_resource", None)
            if main is None or main():
                GLib.idle_add(lambda: self.close_popup(popup) or False)
            return True
        return False

    def popup_blocked(self, uri: str) -> None:
        """Hinweis, höchstens alle 3 s (Werbeskripte versuchen es bei jedem Klick); fremde Seiten
        bietet die Leiste noch BLOCKED_OFFER_S Sekunden zum Öffnen an, Werbung nicht."""
        _log.info("Pop-up gesperrt: %s", uri[:80])
        ad = adblock.is_ad(uri) or not uri.startswith(("http://", "https://"))
        now = time.monotonic()
        if now - self._blocked_at > 3:
            self.show_toast("Werbefenster gesperrt." if ad else
                            "Pop-up von %s gesperrt. Oben „Pop-up öffnen“, falls du es brauchst."
                            % (popups.site(uri) or "einer Seite"))
        self._blocked_at = now
        if ad:
            return
        self.blocked_uri = uri
        self.blocked_btn.set_tooltip_text(address.shown(uri)[:120])
        self.blocked_btn.set_visible(True)
        if self._blocked_timer:
            GLib.source_remove(self._blocked_timer)
        self._blocked_timer = GLib.timeout_add_seconds(BLOCKED_OFFER_S, self._hide_blocked)

    def _hide_blocked(self) -> bool:
        self._blocked_timer = 0
        self.blocked_btn.set_visible(False)
        return False

    def open_blocked(self) -> None:
        """Das gesperrte Pop-up doch zeigen: als Seite im Browser (zurück führt zur alten)."""
        uri, self.blocked_uri = self.blocked_uri, ""
        if self._blocked_timer:
            GLib.source_remove(self._blocked_timer)
        self._hide_blocked()
        if uri:
            self.view.load_uri(uri)
            self.view.grab_focus()

    def on_permission(self, _view, request) -> bool:
        request.deny()                  # Standort, Kamera, Mitteilungen … nicht auf der Konsole
        return True

    def on_fullscreen(self, *_a) -> bool:
        self.bar.set_visible(False)
        self.progress.set_visible(False)
        return False

    def on_unfullscreen(self, *_a) -> bool:
        self.bar.set_visible(True)
        return False

    def on_crash(self, _view, reason) -> None:
        now = time.monotonic()
        self.crashes = [t for t in self.crashes if now - t < 60] + [now]
        _log.warning("Web-Prozess beendet (%s)", reason)
        if len(self.crashes) <= CRASH_LIMIT:
            self.show_toast("Die Seite ist abgestürzt und wird neu geladen.")
            GLib.timeout_add(800, lambda: self.view.reload() and False)
        else:
            self.show_toast("Die Seite stürzt immer wieder ab. Neu laden mit dem Pfeil oben.")

    def show_toast(self, text: str) -> None:
        self.toast.set_text(_t(text))
        self.toast.set_visible(True)
        if self._toast_timer:
            GLib.source_remove(self._toast_timer)
        self._toast_timer = GLib.timeout_add(TOAST_MS, self._hide_toast)

    def _hide_toast(self) -> bool:
        self._toast_timer = 0
        self.toast.set_visible(False)
        return False

    # ------------------------------------------------------------ Downloads
    # E60: http(s)-Downloads lädt das Home (anhalten, fortsetzen, übersteht Neustarts). Der Browser
    # bricht seinen eigenen Download gleich ab und schreibt einen Auftrag mit Adresse, Name und den
    # Kopfzeilen, die der Server erwartet (Cookies, Referer, User-Agent). Was das Home nicht holen
    # kann (blob:, data:, Formular-POST), lädt WebKit wie bisher selbst in den Download-Ordner.
    def on_download(self, _session, download) -> None:
        download.connect("decide-destination", self.on_decide)
        download.connect("failed", self.on_failed)
        download.connect("finished", self.on_finished)

    def on_decide(self, download, suggested: str) -> bool:
        request = download.get_request()
        uri = request.get_uri() if request is not None else ""
        method = (request.get_http_method() or "GET") if request is not None else "GET"
        name = downloads.safe_name(suggested or "")
        if uri.startswith(("http://", "https://")) and method.upper() == "GET":
            self.handed[download] = name
            download.cancel()
            self._hand_over(uri, name)
            return True
        try:
            os.makedirs(self.dl_dir, exist_ok=True)
        except OSError as e:
            _log.warning("Download-Ordner %s: %s", self.dl_dir, e)
        path = downloads.unique_path(self.dl_dir, name)
        download.set_allow_overwrite(False)
        download.set_destination(path)
        response = download.get_response()
        total = response.get_content_length() if response is not None else 0
        self.local[download] = self.local_log.add(os.path.basename(path), path, total)
        download.connect("received-data", self.on_received)
        self.show_toast("Download: %s\nZu sehen im Control Center unter Downloads (PS-Taste)." % os.path.basename(path))
        return True

    def on_received(self, download, _length) -> None:
        item_id = self.local.get(download)
        if item_id is not None:
            response = download.get_response()
            self.local_log.progress(item_id, download.get_received_data_length(),
                                    response.get_content_length() if response is not None else 0)

    def _hand_over(self, uri: str, name: str) -> None:
        headers = {"User-Agent": self.view.get_settings().get_user_agent() or ""}
        page = self.view.get_uri() or ""
        if page.startswith(("http://", "https://")):
            headers["Referer"] = page
        if self.lang:
            headers["Accept-Language"] = self.lang

        def got(manager, result) -> None:
            try:
                cookies = manager.get_cookies_finish(result) or []
                jar = "; ".join("%s=%s" % (c.get_name(), c.get_value()) for c in cookies)
                if jar:
                    headers["Cookie"] = jar
            except GLib.Error as e:
                _log.warning("Cookies für den Download: %s", e.message)
            try:
                downloads.write_request(self.requests, uri, name, {k: v for k, v in headers.items() if v})
                self.show_toast("Download: %s\nDie Konsole lädt ihn; anhalten und fortsetzen im Control "
                                "Center unter Downloads (PS-Taste)." % name)
            except OSError as e:
                _log.error("Download-Auftrag nicht schreibbar: %s", e)
                self.show_toast("Der Download ließ sich nicht an die Konsole übergeben.")
        self.session.get_cookie_manager().get_cookies(uri, None, got)

    def on_failed(self, download, error) -> None:
        if self.handed.pop(download, None) is not None:
            return                                      # absichtlich abgebrochen: lädt das Home
        item_id = self.local.pop(download, None)        # danach kommt noch „finished“: dann nicht mehr hier
        if item_id is None:
            return
        cancelled = error is not None and error.matches(WebKit.download_error_quark(),
                                                        WebKit.DownloadError.CANCELLED_BY_USER)
        message = "" if error is None else getattr(error, "message", "") or ""
        item = self.local_log.get(item_id)
        self.local_log.end(item_id, "cancelled" if cancelled else "failed",
                           downloads.TEXT_CLOSED if cancelled else message)
        if not cancelled:
            self.show_toast("Download fehlgeschlagen: %s" % (item["name"] if item else ""))

    def on_finished(self, download) -> None:
        self.handed.pop(download, None)
        item_id = self.local.pop(download, None)
        if item_id is not None:
            dest = download.get_destination() or ""
            path = GLib.filename_from_uri(dest)[0] if dest.startswith("file:") else dest
            self.local_log.progress(item_id, download.get_received_data_length())
            self.local_log.end(item_id, "done", path=path if os.path.isabs(path or "") else "")
            self.show_toast("Download fertig: %s" % os.path.basename(path or dest))

    def _take_open_request(self) -> None:
        """Adresse vom Home (Store, E68): einmal öffnen, dann ist der Auftrag weg."""
        path = _env_path("COUCH_BROWSER_OPEN_FILE", paths.browser_open_file())
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            os.unlink(path)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return                          # Ausnahme hier beendete den Zeitgeber (_poll_downloads) für immer
        url = str(data.get("url") or "")
        try:
            fresh = time.time() - float(data.get("at") or 0) < 120
        except (TypeError, ValueError):
            fresh = False
        if fresh and url.startswith("https://"):
            self.close_after = str(data.get("close") or "")
            if self.close_after == "app-login":
                self.trust_local_server(url)
            self.view.load_uri(url)

    def _poll_downloads(self) -> bool:
        """Anzeige oben: wie viele Downloads das Home gerade lädt (aus seiner Liste). Nebenbei:
        Adressen, die das Home öffnen lassen will."""
        self._take_open_request()
        items = downloads.read(self.dl_list)
        n = sum(1 for i in items if i.get("state") in ("queued", "running")) + len(self.local)
        self.dl_label.set_visible(n > 0)
        self.dl_label.set_text("⬇ %d" % n)
        self.dl_label.set_tooltip_text(_t("Downloads laufen. Zu sehen in der Konsole unter Downloads."))
        return True

    def update_downloads(self) -> None:
        self._poll_downloads()
        GLib.timeout_add_seconds(2, self._poll_downloads)   # einmal; läuft, solange der Browser läuft

    # ------------------------------------------------------------ Ende
    def osk_text(self, text: str) -> None:
        """Text der Bildschirmtastatur dort einsetzen, wo der Fokus ist: Adressleiste, Seite oder Fenster
        der Seite (Anmeldung). Auf der Seite wie Einfügen (InsertText im Rahmen mit dem Fokus)."""
        focus = self.win.get_focus()
        if focus is not None and (focus is self.entry or focus.is_ancestor(self.entry)):
            self.entry.delete_selection()
            pos = self.entry.insert_text(text, self.entry.get_position())
            self.entry.set_position(pos)
            return
        view = focus
        while view is not None and not isinstance(view, WebKit.WebView):
            view = view.get_parent()
        (view or self.popup or self.view).execute_editing_command_with_argument("InsertText", text)

    def quit(self) -> bool:
        if self.osk is not None:
            self.osk.close()
            self.osk = None
        # Was WebKit selbst lädt, endet mit dem Browser (auch über die PS-Taste, SIGTERM): als
        # „abgebrochen“ in die Liste, sonst stünde es im Control Center für immer als laufend (E99)
        self.local_log.close_running()
        for download in list(self.local):
            try:
                download.cancel()
            except Exception:
                pass
        self.app.quit()
        return False


def main(argv: list[str] | None = None) -> int:
    log.setup()                      # stderr: der AppManager schreibt es nach apps/app_browser.log
    app = Gtk.Application(application_id="org.couch.Browser", flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder: dict = {}

    def activate(gtk_app) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        # Über der Stildatei des Benutzers (~/.config/gtk-4.0/gtk.css, z. B. WhiteSur, Vorrang
        # USER): sonst blieben Knöpfe und Adressleiste klein wie am Schreibtisch
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", True)
        holder["b"] = Browser(gtk_app)

    def stop() -> bool:
        b = holder.get("b")
        if b is not None:
            b.quit()
        else:
            app.quit()
        return False

    app.connect("activate", activate)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, stop)
    return app.run([sys.argv[0]] if argv is None else argv)
