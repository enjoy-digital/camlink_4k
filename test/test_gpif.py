#
# This file is part of CamLink 4K.
#
# Copyright (c) 2026 Florent Kermarrec <florent@enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import random

from migen import *
from migen.sim import passive

from litex.gen import *

from gateware.gpif import GPIFStreamer, CounterGenerator

# FX3 GPIF Model -----------------------------------------------------------------------------------

class FX3Pads:
    def __init__(self):
        self.pclk = Signal()
        self.dq   = Signal(32)
        self.ctl  = Signal(9)

class FX3Model:
    """Behavioural model of the FX3 waveform (firmware/gpif.c) and DMA buffers.

    Waveform: IDLE/DATA on thread 0, DECIDE on ASEL|EOP (commit on EOP), AIDLE/ADATA on thread 1.
    DMA (per thread): ring of `buf_count` buffers of `buf_words` words, drained by the "USB" every
    `drain` cycles. Buffer switch quirk seen on hardware: when a thread's next buffer becomes
    current (immediately after a buffer is filled/committed if one is free, later otherwise), DQ is
    captured as its first word and the first word pushed into it is dropped. FLAGs = current buffer
    available.
    Stream start: hardware writes the GPIF input register (loaded on IDLE entry, see gpif.c), which
    holds the DQ presented by the FPGA streaming logic in reset (modelled as a DQ capture).
    Early sampling quirk (why the FPGA presents a word `head_lead` cycles before VALID): the first
    word after a gap (VALID low) is sampled one cycle early (previous cycle's DQ).
    """
    def __init__(self, dut, buf_words=(64, 48), buf_count=(4, 4), drain=(80, 150),
        switch_delay=(2, 2), dma_start=1):
        self.dma_start    = dma_start
        self.dut          = dut
        self.buf_words    = buf_words
        self.buf_count    = buf_count
        self.drain        = drain
        self.switch_delay = switch_delay
        self.buffers      = ([], []) # Completed buffers (word lists).
        self.commits      = 0
        self.audio_phases = 0
        self.errors       = []

    def words(self, thread):
        return [w for b in self.buffers[thread] for w in b]

    @passive
    def run(self):
        ctl     = self.dut.ctl
        state   = "IDLE"
        cycle   = 0
        cur     = [None, None] # Current buffer: {"words": [...], "skip": bool}.
        free    = [n - 1 for n in self.buf_count]
        filled  = [0, 0]       # Completed buffers not yet drained.
        switch  = [None, None] # Cycle at which a pending switch happens.
        dq_hist = []
        valid_d = 0
        dq_d    = 0
        started = False        # DMA start: DQ captured as the first word of the first buffers.

        def complete(t):
            self.buffers[t].append(cur[t]["words"])
            cur[t]     = None
            filled[t] += 1

        def push(t, word):
            if cur[t] is None:
                self.errors.append(f"push without buffer on thread {t} at {cycle}")
                return
            if cur[t]["skip"]:
                cur[t]["skip"] = False
            else:
                cur[t]["words"].append(word)
            if len(cur[t]["words"]) == self.buf_words[t]:
                complete(t)

        while True:
            # FLAGs (active low in the FPGA: flag_invert=1).
            flags = ((cur[0] is None) << 1) | ((cur[1] is None) << 4)
            yield ctl.i.eq(flags if started else 0b10010)
            yield
            cycle += 1
            valid = (yield ctl.o[0])
            eop   = (yield ctl.o[2])
            asel  = (yield ctl.o[3])
            dq    = (yield self.dut.pads_dq)
            dq_hist.append(dq)
            # First word after a gap: sampled one cycle early.
            dq_push = dq_d if (valid and not valid_d) else dq
            valid_d, dq_d = valid, dq
            if not started:
                if cycle < self.dma_start:
                    continue
                started = True
                for t in range(2):
                    cur[t] = {"words": [dq], "skip": True}

            # USB drain.
            for t in range(2):
                if cycle % self.drain[t] == 0 and filled[t]:
                    filled[t] -= 1
                    free[t]   += 1

            # Waveform.
            if state == "IDLE":
                if valid and not asel:
                    push(0, dq_push)
                elif eop or asel:
                    state = "DECIDE"
            elif state == "DECIDE":
                if valid:
                    self.errors.append(f"VALID in DECIDE at {cycle}")
                if asel:
                    self.audio_phases += 1
                    state = "AIDLE"
                else:
                    self.commits += 1
                    if cur[0] is not None:
                        complete(0)
                    state = "IDLE"
            elif state == "AIDLE":
                if valid:
                    push(1, dq_push)
                elif not asel:
                    state = "IDLE"

            # Buffer switches (DQ captured as the first word of the new buffer).
            for t in range(2):
                if cur[t] is None and switch[t] is None and free[t]:
                    free[t]  -= 1
                    switch[t] = cycle + random.randint(*self.switch_delay)
                if switch[t] is not None and cycle >= switch[t]:
                    cur[t]    = {"words": [dq], "skip": True}
                    switch[t] = None

