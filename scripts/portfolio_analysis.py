"""Portfolio analysis — does running multiple bot configs in parallel
produce greater PnL than running just the best one?

Key insight: parallel bots only help if they fire on DIFFERENT cells.
If two bots have heavily overlapping bet sets, running both is the same
as running one with double stake (no diversification benefit).

This script:
1. Computes the actual bet set for each profitable filter from v2/v3
2. Measures cell overlap between filters (Jaccard similarity)
3. Simulates running all bots in parallel, $1 stake each
4. Compares: single best bot vs portfolio of bots
5. Reports total PnL, $/total-stake, drawdown
"""
import bisect, json, math, re, datetime
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5
DAY_BOUNDARY_MS = 1779753600000


def parse_bets(log_path):
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)")
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    for line in open(log_path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {"mq": float(m.group(3)), "sigma": float(m.group(6)),
                              "p_eu": float(m.group(7)), "drift": float(m.group(9)),
                              "bet_ts_ms": (cx * TIME_INTERVAL - 30) * 1000}
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


def main():
    print("Loading...")
    all_bets = {}
    for p in sorted(glob(str(ROOT / "logs/DRY_*_2026052[5-7]*.log"))):
        for b in parse_bets(p):
            key = (b["bet_ts_ms"], b["cell"])
            if key not in all_bets: all_bets[key] = b
    bets = sorted(all_bets.values(), key=lambda b: b["bet_ts_ms"])

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

    prints = []
    for p in sorted(glob(str(ROOT / "logs/large_prints_2026052[5-7]*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
                if d.get("ts_ms") and d.get("usd", 0) >= 10_000:
                    prints.append({"ts_ms": d["ts_ms"], "usd": d["usd"],
                                   "side": (d.get("side") or "").upper(),
                                   "venue": d.get("venue", "")})
            except: pass
    prints.sort(key=lambda x: x["ts_ms"])
    p_ts = [x["ts_ms"] for x in prints]

    # Enrich
    for b in bets:
        T = b["bet_ts_ms"]
        i0 = bisect.bisect_left(liq_ts, T - 60_000)
        i1 = bisect.bisect_right(liq_ts, T)
        b["liq_n_60s"] = i1 - i0
        b["liq_okx_sell_60s"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "okx" and liqs[i]["side"] == "SELL")
        i010 = bisect.bisect_left(liq_ts, T - 10_000)
        b["liq_n_10s"] = i1 - i010
        i0p = bisect.bisect_left(p_ts, T - 60_000)
        i1p = bisect.bisect_right(p_ts, T)
        buy = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["side"] == "BUY")
        sell = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["side"] == "SELL")
        b["net_flow_60s"] = buy - sell
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        b["bet_dir"] = 1 if band_mid > b["p_eu"] else -1
        b["cell_dist"] = abs(band_mid - b["p_eu"]) / PRICE_INTERVAL
        dt = datetime.datetime.fromtimestamp(T / 1000)
        b["hour"] = dt.hour
        b["day"] = "day1" if T < DAY_BOUNDARY_MS else "day2"

    # ====== Define candidate filters (the 6 both-days-positive + variations) ======
    # The v2 base
    v2 = lambda b: (b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and
                    b["cell_dist"] < 2 and (b["hour"] < 6 or b["hour"] >= 19))

    # Variations on v2 to try as portfolio members
    candidate_filters = {
        "v2_winner": v2,
        "v2_no_recent_liq": lambda b: v2(b) and b["liq_n_10s"] == 0,
        "v2_no_60s_liq": lambda b: v2(b) and b["liq_n_60s"] == 0,
        "v2_neutral_flow": lambda b: v2(b) and -200_000 <= b["net_flow_60s"] <= 200_000,
        "v2_no_okx_sell": lambda b: v2(b) and b["liq_okx_sell_60s"] == 0,
        # Wider variations to add diversification candidates
        "UP_mult4_6_dist2_no_hour": lambda b: b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and b["cell_dist"] < 2,
        "UP_mult3_5_dist2_hours": lambda b: b["bet_dir"] == 1 and 3 <= b["mq"] < 5 and b["cell_dist"] < 2 and (b["hour"] < 6 or b["hour"] >= 19),
        "UP_mult5_7_dist2_hours": lambda b: b["bet_dir"] == 1 and 5 <= b["mq"] < 7 and b["cell_dist"] < 2 and (b["hour"] < 6 or b["hour"] >= 19),
        "UP_mult4_6_dist1_hours": lambda b: b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and b["cell_dist"] < 1 and (b["hour"] < 6 or b["hour"] >= 19),
        "UP_mult4_6_dist3_hours": lambda b: b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and b["cell_dist"] < 3 and (b["hour"] < 6 or b["hour"] >= 19),
        # Different time windows
        "UP_v2_hours_06_18": lambda b: b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and b["cell_dist"] < 2 and 6 <= b["hour"] < 18,
    }

    # Compute bet set for each filter
    print("Computing per-filter bet sets...")
    bet_sets = {}
    for name, fn in candidate_filters.items():
        cells_kept = {(b["bet_ts_ms"], b["cell"]): b for b in bets if fn(b)}
        bet_sets[name] = cells_kept

    out = []
    out.append("# Portfolio Analysis — Multiple Bots in Parallel?")
    out.append("")
    out.append(f"**Unique bets**: {len(bets)}")
    out.append("")
    out.append("**Question**: does running multiple bot configs in parallel produce")
    out.append("greater PnL than the single best bot?")
    out.append("")
    out.append("**Logic**:")
    out.append("- If each filter fires on a $1 stake, two bots betting same cell = $2 staked on same outcome")
    out.append("- Two bots betting *different* cells = real diversification → independent bets")
    out.append("- Portfolio PnL = sum over all cells of (# filters firing on that cell × cell PnL)")
    out.append("- Total stake = sum over all cells of (# filters firing × $1)")
    out.append("- $/total-stake = total PnL / total stake (effective return per dollar deployed)")
    out.append("")
    out.append("---")
    out.append("")

    # ====== Individual filter stats ======
    out.append("## Individual filter performance")
    out.append("")
    out.append("| Filter | n_bets | total PnL | $/tap | day1 $/tap | day2 $/tap |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for name, bs in bet_sets.items():
        bs_list = list(bs.values())
        n = len(bs_list)
        if n == 0: continue
        pnl = sum(pnl_of(b) for b in bs_list)
        per = pnl / n
        d1 = [b for b in bs_list if b["day"] == "day1"]
        d2 = [b for b in bs_list if b["day"] == "day2"]
        d1_per = sum(pnl_of(b) for b in d1) / len(d1) if d1 else 0
        d2_per = sum(pnl_of(b) for b in d2) / len(d2) if d2 else 0
        out.append(f"| {name} | {n} | ${pnl:+.2f} | ${per:+.3f} | ${d1_per:+.3f}(n={len(d1)}) | ${d2_per:+.3f}(n={len(d2)}) |")
    out.append("")

    # ====== Overlap matrix (Jaccard) ======
    out.append("## Bet-set overlap (Jaccard similarity)")
    out.append("")
    out.append("1.00 = identical bet set; 0.00 = no shared cells")
    out.append("")
    names = list(bet_sets.keys())
    out.append("| | " + " | ".join(n[:20] for n in names) + " |")
    out.append("|---|" + "---|" * len(names))
    for n1 in names:
        keys1 = set(bet_sets[n1].keys())
        row = [n1[:30]]
        for n2 in names:
            keys2 = set(bet_sets[n2].keys())
            if not keys1 or not keys2:
                row.append("—")
            else:
                jac = len(keys1 & keys2) / len(keys1 | keys2)
                row.append(f"{jac:.2f}")
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # ====== Portfolio simulation ======
    out.append("## Portfolio simulation — running N bots in parallel, $1 stake each")
    out.append("")
    out.append("`total_pnl` = sum of per-cell PnL × number of filters firing on that cell")
    out.append("`total_stake` = sum of number of filters firing on each cell")
    out.append("`$/stake` = total_pnl / total_stake (effective per-dollar return)")
    out.append("`n_unique_cells` = distinct cells receiving at least one bet")
    out.append("")
    out.append("| Portfolio | total_pnl | total_stake | $/stake | n_unique_cells | max_DD | DD/pnl |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")

    portfolios = [
        ("only v2_winner", ["v2_winner"]),
        ("only v2_no_recent_liq", ["v2_no_recent_liq"]),
        ("v2_winner + v2_no_recent_liq", ["v2_winner", "v2_no_recent_liq"]),
        ("v2_winner + v2_no_60s_liq", ["v2_winner", "v2_no_60s_liq"]),
        ("v2 + v2_no_recent_liq + v2_neutral_flow", ["v2_winner", "v2_no_recent_liq", "v2_neutral_flow"]),
        ("all 5 v2 variants", ["v2_winner", "v2_no_recent_liq", "v2_no_60s_liq", "v2_neutral_flow", "v2_no_okx_sell"]),
        # Diversification via wider mult ranges
        ("v2 + UP_mult3_5", ["v2_winner", "UP_mult3_5_dist2_hours"]),
        ("v2 + UP_mult5_7", ["v2_winner", "UP_mult5_7_dist2_hours"]),
        ("v2 + UP_mult3_5 + UP_mult5_7", ["v2_winner", "UP_mult3_5_dist2_hours", "UP_mult5_7_dist2_hours"]),
        # Diversification via distance
        ("v2 + UP_dist1 + UP_dist3", ["v2_winner", "UP_mult4_6_dist1_hours", "UP_mult4_6_dist3_hours"]),
        # Time-of-day complement
        ("v2 + UP_daytime", ["v2_winner", "UP_v2_hours_06_18"]),
        # Wide-net portfolio
        ("v2_winner + 4 diversifiers (mult3-5, mult5-7, dist1, dist3)",
            ["v2_winner", "UP_mult3_5_dist2_hours", "UP_mult5_7_dist2_hours",
             "UP_mult4_6_dist1_hours", "UP_mult4_6_dist3_hours"]),
    ]

    for pname, members in portfolios:
        # Compute combined PnL
        cell_firings = defaultdict(int)
        cell_pnls = {}
        for m in members:
            for key, b in bet_sets[m].items():
                cell_firings[key] += 1
                cell_pnls[key] = pnl_of(b)
        total_pnl = sum(cell_firings[k] * cell_pnls[k] for k in cell_firings)
        total_stake = sum(cell_firings.values())
        n_unique = len(cell_firings)
        per_stake = total_pnl / total_stake if total_stake > 0 else 0
        # Time-ordered drawdown
        ordered_keys = sorted(cell_firings.keys(), key=lambda k: k[0])
        cum = 0; peak = 0; mdd = 0
        for k in ordered_keys:
            cum += cell_firings[k] * cell_pnls[k]
            if cum > peak: peak = cum
            if peak - cum > mdd: mdd = peak - cum
        dd_ratio = mdd / total_pnl if total_pnl > 0 else 0
        out.append(f"| {pname[:55]} | ${total_pnl:+.2f} | {total_stake} | ${per_stake:+.4f} | {n_unique} | ${mdd:.1f} | {dd_ratio:.2f}x |")
    out.append("")

    # ====== Day-by-day for top portfolios ======
    out.append("## Day-by-day breakdown for key portfolios")
    out.append("")
    out.append("| Portfolio | day1 PnL | day1 $/stake | day2 PnL | day2 $/stake |")
    out.append("|---|---:|---:|---:|---:|")
    for pname, members in portfolios:
        for day in ["day1", "day2"]:
            pass  # we'll compute below
        cell_firings = defaultdict(int)
        cell_pnls = {}
        cell_day = {}
        for m in members:
            for key, b in bet_sets[m].items():
                cell_firings[key] += 1
                cell_pnls[key] = pnl_of(b)
                cell_day[key] = b["day"]
        d1_pnl = sum(cell_firings[k] * cell_pnls[k] for k in cell_firings if cell_day[k] == "day1")
        d1_stake = sum(cell_firings[k] for k in cell_firings if cell_day[k] == "day1")
        d2_pnl = sum(cell_firings[k] * cell_pnls[k] for k in cell_firings if cell_day[k] == "day2")
        d2_stake = sum(cell_firings[k] for k in cell_firings if cell_day[k] == "day2")
        d1_per = d1_pnl / d1_stake if d1_stake > 0 else 0
        d2_per = d2_pnl / d2_stake if d2_stake > 0 else 0
        out.append(f"| {pname[:55]} | ${d1_pnl:+.2f} (n={d1_stake}) | ${d1_per:+.4f} | ${d2_pnl:+.2f} (n={d2_stake}) | ${d2_per:+.4f} |")
    out.append("")

    # Write
    OUT = ROOT / "PORTFOLIO_ANALYSIS_RESULTS.md"
    OUT.write_text("\n".join(out) + "\n")
    print(f"Written: {OUT}")
    # Print summary
    print()
    print("=== Portfolio summary ===")
    for line in out[20:50]: print(line)


if __name__ == "__main__":
    main()
