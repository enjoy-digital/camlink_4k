#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""End-to-end video path simulation: HDMI pixel model -> HDMIIn -> Canvas -> ColorAdjust ->
UVCPacketizer -> GPIFStreamer -> FX3 model (waveform + DMA buffers with the buffer switch capture
quirk). FX3 buffers are decoded as UVC payloads and reassembled into frames (as uvcvideo does)."""

import struct

from migen import *

from litex.gen import *

from litex.soc.interconnect import stream

from camlink_4k.gateware.hdmi_in import HDMIIn
from camlink_4k.gateware.canvas  import Canvas
from camlink_4k.gateware.color   import ColorAdjust
from camlink_4k.gateware.uvc     import UVCPacketizer
from camlink_4k.gateware.gpif    import GPIFStreamer

from test_hdmi_in import Pads as HDMIPads, pixel, HACT, HBLANK, VACT, VBLANK, expected_frame, expected_m420
from test_gpif    import FX3Pads, FX3Model

PAYLOAD_WORDS = 29 # Data words per payload: 3 header words + 29 = 32 words = one FX3 buffer.
BURST_WORDS   = 32

# System -------------------------------------------------------------------------------------------

class System(LiteXModule):
    def __init__(self, fifo_depth=256, gate=True):
        self.hdmi_pads = HDMIPads()
        self.fx3_pads  = FX3Pads()

        self.hdmi_in = hdmi_in = HDMIIn(self.hdmi_pads, fifo_depth=fifo_depth, idle_timeout=256, sim=True)
        self.hdmi_buf = hdmi_buf = ResetInserter()(stream.Buffer([("data", 32)], pipe_valid=True, pipe_ready=True))
        self.canvas  = canvas = ResetInserter()(Canvas())
        self.color   = color  = ResetInserter()(ColorAdjust())
        self.uvc     = uvc    = ResetInserter()(UVCPacketizer(payload_words=PAYLOAD_WORDS))
        self.gpif_buf = gpif_buf = stream.Buffer([("data", 32), ("next", 32)])
        self.gpif    = gpif   = GPIFStreamer(self.fx3_pads, sim=True)
        self.ctl     = gpif.ctl
        self.pads_dq = self.fx3_pads.dq

        timestamp = Signal(32)
        self.sync += timestamp.eq(timestamp + 1)
        self.comb += [
            uvc.timestamp.eq(timestamp),
            uvc.reset.eq(~hdmi_in.control.fields.enable),
            hdmi_buf.reset.eq(~hdmi_in.control.fields.enable),
            canvas.reset.eq(~hdmi_in.control.fields.enable),
            color.reset.eq(~hdmi_in.control.fields.enable),
            gpif.eop_data.eq(uvc.next_header0),
            hdmi_in.admit.eq(canvas.admit | ~gate),
            hdmi_in.source.connect(hdmi_buf.sink),
            hdmi_buf.source.connect(canvas.sink),
            canvas.source.connect(color.sink),
            color.source.connect(uvc.sink),
            uvc.source.connect(gpif_buf.sink),
            gpif_buf.source.connect(gpif.sink),
        ]

# UVC Decoding -------------------------------------------------------------------------------------

def decode_frames(buffers):
    """UVC payloads (one per FX3 buffer) -> list of frames (lists of 32-bit data words)."""
    frames, current, fid, errors = [], [], None, 0
    for words in buffers:
        if not words:
            continue
        b0 = words[0]
        hlen, info = b0 & 0xff, (b0 >> 8) & 0xff
        if hlen != 12 or not (info & 0x80):
            errors += 1
            current, fid = [], None
            continue
        f = info & 1
        if fid is not None and f != fid and current:
            current = [] # FID change without EOF: incomplete frame dropped.
        fid = f
        current += words[3:]
        if info & 0x02:
            frames.append(current)
            current, fid = [], None
    return frames, errors

# Test ---------------------------------------------------------------------------------------------

def ddr_source(pads, frames):
    for f in range(frames):
        for y in range(VACT + VBLANK):
            for x in range(HACT//2 + HBLANK):
                active = (y < VACT) and (x < HACT//2)
                yield pads.de.eq(active)
                yield pads.vsync.eq(y == VACT + 1)
                yield pads.qe.eq(pixel(f, 2*x, y) if active else 0)
                yield pads.qe_fall.eq(pixel(f, 2*x + 1, y) if active else 0)
                yield

def run(frame_words, frames=8, downscale=False, m420=False, canvas=None, drain=(80, 150),
    fifo_depth=256, admit_level=64, gate=True):
    dut = System(fifo_depth=fifo_depth, gate=gate)
    fx3 = FX3Model(dut, buf_words=(BURST_WORDS, 48), drain=drain, dma_start=20)

    def config():
        h = dut.hdmi_in.control.fields
        yield h.y_lane.eq(1)
        yield h.c_lane.eq(0)
        yield h.ddr.eq(1)
        yield h.downscale.eq(downscale)
        yield h.m420.eq(m420)
        yield dut.hdmi_in.admit_level.storage.eq(admit_level)
        if canvas is not None:
            out_w, out_h, in_w, in_h, x0, y0 = canvas
            for csr, v in ((dut.canvas.enable, 1), (dut.canvas.out_hwords, out_w), (dut.canvas.out_vres, out_h),
                (dut.canvas.in_hwords, in_w), (dut.canvas.in_vres, in_h), (dut.canvas.x0, x0), (dut.canvas.y0, y0)):
                yield csr.storage.eq(v)
        yield dut.uvc._payload_words.storage.eq(PAYLOAD_WORDS)
        yield dut.uvc._frame_words.storage.eq(frame_words)
        g = dut.gpif._control.fields
        yield g.flag_invert.eq(1)
        yield g.head_lead.eq(4)
        yield dut.gpif._burst.storage.eq(BURST_WORDS)
        yield dut.gpif._guard.storage.eq(8)
        for _ in range(40):
            yield
        yield g.enable.eq(1)
        yield h.enable.eq(1)
        for _ in range(frames*(VACT + VBLANK)*(HACT//2 + HBLANK)*2 + 2000):
            yield

    run_simulation(dut, {
            "hdmi":  [ddr_source(dut.hdmi_pads, frames)],
            "sys":   [config()],
            "gpif":  [fx3.run()],
        }, clocks={"hdmi": 10, "sys": 7, "gpif": 10, "gpif_cdc": 10})
    assert fx3.errors == []
    return decode_frames(fx3.buffers[0])

def frame_index(words):
    return ((words[0] & 0xff) - 0xa0) % 256 // 16

def check(frames, errors, expected, min_frames=3):
    assert errors == 0
    assert len(frames) >= min_frames
    for words in frames:
        assert words == expected(frame_index(words))

def test_system_direct():
    frames, errors = run(frame_words=HACT*VACT//2)
    check(frames, errors, expected_frame)

def test_system_direct_slow_usb():
    # USB draining slowly: FX3 buffer switches lag (captured words must still be right).
    frames, errors = run(frame_words=HACT*VACT//2, drain=(300, 150))
    check(frames, errors, expected_frame, min_frames=2)

def test_system_m420():
    frames, errors = run(frame_words=HACT*VACT*3//8, m420=True)
    check(frames, errors, expected_m420)

def test_system_canvas():
    # 16x4 input (8 words x 4 lines) centered in a 24x6 frame (12 words x 6 lines).
    frames, errors = run(frame_words=12*6, canvas=(12, 6, 8, 4, 2, 1))
    def expected(f):
        src = expected_frame(f)
        out = []
        for y in range(6):
            for x in range(12):
                inside = (2 <= x < 10) and (1 <= y < 5)
                out.append(src[(y - 1)*8 + (x - 2)] if inside else 0x80108010)
        return out
    assert errors == 0 and len(frames) >= 3
    for words in frames:
        # Frame index from the first window word.
        assert words == expected(frame_index([words[12 + 2]]))

def test_system_canvas_overflow(gate=True):
    # Input FIFO smaller than a frame and tall borders (the input FIFO would overflow during the
    # top border): frames starting during the borders are skipped, the others are complete.
    frames, errors = run(frame_words=8*20, canvas=(8, 20, 8, 4, 0, 8), frames=24, fifo_depth=24,
        admit_level=8, gate=gate)
    def expected(f):
        src = expected_frame(f)
        return [src[(y - 8)*8 + x] if 8 <= y < 12 else 0x80108010 for y in range(20) for x in range(8)]
    assert errors == 0 and len(frames) >= 2
    for words in frames:
        # Frame index from the first window word.
        assert words == expected(frame_index([words[8*8]]))
