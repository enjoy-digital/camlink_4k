/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * ECP5 configuration over Slave-SPI (sysCONFIG), bit-banged on FX3 GPIOs.
 *
 * Cam Link 4K wiring: GPIO50 -> SN, GPIO51 -> CCLK, GPIO52 -> MOSI (D0), GPIO57 <- MISO (D1).
 * Sequence (Lattice TN1260, as used by ktemkin/camlink-re): REFRESH, ISC_ENABLE, ISC_ERASE,
 * LSC_INIT_ADDRESS, LSC_BITSTREAM_BURST + bitstream, ISC_DISABLE.
 */

#include "fx3.h"
#include "fpga.h"

#include <rdb/gpio.h>

enum {
    GPIO_SN   = 50,
    GPIO_CCLK = 51,
    GPIO_MOSI = 52,
    GPIO_MISO = 57,
};

enum {
    OP_READ_ID         = 0xe0,
    OP_LSC_READ_STATUS = 0x3c,
    OP_LSC_REFRESH     = 0x79,
    OP_ISC_ENABLE      = 0xc6,
    OP_ISC_ERASE       = 0x0e,
    OP_LSC_INIT_ADDR   = 0x46,
    OP_LSC_BURST       = 0x7a,
    OP_ISC_DISABLE     = 0x26,
};

/* Bit-Bang SPI (Mode 0, MSB first) -------------------------------------------------------------- */

#define GPIO_REG(n) (FX3_GPIO_SIMPLE + ((n) << 2))
#define GPIO_OUT    (FX3_GPIO_SIMPLE_ENABLE | FX3_GPIO_SIMPLE_DRIVE_HI_EN | FX3_GPIO_SIMPLE_DRIVE_LO_EN)

static inline void pin_write(uint8_t num, int value)
{
    reg_write(GPIO_REG(num), GPIO_OUT | (value ? FX3_GPIO_SIMPLE_OUT_VALUE : 0));
}

static uint8_t spi_xfer(uint8_t tx)
{
    uint8_t rx = 0;
    for (int i = 0; i < 8; i++) {
        pin_write(GPIO_MOSI, tx & 0x80);
        tx <<= 1;
        pin_write(GPIO_CCLK, 1);
        rx = (rx << 1) | ((reg_read(GPIO_REG(GPIO_MISO)) & FX3_GPIO_SIMPLE_IN_VALUE) ? 1 : 0);
        pin_write(GPIO_CCLK, 0);
    }
    return rx;
}

static void spi_cs(int active)
{
    pin_write(GPIO_CCLK, 0);
    pin_write(GPIO_SN, !active);
}

/* sysCONFIG Commands ---------------------------------------------------------------------------- */

static uint32_t fpga_command(uint8_t op, int response)
{
    uint32_t value = 0;
    spi_cs(1);
    spi_xfer(op);
    spi_xfer(0x00);
    spi_xfer(0x00);
    spi_xfer(0x00);
    if (response)
        for (int i = 0; i < 4; i++)
            value = (value << 8) | spi_xfer(0x00);
    spi_cs(0);
    return value;
}

static void fpga_wait_busy(void)
{
    uint32_t timeout = 100000;
    while (timeout--) {
        if (!(fpga_command(OP_LSC_READ_STATUS, 1) & FPGA_STATUS_BUSY))
            return;
        delay_us(10);
    }
}

/* Public ---------------------------------------------------------------------------------------- */

void fpga_init(void)
{
    gpio_setup_output(GPIO_SN,   1);
    gpio_setup_output(GPIO_CCLK, 0);
    gpio_setup_output(GPIO_MOSI, 0);
    gpio_setup_input(GPIO_MISO);
}

uint32_t fpga_read_idcode(void)
{
    return fpga_command(OP_READ_ID, 1);
}

uint32_t fpga_read_status(void)
{
    return fpga_command(OP_LSC_READ_STATUS, 1);
}

void fpga_config_start(void)
{
    fpga_command(OP_LSC_REFRESH, 0);
    delay_us(50000);
    fpga_command(OP_ISC_ENABLE, 0);
    fpga_wait_busy();
    fpga_command(OP_ISC_ERASE, 0);
    fpga_wait_busy();
    fpga_command(OP_LSC_INIT_ADDR, 0);

    /* Keep SN low: the bitstream follows the burst command. */
    spi_cs(1);
    spi_xfer(OP_LSC_BURST);
    spi_xfer(0x00);
    spi_xfer(0x00);
    spi_xfer(0x00);
}

void fpga_config_data(const uint8_t *data, uint32_t length)
{
    for (uint32_t i = 0; i < length; i++)
        spi_xfer(data[i]);
}

uint32_t fpga_config_finish(void)
{
    uint32_t status;
    spi_cs(0);
    status = fpga_read_status();
    fpga_command(OP_ISC_DISABLE, 0);
    /* Extra clocks to complete the wake-up sequence. */
    for (int i = 0; i < 16; i++)
        spi_xfer(0x00);
    return status;
}
