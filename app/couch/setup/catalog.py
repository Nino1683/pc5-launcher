"""Katalog des Einrichtungs-Assistenten (E105, Bauplan 8.4): was angeboten wird, woher es kommt und
wie ROM-Ordner und Start aussehen. Reine Daten, nur Standardbibliothek, nichts hier liest die Platte.

Größen: Apps aus Flathub bzw. lokal gelesen (Stand 2026-10-24), Laufzeiten aus der Flathub-API
(installiert und Download). Startargumente sind unbestätigt (je Emulator am Fernseher zu prüfen).
{rom} = Pfad der Spieldatei, {core} = Kern-Datei (RetroArch).

Felder je Emulator: id, title, flatpak, systems ({ordner: endungen}), size_mb, runtime, section,
hint, offer (im Assistenten anbieten), library (ROM-Quelle liest seine Ordner; "nosrm" = nur ohne
Steam ROM Manager), args, cores, bios. BIOS, Schlüssel, Firmware und ROMs lädt der PC5 Launcher nie;
es gibt hier keine Adressen dafür (SAFETY 13).
"""
from __future__ import annotations

FLATHUB = "flathub"

# ---------------------------------------------------------------- Systeme (Ordnernamen wie EmuDeck/ES-DE)
# ordner: (Name in der Oberfläche, Marke auf der Kachel)
SYSTEMS = {
    "nes": ("NES", "NES"),
    "snes": ("SNES", "SNES"),
    "gb": ("Game Boy", "GAME BOY"),
    "gbc": ("Game Boy Color", "GBC"),
    "gba": ("Game Boy Advance", "GBA"),
    "n64": ("Nintendo 64", "N64"),
    "nds": ("Nintendo DS", "DS"),
    "n3ds": ("Nintendo 3DS", "3DS"),
    "gc": ("GameCube", "GAMECUBE"),
    "wii": ("Wii", "WII"),
    "wiiu": ("Wii U", "WII U"),
    "switch": ("Nintendo Switch", "SWITCH"),
    "psx": ("PS1", "PS1"),
    "ps2": ("PS2", "PS2"),
    "ps3": ("PS3", "PS3"),
    "psp": ("PSP", "PSP"),
    "xbox": ("Xbox", "XBOX"),
    "dreamcast": ("Dreamcast", "DREAMCAST"),
    "genesis": ("Mega Drive", "MEGA DRIVE"),
    "mastersystem": ("Master System", "MASTER SYSTEM"),
    "arcade": ("Arcade", "ARCADE"),
    "scummvm": ("Klassische Adventures", "SCUMMVM"),
}

SECTIONS = (("nintendo", "Nintendo"), ("ps", "PS-Konsolen"), ("sega", "Sega und weitere"))

# ---------------------------------------------------------------- Laufzeiten (Flathub, 2026-10-24)
# id: (Name, Ref, installiert MB, Download MB)
RUNTIMES = {
    "kde-6.10": ("KDE 6.10", "runtime/org.kde.Platform/x86_64/6.10", 1050, 392),
    "kde-6.11": ("KDE 6.11", "runtime/org.kde.Platform/x86_64/6.11", 1082, 410),
    "fdo-24.08": ("Freedesktop 24.08", "runtime/org.freedesktop.Platform/x86_64/24.08", 691, 269),
    "fdo-25.08": ("Freedesktop 25.08", "runtime/org.freedesktop.Platform/x86_64/25.08", 671, 260),
    "fdo-26.08": ("Freedesktop 26.08", "runtime/org.freedesktop.Platform/x86_64/26.08", 724, 283),
    "gnome-49": ("GNOME 49", "runtime/org.gnome.Platform/x86_64/49", 1091, 420),
}
# Ohne genaue Zahlen (kein libflatpak): Download ≈ dieser Anteil der installierten Größe (geschätzt)
DOWNLOAD_SHARE = 0.5

_ZIP = (".zip", ".7z")


def _emu(id_, title, flatpak, systems, size_mb, runtime, section, hint="", offer=True, library=True,
         args=(), cores=None, bios=False):
    return {"id": id_, "title": title, "flatpak": flatpak, "systems": systems, "size_mb": size_mb,
            "runtime": runtime, "section": section, "hint": hint, "offer": offer, "library": library,
            "args": tuple(args), "cores": dict(cores or {}), "bios": bios, "kind": "emulator"}


