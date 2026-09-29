/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * Copyright (c) 2018 Marcus Comstedt (fx3lafw)
 * SPDX-License-Identifier: BSD-2-Clause AND MIT
 *
 * FX3 USB device controller: SuperSpeed with High-Speed fallback, EP0 control transfers,
 * IN endpoints configuration. Derived from fx3lafw's bsp/usb.c.
 */

#include "fx3.h"
#include "uvc.h"

#include <rdb/gctl.h>
#include <rdb/uib.h>
#include <rdb/uibin.h>
#include <rdb/vic.h>

static usb_setup_cb   usb_setup_callback;
static enum usb_speed usb_speed = USB_HIGH_SPEED;

enum usb_speed usb_get_speed(void)
{
    return usb_speed;
}

/* USB liveness, polled periodically (~40ms): SuperSpeed link in U0 with the ITP frame counter
 * advancing, or in U1/U2/U3 (low power/suspended), or no VBUS. LTSSM encodings measured on
 * hardware: 0x08-0x0a polling, 0x10 U0, 0x13 U3 (autosuspend), 0x18-0x19 recovery. USB2 mode
 * (SS fallback): not monitored. */
int usb_alive(void)
{
    static uint32_t last_framecnt;
    uint32_t ltssm, framecnt;

    if (!(reg_read(FX3_GCTL_IOPOWER) & FX3_GCTL_IOPOWER_VBUS) || usb_speed != USB_SUPER_SPEED)
        return 1;
    ltssm    = reg_read(FX3_LNK_LTSSM_STATE) & 0x3f;
    framecnt = reg_read(FX3_PROT_FRAMECNT);
    if (ltssm == 0x10) {
        int alive = framecnt != last_framecnt;
        last_framecnt = framecnt;
        return alive;
    }
    return ltssm >= 0x11 && ltssm <= 0x13;
}

/* Helpers --------------------------------------------------------------------------------------- */

static void usb_set_uib_clock(uint32_t pclk_src, uint32_t epmclk_src)
{
    reg_clear(FX3_GCTL_UIB_CORE_CLK, FX3_GCTL_UIB_CORE_CLK_CLK_EN);
    delay_us(5);
    reg_write(FX3_GCTL_UIB_CORE_CLK,
        (pclk_src   << FX3_GCTL_UIB_CORE_CLK_PCLK_SRC_SHIFT) |
        (epmclk_src << FX3_GCTL_UIB_CORE_CLK_EPMCLK_SRC_SHIFT));
    reg_set(FX3_GCTL_UIB_CORE_CLK, FX3_GCTL_UIB_CORE_CLK_CLK_EN);
    delay_us(5);
}

/* Link debug counters: SS -> USB2 fallbacks, SS connects, PHY CR access timeouts. */
volatile uint32_t usb_link_stats[3];

#define USB_PHY_CR_DATA 0xe0033024
#define USB_PHY_CR_STAT 0xe0033028

static int usb_phy_cr_wait(int ack)
{
    /* Bounded: called from the link interrupt, the PHY may not answer while it is powered down. */
    for (uint32_t timeout = 1000; timeout; timeout--) {
        if (!!(reg_read(USB_PHY_CR_STAT) & (1UL << 16)) == ack)
            return 0;
        delay_us(1);
    }
    usb_link_stats[2]++;
    return -1;
}

static void usb_phy_write(uint16_t addr, uint16_t value)
{
    /* USB3 PHY control register access (CR port): capture address, capture data, write. */
    static const uint32_t strobes[3] = {1UL << 16, 1UL << 17, 1UL << 19};

    if (!(reg_read(FX3_OTG_CTRL) & FX3_OTG_CTRL_SSDEV_ENABLE))
        return;

    for (int i = 0; i < 3; i++) {
        uint32_t data = (i == 0) ? addr : value;
        reg_write(USB_PHY_CR_DATA, data);
        reg_write(USB_PHY_CR_DATA, data | strobes[i]);
        if (usb_phy_cr_wait(1))
            return;
        reg_write(USB_PHY_CR_DATA, data);
        if (usb_phy_cr_wait(0))
            return;
    }
}

