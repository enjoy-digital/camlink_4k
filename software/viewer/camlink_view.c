/*
 * This file is part of CamLink 4K.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * CamLink 4K low latency viewer.
 *
 * uvcvideo (and so every V4L2 application) only delivers complete frames: a pixel at the top of
 * the frame waits for the whole frame to be received. This viewer detaches uvcvideo, starts the
 * stream with a UVC PROBE/COMMIT, reads the bulk payloads with libusb and draws the lines as they
 * arrive (the display races the incoming frame, like a tearing monitor), saving up to one frame.
 *
 * Devices: CamLink 4K (1209:0001) or the stock Elgato firmware (0fd9:0066/0067), both UVC bulk: the
 * streaming interface, endpoint and format/frame indexes are read from the UVC descriptors.
 * Formats: YUY2, and M420 (CamLink 4K: 4K30 direct path, no DRAM frame buffer).
 *
 * --front (YUY2): OpenGL without double buffering, the new rows are converted by a shader and drawn
 * straight into the displayed (front) buffer, no swap: no present queue (tearing, like a beam
 * racing display) when the window is flipped/unredirected.
 *
 * --drm OUTPUT (YUY2): direct scanout on a monitor leased from the X server (turn it off in the
 * desktop first: xrandr --output OUTPUT --off), rows converted straight into the scanned out
 * buffer: no compositor, no swap (drm_out.c).
 *
 * --vk OUTPUT (YUY2): direct display with Vulkan (VK_EXT_acquire_xlib_display, the NVIDIA way:
 * NVIDIA refuses RandR leases): xrandr --output OUTPUT --off --set non-desktop 1 first, shared
 * presentable image (front buffer scanned continuously) or immediate presents (vk_out.c).
 *
 * --latency: decodes the software/source.py barcode (host CLOCK_MONOTONIC ms, top or bottom of the
 * frame) from the incoming YUY2 data and timestamps, per frame: the first payload of the frame,
 * the arrival of the barcode rows and the present showing them (per stage latency, doc/LATENCY.md).
 *
 * Usage: camlink_view [--device auto|camlink|stock] [--format yuy2|m420] [--size WxH] [--fps N]
 *                     [--fullscreen] [--vsync] [--front] [--latency] [--csv file] [--seconds S]
 */

#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libusb.h>
#include <SDL.h>
#include <SDL_syswm.h>
#define GL_GLEXT_PROTOTYPES
#include <SDL_opengl.h>

#include "drm_out.h"
#include "vk_out.h"

/* Device ---------------------------------------------------------------------------------------- */

#define CAMLINK_VID  0x1209
#define CAMLINK_PID  0x0001  /* pid.codes test PID (shared): the product string is checked too. */
#define STOCK_VID    0x0fd9
#define STOCK_PID0   0x0066
#define STOCK_PID1   0x0067

#define UVC_SET_CUR           0x01
#define UVC_GET_CUR           0x81
#define UVC_VS_PROBE_CONTROL  0x01
#define UVC_VS_COMMIT_CONTROL 0x02
#define UVC_PROBE_LEN         26
#define CS_INTERFACE          0x24
#define VS_FORMAT_UNCOMPRESSED 0x04
#define VS_FRAME_UNCOMPRESSED  0x05

#define UVC_HEADER_FID 0x01
#define UVC_HEADER_EOF 0x02
#define UVC_HEADER_ERR 0x40

#define PAYLOAD_SIZE_DEFAULT 32768
#define TRANSFER_BYTES       (128*1024) /* Per libusb transfer (~0.5ms at 1080p60), whole payloads. */
#define TRANSFERS            16

enum { FMT_YUY2, FMT_M420 };

struct stream {
    const char *device;      /* "CamLink 4K" or "stock".                                              */
    int interface, endpoint;
    int format_index, frame_index;
    int format, width, height, fps;
};

/* Latency instrumentation (--latency) ----------------------------------------------------------- */

#define BARCODE_BITS 48
#define BARCODE_COLS 24
#define MAX_SAMPLES  100000

struct sample {
    uint32_t src_ms;         /* Source render time (barcode).                                      */
    int      pos;            /* 0: top, 1: bottom.                                                 */
    double   t_first;        /* First payload of the frame received (host ms).                     */
    double   t_rows;         /* Transfer completing the barcode rows received.                     */
    double   t_present;      /* Present including the barcode rows returned.                       */
    double   t_scan;         /* --drm: scanout of the last barcode row (computed from vblank).     */
};

/* Shared state (USB thread -> display thread) --------------------------------------------------- */

