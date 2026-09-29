/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FPGA -> FX3 streaming over GPIF-II (32-bit, FX3 is the clock master).
 *
 * Interface:
 * - PCLK (GPIO16) : GPIF clock, driven by the FX3.
 * - DQ[31:0]      : Data, driven by the FPGA.
 * - CTL0 (GPIO17) : VALID, driven by the FPGA: a word is sampled/pushed on each VALID clock.
 * - CTL1 (GPIO18) : FLAG, driven by the FX3: thread 0 DMA flag (selectable omega signal).
 * - CTL2 (GPIO19) : EOP, driven by the FPGA: commit the current (partial) DMA buffer.
 * - CTL3 (GPIO20) : ASEL, driven by the FPGA: audio (thread 1) selected.
 * - CTL4 (GPIO21) : AFLAG, driven by the FX3: thread 1 DMA flag.
 *
 * Waveform: START -> IDLE -> DATA_A <-> DATA_B -> IDLE (thread 0). Alphas (SAMPLE_DIN) only apply
 * on state entry, so DATA_A/DATA_B alternate on every VALID clock to sample and push each word.
 * IDLE -> DECIDE (on EOP or ASEL):
 * - DECIDE -> COMMIT (EOP) -> EOP_WAIT (until EOP low) -> IDLE commits short buffers.
 * - DECIDE -> AIDLE (ASEL) -> ADATA_A <-> ADATA_B -> AIDLE -> IDLE (ASEL low): audio words on
 *   thread 1 (full 192-byte buffers, no commit needed).
 * DMA: PIB socket 0 -> ring of buffers -> UIB socket 1 (EP1 IN), PIB socket 1 -> UIB socket 2
 * (EP2 IN, audio), no CPU intervention.
 * GPIF waveform descriptor format from fx3lafw (bsp/gpif.h).
 */

#include "fx3.h"
#include "gpif.h"
#include "usb_desc.h"

#include <rdb/gctl.h>
#include <rdb/gpif.h>
#include <rdb/pib.h>
#include <rdb/dma.h>

/* Waveform Descriptors -------------------------------------------------------------------------- */

#define GPIF_STATE(n, fa, fb, fc, fd, f0, f1, al, ar, b, rep, bd) {                                  \
    ((n) & 0xff) | (((fa) & 0x1f) << 8) | (((fb) & 0x1f) << 13) | (((fc) & 0x1f) << 18) |          \
        (((fd) & 0x1f) << 23) | (((f0) & 0xf) << 28),                                               \
    (((f0) & 0x10) >> 4) | (((f1) & 0x1f) << 1) | (((al) & 0xff) << 6) | (((ar) & 0xff) << 14) |    \
        (((b) & 0x3ff) << 22),                                                                      \
    (((b) & 0xfffffc00UL) >> 10) | (((rep) & 0xff) << 22) | (((bd) & 1) << 30) | (1UL << 31)}

#define ALPHA_SAMPLE_DIN (1UL << 2)
#define BETA_THREAD_0    (0UL << 4)
#define BETA_THREAD_1    (1UL << 4)
#define BETA_WQ_PUSH     (1UL << 7)
#define BETA_COMMIT      (1UL << 30)

#define LAMBDA_CTL0      0
#define LAMBDA_CTL2      2
#define LAMBDA_CTL3      3

enum {
    STATE_START    = 0,
    STATE_IDLE     = 1,
    STATE_DATA_A   = 2,
    STATE_DATA_B   = 3,
    STATE_COMMIT   = 4,
    STATE_EOP_WAIT = 5,
    STATE_DECIDE   = 6,
    STATE_AIDLE    = 7,
    STATE_ADATA_A  = 8,
    STATE_ADATA_B  = 9,
};

/* Functions of (Fa, Fb, Fc, Fd): bit (Fa | Fb << 1 | Fc << 2 | Fd << 3) of the truth table. */
enum {
    FUNC_ZERO = 0, FUNC_FA, FUNC_NFA, FUNC_FB, FUNC_NFB, FUNC_ONE,
    FUNC_FA_NFC, FUNC_FB_OR_FC, FUNC_FC, FUNC_NFC, FUNC_NFA_NFC,
};

