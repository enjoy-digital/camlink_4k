/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * UVC Video Streaming: probe/commit negotiation, stream start/stop.
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

/* Class Requests -------------------------------------------------------------------------------- */

int uvc_class_request(const struct usb_setup *setup, uint8_t *buf)
{
    uint8_t intf     = setup->index & 0xff;
    uint8_t selector = setup->value >> 8;
    uint16_t len     = setup->length;
    struct uvc_probe *ctrl;

    /* Video Control interface: no controls. */
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

void uvc_stream_halt(void)
{
    stream_request = STREAM_STOP;
    uvc_stats[1]++;
}

/* Stream Control -------------------------------------------------------------------------------- */

static void uvc_stream_start(void)
{
    const struct uvc_frame *frame = &uvc_frames[commit.bFrameIndex - 1];
    uint32_t fps = 10000000UL/commit.dwFrameInterval;

    if (streaming)
        fpga_stream_stop();
    gpif_stream_start(GPIF_CLK_DIV_X2, GPIF_FLAG_OMEGA);
    fpga_stream_start(frame->width, frame->height, fps);
    streaming = 1;
}

static void uvc_stream_stop(void)
{
    if (!streaming)
        return;
    fpga_stream_stop();
    gpif_stream_stop();
    streaming = 0;
}

void uvc_service(void)
{
    int request = stream_request;
    stream_request = STREAM_IDLE;
    if (request == STREAM_START) {
        uvc_stats[2]++;
        uvc_stream_start();
    }
    if (request == STREAM_STOP) {
        uvc_stats[3]++;
        uvc_stream_stop();
    }
}

/* Init ------------------------------------------------------------------------------------------ */

void uvc_init(void)
{
    uvc_probe_default(&probe, 0);
    uvc_probe_default(&commit, 0);
}
