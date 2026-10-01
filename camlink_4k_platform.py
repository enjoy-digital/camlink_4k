#
# This file is part of CamLink 4K.
#
# Copyright (c) 2019-2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause
#
# Elgato Cam Link 4K (1st gen, USB 0fd9:0066) pinout.
#
# Sources:
# - LiteX-Boards camlink_4k platform (clk27, LEDs, DDR3, verified with LiteX/Linux in 2019).
# - apertus/Greg Davill WIP netlist spreadsheet (FX3/IT6802/I2C/I2S), see doc/pinout.csv.
#
# Board overview:
# - FPGA: Lattice ECP5 LFE5U-25F-8BG381C, configured by the FX3 in Slave-SPI mode (CFG=001).
# - USB : Cypress FX3 CYUSB3014, 32-bit GPIF-II to the FPGA (right banks 2/3).
# - HDMI: ITE IT6802 receiver, parallel video to the FPGA (top banks 0/1), I2S audio.
# - DRAM: Micron MT41K64M16TW DDR3L (128MB, x16).
# - I2C : Shared between FX3 (master), FPGA, IT6802 (0x30-0x32 + EDID 0x50).
# - Banks 0-3 are 3.3V (BANK.VCCIO 3V3 in the stock bitstream), banks 6/7 DDR3 (stock: SSTL15).

from litex.build.generic_platform import *
from litex.build.lattice import LatticePlatform

# IOs ----------------------------------------------------------------------------------------------

