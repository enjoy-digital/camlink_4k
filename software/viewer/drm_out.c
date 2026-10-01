/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Direct scanout output (see drm_out.h).
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>

#include <X11/Xlib.h>
#include <X11/Xlib-xcb.h>
#include <X11/extensions/Xrandr.h>
#include <xcb/xcb.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

#include "drm_out.h"

/* RandR 1.6 lease (libxcb-randr, its header is not installed on every system). */
typedef uint32_t xcb_randr_crtc_t;
typedef uint32_t xcb_randr_output_t;
typedef uint32_t xcb_randr_lease_t;
typedef struct { unsigned int sequence; } xcb_randr_create_lease_cookie_t;
typedef struct {
    uint8_t  response_type;
    uint8_t  nfd;
    uint16_t sequence;
    uint32_t length;
    uint8_t  pad0[24];
} xcb_randr_create_lease_reply_t;
xcb_randr_create_lease_cookie_t xcb_randr_create_lease(xcb_connection_t *c, xcb_window_t window,
    xcb_randr_lease_t lid, uint16_t num_crtcs, uint16_t num_outputs, const xcb_randr_crtc_t *crtcs,
    const xcb_randr_output_t *outputs);
xcb_randr_create_lease_reply_t *xcb_randr_create_lease_reply(xcb_connection_t *c,
    xcb_randr_create_lease_cookie_t cookie, xcb_generic_error_t **e);
int *xcb_randr_create_lease_reply_fds(xcb_connection_t *c, xcb_randr_create_lease_reply_t *reply);

static int lease(struct drm_out *d, const char *name)
{
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) { fprintf(stderr, "drm: no X display.\n"); return -1; }
    Window root = DefaultRootWindow(dpy);
    XRRScreenResources *res = XRRGetScreenResourcesCurrent(dpy, root);
    RROutput output = 0;
    RRCrtc   crtc   = 0;
    for (int i = 0; i < res->noutput && !output; i++) {
        XRROutputInfo *oi = XRRGetOutputInfo(dpy, res, res->outputs[i]);
        if (!strcmp(oi->name, name)) {
            output = res->outputs[i];
            if (oi->crtc) {
                fprintf(stderr, "drm: %s is in use by the desktop (xrandr --output %s --off first).\n", name, name);
                XRRFreeOutputInfo(oi); XRRFreeScreenResources(res); XCloseDisplay(dpy);
                return -1;
            }
            for (int c = 0; c < oi->ncrtc && !crtc; c++) { /* A free CRTC this output can use. */
                XRRCrtcInfo *ci = XRRGetCrtcInfo(dpy, res, oi->crtcs[c]);
                if (!ci->noutput)
                    crtc = oi->crtcs[c];
                XRRFreeCrtcInfo(ci);
            }
        }
        XRRFreeOutputInfo(oi);
    }
    XRRFreeScreenResources(res);
    if (!output || !crtc) { fprintf(stderr, "drm: output %s or a free CRTC not found.\n", name); XCloseDisplay(dpy); return -1; }

    xcb_connection_t *c = XGetXCBConnection(dpy);
    xcb_randr_crtc_t   lc = crtc;
    xcb_randr_output_t lo = output;
    xcb_randr_create_lease_cookie_t cookie = xcb_randr_create_lease(c, root, xcb_generate_id(c), 1, 1, &lc, &lo);
    xcb_generic_error_t *err = NULL;
    xcb_randr_create_lease_reply_t *reply = xcb_randr_create_lease_reply(c, cookie, &err);
    if (!reply) {
        fprintf(stderr, "drm: RandR lease refused (error %d).\n", err ? err->error_code : -1);
        free(err); XCloseDisplay(dpy);
        return -1;
    }
    d->fd  = xcb_randr_create_lease_reply_fds(c, reply)[0];
    d->dpy = dpy;
    free(reply);
    return 0;
}

