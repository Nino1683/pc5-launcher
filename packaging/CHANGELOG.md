# Änderungen

## 0.3.0 (2026-10-25)

- **Englische Oberfläche:** Einstellungen › System › Systemsprache (Deutsch/English); neue Geräte richten sich
  nach der Systemsprache. Bildschirmtastatur auf Englisch mit QWERTY und englischen Wortvorschlägen; Datum und
  Zahlen nach der Sprache. Auch der eingebaute Browser und das Terminal sind übersetzt.

## 0.2.0 (2026-10-24)

Diese Fassung ist nur mit Attrappen und in einem unsichtbaren Testbild geprüft, noch nicht an einem
Fernseher, einem Steam Deck oder einer Steam Machine.

- **OptiScaler je Spiel** (Spieleinstellungen › Bild): Proton-CachyOS und GE-Proton laden und schleusen ihn selbst
  ein, der Spielordner bleibt unberührt.
- **Systemfreigaben** (Einstellungen › System): Schritte, die das eigene Kennwort brauchen, mit dem Controller
  einrichten; als erstes: das Touchpad des DualSense bewegt in Spielen nicht mehr zugleich die Maus.
- **Downloads:** Restzeit auch bei normalen Downloads, lange Dateinamen ganz lesbar, Balken in eigener Zeile.
- **Bildschirmtastatur** tippt im eingebauten Browser und im Terminal jetzt auch @, Großbuchstaben und Umlaute.
- **Cover von itch.io** für Spiele aus Archiven (mit itch.io-Anmeldung).
- Fehlerbehebungen: Startweg „wie in Lutris“ blieb hängen und überging die Proton-Wahl; Fehlerfenster zeigte nur
  Umgebungsvariablen; eigene Spiele bei itch.io ohne Spiele galten als „nicht erreichbar“.
- **Paket und Installer** (E104): `install.sh`, `uninstall.sh` (nur im eigenen Benutzerordner, nie sudo, nie
  ein Kennwort), Desktop-Einträge „Spielmodus aus/an“, „Zustand“, „Deinstallieren“; Haken für
  gamescope-session-plus (Bazzite, ChimeraOS, Nobara …) und für SteamOS 3 (Ergänzungsdatei mit Weiche auf
  Valves Launcher); Prüfung der Laufzeit mit `couch-home --check`. Ohne WebKitGTK 6.0 (SteamOS, sehr
  wahrscheinlich) wird die Fassung abgelegt, aber nicht eingehängt.
- **Steam Machine und andere Rechner** (E104): keine festen Werte eines bestimmten Rechners mehr: Spiele-Ort
  einstellbar (neu: `~/Games`), Fernseh-Anschluss aus der Sitzung bzw. vom System, XDG-Ordner, Bildschirme mit
  16:10, Updates auch für Flatpaks der Nutzer-Installation, umu-launcher aus dem System oder als eigene Kopie.
  Auf SteamOS endet die Sitzung nur auf Valves Signal; Valves Kurzsitzungs-Zähler bleibt unberührt.
- **Einrichtungs-Assistent** (E105): fragt beim ersten Start nach Spiele-Ort, Shops, Konsolen, Apps und
  Windows-Spielen und installiert Emulatoren und Apps aus Flathub (nur `flatpak --user`, nie deinstallieren),
  umu-launcher und Proton. Neue Bibliotheksquelle „ROM-Ordner“ (`<Emulator-Ordner>/roms/<System>`), Start mit
  Lesezugriff auf den Emulator-Ordner. BIOS, Schlüssel, Firmware und Spiele lädt der PC5 Launcher nie.
- **Startanimation „Polarlicht“** (E103) mit neuem Klang; die variable Noto Sans (OFL) liegt bei.
- **Einstellungen sichern und übernehmen** (E106), etwa für den Umzug auf einen neuen Rechner.
- **Ruhemodus-Timer** (E107) im Ein/Aus-Menü.
- **Spielzeit-Rückblick** „Deine Woche“ (E108) mit Wochenmitteilung.
- **Tastenbelegung je Spiel** für „Als Tastatur und Maus“ (E109).
- **Platz schaffen** (E110): Papierkörbe, Archive installierter Spiele, große Downloads.
- **Internet-Geschwindigkeit messen** (E111) über den Messdienst von Cloudflare.
- Das Klangpaket mit fremden Tönen gehört nicht zum Paket; Vorgabe sind die eigenen Töne.

## 0.1.0

- Erste Fassung, nur auf dem Rechner des Entwicklers über `tools/install-release`.
