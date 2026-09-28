#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""HDMI In: IT6802 parallel video capture (YUV 4:2:2 16-bit, SDR or DDR) to a YUY2 frame stream.

The IT6802 is configured to output YUV 4:2:2 8-bit (16-bit bus): each pixel carries Y and one
chroma sample (Cb on even pixels, Cr on odd ones). Two pixels are packed into a 32-bit YUY2 word
(Y0 Cb Y1 Cr, little endian). The Y/C byte lanes (QE[11:4], QE[23:16], QE[35:28]) are selectable.

QE/DE are captured with DDR input registers: SDR modes (up to 1080p60) use the rising edge sample,
the 0.5x PCLK DDR modes (4K) provide a pixel pair per clock. An optional 2x downscaler (2x2 box
filter: horizontal luma/chroma averaging, even lines kept in a line buffer and averaged with the
odd ones) turns 3840x2160 into 1920x1080.

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

# M420 Packer --------------------------------------------------------------------------------------

class M420Packer(LiteXModule):
    """YUV 4:2:2 pixel pairs -> M420 (YUV 4:2:0: 2 lines of Y, then 1 line of interleaved CbCr).

    Per pixel pair (one per clock at most): Y0/Y1 and the pair chroma (U, V). Y words (4 luma
    samples) of all lines go through a line FIFO; the chroma of even lines is kept in a pair buffer
    and averaged with the odd line into a (double buffered) UV line. The output sequencer emits the
    even Y line, the odd Y line, then the UV line of the pair. Lines must have an even number of
    pairs. `restart` (VSYNC) resets the packer; `last_pair` marks the last line pair of the frame.
    """
    def __init__(self, max_pairs=1920, y_fifo_depth=1024):
        self.valid     = Signal()   # Active pixel pair.
        self.ya        = Signal(8)
        self.yb        = Signal(8)
        self.u         = Signal(8)
        self.v         = Signal(8)
        self.odd_line  = Signal()
        self.eol       = Signal()   # Last pair of the line.
        self.sof       = Signal()   # First pair of the frame.
        self.last_pair = Signal()   # Current (odd) line is the last line of the frame.
        self.source    = source = stream.Endpoint([("data", 32)])

        # # #

        max_words = max_pairs//2

        # Y packing (2 pairs -> 1 word) -> line FIFO (data + end of line + start of frame).
        y_layout    = [("data", 32), ("eol", 1), ("sof", 1)]
        self.y_fifo = y_fifo = stream.SyncFIFO(y_layout, y_fifo_depth, buffered=True)
        self.y_buf  = y_buf  = stream.Buffer(y_layout) # Register stage (timing: BRAM -> sequencer).
        self.comb += y_fifo.source.connect(y_buf.sink)
        y_half   = Signal()
        y_first  = Signal(16)
        y_sof    = Signal()
        self.sync += [
            y_fifo.sink.valid.eq(0),
            If(self.valid,
                y_half.eq(~y_half),
                If(~y_half,
                    y_first.eq(Cat(self.ya, self.yb)),
                    y_sof.eq(self.sof),
                ).Else(
                    y_fifo.sink.valid.eq(1),
                    y_fifo.sink.data.eq(Cat(y_first, self.ya, self.yb)),
                    y_fifo.sink.eol.eq(self.eol),
                    y_fifo.sink.sof.eq(y_sof),
                ),
                If(self.eol, y_half.eq(0)),
            ),
        ]

        # Chroma: even lines -> pair buffer; odd lines -> vertical average -> UV line (2 banks).
        px     = Signal(max=max_pairs)
        cbuf   = Memory(16, max_pairs)
        cbuf_w = cbuf.get_port(write_capable=True)
        cbuf_r = cbuf.get_port(mode=READ_FIRST)
        self.specials += cbuf, cbuf_w, cbuf_r
        self.comb += [
            cbuf_w.adr.eq(px),
            cbuf_w.dat_w.eq(Cat(self.u, self.v)),
            cbuf_w.we.eq(self.valid & ~self.odd_line),
            cbuf_r.adr.eq(px),
        ]
        self.sync += If(self.valid, px.eq(Mux(self.eol, 0, px + 1)))

        # Odd line: pair delayed by 2 cycles (buffer read + registered BRAM output), averages.
        p_valid = Signal()
        p_eol   = Signal()
        p_last  = Signal()
        p_u     = Signal(8)
        p_v     = Signal(8)
        o_valid = Signal()
        o_eol   = Signal()
        o_last  = Signal()
        o_u     = Signal(8)
        o_v     = Signal(8)
        o_c     = Signal(16)
        self.sync += [
            p_valid.eq(self.valid & self.odd_line),
            p_eol.eq(self.eol),
            p_last.eq(self.last_pair),
            p_u.eq(self.u),
            p_v.eq(self.v),
            o_valid.eq(p_valid),
            o_eol.eq(p_eol),
            o_last.eq(p_last),
            o_u.eq(p_u),
            o_v.eq(p_v),
            o_c.eq(cbuf_r.dat_r),
        ]
        su = Signal(9)
        sv = Signal(9)
        self.comb += [
            su.eq(o_u + o_c[0:8]  + 1),
            sv.eq(o_v + o_c[8:16] + 1),
        ]
        bank_words = 2**bits_for(max_words - 1)
        uvbuf   = Memory(32, 2*bank_words)
        uvbuf_w = uvbuf.get_port(write_capable=True)
        uvbuf_r = uvbuf.get_port(has_re=True, mode=READ_FIRST)
        self.specials += uvbuf, uvbuf_w, uvbuf_r
        uv_half  = Signal()
        uv_first = Signal(16)
        uv_x     = Signal(bits_for(bank_words - 1))
        wbank    = Signal()
        uv_len   = Array(Signal(max=max_words + 1) for _ in range(2))
        uv_last  = Array(Signal() for _ in range(2))
        uv_ready = Array(Signal() for _ in range(2)) # UV line complete (bank), cleared when emitted.
        uv_done  = Signal()                           # Sequencer: UV line emitted (clears ready).
        rbank    = Signal()
        self.comb += [
            uvbuf_w.adr.eq(Cat(uv_x, wbank)),
            uvbuf_w.dat_w.eq(Cat(uv_first, su[1:], sv[1:])),
            uvbuf_w.we.eq(o_valid & uv_half),
        ]
        self.sync += [
            If(o_valid,
                uv_half.eq(~uv_half),
                If(~uv_half,
                    uv_first.eq(Cat(su[1:], sv[1:])),
                ).Else(
                    uv_x.eq(uv_x + 1),
                ),
                If(o_eol,
                    uv_half.eq(0),
                    uv_x.eq(0),
                    uv_len[wbank].eq(uv_x + uv_half),
                    uv_last[wbank].eq(o_last),
                    uv_ready[wbank].eq(1),
                    wbank.eq(~wbank),
                ),
            ),
            If(uv_done, uv_ready[rbank].eq(0), rbank.eq(~rbank)),
        ]

        # Output sequencer: Y even line, Y odd line, UV line.
        out_odd  = Signal() # Y line being emitted is odd.
        rd_i     = Signal(max=max_words + 1)
        rd_idx   = Signal(max=max_words + 1) # Index of the UV word on dat_r.
        uv_valid = Signal()
        advance  = Signal()
        self.fsm = fsm = FSM(reset_state="Y")
        fsm.act("Y",
            source.valid.eq(y_buf.source.valid),
            source.data.eq(y_buf.source.data),
            source.first.eq(y_buf.source.sof),
            y_buf.source.ready.eq(source.ready),
            If(y_buf.source.valid & source.ready,
                If(y_buf.source.sof, NextValue(out_odd, 0)),
                If(y_buf.source.eol,
                    If(out_odd,
                        NextValue(out_odd, 0),
                        NextValue(rd_i, 0),
                        NextState("UV_WAIT"),
                    ).Else(
                        NextValue(out_odd, 1),
                    )
                )
            )
        )
        fsm.act("UV_WAIT",
            If(uv_ready[rbank], NextState("UV"))
        )
        # UV read pipeline: BRAM (read enable = advance) -> output register (timing: plain enabled
        # register on the BRAM output).
        uv_q_en = Signal()
        uv_valid0 = Signal() # Word on dat_r.
        uv_idx0   = Signal(max=max_words + 1)
        uv_q      = Signal(32)
        self.comb += [
            advance.eq(~uv_valid | source.ready),
            uvbuf_r.adr.eq(Cat(rd_i[:len(uv_x)], rbank)),
            uvbuf_r.re.eq(advance & fsm.ongoing("UV")),
            uv_q_en.eq(advance & fsm.ongoing("UV")),
        ]
        self.sync += If(uv_q_en, uv_q.eq(uvbuf_r.dat_r))
        fsm.act("UV",
            source.valid.eq(uv_valid),
            source.data.eq(uv_q),
            source.last.eq(uv_last[rbank] & (rd_idx == (uv_len[rbank] - 1))),
            If(advance,
                NextValue(uv_valid, uv_valid0),
                NextValue(rd_idx,   uv_idx0),
                If(rd_i < uv_len[rbank],
                    NextValue(uv_valid0, 1),
                    NextValue(uv_idx0,   rd_i),
                    NextValue(rd_i,      rd_i + 1),
                ).Else(
                    NextValue(uv_valid0, 0),
                    If(~uv_valid0,
                        uv_done.eq(1),
                        NextState("Y"),
                    )
                )
            )
        )

