#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""GPIF: FPGA -> FX3 streaming over the 32-bit GPIF-II bus.

The FX3 is the clock master (PCLK) and pushes a DQ word to its DMA on each clock where VALID
(CTL0) is high. The FX3 exposes a DMA flag on CTL1 (FLAG). Data is sent in bursts of exactly one
FX3 DMA buffer: the FPGA waits for FLAG, sends `burst` words, waits `guard` cycles (for FLAG to
reflect the next buffer) and starts again.

The alignment between VALID and DQ (FX3 sampling latency) is configurable (`data_delay`).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# GPIF Streamer ------------------------------------------------------------------------------------

class GPIFStreamer(LiteXModule):
    def __init__(self, pads, clk_freq=100e6, fifo_depth=512, max_delay=4):
        self.sink = sink = stream.Endpoint([("data", 32)])

        self._control    = CSRStorage(fields=[
            CSRField("enable",     size=1, offset=0,  description="Enable streaming."),
            CSRField("flag_invert",size=1, offset=1,  description="Invert FLAG (CTL1) polarity."),
            CSRField("data_delay", size=2, offset=4,  description="DQ delay (cycles) relative to VALID."),
        ])
        self._burst      = CSRStorage(32, reset=16384//4, description="Burst length (32-bit words).")
        self._guard      = CSRStorage(8,  reset=32,       description="Guard cycles after a burst.")
        self._status     = CSRStatus(fields=[
            CSRField("flag",   size=1, offset=0, description="FLAG (CTL1) level."),
        ])
        self._bursts     = CSRStatus(32, description="Number of sent bursts.")
        self._last       = CSRStatus(32, description="Last word sent (debug).")
        self._force      = CSRStorage(fields=[
            CSRField("enable", size=1, offset=0, description="Force DQ to `force_value` (debug)."),
        ])
        self._force_value = CSRStorage(32, description="Forced DQ value (debug).")

        # # #

        # Clock domain (FX3 PCLK).
        self.cd_gpif = ClockDomain()
        self.comb += self.cd_gpif.clk.eq(pads.pclk)

        # CDC.
        self.fifo = fifo = stream.ClockDomainCrossing([("data", 32)], cd_from="sys", cd_to="gpif",
            depth=fifo_depth)
        self.comb += sink.connect(fifo.sink)

        enable      = Signal()
        flag_invert = Signal()
        data_delay  = Signal(2)
        burst       = Signal(32)
        guard       = Signal(8)
        self.specials += [
            MultiReg(self._control.fields.enable,      enable,      "gpif"),
            MultiReg(self._control.fields.flag_invert, flag_invert, "gpif"),
            MultiReg(self._control.fields.data_delay,  data_delay,  "gpif"),
            MultiReg(self._burst.storage,              burst,       "gpif"),
            MultiReg(self._guard.storage,              guard,       "gpif"),
        ]

        # CTL: per-bit tristate (CTL0 = VALID output, CTL1 = FLAG input, others inputs).
        self.ctl = ctl = TSTriple(len(pads.ctl))
        self.specials += ctl.get_tristate(pads.ctl)
        self.comb += ctl.oe.eq(0b1)

        # FLAG input.
        flag_i = Signal()
        flag   = Signal()
        self.sync.gpif += flag_i.eq(ctl.i[1])
        self.comb += flag.eq(flag_i ^ flag_invert)
        self.specials += MultiReg(flag, self._status.fields.flag)

        # Burst FSM.
        count  = Signal(32)
        gcount = Signal(8)
        valid  = Signal()
        data   = Signal(32)
        bursts = Signal(32)
        self.fsm = fsm = ClockDomainsRenamer("gpif")(FSM(reset_state="WAIT"))
        fsm.act("WAIT",
            NextValue(count, 0),
            If(enable & flag,
                NextState("BURST"),
            )
        )
        # DQ always presents the FIFO head: the FX3 samples the first word of a burst one cycle
        # before it sees VALID.
        self.comb += data.eq(fifo.source.data)
        fsm.act("BURST",
            valid.eq(fifo.source.valid),
            fifo.source.ready.eq(1),
            If(fifo.source.valid,
                NextValue(count, count + 1),
                If(count == (burst - 1),
                    NextValue(gcount, 0),
                    NextValue(bursts, bursts + 1),
                    NextState("GUARD"),
                )
            )
        )
        fsm.act("GUARD",
            NextValue(gcount, gcount + 1),
            If(gcount == guard,
                NextState("WAIT"),
            )
        )
        self.specials += MultiReg(bursts, self._bursts.status)
        last = Signal(32)
        self.sync.gpif += If(valid, last.eq(data))
        self.specials += MultiReg(last, self._last.status)

        # Outputs: VALID and DQ (DQ delayed by data_delay cycles), registered.
        data_pipe = [data]
        for i in range(max_delay - 1):
            d = Signal(32)
            self.sync.gpif += d.eq(data_pipe[-1])
            data_pipe.append(d)
        data_o  = Signal(32)
        valid_o = Signal()
        self.comb += Case(data_delay, {i: data_o.eq(data_pipe[i]) for i in range(max_delay)})
        force       = Signal()
        force_value = Signal(32)
        self.specials += [
            MultiReg(self._force.fields.enable, force,       "gpif"),
            MultiReg(self._force_value.storage, force_value, "gpif"),
        ]
        self.sync.gpif += [
            pads.dq.eq(Mux(force, force_value, data_o)),
            ctl.o[0].eq(valid),
        ]

# Pattern Generator --------------------------------------------------------------------------------

class CounterGenerator(LiteXModule):
    """32-bit incrementing counter stream (always valid when enabled)."""
    def __init__(self):
        self.source = source = stream.Endpoint([("data", 32)])
        self.enable = CSRStorage(description="Enable counter generator.")

        # # #

        self.comb += source.valid.eq(self.enable.storage)
        self.sync += If(source.valid & source.ready, source.data.eq(source.data + 1))
