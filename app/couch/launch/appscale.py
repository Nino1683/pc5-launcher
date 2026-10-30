"""Größe von Apps auf dem Fernseher (E145, Wunsch 2026-10-30: „Die ist viel zu hoch man kann nichts lesen. Auch andere
apps dürfen nicht so klein dargestellt werden“ – „Man sollte dies einstellen können“).

Apps laufen im Xwayland der Spiele in der vollen Auflösung des Fernsehers. Ohne Hinweis zeichnen sie sich dort im
Maßstab 1, bei 4K also winzig (Discord merkte sich ein Fenster über den ganzen Bildschirm). Gamescope vergrößert nur
Fenster, die kleiner sind als der Bildschirm. Deshalb bekommt jede App beim Start einen Maßstab:

  Einstellung     display.app_scale: 0 = automatisch (nach der Höhe der Ausgabe: bis 1080 → 100 %, bis unter 2160
                  → 150 %, ab 2160 → 200 %), sonst ein fester Wert aus CHOICES (Prozent)
  GTK             GDK_SCALE (ganzzahlig) und GDK_DPI_SCALE (der Rest, nur Schrift): 150 % = 1 und 1,5
  Qt              QT_SCALE_FACTOR, QT_AUTO_SCREEN_SCALE_FACTOR=0
  Chromium/Electron  --force-device-scale-factor (Chrome, Discord und Flatpaks auf der Basis von Electron oder
                  Chromium, erkannt an „base=“ in den Metadaten des Flatpaks)

Gilt für Apps (Discord, App Store, Chrome, Apps aus dem Reiter „Apps“ und „Medien“, Emulatoren zum Einstellen), nie
für Spiele. YouTube (VacuumTube), Kodi, Moonlight und der eigene Browser haben eigene Größen und bleiben, wie sie sind.
Bei 100 % kommt nichts dazu: dann sieht die App genau aus wie vorher.
"""
from __future__ import annotations

import os

CHOICES = (0, 100, 125, 150, 175, 200, 250, 300)     # 0 = automatisch
DEFAULT = 0
KEY = "display.app_scale"
# Chromium-Browser und Electron-Apps, deren Metadaten keine Basis nennen (extra-data, eigene Laufzeit)
CHROMIUM_IDS = {"com.google.Chrome", "org.chromium.Chromium", "com.brave.Browser", "com.microsoft.Edge",
                "com.vivaldi.Vivaldi", "com.opera.Opera", "com.discordapp.Discord", "com.spotify.Client",
                "io.github.ungoogled_software.ungoogled_chromium"}
CHROMIUM_BASES = ("org.electronjs.Electron2.BaseApp", "org.chromium.Chromium.BaseApp")


def auto_percent(height: int) -> int:
    """Maßstab nach der Höhe der Ausgabe (wie --force-device-scale-factor des Browsers bisher)."""
    if height <= 1080:
        return 100
    if height < 2160:
        return 150
    return 200


def choice(raw) -> int:
    """Gespeicherten Wert lesen: einer aus CHOICES, sonst automatisch."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in CHOICES:
        return DEFAULT
    return raw


def percent(config, height: int) -> int:
    """Wirksamer Maßstab in Prozent für eine Ausgabe dieser Höhe."""
    value = choice(config.get(KEY, DEFAULT) if config is not None else DEFAULT)
    return value or auto_percent(height)


def factor(pct: int) -> str:
    """Prozent als Faktor für Schalter und Umgebung: 150 -> "1.5", 200 -> "2"."""
    return ("%.2f" % (pct / 100)).rstrip("0").rstrip(".")


def toolkit_env(pct: int) -> dict:
    """Umgebung für GTK- und Qt-Apps. Leer bei 100 %."""
    if pct == 100:
        return {}
    scale = pct / 100
    whole = max(1, int(scale))
    env = {"GDK_SCALE": str(whole), "QT_SCALE_FACTOR": factor(pct), "QT_AUTO_SCREEN_SCALE_FACTOR": "0"}
    rest = scale / whole
    if abs(rest - 1) > 1e-6:
        env["GDK_DPI_SCALE"] = ("%.3f" % rest).rstrip("0").rstrip(".")
    return env


def chromium_args(pct: int) -> list:
    """Schalter für Chromium und Electron. Leer bei 100 %."""
    return [] if pct == 100 else ["--force-device-scale-factor=%s" % factor(pct)]


def is_chromium(app_id: str, flatpak_dirs) -> bool:
    """Ist das Flatpak ein Chromium oder eine Electron-App? Bekannte Kennungen oder „base=“ in seinen Metadaten
    (<installation>/app/<id>/current/active/metadata, nur gelesen, höchstens 64 KB)."""
    if app_id in CHROMIUM_IDS:
        return True
    for root in flatpak_dirs or ():
        path = os.path.join(root, "app", app_id, "current", "active", "metadata")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(64 * 1024)
        except OSError:
            continue
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "base" and value.strip().split("/")[0] in CHROMIUM_BASES:
                return True
        return False
    return False
