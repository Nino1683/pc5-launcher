"""Entpacker: Archiv in einen frischen Zwischenordner, dann per rename an den Zielort.

Aus docs/research/prototypes/apps/archive_extract.py. Ablauf von run():

  1. Plan neu erstellen (probe); dem Plan aus der Oberfläche wird nicht geglaubt.
  2. Zwischenordner <Spiele-Ordner>/.couch-staging-<zufall>/ anlegen (mkdir scheitert, wenn es ihn
     gibt), Markerdatei mit dem Zufallswert hineinlegen und sperren (flock = "Auftrag läuft").
  3. 7z bzw. unrar in bubblewrap: alles nur lesbar, schreibbar allein <zwischenordner>/inhalt,
     kein Netz. Währenddessen alle 3 s statvfs: unter der Reserve wird abgebrochen (Befund 21).
  4. Symlinks entfernen, die hinauszeigen; einen einzelnen Oberordner auflösen; Markerdatei
     .couch-install.json in den Spielordner schreiben; rename an den Zielort (ersetzt nie etwas).
  5. Zwischenordner über trash.remove_staging() entfernen. Scheitert etwas, ebenfalls.

Es wird nie in einen bestehenden Ordner entpackt. Das Archiv wird nur gelesen und bleibt liegen.
Geschrieben wird ausschließlich unterhalb des Spiele-Ordners.
E114: Enthält das Archiv ein Installationsprogramm (Inno Setup, NSIS, MSI), bleibt der Zwischenordner
mit den entpackten Setup-Dateien stehen, und statt des Inhalts kommt ein leerer Spielordner mit Marker
an den Zielort (make_game_folder). Den stillen Lauf dort hinein macht der JobManager; danach entfernt
er den Zwischenordner über trash.remove_staging(), wie bei jedem anderen Auftrag.
Einzige Ausnahme (E100): unpack_into() entpackt das Archiv eines Stores (itch.io) in dessen eigenen,
frisch angelegten und markierten Spielordner, der außer dem Marker nur den Download-Ordner enthält;
mit denselben Schutzregeln (Käfig, Zwischenordner im Spielordner, Verweise, rename ohne Ersetzen).

Als Programm (so startet es der JobManager, eigene Prozessgruppe):
    python3 -I -B extract.py < auftrag.json      Fortschritt als JSON-Zeilen auf stdout
Der Auftrag kommt über stdin, damit das Kennwort weder auf einer Befehlszeile dieses Programms noch
auf der Platte steht. SIGTERM bricht ab und räumt auf. Verschwindet der Elternprozess, ebenfalls.

run() und main() blockieren, bis der Auftrag fertig ist.
"""
from __future__ import annotations

if __name__ == "__main__" and not __package__:
    # Als Skript gestartet (python3 -I kennt den Paketpfad nicht): Paketwurzel eintragen und als Modul laden
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.realpath(__file__)))))
    from couch.installer.extract import main as _main
    _sys.exit(_main())

import fcntl
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Callable

from .. import jsontext
from . import human_size, test_mode_violation, thread_limit
from . import probe, trash

PAYLOAD_DIR = "inhalt"          # Unterordner des Zwischenordners, in den entpackt wird
WATCH_INTERVAL = 3.0            # Sekunden zwischen zwei Blicken auf den freien Platz
STOP_GRACE = 5.0                # so lange darf der Entpacker nach SIGTERM brauchen, dann SIGKILL
START_FILE_SCAN_LIMIT = 50
WORKER_NICE = 10                # E30: Priorität des Arbeiters und damit von 7z/unrar

MSG_CANCELLED = "Die Installation wurde abgebrochen."


