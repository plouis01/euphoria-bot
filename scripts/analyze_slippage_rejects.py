"""Post-hoc analyzer for slippage-rejected cells.

Tests the selection-bias hypothesis: do MM-rejected cells systematically have
HIGHER touch rates than accepted-and-settled cells? If yes, the MM is
censoring our winning bets, and our LIVE PnL underestimates the strategy's
true edge.

Reads:
  - logs/slippage_rejects.jsonl  (cells we attempted to tap but MM rejected)
  - logs/settlements.jsonl       (cells that actually executed and settled)

Resolves each rejected cell by:
  - Pulling /coinbase_tape from the running proxy (long buffer, ~3h)
  - If the cell's window is within the tape, segment-crossing resolver
    determines would-it-have-won.
  - Otherwise: marks "tape_expired" — needs to be run before the proxy tape
    rolls over (typically within ~30min of the session end).

Outputs:
  - logs/slippage_rejects_resolved.jsonl  (one entry per rejection w/ outcome)
  - stdout summary: WR by mult bucket, rejected vs settled comparison.

Usage:
    python scripts/analyze_slippage_rejects.py
    python scripts/analyze_slippage_rejects.py --proxy http://127.0.0.1:8787
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resolver import touched_band

PRICE_INTERVAL = 0.5
REJECTS_PATH = "logs/slippage_rejects.jsonl"
SETTLEMENTS_PATH = "logs/settlements.jsonl"
RESOLVED_PATH = "logs/slippage_rejects_resolved.jsonl"


def fetch_coinbase_tape(proxy: str) -> list[tuple[int, float]]:
    try:
        with urllib.request.urlopen(f"{proxy}/coinbase_tape", timeout=5) as r:
            d = json.loads(r.read())
    except Exception as e:
        print(f"[!] /coinbase_tape unreachable: {e}", file=sys.stderr)
        return []
    # The endpoint may return either a list directly or a dict with "ticks"/"trades"
    if isinstance(d, list):
        return d
    return d.get("ticks") or d.get("trades") or []


def resolve_rejection(rej: dict, tape: list[tuple[int, float]]) -> dict | None:
    """Resolve a single rejected cell. Returns rej dict augmented with
    would_have_won + n_ticks_in_window, or None if tape doesn't cover the
    window."""
    ws = rej.get("window_start_ms")
    we = rej.get("window_end_ms")
    cy = rej.get("cellY")
    if ws is None or we is None or cy is None:
        return None
    if not tape:
        return None
    # Tape format: [(ts_ms, price), ...]. Confirm window is within tape range.
    if tape[0][0] > ws or tape[-1][0] < we:
        return {**rej, "would_have_won": None, "tape_status": "window_outside_tape"}
    window_ticks = [(ts, p) for ts, p in tape if ws <= ts <= we]
    band_lo = cy * PRICE_INTERVAL
    band_hi = band_lo + PRICE_INTERVAL
    touched = touched_band(window_ticks, band_lo, band_hi)
    return {**rej, "would_have_won": bool(touched),
            "n_ticks_in_window": len(window_ticks),
            "band_lo": band_lo, "band_hi": band_hi,
            "tape_status": "resolved"}


def load_jsonl(path: str) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def settled_outcomes() -> list[dict]:
    """Parse settlements.jsonl for executed-and-settled positions, return
    [{mult, outcome, pnl, cellX, cellY}, ...]."""
    out = []
    for e in load_jsonl(SETTLEMENTS_PATH):
        if not e.get("matched_current_tap"):
            continue
        data = e.get("data") or {}
        settled = data.get("settled") or {}
        outcome = settled.get("outcome")
        if outcome not in ("won", "lost"):
            continue
        try:
            mult = float(settled.get("multiplier", 0))
        except (TypeError, ValueError):
            mult = 0
        try:
            pnl = float(settled.get("pnl", 0))
        except (TypeError, ValueError):
            pnl = 0
        out.append({
            "mult": mult,
            "won": outcome == "won",
            "pnl": pnl,
            "cellX": e.get("cellX"),
            "cellY": e.get("cellY"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:8787")
    args = ap.parse_args()

    rejects = load_jsonl(REJECTS_PATH)
    settled = settled_outcomes()
    print(f"Loaded: {len(rejects)} rejected, {len(settled)} settled")

    tape = fetch_coinbase_tape(args.proxy)
    print(f"Coinbase tape: {len(tape)} ticks")
    if tape:
        print(f"  spans ts {tape[0][0]} → {tape[-1][0]} "
              f"({(tape[-1][0] - tape[0][0]) / 60000:.1f} min)")

    resolved = []
    for r in rejects:
        r_out = resolve_rejection(r, tape)
        if r_out:
            resolved.append(r_out)

    # Persist
    with open(RESOLVED_PATH, "w") as f:
        for r in resolved:
            f.write(json.dumps(r) + "\n")
    print(f"Wrote {len(resolved)} resolved rejections to {RESOLVED_PATH}")

    # Stats
    in_tape = [r for r in resolved if r.get("tape_status") == "resolved"]
    if not in_tape:
        print("[!] No resolvable rejections (tape didn't cover any windows).")
        print("    Run this immediately after the LIVE session; the proxy")
        print("    Coinbase tape buffer rolls over.")
        return

    rej_w = sum(1 for r in in_tape if r["would_have_won"])
    rej_n = len(in_tape)
    set_w = sum(1 for s in settled if s["won"])
    set_n = len(settled)

    print()
    print("=" * 64)
    print(f"REJECTED (would-have-won):   {rej_w}/{rej_n} = "
          f"{100 * rej_w / rej_n:.1f}% WR")
    print(f"SETTLED  (actual):            {set_w}/{set_n} = "
          f"{100 * set_w / set_n if set_n else 0:.1f}% WR")
    print("=" * 64)
    if rej_n >= 5 and set_n >= 5:
        diff = (rej_w / rej_n) - (set_w / set_n)
        verdict = "MM is censoring winners" if diff > 0.05 else (
            "MM rejection ~ random" if abs(diff) <= 0.05 else
            "MM rejection FAVORS us (rejected cells lose more)")
        print(f"Verdict: {verdict}  (Δ WR = {diff:+.1%})")

    # By mult bucket
    print()
    print("By mult bucket:")
    buckets = [(1.2, 3), (3, 5), (5, 8), (8, 12), (12, 20), (20, 999)]
    header = f"  {'mult':<12} {'rej n':>6} {'rej WR':>8} {'set n':>6} {'set WR':>8}"
    print(header)
    for lo, hi in buckets:
        rj = [r for r in in_tape if lo <= r.get("mult_signed", 0) < hi]
        st = [s for s in settled if lo <= s["mult"] < hi]
        rj_wr = 100 * sum(1 for r in rj if r["would_have_won"]) / len(rj) if rj else None
        st_wr = 100 * sum(1 for s in st if s["won"]) / len(st) if st else None
        rj_wr_s = f"{rj_wr:.1f}%" if rj_wr is not None else "—"
        st_wr_s = f"{st_wr:.1f}%" if st_wr is not None else "—"
        print(f"  {lo:>5.1f}-{hi:<5.1f} {len(rj):>6} {rj_wr_s:>8} "
              f"{len(st):>6} {st_wr_s:>8}")


if __name__ == "__main__":
    main()