static void usb_ep0_flush(void)
{
    dma_abort_socket(DMA_UIB_SCK(0));
    dma_abort_socket(DMA_UIBIN_SCK(0));
    reg_set(FX3_EEPM_ENDPOINT, FX3_EEPM_ENDPOINT_SOCKET_FLUSH);
    delay_us(10);
    reg_clear(FX3_EEPM_ENDPOINT, FX3_EEPM_ENDPOINT_SOCKET_FLUSH);
}

/* Connection ------------------------------------------------------------------------------------ */

static void usb_connect_high_speed(void)
{
    usb_speed = USB_HIGH_SPEED;
    usb_link_stats[0]++;

    usb_phy_write(0x1005, 0x0000);

    /* Force the link state machine into SS.Disabled. */
    reg_write(FX3_LNK_LTSSM_STATE, (0UL << 6) | FX3_LNK_LTSSM_STATE_LTSSM_OVERRIDE_EN);

    /* Switch the EPM to USB2 mode, turn off USB3 PHY and remove Rx termination. */
    usb_set_uib_clock(2, 2);
    reg_clear(FX3_OTG_CTRL, FX3_OTG_CTRL_SSDEV_ENABLE);
    delay_us(5);
    reg_clear(FX3_OTG_CTRL, FX3_OTG_CTRL_SSEPM_ENABLE);

    reg_clear(FX3_UIB_INTR_MASK,
        FX3_UIB_INTR_DEV_CTL_INT | FX3_UIB_INTR_DEV_EP_INT  |
        FX3_UIB_INTR_LNK_INT     | FX3_UIB_INTR_PROT_INT    |
        FX3_UIB_INTR_PROT_EP_INT | FX3_UIB_INTR_EPM_URUN);

    reg_write(FX3_LNK_PHY_CONF, reg_read(FX3_LNK_PHY_CONF) & 0x1fffffffUL);
    reg_write(FX3_LNK_PHY_MPLL_STATUS, 0x910410);

    /* Power cycle the PHY blocks. */
    reg_clear(FX3_GCTL_CONTROL, FX3_GCTL_CONTROL_USB_POWER_EN);
    delay_us(5);
    reg_set(FX3_GCTL_CONTROL, FX3_GCTL_CONTROL_USB_POWER_EN);
    delay_us(10);

    /* Clear USB2 interrupts, clear/disable USB3 interrupts. */
    reg_write(FX3_DEV_CTRL_INTR,  ~0UL);
    reg_write(FX3_DEV_EP_INTR,    ~0UL);
    reg_write(FX3_OTG_INTR,       ~0UL);
    reg_write(FX3_LNK_INTR_MASK,  0UL);
    reg_write(FX3_LNK_INTR,       ~0UL);
    reg_write(FX3_PROT_INTR_MASK, 0UL);
    reg_write(FX3_PROT_INTR,      ~0UL);

    reg_set(FX3_UIB_INTR_MASK,
        FX3_UIB_INTR_DEV_CTL_INT | FX3_UIB_INTR_DEV_EP_INT  |
        FX3_UIB_INTR_LNK_INT     | FX3_UIB_INTR_PROT_INT    |
        FX3_UIB_INTR_PROT_EP_INT | FX3_UIB_INTR_EPM_URUN);

    /* Disable USB2 EP0 IN/OUT. */
    reg_clear(FX3_DEV_EPI_CS + 0, FX3_DEV_EPI_CS_VALID);
    reg_clear(FX3_DEV_EPO_CS + 0, FX3_DEV_EPO_CS_VALID);

    reg_write(FX3_EHCI_PORTSC, (1UL << 22));
    reg_write(FX3_DEV_PWR_CS,  (1UL << 2) | (1UL << 3));

    /* Enable USB2 PHY. */
    delay_us(2);
    reg_set(FX3_OTG_CTRL, (1UL << 13));
    delay_us(100);
    reg_write(FX3_PHY_CONF,         0xd4a480UL);
    reg_write(FX3_PHY_CLK_AND_TEST, 0xa0000011UL);
    delay_us(80);

    usb_set_uib_clock(2, 0);

    /* Enable D+ pull-up. */
    reg_clear(FX3_DEV_PWR_CS, (1UL << 3));
}

