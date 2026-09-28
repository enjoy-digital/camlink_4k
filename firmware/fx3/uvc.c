/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * UVC Video Streaming: probe/commit negotiation, stream start/stop, and UAC audio stream start/stop
 * (video and audio share the GPIF: any change restarts it with the active streams).
 *
 * The video payloads (headers included) are produced by the FPGA and streamed through the GPIF
 * auto DMA channel: the FX3 only negotiates the format and configures the FPGA (through the FPGA
 * I2C bridge) and the GPIF.
 */

#include <string.h>

#include "fx3.h"
#include "uvc.h"
#include "gpif.h"
#include "fpga_ctrl.h"
#include "it6802.h"

#include "generated/fpga_csr.h"

const struct uvc_frame uvc_frames[UVC_FRAME_COUNT] = {
    {1920, 1080},
    {1280,  720},
    { 640,  480},
};

static struct uvc_probe probe;
static struct uvc_probe commit;

enum { STREAM_IDLE = 0, STREAM_START, STREAM_STOP };
static volatile int stream_request;
static int streaming;

static volatile int      crop_mode;     /* 0: downscale, 1: crop (inputs larger than the frame). */
static volatile uint16_t crop_x, crop_y; /* Crop window position (pixels, lines). */
static volatile int      video_request; /* Video settings changed: restart if streaming. */
static uint32_t          input_generation;

static volatile int     audio_request; /* Pending audio alternate setting + 1 (0: none). */
static volatile uint8_t audio_alt;
static volatile int     audio_test;
static int              audio_on;

/* Debug counters (read with camlink.py peek). */
volatile uint32_t uvc_stats[4]; /* commits, halts, starts, stops. */

/* Probe Helpers --------------------------------------------------------------------------------- */

static void uvc_probe_fixup(struct uvc_probe *p)
{
    const struct uvc_frame *frame;
    uint32_t fps;

    p->bFormatIndex = 1;
    if (p->bFrameIndex < 1 || p->bFrameIndex > UVC_FRAME_COUNT)
        p->bFrameIndex = 1;
    frame = &uvc_frames[p->bFrameIndex - 1];

    /* Snap to a supported interval. */
    if (p->dwFrameInterval == 0 || p->dwFrameInterval >= UVC_INTERVAL(UVC_FPS_MIN))
        p->dwFrameInterval = UVC_INTERVAL(UVC_FPS_MIN);
    else
        p->dwFrameInterval = UVC_INTERVAL(UVC_FPS_MAX);
    fps = 10000000UL/p->dwFrameInterval;
    (void)fps;

    p->wKeyFrameRate            = 0;
    p->wPFrameRate              = 0;
    p->wCompQuality             = 0;
    p->wCompWindowSize          = 0;
    p->wDelay                   = 0;
    p->dwMaxVideoFrameSize      = (uint32_t)frame->width*frame->height*2;
    p->dwMaxPayloadTransferSize = UVC_PAYLOAD_SIZE;
    p->dwClockFrequency         = UVC_CLOCK_FREQ;
    p->bmFramingInfo            = 0x03; /* FID + EOF. */
    p->bPreferedVersion         = 1;
    p->bMinVersion              = 1;
    p->bMaxVersion              = 1;
}

static void uvc_probe_default(struct uvc_probe *p, int max)
{
    memset(p, 0, sizeof(*p));
    p->bFrameIndex     = 1;
    p->dwFrameInterval = UVC_INTERVAL(max ? UVC_FPS_MAX : UVC_FPS_MIN);
    uvc_probe_fixup(p);
}

/* Video Control Requests ------------------------------------------------------------------------ */

/* Processing Unit controls (applied by the FPGA ColorAdjust on the HDMI path). */
struct pu_control {
    uint8_t  selector;
    int16_t  min, max, def;
    volatile int16_t cur;
};

