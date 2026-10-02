#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2019-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CamLink 4K: LiteX based gateware for the Elgato Cam Link 4K."""

import os
import sys
import argparse
import subprocess

from migen import *
from migen.fhdl.specials import Tristate

from litex.gen import *

from litex.soc.integration.soc_core import *
from litex.soc.integration.builder  import *
from litex.soc.cores.led            import LedChaser
from litex.soc.cores.freqmeter      import FreqMeter
from litex.soc.interconnect.csr     import CSRStorage, CSRField

from litedram.modules import MT41K64M16
from litedram.frontend.bist import LiteDRAMBISTGenerator, LiteDRAMBISTChecker
from litedram.core.controller import ControllerSettings

from litex.soc.interconnect import stream

from camlink_4k_platform import Platform

from gateware.crg     import CRG
from gateware.pintest    import PinTest
from gateware.i2c_bridge import I2CBridge
from gateware.gpif       import GPIFStreamer, CounterGenerator
from gateware.video      import VideoPatternGenerator
from gateware.uvc        import UVCPacketizer
from gateware.hdmi_in    import HDMIIn
from gateware.audio      import AudioSource
from gateware.color      import ColorAdjust
from gateware.canvas     import Canvas
from gateware.watchdog   import FX3Watchdog
from gateware.framebuffer import NV12FrameBuffer
from gateware.ecp5ddrphy import ECP5DDRPHY, ecp5ddrphy_with_ratio
from gateware.dram       import LiteDRAMNativePortBuffer, MT41K64M16_4Banks

