"""Test whether mq trajectory and cell freshness predict bet outcome.

For each fired bet in yesterday's DRY logs:
  - Look up cell's mq history in the snapshot tape
  - Compute Δmq over last {1s, 5s, 30s} before bet time
  - Parse q_age (freshness) directly from SIGNAL line
  - Split bets by trajectory sign + freshness
  - Compare WR / $/tap / t-stat across splits

If mq was FALLING before our bet (MM repricing toward cell), that's
confirmation. If RISING (MM repricing away), our drift-pick disagrees with MM.

H1 (trajectory): bets on cells with falling mq win more than bets on rising-mq cells.
H2 (freshness): bets on fresh-quote cells behave differently than stale.

INDEPENDENCE CHECK: bin bets by ev_gauss tertile, then check trajectory $/tap
within each bin. If trajectory still separates within a bin, it's an
orthogonal signal to ev_gauss (which is mostly captured by 1/mq calibration).
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
TIME_INTERVAL = 5  # cellX bucket = 5s

PAIRS = [
    ("LOSER_naked",     "logs/DRY_naked_20260524_163524.log",
                        "logs/cell_snapshots_20260524_224305.jsonl"),
    ("WINNER_naked_2",  "logs/DRY_naked_2_20260524_224305.log",
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
                "mq": float(m.group(3)),
                "p_gauss": float(m.group(4)),
                "ev_gauss": float(m.group(5)),
                "sigma": float(m.group(6)),
                "p_eu": float(m.group(7)),
                "drift": float(m.group(9)),
                "q_age_ms": int(m.group(10)),
                # Approximate bet timestamp from cx (bet fires ~30s before target_cx*5)
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
            bets.append({
                "cell": (cx, cy), "won": rs == "WIN", **sig,
            })
    return bets


def build_cell_mq_history(snap_path, cells_needed):
    """Walk snapshot tape once; return dict {(cx,cy): [(ts_ms, mq), ...]}."""
    history = defaultdict(list)
    running = {}
    n = 0
    for line in open(snap_path):
        try: d = json.loads(line)
        except: continue
        ts = d["ts_ms"]
        t = d.get("t")
        if t == "f":
            for k, v in d["full"].items():
                cx_s, cy_s = k.split("_")
                key = (int(cx_s), int(cy_s))
                if key in cells_needed and (key not in running or running[key] != v):
                    history[key].append((ts, v))
                    running[key] = v
        elif t == "c":
            for cx, cy, m in d["diffs"]:
                key = (cx, cy)
                if key not in cells_needed: continue
                if m is None:
                    running.pop(key, None)
                elif key not in running or running[key] != m:
                    history[key].append((ts, m))
                    running[key] = m
        n += 1
        if n % 50000 == 0:
            print(f"  scanned {n} events, tracking {sum(1 for k in history if history[k])} cells", flush=True)
    # Sort each cell's history just in case
    for k in history:
        history[k].sort()
    return history


def mq_at_or_before(hist, ts):
    """Latest mq value at or before ts; None if no record."""
    if not hist: return None
    # bisect for ts in list of (ts, mq)
    ts_list = [x[0] for x in hist]
    i = bisect.bisect_right(ts_list, ts) - 1
    if i < 0: return None
    return hist[i][1]


def pnl_of(b):
    return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def _stats(bets):
    """Return (n, wins, pnl, per_tap, t)."""
    n = len(bets)
    if n == 0: return 0, 0, 0.0, 0.0, 0.0
    wins = sum(1 for b in bets if b["won"])
    pnls = [pnl_of(b) for b in bets]
    pnl = sum(pnls)
    per = pnl / n
    if n >= 2:
        sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5
        t = (per / sd * math.sqrt(n)) if sd > 0 else 0
    else:
        t = 0
    return n, wins, pnl, per, t


def jackknife(bets, top_k):
    """Return bets with top_k highest-PnL bets removed."""
    if not bets: return bets
    sorted_b = sorted(bets, key=pnl_of, reverse=True)
    return sorted_b[top_k:]


def summary(bets, label):
    n, wins, pnl, per, t = _stats(bets)
    if n == 0: return f"  {label:<35} n=0"
    base = (f"  {label:<35} n={n:>4}  WR={wins/n*100:>5.1f}%  "
            f"pnl=${pnl:>+8.2f}  $/tap=${per:>+7.3f}  t={t:>+5.2f}")
    return base


def summary_with_jackknife(bets, label):
    """Summary line + ex-top-3 and ex-top-20 jackknife."""
    n, wins, pnl, per, t = _stats(bets)
    if n == 0: return f"  {label:<35} n=0"
    line = (f"  {label:<35} n={n:>4}  WR={wins/n*100:>5.1f}%  "
            f"$/tap=${per:>+7.3f} t={t:>+5.2f}")
    # ex-top-3
    ex3 = jackknife(bets, 3)
    n3, _, pnl3, per3, t3 = _stats(ex3)
    line += f"  | ex-top-3: $/tap=${per3:>+7.3f} t={t3:>+5.2f}"
    # ex-top-20
    ex20 = jackknife(bets, 20)
    n20, _, pnl20, per20, t20 = _stats(ex20)
    line += f"  | ex-top-20: $/tap=${per20:>+7.3f} t={t20:>+5.2f} (n={n20})"
    # Top-20 share of PnL
    if pnl != 0:
        top20_pnl = pnl - pnl20
        share = 100 * top20_pnl / pnl
        line += f"  top20={share:.0f}%"
    return line


def main():
    for label, log_path, snap_path in PAIRS:
        print(f"\n{'='*70}")
        print(f"=== {label} ===")
        print(f"=== log:  {log_path}")
        print(f"=== snap: {snap_path}")

        bets = parse_bets(ROOT / log_path)
        print(f"  bets parsed: {len(bets)}")
        if not bets: continue

        cells_needed = set(b["cell"] for b in bets)
        print(f"  unique cells: {len(cells_needed)} — building mq history...")
        hist = build_cell_mq_history(ROOT / snap_path, cells_needed)
        print(f"  cells with history: {len(hist)}")

        # Enrich each bet with trajectory
        for b in bets:
            h = hist.get(b["cell"], [])
            T = b["bet_ts_ms"]
            mq_now = mq_at_or_before(h, T)
            b["mq_now_snap"] = mq_now
            b["mq_minus_1s"] = mq_at_or_before(h, T - 1000)
            b["mq_minus_5s"] = mq_at_or_before(h, T - 5000)
            b["mq_minus_30s"] = mq_at_or_before(h, T - 30000)
            # Δmq over each window (positive = mq rose = MM moved AWAY from cell)
            b["dmq_1s"] = (mq_now - b["mq_minus_1s"]) if (mq_now is not None and b["mq_minus_1s"] is not None) else None
            b["dmq_5s"] = (mq_now - b["mq_minus_5s"]) if (mq_now is not None and b["mq_minus_5s"] is not None) else None
            b["dmq_30s"] = (mq_now - b["mq_minus_30s"]) if (mq_now is not None and b["mq_minus_30s"] is not None) else None
            # As pct ratio
            b["rmq_5s"] = (mq_now / b["mq_minus_5s"]) if (mq_now is not None and b["mq_minus_5s"] and b["mq_minus_5s"] > 0) else None
            b["rmq_30s"] = (mq_now / b["mq_minus_30s"]) if (mq_now is not None and b["mq_minus_30s"] and b["mq_minus_30s"] > 0) else None

        coverage = sum(1 for b in bets if b["mq_now_snap"] is not None)
        print(f"  snap coverage: {coverage}/{len(bets)} ({100*coverage/len(bets):.1f}%)")

        # ====== H1: trajectory split ======
        print(f"\n  --- H1: mq trajectory (Δmq over 5s before bet) ---")
        print(summary([b for b in bets], "BASELINE all bets"))
        for thresh, lbl in [(-0.5, "FALLING<-0.5 (MM toward cell)"),
                            (-0.05, "FALLING<-0.05"),
                            (None, "neutral [-0.05, 0.05]"),
                            (0.05, "RISING>0.05 (MM away)"),
                            (0.5, "RISING>0.5")]:
            if thresh is None:
                sub = [b for b in bets if b["dmq_5s"] is not None and -0.05 <= b["dmq_5s"] <= 0.05]
            elif thresh < 0:
                sub = [b for b in bets if b["dmq_5s"] is not None and b["dmq_5s"] < thresh]
            else:
                sub = [b for b in bets if b["dmq_5s"] is not None and b["dmq_5s"] > thresh]
            print(summary(sub, lbl))

        # Pct ratio (more robust across mq levels) — with jackknife
        print(f"\n  --- H1b: ratio rmq_5s = mq_now / mq_5s_ago (with jackknife) ---")
        for lo, hi, lbl in [(None, 0.80, "rmq < 0.80 (MM cut >20%)"),
                            (0.80, 0.95, "rmq 0.80-0.95"),
                            (0.95, 1.05, "rmq ~unchanged"),
                            (1.05, 1.20, "rmq 1.05-1.20"),
                            (1.20, None, "rmq > 1.20 (MM raised >20%)")]:
            sub = [b for b in bets if b["rmq_5s"] is not None
                   and (lo is None or b["rmq_5s"] >= lo)
                   and (hi is None or b["rmq_5s"] < hi)]
            print(summary_with_jackknife(sub, lbl))
        # Baseline row for reference
        print(summary_with_jackknife([b for b in bets], "BASELINE all"))

        # ====== H2: q_age freshness ======
        print(f"\n  --- H2: q_age (cell quote freshness at bet time) ---")
        for lo, hi, lbl in [(0, 200, "q_age < 200ms (very fresh)"),
                            (200, 500, "q_age 200-500ms"),
                            (500, 1000, "q_age 500ms-1s"),
                            (1000, 3000, "q_age 1-3s (stale)"),
                            (3000, None, "q_age >3s (very stale)")]:
            sub = [b for b in bets if lo <= b["q_age_ms"]
                   and (hi is None or b["q_age_ms"] < hi)]
            print(summary(sub, lbl))

        # ====== Orthogonality check: trajectory split WITHIN ev_gauss bins ======
        print(f"\n  --- ORTHOGONALITY: trajectory $/tap WITHIN ev_gauss tertiles ---")
        bets_with_traj = [b for b in bets if b["rmq_5s"] is not None]
        if len(bets_with_traj) >= 30:
            evs = sorted(b["ev_gauss"] for b in bets_with_traj)
            q1, q2 = evs[len(evs)//3], evs[2*len(evs)//3]
            print(f"  ev_gauss tertile cuts: low<{q1:.2f}  high>{q2:.2f}")
            for ev_lo, ev_hi, ev_lbl in [(None, q1, "EV-low"),
                                          (q1, q2, "EV-mid"),
                                          (q2, None, "EV-high")]:
                tertile = [b for b in bets_with_traj
                           if (ev_lo is None or b["ev_gauss"] >= ev_lo)
                           and (ev_hi is None or b["ev_gauss"] < ev_hi)]
                print(f"\n    {ev_lbl} ev tertile (n={len(tertile)}):")
                falling = [b for b in tertile if b["rmq_5s"] < 0.95]
                neutral = [b for b in tertile if 0.95 <= b["rmq_5s"] <= 1.05]
                rising = [b for b in tertile if b["rmq_5s"] > 1.05]
                print(summary_with_jackknife(falling, "  falling (rmq<0.95)"))
                print(summary_with_jackknife(neutral, "  neutral"))
                print(summary_with_jackknife(rising, "  rising (rmq>1.05)"))

        # ====== DEEP DIVE: the headline "rmq<0.80" subset ======
        print(f"\n  --- DEEP DIVE: rmq<0.80 distributional check ---")
        falling_strong = sorted(
            [b for b in bets if b["rmq_5s"] is not None and b["rmq_5s"] < 0.80],
            key=pnl_of, reverse=True,
        )
        if falling_strong:
            print(f"  total n={len(falling_strong)}, total pnl=${sum(pnl_of(b) for b in falling_strong):+.2f}")
            print(f"  top-5 individual PnLs: " + ", ".join(
                f"${pnl_of(b):+.2f}" for b in falling_strong[:5]))
            cum = 0
            for k in [3, 10, 20, 50]:
                if k >= len(falling_strong): continue
                tk = sum(pnl_of(b) for b in falling_strong[:k])
                total = sum(pnl_of(b) for b in falling_strong)
                pct = 100 * tk / total if total != 0 else 0
                print(f"  top-{k}: ${tk:+.2f} = {pct:.0f}% of subset PnL")
            # WR check on ex-top-20
            ex20 = falling_strong[20:]
            if ex20:
                n_ex, wins_ex, pnl_ex, per_ex, t_ex = _stats(ex20)
                print(f"  ex-top-20: n={n_ex}, WR={wins_ex/n_ex*100:.1f}%, "
                      f"pnl=${pnl_ex:+.2f}, $/tap=${per_ex:+.3f}, t={t_ex:+.2f}")


if __name__ == "__main__":
    main()
