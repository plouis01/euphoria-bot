"""Compare Run #2 (winner, +$45) vs Run #3 (loser, -$3.43) regimes.

Extract SIGNAL→settled pairs from each log, then compare:
  - σ distribution
  - multiplier distribution
  - predicted p distribution
  - |drift_cb| distribution
  - WR by σ bucket / mult bucket / p bucket
  - underlying volatility (from settlements timeline)

Usage: python scripts/regime_compare.py
"""
import json
import re
from collections import defaultdict
from statistics import mean, median, pstdev

RUN2_LOG = "logs/LIVE_c30r-vw30_v2_20260522_185250.log"
RUN3_LOG = "logs/LIVE_c30r-vw30_v3_20260522_193642.log"

SIGNAL_RE = re.compile(
    r"SIGNAL.*cell=\((\d+),(\d+)\)\s+mult=([\d.]+)\s+p=([\d.]+)\s+ev=\+?(-?[\d.]+)\s+σ=([\d.]+)\s+P_eu=\$([\d.]+)\s+P_cb=\$([\d.]+)\s+drift_cb=\$(-?[\d.+]+)"
)
SETTLE_RE = re.compile(
    r"LIVE settled.*cell=\((\d+),(\d+)\)\s+mult=([\d.]+)\s+outcome=(\w+)\s+pnl=\$(-?[\d.]+)"
)


def parse_run(path):
    """Return list of dicts: one per signal that resolved to a settled bet."""
    signals = []  # all signals fired (the ones that became taps)
    settles = {}  # by (cellX, cellY) → last settle
    with open(path) as f:
        for line in f:
            m = SIGNAL_RE.search(line)
            if m:
                signals.append({
                    "cellX": int(m.group(1)),
                    "cellY": int(m.group(2)),
                    "mult_signed": float(m.group(3)),
                    "p_pred": float(m.group(4)),
                    "ev": float(m.group(5)),
                    "sigma": float(m.group(6)),
                    "P_eu": float(m.group(7)),
                    "P_cb": float(m.group(8)),
                    "drift": float(m.group(9)),
                })
                continue
            m = SETTLE_RE.search(line)
            if m:
                key = (int(m.group(1)), int(m.group(2)))
                settles[key] = {
                    "mult_filled": float(m.group(3)),
                    "outcome": m.group(4),
                    "pnl": float(m.group(5)),
                }
    # Match: walk signals in order, attach settle if cell matches
    out = []
    seen_settle = set()
    for s in signals:
        key = (s["cellX"], s["cellY"])
        if key in settles and key not in seen_settle:
            # Note: this is order-naïve — if same cell appears twice, we pair
            # signal→settle on first match. For the c30r-vw30 strategy each
            # bet uses a fresh cellX (timestamp-based), so this is fine.
            out.append({**s, **settles[key], "won": settles[key]["outcome"] == "won"})
            seen_settle.add(key)
    return out


def summary(label, bets):
    if not bets:
        print(f"{label}: NO DATA")
        return
    n = len(bets)
    w = sum(1 for b in bets if b["won"])
    pnl = sum(b["pnl"] for b in bets)
    pred_ev = sum(b["ev"] for b in bets)
    print(f"\n=== {label}  (n={n}, W={w}, WR={100*w/n:.1f}%, realized=${pnl:+.2f}, pred_cum_EV=${pred_ev:+.2f}) ===")

    def stats(field, fmt=".3f"):
        vals = [b[field] for b in bets]
        s = sorted(vals)
        q = lambda p: s[min(int(p * len(s)), len(s)-1)]
        return (f"mean={mean(vals):{fmt}} med={median(vals):{fmt}} "
                f"std={pstdev(vals):{fmt}} p10={q(0.10):{fmt}} p90={q(0.90):{fmt}} "
                f"min={min(vals):{fmt}} max={max(vals):{fmt}}")

    print(f"  σ            : {stats('sigma')}")
    print(f"  mult_signed  : {stats('mult_signed', '.2f')}")
    print(f"  mult_filled  : {stats('mult_filled', '.2f')}")
    print(f"  p_pred       : {stats('p_pred')}")
    print(f"  |drift|      : {stats('drift', '.3f')}  (absolute below)")
    abs_drift = [abs(b["drift"]) for b in bets]
    s = sorted(abs_drift)
    q = lambda p: s[min(int(p * len(s)), len(s)-1)]
    print(f"     abs       : mean={mean(abs_drift):.3f} med={median(abs_drift):.3f} "
          f"p90={q(0.90):.3f} max={max(abs_drift):.3f}")

    # Slippage: (mult_filled - mult_signed) / mult_signed
    slips = [(b["mult_filled"] - b["mult_signed"]) / b["mult_signed"] for b in bets]
    print(f"  slip %       : mean={100*mean(slips):+.1f}% med={100*median(slips):+.1f}% "
          f"min={100*min(slips):+.1f}% max={100*max(slips):+.1f}%")


