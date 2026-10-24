"""Text der Bildschirmtastatur direkt an eine eigene App (Browser, Terminal), ohne XTEST.

Warum (Wunsch 2026-10-24: „Ich kann im Browser kein @ eingeben, die Taste macht eine 2“): Gamescope
reicht Umschalt und AltGr aus XTEST an GTK-Fenster nicht weiter. Gemessen am echten Fernseher: Aus
„@“ (Umschalt+2 auf us) wurde „2“, aus Strg+A ein „a“ – auch mit xdotool und Pausen zwischen den
Tasten. Tasten ohne Umschalttaste (Rücktaste, Pfeile, Enter) gehen weiter über XTEST; nur Text kommt
hierüber: ein Datagramm {"text": …} an <run>/osk-<app>.sock, das die App selbst einsetzt.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import stat
from typing import Callable

_log = logging.getLogger("couch.oskdirect")

MAX_BYTES = 4096


def send(path: str, text: str) -> bool:
    """True, wenn die App den Text angenommen hat (sonst XTEST als Rückfall). Blockiert nie."""
    data = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    if not text or len(data) > MAX_BYTES:
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.setblocking(False)
        s.sendto(data, path)
        return True
    except OSError as e:                # keine App am Eingang (beendet, ältere Fassung): Rückfall
        _log.debug("Bildschirmtastatur direkt an %s: %s", path, e)
        return False
    finally:
        s.close()


def parse(data: bytes) -> str:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    text = msg.get("text") if isinstance(msg, dict) else None
    return text if isinstance(text, str) else ""


class Server:
    """Eingang in einer GTK-App: liest Datagramme in der GLib-Hauptschleife und ruft on_text."""

    def __init__(self, path: str, on_text: Callable[[str], None]):
        from gi.repository import GLib
        self.path, self.on_text, self._GLib = path, on_text, GLib
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._remove_stale()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        old = os.umask(0o177)                   # nur der eigene Benutzer darf hineinschreiben
        try:
            self.sock.bind(path)
        finally:
            os.umask(old)
        self.source = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.sock.fileno(), GLib.IOCondition.IN, self._ready)

    def _remove_stale(self) -> None:
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise OSError("%s ist kein Socket" % self.path)
        os.unlink(self.path)                    # Rest einer früheren Instanz (nur ein Socket, nie eine Datei)

    def _ready(self, *_a) -> bool:
        while True:
            try:
                data = self.sock.recv(MAX_BYTES)
            except BlockingIOError:
                return True
            except OSError as e:
                _log.warning("Eingang der Bildschirmtastatur: %s", e)
                return True
            text = parse(data)
            if text:
                try:
                    self.on_text(text)
                except Exception:
                    _log.exception("Bildschirmtastatur: Text einsetzen")

    def close(self) -> None:
        if self.source:
            self._GLib.source_remove(self.source)
            self.source = 0
        try:
            self.sock.close()
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except OSError:
            pass
