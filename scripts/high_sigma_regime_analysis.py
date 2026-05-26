"""High-σ regime analysis on today's (2026-05-26) data.

Yesterday was calm-grind (σ ≈ 0.01% per 5s). Today is trending-down
(σ ≈ 0.03%, price -2.6%). The c30-strong bots are losing across the board.
Question: is there a filter combination that turns today positive, or is the
strategy fundamentally broken in this regime?

Tests:
  1. Today's per-bot PnL stratified by σ bucket
  2. Counterfactual filter sweep on today's B0_baseline:
       - σ-gate (skip if σ > threshold)
       - liq-count gate (already tested, redo on today)
       - rmq filter (we know this is inverted today)
       - inverse-drift (bet DOWN when c30 says UP, etc.)
       - combinations
  3. Try the "bet DOWN on liquidation cluster" reversal — apply to bets
     during high-liq windows: invert the bot's direction
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

OUT_MD = ROOT / "HIGH_SIGMA_REGIME_ANALYSIS.md"


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
                "drift": float(m.group(9)),
                "bet_ts_ms": (cx * TIME_INTERVAL - 30) * 1000,
                "p_eu": float(m.group(7)),
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
    return f"n={n:>4}  WR={w/n*100:>5.1f}%  pnl=${p:>+8.2f}  $/tap=${per:>+7.3f}  t={t:>+5.2f}"


def main():
    out = []
    out.append("# High-σ Regime Analysis (2026-05-26)")
    out.append("")
    out.append("**Regime**: trending-down. Price $2129.93 → $2073.79 (-2.64%).")
    out.append("**σ (5s, realized)**: 0.0316% (3× yesterday's calm 0.010%).")
    out.append("**All 4 bots losing**. Question: is there a filter or bot configuration")
    out.append("that turns today positive, or is c30-strong fundamentally broken in this regime?")
    out.append("")
    out.append("---")
    out.append("")

    # Load liquidations for cross-reference
    liqs = []
    for p in sorted(glob(str(ROOT / "logs/liquidations_2026052[5-6]*.jsonl"))):
        for line in open(p):
            try:
                d = json.loads(line)
                if "ts_ms" in d:
                    liqs.append({"ts_ms": d["ts_ms"], "usd": d.get("usd", 0),
                                 "side": d.get("side", "?"), "venue": d.get("venue", "?")})
            except: pass
    liqs.sort(key=lambda x: x["ts_ms"])
    liq_ts = [x["ts_ms"] for x in liqs]
    liq_usd = [x["usd"] for x in liqs]

    def liq_n_in_window(end_ts_ms, window_sec):
        start = end_ts_ms - window_sec * 1000
        i_start = bisect.bisect_left(liq_ts, start)
        i_end = bisect.bisect_right(liq_ts, end_ts_ms)
        return i_end - i_start

    def liq_usd_in_window(end_ts_ms, window_sec):
        start = end_ts_ms - window_sec * 1000
        i_start = bisect.bisect_left(liq_ts, start)
        i_end = bisect.bisect_right(liq_ts, end_ts_ms)
        return sum(liq_usd[i_start:i_end])

    # ====== Section 1: today's per-bot stratified by σ ======
    out.append("## 1. Today's PnL stratified by σ")
    out.append("")
    out.append("`σ` value comes from the SIGNAL line itself (bot's realized σ at signal time).")
    out.append("")

    all_today_bets = {}
    for tag in ["B0_baseline", "B50", "B75_rmq80", "B75_rmq80_liq5"]:
        paths = sorted(glob(str(ROOT / f"logs/DRY_{tag}_20260526_*.log")))
        bets = []
        for p in paths:
            bets.extend(parse_bets(p))
        all_today_bets[tag] = bets

    for tag, bets in all_today_bets.items():
        if not bets: continue
        out.append(f"**{tag}**  (total n={len(bets)})")
        out.append("")
        for lo, hi, lbl in [(0, 0.10, "σ < 0.10 (calm-ish)"),
                            (0.10, 0.20, "σ 0.10-0.20"),
                            (0.20, 0.30, "σ 0.20-0.30"),
                            (0.30, 0.50, "σ 0.30-0.50"),
                            (0.50, None, "σ > 0.50 (very high)")]:
            sub = [b for b in bets if lo <= b["sigma"]
                   and (hi is None or b["sigma"] < hi)]
            s = stats(sub)
            out.append(f"- {lbl:<25} {fmt(s)}")
        out.append("")

    # ====== Section 2: counterfactual filters on B0_baseline ======
    out.append("---")
    out.append("")
    out.append("## 2. Counterfactual filters on B0_baseline (today only)")
    out.append("")
    out.append("Apply offline filters to B0_baseline's 1,430 bets to see which would")
    out.append("turn today's losses around.")
    out.append("")

    b0 = all_today_bets.get("B0_baseline", [])
    # Enrich with liq features
    for b in b0:
        b["liq_n_60s"] = liq_n_in_window(b["bet_ts_ms"], 60)
        b["liq_usd_60s"] = liq_usd_in_window(b["bet_ts_ms"], 60)
        b["liq_n_30s"] = liq_n_in_window(b["bet_ts_ms"], 30)

    out.append("| Filter | n_kept | n_drop | $/tap kept | t |")
    out.append("|---|---:|---:|---:|---:|")
    base_stats = stats(b0)
    out.append(f"| baseline (no filter) | {base_stats[0]} | 0 | ${base_stats[3]:+.3f} | {base_stats[4]:+.2f} |")

    filters = [
        ("σ < 0.15", lambda b: b["sigma"] < 0.15),
        ("σ < 0.20", lambda b: b["sigma"] < 0.20),
        ("σ < 0.30", lambda b: b["sigma"] < 0.30),
        ("σ > 0.30 (counter)", lambda b: b["sigma"] > 0.30),
        ("σ > 0.50 (counter)", lambda b: b["sigma"] > 0.50),
        ("liq_n_60s < 5", lambda b: b["liq_n_60s"] < 5),
        ("liq_n_60s < 3", lambda b: b["liq_n_60s"] < 3),
        ("liq_n_30s < 2", lambda b: b["liq_n_30s"] < 2),
        ("liq_usd_60s < $100k", lambda b: b["liq_usd_60s"] < 100_000),
        ("liq_usd_60s < $250k", lambda b: b["liq_usd_60s"] < 250_000),
        ("|drift| < 0.20", lambda b: abs(b["drift"]) < 0.20),
        ("|drift| > 0.50", lambda b: abs(b["drift"]) > 0.50),
        ("σ<0.15 AND liq_n_60s<5", lambda b: b["sigma"]<0.15 and b["liq_n_60s"]<5),
        ("σ<0.20 AND liq_n_60s<3", lambda b: b["sigma"]<0.20 and b["liq_n_60s"]<3),
    ]
    for lbl, fn in filters:
        kept = [b for b in b0 if fn(b)]
        n_drop = len(b0) - len(kept)
        s = stats(kept)
        if not s:
            out.append(f"| {lbl} | 0 | {n_drop} | n/a | n/a |")
        else:
            out.append(f"| {lbl} | {s[0]} | {n_drop} | ${s[3]:+.3f} | {s[4]:+.2f} |")
    out.append("")

    # ====== Section 3: Drift-reversal experiment ======
    out.append("---")
    out.append("")
    out.append("## 3. Drift-reversal experiment")
    out.append("")
    out.append("Hypothesis: in a strong trending day, the c30-strong drift-following signal")
    out.append("is actually anti-edge. Test: invert the cell's expected outcome — if the")
    out.append("bot loses on UP bets in a down regime, the down side should win.")
    out.append("")
    out.append("We approximate this by stratifying bet direction (cy above or below P_eu at signal):")
    out.append("")
    out.append("| Direction | n | WR | $/tap | t |")
    out.append("|---|---:|---:|---:|---:|")
    up_bets = []  # bet was on a cell above current price
    dn_bets = []
    for b in b0:
        band_lo = b["cell"][1] * PRICE_INTERVAL
        band_mid = band_lo + PRICE_INTERVAL / 2
        if band_mid > b["p_eu"]:
            up_bets.append(b)
        else:
            dn_bets.append(b)
    for lbl, sub in [("UP-bets (band above P_eu)", up_bets),
                     ("DOWN-bets (band below P_eu)", dn_bets)]:
        s = stats(sub)
        if not s: out.append(f"| {lbl} | 0 | - | - | - |"); continue
        out.append(f"| {lbl} | {s[0]} | {s[1]/s[0]*100:.1f}% | ${s[3]:+.3f} | {s[4]:+.2f} |")
    out.append("")
    out.append("If one side is decisively worse than the other in a single regime, the bot")
    out.append("should be regime-gated by drift direction.")
    out.append("")

    # ====== Section 4: liquidation-direction match ======
    out.append("---")
    out.append("")
    out.append("## 4. Liquidation-direction synergy")
    out.append("")
    out.append("Today's OKX-SELL liquidations (long liqs) preceded average -$0.28 over 30s.")
    out.append("Test: did UP-bets fire during OKX-SELL-heavy windows? If so, they should be skipped.")
    out.append("")
    # For each bet, count OKX-SELL liqs in last 60s
    okx_sell_liqs = [(l["ts_ms"], l["usd"]) for l in liqs
                     if l["venue"] == "okx" and (l["side"] or "").upper() == "SELL"]
    okx_sell_ts = [x[0] for x in okx_sell_liqs]
    for b in b0:
        i_start = bisect.bisect_left(okx_sell_ts, b["bet_ts_ms"] - 60_000)
        i_end = bisect.bisect_right(okx_sell_ts, b["bet_ts_ms"])
        b["okx_sell_n_60s"] = i_end - i_start
    out.append("| Subset | n | WR | $/tap | t |")
    out.append("|---|---:|---:|---:|---:|")
    for lbl, fn in [("UP-bets, 0 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 > b["p_eu"] and b["okx_sell_n_60s"]==0),
                    ("UP-bets, >=1 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 > b["p_eu"] and b["okx_sell_n_60s"]>=1),
                    ("UP-bets, >=3 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 > b["p_eu"] and b["okx_sell_n_60s"]>=3),
                    ("DOWN-bets, 0 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"] and b["okx_sell_n_60s"]==0),
                    ("DOWN-bets, >=1 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"] and b["okx_sell_n_60s"]>=1),
                    ("DOWN-bets, >=3 OKX-SELL in 60s",
                     lambda b: b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"] and b["okx_sell_n_60s"]>=3)]:
        sub = [b for b in b0 if fn(b)]
        s = stats(sub)
        if not s: out.append(f"| {lbl} | 0 | - | - | - |"); continue
        out.append(f"| {lbl} | {s[0]} | {s[1]/s[0]*100:.1f}% | ${s[3]:+.3f} | {s[4]:+.2f} |")
    out.append("")

    # ====== Section 5: combined best filter ======
    out.append("---")
    out.append("")
    out.append("## 5. Best filter combination")
    out.append("")
    # Try a bunch of combinations
    combos = [
        ("σ<0.30 AND DOWN-bets",
         lambda b: b["sigma"]<0.30 and b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"]),
        ("liq_n_60s<3 AND DOWN-bets",
         lambda b: b["liq_n_60s"]<3 and b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"]),
        ("σ<0.20 AND liq_n_60s<3 AND DOWN-bets",
         lambda b: b["sigma"]<0.20 and b["liq_n_60s"]<3 and b["cell"][1]*PRICE_INTERVAL+PRICE_INTERVAL/2 <= b["p_eu"]),
        ("σ<0.20 AND liq_n_60s<3 AND |drift|<0.5",
         lambda b: b["sigma"]<0.20 and b["liq_n_60s"]<3 and abs(b["drift"])<0.5),
    ]
    out.append("| Combo | n_kept | $/tap | t |")
    out.append("|---|---:|---:|---:|")
    for lbl, fn in combos:
        kept = [b for b in b0 if fn(b)]
        s = stats(kept)
        if not s: out.append(f"| {lbl} | 0 | - | - |"); continue
        out.append(f"| {lbl} | {s[0]} | ${s[3]:+.3f} | {s[4]:+.2f} |")
    out.append("")

    # ====== Section 6: Global data inventory ======
    out.append("---")
    out.append("")
    out.append("## 6. Global data inventory")
    out.append("")
    # Count total bets across yesterday + today
    yest_bets = 0
    for p in sorted(glob(str(ROOT / "logs/DRY_*_20260525*.log"))):
        if "_2_" in p: continue  # avoid double-counting
        sig_re_local = re.compile(r"resolve cell=.*RS=(WIN|LOSS)")
        yest_bets += sum(1 for line in open(p) if sig_re_local.search(line))
    today_bets = sum(len(b) for b in all_today_bets.values())
    out.append(f"- **Yesterday's resolved bets (5 bots)**: ~{yest_bets}")
    out.append(f"- **Today's resolved bets (4 bots)**: {today_bets}")
    out.append(f"- **Total across days**: ~{yest_bets + today_bets} resolved bets")
    out.append("")
    # Liquidations
    out.append(f"- **Liquidation events**: {len(liqs)} (bybit+okx+HL backstops, ~24h)")
    # Cell snapshots
    snap_sizes = []
    for p in sorted(glob(str(ROOT / "logs/cell_snapshots_2026052[5-6]*.jsonl"))):
        snap_sizes.append((p, Path(p).stat().st_size / (1024*1024)))
    snap_total = sum(s for _, s in snap_sizes)
    out.append(f"- **Cell snapshots**: {len(snap_sizes)} files, total {snap_total:.0f}MB")
    lat_size = sum(Path(p).stat().st_size for p in glob(str(ROOT / "logs/latency_2026052[5-6]*.jsonl"))) / (1024*1024)
    out.append(f"- **Latency samples**: {lat_size:.1f}MB total")
    out.append("")
    out.append("**Regime coverage**:")
    out.append("- 1 calm-grind day (2026-05-25): σ ≈ 0.010%, price -1.5%")
    out.append("- 1 trending-down day (2026-05-26): σ ≈ 0.032%, price -2.6%")
    out.append("- **0 trending-up days** observed with our infrastructure")
    out.append("- **0 high-vol days** (σ > 0.10%) observed")
    out.append("")
    out.append("**Verdict on data sufficiency**: We have **just enough** to falsify")
    out.append("strong claims (like 'rmq always works' — clearly not), but **not enough**")
    out.append("to validate any signal as universally edge. Every finding so far is")
    out.append("regime-conditional. To call a signal robust we'd need at least:")
    out.append("- 5+ days of each major regime type (calm, trending-up, trending-down, high-vol)")
    out.append("- 5,000+ bets per bot config per regime")
    out.append("- Cross-regime statistical tests (signal sign should be stable or known to flip)")
    out.append("")
    out.append("**Current state: we have ~4,000 bets across 2 days, 2 regimes.** Roughly 10% of what")
    out.append("would be needed for a confident production deploy.")

    OUT_MD.write_text("\n".join(out) + "\n")
    print(f"Written: {OUT_MD}\n")
    for line in out: print(line)


if __name__ == "__main__":
    main()
