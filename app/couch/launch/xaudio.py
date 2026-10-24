"""Microsofts XAudio2 für Spiele mit Sonys Controller-Modul (Bauplan E77).

Sonys Unreal-Modul „WinDualShock“ (Silent Hill Townfall, Silent Hill f, Jedi Survivor, Bodycam) richtet für
einen per Kabel oder USB-Dongle verbundenen DualSense Ton und Haptik über XAudio2 ein. Unter Wine findet es
die Soundkarte des Controllers nicht und übergibt eine leere Geräte-Kennung; Wines XAudio2 übergeht die
Kennung („TODO“ in dlls/xaudio2_7/xaudio_dll.c), prüft das Ergebnis von FAudio nicht und meldet Erfolg,
obwohl die 4-Kanal-Ausgabe fehlt. Der nächste Aufruf (CreateSourceVoice) stürzt ab. Microsofts XAudio2
meldet den Fehler, das Modul lässt Ton und Haptik weg, das Spiel läuft.

Herkunft: NuGet-Paket Microsoft.XAudio2.Redist (VERSION, SHA-512 wie im NuGet-Katalog), daraus die
64-Bit-DLL. Einmal geladen nach <data>/tools/xaudio2/<VERSION>/, vor jedem Start (Vorab-Befehl,
bin/couch-native-xaudio2 <ordner>) als xaudio2_9.dll neben die Programmdatei gelegt. Eine fremde
xaudio2_9.dll im Spielordner wird nie überschrieben. Scheitert etwas, startet das Spiel trotzdem.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import urllib.request
import zipfile

VERSION = "1.2.13"
URL = ("https://api.nuget.org/v3-flatcontainer/microsoft.xaudio2.redist/%s/microsoft.xaudio2.redist.%s.nupkg"
       % (VERSION, VERSION))
SHA512 = "LSpgXNoi0sbnkY1Sy2c8sLT058K0tu4+H5iEMfXLb5RaF5iFdOD6ypRl/ENwsiLp6OIyFVJfPWtcJ2sePcRHbg=="
MEMBER = "build/native/release/bin/x64/xaudio2_9redist.dll"
DLL_NAME = "xaudio2_9.dll"
TIMEOUT_S = 12.0               # der Vorab-Befehl darf höchstens 15 s dauern (proc/reaper)


def _download(url: str, timeout: float = TIMEOUT_S) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "PC5-Launcher"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (feste https-Adresse)
        return resp.read(64 * 1024 * 1024)


def cached_dll(data_dir: str) -> str:
    return os.path.join(data_dir, "tools", "xaudio2", VERSION, "xaudio2_9redist.dll")


def ensure_cached(data_dir: str, fetch=_download) -> str | None:
    """Pfad der geprüften DLL; lädt sie beim ersten Mal (Prüfsumme des Pakets). None bei Fehlern."""
    path = cached_dll(data_dir)
    if os.path.isfile(path):
        return path
    try:
        blob = fetch(URL)
        if base64.b64encode(hashlib.sha512(blob).digest()).decode() != SHA512:
            print("xaudio2: Paket mit falscher Prüfsumme, nicht benutzt", file=sys.stderr)
            return None
        dll = zipfile.ZipFile(io.BytesIO(blob)).read(MEMBER)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(dll)
        os.replace(tmp, path)
        return path
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as e:
        print("xaudio2: Laden gescheitert: %s" % e, file=sys.stderr)
        return None


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def place(game_dir: str, data_dir: str, fetch=_download) -> str:
    """xaudio2_9.dll in game_dir: "placed" | "present" | "foreign" (fremde DLL, bleibt) | "missing"."""
    target = os.path.join(game_dir, DLL_NAME)
    source = ensure_cached(data_dir, fetch)
    if source is None:
        return "missing"
    if os.path.lexists(target):
        return "present" if os.path.isfile(target) and _digest(target) == _digest(source) else "foreign"
    tmp = target + ".pc5-neu"
    with open(source, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.replace(tmp, target)
    return "placed"


def main(argv: list[str]) -> int:
    from .. import paths
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        print("Aufruf: couch-native-xaudio2 <ordner der programmdatei>", file=sys.stderr)
        return 0
    try:
        state = place(argv[1], paths.data_dir())
    except OSError as e:
        state = "Fehler: %s" % e
    print("xaudio2 (Microsoft %s) in %s: %s" % (VERSION, argv[1], state), file=sys.stderr)
    return 0                     # nie den Start aufhalten


if __name__ == "__main__":
    sys.exit(main(sys.argv))
