"""Bereich „System“: Systemsoftware und Updates, Konsoleninformationen, Uhr, Sprache, Energie.

Schreibwege:
    Betriebssystem-Update      backends/osupdate.py (Bazzites Helfer, gekapselt, im Prüfmodus aus)
    Flatpak-Updates            run_write: flatpak update --system|--user -y --noninteractive (E104: in der
                               Installation, in der die App liegt; backends/flatpak.update_cmds)
    Zeitzone                   run_write: /usr/bin/steamos-polkit-helpers/steamos-set-timezone <Zone>
                               (ruft sich selbst über pkexec --disable-internal-agent auf)
    Ausschalten/Neustart/Ruhe  deps.extra["power"](id) mit id in off | reboot | suspend
    Bildschirmschoner/Ruhemodus nach Leerlauf  config ui.saver_after, power.sleep_after(_media) (idle.py)
Nichts davon fragt je nach einem Kennwort. Ob der Controller den PC aus dem Ruhemodus wecken darf,
zeigt die Zeile system.power.wake (power/wakeup des Bluetooth-Adapters samt Hubs, eingerichtet per
udev-Regel mit Bestätigung des Nutzers). Uhr stellen und Sprache ändern bräuchten ein Kennwort und
entfallen deshalb (05 Abschnitt 10).

deps.extra: power, game_running (nur Spiele und Emulatoren), running_kinds (Arten aller laufenden
Apps, für den Text der Rückfragen), busy (-> Sätze für die Rückfragen); für Tests os_update_helper, os_update_marker, os_update_fixup,
os_update_bazzite, os_release, ostree_lister, ostree_root, ostree_booted, hwmon_root, zoneinfo_root, tz_helper.
E104: Das Systemabbild (ostree: neue Fassung wartet auf den Neustart) wird nur gelesen, wo /run/ostree-booted
liegt (Bazzite und andere Atomic-Systeme; nicht auf SteamOS).

Für den Verdrahter: area.updater.check_if_due() einmal nach dem Home-Start im Thread rufen
(tägliche Prüfung; ersetzt den abgeschalteten uupd.timer), area.update_badge() für die Anzeige.

Proton-Versionen (E102): Unter Updates öffnet die Zeile „Proton-Versionen“ die Seite system.proton
(installierte Versionen mit Zustand, Standard und Größe, darunter die verfügbaren je Familie) und je
installierter Version system.proton.build:<ordner> (Als Standard verwenden, Reparieren bzw. Neu laden,
Entfernen in den Papierkorb). Alles kommt aus deps.extra["proton"]() (wiring_proton.ProtonJobs);
fehlt sie, entfallen Zeile und Seite. Der Standard bleibt der eine Schlüssel games.proton (E22).

PC5 Launcher (E116): Unter Updates öffnet die Zeile „PC5 Launcher“ die Seite system.launcher (Version,
Zustand, Aktualisieren mit Rückfrage, danach Neu starten wie im Ein/Aus-Menü, Jetzt suchen, Schalter
update.check). Der Dienst ist selfupdate.SelfUpdate, spät gebunden über deps.extra["late"]("self_update")
(wiring_selfupdate); fehlt er, entfallen Zeile und Seite.
"""
from __future__ import annotations

import datetime
import logging
import math
import os
import re
import time
import zoneinfo

from .. import i18n, sysadmin
from . import rows as R
from .backends import TtlCache, flatpak, fmt_bytes, osupdate, ostree, silent_helper, sysinfo, test_mode
from .rows import AREA_ORDER, Page, SettingsError

log = logging.getLogger("couch.settings.system")

# Leerlauf (idle.py): Zeile -> (Schlüssel, Auswahl in Minuten, Beschriftung, Hinweis)
_SAVER = [(5, "5 Minuten"), (10, "10 Minuten"), (20, "20 Minuten"), (30, "30 Minuten"), (0, "Nie")]
_SLEEP = [(20, "20 Minuten"), (60, "1 Stunde"), (120, "2 Stunden"), (180, "3 Stunden"), (0, "Nie")]
_SLEEP_MEDIA = [(60, "1 Stunde"), (120, "2 Stunden"), (180, "3 Stunden"), (300, "5 Stunden"), (0, "Nie")]
IDLE_ROWS = {
    "system.power.saver": ("ui.saver_after", _SAVER, "Bildschirmschoner nach",
                           "Ohne Bedienung am Controller, im Home und in Spielen. Jede Taste beendet ihn."),
    "system.power.sleep": ("power.sleep_after", _SLEEP, "Ruhemodus nach",
                           "Ohne Bedienung im Home und in Spielen. Eine Minute vorher kommt eine Mitteilung; "
                           "Downloads und Installationen halten die Konsole wach."),
    "system.power.sleep_media": ("power.sleep_after_media", _SLEEP_MEDIA, "Ruhemodus bei Medien",
                                 "Solange YouTube, Kodi oder der Browser vorn ist."),
}
IDLE_DEFAULTS = {"ui.saver_after": 10, "power.sleep_after": 60, "power.sleep_after_media": 180}


def _app_titles() -> dict:
    """Flatpak-ID -> Name der Kachel (rocks.shy.VacuumTube -> YouTube)."""
    try:
        from ..library.apps import APPS, CHROME
    except Exception:  # noqa: BLE001
        return {}
    # nur die ersten vier Felder: library/apps.APPS wächst (E105 hängt hinten weitere an)
    titles = {app[3]: app[1] for app in APPS if app[2] == "flatpak"}
    # Die Kachel „Browser“ ist seit E59 der eigene Browser (browser.app "couch"); Chrome heißt beim Namen
    titles[CHROME] = "Google Chrome"
    return titles


def bt_wake_state(sys_root: str = "/sys", hci: str = "hci0") -> bool | None:
    """Darf der Bluetooth-Adapter den PC aus dem Ruhemodus wecken? Dazu muss power/wakeup am
    USB-Gerät und an jedem Hub bis zum Wurzel-Hub auf enabled stehen. None: kein Adapter."""
    dev = os.path.join(sys_root, "class", "bluetooth", hci or "hci0", "device")
    if not os.path.exists(dev):
        return None
    states = []
    path = os.path.realpath(dev)
    while path and path not in ("/", os.path.realpath(sys_root)):
        wakeup = os.path.join(path, "power", "wakeup")
        if os.path.isfile(os.path.join(path, "idVendor")) and os.path.isfile(wakeup):
            try:
                with open(wakeup) as f:
                    states.append(f.read().strip() == "enabled")
            except OSError:
                return None
        path = os.path.dirname(path)
    return all(states) if states else None

try:
    from .. import VERSION
except Exception:  # noqa: BLE001 - die Versionszeile darf den Bereich nicht kippen
    VERSION = "unbekannt"

