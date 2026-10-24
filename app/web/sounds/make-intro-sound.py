#!/usr/bin/python3
"""Startklang „Polarlicht“ (E103, Startanimation components/intro.js): reine Synthese, nur Python-Standardbibliothek
(math, random mit festem Startwert, struct, wave), kein fremdes Material. Gehört ins Paket (eigene Synthese).

  python3 -I make-intro-sound.py <ausgabe.wav>        (make-sounds.sh ruft es für sounds/intro.wav auf)

Ergebnis: 3,9 s, 44,1 kHz, Stereo, 16 Bit, Spitze etwa −10 dBFS, etwa −22 LUFS (wie der Klang von E92).
Gleiche Eingabe = gleiche Datei (fester Zufallswert; tests/unit/test_intro_sound.py prüft das).
Zeitachse wie die Animation (ms ab Tonbeginn):
     0  Luft: gefiltertes Rauschen, Filter öffnet sich mit der Lichtnaht und dem Vorhang (bis ~1,6 s)
   150  Flächenklang Fmaj9 (F2 C3 A3 E4 G4), je Stimme zwei leicht verstimmte Oszillatoren links/rechts,
        Tiefpass öffnet bis 1,6 s und schließt sich langsam wieder
  1050  Glasklang: eine Glocke aus den Teiltönen eines freien Stabs (1 : 2,756 : 5,404 : 8,933), wenn der Schriftzug landet
  1450  Schimmer: hohe Teiltöne mit leichtem Aufwärtsgleiten, wandert von links nach rechts mit dem Glanzlicht
  1500  Fundament: weiches F1/F2 unter „LAUNCHER“
  danach Ausklang mit Hall bis 3,9 s
"""
import math
import random
import struct
import sys
import wave

SR = 44100
DUR = 3.9
N = int(SR * DUR)
TAU = 2 * math.pi
rng = random.Random(20261024)


def smooth(x):            # 0…1 weich (smoothstep)
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def env_ad(t, start, attack, decay):
    """Einsatz bei start, Anstieg attack (s), danach exponentieller Abfall mit Zeitkonstante decay."""
    if t < start:
        return 0.0
    u = t - start
    a = smooth(u / attack) if attack > 0 else 1.0
    return a * math.exp(-max(0.0, u - attack) / decay)


L = [0.0] * N
R = [0.0] * N
sendL = [0.0] * N          # Anteil für den Hall
sendR = [0.0] * N

# ------------------------------------------------------------------ 1. Luft (Rauschen durch wanderndes Bandpassfilter)
for ch, out in ((0, L), (1, R)):
    low = band = 0.0
    for n in range(N):
        t = n / SR
        if t > 2.2:
            break
        fc = 300 + 3200 * smooth(t / 1.5)                 # öffnet mit dem Vorhang
        f = 2 * math.sin(math.pi * fc / SR)
        q = 0.55
        x = rng.uniform(-1, 1)
        low += f * band
        high = x - low - q * band
        band += f * high
        g = 0.055 * smooth(t / 0.5) * (1 - smooth((t - 1.0) / 1.2))
        out[n] += band * g
        (sendL if ch == 0 else sendR)[n] += band * g * 0.4

# ------------------------------------------------------------------ 2. Flächenklang Fmaj9 (additiv, sägezahnähnlich, Tiefpass über die Teiltöne)
VOICES = [87.31, 130.81, 220.00, 329.63, 392.00]
LEVEL = [0.9, 0.75, 0.6, 0.45, 0.38]
BLOCK = 256


def cutoff(t):
    if t < 1.6:
        return 380 + 2000 * smooth((t - 0.15) / 1.45)
    return 2380 - 1100 * smooth((t - 1.6) / 2.3)


def pad_gain(t):
    a = smooth((t - 0.15) / 0.9)
    rel = math.exp(-max(0.0, t - 2.3) / 0.75)
    return a * rel


for vi, f0 in enumerate(VOICES):
    for side, cents in ((0, -4.0), (1, 4.0)):
        f = f0 * 2 ** (cents / 1200)
        parts = [k for k in range(1, 17) if k * f < 6000]
        # Oszillator als Drehung (Rekursion): je Teilton zwei Zustände
        st = []
        for k in parts:
            w = TAU * k * f / SR
            ph = rng.uniform(0, TAU)
            st.append([math.sin(ph), math.sin(ph - w), 2 * math.cos(w), k])
        out = L if side == 0 else R
        for b0 in range(0, N, BLOCK):
            t = b0 / SR
            fc = cutoff(t)
            g = pad_gain(t) * LEVEL[vi] * 0.036
            if g < 1e-5:
                for s in st:                                   # Zustand weiterdrehen, stumm
                    for _ in range(min(BLOCK, N - b0)):
                        s[0], s[1] = s[2] * s[0] - s[1], s[0]
                continue
            amps = [g / s[3] / math.sqrt(1 + (s[3] * f / fc) ** 4) for s in st]
            for n in range(b0, min(N, b0 + BLOCK)):
                acc = 0.0
                for s, a in zip(st, amps):
                    y = s[2] * s[0] - s[1]
                    s[1] = s[0]
                    s[0] = y
                    acc += a * y
                out[n] += acc
                (sendL if side == 0 else sendR)[n] += acc * 0.25

