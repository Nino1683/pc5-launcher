"""Anbindung an Gamescope: atoms (X-Eigenschaften, Beobachter), focus (wer vorn ist,
Overlay, Sitzungsvorgaben), ctl (Wayland-Protokoll gamescope_control). Bauplan 4.4."""


class GamescopeError(Exception):
    """Gamescope ist nicht erreichbar, oder das Display gehört nicht zu Gamescope."""
