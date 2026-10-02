# Test Bench

## Minimal setup

- Cam Link 4K on the host, on a USB 3 port for SuperSpeed (a USB 2 port works for bring-up).
- Power switching (YKUSH, `ykushcmd -d N` / `ykushcmd -u N`) to power-cycle the Cam Link
  without human intervention. Without it, reboots go through the stock/FX3 reset commands.
- HDMI source: the host GPU output, modes driven with `xrandr`, test patterns full screen.
- Optional: logic analyzer (sigrok) on I2C, UART on LED pins `A6`/`A9`.

YKUSH, `xrandr` and the `HDMI-0` output named in the docs and scripts are specific to the
development bench: adapt them to your host (`xrandr` lists its outputs).

## Access rights

`software/udev/70-camlink.rules` gives the logged-in user access (`TAG+="uaccess"`) to the stock
device (`0fd9:0066/0067` and its HID interface), the FX3 bootloader (`04b4:00f3`) and the CamLink 4K
firmware (`1209:0001`):

    sudo cp software/udev/70-camlink.rules /etc/udev/rules.d/
    sudo udevadm control --reload-rules && sudo udevadm trigger

## Flash backup

The stock flash is dumped (twice, compared) before any write with `cl4k-fwtool.py` from
[elgato-cam-link-4k-firmware-re](https://github.com/schlarpc/elgato-cam-link-4k-firmware-re)
(stock firmware, vendor HID interface), and kept outside the repository:

    sudo ./tools/cl4k-fwtool.py dump <backup dir>/camlink4k_<serial>_flash.bin

It is never committed (copyrighted firmware, unit serial number).

## Development loop (no power switching needed)

Flash block 0 (start of the FX3 image) is erased on the development unit, so the FX3 boot ROM
always falls back to USB boot (`04b4:00f3`) and our firmware is loaded to RAM:

    make -C firmware
    python3 software/camlink.py fx3-load firmware/build/fx3.img  # -> 1209:0001
    python3 software/camlink.py ident
    python3 software/camlink.py reboot                                # -> 04b4:00f3

`reboot` does a FX3 hard reset through the boot ROM. A power cycle (YKUSH or replug) is only
needed if the firmware hangs.

To go back to the stock firmware, write the flash backup back from the CamLink 4K firmware
running from RAM, then power cycle:

    python3 software/camlink.py flash-recover                     # Erase the FX3 image -> 04b4:00f3.
    python3 software/camlink.py boot                              # CamLink 4K firmware in RAM.
    python3 software/camlink.py flash-write <backup>.bin --force  # Whole flash (stock regions too).

To run the stock firmware from RAM without writing the flash (comparisons, `software/bench.py`),
extract its FX3 image from the backup and load it from the bootloader:

    python3 software/camlink.py fx3-extract <backup>.bin stock_fx3.img
    python3 software/camlink.py fx3-load stock_fx3.img

`bench.py` looks for it at `~/camlink_backup/stock_fx3.img` unless `CAMLINK_STOCK_FX3` is set.
