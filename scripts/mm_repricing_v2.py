"""MM repricing v2 — fix the 'median = 1.0000' artifact.

v1's median-based analysis showed ratios of 1.0000 at every time delta, which
is the median of "many cells unchanged + a few cells changed dramatically".
Median was hiding the actual repricing behavior in the tails.

v2:
1. Use MEAN ratio (skewed by tails but more honest)
2. Count what FRACTION of cells changed
3. For changed cells, look at magnitude of change
4. Split by cell distance from current price
"""
import json
import math
import sys
import bisect
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP = sys.argv[1] if len(sys.argv) > 1 else "logs/cell_snapshots_20260524_224305.jsonl"

MOVE_THRESHOLD = 0.30
MOVE_WINDOW_SEC = 2
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5


def find_price_moves(price_tape, threshold, window_sec):
    ts_arr = [t for t, _ in price_tape]
    p_arr = [p for _, p in price_tape]
    moves = []
    for i in range(len(price_tape)):
        end_ts = ts_arr[i]
        start_ts = end_ts - window_sec * 1000
        j = bisect.bisect_left(ts_arr, start_ts)
        if j >= i: continue
        dp = p_arr[i] - p_arr[j]
        if abs(dp) >= threshold:
            moves.append((end_ts, p_arr[j], p_arr[i], 1 if dp > 0 else -1))
    if not moves: return []
    dedup = [moves[0]]
    for m in moves[1:]:
        if m[0] - dedup[-1][0] > window_sec * 1000:
            dedup.append(m)
    return dedup


