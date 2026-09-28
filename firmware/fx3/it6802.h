/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 */

#ifndef IT6802_H
#define IT6802_H

#include <stdint.h>

struct it6802_status {
    uint8_t  present;      /* Chip answered.                       */
    uint8_t  sys_status;   /* Reg 0x0A (port 0): 5V, clock, SCDT.  */
    uint8_t  hpd;          /* HPD driven high.                     */
    uint8_t  stable;       /* Video stable (SCDT).                 */
    uint16_t htotal;
    uint16_t hactive;
    uint16_t vtotal;
    uint16_t vactive;
    uint8_t  pclk_reg;     /* Reg 0x9A (pixel clock measurement).  */
    uint8_t  video_mode;   /* Reg 0x99.                            */
    uint8_t  reserved[2];
};

int  it6802_init(void);
void it6802_service(void);
const struct it6802_status *it6802_get_status(void);
int  it6802_read(uint8_t bank, uint8_t reg, uint8_t *value);
int  it6802_write(uint8_t bank, uint8_t reg, uint8_t value);

#endif /* IT6802_H */
