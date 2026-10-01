# Ideas: PCIe Video Input/Output Cards with LiteX/LitePCIe

Design note (2026-10-01), estimates based on the CamLink 4K measurements ([LATENCY.md](LATENCY.md),
[COMPARISON.md](COMPARISON.md)), not results. Idea: the same "line by line, nothing buffered
unless asked" approach as CamLink 4K, on PCIe cards in the class of the Blackmagic DeckLink Mini
Monitor 4K (output) and Mini Recorder 4K (input), with LiteX/LitePCIe gateware.

## Why

The CamLink 4K measurements show where the latency of a capture/display chain goes once the device
streams lines (1080p60, direct display, from the source render):

| Stage                                            | Today                 | Who                    |
|--------------------------------------------------|-----------------------|------------------------|
| Source compositor + HDMI scan to the row         | 3.5 + 3.5..15 ms      | Source PC              |
| HDMI line -> host memory (CamLink 4K + USB)         | ~1 ms                 | Capture                |
| Host -> displayed buffer (viewer, direct display)| ~0.3 ms               | Host                   |
| Wait for the display scan to reach the row       | 0..16.7 ms (phase)    | Display (not ours)     |

The capture side is done; the display phase is the biggest remaining stage and only an output
whose timing we control removes it. USB/UVC also has frame semantics (and USB transfer
granularity) that a PCIe design does not need.

## Output card ("monitor")

    host (slices) --PCIe DMA--> line FIFO / DRAM ring --> timing generator --> HDMI TX --> monitor
                                                           ^ phase locked to the input frames

- **Host -> card**: LitePCIe DMA of each slice (e.g. 128KB, ~30 rows at 1080p) into a line FIFO
  or a small DRAM ring (LiteDRAM) on the card: ~10-50 us per slice.
- **Timing generator owned by us**: the output frame starts a few lines after the input frame
  (phase lock), then the scanout races the incoming lines. Locking to a source with its own clock:
  pixel clock trim (fractional MMCM/PLL steps, a slow PLL loop on the frame start error, accepted
  by any monitor) or vertical blanking stretch (VRR/FreeSync style, monitor dependent).
- **Expected**: input row -> output scanout of the same row ~0.5-1 ms (a few lines of margin)
  instead of 0-16.7 ms; no compositor, no swap, no GPU in the path.
- **Also**: deterministic output timing (no dropped/duplicated frames once locked), 10-bit/4:4:4
  output, overlay/test patterns in gateware, hardware timestamps of the output lines.

## Input card ("recorder")

    HDMI RX --> line buffers --> slice packer + timestamp --PCIe DMA--> host ring (pinned) --> app
                                                                    \--> MSI or write pointer polling

- **Card -> host**: lines DMA'd into a pinned host ring as soon as a slice (e.g. 8-16 lines) is
  complete, with a write pointer/MSI per slice: data in host memory ~0.1-0.3 ms after the line was
  received (16 lines at 1080p60 = 0.24 ms), no USB packetization, no UVC frame semantics.
- **Bandwidth**: 4K60 YUY2 ~1 GB/s, 4K60 RGB/4:4:4 ~1.5 GB/s: PCIe Gen2 x4 (~1.6 GB/s usable)
  is tight for 4:4:4, Gen3 x4 comfortable; several 1080p channels on one card.
- **Hardware timestamps** per line/slice (card clock, disciplinable to PTP): exact capture timing
  for A/V sync, latency measurement and frame pacing.
- **Zero copy options**: DMA into memory shared with the encoder/GPU (dma-buf, peer to peer DMA
  where the platform allows it), so a slice encoder can start on the first rows.
- **Linux**: LitePCIe kernel driver + user space library (DMA rings, MMAP), a V4L2 front end for
  standard applications (frame based) next to a slice API for low latency ones.

## Input + output on one card

- Local passthrough of a few lines (microseconds) with the host in the loop for monitoring only.
- Host in the loop (overlays, analysis, processing): HDMI in -> host -> HDMI out ~1-2 ms with the
  output phase locked to the input.
- **Measurement instrument**: both ends timestamped in hardware on one clock (+ a photodiode on
  the monitor): exact glass to glass of any player, encoder or network stack (today's method,
  barcode + screen grab, only approximates the display side).

## For low latency streaming

