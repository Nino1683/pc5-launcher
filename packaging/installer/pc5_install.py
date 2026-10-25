"""pc5_install.py – Kern von install.sh und uninstall.sh des PC5 Launchers (E104 b, Bauplan 7.3). Nur
Standardbibliothek, ab Python 3.10.

  pc5_install.py install   [--yes] [--hook|--no-hook] [--dry-run] [--no-network] [--status] [--rollback] [--adopt]
  pc5_install.py uninstall [--yes] [--purge-data] [--dry-run]
  pc5_install.py status | rollback

Arbeitet nur auf Nutzer-Ebene, unter $HOME. Ruft nie sudo, pkexec, su, steamos-readonly, pacman, rpm-ostree,
ujust, dnf oder apt, nie „flatpak --system“, fragt nie nach einem Kennwort. Systembefehle nur über den PATH
(nie absolute Pfade), damit Stummel in Tests greifen; mit PC5_DRY_SYSTEM=1 werden sie nur protokolliert
(Ausgabe und PC5_DRY_LOG=<datei>). Für Tests außerdem: PC5_SYSROOT (Systemerkennung auf eine Attrappe),
PC5_DEPCHECK (Ergebnis von couch-home --check aus einer Datei).

Orte wie im Kern (app/couch/paths.py) und im Supervisor: ~/.local/share/couch (Fassungen releases/<v>-<zeit>
mit Marke .couch-release, Verweise current und previous, hook/, install-manifest.json), ~/.config/couch,
~/.local/state/couch. Eingehängt wird nur nach Zustimmung und nur, wenn couch-home --check keine
Pflicht-Lücke meldet (Bauplan 2.2 bis 2.4).

Rückgabewerte: 0 erledigt (auch „abgelegt, nicht eingehängt“), 1 Fehler, 2 falscher Aufruf, 3 nichts getan
(fremde Einrichtung, abgelehnt).
"""
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse

sys.dont_write_bytecode = True               # sysdetect.py wird geladen: kein __pycache__ ins Paket oder die Fassung
HERE = os.path.dirname(os.path.realpath(__file__))
PACKAGE = os.path.dirname(HERE)
RELEASE_MARK = ".couch-release"          # dieselbe Marke wie tools/install-release
MANIFEST_FORMAT = 1
KEEP_NEWEST = 2                           # außer current und previous bleiben die zwei neuesten
HOOK_LINE = re.compile(r"pc5-launcher-hook v(\d+)")
VERSION_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,40}")
BLOCK_START = "# >>> pc5-launcher >>>"
BLOCK_END = "# <<< pc5-launcher <<<"
BLOCK = (BLOCK_START + " (angelegt von install.sh, entfernt von uninstall.sh; nicht von Hand ändern)\n"
         '[ -r "${HOME:-/nonexistent}/.local/share/couch/hook/gsp-hook.sh" ] && '
         '. "${HOME}/.local/share/couch/hook/gsp-hook.sh"\n'
         + BLOCK_END + "\n")
LEGACY_MARK = "_couch_entry"             # Haken aus session/sessions.d-steam (tools/install-release, Entwicklungsrechner)
DESKTOP = ("pc5-launcher-off.desktop", "pc5-launcher-on.desktop", "pc5-launcher-status.desktop",
           "pc5-launcher-uninstall.desktop")
# Diese Befehle gibt es für den Installer nicht (Bauplan 7.3, SAFETY 13). Nur zur Abwehr eines Fehlers hier.
FORBIDDEN = frozenset(("sudo", "pkexec", "su", "doas", "steamos-readonly", "pacman", "rpm-ostree", "ujust",
                       "dnf", "apt", "apt-get", "passwd"))

LANG = "en" if (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "") \
    .lower().startswith("en") else "de"