EMULATORS = (
    _emu("retroarch", "RetroArch", "org.libretro.RetroArch",
         {"nes": (".nes",) + _ZIP, "snes": (".sfc", ".smc") + _ZIP, "gb": (".gb", ".zip"), "gbc": (".gbc", ".zip"),
          "gba": (".gba", ".zip"), "n64": (".z64", ".n64", ".v64", ".zip"), "psx": (".chd", ".cue", ".pbp", ".m3u"),
          "genesis": (".md", ".gen", ".bin", ".zip"), "mastersystem": (".sms", ".zip")},
         785, "kde-6.11", "nintendo", "Ein Programm für viele ältere Konsolen. Kerne lädst du in RetroArch selbst.",
         args=("-f", "-L", "{core}", "{rom}"),
         cores={"nes": ("mesen", "fceumm"), "snes": ("snes9x",), "gb": ("gambatte",), "gbc": ("gambatte",),
                "gba": ("mgba",), "n64": ("mupen64plus_next",), "psx": ("swanstation",),
                "genesis": ("genesis_plus_gx",), "mastersystem": ("genesis_plus_gx",)}),
    _emu("dolphin", "Dolphin", "org.DolphinEmu.dolphin-emu",
         {"gc": (".rvz", ".iso", ".gcz", ".ciso"), "wii": (".rvz", ".wbfs", ".iso")},
         55, "kde-6.11", "nintendo", "Läuft ohne BIOS.",
         args=("-b", "-e", "{rom}", "-C", "Dolphin.Display.Fullscreen=True")),
    _emu("cemu", "Cemu", "info.cemu.Cemu", {"wiiu": (".wua", ".wud", ".wux", ".rpx")},
         61, "fdo-26.08", "nintendo", "Braucht die Schlüssel deiner Wii U.", library="nosrm",
         args=("-f", "-g", "{rom}")),
    _emu("melonds", "melonDS", "net.kuribo64.melonDS", {"nds": (".nds", ".zip")},
         8, "kde-6.10", "nintendo", args=("-f", "{rom}")),
    _emu("azahar", "Azahar", "org.azahar_emu.Azahar", {"n3ds": (".3ds", ".cci", ".cxi", ".3dsx")},
         60, "kde-6.11", "nintendo", "Nachfolger von Citra. Nur entschlüsselte Spiele.", args=("-f", "{rom}")),
    # Switch: im veröffentlichten Assistenten erst nach Klärung der offenen Frage 3; gelesen wird trotzdem
    _emu("eden", "Eden", "dev.eden_emu.eden", {"switch": (".nsp", ".xci")},
         30, "kde-6.11", "nintendo", "Braucht Schlüssel und Firmware deiner eigenen Switch.", offer=False,
         library="nosrm", args=("-f", "-g", "{rom}")),
    _emu("ryujinx", "Ryujinx", "io.github.ryubing.Ryujinx", {"switch": (".nsp", ".xci")},
         96, "fdo-25.08", "nintendo", offer=False, library=False, args=("--fullscreen", "{rom}")),
    _emu("pcsx2", "PCSX2", "net.pcsx2.PCSX2", {"ps2": (".chd", ".iso", ".cso")},
         122, "kde-6.10", "ps", "Braucht das BIOS deiner Konsole.", args=("-batch", "-fullscreen", "--", "{rom}"),
         bios=True),
    _emu("rpcs3", "RPCS3", "net.rpcs3.RPCS3", {"ps3": ()},
         151, "kde-6.11", "ps", "Die Systemsoftware installierst du in RPCS3 über Datei › Firmware installieren. "
                                "Spiele startest du in RPCS3.", library=False),
    _emu("ppsspp", "PPSSPP", "org.ppsspp.PPSSPP", {"psp": (".iso", ".cso", ".chd", ".pbp")},
         79, "fdo-26.08", "ps", "Läuft ohne BIOS.", args=("--fullscreen", "{rom}")),
    _emu("xemu", "xemu", "app.xemu.xemu", {"xbox": (".iso", ".xiso")},
         34, "fdo-26.08", "sega", "Braucht BIOS-Dateien und ein Festplatten-Abbild deiner Xbox. Für Fortgeschrittene.",
         args=("-full-screen", "-dvd_path", "{rom}"), bios=True),
    _emu("flycast", "Flycast", "org.flycast.Flycast", {"dreamcast": (".chd", ".gdi", ".cdi")},
         35, "fdo-26.08", "sega", args=("-config", "window:fullscreen=yes", "{rom}")),
    _emu("mame", "MAME", "org.mamedev.MAME", {"arcade": ()},
         620, "kde-6.11", "sega", "Nur eigene, rechtmäßig erworbene ROM-Sätze. Spiele startest du in MAME.",
         library=False),
    _emu("scummvm", "ScummVM", "org.scummvm.ScummVM", {"scummvm": ()},
         379, "fdo-25.08", "sega", "Spiele fügst du in ScummVM hinzu.", library=False),
)

