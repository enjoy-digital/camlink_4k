/*
 * This file is part of CamLinX.
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
 * (camlinx_4k/gateware/csc.py: bt709_coefficients). */
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
    /* UVC HDMI / UVC pattern (NV12: through the DRAM frame buffer). */
    fpga_csr_write(CSR_MAIN_SOURCE_SEL,       v->hdmi ? (v->nv12 ? 4 : 3) : (v->nv12 ? 5 : 1));
#ifdef CSR_FRAMEBUFFER_BASE
    if (v->nv12) {
        /* 3 slots from address 0, 128-bit port words (DDR3 x16 at 1:4). */
        uint32_t line_words = v->width/16;
        uint32_t slot_words = ((frame_words/4) + 0xfff) & ~0xfffUL;
        fpga_csr_write(CSR_FRAMEBUFFER_BASE,        0);
        fpga_csr_write(CSR_FRAMEBUFFER_SLOT_WORDS,  slot_words);
        fpga_csr_write(CSR_FRAMEBUFFER_LINE_WORDS,  line_words);
        fpga_csr_write(CSR_FRAMEBUFFER_HEIGHT,      v->height);
        fpga_csr_write(CSR_FRAMEBUFFER_UV_OFFSET,   (uint32_t)v->height*line_words);
        fpga_csr_write(CSR_FRAMEBUFFER_FRAME_WORDS, frame_words);
    }
    fpga_csr_write(CSR_FRAMEBUFFER_ENABLE, v->nv12);
#endif
    fpga_csr_write(CSR_PATTERN_HWORDS,        v->width/2);
    fpga_csr_write(CSR_PATTERN_VRES,          pattern_lines);
    fpga_csr_write(CSR_PATTERN_BAR_WORDS,     v->width/16);
    fpga_csr_write(CSR_PATTERN_FRAME_PERIOD,  UVC_CLOCK_FREQ/v->fps);
    fpga_csr_write(CSR_PATTERN_MODE,          v->no_signal);
#ifdef CSR_PATTERN_M420
    fpga_csr_write(CSR_PATTERN_M420,          v->m420); /* 4:2:0 layout (M420, NV12 through the frame buffer). */
#endif
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
     * HDMI: enable, Y lane 1 (QE[23:16]), C lane 2 (QE[35:28]), DDR/downscale/crop/M420, Cb/Cr
     * swap as configured (natural order for both IT6802 paths, see uvc.c). */
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
     * packets per thread switch, threads independent of the other thread's FLAG (xflag_off: the
     * audio ring is full in steady state, video gated on the audio FLAG starved; no audio word
     * corruption without the gating, validated on hardware). */
    fpga_csr_write(CSR_GPIF_BURST, GPIF_DMA_BUF_SIZE/4);
    /* Thread switch guard: 256 cycles (1024 overflowed the video FIFO with audio at 4K30 M420,
     * 64 still OK on hardware). */
    fpga_csr_write(CSR_GPIF_SWITCH_GUARD, 256);
    fpga_csr_write(CSR_GPIF_CONTROL, ((uint32_t)(audio_batch & 0xf) << 24) | (8UL << 20) |
        ((uint32_t)!!audio << 16) | (1UL << 13) | (4 << 8) | 0x2 | !!video);
}

void fpga_audio_control(int enable, int test)
{
    fpga_csr_write(CSR_AUDIO_CONTROL, (!!test << 1) | !!enable);
}

/* FX3 Watchdog (FPGA) --------------------------------------------------------------------------- */

/* The FPGA resets the FX3 (RESET#) when the heartbeat (GPIO45 toggled from the main loop) stops for
 * the watchdog period (4s FPGA default): recovers from any FX3 hang (the FX3 internal watchdog
 * does not reset the chip on the Cam Link, its interrupt mode does not preempt IRQ handlers). */
#define HEARTBEAT_GPIO 45

static int     heartbeat_on = 1;
static uint8_t heartbeat_level;

void fpga_watchdog_init(void)
{
    gpio_setup_output(HEARTBEAT_GPIO, 0);
}

void fpga_watchdog_service(void)
{
    static uint32_t calls;
    static uint8_t  dead;
    if (heartbeat_on && (++calls & 0xffff) == 0) { /* ~25 toggles/s. */
        /* USB dead for ~3s (e.g. host gave up on the device): heartbeat stopped, the FPGA resets
         * the FX3 (the main loop can be alive with a dead USB). */
        dead = usb_alive() ? 0 : (dead < 255 ? dead + 1 : dead);
        if (dead > 75)
            return;
        heartbeat_level ^= 1;
        gpio_set(HEARTBEAT_GPIO, heartbeat_level);
    }
}

/* Heartbeat edge now, from long operations blocking or starving the main loop (DRAM init over
 * the I2C bridge, flash requests handled in the USB interrupt): a main loop based heartbeat
 * stopped for more than the watchdog period there and the FPGA reset the FX3. */
void fpga_watchdog_kick(void)
{
    if (!heartbeat_on)
        return;
    heartbeat_level ^= 1;
    gpio_set(HEARTBEAT_GPIO, heartbeat_level);
}

void fpga_watchdog_config(uint32_t period_ms)
{
    if (period_ms) {
        fpga_csr_write(CSR_FX3_WATCHDOG_PERIOD, period_ms*(CSR_CONST_CONFIG_CLOCK_FREQUENCY/1000));
        fpga_csr_write(CSR_FX3_WATCHDOG_CONTROL, 1);
        heartbeat_on = 1;
    } else {
        /* Disabled before the heartbeat stops (intentional reboots). */
        fpga_csr_write(CSR_FX3_WATCHDOG_CONTROL, 0);
        heartbeat_on = 0;
    }
}

/* HDMI input frame period in 100MHz cycles (measured in video clock cycles; host tools use 100MHz
 * for all builds). */
uint32_t fpga_hdmi_frame_period(void)
{
    uint32_t period = 0;
    fpga_csr_read(CSR_HDMI_IN_FRAME_PERIOD, &period);
    return (uint32_t)((uint64_t)period*100000000ULL/VIDEO_CLOCK_FREQ);
}
