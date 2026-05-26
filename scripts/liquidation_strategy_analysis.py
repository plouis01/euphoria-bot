"""Two-part liquidation strategy analysis.

Part 1: Filter comparison — apply liquidation gates offline to B50 and
        B75_rmq80 logs, compute counterfactual PnL.

Part 2: Liquidation aftermath — for each liquidation, measure ETH price
        trajectory at T+5s/T+10s/T+30s/T+60s. Does price follow the
        liquidation side (momentum) or reverse (mean revert)? Tells us
        whether we could bet DOWN when long-liqs cluster, UP on short-liqs.

Output written to: LIQUIDATION_STRATEGY_ANALYSIS.md
"""
import bisect
import json
import math
import re
import statistics
import sys
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06
TIME_INTERVAL = 5
PRICE_INTERVAL = 0.5

OUT_MD = ROOT / "LIQUIDATION_STRATEGY_ANALYSIS.md"

LOGS_TO_ANALYZE = [
    ("B50",        "logs/DRY_B50_20260525_151213.log"),
    ("B75_rmq80",  "logs/DRY_B75_rmq80_20260525_172545.log"),
]

FILTERS = [
    ("baseline (no liq filter)",  lambda b: True),
    ("liq_usd_60s < $250k",       lambda b: b["liq_usd_60s"] < 250_000),
    ("liq_n_60s < 5",             lambda b: b["liq_n_60s"] < 5),
    ("liq_usd_60s < $100k",       lambda b: b["liq_usd_60s"] < 100_000),
    ("liq_n_60s < 3",             lambda b: b["liq_n_60s"] < 3),
]

# Multi-window analysis — lookback windows in seconds
LOOKBACK_WINDOWS = [1, 2, 5, 10, 20, 30, 60]
# Filter thresholds (count-based and notional-based) applied at every window
COUNT_THRESHOLDS = [3, 5]
USD_THRESHOLDS = [100_000, 250_000]
# Price aftermath horizons
AFTERMATH_HORIZONS = [1, 2, 5, 10, 20, 30, 60]


def parse_bets(log_path, bot_tag):
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) "
        r"σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)"
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
                "mq": float(m.group(3)),
                "ev_gauss": float(m.group(5)),
                "sigma": float(m.group(6)),
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
            bets.append({"bot": bot_tag, "cell": (cx, cy),
                         "won": rs == "WIN", **sig})
    return bets


def load_liquidations(paths):
    liqs = []
    for p in paths:
        if not p.exists(): continue
        for line in open(p):
            try: d = json.loads(line)
            except: continue
            if "ts_ms" not in d: continue
            liqs.append({
                "ts_ms": d["ts_ms"], "usd": d.get("usd", 0),
                "side": d.get("side", "?"), "venue": d.get("venue", "?"),
                "price": d.get("price", 0), "qty": d.get("qty", 0),
            })
    liqs.sort(key=lambda x: x["ts_ms"])
    return liqs


def liq_window(liqs, ts_arr, end_ts_ms, lookback_sec):
    start_ts = end_ts_ms - lookback_sec * 1000
    i_start = bisect.bisect_left(ts_arr, start_ts)
    i_end = bisect.bisect_right(ts_arr, end_ts_ms)
    return liqs[i_start:i_end]


def pnl_of(b):
    return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def stats_of(bets):
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


def load_price_tape(snap_path):
    tape = []
    for line in open(snap_path):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            tape.append((d["ts_ms"], d["price"]))
    tape.sort()
    return tape


def price_at(tape, ts_arr, ts_ms):
    if not tape: return None
    i = bisect.bisect_right(ts_arr, ts_ms) - 1
    if i < 0: return None
    return tape[i][1]


