"""Check the directional pattern: was bot picking cells AGAINST drift in Run #3?

Cell Y corresponds to a price band (cellY * 0.5 = band_lo).
If drift is strongly negative (price falling) AND bot picks cells with high Y
(above current price), it's betting on price going UP into the band — against
the trend.
"""
import re

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
                "cellY": int(m.group(2)),
                "p_pred": float(m.group(4)),
                "sigma": float(m.group(6)),
                "P_cb": float(m.group(8)),
                "drift": float(m.group(9)),
            })
            continue
        m = SETTLE_RE.search(line)
        if m:
            sets[(int(m.group(1)), int(m.group(2)))] = m.group(4)
    out = []
    seen = set()
    for s in sigs:
        # naive cell key — but cellX comes from log; re-parse
        pass
    # Re-parse with cellX so we can pair
    sigs2 = []
    for line in open(path):
        m = SIGNAL_RE.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            sigs2.append({
                "cellX": cx, "cellY": cy,
                "sigma": float(m.group(6)),
                "P_cb": float(m.group(8)),
                "drift": float(m.group(9)),
                "p_pred": float(m.group(4)),
            })
    for s in sigs2:
        k = (s["cellX"], s["cellY"])
        if k in sets and k not in seen:
            s["won"] = sets[k] == "won"
            band_lo = s["cellY"] * PRICE_INTERVAL
            band_hi = band_lo + PRICE_INTERVAL
            band_mid = (band_lo + band_hi) / 2
            s["band_offset"] = band_mid - s["P_cb"]   # +ve: band above price
            s["with_drift"] = (s["drift"] * s["band_offset"]) > 0  # trend pushes toward band
            out.append(s)
            seen.add(k)
    return out


def summary(label, bets):
    if not bets: return
    print(f"\n=== {label} (n={len(bets)}) ===")
    w_drift = [b for b in bets if b["with_drift"]]
    a_drift = [b for b in bets if not b["with_drift"]]
    print(f"  With drift (trend pushes toward band): n={len(w_drift)} "
          f"W={sum(1 for b in w_drift if b['won'])} "
          f"WR={100*sum(1 for b in w_drift if b['won'])/len(w_drift) if w_drift else 0:.1f}%")
    print(f"  Against drift (trend away from band):   n={len(a_drift)} "
          f"W={sum(1 for b in a_drift if b['won'])} "
          f"WR={100*sum(1 for b in a_drift if b['won'])/len(a_drift) if a_drift else 0:.1f}%")

    # Sign of band offset
    above = [b for b in bets if b["band_offset"] > 0]
    below = [b for b in bets if b["band_offset"] < 0]
    print(f"  Band above current price: n={len(above)} "
          f"WR={100*sum(1 for b in above if b['won'])/len(above) if above else 0:.1f}%")
    print(f"  Band below current price: n={len(below)} "
          f"WR={100*sum(1 for b in below if b['won'])/len(below) if below else 0:.1f}%")

    # Drift sign
    pos_drift = [b for b in bets if b["drift"] > 0]
    neg_drift = [b for b in bets if b["drift"] < 0]
    print(f"  Drift +ve (price rising): n={len(pos_drift)} "
          f"WR={100*sum(1 for b in pos_drift if b['won'])/len(pos_drift) if pos_drift else 0:.1f}%")
    print(f"  Drift -ve (price falling): n={len(neg_drift)} "
          f"WR={100*sum(1 for b in neg_drift if b['won'])/len(neg_drift) if neg_drift else 0:.1f}%")

    # Distribution
    offsets = sorted([b["band_offset"] for b in bets])
    drifts = sorted([b["drift"] for b in bets])
    print(f"  band_offset range: ${offsets[0]:+.2f} → ${offsets[-1]:+.2f}, "
          f"median ${offsets[len(offsets)//2]:+.2f}")
    print(f"  drift range:       ${drifts[0]:+.2f} → ${drifts[-1]:+.2f}, "
          f"median ${drifts[len(drifts)//2]:+.2f}")


run2 = parse("logs/LIVE_c30r-vw30_v2_20260522_185250.log")
run3 = parse("logs/LIVE_c30r-vw30_v3_20260522_193642.log")
summary("RUN #2 (winner)", run2)
summary("RUN #3 (loser)", run3)
