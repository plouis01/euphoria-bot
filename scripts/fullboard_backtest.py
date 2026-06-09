"""Full-board out-of-sample test of the portfolio candidate rule.

Motivation: DEEP_ANALYSIS_V2/V3 + PORTFOLIO_ANALYSIS selected
  UP × mult [4,6) × cell_dist < 3 bands × hours 22:00→09:00 UTC
from 5,263 bets that the DRY bot fleet happened to signal — a
selection-biased population, and the hour gate was fitted in-sample.

This script replays the rule against EVERY cell the MM ever quoted
(cell_snapshots: 'f' full frames + 'c' 50ms deltas + 'p' EU price ticks),
betting each qualifying cell exactly once at ~30s lead (matching c30 bet
stamping), and resolves on the reconstructed on-chain RedStone tape
(scripts/fetch_redstone_tape.py) with the same segment-crossing
touched_band resolver the live bot uses.

Arms reported: the rule, dist<2 / dist<1 variants, no-hour-gate, inverse
hours, DOWN placebo, neighbor mult bands — plus resolver validation against
the RS=WIN/LOSS lines recorded live in the DRY logs.

Usage:
  python3 scripts/fullboard_backtest.py --extract-only     # phase 1 sanity
  python3 scripts/fullboard_backtest.py                    # full run
"""
import argparse
import bisect
import json
import math
import re
import sys
import time
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from resolver import touched_band  # noqa: E402  (same resolver as live bot)

DRAG = 0.06
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5
BET_LEAD_S = 30          # c30: bet stamped 30s before window start
MAX_PRICE_AGE_MS = 2500  # mirrors --max-euphoria-price-age-s 2 (+ slack)
KEEP_MULT = (3.0, 7.0)   # superset of arms
KEEP_DIST = 4.0          # bands; superset of arms

BETS_OUT = ROOT / "logs/fullboard_bets.jsonl"
TAPE_GLOB = str(ROOT / "logs/redstone_tape_span*.jsonl")
SNAP_GLOB = str(ROOT / "logs/cell_snapshots_*.jsonl")
DRY_GLOB = str(ROOT / "logs/DRY_*_2026052[5-7]*.log")


