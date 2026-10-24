"""Ein/Aus-Menü: Ausschalten, Neustart, Ruhemodus (logind, ohne Rückfrage nach einem Kennwort),
Wechsel zum Desktop, Übergabe an Steam, Sitzung neu starten.

Vor jedem Schritt außer dem Ruhemodus werden laufende Apps geschlossen; welche Aktion das tut,
steht hier (ENDS_APPS) und geht mit power.menu an die Oberfläche.
Im Ruhemodus bleibt alles, wie es ist: Das Spiel schläft mit dem Rechner und läuft danach weiter.
Alles, was den Rechner verändert, geht durch system.run_write und wird im Prüfmodus nur protokolliert.

Rückfrage (E31): Vor jeder Aktion werden die busy_checks der Verdrahtung befragt (laufende
Installation, Prefix-Kopie …). Meldet einer etwas oder sind Spiele offen, wird einmal gefragt,
Vorgabe Abbrechen; der Ruhemodus fragt nur, wenn ein busy_check etwas meldet (er würde es
unterbrechen). Wer fragt, sagt der Aufrufer mit confirmed:
  True    der Aufrufer hat schon gefragt (power.menu liefert ihm je Eintrag den Text): nicht fragen
  False   der Kern fragt über die Overlay-Ansicht "confirm"
  fehlt   Aufrufer von vor dieser Regel. Die Seite fragt nach E40 selbst, wenn eine Aktion Apps
          beendet und sie etwas davon sieht (running, jobs, copying); nur dann fragt der Kern
          nicht noch einmal. Alles andere (Sätze weiterer busy_checks, Ruhemodus) fragt er.

Einschaltknopf am Gehäuse (E38 in der Fassung vom 2026-10-14): Lebt das Home, reicht der Supervisor
jeden Druck als control "power-key" herein (key()) und schaltet selbst nie aus.
  erster Druck    Ein/Aus-Menü im Control Center (byPowerKey: die Seite blendet den Hinweis ein)
  zweiter Druck   binnen COUCH_POWER_CONFIRM (5 s) oder solange das vom Knopf geöffnete Menü offen
                  ist: Rückfrage KEY_TITLE über die Overlay-Ansicht "confirm", mit den Sätzen zu
                  offenen Spielen und laufenden Installationen, Vorgabe Abbrechen, ohne Antwort
                  nach KEY_QUESTION_S ebenfalls Abbrechen
  dritter Druck   bei offener Rückfrage: bestätigt sie (der Weg ohne Pad, z. B. Akku leer)
Danach läuft power.do "off" wie gehabt (Apps schließen, logind). Ohne lebendes Home zählt der
Supervisor selbst: erst ein zweiter Druck binnen 5 s schaltet aus.

Ruhemodus-Timer (E107): letzter Eintrag "sleep-timer", nur wenn wiring_sleeptimer seinen Dienst
an sleep_timer gehängt hat. Er führt nichts aus (power.do lehnt ab, die Seite stellt ihn über
power.timer), beendet keine App und braucht keine Rückfrage; gesperrt, wenn logind kein Suspend kann.
"""
from __future__ import annotations

import math
import os
import shutil
import time
from concurrent.futures import Future
from typing import Any, Callable

from . import intro, log, paths, sinknote, system
from .rpc import RpcError

_log = log.get("power")

_LOGIND = ["busctl", "--system", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
           "org.freedesktop.login1.Manager"]
# letzter Parameter "interactive" = false: nie nach einem Kennwort fragen
_LOGIND_CALL = {"off": "PowerOff", "reboot": "Reboot", "suspend": "Suspend"}
_LOGIND_CAN = {"off": "CanPowerOff", "reboot": "CanReboot", "suspend": "CanSuspend"}

