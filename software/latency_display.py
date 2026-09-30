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

Players: ffplay (low latency options, fullscreen) and camlinx_view (CamLinX firmware only).
Results are appended to doc/bench/latency_display.json.
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

def start(cmd):
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)

def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

DEVICE = "/dev/video0"

def player_cmd(player):
    if player == "ffplay":
        return ["ffplay", "-hide_banner", "-loglevel", "quiet", "-fs", "-left", "0", "-top", "0"] + FFPLAY_LOW_LATENCY + \
            ["-f", "v4l2", "-input_format", "yuyv422", "-video_size", f"{W}x{H}", "-framerate", str(FPS), "-i", DEVICE]
    return [VIEWER, "--format", "yuy2", "--size", f"{W}x{H}", "--fps", str(FPS), "--fullscreen"]

def measure(player, pos, seconds, tries=3):
    """One player/position run: source + player fullscreen, then the screen probe."""
    y0 = barcode_y(pos)
    for attempt in range(tries):
        src = start([sys.executable, os.path.join(ROOT, "software", "source.py"), "--output", "HDMI-0",
            "--content", "gray", "--barcode-pos", pos, "--duration", str(seconds + 30)])
        time.sleep(3)
        calib = latgrab(SOURCE_X + BLOCK, y0, 3) # Source render -> HDMI-0 frame buffer.
        proc = None
        if player != "none":
            proc = start(player_cmd(player))
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

    global DEVICE
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