# DUT / Test Runner --------------------------------------------------------------------------------

class DUT(LiteXModule):
    def __init__(self, with_csr=False):
        self.pads    = pads = FX3Pads()
        self.gpif    = GPIFStreamer(pads, with_audio=True, audio_packet_words=48, sim=True,
            with_csr=with_csr)
        self.ctl     = self.gpif.ctl
        self.pads_dq = pads.dq

def run_video_audio(video_gap, drain=(80, 150), switch_delay=(2, 2), switch_guard=16,
    audio_batch=1, audio_packets=4):
    random.seed(0)
    dut = DUT()
    fx3 = FX3Model(dut, drain=drain, switch_delay=switch_delay)
    # Video payloads: full (64 words = one buffer/burst) and short (EOP commit) ones.
    lengths  = [64, 64, 40]*4
    payloads = []
    word     = 0x1000
    for n in lengths:
        payloads.append(list(range(word, word + n)))
        word += n
    video = [w for p in payloads for w in p]
    audio = [0xa0000000 + i for i in range(48*audio_packets)]

    def setup():
        yield dut.gpif.enable.eq(1)
        yield dut.gpif.flag_invert.eq(1)
        yield dut.gpif.head_lead.eq(4)
        yield dut.gpif.audio_enable.eq(1)
        yield dut.gpif.audio_lead.eq(8)
        yield dut.gpif.audio_batch.eq(audio_batch)
        yield dut.gpif.burst.eq(64)
        yield dut.gpif.guard.eq(8)
        yield dut.gpif.switch_guard.eq(switch_guard)

    def video_gen():
        yield from setup()
        # First word of the stream (UVCPacketizer.next_header0), payload last words tagged with
        # the first word of the following payload (UVCPacketizer `next`).
        yield dut.gpif.eop_data.eq(payloads[0][0])
        for k, payload in enumerate(payloads):
            for i, v in enumerate(payload):
                yield dut.gpif.sink.valid.eq(1)
                yield dut.gpif.sink.data.eq(v)
                yield dut.gpif.sink.last.eq(i == len(payload) - 1)
                yield dut.gpif.sink.next.eq(payloads[k + 1][0] if k + 1 < len(payloads) else 0)
                yield
                while not (yield dut.gpif.sink.ready):
                    yield
                if video_gap:
                    yield dut.gpif.sink.valid.eq(0)
                    for _ in range(video_gap):
                        yield
        yield dut.gpif.sink.valid.eq(0)

    def audio_gen():
        for _ in range(50):
            yield
        # One extra sample: a packet is only sent once the next packet's first word is available.
        for a in audio + [0xdeadbeef]:
            yield dut.gpif.audio_sink.valid.eq(1)
            yield dut.gpif.audio_sink.data.eq(a)
            yield
            yield dut.gpif.audio_sink.valid.eq(0)
            for _ in range(7):
                yield

    def timeout():
        for _ in range(12000 + 20*len(video)*video_gap):
            yield

    run_simulation(dut, {
            "sys":  [video_gen(), audio_gen(), timeout()],
            "gpif": [fx3.run()],
        }, clocks={"sys": 10, "gpif": 10, "gpif_cdc": 10})

    assert fx3.errors == []
    # First word of each thread is captured at start (not data).
    assert fx3.words(1)[1:len(audio)] == audio[1:]
    assert fx3.words(0)[1:len(video)] == video[1:]
    assert fx3.commits == lengths.count(40)
    return fx3

# Tests --------------------------------------------------------------------------------------------

def test_gpif_video_audio():
    run_video_audio(video_gap=0)

def test_gpif_video_starved_audio():
    # Slow video: audio packets are inserted inside video bursts.
    run_video_audio(video_gap=6)

def test_gpif_video_audio_slow_usb():
    # Slow USB drain: buffer switches happen late (while the other thread may be active).
    run_video_audio(video_gap=0, drain=(400, 900))

def test_gpif_video_audio_switch_lag():
    # FX3 DMA buffer switch lagging the last GPIF word (queue drain under load).
    run_video_audio(video_gap=0, switch_delay=(2, 100), switch_guard=128)

