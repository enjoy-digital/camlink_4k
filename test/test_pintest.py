#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from gateware.pintest import PinTest

# Test ---------------------------------------------------------------------------------------------

def test_pintest_ids():
    step = Signal()
    pins = [Signal(name=f"pin{i}") for i in range(12)]
    dut  = PinTest(step_pin=step, pins=pins, id_bits=4)
    ids  = [0]*len(pins)

    def generator():
        for bit in range(4):
            # Let synchronizers/outputs settle, then sample.
            for _ in range(8):
                yield
            for i, pin in enumerate(pins):
                ids[i] |= (yield pin) << bit
            # One rising edge on step.
            yield step.eq(1)
            for _ in range(4):
                yield
            yield step.eq(0)

    run_simulation(dut, generator())
    assert ids == [i + 1 for i in range(len(pins))]
