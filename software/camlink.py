#!/usr/bin/env python3

#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""LiteCamLink host tool: FX3 RAM boot, device control and tests."""

import os
import sys
import time
import struct
import argparse

import usb.core
import usb.util

# Constants ----------------------------------------------------------------------------------------

FX3_BOOT_VID    = 0x04b4
FX3_BOOT_PID    = 0x00f3
LITECAMLINK_VID = 0x1209
LITECAMLINK_PID = 0x0001
STOCK_VID       = 0x0fd9
STOCK_PIDS      = (0x0066, 0x0067)

FX3_REQ_FW      = 0xa0 # FX3 boot ROM firmware download/upload/jump request.
FX3_CHUNK       = 2048

# Helpers ------------------------------------------------------------------------------------------

def _is_litecamlink(dev):
    try:
        return dev.product == "LiteCamLink"
    except (ValueError, usb.core.USBError):
        return False

def find_device(vid, pid, timeout=0.0):
    deadline = time.time() + timeout
    while True:
        if (vid, pid) == (LITECAMLINK_VID, LITECAMLINK_PID):
            # The pid.codes test VID/PID is shared: also match the product string.
            dev = usb.core.find(idVendor=vid, idProduct=pid, custom_match=_is_litecamlink)
        else:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is not None or time.time() >= deadline:
            return dev
        time.sleep(0.1)

def parse_fx3_image(data):
    """Parse a FX3 boot image ("CY" header, sections, entry point, checksum)."""
    if data[0:2] != b"CY":
        raise ValueError("Not a FX3 image (missing CY signature).")
    if data[3] != 0xb0:
        raise ValueError(f"Unsupported FX3 image type 0x{data[3]:02x}.")
    sections = []
    checksum = 0
    offset   = 4
    while True:
        length, address = struct.unpack_from("<II", data, offset)
        offset += 8
        if length == 0:
            entry = address
            break
        section = data[offset:offset + 4*length]
        checksum = (checksum + sum(struct.unpack(f"<{length}I", section))) & 0xffffffff
        sections.append((address, section))
        offset += 4*length
    expected, = struct.unpack_from("<I", data, offset)
    if checksum != expected:
        raise ValueError(f"Bad FX3 image checksum (0x{checksum:08x} vs 0x{expected:08x}).")
    return sections, entry

# FX3 Boot -----------------------------------------------------------------------------------------

def fx3_load(filename, timeout=5.0):
    """Load a FX3 image to RAM through the FX3 boot ROM and jump to it."""
    dev = find_device(FX3_BOOT_VID, FX3_BOOT_PID, timeout)
    if dev is None:
        raise RuntimeError("FX3 bootloader (04b4:00f3) not found.")
    sections, entry = parse_fx3_image(open(filename, "rb").read())
    for address, section in sections:
        for i in range(0, len(section), FX3_CHUNK):
            chunk = section[i:i + FX3_CHUNK]
            addr  = address + i
            dev.ctrl_transfer(0x40, FX3_REQ_FW, addr & 0xffff, addr >> 16, chunk, timeout=1000)
    print(f"Loaded {sum(len(s) for _, s in sections)} bytes, jumping to 0x{entry:08x}.")
    try:
        dev.ctrl_transfer(0x40, FX3_REQ_FW, entry & 0xffff, entry >> 16, b"", timeout=1000)
    except usb.core.USBError:
        pass # Device may disconnect before the status stage.
    usb.util.dispose_resources(dev)

# LiteCamLink Device ------------------------------------------------------------------------------

VREQ_IDENT     = 0x00
VREQ_MEM_READ  = 0x01
VREQ_MEM_WRITE = 0x02
VREQ_REBOOT    = 0x0f
VREQ_FPGA_INFO = 0x10
VREQ_FPGA_CFG  = 0x11
VREQ_FPGA_DATA = 0x12
VREQ_FPGA_DONE = 0x13
VREQ_GPIO_CFG  = 0x20
VREQ_GPIO_READ = 0x21
VREQ_I2C_WRITE = 0x30
VREQ_I2C_READ  = 0x31
VREQ_I2C_STAT  = 0x32
VREQ_STREAM_START  = 0x40
VREQ_STREAM_STOP   = 0x41
VREQ_STREAM_STATUS = 0x42
VREQ_HDMI_STATUS   = 0x50
VREQ_HDMI_INIT     = 0x51
VREQ_FLASH_ID      = 0x60
VREQ_FLASH_READ    = 0x61
VREQ_FLASH_PROGRAM = 0x62
VREQ_FLASH_ERASE   = 0x63
VREQ_FLASH_STATUS  = 0x64
VREQ_FLASH_RECOVER = 0x65
VREQ_FPGA_BOOT     = 0x66
VREQ_AUDIO_TEST    = 0x70
VREQ_CROP          = 0x71
VREQ_WATCHDOG      = 0x72
VREQ_WATCHDOG_READ = 0x73
VREQ_HANG          = 0x74
VREQ_STATS         = 0x75
VREQ_AUDIO_BATCH   = 0x76
VREQ_RANGE         = 0x77

