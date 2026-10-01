#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""I2CBridge: I2C slave to Wishbone master bridge (host access to the SoC through the FX3).

Protocol (big endian, byte addresses, 32-bit words):
- Write : S | ADDR+W | A3 A2 A1 A0 | [D3 D2 D1 D0]... | P  -> one Wishbone write per data word.
- Read  : S | ADDR+R | D3 D2 D1 D0 | ... | P               -> reads from the last written address.
The address auto-increments by 4 after each word.
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect import wishbone

# I2C Bridge ---------------------------------------------------------------------------------------

class I2CBridge(LiteXModule):
    def __init__(self, pads, address=0x10):
        self.bus = bus = wishbone.Interface(data_width=32, address_width=32, addressing="word")

        # # #

        # SCL/SDA synchronization (SDA open-drain: only driven low).
        scl    = Signal(reset=1)
        sda    = Signal(reset=1)
        scl_d  = Signal(reset=1)
        sda_d  = Signal(reset=1)
        sda_oe = Signal()
        if hasattr(pads, "sda_i"):
            # Simulation pads.
            self.specials += MultiReg(pads.scl,   scl, reset=1)
            self.specials += MultiReg(pads.sda_i, sda, reset=1)
            self.comb += pads.sda_oe.eq(sda_oe)
        else:
            sda_t = TSTriple()
            self.specials += sda_t.get_tristate(pads.sda)
            self.comb += [sda_t.o.eq(0), sda_t.oe.eq(sda_oe)]
            self.specials += MultiReg(pads.scl, scl, reset=1)
            self.specials += MultiReg(sda_t.i,  sda, reset=1)
        self.sync += [scl_d.eq(scl), sda_d.eq(sda)]

        scl_rise = Signal()
        scl_fall = Signal()
        start    = Signal()
        stop     = Signal()
        self.comb += [
            scl_rise.eq( scl & ~scl_d),
            scl_fall.eq(~scl &  scl_d),
            start.eq(scl & scl_d &  sda_d & ~sda),
            stop.eq( scl & scl_d & ~sda_d &  sda),
        ]

        # Datapath.
        bitcount = Signal(4)
        shift    = Signal(8)
        byte     = Signal(8)
        bytecnt  = Signal(3)
        addr       = Signal(32)
        addr_shift = Signal(32)
        addr_load  = Signal()
        wdata    = Signal(32)
        rdata    = Signal(32)
        rw       = Signal()
        master_ack = Signal()

        # Wishbone accesses.
        wb_write = Signal()
        wb_read  = Signal()
        wb_busy  = Signal()
        wb_we    = Signal()
        self.comb += [
            bus.adr.eq(addr[2:]),
            bus.dat_w.eq(wdata),
            bus.sel.eq(0xf),
            bus.we.eq(wb_we),
            bus.cyc.eq(wb_busy),
            bus.stb.eq(wb_busy),
        ]
        self.sync += [
            If(wb_busy & bus.ack,
                wb_busy.eq(0),
                If(~wb_we, rdata.eq(bus.dat_r)),
                addr.eq(addr + 4),
            ).Elif(addr_load,
                addr.eq(Cat(byte, addr_shift[:24])),
            ).Elif(wb_write,
                wb_busy.eq(1),
                wb_we.eq(1),
            ).Elif(wb_read,
                wb_busy.eq(1),
                wb_we.eq(0),
            )
        ]

        # FSM.
        rshift = Signal(32)
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            NextValue(sda_oe, 0),
        )
        fsm.act("ADDR",
            If(scl_rise,
                NextValue(shift, Cat(sda, shift[:7])),
                NextValue(bitcount, bitcount + 1),
            ),
            If(scl_fall & (bitcount == 8),
                If(shift[1:] == address,
                    NextValue(rw, shift[0]),
                    NextValue(sda_oe, 1),
                    NextValue(bytecnt, 0),
                    NextState("ADDR-ACK"),
                ).Else(
                    NextState("IDLE"),
                )
            )
        )
        fsm.act("ADDR-ACK",
            If(scl_rise & rw, wb_read.eq(1)), # Fetch first read word during the ACK clock.
            If(scl_fall,
                NextValue(sda_oe, 0),
                NextValue(bitcount, 0),
                If(rw,
                    NextState("READ-LOAD"),
                ).Else(
                    NextState("WRITE"),
                )
            )
        )
        fsm.act("WRITE",
            If(scl_rise,
                NextValue(shift, Cat(sda, shift[:7])),
                NextValue(bitcount, bitcount + 1),
            ),
            If(scl_fall & (bitcount == 8),
                NextValue(sda_oe, 1),
                NextValue(byte, shift),
                NextState("WRITE-ACK"),
            )
        )
        fsm.act("WRITE-ACK",
            If(scl_rise,
                # First 4 bytes: address, then 32-bit data words.
                If(bytecnt < 4,
                    NextValue(addr_shift, Cat(byte, addr_shift[:24])),
                ).Else(
                    NextValue(wdata, Cat(byte, wdata[:24])),
                ),
                If(bytecnt == 3,
                    addr_load.eq(1),
                ),
                If(bytecnt == 7,
                    NextValue(bytecnt, 4),
                    wb_write.eq(1),
                ).Else(
                    NextValue(bytecnt, bytecnt + 1),
                )
            ),
            If(scl_fall,
                NextValue(sda_oe, 0),
                NextValue(bitcount, 0),
                NextState("WRITE"),
            )
        )
        fsm.act("READ-LOAD",
            # Wishbone access is much faster than an I2C bit: wait for data, drive MSB.
            If(~wb_busy,
                If(bytecnt == 0,
                    NextValue(sda_oe, ~rdata[31]),
                    NextValue(shift,  rdata[24:32]),
                    NextValue(rshift, Cat(Constant(0, 8), rdata[:24])),
                ).Else(
                    NextValue(sda_oe, ~rshift[31]),
                    NextValue(shift,  rshift[24:32]),
                    NextValue(rshift, Cat(Constant(0, 8), rshift[:24])),
                ),
                NextValue(bitcount, 1),
                NextState("READ"),
            )
        )
        fsm.act("READ",
            If(scl_fall,
                If(bitcount == 8,
                    NextValue(sda_oe, 0), # Release for master ACK.
                    NextState("READ-ACK"),
                ).Else(
                    NextValue(sda_oe, ~shift[6]),
                    NextValue(shift, Cat(Constant(0, 1), shift[:7])),
                    NextValue(bitcount, bitcount + 1),
                )
            )
        )
        fsm.act("READ-ACK",
            If(scl_rise,
                NextValue(master_ack, ~sda),
                If((bytecnt == 3) & ~sda, wb_read.eq(1)), # Prefetch next word.
            ),
            If(scl_fall,
                If(~master_ack,
                    NextState("IDLE"),
                ).Else(
                    NextValue(bytecnt, Mux(bytecnt == 3, 0, bytecnt + 1)),
                    NextState("READ-LOAD"),
                )
            )
        )

        # START/STOP: override current state (last assignment wins).
        for state in list(fsm.actions.keys()):
            fsm.act(state,
                If(start,
                    NextValue(sda_oe, 0),
                    NextValue(bitcount, 0),
                    NextState("ADDR"),
                ).Elif(stop,
                    NextValue(sda_oe, 0),
                    NextState("IDLE"),
                )
            )