struct viewer {
    struct stream s;
    size_t frame_size;       /* Bytes per frame in the USB stream.                                 */
    size_t payload_size;     /* Negotiated dwMaxPayloadTransferSize.                               */

    /* Current frame, written in place as payloads arrive (the display shows it while it fills). */
    uint8_t *y;              /* YUY2: packed frame. M420: NV12 Y plane.                            */
    uint8_t *uv;             /* M420: NV12 UV plane.                                               */
    size_t   offset;         /* Bytes of the current frame received.                               */
    int      fid;            /* Current frame FID (-1: waiting for a frame start).                 */

    pthread_mutex_t lock;
    int      rows_done;      /* Rows complete in the current frame.                                */
    int      rows_shown;     /* Rows already uploaded to the texture for this frame.               */
    unsigned frame_seq;      /* Incremented at each frame start.                                   */
    unsigned frame_shown;

    /* Latency: barcode decoded by the USB thread, presented by the display thread. */
    int      latency;
    double   t_xfer;         /* Completion time of the transfer being parsed.                      */
    double   t_first;        /* First payload of the current frame.                                */
    int      bar_block, bar_y[2], bar_done;
    struct sample pending;   /* Decoded, waiting for its present.                                  */
    unsigned pending_seq;
    int      pending_valid;
    uint32_t last_src;
    struct sample *samples;
    size_t   nsamples;

    /* Statistics. */
    uint64_t bytes, frames, bad_frames, header_errors;
    int      synced;         /* First frame start seen (the stream may be joined mid-frame).       */
    volatile int running;
    int usb_error;
};

static struct viewer V;

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec*1e3 + ts.tv_nsec*1e-6;
}

/* Barcode decode from the YUY2 luma (same geometry/thresholds as software/source.py). */
static int barcode_decode(int pos, uint64_t *value)
{
    int b = V.bar_block, x0 = b, y0 = V.bar_y[pos], w = V.s.width;
    #define LUMA(px, py) V.y[((size_t)(py)*w + (px))*2]
    for (int i = 0; i < 4; i++)
        if ((LUMA(x0 + i*b + b/2, y0 + 2*b + b/2) > 128) != !(i & 1))
            return 0;
    uint64_t v = 0;
    for (int bit = 0; bit < BARCODE_BITS; bit++) {
        int cx = x0 + (bit%BARCODE_COLS)*b + b/2, cy = y0 + (bit/BARCODE_COLS)*b + b/2;
        long sum = 0, n = 0;
        for (int yy = cy - b/4; yy < cy + b/4; yy++)
            for (int xx = cx - b/4; xx < cx + b/4; xx++) { sum += LUMA(xx, yy); n++; }
        v |= (uint64_t)(sum > 128*n) << bit;
    }
    #undef LUMA
    *value = v;
    return 1;
}

static void latency_rows(int rows)
{
    for (int pos = 0; pos < 2; pos++) {
        if (V.bar_done & (1 << pos) || rows < V.bar_y[pos] + 3*V.bar_block)
            continue;
        V.bar_done |= 1 << pos;
        uint64_t v;
        if (!barcode_decode(pos, &v) || (uint32_t)v == V.last_src)
            continue;
        V.last_src = (uint32_t)v;
        pthread_mutex_lock(&V.lock);
        V.pending = (struct sample){(uint32_t)v, pos, V.t_first, V.t_xfer, 0, 0};
        V.pending_seq   = V.frame_seq;
        V.pending_valid = 1;
        pthread_mutex_unlock(&V.lock);
    }
}

/* Payload data -> frame buffers ----------------------------------------------------------------- */

/* Copies `len` bytes at frame offset `off` to the frame buffers, returns the complete rows. */
static int frame_write(const uint8_t *data, size_t off, size_t len)
{
    size_t w = V.s.width;
    if (off + len > V.frame_size)
        len = off < V.frame_size ? V.frame_size - off : 0;
    if (V.s.format == FMT_YUY2) {
        memcpy(V.y + off, data, len);
        return (off + len)/(w*2);
    }
    /* M420: per row pair, Y row 2k, Y row 2k+1, then the CbCr row k (= NV12 UV row k). */
    size_t group = 3*w;
    while (len) {
        size_t k = off/group, in = off%group, n;
        if (in < 2*w) {
            n = 2*w - in; if (n > len) n = len;
            memcpy(V.y + k*2*w + in, data, n);
        } else {
            n = group - in; if (n > len) n = len;
            memcpy(V.uv + k*w + (in - 2*w), data, n);
        }
        data += n; off += n; len -= n;
    }
    return 2*(int)(off/group);
}

