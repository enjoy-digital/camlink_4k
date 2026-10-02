/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef GPIF_H
#define GPIF_H

#include <stdint.h>

#ifndef GPIF_DMA_BUF_SIZE
#define GPIF_DMA_BUF_SIZE  32768 /* One UVC payload per buffer (host URB size with uvcvideo: 32KB halves
                                  * the URB rate vs 16KB, fewer drops under host load). */
#endif
#ifndef GPIF_DMA_BUF_COUNT
/* 320KB of video buffering (~0.9ms at the 4K30 M420 rate): absorbs host/xHCI scheduling stalls
 * (the FPGA only has a 8KB FIFO without DRAM). */
#define GPIF_DMA_BUF_COUNT (320*1024/GPIF_DMA_BUF_SIZE)
#endif

#define GPIF_CLK_DIV_X2    8   /* PIB clock = SYS_CLK*2/8 = 100.8MHz (96MHz with FX3_PLL_FBDIV=20). */
#define GPIF_FLAG_OMEGA    16  /* EMPTY_FULL_TH0 (active low in the FPGA). */
#define GPIF_AFLAG_OMEGA   17  /* EMPTY_FULL_TH1 (audio, active low in the FPGA). */
#define GPIF_AUDIO_BUF_SIZE  192 /* 1ms of 48kHz stereo 16-bit. */
#define GPIF_AUDIO_BUF_COUNT 8 /* Room for audio batches (FPGA audio_batch packets per switch). */

void gpif_stream_start(int video, int audio);
void gpif_stream_stop(void);
int  gpif_stream_running(void);
void gpif_thread_restart(int thread, int enable); /* 0: video, 1: audio. */
void gpif_stream_status(uint32_t *status);

#endif /* GPIF_H */
