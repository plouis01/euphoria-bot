"""
Closed-form / numerical-quadrature probability that an arithmetic-Brownian
price path visits a band [a, b] during a future window.

Model: P(t) = P0 + σ · W(t), with σ in $ per √sec, no drift.

We want: p_touch = P[ ∃ s ∈ [t_start, t_start + τ] : P(s) ∈ [a, b] ]

If t_start = 0 this is the standard first-hitting-time formula:
  - if P0 ∈ [a, b]: trivially 1
  - if P0 < a:  P[max ≥ a] = 2 (1 - Φ((a - P0) / (σ√τ)))   (reflection)
  - if P0 > b:  P[min ≤ b] = 2 (1 - Φ((P0 - b) / (σ√τ)))

For t_start > 0 we integrate over the marginal P(t_start) ~ N(P0, σ²·t_start),
multiplying by the conditional in-window touch probability.

Short-horizon arithmetic BM is appropriate here: 5-sec window, ~$0.1 moves
on a $2000+ asset — log vs arithmetic is indistinguishable.
"""
from math import erf, exp, pi, sqrt
from typing import Tuple

try:
    from scipy.stats import t as _student_t
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False

try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    _NUMPY_OK = False


def _phi(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2)))


def _student_t_cdf(x: float, df: float) -> float:
    """Standard Student-t CDF. Falls back to Gaussian if scipy missing."""
    if not _SCIPY_OK:
        return _phi(x)
    return float(_student_t.cdf(x, df))


def _touch_in_window(x: float, a: float, b: float, sigma: float, tau: float,
                     df: float = 0.0) -> float:
    """P[path visits [a,b] during [0, τ] | starts at x].

    Reflection-principle formula. For Gaussian (df=0), exact. For Student-t
    with df>2, the same form is used as an approximation (reflection is only
    exact for Brownian motion; for heavy-tailed walks this overstates
    short-tail touch and understates long-tail touch in moderate ways — but
    captures the fat-tail correction we want for far-OOM cells).

    df=0 → Gaussian (default, original behavior).
    df>2 → Student-t with df degrees of freedom, scaled to match sigma.
    """
    if a <= x < b:
        return 1.0
    sd = sigma * sqrt(tau)
    if sd <= 0:
        return 0.0
    if df > 2.0 and _SCIPY_OK:
        # Scale so that resulting variable has SD = sd.
        # Student-t with df has variance df/(df-2); scale s makes Var = s²·df/(df-2) = sd²
        # → s = sd·sqrt((df-2)/df)
        s = sd * sqrt((df - 2.0) / df)
        gap = (a - x) if x < a else (x - b)
        z = gap / s
        return min(1.0, 2.0 * (1.0 - _student_t_cdf(z, df)))
    if x < a:
        return min(1.0, 2.0 * (1.0 - _phi((a - x) / sd)))
    return min(1.0, 2.0 * (1.0 - _phi((x - b) / sd)))


def touch_prob(
    P0: float, a: float, b: float, sigma: float,
    t_start: float, tau: float,
    n_quad: int = 41,
    df: float = 0.0,
) -> float:
    """Probability of touching band [a, b] during [t_start, t_start + τ].

    P0       current price
    a, b     band edges ($)
    sigma    vol in $/√sec
    t_start  seconds until window opens (≥0)
    tau      window duration (sec)
    df       0 (default): Gaussian. >2: Student-t (leptokurtic, fatter tails).
             Typical ν for HF crypto returns: 4-5.
    """
    if a > b:
        a, b = b, a
    if sigma <= 0 or tau <= 0:
        return 1.0 if (a <= P0 < b) else 0.0
    if t_start <= 0:
        return _touch_in_window(P0, a, b, sigma, tau, df=df)

    # Numerical integration over P(t_start). Marginal distribution at t_start:
    #  Gaussian: N(P0, σ²·t_start). Student-t: scaled-t with df, same SD.
    sig_gap = sigma * sqrt(t_start)
    lo = P0 - 6 * sig_gap  # wider for fat tails
    hi = P0 + 6 * sig_gap
    dx = (hi - lo) / (n_quad - 1)
    total = 0.0
    if df > 2.0 and _SCIPY_OK:
        s = sig_gap * sqrt((df - 2.0) / df)
        from scipy.stats import t as _t
        for i in range(n_quad):
            x = lo + i * dx
            w = _t.pdf((x - P0) / s, df) / s
            total += w * _touch_in_window(x, a, b, sigma, tau, df=df) * dx
    else:
        norm = 1.0 / (sig_gap * sqrt(2 * pi))
        for i in range(n_quad):
            x = lo + i * dx
            w = norm * exp(-((x - P0) ** 2) / (2 * sig_gap ** 2))
            total += w * _touch_in_window(x, a, b, sigma, tau) * dx
    return max(0.0, min(1.0, total))