def main():
    print(f"loading {SNAP}", flush=True)
    price_tape = []
    for line in open(SNAP):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            price_tape.append((d["ts_ms"], d["price"]))
    print(f"  price tape: {len(price_tape)} ticks", flush=True)

    moves = find_price_moves(price_tape, MOVE_THRESHOLD, MOVE_WINDOW_SEC)
    print(f"  moves ≥${MOVE_THRESHOLD} in {MOVE_WINDOW_SEC}s: {len(moves)}", flush=True)

    # Capture cell state at move_end and at +500ms/+1500ms/+3000ms
    print(f"\nwalking snapshots...", flush=True)
    move_dirs = {m[0]: m[3] for m in moves}
    move_prices_end = {m[0]: m[2] for m in moves}  # price AT move_end
    move_end_ts = sorted(m[0] for m in moves)

    # Per move, capture cell states at multiple time deltas
    captures = defaultdict(dict)  # move_end -> {delta_bucket: (price, cells_dict)}
    target_deltas = [0, 500, 1500, 3000]
    capture_tolerance = 100  # accept within ±100ms of target

    cells_running = {}
    cur_price = None
    n_events = 0
    for line in open(SNAP):
        try: d = json.loads(line)
        except: continue
        ts = d["ts_ms"]
        t = d.get("t")
        if t == "p":
            cur_price = d["price"]
        elif t == "f":
            cells_running = {tuple(map(int, k.split("_"))): v for k, v in d["full"].items()}
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None: cells_running.pop((cx, cy), None)
                else: cells_running[(cx, cy)] = m
        n_events += 1
        # Check if this snapshot fits any target delta for any move
        idx = bisect.bisect_left(move_end_ts, ts - 3500)
        for j in range(idx, len(move_end_ts)):
            me = move_end_ts[j]
            delta = ts - me
            if delta < -100: continue
            if delta > 3500: break
            # Match to closest target delta
            for target in target_deltas:
                if abs(delta - target) <= capture_tolerance:
                    if target not in captures[me]:
                        captures[me][target] = (cur_price, dict(cells_running))
                    break
    print(f"  processed {n_events} events; captured {sum(1 for c in captures.values() if 0 in c and 1500 in c)} usable moves", flush=True)

    # For each move, look at cells that EXIST at delta=0
    # Classify by: side (leading vs trailing relative to move direction) AND distance bucket
    # Compute mean and median mult ratio at each later delta
    print(f"\n=== Mean & median mult ratio by time delta ===")
    delta_stats = defaultdict(lambda: {
        "lead_near": [], "lead_mid": [], "lead_far": [],
        "trail_near": [], "trail_mid": [], "trail_far": [],
        "all_lead": [], "all_trail": [],
        "any_change_lead": 0, "any_change_trail": 0,
        "total_lead": 0, "total_trail": 0,
    })
    for me, caps in captures.items():
        if 0 not in caps: continue
        direction = move_dirs[me]
        price0, cells0 = caps[0]
        if price0 is None: continue
        for target_delta in target_deltas[1:]:
            if target_delta not in caps: continue
            price_later, cells_later = caps[target_delta]
            for (cx, cy), mq0 in cells0.items():
                if mq0 <= 1.0: continue
                mq_later = cells_later.get((cx, cy))
                if mq_later is None: continue
                band_center = cy * PRICE_INTERVAL + PRICE_INTERVAL / 2
                # side at MOVE_END (relative to price0)
                side = 1 if band_center > price0 else -1
                dist = abs(band_center - price0)
                # "lead" if cell is in direction of move
                is_lead = (side == direction)
                bucket = "near" if dist < 0.5 else ("mid" if dist < 1.5 else "far")
                ratio = mq_later / mq0
                key = f"{'lead' if is_lead else 'trail'}_{bucket}"
                delta_stats[target_delta][key].append(ratio)
                if is_lead:
                    delta_stats[target_delta]["all_lead"].append(ratio)
                    delta_stats[target_delta]["total_lead"] += 1
                    if abs(ratio - 1.0) > 0.01:
                        delta_stats[target_delta]["any_change_lead"] += 1
                else:
                    delta_stats[target_delta]["all_trail"].append(ratio)
                    delta_stats[target_delta]["total_trail"] += 1
                    if abs(ratio - 1.0) > 0.01:
                        delta_stats[target_delta]["any_change_trail"] += 1

    print(f"\n{'Δ ms':<8} {'lead_n':>7} {'lead_mean':>10} {'lead_med':>9} {'lead_change%':>13} "
          f"{'trail_n':>8} {'trail_mean':>11} {'trail_med':>10} {'trail_change%':>14}")
    for d in sorted(delta_stats.keys()):
        s = delta_stats[d]
        if not s["all_lead"]: continue
        lead_mean = statistics.mean(s["all_lead"])
        lead_med = statistics.median(s["all_lead"])
        trail_mean = statistics.mean(s["all_trail"]) if s["all_trail"] else 0
        trail_med = statistics.median(s["all_trail"]) if s["all_trail"] else 0
        lead_change_pct = 100 * s["any_change_lead"] / max(s["total_lead"], 1)
        trail_change_pct = 100 * s["any_change_trail"] / max(s["total_trail"], 1)
        print(f"{d:>5}ms {len(s['all_lead']):>7} {lead_mean:>9.4f} {lead_med:>8.4f} "
              f"{lead_change_pct:>11.1f}%  {len(s['all_trail']):>8} "
              f"{trail_mean:>10.4f} {trail_med:>9.4f} {trail_change_pct:>12.1f}%")

    print(f"\n=== Breakdown by distance × time delta (MEAN ratio for changed cells only) ===")
    print(f"{'bucket':<14} {'+500ms n':>10} {'mean':>7} {'+1500 n':>9} {'mean':>7} {'+3000 n':>9} {'mean':>7}")
    for bk in ["lead_near", "lead_mid", "lead_far", "trail_near", "trail_mid", "trail_far"]:
        row = [bk]
        for d in [500, 1500, 3000]:
            vals = delta_stats[d].get(bk, [])
            # Only changed cells
            changed = [v for v in vals if abs(v - 1.0) > 0.01]
            if changed:
                row.append(f"{len(changed):>10}")
                row.append(f"{statistics.mean(changed):>7.3f}")
            else:
                row.append(f"{0:>10}")
                row.append(f"{'—':>7}")
        print(f"{row[0]:<14} {row[1]} {row[2]} {row[3]} {row[4]} {row[5]} {row[6]}")

    print()
    print("=== Interpretation ===")
    print("- lead_change_% = fraction of cells in price-direction that changed mult after the move")
    print("- If lead_change% > trail_change%: MM is responding to moves by repricing leading cells")
    print("- If lead_change% ≈ trail_change%: MM repricing is uncorrelated with the move direction")
    print()
    print("- Mean ratio < 1 on changed leading cells means MM DROPPED mult on leading cells")
    print("  → MM thinks those cells became MORE likely to touch (correct)")
    print("- Mean ratio > 1 on trailing cells means MM RAISED mult (less likely to touch — correct)")


if __name__ == "__main__":
    main()
