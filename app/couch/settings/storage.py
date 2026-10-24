"""Bereich „Speicher“: Belegung der Datenträger, Größen der Spiele, eigene Installationen löschen.

Lesen: `df -B1 --output=target,fstype,size,used,avail <pfad>` (durch run_read), Steam-Größen aus
SizeOnDisk der Manifeste, alles andere per `du -sxb` im Hintergrund mit Zwischenspeicher. du läuft
immer mit -x und nie in FUSE-, NTFS- oder Netz-Dateisysteme hinein (dort ging am 2026-10-12 ein
rm -rf durch). Gelöscht wird hier nichts selbst: „Mit dieser Konsole installiert“ ruft die
Löschfunktion des Archiv-Installers (deps.extra["delete_local"]); fehlt sie, ist die Zeile gesperrt.
Es gibt kein „Alle auswählen“ und kein Löschen fremder Spiele (E17).

deps.extra:
    games_disk      Pfad der Spiele-Platte (Vorgabe paths.games_disk(), E104)
    internal_disk   Konsolenspeicher (Vorgabe der persönliche Ordner, realpath)
    steam_roots     Steam-Wurzeln (Vorgabe ~/.local/share/Steam); Bibliotheken aus libraryfolders.vdf
    games           callable -> [{"id", "title", "source", "path"}] (Ordner je Spiel, ohne Steam)
    local_entries   callable -> [{"id", "title", "path"}] (Einträge des Archiv-Installers);
                    fehlt es, zählen die Einträge aus games mit source == "local"
    delete_local    callable(entry_id) (Löschfunktion des Installers)
    late            callable(name) -> Dienst eines später eingehängten Teils (E103); „cleanup“ (E110)
                    gibt es, dann steht unten die Zeile „Platz schaffen“ (öffnet den Bildschirm cleanup)
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time

from .. import jsontext, paths
from . import rows as R
from .backends import TtlCache, fmt_bytes
from .rows import AREA_ORDER, Page, SettingsError

log = logging.getLogger("couch.settings.storage")

# Dateisysteme, in die du nie hineinläuft: FUSE (Portale, NTFS über ntfs-3g), NTFS, Netz
_UNSAFE_FS = ("fuse", "ntfs", "cifs", "smb", "nfs", "9p", "sshfs", "davfs", "afs", "ceph")
DU_MAX_AGE = 7 * 86400


def parse_df(text: str) -> dict | None:
    """Letzte Zeile von `df -B1 --output=target,fstype,size,used,avail`. Der Einhängepunkt darf
    Leerzeichen enthalten, deshalb von rechts zerlegen. None bei unbrauchbarer Ausgabe."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].rsplit(None, 4)
    if len(parts) != 5:
        return None
    try:
        total, used, free = int(parts[2]), int(parts[3]), int(parts[4])
    except ValueError:
        return None
    if total <= 0:
        return None
    return {"target": parts[0], "fstype": parts[1], "total": total, "used": used, "free": free}


def read_volume(run_read, path: str) -> dict:
    """{"path", "mounted", "total", "used", "free", "fstype"}. mounted=False, wenn unter path kein
    eigenes Dateisystem hängt (df nennt dann das darüberliegende)."""
    out = {"path": path, "mounted": False, "total": 0, "used": 0, "free": 0, "fstype": ""}
    rc, text, _ = run_read(["df", "-B1", "--output=target,fstype,size,used,avail", path], timeout=5)
    info = parse_df(text) if rc == 0 else None
    if info is None:
        return out
    if os.path.realpath(info["target"]) != os.path.realpath(path):
        return out
    out.update(mounted=True, total=info["total"], used=info["used"], free=info["free"], fstype=info["fstype"])
    return out


def fstype_of(path: str, mountinfo: str = "/proc/self/mountinfo") -> str:
    """Dateisystem, auf dem path liegt (längster passender Einhängepunkt). "" wenn unbekannt."""
    real = os.path.realpath(path)
    best, best_len = "", -1
    try:
        with open(mountinfo, encoding="utf-8", errors="replace") as f:
            for line in f:
                left, sep, right = line.partition(" - ")
                fields = left.split()
                if not sep or len(fields) < 5:
                    continue
                # mountinfo maskiert Leerzeichen als \040
                mp = fields[4].replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")
                if (real == mp or real.startswith(mp.rstrip("/") + "/")) and len(mp) > best_len:
                    best, best_len = right.split()[0] if right.split() else "", len(mp)
    except OSError:
        return ""
    return best


