"""Einstellungen › Bildschirm und Video › Fernseher steuern (E142).

Kein eigener Bereich (eine neue Kategorie verschöbe die abgezählten Fotos, E141): Der Bereich display zeigt am Ende
seiner Seite die Zeile „Fernseher steuern“ und leitet alle Seiten und Zeilen unter display.tvctl hierher.

  display.tvctl          „Fernseher übers Netz steuern“ (tv.enabled, Vorgabe aus); eingeschaltet: Fernseher,
                         „Fernseher koppeln“, gekoppelt dazu „Eingänge genauer erkennen (optional)“, Eingang des PCs,
                         „Beim Ruhemodus und Ausschalten Fernseher ausschalten“ (tv.off_with_pc), „Beim Aufwachen
                         Fernseher einschalten und auf den PC umschalten“ (tv.wake_with_pc), Netzwerk-Standby,
                         „Kopplung vergessen“
  display.tvctl.pair     Suche im Heimnetz (nur eingeschaltet), je Fund eine Zeile, „Erneut suchen“, „Adresse eingeben“
  display.tvctl.code     Code vom Fernseher eingeben, „Neuen Code anfordern“
  display.tvctl.inputs   ADB in drei Schritten erklärt, „Jetzt erkennen“, die gefundenen Eingänge

Was der Kern einsteckt: deps.extra["late"]("tv_part") -> wiring_tv.TvPart (status, discover, pick, pick_address,
submit_code, new_code, learn_inputs, forget; alle blockierend, Fehler als TvError mit Satz). Ohne den Teil gibt es die
Zeile nicht. Die Schalter wirken über die Schlüssel tv.* (der Teil beobachtet sie).
"""
from __future__ import annotations

import logging

from .. import tv
from ..tv import keys as tv_keys
from ..tv import net as tv_net
from .rows import (Page, SettingsError, action, confirm_action, info, late_part, link, message_of, option, select,
                   text, toggle)

log = logging.getLogger("couch.settings.tvctl")

PAGE = "display.tvctl"
PAGE_PAIR = "display.tvctl.pair"
PAGE_CODE = "display.tvctl.code"
PAGE_INPUTS = "display.tvctl.inputs"
PAGES = [PAGE, PAGE_PAIR, PAGE_CODE, PAGE_INPUTS]

ROW_ENABLED = "display.tvctl.enabled"
ROW_OFF = "display.tvctl.off"
ROW_WAKE = "display.tvctl.wake"
ROW_FORGET = "display.tvctl.forget"
ROW_PICK = "display.tvctl.pair.pick:"
ROW_SEARCH = "display.tvctl.pair.search"
ROW_ADDRESS = "display.tvctl.pair.address"
ROW_CODE = "display.tvctl.code.enter"
ROW_CODE_NEW = "display.tvctl.code.new"
ROW_READ = "display.tvctl.inputs.read"
KEYS = {ROW_ENABLED: "tv.enabled", ROW_OFF: "tv.off_with_pc", ROW_WAKE: "tv.wake_with_pc"}

OFF_LABELS = {"never": "Nie", "pc": "Nur wenn er den PC zeigt", "always": "Immer"}
HINT_ENABLED = ("Lautstärke, Eingang und Ausschalten des Fernsehers über das Heimnetz, im Control Center unter "
                "„Fernseher“. Geht nur mit Fernsehern mit Google TV oder Android TV. Aus: Der PC5 Launcher spricht nie "
                "mit dem Fernseher und sucht auch nicht nach ihm.")
HINT_PAIR = ("Der Fernseher muss an sein und im selben Heimnetz wie der PC. Kennt er diesen PC noch nicht, zeigt er "
             "einen Code, den du dann hier eingibst.")
HINT_INPUTS = ("Ohne das schaltet der PC5 Launcher mit den Tasten HDMI 1 bis 4, die manche Fernseher übergehen, und weiß "
               "nicht, welcher Eingang gerade läuft. Mit Netzwerk-Debugging am Fernseher kennt er die Eingänge samt den "
               "Namen der Geräte daran und schaltet zuverlässig um.")
HINT_OFF = ("„Nur wenn er den PC zeigt“ braucht „Eingänge genauer erkennen“. „Immer“ schaltet ihn auch aus, wenn gerade "
            "ein anderes Gerät läuft. Wartet höchstens drei Sekunden, der Ruhemodus kommt in jedem Fall.")
HINT_WAKE = ("Nach dem Ruhemodus und nach dem Einschalten des PCs (nicht bei jedem Neustart des PC5 Launchers): weckt den "
             "Fernseher über das Netz und schaltet auf den Eingang des PCs.")
