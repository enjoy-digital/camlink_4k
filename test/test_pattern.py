#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from camlink_4k.gateware.video import VideoPatternGenerator, COLOR_BARS, m420_words

# Small frame: 32x4 pixels, colour bars of 4 pixels.
WIDTH, HEIGHT = 32, 4

def run(m420, mode=0):
    dut = VideoPatternGenerator(sys_clk_freq=1e6)
    words = []
    def gen():
        yield dut._hwords.storage.eq(WIDTH // 2)
        yield dut._vres.storage.eq(HEIGHT * 3 // 4 if m420 else HEIGHT)
        yield dut._bar_words.storage.eq(WIDTH // 16)
        yield dut._frame_period.storage.eq(2000)
        yield dut._m420.storage.eq(m420)
        yield dut._mode.storage.eq(mode)
        yield dut._enable.storage.eq(1)
        yield dut.source.ready.eq(1)
        for _ in range(3000):
            yield
            if (yield dut.source.valid):
                words.append(((yield dut.source.data), (yield dut.source.first), (yield dut.source.last)))
                if words[-1][2]:
                    return
    run_simulation(dut, gen())
    return words

def test_pattern_m420_layout():
    # M420: per line pair, even Y line, odd Y line, CbCr line (WIDTH/4 words each), colour bars of
    # WIDTH/8 pixels (1 word here), same frame size as YUY2 (hres/2 x vres*3/4 words).
    words = run(m420=1)
    assert len(words) == WIDTH * HEIGHT * 3 // 2 // 4
    assert words[0][1] and words[-1][2]
    lw = WIDTH // 4
    for pair in range(HEIGHT // 2):
        for sub in range(3):
            line = [w for w, _, _ in words[(pair * 3 + sub) * lw:(pair * 3 + sub + 1) * lw]]
            expected = [m420_words(*COLOR_BARS[i])[1 if sub == 2 else 0] for i in range(lw)]
            assert line == expected, (pair, sub, [hex(w) for w in line])

def test_pattern_m420_no_signal():
    words = run(m420=1, mode=1)
    y, uv = m420_words(40, 170, 118)
    lw = WIDTH // 4
    assert [w for w, _, _ in words[:2 * lw]] == [y] * (2 * lw)
    assert [w for w, _, _ in words[2 * lw:3 * lw]] == [uv] * lw