static void frame_start(int fid)
{
    V.fid      = fid;
    V.offset   = 0;
    V.t_first  = V.t_xfer;
    V.bar_done = 0;
    pthread_mutex_lock(&V.lock);
    V.rows_done = 0;
    V.frame_seq++;
    pthread_mutex_unlock(&V.lock);
}

static void on_payload(const uint8_t *p, size_t len)
{
    if (len < 2 || p[0] < 2 || p[0] > len || !(p[1] & 0x80)) {
        V.header_errors++;
        return;
    }
    uint8_t hlen = p[0], info = p[1];
    int fid = info & UVC_HEADER_FID;
    if (info & UVC_HEADER_ERR)
        V.header_errors++;
    if (V.fid < 0 || fid != V.fid) {
        if (V.fid >= 0 && V.offset && V.synced)
            V.bad_frames++; /* FID changed before EOF: incomplete frame. */
        frame_start(fid);
    }
    size_t n = len - hlen;
    if (n) {
        int rows = frame_write(p + hlen, V.offset, n);
        V.offset += n;
        pthread_mutex_lock(&V.lock);
        V.rows_done = rows;
        pthread_mutex_unlock(&V.lock);
        if (V.latency && V.synced)
            latency_rows(rows);
    }
    if (info & UVC_HEADER_EOF) {
        if (V.offset == V.frame_size) V.frames++; else if (V.synced) V.bad_frames++;
        V.synced = 1;
        V.fid    = -1; /* Next payload starts a new frame (whatever its FID). */
    }
}

static void LIBUSB_CALL on_transfer(struct libusb_transfer *t)
{
    if (t->status == LIBUSB_TRANSFER_COMPLETED) {
        size_t len = t->actual_length;
        V.t_xfer = now_ms();
        V.bytes += len;
        for (size_t off = 0; off < len; off += V.payload_size) {
            size_t n = len - off < V.payload_size ? len - off : V.payload_size;
            on_payload(t->buffer + off, n);
        }
    } else if (t->status != LIBUSB_TRANSFER_TIMED_OUT && t->status != LIBUSB_TRANSFER_CANCELLED) {
        V.usb_error = t->status;
        V.running   = 0;
    }
    if (V.running && libusb_submit_transfer(t) < 0)
        V.running = 0;
}

/* USB ------------------------------------------------------------------------------------------- */

static libusb_device_handle *open_device(libusb_context *ctx, const char *which, const char **name)
{
    libusb_device **list;
    libusb_device_handle *found = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    for (int pass = 0; pass < 2 && !found; pass++) { /* auto: CamLink 4K first, then stock. */
        int want_stock = pass == 1;
        if (strcmp(which, "auto") && want_stock != !strcmp(which, "stock"))
            continue;
        for (ssize_t i = 0; i < n && !found; i++) {
            struct libusb_device_descriptor d;
            libusb_device_handle *h;
            unsigned char product[64] = {0};
            if (libusb_get_device_descriptor(list[i], &d))
                continue;
            if (want_stock) {
                if (d.idVendor != STOCK_VID || (d.idProduct != STOCK_PID0 && d.idProduct != STOCK_PID1) || libusb_open(list[i], &h))
                    continue;
                found = h; *name = "stock";
                continue;
            }
            if (d.idVendor != CAMLINK_VID || d.idProduct != CAMLINK_PID || libusb_open(list[i], &h))
                continue;
            if (d.iProduct && libusb_get_string_descriptor_ascii(h, d.iProduct, product, sizeof(product)) > 0 &&
                (!strcmp((char *)product, "CamLink 4K") || !strncmp((char *)product, "CamLinX", 7) || !strcmp((char *)product, "LiteCamLink"))) {
                found = h; *name = "CamLink 4K";
            } else
                libusb_close(h);
        }
    }
    libusb_free_device_list(list, 1);
    return found;
}

