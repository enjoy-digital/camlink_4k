/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FX3 minimal BSP: global control, caches, interrupts, GPIO and DMA.
 */

#include "fx3.h"

#include <rdb/gctl.h>
#include <rdb/gpio.h>
#include <rdb/vic.h>
#include <rdb/dma.h>

/* Delay ----------------------------------------------------------------------------------------- */

void delay_us(uint32_t us)
{
    /* Each loop is 4 CPU cycles. */
    uint32_t count = us*(FX3_CPU_CLK/4/1000000);
    __asm__ __volatile__("1: subs %0, %0, #1; bcs 1b" : "=r"(count) : "0"(count) : "cc");
}

/* Global Control -------------------------------------------------------------------------------- */

void gctl_init_clock(void)
{
    /* CPU = SYS/CPU_DIV, DMA/MMIO = CPU/2. */
    reg_write(FX3_GCTL_CPU_CLK_CFG,
        (1UL << FX3_GCTL_CPU_CLK_CFG_MMIO_DIV_SHIFT) |
        (1UL << FX3_GCTL_CPU_CLK_CFG_DMA_DIV_SHIFT)  |
        (3UL << FX3_GCTL_CPU_CLK_CFG_SRC_SHIFT)      |
        ((FX3_CPU_DIV - 1UL) << FX3_GCTL_CPU_CLK_CFG_CPU_DIV_SHIFT));
    delay_us(10);

    /* PLL. */
    if (reg_get_field(FX3_GCTL_PLL_CFG, FBDIV) != FX3_PLL_FBDIV) {
        reg_set_field(FX3_GCTL_PLL_CFG, FBDIV, FX3_PLL_FBDIV);
        delay_us(10);
        while (!(reg_read(FX3_GCTL_PLL_CFG) & FX3_GCTL_PLL_CFG_PLL_LOCK));
        delay_us(10);
    }
}

void gctl_init_iomatrix(uint32_t alt_func)
{
    /* Disable GPIO overrides. */
    reg_write(FX3_GCTL_GPIO_SIMPLE + 0,  0);
    reg_write(FX3_GCTL_GPIO_SIMPLE + 4,  0);
    reg_write(FX3_GCTL_GPIO_COMPLEX + 0, 0);
    reg_write(FX3_GCTL_GPIO_COMPLEX + 4, 0);
    delay_us(1);

    /* Configure IO matrix. */
    reg_write(FX3_GCTL_IOMATRIX,
        (alt_func << FX3_GCTL_IOMATRIX_S1CFG_SHIFT) |
        ((alt_func == IOMATRIX_GPIF32BIT_UART_I2S) ? FX3_GCTL_IOMATRIX_S0CFG : 0));
}

void gctl_hard_reset(void)
{
    /* Reboots through the boot ROM (USB boot when the flash has no valid image). */
    delay_us(5);
    reg_clear(FX3_GCTL_CONTROL, FX3_GCTL_CONTROL_HARD_RESET_N);
    for (;;);
}

/* Caches ---------------------------------------------------------------------------------------- */

void cache_enable(void)
{
    uint32_t creg;
    cache_invalidate_all();
    __asm__ __volatile__("mrc p15, 0, %0, c1, c0, 0" : "=r"(creg));
    creg |= (1UL << 12) | (1UL << 2); /* I-Cache, D-Cache. */
    __asm__ __volatile__("mcr p15, 0, %0, c1, c0, 0" : : "r"(creg));
}

void cache_clean_dcache_range(const volatile void *ptr, uint32_t len)
{
    uint32_t addr = (uint32_t)ptr & ~0x1fUL;
    uint32_t end  = (uint32_t)ptr + len;
    for (; addr < end; addr += 32)
        cache_clean_dcache_line((const volatile void *)addr);
}

