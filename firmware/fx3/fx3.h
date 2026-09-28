/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FX3 minimal BSP: register access, clocks, caches, interrupts, DMA, GPIO, USB.
 */

#ifndef FX3_H
#define FX3_H

#include <stdint.h>
#include <stddef.h>

/* Clocks ---------------------------------------------------------------------------------------- */

#define FX3_REF_CLK   19200000UL          /* FSLC[2:0] = 000: 19.2MHz crystal. */
#define FX3_PLL_FBDIV 20
#define FX3_SYS_CLK   (FX3_REF_CLK*FX3_PLL_FBDIV) /* 384MHz. */
#define FX3_CPU_DIV   2
#define FX3_CPU_CLK   (FX3_SYS_CLK/FX3_CPU_DIV)   /* 192MHz. */

/* Register Access ------------------------------------------------------------------------------- */

#define reg_read(a)           (*(volatile uint32_t *)(uintptr_t)(a))
#define reg_write(a, v)       (*(volatile uint32_t *)(uintptr_t)(a) = (uint32_t)(v))
#define reg_set(a, v)         reg_write((a), reg_read(a) |  (v))
#define reg_clear(a, v)       reg_write((a), reg_read(a) & ~(v))
#define reg_set_field(a, f, v) \
    reg_write((a), (reg_read(a) & ~(a ## _ ## f ## _MASK)) | \
        (((v) << (a ## _ ## f ## _SHIFT)) & (a ## _ ## f ## _MASK)))
#define reg_get_field(a, f)   ((reg_read(a) & (a ## _ ## f ## _MASK)) >> (a ## _ ## f ## _SHIFT))

/* Caches ---------------------------------------------------------------------------------------- */

static inline void cache_invalidate_all(void)
{
    __asm__ __volatile__("mcr p15, 0, %0, c7, c7, 0" : : "r"(0));
}

static inline void cache_invalidate_icache(void)
{
    __asm__ __volatile__("mcr p15, 0, %0, c7, c5, 0" : : "r"(0));
}

static inline void cache_clean_dcache(void)
{
    __asm__ __volatile__("1: mrc p15, 0, r15, c7, c10, 3; bne 1b" ::: "cc");
}

static inline void cache_clean_dcache_line(const volatile void *ptr)
{
    __asm__ __volatile__("mcr p15, 0, %0, c7, c10, 1" : : "r"(((uint32_t)ptr) & ~0x1fUL));
}

static inline void cache_invalidate_dcache_line(const volatile void *ptr)
{
    __asm__ __volatile__("mcr p15, 0, %0, c7, c6, 1" : : "r"(((uint32_t)ptr) & ~0x1fUL));
}

void cache_clean_dcache_range(const volatile void *ptr, uint32_t len);
void cache_invalidate_dcache_range(const volatile void *ptr, uint32_t len);
void cache_enable(void);

/* Interrupts ------------------------------------------------------------------------------------ */

enum {
    IRQ_GCTL_CORE  = 0,
    IRQ_GPIF_DMA   = 6,
    IRQ_GPIF_CORE  = 7,
    IRQ_USB_DMA    = 8,
    IRQ_USB_CORE   = 9,
    IRQ_GPIO_CORE  = 19,
    IRQ_GCTL_POWER = 21,
};

static inline void irq_enable(void)
{
    uint32_t cpsr;
    __asm__ __volatile__("mrs %0, cpsr" : "=r"(cpsr));
    __asm__ __volatile__("msr cpsr_c, %0" : : "r"(cpsr & 0x3f));
}

static inline void irq_disable(void)
{
    uint32_t cpsr;
    __asm__ __volatile__("mrs %0, cpsr" : "=r"(cpsr));
    __asm__ __volatile__("msr cpsr_c, %0" : : "r"(cpsr | 0xc0));
}

void irq_init(void);

/* Global Control -------------------------------------------------------------------------------- */

enum {
    IOMATRIX_GPIO               = 0,
    IOMATRIX_GPIO_UART          = 1,
    IOMATRIX_GPIO_SPI           = 2,
    IOMATRIX_GPIO_I2S           = 3,
    IOMATRIX_UART_SPI_I2S       = 4,
    IOMATRIX_GPIF32BIT_UART_I2S = 5,
};

void gctl_init_clock(void);
void gctl_init_iomatrix(uint32_t alt_func);
void gctl_hard_reset(void) __attribute__((noreturn));
void delay_us(uint32_t us);

/* GPIO ------------------------------------------------------------------------------------------ */

#define GPIO_FAST_DIV 2
#define GPIO_SLOW_DIV 48

void gpio_init_clock(void);
void gpio_setup_output(uint8_t num, uint8_t value);
void gpio_setup_input(uint8_t num);
void gpio_set(uint8_t num, uint8_t value);
uint8_t gpio_get(uint8_t num);

/* DMA ------------------------------------------------------------------------------------------- */

#define DMA_LPP_SCK(n)   (0xe0008000 + ((n) << 7))
#define DMA_PIB_SCK(n)   (0xe0018000 + ((n) << 7))
#define DMA_UIB_SCK(n)   (0xe0038000 + ((n) << 7))
#define DMA_UIBIN_SCK(n) (0xe0048000 + ((n) << 7))

#define DMA_SCK_IP(s)    (((s) >> 16) & 0x3fUL)
#define DMA_SCK_NUM(s)   (((s) >>  7) & 0xffUL)

struct dma_descriptor {
    uint32_t buffer;
    uint32_t sync;
    uint32_t chain;
    uint32_t size;
};

/* Descriptors live at the beginning of SRAM (1 <= n < 768). */
#define DMA_DESCRIPTOR(n) ((volatile struct dma_descriptor *)(uintptr_t)(0x40000000 + ((n) << 4)))

uint16_t dma_alloc_descriptor(void);
void dma_free_descriptor(uint16_t d);
void dma_abort_socket(uint32_t socket);
int  dma_transfer_read(uint32_t socket, const volatile void *buffer, uint16_t length);
int  dma_transfer_write(uint32_t socket, volatile void *buffer, uint16_t length);
void dma_fill_through(uint16_t d, uint32_t prod_socket, uint32_t cons_socket,
    volatile void *buffer, uint16_t size, uint16_t wrchain, uint16_t rdchain);
void dma_start_producer(uint32_t socket, uint16_t d);
void dma_start_consumer(uint32_t socket, uint16_t d);

/* USB ------------------------------------------------------------------------------------------- */

#define USB_DIR_IN        0x80
#define USB_TYPE_MASK     0x60
#define USB_TYPE_STANDARD 0x00
#define USB_TYPE_CLASS    0x20
#define USB_TYPE_VENDOR   0x40
#define USB_RECIP_MASK    0x1f
#define USB_RECIP_DEVICE  0x00
#define USB_RECIP_IFACE   0x01
#define USB_RECIP_EP      0x02

enum {
    USB_REQ_GET_STATUS        = 0x00,
    USB_REQ_CLEAR_FEATURE     = 0x01,
    USB_REQ_SET_FEATURE       = 0x03,
    USB_REQ_SET_ADDRESS       = 0x05,
    USB_REQ_GET_DESCRIPTOR    = 0x06,
    USB_REQ_GET_CONFIGURATION = 0x08,
    USB_REQ_SET_CONFIGURATION = 0x09,
    USB_REQ_GET_INTERFACE     = 0x0a,
    USB_REQ_SET_INTERFACE     = 0x0b,
    USB_REQ_SET_SEL           = 0x30,
    USB_REQ_SET_ISOCH_DELAY   = 0x31,
};

enum {
    USB_DT_DEVICE           = 0x01,
    USB_DT_CONFIG           = 0x02,
    USB_DT_STRING           = 0x03,
    USB_DT_INTERFACE        = 0x04,
    USB_DT_ENDPOINT         = 0x05,
    USB_DT_DEVICE_QUALIFIER = 0x06,
    USB_DT_BOS              = 0x0f,
};

enum usb_speed {
    USB_HIGH_SPEED  = 0,
    USB_SUPER_SPEED = 1,
};

enum usb_ep_type {
    USB_EP_ISOCHRONOUS = 0,
    USB_EP_INTERRUPT   = 1,
    USB_EP_BULK        = 2,
    USB_EP_CONTROL     = 3,
};

struct usb_setup {
    uint8_t  request_type;
    uint8_t  request;
    uint16_t value;
    uint16_t index;
    uint16_t length;
};

/* Setup callback, called from interrupt context: must ack (usb_ep0_*) or stall. */
typedef void (*usb_setup_cb)(const struct usb_setup *setup, enum usb_speed speed);

void usb_init(usb_setup_cb cb);
void usb_connect(void);
void usb_ep0_stall(void);
void usb_ep0_ack(void);
int  usb_ep0_in(const volatile void *buffer, uint16_t length);
int  usb_ep0_out(volatile void *buffer, uint16_t length);
void usb_enable_in_ep(uint8_t ep, enum usb_ep_type type, uint16_t pktsize, uint8_t burst);
void usb_flush_in_ep(uint8_t ep);
enum usb_speed usb_get_speed(void);

#endif /* FX3_H */