int drm_out_open(struct drm_out *d, const char *output, int width, int height)
{
    memset(d, 0, sizeof(*d));
    d->fd = -1;
    if (lease(d, output))
        return -1;

    /* The lessee only sees the leased connector/CRTC. */
    drmModeRes *res = drmModeGetResources(d->fd);
    if (!res || res->count_connectors < 1 || res->count_crtcs < 1) { fprintf(stderr, "drm: empty lease.\n"); return -1; }
    drmModeConnector *conn = drmModeGetConnector(d->fd, res->connectors[0]);
    d->conn = conn->connector_id;
    d->crtc = res->crtcs[0];
    drmModeModeInfo *mode = NULL;
    for (int i = 0; i < conn->count_modes; i++) {
        drmModeModeInfo *m = &conn->modes[i];
        if (m->hdisplay == width && m->vdisplay == height && (!mode || m->vrefresh > mode->vrefresh))
            mode = m;
    }
    for (int i = 0; i < conn->count_modes && !mode; i++)
        if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED)
            mode = &conn->modes[i];
    if (!mode) mode = &conn->modes[0];
    d->width     = mode->hdisplay;
    d->height    = mode->vdisplay;
    d->period_ms = (double)mode->htotal*mode->vtotal/mode->clock;
    d->line_ms   = (double)mode->htotal/mode->clock;
    d->vstart    = 0; /* DRM vblank timestamps are taken at the start of the active area. */

    /* One dumb buffer, scanned out as is. */
    struct drm_mode_create_dumb cd = {.width = d->width, .height = d->height, .bpp = 32};
    if (drmIoctl(d->fd, DRM_IOCTL_MODE_CREATE_DUMB, &cd)) { perror("drm: create dumb"); return -1; }
    d->handle = cd.handle; d->pitch = cd.pitch; d->size = cd.size;
    if (drmModeAddFB(d->fd, d->width, d->height, 24, 32, d->pitch, d->handle, &d->fb)) { perror("drm: add fb"); return -1; }
    struct drm_mode_map_dumb md = {.handle = d->handle};
    if (drmIoctl(d->fd, DRM_IOCTL_MODE_MAP_DUMB, &md)) { perror("drm: map dumb"); return -1; }
    d->map = mmap(NULL, d->size, PROT_READ | PROT_WRITE, MAP_SHARED, d->fd, md.offset);
    if (d->map == MAP_FAILED) { perror("drm: mmap"); return -1; }
    memset(d->map, 0, d->size);
    if (drmModeSetCrtc(d->fd, d->crtc, d->fb, 0, 0, &d->conn, 1, mode)) { perror("drm: set crtc"); return -1; }
    printf("drm: %s leased, %dx%d@%.2f Hz (htotal %d, vtotal %d), direct scanout\n", output, d->width, d->height,
        1e3/d->period_ms, mode->htotal, mode->vtotal);
    drmModeFreeConnector(conn);
    drmModeFreeResources(res);
    return 0;
}

static inline uint8_t clip(int v) { return v < 0 ? 0 : v > 255 ? 255 : v; }

void drm_out_write_yuy2(struct drm_out *d, const uint8_t *yuy2, int src_width, int r0, int r1)
{
    int w = src_width < d->width ? src_width : d->width;
    if (r1 > d->height) r1 = d->height;
    for (int y = r0; y < r1; y++) {
        const uint8_t *s = yuy2 + (size_t)y*src_width*2;
        uint32_t *o = (uint32_t *)(d->map + (size_t)y*d->pitch);
        for (int x = 0; x + 1 < w; x += 2, s += 4) {
            int u = s[1] - 128, v = s[3] - 128;
            int ru = 459*v, gu = -55*u - 136*v, bu = 541*u; /* BT.709 limited range, x256. */
            for (int k = 0; k < 2; k++) {
                int c = 298*(s[2*k] - 16) + 128;
                o[x + k] = (uint32_t)clip((c + ru) >> 8) << 16 | (uint32_t)clip((c + gu) >> 8) << 8 | clip((c + bu) >> 8);
            }
        }
    }
}

double drm_out_scanout_ms(struct drm_out *d, int row, double t_ms)
{
    uint64_t seq, ns;
    if (drmCrtcGetSequence(d->fd, d->crtc, &seq, &ns))
        return -1;
    double t0 = ns*1e-6 + (d->vstart + row)*d->line_ms; /* Row scanned in the last frame. */
    while (t0 < t_ms)
        t0 += d->period_ms;
    return t0;
}

void drm_out_close(struct drm_out *d)
{
    if (d->map && d->map != MAP_FAILED) munmap(d->map, d->size);
    if (d->fd >= 0) close(d->fd); /* Ends the lease: the X server gets the output back. */
    if (d->dpy) XCloseDisplay(d->dpy);
}
