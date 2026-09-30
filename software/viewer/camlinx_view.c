/*
 * This file is part of CamLinX.
 *
 * Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
 * SPDX-License-Identifier: BSD-2-Clause
 *
 * CamLinX 4K low latency viewer.
 *
 * uvcvideo (and so every V4L2 application) only delivers complete frames: a pixel at the top of
 * the frame waits for the whole frame to be received. This viewer detaches uvcvideo, starts the
 * stream with a UVC PROBE/COMMIT, reads the bulk payloads with libusb and draws the lines as they
 * arrive (the display races the incoming frame, like a tearing monitor), saving up to one frame.
 *
 * Formats: YUY2 (1920x1080/1280x720/640x480, 30/60 fps) and M420 (3840x2160@30, 1920x1080@30/60,
 * direct path, no DRAM frame buffer).
 *
 * Usage: camlinx_view [--format yuy2|m420] [--size WxH] [--fps N] [--fullscreen] [--vsync]
 */

#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libusb.h>
#include <SDL.h>

/* Device ---------------------------------------------------------------------------------------- */

#define CAMLINX_VID  0x1209
#define CAMLINX_PID  0x0001  /* pid.codes test PID (shared): the product string is checked too. */
#define VS_INTERFACE 1
#define VIDEO_EP     0x81

#define UVC_SET_CUR           0x01
#define UVC_GET_CUR           0x81
#define UVC_VS_PROBE_CONTROL  0x01
#define UVC_VS_COMMIT_CONTROL 0x02
#define UVC_PROBE_LEN         26

#define UVC_HEADER_FID 0x01
#define UVC_HEADER_EOF 0x02
#define UVC_HEADER_ERR 0x40

#define PAYLOAD_SIZE_DEFAULT 32768
#define TRANSFER_PAYLOADS    4       /* Payloads per libusb transfer (128KB): ~0.5ms at 1080p60. */
#define TRANSFERS            16

enum { FMT_YUY2 = 1, FMT_M420 = 2 }; /* UVC format indexes (firmware/fx3/usb_desc.c). */

struct mode {
    int format, width, height, frame_index;
};

static const struct mode modes[] = {
    {FMT_YUY2, 1920, 1080, 1}, {FMT_YUY2, 1280, 720, 2}, {FMT_YUY2, 640, 480, 3},
    {FMT_M420, 3840, 2160, 1}, {FMT_M420, 1920, 1080, 2},
};

/* Shared state (USB thread -> display thread) --------------------------------------------------- */

struct viewer {
    const struct mode *mode;
    size_t frame_size;       /* Bytes per frame in the USB stream.                                 */
    size_t payload_size;     /* Negotiated dwMaxPayloadTransferSize.                               */

    /* Current frame, written in place as payloads arrive (the display shows it while it fills). */
    uint8_t *y;              /* YUY2: packed frame. M420: NV12 Y plane.                            */
    uint8_t *uv;             /* M420: NV12 UV plane.                                               */
    size_t   offset;         /* Bytes of the current frame received.                               */
    int      fid;            /* Current frame FID (-1: waiting for a frame start).                 */

    pthread_mutex_t lock;
    int      rows_done;      /* Rows (YUY2) / row pairs x2 (M420) complete in the current frame.   */
    int      rows_shown;     /* Rows already uploaded to the texture for this frame.               */
    unsigned frame_seq;      /* Incremented at each frame start.                                   */
    unsigned frame_shown;

    /* Statistics. */
    uint64_t bytes, frames, bad_frames, header_errors;
    int      synced;         /* First frame start seen (the stream may be joined mid-frame).       */
    volatile int running;
    int usb_error;
};

static struct viewer V;

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec*1e-9;
}

/* Payload data -> frame buffers ----------------------------------------------------------------- */

