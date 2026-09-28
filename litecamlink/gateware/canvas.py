#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Canvas: places an input frame inside a larger output frame (black borders, letterbox/pillarbox).

Output frames of `out_hwords` x `out_vres` YUY2 words. Positions inside the window (`x0`, `y0`,
`in_hwords` x `in_vres`) take the input words (the output waits for them), others are black. An
output frame starts on an input frame start (`first`); an input frame ending early leaves the
rest of the window black, extra input words are dropped. Disabled: pass-through.
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

BLACK = 0x80108010 # YUY2 black (Y=16, U=V=128).

# Canvas -------------------------------------------------------------------------------------------

class Canvas(LiteXModule):
    def __init__(self):
        self.sink   = sink   = stream.Endpoint([("data", 32)])
        self.source = source = stream.Endpoint([("data", 32)])

        self.enable    = CSRStorage(1,  description="Enable canvas (else pass-through).")
        self.out_hwords = CSRStorage(16, reset=1920//2, description="Output words per line.")
        self.out_vres   = CSRStorage(16, reset=1080,    description="Output lines.")
        self.in_hwords  = CSRStorage(16, reset=1280//2, description="Window (input) words per line.")
        self.in_vres    = CSRStorage(16, reset=720,     description="Window (input) lines.")
        self.x0         = CSRStorage(16, reset=320//2,  description="Window X (words).")
        self.y0         = CSRStorage(16, reset=180,     description="Window Y (lines).")

        # # #

        enable = self.enable.storage

        x       = Signal(16)
        y       = Signal(16)
        in_x    = Signal() # x inside the window (registered flag, updated with x).
        in_y    = Signal() # y inside the window (registered flag, updated with y).
        in_done = Signal() # Input frame ended (early end: rest of the window black).
        last_x  = Signal()
        last_y  = Signal()
        self.comb += [
            last_x.eq(x == (self.out_hwords.storage - 1)),
            last_y.eq(y == (self.out_vres.storage - 1)),
        ]

        # Next position window flags (equality compares on the next x/y).
        x1 = Signal(16)
        y1 = Signal(16)
        self.comb += [
            x1.eq(self.x0.storage + self.in_hwords.storage),
            y1.eq(self.y0.storage + self.in_vres.storage),
        ]

        window = Signal()
        self.comb += window.eq(in_x & in_y & ~in_done)

        self.fsm = fsm = FSM(reset_state="SYNC")
        fsm.act("SYNC",
            # Wait for an input frame start (drop anything else).
            If(~enable,
                sink.connect(source),
            ).Else(
                sink.ready.eq(~sink.first),
                If(sink.valid & sink.first,
                    NextValue(x, 0),
                    NextValue(y, 0),
                    NextValue(in_x, self.x0.storage == 0),
                    NextValue(in_y, self.y0.storage == 0),
                    NextValue(in_done, 0),
                    NextState("FRAME"),
                )
            )
        )
        fsm.act("FRAME",
            If(window,
                source.valid.eq(sink.valid),
                source.data.eq(sink.data),
                sink.ready.eq(source.ready),
            ).Else(
                source.valid.eq(1),
                source.data.eq(BLACK),
            ),
            source.first.eq((x == 0) & (y == 0)),
            source.last.eq(last_x & last_y),
            If(source.valid & source.ready,
                If(window & sink.last,
                    NextValue(in_done, 1),
                ),
                If(last_x,
                    NextValue(x, 0),
                    NextValue(in_x, self.x0.storage == 0),
                    NextValue(y, y + 1),
                    If(y + 1 == self.y0.storage, NextValue(in_y, 1)),
                    If(y + 1 == y1,              NextValue(in_y, 0)),
                    If(last_y,
                        # Next frame (the rest of a longer input frame is dropped in SYNC).
                        NextState("SYNC"),
                    )
                ).Else(
                    NextValue(x, x + 1),
                    If(x + 1 == self.x0.storage, NextValue(in_x, 1)),
                    If(x + 1 == x1,              NextValue(in_x, 0)),
                )
            )
        )
