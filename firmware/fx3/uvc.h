/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef UVC_H
#define UVC_H

#include <stdint.h>

#include "fx3.h"
#include "gpif.h"
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
#define UVC_VC_PROCESSING_UNIT            0x05
#define UVC_VC_EXTENSION_UNIT             0x06

/* Video Control entities. */
#define UVC_ID_CAMERA     1
#define UVC_ID_OUTPUT     2
#define UVC_ID_PROCESSING 3
#define UVC_ID_EXTENSION  4

/* Processing Unit controls. */
#define UVC_PU_BRIGHTNESS_CONTROL 0x02
#define UVC_PU_CONTRAST_CONTROL   0x03
#define UVC_PU_SATURATION_CONTROL 0x07

/* Extension Unit controls (CamLink 4K). */
#define XU_INPUT_INFO_CONTROL 0x01 /* GET: struct it6802_status (24 bytes).                  */
#define XU_CROP_CONTROL       0x02 /* GET/SET: x (u16), y (u16); x = 0xffff: 2x downscale.   */
#define XU_CONTROLS           2
/* {4c434c4c-4b4e-4943-4c43-4b3478750001} ("CamLink 4K XU"). */
#define XU_GUID 0x4c, 0x4c, 0x43, 0x4c, 0x4e, 0x4b, 0x43, 0x49, 0x4c, 0x43, 0x4b, 0x34, 0x78, 0x75, 0x00, 0x01
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

#ifdef CSR_CONST_CONFIG_VIDEO_CLOCK_FREQUENCY
#define VIDEO_CLOCK_FREQ    CSR_CONST_CONFIG_VIDEO_CLOCK_FREQUENCY /* FPGA video pipeline clock. */
#else
#define VIDEO_CLOCK_FREQ    CSR_CONST_CONFIG_CLOCK_FREQUENCY
#endif
#define UVC_CLOCK_FREQ      VIDEO_CLOCK_FREQ /* PTS/SCR (video clock timestamps). */
#define UVC_PAYLOAD_SIZE    GPIF_DMA_BUF_SIZE /* One FX3 DMA buffer per payload. */
#define UVC_FRAME_INTERVALS 2
#define UVC_FPS_MAX         60
#define UVC_FPS_MIN         30
#define UVC_INTERVAL(fps)   (10000000UL/(fps))

/* Formats: 1 = YUY2 (1920x1080, 1280x720, 640x480 at 30/60), 2 = M420 (YUV 4:2:0, 2 lines of Y
 * then 1 line of CbCr: 3840x2160 at 30, 1920x1080 at 30/60). */
#define UVC_FORMAT_YUY2      1
#define UVC_FORMAT_M420      2
#ifdef CSR_FRAMEBUFFER_BASE
/* 3 = NV12 (Y plane then CbCr plane) through the FPGA DRAM frame buffer: 3840x2160 at 30,
 * 1920x1080 at 30/60. */
#define UVC_FORMAT_NV12      3
#define UVC_FORMAT_COUNT     3
#else
#define UVC_FORMAT_COUNT     2
#endif
#define UVC_YUY2_FRAME_COUNT 3
#define UVC_M420_FRAME_COUNT 2
#define UVC_NV12_FRAME_COUNT 2

struct uvc_frame {
    uint16_t width;
    uint16_t height;
    uint8_t  fps_max;
};

struct uvc_format {
    const struct uvc_frame *frames;
    uint8_t count;
    uint8_t bpp;
    uint8_t m420; /* YUV 4:2:0 (M420 or NV12). */
    uint8_t nv12; /* NV12 (DRAM frame buffer). */
};

extern const struct uvc_format uvc_formats[UVC_FORMAT_COUNT];

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
/* USB bus reset / link change: stop all streams (interrupt context). */
void uvc_bus_reset(void);
/* Main loop service: applies pending stream start/stop (FPGA configuration over I2C). */
void uvc_service(void);
/* Audio streaming interface alternate setting (0: idle, 1: streaming). Called from interrupt context. */
void uvc_audio_set_interface(uint8_t alt);
uint8_t uvc_audio_get_interface(void);
/* Scaling of inputs larger than the frame: 2x downscale (default) or crop window at (x, y). */
void uvc_set_crop(int enable, uint16_t x, uint16_t y);
/* RGB input range: 0 = auto (AVI InfoFrame, full when default), 1 = limited, 2 = full. */
void uvc_set_range(int range);
/* Audio source: 0 = HDMI (I2S), 1 = test counter. */
void uvc_audio_set_test(int test);
/* Audio packets (1ms) per GPIF thread switch (1-8, latency vs video bandwidth). */
void uvc_audio_set_batch(int batch);

#endif /* UVC_H */
