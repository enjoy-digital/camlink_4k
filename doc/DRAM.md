# DDR3 Bandwidth: LiteDRAM 1:4 on ECP5

## Why

4K30 NV12 (the stock format) needs a frame buffer: NV12 sends the whole Y plane then the whole UV
plane, while HDMI delivers them interleaved line by line, and the USB output is busy ~31 ms of each
33 ms frame. Each frame is written to DRAM and read back: ~373 MB/s each way, **~746 MB/s** total.

The hardware can do it: MT41K64M16 (DDR3L-1600, x16) on ECP5-8 bank edge clocks up to 400 MHz =
DDR3-800 = 1.6 GB/s peak. The stock gateware (Lattice DDR3 IP) runs its controller at 200 MHz
(2:1 gearing). The limit was LiteDRAM's usual ECP5 configuration: `ECP5DDRPHY` is 1:2 (DRAM clock =
2 x controller clock) and the controller + our SoC close timing at ~81-100 MHz = DDR3-324..400 =
0.65-0.8 GB/s peak, below 746 MB/s once refresh and read/write turnarounds are counted.

## What (implemented, validated on hardware: see results below)

- `litecamlink/gateware/ecp5ddrphy.py`: copy of LiteDRAM's `ECP5DDRPHY` with a `csr_cdc` hook (CSR
  write strobes into the PHY clock domain), plus `ecp5ddrphy_with_ratio(2)`: the PHY runs at 2x
  the controller clock behind LiteDRAM's generic `DFIRateConverter` (as done for the 7-series
  PHY): **controller at sys (4 DFI phases), PHY at sys2x, DDR edge clock at sys4x (1:4)**.
  The `csr_cdc` change is small and meant for upstream LiteDRAM.
- CRG (`crg.py`, `--sdram-rate 1:4`): PLL -> ECLKSYNCB (sys4x) -> CLKDIVF /2 (sys2x). The
  controller clock (sys) must be phase aligned with sys2x (DFI rate converter serializers):
  - `--sdram-sys-clk-src clkdivf` (default): a second PLL output at 2x sys through the second
    ECLKSYNC/CLKDIVF of the DDR bank (same structural path as sys2x, both re-aligned by the PHY
    init sequence stop/reset);
  - `--sdram-sys-clk-src pll --sdram-sys-phase <deg>`: PLL output with a phase offset (fallback,
    to sweep if the first option misaligns).
  sys2x reset is released from sys (deterministic serializer phase).
- `--sdram-banks 4`: MT41K64M16 used with 4 banks (BA2=0, 64MB): the command multiplexer
  arbitrating 8 bank machines was the critical path at ~100MHz (92 MHz), 4 banks close at 114 MHz.
  64MB = 5 4K NV12 frames. Unbuffered bank machine command buffers (a buffered build lost reads, cause unconfirmed).
- `LiteDRAMNativePortBuffer` (`dram.py`): registered cmd/wdata/rdata between frontends and the
  crossbar (timing).
- `--with-sdram-bist`: LiteDRAM BIST generator (writes) + checker (reads) on separate ports:
  bandwidth (ticks) and integrity from the host, concurrent write+read = frame buffer traffic.
- Video path timing fixes found on the way (also in the main build): registered HDMI frame
  admission, register stage before the canvas, BRAM read ports without write bypass logic.
- `software/dram.py`: DRAM bring-up without CPU through the I2C CSR bridge: init sequence replayed
  from the generated `sdram_phy.h`, ECP5 read leveling (BIOS flow: bitslip x delay scan per
  module), BIST memtest/bandwidth.

## Builds (timing, default seed)

| Build dir | Config | sys | PHY (sys2x) | DRAM | Timing (MHz) |
|---|---|---|---|---|---|
| `build_dram12_cpu` | 1:2 + VexRiscv/BIOS | 75.6 MHz | - | DDR3-302 | sys 91.3 |
| `build_dram14_cpu` | 1:4 + VexRiscv/BIOS | 74.25 MHz | 148.5 MHz | DDR3-594 | sys 87.2, sys2x 280.7 |
| `build_dram12` | 1:2, no CPU | 99.0 MHz | - | DDR3-396 | sys 102.8 |
| `build_dram14` | 1:4, no CPU | 99.56 MHz | 199.1 MHz | DDR3-796 | sys 109.9, sys2x 282.9 |

