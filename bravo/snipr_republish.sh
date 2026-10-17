#!/bin/bash
# snipr_republish.sh v3.2 - Bravo: serve the newest reassembled SNIPR frame as RTSP video,
# with an on-screen age readout so a stale picture cannot pass for a live one.
#
#   AGE 12 s          white box   = seconds since the picture on screen finished arriving at Bravo
#   STALE 35 min      red box     = nothing new for STALE_AFTER seconds or more
#
# The "AS OF hh:mm:ss" text inside the picture is still the capture time on Alpha.
#
# v3:   frames may be plain JPEG or HEVC/H.264 from the Alpha compressor (snipr_decode.py).
# v3.1: "-preset veryfast -tune zerolatency" stops x264 holding back about 2.7 s of
#       frames (its default lookahead) before it hands a new picture on to RTSP.
# v3.2: the stream no longer freezes while a picture is being decoded, and it runs on a
#       steady 5 pictures per second.
#         * Decoding now happens in the background. The feeder keeps repeating the
#           current picture and swaps in the new one the moment it is ready. (In v3 the
#           feeder waited for the decoder, so the stream stalled once per picture and
#           ATAK's timer ran slow.)
#         * The feeder ticks against the clock (0.2 s per tick) instead of "sleep 0.2
#           plus whatever the loop cost", so it really delivers 5 pictures per second.
#         * AGE is counted from the picture that is on screen, not from the newest file,
#           so it includes the decode time instead of hiding it.
set -uo pipefail

LATEST_FRAME="${SNIPR_LATEST_FRAME:-/tmp/snipr_latest.jpg}"
SHOW_FRAME="${SNIPR_DECODED_FRAME:-/tmp/snipr_decoded.jpg}"
SHOWN_MT_FILE="${SHOW_FRAME}.mt"
DECODER="${SNIPR_DECODER:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/snipr_decode.py}"
CANVAS="${SNIPR_CANVAS:-640x480}"
RTSP_URL="${SNIPR_RTSP_URL:-rtsp://127.0.0.1:8554/snipr}"
AGE_OK="${SNIPR_AGE_OK:-/tmp/snipr_age_ok.txt}"
AGE_STALE="${SNIPR_AGE_STALE:-/tmp/snipr_age_stale.txt}"
FONT="${SNIPR_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf}"
STALE_AFTER="${SNIPR_STALE_AFTER:-30}"
TICK_US=200000                      # 5 pictures per second
export SNIPR_CANVAS="$CANVAS"
CW="${CANVAS%x*}"; CH="${CANVAS#*x}"

# drawtext re-reads these files before every frame, so always replace them atomically.
write_atomic() { printf '%s\n' "$2" > "$1.tmp" && mv -f "$1.tmp" "$1"; }

# Convert the newest reassembled frame into the picture that is shown. $1 = that frame's
# modification time. Runs in the background; a non-zero exit means "could not be used".
convert_job() {
    local mt="$1" magic tail
    magic=$(od -An -tx1 -N2 "$LATEST_FRAME" 2>/dev/null | tr -d ' \n')
    if [[ "$magic" == "ffd8" ]]; then
        tail=$(tail -c2 "$LATEST_FRAME" 2>/dev/null | od -An -tx1 | tr -d ' \n')
        [[ "$tail" == "ffd9" ]] || return 1            # still being written
        cp -f "$LATEST_FRAME" "$SHOW_FRAME.tmp" && mv -f "$SHOW_FRAME.tmp" "$SHOW_FRAME" || return 1
    else
        python3 "$DECODER" "$LATEST_FRAME" "$SHOW_FRAME" >&2 || return 1
    fi
    write_atomic "$SHOWN_MT_FILE" "$mt"
}

# Wait until the next tick of a steady 0.2 s clock. Needs bash 5 (EPOCHREALTIME); on an
# older bash it falls back to a plain sleep.
TICK_FD=""
NEXT_US=0
setup_clock() {
    local f
    [[ -n "${EPOCHREALTIME:-}" ]] || return 0
    f=$(mktemp -u "${TMPDIR:-/tmp}/snipr_tick.XXXXXX") || return 0
    mkfifo "$f" 2>/dev/null || return 0
    { exec {TICK_FD}<>"$f"; } 2>/dev/null || TICK_FD=""
    rm -f "$f"
}
now_us() { local t=$EPOCHREALTIME; NOW_US=${t/[.,]/}; }
next_tick() {
    local d
    if [[ -z "$TICK_FD" ]]; then sleep 0.2; return; fi
    now_us
    if (( NEXT_US == 0 )); then NEXT_US=$NOW_US; fi
    NEXT_US=$(( NEXT_US + TICK_US ))
    d=$(( NEXT_US - NOW_US ))
    # Far behind, or the system clock was stepped (the Pi has no clock of its own until it
    # syncs): restart the tick clock instead of racing to catch up or sleeping for hours.
    if (( d < -1000000 || d > 1000000 )); then NEXT_US=$(( NOW_US + TICK_US )); d=$TICK_US; fi
    if (( d > 0 )); then
        printf -v d '%d.%06d' $(( d / 1000000 )) $(( d % 1000000 ))
        read -r -t "$d" -u "$TICK_FD" || true
    fi
}

