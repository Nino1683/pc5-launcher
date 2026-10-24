"""Updates an einem Ort (Wunsch 2026-10-21: „Ich möchte auch, dass das System-Update gemeldet wird,
oder schon von installierten Apps wie YouTube, Proton etc. die Updates angezeigt werden, wenn
verfügbar“).

Gesucht wird kurz nach dem Start und dann alle CHECK_AGAIN_S (UpdateWatch, Arbeits-Thread):
  Systemsoftware   der tägliche Updater (settings/backends/osupdate.py, E32); hier nur sein Stand
  Apps             Flatpak (YouTube = VacuumTube, Kodi, Emulatoren …), settings/backends/flatpak.py; E104: die
                   Installation des Systems und die des Nutzers (flatpak remote-ls --updates --system/--user)
  Proton           Proton-CachyOS: neueste Fassung bei GitHub gegen die Ordner in compatibilitytools.d, in
                   der Bauart dieses Prozessors (E104: proton_repair.this_arch, sonst x86_64_v3)
  Spiele           über den Store installierte Epic-Spiele (legendary list-installed --check-updates)
Steam aktualisiert seine Spiele selbst; für GOG-Spiele gibt es noch keine Suche.

Was neu dazukommt, meldet eine Mitteilung, einmal je Fassung (gemerkt in updates-seen.json). Der
Stand steht in sys.state.updates und schaltet den Punkt am Zahnrad mit (update_available).
Scheitert eine Quelle (kein Netz, GitHub-Grenze), gilt für sie der letzte bekannte Stand weiter, und
ihre Meldungen bleiben gemerkt (E99: sonst kamen sie nach der nächsten Suche doppelt).

Die Zeitgeber laufen auf der monotonen Uhr, die im Ruhemodus steht. Nach dem Aufwachen fragt
resumed() deshalb nach der Wanduhr, ob die Suche fällig ist, und sucht dann RESUME_DELAY_S später
(dann ist das Netz wieder da). Sonst kam die Suche bei einer Konsole, die meist schläft, nur alle
paar Tage (E99). Danach richtet sich der Takt nach dieser Suche. Erreicht eine Suche keine Quelle
im Netz, bleibt sie fällig.

Proton wird neben die bisherige Fassung gelegt (install_proton). Seit E101/E102 gibt es dafür nur
noch einen Weg, proton_repair.install: laden (mit Fortsetzen), Prüfsumme (sha512 von GitHub),
sicher entpacken in einen markierten, gesperrten Zwischenordner, einsetzen per rename ohne
Ersetzen, Manifest. Mit „Standard-Proton: Automatisch“ nimmt der Start danach von selbst die
neueste; Spiele mit fest gewählter Fassung behalten ihre. cleanup_proton räumt nur noch Reste der
früheren eigenen Fassung ab (pc5-*-Teildatei und -Entpack-Ordner).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from . import jsontext, log, paths

_log = log.get("updates")

CHECK_FIRST_S = 180.0
CHECK_AGAIN_S = 12 * 3600.0
RESUME_DELAY_S = 60.0           # nach dem Aufwachen: so lange auf das Netz warten
DUE_SLACK_S = 600.0             # fällig ist eine Suche schon so lange vor CHECK_AGAIN_S (Spielraum des Zeitgebers)
PROTON_API = "https://api.github.com/repos/CachyOS/proton-cachyos/releases/latest"
PROTON_FLAVOR = "x86_64_v3"             # bis E104 fest; jetzt nur noch Rückfall von proton_flavor()
_PROTON = re.compile(r"^proton-cachyos-")
_ASSET = re.compile(r"^(proton-cachyos-[A-Za-z0-9._-]+)\.tar\.xz$")
# Zwischenstände der früheren eigenen Fassung von install_proton (vor E101: Teildatei, Entpack-Ordner);
# nur genau diese räumt cleanup_proton ab, den Ordner nur mit der Marke darin (SAFETY 13)
_LEFT_PART = re.compile(r"^pc5-proton-cachyos-[A-Za-z0-9._-]+\.tar\.xz\.part$")
_LEFT_DIR = re.compile(r"^pc5-entpacken-proton-cachyos-[A-Za-z0-9._-]+$")
STAGING_MARK = ".couch-proton-staging"
# Schlüssel in updates-seen.json -> Quelle (keys_of)
_KEY_SOURCE = {"app": "apps", "proton": "proton", "game": "games"}


# ---------------------------------------------------------------- Proton
def proton_installed(compat_dir: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(compat_dir) if _PROTON.match(n)
                      and os.path.isfile(os.path.join(compat_dir, n, "proton")))
    except OSError:
        return []


def proton_flavor() -> str:
    """Bauart für Proton-CachyOS auf diesem Rechner (E104): die Erkennung aus proton_repair (x86_64_v3, wenn
    die CPU alle v3-Befehle kann, sonst x86_64; arm64 auf ARM), einmal je Prozess gelesen."""
    try:
        from . import proton_repair
        return proton_repair.this_arch() or PROTON_FLAVOR
    except Exception:  # noqa: BLE001
        return PROTON_FLAVOR


def proton_release(fetch_json: Callable[[str], Any], flavor: str | None = None) -> dict | None:
    """Neueste Fassung bei GitHub: {tag, name, url, sha_url, size} für die Bauart flavor (Vorgabe proton_flavor())."""
    flavor = flavor or proton_flavor()
    data = fetch_json(PROTON_API)
    if not isinstance(data, dict):
        return None
    assets = {str(a.get("name") or ""): a for a in data.get("assets") or [] if isinstance(a, dict)}
    for name, asset in assets.items():
        m = _ASSET.match(name)
        if m and m.group(1).endswith("-" + flavor):
            sha = assets.get(m.group(1) + ".sha512sum") or {}
            return {"tag": str(data.get("tag_name") or ""), "name": m.group(1),
                    "url": str(asset.get("browser_download_url") or ""),
                    "sha_url": str(sha.get("browser_download_url") or ""), "size": int(asset.get("size") or 0)}
    return None


def proton_update(compat_dir: str, fetch_json: Callable[[str], Any]) -> dict | None:
    """Die neueste Fassung, wenn sie noch nicht installiert ist (sonst None)."""
    rel = proton_release(fetch_json)
    if rel is None or not rel["url"] or rel["name"] in proton_installed(compat_dir):
        return None
    installed = proton_installed(compat_dir)
    rel["installed"] = installed[-1] if installed else ""
    return rel


def _safe_remove(path: str) -> None:
    """Eigene Zwischenstände wegräumen: Datei per unlink, Ordner über die eine Löschstelle (E17)."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            from .installer import trash
            trash.remove_own_dir(path)
        elif os.path.lexists(path):
            os.unlink(path)
    except Exception as e:  # noqa: BLE001
        _log.warning("Proton: Zwischenstand %s bleibt liegen: %s", path, e)


