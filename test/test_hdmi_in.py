#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.sim import passive

from litex.gen import *

from litecamlink.gateware.hdmi_in import HDMIIn

# Video Source Model -------------------------------------------------------------------------------

HACT, HBLANK = 16, 6
VACT, VBLANK = 4, 3

class Pads:
    def __init__(self):
        self.pclk  = Signal()
        self.qe    = Signal(24)
        self.de    = Signal()
        self.hsync = Signal()
        self.vsync = Signal()

def pixel(frame, x, y):
    # Y on QE[23:16] (lane 1), C on QE[7:0] (lane 0): Cb on even pixels, Cr on odd ones.
    luma   = (frame*16 + y*4 + x) & 0xff
    chroma = (0x40 + x) & 0xff if x % 2 == 0 else (0xc0 + y) & 0xff
    return (luma << 8) | chroma

def video_source(pads, frames):
    for f in range(frames):
        for y in range(VACT + VBLANK):
            for x in range(HACT + HBLANK):
                active = (y < VACT) and (x < HACT)
                yield pads.de.eq(active)
                yield pads.vsync.eq(y == VACT + 1)
                yield pads.qe.eq(pixel(f, x, y) if active else 0)
                yield

# Test ---------------------------------------------------------------------------------------------

def run(ready_pattern, frames=6):
    pads = Pads()
    dut  = HDMIIn(pads, fifo_depth=64)
    out  = []

    def config():
        # CSR field logic is elaborated by the SoC CSR bank: drive the fields directly.
        yield dut.control.fields.enable.eq(1)
        yield dut.control.fields.y_lane.eq(1)
        yield dut.control.fields.c_lane.eq(0)
        yield dut.admit_level.storage.eq(40)
        yield


    @passive
    def sink():
        cycle = 0
        current = []
        while True:
            yield dut.source.ready.eq(ready_pattern(cycle))
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                current.append((yield dut.source.data))
                if (yield dut.source.last):
                    out.append(current)
                    current = []
            cycle += 1

    run_simulation(dut, {
        "hdmi": [video_source(pads, frames)],
        "sys":  [config(), sink()],
    }, clocks={"hdmi": 10, "sys": 7})
    return dut, out

def expected_frame(f):
    words = []
    for y in range(VACT):
        for x in range(0, HACT, 2):
            p0, p1 = pixel(f, x, y), pixel(f, x + 1, y)
            y0, c0 = p0 >> 8, p0 & 0xff
            y1, c1 = p1 >> 8, p1 & 0xff
            words.append(y0 | (c0 << 8) | (y1 << 16) | (c1 << 24))
    return words

def test_hdmi_in_frames():
    dut, frames = run(lambda cycle: 1)
    # First frame is used to measure the height: following frames are complete.
    assert len(frames) >= 3
    for frame in frames:
        assert len(frame) == HACT*VACT//2
        f = (frame[0] & 0xff) // 16
        assert frame == expected_frame(f)

def test_hdmi_in_drop():
    # Slow sink: frames that do not fit are dropped entirely, admitted ones stay complete.
    dut, frames = run(lambda cycle: (cycle % 8) == 0, frames=8)
    assert len(frames) >= 1
    for frame in frames:
        assert len(frame) == HACT*VACT//2
