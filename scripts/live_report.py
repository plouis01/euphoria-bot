"""Generate a Markdown report from current live bot logs.

Outputs to stdout. Used by the scheduled hourly checkpoints.

Usage:
    python3 scripts/live_report.py "HOUR 0.5" > 23_may_Repport_HOUR_0_5.md
"""
import os, re, sys, glob, time, json, math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# Match bots launched today across the three groups
PATTERNS = ["RSUS_*", "BSIG_*", "M12_*"]
DRAG = 0.06
TIME_INTERVAL = 5

RESOLVE_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] resolve cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"band=\[(?P<lo>[\d.]+),(?P<hi>[\d.]+)\) strat=(?P<strat>\S+) "
    r"eu_n=(?P<eun>\d+) cb_n=(?P<cbn>\d+) "
    r"(?:rs_n=(?P<rsn>\d+) )?"
    r"EU=(?P<eu>WIN|LOSS) CB=(?P<cb>WIN|LOSS)(?: RS=(?P<rs>WIN|LOSS|N/A))? "
    r"p_pred=(?P<p>[\d.]+) ev_pred=\+?(?P<ev>-?[\d.]+) "
    r"σ_at_bet=(?P<sigma>[\d.]+) drift_at_bet=\$(?P<drift>[+-][\d.]+) "
    r"pnl=\$(?P<pnl>[+-]?[\d.]+)"
)
SIGNAL_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] SIGNAL strat=\S+ cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"mult=(?P<mult>[\d.]+)"
)


