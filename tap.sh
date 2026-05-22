#!/bin/bash
# Pick the cheapest cell in [min_mult, max_mult] range and tap it.
# Usage: ./tap.sh [amount=1] [min_mult=1.4] [max_mult=2.0] [slippage=60]

AMOUNT="${1:-1}"
MIN="${2:-1.4}"
MAX="${3:-2.0}"
SLIP="${4:-60}"
PROXY="${PROXY:-http://127.0.0.1:8787}"

CELL=$(curl -s "$PROXY/multipliers" \
  | jq -c --argjson lo "$MIN" --argjson hi "$MAX" \
        '[.cells[] | select(.mult >= $lo and .mult <= $hi)] | sort_by(.mult) | .[0]')

if [ -z "$CELL" ] || [ "$CELL" = "null" ]; then
  echo "No cells in [$MIN, $MAX] range."
  exit 1
fi

X=$(echo "$CELL" | jq .cellX)
Y=$(echo "$CELL" | jq .cellY)
M=$(echo "$CELL" | jq .mult)

echo "Tapping cellX=$X cellY=$Y mult=$M amount=\$$AMOUNT"
BEFORE=$(curl -s "$PROXY/state" | jq -r .balance)
echo "Balance before: $BEFORE"

curl -s -X POST "$PROXY/tap" \
  -H 'content-type: application/json' \
  -d "{\"cellX\": $X, \"cellY\": $Y, \"amount\": $AMOUNT, \"slippage\": $SLIP}" \
  | jq

sleep 8
AFTER=$(curl -s "$PROXY/state" | jq -r .balance)
echo "Balance after:  $AFTER"
python3 -c "print(f'PnL: \${(float(\"$AFTER\") - float(\"$BEFORE\")):.4f}')"
