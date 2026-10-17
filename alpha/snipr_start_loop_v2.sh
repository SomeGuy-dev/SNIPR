#!/bin/bash
# snipr_start_loop_v2.sh - Alpha: always-open capture, paced to the radio.
#
# v1 (snipr_start_loop.sh) reconnects to the drone stream for every picture and waits for
# the next full frame (3 s or more on the Pi), then encodes, hands the picture to
# cot_bridge.py and pauses 1 s. That made a picture every ~7 s although the radio can send
# one every ~3.4 s.
#
# v2 keeps one ffmpeg connected to the stream. It rewrites the newest frame in RAM about
# twice a second. Each cycle then only copies that frame, encodes it and hands it to
# cot_bridge.py (UDP). The cycle is timed so the new picture reaches the bridge just before
# the bridge has finished sending the previous one, so the radio sets the pace. The bridge
# still keeps only the newest waiting picture, so an early picture is never queued behind others.
#
# Same files and log format as v1 ($SNIPR_RUN_DIR/snipr_loop.pid and snipr_loop.log), so
# snipr_stop_loop.sh and snipr_collect.sh work unchanged.
#
# Never sends an old picture as if it were new: a frame older than SNIPR_MAX_RAW_AGE_MS is
# skipped, and the AS OF label is the frame's own capture time, not the encode time.
#
# Settings (environment, all optional):
#   SNIPR_STREAM_URL        rtmp://127.0.0.1:1935/livedji
#   SNIPR_RUN_DIR           /run/snipr
#   SNIPR_UDP_HOST/PORT     127.0.0.1 / 4246
#   SNIPR_TARGET_PACKETS    14      ceiling; the codec switch file's packets= still applies
#   SNIPR_CAPTURE_FPS       2       the newest frame is at most 1/FPS s old
#   SNIPR_TX_MS_PER_PACKET  450     bridge time per packet (the bridge paces 0.45 s). Err on the high side:
#                                   too low and pictures wait in the bridge and go out older; too high
#                                   and the radio idles a little between pictures.
#   SNIPR_SLACK_MS          300     hand a picture over this long before the bridge is expected to be free
#   SNIPR_MAX_RAW_AGE_MS    2000    never send a frame older than this
#   SNIPR_COMPRESSOR        <this folder>/snipr_compress.py

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNIPR_RUN_DIR="${SNIPR_RUN_DIR:-/run/snipr}"   # RAM-backed; created at boot by /etc/tmpfiles.d/snipr.conf
STREAM_URL="${SNIPR_STREAM_URL:-rtmp://127.0.0.1:1935/livedji}"
UDP_HOST="${SNIPR_UDP_HOST:-127.0.0.1}"
UDP_PORT="${SNIPR_UDP_PORT:-4246}"            # matches SNIPR_RAW_UDP_PORT in cot_bridge.py
TARGET_PACKETS="${SNIPR_TARGET_PACKETS:-14}"
CAPTURE_FPS="${SNIPR_CAPTURE_FPS:-2}"
TX_MS="${SNIPR_TX_MS_PER_PACKET:-450}"
SLACK_MS="${SNIPR_SLACK_MS:-300}"
MAX_AGE_MS="${SNIPR_MAX_RAW_AGE_MS:-2000}"
COMPRESSOR="${SNIPR_COMPRESSOR:-$SCRIPT_DIR/snipr_compress.py}"

PID_FILE="$SNIPR_RUN_DIR/snipr_loop.pid"
LOG_FILE="$SNIPR_RUN_DIR/snipr_loop.log"
CAP_PID_FILE="$SNIPR_RUN_DIR/snipr_capture.pid"
RAW_CAPTURE="$SNIPR_RUN_DIR/snipr_raw_capture.jpg"          # newest frame, rewritten by the capture ffmpeg
WORK_RAW="$SNIPR_RUN_DIR/snipr_raw_work.jpg"                # private copy the cycle works on
COMPRESSED_FRAME="$SNIPR_RUN_DIR/snipr_compressed_frame.jpg"

