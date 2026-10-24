"""Einstellungen › Spiele.

Hier stehen die globalen Vorgaben. Je Spiel lässt sich in entry.options nur zusätzlich abschalten
(E41: Ein globales Aus gilt für alle; das stellt die Oberfläche über lib.setOption ein, nicht
dieser Bereich). Steam-, Lutris- und EmuDeck-Dateien werden nicht
geschrieben; gelesen wird nur der Ordner der Proton-Builds.

Was der Kern einsteckt (deps.extra, alles freiwillig; fehlt ein Rückruf, entfällt seine Zeile):
    "rescan"()               Bibliothek neu einlesen (kehrt sofort zurück)
    "steam_account"()        -> {"name": "…", "logged_in": bool} oder None
    "power"(id)              wie power.do; hier nur "steam"
    "emudeck_info"()         -> {"path": "…", "available": bool, "systems": [{"id", "label", "count"}]}
    "launch"(entry_id)       App starten; hier nur "app:emudeck"
    "has_entry"(entry_id)    -> bool: Gibt es diesen Eintrag in der Bibliothek? Ohne Eintrag
                             "app:emudeck" entfällt die Zeile „EmuDeck öffnen“ (E20)
    "compat_tools_dirs"      Liste von Ordnern statt ~/.local/share/Steam/compatibilitytools.d
    "itch_status"()          -> {"configured": bool, "loggedIn": bool, "name": "…"} oder None (nicht
                             lesbar); ohne ihn entfällt die Seite itch.io (E100)
    "itch_logout"()          -> {"ok": bool, "message": "…"}: abmelden (Netz, blockiert)
    "itch_set_client"(cid)   -> {"ok": bool, "message": "…"}: App-Kennung übernehmen (geprüft hier)

Für den Starter (launch/proton.py): resolve_proton(config.get("games.proton", "")) liefert den
Build, den "Automatisch" meint.

Spielstände (E99): „Spielstände automatisch sichern“ (saves.auto) und wie viele automatische
Sicherungen je Spiel bleiben (saves.keep); was sie bewirken, steht in wiring_saves.py.

itch.io (E100): Unterseite mit dem Konto („Angemeldet als …“ / „Nicht angemeldet“ / „Nicht
eingerichtet“), „Bei itch.io abmelden“ (Rückfrage, nur angemeldet) und der App-Kennung (Textfeld,
nicht geheim). Einziger Schlüssel ist store.itch_client_id (E22); geschrieben wird er nur über
itch_set_client, nie hier direkt.
"""
from __future__ import annotations

import logging
import os
import re

from .rows import (AREA_ORDER, Page, SettingsError, action, confirm_action, info, link, option,
                   select, text, toggle)

log = logging.getLogger("couch.settings.games")

AREA_ID = "games"
PAGE_STEAM = "games.steam"
PAGE_EMULATORS = "games.emulators"
PAGE_ITCH = "games.itch"

COMPAT_TOOLS_DIR = "~/.local/share/Steam/compatibilitytools.d"
FSR4_MARKER = b"PROTON_FSR4_UPGRADE"      # steht im Startskript der Builds, die FSR 4 hochstufen

DEFAULTS = {
    "games.proton": "",            # Ordnername des Builds; leer = automatisch (resolve_proton)
    "games.fsr4": True,
    "games.hdr": True,
    "games.freeze": True,
    "games.pad_auto": True,
}

# Spielstände (E99): eigene Tabelle, weil die Schlüssel nicht mit "games." beginnen
SAVES_DEFAULTS = {
    "saves.auto": True,
    "saves.keep": 5,
}
KEEP_CHOICES = (5, 10, 20)
ROW_SAVES_AUTO = "games.saves.auto"     # Zeilen-IDs des Bereichs beginnen mit "games."
ROW_SAVES_KEEP = "games.saves.keep"

