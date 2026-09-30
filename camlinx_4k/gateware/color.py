#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Color: YUY2 brightness/contrast/saturation adjustment (UVC Processing Unit controls).

For each YUY2 word (Y0 U Y1 V):

    Y' = clamp(((Y - 16)*contrast >> 7) + 16 + brightness, 0, 255)
    C' = clamp(((C - 128)*saturation >> 7) + 128, 0, 255)

contrast/saturation are unsigned 1.7 fixed point (128 = 1.0), brightness is signed. Pipelined
(center, multiply, then offset/clamp); the stream is stalled as a whole (valid/ready follow the pipeline).
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# Color Adjust -------------------------------------------------------------------------------------

class ColorAdjust(LiteXModule):
    def __init__(self):
        self.sink   = sink   = stream.Endpoint([("data", 32)])
        self.source = source = stream.Endpoint([("data", 32)])

        self.brightness = CSRStorage(8, reset=0,   description="Brightness (signed luma offset).")
        self.contrast   = CSRStorage(8, reset=128, description="Contrast (luma gain, 128 = 1.0).")
        self.saturation = CSRStorage(8, reset=128, description="Saturation (chroma gain, 128 = 1.0).")

        # # #

        brightness = Signal((8, True))
        self.comb += brightness.eq(self.brightness.storage) # Reinterpreted as signed.

        # Pipeline control: 3 stages, advance when the output is free.
        stages  = 3
        advance = Signal()
        valid   = [Signal() for _ in range(stages)]
        first   = [Signal() for _ in range(stages)]
        last    = [Signal() for _ in range(stages)]
        self.comb += [
            advance.eq(~valid[-1] | source.ready),
            sink.ready.eq(advance),
        ]
        prev = (sink.valid, sink.first, sink.last)
        for i in range(stages):
            self.sync += If(advance,
                valid[i].eq(prev[0]),
                first[i].eq(prev[1]),
                last[i].eq(prev[2]),
            )
            prev = (valid[i], first[i], last[i])

        # Stage 0: centered components (registered, DSP input). Stage 1: x gain (DSP output).
        products = []
        for i in range(4):
            is_luma = (i % 2) == 0
            c = sink.data[8*i:8*(i+1)]
            d = Signal((9, True))
            g = Signal(8)
            p = Signal((18, True))
            gain = self.contrast.storage if is_luma else self.saturation.storage
            self.sync += If(advance,
                d.eq(c - (16 if is_luma else 128)),
                g.eq(gain),
                p.eq(d*g),
            )
            products.append((p, is_luma))

        # Stage 2: scale, offset, clamp.
        data = Signal(32)
        for i, (p, is_luma) in enumerate(products):
            v = Signal((12, True))
            r = Signal(8)
            self.comb += [
                v.eq((p >> 7) + ((16 + brightness) if is_luma else 128)),
                If(v < 0,
                    r.eq(0),
                ).Elif(v > 255,
                    r.eq(255),
                ).Else(
                    r.eq(v[:8]),
                ),
            ]
            self.sync += If(advance, data[8*i:8*(i+1)].eq(r))

        self.comb += [
            source.valid.eq(valid[-1]),
            source.first.eq(first[-1]),
            source.last.eq(last[-1]),
            source.data.eq(data),
        ]
