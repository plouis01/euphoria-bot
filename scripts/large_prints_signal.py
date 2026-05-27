"""Test whether large-print net flow predicts bet outcomes.

We have 32k large prints (>= $10k USD) across Binance, Coinbase, Hyperliquid.
Net flow = sum(BUY notional) - sum(SELL notional) in a recent window.

Hypothesis: when net flow is strongly positive (aggressive buying), UP-bets
should win more. When strongly negative, DOWN-bets should win.

For each bot bet, compute:
  net_flow_60s: USD imbalance in last 60s across all venues
  net_flow_30s: same in 30s
  buy_dominance: BUY_usd / (BUY_usd + SELL_usd)
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


def parse_bets(log_path):
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) "
        r"σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)"
    )
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    for line in open(log_path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "p_gauss": float(m.group(4)),
                "sigma": float(m.group(6)),
                "p_eu": float(m.group(7)), "drift": float(m.group(9)),
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


def pnl_of(b): return (b["mq"]-1)*(1-DRAG) if b["won"] else -1


def stats(bs):
    n = len(bs)
    if n == 0: return None
    wins = sum(1 for b in bs if b["won"])
    pnls = [pnl_of(b) for b in bs]
    pnl = sum(pnls); per = pnl/n
    if n >= 2:
        sd = (sum((p-per)**2 for p in pnls)/(n-1))**0.5
        t = per/sd*math.sqrt(n) if sd > 0 else 0
    else: t = 0
    return n, wins, pnl, per, t


def fmt(s):
    if not s: return "n=0"
    n,w,p,per,t = s
    return f"n={n:>4} WR={w/n*100:>5.1f}% pnl=${p:>+8.2f} $/tap=${per:>+7.3f} t={t:>+5.2f}"


def main():
    # Load all large prints
    prints = []
    for p in sorted(glob(str(ROOT / "logs/large_prints_2026052[5-7]*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
                if d.get("usd", 0) >= 10_000:
                    prints.append({
                        "ts_ms": d["ts_ms"], "usd": d["usd"],
                        "side": d.get("side", "").upper(),
                        "venue": d.get("venue", ""),
                    })
            except: pass
    prints.sort(key=lambda x: x["ts_ms"])
    print(f"loaded {len(prints)} large prints (>= $10k)")

    p_ts = [x["ts_ms"] for x in prints]

    def net_flow(end_ts_ms, window_sec):
        """Net USD flow (BUY - SELL) in window."""
        start = end_ts_ms - window_sec * 1000
        i_start = bisect.bisect_left(p_ts, start)
        i_end = bisect.bisect_right(p_ts, end_ts_ms)
        buy_usd = 0.0; sell_usd = 0.0
        for i in range(i_start, i_end):
            p = prints[i]
            if p["side"] == "BUY": buy_usd += p["usd"]
            elif p["side"] == "SELL": sell_usd += p["usd"]
        return buy_usd - sell_usd, buy_usd, sell_usd

    # Load all bets
    all_bets = []
    for p in sorted(glob(str(ROOT / "logs/DRY_*_2026052[5-7]*.log"))):
        all_bets.extend(parse_bets(p))
    print(f"loaded {len(all_bets)} bets")

    # Filter to bets within large-prints observation window
    bets = [b for b in all_bets
            if prints[0]["ts_ms"] <= b["bet_ts_ms"] <= prints[-1]["ts_ms"]]
    print(f"  {len(bets)} bets within large-prints window")

    # Enrich each bet
    for b in bets:
        T = b["bet_ts_ms"]
        nf60, buy60, sell60 = net_flow(T, 60)
        nf30, buy30, sell30 = net_flow(T, 30)
        b["net_flow_60s"] = nf60
        b["net_flow_30s"] = nf30
        b["buy_dom_60s"] = buy60 / (buy60 + sell60) if (buy60 + sell60) > 0 else 0.5
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        b["bet_dir"] = 1 if band_mid > b["p_eu"] else -1  # UP=1, DOWN=-1

    # ============ Section 1: net flow buckets ============
    print()
    print("=== H1: bet outcome vs net flow in last 60s (all venues combined) ===")
    for lo, hi, lbl in [(None, -2_000_000, "<-$2M (heavy SELL)"),
                        (-2_000_000, -500_000, "-$2M to -$500k"),
                        (-500_000, -50_000, "-$500k to -$50k"),
                        (-50_000, 50_000, "near zero (±$50k)"),
                        (50_000, 500_000, "+$50k to +$500k"),
                        (500_000, 2_000_000, "+$500k to +$2M"),
                        (2_000_000, None, "> +$2M (heavy BUY)")]:
        sub = [b for b in bets
               if (lo is None or b["net_flow_60s"] >= lo)
               and (hi is None or b["net_flow_60s"] < hi)]
        s = stats(sub)
        print(f"  {lbl:<25} {fmt(s)}")

    print()
    print("=== H2: bet outcome × bet direction × net flow ===")
    print("Hypothesis: UP-bets win when flow is positive, DOWN-bets win when negative")
    print()
    for lbl, dir_filt, flow_filt in [
            ("UP-bet, flow > +$500k", lambda b: b["bet_dir"] == 1, lambda b: b["net_flow_60s"] > 500_000),
            ("UP-bet, flow -$500k to +$500k", lambda b: b["bet_dir"] == 1, lambda b: -500_000 <= b["net_flow_60s"] <= 500_000),
            ("UP-bet, flow < -$500k", lambda b: b["bet_dir"] == 1, lambda b: b["net_flow_60s"] < -500_000),
            ("DOWN-bet, flow > +$500k", lambda b: b["bet_dir"] == -1, lambda b: b["net_flow_60s"] > 500_000),
            ("DOWN-bet, flow -$500k to +$500k", lambda b: b["bet_dir"] == -1, lambda b: -500_000 <= b["net_flow_60s"] <= 500_000),
            ("DOWN-bet, flow < -$500k", lambda b: b["bet_dir"] == -1, lambda b: b["net_flow_60s"] < -500_000)]:
        sub = [b for b in bets if dir_filt(b) and flow_filt(b)]
        s = stats(sub)
        print(f"  {lbl:<35} {fmt(s)}")

    print()
    print("=== H3: aligned-with-flow vs anti-flow bets ===")
    aligned = [b for b in bets
               if (b["bet_dir"] == 1 and b["net_flow_60s"] > 100_000) or
                  (b["bet_dir"] == -1 and b["net_flow_60s"] < -100_000)]
    anti = [b for b in bets
            if (b["bet_dir"] == 1 and b["net_flow_60s"] < -100_000) or
               (b["bet_dir"] == -1 and b["net_flow_60s"] > 100_000)]
    neutral = [b for b in bets
               if abs(b["net_flow_60s"]) <= 100_000]
    print(f"  bet direction ALIGNED with flow sign:   {fmt(stats(aligned))}")
    print(f"  bet direction ANTI with flow sign:      {fmt(stats(anti))}")
    print(f"  neutral flow (±$100k):                  {fmt(stats(neutral))}")

    print()
    print("=== H4: 30s vs 60s window (which is more predictive?) ===")
    print("30s window:")
    for lo, hi, lbl in [(None, -200_000, "<-$200k 30s"),
                        (-200_000, 200_000, "near zero 30s"),
                        (200_000, None, ">+$200k 30s")]:
        sub = [b for b in bets
               if (lo is None or b["net_flow_30s"] >= lo)
               and (hi is None or b["net_flow_30s"] < hi)]
        s = stats(sub)
        print(f"  {lbl:<25} {fmt(s)}")
    print("60s window:")
    for lo, hi, lbl in [(None, -200_000, "<-$200k 60s"),
                        (-200_000, 200_000, "near zero 60s"),
                        (200_000, None, ">+$200k 60s")]:
        sub = [b for b in bets
               if (lo is None or b["net_flow_60s"] >= lo)
               and (hi is None or b["net_flow_60s"] < hi)]
        s = stats(sub)
        print(f"  {lbl:<25} {fmt(s)}")


if __name__ == "__main__":
    main()
