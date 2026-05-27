"""Deep investigation: why are all 10 bots losing today?

Today (2026-05-26 → 27): ~14h run, all 10 bots negative. The c30-strong
strategy + 10 filter combos all underperformed. Yesterday's calm-grind day
some bots were slightly positive; today's trending-down regime broke them.

This script analyzes B0_baseline (no filters, cleanest signal) and asks:
  1. Hourly PnL — when did losses concentrate?
  2. UP-bets vs DOWN-bets PnL — is the direction signal inverted today?
  3. WR by mult bucket — losses on near-money or far-OOM?
  4. p_gauss calibration — does predicted p match actual WR?
  5. σ-bucket × WR — what σ window is the bot losing in?
  6. Cell-distance from current price (drift-correctness check)
  7. EU vs CB price gap — is the bot betting on stale EU prices?
"""
import bisect
import json
import math
import re
import statistics
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5

# Use B0_baseline today (cleanest, no filters)
BOT_LOG = sorted(glob(str(ROOT / "logs/DRY_B0_baseline_20260526_19*.log")))
SNAP = sorted(glob(str(ROOT / "logs/cell_snapshots_20260526_*.jsonl")))[0]


def parse_bets(log_path):
    sig_re = re.compile(
        r"\[(\d\d:\d\d:\d\d)\].*SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) "
        r"ev=\+?(-?[\d.]+) σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) "
        r"drift_cb=\$([+\-][\d.]+)"
    )
    # Fallback without timestamp prefix
    sig_re_no = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) "
        r"σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)"
    )
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    for line in open(log_path):
        m = sig_re_no.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "p_gauss": float(m.group(4)),
                "ev_gauss": float(m.group(5)), "sigma": float(m.group(6)),
                "p_eu": float(m.group(7)), "p_cb": float(m.group(8)),
                "drift": float(m.group(9)),
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
            bets.append({"cell": (cx, cy), "won": rs == "WIN", **sig})
    return bets


def pnl_of(b): return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def stats(bs):
    n = len(bs)
    if n == 0: return None
    wins = sum(1 for b in bs if b["won"])
    pnls = [pnl_of(b) for b in bs]
    pnl = sum(pnls); per = pnl / n
    if n >= 2:
        sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5
        t = per / sd * math.sqrt(n) if sd > 0 else 0
    else: t = 0
    return n, wins, pnl, per, t


def fmt(s):
    if not s: return "n=0"
    n, w, p, per, t = s
    return f"n={n:>4} WR={w/n*100:>5.1f}% pnl=${p:>+8.2f} $/tap=${per:>+7.3f} t={t:>+5.2f}"