MSG = {
    "system": ("System:   %s", "System:   %s"),
    "kind_gsp": ("Sitzung:  gamescope-session-plus (Art A), Clients: %s",
                 "Session:  gamescope-session-plus (type A), clients: %s"),
    "kind_steamos": ("Sitzung:  Valves gamescope-session (SteamOS, Art B)%s",
                     "Session:  Valve's gamescope-session (SteamOS, type B)%s"),
    "kind_none": ("Sitzung:  keine Spielmodus-Sitzung gefunden (Art C)", "Session:  no game mode session found (type C)"),
    "unit_odd": (" – steam-launcher.service sieht anders aus als erwartet",
                 " – steam-launcher.service does not look as expected"),
    "foreign": ("Diese Einrichtung stammt aus tools/install-release; install.sh lässt sie unverändert. "
                "Umstellen nur mit --adopt.",
                "This setup comes from tools/install-release; install.sh leaves it unchanged. "
                "Switching over only with --adopt."),
    "adopt": ("Übernehme die Einrichtung aus tools/install-release (--adopt). Der bisherige Haken bleibt, wie er ist.",
              "Adopting the setup from tools/install-release (--adopt). The existing hook stays as it is."),
    "check_ok": ("Prüfung:  alle Pflichtteile vorhanden", "Check:    all required parts present"),
    "check_bad": ("Prüfung:  es fehlen Pflichtteile: %s", "Check:    required parts missing: %s"),
    "check_run": ("Prüfung:  couch-home --check lief nicht (%s)", "Check:    couch-home --check did not run (%s)"),
    "optional": ("          fehlt (optional) %s: %s", "          missing (optional) %s: %s"),
    "packages": ("          Paketnamen (nur als Hinweis, der Installer führt nichts aus): %s",
                 "          Package names (hint only, the installer runs nothing): %s"),
    "dry": ("Probelauf (--dry-run): Es wird nichts geschrieben.", "Dry run (--dry-run): nothing is written."),
    "would": ("würde: %s", "would: %s"),
    "same": ("Diese Fassung ist schon installiert (%s).", "This version is already installed (%s)."),
    "placed": ("Fassung abgelegt und geprüft: %s", "Version placed and checked: %s"),
    "current": ("current  -> %s", "current  -> %s"),
    "previous": ("previous -> %s", "previous -> %s"),
    "pruned": ("alte Fassungen entfernt: %s", "old versions removed: %s"),
    "fail": ("FEHLER  %s", "ERROR  %s"),
    "aborted": ("Abgebrochen: Prüfung nicht bestanden. current bleibt unverändert.",
                "Aborted: check failed. current stays unchanged."),
    "question": ("Beim nächsten Start des Spielmodus den PC5 Launcher verwenden? [j/N] ",
                 "Use PC5 Launcher the next time game mode starts? [y/N] "),
    "no_tty": ("Ohne Terminal keine Rückfrage: nicht eingehängt (mit --yes --hook einhängen).",
               "No terminal for the question: not hooked in (use --yes --hook)."),
    "hooked_gsp": ("Eingehängt: %s", "Hooked in: %s"),
    "hooked_steamos": ("Ergänzungsdatei angelegt: %s. Gilt ab dem nächsten Start des Spielmodus.",
                       "Drop-in created: %s. Takes effect the next time game mode starts."),
    "hook_kept": ("Haken bleibt unverändert: %s", "Hook stays unchanged: %s"),
    "hook_changed": ("Vom Nutzer verändert, nicht angefasst: %s", "Changed by the user, left alone: %s"),
    "not_hooked": ("Nicht eingehängt: %s", "Not hooked in: %s"),
    "hook_off_hint": ("der vorhandene Haken (--no-hook entfernt ihn nicht; dafür uninstall.sh oder „Spielmodus aus“)",
                      "the existing hook (--no-hook does not remove it; use uninstall.sh or “game mode off”)"),
    "declined": ("auf Wunsch", "as requested"),
    "steamos_runtime": ("Auf diesem SteamOS fehlt die Oberflächen-Laufzeit (WebKitGTK 6.0, PyGObject …). Der PC5 "
                        "Launcher ist abgelegt, aber nicht eingehängt; der Spielmodus startet weiter mit Steam. Diese "
                        "Laufzeit kommt mit einer späteren Fassung (Flatpak). Was genau fehlt: %s",
                        "This SteamOS lacks the interface runtime (WebKitGTK 6.0, PyGObject …). PC5 Launcher is "
                        "placed but not hooked in; game mode keeps starting Steam. The runtime will come with a later "
                        "version (Flatpak). Missing exactly: %s"),
    "missing_runtime": ("Es fehlen Pflichtteile (%s). Die Fassung ist abgelegt, aber nicht eingehängt.",
                        "Required parts are missing (%s). The version is placed but not hooked in."),
    "no_session": ("Es gibt hier keine Spielmodus-Sitzung (gamescope-session-plus oder Valves gamescope-session). "
                   "Der PC5 Launcher braucht eine Gamescope-Sitzung mit Steam. Möglich: Paket gamescope-session-plus "
                   "(ChimeraOS-Projekt; Fedora COPR, Arch AUR) installieren und den Installer erneut starten.",
                   "There is no game mode session here (gamescope-session-plus or Valve's gamescope-session). PC5 "
                   "Launcher needs a Gamescope session with Steam. Option: install the gamescope-session-plus package "
                   "(ChimeraOS project; Fedora COPR, Arch AUR) and run the installer again."),
    "no_clients": ("gamescope-session-plus ist da, aber kein Client in der Anmeldung zu finden.",
                   "gamescope-session-plus is present, but no client was found in the login setup."),
    "unit_bad": ("steam-launcher.service sieht nicht so aus wie erwartet (ExecStart ist nicht "
                 "/usr/lib/steamos/steam-launcher). Es wird nichts eingehängt.",
                 "steam-launcher.service does not look as expected (ExecStart is not /usr/lib/steamos/steam-launcher). "
                 "Nothing is hooked in."),
    "desktop": ("Desktop-Einträge: „PC5 Launcher – Spielmodus aus/an“, „Zustand“, „Deinstallieren“",
                "Desktop entries: “PC5 Launcher – game mode off/on”, “status”, “uninstall”"),
    "done_hooked": ("Fertig. Beim nächsten Start des Spielmodus startet der PC5 Launcher; beim ersten Start führt "
                    "dich der Einrichtungs-Assistent am Fernseher durch alles.",
                    "Done. PC5 Launcher starts the next time game mode starts; on first start the setup assistant "
                    "guides you on the TV."),
    "done_plain": ("Fertig. Der Spielmodus startet weiter mit Steam.", "Done. Game mode keeps starting Steam."),
    "back": ("Zurück zu Steam: PS + Create + Options 5 s am Pad, im Desktop-Modus „PC5 Launcher – Spielmodus aus“ "
             "oder bash uninstall.sh.",
             "Back to Steam: hold PS + Create + Options for 5 s, in desktop mode “PC5 Launcher – game mode off” or "
             "bash uninstall.sh."),
    "rollback_none": ("Keine vorige Fassung (previous) vorhanden.", "No previous version available."),
    "rollback": ("current -> %s (vorher %s)", "current -> %s (before %s)"),
    "nothing": ("Hier ist kein PC5 Launcher von install.sh installiert.", "No PC5 Launcher installed by install.sh here."),
    "confirm_uninstall": ("PC5 Launcher deinstallieren? Einstellungen, Spiele und Steam bleiben. [j/N] ",
                          "Uninstall PC5 Launcher? Settings, games and Steam stay. [y/N] "),
    "confirm_purge": ("Auch Einstellungen, Bibliothek und Zustand des PC5 Launchers in den Papierkorb legen? [j/N] ",
                      "Also move PC5 Launcher settings, library and state to the trash? [y/N] "),
    "removed": ("entfernt:      %s", "removed:       %s"),
    "kept": ("behalten:      %s", "kept:          %s"),
    "untouched": ("nicht angefasst: %s", "not touched:   %s"),
    "trashed": ("im Papierkorb: %s", "in the trash:  %s"),
    "flatpak_hint": ("Vom Einrichtungs-Assistenten installiert (selbst entfernen mit: flatpak uninstall --user %s)",
                     "Installed by the setup assistant (remove yourself with: flatpak uninstall --user %s)"),
    "system_cmd": ("(nur protokolliert) %s", "(logged only) %s"),
}
# Wie "python3 -m compileall", aber ohne Bytecode zu schreiben: jede .py-Datei und jedes Python-Skript
# (Kopfzeile) mit compile() übersetzen. So bleibt nichts liegen, was wieder weggeräumt werden müsste.
COMPILE = r"""
import os, sys
bad = 0
for top in sys.argv[1:]:
    for folder, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            path = os.path.join(folder, name)
            if not name.endswith(".py"):
                try:
                    with open(path, "rb") as f:
                        head = f.readline(200)
                except OSError:
                    continue
                if not (head.startswith(b"#!") and b"python" in head):
                    continue
            try:
                with open(path, "rb") as f:
                    compile(f.read(), path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as e:
                bad += 1
                print("%s: %s" % (path, e))
sys.exit(1 if bad else 0)
"""

UNTOUCHED = ("~/Games (Spiele, prefixes/couch, backups/couch)", "Steam-Dateien", "Proton in compatibilitytools.d",
             "Downloads", "Flatpaks")


def t(key, *args):
    text = MSG[key][1 if LANG == "en" else 0]
    return text % args if args else text


def tr(de, en):
    """Einzelne Sätze ohne eigenen Schlüssel in MSG: deutsch oder englisch nach LANG."""
    return en if LANG == "en" else de


