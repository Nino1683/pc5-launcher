"""Anmelde-Code vom Browser an das Home (GOG E68, Epic E73): {"code", "at"} in <state>/<shop>-login.json (0600).

Der Browser fängt die Rückleitung der Anmeldeseite ab und legt den Code ab (store); das Home nimmt ihn genau
einmal (take) und tauscht ihn gegen Schlüssel. itch.io hat mit PKCE und state einen eigenen Ablauf
(itch_account.take_code).
"""
from __future__ import annotations

import json
import os
import time

from .. import system


def store(path: str, code: str, clock=time.time) -> None:
    """Vom Browser gerufen: den Code für das Home ablegen. Atomar, nur für dich lesbar (0600); eine
    liegengebliebene ältere Zwischendatei bestimmt nie die Rechte (E99)."""
    system.write_atomic(path, json.dumps({"code": code, "at": clock()}), 0o600)


def take(path: str, ttl: float, clock=time.time) -> str:
    """Der Code, wenn er jünger als ttl ist, sonst "". Die Datei ist danach weg, auch eine alte, leere oder
    kaputte: Ein Code gilt nur einmal, und eine unlesbare Datei läge sonst für immer da und würde bei jedem
    Blick in den Store neu gelesen."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read(64 * 1024)
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):       # ValueError: kein UTF-8
        text = ""
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    code = str(data.get("code") or "")
    try:
        fresh = clock() - float(data.get("at") or 0) < ttl
    except (TypeError, ValueError):
        fresh = False
    return code if code and fresh else ""