static void usb_connect_super_speed(void)
{
    usb_speed = USB_SUPER_SPEED;
    usb_link_stats[1]++;

    reg_write(FX3_LNK_PHY_TX_TRIM, 0x0b569011UL);
    usb_phy_write(0x1006, 0x0180);
    usb_phy_write(0x1024, 0x0080);

    /* Switch EPM clock to USB3 mode. */
    usb_set_uib_clock(1, 1);

    reg_write(FX3_LNK_PHY_CONF,
        FX3_LNK_PHY_CONF_RX_TERMINATION_ENABLE  |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR_VAL |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR     |
        (1UL << FX3_LNK_PHY_CONF_PHY_MODE_SHIFT));
    reg_set(FX3_OTG_CTRL, FX3_OTG_CTRL_SSEPM_ENABLE);

    /* Reset EPM mux. */
    reg_set(FX3_IEPM_CS, FX3_IEPM_CS_EPM_MUX_RESET | FX3_IEPM_CS_EPM_FLUSH);
    delay_us(1);
    reg_clear(FX3_IEPM_CS, FX3_IEPM_CS_EPM_MUX_RESET | FX3_IEPM_CS_EPM_FLUSH);
    delay_us(1);

    usb_phy_write(0x0030, 0x00c0);
    usb_phy_write(0x1010, 0x0080);

    reg_write(FX3_EEPM_ENDPOINT + 0, 512UL << FX3_EEPM_ENDPOINT_PACKET_SIZE_SHIFT);
    reg_write(FX3_IEPM_ENDPOINT + 0, 512UL << FX3_IEPM_ENDPOINT_PACKET_SIZE_SHIFT);
}

