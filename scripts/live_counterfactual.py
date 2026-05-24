"""Build LIVE_RUN_F4K4_ANALYSIS.md.

Includes:
- All 6 findings from live_deep_analysis (re-computed)
- For each filterable finding (mult, |drift|, q_age, distance): show how it changes
  the other findings' distributions
- All 16 combinations of the 4 filterable findings
"""
import re, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "LIVE_F4k4_20260523_230147.log"
TIME_INTERVAL = 5
STARTING = 21.8917

SIGNAL = re.compile(
    r"\[LIVE_F4k4\] \[bot\] SIGNAL strat=\S+ cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"mult=(?P<mult>[\d.]+) p=(?P<p>[\d.]+) ev=\+?(?P<ev>-?[\d.]+) "
    r"σ=(?P<sigma>[\d.]+) P_eu=\$(?P<eu>[\d.]+) P_cb=\$(?P<cb>\S+) "
    r"drift_cb=\$(?P<drift>[+-][\d.]+) q_age=(?P<qage>-?\d+)ms"
)
SETTLED = re.compile(
    r"\[LIVE_F4k4\] \[bot\] LIVE settled tradeId=\S+ "
    r"cell=\((?P<cx>\d+),(?P<cy>\d+)\) mult=(?P<mult>[\d.]+) "
    r"outcome=(?P<outcome>won|lost) pnl=\$(?P<pnl>[+-]?[\d.]+) "
    r"bal=\$\S+ w/l=(?P<w>\d+)/(?P<l>\d+) realized=\$(?P<realized>[+-]?[\d.]+)"
)


def parse():
    signals = {}
    settlements = []
    for line in open(LOG):
        m = SIGNAL.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            signals[(cx, cy)] = {
                "mult_signal": float(m.group("mult")),
                "p_pred": float(m.group("p")),
                "sigma": float(m.group("sigma")),
                "drift": float(m.group("drift")),
                "p_eu": float(m.group("eu")),
                "qage": int(m.group("qage")),
            }
            continue
        m = SETTLED.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            sig = signals.get((cx, cy), {})
            band_center = cy * 0.5 + 0.25
            dist = abs(band_center - sig.get("p_eu", 0)) if sig else None
            s = {
                "cx": cx, "cy": cy,
                "mult_filled": float(m.group("mult")),
                "won": m.group("outcome") == "won",
                "ts": cx * TIME_INTERVAL,
                "dist": dist,
                **sig,
            }
            settlements.append(s)
    return settlements


def stats(bets):
    n = len(bets)
    if n == 0:
        return {"n": 0, "wr": 0, "pnl": 0, "per_tap": 0, "top3_pct": 0,
                "t_stat": 0, "wins": 0}
    wins = sum(1 for b in bets if b["won"])
    pnls = [(b["mult_filled"] - 1.0) if b["won"] else -1.0 for b in bets]
    cum = sum(pnls)
    pnls_sorted = sorted(pnls, reverse=True)
    top3 = sum(pnls_sorted[:3])
    mean_p = cum / n
    var = sum((p - mean_p) ** 2 for p in pnls) / max(n - 1, 1)
    sd = var ** 0.5
    t = (mean_p / sd * (n ** 0.5)) if sd > 0 else 0
    return {"n": n, "wr": 100*wins/n, "pnl": cum, "per_tap": cum/n,
            "top3_pct": (100*top3/cum) if cum > 0 else (0 if cum == 0 else -100*abs(top3)/cum),
            "t_stat": t, "wins": wins}


# ---- Filter functions ----
FILTERS = {
    "M8":   ("mult ≤ 8",       lambda b: b["mult_filled"] <= 8),
    "D40":  ("|drift| ≤ 0.40", lambda b: b.get("drift") is None or abs(b["drift"]) <= 0.40),
    "Q50":  ("q_age ≥ 50ms",   lambda b: b.get("qage") is None or b["qage"] >= 50),
    "C1":   ("|cell-P| ≤ $1",  lambda b: b.get("dist") is None or b["dist"] <= 1.0),
}


def apply_filters(bets, keys):
    out = bets
    for k in keys:
        _, fn = FILTERS[k]
        out = [b for b in out if fn(b)]
    return out


def fmt_stat_row(label, s):
    return (f"| {label} | {s['n']} | {s['wr']:.1f}% | ${s['pnl']:+.2f} "
            f"| ${s['per_tap']:+.3f} | {s['top3_pct']:.0f}% | {s['t_stat']:+.2f} |")