static struct pu_control pu_controls[] = {
    {UVC_PU_BRIGHTNESS_CONTROL, -64,  64,   0,   0},
    {UVC_PU_CONTRAST_CONTROL,     0, 255, 128, 128},
    {UVC_PU_SATURATION_CONTROL,   0, 255, 128, 128},
};

static volatile int color_request;

static void uvc_apply_color(void)
{
    fpga_csr_write(CSR_COLOR_BRIGHTNESS, (uint8_t)pu_controls[0].cur);
    fpga_csr_write(CSR_COLOR_CONTRAST,   (uint8_t)pu_controls[1].cur);
    fpga_csr_write(CSR_COLOR_SATURATION, (uint8_t)pu_controls[2].cur);
}

static void ep0_in16(uint8_t *buf, uint16_t v0, uint16_t v1, uint16_t len)
{
    buf[0] = v0; buf[1] = v0 >> 8;
    buf[2] = v1; buf[3] = v1 >> 8;
    usb_ep0_in(buf, len);
}

static int uvc_pu_request(const struct usb_setup *setup, uint8_t *buf)
{
    uint8_t selector = setup->value >> 8;
    uint16_t len     = setup->length < 2 ? setup->length : 2;
    struct pu_control *c = 0;

    for (unsigned i = 0; i < sizeof(pu_controls)/sizeof(pu_controls[0]); i++)
        if (pu_controls[i].selector == selector)
            c = &pu_controls[i];
    if (!c)
        return -1;
    switch (setup->request) {
    case UVC_SET_CUR: {
        int16_t v;
        if (usb_ep0_out(buf, 2) < 0)
            return 0;
        v = (int16_t)(buf[0] | (buf[1] << 8));
        c->cur = v < c->min ? c->min : v > c->max ? c->max : v;
        color_request = 1;
        return 0;
    }
    case UVC_GET_CUR: ep0_in16(buf, c->cur, 0, len); return 0;
    case UVC_GET_MIN: ep0_in16(buf, c->min, 0, len); return 0;
    case UVC_GET_MAX: ep0_in16(buf, c->max, 0, len); return 0;
    case UVC_GET_DEF: ep0_in16(buf, c->def, 0, len); return 0;
    case UVC_GET_RES: ep0_in16(buf, 1,      0, len); return 0;
    case UVC_GET_INFO:
        buf[0] = 0x03; /* GET/SET supported. */
        usb_ep0_in(buf, 1);
        return 0;
    }
    return -1;
}

static int uvc_xu_request(const struct usb_setup *setup, uint8_t *buf)
{
    uint8_t selector = setup->value >> 8;
    uint16_t size    = (selector == XU_INPUT_INFO_CONTROL) ? sizeof(struct it6802_status) : 4;
    uint16_t len     = setup->length < size ? setup->length : size;

    if (selector != XU_INPUT_INFO_CONTROL && selector != XU_CROP_CONTROL)
        return -1;
    switch (setup->request) {
    case UVC_GET_LEN:
        ep0_in16(buf, size, 0, setup->length < 2 ? setup->length : 2);
        return 0;
    case UVC_GET_INFO:
        buf[0] = (selector == XU_CROP_CONTROL) ? 0x03 : 0x01;
        usb_ep0_in(buf, 1);
        return 0;
    case UVC_GET_CUR:
        if (selector == XU_INPUT_INFO_CONTROL) {
            struct it6802_status *st = (struct it6802_status *)buf;
            memcpy(st, it6802_get_status(), sizeof(*st));
            fpga_csr_read(CSR_HDMI_IN_FRAME_PERIOD, &st->frame_period);
            usb_ep0_in(buf, len);
        } else {
            ep0_in16(buf, crop_mode ? crop_x : 0xffff, crop_y, len);
        }
        return 0;
    case UVC_GET_MIN:
    case UVC_GET_RES:
        memset(buf, 0, size);
        if (setup->request == UVC_GET_RES && selector == XU_CROP_CONTROL)
            buf[0] = buf[2] = 1;
        usb_ep0_in(buf, len);
        return 0;
    case UVC_GET_MAX:
    case UVC_GET_DEF:
        memset(buf, setup->request == UVC_GET_MAX ? 0xff : 0, size);
        if (setup->request == UVC_GET_DEF && selector == XU_CROP_CONTROL)
            buf[0] = buf[1] = 0xff;
        usb_ep0_in(buf, len);
        return 0;
    case UVC_SET_CUR:
        if (selector != XU_CROP_CONTROL)
            return -1;
        if (usb_ep0_out(buf, 4) < 0)
            return 0;
        {
            uint16_t x = buf[0] | (buf[1] << 8);
            uint16_t y = buf[2] | (buf[3] << 8);
            uvc_set_crop(x != 0xffff, x, y);
        }
        return 0;
    }
    return -1;
}

