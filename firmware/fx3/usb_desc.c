/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * USB descriptors (High-Speed and SuperSpeed): UVC 1.1 camera, bulk streaming on EP1 IN, UAC 1.0
 * 48kHz stereo 16-bit audio, isochronous EP2 IN.
 *
 * Interface 0: Video Control   (Camera Terminal -> Processing Unit (brightness, contrast, saturation)
 *              -> Extension Unit (input info, crop) -> Output Terminal).
 * Interface 1: Video Streaming (YUY2, frames from uvc_frames[], bulk EP1 IN).
 * Interface 2: Audio Control   (Input Terminal (digital audio interface) -> USB Streaming).
 * Interface 3: Audio Streaming (alt 0: idle, alt 1: PCM 48kHz stereo 16-bit, iso EP2 IN, 1ms).
 */

#include "usb_desc.h"
#include "uvc.h"

#define LO(x)  ((x) & 0xff)
#define HI(x)  (((x) >> 8) & 0xff)
#define W16(x) LO(x), HI(x)
#define W32(x) ((x) & 0xff), (((x) >> 8) & 0xff), (((x) >> 16) & 0xff), (((x) >> 24) & 0xff)

/* Device ---------------------------------------------------------------------------------------- */

static const uint8_t device_hs[] = {
    18, USB_DT_DEVICE,
    W16(0x0210),                         /* bcdUSB 2.10.                          */
    0xef, 0x02, 0x01,                    /* Misc/Common/IAD.                      */
    64,                                  /* bMaxPacketSize0.                      */
    W16(USB_DESC_VID), W16(USB_DESC_PID),
    W16(0x0100),                         /* bcdDevice 1.00.                       */
    1, 2, 3,                             /* Strings.                              */
    1,                                   /* Configurations.                       */
};

static const uint8_t device_ss[] = {
    18, USB_DT_DEVICE,
    W16(0x0320),                         /* bcdUSB 3.20.                          */
    0xef, 0x02, 0x01,
    9,                                   /* 2^9 = 512 bytes.                      */
    W16(USB_DESC_VID), W16(USB_DESC_PID),
    W16(0x0100),
    1, 2, 3,
    1,
};

static const uint8_t device_qualifier[] = {
    10, USB_DT_DEVICE_QUALIFIER, W16(0x0200), 0xef, 0x02, 0x01, 64, 1, 0,
};

/* BOS ------------------------------------------------------------------------------------------- */

static const uint8_t bos[] = {
    5, USB_DT_BOS, W16(22), 2,
    /* USB 2.0 extension (LPM). */
    7, 0x10, 0x02, W32(0x00000002),
    /* SuperSpeed device capability. */
    10, 0x10, 0x03, 0x00, W16(0x000e), 0x03, 0x0a, W16(0x07ff),
};

/* Configuration --------------------------------------------------------------------------------- */

#define VC_TOTAL      (13 + 18 + 12 + 26 + 9)
#define FRAME_LEN     (26 + 4*UVC_FRAME_INTERVALS)
#define FRAME30_LEN   (26 + 4)
#ifdef UVC_FORMAT_NV12
#define NV12_LEN      (27 + FRAME30_LEN + FRAME_LEN + 6)
#define VS_HEADER_CTRLS , 0 /* bmaControls of format 3. */
#else
#define NV12_LEN      0
#define VS_HEADER_CTRLS
#endif
/* VS input header: 13 bytes + 1 bmaControls byte per format. */
#define VS_HEADER_LEN (13 + UVC_FORMAT_COUNT)
#define VS_TOTAL      (VS_HEADER_LEN + (27 + UVC_YUY2_FRAME_COUNT*FRAME_LEN + 6) + (27 + FRAME30_LEN + FRAME_LEN + 6) + NV12_LEN)
#define AC_TOTAL      (9 + 12 + 9)
#define AUDIO_HS_LEN  (8 + 9 + AC_TOTAL + 9 + 9 + 7 + 11 + 9 + 7)
#define AUDIO_SS_LEN  (AUDIO_HS_LEN + 6)
#define CONFIG_HS_LEN (9 + 8 + 9 + VC_TOTAL + 9 + VS_TOTAL + 7 + AUDIO_HS_LEN)
#define CONFIG_SS_LEN (9 + 8 + 9 + VC_TOTAL + 9 + VS_TOTAL + 7 + 6 + AUDIO_SS_LEN)

