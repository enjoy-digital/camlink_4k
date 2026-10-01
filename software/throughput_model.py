#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""GPIF throughput model (analytic) for a video mode + audio, per GPIF configuration.

During active lines the input rate is fixed (IT6802 timing); the GPIF must sustain it plus its
overheads (UVC headers, burst guard/head lead, audio phases with thread switch guards). On-chip
FIFOs (HDMI frame FIFO + CDCs) only absorb a small deficit over the active part of a frame (no
DRAM).

    throughput_model.py # 4K30 M420 table (PCLK, buffer size, audio batch, switch guard).
"""

import argparse

# Mode ---------------------------------------------------------------------------------------------

class Mode:
    def __init__(self, name, hactive, vactive, htotal, vtotal, fps, bpp):
        self.name    = name
        self.hactive = hactive
        self.vactive = vactive
        self.htotal  = htotal
        self.vtotal  = vtotal
        self.fps     = fps
        self.bpp     = bpp

    @property
    def active_words_per_ms(self):
        """32-bit words/ms during active lines (line rate x words per line)."""
        line_rate  = self.vtotal*self.fps      # Lines/s.
        words_line = self.hactive*self.bpp/8/4 # Words per line (M420: 1.5 lines of Y+UV/2 lines).
        return line_rate*words_line/1e3

    @property
    def average_mbs(self):
        """Average video rate (MB/s)."""
        return self.hactive*self.vactive*self.bpp/8*self.fps/1e6

    @property
    def active_ms(self):
        """Duration of the active lines of a frame (ms)."""
        return 1e3/self.fps*self.vactive/self.vtotal

MODES = {
    "2160p30-m420": Mode("3840x2160@30 M420", 3840, 2160, 4400, 2250, 30, 12),
    "1080p60-yuy2": Mode("1920x1080@60 YUY2", 1920, 1080, 2200, 1125, 60, 16),
}

# GPIF Model ---------------------------------------------------------------------------------------

def gpif_budget(mode, pclk=100.8e6, buf_bytes=16384, guard=32, head_lead=4, audio=True,
    audio_batch=1, switch_guard=1024, audio_lead=8, fifo_words=2048 + 1024 + 512):
    """GPIF cycles budget per ms during active lines (margin, USB rate, on-chip FIFOs check)."""
    cycles_ms   = pclk/1e3
    burst_words = buf_bytes//4
    video_words = mode.active_words_per_ms*burst_words/(burst_words - 3) # + UVC headers.
    bursts      = video_words/burst_words
    video_ovh   = bursts*(guard + head_lead + 4)                         # Guard, head lead, FSM.
    audio_cost  = 0
    if audio:
        phases = 1/audio_batch # Thread 1 phases per ms.
        # Switch in (idle >= switch_guard), lead, packets, short guards, switch back (idle >=
        # switch_guard).
        audio_cost = phases*(2*switch_guard + audio_lead + 2*guard) + 48 + 32
    need   = video_words + video_ovh + audio_cost
    margin = cycles_ms - need
    # Deficit over the active part of a frame vs on-chip FIFOs.
    deficit_frame = max(0, -margin)*mode.active_ms
    return {
        "need_cycles_ms": need,
        "margin_pct":     100*margin/cycles_ms,
        "usb_mbs_active": (video_words + (48 if audio else 0))*4/1e3, # Sustained USB rate needed.
        "fifo_ok":        deficit_frame <= fifo_words,
        "deficit_words":  deficit_frame,
    }

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="2160p30-m420", choices=MODES.keys())
    args = parser.parse_args()
    mode = MODES[args.mode]
    print(f"{mode.name}: {mode.average_mbs:.0f} MB/s average, "
          f"{mode.active_words_per_ms*4/1e3:.0f} MB/s during active lines")
    print(f"{'PCLK':>6} {'buf':>5} {'audio':>6} {'batch':>5} {'sw_guard':>8} | "
          f"{'margin':>7} {'USB MB/s':>8} {'FIFO':>5}")
    # Audio configurations: (audio, batch, switch guard).
    audio_configs = [
        (False, 1,    0),
        (True,  1, 1024),
        (True,  4, 1024),
        (True,  1,  256),
        (True,  4,  256),
    ]
    for pclk in (96e6, 100.8e6):
        for buf in (16384, 32768):
            for audio, batch, sg in audio_configs:
                r = gpif_budget(mode,
                    pclk         = pclk,
                    buf_bytes    = buf,
                    audio        = audio,
                    audio_batch  = batch,
                    switch_guard = sg,
                )
                print(f"{pclk/1e6:6.1f} {buf//1024:4d}K {('on' if audio else 'off'):>6} "
                      f"{batch:5d} {sg:8d} | "
                      f"{r['margin_pct']:6.2f}% {r['usb_mbs_active']:8.1f} "
                      f"{'ok' if r['fifo_ok'] else 'NO':>5}")

if __name__ == "__main__":
    main()
