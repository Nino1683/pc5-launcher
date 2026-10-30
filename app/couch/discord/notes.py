"""Nachrichten aus Discord als Mitteilung des PC5 Launchers (E139): mitlesen, nie selbst Mitteilungsdienst sein.

In der Spielmodus-Sitzung gehört der Name org.freedesktop.Notifications Steams steam_notif_daemon (gesehen 2026-10-29).
Der Launcher übernimmt ihn deshalb nicht, sondern öffnet eine eigene, zweite Verbindung zum Sitzungsbus und macht sie
mit org.freedesktop.DBus.Monitoring.BecomeMonitor zum Beobachter, nur für diese Aufrufe (MATCH_RULES):

  org.freedesktop.Notifications.Notify                 (app_name, replaces_id, icon, summary, body, actions, hints, …);
                                                        Discord am Namen oder an der .flatpak-info des Absenders
  org.freedesktop.portal.Notification.AddNotification  (id, notification) – vom Flatpak über das Portal; welche App,
                                                        sagt die .flatpak-info des Absenders (Prozess über den Bus)
  org.freedesktop.impl.portal.Notification.AddNotification  (app_id, id, notification) – das Portal an sein Backend

Am Fernseher geprüft (2026-10-29, notify-send aus der Discord-Sandbox, dieselbe libnotify wie Discord): Das Portal der
Spielmodus-Sitzung hat keine Schnittstelle Notification, libnotify fällt auf Notify zurück, und das sperrt die Sandbox,
solange Discords finish-args es nicht erlauben. Deshalb trägt bin/couch-discord-prep vor jedem Discord-Start die
Freigabe org.freedesktop.Notifications=talk in Discords Flatpak-Overrides ein (launch/apps._discord); dann kommt jede
Nachricht als Notify an Steams Dienst, und der Absender ist Discords D-Bus-Stellvertreter (er trägt die .flatpak-info).
Die Portal-Wege bleiben für Sitzungen mit Backend. Kommt dieselbe Nachricht auf zwei Wegen, zeigt Dedupe sie einmal.

Ein Beobachter darf nichts senden (sonst trennt ihn der Bus): Der Filter lässt Aufrufe und Signale nicht weiter in GDBus
(sonst antwortete GDBus mit „UnknownMethod“), nur die Antwort auf BecomeMonitor. Er läuft im Thread von GDBus, kopiert
nur die Felder und legt sie in eine Warteschlange; ausgewertet wird in einem eigenen Thread (für das Portal fragt der
eine normale Verbindung nach dem Prozess des Absenders). Die GLib-Hauptschleife berührt nichts davon. Fremde
Mitteilungen werden sofort verworfen und nirgends gespeichert oder protokolliert.

Die Auswertung (from_notify, from_portal, clean, Dedupe) ist ohne D-Bus prüfbar; Monitor ist die dünne Gio-Schicht.
"""
from __future__ import annotations

import html
import logging
import queue
import re
import threading
import time
from typing import Any, Callable

from . import FLATPAK, TITLE

log = logging.getLogger("couch.discord")

NOTIFY_IFACE = "org.freedesktop.Notifications"
PORTAL_IFACE = "org.freedesktop.portal.Notification"
IMPL_IFACE = "org.freedesktop.impl.portal.Notification"
MATCH_RULES = (
    "type='method_call',interface='%s',member='Notify'" % NOTIFY_IFACE,
    "type='method_call',interface='%s',member='AddNotification'" % PORTAL_IFACE,
    "type='method_call',interface='%s',member='AddNotification'" % IMPL_IFACE,
)
NAMES = frozenset({"discord", FLATPAK.lower(), FLATPAK.lower() + ".desktop"})
MAX_TITLE, MAX_BODY = 80, 240
DEDUPE_S = 5.0                      # dieselbe Nachricht auf zwei Wegen (Portal, Backend) gilt einmal
BURST, BURST_S = 8, 60.0            # mehr Einblendungen je Minute nicht: der Rest steht nur in der Liste

_TAG = re.compile(r"<[^>]{0,400}>")
_SPACE = re.compile(r"[ \t\r\f\v]+")