# Zeilen in S4 (Titel ← Systeme eines Emulators). RetroArch zählt nur einmal, auch über mehrere Zeilen.
ROWS = (
    {"id": "nintendo-classic", "title": "NES, SNES, Game Boy, GBA, N64", "emulator": "retroarch",
     "systems": ("nes", "snes", "gb", "gbc", "gba", "n64"), "section": "nintendo", "hint": ""},
    {"id": "gc-wii", "title": "GameCube und Wii", "emulator": "dolphin", "systems": ("gc", "wii"),
     "section": "nintendo", "hint": ""},
    {"id": "wiiu", "title": "Wii U", "emulator": "cemu", "systems": ("wiiu",), "section": "nintendo", "hint": ""},
    {"id": "nds", "title": "Nintendo DS", "emulator": "melonds", "systems": ("nds",), "section": "nintendo", "hint": ""},
    {"id": "n3ds", "title": "Nintendo 3DS", "emulator": "azahar", "systems": ("n3ds",), "section": "nintendo",
     "hint": ""},
    {"id": "switch", "title": "Nintendo Switch", "emulator": "eden", "systems": ("switch",), "section": "nintendo",
     "hint": ""},
    {"id": "ps1", "title": "PS1", "emulator": "retroarch", "systems": ("psx",), "section": "ps",
     "hint": "Braucht das BIOS deiner Konsole."},
    {"id": "ps2", "title": "PS2", "emulator": "pcsx2", "systems": ("ps2",), "section": "ps", "hint": ""},
    {"id": "ps3", "title": "PS3", "emulator": "rpcs3", "systems": ("ps3",), "section": "ps", "hint": ""},
    {"id": "psp", "title": "PSP", "emulator": "ppsspp", "systems": ("psp",), "section": "ps", "hint": ""},
    {"id": "sega-classic", "title": "Mega Drive, Master System", "emulator": "retroarch",
     "systems": ("genesis", "mastersystem"), "section": "sega", "hint": ""},
    {"id": "dreamcast", "title": "Dreamcast", "emulator": "flycast", "systems": ("dreamcast",), "section": "sega",
     "hint": ""},
    {"id": "xbox", "title": "Xbox", "emulator": "xemu", "systems": ("xbox",), "section": "sega", "hint": ""},
    {"id": "arcade", "title": "Arcade", "emulator": "mame", "systems": ("arcade",), "section": "sega", "hint": ""},
    {"id": "scummvm", "title": "Klassische Adventures", "emulator": "scummvm", "systems": ("scummvm",),
     "section": "sega", "hint": ""},
)


def _app(id_, title, flatpak, size_mb, runtime, hint, default=True, tile=""):
    return {"id": id_, "title": title, "flatpak": flatpak, "size_mb": size_mb, "runtime": runtime, "hint": hint,
            "default": default, "tile": tile or "app:" + id_, "kind": "app", "offer": True}


APPS = (
    _app("youtube", "YouTube", "rocks.shy.VacuumTube", 313, "fdo-25.08",
         "YouTube in der Ansicht für den Fernseher (VacuumTube)."),
    _app("kodi", "Kodi", "tv.kodi.Kodi", 578, "fdo-24.08", "Mediacenter für Filme, Serien und Musik."),
    _app("jellyfin", "Jellyfin", "org.jellyfin.JellyfinDesktop", 316, "kde-6.10",
         "Filme und Serien von deinem eigenen Jellyfin-Server."),
    _app("moonlight", "Moonlight", "com.moonlight_stream.Moonlight", 19, "kde-6.11",
         "Spiele von einem anderen PC auf diesen Fernseher streamen."),
    _app("chrome", "Google Chrome", "com.google.Chrome", 178, "fdo-25.08",
         "Nur, wenn du lieber Chrome statt des eingebauten Browsers nutzt. Chrome lädt beim Installieren "
         "zusätzlich von Google.", default=False, tile="app:browser"),
)

