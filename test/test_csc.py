#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *

from camlinx.gateware.csc import RGB2YCbCr422, bt709_coefficients

def model(p0, p1, coefs):
    ky, kcb, kcr, y_off, c_off, in_off = coefs
    clamp = lambda v: max(0, min(255, v))
    def dot(k, v):
        return sum(ki*(vi - in_off) for ki, vi in zip(k, v)) >> 10
    avg = [(a + b + 1) >> 1 for a, b in zip(p0, p1)]
    return (clamp(dot(ky, p0) + y_off), clamp(dot(ky, p1) + y_off),
            clamp(dot(kcb, avg) + c_off), clamp(dot(kcr, avg) + c_off))

def run(pairs, coefs):
    dut = RGB2YCbCr422(sim=True)
    out = []
    def gen():
        ky, kcb, kcr, y_off, c_off, in_off = coefs
        for sigs, vals in ((dut.ky, ky), (dut.kcb, kcb), (dut.kcr, kcr)):
            for s, v in zip(sigs, vals):
                yield s.eq(v)
        yield dut.y_off.eq(y_off)
        yield dut.c_off.eq(c_off)
        yield dut.in_off.eq(in_off)
        for p0, p1 in pairs + [((0, 0, 0), (0, 0, 0))]*dut.LATENCY:
            for s, v in zip((dut.r0, dut.g0, dut.b0, dut.r1, dut.g1, dut.b1), p0 + p1):
                yield s.eq(v)
            yield
            out.append(((yield dut.y0), (yield dut.y1), (yield dut.cb), (yield dut.cr)))
    run_simulation(dut, gen())
    return out[dut.LATENCY:dut.LATENCY + len(pairs)]

def test_csc_bars_full_range():
    coefs = bt709_coefficients(full_range_input=True)
    white, black = (255, 255, 255), (0, 0, 0)
    out = run([(white, white), (black, black), ((191, 191, 0), (191, 191, 0))], coefs)
    assert out[0] == (235, 235, 128, 128) or all(abs(a - b) <= 1 for a, b in zip(out[0], (235, 235, 128, 128)))
    assert all(abs(a - b) <= 1 for a, b in zip(out[1], (16, 16, 128, 128)))
    # 75% yellow (BT.709): Y 168, Cb 44, Cr 136.
    assert all(abs(a - b) <= 2 for a, b in zip(out[2], (168, 168, 44, 136)))

def test_csc_limited_range():
    coefs = bt709_coefficients(full_range_input=False)
    out = run([((235, 235, 235), (16, 16, 16))], coefs)
    y0, y1, cb, cr = out[0]
    assert abs(y0 - 235) <= 1 and abs(y1 - 16) <= 1
    assert abs(cb - 128) <= 1 and abs(cr - 128) <= 1

def test_csc_random():
    random.seed(0)
    coefs = bt709_coefficients(True)
    pairs = [(tuple(random.randrange(256) for _ in range(3)), tuple(random.randrange(256) for _ in range(3)))
        for _ in range(64)]
    assert run(pairs, coefs) == [model(p0, p1, coefs) for p0, p1 in pairs]
