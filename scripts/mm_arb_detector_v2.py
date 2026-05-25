"""MM mispricing detector v2 — model-based, not structural.

For each cell snapshot:
1. Compute realized σ from local price history
2. Model expected touch probability using existing touch_prob.py
3. Compute "fair" mult = (1 - MM_margin) / expected_p
4. Compare actual MM mult to fair mult
5. Flag bets where MM_mult > fair_mult × deviation_threshold
   (those are UNDER-PRICED — they'd give us extra edge if we bet them)

A consistent pattern of underpriced cells at specific (relative position, time)
is exploitable. Random noise is not.

Output: stats on mispricings + flagged cell list.
"""
import json
import math
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Import touch_prob
sys.path.insert(0, str(ROOT))
from touch_prob import touch_prob

PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5
MM_MARGIN = 0.05  # assume MM takes ~5% margin


def load_snapshots_with_state(path, sample_every_n=20):
    """Reconstruct full cell state at each sampled snapshot.
    Returns: [(ts_ms, price, cells_dict), ...]"""
    cells_state = {}
    current_price = None
    snapshots = []
    n = 0
    for line in open(path):
        try: d = json.loads(line)
        except: continue
        ts = d.get("ts_ms")
        t = d.get("t")
        if t == "p":
            current_price = d["price"]
        elif t == "f":
            cells_state = {tuple(map(int, k.split("_"))): v for k, v in d["full"].items()}
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None: cells_state.pop((cx, cy), None)
                else: cells_state[(cx, cy)] = m
        n += 1
        if n % sample_every_n == 0 and current_price is not None and cells_state:
            snapshots.append((ts, current_price, dict(cells_state)))
    return snapshots


