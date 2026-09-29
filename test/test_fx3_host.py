#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""FX3 firmware unit tests on the host: uvc.c + usb_desc.c compiled for x86 with the hardware
facing functions stubbed (test/fx3/stubs.c), driven through ctypes."""

import os
import re
import ctypes
import struct
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW   = os.path.join(ROOT, "firmware", "fx3")

# Library ------------------------------------------------------------------------------------------

class Setup(ctypes.Structure):
    _fields_ = [("request_type", ctypes.c_uint8), ("request", ctypes.c_uint8),
        ("value", ctypes.c_uint16), ("index", ctypes.c_uint16), ("length", ctypes.c_uint16)]

class HDMIStatus(ctypes.Structure):
    _fields_ = [("present", ctypes.c_uint8), ("sys_status", ctypes.c_uint8), ("hpd", ctypes.c_uint8),
        ("stable", ctypes.c_uint8), ("htotal", ctypes.c_uint16), ("hactive", ctypes.c_uint16),
        ("vtotal", ctypes.c_uint16), ("vactive", ctypes.c_uint16), ("pclk_reg", ctypes.c_uint8),
        ("video_mode", ctypes.c_uint8), ("colorspace", ctypes.c_uint8), ("quant_range", ctypes.c_uint8),
        ("generation", ctypes.c_uint32), ("frame_period", ctypes.c_uint32)]

class Video(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint16), ("height", ctypes.c_uint16), ("fps", ctypes.c_uint32),
        ("hdmi", ctypes.c_uint8), ("ddr", ctypes.c_uint8), ("downscale", ctypes.c_uint8),
        ("crop", ctypes.c_uint8), ("crop_x", ctypes.c_uint16), ("crop_y", ctypes.c_uint16),
        ("c_swap", ctypes.c_uint8), ("m420", ctypes.c_uint8), ("no_signal", ctypes.c_uint8),
        ("canvas", ctypes.c_uint8), ("rgb", ctypes.c_uint8), ("full_range", ctypes.c_uint8),
        ("in_width", ctypes.c_uint16), ("in_height", ctypes.c_uint16)]

PROBE_FMT = "<HBBIHHHHHIIIBBBB" # UVC 1.1 probe/commit (34 bytes).

UVC_SET_CUR, UVC_GET_CUR, UVC_GET_MIN, UVC_GET_MAX = 0x01, 0x81, 0x82, 0x83
UVC_GET_RES, UVC_GET_LEN, UVC_GET_INFO, UVC_GET_DEF = 0x84, 0x85, 0x86, 0x87

@pytest.fixture(scope="module")
def build(tmp_path_factory):
    if not os.path.exists(os.path.join(FW, "generated", "fpga_csr.h")):
        r = subprocess.run(["make", "-C", FW, "generated/fpga_csr.h", "generated/edid.h"], capture_output=True)
        if r.returncode:
            pytest.skip("generated/fpga_csr.h not available (build the gateware first).")
    lib = str(tmp_path_factory.mktemp("fx3") / "libfx3host.so")
    subprocess.run(["gcc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        "-I" + FW, "-o", lib,
        os.path.join(FW, "uvc.c"), os.path.join(FW, "usb_desc.c"),
        os.path.join(ROOT, "test", "fx3", "stubs.c")], check=True)
    csr = {}
    for m in re.finditer(r"#define (CSR_\w+)\s+(0x[0-9a-fA-F]+|\d+)", open(os.path.join(FW, "generated", "fpga_csr.h")).read()):
        csr[m.group(1)] = int(m.group(2), 0)
    return lib, csr

class FX3:
    def __init__(self, lib, csr):
        # Fresh library instance per test (static state reset).
        self.lib = ctypes.CDLL(lib, mode=os.RTLD_LOCAL)
        self.csr = csr
        self.buf = (ctypes.c_uint8*4096)()
        self.lib.uvc_init()

    def var(self, name, ctype=ctypes.c_uint32):
        return ctype.in_dll(self.lib, name)

    @property
    def hdmi(self):
        return HDMIStatus.in_dll(self.lib, "stub_hdmi")

    @property
    def video(self):
        return Video.in_dll(self.lib, "stub_video")

    def set_input(self, w=0, h=0, stable=True, colorspace=0, quant_range=0):
        s = self.hdmi
        s.stable, s.hactive, s.vactive, s.colorspace, s.quant_range = int(stable), w, h, colorspace, quant_range
        s.generation += 1

    def request(self, request_type, request, value, index, length, data=b""):
        """Class request: returns IN data (bytes) or b"" (OUT), None when stalled (-1)."""
        out = (ctypes.c_uint8*4096).in_dll(self.lib, "stub_ep0_out_data")
        ctypes.memmove(out, data, len(data))
        self.var("stub_ep0_in_len").value = 0
        setup = Setup(request_type, request, value, index, length)
        r = self.lib.uvc_class_request(ctypes.byref(setup), self.buf)
        if r != 0:
            return None
        n = self.var("stub_ep0_in_len").value
        return bytes((ctypes.c_uint8*4096).in_dll(self.lib, "stub_ep0_in_data")[:n])

    # UVC helpers.
    def vs(self, request, selector, data=b"", length=34):
        rt = 0x21 if request == UVC_SET_CUR else 0xa1
        return self.request(rt, request, selector << 8, 1, length, data)

    def commit(self, fmt, frame, fps):
        probe = struct.pack(PROBE_FMT, 0, fmt, frame, 10000000//fps, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1)
        self.vs(UVC_SET_CUR, 1, probe)
        got = struct.unpack(PROBE_FMT, self.vs(UVC_GET_CUR, 1))
        self.vs(UVC_SET_CUR, 2, probe)
        self.lib.uvc_service()
        return got

    def unit(self, request, entity, selector, data=b"", length=2):
        rt = 0x21 if request == UVC_SET_CUR else 0xa1
        return self.request(rt, request, selector << 8, (entity << 8) | 0, length, data)

    def csr_writes(self):
        n = min(self.var("stub_csr_count").value, 1024)
        addrs  = (ctypes.c_uint32*1024).in_dll(self.lib, "stub_csr_addr")
        values = (ctypes.c_uint32*1024).in_dll(self.lib, "stub_csr_value")
        return [(addrs[i], values[i]) for i in range(n)]

@pytest.fixture
def fx3(build):
    lib, csr = build
    # dlopen caches by path: copy the library per test for a fresh state.
    import shutil, tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "libfx3host.so")
    shutil.copy(lib, path)
    return FX3(path, csr)

# Probe / Commit -----------------------------------------------------------------------------------

def test_probe_defaults(fx3):
    for req in (UVC_GET_MIN, UVC_GET_DEF, UVC_GET_MAX):
        p = struct.unpack(PROBE_FMT, fx3.vs(req, 1))
        assert p[1] == 1 and p[2] == 1                       # YUY2 1080p.
        assert p[9] == 1920*1080*2                           # dwMaxVideoFrameSize.
    assert fx3.vs(UVC_GET_LEN, 1, length=2) == bytes([34, 0])
    assert fx3.vs(UVC_GET_INFO, 1, length=1) == bytes([3])

@pytest.mark.parametrize("fmt, frame, fps, expected", [
    (1, 1, 60, (1, 1, 60, 1920*1080*2)),
    (1, 2, 30, (1, 2, 30, 1280*720*2)),
    (1, 9, 60, (1, 1, 60, 1920*1080*2)),     # Invalid frame -> 1.
    (2, 1, 60, (2, 1, 30, 3840*2160*3//2)),  # M420 4K: 30 fps only.
    (2, 2, 60, (2, 2, 60, 1920*1080*3//2)),
    (5, 1, 30, (1, 1, 30, 1920*1080*2)),     # Invalid format -> 1.
])
def test_probe_fixup(fx3, fmt, frame, fps, expected):
    probe = struct.pack(PROBE_FMT, 0, fmt, frame, 10000000//fps, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1)
    fx3.vs(UVC_SET_CUR, 1, probe)
    p = struct.unpack(PROBE_FMT, fx3.vs(UVC_GET_CUR, 1))
    assert (p[1], p[2], 10000000//p[3], p[9]) == expected

# Video source policy ------------------------------------------------------------------------------

@pytest.mark.parametrize("inp, fmt, frame, crop, expected", [
    # (input w, h, stable), format, frame, crop (x, y) or None, expected fpga_video fields.
    ((1920, 1080, True), 1, 1, None, dict(hdmi=1, downscale=0, crop=0, canvas=0, m420=0)),
    ((3840, 2160, True), 1, 1, None, dict(hdmi=1, downscale=1, crop=0, canvas=0)),
    ((3840, 2160, True), 1, 1, (100, 50), dict(hdmi=1, downscale=0, crop=1, crop_x=100, crop_y=50, canvas=0)),
    ((3840, 2160, True), 1, 1, (101, 50), dict(hdmi=1, crop=1, crop_x=100, crop_y=50)),      # X even.
    ((3840, 2160, True), 1, 1, (5000, 5000), dict(hdmi=1, crop=1, crop_x=1920, crop_y=1080)), # Clamped.
    ((2560, 1440, True), 1, 1, None, dict(hdmi=1, crop=1, crop_x=320, crop_y=180, canvas=0)), # Centered.
    ((1920, 1080, True), 1, 2, None, dict(hdmi=1, crop=1, crop_x=320, crop_y=180, canvas=0)), # 1080p -> 720p.
    ((1280,  720, True), 1, 1, None, dict(hdmi=1, crop=0, canvas=1, in_width=1280, in_height=720)),
    ((1366,  768, True), 1, 1, None, dict(hdmi=1, crop=1, crop_x=0, canvas=1, in_width=1364, in_height=768)),
    ((1600, 1200, True), 1, 1, None, dict(hdmi=1, crop=1, crop_x=0, crop_y=60, canvas=1, in_width=1600, in_height=1080)),
    ((3840, 2160, True), 2, 1, None, dict(hdmi=1, m420=1, downscale=0, crop=0, canvas=0)),
    ((1920, 1080, True), 2, 1, None, dict(hdmi=0, m420=1, no_signal=0)),                     # Pattern.
    ((1920, 1080, False), 1, 1, None, dict(hdmi=0, no_signal=1)),
])
def test_video_policy(fx3, inp, fmt, frame, crop, expected):
    fx3.set_input(*inp)
    if crop is not None:
        fx3.lib.uvc_set_crop(1, crop[0], crop[1])
    fx3.commit(fmt, frame, 30)
    v = fx3.video
    assert fx3.var("stub_video_starts").value >= 1
    for k, val in expected.items():
        assert getattr(v, k) == val, (k, getattr(v, k), val)
    # Window fits in the input/frame.
    if v.crop or v.canvas:
        assert v.in_width % 4 == 0 and v.in_width <= v.width and v.in_height <= v.height
        assert v.crop_x % 2 == 0
        assert v.crop_x + v.in_width <= inp[0] and v.crop_y + v.in_height <= inp[1]

def test_input_change_restarts_video(fx3):
    fx3.set_input(1920, 1080)
    fx3.commit(1, 1, 60)
    starts = fx3.var("stub_video_starts").value
    fx3.set_input(3840, 2160)
    fx3.lib.uvc_service()
    assert fx3.var("stub_video_starts").value == starts + 1
    assert fx3.video.downscale == 1
    fx3.lib.uvc_service() # No change: no restart.
    assert fx3.var("stub_video_starts").value == starts + 1

# Audio / bus reset --------------------------------------------------------------------------------

def test_audio_interface_and_bus_reset(fx3):
    fx3.set_input(1920, 1080)
    fx3.commit(1, 1, 60)
    assert fx3.var("stub_gpif_start_video", ctypes.c_int32).value == 1
    assert fx3.var("stub_gpif_start_audio", ctypes.c_int32).value == 0
    starts   = fx3.var("stub_gpif_starts").value
    restarts = fx3.var("stub_thread_restarts", ctypes.c_uint32*2)
    video_restarts = restarts[0]
    fx3.lib.uvc_audio_set_interface(1)
    fx3.lib.uvc_service()
    # Audio joins the running video: only the audio thread is (re)started.
    assert fx3.var("stub_gpif_starts").value == starts
    assert restarts[1] == 1 and restarts[0] == video_restarts
    assert fx3.var("stub_gpif_video", ctypes.c_int32).value == 1
    assert fx3.var("stub_gpif_audio", ctypes.c_int32).value == 1
    assert fx3.var("stub_audio_enable", ctypes.c_int32).value == 1
    assert fx3.lib.uvc_audio_get_interface() == 1
    fx3.lib.uvc_audio_set_batch(4)
    fx3.lib.uvc_service()
    assert fx3.var("stub_gpif_batch", ctypes.c_int32).value == 4
    # Bus reset: everything stops.
    fx3.lib.uvc_bus_reset()
    fx3.lib.uvc_service()
    assert fx3.var("stub_gpif_video", ctypes.c_int32).value == 0
    assert fx3.var("stub_gpif_audio", ctypes.c_int32).value == 0
    assert fx3.lib.uvc_audio_get_interface() == 0

def test_bus_reset_idle_no_restart(fx3):
    fx3.lib.uvc_bus_reset()
    fx3.lib.uvc_service()
    assert fx3.var("stub_gpif_starts").value == 0

# Processing / Extension Units ---------------------------------------------------------------------

PU, XU = 3, 4

def test_pu_brightness(fx3):
    sel = 0x02
    assert fx3.unit(UVC_GET_INFO, PU, sel, length=1) == bytes([3])
    assert struct.unpack("<h", fx3.unit(UVC_GET_MIN, PU, sel))[0] == -64
    assert struct.unpack("<h", fx3.unit(UVC_GET_MAX, PU, sel))[0] == 64
    assert struct.unpack("<h", fx3.unit(UVC_GET_DEF, PU, sel))[0] == 0
    fx3.unit(UVC_SET_CUR, PU, sel, struct.pack("<h", 200))  # Clamped.
    assert struct.unpack("<h", fx3.unit(UVC_GET_CUR, PU, sel))[0] == 64
    fx3.unit(UVC_SET_CUR, PU, sel, struct.pack("<h", -10))
    fx3.lib.uvc_service()
    writes = dict(fx3.csr_writes())
    assert writes[fx3.csr["CSR_COLOR_BRIGHTNESS"]] == (-10 & 0xff)

def test_pu_unknown_selector_stalls(fx3):
    assert fx3.unit(UVC_GET_CUR, PU, 0x04) is None  # Gain: not supported.

def test_xu_crop_and_info(fx3):
    assert struct.unpack("<H", fx3.unit(UVC_GET_LEN, XU, 2))[0] == 4
    assert struct.unpack("<H", fx3.unit(UVC_GET_LEN, XU, 1))[0] == 24
    assert struct.unpack("<HH", fx3.unit(UVC_GET_CUR, XU, 2, length=4))[0] == 0xffff
    fx3.unit(UVC_SET_CUR, XU, 2, struct.pack("<HH", 640, 360), length=4)
    assert struct.unpack("<HH", fx3.unit(UVC_GET_CUR, XU, 2, length=4)) == (640, 360)
    fx3.set_input(3840, 2160, colorspace=1)
    fx3.var("stub_csr_read_value").value = 3333333
    info = fx3.unit(UVC_GET_CUR, XU, 1, length=24)
    assert struct.unpack_from("<H", info, 6)[0] == 3840   # hactive.
    assert struct.unpack_from("<H", info, 10)[0] == 2160  # vactive.
    assert info[3] == 1 and info[14] == 1
    assert struct.unpack_from("<I", info, 20)[0] == 3333333
    assert fx3.unit(UVC_SET_CUR, XU, 1, bytes(24), length=24) is None  # Read-only.

# Descriptors --------------------------------------------------------------------------------------

def test_descriptors(fx3):
    import sys
    sys.path.insert(0, os.path.join(ROOT, "software"))
    from usb_desc_check import parse_config
    fx3.lib.usb_desc_get.restype = ctypes.POINTER(ctypes.c_uint8)
    fx3.lib.usb_desc_length.restype = ctypes.c_uint16
    for speed in (0, 1):
        p = fx3.lib.usb_desc_get(2, 0, speed)
        n = fx3.lib.usb_desc_length(p)
        cfg = parse_config(bytes(p[:n]))
        assert cfg["interfaces"] == 4
        assert cfg["vc_chain"] == [(1, None), (3, 1), (4, 3), (2, 4)]
        assert [(f["guid"], [(fr["w"], fr["h"], fr["fps"]) for fr in f["frames"]]) for f in cfg["formats"]] == [
            (b"YUY2", [(1920, 1080, [60, 30]), (1280, 720, [60, 30]), (640, 480, [60, 30])]),
            (b"M420", [(3840, 2160, [30]), (1920, 1080, [60, 30])]),
        ]
        assert cfg["endpoints"] == {0x81: 1024 if speed else 512, 0x82: 192}

def test_settings_apply_while_streaming(fx3):
    # Idle: settings are stored, nothing restarted.
    fx3.lib.uvc_audio_set_test(1)
    fx3.lib.uvc_set_crop(1, 0, 0)
    fx3.lib.uvc_service()
    assert fx3.var("stub_gpif_starts").value == 0
    # Audio streaming: audio source/batch changes are applied.
    fx3.lib.uvc_audio_set_interface(1)
    fx3.lib.uvc_service()
    assert fx3.var("stub_audio_test", ctypes.c_int32).value == 1
    fx3.lib.uvc_audio_set_test(0)
    fx3.lib.uvc_audio_set_batch(3)
    fx3.lib.uvc_service()
    assert fx3.var("stub_audio_test", ctypes.c_int32).value == 0
    assert fx3.var("stub_gpif_batch", ctypes.c_int32).value == 3
    # Video streaming: crop changes restart the video source.
    fx3.set_input(3840, 2160)
    fx3.commit(1, 1, 30)
    assert (fx3.video.crop, fx3.video.crop_x) == (1, 0)
    fx3.lib.uvc_set_crop(1, 640, 0)
    fx3.lib.uvc_service()
    assert (fx3.video.crop, fx3.video.crop_x) == (1, 640)
    fx3.lib.uvc_set_crop(0, 0, 0)
    fx3.lib.uvc_service()
    assert (fx3.video.crop, fx3.video.downscale) == (0, 1)

def test_audio_batch_4k(fx3):
    fx3.lib.uvc_audio_set_interface(1)
    fx3.set_input(1920, 1080)
    fx3.commit(1, 1, 60)
    assert fx3.var("stub_gpif_batch", ctypes.c_int32).value == 1
    fx3.set_input(3840, 2160)
    fx3.commit(2, 1, 30)  # 4K M420: at least 4 packets per switch.
    assert fx3.var("stub_gpif_batch", ctypes.c_int32).value == 4
    fx3.lib.uvc_audio_set_batch(6)
    fx3.lib.uvc_service()
    assert fx3.var("stub_gpif_batch", ctypes.c_int32).value == 6

@pytest.mark.parametrize("colorspace, quant, override, rgb, full", [
    (0, 0, None, 1, 1),        # RGB, default range: full (PC sources).
    (0, 2, None, 1, 1),
    (0, 1, None, 1, 0),        # RGB, explicit limited.
    (0, 1, "full", 1, 1),      # Override.
    (0, 0, "limited", 1, 0),
    (1, 0, None, 0, 1),        # YCbCr: IT6802 bypass, no FPGA CSC.
])
def test_rgb_range_policy(fx3, colorspace, quant, override, rgb, full):
    if override is not None:
        fx3.lib.uvc_set_range({"limited": 1, "full": 2}[override])
    fx3.set_input(1920, 1080, colorspace=colorspace, quant_range=quant)
    fx3.commit(1, 1, 60)
    v = fx3.video
    assert (v.rgb, v.c_swap) == (rgb, int(colorspace != 0))
    if rgb:
        assert v.full_range == full
