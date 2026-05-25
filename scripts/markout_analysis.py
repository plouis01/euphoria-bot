"""Markout analysis per bet — extract continuous signal from win/loss tape.

For each fired bet:
  - Δprice from bet-time to T+2.5s (mid-window) and T+5s (expiry)
  - Cell-implied-prob at bet, T+2.5s, T+5s (using snapshot tape)
  - Slippage attribution: entry-mq vs realized payoff
  - Direction agreement: did price actually move toward the cell band?

This converts binary win/loss into continuous attribution, surfacing patterns
that the win/loss tape hides (e.g., systematically entering cells right before
MM reprices).
"""
import bisect
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5

PAIRS = [
    ("WINNER_naked_2", "logs/DRY_naked_2_20260524_224305.log",
                       "logs/cell_snapshots_20260524_224305.jsonl"),
]


def parse_bets(path):
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) "
        r"σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+) "
        r"q_age=(\d+)ms"
    )
    res_re = re.compile(
        r"resolve cell=\((\d+),(\d+)\).*EU=(WIN|LOSS).*RS=(WIN|LOSS|N/A)"
    )
    sigs = {}
    for line in open(path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "p_gauss": float(m.group(4)),
                "ev_gauss": float(m.group(5)), "sigma": float(m.group(6)),
                "p_eu_at_bet": float(m.group(7)), "drift": float(m.group(9)),
                "q_age_ms": int(m.group(10)),
                "bet_ts_ms": (cx * TIME_INTERVAL - 30) * 1000,
            }
    bets = []
    for line in open(path):
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sig = sigs.get((cx, cy))
            if not sig: continue
            rs = m.group(4)
            if rs not in ("WIN", "LOSS"): continue
            bets.append({"cell": (cx, cy), "won": rs == "WIN", **sig})
    return bets


def load_snapshot_streams(snap_path, cells_needed):
    """Return (price_tape, cell_history) — price_tape is [(ts_ms, price)],
    cell_history is {(cx,cy): [(ts_ms, mq), ...]}."""
    price_tape = []
    history = defaultdict(list)
    running = {}
    n = 0
    for line in open(snap_path):
        try: d = json.loads(line)
        except: continue
        ts = d["ts_ms"]; t = d.get("t")
        if t == "p":
            price_tape.append((ts, d["price"]))
        elif t == "f":
            for k, v in d["full"].items():
                cs = k.split("_"); key = (int(cs[0]), int(cs[1]))
                if key in cells_needed and (key not in running or running[key] != v):
                    history[key].append((ts, v)); running[key] = v
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                key = (cx, cy)
                if key not in cells_needed: continue
                if m is None:
                    running.pop(key, None)
                elif key not in running or running[key] != m:
                    history[key].append((ts, m)); running[key] = m
        n += 1
        if n % 50000 == 0:
            print(f"  scanned {n} events...", flush=True)
    for k in history: history[k].sort()
    price_tape.sort()
    return price_tape, history


def price_at(price_tape, ts_ms):
    if not price_tape: return None
    ts_list = [x[0] for x in price_tape]
    i = bisect.bisect_right(ts_list, ts_ms) - 1
    if i < 0: return None
    return price_tape[i][1]


def mq_at(hist, ts_ms):
    if not hist: return None
    ts_list = [x[0] for x in hist]
    i = bisect.bisect_right(ts_list, ts_ms) - 1
    if i < 0: return None
    return hist[i][1]