# ------------------------------------------------------------------ 3. Glasklang beim Landen des Schriftzugs (freier Stab, nicht harmonisch)
BAR = [(1.0, 1.0, 1.7), (2.756, 0.42, 0.9), (5.404, 0.22, 0.45), (8.933, 0.10, 0.25)]


def bell(t0, f0, level, pan_l, pan_r):
    for ratio, amp, dec in BAR:
        f = f0 * ratio
        if f > 16000:
            continue
        w = TAU * f / SR
        for n in range(int(t0 * SR), N):
            u = n / SR - t0
            e = smooth(u / 0.004) * math.exp(-u / dec)
            if e < 1e-5 and u > 0.01:
                break
            v = level * amp * e * math.sin(w * (n - t0 * SR))
            L[n] += v * pan_l
            R[n] += v * pan_r
            sendL[n] += v * pan_l * 0.9
            sendR[n] += v * pan_r * 0.9


bell(1.05, 1318.51, 0.060, 0.9, 1.0)       # E6, Quinte zum Akkord
bell(1.12, 1975.53, 0.022, 1.0, 0.75)      # B6, leise dahinter: wirkt wie ein Glanz, nicht wie eine Melodie

# ------------------------------------------------------------------ 4. Schimmer mit dem Glanzlicht (links -> rechts)
for f0, amp, delay in ((2637.0, 0.020, 0.00), (3520.0, 0.014, 0.06), (4186.0, 0.010, 0.12), (5274.0, 0.007, 0.18)):
    ph = 0.0
    for n in range(int((1.45 + delay) * SR), int(2.9 * SR)):
        u = n / SR - 1.45 - delay
        f = f0 * (1 + 0.012 * smooth(u / 0.9))           # gleitet ein wenig nach oben
        ph += TAU * f / SR
        e = smooth(u / 0.25) * math.exp(-u / 0.45)
        trem = 0.75 + 0.25 * math.sin(TAU * 7.0 * u)        # leichtes Flirren
        pan = smooth(u / 0.9)                               # 0 links … 1 rechts
        v = amp * e * trem * math.sin(ph)
        L[n] += v * math.cos(pan * math.pi / 2)
        R[n] += v * math.sin(pan * math.pi / 2)
        sendL[n] += v * 0.8
        sendR[n] += v * 0.8

# ------------------------------------------------------------------ 5. Fundament unter „LAUNCHER“
for n in range(int(1.5 * SR), N):
    u = n / SR - 1.5
    e = smooth(u / 0.25) * math.exp(-u / 1.1)
    v = e * (0.050 * math.sin(TAU * 43.65 * u) + 0.030 * math.sin(TAU * 87.31 * u))
    L[n] += v
    R[n] += v

# ------------------------------------------------------------------ 6. Hall (Freeverb-artig: vier Kammfilter, zwei Allpässe je Seite)


def reverb(x, combs, aps, fb=0.80, damp=0.30):
    y = [0.0] * len(x)
    for d in combs:
        buf = [0.0] * d
        i = 0
        lp = 0.0
        for n in range(len(x)):
            o = buf[i]
            lp = o * (1 - damp) + lp * damp
            buf[i] = x[n] + lp * fb
            i = (i + 1) % d
            y[n] += o
    for d in aps:
        buf = [0.0] * d
        i = 0
        for n in range(len(y)):
            b = buf[i]
            o = -y[n] + b
            buf[i] = y[n] + b * 0.5
            i = (i + 1) % d
            y[n] = o
    return y


wetL = reverb(sendL, [1557, 1617, 1491, 1422], [556, 441])
wetR = reverb(sendR, [1580, 1640, 1514, 1445], [579, 464])

# ------------------------------------------------------------------ Summe, weiche Begrenzung, Ausblenden, 16 Bit
mix = []
for n in range(N):
    t = n / SR
    fade = 1.0 - smooth((t - (DUR - 0.45)) / 0.45)
    a = (L[n] + 0.22 * wetL[n]) * fade
    b = (R[n] + 0.22 * wetR[n]) * fade
    mix.append((a, b))
peak = max(max(abs(a), abs(b)) for a, b in mix) or 1.0
target = 10 ** (-12.5 / 20)                     # nach der Begrenzung Spitze etwa −10 dBFS, rund −21 LUFS wie bisher
k = target / peak
frames = bytearray()
for a, b in mix:
    for v in (a, b):
        v = math.tanh(v * k * 1.05) / math.tanh(1.05)   # schont Spitzen, ändert den Klang kaum
        v += (rng.random() - rng.random()) / 32768      # TPDF-Dither
        frames += struct.pack("<h", max(-32767, min(32767, int(round(v * 32767)))))
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(bytes(frames))
print(sys.argv[1], "%.2f s" % DUR)