ITEMS = [
    ("suspend", "Ruhemodus", "Aufwecken mit dem Einschaltknopf am PC"),
    ("off", "Ausschalten", ""),
    ("reboot", "Neu starten", ""),
    ("restart-session", "PC5 Launcher neu starten", "Lädt die Oberfläche neu, der Rechner bleibt an"),
    ("steam", "Steam Big Picture öffnen", "Zurück zur Konsole: PS + Create + Options 5 Sekunden halten"),
    ("desktop", "Zum Desktop wechseln", ""),
    # E107: immer der letzte Eintrag (Fotos und Szenarien zählen die Einträge davor ab)
    ("sleep-timer", "Ruhemodus-Timer", ""),
]
ACTIONS = tuple(i[0] for i in ITEMS)
# E107: kein Ausführen, sondern eine Auswahl (power.timer, wiring_sleeptimer)
TIMER_ID = "sleep-timer"
TIMER_CHOICES = (0, 15, 30, 60, 90, 120)            # Minuten, 0 = aus
TIMER_OFF = "Aus"
TIMER_LEFT = "Noch %d Min."
TIMER_NO_SUSPEND = "Der Rechner erlaubt keinen Ruhemodus."
TIMER_NOT_HERE = "Den Timer stellst du im Menü ein."
# Die eine Stelle, die festlegt, welche Aktion laufende Apps beendet (und deshalb nachfragt)
ENDS_APPS = frozenset(ACTIONS) - {"suspend", TIMER_ID}
# Danach gilt die Sitzung nicht mehr: vorher die Ton-Ausgabe von vor der Sitzung wiederherstellen.
# Der Supervisor kommt beim Herunterfahren nicht mehr dazu (er wird hart beendet).
RESTORES_SINK = frozenset({"off", "reboot", "desktop"})
FAILED = {"off": "Ausschalten hat nicht geklappt.", "reboot": "Der Neustart hat nicht geklappt.",
          "suspend": "Der Ruhemodus hat nicht geklappt.",
          "desktop": "Der Wechsel zum Desktop hat nicht geklappt.",
          "steam": "Die Übergabe an Steam hat nicht geklappt.",
          "restart-session": "Der Neustart des PC5 Launchers hat nicht geklappt."}
LABELS = {i[0]: i[1] for i in ITEMS}
# Ruhemodus: Er beendet nichts, aber was gerade arbeitet, bleibt mit dem Rechner stehen
SUSPEND_NOTE = "Im Ruhemodus bleibt das stehen und geht erst nach dem Aufwecken weiter."

# Einschaltknopf (E38): Rückfrage beim zweiten Druck
KEY_TITLE = "Konsole ausschalten?"
KEY_HINT = "Einschaltknopf drücken = Ausschalten"
KEY_NOTHING = "Die Konsole wird heruntergefahren."
KEY_WINDOW_S = 5.0                  # Vorgabe von COUCH_POWER_CONFIRM (wie im Supervisor)
KEY_QUESTION_S = 20.0               # so lange steht die Rückfrage, danach gilt Abbrechen


