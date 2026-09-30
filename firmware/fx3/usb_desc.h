/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef USB_DESC_H
#define USB_DESC_H

#include <stdint.h>

#include "fx3.h"

#define USB_DESC_VID       0x1209 /* pid.codes. */
#define USB_DESC_PID       0x0001 /* pid.codes test PID (development). */

#define USB_DESC_EP_STREAM 1      /* Bulk IN streaming endpoint (0x81). */
#define USB_DESC_SS_BURST  16
#define USB_DESC_EP_AUDIO  2      /* Isochronous IN audio endpoint (0x82). */

/* UAC 1.0. */
#define UAC_CC_AUDIO              0x01
#define UAC_SC_AUDIOCONTROL       0x01
#define UAC_SC_AUDIOSTREAMING     0x02
#define UAC_CS_INTERFACE          0x24
#define UAC_CS_ENDPOINT           0x25
#define UAC_AC_HEADER             0x01
#define UAC_AC_INPUT_TERMINAL     0x02
#define UAC_AC_OUTPUT_TERMINAL    0x03
#define UAC_AS_GENERAL            0x01
#define UAC_AS_FORMAT_TYPE        0x02
#define UAC_EP_GENERAL            0x01

#define UAC_INTF_CONTROL   2
#define UAC_INTF_STREAMING 3
#define UAC_PACKET_SIZE    192    /* 1ms of 48kHz stereo 16-bit (one FX3 DMA buffer). */

const uint8_t *usb_desc_get(uint8_t type, uint8_t index, enum usb_speed speed);
uint16_t usb_desc_length(const uint8_t *desc);

#endif /* USB_DESC_H */
