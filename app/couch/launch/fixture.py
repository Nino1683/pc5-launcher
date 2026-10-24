"""Prüf-Starter "fixture": baut den Startauftrag aus entry.detail["launch"] (siehe
library/fixture.py). Gedacht für tools/fake-app; nur aktiv, wenn COUCH_FIXTURES gesetzt ist."""
from __future__ import annotations

import os
from typing import Any

from .. import paths
from ..library.model import Entry
from . import register_launcher
from .spec import LaunchError, LaunchSpec


def _fill(text: str) -> str:
    """Platzhalter, damit library.json ohne absolute Pfade auskommt."""
    return (text.replace("{fake_app}", os.path.join(paths.release_dir(), "tools", "fake-app"))
                .replace("{fixtures}", paths.fixtures_dir() or "")
                .replace("{release}", paths.release_dir()))


def build(entry: Entry, app: Any = None) -> LaunchSpec:
    launch = entry.detail.get("launch")
    if not isinstance(launch, dict):
        raise LaunchError("Für diesen Prüf-Eintrag ist kein Startbefehl hinterlegt.", entry.id)
    if launch.get("error"):
        raise LaunchError(str(launch["error"]), "aus der Prüf-Bibliothek")
    if launch.get("handover"):
        return LaunchSpec(entry_id=entry.id, argv=[], kind="game", freeze=False,
                          handover=dict(launch["handover"]), title=entry.title)
    kind = launch.get("kind") or ("game" if entry.kind == "game" else "media")
    return LaunchSpec(
        entry_id=entry.id,
        argv=[_fill(str(a)) for a in launch.get("argv", [])],
        env={str(k): _fill(str(v)) for k, v in (launch.get("env") or {}).items()},
        unset=list(launch.get("unset") or []),
        cwd=_fill(launch["cwd"]) if launch.get("cwd") else None,
        kind=kind,
        freeze=bool(launch.get("freeze", kind in ("game", "emulator"))),
        mouse=bool(launch.get("mouse", False)),
        polite_stop=[_fill(str(a)) for a in launch["polite_stop"]] if launch.get("polite_stop") else None,
        prelaunch=[[_fill(str(a)) for a in cmd] for cmd in (launch.get("prelaunch") or [])],
        title=entry.title,
    )


def setup() -> bool:
    if not paths.fixtures_dir():
        return False
    register_launcher("fixture", build)
    return True
