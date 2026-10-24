"""Store des PC5 Launchers (Bauplan E68): Spiele finden, kaufen und installieren, ohne dass man
den Steam-Client oder eine andere Store-App sieht.

    steam.py          Katalog, Suche und Einzelheiten aus dem Steam-Shop (nur lesen, Zwischenspeicher)
    steam_account.py  Was dir gehört und was auf der Wunschliste steht: über die Anmeldung im
                      Browser des PC5 Launchers (dessen Cookies), nie über ein eigenes Kennwort
    steam_install.py  Installieren über Steam im Hintergrund: Eintrag (appmanifest) schreiben,
                      solange Steam nicht läuft; Steam lädt beim nächsten unsichtbaren Start
    gog*.py, epic*.py, battlenet*.py, ubisoft*.py, rockstar*.py, launchers.py
                      die anderen Shops nach demselben Muster (Lesen, Konto, Installieren)
    itch.py           itch.io (E100): Listen aus den RSS-Feeds der Stöberseiten (die Webseiten liegen
                      hinter einer Cloudflare-Prüfung), mit Drossel und Zwischenspeicher
    itch_account.py   itch.io-Konto über OAuth mit PKCE (eigene App-Kennung, nie ein Kennwort):
                      eigene Spiele, Dateien, Download-Adresse, Suche über die API
    itch_install.py   itch.io: Datei wählen, Installer erkennen, laden (wiring_itch.py installiert)

Gekauft wird auf der Shop-Seite im Browser des PC5 Launchers. Alles hier blockiert (Netz,
Platte) und gehört in Arbeits-Threads (wiring_store.py, wiring_itch.py).
"""