static void usb_enable_phy(void)
{
    uint32_t vic_enable;

    reg_write(FX3_OTG_INTR, ~0UL);
    reg_write(FX3_LNK_INTR, ~0UL);
    reg_write(FX3_LNK_INTR_MASK,
        FX3_LNK_INTR_MASK_LTSSM_RESET      |
        FX3_LNK_INTR_MASK_LTSSM_DISCONNECT |
        FX3_LNK_INTR_MASK_LTSSM_CONNECT    |
        FX3_LNK_INTR_MASK_LGO_U3           |
        FX3_LNK_INTR_MASK_LTSSM_STATE_CHG);
    reg_write(FX3_PROT_EP_INTR_MASK, 0);
    reg_write(FX3_PROT_INTR, ~0UL);
    reg_write(FX3_PROT_INTR_MASK,
        FX3_PROT_INTR_MASK_STATUS_STAGE        |
        FX3_PROT_INTR_MASK_SUTOK_EN            |
        FX3_PROT_INTR_MASK_TIMEOUT_PORT_CFG_EN |
        FX3_PROT_INTR_MASK_TIMEOUT_PORT_CAP_EN |
        FX3_PROT_INTR_MASK_LMP_PORT_CFG_EN     |
        FX3_PROT_INTR_MASK_LMP_PORT_CAP_EN     |
        FX3_PROT_INTR_MASK_LMP_RCV_EN);
    reg_write(FX3_DEV_CTRL_INTR, ~0UL);
    reg_write(FX3_DEV_CTRL_INTR_MASK,
        FX3_DEV_CTRL_INTR_MASK_URESUME |
        FX3_DEV_CTRL_INTR_MASK_SUDAV   |
        FX3_DEV_CTRL_INTR_MASK_HSGRANT |
        FX3_DEV_CTRL_INTR_MASK_URESET  |
        FX3_DEV_CTRL_INTR_MASK_SUSP);
    reg_write(FX3_DEV_EP_INTR, ~0UL);
    reg_write(FX3_DEV_EP_INTR_MASK, 0);
    reg_write(FX3_UIB_INTR_MASK,
        FX3_UIB_INTR_MASK_PROT_INT |
        FX3_UIB_INTR_MASK_LNK_INT  |
        FX3_UIB_INTR_MASK_DEV_CTL_INT);
    reg_write(FX3_VIC_INT_ENABLE, (1UL << IRQ_USB_CORE));
    reg_set(FX3_GCTL_CONTROL, FX3_GCTL_CONTROL_USB_POWER_EN);

    /* Link setup for SuperSpeed (timings from fx3lafw). */
    reg_write(FX3_LNK_DEVICE_POWER_CONTROL,
        FX3_LNK_DEVICE_POWER_CONTROL_AUTO_U2 |
        FX3_LNK_DEVICE_POWER_CONTROL_AUTO_U1);
    reg_write(0xe003309c, 10000);
    reg_write(0xe0033080, 10000);
    reg_write(0xe0033084, 0x00fa004b);
    reg_write(0xe00330c4, 0x00000177);
    reg_write(0xe0033078, 0x58);
    reg_write(FX3_PROT_LMP_PORT_CAPABILITY_TIMER,    2312);
    reg_write(FX3_PROT_LMP_PORT_CONFIGURATION_TIMER, 2312);
    reg_set(FX3_LNK_COMPLIANCE_PATTERN_8, FX3_LNK_COMPLIANCE_PATTERN_8_LFPS);
    reg_write(FX3_LNK_PHY_CONF,
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR |
        (1UL << FX3_LNK_PHY_CONF_PHY_MODE_SHIFT));

    vic_enable = reg_read(FX3_VIC_INT_ENABLE);
    reg_write(FX3_VIC_INT_CLEAR, ~0UL);
    usb_set_uib_clock(2, 2);
    reg_write(FX3_LNK_LTSSM_STATE,
        (0UL << FX3_LNK_LTSSM_STATE_LTSSM_OVERRIDE_VALUE_SHIFT) |
        FX3_LNK_LTSSM_STATE_LTSSM_OVERRIDE_EN);
    reg_set(FX3_OTG_CTRL, FX3_OTG_CTRL_SSDEV_ENABLE);
    delay_us(100);
    reg_set_field(FX3_LNK_CONF, EPM_FIRST_DELAY, 15UL);
    reg_set(FX3_LNK_CONF, FX3_LNK_CONF_LDN_DETECTION);
    reg_write(FX3_LNK_PHY_MPLL_STATUS,
        FX3_LNK_PHY_MPLL_STATUS_REF_SSP_EN |
        (136UL << FX3_LNK_PHY_MPLL_STATUS_SSC_REF_CLK_SEL_SHIFT) |
        (0UL   << FX3_LNK_PHY_MPLL_STATUS_SSC_RANGE_SHIFT) |
        FX3_LNK_PHY_MPLL_STATUS_SSC_EN |
        (3UL   << FX3_LNK_PHY_MPLL_STATUS_MPLL_MULTIPLIER_SHIFT));
    usb_phy_write(0x30, 0xc0);
    reg_clear(FX3_LNK_LTSSM_STATE, FX3_LNK_LTSSM_STATE_LTSSM_OVERRIDE_EN);
    reg_write(FX3_LNK_PHY_CONF,
        FX3_LNK_PHY_CONF_RX_TERMINATION_ENABLE  |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR_VAL |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR     |
        (1UL << FX3_LNK_PHY_CONF_PHY_MODE_SHIFT));
    delay_us(100);
    reg_write(FX3_GCTL_UIB_CORE_CLK,
        FX3_GCTL_UIB_CORE_CLK_CLK_EN |
        (1UL << FX3_GCTL_UIB_CORE_CLK_PCLK_SRC_SHIFT) |
        (1UL << FX3_GCTL_UIB_CORE_CLK_EPMCLK_SRC_SHIFT));
    reg_write(FX3_VIC_INT_ENABLE, vic_enable);
}

/* Interrupts ------------------------------------------------------------------------------------ */

static uint16_t usb_ep0_wlength; /* wLength of the current control request. */

static void usb_setup_dispatch(uint32_t dat0, uint32_t dat1)
{
    struct usb_setup setup = {
        .request_type = (dat0 >>  0) & 0xff,
        .request      = (dat0 >>  8) & 0xff,
        .value        = (dat0 >> 16) & 0xffff,
        .index        = (dat1 >>  0) & 0xffff,
        .length       = (dat1 >> 16) & 0xffff,
    };
    usb_ep0_wlength = setup.length;
    if (setup.request_type & USB_DIR_IN)
        reg_write(FX3_DEV_EPI_XFER_CNT, setup.length);
    else
        reg_write(FX3_DEV_EPO_XFER_CNT, setup.length);
    usb_setup_callback(&setup, usb_speed);
}

