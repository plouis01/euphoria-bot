"""Backtest regime gate on yesterday's losing DRY_naked log.

For each bet, compute σ + KER at bet time and decide:
  - SKIP if gate condition holds (e.g., σ ∈ [0.15, 0.20) AND KER < 0.10)
  - KEEP otherwise

Then compute counterfactual PnL with skipped bets removed.

Tests multiple gate configurations:
  G1: skip σ ∈ [0.15, 0.20) AND KER < 0.10  (reviewer-corrected)
  G2: skip σ > 0.15 AND KER < 0.10
  G3: skip σ > 0.20 AND KER < 0.10  (v1's mis-located threshold)
  G4: skip σ > 0.15 (sigma-only, no KER)
  G5: also try mult≤8 cap (per external memory finding)
"""
import json, math, re, sys, bisect
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from regime_classifier_v2 import compute_sigma, compute_ker

LOG_BAD = "logs/DRY_naked_20260524_163524.log"  # yesterday's loser
LOG_GOOD = "logs/DRY_naked_2_20260524_224305.log"  # yesterday's winner
DRAG = 0.06


def reconstruct_tape_from_log(path):
    """Build a price tape from the bot's SIGNAL + no-signal lines."""
    p_re = re.compile(r"P_eu=\$([\d.]+)")
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\)")
    tape = []
    last_anchor_s = None
    line_n = 0
    for line in open(path):
        line_n += 1
        sm = sig_re.search(line)
        if sm:
            # cellX*5 is the future window start; signal fires ~30s before
            # (c-tau-strong-sec). Use as anchor.
            last_anchor_s = int(sm.group(1)) * 5 - 30
        pm = p_re.search(line)
        if pm and last_anchor_s is not None:
            # Stretch in 100ms steps after each anchor
            ts_ms = last_anchor_s * 1000 + (line_n % 600) * 100
            tape.append((ts_ms, float(pm.group(1))))
    # Dedup + sort
    tape.sort()
    return tape


def parse_bets_with_features(path):
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\).*mult=([\d.]+).*σ=([\d.]+)")
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*EU=(WIN|LOSS).*RS=(WIN|LOSS|N/A)")
    cell_data = {}
    for line in open(path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            cell_data[(cx, cy)] = {
                "mq": float(m.group(3)),
                "sigma": float(m.group(4)),  # already computed by bot
            }
    bets = []
    for line in open(path):
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            d = cell_data.get((cx, cy))
            if not d: continue
            rs = m.group(4)
            if rs not in ("WIN", "LOSS"): continue
            bets.append({"bet_time_s": cx * 5, "won": (rs == "WIN"), **d})
    return bets


def pnl_of(b): return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def evaluate_gate(bets, tape, gate_fn, label):
    """Apply gate_fn(b, sig, ker) to each bet; return summary stats."""
    kept = []
    skipped = []
    for b in bets:
        sig = compute_sigma(tape, b["bet_time_s"] * 1000)
        ker = compute_ker(tape, b["bet_time_s"] * 1000)
        if gate_fn(b, sig, ker):
            skipped.append(b)
        else:
            kept.append(b)
    n_kept = len(kept)
    n_skip = len(skipped)
    pnl_kept = sum(pnl_of(b) for b in kept)
    pnl_skipped = sum(pnl_of(b) for b in skipped)
    per_kept = pnl_kept / max(n_kept, 1)
    per_skip = pnl_skipped / max(n_skip, 1)
    # t-stat on kept
    if n_kept >= 2:
        pnls = [pnl_of(b) for b in kept]
        mp = pnl_kept / n_kept
        sd = (sum((p - mp) ** 2 for p in pnls) / (n_kept - 1)) ** 0.5
        t = (mp / sd * math.sqrt(n_kept)) if sd > 0 else 0
    else:
        t = 0
    return {
        "label": label, "n_kept": n_kept, "n_skip": n_skip,
        "pnl_kept": pnl_kept, "per_kept": per_kept,
        "pnl_skipped": pnl_skipped, "per_skip": per_skip,
        "t_kept": t,
    }


def main():
    for log, label in [(LOG_BAD, "YESTERDAY DRY_naked (loser)"),
                       (LOG_GOOD, "YESTERDAY DRY_naked_2 (winner)")]:
        print(f"\n=== {label}: {log} ===")
        bets = parse_bets_with_features(log)
        print(f"  total bets: {len(bets)}")
        # Use REAL price tape if available, otherwise reconstruct
        tape = reconstruct_tape_from_log(log)
        print(f"  reconstructed price tape: {len(tape)} ticks")
        baseline = sum(pnl_of(b) for b in bets)
        print(f"  baseline PnL: ${baseline:+.2f} ($/tap=${baseline/len(bets):+.3f})")

        # Define gates
        gates = [
            ("BASELINE (no gate)", lambda b, s, k: False),
            ("G1: σ∈[0.15,0.20) AND KER<0.10", lambda b, s, k: s is not None and k is not None and 0.15 <= s < 0.20 and k < 0.10),
            ("G2: σ>0.15 AND KER<0.10", lambda b, s, k: s is not None and k is not None and s > 0.15 and k < 0.10),
            ("G3: σ>0.20 AND KER<0.10 (v1 wrong)", lambda b, s, k: s is not None and k is not None and s > 0.20 and k < 0.10),
            ("G4: σ>0.15 (no KER, sigma-only)", lambda b, s, k: s is not None and s > 0.15),
            ("G5: mult>8 (cap lottery)", lambda b, s, k: b["mq"] > 8),
            ("G6: G2 + G5 combined", lambda b, s, k: (b["mq"] > 8) or (s is not None and k is not None and s > 0.15 and k < 0.10)),
            ("G7: G1 + G5 combined", lambda b, s, k: (b["mq"] > 8) or (s is not None and k is not None and 0.15 <= s < 0.20 and k < 0.10)),
        ]

        print(f"  {'gate':<40} {'n_kept':>7} {'n_skip':>7} {'pnl_kept':>10} {'$/tap':>8} {'t':>6} {'pnl_skip':>10}")
        for name, fn in gates:
            r = evaluate_gate(bets, tape, fn, name)
            print(f"  {name:<40} {r['n_kept']:>7} {r['n_skip']:>7} "
                  f"${r['pnl_kept']:>+8.2f} ${r['per_kept']:>+7.3f} {r['t_kept']:>+5.2f} "
                  f"${r['pnl_skipped']:>+8.2f}")


if __name__ == "__main__":
    main()
