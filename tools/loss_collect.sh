#!/bin/bash
# loss_collect.sh - READ-ONLY. Run on Alpha and on Bravo at about the same time
# while SNIPR is running. It only reads logs and prints settings; it changes nothing.
# Usage:  bash loss_collect.sh [MINUTES]      (default 10)
# Output: ~/loss_<hostname>_<date>.txt  - send that file back (one per node).
M="${1:-10}"
H="$(hostname)"
OUT="$HOME/loss_${H}_$(date +%Y%m%d_%H%M).txt"
SINCE="$M min ago"
UNIT=meshtasticd
{
  echo "=== meta ==="
  echo "host=$H collected=$(date '+%F %T.%N') window=last ${M} min"
  uptime
  command -v vcgencmd >/dev/null 2>&1 && { echo "throttled=$(vcgencmd get_throttled)"; vcgencmd measure_temp; vcgencmd measure_clock arm; }
  echo "ntp: $(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
  echo "=== services ==="
  for s in meshtasticd cot-bridge mediamtx snipr-republish; do echo "$s: $(systemctl is-active $s 2>/dev/null)"; done
  echo "=== radio settings (read only) ==="
  if command -v meshtastic >/dev/null 2>&1; then
    timeout 25 meshtastic --host localhost --get device.role --get device.rebroadcast_mode --get lora.hop_limit --get lora.tx_power --get lora.modem_preset --get lora.region 2>&1 | grep -v "^Connected\|^Requesting\|^Retrieved" | head -30
  else
    echo "meshtastic CLI not found - skipped"
  fi
  echo "=== kernel power and throttle events in the window ==="
  sudo journalctl -k --since "$SINCE" --no-pager -o short-precise 2>/dev/null | grep -i "voltage\|throttl" | head -200
  echo "=== SNIPR bridge lines ==="
  sudo journalctl -u cot-bridge --since "$SINCE" --no-pager -o short-precise 2>/dev/null | grep -i "SNIPR" | cut -c1-230 | head -400
  echo "=== meshtasticd radio lines ==="
  sudo journalctl -u $UNIT --since "$SINCE" --no-pager -o short-precise 2>/dev/null \
    | grep -E "Started Tx|Completed sending|enqueue for send|Lora RX|Delivering rx packet|Setting tx delay|Can not send yet|packets remain in the TX queue|decoding failed|rxBad|Rx someone rebroadcasting|cancelSending|Packet RX|Packet TX|Corrected frequency offset| WARN | ERROR " \
    | cut -c1-300
} > "$OUT" 2>&1
echo "wrote $OUT ($(wc -l < "$OUT") lines, $(du -h "$OUT" | cut -f1))"
echo "Nothing was changed. Please attach that file."
