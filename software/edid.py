#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""EDID generator for the CamLink 4K HDMI input (base block + CEA-861 extension)."""

import struct

# Helpers ------------------------------------------------------------------------------------------

def _checksum(block):
    return (256 - (sum(block) % 256)) % 256

def _manufacturer_id(name):
    a, b, c = [ord(x) - ord("A") + 1 for x in name]
    return struct.pack(">H", (a << 10) | (b << 5) | c)

def _dtd(pclk_khz, hactive, hblank, hfp, hsync, vactive, vblank, vfp, vsync, hsize_mm=0, vsize_mm=0,
    hpol=1, vpol=1):
    """18-byte Detailed Timing Descriptor."""
    pclk = pclk_khz//10
    return bytes([
        pclk & 0xff, pclk >> 8,
        hactive & 0xff, hblank & 0xff, ((hactive >> 8) << 4) | (hblank >> 8),
        vactive & 0xff, vblank & 0xff, ((vactive >> 8) << 4) | (vblank >> 8),
        hfp & 0xff, hsync & 0xff, ((vfp & 0xf) << 4) | (vsync & 0xf),
        ((hfp >> 8) << 6) | ((hsync >> 8) << 4) | ((vfp >> 4) << 2) | (vsync >> 4),
        hsize_mm & 0xff, vsize_mm & 0xff, ((hsize_mm >> 8) << 4) | (vsize_mm >> 8),
        0, 0,
        0x18 | (vpol << 2) | (hpol << 1), # Digital separate sync.
    ])

def _descriptor(tag, data):
    return bytes([0, 0, 0, tag, 0]) + data

def _text(text):
    data = text.encode()[:13]
    if len(data) < 13:
        data += b"\n" + b" "*(12 - len(data))
    return data

# Timings ------------------------------------------------------------------------------------------

DTD_1080P60 = _dtd(148500, 1920, 280, 88, 44, 1080, 45, 4, 5, 480, 270)
DTD_2160P30 = _dtd(297000, 3840, 560, 176, 88, 2160, 90, 8, 10, 480, 270)

# CEA VICs (native first): 1080p60, 1080p50/30/25/24, 720p60/50, 480p60, 576p50 (+ 4K30/25/24).
VICS    = [16 | 0x80, 31, 34, 33, 32, 4, 19, 3, 18]
VICS_4K = [95, 94, 93]

# EDID ---------------------------------------------------------------------------------------------

def generate_edid(name="CamLink 4K", manufacturer="LCL", product=0x0001, serial=1,
    max_tmds_mhz=300, with_4k=False):
    """Generate the 256-byte EDID (base block + CEA-861 extension)."""
    # Base block.
    max_pclk = (max_tmds_mhz if with_4k else 170)//10 # Range limits max pixel clock (10MHz).

    base  = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    base += _manufacturer_id(manufacturer)
    base += struct.pack("<HI", product, serial)
    base += bytes([1, 36])                    # Week 1, 2026.
    base += bytes([1, 3])                     # EDID 1.3.
    base += bytes([0x80, 48, 27, 0x78, 0x0a]) # Digital, 48x27cm, gamma 2.2, RGB.
    # sRGB-ish chromaticity.
    base += bytes([0xee, 0x95, 0xa3, 0x54, 0x4c, 0x99, 0x26, 0x0f, 0x50, 0x54])
    # Established timings: 640x480@60, 800x600@60, 1024x768@60.
    base += bytes([0x21, 0x08, 0x00])
    # Standard timings: 1920x1080@60, 1280x720@60, 1280x800@60.
    base += bytes([0xd1, 0xc0, 0x81, 0xc0, 0x81, 0x00, 0x01, 0x01,
                   0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01])
    base += DTD_1080P60
    base += DTD_2160P30 if with_4k else _descriptor(0x10, bytes(13)) # Dummy descriptor.
    base += _descriptor(0xfc, _text(name))                           # Monitor name.
    base += _descriptor(0xfd, bytes([24, 61, 15, 136, max_pclk, 0, 0x0a]) + b" "*6) # Range limits.
    base += bytes([1])                                               # 1 extension.
    base += bytes([_checksum(base)])
    assert len(base) == 128

    # CEA-861 extension.
    vics  = VICS + (VICS_4K if with_4k else [])
    video = bytes([0x40 | len(vics)]) + bytes(vics)
    audio = bytes([0x20 | 3, 0x09, 0x04, 0x07])         # LPCM, 2ch, 48/44.1/32kHz, 16/20/24-bit.
    spk   = bytes([0x80 | 3, 0x01, 0x00, 0x00])         # Speakers: FL/FR.
    tmds  = max_tmds_mhz if with_4k else 165
    vsdb  = bytes([0x60 | 7, 0x03, 0x0c, 0x00, 0x10, 0x00, 0x00, tmds//5]) # HDMI, phys 1.0.0.0.
    dbc   = video + audio + spk + vsdb
    ext   = bytes([0x02, 0x03, 4 + len(dbc), 0x70])      # CEA v3, underscan/audio/YCbCr444/422.
    ext  += dbc
    ext  += DTD_1080P60
    ext  += bytes(127 - len(ext))
    ext  += bytes([_checksum(ext)])
    assert len(ext) == 128
    return base + ext

# Main ---------------------------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    edid = generate_edid()
    if len(sys.argv) > 1:
        open(sys.argv[1], "wb").write(edid)
    else:
        for i in range(0, 256, 16):
            print(f"{i:02x}: " + " ".join(f"{b:02x}" for b in edid[i:i + 16]))