def say(text=""):
    print(text, flush=True)


class Failure(Exception):
    pass


# ----------------------------------------------------------------------------- Orte
class Places:
    """Orte wie im Kern (paths.py) und im Supervisor. Anbindungen an das System (sessions.d, systemd,
    Desktop-Einträge) folgen XDG_CONFIG_HOME bzw. XDG_DATA_HOME wie die Programme, die sie lesen."""

    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.home = env.get("HOME") or os.path.expanduser("~")
        h = self.home
        self.data = os.path.join(h, ".local", "share", "couch")
        self.config = os.path.join(h, ".config", "couch")
        self.state = os.path.join(h, ".local", "state", "couch")
        self.cache = os.path.join(h, ".cache", "couch")
        runtime = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        self.run = os.path.join(runtime, "couch")
        self.releases = os.path.join(self.data, "releases")
        self.current = os.path.join(self.data, "current")
        self.previous = os.path.join(self.data, "previous")
        self.hook = os.path.join(self.data, "hook")
        self.manifest = os.path.join(self.data, "install-manifest.json")
        xc = env.get("XDG_CONFIG_HOME") or ""
        xd = env.get("XDG_DATA_HOME") or ""
        self.xdg_config = xc if os.path.isabs(xc) else os.path.join(h, ".config")
        self.xdg_data = xd if os.path.isabs(xd) else os.path.join(h, ".local", "share")
        self.sessions_d = os.path.join(self.xdg_config, "gamescope-session-plus", "sessions.d")
        self.dropin = os.path.join(self.xdg_config, "systemd", "user", "steam-launcher.service.d",
                                   "50-pc5-launcher.conf")
        self.apps = os.path.join(self.xdg_data, "applications")
        self.wireplumber = os.path.join(self.xdg_config, "wireplumber", "wireplumber.conf.d",
                                        "90-pc5-controller-audio.conf")
        self.trash = os.path.join(self.xdg_data, "Trash")

    def rel(self, path):
        return os.path.relpath(path, self.home)

    def show(self, path):
        """Für Meldungen: ~/…"""
        return "~/" + self.rel(path)

    def abs(self, rel):
        return os.path.join(self.home, rel)


# ----------------------------------------------------------------------------- kleine Helfer
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        return sha256_file(path)
    except OSError:
        return None


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def hook_version(text):
    m = HOOK_LINE.search(text or "")
    return int(m.group(1)) if m else 0


