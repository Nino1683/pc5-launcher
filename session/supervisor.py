#!/usr/bin/python3 -I
"""couch-Supervisor – hält die Game-Mode-Sitzung am Leben (Bauplan Abschnitt 3.3).

Läuft als Client der Sitzung (gestartet von couch-session) und kennt zwei Zustände:

  HOME   Das Home läuft als Kind. Überwacht werden Bereit-Signal, Herzschlag und Abstürze.
  STEAM  Steam Big Picture läuft als Kind (Übergabe aus dem Home oder Notgriff). Danach
         startet das Home wieder.

Rückfallebene: Release "current" -> Release "previous" -> Steam. Gibt der Supervisor das Home
endgültig auf (Sicherung, Notschalter), ersetzt er sich selbst durch den Rückfall-Befehl
(execvp): Die Sitzung ist dann genau die von Bazzite.

Nur Standardbibliothek, eine poll-Schleife, keine Threads: Diese Datei muss auch dann laufen,
wenn GTK, WebKit oder ein Python-Modul nach einem Systemupdate fehlen. Bei jedem eigenen
Fehler: Home beenden, dann execvp des Rückfall-Befehls. Nie stumm oder schnell enden, sonst
zählt gamescope-session-plus eine Kurzsitzung.

  supervisor.py             normaler Lauf (als Client der Sitzung)
  supervisor.py --status    Zustand anzeigen
  supervisor.py --reset     Sicherungen und Fehlstart-Zähler löschen
  supervisor.py --selftest  prüft nur, ob alles Nötige ladbar ist (für couch-session)

Rückgabewerte des normalen Laufs: 0 = Sitzung gewollt beendet, 2 = nicht in einer Game-Mode-Sitzung
gestartet (GAMESCOPE_WAYLAND_DISPLAY fehlt; nichts getan), 3 = Gamescope bzw. sein X-Server ist
weg (kein Fehlstart, keine Sicherung), sonst der Rückgabewert von Steam.

Dateien: <run>/ready, beat, handover.json, exiting (schreibt das Home), session.env und
releases-in-use (schreiben wir), control.sock (gehört dem Home); <state>/supervisor.log,
failed-starts, fuse-launcher, fuse-current, last-good, deliberate-exit, sink-before, bt-off-trial
(Probezeit „Bluetooth aus“ des Home), writes.log (nur im Prüfmodus); <config>/disabled
(Notschalter, nur von Hand), <config>/start-steam (weiche Wahl: Sitzung beginnt mit Steam).

<state>/last-good ("<Kennung>\n<Ordner>") nennt die Fassung, deren Home zuletzt in Stufe current
bereit wurde (reißt sie danach die Absturzgrenze, wird es gelöscht); <run>/releases-in-use die Ordner, aus denen diese Sitzung ein Home startet (eigene
Fassung, previous). Beides liest tools/install-release: previous zeigt nie auf eine als kaputt
bekannte Fassung, und Aufräumen lässt laufende Fassungen stehen (E99).

<run>/exiting ("<code> <pid> <zeit>") schreibt das Home als Erstes, wenn es gewollt endet. Hängt
es danach beim Beenden, wartet der Supervisor COUCH_EXIT_WAIT, beendet es dann hart und wertet
den angekündigten Code aus (0 = Abmelden, 10 = Übergabe an Steam) statt einen Hänger zu zählen.

Umgebungsvariablen (Vorgabe in Klammern; alles für Tests umstellbar):
  COUCH_TEST_ROOT=<R>         alle Orte unter <R>; schreibende Systembefehle werden nur protokolliert
  COUCH_TEST=1                Prüfmodus: keine echten Eingabegeräte, nie das echte Steam
  COUCH_HOME_CMD, COUCH_PREVIOUS_HOME_CMD   Befehl statt <release>/app/couch-home bzw. previous
  COUCH_FALLBACK_CMD          Rückfall-Befehl (steam -gamepadui -steamos3 -steampal -steamdeck)
  COUCH_STEAM_SHUTDOWN_CMD    Steam beenden (steam -shutdown)
  COUCH_READY_TIMEOUT (30)    COUCH_BEAT_MAX_AGE (25)    COUCH_CRASH_WINDOW (600)   COUCH_MAX_FAILS (3)
  COUCH_KILL_GRACE (8)        COUCH_CHORD_HOLD (5)       COUCH_STEAM_GAME_WAIT (180)
  COUCH_STEAM_GAME_GONE (5)   COUCH_STEAM_SHUTDOWN_WAIT (30)   COUCH_STEAM_SCAN (1)
  COUCH_POWEROFF_WAIT (60)    COUCH_RESTART_DELAY (1)    COUCH_TICK (0.25)          COUCH_RESCAN (2)
  COUCH_WATCH_DEVICES         nur im Prüfmodus: Dateien/FIFOs statt der Eingabegeräte, mit Komma getrennt
  COUCH_SHORT_SESSION_TRACKER Zählerdatei der Sitzung (/tmp/chimeraos-short-session-tracker)
  COUCH_PACTL                 pactl-Befehl (pactl)
  COUCH_POWER_CONFIRM (5)     Einschaltknopf (E38): ohne lebendes Home schaltet erst ein zweiter Druck
                              binnen so vieler Sekunden aus; mit lebendem Home geht jeder Druck ans
                              Home (dort fragt der Kern nach, das Home liest dieselbe Variable)
  COUCH_POWER_WAKE_GUARD (3)  so viele Sekunden nach dem Aufwachen aus dem Ruhemodus gilt der Einschaltknopf nicht
  COUCH_EXIT_WAIT (20)        so lange darf ein Home mit <run>/exiting für sein Ende brauchen
  COUCH_TEST_RAISE            nur im Prüfmodus: absichtlicher Fehler bei start|loop|home|steam|watcher
  COUCH_X_SOCKET_DIR          nur im Prüfmodus: Ordner der X-Sockets statt /tmp/.X11-unix

SteamOS 3 (E104 d, nur mit COUCH_PLATFORM=steamos aus der Ergänzungsdatei bzw. steamos-switch.sh; ohne diesen
Wert gilt alles oben unverändert). Valves gamescope-session.target hält steam-launcher.service am Laufen
(Upholds=), und Valves Zähler packt Steam nach drei Läufen unter 60 s neu aus. Darum endet der Supervisor
dort nur auf SIGTERM:
  Home endet mit 0        -> steamosctl switch-to-desktop-mode, dann auf SIGTERM warten (COUCH_DESKTOP_WAIT, 60;
                             kommt keins, Home neu); scheitert der Wechsel: Valves Launcher als Kind
  eigener Rückfall        -> Valves Launcher (COUCH_FALLBACK_CMD) als Kind statt exec; endet er von selbst,
                             wird der Supervisor nach COUCH_VALVE_RESTART_WAIT (5) zu ihm (exec)
  Gamescope weg           -> kein Ende mit 3, nach COUCH_STEAMOS_LOST_WAIT (30) ohne SIGTERM Home neu
  Steam will ausschalten  -> wie sonst über logind, dann auf SIGTERM warten (COUCH_POWEROFF_WAIT)
  Schnellstart-Sicherung  -> <state>/steamos-quick: Zeit eines Starts, dessen Home noch nicht bereit war; findet
                             der nächste Start eine binnen COUCH_STEAMOS_QUICK_WINDOW (180) s, setzt er
                             fuse-launcher und startet Valves Launcher (vor Valves drittem Kurzlauf)
  Steam für Übergaben     -> COUCH_STEAM_CMD (steam -steamos3 -steampal -steamdeck -gamepadui): Valves Launcher
                             nimmt keine steam://-Adresse an
Kein Kurzsitzungs-Zähler wird dort je angefasst, auch nicht Valves /tmp/steamos-short-session-tracker.
"""
import ctypes
import fcntl
import json
import os
import re
import select
import shlex
import signal
import socket
import stat
import struct
import sys
import time
import traceback

# Die unveränderte Sitzungsumgebung, sofort festgehalten: Genau sie bekommt Steam.
SESSION_ENV = dict(os.environ)
SESSION_ENVB = dict(os.environb)

SELF = os.path.realpath(__file__)          # realpath: "current" ist ein Verweis und kann sich ändern
SESSION_DIR = os.path.dirname(SELF)
RELEASE = os.path.dirname(SESSION_DIR)

DEFAULT_FALLBACK = "steam -gamepadui -steamos3 -steampal -steamdeck"
DEFAULT_SHUTDOWN = "steam -shutdown"
DEFAULT_TRACKER = "/tmp/chimeraos-short-session-tracker"
DEFAULT_STEAMOS_STEAM = "steam -steamos3 -steampal -steamdeck -gamepadui"   # E104 d: wie Valves steam-launcher
NOOP = ["/usr/bin/true"]                   # ersetzt im Prüfmodus alles, was das echte Steam wäre
X_SOCKET_DIR = "/tmp/.X11-unix"

EXIT_NOT_IN_SESSION = 2                    # von Hand gestartet, außerhalb der Game-Mode-Sitzung
EXIT_NO_DISPLAY = 3                        # Gamescope weg; dieselbe Zahl meldet das Home (main.EXIT_NO_DISPLAY)

# Was nur für Steam gedacht ist und dem Home schadet (Bauplan 3.3, Bericht 01 Abschnitt 8.2)
HOME_STRIP = ("GTK_IM_MODULE", "QT_IM_MODULE", "XCURSOR_THEME", "XCURSOR_SCALE",
              "WINEDLLOVERRIDES", "SRT_URLOPEN_PREFER_STEAM", "MANGOHUD", "LD_PRELOAD")

PR_SET_CHILD_SUBREAPER = 36

# Eingabe (linux/input-event-codes.h)
EV_SYN, EV_KEY, SYN_DROPPED = 0, 1, 3
KEY_A, KEY_POWER = 30, 116
BTN_SOUTH, BTN_SELECT, BTN_START, BTN_MODE = 304, 314, 315, 316
CHORD = frozenset((BTN_MODE, BTN_SELECT, BTN_START))     # PS + Create + Options
INPUT_EVENT = struct.Struct("llHHi")                     # struct input_event, 24 Byte auf 64 Bit
LONG_BITS = struct.calcsize("l") * 8
EVIOCGKEY = (2 << 30) | (96 << 16) | (ord("E") << 8) | 0x18   # Tastenzustand, 768 Bit
STEAM_VIRTUAL_PAD = ("28de", "11ff")
BUS_HOST = "0019"                                        # ACPI-Knöpfe (Power Button), nicht USB/Bluetooth
BT_ADAPTER = re.compile(r"^/org/bluez/hci[0-9]{1,3}$")    # wie settings/backends/bluez.valid_adapter_path

# Eigene App-IDs gestarteter Apps (E3): nur reaper mit solchen IDs räumt der Supervisor ab
OWN_APPID_MIN, OWN_APPID_MAX = 0x7F100000, 0x7FFFFFFF

LOG_LIMIT = 512 * 1024
URI_OK = re.compile(r"^steam://[A-Za-z0-9/_.:?=&%+,~@-]{1,400}$")
# Schalter aus Einstellungen › Spiele, die das Home für ein Steam-Spiel mitgibt (handover.json "env").
# Steam reicht seine Umgebung an das Spiel weiter; nur diese Namen, nur einfache Werte.
HANDOVER_ENV = ("PROTON_FSR4_UPGRADE", "FSR4_UPGRADE")   # HDR regelt Big Picture selbst
HANDOVER_ENV_VALUE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


# ----------------------------------------------------------------------------- kleine Helfer
def env_number(env, name, default, minimum=0.0):
    try:
        value = float(env.get(name, ""))
    except ValueError:
        return default
    return value if value >= minimum else default      # NaN fällt hier auch durch