static void usb_prot_isr(void)
{
    uint32_t req = reg_read(FX3_PROT_INTR) & reg_read(FX3_PROT_INTR_MASK);
    reg_write(FX3_PROT_INTR, req);

    if (req & FX3_PROT_INTR_SUTOK_EV) {
        reg_write(FX3_PROT_EPO_CS1 + 0, FX3_PROT_EPO_CS1_VALID);
        reg_write(FX3_PROT_EPI_CS1 + 0, FX3_PROT_EPI_CS1_VALID);
        usb_ep0_flush();
        usb_setup_dispatch(reg_read(FX3_PROT_SETUP_DAT + 0), reg_read(FX3_PROT_SETUP_DAT + 4));
    }
    if (req & FX3_PROT_INTR_LMP_PORT_CFG_EV) {
        reg_write(FX3_PROT_LMP_PORT_CONFIGURATION_TIMER, 0x80008000UL);
        reg_write(FX3_PROT_INTR, FX3_PROT_INTR_TIMEOUT_PORT_CFG_EV);
    }
    if (req & FX3_PROT_INTR_LMP_PORT_CAP_EV) {
        reg_write(FX3_PROT_LMP_PORT_CAPABILITY_TIMER, 0x80008000UL);
        reg_write(FX3_PROT_INTR, FX3_PROT_INTR_TIMEOUT_PORT_CAP_EV);
    }
}

static void usb_lnk_isr(void)
{
    uint32_t req = reg_read(FX3_LNK_INTR) & reg_read(FX3_LNK_INTR_MASK);
    reg_write(FX3_LNK_INTR, req);

    if (req & FX3_LNK_INTR_LTSSM_DISCONNECT) {
        uvc_bus_reset();
        usb_connect_high_speed();
    }
    if (req & FX3_LNK_INTR_LTSSM_CONNECT) {
        uvc_bus_reset();
        usb_connect_super_speed();
    }
}

static void usb_dev_ctl_isr(void)
{
    uint32_t req = reg_read(FX3_DEV_CTRL_INTR) & reg_read(FX3_DEV_CTRL_INTR_MASK);
    reg_write(FX3_DEV_CTRL_INTR, req);

    if (req & FX3_DEV_CTRL_INTR_URESET)
        uvc_bus_reset();
    if (req & FX3_DEV_CTRL_INTR_SUDAV) {
        reg_write(FX3_DEV_EPO_CS + 0, FX3_DEV_EPO_CS_VALID);
        reg_write(FX3_DEV_EPI_CS + 0, FX3_DEV_EPI_CS_VALID);
        usb_ep0_flush();
        usb_setup_dispatch(reg_read(FX3_DEV_SETUPDAT + 0), reg_read(FX3_DEV_SETUPDAT + 4));
    }
}

volatile uint32_t usb_isr_count;

static void __attribute__((isr("IRQ"))) usb_core_isr(void)
{
    usb_isr_count++;
    uint32_t req = reg_read(FX3_UIB_INTR) & reg_read(FX3_UIB_INTR_MASK);
    reg_write(FX3_UIB_INTR, req);

    if (req & FX3_UIB_INTR_PROT_INT)
        usb_prot_isr();
    if (req & FX3_UIB_INTR_LNK_INT)
        usb_lnk_isr();
    if (req & FX3_UIB_INTR_DEV_CTL_INT)
        usb_dev_ctl_isr();

    reg_write(FX3_VIC_ADDRESS, 0);
}

static void __attribute__((isr("IRQ"))) usb_gctl_core_isr(void)
{
    uint32_t req = reg_read(FX3_GCTL_WAKEUP_EVENT) & reg_read(FX3_GCTL_WAKEUP_EN);
    reg_write(FX3_GCTL_WAKEUP_EVENT, req);
    reg_write(FX3_VIC_ADDRESS, 0);
}

static void __attribute__((isr("IRQ"))) usb_gctl_power_isr(void)
{
    uint32_t req = reg_read(FX3_GCTL_IOPOWER_INTR) & reg_read(FX3_GCTL_IOPOWER_INTR_MASK);
    reg_write(FX3_GCTL_IOPOWER_INTR, req);
    reg_write(FX3_VIC_ADDRESS, 0);
}

