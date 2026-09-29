#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *

from litecamlink.gateware.watchdog import FX3Watchdog

# Test ---------------------------------------------------------------------------------------------

def run(beats, gap, cycles, arm_edges=4):
    """`beats` heartbeat toggles every `gap` cycles, then silence; returns reset activity."""
    hb  = Signal()
    dut = FX3Watchdog(hb, sys_clk_freq=1000, period=0.1, pulse=0.01, arm_edges=arm_edges)
    log = {"reset_cycles": 0, "first_reset": None}
    def gen():
        for i in range(cycles):
            if i < beats*gap and i % gap == 0:
                yield hb.eq(~(yield hb))
            if (yield dut.reset):
                log["reset_cycles"] += 1
                if log["first_reset"] is None:
                    log["first_reset"] = i
            yield
        log["resets"] = (yield dut.resets.status)
        log["armed"]  = (yield dut.status.fields.armed)
    run_simulation(dut, gen())
    return log

def test_watchdog_no_heartbeat():
    # Never armed: no reset.
    assert run(beats=0, gap=10, cycles=500)["resets"] == 0

def test_watchdog_few_edges():
    # Less than arm_edges edges (e.g. a floating input glitch): not armed.
    assert run(beats=3, gap=10, cycles=500)["resets"] == 0

def test_watchdog_alive():
    # Heartbeat faster than the period: no reset.
    r = run(beats=60, gap=50, cycles=3000)
    assert r["resets"] == 0 and r["armed"]

def test_watchdog_fires_once():
    # Heartbeat stops at 500: one reset pulse (10 cycles) ~100 cycles later, then disarmed.
    r = run(beats=10, gap=50, cycles=2000)
    assert r["resets"] == 1 and r["reset_cycles"] == 10 and not r["armed"]
    assert 550 <= r["first_reset"] <= 560