def pnl_of(b):
    return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def _stats(bets, key):
    """Stats for a numeric key. Returns mean, median, p10, p90."""
    vals = [b[key] for b in bets if b.get(key) is not None]
    if not vals: return None
    vals.sort()
    n = len(vals)
    return {
        "n": n, "mean": sum(vals) / n,
        "median": vals[n // 2],
        "p10": vals[n // 10], "p90": vals[9 * n // 10],
    }


def main():
    for label, log_path, snap_path in PAIRS:
        print(f"\n{'='*70}\n=== {label} ===\n")
        bets = parse_bets(ROOT / log_path)
        print(f"  bets: {len(bets)}")
        cells_needed = set(b["cell"] for b in bets)
        print(f"  loading snapshot tape...")
        price_tape, hist = load_snapshot_streams(ROOT / snap_path, cells_needed)
        print(f"  price ticks: {len(price_tape)}, cells: {len(hist)}")

        # Enrich each bet with markout features
        for b in bets:
            T = b["bet_ts_ms"]
            cx, cy = b["cell"]
            band_a = cy * PRICE_INTERVAL
            band_b = band_a + PRICE_INTERVAL
            band_mid = (band_a + band_b) / 2

            # Window times
            t_start = cx * TIME_INTERVAL * 1000  # window starts at cx*5s
            t_end = t_start + TIME_INTERVAL * 1000
            t_mid = (t_start + t_end) // 2

            # Prices at bet, mid-window, expiry
            p_bet = price_at(price_tape, T) or b["p_eu_at_bet"]
            p_mid = price_at(price_tape, t_mid)
            p_end = price_at(price_tape, t_end)

            b["p_bet"] = p_bet
            b["p_mid"] = p_mid
            b["p_end"] = p_end
            b["dp_to_mid"] = (p_mid - p_bet) if (p_mid and p_bet) else None
            b["dp_to_end"] = (p_end - p_bet) if (p_end and p_bet) else None

            # Direction: did price move TOWARD the band?
            # Distance from price to nearest band edge, signed (negative = inside)
            def dist_to_band(p):
                if p is None: return None
                if band_a <= p <= band_b: return 0
                if p < band_a: return band_a - p  # need to move up
                return p - band_b  # need to move down

            d_bet = dist_to_band(p_bet)
            d_end = dist_to_band(p_end)
            b["dist_at_bet"] = d_bet
            b["dist_at_end"] = d_end
            b["closer_to_band"] = (d_end < d_bet) if (d_bet is not None and d_end is not None) else None

            # Cell-implied prob (1/mq) at bet vs at mid-window
            h = hist.get(b["cell"], [])
            mq_bet = mq_at(h, T)
            mq_mid = mq_at(h, t_mid)
            b["mq_at_bet_snap"] = mq_bet
            b["mq_at_mid"] = mq_mid
            b["mm_implied_p_bet"] = (1.0 / mq_bet) if (mq_bet and mq_bet > 0) else None
            b["mm_implied_p_mid"] = (1.0 / mq_mid) if (mq_mid and mq_mid > 0) else None
            if b["mm_implied_p_bet"] and b["mm_implied_p_mid"]:
                b["mm_p_delta"] = b["mm_implied_p_mid"] - b["mm_implied_p_bet"]
            else:
                b["mm_p_delta"] = None

        # ---- Summaries ----
        print(f"\n--- Markout: price movement bet→expiry ---")
        wins = [b for b in bets if b["won"]]
        losses = [b for b in bets if not b["won"]]
        print(f"  N: wins={len(wins)} losses={len(losses)}")
        for label2, sub in [("WINS", wins), ("LOSSES", losses)]:
            s_mid = _stats(sub, "dp_to_mid")
            s_end = _stats(sub, "dp_to_end")
            if s_mid:
                print(f"  {label2} Δprice→mid: mean=${s_mid['mean']:+.3f} "
                      f"med=${s_mid['median']:+.3f} p10/p90=${s_mid['p10']:+.3f}/${s_mid['p90']:+.3f}")
            if s_end:
                print(f"  {label2} Δprice→end: mean=${s_end['mean']:+.3f} "
                      f"med=${s_end['median']:+.3f} p10/p90=${s_end['p10']:+.3f}/${s_end['p90']:+.3f}")

        # Direction-agreement check: did the price move toward the band?
        moved_closer = [b for b in bets if b["closer_to_band"] is True]
        moved_away = [b for b in bets if b["closer_to_band"] is False]
        print(f"\n--- Direction agreement ---")
        print(f"  bets where price moved CLOSER to band: {len(moved_closer)} "
              f"({100*len(moved_closer)/len(bets):.1f}%)")
        if moved_closer:
            wins_c = sum(1 for b in moved_closer if b["won"])
            pnl_c = sum(pnl_of(b) for b in moved_closer)
            print(f"    WR={100*wins_c/len(moved_closer):.1f}% pnl=${pnl_c:+.2f} "
                  f"$/tap=${pnl_c/len(moved_closer):+.3f}")
        print(f"  bets where price moved AWAY: {len(moved_away)} "
              f"({100*len(moved_away)/len(bets):.1f}%)")
        if moved_away:
            wins_a = sum(1 for b in moved_away if b["won"])
            pnl_a = sum(pnl_of(b) for b in moved_away)
            print(f"    WR={100*wins_a/len(moved_away):.1f}% pnl=${pnl_a:+.2f} "
                  f"$/tap=${pnl_a/len(moved_away):+.3f}")

        # MM-implied-prob delta: is MM repricing AFTER our bet?
        print(f"\n--- MM repricing during the window ---")
        for label2, sub in [("WINS", wins), ("LOSSES", losses)]:
            s_dp = _stats(sub, "mm_p_delta")
            if s_dp:
                print(f"  {label2} ΔMM-implied-p (bet→mid-window): "
                      f"mean={s_dp['mean']:+.4f} med={s_dp['median']:+.4f}  (n={s_dp['n']})")

        # Slippage: realized payoff vs entry-mq predicted
        # For wins: payoff = (mq-1) * (1-DRAG). The DRAG itself is the slippage.
        # Per-bet slippage attribution = entry_mq vs what we cleared on resolution
        # We don't have a direct "resolved mq" but we can compute:
        #   expected_payoff_under_fair = mq_at_bet
        #   realized_payoff_factor = (1 - DRAG) for wins
        # Slippage drag = DRAG of (mq-1) on wins; full $1 on losses (no slippage on losses)
        win_slip = [b["mq"] - 1 - ((b["mq"] - 1) * (1 - DRAG)) for b in wins]
        print(f"\n--- Slippage attribution (on wins) ---")
        if win_slip:
            mean_slip = sum(win_slip) / len(win_slip)
            print(f"  mean per-win slippage drag: ${mean_slip:+.3f} "
                  f"(={DRAG*100:.0f}%% of (mq-1))")
            # The DRAG is constant — this just confirms the model. The interesting
            # comparison: is realized 5-11% range correct, or worse for some subsets?
            # We don't have per-bet realized slippage in the log (only resolution).

        # ---- Cross-tab: direction-agreement × EV bucket ----
        print(f"\n--- Direction agreement × ev_gauss tertile ---")
        bets_with_dir = [b for b in bets if b["closer_to_band"] is not None]
        if len(bets_with_dir) >= 30:
            evs = sorted(b["ev_gauss"] for b in bets_with_dir)
            q1, q2 = evs[len(evs)//3], evs[2*len(evs)//3]
            for ev_lo, ev_hi, ev_lbl in [(None, q1, "EV-low"),
                                          (q1, q2, "EV-mid"),
                                          (q2, None, "EV-high")]:
                tertile = [b for b in bets_with_dir
                           if (ev_lo is None or b["ev_gauss"] >= ev_lo)
                           and (ev_hi is None or b["ev_gauss"] < ev_hi)]
                closer = [b for b in tertile if b["closer_to_band"]]
                away = [b for b in tertile if not b["closer_to_band"]]
                pnl_c = sum(pnl_of(b) for b in closer)
                pnl_a = sum(pnl_of(b) for b in away)
                print(f"  {ev_lbl}: closer n={len(closer)} pnl=${pnl_c:+.2f} "
                      f"$/tap=${pnl_c/max(len(closer),1):+.3f}  |  "
                      f"away n={len(away)} pnl=${pnl_a:+.2f} "
                      f"$/tap=${pnl_a/max(len(away),1):+.3f}")


if __name__ == "__main__":
    main()
