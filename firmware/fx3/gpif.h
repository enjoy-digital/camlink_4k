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

#define GPIF_CLK_DIV_X2    8   /* PIB clock = SYS_CLK*2/8 = 96MHz (100.8MHz with FX3_PLL_FBDIV=21). */
#define GPIF_FLAG_OMEGA    16  /* EMPTY_FULL_TH0 (active low in the FPGA). */
#define GPIF_AFLAG_OMEGA   17  /* EMPTY_FULL_TH1 (audio, active low in the FPGA). */
#define GPIF_AUDIO_BUF_SIZE  192 /* 1ms of 48kHz stereo 16-bit. */
#define GPIF_AUDIO_BUF_COUNT 4

void gpif_stream_start(int video, int audio);
void gpif_stream_stop(void);
void gpif_stream_status(uint32_t *status);

#endif /* GPIF_H */
