#
# This file is part of LiteCamLink.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

"""DRAM helpers: pipelined LiteDRAM native port (timing between frontends and the crossbar)."""

from migen import *

from litex.gen import *

from litex.soc.interconnect import stream

from litedram.common  import LiteDRAMNativePort
from litedram.modules import MT41K64M16

# DRAM Module --------------------------------------------------------------------------------------

class MT41K64M16_4Banks(MT41K64M16):
    """MT41K64M16 used with 4 banks (BA2 = 0, 64MB): half the bank machines, simpler command
    multiplexer (timing at 1:4 ~100MHz); 4-bank interleaving is enough for streaming traffic."""
    nbanks = 4

# Native Port Buffer -------------------------------------------------------------------------------

class LiteDRAMNativePortBuffer(LiteXModule):
    """User-side native port connected to a crossbar port through registered (skid) buffers on
    cmd, wdata and rdata (valid and ready paths registered): breaks the long combinational paths
    between frontends and the crossbar/bank machines, at the cost of a few cycles of latency."""
    def __init__(self, port):
        self.port = user = LiteDRAMNativePort(port.mode, port.address_width, port.data_width,
            port.clock_domain, port.id)

        # # #

        def buffer(layout):
            return stream.Buffer(layout, pipe_valid=True, pipe_ready=True)

        self.cmd = cmd = buffer(user.cmd.description)
        self.comb += [
            user.cmd.connect(cmd.sink),
            cmd.source.connect(port.cmd),
        ]
        if port.mode in ["write", "both"]:
            self.wdata = wdata = buffer(user.wdata.description)
            self.comb += [
                user.wdata.connect(wdata.sink),
                wdata.source.connect(port.wdata),
            ]
        if port.mode in ["read", "both"]:
            self.rdata = rdata = buffer(user.rdata.description)
            self.comb += [
                port.rdata.connect(rdata.sink),
                rdata.source.connect(user.rdata),
            ]
