# Capture Devices: Comparison and Possibilities

Three ways to capture the HDMI output of the bench host (2026-10-01): the Elgato Cam Link 4K with
its stock firmware, the same hardware with CamLink 4K, and a cheap USB 2.0 capture stick
(MacroSilicon MS2109). Measurements and method: [LATENCY.md](LATENCY.md).

## Hardware

|                         | Cam Link 4K (stock)                       | Cam Link 4K (CamLink 4K)                          | MS2109 stick                                 |
|-------------------------|-------------------------------------------|------------------------------------------------|----------------------------------------------|
| Chips                   | ITE IT6802 HDMI RX, Lattice ECP5 FPGA, Cypress FX3 USB 3.0, 128 MB DDR3 | same                        | MacroSilicon MS2109 (HDMI RX, scaler, JPEG encoder, USB 2.0, 8051 MCU), I2C EEPROM |
| Logic                   | Vendor FPGA bitstream + FX3 firmware      | LiteX gateware + bare-metal FX3 firmware (open) | Fixed function ASIC, 8051 firmware (mask ROM + EEPROM) |
| USB                     | 3.0 bulk                                   | 3.0 bulk                                       | 2.0 isochronous (~24 MB/s)                   |
| Formats                 | YUY2/NV12/I420 at the input mode, 4K30 max | YUY2 1080p60/720p60/480p, M420 4K30/1080p60, NV12 4K30 (DDR3), scaling, crop, color controls | MJPEG up to 1080p30 / 720p60, YUY2 at 5-10 fps |
| Compression             | None                                       | None                                           | MJPEG (mandatory above ~640x480)             |
| Audio                   | UAC 48 kHz                                 | UAC 48 kHz (bit exact)                         | UAC                                          |

## Measured latency (1080p, ms from the source render, medians)

| Stage                                    | Stock (1080p60) | CamLink 4K (1080p60) | MS2109 (1080p in, MJPEG 1080p30) |
|------------------------------------------|-----------------|-------------------|----------------------------------|
| First data on USB                        | 34-35           | **3-4**           | 29-32                            |
| Frame delivered to applications (V4L2)   | 45-46           | **19**            | 62-66                            |
| On screen, ffplay (low latency options)  | 87-88           | 72                | 103-113                          |
| On screen, CamLink 4K viewer (desktop window) | 68-70         | **35-38**         | n/a (isochronous MJPEG)          |
| Scanout, CamLink 4K viewer direct display   | -               | **19-30**         | n/a                              |

At 720p60 the MS2109 delivers frames in 43-48 ms (ffplay on screen ~105 ms). The Cam Link at
720p60 is still to be measured for a same mode comparison.

- Stock buffers the frame (~2 frames) then sends it as a burst; CamLink 4K streams each line as it is
  received (first data ~3.5 ms after the render, rows at the HDMI scan time).
- The MS2109 buffers ~1.5-2 frames and compresses; at 1080p it only outputs 30 fps.
- Hands on: CamLink 4K with direct display shows no noticeable difference with the native screen;
  the MS2109 is a lot laggier at 1080p, better at 720p60 but still more lag than the Cam Link.

## IP-KVMs (published figures, not measured here)

| Device                    | Video path                     | Published latency                                    |
|---------------------------|--------------------------------|------------------------------------------------------|
| NanoKVM (Lite/Full/PCIe)  | MJPEG                          | 90-230 ms                                            |
| NanoKVM Pro               | MJPEG/H.265                    | 60-100 ms                                            |
| PiKVM V4                  | H.264 WebRTC, 1080p60, LAN     | 35-50 ms (capture 17, encode 13, browser 10-20) + 17 ms display |
| JetKVM                    | H.264 WebRTC                   | claimed 30-60 ms, measured ~98 ms click to photon    |

Vendor claims and one independent measurement with different definitions: to be measured with the
same bench method (source on HDMI-0, web client fullscreen, `latgrab`). Per stage comparison with
a CamLink 4K/LitePCIe capture + NVENC + direct display chain and sources:
[IDEAS_PCIE.md](IDEAS_PCIE.md).

## Possibilities

### Cam Link 4K: full control (done: CamLink 4K)

The FPGA sits between the HDMI receiver and the USB chip, so everything on the video path is ours:

- Line by line streaming (no frame buffer unless asked), 4K30 NV12 through the DDR3, scaling,
  crop, color, audio, test patterns, standalone boot from flash.
- Latency: device ~1 ms after a line arrives; with the low latency viewer (rows drawn as they
  arrive, direct display) the remaining latency is the phase between the source and display scans.
- Next: variable refresh alignment, low latency NV12 (reader chasing the writer), 1080p120
  (fits the GPIF/USB bandwidth in NV12/M420), device timestamps.

### MS2109: configuration of a fixed chip (not done, feasible within limits)

The video path is hardware (HDMI RX, scaler, JPEG encoder, USB 2.0) configured by registers; the
8051 boots from a mask ROM and loads extension code from an I2C EEPROM (24C16). Community work:
EEPROM and 8051 XDATA readable through the factory HID interface
([ms-tools](https://github.com/BertoldVdb/ms-tools)), custom EEPROM firmware rebuilt in C from the
mask ROM disassembly ([macrosilicon_firmware](https://github.com/kraln/macrosilicon_firmware)),
research notes ([amnemonic/MacroSilicon](https://github.com/amnemonic/MacroSilicon),
[ms21xx-firmware-research](https://github.com/Forest178/ms21xx-firmware-research)).

- Fixed limits: USB 2.0 bandwidth (~24 MB/s isochronous vs 249 MB/s for uncompressed 1080p60):
  MJPEG stays mandatory; no frame buffer or scaler changes beyond what the registers allow.
- Possible gain: the ~1.5-2 buffered frames (first data ~30 ms after the render), if the chip has
  register settings for strip/slice encoding and sending (JPEG works on 8/16 line blocks); unknown
  until the registers are mapped.
- Low risk plan: (1) read only: own HID tool, EEPROM dump/backup, firmware identification;
  (2) latency related registers tried live in RAM (power cycle undoes); (3) only if useful, own
  EEPROM firmware (an invalid EEPROM checksum leaves the chip on its mask ROM: recoverable).

## Summary

| Need                                   | Best option                                          |
|----------------------------------------|------------------------------------------------------|
| Lowest latency (gaming, live monitoring) | CamLink 4K + `camlink_view --vk` (direct display)      |
| Standard apps (OBS, VLC, ffplay)       | CamLink 4K (frame delivered 19 ms after the render vs 46 ms stock) |
| 4K30 uncompressed                      | CamLink 4K (NV12/M420) or stock (NV12)                  |
| Cheapest, latency not critical         | MS2109 (720p60 preferred over 1080p30)               |
