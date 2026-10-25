# PC5 Launcher

A console-style home screen for the game mode of Linux PCs: games from Steam, GOG, Epic, itch.io, Battle.net,
Ubisoft, Rockstar, Lutris and emulators, TV apps, a browser and the PC's settings, all with a controller. It runs in the same Gamescope session as
Steam Big Picture and starts Steam when a Steam game is chosen.

Free and open source (GPL-3.0). This is a preview package: “unconfirmed” means not yet seen on a real device. The
interface is available in English and German (Settings › System › System language; default from your system
language).

PC5 Launcher is an independent project and is not affiliated with Sony Interactive Entertainment or Valve.

## Supported systems

| System | Game mode | Status |
|---|---|---|
| Bazzite (Fedora Atomic) | gamescope-session-plus | main system; everything needed is included |
| ChimeraOS, Nobara and others with gamescope-session-plus | gamescope-session-plus | should work (unconfirmed) |
| SteamOS 3 (Steam Machine, Steam Deck) | Valve's gamescope-session | **stage 2**: the interface needs WebKitGTK 6.0 and PyGObject, which SteamOS very likely lacks (unconfirmed). Then `install.sh` places PC5 Launcher but does not hook it in, and game mode keeps starting Steam. The runtime will come with a later version (Flatpak). |
| Fedora, Arch, Ubuntu … without a game mode session | – | only with the gamescope-session-plus package (ChimeraOS project; Fedora COPR, Arch AUR) |

## Requirements

In the unpacked folder, `app/couch-home --check` checks (without changing anything) that everything is there:
Python 3.10 or newer, GTK 4, WebKitGTK 6.0, Soup 3, PyGObject, Pillow, python-evdev, python-xlib, pyudev, vdf, PyYAML,
gamescope and bubblewrap. For missing parts it names your system's package names; you install them yourself. The
installer never calls sudo, pkexec, steamos-readonly, pacman, rpm-ostree or ujust and never asks for a password.

## Installing

In desktop mode, open a terminal in the unpacked folder and run:

    bash install.sh

The installer detects the system, checks the runtime, places PC5 Launcher under `~/.local/share/couch/releases/`,
checks the copy and then asks whether to use PC5 Launcher the next time game mode starts. Only on yes does it hook
itself in:

- with gamescope-session-plus through a marked block in `~/.config/gamescope-session-plus/sessions.d/<client>` (or
  an own file containing exactly that block),
- on SteamOS through the drop-in `~/.config/systemd/user/steam-launcher.service.d/50-pc5-launcher.conf`.

Options: `--yes` (no questions; hooks in only with `--hook`), `--hook`, `--no-hook`, `--dry-run` (only show what
would happen), `--status`, `--rollback`, `--no-network` (the installer downloads nothing anyway).

## First start

The next time game mode starts, the setup assistant follows the start animation: game location, stores, consoles
and apps. It installs emulators and apps only after you confirm, only with `flatpak --user` from Flathub. BIOS files,
keys, firmware and games are yours to bring; PC5 Launcher never downloads any of them.

## Back to Steam

- Hold PS + Create + Options on the controller for 5 seconds: Steam Big Picture starts.
- If PC5 Launcher fails to start three times, the session takes the previous version and then plain Steam.
- In desktop mode: the entry “PC5 Launcher – game mode off” (kill switch `~/.config/couch/disabled`), back with “PC5
  Launcher – game mode on”. “PC5 Launcher – status” shows version, safeguards and hook.
- `bash uninstall.sh` removes the hook completely.

## Game mode does not start

1. Switch to desktop mode (on SteamOS: hold the power button, “Switch to Desktop”; on Bazzite choose the Plasma
   session at the login screen).
2. Run “PC5 Launcher – game mode off”. The next game mode is plain Steam.
3. Check “PC5 Launcher – status”; logs are in `~/.local/state/couch/` (`supervisor.log`, `home.log`).
4. If a file is missing or broken, the session starts Steam anyway: the hook checks the entry before every start.

## Updating and rolling back

From 0.3.1 on, PC5 Launcher updates itself: it checks GitHub once a day and shows a notification when there is a new
version. Settings › System › Updates › PC5 Launcher › "Update" downloads the package from GitHub, verifies its
checksum and runs this installer (`install.sh --yes`); the new version starts after "Restart". Turn the daily check
off on the same page.

You can also install a newer version the same way with `bash install.sh`. It is placed next to the old one, checked and only then
switched on; settings stay. Hook files are replaced only when they changed and you did not edit them yourself.
`bash install.sh --rollback` switches back to the previous version.

## Uninstalling

    bash uninstall.sh

Removes the block or the own file in `sessions.d` (only if unchanged), the drop-in on SteamOS, the desktop entries
and all versions. Settings, library and state stay, except with `--purge-data`: then `~/.config/couch`,
`~/.local/share/couch`, `~/.local/state/couch`, `~/.cache/couch` and PC5 Launcher's WirePlumber rule go to the
trash. Never touched: `~/Games` (games, prefixes, backups), Steam files, Proton versions, downloads and Flatpaks;
`uninstall.sh` lists the Flatpaks the assistant installed with the command to remove them yourself
(`flatpak uninstall --user <id>`).

## What is downloaded later

Only when you start it in the console: legendary (GPL-3.0) for Epic games, umu-launcher (GPL-3.0) for Windows games,
Proton variants (Proton-CachyOS, GE-Proton) from GitHub, Microsoft's XAudio2 for some games (under Microsoft's
license), emulators and apps from Flathub. None of it is in the package.

## Privacy

No telemetry. Store sign-ins run in the built-in browser; keys and credentials stay on your PC (files with mode 0600).

## System permissions

A few system settings need your password (sudo), for example so that the DualSense touchpad does not also move
the mouse in games. If you want them, set them up inside PC5 Launcher under Einstellungen › System ›
Systemfreigaben with the controller. Each one is explained there and can be removed again. The password only goes
to sudo and is never stored. PC5 Launcher works without them.

## Support

PC5 Launcher is free and stays free. If you like it, you can give something voluntarily via PayPal to
**pay.nino.pal@gmail.com**. A donation is a gift without anything in return: there are no paid features and no
perks for donors.

## License

Copyright (C) 2026 The PC5 Launcher authors

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public
License as published by the Free Software Foundation, version 3. It is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See `LICENSE` for the full text.

Third-party parts keep their own licenses: `THIRD-PARTY.md`.

## Trademarks

PlayStation, PS5 and DualSense are trademarks of Sony Interactive Entertainment. Steam and Steam Deck are
trademarks of Valve Corporation. All other trademarks belong to their owners. They are only named to identify the
devices and services PC5 Launcher works with.
