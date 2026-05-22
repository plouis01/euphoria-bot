# LIVE Run #2 — Pre-Registration

**Locked at**: commit time. Do not edit mid-run. Treat any edit during the run as invalidating the result.

## Goal

Prove that `c30-strong` has a durable positive-EV edge across mixed σ regimes
— not single-regime luck from Run #1 (n=12, 28 min, ETH was volatile).

## Parameters (do not change during the run)

| Parameter | Value |
|-----------|-------|
| Stake per tap | $1 |
| Max-loss per session | $15 |
| Max taps per session | 200 |
| Cadence | 8 s (`--cadence-sec 8`) |
| Slippage | 60 bp |
| Drift threshold | `--c-thr-strong 0.40` |
| Drift lookback | `--c-tau-strong-sec 30` |
| Vol window | `--vol-window-sec 90` |
| Max quote age | `--max-quote-age-ms 700` |
| Watchdog | `min-balance=$25, max-drop=$15/600s, stale-tolerance=30s` |

## Sample-size target

- **Per-session minimum**: 40 resolved taps.
- **Combined target across sessions**: ≥ 120 resolved taps.
- **Stretch**: 200, if regime coverage allows.

Power note: at σ($/tap) ≈ $3 empirical, n=120 → SE($0.27) → 95% CI half-width $0.55. We can reject H0:mean ≤ 0 if true mean ≥ $0.55. Run #1 point estimate is $1.47 → we'd reject easily if that's the true mean; dry-run point estimate is $0.25 → marginal.

## Session structure

≥ 3 sessions, each ≥ 2 hours, across at least 3 σ-regime buckets:

- One session in σ < 0.10 (quiet)
- One session in σ ∈ [0.10, 0.15) (moderate)
- One session in σ ≥ 0.15 (volatile)

Within each session: c30s + **one** Lane-4 challenger from
`{c30-strong-meanrev, c30-strong-quotedrop}`. Round-robin across sessions.
**Not** both challengers in the same session.

## Per-session command shape

```bash
# session 1 (quiet regime expected — pick a low-σ window from /coinbase_tape)
python dryrun_bot_v3.py --live --tag c30s    --strategy c30-strong              \
  --stake 1 --max-loss 15 --max-taps 200 --cadence-sec 8 --slippage 60 \
  --c-thr-strong 0.40 --c-tau-strong-sec 30 --vol-window-sec 90        \
  --max-quote-age-ms 700

python dryrun_bot_v3.py --live --tag c30s-mr --strategy c30-strong-meanrev      \
  --stake 1 --max-loss 15 --max-taps 200 --cadence-sec 8 --slippage 60 \
  --c-thr-strong 0.40 --c-tau-strong-sec 30 --vol-window-sec 90        \
  --max-quote-age-ms 700

# Always pair with the watchdog (separate terminal):
python scripts/balance_watchdog.py
```

## Stop conditions (any one ends the session)

1. $15 cumulative loss hit (`--max-loss` triggers, bot exits).
2. 200 taps placed (`--max-taps`).
3. Watchdog wrote `/tmp/euphoria_bot_kill`.
4. > 30 min with zero signal-firing (drift never crosses threshold) — abandon, label "regime did not trigger", count separately.

## Edge-confirmed criteria (ALL must hold)

1. **c30s bootstrap 95% CI lower bound on $/tap > 0** across the combined dataset.
2. **c30s positive mean $/tap in ≥ 2 of 3 σ-regime buckets** (paired by session).
3. **c30s beats challenger by paired bootstrap on overlapping minutes**, OR challenger fails to beat zero in its session.

If any one fails → **inconclusive, do another round**. Do NOT scale stake.

## Reporting

After all sessions:

```bash
python scripts/leaderboard.py --log logs/c30s-run2-*.log --log logs/c30s-mr-run2-*.log \
                              --log logs/c30s-qd-run2-*.log
```

Produces the side-by-side table. Save as `run2_report_<date>.txt`.

Plot calibration (use the `Calibrator.summary()` schema from `dryrun_bot_v3.py:132-144`):
predicted p vs actual WR, bin counts, gaps. Save as `run2_calibration.txt`.

## What "scale stake" looks like if confirmed

- Stake doubles to $2 only after a second run (Run #3) repeats the three pre-committed assertions at $2 stake.
- Risk-of-ruin recomputed with σ($/tap) updated from Run #2 empirical.
- Watchdog `--min-balance` ratchets up to keep cushion at $13.