Capture (frame based UVC) and display (vsync, compositor) typically cost 1-2 frames each; the
network and slice based codecs can be a few ms. With a line based input card at the sender and a
phase locked output card at the receiver (estimates, 60 Hz, monitor excluded):

| Stage                 | Typical        | With the cards                              |
|-----------------------|----------------|---------------------------------------------|
| Capture               | 17-33 ms       | ~0.3 ms                                     |
| Encode (slices)       | 5-15 ms        | 2-4 ms                                      |
| Network (LAN)         | ~1 ms          | ~1 ms                                       |
| Decode (slices)       | 5-10 ms        | 2-3 ms                                      |
| Display               | 17-33 ms       | ~1 ms (phase locked to the received stream) |
| **Total**             | **~50-100 ms** | **~7-10 ms**                                |

The receiver output card needs clock recovery from the stream (frame timestamps -> pixel clock
trim) instead of a local input.

### External sources

Software streaming stacks usually capture on the source machine itself (screen/GPU frames). When
the video comes from equipment that cannot run software, a capture device is needed, and today's
options are frame based (measured at 1080p, frame in the application: MS2109 62-66 ms, Cam Link
stock 45-46 ms, CamLink 4K 19 ms with the first lines at ~3.5 ms; a LitePCIe input card would have
each slice in memory ~0.3 ms after reception).

- **Sources**: game consoles and set-top boxes, medical imaging (endoscopes, ultrasound, surgical
  cameras: remote assistance), industrial HMIs/PLC panels/embedded PCs (remote supervision and
  control), broadcast gear and cameras (HDMI/SDI: remote production), servers and PCs at BIOS/boot
  level or locked down systems (remote KVM), drones/robots with closed HDMI video downlinks.
- **Gain with a frame encoder** (NVENC...): line based capture puts the complete frame in memory as
  the HDMI frame ends, 25-45 ms earlier than stock/cheap capture.
- **Line based end to end** (LAN, production): line based codecs (e.g. JPEG XS, sub-ms, used in
  professional video over IP) can encode from the first slices: capture, encode, network, decode
  and a phase locked output all in lines, a few ms in total. Over the internet H.264/HEVC stay
  frame based, the capture/display gains still apply.
- **Output side**: when the destination is not a PC (projector, monitor wall, medical display,
  broadcast switcher, SDI monitor), an output card phase locked to the stream is the client.
- **Integration**: FFmpeg-style pipelines are frame based; the low latency path needs a slice level
  input/output API (frame mode for every other application).

### Example: an open low latency IP-KVM

Capture card on the target's HDMI + USB keyboard/mouse emulation toward the target (USB HID device
core in the same FPGA) + a low latency streaming stack: remote control of any machine without
software on it.

| Category              | Examples                                  | Video path                                         | Fit                                          |
|-----------------------|-------------------------------------------|----------------------------------------------------|----------------------------------------------|
| Enterprise IP-KVM     | Raritan, Aten, Lantronix, Adder           | Dedicated capture/compression, vendor software     | Servers, data centres; feature rich, closed  |
| Open/cheap IP-KVM     | PiKVM, JetKVM, NanoKVM, TinyPilot         | SBC + HDMI bridge, MJPEG/H.264, web client         | Cheap, popular, "good enough" latency        |
| Game streaming        | Parsec, Moonlight/Sunshine, Steam Link    | Software on the source machine                     | Low latency, not for external equipment      |
| Pro AV over IP        | SDVoE, NDI, JPEG XS/SMPTE 2110 systems    | Dedicated hardware or frame based software         | Production; costly or network heavy          |
| **This idea**         | LiteX capture + streaming + HID emulation | Line based capture, GPU encode, phase locked/direct display | External equipment with game streaming class latency, open |

Published IP-KVM latencies (vendor claims and one independent measurement, definitions differ;
not measured here, to be measured with the same bench method):

| Device                     | Video path                         | Published latency                                                    |
|----------------------------|------------------------------------|----------------------------------------------------------------------|
| NanoKVM (Lite/Full/PCIe)   | MJPEG (H.264 work in progress)     | 90-230 ms                                                            |
| NanoKVM Pro                | MJPEG/H.265                        | 60-100 ms (as low as 60 ms at 2K)                                    |
| PiKVM V4                   | H.264 WebRTC, 1080p60, LAN         | 35-50 ms capture to displaying: capture 17, encode 13, network/queue <2, browser 10-20, + display 17 ms |
| JetKVM                     | H.264 WebRTC                       | claimed 30-60 ms, measured ~98 ms click to photon (~85 ms in the device) |
| Generic                    | MJPEG/WebSocket, H.264/WebRTC      | 100-150 ms, 50-100 ms                                                |

