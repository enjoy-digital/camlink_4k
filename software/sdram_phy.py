#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteDRAM PHY settings and DDR3 init sequence from the generated sdram_phy.h (host DRAM tools,
FX3 firmware generator)."""

import re

# DFII ---------------------------------------------------------------------------------------------

DFII_CONTROL_SEL     = 0x01
DFII_CONTROL_CKE     = 0x02
DFII_CONTROL_ODT     = 0x04
DFII_CONTROL_RESET_N = 0x08

DFII_COMMAND_CS     = 0x01
DFII_COMMAND_WE     = 0x02
DFII_COMMAND_CAS    = 0x04
DFII_COMMAND_RAS    = 0x08
DFII_COMMAND_WRDATA = 0x10
DFII_COMMAND_RDDATA = 0x20

# PHY settings (from sdram_phy.h) ------------------------------------------------------------------

class PhySettings:
    def __init__(self, header):
        self.header = open(header).read()
        def define(name, default=None):
            m = re.search(rf"#define {name} (\S+)", self.header)
            return int(m.group(1).rstrip("UL"), 0) if m else default
        self.phases     = define("SDRAM_PHY_PHASES")
        self.databits   = define("SDRAM_PHY_DATABITS")
        self.dfi_databits = define("SDRAM_PHY_DFI_DATABITS")
        self.rdphase    = define("SDRAM_PHY_RDPHASE")
        self.wrphase    = define("SDRAM_PHY_WRPHASE")
        self.modules    = define("SDRAM_PHY_MODULES")
        self.delays     = define("SDRAM_PHY_DELAYS")
        self.bitslips   = define("SDRAM_PHY_BITSLIPS")
        self.memory     = define("SDRAM_PHY_SUPPORTED_MEMORY")

    def init_sequence(self):
        """Parse init_sequence() into (kind, args) steps."""
        body  = self.header.split("static inline void init_sequence(void)", 1)[1]
        body  = body.split("\n}\n", 1)[0]
        steps = []
        flags = {
            "DFII_CONTROL_SEL": DFII_CONTROL_SEL, "DFII_CONTROL_CKE": DFII_CONTROL_CKE,
            "DFII_CONTROL_ODT": DFII_CONTROL_ODT, "DFII_CONTROL_RESET_N": DFII_CONTROL_RESET_N,
            "DFII_COMMAND_CS": DFII_COMMAND_CS, "DFII_COMMAND_WE": DFII_COMMAND_WE,
            "DFII_COMMAND_CAS": DFII_COMMAND_CAS, "DFII_COMMAND_RAS": DFII_COMMAND_RAS,
        }
        def value(expr):
            expr = expr.strip()
            if re.fullmatch(r"(0x)?[0-9a-fA-F]+", expr):
                return int(expr, 0)
            v = 0
            for f in expr.split("|"):
                v |= flags[f.strip()]
            return v
        for line in body.splitlines():
            line = line.strip()
            m = re.match(r"(\w+)\((.*)\);", line)
            if not m:
                continue
            fn, arg = m.groups()
            if fn == "cdelay":
                steps.append(("delay", int(arg)))
            elif fn == "sdram_dfii_control_write":
                steps.append(("control", value(arg)))
            else:
                m2 = re.match(r"sdram_dfii_pi(\d)_(address|baddress)_write", fn)
                m3 = re.match(r"command_p(\d)", fn)
                if m2:
                    steps.append((m2.group(2), int(m2.group(1)), value(arg)))
                elif m3:
                    steps.append(("command", int(m3.group(1)), value(arg)))
                else:
                    raise ValueError(f"Unsupported init step: {line}")
        return steps
