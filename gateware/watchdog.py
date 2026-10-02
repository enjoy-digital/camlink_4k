#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""FX3 hardware watchdog: resets the FX3 (RESET#) when its heartbeat stops.

The FX3 firmware toggles a GPIO (heartbeat) from its main loop. The watchdog arms after `arm_edges`
heartbeat edges while enabled (a firmware without heartbeat is never reset, a floating heartbeat
input while the FX3 is in reset/boot ROM does not arm it: pull-down + edge count), reloads `period`
on each edge and, on expiry, pulls RESET# low for `pulse` then disarms (the rebooted firmware
re-arms it with its heartbeat). Independent of the FX3 CPU state: recovers from hangs with
interrupts off or inside interrupt handlers (the FX3 internal watchdog does not reset the chip on
the Cam Link, see firmware.c). RESET# is driven open-drain style (low or released).
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *

# FX3 Watchdog -------------------------------------------------------------------------------------

class FX3Watchdog(LiteXModule):
    def __init__(self, heartbeat, sys_clk_freq, period=4.0, pulse=10e-3, arm_edges=8,
        with_csr=True):
        # FX3 reset (active high, drive RESET# low).
        self.reset = Signal()

        # Control.
        # Enable (arms after `arm_edges` heartbeat edges).
        self.enable = Signal(reset=1)
        # Timeout (sys clocks).
        self.period = Signal(32, reset=int(period*sys_clk_freq))

        # Status.
        self.armed  = Signal()   # Armed (heartbeat seen).
        self.resets = Signal(32) # FX3 resets issued (kept across FX3 resets).

        # # #

        beat    = Signal()
        beat_d  = Signal()
        edge    = Signal()
        edges   = Signal(max=arm_edges + 1)
        count   = Signal(32)
        pulse_n = Signal(max=int(pulse*sys_clk_freq) + 1)

        # Heartbeat synchronization/edge detection (both edges).
        self.specials += MultiReg(heartbeat, beat)
        self.sync += beat_d.eq(beat)
        self.comb += edge.eq(beat ^ beat_d)

        self.sync += [
            If(pulse_n != 0,
                # FX3 held in reset.
                pulse_n.eq(pulse_n - 1),
            ).Elif(~self.enable,
                self.armed.eq(0),
                edges.eq(0),
            ).Elif(edge,
                If(edges == arm_edges,
                    self.armed.eq(1),
                ).Else(
                    edges.eq(edges + 1),
                ),
                count.eq(self.period),
            ).Elif(self.armed,
                If(count == 0,
                    self.armed.eq(0),
                    edges.eq(0),
                    pulse_n.eq(int(pulse*sys_clk_freq)),
                    self.resets.eq(self.resets + 1),
                ).Else(
                    count.eq(count - 1),
                )
            )
        ]
        self.comb += self.reset.eq(pulse_n != 0)

        # CSRs.
        if with_csr:
            self.add_csr()

    def add_csr(self):
        self._control = CSRStorage(fields=[
            CSRField("enable", size=1, offset=0, reset=1,
                description="Enable (arms after `arm_edges` heartbeat edges)."),
        ])
        self._period  = CSRStorage(32, reset=self.period.reset.value,
            description="Timeout (sys clocks).")
        self._status  = CSRStatus(fields=[
            CSRField("armed", size=1, offset=0, description="Armed (heartbeat seen)."),
        ])
        self._resets  = CSRStatus(32, description="FX3 resets issued (kept across FX3 resets).")

        # # #

        self.comb += [
            self.enable.eq(self._control.fields.enable),
            self.period.eq(self._period.storage),
            self._status.fields.armed.eq(self.armed),
            self._resets.status.eq(self.resets),
        ]