def bucket_wr(label, bets, field, buckets, fmt=".2f"):
    print(f"\n  WR by {field} bucket — {label}:")
    print(f"    {'range':<18} {'n':>4} {'W':>3} {'WR':>7} {'pnl':>8} {'avg_mult':>9}")
    for lo, hi in buckets:
        bs = [b for b in bets if lo <= b[field] < hi]
        if not bs:
            continue
        w = sum(1 for b in bs if b["won"])
        pnl = sum(b["pnl"] for b in bs)
        am = mean([b["mult_signed"] for b in bs])
        print(f"    [{lo:{fmt}}, {hi:{fmt}}) {len(bs):>4} {w:>3} "
              f"{100*w/len(bs):>6.1f}% ${pnl:>+7.2f} {am:>9.2f}")


def main():
    run2 = parse_run(RUN2_LOG)
    run3 = parse_run(RUN3_LOG)

    summary("RUN #2 (winner)", run2)
    summary("RUN #3 (loser)", run3)

    print("\n" + "=" * 64)
    print("REGIME DIFFS")
    print("=" * 64)
    if run2 and run3:
        for f in ("sigma", "mult_signed", "p_pred"):
            d = mean([b[f] for b in run3]) - mean([b[f] for b in run2])
            print(f"  Δ mean {f:<15} = {d:+.4f}  (run3 - run2)")
        d_drift = mean([abs(b["drift"]) for b in run3]) - mean([abs(b["drift"]) for b in run2])
        print(f"  Δ mean |drift|        = {d_drift:+.4f}  (run3 - run2)")

    # WR by σ bucket
    sigma_buckets = [(0, 0.06), (0.06, 0.10), (0.10, 0.15), (0.15, 0.25)]
    bucket_wr("RUN #2", run2, "sigma", sigma_buckets, ".2f")
    bucket_wr("RUN #3", run3, "sigma", sigma_buckets, ".2f")

    mult_buckets = [(1, 4), (4, 7), (7, 10), (10, 15), (15, 30)]
    bucket_wr("RUN #2", run2, "mult_signed", mult_buckets, ".0f")
    bucket_wr("RUN #3", run3, "mult_signed", mult_buckets, ".0f")

    p_buckets = [(0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.7), (0.7, 1.0)]
    bucket_wr("RUN #2", run2, "p_pred", p_buckets, ".1f")
    bucket_wr("RUN #3", run3, "p_pred", p_buckets, ".1f")

    # Underlying volatility per minute (approx from |P_eu - P_cb| trail and σ field)
    print("\n  Underlying drift summary (proxy for vol regime):")
    for label, bets in [("RUN #2", run2), ("RUN #3", run3)]:
        if not bets: continue
        prices = [b["P_eu"] for b in bets]
        print(f"    {label}: P_eu range ${min(prices):.2f} → ${max(prices):.2f} "
              f"span=${max(prices) - min(prices):.2f}  n={len(bets)}")

    # Predicted-vs-actual calibration
    print("\n  Calibration (predicted p × n  vs  actual W):")
    for label, bets in [("RUN #2", run2), ("RUN #3", run3)]:
        if not bets: continue
        exp_w = sum(b["p_pred"] for b in bets)
        actual_w = sum(1 for b in bets if b["won"])
        print(f"    {label}: predicted ~{exp_w:.1f} wins, actual {actual_w} "
              f"(delta {actual_w - exp_w:+.1f})")


if __name__ == "__main__":
    main()
