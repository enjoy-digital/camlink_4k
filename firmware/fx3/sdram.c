/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

/* DDR3 init from the FX3 (no CPU in the FPGA), through the I2C CSR bridge. Same flow as
 * `software/dram.py init`:
 * - DFII init sequence (generated from the gateware sdram_phy.h),
 * - 1:4 rate crossing search (`ddrphy_rate`): capture edge x read word pairing, validated by the
 *   ECP5 read leveling (bitslip x delay scan, center of the longest window, per module),
 * - read word alignment (`shift`) validated by a 1MB BIST write/check through the controller,
 * - retries with a DDR PHY init sequence replay (new CLKDIVF division phases).
 */

#include <stdint.h>

#include "fx3.h"
#include "fpga_ctrl.h"
#include "sdram.h"

#include "generated/fpga_csr.h"
#include "generated/sdram_init.h"

struct sdram_status sdram_status;

#if defined(CSR_SDRAM_DFII_CONTROL) && defined(SDRAM_PHASES)

#define DFII_CONTROL_SEL     0x01
#define DFII_CONTROL_CKE     0x02
#define DFII_CONTROL_ODT     0x04
#define DFII_CONTROL_RESET_N 0x08

#define DFII_COMMAND_CS      0x01
#define DFII_COMMAND_WE      0x02
#define DFII_COMMAND_CAS     0x04
#define DFII_COMMAND_RAS     0x08
#define DFII_COMMAND_WRDATA  0x10
#define DFII_COMMAND_RDDATA  0x20

#if SDRAM_DFI_DATABITS > 32
#error "DFI data wider than one CSR not supported."
#endif

#define SDRAM_ATTEMPTS  8
#define SDRAM_TRIES     2           /* Patterns per leveling point. */
#define SDRAM_BIST_SIZE (1 << 20)

/* CSR Access ------------------------------------------------------------------------------------ */

/* CSR accesses retried on I2C errors (counted): a lost write would silently break the init. */
static void csr_write(uint32_t addr, uint32_t value)
{
    fpga_watchdog_kick(); /* The init blocks the main loop for seconds. */
    for (int i = 0; i < 3; i++) {
        if (!fpga_csr_write(addr, value))
            return;
        sdram_status.csr_errors++;
    }
}

static uint32_t csr_read(uint32_t addr)
{
    uint32_t value = 0;
    fpga_watchdog_kick();
    for (int i = 0; i < 3; i++) {
        if (!fpga_csr_read(addr, &value))
            return value;
        sdram_status.csr_errors++;
    }
    return value;
}

/* DFII ------------------------------------------------------------------------------------------ */

#define PI(n, reg) CSR_SDRAM_DFII_PI##n##_##reg
#if SDRAM_PHASES == 4
#define PI_TABLE(reg) {PI(0, reg), PI(1, reg), PI(2, reg), PI(3, reg)}
#elif SDRAM_PHASES == 2
#define PI_TABLE(reg) {PI(0, reg), PI(1, reg)}
#else
#error "Unsupported DFI phases."
#endif

static const uint32_t pi_command[]       = PI_TABLE(COMMAND);
static const uint32_t pi_command_issue[] = PI_TABLE(COMMAND_ISSUE);
static const uint32_t pi_address[]       = PI_TABLE(ADDRESS);
static const uint32_t pi_baddress[]      = PI_TABLE(BADDRESS);
static const uint32_t pi_wrdata[]        = PI_TABLE(WRDATA);
static const uint32_t pi_rddata[]        = PI_TABLE(RDDATA);

static void command(int phase, uint32_t cmd, uint32_t address, uint32_t bank)
{
    csr_write(pi_address[phase],  address);
    csr_write(pi_baddress[phase], bank);
    csr_write(pi_command[phase],  cmd);
    csr_write(pi_command_issue[phase], 1);
}

static void software_control(void)
{
    csr_write(CSR_SDRAM_DFII_CONTROL, DFII_CONTROL_CKE | DFII_CONTROL_ODT | DFII_CONTROL_RESET_N);
}

