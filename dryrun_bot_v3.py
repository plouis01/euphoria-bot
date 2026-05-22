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
import urllib.request
from collections import deque
from dataclasses import dataclass, field

# Use centralized proxy feeds (BinanceFeed/CoinbaseFeed both route through the
# signing_proxy's /binance_tape and /coinbase_tape endpoints).
from proxy_feed import BinanceFeed, CoinbaseFeed
from resolver import touched_band
from touch_prob import touch_prob, simulate_heston_paths, touch_prob_heston_from_paths


PROXY = os.environ.get("PROXY", "http://127.0.0.1:8787")
PRICE_INTERVAL = 0.5
TIME_INTERVAL = 5
KILL_FILE = "/tmp/euphoria_bot_kill"


def http_get(path: str):
    with urllib.request.urlopen(PROXY + path, timeout=3) as r:
        return json.loads(r.read())


def http_post(path: str, body: dict):
    req = urllib.request.Request(
        PROXY + path, method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
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


# ---------------------------------------------------------------------------
# Strategy: Edge C — OOM cell in direction of (external - EU) drift
# ---------------------------------------------------------------------------

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
    ap.add_argument("--roll-correction", action="store_true",
                    help="Apply Roll (1984) bid-ask bounce correction to realized σ.")
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
    # Per-cell mult history for --min-quote-mult-change-pct filter.
    # Keyed by (cellX, cellY); each value is a deque of (ts_ms, mult) entries.
    cell_mult_history: dict[tuple[int, int], deque] = {}
    MULT_HISTORY_MAXLEN = 40  # ~5 min at 8s cadence

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
                calibrator.record(b.p_pred, touched_eu)
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
            if -realized_pnl >= args.max_loss:
                print(f"{TAG}[bot] hit max-loss ${args.max_loss}", flush=True); break
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

            def _touch(a: float, b: float, eff_ts: float, eff_tau: float) -> float:
                """Dispatch to the configured touch-prob model."""
                if heston_paths is not None:
                    return touch_prob_heston_from_paths(
                        heston_paths, a, b, eff_ts, eff_tau)
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
                    ev = m * p - 1
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
                ev = pick["mult"] * p - 1
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
                        ev = m * p - 1
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
                  f"drift_cb=${drift_cb:+.2f} q_age={q_age}ms (scored={scored})",
                  flush=True)
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

            # LIVE — new /tap blocks until settled (60s default timeout), so
            # the response carries the full lifecycle outcome in one round-trip.
            bal_before = cur_bal
            try:
                resp = http_post("/tap", {"cellX": cx, "cellY": cy,
                                          "amount": args.stake,
                                          "slippage": args.slippage,
                                          "waitSettled": True,
                                          "timeoutS": 60})
                print(f"{TAG}[bot] tap response: {json.dumps(resp)[:300]}", flush=True)
            except Exception as e:
                # Network error / 422 hard fail / 504 timeout — count as no-tap and continue.
                # We do NOT increment `taps` because we can't confirm execution.
                print(f"{TAG}[bot] /tap error: {e}", flush=True)
                last_tap_ts = now
                time.sleep(2.0)
                continue
            last_tap_ts = now
            taps += 1

            # Parse settled outcome from response.
            settled = resp.get("settled") if isinstance(resp, dict) else None
            new_bal = None
            won = None
            if isinstance(settled, dict):
                settled_inner = settled.get("settled") or {}
                new_bal_str = settled_inner.get("updatedBalance")
                won = bool(settled_inner.get("won")) if "won" in settled_inner else None
                if new_bal_str is not None:
                    try: new_bal = float(new_bal_str)
                    except (ValueError, TypeError): new_bal = None

            # Fallback: read /chart for balance (more reliable than /state).
            if new_bal is None:
                try:
                    ch = http_get("/chart")
                    new_bal = float(ch["balance"]) if ch.get("balance") is not None else bal_before
                except Exception:
                    new_bal = bal_before

            delta = new_bal - bal_before
            # If we got an explicit `won` flag, use it; otherwise infer from delta.
            if won is None:
                won = delta > 0.001  # small epsilon to ignore floating noise
            if won:
                wins_eu += 1
            else:
                losses_eu += 1
            # Track realized PnL locally for max-loss gate
            realized_pnl = new_bal - starting_balance
            print(f"{TAG}[bot] tap #{taps}: settled={resp.get('settledStatus','?') if isinstance(resp, dict) else '?'} "
                  f"{'WIN' if won else 'LOSS'} delta=${delta:+.4f} "
                  f"bal=${new_bal:.4f} cum=${realized_pnl:+.4f} "
                  f"w/l={wins_eu}/{losses_eu}", flush=True)
            # Max-loss gate (now actually works since balance is reliable)
            if -realized_pnl >= args.max_loss:
                print(f"{TAG}[bot] hit max-loss ${args.max_loss}, stopping", flush=True)
                break

    finally:
        binance.stop(); coinbase.stop()
        if pending:
            print(f"{TAG}[bot] {len(pending)} pending bets unresolved", flush=True)
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
