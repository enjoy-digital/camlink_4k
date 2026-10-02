#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *
from migen.sim import passive

from litex.gen import *

from litex.soc.interconnect.csr_bus import CSRBank, Interface

from litedram.common import LiteDRAMNativePort

from gateware.framebuffer import NV12FrameBuffer

# Geometry: 16-byte lines (1 port word of 128 bits = 4 words), 4 lines.
LINE_WORDS  = 1
HEIGHT      = 4
FRAME_WORDS = 4*LINE_WORDS*HEIGHT*3//2 # 32-bit words (Y + UV).
SLOT_WORDS  = 16

# M420 / NV12 Frames -------------------------------------------------------------------------------

def m420_frame(tag, truncate=None):
    """M420 words of frame `tag`: per line pair even Y line, odd Y line, UV line."""
    words = []
    for pair in range(HEIGHT//2):
        for line in (2*pair, 2*pair + 1):
            words += [(tag << 24) | (0x10 + line) << 16 | j for j in range(4*LINE_WORDS)]
        words += [(tag << 24) | (0x80 + pair) << 16 | j for j in range(4*LINE_WORDS)]
    return words[:truncate] if truncate else words

def nv12_frame(tag):
    """NV12 words of frame `tag`: Y plane then UV plane."""
    y  = [(tag << 24) | (0x10 + line) << 16 | j
        for line in range(HEIGHT) for j in range(4*LINE_WORDS)]
    uv = [(tag << 24) | (0x80 + pair) << 16 | j
        for pair in range(HEIGHT//2) for j in range(4*LINE_WORDS)]
    return y + uv

# Native Port Memory Model -------------------------------------------------------------------------

class Memory:
    def __init__(self):
        self.data = {}

    @passive
    def write_port(self, port):
        addrs = []
        while True:
            yield port.cmd.ready.eq(random.random() < 0.7)
            yield port.wdata.ready.eq(random.random() < 0.7)
            yield
            if (yield port.cmd.valid) and (yield port.cmd.ready):
                assert (yield port.cmd.we)
                addrs.append((yield port.cmd.addr))
            if (yield port.wdata.valid) and (yield port.wdata.ready):
                self.data[addrs.pop(0)] = (yield port.wdata.data)

    @passive
    def read_port(self, port, latency=6):
        pending = []
        cycle   = 0
        while True:
            yield port.cmd.ready.eq(random.random() < 0.7)
            # Responses in order after `latency` cycles.
            if pending and pending[0][0] <= cycle:
                yield port.rdata.valid.eq(1)
                yield port.rdata.data.eq(self.data.get(pending[0][1], 0))
            else:
                yield port.rdata.valid.eq(0)
            yield
            cycle += 1
            if (yield port.cmd.valid) and (yield port.cmd.ready):
                assert not (yield port.cmd.we)
                pending.append((cycle + latency, (yield port.cmd.addr)))
            if (yield port.rdata.valid) and (yield port.rdata.ready):
                pending.pop(0)

# Test Runner --------------------------------------------------------------------------------------

def run(frames, gap=0, out_ready=0.8, cycles=6000, seed=0, stops=()):
    random.seed(seed)
    wport = LiteDRAMNativePort("both", 24, 128)
    rport = LiteDRAMNativePort("both", 24, 128)
    dut   = NV12FrameBuffer(wport, rport, with_csr=False)
    mem   = Memory()
    out   = []

    def config():
        yield dut.base.eq(0x100)
        yield dut.slot_words.eq(SLOT_WORDS)
        yield dut.line_words.eq(LINE_WORDS)
        yield dut.height.eq(HEIGHT)
        yield dut.uv_offset.eq(HEIGHT*LINE_WORDS)
        yield dut.frame_words.eq(FRAME_WORDS)
        yield
        yield dut.enable.eq(1)
        for _ in range(4):
            yield

    def source():
        yield from config()
        for words in frames:
            for i, w in enumerate(words):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(w)
                yield dut.sink.first.eq(i == 0)
                yield dut.sink.last.eq(i == len(words) - 1)
                yield
                while not (yield dut.sink.ready):
                    yield
            yield dut.sink.valid.eq(0)
            for _ in range(gap):
                yield

    def sink():
        current = []
        for _ in range(cycles):
            yield dut.source.ready.eq(random.random() < out_ready)
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                if (yield dut.source.first):
                    current = []
                current.append((yield dut.source.data))
                if (yield dut.source.last):
                    out.append(current)
                    current = []

    def stopper():
        # Stop pulses (SoC stream stop/restart) at the given cycles.
        cycle = 0
        for start, length in stops:
            while cycle < start:
                yield
                cycle += 1
            yield dut.stop.eq(1)
            for _ in range(length):
                yield
                cycle += 1
            yield dut.stop.eq(0)

    run_simulation(dut, [source(), sink(), stopper(), mem.write_port(wport), mem.read_port(rport)])
    return out

# Tests --------------------------------------------------------------------------------------------

def test_framebuffer_nv12_order():
    # Frames spaced out (the reader keeps up): each frame output once, in order, NV12 layout.
    out = run([m420_frame(t) for t in range(1, 4)], gap=200)
    assert out == [nv12_frame(t) for t in range(1, 4)]

def test_framebuffer_truncated_frame_dropped():
    frames = [m420_frame(1), m420_frame(2, truncate=10), m420_frame(3)]
    out    = run(frames, gap=200)
    assert out == [nv12_frame(1), nv12_frame(3)]

def test_framebuffer_slow_output_skips_frames():
    # Output slower than the input: frames are skipped, output frames are complete, unique and in
    # order (several random timings: slot reuse races).
    for seed in range(6):
        out = run([m420_frame(t) for t in range(1, 9)], gap=seed*7, out_ready=0.2, seed=seed)
        assert len(out) >= 2
        tags = [f[0] >> 24 for f in out]
        assert tags == sorted(tags) and len(set(tags)) == len(tags), (seed, tags)
        for f in out:
            assert f == nv12_frame(f[0] >> 24), seed

def test_framebuffer_stop_restart():
    # Stops in the middle of frames (input and output): the outstanding DRAM reads drain (no
    # reset), the output resumes with complete, correct frames after each stop.
    for seed in range(4):
        out = run([m420_frame(t) for t in range(1, 13)],
            gap       = 50,
            out_ready = 0.5,
            cycles    = 8000,
            seed      = seed,
            stops     = [(400 + 97*seed, 20), (1500 + 31*seed, 3), (2600, 60)],
        )
        complete = [f for f in out if len(f) == FRAME_WORDS and f[0] == nv12_frame(f[0] >> 24)[0]]
        assert len(complete) >= 3, seed
        # Frames after the last stop are complete and correct.
        tags = [f[0] >> 24 for f in out]
        assert out[-1] == nv12_frame(tags[-1]), seed

def test_framebuffer_csr():
    # CSR writes reach the control signals (CSR map: enable, base, slot_words, line_words, height,
    # uv_offset, frame_words, written, dropped, read, in_stalls, wr_stalls, debug).
    wport = LiteDRAMNativePort("both", 24, 128)
    rport = LiteDRAMNativePort("both", 24, 128)
    dut   = NV12FrameBuffer(wport, rport)
    names = [c.name for c in dut.get_csrs()]
    assert names == ["enable", "base", "slot_words", "line_words", "height", "uv_offset",
        "frame_words", "written", "dropped", "read", "in_stalls", "wr_stalls", "debug"]
    bank   = CSRBank(dut.get_csrs(), bus=Interface(data_width=32))
    dut.submodules.bank = bank
    values = {}

    def generator():
        yield from bank.bus.write(names.index("enable"),      1)
        yield from bank.bus.write(names.index("base"),        0x123)
        yield from bank.bus.write(names.index("frame_words"), FRAME_WORDS)
        yield
        values["enable"]      = (yield dut.enable)
        values["base"]        = (yield dut.base)
        values["frame_words"] = (yield dut.frame_words)

    run_simulation(dut, generator())
    assert values == {"enable": 1, "base": 0x123, "frame_words": FRAME_WORDS}
