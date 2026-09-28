/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef FPGA_CTRL_H
#define FPGA_CTRL_H

#include <stdint.h>

int  fpga_csr_write(uint32_t addr, uint32_t value);
int  fpga_csr_read(uint32_t addr, uint32_t *value);
void fpga_stream_start(uint16_t width, uint16_t height, uint32_t fps);
void fpga_stream_stop(void);

#endif /* FPGA_CTRL_H */