def read_first_line(path):
    """Erste Zeile ohne Rand, "" bei leerer Datei, None wenn nicht lesbar."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def write_atomic(path, text, mode=0o600):
    """Schreibt über eine Nachbardatei und rename. False statt Ausnahme: Zustand ist nur Beiwerk."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def remove_file(path):
    """Einzige Löschfunktion: nur eine einzelne Datei oder ein Verweis, nie ein Ordner."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def which(cmd, env):
    if os.sep in cmd:
        return cmd
    for folder in (env.get("PATH") or os.defpath).split(os.pathsep):
        path = os.path.join(folder or ".", cmd)
        if os.access(path, os.X_OK) and not os.path.isdir(path):
            return path
    return None


def x_socket_path(display, folder=X_SOCKET_DIR):
    """Socket-Datei zu einem lokalen DISPLAY (":1", ":1.0"); None, wenn der Name anders aussieht."""
    m = re.fullmatch(r":(\d+)(?:\.\d+)?", display or "")
    return os.path.join(folder, "X" + m.group(1)) if m else None


def x_socket_dead(path):
    """True nur, wenn sicher niemand mehr an diesem X-Socket lauscht (Datei fehlt oder Verbindung
    abgelehnt). Zeitüberschreitung und alles Unklare zählen als "lebt": Im Zweifel bleibt es
    beim gewohnten Ablauf mit Fehlstart-Zähler."""
    addresses = [path]
    dirfd = None
    try:
        if len(os.fsencode(path)) > 100:        # sun_path fasst 107 Byte (nur in Tests so lang)
            try:
                dirfd = os.open(os.path.dirname(path), os.O_PATH | os.O_DIRECTORY)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            addresses = ["/proc/self/fd/%d/%s" % (dirfd, os.path.basename(path))]
        else:
            addresses.append("\0" + path)       # Xwayland lauscht je nach Fassung auch abstrakt
        for address in addresses:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            try:
                sock.settimeout(0.5)
                sock.connect(address)
                return False
            except (FileNotFoundError, ConnectionRefusedError):
                continue
            except OSError:
                return False
            finally:
                sock.close()
        return True
    finally:
        if dirfd is not None:
            os.close(dirfd)


def crash_clock():
    """Uhr der Absturzgrenze: BOOTTIME zählt den Ruhemodus mit, MONOTONIC nicht. Mit der monotonen
    Uhr lagen zwei Abstürze vom Abend und einer am Morgen nach dem Aufwachen „binnen 600 s“ (E99)."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def release_id(release=RELEASE):
    """Kennung der Fassung; sessions.d-steam rechnet dieselbe aus (VERSION@Ordnername)."""
    version = read_first_line(os.path.join(release, "VERSION")) or "0"
    return "%s@%s" % (version, os.path.basename(release))


class Paths:
    """Dieselbe Regel wie app/couch/paths.py (kein gemeinsamer Code: nur Standardbibliothek)."""

    def __init__(self, env):
        root = env.get("COUCH_TEST_ROOT")
        if root:
            self.config, self.data, self.state, self.run, self.cache = (
                os.path.join(root, name) for name in ("config", "data", "state", "run", "cache"))
        else:
            home = os.path.expanduser("~")
            runtime = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
            self.config = os.path.join(home, ".config", "couch")
            self.data = os.path.join(home, ".local", "share", "couch")
            self.state = os.path.join(home, ".local", "state", "couch")
            self.run = os.path.join(runtime, "couch")
            self.cache = os.path.join(home, ".cache", "couch")
        self.disabled = os.path.join(self.config, "disabled")
        self.start_steam = os.path.join(self.config, "start-steam")
        self.sink_before = os.path.join(self.state, "sink-before")
        self.log = os.path.join(self.state, "supervisor.log")
        self.writes = os.path.join(self.state, "writes.log")
        self.fails = os.path.join(self.state, "failed-starts")
        self.deliberate = os.path.join(self.state, "deliberate-exit")
        self.bt_trial = os.path.join(self.state, "bt-off-trial")
        self.last_good = os.path.join(self.state, "last-good")
        self.in_use = os.path.join(self.run, "releases-in-use")
        self.session_env = os.path.join(self.run, "session.env")
        self.exiting = os.path.join(self.run, "exiting")
        self.steamos_quick = os.path.join(self.state, "steamos-quick")    # E104 d, nur SteamOS

    def fuse(self, name):
        return os.path.join(self.state, name)


# ----------------------------------------------------------------------------- Prozesse über /proc
def proc_table():
    """{pid: (ppid, comm, startzeit, zustand)} aller Prozesse."""
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as f:
                raw = f.read()
            close = raw.rindex(b")")               # comm darf Klammern und Leerzeichen enthalten
            rest = raw[close + 2:].split()
            table[int(name)] = (int(rest[1]), raw[raw.index(b"(") + 1:close].decode("utf-8", "replace"),
                                rest[19].decode(), rest[0].decode())
        except (OSError, ValueError, IndexError):
            continue
    return table


def descendants(table, root):
    kids = {}
    for pid, row in table.items():
        kids.setdefault(row[0], []).append(pid)
    out, todo = [], [root]
    while todo:
        for kid in kids.get(todo.pop(), ()):
            out.append(kid)
            todo.append(kid)
    return out


def reaper_appid(pid, comm):
    """App-ID eines "reaper SteamLaunch AppId=<n> -- …", sonst None. Gleiche Regel wie Gamescope."""
    if comm != "reaper":
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().split(b"\0")
    except OSError:
        return None
    if b"--" in args:
        args = args[:args.index(b"--")]
    if b"SteamLaunch" not in args:
        return None
    for arg in args[args.index(b"SteamLaunch") + 1:]:
        if arg.startswith(b"AppId="):
            try:
                return int(arg[6:].decode("ascii", "replace"), 0)
            except ValueError:
                return -1
    return None


def same_process(pid, start):
    """Lebt genau dieser Prozess noch (PID und Startzeit, kein Zombie)?"""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            raw = f.read()
        rest = raw[raw.rindex(b")") + 2:].split()
        return rest[19].decode() == start and rest[0] != b"Z"
    except (OSError, ValueError, IndexError):
        return False


def send_signal(pid, signum):
    try:
        os.kill(pid, signum)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------------- Eingabe-Wächter
def parse_input_devices(text):
    """Aus /proc/bus/input/devices: [(eventN, "pad"|"power", name)].

    pad   = Gerät mit BTN_SOUTH und allen drei Tasten des Notgriffs, außer Steams virtuellem Pad
    power = Einschaltknopf am Gehäuse: KEY_POWER an einem ACPI-Gerät (Bus 0019). Tastaturen
            melden die Taste auch (als USB-Gerät); die zählen nicht, wie bisher unter Steam.
    """
    found = []
    for block in text.split("\n\n"):
        bus, ids, name, node, keys = "", (None, None), "", None, 0
        for line in block.splitlines():
            if line.startswith("I:"):
                m = re.search(r"Bus=([0-9a-fA-F]+)\s+Vendor=([0-9a-fA-F]+)\s+Product=([0-9a-fA-F]+)", line)
                if m:
                    bus, ids = m.group(1).lower(), (m.group(2).lower(), m.group(3).lower())
            elif line.startswith("N: Name="):
                name = line[8:].strip().strip('"')
            elif line.startswith("H: Handlers="):
                for handler in line.split("=", 1)[1].split():
                    if re.fullmatch(r"event\d+", handler):
                        node = handler
            elif line.startswith("B: KEY="):
                try:
                    for word in line.split("=", 1)[1].split():
                        keys = (keys << LONG_BITS) | int(word, 16)
                except ValueError:
                    keys = 0
        if node is None or not keys or ids == STEAM_VIRTUAL_PAD:
            continue
        has = lambda code: bool(keys >> code & 1)
        if has(BTN_SOUTH) and all(has(code) for code in CHORD):
            found.append((node, "pad", name))
        elif has(KEY_POWER) and bus == BUS_HOST and not has(KEY_A):
            found.append((node, "power", name))
    return found


class Device:
    __slots__ = ("path", "kind", "fd", "buf", "down", "since", "fired", "polled")

    def __init__(self, path, kind, fd, polled):
        self.path, self.kind, self.fd, self.polled = path, kind, fd, polled
        self.buf = b""
        self.down = set()        # gedrückte Tasten des Notgriffs
        self.since = None        # seit wann alle drei gedrückt sind
        self.fired = False