# Werkzeuge: kommen nicht von Flathub. Größen geschätzt (Proton: Download und entpackt, E102)
TOOLS = (
    {"id": "umu", "title": "umu-launcher", "size_mb": 1, "download_mb": 1, "kind": "tool",
     "hint": "Startet Windows-Spiele mit Proton, ohne Steam zu öffnen. Wird im Ordner des PC5 Launchers "
             "abgelegt, nicht im System."},
    {"id": "legendary", "title": "legendary", "size_mb": 4, "download_mb": 4, "kind": "tool",
     "hint": "Lädt das kleine Werkzeug „legendary“ (Open Source), damit Epic-Spiele später sofort gehen."},
    {"id": "proton-cachyos", "title": "Proton-CachyOS", "size_mb": 1700, "download_mb": 520, "kind": "proton",
     "family": "proton-cachyos", "hint": "Wird von GitHub geladen und geprüft."},
    {"id": "ge-proton", "title": "GE-Proton", "size_mb": 1600, "download_mb": 480, "kind": "proton",
     "family": "ge-proton", "hint": "Wird von GitHub geladen und geprüft."},
)
PROTON_IDS = ("proton-cachyos", "ge-proton")

RECOMMENDED = ["retroarch", "dolphin", "pcsx2", "ppsspp", "melonds", "youtube"]

# Unter-Ordner des Emulator-Ordners
ROMS_DIR = "roms"
BIOS_DIR = "bios"


# ---------------------------------------------------------------- Abfragen
_BY_ID = {e["id"]: e for e in EMULATORS + APPS + TOOLS}
_BY_FLATPAK = {e["flatpak"]: e for e in EMULATORS + APPS}
_ROWS = {r["id"]: r for r in ROWS}


def get(item_id: str) -> dict | None:
    return _BY_ID.get(str(item_id or ""))


def by_flatpak(app_id: str) -> dict | None:
    return _BY_FLATPAK.get(str(app_id or ""))


def row(row_id: str) -> dict | None:
    return _ROWS.get(str(row_id or ""))


def emulator(emu_id: str) -> dict | None:
    item = get(emu_id)
    return item if item is not None and item["kind"] == "emulator" else None


def system_title(folder: str) -> str:
    return SYSTEMS.get(folder, (folder, ""))[0]


def system_badge(folder: str) -> str:
    return SYSTEMS.get(folder, ("", folder.upper()))[1]


def reads_library(emu: dict, srm: bool) -> bool:
    """Liest die ROM-Quelle die Ordner dieses Emulators? srm: Es gibt eine SRM-Konfiguration (dann
    liefert die Quelle „emulators“ Switch und Wii U, keine Dubletten)."""
    lib = emu.get("library")
    return lib is True or (lib == "nosrm" and not srm)


def library_systems(srm: bool = False) -> dict:
    """{ordner: [emulator, …]} der Systeme, die die ROM-Quelle liest (Reihenfolge des Katalogs)."""
    out: dict = {}
    for emu in EMULATORS:
        if not reads_library(emu, srm) or not emu["args"]:
            continue
        for folder, exts in emu["systems"].items():
            if exts:
                out.setdefault(folder, []).append(emu)
    return out


def extensions(folder: str, srm: bool = False) -> tuple:
    """Endungen eines Systems über alle Emulatoren, die es lesen (klein, mit Punkt)."""
    seen: list = []
    for emu in library_systems(srm).get(folder, []):
        for ext in emu["systems"][folder]:
            if ext not in seen:
                seen.append(ext)
    return tuple(seen)


def unique_extensions() -> dict:
    """{endung ohne Punkt: ordner} für Endungen, die genau zu einem System gehören (Archiv-Installer)."""
    owners: dict = {}
    for emu in EMULATORS:
        for folder, exts in emu["systems"].items():
            for ext in exts:
                owners.setdefault(ext.lstrip("."), set()).add(folder)
    return {ext: next(iter(folders)) for ext, folders in owners.items() if len(folders) == 1}


