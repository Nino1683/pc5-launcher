# PC5 Launcher

![Startanimation](screenshots/startanimation.png)

**Ein Konsolen-Startbildschirm für den Spielmodus von Linux-PCs** – Spiele aus Steam, GOG, Epic, itch.io, Lutris
und von Emulatoren, Apps für den Fernseher, ein Browser und die Einstellungen des Rechners, alles mit dem
Controller. Er läuft in derselben Gamescope-Sitzung wie Steam Big Picture. Die Oberfläche ist deutsch.

*A console-style home screen for the game mode of Linux PCs (Bazzite and other systems with
gamescope-session-plus). The interface is in German. English notes: [packaging/README.en.md](packaging/README.en.md).*

Kostenlos und quelloffen (GPL-3.0). Vorab-Fassung 0.2.0.

## Was er kann

- Startbildschirm mit allen Spielen und Apps, Bibliothek mit Covern, Spielzeit und Wochenrückblick
- Shops: Steam, GOG, Epic, itch.io, Battle.net, Ubisoft, Rockstar – kaufen, installieren und starten in der Konsole
- Windows-Spiele mit Proton (Proton-CachyOS, GE-Proton), FSR 4, HDR, OptiScaler je Spiel ein- und ausschaltbar
- Emulatoren und Apps über einen Einrichtungs-Assistenten (nur `flatpak --user`)
- Controller-Modi für Spiele ohne Controller-Unterstützung (Tastatur und Maus, Zeiger), eigene Tastenbelegung
- Download-Manager, Datei-Explorer mit Text-Editor, Browser, Bildschirmtastatur mit Wortvorschlägen
- Einstellungen für Ton, Bild, Netzwerk, Bluetooth, Speicher, Energie, Updates, Ruhemodus-Timer, Speedtest

## Installieren

Unter [Releases](../../releases) das Paket `pc5-launcher-<version>.tar.gz` laden, entpacken und im Desktop-Modus
in einer Konsole im entpackten Ordner:

    bash install.sh

Alles Weitere, auch Systeme, Voraussetzungen, der Weg zurück zu Steam und das Deinstallieren:
[packaging/README.md](packaging/README.md).

| System | Stand |
|---|---|
| Bazzite | Hauptsystem |
| ChimeraOS, Nobara und andere mit gamescope-session-plus | sollte gehen (unbestätigt) |
| SteamOS (Steam Deck, Steam Machine) | noch nicht: WebKitGTK fehlt dort; der Installer sagt, was fehlt |

## Unterstützen

[![Spenden mit PayPal](https://img.shields.io/badge/Spenden-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)

Der PC5 Launcher ist kostenlos und bleibt es. Wenn er dir gefällt, kannst du freiwillig etwas geben, per PayPal
an **[pay.nino.pal@gmail.com](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)**. Eine Spende ist ein Geschenk ohne Gegenleistung: keine Bezahlfunktionen, keine
Vorteile für Spender.

## Lizenz und Marken

GNU General Public License, Version 3 – siehe [LICENSE](LICENSE). Ohne jede Gewährleistung. Fremde Teile:
[packaging/THIRD-PARTY.md](packaging/THIRD-PARTY.md).

PlayStation, PS5 und DualSense sind Marken von Sony Interactive Entertainment; Steam und Steam Deck sind Marken
der Valve Corporation. Der PC5 Launcher ist ein unabhängiges Projekt und steht in keiner Verbindung zu Sony oder
Valve.

## Paket bauen

    tools/make-dist            # aus dem aktuellen Commit: dist/pc5-launcher-<version>.tar.gz (+ .sha256)
    tools/make-dist --check-only