/* Frame at 30/60 fps, and 30 fps only (bpp: bits per pixel). */
#define FRAME_DESC(index, w, h, bpp)                                                                \
    FRAME_LEN, UVC_CS_INTERFACE, UVC_VS_FRAME_UNCOMPRESSED, index, 0x00,                            \
    W16(w), W16(h),                                                                                 \
    W32((w)*(h)*(bpp)*(uint32_t)UVC_FPS_MIN), W32((w)*(h)*(bpp)*(uint32_t)UVC_FPS_MAX), W32((w)*(h)*(bpp)/8),           \
    W32(UVC_INTERVAL(UVC_FPS_MIN)), UVC_FRAME_INTERVALS,                                            \
    W32(UVC_INTERVAL(UVC_FPS_MAX)), W32(UVC_INTERVAL(UVC_FPS_MIN))

#define FRAME30_DESC(index, w, h, bpp)                                                              \
    FRAME30_LEN, UVC_CS_INTERFACE, UVC_VS_FRAME_UNCOMPRESSED, index, 0x00,                          \
    W16(w), W16(h),                                                                                 \
    W32((w)*(h)*(bpp)*(uint32_t)UVC_FPS_MIN), W32((w)*(h)*(bpp)*(uint32_t)UVC_FPS_MIN), W32((w)*(h)*(bpp)/8),           \
    W32(UVC_INTERVAL(UVC_FPS_MIN)), 1,                                                              \
    W32(UVC_INTERVAL(UVC_FPS_MIN))

/* Format 3 (FPGA DRAM frame buffer): Uncompressed NV12 (Y plane, then interleaved CbCr plane). */
#ifdef UVC_FORMAT_NV12
#define NV12_BODY                                                                                   \
    , 27, UVC_CS_INTERFACE, UVC_VS_FORMAT_UNCOMPRESSED, UVC_FORMAT_NV12, UVC_NV12_FRAME_COUNT,      \
    'N', 'V', '1', '2', 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71,     \
    12, 1, 0, 0, 0, 0,                                                                              \
    FRAME30_DESC(1, 3840, 2160, 12),                                                                \
    FRAME_DESC(2, 1920, 1080, 12),                                                                  \
    6, UVC_CS_INTERFACE, UVC_VS_COLORFORMAT, 1, 1, 1
#else
#define NV12_BODY
#endif

