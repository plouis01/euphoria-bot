"""Evaluate candidate regime filters on Run #2 & Run #3 bets.

For each candidate filter we ask:
  - how many Run #2 (winning-regime) bets does it skip, and what PnL is lost?
  - how many Run #3 (losing-regime) bets does it skip, and what PnL is saved?

Filters covered:
  F1  one-way-trend gate: skip if last K drift readings all same-sign
        (computed from sequential SIGNAL lines, drift_cb field)
  F2  σ-stability gate: skip if rolling std(σ over last W bets) > τ
  F3  MM-implied-p check: skip if model_p - 1/mult < ε
  F4  realized-WR bandit: stop after rolling WR over last K resolved bets < τ
  F5  drift-vs-band magnitude gate: require |drift| ≤ k·band_offset
  F6  σ-range gate: keep σ ∈ [σ_lo, σ_hi]

Usage: python3 scripts/countermeasure_eval.py
"""
import re
from statistics import mean, pstdev

SIGNAL_RE = re.compile(
    r"SIGNAL.*cell=\((\d+),(\d+)\)\s+mult=([\d.]+)\s+p=([\d.]+)\s+ev=\+?(-?[\d.]+)\s+σ=([\d.]+)\s+P_eu=\$([\d.]+)\s+P_cb=\$([\d.]+)\s+drift_cb=\$(-?[\d.+]+)"
)
SETTLE_RE = re.compile(
    r"LIVE settled.*cell=\((\d+),(\d+)\)\s+mult=([\d.]+)\s+outcome=(\w+)\s+pnl=\$(-?[\d.]+)"
)
PRICE_INTERVAL = 0.5


def parse(path):
    sigs, sets = [], {}
    for line in open(path):
        m = SIGNAL_RE.search(line)
        if m:
            sigs.append({
                "cellX": int(m.group(1)),
                "cellY": int(m.group(2)),
                "mult": float(m.group(3)),
                "p_pred": float(m.group(4)),
                "ev": float(m.group(5)),
                "sigma": float(m.group(6)),
                "P_eu": float(m.group(7)),
                "P_cb": float(m.group(8)),
                "drift": float(m.group(9)),
            })
            continue
        m = SETTLE_RE.search(line)
        if m:
            sets[(int(m.group(1)), int(m.group(2)))] = {
                "outcome": m.group(4), "pnl": float(m.group(5)),
                "mult_filled": float(m.group(3)),
            }
    bets = []
    seen = set()
    for s in sigs:
        k = (s["cellX"], s["cellY"])
        if k in sets and k not in seen:
            s.update(sets[k])
            s["won"] = sets[k]["outcome"] == "won"
            band_lo = s["cellY"] * PRICE_INTERVAL
            band_mid = band_lo + PRICE_INTERVAL / 2
            s["band_offset"] = band_mid - s["P_cb"]
            s["implied_p"] = 1.0 / s["mult"]
            s["edge"] = s["p_pred"] - s["implied_p"]
            bets.append(s)
            seen.add(k)
    return bets


def report_filter(name, run2, run3, predicate):
    """predicate(bets, i) -> True if bet i should be SKIPPED."""
    for label, bets in [("RUN #2", run2), ("RUN #3", run3)]:
        kept = [b for i, b in enumerate(bets) if not predicate(bets, i)]
        skipped = [b for i, b in enumerate(bets) if predicate(bets, i)]
        if not bets:
            continue
        kw = sum(1 for b in kept if b["won"])
        kp = sum(b["pnl"] for b in kept)
        sw = sum(1 for b in skipped if b["won"])
        sp = sum(b["pnl"] for b in skipped)
        nk = len(kept)
        ns = len(skipped)
        print(f"  {label}: kept n={nk} W={kw} pnl=${kp:+.2f} | "
              f"skipped n={ns} W={sw} pnl_avoided=${-sp:+.2f}  "
              f"(WR_kept={100*kw/nk if nk else 0:.1f}%)")


