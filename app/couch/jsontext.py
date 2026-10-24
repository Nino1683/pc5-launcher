"""JSON-Text, der sich immer als UTF-8 schreiben lässt.

Namen aus dem Dateisystem in kaputter Kodierung (alte Archive, Latin-1 auf NTFS) kommen aus
os.listdir/os.fsdecode mit einzelnen Surrogaten (\\udc80…\\udcff). json.dumps(ensure_ascii=False)
lässt sie stehen, und das Schreiben als UTF-8 (Datei, Pipe, WebKit) scheitert dann mit
UnicodeEncodeError. In dem Fall kommt der ganze Text als \\u-Escape heraus: json.loads liefert
denselben Namen zurück, os.fsencode daraus die ursprünglichen Bytes (2026-10-18/21).
"""
from __future__ import annotations

import json
from typing import Any


def dumps(obj: Any, **kwargs: Any) -> str:
    """Wie json.dumps(obj, ensure_ascii=False, **kwargs), nur ohne Surrogate im Ergebnis."""
    kwargs.pop("ensure_ascii", None)
    text = json.dumps(obj, ensure_ascii=False, **kwargs)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, ensure_ascii=True, **kwargs)
    return text
