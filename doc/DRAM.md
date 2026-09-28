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

## What (implemented, timing closed, not yet tested on hardware)

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
  64MB = 5 4K NV12 frames. Plus registered bank machine command buffers.
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

| Build dir | Config | sys | PHY (sys2x) | DRAM | Timing |
|---|---|---|---|---|---|
| `build_dram12_cpu` | 1:2 + VexRiscv/BIOS | 75.6 MHz | - | DDR3-302 | sys 87.9 |
| `build_dram14_cpu` | 1:4 + VexRiscv/BIOS | 74.25 MHz | 148.5 MHz | DDR3-594 | sys 85.6, sys2x 266 |
| `build_dram12` | 1:2, no CPU | 99.0 MHz | - | DDR3-396 | sys 105 |
| `build_dram14` | 1:4, no CPU | 99.56 MHz | 199.1 MHz | DDR3-796 | sys 114.7, sys2x 297 |

(VexRiscv limits CPU builds to ~85 MHz, hence the 75 MHz bring-up variants.)

## Bring-up Procedure (tomorrow)

The FX3 firmware must match the CSR map of the loaded bitstream:

```
make -C firmware/fx3 clean && make -C firmware/fx3 CSR_CSV=../../build_dram12_cpu/csr.csv
python3 software/camlink.py boot --bit build_dram12_cpu/gateware/litecamlink.bit
python3 software/camlink.py --csr-csv build_dram12_cpu/csr.csv term
```

1. `build_dram12_cpu` (known LiteDRAM ECP5 configuration): BIOS SDRAM init/leveling/memtest must
   pass -> validates pinout/IO/PCB. `sdram_bist` / `memspeed` from the BIOS console.
2. `build_dram14_cpu` (1:4): same. If leveling/memtest fail: rebuild with
   `--sdram-sys-clk-src pll` and sweep `--sdram-sys-phase` (0, 90, 180, 270); check the DFI
   rate converter latencies (`write_delay`/`read_delay`) against the BIOS results.
3. `build_dram14` (1:4, 99.56 MHz, no CPU): `software/dram.py --build build_dram14 init`, then
   `memtest` and `bandwidth`. Target: concurrent write+read >= 750 MB/s.
4. If OK: NV12 frame buffer path (4:2:0 packer to DRAM writer, Y/UV plane readers to UVC), and
   upstream the PHY `csr_cdc` + 1:4 wrapper to LiteDRAM.

Restore the video firmware afterwards: `make -C firmware/fx3 clean && make -C firmware/fx3`.
