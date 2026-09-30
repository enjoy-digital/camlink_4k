# Cam Link 4K Hardware

What is known about the first generation Elgato Cam Link 4K (USB `0fd9:0066` on SuperSpeed,
`0fd9:0067` on High-Speed), where it comes from and what is still missing. MK.2 (`0x007B`) and
Rev.3 (`0x00A1`) are different designs and are not covered.

## Parts

| Part                   | Device                                   | Notes                                     |
|------------------------|------------------------------------------|-------------------------------------------|
| FPGA                   | Lattice ECP5 `LFE5U-25F-8BG381C`         | IDCODE `0x41111043`, Slave-SPI config.    |
| USB controller         | Cypress FX3 `CYUSB3014` (ARM926EJ-S)     | 512KB SRAM, GPIF-II 32-bit to the FPGA.   |
| HDMI receiver          | ITE `IT6802` rev `0xB1` (IT6801FN marking)| Parallel video + I2S to the FPGA.        |
| DRAM                   | Micron `MT41K64M16TW` DDR3L, 128MB x16   | Stock: SSTL15, LiteX: SSTL135 (both work?)|
| SPI Flash              | Winbond `W25Q32JVIQ`, 4MB                | On FX3 SPI only.                          |
| DDR termination        | Anpec `APL5338`                          |                                           |
| Clock                  | 27MHz on FPGA `B11` (PCLKT0_0)           |                                           |

## Buses

### FX3 <-> FPGA

- **GPIF-II, 32-bit**: FX3 `DQ0-15` = GPIO0-15, `DQ16-27` = GPIO33-44, `DQ28-31` = GPIO46-49,
  `PCLK` = GPIO16 -> FPGA `L19` (PCLKT3_0, through a resistor), `CTL0-5` = GPIO17-22,
  `CTL7` = GPIO24, `CTL11` = GPIO28, `CTL12` = GPIO29. All on FPGA banks 2/3 (3.3V).
  The stock GPIF waveform is irrelevant: both sides are ours.
- **FPGA configuration (Slave-SPI, CFG[2:0]=001)**: FX3 GPIO50 -> `SN` (T2), GPIO51 -> `CCLK`
  (U3), GPIO52 -> `D0/MOSI` (W2), GPIO57 <- `D1/MISO` (V2). Bit-banged by the FX3
  (ktemkin/camlink-re: `0x79` refresh, `0xC6` ISC enable, `0x0E` erase, `0x46` set address,
  `0x7A` burst, poll DONE bit 8 of status).
- **FX3 RESET#** is connected to FPGA `P20`: the FPGA watchdog (`gateware/watchdog.py`) resets the
  FX3 when its heartbeat (GPIO45, toggled from the firmware main loop) stops.
- **FX3 internal watchdog** (2026-09-29, measured): timer 0 counts at 32.768kHz (the backup divider
  has no effect); the reset mode (MODE0=0) never resets nor flags an event (with any BITS0, the
  counter just wraps); the interrupt mode (MODE0=1) sets INTR0 (write 1 to clear) on expiry and
  raises VIC line 4. The timer is in the always-on domain: writes land after a few 32kHz cycles
  and a TIMER0 write only lands while the timer is disabled (MODE0=3); the value survives FX3
  resets. Routed to the FIQ with a hard reset handler, it recovered hangs in thread mode but not
  a hang inside an IRQ handler (FIQ apparently not taken while an IRQ is serviced): not used.
- **Extra GPIOs**: FX3 GPIO27 <-> `C7`, GPIO45 <-> `C8`. FX3 GPIO26 <-> `D7` is not a direct
  connection (FPGA output not seen by the FX3, netlist says "TR?, I2C sel?").

**Verified on hardware** (`camlinx_4k.py --with-pintest` + `camlink.py pintest`): all 32 GPIF
`DQ` lines, `PCLK`, the 9 `CTL` lines and GPIO27 (FPGA -> FX3), GPIO45 (FX3 -> FPGA).
FPGA Slave-SPI configuration from the FX3 verified (IDCODE `0x41111043`, ~0.4s for 100KB).

### I2C (shared)

FX3 GPIO58/59 (I2C master), FPGA `P18`/`P19`, IT6802 `PCSCL`/`PCSDA`.

