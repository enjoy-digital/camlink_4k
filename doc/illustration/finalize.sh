#!/bin/sh
# Deliverables for doc/images from the full quality renders of render-video.sh: README hero (1600px),
# high resolution stills (3200px, with/without text) and the web-friendly video.
set -e
cd "$(dirname "$0")"
OUT=../images
mkdir -p $OUT
convert camlink_4k-hero.png        -resize 1600x -quality 92 $OUT/camlink_4k.jpg
convert camlink_4k-hero.png                      -quality 93 $OUT/camlink_4k-hires.jpg
for s in inside architecture datapath race latency opensource credits; do
    convert camlink_4k-$s.png -resize 1600x -quality 90 $OUT/camlink_4k-$s.jpg
done
# Poster: the hero still as the first 2 frames (GitHub's player shows the first frame until played).
ffmpeg -y -hide_banner -loglevel error -i camlink_4k.mp4 -loop 1 -framerate 30 -t 0.0667 -i camlink_4k-hero.png \
    -filter_complex "[1:v]scale=1920:1080,format=yuv420p,setsar=1[p];[0:v]format=yuv420p,setsar=1[v];[p][v]concat=n=2:v=1:a=0[o];[0:a]adelay=67:all=1[a]" \
    -map "[o]" -map "[a]" -c:v libx264 -preset slow -crf 25 -c:a aac -b:a 160k \
    -pix_fmt yuv420p -movflags +faststart $OUT/camlink_4k.mp4
ls -l $OUT/camlink_4k*
