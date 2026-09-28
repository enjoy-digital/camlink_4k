#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Raw UVC capture (libusb, uvcvideo detached): real PROBE/COMMIT then bulk payload checks.

Unlike `camlink.py uvc-raw-test` (vendor stream start), this goes through the firmware UVC path, so
it can run while the audio interface streams (arecord through snd-usb-audio).
"""

import time
import struct
import argparse

from usb_stream import USBStreamReader

UVC_SET_CUR = 0x01
UVC_VS_PROBE_CONTROL  = 0x01
UVC_VS_COMMIT_CONTROL = 0x02
PAYLOAD_SIZE = 16384

def uvc_commit(handle, frame_index, fps):
    probe = struct.pack("<HBBIHHHHHIIIBBBB", 0, 1, frame_index, 10000000//fps, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 1, 1, 1)
    for selector in (UVC_VS_PROBE_CONTROL, UVC_VS_COMMIT_CONTROL):
        handle.controlWrite(0x21, UVC_SET_CUR, selector << 8, 1, probe)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frame",   type=int, default=1, help="UVC frame index (1: 1080p, 2: 720p, 3: 480p).")
    parser.add_argument("--fps",     type=int, default=30)
    parser.add_argument("--seconds", type=float, default=4)
    parser.add_argument("--dump",    type=int, default=4, help="Bad payloads to dump.")
    args = parser.parse_args()

    width, height = {1: (1920, 1080), 2: (1280, 720), 3: (640, 480)}[args.frame]
    frame_size = width*height*2

    reader = USBStreamReader()
    uvc_commit(reader.handle, args.frame, args.fps)

    state = {"frame": 0, "frames": [], "fid": None, "errors": 0, "dumps": 0, "payloads": 0,
        "short": []}
    def on_transfer(chunk):
        for off in range(0, len(chunk), PAYLOAD_SIZE):
            payload = chunk[off:off + PAYLOAD_SIZE]
            state["payloads"] += 1
            if len(payload) < PAYLOAD_SIZE:
                state["short"].append(len(payload))
            hlen, info = payload[0], payload[1]
            if hlen != 12 or not (info & 0x80):
                state["errors"] += 1
                if state["dumps"] < args.dump:
                    state["dumps"] += 1
                    print(f"bad payload #{state['payloads']} (chunk {len(chunk)}, off {off}, "
                          f"len {len(payload)}): {payload[:16].hex()}")
                continue
            fid = info & 1
            if state["fid"] is not None and fid != state["fid"] and state["frame"]:
                state["errors"] += 1
                state["frame"] = 0
            state["fid"] = fid
            state["frame"] += len(payload) - 12
            if info & 0x02:
                state["frames"].append(state["frame"])
                state["frame"] = 0
                state["fid"] = None

    size = int(args.seconds*frame_size*args.fps)
    received, duration, error = reader.read(size, on_transfer, timeout=2.0)
    # Stream off (endpoint halt cleared, as uvcvideo does for bulk).
    reader.handle.clearHalt(0x81)
    reader.close()

    sizes = state["frames"]
    good  = sum(1 for s in sizes if s == frame_size)
    print(f"{received/1e6:.1f} MB in {duration:.2f}s ({received/max(duration, 1e-3)/1e6:.1f} MB/s), "
          f"{len(sizes)} frames ({good} good, {len(sizes)/max(duration, 1e-3):.1f} fps), "
          f"{state['payloads']} payloads, {state['errors']} header errors, error: {error}")
    bad = [s for s in sizes if s != frame_size]
    if bad:
        print(f"bad frame sizes (expected {frame_size}): {bad[:8]}")
    short = sorted(set(state["short"]))
    print(f"short payload sizes: {short[:8]}")

if __name__ == "__main__":
    main()
