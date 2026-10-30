# Änderungen

## 0.5.1 (2026-10-30)

Nur Fehlerbehebungen.

- **Hintergrund-Apps bleiben an:** Discord und der App Store, die über ihre Kachel nach vorn geholt wurden, sowie die
  Beobachter für Ton und Netz endeten nach rund 30 Sekunden. Danach ging der Ton nicht mehr von selbst auf den
  Fernseher.
- **Browser:** Codes für die Anmeldung bei GOG und Epic werden nur noch während deiner eigenen Anmeldung aus dem
  Store angenommen; vorher konnte eine fremde Seite die Konsole mit einem anderen GOG-Konto anmelden. Ein
  Bildschirm, der noch nicht zu sehen ist, schluckt Kreis nicht mehr; eine kaputte Startseite oder Lesezeichen-Datei
  hält den Browser nicht mehr auf.
- **Anmeldedaten:** Die Dateien der Anmeldung bei GOG, Epic und Ubisoft sind immer nur für dich lesbar, auch wenn
  vorher etwas schiefging.
- **Einstellungen › Speicher:** Der Konsolenspeicher zeigt wieder Größe und freien Platz statt „Nicht verbunden“.
- **Papierkorb des Installers:** Er hieß „Trophäen“ und zeigte deren Texte; jetzt wieder „Papierkorb“ mit der Frist.
- **Datei-Explorer:** Kopieren, Verschieben, Umbenennen und Wiederherstellen ersetzen oder löschen nie etwas, das
  zwischendurch an derselben Stelle entstanden ist.
- **Deinstallieren:** Lässt sich nicht prüfen, ob eine App gerade woanders läuft, wird nicht deinstalliert.
- **Stores:** Ein Abbruch, während Steam noch Daten liest, bleibt nicht mehr bei „Wird abgebrochen …“ hängen; ein
  abgebrochener Installer stoppt auch kurz vor seinem Start; der Prefix eines abgebrochenen Spiels bleibt liegen,
  solange darin noch Wine läuft. Bilder neuer Steam-Spiele in langen Listen fehlen nicht mehr, und gleichzeitige
  Anfragen zerstören den Zwischenspeicher nicht mehr.
- **Ein/Aus und Beenden:** Nach einem Fehler meldete das Menü dauerhaft „Es läuft schon ein Vorgang“; beim Beenden
  des Home gehen Einstellungen und Spielzeiten auch dann nicht verloren, wenn vorher etwas scheitert.
- **Einrichtung:** Ein abgebrochener Download von umu-launcher wird beim nächsten Versuch fortgesetzt statt als
  „beschädigt“ gemeldet.
- **Fernseher:** Er wird auch an der zweiten Grafikkarte gefunden; zweimal schnell „Auswählen“ beim ersten Koppeln
  kann die Kopplung nicht mehr unbrauchbar machen; Verbindungen werden sauber geschlossen.
- **Discord:** Eine ungewöhnliche Antwort von Discord beendet „Spielt gerade“ nicht mehr bis zum Neustart.
- **Rückfragen:** Vier Aktionen in den Einstellungen fragen jetzt immer nach, bevor sie etwas tun.
- **Englische Oberfläche:** Anführungszeichen im englischen Stil; Menüs mit Sonderzeichen im Namen gehen wieder auf.
- **Speicher unter dem Bildschirmschoner:** Hintergrundbilder werden freigegeben, ein Trailer im Vollbild hält an.

## 0.5.0 (2026-10-30)

- **Fernseher steuern:** Hat dein Fernseher Google TV oder Android TV, steuert der PC5 Launcher ihn über das Heimnetz,
  auch ohne HDMI-CEC am PC: im Control Center unter „Fernseher“ Leiser, Lauter, Stumm, Eingang wechseln und
  Ausschalten. Einschalten und koppeln unter Einstellungen › Bildschirm und Video › Fernseher steuern (aus ist
  Vorgabe): Der PC5 Launcher sucht den Fernseher im Netz (oder du gibst seine Adresse ein), der Fernseher zeigt einen
  Code, den du mit dem Controller eintippst. Wahlweise schaltet er den Fernseher beim Ruhemodus und Ausschalten aus
  (immer oder nur, wenn er gerade den PC zeigt) und beim Aufwachen wieder ein, gleich auf den Eingang des PCs. Mit
  „Eingänge genauer erkennen“ (Netzwerk-Debugging am Fernseher) kennt er die Eingänge samt den Namen der Geräte
  daran und schaltet zuverlässig um; ohne geht es über die Tasten HDMI 1 bis 4 und das Eingangsmenü. Damit der
  Fernseher aus dem Standby aufwacht, muss in seinen Einstellungen „Netzwerk-Standby“ (je nach Hersteller „Wake on
  LAN“) an sein.