def timer_note(info: dict | None, now: float | None = None) -> str:
    """„Aus“ bzw. „Noch 42 Min.“ (aufgerundet, mindestens 1) zum Stand {until, minutes} (E107)."""
    until = (info or {}).get("until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return TIMER_OFF
    left = until - (time.time() if now is None else now)
    return TIMER_LEFT % max(1, math.ceil(left / 60.0))


def key_window() -> float:
    """Zeitfenster für den zweiten Druck: COUCH_POWER_CONFIRM, dieselbe Variable wie im Supervisor."""
    try:
        value = float(os.environ.get("COUCH_POWER_CONFIRM", ""))
    except ValueError:
        return KEY_WINDOW_S
    return value if value >= 0.1 else KEY_WINDOW_S


class Power:
    def __init__(self, app: Any, read: Callable = system.run_read, write: Callable = system.run_write,
                 background: Callable | None = None):
        self.app = app
        self._read = read
        self._write = write
        self._background = background or getattr(app, "background", None) or system.background
        # Ausschalten, Neustart und die logind-Abfrage dafür warten nie hinter anderen Aufträgen
        # des gemeinsamen Pools (die an einer schlafenden Platte hängen könnten): eigener Thread.
        # Tests mit eigenem background behalten ihren (synchronen) Ablauf.
        self._own = system.dedicated if self._background is system.background else self._background
        self._can: dict[str, bool] | None = None
        self.busy: str | None = None
        self._key_at: float | None = None       # letzter Druck auf den Einschaltknopf (Uhr der Shell)
        self._key_question: Future | None = None    # offene Rückfrage des Knopfs (E38)
        # Von der Verdrahtung gesetzt: fn() nach einer Aktion, die die Ton-Ausgabe zurückgestellt
        # hat, wenn die Sitzung doch weiterläuft (Ausschalten gescheitert): wieder zum Fernseher
        self.on_stay: Callable[[], None] | None = None
        # E107: Dienst des Ruhemodus-Timers (wiring_sleeptimer): info() -> {until, minutes} | None,
        # available() -> bool. Ohne ihn fehlt der Eintrag im Menü.
        self.sleep_timer: Any = None

    # ------------------------------------------------------------ Menü
    def _probe(self) -> dict[str, bool]:
        """Was logind erlaubt (liest nur). Läuft im Arbeits-Thread, einmal je Sitzung."""
        can = {}
        for action, method in _LOGIND_CAN.items():
            r = self._read(_LOGIND + [method], timeout=3.0)
            # Antwort z. B.: s "yes"   ("challenge" hieße Kennwort-Abfrage -> für uns nein)
            can[action] = r.returncode == 0 and '"yes"' in r.stdout
        can["desktop"] = shutil.which("steamosctl") is not None
        return can

    def _menu(self, can: dict[str, bool]) -> dict:
        shell = self.app.shell
        running = [a for a in self.app.procs.running()]
        closing, busy = self._interruptions()
        # confirm: die Rückfrage, die zu diesem Eintrag jetzt nötig wäre ({title, text, ok}
        # oder None). Fragt die Seite damit selbst, ruft sie power.do mit confirmed: true.
        items = [{"id": i, "label": label, "enabled": can.get(i, True), "note": note,
                  "endsApps": i in ENDS_APPS, "confirm": self._question(i, closing, busy)}
                 for i, label, note in ITEMS if i != TIMER_ID]
        timer = self._timer_item(can)
        if timer is not None:
            items.append(timer)
        # running/jobs/copying: was eine Aktion mit endsApps beenden würde (E31);
        # busy: die Sätze der busy_checks, wie sie auch in confirm.text stehen
        out: dict = {"items": items, "running": len(running),
                     "jobs": int(getattr(shell, "jobs_running", lambda: 0)()),
                     "copying": int(getattr(shell, "adopting_count", lambda: 0)()),
                     "busy": list(busy)}
        if running:
            out["warning"] = "Laufende Spiele und Apps werden vorher geschlossen."
        return out

    # ------------------------------------------------------------ Ruhemodus-Timer (E107)
    def can_do(self, action: str) -> bool | None:
        """Was logind laut letzter Abfrage erlaubt; None, solange noch nicht gefragt wurde."""
        if self._can is None:
            return None
        return bool(self._can.get(action, True))

    def _timer_item(self, can: dict[str, bool]) -> dict | None:
        """Eintrag „Ruhemodus-Timer“ mit Stand (timer) und Auswahl (choices); None ohne Dienst."""
        svc = self.sleep_timer
        if svc is None:
            return None
        try:
            info = svc.info()
            available = bool(svc.available())
        except Exception:
            _log.exception("Ruhemodus-Timer lesen")
            return None
        suspend_ok = can.get("suspend", True)
        if not available:
            note = "Nicht verfügbar"
        elif not suspend_ok and info is None:
            note = TIMER_NO_SUSPEND
        else:
            wall = getattr(svc, "wall", None)          # dieselbe Uhr wie der Dienst (Tests: Handuhr)
            note = timer_note(info, wall() if callable(wall) else None)
        # Läuft schon ein Timer (etwa aus der Ablage), bleibt der Eintrag offen: ausschalten geht immer
        enabled = available and (suspend_ok or info is not None)
        return {"id": TIMER_ID, "label": LABELS[TIMER_ID], "enabled": bool(enabled),
                "note": note, "endsApps": False, "confirm": None, "timer": info,
                "choices": list(TIMER_CHOICES)}

    # ------------------------------------------------------------ Rückfrage (E31)
    def _interruptions(self) -> tuple[list[str], list[str]]:
        """(Sätze zu offenen Apps, Sätze der busy_checks); befragt jeden busy_check."""
        fn = getattr(self.app.shell, "interruptions", None)
        if fn is None:
            return [], []
        try:
            closing, busy = fn()
            return list(closing), list(busy)
        except Exception:
            _log.exception("busy_checks")
            return [], []

    def _question(self, action: str, closing: list[str], busy: list[str]) -> dict | None:
        """Die Rückfrage vor action oder None, wenn nichts dagegen spricht."""
        label = LABELS.get(action, action)
        if action in ENDS_APPS:
            lines = list(closing) + list(busy)
        else:
            # Ruhemodus: Spiele schlafen einfach mit; gefragt wird nur für das, was arbeitet
            lines = list(busy) + ([SUSPEND_NOTE] if busy else [])
        if not lines:
            return None
        return {"title": "%s?" % label, "text": "\n".join(lines), "ok": label}

    def question(self, action: str) -> dict | None:
        """Öffentlich für andere Aufrufer (Einstellungen): befragt die busy_checks neu."""
        return self._question(action, *self._interruptions())

    def _page_asks(self, action: str) -> bool:
        """Fragt eine Seite nach E40 schon selbst? Sie tut es, wenn die Aktion Apps beendet
        und sie etwas sieht, das dabei endet: laufende Apps, Installationen laut sys.state,
        Prefix-Kopien (power.menu: running, jobs, copying)."""
        if action not in ENDS_APPS:
            return False
        shell = self.app.shell
        return bool(self.app.procs.running()
                    or getattr(shell, "jobs_running", lambda: 0)()
                    or getattr(shell, "adopting_count", lambda: 0)())

    def menu(self) -> Any:
        if self._can is not None:
            return self._menu(self._can)
        fut: Future = Future()
        work = self._own(self._probe)

        def done(f: Future) -> None:
            try:
                self._can = f.result()
            except Exception:
                _log.exception("logind-Abfrage")
                self._can = {}
            fut.set_result(self._menu(self._can))
        work.add_done_callback(lambda f: self.app.shell.sched.soon(lambda: done(f)))
        return fut

    def key(self) -> None:
        """Einschaltknopf am Gehäuse (über den Supervisor, E38): erster Druck Ein/Aus-Menü,
        zweiter Druck Rückfrage, dritter Druck bestätigt sie (siehe Kopf der Datei)."""
        shell = self.app.shell
        now = shell.sched.now()
        asked = self._key_question
        if asked is not None and not asked.done():
            _log.info("Einschaltknopf: Rückfrage bestätigt (dritter Druck)")
            shell.confirm_result(getattr(asked, "cid", ""), "yes")
            return
        if self.busy or getattr(shell, "ending", False):
            # Eine andere Rückfrage steht oder es wird schon ausgeschaltet: Der Knopf bestätigt
            # nichts Fremdes und schließt auch nichts (das Menü würde die Frage verwerfen)
            _log.info("Einschaltknopf: übergangen, es läuft schon %s", self.busy or "das Ende")
            return
        second = ((self._key_at is not None and now - self._key_at <= key_window())
                  or shell.key_menu_open())
        self._key_at = now
        if second:
            self._ask_key()
            return
        shell.open_control_center(section="power", by_power_key=True)

    def _ask_key(self) -> None:
        """Zweiter Druck: „Konsole ausschalten?“ mit allem, was dabei endet (dieselben Sätze wie
        power.menu.items[off].confirm), Vorgabe Abbrechen."""
        question = self.question("off")
        lines = question["text"].split("\n") if question else [KEY_NOTHING]
        self.busy = "off"                           # solange die Frage steht, keine zweite Aktion
        _log.info("Einschaltknopf: Rückfrage (%s)", " | ".join(lines))
        try:
            asked = self.app.shell.confirm(KEY_TITLE, "\n".join(lines + [KEY_HINT]),
                                           [{"value": "yes", "label": LABELS["off"]},
                                            {"value": "no", "label": "Abbrechen"}],
                                           default="no", timeout=KEY_QUESTION_S, extra={"powerKey": True})
        except Exception:
            self.busy = None
            raise
        self._key_question = asked

        def answered(done: Future) -> None:
            self.busy = None
            self._key_question = None
            try:
                if done.result() != "yes":
                    _log.info("Einschaltknopf: Ausschalten abgebrochen")
                    return
                work = self.do("off", confirmed=True)
            except Exception:
                _log.exception("Einschaltknopf: Ausschalten")
                return
            work.add_done_callback(self.tell_failed)
        asked.add_done_callback(answered)

    def tell_failed(self, work: Future) -> None:
        """Mitteilung, wenn ein Auftrag ohne Seite dahinter scheitert (Einschaltknopf,
        Einstellungen): Sonst blieb der PC still an, die Spiele aber schon geschlossen."""
        if work.cancelled() or work.exception() is None:
            return
        exc = work.exception()
        _log.error("Ein/Aus gescheitert: %s", exc)
        notify = getattr(self.app, "notify", None)
        if notify is not None:
            notify.push(getattr(exc, "message", "") or "Das hat nicht geklappt.", "Versuch es bitte noch einmal.",
                        icon="warn", important=True)

    # ------------------------------------------------------------ Ausführen
    def do(self, action: str, confirmed: bool | None = None) -> Future:
        """Führt action aus; vorher die Rückfrage nach E31 (siehe Kopf der Datei). Lehnt der
        Nutzer ab, liefert das Future {ok: False, cancelled: True} und nichts ist passiert."""
        if action == TIMER_ID:
            raise RpcError(TIMER_NOT_HERE, "BadAction")     # E107: Auswahl über power.timer
        if action not in ACTIONS:
            raise RpcError("Diese Aktion gibt es nicht.", "BadAction")
        if self.busy:
            raise RpcError("Es läuft schon ein Vorgang.", "Busy")
        question = self.question(action)            # befragt die busy_checks in jedem Fall
        if question is None or confirmed is True or (confirmed is None and self._page_asks(action)):
            return self._run(action)
        return self._ask(action, question)

    def _ask(self, action: str, question: dict) -> Future:
        fut: Future = Future()
        self.busy = action                          # solange die Frage steht, keine zweite Aktion
        _log.info("power.do %s: Rückfrage (%s)", action, question["text"].replace("\n", " | "))
        asked = self.app.shell.confirm(question["title"], question["text"],
                                       [{"value": "yes", "label": question["ok"]},
                                        {"value": "no", "label": "Abbrechen"}], default="no")

        def answered(done: Future) -> None:
            self.busy = None
            try:
                if done.result() != "yes":
                    fut.set_result({"ok": False, "cancelled": True})
                    return
                inner = self._run(action)
            except BaseException as e:
                fut.set_exception(e if isinstance(e, RpcError) else RpcError(
                    FAILED.get(action, "Das hat nicht geklappt."), "PowerFailed"))
                return
            inner.add_done_callback(lambda f: fut.set_exception(f.exception())
                                    if f.exception() else fut.set_result(f.result()))
        asked.add_done_callback(answered)
        return fut

    def _run(self, action: str) -> Future:
        if self.busy:
            raise RpcError("Es läuft schon ein Vorgang.", "Busy")
        self.busy = action
        fut: Future = Future()
        shell = self.app.shell
        _log.info("power.do %s", action)

        def failed(e: BaseException) -> None:
            # Nie hängen bleiben: Sonst meldete jede weitere Aktion „Es läuft schon ein Vorgang“
            _log.error("power.%s abgebrochen: %s", action, e)
            self._release()
            if not fut.done():
                fut.set_exception(RpcError(FAILED.get(action, "Das hat nicht geklappt."), "PowerFailed"))

        def guarded() -> None:
            try:
                self._execute(action, fut)
            except Exception as e:
                failed(e)
        try:
            if action == "steam":
                shell.handover({"uri": None, "return": "manual"},
                               done=lambda: fut.set_result({"ok": True}), failed=failed)
            elif action in ENDS_APPS:
                shell.after_shutdown(guarded)
            else:
                guarded()                           # Ruhemodus: nichts schließen
        except Exception as e:
            failed(e)
        return fut

    def _mark_deliberate(self) -> None:
        """Marke für den Supervisor: gewolltes Ende, zählt nicht als Kurzsitzung."""
        try:
            system.write_atomic(paths.deliberate_exit_file(), "%d\n" % time.time())
        except OSError as e:
            _log.warning("deliberate-exit nicht schreibbar: %s", e)

    def _release(self) -> None:
        was, self.busy = self.busy, None
        self.app.shell.ending = False
        if was in RESTORES_SINK and self.on_stay is not None:
            try:
                self.on_stay()
            except Exception:
                _log.exception("on_stay")

    def _system_call(self, action: str, argv: list) -> Any:
        """Im Arbeits-Thread: erst die Ton-Ausgabe von vor der Sitzung, dann der Befehl selbst."""
        if action in RESTORES_SINK:
            try:
                sinknote.restore(self._write)
            except Exception:
                _log.exception("Tonausgang zurückstellen")     # Beiwerk: hält das Ausschalten nicht auf
        return self._write(argv, timeout=30.0, note="power.%s" % action)

    def _execute(self, action: str, fut: Future) -> None:
        if action == "restart-session":
            self._mark_deliberate()
            intro.forget()                      # E92: nach dem Neustart wieder die Startanimation
            fut.set_result({"ok": True})
            self.app.quit(0)
            return
        if action in _LOGIND_CALL:
            argv = _LOGIND + [_LOGIND_CALL[action], "b", "false"]
            if action != "suspend":
                self._mark_deliberate()
        else:
            argv = ["steamosctl", "switch-to-desktop-mode"]
        work = self._own(self._system_call, action, argv)
        work.add_done_callback(lambda f: self.app.shell.sched.soon(lambda: self._written(action, f, fut)))

    def _written(self, action: str, work: Future, fut: Future) -> None:
        try:
            r = work.result()
            ok = r.returncode == 0
            detail = (r.stderr or "").strip()
        except Exception as e:
            ok, detail = False, str(e)
        if not ok:
            _log.error("power.%s fehlgeschlagen: %s", action, detail)
            self._release()
            fut.set_exception(RpcError(FAILED[action], "PowerFailed"))
            return
        fut.set_result({"ok": True})
        if action == "desktop":
            self._mark_deliberate()
            self.app.quit(0)
        elif action == "suspend" or paths.test_mode():
            self._release()                 # nach dem Aufwachen (bzw. im Prüfmodus) geht es weiter
        else:
            # Der Rechner fährt herunter. Leben wir in einer Minute noch, ist etwas schiefgegangen.
            self.app.shell.sched.later(60000, self._release)

    # ------------------------------------------------------------ RPC
    def register_rpc(self, rpc: Any) -> None:
        def power_do(params: dict) -> Future:
            confirmed = params.get("confirmed")
            return self.do(str(params.get("id", "")),
                           confirmed if isinstance(confirmed, bool) else None)

        rpc.register("power.menu", lambda p: self.menu())
        rpc.register("power.do", power_do)