[[ -d "$SNIPR_RUN_DIR" && -w "$SNIPR_RUN_DIR" ]] || {
    echo "SNIPR: $SNIPR_RUN_DIR is missing or not writable (run: sudo systemd-tmpfiles --create /etc/tmpfiles.d/snipr.conf)" >&2
    exit 1
}

tick()     { NOW=$(( ${EPOCHREALTIME/[.,]/} / 1000 )); }
nap()      { sleep "$1" & wait $! 2>/dev/null; }                  # a stop signal interrupts this at once
sleep_ms() { local ms=$1; (( ms > 0 )) || return 0; printf -v _s '%d.%03d' $(( ms / 1000 )) $(( ms % 1000 )); nap "$_s"; }
log()      { printf '%s %s\n' "$(date -Iseconds)" "$*" >> "$LOG_FILE"; }

if [[ "$COMPRESSOR" == *.py ]]; then COMPRESS=(python3 "$COMPRESSOR"); else COMPRESS=("$COMPRESSOR"); fi
HAVE_VC="$(command -v vcgencmd || true)"

CAP_PID=""
cap_start() {
    ffmpeg -nostdin -y -loglevel error -fflags nobuffer \
        -i "$STREAM_URL" -an -vf "fps=${CAPTURE_FPS}" -q:v 2 -update 1 -atomic_writing 1 \
        "$RAW_CAPTURE" 2>>"$LOG_FILE" &
    CAP_PID=$!
    echo "$CAP_PID" > "$CAP_PID_FILE"
}
cap_stop() {
    if [[ -n "$CAP_PID" ]]; then
        kill "$CAP_PID" 2>/dev/null
        for _i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$CAP_PID" 2>/dev/null || break; sleep 0.1; done
        kill -9 "$CAP_PID" 2>/dev/null
        wait "$CAP_PID" 2>/dev/null
        CAP_PID=""
    fi
    rm -f "$CAP_PID_FILE"
}
send_udp() {
    python3 - "$COMPRESSED_FRAME" "$UDP_HOST" "$UDP_PORT" <<'PY'
import socket, sys
data = open(sys.argv[1], 'rb').read()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.sendto(data, (sys.argv[2], int(sys.argv[3])))
s.close()
PY
}
cleanup() { cap_stop; exit 0; }
trap cleanup TERM INT
trap cap_stop EXIT

# A capture left over from a loop that was killed hard must not keep running.
if [[ -f "$CAP_PID_FILE" ]]; then
    old="$(cat "$CAP_PID_FILE" 2>/dev/null)"
    if [[ -n "$old" ]] && tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null | grep -q "$RAW_CAPTURE"; then
        kill "$old" 2>/dev/null
    fi
    rm -f "$CAP_PID_FILE"
fi

echo $$ > "$PID_FILE"
log "SNIPR loop started (PID $$) v2 always-open capture fps=$CAPTURE_FPS tx_ms=$TX_MS slack_ms=$SLACK_MS url=$STREAM_URL"
rm -f "$RAW_CAPTURE" "$WORK_RAW"
cap_start

F_MS=0           # when the bridge is expected to have sent everything handed to it so far
E_MS=1500        # how long one cycle's own work takes (copy + encode + hand-off); learned as it runs
LAST_RAW_MS=0
cycle=0
cap_fail=0
stale_since=0
waited=0

