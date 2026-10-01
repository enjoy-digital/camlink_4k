#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

from camlink_4k.gateware.canvas import Canvas, BLACK

# Helpers ------------------------------------------------------------------------------------------

def run(out_w, out_h, in_w, in_h, x0, y0, frames, in_ready_gap=0, enable=1):
    dut = Canvas()
    out = []

    def config():
        yield dut.enable.storage.eq(enable)
        yield dut.out_hwords.storage.eq(out_w)
        yield dut.out_vres.storage.eq(out_h)
        yield dut.in_hwords.storage.eq(in_w)
        yield dut.in_vres.storage.eq(in_h)
        yield dut.x0.storage.eq(x0)
        yield dut.y0.storage.eq(y0)
        yield

    def source():
        yield from config()
        for f, words in enumerate(frames):
            for i, w in enumerate(words):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(w)
                yield dut.sink.first.eq(i == 0)
                yield dut.sink.last.eq(i == len(words) - 1)
                yield
                while not (yield dut.sink.ready):
                    yield
                if in_ready_gap:
                    yield dut.sink.valid.eq(0)
                    for _ in range(in_ready_gap):
                        yield
        yield dut.sink.valid.eq(0)

    def sink():
        current = []
        for cycle in range(20000):
            yield dut.source.ready.eq(cycle % 5 != 0)
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                current.append((yield dut.source.data))
                if (yield dut.source.last):
                    out.append(current)
                    current = []

    run_simulation(dut, [source(), sink()])
    return out

def expected(out_w, out_h, in_w, in_h, x0, y0, words):
    frame = []
    it    = iter(words)
    for y in range(out_h):
        for x in range(out_w):
            inside = (x0 <= x < x0 + in_w) and (y0 <= y < y0 + in_h)
            frame.append(next(it, BLACK) if inside else BLACK)
    return frame

# Tests --------------------------------------------------------------------------------------------

def test_canvas_letterbox():
    frames = [[0x1000*(f + 1) + i for i in range(4*3)] for f in range(3)]
    out = run(8, 6, 4, 3, 2, 1, frames, in_ready_gap=2)
    assert len(out) == 3
    for f, frame in enumerate(out):
        assert frame == expected(8, 6, 4, 3, 2, 1, frames[f])

def test_canvas_origin_short_input():
    # Window at the origin, input frame shorter than the window: rest of the window black.
    frames = [[0x100 + i for i in range(5)], [0x200 + i for i in range(12)]]
    out = run(6, 4, 4, 3, 0, 0, frames)
    assert out[0] == expected(6, 4, 4, 3, 0, 0, frames[0])
    assert out[1] == expected(6, 4, 4, 3, 0, 0, frames[1])

def test_canvas_bypass():
    frames = [[0x300 + i for i in range(10)]]
    out = run(8, 6, 4, 3, 2, 1, frames, enable=0)
    assert out == frames
