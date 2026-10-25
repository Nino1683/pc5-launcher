"""Prüft, ob dieses System den PC5 Launcher tragen kann (E104, Bauplan 7.4). Nur Standardbibliothek, liest nur.

  couch-home --check [--json]          (ruft diese Datei mit dem Python des Home)
  python3 -I depcheck.py [--json]

Jeder Punkt läuft in einem eigenen „python3 -I -c …“: Ein Absturz von gi oder einer C-Erweiterung trifft
nur diesen Punkt, nicht den Prüfer. Nichts wird installiert, nichts geschrieben, kein Netz.

Rückgabewert 0 = alle Pflichtteile da, 1 = mindestens ein Pflichtteil fehlt, 2 = falscher Aufruf.
Für Tests des Installers: PC5_DEPCHECK=<datei.json> liefert den Inhalt dieser Datei als Ergebnis
(Rückgabewert aus deren "ok"). PC5_SYSROOT stellt nur die Systemerkennung (Paketnamen) auf eine Attrappe um.
"""
import concurrent.futures
import importlib.util
import json
import os
import shutil
import subprocess
import sys

TIMEOUT_S = 20
MIN_PYTHON = (3, 10)

# (Art, Name, Pflicht, Prüfung, Folge deutsch, Folge englisch). Prüfung: GI "Namespace Version",
# Modul = Importname, Werkzeug = Programmname im PATH.
CHECKS = (
    ("gi", "Gtk 4.0", True, "Gtk 4.0", "", ""),
    ("gi", "Gdk 4.0", True, "Gdk 4.0", "", ""),
    ("gi", "WebKit 6.0", True, "WebKit 6.0", "", ""),
    ("gi", "JavaScriptCore 6.0", True, "JavaScriptCore 6.0", "", ""),
    ("gi", "Soup 3.0", True, "Soup 3.0", "", ""),
    ("gi", "GLibUnix 2.0", False, "GLibUnix 2.0",
     "Keine Folge: Der PC5 Launcher nimmt dann den älteren Weg über GLib.", "None: an older GLib path is used."),
    ("gi", "NM 1.0", False, "NM 1.0",
     "WLAN wird über nmcli eingerichtet (etwas langsamer).", "Wi-Fi is configured through nmcli (a bit slower)."),
    ("gi", "OSTree 1.0", False, "OSTree 1.0",
     "Die Zeile „Systemabbild“ in den Einstellungen fehlt.", "The “system image” row in Settings is missing."),
    ("gi", "Flatpak 1.0", False, "Flatpak 1.0",
     "Der Einrichtungs-Assistent installiert über die Kommandozeile; Fortschritt nur „n von m“.",
     "The setup assistant installs through the command line; progress shows only “n of m”."),
    ("module", "PIL", True, "PIL", "", ""),
    ("module", "evdev", True, "evdev", "", ""),
    ("module", "Xlib", True, "Xlib", "", ""),
    ("module", "pyudev", True, "pyudev", "", ""),
    ("module", "vdf", True, "vdf", "", ""),
    ("module", "yaml", True, "yaml", "", ""),
    ("module", "requests", False, "requests",
     "Bilder aus dem Netz (Cover) lädt der PC5 Launcher nicht.", "Cover images are not downloaded."),
    ("tool", "gamescope", True, "gamescope", "", ""),
    ("tool", "gamescopectl", True, "gamescopectl", "", ""),
    ("tool", "bwrap", True, "bwrap", "", ""),
    ("tool", "flatpak", False, "flatpak",
     "Der Einrichtungs-Assistent kann keine Emulatoren und Apps installieren.",
     "The setup assistant cannot install emulators and apps."),
    ("tool", "7z", False, "7z",
     "Archive im Format 7z lassen sich nicht entpacken (ZIP und TAR gehen).",
     "7z archives cannot be unpacked (ZIP and TAR work)."),
    ("tool", "unrar", False, "unrar",
     "RAR-Archive lassen sich nicht entpacken.", "RAR archives cannot be unpacked."),
    ("tool", "umu-run", False, "umu-run",
     "Windows-Spiele aus GOG, Epic und anderen Shops starten erst, wenn der Einrichtungs-Assistent "
     "umu-launcher geladen hat.",
     "Windows games from GOG, Epic and other stores start only after the setup assistant has fetched umu-launcher."),
    ("tool", "steamosctl", False, "steamosctl",
     "„Zum Desktop wechseln“ und das System-Update im Ein/Aus-Menü fehlen.",
     "“Switch to desktop” and the system update in the power menu are missing."),
    ("tool", "pactl", False, "pactl",
     "Tonausgang und Lautstärke lassen sich nicht einstellen.", "Audio output and volume cannot be changed."),
    ("tool", "wpctl", False, "wpctl",
     "Lautstärke einzelner Apps lässt sich nicht einstellen.", "Per-app volume cannot be changed."),
    ("tool", "nmcli", False, "nmcli",
     "WLAN-Einstellungen fehlen, wenn auch NM 1.0 fehlt.", "Wi-Fi settings are missing if NM 1.0 is missing too."),
    ("tool", "busctl", False, "busctl",
     "Ausschalten, Neustart und Bluetooth gehen nicht über die Konsole.",
     "Power off, restart and Bluetooth do not work from the console."),
    ("tool", "bluetoothctl", False, "bluetoothctl",
     "Controller lassen sich nicht über die Konsole koppeln.", "Controllers cannot be paired from the console."),
    ("tool", "startplasma-wayland", False, "startplasma-wayland",
     "Die Kachel „Desktop“ fehlt.", "The “Desktop” tile is missing."),
    ("tool", "xdg-open", False, "xdg-open",
     "Links aus Apps öffnen sich nicht im eingebauten Browser.", "Links from apps do not open in the built-in browser."),
    ("tool", "setpriv", False, "setpriv",
     "Gestartete Apps beenden sich nicht von selbst, wenn das Home abstürzt.",
     "Started apps do not end by themselves if the Home crashes."),
    ("tool", "ionice", False, "ionice",
     "Hintergrundarbeit läuft mit normaler Priorität.", "Background work runs with normal priority."),
    ("tool", "claude", False, "claude",
     "Die Kachel „Claude Code“ fehlt.", "The “Claude Code” tile is missing."),
)