def header():
    print("# LIVE F4k4 — Counterfactual Filter Analysis")
    print("")
    print("Source: `LIVE_F4k4_20260523_230147.log` (151 bets, 22.5% WR, +$29.69, top 3 = 136% of PnL)")
    print("")
    print("**Goal**: each of the 6 findings from the deep analysis suggested a filter. This doc tests:")
    print("1. What each filter does in isolation")
    print("2. How applying one filter changes the OTHER findings' distributions")
    print("3. All 16 combinations of the 4 actionable filters")
    print("")
    print("**Note on F4 (Finding 1) and calibration (Finding 3)**: these aren't simple per-bet filters on the existing 151 bets.")
    print("- F4 changes which bets would have been TAKEN — can't simulate from existing data.")
    print("- Calibration miscalibration → tightens future EV thresholds, but this run already filtered by ev≥0.05.")
    print("Both are recorded as recommendations but not counterfactual-simulated here. The 4 simulable filters are:")
    print("")
    for k, (desc, _) in FILTERS.items():
        print(f"- **{k}**: {desc}")
    print("")


def section_individual(bets):
    print("## 1. Individual filter impact")
    print("")
    print("Apply ONE filter at a time, see all 6 findings re-computed.")
    print("")
    baseline = stats(bets)
    print("### Baseline (no filters)")
    print("")
    print("| Filter | n | WR | PnL | $/tap | Top3 % | t-stat |")
    print("|---|---|---|---|---|---|---|")
    print(fmt_stat_row("none (151 bets)", baseline))
    print("")
    for fk, (desc, _) in FILTERS.items():
        filtered = apply_filters(bets, [fk])
        s = stats(filtered)
        print(f"### Filter {fk} only ({desc})")
        print("")
        print(f"Bets remaining: {s['n']} of 151")
        print("")
        print("| | n | WR | PnL | $/tap | Top3 % | t-stat |")
        print("|---|---|---|---|---|---|---|")
        print(fmt_stat_row(f"{fk}", s))
        print("")
        # Now show OTHER findings' dists on this subset
        print(f"#### Sub-distributions under {fk}")
        print("")
        # Mult
        if fk != "M8":
            print("Mult distribution:")
            print("")
            print("| Mult range | n | WR | $/tap |")
            print("|---|---|---|---|")
            for lo, hi in [(1,5), (5,8), (8,12), (12,20), (20,50), (50,999)]:
                sub = [b for b in filtered if lo <= b["mult_filled"] < hi]
                if not sub: continue
                ss = stats(sub)
                print(f"| [{lo},{hi}) | {ss['n']} | {ss['wr']:.1f}% | ${ss['per_tap']:+.3f} |")
            print("")
        # Drift
        if fk != "D40":
            print("|drift| distribution:")
            print("")
            print("| \|drift\| range | n | WR | $/tap |")
            print("|---|---|---|---|")
            for lo, hi in [(0,0.20), (0.20,0.30), (0.30,0.40), (0.40,0.55), (0.55,99)]:
                sub = [b for b in filtered if b.get("drift") is not None and lo <= abs(b["drift"]) < hi]
                if not sub: continue
                ss = stats(sub)
                print(f"| [{lo:.2f},{hi:.2f}) | {ss['n']} | {ss['wr']:.1f}% | ${ss['per_tap']:+.3f} |")
            print("")
        # q_age
        if fk != "Q50":
            print("q_age distribution:")
            print("")
            print("| q_age range (ms) | n | WR | $/tap |")
            print("|---|---|---|---|")
            for lo, hi in [(-1000,0), (0,100), (100,250), (250,500), (500,9999)]:
                sub = [b for b in filtered if b.get("qage") is not None and lo <= b["qage"] < hi]
                if not sub: continue
                ss = stats(sub)
                print(f"| [{lo},{hi}) | {ss['n']} | {ss['wr']:.1f}% | ${ss['per_tap']:+.3f} |")
            print("")
        # Distance
        if fk != "C1":
            print("|cell-P_eu| distribution:")
            print("")
            print("| distance ($) | n | WR | $/tap |")
            print("|---|---|---|---|")
            for lo, hi in [(0,0.5), (0.5,1.0), (1.0,2.0), (2.0,99)]:
                sub = [b for b in filtered if b.get("dist") is not None and lo <= b["dist"] < hi]
                if not sub: continue
                ss = stats(sub)
                print(f"| [{lo:.1f},{hi:.1f}) | {ss['n']} | {ss['wr']:.1f}% | ${ss['per_tap']:+.3f} |")
            print("")
        # p_pred calibration
        print("p_pred calibration:")
        print("")
        print("| p_pred bin | n | predicted (avg) | actual WR | gap |")
        print("|---|---|---|---|---|")
        for lo, hi in [(0,0.2), (0.2,0.3), (0.3,0.4), (0.4,0.5), (0.5,1.0)]:
            sub = [b for b in filtered if b.get("p_pred") is not None and lo <= b["p_pred"] < hi]
            if not sub: continue
            pred_avg = sum(b["p_pred"] for b in sub) / len(sub)
            actual = sum(1 for b in sub if b["won"]) / len(sub)
            print(f"| [{lo:.1f},{hi:.1f}) | {len(sub)} | {pred_avg:.3f} | {actual:.3f} | {pred_avg-actual:+.3f} |")
        print("")


