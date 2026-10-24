"""Browser der Konsole (Bauplan E59): ein eigenes WebKit-Fenster für den Fernseher statt Chrome im
Desktop-Modus.

Bedienung wie im Rest der Konsole über padmouse (Route "mouse"): linker Stick Zeiger, Kreuz/R2
Klick, rechter Stick Rollen, L1/R1 zurück/vor (Maustasten 8/9), Dreieck Bildschirmtastatur,
Kreis Escape. Oben eine große Leiste: zurück, vor, neu laden, Startseite, Adresse/Suche, Zoom.

Downloads (E60): http(s)-Downloads übergibt der Browser als Auftrag an das Home, das sie selbst
lädt (couch/fetcher.py, Stand in <state>/downloads.json). Was WebKit selbst laden muss (blob:,
data:, POST), führt der Browser in <state>/browser-downloads.json (E99); das Home zeigt beides im
Control Center unter „Downloads“ (wiring_install._downloads, sys.state.downloads).

Aufbau:
    address.py     Eingabe der Adressleiste -> Adresse (pur)
    adblock.py     Regeln des Werbeblockers für WebKit, Erkennen von Werbe-Adressen (pur, E61)
    bookmarks.py   Lesezeichen des Google-Kontos aus Chromes Profil -> Seite (pur, nur lesen, E89)
    popups.py      welche Fenster einer Seite gezeigt werden (pur, E89)
    prefs.py       Optionen (Favoriten, Startseite, Suche, Werbeblocker, Zoom), Startseite (pur, E61)
    downloads.py   Dateinamen, Aufträge an das Home, Liste der eigenen Downloads (pur, ohne GTK)
    window.py      GTK-4-Fenster mit WebKit (nur im echten Lauf)
"""