# HDMI In ------------------------------------------------------------------------------------------

class HDMIIn(LiteXModule):
    def __init__(self, pads, fifo_depth=2048, max_line_words=1024, idle_timeout=2**20, sim=False):
        self.source = source = stream.Endpoint([("data", 32)])

        self.control = CSRStorage(fields=[
            CSRField("enable",  size=1, offset=0, description="Enable capture."),
            CSRField("y_lane",  size=2, offset=4, reset=1, description="Y byte lane (0: QE[11:4], 1: QE[23:16], 2: QE[35:28])."),
            CSRField("c_lane",  size=2, offset=6, reset=0, description="C byte lane."),
            CSRField("c_swap",  size=1, offset=8, description="Swap Cb/Cr order."),
            CSRField("ddr",       size=1, offset=12, description="DDR input (2 pixels per clock, IT6802 0.5x PCLK modes, 4K)."),
            CSRField("ddr_swap",  size=1, offset=13, description="DDR: falling edge carries the first pixel."),
            CSRField("downscale", size=1, offset=14, description="2x downscale (2x2 box filter, DDR modes)."),
            CSRField("crop",      size=1, offset=15, description="Crop window (DDR modes, no downscale)."),
            CSRField("m420",      size=1, offset=16, description="M420 output (YUV 4:2:0, DDR modes, no downscale/crop)."),
        ])
        self.crop_x = CSRStorage(16, description="Crop window X (words, 2 pixels per word).")
        self.crop_y = CSRStorage(16, description="Crop window Y (lines).")
        self.crop_w = CSRStorage(16, reset=960,  description="Crop window width (words).")
        self.crop_h = CSRStorage(16, reset=1080, description="Crop window height (lines).")
        self.admit_level = CSRStorage(16, reset=fifo_depth//2, description="Minimum free FIFO words to admit a frame.")
        self.hres     = CSRStatus(16, description="Measured active width (pixels).")
        self.vres     = CSRStatus(16, description="Measured active height (lines).")
        self.frames   = CSRStatus(32, description="Captured frames.")
        self.dropped  = CSRStatus(32, description="Dropped frames (not admitted).")
        self.overflow = CSRStatus(32, description="Words lost on FIFO overflow.")
        self.aborted  = CSRStatus(32, description="Frames closed on input loss (idle timeout).")
        self.frame_period = CSRStatus(32, description="Input frame period (sys clock cycles, VSYNC to VSYNC).")

        # # #

        # Clock domain (IT6802 PCLK).
        self.cd_hdmi = ClockDomain()
        self.comb += self.cd_hdmi.clk.eq(pads.pclk)

        # Control (CDC).
        enable    = Signal()
        y_lane    = Signal(2)
        c_lane    = Signal(2)
        c_swap    = Signal()
        ddr       = Signal()
        ddr_swap  = Signal()
        downscale = Signal()
        self.specials += [
            MultiReg(self.control.fields.enable,    enable,    "hdmi"),
            MultiReg(self.control.fields.y_lane,    y_lane,    "hdmi"),
            MultiReg(self.control.fields.c_lane,    c_lane,    "hdmi"),
            MultiReg(self.control.fields.c_swap,    c_swap,    "hdmi"),
            MultiReg(self.control.fields.ddr,       ddr,       "hdmi"),
            MultiReg(self.control.fields.ddr_swap,  ddr_swap,  "hdmi"),
            MultiReg(self.control.fields.downscale, downscale, "hdmi"),
        ]
        m420    = Signal()
        self.specials += MultiReg(self.control.fields.m420, m420, "hdmi")
        crop    = Signal()
        crop_x0 = Signal(16)
        crop_x1 = Signal(16)
        crop_y0 = Signal(16)
        crop_y1 = Signal(16)
        crop_x0m1   = Signal(16)
        crop_x1_sys = Signal(16)
        crop_y1_sys = Signal(16)
        crop_x0m1_sys = Signal(16)
        self.comb += [
            crop_x0m1_sys.eq(self.crop_x.storage - 1),
            crop_x1_sys.eq(self.crop_x.storage + self.crop_w.storage - 1),
            crop_y1_sys.eq(self.crop_y.storage + self.crop_h.storage - 1),
        ]
        self.specials += [
            MultiReg(self.control.fields.crop, crop,    "hdmi"),
            MultiReg(self.crop_x.storage,      crop_x0, "hdmi"),
            MultiReg(self.crop_y.storage,      crop_y0, "hdmi"),
            MultiReg(crop_x1_sys,              crop_x1, "hdmi"),
            MultiReg(crop_x0m1_sys,            crop_x0m1, "hdmi"),
            MultiReg(crop_y1_sys,              crop_y1, "hdmi"),
        ]

        # Inputs: DDR input registers on QE/DE (Q0: rising edge, Q1: falling edge), VS registered.
        qe_r  = Signal(24) # Rising edge sample.
        qe_f  = Signal(24) # Falling edge sample.
        de_r  = Signal()
        vs    = Signal()
        if sim:
            self.sync.hdmi += [qe_r.eq(pads.qe), qe_f.eq(pads.qe_fall), de_r.eq(pads.de)]
        else:
            for i in range(24):
                self.specials += Instance("IDDRX1F",
                    i_D    = pads.qe[i],
                    i_SCLK = ClockSignal("hdmi"),
                    i_RST  = 0,
                    o_Q0   = qe_r[i],
                    o_Q1   = qe_f[i],
                )
            self.specials += Instance("IDDRX1F",
                i_D    = pads.de,
                i_SCLK = ClockSignal("hdmi"),
                i_RST  = 0,
                o_Q0   = de_r,
            )
        self.sync.hdmi += vs.eq(pads.vsync)

        # Pipeline alignment (sample stage): pixel data/DE of the current clock.
        qe0 = Signal(24) # First pixel (SDR: the pixel, DDR: first of the pair).
        qe1 = Signal(24) # Second pixel (DDR only).
        de  = Signal()
        de_next = Signal()
        self.sync.hdmi += [
            qe0.eq(Mux(ddr_swap, qe_f, qe_r)),
            qe1.eq(Mux(ddr_swap, qe_r, qe_f)),
            de.eq(de_r),
        ]
        self.comb += de_next.eq(de_r)
        self.debug_qe = qe0 # Debug (IOScan).
        self.debug_de = de

        # Lanes.
        def lanes(qe):
            y = Signal(8)
            c = Signal(8)
            self.comb += [
                Case(y_lane, {0: y.eq(qe[0:8]), 1: y.eq(qe[8:16]), "default": y.eq(qe[16:24])}),
                Case(c_lane, {0: c.eq(qe[0:8]), 1: c.eq(qe[8:16]), "default": c.eq(qe[16:24])}),
            ]
            return y, c
        ya, ca = lanes(qe0)
        yb, cb = lanes(qe1)

        # Timing measurement (DE/VS): hres in DE clocks (pixels in SDR, pixel pairs in DDR).
        de_d     = Signal()
        vs_d     = Signal()
        x        = Signal(16) # DE clock in line.
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

        # Word generation: YUY2 word = Y0 | C0 << 8 | Y1 << 16 | C1 << 24.
        # - SDR: two consecutive pixels per word.
        # - DDR: the pixel pair of each clock is a word.
        # - Downscale (2x, DDR): pairs of words are averaged into one word; even lines are stored in
        #   a line buffer and averaged with the following odd line, which is emitted.
        # Line-based conditions are registered (constant during a line, the line counter changes in
        # the horizontal blanking).
        last_line = Signal(16)
        is_last_line    = Signal()
        crop_line       = Signal()
        crop_first_line = Signal()
        crop_last_line  = Signal()
        # Crop window X membership (x counts up during DE): set one clock before x0, cleared after x1.
        crop_in_x = Signal()
        self.sync.hdmi += [
            If(~de,
                crop_in_x.eq(crop_x0 == 0),
            ).Else(
                If(x == crop_x0m1, crop_in_x.eq(1)),
                If(x == crop_x1,   crop_in_x.eq(0)),
            ),
        ]
        self.sync.hdmi += [
            last_line.eq(vres - 1 - (downscale & vres[0])),
            is_last_line.eq(line == last_line),
            crop_line.eq((line >= crop_y0) & (line <= crop_y1)),
            crop_first_line.eq(line == crop_y0),
            crop_last_line.eq(line == crop_y1),
        ]
        active    = Signal()
        capture_ok = Signal() # Registered (static during a frame): capture enabled, height known.
        self.sync.hdmi += capture_ok.eq(enable & (vres != 0))
        self.comb += active.eq(de & capture_ok)
        odd_line  = Signal()
        self.comb += odd_line.eq(line[0])

        w_valid = Signal()
        w_data  = Signal(32)
        w_first = Signal()
        w_last  = Signal()
        end_of_frame = Signal()
        self.comb += end_of_frame.eq(is_last_line & ~de_next)

        y0   = Signal(8)
        c0   = Signal(8)
        c1   = Signal(8)
        odd  = Signal()
        # Averages use explicit 9-bit sums (Verilog would size (a + b) >> 1 to 8 bits).
        def avg(a, b, rnd=0):
            s = Signal(9)
            r = Signal(8)
            self.comb += [s.eq(a + b + rnd), r.eq(s[1:])]
            return r
        yavg = avg(ya, yb) # Averaged luma of the current pixel pair.

        # Downscale: horizontal 2x word (averaged luma of each pair, averaged Cb/Cr of both pairs),
        # line buffer (even lines) and vertical average (odd lines).
        hword  = Signal(32)
        self.comb += hword.eq(Cat(y0, avg(c0, ca), yavg, avg(c1, cb)))
        wx     = Signal(max=max_line_words)
        lbuf   = Memory(32, max_line_words)
        lbuf_w = lbuf.get_port(write_capable=True, clock_domain="hdmi")
        lbuf_r = lbuf.get_port(clock_domain="hdmi", mode=READ_FIRST) # No write bypass (timing).
        self.specials += lbuf, lbuf_w, lbuf_r
        # Stage H (second pair): registered horizontal word, line buffer read issued.
        h_valid    = Signal()
        h_word     = Signal(32)
        h_wx       = Signal(max=max_line_words)
        h_odd_line = Signal()
        h_first    = Signal()
        h_last     = Signal()
        h_stage    = Signal()
        self.comb += h_stage.eq(active & ddr & downscale & odd)
        self.sync.hdmi += [
            h_valid.eq(h_stage),
            If(h_stage,
                h_word.eq(hword),
                h_wx.eq(wx),
                h_odd_line.eq(odd_line),
                h_first.eq((line == 1) & (x == 1)),
                h_last.eq(end_of_frame),
            ),
            If(~active, wx.eq(0)).Elif(h_stage, wx.eq(wx + 1)),
        ]
        # Stage V: even lines -> line buffer, odd lines -> registered stored word (BRAM output)...
        self.comb += [
            lbuf_r.adr.eq(wx),
            lbuf_w.adr.eq(h_wx),
            lbuf_w.dat_w.eq(h_word),
            lbuf_w.we.eq(h_valid & ~h_odd_line),
        ]
        v_valid = Signal()
        v_word  = Signal(32)
        v_mem   = Signal(32)
        v_first = Signal()
        v_last  = Signal()
        self.sync.hdmi += [
            v_valid.eq(h_valid & h_odd_line),
            v_word.eq(h_word),
            v_mem.eq(lbuf_r.dat_r),
            v_first.eq(h_first),
            v_last.eq(h_last),
        ]
        # ... and vertical average (emitted on the next clock).
        vword = Signal(32)
        self.comb += vword.eq(Cat(*[avg(v_word[8*i:8*(i+1)], v_mem[8*i:8*(i+1)], rnd=1) for i in range(4)]))
        self.sync.hdmi += [
            w_valid.eq(0),
            If(active,
                odd.eq(~odd),
                If(ddr,
                    If(crop,
                        # Crop window: native pixels inside [x0, x1] x [y0, y1].
                        If(crop_in_x & crop_line,
                            w_valid.eq(1),
                            w_data.eq(Mux(c_swap, Cat(ya, cb, yb, ca), Cat(ya, ca, yb, cb))),
                            w_first.eq(crop_first_line & (x == crop_x0)),
                            w_last.eq(crop_last_line & (x == crop_x1)),
                        )
                    ).Elif(~downscale,
                        w_valid.eq(1),
                        w_data.eq(Mux(c_swap, Cat(ya, cb, yb, ca), Cat(ya, ca, yb, cb))),
                        w_first.eq((line == 0) & (x == 0)),
                        w_last.eq(end_of_frame),
                    ).Elif(~odd,
                        # First pair: averaged luma + chroma.
                        y0.eq(yavg),
                        c0.eq(ca),
                        c1.eq(cb),
                    )
                ).Else(
                    If(~odd,
                        y0.eq(ya),
                        c0.eq(ca),
                    ).Else(
                        w_valid.eq(1),
                        w_data.eq(Mux(c_swap, Cat(y0, ca, ya, c0), Cat(y0, c0, ya, ca))),
                        w_first.eq((line == 0) & (x == 1)),
                        w_last.eq(end_of_frame),
                    )
                )
            ).Else(
                odd.eq(0),
            ),
            # Downscale output (stage V, odd lines).
            If(v_valid,
                w_valid.eq(1),
                w_data.eq(Mux(c_swap, Cat(vword[0:8], vword[24:32], vword[16:24], vword[8:16]), vword)),
                w_first.eq(v_first),
                w_last.eq(v_last),
            ),
        ]
        # M420 packer (4:2:0 line pairs, reset on VSYNC).
        self.m420 = m420_packer = ClockDomainsRenamer("hdmi")(ResetInserter()(M420Packer()))
        self.comb += [
            m420_packer.reset.eq(vs_start | ~m420 | ~enable),
            m420_packer.valid.eq(active & ddr & m420),
            m420_packer.ya.eq(ya),
            m420_packer.yb.eq(yb),
            m420_packer.u.eq(Mux(c_swap, cb, ca)),
            m420_packer.v.eq(Mux(c_swap, ca, cb)),
            m420_packer.odd_line.eq(line[0]),
            m420_packer.eol.eq(~de_next),
            m420_packer.sof.eq((line == 0) & (x == 0)),
            m420_packer.last_pair.eq(is_last_line),
        ]

        cdc = stream.Endpoint([("data", 32)])
        self.comb += [
            If(m420,
                m420_packer.source.connect(cdc),
            ).Else(
                cdc.valid.eq(w_valid),
                cdc.data.eq(w_data),
                cdc.first.eq(w_first),
                cdc.last.eq(w_last),
            )
        ]

        # CDC (hdmi -> sys): the sys side drains faster than the average input rate (M420 bursts
        # the UV lines at up to 1 word per pixel clock: deeper FIFO).
        self.cdc = cdc_fifo = stream.ClockDomainCrossing([("data", 32)], cd_from="hdmi", cd_to="sys", depth=1024)
        # Register stage (timing: packer BRAM -> mux -> CDC BRAM).
        self.cdc_buf = cdc_buf = ClockDomainsRenamer("hdmi")(stream.Buffer([("data", 32)], pipe_ready=True))
        self.comb += [
            cdc.connect(cdc_buf.sink),
            cdc_buf.source.connect(cdc_fifo.sink),
        ]

        # Frame admission / drop (sys) + frame FIFO.
        # The FIFO is flushed while the capture is disabled; a frame without input for
        # `idle_timeout` cycles (signal loss) is closed with an end marker.
        self.fifo = fifo = ResetInserter()(stream.SyncFIFO([("data", 32)], fifo_depth, buffered=True))
        enable_sys   = self.control.fields.enable
        idle         = Signal(max=idle_timeout + 1)
        aborted      = Signal(32)
        self.comb += fifo.reset.eq(~enable_sys)
        in_frame     = Signal()
        pending_last = Signal()
        frames       = Signal(32)
        dropped      = Signal(32)
        overflow     = Signal(32)
        sink         = cdc_fifo.source
        admit_ok     = Signal()
        self.comb += [
            sink.ready.eq(1),

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
            # Registered (timing): one cycle old level, conservative for admission.
            admit_ok.eq((fifo_depth - fifo.level) >= self.admit_level.storage),
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
            # Input loss: close the current frame.
            If(~in_frame | sink.valid,
                idle.eq(0),
            ).Elif(idle == idle_timeout,
                idle.eq(0),
                in_frame.eq(0),
                pending_last.eq(1),
                aborted.eq(aborted + 1),
            ).Else(
                idle.eq(idle + 1),
            ),
            # Capture disabled: flush.
            If(~enable_sys,
                in_frame.eq(0),
                pending_last.eq(0),
            ),
        ]
        self.comb += [
            self.frames.status.eq(frames),
            self.dropped.status.eq(dropped),
            self.overflow.status.eq(overflow),
            self.aborted.status.eq(aborted),
        ]

        # Input frame period (sys clock cycles between VSYNC rising edges, 0 when no input).
        vs_sys    = Signal()
        vs_sys_d  = Signal()
        vs_count  = Signal(32)
        self.specials += MultiReg(pads.vsync, vs_sys)
        self.sync += [
            vs_sys_d.eq(vs_sys),
            If(vs_sys & ~vs_sys_d,
                self.frame_period.status.eq(vs_count),
                vs_count.eq(0),
            ).Elif(vs_count == (2**31),
                self.frame_period.status.eq(0),
            ).Else(
                vs_count.eq(vs_count + 1),
            )
        ]