FLASH_BLOCK_SIZE      = 0x10000
FLASH_BITSTREAM_HDR   = 0x100000
FLASH_BITSTREAM       = 0x100100
FLASH_BITSTREAM_MAGIC = 0x4b4c434c # "LCLK".
SYS_CLK_FREQ          = 100e6      # FPGA sys clock (default build).

GPIF_OMEGA_EMPTY_FULL_TH0 = 16

FPGA_STATUS_DONE = (1 <<  8)
FPGA_STATUS_BUSY = (1 << 12)
FPGA_STATUS_FAIL = (1 << 13)

class CamLink:
    def __init__(self, timeout=5.0):
        self.dev = find_device(LITECAMLINK_VID, LITECAMLINK_PID, timeout)
        if self.dev is None:
            raise RuntimeError("LiteCamLink device (1209:0001) not found.")

    def vendor_in(self, req, value=0, index=0, length=0):
        return bytes(self.dev.ctrl_transfer(0xc0, req, value, index, length, timeout=1000))

    def vendor_out(self, req, value=0, index=0, data=b""):
        return self.dev.ctrl_transfer(0x40, req, value, index, data, timeout=1000)

    def ident(self):
        return self.vendor_in(VREQ_IDENT, length=64).rstrip(b"\x00").decode()

    def mem_read(self, addr, length=4):
        return self.vendor_in(VREQ_MEM_READ, addr & 0xffff, addr >> 16, length)

    def read32(self, addr):
        return struct.unpack("<I", self.mem_read(addr, 4))[0]

    def write32(self, addr, value):
        self.vendor_out(VREQ_MEM_WRITE, addr & 0xffff, addr >> 16, struct.pack("<I", value))

    def fpga_info(self):
        return struct.unpack("<II", self.vendor_in(VREQ_FPGA_INFO, length=8))

    def fpga_load(self, filename, chunk=4096):
        bitstream = open(filename, "rb").read()
        start     = time.time()
        self.vendor_out(VREQ_FPGA_CFG)
        for i in range(0, len(bitstream), chunk):
            self.dev.ctrl_transfer(0x40, VREQ_FPGA_DATA, 0, 0, bitstream[i:i + chunk], timeout=5000)
        status, = struct.unpack("<I", self.vendor_in(VREQ_FPGA_DONE, length=4))
        print(f"FPGA configured with {filename} ({len(bitstream)} bytes, {time.time() - start:.2f}s), "
              f"status 0x{status:08x}.")
        if not (status & FPGA_STATUS_DONE) or (status & FPGA_STATUS_FAIL):
            raise RuntimeError("FPGA configuration failed.")

    def gpio_cfg(self, pin, mode):
        self.vendor_out(VREQ_GPIO_CFG, mode, pin)

    def gpio_read(self):
        return struct.unpack("<Q", self.vendor_in(VREQ_GPIO_READ, length=8))[0]

    def i2c_write(self, addr, prefix=b"", data=b""):
        self.vendor_out(VREQ_I2C_WRITE, addr | (len(prefix) << 8), 0, bytes(prefix) + bytes(data))
        if self.vendor_in(VREQ_I2C_STAT, length=1)[0]:
            raise IOError(f"I2C write to 0x{addr:02x} failed.")

    def i2c_read(self, addr, prefix=b"", length=1):
        assert len(prefix) <= 2
        index = int.from_bytes(bytes(prefix), "little")
        try:
            return self.vendor_in(VREQ_I2C_READ, addr | (len(prefix) << 8), index, length)
        except usb.core.USBError:
            raise IOError(f"I2C read from 0x{addr:02x} failed.")

    def i2c_scan(self):
        found = []
        for addr in range(0x08, 0x78):
            try:
                self.i2c_read(addr, length=1)
                found.append(addr)
            except IOError:
                pass
        return found

    def stream_start(self, video=True, audio=False):
        """Start the GPIF (96MHz PCLK) with the video (thread 0) and/or audio (thread 1) DMA."""
        self.vendor_out(VREQ_STREAM_START, int(video) | (int(audio) << 1))

    def stream_stop(self):
        self.vendor_out(VREQ_STREAM_STOP)

    def stream_status(self):
        names = ["gpif_wave_stat", "gpif_lambda", "pib_intr", "pib_error",
                 "pib_sck0_status", "pib_sck0_dscr", "uib_sck1_status", "uib_sck1_dscr"]
        return dict(zip(names, struct.unpack("<8I", self.vendor_in(VREQ_STREAM_STATUS, length=32))))

    def read_stream(self, length, timeout=1000):
        return self.dev.read(0x81, length, timeout=timeout)

    def hdmi_init(self):
        self.vendor_out(VREQ_HDMI_INIT)

    def hdmi_status(self):
        names = ["present", "sys_status", "hpd", "stable"]
        d = self.vendor_in(VREQ_HDMI_STATUS, length=24)
        st = dict(zip(names, d[:4]))
        st.update(zip(["htotal", "hactive", "vtotal", "vactive"], struct.unpack("<4H", d[4:12])))
        st["pclk_reg"], st["video_mode"], st["colorspace"] = d[12], d[13], ["RGB", "YCbCr422", "YCbCr444", "?"][d[14] & 3]
        st["5v"]  = st["sys_status"] & 1
        st["pclk_mhz"] = (124*255/st["pclk_reg"])/10 if st["pclk_reg"] else 0
        if len(d) >= 24:
            st["generation"], period = struct.unpack("<II", d[16:24])
            st["fps"] = round(SYS_CLK_FREQ/period, 3) if period else 0
        return st

    # SPI Flash.
    def flash_id(self):
        return struct.unpack("<I", self.vendor_in(VREQ_FLASH_ID, length=4))[0]

    def flash_read(self, addr, length, chunk=4096):
        data = bytearray()
        while len(data) < length:
            n = min(chunk, length - len(data))
            a = addr + len(data)
            data += self.dev.ctrl_transfer(0xc0, VREQ_FLASH_READ, a & 0xffff, a >> 16, n, timeout=5000)
        return bytes(data)

    def flash_erase(self, addr):
        self.vendor_out(VREQ_FLASH_ERASE, addr & 0xffff, addr >> 16)
        deadline = time.time() + 5
        while self.vendor_in(VREQ_FLASH_STATUS, length=1)[0]:
            if time.time() > deadline:
                raise TimeoutError("Flash erase timeout.")
            time.sleep(0.05)

    def flash_write(self, addr, data, verify=True, progress=True):
        """Erase the covered 64KB blocks, program and verify."""
        assert addr % 256 == 0
        first = addr & ~(FLASH_BLOCK_SIZE - 1)
        last  = (addr + len(data) - 1) & ~(FLASH_BLOCK_SIZE - 1)
        for block in range(first, last + 1, FLASH_BLOCK_SIZE):
            self.flash_erase(block)
        for off in range(0, len(data), 4096):
            chunk = data[off:off + 4096]
            self.dev.ctrl_transfer(0x40, VREQ_FLASH_PROGRAM, (addr + off) & 0xffff, (addr + off) >> 16, chunk, timeout=5000)
            if progress:
                print(f"\r  programmed {off + len(chunk)}/{len(data)}", end="", flush=True)
        if progress:
            print()
        if verify and self.flash_read(addr, len(data)) != bytes(data):
            raise IOError("Flash verify failed.")

    def flash_bitstream(self, filename):
        bitstream = open(filename, "rb").read()
        size   = len(bitstream)
        header = struct.pack("<III", size, ~size & 0xffffffff, FLASH_BITSTREAM_MAGIC).ljust(256, b"\xff")
        self.flash_write(FLASH_BITSTREAM_HDR, header + bitstream)

    def flash_recover(self):
        """Erase the FX3 image (block 0) and reboot to the USB bootloader."""
        self.vendor_out(VREQ_FLASH_RECOVER)
        usb.util.dispose_resources(self.dev)

    def fpga_boot(self):
        """Load the FPGA from the flash bitstream, return the configuration status (0: none)."""
        self.vendor_out(VREQ_FPGA_BOOT)
        deadline = time.time() + 10
        while True:
            status = self.vendor_in(VREQ_FLASH_STATUS, length=8)
            if not status[0]:
                return struct.unpack_from("<I", status, 4)[0]
            if time.time() > deadline:
                raise TimeoutError("FPGA boot timeout.")
            time.sleep(0.05)

    def audio_test(self, enable):
        """Audio source: FPGA test counter (True) or HDMI I2S (False)."""
        self.vendor_out(VREQ_AUDIO_TEST, int(enable))

    def crop(self, x=None, y=0):
        """Inputs larger than the UVC frame: crop window at (x, y), or 2x downscale (x=None)."""
        self.vendor_out(VREQ_CROP, 0xffff if x is None else x, y)

    def watchdog(self, ticks, divider=1):
        """FX3 watchdog (reset mode): reload value (ticks, multiple of 256, 0 = off), backup divider."""
        self.vendor_out(VREQ_WATCHDOG, ticks >> 8, divider)

    def watchdog_value(self):
        return struct.unpack("<I", self.vendor_in(VREQ_WATCHDOG_READ, length=4))[0]

    def stats(self):
        names = ["main_loops", "usb_isrs", "ss_to_usb2_fallbacks", "ss_connects", "phy_cr_timeouts",
            "uvc_commits", "uvc_halts", "stream_starts", "stream_stops"]
        return dict(zip(names, struct.unpack("<9I", self.vendor_in(VREQ_STATS, length=36))))

    def audio_batch(self, packets):
        """Audio packets (1ms) sent per GPIF thread switch."""
        self.vendor_out(VREQ_AUDIO_BATCH, packets)

    def range(self, value):
        """RGB input range: "auto" (AVI InfoFrame), "limited" or "full"."""
        self.vendor_out(VREQ_RANGE, {"auto": 0, "limited": 1, "full": 2}[value])

    def hang(self):
        """Debug: hang the FX3 CPU with interrupts off (watchdog test)."""
        self.vendor_out(VREQ_HANG)

    def reboot(self):
        self.vendor_out(VREQ_REBOOT)
        usb.util.dispose_resources(self.dev)

