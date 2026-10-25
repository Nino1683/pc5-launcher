# Fremde Teile im PC5 Launcher / Third-party parts

Was im Paket liegt und nicht vom PC5 Launcher selbst stammt, mit Herkunft und Lizenz. Eigene Töne
(`app/web/sounds/make-sounds.sh`, `make-intro-sound.py`), Symbole (`app/web/icons.svg`) und der übrige Code sind
eigene Arbeit.

## Im Paket

| Teil | Ort im Paket | Herkunft | Lizenz |
|---|---|---|---|
| xterm.js 5.5.0 (`@xterm/xterm`) und `@xterm/addon-fit` 0.10.0 | `app/couch/terminal/page/vendor/` | npm-Verzeichnis, Prüfsummen in `HERKUNFT.txt` | MIT, Text in `LICENSE-xterm` |
| Noto Sans (variable Schrift, Version 2.015) | `app/web/fonts/NotoSans-VF.ttf` | The Noto Project Authors, Kopie aus dem Fedora-Paket google-noto-sans-vf-fonts | SIL Open Font License 1.1 (OFL), Text in `app/web/fonts/OFL.txt` |
| Deutsche Wortliste für die Bildschirmtastatur | `app/web/data/words-de.txt` | FrequencyWords von Hermit Dave (github.com/hermitdave/FrequencyWords, `de_50k`), aus OpenSubtitles 2018; gefiltert (nur Buchstaben, 2–24 Zeichen) | CC BY-SA 4.0 (Namensnennung, Weitergabe unter gleichen Bedingungen); die gefilterte Liste steht unter derselben Lizenz |
| Englische Wortliste für die Bildschirmtastatur | `app/web/data/words-en.txt` | FrequencyWords von Hermit Dave (github.com/hermitdave/FrequencyWords, `en_50k`), aus OpenSubtitles 2018; gefiltert (nur Buchstaben, 2–24 Zeichen) | CC BY-SA 4.0 (Namensnennung, Weitergabe unter gleichen Bedingungen); die gefilterte Liste steht unter derselben Lizenz |

## Zur Laufzeit nachgeladen (nicht im Paket)

Nur, wenn du es in der Konsole anstößt. Jedes davon behält seine eigene Lizenz.

| Teil | Wofür | Lizenz |
|---|---|---|
| legendary | Epic-Spiele laden und starten | GPL-3.0 |
| umu-launcher (Zipapp) | Windows-Spiele mit Proton ohne Steam starten | GPL-3.0 |
| Proton-CachyOS, GE-Proton | Windows-Spiele | jeweilige Lizenzen der Projekte (überwiegend BSD/LGPL/GPL-Bestandteile) |
| Microsoft XAudio2 | Ton für einzelne Windows-Spiele | Lizenz von Microsoft |
| Emulatoren und Apps von Flathub | Konsolen, Medien | jeweilige Lizenz der App |

BIOS-Dateien, Schlüssel, Firmware und Spiele lädt der PC5 Launcher nie.

## English summary

xterm.js 5.5.0 and addon-fit 0.10.0 (MIT, `LICENSE-xterm`); Noto Sans variable font (SIL OFL 1.1,
`app/web/fonts/OFL.txt`); German and English word lists `words-de.txt`, `words-en.txt` derived from Hermit Dave's FrequencyWords (OpenSubtitles
2018), CC BY-SA 4.0, the filtered list under the same license. Downloaded later on request only: legendary and
umu-launcher (GPL-3.0), Proton variants, Microsoft XAudio2 (Microsoft's license), Flathub apps (their licenses).