static const uint16_t functions[] = {
    [FUNC_ZERO]     = 0x0000, /* Constant 0.  */
    [FUNC_FA]       = 0xaaaa, /* Fa.          */
    [FUNC_NFA]      = 0x5555, /* !Fa.         */
    [FUNC_FB]       = 0xcccc, /* Fb.          */
    [FUNC_NFB]      = 0x3333, /* !Fb.         */
    [FUNC_ONE]      = 0xffff, /* Constant 1.  */
    [FUNC_FA_NFC]   = 0x0a0a, /* Fa & !Fc.    */
    [FUNC_FB_OR_FC] = 0xfcfc, /* Fb | Fc.     */
    [FUNC_FC]       = 0xf0f0, /* Fc.          */
    [FUNC_NFC]      = 0x0f0f, /* !Fc.         */
    [FUNC_NFA_NFC]  = 0x0505, /* !Fa & !Fc.   */
};

/* IDLE: left -> DATA_A on VALID & !ASEL, right -> DECIDE on EOP | ASEL. Samples DQ on entry: the
 * first push into a new DMA buffer writes the input register (not the bus), stale (0 after reset)
 * at stream start otherwise (first UVC header word lost, validated on hardware with a forced DQ). */
static const uint32_t state_idle[3] = GPIF_STATE(STATE_IDLE, LAMBDA_CTL0, LAMBDA_CTL2, LAMBDA_CTL3, 0,
    FUNC_FA_NFC, FUNC_FB_OR_FC, ALPHA_SAMPLE_DIN, ALPHA_SAMPLE_DIN, BETA_THREAD_0, 0, 0);

/* DECIDE: left -> AIDLE on ASEL, right -> COMMIT otherwise (EOP). */
static const uint32_t state_decide[3] = GPIF_STATE(STATE_DECIDE, LAMBDA_CTL0, LAMBDA_CTL2, LAMBDA_CTL3, 0,
    FUNC_FC, FUNC_NFC, 0, 0, BETA_THREAD_0, 0, 0);

/* AIDLE (thread 1): left -> ADATA_A on VALID, right -> IDLE on !VALID & !ASEL. */
static const uint32_t state_aidle[3] = GPIF_STATE(STATE_AIDLE, LAMBDA_CTL0, LAMBDA_CTL2, LAMBDA_CTL3, 0,
    FUNC_FA, FUNC_NFA_NFC, 0, 0, BETA_THREAD_1, 0, 0);

/* ADATA_A/ADATA_B (thread 1): sample + push, left -> AIDLE when !VALID, right -> other on VALID. */
static const uint32_t state_adata_a[3] = GPIF_STATE(STATE_ADATA_A, LAMBDA_CTL0, 0, 0, 0,
    FUNC_NFA, FUNC_FA, ALPHA_SAMPLE_DIN, ALPHA_SAMPLE_DIN, BETA_THREAD_1 | BETA_WQ_PUSH, 0, 0);
static const uint32_t state_adata_b[3] = GPIF_STATE(STATE_ADATA_B, LAMBDA_CTL0, 0, 0, 0,
    FUNC_NFA, FUNC_FA, ALPHA_SAMPLE_DIN, ALPHA_SAMPLE_DIN, BETA_THREAD_1 | BETA_WQ_PUSH, 0, 0);

/* COMMIT: commit the current buffer, then wait for EOP low. */
static const uint32_t state_commit[3] = GPIF_STATE(STATE_COMMIT, LAMBDA_CTL0, LAMBDA_CTL2, 0, 0,
    FUNC_ONE, FUNC_ZERO, 0, 0, BETA_THREAD_0 | BETA_COMMIT, 0, 0);
static const uint32_t state_eop_wait[3] = GPIF_STATE(STATE_EOP_WAIT, LAMBDA_CTL0, LAMBDA_CTL2, 0, 0,
    FUNC_NFB, FUNC_ZERO, 0, 0, BETA_THREAD_0, 0, 0);

