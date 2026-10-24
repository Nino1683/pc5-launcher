"""Auswahl auflösen und Platz rechnen (E105, Bauplan 8.5; setup.plan, setup.start). Rein rechnerisch bis
auf statvfs und lexists; den Probelauf des Hilfsprogramms ruft jobs.py und reicht dessen Ergebnis herein.

Auswahl (items): Katalog-IDs (Emulator, App, Werkzeug) und Zeilen aus S4 als "row:<id>". Eine Zeile wählt
ihren Emulator und nur ihre Systeme; ein Emulator allein alle seine Systeme. RetroArch zählt einmal, auch
über mehrere Zeilen; von den Proton-Familien gilt höchstens eine. Schon Installiertes (System oder Nutzer)
wird übersprungen. Geteilte Laufzeiten zählen einmal. Was der Katalog nicht anbietet (offer False: Eden,
Ryujinx, Frage 3), gilt wie Unbekanntes und wird nie installiert.
"""
from __future__ import annotations

import os

from . import catalog, folders

MIB = 1024 * 1024
GIB = 1024 ** 3


class PlanError(Exception):
    """Ein deutscher Satz für die Oberfläche."""


def resolve(items) -> dict:
    """{emulators, apps, tools, proton, systems, unknown} aus der Auswahl (Reihenfolge des Katalogs)."""
    emus, apps, tools, systems, unknown = [], [], [], [], []
    proton = ""
    for raw in items if isinstance(items, (list, tuple)) else []:
        key = str(raw or "")
        if key.startswith("proton:"):
            key = key[len("proton:"):]                  # Schreibweise der Oberfläche: "proton:<familie>"
        if key.startswith("row:"):
            r = catalog.row(key[4:])
            emu = catalog.emulator(r["emulator"]) if r else None
            if emu is None or not emu["offer"]:          # nicht angeboten (Switch, Frage 3) = unbekannt
                unknown.append(key)
                continue
            if emu["id"] not in emus:
                emus.append(emu["id"])
            systems.extend(s for s in r["systems"] if s not in systems)
            continue
        item = catalog.get(key)
        if item is None or item.get("offer") is False:
            unknown.append(key)
        elif item["kind"] == "emulator":
            if key not in emus:
                emus.append(key)
            systems.extend(s for s in item["systems"] if s not in systems)
        elif item["kind"] == "app":
            if key not in apps:
                apps.append(key)
        elif item["kind"] == "proton":
            if proton and proton != key:
                raise PlanError("Wähle höchstens eine Proton-Fassung.")
            proton = key
        elif key not in tools:
            tools.append(key)
    order = {e["id"]: i for i, e in enumerate(catalog.EMULATORS + catalog.APPS + catalog.TOOLS)}
    return {"emulators": sorted(emus, key=order.get), "apps": sorted(apps, key=order.get),
            "tools": sorted(tools, key=order.get), "proton": proton,
            "systems": [s for s in catalog.SYSTEMS if s in systems], "unknown": unknown}


def flatpaks(selection: dict) -> list:
    """Flatpak-Kennungen der Auswahl (Emulatoren, dann Apps)."""
    return [catalog.get(i)["flatpak"] for i in selection["emulators"] + selection["apps"]]