def install_proton(rel: dict, compat_dir: str, on_progress: Callable[[int], None] = lambda p: None, *,
                   get: Callable | None = None, stop: Any = None, cache_dir: str | None = None,
                   manifest_dir: str | None = None) -> str:
    """Lädt rel (Proton-CachyOS, aus proton_release) nach compat_dir/<name>, über den einen sicheren
    Weg proton_repair.install (E101/E102). Die Quelle leitet proton_repair aus dem Namen ab
    (GitHub-Release), nie aus einer Adresse in rel. get/stop/cache_dir/manifest_dir: für Tests und
    die Warteschlange (wiring_proton). Liefert den Ordner; wirft ValueError mit deutschem Satz."""
    from . import proton_repair
    name = str(rel.get("name") or "")
    if not _PROTON.match(name) or "/" in name or name.startswith("."):
        raise ValueError("Unerwarteter Name: %s" % name)
    target = os.path.join(compat_dir, name)
    if os.path.lexists(target):
        return target
    source = proton_repair.source_for(name)
    if source is None:
        raise ValueError("Für %s ist keine Quelle bekannt." % name)
    source["size"] = int(rel.get("size") or 0)          # Fortschritt, falls GitHub keine Länge nennt
    root = paths.test_root()
    if not paths.test_mode() or (root and paths.is_under(compat_dir, [root])):
        os.makedirs(compat_dir, exist_ok=True)          # im Prüfmodus nie außerhalb des Käfigs anlegen
    result = proton_repair.install(
        name, compat_dir, cache_dir or proton_repair.cache_root(),
        progress=lambda p: on_progress(int(p.get("percent") or 0)), stop=stop,
        manifest_dir=manifest_dir, source=source, **({"get": get} if get is not None else {}))
    if result.get("ok") or result.get("code") == "exists":
        return target
    raise ValueError(str(result.get("message") or "Proton-CachyOS ließ sich nicht installieren."))


