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
#include "i2c.h"
#include "gpif.h"
#include "uvc.h"
#include "it6802.h"
#include "spi_flash.h"
#include "fpga_ctrl.h"

#include "generated/fpga_csr.h"

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
    VREQ_I2C_WRITE = 0x30, /* OUT: I2C write, value = addr | prefix_len << 8, data = prefix + payload. */
    VREQ_I2C_READ  = 0x31, /* IN : I2C read,  value = addr | prefix_len << 8, index = prefix (LE).  */
    VREQ_I2C_STATUS= 0x32, /* IN : Status of the last I2C transfer (0 = OK).       */
    VREQ_STREAM_START  = 0x40, /* OUT: Start GPIF streaming, value = bit0: video, bit1: audio (96MHz). */
    VREQ_STREAM_STOP   = 0x41, /* OUT: Stop GPIF streaming.                        */
    VREQ_STREAM_STATUS = 0x42, /* IN : GPIF/DMA status (8x32-bit).                 */
    VREQ_HDMI_STATUS   = 0x50, /* IN : IT6802 status (struct it6802_status).        */
    VREQ_HDMI_INIT     = 0x51, /* OUT: (Re)initialize the IT6802.                   */
    VREQ_FLASH_ID      = 0x60, /* IN : SPI flash JEDEC ID.                          */
    VREQ_FLASH_READ    = 0x61, /* IN : Read flash at (index << 16 | value).         */
    VREQ_FLASH_PROGRAM = 0x62, /* OUT: Program flash at (index << 16 | value).      */
    VREQ_FLASH_ERASE   = 0x63, /* OUT: Erase the 64KB block at (index << 16 | value) (deferred). */
    VREQ_FLASH_STATUS  = 0x64, /* IN : 1 while a deferred flash operation is pending. */
    VREQ_FLASH_RECOVER = 0x65, /* OUT: Erase block 0 (FX3 image) and reboot to the USB bootloader. */
    VREQ_AUDIO_TEST    = 0x70, /* OUT: Audio source, value = 0: HDMI (I2S), 1: test counter. */
    VREQ_WATCHDOG      = 0x72, /* OUT: FPGA watchdog period (ms, 0: off, heartbeat stopped). */
    VREQ_HANG          = 0x74, /* OUT: Debug: hang with interrupts off (watchdog test). */
    VREQ_STATS         = 0x75, /* IN : Debug counters (see vendor_request).          */
    VREQ_AUDIO_BATCH   = 0x76, /* OUT: Audio packets per GPIF thread switch (value, 1-8). */
    VREQ_RANGE         = 0x77, /* OUT: RGB input range, value = 0: auto, 1: limited, 2: full. */
    VREQ_CROP          = 0x71, /* OUT: Crop mode, value = x (0xffff: off, downscale), index = y. */
    VREQ_FPGA_BOOT     = 0x66, /* OUT: Load the FPGA from the flash bitstream (deferred, status via FLASH_STATUS). */
};

#define EP0_BUF_SIZE 4096

static uint8_t ep0_buf[EP0_BUF_SIZE] __attribute__((aligned(32)));
static volatile int reboot_request;
static int i2c_status;
volatile uint32_t main_loops;

extern volatile uint32_t usb_isr_count;
extern volatile uint32_t usb_link_stats[3];
extern volatile uint32_t uvc_stats[4];
static volatile int hdmi_init_request;
static volatile int debug_stream_request; /* 0x100 | streams: start, 0x200: stop. */

/* Flash / FPGA Boot ------------------------------------------------------------------------------ */

#define FLASH_BITSTREAM_MAGIC 0x4b4c434cUL /* "LCLK": LiteCamLink bitstream (stock ones not autoloaded). */

static volatile uint32_t flash_erase_addr;
static volatile int      flash_erase_request;
static volatile int      flash_recover_request;
static volatile int      fpga_boot_request;
static volatile uint32_t fpga_boot_status;
static uint8_t           flash_buf[4096] __attribute__((aligned(32)));