void cache_invalidate_dcache_range(const volatile void *ptr, uint32_t len)
{
    uint32_t addr = (uint32_t)ptr & ~0x1fUL;
    uint32_t end  = (uint32_t)ptr + len;
    for (; addr < end; addr += 32)
        cache_invalidate_dcache_line((const volatile void *)addr);
}

/* Interrupts ------------------------------------------------------------------------------------ */

extern void irq_install_vectors(void);

void irq_init(void)
{
    uint32_t creg;

    reg_write(FX3_VIC_INT_CLEAR, ~0UL);
    reg_write(FX3_VIC_INT_SELECT, 0UL);
    irq_install_vectors();
    cache_clean_dcache();
    cache_invalidate_icache();

    /* Vector base at 0 (ITCM). */
    __asm__ __volatile__("mrc p15, 0, %0, c1, c0, 0" : "=r"(creg));
    creg &= ~(1UL << 13);
    __asm__ __volatile__("mcr p15, 0, %0, c1, c0, 0" : : "r"(creg));
}

/* GPIO ------------------------------------------------------------------------------------------ */

void gpio_init_clock(void)
{
    reg_write(FX3_GCTL_GPIO_FAST_CLK,
        FX3_GCTL_GPIO_FAST_CLK_CLK_EN |
        (3UL << FX3_GCTL_GPIO_FAST_CLK_SRC_SHIFT) |
        ((GPIO_FAST_DIV - 1UL) << FX3_GCTL_GPIO_FAST_CLK_DIV_SHIFT));
    delay_us(10);
    reg_write(FX3_GCTL_GPIO_SLOW_CLK,
        FX3_GCTL_GPIO_SLOW_CLK_CLK_EN |
        ((GPIO_SLOW_DIV - 1UL) << FX3_GCTL_GPIO_SLOW_CLK_DIV_SHIFT));
    delay_us(10);
}

static void gpio_setup(uint8_t num, uint32_t config)
{
    /* Simple GPIO override (takes the pin from its IO matrix function). */
    reg_write(FX3_GPIO_SIMPLE + (num << 2), config);
    reg_set(FX3_GCTL_GPIO_SIMPLE + ((num & 32) >> 3), 1UL << (num & 31));
}

void gpio_setup_output(uint8_t num, uint8_t value)
{
    gpio_setup(num,
        FX3_GPIO_SIMPLE_ENABLE       |
        FX3_GPIO_SIMPLE_DRIVE_HI_EN  |
        FX3_GPIO_SIMPLE_DRIVE_LO_EN  |
        (value ? FX3_GPIO_SIMPLE_OUT_VALUE : 0));
}

void gpio_setup_input(uint8_t num)
{
    gpio_setup(num, FX3_GPIO_SIMPLE_ENABLE | FX3_GPIO_SIMPLE_INPUT_EN);
}

void gpio_set(uint8_t num, uint8_t value)
{
    uint32_t addr = FX3_GPIO_SIMPLE + (num << 2);
    reg_write(addr,
        (reg_read(addr) & ~(FX3_GPIO_SIMPLE_INTR | FX3_GPIO_SIMPLE_OUT_VALUE)) |
        (value ? FX3_GPIO_SIMPLE_OUT_VALUE : 0));
}

uint8_t gpio_get(uint8_t num)
{
    return (reg_read(FX3_GPIO_SIMPLE + (num << 2)) & FX3_GPIO_SIMPLE_IN_VALUE) ? 1 : 0;
}

/* DMA ------------------------------------------------------------------------------------------- */

#define DMA_SCK_STATUS_DEFAULT (      \
    FX3_SCK_STATUS_SUSP_TRANS     |   \
    FX3_SCK_STATUS_EN_CONS_EVENTS |   \
    FX3_SCK_STATUS_EN_PROD_EVENTS |   \
    FX3_SCK_STATUS_TRUNCATE)

#define DMA_TIMEOUT_US 100000

static uint16_t dma_first_unallocated = 1;
static uint16_t dma_free_list         = 0;

