#!/usr/bin/env python3

#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Minimal V4L2 mmap capture (no dependencies besides numpy): frames + kernel buffer timestamps."""

import os
import mmap
import select
import time
import glob
import fcntl
import struct

import numpy as np

# V4L2 Constants (x86_64 layouts) ------------------------------------------------------------------

V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP            = 1
V4L2_FIELD_NONE             = 1

VIDIOC_S_FMT    = 0xc0d05605
VIDIOC_REQBUFS  = 0xc0145608
VIDIOC_QUERYBUF = 0xc0585609
VIDIOC_QBUF     = 0xc058560f
VIDIOC_DQBUF    = 0xc0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF= 0x40045613
VIDIOC_S_PARM   = 0xc0cc5616

def fourcc(s):
    return struct.unpack("<I", s.encode())[0]

# Helpers ------------------------------------------------------------------------------------------

def find_device(name="CamLinX"):
    """Return the /dev/videoN capture node of the device whose name matches."""
    for node in sorted(glob.glob("/sys/class/video4linux/video*")):
        try:
            dev_name = open(os.path.join(node, "name")).read().strip()
            index    = int(open(os.path.join(node, "index")).read().strip())
        except OSError:
            continue
        if name in dev_name and index == 0:
            return "/dev/" + os.path.basename(node)
    return None

def yuy2_to_rgb(frame, width, height):
    """YUY2 -> RGB (BT.709, limited range)."""
    f = np.frombuffer(frame, dtype=np.uint8)[:width*height*2].reshape(height, width//2, 4).astype(np.float32)
    y = np.stack([f[..., 0], f[..., 2]], axis=-1).reshape(height, width) - 16
    u = np.repeat(f[..., 1] - 128, 2, axis=1)
    v = np.repeat(f[..., 3] - 128, 2, axis=1)
    r = 1.164*y + 1.793*v
    g = 1.164*y - 0.213*u - 0.533*v
    b = 1.164*y + 2.112*u
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)

def m420_to_yuv(frame, width, height):
    """M420 (2 lines of Y, then 1 line of interleaved CbCr) -> Y (h, w), U/V (h/2, w/2) planes."""
    f  = np.frombuffer(frame, dtype=np.uint8)[:width*height*3//2].reshape(height//2, 3, width)
    y  = f[:, :2, :].reshape(height, width)
    uv = f[:, 2, :].reshape(height//2, width//2, 2)
    return y, uv[..., 0], uv[..., 1]

def m420_to_rgb(frame, width, height):
    """M420 -> RGB (BT.709, limited range)."""
    y, u, v = m420_to_yuv(frame, width, height)
    y = y.astype(np.float32) - 16
    u = np.repeat(np.repeat(u.astype(np.float32) - 128, 2, axis=0), 2, axis=1)
    v = np.repeat(np.repeat(v.astype(np.float32) - 128, 2, axis=0), 2, axis=1)
    r = 1.164*y + 1.793*v
    g = 1.164*y - 0.213*u - 0.533*v
    b = 1.164*y + 2.112*u
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255).astype(np.uint8)

def yuy2_luma(frame, width, height):
    f = np.frombuffer(frame, dtype=np.uint8)[:width*height*2].reshape(height, width*2)
    return f[:, 0::2]

# Capture ------------------------------------------------------------------------------------------

class Capture:
    def __init__(self, device=None, width=1920, height=1080, fps=30, pixfmt="YUYV", buffers=4):
        self.device = device or find_device()
        if self.device is None:
            raise RuntimeError("Capture device not found.")
        self.width, self.height = width, height
        self.fd = os.open(self.device, os.O_RDWR)

        # Format.
        fmt = bytearray(208)
        struct.pack_into("<I", fmt, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<IIII", fmt, 8, width, height, fourcc(pixfmt), V4L2_FIELD_NONE)
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)
        w, h, pf = struct.unpack_from("<III", fmt, 8)
        self.sizeimage = struct.unpack_from("<I", fmt, 8 + 20)[0]
        if (w, h) != (width, height):
            raise RuntimeError(f"Format rejected: got {w}x{h}.")

        # Frame rate.
        parm = bytearray(204)
        struct.pack_into("<I", parm, 0, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<II", parm, 4 + 8, 1, fps)
        fcntl.ioctl(self.fd, VIDIOC_S_PARM, parm)

        # Buffers.
        req = bytearray(struct.pack("<III8x", buffers, V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_MEMORY_MMAP))
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, req)
        count = struct.unpack_from("<I", req, 0)[0]
        self.maps = []
        for i in range(count):
            buf = self._buffer(i)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, buf)
            offset = struct.unpack_from("<I", buf, 64)[0]
            length = struct.unpack_from("<I", buf, 72)[0]
            self.maps.append(mmap.mmap(self.fd, length, mmap.MAP_SHARED, mmap.PROT_READ, offset=offset))
            fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
        self.streaming = False

    def _buffer(self, index):
        buf = bytearray(88)
        struct.pack_into("<II", buf, 0, index, V4L2_BUF_TYPE_VIDEO_CAPTURE)
        struct.pack_into("<I", buf, 60, V4L2_MEMORY_MMAP)
        return buf

    def start(self):
        self.t_start = time.monotonic()
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE))
        self.streaming = True

    def read(self, copy=True, timeout=2.0):
        """Return (data, buffer_ts, sequence, dequeue_ts, bytesused, flags); timestamps in seconds
        (CLOCK_MONOTONIC). Raises TimeoutError when no frame arrives within timeout."""
        if not select.select([self.fd], [], [], timeout)[0]:
            raise TimeoutError("No frame.")
        buf = self._buffer(0)
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf)
        t_dq = time.monotonic()
        index, _, bytesused, flags = struct.unpack_from("<IIII", buf, 0)
        sec, usec = struct.unpack_from("<qq", buf, 24)
        seq = struct.unpack_from("<I", buf, 56)[0]
        data = bytes(self.maps[index][:bytesused]) if copy else None
        fcntl.ioctl(self.fd, VIDIOC_QBUF, buf)
        return data, sec + usec*1e-6, seq, t_dq, bytesused, flags

    def stop(self):
        if self.streaming:
            fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, struct.pack("<I", V4L2_BUF_TYPE_VIDEO_CAPTURE))
            self.streaming = False

    def close(self):
        self.stop()
        for m in self.maps:
            m.close()
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
