#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CamLink 4K UVC Extension Unit access through uvcvideo (UVCIOC_CTRL_QUERY, no libusb).

Works while the camera is in use (OBS, VLC...):
    uvc_xu.py info          : input info (resolution, fps, color space, signal).
    uvc_xu.py crop X Y      : 1080p crop window of a 4K input at (X, Y).
    uvc_xu.py crop off      : 2x downscale (default).

Brightness/contrast/saturation are standard UVC Processing Unit controls (v4l2-ctl -l).
"""

import os
import sys
import fcntl
import ctypes
import struct
import argparse

from v4l2cap import find_device

XU_UNIT_ID            = 4
XU_INPUT_INFO_CONTROL = 1
XU_CROP_CONTROL       = 2

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81

class uvc_xu_control_query(ctypes.Structure):
    _fields_ = [
        ("unit",     ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query",    ctypes.c_uint8),
        ("size",     ctypes.c_uint16),
        ("data",     ctypes.POINTER(ctypes.c_uint8)),
    ]

UVCIOC_CTRL_QUERY = 0xc0000000 | (ctypes.sizeof(uvc_xu_control_query) << 16) | (ord("u") << 8) | 0x21

def xu_query(fd, selector, query, data):
    buf = (ctypes.c_uint8*len(data)).from_buffer_copy(bytes(data))
    q   = uvc_xu_control_query(XU_UNIT_ID, selector, query, len(data), buf)
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, q)
    return bytes(buf)

def input_info(fd, sys_clk_freq=100e6):
    d = xu_query(fd, XU_INPUT_INFO_CONTROL, UVC_GET_CUR, bytes(24))
    present, sys_status, hpd, stable = d[:4]
    htotal, hactive, vtotal, vactive = struct.unpack("<4H", d[4:12])
    generation, period = struct.unpack("<II", d[16:24])
    return {
        "signal":     bool(stable),
        "5v":         bool(sys_status & 1),
        "resolution": f"{hactive}x{vactive}" if stable else None,
        "total":      f"{htotal}x{vtotal}" if stable else None,
        "fps":        round(sys_clk_freq/period, 3) if (stable and period) else None,
        "colorspace": ["RGB", "YCbCr422", "YCbCr444", "?"][d[14] & 3] if stable else None,
        "generation": generation,
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("crop")
    p.add_argument("x")
    p.add_argument("y", nargs="?", type=int, default=0)
    args = parser.parse_args()

    device = args.device or find_device("CamLink 4K")
    if device is None:
        print("CamLink 4K video device not found.")
        sys.exit(1)
    fd = os.open(device, os.O_RDWR)
    try:
        if args.cmd == "info":
            for k, v in input_info(fd).items():
                print(f"{k:10s}: {v}")
        if args.cmd == "crop":
            x = 0xffff if args.x == "off" else int(args.x)
            xu_query(fd, XU_CROP_CONTROL, UVC_SET_CUR, struct.pack("<HH", x, args.y))
    finally:
        os.close(fd)

if __name__ == "__main__":
    main()