def runtime(rid: str) -> dict | None:
    hit = RUNTIMES.get(str(rid or ""))
    if hit is None:
        return None
    title, ref, size, download = hit
    return {"id": rid, "title": title, "ref": ref, "size_mb": size, "download_mb": download}


def runtime_by_ref(ref: str) -> dict | None:
    """Laufzeit zu einem Flatpak-Ref; unbekannte (GL-Treiber, Erweiterungen) mit lesbarem Namen."""
    ref = str(ref or "")
    for rid, (_title, known, _s, _d) in RUNTIMES.items():
        if known == ref:
            return runtime(rid)
    return None


def ref_title(ref: str) -> str:
    """„Laufzeit KDE 6.11“ für runtime/org.kde.Platform/x86_64/6.11, sonst der Name aus dem Ref."""
    hit = runtime_by_ref(ref)
    if hit is not None:
        return "Laufzeit " + hit["title"]
    parts = str(ref or "").split("/")
    if len(parts) >= 2 and parts[0] == "app":
        item = by_flatpak(parts[1])
        return item["title"] if item else parts[1]
    if len(parts) >= 4:
        return "Laufzeit %s %s" % (parts[1], parts[3])
    return str(ref or "")


def flatpak_ids() -> list:
    return [e["flatpak"] for e in EMULATORS + APPS]


def view(installed: dict | None = None) -> dict:
    """Für setup.catalog: {emulators, apps, tools, recommended, rows, sections, runtimes, systems}; installed je
    Eintrag "system" | "user" | "" (aus detect; Werkzeuge über die Schlüssel "tool:<id>")."""
    installed = installed or {}

    def where(item: dict) -> str:
        return str(installed.get(item.get("flatpak") or "", "") or "")

    emulators = []
    for e in EMULATORS:
        rt = runtime(e["runtime"])
        emulators.append({"id": e["id"], "title": e["title"], "flatpak": e["flatpak"], "size_mb": e["size_mb"],
                          "runtime": e["runtime"], "runtime_title": rt["title"] if rt else "",
                          "section": e["section"], "hint": e["hint"], "offer": e["offer"],
                          "library": e["library"] is not False, "systems": list(e["systems"]),
                          "bios": e["bios"], "installed": where(e)})
    apps = [{"id": a["id"], "title": a["title"], "flatpak": a["flatpak"], "size_mb": a["size_mb"],
             "runtime": a["runtime"], "hint": a["hint"], "default": a["default"], "tile": a["tile"],
             "installed": where(a)} for a in APPS]
    # Werkzeuge wie in S6: umu, legendary und Proton mit seinen Familien (gewählt wird "proton:<familie>")
    tools = [{"id": t["id"], "title": t["title"], "size_mb": t["size_mb"], "kind": t["kind"], "hint": t["hint"],
              "installed": bool(installed.get("tool:" + t["id"]))} for t in TOOLS if t["kind"] == "tool"]
    tools.append({"id": "proton", "title": "Proton", "kind": "proton", "hint": "Wird von GitHub geladen und geprüft.",
                  "installed": bool(installed.get("tool:proton")),
                  "families": [{"id": t["id"], "label": t["title"], "recommended": t["id"] == PROTON_IDS[0],
                                "size_mb": t["size_mb"], "download_mb": t["download_mb"]}
                               for t in TOOLS if t["kind"] == "proton"]})
    rows = []
    for r in ROWS:
        emu = emulator(r["emulator"])
        rows.append({"id": r["id"], "title": r["title"], "emulator": r["emulator"],
                     "emulator_title": emu["title"], "size_mb": emu["size_mb"], "systems": list(r["systems"]),
                     "section": r["section"], "hint": r["hint"] or emu["hint"], "offer": emu["offer"],
                     "installed": where(emu)})
    return {"emulators": emulators, "apps": apps, "tools": tools, "recommended": list(RECOMMENDED),
            "rows": rows, "sections": [{"id": s, "title": t} for s, t in SECTIONS],
            "runtimes": [runtime(rid) for rid in RUNTIMES],
            "systems": [{"id": k, "title": v[0], "badge": v[1]} for k, v in SYSTEMS.items()]}