/* Streaming interface, bulk endpoint and format/frame indexes from the UVC descriptors. */
static int find_stream(libusb_device_handle *h, struct stream *s)
{
    struct libusb_config_descriptor *cfg;
    const char *fourcc = s->format == FMT_YUY2 ? "YUY2" : "M420";
    if (libusb_get_active_config_descriptor(libusb_get_device(h), &cfg))
        return -1;
    int ok = 0;
    for (int i = 0; i < cfg->bNumInterfaces && !ok; i++) {
        const struct libusb_interface_descriptor *alt = &cfg->interface[i].altsetting[0];
        if (alt->bInterfaceClass != 0x0e || alt->bInterfaceSubClass != 0x02)
            continue;
        s->endpoint = -1;
        for (int e = 0; e < alt->bNumEndpoints; e++) {
            const struct libusb_endpoint_descriptor *ep = &alt->endpoint[e];
            if ((ep->bEndpointAddress & 0x80) && (ep->bmAttributes & 3) == LIBUSB_TRANSFER_TYPE_BULK)
                s->endpoint = ep->bEndpointAddress;
        }
        if (s->endpoint < 0)
            continue;
        s->interface = alt->bInterfaceNumber;
        /* Class specific VS descriptors: format (GUID: FourCC first) then its frames. */
        const uint8_t *p = alt->extra, *end = alt->extra + alt->extra_length;
        int fmt = -1;
        for (; p + 2 < end && p[0] >= 3 && p + p[0] <= end; p += p[0]) {
            if (p[1] != CS_INTERFACE)
                continue;
            if (p[2] == VS_FORMAT_UNCOMPRESSED && p[0] >= 21)
                fmt = !memcmp(&p[5], fourcc, 4) ? p[3] : -1;
            else if (p[2] == VS_FRAME_UNCOMPRESSED && fmt >= 0 && p[0] >= 9 &&
                     (p[5] | p[6] << 8) == s->width && (p[7] | p[8] << 8) == s->height) {
                s->format_index = fmt;
                s->frame_index  = p[3];
                ok = 1;
                break;
            }
        }
    }
    libusb_free_config_descriptor(cfg);
    return ok ? 0 : -1;
}

static int uvc_commit(libusb_device_handle *h, const struct stream *s)
{
    uint8_t probe[UVC_PROBE_LEN] = {0};
    uint32_t interval = 10000000/s->fps;
    probe[2] = s->format_index;
    probe[3] = s->frame_index;
    memcpy(&probe[4], &interval, 4);
    for (int sel = UVC_VS_PROBE_CONTROL; sel <= UVC_VS_COMMIT_CONTROL; sel++)
        if (libusb_control_transfer(h, 0x21, UVC_SET_CUR, sel << 8, s->interface, probe, sizeof(probe), 1000) != sizeof(probe))
            return -1;
    if (libusb_control_transfer(h, 0xa1, UVC_GET_CUR, UVC_VS_COMMIT_CONTROL << 8, s->interface, probe, sizeof(probe), 1000) != sizeof(probe))
        return -1;
    uint32_t payload;
    memcpy(&payload, &probe[22], 4);
    return payload ? (int)payload : PAYLOAD_SIZE_DEFAULT;
}

static void *usb_thread(void *arg)
{
    libusb_context *ctx = arg;
    struct timeval tv = {0, 100000};
    while (V.running)
        libusb_handle_events_timeout_completed(ctx, &tv, NULL);
    return NULL;
}

/* Window activation (X11): an obstructed window is copied (blit) by the compositor instead of
 * flipped; _NET_ACTIVE_WINDOW with a pager source passes the window manager's focus stealing
 * prevention (a plain raise does not when another window has the focus). */
static void activate_window(SDL_Window *win)
{
    SDL_SysWMinfo wm;
    SDL_VERSION(&wm.version);
    SDL_RaiseWindow(win);
    if (!SDL_GetWindowWMInfo(win, &wm) || wm.subsystem != SDL_SYSWM_X11)
        return;
    Display *dpy = wm.info.x11.display;
    XEvent e = {0};
    e.xclient.type         = ClientMessage;
    e.xclient.window       = wm.info.x11.window;
    e.xclient.message_type = XInternAtom(dpy, "_NET_ACTIVE_WINDOW", False);
    e.xclient.format       = 32;
    e.xclient.data.l[0]    = 2; /* Source: pager. */
    e.xclient.data.l[1]    = CurrentTime;
    XSendEvent(dpy, DefaultRootWindow(dpy), False, SubstructureRedirectMask | SubstructureNotifyMask, &e);
    XFlush(dpy);
}

/* Front buffer rendering (--front) ------------------------------------------------------------- */

static const char *front_vs =
    "#version 120\n"
    "attribute vec2 pos; varying vec2 uv;\n"
    "void main() { uv = vec2((pos.x + 1.0)*0.5, (1.0 - pos.y)*0.5); gl_Position = vec4(pos, 0.0, 1.0); }\n";