def wilson_ci(k, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    p = k/n
    denom = 1 + z**2/n
    center = (p + z**2/(2*n))/denom
    half = z*math.sqrt(p*(1-p)/n + z**2/(4*n**2))/denom
    return (max(0, center-half), min(1, center+half))


def bootstrap_pnl(pnls, n_iter=1500, seed=42):
    if len(pnls) < 2: return (sum(pnls), sum(pnls))
    import random
    rng = random.Random(seed)
    sums = []
    n = len(pnls)
    for _ in range(n_iter):
        sums.append(sum(rng.choice(pnls) for _ in range(n)))
    sums.sort()
    return (sums[int(0.025*n_iter)], sums[int(0.975*n_iter)])


def max_dd(pnls):
    cum, peak, mdd = 0, 0, 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def collect_bets():
    by_tag = defaultdict(list)
    mult_map = defaultdict(dict)  # tag → {(cx,cy): mult}
    for pat in PATTERNS:
        for path in glob.glob(f"logs/{pat}.log"):
            for line in open(path):
                m = SIGNAL_RE.search(line)
                if m:
                    tag = m.group("tag")
                    mult_map[tag][(int(m.group("cx")), int(m.group("cy")))] = float(m.group("mult"))
            for line in open(path):
                m = RESOLVE_RE.search(line)
                if not m: continue
                tag = m.group("tag")
                cx = int(m.group("cx")); cy = int(m.group("cy"))
                rs = m.group("rs")
                rs_n = int(m.group("rsn")) if m.group("rsn") else 0
                # Prefer RS as ground truth; fall back to EU
                if rs == "WIN":
                    won = True; oracle = "RS"
                elif rs == "LOSS":
                    won = False; oracle = "RS"
                else:
                    won = (m.group("eu") == "WIN"); oracle = "EU"
                mult = mult_map[tag].get((cx, cy))
                if mult is None:
                    # Fall back from PnL
                    pnl_line = float(m.group("pnl"))
                    mult = (pnl_line + 1.0) if m.group("eu") == "WIN" else 3.0
                by_tag[tag].append({
                    "cx": cx, "cy": cy, "mult": mult,
                    "rs_n": rs_n,
                    "won": won, "oracle": oracle,
                    "eu_won": m.group("eu") == "WIN",
                    "cb_won": m.group("cb") == "WIN",
                    "p_pred": float(m.group("p")),
                    "sigma": float(m.group("sigma")),
                    "drift": float(m.group("drift")),
                })
    return by_tag


def stat_block(bets, label=""):
    n = len(bets)
    wins = sum(1 for b in bets if b["won"])
    wr = wins/n if n else 0
    wr_lo, wr_hi = wilson_ci(wins, n)
    pnls = [b["mult"]*(1-DRAG)-1.0 if b["won"] else -1.0 for b in bets]
    cum = sum(pnls)
    per_tap = cum/n if n else 0
    pnl_lo, pnl_hi = bootstrap_pnl(pnls) if n >= 2 else (cum, cum)
    mdd = max_dd(pnls)
    mean_p = cum/n if n else 0
    var = sum((p-mean_p)**2 for p in pnls)/max(n-1, 1)
    sd = var**0.5
    t = (mean_p/sd*math.sqrt(n)) if sd > 0 else 0
    return {
        "label": label, "n": n, "wins": wins, "wr": wr,
        "wr_lo": wr_lo, "wr_hi": wr_hi,
        "cum_pnl": cum, "per_tap": per_tap,
        "pnl_lo": pnl_lo, "pnl_hi": pnl_hi,
        "max_dd": mdd, "t_stat": t,
    }


def fmt_row(s, name_pad=24):
    return (f"| {s['label']:<{name_pad}} | {s['n']:>4} | {s['wr']*100:>4.1f}% "
            f"| [{s['wr_lo']*100:>4.1f}, {s['wr_hi']*100:>4.1f}]% "
            f"| ${s['cum_pnl']:>+7.2f} "
            f"| [${s['pnl_lo']:>+6.1f}, ${s['pnl_hi']:>+6.1f}] "
            f"| ${s['per_tap']:>+5.2f} "
            f"| ${s['max_dd']:>5.2f} "
            f"| {s['t_stat']:>+5.2f} |")


def main():
    hour_label = sys.argv[1] if len(sys.argv) > 1 else f"HOUR {time.strftime('%H:%M')}"
    by_tag = collect_bets()
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z")

    # Compute stats for full bots
    rows = []
    for tag, bets in by_tag.items():
        if not bets: continue
        s = stat_block(bets, tag)
        rows.append(s)
    rows.sort(key=lambda r: -r["cum_pnl"])

    print(f"# {hour_label} — Bot Sweep Checkpoint")
    print(f"")
    print(f"**Generated**: {now}")
    print(f"**Bots reporting**: {len(rows)} (RSUS + BSIG + M12)")
    print(f"")

    # Section 1: full leaderboard
    print("## Full leaderboard (RS-resolved where available, EU fallback)")
    print()
    print("| Tag | n | WR | WR 95%CI | Cum PnL | PnL 95%CI | $/tap | Max DD | t |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(fmt_row(r))
    print()

    # Section 2: per-group rollup
    print("## Per-group rollup (pooled bets)")
    print()
    print("| Group | n | WR | WR 95%CI | Cum PnL | PnL 95%CI | $/tap | t |")
    print("|---|---|---|---|---|---|---|---|")
    for group_name, prefix in [("RSUS (sustainable)", "RSUS_"),
                                ("BSIG (Binance signal)", "BSIG_"),
                                ("M12 (mult≤12)", "M12_")]:
        pooled = []
        for tag, bets in by_tag.items():
            if tag.startswith(prefix):
                pooled.extend(bets)
        if not pooled: continue
        s = stat_block(pooled, group_name)
        print(f"| {s['label']:<24} | {s['n']:>4} | {s['wr']*100:>4.1f}% "
              f"| [{s['wr_lo']*100:>4.1f}, {s['wr_hi']*100:>4.1f}]% "
              f"| ${s['cum_pnl']:>+7.2f} | [${s['pnl_lo']:>+6.1f}, ${s['pnl_hi']:>+6.1f}] "
              f"| ${s['per_tap']:>+5.2f} | {s['t_stat']:>+5.2f} |")
    print()

    # Section 3: mult-bucket analysis on the WINNING-est full bots
    if rows:
        print("## Mult-bucket analysis (top 3 bots)")
        print()
        for r in rows[:3]:
            tag = r["label"]
            bets = by_tag[tag]
            if len(bets) < 10: continue
            print(f"### {tag} (n={len(bets)})")
            print("| Mult cap | n | WR | $/tap | t |")
            print("|---|---|---|---|---|")
            for cap in [5, 8, 12, 20, 30, 999]:
                sub = [b for b in bets if b["mult"] <= cap]
                if len(sub) < 5: continue
                s = stat_block(sub, f"≤{cap}")
                print(f"| ≤{cap} | {s['n']} | {s['wr']*100:.1f}% | ${s['per_tap']:+.3f} | {s['t_stat']:+.2f} |")
            print()

    # Section 4: jackpot dependency check on top 5
    print("## Jackpot dependency (top 5 by Cum PnL)")
    print()
    print("| Tag | Top-3 win PnL | % of cum | Other bets net |")
    print("|---|---|---|---|")
    for r in rows[:5]:
        tag = r["label"]
        bets = by_tag[tag]
        bets_with_pnl = [(b["mult"]*(1-DRAG)-1.0 if b["won"] else -1.0, b) for b in bets]
        bets_with_pnl.sort(key=lambda x: -x[0])
        top3 = bets_with_pnl[:3]
        top3_pnl = sum(p for p,_ in top3)
        cum = sum(p for p,_ in bets_with_pnl)
        other = cum - top3_pnl
        pct = (top3_pnl/cum*100) if cum != 0 else 0
        print(f"| {tag} | ${top3_pnl:+.2f} | {pct:.1f}% | ${other:+.2f} |")
    print()

    # Section 5: market regime snapshot
    print("## Market regime snapshot")
    print()
    sigma_samples = []
    drift_samples = []
    for bets in by_tag.values():
        for b in bets[-30:]:  # last 30 bets per bot
            sigma_samples.append(b["sigma"])
            drift_samples.append(abs(b["drift"]))
    if sigma_samples:
        sigma_samples.sort()
        n = len(sigma_samples)
        print(f"σ_at_bet: median={sigma_samples[n//2]:.3f}, "
              f"p25={sigma_samples[n//4]:.3f}, p75={sigma_samples[3*n//4]:.3f}, "
              f"max={max(sigma_samples):.3f}")
    if drift_samples:
        drift_samples.sort()
        n = len(drift_samples)
        print(f"|drift|: median=${drift_samples[n//2]:.2f}, "
              f"p75=${drift_samples[3*n//4]:.2f}, max=${max(drift_samples):.2f}")
    print()
    print(f"Reference: autonomous-sweep regime had σ≈0.06-0.15. Current may indicate a high-volatility regime.")


if __name__ == "__main__":
    main()
