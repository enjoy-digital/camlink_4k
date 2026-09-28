/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef I2C_H
#define I2C_H

#include <stdint.h>

void i2c_init(uint32_t bitrate);
int  i2c_write(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
               const uint8_t *data, uint16_t len);
int  i2c_read(uint8_t addr, const uint8_t *prefix, uint8_t prefix_len,
              uint8_t *data, uint16_t len);

#endif /* I2C_H */
