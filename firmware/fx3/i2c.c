/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * FX3 I2C master (register mode, polled). GPIO58 = SCL, GPIO59 = SDA.
 *
 * The FX3 I2C block sends a "preamble" (up to 8 bytes: device address, register bytes, and for
 * reads a repeated start + device address) and then a data phase of BYTE_COUNT bytes.
 */

#include "fx3.h"
#include "i2c.h"

#include <rdb/gctl.h>
#include <rdb/lpp.h>
#include <rdb/i2c.h>

#define I2C_TIMEOUT_US 20000

/* Init ------------------------------------------------------------------------------------------ */

void i2c_init(uint32_t bitrate)
{
    /* Low performance peripherals block. */
    if (!(reg_read(FX3_LPP_POWER) & FX3_LPP_POWER_ACTIVE)) {
        reg_clear(FX3_LPP_POWER, FX3_LPP_POWER_RESETN);
        delay_us(10);
        reg_set(FX3_LPP_POWER, FX3_LPP_POWER_RESETN);
        while (!(reg_read(FX3_LPP_POWER) & FX3_LPP_POWER_ACTIVE));
    }

    /* Core clock = 10 x bitrate, from SYS_CLK. */
    reg_write(FX3_GCTL_I2C_CORE_CLK,
        (3UL << FX3_GCTL_I2C_CORE_CLK_SRC_SHIFT) |
        (((FX3_SYS_CLK/(10*bitrate)) - 1) << FX3_GCTL_I2C_CORE_CLK_DIV_SHIFT));
    reg_set(FX3_GCTL_I2C_CORE_CLK, FX3_GCTL_I2C_CORE_CLK_CLK_EN);

    /* Reset/enable block. */
    reg_clear(FX3_I2C_POWER, FX3_I2C_POWER_RESETN);
    delay_us(10);
    reg_set(FX3_I2C_POWER, FX3_I2C_POWER_RESETN);
    while (!(reg_read(FX3_I2C_POWER) & FX3_I2C_POWER_ACTIVE));

    reg_write(FX3_I2C_CONFIG,
        FX3_I2C_CONFIG_TX_CLEAR | FX3_I2C_CONFIG_RX_CLEAR |
        ((bitrate <= 100000) ? FX3_I2C_CONFIG_I2C_100KHz : 0));
    delay_us(10);
    reg_write(FX3_I2C_CONFIG,
        FX3_I2C_CONFIG_ENABLE |
        ((bitrate <= 100000) ? FX3_I2C_CONFIG_I2C_100KHz : 0));
    reg_write(FX3_I2C_TIMEOUT, 0xffffffffUL);
    reg_write(FX3_I2C_INTR_MASK, 0);
}

/* Transfers ------------------------------------------------------------------------------------- */

static void i2c_preamble(const uint8_t *bytes, uint8_t len, uint32_t start_mask, uint32_t stop_mask)
{
    uint32_t words[2] = {0, 0};
    for (int i = 0; i < len; i++)
        words[i/4] |= (uint32_t)bytes[i] << (8*(i%4));
    reg_write(FX3_I2C_PREAMBLE_DATA + 0, words[0]);
    reg_write(FX3_I2C_PREAMBLE_DATA + 4, words[1]);
    reg_write(FX3_I2C_PREAMBLE_CTRL,
        (stop_mask  << FX3_I2C_PREAMBLE_CTRL_STOP_SHIFT) |
        (start_mask << FX3_I2C_PREAMBLE_CTRL_START_SHIFT));
    reg_write(FX3_I2C_PREAMBLE_RPT, 0);
}

#define I2C_INTR_ERRORS (FX3_I2C_INTR_ERROR | FX3_I2C_INTR_TIMEOUT | FX3_I2C_INTR_LOST_ARBITRATION)

/* Note: STATUS.ERROR is not a per-transfer flag, errors are checked on INTR (cleared per transfer). */
static int i2c_wait(uint32_t flag)
{
    uint32_t timeout = I2C_TIMEOUT_US;
    while (timeout--) {
        if (reg_read(FX3_I2C_INTR) & I2C_INTR_ERRORS)
            return -1;
        if (reg_read(FX3_I2C_STATUS) & flag)
            return 0;
        delay_us(1);
    }
    return -2;
}