def clean(text: Any, limit: int) -> str:
    """Text einer Mitteilung ohne Markup (b, i, u, a, img nach der Spezifikation), Entitäten aufgelöst, gekürzt."""
    text = _TAG.sub("", str(text or ""))
    text = html.unescape(text)
    text = "".join(ch for ch in text if ch in "\n" or ch.isprintable())
    lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def is_discord(*names: Any) -> bool:
    return any(isinstance(n, str) and n.strip().lower() in NAMES for n in names)


def _note(summary: Any, body: Any, via: str) -> dict | None:
    title = clean(summary, MAX_TITLE)
    text = clean(body, MAX_BODY)
    if not title and not text:
        return None
    return {"title": title or TITLE, "body": text, "via": via}


def from_notify(args: Any, app_id: str = "") -> dict | None:
    """Notify (s app_name, u replaces_id, s app_icon, s summary, s body, as actions, a{sv} hints, i timeout);
    app_id ist die Flatpak-Kennung des Absenders, falls schon bekannt (sonst zählen nur Name und desktop-entry)."""
    if not isinstance(args, (tuple, list)) or len(args) < 7:
        return None
    hints = args[6] if isinstance(args[6], dict) else {}
    if not is_discord(args[0], hints.get("desktop-entry"), app_id):
        return None
    return _note(args[3], args[4], "notify")


def _portal_text(notification: Any) -> tuple:
    n = notification if isinstance(notification, dict) else {}
    return n.get("title"), n.get("markup-body") or n.get("body")


def from_impl(args: Any) -> dict | None:
    """Backend des Portals: AddNotification (s app_id, s id, a{sv} notification)."""
    if not isinstance(args, (tuple, list)) or len(args) < 3 or not is_discord(args[0]):
        return None
    return _note(*_portal_text(args[2]), "portal")


def from_portal(args: Any, app_id: str) -> dict | None:
    """Portal: AddNotification (s id, a{sv} notification); app_id ist die Flatpak-Kennung des Absenders."""
    if not isinstance(args, (tuple, list)) or len(args) < 2 or not is_discord(app_id):
        return None
    return _note(*_portal_text(args[1]), "portal")


def flatpak_app_of(pid: int, proc: str = "/proc") -> str:
    """Flatpak-Kennung eines Prozesses aus seiner /.flatpak-info (auch der D-Bus-Stellvertreter einer Sandbox hat
    sie); "" ohne. Nur lesend."""
    try:
        with open("%s/%d/root/.flatpak-info" % (proc, int(pid)), encoding="utf-8", errors="replace") as f:
            text = f.read(16384)
    except (OSError, ValueError):
        return ""
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Application]" and line.startswith("name="):
            return line[5:].strip()
    return ""


