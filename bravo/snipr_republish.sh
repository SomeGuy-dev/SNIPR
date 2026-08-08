#!/bin/bash

set -uo pipefail

LATEST_FRAME="/tmp/snipr_latest.jpg"
RTSP_URL="rtsp://127.0.0.1:8554/snipr"

while true; do
    if [[ -f "$LATEST_FRAME" ]]; then
        cat "$LATEST_FRAME"
    fi
    sleep 0.2
done | ffmpeg \
    -hide_banner \
    -loglevel warning \
    -f image2pipe \
    -vcodec mjpeg \
    -framerate 5 \
    -i - \
    -c:v libx264 \
    -profile:v baseline \
    -level 3.1 \
    -pix_fmt yuv420p \
    -r 5 \
    -g 10 \
    -an \
    -f rtsp \
    -rtsp_transport tcp \
    "$RTSP_URL"
