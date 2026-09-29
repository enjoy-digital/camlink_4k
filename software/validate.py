#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Hardware validation steps (doc/VALIDATION.md), each PASS/FAIL/SKIP with details.

    validate.py                 # All steps in order (stops the video/audio users first).
    validate.py enum capture    # Selected steps.
    validate.py --list

The device must be booted (`camlink.py boot`) with the HDMI source connected (HDMI-0).
"""

import os
import sys
import time
import fcntl
import struct
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usb.core
import numpy as np

from camlink  import CamLink
from v4l2cap  import Capture, find_device

# Helpers ------------------------------------------------------------------------------------------

def video_node():
    node = find_device("LiteCamLink")
    if node is None:
        raise RuntimeError("LiteCamLink video node not found.")
    return node

def capture_stats(w, h, fps, seconds=3, pixfmt="YUYV"):
    with Capture(video_node(), w, h, fps, pixfmt=pixfmt) as c:
        c.start()
        t0, n, seqs, short, flags_err = time.monotonic(), 0, [], 0, 0
        expected = w*h*(12 if pixfmt == "M420" else 16)//8
        while time.monotonic() - t0 < seconds:
            data, ts, seq, _, used, flags = c.read(copy=False)
            n += 1
            seqs.append(seq)
            short += used != expected
            flags_err += bool(flags & 0x40) # V4L2_BUF_FLAG_ERROR.
        dt = time.monotonic() - t0
    gaps = int(np.sum(np.diff(seqs) != 1)) if len(seqs) > 1 else 0
    return {"frames": n, "fps": round(n/dt, 2), "gaps": gaps, "short": short, "errors": flags_err}

def enum_formats():
    """VIDIOC_ENUM_FMT / ENUM_FRAMESIZES: {fourcc: [(w, h), ...]}."""
    VIDIOC_ENUM_FMT        = 0xc0405602
    VIDIOC_ENUM_FRAMESIZES = 0xc02c564a
    fd = os.open(video_node(), os.O_RDWR)
    formats = {}
    try:
        i = 0
        while True:
            buf = bytearray(struct.pack("<II", i, 1) + bytes(56))
            try:
                fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf)
            except OSError:
                break
            pf = struct.unpack_from("<I", buf, 44)[0]
            name = struct.pack("<I", pf).decode()
            sizes, j = [], 0
            while True:
                fs = bytearray(struct.pack("<II", j, pf) + bytes(36))
                try:
                    fcntl.ioctl(fd, VIDIOC_ENUM_FRAMESIZES, fs)
                except OSError:
                    break
                sizes.append(struct.unpack_from("<II", fs, 12))
                j += 1
            formats[name] = sizes
            i += 1
    finally:
        os.close(fd)
    return formats

# Steps --------------------------------------------------------------------------------------------

def step_enum(ctx):
    """Enumeration: LiteCamLink firmware, SuperSpeed, UVC formats, UAC card."""
    cl    = CamLink()
    ident = cl.ident()
    speed = {3: "HighSpeed", 4: "SuperSpeed"}.get(cl.dev.speed, cl.dev.speed)
    fmts  = enum_formats()
    card  = any("LiteCamLink" in l for l in open("/proc/asound/cards"))
    ok = (speed == "SuperSpeed" and "YUYV" in fmts and "M420" in fmts and (3840, 2160) in fmts["M420"]
        and card)
    return ok, f"{ident} ({speed}); formats {fmts}; sound card {card}"

def step_stats(ctx):
    """FX3 debug counters: no USB3 PHY timeouts / link fallbacks."""
    s = CamLink().stats()
    return s["phy_cr_timeouts"] == 0 and s["ss_to_usb2_fallbacks"] == 0, str(s)

def step_hdmi(ctx):
    """HDMI input: stable, resolution, fps (FPGA frame period)."""
    st = CamLink().hdmi_status()
    ctx["hdmi"] = st
    return bool(st["stable"]), (f"{st['hactive']}x{st['vactive']} @ {st.get('fps')} fps, "
        f"{st['colorspace']}, generation {st.get('generation')}")

def step_capture(ctx):
    """1080p capture through uvcvideo: fps, no gaps/short/error frames."""
    st = ctx.get("hdmi") or CamLink().hdmi_status()
    fps = 60 if (st.get("fps") or 0) > 45 and st["vactive"] <= 1080 else 30
    r = capture_stats(1920, 1080, fps)
    ok = r["gaps"] == 0 and r["short"] == 0 and r["errors"] == 0 and r["fps"] > 0.9*(st.get("fps") or fps)
    return ok, f"requested 1080p{fps}: {r}"

def step_first_frame(ctx):
    """Raw UVC stream (uvcvideo detached): no payload header errors (first frame fix)."""
    from uvc_raw import raw_capture
    r = raw_capture(frame=1, fps=30, seconds=2)
    return r["header_errors"] == 0 and r["good_frames"] > 0, str(r)

def step_restart(ctx):
    """20 stream start/stop cycles: first frame each time."""
    fails = 0
    for _ in range(20):
        try:
            with Capture(video_node(), 1920, 1080, 30) as c:
                c.start()
                c.read(copy=False, timeout=1.0)
        except TimeoutError:
            fails += 1
    return fails == 0, f"{20 - fails}/20 OK"

def step_audio_counter(ctx):
    """UAC with the FPGA test counter: bit exact (1 bad sample at start at most)."""
    from audio_check import find_card, capture, check_test
    CamLink().audio_test(True)
    try:
        r = check_test(capture(find_card(), 3))
    finally:
        CamLink().audio_test(False)
    return r["not_inverted"] <= 1 and r["discontinuities"] <= 2, str(r)

def step_audio_video(ctx):
    """Audio counter + 1080p video concurrently: both clean."""
    import threading
    from audio_check import find_card, capture, check_test
    CamLink().audio_test(True)
    res = {}
    t = threading.Thread(target=lambda: res.update(audio=check_test(capture(find_card(), 6))))
    t.start()
    time.sleep(1.5)
    try:
        res["video"] = capture_stats(1920, 1080, 30, seconds=3)
    finally:
        t.join()
        CamLink().audio_test(False)
    a, v = res["audio"], res["video"]
    return v["gaps"] == 0 and v["short"] == 0 and a["not_inverted"] <= 4, str(res)

def step_xu(ctx):
    """UVC Extension Unit: input info, crop control."""
    from uvc_xu import input_info, xu_query, XU_CROP_CONTROL, UVC_SET_CUR, UVC_GET_CUR
    fd = os.open(video_node(), os.O_RDWR)
    try:
        info = input_info(fd)
        xu_query(fd, XU_CROP_CONTROL, UVC_SET_CUR, struct.pack("<HH", 640, 360))
        crop = struct.unpack("<HH", xu_query(fd, XU_CROP_CONTROL, UVC_GET_CUR, bytes(4)))
        xu_query(fd, XU_CROP_CONTROL, UVC_SET_CUR, struct.pack("<HH", 0xffff, 0))
    finally:
        os.close(fd)
    return bool(info["signal"]) and crop == (640, 360), f"{info}; crop readback {crop}"

def step_controls(ctx):
    """UVC Processing Unit controls visible through V4L2 (brightness/contrast/saturation)."""
    VIDIOC_QUERYCTRL = 0xc0445624
    ids = {"brightness": 0x00980900, "contrast": 0x00980901, "saturation": 0x00980902}
    fd = os.open(video_node(), os.O_RDWR)
    found = {}
    try:
        for name, cid in ids.items():
            q = bytearray(struct.pack("<II", cid, 0) + bytes(60))
            try:
                fcntl.ioctl(fd, VIDIOC_QUERYCTRL, q)
                mn, mx, step, df = struct.unpack_from("<iiii", q, 40)
                found[name] = (mn, mx, df)
            except OSError:
                found[name] = None
    finally:
        os.close(fd)
    return all(found.values()), str(found)

def step_m420(ctx):
    """4K30 M420 capture (needs a 3840x2160 input and ideally the PLL_FBDIV=21 firmware)."""
    st = ctx.get("hdmi") or CamLink().hdmi_status()
    if (st["hactive"], st["vactive"]) != (3840, 2160):
        return None, f"input {st['hactive']}x{st['vactive']} (set HDMI-0 to 3840x2160@30)"
    r = capture_stats(3840, 2160, 30, seconds=4, pixfmt="M420")
    return r["gaps"] == 0 and r["short"] == 0 and r["fps"] > 25, str(r)

STEPS = [
    ("enum",        step_enum),
    ("stats",       step_stats),
    ("hdmi",        step_hdmi),
    ("capture",     step_capture),
    ("first_frame", step_first_frame),
    ("restart",     step_restart),
    ("audio",       step_audio_counter),
    ("audio_video", step_audio_video),
    ("xu",          step_xu),
    ("controls",    step_controls),
    ("m420",        step_m420),
    ("stats_end",   step_stats), # Link counters after all the traffic.
]

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("steps", nargs="*", help="Steps to run (default: all).")
    parser.add_argument("--list", action="store_true", help="List the steps.")
    args = parser.parse_args()

    if args.list:
        for name, fn in STEPS:
            print(f"{name:12s} {fn.__doc__}")
        return
    names = args.steps or [name for name, _ in STEPS]
    ctx, results = {}, []
    for name, fn in STEPS:
        if name not in names:
            continue
        print(f"[{name}] {fn.__doc__}", flush=True)
        try:
            ok, details = fn(ctx)
        except (Exception, usb.core.USBError) as e:
            ok, details = False, f"{type(e).__name__}: {e}"
            traceback.print_exc()
        status = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        print(f"  {status}: {details}", flush=True)
        results.append((name, status))
    print()
    for name, status in results:
        print(f"{name:12s} {status}")
    sys.exit(0 if all(s != "FAIL" for _, s in results) else 1)

if __name__ == "__main__":
    main()
