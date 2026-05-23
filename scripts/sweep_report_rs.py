"""Aggregate per-bot stats using RedStone resolution (RS=WIN/LOSS).

RedStone is the actual oracle Euphoria settles against (per
REDSTONE_FEED_RESEARCH.md), so RS-resolved PnL is the closest dry-run
predictor of LIVE PnL.

Bets logged BEFORE the RS column was added show RS=N/A and are excluded.
"""
import os, re, sys, glob, math, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

RESOLVE_RS_RE = re.compile(
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

SLIP_AVG_DRAG = 0.06
def slip_drag_pnl(signed_mult: float, won: bool) -> float:
    if not won:
        return -1.0
    return signed_mult * (1.0 - SLIP_AVG_DRAG) - 1.0


def parse_log(path: str) -> list[dict]:
    bets = []
    for line in open(path):
        m = RESOLVE_RS_RE.search(line)
        if not m:
            continue
        d = m.groupdict()
        rs = d.get("rs")
        bets.append({
            "cx": int(d["cx"]), "cy": int(d["cy"]),
            "eu_won": d["eu"] == "WIN",
            "cb_won": d["cb"] == "WIN",
            "rs_won": (rs == "WIN") if rs in ("WIN", "LOSS") else None,
            "rs_n": int(d["rsn"]) if d.get("rsn") else 0,
            "p": float(d["p"]),
            "sigma": float(d["sigma"]),
            "drift": float(d["drift"]),
            "pnl_dry_eu": float(d["pnl"]),
        })
    return bets


def collect_mults(path: str) -> dict[tuple[int, int], float]:
    out = {}
    for line in open(path):
        m = SIGNAL_RE.search(line)
        if m:
            out[(int(m.group("cx")), int(m.group("cy")))] = float(m.group("mult"))
    return out


def report(tag_pattern: str = "*", min_mtime_epoch: float = 0,
           require_rs: bool = True, min_rs_ticks: int = 3):
    print(f"# RedStone-resolved sweep — pattern: {tag_pattern}  "
          f"(only bets with RS column; min_rs_ticks={min_rs_ticks})")
    print()
    print(f"{'tag':<24} {'n_rs':>5} "
          f"{'WR_eu':>6} {'WR_cb':>6} {'WR_RS':>6} "
          f"{'PnL_eu':>9} {'PnL_RS_dry':>11} {'PnL_RS_live':>11} "
          f"{'EUvsRS%':>7}")
    print("-" * 105)
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
        all_bets = parse_log(log)
        if not all_bets:
            continue
        rs_bets = [b for b in all_bets
                   if b["rs_won"] is not None and b["rs_n"] >= min_rs_ticks]
        if require_rs and not rs_bets:
            continue
        bets = rs_bets if require_rs else all_bets
        signal_mults = collect_mults(log)
        n = len(bets)
        w_eu = sum(1 for b in bets if b["eu_won"])
        w_cb = sum(1 for b in bets if b["cb_won"])
        w_rs = sum(1 for b in bets if b["rs_won"])
        pnl_eu = sum(b["pnl_dry_eu"] for b in bets)
        pnl_rs_dry = 0.0
        pnl_rs_live = 0.0
        for b in bets:
            sm = signal_mults.get((b["cx"], b["cy"]),
                                  (b["pnl_dry_eu"] + 1.0) if b["eu_won"] else 3.0)
            if b["rs_won"]:
                pnl_rs_dry += abs(sm) - 1.0
            else:
                pnl_rs_dry -= 1.0
            pnl_rs_live += slip_drag_pnl(sm, b["rs_won"])
        eu_vs_rs = sum(1 for b in bets if b["eu_won"] != b["rs_won"])
        rows.append({
            "tag": tag, "n": n,
            "wr_eu": 100*w_eu/n, "wr_cb": 100*w_cb/n, "wr_rs": 100*w_rs/n,
            "pnl_eu": pnl_eu, "pnl_rs_dry": pnl_rs_dry,
            "pnl_rs_live": pnl_rs_live,
            "eu_vs_rs_pct": 100*eu_vs_rs/n,
        })
    rows.sort(key=lambda r: -r["pnl_rs_live"])
    for r in rows:
        print(f"{r['tag']:<24} {r['n']:>5} "
              f"{r['wr_eu']:>5.1f}% {r['wr_cb']:>5.1f}% {r['wr_rs']:>5.1f}% "
              f"${r['pnl_eu']:>+8.2f} ${r['pnl_rs_dry']:>+10.2f} "
              f"${r['pnl_rs_live']:>+10.2f} "
              f"{r['eu_vs_rs_pct']:>6.1f}%")
    print()
    return rows


if __name__ == "__main__":
    pat = sys.argv[1] if len(sys.argv) > 1 else "*"
    min_rs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    # No mtime filter — pick up logs created after RS column was added
    report(pat, min_rs_ticks=min_rs)
