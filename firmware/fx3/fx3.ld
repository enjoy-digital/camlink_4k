/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FX3 memory map: SRAM 0x40000000-0x40080000 (512KB), first 12KB for DMA descriptors.
 */

ENTRY(_start)

SECTIONS
{
    . = 0x40003000;

    /* Stacks in DTCM (8KB). */
    __stack_sys   = 0x10002000; /* 6KB.  */
    __stack_undef = 0x10000800; /* 128B. */
    __stack_abort = 0x10000780; /* 128B. */
    __stack_fiq   = 0x10000700; /* 256B. */
    __stack_irq   = 0x10000600; /* 1KB.  */
    __stack_svc   = 0x10000200; /* 512B. */

    .text : {
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    }

    .data : {
        *(.data*)
        . = ALIGN(4);
    }

    .bss : {
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    }

    /DISCARD/ : {
        *(.ARM.exidx*)
        *(.ARM.extab*)
    }

    _end = .;
    ASSERT(_end <= 0x40080000, "Image does not fit in the 512KB SRAM.")
}
