"""Der gekoppelte Fernseher (E142): <config>/tv/tv.json (0600), neben den Schlüsseln.

  host        Adresse (IPv4 oder Rechnername), mit der zuletzt gesprochen wurde
  name        Name aus der Suche (mDNS) bzw. die eingegebene Adresse; nur zur Anzeige
  hostname    Rechnername aus der Suche: Antwortet die Adresse nicht mehr (neue Adresse per DHCP), sucht der Teil
              den Fernseher unter diesem Namen bzw. name neu
  mac, iface  MAC und Netzgerät aus der Nachbartabelle (Wake-on-LAN)
  paired_at   wann gekoppelt
  adb         ob „Eingänge genauer erkennen“ geklappt hat (der Schlüssel liegt in adbkey)
  ports       gelernte HDMI-Eingänge [{port, id, name, state}] (adb)
  pc_port     Eingang des PCs; pc_from: „adb“ (lief beim Erkennen) oder „edid“
  boot        boot_id, nach dem schon einmal geweckt wurde (Einschalten nach einem echten Start, einmal je Start)

„Kopplung vergessen“ schreibt eine leere Datei; die Schlüssel bleiben (SAFETY: gelöscht wird nur über trash.py).
"""
from __future__ import annotations

import json
import logging
import os
import threading

from .. import system

log = logging.getLogger("couch.tv")

FILE = "tv.json"
KEYS = ("host", "name", "hostname", "mac", "iface", "paired_at", "adb", "ports", "pc_port", "pc_from", "boot")
MAX_BYTES = 256 * 1024


class Store:
    def __init__(self, folder: str):
        self.folder = folder
        self.path = os.path.join(folder, FILE)
        self._lock = threading.Lock()
        self._data: dict | None = None

    def _load(self) -> dict:
        if self._data is None:
            data: dict = {}
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                with os.fdopen(fd, "rb") as f:
                    raw = json.loads(f.read(MAX_BYTES).decode("utf-8"))
                if isinstance(raw, dict):
                    data = {k: v for k, v in raw.items() if k in KEYS}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, UnicodeDecodeError) as e:
                log.warning("tv.json unlesbar: %s", e)
            self._data = data
        return self._data

    def get(self, key: str, default=None):
        with self._lock:
            return self._load().get(key, default)

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._load()))

    def update(self, **values) -> None:
        with self._lock:
            data = dict(self._load())
            for k, v in values.items():
                if k not in KEYS:
                    raise KeyError(k)
                if v is None:
                    data.pop(k, None)
                else:
                    data[k] = v
            if data == self._data and os.path.exists(self.path):
                return
            self._write(data)

    def clear(self) -> None:
        with self._lock:
            self._write({})

    def _write(self, data: dict) -> None:
        os.makedirs(self.folder, mode=0o700, exist_ok=True)
        system.write_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=1) + "\n", mode=0o600)
        self._data = data

    def paired(self) -> bool:
        return bool(self.get("host")) and bool(self.get("paired_at"))
