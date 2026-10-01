#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""HDMI bring-up helper: receiver status, FPGA measurements, raw frame captures."""

import os
import sys
import time
import struct
import argparse

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from camlink    import CamLink, CamLinkBus
from usb_stream import USBStreamReader

import usb.util

IT6802 = 0x49

def it_read(cl, reg, bank=0):
    cl.i2c_write(IT6802, bytes([0x0f]), bytes([bank]))
    v = cl.i2c_read(IT6802, bytes([reg]), 1)[0]
    cl.i2c_write(IT6802, bytes([0x0f]), bytes([0]))
    return v

def status(cl, bus):
    st = cl.hdmi_status()
    print("IT6802  : " + ", ".join(f"{k}={v}" for k, v in st.items()))
    regs = [0x0a, 0x0c, 0x50, 0x51, 0x53, 0x65, 0x99, 0x9a, 0xa8]
    print("Regs    : " + " ".join(f"{r:02x}={it_read(cl, r):02x}" for r in regs))
    print(f"FPGA    : pclk {bus.regs.hdmi_clk_freq_value.read()/1e6:.2f}MHz, "
          f"hres {bus.regs.hdmi_in_hres.read()}, vres {bus.regs.hdmi_in_vres.read()}, "
          f"frames {bus.regs.hdmi_in_frames.read()}, dropped {bus.regs.hdmi_in_dropped.read()}, "
          f"overflow {bus.regs.hdmi_in_overflow.read()}")
    return st

def yuy2_to_rgb(frame, width, height):
    f = np.frombuffer(frame, dtype=np.uint8)[:width*height*2].reshape(height, width//2, 4).astype(np.float32)
    y0, u, y1, v = f[..., 0], f[..., 1] - 128, f[..., 2], f[..., 3] - 128
    y = np.stack([y0, y1], axis=-1).reshape(height, width) - 16
    u = np.repeat(u, 2, axis=1)
    v = np.repeat(v, 2, axis=1)
    r = 1.164*y + 1.793*v
    g = 1.164*y - 0.213*u - 0.533*v
    b = 1.164*y + 2.112*u
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)

def capture(cl, bus, width, height, y_lane, c_lane, c_swap=0, ddr=0, ddr_swap=0, downscale=0, filename=None):
    """Capture one HDMI frame through the UVC packetizer over raw USB."""
    bus.regs.gpif_control.write(0)
    bus.regs.pattern_enable.write(0)
    bus.regs.main_source_sel.write(3)
    bus.regs.uvc_frame_words.write(width*height//2)
    bus.regs.hdmi_in_control.write(1 | (y_lane << 4) | (c_lane << 6) | (c_swap << 8) |
        (ddr << 12) | (ddr_swap << 13) | (downscale << 14))
    cl.stream_start()
    bus.regs.gpif_control.write((4 << 8) | 3)
    frame_size = width*height*2
    payload_size = bus.regs.uvc_payload_words.read()*4 + 12 # One FX3 DMA buffer.
    state = {"frame": bytearray(), "frames": []}
    def on_transfer(chunk):
        for off in range(0, len(chunk), payload_size):
            payload = chunk[off:off + payload_size]
            if len(payload) < 12 or payload[0] != 12:
                state["frame"] = bytearray()
                continue
            state["frame"] += payload[12:]
            if payload[1] & 0x02:
                state["frames"].append(bytes(state["frame"]))
                state["frame"] = bytearray()
    usb.util.dispose_resources(cl.dev)
    reader = USBStreamReader()
    reader.read(4*frame_size, on_transfer, timeout=2.0)
    reader.close()
    bus.regs.gpif_control.write(0)
    bus.regs.hdmi_in_control.write(0)
    cl.stream_stop()
    good = [f for f in state["frames"] if len(f) == frame_size]
    print(f"Capture y_lane={y_lane} c_lane={c_lane} c_swap={c_swap} ddr={ddr}/{ddr_swap} ds={downscale}: {len(state['frames'])} frames, "
          f"{len(good)} complete, sizes {[len(f) for f in state['frames'][:4]]}")
    if good and filename:
        from PIL import Image
        Image.fromarray(yuy2_to_rgb(good[-1], width, height)).save(filename)
        print(f"  saved {filename}")
    return good

def main():
    parser = argparse.ArgumentParser(description="CamLink 4K HDMI bring-up helper.")
    parser.add_argument("--capture", action="store_true", help="Capture frames for all Y/C lane combinations.")
    parser.add_argument("--y-lane",  type=int, help="Only this Y lane.")
    parser.add_argument("--c-lane",  type=int, help="Only this C lane.")
    parser.add_argument("--c-swap",    type=int, default=0)
    parser.add_argument("--ddr",       type=int, default=0)
    parser.add_argument("--ddr-swap",  type=int, default=0)
    parser.add_argument("--downscale", type=int, default=0)
    args = parser.parse_args()

    cl  = CamLink()
    bus = CamLinkBus(cl)
    st  = status(cl, bus)
    if args.capture:
        width, height = bus.regs.hdmi_in_hres.read(), bus.regs.hdmi_in_vres.read()
        if args.ddr:
            width *= 2
        if args.downscale:
            width, height = width//2, height//2
        if not (width and height):
            print("No active video measured.")
            return
        lanes = [(y, c) for y in range(3) for c in range(3) if y != c]
        if args.y_lane is not None:
            lanes = [(args.y_lane, args.c_lane)]
        for y, c in lanes:
            capture(cl, bus, width, height, y, c, c_swap=args.c_swap, ddr=args.ddr, ddr_swap=args.ddr_swap,
                downscale=args.downscale,
                filename=f"build/hdmi_y{y}_c{c}_s{args.c_swap}_d{args.ddr_swap}.png")

if __name__ == "__main__":
    main()