def run_all():
    run2 = parse("logs/LIVE_c30r-vw30_v2_20260522_185250.log")
    run3 = parse("logs/LIVE_c30r-vw30_v3_20260522_193642.log")
    print(f"baseline run2: n={len(run2)} W={sum(1 for b in run2 if b['won'])} "
          f"pnl=${sum(b['pnl'] for b in run2):+.2f}")
    print(f"baseline run3: n={len(run3)} W={sum(1 for b in run3 if b['won'])} "
          f"pnl=${sum(b['pnl'] for b in run3):+.2f}")

    # --- F1: K-consecutive-same-sign drift gate, K=4..8 ---
    for K in (3, 4, 5, 6, 8):
        def pred(bs, i, K=K):
            if i < K - 1: return False
            window = [bs[j]["drift"] for j in range(i - K + 1, i + 1)]
            return all(d < 0 for d in window) or all(d > 0 for d in window)
        print(f"\nF1 one-way-trend gate (last {K} signed drifts all same sign):")
        report_filter(f"F1.K={K}", run2, run3, pred)

    # --- F2: σ-stability gate over last W bets, std-threshold ---
    for W, tau in ((5, 0.06), (5, 0.04), (8, 0.06), (8, 0.08)):
        def pred(bs, i, W=W, tau=tau):
            if i < W - 1: return False
            window = [bs[j]["sigma"] for j in range(i - W + 1, i + 1)]
            return pstdev(window) > tau
        print(f"\nF2 σ-stability gate (std of last {W} σ > {tau}):")
        report_filter(f"F2.W={W}.τ={tau}", run2, run3, pred)

    # --- F3: MM-implied-p edge gate (skip if model edge over MM small) ---
    for eps in (0.0, 0.05, 0.10, 0.15):
        def pred(bs, i, eps=eps):
            return bs[i]["edge"] < eps
        print(f"\nF3 MM-implied-p edge gate (skip if p_pred - 1/mult < {eps}):")
        report_filter(f"F3.eps={eps}", run2, run3, pred)

    # --- F4: rolling-WR bandit (need K resolved bets, stop if WR < τ) ---
    for K, tau in ((6, 0.15), (8, 0.15), (10, 0.20)):
        def pred(bs, i, K=K, tau=tau):
            if i < K: return False
            window = [bs[j] for j in range(i - K, i)]
            wr = sum(1 for b in window if b["won"]) / K
            return wr < tau
        print(f"\nF4 rolling-WR bandit (stop if last {K} bets WR < {tau}):")
        report_filter(f"F4.K={K}.τ={tau}", run2, run3, pred)

    # --- F5: |drift| / σ ratio ---
    for r in (2.0, 2.5, 3.0, 3.5):
        def pred(bs, i, r=r):
            b = bs[i]
            return abs(b["drift"]) / max(b["sigma"], 0.01) > r
        print(f"\nF5 |drift|/σ_30s > {r} (trend dominates noise):")
        report_filter(f"F5.r={r}", run2, run3, pred)

    # --- F6: σ window ---
    for lo, hi in ((0.06, 0.15), (0.08, 0.15), (0.06, 0.12)):
        def pred(bs, i, lo=lo, hi=hi):
            return not (lo <= bs[i]["sigma"] < hi)
        print(f"\nF6 σ ∈ [{lo}, {hi}):")
        report_filter(f"F6.{lo}-{hi}", run2, run3, pred)

    # --- COMBO: F1.K=5 AND F3.eps=0.10 (one-way trend + MM-edge gate) ---
    print("\nCOMBO A: F1.K=5 OR F3.eps=0.05")
    def predA(bs, i):
        K = 5
        if i >= K - 1:
            window = [bs[j]["drift"] for j in range(i - K + 1, i + 1)]
            if all(d < 0 for d in window) or all(d > 0 for d in window):
                return True
        return bs[i]["edge"] < 0.05
    report_filter("COMBO_A", run2, run3, predA)

    print("\nCOMBO B: F1.K=4 OR F4.K=8.τ=0.20")
    def predB(bs, i):
        K = 4
        if i >= K - 1:
            window = [bs[j]["drift"] for j in range(i - K + 1, i + 1)]
            if all(d < 0 for d in window) or all(d > 0 for d in window):
                return True
        if i >= 8:
            window = [bs[j] for j in range(i - 8, i)]
            wr = sum(1 for b in window if b["won"]) / 8
            if wr < 0.20:
                return True
        return False
    report_filter("COMBO_B", run2, run3, predB)

    print("\nCOMBO C: F1.K=5 OR F4.K=6.τ=0.15")
    def predC(bs, i):
        K = 5
        if i >= K - 1:
            window = [bs[j]["drift"] for j in range(i - K + 1, i + 1)]
            if all(d < 0 for d in window) or all(d > 0 for d in window):
                return True
        if i >= 6:
            window = [bs[j] for j in range(i - 6, i)]
            wr = sum(1 for b in window if b["won"]) / 6
            if wr < 0.15:
                return True
        return False
    report_filter("COMBO_C", run2, run3, predC)

    # --- Drift-sign autocorrelation (open question) ---
    print("\nDrift-sign run lengths (sequential SIGNAL drift sign):")
    for label, bets in [("RUN #2", run2), ("RUN #3", run3)]:
        if not bets: continue
        signs = [1 if b["drift"] > 0 else (-1 if b["drift"] < 0 else 0) for b in bets]
        runs = []
        cur = 1
        for i in range(1, len(signs)):
            if signs[i] == signs[i-1] and signs[i] != 0:
                cur += 1
            else:
                runs.append(cur); cur = 1
        runs.append(cur)
        flips = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i-1])
        print(f"  {label}: n_signals={len(signs)} sign_flips={flips} "
              f"runs={runs} max_run={max(runs)} "
              f"frac_neg={sum(1 for s in signs if s<0)/len(signs):.2f}")

    # --- MM edge distribution ---
    print("\nMM-edge (p_pred - 1/mult) summary:")
    for label, bets in [("RUN #2", run2), ("RUN #3", run3)]:
        if not bets: continue
        edges = [b["edge"] for b in bets]
        ew = [b["edge"] for b in bets if b["won"]]
        el = [b["edge"] for b in bets if not b["won"]]
        print(f"  {label}: mean={mean(edges):+.3f}  "
              f"winners_edge_mean={mean(ew) if ew else 0:+.3f}  "
              f"losers_edge_mean={mean(el) if el else 0:+.3f}")


if __name__ == "__main__":
    run_all()