# Schalter: Zeile -> (Schlüssel, Beschriftung, Erklärung)
_TOGGLES = {
    "games.fsr4": (
        "games.fsr4", "FSR 4",
        "Wirkt bei Spielen mit FSR 3.1 oder neuer. Im Spiel muss FSR in den Grafikoptionen "
        "gewählt sein."),
    "games.hdr": (
        "games.hdr", "HDR in Spielen",
        "Spiele dürfen HDR ausgeben, solange HDR unter „Bildschirm und Video“ eingeschaltet ist."),
    "games.freeze": (
        "games.freeze", "Spiele im Hintergrund anhalten",
        "Hält das Spiel an, solange das Home oder das Control Center offen ist. Online-Spiele "
        "können dabei die Verbindung verlieren."),
    "games.pad_auto": (
        "games.pad_auto", "Controller automatisch anpassen",
        "Spiele, die nur Xbox-Controller kennen, sehen deinen Controller als Xbox-Controller (mit "
        "Xbox-Symbolen); Spiele mit eigener Unterstützung bekommen ihn direkt. Je Spiel einstellbar "
        "unter Spieleinstellungen › Controller."),
}
# „Bilder automatisch laden“ (library.fetch_art) steht unter Home-Bildschirm: jede Zeile genau einmal (E22)

# Emulatoren (E105, Teil P6)
ROW_EMU_ROMS = "games.emulators.roms"
ROW_EMU_ROOT = "games.emulators.root"
ROW_EMU_APP = "games.emulators.app:"

# itch.io (E100)
ROW_ITCH_ACCOUNT = "games.itch.account"
ROW_ITCH_LOGOUT = "games.itch.logout"
ROW_ITCH_CLIENT = "games.itch.client_id"
ITCH_CLIENT_KEY = "store.itch_client_id"
ITCH_APP_NAME = "PC5 Launcher"
ITCH_REDIRECT = "http://127.0.0.1:34711/itch-callback"      # = store/itch_account.REDIRECT (Test hält beide gleich)
ITCH_BAD_CLIENT = "Die App-Kennung hat 32 Zeichen aus 0–9 und a–f."
_HEX32 = re.compile(r"[0-9a-f]{32}")


def _natural(name: str) -> list:
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", name)]


def pretty_name(folder: str, display_name: str = "") -> str:
    """Lesbarer Name eines Builds: "Proton-CachyOS 11.0 (2026-07-03)"."""
    if display_name and display_name != folder:
        return display_name
    m = re.match(r"proton-cachyos-(\d+(?:\.\d+)*)-(\d{4})(\d{2})(\d{2})", folder, re.I)
    if m:
        return "Proton-CachyOS %s (%s-%s-%s)" % m.groups()
    # Seit GE-Proton11-4 trägt der Ordner die Bauart im Namen (GE-Proton11-11-x86_64, E101)
    m = re.match(r"GE-Proton(\d+)-(\d+)(?:-x86_64)?$", folder)
    if m:
        return "GE-Proton %s-%s" % m.groups()
    return folder


def _rank(build: dict) -> tuple:
    """Reihenfolge für „Automatisch“ (E102): FSR 4 zuerst, darin die Familie in der Reihenfolge des Katalogs
    (Proton-CachyOS vorn, Unbekanntes zuletzt), darin die neueste. Nach dem Ordnernamen allein lag
    proton-EM-10.0-30 vor proton-cachyos-11.0-…: Ein Klick auf „Installieren“ hätte alle Spiele ohne eigene
    Auswahl auf das experimentelle Proton-EM gestellt."""
    from .. import proton_repair as pr
    order = {c["family"]: i for i, c in enumerate(pr.CATALOG)}
    return (not build["fsr4"], order.get(pr.family_of(build["id"]), len(order)),
            tuple(-n for n in pr.version_key(build["id"])))


def list_proton_builds(dirs: list | None = None) -> list[dict]:
    """Proton-Builds = Unterordner mit einer Datei "proton". Blockierend (liest die Startskripte).

    -> [{"id": Ordnername, "label", "path", "fsr4": bool}], zuerst die mit FSR 4, darin Proton-CachyOS vor
    den anderen Familien (_rank), darin die neueste.
    """
    found: dict[str, dict] = {}
    for base in (dirs if dirs is not None else [COMPAT_TOOLS_DIR]):
        base = os.path.expanduser(str(base))
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            script = os.path.join(path, "proton")
            # versteckt: Zwischenordner und Sicherungen von proton_repair (.couch-proton-*, E101)
            if name in found or name.startswith(".") or not os.path.isfile(script):
                continue
            fsr4, display = False, ""
            try:
                with open(script, "rb") as f:
                    fsr4 = FSR4_MARKER in f.read(4 << 20)
            except OSError:
                pass
            try:
                with open(os.path.join(path, "compatibilitytool.vdf"), encoding="utf-8", errors="replace") as f:
                    m = re.search(r'"display_name"\s+"([^"]+)"', f.read(65536))
                    display = m.group(1) if m else ""
            except OSError:
                pass
            found[name] = {"id": name, "label": pretty_name(name, display), "path": path, "fsr4": fsr4}
    builds = sorted(found.values(), key=lambda b: _natural(b["id"]), reverse=True)
    return sorted(builds, key=_rank)                        # stabil: bei Gleichstand der Ordnername


