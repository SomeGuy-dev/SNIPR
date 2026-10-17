#!/bin/bash
# snipr_start_loop.sh — invoked automatically by mediamtx's runOnReady hook
# the instant the DJI controller starts publishing to rtmp://127.0.0.1:1935/livedji
#
# Runs the Phase 1 capture/compress pipeline, then hands the finished frame
# off to cot_bridge.py over local UDP — mirroring VLoRa's vlora_tx_bridge.py
# pattern exactly. cot_bridge.py (already running as a separate service) is
# the only thing that touches the radio; this script never does.

set -uo pipefail  # deliberately NOT -e: one bad capture cycle shouldn't kill the whole loop

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNIPR_RUN_DIR="${SNIPR_RUN_DIR:-/run/snipr}"   # RAM-backed; created at boot by /etc/tmpfiles.d/snipr.conf
PID_FILE="$SNIPR_RUN_DIR/snipr_loop.pid"
LOG_FILE="$SNIPR_RUN_DIR/snipr_loop.log"
RAW_CAPTURE="$SNIPR_RUN_DIR/snipr_raw_capture.jpg"
COMPRESSED_FRAME="$SNIPR_RUN_DIR/snipr_compressed_frame.jpg"   # fixed name, overwritten each cycle —
                                                       # NOT numbered per-cycle, so the run folder doesn't
                                                       # fill up over a long autonomous flight
TARGET_PACKETS=14   # qualified profile: 14 data packets + 1 XOR parity
SNIPR_UDP_HOST="127.0.0.1"
SNIPR_UDP_PORT=4246  # matches SNIPR_RAW_UDP_PORT in cot_bridge_SNIPR_patched.py

[[ -d "$SNIPR_RUN_DIR" && -w "$SNIPR_RUN_DIR" ]] || {
    echo "SNIPR: $SNIPR_RUN_DIR is missing or not writable (run: sudo systemd-tmpfiles --create /etc/tmpfiles.d/snipr.conf)" >&2
    exit 1
}

echo $$ > "$PID_FILE"
echo "$(date -Iseconds) SNIPR loop started (PID $$)" >> "$LOG_FILE"

cycle=0
while true; do
    cycle=$((cycle + 1))
    ts=$(date -Iseconds)

    if ! ffmpeg -y -loglevel error \
        -i rtmp://127.0.0.1:1935/livedji \
        -frames:v 1 \
        "$RAW_CAPTURE" 2>>"$LOG_FILE"; then
        echo "$ts cycle=$cycle CAPTURE FAILED — stream may have just ended, will retry" >> "$LOG_FILE"
        sleep 1
        continue
    fi

    if ! python3 "${SCRIPT_DIR}/snipr_compress.py" \
        "$RAW_CAPTURE" \
        --target-packets "$TARGET_PACKETS" \
        --out "$COMPRESSED_FRAME" \
        >> "$LOG_FILE" 2>&1; then
        echo "$ts cycle=$cycle COMPRESSION FAILED — skipping this cycle" >> "$LOG_FILE"
        continue
    fi

    # ── Hand off to cot_bridge.py over local UDP (mirrors vlora_tx_bridge.py) ──
    if python3 -c "
import socket, sys
with open('$COMPRESSED_FRAME', 'rb') as f:
    data = f.read()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(data, ('$SNIPR_UDP_HOST', $SNIPR_UDP_PORT))
sock.close()
" >> "$LOG_FILE" 2>&1; then
        echo "$ts cycle=$cycle OK -> sent to cot_bridge.py (UDP $SNIPR_UDP_HOST:$SNIPR_UDP_PORT)" >> "$LOG_FILE"
    else
        echo "$ts cycle=$cycle UDP SEND FAILED — is cot_bridge.py running?" >> "$LOG_FILE"
    fi

    sleep 1   # small gap; real cadence is dominated by TX time once
              # a frame is actually being chunked and sent over LoRa (§3.3.1)
done
