#!/usr/bin/env bash
# Live-bot watchdog: kill the bot if Euphoria frontend lags/drops, or after timeout.
# Args: $1 = bot PID, $2 = max runtime seconds (default 36000=10h)
# Stops the bot when:
#   - chart hook drops (price=null) for >60 consecutive seconds
#   - proxy unreachable for >60 consecutive seconds
#   - frameBuf stalls at 0 for >60 consecutive seconds
#   - max runtime reached

BOT_PID=${1:?bot pid required}
MAX_RUNTIME=${2:-36000}
PROXY=http://127.0.0.1:8787
START=$(date +%s)
STALE_SINCE=0   # 0 = currently healthy; else timestamp when staleness began
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG=$REPO_DIR/logs/live_watchdog_$(date +%Y%m%d_%H%M%S).log

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" ; }

log "watchdog started: bot_pid=$BOT_PID max_runtime=${MAX_RUNTIME}s"

while true; do
  # Bot still alive?
  if ! ps -p $BOT_PID > /dev/null 2>&1; then
    log "bot pid $BOT_PID is gone — exiting watchdog"
    exit 0
  fi

  # Runtime cap?
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  if [ $ELAPSED -ge $MAX_RUNTIME ]; then
    log "max runtime ${MAX_RUNTIME}s reached — sending SIGTERM to bot $BOT_PID"
    kill -TERM $BOT_PID 2>/dev/null
    sleep 5
    kill -KILL $BOT_PID 2>/dev/null
    exit 0
  fi

  # Frontend health: price not null AND frameBuf > 0
  RESP=$(curl -s --max-time 4 $PROXY/state 2>/dev/null)
  HEALTHY=0
  if [ -n "$RESP" ]; then
    PRICE=$(echo "$RESP" | python3 -c "import sys,json
try: d=json.load(sys.stdin); print(d.get('price'))
except: print('null')" 2>/dev/null)
    FB=$(echo "$RESP" | python3 -c "import sys,json
try: d=json.load(sys.stdin); print(d.get('frameBuf', 0))
except: print(0)" 2>/dev/null)
    if [ "$PRICE" != "null" ] && [ "$PRICE" != "None" ] && [ "$FB" != "0" ]; then
      HEALTHY=1
    fi
  fi

  if [ $HEALTHY -eq 1 ]; then
    if [ $STALE_SINCE -ne 0 ]; then
      log "frontend recovered after $((NOW - STALE_SINCE))s of staleness"
      STALE_SINCE=0
    fi
  else
    if [ $STALE_SINCE -eq 0 ]; then
      STALE_SINCE=$NOW
      log "frontend stale detected (price=$PRICE frameBuf=$FB) — starting 60s grace timer"
    else
      STALE_FOR=$((NOW - STALE_SINCE))
      if [ $STALE_FOR -ge 60 ]; then
        log "frontend stale for ${STALE_FOR}s — KILLING bot $BOT_PID"
        kill -TERM $BOT_PID 2>/dev/null
        sleep 5
        kill -KILL $BOT_PID 2>/dev/null
        exit 1
      fi
    fi
  fi

  sleep 10
done
