#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""Audio: IT6802 I2S capture to 1ms stereo 16-bit packets (48 samples x 32-bit words).

- I2SReceiver: standard I2S (MSB one SCK after the WS edge, WS low = left), oversampled by the sys
  clock (SCK is a few MHz), keeps the 16 MSBs of each channel. Output words: L16 | R16 << 16.
- AudioSource: I2S or test counter (48kHz from the sys clock) samples; packetized into 1ms packets
  (48 words) and sent on GPIF thread 1 by the GPIFStreamer.
"""

from migen import *
from migen.genlib.cdc import MultiReg

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# I2S Receiver -------------------------------------------------------------------------------------

class I2SReceiver(LiteXModule):
    def __init__(self, pads, width=16):
        self.source = source = stream.Endpoint([("data", 32)])

        # # #

        sck   = Signal()
        ws    = Signal()
        sd    = Signal()
        sck_d = Signal()
        self.specials += [
            MultiReg(pads.sck, sck),
            MultiReg(pads.ws,  ws),
            MultiReg(pads.sd,  sd),
        ]
        self.sync += sck_d.eq(sck)
        rise = Signal()
        self.comb += rise.eq(sck & ~sck_d)

        ws_d     = Signal()
        bitcount = Signal(6)
        shift    = Signal(width)
        left     = Signal(width)
        channel  = Signal() # Channel of the current word (0: left, 1: right).
        self.sync += [
            source.valid.eq(0),
            If(rise,
                ws_d.eq(ws),
                If(ws != ws_d,
                    # WS edge: the MSB of the new channel comes on the next SCK rising edge.
                    bitcount.eq(0),
                    channel.eq(ws),
                ).Elif(bitcount < width,
                    shift.eq(Cat(sd, shift[:-1])),
                    bitcount.eq(bitcount + 1),
                    If(bitcount == (width - 1),
                        If(channel == 0,
                            left.eq(Cat(sd, shift[:-1])),
                        ).Else(
                            source.valid.eq(1),
                            source.data.eq(Cat(left, Cat(sd, shift[:-1]))),
                        )
                    )
                )
            )
        ]

# Audio Source -------------------------------------------------------------------------------------

class AudioSource(LiteXModule):
    """48kHz stereo 16-bit samples (L16 | R16 << 16) from the I2S receiver or a test counter.

    Samples are presented for one cycle (no backpressure): samples not accepted are counted as
    overflow. Packetization is done in the GPIF domain (see GPIFStreamer). Without I2S samples for
    more than 2 sample periods (source without audio, IT6802 audio muted), silence is generated at
    48kHz from the sys clock (the host always receives audio packets: no capture I/O errors)."""
    def __init__(self, pads, sys_clk_freq):
        self.source = source = stream.Endpoint([("data", 32)])

        self.control = CSRStorage(fields=[
            CSRField("enable", size=1, offset=0, description="Enable audio."),
            CSRField("test",   size=1, offset=1, description="Test source (48kHz counter) instead of I2S."),
        ])
        self.samples  = CSRStatus(32, description="Received samples.")
        self.overflow = CSRStatus(32, description="Samples lost (not accepted downstream).")

        # # #

        enable = self.control.fields.enable
        test   = self.control.fields.test

        # I2S.
        self.i2s = i2s = I2SReceiver(pads)
        self.comb += i2s.source.ready.eq(1)

        # Test source: 48kHz counter (L = n, R = ~n).
        tick    = Signal()
        timer   = Signal(32)
        period  = int(sys_clk_freq/48000)
        counter = Signal(16)
        self.sync += [
            tick.eq(0),
            If(timer == (period - 1),
                timer.eq(0),
                tick.eq(1),
            ).Else(
                timer.eq(timer + 1),
            ),
            If(tick, counter.eq(counter + 1)),
        ]

        # I2S activity: silence when no I2S sample for more than 2 sample periods.
        i2s_idle = Signal(max=3*period + 1)
        silence  = Signal()
        self.sync += If(i2s.source.valid,
            i2s_idle.eq(0),
        ).Elif(~silence,
            i2s_idle.eq(i2s_idle + 1),
        )
        self.comb += silence.eq(i2s_idle == 3*period)
        self.silences = CSRStatus(32, description="Silence samples generated (no I2S).")
        silences = Signal(32)
        self.comb += self.silences.status.eq(silences)

        # Output.
        samples  = Signal(32)
        overflow = Signal(32)
        self.sync += [
            source.valid.eq(0),
            If(enable,
                If(test,
                    source.valid.eq(tick),
                    source.data.eq(Cat(counter, ~counter)),
                ).Elif(silence,
                    source.valid.eq(tick),
                    source.data.eq(0),
                    If(tick, silences.eq(silences + 1)),
                ).Else(
                    source.valid.eq(i2s.source.valid),
                    source.data.eq(i2s.source.data),
                )
            ),
            If(source.valid,
                samples.eq(samples + 1),
                If(~source.ready, overflow.eq(overflow + 1)),
            ),
        ]
        self.comb += [
            self.samples.status.eq(samples),
            self.overflow.status.eq(overflow),
        ]
