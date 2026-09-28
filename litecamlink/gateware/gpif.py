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

Audio (optional) is sent on GPIF thread 1 between video bursts (never inside a video burst: a
thread switch with a partially filled thread 0 buffer corrupts the video stream; bursts only start
when video data is available so audio is serviced during blanking): audio samples are packetized in
`audio_packet_words` packets (one FX3 thread 1 DMA buffer, 1ms). When a packet is available and the
thread 1 DMA flag (CTL4) is ready, the FPGA asserts ASEL (CTL3), waits `audio_lead` cycles for the
FX3 to switch to its thread 1 states, sends the packet (VALID) and releases ASEL.
As with video, the FX3 uses the DQ value latched at a buffer switch as the first word of the next
buffer (the first word sent is dropped): a packet is only sent once the first word of the next
packet is available, and that word stays on DQ during the guard time after the packet.
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# GPIF Streamer ------------------------------------------------------------------------------------

class GPIFStreamer(LiteXModule):
    def __init__(self, pads, clk_freq=100e6, fifo_depth=512, max_delay=4,
        with_audio=False, audio_packet_words=48, audio_fifo_depth=256, sim=False):
        self.sink       = sink = stream.Endpoint([("data", 32)])
        self.audio_sink = audio_sink = stream.Endpoint([("data", 32)]) # Audio samples (sys domain).
        self.eop_data   = Signal(32) # Word presented on DQ during EOP (first word of the next buffer).

        self._control    = CSRStorage(fields=[
            CSRField("enable",     size=1, offset=0,  description="Enable streaming."),
            CSRField("flag_invert",size=1, offset=1,  description="Invert FLAG (CTL1) polarity."),
            CSRField("data_delay", size=2, offset=4,  description="DQ delay (cycles) relative to VALID."),
            CSRField("head_lead",  size=3, offset=8,  reset=2, description="Cycles a word is presented on DQ before VALID after a gap."),
            CSRField("dq_cycles",  size=1, offset=12, description="Debug: drive a cycle counter on DQ outside bursts."),
            CSRField("audio_enable", size=1, offset=16, description="Enable audio (GPIF thread 1)."),
            CSRField("audio_lead",   size=4, offset=20, reset=8, description="Cycles between ASEL and the first audio word."),
        ])
        self._burst      = CSRStorage(32, reset=16384//4, description="Burst length (32-bit words).")
        self._guard      = CSRStorage(8,  reset=32,       description="Guard cycles after a burst.")
        self._switch_guard = CSRStorage(16, reset=1024,   description="Minimum idle cycles before a thread switch (video <-> audio).")
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
        self._audio_packets = CSRStatus(32, description="Number of sent audio packets.")
        self._audio_status  = CSRStatus(fields=[
            CSRField("flag",  size=1, offset=0, description="Audio FLAG (CTL4) level."),
            CSRField("level", size=16, offset=16, description="Audio FIFO level (words)."),
        ])

        # # #

        # Clock domain (FX3 PCLK).
        # gpif: streaming logic, held in reset while video and audio are disabled (so a GPIF
        # restart always starts from WAIT with empty audio packets). gpif_cdc: same clock, no reset
        # (CDC FIFO read sides).
        self.cd_gpif     = ClockDomain()
        self.cd_gpif_cdc = ClockDomain(reset_less=True)
        if not sim:
            self.comb += self.cd_gpif.clk.eq(pads.pclk)
            self.comb += self.cd_gpif_cdc.clk.eq(pads.pclk)
        gpif_rst = Signal()
        self.specials += MultiReg(~(self._control.fields.enable | self._control.fields.audio_enable),
            gpif_rst, "gpif_cdc")
        self.comb += self.cd_gpif.rst.eq(gpif_rst)

        # CDC.
        self.fifo = fifo = stream.ClockDomainCrossing([("data", 32)], cd_from="sys", cd_to="gpif_cdc",
            depth=fifo_depth)
        self.comb += sink.connect(fifo.sink)
        # Video disabled: the CDC FIFO is drained in WAIT (no stale words at the next start).
        video_drain = Signal()
        self.specials += MultiReg(~self._control.fields.enable, video_drain, "gpif_cdc")

        enable      = Signal()
        flag_invert = Signal()
        data_delay  = Signal(2)
        burst       = Signal(32)
        guard       = Signal(8)
        switch_guard = Signal(16)
        self.specials += [
            MultiReg(self._control.fields.enable,      enable,      "gpif"),
            MultiReg(self._control.fields.flag_invert, flag_invert, "gpif"),
            MultiReg(self._control.fields.data_delay,  data_delay,  "gpif"),
            MultiReg(self._burst.storage,              burst,       "gpif"),
            MultiReg(self._guard.storage,              guard,       "gpif"),
            MultiReg(self._switch_guard.storage,       switch_guard, "gpif"),
        ]

        # CTL: per-bit tristate (CTL0 = VALID, CTL2 = EOP, CTL3 = ASEL outputs, CTL1 = FLAG,
        # CTL4 = audio FLAG inputs).
        self.ctl = ctl = TSTriple(len(pads.ctl))
        if not sim:
            self.specials += ctl.get_tristate(pads.ctl)
        self.comb += ctl.oe.eq(0b01101)

        # FLAG input.
        flag_i = Signal()
        flag   = Signal()
        self.sync.gpif += flag_i.eq(ctl.i[1])
        self.comb += flag.eq(flag_i ^ flag_invert)
        self.specials += MultiReg(flag, self._status.fields.flag)

        # Audio: samples -> CDC -> packet FIFO (gpif domain), packets count, audio FLAG input.
        audio_enable  = Signal()
        audio_lead    = Signal(4)
        audio_flag_i  = Signal()
        audio_flag    = Signal()
        audio_packets = Signal(8)
        audio_next    = Signal() # First word of the next packet available.
        self.specials += [
            MultiReg(self._control.fields.audio_enable, audio_enable, "gpif"),
            MultiReg(self._control.fields.audio_lead,   audio_lead,   "gpif"),
        ]
        self.sync.gpif += audio_flag_i.eq(ctl.i[4])
        self.comb += audio_flag.eq(audio_flag_i ^ flag_invert)
        self.specials += MultiReg(audio_flag, self._audio_status.fields.flag)
        if with_audio:
            self.audio_cdc  = audio_cdc  = stream.ClockDomainCrossing([("data", 32)],
                cd_from="sys", cd_to="gpif_cdc", depth=8)
            self.audio_fifo = audio_fifo = ClockDomainsRenamer("gpif")(
                stream.SyncFIFO([("data", 32)], audio_fifo_depth, buffered=True))
            audio_index   = Signal(max=audio_packet_words)
            audio_written = Signal(8)
            audio_read    = Signal(8)
            self.comb += [
                audio_sink.connect(audio_cdc.sink),
                audio_cdc.source.connect(audio_fifo.sink, omit={"last"}),
                audio_fifo.sink.last.eq(audio_index == (audio_packet_words - 1)),
                audio_packets.eq(audio_written - audio_read),
            ]
            self.sync.gpif += [
                If(audio_fifo.sink.valid & audio_fifo.sink.ready,
                    audio_index.eq(Mux(audio_index == (audio_packet_words - 1), 0, audio_index + 1)),
                    If(audio_fifo.sink.last, audio_written.eq(audio_written + 1)),
                ),
                If(audio_fifo.source.valid & audio_fifo.source.ready & audio_fifo.source.last,
                    audio_read.eq(audio_read + 1),
                ),
            ]
            self.comb += audio_next.eq(audio_fifo.level > audio_packet_words)
            self.specials += MultiReg(audio_fifo.level, self._audio_status.fields.level)
            audio_source = audio_fifo.source
        else:
            self.comb += audio_sink.ready.eq(1)
            audio_source = stream.Endpoint([("data", 32)])

        # Burst FSM.
        count  = Signal(32)
        gcount = Signal(8)
        valid  = Signal()
        eop    = Signal()
        data   = Signal(32)
        bursts = Signal(32)
        self.fsm = fsm = ClockDomainsRenamer("gpif")(FSM(reset_state="WAIT"))
        asel        = Signal()
        audio_ready = Signal()
        last_audio  = Signal() # Last data phase was audio (thread 1).
        idle        = Signal(16) # Cycles since the last word sent (saturating).
        switch_ok   = Signal()   # Thread switch allowed (previous thread buffer switch done).
        self.comb += audio_ready.eq(audio_enable & (audio_packets != 0) & audio_next & audio_flag)
        fsm.act("WAIT",
            fifo.source.ready.eq(video_drain),
            NextValue(count, 0),
            NextValue(gcount, 0),
            If(audio_ready & (flag | ~enable) & (last_audio | switch_ok),
                NextState("ASEL"),
            ).Elif(enable & flag & fifo.source.valid & (audio_flag | ~audio_enable) & (~last_audio | switch_ok),
                NextValue(last_audio, 0),
                NextState("BURST"),
            )
        )
        # DQ always presents the FIFO head and, after any gap, a word is only sent once it has been
        # presented for one cycle: the FX3 samples the first word after a gap one cycle early.
        head_lead  = Signal(3)
        head_count = Signal(3)
        head_valid = Signal()
        self.specials += MultiReg(self._control.fields.head_lead, head_lead, "gpif")
        # DQ source. The FX3 captures DQ as the first word of a thread's next DMA buffer when that
        # buffer becomes current (right after a buffer is filled/committed, or later when the USB
        # side frees one): outside bursts, DQ presents the next word of the last used thread (audio
        # FIFO head, or video FIFO head / next UVC header word when the video FIFO is empty; bursts
        # always end at payload boundaries), and a thread is only used once the other thread's
        # buffer switch is done: its FLAG is ready and `switch_guard` idle cycles
        # elapsed (the FX3 DMA switches buffers when its internal queue is drained, which lags the
        # last GPIF word under load). The head count restarts when the source changes.
        eop_data    = Signal(32)
        self.specials += MultiReg(self.eop_data, eop_data, "gpif")
        video_next  = Signal(32) # Next video word: FIFO head, or next UVC header word if empty.
        src_valid   = Signal()
        audio_sel   = Signal()
        audio_sel_d = Signal()
        self.comb += [
            audio_sel.eq(asel | (last_audio & ~fsm.ongoing("BURST") & ~fsm.ongoing("EOP"))),
            If(audio_sel,
                data.eq(audio_source.data),
                src_valid.eq(audio_source.valid),
            ).Else(
                data.eq(Mux(fsm.ongoing("BURST"), fifo.source.data, video_next)),
                src_valid.eq(fifo.source.valid),
            ),
            video_next.eq(Mux(fifo.source.valid, fifo.source.data, eop_data)),
            head_valid.eq(src_valid & (head_count >= head_lead)),
        ]
        self.comb += switch_ok.eq(idle >= switch_guard)
        self.sync.gpif += [
            If(valid,
                idle.eq(0),
            ).Elif(idle != 0xffff,
                idle.eq(idle + 1),
            ),
            audio_sel_d.eq(audio_sel),
            If(~src_valid | (audio_sel != audio_sel_d),
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
        # EOP: DQ presents `eop_data` around the EOP strobe (the FX3 latches DQ at commit time,
        # which happens 1-2 cycles after the strobe with the audio-capable waveform).
        fsm.act("EOP",
            eop.eq(gcount == 4),
            NextValue(gcount, gcount + 1),
            If(gcount == 7,
                NextValue(gcount, 0),
                NextState("GUARD"),
            )
        )
        audio_count      = Signal(max=max(audio_packet_words, 2))
        audio_sent       = Signal(32)
        fsm.act("ASEL",
            asel.eq(1),
            NextValue(gcount, gcount + 1),
            If(gcount == audio_lead,
                NextValue(audio_count, 0),
                NextState("ADATA"),
            )
        )
        fsm.act("ADATA",
            asel.eq(1),
            valid.eq(head_valid),
            audio_source.ready.eq(head_valid),
            If(head_valid,
                NextValue(audio_count, audio_count + 1),
                If(audio_source.last | (audio_count == (audio_packet_words - 1)),
                    NextValue(gcount, 0),
                    NextValue(audio_sent, audio_sent + 1),
                    NextValue(last_audio, 1),
                    NextState("AGUARD"),
                )
            )
        )
        fsm.act("AGUARD",
            NextValue(gcount, gcount + 1),
            If(gcount == guard,
                NextValue(gcount, 0),
                NextState("WAIT"),
            )
        )
        fsm.act("GUARD",
            NextValue(gcount, gcount + 1),
            If(gcount == guard,
                NextState("WAIT"),
            )
        )
        self.specials += MultiReg(bursts, self._bursts.status)
        self.specials += MultiReg(audio_sent, self._audio_packets.status)
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
        # DQ output register without reset: while the streaming logic is held in reset (GPIF
        # restart), DQ already presents the next video word (next UVC header word), which the FX3
        # captures as the first word of its first DMA buffer.
        self.sync.gpif_cdc += [
            If(force,
                pads.dq.eq(force_value),
            ).Elif(fsm.ongoing("EOP"),
                # The FX3 latches DQ at commit and uses it as the first word of its next buffer.
                pads.dq.eq(video_next),
            ).Elif(dq_cycles & fsm.ongoing("WAIT"),
                pads.dq.eq(0x80000000 | cycles),
            ).Else(
                pads.dq.eq(data_o),
            ),
        ]
        self.sync.gpif += [
            ctl.o[0].eq(valid),
            ctl.o[2].eq(eop),
            ctl.o[3].eq(asel),
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
