# PC5 Launcher

**A console-style home screen for the Game Mode of Linux PCs.** All your games from Steam, GOG, Epic, itch.io,
Battle.net, Ubisoft, Rockstar and Lutris, your emulators, TV apps, a browser and every setting of your PC: in one
place on the TV, fully usable with a controller. It runs in the same Gamescope session as Steam Big Picture, and
Steam is always one button combination away.

Free and open source (GPL-3.0) · Preview version 0.3.0 · Interface in **English and German**

[![Donate with PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white)](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)

![Home](screenshots/home.jpg)

*Deutsch: [weiter unten](#deutsch).*

## Features

### All your games in one library

- Steam, GOG, Epic Games, itch.io, Battle.net, Ubisoft Connect, Rockstar Games, Lutris and emulators side by side
- Covers and backgrounds load by themselves (Steam, GOG, Epic, Microsoft Store, Nintendo, itch.io, Battle.net;
  SteamGridDB with your own key)
- Rename games, pin favorites, hide entries, remove entries whose folder you deleted
- Steam games start without Big Picture: Steam stays invisible in the background
- Play time per game and a weekly recap, "Your week"

### Install games

- **Built-in stores** for Steam, GOG, Epic, itch.io, Battle.net, Ubisoft and Rockstar: lists by genre, store pages,
  install and play. You buy on the store's own checkout page in the built-in browser.
- The launchers of Battle.net, Ubisoft and Rockstar install and run hidden in the background; you rarely see them.
- **From archives:** zip, 7z and rar, also split into several parts or protected with a password. PC5 Launcher finds
  the game's program and adds it to the library.
- **Installers (setup.exe):** games from the GOG and itch.io stores run their installer (Inno Setup, NSIS, MSI)
  silently. Installing with your own setup.exe from Downloads or the file explorer is coming in version 0.3.1.
- Start any .exe from Downloads or the file explorer; it becomes a library entry.
- Download manager with pause, resume and the time left

### Windows games with Proton

- Proton-CachyOS or GE-Proton, chosen per game; more Proton versions under Settings › System › Updates
- Per-game switches for **FSR 4, HDR and OptiScaler**, frame-rate limit and resolution
- Damaged Proton versions are detected and downloaded again
- The controller works in XInput games too, with no setting to change

### Emulators

- The setup wizard installs emulators from Flathub (`flatpak --user`, no password): RetroArch, Dolphin, Cemu, melonDS,
  Azahar, Eden, PCSX2, RPCS3, PPSSPP, Flycast, xemu, MAME and ScummVM
- ROM folders and EmuDeck libraries appear as games. BIOS files, keys, firmware and games are yours to bring:
  PC5 Launcher never downloads any of them.

### Made for the controller

- On-screen keyboard with word suggestions (QWERTY and QWERTZ)
- Keyboard-and-mouse mode for games without controller support, with per-game button mapping
- Pointer mode for games that only use the mouse (card games, for example)
- DualSense: light bar color and brightness, vibration; the touchpad no longer moves the mouse in games

### Apps and media

- Built-in browser with bookmarks and downloads
- From the setup wizard: YouTube (TV view), Kodi, Jellyfin, Moonlight
- File explorer with a text editor for .txt and .ini files
- Save-game backups, and a trash for uninstalled games

### Control Center and settings

- **Control Center:** switch between running apps, downloads, notifications, sound, microphone, accessories,
  performance overlay
- **Settings like on a console:** sound, screen and video (HDR, VRR), network (Wi-Fi, speed test), Bluetooth and
  controllers, storage, power (sleep, sleep timer, screensaver), updates for the system, Flatpaks and Proton
- Back up your settings and take them to a new PC
- Free up space: trash, archives of installed games, large downloads
- Startup animation with its own sound, setup wizard on first start

### Safe

- Installs into your home folder only: no sudo, no password, no change to the system image
- Hold PS + Create + Options for 5 seconds and Steam Big Picture starts. After three failed starts the session
  takes the previous version, then plain Steam.
- System permissions: the few steps that need your password (like the touchpad fix) are set up only when you ask,
  with the controller, and can be removed again. The password is never stored.

## Screenshots

| | |
|---|---|
| ![Startup animation](screenshots/startup-animation.jpg) Startup animation | ![Library](screenshots/library.jpg) Library |
| ![Store](screenshots/store.jpg) Store | ![Store page](screenshots/store-page.jpg) Store page |
| ![Game settings](screenshots/game-settings.jpg) Game settings (FSR 4, HDR, OptiScaler) | ![Control Center](screenshots/control-center.jpg) Control Center |
| ![Downloads](screenshots/downloads.jpg) Downloads | ![On-screen keyboard](screenshots/keyboard.jpg) On-screen keyboard |
| ![Setup wizard](screenshots/setup-wizard.jpg) Setup wizard | ![Emulators](screenshots/setup-consoles.jpg) Choosing emulators |
| ![Your week](screenshots/your-week.jpg) Your week | ![Sleep timer](screenshots/sleep-timer.jpg) Power menu and sleep timer |
| ![Settings](screenshots/settings.jpg) Settings | ![Network](screenshots/network-speedtest.jpg) Network and speed test |
| ![Free up space](screenshots/free-up-space.jpg) Free up space | |

*Game covers in the screenshots are blurred on purpose: they belong to their publishers.*

## Install

Download `pc5-launcher-<version>.tar.gz` from [Releases](../../releases), extract it and run in Desktop Mode, in a
terminal inside the extracted folder:

    bash install.sh

Updating works the same way; your settings stay. Everything else (supported systems, requirements, the way back to
Steam, updating and uninstalling): [packaging/README.en.md](packaging/README.en.md).

| System | Status |
|---|---|
| Bazzite | main system |
| ChimeraOS, Nobara and others with gamescope-session-plus | should work (unconfirmed) |
| SteamOS (Steam Deck, Steam Machine) | not yet: WebKitGTK is missing there; the installer tells you what's missing |

The interface language follows your system; change it under Settings › System › System language.

## Support

PC5 Launcher is free and stays free. If you like it, you can give something voluntarily via PayPal to
**[pay.nino.pal@gmail.com](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)**.
A donation is a gift without anything in return: no paid features, no perks for donors.

Bugs and ideas: [Issues](../../issues).

## License and trademarks

GNU General Public License, version 3: see [LICENSE](LICENSE). No warranty. Third-party parts:
[packaging/THIRD-PARTY.md](packaging/THIRD-PARTY.md).

PlayStation, PS5 and DualSense are trademarks of Sony Interactive Entertainment; Steam and Steam Deck are
trademarks of Valve Corporation. PC5 Launcher is an independent project and is not affiliated with Sony or Valve.

## Build the package

    tools/make-dist            # from the current commit: dist/pc5-launcher-<version>.tar.gz (+ .sha256)
    tools/make-dist --check-only

Translations: `tools/i18n-extract` collects the texts of the core into `app/couch/i18n/de-templates.json`; the English
versions live in `app/couch/i18n/en.json` and `app/web/js/core/strings-en.js`.

---

## Deutsch

**Ein Konsolen-Startbildschirm für den Spielmodus von Linux-PCs.** Alle Spiele aus Steam, GOG, Epic, itch.io,
Battle.net, Ubisoft, Rockstar und Lutris, die Emulatoren, Apps für den Fernseher, ein Browser und alle Einstellungen
des Rechners: an einem Ort auf dem Fernseher, ganz mit dem Controller. Er läuft in derselben Gamescope-Sitzung wie
Steam Big Picture, und Steam ist immer nur eine Tastenkombination entfernt.

Kostenlos und quelloffen (GPL-3.0) · Vorab-Fassung 0.3.0 · Oberfläche auf **Deutsch und Englisch**

### Funktionen

**Alle Spiele in einer Bibliothek**

- Steam, GOG, Epic Games, itch.io, Battle.net, Ubisoft Connect, Rockstar Games, Lutris und Emulatoren nebeneinander
- Cover und Hintergründe kommen von selbst (Steam, GOG, Epic, Microsoft Store, Nintendo, itch.io, Battle.net;
  SteamGridDB mit eigenem Schlüssel)
- Spiele umbenennen, Favoriten, ausblenden, Einträge gelöschter Ordner entfernen
- Steam-Spiele starten ohne Big Picture: Steam bleibt unsichtbar im Hintergrund
- Spielzeit je Spiel und der Wochenrückblick „Deine Woche“

**Spiele installieren**

- **Eingebaute Stores** für Steam, GOG, Epic, itch.io, Battle.net, Ubisoft und Rockstar: Listen nach Genre,
  Store-Seiten, installieren und spielen. Gekauft wird auf der Kassenseite des Stores im eingebauten Browser.
- Die Launcher von Battle.net, Ubisoft und Rockstar installieren und laufen versteckt im Hintergrund.
- **Aus Archiven:** zip, 7z und rar, auch mehrteilig oder mit Kennwort. Der PC5 Launcher findet das Programm des
  Spiels und nimmt es in die Bibliothek auf.
- **Installationsprogramme (setup.exe):** Spiele aus den Stores von GOG und itch.io laufen ihr Installationsprogramm
  (Inno Setup, NSIS, MSI) unsichtbar durch. Installieren mit einer eigenen setup.exe aus den Downloads oder dem
  Datei-Explorer kommt mit Fassung 0.3.1.
- Jede .exe aus den Downloads oder dem Datei-Explorer starten; sie wird zum Eintrag der Bibliothek.
- Download-Manager mit Anhalten, Fortsetzen und Restzeit

**Windows-Spiele mit Proton**

- Proton-CachyOS oder GE-Proton je Spiel; weitere Proton-Versionen unter Einstellungen › System › Updates
- Je Spiel schaltbar: **FSR 4, HDR und OptiScaler**, Bildratenbegrenzung und Auflösung
- Beschädigte Proton-Versionen werden erkannt und neu geladen
- Der Controller geht auch in XInput-Spielen, ohne dass man etwas einstellen muss

**Emulatoren**

- Der Einrichtungs-Assistent installiert Emulatoren aus Flathub (`flatpak --user`, ohne Kennwort): RetroArch,
  Dolphin, Cemu, melonDS, Azahar, Eden, PCSX2, RPCS3, PPSSPP, Flycast, xemu, MAME und ScummVM
- ROM-Ordner und EmuDeck-Bibliotheken erscheinen als Spiele. BIOS, Schlüssel, Firmware und Spiele bringst du selbst
  mit: Der PC5 Launcher lädt nichts davon.

**Für den Controller gemacht**

- Bildschirmtastatur mit Wortvorschlägen (QWERTZ und QWERTY)
- Tastatur-und-Maus-Modus für Spiele ohne Controller-Unterstützung, mit Tastenbelegung je Spiel
- Zeiger-Modus für Spiele, die nur die Maus kennen (etwa Kartenspiele)
- DualSense: Farbe und Helligkeit der Leuchtleiste, Vibration; das Touchpad bewegt in Spielen nicht mehr die Maus

**Apps und Medien**

- Eingebauter Browser mit Lesezeichen und Downloads
- Aus dem Einrichtungs-Assistenten: YouTube (Fernsehansicht), Kodi, Jellyfin, Moonlight
- Datei-Explorer mit Text-Editor für .txt- und .ini-Dateien
- Sicherungen der Spielstände und ein Papierkorb für deinstallierte Spiele

**Control Center und Einstellungen**

- **Control Center:** zwischen laufenden Apps wechseln, Downloads, Mitteilungen, Ton, Mikrofon, Zubehör,
  Leistungsanzeige
- **Einstellungen wie bei einer Konsole:** Ton, Bildschirm und Video (HDR, VRR), Netzwerk (WLAN,
  Geschwindigkeitsmessung), Bluetooth und Controller, Speicher, Energie (Ruhemodus, Ruhemodus-Timer,
  Bildschirmschoner), Updates für System, Flatpaks und Proton
- Einstellungen sichern und auf einen neuen PC mitnehmen
- Platz schaffen: Papierkörbe, Archive installierter Spiele, große Downloads
- Startanimation mit eigenem Klang, Einrichtungs-Assistent beim ersten Start

**Sicher**

- Installiert nur im eigenen Benutzerordner: kein sudo, kein Kennwort, keine Änderung am System-Abbild
- PS + Create + Options 5 Sekunden halten startet Steam Big Picture. Nach drei Fehlstarten nimmt die Sitzung die
  vorige Fassung, dann das normale Steam.
- Systemfreigaben: Die wenigen Schritte, die dein Kennwort brauchen (etwa für das Touchpad), richtest du nur auf
  Wunsch mit dem Controller ein und kannst sie wieder entfernen. Das Kennwort wird nie gespeichert.

### Installieren

Paket unter [Releases](../../releases) laden, entpacken und im Desktop-Modus im entpackten Ordner `bash install.sh`
ausführen. Aktualisieren geht genauso, die Einstellungen bleiben. Alles Weitere:
[packaging/README.md](packaging/README.md).

### Unterstützen

Der PC5 Launcher ist kostenlos und bleibt es. Wenn er dir gefällt, kannst du freiwillig etwas geben, per PayPal an
**[pay.nino.pal@gmail.com](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)**
– ohne Gegenleistung.

Die Spiele-Cover auf den Bildern sind absichtlich unscharf: Sie gehören den Verlagen. Der PC5 Launcher ist ein
unabhängiges Projekt ohne Verbindung zu Sony Interactive Entertainment oder Valve.
