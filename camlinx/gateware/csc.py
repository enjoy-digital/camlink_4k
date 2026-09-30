#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""CSC: RGB 4:4:4 pixel pairs -> YCbCr 4:2:2 (programmable matrix, e.g. BT.709 full/limited range).

Per pixel pair (DDR capture: 2 pixels per clock):

    Y  = ((ky_r*R' + ky_g*G' + ky_b*B') >> 10) + y_off          (both pixels)
    Cb = ((kcb_r*R'' + kcb_g*G'' + kcb_b*B'') >> 10) + c_off    (pair average R'', G'', B'')
    Cr = ((kcr_r*R'' + kcr_g*G'' + kcr_b*B'') >> 10) + c_off
    X' = X - in_off                                             (limited range inputs: 16)

Coefficients are signed Q1.10 (1024 = 1.0). Outputs are clamped to [0, 255]. Fixed latency of
`LATENCY` clocks (12 multipliers: 6 luma, 6 chroma, with dedicated registered operands so the
DSP input/output registers are used).
"""

from migen import *

from litex.gen import *

# Coefficients -------------------------------------------------------------------------------------

def bt709_coefficients(full_range_input=True):
    """BT.709 RGB -> YCbCr (limited range output): (ky, kcb, kcr as (r, g, b)), y_off, c_off, in_off."""
    kr, kg, kb = 0.2126, 0.7152, 0.0722
    if full_range_input:
        ys, cs, in_off = 219/255, 224/255, 0
    else:
        ys, cs, in_off = 1.0, 224/219, 16
    ky  = (kr*ys, kg*ys, kb*ys)
    kcb = (-kr/(2*(1 - kb))*cs, -kg/(2*(1 - kb))*cs, 0.5*cs)
    kcr = (0.5*cs, -kg/(2*(1 - kr))*cs, -kb/(2*(1 - kr))*cs)
    q = lambda k: tuple(int(round(v*1024)) for v in k)
    return q(ky), q(kcb), q(kcr), 16, 128, in_off

# Registered Multiplier ----------------------------------------------------------------------------

class RegisteredMultiplier(LiteXModule):
    """p = a*b (signed), registered inputs and output (2 clocks). ECP5: MULT18X18D with its internal
    input/output registers (yosys does not pack fabric registers into the DSP; the combinational DSP
    path limited the HDMI domain to ~144MHz). Behavioural model in simulation."""
    def __init__(self, a, b, width=22, sim=False):
        self.p = p = Signal((width, True))

        # # #

        if sim:
            ar = Signal.like(a)
            br = Signal.like(b)
            self.sync += [ar.eq(a), br.eq(b), p.eq(ar*br)]
        else:
            a18 = Signal((18, True))
            b18 = Signal((18, True))
            p36 = Signal((36, True))
            self.comb += [a18.eq(a), b18.eq(b), p.eq(p36)]
            params = dict(
                p_REG_INPUTA_CLK = "CLK0", p_REG_INPUTA_CE = "CE0", p_REG_INPUTA_RST = "RST0",
                p_REG_INPUTB_CLK = "CLK0", p_REG_INPUTB_CE = "CE0", p_REG_INPUTB_RST = "RST0",
                p_REG_INPUTC_CLK = "NONE",
                p_REG_PIPELINE_CLK = "NONE",
                p_REG_OUTPUT_CLK = "CLK0", p_REG_OUTPUT_CE = "CE0", p_REG_OUTPUT_RST = "RST0",
                p_CLK0_DIV = "ENABLED", p_CLK1_DIV = "ENABLED", p_CLK2_DIV = "ENABLED", p_CLK3_DIV = "ENABLED",
                p_GSR = "DISABLED", p_RESETMODE = "SYNC", p_MULT_BYPASS = "DISABLED",
                p_CAS_MATCH_REG = "FALSE", p_SOURCEB_MODE = "B_SHIFT",
                i_CLK0 = ClockSignal(), i_CE0 = 1, i_RST0 = 0,
                i_SIGNEDA = 1, i_SIGNEDB = 1, i_SOURCEA = 0, i_SOURCEB = 0,
            )
            for i in range(18):
                params[f"i_A{i}"] = a18[i]
                params[f"i_B{i}"] = b18[i]
                params[f"i_C{i}"] = 0
            for i in range(36):
                params[f"o_P{i}"] = p36[i]
            self.specials += Instance("MULT18X18D", **params)

# RGB -> YCbCr 4:2:2 -------------------------------------------------------------------------------

class RGB2YCbCr422(LiteXModule):
    LATENCY = 5

    def __init__(self, sim=False):
        # Inputs (pixel pair).
        self.r0, self.g0, self.b0 = Signal(8), Signal(8), Signal(8)
        self.r1, self.g1, self.b1 = Signal(8), Signal(8), Signal(8)
        # Configuration (static).
        self.ky  = [Signal((12, True)) for _ in range(3)] # (r, g, b).
        self.kcb = [Signal((12, True)) for _ in range(3)]
        self.kcr = [Signal((12, True)) for _ in range(3)]
        self.y_off  = Signal(8)
        self.c_off  = Signal(8)
        self.in_off = Signal(8)
        # Outputs.
        self.y0, self.y1, self.cb, self.cr = Signal(8), Signal(8), Signal(8), Signal(8)

        # # #

        # Stage 1: input offset, pair average.
        def centered(x):
            s = Signal((10, True))
            self.sync += s.eq(x - self.in_off)
            return s
        def average(a, b):
            s = Signal(9)
            m = Signal((10, True))
            self.comb += s.eq(a + b + 1)
            self.sync += m.eq(s[1:] - self.in_off)
            return m
        p0  = [centered(self.r0), centered(self.g0), centered(self.b0)]
        p1  = [centered(self.r1), centered(self.g1), centered(self.b1)]
        avg = [average(self.r0, self.r1), average(self.g0, self.g1), average(self.b0, self.b1)]

        # Stages 2-3: products (DSP registered inputs/output), stage 4: sums.
        def dot(k, v):
            products = []
            for i in range(3):
                mult = RegisteredMultiplier(k[i], v[i], sim=sim)
                self.submodules += mult
                products.append(mult.p)
            s = Signal((24, True))
            self.sync += s.eq(products[0] + products[1] + products[2])
            return s
        sy0 = dot(self.ky,  p0)
        sy1 = dot(self.ky,  p1)
        scb = dot(self.kcb, avg)
        scr = dot(self.kcr, avg)

        # Stage 5: scale, offset, clamp.
        def output(s, off, o):
            v = Signal((16, True))
            self.comb += v.eq((s >> 10) + off)
            self.sync += If(v < 0, o.eq(0)).Elif(v > 255, o.eq(255)).Else(o.eq(v[:8]))
        output(sy0, self.y_off, self.y0)
        output(sy1, self.y_off, self.y1)
        output(scb, self.c_off, self.cb)
        output(scr, self.c_off, self.cr)
