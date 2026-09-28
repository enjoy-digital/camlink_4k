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
    """Clocks: sys (+ DDR3 domains).

    sdram_rate:
    - None  : sys only (PLL).
    - "1:2" : ECP5DDRPHY at sys: sys2x = DDR edge clock (ECLKSYNCB), sys = sys2x/2 (CLKDIVF).
    - "1:4" : ECP5DDRPHY at sys2x behind a DFI rate converter: sys4x = DDR edge clock, sys2x =
              sys4x/2 (CLKDIVF), sys = controller clock, phase aligned with sys2x:
              - sys_clk_src "clkdivf": second edge clock at 2x sys (PLL output aligned with sys4x)
                divided by 2 (same structural path as sys2x).
              - sys_clk_src "pll": PLL output with `sys_phase` (degrees).
    The DDR PHY init sequence stops (`stop`) and resets (`reset`) the edge clock domains.
    """
    def __init__(self, platform, sys_clk_freq, sdram_rate=None, sys_clk_src="clkdivf", sys_phase=0):
        self.rst    = Signal()
        self.stop   = Signal()
        self.reset  = Signal()
        self.cd_por = ClockDomain(reset_less=True)
        self.cd_sys = ClockDomain()
        if sdram_rate is not None:
            self.cd_init    = ClockDomain()
            self.cd_sys2x   = ClockDomain()
        if (sdram_rate == "1:2") or (sdram_rate == "1:4" and sys_clk_src == "clkdivf"):
            self.cd_sys2x_i = ClockDomain(reset_less=True)
        if sdram_rate == "1:4":
            self.cd_sys4x   = ClockDomain()
            self.cd_sys4x_i = ClockDomain(reset_less=True)

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
        if sdram_rate == "1:2":
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
                    i_RST     = self.reset,
                    o_CDIVX   = self.cd_sys.clk),
                AsyncResetSynchronizer(self.cd_sys,   ~pll.locked | self.reset),
                AsyncResetSynchronizer(self.cd_sys2x, ~pll.locked | self.reset),
            ]
        elif sdram_rate == "1:4":
            pll.create_clkout(self.cd_sys4x_i, 4*sys_clk_freq)
            pll.create_clkout(self.cd_init, 25e6) # DDRDLLA init sequencing (not critical).
            self.specials += [
                Instance("ECLKSYNCB",
                    i_ECLKI = self.cd_sys4x_i.clk,
                    i_STOP  = self.stop,
                    o_ECLKO = self.cd_sys4x.clk),
                Instance("CLKDIVF",
                    p_DIV     = "2.0",
                    i_ALIGNWD = 0,
                    i_CLKI    = self.cd_sys4x.clk,
                    i_RST     = self.reset,
                    o_CDIVX   = self.cd_sys2x.clk),
                AsyncResetSynchronizer(self.cd_sys4x, ~pll.locked | self.reset),
            ]
            if sys_clk_src == "clkdivf":
                sys2x_e = Signal()
                pll.create_clkout(self.cd_sys2x_i, 2*sys_clk_freq)
                self.specials += [
                    Instance("ECLKSYNCB",
                        i_ECLKI = self.cd_sys2x_i.clk,
                        i_STOP  = self.stop,
                        o_ECLKO = sys2x_e,
                        # Second edge clock of the DDR bank (the first one is sys4x).
                        attr    = {("BEL", "X0/Y25/ECLKSYNC1_BK7")}),
                    Instance("CLKDIVF",
                        p_DIV     = "2.0",
                        i_ALIGNWD = 0,
                        i_CLKI    = sys2x_e,
                        i_RST     = self.reset,
                        o_CDIVX   = self.cd_sys.clk,
                        # Second divider of the DDR (left) side (the first one divides sys4x).
                        attr      = {("BEL", "X0/Y25/CLKDIV1")}),
                ]
            else:
                pll.create_clkout(self.cd_sys, sys_clk_freq, phase=sys_phase)
            # Resets: sys released first, sys2x released from sys (deterministic phase of the DFI
            # rate converter serializers relative to sys edges).
            sys_rst_2x = Signal(reset=1, reset_less=True) # Drives cd_sys2x.rst: must not reset itself.
            self.specials += AsyncResetSynchronizer(self.cd_sys, ~pll.locked | self.reset)
            self.sync.sys2x += sys_rst_2x.eq(ResetSignal("sys"))
            self.comb += self.cd_sys2x.rst.eq(sys_rst_2x)
        else:
            pll.create_clkout(self.cd_sys, sys_clk_freq)
