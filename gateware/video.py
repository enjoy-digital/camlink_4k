#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Video: YUY2 test pattern generator.

Generates frames of `hres` x `vres` YUY2 pixels (32-bit words = 2 pixels: Y0 U Y1 V, little
endian) at a rate set by `frame_period` (in sys clock cycles). Words are produced as fast as the
downstream accepts them; a frame starts on each period tick (skipped if the previous one is still
being sent).

Pattern: 8 colour bars (BT.601 limited range), a moving white bar and a bottom luma ramp, plus the
frame number encoded in the first 32 words of the first line (for host-side checks). The "no
signal" mode replaces the bars and ramp with a flat dark blue background (moving bar and frame
number kept, so the stream is visibly alive).
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

def m420_words(y, u, v):
    """M420 words of a constant colour: Y line word (4 luma), CbCr line word (2 CbCr pairs)."""
    return y * 0x01010101, (v << 24) | (u << 16) | (v << 8) | u

# Video Pattern Generator --------------------------------------------------------------------------

class VideoPatternGenerator(LiteXModule):
    def __init__(self, sys_clk_freq, with_csr=True):
        self.source = source = stream.Endpoint([("data", 32)])

        # Control.
        self.enable       = Signal()
        self.hwords       = Signal(16, reset=1920//2)
        self.vres         = Signal(16, reset=1080)
        self.bar_words    = Signal(16, reset=1920//16)
        self.frame_period = Signal(32, reset=int(sys_clk_freq/30))
        self.mode         = Signal()
        self.m420         = Signal()

        # Status.
        self.frames  = Signal(32)
        self.skipped = Signal(32)

        # # #

        enable = self.enable

        # Frame timer.
        timer   = Signal(32)
        tick    = Signal()
        pending = Signal()
        self.sync += [
            tick.eq(0),
            If(~enable,
                timer.eq(0),
            ).Elif(timer >= (self.frame_period - 1),
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
        frame   = self.frames
        skipped = self.skipped
        active  = Signal()
        last_x  = Signal()
        last_y  = Signal()
        self.comb += [
            last_x.eq(x == (self.hwords - 1)),
            last_y.eq(y == (self.vres   - 1)),
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
                    If(bar_x == (self.bar_words - 1),
                        bar_x.eq(0),
                        bar.eq(bar + 1),
                    ).Else(
                        bar_x.eq(bar_x + 1),
                    )
                )
            ),
            If(~enable, active.eq(0), pending.eq(0)),
        ]

        # M420 layout: words in M420 lines of hres/4 words (4 pixels/word), bars of bar_words/2
        # words.
        m420     = self.m420
        mx       = Signal(16)
        ms       = Signal(2)  # 0/1: Y lines, 2: CbCr line.
        mbar     = Signal(3)
        mbar_x   = Signal(16)
        m_last_x = Signal()
        self.comb += m_last_x.eq(mx == ((self.hwords >> 1) - 1))
        self.sync += [
            If(~active & pending,
                mx.eq(0), ms.eq(0), mbar.eq(0), mbar_x.eq(0),
            ).Elif(source.valid & source.ready,
                If(m_last_x,
                    mx.eq(0), mbar.eq(0), mbar_x.eq(0),
                    ms.eq(Mux(ms == 2, 0, ms + 1)),
                ).Else(
                    mx.eq(mx + 1),
                    If(mbar_x == ((self.bar_words >> 1) - 1),
                        mbar_x.eq(0),
                        mbar.eq(mbar + 1),
                    ).Else(
                        mbar_x.eq(mbar_x + 1),
                    )
                )
            )
        ]
        m_word = Signal(32)
        m_blue = Signal(32)
        self.comb += [
            Case(mbar, {i: m_word.eq(Mux(ms == 2, m420_words(*c)[1], m420_words(*c)[0]))
                for i, c in enumerate(COLOR_BARS)}),
            m_blue.eq(Mux(ms == 2, m420_words(40, 170, 118)[1], m420_words(40, 170, 118)[0])),
        ]

        # Pixel data.
        bar_word = Signal(32)
        self.comb += Case(bar, {i: bar_word.eq(yuy2_word(y_, u, y_, v))
            for i, (y_, u, v) in enumerate(COLOR_BARS)})

        moving    = Signal()
        ramp      = Signal()
        framebits = Signal()
        self.comb += [
            # Moving white bar (16 words wide, 4 words/frame).
            moving.eq((x - (frame[:14] << 2))[:16] < 16),
            # Bottom 64 lines: luma ramp.
            ramp.eq(y >= (self.vres - 64)),
            # First line, first 32 words: frame number bits (white = 1, black = 0).
            framebits.eq((y == 0) & (x < 32)),
        ]
        data = Signal(32)
        self.comb += [
            If(m420,
                data.eq(Mux(self.mode, m_blue, m_word)),
            ).Elif(framebits,
                If((frame >> x[:5])[0],
                    data.eq(yuy2_word(235, 128, 235, 128)),
                ).Else(
                    data.eq(yuy2_word(16, 128, 16, 128)),
                )
            ).Elif(moving,
                data.eq(yuy2_word(235, 128, 235, 128)),
            ).Elif(self.mode,
                data.eq(yuy2_word(40, 170, 40, 118)), # Dark blue.
            ).Elif(ramp,
                data.eq(yuy2_word(x[:8], 128, x[:8], 128)),
            ).Else(
                data.eq(bar_word),
            ),
            source.valid.eq(active),
            source.data.eq(data),
            source.first.eq((x == 0) & (y == 0)),
            source.last.eq(last_x & last_y),
        ]

        if with_csr:
            self.add_csr()

    def add_csr(self):
        frame_period_reset = self.frame_period.reset.value

        self._enable       = CSRStorage(description="Enable pattern generator.")
        self._hwords       = CSRStorage(16, reset=1920//2, description="Words per line (hres/2).")
        self._vres         = CSRStorage(16, reset=1080,    description="Lines per frame.")
        self._bar_words    = CSRStorage(16, reset=1920//16, description="Colour bar width (words).")
        self._frame_period = CSRStorage(32, reset=frame_period_reset,
            description="Frame period (sys cycles).")
        self._mode         = CSRStorage(1, description="Pattern: 0 = colour bars, 1 = no signal.")
        self._m420         = CSRStorage(1, description="M420 layout (4:2:0: lines of Y, Y, CbCr), "
            "`hwords`/`vres` still sized as YUY2 words (hres/2 x vres*3/4).")
        self._frames       = CSRStatus(32, description="Generated frames.")
        self._skipped      = CSRStatus(32,
            description="Skipped frame ticks (frame still being sent).")

        self.comb += [
            self.enable.eq(self._enable.storage),
            self.hwords.eq(self._hwords.storage),
            self.vres.eq(self._vres.storage),
            self.bar_words.eq(self._bar_words.storage),
            self.frame_period.eq(self._frame_period.storage),
            self.mode.eq(self._mode.storage),
            self.m420.eq(self._m420.storage),
            self._frames.status.eq(self.frames),
            self._skipped.status.eq(self.skipped),
        ]
