#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.sim import passive

from litex.gen import *

from litex.soc.interconnect.csr_bus import CSRBank, Interface

from gateware.audio import I2SReceiver, AudioSource

# I2S Model ----------------------------------------------------------------------------------------

class I2SPads:
    def __init__(self):
        self.sck  = Signal()
        self.ws   = Signal()
        self.sd   = Signal()
        self.mclk = Signal()

def i2s_transmit(pads, samples, bits=32, half=3):
    """Standard I2S: 32 SCK per channel, MSB one SCK after the WS edge, WS low = left."""
    for left, right in samples:
        for ch, value in ((0, left), (1, right)):
            for bit in range(bits):
                # Data changes on the falling edge, sampled on the rising edge.
                if bit == 0:
                    yield pads.ws.eq(ch)
                    b = 0 # LSB of the previous word (don't care).
                else:
                    pos = bit - 1
                    b   = (value >> (15 - pos)) & 1 if pos < 16 else 0
                yield pads.sd.eq(b)
                yield pads.sck.eq(0)
                for _ in range(half):
                    yield
                yield pads.sck.eq(1)
                for _ in range(half):
                    yield

# Tests --------------------------------------------------------------------------------------------

def test_i2s_receiver():
    pads    = I2SPads()
    dut     = I2SReceiver(pads)
    samples = [(0x1234, 0xabcd), (0x8001, 0x7ffe), (0xffff, 0x0000), (0x5a5a, 0xa5a5)]
    out     = []

    @passive
    def monitor():
        while True:
            yield
            if (yield dut.source.valid):
                out.append((yield dut.source.data))

    run_simulation(dut, [i2s_transmit(pads, samples), monitor()])
    # First sample may be partial (receiver syncs on the first WS edge).
    expected = [l | (r << 16) for l, r in samples]
    assert out[-3:] == expected[-3:]

def test_audio_source_test_counter():
    pads = I2SPads()
    dut  = AudioSource(pads, sys_clk_freq=48000*8, with_csr=False)
    out  = []

    def generator():
        yield dut.enable.eq(1)
        yield dut.test.eq(1)
        yield dut.source.ready.eq(1)
        for _ in range(8*10):
            yield
            if (yield dut.source.valid):
                out.append((yield dut.source.data))

    run_simulation(dut, generator())
    assert len(out) >= 8
    counters = [d & 0xffff for d in out]
    assert all((b - a) & 0xffff == 1 for a, b in zip(counters, counters[1:]))
    assert all(((d >> 16) ^ d) & 0xffff == 0xffff for d in out)

def test_audio_source_silence_without_i2s():
    # No I2S: zero samples at the 48kHz rate after 2 sample periods; I2S samples when active.
    pads = I2SPads()
    dut  = AudioSource(pads, sys_clk_freq=48000*8, with_csr=False)
    out  = []

    def generator():
        yield dut.enable.eq(1)
        yield dut.source.ready.eq(1)
        for i in range(8*20):
            yield
            if (yield dut.source.valid):
                out.append((i, (yield dut.source.data)))

    run_simulation(dut, generator())
    assert len(out) >= 15 and all(d == 0 for _, d in out)
    assert out[0][0] < 8*5

def test_audio_source_i2s_after_silence():
    pads    = I2SPads()
    dut     = AudioSource(pads, sys_clk_freq=48000*400, with_csr=False) # 400 cycles/sample (I2S model: 384).
    samples = [(0x1111*(i + 1) & 0xffff, 0x2222) for i in range(6)]
    out     = []

    def generator():
        yield dut.enable.eq(1)
        yield dut.source.ready.eq(1)
        for _ in range(400*6):
            yield
        yield from i2s_transmit(pads, samples)

    @passive
    def monitor():
        while True:
            yield
            if (yield dut.source.valid):
                out.append((yield dut.source.data))

    run_simulation(dut, [generator(), monitor()])
    assert 0 in out[:3]
    assert out[-3:] == [l | (r << 16) for l, r in samples][-3:]

def test_audio_source_csr():
    # CSR control fields reach the control signals, the counters reach the status CSRs.
    pads  = I2SPads()
    dut   = AudioSource(pads, sys_clk_freq=48000*8)
    names = [c.name for c in dut.get_csrs()]
    assert names == ["control", "samples", "overflow", "silences"]
    bank   = CSRBank(dut.get_csrs(), bus=Interface(data_width=32))
    dut.submodules.bank = bank
    values = {}

    def generator():
        yield from bank.bus.write(names.index("control"), 0b11) # enable + test.
        yield
        values["enable"] = (yield dut.enable)
        values["test"]   = (yield dut.test)
        yield dut.source.ready.eq(1)
        for _ in range(8*10):
            yield
        # Bank read data registered: one more cycle than Interface.read waits for.
        yield bank.bus.adr.eq(names.index("samples"))
        yield
        yield
        values["samples"] = (yield bank.bus.dat_r)

    run_simulation(dut, generator())
    assert values["enable"] == 1 and values["test"] == 1
    assert values["samples"] >= 8