def cleanup_proton(compat_dir: str) -> list[str]:
    """Arbeits-Thread nach dem Start: Zwischenstände eines abgebrochenen Ladens entfernen. Endet
    das Home mitten im Laden (Ausschalten, Übergabe, SIGKILL), läuft das finally von
    install_proton nicht; Teildatei und Entpack-Ordner blieben sonst für immer liegen (E99).
    Nur während kein Laden läuft aufrufen. Liefert die entfernten Namen."""
    try:
        names = sorted(os.listdir(compat_dir))
    except OSError:
        return []
    gone = []
    for name in names:
        path = os.path.join(compat_dir, name)
        if _LEFT_PART.match(name) and os.path.isfile(path) and not os.path.islink(path):
            _safe_remove(path)
        elif _LEFT_DIR.match(name) and os.path.isdir(path) and not os.path.islink(path):
            if not os.path.isfile(os.path.join(path, STAGING_MARK)):
                _log.info("Proton: %s ohne Marke, bleibt liegen", path)
                continue
            _safe_remove(path)
        else:
            continue
        if not os.path.lexists(path):
            gone.append(name)
    if gone:
        _log.info("Proton: Reste eines abgebrochenen Ladens entfernt: %s", ", ".join(gone))
    return gone


# ---------------------------------------------------------------- gemerkte Meldungen
def seen_file() -> str:
    return os.path.join(paths.state_dir(), "updates-seen.json")


def load_seen() -> set[str]:
    try:
        with open(seen_file(), encoding="utf-8") as f:
            data = json.load(f)
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def save_seen(keys: set[str]) -> None:
    try:
        os.makedirs(paths.state_dir(), exist_ok=True)
        tmp = seen_file() + ".neu"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(jsontext.dumps(sorted(keys)))
        os.replace(tmp, seen_file())
    except OSError as e:
        _log.warning("updates-seen.json nicht schreibbar: %s", e)


def keys_of(found: dict) -> dict[str, str]:
    """Schlüssel je gefundenem Update (für „schon gemeldet“) -> Name für die Mitteilung."""
    out = {}
    # Die Systemsoftware meldet ihr eigener täglicher Updater (wiring_settings.update_check)
    for a in found.get("apps") or []:
        if not a.get("runtime"):
            out["app:%s@%s" % (a["id"], a.get("version") or a.get("branch") or "")] = a["name"]
    if found.get("proton"):
        out["proton:%s" % found["proton"]["name"]] = "Proton-CachyOS"
    for g in found.get("games") or []:
        out["game:%s@%s" % (g["app"], g["latest"])] = g["title"]
    return out


def summary(names: list[str]) -> str:
    if len(names) <= 3:
        return ", ".join(names)
    return "%s und %d weitere" % (", ".join(names[:2]), len(names) - 2)


