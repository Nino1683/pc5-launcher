"""Bibliothek: Einträge (model), Zusammenführung (index), Quellen (ein Modul je Herkunft)."""
from .index import LibraryIndex, register_source, unregister_source
from .model import OPTION_DEFAULTS, Entry

__all__ = ["Entry", "LibraryIndex", "OPTION_DEFAULTS", "register_source", "unregister_source"]
