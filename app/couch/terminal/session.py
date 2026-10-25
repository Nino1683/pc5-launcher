"""Was im Terminal läuft (rein Python, ohne GTK): Befehle der Startauswahl, Umgebung, Pseudo-Terminal.

Die Startauswahl (term.js) schickt eine Kennung aus ACTIONS; argv_for() macht daraus den Befehl.
Claude Code läuft im Home-Ordner wie im Desktop-Terminal, damit „Weitermachen“ (--continue) die
Gespräche von dort findet.
"""
from __future__ import annotations

import errno
import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import termios

# Kennung -> (Programm, Schalter). "shell" ist die Befehlszeile des Benutzers.
ACTIONS = {
    "continue": ("claude", ["--continue"]),
    "resume": ("claude", ["--resume"]),
    "new": ("claude", []),
    "update": ("claude", ["update"]),
    "shell": ("bash", ["-l"]),
}

# Setzt nur die Konsole bzw. dieses Fenster; das Programm im Terminal soll sie nicht erben
DROP_ENV = ("GDK_BACKEND", "GDK_SCALE", "WEBKIT_FORCE_VBLANK_TIMER", "DISABLE_GAMESCOPE_WSI", "PYTHONDONTWRITEBYTECODE",
            "COUCH_PYTHON", "COUCH_LANG")
DROP_PREFIX = ("COUCH_TERM_",)
# Merkmale einer laufenden Claude-Code-Sitzung: Geerbt (Konsole aus einem Claude-Terminal neu
# gestartet) hielte sich das neue Claude für eine Unter-Sitzung und speicherte kein Gespräch
CLAUDE_MARKERS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT", "CLAUDE_CODE_CHILD_SESSION",
                  "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT",
                  "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_SSE_PORT")
MIN_COLS, MIN_ROWS = 20, 5


def search_path(env: dict, home: str) -> str:
    """PATH mit ~/.local/bin (dort installiert sich Claude Code), ohne Dopplungen."""
    parts = [p for p in env.get("PATH", "/usr/local/bin:/usr/bin:/bin").split(":") if p]
    local = os.path.join(home, ".local", "bin")
    if local not in parts:
        parts.insert(0, local)
    return ":".join(parts)


def child_env(env: dict, home: str, app_dir: str = "") -> dict:
    """Umgebung für das Programm im Terminal: wie die der App, ohne die Schalter für GTK/WebKit
    und ohne den eigenen Ordner im PYTHONPATH (den setzt erst bin/couch-terminal)."""
    out = {k: v for k, v in env.items()
           if k not in DROP_ENV and k not in CLAUDE_MARKERS and not k.startswith(DROP_PREFIX)}
    if app_dir and out.get("PYTHONPATH"):
        rest = [p for p in out["PYTHONPATH"].split(":") if p and os.path.realpath(p) != os.path.realpath(app_dir)]
        if rest:
            out["PYTHONPATH"] = ":".join(rest)
        else:
            del out["PYTHONPATH"]
    out["TERM"] = "xterm-256color"
    out["COLORTERM"] = "truecolor"
    out["HOME"] = home
    out["PATH"] = search_path(env, home)
    return out


def argv_for(action: str, env: dict) -> list[str] | None:
    """Befehl zu einer Kennung; None, wenn es die Kennung oder das Programm nicht gibt."""
    if action not in ACTIONS:
        return None
    program, args = ACTIONS[action]
    exe = shutil.which(program, path=env.get("PATH"))
    return [exe, *args] if exe else None


def clamp_size(cols, rows) -> tuple[int, int]:
    try:
        cols, rows = int(cols), int(rows)
    except (TypeError, ValueError):
        cols, rows = 80, 24
    return max(MIN_COLS, min(cols, 1000)), max(MIN_ROWS, min(rows, 500))


class Pty:
    """Ein Programm an einem Pseudo-Terminal. Lesen und Schreiben ohne Blockieren; das Fenster
    beobachtet fd (GLib) und den Prozess (pid)."""

    def __init__(self, argv: list[str], cwd: str, env: dict, cols: int, rows: int):
        master, slave = pty.openpty()
        try:
            self._set_size(slave, cols, rows)
            # setsid -c: eigene Sitzung mit dem Terminal als steuerndem Terminal (Strg+C, Größe,
            # Auflegen). Ohne preexec_fn, das in einem Programm mit Threads (GTK) nicht sicher ist
            self.proc = subprocess.Popen(["setsid", "-c", *argv], stdin=slave, stdout=slave, stderr=slave,
                                         cwd=cwd, env=env, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.fd = master
        self.pid = self.proc.pid
        self._pending = b""

    @staticmethod
    def _set_size(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        if self.fd >= 0:
            self._set_size(self.fd, cols, rows)        # der Kern schickt SIGWINCH an die Vordergrund-Gruppe

    def read(self, limit: int = 65536) -> bytes | None:
        """Was das Programm geschrieben hat; b"" = gerade nichts, None = Ende (Programm weg)."""
        try:
            return os.read(self.fd, limit) or None
        except BlockingIOError:
            return b""
        except OSError as e:
            if e.errno == errno.EIO:                    # Linux: alle Schreiber zu
                return None
            raise

    def write(self, data: bytes) -> bool:
        """Eingabe ans Programm. False, wenn noch etwas wartet (später flush())."""
        self._pending += data
        return self.flush()

    def flush(self) -> bool:
        while self._pending and self.fd >= 0:
            try:
                n = os.write(self.fd, self._pending)
            except BlockingIOError:
                return False
            except OSError:
                self._pending = b""                     # Programm weg: Eingabe verfällt
                return True
            self._pending = self._pending[n:]
        return True

    def signal(self, sig: int = signal.SIGHUP) -> None:
        """An die ganze Sitzung (das Programm und alles, was es gestartet hat)."""
        try:
            os.killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
