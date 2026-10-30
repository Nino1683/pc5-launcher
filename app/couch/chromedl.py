"""Downloads aus Chrome im Download-Bereich der Konsole (E146, Wunsch 2026-10-30: „Könnte man auch die downloads die
aus chrome gestartet wird in den downloadsbereich vom pc5 Launcher packen?“).

Nur lesend, Chrome bleibt unberührt:

  Verlauf    <Profil>/Default/History, Tabelle downloads (sqlite3 mit mode=ro&immutable=1: keine Sperre, keine
             Kopie; Chrome schreibt sie in Schüben alle paar Sekunden). Gezeigt wird nur, was seit `since` begann
             (der erste Start dieser Fassung), höchstens KEEP: nicht der ganze alte Verlauf.
  Ordner     *.crdownload im Download-Ordner von Chrome (Einstellung download.default_directory in Preferences,
             sonst der Download-Ordner des Nutzers): Ein Download erscheint sofort, bevor er im Verlauf steht, und
             sein Fortschritt kommt aus der wachsenden Datei.

Zustände von Chrome: 0 läuft, 1 fertig, 2 abgebrochen, 4 unterbrochen. Ändert sich eine laufende .crdownload-Datei
STALE_S lang nicht (Chrome beendet, Absturz), gilt der Download als abgebrochen: Sonst sperrte er Ruhemodus und
Bildschirmschoner („Ein Download läuft.“). Anhalten, Fortsetzen und Abbrechen gehen nur in Chrome; „Entfernen“
blendet aus (wie die Downloads des eigenen Browsers, fetcher.py).

Prüfmodus: Profil nur aus COUCH_TEST_CHROME_PROFILE, nie das echte.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Callable

from . import log, paths

_log = log.get("chromedl")

PROFILE = os.path.join("~", ".var", "app", "com.google.Chrome", "config", "google-chrome")
CHROME_EPOCH_S = 11644473600            # Chrome zählt Mikrosekunden seit 1601-01-01
STATES = {0: "running", 1: "done", 2: "cancelled", 4: "failed"}
PARTIAL = ".crdownload"
STALE_S = 120.0
KEEP = 15
TEXT_INTERRUPTED = "Chrome hat den Download unterbrochen."
TEXT_STOPPED = "Abgebrochen: Chrome lädt nicht mehr."


def profile_dir() -> str | None:
    if paths.test_mode():
        return os.environ.get("COUCH_TEST_CHROME_PROFILE") or None
    return os.path.expanduser(PROFILE)


def unix_time(chrome_us) -> float:
    """Zeit aus dem Verlauf (Mikrosekunden seit 1601) in Sekunden seit 1970; 0 für leer."""
    try:
        value = int(chrome_us or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 1e6 - CHROME_EPOCH_S if value > 0 else 0.0


def download_dir(profile: str, fallback: str) -> str:
    """Der Download-Ordner von Chrome: download.default_directory aus Preferences, sonst fallback."""
    try:
        with open(os.path.join(profile, "Default", "Preferences"), encoding="utf-8") as fh:
            data = json.load(fh)
        folder = ((data.get("download") or {}).get("default_directory") or "") if isinstance(data, dict) else ""
    except (OSError, ValueError, AttributeError):
        folder = ""
    return folder if isinstance(folder, str) and os.path.isabs(folder) else fallback


def _size(path: str) -> tuple[int, float] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_size, st.st_mtime


def read_history(profile: str, since: float) -> list[dict] | None:
    """Downloads aus dem Verlauf, die seit since begannen (neueste zuerst). None, wenn er gerade unlesbar ist."""
    path = os.path.join(profile, "Default", "History")
    if not os.path.isfile(path):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro&immutable=1" % path, uri=True, timeout=1)
        try:
            rows = con.execute(
                "select guid, current_path, target_path, start_time, received_bytes, total_bytes, state, end_time "
                "from downloads where start_time >= ? order by start_time desc limit ?",
                (int((since + CHROME_EPOCH_S) * 1e6), KEEP)).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:              # Chrome schreibt gerade: beim nächsten Blick wieder
        _log.debug("Verlauf von Chrome nicht lesbar: %s", e)
        return None
    out = []
    for guid, current, target, start, received, total, state, end in rows:
        if state not in STATES or not isinstance(guid, str) or not guid:
            continue
        target = target if isinstance(target, str) else ""
        current = current if isinstance(current, str) else ""
        name = os.path.basename(target or current)
        if not name or not os.path.isabs(target or current):
            continue
        out.append({"id": "chrome:" + guid, "name": name, "path": target or current, "partial": current,
                    "received": int(received or 0), "total": max(0, int(total or 0)), "state": STATES[state],
                    "error": TEXT_INTERRUPTED if state == 4 else "", "started": unix_time(start),
                    "finished": unix_time(end), "source": "chrome"})
    return out


def scan_partials(folder: str, since: float) -> list[dict]:
    """*.crdownload im Download-Ordner, die seit since entstanden (laufend, ohne Gesamtgröße)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(PARTIAL) or name.startswith("."):
            continue
        path = os.path.join(folder, name)
        got = _size(path)
        if got is None or got[1] < since:
            continue
        final = name[:-len(PARTIAL)] or name
        out.append({"id": "chrome-file:" + name, "name": final, "path": os.path.join(folder, final), "partial": path,
                    "received": got[0], "total": 0, "state": "running", "error": "", "started": got[1],
                    "finished": 0.0, "source": "chrome"})
    return out


def collect(profile: str, fallback_dir: str, since: float, clock: Callable[[], float] = time.time,
            previous: list | None = None) -> list[dict]:
    """Die Chrome-Downloads für den Download-Bereich: Verlauf plus laufende Dateien, die er noch nicht kennt.
    Ist der Verlauf gerade unlesbar, gilt für ihn der vorige Stand."""
    history = read_history(profile, since)
    if history is None:
        history = [i for i in previous or [] if str(i.get("id", "")).startswith("chrome:")]
    now = clock()
    known = set()
    for item in history:
        if item["partial"]:
            known.add(item["partial"])
        if item["state"] != "running":
            if item["state"] == "done" and not item["received"]:
                got = _size(item["path"])
                item["received"] = got[0] if got else item["total"]
            continue
        got = _size(item["partial"]) if item["partial"] else None
        if got is None:
            continue                         # Datei schon umbenannt: der Verlauf meldet gleich „fertig“
        item["received"] = max(item["received"], got[0])
        if now - got[1] > STALE_S:
            item.update(state="cancelled", error=TEXT_STOPPED)
    files = [f for f in scan_partials(download_dir(profile, fallback_dir), since) if f["partial"] not in known]
    for item in files:
        got = _size(item["partial"])
        if got is not None and now - got[1] > STALE_S:
            item.update(state="cancelled", error=TEXT_STOPPED)
    out = files + history
    out.sort(key=lambda i: i["started"], reverse=True)
    return out[:KEEP]
