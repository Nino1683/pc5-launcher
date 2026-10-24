"""Einrichtungs-Assistent (E105, Teil P4): Verdrahtung, eingehängt über wiring.PARTS.

Wunsch 2026-10-24: „Ein Install-Wizard, der den Nutzer fragt, was er alles nutzen möchte, und dies dann für
ihn installiert, wie die Emulatoren etc.“ Bauplan docs/research/e103/SPEC.md 4.2, 4.3, 8.

  RPC        setup.state, setup.detect, setup.catalog, setup.plan, setup.start, setup.cancel, setup.later,
             setup.finish (Formen siehe Bauplan 4.3; Langsames im Arbeits-Thread)
  Ereignisse setup.progress, setup.done
  sys.state  setup {due, state}: due = der Assistent soll jetzt von selbst erscheinen (nur ohne Bestand,
             setup/state.py); Aufträge in store_jobs mit store "setup", Kreuz unter Downloads über store.cancel
  busy       „Die Einrichtung installiert gerade Emulatoren und Apps.“ (Ruhemodus, Ausschalten, Übergabe)
  D4         lief beim letzten Ende eine Installation, kommt nach dem Start eine Mitteilung; fortgesetzt wird
             über setup.start mit setup.state().resume
  wiring.setup_part  für andere Teile: emulator_rows() und open_emulator() (Einstellungen › Spiele ›
             Emulatoren, settings/games.py über deps.extra["late"]("setup_part"))

Bei setup.start setzt der Teil library.games_disk und emu.root nur, wenn etwas anderes als „automatisch“
gewählt ist, und library.roms = True. Installiert wird nur mit Flatpak in der Nutzer-Installation, nur nach
dem Druck auf „Installieren“; nie deinstalliert, nie Rechte geändert (SAFETY 13). Im Prüfmodus nur über die
Attrappe aus COUCH_SETUP_FLATPAK, umu nur von COUCH_SETUP_UMU_URL (127.0.0.1).
"""
from __future__ import annotations

import os
from typing import Any

from . import log, paths, setup as setup_pkg
from .setup import catalog, detect as detect_mod, folders, jobs as jobs_mod, plan as plan_mod, state as state_mod

_log = log.get("wiring.setup")


def _bad(message: str) -> dict:
    return {"ok": False, "message": message}


