#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Display latency: source render (HDMI-0 barcode) -> pixels in the displayed frame buffer.

software/source.py draws a barcode (host CLOCK_MONOTONIC ms) on HDMI-0 (-> Cam Link input), a
player shows the capture fullscreen on the primary output (DP-1), software/viewer/latgrab polls
the barcode region of the screen (XShm) and timestamps the first appearance of each value. The
same probe on HDMI-0 itself gives the source's own render -> frame buffer time (calibration).
Monitor scanout/processing is not included (same for all players).

Players: ffplay (low latency options, fullscreen), "direct" (camlink_view on a monitor taken from
X with --vk/--drm in --viewer-args: stages and computed scanout from its own report) and
camlink_view (CamLink 4K or stock, its own stage timestamps: first payload of the frame, barcode
rows received, present), plus the V4L2 path alone ("v4l2": uvcvideo buffer timestamp and dequeue
time, what ffplay receives).
Results are appended to doc/bench/latency_display.json (per sample CSVs in doc/bench/latency/).
"""

import os
import re
import sys
import json
import time
import signal
import argparse
import subprocess

# Configuration ------------------------------------------------------------------------------------

ROOT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LATGRAB = os.path.join(ROOT, "software", "viewer", "latgrab")
VIEWER  = os.path.join(ROOT, "software", "viewer", "camlink_view")
RESULTS = os.path.join(ROOT, "doc", "bench", "latency_display.json")
CSV_DIR = os.path.join(ROOT, "doc", "bench", "latency")

# Defaults, updated from the command line arguments.
W, H, FPS     = 1920, 1080, 60   # Capture size/rate (--mode, --capture-fps).
SRC_RATE      = 60               # Source (HDMI-0) refresh (--rate).
PIXFMT        = "YUYV"           # Capture pixel format (--pixfmt: YUYV or MJPG).
DISP_BLOCK    = 1920 // (24 + 8) # Barcode block on the fullscreen player (DP-1, 1920x1080).
SOURCE_X      = 1920             # HDMI-0 position (read from xrandr at start).
BLOCK         = W // (24 + 8)    # Barcode block on the source (HDMI-0 at the capture size).
DEVICE        = "/dev/video0"
FIRMWARE      = "camlink_4k"
VIEWER_ARGS   = []
LABEL         = "camlink_4k"
HPD_ON_STREAM = False # Capture devices asserting hot plug only while streaming (MS2109).

FFPLAY_LOW_LATENCY = ["-fflags", "nobuffer", "-flags", "low_delay", "-framedrop", "-sync", "ext",
    "-probesize", "32", "-analyzeduration", "0"]

# Helpers ------------------------------------------------------------------------------------------

def barcode_y(pos):
    return BLOCK if pos == "top" else H - 4*BLOCK

def disp_y(pos):
    return DISP_BLOCK if pos == "top" else 1080 - 4*DISP_BLOCK

def latgrab(x0, y0, seconds, block=None):
    cmd = [LATGRAB,
        "--x0",      str(x0),
        "--y0",      str(y0),
        "--block",   str(block or DISP_BLOCK),
        "--seconds", str(seconds),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    return json.loads(out.splitlines()[-1]) if out else {"samples": 0}

def start(cmd, out=None):
    return subprocess.Popen(cmd,
        stdout     = out or subprocess.DEVNULL,
        stderr     = subprocess.STDOUT if out else subprocess.DEVNULL,
        preexec_fn = os.setsid,
    )

def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

# Players ------------------------------------------------------------------------------------------

def player_cmd(player):
    if player == "ffplay":
        return ["ffplay", "-hide_banner", "-loglevel", "quiet",
            "-fs", "-left", "0", "-top", "0"] + FFPLAY_LOW_LATENCY + [
            "-f",            "v4l2",
            "-input_format", "mjpeg" if PIXFMT == "MJPG" else "yuyv422",
            "-video_size",   f"{W}x{H}",
            "-framerate",    str(FPS),
            "-i",            DEVICE,
        ]
    return [VIEWER, "--device", "stock" if FIRMWARE == "stock" else "camlink", "--format", "yuy2",
        "--size", f"{W}x{H}", "--fps", str(FPS), "--fullscreen", "--latency"] + VIEWER_ARGS

# Measurements -------------------------------------------------------------------------------------

def stats(values):
    import numpy as np
    v = np.array(values)
    if not len(v):
        return {"samples": 0}
    return {
        "samples":   len(v),
        "median_ms": round(float(np.median(v)), 1),
        "p5_ms":     round(float(np.percentile(v, 5)), 1),
        "p95_ms":    round(float(np.percentile(v, 95)), 1),
    }

def measure_v4l2(pos, seconds):
    """V4L2 path alone: buffer timestamp (uvcvideo: first payload), dequeue (frame complete)."""
    from v4l2cap import Capture
    from bench import to_luma
    from source import barcode_decode
    first, complete, last = [], [], None
    def luma(data, n):
        if PIXFMT == "MJPG": # Decoded after the timestamps (decode time not counted).
            import io
            import numpy as np
            from PIL import Image
            try:
                return np.asarray(Image.open(io.BytesIO(bytes(data[:n]))).convert("L"))
            except Exception:
                return None
        return to_luma(data, "YUYV", W, H) if n >= W*H*2 else None
    src = calib = None
    if not HPD_ON_STREAM:
        src, calib = source_up(pos, seconds)
    with Capture(DEVICE, W, H, FPS, PIXFMT) as cap:
        cap.start()
        if HPD_ON_STREAM: # Streaming: hot plug, then the source (frames keep being dequeued).
            import threading
            box = {}
            th  = threading.Thread(
                target=lambda: box.update(zip(("src", "calib"), source_up(pos, seconds))))
            th.start()
            while th.is_alive():
                cap.read()
            src, calib = box["src"], box["calib"]
            for _ in range(int(FPS)):
                cap.read()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            data, ts, seq, tdq, n, flags = cap.read()
            y = luma(data, n)
            d = barcode_decode(y, W, H, pos) if y is not None else None
            if d is None or d[0] == last:
                continue
            last = d[0]
            first.append(((int(ts*1000) - d[0]) & 0xffffffff) + (ts*1000 % 1))
            complete.append(((int(tdq*1000) - d[0]) & 0xffffffff) + (tdq*1000 % 1))
    first    = [x for x in first if x < 1000]
    complete = [x for x in complete if x < 1000]
    return {"buffer_timestamp": stats(first), "dequeue": stats(complete)}, src, calib

def set_source_mode():
    """Source (HDMI-0) mode; an output left off (hotplug) is placed right of the other monitors."""
    global SOURCE_X
    mons  = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True).stdout
    geo   = [re.search(r"(\d+)/\d+x\d+/\d+\+(\d+)\+\d+\s+(\S+)$", l) for l in mons.splitlines()]
    geo   = [(int(m.group(1)), int(m.group(2)), m.group(3)) for m in geo if m]
    right = max([w + x for w, x, name in geo if name != "HDMI-0"] or [0])
    subprocess.run(["xrandr", "--output", "HDMI-0",
        "--mode", f"{W}x{H}",
        "--rate", str(SRC_RATE),
        "--pos",  f"{right}x0",
    ], check=True)
    time.sleep(4)
    from source import output_geometry
    SOURCE_X = output_geometry("HDMI-0")[2]

def source_up(pos, seconds):
    """Start the barcode source (after the hot plug for HPD_ON_STREAM devices).

    Return (proc, calibration), the calibration being the source render -> HDMI-0 frame buffer time.
    """
    def connected():
        xrandr = subprocess.run(["xrandr"], capture_output=True, text=True).stdout
        return "HDMI-0 connected" in xrandr
    if HPD_ON_STREAM:
        deadline = time.time() + 15
        while time.time() < deadline and not connected():
            time.sleep(0.5)
        set_source_mode()
    src = start([sys.executable, os.path.join(ROOT, "software", "source.py"),
        "--output",      "HDMI-0",
        "--content",     "gray",
        "--barcode-pos", pos,
        "--duration",    str(seconds + 40),
    ])
    time.sleep(3)
    return src, latgrab(SOURCE_X + BLOCK, barcode_y(pos), 3, BLOCK)

def measure(player, pos, seconds, tries=3):
    """One player/position run: source + player fullscreen, then the screen probe."""
    for attempt in range(tries):
        if player == "v4l2":
            res, src, calib = measure_v4l2(pos, seconds)
            stop(src)
            time.sleep(2)
            res["calibration"] = calib
            return res
        src = calib = None
        if not HPD_ON_STREAM:
            src, calib = source_up(pos, seconds)
        if player == "direct":
            # camlink_view on a monitor taken from X (--vk/--drm in --viewer-args): no X probe
            # possible, the viewer reports its stages and the computed scanout of the rows.
            csv = os.path.join(CSV_DIR, f"{LABEL}_direct_{pos}.csv")
            cmd = [VIEWER,
                "--device",  "stock" if FIRMWARE == "stock" else "camlink",
                "--format",  "yuy2",
                "--size",    f"{W}x{H}",
                "--fps",     str(FPS),
                "--latency",
                "--seconds", str(seconds),
                "--csv",     csv,
            ] + VIEWER_ARGS
            out = subprocess.run(cmd, capture_output=True, text=True).stdout
            stop(src)
            time.sleep(2)
            res = {
                "calibration": calib,
                "output":      [l for l in out.splitlines() if l.startswith(("vk:", "drm:"))],
            }
            for line in out.splitlines():
                if line.startswith("{") and f'"pos": "{pos}"' in line:
                    res["viewer_stages"] = json.loads(line)
            if "viewer_stages" in res:
                return res
            print(f"  retry ({player}, {pos}): {out[-300:]}", flush=True)
            continue
        proc = None
        log  = None
        if player == "viewer":
            log = open(os.path.join(CSV_DIR, f"{LABEL}_{player}_{pos}.log"), "w")
        if player != "none":
            cmd = player_cmd(player)
            if player == "viewer":
                cmd += ["--csv", os.path.join(CSV_DIR, f"{LABEL}_viewer_{pos}.csv")]
            proc = start(cmd, log)
            if HPD_ON_STREAM: # The player opened the device: hot plug, then the source.
                src, calib = source_up(pos, seconds)
            # Wait for the player to show the barcode (valid decodes on DP-1).
            deadline = time.time() + 10
            def shown():
                return latgrab(DISP_BLOCK, disp_y(pos), 0.5).get("samples", 0) >= 5
            while time.time() < deadline and not shown():
                if proc.poll() is not None:
                    break
            time.sleep(2) # Warm up (ffplay queues, uvcvideo).
        res = latgrab(DISP_BLOCK, disp_y(pos), seconds) if player != "none" else calib
        alive = proc is None or proc.poll() is None
        stop(proc)
        stop(src)
        time.sleep(2)
        if log:
            log.close()
            for line in open(log.name):
                if line.startswith("{") and f'"pos": "{pos}"' in line:
                    res["viewer_stages"] = json.loads(line)
        if alive and res.get("samples", 0) > seconds*FPS*0.5:
            res["calibration"] = calib
            res["attempt"]     = attempt + 1
            return res
        print(f"  retry ({player}, {pos}): alive={alive}, samples={res.get('samples')}", flush=True)
    return res

# Main ---------------------------------------------------------------------------------------------

def main():
    global DEVICE, FIRMWARE, VIEWER_ARGS, LABEL, HPD_ON_STREAM
    global W, H, FPS, SRC_RATE, PIXFMT, BLOCK
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--firmware",      required=True,
        help="Label: stock, camlink_4k or another device (ms2109...).")
    parser.add_argument("--mode",          default="1920x1080",
        help="Source (HDMI-0) and capture size.")
    parser.add_argument("--rate",          type=float, default=60, help="Source refresh (Hz).")
    parser.add_argument("--capture-fps",   type=int,   default=None,
        help="Capture rate (default: the source refresh).")
    parser.add_argument("--pixfmt",        default="YUYV", choices=["YUYV", "MJPG"])
    parser.add_argument("--device-name",   default=None,
        help="V4L2 card name (default: from --firmware).")
    parser.add_argument("--hpd-on-stream", action="store_true",
        help="The device asserts hot plug only while streaming (MS2109).")
    parser.add_argument("--players",       default="ffplay,viewer")
    parser.add_argument("--pos",           default="top,bottom")
    parser.add_argument("--seconds",       type=float, default=20)
    parser.add_argument("--label",
        help="Results key (default: the firmware), e.g. for viewer/driver variants.")
    parser.add_argument("--viewer-args",   default="",
        help="Extra camlink_view arguments (e.g. --front).")
    args = parser.parse_args()

    FIRMWARE    = args.firmware
    VIEWER_ARGS = args.viewer_args.split()
    label       = args.label or args.firmware
    LABEL       = label
    os.makedirs(CSV_DIR, exist_ok=True)
    from v4l2cap import find_device
    W, H     = map(int, args.mode.split("x"))
    SRC_RATE = args.rate
    FPS      = args.capture_fps or round(args.rate)
    PIXFMT   = args.pixfmt
    BLOCK    = W // (24 + 8)
    name     = args.device_name or ("Cam Link 4K" if args.firmware == "stock" else "CamLink 4K")
    DEVICE   = find_device(name) or DEVICE
    print(f"device: {DEVICE}", flush=True)
    HPD_ON_STREAM = args.hpd_on_stream
    if not HPD_ON_STREAM:
        set_source_mode()
    results = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    for player in args.players.split(","):
        for pos in args.pos.split(","):
            print(f"{label} / {player} / barcode {pos}...", flush=True)
            res = measure(player, pos, args.seconds)
            res["date"] = time.strftime("%Y-%m-%d %H:%M")
            results.setdefault(label, {}).setdefault(player, {})[pos] = res
            print(f"  {json.dumps(res)}", flush=True)
            json.dump(results, open(RESULTS, "w"), indent=2)

if __name__ == "__main__":
    main()