- **Neuer Reiter „Apps“:** Das Home hat jetzt drei Reiter, „Spiele“, „Apps“ und „Medien“ (L1/R1 wechseln der Reihe
  nach). Unter „Apps“ stehen der App Store (Bazaar, wenn es auf dem Rechner ist, zum Installieren von Apps und Spielen aus Flathub),
  Browser, Dateien, Desktop, Claude Code, Discord und EmuDeck, unter „Medien“ YouTube, Kodi, Jellyfin, Moonlight und
  die Mediengalerie. Was du in Bazaar (oder sonst wie) als Flatpak installierst, erscheint von selbst als Kachel, auch
  während Bazaar noch offen ist: Spiele unter „Spiele“, Video- und Musik-Apps unter „Medien“, alles andere unter
  „Apps“; Deinstalliertes verschwindet wieder. Kacheln lassen sich jetzt in jeder Reihe verschieben (Optionsmenü ›
  „Kachel verschieben“, je Reihe „Reihenfolge zurücksetzen“), und Flatpak-Apps wie YouTube oder Kodi lassen sich dort
  auch deinstallieren; deine Daten der App bleiben dabei erhalten.
- **Apps ausblenden:** Jede Kachel unter „Apps“ und „Medien“ hat im Optionsmenü „Ausblenden“ (auch Browser, Desktop,
  Dateien oder die Mediengalerie; nur Steam nicht). Die App bleibt installiert, sie verschwindet nur aus dem Home.
  Zurück holst du sie unter Einstellungen › Apps › Ausgeblendete Apps mit „Wieder anzeigen“.
- **App Store schließt sich richtig:** Machst du den App Store zu (sein Fenster oder „Schließen“ im PC5 Launcher), wird
  er ganz beendet. Läuft gerade ein Download oder Update, fragt der PC5 Launcher vorher nach; geht nur das Fenster zu,
  läuft er unsichtbar weiter, bis die Übertragung fertig ist, und beendet sich dann selbst. Ausschalten und Neustart
  sagen dazu, wenn im App Store noch etwas lädt.
- **Claude Code mitstarten:** Unter Einstellungen › Apps lässt sich einschalten, dass Claude Code mit dem PC5 Launcher
  im Hintergrund startet und gleich die letzte Unterhaltung lädt (aus ist Vorgabe). Beim Einschalten startet es sofort.
  Die Kachel oder die Karte im Control Center holt es nach vorn.
- **Sicherung mit allen Reihen:** „Einstellungen sichern und übernehmen“ nimmt jetzt auch die Reihenfolge unter
  „Apps“ und „Medien“ und die ausgeblendeten Kacheln mit. Ältere Sicherungen lassen sich weiter übernehmen.
- **Discord, wenn du willst:** Unter Einstellungen › Apps › Discord lässt sich der offizielle Discord-Client
  einschalten (aus ist Vorgabe) und von dort aus Flathub installieren. Er startet dann mit dem PC5 Launcher
  unsichtbar im Hintergrund und läuft auch während eines Spiels weiter, für Nachrichten und Sprachchat. Seine
  Nachrichten erscheinen als Mitteilung, im Control Center gibt es ein Symbol „Discord“ mit „Mikrofon in Discord
  stumm/an“, „Ton in Discord aus/an“ und „Discord öffnen“ (dafür trägst du in Discord zwei Tastenbelegungen ein, die
  Seite nennt sie), und mit einer eigenen, kostenlosen Application ID sehen deine Freunde, was du gerade spielst.
  Damit Discord im Spielmodus überhaupt Mitteilungen schicken darf, gibt der PC5 Launcher seinem Flatpak vor dem
  Start die Freigabe dafür (dieselbe wie mit Flatseal). Ausgeschaltet läuft und erscheint davon nichts.