# ---------------------------------------------------------------- Suche
class UpdateWatch:
    """sources: {"os": fn() -> (verfügbar, Fassung), "apps": fn() -> [...], "proton": fn() -> dict|None,
    "games": fn() -> [...]}; jede Quelle darf scheitern, die anderen zählen trotzdem.
    publish(found) in die Hauptschleife; notify(title, body)."""

    def __init__(self, sources: dict, publish: Callable[[dict], None], notify: Callable[[str, str], None],
                 later: Callable[[float, Callable[[], None]], Any], background: Callable[..., Any],
                 proton_installer: Callable[[dict, Callable[[int], None]], str] | None = None,
                 changed: Callable[[], None] = lambda: None, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.sources, self.publish, self.notify = sources, publish, notify
        self.later, self.background = later, background
        self.proton_installer, self.changed, self.clock = proton_installer, changed, clock
        self.wall = wall
        self.found: dict = {}
        self.lock = threading.Lock()
        self.checking = False
        self.proton_job = {"phase": "idle", "percent": 0, "error": ""}
        self._shown_at = 0.0
        self._resume_pending = False

    def busy_text(self) -> str | None:
        """Für busy_checks und den Leerlauf: Ein laufendes Laden bräche beim Ausschalten ab (E99)."""
        return "Proton-CachyOS wird geladen." if self.proton_job.get("phase") == "loading" else None

    def proton_installed(self, name: str) -> None:
        """Eine Proton-Version kam auf anderem Weg dazu (Einstellungen › Proton-Versionen, E102). Ist es
        die gemeldete neue Fassung, gilt Proton-CachyOS als aktuell (Zeile und Punkt am Zahnrad)."""
        rel = (self.found or {}).get("proton")
        if not rel or rel.get("name") != name:
            return
        self.found = {**self.found, "proton": None}
        self.publish(self.found)
        self.changed()

    # ------------------------------------------------------------ Proton installieren
    def start_proton(self) -> str:
        """Lädt die gefundene Proton-Fassung im Hintergrund. Liefert den Satz für die Seite."""
        rel = (self.found or {}).get("proton")
        if not rel:
            raise ValueError("Proton-CachyOS ist schon aktuell.")
        if self.proton_installer is None:
            raise ValueError("Das ist gerade nicht verfügbar.")
        with self.lock:
            if self.proton_job["phase"] == "loading":
                raise ValueError("Proton-CachyOS wird schon geladen.")
            self.proton_job = {"phase": "loading", "percent": 0, "error": ""}
        self.background(self._proton_run, rel)
        return "Proton-CachyOS wird geladen (%d MB)." % max(1, rel.get("size", 0) // 1048576)

    def _proton_progress(self, percent: int) -> None:
        self.proton_job = {**self.proton_job, "percent": int(percent)}
        now = self.clock()
        if now - self._shown_at >= 1.0 or percent >= 100:
            self._shown_at = now
            self.changed()

    def _proton_run(self, rel: dict) -> None:
        try:
            path = self.proton_installer(rel, self._proton_progress)
            _log.info("Proton-CachyOS installiert: %s", path)
            self.proton_job = {"phase": "idle", "percent": 100, "error": ""}
            self.found = {**self.found, "proton": None}
            self.publish(self.found)
            self.notify("Proton-CachyOS aktualisiert", "%s · Spiele mit „Standard-Proton: Automatisch“ nutzen sie ab dem "
                        "nächsten Start." % rel["name"].replace("proton-cachyos-", ""))
        except Exception as e:  # noqa: BLE001
            _log.warning("Proton-CachyOS: %s", e)
            self.proton_job = {"phase": "failed", "percent": 0, "error": str(e)}
            self.notify("Proton-CachyOS: das hat nicht geklappt", str(e))
        finally:
            self.changed()

    def start(self) -> None:
        self.later(CHECK_FIRST_S, self._due)

    def due(self) -> bool:
        """Ist eine Suche fällig, nach der Wanduhr? Die Zeitgeber allein zählen den Ruhemodus nicht.
        Ging die Uhr weit zurück (vor die letzte Suche), gilt sie als fällig, sonst käme lange keine
        mehr; kleine Korrekturen der Uhr zählen nicht."""
        at = (self.found or {}).get("at")
        if not isinstance(at, (int, float)):
            return True
        passed = self.wall() - at
        return passed < -DUE_SLACK_S or passed >= CHECK_AGAIN_S - DUE_SLACK_S

    def _due(self) -> None:
        if self.due():
            self.background(self.check)
            wait = CHECK_AGAIN_S
        else:
            # Gesucht wurde zwischendurch (beim Aufwachen): Der Takt richtet sich nach dieser Suche,
            # sonst kämen bis zu 24 h zwischen zwei Suchen (E99)
            at = (self.found or {}).get("at")
            passed = self.wall() - at if isinstance(at, (int, float)) else CHECK_AGAIN_S
            wait = max(RESUME_DELAY_S, CHECK_AGAIN_S - passed)
        self.later(wait, self._due)

    def resumed(self) -> None:
        """Hauptschleife, nach dem Aufwachen (ResumeWatch): Ist die Suche nach der Wanduhr fällig,
        kommt sie RESUME_DELAY_S später (E99)."""
        if self._resume_pending or not self.found or not self.due():
            return                      # noch nie gesucht: das erledigt der erste Zeitgeber
        self._resume_pending = True
        self.later(RESUME_DELAY_S, self._resume_due)

    def _resume_due(self) -> None:
        self._resume_pending = False
        if self.due():
            self.background(self.check)

    def check(self) -> dict:
        """Arbeits-Thread: alle Quellen fragen, Stand veröffentlichen, Neues melden."""
        with self.lock:
            if self.checking:
                return self.found
            self.checking = True
        try:
            before = self.found or {}
            found: dict = {"at": self.wall()}
            failed: set[str] = set()
            # „os“ liest nur den Stand des eigenen Updaters, das gelingt auch ohne Netz
            for key, fn in self.sources.items():
                try:
                    value = fn()
                except Exception as e:  # noqa: BLE001
                    _log.warning("Update-Suche %s: %s", key, e)
                    failed.add(key)
                if key in failed:
                    # Gescheitert heißt „unbekannt“, nicht „nichts da“: der letzte Stand gilt weiter
                    if key == "os":
                        found["os"], found["os_version"] = before.get("os", False), before.get("os_version", "")
                    else:
                        found[key] = before.get(key, [] if key in ("apps", "games") else None)
                elif key == "os":
                    found["os"], found["os_version"] = (value or (False, ""))
                else:
                    found[key] = value or ([] if key in ("apps", "games") else None)
            searched = set(self.sources) - {"os"}
            if searched and searched <= failed:
                # Nichts erreicht (Netz kurz nach dem Aufwachen noch nicht da): Die Suche bleibt
                # fällig, statt erst in 12 h wieder dran zu sein (E99)
                found.pop("at")
                if isinstance(before.get("at"), (int, float)):
                    found["at"] = before["at"]
            self.found = found
            self.publish(found)
            self._announce(found, failed)
            return found
        finally:
            with self.lock:
                self.checking = False

    def _announce(self, found: dict, failed: set[str] = frozenset()) -> None:
        keys = keys_of(found)
        seen = load_seen()
        new = [name for key, name in keys.items() if key not in seen]
        # nur Gültiges merken: Was es nicht mehr gibt, darf später wieder gemeldet werden. Was eine
        # gescheiterte Quelle gemeldet hatte, bleibt gemerkt (auch gleich nach einem Neustart des
        # Home, wenn ihr letzter Stand noch fehlt)
        keep = {key for key in seen if _KEY_SOURCE.get(key.split(":", 1)[0]) in failed}
        save_seen(set(keys) | keep)
        if new:
            _log.info("Updates verfügbar: %s", ", ".join(new))
            self.notify("Updates verfügbar", summary(new) + " · Einstellungen › System › Updates")