Sources: [PiKVM latency](https://docs.pikvm.org/latency/),
[NanoKVM Pro](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Pro/introduction.html),
[JetKVM review](https://warpkvm.com/blog/jetkvm-review),
[comparison](https://computingforgeeks.com/best-ip-kvm-homelab/).

Against PiKVM the capture stage is close (its capture is ~1 frame, like CamLink 4K frame complete at
19 ms; the 25-45 ms capture gain is against stock/cheap capture): the gains are in the other
stages (estimates, 1080p60):

| Stage            | PiKVM V4 (published)   | Line based capture + NVENC + native client + direct display (estimates) |
|------------------|------------------------|--------------------------------------------------------------------------|
| Capture          | 17 ms (frame)          | 17 ms (frame encoder) / ~1-2 ms (slice encoding)                         |
| Encode           | 13 ms                  | ~2-5 ms (NVENC)                                                          |
| Network          | <2 ms                  | ~1 ms                                                                    |
| Client           | 10-20 ms (browser)     | ~2-5 ms (native client)                                                  |
| Display          | +17 ms                 | ~1-8 ms (direct display / phase locked output)                           |
| **Total**        | **~57-70 ms**          | **~23-36 ms frame based, ~7-15 ms with slices**                          |

- **Advantages**: low latency on any HDMI source without software on it; uncompressed capture into
  NVENC HEVC/AV1 instead of MJPEG (sharper text/UI at lower bitrates); open and auditable stack (a
  KVM has BIOS level control of its targets); low input latency (USB HID in the FPGA, 1 kHz
  polling); provable latency (hardware timestamps).
- **Weaknesses**: cost and form factor (FPGA PCIe card + GPU host vs ~$100 SBC boxes); feature
  parity (virtual media, ATX power, multi-user, authentication); HDCP sources not capturable;
  sub-frame end to end needs a line based codec (JPEG XS: licensing/IP).
- **Prototype without a PCIe card**: CamLink 4K (USB 3, line based) + a host GPU encoder + a USB
  HID emulator, measured glass to glass against a PiKVM/JetKVM with the bench tools.

## Building blocks

| Block                      | Status                                                               |
|----------------------------|----------------------------------------------------------------------|
| LitePCIe (DMA, MSI, Linux driver, user space) | Exists (used on several boards, e.g. the TriXium FK33/XCU1525 work) |
| LiteDRAM (frame/line rings) | Exists (DDR3/DDR4, used here at 1:4 on ECP5)                        |
| LiteX video timing generator, HDMI/DVI out | Exists for FPGA serializers up to ~1080p60; 4K60 needs HDMI 2.0 rates (transceivers or an external HDMI 2.0 TX) |
| HDMI input                 | CamLink 4K `HDMIIn` (IT6802 parallel video, 4K30); 4K60 needs an HDMI 2.0 RX |
| Line/slice streaming, NV12/M420 packing, audio | CamLink 4K cores (`camlink_4k/gateware`)              |
| Phase lock (pixel clock trim / VRR) | New                                                         |
| Host slice API, viewer, latency tools | CamLink 4K `software/viewer` (libusb today), to port to LitePCIe |

## Open questions

- **Hardware**: repurposing DeckLink cards means the same reverse engineering as the Cam Link
  (FPGA, HDMI chips, pinout, clocking; not known here) and the FPGA vendor toolchain if it is not
  supported by the open flow; alternatively a LiteX-Boards PCIe FPGA board with an HDMI daughter
  card (in and out, HDMI 2.0 for 4K60).
- **4K60**: HDMI 2.0 TX/RX (transceivers or external chips) and PCIe Gen3 x4 class bandwidth.
- **Phase lock**: pixel clock trim range/step on the chosen FPGA, monitor tolerance; VRR over
  HDMI depends on the monitor.
- **Software**: slice API + V4L2 front end, FFmpeg integration (sender: slices to a slice
  encoder; receiver: decoded slices to the output ring with clock recovery).
- **First step**: output card alone (biggest gain: the display phase), measured with CamLink 4K as
  the input and the existing latency tools.