#define CONFIG_BODY(total, max_power)                                                               \
    /* Configuration. */                                                                            \
    9, USB_DT_CONFIG, W16(total), 4, 1, 0, 0x80, max_power,                                         \
    /* Interface Association. */                                                                   \
    8, 0x0b, 0, 2, UVC_CC_VIDEO, UVC_SC_VIDEO_INTERFACE_COLLECTION, 0, 2,                           \
    /* VC Interface. */                                                                             \
    9, USB_DT_INTERFACE, UVC_INTF_CONTROL, 0, 0, UVC_CC_VIDEO, UVC_SC_VIDEOCONTROL, 0, 2,           \
    /* VC Header. */                                                                                \
    13, UVC_CS_INTERFACE, UVC_VC_HEADER, W16(0x0110), W16(VC_TOTAL), W32(UVC_CLOCK_FREQ),           \
    1, UVC_INTF_STREAMING,                                                                          \
    /* Camera Terminal (ID 1). */                                                                   \
    18, UVC_CS_INTERFACE, UVC_VC_INPUT_TERMINAL, 1, W16(0x0201), 0, 0, W16(0), W16(0), W16(0),      \
    3, 0, 0, 0,                                                                                     \
    /* Processing Unit (ID 3): brightness, contrast, saturation (UVC 1.1: + bmVideoStandards). */ \
    12, UVC_CS_INTERFACE, UVC_VC_PROCESSING_UNIT, UVC_ID_PROCESSING, UVC_ID_CAMERA, W16(0), 2,      \
    W16(0x000b), 0, 0,                                                                              \
    /* Extension Unit (ID 4): input info, crop. */                                                 \
    26, UVC_CS_INTERFACE, UVC_VC_EXTENSION_UNIT, UVC_ID_EXTENSION, XU_GUID, XU_CONTROLS, 1,         \
    UVC_ID_PROCESSING, 1, 0x03, 0,                                                                  \
    /* Output Terminal (ID 2). */                                                                   \
    9, UVC_CS_INTERFACE, UVC_VC_OUTPUT_TERMINAL, UVC_ID_OUTPUT, W16(0x0101), 0, UVC_ID_EXTENSION, 0,\
    /* VS Interface. */                                                                             \
    9, USB_DT_INTERFACE, UVC_INTF_STREAMING, 0, 1, UVC_CC_VIDEO, UVC_SC_VIDEOSTREAMING, 0, 0,       \
    /* VS Input Header. */                                                                          \
    VS_HEADER_LEN, UVC_CS_INTERFACE, UVC_VS_INPUT_HEADER, UVC_FORMAT_COUNT, W16(VS_TOTAL),          \
    0x80 | USB_DESC_EP_STREAM, 0, 2, 0, 0, 0, 1, 0, 0 VS_HEADER_CTRLS,                              \
    /* Format 1: Uncompressed YUY2. */                                                              \
    27, UVC_CS_INTERFACE, UVC_VS_FORMAT_UNCOMPRESSED, UVC_FORMAT_YUY2, UVC_YUY2_FRAME_COUNT,        \
    'Y', 'U', 'Y', '2', 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71,     \
    16, 1, 0, 0, 0, 0,                                                                              \
    FRAME_DESC(1, 1920, 1080, 16),                                                                  \
    FRAME_DESC(2, 1280,  720, 16),                                                                  \
    FRAME_DESC(3,  640,  480, 16),                                                                  \
    /* Color Matching (BT.709 primaries/transfer/matrix). */                                       \
    6, UVC_CS_INTERFACE, UVC_VS_COLORFORMAT, 1, 1, 1,                                               \
    /* Format 2: Uncompressed M420 (YUV 4:2:0, 2 lines of Y, 1 line of CbCr). */                   \
    27, UVC_CS_INTERFACE, UVC_VS_FORMAT_UNCOMPRESSED, UVC_FORMAT_M420, UVC_M420_FRAME_COUNT,        \
    'M', '4', '2', '0', 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71,     \
    12, 1, 0, 0, 0, 0,                                                                              \
    FRAME30_DESC(1, 3840, 2160, 12),                                                                \
    FRAME_DESC(2, 1920, 1080, 12),                                                                  \
    6, UVC_CS_INTERFACE, UVC_VS_COLORFORMAT, 1, 1, 1                                                \
    NV12_BODY

#define AUDIO_BODY_START                                                                            \
    /* Interface Association. */                                                                   \
    8, 0x0b, UAC_INTF_CONTROL, 2, UAC_CC_AUDIO, 0x00, 0x00, 0,                                      \
    /* AC Interface. */                                                                             \
    9, USB_DT_INTERFACE, UAC_INTF_CONTROL, 0, 0, UAC_CC_AUDIO, UAC_SC_AUDIOCONTROL, 0, 0,          \
    /* AC Header. */                                                                                \
    9, UAC_CS_INTERFACE, UAC_AC_HEADER, W16(0x0100), W16(AC_TOTAL), 1, UAC_INTF_STREAMING,         \
    /* Input Terminal (ID 1): digital audio interface (HDMI), stereo. */                           \
    12, UAC_CS_INTERFACE, UAC_AC_INPUT_TERMINAL, 1, W16(0x0602), 0, 2, W16(0x0003), 0, 0,           \
    /* Output Terminal (ID 2): USB streaming. */                                                   \
    9, UAC_CS_INTERFACE, UAC_AC_OUTPUT_TERMINAL, 2, W16(0x0101), 0, 1, 0,                           \
    /* AS Interface, alt 0 (idle). */                                                              \
    9, USB_DT_INTERFACE, UAC_INTF_STREAMING, 0, 0, UAC_CC_AUDIO, UAC_SC_AUDIOSTREAMING, 0, 0,      \
    /* AS Interface, alt 1 (streaming). */                                                         \
    9, USB_DT_INTERFACE, UAC_INTF_STREAMING, 1, 1, UAC_CC_AUDIO, UAC_SC_AUDIOSTREAMING, 0, 0,      \
    /* AS General: linked to the Output Terminal, PCM. */                                          \
    7, UAC_CS_INTERFACE, UAC_AS_GENERAL, 2, 1, W16(0x0001),                                         \
    /* Format Type I: 2 channels, 16-bit, 48kHz. */                                                \
    11, UAC_CS_INTERFACE, UAC_AS_FORMAT_TYPE, 0x01, 2, 2, 16, 1, 0x80, 0xbb, 0x00,                  \
    /* EP2 IN: Isochronous (asynchronous), 1ms. */                                                 \
    9, USB_DT_ENDPOINT, 0x80 | USB_DESC_EP_AUDIO, 0x05, W16(UAC_PACKET_SIZE), 4, 0, 0

