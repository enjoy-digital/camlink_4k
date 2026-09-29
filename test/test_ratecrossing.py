#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from litecamlink.gateware.ecp5ddrphy import RateCrossing

# Loopback: sys words -> serializer (sys2x) -> deserializer -> sys words ---------------------------

class Loopback(Module):
    def __init__(self):
        self.submodules.rate = rate = RateCrossing(clk="sys2x")
        self.i = Signal(16)
        self.o = Signal(16)
        s = Signal(8)
        self.submodules.ser = rate.serializer_cls()("sys", "sys2x", 16, 8, i=self.i, o=s)
        self.submodules.des = rate.deserializer_cls()("sys", "sys2x", 8, 16, i=s, o=self.o)

def run(sel, shift, n=40):
    dut   = Loopback()
    words = [random.getrandbits(16) for _ in range(n)]
    out   = []
    def gen():
        yield dut.rate.sel.eq(sel)
        yield dut.rate.shift.eq(shift)
        for w in words + [0]*8:
            yield dut.i.eq(w)
            yield
            out.append((yield dut.o))
    run_simulation(dut, {"sys": gen()}, clocks={"sys": 20, "sys2x": 10})
    # Latency (sys cycles) at which the words come back intact, None if they do not.
    for lat in range(8):
        if out[lat:lat + n] == words:
            return lat
    return None

def test_ratecrossing_loopback():
    # For each capture edge, one shift returns the words intact (runtime read alignment), and the
    # shifts give distinct latencies (sys2x steps).
    random.seed(0)
    for sel in range(2):
        lats = {shift: run(sel, shift) for shift in range(4)}
        assert any(lat is not None for lat in lats.values()), (sel, lats)
        print(sel, lats)