# ---------------------------------------------------------------- phase 1
def extract_bets():
    """Replay snapshot files; emit one bet per quoted cell at ~30s lead."""
    bets = []
    for fp in sorted(glob(SNAP_GLOB)):
        board = {}            # (cx, cy) -> mult
        cols = {}             # cx -> set of cy present
        done_cols = set()
        pending = []          # sorted unique cx awaiting their bet time
        last_price = None
        last_price_ts = 0
        n_lines = 0
        for line in open(fp):
            n_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = d["ts_ms"]
            t = d["t"]
            if t == "p":
                last_price = d["price"]
                last_price_ts = ts
            elif t == "f":
                board = {}
                cols = defaultdict(set)
                for key, mult in d["full"].items():
                    cx_s, cy_s = key.split("_")
                    cx, cy = int(cx_s), int(cy_s)
                    board[(cx, cy)] = mult
                    cols[cx].add(cy)
                    if cx not in done_cols and cx not in pending:
                        bisect.insort(pending, cx)
            elif t == "c":
                for cx, cy, mult in d["diffs"]:
                    board[(cx, cy)] = mult
                    cols.setdefault(cx, set()).add(cy)
                    if cx not in done_cols and cx not in pending:
                        bisect.insort(pending, cx)
            # evaluate any column whose bet time has just passed
            due = (ts / 1000 + BET_LEAD_S) / TIME_INTERVAL
            while pending and pending[0] <= due:
                cx = pending.pop(0)
                done_cols.add(cx)
                t_bet_ms = (cx * TIME_INTERVAL - BET_LEAD_S) * 1000
                # column must be evaluated close to its bet time — a column
                # first seen long after its bet time (collector gap) is void
                if ts - t_bet_ms > 1500:
                    continue
                if last_price is None or ts - last_price_ts > MAX_PRICE_AGE_MS:
                    continue
                p = last_price
                for cy in cols.get(cx, ()):
                    mult = board.get((cx, cy))
                    if mult is None or not (KEEP_MULT[0] <= mult < KEEP_MULT[1]):
                        continue
                    band_mid = cy * PRICE_INTERVAL + PRICE_INTERVAL / 2
                    dist = abs(band_mid - p) / PRICE_INTERVAL
                    if dist >= KEEP_DIST:
                        continue
                    bets.append({
                        "t_bet_ms": t_bet_ms, "cx": cx, "cy": cy,
                        "mult": mult, "p_eu": p, "dist": dist,
                        "dir": 1 if band_mid > p else -1,
                        "hour_utc": int((t_bet_ms / 1000) % 86400 // 3600),
                    })
        print(f"[extract] {Path(fp).name}: {n_lines:,} lines → "
              f"{len(bets):,} bets cumulative", flush=True)
    # dedupe (file overlap safety) and sort
    uniq = {(b["cx"], b["cy"]): b for b in bets}
    bets = sorted(uniq.values(), key=lambda b: b["t_bet_ms"])
    with open(BETS_OUT, "w") as f:
        for b in bets:
            f.write(json.dumps(b) + "\n")
    print(f"[extract] {len(bets):,} unique cell-bets → {BETS_OUT}", flush=True)
    return bets


# ---------------------------------------------------------------- phase 2
def load_tape():
    ticks = []
    for fp in sorted(glob(TAPE_GLOB)):
        for line in open(fp):
            try:
                d = json.loads(line)
                ticks.append((d["started_at_ms"], d["price"]))
            except (json.JSONDecodeError, KeyError):
                continue
    ticks.sort()
    print(f"[tape] {len(ticks):,} RedStone prints loaded", flush=True)
    return ticks


def resolve_bets(bets, ticks):
    ts_arr = [t[0] for t in ticks]
    resolved = []
    n_no_tape = 0
    for b in bets:
        ws = b["cx"] * TIME_INTERVAL * 1000
        we = ws + TIME_INTERVAL * 1000
        i0 = bisect.bisect_left(ts_arr, ws)
        i1 = bisect.bisect_right(ts_arr, we)
        if i1 - i0 < 2:  # need at least 2 prints for a meaningful window
            n_no_tape += 1
            continue
        lo = b["cy"] * PRICE_INTERVAL
        hi = lo + PRICE_INTERVAL
        b["won"] = touched_band(ticks[i0:i1], lo, hi)
        b["rs_n"] = i1 - i0
        resolved.append(b)
    print(f"[resolve] {len(resolved):,} resolved, {n_no_tape:,} dropped "
          f"(no tape coverage)", flush=True)
    return resolved


# ---------------------------------------------------------------- phase 3
def validate_against_dry(ticks):
    """Re-resolve DRY-log bets with our tape; compare to live-recorded RS=."""
    ts_arr = [t[0] for t in ticks]
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS)")
    seen = {}
    for fp in sorted(glob(DRY_GLOB)):
        for line in open(fp):
            m = res_re.search(line)
            if m:
                seen[(int(m.group(1)), int(m.group(2)))] = m.group(3) == "WIN"
    agree = disagree = uncovered = 0
    mism = []
    for (cx, cy), live_won in seen.items():
        ws = cx * TIME_INTERVAL * 1000
        we = ws + TIME_INTERVAL * 1000
        i0 = bisect.bisect_left(ts_arr, ws)
        i1 = bisect.bisect_right(ts_arr, we)
        if i1 - i0 < 2:
            uncovered += 1
            continue
        lo = cy * PRICE_INTERVAL
        hi = lo + PRICE_INTERVAL
        mine = touched_band(ticks[i0:i1], lo, hi)
        if mine == live_won:
            agree += 1
        else:
            disagree += 1
            if len(mism) < 8:
                mism.append((cx, cy, live_won, i1 - i0))
    total = agree + disagree
    pct = agree / total * 100 if total else 0
    lines = [
        f"- DRY-log bets with live RS outcome: {len(seen):,}",
        f"- covered by reconstructed tape: {total:,} "
        f"(uncovered {uncovered:,} — outside snapshot spans)",
        f"- **agreement: {agree:,}/{total:,} = {pct:.2f}%**",
    ]
    if mism:
        lines.append(f"- sample mismatches (cx, cy, live_won, my_rs_n): {mism}")
    return pct, lines