class SetupPart:
    def __init__(self, wiring: Any):
        self.wiring = wiring
        self.app = wiring.app
        self.jobs = jobs_mod.SetupJobs(self)
        self.due = False
        self.resume_note = False

    # ------------------------------------------------------------ Orte und Kontext
    def sources(self) -> Any:
        return getattr(self.wiring, "sources", None)

    def home(self) -> str:
        src = self.sources()
        return str(getattr(src, "home", "") or paths.home())

    def user_home(self) -> str:
        """Wo „Interner Speicher“ (~/Games) liegt; im Prüfmodus im Käfig."""
        return os.path.join(paths.test_root(), "home") if paths.test_root() else paths.home()

    def flatpak_dirs(self) -> list:
        src = self.sources()
        dirs = list(getattr(src, "flatpak_dirs", None) or [])
        if not dirs:
            dirs = [os.path.join(self.home(), ".local/share/flatpak")] if paths.test_mode() else \
                ["/var/lib/flatpak", os.path.join(paths.home(), ".local/share/flatpak")]
        return dirs

    def user_flatpak(self) -> str:
        if paths.test_mode():
            src = self.sources()
            dirs = list(getattr(src, "flatpak_dirs", None) or [])
            return dirs[0] if dirs else os.path.join(self.user_home(), ".local/share/flatpak")
        return os.environ.get("FLATPAK_USER_DIR") or os.path.join(paths.home(), ".local/share/flatpak")

    def srm_dir(self) -> str | None:
        return getattr(self.sources(), "srm_dir", None)

    def auto_disk(self) -> str:
        """Spiele-Ort, wie er ohne Wahl gilt: library.games_disk, sonst der der Bibliothek, sonst paths."""
        configured = setup_pkg.abspath_setting("library.games_disk")
        if configured:
            return configured
        src = self.sources()
        disk = getattr(src, "games_disk", None)
        if disk:
            return str(disk)
        return paths.games_disk()

    def emu_root(self) -> str:
        return folders.emu_root(setup_pkg.abspath_setting("emu.root"), self.home(), self.auto_disk(), self.srm_dir())

    # ------------------------------------------------------------ Hauptschleife, Ereignisse
    def emit(self, event: str, payload: dict) -> None:
        rpc = getattr(self.app, "rpc", None)
        if rpc is not None:
            self.wiring.main.post(lambda: rpc.emit(event, payload))

    def notify(self, title: str, body: str, icon: str = "info") -> None:
        try:
            self.app.notify.push(title, body, icon=icon)
        except Exception:
            _log.exception("Mitteilung „%s“", title)

    def publish_jobs(self, views: list) -> None:
        def run() -> None:
            collect = getattr(self.wiring, "store_publish_jobs", None)
            if callable(collect):
                collect({jobs_mod.STORE}, views)
                return
            state = self.app.state
            others = [j for j in (state.value("store_jobs") or []) if j.get("store") != jobs_mod.STORE] \
                if hasattr(state, "value") else []
            state.update(store_jobs=others + views)
        self.wiring.main.post(run)

    def set_state(self, value: str, last: bool = False) -> None:
        """Zustand in setup.json und sys.state.setup. Nach „done“/„later“ gibt es nichts fortzusetzen."""
        fields: dict = {"state": value}
        if value in ("done", "later"):
            fields["resume"] = None             # write() lässt None weg
        state_mod.update(**fields)
        self.due = False
        self.wiring.main.post(lambda: self.app.state.update(setup={"due": False, "state": value}))

    def rescan(self) -> None:
        library = getattr(self.app, "library", None)
        if library is None:
            return
        fn = getattr(library, "rescan", None)
        if callable(fn):
            self.wiring.main.post(fn)

    # ------------------------------------------------------------ RPC
    def rpc_state(self, params: dict) -> dict:
        data = state_mod.read()
        st = state_mod.state_of(data)
        if self.jobs.running():
            st = "running"
        items = [i for i in data.get("items") or [] if isinstance(i, dict)]
        last = {"at": data.get("at"), "items": items} if items else None
        out = {"state": st, "due": bool(self.due), "last": last, "gamesDisk": self.auto_disk(), "emuRoot": self.emu_root()}
        resume = data.get("resume")
        if st == "interrupted" and isinstance(resume, dict):
            # D4: fortgesetzt wird mit der Auswahl von damals (last.items wie resume.items, Kennungen)
            out["resume"] = resume
            out["last"] = {"at": data.get("at"), "items": [str(i) for i in resume.get("items") or []]}
        return out

    def env(self) -> dict:
        src = self.sources()
        net = {}
        try:
            net = self.app.state.value("network") or {}
        except Exception:  # noqa: BLE001
            pass
        proton = None
        pj = getattr(self.wiring, "proton_jobs", None)
        if pj is not None:
            try:
                proton = pj.summary()
            except Exception:
                _log.exception("Proton-Stand")
        flatpak_present = None
        if paths.test_mode():
            flatpak_present = jobs_mod.helper_argv() is not None
        env = {"home": self.home(), "user_home": self.user_home(), "user": paths.user_name(),
               "games_disk": self.auto_disk(), "steam_root": getattr(src, "steam_root", None),
               "srm_dir": self.srm_dir(), "flatpak_dirs": self.flatpak_dirs(), "user_flatpak": self.user_flatpak(),
               "network": net, "proton": proton, "games_proton": self.wiring.config.get("games.proton", ""),
               "test": paths.test_mode()}
        if flatpak_present is not None:
            env["flatpak_present"] = flatpak_present
        return env

    def detect(self) -> dict:
        return detect_mod.detect(self.env())

    def rpc_detect(self, params: dict) -> Any:
        return self.wiring._bg(self.detect)

    def rpc_catalog(self, params: dict) -> Any:
        def run() -> dict:
            installed = detect_mod.installed_flatpaks(self.flatpak_dirs())
            legendary = getattr(self.wiring, "epic_legendary", None)
            try:
                installed["tool:legendary"] = bool(legendary is not None and legendary.ready())
            except Exception:  # noqa: BLE001
                pass
            from .setup import umu
            installed["tool:umu"] = bool(umu.present())
            pj = getattr(self.wiring, "proton_jobs", None)
            try:
                installed["tool:proton"] = bool(pj is not None and int((pj.summary() or {}).get("installed") or 0) > 0)
            except Exception:  # noqa: BLE001
                pass
            return catalog.view(installed)
        return self.wiring._bg(run)

    def _prepare(self, params: dict) -> tuple:
        """(Auswahl, Ort, detect) aus {place, emuRoot, items}; PlanError mit Satz. Arbeits-Thread."""
        params = params if isinstance(params, dict) else {}
        selection = plan_mod.resolve(params.get("items") or [])
        if selection["unknown"]:
            raise plan_mod.PlanError("Unbekannte Auswahl: %s" % ", ".join(selection["unknown"][:3]))
        det = self.detect()
        where = plan_mod.resolve_place(det, str(params.get("place") or ""), str(params.get("emuRoot") or ""),
                                       auto_disk=self.auto_disk(), home=self.home(), srm_dir=self.srm_dir(),
                                       emu_setting=setup_pkg.abspath_setting("emu.root"))
        if paths.test_mode() and not jobs_mod.in_cage(where["emuRoot"]):
            raise plan_mod.PlanError("Im Prüfmodus legt der Assistent nur Ordner im Käfig an.")
        return selection, where, det

    def plan(self, params: dict) -> dict:
        try:
            selection, where, det = self._prepare(params)
        except plan_mod.PlanError as e:
            return _bad(str(e))
        ids = [i for i in plan_mod.flatpaks(selection)
               if i not in det.get("installed", {})]
        helper = self.jobs.probe_plan(ids) if ids else {"ev": "plan", "ops": [], "skipped": [], "exact": True}
        if helper is not None and helper.get("code") == "no-network":
            return {"ok": False, "code": "no-network", "message": helper.get("message") or
                    "Flathub ist gerade nicht erreichbar."}
        reserve = self.wiring.config.get("install.reserve_gib", 5)
        out = plan_mod.plan(selection, dict(det, flatpak_dirs=self.flatpak_dirs()), where, helper=helper,
                            reserve_gib=reserve if isinstance(reserve, (int, float)) else 5,
                            user_flatpak=self.user_flatpak())
        return out

    def rpc_plan(self, params: dict) -> Any:
        return self.wiring._bg(self.plan, params or {})

    def start(self, params: dict) -> dict:
        if self.jobs.running():
            return _bad("Die Einrichtung läuft schon.")
        result = self.plan(params)
        if not result.get("ok"):
            return result
        if not result.get("enough"):
            return _bad(result.get("message") or "Zu wenig Platz. Wähle weniger aus.")
        selection, where, _det = self._prepare(params)
        where = dict(where, download=result["totals"]["download"],
                     params={"place": str((params or {}).get("place") or ""),
                             "emuRoot": str((params or {}).get("emuRoot") or ""),
                             "items": [str(i) for i in (params or {}).get("items") or []]})
        cfg = self.wiring.config
        if where["setDisk"]:
            cfg.set("library.games_disk", where["setDisk"])
        if where["setEmu"]:
            cfg.set("emu.root", where["setEmu"])
        if cfg.get("library.roms", True) is not True:
            cfg.set("library.roms", True)
        state_mod.update(gamesDisk=where["gamesDisk"], emuRoot=where["emuRoot"], resume=where["params"])
        return self.jobs.start(selection, where)

    def rpc_start(self, params: dict) -> Any:
        def run() -> dict:
            try:
                return self.start(params or {})
            except plan_mod.PlanError as e:
                return _bad(str(e))
        return self.wiring._bg(run)

    def rpc_cancel(self, params: dict) -> dict:
        job_id = str((params or {}).get("jobId") or "") or None
        return self.jobs.cancel(job_id)

    def rpc_later(self, params: dict) -> dict:
        if not self.jobs.running():
            self.set_state("later")
        return {"ok": True}

    def rpc_finish(self, params: dict) -> dict:
        if not self.jobs.running():
            self.set_state("done")
        return {"ok": True}

    # ------------------------------------------------------------ Start des Home
    def mount(self) -> None:
        """Beim Einhängen (Hauptschleife, schnell): Zustand bestimmen, sys.state.setup setzen."""
        leftover = os.path.isfile(jobs_mod.jobs_file())
        if leftover:
            self._interrupted()
        result = state_mod.due(self.wiring.config)
        self.due = bool(result["due"])
        self.resume_note = bool(result.get("resume"))
        try:
            self.app.state.update(setup={"due": self.due, "state": result["state"]})
        except Exception:
            _log.exception("sys.state.setup")
        if self.due:
            _log.info("Einrichtung: erster Start ohne Bestand, der Assistent erscheint")

    def _interrupted(self) -> None:
        """Ein Auftrag lief beim letzten Ende des Home: als unterbrochen merken (D4), Datei weg."""
        try:
            import json
            with open(jobs_mod.jobs_file(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        resume = data.get("params") if isinstance(data, dict) and isinstance(data.get("params"), dict) else None
        state_mod.update(state="interrupted", resume=resume)
        jobs_mod.SetupJobs._drop_file()
        _log.info("Einrichtung: der letzte Auftrag wurde unterbrochen")

    def after_ready(self) -> None:
        if self.resume_note:
            self.notify("Einrichtung unterbrochen", "Die Installation wurde unterbrochen. Fertige Teile bleiben. "
                        "Fortsetzen kannst du unter Einstellungen › PC5 Launcher › Einrichtung.", "warn")

    # ------------------------------------------------------------ für Einstellungen › Spiele › Emulatoren
    def emulator_rows(self) -> dict:
        """{root, auto, emudeck, roms, emulators: [{id, title, flatpak, installation, entry}]} (blockierend)."""
        installed = detect_mod.installed_flatpaks(self.flatpak_dirs(), [e["flatpak"] for e in catalog.EMULATORS])
        root = self.emu_root()
        emudeck = folders.emudeck_root(self.home(), self.srm_dir())
        return {"root": root, "auto": not setup_pkg.abspath_setting("emu.root"),
                "emudeck": bool(emudeck) and os.path.normpath(emudeck) == os.path.normpath(root),
                "available": bool(root) and os.path.isdir(root),
                "roms": bool(self.wiring.config.get("library.roms", True)),
                "emulators": [{"id": e["id"], "title": e["title"], "flatpak": e["flatpak"],
                               "installation": installed[e["flatpak"]], "entry": "app:emu-" + e["id"]}
                              for e in catalog.EMULATORS if e["flatpak"] in installed]}

    def stop(self) -> None:
        self.jobs.stop()
        setup_pkg.bind_config(None)


def extend(wiring: Any) -> None:
    part = SetupPart(wiring)
    wiring.setup_part = part
    # Bibliotheksquellen und Archiv-Installer lesen library.roms, emu.root und library.games_disk hierüber
    setup_pkg.bind_config(lambda: wiring.config)
    rpc = wiring.app.rpc
    def in_bg(fn):
        # liest bzw. schreibt Dateien (setup.json mit fsync): nicht in der Hauptschleife
        return lambda params: wiring._bg(fn, params or {})
    for name, fn in (("setup.state", in_bg(part.rpc_state)), ("setup.detect", part.rpc_detect),
                     ("setup.catalog", part.rpc_catalog), ("setup.plan", part.rpc_plan),
                     ("setup.start", part.rpc_start), ("setup.cancel", part.rpc_cancel),
                     ("setup.later", in_bg(part.rpc_later)), ("setup.finish", in_bg(part.rpc_finish))):
        rpc.register(name, fn)
    routes = getattr(wiring, "store_routes", None)
    if isinstance(routes, dict) and isinstance(routes.get("store.cancel"), dict):
        routes["store.cancel"][jobs_mod.STORE] = lambda params: part.jobs.cancel(
            str((params or {}).get("id") or "") or None)
    wiring.busy_checks.append(part.jobs.busy)
    wiring.shutdown_hooks.append(part.stop)
    wiring.after_ready.append(part.after_ready)
    part.mount()