/* Class Requests -------------------------------------------------------------------------------- */

int uvc_class_request(const struct usb_setup *setup, uint8_t *buf)
{
    uint8_t intf     = setup->index & 0xff;
    uint8_t selector = setup->value >> 8;
    uint16_t len     = setup->length;
    struct uvc_probe *ctrl;

    /* Video Control interface: Processing/Extension Unit controls. */
    if (intf == UVC_INTF_CONTROL) {
        if ((setup->index >> 8) == UVC_ID_PROCESSING)
            return uvc_pu_request(setup, buf);
        if ((setup->index >> 8) == UVC_ID_EXTENSION)
            return uvc_xu_request(setup, buf);
        return -1;
    }
    if (intf != UVC_INTF_STREAMING)
        return -1;
    if (selector != UVC_VS_PROBE_CONTROL && selector != UVC_VS_COMMIT_CONTROL)
        return -1;
    ctrl = (selector == UVC_VS_PROBE_CONTROL) ? &probe : &commit;
    if (len > sizeof(struct uvc_probe))
        len = sizeof(struct uvc_probe);

    switch (setup->request) {
    case UVC_SET_CUR:
        memset(buf, 0, sizeof(struct uvc_probe));
        if (usb_ep0_out(buf, setup->length) < 0)
            return 0;
        memcpy(ctrl, buf, len);
        uvc_probe_fixup(ctrl);
        if (selector == UVC_VS_COMMIT_CONTROL) {
            stream_request = STREAM_START;
            uvc_stats[0]++;
        }
        return 0;
    case UVC_GET_CUR:
        memcpy(buf, ctrl, len);
        usb_ep0_in(buf, len);
        return 0;
    case UVC_GET_MIN:
    case UVC_GET_DEF:
    case UVC_GET_MAX: {
        struct uvc_probe p;
        uvc_probe_default(&p, setup->request == UVC_GET_MAX);
        memcpy(buf, &p, len);
        usb_ep0_in(buf, len);
        return 0;
    }
    case UVC_GET_LEN:
        buf[0] = sizeof(struct uvc_probe);
        buf[1] = 0;
        usb_ep0_in(buf, 2);
        return 0;
    case UVC_GET_INFO:
        buf[0] = 0x03; /* GET/SET supported. */
        usb_ep0_in(buf, 1);
        return 0;
    }
    return -1;
}

void uvc_bus_reset(void)
{
    stream_request = STREAM_STOP;
    audio_alt      = 0;
    audio_request  = 1; /* Alt 0. */
}

void uvc_stream_halt(void)
{
    stream_request = STREAM_STOP;
    uvc_stats[1]++;
}

/* Stream Control -------------------------------------------------------------------------------- */

