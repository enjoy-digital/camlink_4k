/*
 * This file is part of LiteCamLink.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Host stubs for the FX3 firmware unit tests (test/test_fx3_host.py): the hardware facing
 * functions record what the firmware logic (uvc.c, usb_desc.c) does, the tests drive/inspect
 * them through ctypes.
 */

#include <string.h>

#include "fx3.h"
#include "uvc.h"
#include "gpif.h"
#include "fpga_ctrl.h"
#include "it6802.h"

/* EP0 ------------------------------------------------------------------------------------------- */

uint8_t  stub_ep0_out_data[4096]; /* Data stage of OUT requests (set by the test). */
uint8_t  stub_ep0_in_data[4096];  /* Data stage of IN requests (captured).        */
uint32_t stub_ep0_in_len;
uint32_t stub_ep0_acks;
uint32_t stub_ep0_stalls;

int usb_ep0_in(const volatile void *buffer, uint16_t length)
{
    memcpy(stub_ep0_in_data, (const void *)buffer, length);
    stub_ep0_in_len = length;
    return 0;
}

int usb_ep0_out(volatile void *buffer, uint16_t length)
{
    memcpy((void *)buffer, stub_ep0_out_data, length);
    return 0;
}

void usb_ep0_ack(void)   { stub_ep0_acks++; }
void usb_ep0_stall(void) { stub_ep0_stalls++; }

/* FPGA ------------------------------------------------------------------------------------------ */

#define STUB_LOG 1024

uint32_t stub_csr_addr[STUB_LOG];
uint32_t stub_csr_value[STUB_LOG];
uint32_t stub_csr_count;
uint32_t stub_csr_read_value; /* Returned by fpga_csr_read. */

int fpga_csr_write(uint32_t addr, uint32_t value)
{
    if (stub_csr_count < STUB_LOG) {
        stub_csr_addr[stub_csr_count]  = addr;
        stub_csr_value[stub_csr_count] = value;
    }
    stub_csr_count++;
    return 0;
}

int fpga_csr_read(uint32_t addr, uint32_t *value)
{
    (void)addr;
    *value = stub_csr_read_value;
    return 0;
}

struct fpga_video stub_video;          /* Last video configuration.         */
uint32_t          stub_video_starts;
uint32_t          stub_video_stops;
int32_t           stub_gpif_video = -1; /* Last fpga_gpif_control arguments. */
int32_t           stub_gpif_audio = -1;
int32_t           stub_gpif_batch = -1;
int32_t           stub_audio_enable = -1;
int32_t           stub_audio_test   = -1;

void fpga_stream_start(const struct fpga_video *v) { stub_video = *v; stub_video_starts++; }
void fpga_stream_stop(void)                        { stub_video_stops++; }

void fpga_gpif_control(int video, int audio, int audio_batch)
{
    stub_gpif_video = video;
    stub_gpif_audio = audio;
    stub_gpif_batch = audio_batch;
}

void fpga_audio_control(int enable, int test)
{
    stub_audio_enable = enable;
    stub_audio_test   = test;
}

/* GPIF ------------------------------------------------------------------------------------------ */

uint32_t stub_gpif_starts;
int32_t  stub_gpif_running;
uint32_t stub_thread_restarts[2];
int32_t  stub_gpif_start_video = -1;
int32_t  stub_gpif_start_audio = -1;

void gpif_stream_start(int video, int audio)
{
    stub_gpif_starts++;
    stub_gpif_start_video = video;
    stub_gpif_start_audio = audio;
    stub_gpif_running     = video || audio;
}

void gpif_stream_stop(void) { stub_gpif_running = 0; }
int  gpif_stream_running(void) { return stub_gpif_running; }
void gpif_thread_restart(int thread, int enable) { (void)enable; stub_thread_restarts[thread & 1]++; }
void gpif_stream_status(uint32_t *status) { (void)status; }

/* IT6802 ---------------------------------------------------------------------------------------- */

struct it6802_status stub_hdmi; /* Filled by the test. */

const struct it6802_status *it6802_get_status(void) { return &stub_hdmi; }

/* Timing ---------------------------------------------------------------------------------------- */

void delay_us(uint32_t us) { (void)us; }

uint32_t fpga_hdmi_frame_period(void) { return stub_csr_read_value; }
