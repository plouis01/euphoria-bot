"""Re-resolve autonomous-run dryrun logs using CB outcome instead of EU outcome.

Same parser as sweep_report.py, but uses the CB= field (Coinbase tape) as
ground truth instead of EU= (Euphoria price tape).

Goal: validate whether the HOUR_FINAL leaderboard was inflated by EU-tape
over-counting wins relative to RedStone/Coinbase consensus.
"""
import os, re, sys, glob, math, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

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

SLIP_REJECT_ALPHA = 0.05
SLIP_AVG_DRAG = 0.06
def slip_adjusted_pnl(signed_mult: float, won: bool) -> float:
    reject_p = 1.0 - math.exp(-SLIP_REJECT_ALPHA * max(0.0, signed_mult - 5.0))
    if not won:
        return -1.0 * (1.0 - reject_p)
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
            "cb_n": int(d["cbn"]),
            "eu_won": d["eu"] == "WIN",
            "cb_won": d["cb"] == "WIN",
            "p": float(d["p"]),
            "sigma": float(d["sigma"]),
            "drift": float(d["drift"]),
            "pnl_dry_eu": float(d["pnl"]),
        })
    return bets


def collect_signal_mults(path: str) -> dict[tuple[int, int], float]:
    out = {}
    for line in open(path):
        m = SIGNAL_RE.search(line)
        if m:
            out[(int(m.group("cx")), int(m.group("cy")))] = float(m.group("mult"))
    return out


def report(tag_pattern: str = "*", min_mtime_epoch: float = 0,
           require_cb_ticks: int = 3):
    print(f"# CB-resolved sweep report — pattern: {tag_pattern}  "
          f"(min CB ticks per bet: {require_cb_ticks})")
    print()
    print(f"{'tag':<24} {'n':>4} {'n_cb':>4} "
          f"{'WR_eu':>6} {'WR_cb':>6} "
          f"{'PnL_eu_dry':>11} {'PnL_cb_dry':>11} {'PnL_cb_live':>11} "
          f"{'EUorCB%':>7} {'EUnotCB%':>9}")
    print("-" * 120)
    all_logs = sorted(glob.glob(f"logs/{tag_pattern}*.log"))
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
        bets_all = parse_tag_log(log)
        if not bets_all:
            continue
        # Filter to bets with enough CB ticks to be a trustworthy oracle
        bets = [b for b in bets_all if b["cb_n"] >= require_cb_ticks]
        if not bets:
            continue
        signal_mults = collect_signal_mults(log)
        n = len(bets)
        w_eu = sum(1 for b in bets if b["eu_won"])
        w_cb = sum(1 for b in bets if b["cb_won"])
        pnl_eu_dry = sum(b["pnl_dry_eu"] for b in bets)
        # Re-compute dry PnL using CB outcome instead of EU
        pnl_cb_dry = 0.0
        pnl_cb_live = 0.0
        for b in bets:
            sm = signal_mults.get((b["cx"], b["cy"]))
            if sm is None:
                sm = (b["pnl_dry_eu"] + 1.0) if b["eu_won"] else 3.0
            if b["cb_won"]:
                pnl_cb_dry += (abs(sm) - 1.0)
            else:
                pnl_cb_dry -= 1.0
            pnl_cb_live += slip_adjusted_pnl(sm, b["cb_won"])
        # Disagreement breakdown
        eu_or_cb = sum(1 for b in bets if b["eu_won"] != b["cb_won"])
        eu_not_cb = sum(1 for b in bets if b["eu_won"] and not b["cb_won"])  # over-count
        rows.append({
            "tag": tag, "n": n, "n_cb": n,
            "wr_eu": 100*w_eu/n, "wr_cb": 100*w_cb/n,
            "pnl_eu_dry": pnl_eu_dry, "pnl_cb_dry": pnl_cb_dry,
            "pnl_cb_live": pnl_cb_live,
            "disagree_pct": 100*eu_or_cb/n,
            "eu_over_cb_pct": 100*eu_not_cb/n,
        })
    rows.sort(key=lambda r: -r["pnl_cb_live"])
    for r in rows:
        print(f"{r['tag']:<24} {r['n']:>4} {r['n_cb']:>4} "
              f"{r['wr_eu']:>5.1f}% {r['wr_cb']:>5.1f}% "
              f"${r['pnl_eu_dry']:>+10.2f} ${r['pnl_cb_dry']:>+10.2f} "
              f"${r['pnl_cb_live']:>+10.2f} "
              f"{r['disagree_pct']:>6.1f}% {r['eu_over_cb_pct']:>8.1f}%")
    print()
    return rows


if __name__ == "__main__":
    AUTONOMOUS_START = time.mktime(time.strptime("2026-05-22 22:38:00", "%Y-%m-%d %H:%M:%S"))
    pat = sys.argv[1] if len(sys.argv) > 1 else "*"
    require_cb = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    report(pat, min_mtime_epoch=AUTONOMOUS_START, require_cb_ticks=require_cb)