class Dedupe:
    """Was schon gezeigt wurde (Titel und Text) und wie viele Einblendungen gerade liefen."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._seen: dict[tuple, float] = {}
        self._shown: list[float] = []

    def take(self, note: dict) -> str | None:
        """None = schon gezeigt (verwerfen); "toast" = einblenden; "list" = nur in die Liste (zu viele)."""
        now = self._clock()
        self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_S}
        key = (note.get("title"), note.get("body"))
        if key in self._seen:
            return None
        self._seen[key] = now
        self._shown = [t for t in self._shown if now - t < BURST_S]
        if len(self._shown) >= BURST:
            return "list"
        self._shown.append(now)
        return "toast"


class Monitor:
    """Beobachter auf dem Sitzungsbus. on_note(note, how) läuft im eigenen Thread (how wie Dedupe.take).
    start() und stop() kehren sofort zurück; der Aufbau der Verbindung läuft im Thread."""

    def __init__(self, on_note: Callable[[dict, str], None], *, address: str | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._on_note = on_note
        self._address = address               # None = Sitzungsbus der Umgebung
        self._dedupe = Dedupe(clock)
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._conn: Any = None
        self._filter_id = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "off"                    # off | starting | on | failed (für die Einstellungsseite)

    # ------------------------------------------------------------ außen
    def start(self) -> None:
        if self._thread is not None:
            return
        # Je Lauf eigene Warteschlange und Marke: Ein alter Thread, der nach stop() noch aufräumt, nimmt dem neuen nichts
        self._stop, self._queue = threading.Event(), queue.Queue(maxsize=200)
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, args=(self._stop, self._queue), name="discord-notes",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                if self._filter_id:
                    conn.remove_filter(self._filter_id)
                conn.close(None, None, None)        # ohne zu warten
            except Exception:  # noqa: BLE001
                log.exception("Discord: Beobachter schließen")
        self._filter_id = 0
        self._thread = None
        self.state = "off"

    def feed(self, interface: str, sender: str, args: Any) -> None:
        """Eine beobachtete Nachricht (aus dem Filter oder aus Tests)."""
        try:
            self._queue.put_nowait((interface, sender, args))
        except queue.Full:
            pass                                # Flut: lieber verlieren als die Hauptschleife oder GDBus aufhalten

    # ------------------------------------------------------------ Thread
    def _run(self, stop: threading.Event, items: queue.Queue) -> None:
        if not self._connect(stop):
            if not stop.is_set():
                self.state = "failed"
            return
        if not stop.is_set():
            self.state = "on"
        while not stop.is_set():
            item = items.get()
            if item is None or stop.is_set():
                break
            try:
                self._handle(*item)
            except Exception:  # noqa: BLE001
                log.exception("Discord: Mitteilung auswerten")

    def _handle(self, interface: str, sender: str, args: Any) -> None:
        if interface == NOTIFY_IFACE:
            # Erst am Namen, sonst am Absender (der D-Bus-Stellvertreter der Sandbox trägt Discords .flatpak-info)
            note = from_notify(args) or from_notify(args, self._sender_app(sender))
        elif interface == IMPL_IFACE:
            note = from_impl(args)
        elif interface == PORTAL_IFACE:
            note = from_portal(args, self._sender_app(sender))
        else:
            note = None
        if note is None:
            return
        how = self._dedupe.take(note)
        if how is not None:
            self._on_note(note, how)

    def _sender_app(self, sender: str) -> str:
        """Flatpak-Kennung des Absenders (Prozess über org.freedesktop.DBus, dann /.flatpak-info)."""
        if not sender:
            return ""
        try:
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None) if self._address is None else \
                Gio.DBusConnection.new_for_address_sync(self._address, _flags(Gio), None, None)
            reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)),
                                  GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
            if self._address is not None:
                bus.close_sync(None)
            return flatpak_app_of(int(reply.unpack()[0]))
        except Exception as e:  # noqa: BLE001
            log.info("Discord: Absender %s unbekannt: %s", sender, e)
            return ""

    def _connect(self, stop: threading.Event) -> bool:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
            address = self._address or Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            conn = Gio.DBusConnection.new_for_address_sync(address, _flags(Gio), None, None)
            # Der Filter zuerst: Ab der Antwort auf BecomeMonitor kommen die Aufrufe, und GDBus darf keinen
            # davon selbst beantworten
            self._filter_id = conn.add_filter(self._filter, None)
            conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.Monitoring",
                           "BecomeMonitor", GLib.Variant("(asu)", (list(MATCH_RULES), 0)), None,
                           Gio.DBusCallFlags.NONE, 5000, None)
        except Exception as e:  # noqa: BLE001
            log.warning("Discord: Mitteilungen lassen sich nicht mitlesen: %s", e)
            return False
        if stop.is_set():
            conn.close(None, None, None)
            return False
        self._conn = conn
        log.info("Discord: Mitteilungen werden mitgelesen")
        return True

    def _filter(self, _conn: Any, message: Any, incoming: bool, _data: Any = None) -> Any:
        """Im Thread von GDBus: nur kopieren. Aufrufe und Signale gehen nicht weiter (sonst antwortete GDBus)."""
        try:
            from gi.repository import Gio
            if not incoming:
                return message
            kind = message.get_message_type()
            if kind not in (Gio.DBusMessageType.METHOD_CALL, Gio.DBusMessageType.SIGNAL):
                return message                  # Antwort auf BecomeMonitor
            if kind == Gio.DBusMessageType.METHOD_CALL and not self._stop.is_set():
                body = message.get_body()
                self.feed(message.get_interface() or "", message.get_sender() or "",
                          body.unpack() if body is not None else ())
        except Exception:  # noqa: BLE001
            log.exception("Discord: Filter")
        return None


def _flags(Gio: Any) -> Any:
    return Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