def unsafe_fs(fstype: str) -> bool:
    return not fstype or fstype.lower().startswith(_UNSAFE_FS)


def steam_libraries(roots: list[str]) -> list[str]:
    """Bibliotheksordner aus libraryfolders.vdf (nur die Felder "path")."""
    libs: list[str] = []
    for root in roots:
        root = os.path.expanduser(root)
        libs.append(root)
        for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            try:
                with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
                    libs += re.findall(r'"path"\s+"([^"]+)"', f.read())
            except OSError:
                continue
    seen, out = set(), []
    for lib in libs:
        real = os.path.realpath(lib)
        if real not in seen and os.path.isdir(os.path.join(real, "steamapps")):
            seen.add(real)
            out.append(real)
    return out


def steam_sizes(libraries: list[str]) -> list[dict]:
    """[{"id": "steam:<appid>", "title", "bytes", "path", "source": "steam"}] aus den Manifesten."""
    out, seen = [], set()
    for lib in libraries:
        for mf in sorted(glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf"))):
            try:
                with open(mf, encoding="utf-8", errors="replace") as f:
                    txt = f.read(65536)
            except OSError:
                continue

            def field(key: str, _txt=txt):
                m = re.search(r'"%s"\s+"([^"]*)"' % key, _txt)
                return m.group(1) if m else None
            appid, name = field("appid"), field("name")
            if not appid or not appid.isdigit() or appid in seen or not name:
                continue
            try:
                size = int(field("SizeOnDisk") or 0)
            except ValueError:
                size = 0
            if size <= 0:
                continue          # Werkzeuge ohne Inhalt (Laufzeiten melden 0)
            seen.add(appid)
            out.append({"id": f"steam:{appid}", "title": name, "bytes": size, "path": lib, "source": "steam"})
    return out


class FolderSizes:
    """Ordnergrößen per du, im Hintergrund und nacheinander (vielleicht läuft gerade ein Spiel),
    gemerkt in <cache>/folder-sizes.json."""

    def __init__(self, run_read, scheduler, cache_file: str, on_done=None, clock=time.time,
                 max_age: float = DU_MAX_AGE, mountinfo: str = "/proc/self/mountinfo"):
        self.run_read = run_read
        self.scheduler = scheduler
        self.cache_file = cache_file
        self.on_done = on_done
        self.clock = clock
        self.max_age = max_age
        self.mountinfo = mountinfo
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._running = False
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict:
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(jsontext.dumps(self._data))
            os.replace(tmp, self.cache_file)
        except OSError as e:
            log.warning("Größen-Zwischenspeicher nicht schreibbar: %s", e)

    def get(self, path: str) -> dict:
        """{"bytes": int|None, "pending": bool, "skipped": str}. Fehlt der Wert oder ist er alt,
        wird im Hintergrund gemessen; pending zeigt das an."""
        key = os.path.realpath(path)
        with self._lock:
            hit = self._data.get(key)
            fresh = hit is not None and self.clock() - hit.get("at", 0) < self.max_age
            if not fresh and key not in self._queue:
                self._queue.append(key)
                start = not self._running
                self._running = True
            else:
                start = False
            pending = not fresh
        if start:
            self.scheduler.background(self._work)
        if hit is None:
            return {"bytes": None, "pending": True, "skipped": ""}
        return {"bytes": hit.get("bytes"), "pending": pending and hit.get("bytes") is None,
                "skipped": hit.get("skipped", "")}

    def invalidate(self, path: str | None = None) -> None:
        with self._lock:
            if path is None:
                self._data.clear()
            else:
                self._data.pop(os.path.realpath(path), None)
        self._save()

    def _work(self) -> None:
        changed = False
        while True:
            with self._lock:
                if not self._queue:
                    self._running = False
                    break
                key = self._queue[0]
            result = self.measure(key)
            with self._lock:
                self._data[key] = {**result, "at": self.clock()}
                self._queue.pop(0)
            changed = True
        if changed:
            self._save()
            if self.on_done:
                self.on_done()

    def measure(self, real: str) -> dict:
        """Ein Ordner, blockierend (Sekunden bis Minuten). Nie durch fremde Einhängungen."""
        if not os.path.isdir(real) or os.path.islink(real):
            return {"bytes": None, "skipped": "fehlt"}
        fs = fstype_of(real, self.mountinfo)
        if unsafe_fs(fs):
            return {"bytes": None, "skipped": fs or "unbekanntes Dateisystem"}
        rc, out, _ = self.run_read(["ionice", "-c3", "nice", "-n", "19", "du", "-sxb", "--", real], timeout=600)
        first = out.split()[0] if out.split() else ""
        if rc != 0 and not first.isdigit():
            return {"bytes": None, "skipped": "nicht lesbar"}
        return {"bytes": int(first) if first.isdigit() else None, "skipped": ""}


class StorageArea:
    id = "storage"
    label = "Speicher"
    icon = "storage"
    order = AREA_ORDER["storage"]

    def __init__(self, deps):
        self.deps = deps
        x = deps.extra
        # E104/E110: keine festen Orte dieses Rechners (früher /run/media/<nutzer>/Spiele und /var/home)
        self.games_disk = x.get("games_disk") or paths.games_disk()
        self.internal = x.get("internal_disk") or os.path.realpath(paths.home())
        self._cache = TtlCache(5.0)
        self._steam = TtlCache(60.0)
        self.sizes = FolderSizes(deps.run_read, deps.scheduler,
                                 os.path.join(deps.paths.cache_dir, "folder-sizes.json"),
                                 on_done=lambda: deps.changed("storage.apps"),
                                 mountinfo=x.get("mountinfo", "/proc/self/mountinfo"))

    # --- Lesen ---------------------------------------------------------------------------

    def volumes(self) -> list[dict]:
        def load():
            vols = []
            for key, label, path in (("internal", "Konsolenspeicher", self.internal),
                                     ("games-disk", "Spiele-Platte", self.games_disk)):
                v = read_volume(self.deps.run_read, path)
                v.update(key=key, label=label)
                vols.append(v)
            return vols
        return self._cache.get("volumes", load)

    def _where(self, path: str) -> str:
        real = os.path.realpath(path)
        disk = os.path.realpath(self.games_disk)
        return "Spiele-Platte" if real == disk or real.startswith(disk + os.sep) else "Konsolenspeicher"

    def _call_list(self, name: str) -> list[dict] | None:
        fn = self.deps.extra.get(name)
        if not callable(fn):
            return None
        try:
            return [e for e in (fn() or []) if isinstance(e, dict) and e.get("id")]
        except Exception:  # noqa: BLE001
            log.exception("%s() wirft", name)
            return []

    def _local_entries(self) -> list[dict]:
        local = self._call_list("local_entries")
        if local is None:
            local = [g for g in (self._call_list("games") or []) if g.get("source") == "local"]
        return local

    def pages(self) -> list[str]:
        return ["storage", "storage.apps", "storage.local"]

    def page(self, page_id: str) -> Page:
        if page_id == "storage":
            return Page(page_id, "Speicher", self._main_rows())
        if page_id == "storage.apps":
            return Page(page_id, "Spiele und Apps", self._apps_rows())
        if page_id == "storage.local":
            return Page(page_id, "Mit dieser Konsole installiert", self._local_rows())
        raise KeyError(page_id)

    def _main_rows(self) -> list:
        rows = []
        for v in self.volumes():
            rid = f"storage.{v['key']}"
            if not v["mounted"]:
                rows.append(R.info(rid, v["label"], "Nicht verbunden", enabled=False))
                continue
            percent = round(100 * v["used"] / v["total"]) if v["total"] else 0
            rows.append(R.info(rid, v["label"], f"{fmt_bytes(v['free'])} frei von {fmt_bytes(v['total'])}",
                               note=f"{percent} % belegt"))
        rows.append(R.link("storage.apps-page", "Spiele und Apps", "storage.apps"))
        n = len(self._local_entries())
        rows.append(R.link("storage.local-page", "Mit dieser Konsole installiert", "storage.local",
                           note=f"{n} Spiele" if n != 1 else "1 Spiel"))
        if self._cleanup() is not None:
            # E110: Bildschirm cleanup; gelöscht wird dort nur über die vorhandenen Wege (Papierkörbe)
            rows.append(R.action("storage.cleanup", "Platz schaffen",
                                 hint="Zeigt Papierkörbe, schon installierte Archive, große Downloads und andere "
                                      "Platzfresser. Gelöscht wird nur nach deiner Rückfrage."))
        return rows

    def _cleanup(self):
        """Dienst „Platz schaffen“ (wiring_cleanup, E110), spät gebunden; None ohne ihn (Zeile entfällt, E20)."""
        late = self.deps.extra.get("late")
        try:
            return late("cleanup") if callable(late) else None
        except Exception:  # noqa: BLE001
            log.exception("late(cleanup)")
            return None

    def _apps_rows(self) -> list:
        x = self.deps.extra
        libs = self._steam.get("libs", lambda: steam_libraries(x.get("steam_roots") or ["~/.local/share/Steam"]))
        items = [dict(i, pending=False, skipped="") for i in self._steam.get("sizes", lambda: steam_sizes(libs))]
        for g in self._call_list("games") or []:
            path = g.get("path")
            if g.get("source") == "steam" or not path:
                continue
            s = self.sizes.get(str(path))
            items.append({"id": g["id"], "title": str(g.get("title") or g["id"]), "bytes": s["bytes"],
                          "path": str(path), "source": str(g.get("source") or ""),
                          "pending": s["pending"], "skipped": s["skipped"]})
        if not items:
            return [R.info("storage.apps.empty", "Keine Spiele gefunden.", enabled=False)]
        items.sort(key=lambda i: (-(i["bytes"] or 0), i["title"].casefold()))
        total = sum(i["bytes"] or 0 for i in items)
        rows = [R.info("storage.apps.total", "Insgesamt", fmt_bytes(total), note=f"{len(items)} Einträge")]
        names = {"steam": "Steam", "proton": "Proton", "native": "Linux", "switch": "Switch", "wiiu": "Wii U",
                 "local": "Installiert", "app": "App"}
        for i in items:
            if i["bytes"] is not None:
                value = fmt_bytes(i["bytes"])
            elif i["pending"]:
                value = "Wird berechnet …"
            else:
                value = "Unbekannt"
            src = names.get(i["source"], "")
            rows.append(R.info(f"storage.app:{i['id']}", i["title"], value,
                               note=" · ".join(p for p in (src, self._where(i["path"])) if p)))
        return rows

    def _local_rows(self) -> list:
        entries = self._local_entries()
        if not entries:
            return [R.info("storage.local.empty", "Noch nichts installiert.", enabled=False,
                           hint="Hier erscheinen Spiele, die du aus einem Archiv installiert hast.")]
        can_delete = callable(self.deps.extra.get("delete_local"))
        rows = []
        for e in sorted(entries, key=lambda e: str(e.get("title") or "").casefold()):
            title = str(e.get("title") or e["id"])
            size = ""
            if e.get("path"):
                s = self.sizes.get(str(e["path"]))
                size = fmt_bytes(s["bytes"]) if s["bytes"] is not None else ("Wird berechnet …" if s["pending"] else "")
            rows.append(R.confirm_action(
                f"storage.local.delete:{e['id']}", f"{title} löschen",
                title=f"„{title}“ löschen?",
                text="Das Spiel und seine Dateien werden von der Konsole gelöscht. Das Archiv, aus dem es "
                     "installiert wurde, bleibt erhalten.",
                ok="Löschen", enabled=can_delete,
                note=size if can_delete else "Nicht verfügbar"))
        return rows

    # --- Schreiben -----------------------------------------------------------------------

    def set(self, row_id: str, value):
        raise SettingsError("Diese Einstellung gibt es nicht.")

    def action(self, row_id: str, value=None):
        if row_id == "storage.cleanup":
            if self._cleanup() is None:
                raise SettingsError("„Platz schaffen“ ist gerade nicht verfügbar.")
            return {"ui": "cleanup"}
        head, _, entry_id = row_id.partition(":")
        if head != "storage.local.delete" or not entry_id:
            raise SettingsError("Diese Aktion gibt es nicht.")
        delete = self.deps.extra.get("delete_local")
        if not callable(delete):
            raise SettingsError("Löschen ist gerade nicht verfügbar.")
        # Nur Einträge, die der Installer selbst angelegt hat: Die ID muss in seiner Liste stehen.
        entry = next((e for e in self._local_entries() if e["id"] == entry_id), None)
        if entry is None:
            raise SettingsError("Diesen Eintrag gibt es nicht mehr.")
        delete(entry_id)
        if entry.get("path"):
            self.sizes.invalidate(str(entry["path"]))
        self._cache.drop()
        return {"message": f"„{entry.get('title') or entry_id}“ wurde gelöscht.", "refresh": True}


def make_area(deps) -> StorageArea:
    return StorageArea(deps)
