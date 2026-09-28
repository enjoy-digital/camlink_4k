/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * USB descriptors (High-Speed and SuperSpeed).
 */

#include "usb_desc.h"

#define LO(x) ((x) & 0xff)
#define HI(x) (((x) >> 8) & 0xff)

/* Device ---------------------------------------------------------------------------------------- */

static const uint8_t device_hs[] = {
    18, USB_DT_DEVICE,
    0x10, 0x02,                          /* bcdUSB 2.10.        */
    0x00, 0x00, 0x00,                    /* Class in interface. */
    64,                                  /* bMaxPacketSize0.    */
    LO(USB_DESC_VID), HI(USB_DESC_VID),
    LO(USB_DESC_PID), HI(USB_DESC_PID),
    0x00, 0x01,                          /* bcdDevice 1.00.     */
    1, 2, 3,                             /* Strings.            */
    1,                                   /* Configurations.     */
};

static const uint8_t device_ss[] = {
    18, USB_DT_DEVICE,
    0x20, 0x03,                          /* bcdUSB 3.20.        */
    0x00, 0x00, 0x00,
    9,                                   /* 2^9 = 512 bytes.    */
    LO(USB_DESC_VID), HI(USB_DESC_VID),
    LO(USB_DESC_PID), HI(USB_DESC_PID),
    0x00, 0x01,
    1, 2, 3,
    1,
};

static const uint8_t device_qualifier[] = {
    10, USB_DT_DEVICE_QUALIFIER,
    0x00, 0x02,
    0x00, 0x00, 0x00,
    64,
    1,
    0,
};

/* BOS ------------------------------------------------------------------------------------------- */

static const uint8_t bos[] = {
    5, USB_DT_BOS, 22, 0, 2,
    /* USB 2.0 extension (LPM). */
    7, 0x10, 0x02, 0x02, 0x00, 0x00, 0x00,
    /* SuperSpeed device capability. */
    10, 0x10, 0x03,
    0x00,                                /* bmAttributes.                   */
    0x0e, 0x00,                          /* Speeds: FS, HS, SS.             */
    0x03,                                /* Lowest full functionality: SS.  */
    0x0a,                                /* U1 exit latency.                */
    0xff, 0x07,                          /* U2 exit latency.                */
};

/* Configuration --------------------------------------------------------------------------------- */

#define CONFIG_HS_LEN (9 + 9 + 7)
#define CONFIG_SS_LEN (9 + 9 + 7 + 6)

static const uint8_t config_hs[] = {
    9, USB_DT_CONFIG, LO(CONFIG_HS_LEN), HI(CONFIG_HS_LEN),
    1,                                   /* Interfaces.         */
    1,                                   /* Configuration.      */
    0,
    0x80,                                /* Bus powered.        */
    250,                                 /* 500mA.              */
    /* Interface 0: Vendor. */
    9, USB_DT_INTERFACE, 0, 0, 1, 0xff, 0x00, 0x00, 0,
    /* EP1 IN: Bulk 512. */
    7, USB_DT_ENDPOINT, 0x80 | USB_DESC_EP_STREAM, 0x02, LO(512), HI(512), 0,
};

static const uint8_t config_ss[] = {
    9, USB_DT_CONFIG, LO(CONFIG_SS_LEN), HI(CONFIG_SS_LEN),
    1,
    1,
    0,
    0x80,
    112,                                 /* 896mA (8mA units).  */
    /* Interface 0: Vendor. */
    9, USB_DT_INTERFACE, 0, 0, 1, 0xff, 0x00, 0x00, 0,
    /* EP1 IN: Bulk 1024. */
    7, USB_DT_ENDPOINT, 0x80 | USB_DESC_EP_STREAM, 0x02, LO(1024), HI(1024), 0,
    /* SuperSpeed endpoint companion. */
    6, 0x30, USB_DESC_SS_BURST - 1, 0, 0, 0,
};

/* Strings --------------------------------------------------------------------------------------- */

static const uint8_t string_lang[] = {4, USB_DT_STRING, 0x09, 0x04};

static const uint8_t string_manufacturer[] = {
    28, USB_DT_STRING,
    'E',0, 'n',0, 'j',0, 'o',0, 'y',0, '-',0, 'D',0, 'i',0, 'g',0, 'i',0, 't',0, 'a',0, 'l',0,
};

static const uint8_t string_product[] = {
    24, USB_DT_STRING,
    'L',0, 'i',0, 't',0, 'e',0, 'C',0, 'a',0, 'm',0, 'L',0, 'i',0, 'n',0, 'k',0,
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
