"""
External balance watchdog.

Runs as its own process. Polls the signing_proxy's /chart every POLL_INTERVAL_S
to read the live USDm balance. On any of:

  - balance <= MIN_BALANCE_USD
  - balance dropped by >= MAX_DROP_USD within MAX_DROP_WINDOW_S
  - balance is None / unreadable for >= STALE_TOLERANCE_S

writes /tmp/euphoria_bot_kill (the existing KILL_FILE honored by
dryrun_bot_v3.py:323-325) and appends a reason line to logs/watchdog.log.

Idempotent: once tripped, the kill file stays until manually removed (`rm
/tmp/euphoria_bot_kill`). The watchdog itself continues polling so subsequent
events are still logged.

CLI:
  python scripts/balance_watchdog.py
    [--min-balance 25] [--max-drop 15] [--max-drop-window 600]
    [--stale-tolerance 30] [--poll 5] [--proxy http://127.0.0.1:8787]

Read-only with respect to the proxy. Cost of a false positive: one kill file
to manually remove; bots restart cleanly.
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.request


KILL_FILE = "/tmp/euphoria_bot_kill"
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs")
LOG_PATH = os.path.join(LOG_DIR, "watchdog.log")


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[watchdog] log write failed: {e}", flush=True)


def _trip(reason: str) -> None:
    if os.path.exists(KILL_FILE):
        _log(f"KILL already tripped; new reason: {reason}")
        return
    try:
        with open(KILL_FILE, "w") as f:
            f.write(reason + "\n")
        _log(f"KILL FILE WRITTEN: {reason}")
    except Exception as e:
        _log(f"FAILED to write kill file: {e} (reason was: {reason})")


def _read_balance(proxy: str, timeout_s: float = 3.0):
    """Try /chart first (richer), fall back to /state. Returns float or None."""
    for path in ("/chart", "/state"):
        try:
            with urllib.request.urlopen(proxy + path, timeout=timeout_s) as r:
                data = json.loads(r.read())
            bal = data.get("balance")
            if bal is None:
                continue
            return float(bal)
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-balance", type=float, default=25.0,
                    help="trip if balance falls to/below this (USD)")
    ap.add_argument("--max-drop", type=float, default=15.0,
                    help="trip if balance drops by >= this within window")
    ap.add_argument("--max-drop-window", type=float, default=600.0,
                    help="window for max-drop check (seconds)")
    ap.add_argument("--stale-tolerance", type=float, default=30.0,
                    help="trip if balance is None for >= this long (seconds)")
    ap.add_argument("--poll", type=float, default=5.0,
                    help="poll interval (seconds)")
    ap.add_argument("--proxy", type=str,
                    default=os.environ.get("PROXY", "http://127.0.0.1:8787"))
    args = ap.parse_args()

    _log(f"watchdog start: min_balance=${args.min_balance} "
         f"max_drop=${args.max_drop}/{args.max_drop_window:.0f}s "
         f"stale_tolerance={args.stale_tolerance:.0f}s "
         f"poll={args.poll:.1f}s proxy={args.proxy}")

    # rolling (ts, balance) for max-drop check
    history: collections.deque = collections.deque()
    first_stale_ts: float | None = None

    while True:
        now = time.time()
        bal = _read_balance(args.proxy)

        if bal is None:
            if first_stale_ts is None:
                first_stale_ts = now
            elapsed = now - first_stale_ts
            if elapsed >= args.stale_tolerance:
                _trip(f"balance unreadable for {elapsed:.0f}s (>= "
                      f"{args.stale_tolerance:.0f}s tolerance)")
            time.sleep(args.poll)
            continue

        # got a reading; clear stale-counter
        first_stale_ts = None
        history.append((now, bal))
        # drop entries older than the drop window (with small buffer)
        cutoff = now - args.max_drop_window - 5.0
        while history and history[0][0] < cutoff:
            history.popleft()

        # Threshold check
        if bal <= args.min_balance:
            _trip(f"balance ${bal:.4f} <= min_balance ${args.min_balance:.2f}")

        # Max-drop check: compare current to the maximum within window
        window_max = max(b for _, b in history)
        drop = window_max - bal
        if drop >= args.max_drop:
            _trip(f"balance dropped ${drop:.4f} within {args.max_drop_window:.0f}s "
                  f"(peak ${window_max:.4f} -> ${bal:.4f}, threshold ${args.max_drop:.2f})")

        time.sleep(args.poll)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("watchdog stopped (SIGINT)")
        sys.exit(0)
