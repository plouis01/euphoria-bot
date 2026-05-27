"""Build an empirical touch-prob calibration table from historical bets.

Replaces Gaussian touch_prob (which today over-predicted by 24-40pp) with
an actual observed WR per bucket: (mult_bin, sigma_bin, drift_sign).

Output: empirical_calibration.json
  schema: { "<mult_bin>_<sigma_bin>_<drift_sign>": {"n": int, "wr": float, "p_gauss_mean": float} }
"""
import json
import re
import statistics
from collections import defaultdict
from glob import glob
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIME_INTERVAL = 5

MULT_BINS = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 999.0]
SIGMA_BINS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.50, 999.0]
DRIFT_SIGNS = [-1, 0, 1]

OUT = ROOT / "empirical_calibration.json"


def bucket_idx(value, bins):
    for i, b in enumerate(bins):
        if value < b: return i
    return len(bins) - 1


def parse_bets(log_path):
    sig_re = re.compile(
        r"SIGNAL .*cell=\((\d+),(\d+)\) mult=([\d.]+) p=([\d.]+) ev=\+?(-?[\d.]+) "
        r"σ=([\d.]+) P_eu=\$([\d.]+) P_cb=\$([\d.]+) drift_cb=\$([+\-][\d.]+)"
    )
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    for line in open(log_path):
        m = sig_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs[(cx, cy)] = {
                "mq": float(m.group(3)), "p_gauss": float(m.group(4)),
                "sigma": float(m.group(6)), "drift": float(m.group(9)),
            }
    bets = []
    for line in open(log_path):
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sig = sigs.get((cx, cy))
            if not sig: continue
            rs = m.group(3)
            if rs not in ("WIN", "LOSS"): continue
            bets.append({"won": rs == "WIN", **sig})
    return bets


def main():
    # Use all bot logs we have (yesterday + today, all variants)
    all_bets = []
    for p in sorted(glob(str(ROOT / "logs/DRY_*_2026052[5-7]*.log"))):
        all_bets.extend(parse_bets(p))
    print(f"loaded {len(all_bets)} historical bets")

    # Build buckets
    buckets = defaultdict(list)
    for b in all_bets:
        mb = bucket_idx(b["mq"], MULT_BINS)
        sb = bucket_idx(b["sigma"], SIGMA_BINS)
        ds = 0 if abs(b["drift"]) < 0.1 else (1 if b["drift"] > 0 else -1)
        buckets[(mb, sb, ds)].append(b)

    # Build calibration table
    calib = {}
    for key, bs in buckets.items():
        mb, sb, ds = key
        n = len(bs)
        wins = sum(1 for b in bs if b["won"])
        wr = wins / n
        p_gauss_mean = statistics.mean(b["p_gauss"] for b in bs)
        # The key is the string "mult_idx_sigma_idx_drift_sign"
        sk = f"{mb}_{sb}_{ds}"
        calib[sk] = {"n": n, "wr": round(wr, 4),
                      "p_gauss_mean": round(p_gauss_mean, 4),
                      "gap_pp": round((p_gauss_mean - wr) * 100, 1)}

    # Also write the bin edges so the bot can reproduce bucketing
    output = {
        "mult_bins": MULT_BINS,
        "sigma_bins": SIGMA_BINS,
        "n_total": len(all_bets),
        "buckets": calib,
    }

    OUT.write_text(json.dumps(output, indent=2))
    print(f"wrote {OUT}")

    # Show coverage
    n_with_data = sum(1 for v in calib.values() if v["n"] >= 20)
    n_all = len(calib)
    print(f"\n=== bucket coverage ===")
    print(f"  buckets with n>=20: {n_with_data}/{n_all}")

    # Show top-populated buckets and their gaps
    print(f"\n=== top 15 populated buckets ===")
    top = sorted(calib.items(), key=lambda x: -x[1]["n"])[:15]
    print(f"  {'key':<12} {'n':>5} {'wr':>7} {'p_gauss':>9} {'gap_pp':>8}")
    for k, v in top:
        print(f"  {k:<12} {v['n']:>5} {v['wr']:>6.3f} {v['p_gauss_mean']:>9.3f} {v['gap_pp']:>+7.1f}")


if __name__ == "__main__":
    main()
