# GPIF / USB Throughput (4K30)

Model: `software/throughput_model.py` (analytic: active-line input rate vs GPIF cycles, UVC
headers, burst guard/head lead, audio phases with 2 x switch_guard idle cycles, on-chip FIFOs).

```
3840x2160@30 M420: 373 MB/s average, 389 MB/s during active lines
  PCLK   buf  audio batch sw_guard |  margin USB MB/s  FIFO
  96.0   16K    off     1        0 |  -2.31%    389.1    NO
  96.0   16K     on     1     1024 |  -4.61%    389.3    NO
  96.0   16K     on     4     1024 |  -2.95%    389.3    NO
  96.0   16K     on     1      256 |  -3.01%    389.3    NO
  96.0   16K     on     4      256 |  -2.55%    389.3    NO
  96.0   32K    off     1        0 |  -1.78%    388.9    NO
  96.0   32K     on     1     1024 |  -4.07%    389.1    NO
  96.0   32K     on     4     1024 |  -2.42%    389.1    NO
  96.0   32K     on     1      256 |  -2.47%    389.1    NO
  96.0   32K     on     4      256 |  -2.02%    389.1    NO
 100.8   16K    off     1        0 |   2.56%    389.1    ok
 100.8   16K     on     1     1024 |   0.38%    389.3    ok
 100.8   16K     on     4     1024 |   1.95%    389.3    ok
 100.8   16K     on     1      256 |   1.90%    389.3    ok
 100.8   16K     on     4      256 |   2.33%    389.3    ok
 100.8   32K    off     1        0 |   3.07%    388.9    ok
 100.8   32K     on     1     1024 |   0.88%    389.1    ok
 100.8   32K     on     4     1024 |   2.46%    389.1    ok
 100.8   32K     on     1      256 |   2.41%    389.1    ok
 100.8   32K     on     4      256 |   2.84%    389.1    ok
```

Conclusions:
- 4K30 M420 needs the 403.2MHz FX3 PLL (GPIF 100.8MHz, `PLL_FBDIV=21`, now the default in
  `firmware/fx3/Makefile`, as the 32KB DMA buffers): at 96MHz the GPIF cannot keep up with the
  active lines and the on-chip FIFOs (~3.5k words) cannot absorb it.
- Audio thread switches are the main overhead (2 x switch_guard per switch): the firmware uses
  >= 4 audio packets per switch for 4K streams (+3ms audio latency). switch_guard is 256 (gateware
  reset value and firmware setting, `fpga_ctrl.c`): 1024 overflowed the video FIFO with audio at
  4K30 M420 on hardware, 256 gives ~2% margin even with 1 packet per switch.
- Without DRAM the USB link must sustain ~389MB/s during active lines (373MB/s average), close to
  the FX3 bulk limit. The DDR3 1:4 path (doc/DRAM.md) used as a frame FIFO brings it down to the
  373MB/s average (vertical blanking used) - one more reason for the DRAM work, for M420 too.
