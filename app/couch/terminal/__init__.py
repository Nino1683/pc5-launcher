"""Terminal der Konsole für Claude Code (Bauplan E72): Kachel „Claude Code“ unter Medien.

Wunsch 2026-10-19: „Ich möchte unter Medien eine neue Kachel Claude Code, hier soll das Terminal,
in dem du gerade läufst, ausführbar sein. Damit ich nicht immer in den Desktop muss … Angepasst an
Controller. Befehle mit dem Controller ausführen wie /resume, Update von Claude Code ausführen usw.“

Ein eigenes WebKit-Fenster mit xterm.js (page/vendor, MIT) statt Konsole: Startauswahl, Befehlsmenü
und Schreibfeld sind Teil der Seite und mit dem Pad bedienbar. Das Pad kommt als feste Tasten
(padmouse, profile="term"): Steuerkreuz Pfeile, Kreuz Enter, Kreis Escape, Quadrat Rücktaste,
R1 Tab, L1 F10 (Umschalt+Tab), L2/R2 F7/F8 (Blättern), Options F12 (Befehle), Dreieck F9
(Schreibfeld) und dazu die Bildschirmtastatur der Konsole, rechter Stick rollt.

Aufbau:
    session.py   Befehle der Startauswahl, Umgebung, Pseudo-Terminal (pur, getestet)
    window.py    GTK-4-Fenster mit WebKit (nur im echten Lauf)
    page/        term.html, term.js, term.css, vendor/ (xterm.js 5.5.0, addon-fit 0.10.0)
"""
