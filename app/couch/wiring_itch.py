"""itch.io im Store (Bauplan E100): RPC store.* für itch, Laden und Installieren, Konto für die
Einstellungen.

Hängt sich wie wiring_launchers über wiring.store_routes ein (Teil "itch" direkt nach "store" in
wiring.PARTS); scheitert er, läuft der übrige Store weiter. Stöbern über die RSS-Feeds (store/itch.py,
ohne Konto), alles mit Konto über api.itch.io (store/itch_account.py: OAuth mit PKCE, nie ein
Kennwort). Kaufen, kostenlos holen und Spiele, die nur im Browser laufen, gehen im eigenen Browser
(store.buy, store.web); store.web gibt es nur für itch.io.

ItchDownloads (wie GOG über wiring_store._FolderQueue, eines nach dem anderen):
  1. Datei wählen (itch_install.pick_upload: Windows zuerst), Platz prüfen (2,2 × Größe + 2 GB).
  2. Ordner <Spiele-Ordner>/itch.io/<Titel> sofort mit Marker (entry_id, Zufallswert); Fortsetzen nur
     im nachweislich eigenen Ordner.
  3. Laden nach <ziel>/.itch-setup/<upload>-<datei>.part (Range; nach einem Neustart weiter; die
     signierte CDN-Adresse je Versuch frisch, der Schlüssel geht nie ans CDN).
  4. Je Dateiart: Archiv entpacken (installer/extract.unpack_into), Inno-/NSIS-/MSI-Installer still
     über Proton im Prefix des späteren Spiels, eine einzelne .exe, ein AppImage oder ein
     Linux-Programm in den Ordner (x-Bit).
  5. Startdatei wie beim Archiv-Installer bewerten; unklar = Marke needs-exe („Startdatei wählen“).
  6. .itch-setup in den Papierkorb, Eintrag in der Bibliothek (archive "itch.io", detail.itch,
     detail.store "itchio"), <state>/itch-installed.json, gezielt die Bilder (Kunstquelle itch).
  7. Abbruch und Fehler räumen nur den eigenen Ordner und den Prefix eines nie eingetragenen Spiels weg.
Deinstalliert wird in der Bibliothek (Papierkorb, trash.STORE_LEVELS „itch.io“).

Für die Einstellungen (B2): wiring.itch_status(), wiring.itch_logout(), wiring.itch_set_client(cid).
Unbestätigt, nur mit Attrappen geprüft: Anmeldung, owned-keys, uploads, CDN-Download, die stillen
Schalter von NSIS und MSI und ob „Kostenlos holen“ einen Schlüssel im Konto anlegt.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import Future
from typing import Any

from . import log, paths, wiring_store
from .store import gog_install, itch, itch_account, itch_install
from .store.cache import StoreError
from .store.itch_account import ItchError, ItchLoginNeeded

_log = log.get("wiring.itch")

STORE_ID = "itch"
LEVEL = "itch.io"                    # Store-Ebene im Spiele-Ordner und archive der Einträge (trash.STORE_LEVELS)
CLIENT_KEY = "store.itch_client_id"  # öffentliche Kennung der eigenen OAuth-App (E22: genau ein Schlüssel)
ITCH_STATES = {"queued": "Wartet", "downloading": "Lädt", "installing": "Wird installiert …",
               "cancelling": "Wird abgebrochen …", "waiting": "Wartet auf itch.io …"}
# Netz oder itch.io kurz weg (E99): so lange warten, bevor es noch einmal versucht wird (zusammen gut 15 min)
ITCH_RETRY_S = (30, 60, 120, 240, 480)
UPLOADS_TTL_S = 120.0                # Dateiliste eines Spiels für die Spielseite so lange merken
LOGIN_STEP_S = 2.0
LOGIN_WATCH_S = 15 * 60              # so lange wartet die Anmeldung darauf, dass der Browser wieder zu ist
BROWSER_APPEAR_S = 60.0              # so lange darf der Browser zum Erscheinen brauchen
SEARCH_URL = "https://itch.io/search?type=games&q=%s"
BROWSE_URL = "https://itch.io/games/platform-windows"

MSG_GONE = "Dieses Spiel gibt es nicht."
MSG_SEARCH_BROWSER = "Die Suche von itch.io öffnet sich im Browser."
MSG_UNINSTALL = "itch.io-Spiele deinstallierst du in der Bibliothek."
MSG_NOT_OWNED = "Dieses Spiel gehört dir (noch) nicht. Kaufe es zuerst."
MSG_FREE_NO_KEY = "Dieses Spiel steht noch nicht in deinem itch.io-Konto. Hol es zuerst auf seiner itch.io-Seite."
MSG_OFFLINE = "itch.io ist gerade nicht erreichbar. Versuche es gleich noch einmal."
MSG_INSTALLED = "Das Spiel ist schon installiert."
MSG_NOT_RUNNING = "Dieser Download läuft nicht."


def _card_id(params: Any) -> str:
    """Karten-ID "<benutzer>/<slug>" aus den Parametern ("" wenn ungültig)."""
    raw = str((params or {}).get("id") or "").strip().lower() if isinstance(params, dict) else ""
    return raw if itch.valid_id(raw) else ""


def _count(value: Any) -> int:
    """Positive ganze Zahl oder 0 (Felder aus Dateien und Antworten)."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# ---------------------------------------------------------------- Warteschlange