TZ_HELPER = "/usr/bin/steamos-polkit-helpers/steamos-set-timezone"
_ZONE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
ZONES = [
    ("Europe/Berlin", "Berlin"), ("Europe/Vienna", "Wien"), ("Europe/Zurich", "Zürich"),
    ("Europe/Paris", "Paris"), ("Europe/Amsterdam", "Amsterdam"), ("Europe/Brussels", "Brüssel"),
    ("Europe/Rome", "Rom"), ("Europe/Madrid", "Madrid"), ("Europe/Warsaw", "Warschau"),
    ("Europe/Prague", "Prag"), ("Europe/Copenhagen", "Kopenhagen"), ("Europe/Stockholm", "Stockholm"),
    ("Europe/London", "London"), ("Europe/Lisbon", "Lissabon"), ("Europe/Athens", "Athen"),
    ("Europe/Helsinki", "Helsinki"), ("Europe/Kyiv", "Kiew"), ("Europe/Istanbul", "Istanbul"),
    ("Asia/Tbilisi", "Tiflis"), ("Asia/Dubai", "Dubai"), ("Asia/Kolkata", "Kalkutta"),
    ("Asia/Shanghai", "Shanghai"), ("Asia/Tokyo", "Tokio"), ("Australia/Sydney", "Sydney"),
    ("America/Sao_Paulo", "São Paulo"), ("America/New_York", "New York"), ("America/Chicago", "Chicago"),
    ("America/Denver", "Denver"), ("America/Los_Angeles", "Los Angeles"), ("UTC", "UTC (Weltzeit)"),
]
_DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"]
FLATPAK_TTL = 1800.0
GAME_KINDS = ("game", "emulator")       # LaunchSpec.kind, die als Spiel zählen (Spielstände)

# Proton-Versionen (E102): Seite und Unterseite je installierter Version; Daten und Aufträge aus
# deps.extra["proton"]() (wiring_proton.ProtonJobs)
PROTON_PAGE = "system.proton"
ADMIN_PAGE = "system.admin"                 # Systemfreigaben (sysadmin.py)
PROTON_BUILD = "system.proton.build:"
PROTON_PHASES = {"queued": "Wartet", "download": "Lädt", "verify": "Prüft", "extract": "Entpackt",
                 "swap": "Setzt ein", "done": "Fertig"}
PROTON_DOING = {"repair": "wird neu geladen", "install": "wird installiert"}
PROTON_BACKUP_TEXT = "Der bisherige Ordner kommt nach ~/Games/backups/proton und bleibt dort, bis du ihn löschst."
# PC5 Launcher aktualisieren (E116, selfupdate.py)
LAUNCHER_PAGE = "system.launcher"
UPDATE_CHECK_KEY = "update.check"           # automatisch nach einer neuen Fassung suchen (config.DEFAULTS)


def german_datetime(dt: datetime.datetime) -> str:
    return f"{_DAYS[dt.weekday()]}, {dt.day}. {_MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M} Uhr"


def utc_offset(zone: str, now: datetime.datetime | None = None) -> str:
    """"UTC+2" für die Auswahl; leer, wenn die Zone hier unbekannt ist."""
    try:
        tz = zoneinfo.ZoneInfo(zone)
    except Exception:  # noqa: BLE001 - unbekannte oder kaputte Zone
        return ""
    off = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz).utcoffset()
    if off is None:
        return ""
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def ram_label(total: int | None) -> str:
    if not total:
        return "Unbekannt"
    gib = total / 2 ** 30
    # MemTotal liegt etwas unter der eingebauten Menge (Reservierungen): auf Riegelgrößen aufrunden.
    shown = math.ceil(gib / 4) * 4 if gib >= 8 else math.ceil(gib)
    return f"{shown} GB"


