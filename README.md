# CamLinX 4K

**CamLinX 4K** (Cam Link + LiteX): open gateware and firmware for the **Elgato Cam Link 4K** (1st gen,
USB `0fd9:0066`), a real UVC/UAC capture card that goes further than the stock firmware on the same
hardware:

- **One Python script**: LiteX/Migen gateware for the Lattice ECP5 LFE5U-25F, open toolchain
  (Yosys/nextpnr/Trellis), no vendor bitstream.
- **No vendor firmware**: minimal bare-metal C for the Cypress FX3 (no SDK), standalone boot from
  the SPI flash.
- **4K30 NV12** through an open DDR3 frame buffer (LiteDRAM, DDR3-594 at 1:4), 4K30 M420, 1080p60
  YUY2 with scaling/crop/color controls, HDMI audio (UAC).
- **Upstream**: ECP5 DDR3 1:4 contributed to LiteDRAM/LiteX-Boards.
- **Host tools**: Python tools to boot, flash, debug, validate and benchmark the device over USB
  (made for agentic development: video/audio in, CSRs/flash/FPGA control out).

> Status: working capture card, validated on hardware (`software/validate.py`), see
> [doc/PLAN.md](doc/PLAN.md) and [doc/DRAM.md](doc/DRAM.md).

## Architecture

The ECP5 runs one LiteX SoC ([`camlinx_4k.py`](camlinx_4k.py)): the IT6802 video is captured (`HDMIIn`),
cropped/scaled (`Canvas`), color adjusted (`ColorAdjust`), written as NV12 planes to DDR3
(`NV12FrameBuffer` on LiteDRAM), read back into UVC payloads (`UVCPacketizer`) and sent with the
I2S audio (`AudioSource`) to the FX3 over GPIF-II (`GPIFStreamer`). The FX3 firmware
([`firmware/fx3`](firmware/fx3)) configures the FPGA from the SPI flash, initializes the DRAM,
bridges CSRs and exposes the UVC/UAC interfaces to the host.

```
            +---------+  parallel video  +-----------+  GPIF-II 32-bit  +-------+  USB3
  HDMI ---->| IT6802  |----------------->|   ECP5    |<===============>|  FX3  |<======> Host
            | HDMI RX |  I2S audio       | LFE5U-25F |  Slave-SPI cfg  |       |
            +---------+----------------->|           |<----------------|       |
                 ^                        +-----------+                 +-------+
                 |     I2C (shared)            |  DDR3L 128MB             |  SPI Flash 4MB
                 +-----------------------------+--------------------------+
```

## Built on LiteX

Most of the SoC already existed as open-source cores: [LiteX](https://github.com/enjoy-digital/litex)
(SoC builder, CSRs, streams), [LiteDRAM](https://github.com/enjoy-digital/litedram) (DDR3
controller, crossbar, BIST), [Migen](https://github.com/m-labs/migen) and
[LiteX-Boards](https://github.com/litex-hub/litex-boards), built with Yosys/nextpnr/Project Trellis.
CamLinX 4K adds the capture pipeline and gives back ECP5 DDR3 at 1:4 to LiteDRAM (#408 merged,
#409/#410) and LiteX-Boards (#866 merged), see [doc/upstream](doc/upstream).

## Docs

- [doc/HARDWARE.md](doc/HARDWARE.md): what is known (and missing) about the hardware.
- [doc/PLAN.md](doc/PLAN.md): phases and status.
- [doc/DRAM.md](doc/DRAM.md): DDR3 1:4 and the NV12 frame buffer.
- [doc/BENCH.md](doc/BENCH.md): test bench setup.
- [doc/LATENCY.md](doc/LATENCY.md): latency measurements, low latency viewer (`software/viewer`), next steps.
- [doc/COMPARISON.md](doc/COMPARISON.md): stock vs CamLinX vs a cheap USB 2.0 stick (MS2109), possibilities of each.
- [doc/IDEAS_PCIE.md](doc/IDEAS_PCIE.md): design note, LitePCIe video input/output cards (line based capture, phase locked output, low latency streaming, IP-KVM).
- [doc/ROADMAP.md](doc/ROADMAP.md): benchmarking vs stock, improvements and new features.
- [doc/upstream](doc/upstream): LiteDRAM/LiteX-Boards contributions.

## Build

```sh
./camlinx_4k.py --build                 # Default: NV12 variant (DRAM frame buffer, 4K30 NV12).
./camlinx_4k.py --build --variant base  # Without DRAM (YUY2/M420), --output-dir to keep both.
make -C firmware/fx3                 # FX3 firmware for build/csr.csv (CSR_CSV=... otherwise).
python3 software/camlink.py boot     # Load FX3 firmware + bitstream, HDMI and DRAM init.
```

## Credits

Builds on the reverse engineering work of ktemkin (camlink-re), Greg Davill and the apertus
team (netlist), Mike Walters, Chaz Schlarp (elgato-cam-link-4k-firmware-re) and Marcus Comstedt
(fx3lafw), on LiteX/LiteDRAM/Migen, and on the open ECP5 toolchain from YosysHQ (Yosys, nextpnr,
Project Trellis). Code written by a Claude agent (Anthropic), directed by
[Enjoy-Digital](https://enjoy-digital.fr).

## License

BSD-2-Clause, see [LICENSE](LICENSE).

Custom work, 15+ years of FPGA: [enjoy-digital.fr](https://enjoy-digital.fr).

<sub>Elgato and Cam Link are trademarks of Corsair. This project is not affiliated with them.</sub>
