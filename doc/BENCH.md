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