/* DATA_A/DATA_B: sample + push, left -> IDLE when !VALID, right -> other DATA state when VALID. */
static const uint32_t state_data_a[3] = GPIF_STATE(STATE_DATA_A, LAMBDA_CTL0, 0, 0, 0,
    FUNC_NFA, FUNC_FA, ALPHA_SAMPLE_DIN, ALPHA_SAMPLE_DIN, BETA_THREAD_0 | BETA_WQ_PUSH, 0, 0);
static const uint32_t state_data_b[3] = GPIF_STATE(STATE_DATA_B, LAMBDA_CTL0, 0, 0, 0,
    FUNC_NFA, FUNC_FA, ALPHA_SAMPLE_DIN, ALPHA_SAMPLE_DIN, BETA_THREAD_0 | BETA_WQ_PUSH, 0, 0);

static void gpif_write_transition(uint8_t state, const uint32_t *left, const uint32_t *right)
{
    for (int i = 0; i < 3; i++) {
        reg_write(FX3_GPIF_LEFT_WAVEFORM  + state*16 + 4*i, left  ? left[i]  : 0);
        reg_write(FX3_GPIF_RIGHT_WAVEFORM + state*16 + 4*i, right ? right[i] : 0);
    }
}

/* PIB Clock ------------------------------------------------------------------------------------- */

static void pib_start(uint16_t clk_div_x2)
{
    reg_write(FX3_GCTL_PIB_CORE_CLK,
        ((uint32_t)((clk_div_x2 >> 1) - 1) << FX3_GCTL_PIB_CORE_CLK_DIV_SHIFT) |
        (3UL << FX3_GCTL_PIB_CORE_CLK_SRC_SHIFT) |
        ((clk_div_x2 & 1) ? FX3_GCTL_PIB_CORE_CLK_HALFDIV : 0));
    reg_set(FX3_GCTL_PIB_CORE_CLK, FX3_GCTL_PIB_CORE_CLK_CLK_EN);

    reg_write(FX3_PIB_POWER, 0);
    delay_us(10);
    reg_set(FX3_PIB_POWER, FX3_PIB_POWER_RESETN);
    for (uint32_t timeout = 10000; timeout && !(reg_read(FX3_PIB_POWER) & FX3_PIB_POWER_ACTIVE); timeout--)
        delay_us(1);

    /* DLL disabled (default phases): with the DLL enabled, the PCLK output is unstable. */
    reg_write(FX3_PIB_DLL_CTRL, 0xf8f0);

    reg_write(FX3_PIB_INTR, ~0UL);
    reg_write(FX3_PIB_INTR_MASK, 0);
}

static void pib_stop(void)
{
    reg_write(FX3_PIB_INTR_MASK, 0);
    reg_write(FX3_PIB_INTR, ~0UL);
    reg_write(FX3_PIB_POWER, 0);
    delay_us(10);
    reg_clear(FX3_GCTL_PIB_CORE_CLK, FX3_GCTL_PIB_CORE_CLK_CLK_EN);
}

/* DMA ------------------------------------------------------------------------------------------- */

static uint8_t  dma_buf[GPIF_DMA_BUF_COUNT][GPIF_DMA_BUF_SIZE] __attribute__((aligned(32)));
static uint16_t dma_desc[GPIF_DMA_BUF_COUNT];
static uint8_t  audio_buf[GPIF_AUDIO_BUF_COUNT][GPIF_AUDIO_BUF_SIZE] __attribute__((aligned(32)));
static uint16_t audio_desc[GPIF_AUDIO_BUF_COUNT];

static void dma_setup_ring(uint16_t *desc, uint8_t *buf, int count, uint32_t size,
    uint32_t producer, uint32_t consumer)
{
    if (!desc[0])
        for (int i = 0; i < count; i++)
            desc[i] = dma_alloc_descriptor();
    for (int i = 0; i < count; i++) {
        uint16_t next = desc[(i + 1) % count];
        dma_fill_through(desc[i], producer, consumer, buf + i*size, size, next, next);
    }
    dma_start_consumer(consumer, desc[0]);
    dma_start_producer(producer, desc[0]);
}

/* Stream ---------------------------------------------------------------------------------------- */

static int gpif_running;

