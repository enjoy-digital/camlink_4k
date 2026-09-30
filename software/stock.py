#!/usr/bin/env python3

#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Minimal control of the stock Elgato firmware (vendor HID interface) for A/B benchmarks.

Protocol documented by schlarpc/elgato-cam-link-4k-firmware-re (MIT): SET_REPORT output 0x12 with
value 0 = cold reset (the FX3 boot ROM runs again: USB bootloader when the flash image is invalid).
"""

import os
import glob
import fcntl

VID  = 0x0fd9
PIDS = (0x0066, 0x0067)

def _ioc(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr

def HIDIOCSOUTPUT(n):
    return _ioc(3, "H", 0x0b, n)

def find_hidraw():
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(node, "device/uevent")).read()
        except OSError:
            continue
        for pid in PIDS:
            if f"HID_ID=0003:{VID:08X}:{pid:08X}" in uevent:
                return "/dev/" + os.path.basename(node)
    return None

def cold_reset():
    path = find_hidraw()
    if path is None:
        raise RuntimeError("Stock Cam Link HID interface not found.")
    fd = os.open(path, os.O_RDWR)
    try:
        buf = bytearray([0x12, 0x00])
        try:
            fcntl.ioctl(fd, HIDIOCSOUTPUT(len(buf)), buf, True)
        except OSError:
            pass # Device resets before completing the request.
    finally:
        os.close(fd)
