"""Schnittstellen zwischen den Diensten des Home-Prozesses.

Nur Beschreibung (typing.Protocol), kein Verhalten. Wer einen Dienst baut, hält diese Signaturen
ein; wer einen benutzt, verlässt sich nur auf sie. Änderungen hier nur durch den Hauptagenten.
Alle Dienste laufen in der GLib-Hauptschleife; Rückrufe kommen dort an. Nichts blockiert länger
als etwa 50 ms: Längeres läuft in einem Thread und meldet sich über GLib.idle_add zurück.
"""
from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Callable, Protocol

from .launch.spec import LaunchSpec
from .library.model import Entry

Role = str          # "home" | "overlay"
OverlayMode = str   # "hidden" | "passive" | "active"
Route = str         # "home" | "overlay" | "app" | "mouse"


class Config(Protocol):
    """Einstellungen in settings.json. Schlüssel mit Punkten ("audio.ui_volume")."""
    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...            # ruft Beobachter; die Datei schreibt im Home ein eigener Thread (E43)
    def on_change(self, prefix: str, fn: Callable[[str, Any], None]) -> None: ...
    def reset(self, key: str) -> None: ...                      # zurück auf die Vorgabe
    def reset_all(self) -> list[str]: ...                       # alles auf die Vorgaben; liefert die geänderten Schlüssel
    def add_defaults(self, table: dict[str, Any]) -> None: ...  # Vorgaben eines Bausteins anmelden (E22)
    def all(self) -> dict[str, Any]: ...


class Rpc(Protocol):
    def method(self, name: str) -> Callable: ...                 # Dekorator: fn(params: dict) -> dict | Future | None
    def register(self, name: str, fn: Callable[[dict], Any]) -> None: ...
    def emit(self, event: str, payload: dict | None = None, target: Role | None = None) -> None: ...
    def dispatch(self, role: Role, name: str, params: dict) -> Any: ...   # ruft die Methode; Ausnahme = Fehler an die Seite
    def names(self) -> list[str]: ...
    # Antwort an die Seite; meta bekommt "deferred", wenn die Methode ein Future geliefert hat
    def handle(self, role: Role, message: Any, reply: Callable[[dict], None], meta: dict | None = None) -> None: ...


class StateStore(Protocol):
    """Der Zustand, den sys.state liefert. update() schickt das Ereignis "state" mit der Teilmenge."""
    def get(self) -> dict: ...
    def value(self, key: str, default: Any = None) -> Any: ...
    def update(self, **partial: Any) -> None: ...


class UiHost(Protocol):
    def start(self) -> None: ...                                 # Fenster anlegen, Seiten laden
    def emit(self, role: Role | None, event: str, payload: dict | None) -> None: ...
    def eval(self, role: Role, js: str, callback: Callable[[Any], None] | None = None) -> None: ...
    def snapshot(self, role: Role, path: str, callback: Callable[[bool], None] | None = None) -> None: ...
    def reload(self, role: Role) -> None: ...
    def xid(self, role: Role) -> int: ...                        # X-Fenster-ID, 0 solange nicht realisiert
    def frames(self, reset: bool = False) -> dict: ...           # gezeichnete Bilder je Rolle seit dem letzten Reset
    def resize(self, width: int | None = None, height: int | None = None) -> None: ...   # der Wurzel folgen
    on_realize: Callable[[Role, int], None] | None               # von main gesetzt; läuft VOR dem Einblenden (Fenster markieren)
    on_first_frame: Callable[[Role], None] | None
    on_beat: Callable[[Role], None] | None
    on_page_failed: Callable[[Role, str, str], None] | None      # (rolle, adresse, text): Seite nicht ladbar
    on_crash_loop: Callable[[Role], None] | None                 # Web-Prozess einer Rolle stürzt immer wieder ab


class Gamescope(Protocol):
    """X-Eigenschaften der Gamescope-Sitzung (Wurzel von Xwayland 0, Fenster auf beiden Servern).
    server: 0 = Xwayland 0 (Home, Overlay), 1 = Xwayland 1 (Spiele; das Home sucht ihn selbst, E36)."""
    def get(self, name: str, window: int | None = None, server: int = 0) -> list[int] | None: ...
    def set(self, name: str, values: list[int], window: int | None = None, server: int = 0) -> None: ...
    def delete(self, name: str, window: int | None = None, server: int = 0) -> None: ...
    def focused_app(self) -> int: ...
    def focusable_apps(self) -> list[int]: ...
    def focusable_windows(self) -> list[tuple[int, int, int]]: ...   # (xid, app_id, pid)
    def root_size(self) -> tuple[int, int]: ...                 # Wurzel von Xwayland 0 (Home, Overlay)
    def game_root_size(self) -> tuple[int, int]: ...            # Wurzel von Xwayland 1 (Spielauflösung)
    def game_display(self) -> str: ...                           # DISPLAY von Xwayland 1 (":N")
    def hdr_output_active(self) -> bool: ...                     # GAMESCOPE_HDR_OUTPUT_FEEDBACK == 1
    def watch(self, callback: Callable[[str], None]) -> None: ...   # "focus" | "apps" | "size" | "baselayer"


