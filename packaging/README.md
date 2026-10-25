# PC5 Launcher

Ein Konsolen-Startbildschirm für den Spielmodus von Linux-PCs: Spiele aus Steam, GOG, Epic, itch.io, Battle.net,
Ubisoft, Rockstar, Lutris und von Emulatoren, Apps für den Fernseher, ein Browser und die Einstellungen des Rechners, alles mit dem Controller.
Er läuft in derselben Gamescope-Sitzung wie Steam Big Picture und startet Steam, wenn ein Steam-Spiel gewählt wird.

Kostenlos und quelloffen (GPL-3.0). Diese Fassung ist ein Vorab-Paket: Was „unbestätigt“ heißt, ist auf einem
echten Gerät noch nicht gesehen. Die Oberfläche gibt es auf Deutsch und Englisch
(Einstellungen › System › Systemsprache; Vorgabe nach der Systemsprache).

Der PC5 Launcher ist ein unabhängiges Projekt und steht in keiner Verbindung zu Sony Interactive Entertainment oder
Valve.

## Unterstützte Systeme

| System | Spielmodus | Stand |
|---|---|---|
| Bazzite (Fedora Atomic) | gamescope-session-plus | Hauptsystem; alles Nötige ist dabei |
| ChimeraOS, Nobara und andere mit gamescope-session-plus | gamescope-session-plus | sollte gehen (unbestätigt) |
| SteamOS 3 (Steam Machine, Steam Deck) | Valves gamescope-session | **Stufe 2**: Die Oberfläche braucht WebKitGTK 6.0 und PyGObject, die SteamOS sehr wahrscheinlich nicht mitbringt (unbestätigt). Dann legt `install.sh` den PC5 Launcher ab, hängt ihn aber nicht ein, und der Spielmodus startet weiter mit Steam. Die Laufzeit kommt mit einer späteren Fassung (Flatpak). |
| Fedora, Arch, Ubuntu … ohne Spielmodus-Sitzung | – | geht erst mit dem Paket gamescope-session-plus (ChimeraOS-Projekt; Fedora COPR, Arch AUR) |

## Voraussetzungen

Im entpackten Ordner prüft `app/couch-home --check` (ohne etwas zu ändern), ob alles da ist: Python ab 3.10,
GTK 4, WebKitGTK 6.0, Soup 3, PyGObject, Pillow, python-evdev, python-xlib, pyudev, vdf, PyYAML, gamescope und
bubblewrap. Für fehlende Teile nennt es die Paketnamen deines Systems; installieren musst du sie selbst. Der
Installer ruft nie sudo, pkexec, steamos-readonly, pacman, rpm-ostree oder ujust auf und fragt nie nach einem
Kennwort.

## Installieren

Im Desktop-Modus eine Konsole im entpackten Ordner öffnen und:

    bash install.sh

Der Installer erkennt das System, prüft die Laufzeit, legt den PC5 Launcher unter
`~/.local/share/couch/releases/` ab, prüft die Kopie und fragt dann: „Beim nächsten Start des Spielmodus den PC5
Launcher verwenden?“ Nur mit Ja hängt er sich ein:

- mit gamescope-session-plus über einen markierten Block in `~/.config/gamescope-session-plus/sessions.d/<client>`
  (oder eine eigene Datei mit genau diesem Block),
- auf SteamOS über die Ergänzungsdatei `~/.config/systemd/user/steam-launcher.service.d/50-pc5-launcher.conf`.

Schalter: `--yes` (keine Fragen; eingehängt wird dann nur mit `--hook`), `--hook`, `--no-hook`, `--dry-run` (nur
zeigen, was geschähe), `--status`, `--rollback`, `--no-network` (der Installer lädt ohnehin nichts).

## Erster Start

Beim nächsten Start des Spielmodus kommt nach der Startanimation der Einrichtungs-Assistent: Spiele-Ort, Shops,
Konsolen und Apps. Emulatoren und Apps installiert er nur nach deiner Bestätigung, nur mit `flatpak --user` von
Flathub. BIOS-Dateien, Schlüssel, Firmware und Spiele bringst du selbst mit; der PC5 Launcher lädt nichts davon.

## Zurück zu Steam

- Am Controller PS + Create + Options 5 Sekunden halten: Steam Big Picture startet.
- Startet der PC5 Launcher dreimal nicht, nimmt die Sitzung von selbst die vorige Fassung und danach reines Steam.
- Im Desktop-Modus: Eintrag „PC5 Launcher – Spielmodus aus“ (Notschalter `~/.config/couch/disabled`), zurück mit
  „PC5 Launcher – Spielmodus an“. „PC5 Launcher – Zustand“ zeigt Fassung, Sicherungen und Haken.
- `bash uninstall.sh` entfernt den Haken ganz.

## Der Spielmodus startet nicht

1. In den Desktop-Modus wechseln (auf SteamOS: Ein/Aus-Taste halten, „Zum Desktop wechseln“; auf Bazzite im
   Anmeldebildschirm die Plasma-Sitzung wählen).
