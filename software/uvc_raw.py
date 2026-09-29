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

FRAMES = {
    1: {1: (1920, 1080), 2: (1280, 720), 3: (640, 480)}, # YUY2.
    2: {1: (3840, 2160), 2: (1920, 1080)},               # M420.
}
BPP = {1: 16, 2: 12}

def uvc_commit(handle, frame_index, fps, format_index=1):
    probe = struct.pack("<HBBIHHHHHIIIBBBB", 0, format_index, frame_index, 10000000//fps, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 1, 1, 1)
    for selector in (UVC_VS_PROBE_CONTROL, UVC_VS_COMMIT_CONTROL):
        handle.controlWrite(0x21, UVC_SET_CUR, selector << 8, 1, probe)

def raw_capture(frame=1, fps=30, seconds=4, format_index=1, dump=0, payload_size=PAYLOAD_SIZE):
    """Raw UVC capture (uvcvideo detached), returns statistics (frames, header errors...)."""
    width, height = FRAMES[format_index][frame]
    frame_size = width*height*BPP[format_index]//8

    reader = USBStreamReader()
    uvc_commit(reader.handle, frame, fps, format_index)

    state = {"frame": 0, "frames": [], "fid": None, "errors": 0, "dumps": 0, "payloads": 0,
        "short": [], "error_at": []}
    def on_transfer(chunk):
        for off in range(0, len(chunk), payload_size):
            payload = chunk[off:off + payload_size]
            state["payloads"] += 1
            if len(payload) < payload_size:
                state["short"].append(len(payload))
            hlen, info = payload[0], payload[1]
            if hlen != 12 or not (info & 0x80):
                state["errors"] += 1
                if state["dumps"] < dump:
                    state["dumps"] += 1
                    print(f"bad payload #{state['payloads']} (chunk {len(chunk)}, off {off}, "
                          f"len {len(payload)}): {payload[:16].hex()}")
                continue
            fid = info & 1
            if state["fid"] is not None and fid != state["fid"] and state["frame"]:
                state["errors"] += 1
                state["error_at"].append((len(state["frames"]), state["frame"]))
                state["frame"] = 0
            state["fid"] = fid
            state["frame"] += len(payload) - 12
            if info & 0x02:
                state["frames"].append(state["frame"])
                state["frame"] = 0
                state["fid"] = None

    size = int(seconds*frame_size*fps)
    received, duration, error = reader.read(size, on_transfer, timeout=2.0)
    # Stream off (endpoint halt cleared, as uvcvideo does for bulk).
    reader.handle.clearHalt(0x81)
    reader.close()

    sizes = state["frames"]
    return {
        "frame_size":    frame_size,
        "received_mb":   received/1e6,
        "duration":      duration,
        "rate_mbs":      received/max(duration, 1e-3)/1e6,
        "frames":        len(sizes),
        "good_frames":   sum(1 for s in sizes if s == frame_size),
        "bad_sizes":     [s for s in sizes if s != frame_size][:8],
        "bad_index":     [i for i, s in enumerate(sizes) if s != frame_size][:8],
        "error_at":      state["error_at"][:8], # (frame index, bytes received) on FID errors.
        "fps":           len(sizes)/max(duration, 1e-3),
        "payloads":      state["payloads"],
        "header_errors": state["errors"],
        "short":         sorted(set(state["short"]))[:8],
        "error":         error,
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format",  type=int, default=1, help="UVC format index (1: YUY2, 2: M420).")
    parser.add_argument("--frame",   type=int, default=1, help="UVC frame index (YUY2: 1080p/720p/480p, M420: 2160p/1080p).")
    parser.add_argument("--fps",     type=int, default=30)
    parser.add_argument("--seconds", type=float, default=4)
    parser.add_argument("--dump",    type=int, default=4, help="Bad payloads to dump.")
    args = parser.parse_args()

    r = raw_capture(args.frame, args.fps, args.seconds, args.format, args.dump)
    print(f"{r['received_mb']:.1f} MB in {r['duration']:.2f}s ({r['rate_mbs']:.1f} MB/s), "
          f"{r['frames']} frames ({r['good_frames']} good, {r['fps']:.1f} fps), "
          f"{r['payloads']} payloads, {r['header_errors']} header errors, error: {r['error']}")
    if r["bad_sizes"]:
        print(f"bad frame sizes (expected {r['frame_size']}): {r['bad_sizes']}")
    print(f"short payload sizes: {r['short']}")

if __name__ == "__main__":
    main()
