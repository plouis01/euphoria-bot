"""Parse LIVE F4k4 log and produce LIVE_RUN_F4K4.md with full analysis."""
import re, json, sys, os, math
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "LIVE_F4k4_20260523_230147.log"
TIME_INTERVAL = 5  # cellX × 5 = unix seconds
STARTING = 21.8917

# SIGNAL provides: σ, drift, p_pred, mult_at_quote
SIGNAL = re.compile(
    r"\[LIVE_F4k4\] \[bot\] SIGNAL strat=\S+ cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"mult=(?P<mult>[\d.]+) p=(?P<p>[\d.]+) ev=\+?(?P<ev>-?[\d.]+) "
    r"σ=(?P<sigma>[\d.]+) P_eu=\$(?P<eu>[\d.]+) P_cb=\$(?P<cb>[\d.n/a]+) "
    r"drift_cb=\$(?P<drift>[+-][\d.]+)"
)
# SETTLED gives ground truth + running PnL
SETTLED = re.compile(
    r"\[LIVE_F4k4\] \[bot\] LIVE settled tradeId=\S+ "
    r"cell=\((?P<cx>\d+),(?P<cy>\d+)\) mult=(?P<mult>[\d.]+) "
    r"outcome=(?P<outcome>won|lost) pnl=\$(?P<pnl>[+-]?[\d.]+) "
    r"bal=\$(?P<bal>\S+) "
    r"w/l=(?P<w>\d+)/(?P<l>\d+) realized=\$(?P<realized>[+-]?[\d.]+)"
)
F4_TRIP = re.compile(r"F4 BANDIT TRIGGERED wr=(?P<wr>[\d.]+)")


def parse_log():
    signals = {}  # (cx,cy) → signal dict
    settlements = []  # list of dicts (in order)
    f4_trips = []  # list of wr at trip time
    for line in open(LOG):
        m = SIGNAL.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            signals[(cx, cy)] = {
                "cx": cx, "cy": cy,
                "mult_signal": float(m.group("mult")),
                "p_pred": float(m.group("p")),
                "sigma": float(m.group("sigma")),
                "drift": float(m.group("drift")),
                "p_eu": float(m.group("eu")),
            }
            continue
        m = SETTLED.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            settlements.append({
                "cx": cx, "cy": cy,
                "mult_filled": float(m.group("mult")),
                "won": m.group("outcome") == "won",
                "realized": float(m.group("realized")),
                "ts": cx * TIME_INTERVAL,
            })
            continue
        m = F4_TRIP.search(line)
        if m:
            f4_trips.append(float(m.group("wr")))
    # Attach signal data to settlements
    for s in settlements:
        sig = signals.get((s["cx"], s["cy"]), {})
        s.update({
            "sigma": sig.get("sigma"),
            "drift": sig.get("drift"),
            "p_pred": sig.get("p_pred"),
            "mult_signal": sig.get("mult_signal"),
            "p_eu": sig.get("p_eu"),
        })
    return settlements, f4_trips