_io = [
    # Clk.
    ("clk27", 0, Pins("B11"), IOStandard("LVCMOS33")),

    # Leds (A6/C11 on LED #1, A9 on LED #2 footprint, not populated on all boards).
    ("user_led", 0, Pins("A6"), IOStandard("LVCMOS33")),
    ("user_led", 1, Pins("A9"), IOStandard("LVCMOS33")),

    # Serial (on LED pins, requires external wiring).
    ("serial", 0,
        Subsignal("tx", Pins("A6")), # LED #1.
        Subsignal("rx", Pins("A9")), # LED #2.
        IOStandard("LVCMOS33")
    ),

    # DDR3 SDRAM.
    ("ddram", 0,
        Subsignal("a", Pins(
            "P2 L2 N1 P1 N5 M1 M3 N4",
            "L3 L1 P5 N2 N3"),
            IOStandard("SSTL135_I")),
        Subsignal("ba",    Pins("C4 A3 B4"), IOStandard("SSTL135_I")),
        Subsignal("ras_n", Pins("D3"), IOStandard("SSTL135_I")),
        Subsignal("cas_n", Pins("C3"), IOStandard("SSTL135_I")),
        Subsignal("we_n",  Pins("D5"), IOStandard("SSTL135_I")),
        Subsignal("cs_n",  Pins("B5"), IOStandard("SSTL135_I")),
        Subsignal("dm", Pins("J4 H5"), IOStandard("SSTL135_I")),
        Subsignal("dq", Pins(
            "L5 F1 K4 G1 L4 H1 G2 J3",
            "D1 C1 E2 C2 F3 A2 E1 B1"),
            IOStandard("SSTL135_I"),
            Misc("TERMINATION=75")),
        Subsignal("dqs_p", Pins("K2 H4"), IOStandard("SSTL135D_I"),
            Misc("TERMINATION=OFF"),
            Misc("DIFFRESISTOR=100")),
        Subsignal("clk_p",   Pins("A4"), IOStandard("SSTL135D_I")),
        Subsignal("cke",     Pins("E4"), IOStandard("SSTL135_I")),
        Subsignal("odt",     Pins("B3"), IOStandard("SSTL135_I")),
        Subsignal("reset_n", Pins("C5"), IOStandard("SSTL135_I")),
        Misc("SLEWRATE=FAST"),
    ),

    # FX3 GPIF-II (32-bit, FPGA side). dq[i]/ctl[i] follow FX3 DQ[i]/CTL[i] numbering.
    ("fx3", 0,
        Subsignal("pclk", Pins("L19")), # FX3 GPIO16 (PCLKT3_0).
        Subsignal("dq", Pins(
            # DQ0-15: FX3 GPIO0-15.
            "J16 H17 J17 H18 F17 H16 K18 G19",
            "K19 J19 K20 J20 J18 H20 G18 E17",
            # DQ16-27: FX3 GPIO33-44.
            "F20 G20 E19 E20 F19 D19 D20 D17",
            "E18 C20 D18 F16",
            # DQ28-31: FX3 GPIO46-49.
            "F18 C18 G16 E16")),
        # CTL0-5: FX3 GPIO17-22, CTL7: GPIO24, CTL11: GPIO28, CTL12: GPIO29.
        Subsignal("ctl", Pins("L17 M18 N16 M17 N18 P17 R17 T20 U20")),
        Subsignal("reset_n", Pins("P20"), Misc("PULLMODE=UP")), # FX3 RESET# (FX3Watchdog).
        IOStandard("LVCMOS33"),
    ),

    # FX3 extra GPIOs.
    ("fx3_gpio", 0, Pins("D7"), IOStandard("LVCMOS33")), # FX3 GPIO26 (not direct, "TR?" in netlist).
    ("fx3_gpio", 1, Pins("C7"), IOStandard("LVCMOS33")), # FX3 GPIO27.
    ("fx3_gpio", 2, Pins("C8"), IOStandard("LVCMOS33"), Misc("PULLMODE=DOWN")), # FX3 GPIO45 (heartbeat).
    # Note: GPIF dq/ctl/pclk and fx3_gpio 1/2 verified on hardware with PinTest (2026-09-28).

    # Shared I2C (FX3 GPIO58/59, IT6802 PCSCL/PCSDA).
    ("i2c", 0,
        Subsignal("scl", Pins("P18")),
        Subsignal("sda", Pins("P19")),
        IOStandard("LVCMOS33"), Misc("PULLMODE=NONE"),
    ),

    # IT6802 HDMI Receiver (24-bit output: QE4-11, QE16-23, QE28-35).
    ("hdmi_in", 0,
        Subsignal("pclk",  Pins("D11")), # PCLKT1_1.
        Subsignal("de",    Pins("C16")),
        Subsignal("hsync", Pins("D16")),
        Subsignal("vsync", Pins("B17")),
        Subsignal("qe", Pins(
            "A7  A8  E9  B9  B6  E6  D6  E7",   # QE4-11.
            "A12 A13 B13 C13 D13 E13 A14 C14",  # QE16-23.
            "D14 E14 B15 C15 D15 E15 A16 B16")), # QE28-35.
        Subsignal("rst_n", Pins("R20")), # SYSRSTN.
        IOStandard("LVCMOS33"),
    ),

    # IT6802 I2S Audio.
    ("i2s", 0,
        Subsignal("mclk", Pins("A10")), # PCLKT0_1.
        Subsignal("sck",  Pins("B12")), # PCLKT1_0.
        Subsignal("ws",   Pins("A17")),
        Subsignal("sd",   Pins("B18")), # I2S0.
        IOStandard("LVCMOS33"),
    ),
]

# Platform -----------------------------------------------------------------------------------------

class Platform(LatticePlatform):
    default_clk_name   = "clk27"
    default_clk_period = 1e9/27e6

    def __init__(self, toolchain="trellis", **kwargs):
        LatticePlatform.__init__(self, "LFE5U-25F-8BG381C", _io, toolchain=toolchain, **kwargs)
        # Keep the Slave-SPI port enabled after configuration: the FX3 (re)configures the FPGA.
        self.add_platform_command("SYSCONFIG SLAVE_SPI_PORT=ENABLE;")

    def do_finalize(self, fragment):
        LatticePlatform.do_finalize(self, fragment)
        self.add_period_constraint(self.lookup_request("clk27", loose=True), 1e9/27e6)
