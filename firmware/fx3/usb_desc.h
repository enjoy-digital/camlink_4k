/*
 * This file is part of LiteCamLink.
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

const uint8_t *usb_desc_get(uint8_t type, uint8_t index, enum usb_speed speed);
uint16_t usb_desc_length(const uint8_t *desc);

#endif /* USB_DESC_H */
