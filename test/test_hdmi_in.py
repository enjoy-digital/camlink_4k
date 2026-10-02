#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.sim import passive

from litex.gen import *

from gateware.hdmi_in import HDMIIn

# Video Source Model -------------------------------------------------------------------------------

HACT, HBLANK = 16, 6
VACT, VBLANK = 4, 3

class Pads:
    def __init__(self):
        self.pclk    = Signal()
        self.qe      = Signal(24)
        self.qe_fall = Signal(24) # Simulation model of the falling edge sample (DDR).
        self.de      = Signal()
        self.hsync   = Signal()
        self.vsync   = Signal()

def pixel(frame, x, y):
    # Y on QE[23:16] (lane 1), C on QE[7:0] (lane 0): Cb on even pixels, Cr on odd ones.
    luma   = (0xa0 + frame*16 + y*4 + x) & 0xff # Values >= 128 (catches carry/width issues).
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

# Test Runner --------------------------------------------------------------------------------------

def ddr_source(pads, frames):
    # DDR: pixel pair per clock (rising = even pixel, falling = odd pixel).
    for f in range(frames):
        for y in range(VACT + VBLANK):
            for x in range(HACT//2 + HBLANK):
                active = (y < VACT) and (x < HACT//2)
                yield pads.de.eq(active)
                yield pads.vsync.eq(y == VACT + 1)
                yield pads.qe.eq(pixel(f, 2*x, y) if active else 0)
                yield pads.qe_fall.eq(pixel(f, 2*x + 1, y) if active else 0)
                yield

def run(ready_pattern, frames=6, ddr=False, downscale=False, crop=None, source=None, m420=False,
    rgb=False):
    pads = Pads()
    dut  = HDMIIn(pads, fifo_depth=64, idle_timeout=64, sim=True)
    out  = []

    def config():
        # CSR field logic is elaborated by the SoC CSR bank: drive the fields directly.
        yield dut.control.fields.enable.eq(1)
        yield dut.control.fields.y_lane.eq(1)
        yield dut.control.fields.c_lane.eq(0)
        yield dut.control.fields.ddr.eq(ddr)
        yield dut.control.fields.downscale.eq(downscale)
        yield dut.admit_level.storage.eq(40)
        yield dut.control.fields.m420.eq(m420)
        yield dut.control.fields.rgb.eq(rgb)
        if crop is not None:
            yield dut.control.fields.crop.eq(1)
            for csr, v in zip((dut.crop_x, dut.crop_y, dut.crop_w, dut.crop_h), crop):
                yield csr.storage.eq(v)
        yield

    @passive
    def sink():
        cycle   = 0
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
        "hdmi": [(source or (ddr_source if ddr else video_source))(pads, frames)],
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

def expected_m420(f):
    words = []
    for y in range(0, VACT, 2):
        for yy in (y, y + 1):
            for x in range(0, HACT, 4):
                l = [pixel(f, x + i, yy) >> 8 for i in range(4)]
                words.append(l[0] | (l[1] << 8) | (l[2] << 16) | (l[3] << 24))
        for x in range(0, HACT, 4):
            uv = []
            for px in (x, x + 2):
                u = ((pixel(f, px, y) & 0xff) + (pixel(f, px, y + 1) & 0xff) + 1) >> 1
                v = ((pixel(f, px + 1, y) & 0xff) + (pixel(f, px + 1, y + 1) & 0xff) + 1) >> 1
                uv += [u, v]
            words.append(uv[0] | (uv[1] << 8) | (uv[2] << 16) | (uv[3] << 24))
    return words

def rgb_pixel(f, x, y):
    """RGB test pixel: lane 0 = B, lane 1 = G, lane 2 = R."""
    r, g, b = (0x20 + 16*f + 5*x) & 0xff, (0xa0 + 7*y + 3*x) & 0xff, (0x40 + 11*x) & 0xff
    return (r << 16) | (g << 8) | b

def run_crop(x0, y0, w, h):
    dut, frames = run(lambda cycle: 1, ddr=True, crop=(x0, y0, w, h))
    assert len(frames) >= 3
    for frame in frames:
        assert len(frame) == w*h
        f = ((frame[0] & 0xff) - 0xa0 - 4*y0 - 2*x0) % 256 // 16
        full  = expected_frame(f)
        words = [full[y*(HACT//2) + x] for y in range(y0, y0 + h) for x in range(x0, x0 + w)]
        assert frame == words

# Tests --------------------------------------------------------------------------------------------

def test_hdmi_in_frames():
    dut, frames = run(lambda cycle: 1)
    # First frame is used to measure the height: following frames are complete.
    assert len(frames) >= 3
    for frame in frames:
        assert len(frame) == HACT*VACT//2
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        assert frame == expected_frame(f)

def test_hdmi_in_drop():
    # Slow sink: frames that do not fit are dropped entirely, admitted ones stay complete.
    dut, frames = run(lambda cycle: (cycle % 8) == 0, frames=8)
    assert len(frames) >= 1
    for frame in frames:
        assert len(frame) == HACT*VACT//2

def test_hdmi_in_ddr():
    dut, frames = run(lambda cycle: 1, ddr=True)
    assert len(frames) >= 3
    for frame in frames:
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        assert frame == expected_frame(f)

def test_hdmi_in_ddr_downscale():
    dut, frames = run(lambda cycle: 1, ddr=True, downscale=True)
    assert len(frames) >= 3
    for frame in frames:
        assert len(frame) == (HACT//2)*(VACT//2)//2
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        words = []
        def hword(y, x):
            p  = [pixel(f, x + i, y) for i in range(4)]
            l  = [v >> 8 for v in p]
            c  = [v & 0xff for v in p]
            return [(l[0] + l[1]) >> 1, (c[0] + c[2]) >> 1, (l[2] + l[3]) >> 1, (c[1] + c[3]) >> 1]
        for y in range(0, VACT, 2):
            for x in range(0, HACT, 4):
                a, b = hword(y, x), hword(y + 1, x)
                v = [(a[i] + b[i] + 1) >> 1 for i in range(4)]
                words.append(v[0] | (v[1] << 8) | (v[2] << 16) | (v[3] << 24))
        assert frame == words

def test_hdmi_in_ddr_crop():
    # Window: words 2-5 (pixels 4-11), lines 1-2.
    run_crop(2, 1, 4, 2)

def test_hdmi_in_ddr_crop_origin():
    # Window at the origin (first word of the line in the window).
    run_crop(0, 0, 3, 3)

def test_hdmi_in_signal_loss():
    # Frame 3 is cut after 2 lines (input lost for a while): it is closed by the idle timeout and
    # the following frames are complete.
    def source(pads, frames):
        for f in range(frames):
            for y in range(VACT + VBLANK):
                for x in range(HACT + HBLANK):
                    cut    = (f == 3) and (y >= 2)
                    active = (y < VACT) and (x < HACT) and not cut
                    yield pads.de.eq(active)
                    yield pads.vsync.eq((y == VACT + 1) and not cut)
                    yield pads.qe.eq(pixel(f, x, y) if active else 0)
                    yield
            if f == 3:
                for _ in range(200):
                    yield
    dut, frames = run(lambda cycle: 1, frames=8, source=source)
    sizes = [len(frame) for frame in frames]
    full  = HACT*VACT//2
    assert any(size < full for size in sizes)  # The cut frame, closed.
    assert sizes[-2:] == [full, full]          # Recovery.
    for frame in frames[-2:]:
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        assert frame == expected_frame(f)

def test_hdmi_in_ddr_m420():
    dut, frames = run(lambda cycle: 1, ddr=True, m420=True)
    assert len(frames) >= 3
    for frame in frames:
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        assert frame == expected_m420(f)

def test_hdmi_in_ddr_m420_backpressure():
    dut, frames = run(lambda cycle: (cycle % 3) != 0, ddr=True, m420=True)
    assert len(frames) >= 2
    for frame in frames:
        f = ((frame[0] & 0xff) - 0xa0) % 256 // 16
        assert frame == expected_m420(f)

def test_hdmi_in_ddr_rgb():
    from test_csc import model
    from gateware.csc import bt709_coefficients
    def source(pads, frames):
        for f in range(frames):
            for y in range(VACT + VBLANK):
                for x in range(HACT//2 + HBLANK):
                    active = (y < VACT) and (x < HACT//2)
                    yield pads.de.eq(active)
                    yield pads.vsync.eq(y == VACT + 1)
                    yield pads.qe.eq(rgb_pixel(f, 2*x, y) if active else 0)
                    yield pads.qe_fall.eq(rgb_pixel(f, 2*x + 1, y) if active else 0)
                    yield
    dut, frames = run(lambda cycle: 1, ddr=True, rgb=True, source=source)
    coefs = bt709_coefficients(True)
    split = lambda v: ((v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff)
    assert len(frames) >= 3
    matched = 0
    for frame in frames:
        for f in range(6):
            words = []
            for y in range(VACT):
                for x in range(0, HACT, 2):
                    p0 = split(rgb_pixel(f, x,     y))
                    p1 = split(rgb_pixel(f, x + 1, y))
                    y0, y1, cb, cr = model(p0, p1, coefs)
                    words.append(y0 | (cb << 8) | (y1 << 16) | (cr << 24))
            if frame == words:
                matched += 1
                break
        else:
            assert False, "RGB frame does not match any source frame."
    assert matched == len(frames)
