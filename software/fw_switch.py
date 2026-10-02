#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Switch the Cam Link between the stock Elgato firmware (RAM) and CamLink 4K (flash).

  fw_switch.py stock    : CamLink 4K FPGA watchdog off (it would reset the stock FX3 firmware), our
                          FX3 image erased (flash block 0 only, to reach the USB bootloader), stock
                          FX3 image (~/camlink_backup/stock_fx3.img) loaded to RAM. The bitstreams
                          in flash are untouched. A power cycle then leaves the FX3 in the
                          bootloader.
  fw_switch.py camlink  : CamLink 4K booted from RAM, FX3 image written back to the flash,
                          standalone reboot (also the way back after a power cycle in stock mode).
  fw_switch.py status   : current state (camlink_4k, stock, bootloader, none).

The HDMI output feeding the Cam Link (--output, HDMI-0) is re-probed after a switch (the EDID
changes), keeping its place in the desktop layout.
"""

import os
import sys
import time
import argparse
import subprocess

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "software"))

import bench
from source import output_geometry

# Helpers ------------------------------------------------------------------------------------------

def camlink(*args, check=True):
    """Run a software/camlink.py command."""
    cmd = [sys.executable, os.path.join(ROOT, "software", "camlink.py"), *args]
    return subprocess.run(cmd, cwd=ROOT, check=check)

def reprobe(output):
    """Re-probe the source output (new EDID), back at the same desktop position and mode."""
    w, h, x, y = output_geometry(output)
    subprocess.run(["xrandr", "--output", output, "--off"])
    time.sleep(2)
    subprocess.run(["xrandr", "--output", output, "--mode", f"{w}x{h}", "--pos", f"{x}x{y}"])
    time.sleep(4)

# Firmware Switching -------------------------------------------------------------------------------

def to_stock(output):
    state = bench.usb_state()
    if state == "stock":
        print("Already stock.")
        return
    if state == "camlink_4k":
        camlink("csr", "fx3_watchdog_control", "0")
        camlink("flash-recover")
    if not bench.wait_state("bootloader", timeout=20):
        raise RuntimeError(f"FX3 bootloader not reached (state {bench.usb_state()}).")
    camlink("fx3-load", bench.STOCK_IMG)
    if not bench.wait_state("stock", timeout=30):
        raise RuntimeError(f"Stock firmware did not enumerate (state {bench.usb_state()}).")
    time.sleep(4)
    reprobe(output)
    print("Stock firmware running (RAM). Back: fw_switch.py camlink")

def to_camlink(output):
    state = bench.usb_state()
    if state == "stock":
        bench.stock.cold_reset()
    if state != "bootloader" and not bench.wait_state("bootloader", timeout=20):
        if bench.usb_state() == "camlink_4k":
            print("Already CamLink 4K.")
            return
        raise RuntimeError(f"FX3 bootloader not reached (state {bench.usb_state()}).")
    # The first load after a stock session can miss the enumeration: retried once.
    if camlink("boot", check=False).returncode:
        time.sleep(5)
        camlink("boot")
    camlink("flash-fx3", os.path.join(ROOT, "firmware", "build", "fx3.img"))
    camlink("reboot")
    if not bench.wait_state("camlink_4k", timeout=30):
        raise RuntimeError(f"CamLink 4K did not boot from flash (state {bench.usb_state()}).")
    time.sleep(4)
    reprobe(output)
    print("CamLink 4K running (standalone, from flash).")

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", choices=["stock", "camlink", "status"])
    parser.add_argument("--output", default="HDMI-0",
        help="Host output feeding the Cam Link (re-probed).")
    args = parser.parse_args()
    if args.target == "status":
        print(bench.usb_state())
    elif args.target == "stock":
        to_stock(args.output)
    else:
        to_camlink(args.output)

if __name__ == "__main__":
    main()
