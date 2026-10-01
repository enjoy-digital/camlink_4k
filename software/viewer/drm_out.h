/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Direct scanout output: a monitor leased from the X server (RandR 1.6 lease -> DRM fd), driven
 * with KMS, a single CPU mapped buffer scanned out as is (no compositor, no swap).
 */

#ifndef DRM_OUT_H
#define DRM_OUT_H

#include <stdint.h>

struct drm_out {
    void    *dpy;            /* X Display holding the lease (kept open while leased).              */
    int      fd;             /* Leased DRM fd.                                                     */
    uint32_t crtc, conn, fb, handle, pitch, size;
    uint8_t *map;            /* Scanout buffer (XRGB8888).                                         */
    int      width, height;  /* Mode size.                                                         */
    double   period_ms;      /* Refresh period.                                                    */
    double   line_ms;        /* Scanline period.                                                   */
    int      vstart;         /* Lines from the vblank timestamp to the first active line.          */
};

/* Leases `output` (it must be off in the desktop: xrandr --output NAME --off), sets its preferred
 * mode (or width x height when available). Returns 0 on success. */
int drm_out_open(struct drm_out *d, const char *output, int width, int height);

/* Converts YUY2 rows [r0, r1) (BT.709 limited range) into the scanout buffer. */
void drm_out_write_yuy2(struct drm_out *d, const uint8_t *yuy2, int src_width, int r0, int r1);

/* Host CLOCK_MONOTONIC time (ms) at which the scanout next reaches `row` after `t_ms`. */
double drm_out_scanout_ms(struct drm_out *d, int row, double t_ms);

void drm_out_close(struct drm_out *d);

#endif
