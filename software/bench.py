#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Stock Elgato firmware vs CamLink 4K benchmark (same unit, same cable, same host source).

Source: software/source.py on a host output (HDMI-0 -> Cam Link). Firmwares are RAM-loaded (the
stock FX3 image from the flash backup loads the stock bitstream still in flash).

Results: doc/bench/results_<firmware>.json + doc/BENCHMARK.md (report command).
"""

import os
import sys
import json
import time
import signal
import resource
import argparse
import subprocess

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import stock
from v4l2cap import Capture, find_device, yuy2_to_rgb, yuy2_luma, m420_to_rgb, m420_to_yuv
from source  import barcode_decode, barcode_geometry, chart, output_geometry

import usb.core

ROOT      = os.path.join(os.path.dirname(__file__), "..")
BENCH_DIR = os.path.join(ROOT, "doc", "bench")
STOCK_IMG = os.path.expanduser("~/camlink_backup/stock_fx3.img")
OUTPUT    = "HDMI-0"

# Firmware Switching -------------------------------------------------------------------------------

def usb_state():
    if usb.core.find(idVendor=0x04b4, idProduct=0x00f3) is not None:
        return "bootloader"
    if usb.core.find(idVendor=0x0fd9) is not None:
        return "stock"
    from camlink import find_device as cl_find, CAMLINK_VID, CAMLINK_PID
    if cl_find(CAMLINK_VID, CAMLINK_PID) is not None:
        return "camlink_4k"
    return "none"

def wait_state(target, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if usb_state() == target:
            return True
        time.sleep(0.5)
    return False

def to_bootloader():
    state = usb_state()
    if state == "camlink_4k":
        subprocess.run([sys.executable, os.path.join(ROOT, "software", "camlink.py"), "reboot"], cwd=ROOT)
    elif state == "stock":
        stock.cold_reset()
    if not wait_state("bootloader"):
        raise RuntimeError(f"Could not reach the FX3 bootloader (state {usb_state()}).")

def select_firmware(fw):
    if usb_state() == fw:
        return
    to_bootloader()
    if fw == "stock":
        subprocess.run([sys.executable, os.path.join(ROOT, "software", "camlink.py"), "fx3-load", STOCK_IMG], cwd=ROOT, check=True)
    else:
        subprocess.run([sys.executable, os.path.join(ROOT, "software", "camlink.py"), "boot"], cwd=ROOT, check=True)
    if not wait_state(fw, timeout=30):
        raise RuntimeError(f"{fw} did not enumerate.")
    time.sleep(4) # uvcvideo enumeration.
    reprobe_output()

def screen_locked():
    out = subprocess.run(["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver", "--object-path",
        "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"], capture_output=True, text=True).stdout
    return "true" in out

def reprobe_output():
    """Re-probe the HDMI output (the NVIDIA source only restarts after a re-probe)."""
    subprocess.run(["xrandr", "--output", OUTPUT, "--off"])
    time.sleep(2)
    subprocess.run(["xrandr", "--output", OUTPUT, "--auto", "--right-of", "DP-1"])
    time.sleep(5)

def video_node(fw):
    return find_device("CamLink 4K" if fw == "camlink_4k" else "Cam Link 4K")

# Source Control -----------------------------------------------------------------------------------

class Source:
    def __init__(self, content="bars", moving=False):
        cmd = [sys.executable, os.path.join(ROOT, "software", "source.py"), "--output", OUTPUT, "--content", content]
        if moving:
            cmd.append("--moving")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

def set_mode(mode, rate):
    subprocess.run(["xrandr", "--output", OUTPUT, "--mode", mode, "--rate", str(rate)], check=True)
    time.sleep(5)

# Frame Helpers ------------------------------------------------------------------------------------

def to_rgb(data, fmt, w, h):
    if fmt == "YUYV":
        return yuy2_to_rgb(data, w, h)
    if fmt == "M420":
        return m420_to_rgb(data, w, h)
    # NV12.
    f = np.frombuffer(data, dtype=np.uint8)
    y = f[:w*h].reshape(h, w).astype(np.float32) - 16
    uv = f[w*h:w*h + w*h//2].reshape(h//2, w//2, 2).astype(np.float32) - 128
    u = np.repeat(np.repeat(uv[..., 0], 2, axis=0), 2, axis=1)
    v = np.repeat(np.repeat(uv[..., 1], 2, axis=0), 2, axis=1)
    r = 1.164*y + 1.793*v
    g = 1.164*y - 0.213*u - 0.533*v
    b = 1.164*y + 2.112*u
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)

def to_luma(data, fmt, w, h):
    if fmt == "YUYV":
        return yuy2_luma(data, w, h)
    if fmt == "M420":
        return m420_to_yuv(data, w, h)[0]
    return np.frombuffer(data, dtype=np.uint8)[:w*h].reshape(h, w)

def psnr(a, b):
    mse = np.mean((a.astype(np.float64) - b.astype(np.float64))**2)
    return 99.0 if mse == 0 else 10*np.log10(255**2/mse)

def ssim(a, b):
    """Mean SSIM on luma (8x8 blocks, no external deps)."""
    from scipy.ndimage import uniform_filter
    a = a.astype(np.float64); b = b.astype(np.float64)
    c1, c2 = (0.01*255)**2, (0.03*255)**2
    ma, mb = uniform_filter(a, 8), uniform_filter(b, 8)
    va = uniform_filter(a*a, 8) - ma*ma
    vb = uniform_filter(b*b, 8) - mb*mb
    cab = uniform_filter(a*b, 8) - ma*mb
    s = ((2*ma*mb + c1)*(2*cab + c2))/((ma*ma + mb*mb + c1)*(va + vb + c2))
    return float(np.mean(s))

# Tests --------------------------------------------------------------------------------------------

def test_latency_pacing(node, fmt, w, h, fps, seconds):
    """Latency (render -> first byte / frame complete), fps, jitter, drops/duplicates."""
    src = Source("bars", moving=True)
    try:
        ru0 = resource.getrusage(resource.RUSAGE_SELF)
        with Capture(node, w, h, fps, fmt) as cap:
            t0 = time.monotonic()
            cap.start()
            first = None
            rows  = []
            while time.monotonic() - t0 < seconds:
                data, ts, seq, tdq, n, flags = cap.read()
                if first is None:
                    first = tdq - t0
                d = barcode_decode(to_luma(data, fmt, w, h), w, h) if n >= w*h else None
                rows.append((ts, tdq, seq, n, d))
        ru1 = resource.getrusage(resource.RUSAGE_SELF)
    finally:
        src.stop()
    ts  = np.array([r[0] for r in rows])
    seq = np.array([r[2] for r in rows])
    dec = [r for r in rows if r[4] is not None]
    lat_first    = np.array([((int(r[0]*1000) - r[4][0]) & 0xffffffff) for r in dec], dtype=np.int64)
    lat_complete = np.array([((int(r[1]*1000) - r[4][0]) & 0xffffffff) for r in dec], dtype=np.int64)
    lat_first    = lat_first[lat_first < 1000]
    lat_complete = lat_complete[lat_complete < 1000]
    render_ms = [r[4][0] for r in dec]
    dup = sum(1 for a, b in zip(render_ms, render_ms[1:]) if a == b)
    dt  = np.diff(ts)*1000 if len(ts) > 1 else np.array([0])
    cpu = (ru1.ru_utime + ru1.ru_stime - ru0.ru_utime - ru0.ru_stime)/max(seconds, 1)
    return {
        "frames":            len(rows),
        "decoded":           len(dec),
        "time_to_first_s":   round(first or -1, 3),
        "fps":               round((len(rows) - 1)/((ts[-1] - ts[0]) or 1), 2) if len(rows) > 1 else 0,
        "interval_ms_mean":  round(float(np.mean(dt)), 2),
        "interval_ms_std":   round(float(np.std(dt)), 2),
        "interval_ms_max":   round(float(np.max(dt)), 2),
        "seq_gaps":          int(np.sum(np.diff(seq) > 1)) if len(seq) > 1 else 0,
        "duplicates":        dup,
        "short_frames":      sum(1 for r in rows if r[3] < w*h*(2 if fmt == "YUYV" else 1.5)),
        "lat_first_ms":      {k: round(float(f(lat_first)), 1) for k, f in [("median", np.median), ("p95", lambda x: np.percentile(x, 95)), ("max", np.max)]} if len(lat_first) else None,
        "lat_complete_ms":   {k: round(float(f(lat_complete)), 1) for k, f in [("median", np.median), ("p95", lambda x: np.percentile(x, 95)), ("max", np.max)]} if len(lat_complete) else None,
        "capture_cpu_pct":   round(cpu*100, 1),
    }

def test_quality(node, fmt, w, h, fps, fw, tag):
    """PSNR/SSIM of charts vs rendered reference (barcode area excluded)."""
    sw, sh, _, _ = output_geometry(OUTPUT)
    block, x0, y0 = barcode_geometry(w, h)
    mask_h = y0 + 4*block
    results = {}
    for name in ["bars", "zoneplate", "text", "levels", "gradient"]:
        src = Source(name)
        try:
            with Capture(node, w, h, fps, fmt) as cap:
                cap.start()
                for i in range(int(fps)):
                    data, *_ = cap.read()
        finally:
            src.stop()
        from PIL import Image
        ref = Image.fromarray(chart(name, sw, sh))
        if (sw, sh) != (w, h):
            ref = ref.resize((w, h), Image.BOX)
        ref = np.asarray(ref)
        rgb = to_rgb(data, fmt, w, h)
        ly  = lambda x: (0.2126*x[..., 0] + 0.7152*x[..., 1] + 0.0722*x[..., 2])
        results[name] = {
            "psnr_rgb":  round(psnr(rgb[mask_h:], ref[mask_h:]), 2),
            "ssim_luma": round(ssim(ly(rgb[mask_h:]), ly(ref[mask_h:])), 4),
        }
        if name in ("zoneplate", "text"):
            crop = rgb[h//2:h//2 + 200, w//2:w//2 + 300]
            Image.fromarray(crop).save(os.path.join(BENCH_DIR, f"{fw}_{tag}_{name}.png"))
    return results

def test_modes(node):
    out = subprocess.run(["v4l2-ctl", "-d", node, "--list-formats-ext"], capture_output=True, text=True).stdout
    return [l.strip() for l in out.splitlines() if l.strip().startswith(("[", "Size", "Interval"))]

def test_start_stop(node, fmt, w, h, fps, cycles=20):
    ok, times = 0, []
    for i in range(cycles):
        try:
            with Capture(node, w, h, fps, fmt) as cap:
                t0 = time.monotonic()
                cap.start()
                data, *_ = cap.read()
                times.append(time.monotonic() - t0)
                ok += 1
        except OSError:
            time.sleep(0.5)
    return {"cycles": cycles, "ok": ok, "first_frame_s_median": round(float(np.median(times)), 3) if times else None}

# Runner -------------------------------------------------------------------------------------------

# (source mode, rate, capture per firmware: fmt, w, h, fps)
SCENARIOS = {
    "1080p60": ("1920x1080", 60, {"stock": ("YUYV", 1920, 1080, 60), "camlink_4k": ("YUYV", 1920, 1080, 60)}),
    "1080p30": ("1920x1080", 29.97, {"stock": ("YUYV", 1920, 1080, 30), "camlink_4k": ("YUYV", 1920, 1080, 30)}),
    "720p60":  ("1280x720", 60, {"stock": ("YUYV", 1280, 720, 60), "camlink_4k": ("YUYV", 1280, 720, 60)}),
    "2160p30": ("3840x2160", 30, {"stock": ("NV12", 3840, 2160, 30), "camlink_4k": ("YUYV", 1920, 1080, 30)}),
    # Native 4K30 (4:2:0 both): stock NV12, CamLink 4K M420 (firmware built with PLL_FBDIV=21).
    "2160p30n": ("3840x2160", 30, {"stock": ("NV12", 3840, 2160, 30), "camlink_4k": ("M420", 3840, 2160, 30)}),
}

def run(fw, scenarios, seconds):
    os.makedirs(BENCH_DIR, exist_ok=True)
    path = os.path.join(BENCH_DIR, f"results_{fw}.json")
    results = json.load(open(path)) if os.path.exists(path) else {}
    select_firmware(fw)
    for name in scenarios:
        mode, rate, caps = SCENARIOS[name]
        fmt, w, h, fps = caps[fw]
        print(f"[{fw}] {name}: source {mode}@{rate}, capture {fmt} {w}x{h}@{fps}", flush=True)
        set_mode(mode, rate)
        node = video_node(fw)
        entry = {"source": f"{mode}@{rate}", "capture": f"{fmt} {w}x{h}@{fps}", "node": node,
                 "screen_locked": screen_locked()}
        for test, fn in [
            ("latency_pacing", lambda: test_latency_pacing(node, fmt, w, h, fps, seconds)),
            ("quality",        lambda: {"skipped": "screen locked"} if screen_locked() else test_quality(node, fmt, w, h, fps, fw, name)),
            ("start_stop",     lambda: test_start_stop(node, fmt, w, h, fps)),
            ("modes",          lambda: test_modes(node)),
        ]:
            try:
                entry[test] = fn()
            except Exception as e:
                entry[test] = {"error": repr(e)}
            print(f"  {test}: {json.dumps(entry[test])[:300]}", flush=True)
        results[name] = entry
        json.dump(results, open(path, "w"), indent=2)
    return results

# Report -------------------------------------------------------------------------------------------

def report():
    res = {}
    for fw in ("stock", "camlink_4k"):
        path = os.path.join(BENCH_DIR, f"results_{fw}.json")
        res[fw] = json.load(open(path)) if os.path.exists(path) else {}
    lines = ["# Benchmark: Stock Elgato firmware vs CamLink 4K", "",
             "Same Cam Link 4K unit, same cable, host `HDMI-0` as source (`software/source.py`), "
             "captures through uvcvideo (`software/bench.py`). Latency: host render time (barcode) to "
             "first byte (V4L2 buffer timestamp) / frame complete (dequeue).", ""]
    def g(d, *keys):
        for k in keys:
            if not isinstance(d, dict) or k not in d:
                return "-"
            d = d[k]
        return d
    for name in SCENARIOS:
        if not any(name in res[fw] for fw in res):
            continue
        lines += [f"## {name}", "", "| Metric | Stock | CamLink 4K |", "|---|---|---|"]
        rows = [
            ("Source", ("source",)), ("Capture", ("capture",)),
            ("FPS", ("latency_pacing", "fps")),
            ("Frame interval std (ms)", ("latency_pacing", "interval_ms_std")),
            ("Frame interval max (ms)", ("latency_pacing", "interval_ms_max")),
            ("Sequence gaps (drops)", ("latency_pacing", "seq_gaps")),
            ("Short frames", ("latency_pacing", "short_frames")),
            ("Latency first byte median (ms)", ("latency_pacing", "lat_first_ms", "median")),
            ("Latency frame complete median (ms)", ("latency_pacing", "lat_complete_ms", "median")),
            ("Latency frame complete p95 (ms)", ("latency_pacing", "lat_complete_ms", "p95")),
            ("Capture CPU (%)", ("latency_pacing", "capture_cpu_pct")),
            ("Start/stop OK", ("start_stop", "ok")),
            ("Time to first frame (s)", ("start_stop", "first_frame_s_median")),
        ]
        for chart_name in ["bars", "zoneplate", "text", "levels", "gradient"]:
            rows.append((f"{chart_name} PSNR (dB) / SSIM", ("quality", chart_name)))
        for label, keys in rows:
            vals = []
            for fw in ("stock", "camlink_4k"):
                v = g(res[fw].get(name, {}), *keys)
                if isinstance(v, dict):
                    v = f"{v.get('psnr_rgb', '-')} / {v.get('ssim_luma', '-')}"
                vals.append(str(v))
            lines.append(f"| {label} | {vals[0]} | {vals[1]} |")
        lines.append("")
    open(os.path.join(ROOT, "doc", "BENCHMARK.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))

def main():
    parser = argparse.ArgumentParser(description="Stock vs CamLink 4K benchmark.")
    parser.add_argument("command", choices=["run", "report", "select"])
    parser.add_argument("--firmware",  default="camlink_4k", choices=["stock", "camlink_4k"])
    parser.add_argument("--scenarios", default=",".join(SCENARIOS))
    parser.add_argument("--seconds",   default=20, type=float)
    args = parser.parse_args()
    if args.command == "run":
        run(args.firmware, args.scenarios.split(","), args.seconds)
    if args.command == "select":
        select_firmware(args.firmware)
    if args.command == "report":
        report()

if __name__ == "__main__":
    main()