def resolve_place(det: dict, place: str, emu_choice: str, *, auto_disk: str, home: str,
                  srm_dir: str | None = None, emu_setting: str = "") -> dict:
    """Wohin die Einrichtung schreibt. place: "" bzw. "auto" (Spiele-Ort bleibt automatisch) oder eine
    Kennung aus detect().places. emu_choice: "" / "auto", "emudeck", "new" oder ein Pfad aus genau diesen.
    → {place, gamesDisk, base, emuRoot, emudeck, setDisk, setEmu}; PlanError mit Satz, wenn es nicht passt."""
    places = {p["id"]: p for p in det.get("places") or []}
    chosen = None
    if place and place != "auto":
        chosen = places.get(place) or next((p for p in places.values() if p.get("path") == place), None)
        if chosen is None:
            raise PlanError("Diesen Ort gibt es gerade nicht. Ist die Platte noch angeschlossen?")
        if not chosen.get("ok"):
            raise PlanError(chosen.get("reason") or "Dieser Ort eignet sich nicht für Spiele.")
        disk = chosen["path"]
    else:
        disk = auto_disk
        chosen = next((p for p in places.values() if os.path.normpath(p.get("path") or "") ==
                       os.path.normpath(disk or "")), None)
    base = (chosen or {}).get("base") or disk
    emudeck = ((det.get("emudeck") or {}).get("emulationPath") or "") if (det.get("emudeck") or {}).get("present") \
        else ""
    new_root = os.path.join(disk, "Emulation") if disk else ""
    choice = str(emu_choice or "")
    if choice in ("", "auto"):
        root = folders.emu_root(emu_setting, home, disk, srm_dir)
        set_emu = ""
    elif choice == "emudeck" or (emudeck and os.path.normpath(choice) == os.path.normpath(emudeck)):
        if not emudeck:
            raise PlanError("Es gibt hier keinen EmuDeck-Ordner.")
        root = emudeck
        set_emu = "" if folders.emu_root("", home, disk, srm_dir) == emudeck else emudeck
    elif choice == "new" or (new_root and os.path.normpath(choice) == os.path.normpath(new_root)):
        root = new_root
        set_emu = "" if folders.emu_root("", home, disk, srm_dir) == new_root else new_root
    else:
        raise PlanError("Dieser Ordner für Emulator-Spiele lässt sich hier nicht wählen.")
    if not root:
        raise PlanError("Es ist kein Ort für die Emulator-Spiele bekannt.")
    set_disk = disk if chosen is not None and place and place != "auto" and \
        os.path.normpath(disk) != os.path.normpath(auto_disk or "") else ""
    # Liegt der Emulator-Ordner nicht am gewählten Ort (EmuDeck auf einer anderen Platte), gilt dessen Wurzel
    if base and not (os.path.normpath(root) + os.sep).startswith(os.path.normpath(base).rstrip(os.sep) + os.sep):
        base = os.path.dirname(os.path.normpath(root))
    return {"place": (chosen or {}).get("id", "auto"), "gamesDisk": disk, "base": base, "emuRoot": root,
            "emudeck": emudeck, "setDisk": set_disk, "setEmu": set_emu}


def _free(path: str, statvfs=os.statvfs) -> int:
    """Freier Platz am nächsten vorhandenen Ordner über path."""
    probe = os.path.abspath(path or "/")
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        st = statvfs(probe)
    except OSError:
        return 0
    return st.f_bavail * st.f_frsize