/* Copies `len` bytes at frame offset `off` to the frame buffers, returns the complete rows. */
static int frame_write(const uint8_t *data, size_t off, size_t len)
{
    const struct mode *m = V.mode;
    if (off + len > V.frame_size)
        len = off < V.frame_size ? V.frame_size - off : 0;
    if (m->format == FMT_YUY2) {
        memcpy(V.y + off, data, len);
        return (off + len)/(m->width*2);
    }
    /* M420: per row pair, Y row 2k, Y row 2k+1, then the CbCr row k (= NV12 UV row k). */
    size_t w = m->width, group = 3*w;
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
    V.fid    = fid;
    V.offset = 0;
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

static libusb_device_handle *open_camlinx(libusb_context *ctx)
{
    libusb_device **list;
    libusb_device_handle *found = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    for (ssize_t i = 0; i < n && !found; i++) {
        struct libusb_device_descriptor d;
        libusb_device_handle *h;
        unsigned char product[64] = {0};
        if (libusb_get_device_descriptor(list[i], &d) || d.idVendor != CAMLINX_VID || d.idProduct != CAMLINX_PID)
            continue;
        if (libusb_open(list[i], &h))
            continue;
        if (d.iProduct && libusb_get_string_descriptor_ascii(h, d.iProduct, product, sizeof(product)) > 0 &&
            (!strncmp((char *)product, "CamLinX", 7) || !strcmp((char *)product, "LiteCamLink")))
            found = h;
        else
            libusb_close(h);
    }
    libusb_free_device_list(list, 1);
    return found;
}

static int uvc_commit(libusb_device_handle *h, const struct mode *m, int fps)
{
    uint8_t probe[UVC_PROBE_LEN] = {0};
    uint32_t interval = 10000000/fps;
    probe[2] = m->format;
    probe[3] = m->frame_index;
    memcpy(&probe[4], &interval, 4);
    for (int sel = UVC_VS_PROBE_CONTROL; sel <= UVC_VS_COMMIT_CONTROL; sel++)
        if (libusb_control_transfer(h, 0x21, UVC_SET_CUR, sel << 8, VS_INTERFACE, probe, sizeof(probe), 1000) != sizeof(probe))
            return -1;
    if (libusb_control_transfer(h, 0xa1, UVC_GET_CUR, UVC_VS_COMMIT_CONTROL << 8, VS_INTERFACE, probe, sizeof(probe), 1000) != sizeof(probe))
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

/* Main ------------------------------------------------------------------------------------------ */

static void on_signal(int sig) { (void)sig; V.running = 0; }

static void usage(const char *prog)
{
    fprintf(stderr,
        "Usage: %s [--format yuy2|m420] [--size WxH] [--fps N] [--fullscreen] [--vsync]\n"
        "  yuy2: 1920x1080 (default), 1280x720, 640x480 at 30/60 fps\n"
        "  m420: 3840x2160 at 30 fps, 1920x1080 at 30/60 fps\n", prog);
}

int main(int argc, char **argv)
{
    int format = FMT_YUY2, width = 1920, height = 1080, fps = 60, fullscreen = 0, vsync = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--format") && i + 1 < argc) {
            i++;
            format = !strcmp(argv[i], "m420") ? FMT_M420 : FMT_YUY2;
        } else if (!strcmp(argv[i], "--size") && i + 1 < argc) {
            if (sscanf(argv[++i], "%dx%d", &width, &height) != 2) { usage(argv[0]); return 1; }
        } else if (!strcmp(argv[i], "--fps") && i + 1 < argc) {
            fps = atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--fullscreen")) {
            fullscreen = 1;
        } else if (!strcmp(argv[i], "--vsync")) {
            vsync = 1;
        } else {
            usage(argv[0]); return 1;
        }
    }
    for (size_t i = 0; i < sizeof(modes)/sizeof(modes[0]); i++)
        if (modes[i].format == format && modes[i].width == width && modes[i].height == height)
            V.mode = &modes[i];
    if (!V.mode || (fps != 30 && fps != 60) || (format == FMT_M420 && width == 3840 && fps != 30)) {
        fprintf(stderr, "Unsupported mode.\n"); usage(argv[0]); return 1;
    }
    const struct mode *m = V.mode;
    V.frame_size = format == FMT_YUY2 ? (size_t)width*height*2 : (size_t)width*height*3/2;
    V.fid = -1;
    pthread_mutex_init(&V.lock, NULL);
    if (format == FMT_YUY2) {
        V.y = calloc(V.frame_size, 1);
    } else {
        V.y  = calloc((size_t)width*height, 1);
        V.uv = malloc((size_t)width*height/2);
        memset(V.uv, 128, (size_t)width*height/2);
    }

    /* USB: open, detach uvcvideo from the streaming interface, commit the mode. */
    libusb_context *ctx;
    if (libusb_init(&ctx)) { fprintf(stderr, "libusb init failed.\n"); return 1; }
    libusb_device_handle *h = open_camlinx(ctx);
    if (!h) { fprintf(stderr, "CamLinX device not found (1209:0001, \"CamLinX...\" product).\n"); return 1; }
    libusb_set_auto_detach_kernel_driver(h, 1);
    int r = libusb_claim_interface(h, VS_INTERFACE);
    if (r) { fprintf(stderr, "Claim interface failed: %s (device in use?).\n", libusb_error_name(r)); return 1; }
    int payload = uvc_commit(h, m, fps);
    if (payload <= 0) { fprintf(stderr, "UVC commit failed.\n"); return 1; }
    V.payload_size = payload;

    /* Display. */
    SDL_SetHint(SDL_HINT_RENDER_VSYNC, vsync ? "1" : "0");
    if (SDL_Init(SDL_INIT_VIDEO)) { fprintf(stderr, "SDL: %s\n", SDL_GetError()); return 1; }
    char title[96];
    snprintf(title, sizeof(title), "CamLinX 4K low latency · %s %dx%d@%d", format == FMT_YUY2 ? "YUY2" : "M420", width, height, fps);
    SDL_Window *win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
        width > 1920 ? width/2 : width, width > 1920 ? height/2 : height,
        SDL_WINDOW_RESIZABLE | (fullscreen ? SDL_WINDOW_FULLSCREEN_DESKTOP : 0));
    SDL_Renderer *ren = SDL_CreateRenderer(win, -1, SDL_RENDERER_ACCELERATED | (vsync ? SDL_RENDERER_PRESENTVSYNC : 0));
    SDL_Texture *tex = SDL_CreateTexture(ren, format == FMT_YUY2 ? SDL_PIXELFORMAT_YUY2 : SDL_PIXELFORMAT_NV12,
        SDL_TEXTUREACCESS_STREAMING, width, height);
    if (!win || !ren || !tex) { fprintf(stderr, "SDL: %s\n", SDL_GetError()); return 1; }

    /* Stream: transfers of whole payloads, the last payload of a frame is short and ends its transfer. */
    V.running = 1;
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    struct libusb_transfer *xfers[TRANSFERS];
    size_t xfer_size = V.payload_size*TRANSFER_PAYLOADS;
    for (int i = 0; i < TRANSFERS; i++) {
        xfers[i] = libusb_alloc_transfer(0);
        libusb_fill_bulk_transfer(xfers[i], h, VIDEO_EP, malloc(xfer_size), xfer_size, on_transfer, NULL, 2000);
        libusb_submit_transfer(xfers[i]);
    }
    pthread_t thread;
    pthread_create(&thread, NULL, usb_thread, ctx);

    double t0 = now_s(), tstat = t0;
    uint64_t last_bytes = 0, last_frames = 0, presents = 0;
    while (V.running) {
        SDL_Event e;
        while (SDL_PollEvent(&e))
            if (e.type == SDL_QUIT || (e.type == SDL_KEYDOWN && (e.key.keysym.sym == SDLK_q || e.key.keysym.sym == SDLK_ESCAPE)))
                V.running = 0;
            else if (e.type == SDL_KEYDOWN && e.key.keysym.sym == SDLK_f) {
                fullscreen = !fullscreen;
                SDL_SetWindowFullscreen(win, fullscreen ? SDL_WINDOW_FULLSCREEN_DESKTOP : 0);
            }

        /* New rows since the last upload (a new frame restarts from the top). */
        pthread_mutex_lock(&V.lock);
        if (V.frame_seq != V.frame_shown) { V.frame_shown = V.frame_seq; V.rows_shown = 0; }
        int r0 = V.rows_shown, r1 = V.rows_done;
        V.rows_shown = r1;
        pthread_mutex_unlock(&V.lock);
        if (r1 <= r0) {
            SDL_Delay(0);
            continue;
        }
        SDL_Rect rect = {0, r0, width, r1 - r0};
        if (format == FMT_YUY2)
            SDL_UpdateTexture(tex, &rect, V.y + (size_t)r0*width*2, width*2);
        else
            SDL_UpdateNVTexture(tex, &rect, V.y + (size_t)r0*width, width, V.uv + (size_t)(r0/2)*width, width);
        SDL_RenderCopy(ren, tex, NULL, NULL);
        SDL_RenderPresent(ren);
        presents++;

        double t = now_s();
        if (t - tstat >= 1.0) {
            printf("%.1f fps, %.1f MB/s, %.0f presents/s, frames %llu (bad %llu), header errors %llu\n",
                (V.frames - last_frames)/(t - tstat), (V.bytes - last_bytes)/(t - tstat)/1e6, presents/(t - tstat),
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
    libusb_clear_halt(h, VIDEO_EP);
    libusb_release_interface(h, VS_INTERFACE);
    libusb_close(h);
    libusb_exit(ctx);
    if (V.usb_error)
        fprintf(stderr, "USB transfer error: %s\n", libusb_error_name(V.usb_error));
    SDL_Quit();
    return V.usb_error ? 1 : 0;
}
