"""Protokoll des Home. app/couch-home leitet stdout und stderr nach <state>/home.log um;
hier wird nur formatiert und im Lauf rotiert (GTK, WebKit und die Seite schreiben in
denselben Strom, deshalb kein eigener Dateihandler)."""
from __future__ import annotations

import logging
import os
import re
import sys
import threading

MAX_BYTES = 2 * 1024 * 1024
KEEP = 3
APPEND_LIMIT = 5 * 1024 * 1024      # Protokolle unter <state>/apps, an die angehängt wird (capped)
_lock = threading.Lock()
_count = 0


def rotate_files(path: str, keep: int = KEEP) -> None:
    """home.log -> home.log.1 -> … ; die älteste Fassung fällt weg."""
    for i in range(keep, 0, -1):
        src = path if i == 1 else "%s.%d" % (path, i - 1)
        try:
            os.replace(src, "%s.%d" % (path, i))
        except OSError:
            pass


def capped(path: str, limit: int = APPEND_LIMIT) -> str:
    """Pfad eines Protokolls, das zum Anhängen ("a") geöffnet wird: Ist es größer als limit, wird
    es zu <pfad>.1 (die ältere Fassung fällt weg), und der Schreiber beginnt neu. Sonst wuchsen
    legendary-, Download-Steam- und Launcher-Protokolle mit jedem Lauf ohne Grenze (E99).

    Nur dort rufen, wo zum Anhängen geöffnet wird, nie zum Lesen: Sonst wanderte das Protokoll
    eines laufenden Schreibers weg (Fehlerende, „MELDUNG:“-Zeile)."""
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass                            # fehlt oder nicht umbenennbar: dann eben ohne Grenze
    return path


def _maybe_rotate() -> None:
    """Wird die Datei im Lauf zu groß, eine neue anfangen und fd 1/2 darauf umbiegen.
    Kindprozesse mit dem alten Deskriptor schreiben in die abgelegte Datei weiter."""
    path = os.environ.get("COUCH_LOG_FILE")
    if not path:
        return
    try:
        st = os.fstat(2)
        if st.st_size < MAX_BYTES or os.stat(path).st_ino != st.st_ino:
            return
        rotate_files(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass


class _Handler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        global _count
        super().emit(record)
        with _lock:
            _count += 1
            if _count % 200 == 0:
                _maybe_rotate()


def setup(level: str | None = None) -> None:
    root = logging.getLogger()
    if any(isinstance(h, _Handler) for h in root.handlers):
        return
    handler = _Handler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    name = (level or os.environ.get("COUCH_LOG_LEVEL") or "INFO").upper()
    root.setLevel(getattr(logging, name, logging.INFO))


def get(name: str) -> logging.Logger:
    return logging.getLogger("couch." + name)


# Zeilen ohne Aussage für das Fehlerfenster: umu listet mit UMU_LOG=1 jede Umgebungsvariable auf (bei
# exportierten Shell-Funktionen samt Rumpf über viele Zeilen); die Ursache stand dann nicht mehr da
_ENV_DUMP = re.compile(r"^\[umu\.__main__:\d+\] DEBUG: [A-Za-z_][A-Za-z0-9_%]*=")


def _readable(text: str) -> list:
    out, in_func = [], False
    for line in text.splitlines():
        if _ENV_DUMP.match(line):
            in_func = "BASH_FUNC_" in line.split("=", 1)[0] and not line.rstrip().endswith("}")
            continue
        if in_func:                             # Rumpf einer Shell-Funktion bis zur schließenden Klammer
            if line.strip() == "}":
                in_func = False
            continue
        out.append(line)
    return out


def tail(path: str, lines: int = 20, max_bytes: int = 16384) -> str:
    """Die letzten Zeilen einer Protokolldatei (für Fehlermeldungen in der Oberfläche), ohne umus
    Auflistung der Umgebung."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return "\n".join(_readable(text)[-lines:])