while true; do
    # ---- keep the capture alive (the stream may drop and come back) ----
    if ! kill -0 "$CAP_PID" 2>/dev/null; then
        cap_fail=$(( cap_fail + 1 ))
        (( cap_fail == 1 || cap_fail % 15 == 0 )) && log "CAPTURE ENDED (stream down?) retry #$cap_fail"
        nap 2
        cap_start
        continue
    fi

    # ---- wait until it is time to start the next cycle ----
    tick
    sleep_ms $(( F_MS - E_MS - SLACK_MS - NOW ))

    # ---- the newest frame ----
    if [[ ! -s "$RAW_CAPTURE" ]]; then
        waited=$(( waited + 1 ))
        (( waited % 50 == 1 )) && log "waiting for the first frame from the stream"
        nap 0.2
        continue
    fi
    cp -p "$RAW_CAPTURE" "$WORK_RAW" 2>/dev/null || { nap 0.2; continue; }
    RAW_MS="$(date -r "$WORK_RAW" +%s%3N 2>/dev/null)" || { nap 0.2; continue; }
    tick
    AGE=$(( NOW - RAW_MS ))
    if (( AGE > MAX_AGE_MS )); then
        # the capture has stopped producing: never send an old picture as if it were new
        if (( stale_since == 0 )); then
            stale_since=$NOW
            log "STALE CAPTURE the newest frame is ${AGE} ms old, not sending"
        fi
        if (( NOW - stale_since > 15000 )); then
            log "capture silent for 15 s, restarting it"
            cap_stop; cap_start
            stale_since=$NOW
        fi
        nap 0.5
        continue
    fi
    stale_since=0
    if (( RAW_MS == LAST_RAW_MS )); then nap 0.1; continue; fi    # same frame as last time
    LAST_RAW_MS=$RAW_MS

    cycle=$(( cycle + 1 ))
    ts="$(date -Iseconds)"
    T0=$NOW
    printf -v LABEL 'AS OF %(%H:%M:%S)T' $(( RAW_MS / 1000 ))

    # ---- encode ----
    if ! "${COMPRESS[@]}" "$WORK_RAW" --target-packets "$TARGET_PACKETS" --timestamp "$LABEL" \
            --out "$COMPRESSED_FRAME" >> "$LOG_FILE" 2>&1; then
        echo "$ts cycle=$cycle COMPRESSION FAILED — skipping this cycle" >> "$LOG_FILE"
        nap 0.5
        continue
    fi
    tick; T2=$NOW

    # ---- hand over to cot_bridge.py ----
    if send_udp >> "$LOG_FILE" 2>&1; then
        tick; T3=$NOW
        cap_fail=0
        SIZE="$(stat -c %s "$COMPRESSED_FRAME")"
        CH=$(( (SIZE + 222) / 223 ))                       # 223 payload bytes per radio packet
        N=$(( CH > 1 ? CH + 1 : CH ))                      # plus the parity packet
        (( F_MS < T3 )) && F_MS=$T3
        F_MS=$(( F_MS + N * TX_MS ))
        OBS=$(( T3 - T0 ))
        E_MS=$(( (E_MS * 2 + OBS) / 3 ))
        TEMP=""
        if [[ -n "$HAVE_VC" ]]; then t="$(vcgencmd measure_temp 2>/dev/null)"; TEMP=" temp=${t#temp=}"; fi
        echo "$ts cycle=$cycle OK -> sent to cot_bridge.py (UDP $UDP_HOST:$UDP_PORT) | asof=${LABEL#AS OF } frame_age_ms=$(( T3 - RAW_MS )) work_ms=$OBS compress_ms=$(( T2 - T0 )) bytes=$SIZE tx_packets=$N bridge_free_in_ms=$(( F_MS - T3 ))$TEMP" >> "$LOG_FILE"
    else
        echo "$ts cycle=$cycle UDP SEND FAILED — is cot_bridge.py running?" >> "$LOG_FILE"
        nap 1
    fi

    # keep the RAM log from growing without limit on a long flight
    if (( cycle % 200 == 0 )) && [[ "$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 5000000 ]]; then
        { grep 'SNIPR loop started' "$LOG_FILE" | tail -1; tail -n 3000 "$LOG_FILE"; } > "$LOG_FILE.new" && mv -f "$LOG_FILE.new" "$LOG_FILE"
    fi
done
