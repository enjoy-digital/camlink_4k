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

void fpga_stream_start(uint16_t width, uint16_t height, uint32_t fps, int hdmi)
{
    fpga_stream_stop();
    fpga_csr_write(CSR_MAIN_SOURCE_SEL,       hdmi ? 3 : 1); /* UVC HDMI / UVC pattern. */
    fpga_csr_write(CSR_HDMI_IN_CONTROL,       hdmi ? (1 | (1 << 4) | (0 << 6)) : 0); /* Y: lane 1, C: lane 0. */
    fpga_csr_write(CSR_PATTERN_HWORDS,        width/2);
    fpga_csr_write(CSR_PATTERN_VRES,          height);
    fpga_csr_write(CSR_PATTERN_BAR_WORDS,     width/16);
    fpga_csr_write(CSR_PATTERN_FRAME_PERIOD,  UVC_CLOCK_FREQ/fps);
    fpga_csr_write(CSR_UVC_PAYLOAD_WORDS,     (UVC_PAYLOAD_SIZE - 12)/4);
    fpga_csr_write(CSR_UVC_FRAME_WORDS,       (uint32_t)width*height/2);
    fpga_csr_write(CSR_GPIF_CONTROL,          (4 << 8) | 0x3); /* Head lead 4, FLAG inverted, enable. */
    fpga_csr_write(CSR_PATTERN_ENABLE,        !hdmi);
}

void fpga_stream_stop(void)
{
    fpga_csr_write(CSR_PATTERN_ENABLE,  0);
    fpga_csr_write(CSR_HDMI_IN_CONTROL, 0);
    fpga_csr_write(CSR_GPIF_CONTROL,    0);
}