/* EP0 ------------------------------------------------------------------------------------------- */

void usb_ep0_stall(void)
{
    if (usb_speed == USB_SUPER_SPEED) {
        reg_set(FX3_PROT_EPI_CS1 + 0, FX3_PROT_EPI_CS1_STALL);
        reg_set(FX3_PROT_EPO_CS1 + 0, FX3_PROT_EPO_CS1_STALL);
        delay_us(1);
        reg_set(FX3_PROT_CS, FX3_PROT_CS_SETUP_CLR_BUSY);
    } else {
        reg_set(FX3_DEV_EPI_CS + 0, FX3_DEV_EPI_CS_STALL);
        reg_set(FX3_DEV_EPO_CS + 0, FX3_DEV_EPO_CS_STALL);
        delay_us(1);
        reg_set(FX3_DEV_CS, FX3_DEV_CS_SETUP_CLR_BUSY);
    }
}

void usb_ep0_ack(void)
{
    if (usb_speed == USB_SUPER_SPEED) {
        reg_clear(FX3_PROT_EPI_CS1 + 0, FX3_PROT_EPI_CS1_STALL);
        reg_clear(FX3_PROT_EPO_CS1 + 0, FX3_PROT_EPO_CS1_STALL);
        delay_us(1);
        reg_set(FX3_PROT_CS, FX3_PROT_CS_SETUP_CLR_BUSY);
    } else {
        reg_clear(FX3_DEV_EPI_CS + 0, FX3_DEV_EPI_CS_STALL);
        reg_clear(FX3_DEV_EPO_CS + 0, FX3_DEV_EPO_CS_STALL);
        delay_us(1);
        reg_set(FX3_DEV_CS, FX3_DEV_CS_SETUP_CLR_BUSY);
    }
}

int usb_ep0_in(const volatile void *buffer, uint16_t length)
{
    /* Never return more than requested (wLength). */
    if (length > usb_ep0_wlength)
        length = usb_ep0_wlength;
    usb_ep0_ack();
    return dma_transfer_read(DMA_UIB_SCK(0), buffer, length);
}

int usb_ep0_out(volatile void *buffer, uint16_t length)
{
    usb_ep0_ack();
    return dma_transfer_write(DMA_UIBIN_SCK(0), buffer, length);
}

/* Endpoints ------------------------------------------------------------------------------------- */

void usb_enable_in_ep(uint8_t ep, enum usb_ep_type type, uint16_t pktsize, uint8_t burst)
{
    static const uint8_t usb2_type[] = {
        [USB_EP_ISOCHRONOUS] = 1,
        [USB_EP_INTERRUPT]   = 3,
        [USB_EP_BULK]        = 2,
        [USB_EP_CONTROL]     = 0,
    };

    /* USB3. */
    reg_write(FX3_PROT_EPI_CS1 + (ep << 2), FX3_PROT_EPI_CS1_VALID);
    reg_write(FX3_PROT_EPI_CS2 + (ep << 2),
        (((uint32_t)(burst ? burst - 1 : 0)) << FX3_PROT_EPI_CS2_MAXBURST_SHIFT) |
        (1UL  << FX3_PROT_EPI_CS2_ISOINPKS_SHIFT) |
        ((type << FX3_PROT_EPI_CS2_TYPE_SHIFT) & FX3_PROT_EPI_CS2_TYPE_MASK));

    /* USB2. */
    reg_write(FX3_DEV_EPI_CS + (ep << 2),
        FX3_DEV_EPI_CS_VALID |
        ((type == USB_EP_ISOCHRONOUS) ? (1UL << FX3_DEV_EPI_CS_ISOINPKS_SHIFT) : 0) |
        ((uint32_t)usb2_type[type & 3] << FX3_DEV_EPI_CS_TYPE_SHIFT) |
        ((pktsize << FX3_DEV_EPI_CS_PAYLOAD_SHIFT) & FX3_DEV_EPI_CS_PAYLOAD_MASK));

    /* Endpoint manager. */
    reg_write(FX3_EEPM_ENDPOINT + (ep << 2),
        (pktsize << FX3_EEPM_ENDPOINT_PACKET_SIZE_SHIFT) & FX3_EEPM_ENDPOINT_PACKET_SIZE_MASK);
}