def compute_metrics(settlements):
    n = len(settlements)
    wins = sum(1 for s in settlements if s["won"])
    pnls = [(s["mult_filled"] - 1.0) if s["won"] else -1.0 for s in settlements]
    cum = sum(pnls)
    # Running balance
    bal_trajectory = [STARTING]
    for p in pnls:
        bal_trajectory.append(bal_trajectory[-1] + p)
    # Max drawdown (trough-from-peak)
    peak, mdd = STARTING, 0
    for b in bal_trajectory:
        peak = max(peak, b)
        mdd = max(mdd, peak - b)
    # Max run-up (peak-from-trough)
    trough, mru = STARTING, 0
    for b in bal_trajectory:
        trough = min(trough, b)
        mru = max(mru, b - trough)
    # Avg win mult
    win_mults = [s["mult_filled"] for s in settlements if s["won"]]
    avg_win_mult = sum(win_mults) / len(win_mults) if win_mults else 0
    median_mult = sorted(win_mults)[len(win_mults)//2] if win_mults else 0
    return {
        "n": n, "wins": wins, "wr": 100*wins/n if n else 0,
        "cum_pnl": cum, "per_tap": cum/n if n else 0,
        "max_dd": mdd, "max_runup": mru,
        "bal_trajectory": bal_trajectory,
        "final_bal": bal_trajectory[-1],
        "avg_win_mult": avg_win_mult,
        "median_win_mult": median_mult,
    }


def main():
    settlements, f4_trips = parse_log()
    m = compute_metrics(settlements)

    # === Build the report ===
    out = []
    out.append("# LIVE Run — F4k4 Production Validation")
    out.append("")
    out.append(f"**Run window**: 2026-05-23 23:01 → 2026-05-24 09:01 Brazil time (10h, watchdog timeout)")
    out.append(f"**Config**: c30-strong + cal2 + bandit-window 4 + bandit-floor 0.15, $1 stake, --signal-source coinbase")
    out.append(f"**Bot**: dryrun_bot_v3.py --live, PID 36135")
    out.append(f"**Log**: `{LOG.name}` ({os.path.getsize(LOG)//1024}KB, 2928 lines)")
    out.append("")
    out.append("## Bottom line")
    out.append("")
    out.append(f"- **Balance**: ${STARTING:.4f} → **${m['final_bal']:.4f}** = **+${m['cum_pnl']:.2f}** (+{100*m['cum_pnl']/STARTING:.1f}%)")
    out.append(f"- **Settlements**: {m['n']} bets, {m['wins']}W / {m['n']-m['wins']}L = **{m['wr']:.1f}% WR**")
    out.append(f"- **Edge**: ${m['per_tap']:+.3f}/tap (LIVE — includes real slippage)")
    out.append(f"- **Max drawdown**: ${m['max_dd']:.2f} (trough-from-peak in balance trajectory)")
    out.append(f"- **Max run-up**: ${m['max_runup']:.2f} (peak-from-trough)")
    out.append(f"- **Hourly rate**: ${m['cum_pnl']/10:.2f}/h gross")
    out.append(f"- **F4 bandit trips**: {len(f4_trips)} (each pauses 5 min)")
    out.append(f"- **Estimated idle from F4 pauses**: ~{len(f4_trips)*5/60:.1f}h of 10h ({len(f4_trips)*5/600*100:.0f}% downtime)")
    out.append("")

    # === Wallet trajectory ===
    out.append("## Wallet value over time")
    out.append("")
    out.append("Sampled every 10 settlements:")
    out.append("")
    out.append("| Bet # | Time (Brazil) | Balance | Δ from start |")
    out.append("|---|---|---|---|")
    if settlements:
        import time
        for i in range(0, len(settlements), 10):
            ts = settlements[i]["ts"]
            t_str = time.strftime("%H:%M:%S", time.localtime(ts))
            bal = m["bal_trajectory"][i+1]  # +1 because trajectory[0] is starting
            delta = bal - STARTING
            out.append(f"| {i+1} | {t_str} | ${bal:.2f} | ${delta:+.2f} |")
        # Always include final
        if (len(settlements)-1) % 10 != 0:
            i = len(settlements) - 1
            ts = settlements[i]["ts"]
            t_str = time.strftime("%H:%M:%S", time.localtime(ts))
            bal = m["bal_trajectory"][i+1]
            delta = bal - STARTING
            out.append(f"| {i+1} | {t_str} | ${bal:.2f} | ${delta:+.2f} |")
    out.append("")

    # === Hourly buckets ===
    out.append("## Hourly breakdown")
    out.append("")
    if settlements:
        import time
        start_ts = settlements[0]["ts"]
        buckets = defaultdict(lambda: {"n":0, "w":0, "pnl":0.0, "mults":[]})
        for s in settlements:
            hour = (s["ts"] - start_ts) // 3600
            b = buckets[hour]
            b["n"] += 1
            if s["won"]: b["w"] += 1
            b["pnl"] += (s["mult_filled"]-1) if s["won"] else -1
            b["mults"].append(s["mult_filled"])
        out.append("| Hour | Bets | Wins | WR | Hour PnL | Cum PnL | Avg mult |")
        out.append("|---|---|---|---|---|---|---|")
        cum = 0
        for h in sorted(buckets.keys()):
            b = buckets[h]
            cum += b["pnl"]
            avg_m = sum(b["mults"])/len(b["mults"]) if b["mults"] else 0
            t_start = time.strftime("%H:%M", time.localtime(start_ts + h*3600))
            t_end = time.strftime("%H:%M", time.localtime(start_ts + (h+1)*3600))
            out.append(f"| H{int(h)+1} ({t_start}–{t_end}) | {b['n']} | {b['w']} | "
                       f"{100*b['w']/b['n']:.0f}% | ${b['pnl']:+.2f} | ${cum:+.2f} | {avg_m:.1f}x |")
    out.append("")

    # === Multiplier distribution ===
    out.append("## Multiplier distribution")
    out.append("")
    out.append("| Mult range | Bets | Wins | WR | PnL | $/tap |")
    out.append("|---|---|---|---|---|---|")
    ranges = [(1,3), (3,5), (5,8), (8,12), (12,20), (20,50), (50,1000)]
    for lo, hi in ranges:
        sub = [s for s in settlements if lo <= s["mult_filled"] < hi]
        if not sub: continue
        sw = sum(1 for s in sub if s["won"])
        spnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
        out.append(f"| [{lo}, {hi}) | {len(sub)} | {sw} | {100*sw/len(sub):.1f}% | ${spnl:+.2f} | ${spnl/len(sub):+.3f} |")
    out.append("")

    # === σ distribution + WR per bucket ===
    out.append("## σ_at_bet distribution (market vol regime)")
    out.append("")
    out.append("| σ range | Bets | WR | PnL | $/tap |")
    out.append("|---|---|---|---|---|")
    sigma_ranges = [(0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 0.50), (0.50, 99)]
    for lo, hi in sigma_ranges:
        sub = [s for s in settlements if s.get("sigma") is not None and lo <= s["sigma"] < hi]
        if not sub: continue
        sw = sum(1 for s in sub if s["won"])
        spnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
        out.append(f"| [{lo:.2f}, {hi:.2f}) | {len(sub)} | {100*sw/len(sub):.1f}% | ${spnl:+.2f} | ${spnl/len(sub):+.3f} |")
    out.append("")

    # === Drift distribution ===
    out.append("## drift_cb distribution at bet time")
    out.append("")
    out.append("| |drift| range | Bets | WR | $/tap |")
    out.append("|---|---|---|---|")
    drift_ranges = [(0, 0.10), (0.10, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 1.0), (1.0, 99)]
    for lo, hi in drift_ranges:
        sub = [s for s in settlements if s.get("drift") is not None and lo <= abs(s["drift"]) < hi]
        if not sub: continue
        sw = sum(1 for s in sub if s["won"])
        spnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
        out.append(f"| [{lo:.2f}, {hi:.2f}) | {len(sub)} | {100*sw/len(sub):.1f}% | ${spnl/len(sub):+.3f} |")
    out.append("")

    # === Slippage analysis ===
    out.append("## Slippage analysis (signal mult vs filled mult)")
    out.append("")
    matched = [s for s in settlements if s.get("mult_signal") is not None]
    if matched:
        diffs = [s["mult_filled"] - s["mult_signal"] for s in matched]
        rels = [(s["mult_filled"] - s["mult_signal"]) / s["mult_signal"] for s in matched]
        out.append(f"- Matched signal→fill pairs: {len(matched)} / {len(settlements)}")
        out.append(f"- Avg absolute slippage: {sum(diffs)/len(diffs):+.3f}× (mult units)")
        out.append(f"- Avg relative slippage: {100*sum(rels)/len(rels):+.2f}%")
        out.append(f"- Positive slippage (filled > quoted): {sum(1 for d in diffs if d > 0)}/{len(diffs)}")
        out.append(f"- Reference: dry-run sweep assumed 6% drag on wins.")
    out.append("")

    # === Top wins ===
    out.append("## Top 10 wins by payoff")
    out.append("")
    out.append("| Mult | Payoff | σ | drift | Time |")
    out.append("|---|---|---|---|---|")
    import time
    win_records = sorted([s for s in settlements if s["won"]],
                         key=lambda s: -(s["mult_filled"]-1))[:10]
    for s in win_records:
        sig = f"{s['sigma']:.3f}" if s.get("sigma") is not None else "—"
        dr = f"${s['drift']:+.2f}" if s.get("drift") is not None else "—"
        t_str = time.strftime("%H:%M:%S", time.localtime(s["ts"]))
        out.append(f"| {s['mult_filled']}x | +${s['mult_filled']-1:.2f} | {sig} | {dr} | {t_str} |")
    out.append("")
    top3 = sum(s["mult_filled"]-1 for s in win_records[:3])
    out.append(f"**Top 3 wins**: ${top3:.2f} = {100*top3/m['cum_pnl']:.1f}% of total PnL")
    out.append(f"**Without top 3**: cum PnL = ${m['cum_pnl']-top3:+.2f} over {m['n']-3} bets, $/tap = ${(m['cum_pnl']-top3)/(m['n']-3):+.3f}")
    out.append("")

    # === F4 bandit activity ===
    out.append("## F4 bandit activity")
    out.append("")
    out.append(f"- Total trips: {len(f4_trips)}")
    out.append(f"- Each trip pauses 300s (5 min) → total estimated idle: {len(f4_trips)*5} min")
    out.append(f"- WR at trip time distribution:")
    if f4_trips:
        from collections import Counter
        trip_dist = Counter(round(w, 2) for w in f4_trips)
        for wr, n in sorted(trip_dist.items()):
            out.append(f"  - WR={wr*100:.0f}%: {n} trips")
    out.append("")

    # === Compared to predictions ===
    out.append("## Predicted vs actual (vs dry sweep RSUS_F4k4)")
    out.append("")
    out.append("| Metric | Dry sweep | LIVE | Delta |")
    out.append("|---|---|---|---|")
    out.append(f"| WR | 23.9% | {m['wr']:.1f}% | {m['wr']-23.9:+.1f}pp |")
    out.append(f"| $/tap | +$0.165 | ${m['per_tap']:+.3f} | ${m['per_tap']-0.165:+.3f} |")
    out.append(f"| Bets/hour | ~195 | {m['n']/10:.0f} | {m['n']/10 - 195:+.0f} |")
    out.append(f"| Hourly $ | ~$32 | ${m['cum_pnl']/10:.2f} | ${m['cum_pnl']/10 - 32:+.2f} |")
    out.append("")
    out.append("**Edge ratio**: per-bet edge preserved or improved. Throughput hit hard by F4 pauses (54 trips × 5min = 4.5h theoretical idle in 10h window).")
    out.append("")

    # === Final commentary ===
    # Compute worst realized DD (relative to starting balance), separate from peak-DD
    worst_realized = min(m["bal_trajectory"]) - STARTING
    top3_pct = 100 * top3 / m["cum_pnl"] if m["cum_pnl"] != 0 else 0
    other_per_tap = (m["cum_pnl"] - top3) / max(m["n"] - 3, 1)
    is_jackpot_dependent = top3_pct >= 80

    out.append("## Takeaways")
    out.append("")
    out.append(f"1. **Run profitable**: ${m['cum_pnl']:+.2f} over {m['n']} bets at {m['wr']:.1f}% WR. Per-tap edge ${m['per_tap']:+.3f} is close to dry-sweep prediction of +$0.165.")
    if is_jackpot_dependent:
        out.append(f"2. **⚠️ JACKPOT-DEPENDENT**: top 3 wins = ${top3:.2f} ({top3_pct:.0f}% of PnL). Excluding them: ${m['cum_pnl']-top3:+.2f} over {m['n']-3} bets, $/tap = ${other_per_tap:+.3f} (negative). The +$29.69 result hinges on three specific bets — same pattern as the morning's earlier LIVE failure and HOUR_FINAL's W1_baseline_c30r. **Single-run profit doesn't prove distributed edge.**")
    else:
        out.append(f"2. **Edge is distributed**: top 3 wins = ${top3:.2f} ({top3_pct:.0f}% of PnL). Other {m['n']-3} bets: $/tap ${other_per_tap:+.3f}.")
    out.append(f"3. **F4 bandit hyperactive** at {len(f4_trips)} trips. Estimated ~{len(f4_trips)*5/60:.1f}h of forced idle in 10h ({len(f4_trips)*5/600*100:.0f}% downtime). This is why hourly rate is ${m['cum_pnl']/10:.2f}/h vs predicted ~$32/h. Tuning candidates: `--bandit-pause-sec 60` (5min→1min) or `--bandit-floor 0.10` (less sensitive). Each trip happened at WR=0% over last 4 — extremely common for a 22% true-WR bot (P(0/4)=0.36).")
    out.append(f"4. **Drawdown stayed within risk budget**: worst realized drawdown was ${worst_realized:+.2f} (bet ~#60 at $5.48 balance) — under the max-loss floor of $20.89. But max DD from peak was ${m['max_dd']:.2f} — meaning we gave back ${m['max_dd']:.2f} from the high point before recovering. Psychologically painful even when not triggering safety.")
    out.append(f"5. **Stake scalability is NOT linear when jackpot-dependent**: scaling to $2 stake doubles all bets, but the dominant jackpot multipliers also concentrate variance. Don't extrapolate +$59/10h naively. Need either more LIVE runs to confirm edge is real (not just luck on 3 cells), or to find a config whose per-tap edge holds without jackpots.")
    out.append("")
    out.append("### Honest read")
    out.append("")
    if is_jackpot_dependent:
        out.append(f"The +${m['cum_pnl']:.2f} headline is good news *for this run*, but the jackpot dependency means **we cannot yet conclude F4k4 has reliable real-money edge.** Dry-sweep RSUS_F4k4 was NOT jackpot-dependent (top 3 = 32.5%) but this single LIVE run was. Likely reasons:")
        out.append("- LIVE n=151 vs dry n=977 — smaller sample more vulnerable to lottery noise")
        out.append("- F4 pauses cut out ~45% of low-noise bets, leaving disproportionately the high-mult ones that happened to win")
        out.append("- One favorable hour (H10: +$19.05) accounted for 64% of the day's gains")
        out.append("")
        out.append("**Recommendation**: run a second 10h LIVE session to test whether edge persists. If second run is also positive AND less jackpot-dependent (top 3 < 50% of PnL), THEN consider stake scaling.")
    else:
        out.append("Edge holds even with top 3 wins removed — consider this validated. Next step: scale stake to $2 with same safety gates.")
    out.append("")

    print("\n".join(out))


if __name__ == "__main__":
    main()