#define AUDIO_BODY_END                                                                              \
    /* CS Endpoint General. */                                                                      \
    7, UAC_CS_ENDPOINT, UAC_EP_GENERAL, 0x00, 0, W16(0)

static const uint8_t config_hs[] = {
    CONFIG_BODY(CONFIG_HS_LEN, 250),
    /* EP1 IN: Bulk 512. */
    7, USB_DT_ENDPOINT, 0x80 | USB_DESC_EP_STREAM, 0x02, W16(512), 0,
    AUDIO_BODY_START,
    AUDIO_BODY_END,
};

static const uint8_t config_ss[] = {
    CONFIG_BODY(CONFIG_SS_LEN, 112),
    /* EP1 IN: Bulk 1024 + SuperSpeed companion. */
    7, USB_DT_ENDPOINT, 0x80 | USB_DESC_EP_STREAM, 0x02, W16(1024), 0,
    6, 0x30, USB_DESC_SS_BURST - 1, 0, 0, 0,
    AUDIO_BODY_START,
    /* EP2 SuperSpeed companion: 1 packet per interval. */
    6, 0x30, 0, 0, W16(UAC_PACKET_SIZE),
    AUDIO_BODY_END,
};

_Static_assert(sizeof(config_hs) == CONFIG_HS_LEN, "HS configuration length mismatch");
_Static_assert(sizeof(config_ss) == CONFIG_SS_LEN, "SS configuration length mismatch");

/* Strings --------------------------------------------------------------------------------------- */

static const uint8_t string_lang[] = {4, USB_DT_STRING, W16(0x0409)};

static const uint8_t string_manufacturer[] = {
    28, USB_DT_STRING,
    'E',0, 'n',0, 'j',0, 'o',0, 'y',0, '-',0, 'D',0, 'i',0, 'g',0, 'i',0, 't',0, 'a',0, 'l',0,
};

static const uint8_t string_product[] = {
    16, USB_DT_STRING,
    'C',0, 'a',0, 'm',0, 'L',0, 'i',0, 'n',0, 'X',0,
};

static const uint8_t string_serial[] = {
    10, USB_DT_STRING,
    '0',0, '0',0, '0',0, '1',0,
};

static const uint8_t *const strings[] = {
    string_lang,
    string_manufacturer,
    string_product,
    string_serial,
};

/* Access ---------------------------------------------------------------------------------------- */

const uint8_t *usb_desc_get(uint8_t type, uint8_t index, enum usb_speed speed)
{
    int ss = (speed == USB_SUPER_SPEED);

    switch (type) {
    case USB_DT_DEVICE:
        return ss ? device_ss : device_hs;
    case USB_DT_CONFIG:
        return ss ? config_ss : config_hs;
    case USB_DT_DEVICE_QUALIFIER:
        return ss ? 0 : device_qualifier;
    case USB_DT_BOS:
        return bos;
    case USB_DT_STRING:
        if (index < sizeof(strings)/sizeof(strings[0]))
            return strings[index];
        return 0;
    }
    return 0;
}

uint16_t usb_desc_length(const uint8_t *desc)
{
    /* Configuration/BOS descriptors carry their total length. */
    if (desc[1] == USB_DT_CONFIG || desc[1] == USB_DT_BOS)
        return desc[2] | (desc[3] << 8);
    return desc[0];
}
