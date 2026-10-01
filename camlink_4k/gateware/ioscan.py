#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""IOScan: debug core counting, for each probed pin, the clock cycles where it is high while a
qualifier (e.g. HDMI DE) is active. Used to locate unknown board connections."""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *

# IO Scan ------------------------------------------------------------------------------------------

class IOScan(LiteXModule):
    def __init__(self, pins, qualifier, cd="sys", width=32):
        self.sel    = CSRStorage(8, description="Pin select.")
        self.clear  = CSRStorage(description="Clear counters.")
        self.count  = CSRStatus(width, description="High count of the selected pin.")
        self.total  = CSRStatus(width, description="Qualified cycles.")

        # # #

        n       = len(pins)
        sync    = getattr(self.sync, cd)
        pins_r  = Signal(n)
        qual_r  = Signal()
        clear   = Signal()
        self.specials += MultiReg(self.clear.storage, clear, cd)
        sync += [pins_r.eq(Cat(*pins)), qual_r.eq(qualifier)]
        counters = [Signal(width) for _ in range(n)]
        total    = Signal(width)
        for i in range(n):
            sync += If(clear, counters[i].eq(0)).Elif(qual_r & pins_r[i], counters[i].eq(counters[i] + 1))
        sync += If(clear, total.eq(0)).Elif(qual_r, total.eq(total + 1))
        sel = Signal(8)
        self.specials += MultiReg(self.sel.storage, sel, cd)
        count = Signal(width)
        sync += Case(sel, {i: count.eq(counters[i]) for i in range(n)})
        self.specials += MultiReg(count, self.count.status)
        self.specials += MultiReg(total, self.total.status)