- **Lutris wird nicht mehr gebraucht:** Deine Lutris-Spiele werden beim ersten Start dieser Fassung einmal
  übernommen, mit Spielzeit, Einstellungen, Favoriten, Spielständen und Bildern, und starten danach ohne Lutris.
  Was später noch in Lutris dazukommt, erscheint nicht mehr in der Bibliothek. Den Startweg „wie in Lutris“ gibt es
  nicht mehr: Solche Spiele starten wieder mit Proton. Für GOG-Installer, die sich nur auspacken lassen, sucht der
  PC5 Launcher innoextract jetzt auch in Homebrew (`brew install innoextract`) statt bei Lutris.
- **Control Center:** Alle laufenden Spiele und Apps haben jetzt eine Karte (bisher nur zwei), die vorn laufende
  zuerst; sind es mehr, als nebeneinander passen, rückt die Reihe mit. Die große Karte „Zum Home-Bildschirm“ ist
  weg, dafür gibt es das Home-Symbol in der Leiste.
- **Starten mit wine-run:** Gibt es auf dem Rechner das Skript `~/.local/bin/wine-run`, lässt sich ein eigenes
  Windows-Spiel in den Spieleinstellungen unter „Starten mit“ auf wine-run stellen. Es startet dann wie ein
  Doppelklick im Desktop statt mit Proton.
- **DXVK je Spiel:** In den Spieleinstellungen unter „Bild“ lässt sich DXVK für ein Spiel ausschalten. Es startet
  dann mit Wines eigenem Direct3D (WineD3D), das ist langsamer, hilft aber manchen Spielen, die mit DXVK abstürzen
  oder falsch aussehen.
- **Proton bleibt heil:** Steam am Desktop ließ beim Start mehrere Proton-Versionen gleichzeitig auf einem Prefix
  laufen, und eine überschrieb dabei Dateien einer anderen. Danach starteten Spiele mit der betroffenen Version
  nicht mehr, bis der PC5 Launcher sie neu geladen hatte. Proton-Versionen, die der PC5 Launcher installiert oder
  repariert hat, sind jetzt schreibgeschützt und lassen sich so nicht mehr überschreiben.
- **Controller-Mitteilungen:** „Akku des Controllers fast leer“ kommt nicht mehr bei jedem Wiederverbinden, sondern
  erst wieder, nachdem der Controller geladen wurde; nach einem Neustart des PC5 Launchers meldet sich ein schon
  verbundener Controller nicht mehr mit „Controller verbunden“.
- **Leistung im Home:** Nach dem Weg aus der Spielansicht zurück in die Kachelreihe lief der Schimmer des Fokusrings
  ohne Ende weiter, und das Home zeichnete auch in Ruhe 60 Bilder in der Sekunde neu (seit 0.4.0). Jetzt steht er
  nach einigen Sekunden wieder still.
- **Home:** Nach kurzem Hin und Her zwischen zwei Spielen fehlten Kurzbeschreibung und Karten; die Karten unter dem
  Spiel springen nicht mehr um, wenn Neuigkeiten nachkommen; in der Widget-Leiste geht der Fokus nicht mehr verloren,
  wenn „Zuletzt gespielt“ neu gezeichnet wird. „Beschreibung“ steht in der Spielansicht jetzt oben rechts neben „…“
  statt dicht unter den Steam-Bildern.
- **Trophäen:** Die Übersicht öffnet sofort, die Seltenheiten kommen im Hintergrund nach (vorher mehrere Sekunden
  Warten beim ersten Öffnen).
- **Nach einem Update** kommt der Hinweis nur noch einmal (als Frage im Home); „Was ist neu“ erscheint nach dem
  Ansehen nicht wieder, auch wenn gerade nach Updates gesucht wurde.
- **Englische Oberfläche:** Spieltitel mit Tausender-Komma („Warhammer 40,000“) bleiben, wie sie sind, und die
  Galerie findet ihre Bildschirmfotos; bei GOG kommt zuerst die englische Spielfassung; Datum, Uhrzeit (nach der
  24-Stunden-Einstellung), Zahlen und Rabatte im Format der Sprache.
- **Trailer:** Ein Trailer, den niemand mehr abspielt, belegt nach seiner Laufzeit keinen Platz im Trailer-Dienst
  mehr; klappt das Ablegen im Spielordner nicht, wird der Rest nicht mehr umsonst geladen.
