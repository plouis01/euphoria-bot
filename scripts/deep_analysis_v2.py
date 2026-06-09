"""Deep analysis v2 — exhaustive search for ANY edge.

v1 tested 26 hand-picked filter combos and found nothing reliable.
v2 broadens the search significantly:

- Time-of-day patterns (which hours are profitable?)
- Mult sub-bins (3-4, 4-5, 5-6, 6-7, 7-8 — narrower than v1's mult<=5)
- σ sub-bins (very fine granularity)
- Counter-strategy: bet opposite the c30-strong direction
- Venue-specific flow (Binance vs Coinbase vs HL)
- Multi-window flow (30s + 60s + 5min)
- Liquidation venue/side specific
- Flow confluence: ALL venues agreeing on direction
- Compound logical filters (e.g., narrow mult × narrow σ × flow agreement)
- High-EV-only cells
- Cell distance from current price
- Inverse outcomes (would betting opposite produce edge?)

Output: DEEP_ANALYSIS_V2_RESULTS.md
"""
import bisect
import json
import math
import re
import statistics
import datetime
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5
DAY_BOUNDARY_MS = 1779753600000  # 2026-05-26 00:00 UTC

OUT_MD = ROOT / "DEEP_ANALYSIS_V2_RESULTS.md"


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
def pnl_inverted(b): return (b["mq"] - 1) * (1 - DRAG) if not b["won"] else -1


def stats_full(bets, pnl_fn=pnl_of):
    n = len(bets)
    if n == 0: return None
    wins = sum(1 for b in bets if b["won"])
    pnls = [pnl_fn(b) for b in bets]
    pnl = sum(pnls); per = pnl / n
    sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5 if n >= 2 else 0
    t = per / sd * math.sqrt(n) if sd > 0 else 0
    sorted_b = sorted(bets, key=lambda b: b["bet_ts_ms"])
    cum = 0; peak = 0; max_dd = 0
    for b in sorted_b:
        cum += pnl_fn(b)
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    return {"n": n, "wins": wins, "wr": wins/n, "pnl": pnl, "per": per,
            "t": t, "max_dd": max_dd, "sd": sd}


