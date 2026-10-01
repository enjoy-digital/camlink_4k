#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""NV12 frame buffer: M420 frames (HDMI) -> DRAM frame slots (NV12 planes) -> NV12 frames (UVC).

NV12 sends the whole Y plane then the whole interleaved CbCr plane, while the HDMI input produces
them interleaved line by line (M420: even Y line, odd Y line, CbCr line of the pair). Each input
frame is written to a DRAM slot with the Y lines in the Y plane and the CbCr lines in the UV plane;
the reader streams the latest complete slot linearly (Y plane then UV plane = NV12).

Slots: `nslots` (3) slots of `slot_words` port words from `base`. The writer never overwrites the
slot being read nor the latest complete slot; a frame is only published (made readable) when it
has exactly `frame_words` words (truncated frames, e.g. input FIFO overflow, are dropped). The
reader starts a frame when a new frame was published since its last start (frames are skipped
when the output is slower than the input, output frames wait for the input otherwise).

Stop (`enable` CSR low or `stop` input high): the input is dropped, the buffered writes complete and
the reader drains its outstanding reads (never reset the DMAs with DRAM accesses in flight: the
crossbar does not handshake write/read data, a reset would desynchronize the port data FIFOs).

DRAM accesses are issued in bursts of `burst` port words (writes: when `burst` words are buffered
or at the frame end, reads: when `burst` reservations are free): with single accesses (reader paced
by the USB output), the crossbar re-granted the banks between the writer and the reader at almost
every access (row switches, read/write turnarounds): 3.5 frames/s at 4K on hardware.

