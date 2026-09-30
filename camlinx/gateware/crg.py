#
# This file is part of CamLinX.
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

    video_clk_freq: optional `video` domain (video pipeline) from the PLL, when the DRAM ratio sets a
    sys clock too slow for 4K30 streaming (e.g. 87.75MHz sys for DDR3-700 at 1:4, video at ~100MHz).
    """
    def __init__(self, platform, sys_clk_freq, sdram_rate=None, sys_clk_src="clkdivf", sys_phase=0,
        video_clk_freq=None):
        self.rst    = Signal()
        self.stop   = Signal()
        self.reset  = Signal()
        # 1:4 sys/sys2x phase control: `alignwd` pulse slips sys2x by one sys4x cycle (the sys2x
        # CLKDIVF division phase is set at reset release: sys edges end up either between sys2x
        # edges or on them, a setup/hold race of the DFI rate converter crossings that nextpnr does
        # not check, build dependent on hardware), `sys2x_rst` re-releases the sys2x reset from sys.
        self.alignwd   = Signal()
        self.sys2x_rst = Signal()
        # DDR PHY init sequence replay (init domain reset: DDRDLLA relock, ECLK stop/reset, DQSBUFM
        # update): new CLKDIVF division phases, DRAM init retries on hardware.
        self.phy_init  = Signal()
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
        if video_clk_freq is not None:
            self.cd_video   = ClockDomain()

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
            # DDRDLLA init sequencing clock (not critical) from the input clock: keeps a PLL output
            # free for the video clock with the dedicated PLL feedback (CLKOS3). With the 4 outputs
            # used (feedback from CLKOP), the 1:4 DRAM had no read window on hardware.
            self.comb += self.cd_init.clk.eq(clk27)
            self.specials += AsyncResetSynchronizer(self.cd_init, ~por_done | ~pll.locked | self.phy_init) # Init after lock.
            self.specials += [
                Instance("ECLKSYNCB",
                    i_ECLKI = self.cd_sys4x_i.clk,
                    i_STOP  = self.stop,
                    o_ECLKO = self.cd_sys4x.clk),
                Instance("CLKDIVF",
                    p_DIV     = "2.0",
                    i_ALIGNWD = self.alignwd,
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
            self.sync.sys2x += sys_rst_2x.eq(ResetSignal("sys") | self.sys2x_rst)
            self.comb += self.cd_sys2x.rst.eq(sys_rst_2x)
        else:
            pll.create_clkout(self.cd_sys, sys_clk_freq)

        # Video domain (PLL output, reset with the PLL lock by create_clkout; not stopped/reset by
        # the DDR PHY init).
        if video_clk_freq is not None:
            pll.create_clkout(self.cd_video, video_clk_freq)
