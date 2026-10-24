"""couch – Konsolen-Oberfläche für den Game-Mode, nach außen „PC5 Launcher“ (seit 2026-10-19; der Name
steht in PRODUCT, intern heißt alles weiter couch). Bauplan: docs/ARCHITECTURE.md."""
import os

HOME_APPID = 0x7F000001          # App-ID des Home-Fensters (E3)
APP_ID_BASE = 0x7F100000         # gestartete Apps: APP_ID_BASE + n


def _version() -> str:
    here = os.path.dirname(os.path.realpath(__file__))
    for rel in ("../../VERSION", "../VERSION"):
        try:
            with open(os.path.join(here, rel), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "0"


VERSION = _version()

PRODUCT = "PC5 Launcher"           # Name in der Oberfläche (Wunsch 2026-10-19: „Benenne couch in PC5 Launcher um“)

# E104: App-Kennung gegenüber GTK und dem Tonsystem, eine Konstante für alles (Platzhalter bis zur
# endgültigen Entscheidung, Bauplan E103 Frage 10).
APP_ID = "io.github.pc5launcher.Launcher"
