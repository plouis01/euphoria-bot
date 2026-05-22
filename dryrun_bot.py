"""
dryrun_bot.py — Edge-A tap bot for Euphoria, driving signing_proxy.py.

Edge A (momentum): poll the proxy's /state price feed at ~300ms; when ETH
moves > $X/sec over the last N seconds, bet on a cell whose price band is
one tick above (if rising) or below (if falling) the current band — i.e. the
band most likely to be touched in the next 5s window.

This is not a real edge against a sharp MM (they see the same price). It's
a plumbing test that exercises the full poll → signal → /multipliers → /tap
loop. To get actual edge, swap the price source for a faster external feed
(Binance WS) — see momentum() docstring for the swap point.

Defaults to DRY-RUN (logs decisions, no taps). Pass --live to trade real.

Safety:
  - --max-taps caps total trades per session.
  - --max-loss stops when cumulative PnL drops by this much.
  - Touching /tmp/euphoria_bot_kill stops the bot at the next loop tick.
  - Ctrl-C / SIGTERM stops cleanly.
"""
import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass

PROXY = os.environ.get("PROXY", "http://127.0.0.1:8787")
PRICE_INTERVAL = 0.5  # $ per cellY band
TIME_INTERVAL = 5     # sec per cellX window
KILL_FILE = "/tmp/euphoria_bot_kill"


def get(path: str):
    with urllib.request.urlopen(PROXY + path, timeout=3) as r:
        return json.loads(r.read())