from litex.build.generic_platform import Pins, IOStandard, Subsignal

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=100e6, toolchain="trellis",
        with_cpu     = False,
        with_sdram   = False,
        sdram_rate   = "1:2",
        sdram_sys_clk_src = "clkdivf",
        sdram_sys_phase   = 0,
        with_sdram_bist   = False,
        sdram_banks       = 8,
        sdram_lat_adj     = (0, 0),
        video_clk_freq    = None,
        gpif_io_regs      = ("ctl",),
        with_framebuffer  = False,
        with_pintest = False,
        ):
        platform = Platform(toolchain=toolchain)

        # CRG --------------------------------------------------------------------------------------
        self.crg = CRG(platform, sys_clk_freq,
            sdram_rate  = sdram_rate if with_sdram else None,
            sys_clk_src = sdram_sys_clk_src,
            sys_phase   = sdram_sys_phase,
            video_clk_freq = video_clk_freq,
        )

        # SoCCore ----------------------------------------------------------------------------------
        # Optional VexRiscv + BIOS (DRAM init/debug); console on a UART crossover (CSRs, host access
        # through the I2C bridge, see software/camlink.py term).
        SoCCore.__init__(self, platform, sys_clk_freq,
            ident                = "CamLink 4K SoC on Cam Link 4K.",
            cpu_type             = "vexriscv" if with_cpu else None,
            cpu_variant          = "minimal",
            integrated_rom_size  = 0xa000 if with_cpu else 0, # BIOS with DRAM init/leveling.
            integrated_sram_size = 0x1000 if with_cpu else 0,
            uart_name            = "crossover" if with_cpu else "stub",
            with_uart            = with_cpu,
            with_timer           = with_cpu,
        )

        # Yosys: no read/write collision emulation on the M420 UV line buffers (banks never written
        # and read at the same time; the emulation logic after the EBR outputs was the HDMI clock
        # critical path).
        platform.toolchain._yosys_cmds.append("setattr -set no_rw_check 1 m:*m420_uvbuf*")

        # DDR3 SDRAM -------------------------------------------------------------------------------
        # - 1:2: ECP5DDRPHY at sys (DRAM clock = 2 x sys).
        # - 1:4: ECP5DDRPHY at sys2x behind a DFI rate converter (DRAM clock = 4 x sys, e.g. 100MHz
        #   sys -> DDR3-800, stock-like bandwidth).
        if with_sdram:
            phy_cls = ECP5DDRPHY if sdram_rate == "1:2" else ecp5ddrphy_with_ratio(2)
            self.ddrphy = phy_cls(platform.request("ddram"), sys_clk_freq=sys_clk_freq)
            # Debug: controller read/write latency offsets (sys cycles) and write phase offset (1:4
            # bring-up).
            self.ddrphy.settings.read_latency  += sdram_lat_adj[0]
            self.ddrphy.settings.write_latency += sdram_lat_adj[1]
            if len(sdram_lat_adj) > 2:
                self.ddrphy.settings.wrphase   += sdram_lat_adj[2]
            self.comb += [
                self.crg.stop.eq(self.ddrphy.init.stop),
                self.crg.reset.eq(self.ddrphy.init.reset),
            ]
            if sdram_rate == "1:4":
                # sys/sys2x phase control (see CRG): ALIGNWD pulse and sys2x reset (host/firmware
                # DRAM init retries with the other phase when the BIST check fails).
                self.crg_phase = CSRStorage(fields=[
                    CSRField("alignwd",   size=1, offset=0,
                        description="sys2x CLKDIVF ALIGNWD (pulse: 1 then 0)."),
                    CSRField("sys2x_rst", size=1, offset=1,
                        description="sys2x domain reset (pulse: 1 then 0)."),
                    CSRField("phy_init",  size=1, offset=2,
                        description="DDR PHY init sequence replay (pulse: 1 then 0, resets sys)."),
                ])
                self.comb += [
                    self.crg.alignwd.eq(self.crg_phase.fields.alignwd),
                    self.crg.sys2x_rst.eq(self.crg_phase.fields.sys2x_rst),
                    self.crg.phy_init.eq(self.crg_phase.fields.phy_init),
                ]
            self.add_sdram("sdram",
                phy           = self.ddrphy,
                module        = (MT41K64M16_4Banks if sdram_banks == 4 else MT41K64M16)(
                    sys_clk_freq, sdram_rate),
                l2_cache_size = 0,
                # Unbuffered bank machine command buffers: a `cmd_buffer_buffered=True` build lost
                # frame buffer reads on hardware (accepted by the crossbar, never answered); cause
                # not reproduced in simulation (doc/upstream/README.md).
                controller_settings = ControllerSettings(
                    cmd_buffer_depth    = 4,
                    cmd_buffer_buffered = False,
                ),
            )
            # BIST (bandwidth/integrity tests from the host, no CPU needed): generator (writes) and
            # checker (reads) on separate ports (concurrent read/write = frame buffer traffic).
            if with_sdram_bist:
                self.sdram_generator_port = LiteDRAMNativePortBuffer(self.sdram.crossbar.get_port())
                self.sdram_checker_port   = LiteDRAMNativePortBuffer(self.sdram.crossbar.get_port())
                self.sdram_generator = LiteDRAMBISTGenerator(self.sdram_generator_port.port)
                self.sdram_checker   = LiteDRAMBISTChecker(self.sdram_checker_port.port)

        # Leds -------------------------------------------------------------------------------------
        self.leds = LedChaser(
            pads         = platform.request_all("user_led"),
            sys_clk_freq = sys_clk_freq,
        )

        # I2C Bridge (Host access through the FX3 I2C master) --------------------------------------
        self.i2c_bridge = I2CBridge(platform.request("i2c"), address=0x10)
        self.bus.add_master(name="i2c_bridge", master=self.i2c_bridge.bus)

        # Video Clock Domain -----------------------------------------------------------------------
        # The video pipeline (HDMI frame FIFO read side -> canvas/color -> UVC -> GPIF input) runs
        # in `video`: sys itself, or its own ~100MHz clock when sys is set by the DRAM (e.g.
        # 87.75MHz for DDR3-700 at 1:4, too slow for 4K30: ~93-97M words/s). Only quasi-static CSRs
        # cross.
        vd    = "video" if video_clk_freq else "sys"
        vfreq = video_clk_freq or sys_clk_freq
        VR    = ((lambda m: ClockDomainsRenamer({"sys": "video"})(m)) if video_clk_freq else
                 (lambda m: m))
        self.add_constant("CONFIG_VIDEO_CLOCK_FREQUENCY", int(vfreq))
        if video_clk_freq:
            platform.add_false_path_constraints(self.crg.cd_sys.clk, self.crg.cd_video.clk)

        # HDMI Receiver (IT6802) -------------------------------------------------------------------
        hdmi_in = platform.request("hdmi_in")
        self.hdmi_rst = CSRStorage(description="IT6802 reset (1 = held in reset).")
        self.comb += hdmi_in.rst_n.eq(~self.hdmi_rst.storage)
        self.hdmi_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=hdmi_in.pclk)
        self.hdmi_in = VR(HDMIIn(hdmi_in))
        platform.add_period_constraint(hdmi_in.pclk, 1e9/150e6)
        platform.add_false_path_constraints(self.crg.cd_sys.clk, self.hdmi_in.cd_hdmi.clk)

        # PinTest ----------------------------------------------------------------------------------
        fx3 = platform.request("fx3")
        if not with_pintest:
            self.fx3_clk_freq = FreqMeter(period=int(sys_clk_freq), clk=fx3.pclk)

            # FX3 Watchdog (heartbeat: FX3 GPIO45, reset: FX3 RESET#) -----------------------------
            self.fx3_watchdog = FX3Watchdog(platform.request("fx3_gpio", 2), sys_clk_freq)
            self.specials += Tristate(fx3.reset_n, o=0, oe=self.fx3_watchdog.reset)

            # GPIF Streamer ------------------------------------------------------------------------
            self.gpif = VR(GPIFStreamer(fx3, with_audio=True, io_regs=gpif_io_regs))

            # Audio (IT6802 I2S) -> GPIF thread 1.
            self.audio = VR(AudioSource(platform.request("i2s"), vfreq))
            self.comb += self.audio.source.connect(self.gpif.audio_sink)

            # Sources: Counter (raw) / Video Pattern -> UVC Packetizer.
            self.gen     = VR(CounterGenerator())
            self.pattern = VR(VideoPatternGenerator(vfreq))
            self.uvc     = ResetInserter()(VR(UVCPacketizer()))
            # HDMI: brightness/contrast/saturation (UVC Processing Unit), window centered in the UVC
            # frame, register stage (timing: HDMI frame FIFO BRAM -> canvas -> FIFO read).
            self.color    = ResetInserter()(VR(ColorAdjust()))
            self.canvas   = ResetInserter()(VR(Canvas()))
            self.hdmi_buf = ResetInserter()(VR(stream.Buffer([("data", 32)],
                pipe_valid = True,
                pipe_ready = True,
            )))

            # Timestamp (video clock) for UVC PTS/SCR.
            timestamp = Signal(32)
            if video_clk_freq:
                self.sync.video += timestamp.eq(timestamp + 1)
            else:
                self.sync += timestamp.eq(timestamp + 1)
            self.comb += self.uvc.timestamp.eq(timestamp)
            self.comb += self.gpif.eop_data.eq(self.uvc.next_header0)
            # UVC packetizer held in reset while no video source is enabled (restarts on a frame
            # boundary).
            video_off = Signal()
            self.comb += [
                video_off.eq(~self.pattern.enable & ~self.hdmi_in.enable),
                self.uvc.reset.eq(video_off),
                # No stale words across restarts (HDMI path stages).
                self.hdmi_buf.reset.eq(video_off),
                self.canvas.reset.eq(video_off),
                self.color.reset.eq(video_off),
            ]

            # NV12 frame buffer (DRAM): HDMI M420 frames -> DRAM slots -> NV12 frames (UVC).
            if with_framebuffer:
                assert with_sdram and video_clk_freq
                # Video domain ports with explicit CDCs: the crossbar has no read data
                # backpressure, the read data CDC FIFO must hold all the outstanding reads of the
                # frame buffer reader (256; the default 16-deep CDC lost read data and the reader
                # stalled on hardware).
                from litedram.common import LiteDRAMNativePort
                from litedram.frontend.adapter import LiteDRAMNativePortCDC
                def video_port():
                    sys_port = self.sdram.crossbar.get_port()
                    port     = LiteDRAMNativePort("both",
                        sys_port.address_width,
                        sys_port.data_width,
                        clock_domain = "video",
                    )
                    self.submodules += LiteDRAMNativePortCDC(port, sys_port,
                        cmd_depth=16, wdata_depth=64, rdata_depth=256)
                    return port
                fb_wport = video_port()
                fb_rport = video_port()
                self.framebuffer = VR(NV12FrameBuffer(fb_wport, fb_rport))
                # Register stage (timing: HDMI frame FIFO BRAM -> frame buffer writer).
                self.fb_buf = ResetInserter()(VR(stream.Buffer([("data", 32)],
                    pipe_valid = True,
                    pipe_ready = True,
                )))
                # Register stage (timing: GPIF CDC FIFO -> UVC -> frame buffer reader ready chain).
                self.fb_out_buf = ResetInserter()(VR(stream.Buffer([("data", 32)],
                    pipe_valid = True,
                    pipe_ready = True,
                )))

            self.source_sel = CSRStorage(3, description="Stream source: 0 = Counter, "
                "1 = UVC Pattern, 2 = Raw Pattern, 3 = UVC HDMI, 4 = UVC HDMI NV12 (DRAM frame "
                "buffer), 5 = UVC Pattern NV12 (M420 pattern through the frame buffer).")
            # Source mux -> register stage (timing: FIFO BRAM -> mux -> CDC BRAM) -> GPIF.
            # Timing: ready chain.
            self.gpif_buf = gpif_buf = VR(stream.Buffer([("data", 32), ("next", 32)],
                pipe_ready = True,
            ))
            # Frame buffer not used (sources 4/5).
            fb_off = Signal()
            self.comb += fb_off.eq(video_off |
                ((self.source_sel.storage != 4) & (self.source_sel.storage != 5)))
            self.comb += [
                Case(self.source_sel.storage, {
                    0: self.gen.source.connect(gpif_buf.sink),
                    1: [
                        self.pattern.source.connect(self.uvc.sink),
                        self.uvc.source.connect(gpif_buf.sink),
                    ],
                    2: self.pattern.source.connect(gpif_buf.sink, omit={"last"}),
                    3: [
                        self.hdmi_in.source.connect(self.hdmi_buf.sink),
                        self.hdmi_buf.source.connect(self.canvas.sink),
                        self.canvas.source.connect(self.color.sink),
                        self.color.source.connect(self.uvc.sink),
                        self.uvc.source.connect(gpif_buf.sink),
                    ],
                    **({src: [
                        (self.hdmi_in.source if src == 4 else self.pattern.source).connect(
                            self.fb_buf.sink),
                        self.fb_buf.source.connect(self.framebuffer.sink),
                        self.framebuffer.source.connect(self.fb_out_buf.sink),
                        self.fb_out_buf.source.connect(self.uvc.sink),
                        self.uvc.source.connect(gpif_buf.sink),
                    ] for src in (4, 5)} if with_framebuffer else {}),
                }),
                gpif_buf.source.connect(self.gpif.sink),
                # HDMI frames only admitted when the canvas can consume them from their start.
                self.hdmi_in.admit.eq((self.source_sel.storage != 3) | self.canvas.admit),
                # Frame buffer stopped (not reset: DRAM accesses in flight must complete).
                *([
                    self.framebuffer.stop.eq(fb_off),
                    self.fb_buf.reset.eq(fb_off),
                    self.fb_out_buf.reset.eq(fb_off),
                  ] if with_framebuffer else []),
            ]
            # FX3 PLL at 403.2MHz (4K30).
            platform.add_period_constraint(fx3.pclk, 1e9/100.8e6)
            platform.add_false_path_constraints(self.crg.cd_sys.clk, self.gpif.cd_gpif.clk)
            if video_clk_freq:
                platform.add_false_path_constraints(self.crg.cd_video.clk, self.gpif.cd_gpif.clk)
        if with_pintest:
            fx3_gpio = [platform.request("fx3_gpio", i) for i in (1, 2)]
            self.pintest = PinTest(
                step_pin = fx3_gpio[1], # FX3 GPIO45.
                pins     = [fx3.dq[i] for i in range(32)] + [fx3.ctl[i] for i in range(9)] +
                           [fx3.pclk, fx3_gpio[0]],
            )

