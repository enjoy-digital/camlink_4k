# Hardware validation log (development, historical)

> Records the 2026-09 hardware bring-up of the features developed offline; kept as a log, not
> maintained. The current hardware check is `python3 software/validate.py` (12 steps).

The checklist was written while the device was offline, for features that passed simulation and
timing at the time (test suite and build seed have changed since: now `python3 -m pytest test`, 87
tests, NV12 default build seed 6). Validate in this order: each step relies on the previous ones.

Setup: replug the Cam Link (flash block 0 is erased: it enumerates as the FX3 bootloader
`04b4:00f3`), HDMI-0 of the bench host as source, screen unlocked for latency/quality.

```
make -C firmware/fx3 && python3 camlink_4k.py --build
python3 software/camlink.py boot
```

Most checks below are scripted: `python3 software/validate.py` (all steps, PASS/FAIL table) or
`python3 software/validate.py --list` / `validate.py <step> ...`.

## 1. Base + robustness (commit cd6a483, 94b5d4d, FX3 PHY fix)

| Check | Command | Expected |
|---|---|---|
| Enumeration | `lsusb -v -d 1209:0001` | UVC (2 formats) + UAC interfaces, SuperSpeed |
| Link stats | `camlink.py stats` | `phy_cr_timeouts` 0, `ss_to_usb2_fallbacks` 0 |
| 1080p capture | `bench.py run --firmware camlink_4k` | as before (60/30 fps, 0 drops) |
| First frame | `software/uvc_raw.py --seconds 2` | `header errors` 0 (was 1-2: first payload) |
| Input fps | `camlink.py hdmi-status` | `fps` ~60.000 / 29.97 / 30.000 |
| Signal loss | stream, `xrandr --output HDMI-0 --off`, then `--auto` | dark blue "no signal" pattern within ~0.3 s, HDMI back after re-probe, stream keeps running |
| Mode change | stream, `xrandr --output HDMI-0 --mode 1280x720` | pattern (size mismatch) or HDMI, no hang |

## 2. Hang recovery (FPGA watchdog)

The FX3 internal watchdog does not recover a hang inside an IRQ handler (see `doc/HARDWARE.md`,
2026-09-29: `hang()` with the FIQ watchdog lost the device until a replug). Replaced by the FPGA
watchdog: heartbeat on FX3 GPIO45, FX3 RESET# driven by the FPGA (4s default, armed after 8
heartbeat edges, disabled by the firmware before intentional reboots).

1. After `camlink.py boot`: `camlink.py csr fx3_watchdog_status` -> armed (1), also while
   streaming. OK (2026-09-29).
2. `camlink.py reboot`: bootloader `04b4:00f3` stays enumerated (same device number after 10s: no
   watchdog reset). OK. (The resets counter cannot be read after an FX3 reset: the firmware
   reloads the flash bitstream at startup.)
3. `CamLink().hang()` (hang in the USB IRQ handler): device back as `04b4:00f3` after ~5s. OK.
4. Not covered: FX3 main loop alive but USB dead (2026-09-29, audio+video stall below: EP0 stopped
   answering, the host dropped the device, heartbeat still running). Next: gate the heartbeat on
   USB liveness (frame counter `PROT_FRAMECNT`/`DEV_FRAMECNT` advancing unless suspended/U3, see
   `LNK_LTSSM_STATE`): measure these registers with `camlink.py peek` while streaming, idle and
   autosuspended before implementing it.
5. Then the audio + video start/stop sequence that hung the FX3 (`uvc_raw.py` + `audio_check.py`
   loops), reading `camlink.py stats` (fallbacks/PHY timeouts) when it survives.

## 3. Audio (commit 01c02bd)

