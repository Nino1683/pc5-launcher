"""GLib-Quelle für eine Dateinummer: die eine Stelle für gs/atoms, proc/manager, input/service, oskdirect und das
Terminal.

GLib.unix_fd_add_full ist in neuem PyGObject abgekündigt (Warnung bei jedem Aufruf); sein Nachfolger
GLibUnix.fd_add_full fehlt in älterem. Beide haben dieselbe Unterschrift: (Vorrang, Dateinummer, Bedingung, Rückruf,
*Zusatzdaten), der Rückruf bekommt (Dateinummer, Bedingung, *Zusatzdaten) und bleibt, solange er True liefert.
gi wird erst beim ersten Aufruf geladen.
"""
from __future__ import annotations

from typing import Any, Callable

_impl: Callable[..., int] | None = None


def _resolve() -> Callable[..., int]:
    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix
        return GLibUnix.fd_add_full
    except (ValueError, ImportError, AttributeError):
        return GLib.unix_fd_add_full


def fd_add(priority: int, fd: int, condition: Any, callback: Callable[..., bool], *data: Any) -> int:
    """Quelle anmelden; liefert ihre Kennung (für GLib.source_remove)."""
    global _impl
    if _impl is None:
        _impl = _resolve()
    return _impl(priority, fd, condition, callback, *data)
