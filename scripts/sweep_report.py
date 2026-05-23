"""Aggregate per-bot stats from all logs/{tag}_*.log files.

Each bet line has format:
  [tag] [bot] resolve cell=(X,Y) ... EU=WIN|LOSS CB=WIN|LOSS p_pred=... ev_pred=...
       σ_at_bet=... drift_at_bet=$+/-X.XX pnl=$+/-X.XXXX [⚠ EU/CB DISAGREE]

Outputs:
  - per-tag rolling: n bets, WR, cum pnl, mean σ, mean drift, mean filled-mult,
    disagree rate, slippage-adjusted-PnL
  - cum PnL over time (last 60min)
  - cell-bucket breakdown
"""
import os, re, sys, glob, json, math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

# Parse one resolve line
RESOLVE_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] resolve cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"band=\[(?P<lo>[\d.]+),(?P<hi>[\d.]+)\) strat=(?P<strat>\S+) "
    r"eu_n=(?P<eun>\d+) cb_n=(?P<cbn>\d+) "
    r"EU=(?P<eu>WIN|LOSS) CB=(?P<cb>WIN|LOSS) "
    r"p_pred=(?P<p>[\d.]+) ev_pred=\+?(?P<ev>-?[\d.]+) "
    r"σ_at_bet=(?P<sigma>[\d.]+) drift_at_bet=\$(?P<drift>[+-][\d.]+) "
    r"pnl=\$(?P<pnl>[+-]?[\d.]+)"
)
SIGNAL_RE = re.compile(
    r"\[(?P<tag>[^\]]+)\] \[bot\] SIGNAL strat=\S+ cell=\((?P<cx>\d+),(?P<cy>\d+)\) "
    r"mult=(?P<mult>[\d.]+) p=[\d.]+ ev=\+?(?P<ev>-?[\d.]+) σ=[\d.]+ "
    r"P_eu=\$(?P<p_eu>[\d.]+).*drift_cb=\$(?P<drift_cb>[+-][\d.]+)"
)

# Slippage haircut model — fit from LIVE settlement history (~80 bets).
# In Run #2: slippage avg -8.3% (winners) / +X% (losers). Net signed PnL
# differs from filled PnL.
# Simplified model:
#   - Rejection probability: f(signed_mult) = 1 - exp(-α·max(0, signed_mult-5))
#     (high-mult cells get rejected more, low-mult almost never)
#   - Conditional on accepted, slip_pct = -0.05 (5% average drag)
SLIP_REJECT_ALPHA = 0.05  # gentler than v1 LIVE which had α≈0.10
SLIP_AVG_DRAG = 0.06      # 6% mult drag on average
def slip_adjusted_pnl(signed_mult: float, won: bool) -> float:
    """Estimate live-mode PnL accounting for rejection + slip drag."""
    reject_p = 1.0 - math.exp(-SLIP_REJECT_ALPHA * max(0.0, signed_mult - 5.0))
    # If rejected, PnL = 0 (no money moved)
    if not won:
        return -1.0 * (1.0 - reject_p)
    # Win — net = (mult - 1) * (1 - drag) - rejection lottery weight
    filled_mult = signed_mult * (1.0 - SLIP_AVG_DRAG)
    return (filled_mult - 1.0) * (1.0 - reject_p)


def parse_tag_log(path: str) -> list[dict]:
    bets = []
    for line in open(path):
        m = RESOLVE_RE.search(line)
        if not m:
            continue
        d = m.groupdict()
        bets.append({
            "cx": int(d["cx"]), "cy": int(d["cy"]),
            "strat": d["strat"],
            "eu": d["eu"] == "WIN",
            "cb": d["cb"] == "WIN",
            "p": float(d["p"]),
            "ev": float(d["ev"]),
            "sigma": float(d["sigma"]),
            "drift": float(d["drift"]),
            "pnl_dry": float(d["pnl"]),
            "band_lo": float(d["lo"]),
            "band_hi": float(d["hi"]),
        })
    return bets


