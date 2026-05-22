"""
dryrun_bot_v2.py — EV-driven Euphoria bot with Binance feed + realized-vol model.

Strategy:
  1. Subscribe to Binance ETH/USDT @trade WS in a background thread.
  2. Estimate σ (in $/√sec) from the last N seconds of Binance trades.
  3. For every quoted Euphoria cell within the next 30s, compute
        p_touch = touch_prob(P_binance, band, σ, time_to_window, 5s)
        ev      = mult × p_touch - 1
     Bet the highest-EV cell if its EV > --ev-threshold.

Safeguards:
  - If |Binance - Euphoria| > $1 → panic, no bets (someone's feed is broken).
  - --max-taps, --max-loss caps.
  - /tmp/euphoria_bot_kill file stops the bot.

Dry-run by default — outcomes are validated against the Binance tape (much
finer-grained than the proxy's /state poll, so much fewer missed touches).
Pass --live to actually trade.
"""
import argparse
import json
import os
import signal
import statistics
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass

from binance_feed import BinanceFeed
from touch_prob import touch_prob


@dataclass
class EuTick:
    ts_ms: int
    price: float


def realized_vol_from_eu_tape(tape: "deque[EuTick]", window_ms: int) -> float | None:
    cutoff = int(time.time() * 1000) - window_ms
    ticks = [t for t in tape if t.ts_ms >= cutoff]
    if len(ticks) < 5:
        return None
    buckets: dict[int, float] = {}
    for t in ticks:
        buckets[t.ts_ms // 1000] = t.price
    secs = sorted(buckets.keys())
    if len(secs) < 5:
        return None
    prices = [buckets[s] for s in secs]
    diffs = []
    for i in range(len(secs) - 1):
        dt = secs[i + 1] - secs[i]
        if dt <= 0 or dt > 3:
            continue
        diffs.append((prices[i + 1] - prices[i]) / (dt ** 0.5))
    if len(diffs) < 4:
        return None
    return statistics.stdev(diffs)

PROXY = os.environ.get("PROXY", "http://127.0.0.1:8787")
PRICE_INTERVAL = 0.5  # $ per cellY band
TIME_INTERVAL = 5     # sec per cellX window
KILL_FILE = "/tmp/euphoria_bot_kill"


def http_get(path: str):
    with urllib.request.urlopen(PROXY + path, timeout=3) as r:
        return json.loads(r.read())


def http_post(path: str, body: dict):
    req = urllib.request.Request(
        PROXY + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


@dataclass
class PendingBet:
    cellX: int
    cellY: int
    mult: float
    stake: float
    window_start_ms: int
    window_end_ms: int
    p_pred: float
    ev_pred: float
    sigma: float


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--slippage", type=int, default=60)
    ap.add_argument("--max-taps", type=int, default=20)
    ap.add_argument("--max-loss", type=float, default=5.0)
    ap.add_argument("--cadence-sec", type=float, default=15.0)
    ap.add_argument("--ev-threshold", type=float, default=0.05,
                    help="bet only if EV > this fraction of stake (0.05 = 5%%)")
    ap.add_argument("--vol-window-sec", type=float, default=60.0)
    ap.add_argument("--max-drift", type=float, default=5.0,
                    help="pause if |Binance - Euphoria| > $X (raised — drift IS the edge)")
    ap.add_argument("--max-euphoria-price-age-s", type=float, default=5.0,
                    help="skip if Euphoria priceTs older than this (feed broken)")
    ap.add_argument("--max-time-out-sec", type=float, default=30.0,
                    help="only score cells within this many seconds")
    ap.add_argument("--min-mult", type=float, default=1.1,
                    help="ignore cells under this mult (likely junk quotes)")
    ap.add_argument("--max-mult", type=float, default=1000.0,
                    help="ignore cells above this mult (model overestimates OOM tails)")
    ap.add_argument("--min-p", type=float, default=0.0,
                    help="require predicted touch prob >= this (model only calibrated near ATM)")
    ap.add_argument("--max-p", type=float, default=1.01,
                    help="reject cells above this p_pred — model over-confident on near-certain cells")
    ap.add_argument("--signal-source", choices=["binance", "euphoria"],
                    default="binance",
                    help="which price feed drives signal generation + vol")
    args = ap.parse_args()

    binance = BinanceFeed()
    binance.start()
    print(f"[bot] warming up Binance feed for 5s...")
    time.sleep(5)
    if not binance.connected:
        print(f"[bot] FATAL: Binance WS not connected: {binance.last_error}")
        sys.exit(1)
    print(f"[bot] Binance connected; tape size {binance.msg_count} after 5s")

    state0 = http_get("/state")
    bal0 = state0.get("balance")
    if bal0 is None:
        chart0 = http_get("/chart")
        bal0 = chart0.get("balance")
    if bal0 is None:
        print("[bot] FATAL: balance missing from /state and /chart — reload Euphoria page.")
        binance.stop()
        sys.exit(1)
    starting_balance = float(bal0)

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"[bot] {mode} starting=${starting_balance:.4f} "
          f"ev_thr={args.ev_threshold} stake=${args.stake} "
          f"signal_source={args.signal_source}")

    last_tap_ts = 0.0
    taps = 0
    wins = 0
    losses = 0
    wins_binance = 0   # what we'd have called wins using the (biased) Binance tape
    losses_binance = 0
    pending: list[PendingBet] = []
    drift_panic = False
    cum_ev_predicted = 0.0
    eu_tape: deque[EuTick] = deque(maxlen=3000)  # ~15min at 300ms

    stop = {"flag": False}
    def on_sig(*_):
        stop["flag"] = True
        print("\n[bot] stop signal received")
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        while not stop["flag"]:
            if os.path.exists(KILL_FILE):
                print(f"[bot] kill file present, stopping")
                break

            # ----- Resolve pending dry-run bets against EUPHORIA tape (ground truth) -----
            # We also compute Binance-tape outcome for comparison/debugging.
            now_ms = int(time.time() * 1000)
            still_pending = []
            for b in pending:
                if now_ms < b.window_end_ms + 2500:
                    still_pending.append(b)
                    continue
                band_lo = b.cellY * PRICE_INTERVAL
                band_hi = band_lo + PRICE_INTERVAL
                eu_ticks = [t for t in eu_tape
                            if b.window_start_ms <= t.ts_ms <= b.window_end_ms]
                bn_ticks = [t for t in binance.window(120_000)
                            if b.window_start_ms <= t.ts_ms <= b.window_end_ms]
                touched_eu = any(band_lo <= t.price < band_hi for t in eu_ticks)
                touched_bn = any(band_lo <= t.price < band_hi for t in bn_ticks)
                outcome = "WIN" if touched_eu else "LOSS"
                pnl = (b.mult - 1) * b.stake if touched_eu else -b.stake
                disagree = "" if touched_eu == touched_bn else "  ⚠ EU/BN DISAGREE"
                print(f"[bot] resolve cell=({b.cellX},{b.cellY}) "
                      f"band=[{band_lo:.2f},{band_hi:.2f}) "
                      f"eu_ticks={len(eu_ticks)} bn_ticks={len(bn_ticks)} "
                      f"{outcome} (bn_says={'WIN' if touched_bn else 'LOSS'}) "
                      f"p_pred={b.p_pred:.3f} ev_pred={b.ev_pred:+.3f} "
                      f"σ_at_bet={b.sigma:.3f} pnl=${pnl:+.4f}{disagree}")
                if touched_eu:
                    wins += 1
                else:
                    losses += 1
                if touched_bn:
                    wins_binance += 1
                else:
                    losses_binance += 1
            pending = still_pending

            # ----- Read proxy state -----
            try:
                st = http_get("/state")
                chart = http_get("/chart")
                mults = http_get("/multipliers")
            except Exception as e:
                print(f"[bot] proxy error: {e}")
                time.sleep(1.0)
                continue

            # Maintain Euphoria price tape (dedup by priceTs)
            if st.get("price") is not None and st.get("priceTs") is not None:
                ts = int(st["priceTs"])
                if not eu_tape or eu_tape[-1].ts_ms != ts:
                    eu_tape.append(EuTick(ts, float(st["price"])))

            cur_bal = float(st["balance"]) if st["balance"] else starting_balance
            realized_pnl = cur_bal - starting_balance
            if -realized_pnl >= args.max_loss:
                print(f"[bot] hit max-loss ${args.max_loss}, stopping")
                break
            if taps >= args.max_taps:
                print(f"[bot] hit max-taps {args.max_taps}, stopping")
                break

            now = time.time()
            if now - last_tap_ts < args.cadence_sec:
                time.sleep(0.3)
                continue

            # ----- Binance freshness check -----
            b_latest = binance.latest()
            if not b_latest:
                time.sleep(0.5)
                continue
            b_age_ms = int(time.time() * 1000) - b_latest.ts_ms
            if b_age_ms > 5000 and args.signal_source == "binance":
                print(f"[bot] Binance feed stale ({b_age_ms}ms); skipping")
                time.sleep(0.5)
                continue
            e_price = st.get("price")
            e_priceTs = st.get("priceTs")
            if e_price is None or e_priceTs is None:
                time.sleep(0.5)
                continue
            e_age_ms = int(time.time() * 1000) - int(e_priceTs)
            if e_age_ms > args.max_euphoria_price_age_s * 1000:
                print(f"[bot] Euphoria price stale ({e_age_ms}ms); skipping")
                time.sleep(0.5)
                continue
            drift_signed = b_latest.price - e_price   # >0 = Binance higher
            drift = abs(drift_signed)
            if args.signal_source == "binance" and drift > args.max_drift:
                if not drift_panic:
                    print(f"[bot] PAUSE: drift=${drift_signed:+.2f} "
                          f"(BN ${b_latest.price:.2f} EU ${e_price:.2f} "
                          f"e_age={e_age_ms}ms) — large drift, skipping")
                    drift_panic = True
                time.sleep(2.0)
                continue
            drift_panic = False

            # ----- Choose signal source: price & realized vol -----
            if args.signal_source == "binance":
                P_now = b_latest.price
                sigma = binance.realized_vol_per_sec(int(args.vol_window_sec * 1000))
            else:  # euphoria
                P_now = e_price
                sigma = realized_vol_from_eu_tape(
                    eu_tape, int(args.vol_window_sec * 1000))
            if sigma is None or sigma <= 0:
                time.sleep(0.5)
                continue

            # ----- Score every in-window cell -----
            now_s = time.time()
            best = None
            scored = 0
            for c in mults["cells"]:
                cx, cy, m = c["cellX"], c["cellY"], c["mult"]
                if m < args.min_mult or m > args.max_mult:
                    continue
                t_start = cx * TIME_INTERVAL - now_s
                if t_start < -TIME_INTERVAL:  # window already closed
                    continue
                if t_start > args.max_time_out_sec:
                    continue
                effective_t_start = max(0.0, t_start)
                effective_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
                if effective_tau <= 0:
                    continue
                a = cy * PRICE_INTERVAL
                b = a + PRICE_INTERVAL
                p = touch_prob(P_now, a, b, sigma, effective_t_start, effective_tau)
                if p < args.min_p or p > args.max_p:
                    continue
                ev = m * p - 1
                scored += 1
                if best is None or ev > best["ev"]:
                    best = {"cx": cx, "cy": cy, "mult": m, "p": p, "ev": ev}

            if not best:
                time.sleep(0.5)
                continue

            if best["ev"] < args.ev_threshold:
                # Periodic log so we know it's alive
                if scored > 0 and int(now) % 5 == 0:
                    print(f"[bot] no-edge scored={scored} best_ev={best['ev']:+.3f} "
                          f"cell=({best['cx']},{best['cy']}) "
                          f"mult={best['mult']} p={best['p']:.3f} "
                          f"σ={sigma:.3f} P=${P_now:.2f}")
                time.sleep(0.5)
                continue

            cx, cy, mult, p, ev = (best["cx"], best["cy"], best["mult"],
                                   best["p"], best["ev"])
            print(f"[bot] SIGNAL cell=({cx},{cy}) mult={mult} p={p:.3f} "
                  f"ev={ev:+.3f} σ={sigma:.3f}/s P_bn=${P_now:.2f} "
                  f"P_eu=${e_price:.2f} drift=${drift_signed:+.2f} "
                  f"e_age={e_age_ms}ms (scored {scored})")
            cum_ev_predicted += ev * args.stake

            if not args.live:
                window_start_ms = cx * TIME_INTERVAL * 1000
                window_end_ms = window_start_ms + TIME_INTERVAL * 1000
                pending.append(PendingBet(
                    cellX=cx, cellY=cy, mult=mult, stake=args.stake,
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                    p_pred=p, ev_pred=ev, sigma=sigma,
                ))
                eta = (window_end_ms - int(time.time() * 1000)) / 1000
                print(f"[bot] DRY-RUN — would tap ${args.stake} resolves in ~{eta:.1f}s")
                last_tap_ts = now
                taps += 1
                continue

            bal_before = cur_bal
            try:
                resp = http_post("/tap", {"cellX": cx, "cellY": cy,
                                          "amount": args.stake,
                                          "slippage": args.slippage})
                print(f"[bot] tap response: {json.dumps(resp)[:200]}")
            except Exception as e:
                print(f"[bot] /tap error: {e}")
                last_tap_ts = now
                continue
            last_tap_ts = now
            taps += 1
            time.sleep(8)
            try:
                st2 = http_get("/state")
                bal_after = float(st2["balance"]) if st2["balance"] else bal_before
            except Exception:
                bal_after = bal_before
            delta = bal_after - bal_before
            if delta > 0:
                wins += 1
            else:
                losses += 1
            print(f"[bot] tap #{taps}: delta=${delta:+.4f} "
                  f"balance=${bal_after:.4f} "
                  f"cum=${(bal_after - starting_balance):+.4f} "
                  f"w/l={wins}/{losses}")

    finally:
        binance.stop()
        try:
            st_f = http_get("/state")
            bal_f = float(st_f["balance"]) if st_f["balance"] else starting_balance
        except Exception:
            bal_f = starting_balance
        if pending:
            print(f"[bot] {len(pending)} pending bets unresolved at exit")
        print(f"\n[bot] done. starting=${starting_balance:.4f} "
              f"final=${bal_f:.4f} pnl=${(bal_f - starting_balance):+.4f}")
        print(f"[bot] taps={taps} EU_wins={wins} EU_losses={losses}  "
              f"BN_wins={wins_binance} BN_losses={losses_binance}")
        if taps > 0:
            wr_eu = wins / max(wins + losses, 1) * 100
            wr_bn = wins_binance / max(wins_binance + losses_binance, 1) * 100
            print(f"[bot] EU win rate: {wr_eu:.1f}%   BN win rate: {wr_bn:.1f}%   "
                  f"predicted cum EV: ${cum_ev_predicted:+.4f}")
            print(f"[bot] Euphoria tape size: {len(eu_tape)}")


if __name__ == "__main__":
    main()
