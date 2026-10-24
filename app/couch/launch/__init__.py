"""Starter: aus einem Bibliothekseintrag wird ein Startauftrag (LaunchSpec). Hier wird nichts
gestartet; das macht der AppManager bzw. bei Steam die Übergabe an den Supervisor.

Verteilt wird nach entry.source über ein Register:
    register_launcher("proton", fn)        fn(entry, app) -> LaunchSpec, LaunchError bei Fehlern
build_spec() läuft in einem Arbeits-Thread: Ein Starter darf blockieren (Platte, /proc), aber
weder GTK noch X anfassen. Er kopiert nichts: Steht eine Prefix-Übernahme aus, wirft er
AdoptionRequired, und die Shell fragt und kopiert (E24). LaunchError.reason nennt der
Oberfläche die Ursache (Ereignis app.error, Feld code).

Steam-Spiele ("steam:<appid>") und Steam-Verknüpfungen ("steam:shortcut:<appid>", E25) startet
couch-steam-game als gewöhnliche App: Steam läuft dabei unsichtbar, das Home bleibt an (E57).
Nur die Kachel „Steam“ (Big Picture ohne Spiel) ist eine Übergabe an den Supervisor
(steam_client_spec).
"""
from __future__ import annotations

import os
from typing import Any, Callable

from ..library.model import Entry
from .spec import APP_KINDS, LaunchError, LaunchSpec

Launcher = Callable[[Entry, Any], LaunchSpec]

_LAUNCHERS: dict[str, Launcher] = {}

NO_LAUNCHER = "Für diese Art Spiel gibt es noch keinen Starter."


def register_launcher(source: str, fn: Launcher) -> None:
    _LAUNCHERS[source] = fn


def unregister_launcher(source: str) -> None:
    _LAUNCHERS.pop(source, None)


def has_launcher(source: str) -> bool:
    return source in _LAUNCHERS


def build_spec(entry: Entry, app: Any = None) -> LaunchSpec:
    """Baut den Startauftrag für entry. Wirft LaunchError mit einem deutschen Satz."""
    fn = None
    if "fixture" in _LAUNCHERS and isinstance(entry.detail.get("launch"), dict):
        fn = _LAUNCHERS["fixture"]          # Prüf-Einträge bringen ihren Startbefehl selbst mit
    if fn is None:
        fn = _LAUNCHERS.get(entry.source)
    if fn is None:
        raise LaunchError(NO_LAUNCHER, "source=%s id=%s" % (entry.source, entry.id),
                          reason="no-launcher")
    spec = fn(entry, app)
    if not isinstance(spec, LaunchSpec):
        raise LaunchError(NO_LAUNCHER, "Starter für %s lieferte %r" % (entry.source, type(spec)),
                          reason="no-launcher")
    if spec.kind not in APP_KINDS:
        raise LaunchError("Der Starter hat eine unbekannte App-Art geliefert.", str(spec.kind),
                          reason="no-launcher")
    spec.entry_id = entry.id
    if not spec.title:
        spec.title = entry.title
    if spec.handover is None and not spec.argv:
        raise LaunchError("Für diesen Eintrag ist kein Startbefehl hinterlegt.", entry.id,
                          reason="no-launcher")
    if entry.option("freeze") is False:
        spec.freeze = False                 # Einstellung je Spiel: nie anhalten
    return spec


# ---------------------------------------------------------------- Steam
SHORTCUT_PREFIX = "steam:shortcut:"
SHORTCUT_FLAG = 0x02000000          # untere Hälfte der Spielkennung einer Verknüpfung (E25)
STEAM_CLIENT_ID = "app:steam"


def is_shortcut(entry: Entry) -> bool:
    return str(entry.id).startswith(SHORTCUT_PREFIX)


def _digits(value: Any) -> str:
    """Eine Zahl als Ziffernfolge, sonst "". Negative Werte (so steht die appid einer
    Verknüpfung in shortcuts.vdf) werden als ihre unteren 32 Bit gelesen."""
    if isinstance(value, bool):
        return ""
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return text
    if text.startswith("-") and text[1:].isdigit():
        return str(int(text) & 0xFFFFFFFF)
    return ""


def steam_app_id(entry: Entry) -> str:
    """App-Nummer aus detail.appid (Verknüpfungen: detail.shortcut_appid) bzw. aus der ID
    ("steam:<n>", "steam:shortcut:<n>")."""
    appid = _digits(entry.detail.get("appid")) or _digits(entry.detail.get("shortcut_appid"))
    if not appid and str(entry.id).startswith("steam:"):
        appid = _digits(str(entry.id).rsplit(":", 1)[1])
    if not appid:
        raise LaunchError("Zu diesem Steam-Spiel fehlt die App-Nummer.", entry.id, reason="no-appid")
    return appid