void gpif_stream_stop(void)
{
    /* Pause/disable the GPIF, abort sockets, flush the endpoints. */
    if (!gpif_running)
        return;
    reg_set(FX3_GPIF_WAVEFORM_CTRL_STAT, FX3_GPIF_WAVEFORM_CTRL_STAT_PAUSE);
    delay_us(10);
    reg_write(FX3_GPIF_WAVEFORM_CTRL_STAT, 0);
    reg_write(FX3_GPIF_CONFIG, 0);
    dma_abort_socket(DMA_PIB_SCK(0));
    dma_abort_socket(DMA_UIB_SCK(USB_DESC_EP_STREAM));
    usb_flush_in_ep(USB_DESC_EP_STREAM);
    dma_abort_socket(DMA_PIB_SCK(1));
    dma_abort_socket(DMA_UIB_SCK(USB_DESC_EP_AUDIO));
    usb_flush_in_ep(USB_DESC_EP_AUDIO);
    pib_stop();
    gpif_running = 0;
}

void gpif_stream_start(int video, int audio)
{
    gpif_stream_stop();
    if (!video && !audio)
        return;

    /* Give the GPIF pins back to the GPIF (remove simple GPIO overrides on GPIO0-49). */
    reg_write(FX3_GCTL_GPIO_SIMPLE + 0, 0);
    reg_clear(FX3_GCTL_GPIO_SIMPLE + 4, 0x3ffffUL);

    pib_start(GPIF_CLK_DIV_X2);

    /* Waveform: invalidate all, then program transitions. */
    reg_write(FX3_GPIF_CONFIG, 0x220);
    for (int i = 0; i < 256; i++) {
        reg_write(FX3_GPIF_LEFT_WAVEFORM  + i*16 + 8, 0);
        reg_write(FX3_GPIF_RIGHT_WAVEFORM + i*16 + 8, 0);
    }
    gpif_write_transition(STATE_START,    state_idle,    0);
    gpif_write_transition(STATE_IDLE,     state_data_a,  state_decide);
    gpif_write_transition(STATE_DECIDE,   state_aidle,   state_commit);
    gpif_write_transition(STATE_COMMIT,   state_eop_wait, 0);
    gpif_write_transition(STATE_EOP_WAIT, state_idle,    0);
    gpif_write_transition(STATE_DATA_A,   state_idle,    state_data_b);
    gpif_write_transition(STATE_DATA_B,   state_idle,    state_data_a);
    gpif_write_transition(STATE_AIDLE,    state_adata_a, state_idle);
    gpif_write_transition(STATE_ADATA_A,  state_aidle,   state_adata_b);
    gpif_write_transition(STATE_ADATA_B,  state_aidle,   state_adata_a);
    for (unsigned i = 0; i < sizeof(functions)/sizeof(functions[0]); i++)
        reg_write(FX3_GPIF_FUNCTION + 4*i, functions[i]);

    /* Bus: 32-bit, DQ inputs, CTL1 output (FLAG), others inputs. */
    reg_write(FX3_GPIF_BUS_CONFIG, 3UL << FX3_GPIF_BUS_CONFIG_BUS_WIDTH_SHIFT);
    reg_write(FX3_GPIF_BUS_CONFIG2, 0);
    reg_write(FX3_GPIF_AD_CONFIG,
        (1UL << FX3_GPIF_AD_CONFIG_A_OEN_CFG_SHIFT) |
        (1UL << FX3_GPIF_AD_CONFIG_DQ_OEN_CFG_SHIFT));
    reg_write(FX3_GPIF_CTRL_BUS_DIRECTION, (1UL << (2*1)) | (1UL << (2*4))); /* CTL1/CTL4 outputs. */
    reg_write(FX3_GPIF_CTRL_BUS_DEFAULT,   0);
    reg_write(FX3_GPIF_CTRL_BUS_POLARITY,  0);
    reg_write(FX3_GPIF_CTRL_BUS_TOGGLE,    0);
    for (int i = 0; i < 16; i++)
        reg_write(FX3_GPIF_CTRL_BUS_SELECT + 4*i,
            (i == 1) ? GPIF_FLAG_OMEGA : (i == 4) ? GPIF_AFLAG_OMEGA : 31);
    for (int i = 0; i < 2; i++)
        reg_write(FX3_GPIF_THREAD_CONFIG + 4*i,
            FX3_GPIF_THREAD_CONFIG_ENABLE |
            (16UL << FX3_GPIF_THREAD_CONFIG_WATERMARK_SHIFT) |
            (4UL  << FX3_GPIF_THREAD_CONFIG_BURST_SIZE_SHIFT) |
            ((uint32_t)i << FX3_GPIF_THREAD_CONFIG_THREAD_SOCK_SHIFT));
    reg_write(FX3_GPIF_INTR, ~0UL);
    reg_write(FX3_GPIF_INTR_MASK, 0);
    reg_write(FX3_GPIF_CONFIG,
        FX3_GPIF_CONFIG_ENABLE          |
        FX3_GPIF_CONFIG_THREAD_IN_STATE |
        (1UL << 9)                      | /* Undocumented, set in GPIF II Designer configs. */
        FX3_GPIF_CONFIG_SYNC            |
        FX3_GPIF_CONFIG_DOUT_POP_EN     |
        FX3_GPIF_CONFIG_CLK_OUT         |
        FX3_GPIF_CONFIG_CLK_SOURCE);

    /* Start the waveform at START. */
    reg_set(FX3_GPIF_WAVEFORM_CTRL_STAT, FX3_GPIF_WAVEFORM_CTRL_STAT_WAVEFORM_VALID);
    reg_write(FX3_GPIF_WAVEFORM_SWITCH,
        (reg_read(FX3_GPIF_WAVEFORM_SWITCH) &
         ~(FX3_GPIF_WAVEFORM_SWITCH_DESTINATION_STATE_MASK |
           FX3_GPIF_WAVEFORM_SWITCH_TERMINAL_STATE_MASK    |
           FX3_GPIF_WAVEFORM_SWITCH_SWITCH_NOW             |
           FX3_GPIF_WAVEFORM_SWITCH_WAVEFORM_SWITCH)) |
        ((uint32_t)STATE_START << FX3_GPIF_WAVEFORM_SWITCH_DESTINATION_STATE_SHIFT));
    reg_set(FX3_GPIF_WAVEFORM_SWITCH,
        FX3_GPIF_WAVEFORM_SWITCH_SWITCH_NOW | FX3_GPIF_WAVEFORM_SWITCH_WAVEFORM_SWITCH);

    /* DMA, once PCLK runs (the FPGA streaming logic is held in reset, VALID low): the FX3 captures
     * DQ as the first word of the first buffer when the producer starts, the FPGA DQ register
     * (clocked by PCLK) must already present the first UVC header word. */
    delay_us(10);
    if (video)
        dma_setup_ring(dma_desc, &dma_buf[0][0], GPIF_DMA_BUF_COUNT, GPIF_DMA_BUF_SIZE,
            DMA_PIB_SCK(0), DMA_UIB_SCK(USB_DESC_EP_STREAM));
    if (audio)
        dma_setup_ring(audio_desc, &audio_buf[0][0], GPIF_AUDIO_BUF_COUNT, GPIF_AUDIO_BUF_SIZE,
            DMA_PIB_SCK(1), DMA_UIB_SCK(USB_DESC_EP_AUDIO));
    gpif_running = 1;
}

void gpif_stream_status(uint32_t *status)
{
    status[0] = reg_read(FX3_GPIF_WAVEFORM_CTRL_STAT);
    status[1] = reg_read(FX3_GPIF_LAMBDA_STAT);
    status[2] = reg_read(FX3_PIB_INTR);
    status[3] = reg_read(FX3_PIB_ERROR);
    status[4] = reg_read(DMA_PIB_SCK(0) + FX3_SCK_STATUS);
    status[5] = reg_read(DMA_PIB_SCK(0) + FX3_SCK_DSCR);
    status[6] = reg_read(DMA_UIB_SCK(USB_DESC_EP_STREAM) + FX3_SCK_STATUS);
    status[7] = reg_read(DMA_UIB_SCK(USB_DESC_EP_STREAM) + FX3_SCK_DSCR);
}