Geometry (CSRs, port words = `data_width` bits): `line_words` port words per line (W/16 for
128-bit ports), `height` lines (even), `uv_offset` = UV plane offset in the slot (= height x
line_words), `frame_words` 32-bit words per frame (W x H x 3/8).
"""

from migen import *

from litex.gen import *

from migen.genlib.cdc import MultiReg

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

from litedram.frontend.dma import LiteDRAMDMAWriter, LiteDRAMDMAReader

# NV12 Frame Buffer --------------------------------------------------------------------------------

class NV12FrameBuffer(LiteXModule):
    def __init__(self, write_port, read_port, nslots=3, burst=64, rd_depth=256):
        assert write_port.data_width == read_port.data_width
        dw    = write_port.data_width
        ratio = dw//32
        aw    = write_port.address_width
        self.sink   = sink   = stream.Endpoint([("data", 32)])
        self.source = source = stream.Endpoint([("data", 32)])
        self.stop   = stop   = Signal()

        self.enable      = CSRStorage(description="Enable (frame buffer state reset when disabled).")
        self.base        = CSRStorage(aw, description="Slot 0 address (port words).")
        self.slot_words  = CSRStorage(aw, description="Slot size (port words).")
        self.line_words  = CSRStorage(16, description="Port words per line.")
        self.height      = CSRStorage(16, description="Lines per frame (even).")
        self.uv_offset   = CSRStorage(aw, description="UV plane offset in a slot (port words).")
        self.frame_words = CSRStorage(32, description="32-bit words per frame (W x H x 3/8).")
        self.written     = CSRStatus(32, description="Frames written (published).")
        self.dropped     = CSRStatus(32, description="Input frames dropped (truncated).")
        self.read        = CSRStatus(32, description="Frames read.")
        self.in_stalls   = CSRStatus(32, description="Debug: cycles with input data not accepted.")
        self.wr_stalls   = CSRStatus(32, description="Debug: cycles with a write waiting for the DRAM port.")
        self.debug       = CSRStatus(32, description="Debug: reader state.")

        # # #

        enable     = Signal()
        enable_csr = Signal()
        self.specials += MultiReg(self.enable.storage, enable_csr)
        self.comb += enable.eq(enable_csr & ~stop)

        # Slot bases (registered sums of static CSRs).
        slot_base = Array(Signal(aw, name=f"slot_base{i}", reset_less=True) for i in range(nslots))
        for i in range(nslots):
            self.sync += slot_base[i].eq(self.base.storage + i*self.slot_words.storage)

        # Slot state.
        last_slot = Signal(max=nslots)     # Latest complete slot.
        have_last = Signal()               # A complete slot exists.
        rd_slot   = Signal(max=nslots)     # Slot being read.
        reading   = Signal()
        published = Signal(32)             # Frames published (also the reader's new frame marker).
        dropped   = Signal(32)
        read      = Signal(32)
        in_stalls = Signal(32)
        wr_stalls = Signal(32)
        self.comb += [
            self.in_stalls.status.eq(in_stalls),
            self.wr_stalls.status.eq(wr_stalls),
            self.written.status.eq(published),
            self.dropped.status.eq(dropped),
            self.read.status.eq(read),
        ]

        # Writer -----------------------------------------------------------------------------------
        self.writer = writer = LiteDRAMDMAWriter(write_port, fifo_depth=32)
        # Write buffer: the DMA writer does not buffer commands (a DRAM port stall, e.g. refresh or
        # row switch, stalled the input at once: ~5% of the cycles on hardware, too much for 4K30
        # M420 at ~97M words/s from a ~99MHz video clock). Absorbs short stalls (the DRAM write
        # bandwidth is ~2.5x the need).
        self.wr_fifo = wr_fifo = stream.SyncFIFO([("address", aw), ("data", dw)], 256, buffered=True)
        wr_burst = Signal(max=burst + 1) # Words left in the current write burst.
        self.comb += [
            wr_fifo.source.connect(writer.sink, omit={"valid", "ready"}),
            writer.sink.valid.eq(wr_fifo.source.valid & (wr_burst != 0)),
            wr_fifo.source.ready.eq(writer.sink.ready & (wr_burst != 0)),
        ]

        # 32 -> port width packing (first 32-bit word in the LSBs = lowest address).
        pack      = Signal(dw)
        pack_n    = Signal(max=ratio)
        pack_full = Signal()
        wr_slot   = Signal(max=nslots)
        in_frame  = Signal() # Writing a frame (started on an input frame start).
        wleft     = Signal(32) # 32-bit words left in the current frame (after this one).
        x         = Signal(16) # Port word in line.
        sub       = Signal(2)  # 0: even Y line, 1: odd Y line, 2: UV line.
        y_addr    = Signal(aw) # Current Y line start.
        uv_addr   = Signal(aw) # Current UV line start.
        wr_data   = Signal(dw)
        wr_valid  = Signal()
        wr_addr   = Signal(aw)
        frame_end = Signal() # Input frame end (last 32-bit word accepted).
        frame_ok  = Signal()

        # Next write slot: not the one being read, not the latest complete one.
        next_slot = Signal(max=nslots)
        self.comb += next_slot.eq(0)
        for i in reversed(range(nslots)):
            self.comb += If(~((reading & (rd_slot == i)) | (have_last & (last_slot == i))), next_slot.eq(i))

        # Input: accepted when the write buffer is free; a frame start waits for the previous frame
        # to be published (its slot is then excluded from the next slot choice).
        self.comb += [
            sink.ready.eq((~wr_valid | wr_fifo.sink.ready) & ~(sink.first & frame_end)),
            If(~enable, sink.ready.eq(1)), # Dropped.
        ]
        self.sync += [
            If(wr_fifo.sink.valid & wr_fifo.sink.ready, wr_valid.eq(0)),
            If(sink.valid & sink.ready & enable,
                If(sink.first,
                    # Frame start: new slot (a frame in progress without its end is dropped).
                    If(in_frame, dropped.eq(dropped + 1)),
                    in_frame.eq(1),
                    wr_slot.eq(next_slot),
                    y_addr.eq(slot_base[next_slot]),
                    uv_addr.eq(slot_base[next_slot] + self.uv_offset.storage),
                    x.eq(0),
                    sub.eq(0),
                    wleft.eq(self.frame_words.storage - 1),
                    pack.eq(Cat(Signal(dw - 32), sink.data)),
                    pack_n.eq(1 % ratio),
                ).Elif(in_frame,
                    wleft.eq(wleft - 1),
                    pack.eq(Cat(pack[32:], sink.data)),
                    pack_n.eq(Mux(pack_n == (ratio - 1), 0, pack_n + 1)),
                ),
                If((sink.first | in_frame) & (Mux(sink.first, 0, pack_n) == (ratio - 1)),
                    # Full port word: write it, advance the line position.
                    wr_valid.eq(1),
                    wr_data.eq(Cat(pack[32:], sink.data)),
                    wr_addr.eq(Mux(sub == 2, uv_addr, y_addr) + x),
                    If(x == (self.line_words.storage - 1),
                        x.eq(0),
                        If(sub == 2,
                            sub.eq(0),
                            uv_addr.eq(uv_addr + self.line_words.storage),
                        ).Else(
                            sub.eq(sub + 1),
                            y_addr.eq(y_addr + self.line_words.storage),
                        )
                    ).Else(
                        x.eq(x + 1),
                    )
                ),
                If(sink.last & (sink.first | in_frame),
                    in_frame.eq(0),
                    frame_end.eq(1),
                    frame_ok.eq(Mux(sink.first, self.frame_words.storage == 1, wleft == 1)),
                )
            ),
            # Publish the frame once all its words are handed to the DMA writer (write buffer empty).
            If(frame_end & ~wr_valid & ~wr_fifo.source.valid & (wr_fifo.level == 0),
                frame_end.eq(0),
                If(frame_ok,
                    last_slot.eq(wr_slot),
                    have_last.eq(1),
                    published.eq(published + 1),
                ).Else(
                    dropped.eq(dropped + 1),
                )
            ),
            If(~enable,
                # Pending buffered writes still complete (wr_fifo -> DMA writer).
                in_frame.eq(0),
                frame_end.eq(0),
                have_last.eq(0),
            )
        ]
        # Write bursts: `burst` buffered words, or the rest at the frame end (not in a frame).
        self.sync += [
            If(wr_burst == 0,
                If(wr_fifo.level >= burst,
                    wr_burst.eq(burst),
                ).Elif((wr_fifo.level != 0) & ~in_frame,
                    wr_burst.eq(wr_fifo.level),
                )
            ).Elif(writer.sink.valid & writer.sink.ready,
                wr_burst.eq(wr_burst - 1),
            )
        ]
        self.sync += [
            If(sink.valid & ~sink.ready, in_stalls.eq(in_stalls + 1)),
            If(wr_fifo.sink.valid & ~wr_fifo.sink.ready, wr_stalls.eq(wr_stalls + 1)),
        ]
        self.comb += [
            wr_fifo.sink.valid.eq(wr_valid),
            wr_fifo.sink.address.eq(wr_addr),
            wr_fifo.sink.data.eq(wr_data),
        ]

        # Reader -----------------------------------------------------------------------------------
        self.reader = reader = LiteDRAMDMAReader(read_port, fifo_depth=rd_depth) # Outstanding reads: absorbs DRAM stalls.

        seen     = Signal(32) # Published count at the last read start.
        rd_addr  = Signal(aw)
        rd_left  = Signal(aw) # Port words left to request.
        out_left = Signal(32) # 32-bit words left to output.
        out_word = Signal(dw)
        out_n    = Signal(max=ratio)
        out_have  = Signal()
        out_first = Signal()
        out_next  = Signal() # Last 32-bit word of the current port word output.
        pending   = Signal(max=rd_depth + 2) # Reads requested, data not received yet.
        rd_issue  = Signal() # Registered request/data strobes (timing: the output ready chain).
        rd_done   = Signal()
        rd_burst  = Signal(max=burst + 1) # Reads left in the current read burst.
        fsm_read  = Signal()
        self.sync += [
            rd_issue.eq(reader.sink.valid & reader.sink.ready),
            rd_done.eq(reader.source.valid & reader.source.ready),
            pending.eq(pending + rd_issue - rd_done),
        ]
        # Read bursts: started when `burst` reservations are free.
        self.sync += [
            If(rd_burst == 0,
                If(fsm_read & (rd_left != 0) & (pending <= (rd_depth - burst)),
                    rd_burst.eq(Mux(rd_left < burst, rd_left, burst)),
                )
            ).Elif(reader.sink.valid & reader.sink.ready,
                rd_burst.eq(rd_burst - 1),
            ),
            If(~enable, rd_burst.eq(0)),
        ]

        self.fsm = fsm = FSM(reset_state="IDLE")
        self.comb += fsm_read.eq(fsm.ongoing("READ"))
        fsm.act("IDLE",
            If(enable & have_last & (published != seen),
                NextValue(seen, published),
                NextValue(rd_slot, last_slot),
                NextValue(reading, 1),
                NextValue(rd_addr, slot_base[last_slot]),
                NextValue(rd_left, self.frame_words.storage[log2_int(ratio):]),
                NextValue(out_left, self.frame_words.storage),
                NextValue(out_first, 1),
                NextState("READ"),
            )
        )
        fsm.act("READ",
            # Requests (frame words only: the slot may be larger than the frame).
            reader.sink.valid.eq(rd_burst != 0),
            reader.sink.address.eq(rd_addr),
            If(reader.sink.valid & reader.sink.ready,
                NextValue(rd_addr, rd_addr + 1),
                NextValue(rd_left, rd_left - 1),
            ),
            # Port words -> 32-bit words (full throughput: the next port word is loaded while the
            # last 32-bit word of the current one is output).
            source.valid.eq(out_have),
            source.data.eq(out_word[:32]),
            source.first.eq(out_first),
            source.last.eq(out_left == 1),
            out_next.eq(source.valid & source.ready & (out_n == (ratio - 1))),
            reader.source.ready.eq(~out_have | out_next),
            If(source.valid & source.ready,
                NextValue(out_first, 0),
                NextValue(out_left, out_left - 1),
                NextValue(out_word, out_word[32:]),
                NextValue(out_n, out_n + 1),
                If(out_next, NextValue(out_have, 0)),
            ),
            If(reader.source.valid & reader.source.ready,
                NextValue(out_word, reader.source.data),
                NextValue(out_n, 0),
                NextValue(out_have, 1),
            ),
            If(source.valid & source.ready & (out_left == 1),
                NextValue(out_have, 0),
                NextValue(read, read + 1),
                NextState("FLUSH"),
            ),
            If(~enable,
                NextValue(out_have, 0),
                NextState("FLUSH"),
            ),
        )
        self.comb += self.debug.status.eq(Cat(
            fsm.ongoing("IDLE"), fsm.ongoing("READ"), fsm.ongoing("FLUSH"), have_last, reading,
            rd_left != 0, reader.sink.valid, reader.sink.ready, reader.source.valid, reader.source.ready,
            source.valid, source.ready, out_have, enable, pending != 0, stop, last_slot, Signal(2),
            rd_left[:8]))
        fsm.act("FLUSH",
            # Frame done or stopped: drain the outstanding reads, then release the slot.
            reader.source.ready.eq(1),
            If((pending == 0) & ~rd_issue & ~rd_done,
                NextValue(reading, 0),
                NextState("IDLE"),
            )
        )
