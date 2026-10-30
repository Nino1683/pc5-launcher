"""Einstellungen › Apps (E141, E143): ausgeblendete Apps zurückholen, Claude Code mit dem PC5 Launcher starten, Discord.

Wunsch 2026-10-30: „Ja“ auf „Soll man Apps ausblenden können? … Weg zurück, z. B. „Ausgeblendete Apps“ in den
Einstellungen“ und „Baue einen switch ein claude code nach einem neustart mitstarten und die letzte Unterhaltung
laden“. Ein eigener Bereich wie der Reiter „Apps“ im Home (E140): Beides betrifft Apps, nicht Spiele und nicht das
Aussehen des Home. Er steht unter „Updates“, damit „Updates“ wie in E133 direkt unter „Spiele“ bleibt.
E143 (Wunsch 2026-10-30, „Ja“ auf „Sollen Discord und Claude Code ganz in den Bereich „Apps“ ziehen?“ und „Soll das
Einschalten des Claude-Schalters Claude Code gleich starten?“): Discord zieht von Spiele › Discord hierher (Seiten-
und Zeilenkennungen jetzt apps.discord.*), und das Einschalten startet Claude Code sofort, wenn es nicht schon läuft.

  apps                Hauptseite: „Ausgeblendete Apps“ (Zahl als Zustand), „Claude Code mit dem PC5 Launcher starten“
                      (claude.autostart, Vorgabe aus; nur, wenn es die Kachel Claude Code gibt), „Discord“ (An/Aus)
  apps.hidden         je ausgeblendeter App eine Zeile mit „Wieder anzeigen“ (Unterzeile: ihr Reiter im Home); ohne
                      ausgeblendete eine Hinweiszeile
  apps.discord        (E139) nur mit dem Teil „discord“ (deps.extra["late"]("discord_part"), wiring_discord.DiscordPart:
                      status(), open()). Schalter „Discord verwenden“ (discord.enabled, Vorgabe aus), darunter nur
                      eingeschaltet: Zustand, „Discord installieren“ (öffnet den Schritt Apps der Einrichtung mit nur
                      Discord, derselbe Flatpak-Weg), „Discord öffnen“, Hintergrund-Start, Mitteilungen, „Spielt gerade“
                      mit Application ID und die Tasten, die der Nutzer in Discord belegt. Was die Schalter bewirken, macht der
                      Teil selbst (er beobachtet die Schlüssel discord.*).

Was der Kern einsteckt (deps.extra, freiwillig):
    "late"("apps_part")  wiring_apps.AppsPart: hidden_apps() -> [{id, title, row}], show_app(id) -> Titel
                         (wirft SettingsError bzw. KeyError, wenn es die App nicht mehr gibt), start_claude() ->
                         „started“ | „running“ | „missing“ (E143)
    "late"("discord_part") wiring_discord.DiscordPart (E139)
    "has_entry"(id)      Gibt es die Kachel? Ohne "app:claude" entfällt der Schalter

Ausblenden selbst geschieht im Optionsmenü jeder Kachel unter „Apps“ und „Medien“ (lib.hide bzw. home.setHidden für
„Dateien“ und die Mediengalerie). Ausgeblendet heißt nur: keine Kachel im Home und keine Karte „zuletzt geöffnet“ im
Control Center; Installation, Daten und alle übrigen Wege (Einstellungen, Control Center, eine laufende App) bleiben.
"""
from __future__ import annotations

import logging

from .rows import AREA_ORDER, Page, SettingsError, action, info, late_part, link, message_of, text, toggle

log = logging.getLogger("couch.settings.apps")

AREA_ID = "apps"
PAGE_HIDDEN = "apps.hidden"
ROW_SHOW = "apps.hidden.show:"
ROW_CLAUDE = "apps.claude.autostart"
CLAUDE_ID = "app:claude"
PAGE_DISCORD = "apps.discord"

# Discord (E139): Zeile -> Schlüssel (Vorgaben in couch.discord.DEFAULTS)
DISCORD_TOGGLES = {
    "apps.discord.enabled": "discord.enabled",
    "apps.discord.autostart": "discord.autostart",
    "apps.discord.notify": "discord.notify",
    "apps.discord.presence": "discord.presence",
}
ROW_DISCORD_APP_ID = "apps.discord.app_id"
ROW_DISCORD_INSTALL = "apps.discord.install"
ROW_DISCORD_OPEN = "apps.discord.open"
DISCORD_BAD_ID = "Die Application ID besteht aus 17 bis 20 Ziffern."
DISCORD_PORTAL = "discord.com/developers/applications"

