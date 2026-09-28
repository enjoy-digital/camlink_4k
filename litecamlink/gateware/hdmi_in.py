#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""HDMI In: IT6802 parallel video capture (YUV 4:2:2 16-bit SDR) to a YUY2 frame stream.

The IT6802 is configured to output YUV 4:2:2 8-bit (16-bit bus): each pixel carries Y and one
chroma sample (Cb on even pixels, Cr on odd ones). Two pixels are packed into a 32-bit YUY2 word
(Y0 Cb Y1 Cr, little endian). The Y/C byte lanes (QE[11:4], QE[23:16], QE[35:28]) are selectable.

The active area (DE) is measured on each frame (`hres`/`vres` CSRs). Output frames are marked with
`first`/`last`; `last` is set on the last word of the last line using the previous frame's height.
A frame is only admitted when the CDC FIFO has room for `admit_level` words, otherwise it is fully
dropped (`dropped` CSR); if the FIFO still overflows, the frame is truncated but `last` is kept.
Frame admission and the frame FIFO run in the sys domain (drains faster than the pixel rate).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# HDMI In ------------------------------------------------------------------------------------------

class HDMIIn(LiteXModule):
    def __init__(self, pads, fifo_depth=2048, sim=False):
        self.source = source = stream.Endpoint([("data", 32)])

        self.control = CSRStorage(fields=[
            CSRField("enable",  size=1, offset=0, description="Enable capture."),
            CSRField("y_lane",  size=2, offset=4, reset=1, description="Y byte lane (0: QE[11:4], 1: QE[23:16], 2: QE[35:28])."),
            CSRField("c_lane",  size=2, offset=6, reset=0, description="C byte lane."),
            CSRField("c_swap",  size=1, offset=8, description="Swap Cb/Cr order."),
        ])
        self.admit_level = CSRStorage(16, reset=fifo_depth//2, description="Minimum free FIFO words to admit a frame.")
        self.hres     = CSRStatus(16, description="Measured active width (pixels).")
        self.vres     = CSRStatus(16, description="Measured active height (lines).")
        self.frames   = CSRStatus(32, description="Captured frames.")
        self.dropped  = CSRStatus(32, description="Dropped frames (not admitted).")
        self.overflow = CSRStatus(32, description="Words lost on FIFO overflow.")

        # # #

        # Clock domain (IT6802 PCLK).
        self.cd_hdmi = ClockDomain()
        self.comb += self.cd_hdmi.clk.eq(pads.pclk)

        # Control (CDC).
        enable = Signal()
        y_lane = Signal(2)
        c_lane = Signal(2)
        c_swap = Signal()
        self.specials += [
            MultiReg(self.control.fields.enable, enable, "hdmi"),
            MultiReg(self.control.fields.y_lane, y_lane, "hdmi"),
            MultiReg(self.control.fields.c_lane, c_lane, "hdmi"),
            MultiReg(self.control.fields.c_swap, c_swap, "hdmi"),
        ]

        # Input registers.
        qe = Signal(24)
        de = Signal()
        vs = Signal()
        self.sync.hdmi += [
            qe.eq(pads.qe),
            de.eq(pads.de),
            vs.eq(pads.vsync),
        ]

        # Lanes.
        y = Signal(8)
        c = Signal(8)
        self.comb += [
            Case(y_lane, {0: y.eq(qe[0:8]), 1: y.eq(qe[8:16]), "default": y.eq(qe[16:24])}),
            Case(c_lane, {0: c.eq(qe[0:8]), 1: c.eq(qe[8:16]), "default": c.eq(qe[16:24])}),
        ]

        # Timing measurement (DE/VS).
        de_d     = Signal()
        vs_d     = Signal()
        x        = Signal(16) # Pixel in line.
        line     = Signal(16) # Active line in frame.
        hres     = Signal(16)
        vres     = Signal(16)
        vs_start = Signal()
        self.comb += vs_start.eq(vs & ~vs_d)
        self.sync.hdmi += [
            de_d.eq(de),
            vs_d.eq(vs),
            If(de,
                x.eq(x + 1),
            ).Else(
                x.eq(0),
            ),
            If(de_d & ~de,
                hres.eq(x),
                line.eq(line + 1),
            ),
            If(vs_start,
                If(line != 0, vres.eq(line)),
                line.eq(0),
            ),
        ]
        self.specials += [
            MultiReg(hres, self.hres.status),
            MultiReg(vres, self.vres.status),
        ]

        # Pixel pairing: YUY2 word = Y0 | C0 << 8 | Y1 << 16 | C1 << 24.
        y0     = Signal(8)
        c0     = Signal(8)
        odd    = Signal()
        cdc    = stream.Endpoint([("data", 32)])
        self.sync.hdmi += [
            cdc.valid.eq(0),
            If(de & enable & (vres != 0), # Wait for a measured frame height.
                odd.eq(~odd),
                If(~odd,
                    y0.eq(y),
                    c0.eq(c),
                ).Else(
                    cdc.valid.eq(1),
                    cdc.data.eq(Mux(c_swap, Cat(y0, c, y, c0), Cat(y0, c0, y, c))),
                    cdc.first.eq((line == 0) & (x == 1)),
                    cdc.last.eq((line == (vres - 1)) & ~pads.de), # Last pixel of the last line.
                )
            ).Else(
                odd.eq(0),
            )
        ]

        # CDC (hdmi -> sys): the sys side always drains faster than the input rate.
        self.cdc = cdc_fifo = stream.ClockDomainCrossing([("data", 32)], cd_from="hdmi", cd_to="sys", depth=16)
        self.comb += cdc.connect(cdc_fifo.sink)

        # Frame admission / drop (sys) + frame FIFO.
        self.fifo = fifo = stream.SyncFIFO([("data", 32)], fifo_depth, buffered=True)
        in_frame     = Signal()
        pending_last = Signal()
        frames       = Signal(32)
        dropped      = Signal(32)
        overflow     = Signal(32)
        sink         = cdc_fifo.source
        admit_ok     = Signal()
        self.comb += [
            sink.ready.eq(1),
            admit_ok.eq((fifo_depth - fifo.level) >= self.admit_level.storage),
            If(pending_last,
                # Frame end lost on overflow: push an end marker word as soon as possible.
                fifo.sink.valid.eq(1),
                fifo.sink.data.eq(0),
                fifo.sink.last.eq(1),
            ).Else(
                fifo.sink.valid.eq(sink.valid & (in_frame | (sink.first & admit_ok))),
                fifo.sink.data.eq(sink.data),
                fifo.sink.first.eq(sink.first),
                fifo.sink.last.eq(sink.last),
            ),
            fifo.source.connect(source),
        ]
        self.sync += [
            If(sink.valid & sink.first,
                If(admit_ok,
                    in_frame.eq(1),
                    frames.eq(frames + 1),
                ).Else(
                    in_frame.eq(0),
                    dropped.eq(dropped + 1),
                )
            ),
            If(sink.valid & (in_frame | (sink.first & admit_ok)),
                If(~fifo.sink.ready,
                    overflow.eq(overflow + 1),
                    If(sink.last, pending_last.eq(1)),
                ),
                If(sink.last, in_frame.eq(0)),
            ),
            If(pending_last & fifo.sink.ready, pending_last.eq(0)),
        ]
        self.comb += [
            self.frames.status.eq(frames),
            self.dropped.status.eq(dropped),
            self.overflow.status.eq(overflow),
        ]
