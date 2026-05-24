#!/usr/bin/env bash
# Launch LIVE F4k4_v3 — same as v2 but with Option B network-tightening:
#   --max-quote-age-ms 400 (was 700)
#   --max-euphoria-price-age-s 2 (was 5)
#
# Per LIVE_F4k4_v2 H4 analysis (2026-05-24): network degradation caused
# ~$3-4/hour of extra slippage when feeds went sluggish. Tighter thresholds
# skip more bets during bad-network windows, accepting lower throughput in
# exchange for higher fill quality.
#
# Usage:
#   bash scripts/launch_live_v3.sh
#   # Edit MAX_LOSS / MAX_GAIN below before running if balance has changed.

cd "$(dirname "$0")/.."
TS=$(date +%Y%m%d_%H%M%S)
LOG=logs/LIVE_F4k4_v3_${TS}.log

# Safety stops — adjust for current balance
START_BAL=$(curl -s http://127.0.0.1:8787/chart | python3 -c "import sys,json;print(json.load(sys.stdin)['balance'])")
echo "current balance: \$$START_BAL"
# floor $1 → max_loss = balance - 1
MAX_LOSS=$(python3 -c "print(round($START_BAL - 1, 2))")
# ceiling $200 → max_gain = 200 - balance
MAX_GAIN=$(python3 -c "print(round(200 - $START_BAL, 2))")
echo "max_loss: \$$MAX_LOSS  max_gain: \$$MAX_GAIN"

rm -f /tmp/euphoria_bot_kill

nohup python3 -u dryrun_bot_v3.py --live \
  --strategy c30-strong --signal-source coinbase \
  --c-thr-strong 0.25 --c-tau-strong-sec 30 --vol-window-sec 30 \
  --calibrator2 \
  --min-mult 3 --max-mult 8 \
  --max-abs-drift 0.40 \
  --max-cell-distance 1.0 \
  --min-quote-age-ms 1 \
  --bandit-window 6 --bandit-floor 0.10 --bandit-pause-sec 60 \
  --max-time-out-sec 50 --cadence-sec 8 \
  --max-quote-age-ms 400 \
  --max-euphoria-price-age-s 2 \
  --stake 1 --max-loss "$MAX_LOSS" --max-gain "$MAX_GAIN" --max-taps 99999 \
  --tag LIVE_F4k4_v3 \
  > $LOG 2>&1 &
BOT_PID=$!
echo "$BOT_PID $TS" > /tmp/live_f4k4_v3_pid
echo "BOT PID=$BOT_PID  log=$LOG"

# 10h watchdog
nohup bash scripts/live_watchdog.sh $BOT_PID 36000 > /dev/null 2>&1 &
echo "watchdog PID=$!"

sleep 12
echo
echo "=== first lines ==="
head -10 $LOG