class _Abort(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


# ---------------------------------------------------------------- Zwischenordner

def make_staging(dest_root: str) -> tuple:
    """Legt <dest_root>/.couch-staging-<zufall>/ samt Marker und Unterordner an.

    Rückgabe: (zwischenordner, inhalt, zufallswert, sperre). Die Sperre (flock auf der Markerdatei)
    hält der Aufrufer, solange er arbeitet; staging_in_use() erkennt daran einen laufenden Auftrag."""
    token = secrets.token_hex(16)
    staging = os.path.join(dest_root, trash.STAGING_PREFIX + token)
    os.mkdir(staging, 0o700)                 # scheitert, falls es den Namen schon gibt
    lock_fd = os.open(os.path.join(staging, trash.STAGING_MARKER),
                      os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    os.write(lock_fd, token.encode("ascii"))
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    payload = os.path.join(staging, PAYLOAD_DIR)
    os.mkdir(payload, 0o755)
    return staging, payload, token, lock_fd


def staging_in_use(staging: str) -> bool:
    """Arbeitet gerade ein Entpacker in diesem Zwischenordner? (Er hält eine Sperre auf dem Marker.)"""
    try:
        fd = os.open(os.path.join(staging, trash.STAGING_MARKER), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False                         # Sperre bekommen: niemand arbeitet (close gibt sie wieder frei)
    except OSError:
        return True
    finally:
        os.close(fd)


def make_game_folder(final_dir: str, fields: dict) -> dict:
    """Leerer Spielordner mit unserem Marker (E114: Ziel eines Installationsprogramms). mkdir ersetzt nie
    etwas (FileExistsError). Scheitert der Marker, verschwindet der eben angelegte, leere Ordner wieder."""
    os.mkdir(final_dir, 0o755)
    try:
        return write_marker(final_dir, fields)
    except BaseException:
        try:
            os.rmdir(final_dir)              # nur der eigene, eben angelegte und noch leere Ordner
        except OSError:
            pass
        raise


def write_marker(folder: str, fields: dict) -> dict:
    """Schreibt .couch-install.json in einen frisch entpackten Spielordner (noch im Zwischenordner)
    oder in einen frisch angelegten Spielordner des Stores (GOG/<Titel>, E99).

    Ergänzt st_dev und die Kennung des Dateisystems. Eine gleichnamige Datei aus dem Archiv wird
    ersetzt: Dem Marker eines Archivs wird nie geglaubt."""
    dir_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(dir_fd)
        marker = dict(fields, st_dev=st.st_dev, fsid=os.fstatvfs(dir_fd).f_fsid)
        tmp = "%s.%s.tmp" % (trash.MARKER_NAME, secrets.token_hex(4))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as fh:
            fh.write(jsontext.dumps(marker, indent=1).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, trash.MARKER_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return marker
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------- Entpacken

def extractor_argv(fmt: str, first: str, payload: str, password: str | None, tools: dict | None) -> list:
    """Befehl des Entpackers im Käfig. Enthält gegebenenfalls das Kennwort: nie protokollieren."""
    if fmt == "rar":
        # -ol-: Symlinks aus dem Archiv nicht anlegen; -o+: überschreiben (Ordner ist frisch); -p-: nie nachfragen
        argv = probe.tool_argv(tools, "unrar") + ["x", "-cfg-", "-y", "-o+", "-ol-", "-p" + (password or "-"),
                                                  "--", first, payload + "/"]
    else:
        # -bsp1: Fortschritt auf stdout, -bso0: sonst nichts auf stdout, -bse2: Fehler auf stderr
        # -snld: relative Symlinks mit ".." anlegen, solange sie im Entpackten bleiben (bei Linux-Spielen üblich).
        #        Was hinausführt, lässt 7z weg; zusätzlich schützen der Käfig und remove_escaping_links().
        # -mmt:  höchstens MAX_THREADS Threads (E30); ohne den Schalter nimmt 7z alle Kerne.
        argv = probe.tool_argv(tools, "7z") + ["x", "-bsp1", "-bso0", "-bse2", "-y", "-snld", "-sccUTF-8",
                                               "-mmt=%d" % thread_limit(), "-o" + payload,
                                               "-p" + (password or probe.DUMMY_PW), "--", first]
    return probe.sandbox_argv(argv, read_only=[os.path.dirname(first)], writable=payload, tools=tools)


class ProgressParser:
    """Liest den Fortschritt aus der Ausgabe von 7z (-bsp1) bzw. unrar.

    Beide trennen ihre Meldungen mit Rückschritt-Zeichen statt Zeilenumbrüchen. Schnelle Archive
    liefern nur ein, zwei Werte; niemand darf erwarten, dass jede Prozentzahl vorkommt."""

    def __init__(self, fmt: str, payload: str):
        self.rar = fmt == "rar"
        self.prefix = payload.rstrip("/") + "/"
        self.buf = b""
        self.percent = 0
        self.file: str | None = None

    def feed(self, chunk: bytes) -> bool:
        """Verarbeitet ein Stück Ausgabe. True, wenn sich Prozent oder Datei geändert haben."""
        self.buf += chunk
        pieces = re.split(rb"[\x08\r\n]+", self.buf)
        self.buf = pieces.pop()[-8192:]        # unvollständiges Stück für die nächste Runde
        changed = False
        for piece in pieces:
            text = piece.decode("utf-8", "replace").strip()
            if not text:
                continue
            if self.rar:
                m = re.match(r"^(?:Extracting|Creating)\s+(.*?)(?:\s+OK)?$", text)
                if m and not text.startswith("Extracting from "):
                    name = m.group(1).strip()
                    self.file = name[len(self.prefix):] if name.startswith(self.prefix) else name
                    changed = True
                    continue
            m = re.match(r"^(\d{1,3})%(?:\s+\d+)?(?:\s+\S\s+(.*))?$", text)
            if m:
                percent = min(100, int(m.group(1)))
                if m.group(2):
                    self.file = m.group(2).strip()
                changed = changed or percent != self.percent or bool(m.group(2))
                self.percent = percent
        return changed


class _ErrorLines:
    """Sieht jede Fehlerzeile des Entpackers (nicht nur das Ende) und merkt sich, ob eine davon
    etwas anderes meldet als einen weggelassenen gefährlichen Symlink."""

    IGNORED_LINK = b"ERROR: Dangerous link path was ignored"

    def __init__(self):
        self.buf = b""
        self.links = 0
        self.other = 0

    def feed(self, chunk: bytes) -> None:
        *lines, self.buf = (self.buf + chunk).split(b"\n")
        self.buf = self.buf[-8192:]
        for line in lines:
            self._count(line)

    def _count(self, line: bytes) -> None:
        if not line.strip():
            return
        if line.startswith(self.IGNORED_LINK):
            self.links += 1
        else:
            self.other += 1

    def only_ignored_links(self) -> bool:
        self._count(self.buf)
        self.buf = b""
        return self.links > 0 and self.other == 0


def _signal_extractor(proc: subprocess.Popen, own_group: bool, sig: int) -> None:
    """Signal nur an den selbst gestarteten Entpacker. Nie über Namen, nie an fremde Gruppen."""
    if proc.poll() is not None:
        return                               # schon beendet und eingesammelt: die PID gehört uns nicht mehr
    try:
        if own_group:
            os.killpg(proc.pid, sig)         # eigene Sitzung des Entpackers: Gruppe = seine PID
        else:
            proc.send_signal(sig)            # teilt die Gruppe mit uns: nur das Kind selbst (bwrap reißt den Rest mit)
    except ProcessLookupError:
        pass


def _extract(argv: list, parser: ProgressParser, emit: Callable[[dict], None], *, free: Callable[[], int],
             reserve: int, watch_interval: float, cancel: threading.Event, own_group: bool, warnings_ok: bool,
             parent_alive: Callable[[], bool] | None, clock: Callable[[], float]) -> tuple:
    """Lässt den Entpacker laufen und wacht über Platz, Abbruch und Elternprozess.

    Rückgabe: (zustand, exit-code, stderr-ende) mit zustand in "ok" | "cancelled" | "no-space" |
    "disk-gone" | "orphaned" | "failed" | "no-tool".
    warnings_ok (nur 7z): Rückgabe 1 zählt als gelungen. Außerdem meldet 7z weggelassene gefährliche
    Symlinks als Fehler (Rückgabe 2); sind das die einzigen Fehlerzeilen, gilt der Lauf als gelungen,
    die Verweise fehlen dann einfach."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=own_group, env=probe.TOOL_ENV, cwd="/")
    except OSError as exc:
        return "no-tool", -1, str(exc.strerror or exc)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    open_streams = 2
    errors = b""
    error_lines = _ErrorLines()
    reason = None
    kill_at = None
    next_watch = clock() + watch_interval
    last_emit = 0.0
    try:
        while open_streams:
            for key, _events in selector.select(0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    open_streams -= 1
                elif key.data == "err":
                    errors = (errors + chunk)[-8192:]
                    error_lines.feed(chunk)
                elif parser.feed(chunk) and reason is None and clock() - last_emit >= 0.2:
                    last_emit = clock()
                    emit({"event": "progress", "percent": min(parser.percent, 99), "file": parser.file})
            now = clock()
            if reason is None:
                if cancel.is_set():
                    reason = "cancelled"
                elif now >= next_watch:
                    next_watch = now + watch_interval
                    try:
                        low = free() < reserve
                    except OSError:
                        reason = "disk-gone"      # Platte abgezogen: statvfs scheitert
                    else:
                        if low:
                            reason = "no-space"
                        elif parent_alive is not None and not parent_alive():
                            reason = "orphaned"
                if reason is not None:
                    _signal_extractor(proc, own_group, signal.SIGTERM)
                    kill_at = now + STOP_GRACE
            elif kill_at is not None and now >= kill_at:
                _signal_extractor(proc, own_group, signal.SIGKILL)
                kill_at = None
        code = proc.wait()
    finally:
        selector.close()
        if proc.poll() is None:              # nur bei einer Ausnahme oben: nichts zurücklassen
            _signal_extractor(proc, own_group, signal.SIGKILL)
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    tail = errors.decode("utf-8", "replace").strip()
    if reason is None and code < 0:
        time.sleep(0.05)                     # Signal an die ganze Gruppe: unserem eigenen Behandler kurz Zeit geben
        if cancel.is_set():
            reason = "cancelled"
    if reason is not None:
        return reason, code, tail
    if code == 0 or (code == 1 and warnings_ok and "bwrap:" not in tail):
        return "ok", code, tail              # 7z: 1 = Warnung (z. B. Daten hinter dem Archivende)
    if code == 2 and warnings_ok and error_lines.only_ignored_links():
        return "ok", code, tail              # 7z: nur weggelassene Symlinks
    return "failed", code, tail


def _failure(fmt: str, code: int, tail: str) -> tuple:
    """Fehler des Entpackers -> (Kürzel, deutscher Satz)."""
    low = tail.lower()
    if "bwrap:" in low:
        return "no-sandbox", "Der geschützte Entpacker (bubblewrap) konnte nicht starten."
    if "wrong password" in low or "incorrect password" in low or (fmt == "rar" and code == 11):
        return "password-wrong", "Das Kennwort stimmt nicht."
    if "no space left" in low or "errno=28" in low:
        return "no-space", "Der Speicherplatz ist beim Entpacken ausgegangen."
    if "cannot find volume" in low or "unexpected end" in low or "missing volume" in low or (fmt == "rar" and code == 6):
        return "incomplete", "Es fehlen Teile des Archivs oder es ist unvollständig."
    if "crc failed" in low or "data error" in low or "checksum error" in low or (fmt == "rar" and code == 3):
        return "damaged", "Das Archiv ist beschädigt."
    return "extract-failed", "Das Archiv konnte nicht entpackt werden."


# ---------------------------------------------------------------- Nacharbeit

def _is_real_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_real_file(path: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def verify_start_file(install_dir: str, rel: str) -> str | None:
    """Prüft eine Startdatei: relativer Pfad ohne "..", gewöhnliche Datei innerhalb des Spielordners.

    Rückgabe: der bereinigte relative Pfad oder None. Nur lesend."""
    if not isinstance(rel, str) or not rel or "\0" in rel or rel.startswith("/"):
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    clean = "/".join(parts)
    root = os.path.realpath(install_dir)
    full = os.path.join(root, clean)
    if not _is_real_file(full) or not os.path.realpath(full).startswith(root + os.sep):
        return None
    return clean


def scan_start_files(install_dir: str, platform: str, limit: int = START_FILE_SCAN_LIMIT) -> list:
    """Notliste möglicher Startdateien, wenn keiner der bewerteten Kandidaten auf der Platte liegt.

    Nur lesend, höchstens vier Ebenen tief. Rückgabe: [{"path", "score": 0, "why": []}]."""
    root = os.path.realpath(install_dir)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= 4:
            dirnames[:] = []
        for name in sorted(filenames, key=str.casefold):
            low = name.lower()
            wanted = low.endswith(".exe") if platform == "windows" else \
                low.endswith((".sh", ".x86_64", ".x64", ".x86", ".appimage"))
            if wanted and _is_real_file(os.path.join(dirpath, name)):
                found.append({"path": os.path.relpath(os.path.join(dirpath, name), root), "score": 0, "why": []})
                if len(found) >= limit:
                    return found
    return found


def _verified_candidates(plan: dict, top: str | None, source: str) -> list:
    """Kandidaten aus der Auflistung gegen den entpackten Ordner prüfen (Pfad im fertigen Spielordner)."""
    verified = []
    for cand in plan.get("candidates", []):
        archive_path = cand.get("archive_path") or ""
        if top is not None:
            if not archive_path.startswith(top + "/"):
                continue
            rel = archive_path[len(top) + 1:]
        else:
            rel = archive_path
        clean = verify_start_file(source, rel)
        if clean:
            verified.append({"path": clean, "score": cand.get("score", 0), "why": cand.get("why", [])})
    return verified or scan_start_files(source, plan.get("platform") or "windows")


def _move(source: str, final: str) -> None:
    """Der eine Schritt an den Zielort: rename im selben Dateisystem, ersetzt nie etwas."""
    try:
        trash.rename_noreplace(source, final)
    except FileExistsError:
        raise _Abort("target-exists", "Am Zielort gibt es inzwischen schon „%s“." % os.path.basename(final))
    except OSError as exc:
        raise _Abort("move-failed", "Das Spiel konnte nicht an seinen Platz verschoben werden.", str(exc.strerror or exc))
    try:                                     # Verzeichniseintrag auf die Platte bringen
        fd = os.open(os.path.dirname(final), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def marker_fields(spec: dict, plan: dict, real_root: str) -> dict:
    """Inhalt der Markerdatei eines Spielordners, den der Archiv-Installer anlegt."""
    return {"version": 1, "token": spec["token"], "entry_id": spec.get("entry_id"),
            "title": spec.get("title") or plan["title"], "archive": os.path.basename(plan["archive"]),
            "games_root": real_root, "folder": plan["target"]["name"], "created": time.time(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "couch_version": str(spec.get("version") or "")}


def _place_setup(plan: dict, spec: dict, payload: str, real_root: str, may_continue: Callable[[], bool]) -> dict:
    """E114: Das Archiv enthält ein Installationsprogramm. Es bleibt im Zwischenordner (dort läuft es,
    der JobManager entfernt ihn danach); an den Zielort kommt ein leerer Spielordner mit Marker.
    Wirft _Abort, dann liegt am Zielort nichts."""
    from . import setupexe
    rel = str(plan.get("exe") or "")
    if plan.get("layout") == "single-folder" and plan.get("top_folder"):
        rel = plan["top_folder"] + "/" + rel
    clean = verify_start_file(payload, rel)
    if clean is None:
        raise _Abort("bad-content", "Das Installationsprogramm fehlt im entpackten Archiv.")
    installer = os.path.join(os.path.realpath(payload), clean)
    kind = setupexe.installer_type(installer)
    if not kind:
        # erst hier sichtbar (die Auflistung nennt nur Namen); der Zwischenordner geht wie bei jedem Fehler weg
        raise _Abort("unknown-installer", setupexe.MSG_UNKNOWN)
    if not may_continue():
        raise _Abort("cancelled", MSG_CANCELLED)
    final = plan["target"]["dir"]
    try:
        make_game_folder(final, marker_fields(spec, plan, real_root))
    except FileExistsError:
        raise _Abort("target-exists", "Am Zielort gibt es inzwischen schon „%s“." % os.path.basename(final))
    except OSError as exc:
        raise _Abort("move-failed", "Der Spielordner konnte nicht angelegt werden.", str(exc.strerror or exc))
    return {"path": final, "installer": installer, "installer_type": kind, "keep_staging": True,
            "candidates": [], "needs_choice": False}


def _install(plan: dict, spec: dict, payload: str, real_root: str, may_continue: Callable[[], bool]) -> dict:
    """Macht aus dem entpackten Inhalt das fertige Spiel und schiebt es an den Zielort.

    Wirft _Abort; dann liegt nichts am Zielort (bei ROM-Dateien höchstens schon verschobene Dateien,
    die die Meldung nennt)."""
    removed_links = trash.remove_escaping_links(payload)
    target = plan["target"]
    names = sorted(os.listdir(payload))
    if not names:
        raise _Abort("empty", "Das Archiv enthält keine Dateien.")
    result = {"mode": target["mode"], "removed_links": removed_links}

    if target["mode"] == "game" and plan.get("kind") == "windows-installer":
        return dict(result, **_place_setup(plan, spec, payload, real_root, may_continue))

    if target["mode"] == "game":
        # Ein einzelner echter Oberordner wird zum Spielordner (Beigaben daneben wie Lies-mich oder eine
        # Verknüpfung bleiben im Zwischenordner zurück und verschwinden mit ihm), sonst der ganze Inhalt.
        # Entschieden wird am entpackten Inhalt, nicht an der Auflistung.
        dirs = [n for n in names if _is_real_dir(os.path.join(payload, n))]
        others = [n for n in names if n not in dirs]
        top = None
        if len(dirs) == 1 and all(_is_real_file(os.path.join(payload, n)) for n in others) and \
                probe.only_strays([(n, os.path.getsize(os.path.join(payload, n))) for n in others]):
            top = dirs[0]
            result["dropped"] = others
        source = os.path.join(payload, top) if top else payload
        if top:
            # Nur der Oberordner wandert an den Zielort: Verweise gegen ihn prüfen, sonst zeigte
            # „Spiel/nachbar -> ../Anderes Spiel“ danach auf ein Nachbarspiel im Spiele-Ordner
            result["removed_links"] = removed_links + [
                top + "/" + r for r in trash.remove_escaping_links(payload, within=source)]
        if not os.listdir(source):
            raise _Abort("empty", "Das Archiv enthält keine Dateien.")
        os.chmod(source, 0o755)              # ein schreibgeschützter Oberordner ließe sich nicht verschieben
        candidates = _verified_candidates(plan, top, source)
        try:
            write_marker(source, marker_fields(spec, plan, real_root))
        except OSError as exc:
            raise _Abort("bad-content", "Die Markerdatei konnte nicht geschrieben werden.", str(exc.strerror or exc))
        if not may_continue():
            raise _Abort("cancelled", MSG_CANCELLED)
        _move(source, target["dir"])
        result.update(path=target["dir"], candidates=candidates, needs_choice=probe.needs_choice(candidates))
        return result

    if target["mode"] == "rom-folder":
        # Wii U: der Ordner mit code/*.rpx und meta/meta.xml, auf der Platte gesucht (nicht der Auflistung geglaubt)
        source = None
        for dirpath, dirnames, _filenames in os.walk(payload, followlinks=False):
            dirnames.sort()
            code, meta = os.path.join(dirpath, "code"), os.path.join(dirpath, "meta", "meta.xml")
            if _is_real_dir(code) and _is_real_file(meta) and any(n.lower().endswith(".rpx") for n in os.listdir(code)):
                source = dirpath
                break
        if source is None:
            raise _Abort("bad-content", "Im Archiv wurde kein Wii-U-Spielordner gefunden.")
        if source != payload:
            result["removed_links"] = removed_links + [
                os.path.relpath(source, payload) + "/" + r
                for r in trash.remove_escaping_links(payload, within=source)]
        os.chmod(source, 0o755)
        if not may_continue():
            raise _Abort("cancelled", MSG_CANCELLED)
        _move(source, target["dir"])
        result.update(path=target["dir"], files=[target["name"]])
        return result

    # rom-files: jede ROM-Datei einzeln in den ROM-Ordner; vorhandene Dateien werden nie ersetzt
    system = plan["system"]
    found = {}
    for dirpath, dirnames, filenames in os.walk(payload, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            if probe.ROM_EXT.get(name.rsplit(".", 1)[-1].lower()) == system and _is_real_file(os.path.join(dirpath, name)):
                safe = probe.safe_folder_name(name)
                if safe in found:
                    raise _Abort("bad-content", "Das Archiv enthält mehrere ROM-Dateien mit demselben Namen.")
                found[safe] = os.path.join(dirpath, name)
    if not found:
        raise _Abort("bad-content", "Im Archiv wurde keine ROM-Datei gefunden.")
    existing = [name for name in found if os.path.lexists(os.path.join(target["dir"], name))]
    if existing:
        raise _Abort("target-exists", "Im ROM-Ordner liegt schon eine Datei mit diesem Namen: %s." % ", ".join(existing))
    if not may_continue():
        raise _Abort("cancelled", MSG_CANCELLED)
    moved = []
    for name, source in found.items():
        try:
            _move(source, os.path.join(target["dir"], name))
        except _Abort as abort:
            if moved:
                abort.message += " Schon verschoben: %s." % ", ".join(moved)
            raise
        moved.append(name)
    result.update(path=target["dir"], files=moved)
    return result


# ---------------------------------------------------------------- Ablauf

def run(spec: dict, emit: Callable[[dict], None], *, statvfs=os.statvfs, watch_interval: float = WATCH_INTERVAL,
        cancel: threading.Event | None = None, own_group: bool = True,
        parent_alive: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic) -> dict:
    """Führt einen Installationsauftrag aus. Blockiert bis zum Ende. Rückgabe = letztes Ereignis.

    spec: {"archive", "password"?, "games_root", "reserve_bytes", "folder"?, "title"?, "final_dir"?,
           "token" (32 Hex-Zeichen, kommt in den Marker), "entry_id"?, "rom_dirs"?, "tools"?, "version"?,
           "parent_pid"? (nur für main(): PID des Auftraggebers)}
    emit(ereignis): bekommt dicts mit "event" in
        "plan"      {kind, platform, system, title, files, bytes, target, warnings}
        "staging"   {path, token}
        "progress"  {percent 0…99, file}
        "state"     {state: "finishing"}
        "cleanup" / "cleanup-refused" {path, reason?}
        am Ende genau eines von:
        "done"      {mode, path, title, kind, platform, system, candidates, needs_choice, files, removed_links, warnings, seconds}
                    E114 mit Installationsprogramm zusätzlich: installer (absoluter Pfad im Zwischenordner),
                    installer_type, keep_staging (der Zwischenordner steht noch)
        "cancelled" {message}
        "failed"    {code, message, detail}
    statvfs, watch_interval, clock: einsteckbar für Tests (Platzwächter).
    cancel: Ereignis, das den Abbruch auslöst. own_group=True startet den Entpacker in eigener
    Prozessgruppe (Vorgabe für Aufrufe im selben Prozess); der Arbeiter-Unterprozess nimmt False,
    weil dort schon der ganze Arbeiter eine eigene Gruppe ist.
    parent_alive: liefert False, sobald der Auftraggeber verschwunden ist (dann Abbruch)."""
    cancel = cancel or threading.Event()
    started = clock()

    def finish(event: str, **data) -> dict:
        data["event"] = event
        emit(data)
        return data

    def fail(code: str, message: str, detail: str = "") -> dict:
        password = spec.get("password")
        if password and detail:
            detail = detail.replace(password, "***")    # sollte nie vorkommen; sicher ist sicher
        return finish("failed", code=code, message=message, detail=detail[-400:])

    # 1 Auftrag prüfen und den Plan selbst neu erstellen
    token = spec.get("token")
    games_root = spec.get("games_root")
    if not isinstance(token, str) or not trash.TOKEN_RE.match(token) or not isinstance(spec.get("archive"), str) \
            or not isinstance(games_root, str):
        return fail("bad-spec", "Der Installationsauftrag ist unvollständig.")
    reserve = int(spec.get("reserve_bytes") or 0)
    tools = spec.get("tools") or None
    password = spec.get("password") or None
    plan = probe.probe(spec["archive"], password, games_root=games_root, reserve_bytes=reserve,
                       rom_dirs=spec.get("rom_dirs") or {}, folder=spec.get("folder") or None,
                       verify_password=False, tools=tools, statvfs=statvfs)
    if not plan["ready"]:
        return fail(plan["status"], plan["message"])
    if plan.get("format") not in probe.SUPPORTED_FORMATS:
        # E114: Ein Installationsprogramm als Datei wird nicht entpackt; das macht der JobManager selbst
        return fail("plan-changed", "Diese Datei ist kein Archiv. Bitte prüfe sie erneut.")
    target = plan["target"]
    if spec.get("final_dir") and os.path.realpath(spec["final_dir"]) != os.path.realpath(target["dir"]):
        return fail("plan-changed", "Das Archiv hat sich seit der Prüfung geändert. Bitte prüfe es erneut.")
    emit({"event": "plan", "kind": plan["kind"], "platform": plan["platform"], "system": plan["system"],
          "title": spec.get("title") or plan["title"], "files": plan["files"], "bytes": plan["unpacked_bytes"],
          "target": target["dir"], "warnings": plan["issues"]})
    real_root = os.path.realpath(games_root)

    # 2 Zwischenordner im Spiele-Ordner (gleiches Dateisystem wie das Ziel)
    try:
        staging, payload, staging_token, lock_fd = make_staging(real_root)
    except OSError as exc:
        return fail("no-staging", "Auf der Spiele-Platte konnte kein Zwischenordner angelegt werden.", str(exc.strerror or exc))
    emit({"event": "staging", "path": staging, "token": staging_token})

    def cleanup() -> None:
        try:
            trash.remove_staging(staging, real_root, staging_token)
            emit({"event": "cleanup", "path": staging})
        except (trash.Refuse, OSError, RecursionError) as exc:
            # Lieber Reste liegen lassen als etwas Fremdes anfassen; der JobManager versucht es beim nächsten Start
            emit({"event": "cleanup-refused", "path": staging, "reason": str(exc)})

    def may_continue() -> bool:
        return not cancel.is_set() and (parent_alive is None or parent_alive())

    try:
        # 3 Entpacken im Käfig, mit Platzwächter
        argv = extractor_argv(plan["format"], plan["archive"], payload, password, tools)
        state, code, tail = _extract(
            argv, ProgressParser(plan["format"], payload), emit,
            free=lambda: probe.free_bytes(real_root, statvfs), reserve=reserve, watch_interval=watch_interval,
            cancel=cancel, own_group=own_group, warnings_ok=plan["format"] != "rar",   # unrar: nur 0 ist gut
            parent_alive=parent_alive, clock=clock)
        if state == "ok" and not may_continue():
            state = "cancelled"
        if state == "ok":
            # 4 Nacharbeit und der Schritt an den Zielort
            emit({"event": "state", "state": "finishing"})
            try:
                result = _install(plan, spec, payload, real_root, may_continue)
            except _Abort as abort:
                cleanup()
                if abort.code == "cancelled":
                    return finish("cancelled", message=MSG_CANCELLED)
                return fail(abort.code, abort.message, abort.detail)
            except (trash.Refuse, OSError) as exc:
                cleanup()
                return fail("install-failed", "Das Spiel konnte nach dem Entpacken nicht eingerichtet werden.", str(exc))
            # 5 Zwischenordner entfernen (der Inhalt ist schon am Zielort). E114: Mit einem Installationsprogramm
            #   bleibt er stehen; der JobManager lässt es daraus laufen und entfernt ihn danach
            if not result.get("keep_staging"):
                cleanup()
            return finish("done", title=spec.get("title") or plan["title"], kind=plan["kind"],
                          platform=plan["platform"], system=plan["system"], warnings=plan["issues"],
                          seconds=round(clock() - started, 1), **result)
        cleanup()
        if state in ("cancelled", "orphaned"):
            return finish("cancelled", message=MSG_CANCELLED)
        if state == "no-space":
            return fail("no-space", "Der Speicherplatz auf der Spiele-Platte wird knapp (weniger als %s frei). "
                                    "Die Installation wurde abgebrochen." % human_size(reserve))
        if state == "disk-gone":
            return fail("no-target", "Die Spiele-Platte ist während der Installation nicht mehr erreichbar.")
        if state == "no-tool":
            return fail("no-sandbox", "Der geschützte Entpacker (bubblewrap) fehlt auf diesem System.", tail)
        code_name, message = _failure(plan["format"], code, tail)
        return fail(code_name, message, tail)
    except BaseException:
        cleanup()                            # unerwarteter Fehler: nichts halb Entpacktes zurücklassen
        raise
    finally:
        os.close(lock_fd)


# ---------------------------------------------------------------- Store-Ordner (E100)
# itch.io-Spiele kommen oft als Archiv. Anders als run() gibt es den Spielordner dann schon: Die
# Warteschlange des Stores legt <Spiele-Ordner>/itch.io/<Titel> mit unserem Marker an und lädt in einen
# Unterordner darin (keep). unpack_into() entpackt genau dorthin, mit denselben Schutzregeln wie run():
# Werkzeug im Käfig (nur der Zwischenordner beschreibbar, kein Netz), Zwischenordner im Spielordner
# (gleiches Dateisystem), Platzwächter, hinausführende Verweise weg, einzelner Oberordner aufgelöst,
# verschoben nur per rename ohne Ersetzen. probe() und check_games_root() laufen hier nicht: Der Ordner
# liegt eine Ebene tiefer als der Spiele-Ordner und ist nachweislich unserer (Marker mit Zufallswert).

STORE_RESERVE = 1 << 30          # so viel bleibt beim Entpacken eines Store-Spiels mindestens frei
TAR_FORMATS = ("tar", "gz", "xz", "bz2", "zst")
LISTING_LIMIT = 200_000          # so viele Einträge wertet die Startdatei-Suche höchstens aus


def tar_argv(archive: str, payload: str, tools: dict | None) -> list:
    """tar im Käfig (E100). Die Kompression erkennt GNU tar selbst; Besitzer werden nicht übernommen.
    Was aus dem Inhalt hinausführte, verhindern der Käfig (nur payload beschreibbar) und danach
    remove_escaping_links()."""
    argv = probe.tool_argv(tools, "tar") + ["-x", "--no-same-owner", "--no-overwrite-dir", "-f", archive, "-C", payload]
    return probe.sandbox_argv(argv, read_only=[os.path.dirname(archive)], writable=payload, tools=tools)


def _listing(source: str, names: list) -> list:
    """Einträge (wie list_7z) der Namen in source, rekursiv, ohne Verweisen zu folgen. Nur lesend."""
    out = []
    for name in names:
        full = os.path.join(source, name)
        if _is_real_dir(full):
            out.append({"path": name, "size": 0, "dir": True, "encrypted": False, "link": False})
            for dirpath, dirnames, filenames in os.walk(full, followlinks=False):
                dirnames.sort()
                rel_dir = os.path.relpath(dirpath, source)
                for d in dirnames:
                    out.append({"path": rel_dir + "/" + d, "size": 0, "dir": True, "encrypted": False, "link": False})
                for f in sorted(filenames):
                    path = os.path.join(dirpath, f)
                    try:
                        st = os.lstat(path)
                    except OSError:
                        continue
                    out.append({"path": rel_dir + "/" + f, "size": st.st_size if stat.S_ISREG(st.st_mode) else 0,
                                "dir": False, "encrypted": False, "link": stat.S_ISLNK(st.st_mode)})
                if len(out) > LISTING_LIMIT:
                    return out
        else:
            try:
                st = os.lstat(full)
            except OSError:
                continue
            out.append({"path": name, "size": st.st_size if stat.S_ISREG(st.st_mode) else 0, "dir": False,
                        "encrypted": False, "link": stat.S_ISLNK(st.st_mode)})
    return out


def _into(payload: str, real: str, title: str, skip: set, platform: str, may_continue: Callable[[], bool]) -> dict:
    """Entpackten Inhalt in den Spielordner real schieben (unpack_into). Wirft _Abort; was schon
    verschoben ist, bleibt im (eigenen) Spielordner, den der Aufrufer dann als Ganzes wegräumt."""
    removed = trash.remove_escaping_links(payload)
    names = sorted(os.listdir(payload))
    if not names:
        raise _Abort("empty", "Das Archiv enthält keine Dateien.")
    # Ein einzelner echter Oberordner wird aufgelöst (wie run()); Beigaben daneben bleiben zurück
    dirs = [n for n in names if _is_real_dir(os.path.join(payload, n))]
    others = [n for n in names if n not in dirs]
    top = None
    if len(dirs) == 1 and all(_is_real_file(os.path.join(payload, n)) for n in others) and \
            probe.only_strays([(n, os.path.getsize(os.path.join(payload, n))) for n in others]):
        top = dirs[0]
    source = os.path.join(payload, top) if top else payload
    if top:
        # Gemessen gegen das, was wirklich wandert (sonst zeigte „../Nachbar“ danach auf ein Nachbarspiel)
        removed = removed + [top + "/" + r for r in trash.remove_escaping_links(payload, within=source)]
        os.chmod(source, 0o755)                  # ein schreibgeschützter Oberordner gäbe nichts heraus
    # Unser Marker und der Download-Ordner werden nie aus dem Archiv übernommen
    moving = [n for n in sorted(os.listdir(source)) if n not in skip and not n.startswith(trash.STAGING_PREFIX)]
    if not moving:
        raise _Abort("empty", "Das Archiv enthält keine Dateien.")
    if not may_continue():
        raise _Abort("cancelled", MSG_CANCELLED)
    moved = []
    for name in moving:
        full = os.path.join(source, name)
        if _is_real_dir(full):
            # rename eines Ordners an einen anderen Ort braucht Schreibrecht auf ihn (Eintrag „..“)
            os.chmod(full, stat.S_IMODE(os.lstat(full).st_mode) | 0o700)
        _move(full, os.path.join(real, name))
        moved.append(name)
    found = start_candidates(real, title, skip=tuple(skip), platform=platform)
    return dict(found, moved=moved, removed_links=removed, top=top)


def start_candidates(folder: str, title: str, skip: tuple = (), platform: str = "") -> dict:
    """Startdateien eines fertigen Spielordners bewerten, wie beim Archiv-Installer (probe.classify),
    ohne die Namen aus skip (Marker, Download-Ordner) und ohne Zwischenordner (E100: nach dem Entpacken
    oder einem Installer eines Stores). Nur lesend.
    Rückgabe: {"kind", "platform", "candidates" [{path, score, why}], "needs_choice", "installer"}."""
    real = os.path.realpath(folder)
    skip = set(skip) | {trash.MARKER_NAME}
    try:
        names = [n for n in sorted(os.listdir(real)) if n not in skip and not n.startswith(trash.STAGING_PREFIX)]
    except OSError:
        names = []
    content = probe.classify(_listing(real, names), title or os.path.basename(real)) if names else {}
    kind = content.get("kind") or "unknown"
    plat = "linux" if kind == "linux-game" else "windows" if kind == "windows-game" else platform or "windows"
    # archive_path ist hier der Pfad im Spielordner (path ohne einen einzelnen Oberordner)
    candidates = []
    for cand in content.get("candidates") or []:
        clean = verify_start_file(real, cand.get("archive_path") or "")
        if clean:
            candidates.append({"path": clean, "score": cand.get("score", 0), "why": cand.get("why", [])})
    if not candidates:
        candidates = [c for c in scan_start_files(real, plat)
                      if c["path"].split("/")[0] not in skip and not c["path"].startswith(trash.STAGING_PREFIX)]
    installer = ""
    if kind == "windows-installer" and content.get("exe"):
        rel = content["exe"]
        if content.get("layout") == "single-folder" and content.get("top_folder"):
            rel = content["top_folder"] + "/" + rel
        installer = verify_start_file(real, rel) or ""
    return {"kind": kind, "platform": plat, "candidates": candidates[:12], "needs_choice": probe.needs_choice(candidates),
            "installer": installer}


def unpack_into(archive: str, target: str, token: str, *, title: str = "", platform: str = "", keep: tuple = (),
                reserve: int = STORE_RESERVE, cancel: threading.Event | None = None, tools: dict | None = None,
                statvfs=os.statvfs, watch_interval: float = WATCH_INTERVAL,
                clock: Callable[[], float] = time.monotonic, emit: Callable[[dict], None] | None = None) -> dict:
    """Archiv eines Stores in seinen eigenen, frisch angelegten Spielordner entpacken (E100). Blockiert.

    target: echter Ordner (kein Verweis) mit unserem Marker (Zufallswert token), in dem sonst nur die
    Namen aus keep liegen (der Download-Ordner, darin das Archiv). Alte Zwischenordner eines
    abgebrochenen Laufs darin werden über trash.remove_staging() entfernt. zip/7z/rar mit 7z bzw. unrar,
    tar (auch .gz/.xz/.bz2/.zst) mit tar, jeweils im Käfig.
    Rückgabe: {"ok": True, "kind" (probe.classify), "platform", "candidates" [{path, score, why}],
    "needs_choice", "installer" (Installer im Archiv, Pfad im Spielordner, sonst ""), "moved", "removed_links"}
    oder {"ok": False, "code", "message", "detail"}; Abbruch über cancel: code "cancelled".
    emit bekommt {"event": "progress", "percent", "file"} wie bei run()."""
    cancel = cancel or threading.Event()
    emit = emit or (lambda event: None)

    def fail(code: str, message: str, detail: str = "") -> dict:
        return {"ok": False, "code": code, "message": message, "detail": str(detail or "")[-400:]}

    # 1 Der Spielordner: echt, unser Marker mit diesem Zufallswert, sonst nur keep
    if not isinstance(token, str) or not trash.TOKEN_RE.match(token) or not isinstance(archive, str):
        return fail("bad-spec", "Der Installationsauftrag ist unvollständig.")
    if not isinstance(target, str) or not os.path.isabs(target) or os.path.islink(target) or not _is_real_dir(target):
        return fail("no-target", "Der Spielordner fehlt oder ist kein echter Ordner.")
    real = os.path.realpath(target)
    violation = test_mode_violation(real)
    if violation:
        return fail("test-mode", violation)
    try:
        marker = trash.read_marker(real)
    except trash.Refuse:
        return fail("marker-missing", "Im Spielordner fehlt unser Marker. Er wird nicht angefasst.")
    if marker.get("token") != token:
        return fail("marker-mismatch", "Der Marker im Spielordner gehört zu einem anderen Auftrag. Er wird nicht angefasst.")
    for item in trash.list_staging(real):        # Rest eines Laufs, den ein Neustart unterbrochen hat
        if staging_in_use(item["path"]):
            return fail("busy", "In diesem Spielordner wird gerade schon entpackt.")
        try:
            trash.remove_staging(item["path"], real, item["token"])
        except (trash.Refuse, OSError) as exc:
            return fail("staging-left", "Im Spielordner liegt ein alter Zwischenordner, der sich nicht entfernen lässt.",
                        str(exc))
    skip = {trash.MARKER_NAME} | {str(k) for k in keep}
    if [n for n in os.listdir(real) if n not in skip]:
        return fail("target-not-empty", "Im Spielordner liegt schon etwas anderes. Es wird nichts überschrieben.")
    if not _is_real_file(archive):
        return fail("no-archive", "Die geladene Datei fehlt.")
    fmt = probe.sniff(archive)
    if fmt not in ("zip", "7z", "rar") + TAR_FORMATS:
        return fail("unsupported", "Diese Art Archiv kennt der PC5 Launcher nicht.")

    # 2 Zwischenordner im Spielordner (gleiches Dateisystem wie das Ziel)
    try:
        staging, payload, staging_token, lock_fd = make_staging(real)
    except OSError as exc:
        return fail("no-staging", "Im Spielordner konnte kein Zwischenordner angelegt werden.", str(exc.strerror or exc))

    def may_continue() -> bool:
        return not cancel.is_set()

    try:
        # 3 Entpacken im Käfig, mit Platzwächter
        tar = fmt in TAR_FORMATS
        argv = tar_argv(archive, payload, tools) if tar else extractor_argv(fmt, archive, payload, None, tools)
        state, code, tail = _extract(
            argv, ProgressParser("tar" if tar else fmt, payload), emit,
            free=lambda: probe.free_bytes(real, statvfs), reserve=int(reserve), watch_interval=watch_interval,
            cancel=cancel, own_group=True, warnings_ok=fmt in ("zip", "7z"), parent_alive=None, clock=clock)
        if state == "ok" and not may_continue():
            state = "cancelled"
        if state in ("cancelled", "orphaned"):
            return fail("cancelled", MSG_CANCELLED)
        if state == "no-space":
            return fail("no-space", "Der Speicherplatz wird knapp (weniger als %s frei). Die Installation wurde "
                                    "abgebrochen." % human_size(reserve))
        if state == "disk-gone":
            return fail("no-target", "Die Spiele-Platte ist während der Installation nicht mehr erreichbar.")
        if state == "no-tool":
            return fail("no-sandbox", "Der geschützte Entpacker (bubblewrap) fehlt auf diesem System.", tail)
        if state != "ok":
            code_name, message = _failure("tar" if tar else fmt, code, tail)
            return fail(code_name, message, tail)
        # 4 Nacharbeit und die Schritte in den Spielordner
        try:
            result = _into(payload, real, title, skip, platform, may_continue)
        except _Abort as abort:
            return fail(abort.code, abort.message, abort.detail)
        except (trash.Refuse, OSError) as exc:
            return fail("install-failed", "Das Spiel konnte nach dem Entpacken nicht eingerichtet werden.", str(exc))
        return dict(result, ok=True)
    finally:
        try:
            trash.remove_staging(staging, real, staging_token)
        except (trash.Refuse, OSError, RecursionError) as exc:
            # lieber liegen lassen als etwas Fremdes anfassen; der nächste Lauf versucht es wieder
            emit({"event": "cleanup-refused", "path": staging, "reason": str(exc)})
        os.close(lock_fd)


def main(argv: list | None = None, *, statvfs=os.statvfs, watch_interval: float = WATCH_INTERVAL) -> int:
    """Arbeiter-Unterprozess: Auftrag als JSON von stdin, Ereignisse als JSON-Zeilen nach stdout.

    Rückgabewert: 0 fertig, 130 abgebrochen, 1 fehlgeschlagen, 2 kein lesbarer Auftrag."""
    try:
        spec = json.loads(sys.stdin.read())
        if not isinstance(spec, dict):
            raise ValueError("kein Objekt")
    except ValueError:
        sys.stdout.write(json.dumps({"event": "failed", "code": "bad-spec",
                                     "message": "Der Installationsauftrag ist nicht lesbar.", "detail": ""}) + "\n")
        return 2
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_args: cancel.set())
    try:
        # Ein laufendes Spiel soll vom Entpacken nichts merken (E30). Der Wert vererbt sich an
        # bubblewrap und 7z/unrar; die Platten-Priorität (ionice -c3) setzt der Aufrufer davor.
        os.nice(WORKER_NICE)
    except OSError:
        pass
    # Der Auftraggeber nennt seine PID: So fällt auch auf, wenn er schon vor diesem Punkt verschwunden ist
    parent = spec.get("parent_pid") if isinstance(spec.get("parent_pid"), int) else os.getppid()

    def emit(event: dict) -> None:
        try:
            sys.stdout.write(jsontext.dumps(event) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            cancel.set()                     # niemand hört mehr zu: abbrechen und aufräumen

    result = run(spec, emit, statvfs=statvfs, watch_interval=watch_interval, cancel=cancel, own_group=False,
                 parent_alive=lambda: os.getppid() == parent)
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)     # kaputte Leitung: beim Beenden nicht noch einmal stolpern
    return {"done": 0, "cancelled": 130}.get(result.get("event"), 1)
