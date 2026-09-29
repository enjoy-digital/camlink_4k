# Upstream Patches: ECP5 DDR3 1:4

Submitted: LiteDRAM https://github.com/enjoy-digital/litedram/pull/408 (full test suite passes)
and LiteX-Boards https://github.com/litex-hub/litex-boards/pull/866 (depends on the LiteDRAM PR).
The patches below are the submitted commits (`git am` on a clean checkout of each repo):

| Patch | Repo | Content |
|---|---|---|
| `0001-phy-ecp5ddrphy-...` | LiteDRAM | `ECP5DDRPHY(csr_cdc=...)` + `ecp5ddrphy_with_ratio()` (1:4 through `DFIRateConverter`) + `test/test_ecp5ddrphy.py` |
| `0002-targets-camlink_4k-...` | LiteX-Boards | `camlink_4k.py --sdram-rate 1:4` (CRG sys4x/sys2x/sys, 1:4 PHY) |

## Validation

On the Cam Link 4K (ECP5 LFE5U-25F-8, MT41K64M16), same CRG/PHY as the LiteCamLink gateware:

| DRAM | sys | Result | Write | Read | Write+read |
|---|---|---|---|---|---|
| DDR3-300 (1:2) | 75 MHz | BIOS + BIST OK | 495 MB/s | 512 MB/s | 395 MB/s |
| DDR3-594 (1:4) | 74.25 MHz | BIOS leveling/memtest + BIST 64MB OK | 1027 MB/s | 1048 MB/s | 861 MB/s |
| DDR3-700 (1:4) | 87.75 MHz | BIST 64MB + random accesses OK | 1212 MB/s | 1233 MB/s | 1024 MB/s |
| DDR3-796 (1:4) | 99.56 MHz | no read window | - | - | - |

The LiteX-Boards target was built at 1:4/75 MHz (timing met) but not run as is (its BIOS console is
not reachable on this board without the LiteCamLink I2C/UART bridge).

## Notes / Open Items

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
  `litecamlink/gateware/ecp5ddrphy.py`), write DQS timing at 400 MHz (the GW5 1:4 PHY is a
  reference).
- Other ECP5 DDR3 boards (ECPIX-5, OrangeCrab, ButterStick, Versa ECP5, TrellisBoard, ...) can use
  the same CRG pattern: the second ECLKSYNCB/CLKDIVF BEL names depend on the DDR bank side.