HINT_STANDBY = ("Damit der PC5 Launcher den Fernseher im Standby erreicht und wecken kann, muss in dessen Einstellungen "
                "„Netzwerk-Standby“ an sein (je nach Hersteller auch „Wake on LAN“, „Über Netzwerk einschalten“ oder "
                "„Schnellstart“).")
HINT_FORGET = "Danach steuert der PC5 Launcher den Fernseher nicht mehr, bis du ihn neu koppelst."
HINT_ADDRESS = ("Wird der Fernseher nicht gefunden: seine IP-Adresse aus seinen Netzwerk-Einstellungen (vier Zahlen mit "
                "Punkten) oder sein Name im Netz.")
STEPS = [
    ("display.tvctl.inputs.step1", "1. Entwickleroptionen freischalten",
     "Am Fernseher unter Einstellungen › System › Info siebenmal auf „Build“ (Android-TV-Betriebssystem-Build) drücken."),
    ("display.tvctl.inputs.step2", "2. Netzwerk-Debugging einschalten",
     "In Einstellungen › System › Entwickleroptionen „Netzwerk-Debugging“ einschalten (je nach Gerät auch „ADB-Debugging“ "
     "oder „USB-Debugging“)."),
    ("display.tvctl.inputs.step3", "3. Am Fernseher bestätigen",
     "Dann hier „Jetzt erkennen“ wählen und am Fernseher „Debugging zulassen?“ bestätigen, am besten mit „Immer von "
     "diesem Computer zulassen“."),
]
HINT_READ = ("Der PC5 Launcher liest dann nur die Liste der Eingänge, die Namen der Geräte daran und welcher Eingang gerade "
             "läuft (der ist der des PCs), und schaltet die Eingänge um. Sonst nichts.")
TEXT_SWITCH_ON = "Schalte zuerst „Fernseher übers Netz steuern“ ein."
TEXT_UNAVAILABLE = "Das ist gerade nicht verfügbar."


def owns(ident: str) -> bool:
    return ident == PAGE or ident.startswith(PAGE + ".")


def _error(e: Exception) -> SettingsError:
    return SettingsError(message_of(e) or "Das hat nicht geklappt.")


