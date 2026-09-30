# CamLinX

**CamLinX** (Cam Link + LiteX): open gateware and firmware for the **Elgato Cam Link 4K** (1st gen,
USB `0fd9:0066`):

- **Gateware**: LiteX/Migen for the Lattice ECP5 LFE5U-25F, open toolchain (Yosys/nextpnr/Trellis).
- **FX3 firmware**: minimal bare-metal C for the Cypress FX3, no vendor SDK.
- **Host tools**: Python tools to boot, load, debug and test the device.

The aim is a real UVC/UAC capture card that goes further than the stock firmware on the same
hardware (latency, formats, modes, diagnostics).

> Status: working UVC/UAC capture card (4K30 NV12 through an open DDR3 frame buffer, 1080p60,
> audio), standalone boot from flash, see [doc/PLAN.md](doc/PLAN.md) and [doc/DRAM.md](doc/DRAM.md).

## Architecture

```
            +---------+  parallel video  +-----------+  GPIF-II 32-bit  +-------+  USB3
  HDMI ---->| IT6802  |----------------->|   ECP5    |<===============>|  FX3  |<======> Host
            | HDMI RX |  I2S audio       | LFE5U-25F |  Slave-SPI cfg  |       |
            +---------+----------------->|           |<----------------|       |
                 ^                        +-----------+                 +-------+
                 |     I2C (shared)            |  DDR3L 128MB             |  SPI Flash 4MB
                 +-----------------------------+--------------------------+
```

## Docs

- [doc/HARDWARE.md](doc/HARDWARE.md): what is known (and missing) about the hardware.
- [doc/PLAN.md](doc/PLAN.md): phases and status.
- [doc/BENCH.md](doc/BENCH.md): test bench setup.
- [doc/ROADMAP.md](doc/ROADMAP.md): benchmarking vs stock, improvements and new features.

## Build

```sh
./camlinx.py --build                 # Default: NV12 variant (DRAM frame buffer, 4K30 NV12).
./camlinx.py --build --variant base  # Without DRAM (YUY2/M420), --output-dir to keep both.
make -C firmware/fx3                 # FX3 firmware for build/csr.csv (CSR_CSV=... otherwise).
```

## Credits

Builds on the reverse engineering work of ktemkin (camlink-re), Greg Davill and the apertus
team (netlist), Mike Walters, Chaz Schlarp (elgato-cam-link-4k-firmware-re) and Marcus Comstedt
(fx3lafw).

## License

BSD-2-Clause, see [LICENSE](LICENSE).

<sub>Elgato and Cam Link are trademarks of Corsair. This project is not affiliated with them.</sub>