class Watcher:
    """Liest Gamepads und Einschaltknöpfe roh (24-Byte-Ereignisse), ohne python-evdev, ohne Grab.

    Im Prüfmodus wird KEIN echtes Gerät geöffnet (sonst steuert der echte Controller die Tests);
    gelesen wird dann nur aus den Dateien/FIFOs in COUCH_WATCH_DEVICES.
    """

    def __init__(self, sup):
        self.sup = sup
        self.devices = {}        # fd -> Device
        self.next_scan = 0.0
        self.complained = set()
        self.last_power = -1e9
        self.power_pending = False

    # -- Geräte finden
    def wanted(self):
        if self.sup.test:
            out = {}
            for path in self.sup.env.get("COUCH_WATCH_DEVICES", "").split(","):
                path = path.strip()
                if not path:
                    continue
                if os.path.realpath(path).startswith("/dev/"):
                    self.complain(path, "Prüfmodus: echte Geräte werden nicht geöffnet")
                    continue
                out[path] = "any"
            return out
        try:
            with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return {}
        return {"/dev/input/" + node: kind for node, kind, _name in parse_input_devices(text)}

    def complain(self, path, text):
        if path not in self.complained:
            self.complained.add(path)
            self.sup.log("Wächter: %s: %s" % (path, text))

    def scan(self):
        wanted = self.wanted()
        for dev in list(self.devices.values()):
            if dev.path not in wanted:
                self.drop(dev, "nicht mehr vorhanden")
        have = {dev.path for dev in self.devices.values()}
        for path, kind in wanted.items():
            if path not in have:
                self.open(path, kind)

    def open(self, path, kind):
        try:
            mode = os.stat(path).st_mode
            if stat.S_ISCHR(mode) and not self.sup.test:
                flags, polled = os.O_RDONLY, True
            elif stat.S_ISFIFO(mode) and self.sup.test:
                flags, polled = os.O_RDWR, True       # RDWR: sonst meldet poll ohne Schreiber dauernd HUP
            elif stat.S_ISREG(mode) and self.sup.test:
                flags, polled = os.O_RDONLY, False    # poll meldet Dateien immer lesbar: im Takt lesen
            else:
                self.complain(path, "kein lesbarer Gerätetyp")
                return
            fd = os.open(path, flags | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as e:                          # EACCES, ENOENT, ENODEV: beim nächsten Suchen wieder
            self.complain(path, "nicht zu öffnen (%s)" % e.strerror)
            return
        dev = Device(path, kind, fd, polled)
        self.devices[fd] = dev
        self.complained.discard(path)
        if polled:
            self.sup.poller.register(fd, select.POLLIN)
        if not self.sup.test:
            self.resync(dev, time.monotonic())
        self.sup.log("Wächter: lese %s (%s)" % (path, kind))

    def drop(self, dev, why):
        if self.devices.pop(dev.fd, None) is None:
            return
        if dev.polled:
            try:
                self.sup.poller.unregister(dev.fd)
            except (KeyError, OSError, ValueError):
                pass
        try:
            os.close(dev.fd)
        except OSError:
            pass
        self.sup.log("Wächter: %s geschlossen (%s)" % (dev.path, why))

    def close_all(self):
        for dev in list(self.devices.values()):
            self.drop(dev, "Ende")

    # -- lesen
    def resync(self, dev, now):
        """Tastenzustand vom Kernel holen: beim Öffnen und nach verlorenen Ereignissen."""
        if dev.kind == "power":
            return
        state = bytearray(96)
        try:
            fcntl.ioctl(dev.fd, EVIOCGKEY, state)
        except OSError:
            return
        dev.down = {code for code in CHORD if state[code // 8] >> (code % 8) & 1}
        self.update_chord(dev, now)

    def update_chord(self, dev, now):
        if len(dev.down) == len(CHORD):
            if dev.since is None:
                dev.since, dev.fired = now, False
        else:
            dev.since, dev.fired = None, False

    def on_fd(self, fd, flags, now):
        dev = self.devices.get(fd)
        if dev is None:
            return False
        if flags & select.POLLIN:
            self.read(dev, now)
        elif flags & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
            self.drop(dev, "Gerät weg")
        return True

    def read(self, dev, now):
        try:
            data = os.read(dev.fd, INPUT_EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError as e:                          # ENODEV: Pad ausgeschaltet oder abgezogen
            self.drop(dev, e.strerror)
            return
        if not data:
            if dev.polled and not self.sup.test:
                self.drop(dev, "Ende des Geräts")
            return
        data = dev.buf + data
        whole = len(data) - len(data) % INPUT_EVENT.size
        dev.buf = data[whole:]
        for offset in range(0, whole, INPUT_EVENT.size):
            _sec, _usec, etype, code, value = INPUT_EVENT.unpack_from(data, offset)
            if etype == EV_KEY:
                self.key(dev, code, value, now)
            elif etype == EV_SYN and code == SYN_DROPPED:
                self.resync(dev, now)

    def key(self, dev, code, value, now):
        if code in CHORD and dev.kind in ("pad", "any"):
            if value:
                dev.down.add(code)
            else:
                dev.down.discard(code)
            self.update_chord(dev, now)
        elif code == KEY_POWER and value == 1 and dev.kind in ("power", "any"):
            # Ein Druck kann an zwei Knoten ankommen (PNP0C0C und LNXPWRBN): entprellen
            if now - self.last_power >= 1.0:
                self.last_power = now
                self.power_pending = True

    def tick(self, now):
        """Sucht Geräte, liest Dateien ohne poll und meldet, was zu tun ist: [("chord", pfad) | ("power", pfad)].

        Der Wächter handelt nicht selbst: Ein Fehler hier darf nur den Wächter kosten, nicht die
        Sitzung (siehe Supervisor.watch).
        """
        if now >= self.next_scan:
            self.next_scan = now + self.sup.rescan
            self.scan()
        for dev in list(self.devices.values()):
            if not dev.polled:
                self.read(dev, now)
        actions = []
        for dev in self.devices.values():
            if dev.since is not None and not dev.fired and now - dev.since >= self.sup.chord_hold:
                dev.fired = True
                actions.append(("chord", dev.path))
                break
        if self.power_pending:
            self.power_pending = False
            actions.append(("power", ""))
        return actions


# ----------------------------------------------------------------------------- Supervisor
class Supervisor:
    HOME, STEAM = "HOME", "STEAM"

    def __init__(self, env=None):
        self.env = dict(SESSION_ENV if env is None else env)
        e = self.env
        self.test = bool(e.get("COUCH_TEST_ROOT")) or e.get("COUCH_TEST") == "1"
        self.paths = Paths(e)
        self.id = release_id()
        self.state = "START"
        self.started = time.monotonic()

        self.ready_timeout = env_number(e, "COUCH_READY_TIMEOUT", 30.0, 0.1)
        self.beat_max_age = env_number(e, "COUCH_BEAT_MAX_AGE", 25.0, 0.1)
        self.crash_window = env_number(e, "COUCH_CRASH_WINDOW", 600.0, 1.0)
        self.max_fails = int(env_number(e, "COUCH_MAX_FAILS", 3, 1))
        self.kill_grace = env_number(e, "COUCH_KILL_GRACE", 8.0, 0.1)
        self.chord_hold = env_number(e, "COUCH_CHORD_HOLD", 5.0, 0.1)
        self.steam_game_wait = env_number(e, "COUCH_STEAM_GAME_WAIT", 180.0, 0.1)
        # Kommt nach dem Start von Big Picture kein Spiel, die Adresse zu diesen Zeitpunkten (s)
        # erneut schicken: Big Picture verschluckt sie beim Kaltstart manchmal (E53)
        raw = e.get("COUCH_STEAM_RESEND", "15,35")
        try:
            self.steam_resend_after = sorted(float(x) for x in raw.split(",") if x.strip())
        except ValueError:
            self.steam_resend_after = [15.0, 35.0]
        # Nicht im Bauplan benannt, aber für Tests ebenso verkürzbar:
        self.steam_game_gone = env_number(e, "COUCH_STEAM_GAME_GONE", 5.0, 0.0)       # reaper so lange weg = Spiel zu Ende
        self.steam_shutdown_wait = env_number(e, "COUCH_STEAM_SHUTDOWN_WAIT", 30.0, 0.1)
        self.steam_scan = env_number(e, "COUCH_STEAM_SCAN", 1.0, 0.05)                # Abstand der /proc-Suche
        self.poweroff_wait = env_number(e, "COUCH_POWEROFF_WAIT", 60.0, 0.1)
        self.restart_delay = env_number(e, "COUCH_RESTART_DELAY", 1.0, 0.0)
        self.tick = env_number(e, "COUCH_TICK", 0.25, 0.01)
        self.rescan = env_number(e, "COUCH_RESCAN", 2.0, 0.05)
        self.power_confirm = env_number(e, "COUCH_POWER_CONFIRM", 5.0, 0.1)
        self.power_wake_guard = env_number(e, "COUCH_POWER_WAKE_GUARD", 3.0, 0.0)
        self.exit_wait = env_number(e, "COUCH_EXIT_WAIT", 20.0, 0.1)

        self.fallback_cmd = self.command("COUCH_FALLBACK_CMD", DEFAULT_FALLBACK)
        self.shutdown_cmd = self.command("COUCH_STEAM_SHUTDOWN_CMD", DEFAULT_SHUTDOWN)
        self.pactl = e.get("COUCH_PACTL") or "pactl"

        # E104 d: SteamOS. Ohne COUCH_PLATFORM=steamos sind steam_cmd und fallback_cmd dasselbe wie bisher.
        self.steamos = e.get("COUCH_PLATFORM") == "steamos"
        self.steam_cmd = (self.command("COUCH_STEAM_CMD", DEFAULT_STEAMOS_STEAM) if self.steamos
                          else self.fallback_cmd)
        self.desktop_wait = env_number(e, "COUCH_DESKTOP_WAIT", 60.0, 0.1)
        self.lost_wait = env_number(e, "COUCH_STEAMOS_LOST_WAIT", 30.0, 0.1)
        self.quick_window = env_number(e, "COUCH_STEAMOS_QUICK_WINDOW", 180.0, 1.0)
        self.valve_restart_wait = env_number(e, "COUCH_VALVE_RESTART_WAIT", 5.0, 0.0)
        self.valve = False            # Steam-Kind ist Valves Launcher (Rückfall), nicht die Übergabe
        self.valve_exec_at = None     # Valves Launcher endete von selbst: ab dann exec zu ihm
        self.hold_until = None        # wartet auf SIGTERM (Desktop-Wechsel, Gamescope weg); danach Home neu
        self.hold_why = ""
        self.quick_noted = False

        # Kinder
        self.known = set()            # PIDs, deren Rückgabewert jemand abholt
        self.exited = {}              # pid -> Rückgabewert
        self.home_pid = None
        self.home_spawned = None      # PID des zuletzt gestarteten Home (auch nach seinem Ende)
        self.steam_pid = None
        self.wake_r = self.wake_w = None
        self.poller = None
        self.watcher = None
        self.term_signal = None
        self.exit_code = None

        # Zustand HOME
        self.stages = []
        self.stage_i = 0
        self.previous_release = None  # Ordner der Stufe previous (aus <data>/previous), sonst None
        self.fails = []               # Zeitpunkte (crash_clock) der Fehlstarts und Abstürze dieser Stufe
        self.crash_clock = crash_clock
        self.pending = None           # Uhrzeit eines Starts, der noch nicht bereit ist
        self.home_started = 0.0
        self.home_ready = False
        self.beat_sig = None
        self.beat_seen = 0.0
        self.exiting_seen = None      # wann <run>/exiting des laufenden Home zuerst gesehen wurde
        self.restart_at = None
        self.poweroff_at = None

        # Zustand STEAM
        self.steam_started = 0.0
        self.game_phase = None        # "appear" | "gone" | None
        self.gone_since = None
        self.next_game_scan = 0.0
        self.steam_uri = None
        self.steam_resends = []
        self.handover_env = {}
        self.shutdown_stage = 0       # 1 = höflich gebeten, 2 = SIGTERM, 3 = SIGKILL
        self.shutdown_at = 0.0
        self.steam_tree = []

        self.sink = None
        self.watch_errors = 0
        self.raise_at = e.get("COUCH_TEST_RAISE", "") if self.test else ""
        self.x_socket_dir = (e.get("COUCH_X_SOCKET_DIR") if self.test else "") or X_SOCKET_DIR

        # Einschaltknopf
        self.power_lone = None        # wann ein Druck ohne lebendes Home vermerkt wurde (E38)
        self.suspended = self.suspended_total()
        self.woke_at = None           # wann die Schleife ein Aufwachen bemerkt hat

    # -- Befehle, Protokoll
    def command(self, name, default):
        raw = (self.env.get(name) or "").strip() or default
        try:
            argv = shlex.split(raw)
        except ValueError:
            argv = shlex.split(default)
        return argv or shlex.split(default)

    def guard(self, argv):
        """Im Prüfmodus läuft nie das echte Steam, auch nicht aus Versehen (der Nutzer spielt womöglich nebenher)."""
        if self.test and os.path.basename(argv[0]) == "steam":
            self.note_write("(Prüfmodus, nicht gestartet) " + shlex.join(argv))
            return list(NOOP)
        try:    # eine Rückfallebene, die wieder couch-session startet, wäre eine Endlosschleife
            target = which(argv[0], self.env)
            if target and os.path.realpath(target) == os.path.join(SESSION_DIR, "couch-session"):
                self.log("Rückfall-Befehl zeigt auf couch-session, nehme die Vorgabe")
                return self.guard(shlex.split(DEFAULT_FALLBACK))
        except OSError:
            pass
        return argv

    def log(self, text):
        line = "%s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self.state, text)
        try:
            sys.stderr.write("couch-supervisor: [%s] %s\n" % (self.state, text))
            sys.stderr.flush()
        except (OSError, ValueError):
            pass
        try:
            try:
                if os.path.getsize(self.paths.log) > LOG_LIMIT:
                    os.replace(self.paths.log, self.paths.log + ".1")
            except OSError:
                pass
            with open(self.paths.log, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass                       # Zustandsordner voll oder gesperrt: das Journal hat die Zeile

    def note_write(self, text):
        try:
            with open(self.paths.writes, "a", encoding="utf-8") as f:
                f.write("%s supervisor: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass
        self.log("Prüfmodus, nur protokolliert: " + text)

    def run_write(self, argv, timeout=10.0):
        """Der EINE Weg für Befehle, die den Rechner verändern. Im Prüfmodus nur eine Zeile."""
        if self.test:
            self.note_write(shlex.join(argv))
            return True
        code, _out = self.run_cmd(argv, timeout)
        if code != 0:
            self.log("Befehl gescheitert (%s): %s" % (code, shlex.join(argv)))
        return code == 0

    # -- Kinder
    def spawn(self, argv, env, stdout=None):
        pid = os.fork()
        if pid == 0:
            try:
                restore_signals()
                if stdout is not None:
                    os.dup2(stdout, 1)
                os.execvpe(argv[0], argv, env)
            except BaseException as e:      # im Kind darf nichts in die Schleife der Mutter zurücklaufen
                try:
                    os.write(2, ("couch-supervisor: %s: %s\n" % (argv[0], e)).encode("utf-8", "replace"))
                except OSError:
                    pass
            os._exit(127)
        return pid

    def reap(self):
        """Sammelt alle beendeten Kinder ein. Als Subreaper gehören dazu auch adoptierte Waisen."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if pid in self.known:
                code = os.waitstatus_to_exitcode(status)
                self.exited[pid] = code if code >= 0 else 128 - code

    def nap(self, seconds):
        """Wartet höchstens so lange; ein Signal (SIGCHLD) weckt sofort."""
        try:
            ready, _, _ = select.select([self.wake_r], [], [], max(0.0, seconds))
            if ready:
                os.read(self.wake_r, 512)
        except (OSError, ValueError, TypeError):
            time.sleep(max(0.0, min(seconds, 0.05)))

    def wait_exit(self, pid, timeout):
        """Rückgabewert von pid oder None nach Ablauf. Immer begrenzt."""
        self.known.add(pid)
        end = time.monotonic() + timeout
        while True:
            self.reap()
            if pid in self.exited:
                self.known.discard(pid)
                return self.exited.pop(pid)
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.nap(min(left, 0.2))

    def run_cmd(self, argv, timeout=5.0, capture=False):
        """Kurzer Hilfsbefehl mit Zeitgrenze: (Rückgabewert oder None, Ausgabe)."""
        r = w = None
        if capture:
            r, w = os.pipe()
        try:
            pid = self.spawn(argv, self.env, stdout=w)
        finally:
            if w is not None:
                os.close(w)
        code = self.wait_exit(pid, timeout)
        if code is None:
            send_signal(pid, signal.SIGKILL)
            self.wait_exit(pid, 2.0)
        out = b""
        if r is not None:
            try:
                os.set_blocking(r, False)
                out = os.read(r, 65536)
            except OSError:
                pass
            os.close(r)
        return code, out.decode("utf-8", "replace")

    # -- Einrichtung
    def setup(self):
        try:    # Subreaper: verwaiste reaper eines abgestürzten Home landen bei uns
            ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        except (OSError, AttributeError):
            pass
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        signal.set_wakeup_fd(self.wake_w, warn_on_full_buffer=False)
        signal.signal(signal.SIGCHLD, lambda *_: None)      # nur damit die Schleife aufwacht
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.on_term)
        self.poller = select.poll()
        self.poller.register(self.wake_r, select.POLLIN)
        for folder in (self.paths.state, self.paths.run):
            try:
                os.makedirs(folder, mode=0o700, exist_ok=True)
            except OSError:
                pass
        # Das Home startet Spiele mit der Sitzungsumgebung; Steam bekommt sie von uns direkt.
        blob = b"".join(k + b"=" + v + b"\0" for k, v in SESSION_ENVB.items())
        if not write_atomic(self.paths.session_env, blob):
            self.log("session.env nicht schreibbar: " + self.paths.session_env)

    def on_term(self, signum, _frame):
        self.term_signal = signum

    def short_session_marker(self):
        """Gewolltes Sitzungsende (Marke vom Home): Der Kurzsitzungs-Zähler der Sitzung zählt es nicht."""
        if not os.path.lexists(self.paths.deliberate):
            return
        if self.steamos:
            # E104 d: Valves Zähler gehört Valve; nur die eigene Marke geht weg
            remove_file(self.paths.deliberate)
            self.log("letztes Sitzungsende war gewollt (SteamOS: kein Kurzsitzungs-Zähler wird angefasst)")
            return
        tracker = self.env.get("COUCH_SHORT_SESSION_TRACKER")
        if tracker:
            remove_file(tracker)
        elif self.test:
            self.note_write("rm " + DEFAULT_TRACKER)
        else:
            remove_file(DEFAULT_TRACKER)
        remove_file(self.paths.deliberate)
        self.log("letztes Sitzungsende war gewollt: Kurzsitzungs-Zähler gelöscht")

    def fuse_valid(self, name):
        """Sicherung gesetzt und für diese Fassung? Eine für eine andere Fassung fällt hier."""
        path = self.paths.fuse(name)
        if not os.path.lexists(path):
            return False
        owner = read_first_line(path)
        if owner and owner != self.id:
            self.log("Sicherung %s galt für %s, jetzt %s: gelöscht" % (name, owner, self.id))
            remove_file(path)
            return False
        return True                    # leer oder unlesbar zählt als gesetzt

    def set_fuse(self, name, why):
        text = "%s\n%s %s\n" % (self.id, time.strftime("%Y-%m-%d %H:%M:%S"), why)
        if not write_atomic(self.paths.fuse(name), text, 0o644):
            self.log("Sicherung %s nicht schreibbar (gilt nur für diese Sitzung)" % name)

    def build_stages(self):
        stages = []
        raw = (self.env.get("COUCH_HOME_CMD") or "").strip()
        home = os.path.join(RELEASE, "app", "couch-home")
        if raw:
            stages.append(("current", shlex.split(raw)))
        elif os.access(home, os.X_OK):
            stages.append(("current", [home]))
        else:
            self.log("kein ausführbares Home in dieser Fassung: " + home)
        raw = (self.env.get("COUCH_PREVIOUS_HOME_CMD") or "").strip()
        previous = os.path.realpath(os.path.join(self.paths.data, "previous"))
        home = os.path.join(previous, "app", "couch-home")
        self.previous_release = None
        if raw:
            stages.append(("previous", shlex.split(raw)))
        elif previous != RELEASE and os.access(home, os.X_OK):
            stages.append(("previous", [home]))
            self.previous_release = previous
        return stages

    def note_in_use(self):
        """<run>/releases-in-use: Aus diesen Fassungen kann die Sitzung ein Home starten, auch Stunden
        später nach einem Absturz. tools/install-release räumt sie deshalb nie ab (E99)."""
        folders = [RELEASE] + ([self.previous_release] if self.previous_release else [])
        if not write_atomic(self.paths.in_use, "".join(f + "\n" for f in folders), 0o644):
            self.log("releases-in-use nicht schreibbar: " + self.paths.in_use)

    def note_good(self):
        """<state>/last-good: Diese Fassung hat es bis zum Bereit-Signal geschafft (Stufe current).
        tools/install-release nimmt sie als previous, statt eine als kaputt bekannte (E99)."""
        text = "%s\n%s\n" % (self.id, RELEASE)
        try:
            with open(self.paths.last_good, encoding="utf-8") as f:
                if f.read() == text:
                    return
        except OSError:
            pass
        if not write_atomic(self.paths.last_good, text, 0o644):
            self.log("last-good nicht schreibbar: " + self.paths.last_good)

    def drop_good(self):
        """Diese Fassung reißt in Stufe current die Absturzgrenze: Sie gilt nicht mehr als bewährt.
        fuse-current allein reicht nicht, die nächste Fassung löscht es beim Start (fuse_valid);
        bliebe last-good stehen, machte install-release sie später wieder zu previous (E99)."""
        if read_first_line(self.paths.last_good) == self.id:
            remove_file(self.paths.last_good)
            self.log("last-good verworfen: Fassung %s reißt die Absturzgrenze" % self.id)

    # -- Zähler über Sitzungen (nur Kopie; maßgeblich ist der Speicher)
    def stage_name(self):
        return self.stages[self.stage_i][0] if self.stages else "-"

    def persist(self):
        self.tidy("Zähler sichern", self._persist)

    def _persist(self):
        boot, wall = self.crash_clock(), time.time()
        data = {"id": self.id, "stage": self.stage_name(), "pending": self.pending,
                "fails": [round(wall - (boot - t), 1) for t in self.fails]}
        write_atomic(self.paths.fails, json.dumps(data) + "\n")

    def load_fails(self):
        self.tidy("Zähler laden", self._load_fails)

    def _load_fails(self):
        try:
            with open(self.paths.fails, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("id") != self.id or data.get("stage") != self.stage_name():
            return
        stamps = data.get("fails") if isinstance(data.get("fails"), list) else []
        if data.get("pending"):        # letzte Sitzung endete, bevor das Home bereit war
            stamps = stamps + [data["pending"]]
        boot, wall = self.crash_clock(), time.time()
        for stamp in stamps:
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                age = max(0.0, wall - stamp)
                if age < self.crash_window:
                    self.fails.append(boot - age)
        if self.fails:
            self.log("%d Fehlstart(s) aus früheren Sitzungen übernommen" % len(self.fails))

    def tidy(self, what, fn, *args):
        """Beiwerk (Ton, Aufräumen, Zähler): Sein Fehler darf nie ändern, wohin die Sitzung geht."""
        try:
            return fn(*args)
        except Exception:
            self.log("Fehler bei %s (ohne Folgen für den Ablauf):\n%s" % (what, traceback.format_exc()))
            return None

    # -- Bluetooth
    def bt_restore(self, why):
        self.tidy("Bluetooth-Probezeit", self._bt_restore, why)

    def _bt_restore(self, why):
        """Liegt <state>/bt-off-trial, hat ein Home Bluetooth auf Probe ausgeschaltet und die
        Probezeit nie beendet (Absturz, Hänger, Ende mittendrin). Ohne Home kann sie niemand mehr
        bestätigen: Bluetooth wieder ein, sonst bleibt ein Bluetooth-Pad tot (auch für Steam und
        den Notgriff). Nur rufen, solange kein Home läuft."""
        line = read_first_line(self.paths.bt_trial)
        if line is None:
            return
        adapter = (line.split() or [""])[0]
        if BT_ADAPTER.match(adapter):
            self.log("Probezeit „Bluetooth aus“ nicht beendet (%s): Bluetooth wird wieder eingeschaltet (%s)"
                     % (why, adapter))
            argv = ["busctl", "--system", "set-property", "org.bluez", adapter, "org.bluez.Adapter1",
                    "Powered", "b", "true"]
            if not any(self.run_write(argv, timeout=5.0) for _ in range(2)):
                self.log("Bluetooth ließ sich nicht wieder einschalten; die Marke bleibt für den nächsten Versuch")
                return
        else:
            self.log("Marke der Bluetooth-Probezeit unbrauchbar, verworfen: %r" % line[:80])
        remove_file(self.paths.bt_trial)

    # -- Ton
    def sink_get(self):
        return self.tidy("Tonausgang lesen", self._sink_get)

    def _sink_get(self):
        code, out = self.run_cmd([self.pactl, "get-default-sink"], 3.0, capture=True)
        name = out.strip().splitlines()[0].strip() if code == 0 and out.strip() else ""
        return name or None

    def sink_restore(self):
        """Den Standard-Ausgang von vor der Sitzung zurücksetzen (Befund 14); nur wenn er sich geändert hat."""
        self.tidy("Tonausgang zurücksetzen", self._sink_restore)

    def _sink_restore(self):
        if not self.sink:
            # Beim Start war der Ausgang nicht lesbar: Was vorher galt, weiß nur der Merkzettel.
            # Er bleibt für die nächste Sitzung (E99).
            return
        now = self._sink_get()
        if not now:
            self.log("Tonausgang nicht lesbar: Merkzettel bleibt für die nächste Sitzung")
            return
        if now != self.sink:
            self.log("Tonausgang zurück auf " + self.sink)
            if not self.run_write([self.pactl, "set-default-sink", self.sink]):
                # Ausgang gerade nicht da (Focusrite aus): Ohne Zettel hielte die nächste Sitzung
                # den Fernseher für den Ausgang von vorher (E99)
                self.log("Tonausgang ließ sich nicht zurückstellen: Merkzettel bleibt für den nächsten Versuch")
                return
        # Zurückgestellt (oder nie umgestellt): Der Merkzettel des Home hat sich erledigt.
        remove_file(self.paths.sink_before)

    def sink_at_start(self):
        """Welcher Ausgang am Sitzungsende wieder gelten soll.

        Normal ist das der jetzige Standard. Endete die letzte Sitzung aber, ohne
        zurückzustellen (Ausschalten beendet uns hart), steht jetzt noch der Fernseher als
        Standard da, und der Ausgang vom Schreibtisch wäre vergessen. Dafür legt das Home beim
        Umstellen <state>/sink-before an: Zeile 1 = Ausgang vorher, Zeile 2 = worauf es
        umgestellt hat. Gilt Zeile 2 noch, wird Zeile 1 wiederhergestellt und gemerkt. Hat
        inzwischen jemand etwas anderes gewählt (am Desktop), gilt dessen Wahl."""
        self.sink = self.sink_get()
        self.tidy("Merkzettel der Tonausgabe", self._sink_from_note)

    def _sink_from_note(self):
        try:
            with open(self.paths.sink_before, encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for line in f.read().splitlines()]
        except OSError:
            return
        before = lines[0] if lines else ""
        during = lines[1] if len(lines) > 1 else ""
        ok = bool(before) and len(before) < 300 and not before.startswith("-") and before.isprintable()
        if ok and self.sink is None:
            # Ausgang nicht lesbar: Ob der Zettel überholt ist, lässt sich nicht sagen (E99)
            self.log("Tonausgang nicht lesbar: Merkzettel der Tonausgabe bleibt liegen")
            return
        if ok and during and self.sink == during and before != during:
            self.log("letzte Sitzung hat den Tonausgang nicht zurückgestellt: zurück auf %s (statt %s)"
                     % (before, during))
            self.sink = before          # am Sitzungsende gilt das als „vorher“, auch wenn es jetzt scheitert
            if not self.run_write([self.pactl, "set-default-sink", before]):
                self.log("Tonausgang ließ sich nicht zurückstellen: Merkzettel bleibt, das Sitzungsende versucht es wieder")
                return
        else:
            self.log("Merkzettel der Tonausgabe ist überholt (jetzt %s): verworfen" % self.sink)
        remove_file(self.paths.sink_before)

    # -- Home
    def home_env(self):
        env = {k: v for k, v in self.env.items() if k not in HOME_STRIP}
        env["GDK_BACKEND"] = "x11"
        env["COUCH_SUPERVISED"] = "1"
        env["COUCH_SESSION_ENV"] = self.paths.session_env
        return env

    def start_home(self):
        name, cmd = self.stages[self.stage_i]
        for leftover in ("ready", "beat", "handover.json", "exiting"):
            remove_file(os.path.join(self.paths.run, leftover))
        self.pending = round(time.time(), 1)     # pessimistisch: gilt als Fehlstart, bis "ready" da ist
        self.persist()
        self.state = self.HOME
        self.restart_at = None
        self.home_ready = False
        self.beat_sig = None
        self.exiting_seen = None
        self.home_started = time.monotonic()
        if self.steamos and not self.quick_noted:
            self.quick_noted = True
            self.tidy("Schnellstart-Sicherung", self.note_quick)
        self.home_pid = self.spawn(cmd, self.home_env())
        self.home_spawned = self.home_pid
        self.known.add(self.home_pid)
        self.log("Home gestartet (%s, PID %d): %s" % (name, self.home_pid, shlex.join(cmd)))

    def stop_home(self, why):
        """Beendet das Home (nur diese PID): höflich, nach der Frist hart. Seine reaper bleiben."""
        pid, self.home_pid = self.home_pid, None
        if not pid:
            return
        self.log("beende Home (PID %d): %s" % (pid, why))
        send_signal(pid, signal.SIGTERM)
        if self.wait_exit(pid, self.kill_grace) is None:
            send_signal(pid, signal.SIGKILL)
            if self.wait_exit(pid, 5.0) is None:
                self.log("Home (PID %d) reagiert auch auf SIGKILL nicht" % pid)

    def beat_signature(self):
        try:
            st = os.stat(os.path.join(self.paths.run, "beat"))
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def tick_home(self, now):
        if self.home_pid is None:
            if self.hold_until is not None:
                # Nur SteamOS: Der Dienst endet über SIGTERM (Desktop-Wechsel, Ende der Sitzung). Kommt
                # keins, geht es mit dem Home weiter statt mit einem dunklen Bildschirm.
                if now < self.hold_until:
                    return
                self.log("%s: kein SIGTERM gekommen, starte das Home wieder" % self.hold_why)
                self.hold_until = None
                self.restart_at = now
            if self.poweroff_at is not None:
                if now - self.poweroff_at < self.poweroff_wait:
                    return
                self.poweroff_at = None
                self.log("Rechner läuft noch: Ausschalten hat nicht gegriffen, starte das Home wieder")
                self.restart_at = now
            if self.restart_at is not None and now >= self.restart_at:
                self.start_home()
            return
        code = self.exited.pop(self.home_pid, None)
        if code is not None:
            self.known.discard(self.home_pid)
            self.home_pid = None
            if not self.home_ready and os.path.lexists(os.path.join(self.paths.run, "ready")):
                # Bereit-Signal und Ende fielen in denselben Takt (Übergabe gleich nach dem Start):
                # Das Home war bereit, das Ende ist kein Fehlstart.
                self.mark_ready(now)
            intended = self.read_exiting()
            if intended in (0, 10) and code != intended and self.home_ready:
                self.log("Home endete mit %d, hatte aber %d angekündigt: gilt als %d" % (code, intended, intended))
                code = intended
            self.home_exited(code)
            return
        if not self.home_ready:
            if os.path.lexists(os.path.join(self.paths.run, "ready")):
                self.mark_ready(now)
            elif now - self.home_started > self.ready_timeout:
                self.stop_home("kein Bereit-Signal nach %.0f s" % self.ready_timeout)
                self.fail("Fehlstart: nicht bereit geworden")
            return
        intended = self.read_exiting()
        if intended is not None:
            # Das Home endet gewollt; der Herzschlag zählt nicht mehr. Hängt das Ende (ein
            # Arbeits-Thread, das Sichern), nach COUCH_EXIT_WAIT hart beenden und den
            # angekündigten Code nehmen: eine Übergabe an Steam geht so nicht verloren.
            if self.exiting_seen is None:
                self.exiting_seen = now
                self.log("Home kündigt sein Ende an (Rückgabewert %d)" % intended)
            elif now - self.exiting_seen > self.exit_wait:
                self.stop_home("beendet sich nicht, %.0f s nach der Ankündigung (Rückgabewert %d)"
                               % (now - self.exiting_seen, intended))
                self.home_exited(intended)
            return
        # Herzschlag: Wir messen, wann sich die Datei zuletzt geändert hat, mit der eigenen Uhr.
        # So stört weder ein Sprung der Systemzeit noch das Format des Inhalts.
        sig = self.beat_signature()
        if sig != self.beat_sig:
            self.beat_sig, self.beat_seen = sig, now
        elif now - self.beat_seen > self.beat_max_age:
            self.stop_home("Herzschlag steht seit %.0f s" % (now - self.beat_seen))
            self.fail("Home hing (kein Herzschlag)")

    def mark_ready(self, now):
        self.home_ready = True
        self.beat_sig = self.beat_signature()
        self.beat_seen = now
        self.pending = None
        self.persist()
        self.log("Home ist bereit (nach %.1f s)" % (now - self.home_started))
        if self.stage_name() == "current":
            self.tidy("last-good", self.note_good)
        if self.steamos:
            self.tidy("Schnellstart-Sicherung", remove_file, self.paths.steamos_quick)

    def read_exiting(self):
        """Angekündigter Rückgabewert aus <run>/exiting, nur vom zuletzt gestarteten Home; sonst None."""
        line = read_first_line(self.paths.exiting)
        if not line:
            return None
        parts = line.split()
        try:
            code, pid = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return None
        return code if pid == self.home_spawned else None

    def home_alive(self, now):
        return bool(self.home_pid and self.home_ready and now - self.beat_seen <= self.beat_max_age
                    and self.exiting_seen is None)

    def display_gone(self):
        """Lauscht am X-Socket aus $DISPLAY niemand mehr? Dann ist Gamescope abgestürzt oder
        beendet. Nur bei eindeutiger Lage wahr; ohne lokalen Display-Namen (Käfig der Tests)
        ist nichts prüfbar."""
        path = x_socket_path(self.env.get("DISPLAY", ""), self.x_socket_dir)
        return bool(path) and x_socket_dead(path)

    def session_lost(self, why):
        """Gamescope bzw. sein X-Server ist weg. Das liegt nicht am Home: nicht zählen, keine
        Sicherung, nicht an Steam übergeben (auch das hätte keine Anzeige). Die Sitzung endet
        zügig mit Fehlercode; die Sitzungsverwaltung startet sie neu."""
        if self.steamos:
            self.log("keine Gamescope-Anzeige (%s; DISPLAY=%s): nicht gezählt, keine Sicherung"
                     % (why, self.env.get("DISPLAY", "") or "nicht gesetzt"))
        else:
            self.log("keine Gamescope-Anzeige (%s; DISPLAY=%s): Sitzung endet mit %d, "
                     "ohne Fehlstart und ohne Sicherung"
                     % (why, self.env.get("DISPLAY", "") or "nicht gesetzt", EXIT_NO_DISPLAY))
        # Auch was kurz davor gezählt wurde, war wahrscheinlich schon die Folge (das Home stirbt
        # mit seiner X-Verbindung, bevor der Socket verschwunden ist): nichts in die nächste
        # Sitzung mitnehmen.
        self.fails = []
        self.pending = None
        self.restart_at = self.poweroff_at = None
        self.persist()
        self.cleanup_reapers(grace=min(self.kill_grace, 3.0))
        self.sink_restore()
        if self.steamos:
            # E104 d: kein Ende (Valves Zähler); das Ende der Sitzung kommt als SIGTERM
            self.hold("keine Gamescope-Anzeige", self.lost_wait)
            return
        self.exit_code = EXIT_NO_DISPLAY

    def hold(self, why, seconds):
        """Nur SteamOS: kein Home, kein Steam; warten, bis systemd die Sitzung mit SIGTERM beendet."""
        self.log("%s: warte bis zu %g s auf das Ende der Sitzung (SIGTERM)" % (why, seconds))
        self.state = self.HOME
        self.hold_why = why
        self.hold_until = time.monotonic() + seconds
        self.restart_at = None

    def to_desktop(self):
        """Nur SteamOS: „Sitzung beenden“ heißt dort zum Desktop wechseln (ein beendeter Dienst käme über
        Upholds= sofort wieder). Danach endet die Sitzung über SIGTERM; scheitert der Wechsel, läuft
        Valves Launcher als Kind."""
        self.pending = None
        self.persist()
        self.cleanup_reapers()
        self.sink_restore()
        self.bt_restore("Wechsel zum Desktop")
        if self.run_write(["steamosctl", "switch-to-desktop-mode"], timeout=15.0):
            self.hold("Wechsel zum Desktop erbeten", self.desktop_wait)
        else:
            self.start_valve("Wechsel zum Desktop gescheitert")

    def home_exited(self, code):
        if code == EXIT_NO_DISPLAY:
            # Das Home hat selbst festgestellt, dass $DISPLAY kein Gamescope-Xwayland ist
            self.session_lost("Home endete mit %d" % code)
            return
        if not self.home_ready:
            # Vor dem ersten Bild kann niemand etwas gewählt haben: auch 0 und 10 sind dann Fehlstarts.
            self.fail("Fehlstart: beendet mit %d vor dem Bereit-Signal" % code)
        elif code == 0 and self.steamos:
            self.log("Home hat die Sitzung beendet: SteamOS, Wechsel zum Desktop")
            self.to_desktop()
        elif code == 0:
            self.log("Home hat die Sitzung beendet")
            self.finish(0)
        elif code == 10:
            uri, mode = self.read_handover()
            self.log("Übergabe an Steam (uri=%s, zurück=%s)" % (uri, mode))
            self.pending = None
            self.persist()
            self.to_steam(uri, mode)
        else:
            self.fail("Home abgestürzt (Rückgabewert %d)" % code)

    def fail(self, why):
        if self.display_gone():
            self.session_lost(why)
            return
        self.bt_restore(why)
        now, boot = time.monotonic(), self.crash_clock()
        self.fails = [t for t in self.fails if boot - t < self.crash_window] + [boot]
        self.pending = None
        self.persist()
        self.log("%s – %d von %d in %.0f s (%s)" % (why, len(self.fails), self.max_fails,
                                                   self.crash_window, self.stage_name()))
        if len(self.fails) >= self.max_fails:
            self.next_stage()
        else:
            self.restart_at = now + self.restart_delay

    def next_stage(self):
        name = self.stage_name()
        if name == "current":
            self.tidy("last-good", self.drop_good)
        if self.stage_i + 1 < len(self.stages):
            if name == "current":
                # merken, sonst wartet der Nutzer bei jedem Einschalten wieder drei Fehlstarts ab
                self.set_fuse("fuse-current", "Home dieser Fassung startet nicht, nehme previous")
            self.stage_i += 1
            self.fails = []
            self.pending = None
            self.persist()
            self.log("Stufe %s aufgegeben, weiter mit %s" % (name, self.stage_name()))
            self.restart_at = time.monotonic()
            return
        self.set_fuse("fuse-launcher", "Home startet nicht (%s), Sitzung läuft mit Steam" % name)
        self.cleanup_reapers()
        self.sink_restore()
        self.exec_fallback("alle Stufen aufgegeben, Sicherung fuse-launcher gesetzt")

    def read_handover(self):
        try:
            with open(os.path.join(self.paths.run, "handover.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None, "manual"
        if not isinstance(data, dict):
            return None, "manual"
        uri = data.get("uri")
        if not (isinstance(uri, str) and URI_OK.match(uri)):     # landet auf Steams Befehlszeile
            uri = None
        env = data.get("env")
        self.handover_env = {k: v for k, v in env.items()
                             if k in HANDOVER_ENV and isinstance(v, str) and HANDOVER_ENV_VALUE.match(v)} \
            if isinstance(env, dict) else {}
        mode = data.get("return")
        return uri, mode if (mode == "on-game-exit" and uri) else "manual"

    # -- eigene reaper (Spiele des Home)
    def own_reapers(self, table):
        me = os.getpid()
        return [pid for pid in descendants(table, me)
                if OWN_APPID_MIN <= (reaper_appid(pid, table[pid][1]) or 0) <= OWN_APPID_MAX]

    def cleanup_reapers(self, grace=None):
        self.tidy("reaper abräumen", self._cleanup_reapers, grace)

    def _cleanup_reapers(self, grace=None):
        """Vor jeder Übergabe an Steam und am Sitzungsende: Spiele des Home auftauen und beenden.

        Nur Prozesse aus dem Baum eigener reaper (Nachkommen von uns, App-ID 0x7F1…), nur über
        PIDs. Erst SIGCONT (ein angehaltenes Spiel hört sonst kein SIGTERM), dann höflich, nach
        der Frist SIGKILL an alle Nachkommen.
        """
        grace = self.kill_grace if grace is None else grace
        table = proc_table()
        reapers = self.own_reapers(table)
        if not reapers:
            return
        self.log("räume %d eigene reaper ab: %s" % (len(reapers), reapers))
        marks = []
        for reaper in reapers:
            tree = descendants(table, reaper)
            for pid in tree + [reaper]:
                send_signal(pid, signal.SIGCONT)
            send_signal(reaper, signal.SIGTERM)          # unser reaper reicht es an alle Nachkommen weiter
            for pid in tree:
                if table[pid][0] == reaper:
                    send_signal(pid, signal.SIGTERM)     # das Hauptkind zur Sicherheit auch direkt
            marks.append((reaper, table[reaper][2]))
        end = time.monotonic() + grace
        while time.monotonic() < end:
            self.reap()
            if not any(same_process(pid, start) for pid, start in marks):
                return
            self.nap(0.1)
        table = proc_table()
        for reaper, start in marks:
            if reaper in table and table[reaper][2] == start:
                self.log("reaper %d reagiert nicht, SIGKILL an seinen Baum" % reaper)
                for pid in descendants(table, reaper) + [reaper]:
                    send_signal(pid, signal.SIGKILL)
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            self.reap()
            if not any(same_process(pid, start) for pid, start in marks):
                return
            self.nap(0.1)

    # -- Steam
    def steam_env(self):
        env = dict(self.env)
        shims = os.path.join(SESSION_DIR, "shims")
        if os.path.isdir(shims):       # "Zum Desktop wechseln" in Steam führt so zurück ins Home
            env["PATH"] = shims + os.pathsep + env.get("PATH", os.defpath)
        return env

    def sentinel(self, var):
        """Pfad der Sentinel-Datei aus der Sitzungsumgebung; im Prüfmodus nur innerhalb des Käfigs."""
        path = self.env.get(var)
        if not path:
            return None
        root = self.env.get("COUCH_TEST_ROOT")
        if self.test and not (root and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)):
            return None
        return path

    def clear_sentinels(self):
        for var in ("STEAMOS_STEAM_REBOOT_SENTINEL", "STEAMOS_STEAM_SHUTDOWN_SENTINEL"):
            path = self.sentinel(var)
            if path and remove_file(path):
                self.log("alte Sentinel-Datei entfernt: " + path)

    def to_steam(self, uri, mode):
        """Home ist schon beendet: aufräumen, Ton zurück, Steam als Kind starten."""
        self.cleanup_reapers()
        self.sink_restore()
        self.bt_restore("Übergabe an Steam")
        self.state = self.STEAM
        self.tidy("alte Sentinel-Dateien", self.clear_sentinels)
        base = self.guard(self.steam_cmd)          # ohne SteamOS dasselbe wie fallback_cmd
        # Immer Big Picture (E53, 2026-10-18): Der unsichtbare Steam-Client (-silent) ließ das Bild
        # schwarz (Steam setzt dann GAMESCOPECTRL_BASELAYER_APPID nicht, und während eines Steam-
        # Updates sah man gar nichts). Big Picture verschluckt die Adresse beim Kaltstart aber
        # manchmal; dann schickt tick_steam sie dem laufenden Steam noch einmal (resend_uri).
        argv = base + ([uri] if uri else [])
        env = self.steam_env()
        if uri and mode == "on-game-exit":
            env.update(getattr(self, "handover_env", {}))
        now = time.monotonic()
        self.steam_started = now
        self.steam_uri = uri if (uri and mode == "on-game-exit" and base != list(NOOP)) else None
        self.steam_resends = list(self.steam_resend_after) if self.steam_uri else []
        self.game_phase = "appear" if mode == "on-game-exit" else None
        self.gone_since = None
        self.next_game_scan = now + self.steam_scan
        self.shutdown_stage = 0
        self.steam_pid = self.spawn(argv, env)
        self.known.add(self.steam_pid)
        self.log("Steam gestartet (PID %d): %s" % (self.steam_pid, shlex.join(argv)))

    def steam_reapers(self):
        table = proc_table()
        return [pid for pid in descendants(table, self.steam_pid)
                if reaper_appid(pid, table[pid][1]) is not None]

    def request_steam_shutdown(self, why):
        if self.shutdown_stage or not self.steam_pid:
            return
        argv = self.guard(self.shutdown_cmd)
        self.log("bitte Steam zu enden (%s): %s" % (why, shlex.join(argv)))
        self.spawn(argv, self.steam_env())      # Rückgabewert egal, reap() sammelt ihn ein
        self.shutdown_stage, self.shutdown_at = 1, time.monotonic()

    def start_valve(self, why):
        """Nur SteamOS: Valves steam-launcher als Kind statt Ende oder exec (E104 d). Der Supervisor bleibt
        Hauptprozess des Dienstes und beendet Steam bei SIGTERM selbst in Ruhe. Mit der unveränderten
        Sitzungsumgebung (ohne Shims): „Zum Desktop wechseln“ in Steam geht dann den Weg von Valve."""
        self.log("SteamOS: Valves Launcher als Kind (%s)" % why)
        self.bt_restore("Rückfall auf Valves Launcher")
        if self.watcher is None:
            self.watcher = Watcher(self)
        self.state = self.STEAM
        self.valve = True
        self.valve_exec_at = None
        self.hold_until = None
        self.restart_at = self.poweroff_at = None
        argv = self.guard(self.fallback_cmd)
        now = time.monotonic()
        self.steam_started = now
        self.steam_uri = None
        self.steam_resends = []
        self.game_phase = None
        self.gone_since = None
        self.shutdown_stage = 0
        self.steam_pid = self.spawn(argv, dict(self.env))
        self.known.add(self.steam_pid)
        self.log("Valves Launcher gestartet (PID %d): %s" % (self.steam_pid, shlex.join(argv)))

    def tick_steam(self, now):
        if self.steam_pid is None and self.valve:
            # Nur SteamOS: Valves Launcher endete von selbst. Kam binnen der Frist kein SIGTERM (Wechsel zum
            # Desktop), wird der Supervisor zu ihm: ab dann ist es die Sitzung von Valve.
            if self.valve_exec_at is not None and now >= self.valve_exec_at:
                self.valve_exec_at = None
                self.exec_valve_now("Valves Launcher endete von selbst")
            return
        code = self.exited.pop(self.steam_pid, None)
        if code is not None:
            self.known.discard(self.steam_pid)
            self.steam_pid = None
            self.steam_exited(code)
            return
        if self.shutdown_stage == 1 and now - self.shutdown_at > self.steam_shutdown_wait:
            # Steam hört nicht auf die Bitte: seinen Baum (unser Kind und dessen Nachkommen) beenden
            table = proc_table()
            self.steam_tree = [(pid, table[pid][2]) for pid in [self.steam_pid] + descendants(table, self.steam_pid)
                               if pid in table]
            self.log("Steam endet nicht, SIGTERM an %d Prozesse" % len(self.steam_tree))
            for pid, _start in self.steam_tree:
                send_signal(pid, signal.SIGTERM)
            self.shutdown_stage, self.shutdown_at = 2, now
        if self.shutdown_stage or self.game_phase is None or now < self.next_game_scan:
            return
        self.next_game_scan = now + self.steam_scan
        running = self.steam_reapers()
        if self.game_phase == "appear":
            if running:
                self.game_phase = "gone"
                self.log("Steam-Spiel läuft (reaper %s)" % running)
            elif self.steam_resends and now - self.steam_started >= self.steam_resends[0]:
                # Big Picture hat die Adresse beim Kaltstart verschluckt: dem laufenden Steam noch
                # einmal schicken (ein zweiter steam-Aufruf reicht sie über seine IPC weiter)
                self.steam_resends.pop(0)
                argv = [self.guard(self.steam_cmd)[0], self.steam_uri]
                self.log("noch kein Spiel nach %.0f s: Adresse erneut an Steam: %s"
                         % (now - self.steam_started, shlex.join(argv)))
                self.spawn(argv, self.steam_env())
            elif now - self.steam_started > self.steam_game_wait:
                self.game_phase = None
                self.log("kein Spiel binnen %.0f s gestartet: Steam bleibt offen" % self.steam_game_wait)
        elif running:
            self.gone_since = None
        elif self.gone_since is None:
            self.gone_since = now      # kurz warten: Einrichtungsschritte laufen in eigenen reapern
        elif now - self.gone_since >= self.steam_game_gone:
            self.game_phase = None
            self.request_steam_shutdown("Spiel beendet")

    def tick_steam_kill(self, now):
        """Letzte Stufe für ein hängendes Steam; läuft auch weiter, wenn das Startskript schon weg ist."""
        if self.shutdown_stage == 2 and now - self.shutdown_at > self.kill_grace:
            left = [pid for pid, start in self.steam_tree if same_process(pid, start)]
            if left:
                self.log("SIGKILL an %d übrige Steam-Prozesse" % len(left))
                for pid in left:
                    send_signal(pid, signal.SIGKILL)
            self.shutdown_stage, self.steam_tree = 3, []

    def steam_exited(self, code):
        self.log("Steam beendet (Rückgabewert %d)" % code)
        if self.shutdown_stage < 2:
            self.shutdown_stage = 0
        if self.valve:
            # Nur SteamOS: kurz auf ein SIGTERM warten (Valves „Zum Desktop wechseln“ beendet erst Steam,
            # dann die Sitzung), sonst würde Steam mitten im Wechsel gleich wieder gestartet
            self.valve_exec_at = time.monotonic() + self.valve_restart_wait
            return
        for var, method in (("STEAMOS_STEAM_REBOOT_SENTINEL", "Reboot"),
                            ("STEAMOS_STEAM_SHUTDOWN_SENTINEL", "PowerOff")):
            path = self.sentinel(var)
            if path and os.path.lexists(path):
                # Steam wollte neu starten bzw. ausschalten. Die Datei bleibt liegen: Die Sitzung
                # wertet sie nach unserem Ende selbst noch einmal aus (zweites Netz).
                self.log("Sentinel %s liegt vor: %s über logind" % (path, method))
                self.sink_restore()
                self.tidy("logind", self.logind, method)
                if self.steamos:
                    self.hold("Steam wollte %s" % ("neu starten" if method == "Reboot" else "ausschalten"),
                              self.poweroff_wait)
                    return
                self.exit_code = 0
                return
        if os.path.lexists(self.paths.disabled):
            if self.steamos:
                self.sink_restore()
                self.start_valve("Notschalter liegt vor")
                return
            self.log("Notschalter liegt vor: Sitzung endet mit Steam")
            self.sink_restore()
            self.exit_code = code
            return
        self.state = self.HOME
        self.restart_at = time.monotonic()

    # -- Notgriff, Einschaltknopf, Ausschalten
    def on_chord(self, source):
        if self.valve:
            # Nur SteamOS im Rückfall: Das Home kommt in dieser Sitzung nicht wieder (Notschalter, Sicherung)
            self.log("Notgriff (%s): Rückfall auf Valves Launcher läuft, nichts zu tun" % source)
            return
        if self.state == self.HOME:
            self.log("Notgriff (%s): Home beenden, Steam starten" % source)
            self.stop_home("Notgriff")
            self.pending = None
            self.persist()
            self.restart_at = self.poweroff_at = None
            self.to_steam(None, "manual")
        elif self.state == self.STEAM:
            self.log("Notgriff (%s): Steam beenden, danach Home" % source)
            self.game_phase = None
            self.request_steam_shutdown("Notgriff")

    def send_control(self, line):
        """Eine Zeile an die Prüfsteuerung des Home; nie blockierend."""
        dirfd = None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        try:
            address = os.path.join(self.paths.run, "control.sock")
            if len(os.fsencode(address)) > 100:      # sun_path fasst 107 Byte: über den Ordner gehen
                dirfd = os.open(self.paths.run, os.O_PATH | os.O_DIRECTORY)
                address = "/proc/self/fd/%d/control.sock" % dirfd
            sock.settimeout(0.5)
            sock.connect(address)
            sock.sendall((line + "\n").encode("ascii"))
            return True
        except OSError:
            return False
        finally:
            sock.close()
            if dirfd is not None:
                os.close(dirfd)

    @staticmethod
    def suspended_total():
        """Sekunden, die der Rechner seit dem Hochfahren im Ruhemodus war (BOOTTIME zählt sie
        mit, MONOTONIC nicht). Wächst der Wert, ist der Rechner gerade aufgewacht."""
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
        except (AttributeError, OSError):
            return 0.0

    def note_wake(self, now, total=None):
        total = self.suspended_total() if total is None else total
        if total - self.suspended > 1.0:
            self.woke_at = now
            self.power_lone = None
            self.log("aufgewacht (Ruhemodus %.0f s)" % (total - self.suspended))
        self.suspended = total

    def on_power(self, now=None):
        """E38 (Fassung vom 2026-10-14): Lebt das Home, geht jeder Druck an das Home; dort fragt
        der Kern vor dem Ausschalten nach (zweiter Druck = Rückfrage, dritter = bestätigt). Der
        Supervisor schaltet dann nie selbst aus. Ohne lebendes Home kann niemand fragen: Erst ein
        zweiter Druck binnen COUCH_POWER_CONFIRM schaltet aus, ein einzelner nie."""
        now = time.monotonic() if now is None else now
        if self.state != self.HOME:
            return                     # unter Steam arbeitet steamos-powerbuttond
        if self.woke_at is not None and now - self.woke_at < self.power_wake_guard:
            # Der Druck, der den Rechner geweckt hat, kommt hier noch an: Er darf weder das
            # Menü öffnen noch als zweiter Druck gelten.
            self.log("Einschaltknopf gleich nach dem Aufwachen: übergangen")
            return
        if self.home_alive(now):
            if self.send_control("power-key"):
                self.power_lone = None          # ein Druck ans Home ist kein erster „ohne Home“
                self.log("Einschaltknopf an das Home gereicht (das Home fragt vor dem Ausschalten)")
                return
            self.log("Einschaltknopf: control.sock antwortet nicht")
        if self.power_lone is not None and now - self.power_lone <= self.power_confirm:
            # Zweiter Druck ohne lebendes Home: der Weg ohne Pad und ohne Bild. Sonst bliebe nur
            # der lange Druck = hartes Aus.
            self.power_lone = None
            self.power_off("Einschaltknopf zweimal gedrückt, kein lebendes Home")
            return
        self.power_lone = now
        self.log("Einschaltknopf ohne lebendes Home: nur vermerkt, noch ein Druck binnen %.0f s schaltet aus"
                 % self.power_confirm)

    def logind(self, method):
        return self.run_write(["busctl", "--system", "call", "org.freedesktop.login1",
                               "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                               method, "b", "false"])     # false = nie nach einem Kennwort fragen

    def power_off(self, why):
        self.log("fahre herunter: " + why)
        self.stop_home("Ausschalten")
        self.pending = None
        self.persist()
        self.cleanup_reapers()
        self.sink_restore()
        self.tidy("logind", self.logind, "PowerOff")
        # Greift das Ausschalten nicht, startet das Home nach der Wartezeit wieder.
        self.poweroff_at = time.monotonic()
        self.restart_at = None

    # -- Ende
    def finish(self, code):
        self.pending = None
        self.persist()
        self.cleanup_reapers()
        self.sink_restore()
        self.bt_restore("Sitzungsende")
        self.exit_code = code

    def end_by_signal(self):
        self.log("Signal %d: Sitzung wird beendet" % self.term_signal)
        self.stop_home("Signal")
        if self.steam_pid:
            self.request_steam_shutdown("Signal")
            self.wait_exit(self.steam_pid, self.kill_grace)
        self.finish(0)

    def exec_fallback(self, why):
        """Ersetzt den Supervisor durch den Rückfall-Befehl, mit der unveränderten Sitzungsumgebung.
        Auf SteamOS kehrt es zurück: Dort läuft Valves Launcher als Kind (E104 d)."""
        if self.steamos:
            self.start_valve(why)
            return
        self.log("übergebe die Sitzung an: %s (%s)" % (shlex.join(self.fallback_cmd), why))
        self.bt_restore("Übergabe an den Rückfall")
        if self.watcher:
            self.watcher.close_all()
        exec_fallback(self.guard(self.fallback_cmd), self.test)

    def exec_valve_now(self, why):
        """Nur SteamOS: Der Supervisor wird zu Valves Launcher (exec), etwa nachdem dieser als Kind von
        selbst endete. Ein schnelles Ende danach ist Valves Sache (dessen Zähler, dessen Reparatur)."""
        self.log("übergebe die Sitzung an Valves Launcher: %s (%s)" % (shlex.join(self.fallback_cmd), why))
        self.sink_restore()
        if self.watcher:
            self.watcher.close_all()
        exec_fallback(self.guard(self.fallback_cmd), self.test)

    def quick_start_guard(self):
        """Nur SteamOS: Liegt in <state>/steamos-quick ein Start von vor höchstens quick_window Sekunden, dessen
        Home nie bereit wurde, ist dies der zweite Schnellstart: Sicherung setzen (True). Valves Zähler packt
        Steam erst beim dritten Kurzlauf neu aus; unsere Sicherung greift davor."""
        now = time.time()
        try:
            with open(self.paths.steamos_quick, encoding="utf-8", errors="replace") as f:
                stamps = [float(x) for x in f.read().split()[:20]]
        except (OSError, ValueError):
            stamps = []
        recent = [t for t in stamps if 0.0 <= now - t < self.quick_window]
        if not recent:
            return False
        self.log("zweiter Start ohne Bereit-Signal binnen %.0f s (letzter vor %.0f s)"
                 % (self.quick_window, now - max(recent)))
        self.set_fuse("fuse-launcher", "SteamOS: zweiter Schnellstart ohne Bereit-Signal")
        remove_file(self.paths.steamos_quick)
        return True

    def note_quick(self):
        if not write_atomic(self.paths.steamos_quick, "%.1f\n" % time.time(), 0o644):
            self.log("steamos-quick nicht schreibbar: " + self.paths.steamos_quick)

    # -- Schleife
    def test_raise(self, where):
        if self.raise_at == where:
            self.raise_at = ""
            raise RuntimeError("Test: absichtlicher Fehler (%s)" % where)

    def watch(self, events, now):
        """Wächter bedienen. Sein Fehler kostet nur den Wächter: Ein gesundes Home läuft weiter,
        und ein hängendes fängt weiterhin der Herzschlag ab."""
        try:
            for fd, flags in events:
                if not self.watcher.on_fd(fd, flags, now):
                    try:
                        self.poller.unregister(fd)
                    except (KeyError, OSError, ValueError):
                        pass
            if self.raise_at == "watcher":
                self.raise_at = ""
                raise RuntimeError("Test: absichtlicher Fehler (watcher)")
            return self.watcher.tick(now)
        except Exception:
            self.watch_errors += 1
            self.log("Fehler im Eingabe-Wächter (%d):\n%s" % (self.watch_errors, traceback.format_exc()))
            old, self.watcher = self.watcher, Watcher(self)
            for dev in list(old.devices.values()):
                try:
                    old.drop(dev, "Fehler im Wächter")
                except Exception:
                    pass
            if self.watch_errors >= 5:
                self.log("Eingabe-Wächter abgeschaltet: Notgriff und Einschaltknopf sind ohne Wirkung")
                self.watcher.next_scan = float("inf")
            else:
                self.watcher.next_scan = now + self.rescan * 5      # etwas später neu versuchen
            return []

    def loop_once(self):
        events = []
        for fd, flags in self.poller.poll(int(self.tick * 1000)):
            if fd == self.wake_r:
                try:
                    os.read(self.wake_r, 512)
                except OSError:
                    pass
            else:
                events.append((fd, flags))
        self.reap()
        if self.term_signal is not None:
            self.end_by_signal()
            return
        now = time.monotonic()
        self.note_wake(now)
        for action, source in self.watch(events, now):
            if action == "chord":
                self.on_chord(source)
            else:
                self.on_power(now)
            if self.exit_code is not None:
                return
        self.tick_steam_kill(now)
        if self.state == self.HOME:
            self.tick_home(now)
            if self.home_ready and self.home_pid:
                self.test_raise("home")
        elif self.state == self.STEAM:
            self.tick_steam(now)
            if self.steam_pid and now - self.steam_started > 0.3:
                self.test_raise("steam")
        self.test_raise("loop")

    def run(self):
        self.setup()
        self.log("Start: Fassung %s, PID %d%s" % (self.id, os.getpid(), ", Prüfmodus" if self.test else ""))
        self.test_raise("start")
        self.tidy("Kurzsitzungs-Zähler", self.short_session_marker)
        self.bt_restore("Start der Sitzung")
        self.begin()
        while self.exit_code is None:
            self.loop_once()
        self.log("Ende mit Rückgabewert %d" % self.exit_code)
        return self.exit_code

    def begin(self):
        """Erster Schritt der Sitzung. Jedes exec_fallback hier kehrt nur auf SteamOS zurück (Valves
        Launcher läuft dann als Kind), sonst nie."""
        if os.path.lexists(self.paths.disabled):
            return self.exec_fallback("Notschalter " + self.paths.disabled)
        if self.fuse_valid("fuse-launcher"):
            return self.exec_fallback("Sicherung fuse-launcher gesetzt")
        if self.steamos and self.tidy("Schnellstart-Sicherung", self.quick_start_guard):
            return self.exec_fallback("Schnellstart-Sicherung gesetzt")
        self.stages = self.build_stages()
        if not self.stages:
            return self.exec_fallback("kein startbares Home")
        self.tidy("releases-in-use", self.note_in_use)
        if len(self.stages) > 1 and self.fuse_valid("fuse-current"):
            self.stage_i = 1
            self.log("Sicherung fuse-current gesetzt: beginne mit previous")
        self.sink_at_start()
        self.watcher = Watcher(self)
        self.load_fails()
        self.state = self.HOME
        if len(self.fails) >= self.max_fails:
            self.next_stage()
        elif os.path.lexists(self.paths.start_steam):
            # Weiche Wahl aus den Einstellungen (E26): mit Steam beginnen, aber wach bleiben.
            # Notgriff und Steams Ende führen ins Home.
            self.log("Wahl start-steam liegt vor: Sitzung beginnt mit Steam Big Picture")
            self.to_steam(None, "manual")
        else:
            self.start_home()
        return None


# ----------------------------------------------------------------------------- letzte Stufe
def restore_signals():
    """Python ignoriert SIGPIPE und SIGXFSZ; Ignoriertes überlebt exec. Kinder sollen den Normalfall sehen."""
    for name in ("SIGPIPE", "SIGXFSZ"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (OSError, ValueError):
                pass


def exec_fallback(argv, test=False):
    """execvp des Rückfall-Befehls mit der Sitzungsumgebung. Kehrt nie zurück."""
    try:
        signal.set_wakeup_fd(-1)
    except (OSError, ValueError):
        pass
    try:    # Steam soll nicht als Subreaper fremder Waisen starten
        ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
    restore_signals()
    tries = [argv]
    if not test:                       # Vorgabe und fester Pfad als weitere Versuche; im Prüfmodus nie
        default = shlex.split(DEFAULT_FALLBACK)
        tries += [default, ["/usr/bin/steam"] + default[1:]]
    for cmd in tries:
        try:
            sys.stderr.flush()
            os.execvpe(cmd[0], cmd, SESSION_ENV)
        except OSError as e:
            try:
                sys.stderr.write("couch-supervisor: %s nicht ausführbar: %s\n" % (cmd[0], e))
            except (OSError, ValueError):
                pass
    # Nichts ausführbar: schnell enden. Dann greift das Netz der Sitzung selbst
    # (Kurzsitzungs-Zähler -> short_session_recover), statt eines dunklen Bildschirms.
    os._exit(1)


def panic(sup, info):
    """Unerwarteter Fehler im Supervisor: Home beenden, dann der Rückfall-Befehl. Kehrt nie zurück."""
    text = ""
    try:
        text = "".join(traceback.format_exception(*info))
        sup.state = "FEHLER"
        sup.log("unerwarteter Fehler, gehe auf die Rückfallebene:\n" + text)
    except BaseException:
        try:                           # ohne Supervisor-Objekt wenigstens ins Journal
            sys.stderr.write("couch-supervisor: unerwarteter Fehler vor dem Start:\n" + text)
            sys.stderr.flush()
        except BaseException:
            pass
    home, steam = getattr(sup, "home_pid", None), getattr(sup, "steam_pid", None)
    try:
        if home:
            send_signal(home, signal.SIGTERM)
            end = time.monotonic() + 3.0
            while time.monotonic() < end:
                try:
                    if os.waitpid(home, os.WNOHANG)[0] == home:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            else:
                send_signal(home, signal.SIGKILL)
    except BaseException:
        pass
    try:
        if getattr(sup, "wake_r", None) is not None:
            sup.cleanup_reapers(grace=3.0)
    except BaseException:
        pass
    if SESSION_ENV.get("COUCH_PLATFORM") == "steamos":
        steamos_last_resort(sup, steam)        # kehrt nie zurück
    if steam:
        # Steam läuft schon als unser Kind. Ein zweiter Aufruf würde sich nur an das laufende
        # hängen und sofort enden (= Sitzungsende). Also nur noch warten, bis es selbst endet.
        code = getattr(sup, "exited", {}).get(steam)
        while code is None:
            try:
                _pid, status = os.waitpid(steam, 0)
                code = os.waitstatus_to_exitcode(status)
                code = code if code >= 0 else 128 - code
            except InterruptedError:
                continue
            except ChildProcessError:
                code = 0                   # schon eingesammelt
            except BaseException:
                code = 1                   # nie in einer Schleife hängen bleiben
        os._exit(code & 0xFF)
    try:
        argv = sup.guard(sup.fallback_cmd)
        test = sup.test
    except BaseException:
        test = bool(SESSION_ENV.get("COUCH_TEST_ROOT")) or SESSION_ENV.get("COUCH_TEST") == "1"
        argv = list(NOOP) if test else shlex.split(DEFAULT_FALLBACK)
    exec_fallback(argv, test)


def steamos_last_resort(sup, steam=None):
    """SteamOS (E104 d) nach einem unerwarteten Fehler: Der Supervisor bleibt Hauptprozess des Dienstes,
    mit Valves Launcher als Kind (oder dem schon laufenden Steam), und endet nur auf SIGTERM. Dann bittet er
    Steam zu enden, wie Valves ExecStop es täte. Endet das Kind von selbst, wird er zu Valves Launcher
    (exec). Nur Standardbibliothek und eigene Signale, keine Schleife des Supervisors. Kehrt nie zurück."""
    try:
        test = bool(sup.test)
        valve = sup.guard(sup.fallback_cmd)
        shutdown = sup.guard(sup.shutdown_cmd)
        grace = sup.kill_grace
        restart_wait = sup.valve_restart_wait
    except BaseException:
        test = bool(SESSION_ENV.get("COUCH_TEST_ROOT")) or SESSION_ENV.get("COUCH_TEST") == "1"
        valve = list(NOOP) if test else shlex.split(SESSION_ENV.get("COUCH_FALLBACK_CMD") or DEFAULT_FALLBACK)
        shutdown = list(NOOP) if test else shlex.split(DEFAULT_SHUTDOWN)
        grace, restart_wait = 8.0, 5.0
    term = []
    try:
        signal.set_wakeup_fd(-1)
    except (OSError, ValueError):
        pass
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(signum, lambda s, _f: term.append(s))
        except (OSError, ValueError):
            pass

    def launch(argv):
        pid = os.fork()
        if pid == 0:
            try:
                restore_signals()
                os.execvpe(argv[0], argv, SESSION_ENV)
            except BaseException:
                pass
            os._exit(127)
        return pid

    def gone(pid, wait):
        end = time.monotonic() + wait
        while True:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return True
            except ChildProcessError:
                return True
            except OSError:
                pass
            if time.monotonic() >= end or (term and wait == float("inf")):
                return False
            time.sleep(0.2)

    child = steam
    if not child:
        try:
            child = launch(valve)
        except OSError:
            child = None
    while child and not term:
        if gone(child, float("inf")):
            child = None
    if term:
        # Ende der Sitzung: Steam höflich bitten, dann sein Baum per SIGTERM, zuletzt SIGKILL (nur eigene Kinder)
        if child:
            try:
                helper = launch(shutdown)
                gone(helper, 5.0)
            except OSError:
                pass
            if not gone(child, grace):
                for pid in descendants(proc_table(), child) + [child]:
                    send_signal(pid, signal.SIGTERM)
                if not gone(child, 3.0):
                    send_signal(child, signal.SIGKILL)
                    gone(child, 2.0)
        os._exit(0)
    # Kind endete von selbst: kurz auf ein SIGTERM warten (Wechsel zum Desktop), dann Valves Sitzung
    end = time.monotonic() + restart_wait
    while not term and time.monotonic() < end:
        time.sleep(0.1)
    if term:
        os._exit(0)
    exec_fallback(valve, test)


# ----------------------------------------------------------------------------- Befehlszeile
def status(sup):
    p = sup.paths
    sup.log = lambda _text: None       # nur anzeigen, nichts ins Protokoll schreiben
    print("Fassung:      %s  (%s)" % (sup.id, RELEASE))
    stages = sup.build_stages()
    print("Stufen:       %s -> Steam" % (" -> ".join("%s [%s]" % (n, shlex.join(c)) for n, c in stages) or "(kein Home)"))
    print("Rückfall:     %s" % shlex.join(sup.fallback_cmd))
    if sup.steamos:
        quick = "liegt vor" if os.path.lexists(p.steamos_quick) else "aus"
        print("Plattform:    SteamOS (endet nur auf SIGTERM; Schnellstart-Sicherung %s)" % quick)
    print("Notschalter:  %s  (%s)" % ("GESETZT" if os.path.lexists(p.disabled) else "aus", p.disabled))
    print("Start:        %s  (%s)" % ("mit Steam Big Picture, danach Home" if os.path.lexists(p.start_steam)
                                      else "mit dem Home", p.start_steam))
    for name in ("fuse-launcher", "fuse-current"):
        path = p.fuse(name)
        if os.path.lexists(path):
            owner = read_first_line(path)
            valid = "gilt" if (not owner or owner == sup.id) else "veraltet (für %s), fällt beim nächsten Start" % owner
            print("Sicherung:    %s GESETZT, %s" % (name, valid))
        else:
            print("Sicherung:    %s aus" % name)
    try:
        with open(p.fails, encoding="utf-8") as f:
            print("Fehlstarts:   %s" % f.read().strip())
    except OSError:
        print("Fehlstarts:   keine")
    print("Protokoll:    %s" % p.log)
    # Was der Wächter lesen würde (nur aus der Geräteliste gelesen, nichts geöffnet)
    if sup.test:
        print("Wächter:      Prüfmodus, liest nur COUCH_WATCH_DEVICES=%s" % sup.env.get("COUCH_WATCH_DEVICES", ""))
    else:
        try:
            with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as f:
                found = parse_input_devices(f.read())
        except OSError:
            found = []
        for node, kind, name in found:
            readable = "lesbar" if os.access("/dev/input/" + node, os.R_OK) else "NICHT lesbar"
            print("Wächter:      %s %s (%s, %s)" % ("Notgriff an" if kind == "pad" else "Einschaltknopf", node, name, readable))
        if not any(kind == "pad" for _node, kind, _name in found):
            print("Wächter:      kein Pad mit PS-, Create- und Options-Taste verbunden (Notgriff erst nach dem Verbinden)")
    return 0


def reset(sup):
    p = sup.paths
    names = []
    try:
        names = [n for n in os.listdir(p.state) if n.startswith("fuse-") or n == "failed-starts"]
    except OSError:
        pass
    for name in sorted(names):
        if remove_file(os.path.join(p.state, name)):
            print("gelöscht: " + os.path.join(p.state, name))
    print("Sicherungen zurückgesetzt")
    return 0


def main(argv):
    arg = argv[1] if len(argv) > 1 else ""
    if arg == "--selftest":
        ctypes.CDLL(None).prctl
        select.poll()
        print("ok")
        return 0
    if SESSION_ENV.get("COUCH_TEST") == "1" and not SESSION_ENV.get("COUCH_TEST_ROOT"):
        # Prüfmodus ohne Prüfordner: Alle Orte zeigten auf die echten Ordner des Nutzers (Sicherungen,
        # Zähler, Laufzeitdateien). Dann gar nichts tun, auch nicht --reset.
        sys.stderr.write("couch-supervisor: COUCH_TEST=1 ohne COUCH_TEST_ROOT. Der Prüfmodus braucht "
                         "seinen Ordner; nichts getan.\n")
        return EXIT_NOT_IN_SESSION
    if arg in ("--status", "--reset"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
        return status(Supervisor()) if arg == "--status" else reset(Supervisor())
    if arg:
        sys.stderr.write(__doc__)
        return 2
    if not SESSION_ENV.get("GAMESCOPE_WAYLAND_DISPLAY") and SESSION_ENV.get("COUCH_PLATFORM") == "steamos":
        # E104 d: Aus Valves Dienst gestartet, aber ohne Gamescope-Umgebung. Ein Ende mit 2 zählte Valves
        # Kurzsitzungs-Zähler; dann lieber gleich Valves Launcher.
        sys.stderr.write("couch-supervisor: SteamOS ohne GAMESCOPE_WAYLAND_DISPLAY: übergebe an Valves Launcher\n")
        test = bool(SESSION_ENV.get("COUCH_TEST_ROOT")) or SESSION_ENV.get("COUCH_TEST") == "1"
        try:
            argv = Supervisor().guard(shlex.split(SESSION_ENV.get("COUCH_FALLBACK_CMD") or DEFAULT_FALLBACK))
        except Exception:
            argv = list(NOOP) if test else shlex.split(DEFAULT_FALLBACK)
        exec_fallback(argv, test)
    if not SESSION_ENV.get("GAMESCOPE_WAYLAND_DISPLAY"):
        # Von Hand in einem Terminal gestartet: Ohne Gamescope endete das Home dreimal, die
        # Sicherung fiele, und zuletzt startete Steam Big Picture auf dem Desktop.
        sys.stderr.write("couch-supervisor: Das ist keine Game-Mode-Sitzung (GAMESCOPE_WAYLAND_DISPLAY "
                         "fehlt). Nichts gestartet.\nZustand ansehen: couch-session --status\n")
        return EXIT_NOT_IN_SESSION
    sup = None
    try:
        sup = Supervisor()
        return sup.run()
    except SystemExit:
        raise
    except BaseException:
        panic(sup, sys.exc_info())
        return 1                       # unerreichbar: panic kehrt nicht zurück


if __name__ == "__main__":
    code = main(sys.argv)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    os._exit(code)                     # ohne Aufräumarbeiten des Interpreters, die noch hängen könnten
