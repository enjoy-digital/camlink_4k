/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Direct display output with Vulkan (VK_EXT_acquire_xlib_display): a monitor taken from the X
 * server (the NVIDIA way of VR "direct mode"), presented on a display plane, no compositor.
 * Shared presentable image (front buffer scanned continuously) when supported, else immediate
 * (tearing) presents.
 */

#ifndef VK_OUT_H
#define VK_OUT_H

#include <stdint.h>

struct vk_out;

/* Takes `output` (off in the desktop and non-desktop: xrandr --output NAME --off --set
 * non-desktop 1), width x height mode at the refresh closest to `refresh` (0: the highest).
 * Returns NULL on failure. */
struct vk_out *vk_out_open(const char *output, int width, int height, double refresh);

/* Converts YUY2 rows [r0, r1) and puts them on the display (returns when the GPU copy is done). */
void vk_out_write_yuy2(struct vk_out *o, const uint8_t *yuy2, int src_width, int r0, int r1);

/* Host CLOCK_MONOTONIC time (ms) at which the scanout next reaches `row` after `t_ms`. */
double vk_out_scanout_ms(struct vk_out *o, int row, double t_ms);

/* Present mode in use ("shared" or "immediate"). */
const char *vk_out_mode(struct vk_out *o);

void vk_out_close(struct vk_out *o);

#endif