def steam_game_id(entry: Entry) -> str:
    """Die Nummer für steam://rungameid/…: bei Spielen die App-Nummer, bei Verknüpfungen
    detail.gameid bzw. (appid << 32) | 0x02000000 (E25; die appid steht in shortcuts.vdf
    vorzeichenbehaftet, gemeint sind ihre unteren 32 Bit)."""
    if not is_shortcut(entry):
        return steam_app_id(entry)
    gameid = _digits(entry.detail.get("gameid"))
    if gameid:
        return gameid
    return str(((int(steam_app_id(entry)) & 0xFFFFFFFF) << 32) | SHORTCUT_FLAG)


STEAM_START_TIMEOUT = 330.0         # Steam-Kaltstart samt Selbst-Aktualisierung; couch-steam-game gibt nach 300 s auf
STEAM_KILL_GRACE = 12.0             # Steam braucht nach dem Spiel ein paar Sekunden (Cloud), < shell.CLOSE_OTHER_S


def _hdr_snapshot(app: Any) -> bool:
    """Gibt die Sitzung gerade HDR aus? Aus dem Schnappschuss, den die Hauptschleife nachführt
    (app.wiring.launch = LaunchContext, wiring_launch.refresh_snapshot). Nie app.gs: build_spec
    läuft im Arbeits-Thread, und python-xlib hat ohne Xlib.threaded keine Sperren (E99)."""
    ctx = getattr(getattr(app, "wiring", None), "launch", None)
    hdr_active = getattr(ctx, "hdr_active", None)
    try:
        return bool(hdr_active() if callable(hdr_active) else hdr_active)
    except Exception:
        return False


def steam_game_env(entry: Entry, app: Any = None) -> dict:
    """Schalter aus Einstellungen › Spiele und den Spieleinstellungen (fsr4, hdr; E99) für ein Spiel,
    das Steam startet. couch-steam-game gibt sie dem unsichtbaren Steam-Client mit, Steam reicht sie
    an das Spiel weiter (Proton wertet DXVK_HDR und PROTON_FSR4_UPGRADE aus). Gleiche Regeln wie bei
    eigenen Starts (proton.py)."""
    from .proton import FSR4_VERSION, global_switch

    class _Ctx:
        config = getattr(app, "config", None)

    env = {}
    if entry.option("fsr4") == "off" or not global_switch(_Ctx, "games.fsr4"):
        env["FSR4_UPGRADE"] = "0"
    else:
        env["PROTON_FSR4_UPGRADE"] = FSR4_VERSION
    hdr = entry.option("hdr") != "off" and global_switch(_Ctx, "games.hdr") and _hdr_snapshot(app)
    env["DXVK_HDR"] = "1" if hdr else "0"
    # OptiScaler (2026-10-24): "0" schaltet einen geerbten Wert ab (Proton: check_environment)
    env["PROTON_USE_OPTISCALER"] = "1" if entry.option("optiscaler") == "on" else "0"
    return env


def steam_launcher(entry: Entry, app: Any = None) -> LaunchSpec:
    """Steam-Spiele und Steam-Verknüpfungen (E57): couch-steam-game startet Steam unsichtbar und
    lässt es das Spiel starten. Das Home bleibt an und zeigt das Ladebild, bis Gamescope ein
    Fenster der App meldet. Gamescope nimmt den äußersten reaper der Elternkette, also unseren:
    Das Spielfenster trägt unsere Nummer, nicht die Steam-App-Nummer (am Fernseher 2026-10-18
    mit P.T. gesehen). Steams eigene Fenster tragen STEAM_GAME=769 und zählen nicht mit.
    Angehalten wird das Spiel samt Steam, solange die Konsole vorn ist und es ein Fenster hat:
    So kommt Steams Pad-Belegung nicht im Home an; ohne Fenster räumt Steam gerade auf."""
    from .. import paths
    appid = int(steam_app_id(entry)) & 0xFFFFFFFF
    if appid <= 0:
        raise LaunchError("Zu diesem Steam-Spiel fehlt die App-Nummer.", entry.id, reason="no-appid")
    helper = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                          "bin", "couch-steam-game")
    env = steam_game_env(entry, app)
    env["COUCH_STEAM_LOG"] = paths.app_log("steam-client")
    if os.environ.get("COUCH_SESSION_ENV"):
        env["COUCH_STEAM_SESSION_ENV"] = os.environ["COUCH_SESSION_ENV"]
    return LaunchSpec(
        entry_id=entry.id, argv=[helper, steam_game_id(entry), str(appid), entry.title],
        env=env, kind="game", freeze=True, title=entry.title, freeze_needs_window=True,
        start_timeout=STEAM_START_TIMEOUT, kill_grace=STEAM_KILL_GRACE)


def steam_client_spec(entry: Entry) -> LaunchSpec:
    """Die Kachel „Steam“ (app:steam): Big Picture ohne Spiel, zurück geht es von Hand."""
    return LaunchSpec(entry_id=entry.id, argv=[], kind="tool", freeze=False, title=entry.title,
                      handover={"uri": None, "return": "manual"})


register_launcher("steam", steam_launcher)

__all__ = ["LaunchError", "LaunchSpec", "build_spec", "register_launcher", "unregister_launcher",
           "has_launcher", "steam_launcher", "steam_client_spec", "steam_game_id", "steam_app_id",
           "is_shortcut", "NO_LAUNCHER"]