def main():
    print("Loading bets...")
    all_bets_by_ts = {}
    for p in sorted(glob(str(ROOT / "logs/DRY_*_2026052[5-7]*.log"))):
        for b in parse_bets(p):
            key = (b["bet_ts_ms"], b["cell"])
            if key not in all_bets_by_ts:
                all_bets_by_ts[key] = b
    bets = sorted(all_bets_by_ts.values(), key=lambda b: b["bet_ts_ms"])
    print(f"  unique bets: {len(bets)}")

    print("Loading liquidations + large prints...")
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
    print("Enriching...")
    for b in bets:
        T = b["bet_ts_ms"]
        # Liquidations
        i0 = bisect.bisect_left(liq_ts, T - 60_000)
        i1 = bisect.bisect_right(liq_ts, T)
        b["liq_n_60s"] = i1 - i0
        b["liq_okx_sell"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "okx" and liqs[i]["side"] == "SELL")
        b["liq_bybit_buy"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "bybit" and liqs[i]["side"] == "BUY")
        # Flow features - per venue
        i0p = bisect.bisect_left(p_ts, T - 60_000)
        i1p = bisect.bisect_right(p_ts, T)
        for venue in ["binance", "coinbase", "hyperliquid"]:
            buy = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["venue"] == venue and prints[i]["side"] == "BUY")
            sell = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["venue"] == venue and prints[i]["side"] == "SELL")
            b[f"flow_{venue}_60s"] = buy - sell
        b["net_flow_60s"] = b["flow_binance_60s"] + b["flow_coinbase_60s"] + b["flow_hyperliquid_60s"]
        # 30s window flow
        i0p30 = bisect.bisect_left(p_ts, T - 30_000)
        buy30 = sum(prints[i]["usd"] for i in range(i0p30, i1p) if prints[i]["side"] == "BUY")
        sell30 = sum(prints[i]["usd"] for i in range(i0p30, i1p) if prints[i]["side"] == "SELL")
        b["net_flow_30s"] = buy30 - sell30
        # Direction
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        b["bet_dir"] = 1 if band_mid > b["p_eu"] else -1
        # Cell distance from current price (in bands)
        b["cell_dist"] = abs(band_mid - b["p_eu"]) / PRICE_INTERVAL
        # Hour of day
        dt = datetime.datetime.fromtimestamp(T / 1000)
        b["hour"] = dt.hour
        # Day
        b["day"] = "day1" if T < DAY_BOUNDARY_MS else "day2"
        # Drift sign
        b["drift_sign"] = 1 if b["drift"] > 0 else -1 if b["drift"] < 0 else 0
        # Flow confluence: do all 3 venues agree?
        signs = [1 if b[f"flow_{v}_60s"] > 50_000 else -1 if b[f"flow_{v}_60s"] < -50_000 else 0
                 for v in ["binance", "coinbase", "hyperliquid"]]
        b["flow_consensus"] = 1 if all(s == 1 for s in signs) else (-1 if all(s == -1 for s in signs) else 0)

    day1_n = sum(1 for b in bets if b["day"] == "day1")
    day2_n = sum(1 for b in bets if b["day"] == "day2")
    print(f"  day1={day1_n} day2={day2_n}")

    # ====== Filter combinations (broader sweep) ======
    filters = []

    # Time-of-day
    for h_lo, h_hi in [(0, 6), (6, 12), (12, 18), (18, 24)]:
        filters.append((f"hour [{h_lo:02d}-{h_hi:02d}) UTC",
                        lambda b, lo=h_lo, hi=h_hi: lo <= b["hour"] < hi))

    # Mult sub-buckets (narrower)
    for lo, hi in [(3, 4), (4, 5), (5, 6), (6, 7), (7, 8)]:
        filters.append((f"mult [{lo}, {hi})",
                        lambda b, l=lo, h=hi: l <= b["mq"] < h))

    # σ sub-buckets (fine)
    for lo, hi in [(0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 0.50)]:
        filters.append((f"σ [{lo:.2f}, {hi:.2f})",
                        lambda b, l=lo, h=hi: l <= b["sigma"] < h))

    # Drift bands
    for lo, hi in [(0.30, 0.40), (0.40, 0.50), (0.50, 0.70), (0.70, 1.00), (1.00, 999)]:
        filters.append((f"|drift| [{lo:.2f}, {hi:.2f})",
                        lambda b, l=lo, h=hi: l <= abs(b["drift"]) < h))

    # Cell distance (bands away from price)
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 999)]:
        filters.append((f"cell_dist [{lo}, {hi}) bands",
                        lambda b, l=lo, h=hi: l <= b["cell_dist"] < h))

    # Venue-specific flow
    for venue in ["binance", "coinbase", "hyperliquid"]:
        filters.append((f"{venue} flow_60s < -200k",
                        lambda b, v=venue: b[f"flow_{v}_60s"] < -200_000))
        filters.append((f"{venue} flow_60s > +200k",
                        lambda b, v=venue: b[f"flow_{v}_60s"] > 200_000))

    # Flow consensus (all 3 venues agreeing)
    filters.append(("flow consensus SELL (all 3 venues)",
                    lambda b: b["flow_consensus"] == -1))
    filters.append(("flow consensus BUY (all 3 venues)",
                    lambda b: b["flow_consensus"] == 1))
    filters.append(("DOWN-bet + flow consensus SELL",
                    lambda b: b["bet_dir"] == -1 and b["flow_consensus"] == -1))
    filters.append(("UP-bet + flow consensus BUY",
                    lambda b: b["bet_dir"] == 1 and b["flow_consensus"] == 1))
    filters.append(("aligned (bet_dir == flow consensus)",
                    lambda b: b["flow_consensus"] != 0 and b["bet_dir"] == b["flow_consensus"]))
    filters.append(("anti (bet_dir != flow consensus, both nonzero)",
                    lambda b: b["flow_consensus"] != 0 and b["bet_dir"] != b["flow_consensus"]))

    # Liquidation venue/side specific
    filters.append(("OKX-SELL liqs >= 3 (long-cascade)",
                    lambda b: b["liq_okx_sell"] >= 3))
    filters.append(("DOWN-bet + OKX-SELL >= 3",
                    lambda b: b["bet_dir"] == -1 and b["liq_okx_sell"] >= 3))
    filters.append(("Bybit-BUY liqs >= 3 (short-cascade)",
                    lambda b: b["liq_bybit_buy"] >= 3))
    filters.append(("UP-bet + Bybit-BUY >= 3",
                    lambda b: b["bet_dir"] == 1 and b["liq_bybit_buy"] >= 3))

    # Multi-window flow
    filters.append(("flow_30s and flow_60s both negative",
                    lambda b: b["net_flow_30s"] < -100_000 and b["net_flow_60s"] < -100_000))
    filters.append(("flow_30s and flow_60s both positive",
                    lambda b: b["net_flow_30s"] > 100_000 and b["net_flow_60s"] > 100_000))
    filters.append(("DOWN-bet + flow_30s<-200k + flow_60s<-200k",
                    lambda b: b["bet_dir"] == -1 and b["net_flow_30s"] < -200_000 and b["net_flow_60s"] < -200_000))

    # High-EV cells only
    filters.append(("ev_gauss >= 1.0",
                    lambda b: b["ev_gauss"] >= 1.0))
    filters.append(("ev_gauss >= 1.5",
                    lambda b: b["ev_gauss"] >= 1.5))
    filters.append(("ev_gauss < 0.5",
                    lambda b: b["ev_gauss"] < 0.5))

    # Compound: best regime gates
    filters.append(("near-money (cell_dist<2) + flow consensus",
                    lambda b: b["cell_dist"] < 2 and b["flow_consensus"] != 0))
    filters.append(("near-money + flow aligned",
                    lambda b: b["cell_dist"] < 2 and b["flow_consensus"] != 0 and b["bet_dir"] == b["flow_consensus"]))
    filters.append(("UP-only + mult [4,6) + cell_dist<2",
                    lambda b: b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and b["cell_dist"] < 2))

    # ====== Run analysis ======
    print(f"Testing {len(filters)} filters...")
    results = []
    for name, fn in filters:
        kept = [b for b in bets if fn(b)]
        all_s = stats_full(kept)
        d1_s = stats_full([b for b in kept if b["day"] == "day1"])
        d2_s = stats_full([b for b in kept if b["day"] == "day2"])
        inv_s = stats_full(kept, pnl_fn=pnl_inverted)
        results.append({"name": name, "all": all_s, "day1": d1_s, "day2": d2_s, "inverted": inv_s})

    # Score function — heavily reward both-day-positive
    def score(r):
        a = r["all"]
        if not a or a["n"] < 80: return -999
        bonus = 1.0
        d1, d2 = r["day1"], r["day2"]
        if d1 and d2 and d1["per"] > 0 and d2["per"] > 0: bonus *= 2.0  # strong bonus
        elif d1 and d2 and (d1["per"] > 0 or d2["per"] > 0): bonus *= 1.0
        else: bonus *= 0.3  # both negative or near-zero = bad
        # Drawdown penalty
        if a["pnl"] > 0:
            dd_ratio = a["max_dd"] / max(a["pnl"], 0.01)
            bonus /= (1 + dd_ratio / 3)
        return a["per"] * math.sqrt(a["n"]) * bonus

    results.sort(key=score, reverse=True)

    # ====== Write report ======
    out = []
    out.append("# Deep Analysis v2 — Exhaustive Search")
    out.append("")
    out.append(f"**Date**: 2026-05-27")
    out.append(f"**Filters tested**: {len(filters)} (vs 26 in v1)")
    out.append(f"**Unique bets**: {len(bets)}  |  day1={day1_n}  day2={day2_n}")
    out.append("")
    out.append("**Score function**: $/tap × sqrt(n) × bonus")
    out.append("  - 2.0× if BOTH days positive (high confidence)")
    out.append("  - 1.0× if ONE day positive")
    out.append("  - 0.3× if both days negative/zero")
    out.append("  - drawdown penalty: divides by (1 + max_dd/(3*pnl))")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Top 30 by score")
    out.append("")
    out.append("| Rank | Filter | n | WR | $/tap | t | max DD | day1 $/tap | day2 $/tap | score |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for i, r in enumerate(results[:30], 1):
        a = r["all"]
        if not a:
            out.append(f"| {i} | {r['name']} | 0 | — | — | — | — | — | — | — |")
            continue
        d1 = r["day1"]; d2 = r["day2"]
        d1_str = f"${d1['per']:+.3f}(n={d1['n']})" if d1 else "n/a"
        d2_str = f"${d2['per']:+.3f}(n={d2['n']})" if d2 else "n/a"
        sc = score(r)
        out.append(f"| {i} | {r['name'][:55]} | {a['n']} | {a['wr']*100:.1f}% | "
                   f"${a['per']:+.3f} | {a['t']:+.2f} | ${a['max_dd']:.2f} | "
                   f"{d1_str} | {d2_str} | {sc:+.2f} |")
    out.append("")

    # ====== Find both-days-positive filters (most important) ======
    out.append("---")
    out.append("")
    out.append("## Filters POSITIVE on BOTH days (n>=50 each)")
    out.append("")
    bdays_pos = [r for r in results
                 if r["day1"] and r["day2"]
                 and r["day1"]["per"] > 0 and r["day2"]["per"] > 0
                 and r["day1"]["n"] >= 50 and r["day2"]["n"] >= 50]
    out.append(f"**Found: {len(bdays_pos)} filters with positive $/tap on both days**")
    out.append("")
    if bdays_pos:
        out.append("| Filter | n_total | $/tap | t | day1 | day2 |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for r in bdays_pos[:20]:
            a = r["all"]; d1 = r["day1"]; d2 = r["day2"]
            out.append(f"| {r['name']} | {a['n']} | ${a['per']:+.3f} | {a['t']:+.2f} | "
                       f"${d1['per']:+.3f}(n={d1['n']}) | ${d2['per']:+.3f}(n={d2['n']}) |")
    else:
        out.append("**None found.** No filter is reliably positive on both days with n>=50 each.")
    out.append("")

    # ====== Counter-strategy check ======
    out.append("---")
    out.append("")
    out.append("## Counter-strategy check (would betting OPPOSITE direction win?)")
    out.append("")
    out.append("If c30-strong has anti-edge, the inverse should have positive edge.")
    out.append("Compute PnL as if we bet OPPOSITE direction (won→loss, loss→won):")
    out.append("")
    out.append("| Subset | n | PnL inverted | $/tap inverted | t |")
    out.append("|---|---:|---:|---:|---:|")
    baseline = [b for b in bets]
    inv_all = stats_full(baseline, pnl_fn=pnl_inverted)
    out.append(f"| baseline (all bets, inverted) | {inv_all['n']} | ${inv_all['pnl']:+.2f} | ${inv_all['per']:+.3f} | {inv_all['t']:+.2f} |")
    for lbl, fn in [("DOWN-only inverted (bet UP instead)", lambda b: b["bet_dir"] == -1),
                    ("UP-only inverted (bet DOWN instead)", lambda b: b["bet_dir"] == 1)]:
        sub = [b for b in bets if fn(b)]
        inv = stats_full(sub, pnl_fn=pnl_inverted)
        out.append(f"| {lbl} | {inv['n']} | ${inv['pnl']:+.2f} | ${inv['per']:+.3f} | {inv['t']:+.2f} |")
    out.append("")
    out.append("Inverted PnL ≠ -baseline because of multiplier asymmetry (a 5× win pays 4× drag,")
    out.append("a loss costs $1; flipping outcomes pays differently). Tests if MIRROR strategy")
    out.append("would profit.")
    out.append("")

    # ====== Hour-of-day matrix ======
    out.append("---")
    out.append("")
    out.append("## Hour-of-day × bet direction")
    out.append("")
    out.append("| Hour | UP-bets n | UP $/tap | DOWN-bets n | DOWN $/tap |")
    out.append("|---|---:|---:|---:|---:|")
    by_hour_dir = defaultdict(lambda: {"up": [], "dn": []})
    for b in bets:
        if b["bet_dir"] == 1: by_hour_dir[b["hour"]]["up"].append(b)
        else: by_hour_dir[b["hour"]]["dn"].append(b)
    for h in sorted(by_hour_dir.keys()):
        up_s = stats_full(by_hour_dir[h]["up"])
        dn_s = stats_full(by_hour_dir[h]["dn"])
        up_str = f"${up_s['per']:+.3f}" if up_s else "n/a"
        dn_str = f"${dn_s['per']:+.3f}" if dn_s else "n/a"
        up_n = up_s["n"] if up_s else 0
        dn_n = dn_s["n"] if dn_s else 0
        out.append(f"| {h:02d} | {up_n} | {up_str} | {dn_n} | {dn_str} |")
    out.append("")

    OUT_MD.write_text("\n".join(out) + "\n")
    print(f"Written: {OUT_MD}")
    print()
    print("=== TOP 15 ===")
    for line in out[14:34]: print(line)
    print()
    print("=== BOTH-DAYS-POSITIVE ===")
    in_section = False
    for line in out:
        if "BOTH days" in line: in_section = True
        if in_section:
            print(line)
            if line.startswith("---") and "POSITIVE" not in line and len(line) < 10: break


if __name__ == "__main__":
    main()
