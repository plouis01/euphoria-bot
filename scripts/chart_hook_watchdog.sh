#!/usr/bin/env bash
# Chart-hook auto-recovery watchdog.
# Polls /state every 5 min. If price=null or frameBuf=0 for 2 consecutive polls
# (10 min stale), reloads the Euphoria tab via CDP and restarts the proxy.
#
# Usage: bash scripts/chart_hook_watchdog.sh [max_runtime_seconds]
# Default max_runtime: 18000 (5h).

MAX_RUNTIME=${1:-18000}
START=$(date +%s)
STALE_COUNT=0
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG=$REPO_DIR/logs/chart_watchdog_$(date +%Y%m%d_%H%M%S).log
RECOVERIES=0

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG" ; }

log "watchdog started; max_runtime=${MAX_RUNTIME}s"

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START))
  if [ $ELAPSED -ge $MAX_RUNTIME ]; then
    log "max runtime reached, exiting (recoveries performed: $RECOVERIES)"
    exit 0
  fi

  # Poll /state
  RESP=$(curl -s --max-time 4 http://127.0.0.1:8787/state 2>/dev/null)
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
    if [ $STALE_COUNT -gt 0 ]; then
      log "chart recovered after $STALE_COUNT stale polls"
      STALE_COUNT=0
    fi
  else
    STALE_COUNT=$((STALE_COUNT + 1))
    log "chart STALE poll #$STALE_COUNT (price=$PRICE frameBuf=$FB)"
    # Two consecutive stales = 10 min stale → recover
    if [ $STALE_COUNT -ge 2 ]; then
      RECOVERIES=$((RECOVERIES + 1))
      log "INITIATING RECOVERY #$RECOVERIES"
      # 1. Get the Euphoria tab WS URL
      TAB_WS=$(curl -s http://127.0.0.1:9222/json 2>/dev/null | python3 -c "
import sys, json
try:
    tabs = json.load(sys.stdin)
    print(next(t['webSocketDebuggerUrl'] for t in tabs if 'euphoria.finance' in t.get('url','')))
except: print('')" 2>/dev/null)
      if [ -z "$TAB_WS" ]; then
        log "  RECOVERY FAILED: no Euphoria tab found in Chrome"
      else
        log "  found tab: $TAB_WS"
        # 2. Navigate to /trade + reload
        python3 -c "
import asyncio, websockets, json
async def main():
    async with websockets.connect('$TAB_WS') as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Page.navigate', 'params': {'url': 'https://euphoria.finance/trade'}}))
        try: await asyncio.wait_for(ws.recv(), 3.0)
        except: pass
asyncio.run(main())
" 2>&1 | tee -a "$LOG"
        log "  navigated to /trade, waiting 15s for page load"
        sleep 15
        # 3. Restart proxy so it re-attaches
        log "  killing old proxy"
        pkill -f signing_proxy 2>/dev/null
        sleep 3
        log "  starting fresh proxy"
        cd "$REPO_DIR"
        nohup python3 -u signing_proxy.py > logs/proxy_recover_$(date +%Y%m%d_%H%M%S).log 2>&1 &
        sleep 15
        # 4. Verify
        NEW_RESP=$(curl -s --max-time 4 http://127.0.0.1:8787/state 2>/dev/null)
        NEW_PRICE=$(echo "$NEW_RESP" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('price'))
except: print('null')" 2>/dev/null)
        if [ "$NEW_PRICE" != "null" ] && [ "$NEW_PRICE" != "None" ]; then
          log "  RECOVERY SUCCESS: price=$NEW_PRICE"
          STALE_COUNT=0
        else
          log "  RECOVERY INCOMPLETE: price still $NEW_PRICE — will retry next cycle"
        fi
      fi
    fi
  fi

  sleep 300  # 5 min between polls
done
