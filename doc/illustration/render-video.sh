#!/bin/sh
# Render the CamLink 4K promo video (with its soundtrack) + hero stills, deterministic, frame by frame.
# Needs: google-chrome, node, ffmpeg, imagemagick, python3 (numpy/scipy for music.py).
# three.js is loaded from jsDelivr.
# Usage: ./render-video.sh [width] [height] [fps] [workers]   (then ./finalize.sh for doc/images)
set -e
W=${1:-1920}; H=${2:-1080}; FPS=${3:-30}; WORKERS=${4:-4}; PORT=8768; DUR=66
cd "$(dirname "$0")"
[ -d node_modules/puppeteer-core ] || PUPPETEER_SKIP_DOWNLOAD=1 npm install --silent
python3 music.py music.wav
ffmpeg -y -loglevel error -i music.wav -c:a libmp3lame -b:a 160k music.mp3
python3 -m http.server $PORT >/dev/null 2>&1 & SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1
URL=http://localhost:$PORT/video.html
# Chunks on whole seconds, rendered in parallel, then concatenated (same encoder settings).
STEP=$((DUR / WORKERS)); PIDS=""; : > _parts.txt
for i in $(seq 0 $((WORKERS - 1))); do
    T0=$((i * STEP)); T1=$(( i == WORKERS - 1 ? DUR : (i + 1) * STEP ))
    node capture.js "$URL" _part$i.mp4 $W $H $FPS $T0 $T1 > _part$i.log 2>&1 & PIDS="$PIDS $!"
    echo "file '_part$i.mp4'" >> _parts.txt
done
for p in $PIDS; do wait $p; done
ffmpeg -y -loglevel error -f concat -safe 0 -i _parts.txt -i music.wav -map 0:v -map 1:a -c:v copy \
    -c:a aac -b:a 192k -shortest -movflags +faststart camlink_4k.mp4
rm -f _part*.mp4 _part*.log _parts.txt
node sheet.js "$URL?still"           hero 3200 1800 0 && mv hero_00000.png camlink_4k-hero.png
# README snapshots (no captions).
for snap in inside:9.2 architecture:25.0 datapath:32.5 race:39.0 latency:45.0 opensource:53.0 credits:57.0; do
    node sheet.js "$URL?clean" "_snap" 2400 1350 "${snap#*:}" && mv _snap_*.png "camlink_4k-${snap%%:*}.png"
done
