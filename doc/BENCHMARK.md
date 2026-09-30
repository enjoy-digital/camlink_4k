# Benchmark: Stock Elgato firmware vs CamLinX

Same Cam Link 4K unit, same cable, host `HDMI-0` as source (`software/source.py`), captures through uvcvideo (`software/bench.py`). Latency: host render time (barcode) to first byte (V4L2 buffer timestamp) / frame complete (dequeue).

## 1080p60

| Metric | Stock | CamLinX |
|---|---|---|
| Source | 1920x1080@60 | 1920x1080@60 |
| Capture | YUYV 1920x1080@60 | YUYV 1920x1080@60 |
| FPS | 60.0 | 60.0 |
| Frame interval std (ms) | 0.04 | 0.0 |
| Frame interval max (ms) | 17.62 | 16.68 |
| Sequence gaps (drops) | 0 | 0 |
| Short frames | 0 | 0 |
| Latency first byte median (ms) | - | - |
| Latency frame complete median (ms) | - | - |
| Latency frame complete p95 (ms) | - | - |
| Capture CPU (%) | 7.8 | 4.8 |
| Start/stop OK | 20 | 20 |
| Time to first frame (s) | 0.069 | 0.031 |
| bars PSNR (dB) / SSIM | - | - |
| zoneplate PSNR (dB) / SSIM | - | - |
| text PSNR (dB) / SSIM | - | - |
| levels PSNR (dB) / SSIM | - | - |
| gradient PSNR (dB) / SSIM | - | - |

## 1080p30

| Metric | Stock | CamLinX |
|---|---|---|
| Source | 1920x1080@29.97 | 1920x1080@29.97 |
| Capture | YUYV 1920x1080@30 | YUYV 1920x1080@30 |
| FPS | 29.97 | 29.97 |
| Frame interval std (ms) | 0.03 | 0.0 |
| Frame interval max (ms) | 33.68 | 33.37 |
| Sequence gaps (drops) | 0 | 0 |
| Short frames | 0 | 0 |
| Latency first byte median (ms) | - | - |
| Latency frame complete median (ms) | - | - |
| Latency frame complete p95 (ms) | - | - |
| Capture CPU (%) | 3.5 | 3.6 |
| Start/stop OK | 20 | 20 |
| Time to first frame (s) | 0.138 | 0.098 |
| bars PSNR (dB) / SSIM | - | - |
| zoneplate PSNR (dB) / SSIM | - | - |
| text PSNR (dB) / SSIM | - | - |
| levels PSNR (dB) / SSIM | - | - |
| gradient PSNR (dB) / SSIM | - | - |

## 720p60

| Metric | Stock | CamLinX |
|---|---|---|
| Source | 1280x720@60 | 1280x720@60 |
| Capture | YUYV 1280x720@60 | YUYV 1280x720@60 |
| FPS | 60.0 | 60.0 |
| Frame interval std (ms) | 0.0 | 0.0 |
| Frame interval max (ms) | 16.68 | 16.67 |
| Sequence gaps (drops) | 0 | 0 |
| Short frames | 0 | 0 |
| Latency first byte median (ms) | - | - |
| Latency frame complete median (ms) | - | - |
| Latency frame complete p95 (ms) | - | - |
| Capture CPU (%) | 2.7 | 2.7 |
| Start/stop OK | 20 | 20 |
| Time to first frame (s) | 0.069 | 0.032 |
| bars PSNR (dB) / SSIM | - | - |
| zoneplate PSNR (dB) / SSIM | - | - |
| text PSNR (dB) / SSIM | - | - |
| levels PSNR (dB) / SSIM | - | - |
| gradient PSNR (dB) / SSIM | - | - |

## 2160p30

| Metric | Stock | CamLinX |
|---|---|---|
| Source | 3840x2160@30 | 3840x2160@30 |
| Capture | NV12 3840x2160@30 | YUYV 1920x1080@30 |
| FPS | 30.04 | 30.0 |
| Frame interval std (ms) | 0.1 | 0.0 |
| Frame interval max (ms) | 33.34 | 33.35 |
| Sequence gaps (drops) | 0 | 0 |
| Short frames | 0 | 0 |
| Latency first byte median (ms) | - | - |
| Latency frame complete median (ms) | - | - |
| Latency frame complete p95 (ms) | - | - |
| Capture CPU (%) | 8.6 | 2.2 |
| Start/stop OK | 20 | 20 |
| Time to first frame (s) | 0.264 | 0.098 |
| bars PSNR (dB) / SSIM | - | - |
| zoneplate PSNR (dB) / SSIM | - | - |
| text PSNR (dB) / SSIM | - | - |
| levels PSNR (dB) / SSIM | - | - |
| gradient PSNR (dB) / SSIM | - | - |