static void hardware_control(void)
{
    csr_write(CSR_SDRAM_DFII_CONTROL, DFII_CONTROL_SEL);
}

static void dfii_init(void)
{
    for (unsigned i = 0; i < sizeof(sdram_init_steps)/sizeof(sdram_init_steps[0]); i++) {
        const struct sdram_step *s = &sdram_init_steps[i];
        switch (s->kind) {
        case SDRAM_STEP_DELAY:
            delay_us(s->value + 1000);
            break;
        case SDRAM_STEP_CONTROL:
            csr_write(CSR_SDRAM_DFII_CONTROL, s->value);
            break;
        case SDRAM_STEP_ADDRESS:
            csr_write(pi_address[s->phase], s->value);
            break;
        case SDRAM_STEP_BADDRESS:
            csr_write(pi_baddress[s->phase], s->value);
            break;
        case SDRAM_STEP_COMMAND:
            csr_write(pi_command[s->phase], s->value);
            csr_write(pi_command_issue[s->phase], 1);
            break;
        }
    }
}

/* One BL8 burst write + read back at column `col` of row 0, bank 0. */
static void write_read(const uint32_t *pattern, uint32_t *data, uint32_t col)
{
    command(0, DFII_COMMAND_RAS | DFII_COMMAND_CS, 0, 0);                        /* Activate.  */
    for (int n = 0; n < SDRAM_PHASES; n++)
        csr_write(pi_wrdata[n], pattern[n]);
    command(SDRAM_WRPHASE, DFII_COMMAND_CAS | DFII_COMMAND_WE | DFII_COMMAND_CS |
        DFII_COMMAND_WRDATA, col, 0);                                            /* Write.     */
    command(SDRAM_RDPHASE, DFII_COMMAND_CAS | DFII_COMMAND_CS | DFII_COMMAND_RDDATA, col, 0);
    for (int n = 0; n < SDRAM_PHASES; n++)
        data[n] = csr_read(pi_rddata[n]);
    command(0, DFII_COMMAND_RAS | DFII_COMMAND_WE | DFII_COMMAND_CS, 0x400, 0);    /* Precharge. */
}

static uint32_t rng_state = 0x12345678;

static uint32_t rng(void)
{
    /* xorshift32. */
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 17;
    rng_state ^= rng_state << 5;
    return rng_state;
}

/* Byte lanes of `module` in a DFI phase word. */
static uint32_t module_mask(int module)
{
    uint32_t mask = 0;
    for (int b = 0; b < SDRAM_DFI_DATABITS/SDRAM_DATABITS; b++)
        mask |= 0xffUL << (b*SDRAM_DATABITS + 8*module);
    return mask;
}

/* Bitmap of the modules passing SDRAM_TRIES random patterns. */
static uint32_t modules_ok(void)
{
    uint32_t ok = (1 << SDRAM_MODULES) - 1;
    for (int t = 0; t < SDRAM_TRIES; t++) {
        uint32_t pattern[SDRAM_PHASES], data[SDRAM_PHASES];
        for (int n = 0; n < SDRAM_PHASES; n++)
            pattern[n] = rng();
        write_read(pattern, data, 8*t);
        for (int m = 0; m < SDRAM_MODULES; m++)
            for (int n = 0; n < SDRAM_PHASES; n++)
                if ((pattern[n] ^ data[n]) & module_mask(m))
                    ok &= ~(1 << m);
    }
    return ok;
}

/* ECP5 Read Leveling ---------------------------------------------------------------------------- */

/* Delay/bitslip actions, one module selected at a time and only around the action (as the BIOS:
 * with several modules selected together, or a module left selected, the DFII accesses still
 * worked but the controller path failed on hardware). */
static void set_bitslip(uint32_t modules, int bitslip)
{
    for (int m = 0; m < SDRAM_MODULES; m++) {
        if (!(modules & (1 << m)))
            continue;
        csr_write(CSR_DDRPHY_DLY_SEL, 1 << m);
        csr_write(CSR_DDRPHY_RDLY_DQ_BITSLIP_RST, 1);
        for (int i = 0; i < bitslip; i++)
            csr_write(CSR_DDRPHY_RDLY_DQ_BITSLIP, 1);
        csr_write(CSR_DDRPHY_DLY_SEL, 0);
    }
}