class SystemArea:
    id = "system"
    label = "System"
    icon = "system"
    order = AREA_ORDER["system"]

    def __init__(self, deps):
        self.deps = deps
        x = deps.extra
        helper = x.get("os_update_helper")
        self.updater = osupdate.OsUpdater(
            helper=helper,
            marker=x.get("os_update_marker", osupdate.MARKER),
            fixup=x.get("os_update_fixup", None if helper else osupdate.FIXUP),
            bazzite=x.get("os_update_bazzite"),   # E104: Vorgabe osupdate.on_bazzite (sysdetect)
            online=self._online, game_running=self._game_running,
            on_change=lambda: deps.changed("system.software"),
            state_file=os.path.join(deps.paths.state_dir, "os-update.json"),
            # Im Prüfmodus nie den echten Helfer (pkexec); ein eingestecktes Attrappen-Skript schon.
            enabled=bool(helper) or not test_mode())
        self._static: dict = {}                 # Versionen ändern sich in einer Sitzung nicht
        self._cache = TtlCache(5.0)
        self._flatpak: dict = {"at": None, "updates": [], "error": "", "phase": "unknown"}
        self.sudo_runner = x.get("sudo_runner")  # Systemfreigaben: Tests stecken eine Attrappe ein
        self.admin_root = x.get("admin_root", "/")

    # --- Hilfen --------------------------------------------------------------------------

    def _game_running(self) -> bool:
        fn = self.deps.extra.get("game_running")
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:  # noqa: BLE001
            log.exception("game_running() wirft")
            return True            # im Zweifel nichts tun, was ein Spiel stören könnte

    def _online(self) -> bool:
        rc, out, _ = self.deps.run_read(["nmcli", "-t", "networking", "connectivity"], timeout=5)
        word = out.strip()
        return not (rc == 0 and word in ("none", "limited", "portal"))

    def _power(self, power_id: str):
        fn = self.deps.extra.get("power")
        if not callable(fn):
            raise SettingsError("Das ist gerade nicht verfügbar.")
        fn(power_id)
        return None

    def _os(self) -> dict:
        if "os" not in self._static:
            rel = ostree.read_os_release(self.deps.extra.get("os_release", "/etc/os-release"))
            name = rel.get("NAME") or rel.get("PRETTY_NAME") or "Linux"
            version = rel.get("OSTREE_VERSION") or rel.get("VERSION") or rel.get("VERSION_ID") or ""
            self._static["os"] = {"name": name, "version": version, "label": f"{name} {version}".strip()}
        return self._static["os"]

    def _versions(self) -> dict:
        """Kernel, Mesa, Gamescope, Grafikkarte: einmal lesen (zusammen rund 100 ms)."""
        if "versions" not in self._static:
            vk = sysinfo.read_vulkan(self.deps.run_read)
            self._static["versions"] = {
                "kernel": os.uname().release, "mesa": vk["mesa"], "gpu": vk["gpu"],
                "gamescope": sysinfo.read_gamescope_version(self.deps.run_read)}
        return self._static["versions"]

    def _ostree(self) -> dict:
        x = self.deps.extra
        if "ostree_lister" not in x and not ostree.booted(x.get("ostree_booted", ostree.BOOTED)):
            return ostree.no_ostree()             # E104: kein Atomic-System (SteamOS u. a.): libostree nicht fragen
        return self._cache.get("ostree", lambda: ostree.update_state(
            x.get("ostree_lister", ostree.list_deployments), x.get("ostree_root", "/ostree/deploy")))

    def _reboot_needed(self) -> tuple[bool, str]:
        st = self.updater.status()
        tree = self._ostree()
        if st["phase"] == "installed" or tree["reboot_pending"]:
            nxt = tree["next_version"]
            return True, (f"Version {nxt} wartet auf den Neustart." if nxt and tree["reboot_pending"] else
                          "Das Update wird beim nächsten Start aktiv.")
        return False, ""

    def update_badge(self) -> dict:
        """Für Anzeigen außerhalb der Einstellungen: {"state": "none"|"available"|"reboot", "text"}."""
        reboot, _ = self._reboot_needed()
        if reboot:
            return {"state": "reboot", "text": "Neustart erforderlich"}
        if self.updater.status()["phase"] == "available":
            return {"state": "available", "text": "Update verfügbar"}
        return {"state": "none", "text": ""}

    def busy_text(self) -> str | None:
        """Für die Rückfragen vor Ausschalten und Abmelden: ein Satz, solange ein Update läuft,
        das man nicht unterbrechen sollte; sonst None. Schnell (liest nichts)."""
        st = self.updater.status()
        if st["phase"] == "installing":
            done = " (%d %%)" % st["progress"] if st["progress"] is not None else ""
            return "Ein System-Update wird gerade installiert%s." % done
        if self._flatpak.get("phase") == "updating":
            return "Apps werden gerade aktualisiert."
        return None

    # --- Seiten --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        out = ["system", "system.software", "system.apps", "system.info", "system.datetime", "system.power",
               ADMIN_PAGE]
        out += [LAUNCHER_PAGE] if self._launcher() is not None else []
        return out + ([PROTON_PAGE] if self._proton() is not None else [])

    def page(self, page_id: str) -> Page:
        if page_id == PROTON_PAGE or page_id.startswith(PROTON_BUILD):
            return self._proton_page(page_id)
        if page_id == LAUNCHER_PAGE:
            if self._launcher() is None:
                raise KeyError(page_id)
            return Page(LAUNCHER_PAGE, "PC5 Launcher", self._launcher_rows())
        builders = {"system": ("System", self._main_rows),
                    "system.software": ("Systemsoftware", self._software_rows),
                    "system.apps": ("Updates", self._apps_rows),
                    "system.info": ("Konsoleninformationen", self._info_rows),
                    "system.datetime": ("Datum und Uhrzeit", self._datetime_rows),
                    "system.power": ("Energie", self._power_rows),
                    ADMIN_PAGE: ("Systemfreigaben", self._admin_rows)}
        if page_id not in builders:
            raise KeyError(page_id)
        title, fn = builders[page_id]
        return Page(page_id, title, fn())

    def _main_rows(self) -> list:
        badge = self.update_badge()
        return [
            R.link("system.software-page", "Systemsoftware", "system.software",
                   note=badge["text"] or self._os()["label"]),
            R.link("system.info-page", "Konsoleninformationen", "system.info"),
            R.link("system.datetime-page", "Datum und Uhrzeit", "system.datetime"),
            # Englisch (2026-10-25): wirkt sofort, die Seiten laden neu (main._language_changed)
            R.select("system.language", "Systemsprache", i18n.language(self.deps.config),
                     [{"value": code, "label": i18n.NAMES[code]} for code in i18n.LANGS]),
            R.link("system.power-page", "Energie", "system.power"),
            R.link("system.admin-page", "Systemfreigaben", ADMIN_PAGE, note=self._admin_note(),
                   hint="Einstellungen am System, die dein Kennwort brauchen (sudo), mit dem Controller einrichten."),
        ]

    # --- Systemfreigaben (2026-10-24: alle sudo-Schritte im Launcher, mit dem Controller) -----------

    def _admin_states(self) -> dict:
        out = {}
        for aid, act in sysadmin.ACTIONS.items():
            try:
                out[aid] = sysadmin.status(act, self.admin_root)
            except (OSError, ValueError):
                out[aid] = "unknown"
        return out

    def _admin_note(self) -> str:
        open_ = sum(1 for s in self._admin_states().values() if s != "on")
        return f"{open_} offen" if open_ else "Alles eingerichtet"

    def _admin_rows(self) -> list:
        texts = {"on": "Eingerichtet", "off": "Nicht eingerichtet", "changed": "Veraltet",
                 "unknown": "Unbekannt"}
        rows = []
        for aid, state in self._admin_states().items():
            act = sysadmin.ACTIONS[aid]
            rows.append(R.info(f"system.admin.state:{aid}", act.title, texts.get(state, state), hint=act.purpose))
            if state == "unknown":
                continue
            what = "remove" if state == "on" else "install"
            label = "Entfernen" if what == "remove" else ("Neu einrichten" if state == "changed" else "Einrichten")
            rows.append(R.text(f"system.admin.{what}:{aid}", label, "", secret=True, learn=False,
                               hint="Mit deinem Systemkennwort (sudo). Es geht nur an sudo und wird nirgends "
                                    "gespeichert."))
        return rows

    def _admin_set(self, row_id: str, value) -> dict:
        head, _, aid = row_id.partition(":")
        act = sysadmin.ACTIONS.get(aid)
        if act is None:
            raise SettingsError("Diese Systemfreigabe gibt es nicht.")
        mode = "install" if head == "system.admin.install" else "remove"
        ok, text = sysadmin.run(act, mode, str(value or ""), runner=self.sudo_runner)
        if not ok:
            raise SettingsError(text)
        self.deps.changed("system")
        return {"message": text, "refresh": True}

    def _software_rows(self) -> list:
        st = self.updater.status()
        v = self._versions()
        reboot, reboot_text = self._reboot_needed()
        status = "Neustart erforderlich" if reboot else st["text"]
        hint = reboot_text if reboot else st["message"]
        if not hint and st["checked_at"] and st["phase"] in ("current", "available"):
            hint = "Zuletzt geprüft: " + german_datetime(datetime.datetime.fromtimestamp(st["checked_at"]))
        supported = self.updater.supported()
        if not supported and not reboot:
            status, hint = "Nicht unterstützt", ""
        rows = [
            R.info("system.os", "Systemsoftware", self._os()["label"]),
            R.info("system.os.status", "Systemsoftware-Update", status, hint=hint),
        ]
        if reboot:
            rows.append(R.confirm_action(
                "system.os.reboot", "Jetzt neu starten", title="Konsole neu starten?",
                text="Das Update wird beim Neustart aktiv." + self._game_warning(), ok="Neu starten",
                enabled=callable(self.deps.extra.get("power"))))
        else:
            rows.append(R.action("system.os.check", "Nach Updates suchen", enabled=supported and not st["busy"]))
            why = self.updater.can_install()
            rows.append(R.confirm_action(
                "system.os.install", "Update installieren", title="Systemsoftware aktualisieren?",
                text="Das Update wird im Hintergrund geladen. Danach ist ein Neustart nötig. "
                     "Schalte die Konsole währenddessen nicht aus.",
                ok="Installieren", enabled=not why,
                note="Spiel läuft" if why and self._game_running() and st["phase"] == "available" else ""))
        n = self._update_count()
        rows.append(R.link("system.apps-page", "Updates", "system.apps",
                           note=(f"{n} verfügbar" if n else ""),
                           hint="Apps wie YouTube und Kodi, Proton-Versionen und Epic-Spiele."))
        rows += [
            R.info("system.kernel", "Kernel", v["kernel"]),
            R.info("system.mesa", "Grafiktreiber (Mesa)", v["mesa"] or "Unbekannt"),
            R.info("system.gamescope", "Gamescope", v["gamescope"] or "Unbekannt"),
            R.info("system.couch", "PC5 Launcher", VERSION),
        ]
        return rows

    def _running_kinds(self) -> list[str]:
        """Arten der laufenden Apps (game, emulator, media …). Ohne Rückruf zählt game_running."""
        fn = self.deps.extra.get("running_kinds")
        if not callable(fn):
            return ["game"] if self._game_running() else []
        try:
            return [str(k) for k in (fn() or [])]
        except Exception:  # noqa: BLE001
            log.exception("running_kinds() wirft")
            return ["game"]            # im Zweifel vor Spielständen warnen

    def _game_warning(self) -> str:
        # Vom „laufenden Spiel“ nur sprechen, wenn eines läuft; offene Apps allein werden nur
        # geschlossen (Befund 2026-10-14). Welche es sind, nennen die Sätze des Kerns („Noch offen“).
        kinds = self._running_kinds()
        games = any(k in GAME_KINDS for k in kinds)
        others = any(k not in GAME_KINDS for k in kinds)
        if games and others:
            text = " Laufende Spiele und Apps werden geschlossen; nicht gespeicherte Fortschritte gehen verloren."
        elif games:
            text = " Das laufende Spiel wird geschlossen; nicht gespeicherte Fortschritte gehen verloren."
        elif others:
            text = " Laufende Apps werden geschlossen."
        else:
            text = ""
        # Was sonst noch läuft (Installationen, Kopien), nennt der Kern als fertige Sätze (E31)
        fn = self.deps.extra.get("busy")
        if callable(fn):
            try:
                lines = [str(t).strip() for t in (fn() or []) if str(t).strip()]
            except Exception:  # noqa: BLE001
                log.exception("busy() wirft")
                lines = []
            if lines:
                text += " " + " ".join(lines)
        return text

    def _apps_rows(self) -> list:
        fp = self._flatpak
        now = time.monotonic()
        if fp["phase"] not in ("loading", "updating") and (fp["at"] is None or now - fp["at"] > FLATPAK_TTL):
            self._flatpak_load()
        fp = self._flatpak
        n = len(fp["updates"])
        if fp["phase"] == "loading":
            status = "Suche läuft …"
        elif fp["phase"] == "updating":
            status = "Wird aktualisiert …"
        elif fp["error"]:
            status = "Suche fehlgeschlagen"
        else:
            status = "Alles aktuell" if n == 0 else (f"{n} Updates verfügbar" if n != 1 else "1 Update verfügbar")
        busy = fp["phase"] in ("loading", "updating")
        reboot, _ = self._reboot_needed()
        os_status = "Neustart erforderlich" if reboot else self.updater.status()["text"]
        rows = [
            R.link("system.updates.os", "Systemsoftware", "system.software", note=os_status),
            *self._launcher_link(),
            R.action("system.apps.check", "Nach Updates suchen", enabled=not busy,
                     hint="Sucht nach Updates für Apps, Proton und Spiele. Das geschieht auch von selbst alle 12 Stunden."),
            R.info("system.apps.status", "Apps und Laufzeiten", status, hint=fp["error"]),
            R.action("system.apps.update", "Apps aktualisieren", enabled=n > 0 and not busy,
                     hint="Aktualisiert alle Flatpak-Apps. Schließe vorher laufende Apps."),
        ]
        titles = _app_titles()
        for u in fp["updates"][:40]:
            rows.append(R.info(f"system.apps.item:{u['id']}/{u['branch']}", titles.get(u["id"], u["name"]),
                               u["version"] or u["branch"], note="Laufzeit" if u["runtime"] else ""))
        return rows + self._proton_rows() + self._game_update_rows()

    def _watch(self):
        fn = self.deps.extra.get("updates")
        try:
            return fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            return None

    def _update_count(self) -> int:
        """Für die Zeile „Updates“: Apps (ohne Laufzeiten), Proton, Spiele."""
        found = getattr(self._watch(), "found", None) or {}
        apps = [u for u in (found.get("apps") if found else self._flatpak["updates"]) or [] if not u.get("runtime")]
        s = self._launcher_status()
        launcher = 1 if s and s["available"] and s["phase"] != "installed" else 0
        return len(apps) + (1 if found.get("proton") else 0) + len(found.get("games") or []) + launcher

    # --- PC5 Launcher aktualisieren (E116) ---------------------------------------------------

    def _launcher(self):
        """Der Dienst selfupdate.SelfUpdate (wiring_selfupdate) oder None: dann entfallen Zeile und Seite."""
        fn = self.deps.extra.get("late")
        try:
            svc = fn("self_update") if callable(fn) else None
        except Exception:  # noqa: BLE001
            log.exception("self_update")
            return None
        return svc if callable(getattr(svc, "status", None)) else None

    def _launcher_status(self) -> dict | None:
        svc = self._launcher()
        if svc is None:
            return None
        try:
            return svc.status()
        except Exception:  # noqa: BLE001  (eine kaputte Zustandsdatei darf die Seite nicht kosten)
            log.exception("Selbst-Update: Zustand")
            return None

    @staticmethod
    def _launcher_text(s: dict) -> tuple[str, str]:
        """(Zustand auf der Seite, kurzer Zustand in der Zeile unter Updates)."""
        phase = s["phase"]
        if phase == "downloading":
            return "Lädt … %d %%" % s["percent"], "Lädt %d %%" % s["percent"]
        if phase == "installing":
            return "Wird installiert …", "Wird installiert …"
        if phase == "installed":
            return "%s installiert, neu starten" % s["pending"], "Neu starten"
        if phase == "failed":
            return "Update fehlgeschlagen", "Fehlgeschlagen"
        if phase == "available":
            text = "%s verfügbar" % s["available"]
            return text, text
        if phase == "current":
            return "Aktuell", "Aktuell"
        text = "Unbekannt" if s["error"] else "Noch nicht geprüft"
        return text, text

    def _launcher_link(self) -> list:
        s = self._launcher_status()
        if s is None:
            return []
        return [R.link("system.updates.launcher", "PC5 Launcher", LAUNCHER_PAGE, note=self._launcher_text(s)[1],
                       hint="Neue Fassungen des PC5 Launchers von GitHub.")]

    def _launcher_rows(self) -> list:
        s = self._launcher_status()
        if s is None:
            return [R.info("system.launcher.gone", "Das ist gerade nicht verfügbar.", enabled=False)]
        package = s["kind"] == "package"
        state, _short = self._launcher_text(s)
        phase = s["phase"]
        if phase == "failed":
            hint = s["job_error"]
        elif not package and s["available"]:
            hint = s["why"]
        elif s["error"] and phase in ("unknown", "current"):
            hint = s["error"]
        elif isinstance(s["checked_at"], (int, float)) and phase in ("current", "available"):
            hint = "Zuletzt geprüft: " + german_datetime(datetime.datetime.fromtimestamp(s["checked_at"]))
        else:
            hint = ""
        rows = [R.info("system.launcher.version", "Installierte Version", s["own"],
                       note="" if package else "Nicht aus dem Paket"),
                R.info("system.launcher.status", "Zustand", state, hint=hint)]
        version = s["available"]
        if phase == "downloading":
            rows.append(R.action("system.launcher.cancel", "Abbrechen",
                                 hint="Das geladene Stück wird verworfen; ein neuer Versuch lädt das Paket neu."))
        elif phase == "installed":
            power = callable(self.deps.extra.get("power"))
            rows.append(R.confirm_action(
                "system.launcher.restart", "Neu starten", title="PC5 Launcher neu starten?",
                text=("Die Oberfläche wird beendet und startet mit der Fassung %s neu, der Rechner bleibt an."
                      % s["pending"]) + self._game_warning(),
                ok="Neu starten", enabled=power, note="" if power else "Nicht verfügbar"))
        elif version and package and phase != "installing":
            rows.append(R.confirm_action(
                "system.launcher.update", "Aktualisieren", title="PC5 Launcher %s installieren?" % version,
                text="Lädt die Fassung %s (%s) von GitHub, prüft die Prüfsumme und legt sie neben die laufende. Was "
                     "gerade läuft, wird nicht ersetzt: Die neue Fassung gilt nach einem Neustart des PC5 Launchers."
                     % (version, fmt_bytes(s["size"])),
                ok="Aktualisieren", enabled=phase in ("available", "failed"),
                hint="Die bisherige Fassung bleibt als Rückfall erhalten."))
        elif version and not package:
            rows.append(R.info("system.launcher.dev", "Aktualisieren", "Nicht möglich", hint=s["why"]))
        busy = phase in ("downloading", "installing")
        rows.append(R.action("system.launcher.check", "Jetzt nach Updates suchen", enabled=not busy and not s["checking"],
                             note="Suche läuft …" if s["checking"] else "",
                             hint="Fragt GitHub gleich nach einer neuen Fassung des PC5 Launchers."))
        rows.append(R.toggle("system.launcher.auto", "Automatisch nach Updates suchen",
                             bool(self.deps.config.get(UPDATE_CHECK_KEY, True)), default=True,
                             hint="Einmal am Tag fragt der PC5 Launcher bei GitHub nach einer neuen Fassung und meldet "
                                  "sie mit einer Mitteilung. Installiert wird nur, was du hier bestätigst."))
        return rows

    def _launcher_action(self, row_id: str):
        svc = self._launcher()
        if svc is None:
            raise SettingsError("Das ist gerade nicht verfügbar.")
        if row_id == "system.launcher.restart":
            return self._launcher_restart()
        from ..selfupdate import UpdateError
        try:
            if row_id == "system.launcher.check":
                s = svc.check(force=True, quiet=True)       # die Seite zeigt es selbst, keine Mitteilung dazu
                if s["available"]:
                    message = "PC5 Launcher %s ist da." % s["available"]
                elif s["error"]:
                    message = s["error"]
                else:
                    message = "Der PC5 Launcher ist aktuell."
            elif row_id == "system.launcher.update":
                message = svc.start_update()
            elif row_id == "system.launcher.cancel":
                message = svc.cancel()
            else:
                raise SettingsError("Diese Aktion gibt es nicht.")
        except UpdateError as e:
            raise SettingsError(e.message) from None
        self.deps.changed("system.apps")
        return {"message": message, "refresh": True}

    def _launcher_restart(self):
        """Wie „PC5 Launcher neu starten“ im Ein/Aus-Menü (power.do restart-session): Die Sitzung beginnt neu
        und nimmt die Fassung, auf die current jetzt zeigt."""
        fn = self.deps.extra.get("power")
        if not callable(fn):
            raise SettingsError("Das ist gerade nicht verfügbar.")
        try:
            fn("restart-session")
        except SettingsError:
            raise
        except Exception as e:  # noqa: BLE001
            message = getattr(e, "message", None)
            if isinstance(message, str) and message:
                raise SettingsError(message) from e
            raise
        return None

    def _proton_rows(self) -> list:
        return self._proton_update_rows() + self._proton_link()

    def _proton_update_rows(self) -> list:
        watch = self._watch()
        if watch is None:
            return []
        found = watch.found or {}
        rel = found.get("proton")
        job = watch.proton_job
        installed = ""
        for d in self.deps.extra.get("compat_tools_dirs") or []:
            try:
                names = sorted(n for n in os.listdir(d) if n.startswith("proton-cachyos-"))
            except OSError:
                names = []
            installed = names[-1] if names else installed
        short = lambda n: n.replace("proton-cachyos-", "").replace("-x86_64_v3", "").replace("-x86_64", "")  # noqa: E731
        if job["phase"] == "loading":
            value = "Wird geladen … %d %%" % job["percent"]
        elif rel:
            value = "Neu: " + short(rel["name"])
        else:
            value = "Aktuell" if found else "Noch nicht geprüft"
        rows = [R.info("system.updates.proton", "Proton-CachyOS", value,
                       note=short(installed) if installed else "", hint=job["error"] or
                       "Startet deine Windows-Spiele. Die neue Fassung kommt neben die bisherige; mit "
                       "„Standard-Proton: Automatisch“ nutzen Spiele ab dem nächsten Start die neue.")]
        if rel:
            rows.append(R.confirm_action(
                "system.updates.proton.install", "Proton-CachyOS aktualisieren", title="Proton-CachyOS aktualisieren?",
                text="Lädt %s (%d MB) und legt die Fassung neben die bisherige." % (short(rel["name"]), rel.get("size", 0) // 1048576),
                ok="Aktualisieren", enabled=job["phase"] != "loading"))
        return rows

    # --- Proton-Versionen (E102) -----------------------------------------------------------

    def _proton(self):
        """Die Proton-Warteschlange (wiring_proton.ProtonJobs) oder None: dann entfällt die Seite."""
        fn = self.deps.extra.get("proton")
        try:
            return fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            log.exception("proton() wirft")
            return None

    def _proton_link(self) -> list:
        jobs = self._proton()
        if jobs is None:
            return []
        try:
            s = jobs.summary()
        except Exception:  # noqa: BLE001
            log.exception("Proton-Übersicht")
            s = {}
        job = s.get("job")
        if job:
            note = "%s %d %%" % (PROTON_PHASES.get(job.get("phase"), "Lädt"), int(job.get("percent") or 0))
        elif s.get("damaged"):
            note = "%d beschädigt" % s["damaged"]
        else:
            note = "%d installiert" % s["installed"] if s.get("installed") else ""
        return [R.link("system.updates.proton-versions", "Proton-Versionen", PROTON_PAGE, note=note,
                       hint="Weitere Proton-Versionen laden (Proton-CachyOS, GE-Proton, UMU-Proton), installierte "
                            "prüfen, reparieren und entfernen.")]

    def _proton_default(self, compat: str) -> tuple[str, str]:
        """(fest gewählter Build oder "", den „Automatisch“ meint): der eine Schlüssel games.proton (E22)."""
        from .games import list_proton_builds, resolve_proton
        dirs = self.deps.extra.get("compat_tools_dirs") or ([compat] if compat else [])
        builds = list_proton_builds(dirs)
        wanted = str(self.deps.config.get("games.proton", "") or "").strip()
        auto = resolve_proton("", builds)
        return (wanted if any(b["id"] == wanted for b in builds) else ""), (auto["id"] if auto else "")

    def _proton_page(self, page_id: str) -> Page:
        from .. import proton_repair as pr
        jobs = self._proton()
        if jobs is None:
            raise KeyError(page_id)
        ov = jobs.overview()
        wanted, auto = self._proton_default(ov["compat"])
        if page_id == PROTON_PAGE:
            return Page(PROTON_PAGE, "Proton-Versionen", self._proton_main(ov, wanted, auto))
        name = page_id[len(PROTON_BUILD):]
        entry = next((e for e in ov["installed"] if e["name"] == name), None)
        if entry is None:
            return Page(page_id, pr.label(name), [R.info(page_id + ".gone", "Diese Version ist nicht (mehr) installiert.",
                                                         enabled=False)])
        return Page(page_id, entry["label"], self._proton_build(jobs, ov, entry, wanted, auto))

    def _proton_main(self, ov: dict, wanted: str, auto: str) -> list:
        from .. import proton_repair as pr
        rows = []
        job = ov["job"]
        if job:
            rows.append(R.info("system.proton.job", "%s %s" % (job["label"], PROTON_DOING.get(job["action"], "")),
                               "%d %%" % job["percent"], note=PROTON_PHASES.get(job["phase"], ""),
                               hint="Den Fortschritt siehst du auch unter Downloads im Control Center."))
            rows.append(R.action("system.proton.cancel", "Abbrechen",
                                 hint="Ein neuer Versuch setzt den Download dort fort, wo er aufgehört hat."))
        for q in ov["queue"]:
            rows.append(R.info("system.proton.queued:" + q["name"], "%s %s" % (q["label"], PROTON_DOING.get(q["action"], "")),
                               "Wartet",
                               hint="Kommt dran, sobald der laufende Proton-Auftrag fertig ist."))
        if wanted:
            value = pr.label(wanted)
            hint = ("Fest gewählt, gilt für alle Spiele ohne eigene Auswahl. Ändern kannst du das bei jeder Version "
                    "unten oder unter Spiele › Standard-Proton.")
        else:
            value = "Automatisch" + (" · " + pr.label(auto) if auto else "")
            # games.list_proton_builds: FSR 4 zuerst, darin Proton-CachyOS vor den anderen Familien (E102)
            hint = ("Automatisch nimmt die neueste Version mit FSR 4, Proton-CachyOS vor den anderen; eine neu "
                    "installierte gilt ab dem nächsten Spielstart. Fest wählen kannst du bei jeder Version unten.")
        rows.append(R.info("system.proton.default", "Standard-Proton", value, hint=hint))
        if wanted:
            rows.append(R.action("system.proton.auto", "Automatisch wählen lassen",
                                 hint="Dann nimmt jedes Spiel ohne eigene Auswahl die neueste Version mit FSR 4, "
                                      "Proton-CachyOS vor den anderen."))
        inst = ov["installed"]
        damaged = sum(1 for e in inst if not e["ok"])
        rows.append(R.info("system.proton.installed", "Installiert",
                           ("%d Versionen" % len(inst) if len(inst) != 1 else "1 Version") if inst else "Keine",
                           note="%d beschädigt" % damaged if damaged else "",
                           hint="In ~/.local/share/Steam/compatibilitytools.d; Steam sieht sie ebenfalls."))
        for e in inst:
            notes = []
            if e["name"] == (wanted or auto):
                notes.append("Standard")
            if e["name"] in ov["jobs"]:
                notes.append("Wird neu geladen")
            elif not e["ok"]:
                notes.append("Beschädigt")
            if e.get("size_bytes") is not None:
                notes.append(fmt_bytes(e["size_bytes"]))
            rows.append(R.link("system.proton.open:" + e["name"], e["label"], PROTON_BUILD + e["name"],
                               note=" · ".join(notes), hint=e["reason"] if not e["ok"] else ""))
        return rows + self._proton_available(ov)

    def _proton_available(self, ov: dict) -> list:
        from .. import proton_repair as pr
        cat = ov["catalog"]
        items = cat.get("items") or []
        when = datetime.datetime.fromtimestamp(cat["checked_at"]).strftime("%d.%m.%Y %H:%M") \
            if cat.get("checked_at") else ""
        if cat.get("loading") and not items:
            value, hint = "Wird gesucht …", ""
        elif cat.get("error") and items:
            value = "Letzter Stand"
            hint = "Keine Verbindung zu GitHub, das ist der Stand vom %s. %s" % (when or "letzten Mal", cat["error"])
        elif cat.get("error"):
            value, hint = "Nicht erreichbar", cat["error"]
        else:
            value = "Stand: " + when if when else ""
            hint = "Von GitHub, je Familie die neuesten Fassungen mit Prüfsumme. Geladen wird nur, was hier steht."
        rows = [R.info("system.proton.available", "Verfügbar", value, hint=hint),
                R.action("system.proton.refresh", "Neu suchen", enabled=not cat.get("loading"),
                         note="Suche läuft …" if cat.get("loading") else "",
                         hint="Fragt GitHub gleich noch einmal statt nach dem Zwischenspeicher (6 Stunden).")]
        for fam in pr.CATALOG:
            if not fam["listed"] and not ov["show_em"]:
                continue
            mine = [i for i in items if i.get("family") == fam["family"]]
            if not mine:
                continue
            rows.append(R.info("system.proton.family:" + fam["family"], fam["label"],
                               "Empfohlen" if fam.get("default") else "", hint=pr.family_text(fam)))
            for it in mine:
                rows.append(self._proton_item(it, ov))
        em = next(c for c in pr.CATALOG if c["family"] == "proton-em")
        rows.append(R.action("system.proton.em", "Proton-EM ausblenden" if ov["show_em"] else "Proton-EM zeigen",
                             hint=pr.family_text(em)))
        return rows

    def _proton_item(self, it: dict, ov: dict):
        name, label = str(it.get("name") or ""), str(it.get("label") or it.get("name") or "")
        size = fmt_bytes(it["size"]) if it.get("size") else ""
        date = ""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(it.get("date") or ""))
        if m:
            date = "%s.%s.%s" % (m.group(3), m.group(2), m.group(1))
        hint = str(it.get("notes") or "")
        rid = "system.proton.get:" + name
        job = ov["jobs"].get(name)
        if job:
            value = "Wartet" if not job["running"] else "%s %d %%" % (PROTON_PHASES.get(job["phase"], "Lädt"), job["percent"])
            return R.info(rid, label, value, hint=hint)
        if it.get("installed"):
            return R.info(rid, label, "Installiert", note=date, hint=hint)
        return R.confirm_action(
            rid, label, title="%s installieren?" % label,
            text="Lädt %s von GitHub, prüft die Prüfsumme und legt die Version neben die bisherigen. Danach kannst "
                 "du sie als Standard oder je Spiel wählen." % (size or "die Version"),
            ok="Installieren", note=" · ".join(x for x in (date, size) if x), hint=hint)

    def _proton_build(self, jobs, ov: dict, e: dict, wanted: str, auto: str) -> list:
        from .. import proton_repair as pr
        name, label = e["name"], e["label"]
        job = ov["jobs"].get(name)
        if job:
            state = "Wartet aufs Neuladen" if not job["running"] else \
                "Wird neu geladen … %d %%" % job["percent"]
        else:
            state = "Heil" if e["ok"] else "Beschädigt"
        fam = next((c for c in pr.CATALOG if c["family"] == e.get("family")), None)
        size = fmt_bytes(e["size_bytes"]) if e.get("size_bytes") is not None else "Wird berechnet …"
        path = os.path.join(ov["compat"], name)
        rows = [R.info(PROTON_BUILD.replace(":", ".state:") + name, "Zustand", state, hint=e.get("reason") or ""),
                R.info(PROTON_BUILD.replace(":", ".family:") + name, "Familie", fam["label"] if fam else "Unbekannt",
                       hint=pr.family_text(fam) if fam else (e.get("display_name") or "")),
                R.info(PROTON_BUILD.replace(":", ".size:") + name, "Größe", size),
                R.info(PROTON_BUILD.replace(":", ".folder:") + name, "Ordner", name, hint=path)]
        is_default = wanted == name
        rows.append(R.action(
            "system.proton.build.use:" + name, "Als Standard verwenden", enabled=e["ok"] and not is_default and not job,
            note="Standard" if is_default else ("Automatisch gewählt" if not wanted and auto == name else ""),
            hint="Gilt für alle Spiele ohne eigene Auswahl (Spiele › Standard-Proton)." if e["ok"] else
                 "Beschädigt: erst reparieren."))
        can_fix = bool(e.get("source")) and not job
        why_fix = "" if e.get("source") else "Für diese Version ist keine Quelle bekannt; sie lässt sich nur entfernen."
        if not e["ok"]:
            rows.append(R.action(
                "system.proton.build.repair:" + name, "Reparieren", enabled=can_fix, note="Nicht möglich" if why_fix else "",
                hint=why_fix or "Lädt die Version neu von GitHub und tauscht den Ordner aus, sobald kein Spiel ihn "
                                "nutzt. " + PROTON_BACKUP_TEXT))
        else:
            rows.append(R.confirm_action(
                "system.proton.build.reload:" + name, "Neu laden", title="%s neu laden?" % label,
                text="Lädt die Version neu von GitHub (einige hundert MB) und tauscht den Ordner aus, sobald kein "
                     "Spiel ihn nutzt. " + PROTON_BACKUP_TEXT, ok="Neu laden", enabled=can_fix,
                note="Nicht möglich" if why_fix else "",
                hint=why_fix or "Nur nötig, wenn ein Spiel mit dieser Version Fehler zeigt, die Prüfung aber nichts findet."))
        try:
            why = jobs.can_remove(name)
        except Exception:  # noqa: BLE001
            log.exception("Proton entfernen prüfen")
            why = "Das lässt sich gerade nicht prüfen."
        rows.append(R.confirm_action(
            "system.proton.build.remove:" + name, "Entfernen", title="%s entfernen?" % label,
            text="Die Version kommt in den Papierkorb. Zurücklegen oder endgültig löschen kannst du sie im "
                 "Datei-Explorer unter Papierkorb.", ok="Entfernen", enabled=not why,
            note="Nicht möglich" if why else "", hint=why or "Spiele ohne eigene Auswahl nehmen danach den Standard."))
        return rows

    def _proton_action(self, row_id: str, value=None):
        jobs = self._proton()
        if jobs is None:
            raise SettingsError("Das ist gerade nicht verfügbar.")
        head, _sep, name = row_id.partition(":")
        page = None
        if row_id == "system.proton.cancel":
            out = jobs.cancel()
        elif row_id == "system.proton.refresh":
            out = jobs.refresh()
        elif row_id == "system.proton.em":
            jobs.set_show_em(not jobs.show_em)
            out = {"ok": True}
        elif row_id == "system.proton.auto":
            out = jobs.set_default("")
        elif head == "system.proton.get" and name:
            out = jobs.install(name)
        elif head == "system.proton.build.use" and name:
            out = jobs.set_default(name)
        elif head in ("system.proton.build.repair", "system.proton.build.reload") and name:
            out = jobs.repair(name)
        elif head == "system.proton.build.remove" and name:
            out = jobs.remove(name)
            page = PROTON_PAGE              # zurück zur Liste: diese Unterseite gibt es danach nicht mehr
        else:
            raise SettingsError("Diese Aktion gibt es nicht.")
        out = out if isinstance(out, dict) else {}
        if not out.get("ok"):
            raise SettingsError(str(out.get("message") or "Das hat nicht geklappt."))
        result = {"page": page} if page else {"refresh": True}
        if out.get("message"):
            result["message"] = str(out["message"])
        return result

    def _game_update_rows(self) -> list:
        watch = self._watch()
        if watch is None:
            return []
        found = watch.found or {}
        games = found.get("games") or []
        busy = {j.get("app") for j in (self._call_extra("epic_jobs") or []) if j.get("kind") == "update"}
        rows = [R.info("system.updates.games", "Spiele",
                       ("%d Updates" % len(games) if len(games) != 1 else "1 Update") if games else
                       ("Alles aktuell" if found else "Noch nicht geprüft"),
                       # itch.io (E100): nur ein Hinweis auf der Spielseite (updateHint), keine Suche hier
                       hint="Epic-Spiele aus dem Store. Steam aktualisiert seine Spiele selbst; für GOG-Spiele "
                            "gibt es noch keine Suche. Bei itch.io-Spielen nennt die Spielseite im Store eine "
                            "neuere Datei.")]
        for g in games:
            rows.append(R.action("system.updates.game:" + g["app"], g["title"], enabled=g["app"] not in busy,
                                 note="Wird aktualisiert …" if g["app"] in busy else "Aktualisieren",
                                 hint="Installiert: %s · Neu: %s" % (g["installed"][:24], g["latest"][:24])))
        return rows

    def _call_extra(self, name: str):
        fn = self.deps.extra.get(name)
        try:
            return fn() if callable(fn) else None
        except Exception:  # noqa: BLE001
            log.exception("%s() wirft", name)
            return None

    def _flatpak_load(self) -> None:
        self._flatpak = {**self._flatpak, "phase": "loading"}

        def job():
            updates, error = flatpak.read_updates(self.deps.run_read)
            self._flatpak = {"at": time.monotonic(), "updates": updates, "error": error, "phase": "ready"}
            self.deps.changed("system.apps")
        self.deps.scheduler.background(job)

    def _info_rows(self) -> list:
        v = self._versions()
        if "hw" not in self._static:
            self._static["hw"] = {"cpu": sysinfo.cpu_model(), "ram": sysinfo.mem_total()}
        hw = self._static["hw"]
        s = sysinfo.sensors(self.deps.extra.get("hwmon_root", "/sys/class/hwmon"))
        rows = [
            R.info("system.cpu", "Prozessor", hw["cpu"] or "Unbekannt"),
            R.info("system.gpu", "Grafikkarte", re.sub(r"\s*\(RADV[^)]*\)", "", v["gpu"]) or "Unbekannt"),
            R.info("system.ram", "Arbeitsspeicher", ram_label(hw["ram"])),
        ]
        if s["cpu_temp"] is not None:
            rows.append(R.info("system.cpu-temp", "Temperatur Prozessor", f"{s['cpu_temp']:.0f} °C"))
        if s["gpu_temp"] is not None:
            rows.append(R.info("system.gpu-temp", "Temperatur Grafikkarte", f"{s['gpu_temp']:.0f} °C"))
        return rows

    def _datetime_rows(self) -> list:
        td = self._cache.get("time", lambda: sysinfo.read_time(self.deps.run_read))
        tz = td["timezone"]
        options = [R.option(z, label, utc_offset(z)) for z, label in ZONES if self._zone_exists(z)]
        if tz and tz not in [o["value"] for o in options]:
            options.insert(0, R.option(tz, tz.split("/")[-1].replace("_", " "), utc_offset(tz)))
        can_set = silent_helper(self._tz_helper())
        return [
            R.info("system.time", "Datum und Uhrzeit", german_datetime(datetime.datetime.now())),
            R.info("system.ntp", "Automatisch einstellen", "Ein" if td["ntp"] else "Aus",
                   hint="Die Uhrzeit kommt aus dem Internet." if td["ntp"] else ""),
            R.select("system.timezone", "Zeitzone", tz, options, enabled=can_set,
                     note="" if can_set else "Nicht unterstützt"),
        ]

    def _tz_helper(self) -> str:
        return self.deps.extra.get("tz_helper", TZ_HELPER)

    def _zone_exists(self, zone: str) -> bool:
        root = self.deps.extra.get("zoneinfo_root", "/usr/share/zoneinfo")
        return bool(_ZONE.match(zone)) and os.path.isfile(os.path.join(root, zone))

    def _power_rows(self) -> list:
        ok = callable(self.deps.extra.get("power"))
        note = "" if ok else "Nicht verfügbar"
        warn = self._game_warning()
        can_suspend = self._cache.get("suspend", lambda: sysinfo.logind_can(self.deps.run_read, "CanSuspend"))
        # Der Adapter, an dem der Controller hängt (zwei Adapter seit 2026-10-21: Dongle und Intel-Chip)
        hci = self._call_extra("bt_hci") or "hci0"
        wake = bt_wake_state(self.deps.extra.get("sys_root", "/sys"), str(hci))
        wake_text = ("Aufwecken mit der PS-Taste am Controller oder dem Einschaltknopf am PC." if wake else
                     "Aufwecken mit dem Einschaltknopf am PC. Das Wecken per Controller ist noch nicht eingerichtet.")
        return [
            R.confirm_action("system.power.off", "Konsole ausschalten", title="Konsole ausschalten?",
                             text=("Die Konsole wird heruntergefahren." + warn).strip(), ok="Ausschalten",
                             enabled=ok, note=note),
            R.confirm_action("system.power.reboot", "Konsole neu starten", title="Konsole neu starten?",
                             text=("Die Konsole startet neu." + warn).strip(), ok="Neu starten",
                             enabled=ok, note=note),
            R.confirm_action("system.power.suspend", "Ruhemodus aktivieren", title="Ruhemodus aktivieren?",
                             text=wake_text, ok="Ruhemodus", enabled=ok and can_suspend,
                             note=note or ("" if can_suspend else "Nicht unterstützt"), hint=wake_text),
        ] + self._idle_rows(can_suspend, wake)

    def _idle_rows(self, can_suspend: bool, wake: bool | None) -> list:
        """Leerlauf (idle.py): Bildschirmschoner und Ruhemodus nach Minuten ohne Bedienung."""
        cfg = self.deps.config
        rows = []
        for rid, (key, choices, label, hint) in IDLE_ROWS.items():
            if key.startswith("power.") and not can_suspend:
                continue
            value = cfg.get(key, IDLE_DEFAULTS[key])
            if value not in [v for v, _ in choices]:
                value = IDLE_DEFAULTS[key]
            rows.append(R.select(rid, label, value, [R.option(v, text) for v, text in choices],
                                 default=IDLE_DEFAULTS[key], hint=hint))
        if can_suspend:
            rows.append(R.info("system.power.wake", "Mit dem Controller aufwecken",
                               "Eingerichtet" if wake else "Nicht eingerichtet" if wake is False else "Unbekannt",
                               hint="Aus dem Ruhemodus mit der PS-Taste am Bluetooth-Controller. Komplett "
                                    "ausgeschaltet weckt nur der Einschaltknopf am PC."))
        return rows

    # --- Schreiben -----------------------------------------------------------------------

    def set(self, row_id: str, value):
        if row_id == "system.language":
            if value not in i18n.LANGS:
                raise SettingsError("Diese Sprache gibt es nicht.")
            self.deps.config.set("ui.language", value)
            return {"refresh": True}
        if row_id.startswith(("system.admin.install:", "system.admin.remove:")):
            return self._admin_set(row_id, value)
        if row_id == "system.launcher.auto":
            if not isinstance(value, bool):
                raise SettingsError("Ungültiger Wert.")
            self.deps.config.set(UPDATE_CHECK_KEY, value)
            return None
        if row_id in IDLE_ROWS:
            key, choices, _label, _hint = IDLE_ROWS[row_id]
            if isinstance(value, bool) or value not in [v for v, _ in choices]:
                raise SettingsError("Ungültiger Wert.")
            self.deps.config.set(key, value)
            return None
        if row_id == "system.timezone":
            zone = str(value)
            if not self._zone_exists(zone):
                raise SettingsError("Diese Zeitzone gibt es nicht.")
            if not silent_helper(self._tz_helper()):
                raise SettingsError("Die Zeitzone lässt sich auf diesem System nicht ändern.")
            rc, _out, err = self.deps.run_write([self._tz_helper(), zone], timeout=20)
            self._cache.drop("time")
            if rc != 0:
                log.warning("Zeitzone: rc=%s %s", rc, (err or "").strip())
                raise SettingsError("Die Zeitzone ließ sich nicht ändern.")
            try:
                time.tzset()          # eigene Uhr auf die neue Zone umstellen
            except AttributeError:
                pass
            return {"refresh": True}
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        if row_id == "system.os.check":
            st = self.updater.check()
            return {"message": st["message"] or st["text"], "refresh": True}
        if row_id == "system.os.install":
            why = self.updater.can_install()
            if why:
                raise SettingsError(why)
            self.deps.scheduler.background(self._install_job)
            return {"message": "Das Update wird installiert …", "refresh": True}
        if row_id == "system.os.reboot":
            return self._power("reboot")
        if row_id == "system.apps.check":
            if self._flatpak["phase"] in ("loading", "updating"):
                raise SettingsError("Es läuft schon ein Vorgang.")
            self._flatpak_load()
            watch = self._watch()
            if watch is not None:
                self.deps.scheduler.background(watch.check)     # Proton und Spiele gleich mit
            return {"refresh": True}
        if row_id == "system.updates.proton.install":
            watch = self._watch()
            if watch is None:
                raise SettingsError("Das ist gerade nicht verfügbar.")
            try:
                return {"message": watch.start_proton(), "refresh": True}
            except ValueError as e:
                raise SettingsError(str(e)) from None
        if row_id.startswith("system.proton."):
            return self._proton_action(row_id, value)
        if row_id.startswith("system.launcher."):
            return self._launcher_action(row_id)
        if row_id.startswith("system.updates.game:"):
            fn = self.deps.extra.get("epic_update")
            if not callable(fn):
                raise SettingsError("Das ist gerade nicht verfügbar.")
            return {"message": fn(row_id.split(":", 1)[1]), "refresh": True}
        if row_id == "system.apps.update":
            return self._flatpak_update()
        if row_id in ("system.power.off", "system.power.reboot", "system.power.suspend"):
            return self._power(row_id.rsplit(".", 1)[1])
        raise SettingsError("Diese Aktion gibt es nicht.")

    def _install_job(self) -> None:
        ok = self.updater.install()
        self._cache.drop("ostree")
        st = self.updater.status()
        try:
            if ok:
                self.deps.notify("Systemsoftware aktualisiert", "Starte die Konsole neu, um das Update zu verwenden.", "update")
            else:
                self.deps.notify("Update fehlgeschlagen", st["message"], "warn")
        except Exception:  # noqa: BLE001
            log.exception("notify")
        self.deps.changed("system.software")
        self.deps.changed("system")

    def _flatpak_update(self):
        fp = self._flatpak
        if fp["phase"] in ("loading", "updating"):
            raise SettingsError("Es läuft schon ein Vorgang.")
        if not fp["updates"]:
            raise SettingsError("Es gibt nichts zu aktualisieren.")
        if self._game_running():
            raise SettingsError("Schließe zuerst das laufende Spiel.")
        running = flatpak.read_running(self.deps.run_read)
        if running:
            # Eine laufende App würde mitten im Betrieb ihre Dateien verlieren.
            raise SettingsError("Schließe zuerst: " + ", ".join(r.split(".")[-1] for r in running[:3]) + ".")
        self._flatpak = {**fp, "phase": "updating"}

        cmds = flatpak.update_cmds(fp["updates"])

        def job():
            rc = 0
            for cmd in cmds:                     # E104: System und Nutzer je für sich, das Erste zuerst
                code, _out, err = self.deps.run_write(cmd, timeout=3600)
                if code != 0:
                    log.warning("%s: rc=%s %s", " ".join(cmd[:3]), code, (err or "").strip()[-300:])
                    rc = rc or code
            updates, error = flatpak.read_updates(self.deps.run_read)
            self._flatpak = {"at": time.monotonic(), "updates": updates, "phase": "ready",
                             "error": error or ("" if rc == 0 else "Die Aktualisierung ist fehlgeschlagen.")}
            try:
                self.deps.notify("Apps aktualisiert" if rc == 0 else "App-Updates fehlgeschlagen", "",
                                 "update" if rc == 0 else "warn")
            except Exception:  # noqa: BLE001
                log.exception("notify")
            self.deps.changed("system.apps")
            watch = self._watch()
            if watch is not None:
                watch.check()                    # gefundene App-Updates und Punkt am Zahnrad neu
        self.deps.scheduler.background(job)
        return {"message": "Apps werden aktualisiert …", "refresh": True}


def make_area(deps) -> SystemArea:
    return SystemArea(deps)
