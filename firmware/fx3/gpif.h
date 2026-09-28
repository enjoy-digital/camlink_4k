/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef GPIF_H
#define GPIF_H

#include <stdint.h>

#define GPIF_DMA_BUF_SIZE  16384
#define GPIF_DMA_BUF_COUNT 8

void gpif_stream_start(uint16_t clk_div_x2, uint8_t flag_omega);
void gpif_stream_stop(void);
void gpif_stream_status(uint32_t *status);

#endif /* GPIF_H */
