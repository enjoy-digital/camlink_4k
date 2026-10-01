#!/usr/bin/env python3

#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Display latency: source render (HDMI-0 barcode) -> pixels in the displayed frame buffer.

software/source.py draws a barcode (host CLOCK_MONOTONIC ms) on HDMI-0 (-> Cam Link input), a
player shows the capture fullscreen on the primary output (DP-1), software/viewer/latgrab polls
the barcode region of the screen (XShm) and timestamps the first appearance of each value. The
same probe on HDMI-0 itself gives the source's own render -> frame buffer time (calibration).
Monitor scanout/processing is not included (same for all players).

Players: ffplay (low latency options, fullscreen) and camlinx_view (CamLinX or stock, its own
stage timestamps: first payload of the frame, barcode rows received, present), plus the V4L2 path
alone ("v4l2": uvcvideo buffer timestamp and dequeue time, what ffplay receives).
Results are appended to doc/bench/latency_display.json (per sample CSVs in doc/bench/latency/).
"""

import os
import sys
import json
import time
import signal
import argparse
import subprocess

ROOT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LATGRAB = os.path.join(ROOT, "software", "viewer", "latgrab")
VIEWER  = os.path.join(ROOT, "software", "viewer", "camlinx_view")
RESULTS = os.path.join(ROOT, "doc", "bench", "latency_display.json")
CSV_DIR = os.path.join(ROOT, "doc", "bench", "latency")

W, H, FPS    = 1920, 1080, 60
SOURCE_X     = 1920 # HDMI-0 position (right of DP-1).
BLOCK        = W // (24 + 8)

FFPLAY_LOW_LATENCY = ["-fflags", "nobuffer", "-flags", "low_delay", "-framedrop", "-sync", "ext",
    "-probesize", "32", "-analyzeduration", "0"]

def barcode_y(pos):
    return BLOCK if pos == "top" else H - 4*BLOCK

def latgrab(x0, y0, seconds):
    out = subprocess.run([LATGRAB, "--x0", str(x0), "--y0", str(y0), "--block", str(BLOCK),
        "--seconds", str(seconds)], capture_output=True, text=True).stdout.strip()
    return json.loads(out.splitlines()[-1]) if out else {"samples": 0}

def start(cmd, out=None):
    return subprocess.Popen(cmd, stdout=out or subprocess.DEVNULL, stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
        preexec_fn=os.setsid)

def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

DEVICE   = "/dev/video0"
FIRMWARE = "camlinx_4k"

def player_cmd(player):
    if player == "ffplay":
        return ["ffplay", "-hide_banner", "-loglevel", "quiet", "-fs", "-left", "0", "-top", "0"] + FFPLAY_LOW_LATENCY + \
            ["-f", "v4l2", "-input_format", "yuyv422", "-video_size", f"{W}x{H}", "-framerate", str(FPS), "-i", DEVICE]
    return [VIEWER, "--device", "stock" if FIRMWARE == "stock" else "camlinx", "--format", "yuy2",
        "--size", f"{W}x{H}", "--fps", str(FPS), "--fullscreen", "--latency"]

def stats(values):
    import numpy as np
    v = np.array(values)
    return {"samples": len(v), "median_ms": round(float(np.median(v)), 1), "p5_ms": round(float(np.percentile(v, 5)), 1),
        "p95_ms": round(float(np.percentile(v, 95)), 1)} if len(v) else {"samples": 0}

def measure_v4l2(pos, seconds):
    """uvcvideo path alone: buffer timestamp (uvcvideo: first payload) and dequeue (frame complete)."""
    from v4l2cap import Capture
    from bench import to_luma
    from source import barcode_decode
    first, complete, last = [], [], None
    with Capture(DEVICE, W, H, FPS, "YUYV") as cap:
        cap.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            data, ts, seq, tdq, n, flags = cap.read()
            d = barcode_decode(to_luma(data, "YUYV", W, H), W, H, pos) if n >= W*H*2 else None
            if d is None or d[0] == last:
                continue
            last = d[0]
            first.append(((int(ts*1000) - d[0]) & 0xffffffff) + (ts*1000 % 1))
            complete.append(((int(tdq*1000) - d[0]) & 0xffffffff) + (tdq*1000 % 1))
    first    = [x for x in first if x < 1000]
    complete = [x for x in complete if x < 1000]
    return {"buffer_timestamp": stats(first), "dequeue": stats(complete)}

def measure(player, pos, seconds, tries=3):
    """One player/position run: source + player fullscreen, then the screen probe."""
    y0 = barcode_y(pos)
    for attempt in range(tries):
        src = start([sys.executable, os.path.join(ROOT, "software", "source.py"), "--output", "HDMI-0",
            "--content", "gray", "--barcode-pos", pos, "--duration", str(seconds + 30)])
        time.sleep(3)
        calib = latgrab(SOURCE_X + BLOCK, y0, 3) # Source render -> HDMI-0 frame buffer.
        if player == "v4l2":
            res = measure_v4l2(pos, seconds)
            stop(src)
            time.sleep(2)
            res["calibration"] = calib
            return res
        proc = None
        log = open(os.path.join(CSV_DIR, f"{FIRMWARE}_{player}_{pos}.log"), "w") if player == "viewer" else None
        if player != "none":
            cmd = player_cmd(player)
            if player == "viewer":
                cmd += ["--csv", os.path.join(CSV_DIR, f"{FIRMWARE}_viewer_{pos}.csv")]
            proc = start(cmd, log)
            # Wait for the player to show the barcode (valid decodes on DP-1).
            deadline = time.time() + 10
            while time.time() < deadline and latgrab(BLOCK, y0, 0.5).get("samples", 0) < 5:
                if proc.poll() is not None:
                    break
            time.sleep(2) # Warm up (ffplay queues, uvcvideo).
        res = latgrab(BLOCK, y0, seconds) if player != "none" else calib
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
            res["attempt"] = attempt + 1
            return res
        print(f"  retry ({player}, {pos}): alive={alive}, samples={res.get('samples')}", flush=True)
    return res

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--firmware", required=True, help="Label: stock or camlinx_4k (the loaded firmware).")
    parser.add_argument("--players",  default="ffplay,viewer")
    parser.add_argument("--pos",      default="top,bottom")
    parser.add_argument("--seconds",  type=float, default=20)
    args = parser.parse_args()

    global DEVICE, FIRMWARE
    FIRMWARE = args.firmware
    os.makedirs(CSV_DIR, exist_ok=True)
    from v4l2cap import find_device
    DEVICE = find_device("Cam Link 4K" if args.firmware == "stock" else "CamLinX") or DEVICE
    print(f"device: {DEVICE}", flush=True)
    subprocess.run(["xrandr", "--output", "HDMI-0", "--mode", f"{W}x{H}", "--rate", str(FPS)], check=True)
    time.sleep(6)
    results = json.load(open(RESULTS)) if os.path.exists(RESULTS) else {}
    for player in args.players.split(","):
        for pos in args.pos.split(","):
            print(f"{args.firmware} / {player} / barcode {pos}...", flush=True)
            res = measure(player, pos, args.seconds)
            res["date"] = time.strftime("%Y-%m-%d %H:%M")
            results.setdefault(args.firmware, {}).setdefault(player, {})[pos] = res
            print(f"  {json.dumps(res)}", flush=True)
            json.dump(results, open(RESULTS, "w"), indent=2)

if __name__ == "__main__":
    main()
