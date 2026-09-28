/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef UVC_H
#define UVC_H

#include <stdint.h>

#include "fx3.h"
#include "generated/fpga_csr.h"

/* UVC Constants --------------------------------------------------------------------------------- */

#define UVC_CC_VIDEO                      0x0e
#define UVC_SC_VIDEOCONTROL               0x01
#define UVC_SC_VIDEOSTREAMING             0x02
#define UVC_SC_VIDEO_INTERFACE_COLLECTION 0x03
#define UVC_CS_INTERFACE                  0x24
#define UVC_VC_HEADER                     0x01
#define UVC_VC_INPUT_TERMINAL             0x02
#define UVC_VC_OUTPUT_TERMINAL            0x03
#define UVC_VS_INPUT_HEADER               0x01
#define UVC_VS_FORMAT_UNCOMPRESSED        0x04
#define UVC_VS_FRAME_UNCOMPRESSED         0x05
#define UVC_VS_COLORFORMAT                0x0d

#define UVC_SET_CUR  0x01
#define UVC_GET_CUR  0x81
#define UVC_GET_MIN  0x82
#define UVC_GET_MAX  0x83
#define UVC_GET_RES  0x84
#define UVC_GET_LEN  0x85
#define UVC_GET_INFO 0x86
#define UVC_GET_DEF  0x87

#define UVC_VS_PROBE_CONTROL  0x01
#define UVC_VS_COMMIT_CONTROL 0x02

#define UVC_INTF_CONTROL   0
#define UVC_INTF_STREAMING 1

/* Stream Configuration -------------------------------------------------------------------------- */

#define UVC_CLOCK_FREQ      CSR_CONST_CONFIG_CLOCK_FREQUENCY /* FPGA sys clock (PTS/SCR). */
#define UVC_PAYLOAD_SIZE    16384       /* One FX3 DMA buffer per payload.      */
#define UVC_FRAME_COUNT     3
#define UVC_FRAME_INTERVALS 2
#define UVC_FPS_MAX         60
#define UVC_FPS_MIN         30
#define UVC_INTERVAL(fps)   (10000000UL/(fps))

struct uvc_frame {
    uint16_t width;
    uint16_t height;
};

extern const struct uvc_frame uvc_frames[UVC_FRAME_COUNT];

/* Probe/Commit (UVC 1.1, 34 bytes) -------------------------------------------------------------- */

struct __attribute__((packed)) uvc_probe {
    uint16_t bmHint;
    uint8_t  bFormatIndex;
    uint8_t  bFrameIndex;
    uint32_t dwFrameInterval;
    uint16_t wKeyFrameRate;
    uint16_t wPFrameRate;
    uint16_t wCompQuality;
    uint16_t wCompWindowSize;
    uint16_t wDelay;
    uint32_t dwMaxVideoFrameSize;
    uint32_t dwMaxPayloadTransferSize;
    uint32_t dwClockFrequency;
    uint8_t  bmFramingInfo;
    uint8_t  bPreferedVersion;
    uint8_t  bMinVersion;
    uint8_t  bMaxVersion;
};

/* API ------------------------------------------------------------------------------------------- */

void uvc_init(void);
/* Class request (interface recipient), returns 0 if handled. Called from interrupt context. */
int  uvc_class_request(const struct usb_setup *setup, uint8_t *buf);
/* Endpoint halt cleared on the streaming endpoint (stream off). */
void uvc_stream_halt(void);
/* Main loop service: applies pending stream start/stop (FPGA configuration over I2C). */
void uvc_service(void);
/* Audio streaming interface alternate setting (0: idle, 1: streaming). Called from interrupt context. */
void uvc_audio_set_interface(uint8_t alt);
uint8_t uvc_audio_get_interface(void);
/* Scaling of inputs larger than the frame: 2x downscale (default) or crop window at (x, y). */
void uvc_set_crop(int enable, uint16_t x, uint16_t y);
/* Audio source: 0 = HDMI (I2S), 1 = test counter. */
void uvc_audio_set_test(int test);

#endif /* UVC_H */
