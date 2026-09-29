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
/* Video source configuration. */
struct fpga_video {
    uint16_t width, height;   /* UVC frame size.                                          */
    uint32_t fps;             /* Pattern frame rate.                                      */
    uint8_t  hdmi;            /* HDMI input (else test pattern).                          */
    uint8_t  ddr;             /* IT6802 0.5x PCLK DDR output.                             */
    uint8_t  downscale;       /* 2x2 box downscale.                                       */
    uint8_t  crop;            /* Crop window (crop_x, crop_y, in_width x in_height).       */
    uint16_t crop_x, crop_y;
    uint8_t  c_swap;          /* Cb/Cr swap.                                              */
    uint8_t  m420;            /* M420 output (YUV 4:2:0), else YUY2.                      */
    uint8_t  no_signal;       /* Pattern: "no signal" mode.                               */
    uint8_t  canvas;          /* Window centered in the frame (black borders).            */
    uint8_t  rgb;             /* RGB 4:4:4 input converted by the FPGA CSC (BT.709).      */
    uint8_t  full_range;      /* RGB input full range (0-255), else limited (16-235).     */
    uint16_t in_width, in_height; /* Window size (crop/canvas), 0: frame size.            */
};

void fpga_stream_start(const struct fpga_video *v);
void fpga_stream_stop(void);
/* GPIF streamer: video (thread 0) / audio (thread 1) enables (both off: GPIF logic in reset). */
void fpga_payload_config(void);
uint32_t fpga_hdmi_frame_period(void); /* 100MHz cycles. */
void fpga_watchdog_init(void);
void fpga_watchdog_service(void);
void fpga_watchdog_config(uint32_t period_ms); /* 0: off. */
void fpga_gpif_control(int video, int audio, int audio_batch);
/* Audio source: enable, test counter instead of I2S. */
void fpga_audio_control(int enable, int test);

#endif /* FPGA_CTRL_H */