def _call(fn):
    """Ein Aufruf des Teils (blockierend); jeder Fehler wird zum SettingsError mit dem Satz des TvError."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        raise _error(e) from None


class TvRows:
    def __init__(self, deps):
        self.deps = deps
        self.config = deps.config
        self.extra = getattr(deps, "extra", None) or {}

    def part(self):
        return late_part(self.extra, "tv_part", log)

    def _status(self) -> dict:
        part = self.part()
        try:
            return part.status() if part is not None else {}
        except Exception:  # noqa: BLE001 (nur Anzeige)
            log.exception("tv_part.status()")
            return {}

    # --- die Zeile auf der Seite „Bildschirm und Video“ -----------------------------------

    def link_row(self):
        if self.part() is None:
            return None
        st = self._status()
        if not st.get("available", True):
            note = "Nicht verfügbar"
        elif not tv.enabled(self.config):
            note = "Aus"
        else:
            note = "Gekoppelt" if st.get("paired") else "Nicht gekoppelt"
        return link(PAGE, "Fernseher steuern", PAGE, note=note,
                    hint="Lautstärke, Eingang und Ausschalten über das Heimnetz (Google TV und Android TV).")

    # --- Seiten ---------------------------------------------------------------------------

    def page(self, page_id: str) -> Page:
        if self.part() is None:
            raise KeyError(page_id)
        if page_id == PAGE:
            return Page(PAGE, "Fernseher steuern", self._main_rows())
        if page_id == PAGE_PAIR:
            return Page(PAGE_PAIR, "Fernseher koppeln", self._pair_rows())
        if page_id == PAGE_CODE:
            return Page(PAGE_CODE, "Code eingeben", self._code_rows())
        if page_id == PAGE_INPUTS:
            return Page(PAGE_INPUTS, "Eingänge genauer erkennen", self._input_rows())
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        st = self._status()
        on = tv.enabled(self.config)
        if not st.get("available", True):
            return [toggle(ROW_ENABLED, "Fernseher übers Netz steuern", False, enabled=False, note="Nicht verfügbar",
                           hint=st.get("missing") or tv_keys.MISSING)]
        rows = [toggle(ROW_ENABLED, "Fernseher übers Netz steuern", on, default=tv.DEFAULTS["tv.enabled"],
                       hint=HINT_ENABLED)]
        if not on:
            return rows
        paired = bool(st.get("paired"))
        rows.append(info("display.tvctl.status", "Fernseher", st.get("name") if paired else "Nicht gekoppelt",
                         note="Gekoppelt" if paired else ""))
        rows.append(link(PAGE_PAIR, "Anderen Fernseher koppeln" if paired else "Fernseher koppeln", PAGE_PAIR,
                         hint=HINT_PAIR))
        if not paired:
            rows.append(info("display.tvctl.standby", "Fernseher im Standby", "", hint=HINT_STANDBY))
            return rows
        adb = bool(st.get("adb"))
        rows.append(link(PAGE_INPUTS, "Eingänge genauer erkennen (optional)", PAGE_INPUTS,
                         note="Eingerichtet" if adb else "Nicht eingerichtet", hint=HINT_INPUTS))
        port, source = st.get("pc_port"), st.get("pc_from") or ""
        rows.append(info("display.tvctl.pcport", "Eingang des PCs", tv.port_label(port) if port else "Unbekannt",
                         note={"adb": "Erkannt", "edid": "Aus der EDID"}.get(source, ""),
                         hint="Steht der PC an diesem Eingang? Erkannt wird er über das Bildsignal (CEC-Adresse) bzw. "
                              "beim Erkennen der Eingänge." if port else
                              "Der PC5 Launcher kennt den Eingang des PCs nicht. „Eingänge genauer erkennen“ findet "
                              "ihn; bis dahin schaltet er nach dem Aufwachen nicht um."))
        mode = tv.off_mode(self.config)             # eingeschaltet: der gespeicherte Wert, Unbekanntes als Vorgabe
        rows.append(select(ROW_OFF, "Beim Ruhemodus und Ausschalten Fernseher ausschalten", mode,
                           [option(k, OFF_LABELS[k], "Braucht „Eingänge genauer erkennen“" if k == "pc" and not adb
                                   else "") for k in tv.OFF_CHOICES],
                           default=tv.DEFAULTS["tv.off_with_pc"], hint=HINT_OFF,
                           note="Wirkt erst mit „Eingänge genauer erkennen“" if mode == "pc" and not adb else ""))
        rows.append(toggle(ROW_WAKE, "Beim Aufwachen Fernseher einschalten und auf den PC umschalten",
                           self.config.get("tv.wake_with_pc", False) is True, default=tv.DEFAULTS["tv.wake_with_pc"],
                           hint=HINT_WAKE))
        rows.append(info("display.tvctl.standby", "Fernseher im Standby", "", hint=HINT_STANDBY))
        rows.append(confirm_action(ROW_FORGET, "Kopplung vergessen", "Kopplung vergessen?",
                                   "Der PC5 Launcher vergisst „%s“. %s" % (st.get("name") or "den Fernseher", HINT_FORGET),
                                   ok="Vergessen", hint=HINT_FORGET))
        return rows

    def _found(self, force: bool = False) -> list[dict]:
        part = self.part()
        return [i for i in (_call(lambda: part.discover(force)) or []) if isinstance(i, dict) and i.get("host")]

    def _pair_rows(self) -> list:
        if not tv.enabled(self.config):
            return [info("display.tvctl.pair.off", TEXT_SWITCH_ON, enabled=False)]
        st = self._status()
        rows = [info("display.tvctl.pair.howto", "So geht es", "", hint=HINT_PAIR)]
        if st.get("can_discover", True):
            found = self._found()
            for item in found:
                rows.append(action(ROW_PICK + item["host"], item.get("name") or item["host"], note=item["host"],
                                   hint="Auswählen, um zu koppeln."))
            if not found:
                rows.append(info("display.tvctl.pair.none", "Kein Fernseher gefunden.", enabled=False,
                                 hint="Ist er an und im selben Heimnetz? Manche Router lassen die Suche nicht durch; "
                                      "dann gib seine Adresse ein."))
            rows.append(action(ROW_SEARCH, "Erneut suchen"))
        else:
            rows.append(info("display.tvctl.pair.noavahi", "Suche nicht verfügbar", enabled=False,
                             hint=tv_net.TEXT_NO_AVAHI))
        rows.append(text(ROW_ADDRESS, "Adresse eingeben", "", learn=False, hint=HINT_ADDRESS))
        return rows

    def _code_rows(self) -> list:
        st = self._status()
        if not st.get("pairing"):
            return [info("display.tvctl.code.none", "Gerade läuft keine Kopplung.", enabled=False,
                         hint="Wähle den Fernseher unter „Fernseher koppeln“ aus.")]
        return [info("display.tvctl.code.howto", "Der Fernseher zeigt jetzt einen Code", st.get("pairing_name") or "",
                     hint="6 Zeichen aus Ziffern und den Buchstaben A bis F. Gib sie unten ein."),
                text(ROW_CODE, "Code eingeben", "", learn=False, hint=tv.TEXT_CODE_FORMAT),
                action(ROW_CODE_NEW, "Neuen Code anfordern", hint="Wenn der Code am Fernseher verschwunden ist.")]

    def _input_rows(self) -> list:
        st = self._status()
        if not st.get("paired"):
            return [info("display.tvctl.inputs.none", "Koppel zuerst den Fernseher.", enabled=False)]
        rows = [info(ident, label, "", hint=hint) for ident, label, hint in STEPS]
        rows.append(action(ROW_READ, "Neu erkennen" if st.get("adb") else "Jetzt erkennen", hint=HINT_READ))
        pc = st.get("pc_port")
        for p in st.get("ports") or []:
            if not isinstance(p, dict) or not isinstance(p.get("port"), int):
                continue
            note = "Dieser PC" if p["port"] == pc else ("Nicht verbunden" if p.get("state") == "disconnected" else "")
            rows.append(info("display.tvctl.inputs.port:%d" % p["port"], tv.port_label(p["port"]),
                             str(p.get("name") or ""), note=note))
        return rows

    # --- Ändern ---------------------------------------------------------------------------

    def set(self, row_id: str, value):
        part = self.part()
        if part is None:
            raise SettingsError(TEXT_UNAVAILABLE)
        if row_id == ROW_ENABLED:
            if not self._status().get("available", True):
                raise SettingsError(tv_keys.MISSING)
            self.config.set("tv.enabled", bool(value))
            return {"refresh": True}
        if not tv.enabled(self.config):
            raise SettingsError(TEXT_SWITCH_ON)
        if row_id == ROW_OFF:
            if value not in tv.OFF_CHOICES:
                raise SettingsError("Diese Auswahl gibt es nicht.")
            self.config.set("tv.off_with_pc", value)
            if value == "pc" and not self._status().get("adb"):
                return {"refresh": True, "message": "Das wirkt erst, wenn „Eingänge genauer erkennen“ eingerichtet "
                                                    "ist. Bis dahin bleibt der Fernseher an."}
            return {"refresh": True}
        if row_id == ROW_WAKE:
            self.config.set("tv.wake_with_pc", bool(value))
            return {"refresh": True}
        if row_id == ROW_ADDRESS:
            return self._picked(lambda: part.pick_address(str(value or "")), str(value or "").strip())
        if row_id == ROW_CODE:
            name = _call(lambda: part.submit_code(str(value or "")))
            return {"message": "„%s“ ist gekoppelt." % name, "page": PAGE}
        raise SettingsError("Diese Einstellung lässt sich nicht ändern.")

    def _picked(self, fn, name: str) -> dict:
        result = _call(fn)
        if result == "paired":
            return {"message": "„%s“ ist gekoppelt. Der Fernseher kannte diesen PC schon." % name, "page": PAGE}
        return {"page": PAGE_CODE}

    def action(self, row_id: str, value=None):
        part = self.part()
        if part is None:
            raise SettingsError(TEXT_UNAVAILABLE)
        if row_id == ROW_FORGET:
            _call(part.forget)
            return {"message": "Die Kopplung ist vergessen.", "refresh": True}
        if not tv.enabled(self.config):
            raise SettingsError(TEXT_SWITCH_ON)
        if row_id.startswith(ROW_PICK):
            host = row_id[len(ROW_PICK):]
            item = next((i for i in self._found() if i.get("host") == host), None)
            if item is None:
                raise SettingsError("Dieser Fernseher ist nicht mehr in der Liste. Such noch einmal.")
            name = item.get("name") or host
            return self._picked(lambda: part.pick(host, name, item.get("hostname") or ""), name)
        if row_id == ROW_SEARCH:
            self._found(force=True)
            return {"refresh": True}
        if row_id == ROW_CODE_NEW:
            _call(part.new_code)
            return {"message": "Der Fernseher zeigt einen neuen Code.", "refresh": True}
        if row_id == ROW_READ:
            found = _call(part.learn_inputs)
            ports = found.get("ports") or []
            current = found.get("current")
            if current:
                message = "%d Eingänge erkannt. Der PC hängt an %s." % (len(ports), tv.port_label(current))
            else:
                message = "%d Eingänge erkannt." % len(ports)
            return {"message": message, "refresh": True}
        raise SettingsError("Diese Aktion gibt es nicht.")