class ItchDownloads(wiring_store._FolderQueue):
    """itch.io-Spiele laden und installieren, eines nach dem anderen (E100). Gespeichert in
    <state>/store-itch.json (0600); nach einem Neustart geht es im eigenen Ordner weiter."""
    STORE, FILE, KEY = STORE_ID, "store-itch.json", "id"
    SAVED = ("id", "title", "state", "percent", "target", "entry_id", "token",
             "game_id", "key_id", "upload_id", "platform", "kind", "cover")
    STATES = ITCH_STATES
    LEVEL, SETUP_DIR, NAME, MARKER_STORE = LEVEL, itch_install.SETUP_DIR, "itch.io", STORE_ID
    LOG_NAME, REGISTRY = "itch-install", "itch-installed.json"
    EXPECTED = (ItchError, OSError)

    def __init__(self, wiring: Any, account: "itch_account.ItchAccount", store: "itch.ItchStore | None" = None,
                 clock=time.time):
        self.account = account
        self.store = store
        self.clock = clock
        super().__init__(wiring)

    # ------------------------------------------------------------ Datei
    def _valid(self, raw: dict) -> bool:
        return itch.valid_id(raw.get("id")) and _count(raw.get("game_id")) > 0

    def _restored(self, job: dict) -> dict | None:
        job = super()._restored(job)
        for key in ("key_id", "upload_id"):
            if not _count(job.get(key)):
                job.pop(key, None)
        if job.get("platform") not in ("windows", "linux"):
            job.pop("platform", None)
        if not isinstance(job.get("kind"), str):
            job.pop("kind", None)
        if not str(job.get("cover") or "").startswith("https://"):
            job.pop("cover", None)
        return job

    def _transient(self, e: BaseException) -> bool:
        if isinstance(e, ItchError):
            return itch_install.transient(e)
        return itch_install.transient(e) or wiring_store._net_error(e)

    def _retries(self) -> tuple:
        return ITCH_RETRY_S

    def _marker_extra(self, job: dict) -> dict:
        return {"game": job["id"], "game_id": job["game_id"]}

    def _free_target(self, root: str, title: str) -> str:
        # Ein Titel nur aus Zeichen wie „★“ ergäbe sonst den Namen „GOG-Spiel“
        return gog_install.free_target(root, title if re.search(r"\w", title or "") else "itch.io-Spiel")

    def add(self, card_id: str, title: str, game_id: int, key_id: int | None = None, cover: str = "") -> None:
        with self.lock:
            if not any(j["id"] == card_id for j in self.queue):
                job = {"id": card_id, "title": title, "state": "queued", "percent": 0, "game_id": int(game_id),
                       "cancel": threading.Event()}
                if _count(key_id):
                    job["key_id"] = int(key_id)
                if str(cover or "").startswith("https://"):
                    job["cover"] = cover
                self.queue.append(job)
                self._save()
        self.start()

    # ------------------------------------------------------------ Verzeichnis der Store-Spiele
    @classmethod
    def installed(cls) -> dict:
        """{Karten-ID: {entry, upload_id, build_id, updated_at, installed_at, platform, game_id}}."""
        try:
            with open(cls.registry_file(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if itch.valid_id(k) and isinstance(v, dict) and isinstance(v.get("entry"), str)}

    def _entry_ids(self) -> set:
        return {v["entry"] for v in self.installed().values()}

    def _remember(self, card_id: str, info: dict) -> None:
        data = self.installed()
        data[card_id] = info
        wiring_store._write_private(self.registry_file(), data)

    # ------------------------------------------------------------ Installieren
    def _pick(self, job: dict) -> tuple:
        """(upload, platform). Nach einem Neustart dieselbe Datei wie vorher, solange es sie gibt
        (sonst passte das halbe .part nicht)."""
        uploads = self.account.uploads(job["game_id"], job.get("key_id") or None)
        upload, platform = itch_install.pick_upload(uploads)
        old = _count(job.get("upload_id"))
        if old and upload.get("id") != old:
            same = next((u for u in uploads if u.get("id") == old), None)
            if same is not None and job.get("platform") in ("windows", "linux") \
                    and (same.get("platforms") or {}).get(job["platform"]):
                upload, platform = same, job["platform"]
        return upload, platform

    def _install(self, job: dict) -> str:
        cancel = job["cancel"]
        upload, platform = self._pick(job)
        kind = itch_install.kind_of(upload.get("filename"), platform)
        if kind == "unsupported":
            raise itch_install.InstallError("Diese Datei von itch.io kann der PC5 Launcher nicht installieren.")
        size = max(0, int(upload.get("size") or 0))
        root = wiring_store.store_root(self.app, LEVEL)
        os.makedirs(root, exist_ok=True)
        target = self._resume_target(job, root)
        have = gog_install.folder_bytes(os.path.join(target, self.SETUP_DIR)) if target else 0
        import shutil
        if size and shutil.disk_usage(root).free < size * 2.2 - have + 2 * 10**9:
            raise itch_install.InstallError("Auf %s ist nicht genug Platz (%d GB nötig)."
                                            % (root, max(1, round((size * 2.2 - have) / 1e9))))
        if target:
            _log.info("itch.io: %s setzt fort in %s (%d MB schon da)", job["title"], target, have // 2**20)
            self._set(job, target=target)
        else:
            target = self._new_target(job, root)
        self._set(job, save=True, upload_id=int(upload["id"]), platform=platform, kind=kind)
        setup = os.path.join(target, self.SETUP_DIR)
        os.makedirs(setup, exist_ok=True)
        # Die Upload-Nummer im Namen: eine ersetzte Datei gleichen Namens setzt nie ein altes .part fort
        final = os.path.join(setup, "%d-%s" % (int(upload["id"]), itch_install.safe_name(upload.get("filename"))))
        if not os.path.isfile(final):
            part = final + itch_install.PART
            self._download(job, upload, part, size)
            final = itch_install.finish_part(part)
        if cancel.is_set():
            raise itch_install.InstallError("Abgebrochen.")
        self._set(job, state="installing", percent=100)
        placed = self._place(job, kind, final, target, platform, upload)
        if cancel.is_set():
            raise itch_install.InstallError("Abgebrochen.")
        if placed["platform"] == "linux":
            # zip verliert das Ausführungsrecht oft: Startdatei und die übrigen erkannten Programme
            from .installer.jobs import make_executable
            for rel in [placed["exe"]] + [c["path"] for c in placed["candidates"]]:
                if rel:
                    try:
                        make_executable(target, rel)
                    except OSError as e:
                        _log.warning("itch.io: Ausführungsrecht für %s nicht gesetzt: %s", rel, e)
        from . import files, wiring_library
        try:
            files.gio_trash(setup)
        except (OSError, ValueError) as e:
            _log.warning("itch.io: Download-Ordner nicht in den Papierkorb: %s", e)
        now = self.clock()
        origin = {"id": job["id"], "game_id": job["game_id"], "upload_id": int(upload["id"]),
                  "build_id": _count(upload.get("build_id")), "updated_at": str(upload.get("updated_at") or ""),
                  "page": itch.url_of(job["id"])}
        if _count(job.get("key_id")):
            origin["key_id"] = job["key_id"]
        record = {"id": job["entry_id"], "title": job["title"], "platform": placed["platform"], "install_dir": target,
                  "exe": placed["exe"], "args": "",
                  "exe_candidates": [{"path": c["path"], "score": c.get("score", 0)} for c in placed["candidates"]],
                  "created": now, "archive": LEVEL, "marker": job["token"], "itch": origin}
        added = wiring_library.add_local_record(self.app, record)
        if not added:
            raise itch_install.InstallError("Das Spiel ist installiert, ließ sich aber nicht in die Bibliothek eintragen.")
        self._remember(job["id"], {"entry": added, "upload_id": origin["upload_id"], "build_id": origin["build_id"],
                                   "updated_at": origin["updated_at"], "installed_at": now,
                                   "platform": placed["platform"], "game_id": job["game_id"]})
        self._fetch_art(added, job)
        _log.info("itch.io: %s installiert nach %s (%s)", job["title"], target, placed["exe"] or "Startdatei offen")
        return target

    def _download(self, job: dict, upload: dict, part: str, size: int) -> None:
        """Lädt (bzw. setzt fort) nach part. Die Adresse ist signiert und läuft ab: je Versuch neu."""
        done = [os.path.getsize(part) if os.path.isfile(part) else 0]
        last = [0.0]
        rate = wiring_store._Rate()
        job["rate"] = rate
        total = max(1, size)

        def progress(n: int) -> None:
            done[0] += n
            job["tries"] = 0                    # es kommt wieder etwas: die Wartezeiten fangen neu an
            if time.monotonic() - last[0] > 0.5:
                last[0] = time.monotonic()
                rate.update(done[0])
                self._set(job, state="downloading", percent=min(99, int(done[0] * 100 / total)) if size else 0,
                          done=done[0], total=size)
        self._set(job, state="downloading", percent=min(99, int(done[0] * 100 / total)) if size else 0,
                  done=done[0], total=size)
        key_id = job.get("key_id") or None
        session = self.account.download_session(job["game_id"], key_id)
        url = self.account.download_url(int(upload["id"]), key_id, session)
        itch_install.download(url, part, progress, job["cancel"])

    def _place(self, job: dict, kind: str, path: str, target: str, platform: str, upload: dict) -> dict:
        """Die geladene Datei je Art an ihren Platz bringen: {exe, candidates, platform}."""
        from .installer import extract, trash
        if kind == "archive":
            result = extract.unpack_into(path, target, job["token"], title=job["title"], platform=platform,
                                         keep=(self.SETUP_DIR,), cancel=job["cancel"])
            if not result.get("ok"):
                if result.get("code") == "cancelled":
                    raise itch_install.InstallError("Abgebrochen.")
                raise itch_install.InstallError(str(result.get("message") or "Das Archiv ließ sich nicht entpacken."))
            if result.get("kind") == "windows-installer" and result.get("installer"):
                installer = os.path.join(target, result["installer"])
                itype = itch_install.installer_type(installer)
                if itype:
                    # Ein Installer im Archiv (etwa setup.exe mit setup-1.bin daneben): an Ort und Stelle
                    return self._silent(job, itype, installer, target)
            return self._choose(result.get("candidates") or [], bool(result.get("needs_choice")),
                                str(result.get("platform") or platform))
        if kind in ("exe", "msi"):
            itype = itch_install.installer_type(path)
            if itype:
                return self._silent(job, itype, path, target)
            name = itch_install.safe_name(upload.get("filename"))
            trash.rename_noreplace(path, os.path.join(target, name))     # die .exe ist das Spiel
            return {"exe": name, "candidates": [{"path": name, "score": 100}], "platform": "windows"}
        if kind in ("appimage", "linux-bin"):
            name = itch_install.safe_name(upload.get("filename"))
            trash.rename_noreplace(path, os.path.join(target, name))
            return {"exe": name, "candidates": [{"path": name, "score": 100}], "platform": "linux"}
        raise itch_install.InstallError("Diese Datei von itch.io kann der PC5 Launcher nicht installieren.")

    @staticmethod
    def _choose(candidates: list, needs_choice: bool, platform: str) -> dict:
        exe = candidates[0]["path"] if candidates and not needs_choice else ""
        return {"exe": exe, "candidates": candidates, "platform": platform if platform in ("windows", "linux") else "windows"}

    def _silent(self, job: dict, itype: str, installer: str, target: str) -> dict:
        """Inno Setup, NSIS oder MSI unsichtbar über Proton in den Spielordner (unbestätigt für NSIS/MSI)."""
        from .installer import extract
        command = itch_install.silent_command(itype, installer, target)
        rc = self._run_proton(job, job["entry_id"], installer, command, detail={"store": "itchio"})
        if rc != 0:
            raise itch_install.InstallError("Der Installer ist nicht durchgelaufen (Rückgabewert %s)." % rc)
        found = extract.start_candidates(target, job["title"], skip=(self.SETUP_DIR,), platform="windows")
        if not found["candidates"]:
            raise itch_install.InstallError("Der Installer ist durchgelaufen, aber im Spielordner liegt kein Programm.")
        return self._choose(found["candidates"], found["needs_choice"], "windows")

    def _fetch_art(self, entry_id: str, job: dict) -> None:
        """Gezielt das Cover von itch.io als Hintergrund (Kunstquelle itch, E100); die Titelsuche der
        anderen Quellen findet itch-Spiele meist nicht."""
        cover = str(job.get("cover") or "")
        if not cover and self.store is not None:
            cover = str((self.store.remembered(job["id"]) or {}).get("image") or "")
        cover = itch.cover_of(cover)
        part = getattr(self.wiring, "library_part", None)
        worker = getattr(part, "worker", None)
        if not cover or worker is None:
            return
        try:
            worker.fetch_candidate(entry_id, {"source": "itch", "ref": job["id"], "name": job["title"],
                                              "data": {"cover": cover}})
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: Bilder für %s", job["title"])


# ---------------------------------------------------------------- Store-Seite
class ItchShop:
    """RPC-Funktionen von itch.io und das Konto (Stand der eigenen Spiele, Anmeldung)."""

    def __init__(self, wiring: Any, store: "itch.ItchStore | None" = None,
                 account: "itch_account.ItchAccount | None" = None):
        self.wiring = wiring
        self.app = wiring.app
        self.rpc = self.app.rpc
        self.store = store or itch.ItchStore(os.path.join(paths.cache_dir(), "store", "itch"))
        cfg = getattr(wiring, "config", None)       # Proxy: schreibt in der Hauptschleife (Beobachter)
        if cfg is None:
            cfg = self.app.config
        self.account = account or itch_account.ItchAccount(
            paths.config_dir(), paths.state_dir(),
            get_client=lambda: cfg.get(CLIENT_KEY, ""), set_client=lambda cid: cfg.set(CLIENT_KEY, cid))
        self.downloads = ItchDownloads(wiring, self.account, self.store)
        self.lock = threading.Lock()
        self.owned_state = {"at": 0.0, "error": False}
        self.search_browser = False          # die API-Suche ist für diesen Schlüssel nicht erlaubt
        self.uploads_memo: dict = {}         # game_id -> (Zeit, Dateien)
        self.watching_login = False
        self.stopped = threading.Event()

    # ------------------------------------------------------------ Tabelle
    def routes(self) -> dict:
        return {"store.front": self.front, "store.list": self.list, "store.item": self.item,
                "store.install": self.install, "store.cancel": self.cancel, "store.buy": self.buy,
                "store.login": self.login, "store.uninstall": lambda params: {"ok": False, "message": MSG_UNINSTALL},
                "store.web": self.web}

    # ------------------------------------------------------------ Konto
    def _take_login(self) -> bool:
        """Was der Browser abgelegt hat, übernehmen: App-Kennung und Anmelde-Code. True, wenn sich
        dadurch etwas geändert hat."""
        changed = False
        try:
            changed = bool(self.account.take_client_id())
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: App-Kennung übernehmen")
        try:
            if self.account.take_code():
                changed = True
                with self.lock:
                    self.search_browser = False
                    self.owned_state.update(at=0.0, error=False)
                _log.info("itch.io: angemeldet")
        except ItchLoginNeeded as e:
            _log.warning("itch.io-Anmeldung abgelehnt: %s", e)
        except ItchError as e:
            _log.warning("itch.io-Anmeldung: %s", e)        # Netz: der Code bleibt für den nächsten Versuch
        except Exception as e:  # noqa: BLE001 (Platte voll o. ä.: store.front und store.install laufen weiter)
            _log.warning("itch.io-Anmeldung nicht übernommen (%s)", type(e).__name__)   # nur die Art: kein Code
        return changed

    def owned_keys(self, force: bool = False) -> list:
        """Schlüssel aus owned-keys (alle Klassifikationen). Ein Netzfehler heißt nie „gehört dir nicht“
        (E99): dann gilt der letzte gute Stand, und erst nach ERROR_TTL_S wird wieder gefragt."""
        self._take_login()
        acct = self.account
        if not acct.logged_in():
            return []
        with self.lock:
            recent = self.owned_state["error"] and time.monotonic() - self.owned_state["at"] < wiring_store.ERROR_TTL_S
        if recent and not force:
            return acct.last_owned()
        try:
            keys, error = acct.owned(force=force), False
        except ItchLoginNeeded as e:
            _log.warning("itch.io: %s", e)
            return []
        except (ItchError, OSError) as e:       # OSError: Platte voll o. ä., wie ein vorübergehender Fehler
            _log.warning("itch.io: eigene Spiele unbekannt: %s", e if isinstance(e, ItchError) else type(e).__name__)
            keys, error = acct.last_owned(), True
        with self.lock:
            self.owned_state.update(at=time.monotonic(), error=error)
        return keys

    def _key(self, card_id: str) -> dict | None:
        """Schlüssel eines Spiels aus dem letzten Stand (ohne Netz)."""
        return next((k for k in self.account.last_owned() if k.get("id") == card_id), None)

    def owned_cards(self) -> list:
        """Reiter „Deine Spiele“: nur Spiele (keine Bücher, Werkzeuge, Soundtracks), je Spiel eine Karte."""
        items, seen = [], set()
        for k in self.owned_keys():
            item = k.get("item")
            if str(k.get("classification") or "game") != "game" or not isinstance(item, dict) \
                    or not itch.valid_id(item.get("id")) or item["id"] in seen:
                continue
            seen.add(item["id"])
            items.append(item)
        self.store.remember(items)
        return self.annotate([dict(itch.ItchStore.card(i), price="") for i in items])

    def installed_info(self, card_id: str) -> dict | None:
        return ItchDownloads.installed().get(card_id)

    def annotate(self, cards: list) -> list:
        owned = {k.get("id") for k in self.account.last_owned()}
        done = ItchDownloads.installed()
        busy = {j["id"] for j in self.downloads.jobs()}
        for c in cards:
            cid = str(c.get("id") or "")
            entry_id = str((done.get(cid) or {}).get("entry") or "")
            c["installed"] = bool(entry_id) and self.app.library.get(entry_id) is not None
            c["entryId"] = entry_id if c["installed"] else ""
            c["downloading"] = cid in busy
            c["owned"] = cid in owned or c["installed"]
            c["wishlist"] = False
        return cards

    def search_mode(self) -> str:
        return "api" if self.account.logged_in() and not self.search_browser else "browser"

    def _uploads(self, key: dict) -> list | None:
        """Dateien eines eigenen Spiels für die Spielseite (gemerkt); None, wenn itch.io nicht antwortet."""
        game_id = _count(key.get("game_id"))
        if not game_id:
            return None
        with self.lock:
            memo = self.uploads_memo.get(game_id)
        if memo is not None and time.monotonic() - memo[0] < UPLOADS_TTL_S:
            return memo[1]
        try:
            uploads = self.account.uploads(game_id, key.get("key_id") or None)
        except ItchError as e:
            _log.info("itch.io: Dateien von %s unbekannt: %s", key.get("id"), e)
            return None
        with self.lock:
            self.uploads_memo[game_id] = (time.monotonic(), uploads)
        return uploads

    # ------------------------------------------------------------ RPC (Arbeits-Thread)
    def front(self, params: dict) -> dict:
        """Anmeldung, Genres und „Deine Spiele“; die Listen holt die Seite mit store.list (E70)."""
        self._take_login()
        acct = self.account
        items = self.owned_cards() if acct.logged_in() else []
        logged = acct.logged_in()                   # ein abgelehnter Schlüssel meldet beim Abruf ab (401)
        sections = [{"id": "owned", "title": "Deine Spiele", "items": items}] if logged else []
        out = {"ok": True, "loggedIn": logged, "configured": acct.configured(), "genres": itch.genres(),
               "sections": sections, "search": self.search_mode()}
        name = acct.name() if logged else ""
        if name:
            out["account"] = name
        return out

    def list(self, params: dict) -> dict:
        empty = {"items": [], "total": 0, "more": False}
        tab = str(params.get("tab") or "")
        try:
            page = max(0, int(params.get("page") or 0))
        except (TypeError, ValueError):
            page = 0
        if tab == "owned":
            items = self.owned_cards()
            return {"ok": True, "items": items, "total": len(items), "more": False}
        if tab == "search":
            return self._search(str(params.get("query") or ""), page)
        try:
            result = self.store.browse(tab, page, str(params.get("genre") or ""))
        except (StoreError, ValueError) as e:
            return {"ok": False, "message": str(e), **empty}
        self.annotate(result["items"])
        return {"ok": True, **result}

    def _search(self, query: str, page: int) -> dict:
        """Suche über die API (angemeldet); sonst bzw. ohne Erlaubnis im Browser (web: "search")."""
        empty = {"items": [], "total": 0, "more": False}
        browser = {"ok": False, "message": MSG_SEARCH_BROWSER, "web": "search", **empty}
        text = re.sub(r"\s+", " ", query).strip()
        if not text:
            return {"ok": True, **empty}
        if self.search_mode() != "api":
            return browser
        try:
            found = self.account.search(text, page)
        except ItchLoginNeeded:
            return browser
        except ItchError as e:
            return {"ok": False, "message": str(e), **empty}
        if found is None:
            with self.lock:
                self.search_browser = True
            return browser
        self.store.remember(found.get("entries") or [])
        items = self.annotate(list(found.get("items") or []))
        return {"ok": True, "items": items, "total": int(found.get("total") or len(items)), "more": bool(found.get("more"))}

    def item(self, params: dict) -> dict:
        gid = _card_id(params)
        if not gid:
            return {"ok": False, "message": MSG_GONE}
        card = params.get("card") if isinstance(params.get("card"), dict) else None
        key = self._key(gid)
        if key is not None and isinstance(key.get("item"), dict):
            self.store.remember([key["item"]])          # eigenes Spiel, das in keiner Liste stand
        try:
            d = self.store.details(gid, card)
        except StoreError as e:
            return {"ok": False, "message": str(e)}
        self.annotate([d])
        acct = self.account
        d["loggedIn"] = acct.logged_in()
        d["configured"] = acct.configured()
        d["browserOnly"] = bool(d.get("web")) and not d.get("installable")
        # „Zahl, was du willst“: Die Feeds unterscheiden das nicht von „kostenlos“ (beide 0,00); nur ein
        # Eintrag, der es weiß, setzt es (unbestätigt)
        d["pwyw"] = bool((self.store.remembered(gid) or {}).get("pwyw"))
        d["updateHint"] = ""
        if key is not None and d["loggedIn"]:
            uploads = self._uploads(key)
            if uploads is not None:
                try:
                    itch_install.pick_upload(uploads)
                    # Die eigenen Dateien gelten vor dem Feed (der nennt bei Browserspielen oft nur <html>)
                    d["installable"], d["browserOnly"] = True, False
                except ItchError as e:
                    d["browserOnly"] = bool(e.browser_only)
                    if not d["installed"]:
                        d["installable"] = False
                        d["hint"] = str(e)
                info = self.installed_info(gid)
                if d["installed"] and info:
                    d["updateHint"] = itch_install.update_hint(info, uploads)
        job = next((j for j in self.downloads.jobs() if j["id"] == gid), None)
        d["job"] = {"percent": job.get("percent", 0), "state": job["state"],
                    "text": ITCH_STATES.get(job["state"], "")} if job else None
        d["library"] = wiring_store.target_view(self.app, LEVEL)
        return {"ok": True, "item": d}

    def install(self, params: dict) -> dict:
        gid = _card_id(params)
        if not gid:
            return {"ok": False, "message": MSG_GONE}
        acct = self.account
        if not acct.configured():
            return {"ok": False, "message": itch_account.SETUP_FIRST}
        if not acct.logged_in():
            return {"ok": False, "message": itch_account.LOGIN_FIRST}
        info = self.installed_info(gid)
        if info and self.app.library.get(info["entry"]) is not None:
            return {"ok": False, "message": MSG_INSTALLED}
        key = next((k for k in self.owned_keys(force=True) if k.get("id") == gid), None)
        known = self.store.remembered(gid) or {}
        free = bool(known) and bool(itch.ItchStore.card(known).get("free"))
        game_id = _count((key or {}).get("game_id"))
        if not game_id and free and _count(known.get("game_id")):
            game_id = _count(known.get("game_id"))      # kostenlos ohne Schlüssel: nur mit bekannter Nummer
        if not game_id:
            if not acct.logged_in():                    # itch.io hat die Anmeldung eben abgelehnt (E99)
                return {"ok": False, "message": itch_account.LOGIN_AGAIN}
            if self.owned_state["error"]:
                return {"ok": False, "message": MSG_OFFLINE}
            return {"ok": False, "message": MSG_FREE_NO_KEY if free else MSG_NOT_OWNED}
        key_id = _count((key or {}).get("key_id")) or None
        title = str((key or {}).get("title") or known.get("title") or params.get("title") or "Das Spiel")
        cover = str((key or {}).get("cover") or known.get("image") or "")
        try:
            # gleich sagen, wenn es keine passende Datei gibt (sonst käme das erst als Mitteilung)
            itch_install.pick_upload(acct.uploads(game_id, key_id))
        except ItchLoginNeeded:
            return {"ok": False, "message": itch_account.LOGIN_AGAIN}
        except ItchError as e:
            if e.browser_only:
                return {"ok": False, "message": str(e), "web": "page"}
            if not e.transient:
                return {"ok": False, "message": str(e)}
            # Netz kurz weg: Die Warteschlange versucht es selbst noch einmal (E99)
        self.downloads.add(gid, title, game_id, key_id, cover)
        _log.info("Store: itch.io %s (%s) wird installiert", title, gid)
        return {"ok": True, "message": "„%s“ wird im Hintergrund geladen und installiert. Den Fortschritt siehst du "
                                       "unter Downloads." % title}

    # ------------------------------------------------------------ RPC (Hauptschleife)
    def cancel(self, params: dict) -> dict:
        gid = _card_id(params)
        if not gid or not self.downloads.cancel(gid):
            return {"ok": False, "message": MSG_NOT_RUNNING}
        return {"ok": True, "message": "Der Download wird abgebrochen."}

    def buy(self, params: dict) -> Any:
        """Spielseite im Browser: kaufen, „Kostenlos holen“ oder „No thanks, just take me to the
        downloads“. Angemeldet wird nachgesehen, ob das Spiel danach im Konto steht (E71)."""
        gid = _card_id(params)
        if not gid:
            return {"ok": False, "message": MSG_GONE}
        if self.account.logged_in():
            threading.Thread(target=self.wiring.store_watch_purchase,
                             args=(STORE_ID, gid, str(params.get("title") or "Das Spiel")),
                             name="couch-store-kauf", daemon=True).start()
        return self.wiring.store_open_url(itch.url_of(gid), bool(params.get("closeOther")))

    def owned_check(self, pid: Any) -> bool:
        """Für die Kaufbeobachtung (wiring.store_extra_owned)."""
        return str(pid) in {k.get("id") for k in self.owned_keys(force=True)}

    def login(self, params: dict) -> Any:
        """Ohne App-Kennung zuerst die Einrichtung (Seite der OAuth-Apps, close_after itch-app), sonst
        die Anmeldung mit PKCE (close_after itch-login). Nie ein Kennwort.
        Die Dateiarbeit (Kennung übernehmen, PKCE-Datei mit fsync) läuft im Arbeits-Thread: Bei viel
        Plattenlast hielte sie sonst die Hauptschleife an. Das Öffnen selbst danach in der Hauptschleife;
        die Antwort kommt als Future (E100)."""
        close_other = bool((params or {}).get("closeOther"))
        prep = self.wiring._bg(self._login_target)
        if not isinstance(prep, Future):            # Verdrahtung ohne Arbeits-Threads
            return self._login_open(prep, close_other)
        out: Future = Future()

        def step(done: Future) -> None:             # Hauptschleife
            try:
                result = self._login_open(done.result(), close_other)
            except BaseException as e:  # noqa: BLE001 (geht als Fehler an die Seite)
                out.set_exception(e)
                return
            if isinstance(result, Future):          # Rückfrage „anderes schließen?“ (shell.launch)
                result.add_done_callback(lambda f: _chain(f, out))
            else:
                out.set_result(result)
        prep.add_done_callback(lambda f: self.wiring.main.post(step, f))
        return out

    def _login_target(self) -> dict:
        """Arbeits-Thread: {url, close} der Einrichtung bzw. Anmeldung oder {ok: False, message}."""
        acct = self.account
        try:
            acct.take_client_id()
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: App-Kennung übernehmen")
        if not acct.configured():
            return {"url": itch_account.APPS_URL, "close": "itch-app"}
        try:
            return {"url": acct.login_url(), "close": "itch-login"}
        except ItchError as e:
            return {"ok": False, "message": str(e)}
        except OSError as e:
            _log.warning("itch.io: Anmeldung nicht vorbereitet (%s)", type(e).__name__)
            return {"ok": False, "message": "Die Anmeldung bei itch.io ließ sich nicht vorbereiten."}

    def _login_open(self, target: dict, close_other: bool) -> Any:
        """Hauptschleife: den Browser öffnen und auf das Ende der Anmeldung achten."""
        if not target.get("url"):
            return target
        with self.lock:
            self.owned_state["at"] = 0.0
        self._watch_login()
        return self.wiring.store_open_url(target["url"], close_other, target["close"])

    def web(self, params: dict) -> Any:
        """itch.io-Suche bzw. Spielseite im Browser (E100)."""
        params = params or {}
        what = str(params.get("what") or "")
        if what == "search":
            text = re.sub(r"\s+", " ", str(params.get("query") or "")).strip()[:100]
            url = SEARCH_URL % urllib.parse.quote(text, safe="") if text else BROWSE_URL
        elif what == "page":
            gid = _card_id(params)
            if not gid:
                return {"ok": False, "message": MSG_GONE}
            url = itch.url_of(gid)
        else:
            return {"ok": False, "message": "Das geht hier nicht."}
        return self.wiring.store_open_url(url, bool(params.get("closeOther")))

    # ------------------------------------------------------------ nach der Anmeldung
    def _watch_login(self) -> None:
        with self.lock:
            if self.watching_login:
                return
            self.watching_login = True
        threading.Thread(target=self._after_login, name="couch-itch-anmeldung", daemon=True).start()

    def _after_login(self) -> None:
        """Ist der Browser wieder zu (er schließt sich nach der Anmeldung selbst), Kennung bzw. Code
        übernehmen und die Store-Seite neu laden lassen (store.owned mit id '')."""
        try:
            began = time.monotonic()
            seen = False
            while not self.stopped.wait(LOGIN_STEP_S) and time.monotonic() - began < LOGIN_WATCH_S:
                try:
                    running = self.wiring.main.call(self.app.procs.by_entry, "app:browser") is not None
                except RuntimeError:            # das Home endet
                    return
                seen = seen or running
                if not running and (seen or time.monotonic() - began > BROWSER_APPEAR_S):
                    break
            for delay in (0, 1, 3, 6):
                if delay and self.stopped.wait(delay):
                    return
                if self.stopped.is_set():
                    return
                if self._take_login():
                    self._announce()
                    return
        finally:
            with self.lock:
                self.watching_login = False

    def _announce(self) -> None:
        self.wiring.main.post(lambda: self.rpc.emit("store.owned", {"store": STORE_ID, "id": ""}))

    # ------------------------------------------------------------ Einstellungen (B2)
    def status(self) -> dict:
        """{configured, loggedIn, name} für die Zeile „itch.io-Konto“."""
        try:
            self.account.take_client_id()
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: App-Kennung übernehmen")
        acct = self.account
        logged = acct.logged_in()
        return {"configured": acct.configured(), "loggedIn": logged, "name": acct.name() if logged else ""}

    def logout(self) -> dict:
        """Abmelden (widerruft den Schlüssel bei itch.io, ohne Netz trotzdem). Blockiert (Netz):
        im Arbeits-Thread rufen."""
        try:
            self.account.logout()
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: abmelden")
            return {"ok": False, "message": "Das Abmelden bei itch.io hat nicht geklappt."}
        with self.lock:
            self.owned_state.update(at=0.0, error=False)
            self.search_browser = False
            self.uploads_memo.clear()
        self._announce()
        return {"ok": True, "message": "Du bist bei itch.io abgemeldet."}

    def set_client(self, cid: Any) -> dict:
        """App-Kennung setzen ("" löscht sie); geprüft: 32 Zeichen 0–9, a–f."""
        text = str(cid or "").strip().lower()
        if text and not itch_account.valid_client_id(text):
            return {"ok": False, "message": "Die App-Kennung hat 32 Zeichen aus 0–9 und a–f."}
        try:
            self.account.set_client_id(text)
        except Exception:  # noqa: BLE001
            _log.exception("itch.io: App-Kennung speichern")
            return {"ok": False, "message": "Die App-Kennung ließ sich nicht speichern."}
        return {"ok": True, "message": "Die itch.io-App-Kennung ist gespeichert." if text
                else "Die itch.io-App-Kennung ist gelöscht."}

    # ------------------------------------------------------------ Haken
    def busy(self) -> str | None:
        jobs = self.downloads.jobs()
        return "Der Store lädt gerade „%s“ von itch.io." % jobs[0]["title"] if jobs else None

    def stop(self) -> None:
        self.stopped.set()
        self.downloads.stop()


def _chain(src: Future, dst: Future) -> None:
    """Ergebnis bzw. Fehler eines Futures an ein anderes weitergeben."""
    if src.cancelled():
        dst.cancel()
        return
    error = src.exception()
    if error is not None:
        dst.set_exception(error)
    else:
        dst.set_result(src.result())


def _web_route(fns: dict, params: Any) -> Any:
    """store.web nach Shop verteilen; Shops ohne eigene Seite antworten mit ok false."""
    params = params if isinstance(params, dict) else {}
    fn = fns.get(str(params.get("store") or ""))
    if fn is None:
        return {"ok": False, "message": "Das gibt es für diesen Shop nicht."}
    return fn(params)


def extend(wiring: Any) -> None:
    routes = getattr(wiring, "store_routes", None)
    if routes is None:
        return                                  # ohne den Store-Teil gibt es nichts zu erweitern
    shop = ItchShop(wiring)
    wiring.itch_shop = shop
    for name, fn in shop.routes().items():
        if name in routes:
            routes[name][STORE_ID] = fn
    # store.web gibt es nur hier (E100); die Tabelle liegt bei den anderen, weitere Shops könnten sich eintragen
    web = routes.setdefault("store.web", {})
    web[STORE_ID] = shop.web
    wiring.app.rpc.register("store.web", lambda params: _web_route(web, params))
    wiring.store_extra_owned[STORE_ID] = shop.owned_check
    # Für die Einstellungen (wiring_settings mit getattr-Rückfall, B2)
    wiring.itch_status = shop.status
    wiring.itch_logout = shop.logout
    wiring.itch_set_client = shop.set_client
    wiring.busy_checks.append(shop.busy)
    wiring.shutdown_hooks.append(shop.stop)
    # Cover per Titelsuche, sobald angemeldet (Wunsch 2026-10-24); der Abrufer läuft im eigenen Thread
    from .library.artsources import itch as itch_art
    itch_art.SEARCH, itch_art.READY = shop.account.search_games, shop.account.can_search
    if shop.downloads.queue:                    # Aufträge von vor dem Neustart (E99)
        wiring.after_ready.append(shop.downloads.start)