void usb_reset_in_ep(uint8_t ep)
{
    /* Endpoint halt cleared: reset the USB3 sequence number / USB2 data toggle, flush. */
    if (usb_speed == USB_SUPER_SPEED) {
        reg_clear(FX3_PROT_EPI_CS1 + (ep << 2), FX3_PROT_EPI_CS1_STALL);
        reg_set(FX3_PROT_EPI_CS1 + (ep << 2), FX3_PROT_EPI_CS1_EP_RESET);
        delay_us(2);
        /* Sequence number = 0 (IN endpoint). */
        reg_write(FX3_PROT_SEQ_NUM,
            FX3_PROT_SEQ_NUM_COMMAND | FX3_PROT_SEQ_NUM_DIR |
            (0UL << FX3_PROT_SEQ_NUM_SEQUENCE_NUMBER_SHIFT) | ep);
        delay_us(2);
        reg_clear(FX3_PROT_EPI_CS1 + (ep << 2), FX3_PROT_EPI_CS1_EP_RESET);
        /* Clear status bits (OOSERR, ...). */
        reg_write(FX3_PROT_EPI_CS1 + (ep << 2), reg_read(FX3_PROT_EPI_CS1 + (ep << 2)));
    } else {
        uint32_t timeout = 1000;
        reg_clear(FX3_DEV_EPI_CS + (ep << 2), FX3_DEV_EPI_CS_STALL);
        reg_write(FX3_DEV_TOGGLE, ep | FX3_DEV_TOGGLE_IO | FX3_DEV_TOGGLE_R);
        while (timeout-- && !(reg_read(FX3_DEV_TOGGLE) & FX3_DEV_TOGGLE_TOGGLE_VALID))
            delay_us(1);
    }
    usb_flush_in_ep(ep);
}

void usb_flush_in_ep(uint8_t ep)
{
    reg_set(FX3_EEPM_ENDPOINT + (ep << 2), FX3_EEPM_ENDPOINT_SOCKET_FLUSH);
    delay_us(5);
    reg_clear(FX3_EEPM_ENDPOINT + (ep << 2), FX3_EEPM_ENDPOINT_SOCKET_FLUSH);
}

/* Init ------------------------------------------------------------------------------------------ */

