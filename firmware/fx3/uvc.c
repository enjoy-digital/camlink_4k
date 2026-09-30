/*
 * This file is part of CamLinX.
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
#include "sdram.h"

#include "generated/fpga_csr.h"

static const struct uvc_frame yuy2_frames[UVC_YUY2_FRAME_COUNT] = {
    {1920, 1080, 60},
    {1280,  720, 60},
    { 640,  480, 60},
};

static const struct uvc_frame m420_frames[UVC_M420_FRAME_COUNT] = {
    {3840, 2160, 30},
    {1920, 1080, 60},
};

const struct uvc_format uvc_formats[UVC_FORMAT_COUNT] = {
    {yuy2_frames, UVC_YUY2_FRAME_COUNT, 16, 0, 0},
    {m420_frames, UVC_M420_FRAME_COUNT, 12, 1, 0},
#ifdef UVC_FORMAT_NV12
    {m420_frames, UVC_NV12_FRAME_COUNT, 12, 1, 1}, /* Same frames as M420. */
#endif
};

static const struct uvc_frame *uvc_frame(const struct uvc_probe *p)
{
    return &uvc_formats[p->bFormatIndex - 1].frames[p->bFrameIndex - 1];
}

static struct uvc_probe probe;
static struct uvc_probe commit;

enum { STREAM_IDLE = 0, STREAM_START, STREAM_STOP };
static volatile int stream_request;
static int streaming;

static volatile int      range_override; /* 0: auto (AVI), 1: limited, 2: full. */
static volatile int      crop_mode;     /* 0: downscale, 1: crop (inputs larger than the frame). */
static volatile uint16_t crop_x, crop_y; /* Crop window position (pixels, lines). */
static volatile int      settings_request; /* Settings changed: re-apply if a stream is active. */
static uint32_t          input_generation;

static volatile int     audio_request; /* Pending audio alternate setting + 1 (0: none). */
static volatile uint8_t audio_alt;
static volatile int     audio_test;
static volatile int     audio_batch = 1;
static int              audio_on;

/* Debug counters (read with camlink.py peek). */
volatile uint32_t uvc_stats[4]; /* commits, halts, starts, stops. */

/* Probe Helpers --------------------------------------------------------------------------------- */

static void uvc_probe_fixup(struct uvc_probe *p)
{
    const struct uvc_format *format;
    const struct uvc_frame *frame;

    if (p->bFormatIndex < 1 || p->bFormatIndex > UVC_FORMAT_COUNT)
        p->bFormatIndex = 1;
    format = &uvc_formats[p->bFormatIndex - 1];
    if (p->bFrameIndex < 1 || p->bFrameIndex > format->count)
        p->bFrameIndex = 1;
    frame = uvc_frame(p);

    /* Snap to a supported interval. */
    if (p->dwFrameInterval == 0 || p->dwFrameInterval >= UVC_INTERVAL(UVC_FPS_MIN) ||
        frame->fps_max < UVC_FPS_MAX)
        p->dwFrameInterval = UVC_INTERVAL(UVC_FPS_MIN);
    else
        p->dwFrameInterval = UVC_INTERVAL(UVC_FPS_MAX);

    p->wKeyFrameRate            = 0;
    p->wPFrameRate              = 0;
    p->wCompQuality             = 0;
    p->wCompWindowSize          = 0;
    p->wDelay                   = 0;
    p->dwMaxVideoFrameSize      = (uint32_t)frame->width*frame->height*format->bpp/8;
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
    p->bFormatIndex    = 1;
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
        if (setup->length != 2)
            return -1;
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
            st->frame_period = fpga_hdmi_frame_period();
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
        if (selector != XU_CROP_CONTROL || setup->length != 4)
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
        /* UVC 1.1 probe/commit is 34 bytes (up to 48 for UVC 1.5 hosts, extra fields ignored). */
        if (setup->length > 48)
            return -1;
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
    const struct uvc_frame *frame = uvc_frame(&commit);
    const struct it6802_status *hdmi = it6802_get_status();
    uint16_t in_w = hdmi->hactive;
    uint16_t in_h = hdmi->vactive;
    int m420   = uvc_formats[commit.bFormatIndex - 1].m420;
    int nv12   = uvc_formats[commit.bFormatIndex - 1].nv12;
    int signal = hdmi->stable && in_w && in_h;
    int direct = in_w == frame->width   && in_h == frame->height;
    int half   = in_w == 2*frame->width && in_h == 2*frame->height;
    struct fpga_video v = {
        .width     = frame->width,
        .height    = frame->height,
        .fps       = 10000000UL/commit.dwFrameInterval,
        .ddr       = 1, /* IT6802 always in 0.5x PCLK DDR output mode (see it6802.c). */
        .c_swap    = 0, /* Natural Cb/Cr order for RGB (FPGA CSC) and YCbCr (CSC bypass) sources:
                         * validated with a MacBook YCbCr 4:4:4 source (a swap exchanges red/blue). */
        /* RGB sources: FPGA CSC. Range: AVI Q when explicit, else full (PC sources with a default
         * Q send full range, e.g. NVIDIA), unless overridden (uvc_set_range). */
        .rgb        = hdmi->colorspace == 0,
        .full_range = range_override ? (range_override == 2) : (hdmi->quant_range != 1),
        .m420      = m420,
        .no_signal = !signal,
        .nv12      = nv12 && sdram_ok(), /* NV12: HDMI or pattern through the DRAM frame buffer. */
    };

    /* The HDMI input (when stable) always fits the requested frame:
     * - direct (same size, YUY2 or M420),
     * - 2x2 downscale (2x input, YUY2, crop mode off),
     * - window (YUY2): the input clipped to the frame size, cropped from the input when larger
     *   (at the crop mode position, else centered) and centered in black borders when smaller
     *   (per axis: e.g. 1600x1200 -> centered 1600x1080 region, pillarboxed in 1920x1080).
     * M420 inputs of another size, NV12 without a working DRAM: test pattern (NV12 pattern
     * layout needs the frame buffer: garbled without DRAM). */
    if (signal && direct && (!nv12 || v.nv12)) {
        v.hdmi = 1;
    } else if (signal && !m420 && half && !crop_mode) {
        v.hdmi      = 1;
        v.downscale = 1;
    } else if (signal && !m420) {
        uint16_t win_w = (in_w < frame->width  ? in_w : frame->width) & ~3; /* Even word count. */
        uint16_t win_h =  in_h < frame->height ? in_h : frame->height;
        uint16_t max_x = in_w - win_w;
        uint16_t max_y = in_h - win_h;
        v.hdmi      = 1;
        v.in_width  = win_w;
        v.in_height = win_h;
        v.crop      = (win_w != in_w) || (win_h != in_h);
        v.crop_x    = (crop_mode ? (crop_x < max_x ? crop_x : max_x) : max_x/2) & ~1;
        v.crop_y    =  crop_mode ? (crop_y < max_y ? crop_y : max_y) : max_y/2;
        v.canvas    = (win_w < frame->width) || (win_h < frame->height);
    }
    fpga_stream_start(&v);
}