/* YUY2 texels (RGBA = Y0 U Y1 V) -> RGB, BT.709 limited range. */
static const char *front_fs =
    "#version 120\n"
    "uniform sampler2D tex; uniform float width; varying vec2 uv;\n"
    "void main() {\n"
    "  vec4 t = texture2D(tex, uv);\n"
    "  float y = mod(floor(uv.x*width), 2.0) < 0.5 ? t.r : t.b;\n"
    "  y = (y - 16.0/255.0)*1.164; float u = (t.g - 0.5)*1.138; float v = (t.a - 0.5)*1.138;\n"
    "  gl_FragColor = vec4(y + 1.793*v, y - 0.213*u - 0.533*v, y + 2.112*u, 1.0);\n"
    "}\n";

struct front {
    GLuint tex, prog;
    GLint  pos;
};

static GLuint front_shader(GLenum type, const char *src)
{
    GLuint sh = glCreateShader(type);
    GLint ok;
    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) { char log[512]; glGetShaderInfoLog(sh, sizeof(log), NULL, log); fprintf(stderr, "GLSL: %s\n", log); }
    return sh;
}

static void front_init(struct front *f, int width, int height)
{
    f->prog = glCreateProgram();
    glAttachShader(f->prog, front_shader(GL_VERTEX_SHADER, front_vs));
    glAttachShader(f->prog, front_shader(GL_FRAGMENT_SHADER, front_fs));
    glLinkProgram(f->prog);
    glUseProgram(f->prog);
    f->pos = glGetAttribLocation(f->prog, "pos");
    glUniform1i(glGetUniformLocation(f->prog, "tex"), 0);
    glUniform1f(glGetUniformLocation(f->prog, "width"), width);
    glGenTextures(1, &f->tex);
    glBindTexture(GL_TEXTURE_2D, f->tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, width/2, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glDrawBuffer(GL_FRONT);
    glEnable(GL_SCISSOR_TEST);
}

/* Uploads rows [r0, r1) and draws them (scissored) into the front buffer. */
static void front_draw(struct front *f, SDL_Window *win, int width, int height, int r0, int r1)
{
    static const GLfloat quad[] = {-1, -1, 1, -1, -1, 1, 1, 1};
    int ww, wh;
    SDL_GL_GetDrawableSize(win, &ww, &wh);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, r0, width/2, r1 - r0, GL_RGBA, GL_UNSIGNED_BYTE, V.y + (size_t)r0*width*2);
    int y0 = (int)((long)r0*wh/height), y1 = (int)(((long)r1*wh + height - 1)/height);
    glViewport(0, 0, ww, wh);
    glScissor(0, wh - y1, ww, y1 - y0);
    glVertexAttribPointer(f->pos, 2, GL_FLOAT, GL_FALSE, 0, quad);
    glEnableVertexAttribArray(f->pos);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glFlush();
}

/* Latency report -------------------------------------------------------------------------------- */

static int cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return x < y ? -1 : x > y;
}

/* Wrap-safe difference (ms) between a host time and the 32-bit source ms counter. */
static double since_src(double t, uint32_t src)
{
    return (double)(uint32_t)((uint32_t)(uint64_t)t - src) + (t - (double)(uint64_t)t);
}

static void latency_report(const char *csv)
{
    static const char *pos_name[2] = {"top", "bottom"};
    FILE *f = csv ? fopen(csv, "w") : NULL;
    if (f) fprintf(f, "pos,src_ms,first_ms,rows_ms,present_ms,scanout_ms\n");
    for (int pos = 0; pos < 2; pos++) {
        double *a = malloc(V.nsamples*sizeof(double)), *b = malloc(V.nsamples*sizeof(double));
        double *c = malloc(V.nsamples*sizeof(double)), *d = malloc(V.nsamples*sizeof(double));
        double *e = malloc(V.nsamples*sizeof(double));
        size_t n = 0;
        for (size_t i = 0; i < V.nsamples; i++) {
            struct sample *s = &V.samples[i];
            if (s->pos != pos)
                continue;
            a[n] = since_src(s->t_first, s->src_ms);
            b[n] = since_src(s->t_rows, s->src_ms);
            c[n] = since_src(s->t_present, s->src_ms);
            d[n] = s->t_rows - s->t_first;
            e[n] = s->t_scan > 0 ? since_src(s->t_scan, s->src_ms) : 0;
            if (f) fprintf(f, "%s,%u,%.3f,%.3f,%.3f,%.3f\n", pos_name[pos], s->src_ms, a[n], b[n], c[n], e[n]);
            if (a[n] < 1000 && c[n] < 1000) n++;
        }
        if (n) {
            qsort(a, n, sizeof(double), cmp_double); qsort(b, n, sizeof(double), cmp_double);
            qsort(c, n, sizeof(double), cmp_double); qsort(d, n, sizeof(double), cmp_double);
            qsort(e, n, sizeof(double), cmp_double);
            printf("{\"pos\": \"%s\", \"samples\": %zu, \"first_payload_ms\": %.1f, \"barcode_rows_ms\": %.1f, "
                   "\"present_ms\": %.1f, \"rows_after_first_ms\": %.1f, \"present_p5_ms\": %.1f, \"present_p95_ms\": %.1f",
                pos_name[pos], n, a[n/2], b[n/2], c[n/2], d[n/2], c[n*5/100], c[n*95/100]);
            if (e[n - 1] > 0)
                printf(", \"scanout_ms\": %.1f, \"scanout_p5_ms\": %.1f, \"scanout_p95_ms\": %.1f", e[n/2], e[n*5/100], e[n*95/100]);
            printf("}\n");
        }
        free(a); free(b); free(c); free(d); free(e);
    }
    if (f) fclose(f);
}

