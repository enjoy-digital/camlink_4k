# Upstream Patches: ECP5 DDR3 1:4

Merged: LiteDRAM https://github.com/enjoy-digital/litedram/pull/408 (full test suite passes)
and LiteX-Boards https://github.com/litex-hub/litex-boards/pull/866 (1:4 PHY import only when used).
The patches below are the merged commits (`git am` on a clean checkout of each repo):

| Patch | Repo | Content |
|---|---|---|
| `0001-phy-ecp5ddrphy-...` | LiteDRAM | `ECP5DDRPHY(csr_cdc=...)` + `ecp5ddrphy_with_ratio()` (1:4 through `DFIRateConverter`) + `test/test_ecp5ddrphy.py` |
| `0002-targets-camlink_4k-...` | LiteX-Boards | `camlink_4k.py --sdram-rate 1:4` (CRG sys4x/sys2x/sys, 1:4 PHY) |

## Validation

On the Cam Link 4K (ECP5 LFE5U-25F-8, MT41K64M16), same CRG/PHY as the CamLinX gateware:

| DRAM | sys | Result | Write | Read | Write+read |
|---|---|---|---|---|---|
| DDR3-300 (1:2) | 75 MHz | BIOS + BIST OK | 495 MB/s | 512 MB/s | 395 MB/s |
| DDR3-594 (1:4) | 74.25 MHz | BIOS leveling/memtest + BIST 64MB OK | 1027 MB/s | 1048 MB/s | 861 MB/s |
| DDR3-700 (1:4) | 87.75 MHz | BIST 64MB + random accesses OK | 1212 MB/s | 1233 MB/s | 1024 MB/s |
| DDR3-796 (1:4) | 99.56 MHz | no read window | - | - | - |

The LiteX-Boards target was built at 1:4/75 MHz (timing met) but not run as is (its BIOS console is
not reachable on this board without the CamLinX I2C/UART bridge).

## Follow-ups (open)

- LiteDRAM https://github.com/enjoy-digital/litedram/pull/409: `ECP5DDRPHY(io_rst_init)`, IO
  gearing reset from the init sequence, default in `ecp5ddrphy_with_ratio`.
- LiteDRAM https://github.com/enjoy-digital/litedram/pull/410 (stacked on #409): selectable
  `DFIRateConverter` serializers, `RateCrossing`, `ecp5ddrphy_with_ratio(rate_crossing=True)`
  (opt-in, sel/shift search needed in the init software: LiteX BIOS support to do).
- Once merged, `camlinx/gateware/ecp5ddrphy.py` can use the upstream PHY again.

## Notes / Open Items

- sys/sys2x crossing of the DFI rate converter (fixed locally, to upstream): `Serializer` samples
  each sys word on both sys2x edges (combinational slice select) and `Deserializer` hands the last
  slice to sys on the next edge. With the ECP5 CLKDIVF clocks (edges coincident or a quarter sys
  period apart, set at each PHY init), one sample is a hold race or has ~3.4ns of setup, and
  nextpnr does not check these cross-domain paths: DRAM worked on ~1 of 8 frame buffer builds
  (DFII writes did not land). `RateCrossing` (`camlinx/gateware/ecp5ddrphy.py`) captures each
  word once per sys cycle on a CSR-selected sys2x edge and aligns the read words with a runtime
  shift: 8/8 loads OK on 2 builds. Candidate for `DFIRateConverter` (optional safe crossing).
- ECP5DDRPHY IO gearing reset (fixed locally with `io_rst_init`, to upstream): the IOLOGIC/DQSBUFM
  `RST` pins use the sys reset, released after the edge clock restarts. Routed to the IOLOGICs with
  up to ~2.5ns of skew (> 1 ECLK period at DDR3-594 on some placements), the pins' gearboxes came
  out of reset on different ECLK edges: commands/data misaligned, DRAM dead on ~1 of 5 builds even
  with the robust crossing (nextpnr detailed net timing: LSR 1.1-3.5ns on the failing build, 1.1-1.8ns
  on a working one). Driving them from the init sequence reset pulse (released while ECLK is
  stopped, as Lattice's sequence intends): 9/9 FPGA loads OK on 3 seeds. Probably also relevant at
  1:2 on other ECP5 boards (random DRAM failures with some builds).
- Lost reads with `cmd_buffer_buffered=True` (open, NOT confirmed as a LiteDRAM bug): on hardware
  a video-domain reader (CDC) lost 128 reads while a writer shared the banks (sys-side counters:
  128 reads accepted by the crossbar, never answered); the same design with unbuffered bank
  machine command buffers had no loss. Suspected crossbar lock window (buffered lookahead FIFO:
  a just accepted command not visible on `source.valid` for a cycle), but a targeted LiteDRAM
  simulation (reader/writer on the same banks, reader pausing after bank switches, buffered vs
  unbuffered) lost nothing, and the hardware A/B predates the IO reset/rate crossing fixes (DRAM
  still placement sensitive then). Not to report upstream without a reproduction.

- Read latency: the controller read latency is one cycle lower than the generic
  `DFIRateConverter.phy_wrapper` estimate for the ECP5DDRPHY (found on hardware by writing with
  DFII and reading through the controller, then confirmed with BIST and random accesses). Fabric
  simulations of the wrapper did not reproduce it reliably (sys/sys2x reset/phase alignment of the
  converter serializers vs the CRG): worth a closer look with the maintainers.
- Generic wrapper at ratio 4 (not used here): with a dummy PHY whose `rddata_valid` is its
  `rddata_en` delayed by exactly its read latency, the wrapper estimate was one cycle short for
  `read_latency % 4` in 0..2 (simulation, aligned clocks): to check before relying on 1:8.
- Read leveling from the host (this project's `software/dram.py`): a module's delay/bitslip was
  lost while the next module was scanned; re-applying all modules at the end fixed it. The BIOS
  passed without it at DDR3-594.
- DDR3-796: the -8 ECP5 and the stock Lattice IP run DDR3-800 on this board. LiteDRAM only uses the
  DQSBUFM READCLKSEL (8 coarse steps) for read leveling; next candidates: DQSBUFM fine read delay
  (RDLOADN/RDMOVE), READ pulse positioning (a `rdly_re`/`rdly_data` calibration is prototyped in
  `camlinx/gateware/ecp5ddrphy.py`), write DQS timing at 400 MHz (the GW5 1:4 PHY is a
  reference).
- Other ECP5 DDR3 boards (ECPIX-5, OrangeCrab, ButterStick, Versa ECP5, TrellisBoard, ...) can use
  the same CRG pattern: the second ECLKSYNCB/CLKDIVF BEL names depend on the DDR bank side.