# ---------------------------------------------------------------- phase 3b
def picker_vs_mates(resolved):
    """Was the DRY fleet's EV picker adding skill WITHIN the rule region?

    Bot-picked cells (any DRY bet) vs other qualifying cells in the SAME
    column (cx) — a matched comparison that controls for timing/regime.
    If bot WR ≈ mates WR, the +$0.20/tap DRY finding was selection luck;
    if bot WR >> mates WR, the picker itself carries the edge.
    """
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\)")
    bot_cells = set()
    for fp in sorted(glob(DRY_GLOB)):
        for line in open(fp):
            m = sig_re.search(line)
            if m:
                bot_cells.add((int(m.group(1)), int(m.group(2))))
    region = [b for b in resolved
              if b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 3]
    by_col = defaultdict(list)
    for b in region:
        by_col[b["cx"]].append(b)
    bot_bets, mate_bets, paired_diffs = [], [], []
    for cx, cells in by_col.items():
        bots = [b for b in cells if (b["cx"], b["cy"]) in bot_cells]
        mates = [b for b in cells if (b["cx"], b["cy"]) not in bot_cells]
        bot_bets += bots
        mate_bets += mates
        if bots and mates:
            paired_diffs.append(
                sum(b["won"] for b in bots) / len(bots)
                - sum(b["won"] for b in mates) / len(mates))
    lines = ["", "## Picker skill test — bot-picked cells vs column-mates "
             "(rule region, all hours)", ""]
    for name, sel in (("bot-picked", bot_bets), ("column-mates", mate_bets),
                      ("bot-picked ∩ gate-hours (the original config)",
                       [b for b in bot_bets if gate(b)]),
                      ("column-mates ∩ gate-hours",
                       [b for b in mate_bets if gate(b)])):
        if not sel:
            lines.append(f"- {name}: n=0")
            continue
        wr = sum(b["won"] for b in sel) / len(sel)
        per = sum(pnl_of(b) for b in sel) / len(sel)
        lines.append(f"- **{name}**: n={len(sel):,}  WR={wr:.1%}  $/tap=${per:+.3f}")
    if paired_diffs:
        nd = len(paired_diffs)
        md = sum(paired_diffs) / nd
        sd = math.sqrt(sum((x - md) ** 2 for x in paired_diffs) / max(nd - 1, 1))
        t = md / (sd / math.sqrt(nd)) if sd > 0 else 0.0
        lines.append(f"- paired per-column WR diff (bot − mates): "
                     f"{md:+.1%} over {nd:,} columns, t={t:+.2f}")
    return lines


# ---------------------------------------------------------------- phase 4
def pnl_of(b):
    return (b["mult"] - 1) * (1 - DRAG) if b["won"] else -1.0


def gate(b):
    return b["hour_utc"] >= 22 or b["hour_utc"] < 9


ARMS = {
    "REC: UP mult[4,6) dist<3 22-09utc": lambda b: b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 3 and gate(b),
    "REC dist<2": lambda b: b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 2 and gate(b),
    "REC dist<1": lambda b: b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 1 and gate(b),
    "no hour gate": lambda b: b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 3,
    "INVERSE hours (09-22utc)": lambda b: b["dir"] == 1 and 4 <= b["mult"] < 6 and b["dist"] < 3 and not gate(b),
    "DOWN placebo (mirror of REC)": lambda b: b["dir"] == -1 and 4 <= b["mult"] < 6 and b["dist"] < 3 and gate(b),
    "mult[3,4) neighbor": lambda b: b["dir"] == 1 and 3 <= b["mult"] < 4 and b["dist"] < 3 and gate(b),
    "mult[6,7) neighbor": lambda b: b["dir"] == 1 and 6 <= b["mult"] < 7 and b["dist"] < 3 and gate(b),
}