- **Startvideos:** Bei Netzfehlern bleibt die zuletzt geladene Liste stehen, und nach einem Aussetzer lädt sie beim
  Weiterblättern nach; eine volle Platte gibt einen klaren Satz.
- **Kleinigkeiten:** Eine Bilddatei mit kaputtem Namen legt die Galerie nicht mehr lahm; bremst Steam die Anfragen,
  steht das so da statt „nicht erreichbar“.
- **Ton auf jedem Fernseher:** Der PC5 Launcher erkennt den Ton-Ausgang des Fernsehers jetzt an dessen Namen aus dem
  Bildsignal, egal welcher Fernseher es ist. Bisher fand er ihn nur bei einem bestimmten Modell.

## 0.4.0 (2026-10-27)

- **Mediengalerie:** Alle Bildschirmfotos nach Spiel sortiert, Vollbild mit L1/R1, Löschen in den Papierkorb, „Als
  Hintergrund verwenden“. Unter Medien und als erste Kachel der Spielebibliothek.
- **Eigener Hintergrund:** Einstellungen › Home-Bildschirm › Hintergrund: Spielbilder, ein eigenes Bild oder eine Diashow
  aus Bildschirmfotos und dem Ordner „Hintergründe“.
- **Spielinfos, Neuigkeiten und Trailer:** In der Spielansicht stehen Kurzbeschreibung, Genre, Entwickler,
  Erscheinungsdatum, Metacritic, Steam-Bilder und die neuesten Meldungen. Ruht die Auswahl, läuft stumm der Trailer im
  Hintergrund; auf Wunsch leise Spielmusik (Einstellungen › Spiele).
- **Steam-Trophäen:** Errungenschaften aus Steam als Trophäen mit Bronze, Silber, Gold und Platin nach Seltenheit, je
  Spiel und als Übersicht, mit Mitteilung beim Freischalten. Ohne Anmeldung, aus Steams Dateien auf dem Rechner.
- **Nützliche Karten unter dem Spiel:** Trophäen, deine Bildschirmfotos, Neuigkeiten und „Über das Spiel“ statt Quelle
  und Ordner (die stehen jetzt unter „…“ › Informationen).
- **Widget-Leiste im Home:** Speicherplatz, Trophäen, Angebote der Wunschliste und zuletzt gespielt, einzeln
  abschaltbar.
- **Angebote der Wunschliste:** Mitteilung, wenn ein Spiel deiner Steam-Wunschliste reduziert ist.
- **Logo anpassen:** Größe und Lage des Spiellogos je Spiel, mit dem Controller.
- **Startvideos:** Statt der eigenen Startanimation ein Video aus der Community-Sammlung steamdeckrepo.com, auch vor dem
  Ruhemodus.
- **Trailer in voller Länge,** ohne die Oberfläche anzuhalten. Bei Spielen, die der PC5 Launcher selbst installiert
  hat, liegt der Trailer im Spielordner (und geht beim Deinstallieren mit), alle anderen werden nur gestreamt.
- **Trailer im Store** (Steam): stumm im Kopfbild, und vorne in „Bilder“ eine Kachel, die ihn groß mit Ton abspielt.
- **Eigener Bereich „Updates“** unter „Spiele“ in den Einstellungen: Systemsoftware, PC5 Launcher, Apps, Proton und Spiele
  auf einer Seite, mit dem Knopf direkt in der Zeile, „Alles aktualisieren“ und „Jetzt nach Updates suchen“.
- **Was ist neu:** Die Änderungen jeder Fassung (Einstellungen › Updates › Was ist neu). Bei einem verfügbaren Update
  siehst du dessen Neuerungen vor dem Aktualisieren, nach einem Update fragt das Home einmal nach.
- **Store:** „Beschreibung“ steht neben „Kaufen“, und ein geöffnetes Bild füllt den Bildschirm.
- **Kleinigkeiten:** Der Fokusring dehnt sich beim Spielstart aus; Hintergründe der Nachbarkacheln werden vorgeladen.
- **GOG-Installer, die unter Proton abbrechen** (z. B. Pizza Connection), packt der Launcher jetzt mit innoextract aus
  (kommt mit Lutris). Die Startdatei findet er auch, wenn GOG sie anders schreibt als die Datei heißt
  („dosbox.exe“ statt „DOSBox.exe“).

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
