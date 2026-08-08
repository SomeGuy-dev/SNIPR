#!/bin/bash
# snipr_stop_loop.sh — invoked automatically by mediamtx's runOnNotReady hook
# the instant the DJI stream ends (operator stopped it, or it dropped).
#
# Kills the loop cleanly — no orphaned process left running against a
# stream that no longer exists.

PID_FILE="/tmp/snipr_loop.pid"
LOG_FILE="/tmp/snipr_loop.log"

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "$(date -Iseconds) SNIPR loop stopped (PID $PID)" >> "$LOG_FILE"
    else
        echo "$(date -Iseconds) SNIPR loop PID $PID not running (already stopped?)" >> "$LOG_FILE"
    fi
    rm -f "$PID_FILE"
else
    echo "$(date -Iseconds) SNIPR stop called but no PID file found" >> "$LOG_FILE"
fi
