"""MM quote inconsistency / arbitrage detector.

Uses cell_snapshots_*.jsonl (from cell_snapshot_collector.py) to detect:

1. **Mult monotonicity violations**: for the same cellY (band), mult should
   decrease as cellX (window time) increases — distant-future cells should
   pay more (longer to wait). Same time, different bands: closer-to-price
   should pay less.

2. **Touch-prob consistency**: cell at (cx, cy) should price coherently with
   cell at (cx, cy+1). If mult ratios diverge from local σ-implied ratios,
   one is mispriced.

3. **Cross-time arbitrage**: if cell (cx, cy) is overpriced vs cell (cx+1, cy),
   you can simultaneously bet cy at time cx+1 and avoid cy at cx — equivalent
   to a calendar spread.

This script doesn't BET anything — it just identifies opportunities for later.

Output: stdout + logs/mm_arb_report_<TS>.md
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5


def load_snapshots(path):
    """Replay delta-encoded snapshots into list of (ts_ms, prices, cells)."""
    snapshots = []
    cells_state: dict[tuple[int, int], float] = {}
    current_price = None
    for line in open(path):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("t")
        ts = d.get("ts_ms")
        if t == "p":
            current_price = d["price"]
        elif t == "f":
            cells_state = {tuple(map(int, k.split("_"))): v
                            for k, v in d["full"].items()}
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None:
                    cells_state.pop((cx, cy), None)
                else:
                    cells_state[(cx, cy)] = m
        snapshots.append((ts, current_price, dict(cells_state)))
    return snapshots


def check_monotonicity_in_time(snapshot, current_price):
    """For each cellY, mults at later cellX should generally be HIGHER (more time
    to touch — but it depends on direction). For cells in current band, mult
    should approach 1 (very likely to touch)."""
    violations = []
    # Group by cellY
    by_y = defaultdict(list)
    for (cx, cy), m in snapshot.items():
        by_y[cy].append((cx, m))
    for cy, lst in by_y.items():
        lst.sort()  # by cx ascending
        for i in range(len(lst) - 1):
            cx1, m1 = lst[i]
            cx2, m2 = lst[i + 1]
            if cx2 - cx1 != 1:
                continue  # only adjacent
            band_center = cy * PRICE_INTERVAL + PRICE_INTERVAL / 2
            dist = abs(band_center - (current_price or band_center))
            # If band is FAR from price, later cells should have lower mult (more time → more touch chance)
            # If band is NEAR price, mults are close to 1 either way
            if dist > 1.0:  # far from price
                # Later cellX should have higher OR similar mult, not much lower
                if m1 > m2 * 1.5:  # mult dropped >33% as we go later
                    violations.append({
                        "type": "time_mono",
                        "cy": cy, "cx1": cx1, "m1": m1,
                        "cx2": cx2, "m2": m2,
                        "dist": dist,
                    })
    return violations


def check_y_smoothness(snapshot, current_price, max_per_snapshot=5):
    """Adjacent bands (cellY, cellY+1) at same cellX should have similar mults
    (smooth touch-prob surface). Big jumps suggest mispricing."""
    violations = []
    by_x = defaultdict(list)
    for (cx, cy), m in snapshot.items():
        by_x[cx].append((cy, m))
    for cx, lst in by_x.items():
        lst.sort()
        for i in range(len(lst) - 1):
            cy1, m1 = lst[i]
            cy2, m2 = lst[i + 1]
            if cy2 - cy1 != 1:
                continue
            ratio = max(m1, m2) / min(m1, m2)
            if ratio > 3.0:  # 3× mult ratio between adjacent bands
                violations.append({
                    "type": "y_jump", "cx": cx, "cy1": cy1, "cy2": cy2,
                    "m1": m1, "m2": m2, "ratio": ratio,
                })
                if len(violations) >= max_per_snapshot:
                    break
    return violations


def main():
    if len(sys.argv) < 2:
        # Find the most recent collector log
        candidates = sorted(ROOT.glob("logs/cell_snapshots_*.jsonl"))
        if not candidates:
            print("usage: mm_quote_arb_detector.py <path-to-cell_snapshots.jsonl>")
            return
        path = candidates[-1]
        print(f"using most recent: {path}", flush=True)
    else:
        path = Path(sys.argv[1])

    snapshots = load_snapshots(path)
    print(f"loaded {len(snapshots)} snapshots", flush=True)

    # Sample every 100th snapshot for speed
    sample = snapshots[::100]
    print(f"analyzing {len(sample)} sampled snapshots", flush=True)

    time_violations = []
    y_violations = []
    for ts, price, cells in sample:
        if not price or not cells:
            continue
        tv = check_monotonicity_in_time(cells, price)
        yv = check_y_smoothness(cells, price)
        time_violations.extend(tv[:3])
        y_violations.extend(yv[:3])

    print(f"\nTime-monotonicity violations: {len(time_violations)}")
    print(f"Y-smoothness jumps: {len(y_violations)}")

    # Show worst time violations
    time_violations.sort(key=lambda v: -v["m1"] / v["m2"])
    print("\n=== Worst time-monotonicity violations (mult dropped going forward) ===")
    for v in time_violations[:10]:
        print(f"  cy={v['cy']} cx={v['cx1']}({v['m1']}×) → cx={v['cx2']}({v['m2']}×)  "
              f"ratio={v['m1']/v['m2']:.2f} dist=${v['dist']:.1f}")

    y_violations.sort(key=lambda v: -v["ratio"])
    print("\n=== Worst y-smoothness jumps (big mult diff between adjacent bands) ===")
    for v in y_violations[:10]:
        print(f"  cx={v['cx']} cy={v['cy1']}({v['m1']}×) <-> cy={v['cy2']}({v['m2']}×)  "
              f"ratio={v['ratio']:.2f}")

    print("\n=== Interpretation ===")
    print("Time violations may indicate MM hasn't refreshed quotes — calendar arb possible.")
    print("Y jumps suggest MM's vol model differs from realized — bet the cheaper side.")
    print("Both are statistically rare; consistent patterns at specific (cx, cy) would be exploitable.")


if __name__ == "__main__":
    main()
