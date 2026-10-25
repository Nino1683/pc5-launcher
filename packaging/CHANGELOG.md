# Änderungen

## 0.3.3 (2026-10-25)

- **OptiScaler-Menü mit dem Controller:** Läuft ein Spiel mit OptiScaler, steht im Control Center unter Leistung
  „OptiScaler-Menü“. Es öffnet das Menü von OptiScaler (sonst nur über die Einfg-Taste), und der Controller bedient
  es: Steuerkreuz wählt, Kreuz ändert, Kreis geht zurück, L1/R1 blättern, Options schließt das Menü. Danach steuert
  der Controller wieder das Spiel.
- **Controller bleibt verbunden:** Manche Spiele sprechen den DualSense selbst an und trennen ihn über Bluetooth,
  wenn sie nach dem Anhalten (Control Center, Home) weiterlaufen. Passiert das zweimal kurz nach dem Fortsetzen,
  hält der PC5 Launcher dieses Spiel nicht mehr an und sagt es per Mitteilung. Umstellen lässt es sich in den
  Spieleinstellungen unter „Im Hintergrund anhalten“.

## 0.3.2 (2026-10-25)

- **Komposition erzwingen** (Einstellungen › Bildschirm und Video): Für Streaming mit Sunshine und ähnlichen
  Programmen, wenn dort nur ein schwarzes Bild ankommt (Ton und Steuerung gehen). Entspricht „Force composition“ in
  den Entwickler-Einstellungen von Steam Big Picture. Gemeldet auf Reddit, danke!

## 0.3.1 (2026-10-25)

- **Aktualisiert sich selbst:** Der PC5 Launcher sucht einmal am Tag bei GitHub nach einer neuen Fassung und meldet
  sie mit einer Mitteilung. Unter Einstellungen › System › Updates › PC5 Launcher lädt „Aktualisieren“ das Paket,
  prüft die Prüfsumme und installiert es neben die laufende Fassung; sie gilt nach „Neu starten“. Von 0.2.0 oder
  0.3.0 aus einmal von Hand aktualisieren.
- **Designs:** Einstellungen › Home-Bildschirm › Design. Sechs Farbdesigns (Standard, Ozean, Smaragd, Violett, Glut,
  Graphit) oder ein eigenes: eine `.css`-Datei mit Farben in `~/.config/couch/themes/`. Ein Design ändert nur
  Farben, nichts wird nachgeladen. Die Idee kam aus der Community auf Reddit.
- **Eigene Installationsprogramme:** Eine setup.exe oder .msi aus den Downloads, dem Datei-Explorer oder aus einem
  Archiv installiert der PC5 Launcher jetzt still über Proton (Inno Setup, NSIS, MSI), in einen eigenen Spielordner
  und im selben Prefix, in dem das Spiel später startet. Fortschritt und Abbrechen stehen unter Downloads;
  unbekannte Installer und gewöhnliche Programme bekommen eine klare Meldung. Mit echten Installern noch nicht am
  Fernseher geprüft.
- **Bildratenbegrenzung:** alle Werte, die die Bildwiederholrate glatt teilen, ab 20 (bei 144 Hz zum Beispiel 24,
  36, 48, 72), statt nur 30, 40 und 60.
- **Englisch:** `install.sh`, `uninstall.sh` und `couch-home --check` sprechen auf englischen Systemen Englisch;
  einige Einstellungen standen noch auf Deutsch (24-Stunden-Anzeige, Zeitangaben, Wochentage, Bilder pro Sekunde).

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