/* Main ------------------------------------------------------------------------------------------ */

static void on_signal(int sig) { (void)sig; V.running = 0; }

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [--device auto|camlink|stock] [--format yuy2|m420] [--size WxH] [--fps N]\n"
        "          [--fullscreen] [--vsync] [--front] [--drm OUTPUT] [--vk OUTPUT [--refresh HZ]] [--latency] [--csv file] [--seconds S]\n"
        "  CamLink 4K yuy2: 1920x1080 (default), 1280x720, 640x480 at 30/60 fps\n"
        "  CamLink 4K m420: 3840x2160 at 30 fps, 1920x1080 at 30/60 fps\n"
        "  stock: YUY2 at the input resolution/rate\n", prog);
}

int main(int argc, char **argv)
{
    const char *which = "auto", *csv = NULL, *drm_name = NULL, *vk_name = NULL;
    int fullscreen = 0, vsync = 0, front = 0;
    double seconds = 0, refresh = 0;
    struct stream *s = &V.s;
    s->format = FMT_YUY2; s->width = 1920; s->height = 1080; s->fps = 60;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--device") && i + 1 < argc)       which = argv[++i];
        else if (!strcmp(argv[i], "--format") && i + 1 < argc)  s->format = !strcmp(argv[++i], "m420") ? FMT_M420 : FMT_YUY2;
        else if (!strcmp(argv[i], "--size") && i + 1 < argc) {
            if (sscanf(argv[++i], "%dx%d", &s->width, &s->height) != 2) { usage(argv[0]); return 1; }
        }
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc)     s->fps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fullscreen"))              fullscreen = 1;
        else if (!strcmp(argv[i], "--vsync"))                   vsync = 1;
        else if (!strcmp(argv[i], "--front"))                   front = 1;
        else if (!strcmp(argv[i], "--drm") && i + 1 < argc)     drm_name = argv[++i];
        else if (!strcmp(argv[i], "--vk") && i + 1 < argc)      vk_name = argv[++i];
        else if (!strcmp(argv[i], "--refresh") && i + 1 < argc) refresh = atof(argv[++i]);
        else if (!strcmp(argv[i], "--latency"))                 V.latency = 1;
        else if (!strcmp(argv[i], "--csv") && i + 1 < argc)     csv = argv[++i];
        else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atof(argv[++i]);
        else { usage(argv[0]); return 1; }
    }
    if ((V.latency || front || drm_name || vk_name) && s->format != FMT_YUY2) { fprintf(stderr, "--latency/--front/--drm/--vk need YUY2.\n"); return 1; }
    int direct = drm_name || vk_name; /* No SDL window. */
    int width = s->width, height = s->height;
    V.frame_size = s->format == FMT_YUY2 ? (size_t)width*height*2 : (size_t)width*height*3/2;
    V.fid = -1;
    V.bar_block = width/(BARCODE_COLS + 8);
    V.bar_y[0]  = V.bar_block;
    V.bar_y[1]  = height - 4*V.bar_block;
    V.samples   = calloc(MAX_SAMPLES, sizeof(struct sample));
    pthread_mutex_init(&V.lock, NULL);
    if (s->format == FMT_YUY2) {
        V.y = calloc(V.frame_size, 1);
    } else {
        V.y  = calloc((size_t)width*height, 1);
        V.uv = malloc((size_t)width*height/2);
        memset(V.uv, 128, (size_t)width*height/2);
    }

    /* USB: open, find the streaming interface/format, detach uvcvideo from it, commit the mode. */
    libusb_context *ctx;
    if (libusb_init(&ctx)) { fprintf(stderr, "libusb init failed.\n"); return 1; }
    libusb_device_handle *h = open_device(ctx, which, &s->device);
    if (!h) { fprintf(stderr, "No CamLink 4K (1209:0001) or stock Cam Link 4K (0fd9:0066) device found.\n"); return 1; }
    if (find_stream(h, s)) {
        fprintf(stderr, "%s: no bulk %s %dx%d stream in the UVC descriptors.\n", s->device,
            s->format == FMT_YUY2 ? "YUY2" : "M420", width, height);
        return 1;
    }
    libusb_set_auto_detach_kernel_driver(h, 1);
    int r = libusb_claim_interface(h, s->interface);
    if (r) { fprintf(stderr, "Claim interface failed: %s (device in use?).\n", libusb_error_name(r)); return 1; }
    int payload = uvc_commit(h, s);
    if (payload <= 0) { fprintf(stderr, "UVC commit failed.\n"); return 1; }
    V.payload_size = payload;
    printf("%s: interface %d, endpoint 0x%02x, format %d frame %d, payload %d bytes\n", s->device,
        s->interface, s->endpoint, s->format_index, s->frame_index, payload);

    /* Display: direct scanout (--drm) or an SDL window. */
    struct drm_out dout;
    SDL_Window *win = NULL;
    SDL_Renderer *ren = NULL;
    SDL_Texture  *tex = NULL;
    struct front gl;
    struct vk_out *vout = NULL;
    if (drm_name) {
        if (drm_out_open(&dout, drm_name, width, height))
            return 1;
        goto stream;
    }
    if (vk_name) {
        if (!(vout = vk_out_open(vk_name, width, height, refresh)))
            return 1;
        goto stream;
    }
    SDL_SetHint(SDL_HINT_RENDER_VSYNC, vsync ? "1" : "0");
    if (SDL_Init(SDL_INIT_VIDEO)) { fprintf(stderr, "SDL: %s\n", SDL_GetError()); return 1; }
    char title[128];
    snprintf(title, sizeof(title), "CamLink 4K low latency · %s · %s %dx%d@%d", s->device,
        s->format == FMT_YUY2 ? "YUY2" : "M420", width, height, s->fps);
    if (front) {
        SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 0);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 1);
    }
    win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
        width > 1920 ? width/2 : width, width > 1920 ? height/2 : height,
        SDL_WINDOW_RESIZABLE | (front ? SDL_WINDOW_OPENGL : 0) | (fullscreen ? SDL_WINDOW_FULLSCREEN_DESKTOP : 0));
    if (front) {
        if (!win || !SDL_GL_CreateContext(win)) { fprintf(stderr, "SDL GL: %s\n", SDL_GetError()); return 1; }
        SDL_GL_SetSwapInterval(0);
        front_init(&gl, width, height);
    } else {
        ren = win ? SDL_CreateRenderer(win, -1, SDL_RENDERER_ACCELERATED | (vsync ? SDL_RENDERER_PRESENTVSYNC : 0)) : NULL;
        tex = ren ? SDL_CreateTexture(ren, s->format == FMT_YUY2 ? SDL_PIXELFORMAT_YUY2 : SDL_PIXELFORMAT_NV12,
            SDL_TEXTUREACCESS_STREAMING, width, height) : NULL;
        if (!win || !ren || !tex) { fprintf(stderr, "SDL: %s\n", SDL_GetError()); return 1; }
    }
    activate_window(win);

