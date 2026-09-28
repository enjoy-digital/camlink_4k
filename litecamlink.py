#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2019-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteCamLink: LiteX based gateware for the Elgato Cam Link 4K."""

import os
import argparse

from migen import *

from litex.gen import *

from litex.soc.integration.soc_core import *
from litex.soc.integration.builder  import *
from litex.soc.cores.led            import LedChaser
from litex.soc.cores.freqmeter      import FreqMeter
from litex.soc.interconnect.csr     import CSRStorage

from litecamlink_platform import Platform

from litecamlink.gateware.crg     import CRG
from litecamlink.gateware.pintest    import PinTest
from litecamlink.gateware.i2c_bridge import I2CBridge
from litecamlink.gateware.gpif       import GPIFStreamer, CounterGenerator
from litecamlink.gateware.video      import VideoPatternGenerator
from litecamlink.gateware.uvc        import UVCPacketizer

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCMini):
    def __init__(self, sys_clk_freq=100e6, toolchain="trellis", with_pintest=False):
        platform = Platform(toolchain=toolchain)

        # CRG --------------------------------------------------------------------------------------
        self.crg = CRG(platform, sys_clk_freq)

        # SoCMini ----------------------------------------------------------------------------------
        SoCMini.__init__(self, platform, sys_clk_freq, ident="LiteCamLink SoC on Cam Link 4K.")

        # Leds -------------------------------------------------------------------------------------
        self.leds = LedChaser(
            pads         = platform.request_all("user_led"),
            sys_clk_freq = sys_clk_freq,
        )

        # I2C Bridge (Host access through the FX3 I2C master) --------------------------------------
        self.i2c_bridge = I2CBridge(platform.request("i2c"), address=0x10)
        self.bus.add_master(name="i2c_bridge", master=self.i2c_bridge.bus)

        # HDMI Receiver (IT6802) -------------------------------------------------------------------
        hdmi_in = platform.request("hdmi_in")
        self.comb += hdmi_in.rst_n.eq(1) # Release IT6802 reset.
        self.hdmi_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=hdmi_in.pclk)

        # PinTest ----------------------------------------------------------------------------------
        fx3 = platform.request("fx3")
        if not with_pintest:
            self.fx3_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=fx3.pclk)

            # GPIF Streamer ------------------------------------------------------------------------
            self.gpif = GPIFStreamer(fx3)

            # Sources: Counter (raw) / Video Pattern -> UVC Packetizer.
            self.gen     = CounterGenerator()
            self.pattern = VideoPatternGenerator(sys_clk_freq)
            self.uvc     = UVCPacketizer()

            # Timestamp (sys clock) for UVC PTS/SCR.
            timestamp = Signal(32)
            self.sync += timestamp.eq(timestamp + 1)
            self.comb += self.uvc.timestamp.eq(timestamp)
            self.comb += self.gpif.eop_data.eq(self.uvc.next_header0)

            self.source_sel = CSRStorage(2, description="Stream source: 0 = Counter, 1 = UVC Pattern, 2 = Raw Pattern.")
            self.comb += [
                Case(self.source_sel.storage, {
                    0: self.gen.source.connect(self.gpif.sink),
                    1: [self.pattern.source.connect(self.uvc.sink), self.uvc.source.connect(self.gpif.sink)],
                    2: self.pattern.source.connect(self.gpif.sink, omit={"last"}),
                })
            ]
            platform.add_period_constraint(fx3.pclk, 1e9/100e6)
            platform.add_false_path_constraints(self.crg.cd_sys.clk, self.gpif.cd_gpif.clk)
        if with_pintest:
            fx3_gpio = [platform.request("fx3_gpio", i) for i in (1, 2)]
            self.pintest = PinTest(
                step_pin = fx3_gpio[1], # FX3 GPIO45.
                pins     = [fx3.dq[i] for i in range(32)] + [fx3.ctl[i] for i in range(9)] +
                           [fx3.pclk, fx3_gpio[0]],
            )

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteCamLink gateware for the Cam Link 4K.")
    parser.add_argument("--build",        action="store_true", help="Build bitstream.")
    parser.add_argument("--no-compile",   action="store_true", help="Generate build files without running the toolchain.")
    parser.add_argument("--load",         action="store_true", help="Load bitstream (through the FX3, see software/camlink.py).")
    parser.add_argument("--sys-clk-freq", default=100e6, type=float, help="System clock frequency.")
    parser.add_argument("--with-pintest", action="store_true",       help="Enable FX3 <-> FPGA pin test.")
    args = parser.parse_args()

    soc     = BaseSoC(sys_clk_freq=args.sys_clk_freq, with_pintest=args.with_pintest)
    builder = Builder(soc, output_dir="build", csr_csv="build/csr.csv")
    builder.build(build_name="litecamlink", run=args.build and not args.no_compile)

    if args.load:
        bitstream = os.path.join(builder.gateware_dir, "litecamlink.bit")
        os.system(f"python3 software/camlink.py fpga-load {bitstream}")

if __name__ == "__main__":
    main()