def post(path: str, body: dict):
    req = urllib.request.Request(
        PROXY + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


@dataclass
class Tick:
    ts_ms: int
    price: float


def momentum(tape: deque, window_ms: int):
    """$/sec slope over the most-recent window_ms of price ticks.

    SWAP POINT for real edge: replace the tape with a Binance WS trade
    tape (wss://stream.binance.com/ws/ethusdt@trade). Their feed is ~50ms
    fresher than Euphoria's quote bus; that's where any latency edge lives.
    """
    if len(tape) < 2:
        return None
    now = tape[-1]
    target = now.ts_ms - window_ms
    old = next((t for t in tape if t.ts_ms >= target), None)
    if old is None or old.ts_ms == now.ts_ms:
        return None
    dt_s = (now.ts_ms - old.ts_ms) / 1000.0
    if dt_s < 1.0:
        return None
    return (now.price - old.price) / dt_s


def cell_for_side(price: float, cell_x: int, side: str, offset_bands: int):
    cy_current = int(math.floor(price / PRICE_INTERVAL))
    if side == "up":
        return (cell_x, cy_current + offset_bands)
    return (cell_x, cy_current - offset_bands)


def find_mult(multipliers: dict, cx: int, cy: int):
    for c in multipliers["cells"]:
        if c["cellX"] == cx and c["cellY"] == cy:
            return c["mult"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually place taps")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--slippage", type=int, default=60)
    ap.add_argument("--max-taps", type=int, default=20)
    ap.add_argument("--max-loss", type=float, default=5.0)
    ap.add_argument("--cadence-sec", type=float, default=30.0)
    ap.add_argument("--poll-ms", type=int, default=300)
    ap.add_argument("--momentum-window-sec", type=float, default=10.0)
    ap.add_argument("--momentum-threshold", type=float, default=0.05,
                    help="$/sec — momentum magnitude that triggers a tap")
    ap.add_argument("--min-mult", type=float, default=1.4)
    ap.add_argument("--max-mult", type=float, default=3.0,
                    help="reject cells above this mult — they're too OOM")
    args = ap.parse_args()

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"[bot] {mode} stake=${args.stake} max_taps={args.max_taps} "
          f"max_loss=${args.max_loss}")
    print(f"[bot] cadence={args.cadence_sec}s poll={args.poll_ms}ms "
          f"window={args.momentum_window_sec}s threshold=${args.momentum_threshold}/s "
          f"min_mult={args.min_mult}")

    state0 = get("/state")
    if state0["balance"] is None:
        print("[bot] FATAL: balance missing from /state. Reload Euphoria in debug Chrome.")
        sys.exit(1)
    starting_balance = float(state0["balance"])
    print(f"[bot] starting balance: ${starting_balance:.4f}")

    tape: deque = deque(maxlen=600)  # ~3min at 300ms
    last_tap_ts = 0.0
    taps = 0
    wins = 0
    losses = 0
    # Dry-run pending bets: list of dicts with cell, window, side, mult.
    # When a bet's window closes, scan tape for any tick in band; that's a would-win.
    pending_dry: list = []

    stop = {"flag": False}
    def on_sig(*_):
        stop["flag"] = True
        print("\n[bot] stop signal received")
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        while not stop["flag"]:
            if os.path.exists(KILL_FILE):
                print(f"[bot] kill file {KILL_FILE} present, stopping")
                break

            try:
                st = get("/state")
            except Exception as e:
                print(f"[bot] /state error: {e}")
                time.sleep(1.0)
                continue

            if st["price"] is not None and st["priceTs"] is not None:
                if not tape or tape[-1].ts_ms != st["priceTs"]:
                    tape.append(Tick(st["priceTs"], st["price"]))

            # Resolve dry-run bets whose windows have closed.
            now_ms = int(time.time() * 1000)
            still_pending = []
            for b in pending_dry:
                if now_ms < b["window_end_ms"] + 1500:
                    still_pending.append(b)
                    continue
                band_lo = b["cellY"] * PRICE_INTERVAL
                band_hi = band_lo + PRICE_INTERVAL
                touched = any(
                    band_lo <= t.price < band_hi
                    for t in tape
                    if b["window_start_ms"] <= t.ts_ms <= b["window_end_ms"]
                )
                samples = sum(1 for t in tape
                              if b["window_start_ms"] <= t.ts_ms <= b["window_end_ms"])
                outcome = "WIN" if touched else "LOSS"
                pnl = (b["mult"] - 1) * b["stake"] if touched else -b["stake"]
                print(f"[bot] dry-resolve cell=({b['cellX']},{b['cellY']}) "
                      f"band=[{band_lo:.2f},{band_hi:.2f}) "
                      f"samples={samples} {outcome} pnl=${pnl:+.4f}")
                if touched:
                    wins += 1
                else:
                    losses += 1
            pending_dry = still_pending

            cur_bal = float(st["balance"]) if st["balance"] else starting_balance
            pnl = cur_bal - starting_balance
            if -pnl >= args.max_loss:
                print(f"[bot] hit max loss ${args.max_loss}: pnl=${pnl:+.4f}, stopping")
                break
            if taps >= args.max_taps:
                print(f"[bot] hit max taps {args.max_taps}, stopping")
                break

            now = time.time()
            if now - last_tap_ts < args.cadence_sec:
                time.sleep(args.poll_ms / 1000.0)
                continue

            m = momentum(tape, window_ms=int(args.momentum_window_sec * 1000))
            if m is None or abs(m) < args.momentum_threshold:
                time.sleep(args.poll_ms / 1000.0)
                continue

            side = "up" if m > 0 else "down"

            try:
                mults = get("/multipliers")
                chart = get("/chart")
            except Exception as e:
                print(f"[bot] /multipliers or /chart error: {e}")
                time.sleep(1.0)
                continue

            cx_start = chart.get("grid", {}).get("startingIndex", {}).get("timeIndex")
            if cx_start is None:
                time.sleep(args.poll_ms / 1000.0)
                continue

            # Try the closest cells first (cx_off=0, cy_off=1), and only
            # accept mults in [min_mult, max_mult] — anything above max_mult
            # is a far-OOM lottery ticket, not Edge-A territory.
            picked = None
            for cx_off in (0, 1, 2):
                for cy_off in (1, 2):
                    cx, cy = cell_for_side(st["price"], cx_start + cx_off, side, cy_off)
                    mult = find_mult(mults, cx, cy)
                    if mult is None:
                        continue
                    if mult < args.min_mult or mult > args.max_mult:
                        continue
                    picked = (cx, cy, mult)
                    break
                if picked:
                    break

            if not picked:
                print(f"[bot] no cell found side={side} mom=${m:+.4f}/s; skipping")
                last_tap_ts = now
                continue

            cx, cy, mult = picked
            print(f"[bot] signal side={side} mom=${m:+.4f}/s cell=({cx},{cy}) mult={mult}")

            if not args.live:
                window_start_ms = cx * TIME_INTERVAL * 1000
                window_end_ms = window_start_ms + TIME_INTERVAL * 1000
                pending_dry.append({
                    "cellX": cx, "cellY": cy, "mult": mult,
                    "stake": args.stake, "side": side,
                    "window_start_ms": window_start_ms,
                    "window_end_ms": window_end_ms,
                })
                eta_s = (window_end_ms - int(time.time() * 1000)) / 1000.0
                print(f"[bot] DRY-RUN — would tap ${args.stake} "
                      f"window=[{window_start_ms},{window_end_ms}] resolves in ~{eta_s:.1f}s")
                last_tap_ts = now
                taps += 1
                continue

            bal_before = cur_bal
            try:
                resp = post("/tap", {"cellX": cx, "cellY": cy,
                                     "amount": args.stake, "slippage": args.slippage})
                print(f"[bot] tap response: {json.dumps(resp)[:200]}")
            except Exception as e:
                print(f"[bot] /tap error: {e}")
                last_tap_ts = now
                continue

            last_tap_ts = now
            taps += 1

            time.sleep(8.0)
            try:
                st_after = get("/state")
                bal_after = float(st_after["balance"]) if st_after["balance"] else bal_before
            except Exception:
                bal_after = bal_before

            delta = bal_after - bal_before
            if delta > 0:
                wins += 1
            else:
                losses += 1
            print(f"[bot] tap #{taps}: pnl_this=${delta:+.4f} "
                  f"balance=${bal_after:.4f} "
                  f"cumulative=${(bal_after - starting_balance):+.4f} "
                  f"w/l={wins}/{losses}")

    finally:
        try:
            st_final = get("/state")
            bal_final = float(st_final["balance"]) if st_final["balance"] else starting_balance
        except Exception:
            bal_final = starting_balance
        if pending_dry:
            print(f"[bot] {len(pending_dry)} dry-run bets unresolved at exit "
                  f"(windows didn't close in time)")
        print(f"\n[bot] done. starting=${starting_balance:.4f} "
              f"final=${bal_final:.4f} pnl=${(bal_final - starting_balance):+.4f}")
        print(f"[bot] taps={taps} wins={wins} losses={losses}")
        if taps > 0:
            wr = wins / max(wins + losses, 1) * 100
            print(f"[bot] win rate: {wr:.1f}%  (need ~{100/1.5:.0f}% at 1.5x mult to break even)")


if __name__ == "__main__":
    main()
