/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef FPGA_H
#define FPGA_H

#include <stdint.h>

#define FPGA_STATUS_DONE (1UL <<  8)
#define FPGA_STATUS_BUSY (1UL << 12)
#define FPGA_STATUS_FAIL (1UL << 13)

void     fpga_init(void);
uint32_t fpga_read_idcode(void);
uint32_t fpga_read_status(void);
void     fpga_config_start(void);
void     fpga_config_data(const uint8_t *data, uint32_t length);
uint32_t fpga_config_finish(void);

#endif /* FPGA_H */