| Address          | Device                                                         |
|------------------|----------------------------------------------------------------|
| `0x10`           | Stock FPGA register file (video timings, version at `0xFE`).   |
| `0x30/0x31/0x32` | IT6802 banks 0/1/2 (aliases +4, `0x49` = bank 2).              |
| `0x38`           | Unknown device (FIFO-like read side effects on `0x59`/`0x5B`). |
| `0x50`           | EDID presented to the HDMI source.                             |

### IT6802 -> FPGA

- 24 of the 36 `QE` outputs: `QE4-11` on `A7 A8 E9 B9 B6 E6 D6 E7`, `QE16-23` on
  `A12 A13 B13 C13 D13 E13 A14 C14`, `QE28-35` on `D14 E14 B15 C15 D15 E15 A16 B16`.
- `PCLK` -> `D11` (PCLKT1_1), `DE` -> `C16`, `HSYNC` -> `D16`, `VSYNC` -> `B17`.
- I2S: `MCLK` -> `A10`, `SCK` -> `B12` (PCLKT1_0), `WS` -> `A17`, `I2S0` -> `B18`.
- `SYSRSTN` <- FPGA `R20`, `INT#` -> FX3 GPIO25.
- Stock design processes 2 pixels/clock (register `0x2E` = 1920 at 3840 wide).

### Misc

- LED #1 on `A6` and `C11`, LED #2 footprint on `A9`/`C10` (not always populated).
- FX3 boot: PMODE = 0Z1 (SPI boot, USB fallback). No I2C EEPROM. FSLC = 000.
- JTAG (FPGA and FX3) only on tie-off resistors, no header.

### IO Banks (from the stock bitstream, `ecpunpack`)

| Bank  | VCCIO  | Use                             |
|-------|--------|---------------------------------|
| 0, 1  | 3.3V   | IT6802 video/I2S, LEDs, clk27   |
| 2, 3  | 3.3V   | GPIF-II, I2C, resets            |
| 6, 7  | VREF   | DDR3 (stock uses SSTL15)        |

## FX3 GPIF-II Findings (empirical, see firmware/fx3/gpif.c)

- FX3 as GPIF master: internal clock + CLK_OUT drives PCLK (GPIO16 -> FPGA L19). PIB DLL must stay
  disabled (0xf8f0): with the DLL enabled the PCLK output is unstable. 96MHz with div_x2 = 8.
- Waveform memory is not readable/writable while the GPIF is enabled.
- Alphas (SAMPLE_DIN) only act on state entry: two DATA states alternate on each VALID clock.
- CTL1 output = omega 16 (EMPTY_FULL_TH0), active low as "DMA ready".
- Beta bit 30 (COMMIT) commits a partial buffer (short packet, followed by a ZLP). The FX3 latches
  DQ one cycle before the EOP strobe and uses it as the first word of the next buffer: the FPGA
  drives the next payload's first word on DQ during EOP.
- USB3: CLEAR_FEATURE(ENDPOINT_HALT) requires resetting the endpoint sequence number
  (PROT_SEQ_NUM), otherwise later transfers fail (OOSERR).
- DQ timing window (FPGA side, 100.8MHz PCLK received from the FX3): DQ from fabric registers
  (transition ~1.5-3.5ns after the received PCLK edge, routing dependent) works; from IO registers
  (earlier) video breaks; from DDR cells switching at the falling edge (later) the words shift by
  one. The window is narrow: on some placements the slowest line (DQ[29]) corrupts rare audio
  samples (the default build seed is chosen bit exact). Open item: IO register + output delay
  (DELAYF, runtime sweep to measure the window) for a placement independent DQ timing.
- The FPGA must not drive CTL1/CTL4 (FX3 FLAG outputs): per-pin output enables (a single TSTriple
  `oe` drove all CTL pins until 5851a2d).

## IT6802 (from bring-up + public reference driver knowledge)

- Answers at 7-bit `0x49` once SYSRSTN (FPGA `R20`) is released; ID `54 49 02 68`, rev `B1`.
  Other stock addresses (`0x30-0x32`, `0x38`, `0x50`) are programmed by the init (EDID RAM
  slave address register `0x87`, MHL `0x34`, CEC `0x86`).
- No internal test pattern: the output pixel clock comes from the input TMDS PLL.
- Output modes (`0x51`/`0x65`): 24-bit SDR up to ~162MHz; 4K30 requires the 0.5x PCLK dual-edge
  (DDR) mode (148.5MHz, data on both edges). 8-bit per channel on QE[11:4]/[23:16]/[35:28],
  matching the 24 wired lines (B, G, R for RGB).

