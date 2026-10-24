"""Eingabe: Gamepads lesen (evdev, Griff nur über einer laufenden App, E56), PS-Taste, Maus-Modus
und Tippen per XTEST.

    ds_codes      Codes, Bereiche, Erkennung der Geräterolle
    padlogic      Totzone, Wiederholung, PS-Taste, Zustand, Tasten für die Oberfläche (reine Logik)
    padreader     PadManager und das echte Backend (python-evdev, pyudev)
    fakepad       Attrappen-Backend für Tests und Prüfmodus
    padmouse      das Pad als Maus/Tastatur, Senke XTEST
    xtest         Text und Tasten per XTEST tippen
    dualsense_fx  Vibration, Lichtleiste, Akku
    service       InputService (Vertrag aus interfaces.py), Einbindung in GLib

Bewusst keine Importe hier: `import couch.input` soll weder GLib noch X noch evdev laden.
"""