static uint32_t fpga_boot_from_flash(void)
{
    uint32_t hdr[3];
    uint32_t size;

    spi_flash_read(FLASH_BITSTREAM_HDR, (uint8_t *)hdr, sizeof(hdr));
    size = hdr[0];
    if (hdr[1] != ~size || hdr[2] != FLASH_BITSTREAM_MAGIC || size == 0 || size > 0x2f0000)
        return 0;
    fpga_config_start();
    for (uint32_t off = 0; off < size; off += sizeof(flash_buf)) {
        uint32_t n = (size - off) < sizeof(flash_buf) ? (size - off) : sizeof(flash_buf);
        spi_flash_read(FLASH_BITSTREAM + off, flash_buf, n);
        fpga_config_data(flash_buf, n);
    }
    return fpga_config_finish();
}

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
    case VREQ_I2C_WRITE: {
        uint8_t addr       = setup->value & 0x7f;
        uint8_t prefix_len = setup->value >> 8;
        if (setup->length > EP0_BUF_SIZE || prefix_len > setup->length)
            break;
        if (setup->length && usb_ep0_out(ep0_buf, setup->length) < 0)
            return;
        if (!setup->length)
            usb_ep0_ack();
        /* Errors are reported through VREQ_I2C_READ/status only (data stage already acked). */
        if (addr == IT6802_I2C_ADDR)
            it6802_invalidate_bank(); /* Host access may change the IT6802 register bank. */
        i2c_status = i2c_write(addr, ep0_buf, prefix_len, ep0_buf + prefix_len,
            setup->length - prefix_len);
        return;
    }
    case VREQ_I2C_READ: {
        uint8_t addr       = setup->value & 0x7f;
        uint8_t prefix_len = setup->value >> 8;
        uint8_t prefix[2]  = {setup->index & 0xff, setup->index >> 8};
        if (setup->length > EP0_BUF_SIZE || prefix_len > 2)
            break;
        if (addr == IT6802_I2C_ADDR)
            it6802_invalidate_bank();
        i2c_status = i2c_read(addr, prefix, prefix_len, ep0_buf, setup->length);
        if (i2c_status)
            break;
        usb_ep0_in(ep0_buf, setup->length);
        return;
    }
    case VREQ_I2C_STATUS:
        ep0_buf[0] = (uint8_t)i2c_status;
        usb_ep0_in(ep0_buf, 1);
        return;
    case VREQ_STREAM_START: /* Debug (raw streaming tests): applied from the main loop. */
        debug_stream_request = 0x100 | (setup->value & 3);
        usb_ep0_ack();
        return;
    case VREQ_STREAM_STOP:
        debug_stream_request = 0x200;
        usb_ep0_ack();
        return;
    case VREQ_STREAM_STATUS:
        gpif_stream_status((uint32_t *)ep0_buf);
        usb_ep0_in(ep0_buf, 32);
        return;
    case VREQ_HDMI_STATUS:
    {
        struct it6802_status *st = (struct it6802_status *)ep0_buf;
        uint16_t len = sizeof(*st) < setup->length ? sizeof(*st) : setup->length;
        memcpy(st, it6802_get_status(), sizeof(*st));
        st->frame_period = fpga_hdmi_frame_period();
        usb_ep0_in(ep0_buf, len);
        return;
    }
    case VREQ_HDMI_INIT:
        usb_ep0_ack();
        hdmi_init_request = 1;
        return;
    case VREQ_FLASH_ID:
        ((uint32_t *)ep0_buf)[0] = spi_flash_read_id();
        usb_ep0_in(ep0_buf, 4);
        return;
    case VREQ_FLASH_READ:
        if (setup->length > EP0_BUF_SIZE)
            break;
        spi_flash_read(addr, ep0_buf, setup->length);
        usb_ep0_in(ep0_buf, setup->length);
        return;
    case VREQ_FLASH_PROGRAM:
        if (setup->length > EP0_BUF_SIZE || (addr & 0xff) || flash_erase_request)
            break;
        if (usb_ep0_out(ep0_buf, setup->length) < 0)
            return;
        for (uint32_t off = 0; off < setup->length; off += SPI_FLASH_PAGE_SIZE) {
            uint32_t n = setup->length - off;
            spi_flash_program_page(addr + off, ep0_buf + off, n > SPI_FLASH_PAGE_SIZE ? SPI_FLASH_PAGE_SIZE : n);
        }
        return;
    case VREQ_FLASH_ERASE:
        if (flash_erase_request)
            break;
        flash_erase_addr    = addr;
        flash_erase_request = 1;
        usb_ep0_ack();
        return;
    case VREQ_FLASH_STATUS:
        /* [0]: operation pending, [4:8]: last FPGA boot status. */
        ep0_buf[0] = flash_erase_request || flash_recover_request || fpga_boot_request;
        ((uint32_t *)ep0_buf)[1] = fpga_boot_status;
        usb_ep0_in(ep0_buf, setup->length < 8 ? setup->length : 8);
        return;
    case VREQ_FLASH_RECOVER:
        flash_recover_request = 1;
        usb_ep0_ack();
        return;
    case VREQ_WATCHDOG:
        fpga_watchdog_config(setup->value);
        usb_ep0_ack();
        return;
    case VREQ_STATS: {
        /* main loops, USB ISRs, SS->USB2 fallbacks, SS connects, PHY CR timeouts, UVC commits,
         * halts, stream starts, stream stops. */
        uint32_t *d = (uint32_t *)ep0_buf;
        d[0] = main_loops;
        d[1] = usb_isr_count;
        for (int i = 0; i < 3; i++)
            d[2 + i] = usb_link_stats[i];
        for (int i = 0; i < 4; i++)
            d[5 + i] = uvc_stats[i];
        usb_ep0_in(ep0_buf, setup->length < 36 ? setup->length : 36);
        return;
    }
    case VREQ_HANG:
        usb_ep0_ack();
        delay_us(1000);
        irq_disable();
        for (;;);
    case VREQ_RANGE:
        uvc_set_range(setup->value & 3);
        usb_ep0_ack();
        return;
    case VREQ_CROP:
        uvc_set_crop(setup->value != 0xffff, setup->value, setup->index);
        usb_ep0_ack();
        return;
    case VREQ_AUDIO_BATCH:
        uvc_audio_set_batch(setup->value);
        usb_ep0_ack();
        return;
    case VREQ_AUDIO_TEST:
        uvc_audio_set_test(setup->value & 1);
        usb_ep0_ack();
        return;
    case VREQ_FPGA_BOOT:
        fpga_boot_request = 1;
        usb_ep0_ack();
        return;
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
        if (!configuration)
            uvc_bus_reset(); /* Deconfigured: stop all streams. */
        if (configuration) {
            usb_enable_in_ep(USB_DESC_EP_STREAM, USB_EP_BULK,
                (speed == USB_SUPER_SPEED) ? 1024 : 512,
                (speed == USB_SUPER_SPEED) ? USB_DESC_SS_BURST : 1);
            usb_enable_in_ep(USB_DESC_EP_AUDIO, USB_EP_ISOCHRONOUS, UAC_PACKET_SIZE, 1);
        }
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
        ep0_buf[0] = ((setup->index & 0xff) == UAC_INTF_STREAMING) ? uvc_audio_get_interface() : 0;
        usb_ep0_in(ep0_buf, 1);
        return;
    case USB_REQ_SET_SEL:
        usb_ep0_out(ep0_buf, 6);
        return;
    case USB_REQ_CLEAR_FEATURE:
        /* ENDPOINT_HALT on the streaming endpoint: stream off. */
        if ((setup->request_type & USB_RECIP_MASK) == USB_RECIP_EP &&
            (setup->index & 0x7f) == USB_DESC_EP_STREAM) {
            uvc_stream_halt();
            usb_reset_in_ep(USB_DESC_EP_STREAM);
        }
        usb_ep0_ack();
        return;
    case USB_REQ_SET_INTERFACE:
        /* Audio streaming interface: alt 1 starts, alt 0 stops the audio stream. */
        if ((setup->index & 0xff) == UAC_INTF_STREAMING) {
            if (setup->value > 1)
                break;
            uvc_audio_set_interface(setup->value);
        }
        usb_ep0_ack();
        return;
    case USB_REQ_SET_FEATURE:
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
    case USB_TYPE_CLASS:
        if (uvc_class_request(setup, ep0_buf) == 0)
            return;
        break;
    }
    usb_ep0_stall();
}