## Flash Layout (stock)

| Offset     | Content                                                          |
|------------|------------------------------------------------------------------|
| `0x000000` | FX3 boot image (`CY` header, sum-of-words checksum, no signature).|
| `0x040000` | 256-byte header: bitstream size (LE32) + its complement.         |
| `0x040100` | ECP5 bitstream (compressed, Diamond 3.9, `UVC_yuan_impl1.ncd`).  |
| `0x3F0000` | Settings (last video mode).                                      |

The unit serial number is stored as a USB string descriptor inside the FX3 image.

## Flash Layout (CamLinX)

| Offset     | Content                                                                   |
|------------|---------------------------------------------------------------------------|
| `0x000000` | CamLinX FX3 boot image (read by the FX3 boot ROM).                    |
| `0x040000` | Stock bitstream header + bitstream, left untouched (stock image RAM-load). |
| `0x100000` | 256-byte header: size (LE32), ~size, magic `LCLK`, then the bitstream.    |
| `0x3F0000` | Stock settings (untouched).                                               |

At startup the FX3 firmware loads the `LCLK` bitstream (if valid) and initializes the IT6802.
Tools: `camlink.py flash-bitstream`, `flash-fx3`, `flash-dump`, `fpga-boot`, and `flash-recover`
(erases block 0 and resets: the boot ROM falls back to the USB bootloader `04b4:00f3`).

## Stock USB Behaviour

- UVC 1.10 bulk (EP `0x83`), UAC 1.0 48kHz stereo 16-bit (iso EP `0x81`), vendor HID (interface 2:
  flash access, I2C tunnel, mode, reset).
- Formats follow the input: YUY2/NV12/I420 at the input resolution/rate; 4K30 max, no scaling.
- bFormatIndex bug in probe/commit (Linux `UVC_QUIRK_FIX_FORMAT_INDEX`, 5.14+).
- Two EDIDs in the FX3 image (1080p60 and 4K30), 300MHz max TMDS, no HDCP.

## Missing / To Recover

| # | Topic                                         | Plan                                                                 |
|---|-----------------------------------------------|----------------------------------------------------------------------|
| 1 | IT6802 init sequence (datasheet under NDA)    | Dump all IT6802 registers through the stock HID I2C tunnel per mode; sniff stock boot I2C with a logic analyzer; public vendor BSP drivers. |
| 2 | IT6802 output format (SDR/DDR, YUV422/RGB, pixel clock at 4K) | Frequency counters and LiteScope on the FPGA side once IT6802 init is ours. |
| 3 | DDR3 VCCIO (1.35V vs 1.5V)                    | Measure; LiteDRAM memtest with SSTL135 already passed in 2019.       |
| 4 | Pin verification of the netlist spreadsheet   | FX3 <-> FPGA done (PinTest). IT6802/I2C/I2S pins still to verify.    |
| 5 | Audio path                                    | Ours: I2S -> FPGA -> GPIF (in-band) -> FX3 -> UAC.                   |
| 6 | Device at I2C `0x38`                          | Low priority.                                                        |

## References

- LiteX-Boards `camlink_4k` platform/target and Linux-on-LiteX-VexRiscv `CamLink4K` (2019).
- ktemkin/camlink-re: FX3 exploration firmware, `camlink` host tool, factory dumps.
  https://github.com/ktemkin/camlink-re
- schlarpc/elgato-cam-link-4k-firmware-re (MIT, 2025-2026): USB, HID protocol, flash map, EDID,
  I2C map, bitstream analysis, `cl4k-fwtool.py`. https://github.com/schlarpc/elgato-cam-link-4k-firmware-re
- apertus wiki + WIP netlist spreadsheet (Greg Davill et al.), see [pinout.csv](pinout.csv).
  https://apertus.org/wiki/index.php?title=Elgato_CAM_LINK_4K
- Mike Walters, "Patching my Cam Link 4K to play nicer on Linux" (2020).
- zeldin/fx3lafw (MIT): blob-free FX3 register definitions and BSP. https://github.com/zeldin/fx3lafw
- greatscottgadgets/pyfwup: `fx3load` (FX3 RAM boot over USB).
