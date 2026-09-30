/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef SPI_FLASH_H
#define SPI_FLASH_H

#include <stdint.h>

#define SPI_FLASH_SIZE        0x400000
#define SPI_FLASH_BLOCK_SIZE  0x10000
#define SPI_FLASH_PAGE_SIZE   256

/* Flash layout: FX3 boot image at 0 (boot ROM), CamLinX bitstream in a region unused by the
 * stock firmware (stock: FX3 image 0x000000-0x02ffff, bitstream 0x040000-0x09ffff, settings
 * 0x3f0000) so the stock bitstream stays usable with a RAM-loaded stock FX3 image. */
#define FLASH_FX3_IMAGE       0x000000
#define FLASH_BITSTREAM_HDR   0x100000
#define FLASH_BITSTREAM       0x100100

void     spi_flash_init(void);
uint32_t spi_flash_read_id(void);
void     spi_flash_read(uint32_t addr, uint8_t *data, uint32_t len);
int      spi_flash_program_page(uint32_t addr, const uint8_t *data, uint16_t len);
int      spi_flash_erase_block(uint32_t addr);
int      spi_flash_busy(void);

#endif /* SPI_FLASH_H */
