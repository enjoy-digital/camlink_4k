# LiteCamLink Plan

Goal: a fully open replacement for the Cam Link 4K firmware (LiteX gateware + minimal bare-metal
FX3 firmware + host tools), validated step by step on hardware, ending as a capture card that
does better than the stock one on the same hardware.

## Process

Each step: **Implement -> Simulate (pytest/Migen) -> Test on hardware (scripted) -> Document ->
Commit/Push**. The stock firmware can be restored at any time from the flash backup.

## Phases

| #  | Phase                          | Content                                                                                     | Status |
|----|--------------------------------|---------------------------------------------------------------------------------------------|--------|
| 0  | Bootstrap                      | Repo, docs, platform, flash backup, stock I2C/EDID dumps, bench scripts.                    | Done   |
| 1  | FX3 bare-metal hello           | Bare-metal C (fx3lafw register defs), RAM boot, EP0 vendor requests (ident, peek/poke).     | Done   |
| 2  | FPGA configuration from FX3    | Slave-SPI bitstream load from the host, DONE readback. `litecamlink.py --load`.             |        |
| 3  | FX3 USB streaming              | Bulk IN from FX3 memory, host throughput benchmark (target >350MB/s on SuperSpeed).         |        |
| 4  | GPIF-II + FPGA pattern         | 32-bit GPIF state machine, LiteX GPIF PHY, pattern generator, CRC checked on the host.      |        |
| 5  | UVC                            | UVC 1.1 descriptors (YUY2 first), probe/commit, payload headers, VLC/ffplay/OBS. No quirks. |        |
| 6  | DDR3 frame buffer              | LiteDRAM memtest (SSTL135 vs SSTL15), frame buffering with LiteDRAM DMA.                    |        |
| 7  | HDMI capture                   | IT6802 init + EDID, mode detection, video capture (2 px/clk at 4K), packing to YUY2/NV12.   |        |
| 8  | Audio                          | I2S capture, in-band over GPIF, UAC 1.0, A/V sync.                                          |        |
| 9  | Beyond stock                   | Low latency, extra modes/EDIDs (1440p...), scaling, formats (P010/RGB), stats, self-test.   |        |

## Debug / Control Path

- EP0 vendor requests on our FX3 firmware: ident, memory peek/poke, FPGA load, I2C access.
- FPGA CSRs through a FX3 bridge (I2C slave in the FPGA first, then in-band GPIF), exposed to the
  host as a `litex_server` compatible `RemoteClient`.
- Optional UART on LED pins `A6`/`A9` with external wiring.

## Beyond Stock: Ideas

- Low latency mode: slice/line based streaming without full frame buffering.
- Modes: 1440p, 4K->1080p60 scaling, custom EDIDs, 1080p120 within 300MHz TMDS.
- Formats: NV12/YUY2/P010/RGB24, full range/limited range control, correct colorimetry.
- Frame timestamps, dropped frames/stats counters, input info exposed through UVC controls.
- Test pattern / self-test mode, latency measurement tool, recovery/update tool.
