# Test Bench

## Minimal setup

- Cam Link 4K on the host, on a USB 3 port for SuperSpeed (a USB 2 port works for bring-up).
- Power switching (YKUSH, `ykushcmd -d N` / `ykushcmd -u N`) to power-cycle the Cam Link
  without human intervention. Without it, reboots go through the stock/FX3 reset commands.
- HDMI source: the host GPU output, modes driven with `xrandr`, test patterns full screen.
- Optional: logic analyzer (sigrok) on I2C, UART on LED pins `A6`/`A9`.

## Access rights

`software/udev/99-camlink.rules` gives user access to the stock device (`0fd9:0066/0067`) and the
FX3 bootloader (`04b4:00f3`):

    sudo cp software/udev/99-camlink.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

## Flash backup

The stock flash is dumped (twice, compared) before any write, and kept outside the repository:

    python3 cl4k-fwtool.py dump <backup dir>/camlink4k_<serial>_flash.bin

It is never committed (copyrighted firmware, unit serial number).

## Development loop (no power switching needed)

Flash block 0 (start of the FX3 image) is erased on the development unit, so the FX3 boot ROM
always falls back to USB boot (`04b4:00f3`) and our firmware is loaded to RAM:

    make -C firmware/fx3
    python3 software/camlink.py fx3-load firmware/fx3/build/fx3.img  # -> 1209:0001
    python3 software/camlink.py ident
    python3 software/camlink.py reboot                                # -> 04b4:00f3

`reboot` does a FX3 hard reset through the boot ROM. A power cycle (YKUSH or replug) is only
needed if the firmware hangs.

To go back to the stock firmware, write back the flash backup (FX3 image blocks at least) with
a flash-capable firmware, then power cycle.
