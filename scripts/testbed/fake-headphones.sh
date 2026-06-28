#!/usr/bin/env bash
set -euo pipefail

# Act as an A2DP sink (headphones). Make this box discoverable/pairable as an
# audio sink, then record the arriving Bluetooth stream to a wav so the host
# reverse-leg output can be captured and compared.
# Usage: fake-headphones.sh DURATION_S [WAV_PATH]

DURATION="${1:?duration seconds required}"
WAV="${2:-/tmp/trib-testbed/headphones-capture.wav}"
RATE=48000

echo "fake-headphones: discoverable sink, record ${DURATION}s -> $WAV"
mkdir -p "$(dirname "$WAV")"

bluetoothctl --timeout 5 power on || true
bluetoothctl --timeout 5 agent NoInputNoOutput || true
bluetoothctl --timeout 5 default-agent || true
bluetoothctl --timeout 5 pairable on || true
bluetoothctl --timeout 5 discoverable on || true

SRC=""
for _ in $(seq 1 40); do
  SRC="$(pactl list short sources 2>/dev/null | awk '/bluez_input/{print $2; exit}')" || true
  [ -n "$SRC" ] && break
  sleep 0.5
done

if [ -n "$SRC" ]; then
  echo "fake-headphones: recording source $SRC"
  pw-record --target "$SRC" --rate "$RATE" --channels 2 --format s16 "$WAV" &
else
  echo "fake-headphones: no bluez_input source yet; capturing default source"
  pw-record --rate "$RATE" --channels 2 --format s16 "$WAV" &
fi
REC=$!

sleep "$DURATION"
kill -INT "$REC" 2>/dev/null || true
wait "$REC" 2>/dev/null || true

bluetoothctl --timeout 5 discoverable off || true
echo "fake-headphones: wrote $WAV"
