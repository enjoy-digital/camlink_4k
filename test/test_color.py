#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litex.gen import *

from gateware.color import ColorAdjust

# Helpers ------------------------------------------------------------------------------------------

def model(word, brightness, contrast, saturation):
    out = 0
    for i in range(4):
        c = (word >> (8*i)) & 0xff
        if i % 2 == 0:
            v = (((c - 16)*contrast) >> 7) + 16 + brightness
        else:
            v = (((c - 128)*saturation) >> 7) + 128
        out |= max(0, min(255, v)) << (8*i)
    return out

def run(brightness, contrast, saturation, ready=lambda i: 1):
    random.seed(1)
    dut   = ColorAdjust()
    words = [random.getrandbits(32) for _ in range(64)]
    out   = []

    def gen():
        yield dut.brightness.storage.eq(brightness & 0xff)
        yield dut.contrast.storage.eq(contrast)
        yield dut.saturation.storage.eq(saturation)
        for i, w in enumerate(words):
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(w)
            yield dut.sink.last.eq(i == len(words) - 1)
            yield
            while not (yield dut.sink.ready):
                yield
        yield dut.sink.valid.eq(0)

    def sink():
        for cycle in range(1000):
            yield dut.source.ready.eq(ready(cycle))
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))

    run_simulation(dut, [gen(), sink()])
    assert out == [model(w, brightness, contrast, saturation) for w in words]

# Tests --------------------------------------------------------------------------------------------

def test_color_identity():
    run(0, 128, 128)

def test_color_adjust():
    run(-20, 200, 60)

def test_color_adjust_backpressure():
    run(30, 90, 255, ready=lambda i: (i % 3) != 0)