def main():
    # Collect bets from all B0_baseline logs (today and yesterday's matching session)
    bets = []
    for p in BOT_LOG:
        bets.extend(parse_bets(p))
    # Also include yesterday's B50 for cross-regime comparison
    yest_bets = []
    for p in sorted(glob(str(ROOT / "logs/DRY_B50_20260525_*.log"))):
        yest_bets.extend(parse_bets(p))

    print(f"Today's B0_baseline:  {len(bets)} bets")
    print(f"Yesterday's B50 (ref): {len(yest_bets)} bets")
    print()

    # =================================================================
    # 1. Hourly PnL — when did losses concentrate?
    # =================================================================
    print("=" * 70)
    print("1. HOURLY PnL")
    print("=" * 70)
    import datetime
    by_hour = defaultdict(list)
    for b in bets:
        hour = datetime.datetime.fromtimestamp(b["bet_ts_ms"] / 1000).strftime("%H")
        by_hour[hour].append(b)
    for h in sorted(by_hour.keys()):
        s = stats(by_hour[h])
        print(f"  hour {h}:00  {fmt(s)}")
    print()

    # =================================================================
    # 2. UP vs DOWN bets
    # =================================================================
    print("=" * 70)
    print("2. UP-BETS vs DOWN-BETS (cell band above or below P_eu)")
    print("=" * 70)
    up_bets, dn_bets = [], []
    for b in bets:
        band_lo = b["cell"][1] * PRICE_INTERVAL
        band_mid = band_lo + PRICE_INTERVAL / 2
        if band_mid > b["p_eu"]: up_bets.append(b)
        else: dn_bets.append(b)
    print(f"  TODAY:")
    print(f"    UP-bets   {fmt(stats(up_bets))}")
    print(f"    DOWN-bets {fmt(stats(dn_bets))}")
    # Same for yesterday for comparison
    yup, ydn = [], []
    for b in yest_bets:
        band_lo = b["cell"][1] * PRICE_INTERVAL
        band_mid = band_lo + PRICE_INTERVAL / 2
        if band_mid > b["p_eu"]: yup.append(b)
        else: ydn.append(b)
    print(f"  YESTERDAY (B50 calm-grind ref):")
    print(f"    UP-bets   {fmt(stats(yup))}")
    print(f"    DOWN-bets {fmt(stats(ydn))}")
    print()

    # =================================================================
    # 3. WR by mult bucket — where are losses concentrated?
    # =================================================================
    print("=" * 70)
    print("3. WR BY MULT BUCKET")
    print("=" * 70)
    print(f"  TODAY:")
    for lo, hi, lbl in [(1, 1.5, "1.0-1.5 (near-money)"),
                        (1.5, 2.0, "1.5-2.0"),
                        (2.0, 3.0, "2.0-3.0"),
                        (3.0, 5.0, "3.0-5.0"),
                        (5.0, 8.0, "5.0-8.0 (max edge)"),
                        (8.0, None, ">=8.0")]:
        sub = [b for b in bets if lo <= b["mq"] and (hi is None or b["mq"] < hi)]
        s = stats(sub)
        print(f"    mult {lbl:<22} {fmt(s)}")
    print(f"  YESTERDAY:")
    for lo, hi, lbl in [(1, 1.5, "1.0-1.5"), (1.5, 2.0, "1.5-2.0"),
                        (2.0, 3.0, "2.0-3.0"), (3.0, 5.0, "3.0-5.0"),
                        (5.0, 8.0, "5.0-8.0"), (8.0, None, ">=8.0")]:
        sub = [b for b in yest_bets if lo <= b["mq"] and (hi is None or b["mq"] < hi)]
        s = stats(sub)
        print(f"    mult {lbl:<22} {fmt(s)}")
    print()

    # =================================================================
    # 4. p_gauss calibration — predicted vs actual WR
    # =================================================================
    print("=" * 70)
    print("4. TOUCH-PROB CALIBRATION (predicted p_gauss vs actual WR)")
    print("=" * 70)
    print(f"  TODAY:")
    for lo, hi in [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.01)]:
        sub = [b for b in bets if lo <= b["p_gauss"] < hi]
        n = len(sub)
        if n == 0: continue
        wins = sum(1 for b in sub if b["won"])
        actual_wr = wins / n
        pred = statistics.mean([b["p_gauss"] for b in sub])
        gap = pred - actual_wr
        print(f"    p_gauss [{lo:.2f},{hi:.2f}) n={n:>4} predicted={pred:.3f} "
              f"actual_WR={actual_wr:.3f} gap={gap:+.3f} (over-predict>0)")
    print(f"  YESTERDAY (B50):")
    for lo, hi in [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.50), (0.50, 1.01)]:
        sub = [b for b in yest_bets if lo <= b["p_gauss"] < hi]
        n = len(sub)
        if n == 0: continue
        wins = sum(1 for b in sub if b["won"])
        actual_wr = wins / n
        pred = statistics.mean([b["p_gauss"] for b in sub])
        gap = pred - actual_wr
        print(f"    p_gauss [{lo:.2f},{hi:.2f}) n={n:>4} predicted={pred:.3f} "
              f"actual_WR={actual_wr:.3f} gap={gap:+.3f}")
    print()

    # =================================================================
    # 5. σ-bucket × outcome
    # =================================================================
    print("=" * 70)
    print("5. σ AT SIGNAL TIME (drift-trigger condition: σ correlates with WR?)")
    print("=" * 70)
    print(f"  TODAY:")
    for lo, hi, lbl in [(0, 0.05, "very calm"),
                        (0.05, 0.10, "calm"),
                        (0.10, 0.15, "moderate"),
                        (0.15, 0.25, "elevated"),
                        (0.25, 0.50, "high"),
                        (0.50, None, "very high")]:
        sub = [b for b in bets if lo <= b["sigma"] and (hi is None or b["sigma"] < hi)]
        s = stats(sub)
        print(f"    σ {lbl:<12} ({lo:.2f}-{hi if hi else '∞'})  {fmt(s)}")
    print()

    # =================================================================
    # 6. EU vs CB price gap at signal time
    # =================================================================
    print("=" * 70)
    print("6. EU-CB PRICE GAP (was bot betting on stale-EU vs fresh-CB?)")
    print("=" * 70)
    for b in bets:
        b["eu_cb_gap"] = b["p_eu"] - b["p_cb"]
    print(f"  TODAY (gap = P_eu - P_cb; |gap| > 0.50 suggests staleness):")
    for lo, hi, lbl in [(-10, -1.0, "EU way below CB (<-1.0)"),
                        (-1.0, -0.50, "EU below CB (-1.0,-0.50)"),
                        (-0.50, -0.10, "EU mildly below"),
                        (-0.10, 0.10, "tracking (±$0.10)"),
                        (0.10, 0.50, "EU mildly above"),
                        (0.50, 1.0, "EU above CB (0.50,1.0)"),
                        (1.0, 10, "EU way above (>1.0)")]:
        sub = [b for b in bets if lo <= b["eu_cb_gap"] < hi]
        s = stats(sub)
        print(f"    gap {lbl:<28} {fmt(s)}")
    print()

    # =================================================================
    # 7. Drift magnitude
    # =================================================================
    print("=" * 70)
    print("7. DRIFT MAGNITUDE (signal strength)")
    print("=" * 70)
    print(f"  TODAY:")
    for lo, hi, lbl in [(0.20, 0.30, "drift 0.20-0.30 (just over threshold)"),
                        (0.30, 0.50, "drift 0.30-0.50"),
                        (0.50, 1.00, "drift 0.50-1.00"),
                        (1.00, 2.00, "drift 1.00-2.00"),
                        (2.00, None, "drift > 2.00 (extreme)")]:
        sub = [b for b in bets if lo <= abs(b["drift"]) and (hi is None or abs(b["drift"]) < hi)]
        s = stats(sub)
        print(f"    {lbl:<40} {fmt(s)}")
    print()

    # =================================================================
    # 8. Drift direction vs. cell direction (does bot align?)
    # =================================================================
    print("=" * 70)
    print("8. DRIFT-DIRECTION × CELL-DIRECTION (alignment check)")
    print("=" * 70)
    aligned, anti = [], []
    for b in bets:
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        cell_dir = 1 if band_mid > b["p_eu"] else -1
        drift_dir = 1 if b["drift"] > 0 else -1
        if cell_dir == drift_dir: aligned.append(b)
        else: anti.append(b)
    print(f"  aligned (cell matches drift sign): {fmt(stats(aligned))}")
    print(f"  anti (cell opposite drift)      : {fmt(stats(anti))}")
    print()
    print(f"  Sanity check: c30-strong should ALWAYS align cell direction with drift.")
    print(f"  Any 'anti' bets are bugs or counter-strategies.")


if __name__ == "__main__":
    main()