(All with hdmi >= 150 MHz and fx3 >= 100.8 MHz. Rebuilt after the review fix of the 1:4 sys2x
reset: before it, sys2x never left reset.)

(VexRiscv limits CPU builds to ~85 MHz, hence the 75 MHz bring-up variants.)

## Hardware Results (2026-09-29)

| Config | sys | DRAM | Result | Write | Read | Concurrent |
|---|---|---|---|---|---|---|
| 1:2 | 75 MHz | DDR3-300 | BIOS + BIST OK | 495 MB/s | 512 MB/s | 395 MB/s |
| 1:4 | 74.25 MHz | DDR3-594 | BIOS + BIST 64MB OK | 1027 MB/s | 1048 MB/s | 861 MB/s |
| 1:4 | 87.75 MHz | DDR3-700 | BIST 64MB OK | 1212 MB/s | 1233 MB/s | 1024 MB/s |
| 1:4 | 99.56 MHz | DDR3-796 | no read window | - | - | - |

Findings:
- 1:4 needed the controller read latency one cycle lower than the generic `DFIRateConverter`
  wrapper estimate (isolated with DFII write -> controller read, latency sweep builds).
- Leveling from the host: select the module only around delay/bitslip actions (as the BIOS), and
  re-apply all modules at the end (a module's setting was lost while the next was scanned).
- Read windows are narrow (3-4 of the 8 READCLKSEL steps) and move right with the frequency. At
  DDR3-796 no setting works, even with the added read window calibration (`rdly_re`: DQSBUF READ
  pulse offset, `rdly_data`: read data delay, scanned by `dram.py init`): data comes back with wrong
  beats, burst detection on one module only. The -8 ECP5 and the stock Lattice IP do DDR3-800 on this
  board: next steps are the DQSBUFM fine read delay (RDLOADN/RDMOVE, unused by LiteDRAM) and the
  write DQS timing at 400 MHz (the Gowin GW5 1:4 PHY is a good reference).
- DDR3-700 is enough for the 4K30 NV12 frame buffer (37% margin), with the video pipeline in its
  own 100 MHz domain (4K30 M420/NV12 needs ~93 Mwords/s, more than a 87.75 MHz sys).

## NV12 Frame Buffer (hardware validated, 2026-09-29)

4K30 NV12 (the stock format) through the DRAM: `litecamlink/gateware/framebuffer.py`, UVC format 3.

- Datapath: HDMI M420 frames -> `NV12FrameBuffer` writer (Y lines to the Y plane, CbCr lines to the
  UV plane of a DRAM slot) -> 3 slots -> reader (latest complete slot, linear = NV12) -> UVC.
  Video path in its own 99 MHz domain (`--video-clk-freq`), native ports in the video domain
  through explicit `LiteDRAMNativePortCDC`s (read data CDC deep enough for all outstanding reads).
- Frames only published complete (truncated frames dropped); frames skipped when the output is
  slower, the writer never touches the slot being read nor the latest complete one.
- Stop without reset (stream stop/format change): input dropped, buffered writes complete, reader
  drains its outstanding reads. Resetting the DMAs with accesses in flight desynchronizes the port
  data FIFOs for good (the crossbar does not handshake write/read data).
- DRAM accesses in bursts of 64 port words (writes: 64 buffered words or the frame end, reads: 64
  free reservations). Single accesses (reader paced by USB) made the crossbar re-grant the banks
  between writer and reader at almost every access: 4K30 at 15 fps with 32-word bursts, 30 fps with
  64-word bursts.

Results (DDR3-594, sys 74.25 MHz, video 99 MHz, seed 2, all clocks met; 64MB memtest OK, 846MB/s
concurrent write+read), uvcvideo:

| Format | Result |
|---|---|
| NV12 3840x2160@30 | 29.95 fps over 30s, 0 dropped/stalls/errors, image = M420 reference (static areas) |
| NV12 1920x1080@30 | 29.8 fps |
| M420 3840x2160@30 / YUYV 1080p30 (same build) | 29.6 fps |
| 7 start/stop cycles mixing NV12/M420/YUYV (1080p/720p) | all ~30 fps, no errors, 0 dropped |

Findings:
- DRAM worked on ~1 of 8 frame buffer builds: sys/sys2x crossing of the DFI rate converter not
  timed by nextpnr (see `doc/upstream/README.md`). Fixed with `RateCrossing` (single capture per
  sys cycle on a CSR-selected sys2x edge, runtime read alignment): 8/8 FPGA loads OK, first try.
- DRAM still dead on ~1 of 5 builds with the robust crossing: IOLOGIC gearbox reset released with
  the edge clock running and routed with > 1 ECLK period of skew. The IO reset now comes from the
  init sequence (released while ECLK is stopped): 9/9 FPGA loads OK on 3 seeds, first attempt.
- A build with `cmd_buffer_buffered=True` lost reads (128 accepted by the crossbar, never
  answered), unbuffered bank machine command buffers did not: kept unbuffered, cause unconfirmed
  (not reproduced in simulation, see `doc/upstream/README.md`).
- NV12 build regressions (audio test counter bit errors, M420 4K30 gaps; the main build passed):
  the FPGA drove all 9 GPIF CTL pins (single TSTriple `oe`), FX3 FLAG outputs included (bus
  contention next to DQ), and the first audio word changed on DQ with the ASEL edge (a slow DQ line,
  DQ[29], lost it at the FX3 thread switch). Per-pin CTL enables + CTL IO registers + first audio
  word before ASEL: validate.py 12/12 on both builds. DQ must stay in fabric registers (IO
  registered DQ switched too early for the FX3: video gaps).
- Timing closure of the NV12 build (video 99 MHz, HDMI 148.5 MHz): frame buffer read counter
  (registered strobes, reservation depth width) and M420 UV line buffer (one memory per bank,
  yosys `no_rw_check`): all clocks met with margin.
- Standalone: `flash-bitstream` + `flash-fx3` of the NV12 build and its firmware: boot from flash,
  DRAM init by the firmware (first attempt), NV12 4K30 29.85 fps, validate.py 11/12 (5851a2d:
  audio + video bit errors, fixed by the first audio word before ASEL).
- `software/uvc_raw.py` (Python, libusb) tops out at ~44 MB/s when the host is loaded: use uvcvideo
  (`validate.capture_stats`) for 4K throughput.
- DDR3-700 (sys 87.75 MHz) needs video at 100.29 MHz (VCO 702 MHz): builds missed timing on video
  or sys (2 seeds); not needed with the 64-word bursts at DDR3-594.

## Procedure

The FX3 firmware must match the CSR map of the loaded bitstream:

```
python3 litecamlink.py --build --with-sdram --sdram-rate 1:4 --sdram-banks 4 --with-sdram-bist \
    --sys-clk-freq 74.25e6 --video-clk-freq 99e6 --with-framebuffer --output-dir build_nv12
make -C firmware/fx3 clean && make -C firmware/fx3 CSR_CSV=../../build_nv12/csr.csv
python3 software/camlink.py boot --bit build_nv12/gateware/litecamlink.bit  # FPGA + HDMI + DRAM init.
python3 software/camlink.py sdram-status
python3 software/dram.py --build build_nv12 bandwidth                        # Optional (host tools).
```

The FX3 firmware initializes the DRAM (`firmware/fx3/sdram.c`, ~4s over the I2C CSR bridge):
after a flash boot, and on `VREQ_SDRAM_INIT` (`camlink.py boot`/`sdram-init`). Same flow as
`dram.py init`: DFII init sequence (`generated/sdram_init.h` from the gateware `sdram_phy.h`), rate
crossing capture edge x read pairing validated by the read leveling, read word shift validated by a
1MB BIST, PHY init sequence replay retries. NV12 falls back to the test pattern if the DRAM init
failed. Leveling scans one module at a time (the other one at its current setting): scanning both
together through the bad delays found the same settings but left the controller read path failing
(DFII still OK). `software/dram.py` stays for bring-up/debug (do not run it while the firmware init
runs: shared CSRs).

Restore the video firmware afterwards:
`make -C firmware/fx3 clean && make -C firmware/fx3`.
