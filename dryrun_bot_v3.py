"""
dryrun_bot_v3.py — multi-strategy Euphoria bot.

Improvements over v2:
  1. Coinbase ETH-USD feed added (RedStone-equivalent). Used as external
     drift reference AND outcome verifier (#6 — RedStone-equivalent ground truth).
  2. Dense EU price tape pulled from proxy's /price_tape (every WS tick),
     not the polled /state value (#2).
  3. Quote-freshness gate: skip if multiplier grid age > --max-quote-age-ms (#3).
  4. Strategies (--strategy):
       prob       — touch-prob model (v2 default)
       c90        — Edge C, τ=90s, |Coinbase-EU| ≥ --c-thr (default 0.10)
       c30-strong — Edge C, τ=30s, |Coinbase-EU| ≥ --c-thr-strong (default 0.40)
       combo      — c90 if signal present, else fall back to prob
  5. Calibrator: rolling per-p_pred-bin win-rate tracker. Logs current gap.
     Optionally auto-shifts min_p/max_p toward the best-calibrated bin
     when --adapt is set.

Dry-run by default. Pass --live to actually trade.
"""
import argparse
import json
import os
import signal
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field

# Use centralized proxy feeds (BinanceFeed/CoinbaseFeed both route through the
# signing_proxy's /binance_tape and /coinbase_tape endpoints).
from proxy_feed import BinanceFeed, CoinbaseFeed
from resolver import touched_band
from touch_prob import (touch_prob, simulate_heston_paths,
                        touch_prob_heston_from_paths, touch_prob_drift)


PROXY = os.environ.get("PROXY", "http://127.0.0.1:8787")
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5
KILL_FILE = "/tmp/euphoria_bot_kill"


def http_get(path: str):
    with urllib.request.urlopen(PROXY + path, timeout=3) as r:
        return json.loads(r.read())