/* Main ------------------------------------------------------------------------------------------ */

int main(void)
{
    cache_enable();
    irq_init();
    /* Boot watchdog: back to the USB bootloader if not configured by the host within 10s. */
    boot_watchdog_start(10*32768);
    gctl_init_clock();
    gctl_init_iomatrix(IOMATRIX_GPIF32BIT_UART_I2S);
    gpio_init_clock();
    fpga_init();
    spi_flash_init();
    fpga_watchdog_init();
    i2c_init(400000);
    /* Standalone boot: LiteCamLink bitstream from flash (if present), then HDMI receiver init
     * (`make NO_FLASH_BOOT=1`: skipped, e.g. to recover from a bad flash bitstream). */
#ifndef NO_FLASH_BOOT
    fpga_boot_status = fpga_boot_from_flash();
#endif
    if ((fpga_boot_status & FPGA_STATUS_DONE) && !(fpga_boot_status & FPGA_STATUS_FAIL))
        hdmi_init_request = 1;
    irq_enable();

    uvc_init();
    usb_init(setup_request);
    usb_connect();

    for (;;) {
        static int boot_watchdog = 1;
        main_loops++;
        if (boot_watchdog && configuration) {
            boot_watchdog_stop();
            boot_watchdog = 0;
        }
        fpga_watchdog_service();
        uvc_service();
        it6802_service();
        if (flash_erase_request) {
            spi_flash_erase_block(flash_erase_addr);
            flash_erase_request = 0;
        }
        if (fpga_boot_request) {
            fpga_boot_status  = fpga_boot_from_flash();
            fpga_boot_request = 0;
        }
        if (flash_recover_request) {
            /* Invalidate the FX3 image: the boot ROM falls back to USB boot. */
            fpga_watchdog_config(0);
            spi_flash_erase_block(FLASH_FX3_IMAGE);
            delay_us(10000);
            gctl_hard_reset();
        }
        if (debug_stream_request) {
            int r;
            irq_disable();
            r = debug_stream_request;
            debug_stream_request = 0;
            irq_enable();
            if (r & 0x100) {
                fpga_payload_config();
                gpif_stream_start(r & 1, (r >> 1) & 1);
            } else {
                gpif_stream_stop();
            }
        }
        if (hdmi_init_request) {
            hdmi_init_request = 0;
            it6802_init();
        }
        if (reboot_request) {
            fpga_watchdog_config(0);
            delay_us(10000);
            gctl_hard_reset();
        }
    }
}
