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
#include "gpif.h"

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
    /* Address as data (a preamble-only write with no data phase does not complete). */
    int ret = i2c_write(FPGA_I2C_ADDR, 0, 0, a, 4);
    if (!ret)
        ret = i2c_read(FPGA_I2C_ADDR, 0, 0, d, 4);
    if (!ret)
        *value = ((uint32_t)d[0] << 24) | ((uint32_t)d[1] << 16) | ((uint32_t)d[2] << 8) | d[3];
    return ret;
}

/* Stream ---------------------------------------------------------------------------------------- */

/* BT.709 RGB -> YCbCr (limited range output) coefficients, Q1.10 (r, g, b per row), offsets
 * (litecamlink/gateware/csc.py: bt709_coefficients). */
static const int16_t csc_full[9]    = { 187,  629,   63, -103, -347,  450,  450, -409,  -41};
static const int16_t csc_limited[9] = { 218,  732,   74, -120, -404,  524,  524, -476,  -48};

static void fpga_csc_config(int full_range)
{
    static const uint32_t regs[9] = {
        CSR_HDMI_IN_CSC_Y_R,  CSR_HDMI_IN_CSC_Y_G,  CSR_HDMI_IN_CSC_Y_B,
        CSR_HDMI_IN_CSC_CB_R, CSR_HDMI_IN_CSC_CB_G, CSR_HDMI_IN_CSC_CB_B,
        CSR_HDMI_IN_CSC_CR_R, CSR_HDMI_IN_CSC_CR_G, CSR_HDMI_IN_CSC_CR_B,
    };
    const int16_t *k = full_range ? csc_full : csc_limited;
    for (int i = 0; i < 9; i++)
        fpga_csr_write(regs[i], (uint32_t)k[i] & 0xfff);
    /* Y offset 16, C offset 128, RGB input offset (limited range: 16). */
    fpga_csr_write(CSR_HDMI_IN_CSC_OFFSETS, (full_range ? 0UL : 16UL) << 16 | 128UL << 8 | 16UL);
}

void fpga_stream_start(const struct fpga_video *v)
{
    /* Pattern fallback of a M420 frame: YUY2 pattern words with the M420 frame size. */
    uint16_t pattern_lines = v->m420 ? (uint16_t)(v->height*3/4) : v->height;
    uint32_t frame_words   = (uint32_t)v->width*v->height*(v->m420 ? 12 : 16)/32;

    fpga_stream_stop();
    fpga_csr_write(CSR_HDMI_IN_CROP_X,        v->crop_x/2);
    fpga_csr_write(CSR_HDMI_IN_CROP_Y,        v->crop_y);
    fpga_csr_write(CSR_HDMI_IN_CROP_W,        (v->in_width  ? v->in_width  : v->width)/2);
    fpga_csr_write(CSR_HDMI_IN_CROP_H,         v->in_height ? v->in_height : v->height);
    fpga_csr_write(CSR_MAIN_SOURCE_SEL,       v->hdmi ? 3 : 1); /* UVC HDMI / UVC pattern. */
    fpga_csr_write(CSR_PATTERN_HWORDS,        v->width/2);
    fpga_csr_write(CSR_PATTERN_VRES,          pattern_lines);
    fpga_csr_write(CSR_PATTERN_BAR_WORDS,     v->width/16);
    fpga_csr_write(CSR_PATTERN_FRAME_PERIOD,  UVC_CLOCK_FREQ/v->fps);
    fpga_csr_write(CSR_PATTERN_MODE,          v->no_signal);
    fpga_csr_write(CSR_UVC_PAYLOAD_WORDS,     (UVC_PAYLOAD_SIZE - 12)/4);
    fpga_csr_write(CSR_UVC_FRAME_WORDS,       frame_words);
    fpga_csr_write(CSR_CANVAS_OUT_HWORDS,     v->width/2);
    fpga_csr_write(CSR_CANVAS_OUT_VRES,       v->height);
    fpga_csr_write(CSR_CANVAS_IN_HWORDS,      v->in_width/2);
    fpga_csr_write(CSR_CANVAS_IN_VRES,        v->in_height);
    fpga_csr_write(CSR_CANVAS_X0,             (v->width - v->in_width)/4);
    fpga_csr_write(CSR_CANVAS_Y0,             (v->height - v->in_height)/2);
    fpga_csr_write(CSR_CANVAS_ENABLE,         v->canvas);
    if (v->rgb)
        fpga_csc_config(v->full_range);
    /* Sources enabled last (the UVC packetizer leaves reset with its configuration set).
     * HDMI: enable, Y lane 1 (QE[23:16]), C lane 2 (QE[35:28]), DDR/downscale/crop/M420. The Cb/Cr
     * order depends on the IT6802 path: swapped with CSC bypass (YCbCr sources), not with the
     * RGB->YUV CSC (RGB sources) (validated with a MacBook YCbCr and a PC RGB source). */
    fpga_csr_write(CSR_HDMI_IN_CONTROL, v->hdmi ?
        (1UL | (1UL << 4) | (2UL << 6) | ((uint32_t)v->c_swap << 8) | ((uint32_t)v->ddr << 12) |
         ((uint32_t)v->downscale << 14) | ((uint32_t)v->crop << 15) | ((uint32_t)v->m420 << 16) |
         ((uint32_t)v->rgb << 17)) : 0);
    fpga_csr_write(CSR_PATTERN_ENABLE,        !v->hdmi);
}

void fpga_stream_stop(void)
{
    fpga_csr_write(CSR_PATTERN_ENABLE,  0);
    fpga_csr_write(CSR_HDMI_IN_CONTROL, 0);
}

/* Payload/burst = one FX3 DMA buffer (the FPGA resets assume 16KB buffers). */
void fpga_payload_config(void)
{
    fpga_csr_write(CSR_UVC_PAYLOAD_WORDS, (UVC_PAYLOAD_SIZE - 12)/4);
    fpga_csr_write(CSR_GPIF_BURST,        GPIF_DMA_BUF_SIZE/4);
}

void fpga_gpif_control(int video, int audio, int audio_batch)
{
    /* Burst = one FX3 DMA buffer. Head lead 4, FLAGs inverted (active low), audio lead 8, audio
     * packets per thread switch. */
    fpga_csr_write(CSR_GPIF_BURST, GPIF_DMA_BUF_SIZE/4);
    fpga_csr_write(CSR_GPIF_CONTROL, ((uint32_t)(audio_batch & 0xf) << 24) | (8UL << 20) |
        ((uint32_t)!!audio << 16) | (4 << 8) | 0x2 | !!video);
}

void fpga_audio_control(int enable, int test)
{
    fpga_csr_write(CSR_AUDIO_CONTROL, (!!test << 1) | !!enable);
}
