"""
Variant-leaderboard parser for the 19-bot horse race.

Reads bot log files (one per variant tag) on stdin or via --log paths.
Parses every `resolve` line emitted by dryrun_bot_v3.py (see line ~351-357):

  [tag] [bot] resolve cell=(X,Y) band=[lo,hi) strat=S eu_n=N cb_n=N
        EU=WIN|LOSS CB=WIN|LOSS p_pred=P ev_pred=E σ_at_bet=S
        drift_at_bet=$+/-X.XX pnl=$+/-X.XXXX

Produces a leaderboard with:
  - n, win rate, mean PnL/tap
  - bootstrap 95% CI lower bound on PnL/tap (5000 resamples)
  - CB-disagree rate
  - per-σ-regime breakdown

Applies the survivor cutoffs from the plan and prints the top 3 by lower CI.

Usage:
  python scripts/leaderboard.py --log logs/c30s.log --log logs/prob.log ...
  # or
  ls logs/*.log | python scripts/leaderboard.py --stdin-list
"""
import argparse
import collections
import math
import os
import random
import re
import sys
from typing import Iterable


# Compile once. Matches:
# "...[bot] resolve cell=(151,4280) band=[2140.00,2140.50) strat=c30-strong
#   eu_n=12 cb_n=43 EU=LOSS CB=LOSS p_pred=0.231 ev_pred=+0.045
#   σ_at_bet=0.124 drift_at_bet=$+0.41 pnl=$-1.0000"
RESOLVE_RE = re.compile(
    r"^(?:\[(?P<tag>[^\]]*)\]\s*)?"
    r"\[bot\]\s*resolve\s+"
    r"cell=\((?P<cx>-?\d+),(?P<cy>-?\d+)\)\s+"
    r"band=\[(?P<lo>[-\d.]+),(?P<hi>[-\d.]+)\)\s+"
    r"strat=(?P<strat>\S+)\s+"
    r"eu_n=(?P<eun>\d+)\s+cb_n=(?P<cbn>\d+)\s+"
    r"EU=(?P<eu>WIN|LOSS)\s+CB=(?P<cb>WIN|LOSS)\s+"
    r"p_pred=(?P<p>[-\d.]+)\s+ev_pred=(?P<ev>[+\-\d.]+)\s+"
    r"σ_at_bet=(?P<sigma>[-\d.]+)\s+"
    r"drift_at_bet=\$(?P<drift>[+\-\d.]+)\s+"
    r"pnl=\$(?P<pnl>[+\-\d.]+)"
)


def parse_logs(paths: Iterable[str]) -> dict[str, list[dict]]:
    by_tag: dict[str, list[dict]] = collections.defaultdict(list)
    for path in paths:
        if not os.path.exists(path):
            print(f"[lb] skip: {path} not found", file=sys.stderr)
            continue
        with open(path, errors="replace") as f:
            for line in f:
                m = RESOLVE_RE.search(line)
                if not m:
                    continue
                row = {
                    "tag": m["tag"] or "_untagged",
                    "strat": m["strat"],
                    "eu": m["eu"] == "WIN",
                    "cb": m["cb"] == "WIN",
                    "p_pred": float(m["p"]),
                    "ev_pred": float(m["ev"]),
                    "sigma": float(m["sigma"]),
                    "drift": float(m["drift"]),
                    "pnl": float(m["pnl"]),
                }
                by_tag[row["tag"]].append(row)
    return by_tag