def write_atomic(path, data, mode=0o644):
    """Über eine Nachbardatei und rename; nie halb. Rechte genau mode."""
    tmp = "%s.pc5-new.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def link_atomic(link, target):
    tmp = "%s.pc5-new.%d" % (link, os.getpid())
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def remove_file(path):
    """Eine einzelne Datei oder ein Verweis, nie ein Ordner."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            return False
        os.unlink(path)
        return True
    except OSError:
        return False


def remove_own_tree(path, mark, parent):
    """Einzige rekursive Abräumfunktion des Installers (wie in tools/install-release): nur ein Ordner mit
    unserer Marke direkt unter parent, kein Verweis, keine Einhängung darin, Verweisen wird nicht gefolgt,
    Datei für Datei. Im Zweifel bleibt der Ordner liegen (False)."""
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            return False
        if os.path.realpath(os.path.dirname(path)) != os.path.realpath(parent):
            return False
        if not os.path.isfile(os.path.join(path, mark)):
            return False
        device = os.lstat(path).st_dev
        for folder, dirs, _files in os.walk(path, followlinks=False):
            for name in dirs:
                full = os.path.join(folder, name)
                if os.path.islink(full):
                    continue
                if os.lstat(full).st_dev != device or os.path.ismount(full):
                    say(tr("lasse %s liegen: enthält eine Einhängung (%s)", "leaving %s in place: it contains a mount (%s)") % (path, full))
                    return False
        for folder, dirs, files in os.walk(path, topdown=False, followlinks=False):
            for name in files:
                os.unlink(os.path.join(folder, name))
            for name in dirs:
                full = os.path.join(folder, name)
                if os.path.islink(full):
                    os.unlink(full)
                else:
                    os.rmdir(full)
        os.rmdir(path)
        return True
    except OSError as e:
        say(tr("konnte %s nicht entfernen: %s", "could not remove %s: %s") % (path, e))
        return False


def makedirs_noted(path, created, mode=0o755):
    """Legt fehlende Ordner Stufe für Stufe an und merkt sich jeden selbst angelegten (für uninstall)."""
    missing = []
    probe = os.path.abspath(path)
    while not os.path.lexists(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for folder in reversed(missing):
        os.mkdir(folder, mode)
        created.append(folder)
    if not os.path.isdir(path):
        raise Failure(tr("kein Ordner: %s", "not a folder: %s") % path)


def run_check(argv, timeout=120, env=None):
    """(ok, Ausgabe) eines Prüfbefehls; hängt nie länger als timeout."""
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env, text=True, errors="replace")
        return proc.returncode == 0, proc.stdout
    except subprocess.TimeoutExpired:
        return False, "Zeitgrenze von %d s überschritten" % timeout
    except OSError as e:
        return False, str(e)


def system(argv):
    """Der eine Weg für Systembefehle: nur über den PATH, nie ein verbotener, mit PC5_DRY_SYSTEM=1 nur notiert."""
    if "/" in argv[0] or argv[0] in FORBIDDEN:
        raise Failure(tr("unerlaubter Befehl: %s", "command not allowed: %s") % shlex.join(argv))
    line = shlex.join(argv)
    if os.environ.get("PC5_DRY_SYSTEM") == "1":
        say(t("system_cmd", line))
        log = os.environ.get("PC5_DRY_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    ok, out = run_check(argv, timeout=60)
    if not ok:
        say(tr("Befehl gescheitert: %s\n%s", "command failed: %s\n%s") % (line, out.strip()))
    return ok


def ask(question, yes):
    if yes:
        return True
    if not sys.stdin.isatty():
        return None
    try:
        answer = input(question).strip().lower()
    except EOFError:
        return False
    return answer in ("j", "ja", "y", "yes")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Failure(tr("nicht ladbar: %s", "cannot be loaded: %s") % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------- Paket
def package_version(pkg):
    version = first_line(os.path.join(pkg, "VERSION"))
    if not version or not VERSION_OK.fullmatch(version):
        raise Failure(tr("VERSION fehlt oder ist unbrauchbar", "VERSION is missing or unusable"))
    return version


def read_manifest_sha(path):
    """MANIFEST.sha256 → {relativer Pfad: sha256}. Pfade ohne .., ohne führenden /."""
    entries = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not m:
                raise Failure(tr("MANIFEST.sha256: unbrauchbare Zeile: %r", "MANIFEST.sha256: unusable line: %r") % line[:80])
            rel = m.group(2)
            parts = rel.split("/")
            if rel.startswith("/") or ".." in parts or "" in parts:
                raise Failure(tr("MANIFEST.sha256: unerlaubter Pfad: %r", "MANIFEST.sha256: path not allowed: %r") % rel)
            entries[rel] = m.group(1)
    return entries


def shell_scripts(tree):
    found = []
    for folder, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            path = os.path.join(folder, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as f:
                    first = f.readline(200)
            except OSError:
                continue
            if (first.startswith(b"#!") and re.search(rb"\b(ba)?sh\b", first)) or name.endswith(".sh"):
                found.append(path)
    return sorted(found)


# ----------------------------------------------------------------------------- Manifest
def load_manifest(p):
    try:
        with open(p.manifest, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Failure(tr("install-manifest.json unlesbar: %s", "install-manifest.json is unreadable: %s") % e)
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        raise Failure(tr("install-manifest.json hat ein unbekanntes Format", "install-manifest.json has an unknown format"))
    data.setdefault("files", [])
    data.setdefault("dirs", [])
    data.setdefault("legacy", [])
    return data


def save_manifest(p, data):
    write_atomic(p.manifest, json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", 0o600)


def entry_for(manifest, rel):
    for item in manifest["files"]:
        if item.get("path") == rel:
            return item
    return None


def note_file(manifest, rel, **info):
    item = entry_for(manifest, rel)
    if item is None:
        item = {"path": rel}
        manifest["files"].append(item)
    item.update(info)
    return item


def note_dirs(manifest, p, created):
    for folder in created:
        rel = p.rel(folder)
        if rel not in manifest["dirs"]:
            manifest["dirs"].append(rel)


# ----------------------------------------------------------------------------- fremde Einrichtung
def legacy_hooks(p):
    """sessions.d-Dateien mit dem Haken aus session/sessions.d-steam (ohne unseren Block)."""
    found = []
    try:
        names = sorted(os.listdir(p.sessions_d))
    except OSError:
        return found
    seen = set()
    for name in names:
        path = os.path.join(p.sessions_d, name)
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            continue
        seen.add(real)
        try:
            text = read_bytes(real).decode("utf-8", "replace")
        except OSError:
            continue
        if LEGACY_MARK in text and BLOCK_START not in text:
            found.append(real)
    return found


def foreign_setup(p):
    """Einrichtung, die nicht von install.sh stammt: Fassungen ohne Manifest oder Haken aus sessions.d-steam."""
    if os.path.exists(p.manifest):
        return []
    reasons = []
    try:
        if os.path.isdir(p.releases) and os.listdir(p.releases):
            reasons.append(p.releases)
    except OSError:
        pass
    if os.path.lexists(p.current):
        reasons.append(p.current)
    reasons += legacy_hooks(p)
    return reasons


# ----------------------------------------------------------------------------- Fassungen
def is_release(path, releases):
    return (bool(path) and not path.endswith(".new") and os.path.isdir(path) and not os.path.islink(path)
            and os.path.realpath(os.path.dirname(path)) == os.path.realpath(releases)
            and os.path.isfile(os.path.join(path, RELEASE_MARK)))


def release_id(release):
    return "%s@%s" % (first_line(os.path.join(release, "VERSION")) or "0", os.path.basename(release))


def last_good(p):
    try:
        with open(os.path.join(p.state, "last-good"), encoding="utf-8", errors="replace") as f:
            lines = [line.strip() for line in f.read().splitlines()]
    except OSError:
        return None
    if len(lines) < 2:
        return None
    path = os.path.realpath(lines[1])
    return path if is_release(path, p.releases) and release_id(path) == lines[0] else None


def choose_previous(old, p):
    """Wie tools/install-release: die zuletzt bewährte Fassung, sonst das bisherige current, nie eine Fassung,
    für die <state>/fuse-current gilt (der Supervisor fiele sonst auf eine kaputte zurück)."""
    fuse = os.path.join(p.state, "fuse-current")
    bad = first_line(fuse)
    blanket = os.path.lexists(fuse) and not bad

    def broken(release):
        return release_id(release) == bad or (blanket and release == old)
    good = last_good(p)
    if good and not broken(good):
        return good
    if old and not broken(old):
        return old
    return None


def in_use(p):
    """Namen der Fassungen, die gerade benutzt werden: <run>/releases-in-use des Supervisors und eigene
    Prozesse mit Programm, Ordner oder Befehlszeile darin. Nur lesend."""
    base = os.path.realpath(p.releases) + os.sep
    names = set()

    def note(path):
        if path.startswith(base):
            names.add(path[len(base):].split(os.sep, 1)[0])
    try:
        with open(os.path.join(p.run, "releases-in-use"), encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    note(os.path.realpath(line.strip()) + os.sep)
    except OSError:
        pass
    uid = os.getuid()
    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit() and int(n) != os.getpid()]
    except OSError:
        pids = []
    for pid in pids:
        try:
            if os.stat("/proc/" + pid).st_uid != uid:
                continue
            for link in ("exe", "cwd"):
                try:
                    note(os.path.realpath(os.readlink("/proc/%s/%s" % (pid, link))) + os.sep)
                except OSError:
                    pass
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                for arg in f.read().split(b"\0"):
                    text = os.fsdecode(arg)
                    if "/" in text and "couch" in text:
                        note(os.path.realpath(text[text.index("/"):]) + os.sep)
        except OSError:
            continue
    return names


def releases_list(p):
    out = []
    try:
        names = sorted(os.listdir(p.releases))
    except OSError:
        return out
    for name in names:
        path = os.path.join(p.releases, name)
        if is_release(path, p.releases):
            try:
                age = time.time() - os.lstat(os.path.join(path, RELEASE_MARK)).st_mtime
            except OSError:
                continue
            out.append((age, name))
    out.sort()                                        # jüngste zuerst
    return out


def prune(p):
    keep = {os.path.basename(os.path.realpath(x)) for x in (p.current, p.previous) if os.path.lexists(x)}
    found = releases_list(p)
    keep |= {name for _age, name in found[:KEEP_NEWEST]} | in_use(p)
    gone = []
    for _age, name in found:
        if name not in keep and remove_own_tree(os.path.join(p.releases, name), RELEASE_MARK, p.releases):
            gone.append(name)
    return gone


def installed_same(p, pkg):
    """Zeigt current auf eine Fassung mit genau diesem Paket-Manifest?"""
    cur = os.path.realpath(p.current) if os.path.lexists(p.current) else None
    if not cur or not is_release(cur, p.releases):
        return None
    try:
        if read_bytes(os.path.join(cur, "MANIFEST.sha256")) == read_bytes(os.path.join(pkg, "MANIFEST.sha256")):
            return cur
    except OSError:
        pass
    return None


def check_release(tree, env_check):
    """bash -n, Python übersetzen (ohne Bytecode), Selbsttest des Supervisors, couch-home --check."""
    problems = []
    for script in shell_scripts(tree):
        ok, out = run_check(["bash", "-n", script], timeout=30)
        if not ok:
            problems.append("bash -n %s: %s" % (os.path.relpath(script, tree), out.strip()[:300]))
    ok, out = run_check([sys.executable, "-I", "-c", COMPILE, os.path.join(tree, "app"), os.path.join(tree, "session"),
                         os.path.join(tree, "installer")], timeout=300)
    if not ok:
        problems.append("Python übersetzen: " + out.strip()[-600:])
    ok, out = run_check([sys.executable, "-I", os.path.join(tree, "session", "supervisor.py"), "--selftest"],
                        timeout=30, env={"PATH": os.environ.get("PATH", os.defpath)})
    if not ok or out.strip() != "ok":
        problems.append("Selbsttest des Supervisors: " + out.strip()[:300])
    result, why = depcheck(tree, env_check)
    if result is None:
        problems.append("couch-home --check: " + why)
    return problems


def depcheck(tree, env=None):
    """(Ergebnis oder None, Grund) von <tree>/app/couch-home --check --json."""
    home = os.path.join(tree, "app", "couch-home")
    try:
        proc = subprocess.run([home, "--check", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=180, env=env, text=True, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, (proc.stderr.strip() or "keine Antwort")[:300]
    if not isinstance(data, dict):
        return None, "unbrauchbare Antwort"
    data["ok"] = bool(data.get("ok")) and not data.get("missing_required")
    return data, ""


def place_release(p, pkg, version, manifest):
    """Kopie nach releases/<v>-<zeit>.new, Abgleich mit MANIFEST.sha256, Prüfungen, dann umbenennen und die
    Verweise setzen. Liefert den Ordnernamen. Scheitert etwas, bleibt current unverändert."""
    entries = read_manifest_sha(os.path.join(pkg, "MANIFEST.sha256"))
    created = []
    makedirs_noted(p.releases, created)
    note_dirs(manifest, p, created)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (version, stamp)
    while os.path.lexists(os.path.join(p.releases, name)) or os.path.lexists(os.path.join(p.releases, name + ".new")):
        name += "x"
    staging = os.path.join(p.releases, name + ".new")
    target = os.path.join(p.releases, name)
    os.mkdir(staging, 0o755)
    switched = False
    try:
        with open(os.path.join(staging, RELEASE_MARK), "w", encoding="utf-8") as f:
            f.write(name + "\n")
        for rel in sorted(entries):
            src = os.path.join(pkg, rel)
            if os.path.islink(src) or not os.path.isfile(src):
                raise Failure(tr("Paketdatei fehlt oder ist ein Verweis: %s", "package file is missing or a symlink: %s") % rel)
            data = read_bytes(src)
            if sha256_bytes(data) != entries[rel]:
                raise Failure(tr("Prüfsumme stimmt nicht: %s", "checksum mismatch: %s") % rel)
            dst = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            mode = 0o755 if os.stat(src).st_mode & stat.S_IXUSR else 0o644
            with open(dst, "wb") as f:
                f.write(data)
            os.chmod(dst, mode)
        shutil.copyfile(os.path.join(pkg, "MANIFEST.sha256"), os.path.join(staging, "MANIFEST.sha256"))
        for rel, digest in entries.items():           # die Kopie selbst noch einmal gegen das Manifest
            if sha256_file(os.path.join(staging, rel)) != digest:
                raise Failure(tr("Kopie weicht ab: %s", "copy differs: %s") % rel)
        problems = check_release(staging, None)
        if problems:
            for problem in problems:
                say(t("fail", problem))
            raise Failure(t("aborted"))
        old = os.path.realpath(p.current) if os.path.lexists(p.current) else None
        if old and not is_release(old, p.releases):
            old = None
        previous = choose_previous(old, p)
        os.rename(staging, target)
        switched = True
        if previous and previous != target:
            link_atomic(p.previous, os.path.relpath(previous, os.path.realpath(p.data)))
        link_atomic(p.current, os.path.join("releases", name))
        say(t("placed", target))
        say(t("current", "releases/" + name))
        if os.path.lexists(p.previous):
            say(t("previous", os.readlink(p.previous)))
        gone = prune(p)
        if gone:
            say(t("pruned", ", ".join(gone)))
        return name
    finally:
        if not switched and os.path.isdir(staging):
            remove_own_tree(staging, RELEASE_MARK, p.releases)


# ----------------------------------------------------------------------------- Haken und Einträge
def install_own_file(p, manifest, path, data, kind, mode=0o644, check_bash=False):
    """Eigene Datei anlegen oder ersetzen. Ersetzt wird nur eine, die noch genau unsere ist (sha256 im
    Manifest); Haken-Dateien (hook, dropin) nur, wenn sich dabei die Kennzeile (pc5-launcher-hook vN) ändert.
    Vom Nutzer veränderte oder fremde Dateien bleiben. Rückgabe: "created", "replaced", "same", "kept-user",
    "kept-version"."""
    rel = p.rel(path)
    data = data if isinstance(data, bytes) else data.encode("utf-8")
    new_sha = sha256_bytes(data)
    current = file_sha(path)
    item = entry_for(manifest, rel)
    if current == new_sha:
        note_file(manifest, rel, kind=kind, sha256=new_sha, mode=mode)
        return "same"
    if os.path.lexists(path):
        if item is None or item.get("sha256") != current:
            return "kept-user"
        if kind in ("hook", "dropin") and hook_version(read_bytes(path).decode("utf-8", "replace")) == \
                hook_version(data.decode("utf-8", "replace")):
            return "kept-version"
    made = []
    makedirs_noted(os.path.dirname(path), made)
    note_dirs(manifest, p, made)
    if check_bash:
        tmp = path + ".pc5-check"
        write_atomic(tmp, data, mode)
        ok, out = run_check(["bash", "-n", tmp], timeout=30)
        if not ok:
            remove_file(tmp)
            raise Failure("bash -n %s: %s" % (rel, out.strip()[:300]))
        os.replace(tmp, path)
    else:
        write_atomic(path, data, mode)
    note_file(manifest, rel, kind=kind, sha256=new_sha, mode=mode)
    return "replaced" if current else "created"


def hook_gsp(p, info, manifest, release):
    """Art A: feste Kopie des Hakens, dann je Client der markierte Block bzw. eine eigene Datei."""
    msgs = []
    src = os.path.join(release, "session", "hooks", "gsp-hook.sh")
    result = install_own_file(p, manifest, os.path.join(p.hook, "gsp-hook.sh"), read_bytes(src), "hook",
                              check_bash=True)
    if result == "kept-user":
        msgs.append(t("hook_changed", p.show(os.path.join(p.hook, "gsp-hook.sh"))))
    sessions_real = os.path.realpath(p.sessions_d)
    done = set()
    for client in info["session"]["clients"]:
        path = os.path.join(p.sessions_d, client)
        real = os.path.realpath(path)
        if os.path.dirname(real) != sessions_real:
            msgs.append(t("hook_kept", tr("%s (Verweis aus sessions.d hinaus)", "%s (symlink pointing out of sessions.d)") % p.show(path)))
            continue
        if real in done:
            continue                                   # Verweis wie ogui-steam -> steam: nur einmal
        done.add(real)
        rel = p.rel(os.path.join(p.sessions_d, os.path.basename(real)))
        if not os.path.lexists(real):
            made = []
            makedirs_noted(os.path.dirname(real), made)
            note_dirs(manifest, p, made)
            write_atomic(real, BLOCK, 0o644)
            note_file(manifest, rel, kind="created", sha256=sha256_bytes(BLOCK.encode()), mode=0o644)
            msgs.append(t("hooked_gsp", "~/" + rel))
            continue
        if os.path.islink(real) or not os.path.isfile(real):
            msgs.append(t("hook_kept", "~/" + rel))
            continue
        data = read_bytes(real)
        text = data.decode("utf-8", "replace")
        if BLOCK_START in text:
            item = entry_for(manifest, rel)
            msgs.append(t("hook_kept" if item else "hook_changed", "~/" + rel))
            continue
        if LEGACY_MARK in text:
            note_legacy(manifest, rel, data)
            msgs.append(t("hook_kept", "~/" + rel + tr(" (Haken aus tools/install-release)", " (hook from tools/install-release)")))
            continue
        suffix = ("" if not data or data.endswith(b"\n") else "\n") + BLOCK
        new = data + suffix.encode("utf-8")
        write_atomic(real, new, stat.S_IMODE(os.stat(real).st_mode))
        note_file(manifest, rel, kind="appended", before=sha256_bytes(data), sha256=sha256_bytes(new),
                  suffix=suffix)
        msgs.append(t("hooked_gsp", "~/" + rel + tr(" (Block angehängt)", " (block appended)")))
    return msgs


def note_legacy(manifest, rel, data):
    if not any(x.get("path") == rel for x in manifest["legacy"]):
        manifest["legacy"].append({"path": rel, "sha256": sha256_bytes(data)})


def hook_steamos(p, manifest, release):
    """Art B: Weiche steamos-switch.sh (fest kopiert) und die Ergänzungsdatei, dann daemon-reload."""
    msgs = []
    sw = os.path.join(p.hook, "steamos-switch.sh")
    r1 = install_own_file(p, manifest, sw, read_bytes(os.path.join(release, "session", "hooks", "steamos-switch.sh")),
                          "hook", mode=0o755, check_bash=True)
    if r1 == "kept-user":
        msgs.append(t("hook_changed", p.show(sw)))
    data = read_bytes(os.path.join(release, "session", "hooks", "steamos-dropin.conf"))
    r2 = install_own_file(p, manifest, p.dropin, data, "dropin")
    if r2 == "kept-user":
        msgs.append(t("hook_changed", p.show(p.dropin)))
        return msgs
    if r2 in ("created", "replaced"):
        system(["systemctl", "--user", "daemon-reload"])
    msgs.append(t("hooked_steamos", p.show(p.dropin)))
    return msgs


def desktop_entries(p, manifest, release):
    ctl = os.path.join(p.hook, "pc5-launcher-ctl")
    install_own_file(p, manifest, ctl, read_bytes(os.path.join(release, "installer", "pc5-launcher-ctl")), "ctl",
                     mode=0o755, check_bash=True)
    # Desktop Entry Spec: Argument in "…", darin \\ " ` $ mit Backslash, % als %%
    quoted = '"%s"' % ctl.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") \
        .replace("%", "%%")
    for name in DESKTOP:
        text = read_bytes(os.path.join(release, "installer", "desktop", name)).decode("utf-8")
        install_own_file(p, manifest, os.path.join(p.apps, name), text.replace("@CTL@", quoted), "desktop")


def hook_present(p, manifest):
    """Steht einer unserer Haken (Datei, Block, Ergänzungsdatei) unverändert da?"""
    return any(item.get("kind") in ("created", "appended", "dropin") and
               file_sha(os.path.realpath(p.abs(item["path"]))) == item.get("sha256") for item in manifest["files"])


def hook_allowed(info, check):
    """(erlaubt, Grund) für das Einhängen."""
    kind = info["session"]["kind"]
    missing = ", ".join(check.get("missing_required") or []) if check else ""
    if kind == "none":
        return False, t("no_session")
    if check is None or not check.get("ok"):
        if info["os"]["id"] == "steamos" or kind == "steamos":
            return False, t("steamos_runtime", missing or "?")
        return False, t("missing_runtime", missing or "?")
    if kind == "steamos" and not info["session"]["unit_ok"]:
        return False, t("unit_bad")
    if kind == "gsp" and not info["session"]["clients"]:
        return False, t("no_clients")
    return True, ""


# ----------------------------------------------------------------------------- Befehle
def describe(info):
    try:
        label = load_module("_pc5_sysdetect", os.path.join(PACKAGE, "app", "couch", "sysdetect.py")).label(info)
    except (Failure, OSError, AttributeError):
        label = info["os"].get("name", "Linux")
    say(t("system", label))
    kind = info["session"]["kind"]
    if kind == "gsp":
        say(t("kind_gsp", ", ".join(info["session"]["clients"]) or "–"))
    elif kind == "steamos":
        say(t("kind_steamos", "" if info["session"]["unit_ok"] else t("unit_odd")))
    else:
        say(t("kind_none"))


def show_check(check, why):
    if check is None:
        say(t("check_run", why))
        return
    if check.get("ok"):
        say(t("check_ok"))
    else:
        say(t("check_bad", ", ".join(check.get("missing_required") or [])))
    for item in check.get("items") or []:
        if not item.get("ok") and not item.get("required"):
            effect = item.get("effect_en" if LANG == "en" else "effect") or ""
            if effect:
                say(t("optional", item.get("name", ""), effect))
    if check.get("packages") and not check.get("ok"):
        say(t("packages", check["packages"]))
    note = check.get("note_en" if LANG == "en" else "note")
    if note and not check.get("ok"):
        say("          " + note)


def detect(pkg):
    sysdetect = load_module("_pc5_sysdetect", os.path.join(pkg, "app", "couch", "sysdetect.py"))
    return sysdetect.detect(os.environ.get("PC5_SYSROOT") or None)


def cmd_install(opts):
    p = Places()
    pkg = PACKAGE
    version = package_version(pkg)
    info = detect(pkg)
    describe(info)
    foreign = foreign_setup(p)
    if foreign and not opts["--adopt"]:
        say(t("foreign"))
        for path in foreign:
            say("          " + p.show(path))
        return 3
    check, why = depcheck(pkg)
    show_check(check, why)
    allowed, reason = hook_allowed(info, check)
    want_hook = opts["--hook"] or (not opts["--no-hook"] and not opts["--yes"])
    if opts["--dry-run"]:
        say(t("dry"))
        same = installed_same(p, pkg)
        say(t("would", t("same", os.path.basename(same)) if same else
              tr("Fassung %s nach %s ablegen und prüfen", "place version %s in %s and check it") % (version, p.show(p.releases))))
        if allowed and want_hook and not opts["--no-hook"]:
            if info["session"]["kind"] == "gsp":
                say(t("would", tr("einhängen in ", "hook into ") + ", ".join(p.show(os.path.join(p.sessions_d, c))
                                                         for c in info["session"]["clients"])))
            else:
                say(t("would", tr("Ergänzungsdatei %s anlegen, systemctl --user daemon-reload",
                                  "create drop-in %s, systemctl --user daemon-reload") % p.show(p.dropin)))
        elif not allowed:
            say(t("not_hooked", reason))
        say(t("would", tr("Desktop-Einträge in ", "desktop entries in ") + p.show(p.apps)))
        return 0
    if foreign:
        say(t("adopt"))

    manifest = load_manifest(p) or {"format": MANIFEST_FORMAT, "product": "PC5 Launcher", "files": [], "dirs": [],
                                    "legacy": [], "installed": time.strftime("%Y-%m-%d %H:%M:%S")}
    before = json.dumps(manifest, sort_keys=True) if os.path.exists(p.manifest) else ""
    created = []
    makedirs_noted(p.data, created)
    note_dirs(manifest, p, created)
    try:
        same = installed_same(p, pkg)
        if same:
            say(t("same", os.path.basename(same)))
            release_name = os.path.basename(same)
        else:
            release_name = place_release(p, pkg, version, manifest)
        release = os.path.join(p.releases, release_name)
        made = []
        makedirs_noted(p.hook, made)
        note_dirs(manifest, p, made)
        desktop_entries(p, manifest, release)
        say(t("desktop"))

        existing = hook_present(p, manifest)
        if not allowed:
            say(t("not_hooked", reason))
        elif existing and opts["--no-hook"]:
            say(t("hook_kept", t("hook_off_hint")))
        else:
            # Ein vorhandener Haken wurde schon einmal erlaubt: Beim Aktualisieren ohne neue Frage erneuern
            # (ersetzt wird trotzdem nur, was noch unseres ist und eine neue Kennzeile hat)
            decision = True if (opts["--hook"] or existing) else (False if opts["--no-hook"] or opts["--yes"] else
                                                                  ask(t("question"), False))
            if decision is None:
                say(t("no_tty"))
            elif not decision:
                say(t("not_hooked", t("declined")))
            elif info["session"]["kind"] == "gsp":
                for line in hook_gsp(p, info, manifest, release):
                    say(line)
            else:
                for line in hook_steamos(p, manifest, release):
                    say(line)
        manifest["hook"] = "on" if hook_present(p, manifest) else ("legacy" if manifest["legacy"] else "off")
        manifest.update({"version": version, "release": release_name, "session": info["session"]["kind"],
                         "adopted": bool(foreign) or bool(manifest.get("adopted"))})
    finally:
        # Ein zweiter Lauf ohne Änderung schreibt nichts (auch nicht die Uhrzeit)
        if os.path.isdir(p.data) and json.dumps(manifest, sort_keys=True) != before:
            manifest["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_manifest(p, manifest)
    say(t("done_hooked") if manifest["hook"] in ("on", "legacy") else t("done_plain"))
    say(t("back"))
    return 0


def cmd_rollback(_opts):
    p = Places()
    if load_manifest(p) is None:
        say(t("nothing"))
        return 3
    prev = os.path.realpath(p.previous) if os.path.lexists(p.previous) else None
    cur = os.path.realpath(p.current) if os.path.lexists(p.current) else None
    if not prev or not is_release(prev, p.releases) or prev == cur:
        say(t("rollback_none"))
        return 1
    link_atomic(p.current, os.path.join("releases", os.path.basename(prev)))
    if cur and is_release(cur, p.releases):
        link_atomic(p.previous, os.path.join("releases", os.path.basename(cur)))
    say(t("rollback", os.path.basename(prev), os.path.basename(cur) if cur else "–"))
    return 0


def cmd_status(_opts):
    p = Places()
    manifest = load_manifest(p)
    if manifest is None:
        say(t("nothing"))
        for path in foreign_setup(p):
            say("          " + p.show(path))
        return 0
    say(tr("PC5 Launcher %s (%s), installiert %s, zuletzt %s", "PC5 Launcher %s (%s), installed %s, last updated %s")
        % (manifest.get("version"), manifest.get("release"), manifest.get("installed"), manifest.get("updated")))
    for name in ("current", "previous"):
        link = getattr(p, name)
        say("%-9s %s" % (name, os.readlink(link) if os.path.islink(link) else "–"))
    say(tr("Sitzung:  %s, Haken %s", "Session:  %s, hook %s") % (manifest.get("session"), manifest.get("hook")))
    for item in manifest["files"]:
        path = p.abs(item["path"])
        now = file_sha(path)
        state = "ok" if now == item.get("sha256") else (tr("fehlt", "missing") if now is None else tr("verändert", "changed"))
        say("  %-10s %-8s %s" % (item.get("kind"), state, item["path"]))
    off = os.path.lexists(os.path.join(p.config, "disabled"))
    say(tr("Notschalter: %s", "Kill switch: %s") % (tr("GESETZT", "SET") if off else tr("aus", "off")))
    return 0


def restore_appended(p, item):
    """Unseren Block wieder abtrennen, nur wenn die Datei noch genau so ist wie nach dem Anhängen."""
    path = p.abs(item["path"])
    real = os.path.realpath(path)
    if file_sha(real) != item.get("sha256"):
        return False
    data = read_bytes(real)
    suffix = item.get("suffix", "").encode("utf-8")
    if not suffix or not data.endswith(suffix):
        return False
    old = data[:-len(suffix)]
    if sha256_bytes(old) != item.get("before"):
        return False
    write_atomic(real, old, stat.S_IMODE(os.stat(real).st_mode))
    return True


def to_trash(p, path):
    """Ordner oder Datei per rename in den XDG-Papierkorb (wiederherstellbar). Nie kopieren, nie löschen."""
    if not os.path.lexists(path):
        return None
    files = os.path.join(p.trash, "files")
    info = os.path.join(p.trash, "info")
    os.makedirs(files, exist_ok=True)
    os.makedirs(info, exist_ok=True)
    base = os.path.basename(path.rstrip("/"))
    name, n = base, 1
    while os.path.lexists(os.path.join(files, name)) or os.path.lexists(os.path.join(info, name + ".trashinfo")):
        n += 1
        name = "%s.%d" % (base, n)
    with open(os.path.join(info, name + ".trashinfo"), "x", encoding="utf-8") as f:
        f.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (urllib.parse.quote(os.path.abspath(path)),
                                                           time.strftime("%Y-%m-%dT%H:%M:%S")))
    try:
        os.rename(path, os.path.join(files, name))
    except OSError:
        remove_file(os.path.join(info, name + ".trashinfo"))
        return None
    return os.path.join(files, name)


def cmd_uninstall(opts):
    p = Places()
    manifest = load_manifest(p)
    if manifest is None:
        foreign = foreign_setup(p)
        if foreign:
            say(t("foreign"))
            return 3
        say(t("nothing"))
        return 0
    if opts["--dry-run"]:
        say(t("dry"))
        for item in manifest["files"]:
            say(t("would", tr("entfernen: ~/", "remove: ~/") + item["path"]))
        say(t("would", tr("entfernen: ~/%s (nur Fassungen mit Marke)", "remove: ~/%s (only versions with our marker)") % p.rel(p.releases)))
        return 0
    answer = ask(t("confirm_uninstall"), opts["--yes"])
    if not answer:
        if answer is None:
            say(t("no_tty"))
        return 3
    removed, kept, trashed = [], [], []
    reload_needed = False
    for item in reversed(manifest["files"]):
        rel = item["path"]
        path = p.abs(rel)
        kind = item.get("kind")
        if kind == "appended":
            if restore_appended(p, item):
                removed.append(rel + " (nur unser Block)")
            elif os.path.lexists(path):
                kept.append(rel + " (verändert, Block bitte von Hand entfernen)")
            continue
        if not os.path.lexists(path):
            continue
        if file_sha(path) == item.get("sha256") and remove_file(path):
            removed.append(rel)
            reload_needed = reload_needed or kind == "dropin"
            continue
        kept.append(rel + " (verändert)")
    if reload_needed:
        system(["systemctl", "--user", "daemon-reload"])
    busy = in_use(p)
    for _age, name in releases_list(p):
        if name in busy:
            kept.append(p.rel(os.path.join(p.releases, name)) + " (läuft gerade)")
            continue
        if remove_own_tree(os.path.join(p.releases, name), RELEASE_MARK, p.releases):
            removed.append(p.rel(os.path.join(p.releases, name)))
    for link in (p.current, p.previous):
        if os.path.islink(link):
            target = os.path.realpath(link)
            if not os.path.exists(target):
                remove_file(link)
                removed.append(p.rel(link))
    for folder in (p.releases, p.hook):           # auch nach --adopt (Ordner aus tools/install-release), nur leer
        try:
            os.rmdir(folder)
            removed.append(p.rel(folder) + "/")
        except OSError:
            pass
    flatpaks = setup_flatpaks(p)
    remove_file(p.manifest)
    removed.append(p.rel(p.manifest))

    def rmdir_recorded(final):
        # nur selbst angelegte Ordner, nur leere (rmdir), tiefste zuerst
        for rel in sorted(manifest["dirs"], key=lambda r: -r.count("/")):
            folder = p.abs(rel)
            try:
                os.rmdir(folder)
                removed.append(rel + "/")
            except OSError:
                if final and os.path.isdir(folder):
                    kept.append(rel + "/ (nicht leer)")
    rmdir_recorded(False)
    data_dirs = [p.config, p.data, p.state, p.cache, p.wireplumber]
    if opts["--purge-data"] and ask(t("confirm_purge"), opts["--yes"]):
        for path in data_dirs:
            moved = to_trash(p, path)
            if moved:
                trashed.append(p.rel(path))
            elif os.path.lexists(path):
                kept.append(p.rel(path) + " (nicht in den Papierkorb verschiebbar)")
    rmdir_recorded(True)
    kept += [p.rel(x) for x in data_dirs if os.path.lexists(x) and p.rel(x) + "/ (nicht leer)" not in kept]
    for rel in removed:
        say(t("removed", "~/" + rel))
    for rel in kept:
        say(t("kept", "~/" + rel))
    for rel in trashed:
        say(t("trashed", "~/" + rel))
    for item in manifest.get("legacy", []):
        say(t("untouched", "~/" + item["path"] + " (Haken aus tools/install-release)"))
    for name in UNTOUCHED:
        say(t("untouched", name))
    for app in flatpaks:
        say(t("flatpak_hint", app))
    return 0


def setup_flatpaks(p):
    """Flatpaks, die der Einrichtungs-Assistent installiert hat (<config>/setup.json); nur gelesen."""
    try:
        with open(os.path.join(p.config, "setup.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for item in data.get("items") or [] if isinstance(data, dict) else []:
        app = item.get("flatpak") if isinstance(item, dict) else None
        if isinstance(app, str) and re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", app) and app not in out:
            out.append(app)
    return out


USAGE = __doc__.split("\n\n")[1]


def parse(argv):
    if not argv:
        return None, None
    command = argv[0]
    allowed = {"install": ("--yes", "--hook", "--no-hook", "--dry-run", "--no-network", "--status", "--rollback",
                           "--adopt"),
               "uninstall": ("--yes", "--purge-data", "--dry-run"),
               "status": (), "rollback": ()}
    if command not in allowed:
        return None, None
    opts = {name: False for names in allowed.values() for name in names}
    for arg in argv[1:]:
        if arg not in allowed[command]:
            return None, None
        opts[arg] = True
    if opts["--hook"] and opts["--no-hook"]:
        return None, None
    if command == "install" and opts["--status"]:
        command = "status"
    elif command == "install" and opts["--rollback"]:
        command = "rollback"
    return command, opts


def main(argv):
    command, opts = parse(argv[1:])
    if command is None:
        sys.stderr.write(USAGE + "\n")
        return 2
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    try:
        return {"install": cmd_install, "uninstall": cmd_uninstall, "status": cmd_status,
                "rollback": cmd_rollback}[command](opts)
    except Failure as e:
        say(t("fail", e))
        return 1
    except KeyboardInterrupt:
        say(tr("abgebrochen", "aborted"))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
