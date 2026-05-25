"""Cell snapshot → ML touch-probability model.

For each cell that existed in our 10h dataset:
  1. Extract features: σ, distance from price, t_start, mq, drift, price recent direction
  2. Determine actual outcome: did the band [a, b] get touched during its 5s window?

Train a model (logistic regression baseline) and compare predictions against:
  - touch_prob.py's Gaussian model (baseline)
  - MM's implied prob (1/mq)
  - Actual realized WR

If ML beats Gaussian by >5pp in calibration, we have an improved touch-prob.
"""
import json
import math
import sys
import bisect
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from touch_prob import touch_prob

PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5


def load_data(path, sample=100):
    """Load price tape + reconstruct cell state at each sampled snapshot.
    For each cell that existed, determine its outcome by replaying price tape.
    Returns: feature rows."""
    print(f"loading {path}", flush=True)
    price_tape = []
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
            price_tape.append((ts, d["price"]))
        elif t == "f":
            cells_state = {tuple(map(int, k.split("_"))): v for k, v in d["full"].items()}
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None: cells_state.pop((cx, cy), None)
                else: cells_state[(cx, cy)] = m
        n += 1
        if n % sample == 0 and current_price is not None and cells_state:
            snapshots.append((ts, current_price, dict(cells_state)))
    print(f"  price tape: {len(price_tape)} ticks, snapshots: {len(snapshots)}", flush=True)
    return price_tape, snapshots


def realized_sigma(price_tape, end_ms, lookback_sec=300):
    ts_arr = [t for t, _ in price_tape]
    idx = bisect.bisect_right(ts_arr, end_ms)
    cutoff = end_ms - lookback_sec * 1000
    start_idx = bisect.bisect_left(ts_arr, cutoff)
    recent = price_tape[start_idx:idx]
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


def recent_drift(price_tape, end_ms, lookback_sec=300):
    """Net price change over lookback period (in $)."""
    ts_arr = [t for t, _ in price_tape]
    idx = bisect.bisect_right(ts_arr, end_ms)
    cutoff = end_ms - lookback_sec * 1000
    start_idx = bisect.bisect_left(ts_arr, cutoff)
    if idx - start_idx < 5: return None
    return price_tape[idx-1][1] - price_tape[start_idx][1]


