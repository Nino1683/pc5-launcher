"""Apps im Home (E140): erkannte Flatpaks live, Deinstallieren, Bazaar wieder zeigen. Eingehängt über wiring.PARTS.

Wunsch 2026-10-29: „Bazaar möchte ich auch im pc5 Launcher wenn ich darin etwas installiere soll es auch im pc5
launcher erscheinen. … Die karten sollen auch verschiebbar sein zum sortieren und deinstallieren etc“.

  Live            Gio.FileMonitor je Installation (Quelle flatpaks: ctx.flatpak_dirs, im Prüfmodus nur der
                  Attrappen-Baum) auf <root>/exports/share/applications; fehlt der Ordner, auf den nächsten vorhandenen
                  bis hinauf zum Elternordner von <root>, bis er entsteht. Entprellt (DEBOUNCE_S), dann liest die
                  Bibliothek neu (wiring_library.rescan, die Seite bekommt lib.changed). Bazaar installiert, während
                  es offen ist: Die Kachel kommt bzw. geht ohne Neustart.
  Deinstallieren  lib.get nennt für jede Kachel mit einem Flatpak dahinter (erkannte und die festen YouTube, Kodi,
                  Jellyfin, Moonlight, Discord, Chrome) die Aktion uninstall {flatpak}. RPC apps.uninstall {id} ->
                  {ok, message} im Arbeits-Thread: `flatpak uninstall --noninteractive -y --user|--system <id>`, nur
                  genau diese Kennung und nur in der Installation, aus der sie startet; nie --delete-data, nie
                  Runtimes, nie --unused. Nicht, solange sie läuft (auch nicht an anderer Stelle, flatpak ps). Ohne
                  Kennwort: --noninteractive lässt flatpak nicht nachfragen (Polkit ohne Rückfrage, etwa per Regel für
                  wheel), eine eigene Sitzung ohne Terminal (setsid) ebenso; verlangt das System eine Anmeldung, bricht
                  es mit einem Satz ab (SAFETY 13). Danach liest die Bibliothek neu. Prüfmodus: nie das echte flatpak,
                  nur die Attrappe aus COUCH_FLATPAK_UNINSTALL (fake-…, tools/fake-flatpak-uninstall; so wie die
                  Einrichtung ihre aus COUCH_SETUP_FLATPAK nimmt), sonst nur eine Zeile in writes.log.
  Bazaar          Läuft Bazaar ohne Fenster weiter (Dienst nach dem Schließen), startet die Kachel es noch einmal mit
                  der Umgebung der App (Shell.reveal, wie Discord E139); bazaar-daemon reicht das an den laufenden
                  weiter. Am Gerät unbestätigt.

E141 (Wunsch 2026-10-30):
  App Store zu    „Wenn man ihn schließt soll er beendet werden nur wenn ein download und update läuft soll gefragt
                  werden“. Solange Bazaar läuft, sieht der Teil alle POLL_S nach (Hauptschleife): Hatte es schon ein
                  Fenster (app.visible) und hat seit WINDOW_GONE_S keins mehr (X, „Quit Bazaar“; der Dienst bliebe ohne
                  Fenster), wird es über den normalen Weg beendet (Shell.close_app: höflich, nach der Schonfrist SIGKILL
                  an den Baum des eigenen reaper; nie über Namen). Kurz nach dem Start (noch nie ein Fenster) und nach
                  dem Zeigen (reveal, REVEAL_GRACE_S) nicht. Läuft eine Flatpak-Übertragung (flatpaklock: Sperre auf
                  repo/.lock einer Installation oder revokefs-fuse), wartet er, bis seit QUIET_S keine mehr zu sehen
                  ist (Pausen zwischen zwei Schritten einer Übertragung), und beendet es dann ohne Fenster.
                  Shell.close_guards[app:bazaar]: Mit laufender Übertragung fragen app.close (Seite: app.closeNote) und
                  PS + Options erst nach; busy_checks: Ein/Aus, Neustart und Übergabe nennen die Übertragung.
  Ausgeblendet    hidden_apps()/show_app() für Einstellungen › Apps › Ausgeblendete Apps (settings/apps.py): Apps
                  der Bibliothek mit meta.hidden (lib.hide) und die Kacheln der Seite aus home-order.json (wiring_home)
  Claude Code     „Baue einen switch ein claude code nach einem neustart mitstarten und die letzte Unterhaltung laden“:
                  mit claude.autostart nach dem Bereit-Signal bzw. dem ersten Einlesen einmal je Sitzung, nur mit der
                  Kachel (claude installiert), nicht, wenn es schon läuft: Shell.launch_background(app:claude) mit
                  COUCH_TERM_RUN=continue (bin/couch-terminal startet dann ohne Auswahl `claude --continue` im
                  Home-Ordner; die Merkmale einer Claude-Sitzung erbt das Kind wie seit E72 nicht). Prüfmodus: nie das
                  echte claude (der Teil startet nur über die Shell, im Test eine Attrappe).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import flatpaklock, log, paths, system
from .library import flatpaks

_log = log.get("wiring.apps")

DEBOUNCE_S = 2.0
UNINSTALL_TIMEOUT_S = 300.0
BAZAAR_ID = "app:bazaar"
EMU_PREFIX = "app:emu-"
# E141: App Store beim Schließen beenden
POLL_S = 2.0                # Takt, solange der App Store läuft
WINDOW_GONE_S = 4.0         # so lange ohne Fenster, bevor er beendet wird (ein Dialog ersetzt das Fenster nur kurz)
REVEAL_GRACE_S = 20.0       # nach dem Zeigen (reveal): so lange darf sein Fenster noch ausbleiben
QUIET_S = 6.0               # so lange ohne Sperre, bis eine Übertragung als fertig gilt
TEXT_BAZAAR_BUSY = "Im App Store läuft gerade ein Download oder Update. Beim Schließen wird er abgebrochen."
TEXT_BAZAAR_POWER = "Im App Store läuft gerade ein Download oder Update."   # Ein/Aus nennt ihn schon unter „geschlossen“
# E141: Claude Code mitstarten
CLAUDE_ID = "app:claude"
CLAUDE_ENV = {"COUCH_TERM_RUN": "continue"}     # Startart von bin/couch-terminal (terminal/window.py)
DEFAULTS = {"claude.autostart": False}         # wie settings/apps.py DEFAULTS und config.DEFAULTS
# E141: Kacheln der Seite, die „Ausblenden“ über home-order.json kennen (wiring_home.TILES): Name und Reihe
TILE_TITLES = {"sys:files": ("Dateien", "apps"), "sys:gallery": ("Mediengalerie", "media")}
TEXT_GONE = "Diese App gibt es nicht mehr."
TEXT_NOT_FLATPAK = "Diese App lässt sich hier nicht deinstallieren."
TEXT_BUSY = "Es wird gerade schon eine App deinstalliert. Warte, bis das fertig ist."
TEXT_RUNNING = "Schließe „%s“ zuerst, dann lässt es sich deinstallieren."
TEXT_RUNNING_ELSEWHERE = "„%s“ läuft gerade an anderer Stelle, zum Beispiel im Desktop. Schließe es dort zuerst."
TEXT_UNCHECKED = "Ob „%s“ gerade an anderer Stelle läuft, ließ sich nicht prüfen. Versuch es gleich noch einmal."
TEXT_NOT_INSTALLED = "„%s“ ist nicht mehr installiert."
TEXT_NO_SOURCES = "Wo „%s“ installiert ist, lässt sich gerade nicht prüfen. Versuch es gleich noch einmal."
TEXT_DONE = "„%s“ wurde deinstalliert."
TEXT_DONE_OTHER = "„%s“ wurde deinstalliert. In der anderen Flatpak-Installation ist es noch vorhanden."
TEXT_NO_AUTH = ("Für „%s“ fehlt die Freigabe zum Deinstallieren aus der System-Installation, und nach einem Kennwort "
                "fragt der PC5 Launcher nie. Deinstalliere es im Desktop-Modus, zum Beispiel in Bazaar.")
TEXT_TIMEOUT = "Das Deinstallieren von „%s“ hat zu lange gedauert und wurde abgebrochen."
TEXT_FAILED = "„%s“ ließ sich nicht deinstallieren: %s"
TEXT_NO_FAKE = "Im Prüfmodus fehlt die Flatpak-Attrappe."
TEXT_BUSY_CHECK = "Gerade wird „%s“ deinstalliert."
# Worte, an denen flatpak (Sprache C) eine fehlende Freigabe meldet
_AUTH_WORDS = ("not allowed", "not authorized", "authoriz", "authentication", "polkit", "permission denied")


def flatpak_of(entry: Any) -> str:
    """Die Flatpak-Kennung hinter einer Kachel, die sich deinstallieren lässt, sonst "" (E140): erkannte Flatpaks und
    feste Apps, die ein Flatpak starten (YouTube, Kodi, Jellyfin, Moonlight, Discord, Chrome als „Browser“). Nicht die
    ausgeblendeten Emulator-Einträge app:emu-* (die verwaltet die Einrichtung)."""
    detail = getattr(entry, "detail", None)
    app_id = detail.get("flatpak") if isinstance(detail, dict) else None
    if not flatpaks.valid_id(app_id):
        return ""
    source = getattr(entry, "source", "")
    if source == "flatpak":
        return app_id
    if source == "app" and detail.get("launcher") == "flatpak" and not str(entry.id).startswith(EMU_PREFIX) \
            and not getattr(entry, "hidden", False):
        return app_id
    return ""


def uninstall_argv(app_id: str, installation: str) -> list | None:
    """Der eine erlaubte Befehl (SAFETY 13, E140). None bei allem, was nicht genau passt."""
    if not flatpaks.valid_id(app_id) or installation not in ("user", "system"):
        return None
    return ["flatpak", "uninstall", "--noninteractive", "-y", "--" + installation, app_id]


def fake_tool() -> str | None:
    """Prüfmodus: die Attrappe aus COUCH_FLATPAK_UNINSTALL (Dateiname fake-…), sonst keine."""
    fake = os.environ.get("COUCH_FLATPAK_UNINSTALL") or ""
    if fake and os.path.basename(fake).startswith("fake-") and os.access(fake, os.X_OK):
        return fake
    return None


def failure_text(title: str, output: str) -> str:
    low = (output or "").lower()
    if any(word in low for word in _AUTH_WORDS):
        return TEXT_NO_AUTH % title
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    last = lines[-1] if lines else "unbekannter Fehler"
    return TEXT_FAILED % (title, last[:200])


class AppsPart:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self._lock = threading.Lock()
        self._busy = ""                         # Titel der App, die gerade deinstalliert wird
        self._monitors: dict[str, tuple] = {}   # Wurzel -> (beobachteter Ordner, Gio.FileMonitor)
        self._timer: threading.Timer | None = None
        self._stopped = False
        # E141: App Store
        self._bazaar_timer: Any = 0
        self._gone_since: float | None = None   # seit wann er kein Fenster mehr hat
        self._revealed_at = -1e9                # zuletzt gezeigt (reveal)
        self._busy_at: float | None = None      # zuletzt eine Übertragung gesehen
        self._waiting_said = False
        # E141: Claude Code
        self._claude_done = False

    # ------------------------------------------------------------ Hilfen
    def ctx(self) -> Any:
        return getattr(self.wiring, "sources", None)

    def roots(self) -> list:
        ctx = self.ctx()
        return list(getattr(ctx, "flatpak_dirs", None) or [])

    def rescan(self) -> None:
        fn = getattr(self.wiring, "apps_rescan", None)
        if fn is not None:
            fn()
            return
        from . import wiring_library
        try:
            wiring_library.rescan(self.app)
        except Exception:
            _log.exception("Bibliothek neu einlesen")

    def main_call(self, fn: Any, *args: Any) -> Any:
        main = getattr(self.wiring, "main", None)
        return main.call(fn, *args) if main is not None else fn(*args)

    # ------------------------------------------------------------ Live
    @staticmethod
    def chain(root: str) -> list:
        """Ordner von <root>/exports/share/applications aufwärts bis zum Elternordner von <root>."""
        target = os.path.join(root, "exports", "share", "applications")
        return [target, os.path.dirname(target), os.path.join(root, "exports"), root, os.path.dirname(root)]

    def watch(self) -> None:
        """Beobachter anlegen bzw. auf einen tieferen Ordner umsetzen. Hauptschleife."""
        for root in self.roots():
            self._arm(root)

    def _arm(self, root: str) -> None:
        if self._stopped:
            return
        path = next((p for p in self.chain(root) if os.path.isdir(p)), None)
        old = self._monitors.get(root)
        if old is not None and old[0] == path:
            return
        if old is not None:
            old[1].cancel()
            self._monitors.pop(root, None)
        if path is None:
            return
        try:
            from gi.repository import Gio
            monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:  # noqa: BLE001 (GLib.Error)
            _log.warning("Flatpaks: %s lässt sich nicht beobachten: %s", path, e)
            return
        monitor.connect("changed", lambda *_args, r=root: self._changed(r))
        self._monitors[root] = (path, monitor)
        _log.info("Flatpaks: beobachte %s", path)

    def _changed(self, root: str) -> None:
        """Etwas im beobachteten Ordner änderte sich. Hauptschleife."""
        before = self._monitors.get(root, (None,))[0]
        self._arm(root)
        target = self.chain(root)[0]
        now = self._monitors.get(root, (None,))[0]
        if before == target or now == target:
            self.later()

    def later(self) -> None:
        """Entprellt neu einlesen: Eine Installation schreibt mehrere Dateien nacheinander."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            if self._stopped:
                return
            self._timer = threading.Timer(DEBOUNCE_S, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        if not self._stopped:
            _log.info("Flatpaks haben sich geändert, lese die Bibliothek neu")
            self.rescan()

    def stop(self) -> None:
        self._stopped = True
        self._unwatch_bazaar()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        for _path, monitor in self._monitors.values():
            try:
                monitor.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._monitors.clear()

    # ------------------------------------------------------------ lib.get
    def detail_hook(self, entry: Any, out: dict) -> None:
        """„Deinstallieren“ ans Ende des Optionsmenüs jeder Kachel mit einem Flatpak dahinter (Hauptschleife)."""
        app_id = flatpak_of(entry)
        actions = out.get("actions")
        if not app_id or not getattr(entry, "installed", False) or not isinstance(actions, list):
            return
        if any(isinstance(a, dict) and a.get("id") in ("uninstall", "delete") for a in actions):
            return
        actions.append({"id": "uninstall", "label": "Deinstallieren", "flatpak": app_id})

    # ------------------------------------------------------------ Deinstallieren
    def busy_text(self) -> str | None:
        return TEXT_BUSY_CHECK % self._busy if self._busy else None

    def uninstall_rpc(self, params: dict) -> Any:
        entry_id = str((params or {}).get("id") or "")
        run = getattr(self.wiring, "_bg", None) or self.app.background
        return run(self.uninstall, entry_id)

    def uninstall(self, entry_id: str) -> dict:
        """apps.uninstall im Arbeits-Thread. Wirft nicht."""
        entry = self.main_call(self.app.library.get, entry_id) if entry_id else None
        if entry is None:
            return {"ok": False, "message": TEXT_GONE}
        app_id = flatpak_of(entry)
        if not app_id:
            return {"ok": False, "message": TEXT_NOT_FLATPAK}
        title = str(entry.title or app_id)
        with self._lock:
            if self._busy:
                return {"ok": False, "message": TEXT_BUSY}
            self._busy = title
        try:
            return self._uninstall(entry_id, app_id, title)
        except Exception:
            _log.exception("Deinstallieren von %s", app_id)
            return {"ok": False, "message": TEXT_FAILED % (title, "unerwarteter Fehler")}
        finally:
            with self._lock:
                self._busy = ""

    def _uninstall(self, entry_id: str, app_id: str, title: str) -> dict:
        procs = getattr(self.app, "procs", None)
        if procs is not None and self.main_call(procs.by_entry, entry_id) is not None:
            return {"ok": False, "message": TEXT_RUNNING % title}
        ctx = self.ctx()
        if ctx is None:                         # Bibliothek ohne Quellen: ob es noch installiert ist, weiß hier niemand
            return {"ok": False, "message": TEXT_NO_SOURCES % title}
        where = flatpaks.installed_in(ctx, app_id)
        if not where:
            self.rescan()
            return {"ok": True, "message": TEXT_NOT_INSTALLED % title}
        elsewhere = self.running_elsewhere(app_id)
        if elsewhere is None:                   # flatpak ps gescheitert: im Zweifel nicht (SAFETY 13, E140)
            return {"ok": False, "message": TEXT_UNCHECKED % title}
        if elsewhere:
            return {"ok": False, "message": TEXT_RUNNING_ELSEWHERE % title}
        installation = where[0]                 # die, aus der die Kachel startet (Nutzer vor System)
        argv = uninstall_argv(app_id, installation)
        if argv is None:
            return {"ok": False, "message": TEXT_NOT_FLATPAK}
        _log.info("Deinstalliere %s (%s, %s)", app_id, installation, title)
        code, output = self.run(argv)
        if code is None:
            return {"ok": False, "message": TEXT_NO_FAKE}
        left = flatpaks.installed_in(ctx, app_id)
        self.rescan()
        if installation in left:
            # Zählt, was danach auf der Platte steht, nicht der Rückgabewert allein
            _log.warning("flatpak uninstall %s: %s %s", app_id, code, (output or "").strip()[-400:])
            if code == 124:
                return {"ok": False, "message": TEXT_TIMEOUT % title}
            return {"ok": False, "message": failure_text(title, output or "Es ist danach noch installiert.")}
        return {"ok": True, "message": (TEXT_DONE_OTHER if left else TEXT_DONE) % title}

    def run(self, argv: list) -> tuple:
        """(Rückgabewert, Ausgabe); (None, "") im Prüfmodus ohne Attrappe. Blockierend."""
        env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")
        if paths.test_mode():
            fake = fake_tool()
            if fake is None:
                system.record_write(argv, "E140 deinstallieren (keine Attrappe)")
                return None, ""
            result = system.run_read([fake, *argv[1:]], timeout=60, env=env)
        else:
            result = system.run_write(argv, timeout=UNINSTALL_TIMEOUT_S, env=env, note="E140 deinstallieren",
                                      new_session=True)
        return result.returncode, (result.stdout or "") + (result.stderr or "")

    def running_elsewhere(self, app_id: str) -> bool | None:
        """Läuft die App außerhalb des PC5 Launchers (flatpak ps, nur lesend)? None, wenn sich das nicht prüfen
        ließ (flatpak ps scheitert oder hängt). Im Prüfmodus nie gefragt."""
        if paths.test_mode():
            return False
        from types import SimpleNamespace

        from . import wiring as wiring_mod
        from .launch import flatpak as launch_flatpak
        rows = launch_flatpak.running(SimpleNamespace(run_read=wiring_mod.read3), app_id)
        return None if rows is None else bool(rows)

    # ------------------------------------------------------------ Bazaar
    def reveal(self, app: Any) -> None:
        """Bazaar ohne Fenster: dasselbe Programm noch einmal starten (Hauptschleife, wiring.reveal_again)."""
        self._revealed_at = self._now()          # E141: bis das Fenster kommt, nicht beenden
        from . import wiring as wiring_mod
        wiring_mod.reveal_again(self.wiring, app, "Bazaar", "bazaar-show", _log,
                                spawn=getattr(self.wiring, "apps_spawn", None))

    # ------------------------------------------------------------ App Store beim Schließen beenden (E141)
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def bazaar(self) -> Any:
        procs = getattr(self.app, "procs", None)
        return procs.by_entry(BAZAAR_ID) if procs is not None else None

    def transfer_now(self) -> bool:
        """Läuft gerade eine Flatpak-Übertragung? Nur lesend (flatpaklock); im Prüfmodus nur der Attrappen-Baum."""
        fn = getattr(self.wiring, "apps_transfer", None)            # Tests
        if callable(fn):
            return bool(fn())
        ctx = self.ctx()
        home = getattr(ctx, "home", None) or paths.home()
        try:
            return flatpaklock.transfer_running(self.roots(), home)
        except Exception:
            _log.exception("Flatpak-Übertragung prüfen")
            return False

    def transfer(self) -> bool:
        """Wie transfer_now, aber mit Nachlauf: Eine Übertragung gilt erst QUIET_S nach der letzten Sperre als fertig
        (zwischen zwei Schritten eines Updates kann die Sperre kurz frei sein)."""
        now = self._now()
        if self.transfer_now():
            self._busy_at = now
            return True
        return self._busy_at is not None and now - self._busy_at < QUIET_S

    def close_guard(self, app: Any) -> str | None:
        """Shell.close_guards: Was ein ausdrückliches Schließen des App Stores jetzt abbräche (Hauptschleife)."""
        return TEXT_BAZAAR_BUSY if self.transfer() else None

    def bazaar_busy_text(self) -> str | None:
        """busy_checks (Ein/Aus, Neustart, Übergabe): nur, solange der App Store läuft und etwas überträgt."""
        if self.main_call(self.bazaar) is None:
            return None
        return TEXT_BAZAAR_POWER if self.transfer() else None

    def _has_window(self, app: Any) -> bool:
        try:
            return app.app_id in set(self.app.gs.focusable_apps())
        except Exception:
            return True                         # im Zweifel: nicht beenden

    def bazaar_tick(self) -> None:
        """Einmal nachsehen (Hauptschleife): Fenster zu und nichts mehr zu übertragen -> beenden."""
        app = self.bazaar()
        if app is None or getattr(app, "closing", False) or getattr(app, "exited", False):
            return
        busy = self.transfer()                  # auch mit Fenster: Rückfrage und Ein/Aus wissen es dann
        if self._has_window(app):
            self._gone_since = None
            self._waiting_said = False
            return
        if not getattr(app, "visible", False):
            return                              # kurz nach dem Start: noch nie ein Fenster
        now = self._now()
        if self._gone_since is None:
            self._gone_since = now
        if now - self._gone_since < WINDOW_GONE_S or now - self._revealed_at < REVEAL_GRACE_S:
            return
        if busy:
            if not self._waiting_said:
                self._waiting_said = True
                _log.info("App Store ohne Fenster, eine Übertragung läuft: er läuft weiter, bis sie fertig ist")
            return
        shell = getattr(self.app, "shell", None)
        if shell is None:
            return
        _log.info("App Store hat kein Fenster mehr und überträgt nichts: wird beendet")
        try:
            shell.close_app(app)
        except Exception:
            _log.exception("App Store beenden")

    def on_proc(self, app: Any, event: str) -> None:
        """Process-Manager (Hauptschleife): Solange der App Store läuft, sieht der Teil nach."""
        if getattr(app, "entry_id", "") != BAZAAR_ID:
            return
        if event == "started":
            self._gone_since = None
            self._busy_at = None
            self._waiting_said = False
            self._watch_bazaar()
        elif event == "exited":
            self._gone_since = None
            self._busy_at = None
            self._unwatch_bazaar()

    def _watch_bazaar(self) -> None:
        if self._bazaar_timer or self._stopped:
            return
        every = getattr(self.wiring, "apps_every", None)            # Tests: eigener Takt
        if callable(every):
            self._bazaar_timer = every(POLL_S, self._bazaar_fire) or 1
            return
        if getattr(self.wiring, "main", None) is None:
            return                              # ohne Hauptschleife kein Takt (Tests rufen bazaar_tick selbst)
        from gi.repository import GLib
        self._bazaar_timer = GLib.timeout_add(int(POLL_S * 1000), self._bazaar_fire)

    def _bazaar_fire(self) -> bool:
        try:
            self.bazaar_tick()
        except Exception:
            _log.exception("App Store nachsehen")
        if self._stopped or self.bazaar() is None:
            self._bazaar_timer = 0
            return False
        return True

    def _unwatch_bazaar(self) -> None:
        timer, self._bazaar_timer = self._bazaar_timer, 0
        # GLib vergibt Quellen ab 1; der Platzhalter „or 1“ der Tests kommt nur mit apps_every vor
        if isinstance(timer, int) and timer > 0 and getattr(self.wiring, "apps_every", None) is None:
            try:
                from gi.repository import GLib
                GLib.source_remove(timer)
            except Exception:  # noqa: BLE001 (schon abgelaufen)
                pass

    # ------------------------------------------------------------ Ausgeblendete Apps (E141)
    def hidden_apps(self) -> list[dict]:
        """Einstellungen › Apps › Ausgeblendete Apps: [{id, title, row}] (Arbeits-Thread)."""
        from . import wiring_home
        from .library import describe

        def collect() -> list[dict]:
            out = []
            library = getattr(self.app, "library", None)
            fn = getattr(library, "hidden_by_user", None)
            for e in (fn("app") if callable(fn) else []):
                if e.id in describe.NOT_HIDEABLE or str(e.id).startswith(EMU_PREFIX):
                    continue
                tags = e.tags if isinstance(e.tags, list) else []
                row = "media" if "media-row" in tags else "games" if "games-row" in tags else "apps"
                out.append({"id": e.id, "title": e.title, "row": row})
            return out
        items = self.main_call(collect)
        for tile in wiring_home.read_orders()[wiring_home.HIDDEN]:
            title, row = TILE_TITLES.get(tile, (tile, "apps"))
            items.append({"id": tile, "title": title, "row": row})
        return items

    def show_app(self, entry_id: str) -> str:
        """„Wieder anzeigen“: lib.hide false bzw. die Kachel der Seite wieder einblenden. Liefert den Titel; KeyError,
        wenn es sie nicht (mehr) gibt."""
        from . import wiring_home
        if entry_id in TILE_TITLES:
            wiring_home.set_tile_hidden(entry_id, False)
            changed = getattr(self.wiring, "home_changed", None)
            if callable(changed):
                self.main_call(changed)
            _log.info("Kachel %s wieder angezeigt", entry_id)
            return TILE_TITLES[entry_id][0]

        def show() -> str:
            library = self.app.library
            entry = library.get(entry_id)
            if entry is None or entry.kind != "app":
                raise KeyError(entry_id)
            library.set_hidden(entry_id, False)
            return entry.title
        title = self.main_call(show)
        _log.info("%s wieder angezeigt", entry_id)
        return title

    # ------------------------------------------------------------ Claude Code mitstarten (E141)
    def _config(self) -> Any:
        return getattr(self.wiring, "config", None) or getattr(self.app, "config", None)

    def after_ready(self) -> None:
        library = getattr(self.app, "library", None)
        pending = getattr(library, "first_scan_pending", lambda: None)()
        if pending is not None and hasattr(pending, "add_done_callback"):
            main = getattr(self.wiring, "main", None)
            pending.add_done_callback(lambda _f: main.post(self.claude_autostart) if main is not None
                                      else self.claude_autostart())
        else:
            self.claude_autostart()

    def claude_autostart(self) -> None:
        """Einmal je Sitzung: Schalter an, Kachel da (claude installiert), läuft nicht -> im Hintergrund mit
        „continue“. Hauptschleife."""
        if self._claude_done:
            return
        config = self._config()
        if config is None or config.get("claude.autostart", False) is not True:
            return
        self._launch_claude("startet mit dem PC5 Launcher (letzte Unterhaltung)")

    def start_claude(self) -> str:
        """E143: Einschalten des Schalters startet Claude Code gleich (Arbeits-Thread der Einstellungen). „started“,
        „running“ (läuft schon) oder „missing“ (keine Kachel, keine Shell)."""
        return self.main_call(self._launch_claude, "startet (Schalter eingeschaltet, letzte Unterhaltung)")

    def _launch_claude(self, why: str) -> str:
        """Der eine Start von Claude Code im Hintergrund mit „continue“ (Hauptschleife): „started“, „running“ (läuft
        schon, über die Kachel oder übernommen) oder „missing“ (keine Kachel, keine Shell). Nach „started“ und
        „running“ startet das erste Einlesen es nicht noch einmal."""
        library = getattr(self.app, "library", None)
        entry = library.get(CLAUDE_ID) if library is not None else None
        if entry is None or not getattr(entry, "installed", False):
            _log.info("Claude Code: keine Kachel (claude nicht installiert)")
            return "missing"
        procs = getattr(self.app, "procs", None)
        if procs is not None and procs.by_entry(CLAUDE_ID) is not None:
            self._claude_done = True
            return "running"
        shell = getattr(self.app, "shell", None)
        if shell is None or not hasattr(shell, "launch_background"):
            return "missing"
        self._claude_done = True
        _log.info("Claude Code %s", why)
        work = shell.launch_background(CLAUDE_ID, (), env=dict(CLAUDE_ENV))
        add = getattr(work, "add_done_callback", None)
        if callable(add):
            add(self._claude_started)
        return "started"

    @staticmethod
    def _claude_started(work: Any) -> None:
        try:
            work.result()
        except Exception as e:  # noqa: BLE001
            _log.warning("Claude Code im Hintergrund: %s", getattr(e, "message", e))


def extend(wiring: Any) -> None:
    app = wiring.app
    part = AppsPart(wiring)
    wiring.apps_part = part
    app.rpc.register("apps.uninstall", part.uninstall_rpc)
    hooks = getattr(getattr(app, "library", None), "detail_hooks", None)
    if isinstance(hooks, list):
        hooks.append(part.detail_hook)
    if isinstance(getattr(wiring, "busy_checks", None), list):
        wiring.busy_checks.append(part.busy_text)
    reveal = getattr(getattr(app, "shell", None), "reveal", None)
    if isinstance(reveal, dict):
        reveal[BAZAAR_ID] = part.reveal
    guards = getattr(getattr(app, "shell", None), "close_guards", None)
    if isinstance(guards, dict):
        guards[BAZAAR_ID] = part.close_guard                     # E141
    if isinstance(getattr(wiring, "busy_checks", None), list):
        wiring.busy_checks.append(part.bazaar_busy_text)
    procs = getattr(app, "procs", None)
    if procs is not None and hasattr(procs, "on_change"):
        procs.on_change(part.on_proc)
    add = getattr(getattr(app, "config", None), "add_defaults", None)
    if callable(add):
        add(dict(DEFAULTS))
    if isinstance(getattr(wiring, "after_ready", None), list):
        wiring.after_ready.append(part.after_ready)
    if isinstance(getattr(wiring, "shutdown_hooks", None), list):
        wiring.shutdown_hooks.append(part.stop)
    main = getattr(wiring, "main", None)
    if main is not None:
        main.post(part.watch)
    else:
        part.watch()
    if part.bazaar() is not None:               # nach einem Neustart des Home übernommen: weiter nachsehen
        part._watch_bazaar()
