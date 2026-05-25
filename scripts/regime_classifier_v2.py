"""Regime classifier v2 — σ + KER (Kaufman Efficiency Ratio) combined.

For each time window, compute:
  σ_5min: realized vol per √sec
  KER_30min: |net_move| / sum(|step|) — 0=pure noise, 1=pure trend

Decision matrix:
                       σ_low (<0.10)    σ_mid (0.10-0.20)   σ_high (>0.20)
  KER_high (>0.30)     CALM-TREND        ★ TRENDING          VOLATILE
  KER_mid (0.10-0.30)  CALM-DRIFT        NEUTRAL              CHOPPY-FAST
  KER_low (<0.10)      DEAD              CHOPPY               ✗ KILL

We can validate by replaying bets through this and checking which regimes
the bot was profitable in.
"""
import json
import math
import sys
import bisect
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_price_tape(path):
    """Load EU price ticks (ts_ms, price) chronologically."""
    tape = []
    for line in open(path):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            tape.append((d["ts_ms"], d["price"]))
    return tape


def compute_sigma(tape, end_ms, lookback_sec=300):
    """Realized σ per √sec from 1-sec bucketed returns."""
    cutoff = end_ms - lookback_sec * 1000
    i0 = bisect.bisect_left([t for t, _ in tape], cutoff)
    i1 = bisect.bisect_right([t for t, _ in tape], end_ms)
    recent = tape[i0:i1]
    if len(recent) < 10: return None
    buckets = {}
    for ts, p in recent:
        buckets[ts // 1000] = p
    secs = sorted(buckets.keys())
    if len(secs) < 5: return None
    prices = [buckets[s] for s in secs]
    diffs = []
    for i in range(len(secs) - 1):
        dt = secs[i + 1] - secs[i]
        if dt <= 0 or dt > 3: continue
        diffs.append((prices[i + 1] - prices[i]) / math.sqrt(dt))
    if len(diffs) < 4: return None
    m = sum(diffs) / len(diffs)
    var = sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)
    return math.sqrt(var)


def compute_ker(tape, end_ms, lookback_sec=1800):
    """Kaufman Efficiency Ratio over `lookback_sec`."""
    cutoff = end_ms - lookback_sec * 1000
    ts_arr = [t for t, _ in tape]
    i0 = bisect.bisect_left(ts_arr, cutoff)
    i1 = bisect.bisect_right(ts_arr, end_ms)
    recent = tape[i0:i1]
    if len(recent) < 30: return None
    net = abs(recent[-1][1] - recent[0][1])
    path = sum(abs(recent[i + 1][1] - recent[i][1]) for i in range(len(recent) - 1))
    if path <= 0: return None
    return net / path


def classify(sigma, ker):
    if sigma is None or ker is None: return "NEUTRAL"
    if sigma < 0.10:
        if ker >= 0.30: return "CALM-TREND"
        if ker >= 0.10: return "CALM-DRIFT"
        return "DEAD"
    elif sigma < 0.20:
        if ker >= 0.30: return "TRENDING"
        if ker >= 0.10: return "NEUTRAL"
        return "CHOPPY"
    else:
        if ker >= 0.30: return "VOLATILE-TREND"
        if ker >= 0.10: return "CHOPPY-FAST"
        return "KILL-ZONE"


def parse_bets_from_bot_log(path):
    import re
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\).*mult=([\d.]+).*σ=([\d.]+).*drift_cb=\$([+-][\d.]+)")
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*EU=(WIN|LOSS).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    bets = []
    for line in open(path):
        m = sig_re.search(line)
        if m:
            sigs[(int(m.group(1)), int(m.group(2)))] = {
                "mq": float(m.group(3)), "sigma_bot": float(m.group(4)),
                "drift": float(m.group(5))}
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            s = sigs.get((cx, cy))
            if not s: continue
            rs = m.group(4)
            won = (rs == "WIN") if rs in ("WIN","LOSS") else (m.group(3) == "WIN")
            bets.append({"bet_time_s": cx * 5, "won": won, **s})
    return bets


def main():
    # Use today's data — 41k price ticks from cell_snapshots
    snapshot_path = sys.argv[1] if len(sys.argv) > 1 else "logs/cell_snapshots_20260524_224305.jsonl"
    bot_log = sys.argv[2] if len(sys.argv) > 2 else "logs/DRY_naked_2_20260524_224305.log"
    print(f"price tape: {snapshot_path}")
    print(f"bot log: {bot_log}\n", flush=True)

    tape = load_price_tape(snapshot_path)
    print(f"  loaded {len(tape)} price ticks", flush=True)
    bets = parse_bets_from_bot_log(bot_log)
    print(f"  parsed {len(bets)} bets", flush=True)
    if not tape or not bets:
        print("ERR: empty data"); return

    # Classify each bet by regime at its time
    DRAG = 0.06
    by_regime = defaultdict(list)
    sigmas = []
    kers = []
    for b in bets:
        end_ms = b["bet_time_s"] * 1000
        sig = compute_sigma(tape, end_ms)
        ker = compute_ker(tape, end_ms)
        if sig is not None: sigmas.append(sig)
        if ker is not None: kers.append(ker)
        regime = classify(sig, ker)
        b["regime"] = regime
        b["sig"] = sig
        b["ker"] = ker
        by_regime[regime].append(b)

    print(f"\n=== Distribution of σ and KER across bet times ===")
    if sigmas:
        sigmas.sort()
        print(f"  σ: p25={sigmas[len(sigmas)//4]:.3f} p50={sigmas[len(sigmas)//2]:.3f} "
              f"p75={sigmas[3*len(sigmas)//4]:.3f} p95={sigmas[int(0.95*len(sigmas))]:.3f}")
    if kers:
        kers.sort()
        print(f"  KER: p25={kers[len(kers)//4]:.3f} p50={kers[len(kers)//2]:.3f} "
              f"p75={kers[3*len(kers)//4]:.3f} p95={kers[int(0.95*len(kers))]:.3f}")

    print(f"\n=== Per-regime bet performance ===")
    print(f"{'regime':<18} {'n':>5} {'WR':>6} {'PnL_slip':>10} {'$/tap':>7} {'t':>6}")
    rows = []
    for regime, sub in sorted(by_regime.items(), key=lambda x: -len(x[1])):
        n = len(sub)
        if n == 0: continue
        w = sum(1 for b in sub if b["won"])
        pnls = [(b["mq"]-1)*(1-DRAG) if b["won"] else -1 for b in sub]
        cum = sum(pnls)
        mp = cum/n
        sd = (sum((p-mp)**2 for p in pnls)/max(n-1,1))**0.5
        t = (mp/sd*math.sqrt(n)) if sd > 0 else 0
        rows.append((regime, n, 100*w/n, cum, cum/n, t))
        print(f"{regime:<18} {n:>5} {100*w/n:>5.1f}% ${cum:>+9.2f} ${cum/n:>+6.3f} {t:>+5.2f}")
    return rows


if __name__ == "__main__":
    main()