class FocusManager(Protocol):
    foreground: int | None                                       # unsere App-ID vorn, None = Home
    overlay_mode: OverlayMode
    def tag_window(self, role: Role, xid: int) -> None: ...       # home: STEAM_GAME=HOME_APPID; overlay: STEAM_OVERLAY=1, Deckkraft 0
    def start(self) -> None: ...                                 # Beobachter an, Liste [HOME] schreiben
    def set_foreground(self, app_id: int | None) -> None: ...    # Liste [app_id, HOME] bzw. [HOME] (E35)
    def overlay(self, mode: OverlayMode) -> None: ...
    def apply_session_defaults(self) -> None: ...                # ITM aus, Spielauflösung; nie GAMESCOPE_FPS_LIMIT (E23)
    def on_change(self, fn: Callable[[], None]) -> None: ...     # Vordergrund oder Overlay hat sich geändert
    def set_app_options(self, app_id: int, fps_limit: int | None = None,
                        resolution: str | None = None) -> None: ...   # gilt, solange diese App vorn ist
    def set_hdr(self, on: bool) -> None: ...


class RunningApp(Protocol):
    app_id: int
    entry_id: str
    reaper_pid: int
    started: float
    frozen: bool
    spec: LaunchSpec
    visible: bool                                                # hat schon ein Fenster in GAMESCOPE_FOCUSABLE_APPS
    closing: bool                                                # wird gerade beendet: nicht fortsetzen (E40)
    exited: bool
    exit_code: int | None                                        # des reaper; nach eigenem Schließen ohne Aussage
    runtime: float                                               # Sekunden ohne die angehaltene Zeit
    frozen_total: float
    frozen_since: float
    adopted: bool                                                # nach einem Home-Neustart übernommen
    log_path: str


class AppManager(Protocol):
    def launch(self, spec: LaunchSpec) -> RunningApp: ...        # LaunchError bei Fehlern
    def running(self) -> list[RunningApp]: ...
    def by_entry(self, entry_id: str) -> RunningApp | None: ...
    def by_app_id(self, app_id: int) -> RunningApp | None: ...
    def freeze(self, app: RunningApp) -> None: ...
    def thaw(self, app: RunningApp) -> None: ...
    def close(self, app: RunningApp) -> None: ...                # Stufen: höflich, 8 s, SIGKILL an Nachkommen
    def kill(self, app: RunningApp) -> None: ...
    def shutdown(self) -> None: ...                              # blockiert; das Home schließt über shell.after_shutdown (E39)
    def adopt(self) -> list[RunningApp]: ...                     # laufende Apps eines früheren Home übernehmen (running.json)
    def on_change(self, fn: Callable[[RunningApp, str], None]) -> None: ...   # "started" | "visible" | "exited" | "frozen" | "thawed"


