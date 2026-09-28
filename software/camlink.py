#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteCamLink host tool: FX3 RAM boot, device control and tests."""

import sys
import time
import struct
import argparse

import usb.core
import usb.util

# Constants ----------------------------------------------------------------------------------------

FX3_BOOT_VID    = 0x04b4
FX3_BOOT_PID    = 0x00f3
LITECAMLINK_VID = 0x1209
LITECAMLINK_PID = 0x0001
STOCK_VID       = 0x0fd9
STOCK_PIDS      = (0x0066, 0x0067)

FX3_REQ_FW      = 0xa0 # FX3 boot ROM firmware download/upload/jump request.
FX3_CHUNK       = 2048

# Helpers ------------------------------------------------------------------------------------------

def find_device(vid, pid, timeout=0.0):
    deadline = time.time() + timeout
    while True:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None or time.time() >= deadline:
            return dev
        time.sleep(0.1)

def parse_fx3_image(data):
    """Parse a FX3 boot image ("CY" header, sections, entry point, checksum)."""
    if data[0:2] != b"CY":
        raise ValueError("Not a FX3 image (missing CY signature).")
    if data[3] != 0xb0:
        raise ValueError(f"Unsupported FX3 image type 0x{data[3]:02x}.")
    sections = []
    checksum = 0
    offset   = 4
    while True:
        length, address = struct.unpack_from("<II", data, offset)
        offset += 8
        if length == 0:
            entry = address
            break
        section = data[offset:offset + 4*length]
        checksum = (checksum + sum(struct.unpack(f"<{length}I", section))) & 0xffffffff
        sections.append((address, section))
        offset += 4*length
    expected, = struct.unpack_from("<I", data, offset)
    if checksum != expected:
        raise ValueError(f"Bad FX3 image checksum (0x{checksum:08x} vs 0x{expected:08x}).")
    return sections, entry

# FX3 Boot -----------------------------------------------------------------------------------------

def fx3_load(filename, timeout=5.0):
    """Load a FX3 image to RAM through the FX3 boot ROM and jump to it."""
    dev = find_device(FX3_BOOT_VID, FX3_BOOT_PID, timeout)
    if dev is None:
        raise RuntimeError("FX3 bootloader (04b4:00f3) not found.")
    sections, entry = parse_fx3_image(open(filename, "rb").read())
    for address, section in sections:
        for i in range(0, len(section), FX3_CHUNK):
            chunk = section[i:i + FX3_CHUNK]
            addr  = address + i
            dev.ctrl_transfer(0x40, FX3_REQ_FW, addr & 0xffff, addr >> 16, chunk, timeout=1000)
    print(f"Loaded {sum(len(s) for _, s in sections)} bytes, jumping to 0x{entry:08x}.")
    try:
        dev.ctrl_transfer(0x40, FX3_REQ_FW, entry & 0xffff, entry >> 16, b"", timeout=1000)
    except usb.core.USBError:
        pass # Device may disconnect before the status stage.
    usb.util.dispose_resources(dev)

# LiteCamLink Device ------------------------------------------------------------------------------

VREQ_IDENT     = 0x00
VREQ_MEM_READ  = 0x01
VREQ_MEM_WRITE = 0x02
VREQ_REBOOT    = 0x0f

class CamLink:
    def __init__(self, timeout=5.0):
        self.dev = find_device(LITECAMLINK_VID, LITECAMLINK_PID, timeout)
        if self.dev is None:
            raise RuntimeError("LiteCamLink device (1209:0001) not found.")

    def vendor_in(self, req, value=0, index=0, length=0):
        return bytes(self.dev.ctrl_transfer(0xc0, req, value, index, length, timeout=1000))

    def vendor_out(self, req, value=0, index=0, data=b""):
        return self.dev.ctrl_transfer(0x40, req, value, index, data, timeout=1000)

    def ident(self):
        return self.vendor_in(VREQ_IDENT, length=64).rstrip(b"\x00").decode()

    def mem_read(self, addr, length=4):
        return self.vendor_in(VREQ_MEM_READ, addr & 0xffff, addr >> 16, length)

    def read32(self, addr):
        return struct.unpack("<I", self.mem_read(addr, 4))[0]

    def write32(self, addr, value):
        self.vendor_out(VREQ_MEM_WRITE, addr & 0xffff, addr >> 16, struct.pack("<I", value))

    def reboot(self):
        self.vendor_out(VREQ_REBOOT)
        usb.util.dispose_resources(self.dev)

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteCamLink host tool.")
    sub    = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fx3-load", help="Load a FX3 image to RAM (device in FX3 bootloader).")
    p.add_argument("image")

    sub.add_parser("list",   help="List Cam Link related USB devices.")
    sub.add_parser("ident",  help="Show LiteCamLink firmware identification.")
    sub.add_parser("reboot", help="Reboot the FX3 (back to the USB bootloader).")
    p = sub.add_parser("peek", help="Read FX3 memory (32-bit).")
    p.add_argument("addr", type=lambda x: int(x, 0))
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("poke", help="Write FX3 memory (32-bit).")
    p.add_argument("addr",  type=lambda x: int(x, 0))
    p.add_argument("value", type=lambda x: int(x, 0))

    args = parser.parse_args()

    if args.cmd == "fx3-load":
        fx3_load(args.image)

    if args.cmd == "ident":
        cl  = CamLink()
        spd = {3: "High-Speed", 4: "SuperSpeed"}.get(cl.dev.speed, str(cl.dev.speed))
        print(f"{cl.ident()} ({spd})")

    if args.cmd == "reboot":
        CamLink().reboot()

    if args.cmd == "peek":
        cl = CamLink()
        for i in range(args.count):
            addr = args.addr + 4*i
            print(f"0x{addr:08x}: 0x{cl.read32(addr):08x}")

    if args.cmd == "poke":
        CamLink().write32(args.addr, args.value)

    if args.cmd == "list":
        for dev in usb.core.find(find_all=True):
            ids = (dev.idVendor, dev.idProduct)
            if ids in [(FX3_BOOT_VID, FX3_BOOT_PID), (LITECAMLINK_VID, LITECAMLINK_PID)] or \
               (dev.idVendor == STOCK_VID and dev.idProduct in STOCK_PIDS):
                print(f"{dev.idVendor:04x}:{dev.idProduct:04x} bus {dev.bus} addr {dev.address} speed {dev.speed}")

if __name__ == "__main__":
    main()
