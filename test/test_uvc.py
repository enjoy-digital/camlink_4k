#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

from gateware.video import VideoPatternGenerator, yuy2_word
from gateware.uvc   import UVCPacketizer

# DUT ----------------------------------------------------------------------------------------------

HWORDS = 16
VRES   = 4

class DUT(LiteXModule):
    def __init__(self):
        self.pattern = VideoPatternGenerator(sys_clk_freq=1e3, with_csr=False)
        self.uvc     = UVCPacketizer(payload_words=10, with_csr=False)
        self.comb += self.pattern.source.connect(self.uvc.sink)

# Tests --------------------------------------------------------------------------------------------

def test_pattern_uvc_payloads():
    dut      = DUT()
    payloads = []

    def generator():
        yield dut.pattern.hwords.eq(HWORDS)
        yield dut.pattern.vres.eq(VRES)
        yield dut.pattern.bar_words.eq(HWORDS//8)
        yield dut.pattern.frame_period.eq(200)
        yield dut.uvc.frame_words.eq(HWORDS*VRES)
        yield dut.uvc.source.ready.eq(1)
        yield dut.pattern.enable.eq(1)
        current = []
        for _ in range(600):
            yield
            if (yield dut.uvc.source.valid) and (yield dut.uvc.source.ready):
                current.append((yield dut.uvc.source.data))
                if (yield dut.uvc.source.last):
                    payloads.append(current)
                    current = []

    run_simulation(dut, generator())

    # Frame = 64 words -> 7 payloads (6x10 + 4) per frame, 3 header words each.
    assert len(payloads) >= 14
    sizes = [len(p) - 3 for p in payloads[:7]]
    assert sizes == [10, 10, 10, 10, 10, 10, 4]
    infos = [(p[0] >> 8) & 0xff for p in payloads[:14]]
    for i, info in enumerate(infos):
        assert info & 0x80                         # EOH.
        assert (info >> 1) & 1 == (i % 7 == 6)     # EOF on last payload of each frame.
        assert info & 1 == (i // 7) % 2            # FID toggles per frame.
    assert all(p[0] & 0xff == 12 for p in payloads)
    # Frame data: line 1, third bar (moving bar at x=0..15 covers line on frame 0: check line 1 of
    # frame 1 at word 2*bar_words is covered, so only check sizes and first-line frame bits).
    frame = sum((p[3:] for p in payloads[7:14]), [])
    assert len(frame) == HWORDS*VRES
    assert frame[0] == yuy2_word(235, 128, 235, 128) # Frame 1: bit 0 set (white).
    assert frame[1] == yuy2_word(16, 128, 16, 128)   # Frame 1: bit 1 clear (black).

def test_uvc_csr():
    # CSR map and CSR -> control Signals.
    dut = UVCPacketizer(payload_words=10)
    assert [c.name for c in dut.get_csrs()] == ["payload_words", "frame_words"]
    def generator():
        assert (yield dut.payload_words) == 10
        assert (yield dut.frame_words)   == 1920*1080//2
        yield dut._payload_words.storage.eq(20)
        yield dut._frame_words.storage.eq(HWORDS*VRES)
        yield
        assert (yield dut.payload_words) == 20
        assert (yield dut.frame_words)   == HWORDS*VRES
    run_simulation(dut, generator())
