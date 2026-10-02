#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litex.gen import *

from litex.soc.interconnect import wishbone

from gateware.i2c_bridge import I2CBridge

# I2C Master Model ---------------------------------------------------------------------------------

class SimPads:
    def __init__(self):
        self.scl    = Signal(reset=1)
        self.sda_i  = Signal(reset=1) # Wired-AND bus level seen by the slave.
        self.sda_oe = Signal()        # Slave pulls SDA low.

class DUT(LiteXModule):
    def __init__(self):
        self.pads   = pads = SimPads()
        self.sda_m  = Signal(reset=1) # Master SDA drive (1 = released).
        self.bridge = I2CBridge(pads, address=0x10)
        self.sram   = wishbone.SRAM(64,
            bus=wishbone.Interface(data_width=32, address_width=32, addressing="word"))
        self.comb += self.bridge.bus.connect(self.sram.bus)
        self.comb += pads.sda_i.eq(self.sda_m & ~pads.sda_oe)

HALF = 20 # Sys clocks per half I2C bit.

def wait(n=HALF):
    for _ in range(n):
        yield

def i2c_start(dut):
    yield dut.sda_m.eq(1); yield dut.pads.scl.eq(1); yield from wait()
    yield dut.sda_m.eq(0); yield from wait()
    yield dut.pads.scl.eq(0); yield from wait()

def i2c_stop(dut):
    yield dut.sda_m.eq(0); yield from wait()
    yield dut.pads.scl.eq(1); yield from wait()
    yield dut.sda_m.eq(1); yield from wait()

def i2c_bit_write(dut, bit):
    yield dut.sda_m.eq(bit); yield from wait()
    yield dut.pads.scl.eq(1); yield from wait()
    yield dut.pads.scl.eq(0); yield from wait(2)

def i2c_bit_read(dut):
    yield dut.sda_m.eq(1); yield from wait()
    yield dut.pads.scl.eq(1); yield from wait()
    bit = (yield dut.pads.sda_i)
    yield dut.pads.scl.eq(0); yield from wait(2)
    return bit

def i2c_write_byte(dut, byte):
    for i in range(8):
        yield from i2c_bit_write(dut, (byte >> (7 - i)) & 1)
    ack = yield from i2c_bit_read(dut)
    return ack == 0

def i2c_read_byte(dut, ack=True):
    byte = 0
    for i in range(8):
        bit  = yield from i2c_bit_read(dut)
        byte = (byte << 1) | bit
    yield from i2c_bit_write(dut, 0 if ack else 1)
    return byte

# Tests --------------------------------------------------------------------------------------------

def test_i2c_bridge_write_read():
    dut     = DUT()
    results = {}

    def generator():
        # Wrong address: NACK.
        yield from i2c_start(dut)
        results["nack"] = not (yield from i2c_write_byte(dut, 0x20 << 1))
        yield from i2c_stop(dut)

        # Write 2 words at address 0x8.
        yield from i2c_start(dut)
        acks = [(yield from i2c_write_byte(dut, 0x10 << 1))]
        for b in [0x00, 0x00, 0x00, 0x08, 0x12, 0x34, 0x56, 0x78, 0xca, 0xfe, 0xba, 0xbe]:
            acks.append((yield from i2c_write_byte(dut, b)))
        yield from i2c_stop(dut)
        results["write_acks"] = all(acks)

        # Set address 0x8, read 2 words.
        yield from i2c_start(dut)
        yield from i2c_write_byte(dut, 0x10 << 1)
        for b in [0x00, 0x00, 0x00, 0x08]:
            yield from i2c_write_byte(dut, b)
        yield from i2c_stop(dut)
        yield from i2c_start(dut)
        yield from i2c_write_byte(dut, (0x10 << 1) | 1)
        data = []
        for i in range(8):
            data.append((yield from i2c_read_byte(dut, ack=(i != 7))))
        yield from i2c_stop(dut)
        results["read"] = data

    run_simulation(dut, generator())
    assert results["nack"]
    assert results["write_acks"]
    assert results["read"] == [0x12, 0x34, 0x56, 0x78, 0xca, 0xfe, 0xba, 0xbe]
