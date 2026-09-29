# Hardware Validation Checklist (features developed offline, 2026-09-28 night)

Everything below passed simulation (`python3 -m pytest test`: 27 tests) and timing (seeds 1-3 pass, default seed 1:
hdmi 157 MHz / 150, sys 115 MHz / 100, fx3 115 MHz / 100.8) but was written while the device was
offline. Validate in this order: each step relies on the previous ones.

Setup: replug the Cam Link (flash block 0 is erased: it enumerates as the FX3 bootloader
`04b4:00f3`), HDMI-0 of this PC as source, screen unlocked for latency/quality.

```
make -C firmware/fx3 && python3 litecamlink.py --build
python3 software/camlink.py boot
```

Most checks below are scripted: `python3 software/validate.py` (all steps, PASS/FAIL table) or
`python3 software/validate.py --list` / `validate.py <step> ...`.

## 1. Base + robustness (commit cd6a483, 94b5d4d, FX3 PHY fix)

| Check | Command | Expected |
|---|---|---|
| Enumeration | `lsusb -v -d 1209:0001` | UVC (2 formats) + UAC interfaces, SuperSpeed |
| Link stats | `camlink.py stats` | `phy_cr_timeouts` 0, `ss_to_usb2_fallbacks` 0 |
| 1080p capture | `bench.py run --firmware litecamlink` | as before (60/30 fps, 0 drops) |
| First frame | `software/uvc_raw.py --seconds 2` | `header errors` 0 (was 1-2: first payload) |
| Input fps | `camlink.py hdmi-status` | `fps` ~60.000 / 29.97 / 30.000 |
| Signal loss | stream, `xrandr --output HDMI-0 --off`, then `--auto` | dark blue "no signal" pattern within ~0.3 s, HDMI back after re-probe, stream keeps running |
| Mode change | stream, `xrandr --output HDMI-0 --mode 1280x720` | pattern (size mismatch) or HDMI, no hang |

## 2. Hang recovery (FPGA watchdog)

The FX3 internal watchdog does not recover a hang inside an IRQ handler (see `doc/HARDWARE.md`,
2026-09-29: `hang()` with the FIQ watchdog lost the device until a replug). Replaced by the FPGA
watchdog: heartbeat on FX3 GPIO45, FX3 RESET# driven by the FPGA (4s default, armed after 8
heartbeat edges, disabled by the firmware before intentional reboots).

1. After `camlink.py boot`: `camlink.py csr fx3_watchdog_status` -> armed (1), `fx3_watchdog_resets` 0.
2. `camlink.py reboot`: bootloader `04b4:00f3` and no watchdog reset afterwards
   (`fx3_watchdog_resets` still 0 after `boot`, the FPGA keeps its state across FX3 resets
   until reloaded: read it before `boot` reloads the FPGA, e.g. with a `fx3-load` only).
3. `CamLink().hang()` (hang in the USB IRQ handler): device back as `04b4:00f3` within ~4s.
4. Then the audio + video start/stop sequence that hung the FX3 (`uvc_raw.py` + `audio_check.py`
   loops), reading `camlink.py stats` (fallbacks/PHY timeouts) when it survives.

## 3. Audio (commit 01c02bd)

| Check | Command | Expected |
|---|---|---|
| Test counter | `camlink.py audio-source test; audio_check.py test` | 1 bad sample (start) |
| With video | counter test in parallel with `uvc_raw.py` | 0 video header errors, audio glitches only at restarts |
| HDMI audio | `camlink.py audio-source hdmi`; `speaker-test -D hdmi -t sine -f 1000` to HDMI-0; `audio_check.py hdmi` | 1000 Hz both channels, no silence |

If HDMI audio is silent: check I2S activity (`audio_samples` CSR increasing with source=hdmi),
IT6802 regs 0x52 (0x20), 0x10/0x11 vs `<backup dir>/stock_it6802_signal_mac4k30.txt`. If the
level/sign looks wrong: left-justified vs I2S framing (I2SReceiver MSB position).

## 4. Downscale / crop / color (commits 6f37159, ab2ad50, ba13e91)

| Check | Command | Expected |
|---|---|---|
| Box filter | 4K30 source, capture 1080p30, compare zone plate/text vs previous line-skip | less aliasing, correct colors |
| Crop | `software/uvc_xu.py crop 1920 1080` while streaming 1080p from a 4K source | bottom-right quadrant, pixel exact |
| Crop off | `uvc_xu.py crop off` | back to downscale |
| Input info | `uvc_xu.py info` | resolution, fps, color space |
| Controls | `v4l2-ctl -d /dev/videoN -l`; set brightness/contrast/saturation | visible effect, defaults = identity |
| Letterbox | source 1280x720 (`xrandr --output HDMI-0 --mode 1280x720`), capture 1080p | 720p image centered, black borders |
| Center crop | source 2560x1440, capture 1080p (crop off) | centered 1080p region (not the pattern) |

## 5. 4K30 M420 (commit bee4433)

1. Firmware with the 403.2 MHz PLL: `make -C firmware/fx3 clean && make -C firmware/fx3 PLL_FBDIV=21`,
   check I2C/IT6802/1080p still OK (all clocks scale with `FX3_SYS_CLK`).
2. `stream-test` throughput at 100.8 MHz PCLK (expect ~395-400 MB/s).
3. `v4l2-ctl --list-formats-ext` -> M420 3840x2160@30; capture with `v4l2cap.Capture(..., 3840, 2160,
   30, pixfmt="M420")` and `m420_to_rgb` (check colors, ~30 fps, 0 drops, `hdmi_in_overflow` 0).
4. Bandwidth margin (~0.4% with 1 audio packet per switch): with audio streaming, try
   `camlink.py audio-batch 4` (thread switches every 4 ms) and tune the `gpif_switch_guard` CSR
   (1024 cycles default) down to the smallest value without audio/video corruption
   (`uvc_raw.py` + `audio_check.py test`).
5. Throughput with 32KB DMA buffers: `make -C firmware/fx3 clean && make -C firmware/fx3
   DMA_BUF_SIZE=32768 PLL_FBDIV=21` (FX3 bss 272KB), compare `stream-test` and 4K30 drops.
6. If stable: make `PLL_FBDIV=21` the default, add 2160p30 M420 to `bench.py` (stock: NV12).

## 6. DDR3 1:4 (DDR3-800 class bandwidth for 4K30 NV12)

See `doc/DRAM.md`: bring-up with the 4 prebuilt bitstreams (`build_dram12_cpu`, `build_dram14_cpu`,
`build_dram12`, `build_dram14`), BIOS then `software/dram.py` (init/leveling/BIST bandwidth).

## 7. EDID (not implemented: needs hardware investigation)

The EDID seen by sources comes from a separate DDC EEPROM ("Cam Link 4K"). `camlink.py i2c-scan`
to see whether the FX3 I2C bus reaches it (0x50); otherwise look for an IT6802 DDC pass-through or
EEPROM write-protect path. Keep 0xC0=0x07 / 0x87=0 until then.

## 8. End of session

Re-flash the final images (`camlink.py flash-bitstream`, `flash-fx3`), cold boot, `flash-recover`
test, re-run `bench.py` for both firmwares with the screen unlocked, update `doc/BENCHMARK.md`.
