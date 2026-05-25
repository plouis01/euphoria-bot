"""Re-score historical DRY bets under the new --mm-prior-blend 0.5 calibration.

KEY MATH:
  Under blend=B, p_eff = B*(1/mq) + (1-B)*p_gauss
  ev_new = mq * p_eff - 1 = B + (1-B)*mq*p_gauss - 1 = (1-B)*(mq*p_gauss - 1)
  So:     ev_new = (1-B) * ev_gauss

With B=0.5: ev halves. With B=1.0: ev is always 0. With B=0: ev_gauss preserved.

This means re-scoring an existing log is exact arithmetic — we don't need to
re-run the strategy. For each fired bet:
  - Recover ev_gauss from SIGNAL line
  - Compute ev_new = (1-B) * ev_gauss
  - Decide: would (ev_new - ev_haircut) >= ev_threshold?
  - If yes: keep the bet (outcome unchanged because it's the same cell)
  - If no:  the bot wouldn't have placed this bet

THIS IS A CONSERVATIVE TEST: it answers "of the bets we did place, which
survive the new calibration?" It does NOT answer "are there new bets we'd now
place?" — because tightening the threshold only removes bets, never adds them.

For each input log, sweep B ∈ {0.0, 0.25, 0.5, 0.75, 1.0} and report:
  - n_fired, n_kept, n_dropped
  - PnL kept, $/tap kept, t-stat
  - PnL dropped (negative = bot's old calibration was wrongly firing on losers)
"""
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAG = 0.06

# Logs to test
LOGS = [
    ("YESTERDAY_loser", "logs/DRY_naked_20260524_163524.log"),
    ("YESTERDAY_winner", "logs/DRY_naked_2_20260524_224305.log"),
]


def parse_bot_config(path):
    """Extract ev_threshold and ev_haircut from the bot's startup line."""
    cfg = {"ev_threshold": 0.05, "ev_haircut": 0.0}
    for line in open(path):
        m = re.search(r"ev_thr=([\d.]+)", line)
        if m: cfg["ev_threshold"] = float(m.group(1))
        m = re.search(r"ev_haircut=([\d.]+)", line)
        if m: cfg["ev_haircut"] = float(m.group(1))
        # only need first few lines
        if "DRY-RUN" in line: break
    return cfg


def parse_fired_bets(path):
    """Return list of fired bets matched with their RS resolution."""
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+)"
    )
    res_re = re.compile(
        r"resolve cell=\((\d+),(\d+)\).*EU=(WIN|LOSS).*RS=(WIN|LOSS|N/A)"
    )
    # First pass: collect SIGNAL params per (cx, cy). Keep MOST RECENT (the
    # one that immediately precedes the bet). Multiple SIGNALs to same cell
    # are rare but possible across the whole run; matching to resolve via
    # cell is approximate. This is a known imprecision.
    sigs = {}
    for line in open(path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)),
                "p_gauss": float(m.group(4)),
                "ev_gauss": float(m.group(5)),
            }
    bets = []
    for line in open(path):
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sig = sigs.get((cx, cy))
            if not sig: continue
            rs = m.group(4)
            if rs not in ("WIN", "LOSS"): continue
            bets.append({
                "cell": (cx, cy),
                "won": rs == "WIN",
                **sig,
            })
    return bets


def pnl_of(b):
    return (b["mq"] - 1) * (1 - DRAG) if b["won"] else -1


def evaluate_blend(bets, B, ev_threshold, ev_haircut):
    """For each bet, decide if it would fire under blend=B."""
    kept = []
    dropped = []
    for b in bets:
        ev_new = (1 - B) * b["ev_gauss"]
        ev_adj_new = ev_new - ev_haircut
        if ev_adj_new >= ev_threshold:
            kept.append(b)
        else:
            dropped.append(b)
    n_kept = len(kept)
    n_drop = len(dropped)
    pnl_kept = sum(pnl_of(b) for b in kept)
    pnl_drop = sum(pnl_of(b) for b in dropped)
    per_kept = pnl_kept / max(n_kept, 1)
    if n_kept >= 2:
        pnls = [pnl_of(b) for b in kept]
        mp = sum(pnls) / n_kept
        sd = (sum((p - mp) ** 2 for p in pnls) / (n_kept - 1)) ** 0.5
        t = (mp / sd * math.sqrt(n_kept)) if sd > 0 else 0
    else:
        t = 0
    return {
        "B": B, "n_kept": n_kept, "n_drop": n_drop,
        "pnl_kept": pnl_kept, "per_kept": per_kept,
        "pnl_drop": pnl_drop, "t_kept": t,
    }


def main():
    for label, path in LOGS:
        full = ROOT / path
        if not full.exists():
            print(f"  MISSING {full}")
            continue
        cfg = parse_bot_config(full)
        bets = parse_fired_bets(full)
        if not bets:
            print(f"\n=== {label}: no bets parsed ==="); continue
        baseline_pnl = sum(pnl_of(b) for b in bets)
        print(f"\n=== {label}: {path} ===")
        print(f"  bot config: ev_threshold={cfg['ev_threshold']} "
              f"ev_haircut={cfg['ev_haircut']}")
        print(f"  baseline: n={len(bets)} pnl=${baseline_pnl:+.2f} "
              f"$/tap=${baseline_pnl/len(bets):+.3f}")
        print(f"\n  {'blend':>5} {'n_kept':>7} {'n_drop':>7} {'pnl_kept':>10} "
              f"{'$/tap':>8} {'t':>6} {'pnl_drop':>10} {'per_drop':>9}")
        for B in [0.0, 0.25, 0.5, 0.75, 1.0]:
            r = evaluate_blend(bets, B, cfg["ev_threshold"], cfg["ev_haircut"])
            per_drop = r["pnl_drop"] / max(r["n_drop"], 1)
            print(f"  {B:>5.2f} {r['n_kept']:>7} {r['n_drop']:>7} "
                  f"${r['pnl_kept']:>+8.2f} ${r['per_kept']:>+7.3f} "
                  f"{r['t_kept']:>+5.2f} ${r['pnl_drop']:>+8.2f} "
                  f"${per_drop:>+7.3f}")
        # Also: distribution of ev_gauss for context
        evs = sorted([b["ev_gauss"] for b in bets])
        n = len(evs)
        print(f"\n  ev_gauss distribution: "
              f"p10=${evs[n//10]:+.2f} p50=${evs[n//2]:+.2f} "
              f"p90=${evs[n*9//10]:+.2f} max=${evs[-1]:+.2f}")


if __name__ == "__main__":
    main()
