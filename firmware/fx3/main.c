/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * LiteCamLink FX3 firmware.
 */

#include <string.h>

#include "fx3.h"
#include "usb_desc.h"
#include "fpga.h"

/* Vendor Requests ------------------------------------------------------------------------------- */

enum {
    VREQ_IDENT     = 0x00, /* IN : Identification string.                          */
    VREQ_MEM_READ  = 0x01, /* IN : Read FX3 memory at (index << 16 | value).       */
    VREQ_MEM_WRITE = 0x02, /* OUT: Write FX3 memory at (index << 16 | value).      */
    VREQ_REBOOT    = 0x0f, /* OUT: Hard reset (back to the USB bootloader).        */
    VREQ_FPGA_INFO = 0x10, /* IN : FPGA IDCODE + status (2x32-bit).                */
    VREQ_FPGA_CFG  = 0x11, /* OUT: Start FPGA configuration (Slave-SPI).           */
    VREQ_FPGA_DATA = 0x12, /* OUT: FPGA bitstream chunk.                           */
    VREQ_FPGA_DONE = 0x13, /* IN : Finish FPGA configuration, returns status.      */
    VREQ_GPIO_CFG  = 0x20, /* OUT: GPIO value: 0 = input, 1 = output low, 2 = high. */
    VREQ_GPIO_READ = 0x21, /* IN : GPIO 0-60 input values (64-bit bitmap).         */
};

#define EP0_BUF_SIZE 4096

static uint8_t ep0_buf[EP0_BUF_SIZE] __attribute__((aligned(32)));
static volatile int reboot_request;

static const char ident[] = "LiteCamLink FX3 firmware " GIT_VERSION;

static void vendor_request(const struct usb_setup *setup)
{
    uint32_t addr = ((uint32_t)setup->index << 16) | setup->value;

    switch (setup->request) {
    case VREQ_IDENT: {
        uint16_t len = sizeof(ident);
        if (len > setup->length)
            len = setup->length;
        memcpy(ep0_buf, ident, len);
        usb_ep0_in(ep0_buf, len);
        return;
    }
    case VREQ_MEM_READ:
        if (setup->length > EP0_BUF_SIZE || (addr & 3) || (setup->length & 3))
            break;
        for (int i = 0; i < setup->length; i += 4)
            *(uint32_t *)&ep0_buf[i] = reg_read(addr + i);
        usb_ep0_in(ep0_buf, setup->length);
        return;
    case VREQ_MEM_WRITE:
        if (setup->length > EP0_BUF_SIZE || (addr & 3) || (setup->length & 3))
            break;
        if (usb_ep0_out(ep0_buf, setup->length) < 0)
            return;
        for (int i = 0; i < setup->length; i += 4)
            reg_write(addr + i, *(uint32_t *)&ep0_buf[i]);
        return;
    case VREQ_FPGA_INFO:
        ((uint32_t *)ep0_buf)[0] = fpga_read_idcode();
        ((uint32_t *)ep0_buf)[1] = fpga_read_status();
        usb_ep0_in(ep0_buf, 8);
        return;
    case VREQ_FPGA_CFG:
        fpga_config_start();
        usb_ep0_ack();
        return;
    case VREQ_FPGA_DATA:
        if (setup->length > EP0_BUF_SIZE)
            break;
        if (usb_ep0_out(ep0_buf, setup->length) < 0)
            return;
        fpga_config_data(ep0_buf, setup->length);
        return;
    case VREQ_FPGA_DONE:
        ((uint32_t *)ep0_buf)[0] = fpga_config_finish();
        usb_ep0_in(ep0_buf, 4);
        return;
    case VREQ_GPIO_CFG:
        if (setup->index > 60)
            break;
        if (setup->value == 0)
            gpio_setup_input(setup->index);
        else
            gpio_setup_output(setup->index, setup->value == 2);
        usb_ep0_ack();
        return;
    case VREQ_GPIO_READ: {
        uint32_t bitmap[2] = {0, 0};
        for (int i = 0; i < 61; i++)
            bitmap[i/32] |= (uint32_t)gpio_get(i) << (i%32);
        memcpy(ep0_buf, bitmap, 8);
        usb_ep0_in(ep0_buf, 8);
        return;
    }
    case VREQ_REBOOT:
        usb_ep0_ack();
        reboot_request = 1;
        return;
    }
    usb_ep0_stall();
}

/* Standard Requests ----------------------------------------------------------------------------- */

static uint8_t configuration;

static void standard_request(const struct usb_setup *setup, enum usb_speed speed)
{
    switch (setup->request) {
    case USB_REQ_GET_DESCRIPTOR: {
        const uint8_t *desc = usb_desc_get(setup->value >> 8, setup->value & 0xff, speed);
        uint16_t len;
        if (!desc)
            break;
        len = usb_desc_length(desc);
        if (len > setup->length)
            len = setup->length;
        memcpy(ep0_buf, desc, len);
        usb_ep0_in(ep0_buf, len);
        return;
    }
    case USB_REQ_SET_CONFIGURATION:
        if (setup->value > 1)
            break;
        configuration = setup->value;
        if (configuration)
            usb_enable_in_ep(USB_DESC_EP_STREAM, USB_EP_BULK,
                (speed == USB_SUPER_SPEED) ? 1024 : 512,
                (speed == USB_SUPER_SPEED) ? USB_DESC_SS_BURST : 1);
        usb_ep0_ack();
        return;
    case USB_REQ_GET_CONFIGURATION:
        ep0_buf[0] = configuration;
        usb_ep0_in(ep0_buf, 1);
        return;
    case USB_REQ_GET_STATUS:
        ep0_buf[0] = 0;
        ep0_buf[1] = 0;
        usb_ep0_in(ep0_buf, 2);
        return;
    case USB_REQ_GET_INTERFACE:
        ep0_buf[0] = 0;
        usb_ep0_in(ep0_buf, 1);
        return;
    case USB_REQ_SET_SEL:
        usb_ep0_out(ep0_buf, 6);
        return;
    case USB_REQ_SET_INTERFACE:
    case USB_REQ_SET_FEATURE:
    case USB_REQ_CLEAR_FEATURE:
    case USB_REQ_SET_ISOCH_DELAY:
        usb_ep0_ack();
        return;
    }
    usb_ep0_stall();
}

static void setup_request(const struct usb_setup *setup, enum usb_speed speed)
{
    switch (setup->request_type & USB_TYPE_MASK) {
    case USB_TYPE_STANDARD:
        standard_request(setup, speed);
        return;
    case USB_TYPE_VENDOR:
        vendor_request(setup);
        return;
    }
    usb_ep0_stall();
}

/* Main ------------------------------------------------------------------------------------------ */

int main(void)
{
    cache_enable();
    irq_init();
    gctl_init_clock();
    gctl_init_iomatrix(IOMATRIX_GPIF32BIT_UART_I2S);
    gpio_init_clock();
    fpga_init();
    irq_enable();

    usb_init(setup_request);
    usb_connect();

    for (;;) {
        if (reboot_request) {
            delay_us(10000);
            gctl_hard_reset();
        }
    }
}