static int i2c_finish(void)
{
    int ret = 0;
    uint32_t timeout = I2C_TIMEOUT_US;
    uint32_t status;

    /* Wait for the bus to go idle, check/clear errors. */
    while (timeout-- && (reg_read(FX3_I2C_STATUS) & FX3_I2C_STATUS_BUSY))
        delay_us(1);
    status = reg_read(FX3_I2C_STATUS);
    if (reg_read(FX3_I2C_INTR) & I2C_INTR_ERRORS)
        ret = -1;
    if (status & FX3_I2C_STATUS_BUSY)
        ret = -2;
    if (ret) {
        /* Recover: flush FIFOs and restart the block. */
        uint32_t config = reg_read(FX3_I2C_CONFIG);
        reg_write(FX3_I2C_CONFIG, (config & ~FX3_I2C_CONFIG_ENABLE) |
            FX3_I2C_CONFIG_TX_CLEAR | FX3_I2C_CONFIG_RX_CLEAR);
        delay_us(10);
        reg_write(FX3_I2C_CONFIG, config);
    }
    reg_write(FX3_I2C_INTR, ~0UL);
    return ret;
}

/* Transfers are atomic (main loop and USB interrupt share the bus). */
static int i2c_write_raw(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
                         const uint8_t *data, uint16_t len);
static int i2c_read_raw(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
                        uint8_t *data, uint16_t len);

int i2c_write(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
              const uint8_t *data, uint16_t len)
{
    uint32_t irq = irq_save();
    int ret = i2c_write_raw(addr, prefix, prefix_len, data, len);
    irq_restore(irq);
    return ret;
}

int i2c_read(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
             uint8_t *data, uint16_t len)
{
    uint32_t irq = irq_save();
    int ret = i2c_read_raw(addr, prefix, prefix_len, data, len);
    irq_restore(irq);
    return ret;
}

static int i2c_write_raw(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
                         const uint8_t *data, uint16_t len)
{
    uint8_t preamble[8];
    int ret = 0;

    if (prefix_len > 7)
        return -3;
    preamble[0] = addr << 1;
    for (int i = 0; i < prefix_len; i++)
        preamble[1 + i] = prefix[i];

    reg_write(FX3_I2C_INTR, ~0UL);
    i2c_preamble(preamble, 1 + prefix_len, 0, 0);
    reg_write(FX3_I2C_BYTE_COUNT, len);
    reg_write(FX3_I2C_COMMAND,
        FX3_I2C_COMMAND_START_FIRST | FX3_I2C_COMMAND_STOP_LAST |
        FX3_I2C_COMMAND_PREAMBLE_VALID |
        ((uint32_t)(1 + prefix_len) << FX3_I2C_COMMAND_PREAMBLE_LEN_SHIFT));

    for (int i = 0; i < len && !ret; i++) {
        ret = i2c_wait(FX3_I2C_STATUS_TX_SPACE);
        if (!ret)
            reg_write(FX3_I2C_EGRESS_DATA, data[i]);
    }
    if (!ret)
        ret = i2c_wait(FX3_I2C_STATUS_TX_DONE);
    return i2c_finish() ? -1 : ret;
}

static int i2c_read_raw(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
                        uint8_t *data, uint16_t len)
{
    uint8_t preamble[8];
    uint8_t n = 0;
    uint32_t start_mask = 0;
    int ret = 0;

    if (prefix_len > 6 || len == 0)
        return -3;

    /* [addr|W, prefix..., (Sr) addr|R] or [addr|R] without prefix. */
    if (prefix_len) {
        preamble[n++] = addr << 1;
        for (int i = 0; i < prefix_len; i++)
            preamble[n++] = prefix[i];
        start_mask = 1UL << (n - 1); /* Repeated start after the last prefix byte. */
    }
    preamble[n++] = (addr << 1) | 1;

    reg_write(FX3_I2C_INTR, ~0UL);
    i2c_preamble(preamble, n, start_mask, 0);
    reg_write(FX3_I2C_BYTE_COUNT, len);
    reg_write(FX3_I2C_COMMAND,
        FX3_I2C_COMMAND_READ |
        FX3_I2C_COMMAND_START_FIRST | FX3_I2C_COMMAND_STOP_LAST | FX3_I2C_COMMAND_NAK_LAST |
        FX3_I2C_COMMAND_PREAMBLE_VALID |
        ((uint32_t)n << FX3_I2C_COMMAND_PREAMBLE_LEN_SHIFT));

    for (int i = 0; i < len && !ret; i++) {
        ret = i2c_wait(FX3_I2C_STATUS_RX_DATA);
        if (!ret)
            data[i] = reg_read(FX3_I2C_INGRESS_DATA) & 0xff;
    }
    return i2c_finish() ? -1 : ret;
}