uint16_t dma_alloc_descriptor(void)
{
    uint16_t d = dma_free_list;
    if (d)
        dma_free_list = DMA_DESCRIPTOR(d)->chain;
    else if (dma_first_unallocated < 768)
        d = dma_first_unallocated++;
    return d;
}

void dma_free_descriptor(uint16_t d)
{
    if (d) {
        DMA_DESCRIPTOR(d)->chain = dma_free_list;
        dma_free_list = d;
    }
}

void dma_abort_socket(uint32_t socket)
{
    reg_clear(socket + FX3_SCK_STATUS, FX3_SCK_STATUS_GO_ENABLE | FX3_SCK_STATUS_WRAPUP);
    reg_write(socket + FX3_SCK_INTR, ~0UL);
    while (reg_read(socket + FX3_SCK_STATUS) & FX3_SCK_STATUS_ENABLED);
}

static void dma_fill_descriptor(uint16_t d, uint32_t buffer, uint32_t sync, uint32_t size,
    uint16_t wrchain, uint16_t rdchain)
{
    volatile struct dma_descriptor *desc = DMA_DESCRIPTOR(d);
    if (!(size & FX3_DSCR_SIZE_BUFFER_SIZE_MASK))
        size |= 1UL << FX3_DSCR_SIZE_BUFFER_SIZE_SHIFT;
    desc->buffer = buffer;
    desc->sync   = sync;
    desc->size   = size;
    desc->chain  =
        ((uint32_t)wrchain << FX3_DSCR_CHAIN_WR_NEXT_DSCR_SHIFT) |
        ((uint32_t)rdchain << FX3_DSCR_CHAIN_RD_NEXT_DSCR_SHIFT);
    cache_clean_dcache_line(desc);
}

static void dma_socket_start(uint32_t socket, uint16_t d, uint32_t status, uint32_t size,
    uint32_t count)
{
    reg_write(socket + FX3_SCK_STATUS, DMA_SCK_STATUS_DEFAULT);
    while (reg_read(socket + FX3_SCK_STATUS) & FX3_SCK_STATUS_ENABLED);
    reg_write(socket + FX3_SCK_STATUS, status);
    reg_write(socket + FX3_SCK_INTR,   ~0UL);
    reg_write(socket + FX3_SCK_DSCR,   (uint32_t)d << FX3_SCK_DSCR_DSCR_NUMBER_SHIFT);
    reg_write(socket + FX3_SCK_SIZE,   size);
    reg_write(socket + FX3_SCK_COUNT,  count);
    reg_set(socket + FX3_SCK_STATUS, FX3_SCK_STATUS_GO_ENABLE);
}

static int dma_socket_wait(uint32_t socket, uint32_t event)
{
    uint32_t timeout = DMA_TIMEOUT_US;
    while (timeout--) {
        uint32_t intr = reg_read(socket + FX3_SCK_INTR);
        if (intr & FX3_SCK_INTR_ERROR)
            return -1;
        if (intr & event)
            return 0;
        delay_us(1);
    }
    return -2;
}

int dma_transfer_read(uint32_t socket, const volatile void *buffer, uint16_t length)
{
    /* CPU -> Socket (consumer). */
    int ret;
    uint16_t d = dma_alloc_descriptor();
    cache_clean_dcache_range(buffer, length);
    dma_fill_descriptor(d, (uint32_t)buffer,
        FX3_DSCR_SYNC_EN_PROD_INT   |
        FX3_DSCR_SYNC_EN_PROD_EVENT |
        (0x3fUL << FX3_DSCR_SYNC_PROD_IP_SHIFT) |
        FX3_DSCR_SYNC_EN_CONS_INT   |
        FX3_DSCR_SYNC_EN_CONS_EVENT |
        (DMA_SCK_IP(socket)  << FX3_DSCR_SYNC_CONS_IP_SHIFT) |
        (DMA_SCK_NUM(socket) << FX3_DSCR_SYNC_CONS_SCK_SHIFT),
        ((uint32_t)length << FX3_DSCR_SIZE_BYTE_COUNT_SHIFT) |
        ((length + 15) & FX3_DSCR_SIZE_BUFFER_SIZE_MASK) |
        FX3_DSCR_SIZE_BUFFER_OCCUPIED,
        0xffff, 0xffff);
    dma_socket_start(socket, d, DMA_SCK_STATUS_DEFAULT | FX3_SCK_STATUS_UNIT, 1, 0);
    ret = dma_socket_wait(socket, FX3_SCK_INTR_CONSUME_EVENT);
    dma_free_descriptor(d);
    return ret;
}