stream:

    /* Stream: transfers of whole payloads (a short payload ends its transfer). */
    V.running = 1;
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    struct libusb_transfer *xfers[TRANSFERS];
    size_t xfer_size = (TRANSFER_BYTES/V.payload_size ? TRANSFER_BYTES/V.payload_size : 1)*V.payload_size;
    for (int i = 0; i < TRANSFERS; i++) {
        xfers[i] = libusb_alloc_transfer(0);
        libusb_fill_bulk_transfer(xfers[i], h, s->endpoint, malloc(xfer_size), xfer_size, on_transfer, NULL, 2000);
        libusb_submit_transfer(xfers[i]);
    }
    pthread_t thread;
    pthread_create(&thread, NULL, usb_thread, ctx);

    double t_start = now_ms(), tstat = t_start;
    uint64_t last_bytes = 0, last_frames = 0, presents = 0;
    while (V.running) {
        SDL_Event e;
        while (!direct && SDL_PollEvent(&e))
            if (e.type == SDL_QUIT || (e.type == SDL_KEYDOWN && (e.key.keysym.sym == SDLK_q || e.key.keysym.sym == SDLK_ESCAPE)))
                V.running = 0;
            else if (e.type == SDL_KEYDOWN && e.key.keysym.sym == SDLK_f) {
                fullscreen = !fullscreen;
                SDL_SetWindowFullscreen(win, fullscreen ? SDL_WINDOW_FULLSCREEN_DESKTOP : 0);
            }
        if (seconds && now_ms() - t_start > seconds*1e3)
            V.running = 0;

        /* New rows since the last upload (a new frame restarts from the top). */
        pthread_mutex_lock(&V.lock);
        if (V.frame_seq != V.frame_shown) { V.frame_shown = V.frame_seq; V.rows_shown = 0; }
        int r0 = V.rows_shown, r1 = V.rows_done;
        unsigned seq = V.frame_shown;
        V.rows_shown = r1;
        pthread_mutex_unlock(&V.lock);
        if (r1 <= r0) {
            if (direct) { struct timespec ts = {0, 50000}; nanosleep(&ts, NULL); } else SDL_Delay(0);
            continue;
        }
        if (vout) {
            vk_out_write_yuy2(vout, V.y, width, r0, r1);
        } else if (drm_name) {
            drm_out_write_yuy2(&dout, V.y, width, r0, r1);
        } else if (front) {
            front_draw(&gl, win, width, height, r0, r1);
        } else {
            SDL_Rect rect = {0, r0, width, r1 - r0};
            if (s->format == FMT_YUY2)
                SDL_UpdateTexture(tex, &rect, V.y + (size_t)r0*width*2, width*2);
            else
                SDL_UpdateNVTexture(tex, &rect, V.y + (size_t)r0*width, width, V.uv + (size_t)(r0/2)*width, width);
            SDL_RenderCopy(ren, tex, NULL, NULL);
            SDL_RenderPresent(ren);
        }
        presents++;

        /* Latency: the decoded barcode rows of this frame are now presented. */
        if (V.latency) {
            pthread_mutex_lock(&V.lock);
            if (V.pending_valid && V.pending_seq == seq && r1 >= V.bar_y[V.pending.pos] + 3*V.bar_block) {
                V.pending.t_present = now_ms();
                if (drm_name)
                    V.pending.t_scan = drm_out_scanout_ms(&dout, V.bar_y[V.pending.pos] + 3*V.bar_block - 1, V.pending.t_present);
                if (vout)
                    V.pending.t_scan = vk_out_scanout_ms(vout, V.bar_y[V.pending.pos] + 3*V.bar_block - 1, V.pending.t_present);
                if (V.nsamples < MAX_SAMPLES)
                    V.samples[V.nsamples++] = V.pending;
                V.pending_valid = 0;
            }
            pthread_mutex_unlock(&V.lock);
        }

        double t = now_ms();
        if (t - tstat >= 1e3 && !V.latency) {
            printf("%.1f fps, %.1f MB/s, %.0f presents/s, frames %llu (bad %llu), header errors %llu\n",
                (V.frames - last_frames)*1e3/(t - tstat), (V.bytes - last_bytes)/(t - tstat)/1e3, presents*1e3/(t - tstat),
                (unsigned long long)V.frames, (unsigned long long)V.bad_frames, (unsigned long long)V.header_errors);
            fflush(stdout);
            last_frames = V.frames; last_bytes = V.bytes; presents = 0; tstat = t;
        }
    }

    /* Stop: cancel the transfers, halt/clear the endpoint (stream off, as uvcvideo), hand the
     * interface back to uvcvideo. */
    V.running = 0;
    pthread_join(thread, NULL);
    for (int i = 0; i < TRANSFERS; i++)
        libusb_cancel_transfer(xfers[i]);
    struct timeval tv = {0, 200000};
    libusb_handle_events_timeout_completed(ctx, &tv, NULL);
    libusb_clear_halt(h, s->endpoint);
    libusb_release_interface(h, s->interface);
    libusb_close(h);
    libusb_exit(ctx);
    if (V.usb_error)
        fprintf(stderr, "USB transfer error: %s\n", libusb_error_name(V.usb_error));
    if (vout)
        vk_out_close(vout);
    else if (drm_name)
        drm_out_close(&dout);
    else
        SDL_Quit();
    if (V.latency)
        latency_report(csv);
    printf("frames %llu (bad %llu), header errors %llu\n", (unsigned long long)V.frames,
        (unsigned long long)V.bad_frames, (unsigned long long)V.header_errors);
    return V.usb_error ? 1 : 0;
}
