"""MM repricing latency — find cells that stayed stale after a price move.

Hypothesis: when EU price moves significantly, the MM updates its quotes after
some delay. During that delay, cells in the direction of the move are
"misquoted" — bets on those cells should win at higher rates than the MM mult
implies.

Method:
1. Find significant price moves: ΔP ≥ $0.30 within 2 seconds
2. At t = move_end, snapshot all cell mults (M_pre)
3. At t = move_end + Δ (for Δ = 100, 300, 500, 1000 ms), snapshot mults again (M_post)
4. Compute mult_change_per_cell = M_post / M_pre
5. The "lag" is how slow each cell updated. Cells that stayed close to M_pre
   for longer have larger lag = more exploitable.

Cross-reference with USD-only oracle to see if Coinbase/Kraken WS leads the
move, giving us a "signal" to act on.
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
ORACLE = sys.argv[2] if len(sys.argv) > 2 else "logs/multi_oracle_20260524_224305.jsonl"

MOVE_THRESHOLD = 0.30  # min price change to call a "move"
MOVE_WINDOW_SEC = 2   # over this many seconds
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5


def load_snapshots_full(path):
    """Yield (ts_ms, current_price, dict of (cx,cy)->mult) at every event."""
    cells_state = {}
    current_price = None
    last_p_ts = 0
    for line in open(path):
        try: d = json.loads(line)
        except: continue
        ts = d["ts_ms"]
        t = d.get("t")
        if t == "p":
            current_price = d["price"]
            last_p_ts = ts
            yield ts, current_price, cells_state
        elif t == "f":
            cells_state = {tuple(map(int, k.split("_"))): v for k, v in d["full"].items()}
            yield ts, current_price, cells_state
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None: cells_state.pop((cx, cy), None)
                else: cells_state[(cx, cy)] = m
            yield ts, current_price, cells_state


def find_price_moves(price_tape, threshold, window_sec):
    """Returns list of (move_end_ts_ms, p_start, p_end, dir) where price moved by
    >= threshold within window_sec ending at move_end_ts."""
    ts_arr = [t for t, _ in price_tape]
    p_arr = [p for _, p in price_tape]
    moves = []
    for i in range(len(price_tape)):
        end_ts = ts_arr[i]
        # Find tick that's window_sec earlier
        start_ts = end_ts - window_sec * 1000
        j = bisect.bisect_left(ts_arr, start_ts)
        if j >= i: continue
        dp = p_arr[i] - p_arr[j]
        if abs(dp) >= threshold:
            moves.append((end_ts, p_arr[j], p_arr[i], 1 if dp > 0 else -1))
    # Deduplicate overlapping moves (keep first end_ts in each cluster)
    if not moves: return []
    dedup = [moves[0]]
    for m in moves[1:]:
        if m[0] - dedup[-1][0] > window_sec * 1000:
            dedup.append(m)
    return dedup


def main():
    print(f"loading snapshots {SNAP}", flush=True)
    # Build price tape first
    price_tape = []
    for line in open(SNAP):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            price_tape.append((d["ts_ms"], d["price"]))
    print(f"  price tape: {len(price_tape)} ticks", flush=True)

    moves = find_price_moves(price_tape, MOVE_THRESHOLD, MOVE_WINDOW_SEC)
    print(f"  significant moves (|ΔP|≥${MOVE_THRESHOLD} in {MOVE_WINDOW_SEC}s): {len(moves)}", flush=True)
    if not moves:
        print("no significant moves found")
        return

    # For each move, walk through subsequent snapshots and track how mults change
    # We need snapshot stream around each move's end_ts.
    print(f"\nanalyzing MM repricing response to {len(moves)} moves...", flush=True)

    # Build a more efficient index: pass through file, capture state at each tick
    # First: snapshot mults at each price tick (need (ts, price, cells_dict))
    # The generator yields too much — collect snapshots only at move_end_ts ± window
    move_end_set = {m[0] for m in moves}
    # We'll capture snapshots within ±5 sec of each move_end
    captures = defaultdict(list)  # move_end_ts -> list of (delta_ms, cells_dict)
    move_dirs = {m[0]: m[3] for m in moves}
    move_prices = {m[0]: (m[1], m[2]) for m in moves}

    move_end_arr = sorted(moves, key=lambda x: x[0])
    move_end_ts_arr = [m[0] for m in move_end_arr]

    print(f"  walking snapshots...", flush=True)
    cells_state_running = {}
    current_price_running = None
    last_progress = 0
    n_events = 0
    for line in open(SNAP):
        try: d = json.loads(line)
        except: continue
        ts = d["ts_ms"]
        t = d.get("t")
        if t == "p":
            current_price_running = d["price"]
        elif t == "f":
            cells_state_running = {tuple(map(int, k.split("_"))): v for k, v in d["full"].items()}
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                if m is None: cells_state_running.pop((cx, cy), None)
                else: cells_state_running[(cx, cy)] = m
        else:
            continue
        n_events += 1
        # For any move whose window we're in (-100 to +3000ms), capture
        idx = bisect.bisect_left(move_end_ts_arr, ts - 3000)
        for j in range(idx, len(move_end_ts_arr)):
            me = move_end_ts_arr[j]
            delta = ts - me
            if delta < -100: continue
            if delta > 3000: break
            # Only capture every ~50ms relative to move_end
            if not captures[me] or ts - captures[me][-1][0] >= 50:
                captures[me].append((ts, dict(cells_state_running), current_price_running))
        if n_events - last_progress > 50000:
            last_progress = n_events
            print(f"    {n_events} events processed, {len(captures)} moves with snapshots", flush=True)

    # For each move, compute how cells in the move direction repriced over time
    print(f"\n  analyzing {len(captures)} captured move windows...", flush=True)

    # For each move, classify cells by their POSITION RELATIVE TO MOVE DIRECTION:
    #   "trailing": cells in the direction the price came FROM (now less likely to touch)
    #   "leading":  cells in the direction the price is moving TO (now more likely)
    # We track the mean mult for each group at successive time deltas.

    # Bucket times relative to move end
    time_buckets = [0, 100, 300, 500, 1000, 2000, 3000]

    trailing_changes = defaultdict(list)  # delta_bucket -> list of mult ratios
    leading_changes = defaultdict(list)

    for me, caps in captures.items():
        if len(caps) < 2: continue
        direction = move_dirs[me]
        p_start, p_end = move_prices[me]
        # Snapshot 0 = closest to move_end
        ts0, cells0, price0 = caps[0]
        if price0 is None: continue
        # For each cell at ts0, record mult and side relative to current price
        cell_baseline = {}
        for (cx, cy), mq in cells0.items():
            band_center = cy * PRICE_INTERVAL + PRICE_INTERVAL / 2
            side = 1 if band_center > price0 else -1
            cell_baseline[(cx, cy)] = (mq, side, band_center)
        # For each later snapshot, compute mult ratio for cells in each direction
        for ts_later, cells_later, _ in caps[1:]:
            delta_ms = ts_later - me
            # Find nearest bucket
            for cell, (mq0, side, band_center) in cell_baseline.items():
                mq_later = cells_later.get(cell)
                if mq_later is None or mq0 <= 1: continue
                ratio = mq_later / mq0
                # "leading" if cell is in the direction of the move
                # direction +1 = price moved UP, so cells above (side=+1) are now more likely
                if side == direction:
                    leading_changes[delta_ms].append(ratio)
                else:
                    trailing_changes[delta_ms].append(ratio)

    print(f"\n=== Mult ratio (later / at move_end) by time-after-move ===")
    print(f"{'Δ ms':<8} {'leading_n':>10} {'leading_med':>12} {'trailing_n':>11} {'trailing_med':>13}")
    bucket_centers = []
    leading_seq = []
    trailing_seq = []
    for cap_ts in sorted(set(list(leading_changes.keys()) + list(trailing_changes.keys()))):
        l = leading_changes.get(cap_ts, [])
        t = trailing_changes.get(cap_ts, [])
        if len(l) < 50 and len(t) < 50: continue
        l_med = statistics.median(l) if l else None
        t_med = statistics.median(t) if t else None
        l_str = f"{l_med:>11.4f}" if l_med else "          -"
        t_str = f"{t_med:>12.4f}" if t_med else "           -"
        print(f"{cap_ts:>6}ms {len(l):>10} {l_str} {len(t):>11} {t_str}")

    # Aggregate by bucket
    print(f"\n=== Aggregated by time bucket ===")
    def bucket(ms):
        if ms < 200: return "0-200ms"
        if ms < 500: return "200-500"
        if ms < 1000: return "500-1000"
        if ms < 2000: return "1000-2000"
        return "2000+"
    lead_by_bucket = defaultdict(list)
    trail_by_bucket = defaultdict(list)
    for ts, ratios in leading_changes.items():
        lead_by_bucket[bucket(ts)].extend(ratios)
    for ts, ratios in trailing_changes.items():
        trail_by_bucket[bucket(ts)].extend(ratios)
    print(f"{'bucket':<12} {'leading n':>10} {'leading med':>13} {'trailing n':>11} {'trailing med':>14}")
    for k in ["0-200ms", "200-500", "500-1000", "1000-2000", "2000+"]:
        l = lead_by_bucket.get(k, [])
        t = trail_by_bucket.get(k, [])
        if not l and not t: continue
        l_med = statistics.median(l) if l else 0
        t_med = statistics.median(t) if t else 0
        print(f"{k:<12} {len(l):>10} {l_med:>12.4f} {len(t):>11} {t_med:>13.4f}")
    print()
    print("Interpretation:")
    print("- If leading mult ratio < 1 SOON after move (e.g., <0.9 at 100ms): MM was slow to recalibrate")
    print("  → bets on leading cells AT t=0 had higher real prob than 1/mult — EDGE")
    print("- If leading ratio ≈ 1 at all times: MM repriced instantly. No latency edge.")
    print("- 0-200ms is the critical window — that's the action zone for latency arb")


if __name__ == "__main__":
    main()
