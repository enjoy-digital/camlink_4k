#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""DDR3 bring-up without CPU: init, ECP5 read leveling and BIST tests through the I2C CSR bridge.

Build/boot (example, 1:4 at 99.5625MHz sys = DDR3-796):
    ./litecamlink.py --build --with-sdram --sdram-rate 1:4 --sdram-banks 4 --with-sdram-bist \\
        --sys-clk-freq 99.5625e6 --output-dir build_dram14
    make -C firmware/fx3 clean && make -C firmware/fx3 CSR_CSV=../../build_dram14/csr.csv
    python3 software/camlink.py boot --bit build_dram14/gateware/litecamlink.bit
    python3 software/dram.py --build build_dram14 init       # init + read leveling
    python3 software/dram.py --build build_dram14 memtest    # BIST write/read + errors
    python3 software/dram.py --build build_dram14 bandwidth  # write, read, write+read concurrent

The init sequence is replayed from the generated `sdram_phy.h` (same as the BIOS); read leveling
follows the BIOS ECP5 flow (per module: bitslip x delay scan with a DFII write/read pattern, best
bitslip, center of the longest passing delay window).
"""

import os
import re
import sys
import time
import random
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camlink import CamLink, CamLinkBus

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

# DRAM ---------------------------------------------------------------------------------------------

class DRAM:
    def __init__(self, build):
        self.build = build
        self.phy   = PhySettings(os.path.join(build, "software/include/generated/sdram_phy.h"))
        self.bus   = CamLinkBus(csr_csv=os.path.join(build, "csr.csv"))
        self.regs  = self.bus.regs

    # DFII helpers.
    def pi(self, n, name):
        return getattr(self.regs, f"sdram_dfii_pi{n}_{name}")

    def command(self, phase, cmd, address=0, bank=0):
        self.pi(phase, "address").write(address)
        self.pi(phase, "baddress").write(bank)
        self.pi(phase, "command").write(cmd)
        self.pi(phase, "command_issue").write(1)

    def software_control(self):
        self.regs.sdram_dfii_control.write(DFII_CONTROL_CKE | DFII_CONTROL_ODT | DFII_CONTROL_RESET_N)

    def hardware_control(self):
        self.regs.sdram_dfii_control.write(DFII_CONTROL_SEL)

    # Init.
    def init(self):
        for step in self.phy.init_sequence():
            kind = step[0]
            if kind == "delay":
                time.sleep(step[1]*1e-6 + 1e-3)
            elif kind == "control":
                self.regs.sdram_dfii_control.write(step[1])
            elif kind == "address":
                self.pi(step[1], "address").write(step[2])
            elif kind == "baddress":
                self.pi(step[1], "baddress").write(step[2])
            elif kind == "command":
                self.pi(step[1], "command").write(step[2])
                self.pi(step[1], "command_issue").write(1)

    # Pattern write/read through DFII (one BL8 burst at column `col` of row 0, bank 0).
    def write_read(self, pattern, col=0):
        p = self.phy
        self.command(0, DFII_COMMAND_RAS | DFII_COMMAND_CS, 0, 0)                     # Activate.
        for n in range(p.phases):
            self.pi(n, "wrdata").write(pattern[n])
        self.command(p.wrphase, DFII_COMMAND_CAS | DFII_COMMAND_WE | DFII_COMMAND_CS |
            DFII_COMMAND_WRDATA, col, 0)                                              # Write.
        self.command(p.rdphase, DFII_COMMAND_CAS | DFII_COMMAND_CS | DFII_COMMAND_RDDATA, col, 0)
        data = [self.pi(n, "rddata").read() for n in range(p.phases)]
        self.command(0, DFII_COMMAND_RAS | DFII_COMMAND_WE | DFII_COMMAND_CS, 0x400, 0) # Precharge all.
        return data

    def module_ok(self, module, tries=4):
        """Write/read random patterns, compare the byte lanes of `module`."""
        p     = self.phy
        mask  = 0
        beats = p.dfi_databits//p.databits
        for b in range(beats):
            mask |= 0xff << (b*p.databits + 8*module)
        for i in range(tries):
            pattern = [random.getrandbits(p.dfi_databits) for _ in range(p.phases)]
            data    = self.write_read(pattern, col=8*i)
            if any((d ^ w) & mask for d, w in zip(data, pattern)):
                return False
        return True

    # ECP5 read leveling.
    def select(self, module):
        self.regs.ddrphy_dly_sel.write(1 << module)

    def set_delay(self, delay):
        self.regs.ddrphy_rdly_dq_rst.write(1)
        for _ in range(delay):
            self.regs.ddrphy_rdly_dq_inc.write(1)

    def set_bitslip(self, bitslip):
        self.regs.ddrphy_rdly_dq_bitslip_rst.write(1)
        for _ in range(bitslip):
            self.regs.ddrphy_rdly_dq_bitslip.write(1)

    def read_leveling(self, verbose=True):
        p = self.phy
        self.software_control()
        results = {}
        for module in range(p.modules):
            self.select(module)
            best = None
            if verbose:
                print(f"m{module}:")
            for bitslip in range(p.bitslips):
                self.set_bitslip(bitslip)
                scores = []
                for delay in range(p.delays):
                    self.set_delay(delay)
                    scores.append(self.module_ok(module))
                if verbose:
                    print(f"  bitslip {bitslip}: " + "".join("1" if s else "0" for s in scores))
                # Longest passing window.
                start, length, cur = 0, 0, None
                for d, s in enumerate(scores + [False]):
                    if s and cur is None:
                        cur = d
                    if not s and cur is not None:
                        if d - cur > length:
                            start, length = cur, d - cur
                        cur = None
                if length and (best is None or length > best[2]):
                    best = (bitslip, start + length//2, length)
            if best is None:
                raise RuntimeError(f"Read leveling failed on module {module}.")
            self.set_bitslip(best[0])
            self.set_delay(best[1])
            results[module] = best
            if verbose:
                print(f"  -> bitslip {best[0]}, delay {best[1]} (window {best[2]})")
        self.regs.ddrphy_dly_sel.write(0)
        self.hardware_control()
        return results

    # BIST.
    def bist(self, name, base, length, random_data=True):
        r = lambda s: getattr(self.regs, f"sdram_{name}_{s}")
        r("reset").write(1)
        r("base").write(base)
        r("end").write(base + length)
        r("length").write(length)
        r("random").write(int(random_data))
        return r

    def run(self, blocks, sys_clk_freq):
        """Start BIST blocks [(name, base, length)] together, return {name: (MB/s, errors)}."""
        handles = [(name, length, self.bist(name, base, length)) for name, base, length in blocks]
        for name, length, r in handles:
            r("start").write(1)
        results = {}
        for name, length, r in handles:
            t0 = time.time()
            while not r("done").read():
                if time.time() - t0 > 10:
                    raise TimeoutError(f"BIST {name} timeout.")
            ticks  = r("ticks").read()
            errors = r("errors").read() if name == "checker" else 0
            results[name] = (length/(ticks/sys_clk_freq)/1e6 if ticks else 0, errors)
        return results

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build", default="build_dram14", help="Build directory (csr.csv, sdram_phy.h).")
    parser.add_argument("--length", default=16*1024*1024, type=lambda x: int(x, 0), help="BIST length (bytes).")
    parser.add_argument("cmd", choices=["init", "leveling", "memtest", "bandwidth"])
    args = parser.parse_args()

    dram = DRAM(args.build)
    sys_clk_freq = dram.bus.constants.config_clock_frequency
    print(f"sys {sys_clk_freq/1e6:.3f}MHz, {dram.phy.phases} phases, {dram.phy.memory//2**20}MB")

    if args.cmd == "init":
        dram.init()
        dram.read_leveling()
    if args.cmd == "leveling":
        dram.read_leveling()
    if args.cmd == "memtest":
        w = dram.run([("generator", 0, args.length)], sys_clk_freq)
        r = dram.run([("checker",   0, args.length)], sys_clk_freq)
        print(f"write {w['generator'][0]:.1f}MB/s, read {r['checker'][0]:.1f}MB/s, errors {r['checker'][1]}")
    if args.cmd == "bandwidth":
        half = dram.phy.memory//2
        w = dram.run([("generator", 0, args.length)], sys_clk_freq)
        r = dram.run([("checker",   0, args.length)], sys_clk_freq)
        print(f"write only: {w['generator'][0]:.1f}MB/s")
        print(f"read only : {r['checker'][0]:.1f}MB/s (errors {r['checker'][1]})")
        # Frame buffer traffic: write region B while reading region A (pre-written above).
        c = dram.run([("generator", half, args.length), ("checker", 0, args.length)], sys_clk_freq)
        total = c["generator"][0] + c["checker"][0]
        print(f"concurrent: write {c['generator'][0]:.1f}MB/s + read {c['checker'][0]:.1f}MB/s "
              f"= {total:.1f}MB/s (errors {c['checker'][1]}; 4K30 NV12 frame buffer needs ~746MB/s)")

if __name__ == "__main__":
    main()