def http_post(path: str, body: dict, timeout: float = 15.0):
    req = urllib.request.Request(
        PROXY + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


@dataclass
class PendingBet:
    cellX: int
    cellY: int
    mult: float
    stake: float
    window_start_ms: int
    window_end_ms: int
    p_pred: float
    ev_pred: float
    sigma: float
    strategy: str
    drift_at_bet: float


def realized_vol_from_ticks(ticks: list[tuple[int, float]],
                            window_ms: int, now_ms: int,
                            roll_correction: bool = False) -> float | None:
    """Per-second realized vol in $/√s. If roll_correction is True, applies
    Roll (1984) bid-ask bounce noise correction:
        σ²_real = σ²_obs + 2·Cov(r_t, r_{t-1})
    At HF the autocovariance is negative (bid-ask bounce), so corrected σ
    is smaller than naive σ — better matches true price-process vol."""
    cutoff = now_ms - window_ms
    sample = [(ts, p) for ts, p in ticks if ts >= cutoff]
    if len(sample) < 5:
        return None
    buckets: dict[int, float] = {}
    for ts, p in sample:
        buckets[ts // 1000] = p
    secs = sorted(buckets.keys())
    if len(secs) < 5:
        return None
    prices = [buckets[s] for s in secs]
    diffs = []
    for i in range(len(secs) - 1):
        dt = secs[i + 1] - secs[i]
        if dt <= 0 or dt > 3:
            continue
        diffs.append((prices[i + 1] - prices[i]) / (dt ** 0.5))
    if len(diffs) < 4:
        return None
    sigma_obs = statistics.stdev(diffs)
    if not roll_correction or len(diffs) < 6:
        return sigma_obs
    # Sample autocovariance of consecutive diffs (lag-1)
    n = len(diffs) - 1
    mean = sum(diffs) / len(diffs)
    cov1 = sum((diffs[i] - mean) * (diffs[i + 1] - mean) for i in range(n)) / n
    # Roll: σ²_real = σ²_obs + 2·cov1. If cov1 < 0 (bid-ask bounce), σ shrinks.
    var_adj = sigma_obs * sigma_obs + 2.0 * cov1
    if var_adj <= 0:
        # Heavy bounce; floor to 30% of observed
        return sigma_obs * 0.3
    return (var_adj) ** 0.5


# ---------------------------------------------------------------------------
# Calibrator — rolling per-p_pred-bin win-rate tracker (adaptive optional)
# ---------------------------------------------------------------------------

P_BINS = [(0.00, 0.50), (0.50, 0.70), (0.70, 0.80), (0.80, 0.85),
          (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]


@dataclass
class Calibrator:
    window: int = 100
    history: deque = field(default_factory=lambda: deque(maxlen=100))

    def record(self, p_pred: float, won: bool) -> None:
        self.history.append((p_pred, 1 if won else 0))

    def summary(self) -> list[dict]:
        out = []
        for lo, hi in P_BINS:
            bin_pts = [(p, w) for p, w in self.history if lo <= p < hi]
            n = len(bin_pts)
            if n == 0:
                out.append({"bin": (lo, hi), "n": 0, "actual": None, "gap": None})
                continue
            actual = sum(w for _, w in bin_pts) / n
            avg_p = sum(p for p, _ in bin_pts) / n
            out.append({"bin": (lo, hi), "n": n, "actual": actual,
                         "avg_pred": avg_p, "gap": avg_p - actual})
        return out

    def best_bin(self, min_n: int = 10) -> tuple[float, float] | None:
        """Return (lo, hi) of the bin with smallest |gap| and >= min_n samples."""
        best, best_gap = None, float("inf")
        for s in self.summary():
            if s["n"] < min_n or s["gap"] is None:
                continue
            g = abs(s["gap"])
            if g < best_gap:
                best_gap = g
                best = s["bin"]
        return best


# Regime-aware calibration. Buckets by (p_pred, σ, |drift|) — hierarchical fallback
# from 3D → (p,σ) → (p) → identity. Built to address the LATE-phase failure mode
# observed in Run #3 (3h dry-run): when avg drift ≈ $0.41 and σ ≈ 0.15, model
# over-predicted touch_prob by ~30pp. A 1D-only Calibrator can't see this because
# the gap is conditional on σ and drift, not on p alone.
SIGMA_BINS = [(0.0, 0.08), (0.08, 0.13), (0.13, 0.18), (0.18, 9.99)]
DRIFT_BINS = [(0.0, 0.30), (0.30, 0.45), (0.45, 9.99)]


def _which_bin(value: float, bins: list[tuple[float, float]]) -> int:
    for i, (lo, hi) in enumerate(bins):
        if lo <= value < hi:
            return i
    return len(bins) - 1


@dataclass
class Calibrator2:
    """Regime-aware Calibrator. Records per (p, σ, |drift|) bucket; produces
    an adjusted touch-prob using hierarchical fallback when a bucket is sparse.

    Usage:
      cal2.record(p_pred=0.45, sigma=0.15, drift=0.42, won=False)
      p_adj = cal2.adjust(p_pred=0.45, sigma=0.15, drift=0.42)
      # ev = mult * p_adj - 1
    """
    window: int = 200
    min_n: int = 8  # below this in a bucket, fall back to coarser bucket
    blend: float = 0.4  # weight on empirical actual_WR; 1 - blend stays at p_pred
    # history rows: (p_pred, sigma, drift_abs, won)
    history: deque = field(default_factory=lambda: deque(maxlen=200))

    def __post_init__(self):
        # default_factory hardcodes maxlen=200; resize to honor `window`
        if self.history.maxlen != self.window:
            self.history = deque(self.history, maxlen=self.window)

    def record(self, p_pred: float, sigma: float,
               drift: float, won: bool) -> None:
        self.history.append((p_pred, sigma, abs(drift), 1 if won else 0))

    def _empirical_wr(self, p_lo: float, p_hi: float,
                      s_idx: int | None, d_idx: int | None) -> tuple[float, int]:
        """Mean actual WR for the (optionally-coarsened) bucket. Returns (wr, n)."""
        wins = 0
        n = 0
        for p, s, d, w in self.history:
            if not (p_lo <= p < p_hi):
                continue
            if s_idx is not None and _which_bin(s, SIGMA_BINS) != s_idx:
                continue
            if d_idx is not None and _which_bin(d, DRIFT_BINS) != d_idx:
                continue
            wins += w
            n += 1
        if n == 0:
            return 0.0, 0
        return wins / n, n

    def adjust(self, p_pred: float, sigma: float, drift: float) -> float:
        """Return p_pred shrunk toward the empirical actual_WR for the best
        bucket we have enough data for. Hierarchical fallback:
          1. 3D (p, σ, |drift|) bucket  — needs n >= min_n
          2. 2D (p, σ) bucket
          3. 1D (p) bucket
          4. identity (return p_pred unchanged)
        Blend `blend` of empirical with `1 - blend` of model prediction.
        """
        # Locate the p_pred bin
        p_lo, p_hi = 0.0, 1.01
        for lo, hi in P_BINS:
            if lo <= p_pred < hi:
                p_lo, p_hi = lo, hi
                break
        s_idx = _which_bin(abs(sigma), SIGMA_BINS)
        d_idx = _which_bin(abs(drift), DRIFT_BINS)

        # 1) 3D
        wr, n = self._empirical_wr(p_lo, p_hi, s_idx, d_idx)
        if n >= self.min_n:
            return self.blend * wr + (1 - self.blend) * p_pred
        # 2) 2D (p, σ)
        wr, n = self._empirical_wr(p_lo, p_hi, s_idx, None)
        if n >= self.min_n:
            return self.blend * wr + (1 - self.blend) * p_pred
        # 3) 1D (p)
        wr, n = self._empirical_wr(p_lo, p_hi, None, None)
        if n >= self.min_n:
            return self.blend * wr + (1 - self.blend) * p_pred
        # 4) identity (cold start)
        return p_pred

    def gap_summary(self, top_k: int = 5) -> str:
        """Compact dump: top-k 3D buckets by |gap|, useful for periodic logging."""
        rows = []
        for s_idx in range(len(SIGMA_BINS)):
            for d_idx in range(len(DRIFT_BINS)):
                for p_lo, p_hi in P_BINS:
                    wr, n = self._empirical_wr(p_lo, p_hi, s_idx, d_idx)
                    if n < self.min_n:
                        continue
                    # average p in this bucket — for gap reporting
                    ps = [p for p, s, d, _ in self.history
                          if p_lo <= p < p_hi
                          and _which_bin(s, SIGMA_BINS) == s_idx
                          and _which_bin(d, DRIFT_BINS) == d_idx]
                    avg_p = sum(ps) / len(ps) if ps else (p_lo + p_hi) / 2
                    rows.append((avg_p - wr, p_lo, p_hi, s_idx, d_idx, n, wr, avg_p))
        rows.sort(key=lambda r: -abs(r[0]))
        if not rows:
            return "(no buckets above min_n)"
        out = []
        for gap, p_lo, p_hi, s_idx, d_idx, n, wr, avg_p in rows[:top_k]:
            sl, sh = SIGMA_BINS[s_idx]
            dl, dh = DRIFT_BINS[d_idx]
            out.append(f"p[{p_lo:.2f}-{p_hi:.2f}) σ[{sl:.2f}-{sh:.2f}) "
                       f"d[{dl:.2f}-{dh:.2f}) n={n} avg_p={avg_p:.2f} "
                       f"actual_WR={wr:.2f} gap={gap:+.2f}")
        return " | ".join(out)


# ---------------------------------------------------------------------------
# Strategy: Edge C — OOM cell in direction of (external - EU) drift
# ---------------------------------------------------------------------------

class LiveLedger:
    """LIVE-mode accounting source-of-truth. Polls the proxy's /settlements
    endpoint (which writes a record per on-chain settled position) and updates
    wins/losses/realized_pnl independently of the /tap response. Required
    because /tap frequently returns 504/timeout even when the position
    eventually settles — historic source of "bot blind to its own taps".

    Idempotent on tradeId: each settlement counted once regardless of how
    many times poll() sees it.
    """

    def __init__(self):
        self.seen_trade_ids: set[str] = set()
        self.wins = 0
        self.losses = 0
        self.realized_pnl = 0.0
        self.last_balance: float | None = None

    def prime(self, http_get_fn) -> None:
        """At startup: record all pre-existing tradeIds as 'already seen' so
        we don't count old positions from a previous session as this bot's."""
        try:
            resp = http_get_fn("/settlements?n=500")
            for e in resp.get("entries", []):
                tid = e.get("tradeId")
                if tid:
                    self.seen_trade_ids.add(tid)
        except Exception as e:
            print(f"[ledger] prime failed: {e}", flush=True)

    def poll(self, http_get_fn, tag: str = "") -> list[dict]:
        """Fetch new settlements; update counters; return list of new entries
        (each entry as the proxy schema, useful for callers to log)."""
        new_entries: list[dict] = []
        try:
            resp = http_get_fn("/settlements?n=200")
            entries = resp.get("entries", []) or []
        except Exception as e:
            print(f"{tag}[ledger] poll failed: {e}", flush=True)
            return new_entries
        for e in entries:
            tid = e.get("tradeId")
            if not tid or tid in self.seen_trade_ids:
                continue
            # Only count entries the proxy matched to one of our /tap calls.
            # `matched_current_tap=True` means the settle event arrived while
            # STATE.current_tap was set; we treat these as ours.
            if not e.get("matched_current_tap"):
                self.seen_trade_ids.add(tid)  # mark seen but don't count
                continue
            data = (e.get("data") or {})
            settled = data.get("settled") or {}
            outcome = settled.get("outcome")  # "won" / "lost"
            try:
                pnl = float(settled.get("pnl", 0.0))
            except (TypeError, ValueError):
                pnl = 0.0
            ub = settled.get("updatedBalance")
            try:
                self.last_balance = float(ub) if ub is not None else self.last_balance
            except (TypeError, ValueError):
                pass
            if outcome == "won":
                self.wins += 1
            elif outcome == "lost":
                self.losses += 1
            else:
                # No outcome field — ignore but still mark seen.
                self.seen_trade_ids.add(tid)
                continue
            self.realized_pnl += pnl
            self.seen_trade_ids.add(tid)
            new_entries.append(e)
        return new_entries


def pick_edge_c_cell(cells, P_now, signal, t_idx_target,
                     min_mult, max_mult, mean_rev: bool = False):
    """Pick OOM cell at the given τ in the signal direction, nearest to target.

    If mean_rev is True, bet AGAINST the drift direction — challenger
    `c30s-meanrev` (Lane 4): tests whether c30s's edge is continuation
    or counter-trend retrace.
    """
    eff_signal = -signal if mean_rev else signal
    direction = 1 if eff_signal > 0 else -1
    target = P_now + eff_signal
    best = None
    best_d = float("inf")
    for c in cells:
        cx, cy, m = c["cellX"], c["cellY"], c["mult"]
        if m < min_mult or m > max_mult:
            continue
        a = cy * PRICE_INTERVAL
        b = a + PRICE_INTERVAL
        # Match research: cell must be in drift direction relative to P_now
        if direction > 0:
            if not (a > P_now and a <= target + PRICE_INTERVAL):
                continue
        else:
            if not (b <= P_now and b >= target - PRICE_INTERVAL):
                continue
        d = abs(((a + b) / 2) - target)
        if d < best_d:
            best_d = d
            best = {"cx": cx, "cy": cy, "mult": m, "a": a, "b": b}
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--stake", type=float, default=1.0)
    ap.add_argument("--slippage", type=int, default=60)
    ap.add_argument("--max-taps", type=int, default=2000)
    ap.add_argument("--max-loss", type=float, default=9999)
    ap.add_argument("--max-gain", type=float, default=99999,
                    help="Stop when realized_pnl >= this (LIVE: from ledger; "
                         "DRY: from balance delta). Symmetric to max-loss.")
    ap.add_argument("--cadence-sec", type=float, default=8.0)
    ap.add_argument("--ev-threshold", type=float, default=0.05)
    ap.add_argument("--vol-window-sec", type=float, default=60.0)
    ap.add_argument("--max-drift", type=float, default=5.0,
                    help="pause if |Coinbase - EU| > this (real drift gate, "
                         "uses Coinbase not Binance to avoid USDT basis)")
    ap.add_argument("--max-euphoria-price-age-s", type=float, default=5.0)
    ap.add_argument("--max-quote-age-ms", type=int, default=400,
                    help="skip if MM grid age > this (freshness gate, #3)")
    ap.add_argument("--max-time-out-sec", type=float, default=120.0)
    ap.add_argument("--min-mult", type=float, default=1.1)
    ap.add_argument("--max-mult", type=float, default=1000.0)
    ap.add_argument("--min-p", type=float, default=0.0)
    ap.add_argument("--max-p", type=float, default=1.01)
    ap.add_argument("--strategy", choices=["prob", "c90", "c30-strong",
                                             "c30-strong-meanrev",
                                             "c30-strong-quotedrop",
                                             "b", "combo"],
                    default="prob")
    ap.add_argument("--min-quote-age-ms", type=int, default=0,
                    help="ONLY bet when quote age >= this (Lane-4 challenger "
                         "c30s-quotedrop: MM is almost stale, under-reacts).")
    ap.add_argument("--c-thr", type=float, default=0.10,
                    help="|Coinbase - EU| ≥ this for Edge C90 to fire")
    ap.add_argument("--c-thr-strong", type=float, default=0.40,
                    help="|Coinbase - EU| ≥ this for Edge C30-strong to fire")
    ap.add_argument("--c-tau-sec", type=int, default=90,
                    help="Edge C90 expiry (sec from now); finds cell with cx*5≈now+τ")
    ap.add_argument("--c-tau-strong-sec", type=int, default=30,
                    help="Edge C30-strong expiry (sec from now)")
    # ---- Edge B: vol-regime broad-OOM ----
    ap.add_argument("--b-vol-threshold", type=float, default=0.12,
                    help="trigger Edge B when realized σ ≥ this $/√s (high-vol regime)")
    ap.add_argument("--b-mult-min", type=float, default=2.0,
                    help="Edge B cell multiplier floor (per research: [2, 5) sweet spot)")
    ap.add_argument("--b-mult-max", type=float, default=5.0,
                    help="Edge B cell multiplier ceiling")
    ap.add_argument("--b-target-mult", type=float, default=3.0,
                    help="Edge B picks cell whose mult is closest to this target")
    ap.add_argument("--signal-source", choices=["binance", "coinbase", "euphoria"],
                    default="coinbase",
                    help="external price feed for drift signal & vol")
    ap.add_argument("--adapt", action="store_true",
                    help="auto-shift min_p/max_p toward best-calibrated bin "
                         "after 30+ resolved taps")
    ap.add_argument("--min-quote-mult-change-pct", type=float, default=0.0,
                    help="only bet on cells whose mult moved by >=this fraction "
                         "within --quote-change-window-sec (e.g. 0.05 = 5%%). "
                         "Filters out stale, unchanged MM quotes.")
    ap.add_argument("--quote-change-window-sec", type=float, default=15.0,
                    help="how far back to compare cell mults for the change filter")
    ap.add_argument("--vol-df", type=float, default=0.0,
                    help="touch-prob model: 0 = Gaussian (default), >2 = Student-t "
                         "(leptokurtic, fatter tails). Typical ν for HF crypto: 4-5.")
    ap.add_argument("--vol-model", choices=["auto", "gauss", "t", "heston"],
                    default="auto",
                    help="touch-prob model. 'auto' uses --vol-df. 'heston' enables "
                         "stochastic-vol Monte Carlo paths shared across cells per cadence.")
    ap.add_argument("--heston-kappa", type=float, default=2.0,
                    help="Heston mean-reversion speed (1/s)")
    ap.add_argument("--heston-xi", type=float, default=0.5,
                    help="Heston vol-of-vol")
    ap.add_argument("--heston-rho", type=float, default=0.0,
                    help="Heston leverage correlation (typically 0 to -0.3)")
    ap.add_argument("--heston-paths", type=int, default=400,
                    help="Heston Monte Carlo paths per cadence (shared across cells)")
    ap.add_argument("--heston-dt", type=float, default=0.25,
                    help="Heston path time step (sec)")
    ap.add_argument("--ev-haircut", type=float, default=0.0,
                    help="Adverse-selection haircut: subtract this from ev before "
                         "threshold check (Cartea-Jaimungal style; default 0.0).")
    ap.add_argument("--calibrator2", action="store_true",
                    help="Use regime-aware Calibrator2: shrink p_pred toward "
                         "historical actual_WR for the (p, σ, |drift|) bucket. "
                         "Fixes the LATE-phase overconfidence observed when "
                         "drift jumps and σ stays moderate.")
    ap.add_argument("--cal2-window", type=int, default=200,
                    help="Calibrator2 rolling history length (default 200).")
    ap.add_argument("--cal2-blend", type=float, default=0.4,
                    help="Calibrator2 blend weight on empirical actual_WR; "
                         "1-blend stays at p_pred. Default 0.4.")
    ap.add_argument("--cal2-min-n", type=int, default=8,
                    help="Calibrator2 min samples per bucket before using it "
                         "(falls back to coarser bucket otherwise). Default 8.")
    ap.add_argument("--roll-correction", action="store_true",
                    help="Apply Roll (1984) bid-ask bounce correction to realized σ.")
    # ---- F1: one-way-trend gate ----
    ap.add_argument("--trend-gate-k", type=int, default=0,
                    help="F1 filter: skip tap if last K signal-time drift_cb "
                         "values are all strictly same-sign (nonzero). 0=off. "
                         "Research recommends 5.")
    # ---- F4: rolling-WR bandit ----
    ap.add_argument("--bandit-window", type=int, default=0,
                    help="F4 filter: rolling-WR bandit window over last K "
                         "resolved outcomes. 0=off. Research recommends 6.")
    ap.add_argument("--bandit-floor", type=float, default=0.15,
                    help="F4 trips when rolling WR < this. Default 0.15.")
    ap.add_argument("--bandit-pause-sec", type=float, default=300.0,
                    help="F4 pause duration when triggered. Default 300s.")
    # ---- R1: σ ceiling (kill-zone filter, per evening analysis 2026-05-22) ----
    ap.add_argument("--max-sigma", type=float, default=999.0,
                    help="R1 filter: skip if σ_short > this. Per evening "
                         "analysis, σ ≥ 0.15 had 5.6%% WR / -$14.60 across "
                         "18 bets. 0.15 recommended.")
    # ---- R3: |drift| ceiling (kill-zone filter) ----
    ap.add_argument("--max-abs-drift", type=float, default=999.0,
                    help="R3 filter: skip if |drift_cb| > this. Per analysis, "
                         "|drift|>$0.50 had 0%% WR (n=7) excluding outlier. "
                         "0.50 recommended.")
    # ---- State persistence (survive bot restart without warmup-loss tax) ----
    ap.add_argument("--state-file", type=str,
                    default="logs/bot_filter_state.json",
                    help="Persist F1 drift_streak + F4 outcome_history here. "
                         "Loaded on startup if entries < --state-stale-sec.")
    ap.add_argument("--state-stale-sec", type=float, default=600.0,
                    help="Drop persisted state entries older than this. "
                         "Default 600s.")
    # ---- v9: drift-aware touch-probability model ----
    ap.add_argument("--drift-aware-touch", action="store_true",
                    help="v9: use touch_prob_drift() with μ = drift_cb / "
                         "drift-conv-sec. Down-scores wrong-direction cells.")
    ap.add_argument("--drift-conv-sec", type=float, default=2.0,
                    help="Seconds over which drift_cb converges to zero. "
                         "Higher = weaker drift signal. Default 2.0.")
    ap.add_argument("--max-drift-mu", type=float, default=0.5,
                    help="Safety clamp on |μ| in $/sec. Default 0.5.")
    # ---- v8: regime switcher (HOT/COLD modes) ----
    ap.add_argument("--regime-switch", action="store_true",
                    help="v8: enable regime switcher. HOT mode = no σ filter; "
                         "COLD mode = σ-ceiling=rs-cold-sigma-cap. "
                         "Transitions driven by rolling WR + rolling σ.")
    ap.add_argument("--rs-window", type=int, default=30,
                    help="Rolling-WR window over last K resolved bets.")
    ap.add_argument("--rs-sigma-window-sec", type=float, default=300.0,
                    help="Rolling-σ averaging window (seconds of signals).")
    ap.add_argument("--rs-hot-to-cold-wr", type=float, default=0.07,
                    help="HOT→COLD trigger: rolling WR below this.")
    ap.add_argument("--rs-hot-to-cold-sigma", type=float, default=0.18,
                    help="HOT→COLD trigger: rolling avg σ above this.")
    ap.add_argument("--rs-cold-to-hot-wr", type=float, default=0.10,
                    help="COLD→HOT trigger: rolling WR above this (AND σ ok).")
    ap.add_argument("--rs-cold-to-hot-sigma", type=float, default=0.15,
                    help="COLD→HOT trigger: rolling avg σ below this (AND WR ok).")
    ap.add_argument("--rs-cold-sigma-cap", type=float, default=0.12,
                    help="σ ceiling enforced ONLY in COLD mode "
                         "(W3_R1_strict best variant in HOUR_FINAL).")
    ap.add_argument("--rs-start-mode", choices=["HOT", "COLD"], default="HOT",
                    help="Starting mode (default HOT — unfiltered c30-strong).")
    ap.add_argument("--tag", type=str, default="",
                    help="prefix for log lines (helps distinguish parallel runs)")
    args = ap.parse_args()

    TAG = f"[{args.tag}] " if args.tag else ""

    binance = BinanceFeed()
    binance.start()
    coinbase = CoinbaseFeed()
    coinbase.start()
    print(f"{TAG}[bot] warming up Binance + Coinbase 5s...", flush=True)
    time.sleep(5)
    if not binance.connected:
        print(f"{TAG}[bot] FATAL: Binance WS not connected: {binance.last_error}", flush=True)
        sys.exit(1)
    if not coinbase.connected:
        print(f"{TAG}[bot] FATAL: Coinbase WS not connected: {coinbase.last_error}", flush=True)
        sys.exit(1)
    print(f"{TAG}[bot] Binance msgs={binance.msg_count}  Coinbase msgs={coinbase.msg_count}",
          flush=True)

    state0 = http_get("/state")
    bal0 = state0.get("balance")
    if bal0 is None:
        chart0 = http_get("/chart")
        bal0 = chart0.get("balance")
    if bal0 is None:
        print(f"{TAG}[bot] FATAL: balance missing — reload Euphoria page.", flush=True)
        binance.stop(); coinbase.stop()
        sys.exit(1)
    starting_balance = float(bal0)

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"{TAG}[bot] {mode} strategy={args.strategy} signal_source={args.signal_source} "
          f"starting=${starting_balance:.4f} stake=${args.stake} "
          f"ev_thr={args.ev_threshold} max_quote_age={args.max_quote_age_ms}ms",
          flush=True)

    last_tap_ts = 0.0
    taps = 0
    wins_eu = losses_eu = 0
    wins_cb = losses_cb = 0  # Coinbase ground-truth (RedStone-equivalent)
    pending: list[PendingBet] = []
    cum_ev_predicted = 0.0
    calibrator = Calibrator()
    cal2 = Calibrator2(window=args.cal2_window, min_n=args.cal2_min_n,
                       blend=args.cal2_blend) if args.calibrator2 else None
    # LIVE source-of-truth: poll /settlements for on-chain outcomes.
    # /tap responses are unreliable (frequent 504s on successful settlements);
    # the ledger reads the proxy's persistent settlement log and counts each
    # tradeId at most once. wins_eu/losses_eu/realized_pnl reflect REAL on-chain
    # outcomes, so max-loss gate works correctly.
    ledger = LiveLedger() if args.live else None
    if ledger is not None:
        ledger.prime(http_get)
        print(f"{TAG}[bot] LiveLedger primed with "
              f"{len(ledger.seen_trade_ids)} pre-existing settlements", flush=True)
    slippage_rejects = 0
    live_attempts = 0  # /tap POSTs we made (success+error)
    # Per-cell mult history for --min-quote-mult-change-pct filter.
    # Keyed by (cellX, cellY); each value is a deque of (ts_ms, mult) entries.
    cell_mult_history: dict[tuple[int, int], deque] = {}
    MULT_HISTORY_MAXLEN = 40  # ~5 min at 8s cadence

    # ---- F1 + F4 regime filters ----
    drift_streak: deque = deque(maxlen=args.trend_gate_k) \
        if args.trend_gate_k > 0 else deque(maxlen=1)
    outcome_history: deque = deque(maxlen=args.bandit_window) \
        if args.bandit_window > 0 else deque(maxlen=1)
    bandit_paused_until = 0.0
    f1_skips = 0
    f4_trips = 0
    r1_skips = 0  # σ ceiling
    r3_skips = 0  # |drift| ceiling

    # ---- v8 regime switcher ----
    rs_mode = args.rs_start_mode  # "HOT" or "COLD"
    rs_outcomes: deque = deque(maxlen=args.rs_window) if args.regime_switch else deque(maxlen=1)
    rs_sigmas: deque = deque() if args.regime_switch else deque()  # (ts, sigma)
    rs_transitions = 0
    rs_cold_skips = 0  # bets skipped because in COLD with sigma > cap

    def rs_avg_sigma_window() -> float:
        cutoff = time.time() - args.rs_sigma_window_sec
        while rs_sigmas and rs_sigmas[0][0] < cutoff:
            rs_sigmas.popleft()
        if not rs_sigmas:
            return 0.0
        return sum(s for _, s in rs_sigmas) / len(rs_sigmas)

    def rs_eval_mode() -> None:
        """Re-evaluate mode based on rolling WR + rolling σ. Mutates rs_mode."""
        nonlocal rs_mode, rs_transitions
        if not args.regime_switch:
            return
        if len(rs_outcomes) < args.rs_window:
            return  # not enough data yet, stay in starting mode
        wr = sum(rs_outcomes) / len(rs_outcomes)
        avg_sigma = rs_avg_sigma_window()
        if rs_mode == "HOT":
            if wr < args.rs_hot_to_cold_wr or avg_sigma > args.rs_hot_to_cold_sigma:
                rs_mode = "COLD"
                rs_transitions += 1
                print(f"{TAG}[bot] v8 REGIME HOT→COLD "
                      f"wr_{args.rs_window}={wr:.2%} "
                      f"avg_σ={avg_sigma:.3f} "
                      f"(triggers wr<{args.rs_hot_to_cold_wr} or σ>{args.rs_hot_to_cold_sigma})",
                      flush=True)
        else:  # COLD
            if wr > args.rs_cold_to_hot_wr and avg_sigma < args.rs_cold_to_hot_sigma:
                rs_mode = "HOT"
                rs_transitions += 1
                print(f"{TAG}[bot] v8 REGIME COLD→HOT "
                      f"wr_{args.rs_window}={wr:.2%} "
                      f"avg_σ={avg_sigma:.3f}",
                      flush=True)

    # Load persisted state (drift_streak ts-tagged tuples, outcome ts-tagged)
    if args.state_file and os.path.exists(args.state_file):
        try:
            with open(args.state_file) as f:
                st_persist = json.load(f)
            cutoff = time.time() - args.state_stale_sec
            if args.trend_gate_k > 0:
                kept_drifts = [(t, d) for t, d in st_persist.get("drift_streak", [])
                               if t >= cutoff]
                # Keep only the most recent K entries
                for _, d in kept_drifts[-args.trend_gate_k:]:
                    drift_streak.append(d)
                print(f"{TAG}[bot] F1 state loaded: "
                      f"{len(drift_streak)} drifts from {args.state_file} "
                      f"(filtered {len(st_persist.get('drift_streak', [])) - len(kept_drifts)} stale)",
                      flush=True)
            if args.bandit_window > 0:
                kept_outc = [(t, o) for t, o in st_persist.get("outcomes", [])
                             if t >= cutoff]
                for _, o in kept_outc[-args.bandit_window:]:
                    outcome_history.append(o)
                print(f"{TAG}[bot] F4 state loaded: "
                      f"{len(outcome_history)} outcomes "
                      f"(filtered {len(st_persist.get('outcomes', [])) - len(kept_outc)} stale)",
                      flush=True)
        except Exception as e:
            print(f"{TAG}[bot] state-file load failed: {e}", flush=True)

    def save_filter_state() -> None:
        if not args.state_file:
            return
        try:
            with open(args.state_file, "w") as f:
                json.dump({
                    "drift_streak": [(time.time(), d) for d in drift_streak],
                    "outcomes": [(time.time(), o) for o in outcome_history],
                }, f)
        except Exception:
            pass  # non-critical

    if args.trend_gate_k > 0:
        print(f"{TAG}[bot] F1 trend-gate ARMED K={args.trend_gate_k}", flush=True)
    if args.bandit_window > 0:
        print(f"{TAG}[bot] F4 bandit ARMED window={args.bandit_window} "
              f"floor={args.bandit_floor} pause={args.bandit_pause_sec}s",
              flush=True)
    if args.max_sigma < 99:
        print(f"{TAG}[bot] R1 σ-ceiling ARMED max_sigma={args.max_sigma}",
              flush=True)
    if args.max_abs_drift < 99:
        print(f"{TAG}[bot] R3 |drift|-ceiling ARMED max_abs_drift=${args.max_abs_drift}",
              flush=True)
    if args.drift_aware_touch:
        print(f"{TAG}[bot] v9 DRIFT-AWARE TOUCH ARMED "
              f"conv_sec={args.drift_conv_sec} max_|μ|=${args.max_drift_mu}/s",
              flush=True)
    if args.regime_switch:
        print(f"{TAG}[bot] v8 REGIME-SWITCH ARMED start={rs_mode} "
              f"win={args.rs_window} "
              f"H→C: wr<{args.rs_hot_to_cold_wr} or σ>{args.rs_hot_to_cold_sigma} | "
              f"C→H: wr>{args.rs_cold_to_hot_wr} and σ<{args.rs_cold_to_hot_sigma} | "
              f"cold σ-cap={args.rs_cold_sigma_cap}",
              flush=True)

    stop = {"flag": False}
    def on_sig(*_):
        stop["flag"] = True
        print(f"\n{TAG}[bot] stop signal", flush=True)
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        while not stop["flag"]:
            if os.path.exists(KILL_FILE):
                print(f"{TAG}[bot] kill file present, stopping", flush=True)
                break

            # ----- LIVE: poll on-chain settlements (source of truth) -----
            if ledger is not None:
                for e in ledger.poll(http_get, tag=TAG):
                    data = (e.get("data") or {})
                    settled = data.get("settled") or {}
                    outcome = settled.get("outcome")
                    pnl = settled.get("pnl")
                    mult = settled.get("multiplier")
                    tid = (e.get("tradeId") or "")[:14]
                    print(f"{TAG}[bot] LIVE settled tradeId={tid} "
                          f"cell=({e.get('cellX')},{e.get('cellY')}) "
                          f"mult={mult} outcome={outcome} pnl=${pnl} "
                          f"bal=${ledger.last_balance} "
                          f"w/l={ledger.wins}/{ledger.losses} "
                          f"realized=${ledger.realized_pnl:+.4f}", flush=True)
                    # Drive Calibrator from real outcomes too (for LIVE-mode
                    # calibration data).
                    if outcome in ("won", "lost"):
                        # We don't have the original p_pred for this settlement;
                        # the proxy doesn't carry it. Skip cal training here.
                        pass
                    # v8 regime switcher: feed LIVE outcome into rolling-WR.
                    if args.regime_switch and outcome in ("won", "lost"):
                        rs_outcomes.append(1 if outcome == "won" else 0)
                        rs_eval_mode()
                    # F4 bandit: feed resolved outcome into rolling-WR window.
                    if args.bandit_window > 0 and outcome in ("won", "lost"):
                        outcome_history.append(1 if outcome == "won" else 0)
                        save_filter_state()
                        if len(outcome_history) == args.bandit_window:
                            wr_recent = sum(outcome_history) / len(outcome_history)
                            if wr_recent < args.bandit_floor and \
                               time.time() >= bandit_paused_until:
                                bandit_paused_until = time.time() + args.bandit_pause_sec
                                f4_trips += 1
                                print(f"{TAG}[bot] F4 BANDIT TRIGGERED "
                                      f"wr={wr_recent:.2f} < {args.bandit_floor} "
                                      f"over last {args.bandit_window}, "
                                      f"pausing {args.bandit_pause_sec:.0f}s",
                                      flush=True)
                # Bind for downstream use.
                wins_eu = ledger.wins
                losses_eu = ledger.losses
                realized_pnl = ledger.realized_pnl
                # Max-loss gate now sees REAL losses.
                if -realized_pnl >= args.max_loss:
                    print(f"{TAG}[bot] LIVE max-loss ${args.max_loss} hit "
                          f"(realized=${realized_pnl:+.4f}), stopping",
                          flush=True)
                    break
                if realized_pnl >= args.max_gain:
                    print(f"{TAG}[bot] LIVE max-gain ${args.max_gain} hit "
                          f"(realized=${realized_pnl:+.4f}), stopping",
                          flush=True)
                    break
                if (wins_eu + losses_eu) >= args.max_taps:
                    print(f"{TAG}[bot] LIVE max-taps {args.max_taps} hit "
                          f"(w/l={wins_eu}/{losses_eu}), stopping", flush=True)
                    break

            # F4 bandit pause gate: skip scoring entirely while paused.
            if args.bandit_window > 0 and time.time() < bandit_paused_until:
                if int(time.time()) % 15 == 0:
                    print(f"{TAG}[bot] F4 paused, "
                          f"{bandit_paused_until - time.time():.0f}s remaining",
                          flush=True)
                time.sleep(1.0); continue

            now_ms = int(time.time() * 1000)
            # ----- Resolve pending bets — EU dense tape + Coinbase tape -----
            still_pending = []
            for b in pending:
                if now_ms < b.window_end_ms + 2500:
                    still_pending.append(b)
                    continue
                band_lo = b.cellY * PRICE_INTERVAL
                band_hi = band_lo + PRICE_INTERVAL
                try:
                    tape_resp = http_get(
                        f"/price_tape?window_ms={int(args.vol_window_sec * 1000 + 60_000)}")
                    eu_ticks = [(ts, p) for ts, p in tape_resp["ticks"]
                                if b.window_start_ms <= ts <= b.window_end_ms]
                except Exception:
                    eu_ticks = []
                cb_ticks = [t for t in coinbase.window(180_000)
                            if b.window_start_ms <= t.ts_ms <= b.window_end_ms]
                # Segment-crossing resolver: catches intra-tick band crossings
                # that pure membership-of-sampled-ticks misses. See resolver.py.
                touched_eu = touched_band(eu_ticks, band_lo, band_hi)
                touched_cb = touched_band(
                    [(t.ts_ms, t.price) for t in cb_ticks], band_lo, band_hi)
                outcome_eu = "WIN" if touched_eu else "LOSS"
                outcome_cb = "WIN" if touched_cb else "LOSS"
                pnl_eu = (b.mult - 1) * b.stake if touched_eu else -b.stake
                disagree = "" if touched_eu == touched_cb else "  ⚠ EU/CB DISAGREE"
                print(f"{TAG}[bot] resolve cell=({b.cellX},{b.cellY}) "
                      f"band=[{band_lo:.2f},{band_hi:.2f}) "
                      f"strat={b.strategy} eu_n={len(eu_ticks)} cb_n={len(cb_ticks)} "
                      f"EU={outcome_eu} CB={outcome_cb} "
                      f"p_pred={b.p_pred:.3f} ev_pred={b.ev_pred:+.3f} "
                      f"σ_at_bet={b.sigma:.3f} drift_at_bet=${b.drift_at_bet:+.2f} "
                      f"pnl=${pnl_eu:+.4f}{disagree}", flush=True)
                if touched_eu: wins_eu += 1
                else: losses_eu += 1
                if touched_cb: wins_cb += 1
                else: losses_cb += 1
                if args.regime_switch:
                    rs_outcomes.append(1 if touched_eu else 0)
                    rs_eval_mode()
                calibrator.record(b.p_pred, touched_eu)
                if cal2 is not None:
                    cal2.record(b.p_pred, b.sigma, b.drift_at_bet, touched_eu)
                    if (wins_eu + losses_eu) % 30 == 0:
                        print(f"{TAG}[bot] CAL2 top buckets: "
                              f"{cal2.gap_summary(top_k=3)}", flush=True)
            pending = still_pending

            # ----- Read proxy state -----
            try:
                st = http_get("/state")
                mults = http_get("/multipliers")
                price_tape = http_get(
                    f"/price_tape?window_ms={int(args.vol_window_sec * 1000)}")
            except Exception as e:
                print(f"{TAG}[bot] proxy error: {e}", flush=True)
                time.sleep(1.0)
                continue
            if not isinstance(mults, dict) or "cells" not in mults:
                # transient: chart hook failed, no quote data. Wait and retry.
                if int(now_ms / 1000) % 5 == 0:
                    print(f"{TAG}[bot] /multipliers missing cells: {str(mults)[:120]}",
                          flush=True)
                time.sleep(1.0)
                continue

            cur_bal = float(st["balance"]) if st["balance"] else starting_balance
            realized_pnl = cur_bal - starting_balance
            if not args.live and -realized_pnl >= args.max_loss:
                print(f"{TAG}[bot] hit max-loss ${args.max_loss}", flush=True); break
            if not args.live and realized_pnl >= args.max_gain:
                print(f"{TAG}[bot] hit max-gain ${args.max_gain}", flush=True); break
            if taps >= args.max_taps:
                print(f"{TAG}[bot] hit max-taps {args.max_taps}", flush=True); break

            now = time.time()
            if now - last_tap_ts < args.cadence_sec:
                time.sleep(0.3); continue

            # ----- Feed freshness checks -----
            e_price = st.get("price"); e_priceTs = st.get("priceTs")
            if e_price is None or e_priceTs is None:
                time.sleep(0.5); continue
            e_age_ms = int(time.time() * 1000) - int(e_priceTs)
            if e_age_ms > args.max_euphoria_price_age_s * 1000:
                print(f"{TAG}[bot] EU price stale ({e_age_ms}ms)", flush=True)
                time.sleep(0.5); continue

            cb_latest = coinbase.latest()
            cb_age_ms = (int(time.time() * 1000) - cb_latest.ts_ms) if cb_latest else 999_999
            bn_latest = binance.latest()

            # ----- Quote freshness gate (#3) -----
            q_age = mults.get("quoteAgeMs")
            if q_age is not None and q_age > args.max_quote_age_ms:
                if int(now) % 5 == 0:
                    print(f"{TAG}[bot] quote stale ({q_age}ms > {args.max_quote_age_ms}ms)",
                          flush=True)
                time.sleep(0.3); continue
            # Lane-4 c30s-quotedrop: also require quote to be almost-stale.
            if args.min_quote_age_ms > 0 and (q_age is None or q_age < args.min_quote_age_ms):
                if int(now) % 5 == 0:
                    print(f"{TAG}[bot] quote too fresh "
                          f"({q_age}ms < min {args.min_quote_age_ms}ms)", flush=True)
                time.sleep(0.3); continue

            # ----- Drift signal (Coinbase - EU; USD-clean, RedStone-aligned) -----
            cb_price = cb_latest.price if cb_latest and cb_age_ms < 3000 else None
            drift_cb = (cb_price - e_price) if cb_price else 0.0

            if cb_price is not None and abs(drift_cb) > args.max_drift:
                if int(now) % 5 == 0:
                    print(f"{TAG}[bot] huge drift cb-eu=${drift_cb:+.2f} — sanity skip",
                          flush=True)
                time.sleep(2.0); continue

            # ----- Pick signal source for σ -----
            if args.signal_source == "binance" and bn_latest:
                P_now, sigma = bn_latest.price, binance.realized_vol_per_sec(
                    int(args.vol_window_sec * 1000))
            elif args.signal_source == "coinbase" and cb_price is not None:
                P_now = cb_price
                sigma = coinbase.realized_vol_per_sec(int(args.vol_window_sec * 1000))
            else:
                P_now = e_price
                sigma = realized_vol_from_ticks(
                    price_tape["ticks"], int(args.vol_window_sec * 1000), now_ms,
                    roll_correction=args.roll_correction)
            if sigma is None or sigma <= 0:
                time.sleep(0.5); continue

            # ----- Heston paths (per-cadence, shared across cells) -----
            heston_paths = None
            if args.vol_model == "heston":
                max_horizon = max(60.0, args.max_time_out_sec + 10.0)
                try:
                    heston_paths = simulate_heston_paths(
                        P_now, sigma,
                        theta=sigma * sigma,
                        kappa=args.heston_kappa,
                        xi=args.heston_xi,
                        rho=args.heston_rho,
                        max_time=max_horizon,
                        dt=args.heston_dt,
                        n_paths=args.heston_paths,
                    )
                except Exception as e:
                    print(f"{TAG}[bot] Heston sim failed: {e}", flush=True)
                    heston_paths = None

            # ----- Adaptive narrowing (every 30 resolved bets) -----
            n_resolved = wins_eu + losses_eu
            if args.adapt and n_resolved > 0 and n_resolved % 30 == 0:
                bb = calibrator.best_bin(min_n=10)
                if bb is not None and (bb[0] != args.min_p or bb[1] != args.max_p):
                    print(f"{TAG}[bot] ADAPT min_p={args.min_p}→{bb[0]} "
                          f"max_p={args.max_p}→{bb[1]} (best bin)", flush=True)
                    args.min_p, args.max_p = bb

            # ----- Strategy: pick a cell -----
            best = None
            scored = 0
            now_s = time.time()
            cells = mults["cells"]

            # v9: compute drift μ from drift_cb if enabled.
            if args.drift_aware_touch and args.drift_conv_sec > 0:
                mu_raw = drift_cb / args.drift_conv_sec  # $/sec
                mu_eff = max(-args.max_drift_mu,
                             min(args.max_drift_mu, mu_raw))
            else:
                mu_eff = 0.0

            def _touch(a: float, b: float, eff_ts: float, eff_tau: float) -> float:
                """Dispatch to the configured touch-prob model."""
                if heston_paths is not None:
                    return touch_prob_heston_from_paths(
                        heston_paths, a, b, eff_ts, eff_tau)
                if args.drift_aware_touch:
                    return touch_prob_drift(P_now, a, b, sigma,
                                            eff_ts, eff_tau, mu_eff)
                # Otherwise Gaussian (df=0) or Student-t (df>2) per --vol-df.
                return touch_prob(P_now, a, b, sigma, eff_ts, eff_tau,
                                  df=args.vol_df)

            # Update per-cell mult history (for --min-quote-mult-change-pct).
            if args.min_quote_mult_change_pct > 0:
                cutoff_ms = now_ms - int(args.quote_change_window_sec * 1000) - 5000
                for c in cells:
                    k = (c["cellX"], c["cellY"])
                    h = cell_mult_history.get(k)
                    if h is None:
                        h = deque(maxlen=MULT_HISTORY_MAXLEN)
                        cell_mult_history[k] = h
                    h.append((now_ms, c["mult"]))
                # Drop stale-cell entries to bound memory
                if len(cell_mult_history) > 5000:
                    keep_keys = {(c["cellX"], c["cellY"]) for c in cells}
                    cell_mult_history = {k: v for k, v in cell_mult_history.items() if k in keep_keys}

            def cell_mult_changed_enough(cx: int, cy: int) -> bool:
                if args.min_quote_mult_change_pct <= 0:
                    return True
                h = cell_mult_history.get((cx, cy))
                if h is None or len(h) < 2:
                    return False  # not enough history; conservatively skip
                cutoff_ms = now_ms - int(args.quote_change_window_sec * 1000)
                sample = [m for (ts, m) in h if ts >= cutoff_ms]
                if len(sample) < 2:
                    return False
                mn, mx = min(sample), max(sample)
                if mn <= 0:
                    return False
                return ((mx - mn) / mn) >= args.min_quote_mult_change_pct

            def score_prob_cell():
                nonlocal scored
                best_local = None
                for c in cells:
                    cx, cy, m = c["cellX"], c["cellY"], c["mult"]
                    if m < args.min_mult or m > args.max_mult: continue
                    if not cell_mult_changed_enough(cx, cy): continue
                    t_start = cx * TIME_INTERVAL - now_s
                    if t_start < -TIME_INTERVAL: continue
                    if t_start > args.max_time_out_sec: continue
                    eff_ts = max(0.0, t_start)
                    eff_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
                    if eff_tau <= 0: continue
                    a = cy * PRICE_INTERVAL; b = a + PRICE_INTERVAL
                    p = _touch(a, b, eff_ts, eff_tau)
                    if p < args.min_p or p > args.max_p: continue
                    p_eff = cal2.adjust(p, sigma, drift_cb) if cal2 is not None else p
                    ev = m * p_eff - 1
                    scored += 1
                    if best_local is None or ev > best_local["ev"]:
                        best_local = {"cx": cx, "cy": cy, "mult": m, "p": p,
                                      "ev": ev, "strategy": "prob"}
                return best_local

            def score_edge_c(tau_sec, thr, label, mean_rev: bool = False):
                nonlocal scored
                if cb_price is None: return None
                if abs(drift_cb) < thr: return None
                # find cells whose midpoint time is closest to now + tau
                target_cx = int((now_s + tau_sec) / TIME_INTERVAL)
                cells_at_tau = [c for c in cells if c["cellX"] == target_cx]
                if not cells_at_tau:
                    cells_at_tau = sorted(
                        cells, key=lambda c: abs(c["cellX"] - target_cx))[:60]
                pick = pick_edge_c_cell(cells_at_tau, P_now, drift_cb,
                                        target_cx, args.min_mult, args.max_mult,
                                        mean_rev=mean_rev)
                if pick is None: return None
                if not cell_mult_changed_enough(pick["cx"], pick["cy"]): return None
                scored += 1
                # Compute touch-prob with same model for logging/calibration consistency
                t_start = pick["cx"] * TIME_INTERVAL - now_s
                eff_ts = max(0.0, t_start)
                eff_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
                p = _touch(pick["a"], pick["b"], eff_ts, eff_tau)
                p_eff = cal2.adjust(p, sigma, drift_cb) if cal2 is not None else p
                ev = pick["mult"] * p_eff - 1
                return {"cx": pick["cx"], "cy": pick["cy"], "mult": pick["mult"],
                        "p": p, "ev": ev, "strategy": label}

            def score_edge_b():
                """Edge B: vol-regime arb. Fire when realized σ is high (proxy for
                'MM under-pricing vol'); pick OOM cell with mult near b_target_mult."""
                nonlocal scored
                if sigma < args.b_vol_threshold:
                    return None
                best_local = None
                best_d = float("inf")
                for c in cells:
                    cx, cy, m = c["cellX"], c["cellY"], c["mult"]
                    if m < args.b_mult_min or m >= args.b_mult_max:
                        continue
                    if not cell_mult_changed_enough(cx, cy):
                        continue
                    t_start = cx * TIME_INTERVAL - now_s
                    if t_start < -TIME_INTERVAL: continue
                    if t_start > args.max_time_out_sec: continue
                    a = cy * PRICE_INTERVAL; b = a + PRICE_INTERVAL
                    # OOM only — skip already-in-band cells (those are Edge A)
                    if a <= P_now < b:
                        continue
                    d = abs(m - args.b_target_mult)
                    if d < best_d:
                        eff_ts = max(0.0, t_start)
                        eff_tau = TIME_INTERVAL if t_start >= 0 else TIME_INTERVAL + t_start
                        if eff_tau <= 0: continue
                        p = _touch(a, b, eff_ts, eff_tau)
                        p_eff = cal2.adjust(p, sigma, drift_cb) if cal2 is not None else p
                        ev = m * p_eff - 1
                        best_d = d
                        best_local = {"cx": cx, "cy": cy, "mult": m, "p": p,
                                      "ev": ev, "strategy": "b"}
                if best_local: scored += 1
                return best_local

            if args.strategy == "prob":
                best = score_prob_cell()
            elif args.strategy == "c90":
                best = score_edge_c(args.c_tau_sec, args.c_thr, "c90")
            elif args.strategy == "c30-strong":
                best = score_edge_c(args.c_tau_strong_sec, args.c_thr_strong,
                                    "c30-strong")
            elif args.strategy == "c30-strong-meanrev":
                # Lane-4 challenger: same trigger, opposite direction.
                best = score_edge_c(args.c_tau_strong_sec, args.c_thr_strong,
                                    "c30-strong-meanrev", mean_rev=True)
            elif args.strategy == "c30-strong-quotedrop":
                # Lane-4 challenger: c30s + min-quote-age gate (handled above).
                best = score_edge_c(args.c_tau_strong_sec, args.c_thr_strong,
                                    "c30-strong-quotedrop")
            elif args.strategy == "b":
                best = score_edge_b()
            elif args.strategy == "combo":
                # try strong first, then c90, then B (if high vol), then prob fallback
                best = (score_edge_c(args.c_tau_strong_sec, args.c_thr_strong, "c30-strong")
                        or score_edge_c(args.c_tau_sec, args.c_thr, "c90")
                        or score_edge_b()
                        or score_prob_cell())

            if not best:
                if int(now) % 5 == 0:
                    print(f"{TAG}[bot] no-signal strat={args.strategy} "
                          f"drift_cb=${drift_cb:+.2f} cb_age={cb_age_ms}ms "
                          f"σ={sigma:.3f} P_eu=${e_price:.2f}", flush=True)
                time.sleep(0.5); continue

            # Adverse-selection haircut (Cartea-Jaimungal style): when MM
            # accepts a tap, posterior P(touch) < unconditional P(touch).
            # Subtract a flat haircut from ev before threshold check.
            ev_adj = best["ev"] - args.ev_haircut
            if ev_adj < args.ev_threshold:
                if int(now) % 5 == 0:
                    print(f"{TAG}[bot] no-edge best_ev={best['ev']:+.3f} "
                          f"ev_adj={ev_adj:+.3f} cell=({best['cx']},{best['cy']}) "
                          f"mult={best['mult']} p={best['p']:.3f}", flush=True)
                time.sleep(0.5); continue

            cx, cy, mult, p, ev = (best["cx"], best["cy"], best["mult"],
                                   best["p"], best["ev"])
            print(f"{TAG}[bot] SIGNAL strat={best['strategy']} cell=({cx},{cy}) "
                  f"mult={mult} p={p:.3f} ev={ev:+.3f} σ={sigma:.3f} "
                  f"P_eu=${e_price:.2f} P_cb=${cb_price if cb_price else 'n/a'} "
                  f"drift_cb=${drift_cb:+.2f} q_age={q_age}ms (scored={scored})"
                  f"{' mode='+rs_mode if args.regime_switch else ''}"
                  f"{' μ=$%+.3f/s' % mu_eff if args.drift_aware_touch else ''}",
                  flush=True)

            # v8 regime switcher: record σ for the rolling-σ window, then
            # apply σ-cap only if we're in COLD mode.
            if args.regime_switch:
                rs_sigmas.append((time.time(), sigma))
                if rs_mode == "COLD" and sigma > args.rs_cold_sigma_cap:
                    rs_cold_skips += 1
                    if int(now) % 8 == 0:
                        print(f"{TAG}[bot] v8 COLD-skip σ={sigma:.3f} > "
                              f"{args.rs_cold_sigma_cap}", flush=True)
                    last_tap_ts = now
                    continue

            # R1 σ-ceiling: skip if vol regime is in kill zone.
            if sigma > args.max_sigma:
                r1_skips += 1
                if int(now) % 8 == 0:
                    print(f"{TAG}[bot] R1 SKIP σ-ceiling σ={sigma:.3f} > "
                          f"{args.max_sigma}", flush=True)
                last_tap_ts = now
                continue

            # R3 |drift|-ceiling: skip if drift magnitude is in kill zone.
            if abs(drift_cb) > args.max_abs_drift:
                r3_skips += 1
                if int(now) % 8 == 0:
                    print(f"{TAG}[bot] R3 SKIP |drift|-ceiling "
                          f"|drift|=${abs(drift_cb):.2f} > "
                          f"${args.max_abs_drift}", flush=True)
                last_tap_ts = now
                continue

            # F1 one-way-trend gate: record drift at signal time, then check
            # whether the last K signals are all strictly same-sign. If so,
            # abstain — Run #3 collapse pattern.
            if args.trend_gate_k > 0:
                drift_streak.append(drift_cb)
                save_filter_state()
                if len(drift_streak) == args.trend_gate_k:
                    signs = [1 if d > 0 else (-1 if d < 0 else 0)
                             for d in drift_streak]
                    if 0 not in signs and len(set(signs)) == 1:
                        f1_skips += 1
                        print(f"{TAG}[bot] F1 SKIP trend-gate "
                              f"K={args.trend_gate_k} all-{signs[0]:+d}-sign "
                              f"drift_streak={[f'{d:+.2f}' for d in drift_streak]}",
                              flush=True)
                        last_tap_ts = now  # cadence rate-limit
                        continue

            cum_ev_predicted += ev * args.stake

            if not args.live:
                window_start_ms = cx * TIME_INTERVAL * 1000
                window_end_ms = window_start_ms + TIME_INTERVAL * 1000
                pending.append(PendingBet(
                    cellX=cx, cellY=cy, mult=mult, stake=args.stake,
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                    p_pred=p, ev_pred=ev, sigma=sigma,
                    strategy=best["strategy"],
                    drift_at_bet=drift_cb,
                ))
                eta = (window_end_ms - int(time.time() * 1000)) / 1000
                print(f"{TAG}[bot] DRY-RUN — would tap ${args.stake} resolves in ~{eta:.1f}s",
                      flush=True)
                last_tap_ts = now
                taps += 1
                continue

            # LIVE — fire-and-forget /tap. We do NOT trust the response for
            # accounting (the LIVE proxy frequently returns 504 even when the
            # position later settles successfully). Outcomes are picked up by
            # the LiveLedger.poll() call at the top of the next loop iteration
            # — that's the single source of truth for wins/losses/max-loss.
            #
            # This loop just classifies the /tap outcome for logging and
            # records slippage rejections separately so the offline analysis
            # can resolve "would-they-have-won" against the EU price tape.
            live_attempts += 1
            try:
                resp = http_post("/tap", {"cellX": cx, "cellY": cy,
                                          "amount": args.stake,
                                          "slippage": args.slippage,
                                          "waitSettled": True,
                                          "timeoutS": 60},
                                 timeout=70.0)
                print(f"{TAG}[bot] /tap accepted: "
                      f"{json.dumps(resp)[:200] if isinstance(resp, dict) else str(resp)[:200]}",
                      flush=True)
            except urllib.error.HTTPError as he:
                # Read body once — important: it's consumed on close.
                try:
                    body = he.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                # SLIPPAGE_EXCEEDED → HTTP 409 with detail. Track separately;
                # do NOT count as a settled tap (no money was moved).
                if he.code == 409 and "SLIPPAGE" in body.upper():
                    slippage_rejects += 1
                    # Append a record for later "would they have won" analysis.
                    try:
                        os.makedirs("logs", exist_ok=True)
                        with open("logs/slippage_rejects.jsonl", "a") as f:
                            f.write(json.dumps({
                                "ts": time.time(),
                                "tag": args.tag,
                                "cellX": cx, "cellY": cy,
                                "mult_signed": mult,
                                "p_pred": p, "ev_pred": ev,
                                "sigma": sigma, "drift_cb": drift_cb,
                                "stake": args.stake,
                                "window_start_ms": cx * TIME_INTERVAL * 1000,
                                "window_end_ms": cx * TIME_INTERVAL * 1000
                                                 + TIME_INTERVAL * 1000,
                                "body": body[:500],
                            }) + "\n")
                    except Exception as we:
                        print(f"{TAG}[bot] slippage log write failed: {we}",
                              flush=True)
                    print(f"{TAG}[bot] /tap REJECTED slippage cell=({cx},{cy}) "
                          f"signed_mult={mult} (total slip-rejects="
                          f"{slippage_rejects}/{live_attempts})", flush=True)
                elif he.code == 504:
                    # Timeout — position is likely still settling. The ledger
                    # poll will pick it up on a subsequent loop iteration.
                    print(f"{TAG}[bot] /tap timeout cell=({cx},{cy}) "
                          f"signed_mult={mult} — settlement pending, will be "
                          f"picked up by ledger poll", flush=True)
                elif he.code == 422:
                    # Hard fail — placeBet rejected client-side before submit.
                    print(f"{TAG}[bot] /tap hard-fail cell=({cx},{cy}) "
                          f"signed_mult={mult} body={body[:200]}", flush=True)
                else:
                    print(f"{TAG}[bot] /tap HTTP {he.code} cell=({cx},{cy}) "
                          f"body={body[:200]}", flush=True)
            except Exception as e:
                # Network error / generic timeout. The ledger poll handles
                # late-arriving settlements.
                print(f"{TAG}[bot] /tap error: {type(e).__name__}: {e} "
                      f"cell=({cx},{cy}) signed_mult={mult} — ledger will "
                      f"reconcile if settlement arrives", flush=True)
            last_tap_ts = now
            # NOTE: we do NOT increment wins_eu/losses_eu/taps here. Those are
            # owned by LiveLedger via the poll at the top of the next loop
            # iteration. This guarantees correct accounting even when /tap
            # times out on successful settlements.

    finally:
        binance.stop(); coinbase.stop()
        if pending:
            print(f"{TAG}[bot] {len(pending)} pending bets unresolved", flush=True)
        if ledger is not None:
            # Final ledger poll to catch any settlements that arrived after the
            # last cycle (e.g., during shutdown).
            try:
                ledger.poll(http_get, tag=TAG)
            except Exception:
                pass
            wins_eu = ledger.wins
            losses_eu = ledger.losses
            print(f"\n{TAG}[bot] LIVE DONE  attempts={live_attempts} "
                  f"slip_rejects={slippage_rejects} settled_w/l="
                  f"{wins_eu}/{losses_eu} realized=${ledger.realized_pnl:+.4f} "
                  f"f1_skips={f1_skips} f4_trips={f4_trips} "
                  f"r1_skips={r1_skips} r3_skips={r3_skips} "
                  f"rs_mode={rs_mode} rs_transitions={rs_transitions} "
                  f"rs_cold_skips={rs_cold_skips}",
                  flush=True)
        n = wins_eu + losses_eu
        print(f"\n{TAG}[bot] DONE  taps={taps} EU win/loss={wins_eu}/{losses_eu} "
              f"CB win/loss={wins_cb}/{losses_cb}", flush=True)
        if n > 0:
            wr_eu = 100 * wins_eu / n
            wr_cb = 100 * wins_cb / max(wins_cb + losses_cb, 1)
            print(f"{TAG}[bot] EU win rate {wr_eu:.1f}%   CB win rate {wr_cb:.1f}%   "
                  f"predicted cum EV ${cum_ev_predicted:+.2f}", flush=True)
            print(f"{TAG}[bot] Calibration:", flush=True)
            for s in calibrator.summary():
                if s["n"] == 0: continue
                print(f"{TAG}[bot]   p∈[{s['bin'][0]:.2f},{s['bin'][1]:.2f}) "
                      f"n={s['n']} pred={s.get('avg_pred', 0):.3f} "
                      f"actual={s['actual']:.3f} gap={s['gap']:+.3f}",
                      flush=True)


if __name__ == "__main__":
    main()
