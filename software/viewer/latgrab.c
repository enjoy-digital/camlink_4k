/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * Display latency probe: polls the barcode region of the screen (XShm) and timestamps the first
 * appearance of each new barcode value (software/source.py: host CLOCK_MONOTONIC ms + frame
 * counter). Source render -> pixels in the displayed frame buffer, for any player showing the
 * capture fullscreen (ffplay, camlink_view...). Monitor scanout/processing not included.
 *
 * Usage: latgrab --x0 X --y0 Y --block B [--seconds S] [--csv file]
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/ipc.h>
#include <sys/shm.h>

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>

#define BARCODE_BITS 48
#define BARCODE_COLS 24

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec*1e3 + ts.tv_nsec*1e-6;
}

static int cmp(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

/* Mean green level of the center half of a block (same decode as source.py barcode_decode). */
static int block_bit(XImage *img, int cx, int cy, int block)
{
    long sum = 0, n = 0;
    for (int y = cy - block/4; y < cy + block/4; y++)
        for (int x = cx - block/4; x < cx + block/4; x++) {
            uint32_t p = XGetPixel(img, x, y);
            sum += (p >> 8) & 0xff; n++;
        }
    return sum > 128*n;
}

static int decode(XImage *img, int block, uint64_t *value)
{
    /* Region origin = barcode x0/y0. Guard blocks (row 3): white/black/white/black. */
    for (int i = 0; i < 4; i++) {
        uint32_t p = XGetPixel(img, i*block + block/2, 2*block + block/2);
        if ((((p >> 8) & 0xff) > 128) != !(i & 1))
            return 0;
    }
    uint64_t v = 0;
    for (int bit = 0; bit < BARCODE_BITS; bit++) {
        int row = bit/BARCODE_COLS, col = bit%BARCODE_COLS;
        v |= (uint64_t)block_bit(img, col*block + block/2, row*block + block/2, block) << bit;
    }
    *value = v;
    return 1;
}

int main(int argc, char **argv)
{
    int x0 = -1, y0 = -1, block = 0;
    double seconds = 20;
    const char *csv = NULL;
    for (int i = 1; i + 1 < argc; i += 2) {
        if      (!strcmp(argv[i], "--x0"))      x0 = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--y0"))      y0 = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--block"))   block = atoi(argv[i + 1]);
        else if (!strcmp(argv[i], "--seconds")) seconds = atof(argv[i + 1]);
        else if (!strcmp(argv[i], "--csv"))     csv = argv[i + 1];
    }
    if (x0 < 0 || y0 < 0 || block <= 0) {
        fprintf(stderr, "Usage: %s --x0 X --y0 Y --block B [--seconds S] [--csv file]\n", argv[0]);
        return 1;
    }

    Display *dpy = XOpenDisplay(NULL);
    if (!dpy || !XShmQueryExtension(dpy)) { fprintf(stderr, "X11/XShm not available.\n"); return 1; }
    int scr = DefaultScreen(dpy), w = BARCODE_COLS*block, h = 3*block;
    XShmSegmentInfo shm;
    XImage *img = XShmCreateImage(dpy, DefaultVisual(dpy, scr), DefaultDepth(dpy, scr), ZPixmap, NULL, &shm, w, h);
    shm.shmid   = shmget(IPC_PRIVATE, img->bytes_per_line*img->height, IPC_CREAT | 0600);
    shm.shmaddr = img->data = shmat(shm.shmid, NULL, 0);
    shm.readOnly = False;
    XShmAttach(dpy, &shm);
    shmctl(shm.shmid, IPC_RMID, NULL);

    size_t cap = 100000, n = 0;
    double *lat = malloc(cap*sizeof(double));
    FILE *f = csv ? fopen(csv, "w") : NULL;
    if (f) fprintf(f, "seen_ms,source_ms,frame,latency_ms\n");
    uint64_t last = ~0ULL;
    long polls = 0, invalid = 0, repeats = 0;
    double t_end = now_ms() + seconds*1e3;
    while (now_ms() < t_end) {
        XShmGetImage(dpy, RootWindow(dpy, scr), img, x0, y0, AllPlanes);
        double t = now_ms();
        polls++;
        uint64_t v;
        if (!decode(img, block, &v)) {
            if (getenv("LATGRAB_DEBUG") && invalid < 8) {
                fprintf(stderr, "invalid: guards");
                for (int i = 0; i < 4; i++)
                    fprintf(stderr, " %06lx", XGetPixel(img, i*block + block/2, 2*block + block/2) & 0xffffff);
                fprintf(stderr, " | block0 %06lx\n", XGetPixel(img, block/2, block/2) & 0xffffff);
            }
            invalid++; usleep(200); continue;
        }
        if (v != last) {
            last = v;
            double src = (double)(v & 0xffffffff);
            /* Wrap-safe difference of the 32-bit ms counters. */
            double l = (double)(((uint32_t)(uint64_t)t - (uint32_t)(v & 0xffffffff)) & 0xffffffff) + (t - (double)(uint64_t)t);
            if (l < 1000 && n < cap) lat[n++] = l;
            if (f) fprintf(f, "%.3f,%.0f,%u,%.3f\n", t, src, (unsigned)(v >> 32), l);
        } else {
            repeats++;
        }
        usleep(200);
    }
    if (f) fclose(f);

    if (!n) { printf("{\"samples\": 0, \"polls\": %ld, \"invalid\": %ld}\n", polls, invalid); return 2; }
    qsort(lat, n, sizeof(double), cmp);
    double sum = 0; for (size_t i = 0; i < n; i++) sum += lat[i];
    printf("{\"samples\": %zu, \"median_ms\": %.1f, \"mean_ms\": %.1f, \"p5_ms\": %.1f, \"p95_ms\": %.1f, "
           "\"min_ms\": %.1f, \"max_ms\": %.1f, \"polls\": %ld, \"invalid\": %ld, \"poll_hz\": %.0f}\n",
        n, lat[n/2], sum/n, lat[n*5/100], lat[n*95/100], lat[0], lat[n - 1], polls, invalid, polls/seconds);
    XShmDetach(dpy, &shm);
    shmdt(shm.shmaddr);
    XCloseDisplay(dpy);
    return 0;
}
