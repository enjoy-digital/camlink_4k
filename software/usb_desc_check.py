#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""USB configuration descriptor checker (UVC + UAC): walks the descriptors, checks lengths/links
and summarizes interfaces, Video Control chain, formats/frames and endpoints.

    usb_desc_check.py [firmware/fx3/build/fx3.elf]   # HS and SS configs of the compiled firmware.
"""

import sys
import struct
import subprocess

def parse_config(d):
    """Parse a configuration descriptor set, raise ValueError on inconsistencies."""
    if d[1] != 0x02:
        raise ValueError("Not a configuration descriptor.")
    total = struct.unpack_from("<H", d, 2)[0]
    if total != len(d):
        raise ValueError(f"wTotalLength {total} != {len(d)}.")
    res = {"interfaces": d[4], "vc_chain": [], "formats": [], "endpoints": {}, "vs_total": None,
        "vc_total": None}
    intf_nums = set()
    intf = None
    vc_start = vs_start = None
    i = 0
    while i < len(d):
        l, t = d[i], d[i + 1]
        if l < 2 or i + l > len(d):
            raise ValueError(f"Bad descriptor length {l} at {i}.")
        sub = d[i + 2] if l > 2 else None
        if t == 0x04:
            intf, cls = d[i + 2], (d[i + 5], d[i + 6])
            intf_nums.add(intf)
        elif t == 0x24 and intf == 0:
            if sub == 0x01:
                res["vc_total"] = struct.unpack_from("<H", d, i + 5)[0]
                vc_start = i
            elif sub == 0x02:
                res["vc_chain"].append((d[i + 3], None))
            elif sub == 0x03:
                res["vc_chain"].append((d[i + 3], d[i + 7]))
            elif sub == 0x05:
                # UVC 1.1+: 10 + bControlSize (bmVideoStandards after iProcessing).
                if l != 10 + d[i + 7]:
                    raise ValueError("Processing Unit length.")
                res["vc_chain"].append((d[i + 3], d[i + 4]))
            elif sub == 0x06:
                nrin = d[i + 21]
                if l != 24 + nrin + d[i + 22 + nrin]:
                    raise ValueError("Extension Unit length.")
                res["vc_chain"].append((d[i + 3], d[i + 22]))
        elif t == 0x24 and intf == 1:
            if sub == 0x01:
                res["vs_total"] = struct.unpack_from("<H", d, i + 4)[0]
                vs_start = i
                if l != 13 + d[i + 3]*d[i + 12]:
                    raise ValueError("VS input header length.")
            elif sub == 0x04:
                res["formats"].append({"index": d[i + 3], "guid": bytes(d[i + 5:i + 9]),
                    "bpp": d[i + 21], "nframes": d[i + 4], "frames": []})
            elif sub == 0x05:
                n = d[i + 25]
                if l != 26 + 4*n:
                    raise ValueError("Frame descriptor length.")
                w, h = struct.unpack_from("<HH", d, i + 5)
                maxsize = struct.unpack_from("<I", d, i + 17)[0]
                fmt = res["formats"][-1]
                if maxsize != w*h*fmt["bpp"]//8:
                    raise ValueError(f"Frame {w}x{h}: max size {maxsize}.")
                fps = [10000000//struct.unpack_from("<I", d, i + 26 + 4*k)[0] for k in range(n)]
                fmt["frames"].append({"index": d[i + 3], "w": w, "h": h, "fps": fps})
        elif t == 0x05:
            res["endpoints"][d[i + 2]] = struct.unpack_from("<H", d, i + 4)[0]
        i += l
    if len(intf_nums) != res["interfaces"]:
        raise ValueError(f"bNumInterfaces {res['interfaces']} != {len(intf_nums)}.")
    for f in res["formats"]:
        if f["nframes"] != len(f["frames"]):
            raise ValueError(f"Format {f['index']}: bNumFrameDescriptors.")
        if [fr["index"] for fr in f["frames"]] != list(range(1, len(f["frames"]) + 1)):
            raise ValueError(f"Format {f['index']}: frame indexes.")
    ids = {e for e, _ in res["vc_chain"]}
    for e, src in res["vc_chain"]:
        if src is not None and src not in ids:
            raise ValueError(f"Unit {e}: unknown source {src}.")
    return res

def configs_from_elf(elf):
    """Extract config_hs/config_ss from a firmware ELF (symbols + sections)."""
    syms = {}
    for line in subprocess.run(["arm-none-eabi-nm", "-S", elf], capture_output=True, text=True).stdout.splitlines():
        p = line.split()
        if len(p) == 4 and p[3] in ("config_hs", "config_ss"):
            syms[p[3]] = (int(p[0], 16), int(p[1], 16))
    out = {}
    hdr = subprocess.run(["arm-none-eabi-objdump", "-h", elf], capture_output=True, text=True).stdout
    for line in hdr.splitlines():
        p = line.split()
        if len(p) >= 7 and p[0].isdigit():
            name, size, vma, off = p[1], int(p[2], 16), int(p[3], 16), int(p[5], 16)
            for sym, (addr, ssize) in syms.items():
                if vma <= addr < vma + size:
                    with open(elf, "rb") as f:
                        f.seek(off + addr - vma)
                        out[sym] = f.read(ssize)
    return out

def main():
    elf = sys.argv[1] if len(sys.argv) > 1 else "firmware/fx3/build/fx3.elf"
    for name, data in sorted(configs_from_elf(elf).items()):
        cfg = parse_config(data)
        print(f"{name}: {len(data)} bytes, {cfg['interfaces']} interfaces, VC chain {cfg['vc_chain']}")
        for f in cfg["formats"]:
            frames = ", ".join(f"{fr['w']}x{fr['h']}@{'/'.join(map(str, fr['fps']))}" for fr in f["frames"])
            print(f"  {f['guid'].decode()} ({f['bpp']} bpp): {frames}")
        print(f"  endpoints: " + ", ".join(f"0x{ep:02x}/{mps}" for ep, mps in sorted(cfg["endpoints"].items())))

if __name__ == "__main__":
    main()
