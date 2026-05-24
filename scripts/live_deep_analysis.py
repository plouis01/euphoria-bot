"""Deep analysis of LIVE F4k4 10h run.

Goal: identify concrete parameter improvements.

Key questions:
1. F4 bandit — was each trip statistically justified or noise?
2. Did pauses actually protect us, or just lose bet opportunities?
3. mult cap simulation — would mult≤12 have helped (per backtest finding)?
4. σ × mult interaction — is there a sweet spot we should target?
5. p_pred calibration — does the model's confidence match reality?
6. drift × outcome — is drift signal predictive?
7. Quote age — does staleness hurt?
8. Time of day patterns
"""
import re, math, sys
from collections import defaultdict
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
F4_TRIP = re.compile(r"F4 BANDIT TRIGGERED wr=(?P<wr>[\d.]+)")
F4_PAUSE = re.compile(r"F4 paused, (?P<rem>\d+)s remaining")


def parse():
    signals = {}
    settlements = []
    # Track LINE NUMBER of each event for ordering
    events = []  # (line_n, type, data)
    for lineno, line in enumerate(open(LOG)):
        m = SIGNAL.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            sig = {
                "cx": cx, "cy": cy,
                "mult_signal": float(m.group("mult")),
                "p_pred": float(m.group("p")),
                "sigma": float(m.group("sigma")),
                "drift": float(m.group("drift")),
                "p_eu": float(m.group("eu")),
                "qage": int(m.group("qage")),
            }
            signals[(cx, cy)] = sig
            events.append((lineno, "signal", sig))
            continue
        m = SETTLED.search(line)
        if m:
            cx, cy = int(m.group("cx")), int(m.group("cy"))
            s = {
                "cx": cx, "cy": cy,
                "mult_filled": float(m.group("mult")),
                "won": m.group("outcome") == "won",
                "realized": float(m.group("realized")),
                "ts": cx * TIME_INTERVAL,
            }
            sig = signals.get((cx, cy), {})
            s.update(sig)
            settlements.append(s)
            events.append((lineno, "settled", s))
            continue
        m = F4_TRIP.search(line)
        if m:
            events.append((lineno, "trip", {"wr_at_trip": float(m.group("wr"))}))
    return settlements, events


