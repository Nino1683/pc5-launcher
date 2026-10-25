# PC5 Launcher

**A console-style home screen for the Game Mode of Linux PCs.** All your games from Steam, GOG, Epic, itch.io,
Battle.net, Ubisoft, Rockstar and Lutris, your emulators, TV apps, a browser and every setting of your PC: in one
place on the TV, fully usable with a controller. It runs in the same Gamescope session as Steam Big Picture, and
Steam is always one button combination away.

Free and open source (GPL-3.0) · Preview version 0.3.3 · Interface in **English and German**

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
- **Installers (setup.exe, .msi):** your own installer from Downloads, the file explorer or inside an archive runs
  silently through Proton (Inno Setup, NSIS, MSI), into its own game folder and the same prefix the game later
  starts in. Games from the GOG and itch.io stores install the same way.
- Start any .exe from Downloads or the file explorer; it becomes a library entry.
- Download manager with pause, resume and the time left

### Windows games with Proton

- Proton-CachyOS or GE-Proton, chosen per game; more Proton versions under Settings › System › Updates
- Per-game switches for **FSR 4, HDR and OptiScaler**, frame-rate limit (every value that divides your refresh
  rate, from 20 fps) and resolution
- **OptiScaler menu with the controller:** Control Center › Performance opens it in the running game. The D-pad,
  Cross and Circle work the menu, Options closes it; no keyboard needed
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
- If a game drops the DualSense over Bluetooth whenever it resumes after a pause, PC5 Launcher notices and stops
  pausing that game
- DualSense: light bar color and brightness, vibration; the touchpad no longer moves the mouse in games

### Apps and media

- Built-in browser with bookmarks and downloads
- From the setup wizard: YouTube (TV view), Kodi, Jellyfin, Moonlight
- File explorer with a text editor for .txt and .ini files
- Save-game backups, and a trash for uninstalled games

### Control Center and settings

- **Control Center:** switch between running apps, downloads, notifications, sound, microphone, accessories,
  performance overlay
- **Settings like on a console:** sound, screen and video (HDR, VRR, force composition for streaming with Sunshine), network (Wi-Fi, speed test), Bluetooth and
  controllers, storage, power (sleep, sleep timer, screensaver), updates for the system, Flatpaks and Proton
- **Updates itself:** PC5 Launcher checks GitHub once a day, tells you when there's a new version and installs it
  with one button (Settings › System › Updates). Your settings stay.