def touched_in_window(price_tape, window_start_ms, window_end_ms, band_lo, band_hi):
    """Did any price tick in the window fall within [band_lo, band_hi]?"""
    ts_arr = [t for t, _ in price_tape]
    i0 = bisect.bisect_left(ts_arr, window_start_ms)
    i1 = bisect.bisect_right(ts_arr, window_end_ms)
    if i1 - i0 < 2: return None  # not enough ticks
    ticks = price_tape[i0:i1]
    for _, p in ticks:
        if band_lo <= p < band_hi:
            return True
    # Segment crossing
    for i in range(len(ticks) - 1):
        p0 = ticks[i][1]; p1 = ticks[i + 1][1]
        lo, hi = (p0, p1) if p0 <= p1 else (p1, p0)
        if lo <= band_hi and hi >= band_lo:
            return True
    return False


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/cell_snapshots_20260524_224305.jsonl"
    sample = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    max_cells_per_snapshot = 50  # randomly sample to bound n

    price_tape, snapshots = load_data(path, sample)

    print(f"\nbuilding feature rows...", flush=True)
    import random
    random.seed(42)
    rows = []
    skipped_no_outcome = 0
    for ts, current_price, cells in snapshots:
        sig = realized_sigma(price_tape, ts)
        drift = recent_drift(price_tape, ts)
        if sig is None or drift is None: continue
        keys = list(cells.keys())
        random.shuffle(keys)
        for cx, cy in keys[:max_cells_per_snapshot]:
            mq = cells[(cx, cy)]
            if mq <= 1.1 or mq > 50: continue
            now_s = ts / 1000.0
            t_start = cx * TIME_INTERVAL - now_s
            if t_start < -TIME_INTERVAL: continue
            if t_start > 60: continue
            eff_ts = max(0.0, t_start)
            eff_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
            if eff_tau <= 0: continue
            a = cy * PRICE_INTERVAL
            b = a + PRICE_INTERVAL
            band_center = (a + b) / 2
            dist = band_center - current_price
            # Compute Gaussian baseline
            try:
                p_gauss = touch_prob(current_price, a, b, sig, eff_ts, eff_tau)
            except Exception:
                continue
            # Look up actual outcome
            window_start_ms = int((cx * TIME_INTERVAL) * 1000)
            window_end_ms = window_start_ms + TIME_INTERVAL * 1000
            outcome = touched_in_window(price_tape, window_start_ms, window_end_ms, a, b)
            if outcome is None:
                skipped_no_outcome += 1
                continue
            rows.append({
                "won": int(outcome),
                "mq": mq, "p_gauss": p_gauss, "p_mm": 1 / mq,
                "sig": sig, "drift": drift,
                "dist": dist, "abs_dist": abs(dist),
                "t_start": eff_ts, "tau": eff_tau,
                "dist_norm": dist / (sig * math.sqrt(eff_tau + 1)),
            })
    print(f"  feature rows: {len(rows)}, skipped (no future tape): {skipped_no_outcome}", flush=True)

    # Calibration analysis: Gaussian vs MM vs actual WR
    print(f"\n=== Calibration: predicted prob vs actual WR ===")
    print(f"{'bin':<14} {'n':>5} {'gauss_avg':>10} {'mm_avg':>8} {'actual':>8} {'gauss_gap':>10} {'mm_gap':>8}")
    for lo, hi in [(0,0.05), (0.05,0.10), (0.10,0.20), (0.20,0.30), (0.30,0.50), (0.50,1.0)]:
        sub = [r for r in rows if lo <= r["p_gauss"] < hi]
        if not sub: continue
        n = len(sub)
        gauss_avg = sum(r["p_gauss"] for r in sub)/n
        mm_avg = sum(r["p_mm"] for r in sub)/n
        actual = sum(r["won"] for r in sub)/n
        print(f"[{lo:.2f},{hi:.2f})    {n:>5} {gauss_avg:>9.3f} {mm_avg:>7.3f} {actual:>7.3f} "
              f"{gauss_avg-actual:>+9.3f} {mm_avg-actual:>+7.3f}")

    print(f"\n=== Calibration by distance bucket ===")
    print(f"{'dist bucket':<15} {'n':>6} {'gauss_avg':>10} {'mm_avg':>8} {'actual':>8} {'gauss_err':>10} {'mm_err':>8}")
    for lo, hi in [(0,0.5), (0.5,1.0), (1.0,2.0), (2.0,5.0), (5.0,99)]:
        sub = [r for r in rows if lo <= r["abs_dist"] < hi]
        if not sub: continue
        n = len(sub)
        gauss_avg = sum(r["p_gauss"] for r in sub)/n
        mm_avg = sum(r["p_mm"] for r in sub)/n
        actual = sum(r["won"] for r in sub)/n
        print(f"dist∈[{lo:.1f},{hi:.1f})   {n:>6} {gauss_avg:>9.3f} {mm_avg:>7.3f} {actual:>7.3f} "
              f"{gauss_avg-actual:>+9.3f} {mm_avg-actual:>+7.3f}")

    # Simple logistic regression — using stdlib only
    # Features: log(mq), abs_dist, sig, t_start, tau, abs(drift), dist_norm
    print(f"\n=== Logistic regression (sklearn) ===")
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import log_loss, brier_score_loss
        X = np.array([[math.log(r["mq"]), r["abs_dist"], r["sig"], r["t_start"],
                       r["tau"], abs(r["drift"]), r["dist_norm"]] for r in rows])
        y = np.array([r["won"] for r in rows])
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=42)
        clf = LogisticRegression(max_iter=1000).fit(X_train, y_train)
        p_test = clf.predict_proba(X_test)[:, 1]
        # Compare to gaussian baseline on same test set
        idx = np.arange(len(rows))
        _, test_idx = train_test_split(idx, test_size=0.3, random_state=42)
        rows_test = [rows[i] for i in test_idx]
        gauss_test = [r["p_gauss"] for r in rows_test]
        mm_test = [r["p_mm"] for r in rows_test]
        ll_lr = log_loss(y_test, p_test)
        # Clip Gaussian predictions to avoid log(0)
        gauss_clipped = [max(1e-6, min(1-1e-6, g)) for g in gauss_test]
        ll_gauss = log_loss(y_test, gauss_clipped)
        mm_clipped = [max(1e-6, min(1-1e-6, m)) for m in mm_test]
        ll_mm = log_loss(y_test, mm_clipped)
        print(f"  log loss: LR={ll_lr:.4f}  Gaussian={ll_gauss:.4f}  MM_implied={ll_mm:.4f}")
        brier_lr = brier_score_loss(y_test, p_test)
        brier_gauss = brier_score_loss(y_test, gauss_clipped)
        brier_mm = brier_score_loss(y_test, mm_clipped)
        print(f"  Brier: LR={brier_lr:.4f}  Gaussian={brier_gauss:.4f}  MM={brier_mm:.4f}")
        # Feature importances (LR coefs)
        feat_names = ["log_mq", "abs_dist", "sig", "t_start", "tau", "abs_drift", "dist_norm"]
        coefs = clf.coef_[0]
        print(f"  feature coefficients:")
        for name, c in sorted(zip(feat_names, coefs), key=lambda x: -abs(x[1])):
            print(f"    {name:<10} {c:+.3f}")
    except ImportError:
        print("  sklearn not available — skipping LR")


if __name__ == "__main__":
    main()
