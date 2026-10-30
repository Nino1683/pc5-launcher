"""Discord im Hintergrund (E139): Verdrahtung, eingehängt über wiring.PARTS (als letzter Teil).

Wunsch 2026-10-29: „Könnte man discord in den pc5 launcher einbinden? Als Nachrichten mit freunden sprachchat etc so wie
auf der ps5 nur als discord im Hintergrund?“ – „Ja aber Discord soll optional bleiben und deaktivierbar“. Was erlaubt
ist und was nicht, steht in couch/discord/__init__.py.

Vorgabe aus (discord.enabled). Aus heißt: kein Hintergrund-Start, kein Symbol im Control Center, kein Mitlesen der
Mitteilungen, keine Verbindung zu Discord, keine Kachel. Wird es ausgeschaltet, beendet der Teil ein Discord, das der
Process-Manager führt (das hat immer der PC5 Launcher gestartet), höflich wie jede App (couch-flatpak-stop), und nimmt
alles andere zurück. Jeder Wechsel eines Schlüssels discord.* läuft über apply() in der Hauptschleife.

  Hintergrund   nach dem Bereit-Signal einmal je Sitzung: an, „Mit der Sitzung im Hintergrund starten“, installiert,
                läuft nicht -> Shell.launch_background(app:discord, --start-minimized): kein Ladebild, kommt nicht nach
                vorn, nie angehalten (spec.freeze False), steht in running (Karte im Control Center)
  Nach vorn     Kachel, Karte und „Discord öffnen“ gehen über Shell.focus_app; hat Discord kein Fenster (ohne Leiste für
                Symbole bleibt es nach --start-minimized unsichtbar), startet reveal() das Flatpak ein zweites Mal ohne
                Zusatz: Electron gibt das an die laufende Instanz weiter, die ihr Fenster zeigt; der zweite Prozess endet
  Tasten        discord.key {what: mute|deafen}: Strg+Umschalt+Alt+F11/F12 per XTEST, sobald die App vorn nach dem
                Control Center weiterläuft (Shell.when_running), danach eine kurze Einblendung ohne Listeneintrag
                (nur der Titel, Wunsch 2026-10-29: kein Satz mehr, ob es geklappt hat);
                Discord soll sie als globale Tastenbelegung sehen (X11, dasselbe Xwayland) bzw. als eigene, wenn es
                selbst vorn ist. Den echten Zustand (stumm oder nicht) kennt der Launcher nicht
  Spielt gerade discord.presence, Application ID: Spiel (kind game) wird sichtbar -> ipc.Presence.set_game, endet -> clear
  Mitteilungen  discord.notify: notes.Monitor liest auf dem Sitzungsbus mit; Nachrichten von Discord gehen als Mitteilung
                (Symbol chat) durch notify.push, also mit Einblenden, „Während Spielen“ und Liste wie jede andere
  sys.state     discord {enabled, installed, running, background, front}; aus: {enabled: false}

Prüfmodus: Nie der echte Sitzungsbus und nie die echten Sockets von Discord. Mitlesen nur über COUCH_TEST_DISCORD_BUS
(Adresse eines eigenen Busses), Sockets nur aus COUCH_TEST_DISCORD_IPC (Ordner) bzw. eingesteckt
(wiring.discord_monitor, wiring.discord_connect); das zweite Starten zum Zeigen nur für Attrappen (AppManager-Regel).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import discord, log, paths
from .discord import ipc, notes
from .rpc import RpcError

_log = log.get("wiring.discord")

REVEAL_WAIT_S = 30.0            # so lange wartet der Faden auf den zweiten Start zum Zeigen (beendet wird er nie)
TEXT_OFF = "Discord ist ausgeschaltet. Einschalten unter Einstellungen › Apps › Discord."
TEXT_MISSING = "Discord ist nicht installiert. Installieren unter Einstellungen › Apps › Discord."
TEXT_NOT_RUNNING = "Discord läuft gerade nicht. Öffne es zuerst."
TEXT_NO_APP = ("Das geht, solange ein Spiel oder eine App vorn ist. Im Home schaltest du in Discord selbst stumm "
               "(Discord öffnen).")
TEXT_SENT = {"mute": "Mikrofon umgeschaltet", "deafen": "Ton umgeschaltet"}


class DiscordPart:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.config = wiring.config
        self.monitor: Any = None
        self.presence: Any = None
        self._game: int | None = None           # App-ID des Spiels, das gerade als „Spielt“ gilt
        self._ready = False
        self._auto_done = False                 # der Hintergrund-Start nach dem Bereit-Signal lief (einmal je Sitzung)
        self._on = discord.enabled(self.config)
        self._toasts = 0

    # ------------------------------------------------------------ Zustand
    def _flatpak_dirs(self) -> list:
        dirs = list(getattr(getattr(self.wiring, "sources", None), "flatpak_dirs", None) or [])
        if dirs or paths.test_mode():
            return dirs
        return ["/var/lib/flatpak", os.path.join(paths.home(), ".local/share/flatpak")]

    def installed(self) -> bool:
        """Liegt das Flatpak in einer Installation? Nur stat (wie library/apps.flatpak_installed ohne Rückfall)."""
        return any(os.path.isdir(os.path.join(root, "app", discord.FLATPAK, "current", "active"))
                   for root in self._flatpak_dirs())

    def running(self) -> Any:
        procs = getattr(self.app, "procs", None)
        return procs.by_entry(discord.ENTRY_ID) if procs is not None else None

    def status(self) -> dict:
        from .setup import catalog
        run = self.running()
        item = catalog.get("discord") or {}
        try:
            front = self.app.state.value("foreground") == discord.ENTRY_ID
        except Exception:  # noqa: BLE001
            front = False
        return {"enabled": discord.enabled(self.config), "installed": self.installed(), "running": run is not None,
                "background": bool(run is not None and getattr(run.spec, "background", False)), "front": front,
                "notes": getattr(self.monitor, "state", "off") if self.monitor is not None else "off",
                "size_mb": item.get("size_mb")}

    def publish(self) -> None:
        """sys.state.discord (Hauptschleife). Aus: nur {enabled: false}, das Symbol im Control Center fehlt dann."""
        if not discord.enabled(self.config):
            value: dict = {"enabled": False}
        else:
            st = self.status()
            value = {k: st[k] for k in ("enabled", "installed", "running", "background", "front")}
            value["keys"] = dict(discord.KEY_LABELS)
        try:
            self.app.state.update(discord=value)
        except Exception:  # noqa: BLE001
            _log.exception("sys.state.discord")

    # ------------------------------------------------------------ Ein und aus
    def apply(self) -> None:
        """Alles nach den Einstellungen ausrichten (Hauptschleife; kehrt schnell zurück)."""
        on = discord.enabled(self.config)
        was, self._on = self._on, on
        shell = getattr(self.app, "shell", None)
        if shell is not None and hasattr(shell, "reveal"):
            if on:
                shell.reveal[discord.ENTRY_ID] = self.reveal
            else:
                shell.reveal.pop(discord.ENTRY_ID, None)
        self._apply_notes(on and discord.flag(self.config, "discord.notify"))
        self._apply_presence(on and discord.flag(self.config, "discord.presence") and bool(discord.app_id_of(self.config)))
        if was and not on:
            self._close_own()
        if was != on:
            self._rescan(start=on)
        self.publish()

    def _apply_notes(self, wanted: bool) -> None:
        if wanted and self.monitor is None:
            monitor = self._make_monitor()
            if monitor is None:
                return
            self.monitor = monitor
            monitor.start()
        elif not wanted and self.monitor is not None:
            monitor, self.monitor = self.monitor, None
            monitor.stop()

    def _make_monitor(self) -> Any:
        factory = getattr(self.wiring, "discord_monitor", None)       # Tests: Attrappe
        if callable(factory):
            return factory(self._note)
        address = None
        if paths.test_mode():
            address = os.environ.get("COUCH_TEST_DISCORD_BUS") or None
            if not address:
                return None                     # Prüfmodus: nie der echte Sitzungsbus
        return notes.Monitor(self._note, address=address)

    def _apply_presence(self, wanted: bool) -> None:
        if wanted and self.presence is None:
            self.presence = ipc.Presence(lambda: discord.app_id_of(self.config), self._ipc_paths,
                                         connect=getattr(self.wiring, "discord_connect", None))
            self._presence_game()
        elif not wanted and self.presence is not None:
            presence, self.presence = self.presence, None
            presence.clear()
            presence.close()
            self._game = None

    def _ipc_paths(self) -> list:
        if paths.test_mode():
            folder = os.environ.get("COUCH_TEST_DISCORD_IPC")
            return [os.path.join(folder, "discord-ipc-%d" % n) for n in range(10)] if folder else []
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
        return ipc.socket_paths(runtime)

    def _close_own(self) -> None:
        """Aus geschaltet: ein laufendes Discord (immer vom Launcher gestartet) höflich beenden."""
        run = self.running()
        shell = getattr(self.app, "shell", None)
        if run is None or shell is None:
            return
        _log.info("Discord ausgeschaltet: wird beendet")
        try:
            shell.close_app(run)
        except Exception:  # noqa: BLE001
            _log.exception("Discord beenden")

    def _rescan(self, start: bool) -> None:
        """Die Kachel erscheint bzw. verschwindet erst mit dem nächsten Einlesen (library/apps.SWITCHED)."""
        library = getattr(self.app, "library", None)
        fn = getattr(library, "rescan", None)
        if not callable(fn):
            return
        try:
            fut = fn()
        except Exception:  # noqa: BLE001
            _log.exception("Bibliothek neu einlesen")
            return
        if start and self._ready and hasattr(fut, "add_done_callback"):
            fut.add_done_callback(lambda _f: self.wiring.main.post(self.autostart, True))

    # ------------------------------------------------------------ Hintergrund-Start
    def after_ready(self) -> None:
        self._ready = True
        self.apply()
        library = getattr(self.app, "library", None)
        pending = getattr(library, "first_scan_pending", lambda: None)()
        if pending is not None and hasattr(pending, "add_done_callback"):
            pending.add_done_callback(lambda _f: self.wiring.main.post(self.autostart))
        else:
            self.autostart()

    def autostart(self, switched: bool = False) -> None:
        """Einmal je Sitzung (bzw. nach dem Einschalten): an, Hintergrund-Start gewollt, installiert, läuft nicht."""
        if self._auto_done and not switched:
            return
        if not discord.flag(self.config, "discord.autostart") or self.running() is not None or not self.installed():
            return
        shell = getattr(self.app, "shell", None)
        library = getattr(self.app, "library", None)
        if shell is None or not hasattr(shell, "launch_background") or library is None \
                or library.get(discord.ENTRY_ID) is None:
            return
        self._auto_done = True
        work = shell.launch_background(discord.ENTRY_ID, discord.BACKGROUND_ARGS)
        work.add_done_callback(lambda f: self.wiring.main.post(self._started, f))

    def _started(self, work: Any) -> None:
        try:
            work.result()
        except Exception as e:  # noqa: BLE001
            _log.warning("Discord im Hintergrund: %s", getattr(e, "message", e))
        self.publish()

    # ------------------------------------------------------------ nach vorn
    def open(self) -> None:
        """„Discord öffnen“ (Einstellungen, Control Center): nach vorn holen bzw. normal starten. Aus jedem Thread."""
        def run() -> None:
            if not discord.enabled(self.config):
                raise RpcError(TEXT_OFF, "DiscordOff")
            current = self.running()
            if current is not None:
                self.app.shell.focus_app(current)       # ohne Fenster zeigt reveal() es zuerst
                return
            if not self.installed():
                raise RpcError(TEXT_MISSING, "NotInstalled")
            self.app.shell.launch(discord.ENTRY_ID)     # wie die Kachel: Ladebild, dann vorn
        self.wiring.main.call(run)

    def reveal(self, app: Any) -> None:
        """Discord ohne Fenster: das Flatpak noch einmal starten (ohne --start-minimized). Hauptschleife."""
        argv = [a for a in (getattr(app.spec, "argv", None) or []) if a not in discord.BACKGROUND_ARGS]
        if not argv:
            return
        try:
            display = self.app.gs.game_display()
        except Exception as e:  # noqa: BLE001
            _log.warning("Discord zeigen: kein Display (%s)", e)
            return
        from .proc import manager
        env = manager.build_env(getattr(self.app, "session_env", {}) or {}, app.spec, display, app.app_id)
        exe = manager.find_executable(argv[0], env, app.spec.cwd)
        if exe is None or (paths.test_mode() and not manager.test_mode_allows(exe)):
            _log.info("Discord zeigen: %s wird nicht gestartet", argv[0])
            return
        spawn = getattr(self.wiring, "discord_spawn", None) or self._spawn
        threading.Thread(target=spawn, args=([exe, *argv[1:]], env, app.spec.cwd), name="discord-show",
                         daemon=True).start()

    def _spawn(self, argv: list, env: dict, cwd: Any) -> None:
        import subprocess
        try:
            proc = self.wiring_popen(argv, env=env, cwd=cwd or None, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            _log.warning("Discord zeigen: %s", e)
            return
        try:
            proc.wait(timeout=REVEAL_WAIT_S)
        except subprocess.TimeoutExpired:
            # Nicht beenden: Ist der erste Discord inzwischen weg, ist dieser Start das echte Discord (samt Sprachchat)
            _log.warning("Discord zeigen: der zweite Start endet nicht von selbst, er läuft weiter")

    def wiring_popen(self, argv: list, **kwargs: Any) -> Any:
        from . import wiring as wiring_mod
        return wiring_mod._popen_bound(argv, **kwargs)

    # ------------------------------------------------------------ Tasten
    def key(self, what: str) -> dict:
        """discord.key: Tastenbelegung an Discord, sobald die App vorn weiterläuft. Hauptschleife."""
        combo = discord.KEYS.get(what)
        if combo is None:
            raise RpcError("Ungültiger Aufruf.", "BadRequest")
        if not discord.enabled(self.config):
            raise RpcError(TEXT_OFF, "DiscordOff")
        if self.running() is None:
            raise RpcError(TEXT_NOT_RUNNING, "NotRunning")
        shell = self.app.shell
        inp = self.app.input

        def press(_front: Any) -> None:
            inp.press_combo(combo)
            _log.info("Discord: %s gesendet (%s)", what, discord.KEY_LABELS[what])
            self._feedback(discord.TITLE, TEXT_SENT[what])
        if not shell.when_running(press):
            raise RpcError(TEXT_NO_APP, "NoApp")
        return {"ok": True}

    def _feedback(self, title: str, body: str) -> None:
        """Kurze Einblendung nach dem Senden, ohne Eintrag in der Liste der Mitteilungen (das wäre nur Rauschen); wie
        notify.push über das Ereignis toast und shell.on_toast (das Overlay steht dafür passiv). Ohne Einblendungen
        (notify.popups) nichts."""
        notify = getattr(self.app, "notify", None)
        if notify is None or self.config.get("notify.popups", True) is False:
            return
        self._toasts += 1
        item = {"id": "discord-%d" % self._toasts, "title": title, "body": body, "icon": "chat", "time": time.time()}
        try:
            self.app.rpc.emit("toast", {**item, "seconds": notify.seconds()})
            if callable(getattr(notify, "on_toast", None)):
                notify.on_toast(item)
        except Exception:  # noqa: BLE001
            _log.exception("Einblendung nach der Discord-Taste")

    # ------------------------------------------------------------ Spielt gerade
    def on_proc(self, app: Any, event: str) -> None:
        """Process-Manager (Hauptschleife): Spiel sichtbar -> zeigen, Spiel zu Ende -> löschen (oder das nächste)."""
        if getattr(app, "entry_id", "") == discord.ENTRY_ID and event in ("started", "exited"):
            self.publish()
        if getattr(getattr(app, "spec", None), "kind", "") != "game":
            return
        if event == "visible" or (event == "exited" and app.app_id == self._game):
            self._presence_game()

    def _presence_game(self) -> None:
        if self.presence is None:
            return
        procs = getattr(self.app, "procs", None)
        games = [a for a in (procs.running() if procs is not None else [])
                 if getattr(a.spec, "kind", "") == "game" and getattr(a, "visible", False) and not getattr(a, "exited", False)]
        game = games[-1] if games else None
        if game is None:
            if self._game is not None:
                self._game = None
                self.presence.clear()
            return
        if game.app_id == self._game:
            return
        self._game = game.app_id
        entry = self.app.library.get(game.entry_id) if getattr(self.app, "library", None) is not None else None
        title = (entry.title if entry is not None else "") or getattr(game.spec, "title", "") or game.entry_id
        self.presence.set_game(title, getattr(game, "started", None))

    def _app_id_changed(self) -> None:
        """Eine neue Application ID gilt beim nächsten Wechsel: das laufende Spiel einmal neu melden."""
        if self.presence is not None and self._game is not None:
            self._game = None
            self._presence_game()

    # ------------------------------------------------------------ Mitteilungen
    def _note(self, note: dict, how: str) -> None:
        """Aus dem Thread des Beobachters: als Mitteilung in die Hauptschleife."""
        def push() -> None:
            if not discord.flag(self.config, "discord.notify"):
                return
            try:
                front = self.app.state.value("foreground") == discord.ENTRY_ID
            except Exception:  # noqa: BLE001
                front = False
            # Discord vorn zeigt die Nachricht selbst; zu viele auf einmal: nur in die Liste
            self.app.notify.push(note.get("title") or discord.TITLE, note.get("body") or "", icon="chat",
                                 silent=front or how != "toast")
        self.wiring.main.post(push)

    # ------------------------------------------------------------ Ende
    def stop(self) -> None:
        if self.monitor is not None:
            monitor, self.monitor = self.monitor, None
            monitor.stop()
        if self.presence is not None:
            presence, self.presence = self.presence, None
            presence.close()


def extend(wiring: Any) -> None:
    app = wiring.app
    add = getattr(app.config, "add_defaults", None)
    if callable(add):
        add(dict(discord.DEFAULTS))
    part = DiscordPart(wiring)
    wiring.discord_part = part              # Einstellungen › Apps › Discord (settings/apps.py seit E143, deps.extra["late"])

    def key(params: dict) -> dict:
        return part.key(str((params or {}).get("what") or ""))

    def open_(params: dict) -> dict:
        part.open()
        return {"ok": True}

    rpc = app.rpc
    rpc.register("discord.key", key)
    rpc.register("discord.open", open_)
    rpc.register("discord.state", lambda params: wiring._bg(part.status))

    def changed(key_name: str, _value: Any) -> None:
        if key_name == "discord.app_id":
            wiring.main.post(lambda: (part.apply(), part._app_id_changed()))
        else:
            wiring.main.post(part.apply)
    on_change = getattr(wiring.config, "on_change", None)
    if callable(on_change):
        on_change("discord.", changed)
    procs = getattr(app, "procs", None)
    if procs is not None and hasattr(procs, "on_change"):
        procs.on_change(part.on_proc)
    library = getattr(app, "library", None)
    if library is not None and hasattr(library, "on_change"):
        library.on_change(lambda: wiring.main.post(part.publish))
    wiring.after_ready.append(part.after_ready)
    wiring.shutdown_hooks.append(part.stop)
    part.publish()