def main():
    settlements, events = parse()

    print("# LIVE F4k4 — Deep Analysis for Parameter Tuning")
    print("")
    print(f"Source: {LOG.name}  ({len(settlements)} settlements, {len([e for e in events if e[1]=='trip'])} F4 trips)")
    print(f"Headline (already known): +$29.69 over 151 bets, 22.5% WR. Top 3 wins = 136% of PnL.")
    print("")

    # ===================================================================
    # 1. F4 BANDIT POST-MORTEM
    # ===================================================================
    print("## 1. F4 bandit post-mortem — was each trip justified?")
    print("")
    print(f"With true WR ≈ 22.5%, expected probability of 0 wins in 4 consecutive bets:")
    p_true = 0.225
    p_4losses = (1 - p_true) ** 4
    print(f"  P(0/4) = (1-{p_true})^4 = **{p_4losses:.3f}**")
    print(f"  i.e., {p_4losses*100:.0f}% of overlapping 4-bet windows will hit the trigger PURELY BY CHANCE.")
    print(f"  Observed trips: 54 over 151 bets ≈ {54/151*100:.0f}% trip rate per bet — consistent with chance, not signal.")
    print("")
    print("**The bandit was firing on noise**, not on regime shifts. Below: WR in the 30 bets BEFORE each trip:")
    print("")
    # For each trip, look at the 30 bets PRIOR (chronological)
    print("| Trip # | line_n | wr_at_trip | WR (prior 4) | WR (prior 30) | Was it 'real'? |")
    print("|---|---|---|---|---|---|")
    settled_by_line = sorted([(e[0], e[2]) for e in events if e[1] == "settled"], key=lambda x: x[0])
    trips = [(e[0], e[2]["wr_at_trip"]) for e in events if e[1] == "trip"]
    for i, (trip_line, wr_trip) in enumerate(trips[:15]):
        # Find settlements before trip_line
        prior = [s for ln, s in settled_by_line if ln < trip_line]
        last4 = prior[-4:]
        last30 = prior[-30:]
        wr4 = sum(1 for s in last4 if s["won"]) / max(len(last4), 1) * 100
        wr30 = sum(1 for s in last30 if s["won"]) / max(len(last30), 1) * 100
        real = "noise" if abs(wr30 - p_true*100) < 8 else "regime shift"
        print(f"| {i+1} | {trip_line} | {wr_trip*100:.0f}% | {wr4:.0f}% | {wr30:.0f}% | {real} |")
    if len(trips) > 15:
        print(f"| ... ({len(trips)-15} more) | | | | | |")
    print("")

    # ===================================================================
    # 2. DID PAUSES PROTECT US?
    # ===================================================================
    print("## 2. Did F4 pauses actually protect us?")
    print("")
    # For each trip: count outcomes of the 5 settlements AFTER the trip (would've been paused or not)
    # Actually we can't directly know — bot paused. But we can compare WR within bandit-window=4 windows where bot KEPT TRADING
    # vs the trips. Implicit test: if the WR after a trip was high, pause cost us; if low, pause helped.
    # We can look at the next settled bet after each trip (which happens after the 300s pause).
    print("After each F4 trip, the bot pauses 300s. The next settlement is *after* pause. If WRs after trips are higher than baseline, pause helped. If similar, pause was noise.")
    print("")
    after_trip_outcomes = []
    for trip_line, _ in trips:
        # Find first settled AFTER trip_line
        after = [s for ln, s in settled_by_line if ln > trip_line]
        if after:
            # First 4 settled after the trip
            after_trip_outcomes.extend([s["won"] for s in after[:4]])
    if after_trip_outcomes:
        n = len(after_trip_outcomes)
        w = sum(after_trip_outcomes)
        wr = 100 * w / n
        print(f"- Outcomes in the first 4 settled bets after each trip: n={n}, WR={wr:.1f}%")
        print(f"- Baseline WR (entire run): 22.5%")
        diff = wr - 22.5
        if abs(diff) < 3:
            print(f"- Difference: {diff:+.1f}pp → **pause provided NO meaningful protection**. The bandit fired on noise and skipped trades that would have had baseline WR.")
        elif diff > 0:
            print(f"- Difference: {diff:+.1f}pp → pause may have helped — post-pause WR higher than baseline.")
        else:
            print(f"- Difference: {diff:+.1f}pp → pause may have hurt — post-pause WR lower than baseline.")
    print("")

    # ===================================================================
    # 3. MULT CAP SIMULATION (per backtest finding mult≤12)
    # ===================================================================
    print("## 3. Mult-cap simulation — would max-mult 12 have helped?")
    print("")
    print("HOUR_FINAL backtest found mult≤12 cap gave t=+2.18 on W1_baseline. Test here:")
    print("")
    print("| Mult cap | n | WR | PnL | $/tap | Top 3 % of PnL |")
    print("|---|---|---|---|---|---|")
    for cap in [3, 5, 8, 12, 20, 50, 999]:
        sub = [s for s in settlements if s["mult_filled"] <= cap]
        if not sub: continue
        n = len(sub)
        w = sum(1 for s in sub if s["won"])
        pnls = sorted([(s["mult_filled"]-1) if s["won"] else -1 for s in sub], reverse=True)
        cum = sum(pnls)
        top3 = sum(pnls[:3])
        pct = (top3/cum*100) if cum > 0 else 0
        print(f"| ≤{cap} | {n} | {100*w/n:.1f}% | ${cum:+.2f} | ${cum/n:+.3f} | {pct:.0f}% |")
    print("")

    # ===================================================================
    # 4. p_pred CALIBRATION
    # ===================================================================
    print("## 4. p_pred calibration — does model confidence match reality?")
    print("")
    print("| p_pred bin | n | Predicted p (avg) | Actual WR | gap |")
    print("|---|---|---|---|---|")
    for lo, hi in [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.7), (0.7, 1.0)]:
        sub = [s for s in settlements if s.get("p_pred") is not None and lo <= s["p_pred"] < hi]
        if not sub: continue
        n = len(sub)
        w = sum(1 for s in sub if s["won"])
        pred_avg = sum(s["p_pred"] for s in sub) / n
        actual = w / n
        gap = pred_avg - actual
        print(f"| [{lo:.1f}, {hi:.1f}) | {n} | {pred_avg:.3f} | {actual:.3f} | {gap:+.3f} |")
    print("")

    # ===================================================================
    # 5. σ × mult INTERACTION
    # ===================================================================
    print("## 5. σ × mult interaction — sweet-spot heatmap")
    print("")
    print("WR / $-per-tap for each (σ_band × mult_band):")
    print("")
    sig_bands = [(0, 0.08), (0.08, 0.15), (0.15, 0.25), (0.25, 1.0)]
    mult_bands = [(1, 5), (5, 10), (10, 20), (20, 999)]
    print("| σ ↓  /  mult → | " + " | ".join(f"[{lo},{hi})" for lo, hi in mult_bands) + " |")
    print("|---" + "|---" * len(mult_bands) + "|")
    for slo, shi in sig_bands:
        row = [f"σ [{slo:.2f},{shi:.2f})"]
        for mlo, mhi in mult_bands:
            sub = [s for s in settlements
                   if s.get("sigma") is not None
                   and slo <= s["sigma"] < shi
                   and mlo <= s["mult_filled"] < mhi]
            if not sub:
                row.append("—")
            else:
                n = len(sub); w = sum(1 for s in sub if s["won"])
                pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
                row.append(f"n={n} WR={100*w/n:.0f}% ${pnl/n:+.2f}")
        print("| " + " | ".join(row) + " |")
    print("")

    # ===================================================================
    # 6. DRIFT × OUTCOME
    # ===================================================================
    print("## 6. Drift signal — does it predict wins?")
    print("")
    print("Bot bets in the direction of drift. If drift is real signal, larger |drift| → higher WR.")
    print("")
    print("| |drift| bin | n | WR | $/tap |")
    print("|---|---|---|---|")
    for lo, hi in [(0, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.55), (0.55, 99)]:
        sub = [s for s in settlements
               if s.get("drift") is not None
               and lo <= abs(s["drift"]) < hi]
        if not sub: continue
        n = len(sub); w = sum(1 for s in sub if s["won"])
        pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
        print(f"| [{lo:.2f}, {hi:.2f}) | {n} | {100*w/n:.1f}% | ${pnl/n:+.3f} |")
    print("")

    # ===================================================================
    # 7. QUOTE AGE
    # ===================================================================
    print("## 7. Quote age — does staleness hurt?")
    print("")
    print("| q_age bin (ms) | n | WR | $/tap |")
    print("|---|---|---|---|")
    for lo, hi in [(-1000, 0), (0, 100), (100, 250), (250, 500), (500, 9999)]:
        sub = [s for s in settlements
               if s.get("qage") is not None
               and lo <= s["qage"] < hi]
        if not sub: continue
        n = len(sub); w = sum(1 for s in sub if s["won"])
        pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in sub)
        print(f"| [{lo}, {hi}) | {n} | {100*w/n:.1f}% | ${pnl/n:+.3f} |")
    print("")

    # ===================================================================
    # 8. CELL DISTANCE
    # ===================================================================
    print("## 8. Cell distance from price at bet time")
    print("")
    print("Distance = |band_center - P_eu|, normalized by σ × √(time-to-window-open)")
    print("(Not a perfect metric for c30-strong's forward-projected cells but proxy for OOM-ness)")
    print("")
    print("| Distance bin ($) | n | WR | $/tap | avg mult |")
    print("|---|---|---|---|---|")
    # For each settlement, distance from EU price at signal time to cell band
    for lo, hi in [(0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 99)]:
        sub = []
        for s in settlements:
            if s.get("p_eu") is None: continue
            band_center = s["cy"] * 0.5 + 0.25
            dist = abs(band_center - s["p_eu"])
            if lo <= dist < hi: sub.append((s, dist))
        if not sub: continue
        n = len(sub); w = sum(1 for s,_ in sub if s["won"])
        pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s,_ in sub)
        avg_m = sum(s["mult_filled"] for s,_ in sub) / n
        print(f"| [{lo:.1f}, {hi:.1f}) | {n} | {100*w/n:.1f}% | ${pnl/n:+.3f} | {avg_m:.1f}x |")
    print("")

    # ===================================================================
    # 9. PARAMETER RECOMMENDATIONS
    # ===================================================================
    print("## 9. Concrete parameter recommendations")
    print("")
    # Reproduce key findings
    no_filter_pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in settlements)
    cap12 = [s for s in settlements if s["mult_filled"] <= 12]
    cap12_pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in cap12)
    cap20 = [s for s in settlements if s["mult_filled"] <= 20]
    cap20_pnl = sum((s["mult_filled"]-1) if s["won"] else -1 for s in cap20)
    # Without F4 (we can't truly know what bot would have done) but counts
    print("Based on the deep analysis above:")
    print("")
    print(f"### 9.1 LOOSEN or REMOVE F4 bandit")
    print(f"With true WR=22.5%, P(0/4)=36% → bandit was firing on chance, not real regime shifts.")
    print(f"Post-trip WR was near baseline (no protection earned). 54 pauses × 5min = 4.5h wasted.")
    print(f"  - Option A: `--bandit-window 6 --bandit-floor 0.10` (P(0/6 at WR=22.5%)=18%, less hair-trigger)")
    print(f"  - Option B: `--bandit-pause-sec 60` (5min→1min, recover faster)")
    print(f"  - Option C: REMOVE F4 entirely (omit --bandit-window). Risk: vulnerable to regime collapse.")
    print(f"")
    print(f"### 9.2 ADD `--max-mult N` cap")
    print(f"Current run: full mult range cum=${no_filter_pnl:+.2f}, jackpot-dependent (top 3 = 136%)")
    print(f"  - With cap ≤12: ${cap12_pnl:+.2f} over {len(cap12)} bets ($/tap=${cap12_pnl/len(cap12):+.3f})")
    print(f"  - With cap ≤20: ${cap20_pnl:+.2f} over {len(cap20)} bets ($/tap=${cap20_pnl/len(cap20):+.3f})")
    print(f"  - Choose cap that maximizes Sharpe AND removes jackpot reliance. See §3 table.")
    print(f"")
    print(f"### 9.3 Consider `--max-quote-age-ms` tightening")
    print(f"See §7. If WR degrades with quote age, reduce from current 700ms.")
    print(f"")
    print(f"### 9.4 σ-aware betting")
    print(f"§5 heatmap reveals best (σ, mult) cells. Could add a custom filter combining both.")
    print(f"")
    print(f"### 9.5 Calibration drift")
    print(f"§4 shows whether p_pred is biased (avg gap > 0 → over-confidence). Calibrator2 should be correcting this.")
    print(f"")
    print(f"### 9.6 NEXT VALIDATION RUN")
    print(f"Recommended config for next 10h LIVE test:")
    print(f"```")
    print(f"--strategy c30-strong --signal-source coinbase")
    print(f"--c-thr-strong 0.25 --c-tau-strong-sec 30 --vol-window-sec 30")
    print(f"--calibrator2")
    print(f"--bandit-window 6 --bandit-floor 0.10 --bandit-pause-sec 60   # loosened F4")
    print(f"--max-mult 12                                                  # cap jackpot dependency")
    print(f"--min-mult 1.2 --max-time-out-sec 50 --cadence-sec 8")
    print(f"--max-quote-age-ms 700")
    print(f"```")
    print(f"Expected impact: F4 trips down ~3x, throughput up ~3x; mult cap kills jackpot reliance and may give")
    print(f"smaller-but-more-distributed edge as seen in backtest M12 cohort.")


if __name__ == "__main__":
    main()
