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

A burst also ends on `sink.last` (end of a UVC payload): when this happens before the FX3 buffer is
full, a single-cycle EOP strobe on CTL2 (sent 4 cycles after the last word, once the FX3 is back in
its IDLE state) makes the FX3 commit the partial buffer (short packet).

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
        self.sink     = sink = stream.Endpoint([("data", 32)])
        self.eop_data = Signal(32) # Word presented on DQ during EOP (first word of the next buffer).

        self._control    = CSRStorage(fields=[
            CSRField("enable",     size=1, offset=0,  description="Enable streaming."),
            CSRField("flag_invert",size=1, offset=1,  description="Invert FLAG (CTL1) polarity."),
            CSRField("data_delay", size=2, offset=4,  description="DQ delay (cycles) relative to VALID."),
            CSRField("head_lead",  size=3, offset=8,  reset=2, description="Cycles a word is presented on DQ before VALID after a gap."),
            CSRField("dq_cycles",  size=1, offset=12, description="Debug: drive a cycle counter on DQ outside bursts."),
        ])
        self._burst      = CSRStorage(32, reset=16384//4, description="Burst length (32-bit words).")
        self._guard      = CSRStorage(8,  reset=32,       description="Guard cycles after a burst.")
        self._status     = CSRStatus(fields=[
            CSRField("flag",   size=1, offset=0, description="FLAG (CTL1) level."),
        ])
        self._bursts     = CSRStatus(32, description="Number of sent bursts.")
        self._last       = CSRStatus(32, description="Last word sent (debug).")
        self._wait_cycles    = CSRStatus(32, description="Cycles waiting for FLAG (FX3 not ready).")
        self._starve_cycles  = CSRStatus(32, description="Cycles in a burst without data (source starved).")
        self._active_cycles  = CSRStatus(32, description="Cycles sending data.")
        self._eops           = CSRStatus(32, description="Number of EOP strobes.")
        self._eop_cycle      = CSRStatus(32, description="Debug: cycle counter at the last EOP strobe.")
        self._burst_cycle    = CSRStatus(32, description="Debug: cycle counter at the last burst first word.")
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
        self.comb += ctl.oe.eq(0b101) # CTL0 (VALID), CTL2 (EOP).

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
        eop    = Signal()
        data   = Signal(32)
        bursts = Signal(32)
        self.fsm = fsm = ClockDomainsRenamer("gpif")(FSM(reset_state="WAIT"))
        fsm.act("WAIT",
            NextValue(count, 0),
            If(enable & flag,
                NextState("BURST"),
            )
        )
        # DQ always presents the FIFO head and, after any gap, a word is only sent once it has been
        # presented for one cycle: the FX3 samples the first word after a gap one cycle early.
        head_lead  = Signal(3)
        head_count = Signal(3)
        head_valid = Signal()
        self.specials += MultiReg(self._control.fields.head_lead, head_lead, "gpif")
        self.comb += [
            data.eq(fifo.source.data),
            head_valid.eq(fifo.source.valid & (head_count >= head_lead)),
        ]
        self.sync.gpif += [
            If(~fifo.source.valid,
                head_count.eq(0),
            ).Elif(head_count < 7,
                head_count.eq(head_count + 1),
            )
        ]
        fsm.act("BURST",
            valid.eq(head_valid),
            fifo.source.ready.eq(head_valid),
            If(head_valid,
                NextValue(count, count + 1),
                If(count == (burst - 1),
                    NextValue(gcount, 0),
                    NextValue(bursts, bursts + 1),
                    NextState("GUARD"),
                ).Elif(fifo.source.last,
                    NextValue(gcount, 0),
                    NextValue(bursts, bursts + 1),
                    NextState("EOP"),
                )
            )
        )
        fsm.act("EOP",
            eop.eq(gcount == 4),
            NextValue(gcount, gcount + 1),
            If(gcount == 4,
                NextValue(gcount, 0),
                NextState("GUARD"),
            )
        )
        fsm.act("GUARD",
            NextValue(gcount, gcount + 1),
            If(gcount == guard,
                NextState("WAIT"),
            )
        )
        self.specials += MultiReg(bursts, self._bursts.status)
        wait_cycles   = Signal(32)
        starve_cycles = Signal(32)
        active_cycles = Signal(32)
        eops          = Signal(32)
        self.specials += MultiReg(eops, self._eops.status)
        self.sync.gpif += [
            If(fsm.ongoing("WAIT") & enable & ~flag, wait_cycles.eq(wait_cycles + 1)),
            If(fsm.ongoing("BURST") & ~valid,        starve_cycles.eq(starve_cycles + 1)),
            If(valid,                                active_cycles.eq(active_cycles + 1)),
            If(eop,                                  eops.eq(eops + 1)),
        ]
        self.specials += [
            MultiReg(wait_cycles,   self._wait_cycles.status),
            MultiReg(starve_cycles, self._starve_cycles.status),
            MultiReg(active_cycles, self._active_cycles.status),
        ]
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
        eop_data    = Signal(32)
        self.specials += MultiReg(self.eop_data, eop_data, "gpif")
        cycles      = Signal(32)
        dq_cycles   = Signal()
        eop_cycle   = Signal(32)
        self.sync.gpif += cycles.eq(cycles + 1)
        self.sync.gpif += If(eop, eop_cycle.eq(cycles))
        burst_cycle = Signal(32)
        first_word  = Signal()
        self.sync.gpif += [
            If(fsm.ongoing("WAIT"), first_word.eq(1)),
            If(valid & first_word, first_word.eq(0), burst_cycle.eq(cycles)),
        ]
        self.specials += MultiReg(burst_cycle, self._burst_cycle.status)
        self.specials += MultiReg(self._control.fields.dq_cycles, dq_cycles, "gpif")
        self.specials += MultiReg(eop_cycle, self._eop_cycle.status)
        force       = Signal()
        force_value = Signal(32)
        self.specials += [
            MultiReg(self._force.fields.enable, force,       "gpif"),
            MultiReg(self._force_value.storage, force_value, "gpif"),
        ]
        self.sync.gpif += [
            If(force,
                pads.dq.eq(force_value),
            ).Elif(fsm.ongoing("EOP"),
                # The FX3 latches DQ at commit and uses it as the first word of its next buffer.
                pads.dq.eq(eop_data),
            ).Elif(dq_cycles & ~fsm.ongoing("BURST"),
                pads.dq.eq(0x80000000 | cycles),
            ).Else(
                pads.dq.eq(data_o),
            ),
            ctl.o[0].eq(valid),
            ctl.o[2].eq(eop),
        ]

# Pattern Generator --------------------------------------------------------------------------------

class CounterGenerator(LiteXModule):
    """32-bit incrementing counter stream, with optional on/off gaps and packet `last` (tests)."""
    def __init__(self):
        self.source = source = stream.Endpoint([("data", 32)])
        self.enable = CSRStorage(description="Enable counter generator.")
        self.on     = CSRStorage(32, description="Words per packet (0 = continuous).")
        self.off    = CSRStorage(32, description="Idle cycles after each packet.")
        self.last   = CSRStorage(description="Set `last` on the last word of each packet.")

        # # #

        count = Signal(32)
        idle  = Signal(32)
        self.comb += [
            source.valid.eq(self.enable.storage & (idle == 0)),
            source.last.eq(self.last.storage & (self.on.storage != 0) & (count == (self.on.storage - 1))),
        ]
        self.sync += [
            If(idle != 0,
                idle.eq(idle - 1),
            ).Elif(source.valid & source.ready,
                source.data.eq(source.data + 1),
                If((self.on.storage != 0) & (count == (self.on.storage - 1)),
                    count.eq(0),
                    idle.eq(self.off.storage),
                ).Else(
                    count.eq(count + 1),
                )
            )
        ]