/* Audio packets per GPIF thread switch: 4K video needs ~all the GPIF bandwidth (each thread switch
 * costs 2 x switch_guard idle cycles, see software/throughput_model.py): batch >= 4 (+3ms audio
 * latency). */
static int streams_audio_batch(void)
{
    if (streaming && uvc_frame(&commit)->width >= 3840 && audio_batch < 4)
        return 4;
    return audio_batch;
}

/* Apply the stream states. GPIF not running (or no stream left): full (re)start, FPGA sources and
 * GPIF logic off (reset), FX3 GPIF restart, then FPGA GPIF enables and sources for the active
 * streams (GPIF first: a disabled FPGA GPIF drains its input, the canvas top border produced before
 * the GPIF enable was lost). GPIF running: only the changed stream's FPGA thread and FX3 DMA/endpoint
 * are restarted, the other stream keeps running (a full restart reset the other endpoint under the
 * host: video stalled after one frame when audio started, audio lost when video started). */
static void streams_apply(int video_changed, int audio_changed)
{
    if (!gpif_stream_running() || (!streaming && !audio_on)) {
        fpga_audio_control(0, 0);
        fpga_stream_stop();
        fpga_gpif_control(0, 0, audio_batch);
        gpif_stream_start(streaming, audio_on);
        fpga_gpif_control(streaming, audio_on, streams_audio_batch());
        if (streaming)
            video_start();
        if (audio_on)
            fpga_audio_control(1, audio_test);
        return;
    }
    if (video_changed) {
        /* FPGA video thread off (drained) before its FX3 DMA is restarted. */
        fpga_stream_stop();
        fpga_gpif_control(0, audio_on, streams_audio_batch());
        delay_us(1000);
        gpif_thread_restart(0, streaming);
        if (streaming) {
            fpga_gpif_control(1, audio_on, streams_audio_batch());
            video_start();
        }
    }
    if (audio_changed) {
        fpga_audio_control(0, 0);
        fpga_gpif_control(streaming, 0, streams_audio_batch());
        delay_us(1000);
        gpif_thread_restart(1, audio_on);
        if (audio_on)
            fpga_gpif_control(streaming, 1, streams_audio_batch());
    }
    /* Settings (audio batch/test source) refreshed without restarting the running streams. */
    fpga_gpif_control(streaming, audio_on, streams_audio_batch());
    fpga_audio_control(audio_on, audio_test);
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

void uvc_set_range(int range)
{
    range_override   = range;
    settings_request = 1;
}

void uvc_set_crop(int enable, uint16_t x, uint16_t y)
{
    crop_mode        = enable;
    crop_x           = x;
    crop_y           = y;
    settings_request = 1;
}

void uvc_audio_set_batch(int batch)
{
    audio_batch      = (batch < 1) ? 1 : (batch > 8) ? 8 : batch;
    settings_request = 1;
}

void uvc_audio_set_test(int test)
{
    audio_test       = test;
    settings_request = 1;
}

void uvc_service(void)
{
    int request, audio, settings;
    int was_streaming = streaming;
    int was_audio_on  = audio_on;

    if (color_request) {
        color_request = 0;
        uvc_apply_color();
    }
    /* Settled input change (mode, color space, loss): re-evaluate the video source. */
    if (it6802_get_status()->generation != input_generation) {
        input_generation = it6802_get_status()->generation;
        settings_request = 1;
    }
    if (stream_request == STREAM_IDLE && !audio_request && !settings_request)
        return;

    /* Take the pending requests atomically (set from the USB interrupt). */
    irq_disable();
    request          = stream_request;
    stream_request   = STREAM_IDLE;
    audio            = audio_request;
    audio_request    = 0;
    settings         = settings_request;
    settings_request = 0;
    irq_enable();

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

    /* (Re)start on stream start (new format), stream on/off changes, or settings changes while a
     * stream is active. */
    if (request == STREAM_START || streaming != was_streaming || audio_on != was_audio_on ||
        (settings && (streaming || audio_on)))
        streams_apply(request == STREAM_START || streaming != was_streaming || (settings && streaming),
            audio != 0);
}

/* Init ------------------------------------------------------------------------------------------ */

void uvc_init(void)
{
    uvc_probe_default(&probe, 0);
    uvc_probe_default(&commit, 0);
}