JOB_PID=0
feeder() {
    local st mt key lastkey="" jobkey="" tries=0 rc now sec_prev="" age ok stale last="" shown_mt=""
    trap '(( JOB_PID > 0 )) && kill "$JOB_PID" 2>/dev/null' EXIT
    setup_clock
    while true; do
        # 1. Did the background conversion finish? A frame that cannot be read is retried
        #    for a moment (it may still be mid-write), then skipped; the previous picture stays.
        if (( JOB_PID > 0 )) && ! kill -0 "$JOB_PID" 2>/dev/null; then
            wait "$JOB_PID"; rc=$?
            JOB_PID=0
            if (( rc == 0 )); then
                lastkey="$jobkey"; tries=0
            else
                tries=$(( tries + 1 ))
                if (( tries >= 3 )); then
                    echo "snipr_republish: new frame could not be used, keeping the previous picture" >&2
                    lastkey="$jobkey"; tries=0
                fi
            fi
        fi
        # 2. A new frame arrived: convert it once, in the background (newest frame wins).
        if (( JOB_PID == 0 )) && [[ -f "$LATEST_FRAME" ]]; then
            st=$(stat -c '%Y %y %s' "$LATEST_FRAME" 2>/dev/null) || st=""
            if [[ -n "$st" && "$st" != "$lastkey" ]]; then
                mt=${st%% *}
                convert_job "$mt" &
                JOB_PID=$!; jobkey="$st"
            fi
        fi
        # 3. Age of the picture that is on screen, refreshed once a second.
        printf -v now '%(%s)T' -1
        if [[ "$now" != "$sec_prev" ]]; then
            sec_prev="$now"
            [[ -r "$SHOWN_MT_FILE" ]] && { read -r shown_mt < "$SHOWN_MT_FILE" || true; }
            if [[ "$shown_mt" =~ ^[0-9]+$ ]]; then
                age=$(( now - shown_mt ))
                (( age < 0 )) && age=0
                if   (( age < STALE_AFTER )); then ok="AGE ${age} s";  stale=""
                elif (( age < 120 ));         then ok="";  stale="STALE ${age} s"
                elif (( age < 7200 ));        then ok="";  stale="STALE $(( age / 60 )) min"
                else                               ok="";  stale="STALE > 2 h"
                fi
                if [[ "$ok|$stale" != "$last" ]]; then
                    write_atomic "$AGE_OK" "$ok"
                    write_atomic "$AGE_STALE" "$stale"
                    last="$ok|$stale"
                fi
            fi
        fi
        # 4. Repeat the current picture. If cat fails while the picture still exists, the
        #    reader (ffmpeg) is gone: exit so the service restarts instead of looping into a dead pipe.
        if [[ -f "$SHOW_FRAME" ]]; then
            cat "$SHOW_FRAME" 2>/dev/null || { [[ -f "$SHOW_FRAME" ]] && exit 1; }
        fi
        next_tick
    done
}

# drawtext needs both text files to exist before ffmpeg starts.
write_atomic "$AGE_OK" ""
write_atomic "$AGE_STALE" ""

# A picture from an earlier run must not be shown as if it were new.
rm -f "$SHOW_FRAME" "$SHOWN_MT_FILE"

feeder | ffmpeg \
    -hide_banner \
    -loglevel warning \
    -f image2pipe \
    -vcodec mjpeg \
    -framerate 5 \
    -i - \
    -vf "scale=${CW}:${CH}:force_original_aspect_ratio=decrease:flags=bicubic:in_range=pc:out_range=tv,pad=${CW}:${CH}:(ow-iw)/2:(oh-ih)/2:black,drawtext=fontfile=${FONT}:textfile=${AGE_OK}:reload=1:x=12:y=12:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=8,drawtext=fontfile=${FONT}:textfile=${AGE_STALE}:reload=1:x=12:y=12:fontsize=34:fontcolor=white:box=1:boxcolor=red@0.85:boxborderw=8" \
    -c:v libx264 \
    -profile:v baseline \
    -level 3.1 \
    -pix_fmt yuv420p \
    -preset veryfast \
    -tune zerolatency \
    -r 5 \
    -g 10 \
    -an \
    -f rtsp \
    -rtsp_transport tcp \
    "$RTSP_URL"
