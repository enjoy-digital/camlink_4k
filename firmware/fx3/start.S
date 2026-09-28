/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FX3 (ARM926EJ-S) startup: exception vectors, TCMs, stacks, BSS clear.
 */

#define DTCM_BASE 0x10000000
#define DTCM_SIZE 4 /* 512 << 4 = 8KB.  */
#define ITCM_BASE 0x00000000
#define ITCM_SIZE 5 /* 512 << 5 = 16KB. */

    .text
    .globl _start, irq_install_vectors

vectors:
_start:
    ldr pc, reset_addr           /* Reset.                     */
1:  b   1b                       /* Undefined.                 */
1:  b   1b                       /* SWI.                       */
1:  b   1b                       /* Prefetch abort.            */
1:  b   1b                       /* Data abort.                */
1:  b   1b                       /* Reserved.                  */
    ldr pc, vectors - 0x100      /* IRQ: VIC vectored address. */
1:  b   1b                       /* FIQ.                       */

reset_addr:
    .word reset

/* Copy the vectors to ITCM (address 0). */
irq_install_vectors:
    mov a1, #0
    adr a2, vectors
    adr a3, irq_install_vectors
    sub a3, a3, a2
    b   memcpy

reset:
    /* Enable TCMs. */
    msr CPSR_c, #0xd1
    mov r0, #DTCM_BASE
    orr r0, r0, #((DTCM_SIZE << 2) | 1)
    mcr p15, 0, r0, c9, c1, 0
    mov r0, #ITCM_BASE
    orr r0, r0, #((ITCM_SIZE << 2) | 1)
    mcr p15, 0, r0, c9, c1, 1

    /* Stacks (interrupts disabled). */
    ldr sp, =__stack_fiq - 8
    msr CPSR_c, #0xd7
    ldr sp, =__stack_abort - 8
    msr CPSR_c, #0xdb
    ldr sp, =__stack_undef - 8
    msr CPSR_c, #0xd2
    ldr sp, =__stack_irq - 8
    msr CPSR_c, #0xd3
    ldr sp, =__stack_svc - 8
    msr CPSR_c, #0xdf
    ldr sp, =__stack_sys - 8

    /* Clear BSS. */
    mov fp, #0
    mov r7, #0
    ldr a1, =__bss_start
    mov a2, #0
    ldr a3, =__bss_end
    sub a3, a3, a1
    bl  memset

    bl  main
1:  b   1b