# SoC Bus (FPGA I2C Bridge) ------------------------------------------------------------------------

from litex.tools.remote.csr_builder import CSRBuilder

FPGA_I2C_ADDR = 0x10

class CamLinkBus(CSRBuilder):
    """FPGA SoC bus access through the FX3 I2C master and the FPGA I2CBridge."""
    def __init__(self, cl=None, csr_csv="build/csr.csv"):
        self.cl = CamLink() if cl is None else cl
        CSRBuilder.__init__(self, comm=self, csr_csv=csr_csv)

    def read(self, addr, length=None, burst="incr"):
        n = 1 if length is None else length
        self.cl.i2c_write(FPGA_I2C_ADDR, data=struct.pack(">I", addr))
        data = self.cl.i2c_read(FPGA_I2C_ADDR, length=4*n)
        values = list(struct.unpack(f">{n}I", data))
        return values[0] if length is None else values

    def write(self, addr, data):
        data = data if isinstance(data, list) else [data]
        self.cl.i2c_write(FPGA_I2C_ADDR, data=struct.pack(f">I{len(data)}I", addr, *data))

# Stream Test --------------------------------------------------------------------------------------

import numpy as np

def stream_test(cl, bus, size=64*1024*1024, clk_div_x2=16, flag_omega=GPIF_OMEGA_EMPTY_FULL_TH0,
    flag_invert=0, data_delay=0, chunk=4*1024*1024):
    bus.regs.gen_enable.write(0)
    bus.regs.gpif_control.write(0)
    bus.regs.main_source_sel.write(0)
    cl.stream_start()
    time.sleep(1.2) # FreqMeter period is 1s.
    print(f"FX3 PCLK: {bus.regs.fx3_clk_freq_value.read()/1e6:.2f} MHz, "
          f"FLAG: {bus.regs.gpif_status.read() & 1}")
    bus.regs.gen_enable.write(1)
    bus.regs.gpif_control.write(1 | (flag_invert << 1) | (data_delay << 4))
    from usb_stream import USBStreamReader
    state  = {"errors": 0, "last": None, "first": True}
    def check(chunk):
        data = np.frombuffer(chunk, dtype=np.uint32)
        if not len(data):
            return
        if state["first"]:
            print("First words: " + " ".join(f"{w:08x}" for w in data[:8]))
            state["first"] = False
        if state["last"] is not None:
            state["errors"] += int((int(data[0]) - int(state["last"])) % (1 << 32) != 1)
        diffs = np.diff(data.astype(np.int64)) % (1 << 32)
        state["errors"] += int(np.count_nonzero(diffs != 1))
        state["last"] = data[-1]
    usb.util.dispose_resources(cl.dev)
    reader = USBStreamReader()
    received, duration, error = reader.read(size, check)
    reader.close()
    errors = state["errors"]
    if error is not None:
        print(f"USB error: {error}")
    print(f"Received {received/1e6:.1f} MB in {duration:.2f}s: {received/duration/1e6:.1f} MB/s, "
          f"{errors} counter discontinuities.")
    for k, v in cl.stream_status().items():
        print(f"  {k:16s}: 0x{v:08x}")
    bus.regs.gpif_control.write(0)
    bus.regs.gen_enable.write(0)
    cl.stream_stop()
    return errors == 0 and received >= size

