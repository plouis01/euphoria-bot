"""Test whether liquidation clusters predict bet outcomes.

Hypothesis (STALEQUANT_REVIEW.md task 2): when ETH liquidations cluster,
realized vol spikes and our touch_prob model breaks. If we can detect
clusters within seconds of a bet, we have an ex-ante regime gate.

For each fired bet (across all 5 DRY bots), count liquidations in the
window [T-X, T] for X ∈ {30s, 60s, 5min}. Split bets by liquidation
count + total notional. Measure $/tap per bucket.

Independence check: stratify by ev_gauss tertile to see if the signal
is orthogonal to existing EV ranking.

Sources:
  - Bets: logs/DRY_B*_2026052*.log
  - Liquidations: logs/liquidations_2026052[5-6]*.jsonl
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


def parse_bets(log_path, bot_tag):
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+)"
    )
    res_re = re.compile(
        r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)"
    )
    sigs = {}
    for line in open(log_path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "p_gauss": float(m.group(4)),
                "ev_gauss": float(m.group(5)),
                "bet_ts_ms": (cx * TIME_INTERVAL - 30) * 1000,
            }
    bets = []
    for line in open(log_path):
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sig = sigs.get((cx, cy))
            if not sig: continue
            rs = m.group(3)
            if rs not in ("WIN", "LOSS"): continue
            bets.append({"bot": bot_tag, "cell": (cx, cy),
                         "won": rs == "WIN", **sig})
    return bets


def load_liquidations(paths):
    """Return list of (ts_ms, usd, side, venue) sorted by ts."""
    liqs = []
    for p in paths:
        if not p.exists(): continue
        for line in open(p):
            try: d = json.loads(line)
            except: continue
            if "ts_ms" not in d: continue
            liqs.append((d["ts_ms"], d.get("usd", 0),
                         d.get("side", "?"), d.get("venue", "?")))
    liqs.sort()
    return liqs


def liq_window(liqs_ts, liqs_usd, end_ts_ms, lookback_sec):
    """Count + sum-usd of liquidations in [end - lookback, end]."""
    start_ts = end_ts_ms - lookback_sec * 1000
    i_start = bisect.bisect_left(liqs_ts, start_ts)
    i_end = bisect.bisect_right(liqs_ts, end_ts_ms)
    n = i_end - i_start
    usd = sum(liqs_usd[i_start:i_end])
    return n, usd


def pnl_of(b):
    return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def _stats(bets):
    n = len(bets)
    if n == 0: return (0, 0, 0.0, 0.0, 0.0)
    wins = sum(1 for b in bets if b["won"])
    pnls = [pnl_of(b) for b in bets]
    pnl = sum(pnls); per = pnl / n
    if n >= 2:
        sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5
        t = per / sd * math.sqrt(n) if sd > 0 else 0
    else: t = 0
    return n, wins, pnl, per, t


def summary(bets, label):
    n, w, pnl, per, t = _stats(bets)
    if n == 0: return f"  {label:<35} n=0"
    return (f"  {label:<35} n={n:>4} WR={w/n*100:>5.1f}% "
            f"pnl=${pnl:>+8.2f} $/tap=${per:>+7.3f} t={t:>+5.2f}")


def main():
    # Load all bets across all bots
    bets = []
    bot_logs = sorted((ROOT / "logs").glob("DRY_B*_2026052*.log"))
    for p in bot_logs:
        tag = p.stem.split("_2026")[0]
        b = parse_bets(p, tag)
        bets.extend(b)
        print(f"  {tag}: {len(b)} resolved bets")
    print(f"  TOTAL bets across all bots: {len(bets)}")

    # Load liquidations
    liq_paths = sorted((ROOT / "logs").glob("liquidations_2026052[5-6]*.jsonl"))
    print(f"  liquidation files: {[p.name for p in liq_paths]}")
    liqs = load_liquidations(liq_paths)
    print(f"  liquidations total: {len(liqs)}")
    if not liqs:
        print("  NO LIQUIDATION DATA — abort"); return

    liqs_ts = [x[0] for x in liqs]
    liqs_usd = [x[1] for x in liqs]
    print(f"  liq time range: {liqs[0][0]} → {liqs[-1][0]}  "
          f"({(liqs[-1][0]-liqs[0][0])/3.6e6:.1f}h)")

    # Filter bets to those within the liquidation observation window
    bets_in_window = [b for b in bets
                      if liqs[0][0] <= b["bet_ts_ms"] <= liqs[-1][0]]
    print(f"  bets within liq observation window: {len(bets_in_window)}")
    if not bets_in_window:
        print("  no overlap — abort"); return

    # Enrich each bet with liquidation density
    for b in bets_in_window:
        T = b["bet_ts_ms"]
        for w_sec, lbl in [(30, "30s"), (60, "60s"), (300, "5m")]:
            n, usd = liq_window(liqs_ts, liqs_usd, T, w_sec)
            b[f"liq_n_{lbl}"] = n
            b[f"liq_usd_{lbl}"] = usd

    # ====== Splits ======
    print(f"\n--- H1: bet outcome vs liquidation count in last 30s ---")
    print(summary(bets_in_window, "BASELINE (in window)"))
    for thr_n, lbl in [(0, "no liqs (n=0)"), (1, ">=1 liq"),
                       (3, ">=3 liqs"), (5, ">=5 liqs")]:
        sub = [b for b in bets_in_window if b["liq_n_30s"] >= thr_n] \
              if thr_n > 0 else [b for b in bets_in_window if b["liq_n_30s"] == 0]
        print(summary(sub, f"30s {lbl}"))

    print(f"\n--- H1b: liquidation count in last 60s ---")
    for thr_n, lbl in [(0, "0 liqs"), (1, ">=1 liq"), (3, ">=3 liqs"),
                       (5, ">=5 liqs"), (10, ">=10 liqs")]:
        sub = [b for b in bets_in_window if b["liq_n_60s"] >= thr_n] \
              if thr_n > 0 else [b for b in bets_in_window if b["liq_n_60s"] == 0]
        print(summary(sub, f"60s {lbl}"))

    print(f"\n--- H1c: liquidation count in last 5min ---")
    for thr_n, lbl in [(0, "0 liqs"), (1, ">=1 liq"), (5, ">=5 liqs"),
                       (10, ">=10 liqs"), (20, ">=20 liqs")]:
        sub = [b for b in bets_in_window if b["liq_n_5m"] >= thr_n] \
              if thr_n > 0 else [b for b in bets_in_window if b["liq_n_5m"] == 0]
        print(summary(sub, f"5m {lbl}"))

    print(f"\n--- H2: liquidation NOTIONAL in last 60s (USD) ---")
    for lo, hi, lbl in [(0, 0, "$0 (no liqs)"),
                        (0, 50_000, "$0-$50k"),
                        (50_000, 250_000, "$50k-$250k"),
                        (250_000, None, ">$250k")]:
        if hi is None:
            sub = [b for b in bets_in_window if b["liq_usd_60s"] >= lo]
        elif lo == 0 and hi == 0:
            sub = [b for b in bets_in_window if b["liq_usd_60s"] == 0]
        else:
            sub = [b for b in bets_in_window
                   if lo <= b["liq_usd_60s"] < hi]
        print(summary(sub, lbl))

    # ====== Independence check ======
    print(f"\n--- ORTHOGONALITY: liq density × ev_gauss tertile ---")
    if len(bets_in_window) >= 30:
        evs = sorted(b["ev_gauss"] for b in bets_in_window)
        q1, q2 = evs[len(evs)//3], evs[2*len(evs)//3]
        print(f"  ev_gauss cuts: low<{q1:.2f}  high>{q2:.2f}")
        for ev_lo, ev_hi, ev_lbl in [(None, q1, "EV-low"),
                                      (q1, q2, "EV-mid"),
                                      (q2, None, "EV-high")]:
            tertile = [b for b in bets_in_window
                       if (ev_lo is None or b["ev_gauss"] >= ev_lo)
                       and (ev_hi is None or b["ev_gauss"] < ev_hi)]
            no_liq = [b for b in tertile if b["liq_n_60s"] == 0]
            some_liq = [b for b in tertile if b["liq_n_60s"] >= 1]
            many_liq = [b for b in tertile if b["liq_n_60s"] >= 3]
            print(f"\n  {ev_lbl} (n={len(tertile)}):")
            print(summary(no_liq, "  no liqs in 60s"))
            print(summary(some_liq, "  >=1 liq in 60s"))
            print(summary(many_liq, "  >=3 liqs in 60s"))

    # ====== Distribution check ======
    print(f"\n--- liq_n_60s distribution across in-window bets ---")
    from collections import Counter
    dist = Counter(b["liq_n_60s"] for b in bets_in_window)
    cum = 0
    total = len(bets_in_window)
    for n_liq in sorted(dist.keys()):
        cum += dist[n_liq]
        pct = 100 * dist[n_liq] / total
        cum_pct = 100 * cum / total
        if n_liq <= 5 or dist[n_liq] >= 50:
            print(f"  liq_n_60s={n_liq:>3}: n_bets={dist[n_liq]:>4} ({pct:>5.1f}%)  cum={cum_pct:>5.1f}%")


if __name__ == "__main__":
    main()
