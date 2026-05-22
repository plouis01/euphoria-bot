"""
Offline log re-scorer: applies the segment-crossing resolver to historical
bot logs and emits corrected resolve lines.

Why: in-flight bots ran with the membership-only resolver
(`dryrun_bot_v3.py:345-346` pre-fix). Their EU=WIN/LOSS columns under-count
intra-tick band crossings. This script reconstructs the touched_eu/touched_cb
decision using the segment-crossing rule and the persistent
settlements.jsonl audit log written by signing_proxy.py.

Limitation: settlements.jsonl is keyed by tradeId and stores the proxy's
post-settled view, not the raw price ticks during the window. For full
re-scoring we'd need the dense ticks from the WS tape captured during the
window. If the ticks are unavailable (out of WS-tape retention), we mark the
row 'no_ticks' and leave the original verdict.

Usage:
  python scripts/rescore_logs.py --log logs/c30s.log [--out logs/c30s.rescored.log]
                                 [--settlements logs/settlements.jsonl]

For each input `resolve` line the script writes (to --out, or stdout):
  - if the original verdict is reproducible from the segment resolver applied
    to the ticks present in the line context: emits a corrected line with
    EU= updated and an inline marker `RESCORE=changed|same|no_ticks`.

Note: this script does not need the proxy to be running.
"""
import argparse
import json
import os
import re
import sys
from typing import Iterable


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from resolver import touched_band  # noqa: E402


# Same regex shape as leaderboard.py; capture additionally `pnl` and `mult` (mult
# is reconstructable from pnl: WIN pnl = (mult-1)*stake, LOSS pnl = -stake; we
# can solve for mult only on WIN). We need it to recompute pnl after a flip.
RESOLVE_RE = re.compile(
    r"^(?P<prefix>(?:\[[^\]]*\]\s*)?)"
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


def load_settlements(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return entries


def _settled_ticks(entry: dict) -> list[tuple[int, float]]:
    """If a settlement entry has price ticks attached, return them sorted.
    Most entries won't — they hold the post-trade `settled` field only. If
    the proxy is later extended to attach the dense window ticks (P3),
    this is where they'd be read from."""
    data = entry.get("data") or {}
    ticks = data.get("priceTicks") or []
    out = []
    for t in ticks:
        try:
            out.append((int(t[0]), float(t[1])))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort()
    return out


def rescore(line: str, settlements_by_cell: dict[tuple[int, int], list[dict]]
            ) -> tuple[str, str]:
    """Return (output_line, marker) where marker is 'changed'|'same'|'no_ticks'."""
    m = RESOLVE_RE.search(line)
    if not m:
        return (line, "no_match")
    cx, cy = int(m["cx"]), int(m["cy"])
    lo, hi = float(m["lo"]), float(m["hi"])
    original_eu = m["eu"] == "WIN"
    pnl_old = float(m["pnl"])
    # Estimate stake & mult from original pnl (loss => -stake; win => (mult-1)*stake)
    # We assume stake=1.0 by convention (Run #1 used $1).
    stake = 1.0
    if original_eu:
        mult = (pnl_old / stake) + 1.0
    else:
        mult = float("nan")

    candidates = settlements_by_cell.get((cx, cy), [])
    ticks: list[tuple[int, float]] = []
    for c in candidates:
        ticks.extend(_settled_ticks(c))
    if not ticks:
        return (line.rstrip("\n") + "  RESCORE=no_ticks\n", "no_ticks")

    new_eu = touched_band(ticks, lo, hi)
    if new_eu == original_eu:
        return (line.rstrip("\n") + "  RESCORE=same\n", "same")

    # Replace the EU=WIN|LOSS token and pnl=$ value.
    new_eu_token = "WIN" if new_eu else "LOSS"
    if new_eu and not (mult == mult):  # nan check; we didn't know mult
        # We can't recover mult from a LOSS line without /multipliers data; leave pnl
        # as-is and tag for manual review.
        new_pnl = pnl_old
        out_line = re.sub(r"EU=(WIN|LOSS)", f"EU={new_eu_token}",
                          line.rstrip("\n"))
        return (out_line + "  RESCORE=changed_mult_unknown\n", "changed")
    new_pnl = (mult - 1) * stake if new_eu else -stake
    out_line = re.sub(r"EU=(WIN|LOSS)", f"EU={new_eu_token}", line.rstrip("\n"))
    out_line = re.sub(r"pnl=\$([+\-\d.]+)",
                      f"pnl=${new_pnl:+.4f}", out_line)
    return (out_line + "  RESCORE=changed\n", "changed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="bot log to re-score")
    ap.add_argument("--out", default="-", help="output path or '-' for stdout")
    ap.add_argument("--settlements",
                    default=os.path.join(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "logs", "settlements.jsonl"),
                    help="path to settlements.jsonl audit log")
    args = ap.parse_args()

    entries = load_settlements(args.settlements)
    by_cell: dict[tuple[int, int], list[dict]] = {}
    for e in entries:
        data = e.get("data") or {}
        # The cell coords aren't directly in settled records as of this writing;
        # we look them up under a few likely keys for forward-compat.
        for key_path in (("cellX", "cellY"),):
            cx = data.get(key_path[0])
            cy = data.get(key_path[1])
            if cx is not None and cy is not None:
                by_cell.setdefault((int(cx), int(cy)), []).append(e)
                break

    out_fh = sys.stdout if args.out == "-" else open(args.out, "w")
    counts = {"changed": 0, "same": 0, "no_ticks": 0, "no_match": 0}
    try:
        with open(args.log) as f:
            for line in f:
                new_line, marker = rescore(line, by_cell)
                counts[marker] = counts.get(marker, 0) + 1
                out_fh.write(new_line)
    finally:
        if out_fh is not sys.stdout:
            out_fh.close()

    print(f"[rescore] changed={counts['changed']} same={counts['same']} "
          f"no_ticks={counts['no_ticks']} no_match={counts['no_match']}",
          file=sys.stderr)
    if counts["no_ticks"] > 0:
        print("[rescore] note: 'no_ticks' rows fell outside the WS-tape "
              "retention window or settlements.jsonl lacks priceTicks. "
              "These are passed through unchanged.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