def resolve_proton(wanted: str, builds: list | None = None, dirs: list | None = None) -> dict | None:
    """Der Build zu einer Einstellung: der genannte, sonst (leer oder fehlend) der erste der Liste."""
    builds = list_proton_builds(dirs) if builds is None else builds
    for b in builds:
        if b["id"] == wanted:
            return b
    return builds[0] if builds else None


def _plural(n: int) -> str:
    return "Keine Spiele" if n == 0 else ("1 Spiel" if n == 1 else "%d Spiele" % n)


class GamesArea:
    id = AREA_ID
    label = "Spiele"
    icon = "library"
    order = AREA_ORDER.get(AREA_ID, 50)

    def __init__(self, deps):
        self.deps = deps
        self.config = deps.config
        self.extra = getattr(deps, "extra", None) or {}
        self._dirs = self.extra.get("compat_tools_dirs")

    # --- Vertrag --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return ([AREA_ID, PAGE_STEAM] + ([PAGE_ITCH] if self._has_itch() else [])
                + ([PAGE_EMULATORS] if self._has_emulators() else []))

    def _has_emulators(self) -> bool:
        return callable(self.extra.get("emudeck_info")) or callable(self.extra.get("launch")) \
            or self._setup_part() is not None

    def _setup_part(self):
        """E105: der Teil „setup“ (wiring_setup.SetupPart), spät gebunden; None ohne ihn."""
        late = self.extra.get("late")
        if not callable(late):
            return None
        try:
            return late("setup_part")
        except Exception:  # noqa: BLE001
            log.exception("setup_part")
            return None

    def _has_itch(self) -> bool:
        return callable(self.extra.get("itch_status"))

    def page(self, page_id: str) -> Page:
        """Blockierend: liest den Ordner der Proton-Builds bzw. ruft die Rückrufe des Kerns."""
        if page_id == AREA_ID:
            return Page(AREA_ID, self.label, self._main_rows())
        if page_id == PAGE_STEAM:
            return Page(PAGE_STEAM, "Steam", self._steam_rows())
        if page_id == PAGE_EMULATORS and self._has_emulators():
            return Page(PAGE_EMULATORS, "Emulatoren", self._emulator_rows())
        if page_id == PAGE_ITCH and self._has_itch():
            return Page(PAGE_ITCH, "itch.io", self._itch_rows())
        raise KeyError(page_id)

    def set(self, row_id: str, value):
        if row_id in _TOGGLES:
            self.config.set(_TOGGLES[row_id][0], bool(value))
            return None
        if row_id == ROW_SAVES_AUTO:
            self.config.set("saves.auto", bool(value))
            return None
        if row_id == ROW_SAVES_KEEP:
            try:
                keep = int(value)
            except (TypeError, ValueError):
                keep = 0
            if keep not in KEEP_CHOICES:
                raise SettingsError("So viele Sicherungen lassen sich nicht wählen.")
            self.config.set("saves.keep", keep)
            return None
        if row_id == "games.proton":
            value = "" if value is None else str(value)
            if value and not any(b["id"] == value for b in self._builds()):
                raise SettingsError("Diesen Proton-Build gibt es nicht mehr.")
            self.config.set("games.proton", value)
            return None
        if row_id == ROW_ITCH_CLIENT:
            return self._set_itch_client(value)
        if row_id == ROW_EMU_ROMS:
            self.config.set("library.roms", bool(value))
            if callable(self.extra.get("rescan")):
                self._call("rescan")
            return {"message": "Die Bibliothek wird neu eingelesen.", "refresh": True}
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def action(self, row_id: str, value=None):
        if row_id == "games.rescan":
            self._call("rescan")
            return {"message": "Die Bibliothek wird neu eingelesen."}
        if row_id == "games.steam.open":
            self._call("power", "steam")
            return None
        if row_id == "games.emulators.open":
            self._call("launch", "app:emudeck")
            return None
        if row_id.startswith(ROW_EMU_APP):
            emu_id = row_id[len(ROW_EMU_APP):]
            data = self._emu_data()
            if not any(e.get("id") == emu_id for e in data.get("emulators") or []):
                raise SettingsError("Dieser Emulator ist nicht installiert.")
            self._call("launch", "app:emu-" + emu_id)
            return None
        if row_id == ROW_ITCH_LOGOUT:
            return self._itch_logout()
        raise SettingsError("Diese Aktion gibt es nicht.")

    # --- Zeilen ---------------------------------------------------------------------------

    def _cfg(self, key: str):
        return self.config.get(key, DEFAULTS[key])

    def _builds(self) -> list[dict]:
        return list_proton_builds(self._dirs)

    def _main_rows(self) -> list:
        builds = self._builds()
        wanted = self._cfg("games.proton") or ""
        auto = resolve_proton("", builds)
        known = any(b["id"] == wanted for b in builds)
        hint = "Gilt für alle Spiele ohne eigene Auswahl."
        if wanted and not known:
            hint = "Der gewählte Build „%s“ fehlt. Bis du neu wählst, gilt „Automatisch“." % wanted
        rows = [select(
            "games.proton", "Standard-Proton", wanted if known else "",
            [option("", "Automatisch", auto["label"] if auto else "")]
            + [option(b["id"], b["label"], "FSR 4" if b["fsr4"] else "") for b in builds],
            enabled=bool(builds), note="" if builds else "Kein Proton gefunden", hint=hint,
            default=DEFAULTS["games.proton"])]
        for row_id, (key, label, text) in _TOGGLES.items():
            rows.append(toggle(row_id, label, self._cfg(key), hint=text, default=DEFAULTS[key]))
        rows.extend(self._saves_rows())
        if callable(self.extra.get("rescan")):
            rows.append(action("games.rescan", "Bibliothek neu einlesen"))
        rows.append(link(PAGE_STEAM, "Steam", PAGE_STEAM))
        if self._has_itch():
            rows.append(link(PAGE_ITCH, "itch.io", PAGE_ITCH))
        if self._has_emulators():
            rows.append(link(PAGE_EMULATORS, "Emulatoren", PAGE_EMULATORS))
        return rows

    def _saves_rows(self) -> list:
        keep = self.config.get("saves.keep", SAVES_DEFAULTS["saves.keep"])
        keep = keep if keep in KEEP_CHOICES else SAVES_DEFAULTS["saves.keep"]
        return [
            toggle(ROW_SAVES_AUTO, "Spielstände automatisch sichern",
                   bool(self.config.get("saves.auto", SAVES_DEFAULTS["saves.auto"])),
                   hint="Nach jedem Spielen ab etwa 2 Minuten packt der PC5 Launcher den Spielstand als "
                        "ZIP nach ~/Games/backups/couch, wenn er sich geändert hat. Eine Mitteilung "
                        "kommt nur, wenn das nicht klappt.",
                   default=SAVES_DEFAULTS["saves.auto"]),
            select(ROW_SAVES_KEEP, "Automatische Sicherungen behalten", keep,
                   [option(n, "Die letzten %d" % n) for n in KEEP_CHOICES],
                   hint="Je Spiel. Ältere automatische Sicherungen kommen in den Papierkorb; "
                        "die, die du selbst anlegst, bleiben.",
                   default=SAVES_DEFAULTS["saves.keep"]),
        ]

    def _steam_rows(self) -> list:
        rows = []
        if callable(self.extra.get("steam_account")):
            acc = self._call("steam_account")
            if isinstance(acc, str):
                acc = {"name": acc}
            acc = acc if isinstance(acc, dict) else {}
            name = acc.get("persona") or acc.get("name") or acc.get("account") or ""
            logged_in = acc.get("logged_in", bool(name))
            rows.append(info("games.steam.account", "Konto", name or "Nicht angemeldet",
                             note="" if logged_in or not name else "Abgemeldet"))
        capable = [b["label"] for b in self._builds() if b["fsr4"]]
        rows.append(info(
            "games.steam.fsr4", "FSR 4 in Steam-Spielen",
            ", ".join(capable) if capable else "Kein passendes Kompatibilitätswerkzeug gefunden",
            hint="Steam-Spiele startet Steam selbst. FSR 4 wirkt dort nur mit diesem "
                 "Kompatibilitätswerkzeug: In Steam unter Einstellungen › Kompatibilität als "
                 "Standard wählen oder beim Spiel unter Eigenschaften › Kompatibilität."))
        if callable(self.extra.get("power")):
            rows.append(confirm_action(
                "games.steam.open", "Steam Big Picture öffnen",
                title="Steam Big Picture öffnen?",
                text="Das Home wird beendet, laufende Spiele und Apps werden geschlossen. "
                     "Wenn du Steam verlässt, kommst du hierher zurück.",
                ok="Öffnen"))
        return rows

    # --- itch.io (E100) ----------------------------------------------------------------------

    def _itch_status(self) -> dict | None:
        """{"configured", "loggedIn", "name"} oder None (nicht lesbar: nie als „abgemeldet“ zeigen, E99)."""
        fn = self.extra.get("itch_status")
        if not callable(fn):
            return None
        try:
            st = fn()
        except Exception:  # noqa: BLE001 - nur Anzeige
            log.exception("itch_status() wirft")
            return None
        if not isinstance(st, dict):
            return None
        return {"configured": bool(st.get("configured")),
                "loggedIn": bool(st.get("loggedIn", st.get("logged_in", False))),
                "name": str(st.get("name") or "")}

    def _itch_client(self) -> str:
        cid = self.config.get(ITCH_CLIENT_KEY, "")
        cid = cid.strip().lower() if isinstance(cid, str) else ""
        return cid if _HEX32.fullmatch(cid) else ""

    def _itch_rows(self) -> list:
        st = self._itch_status()
        cid = self._itch_client()
        if st is None:
            value, hint = "Gerade nicht lesbar", ""
        elif st["loggedIn"]:
            value = "Angemeldet als %s" % st["name"] if st["name"] else "Angemeldet"
            hint = "Deine itch.io-Spiele stehen im Store unter „Deine Spiele“."
        elif st["configured"] or cid:
            value, hint = "Nicht angemeldet", "Anmelden geht im Store unter itch.io."
        else:
            value = "Nicht eingerichtet"
            hint = ("Im Store unter itch.io › „Anmelden“ legst du einmal eine eigene itch.io-App an; "
                    "der Browser liest ihre Kennung dann selbst.")
        rows = [info(ROW_ITCH_ACCOUNT, "itch.io-Konto", value, hint=hint)]
        if st is not None and st["loggedIn"] and callable(self.extra.get("itch_logout")):
            rows.append(confirm_action(
                ROW_ITCH_LOGOUT, "Bei itch.io abmelden",
                title="Bei itch.io abmelden?",
                text="Installierte itch.io-Spiele bleiben und starten weiter. Zum Laden und für "
                     "„Deine Spiele“ meldest du dich im Store wieder an.",
                ok="Abmelden"))
        if callable(self.extra.get("itch_set_client")):
            rows.append(text(
                ROW_ITCH_CLIENT, "itch.io-App-Kennung", cid, note="" if cid else "Fehlt",
                hint="Öffentliche Kennung deiner itch.io-App „%s“ mit der Redirect-URL %s: 32 Zeichen "
                     "aus 0–9 und a–f, kein Geheimnis. Beim Einrichten im Store liest der Browser sie "
                     "selbst; hier tippst du sie nur ein, falls das nicht klappt." % (ITCH_APP_NAME, ITCH_REDIRECT),
                learn=False))                   # E100: eine Kennung ist kein Wort für die Vorschläge
        return rows

    def _set_itch_client(self, value) -> dict:
        cid = re.sub(r"\s+", "", value).lower() if isinstance(value, str) else ""
        if not _HEX32.fullmatch(cid):
            raise SettingsError(ITCH_BAD_CLIENT)
        out = self._call("itch_set_client", cid)
        out = out if isinstance(out, dict) else {}
        if out.get("ok") is False:
            raise SettingsError(str(out.get("message") or "Die App-Kennung ließ sich nicht übernehmen."))
        return {"message": str(out.get("message") or "App-Kennung übernommen."), "refresh": True}

    def _itch_logout(self) -> dict:
        st = self._itch_status()
        if st is not None and not st["loggedIn"]:
            return {"message": "Du bist bei itch.io nicht angemeldet.", "refresh": True}
        out = self._call("itch_logout")
        out = out if isinstance(out, dict) else {}
        if out.get("ok") is False:
            raise SettingsError(str(out.get("message") or "Das Abmelden bei itch.io hat nicht geklappt."))
        return {"message": str(out.get("message") or "Du bist bei itch.io abgemeldet."), "refresh": True}

    def _emu_data(self) -> dict:
        part = self._setup_part()
        fn = getattr(part, "emulator_rows", None)
        if not callable(fn):
            return {}
        try:
            data = fn()
        except Exception:  # noqa: BLE001  (nur Anzeige)
            log.exception("emulator_rows()")
            return {}
        return data if isinstance(data, dict) else {}

    def _emulator_rows(self) -> list:
        """E105: Schalter „ROM-Ordner einlesen“, der Emulator-Ordner, je installiertem Emulator „<Name> öffnen“;
        darunter wie bisher EmuDeck (Ordner, Systeme, „EmuDeck öffnen“)."""
        rows = []
        data = self._emu_data()
        if data:
            rows.append(toggle(ROW_EMU_ROMS, "ROM-Ordner einlesen", bool(data.get("roms", True)),
                               hint="Spiele aus den Ordnern roms/<System> im Emulator-Ordner erscheinen in der "
                                    "Bibliothek, auch ohne EmuDeck. Switch und Wii U kommen weiter über EmuDeck, "
                                    "wenn es eingerichtet ist.", default=True))
            root = str(data.get("root") or "")
            note = "" if not root or data.get("available") else "Nicht erreichbar"
            hint = "Erkannt (EmuDeck)." if data.get("emudeck") else ("Erkannt." if data.get("auto") else
                                                                      "Gewählt in der Einrichtung.")
            rows.append(info(ROW_EMU_ROOT, "Emulator-Ordner", root or "Nicht festgelegt", note=note,
                             hint=hint + " Darin liegen roms/ und bios/. Ändern kannst du ihn unter Einstellungen › "
                                         "PC5 Launcher › Einrichtung."))
            for emu in data.get("emulators") or []:
                if not isinstance(emu, dict) or not emu.get("id"):
                    continue
                if callable(self.extra.get("launch")):
                    rows.append(action(ROW_EMU_APP + str(emu["id"]), "%s öffnen" % emu.get("title", emu["id"]),
                                       hint="Für Kerne, BIOS-Dateien und Einstellungen. Der Controller ist dort die "
                                            "Maus."))
        if callable(self.extra.get("emudeck_info")):
            data = self._call("emudeck_info")
            data = data if isinstance(data, dict) else {}
            path = data.get("path") or ""
            available = bool(data.get("available", bool(path)))
            rows.append(info("games.emulators.folder", "EmuDeck-Ordner", path or "Nicht gefunden",
                             note="" if available or not path else "Nicht eingehängt"))
            systems = data.get("systems") or []
            if isinstance(systems, dict):
                systems = [{"id": k, "label": k, "count": v} for k, v in systems.items()]
            for s in systems:
                sid = str(s.get("id") or s.get("label") or "")
                if not sid:
                    continue
                try:
                    count = int(s.get("count") or 0)
                except (TypeError, ValueError):
                    count = 0
                rows.append(info("games.emulators.system:" + sid, str(s.get("label") or sid), _plural(count)))
        if callable(self.extra.get("launch")) and self._has_entry("app:emudeck"):
            rows.append(action("games.emulators.open", "EmuDeck öffnen"))
        if not rows:
            rows.append(info("games.emulators.none", "Keine Emulatoren eingerichtet.", enabled=False))
        return rows

    def _has_entry(self, entry_id: str) -> bool:
        fn = self.extra.get("has_entry")
        if not callable(fn):
            return True                       # der Kern sagt nichts dazu: Zeile zeigen
        try:
            return bool(fn(entry_id))
        except Exception:  # noqa: BLE001
            log.exception("has_entry() wirft")
            return False

    # --- intern ---------------------------------------------------------------------------

    def _call(self, name: str, *args):
        fn = self.extra.get(name)
        if not callable(fn):
            raise SettingsError("Das ist gerade nicht verfügbar.")
        try:
            return fn(*args)
        except SettingsError:
            raise
        except Exception as e:  # noqa: BLE001
            # LaunchError und ähnliche tragen einen deutschen Satz in .message
            message = getattr(e, "message", None)
            if isinstance(message, str) and message:
                raise SettingsError(message) from e
            raise


def make_area(deps) -> GamesArea:
    return GamesArea(deps)