void usb_init(usb_setup_cb cb)
{
    uint32_t timeout = 0;

    usb_setup_callback = cb;

    usb_set_uib_clock(2, 2);

    reg_write(FX3_GCTL_CONTROL,
        FX3_GCTL_CONTROL_HARD_RESET_N  |
        FX3_GCTL_CONTROL_CPU_RESET_N   |
        FX3_GCTL_CONTROL_BOOTROM_EN    |
        (1UL << 27)                    |
        FX3_GCTL_CONTROL_MAIN_POWER_EN |
        FX3_GCTL_CONTROL_MAIN_CLOCK_EN |
        FX3_GCTL_CONTROL_WAKEUP_CPU_INT|
        FX3_GCTL_CONTROL_WAKEUP_CLK    |
        FX3_GCTL_CONTROL_POR);

    /* Reset USB block. */
    reg_clear(FX3_UIB_POWER, FX3_UIB_POWER_RESETN);
    delay_us(50);
    reg_set(FX3_UIB_POWER, FX3_UIB_POWER_RESETN);
    delay_us(100);
    while (++timeout < 1000 && !(reg_read(FX3_UIB_POWER) & FX3_UIB_POWER_ACTIVE))
        delay_us(10);

    reg_write(FX3_OTG_CTRL, 0);
    reg_write(FX3_PHY_CLK_AND_TEST,
        (1UL << 31) |
        FX3_PHY_CLK_AND_TEST_SUSPEND_N |
        FX3_PHY_CLK_AND_TEST_VLOAD     |
        FX3_PHY_CLK_AND_TEST_DATABUS16_8);
    reg_set(FX3_DEV_PWR_CS, FX3_DEV_PWR_CS_DISCON);
    reg_write(FX3_GCTL_WAKEUP_EN,       0);
    reg_write(FX3_GCTL_WAKEUP_POLARITY, 0);
    reg_write(FX3_LNK_PHY_CONF,
        FX3_LNK_PHY_CONF_RX_TERMINATION_ENABLE  |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR_VAL |
        FX3_LNK_PHY_CONF_RX_TERMINATION_OVR     |
        (1UL << FX3_LNK_PHY_CONF_PHY_MODE_SHIFT));
    reg_write(FX3_LNK_ERROR_CONF, ~0UL);

    reg_write(FX3_PHY_CONF,
        FX3_PHY_CONF_PREEMDEPTH |
        FX3_PHY_CONF_ENPRE      |
        (1UL << FX3_PHY_CONF_FSRFTSEL_SHIFT)   |
        (1UL << FX3_PHY_CONF_LSRFTSEL_SHIFT)   |
        (2UL << FX3_PHY_CONF_HSTEDVSEL_SHIFT)  |
        (4UL << FX3_PHY_CONF_FSTUNEVSEL_SHIFT) |
        (2UL << FX3_PHY_CONF_HSDEDVSEL_SHIFT)  |
        (4UL << FX3_PHY_CONF_HSDRVSLOPE_SHIFT));

    /* EP0: control on USB3 and USB2. */
    reg_write(FX3_PROT_EPO_CS1 + 0, FX3_PROT_EPO_CS1_VALID);
    reg_write(FX3_PROT_EPI_CS1 + 0, FX3_PROT_EPI_CS1_VALID);
    reg_write(FX3_PROT_EPO_CS2 + 0, (1UL << 2) | (3UL << FX3_PROT_EPO_CS2_TYPE_SHIFT));
    reg_write(FX3_PROT_EPI_CS2 + 0, (1UL << 2) | (3UL << FX3_PROT_EPI_CS2_TYPE_SHIFT));
    reg_write(FX3_DEV_EPI_CS + 0,
        FX3_DEV_EPI_CS_VALID | (64UL << FX3_DEV_EPI_CS_PAYLOAD_SHIFT));
    reg_write(FX3_DEV_EPO_CS + 0,
        FX3_DEV_EPO_CS_VALID | (64UL << FX3_DEV_EPO_CS_PAYLOAD_SHIFT));
    reg_write(FX3_EEPM_ENDPOINT + 0, 64UL << FX3_EEPM_ENDPOINT_PACKET_SIZE_SHIFT);
    reg_write(FX3_IEPM_ENDPOINT + 0, 64UL << FX3_IEPM_ENDPOINT_PACKET_SIZE_SHIFT);

    /* Invalidate other endpoints. */
    for (int i = 1; i < 16; i++) {
        reg_clear(FX3_DEV_EPO_CS + (i << 2), FX3_DEV_EPO_CS_VALID);
        reg_clear(FX3_DEV_EPI_CS + (i << 2), FX3_DEV_EPI_CS_VALID);
        reg_write(FX3_PROT_EPO_CS1 + (i << 2), 0);
        reg_write(FX3_PROT_EPI_CS1 + (i << 2), 0);
    }

    reg_write(FX3_UIB_INTR_MASK, 0);

    /* Vectored interrupts. */
    reg_write(FX3_VIC_VEC_ADDRESS + (IRQ_GCTL_CORE  << 2), (uint32_t)usb_gctl_core_isr);
    reg_write(FX3_VIC_VEC_ADDRESS + (IRQ_USB_CORE   << 2), (uint32_t)usb_core_isr);
    reg_write(FX3_VIC_VEC_ADDRESS + (IRQ_GCTL_POWER << 2), (uint32_t)usb_gctl_power_isr);
    reg_write(FX3_VIC_INT_CLEAR,
        (1UL << IRQ_GCTL_POWER) | (1UL << IRQ_USB_CORE) | (1UL << IRQ_GCTL_CORE));
}

void usb_connect(void)
{
    reg_write(FX3_GCTL_IOPOWER_INTR,      ~0UL);
    reg_write(FX3_GCTL_IOPOWER_INTR_MASK, FX3_GCTL_IOPOWER_INTR_MASK_VBUS);
    reg_write(FX3_VIC_INT_ENABLE, (1UL << IRQ_GCTL_POWER));
    if (reg_read(FX3_GCTL_IOPOWER) & FX3_GCTL_IOPOWER_VBUS)
        usb_enable_phy();
}