# Paketnamen je System (nach bestem Wissen, unbestätigt; nur als Text, der Installer führt nie etwas aus)
PACKAGES = {
    "fedora": {"gi": "python3-gobject", "Gtk 4.0": "gtk4", "Gdk 4.0": "gtk4", "WebKit 6.0": "webkitgtk6.0",
               "JavaScriptCore 6.0": "webkitgtk6.0", "Soup 3.0": "libsoup3", "PIL": "python3-pillow",
               "evdev": "python3-evdev", "Xlib": "python3-xlib", "pyudev": "python3-pyudev", "vdf": "python3-vdf",
               "yaml": "python3-pyyaml", "requests": "python3-requests", "bwrap": "bubblewrap", "7z": "7zip",
               "gamescope": "gamescope", "gamescopectl": "gamescope", "umu-run": "umu-launcher",
               "flatpak": "flatpak", "NM 1.0": "NetworkManager-libnm", "Flatpak 1.0": "flatpak-libs"},
    "arch": {"gi": "python-gobject", "Gtk 4.0": "gtk4", "Gdk 4.0": "gtk4", "WebKit 6.0": "webkitgtk-6.0",
             "JavaScriptCore 6.0": "webkitgtk-6.0", "Soup 3.0": "libsoup3", "PIL": "python-pillow",
             "evdev": "python-evdev", "Xlib": "python-xlib", "pyudev": "python-pyudev", "vdf": "python-vdf",
             "yaml": "python-yaml", "requests": "python-requests", "bwrap": "bubblewrap", "7z": "7zip",
             "gamescope": "gamescope", "gamescopectl": "gamescope", "flatpak": "flatpak", "NM 1.0": "libnm"},
    "debian": {"gi": "python3-gi", "Gtk 4.0": "gir1.2-gtk-4.0", "Gdk 4.0": "gir1.2-gtk-4.0",
               "WebKit 6.0": "gir1.2-webkit-6.0", "JavaScriptCore 6.0": "gir1.2-javascriptcoregtk-6.0",
               "Soup 3.0": "gir1.2-soup-3.0", "PIL": "python3-pil", "evdev": "python3-evdev",
               "Xlib": "python3-xlib", "pyudev": "python3-pyudev", "vdf": "python3-vdf", "yaml": "python3-yaml",
               "requests": "python3-requests", "bwrap": "bubblewrap", "7z": "7zip", "gamescope": "gamescope",
               "gamescopectl": "gamescope", "flatpak": "flatpak", "NM 1.0": "gir1.2-nm-1.0"},
}
PACKAGES["bazzite"] = PACKAGES["fedora"]
PACKAGES["ubuntu"] = PACKAGES["debian"]
INSTALL_WORD = {"fedora": "dnf install", "bazzite": "rpm-ostree install", "arch": "pacman -S",
                "debian": "apt install", "ubuntu": "apt install"}
