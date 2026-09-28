#
# This file is part of LiteCamLink.
#
# Copyright (c) 2019-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CRG: Clock/Reset Generator for the Cam Link 4K (27MHz input, ECP5 PLL, DDR3 sys2x domain)."""

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.gen import *

from litex.soc.cores.clock import ECP5PLL

# CRG ----------------------------------------------------------------------------------------------

class CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_sdram=False):
        self.rst    = Signal()
        self.stop   = Signal()
        self.cd_por = ClockDomain(reset_less=True)
        self.cd_sys = ClockDomain()
        if with_sdram:
            self.cd_init    = ClockDomain()
            self.cd_sys2x   = ClockDomain()
            self.cd_sys2x_i = ClockDomain(reset_less=True)

        # # #

        # Clk.
        clk27 = platform.request("clk27")

        # Power-On-Reset.
        por_count = Signal(16, reset=2**16-1)
        por_done  = Signal()
        self.comb += self.cd_por.clk.eq(clk27)
        self.comb += por_done.eq(por_count == 0)
        self.sync.por += If(~por_done, por_count.eq(por_count - 1))

        # PLL.
        self.pll = pll = ECP5PLL()
        self.comb += pll.reset.eq(~por_done | self.rst)
        pll.register_clkin(clk27, 27e6)
        if with_sdram:
            pll.create_clkout(self.cd_sys2x_i, 2*sys_clk_freq)
            pll.create_clkout(self.cd_init, 27e6)
            self.specials += [
                Instance("ECLKSYNCB",
                    i_ECLKI = self.cd_sys2x_i.clk,
                    i_STOP  = self.stop,
                    o_ECLKO = self.cd_sys2x.clk),
                Instance("CLKDIVF",
                    p_DIV     = "2.0",
                    i_ALIGNWD = 0,
                    i_CLKI    = self.cd_sys2x.clk,
                    i_RST     = self.cd_sys2x.rst,
                    o_CDIVX   = self.cd_sys.clk),
                AsyncResetSynchronizer(self.cd_init,  ~pll.locked),
                AsyncResetSynchronizer(self.cd_sys,   ~pll.locked),
                AsyncResetSynchronizer(self.cd_sys2x, ~pll.locked),
            ]
        else:
            pll.create_clkout(self.cd_sys, sys_clk_freq)
