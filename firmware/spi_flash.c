/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * SPI flash (Winbond W25Q32, 4MB) access, bit-banged on the FX3 SPI pins (the FX3 SPI block is not
 * available in the 32-bit GPIF IO matrix configuration).
 *
 * Cam Link 4K wiring: GPIO53 -> CLK, GPIO54 -> CS#, GPIO55 <- DO, GPIO56 -> DI.
 */

#include "fx3.h"
#include "spi_flash.h"

#include <rdb/gpio.h>

enum {
    GPIO_CLK  = 53,
    GPIO_CS   = 54,
    GPIO_MISO = 55,
    GPIO_MOSI = 56,
};

enum {
    CMD_WRITE_ENABLE = 0x06,
    CMD_READ_STATUS  = 0x05,
    CMD_READ_DATA    = 0x03,
    CMD_PAGE_PROGRAM = 0x02,
    CMD_BLOCK_ERASE  = 0xd8,
    CMD_JEDEC_ID     = 0x9f,
};

#define STATUS_BUSY (1 << 0)

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
        pin_write(GPIO_CLK, 1);
        rx = (rx << 1) | ((reg_read(GPIO_REG(GPIO_MISO)) & FX3_GPIO_SIMPLE_IN_VALUE) ? 1 : 0);
        pin_write(GPIO_CLK, 0);
    }
    return rx;
}

static void spi_cs(int active)
{
    pin_write(GPIO_CLK, 0);
    pin_write(GPIO_CS, !active);
}

static void spi_cmd_addr(uint8_t cmd, uint32_t addr)
{
    spi_xfer(cmd);
    spi_xfer(addr >> 16);
    spi_xfer(addr >>  8);
    spi_xfer(addr >>  0);
}

/* Flash ----------------------------------------------------------------------------------------- */

void spi_flash_init(void)
{
    gpio_setup_output(GPIO_CS,   1);
    gpio_setup_output(GPIO_CLK,  0);
    gpio_setup_output(GPIO_MOSI, 0);
    gpio_setup_input(GPIO_MISO);
}

uint32_t spi_flash_read_id(void)
{
    uint32_t id;
    spi_cs(1);
    spi_xfer(CMD_JEDEC_ID);
    id  = (uint32_t)spi_xfer(0) << 16;
    id |= (uint32_t)spi_xfer(0) <<  8;
    id |= (uint32_t)spi_xfer(0) <<  0;
    spi_cs(0);
    return id;
}

void spi_flash_read(uint32_t addr, uint8_t *data, uint32_t len)
{
    spi_cs(1);
    spi_cmd_addr(CMD_READ_DATA, addr);
    for (uint32_t i = 0; i < len; i++)
        data[i] = spi_xfer(0);
    spi_cs(0);
}

int spi_flash_busy(void)
{
    uint8_t status;
    spi_cs(1);
    spi_xfer(CMD_READ_STATUS);
    status = spi_xfer(0);
    spi_cs(0);
    return status & STATUS_BUSY;
}

static int spi_flash_wait(uint32_t timeout_us)
{
    while (spi_flash_busy()) {
        if (timeout_us < 10)
            return -1;
        delay_us(10);
        timeout_us -= 10;
    }
    return 0;
}

static void spi_flash_write_enable(void)
{
    spi_cs(1);
    spi_xfer(CMD_WRITE_ENABLE);
    spi_cs(0);
}

int spi_flash_program_page(uint32_t addr, const uint8_t *data, uint16_t len)
{
    if (len == 0 || len > SPI_FLASH_PAGE_SIZE || ((addr & 0xff) + len) > SPI_FLASH_PAGE_SIZE)
        return -2;
    spi_flash_write_enable();
    spi_cs(1);
    spi_cmd_addr(CMD_PAGE_PROGRAM, addr);
    for (uint16_t i = 0; i < len; i++)
        spi_xfer(data[i]);
    spi_cs(0);
    return spi_flash_wait(10000);
}

int spi_flash_erase_block(uint32_t addr)
{
    spi_flash_write_enable();
    spi_cs(1);
    spi_cmd_addr(CMD_BLOCK_ERASE, addr & ~(SPI_FLASH_BLOCK_SIZE - 1));
    spi_cs(0);
    return spi_flash_wait(3000000);
}