# UVC Raw Test -------------------------------------------------------------------------------------

def uvc_pattern_config(bus, width=1920, height=1080, fps=30, sys_clk_freq=None):
    if sys_clk_freq is None:
        sys_clk_freq = bus.constants.config_clock_frequency
    bus.regs.pattern_enable.write(0)
    bus.regs.pattern_hwords.write(width//2)
    bus.regs.pattern_vres.write(height)
    bus.regs.pattern_bar_words.write(width//16)
    bus.regs.pattern_frame_period.write(int(sys_clk_freq/fps))
    bus.regs.uvc_frame_words.write(width*height//2)

def uvc_raw_test(cl, bus, width=1920, height=1080, fps=30, frames=60, clk_div_x2=8):
    """Stream the UVC pattern over raw USB and check payload headers/frames."""
    from usb_stream import USBStreamReader
    bus.regs.gpif_control.write(0)
    bus.regs.main_source_sel.write(1)
    uvc_pattern_config(bus, width, height, fps)
    cl.stream_start()
    bus.regs.gpif_control.write((4 << 8) | 1 | 2)
    bus.regs.pattern_enable.write(1)

    frame_size = width*height*2
    payload_size = bus.regs.uvc_payload_words.read()*4 + 12 # One FX3 DMA buffer.
    state = {"frame": bytearray(), "frames": [], "fid": None, "errors": 0, "pts": []}
    def on_transfer(chunk):
        # Payloads are one FX3 buffer (header + data) except the last one of a frame (short packet).
        for off in range(0, len(chunk), payload_size):
            payload = chunk[off:off + payload_size]
            hlen, info = payload[0], payload[1]
            if hlen != 12 or not (info & 0x80):
                state["errors"] += 1
                continue
            fid = info & 1
            if state["fid"] is not None and fid != state["fid"] and len(state["frame"]):
                state["errors"] += 1 # FID change without EOF.
                state["frame"] = bytearray()
            state["fid"] = fid
            state["frame"] += payload[12:]
            if info & 0x02:
                state["frames"].append(len(state["frame"]))
                state["pts"].append(struct.unpack("<I", payload[2:6])[0])
                if len(state["frames"]) == 2:
                    state["sample"] = bytes(state["frame"])
                state["frame"] = bytearray()
                state["fid"] = None

    usb.util.dispose_resources(cl.dev)
    reader = USBStreamReader()
    received, duration, error = reader.read(frames*frame_size*1.01, on_transfer, timeout=5.0)
    reader.close()
    bus.regs.pattern_enable.write(0)
    bus.regs.gpif_control.write(0)
    cl.stream_stop()

    sizes  = state["frames"]
    good   = sum(1 for s in sizes if s == frame_size)
    pts    = np.diff(np.array(state["pts"], dtype=np.int64)) % (1 << 32) / bus.constants.config_clock_frequency
    print(f"Received {received/1e6:.1f} MB in {duration:.2f}s ({received/duration/1e6:.1f} MB/s), "
          f"{len(sizes)} frames, {good} with size {frame_size}, {state['errors']} header errors.")
    if len(pts):
        print(f"PTS intervals: mean {np.mean(pts)*1e3:.2f} ms, min {np.min(pts)*1e3:.2f}, max {np.max(pts)*1e3:.2f}")
    if "sample" in state:
        f = np.frombuffer(state["sample"], dtype=np.uint32)
        number = sum(((int(f[i]) & 0xff) > 128) << i for i in range(32))
        print(f"Sample frame number: {number}, bars: " +
              " ".join(f"{int(f[width + i*(width//16) + 8]):08x}" for i in range(8)))
        np.save("build/uvc_frame.npy", f)
    return good >= frames - 2 and state["errors"] == 0

# Terminal (UART crossover) -----------------------------------------------------------------------

def term(bus, cmds=None, duration=None):
    """BIOS console over the UART crossover CSRs. Scripted if cmds/duration are given."""
    import select, termios, tty
    def rx():
        out = bytearray()
        while not bus.regs.uart_xover_rxempty.read():
            out.append(bus.regs.uart_xover_rxtx.read() & 0xff)
        return bytes(out)
    def tx(data):
        for c in data:
            while bus.regs.uart_xover_txfull.read():
                pass
            bus.regs.uart_xover_rxtx.write(c)
    if cmds is not None or duration is not None:
        start = time.time()
        for cmd in (cmds or []):
            tx(cmd.encode() + b"\n")
        while time.time() - start < (duration or 2):
            data = rx()
            if data:
                sys.stdout.write(data.decode(errors="replace")); sys.stdout.flush()
            else:
                time.sleep(0.05)
        return
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        while True:
            data = rx()
            if data:
                sys.stdout.write(data.decode(errors="replace")); sys.stdout.flush()
            if select.select([sys.stdin], [], [], 0.02)[0]:
                c = os.read(fd, 1)
                if c == b"\x03":
                    break
                tx(b"\n" if c == b"\r" else c)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# Pin Test -----------------------------------------------------------------------------------------

# FPGA PinTest pins order (see litecamlink.py) with their expected FX3 GPIO.
PINTEST_PINS = \
    [(f"dq{i}",  i)      for i in range(16)] + \
    [(f"dq{i}",  i + 17) for i in range(16, 28)] + \
    [(f"dq{i}",  i + 18) for i in range(28, 32)] + \
    [(f"ctl{i}", g)      for i, g in enumerate([17, 18, 19, 20, 21, 22, 24, 28, 29])] + \
    [("pclk", 16), ("gpio27", 27)]
PINTEST_STEP_GPIO = 45

def pintest(cl, id_bits=8):
    gpios = sorted(g for _, g in PINTEST_PINS)
    for g in gpios:
        cl.gpio_cfg(g, 0)
    cl.gpio_cfg(PINTEST_STEP_GPIO, 1)
    samples = []
    for step in range(id_bits):
        time.sleep(0.01)
        samples.append(cl.gpio_read())
        cl.gpio_cfg(PINTEST_STEP_GPIO, 2)
        time.sleep(0.01)
        cl.gpio_cfg(PINTEST_STEP_GPIO, 1)
    errors = 0
    for g in gpios:
        ident = sum(((samples[b] >> g) & 1) << b for b in range(id_bits))
        name  = PINTEST_PINS[ident - 1][0] if 1 <= ident <= len(PINTEST_PINS) else "?"
        exp   = [n for n, eg in PINTEST_PINS if eg == g][0]
        ok    = (name == exp)
        errors += not ok
        print(f"FX3 GPIO{g:2d}: id {ident:3d} -> {name:7s} (expected {exp:7s}) {'OK' if ok else 'ERROR'}")
    print(f"{len(gpios) - errors}/{len(gpios)} pins OK.")
    return errors == 0

# Main ---------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteCamLink host tool.")
    parser.add_argument("--csr-csv", default="build/csr.csv", help="FPGA CSR map (build directory csr.csv).")
    sub    = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fx3-load", help="Load a FX3 image to RAM (device in FX3 bootloader).")
    p.add_argument("image")

    p = sub.add_parser("boot", help="Boot: load FX3 firmware (if in bootloader) and FPGA bitstream.")
    p.add_argument("--fx3", default="firmware/fx3/build/fx3.img")
    p.add_argument("--bit", default="build/gateware/litecamlink.bit")

    p = sub.add_parser("fpga-load", help="Load a bitstream to the FPGA (through the FX3).")
    p.add_argument("bitstream")
    sub.add_parser("fpga-info", help="Show FPGA IDCODE/status.")
    sub.add_parser("pintest",   help="Run FX3 <-> FPGA pin test (needs --with-pintest bitstream).")

    sub.add_parser("i2c-scan", help="Scan the internal I2C bus.")
    p = sub.add_parser("i2c-dump", help="Dump 256 registers of an I2C device (8-bit register address).")
    p.add_argument("addr", type=lambda x: int(x, 0))
    p = sub.add_parser("i2c-write", help="Write an I2C register (8-bit register address).")
    p.add_argument("addr", type=lambda x: int(x, 0))
    p.add_argument("reg",  type=lambda x: int(x, 0))
    p.add_argument("value", type=lambda x: int(x, 0))

    p = sub.add_parser("csr", help="Read (or write) a FPGA CSR register by name.")
    p.add_argument("name", nargs="?", help="Register name (list all if omitted).")
    p.add_argument("value", nargs="?", type=lambda x: int(x, 0))

    p = sub.add_parser("stream-test", help="GPIF -> USB streaming test with FPGA counter pattern.")
    p.add_argument("--size",        default=64, type=int, help="Size to receive (MB).")
    p.add_argument("--clk-div-x2",  default=16, type=int, help="PIB clock divider x2 (SYS=384MHz).")
    p.add_argument("--flag-omega",  default=16, type=int, help="GPIF omega signal for FLAG (CTL1).")
    p.add_argument("--flag-invert", default=0,  type=int, help="Invert FLAG in the FPGA.")
    p.add_argument("--data-delay",  default=0,  type=int, help="DQ delay relative to VALID (0-3).")
    sub.add_parser("stream-status", help="Show GPIF/DMA status.")
    p = sub.add_parser("uvc-raw-test", help="UVC pattern stream over raw USB (payload/frame checks).")
    p.add_argument("--width",  default=1920, type=int)
    p.add_argument("--height", default=1080, type=int)
    p.add_argument("--fps",    default=30,   type=int)
    p.add_argument("--frames", default=60,   type=int)

    p = sub.add_parser("term", help="BIOS console over the UART crossover (Ctrl-C to exit).")
    p.add_argument("--cmd",  action="append", dest="commands", help="Command(s) to send (scripted mode).")
    p.add_argument("--time", type=float,      help="Capture duration in seconds (scripted mode).")

    sub.add_parser("hdmi-status", help="Show HDMI receiver (IT6802) status.")

    sub.add_parser("flash-id", help="Show the SPI flash JEDEC ID.")
    p = sub.add_parser("flash-dump", help="Dump the SPI flash to a file.")
    p.add_argument("filename")
    p = sub.add_parser("flash-write", help="Write a file to the SPI flash (erase/program/verify).")
    p.add_argument("filename")
    p.add_argument("--offset", default=0, type=lambda x: int(x, 0))
    p = sub.add_parser("flash-bitstream", help="Write a LiteCamLink bitstream (header + data at 0x100000).")
    p.add_argument("bitstream", nargs="?", default="build/gateware/litecamlink.bit")
    p = sub.add_parser("flash-fx3", help="Write a FX3 image at offset 0 (standalone boot).")
    p.add_argument("image", nargs="?", default="firmware/fx3/build/fx3.img")
    sub.add_parser("flash-recover", help="Erase the FX3 image and reboot to the USB bootloader.")
    sub.add_parser("fpga-boot", help="Load the FPGA from the flash bitstream.")
    sub.add_parser("stats", help="Show FX3 debug counters (link fallbacks, PHY timeouts, streams).")
    p = sub.add_parser("crop", help="Crop window for inputs larger than the UVC frame (no args: downscale).")
    p.add_argument("x", nargs="?", type=int)
    p.add_argument("y", nargs="?", type=int, default=0)
    p = sub.add_parser("range", help="RGB input range (auto: AVI InfoFrame).")
    p.add_argument("value", choices=["auto", "limited", "full"])
    p = sub.add_parser("audio-batch", help="Audio packets per GPIF thread switch (1-8).")
    p.add_argument("packets", type=int)
    p = sub.add_parser("audio-source", help="Select the audio source.")
    p.add_argument("source", choices=["hdmi", "test"])

    sub.add_parser("list",   help="List Cam Link related USB devices.")
    sub.add_parser("ident",  help="Show LiteCamLink firmware identification.")
    sub.add_parser("reboot", help="Reboot the FX3 (back to the USB bootloader).")
    p = sub.add_parser("peek", help="Read FX3 memory (32-bit).")
    p.add_argument("addr", type=lambda x: int(x, 0))
    p.add_argument("--count", type=int, default=1)
    p = sub.add_parser("poke", help="Write FX3 memory (32-bit).")
    p.add_argument("addr",  type=lambda x: int(x, 0))
    p.add_argument("value", type=lambda x: int(x, 0))

    args = parser.parse_args()

    if args.cmd == "fx3-load":
        fx3_load(args.image)

    if args.cmd == "boot":
        if find_device(FX3_BOOT_VID, FX3_BOOT_PID) is not None:
            fx3_load(args.fx3)
            time.sleep(1.0) # Let the host (uvcvideo) finish enumeration/probing.
        cl = CamLink()
        for retry in range(5):
            try:
                print(cl.ident())
                cl.fpga_load(args.bit)
                cl.hdmi_init()
                break
            except usb.core.USBError:
                time.sleep(0.5)

    if args.cmd == "fpga-load":
        CamLink().fpga_load(args.bitstream)

    if args.cmd == "fpga-info":
        idcode, status = CamLink().fpga_info()
        print(f"IDCODE: 0x{idcode:08x}, status: 0x{status:08x} (done: {(status >> 8) & 1})")

    if args.cmd == "pintest":
        sys.exit(0 if pintest(CamLink()) else 1)

    if args.cmd == "i2c-scan":
        print(" ".join(f"0x{a:02x}" for a in CamLink().i2c_scan()))

    if args.cmd == "i2c-dump":
        cl   = CamLink()
        data = b"".join(cl.i2c_read(args.addr, bytes([r]), 32) for r in range(0, 256, 32))
        print("     " + " ".join(f"{i:02x}" for i in range(16)))
        for r in range(0, 256, 16):
            print(f"{r:02x} : " + " ".join(f"{b:02x}" for b in data[r:r + 16]))

    if args.cmd == "i2c-write":
        CamLink().i2c_write(args.addr, bytes([args.reg]), bytes([args.value]))

    if args.cmd == "csr":
        bus = CamLinkBus(csr_csv=args.csr_csv)
        names = [args.name] if args.name else list(bus.regs.d.keys())
        for name in names:
            reg = getattr(bus.regs, name)
            if args.value is not None:
                reg.write(args.value)
            else:
                print(f"{name:32s}: 0x{reg.read():08x}")

    if args.cmd == "stream-test":
        cl = CamLink()
        ok = stream_test(cl, CamLinkBus(cl, csr_csv=args.csr_csv), size=args.size*1024*1024, clk_div_x2=args.clk_div_x2,
            flag_omega=args.flag_omega, flag_invert=args.flag_invert, data_delay=args.data_delay)
        sys.exit(0 if ok else 1)

    if args.cmd == "uvc-raw-test":
        cl = CamLink()
        ok = uvc_raw_test(cl, CamLinkBus(cl, csr_csv=args.csr_csv), args.width, args.height, args.fps, args.frames)
        sys.exit(0 if ok else 1)

    if args.cmd == "stream-status":
        for k, v in CamLink().stream_status().items():
            print(f"{k:16s}: 0x{v:08x}")

    if args.cmd == "term":
        term(CamLinkBus(csr_csv=args.csr_csv), cmds=args.commands, duration=args.time)

    if args.cmd == "hdmi-status":
        for k, v in CamLink().hdmi_status().items():
            print(f"{k:12s}: {v}")

    if args.cmd == "flash-id":
        print(f"JEDEC ID: 0x{CamLink().flash_id():06x}")

    if args.cmd == "flash-dump":
        data = CamLink().flash_read(0, 0x400000)
        open(args.filename, "wb").write(data)
        print(f"Dumped {len(data)} bytes to {args.filename}.")

    if args.cmd == "flash-write":
        CamLink().flash_write(args.offset, open(args.filename, "rb").read())

    if args.cmd == "flash-bitstream":
        CamLink().flash_bitstream(args.bitstream)

    if args.cmd == "flash-fx3":
        image = open(args.image, "rb").read()
        parse_fx3_image(image) # Check signature/checksum.
        CamLink().flash_write(0, image)

    if args.cmd == "flash-recover":
        CamLink().flash_recover()

    if args.cmd == "stats":
        for k, v in CamLink().stats().items():
            print(f"{k:22s}: {v}")

    if args.cmd == "crop":
        CamLink().crop(args.x, args.y)

    if args.cmd == "range":
        CamLink().range(args.value)

    if args.cmd == "audio-batch":
        CamLink().audio_batch(args.packets)

    if args.cmd == "audio-source":
        CamLink().audio_test(args.source == "test")

    if args.cmd == "fpga-boot":
        print(f"FPGA status: 0x{CamLink().fpga_boot():08x}")

    if args.cmd == "ident":
        cl  = CamLink()
        spd = {3: "High-Speed", 4: "SuperSpeed"}.get(cl.dev.speed, str(cl.dev.speed))
        print(f"{cl.ident()} ({spd})")

    if args.cmd == "reboot":
        CamLink().reboot()

    if args.cmd == "peek":
        cl = CamLink()
        for i in range(args.count):
            addr = args.addr + 4*i
            print(f"0x{addr:08x}: 0x{cl.read32(addr):08x}")

    if args.cmd == "poke":
        CamLink().write32(args.addr, args.value)

    if args.cmd == "list":
        for dev in usb.core.find(find_all=True):
            ids = (dev.idVendor, dev.idProduct)
            if ids in [(FX3_BOOT_VID, FX3_BOOT_PID), (LITECAMLINK_VID, LITECAMLINK_PID)] or \
               (dev.idVendor == STOCK_VID and dev.idProduct in STOCK_PIDS):
                print(f"{dev.idVendor:04x}:{dev.idProduct:04x} bus {dev.bus} addr {dev.address} speed {dev.speed}")

if __name__ == "__main__":
    main()