2. „PC5 Launcher – Spielmodus aus“ starten. Der nächste Spielmodus ist reines Steam.
3. Mit „PC5 Launcher – Zustand“ nachsehen, was los ist; Protokolle liegen in `~/.local/state/couch/`
   (`supervisor.log`, `home.log`).
4. Fehlt etwas oder ist eine Datei kaputt, startet die Sitzung ohnehin Steam: Der Haken prüft vor jedem Start, ob
   der Einstieg da und lesbar ist.

## Aktualisieren und zurück zur vorigen Fassung

Ab 0.3.1 aktualisiert sich der PC5 Launcher selbst: Er schaut einmal am Tag auf GitHub nach und meldet eine neue
Fassung mit einer Mitteilung. Einstellungen › System › Updates › PC5 Launcher › „Aktualisieren“ lädt das Paket von
GitHub, prüft die Prüfsumme und ruft diesen Installer auf (`install.sh --yes`); die neue Fassung gilt nach „Neu
starten“. Die tägliche Suche lässt sich auf derselben Seite ausschalten.

Von Hand installierst du eine neuere Fassung genauso mit `bash install.sh`. Sie kommt neben die alte, wird geprüft und erst
dann eingeschaltet; Einstellungen bleiben. Haken-Dateien ersetzt der Installer nur, wenn sie sich geändert haben
und du sie nicht selbst bearbeitet hast. `bash install.sh --rollback` schaltet auf die vorige Fassung zurück.

## Deinstallieren

    bash uninstall.sh

Entfernt den Block bzw. die eigene Datei in `sessions.d` (nur, wenn sie unverändert sind), die Ergänzungsdatei auf
SteamOS, die Desktop-Einträge und alle Fassungen. Einstellungen, Bibliothek und Zustand bleiben, außer mit
`--purge-data`: Dann kommen `~/.config/couch`, `~/.local/share/couch`, `~/.local/state/couch`, `~/.cache/couch` und
die WirePlumber-Regel des PC5 Launchers in den Papierkorb. Nie angefasst werden `~/Games` (Spiele, Prefixe,
Sicherungen), Steam-Dateien, Proton-Versionen, Downloads und Flatpaks; die vom Assistenten installierten Flatpaks
nennt `uninstall.sh` mit dem Befehl zum Selbstentfernen (`flatpak uninstall --user <id>`).

## Was aus dem Netz nachgeladen wird

Nur, wenn du es in der Konsole anstößt: legendary (GPL-3.0) für Epic-Spiele, umu-launcher (GPL-3.0) für
Windows-Spiele, Proton-Varianten (Proton-CachyOS, GE-Proton) von GitHub, Microsofts XAudio2 für einzelne Spiele
(unter Microsofts Lizenz), Emulatoren und Apps von Flathub. Nichts davon liegt im Paket.

## Datenschutz

Keine Telemetrie. Anmeldungen bei Shops laufen im eingebauten Browser; Schlüssel und Anmeldedaten liegen nur auf
deinem Rechner (Dateien mit Rechten 0600).

## Systemfreigaben

Ein paar Einstellungen am System brauchen dein Kennwort (sudo), etwa damit das Touchpad des DualSense in Spielen
nicht zugleich die Maus bewegt. Die richtest du, wenn du willst, im PC5 Launcher unter Einstellungen › System ›
Systemfreigaben mit dem Controller ein. Dort steht jede Freigabe mit Erklärung; sie lässt sich wieder entfernen.
Das Kennwort geht nur an sudo und wird nirgends gespeichert. Ohne Freigaben läuft der PC5 Launcher trotzdem.

## Unterstützen

Der PC5 Launcher ist kostenlos und bleibt es. Wenn er dir gefällt, kannst du freiwillig etwas geben, per PayPal an
**pay.nino.pal@gmail.com**. Eine Spende ist ein Geschenk ohne Gegenleistung: Es gibt keine Bezahlfunktionen und
keine Vorteile für Spender.

## Lizenz

Copyright (C) 2026 Die Autoren des PC5 Launchers

Dieses Programm ist freie Software: Du kannst es unter den Bedingungen der GNU General Public License, Version 3,
wie von der Free Software Foundation veröffentlicht, weitergeben und/oder ändern. Es wird in der Hoffnung
verbreitet, dass es nützlich ist, aber OHNE JEDE GEWÄHRLEISTUNG, sogar ohne die implizite Gewährleistung der
MARKTREIFE oder der EIGNUNG FÜR EINEN BESTIMMTEN ZWECK. Den vollständigen Text findest du in `LICENSE`
(maßgeblich ist das englische Original).

Fremde Teile behalten ihre eigenen Lizenzen: `THIRD-PARTY.md`.

## Marken

PlayStation, PS5 und DualSense sind Marken von Sony Interactive Entertainment. Steam und Steam Deck sind Marken
der Valve Corporation. Alle anderen Marken gehören ihren Inhabern. Sie werden hier nur genannt, um die Geräte und
Dienste zu bezeichnen, mit denen der PC5 Launcher zusammenarbeitet.