SYSTEM_NOTE = {
    "bazzite": ("Auf Bazzite ist normalerweise alles vorhanden. Fehlt etwas, geht das nur über das Systemabbild "
                "(„rpm-ostree install …“, danach Neustart). Das macht der Installer nie selbst.",
                "Bazzite normally has everything. Anything missing can only be layered into the system image "
                "(“rpm-ostree install …”, then reboot). The installer never does that itself."),
    "steamos": ("Auf SteamOS lässt sich das nicht ohne Entsperren des Systems nachinstallieren. Die Laufzeit "
                "kommt mit einer späteren Fassung (Flatpak, Stufe 2).",
                "On SteamOS this cannot be added without unlocking the system. The runtime will come with a "
                "later version (Flatpak, stage 2)."),
    "arch": ("umu-launcher und gamescope-session-plus gibt es im AUR.",
             "umu-launcher and gamescope-session-plus are in the AUR."),
}

GI_CODE = ("import gi, sys\n"
           "ns, ver = sys.argv[1], sys.argv[2]\n"
           "gi.require_version(ns, ver)\n"
           "import importlib\n"
           "importlib.import_module('gi.repository.' + ns)\n")
MODULE_CODE = "import importlib, sys\nimportlib.import_module(sys.argv[1])\n"


def _python():
    return sys.executable or "python3"


def _data_dir():
    root = os.environ.get("COUCH_TEST_ROOT")
    if root:
        return os.path.join(root, "data")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "couch")


def _run_python(code, args):
    """(ok, Meldung) eines Prüf-Kindes; nie länger als TIMEOUT_S."""
    try:
        proc = subprocess.run([_python(), "-I", "-c", code] + list(args), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=TIMEOUT_S,
                              env=_child_env())
    except subprocess.TimeoutExpired:
        return False, "Zeitgrenze von %d s überschritten" % TIMEOUT_S
    except OSError as e:
        return False, str(e)
    if proc.returncode == 0:
        return True, ""
    lines = [line for line in proc.stderr.decode("utf-8", "replace").strip().splitlines() if line.strip()]
    return False, (lines[-1] if lines else "Rückgabewert %d" % proc.returncode)[:200]


def _child_env():
    # ohne Anzeige: Die Prüfung soll nie ein Fenster öffnen oder sich an einen Bildschirm hängen. Der Import von
    # Gtk legte sonst ~/.cache an (leer, aber neu): ein Cache-Ort, den es nicht gibt, und nichts bleibt liegen.
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.setdefault("PATH", os.defpath)
    env["XDG_CACHE_HOME"] = "/nonexistent/pc5-depcheck"
    return env


def _tool(name):
    found = shutil.which(name)
    if found:
        return True, found
    own = None
    if name == "umu-run":                   # wie paths.tool: eigene Kopie aus dem Einrichtungs-Assistenten
        own = os.path.join(_data_dir(), "tools", "umu", "umu-run")
    elif name == "claude":                  # wie die Kachel (E105): auch ~/.local/bin
        own = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude")
    if own and os.path.isfile(own) and os.access(own, os.X_OK):
        return True, own
    return False, "nicht im PATH"


def check_one(entry):
    kind, name, required, probe, effect, effect_en = entry
    if kind == "gi":
        ns, ver = probe.split()
        ok, detail = _run_python(GI_CODE, [ns, ver])
    elif kind == "module":
        ok, detail = _run_python(MODULE_CODE, [probe])
    else:
        ok, detail = _tool(probe)
        if ok:
            detail = ""
    return {"kind": kind, "name": name, "required": required, "ok": ok, "detail": detail,
            "effect": "" if ok else effect, "effect_en": "" if ok else effect_en}


def _sysdetect():
    """sysdetect aus demselben Ordner, ohne das Paket couch zu laden (das braucht schon gi)."""
    here = os.path.dirname(os.path.realpath(__file__))
    sys.dont_write_bytecode = True          # nie __pycache__ neben das Paket legen (make-dist, Fassungen)
    spec = importlib.util.spec_from_file_location("_couch_sysdetect", os.path.join(here, "sysdetect.py"))
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def package_hint(os_id, missing, gi_missing):
    table = PACKAGES.get(os_id)
    if not table:
        return ""
    names = []
    if gi_missing and table.get("gi"):
        names.append(table["gi"])
    for name in missing:
        pkg = table.get(name)
        if pkg and pkg not in names:
            names.append(pkg)
    return ("%s %s" % (INSTALL_WORD.get(os_id, ""), " ".join(names))).strip() if names else ""