def test_gpif_video_drain():
    # Words written while the video is disabled are drained (not sent at the next start).
    dut = DUT()
    fx3 = FX3Model(dut)
    def gen():
        yield dut.gpif.flag_invert.eq(1)
        yield dut.gpif.head_lead.eq(4)
        yield dut.gpif.burst.eq(64)
        for i in range(32):
            yield dut.gpif.sink.valid.eq(1)
            yield dut.gpif.sink.data.eq(0xdead0000 + i)
            yield
        yield dut.gpif.sink.valid.eq(0)
        for _ in range(200):
            yield
        yield dut.gpif.enable.eq(1)
        for i in range(64):
            yield dut.gpif.sink.valid.eq(1)
            yield dut.gpif.sink.data.eq(0x1000 + i)
            yield
            while not (yield dut.gpif.sink.ready):
                yield
        yield dut.gpif.sink.valid.eq(0)
        for _ in range(400):
            yield
    run_simulation(dut, {"sys": [gen()], "gpif": [fx3.run()]},
        clocks={"sys": 10, "gpif": 10, "gpif_cdc": 10})
    words = fx3.words(0)
    assert not any((w >> 16) == 0xdead for w in words)
    assert words[1:64] == [0x1000 + i for i in range(1, 64)]

def test_gpif_first_word():
    # Streaming logic in reset (video disabled) at the FX3 DMA start: the captured first word is
    # the next video word (next UVC header word), so the first payload is intact.
    dut     = DUT()
    fx3     = FX3Model(dut, dma_start=40)
    payload = [0x0000880c] + [0x1000 + i for i in range(1, 64)]
    def gen():
        yield dut.gpif.flag_invert.eq(1)
        yield dut.gpif.head_lead.eq(4)
        yield dut.gpif.burst.eq(64)
        yield dut.gpif.eop_data.eq(payload[0])
        for _ in range(100):
            yield
        yield dut.gpif.enable.eq(1)
        for w in payload:
            yield dut.gpif.sink.valid.eq(1)
            yield dut.gpif.sink.data.eq(w)
            yield
            while not (yield dut.gpif.sink.ready):
                yield
        yield dut.gpif.sink.valid.eq(0)
        for _ in range(300):
            yield
    run_simulation(dut, {"sys": [gen()], "gpif": [fx3.run()]},
        clocks={"sys": 10, "gpif": 10, "gpif_cdc": 10})
    assert fx3.words(0)[:64] == payload

def test_gpif_video_audio_batch():
    # Audio packets sent by batches of 3 per thread 1 phase (with buffer switch lag).
    fx3 = run_video_audio(video_gap=0,
        switch_delay  = (2, 20),
        switch_guard  = 64,
        audio_batch   = 3,
        audio_packets = 6,
    )
    assert fx3.audio_phases == 2

def test_gpif_csr():
    # CSR map (names/order, used by the FX3 firmware and host tools) and CSR -> control Signals.
    dut = DUT(with_csr=True)
    assert [c.name for c in dut.gpif.get_csrs()] == [
        "control", "burst", "guard", "switch_guard", "status", "bursts", "last", "wait_cycles",
        "starve_cycles", "active_cycles", "eops", "eop_cycle", "burst_cycle", "force",
        "force_value", "audio_packets", "audio_status"]
    def gen():
        assert (yield dut.gpif.head_lead)   == 2
        assert (yield dut.gpif.audio_lead)  == 8
        assert (yield dut.gpif.audio_batch) == 1
        assert (yield dut.gpif.burst)       == 16384//4
        yield dut.gpif._control.fields.enable.eq(1)
        yield dut.gpif._control.fields.audio_batch.eq(3)
        yield dut.gpif._burst.storage.eq(64)
        yield
        assert (yield dut.gpif.enable)      == 1
        assert (yield dut.gpif.audio_batch) == 3
        assert (yield dut.gpif.burst)       == 64
    run_simulation(dut, {"sys": gen()}, clocks={"sys": 10, "gpif": 10, "gpif_cdc": 10})

# Counter Generator --------------------------------------------------------------------------------

def run_counter(dut, setup, cycles=40):
    words = []
    def gen():
        yield from setup()
        yield dut.source.ready.eq(1)
        for _ in range(cycles):
            yield
            if (yield dut.source.valid) and (yield dut.source.ready):
                words.append(((yield dut.source.data), (yield dut.source.last)))
    run_simulation(dut, gen())
    return words

def test_counter_generator():
    # 4-word packets with `last`, 3 idle cycles between packets.
    dut = CounterGenerator(with_csr=False)
    def setup():
        yield dut.enable.eq(1)
        yield dut.on.eq(4)
        yield dut.off.eq(3)
        yield dut.last.eq(1)
    words = run_counter(dut, setup)
    assert [w for w, _ in words[:8]] == list(range(8))
    assert [l for _, l in words[:8]] == [0, 0, 0, 1]*2

def test_counter_generator_csr():
    dut = CounterGenerator()
    assert [c.name for c in dut.get_csrs()] == ["enable", "on", "off", "last"]
    def setup():
        yield dut._enable.storage.eq(1)
    words = run_counter(dut, setup, cycles=10)
    assert [w for w, l in words] == list(range(len(words))) and len(words) >= 8
