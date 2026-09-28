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

from litedram.modules import MT41K64M16
from litedram.phy     import ECP5DDRPHY

from litex.soc.interconnect import stream

from litecamlink_platform import Platform

from litecamlink.gateware.crg     import CRG
from litecamlink.gateware.pintest    import PinTest
from litecamlink.gateware.i2c_bridge import I2CBridge
from litecamlink.gateware.gpif       import GPIFStreamer, CounterGenerator
from litecamlink.gateware.video      import VideoPatternGenerator
from litecamlink.gateware.uvc        import UVCPacketizer
from litecamlink.gateware.hdmi_in    import HDMIIn
from litecamlink.gateware.ioscan     import IOScan
from litecamlink.gateware.audio      import AudioSource
from litecamlink.gateware.color      import ColorAdjust

from litex.build.generic_platform import Pins, IOStandard, Subsignal

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=100e6, toolchain="trellis",
        with_cpu     = False,
        with_sdram   = False,
        with_pintest = False,
        with_ioscan  = False,
        ):
        platform = Platform(toolchain=toolchain)

        # CRG --------------------------------------------------------------------------------------
        self.crg = CRG(platform, sys_clk_freq, with_sdram=with_sdram)

        # SoCCore ----------------------------------------------------------------------------------
        # Optional VexRiscv + BIOS (DRAM init/debug); console on a UART crossover (CSRs, host access
        # through the I2C bridge, see software/camlink.py term).
        SoCCore.__init__(self, platform, sys_clk_freq,
            ident                = "LiteCamLink SoC on Cam Link 4K.",
            cpu_type             = "vexriscv" if with_cpu else None,
            cpu_variant          = "minimal",
            integrated_rom_size  = 0x8000 if with_cpu else 0,
            integrated_sram_size = 0x1000 if with_cpu else 0,
            uart_name            = "crossover" if with_cpu else "stub",
            with_uart            = with_cpu,
            with_timer           = with_cpu,
        )

        # DDR3 SDRAM -------------------------------------------------------------------------------
        if with_sdram:
            self.ddrphy = ECP5DDRPHY(platform.request("ddram"), sys_clk_freq=sys_clk_freq)
            self.comb += self.crg.stop.eq(self.ddrphy.init.stop)
            self.add_sdram("sdram",
                phy           = self.ddrphy,
                module        = MT41K64M16(sys_clk_freq, "1:2"),
                l2_cache_size = 0,
            )

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
        self.hdmi_rst = CSRStorage(description="IT6802 reset (1 = held in reset).")
        self.comb += hdmi_in.rst_n.eq(~self.hdmi_rst.storage)
        self.hdmi_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=hdmi_in.pclk)
        self.hdmi_in = HDMIIn(hdmi_in)
        platform.add_period_constraint(hdmi_in.pclk, 1e9/150e6)
        platform.add_false_path_constraints(self.crg.cd_sys.clk, self.hdmi_in.cd_hdmi.clk)

        # IO Scan (debug: locate unknown connections, qualified by HDMI DE) ------------------------
        if with_ioscan:
            scan_pins = "A11 B10 B8 C10 C11 C6 D8 D9 E8 A18 A19 B19 B20 C12 C17 D12 E11 E12"
            platform.add_extension([("ioscan", 0, Pins(scan_pins), IOStandard("LVCMOS33"))])
            ioscan_pads = platform.request("ioscan")
            self.ioscan = IOScan(
                pins      = [ioscan_pads[i] for i in range(len(ioscan_pads))] +
                            [self.hdmi_in.debug_qe[i] for i in range(24)],
                qualifier = self.hdmi_in.debug_de,
                cd        = "hdmi",
            )

        # PinTest ----------------------------------------------------------------------------------
        fx3 = platform.request("fx3")
        if not with_pintest:
            self.fx3_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=fx3.pclk)

            # GPIF Streamer ------------------------------------------------------------------------
            self.gpif = GPIFStreamer(fx3, with_audio=True)

            # Audio (IT6802 I2S) -> GPIF thread 1.
            self.audio = AudioSource(platform.request("i2s"), sys_clk_freq)
            self.comb += self.audio.source.connect(self.gpif.audio_sink)

            # Sources: Counter (raw) / Video Pattern -> UVC Packetizer.
            self.gen     = CounterGenerator()
            self.pattern = VideoPatternGenerator(sys_clk_freq)
            self.uvc     = ResetInserter()(UVCPacketizer())
            self.color   = ColorAdjust() # HDMI: brightness/contrast/saturation (UVC Processing Unit).

            # Timestamp (sys clock) for UVC PTS/SCR.
            timestamp = Signal(32)
            self.sync += timestamp.eq(timestamp + 1)
            self.comb += self.uvc.timestamp.eq(timestamp)
            self.comb += self.gpif.eop_data.eq(self.uvc.next_header0)
            # UVC packetizer held in reset while no video source is enabled (restarts on a frame
            # boundary).
            self.comb += self.uvc.reset.eq(~self.pattern._enable.storage & ~self.hdmi_in.control.fields.enable)

            self.source_sel = CSRStorage(2, description="Stream source: 0 = Counter, 1 = UVC Pattern, 2 = Raw Pattern, 3 = UVC HDMI.")
            # Source mux -> register stage (timing: FIFO BRAM -> mux -> CDC BRAM) -> GPIF.
            self.gpif_buf = gpif_buf = stream.Buffer([("data", 32)])
            self.comb += [
                Case(self.source_sel.storage, {
                    0: self.gen.source.connect(gpif_buf.sink),
                    1: [self.pattern.source.connect(self.uvc.sink), self.uvc.source.connect(gpif_buf.sink)],
                    2: self.pattern.source.connect(gpif_buf.sink, omit={"last"}),
                    3: [
                        self.hdmi_in.source.connect(self.color.sink),
                        self.color.source.connect(self.uvc.sink),
                        self.uvc.source.connect(gpif_buf.sink),
                    ],
                }),
                gpif_buf.source.connect(self.gpif.sink),
            ]
            platform.add_period_constraint(fx3.pclk, 1e9/100.8e6) # FX3 PLL at 403.2MHz (4K30).
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
    parser.add_argument("--with-cpu",     action="store_true",       help="Enable VexRiscv CPU + BIOS (console over UART crossover).")
    parser.add_argument("--with-sdram",   action="store_true",       help="Enable DDR3 SDRAM.")
    parser.add_argument("--with-ioscan",  action="store_true",       help="Enable IO scan debug core.")
    parser.add_argument("--seed",         default=1, type=int,       help="Nextpnr seed.")
    args = parser.parse_args()

    soc     = BaseSoC(
        sys_clk_freq = args.sys_clk_freq,
        with_cpu     = args.with_cpu,
        with_sdram   = args.with_sdram,
        with_pintest = args.with_pintest,
        with_ioscan  = args.with_ioscan,
    )
    builder = Builder(soc, output_dir="build", csr_csv="build/csr.csv")
    builder.build(build_name="litecamlink", run=args.build and not args.no_compile, seed=args.seed)

    if args.load:
        bitstream = os.path.join(builder.gateware_dir, "litecamlink.bit")
        os.system(f"python3 software/camlink.py fpga-load {bitstream}")

if __name__ == "__main__":
    main()