- **Themes:** six built-in color themes or your own, see [Themes](#themes)
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
| ![Free up space](screenshots/free-up-space.jpg) Free up space | ![Themes](screenshots/theme-picker.jpg) Choosing a theme |
| ![OptiScaler menu](screenshots/optiscaler-menu.jpg) OptiScaler menu in the Control Center | ![OptiScaler controls](screenshots/optiscaler-controls.jpg) Controller layout in the OptiScaler menu |

*Game covers in the screenshots are blurred on purpose: they belong to their publishers.*

## Themes

Settings › Home screen › Theme: **Default, Ocean, Emerald, Purple, Ember, Graphite** or your own. The colors change
at once, everywhere.

![The six built-in themes](screenshots/themes.jpg)

To make your own, put a `.css` file into `~/.config/couch/themes/`. The file name is the name in the list, so
`~/.config/couch/themes/Midnight.css` appears as "Midnight":

```css
:root {
  --c-accent: #4f8cff;                 /* dot for new items, progress bars */
  --c-ring-a: #cfe0ff;                 /* focus ring */
  --c-ring-b: #6d8fd6;
  --c-bg: #05070d;                     /* base color behind everything */
  --c-bd-a: rgba(40, 70, 160, .6);     /* moving lights of the background */
  --c-bd-b: rgba(90, 60, 150, .35);
  --c-bd-c: rgba(30, 100, 140, .35);
  --g-settings: linear-gradient(to bottom, #0a0f1d, #10172a);
}
```

Every color (`--c-…`) and gradient (`--g-…`) from [app/web/css/tokens.css](app/web/css/tokens.css) can be
changed. Everything else is ignored: sizes, fonts, `url()`, `@import`. So a theme you download can only change
colors; it can't load anything or break the layout. Made a nice one? Share it in [Issues](../../issues).

## Install

Download `pc5-launcher-<version>.tar.gz` from [Releases](../../releases), extract it and run in Desktop Mode, in a
terminal inside the extracted folder:

    bash install.sh

From 0.3.1 on, PC5 Launcher updates itself (Settings › System › Updates). Coming from 0.2.0 or 0.3.0, update once
the same way as installing; your settings stay. Everything else (supported systems, requirements, the way back to
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

Kostenlos und quelloffen (GPL-3.0) · Vorab-Fassung 0.3.3 · Oberfläche auf **Deutsch und Englisch**

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
- **Installationsprogramme (setup.exe, .msi):** Ein eigenes Installationsprogramm aus den Downloads, dem
  Datei-Explorer oder aus einem Archiv läuft unsichtbar über Proton durch (Inno Setup, NSIS, MSI), in einen eigenen
  Spielordner und im selben Prefix, in dem das Spiel später startet. Spiele aus den Stores von GOG und itch.io
  installieren genauso.
- Jede .exe aus den Downloads oder dem Datei-Explorer starten; sie wird zum Eintrag der Bibliothek.
- Download-Manager mit Anhalten, Fortsetzen und Restzeit

**Windows-Spiele mit Proton**

- Proton-CachyOS oder GE-Proton je Spiel; weitere Proton-Versionen unter Einstellungen › System › Updates
- Je Spiel schaltbar: **FSR 4, HDR und OptiScaler**, Bildratenbegrenzung (jeder Wert, der die Bildwiederholrate glatt
  teilt, ab 20) und Auflösung
- **OptiScaler-Menü mit dem Controller:** Control Center › Leistung öffnet es im laufenden Spiel. Steuerkreuz, Kreuz
  und Kreis bedienen das Menü, Options schließt es; keine Tastatur nötig
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
- Trennt ein Spiel den DualSense jedes Mal über Bluetooth, wenn es nach dem Anhalten weiterläuft, merkt der PC5
  Launcher das und hält dieses Spiel nicht mehr an
- DualSense: Farbe und Helligkeit der Leuchtleiste, Vibration; das Touchpad bewegt in Spielen nicht mehr die Maus

**Apps und Medien**

- Eingebauter Browser mit Lesezeichen und Downloads
- Aus dem Einrichtungs-Assistenten: YouTube (Fernsehansicht), Kodi, Jellyfin, Moonlight
- Datei-Explorer mit Text-Editor für .txt- und .ini-Dateien
- Sicherungen der Spielstände und ein Papierkorb für deinstallierte Spiele

**Control Center und Einstellungen**

- **Control Center:** zwischen laufenden Apps wechseln, Downloads, Mitteilungen, Ton, Mikrofon, Zubehör,
  Leistungsanzeige
- **Einstellungen wie bei einer Konsole:** Ton, Bildschirm und Video (HDR, VRR, Komposition erzwingen fürs Streaming mit Sunshine), Netzwerk (WLAN,
  Geschwindigkeitsmessung), Bluetooth und Controller, Speicher, Energie (Ruhemodus, Ruhemodus-Timer,
  Bildschirmschoner), Updates für System, Flatpaks und Proton
- **Aktualisiert sich selbst:** Der PC5 Launcher schaut einmal am Tag auf GitHub nach, meldet eine neue Fassung und
  installiert sie mit einem Knopfdruck (Einstellungen › System › Updates). Die Einstellungen bleiben.
- **Designs:** sechs eingebaute Farbdesigns oder ein eigenes, siehe [Designs](#designs)
- Einstellungen sichern und auf einen neuen PC mitnehmen
- Platz schaffen: Papierkörbe, Archive installierter Spiele, große Downloads
- Startanimation mit eigenem Klang, Einrichtungs-Assistent beim ersten Start

**Sicher**

- Installiert nur im eigenen Benutzerordner: kein sudo, kein Kennwort, keine Änderung am System-Abbild
- PS + Create + Options 5 Sekunden halten startet Steam Big Picture. Nach drei Fehlstarten nimmt die Sitzung die
  vorige Fassung, dann das normale Steam.
- Systemfreigaben: Die wenigen Schritte, die dein Kennwort brauchen (etwa für das Touchpad), richtest du nur auf
  Wunsch mit dem Controller ein und kannst sie wieder entfernen. Das Kennwort wird nie gespeichert.

### Designs

Einstellungen › Home-Bildschirm › Design: **Standard, Ozean, Smaragd, Violett, Glut, Graphit** oder ein eigenes. Die
Farben wechseln sofort, überall. Ein eigenes Design ist eine `.css`-Datei in `~/.config/couch/themes/`; der
Dateiname erscheint in der Liste (Beispiel oben bei [Themes](#themes)). Erlaubt sind alle Farben (`--c-…`) und
Verläufe (`--g-…`) aus [app/web/css/tokens.css](app/web/css/tokens.css); alles andere (Größen, Schriften, `url()`,
`@import`) wird übergangen. Ein Design aus dem Netz kann also nur Farben ändern.

### Installieren

Paket unter [Releases](../../releases) laden, entpacken und im Desktop-Modus im entpackten Ordner `bash install.sh`
ausführen. Ab 0.3.1 aktualisiert sich der PC5 Launcher selbst (Einstellungen › System › Updates); von 0.2.0 oder
0.3.0 aus einmal genauso wie beim Installieren, die Einstellungen bleiben. Alles Weitere:
[packaging/README.md](packaging/README.md).

### Unterstützen

Der PC5 Launcher ist kostenlos und bleibt es. Wenn er dir gefällt, kannst du freiwillig etwas geben, per PayPal an
**[pay.nino.pal@gmail.com](https://www.paypal.com/donate/?business=pay.nino.pal%40gmail.com&no_recurring=0&currency_code=EUR)**
– ohne Gegenleistung.

Die Spiele-Cover auf den Bildern sind absichtlich unscharf: Sie gehören den Verlagen. Der PC5 Launcher ist ein
unabhängiges Projekt ohne Verbindung zu Sony Interactive Entertainment oder Valve.
