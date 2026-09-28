/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * FPGA control through the FPGA I2C bridge (I2C slave 0x10, 32-bit big-endian address/data).
 */

#include "fx3.h"
#include "i2c.h"
#include "fpga_ctrl.h"
#include "uvc.h"

#include "generated/fpga_csr.h"

#define FPGA_I2C_ADDR 0x10

/* CSR Access ------------------------------------------------------------------------------------ */

int fpga_csr_write(uint32_t addr, uint32_t value)
{
    uint8_t a[4] = {addr >> 24, addr >> 16, addr >> 8, addr};
    uint8_t d[4] = {value >> 24, value >> 16, value >> 8, value};
    return i2c_write(FPGA_I2C_ADDR, a, 4, d, 4);
}

int fpga_csr_read(uint32_t addr, uint32_t *value)
{
    uint8_t a[4] = {addr >> 24, addr >> 16, addr >> 8, addr};
    uint8_t d[4];
    int ret = i2c_write(FPGA_I2C_ADDR, a, 4, 0, 0);
    if (!ret)
        ret = i2c_read(FPGA_I2C_ADDR, 0, 0, d, 4);
    if (!ret)
        *value = ((uint32_t)d[0] << 24) | ((uint32_t)d[1] << 16) | ((uint32_t)d[2] << 8) | d[3];
    return ret;
}

/* Stream ---------------------------------------------------------------------------------------- */

void fpga_stream_start(uint16_t width, uint16_t height, uint32_t fps, int hdmi, int ddr, int downscale,
    int c_swap, int crop, uint16_t crop_x, uint16_t crop_y, int no_signal)
{
    fpga_stream_stop();
    fpga_csr_write(CSR_HDMI_IN_CROP_X, crop_x/2);
    fpga_csr_write(CSR_HDMI_IN_CROP_Y, crop_y);
    fpga_csr_write(CSR_HDMI_IN_CROP_W, width/2);
    fpga_csr_write(CSR_HDMI_IN_CROP_H, height);
    fpga_csr_write(CSR_MAIN_SOURCE_SEL,       hdmi ? 3 : 1); /* UVC HDMI / UVC pattern. */
    /* HDMI: enable, Y lane 1 (QE[23:16]), C lane 2 (QE[35:28]), DDR/downscale. The Cb/Cr order
     * depends on the IT6802 path: swapped with CSC bypass (YCbCr sources), not with the RGB->YUV
     * CSC (RGB sources) (validated with a MacBook YCbCr and a PC RGB source). */
    fpga_csr_write(CSR_HDMI_IN_CONTROL, hdmi ?
        (1 | (1 << 4) | (2 << 6) | (c_swap << 8) | (ddr << 12) | (downscale << 14) | (crop << 15)) : 0);
    fpga_csr_write(CSR_PATTERN_HWORDS,        width/2);
    fpga_csr_write(CSR_PATTERN_VRES,          height);
    fpga_csr_write(CSR_PATTERN_BAR_WORDS,     width/16);
    fpga_csr_write(CSR_PATTERN_FRAME_PERIOD,  UVC_CLOCK_FREQ/fps);
    fpga_csr_write(CSR_UVC_PAYLOAD_WORDS,     (UVC_PAYLOAD_SIZE - 12)/4);
    fpga_csr_write(CSR_UVC_FRAME_WORDS,       (uint32_t)width*height/2);
    fpga_csr_write(CSR_PATTERN_MODE,          no_signal);
    fpga_csr_write(CSR_PATTERN_ENABLE,        !hdmi);
}

void fpga_stream_stop(void)
{
    fpga_csr_write(CSR_PATTERN_ENABLE,  0);
    fpga_csr_write(CSR_HDMI_IN_CONTROL, 0);
}

void fpga_gpif_control(int video, int audio)
{
    /* Head lead 4, FLAGs inverted (active low), audio lead 8. */
    fpga_csr_write(CSR_GPIF_CONTROL, (8UL << 20) | ((uint32_t)!!audio << 16) | (4 << 8) | 0x2 | !!video);
}

void fpga_audio_control(int enable, int test)
{
    fpga_csr_write(CSR_AUDIO_CONTROL, (!!test << 1) | !!enable);
}
