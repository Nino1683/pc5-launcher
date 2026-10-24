"""Feste Angaben zum Rechner, Sensoren, Uhr, logind-Fähigkeiten. Alles nur lesend.

Befehle laufen ohne DISPLAY/WAYLAND_DISPLAY: In einer Shell auf diesem Rechner zeigen die auf
den Desktop des Nutzers, und kein Programm soll dort je ein Fenster öffnen (05 Abschnitt 0, Punkt 6).
"""
from __future__ import annotations

import glob
import os
import re

NO_DISPLAY = ["env", "-u", "DISPLAY", "-u", "WAYLAND_DISPLAY", "LC_ALL=C"]


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def cpu_model(cpuinfo: str = "/proc/cpuinfo") -> str:
    for line in _read(cpuinfo).splitlines():
        if line.startswith("model name"):
            name = line.split(":", 1)[1].strip()
            # "Intel(R) Core(TM) i7-14700K" -> "Intel Core i7-14700K"
            return re.sub(r"\s+", " ", re.sub(r"\((R|TM|r|tm)\)", "", name)).strip()
    return ""


def mem_total(meminfo: str = "/proc/meminfo") -> int | None:
    for line in _read(meminfo).splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


def parse_vulkaninfo(text: str) -> dict:
    """Erste echte Grafikkarte aus `vulkaninfo --summary`: {"gpu", "mesa"}. llvmpipe zählt nicht."""
    blocks: list[dict] = []
    for line in text.splitlines():
        if re.match(r"\s*GPU\d+:\s*$", line):
            blocks.append({})          # je Gerät ein Block "GPU<n>:"
            continue
        m = re.match(r"\s*(\w+)\s*=\s*(.+?)\s*$", line)
        if m and blocks:
            blocks[-1][m.group(1)] = m.group(2)
    real = [b for b in blocks if "CPU" not in b.get("deviceType", "") and b.get("deviceName")]
    pick = (real or [b for b in blocks if b.get("deviceName")] or [{}])[0]
    gpu = pick.get("deviceName", "")
    m = re.search(r"Mesa\s+([\w.\-]+)", pick.get("driverInfo", ""))
    mesa = m.group(1) if m else ""
    return {"gpu": gpu, "mesa": mesa}


def read_vulkan(run_read) -> dict:
    rc, out, _ = run_read(NO_DISPLAY + ["vulkaninfo", "--summary"], timeout=8)
    return parse_vulkaninfo(out) if rc == 0 else {"gpu": "", "mesa": ""}


def parse_gamescope_version(text: str) -> str:
    """gamescope schreibt die Version mit Farbcodes nach stderr."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    m = re.search(r"gamescope version (\S+)", clean)
    return m.group(1) if m else ""


def read_gamescope_version(run_read) -> str:
    # --backend headless: Würde --version je nicht erkannt, entstünde nur eine unsichtbare Instanz,
    # die an der Frist endet – nie ein Fenster und nie ein Griff nach dem Bildschirm.
    _rc, out, err = run_read(NO_DISPLAY + ["gamescope", "--backend", "headless", "--version"], timeout=5)
    return parse_gamescope_version((err or "") + "\n" + (out or ""))


def hwmon_dir(name: str, root: str = "/sys/class/hwmon") -> str | None:
    """hwmon-Ordner nach Namen; die Nummer (hwmon2) wechselt zwischen Neustarts."""
    for p in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        if _read(os.path.join(p, "name")) == name:
            return p
    return None


def _milli(path: str) -> float | None:
    raw = _read(path)
    try:
        return round(int(raw) / 1000, 1)
    except ValueError:
        return None


def sensors(root: str = "/sys/class/hwmon") -> dict:
    """{"cpu_temp", "gpu_temp", "gpu_power", "gpu_power_cap"}; None, wo ein Sensor fehlt. Unter 1 ms."""
    out = {"cpu_temp": None, "gpu_temp": None, "gpu_power": None, "gpu_power_cap": None}
    cpu = hwmon_dir("coretemp", root) or hwmon_dir("k10temp", root) or hwmon_dir("zenpower", root)
    if cpu:
        out["cpu_temp"] = _milli(os.path.join(cpu, "temp1_input"))
    gpu = hwmon_dir("amdgpu", root)
    if gpu:
        out["gpu_temp"] = _milli(os.path.join(gpu, "temp1_input"))
        for key, name in (("gpu_power", "power1_average"), ("gpu_power_cap", "power1_cap")):
            raw = _read(os.path.join(gpu, name))
            if raw.isdigit():
                out[key] = round(int(raw) / 1e6)
    return out


def parse_timedatectl(text: str) -> dict:
    """`timedatectl show`: Schlüssel=Wert, unabhängig von der Sprache."""
    td = dict(l.split("=", 1) for l in text.splitlines() if "=" in l)
    return {"timezone": td.get("Timezone", ""), "ntp": td.get("NTP") == "yes",
            "synchronized": td.get("NTPSynchronized") == "yes"}


def read_time(run_read) -> dict:
    rc, out, _ = run_read(["timedatectl", "show"], timeout=5)
    return parse_timedatectl(out if rc == 0 else "")


def logind_can(run_read, what: str) -> bool:
    """CanPowerOff | CanReboot | CanSuspend: True nur bei "yes" (ohne Kennwort erlaubt)."""
    rc, out, _ = run_read(["busctl", "--system", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                           "org.freedesktop.login1.Manager", what], timeout=5)
    return rc == 0 and '"yes"' in out
