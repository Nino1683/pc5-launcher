"""Welches Systemabbild läuft, und wartet ein neues auf den Neustart?

Liest das ostree-Sysroot im eigenen Prozess über libostree (rund 4 ms), ohne Root und ohne den
Dienst rpm-ostreed zu wecken. `ostree admin status` nennt auf diesem Abbild keine Versionen;
die stehen im os-release jeder Bereitstellung.

E104: Nur auf Systemen, die aus ostree gestartet sind (/run/ostree-booted: Bazzite und andere
Atomic-Systeme). SteamOS hat A/B-Abbilder ohne ostree: dort gibt es keine Zeile zum Systemabbild.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("couch.settings.ostree")


BOOTED = "/run/ostree-booted"


def booted(marker: str = BOOTED) -> bool:
    """Läuft das System aus einer ostree-Bereitstellung? (E104; die Marke legt ostree beim Start an)"""
    return os.path.exists(marker)


def no_ostree() -> dict:
    """Stand wie update_state, wenn es kein ostree-System ist: nichts wartet, kein Fehler."""
    return {"booted_version": None, "next_version": None, "reboot_pending": False, "staged": False, "error": "",
            "ostree": False}


def parse_os_release(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k and not k.startswith("#"):
            out[k] = v.strip().strip("'\"")
    return out


def read_os_release(path: str = "/etc/os-release") -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return parse_os_release(f.read())
    except OSError:
        return {}


def list_deployments() -> list[dict]:
    """Bereitstellungen in Startreihenfolge: [{"osname", "csum", "serial", "booted", "staged",
    "pinned"}]. Blockierend (wenige ms). Wirft, wenn libostree fehlt oder kein ostree-System läuft."""
    import gi
    gi.require_version("OSTree", "1.0")
    from gi.repository import OSTree
    sysroot = OSTree.Sysroot.new_default()
    sysroot.load(None)
    booted = sysroot.get_booted_deployment()
    return [{"osname": d.get_osname(), "csum": d.get_csum(), "serial": d.get_deployserial(),
             "booted": bool(booted and d.equal(booted)), "staged": bool(d.is_staged()),
             "pinned": bool(d.is_pinned())} for d in sysroot.get_deployments()]


def deployment_version(dep: dict, deploy_root: str = "/ostree/deploy") -> str | None:
    """OSTREE_VERSION aus dem os-release genau dieser Bereitstellung."""
    path = os.path.join(deploy_root, str(dep.get("osname")), "deploy",
                        f"{dep.get('csum')}.{dep.get('serial')}", "usr/lib/os-release")
    rel = read_os_release(path)
    return rel.get("OSTREE_VERSION") or rel.get("VERSION") or None


def update_state(lister=list_deployments, deploy_root: str = "/ostree/deploy") -> dict:
    """{"booted_version", "next_version", "reboot_pending", "staged", "error"}.

    reboot_pending: Die erste Bereitstellung der Liste (= nächster Start) ist nicht die laufende.
    lister ist einsteckbar (Tests mit erfundenen Bereitstellungen)."""
    out = {"booted_version": None, "next_version": None, "reboot_pending": False, "staged": False, "error": ""}
    try:
        deps = [d for d in (lister() or []) if isinstance(d, dict)]
    except Exception as e:  # noqa: BLE001 - kein ostree-System, Modul fehlt: nur kein Hinweis
        log.info("ostree nicht lesbar: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    booted = next((d for d in deps if d.get("booted")), None)
    nxt = deps[0] if deps else None
    out["booted_version"] = deployment_version(booted, deploy_root) if booted else None
    out["next_version"] = deployment_version(nxt, deploy_root) if nxt else None
    out["staged"] = any(d.get("staged") for d in deps)
    out["reboot_pending"] = bool(booted is not None and nxt is not None and nxt is not booted)
    return out
