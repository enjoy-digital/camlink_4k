#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.soc.interconnect.csr_bus import CSRBank, Interface

from gateware.video import VideoPatternGenerator, COLOR_BARS, m420_words

# Small frame: 32x4 pixels, colour bars of 4 pixels.
WIDTH, HEIGHT = 32, 4

# Helpers ------------------------------------------------------------------------------------------

def run(m420, mode=0):
    dut   = VideoPatternGenerator(sys_clk_freq=1e6, with_csr=False)
    words = []
    def gen():
        yield dut.hwords.eq(WIDTH // 2)
        yield dut.vres.eq(HEIGHT * 3 // 4 if m420 else HEIGHT)
        yield dut.bar_words.eq(WIDTH // 16)
        yield dut.frame_period.eq(2000)
        yield dut.m420.eq(m420)
        yield dut.mode.eq(mode)
        yield dut.enable.eq(1)
        yield dut.source.ready.eq(1)
        for _ in range(3000):
            yield
            if (yield dut.source.valid):
                data  = (yield dut.source.data)
                first = (yield dut.source.first)
                last  = (yield dut.source.last)
                words.append((data, first, last))
                if words[-1][2]:
                    return
    run_simulation(dut, gen())
    return words

# Tests --------------------------------------------------------------------------------------------

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
    lw    = WIDTH // 4
    assert [w for w, _, _ in words[:2 * lw]] == [y] * (2 * lw)
    assert [w for w, _, _ in words[2 * lw:3 * lw]] == [uv] * lw

def test_pattern_csr():
    # CSR writes reach the control signals, CSR resets match the control signal resets.
    dut   = VideoPatternGenerator(sys_clk_freq=1e6)
    names = [c.name for c in dut.get_csrs()]
    assert names == ["enable", "hwords", "vres", "bar_words", "frame_period", "mode", "m420",
        "frames", "skipped"]
    bank   = CSRBank(dut.get_csrs(), bus=Interface(data_width=32))
    dut.submodules.bank = bank
    values = {}

    def generator():
        yield
        values["frame_period"] = (yield dut.frame_period)
        values["hwords"]       = (yield dut.hwords)
        yield from bank.bus.write(names.index("enable"), 1)
        yield from bank.bus.write(names.index("vres"),   720)
        yield
        values["enable"] = (yield dut.enable)
        values["vres"]   = (yield dut.vres)

    run_simulation(dut, generator())
    assert values == {"frame_period": int(1e6/30), "hwords": 1920//2, "enable": 1, "vres": 720}
