"""Windows-Spiele mit umu-run und Proton-CachyOS (Bauplan 4.8, E10 bis E12; 03 Abschnitt 5.3).

Aus einem Eintrag der Quelle "proton" (Lutris) oder "local" mit platform "windows" wird eine
LaunchSpec: argv ["umu-run", exe, *args], Umgebungsblock, Prefix nach E10. Gestartet wird nichts.

Blockierend: build() mit check=True und preflight() prüfen Dateien und lesen /proc (übrig
gebliebener Wineserver). Meist wenige Millisekunden, auf einer schlafenden USB-Platte länger;
deshalb im Thread aufrufen. build(check=False) liest nur den Proton-Ordner und den Prefix-Ordner.
Für Spiele über einen Launcher (E94) macht build(check=True) vorher Platz im Prefix (launchers.before_start,
bis etwa eine Minute), für Epic-Spiele lädt es legendary nach, wenn es fehlt (EPIC_TOOL; E99).

Stand: Der Umgebungsblock ist aus den Quellen von umu 1.4.4 und Proton hergeleitet (03); so starten
seit E55, E74 und E77 viele Spiele am Fernseher. Auf SIGTERM endet umu-run samt Spiel (in echten
Läufen mit Rückgabewert 241).

E101: Nach der Wahl des Builds prüft der Plan ihn mit proton_repair.check (zwischengespeichert, beim
echten Start frisch). Beschädigt: dieses Mal mit proton_repair.healthy_alternative starten (nichts wird
umgestellt), Hinweis in die Start-Hinweise, bei jedem echten Lauf (Start, auch nur die Umgebung für einen
Installer oder Launcher) PROTON_DAMAGED rufen (die Verdrahtung stellt die Reparatur in die Warteschlange
und meldet sich); ohne heile Alternative ein LaunchError. Vorbeugung: kein Start, solange im Prefix ein
Wine-Prozess läuft, auch ein fremder (prefix.wine_processes); hält ein anderes Spiel der Konsole den
Prefix, muss es dieselbe Proton-Fassung nutzen. prefix_free() bietet dieselbe Prüfung den Verdrahtungen,
die umu-run selbst starten (Installer, Launcher).
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import threading
import time
from collections import OrderedDict

from .. import log
from . import prefix as prefixes
from .native import clean_env, expand, local_exe, require_exe, split_args
from .prefix import TEXT_WINESERVER, AdoptionRequired, wineserver_pids
from .spec import STEAM_LEFTOVERS, LaunchError, LaunchSpec, pad_mode

_log = log.get("launch.proton")

# Version der FSR-4-Treiber-DLL (amdxcffx64.dll). Zusammen mit dem Proton-Build pflegen: mit einer
# festen Version lädt Proton nicht von selbst eine neuere nach (03 Abschnitt 5.3).
FSR4_VERSION = "4.1.1"

# Reste eines Steam- oder Lutris-Elternprozesses: die gemeinsame Liste aus spec.py (E99)
LEFTOVERS = STEAM_LEFTOVERS

# Setzen wir nie; ein geerbter Wert wird entfernt. PROTON_FSR3_UPGRADE und PROTON_ENABLE_HDR
# wertet dieser Build nicht aus, ENABLE_HDR_WSI gehört zu einer nicht installierten Schicht,
# SteamDeck=1 stellt Spiele auf 1280x800, UMU_USE_STEAM startet über steam.exe.
NEVER_SET = ("PROTON_FSR3_UPGRADE", "PROTON_ENABLE_HDR", "ENABLE_HDR_WSI", "SteamDeck",
             "UMU_USE_STEAM")

# Schalter, die je nach Option gesetzt oder entfernt werden, samt ihrer Zweitnamen in Proton:
# ein geerbter Wert darf die Wahl je Spiel nicht aushebeln.
SWITCHES = ("PROTON_FSR4_UPGRADE", "FSR4_UPGRADE", "PROTON_PREFER_SDL", "PROTON_USE_SDL",
            "PROTON_USE_WAYLAND", "PROTONFIXES_DISABLE", "STORE", "DXVK_NO_HDR", "PROTON_USE_OPTISCALER")

# Gehören zur Prefix- und Proton-Wahl; weder Lutris' env noch die Option env dürfen sie ändern
# (ein anderer WINEPREFIX liefe an Übernahme und Wineserver-Prüfung vorbei).
PROTECTED = ("WINEPREFIX", "PROTONPATH", "GAMEID", "PROTON_VERB")

# Laufzeit, die ein Proton-Build laut toolmanifest.vdf verlangt -> Ordner unter ~/.local/share/umu
_RUNTIMES = {"1391110": "steamrt2", "1628350": "steamrt3", "4183110": "steamrt4"}
_GAMEID = re.compile(r"^umu-[A-Za-z0-9._-]+$")
_CACHYOS = re.compile(r"^proton-cachyos-")

# Epic (E73, E99): Stellt legendary bereit, bevor ein Epic-Spiel startet (fn() -> Pfad, wirft mit einem
# deutschen Satz; lädt beim ersten Mal ~3 MB). Nach einem Release mit neuer legendary-Fassung fehlte die
# Datei, bis man den Store öffnete, und das Spiel endete sofort mit „… bevor ein Bild kam“. Setzt die
# Verdrahtung (wiring_launchers.extend über wiring.epic_legendary); None = keine Prüfung.
EPIC_TOOL = None

# E101: Ein echter Lauf fand den Proton-Build beschädigt. fn(name: str, tool_dir: str, result: dict) -> bool,
# result wie proton_repair.check() plus "alternative" (Ordnername, mit dem dieses Mal gestartet wird, "" = keiner)
# und "note" („Dieses Mal startet das Spiel mit …“, "" ohne Alternative; für die Mitteilung). Rückgabe True, wenn
# eine Reparatur eingestellt ist oder schon läuft: Nur dann sagt der Start „wird neu geladen“. Darf nicht
# blockieren; Ausnahmen werden hier nur mit ihrer Art protokolliert. Setzt die Verdrahtung (wiring_proton:
# Reparatur in die Proton-Warteschlange, origin "launch"); None = nur ausweichen.
PROTON_DAMAGED = None

HEALTH_TTL_S = 60.0       # so lange gilt eine Prüfung im Plan (der echte Start prüft immer frisch)
SETTLE_S = 3.0            # so lange darf beim echten Start ein Rest ohne Wineserver (umu-run, Skript) noch enden

# „fremd“ heißt: nicht vom Home bzw. unserem reaper gestartet. Das sind meist Steam oder Lutris am Desktop,
# aber auch ein Installer oder Launcher der Konsole, der einen Neustart des Home überlebt hat (eigene Sitzung)
TEXT_FOREIGN_WINE = ("Im Prefix dieses Spiels läuft gerade ein Wine-Programm, das die Konsole nicht selbst gestartet "
                     "hat (zum Beispiel aus Steam oder Lutris am Desktop oder von vor einem Neustart des PC5 "
                     "Launchers). Schließe es zuerst oder warte, bis es endet: Zwei Starts zugleich in einem Prefix "
                     "können Proton beschädigen.")
TEXT_OTHER_BUILD = ("Im selben Prefix läuft noch ein Spiel mit %s, dieses startet mit %s. Schließe das andere "
                    "Spiel zuerst: Zwei Proton-Versionen zugleich in einem Prefix können Proton beschädigen.")
TEXT_DAMAGED_SWITCH = "%s ist beschädigt und wird im Hintergrund neu geladen. Dieses Mal startet das Spiel mit %s."
TEXT_DAMAGED_SWITCH_ONLY = "%s ist beschädigt. Dieses Mal startet das Spiel mit %s."
TEXT_DAMAGED_WAIT = "%s ist beschädigt und wird gerade neu geladen. Versuch es gleich noch mal."
TEXT_DAMAGED_NONE = ("%s ist beschädigt, und es gibt keine heile Proton-Version, mit der das Spiel stattdessen "
                     "starten könnte.")
TEXT_THIS_TIME = "Dieses Mal startet das Spiel mit %s."


# ---------------------------------------------------------------------------------------------
# Proton-Build

def _version_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name))


def newest_cachyos(compat_dir: str) -> str | None:
    """Neuester proton-cachyos-Ordner mit vorhandener Datei "proton" (voller Pfad) oder None."""
    try:
        names = [n for n in os.listdir(compat_dir) if _CACHYOS.match(n)]
    except OSError:
        return None
    names = [n for n in names if os.path.isfile(os.path.join(compat_dir, n, "proton"))]
    if not names:
        return None
    return os.path.join(compat_dir, max(names, key=_version_key))


def _default_proton(ctx) -> str:
    """Standard-Proton der Einstellungen (Zeile „Standard-Proton“ auf „Automatisch“): Die
    Verdrahtung steckt dafür ctx.default_proton ein (wiring_settings.resolve_proton). Fehlt die
    Abfrage, scheitert sie oder nennt sie einen Build, den es nicht gibt, gilt die eigene Suche."""
    ask = getattr(ctx, "default_proton", None)
    if not callable(ask):
        return ""
    try:
        name = str(ask() or "").strip()
    except Exception:                      # kommt von außen (anderer Baustein)
        return ""
    if not name:
        return ""
    path = expand(name, ctx.home)
    if not os.path.isabs(path):
        if os.sep in name or name in (".", ".."):
            return ""
        path = os.path.join(ctx.compat_tools_dir(), name)
    return path if os.path.isfile(os.path.join(path, "proton")) else ""


def find_proton(entry, ctx) -> str:
    """Voller Pfad des Proton-Builds: Option "proton" des Spiels, sonst config "games.proton",
    sonst das Standard-Proton der Einstellungen (ctx.default_proton), sonst der neueste
    proton-cachyos. Ein Name ohne Pfad wäre für umu ein Download-Auftrag (03 1.2)."""
    compat = ctx.compat_tools_dir()
    name = str(entry.option("proton") or ctx.config.get("games.proton", "") or "").strip()
    if name:
        path = expand(name, ctx.home)
        if not os.path.isabs(path):
            if os.sep in name or name in (".", ".."):
                raise LaunchError("Proton wurde nicht gefunden: %s" % name, reason="proton-missing")
            path = os.path.join(compat, name)
        if not os.path.isfile(os.path.join(path, "proton")):
            raise LaunchError("Proton wurde nicht gefunden: %s" % path, reason="proton-missing")
        return path
    path = _default_proton(ctx) or newest_cachyos(compat)
    if not path:
        raise LaunchError("Proton wurde nicht gefunden: kein proton-cachyos unter %s" % compat,
                          reason="proton-missing")
    return path


def required_runtime(protonpath: str) -> str | None:
    """Ordnername der Laufzeit unter ~/.local/share/umu, die dieser Build verlangt."""
    try:
        with open(os.path.join(protonpath, "toolmanifest.vdf"), encoding="utf-8",
                  errors="replace") as fh:
            found = re.search(r'"require_tool_appid"\s+"(\d+)"', fh.read())
    except OSError:
        return None
    return _RUNTIMES.get(found.group(1)) if found else None


# ---------------------------------------------------------------------------------------------
# Zustand des Builds (E101)

_health: dict = {}                 # realpath -> (Schlüssel, Zeitpunkt, Ergebnis)
_health_lock = threading.Lock()    # Pläne rechnen mehrere Arbeits-Threads


def _health_key(real: str, manifest_dir: str) -> tuple | None:
    """Was eine Prüfung ungültig macht: anderer Ordner (Reparatur setzt einen neuen ein), Änderung in
    files/lib/wine (neue, entfernte Einträge) oder am Manifest. Überschreiben an Ort und Stelle ändert
    keine Ordnerzeit; dafür gibt es HEALTH_TTL_S und die frische Prüfung beim echten Start."""
    try:
        st = os.stat(real)
    except OSError:
        return None
    key = [st.st_dev, st.st_ino]
    for path in (os.path.join(real, "files", "lib", "wine"),
                 os.path.join(manifest_dir, os.path.basename(real) + ".json") if manifest_dir else ""):
        try:
            key.append(os.stat(path).st_mtime_ns if path else None)
        except OSError:
            key.append(None)
    return tuple(key)


def proton_health(path: str, *, fresh: bool = False) -> dict | None:
    """proton_repair.check(path, manifest_dir()) mit Zwischenspeicher je Build (HEALTH_TTL_S, Schlüssel
    _health_key). fresh=True prüft immer neu (echter Start, ~8 ms). None, wenn die Prüfung selbst
    scheitert: Dann gilt der Build wie bisher als brauchbar. Blockierend (stat), im Arbeits-Thread."""
    from .. import proton_repair
    real = os.path.realpath(path)
    try:
        manifest_dir = proton_repair.manifest_dir()
    except Exception:                      # Orte kommen aus paths (Käfig, Home)
        manifest_dir = ""
    key = _health_key(real, manifest_dir)
    now = time.monotonic()
    if not fresh and key is not None:
        with _health_lock:
            hit = _health.get(real)
        if hit is not None and hit[0] == key and now - hit[1] < HEALTH_TTL_S:
            return hit[2]
    try:
        result = proton_repair.check(real, manifest_dir or None)
    except Exception:                      # die Prüfung darf keinen Start kippen
        _log.exception("Proton-Prüfung %s", real)
        return None
    with _health_lock:
        _health[real] = (key, now, result)
    return result


def health_clear() -> None:
    """Zwischenspeicher leeren (Tests, nach einer Reparatur)."""
    with _health_lock:
        _health.clear()
        _alt_cache.clear()


def is_damaged(result: dict | None) -> bool:
    """Nur ein echter Befund zählt: überschriebene, abweichende oder fehlende Dateien laut Manifest bzw.
    Zeitstempel. Fehlt files/lib/wine ganz (fremder Aufbau, Attrappen), bleibt es beim bisherigen Weg."""
    return (bool(result) and not result.get("ok") and result.get("method") in ("manifest", "zeitstempel")
            and int(result.get("files") or 0) > 0)


def build_label(name: str) -> str:
    """„Proton-CachyOS 11.0-20261015“ statt des Ordnernamens."""
    try:
        from .. import proton_repair
        label = str(proton_repair.label(name) or name)      # gleiche Benennung wie die Einstellungen
    except Exception:
        label = name
    return label if "proton" in label.lower() else "Proton " + label


_alt_cache: dict = {}              # (compat, kaputter Name) -> (Schlüssel, Zeitpunkt, Pfad)


def _alt_key(compat: str, broken: str, manifest_dir: str) -> tuple | None:
    """Was die Wahl der Alternative ungültig macht: neue oder entfernte Versionen (Zeit von compat), ein
    Manifest dazu (Zeit des Manifest-Ordners); der Zustand der Kandidaten selbst über HEALTH_TTL_S."""
    try:
        key = [os.stat(compat).st_mtime_ns, broken]
    except OSError:
        return None
    try:
        key.append(os.stat(manifest_dir).st_mtime_ns if manifest_dir else None)
    except OSError:
        key.append(None)
    return tuple(key)


def _alternative(ctx, broken: str, *, fresh: bool = False) -> str:
    """Voller Pfad einer heilen anderen Proton-Version (proton_repair.healthy_alternative) oder "".
    Zwischengespeichert wie proton_health (prüft sonst je Plan alle Builds); fresh=True beim echten Start."""
    from .. import proton_repair
    compat = ctx.compat_tools_dir()
    try:
        mdir = proton_repair.manifest_dir()
    except Exception:                      # Orte kommen aus paths (Käfig, Home)
        mdir = ""
    key = _alt_key(compat, broken, mdir)
    now = time.monotonic()
    if not fresh and key is not None:
        with _health_lock:
            hit = _alt_cache.get((compat, broken))
        if hit is not None and hit[0] == key and now - hit[1] < HEALTH_TTL_S:
            return hit[2]
    try:
        name = proton_repair.healthy_alternative(compat, broken, mdir or None)
    except Exception:
        _log.exception("Ausweich-Proton für %s", broken)
        return ""
    path = os.path.join(compat, name) if name else ""
    if path and not (os.path.isfile(os.path.join(path, "proton"))
                     and os.path.isfile(os.path.join(path, "toolmanifest.vdf"))):
        path = ""
    if key is not None:
        with _health_lock:
            _alt_cache[(compat, broken)] = (key, now, path)
    return path


def _report_damaged(name: str, tool_dir: str, result: dict, alternative: str) -> bool:
    """PROTON_DAMAGED rufen; True, wenn die Verdrahtung eine Reparatur eingestellt hat oder eine läuft."""
    fn = PROTON_DAMAGED
    if fn is None:
        return False
    alt = os.path.basename(alternative) if alternative else ""
    note = TEXT_THIS_TIME % build_label(alt) if alt else ""
    try:
        return fn(name, tool_dir, dict(result, alternative=alt, note=note)) is True
    except Exception as e:                 # kommt von außen (Verdrahtung); der Start geht weiter
        _log.warning("Reparatur von %s ließ sich nicht anstoßen (%s)", name, type(e).__name__)
        return False


def _heal(p, ctx, start: bool, report: bool = False) -> None:
    """E101: Ist der gewählte Build beschädigt, startet das Spiel dieses Mal mit einer heilen
    Alternative (p.protonpath, Hinweis in p.notes). Nur ein echter Start (start=True) prüft frisch. Jeder
    echte Lauf (report=True: Start, auch nur die Umgebung für Installer und Launcher) meldet den Schaden
    über PROTON_DAMAGED, auch wenn es keine Alternative gibt (dann LaunchError). „Wird neu geladen“ sagt
    der Satz nur, wenn der Haken eine Reparatur bestätigt; ein Plan (ohne Haken-Aufruf) nimmt an, dass
    die Verdrahtung repariert, sobald es sie gibt."""
    result = proton_health(p.protonpath, fresh=start)
    if not is_damaged(result):
        return
    name = os.path.basename(os.path.realpath(p.protonpath))
    alternative = _alternative(ctx, name, fresh=start)
    if report:
        _log.info("Proton %s beschädigt (%s); dieses Mal: %s", name, result.get("reason"),
                  os.path.basename(alternative) or "kein Start")
        repairing = _report_damaged(name, p.protonpath, result, alternative)
    else:
        repairing = PROTON_DAMAGED is not None
    if alternative:
        note = (TEXT_DAMAGED_SWITCH if repairing else TEXT_DAMAGED_SWITCH_ONLY) % (
            build_label(name), build_label(os.path.basename(alternative)))
    else:
        note = (TEXT_DAMAGED_WAIT if repairing else TEXT_DAMAGED_NONE) % build_label(name)
    if not alternative:
        raise LaunchError(note, str(result.get("reason") or ""), reason="proton-damaged")
    p.damaged = {"name": name, "path": p.protonpath, "alternative": alternative,
                 "reason": str(result.get("reason") or "")}
    p.protonpath = alternative
    p.notes.append(note)


# ---------------------------------------------------------------------------------------------
# Übersetzung der Lutris-Felder

def overrides_env(overrides) -> str:
    """Lutris-Schreibweise {dll: native|builtin|disabled|n,b|…} -> WINEDLLOVERRIDES.

    Gleiche Bündelung wie Lutris (util/wine/wine.py, get_overrides_env), aber ohne dessen
    Zusatz für winemenubuilder: den regelt Proton selbst.
    """
    buckets = OrderedDict((key, []) for key in ("n,b", "b,n", "b", "n", "d", ""))
    for dll, mode in (overrides or {}).items():
        mode = str(mode or "").replace(" ", "")
        mode = mode.replace("builtin", "b").replace("native", "n").replace("disabled", "")
        if mode in buckets:
            buckets[mode].append(str(dll))
    return ";".join("%s=%s" % (",".join(sorted(dlls)), mode)
                    for mode, dlls in buckets.items() if dlls)


def _is_off(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("false", "off", "no", "0")
    return not value


def _store(entry) -> str:
    """STORE für umu/Proton. battlenet schaltet in Proton WINE_SIMULATE_WRITECOPY ein (03 1.2)."""
    d = entry.detail
    store = str(d.get("store") or "").strip().lower()
    if store:
        return store
    slug = str(d.get("slug") or "").lower()
    exe = os.path.basename(str(d.get("exe") or "")).lower()
    if slug in ("battle-net", "battlenet") or exe.startswith("battle.net"):
        return "battlenet"
    return ""


def _gameid(entry, notes: list) -> str:
    raw = str(entry.option("gameid") or "umu-default").strip()
    if raw.isdigit():
        raw = "umu-" + raw
    if not _GAMEID.match(raw):
        notes.append("Die Spielkennung „%s“ ist ungültig; es gilt umu-default." % raw)
        return "umu-default"
    return raw


def _prelaunch(entry, ctx, prefix: str, notes: list) -> list:
    command = entry.detail.get("prelaunch_command") if entry.source == "proton" else None
    argv = split_args(command)
    if not argv:
        return []
    if os.path.basename(argv[0]) == "tombraider-display-sync":
        # Das Nutzer-Skript kennt nur seinen fest eingetragenen Prefix; unsere Fassung bekommt den
        # Prefix dieses Starts (nach einer Übernahme ist das die Kopie).
        return [[ctx.bin("couch-display-sync"), prefix]]
    argv[0] = expand(argv[0], ctx.home)
    if os.path.isabs(argv[0]) and not os.path.isfile(argv[0]):
        notes.append("Der Vorab-Befehl aus Lutris fehlt und wird ausgelassen: %s" % argv[0])
        return []
    return [argv]


def global_switch(ctx, key: str) -> bool:
    """Schalter aus Einstellungen › Spiele; fehlt er oder ist er kein Wahrheitswert, gilt Ein."""
    try:
        value = ctx.config.get(key, True)
    except Exception:                      # die Einstellungen kommen von außen (Stellvertreter)
        return True
    return value if isinstance(value, bool) else True


# Sonys Pad-Bibliothek: Liegt sie im Spielordner, liest das Spiel den Controller selbst (PC-Fassungen
# von Konsolenspielen; Tasten-Symbole, Vibration und Auslöser nur so). E74
NATIVE_PAD_LIB = re.compile(r"^libscepad.*\.dll$", re.I)


def native_pad(*folders: str, depth: int = 2, limit: int = 5000) -> bool:
    """Bringt das Spiel eine eigene Unterstützung für den Controller mit (NATIVE_PAD_LIB)? Sucht nur
    wenige Ebenen tief und höchstens limit Einträge (der Start soll nicht warten). Blockierend."""
    seen = 0
    for top in folders:
        if not top or not os.path.isdir(top):
            continue
        base = top.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(top):
            if any(NATIVE_PAD_LIB.match(f) for f in files):
                return True
            seen += len(files) + len(dirs)
            if root.count(os.sep) - base >= depth or seen > limit:
                dirs[:] = []
    return False


# Programmdateien, die den DualSense beim Namen kennen, unterstützen ihn selbst (Alan Wake 2, Silent Hill
# Townfall: Touchpad, adaptive Trigger nur so; Wunsch 2026-10-20). Spiele, die nur XInput kennen (Hatred),
# nennen ihn nicht. Gesucht wird in der Startdatei und, bei Unreal-Spielen mit kleiner Startdatei, in der
# eigentlichen <Name>-Win64-Shipping.exe darunter
NATIVE_PAD_TEXT = (b"DualSense", "DualSense".encode("utf-16-le"))
# Sonys Controller-Modul für Unreal (Silent Hill Townfall, Silent Hill f, Jedi Survivor, Bodycam): richtet
# für einen Kabel-DualSense (auch USB-Dongle) Ton und Haptik über XAudio2 ein. Wines XAudio2 übergeht
# dabei die Geräte-Kennung und meldet Erfolg, obwohl die Ausgabe fehlt; Townfall stürzte darauf in
# CreateSourceVoice ab (E77). Abhilfe: Microsofts XAudio2 (launch/xaudio.py)
SONY_AUDIO_TEXT = (b"WinDualShock",)
SCAN_MAX_BYTES = 1 << 30
SCAN_CHUNK = 16 << 20


def file_contains(path: str, needles: tuple = NATIVE_PAD_TEXT) -> bool:
    """Steht einer der Texte in der Datei? Liest in Stücken (mit Überlappung). Blockierend."""
    try:
        if os.path.getsize(path) > SCAN_MAX_BYTES:
            return False
        overlap = max(len(t) for t in needles) - 1
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(SCAN_CHUNK)
                if not chunk:
                    return False
                buf = tail + chunk
                if any(t in buf for t in needles):
                    return True
                tail = buf[-overlap:]
    except OSError:
        return False


def exe_knows_pad(path: str) -> bool:
    return file_contains(path, NATIVE_PAD_TEXT)


def game_binaries(exe: str) -> list[str]:
    """Die Startdatei und Unreals *-Shipping.exe bis zwei Ebenen darunter (<Name>/Binaries/Win64)."""
    out = [exe] if exe and os.path.isfile(exe) else []
    base = os.path.dirname(exe) if exe else ""
    for pattern in ("*/Binaries/Win64/*-Shipping.exe", "Binaries/Win64/*-Shipping.exe"):
        out += sorted(glob.glob(os.path.join(glob.escape(base), pattern)))[:3] if base else []
    return list(dict.fromkeys(out))


def knows_pad_cached(paths: list[str], cache_file: str) -> bool:
    """exe_knows_pad über mehrere Dateien, gemerkt je Datei (Größe, Änderungszeit) in cache_file:
    eine große Programmdatei wird nur beim ersten Start durchsucht."""
    return bool(scan_cached(paths, cache_file))


def scan_cached(paths: list[str], cache_file: str, tag: str = "dualsense", needles: tuple = NATIVE_PAD_TEXT) -> str:
    """Die erste Datei aus paths, in der einer der Texte steht ("" = keine). Gemerkt je Datei und Suche
    (Schlüssel "<tag>|<pfad>", bei der DualSense-Suche nur der Pfad) nach Größe und Änderungszeit."""
    cache: dict = {}
    if cache_file:
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh)
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
    found, changed = "", False
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = [st.st_size, st.st_mtime_ns]
        name = path if tag == "dualsense" else "%s|%s" % (tag, path)
        hit = cache.get(name)
        if isinstance(hit, list) and hit[:2] == key and len(hit) == 3:
            known = bool(hit[2])
        else:
            known = file_contains(path, needles)
            cache[name] = key + [known]
            changed = True
        if known:
            found = path
            break
    if changed and cache_file:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            tmp = cache_file + ".neu"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(tmp, cache_file)
        except OSError:
            pass
    return found


def scan_cache_file(ctx) -> str:
    state = getattr(ctx, "state_dir", "") or ""
    return os.path.join(state, "pad-scan.json") if state else ""      # nie in den Spielordner schreiben


def sony_audio_binary(exe: str, ctx) -> str:
    """Die Programmdatei mit Sonys Controller-Modul (SONY_AUDIO_TEXT) oder ""."""
    return scan_cached(game_binaries(exe), scan_cache_file(ctx), "sony-audio", SONY_AUDIO_TEXT)


def controller(entry, ctx, exe: str = "") -> str:
    """Wie das Spiel den Controller bekommt (E74, Wunsch 2026-10-19: „Im Spiel Hatred sieht man, dass es
    Optionen für einen Controller gibt. Das Spiel erkennt ihn aber nicht“):
      "xbox"    PROTON_PREFER_SDL: Wine meldet ihn als Xbox-Controller; das verstehen alle Spiele mit
                XInput (die meisten, Unreal Engine 4 usw.), sie zeigen dann Xbox-Symbole.
      "direct"  hidraw (Protons Vorgabe ohne Steam): Das Spiel bekommt das Gerät selbst, mit Touchpad,
                adaptiven Triggern und Symbolen. Nur Spiele mit eigener Unterstützung (native_pad,
                knows_pad_cached) sehen es, alle anderen gar keinen Controller.
    Option pad je Spiel: "auto" (Vorgabe) wählt danach, solange games.pad_auto an ist. Über den
    GuliKit-Dongle (USB) gilt dasselbe: Seine fehlenden L2/R2-Tasten-Bits ergänzt ein HID-BPF-Filter
    (tools/hid-bpf), den Absturz von Townfall verhindert Microsofts XAudio2 (E77)."""
    pad = entry.option("pad")
    if pad in ("xbox", "direct"):
        return pad
    if pad in ("kbm", "mouse") or entry.options.get("prefer_sdl") is False:    # alte Einstellung je Spiel (bis E73)
        return "direct"
    if pad_mode(entry) == "cards":
        return "direct"             # Spiel nur mit Maus (E97): kein Xbox-Pad neben dem Zeiger
    if not global_switch(ctx, "games.pad_auto"):
        return "direct"
    folders = [os.path.dirname(exe)] if exe else []
    if entry.source == "local" and entry.detail.get("install_dir"):
        folders.append(str(entry.detail["install_dir"]))
    if native_pad(*folders):
        return "direct"
    return "direct" if knows_pad_cached(game_binaries(exe), scan_cache_file(ctx)) else "xbox"


def build_env(entry, ctx, prefix: str, protonpath: str, notes: list | None = None, exe: str = "") -> tuple:
    """(env, unset) für einen Start. Reihenfolge, spätere gewinnen: Lutris' env, übersetzte
    Lutris-Schalter, unser Block (03 5.3), zuletzt die Option env des Spiels."""
    notes = notes if notes is not None else []
    d = entry.detail if entry.source == "proton" else {}
    env = {}

    for key, val in clean_env(d.get("env")).items():
        if key in PROTECTED or key in NEVER_SET:
            continue
        env[key] = val
    overrides = overrides_env(d.get("dll_overrides"))
    if overrides:
        env["WINEDLLOVERRIDES"] = ";".join(p for p in (overrides, env.get("WINEDLLOVERRIDES")) if p)
    if _is_off(d.get("esync")):
        env["PROTON_NO_ESYNC"] = "1"
    if _is_off(d.get("fsync")):
        env["PROTON_NO_FSYNC"] = "1"
    locale = str(d.get("locale") or ctx.config.get("games.locale", "") or "")
    if locale:
        # Proton setzt LC_ALL aus HOST_LC_ALL neu (proton 1669 ff.), deshalb beide
        env["LC_ALL"] = locale
        env["HOST_LC_ALL"] = locale

    gameid = _gameid(entry, notes)
    env.update({
        "GAMEID": gameid,
        "PROTONPATH": protonpath,
        "WINEPREFIX": prefix,
        "PROTON_VERB": "waitforexitandrun",
        # Keine Laufzeit-Aktualisierung mitten im Spielstart (17 s gemessen), kurze Netz-Wartezeit
        "UMU_RUNTIME_UPDATE": "0",
        "UMU_HTTP_TIMEOUT": "3",
        "UMU_HTTP_RETRIES": "0",
        "PROTON_LOCAL_SHADER_CACHE": "1",
        # In Gamescope gibt es für Spiele nur Xwayland; HDR kommt über die WSI-Schicht
        "PROTON_ENABLE_WAYLAND": "0",
    })
    if gameid == "umu-default":
        # Für umu-default gibt es keine Fixes; so entfällt auch der Zenity-Dialog von protonfixes
        env["PROTONFIXES_DISABLE"] = "1"
    else:
        env.pop("PROTONFIXES_DISABLE", None)
    # Die Schalter unter Einstellungen › Spiele (games.fsr4, games.hdr; games.pad_auto in controller()) sind
    # die Vorgabe für alle Spiele; je Spiel lässt sich zusätzlich abschalten. Aus ist aus:
    # Ein globales Aus kann ein einzelnes Spiel nicht übersteuern.
    if entry.option("fsr4") == "off" or not global_switch(ctx, "games.fsr4"):
        # Nur FSR4_UPGRADE=0 schaltet ab; PROTON_FSR4_UPGRADE=0 ließe die Automatik an (03 2.2)
        env.pop("PROTON_FSR4_UPGRADE", None)
        env["FSR4_UPGRADE"] = "0"
    else:
        env.pop("FSR4_UPGRADE", None)
        env["PROTON_FSR4_UPGRADE"] = FSR4_VERSION
    # OptiScaler je Spiel (Wunsch 2026-10-24: „systemweit … für Spiele ein und aus schalten“): Proton-CachyOS
    # (ab 11.0-20260506) und GE-Proton 11 laden ihn selbst nach <prefix>/drive_c/windows/system32/umu und
    # schleusen ihn ein (protonfixes/upscalers.py); der Spielordner bleibt unberührt
    if entry.option("optiscaler") == "on":
        env["PROTON_USE_OPTISCALER"] = "1"
    else:
        env.pop("PROTON_USE_OPTISCALER", None)
    if controller(entry, ctx, exe) == "xbox":
        env["PROTON_PREFER_SDL"] = "1"
    else:
        env.pop("PROTON_PREFER_SDL", None)
    try:
        # hdr_active liest den Schnappschuss der Hauptschleife (wiring_launch), nie Gamescope selbst:
        # Hier läuft der Arbeits-Thread (E99). Ein fertiger Wert wird auch genommen
        active = ctx.hdr_active() if callable(ctx.hdr_active) else ctx.hdr_active
        hdr = entry.option("hdr") != "off" and global_switch(ctx, "games.hdr") and bool(active)
    except Exception:                      # der Schnappschuss kommt von außen (Verdrahter)
        hdr = False
    env["DXVK_HDR"] = "1" if hdr else "0"
    store = _store(entry)
    if store:
        env["STORE"] = store
    else:
        env.pop("STORE", None)
    for alias in ("PROTON_USE_WAYLAND", "PROTON_USE_SDL", "DXVK_NO_HDR"):
        env.pop(alias, None)               # Zweitnamen aus Lutris' env würden unsere Wahl kippen

    for key, val in clean_env(entry.option("env")).items():
        if key in PROTECTED:
            notes.append("Die Variable %s lässt sich je Spiel nicht ändern und wird ignoriert." % key)
            continue
        env[key] = val

    unset = []
    for name in LEFTOVERS + NEVER_SET + SWITCHES:
        if name in env or name in unset:
            continue
        if "*" in name and any(fnmatch.fnmatchcase(key, name) for key in env):
            continue          # Muster träfe eine gesetzte Variable: die Einzelnamen genügen
        unset.append(name)
    return env, unset


# ---------------------------------------------------------------------------------------------
# Plan, Prüfungen, LaunchSpec

class _Plan:
    def __init__(self):
        self.exe = ""
        self.args = []
        self.cwd = ""
        self.protonpath = ""
        self.prefix = None            # prefixes.PrefixPlan
        self.env = {}
        self.unset = []
        self.prelaunch = []
        self.notes = []               # Hinweise aus dem Rechnen (deutsche Sätze)
        self.damaged = None           # E101: {name, path, alternative, reason}, wenn ausgewichen wird


def game_paths(entry, ctx, notes: list | None = None) -> tuple:
    """(exe, args, cwd) für Lutris- und Archiv-Spiele. Rechnet nur."""
    notes = notes if notes is not None else []
    d = entry.detail
    if entry.source == "local" and d.get("launcher_app"):
        # Der Launcher selbst, sichtbar (Anmeldung, Installationsdialog, E94)
        from ..store import launchers
        exe = launchers.exe_path(str(d.get("launcher_prefix") or ""), str(d.get("launcher")))
        return exe, [str(a) for a in launchers.PENDING_ARGS.get(entry.id, [])], os.path.dirname(exe)
    if entry.source == "local":
        exe, args, folder = local_exe(entry, ctx)
        cwd = os.path.dirname(exe) or folder
    else:
        exe = expand(d.get("exe") or "", ctx.home)
        if not exe:
            raise LaunchError("Für dieses Spiel ist keine Startdatei festgelegt.", reason="no-exe")
        workdir = expand(d.get("working_dir") or "", ctx.home)
        if not os.path.isabs(exe):
            if not workdir:
                raise LaunchError("Die Startdatei ist ohne Ordner angegeben: %s" % exe, reason="no-exe")
            exe = os.path.join(workdir, exe)
        args = split_args(d.get("args"))
        cwd = workdir or os.path.dirname(exe)
    args = args + split_args(entry.option("args"))
    return exe, args, cwd


def _plan(entry, ctx, start: bool = False, report: bool = False) -> _Plan:
    """start=True nur für den echten Start (build mit check=True, planning=False): frische Prüfung
    des Builds. report=True für jeden echten Lauf (build mit planning=False): PROTON_DAMAGED (E101)."""
    if entry.source == "local" and str(entry.detail.get("platform") or "") != "windows":
        raise LaunchError("Dieses Spiel ist kein Windows-Spiel.", "platform=%r"
                          % entry.detail.get("platform"), reason="no-launcher")
    p = _Plan()
    p.exe, p.args, p.cwd = game_paths(entry, ctx, p.notes)
    p.protonpath = find_proton(entry, ctx)
    _heal(p, ctx, start, report)
    p.prefix = prefixes.decide(entry, ctx)
    p.env, p.unset = build_env(entry, ctx, p.prefix.path, p.protonpath, p.notes, p.exe)
    p.prelaunch = _prelaunch(entry, ctx, p.prefix.path, p.notes)
    _native_xaudio(p, ctx)
    return p


def _native_xaudio(p: _Plan, ctx) -> None:
    """Spiel mit Sonys Controller-Modul, Controller direkt (E77): Microsofts XAudio2 statt Wines. Der
    Vorab-Befehl legt es neben die Programmdatei (launch/xaudio.py), die Umgebung lässt Wine es nehmen."""
    if p.env.get("PROTON_PREFER_SDL") == "1":
        return                                  # als Xbox-Controller richtet das Modul keinen Ton ein
    binary = sony_audio_binary(p.exe, ctx)
    if not binary:
        return
    p.env["WINEDLLOVERRIDES"] = ";".join(x for x in (p.env.get("WINEDLLOVERRIDES"), "xaudio2_9=n,b") if x)
    p.unset = [u for u in p.unset if u != "WINEDLLOVERRIDES"]
    p.prelaunch.append([ctx.bin("couch-native-xaudio2"), os.path.dirname(binary)])


def _writable(folder: str) -> bool:
    """Ist der Ordner beschreibbar bzw. lässt er sich anlegen (erster vorhandener Elternordner)?"""
    probe = os.path.abspath(folder)
    while not os.path.lexists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    return os.path.isdir(probe) and os.access(probe, os.W_OK | os.X_OK)


def _describe(found: list) -> str:
    return ", ".join("%d %s%s" % (x["pid"], x["comm"], " (fremd)" if x["own"] is False else "") for x in found)


UMU_RUN = "umu-run"


def umu_run(ctx=None) -> str | None:
    """Aufruf für umu-run (E104/E105, wie paths.tool): Liegt es im PATH (System-Paket, z. B.
    /usr/bin/umu-run), bleibt es beim Namen „umu-run“ (argv wie bisher); sonst die ZIP-App des
    Einrichtungs-Assistenten <data>/tools/umu/umu-run mit vollem Pfad (Verweis nicht aufgelöst);
    None, wenn keins da ist. ctx (LaunchContext): dessen which und data_dir, sonst shutil.which und
    paths.data_dir()."""
    import shutil
    which = getattr(ctx, "which", None) or shutil.which
    if which(UMU_RUN):
        return UMU_RUN
    data = getattr(ctx, "data_dir", "") or ""
    if not data:
        from .. import paths
        data = paths.data_dir()
    own = os.path.join(data, "tools", "umu", UMU_RUN)
    return own if os.path.isfile(own) and os.access(own, os.X_OK) else None


def prefix_free(prefix_path: str, protonpath: str = "", proc_root: str = "/proc", held: bool = False,
                settle: bool = True) -> None:
    """Vorbeugung E101: kein Start, solange im Prefix ein Wine-Prozess läuft, auch ein fremder
    (Desktop-Steam, Lutris). Zwei Proton-Fassungen zugleich auf einem Prefix haben am 2026-10-23
    einen Build zerstört: Wines Prefix-Abgleich schreibt durch die absoluten system32-Verweise in
    den Build, auf den der Prefix gerade zeigt. held: ein anderes Spiel der Konsole hält den Prefix
    (die Shell schließt es); dann muss es dieselbe Fassung nutzen (protonpath). settle (echter Start):
    Reste ohne Wineserver (umu-run, Protons Skript) dürfen bis SETTLE_S noch enden. Wirft LaunchError
    (reason "wineserver" bzw. "proton-mismatch"). Liest /proc, blockierend. Auch für Installer und
    Launcher, die die Verdrahtung selbst mit umu-run startet (wiring_store, wiring_launchers)."""
    found = prefixes.wine_processes(prefix_path, proc_root)
    if settle and not held:
        # nur eigene Reste ohne Wineserver bekommen die Frist; Fremdes und ein Wineserver sofort
        until = time.monotonic() + SETTLE_S
        if found and all(x["own"] is not False and not x["wineserver"] for x in found):
            # im Protokoll, worauf gewartet wird: Ein Helfer, der nie endet, ließe sich so am TV erkennen
            _log.info("Warte bis %.0f s auf Reste in %s: %s", SETTLE_S, prefix_path, _describe(found))
        while (found and all(x["own"] is not False and not x["wineserver"] for x in found)
               and time.monotonic() < until):
            time.sleep(0.2)
            found = prefixes.wine_processes(prefix_path, proc_root)
    if not found:
        return
    detail = "Wine-Prozesse in %s: %s" % (prefix_path, _describe(found))
    _log.info("Prefix belegt: %s", detail)
    if any(x["own"] is False for x in found):
        raise LaunchError(TEXT_FOREIGN_WINE, detail, reason="wineserver")
    if not held:
        # waitforexitandrun wartet sonst stumm auf den alten Wineserver (03 1.6, Wartefalle)
        raise LaunchError(TEXT_WINESERVER, detail, reason="wineserver")
    mine = os.path.realpath(protonpath)
    # nur ein Ordner, den es hier gibt: Ein Pfad, wie ihn nur ein Container sieht, gilt als unbekannt
    other = [x["build"] for x in found
             if x["build"] and os.path.isdir(x["build"]) and os.path.realpath(x["build"]) != mine]
    if other:
        raise LaunchError(TEXT_OTHER_BUILD % (build_label(os.path.basename(other[0].rstrip("/"))),
                                              build_label(os.path.basename(mine))),
                          detail + "; Proton %s statt %s" % (other[0], mine), reason="proton-mismatch")


def _prefix_free(p: _Plan, proc_root: str, held: bool, settle: bool) -> None:
    prefix_free(p.prefix.path, p.protonpath, proc_root, held, settle)


def _check(p: _Plan, entry, ctx, planning: bool, room=None, start: bool = False) -> list:
    """room(): macht vor der Wineserver-Prüfung Platz im Prefix eines Launchers (_make_room, erst
    nach den übrigen Prüfungen: Scheitert der Start ohnehin, bleibt der Launcher offen). True = den
    Prefix hält ein anderes Spiel der Konsole, das die Shell vor dem Start schließt: dessen
    Wineserver ist kein Rest, muss aber zur selben Proton-Fassung gehören (E101). start: echter
    Start (wartet kurz auf Reste im Prefix, _prefix_free)."""
    warnings = list(p.notes)
    if umu_run(ctx) is None:
        raise LaunchError("umu-run wurde nicht gefunden. Ohne umu-launcher starten keine Windows-Spiele.",
                          reason="umu-missing")
    if not os.path.isfile(os.path.join(p.protonpath, "proton")):
        raise LaunchError("Proton wurde nicht gefunden: %s" % p.protonpath, reason="proton-missing")
    if not os.path.isfile(os.path.join(p.protonpath, "toolmanifest.vdf")):
        raise LaunchError("Proton ist unvollständig (toolmanifest.vdf fehlt): %s" % p.protonpath,
                          reason="proton-missing")
    require_exe(entry, p.exe, ctx)
    if p.cwd and not os.path.isdir(p.cwd):
        warnings.append("Der Arbeitsordner fehlt (%s); gestartet wird im Ordner der Startdatei." % p.cwd)

    if p.prefix.adopt:
        src, dst = p.prefix.adopt
        if not planning:
            raise AdoptionRequired(src, dst)
        if wineserver_pids(src, ctx.proc_root):
            raise LaunchError(TEXT_WINESERVER, "Wineserver in %s" % src, reason="wineserver")
        warnings.append(prefixes.TEXT_ADOPT_HINT)
    if not _writable(p.prefix.path):
        raise LaunchError("Der Prefix-Ordner ist nicht beschreibbar: %s" % p.prefix.path,
                          reason="prefix-readonly")
    held = bool(room()) if room is not None else False
    _prefix_free(p, ctx.proc_root, held, settle=start)

    if not p.prefix.adopt and not os.path.isdir(p.prefix.path):
        warnings.append("Beim ersten Start richtet Proton den Prefix ein; das dauert etwas länger.")
    runtime = required_runtime(p.protonpath)
    if runtime and not os.path.isdir(os.path.join(ctx.home, ".local/share/umu", runtime)):
        warnings.append("Die Laufzeit für Proton (%s) fehlt noch; der erste Start lädt sie herunter."
                        % runtime)
    return warnings


def preflight(entry, ctx, *, planning: bool = False) -> list:
    """Vorab-Prüfungen. Rückgabe: Hinweise (deutsche Sätze). Harte Fehler als LaunchError:
    umu-run fehlt, PROTONPATH/proton oder toolmanifest.vdf fehlt, Startdatei fehlt bzw. Platte
    nicht eingehängt, Übernahme steht aus (AdoptionRequired), Prefix-Ordner nicht beschreibbar,
    Wine-Prozess im Prefix (auch ein fremder, E101), Proton beschädigt ohne heile Alternative
    (proton-damaged). planning=True meldet die ausstehende Übernahme nur als Hinweis. Ein
    beschädigter Build mit Alternative steht als Hinweis drin; PROTON_DAMAGED ruft nur build().
    Blockierend (Platte, /proc)."""
    return _check(_plan(entry, ctx), entry, ctx, planning)


def build(entry, ctx, *, check: bool = True, planning: bool = False) -> LaunchSpec:
    """Entry -> LaunchSpec für umu-run.

    check=True     führt die Vorab-Prüfungen aus (blockierend) und wirft LaunchError.
    planning=True  nur für Plan und Tests: liefert die Spec auch, solange die Übernahme des
                   Prefix noch aussteht. Sonst gibt es dann immer AdoptionRequired, damit nie
                   ein Spiel in einem frisch angelegten leeren Prefix ohne seine Stände startet.
    Nur check=True mit planning=False ist ein echter Start: frische Prüfung des Builds. Jedes
    planning=False meldet einen Schaden über PROTON_DAMAGED (E101; check=False nimmt dafür die Prüfung aus
    dem Zwischenspeicher). Auch sonst startet ein beschädigter Build nie (Alternative oder LaunchError).
    """
    start = check and not planning
    # auch check=False ohne planning ist ein echter Lauf mit umu-run (Installer, Launcher): Haken rufen
    p = _plan(entry, ctx, start=start, report=not planning)
    epic = str(entry.detail.get("epic") or "") if entry.source == "local" else ""
    launcher = str(entry.detail.get("launcher") or "") if entry.source == "local" else ""
    if check:
        # Ein echter Start (nicht Plan, nicht nur Umgebung): Platz im Prefix des Launchers (E99)
        room = (lambda: _make_room(entry, launcher)) if launcher and not planning else None
        _check(p, entry, ctx, planning, room, start=start)
        if epic and not planning:
            _epic_tool()
    elif p.prefix.adopt and not planning:
        raise AdoptionRequired(*p.prefix.adopt)
    cwd = p.cwd
    if not cwd or (check and not os.path.isdir(cwd)):
        cwd = os.path.dirname(p.exe)
    # E104: umu aus dem PATH (Name) oder die eigene ZIP-App (voller Pfad); ohne Prüfung bleibt es beim Namen
    argv_umu = umu_run(ctx) or UMU_RUN
    argv = [argv_umu, p.exe, *p.args]
    env = p.env
    if launcher:
        return _launcher_spec(entry, ctx, p, argv, env, cwd, launcher)
    if epic:
        # Epic-Spiel aus dem Store (E73): legendary holt die Anmeldung fürs Spiel und startet die
        # Startdatei mit umu-run in diesem Prefix (Umgebung wie bei jedem Windows-Spiel)
        from ..store import epic_account
        # Startoptionen der Spieleinstellungen hängen hinten an (E99, launch-proc-7); nur die Option
        # args, nicht detail.args: legendary bringt die Parameter des Spiels aus dem Manifest selbst mit
        argv = [ctx.bin("couch-epic-game"), epic, *split_args(entry.option("args"))]
        # COUCH_UMU_RUN: legendary ruft den Wrapper über den Namen; liegt umu nur als eigene ZIP-App vor
        # (<data>/tools/umu, nicht im PATH, E105), nimmt das Skript diesen Pfad
        env = {**env, "COUCH_LEGENDARY": os.environ.get("COUCH_LEGENDARY") or os.path.join(
            ctx.data_dir, "tools", "legendary-" + epic_account.VERSION),
            "LEGENDARY_CONFIG_PATH": os.path.join(ctx.data_dir, "epic"), "COUCH_UMU_RUN": argv_umu}
    return LaunchSpec(
        entry_id=entry.id,
        argv=argv,
        env=env,
        unset=p.unset,
        cwd=cwd,
        kind="game",
        freeze=bool(entry.option("freeze")),
        mouse=pad_mode(entry),
        polite_stop=None,             # SIGTERM an umu-run reicht es an alle Nachfahren weiter (03 1.6)
        prelaunch=p.prelaunch,
        title=entry.title,
    )


def _make_room(entry, launcher_id: str) -> bool:
    """Vor dem Start im Prefix eines Launchers (Arbeits-Thread): Die Verdrahtung schließt einen
    sichtbaren oder unsichtbaren Launcher ohne Auftrag oder lehnt ab, solange er lädt (E99; vorher
    scheiterte jeder solche Start an der Wineserver-Prüfung). Ohne Verdrahtung: nichts."""
    from ..store import launchers
    fn = launchers.before_start
    if fn is None:
        return False
    return bool(fn(launcher_id, entry.id, entry.title))


def _epic_tool() -> None:
    """legendary für ein Epic-Spiel bereitstellen (EPIC_TOOL); ein Fehler wird zum LaunchError."""
    fn = EPIC_TOOL
    if fn is None:
        return
    try:
        fn()
    except LaunchError:
        raise
    except Exception as e:                 # EpicError trägt schon einen deutschen Satz (Netz, Prüfsumme)
        raise LaunchError(str(getattr(e, "message", "") or e) or "Das Epic-Werkzeug (legendary) fehlt.",
                          "legendary: %s" % e, reason="tool-missing") from e


def _launcher_spec(entry, ctx, p: _Plan, argv: list, env: dict, cwd: str, launcher_id: str) -> LaunchSpec:
    """Battle.net, Ubisoft Connect, Rockstar (E94). Der Launcher selbst (launcher_app): sichtbar, Pad
    als Maus, nie angehalten (er lädt im Hintergrund weiter). Ein Spiel: bin/couch-launcher-game
    startet den Launcher verborgen, löst den Start aus und wartet auf das Spiel."""
    from .. import paths
    from ..store import launchers
    d = entry.detail
    prefix = str(d.get("launcher_prefix") or "")
    exe = launchers.exe_path(prefix, launcher_id)
    table = launchers.LAUNCHERS[launcher_id]
    env = {**env, "COUCH_OPEN_URL_DIR": paths.open_url_dir()}      # bin/couch-open-url (Anmeldung mit Google …)
    common = dict(entry_id=entry.id, env=env, unset=p.unset, prelaunch=p.prelaunch, title=entry.title,
                  polite_stop=None)
    if d.get("launcher_app"):
        launchers.PENDING_ARGS.pop(entry.id, None)       # gilt für genau diesen Start
        return LaunchSpec(argv=argv, cwd=os.path.dirname(exe), kind="tool", freeze=False, mouse=True,
                          start_timeout=240, **common)
    code, folder_win = str(d.get("launcher_code") or ""), str(d.get("folder_win") or "")
    argv = [ctx.bin("couch-launcher-game"), launcher_id, code, folder_win]
    # Was der Helfer je Launcher braucht, steht nur in store/launchers.py (E99)
    env = {**env, "COUCH_LAUNCHER_EXE": exe, "COUCH_LAUNCHER_NAME": table.name,
           "COUCH_LAUNCHER_READY": ",".join(table.ready_procs), "COUCH_LAUNCHER_SETTLE": str(table.ready_wait),
           "COUCH_LAUNCHER_AFTER": str(table.after_game),
           "COUCH_LAUNCHER_START": json.dumps(launchers.launch_args(launcher_id, code, folder_win)[1:])}
    common["env"] = env
    # freeze_needs_window: ohne Fenster des Spiels nicht anhalten. Solange nur der verborgene Launcher
    # läuft (Start, Nachlauf zum Cloud-Abgleich nach dem Spiel), hätte ein Anhalten ihn mitten im
    # Laden oder Abgleichen eingefroren (E99)
    return LaunchSpec(argv=argv, cwd=cwd, kind="game", freeze=bool(entry.option("freeze")), mouse=pad_mode(entry),
                      start_timeout=360, hide_windows=table.procs, freeze_needs_window=True, **common)


__all__ = ["FSR4_VERSION", "LEFTOVERS", "NEVER_SET", "SWITCHES", "PROTECTED", "TEXT_WINESERVER",
           "TEXT_FOREIGN_WINE", "TEXT_OTHER_BUILD", "TEXT_THIS_TIME", "PROTON_DAMAGED", "AdoptionRequired", "build",
           "build_env", "build_label", "find_proton", "game_paths", "health_clear", "is_damaged",
           "newest_cachyos", "overrides_env", "prefix_free", "preflight", "proton_health", "required_runtime",
           "wineserver_pids"]