def arm_stats(sel):
    n = len(sel)
    if n == 0:
        return None
    pnls = [pnl_of(b) for b in sel]
    tot = sum(pnls)
    mean = tot / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnls) / max(n - 1, 1))
    tstat = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    wr = sum(b["won"] for b in sel) / n
    # per-UTC-day
    by_day = defaultdict(list)
    for b in sel:
        by_day[time.strftime("%m-%d", time.gmtime(b["t_bet_ms"] / 1000))].append(pnl_of(b))
    days = {d: (len(v), sum(v) / len(v)) for d, v in sorted(by_day.items())}
    # proportional jackknife
    wins_desc = sorted(pnls, reverse=True)
    jk = {}
    for frac in (0.01, 0.02, 0.05):
        k = max(1, int(n * frac))
        jk[frac] = (tot - sum(wins_desc[:k])) / (n - k)
    return {"n": n, "wr": wr, "tot": tot, "per": mean, "t": tstat,
            "days": days, "jk": jk}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract-only", action="store_true")
    ap.add_argument("--use-cached-bets", action="store_true",
                    help="reuse logs/fullboard_bets.jsonl from a prior run")
    args = ap.parse_args()

    if args.use_cached_bets and BETS_OUT.exists():
        bets = [json.loads(line) for line in open(BETS_OUT)]
        print(f"[extract] reused {len(bets):,} cached bets", flush=True)
    else:
        bets = extract_bets()
    if args.extract_only:
        # quick qualification census
        for name, fn in ARMS.items():
            print(f"  {name:42s} n={sum(1 for b in bets if fn(b)):,}")
        return

    ticks = load_tape()
    val_pct, val_lines = validate_against_dry(ticks)
    resolved = resolve_bets(bets, ticks)

    out = ["# Full-Board Backtest — out-of-sample test of the portfolio candidate",
           "",
           f"Generated by scripts/fullboard_backtest.py. Bets = every cell the MM "
           f"quoted that qualifies (evaluated at ~{BET_LEAD_S}s lead, "
           f"price age ≤ {MAX_PRICE_AGE_MS}ms), $1 stake, drag {DRAG:.0%}, "
           f"resolved on the reconstructed on-chain RedStone tape.",
           "",
           "## Resolver validation vs live-recorded RS outcomes", ""]
    out += val_lines
    out += ["", f"Resolved full-board bets: **{len(resolved):,}**", "",
            "## Arms", "",
            "| arm | n | WR | total | $/tap | t | ex-1% | ex-2% | ex-5% |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    stats = {}
    for name, fn in ARMS.items():
        s = arm_stats([b for b in resolved if fn(b)])
        stats[name] = s
        if s is None:
            out.append(f"| {name} | 0 | — | — | — | — | — | — | — |")
            continue
        out.append(f"| {name} | {s['n']:,} | {s['wr']:.1%} | ${s['tot']:+.2f} | "
                   f"${s['per']:+.3f} | {s['t']:+.2f} | "
                   f"${s['jk'][0.01]:+.3f} | ${s['jk'][0.02]:+.3f} | "
                   f"${s['jk'][0.05]:+.3f} |")
    out += ["", "## Per-day breakdown (UTC days)", ""]
    for name in ("REC: UP mult[4,6) dist<3 22-09utc", "REC dist<2",
                 "no hour gate", "INVERSE hours (09-22utc)",
                 "DOWN placebo (mirror of REC)"):
        s = stats.get(name)
        if not s:
            continue
        days = "  ".join(f"{d}: ${per:+.3f}(n={n})" for d, (n, per) in s["days"].items())
        out.append(f"- **{name}** — {days}")

    # by-hour table for REC
    rec = [b for b in resolved if ARMS["REC: UP mult[4,6) dist<3 22-09utc"](b)]
    by_h = defaultdict(list)
    for b in rec:
        by_h[b["hour_utc"]].append(pnl_of(b))
    out += ["", "## REC arm by UTC hour", "",
            "| hour | n | $/tap | total |", "|---|---:|---:|---:|"]
    for h in sorted(by_h, key=lambda x: (x < 22, x)):
        v = by_h[h]
        out.append(f"| {h:02d} | {len(v):,} | ${sum(v)/len(v):+.3f} | ${sum(v):+.2f} |")

    # cadence-limited simulation: one bet per 8s bucket (highest mult), REC rule
    by_bucket = {}
    for b in rec:
        k = b["t_bet_ms"] // 8000
        if k not in by_bucket or b["mult"] > by_bucket[k]["mult"]:
            by_bucket[k] = b
    cad = arm_stats(list(by_bucket.values()))
    if cad:
        span_h = (rec[-1]["t_bet_ms"] - rec[0]["t_bet_ms"]) / 3.6e6 if rec else 0
        out += ["", "## Cadence-limited REC (1 bet / 8s, best mult — bot-realistic)", "",
                f"- n={cad['n']:,}  WR={cad['wr']:.1%}  total=${cad['tot']:+.2f}  "
                f"$/tap=${cad['per']:+.3f}  t={cad['t']:+.2f}",
                f"- jackknife ex-1/2/5%: ${cad['jk'][0.01]:+.3f} / "
                f"${cad['jk'][0.02]:+.3f} / ${cad['jk'][0.05]:+.3f}",
                f"- throughput ≈ {cad['n']/max(span_h,1e-9):.0f} taps/h across gate hours "
                f"(span {span_h:.1f}h incl. non-gate gaps)"]

    out += picker_vs_mates(resolved)

    rec_s = stats["REC: UP mult[4,6) dist<3 22-09utc"]
    verdict = ("**PASS** (≥ +$0.05/tap with t ≥ 2 and validation ≥ 97%)"
               if rec_s and rec_s["per"] >= 0.05 and rec_s["t"] >= 2 and val_pct >= 97
               else "**FAIL** — edge does not replicate full-board; treat the "
                    "portfolio finding as sweep noise")
    out += ["", "## Verdict", "", f"- REC $/tap = ${rec_s['per']:+.3f} "
            f"(t={rec_s['t']:+.2f}, n={rec_s['n']:,}), resolver validation "
            f"{val_pct:.2f}% → {verdict}"]

    doc = ROOT / "FULLBOARD_BACKTEST_RESULTS.md"
    doc.write_text("\n".join(out) + "\n")
    print("\n".join(out))
    print(f"\n[done] report → {doc}", flush=True)


if __name__ == "__main__":
    main()
