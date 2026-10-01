#!/usr/bin/env python3
#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Synthesize the CamLink 4K video soundtrack: 120 BPM, D minor, 66 s (the film's length).

Section map (bars; 1 bar = 2 s), the video's chapter cuts use the same grid:
  0-2 product | 2-5 inside | 5-7 vendor code out (riser, the bitstream swap hits bar 6)
  7-10 one Python script (drop) | 10-13 architecture (half-time, blips) | 13-17 data path (drop, lead)
  17-20 race (half-time) | 20-23 latency (lead) | 23-27 open source (lead) | 27-29 credits (breakdown)
  29-33 end card (final hit on the title, bed)

Instruments and mix follow the TriXium soundtrack generator (enjoy-digital/trixium).

Usage: python3 music.py music.wav
"""

import sys
import wave

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi, lfilter

SR    = 44100
BPM   = 120
BEAT  = 60 / BPM
BAR   = 4 * BEAT
NBARS = 33
N     = int(NBARS * BAR * SR)
rng   = np.random.default_rng(11)

def buf(): return np.zeros(N)
def t_(dur): return np.arange(int(dur * SR)) / SR
def add(dst, x, at):
    i = int(round(at * SR))
    if i >= N: return
    x = x[: N - i]
    dst[i:i + len(x)] += x
def mtof(m): return 440.0 * 2 ** ((m - 69) / 12)

def section(b):
    for end, name in [(2, "product"), (5, "inside"), (7, "scan"), (10, "script"), (13, "arch"),
                      (17, "data"), (20, "arch"), (23, "latency"), (27, "litex"), (29, "credits")]:
        if b < end: return name
    return "end"

# i - VI - III - VII in D minor.
CHORDS = [[62, 65, 69], [58, 62, 65], [53, 57, 60], [60, 64, 67]]
ROOTS  = [50, 46, 41, 48]

# Instruments ---------------------------------------------------------------------------------------

def kick():
    t  = t_(0.42)
    f  = 45 + 110 * np.exp(-t * 28)
    x  = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5)
    x[:60] += rng.standard_normal(60) * 0.25 * np.linspace(1, 0, 60)
    return x * 0.95

def snare():
    t = t_(0.25)
    n = lfilter([1, -1], [1], rng.standard_normal(len(t))) * 0.5
    return n * np.exp(-t * 16) * 0.55 + np.sin(2 * np.pi * 185 * t) * np.exp(-t * 25) * 0.4

def hat(open_=False):
    t = t_(0.18 if open_ else 0.05)
    n = sosfilt(butter(4, 7000, "hp", fs=SR, output="sos"), rng.standard_normal(len(t)))
    return n * np.exp(-t * (14 if open_ else 70)) * 0.22

def saw(freq, dur, detune=(0.0,)):
    t = t_(dur)
    x = np.zeros(len(t))
    for d in detune:
        x += 2 * ((t * freq * 2 ** (d / 1200) + rng.random()) % 1.0) - 1
    return x / len(detune)

def pulse(freq, dur, width=0.5):
    return np.where((t_(dur) * freq) % 1.0 < width, 1.0, -1.0)

def env(n, a, d, s, r, dur):
    """ADSR in seconds, note held for dur seconds, n samples."""
    t = np.arange(n) / SR
    e = np.where(t < a, t / max(a, 1e-4), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-4)))
    rel = t > dur
    e[rel] = e[rel] * np.exp(-(t[rel] - dur) / max(r, 1e-4))
    return e

def lp_sweep(x, cutoff, block=512, order=2):
    """Time-varying lowpass, cutoff: time (s) -> Hz."""
    y, zi = np.zeros_like(x), None
    for i in range(0, len(x), block):
        sos = butter(order, float(np.clip(cutoff(i / SR), 40, SR * 0.45)), "lp", fs=SR, output="sos")
        if zi is None: zi = sosfilt_zi(sos) * 0
        y[i:i + block], zi = sosfilt(sos, x[i:i + block], zi=zi)
    return y

def comb(x, d, fb):
    y = x.copy()
    for i in range(d, len(y), d):
        e = min(len(y), i + d)
        y[i:e] += fb * y[i - d:e - d]
    return y

def delay(x, sec, fb, mix):
    d = int(sec * SR)
    return x + comb(np.concatenate([np.zeros(d), x[:-d]]), d, fb) * mix

def reverb(x, mix=0.25):
    out = np.zeros_like(x)
    for ms, g in [(29.7, .78), (37.1, .77), (41.1, .76), (43.7, .75)]:
        out += comb(x, int(ms * SR / 1000), g)
    return x + sosfilt(butter(1, 5000, "lp", fs=SR, output="sos"), out) / 4 * mix

def riser(dur):
    tt, n = t_(dur), rng.standard_normal(int(dur * SR))
    y, zi, blk = np.zeros_like(n), None, 1024
    for i in range(0, len(n), blk):
        fc  = 300 * (40 ** (i / len(n)))
        sos = butter(2, [fc, min(fc * 2.5, SR * .45)], "bp", fs=SR, output="sos")
        if zi is None: zi = sosfilt_zi(sos) * 0
        y[i:i + blk], zi = sosfilt(sos, n[i:i + blk], zi=zi)
    return y * (tt / dur) ** 2 * 0.5

def impact():
    tt = t_(2.5)
    f  = 30 + 60 * np.exp(-tt * 6)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 1.6) * 0.7

def blip(m, dur=0.12):
    """Short sine blip (data/UI accents)."""
    tt = t_(dur)
    return np.sin(2 * np.pi * mtof(m) * tt) * np.exp(-tt * 30) * 0.25

# Tracks --------------------------------------------------------------------------------------------

drums, bass, pad, arp, lead, fx = buf(), buf(), buf(), buf(), buf(), buf()
K, S, Hc, Ho = kick(), snare(), hat(), hat(True)
kick_times = []

for b in range(NBARS):
    sec  = section(b)
    t0   = b * BAR
    ch   = CHORDS[b % 4]
    root = ROOTS[b % 4]
    full = sec in ("script", "data", "latency", "litex")

    # Drums.
    if sec in ("inside", "scan") or full:
        for q in range(4):
            add(drums, K, t0 + q * BEAT); kick_times.append(t0 + q * BEAT)
    if sec == "arch":
        add(drums, K, t0); kick_times.append(t0)
        if b % 2: add(drums, K, t0 + 2.5 * BEAT); kick_times.append(t0 + 2.5 * BEAT)
        add(drums, S * 0.9, t0 + 2 * BEAT)
    if full:
        add(drums, S, t0 + BEAT); add(drums, S, t0 + 3 * BEAT)
    if sec in ("inside", "scan", "arch") or full:
        for e in range(8):
            h = Ho if (e % 2 and full) else Hc
            add(drums, h * (0.6 if e % 2 == 0 else 1.0), t0 + e * BEAT / 2)
    if b == 6:       # snare roll into the drop (bar 7)
        for i in range(16):
            add(drums, S * (0.2 + 0.6 * i / 16), t0 + i * BAR / 16)
    if b in (12, 19):  # rolls out of the architecture and the race
        for i in range(8):
            add(drums, S * (0.25 + 0.5 * i / 8), t0 + 2 * BEAT + i * BEAT / 4)
    if b in (2, 7, 13, 20, 23, 29):
        tt = t_(2.0)
        add(fx, sosfilt(butter(2, 3500, "hp", fs=SR, output="sos"), rng.standard_normal(len(tt))) * np.exp(-tt * 2.2) * 0.22, t0)

    # Bass: 8ths, octave jumps on the offbeats.
    if sec in ("inside", "scan", "arch") or full:
        for e in range(8):
            if sec == "arch" and e % 2: continue
            dur = BEAT / 2 * 0.9
            x   = saw(mtof(root - 12 + (12 if e % 2 else 0)), dur + 0.05, detune=(-6, 6))
            add(bass, x * env(len(x), 0.003, 0.12, 0.6, 0.03, dur) * 0.5, t0 + e * BEAT / 2)
    else:
        x = np.sin(2 * np.pi * mtof(root - 12) * t_(BAR)) * env(int(BAR * SR), 0.05, 1.0, 0.7, 0.3, BAR * 0.9)
        add(bass, x * 0.35, t0)

    # Pad: whole-bar detuned saw chords.
    for m in ch + [ch[0] + 12]:
        x = saw(mtof(m), BAR + 0.6, detune=(-11, 0, 9))
        add(pad, x * env(len(x), 0.25, 1.5, 0.75, 0.5, BAR) * 0.13, t0)

    # Arp: 16ths over the chord tones.
    tones = ch + [c + 12 for c in ch]
    patt  = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3, 5, 4, 3, 1]
    for s16 in range(16):
        d = BEAT / 4
        x = pulse(mtof(tones[patt[s16] % len(tones)] + 12), d * 1.4, 0.5 if sec == "arch" else 0.3)
        x *= env(len(x), 0.002, 0.07, 0.0, 0.02, d)
        add(arp, x * (0.075 if full else 0.10), t0 + s16 * d)

# Lead melody in the drops (4-bar phrase).
MEL = [(0, 74, 1.5), (1.5, 72, 0.5), (2, 70, 1), (3, 72, 1), (4, 70, 1.5), (5.5, 69, 0.5), (6, 67, 2),
       (8, 65, 1.5), (9.5, 67, 0.5), (10, 69, 1), (11, 70, 1), (12, 72, 2), (14, 69, 1), (15, 65, 1)]
for b0, until in [(13, 17), (20, 23), (23, 27)]:
    for o, m, d in MEL:
        at = b0 * BAR + o * BEAT
        if at >= until * BAR: continue
        dur = d * BEAT
        tt  = t_(dur + 0.3)
        f   = mtof(m + (12 if b0 >= 20 and o >= 8 else 0)) * (1 + 0.004 * np.sin(2 * np.pi * 5.5 * tt) * np.clip(tt - 0.2, 0, 1))
        ph  = np.cumsum(f) / SR
        x   = (2 * (ph % 1) - 1) * 0.6 + (2 * ((ph * 1.005) % 1) - 1) * 0.4
        add(lead, x * env(len(x), 0.01, 0.3, 0.7, 0.15, dur) * 0.12, at)

# Bitstream swap (bar 6): bright chord stab.
for m in [62, 69, 74, 77]:
    x = saw(mtof(m), 1.5, detune=(-10, 10)) * env(int(1.5 * SR), 0.002, 0.4, 0.0, 0.3, 0.3)
    add(fx, x * 0.07, 6 * BAR)

# Architecture: data blips on the beats while the diagram draws (video: items every 0.5 s from 20.5 s).
for i in range(9):
    add(fx, blip(CHORDS[(10 + i // 4) % 4][i % 3] + 24), 10 * BAR + BEAT + i * BEAT)

# Final hit on the end card title (bar 29).
add(fx, np.sin(2 * np.pi * 36.7 * t_(3.0)) * np.exp(-t_(3.0) * 1.5) * 0.6, 29 * BAR)
for m in [50, 57, 62, 65, 69]:
    x = saw(mtof(m), 4.0, detune=(-12, 0, 12)) * env(int(4 * SR), 0.005, 1.2, 0.3, 1.0, 1.5)
    add(fx, x * 0.09, 29 * BAR)

# Risers and sub impacts on the cuts.
for at, d in [(4, 1.0), (14, 2.0), (20, 1.0), (26, 1.0), (34, 1.0), (40, 2.0), (46, 1.0), (54, 1.0), (58, 2.0)]:
    add(fx, riser(d), at - d)
    add(fx, impact(), at)

# Mix -----------------------------------------------------------------------------------------------

def cut_arp(t):
    b = t / BAR
    s = section(int(min(b, NBARS - 1)))
    if s == "product": return 500 + 1500 * (b / 2) ** 2
    if s == "scan":    return 1500 + 3500 * ((b - 5) / 2) ** 2
    if s == "arch":    return 1400
    if s == "credits": return 900 + 2500 * ((b - 27) / 2) ** 2
    if s == "end":     return 2500
    return 5200

def cut_pad(t):
    b = t / BAR
    s = section(int(min(b, NBARS - 1)))
    if s == "product": return 400 + 1400 * (b / 2)
    if s in ("script", "data", "latency", "litex"): return 3200
    if s == "arch": return 1000
    return 2200

arp  = lp_sweep(arp, cut_arp)
pad  = lp_sweep(pad, cut_pad)
bass = lp_sweep(bass, lambda t: 900 if section(int(min(t / BAR, NBARS - 1))) in ("script", "data", "latency", "litex") else 600, order=4)

# Sidechain from the kicks.
sc = np.ones(N)
for kt in kick_times:
    i, n = int(kt * SR), int(0.28 * SR)
    e    = min(N, i + n)
    sc[i:e] = np.minimum(sc[i:e], (1 - 0.7 * np.exp(-np.arange(n) / SR * 14))[: e - i])

arp   = delay(arp, BEAT * 0.75, 0.35, 0.35)
lead  = delay(lead, BEAT * 0.75, 0.3, 0.3)
music = drums * 0.9 + bass * sc * 0.9 + reverb(pad * sc, 0.5) + reverb(arp * sc, 0.3) + reverb(lead, 0.35) + reverb(fx, 0.4)

# Fade the last 3 s, normalize, soft clip.
fade = int(3.0 * SR)
music[-fade:] *= np.linspace(1, 0, fade) ** 1.5
music  = sosfilt(butter(2, 25, "hp", fs=SR, output="sos"), music)
music /= np.max(np.abs(music)) + 1e-9
music  = np.tanh(music * 1.6) / np.tanh(1.6) * 0.79  # -2 dBFS: headroom for AAC.

st  = np.stack([music, np.concatenate([np.zeros(485), music[:-485]])], 1)  # Mono-compatible stereo.
out = sys.argv[1] if len(sys.argv) > 1 else "music.wav"
with wave.open(out, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((st * 32767).astype("<i2").tobytes())
print(out, N / SR, "s")