DEFAULTS = {
    "claude.autostart": False,     # E141: Claude Code nach dem Start im Hintergrund, Startart „continue“
}
ROW_LABELS = {"apps": "Apps", "media": "Medien", "games": "Spiele"}
CLAUDE_HINT = ("Nach jedem Start des PC5 Launchers, auch nach einem Neustart des Rechners, startet Claude Code im "
               "Hintergrund und lädt die letzte Unterhaltung wie „Weitermachen“. Beim Einschalten startet es gleich. "
               "Die Kachel Claude Code oder ihre Karte im Control Center holt es nach vorn.")
CLAUDE_STARTED = "Claude Code startet im Hintergrund und lädt die letzte Unterhaltung."
CLAUDE_RUNNING = "Claude Code läuft schon. Ab dem nächsten Start startet es mit."
HIDDEN_HINT = "Apps, die du im Optionsmenü einer Kachel unter Apps oder Medien ausgeblendet hast. Hier holst du sie zurück."
NONE_HINT = "Ausblenden geht im Optionsmenü (Options-Taste) jeder Kachel unter Apps und Medien."


class AppsArea:
    id = AREA_ID
    label = "Apps"
    icon = "apps"
    order = AREA_ORDER.get(AREA_ID, 57)

    def __init__(self, deps):
        self.deps = deps
        self.config = deps.config
        self.extra = getattr(deps, "extra", None) or {}

    # --- Vertrag --------------------------------------------------------------------------

    def pages(self) -> list[str]:
        return [AREA_ID, PAGE_HIDDEN] + ([PAGE_DISCORD] if self._discord_part() is not None else [])

    def page(self, page_id: str) -> Page:
        if page_id == AREA_ID:
            return Page(AREA_ID, self.label, self._main_rows())
        if page_id == PAGE_HIDDEN:
            return Page(PAGE_HIDDEN, "Ausgeblendete Apps", self._hidden_rows())
        if page_id == PAGE_DISCORD and self._discord_part() is not None:
            return Page(PAGE_DISCORD, "Discord", self._discord_rows())
        raise KeyError(page_id)

    def set(self, row_id: str, value):
        if row_id == ROW_CLAUDE:
            if not self._has(CLAUDE_ID):
                raise SettingsError("Claude Code ist nicht installiert.")
            self.config.set("claude.autostart", bool(value))
            return self._start_claude() if value else None
        if row_id in DISCORD_TOGGLES or row_id == ROW_DISCORD_APP_ID:
            return self._set_discord(row_id, value)
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def action(self, row_id: str, value=None):
        if row_id.startswith(ROW_SHOW):
            return self._show(row_id[len(ROW_SHOW):])
        if row_id in (ROW_DISCORD_INSTALL, ROW_DISCORD_OPEN):
            return self._discord_action(row_id)
        raise SettingsError("Diese Aktion gibt es nicht.")

    # --- Zeilen ---------------------------------------------------------------------------

    def _part(self):
        """E141: der Teil „apps“ (wiring_apps.AppsPart), spät gebunden; None ohne ihn."""
        return late_part(self.extra, "apps_part", log)

    def _discord_part(self):
        """E139/E143: der Teil „discord“ (wiring_discord.DiscordPart), spät gebunden; None ohne ihn."""
        return late_part(self.extra, "discord_part", log)

    def _has(self, entry_id: str) -> bool:
        fn = self.extra.get("has_entry")
        if not callable(fn):
            return False
        try:
            return bool(fn(entry_id))
        except Exception:  # noqa: BLE001
            log.exception("has_entry")
            return False

    def _start_claude(self) -> dict | None:
        """E143: Einschalten startet Claude Code gleich (wie nach einem Start des PC5 Launchers), wenn es nicht läuft."""
        part = self._part()
        fn = getattr(part, "start_claude", None)
        if not callable(fn):
            return None
        try:
            how = fn()
        except Exception:  # noqa: BLE001 (der Schalter gilt trotzdem ab dem nächsten Start)
            log.exception("start_claude()")
            return None
        if how == "started":
            return {"message": CLAUDE_STARTED}
        if how == "running":
            return {"message": CLAUDE_RUNNING}
        return None

    def _hidden(self) -> list[dict]:
        part = self._part()
        fn = getattr(part, "hidden_apps", None)
        if not callable(fn):
            return []
        try:
            items = fn() or []
        except Exception:  # noqa: BLE001 (nur Anzeige)
            log.exception("hidden_apps()")
            return []
        return [i for i in items if isinstance(i, dict) and i.get("id")]

    def _main_rows(self) -> list:
        hidden = self._hidden()
        rows = [link(PAGE_HIDDEN, "Ausgeblendete Apps", PAGE_HIDDEN, note=str(len(hidden)) if hidden else "Keine",
                     hint=HIDDEN_HINT)]
        if self._has(CLAUDE_ID):
            rows.append(toggle(ROW_CLAUDE, "Claude Code mit dem PC5 Launcher starten",
                               self.config.get("claude.autostart", DEFAULTS["claude.autostart"]) is True,
                               default=DEFAULTS["claude.autostart"], hint=CLAUDE_HINT))
        if self._discord_part() is not None:
            from .. import discord
            rows.append(link(PAGE_DISCORD, "Discord", PAGE_DISCORD, note="An" if discord.enabled(self.config) else "Aus"))
        return rows

    def _hidden_rows(self) -> list:
        items = self._hidden()
        if not items:
            return [info("apps.hidden.none", "Keine Apps ausgeblendet.", enabled=False, hint=NONE_HINT)]
        return [action(ROW_SHOW + str(i["id"]), str(i.get("title") or i["id"]), value="Wieder anzeigen",
                       note=ROW_LABELS.get(str(i.get("row") or ""), ""))
                for i in items]

    def _show(self, entry_id: str) -> dict:
        part = self._part()
        fn = getattr(part, "show_app", None)
        if not callable(fn):
            raise SettingsError("Das ist gerade nicht verfügbar.")
        try:
            title = fn(entry_id)
        except SettingsError:
            raise
        except KeyError:
            raise SettingsError("Diese App gibt es nicht mehr.") from None
        except Exception as e:  # noqa: BLE001
            raise SettingsError(message_of(e) or "Die App ließ sich nicht wieder anzeigen.") from e
        return {"message": "„%s“ wird wieder angezeigt." % (title or entry_id), "refresh": True}

    # --- Discord (E139) -------------------------------------------------------------------

    def _discord_status(self) -> dict:
        part = self._discord_part()
        try:
            st = part.status() if part is not None else None
        except Exception:  # noqa: BLE001 - nur Anzeige
            log.exception("discord_part.status()")
            st = None
        return st if isinstance(st, dict) else {}

    def _discord_rows(self) -> list:
        from .. import discord
        cfg = self.config
        on = discord.enabled(cfg)
        rows = [toggle("apps.discord.enabled", "Discord verwenden", on, default=False,
                       hint="Der offizielle Discord-Client läuft im PC5 Launcher: als Kachel unter Apps, mit einem "
                            "Symbol im Control Center und seinen Nachrichten als Mitteilung. Aus: Nichts davon läuft "
                            "oder erscheint; ein Discord, das der PC5 Launcher gestartet hat, wird sauber beendet.")]
        if not on:
            return rows
        st = self._discord_status()
        installed = bool(st.get("installed"))
        if not installed:
            value = "Nicht installiert"
        elif st.get("running"):
            value = "Läuft im Hintergrund" if st.get("background") and not st.get("front") else "Läuft"
        else:
            value = "Bereit"
        rows.append(info("apps.discord.status", "Discord", value))
        if not installed:
            size = st.get("size_mb")
            rows.append(action(ROW_DISCORD_INSTALL, "Discord installieren",
                               hint="Von Flathub in deine Nutzer-Installation, wie die Apps der Einrichtung%s. Du "
                                    "meldest dich danach in Discord selbst an." % (
                                        " (rund %d MB)" % size if isinstance(size, int) and size > 0 else "")))
        else:
            rows.append(action(ROW_DISCORD_OPEN, "Discord öffnen",
                               hint="Holt Discord nach vorn. Der Controller ist dort die Maus; mit der PS-Taste "
                                    "kommst du zurück, Discord läuft weiter."))
        rows.append(toggle("apps.discord.autostart", "Mit der Sitzung im Hintergrund starten",
                           discord.flag(cfg, "discord.autostart"), default=True,
                           hint="Discord startet nach dem PC5 Launcher unsichtbar und bleibt für Nachrichten und "
                                "Sprachchat an, auch während ein Spiel läuft. Es wird nie angehalten."))
        notes = str(st.get("notes") or "")
        rows.append(toggle("apps.discord.notify", "Nachrichten als Mitteilung",
                           discord.flag(cfg, "discord.notify"), default=True,
                           note="Nicht verfügbar" if notes == "failed" else "",
                           hint="Was Discord als Mitteilung meldet, erscheint oben rechts wie jede Mitteilung des PC5 "
                                "Launchers (Einstellungen › Mitteilungen gilt). Der PC5 Launcher liest dafür nur mit."))
        app_id = discord.app_id_of(cfg)
        rows.append(toggle("apps.discord.presence", "Spielt gerade anzeigen",
                           discord.flag(cfg, "discord.presence"), default=True,
                           note="" if app_id else "Ohne Application ID",
                           hint="Deine Freunde sehen in Discord, welches Spiel du gerade spielst und seit wann. "
                                "Braucht die Application ID darunter."))
        rows.append(text(ROW_DISCORD_APP_ID, "Application ID", app_id, note="" if app_id else "Fehlt", learn=False,
                         hint="Lege im Discord Developer Portal (%s) kostenlos eine Application „PC5 Launcher“ an und "
                              "trage ihre Application ID ein: 17 bis 20 Ziffern, kein Geheimnis. Leer: keine "
                              "Anzeige." % DISCORD_PORTAL))
        # Die Tasten je Zeile (= discord.KEY_LABELS, Test); drei kurze Zeilen statt eines Absatzes, den die Seite nach
        # drei Zeilen abschneidet
        rows.append(info("apps.discord.keys.mute", "„Mikrofon ein-/ausschalten“", discord.KEY_LABELS["mute"],
                         hint="In Discord unter Einstellungen › System › Benutzerdefinierte Hotkeys › „Hotkey "
                              "hinzufügen“ eintragen. Das Control Center schickt sie mit „Mikrofon in Discord stumm/an“."))
        rows.append(info("apps.discord.keys.deafen", "„Ein- und Ausgabe (de)aktivieren“", discord.KEY_LABELS["deafen"],
                         hint="Ebenso in Discord eintragen. Das Control Center schickt sie mit „Ton in Discord aus/an“, "
                              "solange ein Spiel oder eine App vorn ist."))
        rows.append(info("apps.discord.keys.howto", "Tasten ohne Tastatur belegen", "",
                         hint="In Discord die Aufnahme der Tastenbelegung starten, PS drücken und im Control Center "
                              "unter „Discord“ die passende Zeile wählen."))
        return rows

    def _set_discord(self, row_id: str, value) -> dict:
        from .. import discord
        if row_id == ROW_DISCORD_APP_ID:
            if value is not None and not isinstance(value, str):
                raise SettingsError(DISCORD_BAD_ID)
            try:
                cid = discord.clean_app_id(value or "")
            except ValueError:
                raise SettingsError(DISCORD_BAD_ID) from None
            self.config.set("discord.app_id", cid)
            return {"message": "Application ID übernommen." if cid else "Application ID entfernt.", "refresh": True}
        on = bool(value)
        if row_id != "apps.discord.enabled" and not discord.enabled(self.config):
            raise SettingsError("Schalte zuerst „Discord verwenden“ ein.")
        before = self._discord_status() if row_id == "apps.discord.enabled" and not on else {}
        self.config.set(DISCORD_TOGGLES[row_id], on)
        if row_id == "apps.discord.enabled":
            if not on and before.get("running") and before.get("own", True):
                return {"message": "Discord wird beendet.", "refresh": True}
            if on and not self._discord_status().get("installed"):
                return {"message": "Discord ist noch nicht installiert. „Discord installieren“ lädt es von Flathub.",
                        "refresh": True}
        return {"refresh": True}

    def _discord_action(self, row_id: str) -> dict:
        from .. import discord
        if not discord.enabled(self.config):
            raise SettingsError("Schalte zuerst „Discord verwenden“ ein.")
        st = self._discord_status()
        if row_id == ROW_DISCORD_INSTALL:
            if st.get("installed"):
                return {"message": "Discord ist schon installiert.", "refresh": True}
            # derselbe Weg wie „Installieren“ auf einer fehlenden App-Kachel (E105): Schritt Apps mit nur Discord
            return {"ui": "setup-apps", "params": {"from": "settings", "only": "discord"}}
        part = self._discord_part()
        if part is None or not st.get("installed"):
            raise SettingsError("Discord ist nicht installiert.")
        try:
            part.open()
        except SettingsError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SettingsError(message_of(e) or "Discord lässt sich gerade nicht öffnen.") from e
        return {}


def make_area(deps) -> AppsArea:
    return AppsArea(deps)
