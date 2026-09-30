# CamLinX 4K Plan

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
| 2  | FPGA configuration from FX3    | Slave-SPI bitstream load from the host, DONE readback. `camlinx.py --load`.                 | Done   |
| 3  | FX3 USB streaming              | Bulk IN EP1 on SuperSpeed, async host reader: 300MB/s sustained (GPIF 96MHz x 32-bit).        | Done   |
| 4  | GPIF-II + FPGA pattern         | GPIF master waveform, auto DMA, FPGA GPIFStreamer + counter/pattern sources, CRC-free counter check. | Done   |
| 5  | UVC                            | UVC 1.1 YUY2 480p/720p/1080p @30/60, FPGA packetizer, probe/commit, works with uvcvideo/ffmpeg/VLC. | Done   |
| 6  | DDR3 frame buffer              | LiteDRAM 1:4 DDR3-594 (upstreamed), NV12 frame buffer: 4K30 NV12 at 30 fps, DRAM init in the FX3 firmware, standalone flash boot (`doc/DRAM.md`). | Done   |
| 7  | HDMI capture                   | IT6802 init/EDID/HPD, DDR capture + 2x downscale: MacBook 4K30 -> 1080p30 via uvcvideo/VLC. | WIP    |
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

## Status Notes

- Development loop: `software/camlink.py boot` (FX3 RAM load + FPGA load), `camlink.py csr` (FPGA CSRs
  through the FX3 I2C master and the FPGA I2C bridge), `stream-test`, `uvc-raw-test`, `i2c-dump`.
- UVC measured through uvcvideo: 640x480@60 59.94fps, 1280x720@60 59.94fps, 1920x1080@60 59.94fps.
- HDMI: MacBook Pro 4K30 input captured as 1920x1080@30 (downscaled), 30.00fps sustained, no dropped
  frames. To do: colour validation with a known pattern, native 1080p60 (SDR path), full 4K modes.
- Known limitations: the first frame after a stream start is lost (FX3 first-word quirk); a ZLP
  follows each short payload (ignored by uvcvideo); 4K30 needs > 300MB/s or NV12.
