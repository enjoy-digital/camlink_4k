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
/* Video: pattern or HDMI (DDR, 2x downscale or crop window at (crop_x, crop_y), Cb/Cr swap). */
void fpga_stream_start(uint16_t width, uint16_t height, uint32_t fps, int hdmi, int ddr, int downscale,
    int c_swap, int crop, uint16_t crop_x, uint16_t crop_y, int no_signal);
void fpga_stream_stop(void);
/* GPIF streamer: video (thread 0) / audio (thread 1) enables (both off: GPIF logic in reset). */
void fpga_gpif_control(int video, int audio);
/* Audio source: enable, test counter instead of I2S. */
void fpga_audio_control(int enable, int test);

#endif /* FPGA_CTRL_H */