static void set_delay(uint32_t modules, int delay)
{
    for (int m = 0; m < SDRAM_MODULES; m++) {
        if (!(modules & (1 << m)))
            continue;
        csr_write(CSR_DDRPHY_DLY_SEL, 1 << m);
        csr_write(CSR_DDRPHY_RDLY_DQ_RST, 1);
        for (int i = 0; i < delay; i++)
            csr_write(CSR_DDRPHY_RDLY_DQ_INC, 1);
        csr_write(CSR_DDRPHY_DLY_SEL, 0);
    }
}

#define ALL_MODULES ((1 << SDRAM_MODULES) - 1)

/* DFII write/read cycles at mid delays (resynchronize the DQSBUF read path). */
static void warmup(void)
{
    uint32_t pattern[SDRAM_PHASES], data[SDRAM_PHASES];
    software_control();
    set_bitslip(ALL_MODULES, 0);
    set_delay(ALL_MODULES, SDRAM_DELAYS/2 - 1);
    for (int i = 0; i < 16; i++) {
        for (int n = 0; n < SDRAM_PHASES; n++)
            pattern[n] = i*0x01010101;
        write_read(pattern, data, 8*(i % 4));
    }
    hardware_control();
}

/* Per module (the other modules left at their current settings, as `dram.py`/the BIOS: scanning
 * all the modules together through the bad delays found the same settings but left the controller
 * read path failing on hardware): bitslip x delay scan, best bitslip and center of the longest
 * passing window, then all the modules re-applied. Returns 0 when every module has a window. */
static int read_leveling(void)
{
    int ret = 0;
    software_control();
    for (int i = 0; i < 4; i++)
        sdram_status.scan[i] = 0;
    for (int m = 0; m < SDRAM_MODULES; m++) {
        sdram_status.window[m] = 0;
        for (int bitslip = 0; bitslip < SDRAM_BITSLIPS; bitslip++) {
            int cur = -1;
            set_bitslip(1 << m, bitslip);
            for (int delay = 0; delay <= SDRAM_DELAYS; delay++) {
                int ok = 0;
                if (delay < SDRAM_DELAYS) {
                    set_delay(1 << m, delay);
                    ok = (modules_ok() >> m) & 1;
                    if (bitslip < 4)
                        sdram_status.scan[bitslip] |= ok << (SDRAM_MODULES*delay + m);
                }
                if (ok && cur < 0)
                    cur = delay;
                if (!ok && cur >= 0) {
                    int len = delay - cur;
                    if (len > sdram_status.window[m]) {
                        sdram_status.window[m]  = len;
                        sdram_status.bitslip[m] = bitslip;
                        sdram_status.delay[m]   = cur + len/2;
                    }
                    cur = -1;
                }
            }
        }
        if (!sdram_status.window[m]) {
            ret = -1;
            break;
        }
        set_bitslip(1 << m, sdram_status.bitslip[m]);
        set_delay(1 << m, sdram_status.delay[m]);
    }
    /* Re-apply all the modules (a module's setting was lost while the next was scanned). */
    for (int m = 0; m < SDRAM_MODULES && !ret; m++) {
        set_bitslip(1 << m, sdram_status.bitslip[m]);
        set_delay(1 << m, sdram_status.delay[m]);
    }
    hardware_control();
    return ret;
}

/* BIST ------------------------------------------------------------------------------------------ */

#ifdef CSR_SDRAM_CHECKER_ERRORS
#define BIST(name, reg) CSR_SDRAM_##name##_##reg
#define BIST_RUN(name) do {                                                \
    csr_write(BIST(name, RESET),  1);                                      \
    csr_write(BIST(name, BASE),   0);                                      \
    csr_write(BIST(name, END),    SDRAM_BIST_SIZE);                        \
    csr_write(BIST(name, LENGTH), SDRAM_BIST_SIZE);                        \
    csr_write(BIST(name, RANDOM), 1);                                      \
    csr_write(BIST(name, START),  1);                                      \
    for (int i = 0; i < 1000 && !csr_read(BIST(name, DONE)); i++)          \
        delay_us(1000);                                                    \
} while (0)