2026-09-29: FPGA counter audio alone PASS (144000 samples, bit exact). Audio (counter) streaming
then a 1080p video start (4K input, downscale): exactly one video frame sent (127 x 32KB bursts,
1 EOP), then the GPIF stalls: video FLAG ready, audio FLAG low with 93 words queued, HDMI FIFO
overflowing, `arecord` I/O error, EP0 dead a few seconds later, device dropped by the host (needs a
replug). Deterministic (3/3). Video first then audio (HDMI source, audio not enabled): OK.
Suspect: FX3 GPIF thread switch after the video EOP commit (COMMIT/EOP_WAIT -> audio thread).
Root causes and fixes (commit 433490f): full GPIF restarts reset the running stream's endpoint
(per-thread restarts now), video gated on the audio FLAG while the audio ring is full in steady
state (xflag_off), audio starved by back to back video bursts (audio priority), switch guard
1024 -> 256. Result: validate.py 12/12, 4K30 M420 + audio 30s 0 gaps, bit-exact audio.
HDMI audio (PC, `speaker-test -D hw:0,3` = the Cam Link ELD device): 1000Hz on the right
channel only with `-s 2`, left with `-s 1`; needs an IT6802 audio reset (0x10 bit 1) each time
the source (re)starts its audio stream, done by the firmware (0xB3 bit 3 rising edge).

| Check | Command | Expected |
|---|---|---|
| Test counter | `camlink.py audio-source test; audio_check.py test` | 1 bad sample (start) |
| With video | counter test in parallel with `uvc_raw.py` | 0 video header errors, audio glitches only at restarts |
| HDMI audio | `camlink.py audio-source hdmi`; `speaker-test -D hdmi -t sine -f 1000` to HDMI-0; `audio_check.py hdmi` | 1000 Hz both channels, no silence |

If HDMI audio is silent: check I2S activity (`audio_samples` CSR increasing with source=hdmi),
IT6802 regs 0x52 (0x20), 0x10/0x11 vs a stock register dump of the unit. If the
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

1. Firmware with the 403.2 MHz PLL (`PLL_FBDIV=21`, now the default): check I2C/IT6802/1080p
   still OK (all clocks scale with `FX3_SYS_CLK`).
2. `stream-test` throughput at 100.8 MHz PCLK (expect ~395-400 MB/s).
3. `v4l2-ctl --list-formats-ext` -> M420 3840x2160@30; capture with `v4l2cap.Capture(..., 3840, 2160,
   30, pixfmt="M420")` and `m420_to_rgb` (check colors, ~30 fps, 0 drops, `hdmi_in_overflow` 0).
4. Bandwidth margin (~0.4% with 1 audio packet per switch): with audio streaming, try
   `camlink.py audio-batch 4` (thread switches every 4 ms) and tune the `gpif_switch_guard` CSR
   (1024 cycles at the time, now 256: see §3) down to the smallest value without audio/video
   corruption (`uvc_raw.py` + `audio_check.py test`).
5. Throughput with 32KB DMA buffers (`DMA_BUF_SIZE=32768`, now the default; FX3 bss 272KB), compare
   `stream-test` and 4K30 drops.
6. If stable: make `PLL_FBDIV=21` the default (done), add 2160p30 M420 to `bench.py` (stock: NV12).

## 6. DDR3 1:4 (DDR3-800 class bandwidth for 4K30 NV12)

See `doc/DRAM.md`: bring-up with 4 bitstreams built in local build directories (`build_dram12_cpu`,
`build_dram14_cpu`, `build_dram12`, `build_dram14`, not in the repository), BIOS then
`software/dram.py` (init/leveling/BIST bandwidth).

## 7. EDID (implemented)

Initially planned as a hardware investigation (separate DDC EEPROM?). Implemented since: the EDID is
generated by `software/edid.py` (1080p60 + 2160p30, CEA-861 extension), compiled into the firmware
(`firmware/fx3/gen_edid.py`) and written to the IT6802 EDID RAM (`firmware/fx3/it6802.c`, RAM at
0x50 on the PC bus, programmed through reg 0x87).

## 8. End of session

Re-flash the final images (`camlink.py flash-bitstream`, `flash-fx3`), cold boot, `flash-recover`
test, re-run `bench.py` for both firmwares with the screen unlocked, update `doc/BENCHMARK.md`.

## Standalone images (2026-09-29)

Flashed: bitstream (header written last, verified) + FX3 image fdbd1ad at block 0. Reset -> SS
enumeration in ~3s, FPGA from flash, validate.py 12/12 PASS (4K30 input). Dev loop from here:
`camlink.py flash-recover` (erases block 0, back to the USB bootloader), then `camlink.py boot`.
Boot watchdog: the device resets every 10s while nothing configures it (e.g. USB charger).
