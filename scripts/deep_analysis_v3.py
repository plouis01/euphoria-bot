"""Deep analysis v3 — liquidations + big trades as primary signals.

v2 found one defensible filter (UP-only + mult [4,6) + cell_dist<2 + hours).
v3 asks: can we layer liquidation/flow features on top to STRENGTHEN it,
and can liquidation/flow features stand alone as primary signals?

Tests:
  A. Augment v2 winner with liq/flow features (stack search)
  B. Pure liquidation-driven strategies
  C. Pure flow-driven strategies (per venue + multi-venue)
  D. Time-of-liquidation effects (liq just BEFORE the bet)
  E. Cross-asset confluence: liq + flow + time of day
  F. Counter-signals: bet DOWN when bybit-BUY cascade (short-squeeze direction)
"""
import bisect
import json
import math
import re
import datetime
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5
DAY_BOUNDARY_MS = 1779753600000

OUT_MD = ROOT / "DEEP_ANALYSIS_V3_RESULTS.md"


def parse_bets(log_path):
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)")
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    for line in open(log_path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "sigma": float(m.group(6)),
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


def stats(bs):
    n = len(bs)
    if n == 0: return None
    wins = sum(1 for b in bs if b["won"])
    pnls = [pnl_of(b) for b in bs]
    pnl = sum(pnls); per = pnl / n
    sd = (sum((p - per) ** 2 for p in pnls) / (n - 1)) ** 0.5 if n >= 2 else 0
    t = per / sd * math.sqrt(n) if sd > 0 else 0
    sorted_b = sorted(bs, key=lambda b: b["bet_ts_ms"])
    cum = 0; peak = 0; mdd = 0
    for b in sorted_b:
        cum += pnl_of(b)
        if cum > peak: peak = cum
        if peak - cum > mdd: mdd = peak - cum
    return {"n": n, "wins": wins, "wr": wins/n, "pnl": pnl, "per": per,
            "t": t, "max_dd": mdd, "sd": sd}


def fmt(s, day=None):
    if not s: return "n=0"
    suffix = f" t={s['t']:+.2f}" if day else ""
    return f"n={s['n']:>4} WR={s['wr']*100:>5.1f}% ${s['per']:>+7.3f}{suffix}"


def main():
    print("Loading bets + market data...")
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

    print(f"  bets: {len(bets)}, liqs: {len(liqs)}, prints: {len(prints)}")

    # Enrich
    print("Enriching...")
    for b in bets:
        T = b["bet_ts_ms"]
        # Liquidations
        i0 = bisect.bisect_left(liq_ts, T - 60_000)
        i1 = bisect.bisect_right(liq_ts, T)
        b["liq_n_60s"] = i1 - i0
        b["liq_okx_sell_60s"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "okx" and liqs[i]["side"] == "SELL")
        b["liq_okx_buy_60s"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "okx" and liqs[i]["side"] == "BUY")
        b["liq_bybit_buy_60s"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "bybit" and liqs[i]["side"] == "BUY")
        b["liq_bybit_sell_60s"] = sum(1 for i in range(i0, i1) if liqs[i]["venue"] == "bybit" and liqs[i]["side"] == "SELL")
        b["liq_usd_60s"] = sum(liqs[i]["usd"] for i in range(i0, i1))
        # 30s
        i030 = bisect.bisect_left(liq_ts, T - 30_000)
        b["liq_n_30s"] = i1 - i030
        # 10s (very recent)
        i010 = bisect.bisect_left(liq_ts, T - 10_000)
        b["liq_n_10s"] = i1 - i010
        b["liq_usd_10s"] = sum(liqs[i]["usd"] for i in range(i010, i1))
        # Flow features per venue
        i0p = bisect.bisect_left(p_ts, T - 60_000)
        i1p = bisect.bisect_right(p_ts, T)
        for venue in ["binance", "coinbase", "hyperliquid"]:
            buy = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["venue"] == venue and prints[i]["side"] == "BUY")
            sell = sum(prints[i]["usd"] for i in range(i0p, i1p) if prints[i]["venue"] == venue and prints[i]["side"] == "SELL")
            b[f"flow_{venue}_60s"] = buy - sell
        b["net_flow_60s"] = b["flow_binance_60s"] + b["flow_coinbase_60s"] + b["flow_hyperliquid_60s"]
        # 30s flow
        i030p = bisect.bisect_left(p_ts, T - 30_000)
        buy30 = sum(prints[i]["usd"] for i in range(i030p, i1p) if prints[i]["side"] == "BUY")
        sell30 = sum(prints[i]["usd"] for i in range(i030p, i1p) if prints[i]["side"] == "SELL")
        b["net_flow_30s"] = buy30 - sell30
        # 10s flow (very recent)
        i010p = bisect.bisect_left(p_ts, T - 10_000)
        buy10 = sum(prints[i]["usd"] for i in range(i010p, i1p) if prints[i]["side"] == "BUY")
        sell10 = sum(prints[i]["usd"] for i in range(i010p, i1p) if prints[i]["side"] == "SELL")
        b["net_flow_10s"] = buy10 - sell10
        # Direction & metadata
        band_mid = b["cell"][1] * PRICE_INTERVAL + PRICE_INTERVAL / 2
        b["bet_dir"] = 1 if band_mid > b["p_eu"] else -1
        b["cell_dist"] = abs(band_mid - b["p_eu"]) / PRICE_INTERVAL
        dt = datetime.datetime.fromtimestamp(T / 1000)
        b["hour"] = dt.hour
        b["day"] = "day1" if T < DAY_BOUNDARY_MS else "day2"

    day1_n = sum(1 for b in bets if b["day"] == "day1")
    day2_n = sum(1 for b in bets if b["day"] == "day2")

    # Helper that tests one filter and returns formatted result rows for table
    def test_filter(name, fn, results_list):
        kept = [b for b in bets if fn(b)]
        s = stats(kept)
        d1 = stats([b for b in kept if b["day"] == "day1"])
        d2 = stats([b for b in kept if b["day"] == "day2"])
        results_list.append({"name": name, "all": s, "day1": d1, "day2": d2})

    # ===== A. AUGMENT v2 WINNER WITH LIQ/FLOW FEATURES =====
    print("\n=== A. Augmenting v2 winner with liq/flow features ===")
    v2_base = lambda b: (b["bet_dir"] == 1 and 4 <= b["mq"] < 6 and
                         b["cell_dist"] < 2 and (b["hour"] < 6 or b["hour"] >= 19))

    a_results = []
    test_filter("[v2 winner] BASE", v2_base, a_results)
    # Add liq filters
    test_filter("v2 + liq_n_60s == 0",
                lambda b: v2_base(b) and b["liq_n_60s"] == 0, a_results)
    test_filter("v2 + liq_n_60s < 3",
                lambda b: v2_base(b) and b["liq_n_60s"] < 3, a_results)
    test_filter("v2 + liq_n_60s >= 3",
                lambda b: v2_base(b) and b["liq_n_60s"] >= 3, a_results)
    test_filter("v2 + liq_n_10s == 0",
                lambda b: v2_base(b) and b["liq_n_10s"] == 0, a_results)
    test_filter("v2 + liq_n_10s >= 1",
                lambda b: v2_base(b) and b["liq_n_10s"] >= 1, a_results)
    test_filter("v2 + liq_okx_sell_60s == 0",
                lambda b: v2_base(b) and b["liq_okx_sell_60s"] == 0, a_results)
    test_filter("v2 + liq_okx_buy_60s >= 1 (short-cover bias)",
                lambda b: v2_base(b) and b["liq_okx_buy_60s"] >= 1, a_results)
    test_filter("v2 + liq_bybit_buy_60s >= 1",
                lambda b: v2_base(b) and b["liq_bybit_buy_60s"] >= 1, a_results)
    # Add flow filters
    test_filter("v2 + net_flow_60s > 0",
                lambda b: v2_base(b) and b["net_flow_60s"] > 0, a_results)
    test_filter("v2 + net_flow_60s > +200k (buy flow aligned)",
                lambda b: v2_base(b) and b["net_flow_60s"] > 200_000, a_results)
    test_filter("v2 + net_flow_60s in [-200k, +200k] (neutral)",
                lambda b: v2_base(b) and -200_000 <= b["net_flow_60s"] <= 200_000, a_results)
    test_filter("v2 + net_flow_60s < -200k (anti-flow)",
                lambda b: v2_base(b) and b["net_flow_60s"] < -200_000, a_results)
    test_filter("v2 + binance flow > 0",
                lambda b: v2_base(b) and b["flow_binance_60s"] > 0, a_results)
    test_filter("v2 + hyperliquid flow > 0",
                lambda b: v2_base(b) and b["flow_hyperliquid_60s"] > 0, a_results)
    test_filter("v2 + coinbase flow > 0",
                lambda b: v2_base(b) and b["flow_coinbase_60s"] > 0, a_results)
    test_filter("v2 + ALL venues flow > 0 (full consensus)",
                lambda b: v2_base(b) and b["flow_binance_60s"] > 0 and b["flow_coinbase_60s"] > 0 and b["flow_hyperliquid_60s"] > 0, a_results)
    test_filter("v2 + ALL venues flow > +50k (strong consensus)",
                lambda b: v2_base(b) and b["flow_binance_60s"] > 50_000 and b["flow_coinbase_60s"] > 50_000 and b["flow_hyperliquid_60s"] > 50_000, a_results)
    # Combo
    test_filter("v2 + liq_n_60s<3 + net_flow_60s>0",
                lambda b: v2_base(b) and b["liq_n_60s"] < 3 and b["net_flow_60s"] > 0, a_results)
    test_filter("v2 + liq_n_60s<3 + ALL venues flow>0",
                lambda b: v2_base(b) and b["liq_n_60s"] < 3 and b["flow_binance_60s"] > 0 and b["flow_coinbase_60s"] > 0 and b["flow_hyperliquid_60s"] > 0, a_results)

    # ===== B. PURE LIQUIDATION-DRIVEN STRATEGIES =====
    print("=== B. Pure liquidation-driven strategies ===")
    b_results = []
    # No direction restriction first
    test_filter("[B] liq_n_60s >= 5 (any direction)",
                lambda b: b["liq_n_60s"] >= 5, b_results)
    test_filter("[B] liq_n_60s >= 10",
                lambda b: b["liq_n_60s"] >= 10, b_results)
    test_filter("[B] liq_n_60s == 0",
                lambda b: b["liq_n_60s"] == 0, b_results)
    # OKX-SELL (long-cascade) + DOWN bet
    test_filter("[B] DOWN-bet + okx_sell >= 1",
                lambda b: b["bet_dir"] == -1 and b["liq_okx_sell_60s"] >= 1, b_results)
    test_filter("[B] DOWN-bet + okx_sell >= 3",
                lambda b: b["bet_dir"] == -1 and b["liq_okx_sell_60s"] >= 3, b_results)
    test_filter("[B] DOWN-bet + okx_sell >= 5",
                lambda b: b["bet_dir"] == -1 and b["liq_okx_sell_60s"] >= 5, b_results)
    # Bybit-BUY (short-cascade) + UP bet
    test_filter("[B] UP-bet + bybit_buy >= 1",
                lambda b: b["bet_dir"] == 1 and b["liq_bybit_buy_60s"] >= 1, b_results)
    test_filter("[B] UP-bet + bybit_buy >= 3",
                lambda b: b["bet_dir"] == 1 and b["liq_bybit_buy_60s"] >= 3, b_results)
    test_filter("[B] UP-bet + bybit_buy >= 5",
                lambda b: b["bet_dir"] == 1 and b["liq_bybit_buy_60s"] >= 5, b_results)
    # Counter-cascade: bet AGAINST the liq direction
    test_filter("[B] UP-bet + okx_sell >= 3 (counter-cascade UP)",
                lambda b: b["bet_dir"] == 1 and b["liq_okx_sell_60s"] >= 3, b_results)
    test_filter("[B] DOWN-bet + bybit_buy >= 3 (counter-cascade DOWN)",
                lambda b: b["bet_dir"] == -1 and b["liq_bybit_buy_60s"] >= 3, b_results)
    # Big liq cluster filters
    test_filter("[B] liq_usd_60s > $100k",
                lambda b: b["liq_usd_60s"] > 100_000, b_results)
    test_filter("[B] liq_usd_60s > $250k",
                lambda b: b["liq_usd_60s"] > 250_000, b_results)
    test_filter("[B] DOWN + liq_usd_60s > $250k",
                lambda b: b["bet_dir"] == -1 and b["liq_usd_60s"] > 250_000, b_results)
    test_filter("[B] UP + liq_usd_60s > $250k",
                lambda b: b["bet_dir"] == 1 and b["liq_usd_60s"] > 250_000, b_results)
    # Recent liq (last 10s, very fresh)
    test_filter("[B] DOWN + liq_okx_sell_60s>=2 + liq_n_10s>=1 (active cascade)",
                lambda b: b["bet_dir"] == -1 and b["liq_okx_sell_60s"] >= 2 and b["liq_n_10s"] >= 1, b_results)

    # ===== C. PURE FLOW-DRIVEN STRATEGIES =====
    print("=== C. Pure flow-driven strategies ===")
    c_results = []
    # Symmetric thresholds
    test_filter("[C] DOWN + net_flow_60s < -200k",
                lambda b: b["bet_dir"] == -1 and b["net_flow_60s"] < -200_000, c_results)
    test_filter("[C] DOWN + net_flow_60s < -500k",
                lambda b: b["bet_dir"] == -1 and b["net_flow_60s"] < -500_000, c_results)
    test_filter("[C] DOWN + net_flow_60s < -1M",
                lambda b: b["bet_dir"] == -1 and b["net_flow_60s"] < -1_000_000, c_results)
    test_filter("[C] UP + net_flow_60s > +200k",
                lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 200_000, c_results)
    test_filter("[C] UP + net_flow_60s > +500k",
                lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 500_000, c_results)
    # Per-venue
    test_filter("[C] DOWN + hyperliquid_flow < -200k",
                lambda b: b["bet_dir"] == -1 and b["flow_hyperliquid_60s"] < -200_000, c_results)
    test_filter("[C] UP + hyperliquid_flow > +200k",
                lambda b: b["bet_dir"] == 1 and b["flow_hyperliquid_60s"] > 200_000, c_results)
    test_filter("[C] DOWN + binance_flow < -200k",
                lambda b: b["bet_dir"] == -1 and b["flow_binance_60s"] < -200_000, c_results)
    # 10s window (very recent)
    test_filter("[C] DOWN + net_flow_10s < -100k",
                lambda b: b["bet_dir"] == -1 and b["net_flow_10s"] < -100_000, c_results)
    test_filter("[C] UP + net_flow_10s > +100k",
                lambda b: b["bet_dir"] == 1 and b["net_flow_10s"] > 100_000, c_results)
    # Multi-window confirmation
    test_filter("[C] UP + flow_30s>+100k + flow_60s>+200k (sustained buy)",
                lambda b: b["bet_dir"] == 1 and b["net_flow_30s"] > 100_000 and b["net_flow_60s"] > 200_000, c_results)
    test_filter("[C] DOWN + flow_30s<-100k + flow_60s<-200k (sustained sell)",
                lambda b: b["bet_dir"] == -1 and b["net_flow_30s"] < -100_000 and b["net_flow_60s"] < -200_000, c_results)

    # ===== D. COMBINED LIQ + FLOW + TIME =====
    print("=== D. Combined liq + flow + time ===")
    d_results = []
    test_filter("[D] UP + flow > +200k + hours [00-06 OR 19-24)",
                lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 200_000 and (b["hour"] < 6 or b["hour"] >= 19), d_results)
    test_filter("[D] UP + flow > +200k + cell_dist<2",
                lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 200_000 and b["cell_dist"] < 2, d_results)
    test_filter("[D] UP + flow > 0 + liq_n_60s<3 + cell_dist<2",
                lambda b: b["bet_dir"] == 1 and b["net_flow_60s"] > 0 and b["liq_n_60s"] < 3 and b["cell_dist"] < 2, d_results)
    test_filter("[D] DOWN + okx_sell>=3 + flow<-200k + cell_dist<2",
                lambda b: b["bet_dir"] == -1 and b["liq_okx_sell_60s"] >= 3 and b["net_flow_60s"] < -200_000 and b["cell_dist"] < 2, d_results)
    test_filter("[D] BIDIR signal-aligned: (UP+flow>0+liq<3) OR (DOWN+flow<-200k+okx_sell>=2)",
                lambda b: (b["bet_dir"]==1 and b["net_flow_60s"]>0 and b["liq_n_60s"]<3) or
                          (b["bet_dir"]==-1 and b["net_flow_60s"]<-200_000 and b["liq_okx_sell_60s"]>=2), d_results)

    # Write out results to MD
    out = []
    out.append("# Deep Analysis v3 — Liquidations + Big Trades as Primary Signals")
    out.append("")
    out.append(f"**Bets**: {len(bets)}  |  day1={day1_n}  day2={day2_n}")
    out.append(f"**Liquidations**: {len(liqs)}  |  **Large prints**: {len(prints)}")
    out.append("")
    out.append("v2 found one defensible filter (UP-only + mult[4,6) + cell_dist<2 + hours).")
    out.append("v3 layers liquidation + flow features to look for stronger edges.")
    out.append("")
    out.append("Each section requires: BOTH days positive with n_per_day ≥ 30 for confidence.")
    out.append("")
    out.append("---")
    out.append("")

    def render_section(title, results, min_n_day=30):
        out.append(f"## {title}")
        out.append("")
        out.append("| Filter | n | WR | $/tap | t | maxDD | day1 | day2 | both+? |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|:---:|")
        for r in results:
            a = r["all"]
            if not a or a["n"] < 30:
                out.append(f"| {r['name'][:60]} | {a['n'] if a else 0} | — | — | — | — | — | — | — |")
                continue
            d1 = r["day1"]; d2 = r["day2"]
            d1s = f"${d1['per']:+.3f}(n={d1['n']})" if d1 else "n/a"
            d2s = f"${d2['per']:+.3f}(n={d2['n']})" if d2 else "n/a"
            both_pos = "✓" if (d1 and d2 and d1["per"] > 0 and d2["per"] > 0 and d1["n"] >= min_n_day and d2["n"] >= min_n_day) else ""
            out.append(f"| {r['name'][:60]} | {a['n']} | {a['wr']*100:.1f}% | ${a['per']:+.3f} | {a['t']:+.2f} | ${a['max_dd']:.1f} | {d1s} | {d2s} | {both_pos} |")
        out.append("")

    render_section("A. Augmenting v2 winner with liquidations + flow", a_results)
    render_section("B. Pure liquidation-driven strategies", b_results)
    render_section("C. Pure flow-driven strategies", c_results)
    render_section("D. Combined liq + flow + time", d_results)

    # Collect ALL results and find best both-days-positive
    all_results = a_results + b_results + c_results + d_results
    bdays = [r for r in all_results
             if r["day1"] and r["day2"]
             and r["day1"]["per"] > 0 and r["day2"]["per"] > 0
             and r["day1"]["n"] >= 30 and r["day2"]["n"] >= 30]
    bdays.sort(key=lambda r: r["all"]["per"], reverse=True)

    out.append("---")
    out.append("")
    out.append("## ALL FILTERS POSITIVE ON BOTH DAYS (n ≥ 30 per day)")
    out.append("")
    out.append(f"**Found: {len(bdays)} filters with cross-day stability**")
    out.append("")
    if bdays:
        out.append("| Filter | n | $/tap | t | day1 | day2 |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for r in bdays[:25]:
            a = r["all"]; d1 = r["day1"]; d2 = r["day2"]
            out.append(f"| {r['name']} | {a['n']} | ${a['per']:+.3f} | {a['t']:+.2f} | ${d1['per']:+.3f}(n={d1['n']}) | ${d2['per']:+.3f}(n={d2['n']}) |")
        out.append("")

    OUT_MD.write_text("\n".join(out) + "\n")
    print(f"Written: {OUT_MD}")
    print()
    if bdays:
        print(f"=== {len(bdays)} BOTH-DAYS-POSITIVE filters found ===")
        for r in bdays[:10]:
            a = r["all"]; d1 = r["day1"]; d2 = r["day2"]
            print(f"  {r['name'][:60]:<60} n={a['n']:>4} $/tap=${a['per']:+.3f} t={a['t']:+.2f}  day1 ${d1['per']:+.3f}(n={d1['n']}) day2 ${d2['per']:+.3f}(n={d2['n']})")
    else:
        print("No new both-days-positive filters beyond v2 winner.")


if __name__ == "__main__":
    main()
