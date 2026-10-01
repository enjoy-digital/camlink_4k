#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""HDMI test source: full screen test content on a host output (e.g. HDMI-0 -> Cam Link input).

Each rendered frame carries a barcode (top rows of blocks) with the host CLOCK_MONOTONIC time (ms,
32 bits) and a frame counter (16 bits), decoded on the capture side to measure latency, drops and
duplicates. The rest of the screen shows a selectable chart (bars, zone plate, text, levels,
gradient, moving box).
"""

import re
import time
import argparse
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Barcode ------------------------------------------------------------------------------------------

BARCODE_BITS = 48 # 32-bit ms timestamp + 16-bit frame counter.
BARCODE_COLS = 24 # Blocks per row (2 rows).

def barcode_geometry(width, height, pos="top"):
    """Return (block size, x0, y0) (top: first rows of the frame, bottom: last rows)."""
    block = width // (BARCODE_COLS + 8) # Leaves margins.
    y0    = block if pos == "top" else height - 4*block
    return block, block, y0

def barcode_value(ms, frame):
    """Pack the ms timestamp (32-bit) and the frame counter (16-bit) in the barcode value."""
    return (ms & 0xffffffff) | ((frame & 0xffff) << 32)

def barcode_decode(rgb, width, height, pos="top"):
    """Decode (ms, frame) from an RGB (or luma) frame at the given output size, None if invalid."""
    block, x0, y0 = barcode_geometry(width, height, pos)
    luma          = rgb if rgb.ndim == 2 else rgb[..., 1]
    value         = 0
    for bit in range(BARCODE_BITS):
        row, col = divmod(bit, BARCODE_COLS)
        cx       = x0 + col*block + block//2
        cy       = y0 + row*block + block//2
        patch    = luma[cy - block//4:cy + block//4, cx - block//4:cx + block//4]
        value |= int(patch.mean() > 128) << bit
    # Guard blocks (row 3): white/black/white/black.
    gy     = y0 + 2*block + block//2
    guards = [luma[gy, x0 + i*block + block//2] > 128 for i in range(4)]
    if guards != [True, False, True, False]:
        return None
    return value & 0xffffffff, value >> 32

# Charts -------------------------------------------------------------------------------------------

EIA_BARS = [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
            (191, 0, 191), (191, 0, 0), (0, 0, 191)]

def chart(name, width, height):
    """Render a test chart as an RGB array."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    if name == "bars":
        for i, c in enumerate(EIA_BARS):
            img[:, i*width//7:(i + 1)*width//7] = c
        ramp = np.linspace(0, 255, width).astype(np.uint8)
        img[height*7//8:, :, :] = ramp[None, :, None]
    elif name == "zoneplate":
        y, x = np.mgrid[0:height, 0:width].astype(np.float32)
        r2   = (x - width/2)**2 + (y - height/2)**2
        k    = np.pi/(2*max(width, height))
        img[...] = (127.5 + 127.5*np.cos(k*r2))[..., None].astype(np.uint8)
    elif name == "text":
        pil  = Image.new("RGB", (width, height), (255, 255, 255))
        draw = ImageDraw.Draw(pil)
        font = ImageFont.load_default()
        for row, y in enumerate(range(0, height, 12)):
            line = f"{row:04d} The quick brown fox jumps over the lazy dog 0123456789 "
            draw.text((4, y), line*8, fill=(0, 0, 0), font=font)
        img[...] = np.asarray(pil)
    elif name == "levels":
        for i, v in enumerate([0, 16, 64, 128, 192, 235, 255]):
            img[:, i*width//7:(i + 1)*width//7] = v
    elif name == "gradient":
        g = np.linspace(0, 255, width).astype(np.uint8)
        img[:height//3]            = np.stack([g, g*0, g*0], axis=-1)[None]
        img[height//3:2*height//3] = np.stack([g*0, g, g*0], axis=-1)[None]
        img[2*height//3:]          = np.stack([g*0, g*0, g], axis=-1)[None]
    elif name == "gray":
        img[...] = 128
    return img

# Output Helpers -----------------------------------------------------------------------------------

def output_geometry(output):
    """Return (width, height, x, y) of an xrandr output."""
    txt = subprocess.run(["xrandr"], capture_output=True, text=True).stdout
    m   = re.search(rf"^{output} connected (?:primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", txt, re.M)
    if not m:
        raise RuntimeError(f"{output} not connected/active.")
    return tuple(int(v) for v in m.groups())

def set_mode(output, mode, rate=None):
    """Set the mode (and rate) of an xrandr output."""
    cmd = ["xrandr", "--output", output, "--mode", mode]
    if rate:
        cmd += ["--rate", str(rate)]
    subprocess.run(cmd, check=True)
    time.sleep(3) # Let the sink relock.

# Renderer -----------------------------------------------------------------------------------------

def run(output, content, moving=False, duration=None, barcode_pos="top"):
    """Show the chart and the live barcode full screen on the output (Tk)."""
    import tkinter as tk
    from PIL import ImageTk

    width, height, x, y = output_geometry(output)
    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.configure(background="black", cursor="none")
    canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bg="black")
    canvas.pack()
    bg = ImageTk.PhotoImage(Image.fromarray(chart(content, width, height)))
    canvas.create_image(0, 0, image=bg, anchor="nw")

    block, x0, y0 = barcode_geometry(width, height, barcode_pos)
    canvas.create_rectangle(x0 - block//2, y0 - block//2, x0 + (BARCODE_COLS + 0.5)*block,
        y0 + 3.5*block, fill="gray50", outline="")
    cells = []
    for bit in range(BARCODE_BITS):
        row, col = divmod(bit, BARCODE_COLS)
        cells.append(canvas.create_rectangle(x0 + col*block, y0 + row*block,
            x0 + (col + 1)*block, y0 + (row + 1)*block, fill="black", outline=""))
    for i in range(4):
        canvas.create_rectangle(x0 + i*block, y0 + 2*block, x0 + (i + 1)*block, y0 + 3*block,
            fill="white" if i % 2 == 0 else "black", outline="")
    box = None
    if moving:
        box = canvas.create_rectangle(0, height//2, block*4, height//2 + block*4,
            fill="white", outline="")

    state = {"frame": 0, "bits": [None]*BARCODE_BITS, "start": time.monotonic()}
    def update():
        ms    = int(time.clock_gettime(time.CLOCK_MONOTONIC)*1000)
        value = barcode_value(ms, state["frame"])
        for bit in range(BARCODE_BITS):
            b = (value >> bit) & 1
            if state["bits"][bit] != b:
                canvas.itemconfigure(cells[bit], fill="white" if b else "black")
                state["bits"][bit] = b
        if box is not None:
            bx = (state["frame"]*8) % (width - block*4)
            canvas.coords(box, bx, height//2, bx + block*4, height//2 + block*4)
        state["frame"] += 1
        if duration and time.monotonic() - state["start"] > duration:
            root.destroy()
            return
        root.after(4, update)
    root.after(100, update)
    root.mainloop()

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HDMI test source (full screen on an xrandr output).")
    parser.add_argument("--output",   default="HDMI-0")
    parser.add_argument("--content",  default="bars",
        help="bars, zoneplate, text, levels, gradient, gray.")
    parser.add_argument("--mode",     help="Set the output mode first (e.g. 1920x1080).")
    parser.add_argument("--rate",     type=float)
    parser.add_argument("--moving",   action="store_true")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--barcode-pos", default="top", choices=["top", "bottom"],
        help="Barcode rows at the top or bottom of the frame.")
    args = parser.parse_args()
    if args.mode:
        set_mode(args.output, args.mode, args.rate)
    run(args.output, args.content,
        moving      = args.moving,
        duration    = args.duration,
        barcode_pos = args.barcode_pos,
    )

if __name__ == "__main__":
    main()