# ---------------------------------------------------------------------------
# Heston (1993) stochastic-volatility model — Monte Carlo paths.
#
# Arithmetic-price variant (consistent with our σ in $/√sec):
#   dP_t = √v_t dW1_t
#   dv_t = κ(θ - v_t) dt + ξ√v_t dW2_t
#   Cov(dW1, dW2) = ρ dt
#
# Full-truncation Euler discretization (Lord-Koekkoek-VanDijk 2008) — handles
# v < 0 by setting effective variance to max(v, 0).
#
# Touch probability is computed by sampling paths and counting which ones
# visit the band during the [t_start, t_start+τ] window.
# ---------------------------------------------------------------------------


def simulate_heston_paths(
    P0: float,
    sigma_now: float,    # current vol estimate, $/√s (so v_now = sigma_now**2)
    theta: float = None, # long-run variance ($²/s); defaults to current
    kappa: float = 2.0,  # mean-reversion speed (1/s)
    xi: float = 0.5,     # vol-of-vol ($/s per √s)
    rho: float = 0.0,    # leverage corr (typically 0 to -0.3 for crypto HF)
    max_time: float = 120.0,
    dt: float = 0.25,
    n_paths: int = 500,
    seed: int = None,
):
    """Returns (times, prices). prices.shape = (n_paths, n_steps+1)."""
    if not _NUMPY_OK:
        raise RuntimeError("numpy not available; cannot simulate Heston paths")
    if theta is None:
        theta = sigma_now ** 2
    n_steps = int(max(1, round(max_time / dt)))
    times = np.arange(n_steps + 1) * dt
    rng = np.random.default_rng(seed)
    P = np.full((n_paths, n_steps + 1), P0, dtype=np.float64)
    v = np.full((n_paths, n_steps + 1), sigma_now ** 2, dtype=np.float64)
    sqrt_dt = sqrt(dt)
    Z = rng.standard_normal((n_paths, n_steps, 2))
    W1 = Z[:, :, 0]
    W2 = rho * Z[:, :, 0] + sqrt(max(0.0, 1.0 - rho * rho)) * Z[:, :, 1]
    for k in range(n_steps):
        v_k = np.maximum(v[:, k], 0.0)
        sqrt_v = np.sqrt(v_k)
        # Full-truncation: use max(v, 0) in diffusion + drift; allow neg in state then clip
        v[:, k + 1] = np.maximum(
            0.0,
            v[:, k] + kappa * (theta - v_k) * dt + xi * sqrt_v * sqrt_dt * W2[:, k],
        )
        P[:, k + 1] = P[:, k] + sqrt_v * sqrt_dt * W1[:, k]
    return times, P


def touch_prob_heston_from_paths(paths, a: float, b: float,
                                 t_start: float, tau: float) -> float:
    """Compute P[touch [a,b] in [t_start, t_start+τ]] from precomputed paths."""
    if not _NUMPY_OK:
        return 0.0
    times, prices = paths
    if a > b:
        a, b = b, a
    mask = (times >= t_start) & (times <= t_start + tau)
    if not mask.any():
        return 0.0
    window = prices[:, mask]
    pmin = window.min(axis=1)
    pmax = window.max(axis=1)
    touched = (pmin <= b) & (pmax >= a)
    return float(touched.mean())


if __name__ == "__main__":
    # Quick sanity checks
    print("P0=2140, band [2140.0, 2140.5), σ=0.1, t_start=0, τ=5")
    print(f"  p={touch_prob(2140.0, 2140.0, 2140.5, 0.1, 0, 5):.3f}")  # ~1.0 (in band)
    print("P0=2139.8, band [2140.0, 2140.5), σ=0.1, t_start=0, τ=5")
    print(f"  p={touch_prob(2139.8, 2140.0, 2140.5, 0.1, 0, 5):.3f}")
    print("P0=2139, band [2140.0, 2140.5), σ=0.1, t_start=10, τ=5")
    print(f"  p={touch_prob(2139.0, 2140.0, 2140.5, 0.1, 10, 5):.3f}")
    print("P0=2139, band [2140.0, 2140.5), σ=0.3, t_start=10, τ=5")
    print(f"  p={touch_prob(2139.0, 2140.0, 2140.5, 0.3, 10, 5):.3f}")
    print("Far OOM: P0=2140, band [2160, 2160.5), σ=0.1, t_start=10, τ=5")
    print(f"  p={touch_prob(2140.0, 2160.0, 2160.5, 0.1, 10, 5):.6f}")
