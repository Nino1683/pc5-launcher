"""Anmelde-Code vom Browser an das Home (GOG E68, Epic E73): {"code", "at"} in <state>/<shop>-login.json (0600).

Der Browser fängt die Rückleitung der Anmeldeseite ab und legt den Code ab (store); das Home nimmt ihn genau
einmal (take) und tauscht ihn gegen Schlüssel. itch.io hat mit PKCE und state einen eigenen Ablauf
(itch_account.take_code).
"""
from __future__ import annotations

import json
import os
import threading
import time

from .. import system


def store(path: str, code: str, clock=time.time) -> None:
    """Vom Browser gerufen: den Code für das Home ablegen. Atomar, nur für dich lesbar (0600); eine
    liegengebliebene ältere Zwischendatei bestimmt nie die Rechte (E99)."""
    system.write_atomic(path, json.dumps({"code": code, "at": clock()}), 0o600)


def claim(path: str) -> str | None:
    """Die Datei genau einmal nehmen: erst umbenennen (atomar, von mehreren Fäden gewinnt einer), dann lesen und
    löschen. None: keine da, oder ein anderer Faden war schneller (sonst tauschten zwei Fäden denselben Code, und der
    zweite meldete eine gescheiterte Anmeldung). "" bei unlesbarem Inhalt."""
    mine = "%s.%d-%d.genommen" % (path, os.getpid(), threading.get_ident())
    try:
        os.rename(path, mine)
    except OSError:
        return None
    try:
        with open(mine, encoding="utf-8") as fh:
            return fh.read(64 * 1024)
    except (OSError, ValueError):       # ValueError: kein UTF-8
        return ""
    finally:
        try:
            os.unlink(mine)
        except OSError:
            pass


def take(path: str, ttl: float, clock=time.time) -> str:
    """Der Code, wenn er jünger als ttl ist, sonst "". Die Datei ist danach weg, auch eine alte, leere oder
    kaputte: Ein Code gilt nur einmal, und eine unlesbare Datei läge sonst für immer da und würde bei jedem
    Blick in den Store neu gelesen."""
    text = claim(path)
    if not text:
        return ""
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