# Build --------------------------------------------------------------------------------------------

# Build Variants -----------------------------------------------------------------------------------

VARIANTS = {
    # 4K30 NV12 through the DRAM frame buffer (DDR3-594 1:4, video pipeline in its own 99MHz
    # domain), all the base features (default, flashed image).
    "nv12": dict(
        sys_clk_freq     = 74.25e6,
        with_sdram       = True,
        sdram_rate       = "1:4",
        sdram_banks      = 4,
        with_sdram_bist  = True,
        video_clk_freq   = 99e6,
        with_framebuffer = True,
        seed             = 8,
    ),
    # Without DRAM (YUY2/M420, 100MHz sys).
    "base": dict(
        sys_clk_freq     = 100e6,
        with_sdram       = False,
        sdram_rate       = "1:2",
        sdram_banks      = 8,
        with_sdram_bist  = False,
        video_clk_freq   = 0,
        with_framebuffer = False,
        seed             = 1,
    ),
}

def main():
    parser = argparse.ArgumentParser(description="CamLink 4K gateware for the Cam Link 4K.")
    parser.add_argument("--variant",           default="nv12", choices=list(VARIANTS),
        help="Build variant (defaults of the options below).")
    parser.add_argument("--build",             action="store_true",
        help="Build bitstream.")
    parser.add_argument("--no-compile",        action="store_true",
        help="Generate build files without running the toolchain.")
    parser.add_argument("--load",              action="store_true",
        help="Load bitstream (through the FX3, see software/camlink.py).")
    parser.add_argument("--sys-clk-freq",      default=None, type=float,
        help="System clock frequency.")
    parser.add_argument("--with-pintest",      action="store_true",
        help="Enable FX3 <-> FPGA pin test.")
    parser.add_argument("--with-cpu",          action="store_true",
        help="Enable VexRiscv CPU + BIOS (console over UART crossover).")
    parser.add_argument("--with-sdram",        action=argparse.BooleanOptionalAction,
        help="Enable DDR3 SDRAM.")
    parser.add_argument("--sdram-rate",        default=None, choices=["1:2", "1:4"],
        help="Controller:DRAM clock ratio.")
    parser.add_argument("--sdram-sys-clk-src", default="clkdivf", choices=["clkdivf", "pll"],
        help="1:4 sys clock source.")
    parser.add_argument("--with-framebuffer",  action=argparse.BooleanOptionalAction,
        help="NV12 DRAM frame buffer (needs --with-sdram and --video-clk-freq).")
    parser.add_argument("--gpif-io-regs",      default="ctl",
        help="Debug: GPIF signal groups registered in the IO cells (dq,ctl).")
    parser.add_argument("--video-clk-freq",    default=None, type=float,
        help="Video pipeline clock (own domain, DRAM builds with a slow sys, 0: sys).")
    parser.add_argument("--sdram-lat-adj",     default="0,0",
        help="Debug: controller read,write latency offsets (sys cycles)[,wrphase offset].")
    parser.add_argument("--sdram-sys-phase",   default=0, type=float,
        help="1:4 sys clock phase (degrees, pll source).")
    parser.add_argument("--with-sdram-bist",   action=argparse.BooleanOptionalAction,
        help="Add DRAM BIST generator/checker (firmware DRAM init check).")
    parser.add_argument("--sdram-banks",       default=None, type=int, choices=[4, 8],
        help="DRAM banks used (4: BA2=0, 64MB, timing).")
    parser.add_argument("--output-dir",        default="build",
        help="Build directory.")
    parser.add_argument("--seed",              default=None, type=int,
        help="Nextpnr seed.")
    args = parser.parse_args()
    for k, v in VARIANTS[args.variant].items():
        if getattr(args, k) is None:
            setattr(args, k, v)
    args.video_clk_freq = args.video_clk_freq or None

    soc     = BaseSoC(
        sys_clk_freq      = args.sys_clk_freq,
        with_cpu          = args.with_cpu,
        with_sdram        = args.with_sdram,
        sdram_rate        = args.sdram_rate,
        sdram_sys_clk_src = args.sdram_sys_clk_src,
        sdram_sys_phase   = args.sdram_sys_phase,
        with_sdram_bist   = args.with_sdram_bist,
        sdram_banks       = args.sdram_banks,
        sdram_lat_adj     = tuple(int(x) for x in args.sdram_lat_adj.split(",")),
        video_clk_freq    = args.video_clk_freq,
        gpif_io_regs      = tuple(x for x in args.gpif_io_regs.split(",") if x),
        with_framebuffer  = args.with_framebuffer,
        with_pintest      = args.with_pintest,
    )
    builder = Builder(soc,
        output_dir = args.output_dir,
        csr_csv    = os.path.join(args.output_dir, "csr.csv"),
    )
    builder.build(build_name="camlink_4k", run=args.build and not args.no_compile, seed=args.seed)

    if args.load:
        bitstream  = os.path.join(builder.gateware_dir, "camlink_4k.bit")
        camlink_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
            "software", "camlink.py")
        subprocess.run([sys.executable, camlink_py, "fpga-load", bitstream], check=True)

if __name__ == "__main__":
    main()
