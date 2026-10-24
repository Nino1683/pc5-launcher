"""Flatpak: anstehende Updates und laufende Instanzen lesen. Aktualisiert wird im Bereich system
über run_write (`flatpak update --system|--user -y --noninteractive`).

E104: Gefragt werden beide Installationen, die des Systems und die des Nutzers (der
Einrichtungs-Assistent installiert nur mit `--user`, EmuDeck hat auf dem Entwicklungsrechner auch dorthin installiert).
Aktualisiert wird jede App in der Installation, in der sie liegt; eine App, die in beiden liegt,
steht einmal in der Liste und wird in beiden aktualisiert.
"""
from __future__ import annotations

import os

INSTALLATIONS = ("system", "user")
UPDATE_CMDS = {inst: ["flatpak", "update", "--" + inst, "-y", "--noninteractive"] for inst in INSTALLATIONS}
UPDATE_CMD = UPDATE_CMDS["system"]          # bis E104 der einzige Aufruf


def parse_updates(text: str) -> list[dict]:
    """`flatpak remote-ls --system|--user --updates --columns=name,application,version,branch`
    (Tabulator-getrennt) -> [{"name", "id", "version", "branch", "runtime"}]."""
    out, seen = [], set()
    for line in text.splitlines():
        if not line.strip():
            continue
        f = [p.strip() for p in line.split("\t")]
        f += [""] * (4 - len(f))
        name, app, version, branch = f[:4]
        # Ohne Punkt ist es keine Anwendungs-ID: Kopfzeile oder Fehlermeldung
        if "." not in app or " " in app or (app, branch) in seen:
            continue
        seen.add((app, branch))
        runtime = any(part in app for part in (".Platform", ".Sdk", ".Extension", ".Gtk3theme", ".KStyle"))
        out.append({"name": name or app, "id": app, "version": version, "branch": branch, "runtime": runtime})
    out.sort(key=lambda u: (u["runtime"], u["name"].casefold()))
    return out


def _user_repo() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "flatpak", "repo")


def read_updates(run_read, user_repo: str | None = None) -> tuple[list[dict], str]:
    """(Liste, Fehlertext). Blockierend, je Installation rund 0,7 s, braucht Netz, fragt nie etwas.
    Jeder Eintrag trägt "installations" (["system"], ["user"] oder beide). Ein Fehler der
    Nutzer-Installation zählt nur, wenn es sie gibt (user_repo, Vorgabe ~/.local/share/flatpak/repo):
    Ohne eigene Apps hat sie oft gar keine Quelle."""
    merged: dict[tuple, dict] = {}
    error = ""
    for inst in INSTALLATIONS:
        rc, out, err = run_read(["flatpak", "remote-ls", "--" + inst, "--updates",
                                 "--columns=name,application,version,branch"], timeout=60)
        if rc != 0:
            lines = (err or "").strip().splitlines()
            counts = inst == "system" or os.path.isdir(user_repo if user_repo is not None else _user_repo())
            if counts and not error:
                error = lines[-1] if lines else "flatpak fehlgeschlagen"
            continue
        for u in parse_updates(out):
            key = (u["id"], u["branch"])
            if key in merged:
                if inst not in merged[key]["installations"]:
                    merged[key]["installations"].append(inst)
            else:
                merged[key] = {**u, "installations": [inst]}
    found = sorted(merged.values(), key=lambda u: (u["runtime"], u["name"].casefold()))
    if error and not found:
        return [], error
    return found, error


def update_cmds(updates: list[dict]) -> list[list[str]]:
    """Die Aufrufe für flatpak update, je betroffener Installation einer (System zuerst)."""
    wanted = {inst for u in updates for inst in (u.get("installations") or ["system"])}
    return [list(UPDATE_CMDS[inst]) for inst in INSTALLATIONS if inst in wanted]


def read_running(run_read) -> list[str]:
    """IDs laufender Flatpak-Apps (`flatpak ps`). Während eine läuft, wird nicht aktualisiert."""
    rc, out, _ = run_read(["flatpak", "ps", "--columns=application"], timeout=10)
    if rc != 0:
        return []
    return sorted({l.strip() for l in out.splitlines() if "." in l and " " not in l.strip()})