class InputService(Protocol):
    route: Route
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def set_route(self, route: Route, display: str | None = None, profile: str = "mouse",
                  look: float = 1.0, layout: str = "", keys: dict | None = None) -> None: ...   # nur für "mouse": display, profile
    # ("mouse"/"keys"/"game"/"term"/"cards"), look (Umschauen, "game"), layout (Sprungstellen, "cards"),
    # keys (E109: Tastenbelegung des Spiels {taste: ziel} aus der Option pad_keys, nur "game"; None = E64)
    # Eigene Sprungstellen des Spiels vorn ({name: {x, y, w, h}}, Option pad_spots, E99) über dem
    # layout; wirkt sofort, ohne die Maus neu aufzubauen. set_route setzt sie auf {} zurück.
    def set_spots(self, spots: dict) -> None: ...
    @property
    def mouse_spots(self) -> dict: ...                           # die gerade geltenden eigenen Stellen (Kopie)
    def set_exclusive(self, on: bool) -> None: ...               # Pads per EVIOCGRAB nur für couch (Overlay über App)
    def inject(self, button: str, phase: str = "press") -> None: ...            # "press" | "down" | "up"; auch für Tests
    def all_neutral(self) -> bool: ...
    def pads(self) -> list[dict]: ...                            # [{id, name, battery, charging, type, slot}]
    def idle_seconds(self) -> float: ...
    def rumble(self, kind: str) -> None: ...
    def type_text(self, text: str) -> None: ...                  # XTEST in die vordere App
    def press_key(self, name: str) -> None: ...                  # "Return", "BackSpace", "Escape", "Tab", Pfeile
    # Rückrufe, von main gesetzt:
    on_button: Callable[[str, bool], None] | None                # (taste, wiederholung) für route home/overlay
    on_ps: Callable[[str, float], None] | None                   # ("short"|"long"|"options"|"options-done"|"options-cancel", fortschritt)
    on_pads_changed: Callable[[], None] | None
    on_type_error: Callable[[str], None] | None                  # Tippen gescheitert (deutscher Satz)
    on_idle_off: Callable[[dict], None] | None                   # {id, name, slot}: Pad so lange unbenutzt wie input.idle_off_minutes
    on_cable_pad: Callable[[dict], None] | None                  # {id, name, hidraw}: DualSense steckt per USB (Kabel-Kopplung)
    on_spot: Callable[[str, dict], None] | None                  # (taste, {x, y, w, h}): Create gehalten + Sprungtaste hat die Zeigerstelle gemerkt (E99)


class LibraryIndex(Protocol):
    def entries(self) -> list[Entry]: ...
    def get(self, entry_id: str) -> Entry | None: ...
    def rescan(self) -> Future: ...
    def set_option(self, entry_id: str, key: str, value: Any) -> None: ...
    def set_hidden(self, entry_id: str, hidden: bool) -> None: ...
    def set_favorite(self, entry_id: str, favorite: bool) -> None: ...   # E99; KeyError bei unbekanntem Eintrag
    def record_play(self, entry_id: str, seconds: int) -> None: ...
    def week_playtime(self, entry_id: str, now: float | None = None) -> int: ...   # Sekunden seit Montag 0 Uhr (Ortszeit)
    def touch(self, entry_id: str) -> None: ...                  # zuletzt gestartet = jetzt
    def add_local(self, entry: Entry) -> None: ...               # vom Archiv-Installer
    def remove_local(self, entry_id: str) -> bool: ...
    def update_art(self, arts: dict[str, dict], notify: bool = False) -> list[str]: ...   # Bilder nachtragen; liefert die geänderten ids
    def add_volume(self, path: str, mount: bool | None = None) -> None: ...   # Datenträger anmelden: fehlt er, bleibt alles als available=False stehen
    def missing_volumes(self) -> list[str]: ...
    def position(self, entry_id: str) -> int: ...                # Rang nach "zuletzt gespielt"
    def first_scan_pending(self) -> Future | None: ...           # der erste Lauf, solange er noch läuft
    describe_env: Callable[[], dict] | None                      # Umgebung für lib.get (Proton-Fassung, globale Schalter)
    detail_hooks: list[Callable[[Entry, dict], None]]            # ergänzen das Ergebnis von lib.get (z. B. choices)
    def on_change(self, fn: Callable[[], None]) -> None: ...
    def on_rename(self, fn: Callable[[Entry], None]) -> None: ...  # nach lib.rename, wenn sich der geltende Name änderte (E99)
    def with_own_title(self, entry: Entry) -> Entry: ...         # Eintrag einer Quelle mit dem eigenen Namen (E90); sonst er selbst
    # E108: Spielzeit-Rückblick rein aus dem Speicher; range "week" | "month" | "all", offset 0 = der
    # laufende Zeitraum, andere Werte blättern (Form des Ergebnisses: docs/research/e103/SPEC.md 9.3)
    def stats(self, range: str = "week", offset: int = 0, now: float | None = None) -> dict: ...
    # E106: übertragbare Meta-Daten je Eintrag (Namen, Favoriten, Ausgeblendetes, Optionen, Spielzeit)
    def export_meta(self) -> dict: ...
    def import_meta(self, data: dict, now: float | None = None) -> dict: ...   # -> {applied: n, skipped: [ids]}
    def export_sessions(self) -> list: ...                       # E106: Sitzungsverlauf der übertragbaren Einträge
    def import_sessions(self, items: Any) -> dict: ...           # E106: -> {applied, skipped}
    inactive_sources: Callable[[], Any] | None                   # E108: ausgeschaltete Quellen (seen_sources)