int dma_transfer_write(uint32_t socket, volatile void *buffer, uint16_t length)
{
    /* Socket (producer) -> CPU. */
    int ret;
    uint16_t d = dma_alloc_descriptor();
    cache_clean_dcache_range(buffer, length);
    dma_fill_descriptor(d, (uint32_t)buffer,
        FX3_DSCR_SYNC_EN_PROD_INT   |
        FX3_DSCR_SYNC_EN_PROD_EVENT |
        (DMA_SCK_IP(socket)  << FX3_DSCR_SYNC_PROD_IP_SHIFT) |
        (DMA_SCK_NUM(socket) << FX3_DSCR_SYNC_PROD_SCK_SHIFT) |
        FX3_DSCR_SYNC_EN_CONS_INT   |
        FX3_DSCR_SYNC_EN_CONS_EVENT |
        (0x3fUL << FX3_DSCR_SYNC_CONS_IP_SHIFT),
        (length + 15) & FX3_DSCR_SIZE_BUFFER_SIZE_MASK,
        0xffff, 0xffff);
    dma_socket_start(socket, d, DMA_SCK_STATUS_DEFAULT | FX3_SCK_STATUS_UNIT, 1, 0);
    ret = dma_socket_wait(socket, FX3_SCK_INTR_PRODUCE_EVENT);
    cache_invalidate_dcache_range(buffer, length);
    dma_free_descriptor(d);
    return ret;
}

void dma_fill_through(uint16_t d, uint32_t prod_socket, uint32_t cons_socket,
    volatile void *buffer, uint16_t size, uint16_t wrchain, uint16_t rdchain)
{
    /* Auto channel descriptor: producer socket -> buffer -> consumer socket. */
    dma_fill_descriptor(d, (uint32_t)buffer,
        FX3_DSCR_SYNC_EN_PROD_INT   |
        FX3_DSCR_SYNC_EN_PROD_EVENT |
        (DMA_SCK_IP(prod_socket)  << FX3_DSCR_SYNC_PROD_IP_SHIFT) |
        (DMA_SCK_NUM(prod_socket) << FX3_DSCR_SYNC_PROD_SCK_SHIFT) |
        FX3_DSCR_SYNC_EN_CONS_INT   |
        FX3_DSCR_SYNC_EN_CONS_EVENT |
        (DMA_SCK_IP(cons_socket)  << FX3_DSCR_SYNC_CONS_IP_SHIFT) |
        (DMA_SCK_NUM(cons_socket) << FX3_DSCR_SYNC_CONS_SCK_SHIFT),
        size & FX3_DSCR_SIZE_BUFFER_SIZE_MASK,
        wrchain, rdchain);
}

void dma_start_producer(uint32_t socket, uint16_t d)
{
    dma_socket_start(socket, d,
        FX3_SCK_STATUS_SUSP_TRANS | FX3_SCK_STATUS_EN_PROD_EVENTS | FX3_SCK_STATUS_TRUNCATE, 0, 0);
}

void dma_start_consumer(uint32_t socket, uint16_t d)
{
    dma_socket_start(socket, d,
        FX3_SCK_STATUS_SUSP_TRANS | FX3_SCK_STATUS_EN_CONS_EVENTS | FX3_SCK_STATUS_TRUNCATE, 0, 0);
}