def bootstrap_mean_ci(samples: list[float], n_resamples: int = 5000,
                      alpha: float = 0.05, rng: random.Random | None = None
                      ) -> tuple[float, float, float]:
    """Returns (mean, lo, hi) for a two-sided (1-alpha) percentile bootstrap CI."""
    if not samples:
        return (0.0, 0.0, 0.0)
    if rng is None:
        rng = random.Random(42)
    n = len(samples)
    means = []
    for _ in range(n_resamples):
        s = 0.0
        for _ in range(n):
            s += samples[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_idx = int(n_resamples * (alpha / 2))
    hi_idx = int(n_resamples * (1 - alpha / 2)) - 1
    return (sum(samples) / n, means[lo_idx], means[hi_idx])


SIGMA_BUCKETS = [
    ("<0.06", lambda s: s < 0.06),
    ("0.06-0.10", lambda s: 0.06 <= s < 0.10),
    ("0.10-0.15", lambda s: 0.10 <= s < 0.15),
    (">=0.15", lambda s: s >= 0.15),
]


def per_sigma_bucket(rows: list[dict]) -> dict[str, tuple[int, float]]:
    """Return {bucket: (n, mean_pnl)} for each σ regime."""
    out = {}
    for label, pred in SIGMA_BUCKETS:
        rs = [r for r in rows if pred(r["sigma"])]
        if not rs:
            out[label] = (0, 0.0)
        else:
            out[label] = (len(rs), sum(r["pnl"] for r in rs) / len(rs))
    return out


def summarize(by_tag: dict[str, list[dict]], n_resamples: int,
              alpha_per_variant: float) -> list[dict]:
    rng = random.Random(42)
    out = []
    for tag, rows in by_tag.items():
        n = len(rows)
        if n == 0:
            continue
        pnls = [r["pnl"] for r in rows]
        wr = sum(1 for r in rows if r["eu"]) / n
        disagree = sum(1 for r in rows if r["eu"] != r["cb"]) / n
        mean, lo, hi = bootstrap_mean_ci(pnls, n_resamples=n_resamples,
                                          alpha=alpha_per_variant, rng=rng)
        out.append({
            "tag": tag,
            "n": n,
            "wr": wr,
            "mean_pnl": mean,
            "lo_ci": lo,
            "hi_ci": hi,
            "cb_disagree": disagree,
            "by_sigma": per_sigma_bucket(rows),
        })
    out.sort(key=lambda d: d["lo_ci"], reverse=True)
    return out


def apply_survivor_filters(summaries: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Returns (survivors, rejected_with_reason)."""
    survivors, rejected = [], []
    for s in summaries:
        if s["n"] < 30:
            rejected.append((s, f"n={s['n']} < 30"))
            continue
        if s["cb_disagree"] > 0.25:
            rejected.append((s, f"cb_disagree={s['cb_disagree']:.1%} > 25%"))
            continue
        if s["lo_ci"] <= 0:
            rejected.append((s, f"lower CI ${s['lo_ci']:+.4f} not > 0"))
            continue
        # >= 2 σ-regime buckets with positive mean (kills single-regime)
        positive_buckets = sum(1 for (n, m) in s["by_sigma"].values()
                                if n >= 5 and m > 0)
        if positive_buckets < 2:
            rejected.append((s, f"only {positive_buckets} σ-regime bucket(s) "
                             "with positive mean (need ≥2)"))
            continue
        survivors.append(s)
    return survivors, rejected


def print_table(summaries: list[dict], header: str) -> None:
    print(f"\n=== {header} ===")
    print(f"{'tag':<24} {'n':>4} {'WR':>6} {'mean$':>8} "
          f"{'lo95':>8} {'hi95':>8} {'CBdis':>6}")
    for s in summaries:
        print(f"{s['tag'][:24]:<24} {s['n']:>4} {s['wr']*100:>5.1f}% "
              f"{s['mean_pnl']:>+8.4f} {s['lo_ci']:>+8.4f} "
              f"{s['hi_ci']:>+8.4f} {s['cb_disagree']*100:>5.1f}%")


def print_regime(summary: dict) -> None:
    print(f"  {summary['tag']:<24} σ-regime breakdown:")
    for label, (n, m) in summary["by_sigma"].items():
        mark = "+" if m > 0 else "-"
        print(f"    {label:<10} n={n:>4} mean=${m:+.4f} {mark}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", action="append", default=[],
                    help="path to a bot log file (repeatable)")
    ap.add_argument("--stdin-list", action="store_true",
                    help="read log paths one per line from stdin")
    ap.add_argument("--resamples", type=int, default=5000)
    ap.add_argument("--alpha", type=float, default=None,
                    help="two-sided CI alpha. Default: 0.05 with Bonferroni "
                         "split over the number of distinct tags seen.")
    args = ap.parse_args()

    paths = list(args.log)
    if args.stdin_list:
        paths += [ln.strip() for ln in sys.stdin if ln.strip()]
    if not paths:
        print("usage: scripts/leaderboard.py --log <path> [--log <path>] ...",
              file=sys.stderr)
        return 2

    by_tag = parse_logs(paths)
    if not by_tag:
        print("[lb] no resolve lines parsed from any log", file=sys.stderr)
        return 1

    alpha = args.alpha if args.alpha is not None else (0.05 / max(1, len(by_tag)))
    print(f"[lb] {sum(len(rs) for rs in by_tag.values())} resolved bets "
          f"across {len(by_tag)} tag(s); CI alpha={alpha:.5f} "
          f"({'Bonferroni' if args.alpha is None else 'user-set'})",
          file=sys.stderr)

    summaries = summarize(by_tag, args.resamples, alpha)
    print_table(summaries, "ALL VARIANTS (sorted by lower 95% CI)")

    survivors, rejected = apply_survivor_filters(summaries)
    print_table(survivors[:3], f"TOP-3 SURVIVORS ({len(survivors)} total)")
    for s in survivors[:3]:
        print_regime(s)

    print(f"\n=== REJECTED ({len(rejected)}) ===")
    for s, reason in rejected:
        print(f"  {s['tag']:<24} n={s['n']:>4} mean=${s['mean_pnl']:+.4f} "
              f"-> {reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