def plan(selection: dict, det: dict, where: dict, *, helper: dict | None, reserve_gib: float = 5,
         user_flatpak: str = "", statvfs=os.statvfs) -> dict:
    """Ergebnis für setup.plan (Vertrag 4.3). helper: Antwort des Probelaufs ({"ev":"plan", ops, skipped,
    exact}) oder None (dann Katalog-Schätzung)."""
    installed = dict(det.get("installed") or {})
    for s in (helper or {}).get("skipped") or []:
        if isinstance(s, dict) and s.get("id"):
            installed.setdefault(str(s["id"]), str(s.get("installation") or "user"))
    exact = bool(helper and helper.get("exact"))
    ops = [o for o in (helper or {}).get("ops") or [] if isinstance(o, dict)]
    by_ref = {str(o.get("ref") or ""): o for o in ops}
    items, runtimes, need_rt = [], [], []
    download = size = 0
    for kind, ids in (("emulator", selection["emulators"]), ("app", selection["apps"])):
        for item_id in ids:
            item = catalog.get(item_id)
            state = installed.get(item["flatpak"], "")
            row = {"id": item_id, "title": item["title"], "kind": kind, "flatpak": item["flatpak"],
                   "state": state or "new", "skip": bool(state), "source": "flathub", "download": 0, "installed": 0}
            if not state:
                op = next((o for r, o in by_ref.items() if r.split("/")[1:2] == [item["flatpak"]]), None)
                if exact and op is not None:
                    row["download"], row["installed"] = int(op.get("download") or 0), int(op.get("installed") or 0)
                else:
                    row["installed"] = item["size_mb"] * MIB
                    row["download"] = int(row["installed"] * catalog.DOWNLOAD_SHARE)
                if item["runtime"] and item["runtime"] not in need_rt:
                    need_rt.append(item["runtime"])
                download += row["download"]
                size += row["installed"]
            items.append(row)
    if exact:
        for o in ops:
            if str(o.get("kind")) == "runtime" or str(o.get("ref", "")).startswith("runtime/"):
                rt = {"ref": o.get("ref"), "title": catalog.ref_title(o.get("ref")), "download": int(o.get("download") or 0),
                      "installed": int(o.get("installed") or 0)}
                runtimes.append(rt)
                download += rt["download"]
                size += rt["installed"]
    else:
        for rid in need_rt:
            rt = catalog.runtime(rid)
            if rt is None or _runtime_present(rt["ref"], det):
                continue
            runtimes.append({"ref": rt["ref"], "title": "Laufzeit " + rt["title"], "download": rt["download_mb"] * MIB,
                             "installed": rt["size_mb"] * MIB})
            download += rt["download_mb"] * MIB
            size += rt["size_mb"] * MIB
    home_need = size
    for tool_id in selection["tools"] + ([selection["proton"]] if selection["proton"] else []):
        tool = catalog.get(tool_id)
        state = ""
        if tool_id == "umu" and (det.get("umu") or {}).get("present"):
            state = "present"
        row = {"id": tool_id, "title": tool["title"], "kind": tool["kind"], "state": state or "new", "skip": bool(state),
               "source": "github", "download": 0 if state else tool["download_mb"] * MIB,
               "installed": 0 if state else tool["size_mb"] * MIB}
        items.append(row)
        download += row["download"]
        home_need += row["installed"]
        size += row["installed"]
    root = where["emuRoot"]
    wanted = folders.wanted(root, selection["systems"]) if not (where.get("emudeck") and
                                                               os.path.realpath(root) == os.path.realpath(where["emudeck"])) else []
    new_folders = [p for p in wanted if not os.path.lexists(p)]          # volle Pfade, die neu entstehen
    free_home = _free(user_flatpak or os.path.expanduser("~"), statvfs)
    free_games = _free(where.get("base") or where.get("gamesDisk") or "", statvfs)
    reserve = int(float(reserve_gib or 0) * GIB)
    missing = max(0, home_need + reserve - free_home)
    out = {"ok": True, "items": items, "runtimes": runtimes, "folders": new_folders,
           "totals": {"download": download, "installed": size},
           "free": {"home": free_home, "games": free_games}, "enough": missing == 0, "missing": missing,
           "estimate": "exact" if exact else "approx", "where": {k: where[k] for k in ("place", "gamesDisk", "emuRoot")},
           "systems": selection["systems"]}
    if missing:
        out["message"] = "Zu wenig Platz: Es fehlen %s auf dem internen Speicher. Wähle weniger aus." % size_text(missing)
    return out


def _runtime_present(ref: str, det: dict) -> bool:
    parts = str(ref).split("/")
    if len(parts) != 4:
        return False
    for root in det.get("flatpak_dirs") or []:
        if os.path.isdir(os.path.join(root, "runtime", parts[1], parts[2], parts[3], "active")):
            return True
    return False


def size_text(n: int) -> str:
    if n >= GIB:
        return ("%.1f GB" % (n / GIB)).replace(".", ",")
    return "%d MB" % max(1, round(n / MIB))