def check():
    version = sys.version_info[:3]
    items = [{"kind": "python", "name": "Python ≥ %d.%d" % MIN_PYTHON, "required": True,
              "ok": version >= MIN_PYTHON, "detail": "%d.%d.%d" % version, "effect": "", "effect_en": ""}]
    gi_ok, gi_detail = _run_python("import gi\n", [])
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for entry, result in zip(CHECKS, pool.map(check_one, CHECKS)):
            if entry[0] == "gi" and not gi_ok:
                result.update(ok=False, detail="PyGObject (gi) fehlt: " + gi_detail)
            items.append(result)
    try:
        sysinfo = _sysdetect().detect()
    except Exception:                       # Erkennung ist Beiwerk: Ohne sie fehlen nur die Paketnamen
        sysinfo = {"os": {"id": "other", "name": "Linux"}}
    os_id = sysinfo.get("os", {}).get("id", "other")
    missing_req = [i["name"] for i in items if i["required"] and not i["ok"]]
    missing_opt = [i["name"] for i in items if not i["required"] and not i["ok"]]
    note = SYSTEM_NOTE.get(os_id, ("", ""))
    return {"ok": not missing_req, "python": "%d.%d.%d" % version, "system": os_id,
            "system_name": sysinfo.get("os", {}).get("name", ""),
            "items": items, "missing_required": missing_req, "missing_optional": missing_opt,
            "packages": package_hint(os_id, missing_req + missing_opt, not gi_ok),
            "note": note[0], "note_en": note[1]}


def load_fake(path):
    """Ergebnis aus PC5_DEPCHECK (Attrappe für Tests). Fehlt etwas, gilt es als nicht bestanden."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {"ok": False, "items": [], "missing_required": ["PC5_DEPCHECK unlesbar: %s" % e],
                "missing_optional": [], "packages": "", "note": "", "note_en": ""}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("items", [])
    data.setdefault("missing_required", [])
    data.setdefault("missing_optional", [])
    data["ok"] = bool(data.get("ok")) and not data["missing_required"]
    return data


def lang(env=None):
    """Sprache der Textausgabe wie bei install.sh: Englisch, wenn die Locale mit "en" beginnt, sonst Deutsch."""
    env = os.environ if env is None else env
    loc = env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG") or ""
    return "en" if loc.lower().startswith("en") else "de"


# Textausgabe: (deutsch, englisch)
_TEXT = {"ok": ("ok   ", "ok   "), "MISSING": ("FEHLT", "MISSING"), "missing": ("fehlt", "missing"),
         "required": ("(Pflicht) ", "(required) "), "effect": ("       Folge: ", "       Effect: "),
         "all": ("Alle Pflichtteile sind vorhanden.", "All required parts are present."),
         "lacking": ("Es fehlen Pflichtteile: ", "Required parts are missing: "),
         "packages": ("Paketnamen auf diesem System (nur als Hinweis): ",
                      "Package names on this system (for reference only): ")}
_DETAIL_EN = (("PyGObject (gi) fehlt", "PyGObject (gi) is missing"), ("nicht im PATH", "not in PATH"))


def text(result, language=None):
    en = (language or lang()) == "en"

    def say(key):
        return _TEXT[key][1 if en else 0]

    lines = []
    for item in result.get("items", []):
        mark = say("ok") if item.get("ok") else say("MISSING" if item.get("required") else "missing")
        extra = item.get("detail") if not item.get("ok") else ""
        if en and extra:
            for de, eng in _DETAIL_EN:
                extra = extra.replace(de, eng)
        lines.append("%s  %-22s %s%s" % (mark, item.get("name", ""), say("required") if item.get("required") and not
                                           item.get("ok") else "", extra or ""))
        effect = item.get("effect_en" if en else "effect") or (item.get("effect") if en else "")
        if not item.get("ok") and effect:
            lines.append(say("effect") + effect)
    if result.get("ok"):
        lines.append(say("all"))
    else:
        lines.append(say("lacking") + ", ".join(result.get("missing_required", [])))
    if result.get("packages"):
        lines.append(say("packages") + result["packages"])
    note = result.get("note_en" if en else "note") or (result.get("note") if en else "")
    if note and not result.get("ok"):
        lines.append(note)
    return "\n".join(lines)


def main(argv):
    args = argv[1:]
    if any(a not in ("--json",) for a in args):
        sys.stderr.write("%s: couch-home --check [--json]\n" % ("Usage" if lang() == "en" else "Aufruf"))
        return 2
    fake = os.environ.get("PC5_DEPCHECK")
    result = load_fake(fake) if fake else check()
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(text(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