def compute_realized_sigma(price_tape, lookback_sec=300):
    """Realized σ ($/√sec) from 1-sec bucketed returns."""
    if len(price_tape) < 30: return None
    cutoff_ms = price_tape[-1][0] - lookback_sec * 1000
    recent = [p for p in price_tape if p[0] >= cutoff_ms]
    if len(recent) < 10: return None
    buckets = {}
    for ts, p in recent:
        buckets[ts // 1000] = p
    secs = sorted(buckets.keys())
    if len(secs) < 5: return None
    prices = [buckets[s] for s in secs]
    diffs = []
    for i in range(len(secs) - 1):
        dt = secs[i + 1] - secs[i]
        if dt <= 0 or dt > 3: continue
        diffs.append((prices[i + 1] - prices[i]) / math.sqrt(dt))
    if len(diffs) < 4: return None
    m = sum(diffs) / len(diffs)
    var = sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)
    return math.sqrt(var)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/cell_snapshots_20260524_224305.jsonl"
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 200  # sample every Nth snapshot
    print(f"Loading {path} (sample 1/{sample})...", flush=True)

    # First pass: build price tape
    price_tape = []
    for line in open(path):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            price_tape.append((d["ts_ms"], d["price"]))
    print(f"  price tape: {len(price_tape)} ticks", flush=True)

    snapshots = load_snapshots_with_state(path, sample_every_n=sample)
    print(f"  cell snapshots: {len(snapshots)}", flush=True)

    print("\nAnalyzing each snapshot for MM mispricings...", flush=True)
    # For each snapshot:
    #   1. Compute σ
    #   2. For each cell, compute fair mult vs actual
    deviations = []  # (ratio, snapshot_ts, cx, cy, mq, fair_mult, p_pred, band_dist)
    snap_count = 0
    import bisect
    price_ts = [t for t, _ in price_tape]

    for ts, current_price, cells in snapshots:
        snap_count += 1
        # Get σ at this point
        idx = bisect.bisect_right(price_ts, ts)
        recent_tape = price_tape[max(0, idx - 1000):idx]
        sig = compute_realized_sigma(recent_tape)
        if sig is None or sig <= 0: continue
        if current_price <= 0: continue
        now_s = ts / 1000.0
        for (cx, cy), mq in cells.items():
            if mq <= 1.1 or mq > 100: continue  # skip extreme
            t_start = cx * TIME_INTERVAL - now_s
            if t_start < -TIME_INTERVAL: continue  # cell already past
            if t_start > 100: continue  # too far future
            eff_ts = max(0.0, t_start)
            eff_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
            if eff_tau <= 0: continue
            a = cy * PRICE_INTERVAL
            b = a + PRICE_INTERVAL
            try:
                p = touch_prob(current_price, a, b, sig, eff_ts, eff_tau)
            except Exception:
                continue
            if p < 0.005 or p > 0.999: continue
            fair = (1 - MM_MARGIN) / p  # what the MM "should" pay
            ratio = mq / fair  # >1 = MM overpays, <1 = MM underpays
            band_center = (a + b) / 2
            dist = band_center - current_price
            deviations.append({
                "ratio": ratio, "ts": ts, "cx": cx, "cy": cy,
                "mq": mq, "fair": fair, "p_pred": p,
                "dist": dist, "t_start": eff_ts, "tau": eff_tau,
                "sigma": sig, "current_price": current_price,
            })
    print(f"\n  analyzed {snap_count} snapshots, {len(deviations)} cell evaluations", flush=True)

    # Summary statistics
    ratios = [d["ratio"] for d in deviations]
    ratios.sort()
    print(f"\n=== Distribution of MM_mult / fair_mult ===")
    n = len(ratios)
    print(f"  n: {n}")
    print(f"  p05: {ratios[n//20]:.3f}")
    print(f"  p25: {ratios[n//4]:.3f}")
    print(f"  p50: {ratios[n//2]:.3f}")
    print(f"  p75: {ratios[3*n//4]:.3f}")
    print(f"  p95: {ratios[19*n//20]:.3f}")
    print(f"  >1 (MM overpays): {100*sum(1 for r in ratios if r>1)/n:.1f}%")
    print(f"  >2 (MM overpays 2x): {100*sum(1 for r in ratios if r>2)/n:.1f}%")
    print(f"  <0.5 (MM underpays 50%): {100*sum(1 for r in ratios if r<0.5)/n:.1f}%")

    # Where are the biggest overpays? (best for us to bet)
    print(f"\n=== Top 10 MM overpayments (ratio = mult / fair) ===")
    top_over = sorted(deviations, key=lambda d: -d["ratio"])[:10]
    print(f"{'ratio':>7} {'mq':>7} {'fair':>7} {'p':>6} {'dist':>7} {'tau':>5} {'σ':>6}")
    for d in top_over:
        print(f"{d['ratio']:>6.2f}× {d['mq']:>6.1f}× {d['fair']:>6.1f}× {d['p_pred']:>5.3f} ${d['dist']:>+5.2f} {d['tau']:>4.1f}s {d['sigma']:>5.3f}")

    # Bucket by band_distance and t_start to find systematic mispricings
    print(f"\n=== Avg ratio by (distance, time-to-open) bucket ===")
    buckets = defaultdict(list)
    for d in deviations:
        dist_b = "near" if abs(d["dist"]) < 0.5 else ("close" if abs(d["dist"]) < 1.5 else ("mid" if abs(d["dist"]) < 3 else "far"))
        time_b = "imm" if d["t_start"] < 5 else ("soon" if d["t_start"] < 20 else "later")
        side = "up" if d["dist"] > 0 else "dn"
        buckets[f"{dist_b}-{side}-{time_b}"].append(d["ratio"])
    print(f"{'bucket':<22} {'n':>6} {'mean':>6} {'median':>7} {'p95':>6}")
    for k in sorted(buckets.keys()):
        vals = sorted(buckets[k])
        if len(vals) < 20: continue
        mean = sum(vals)/len(vals)
        med = vals[len(vals)//2]
        p95 = vals[int(len(vals)*0.95)]
        print(f"{k:<22} {len(vals):>6} {mean:>6.2f} {med:>6.2f} {p95:>6.2f}")

    print("\n=== Interpretation ===")
    print("Ratio = 1.0 means MM mult exactly equals theoretical fair mult.")
    print("Ratio > 1 means MM OVERPAYS — those are profitable BET opportunities.")
    print("Look for SYSTEMATIC overpay buckets (consistently >1.1 across many samples).")
    print("Far-OOM cells will often show ratio > 1 because touch_prob can't capture lottery upside.")


if __name__ == "__main__":
    main()