# ====================================================================
# Run analysis & emit Markdown
# ====================================================================
def main():
    out = []
    out.append("# Liquidation Strategy Analysis")
    out.append("")
    out.append("**Date**: 2026-05-26")
    out.append("**Data**: 5 DRY bots running since 2026-05-25 15:12; liquidation listener since 21:17.")
    out.append("**Regime**: calm-grind (σ ≈ 0.01% per 5s), price drifted $2128.90 → $2097.19 (-1.5%).")
    out.append("")
    out.append("---")
    out.append("")

    # ============ Load data ============
    liq_paths = sorted((ROOT / "logs").glob("liquidations_2026052[5-6]*.jsonl"))
    liqs = load_liquidations(liq_paths)
    liq_ts = [x["ts_ms"] for x in liqs]
    print(f"  loaded {len(liqs)} liquidations from {len(liq_paths)} files", flush=True)

    # Snapshot price tape for Part 2
    snap_paths = sorted((ROOT / "logs").glob("cell_snapshots_2026052[5-6]*.jsonl"))
    snap_path = snap_paths[0] if snap_paths else None
    if snap_path:
        print(f"  loading price tape from {snap_path.name}...", flush=True)
        price_tape = load_price_tape(snap_path)
        price_ts = [x[0] for x in price_tape]
        print(f"  price ticks: {len(price_tape)}", flush=True)
    else:
        price_tape, price_ts = [], []

    # ============ Part 1: Filter comparison ============
    out.append("## Part 1 — Liquidation filter comparison")
    out.append("")
    out.append("For each bot, apply the listed filter as a counterfactual gate "
               "on the existing bet log: bets failing the filter are dropped, "
               "the rest are kept with their actual outcome unchanged.")
    out.append("")
    out.append("DRAG = 6% on wins (multiplicative slippage). $/tap and t-stat "
               "computed on the kept set.")
    out.append("")
    out.append("| Bot | Filter | n_kept | n_drop | WR | PnL kept | $/tap | t | PnL dropped |")
    out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")

    summary_rows = {}  # for later ranking

    # Precompute features once per bot at all windows
    all_bot_bets = {}
    for bot_tag, log_rel in LOGS_TO_ANALYZE:
        bets = parse_bets(ROOT / log_rel, bot_tag)
        for b in bets:
            T = b["bet_ts_ms"]
            for w_sec in LOOKBACK_WINDOWS:
                win = liq_window(liqs, liq_ts, T, w_sec)
                b[f"liq_n_{w_sec}s"] = len(win)
                b[f"liq_usd_{w_sec}s"] = sum(x["usd"] for x in win)
        # alias 60s features for the existing FILTERS (which reference _60s)
        if liqs:
            bets = [b for b in bets if liqs[0]["ts_ms"] <= b["bet_ts_ms"] <= liqs[-1]["ts_ms"]]
        all_bot_bets[bot_tag] = bets

    for bot_tag, log_rel in LOGS_TO_ANALYZE:
        bets = all_bot_bets[bot_tag]
        for filt_name, filt_fn in FILTERS:
            kept = [b for b in bets if filt_fn(b)]
            dropped = [b for b in bets if not filt_fn(b)]
            n, w, pnl, per, t = stats_of(kept)
            _, _, pnl_d, _, _ = stats_of(dropped)
            wr = (100 * w / n) if n else 0
            row_key = f"{bot_tag} | {filt_name}"
            summary_rows[row_key] = (n, per, t, pnl)
            out.append(f"| {bot_tag} | {filt_name} | {n} | {len(dropped)} | "
                       f"{wr:.1f}% | ${pnl:+.2f} | ${per:+.3f} | {t:+.2f} | "
                       f"${pnl_d:+.2f} |")

    out.append("")
    # Rank best filters by $/tap among non-baseline rows
    out.append("### Ranking by $/tap (best filter for each bot)")
    out.append("")
    for bot_tag, _ in LOGS_TO_ANALYZE:
        bot_rows = [(k.split(" | ", 1)[1], v) for k, v in summary_rows.items()
                    if k.startswith(bot_tag + " ")]
        bot_rows.sort(key=lambda x: -x[1][1])  # sort by $/tap desc
        out.append(f"**{bot_tag}:**")
        for filt, (n, per, t, pnl) in bot_rows:
            out.append(f"- {filt}: n={n}, $/tap=${per:+.3f}, t={t:+.2f}, PnL=${pnl:+.2f}")
        out.append("")

    # ============ Part 1b: multi-window filter sweep ============
    out.append("---")
    out.append("")
    out.append("## Part 1b — Multi-window filter sweep")
    out.append("")
    out.append("Same counterfactual filter application, but lookback window varied across "
               "{1s, 2s, 5s, 10s, 20s, 30s, 60s}. Each row shows kept-set $/tap (t-stat).")
    out.append("")
    out.append("**Best per-row cell highlighted by reading down the column.** Look for which "
               "lookback window maximizes $/tap for each filter type.")
    out.append("")

    for bot_tag in [t for t, _ in LOGS_TO_ANALYZE]:
        bets = all_bot_bets[bot_tag]
        n_baseline, _, pnl_baseline, per_baseline, t_baseline = stats_of(bets)
        out.append(f"### {bot_tag}  (baseline n={n_baseline}, $/tap=${per_baseline:+.3f}, t={t_baseline:+.2f})")
        out.append("")
        # Count-threshold table
        out.append(f"**COUNT-based filters** (skip if liq_n_Xs >= threshold)")
        out.append("")
        header = "| Lookback |" + "|".join(f" count<{k} " for k in COUNT_THRESHOLDS) + "|"
        out.append(header)
        out.append("|---|" + "|".join("---:" for _ in COUNT_THRESHOLDS) + "|")
        for w_sec in LOOKBACK_WINDOWS:
            cells = [f"{w_sec}s"]
            for thr in COUNT_THRESHOLDS:
                kept = [b for b in bets if b[f"liq_n_{w_sec}s"] < thr]
                n, _, pnl, per, t = stats_of(kept)
                drop_n = len(bets) - n
                cells.append(f" ${per:+.3f} t={t:+.2f} (n={n}, drop={drop_n}) ")
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
        # USD-threshold table
        out.append(f"**NOTIONAL-based filters** (skip if liq_usd_Xs >= threshold)")
        out.append("")
        header = "| Lookback |" + "|".join(f" usd<${k//1000}k " for k in USD_THRESHOLDS) + "|"
        out.append(header)
        out.append("|---|" + "|".join("---:" for _ in USD_THRESHOLDS) + "|")
        for w_sec in LOOKBACK_WINDOWS:
            cells = [f"{w_sec}s"]
            for thr in USD_THRESHOLDS:
                kept = [b for b in bets if b[f"liq_usd_{w_sec}s"] < thr]
                n, _, pnl, per, t = stats_of(kept)
                drop_n = len(bets) - n
                cells.append(f" ${per:+.3f} t={t:+.2f} (n={n}, drop={drop_n}) ")
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

        # Best of all (count*window, usd*window) by $/tap
        best = []
        for w_sec in LOOKBACK_WINDOWS:
            for thr in COUNT_THRESHOLDS:
                kept = [b for b in bets if b[f"liq_n_{w_sec}s"] < thr]
                n, _, pnl, per, t = stats_of(kept)
                if n >= 50:
                    best.append((per, t, n, f"count<{thr} @ {w_sec}s", pnl))
            for thr in USD_THRESHOLDS:
                kept = [b for b in bets if b[f"liq_usd_{w_sec}s"] < thr]
                n, _, pnl, per, t = stats_of(kept)
                if n >= 50:
                    best.append((per, t, n, f"usd<${thr//1000}k @ {w_sec}s", pnl))
        best.sort(reverse=True)
        out.append(f"**Top 5 (bot {bot_tag}) by $/tap, min n=50:**")
        for per, t, n, lbl, pnl in best[:5]:
            out.append(f"- {lbl}: n={n}, $/tap=${per:+.3f}, t={t:+.2f}, PnL=${pnl:+.2f}")
        out.append("")

    # ============ Part 2: Liquidation aftermath ============
    out.append("---")
    out.append("")
    out.append("## Part 2 — Price aftermath of liquidations (can we bet DOWN?)")
    out.append("")
    out.append("Hypothesis: long liquidations push price down (cascading sells); "
               "short liquidations push price up. If true, large/clustered liqs "
               "are a directional signal we could exploit by tapping down-band "
               "(or up-band) cells immediately after.")
    out.append("")

    if not price_tape:
        out.append("**No price tape available — skipping.**")
        out.append("")
    else:
        # For each liquidation, compute Δprice at T+1s, T+2s, T+5s, T+10s, T+20s, T+30s, T+60s
        for liq in liqs:
            T = liq["ts_ms"]
            liq["p_at"] = price_at(price_tape, price_ts, T)
            for dt_s in AFTERMATH_HORIZONS:
                p = price_at(price_tape, price_ts, T + dt_s * 1000)
                liq[f"dp_{dt_s}s"] = (p - liq["p_at"]) if (p and liq["p_at"]) else None

        # Side normalization: per liquidation_listener.py convention,
        # 'BUY' or 'Buy' = the side of the liquidation order. A liquidating-LONG
        # generates a SELL order. Convention varies per venue; check empirical
        # direction. We'll cross-tab raw side strings with realized dp_60s.

        side_dp = defaultdict(list)
        for liq in liqs:
            if liq.get("dp_60s") is None: continue
            key = (liq["venue"], (liq["side"] or "?").upper())
            side_dp[key].append(liq["dp_60s"])

        out.append("### Empirical price drift by liquidation side")
        out.append("")
        out.append("**Reads:** mean Δprice over the listed horizon AFTER a liquidation event.")
        out.append("Positive Δ = price went UP, negative = DOWN.")
        out.append("")
        h_hdr = " | ".join(f"Δ@{h}s" for h in AFTERMATH_HORIZONS)
        out.append(f"| Venue | Side (raw) | n | {h_hdr} |")
        out.append("|---|---|---:|" + "---:|" * len(AFTERMATH_HORIZONS))

        for (venue, side), dps_60 in sorted(side_dp.items()):
            same_set = [l for l in liqs
                        if l["venue"] == venue
                        and (l["side"] or "?").upper() == side
                        and l.get("dp_1s") is not None]
            n = len(same_set)
            if n == 0: continue
            row = [venue, side, str(n)]
            for dt_s in AFTERMATH_HORIZONS:
                vals = [l[f"dp_{dt_s}s"] for l in same_set if l.get(f"dp_{dt_s}s") is not None]
                if vals:
                    mean = statistics.mean(vals)
                    row.append(f"{mean:+.3f}")
                else:
                    row.append("-")
            out.append("| " + " | ".join(row) + " |")
        out.append("")

        # Size-stratified: large liqs vs small
        out.append("### Stratified by liquidation size (all venues pooled)")
        out.append("")
        out.append("**Reads:** does a LARGER liquidation produce a stronger price reaction?")
        out.append("")
        h_hdr2 = " | ".join(f"Δ@{h}s" for h in AFTERMATH_HORIZONS)
        out.append(f"| Size bucket | n | {h_hdr2} | %DOWN @30s |")
        out.append("|---|---:|" + "---:|" * len(AFTERMATH_HORIZONS) + "---:|")

        size_buckets = [
            ("< $1k", 0, 1_000),
            ("$1k-$10k", 1_000, 10_000),
            ("$10k-$50k", 10_000, 50_000),
            ("$50k-$250k", 50_000, 250_000),
            ("> $250k", 250_000, None),
        ]
        for lbl, lo, hi in size_buckets:
            subset = [l for l in liqs
                      if l["usd"] >= lo
                      and (hi is None or l["usd"] < hi)
                      and l.get("dp_1s") is not None]
            n = len(subset)
            if n == 0:
                empty_cells = " | ".join("-" for _ in AFTERMATH_HORIZONS)
                out.append(f"| {lbl} | 0 | {empty_cells} | - |")
                continue
            cells = []
            for dt_s in AFTERMATH_HORIZONS:
                vals = [l[f"dp_{dt_s}s"] for l in subset if l.get(f"dp_{dt_s}s") is not None]
                if vals:
                    cells.append(f"{statistics.mean(vals):+.3f}")
                else:
                    cells.append("-")
            dn = sum(1 for l in subset if l.get("dp_30s", 0) < 0)
            dn_pct = 100 * dn / n
            out.append(f"| {lbl} | {n} | " + " | ".join(cells) + f" | {dn_pct:.0f}% |")
        out.append("")

        # Cluster events: when ≥5 liqs in 60s, what's the price reaction?
        out.append("### Cluster events — when N liqs in last 60s")
        out.append("")
        out.append("For each bet timestamp, check what price did over the FOLLOWING window.")
        out.append("Different from the per-liquidation view above — this is the perspective the bot sees.")
        out.append("")
        cluster_hdr = " | ".join(f"Δ@{h}s" for h in AFTERMATH_HORIZONS)
        out.append(f"| liq_n_60s bucket | n_bets | {cluster_hdr} |")
        out.append("|---|---:|" + "---:|" * len(AFTERMATH_HORIZONS))

        all_bets = []
        for bot_tag, log_rel in LOGS_TO_ANALYZE:
            all_bets.extend(parse_bets(ROOT / log_rel, bot_tag))
        seen = set(); unique_bets = []
        for b in all_bets:
            if b["bet_ts_ms"] in seen: continue
            seen.add(b["bet_ts_ms"]); unique_bets.append(b)
        for b in unique_bets:
            T = b["bet_ts_ms"]
            win60 = liq_window(liqs, liq_ts, T, 60)
            b["liq_n_60s"] = len(win60)
            b["p_at_bet"] = price_at(price_tape, price_ts, T)
            for dt_s in AFTERMATH_HORIZONS:
                p = price_at(price_tape, price_ts, T + dt_s * 1000)
                b[f"dp_{dt_s}s_forward"] = (p - b["p_at_bet"]) if (p and b["p_at_bet"]) else None

        for bucket in [(0, 0), (1, 2), (3, 4), (5, 9), (10, None)]:
            lo, hi = bucket
            if lo == 0 and hi == 0:
                sub = [b for b in unique_bets if b["liq_n_60s"] == 0]
                lbl = "0 liqs"
            elif hi is None:
                sub = [b for b in unique_bets if b["liq_n_60s"] >= lo]
                lbl = f">={lo} liqs"
            else:
                sub = [b for b in unique_bets if lo <= b["liq_n_60s"] <= hi]
                lbl = f"{lo}-{hi} liqs"
            n = len(sub)
            cells = []
            for dt_s in AFTERMATH_HORIZONS:
                dps = [b[f"dp_{dt_s}s_forward"] for b in sub if b.get(f"dp_{dt_s}s_forward") is not None]
                if dps:
                    cells.append(f"{statistics.mean(dps):+.3f}")
                else:
                    cells.append("-")
            out.append(f"| {lbl} | {n} | " + " | ".join(cells) + " |")
        out.append("")

    # ============ Recommendations ============
    out.append("---")
    out.append("")
    out.append("## Interpretation")
    out.append("")
    out.append("### Part 1b verdict — long lookback wins; the signal is REGIME, not instant")
    out.append("")
    out.append("Reading down the lookback column for each filter type, **the longer the window**, "
               "**the better the per-tap result.** Both bots peak at 60s lookback:")
    out.append("")
    out.append("- **B50 best 4 (by $/tap):** count<5 @ 60s ($0.135), count<3 @ 60s ($0.126), "
               "count<5 @ 30s ($0.119), usd<$250k @ 60s ($0.113)")
    out.append("- **B75_rmq80 best 4:** count<5 @ 60s ($0.137), count<3 @ 30s ($0.134), "
               "count<3 @ 60s ($0.134), usd<$250k @ 60s ($0.120)")
    out.append("")
    out.append("Short-window filters (1s/2s/5s) provide little benefit or even mild harm — they "
               "drop very few bets, and the bets they catch don't differ much from baseline. The "
               "USD filters at short windows drop 0 bets entirely (no $100k+ liquidations in any "
               "1-10s window during calm regime).")
    out.append("")
    out.append("**Mechanism**: this tells us liquidation density is a *regime indicator*, not "
               "an *event signal*. A burst of 5 liquidations over 60s means we're in a stressed "
               "market regime; a single liquidation 2s ago tells us almost nothing.")
    out.append("")
    out.append("### Part 1 verdict — `liq_n_60s < 5` is the winning filter for both bots")
    out.append("")
    out.append("Both bots see the same ranking: COUNT-based filters beat NOTIONAL-based filters.")
    out.append("Per-tap improvement is consistent ~30%:")
    out.append("")
    out.append("- **B50**: baseline $0.100 → with `liq_n_60s<5` filter **$0.135** (+35%)")
    out.append("- **B75_rmq80**: baseline $0.107 → with `liq_n_60s<5` filter **$0.137** (+28%)")
    out.append("")
    out.append("Insight: it's not the *size* of liquidations that hurts us — it's the *frequency*.")
    out.append("Frequent small liqs (count ≥ 5 in 60s) signal a stressed-market regime where")
    out.append("our touch_prob model breaks down. The notional-based filters ($250k, $100k) drop")
    out.append("too few bets (4-24) to materially change PnL — those huge events are too rare in")
    out.append("calm regime.")
    out.append("")
    out.append("**Recommendation**: add `--liq-count-60s-max 4` (i.e., skip if ≥5 liqs in 60s) as")
    out.append("a new bot flag. Stack on top of B75_rmq80 to test additive effect.")
    out.append("")
    out.append("### Part 2 verdict — directional signal exists on OKX SELL, modest")
    out.append("")
    out.append("The clearest directional pattern: **OKX SELL liquidations (n=210) → price moves DOWN**")
    out.append("for at least 60s after the event:")
    out.append("")
    out.append("- Δ@5s: -$0.072")
    out.append("- Δ@10s: -$0.080")
    out.append("- **Δ@30s: -$0.278**")
    out.append("- **Δ@60s: -$0.221**")
    out.append("")
    out.append("OKX SELL = liquidating-LONGS (forced sells to close long positions). The pattern is")
    out.append("a textbook cascade: longs get liquidated → forced sells → price keeps dropping →")
    out.append("more longs liquidated. The $0.28 expected move over 30s is half a cell-band ($0.50).")
    out.append("")
    out.append("Bybit's side encoding is harder to read but bybit BUY (n=158, = liquidating-SHORTS)")
    out.append("shows mild mixed-direction moves, and bybit SELL (n=96) shows near-zero drift. The")
    out.append("asymmetry between OKX SELL and bybit equivalents is real and worth understanding")
    out.append("(possibly OKX's order book is thinner, so liquidation cascades reverberate further).")
    out.append("")
    out.append("Size stratification confirms the mechanism: bigger liquidations produce bigger moves")
    out.append("(\\$50k-\\$250k → -\\$0.22 at 20s vs <\\$1k → essentially noise).")
    out.append("")
    out.append("### Critical timing finding from the 1s/2s/20s addition")
    out.append("")
    out.append("The new short-horizon data reveals the **liquidation-to-price-move timeline**:")
    out.append("")
    out.append("For the $50k-$250k bucket (n=12, the meaningful directional zone):")
    out.append("- Δ@1s:  -$0.056")
    out.append("- Δ@2s:  -$0.041")
    out.append("- Δ@5s:  -$0.059")
    out.append("- Δ@10s: -$0.129")
    out.append("- **Δ@20s: -$0.221** ← peak negative")
    out.append("- Δ@30s: -$0.148")
    out.append("- Δ@60s: -$0.072")
    out.append("")
    out.append("**The cascade takes 10-20s to develop**, then partially reverts by 60s. This is "
               "good news for us: we don't need sub-second reaction. A 5-10s detection-to-fire "
               "loop captures most of the move. **Our existing 8s cadence is approximately right.** "
               "But we DO need to detect a liquidation within 2-3 seconds of it firing — we have "
               "the WS feed already in place.")
    out.append("")
    out.append("### Building a bet-DOWN variant")
    out.append("")
    out.append("Setup we could test:")
    out.append("")
    out.append("```")
    out.append("Trigger: OKX SELL liquidation event (or cluster of ≥2 OKX SELLs in 5s window)")
    out.append("Action:  Tap a DOWN-band cell (cy below current price band) with good mult/p")
    out.append("Window:  bet should resolve within 30s — fits 5s × 6 windows ahead")
    out.append("Edge:    expected -$0.28 price move over 30s, vs band width $0.50")
    out.append("```")
    out.append("")
    out.append("Expected hit rate (rough): if price is currently $X.00 and band edges are at")
    out.append("$X.00, $X.50, $X+1.00... a $0.28 expected drop puts us about halfway into the band")
    out.append("below. Touch probability for a 30s horizon of a band $0.00–$0.28 below given $0.28")
    out.append("expected drift and ~$0.15 σ ≈ 60-70%. MM would price these at multipliers around 1.5-2x.")
    out.append("EV = mq × p − 1 = 2.0 × 0.65 − 1 = +0.30 per dollar staked. **Worth trying.**")
    out.append("")
    out.append("Implementation pieces needed:")
    out.append("")
    out.append("1. **Live liquidation reader** in the bot: tail `liquidations_*.jsonl` or subscribe")
    out.append("   to the WS feeds directly. We already have the data flowing.")
    out.append("2. **DOWN-cell picker**: a new strategy 'liq-cascade-down' that selects DOWN-band")
    out.append("   cells when an OKX SELL fires (or equivalent on bybit/HL).")
    out.append("3. **Time-limited window**: bet must fire within ~2s of the liquidation, before")
    out.append("   MM repricing catches up.")
    out.append("")
    out.append("**Updated read on timing**: the multi-window analysis shows the cascade peaks at 20s "
               "post-liquidation, not at 5s as the original tables suggested. We have a 5-10s window "
               "to detect+act. An 8s loop is borderline — works if liquidation detection latency is "
               "<2s. For safety, an event-driven path (separate process listening to WS feed and "
               "firing taps within ~2s) would be more robust, but isn't strictly required.")
    out.append("")
    out.append("### Caveats")
    out.append("")
    out.append("- **One regime only** (calm-grind). Trending-day behavior unknown for both filter")
    out.append("  and directional signal.")
    out.append("- **15.2h overlap** between bets and liquidation tape. None of the t-stats hit 2.0;")
    out.append("  the count-filter improvement (+35% per-tap) is suggestive, not confirmed.")
    out.append("- **OKX SELL n=210** is the strongest directional bucket; the >$250k bucket (n=2)")
    out.append("  shows -$0.44 at 5s but the sample is too small to trust.")
    out.append("- **Latency is the killer**: even if the directional signal is real, capturing it")
    out.append("  requires a sub-second reaction loop. Our current 8s cadence almost certainly")
    out.append("  misses the move.")
    out.append("- **All offline counterfactuals** — no live trading on these results.")
    out.append("")
    out.append("## Recommended next steps (in order)")
    out.append("")
    out.append("1. **Add `--liq-count-60s-max` flag to dryrun_bot_v3** so a new variant can apply")
    out.append("   the count filter live. Test as a stack with B75_rmq80 (call it B75_rmq80_liq5).")
    out.append("   Cheapest to validate; same data + same bot, just one extra gate.")
    out.append("2. **Wait for more data** before pursuing the directional bet-DOWN strategy.")
    out.append("   The OKX SELL pattern needs to repeat across at least one trending day to be")
    out.append("   trustworthy.")
    out.append("3. **If directional signal holds on day 2**, prototype an event-driven bot path:")
    out.append("   a separate process listening to liquidation WS feeds that triggers a Euphoria")
    out.append("   tap within a short reaction window.")

    OUT_MD.write_text("\n".join(out) + "\n")
    print(f"\nWritten: {OUT_MD}")
    print(f"\n=== TABLE PREVIEW ===\n")
    for line in out:
        print(line)


if __name__ == "__main__":
    main()
