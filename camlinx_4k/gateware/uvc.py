#
# This file is part of CamLinX.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""UVC: UVC payload packetizer (bulk, one payload per FX3 DMA buffer).

Splits a frame stream (32-bit words, `last` = end of frame) into UVC payloads of up to
`payload_words` data words, each prefixed with a 12-byte UVC payload header:

    Byte 0      : bHeaderLength (12).
    Byte 1      : bmHeaderInfo (FID, EOF, PTS, SCR, EOH).
    Bytes 2-5   : PTS  (timestamp of the frame start, `timestamp` clock).
    Bytes 6-11  : SCR  (STC at the payload start + 16-bit SOF counter (0)).

`source.last` marks the end of each payload (used by the GPIF to commit short payloads).

The first header word of the first payload of a frame is kept predictable (no PTS, PTS field = 0)
and exported on `next_header0` (first word of a stream). The last word of each payload carries, on
the `next` field, the exact first header word of the following payload: the GPIF presents it on the
bus between payloads when the next word is not available yet (the FX3 captures the bus as the first
word of its next DMA buffer when switching buffers). PTS is carried from the second payload of
each frame.
"""

from migen import *

from litex.gen import *

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

# UVC Header ---------------------------------------------------------------------------------------

UVC_HEADER_FID = (1 << 0)
UVC_HEADER_EOF = (1 << 1)
UVC_HEADER_PTS = (1 << 2)
UVC_HEADER_SCR = (1 << 3)
UVC_HEADER_EOH = (1 << 7)

# UVC Packetizer -----------------------------------------------------------------------------------

class UVCPacketizer(LiteXModule):
    def __init__(self, payload_words=(16384 - 12)//4):
        self.sink      = sink   = stream.Endpoint([("data", 32)])
        self.source    = source = stream.Endpoint([("data", 32), ("next", 32)])
        self.timestamp    = timestamp = Signal(32)
        self.next_header0 = next_header0 = Signal(32)

        self._payload_words = CSRStorage(16, reset=payload_words, description="Data words per payload.")
        self._frame_words   = CSRStorage(32, reset=1920*1080//2,  description="Data words per frame.")

        # # #

        fid       = Signal()
        remaining = Signal(32) # Remaining frame words.
        count     = Signal(16) # Payload data words.
        eof       = Signal()
        pts       = Signal(32)
        stc       = Signal(32)
        in_frame  = Signal()
        info      = Signal(8)
        first     = Signal() # First payload of the frame.

        first_eof  = Signal()
        first_info = Signal(8)
        self.comb += [
            eof.eq(remaining <= self._payload_words.storage),
            first_eof.eq(self._frame_words.storage <= self._payload_words.storage),
            info.eq(UVC_HEADER_EOH | UVC_HEADER_SCR | UVC_HEADER_PTS | (eof << 1) | fid),
            first_info.eq(UVC_HEADER_EOH | UVC_HEADER_SCR | (first_eof << 1) | fid),
            next_header0.eq(Cat(Constant(12, 8), first_info, Constant(0, 16))),
        ]

        # Header word 0 of the payload following the current one (on the payload last word).
        next_frame_header0   = Signal(32) # End of frame: first payload of the next frame (FID toggled).
        next_payload_header0 = Signal(32) # Mid-frame: next payload of this frame.
        next_frame_info      = Signal(8)
        next_payload_info    = Signal(8)
        eof_next             = Signal()
        fid_next             = Signal() # 1-bit (Verilog would size ~fid to the expression width).
        self.comb += [
            fid_next.eq(~fid),
            next_frame_info.eq(UVC_HEADER_EOH | UVC_HEADER_SCR | (first_eof << 1) | fid_next),
            next_frame_header0.eq(Cat(Constant(12, 8), next_frame_info, Constant(0, 16))),
            eof_next.eq((remaining - 1) <= self._payload_words.storage),
            next_payload_info.eq(UVC_HEADER_EOH | UVC_HEADER_SCR | UVC_HEADER_PTS | (eof_next << 1) | fid),
            next_payload_header0.eq(Cat(Constant(12, 8), next_payload_info, pts[:16])),
        ]

        self.fsm = fsm = FSM(reset_state="HEADER0")
        fsm.act("HEADER0",
            If(sink.valid,
                source.valid.eq(1),
                source.data.eq(Mux(in_frame, Cat(Constant(12, 8), info, pts[:16]), next_header0)),
                If(source.ready,
                    NextValue(first, ~in_frame),
                    If(~in_frame,
                        NextValue(in_frame, 1),
                        NextValue(pts, timestamp),
                        NextValue(remaining, self._frame_words.storage),
                    ),
                    NextValue(stc, timestamp),
                    NextState("HEADER1"),
                )
            )
        )
        fsm.act("HEADER1",
            source.valid.eq(1),
            source.data.eq(Cat(Mux(first, 0, pts[16:]), stc[:16])),
            If(source.ready, NextState("HEADER2")),
        )
        fsm.act("HEADER2",
            source.valid.eq(1),
            source.data.eq(Cat(stc[16:], Constant(0, 16))),
            If(source.ready,
                NextValue(count, 0),
                NextState("DATA"),
            )
        )
        fsm.act("DATA",
            source.valid.eq(sink.valid),
            source.data.eq(sink.data),
            sink.ready.eq(source.ready),
            If(sink.valid & sink.ready,
                NextValue(count, count + 1),
                NextValue(remaining, remaining - 1),
                If(sink.last | (remaining == 1),
                    # End of frame: toggle FID, end payload.
                    source.last.eq(1),
                    source.next.eq(next_frame_header0),
                    NextValue(fid, ~fid),
                    NextValue(in_frame, 0),
                    NextState("HEADER0"),
                ).Elif(count == (self._payload_words.storage - 1),
                    source.last.eq(1),
                    source.next.eq(next_payload_header0),
                    NextState("HEADER0"),
                )
            )
        )
