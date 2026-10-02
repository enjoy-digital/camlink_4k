/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef SDRAM_H
#define SDRAM_H

#include <stdint.h>

enum {
    SDRAM_STATE_IDLE    = 0, /* Not initialized. */
    SDRAM_STATE_RUNNING = 1,
    SDRAM_STATE_OK      = 2,
    SDRAM_STATE_FAILED  = 3,
    SDRAM_STATE_NONE    = 4, /* Bitstream (firmware CSR map) without DRAM. */
};

/* DRAM init result (VREQ_SDRAM_STATUS). */
struct sdram_status {
    uint8_t  state;
    uint8_t  attempts;    /* PHY init sequences tried.                        */
    uint8_t  rate;        /* 1:4 rate crossing: sel | shift << 1.             */
    uint8_t  modules;
    uint8_t  bitslip[4];  /* Read leveling per module.                        */
    uint8_t  delay[4];
    uint8_t  window[4];   /* Passing delays (0: no window).                   */
    uint32_t bist_errors; /* Last BIST check (1MB), 0xffffffff: not run.      */
    uint32_t csr_errors;  /* I2C CSR access errors.                           */
    uint32_t scan[4];     /* Last leveling scan per bitslip: bit 2*delay + module = pass. */
};

extern struct sdram_status sdram_status;

/* Init + read leveling + BIST check (blocking, a few seconds over the I2C CSR bridge). Returns 0
 * when the DRAM is usable (or when the bitstream has no DRAM: state NONE, returns -1). */
int sdram_init(void);

/* Debug: one try at the given rate crossing setting (sel | pair << 1), no PHY init replay. */
int sdram_init_rate(uint32_t rate);

static inline int sdram_ok(void)
{
    return sdram_status.state == SDRAM_STATE_OK;
}

#endif /* SDRAM_H */
