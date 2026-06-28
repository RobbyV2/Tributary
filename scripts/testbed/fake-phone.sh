#!/usr/bin/env bash
set -euo pipefail

# Act as an A2DP source (a phone). Pair/connect to the host sink, then stream a
# known steady tone into the host over Bluetooth.
# Usage: fake-phone.sh HOST_MAC [FREQ_HZ] [DURATION_S] [WAV_PATH]

HOST_MAC="${1:?host MAC required}"
FREQ="${2:-440}"
DURATION="${3:-20}"
WAV="${4:-/tmp/trib-testbed/phone-tone.wav}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RATE=48000

echo "fake-phone: target host $HOST_MAC tone ${FREQ}Hz dur ${DURATION}s"

mkdir -p "$(dirname "$WAV")"
python3 "$HERE/inject-tone.py" "$WAV" --duration "$DURATION" --rate "$RATE" --tone "$FREQ"

echo "fake-phone: bluetoothctl pair/trust/connect"
bluetoothctl --timeout 5 power on || true
bluetoothctl --timeout 5 agent NoInputNoOutput || true
bluetoothctl --timeout 20 pair "$HOST_MAC" || echo "fake-phone: pair returned nonzero (may already be paired)"
bluetoothctl --timeout 5 trust "$HOST_MAC" || true
bluetoothctl --timeout 20 connect "$HOST_MAC" || echo "fake-phone: connect returned nonzero (may already be connected)"

SINK=""
for _ in $(seq 1 20); do
  SINK="$(pactl list short sinks 2>/dev/null | awk '/bluez_output/{print $2; exit}')" || true
  [ -n "$SINK" ] && break
  sleep 0.5
done
[ -n "$SINK" ] || { echo "fake-phone: no bluez_output sink appeared" >&2; exit 1; }
echo "fake-phone: streaming to sink $SINK"

pw-cat -p --target "$SINK" --rate "$RATE" --channels 2 --format s16 "$WAV"
echo "fake-phone: done"