/* 1MB random write then check through the controller: errors (0xffffffff: timeout). */
static uint32_t bist_check(void)
{
    BIST_RUN(GENERATOR);
    BIST_RUN(CHECKER);
    if (!csr_read(CSR_SDRAM_CHECKER_DONE))
        return 0xffffffff;
    return csr_read(CSR_SDRAM_CHECKER_ERRORS);
}
#else
static uint32_t bist_check(void)
{
    return 0; /* No BIST in the bitstream: DFII leveling only. */
}
#endif

/* Init ------------------------------------------------------------------------------------------ */

static uint32_t debug_flags; /* bit0: skip init, bit1: skip warmup, bit2: skip leveling, bit3: skip BIST. */

static int try_rate(uint32_t sel, uint32_t pair)
{
#ifdef CSR_DDRPHY_RATE
    csr_write(CSR_DDRPHY_RATE, sel | (pair << 1));
#endif
    if (!(debug_flags & 1))
        dfii_init();
    if (!(debug_flags & 2))
        warmup();
    if (!(debug_flags & 4) && read_leveling())
        return -1;
    if (debug_flags & 8)
        return 0;
    /* Read word alignment: `pair` (needed by the DFII reads) then one sys cycle later. */
    for (uint32_t shift = pair; shift < 4; shift += 2) {
#ifdef CSR_DDRPHY_RATE
        csr_write(CSR_DDRPHY_RATE, sel | (shift << 1));
#endif
        sdram_status.rate        = sel | (shift << 1);
        sdram_status.bist_errors = bist_check();
        if (sdram_status.bist_errors == 0)
            return 0;
    }
    return -1;
}

int sdram_init_rate(uint32_t rate)
{
    sdram_status.state       = SDRAM_STATE_RUNNING;
    sdram_status.attempts    = 1;
    sdram_status.bist_errors = 0xffffffff;
    sdram_status.csr_errors  = 0;
    debug_flags = (rate >> 2) & 0xf;
    sdram_status.state = try_rate(rate & 1, (rate >> 1) & 1) ? SDRAM_STATE_FAILED : SDRAM_STATE_OK;
    debug_flags = 0;
    return sdram_status.state == SDRAM_STATE_OK ? 0 : -1;
}

int sdram_init(void)
{
    sdram_status.state       = SDRAM_STATE_RUNNING;
    sdram_status.modules     = SDRAM_MODULES;
    sdram_status.bist_errors = 0xffffffff;
    sdram_status.csr_errors  = 0;
    for (int attempt = 0; attempt < SDRAM_ATTEMPTS; attempt++) {
        sdram_status.attempts = attempt + 1;
#ifdef CSR_MAIN_CRG_PHASE
        if (attempt) {
            /* New CLKDIVF division phases: DDR PHY init sequence replay (resets sys, the CSR
             * bridge included: the clear write may be lost, the reset clears the field). */
            csr_write(CSR_MAIN_CRG_PHASE, 4);
            csr_write(CSR_MAIN_CRG_PHASE, 0);
            delay_us(10000);
        }
#endif
#ifdef CSR_DDRPHY_RATE
        for (uint32_t i = 0; i < 4; i++) {
            if (try_rate(i >> 1, i & 1) == 0) {
                sdram_status.state = SDRAM_STATE_OK;
                return 0;
            }
        }
#else
        if (try_rate(0, 0) == 0) {
            sdram_status.state = SDRAM_STATE_OK;
            return 0;
        }
#endif
    }
    sdram_status.state = SDRAM_STATE_FAILED;
    return -1;
}

#else

int sdram_init(void)
{
    sdram_status.state = SDRAM_STATE_NONE;
    return -1;
}

int sdram_init_rate(uint32_t rate)
{
    (void)rate;
    return sdram_init();
}

#endif
