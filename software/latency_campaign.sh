#!/bin/sh
#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause
#
# Display latency campaign (doc/LATENCY.md): CamLinX (ffplay, camlinx_view), then the stock
# firmware (ffplay), then CamLinX restored to the flash.
#
# Needs: this PC's HDMI-0 -> Cam Link input, the players fullscreen on the primary output (DP-1),
# screen unlocked, no keyboard/mouse input during the runs (the players quit on q/Esc), the stock
# FX3 image in ~/camlink_backup (loaded to RAM only). The flash FX3 image (block 0) is erased to
# reach the USB bootloader, then re-programmed at the end: the bitstreams are not touched.

set -x
cd "$(dirname "$0")/.."
SECONDS_PER_RUN=${SECONDS_PER_RUN:-20}
make -C software/viewer -s
make -C firmware/fx3 -s

rm -f doc/bench/latency_display.json
python3 software/latency_display.py --firmware camlinx_4k --players ffplay,viewer --pos top,bottom --seconds $SECONDS_PER_RUN

# Stock: the CamLinX FPGA watchdog (still configured) would reset the FX3 without our heartbeat:
# disable it, erase our FX3 image to reach the bootloader, load the stock image to RAM.
python3 software/camlink.py csr fx3_watchdog_control 0
python3 software/camlink.py flash-recover
sleep 5
python3 -c "import sys; sys.path.insert(0, 'software'); import bench; bench.select_firmware('stock')"
python3 software/latency_display.py --firmware stock --players ffplay --pos top,bottom --seconds $SECONDS_PER_RUN

# Restore CamLinX: bootloader, RAM boot (retried: the first load after a stock session can miss
# the enumeration), FX3 image back to the flash, standalone reboot.
python3 -c "import sys; sys.path.insert(0, 'software'); import bench; bench.to_bootloader()"
python3 software/camlink.py boot || (sleep 5; python3 software/camlink.py boot)
python3 software/camlink.py flash-fx3 firmware/fx3/build/fx3.img
python3 software/camlink.py reboot
sleep 14
python3 software/camlink.py ident
python3 software/camlink.py hdmi-status | grep -E "stable|hactive|fps"
