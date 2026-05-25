"""Regime classifier — distinguishes trending vs choppy markets.

Uses Kaufman Efficiency Ratio (KER):
  KER = |price_end - price_start| / sum(|price_step|) for a window
  KER = 1.0: pure trend (every tick moves in same direction)
  KER = 0.0: pure noise (price comes back to start exactly)

Plus σ (realized vol) as secondary filter.

Decision:
  TRENDING: KER >= ker_trend_threshold AND σ <= sigma_max
  CHOPPY:   KER < ker_chop_threshold OR σ > sigma_max
  NEUTRAL:  in between

A bot using --regime-gate can be configured to bet only in TRENDING regimes.
"""
import math
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RegimeClassifier:
    """Maintains rolling price ticks and computes regime label."""
    ker_window_sec: float = 1800.0       # 30 min lookback for KER
    sigma_window_sec: float = 300.0       # 5 min for σ
    ker_trend_threshold: float = 0.30     # >= this = trending
    ker_chop_threshold: float = 0.15      # < this = choppy
    sigma_max: float = 0.20               # σ above this kills trending
    min_ticks: int = 30                   # below this n, NEUTRAL

    # Rolling tick buffer: (ts, price)
    ticks: deque = field(default_factory=lambda: deque(maxlen=20000))

    def add_tick(self, ts: float, price: float) -> None:
        self.ticks.append((ts, price))

    def _window_ticks(self, lookback_sec: float) -> list[tuple[float, float]]:
        if not self.ticks: return []
        cutoff = self.ticks[-1][0] - lookback_sec
        return [t for t in self.ticks if t[0] >= cutoff]

    def kaufman_efficiency(self) -> float | None:
        w = self._window_ticks(self.ker_window_sec)
        if len(w) < self.min_ticks: return None
        net = abs(w[-1][1] - w[0][1])
        path = sum(abs(w[i + 1][1] - w[i][1]) for i in range(len(w) - 1))
        if path <= 0: return None
        return net / path

    def realized_sigma_per_sec(self) -> float | None:
        """Per-second σ from 5-min window, computed via 1-sec bucketed returns."""
        w = self._window_ticks(self.sigma_window_sec)
        if len(w) < self.min_ticks: return None
        # Bucket: last price per second
        buckets: dict[int, float] = {}
        for t, p in w:
            buckets[int(t)] = p
        secs = sorted(buckets.keys())
        if len(secs) < 5: return None
        prices = [buckets[s] for s in secs]
        diffs = []
        for i in range(len(secs) - 1):
            dt = secs[i + 1] - secs[i]
            if dt <= 0 or dt > 3: continue
            diffs.append((prices[i + 1] - prices[i]) / (dt ** 0.5))
        if len(diffs) < 4: return None
        m = sum(diffs) / len(diffs)
        var = sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)
        return math.sqrt(var)

    def classify(self) -> tuple[str, dict]:
        """Returns (label, debug_info)."""
        ker = self.kaufman_efficiency()
        sig = self.realized_sigma_per_sec()
        info = {"ker": ker, "sigma": sig}
        if ker is None or sig is None:
            return "NEUTRAL", info
        if ker >= self.ker_trend_threshold and sig <= self.sigma_max:
            return "TRENDING", info
        if ker < self.ker_chop_threshold or sig > self.sigma_max:
            return "CHOPPY", info
        return "NEUTRAL", info


# ============================================================================
# Backtest harness: replay a cell_snapshots_*.jsonl + tagged bets to score
# the classifier's predictive value.
# ============================================================================

def replay_regime_on_log(snapshot_path: str, bet_log: str, **classifier_kwargs):
    """For each bet in `bet_log`, look up the regime label at bet time using
    price data from `snapshot_path`. Returns per-regime aggregated stats."""
    import json
    import re
    # Load price ticks chronologically
    print(f"Loading price ticks from {snapshot_path}...", flush=True)
    rc = RegimeClassifier(**classifier_kwargs)
    # Build a price tape first
    tape = []
    for line in open(snapshot_path):
        try: d = json.loads(line)
        except: continue
        if d.get("t") == "p":
            tape.append((d["ts_ms"] / 1000.0, d["price"]))
    print(f"  {len(tape)} price ticks loaded", flush=True)

    # Parse bets from bot log
    sig_re = re.compile(r"SIGNAL .*cell=\((\d+),(\d+)\).*mult=([\d.]+)")
    res_re = re.compile(r"resolve cell=\((\d+),(\d+)\).*EU=(WIN|LOSS).*RS=(WIN|LOSS|N/A)")
    sigs = {}
    bets = []
    for line in open(bet_log):
        m = sig_re.search(line)
        if m: sigs[(int(m.group(1)), int(m.group(2)))] = float(m.group(3))
        m = res_re.search(line)
        if m:
            cx, cy = int(m.group(1)), int(m.group(2))
            mq = sigs.get((cx, cy))
            if mq is None: continue
            rs = m.group(4)
            won = (rs == "WIN") if rs in ("WIN","LOSS") else (m.group(3) == "WIN")
            bets.append({"bet_time": cx * 5, "mq": mq, "won": won})
    print(f"  {len(bets)} bets in {bet_log}", flush=True)

    # For each bet, replay ticks up to bet_time then classify
    bets.sort(key=lambda b: b["bet_time"])
    tape.sort()
    import bisect
    tape_times = [t[0] for t in tape]
    from collections import defaultdict
    by_regime = defaultdict(list)
    for b in bets:
        # Build classifier state at bet time
        idx = bisect.bisect_right(tape_times, b["bet_time"])
        # Use last 30min of ticks
        start = b["bet_time"] - 1800
        start_idx = bisect.bisect_left(tape_times, start)
        rc2 = RegimeClassifier(**classifier_kwargs)
        for ts, p in tape[start_idx:idx]:
            rc2.add_tick(ts, p)
        label, info = rc2.classify()
        b["regime"] = label
        b["ker"] = info.get("ker")
        b["sigma"] = info.get("sigma")
        by_regime[label].append(b)

    # Report
    DRAG = 0.06
    print(f"\n=== Per-regime aggregated stats ===")
    print(f"{'regime':<12} {'n':>5} {'WR':>6} {'PnL_slip':>10} {'$/tap':>7} {'t':>6}")
    for regime in ["TRENDING", "NEUTRAL", "CHOPPY"]:
        sub = by_regime.get(regime, [])
        if not sub: continue
        n = len(sub); w = sum(1 for b in sub if b["won"])
        pnls = [(b["mq"]-1)*(1-DRAG) if b["won"] else -1 for b in sub]
        cum = sum(pnls)
        mp = cum/n
        sd = (sum((p-mp)**2 for p in pnls)/max(n-1,1))**0.5
        t = (mp/sd*math.sqrt(n)) if sd > 0 else 0
        print(f"{regime:<12} {n:>5} {100*w/n:>5.1f}% ${cum:>+9.2f} ${cum/n:>+6.3f} {t:>+5.2f}")
    return by_regime


if __name__ == "__main__":
    import sys
    snapshot = sys.argv[1] if len(sys.argv) > 1 else "logs/cell_snapshots_20260524_224305.jsonl"
    bet_log = sys.argv[2] if len(sys.argv) > 2 else "logs/DRY_naked_2_20260524_224305.log"
    print(f"snapshot: {snapshot}")
    print(f"bet log: {bet_log}")
    print(f"\nClassifier params: KER trend>=0.30, KER chop<0.15, σ_max=0.20")
    replay_regime_on_log(snapshot, bet_log)
