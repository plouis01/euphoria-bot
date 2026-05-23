"""Aggregate backtest JSONL with rich per-tag stats.

Computes for each tag:
  n, n_wins, WR_rs, WR_rs lo/hi 95% CI (Wilson),
  cum_pnl, $/tap, pnl_lo95, pnl_hi95 (bootstrap),
  max_drawdown, max_run_up,
  avg_mult, avg_win_payoff, sharpe_like (pnl_per_bet × sqrt(n) / σ),
  bets/hr (derived from cx span).
"""
import json, math, random, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "logs" / "backtest_rs_20260523_144850.jsonl"

DRAG = 0.06
TIME_INTERVAL = 5  # seconds per cell-X step


def pnl_of(rec):
    if rec["rs_won"]:
        return rec["mult"] * (1.0 - DRAG) - 1.0
    return -1.0


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0, center - half), min(1, center + half))


def bootstrap_pnl_ci(pnl_list: list[float], n_iter: int = 2000,
                      seed: int = 42) -> tuple[float, float]:
    """95% CI on cumulative PnL via bootstrap resampling with replacement."""
    if len(pnl_list) < 2:
        return (sum(pnl_list), sum(pnl_list))
    rng = random.Random(seed)
    n = len(pnl_list)
    sums = []
    for _ in range(n_iter):
        s = sum(rng.choice(pnl_list) for _ in range(n))
        sums.append(s)
    sums.sort()
    return (sums[int(0.025 * n_iter)], sums[int(0.975 * n_iter)])


def max_drawdown(pnl_series_sorted: list[float]) -> float:
    """Max trough-from-peak on the cumulative-PnL trajectory."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnl_series_sorted:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def max_runup(pnl_series_sorted: list[float]) -> float:
    """Max peak-from-trough on the cumulative-PnL trajectory."""
    cum = 0.0
    trough = 0.0
    mru = 0.0
    for p in pnl_series_sorted:
        cum += p
        trough = min(trough, cum)
        mru = max(mru, cum - trough)
    return mru


def main():
    by_tag = defaultdict(list)
    for line in open(JSONL):
        r = json.loads(line)
        by_tag[r["tag"]].append(r)

    rows = []
    for tag, bets in by_tag.items():
        # Sort chronologically by cx (cell-X = window_start_ts / 5)
        bets.sort(key=lambda r: r["cx"])
        n = len(bets)
        wins = sum(1 for b in bets if b["rs_won"])
        wr = wins / n
        wr_lo, wr_hi = wilson_ci(wins, n)
        pnls = [pnl_of(b) for b in bets]
        cum_pnl = sum(pnls)
        per_tap = cum_pnl / n
        pnl_lo, pnl_hi = bootstrap_pnl_ci(pnls)
        mdd = max_drawdown(pnls)
        mru = max_runup(pnls)
        # mult stats
        win_mults = [b["mult"] for b in bets if b["rs_won"]]
        avg_mult_all = sum(b["mult"] for b in bets) / n
        avg_win = sum(win_mults) / len(win_mults) if win_mults else 0
        # Sharpe-like: per-bet PnL / per-bet stddev * sqrt(n)
        mean_p = cum_pnl / n
        var_p = sum((p - mean_p) ** 2 for p in pnls) / max(n - 1, 1)
        sd_p = math.sqrt(var_p) if var_p > 0 else 0
        sharpe_like = (mean_p / sd_p * math.sqrt(n)) if sd_p > 0 else 0
        # Time span (chrono)
        span_sec = (bets[-1]["cx"] - bets[0]["cx"]) * TIME_INTERVAL
        bets_per_hr = (n / span_sec * 3600) if span_sec > 0 else 0
        kind = "LIVE" if bets[0]["kind"] == "LIVE" else "DRY"
        rows.append({
            "tag": tag, "kind": kind, "n": n, "wins": wins,
            "wr": wr, "wr_lo95": wr_lo, "wr_hi95": wr_hi,
            "cum_pnl": cum_pnl, "per_tap": per_tap,
            "pnl_lo95": pnl_lo, "pnl_hi95": pnl_hi,
            "max_dd": mdd, "max_runup": mru,
            "avg_mult": avg_mult_all, "avg_win_payoff": avg_win,
            "sharpe_like": sharpe_like,
            "span_sec": span_sec, "bets_per_hr": bets_per_hr,
            "pnl_per_hr": cum_pnl / span_sec * 3600 if span_sec > 0 else 0,
        })
    rows.sort(key=lambda r: -r["cum_pnl"])
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
