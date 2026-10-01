#!/usr/bin/env python3

#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Convert an ELF to a FX3 boot image ("CY" header, sections, entry point, checksum)."""

import sys
import struct

from elftools.elf.elffile import ELFFile

def elf2img(elf_filename, img_filename):
    with open(elf_filename, "rb") as f:
        elf      = ELFFile(f)
        image    = bytearray(b"CY\x1c\xb0") # Signature, 10MHz SPI/no-reset ctl, normal image.
        checksum = 0
        for section in elf.iter_sections():
            # Loadable sections with content only (BSS is cleared by the startup code).
            if not (section["sh_flags"] & 0x2) or section["sh_type"] != "SHT_PROGBITS":
                continue
            if section["sh_size"] == 0:
                continue
            data  = section.data()
            data += b"\x00"*(-len(data) % 4)
            words = struct.unpack(f"<{len(data)//4}I", data)
            image += struct.pack("<II", len(words), section["sh_addr"]) + data
            checksum = (checksum + sum(words)) & 0xffffffff
        image += struct.pack("<II", 0, elf.header["e_entry"])
        image += struct.pack("<I", checksum)
    with open(img_filename, "wb") as f:
        f.write(image)

if __name__ == "__main__":
    elf2img(sys.argv[1], sys.argv[2])
