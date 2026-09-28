#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Video: YUY2 test pattern generator.

Generates frames of `hres` x `vres` YUY2 pixels (32-bit words = 2 pixels: Y0 U Y1 V, little
endian) at a rate set by `frame_period` (in sys clock cycles). Words are produced as fast as the
downstream accepts them; a frame starts on each period tick (skipped if the previous one is still
being sent).

Pattern: 8 colour bars (BT.601 limited range), a moving white bar and a bottom luma ramp, plus the
frame number encoded in the first 32 words of the first line (for host-side checks).
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# Colour Bars (Y, U, V) ----------------------------------------------------------------------------

COLOR_BARS = [
    (235, 128, 128), # White.
    (210,  16, 146), # Yellow.
    (170, 166,  16), # Cyan.
    (145,  54,  34), # Green.
    (106, 202, 222), # Magenta.
    ( 81,  90, 240), # Red.
    ( 41, 240, 110), # Blue.
    ( 16, 128, 128), # Black.
]

def yuy2_word(y0, u, y1, v):
    return (v << 24) | (y1 << 16) | (u << 8) | y0

# Video Pattern Generator --------------------------------------------------------------------------

class VideoPatternGenerator(LiteXModule):
    def __init__(self, sys_clk_freq):
        self.source = source = stream.Endpoint([("data", 32)])

        self._enable       = CSRStorage(description="Enable pattern generator.")
        self._hwords       = CSRStorage(16, reset=1920//2, description="Words per line (hres/2).")
        self._vres         = CSRStorage(16, reset=1080,    description="Lines per frame.")
        self._bar_words    = CSRStorage(16, reset=1920//16, description="Colour bar width (words).")
        self._frame_period = CSRStorage(32, reset=int(sys_clk_freq/30), description="Frame period (sys cycles).")
        self._frames       = CSRStatus(32, description="Generated frames.")
        self._skipped      = CSRStatus(32, description="Skipped frame ticks (frame still being sent).")

        # # #

        enable = self._enable.storage

        # Frame timer.
        timer   = Signal(32)
        tick    = Signal()
        pending = Signal()
        self.sync += [
            tick.eq(0),
            If(~enable,
                timer.eq(0),
            ).Elif(timer >= (self._frame_period.storage - 1),
                timer.eq(0),
                tick.eq(1),
            ).Else(
                timer.eq(timer + 1),
            )
        ]

        # Counters.
        x       = Signal(16) # Word index in line.
        y       = Signal(16) # Line.
        bar     = Signal(3)
        bar_x   = Signal(16)
        frame   = Signal(32)
        skipped = Signal(32)
        active  = Signal()
        last_x  = Signal()
        last_y  = Signal()
        self.comb += [
            last_x.eq(x == (self._hwords.storage - 1)),
            last_y.eq(y == (self._vres.storage   - 1)),
        ]
        self.sync += [
            If(tick,
                If(active,
                    skipped.eq(skipped + 1),
                ).Else(
                    pending.eq(1),
                )
            ),
            If(~active & pending,
                active.eq(1),
                pending.eq(0),
                x.eq(0), y.eq(0), bar.eq(0), bar_x.eq(0),
            ).Elif(source.valid & source.ready,
                If(last_x,
                    x.eq(0), bar.eq(0), bar_x.eq(0),
                    If(last_y,
                        y.eq(0),
                        active.eq(0),
                        frame.eq(frame + 1),
                    ).Else(
                        y.eq(y + 1),
                    )
                ).Else(
                    x.eq(x + 1),
                    If(bar_x == (self._bar_words.storage - 1),
                        bar_x.eq(0),
                        bar.eq(bar + 1),
                    ).Else(
                        bar_x.eq(bar_x + 1),
                    )
                )
            ),
            If(~enable, active.eq(0), pending.eq(0)),
        ]
        self.comb += [
            self._frames.status.eq(frame),
            self._skipped.status.eq(skipped),
        ]

        # Pixel data.
        bar_word  = Signal(32)
        cases = {i: bar_word.eq(yuy2_word(y_, u, y_, v)) for i, (y_, u, v) in enumerate(COLOR_BARS)}
        self.comb += Case(bar, cases)

        moving    = Signal()
        ramp      = Signal()
        framebits = Signal()
        self.comb += [
            # Moving white bar (16 words wide, 4 words/frame).
            moving.eq((x - (frame[:14] << 2))[:16] < 16),
            # Bottom 64 lines: luma ramp.
            ramp.eq(y >= (self._vres.storage - 64)),
            # First line, first 32 words: frame number bits (white = 1, black = 0).
            framebits.eq((y == 0) & (x < 32)),
        ]
        data = Signal(32)
        self.comb += [
            If(framebits,
                If((frame >> x[:5])[0],
                    data.eq(yuy2_word(235, 128, 235, 128)),
                ).Else(
                    data.eq(yuy2_word(16, 128, 16, 128)),
                )
            ).Elif(ramp,
                data.eq(yuy2_word(x[:8], 128, x[:8], 128)),
            ).Elif(moving,
                data.eq(yuy2_word(235, 128, 235, 128)),
            ).Else(
                data.eq(bar_word),
            ),
            source.valid.eq(active),
            source.data.eq(data),
            source.first.eq((x == 0) & (y == 0)),
            source.last.eq(last_x & last_y),
        ]