static void video_start(void)
{
    const struct uvc_frame *frame = &uvc_frames[commit.bFrameIndex - 1];
    const struct it6802_status *hdmi = it6802_get_status();
    uint32_t fps = 10000000UL/commit.dwFrameInterval;
    /* HDMI input when stable and matching the requested frame size directly, 2x downscaled, or
     * cropped (crop mode, input larger than the frame), test pattern otherwise. */
    int direct   = hdmi->hactive == frame->width   && hdmi->vactive == frame->height;
    int half     = hdmi->hactive == 2*frame->width && hdmi->vactive == 2*frame->height;
    int crop     = !direct && crop_mode &&
                   hdmi->hactive >= frame->width && hdmi->vactive >= frame->height;
    int use_hdmi = hdmi->stable && (direct || half || crop);
    int ddr      = 1; /* IT6802 always in 0.5x PCLK DDR output mode (see it6802.c). */
    uint16_t x = 0, y = 0;

    if (use_hdmi && crop) {
        /* Clamp the window to the input (x on a 2-pixel boundary). */
        x = crop_x < hdmi->hactive - frame->width  ? crop_x : hdmi->hactive - frame->width;
        y = crop_y < hdmi->vactive - frame->height ? crop_y : hdmi->vactive - frame->height;
        x &= ~1;
    }
    fpga_stream_start(frame->width, frame->height, fps, use_hdmi, ddr,
        use_hdmi && half && !crop, hdmi->colorspace != 0, use_hdmi && crop, x, y, !hdmi->stable);
}

/* (Re)start the GPIF with the active streams: FPGA sources and GPIF logic off (reset), FX3 GPIF
 * restart, then FPGA sources and GPIF enables for the active streams. */
static void streams_apply(void)
{
    fpga_audio_control(0, 0);
    fpga_stream_stop();
    fpga_gpif_control(0, 0);
    gpif_stream_start(streaming, audio_on);
    if (streaming)
        video_start();
    if (audio_on)
        fpga_audio_control(1, audio_test);
    fpga_gpif_control(streaming, audio_on);
}

void uvc_audio_set_interface(uint8_t alt)
{
    audio_alt     = alt;
    audio_request = alt + 1;
}

uint8_t uvc_audio_get_interface(void)
{
    return audio_alt;
}

void uvc_set_crop(int enable, uint16_t x, uint16_t y)
{
    crop_mode     = enable;
    crop_x        = x;
    crop_y        = y;
    video_request = 1;
}

void uvc_audio_set_test(int test)
{
    audio_test    = test;
    audio_request = audio_alt + 1;
}

void uvc_service(void)
{
    int request;

    int audio;

    /* Take the pending requests atomically (set from the USB interrupt). */
    int video;

    if (color_request) {
        color_request = 0;
        uvc_apply_color();
    }
    /* Settled input change (mode, color space, loss): re-evaluate the video source. */
    if (it6802_get_status()->generation != input_generation) {
        input_generation = it6802_get_status()->generation;
        if (streaming)
            video_request = 1;
    }
    if (stream_request == STREAM_IDLE && !audio_request && !video_request)
        return;
    irq_disable();
    request        = stream_request;
    stream_request = STREAM_IDLE;
    audio          = audio_request;
    audio_request  = 0;
    video          = video_request;
    video_request  = 0;
    irq_enable();

    if (video && request == STREAM_IDLE && !audio) {
        /* Settings change: only applied immediately while streaming. */
        if (streaming)
            streams_apply();
        return;
    }

    {
        int was_streaming = streaming;
        int was_audio_on  = audio_on;

        if (request == STREAM_START) {
            uvc_stats[2]++;
            streaming = 1;
        }
        if (request == STREAM_STOP) {
            uvc_stats[3]++;
            streaming = 0;
        }
        if (audio)
            audio_on = (audio - 1) != 0;
        /* Video (re)start requests always apply (new format); otherwise only on changes. */
        if (request != STREAM_START && !video &&
            streaming == was_streaming && audio_on == was_audio_on)
            return;
    }
    streams_apply();
}

/* Init ------------------------------------------------------------------------------------------ */

void uvc_init(void)
{
    uvc_probe_default(&probe, 0);
    uvc_probe_default(&commit, 0);
}
