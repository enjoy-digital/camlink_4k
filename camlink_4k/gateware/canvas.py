#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Canvas: places an input frame inside a larger output frame (black borders, letterbox/pillarbox).

Output frames of `out_hwords` x `out_vres` YUY2 words. Positions inside the window (`x0`, `y0`,
`in_hwords` x `in_vres`) take the input words (the output waits for them), others are black. An
input frame ending early leaves the rest of the window black, extra input words are dropped.

Output frames are back to back: the top border is emitted, then the canvas waits at the first
window word for an input frame start (`admit` gates the input frame admission: only frames starting
while waiting are admitted, frames starting during the borders are dropped whole instead of
overflowing the input FIFO). The output runs at the input rate or at a fraction of it (e.g. 30fps
from a 60fps input when the borders do not fit in the input blanking). Disabled: pass-through.
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
        self.admit  = Signal() # Ready for a new input frame (gates the input frame admission).

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
        in_done = Signal() # Input frame ended (early end: rest of the window black).
        last_x  = Signal()
        last_y  = Signal()
        in_x    = Signal()
        in_y    = Signal()
        # Window bounds registered from the (static) CSRs; reset_less: valid right after a path
        # reset. The window flags are registers updated with the counters from equality checks
        # (timing: magnitude comparators on the counters limited the sys clock).
        x1   = Signal(16, reset_less=True)
        y1   = Signal(16, reset_less=True)
        x0m1 = Signal(16, reset_less=True)
        y0m1 = Signal(16, reset_less=True)
        x1m1 = Signal(16, reset_less=True)
        y1m1 = Signal(16, reset_less=True)
        x0z  = Signal(reset_less=True)
        y0z  = Signal(reset_less=True)
        self.sync += [
            x1.eq(self.x0.storage + self.in_hwords.storage),
            y1.eq(self.y0.storage + self.in_vres.storage),
            x0m1.eq(self.x0.storage - 1),
            y0m1.eq(self.y0.storage - 1),
            x1m1.eq(x1 - 1),
            y1m1.eq(y1 - 1),
            x0z.eq(self.x0.storage == 0),
            y0z.eq(self.y0.storage == 0),
        ]
        self.comb += [
            last_x.eq(x == (self.out_hwords.storage - 1)),
            last_y.eq(y == (self.out_vres.storage - 1)),
        ]
        started = Signal()  # Window flags computed for (0, 0).
        settle  = Signal(2) # Cycles for the registered bounds to follow the CSRs.

        window = Signal()
        self.comb += window.eq(in_x & in_y & ~in_done)

        # Output frames are emitted back to back: the top border is emitted first, then the canvas
        # waits (at the first window word) for an input frame start and consumes that frame at the
        # input pace (per line: window + left/right borders), then the bottom border. Emitting the
        # top border before syncing avoids overflowing the input FIFO (the borders between two
        # windows cannot fit in the input vertical blanking): input frames arriving during the
        # borders are skipped (partial input frames are dropped in SYNC).
        synced = Signal() # Window synchronized on an input frame start (current output frame).
        self.fsm = fsm = FSM(reset_state="FRAME")
        fsm.act("FRAME",
            If(~enable,
                sink.connect(source),
                NextValue(x, 0),
                NextValue(y, 0),
                NextValue(in_done, 0),
                NextValue(synced, 0),
                NextValue(started, 0),
                NextValue(settle, 0),
            ).Elif(~started,
                NextValue(settle, settle + 1),
                If(settle == 3,
                    NextValue(in_x, x0z),
                    NextValue(in_y, y0z),
                    NextValue(started, 1),
                ),
            ).Elif(in_x & in_y & ~synced,
                # First window word: wait for an input frame start.
                NextState("SYNC"),
            ).Else(
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
                        NextValue(in_x, x0z),
                        NextValue(y, y + 1),
                        NextValue(in_y, Mux(in_y, y != y1m1, y == y0m1)),
                        If(last_y,
                            # Next output frame.
                            NextValue(y, 0),
                            NextValue(in_y, y0z),
                            NextValue(in_done, 0),
                            NextValue(synced, 0),
                        )
                    ).Else(
                        NextValue(x, x + 1),
                        NextValue(in_x, Mux(in_x, x != x1m1, x == x0m1)),
                    )
                )
            )
        )
        fsm.act("SYNC",
            # Drop input words until an input frame start.
            sink.ready.eq(~sink.first),
            If(sink.valid & sink.first,
                NextValue(synced, 1),
                NextState("FRAME"),
            ),
            If(~enable, NextState("FRAME")),
        )
        # Input frames are only admitted while waiting for one: a frame starting during the borders
        # would overflow the input FIFO and be consumed truncated.
        self.comb += self.admit.eq(~enable | fsm.ongoing("SYNC"))