def collect_signal_mults(path: str) -> dict[tuple[int, int], float]:
    """Map (cellX, cellY) → signed-mult from SIGNAL lines."""
    out = {}
    for line in open(path):
        m = SIGNAL_RE.search(line)
        if m:
            out[(int(m.group("cx")), int(m.group("cy")))] = float(m.group("mult"))
    return out


def report(tag_pattern: str = "*", min_mtime_epoch: float = 0):
    print(f"# Sweep report — pattern: {tag_pattern}")
    print()
    print(f"{'tag':<24} {'n':>4} {'W':>3} {'WR':>6} {'PnL_dry':>9} {'PnL_live':>10} "
          f"{'mean_σ':>7} {'meanDrf':>7} {'disagr%':>7}")
    print("-" * 96)
    all_logs = sorted(glob.glob(f"logs/{tag_pattern}*.log"))
    # Pick the LARGEST log file per tag (so we ignore early crashed launches).
    tag_to_log: dict[str, str] = {}
    for log in all_logs:
        if os.path.getmtime(log) < min_mtime_epoch:
            continue
        base = os.path.basename(log).replace(".log", "")
        parts = base.rsplit("_", 2)
        if len(parts) < 3 or not parts[-1].isdigit():
            continue
        tag = "_".join(parts[:-2])
        if tag not in tag_to_log or os.path.getsize(log) > os.path.getsize(tag_to_log[tag]):
            tag_to_log[tag] = log
    rows = []
    for tag, log in tag_to_log.items():
        bets = parse_tag_log(log)
        if not bets:
            continue
        signal_mults = collect_signal_mults(log)
        n = len(bets)
        w = sum(1 for b in bets if b["eu"])
        # dry-run PnL: sum of bet pnl_dry. But the bot's print uses (mult-1) on
        # WIN and -1 on LOSS, so it's the same as recomputing.
        pnl_dry = sum(b["pnl_dry"] for b in bets)
        # live-adjusted PnL: simulate rejection + slip
        pnl_live = 0.0
        for b in bets:
            sm = signal_mults.get((b["cx"], b["cy"]))
            # If we don't have a SIGNAL line (rare race), approximate with
            # filled mult = pnl_dry + 1 if win
            if sm is None:
                sm = (b["pnl_dry"] + 1.0) if b["eu"] else (b["pnl_dry"] / -1.0) * 3.0
            pnl_live += slip_adjusted_pnl(sm, b["eu"])
        mean_sigma = sum(b["sigma"] for b in bets) / n
        mean_drift = sum(b["drift"] for b in bets) / n
        disagree = sum(1 for b in bets if b["eu"] != b["cb"])
        rows.append({
            "tag": tag, "n": n, "w": w, "wr": 100*w/n,
            "pnl_dry": pnl_dry, "pnl_live": pnl_live,
            "mean_sigma": mean_sigma, "mean_drift": mean_drift,
            "disagree_pct": 100*disagree/n,
        })
    # Sort by live PnL desc
    rows.sort(key=lambda r: -r["pnl_live"])
    for r in rows:
        print(f"{r['tag']:<24} {r['n']:>4} {r['w']:>3} {r['wr']:>5.1f}% "
              f"${r['pnl_dry']:>+8.2f} ${r['pnl_live']:>+9.2f} "
              f"{r['mean_sigma']:>7.3f} ${r['mean_drift']:>+6.2f} "
              f"{r['disagree_pct']:>6.1f}%")
    print()
    return rows


if __name__ == "__main__":
    import time
    # Only count logs modified after the autonomous run started (22:38 today).
    AUTONOMOUS_START = time.mktime(time.strptime("2026-05-22 22:38:00", "%Y-%m-%d %H:%M:%S"))
    pat = sys.argv[1] if len(sys.argv) > 1 else "*"
    report(pat, min_mtime_epoch=AUTONOMOUS_START)