def section_all_combinations(bets):
    print("## 2. All 16 combinations of the 4 filters")
    print("")
    print("Each row is one combination. Sorted by $/tap descending. Filters: M8=mult≤8, D40=|drift|≤0.40, Q50=q_age≥50ms, C1=|cell-P|≤$1.")
    print("")
    print("| Filters applied | n | WR | PnL | $/tap | Top 3 % | t-stat |")
    print("|---|---|---|---|---|---|---|")
    rows = []
    keys = list(FILTERS.keys())
    for mask in range(16):
        active = [k for i, k in enumerate(keys) if mask & (1 << i)]
        sub = apply_filters(bets, active)
        s = stats(sub)
        label = "—" if not active else "+".join(active)
        rows.append((label, s, len(active)))
    # Sort by per_tap desc, but keep n≥10 for meaningful comparison
    rows.sort(key=lambda r: (-r[1]["per_tap"], -r[1]["n"]))
    for label, s, n_filt in rows:
        marker = " ◀ BEST" if rows[0][0] == label else ""
        print(f"| {label} | {s['n']} | {s['wr']:.1f}% | ${s['pnl']:+.2f} | ${s['per_tap']:+.3f} | {s['top3_pct']:.0f}% | {s['t_stat']:+.2f}{marker} |")
    print("")
    print("**Reading the table:**")
    print("- `n` is how many of the 151 bets pass all active filters")
    print("- `Top 3 %` < 100% means edge is genuinely distributed; > 100% means jackpot-dependent")
    print("- `t-stat` > 2.0 is statistically significant in this sample size")
    print("")


def section_winner(bets):
    print("## 3. Optimal combination")
    print("")
    keys = list(FILTERS.keys())
    best = None
    best_score = -1e9
    for mask in range(16):
        active = [k for i, k in enumerate(keys) if mask & (1 << i)]
        sub = apply_filters(bets, active)
        s = stats(sub)
        # Compound score: per_tap × sqrt(n) penalizes tiny samples
        # AND require top3 < 80% (distributed edge)
        if s["n"] < 20: continue
        if s["top3_pct"] > 80: continue
        score = s["per_tap"] * math.sqrt(s["n"])
        if score > best_score:
            best_score = score
            best = (active, s)
    if best:
        active, s = best
        print(f"**Best filter combo (Sharpe-weighted, with n≥20 and top3<80%): {'+'.join(active) if active else 'NONE'}**")
        print("")
        print(f"- Bets retained: **{s['n']} of 151**")
        print(f"- Win rate: **{s['wr']:.1f}%**")
        print(f"- Cum PnL: **${s['pnl']:+.2f}** vs baseline +$29.69")
        print(f"- $/tap: **${s['per_tap']:+.3f}** vs baseline +$0.197 (**{(s['per_tap']/0.197 - 1)*100:+.0f}%** vs baseline)")
        print(f"- Top 3 share of PnL: **{s['top3_pct']:.0f}%** (distributed)")
        print(f"- t-stat: **{s['t_stat']:+.2f}**")
        print("")
        print("### Translation to bot CLI args")
        print("")
        print("```")
        if "M8" in active:  print("--max-mult 8")
        if "D40" in active: print("--max-abs-drift 0.40")
        if "Q50" in active: print("--min-quote-age-ms 50")
        if "C1" in active:  print("--max-cell-distance 1.0   # NOTE: bot doesn't yet have this flag; would need to add")
        print("--bandit-window 6 --bandit-floor 0.10 --bandit-pause-sec 60   # F4 loosening (Finding 1)")
        print("```")
        print("")
    print("## 4. What about jackpot dependency?")
    print("")
    print("The baseline (no filters) is **136% jackpot-dependent** (top 3 wins = 136% of cum PnL).")
    print("")
    print("| Combo | Top 3 % | Verdict |")
    print("|---|---|---|")
    for mask in range(16):
        active = [k for i, k in enumerate(keys) if mask & (1 << i)]
        sub = apply_filters(bets, active)
        s = stats(sub)
        if s["n"] < 20: continue
        v = "distributed" if s["top3_pct"] < 70 else ("borderline" if s["top3_pct"] < 100 else "jackpot-dependent")
        label = "—" if not active else "+".join(active)
        print(f"| {label} (n={s['n']}) | {s['top3_pct']:.0f}% | {v} |")
    print("")


def main():
    bets = parse()
    header()
    section_individual(bets)
    section_all_combinations(bets)
    section_winner(bets)


if __name__ == "__main__":
    main()
