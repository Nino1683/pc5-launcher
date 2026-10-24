#!/usr/bin/bash
# Erzeugt die Töne der Oberfläche mit ffmpeg: reine Synthese aus Sinustönen, nichts Fremdes.
#   app/web/sounds/make-sounds.sh [ausgabeordner]     (Vorgabe: dieser Ordner)
# Ergebnis: zwölf kurze WAV-Dateien (44,1 kHz, Mono, 16 Bit), Spitzenpegel etwa −17 bis −10 dBFS.
# Charakter nach docs/research, Bericht 07, Abschnitt 7: weich, kurz, leise, obertonarm.
# Das Skript spielt nichts ab. Die Namen stehen auch in js/core/sound.js (SOUND_NAMES).
set -eu
out="${1:-$(cd "$(dirname "$0")" && pwd)}"
mkdir -p "$out"
gen() { # name dauer ausdruck
  ffmpeg -hide_banner -loglevel error -nostdin -y -f lavfi -i "aevalsrc=$3:d=$2:s=44100" \
    -af "afade=t=in:d=0.004,afade=t=out:st=$(awk "BEGIN{print $2-0.012}"):d=0.012,volume=0.8" \
    -ac 1 -c:a pcm_s16le -map_metadata -1 -fflags +bitexact -flags:a +bitexact "$out/$1.wav"
}
# Fokus wandert: sehr kurzer, heller Tupfer
gen focus         0.07 "0.30*sin(2*PI*1760*t)*exp(-t*85)+0.10*sin(2*PI*2640*t)*exp(-t*120)"
# Rand erreicht / nicht möglich: dumpfer, tiefer Tupfer
gen edge          0.10 "0.45*sin(2*PI*150*t)*exp(-t*45)+0.15*sin(2*PI*300*t)*exp(-t*70)"
# Auswählen: zwei schnell steigende Töne (Quinte)
gen select        0.20 "0.36*sin(2*PI*880*t)*exp(-t*30)+0.36*sin(2*PI*1320*(t-0.055))*exp(-(t-0.055)*20)*gte(t\,0.055)"
# Zurück: zwei fallende Töne
gen back          0.18 "0.34*sin(2*PI*784*t)*exp(-t*32)+0.34*sin(2*PI*523*(t-0.05))*exp(-(t-0.05)*24)*gte(t\,0.05)"
# Reiter wechseln (L1/R1)
gen tab           0.10 "0.30*sin(2*PI*1175*t)*exp(-t*40)+0.08*sin(2*PI*2350*t)*exp(-t*60)"
# Schalter ein / aus
gen toggle-on     0.09 "0.32*sin(2*PI*1320*t)*exp(-t*45)"
gen toggle-off    0.09 "0.32*sin(2*PI*990*t)*exp(-t*45)"
# Overlay auf: weiches Aufgleiten 320 -> 960 Hz; Overlay zu: Abgleiten
gen overlay-open  0.24 "0.30*sin(2*PI*(320*t+1333*t*t))*sin(PI*t/0.24)"
gen overlay-close 0.20 "0.28*sin(2*PI*(960*t-1600*t*t))*sin(PI*t/0.20)"
# Mitteilung: sanfter Zweiklang
gen toast         0.60 "0.27*sin(2*PI*1047*t)*exp(-t*7)+0.23*sin(2*PI*1568*(t-0.09))*exp(-(t-0.09)*6)*gte(t\,0.09)"
# Spielstart: an- und abschwellender Akkord (A-Dur), 1,2 s
gen launch        1.20 "0.11*(sin(2*PI*220*t)+sin(2*PI*330*t)+sin(2*PI*440*t)+0.7*sin(2*PI*554*t))*sin(PI*t/1.2)"
# Fehlerdialog: zwei tiefe, gleich hohe Töne
gen error         0.30 "0.38*sin(2*PI*196*t)*exp(-t*18)+0.38*sin(2*PI*196*(t-0.13))*exp(-(t-0.13)*18)*gte(t\,0.13)"
# Startanimation „Polarlicht“ (E103): Luft, Fmaj9-Fläche, Glasklang, Schimmer, Fundament, Hall; 3,9 s
# Stereo, eigene Synthese in Python (nur Standardbibliothek, fester Zufallswert: immer dieselbe Datei).
# Nicht in SOUND_NAMES (die Prüfung verlangt dort kurze Töne): js/core/sound.js führt ihn unter LONG_SOUNDS.
python3 -I "$(cd "$(dirname "$0")" && pwd)/make-intro-sound.py" "$out/intro.wav" >/dev/null
ls -1 "$out"/*.wav | wc -l
