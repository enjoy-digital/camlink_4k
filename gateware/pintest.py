#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""PinTest: FX3 <-> FPGA pin identification.

Each output pin i drives bit `step` of its ID (i + 1). The FX3 advances `step` with rising edges
on the step pin, samples all its GPIOs at each step and reconstructs which FPGA pin is connected
to which FX3 GPIO.
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

# PinTest ------------------------------------------------------------------------------------------

class PinTest(LiteXModule):
    def __init__(self, step_pin, pins, id_bits=8):
        # # #

        step      = Signal()
        step_d    = Signal()
        step_rise = Signal()
        index     = Signal(max=id_bits)

        # Step synchronization/edge detection.
        self.specials += MultiReg(step_pin, step)
        self.sync += step_d.eq(step)
        self.comb += step_rise.eq(step & ~step_d)
        self.sync += If(step_rise, index.eq(Mux(index == (id_bits - 1), 0, index + 1)))

        # Pins: bit `index` of (i + 1).
        for i, pin in enumerate(pins):
            ident = Constant(i + 1, id_bits)
            self.sync += pin.eq(ident >> index)
