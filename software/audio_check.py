#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CamLink 4K UAC audio check: capture with arecord (in memory) and analyse.

- test : FPGA test counter source (L = n, R = ~n): checks continuity (drops/duplicates).
- hdmi : HDMI (I2S) source: levels, dominant frequency per channel, silence ratio.
"""

import sys
import time
import argparse
import subprocess

import numpy as np

# Capture ------------------------------------------------------------------------------------------

def find_card(name="CamLink 4K"):
    for line in open("/proc/asound/cards"):
        if name in line and "[" in line:
            return line.split("[")[0].strip()
    return None

def capture(card, seconds, rate=48000):
    cmd = ["arecord", "-q", "-D", f"hw:{card},0", "-f", "S16_LE", "-c", "2", "-r", str(rate),
        "-d", str(seconds), "-t", "raw"]
    # The desktop sound server briefly opens a new card after enumeration: retry while busy.
    for _ in range(20):
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=seconds + 10)
        if r.returncode == 0:
            return np.frombuffer(r.stdout, dtype="<i2").reshape(-1, 2)
        if b"busy" not in r.stderr:
            raise RuntimeError(r.stderr.decode().strip())
        time.sleep(0.5)
    raise RuntimeError("Audio device busy.")

# Checks -------------------------------------------------------------------------------------------

def check_test(samples):
    left  = samples[:, 0].astype(np.uint16)
    right = samples[:, 1].astype(np.uint16)
    inv   = np.count_nonzero((left ^ right) != 0xffff)
    diff  = np.diff(left.astype(np.int64)) & 0xffff
    gaps  = np.count_nonzero(diff != 1)
    return {
        "samples":         len(samples),
        "not_inverted":    int(inv),
        "discontinuities": int(gaps),
        "first":           int(left[0]) if len(left) else None,
    }

def check_hdmi(samples, rate=48000):
    res = {"samples": len(samples)}
    for ch, name in ((0, "left"), (1, "right")):
        x    = samples[:, ch].astype(np.float64)/32768
        rms  = np.sqrt(np.mean(x**2)) if len(x) else 0
        spec = np.abs(np.fft.rfft(x*np.hanning(len(x)))) if len(x) else np.zeros(1)
        freq = np.argmax(spec[1:]) + 1 if len(spec) > 1 else 0
        res[name] = {
            "rms_dbfs":  round(20*np.log10(rms), 1) if rms > 0 else None,
            "peak":      int(np.max(np.abs(samples[:, ch]))) if len(x) else 0,
            "freq_hz":   round(freq*rate/len(x), 1) if len(x) else None,
            "silent_%":  round(100*np.mean(samples[:, ch] == 0), 1) if len(x) else None,
        }
    return res

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["test", "hdmi"])
    parser.add_argument("--seconds", type=int, default=3)
    parser.add_argument("--card", default=None, help="ALSA card (default: find CamLink 4K).")
    args = parser.parse_args()

    card = args.card or find_card()
    if card is None:
        print("CamLink 4K sound card not found.")
        sys.exit(1)
    samples = capture(card, args.seconds)
    result  = check_test(samples) if args.mode == "test" else check_hdmi(samples)
    print(result)

if __name__ == "__main__":
    main()
