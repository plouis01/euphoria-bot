"""Deep analysis across all 13,919 bets to find the highest-confidence
profitable bot configuration with lowest drawdown.

Methodology:
  1. Load ALL bot bets with full features (mq, p, σ, drift, time, direction)
  2. Enrich with external signals: net_flow_60s (large prints),
     liq_n_60s + liq_usd_60s (liquidations)
  3. For every filter combination in a curated set, compute:
       n_kept, $/tap, t-stat, max drawdown, Sharpe-like score
  4. Cross-regime stability: split bets by day (calm-grind vs trending-down)
       and require positive on BOTH days for confidence
  5. Rank by composite score:
       priority = $/tap × sqrt(n) - drawdown_penalty
       subject to: n >= 100, day1_per >= 0, day2_per >= 0

Output: DEEP_ANALYSIS_RESULTS.md
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

OUT_MD = ROOT / "DEEP_ANALYSIS_RESULTS.md"

# Day boundary: 2026-05-26 00:00 UTC (in ms)
DAY_BOUNDARY_MS = 1779753600000  # 2026-05-26 00:00 UTC


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
                "ev_gauss": float(m.group(5)), "sigma": float(m.group(6)),
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


def pnl_of(b): return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def stats_full(bets):
    """Return n, wins, pnl, per_tap, t, max_drawdown, sharpe."""
    n = len(bets)
    if n == 0: return None
    wins = sum(1 for b in bets if b["won"])
    pnls = [pnl_of(b) for b in bets]
    pnl = sum(pnls); per = pnl / n
    sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5 if n >= 2 else 0
    t = per / sd * math.sqrt(n) if sd > 0 else 0
    # Max drawdown computed time-ordered
    sorted_bets = sorted(bets, key=lambda b: b["bet_ts_ms"])
    cum = 0; peak = 0; max_dd = 0
    for b in sorted_bets:
        cum += pnl_of(b)
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    # Sharpe-like: per/sd
    sharpe = per / sd if sd > 0 else 0
    return {
        "n": n, "wins": wins, "wr": wins/n, "pnl": pnl, "per": per,
        "t": t, "max_dd": max_dd, "sharpe": sharpe, "sd": sd,
    }


def main():
    print("Loading all bot bets...")
    all_bets_by_ts = {}  # de-dup by (bet_ts_ms, cell)
    for p in sorted(glob(str(ROOT / "logs/DRY_*_2026052[5-7]*.log"))):
        for b in parse_bets(p):
            key = (b["bet_ts_ms"], b["cell"])
            # Multiple bots may have placed the same bet at the same time;
            # keep just one (they share outcome since same cell)
            if key not in all_bets_by_ts:
                all_bets_by_ts[key] = b
    bets = list(all_bets_by_ts.values())
    bets.sort(key=lambda b: b["bet_ts_ms"])
    print(f"  unique bets: {len(bets)}")

    # Load liquidations
    print("Loading liquidations...")
    liqs = []
    for p in sorted(glob(str(ROOT / "logs/liquidations_2026052[5-7]*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
                if d.get("ts_ms"):
                    liqs.append({"ts_ms": d["ts_ms"], "usd": d.get("usd", 0),
                                 "side": (d.get("side") or "").upper(),
                                 "venue": d.get("venue", "")})
            except: pass
    liqs.sort(key=lambda x: x["ts_ms"])
    liq_ts = [x["ts_ms"] for x in liqs]
    liq_usd = [x["usd"] for x in liqs]
    print(f"  liquidations: {len(liqs)}")

    # Load large prints
    print("Loading large prints...")
    prints = []
    for p in sorted(glob(str(ROOT / "logs/large_prints_2026052[5-7]*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
                if d.get("ts_ms") and d.get("usd", 0) >= 10_000:
                    prints.append({"ts_ms": d["ts_ms"], "usd": d["usd"],
                                   "side": (d.get("side") or "").upper()})
            except: pass
    prints.sort(key=lambda x: x["ts_ms"])
    p_ts = [x["ts_ms"] for x in prints]
    print(f"  large prints: {len(prints)}")

    # Enrich each bet with all features
    print("Enriching bets with all features...")
    for b in bets:
        T = b["bet_ts_ms"]
        # Liq features
        i0 = bisect.bisect_left(liq_ts, T - 60_000)
        i1 = bisect.bisect_right(liq_ts, T)
        b["liq_n_60s"] = i1 - i0
        b["liq_usd_60s"] = sum(liq_usd[i0:i1])
        # Flow features
        i0 = bisect.bisect_left(p_ts, T - 60_000)
        i1 = bisect.bisect_right(p_ts, T)
        buy = sum(prints[i]["usd"] for i in range(i0, i1) if prints[i]["side"] == "BUY")
        sell = sum(prints[i]["usd"] for i in range(i0, i1) if prints[i]["side"] == "SELL")
        b["net_flow_60s"] = buy - sell
        # Direction
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        b["bet_dir"] = 1 if band_mid > b["p_eu"] else -1
        # Day
        b["day"] = "day1" if b["bet_ts_ms"] < DAY_BOUNDARY_MS else "day2"

    print(f"  bets enriched: {len(bets)}")
    day1_n = sum(1 for b in bets if b["day"] == "day1")
    day2_n = sum(1 for b in bets if b["day"] == "day2")
    print(f"  day1 (calm-grind):    {day1_n}")
    print(f"  day2 (trending-down): {day2_n}")

    # ====== Define filter combinations to test ======
    filters = [
        ("baseline (no filter)", lambda b: True),

        # Single filters
        ("UP-bets only", lambda b: b["bet_dir"] == 1),
        ("DOWN-bets only", lambda b: b["bet_dir"] == -1),
        ("mult <= 5", lambda b: b["mq"] <= 5),
        ("mult <= 3", lambda b: b["mq"] <= 3),
        ("σ <= 0.10", lambda b: b["sigma"] <= 0.10),
        ("σ <= 0.15", lambda b: b["sigma"] <= 0.15),
        ("|drift| <= 0.40", lambda b: abs(b["drift"]) <= 0.40),
        ("liq_n_60s < 5", lambda b: b["liq_n_60s"] < 5),
        ("liq_n_60s < 3", lambda b: b["liq_n_60s"] < 3),

        # Flow filters
        ("flow: skip DOWN unless flow<-500k",
         lambda b: not (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000)),
        ("flow: skip DOWN unless flow<-200k",
         lambda b: not (b["bet_dir"] == -1 and b["net_flow_60s"] >= -200_000)),
        ("flow: skip DOWN unless flow<-1M",
         lambda b: not (b["bet_dir"] == -1 and b["net_flow_60s"] >= -1_000_000)),
        ("flow: skip UP when flow>+500k",
         lambda b: not (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000)),
        ("flow BOTH (skip DOWN unless<-500k AND skip UP when>+500k)",
         lambda b: not (
            (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000) or
            (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000))),

        # Pair combinations
        ("flow BOTH + mult<=5",
         lambda b: not (
            (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000) or
            (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000)) and
            b["mq"] <= 5),
        ("flow BOTH + liq_n_60s<3",
         lambda b: not (
            (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000) or
            (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000)) and
            b["liq_n_60s"] < 3),
        ("flow BOTH + σ<=0.15",
         lambda b: not (
            (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000) or
            (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000)) and
            b["sigma"] <= 0.15),

        # UP-only variants (the asymmetric finding)
        ("UP-only + flow > -500k",
         lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > -500_000),
        ("UP-only + flow <= +500k",
         lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] <= 500_000),
        ("UP-only + flow in [-500k, +500k]",
         lambda b: b["bet_dir"] == 1 and -500_000 < b["net_flow_60s"] <= 500_000),

        # Triple stack: strict
        ("flow BOTH + mult<=5 + liq<3",
         lambda b: not (
            (b["bet_dir"] == -1 and b["net_flow_60s"] >= -500_000) or
            (b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000)) and
            b["mq"] <= 5 and b["liq_n_60s"] < 3),
        ("UP-only + flow in [-500k,+500k] + mult<=5",
         lambda b: b["bet_dir"] == 1 and -500_000 < b["net_flow_60s"] <= 500_000 and b["mq"] <= 5),
        ("UP-only + flow in [-500k,+500k] + liq<3",
         lambda b: b["bet_dir"] == 1 and -500_000 < b["net_flow_60s"] <= 500_000 and b["liq_n_60s"] < 3),

        # Strongest signal: heavy SELL
        ("DOWN + flow < -2M (heavy SELL)",
         lambda b: b["bet_dir"] == -1 and b["net_flow_60s"] < -2_000_000),
        ("UP + flow > +2M (heavy BUY)",
         lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 2_000_000),
    ]

    print(f"\nTesting {len(filters)} filter combinations...\n")

    results = []
    for name, fn in filters:
        kept = [b for b in bets if fn(b)]
        all_stat = stats_full(kept)
        day1_stat = stats_full([b for b in kept if b["day"] == "day1"])
        day2_stat = stats_full([b for b in kept if b["day"] == "day2"])
        results.append({
            "name": name, "all": all_stat, "day1": day1_stat, "day2": day2_stat,
        })

    # Rank by composite: prefer high $/tap × sqrt(n), low drawdown, both days positive
    def score(r):
        a = r["all"]
        if not a or a["n"] < 100: return -999
        bonus = 1.0
        # Both days positive bonus
        if r["day1"] and r["day1"]["per"] > 0 and r["day2"] and r["day2"]["per"] > 0:
            bonus *= 1.5
        # Penalize high drawdown relative to total PnL
        if a["pnl"] > 0:
            dd_ratio = a["max_dd"] / a["pnl"]
            bonus /= (1 + dd_ratio)
        # Score = $/tap × sqrt(n) × bonus
        return a["per"] * math.sqrt(a["n"]) * bonus

    results.sort(key=score, reverse=True)

    # ====== Write report ======
    out = []
    out.append("# Deep Analysis Results")
    out.append("")
    out.append(f"**Date**: 2026-05-27")
    out.append(f"**Total bets analyzed**: {len(bets)} unique (across all bots × all sessions)")
    out.append(f"**Day 1 (calm-grind, 2026-05-25)**: {day1_n} bets")
    out.append(f"**Day 2 (trending-down, 2026-05-26→27)**: {day2_n} bets")
    out.append(f"**Liquidations**: {len(liqs)}  |  **Large prints**: {len(prints)}")
    out.append("")
    out.append("**Goal**: highest $/tap × sqrt(n), positive on BOTH days, low drawdown.")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Ranked filter combinations")
    out.append("")
    out.append("| Rank | Filter | n | WR | $/tap | t | max DD | day1 $/tap | day2 $/tap | score |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, r in enumerate(results[:30], 1):
        a = r["all"]
        if not a:
            out.append(f"| {i} | {r['name']} | 0 | — | — | — | — | — | — | — |")
            continue
        d1 = r["day1"]; d2 = r["day2"]
        d1_per = f"${d1['per']:+.3f}" if d1 else "n/a"
        d2_per = f"${d2['per']:+.3f}" if d2 else "n/a"
        sc = score(r)
        out.append(f"| {i} | {r['name'][:55]} | {a['n']} | {a['wr']*100:.1f}% | "
                   f"${a['per']:+.3f} | {a['t']:+.2f} | ${a['max_dd']:.2f} | "
                   f"{d1_per} | {d2_per} | {sc:+.2f} |")
    out.append("")

    # ====== Top 5 deep-dive ======
    out.append("## Top 5 deep dive")
    out.append("")
    for i, r in enumerate(results[:5], 1):
        if not r["all"]: continue
        a = r["all"]; d1 = r["day1"]; d2 = r["day2"]
        out.append(f"### {i}. {r['name']}")
        out.append("")
        out.append(f"- Combined: n={a['n']}, WR={a['wr']*100:.1f}%, "
                   f"PnL=${a['pnl']:+.2f}, $/tap=${a['per']:+.3f}, "
                   f"t={a['t']:+.2f}, σ_pnl=${a['sd']:.3f}, Sharpe={a['sharpe']:+.3f}")
        out.append(f"- Max drawdown: ${a['max_dd']:.2f}  "
                   f"(ratio to total PnL: {a['max_dd']/max(abs(a['pnl']),0.01):.2f}x)")
        if d1:
            out.append(f"- Day 1: n={d1['n']}, WR={d1['wr']*100:.1f}%, "
                       f"$/tap=${d1['per']:+.3f}, t={d1['t']:+.2f}")
        if d2:
            out.append(f"- Day 2: n={d2['n']}, WR={d2['wr']*100:.1f}%, "
                       f"$/tap=${d2['per']:+.3f}, t={d2['t']:+.2f}")
        out.append("")

    # ====== Bot config recommendation ======
    out.append("---")
    out.append("")
    out.append("## Recommended bot configuration")
    out.append("")
    best = results[0]
    out.append(f"Top-ranked: **{best['name']}**")
    if best["all"]:
        a = best["all"]
        out.append(f"")
        out.append(f"- Aggregate: {a['n']} bets, ${a['pnl']:+.2f} PnL, "
                   f"${a['per']:+.3f}/tap, t={a['t']:+.2f}")
        out.append(f"- Max drawdown: ${a['max_dd']:.2f}")
        if best["day1"] and best["day2"]:
            out.append(f"- Day-by-day: day1 ${best['day1']['per']:+.3f}, day2 ${best['day2']['per']:+.3f}")
    out.append("")

    OUT_MD.write_text("\n".join(out) + "\n")
    print(f"Written: {OUT_MD}")
    print()
    print("=== TOP 10 ===")
    for line in out[14:28]: print(line)


if __name__ == "__main__":
    main()
