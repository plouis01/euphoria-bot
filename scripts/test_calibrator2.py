"""Unit tests for Calibrator2 (regime-aware calibration).

Covers:
- Cold start identity
- 3D bucket match (p, σ, |drift|) → pulls toward empirical WR
- 2D fallback when 3D bucket sparse
- 1D fallback when 2D bucket also sparse
- Identity fallback when no bucket data
- Per-σ/drift segregation (same p_pred, different regime → different adjust)
- gap_summary shape
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dryrun_bot_v3 import Calibrator2, SIGMA_BINS, DRIFT_BINS, P_BINS


def test_cold_start_identity():
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    assert abs(c.adjust(0.40, 0.15, 0.42) - 0.40) < 1e-9


def test_3d_bucket_match():
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    # Feed 20 losses in (p~0.42, σ=0.15 → SIGMA_BINS[2], drift=0.42 → DRIFT_BINS[1])
    for _ in range(20):
        c.record(p_pred=0.42, sigma=0.15, drift=0.42, won=False)
    adj = c.adjust(0.42, 0.15, 0.42)
    expected = 0.4 * 0.0 + 0.6 * 0.42  # blend=0.4 toward 0.0
    assert abs(adj - expected) < 0.01, f"got {adj}, expected {expected}"


def test_regime_segregation():
    """Same p_pred, two regimes → different adjusts."""
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    # bad bucket: all losses
    for _ in range(15):
        c.record(p_pred=0.42, sigma=0.15, drift=0.42, won=False)
    # good bucket: all wins
    for _ in range(15):
        c.record(p_pred=0.42, sigma=0.10, drift=0.20, won=True)
    bad = c.adjust(0.42, 0.15, 0.42)
    good = c.adjust(0.42, 0.10, 0.20)
    assert good > bad + 0.30, f"good={good}, bad={bad} — segregation failed"


def test_2d_fallback():
    """If exact (p, σ, drift) bucket is sparse but (p, σ) has data, fall back to (p, σ)."""
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    # Distribute data across two drift buckets within the same (p, σ) bucket
    for _ in range(5):
        c.record(p_pred=0.42, sigma=0.15, drift=0.20, won=True)
    for _ in range(5):
        c.record(p_pred=0.42, sigma=0.15, drift=0.42, won=False)
    # Neither 3D bucket has min_n=8. 2D (p=0.42, σ=0.15) has n=10, WR=0.5
    adj = c.adjust(0.42, 0.15, 0.50)  # drift in third bin, no 3D data
    expected = 0.4 * 0.5 + 0.6 * 0.42  # blend toward 2D WR=0.5
    assert abs(adj - expected) < 0.02, f"got {adj}, expected ~{expected}"


def test_1d_fallback():
    """If 2D bucket also sparse, fall back to 1D (p_pred bin)."""
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    # Spread across many (σ, drift) combos within p_pred=0.42
    for sigma, drift, won in [
        (0.05, 0.20, True), (0.10, 0.20, True), (0.15, 0.20, True),
        (0.20, 0.20, True), (0.05, 0.42, False), (0.10, 0.42, False),
        (0.15, 0.42, False), (0.20, 0.42, False), (0.05, 0.50, True),
        (0.10, 0.50, False),
    ]:
        c.record(p_pred=0.42, sigma=sigma, drift=drift, won=won)
    # n=10 in 1D p-bin, WR = 5/10 = 0.5
    # Each (σ, drift) bucket has n<min_n
    adj = c.adjust(0.42, 0.18, 0.42)
    expected = 0.4 * 0.5 + 0.6 * 0.42  # 1D fallback
    assert abs(adj - expected) < 0.02


def test_cold_identity_below_min_n():
    """With < min_n samples even in 1D, return p_pred unchanged."""
    c = Calibrator2(window=200, min_n=8, blend=0.4)
    for _ in range(3):
        c.record(p_pred=0.42, sigma=0.15, drift=0.42, won=False)
    adj = c.adjust(0.42, 0.15, 0.42)
    assert abs(adj - 0.42) < 1e-9, "below min_n in all hierarchies should return identity"


def test_window_rolling():
    """Window should bound history; old data is dropped."""
    c = Calibrator2(window=10, min_n=3, blend=0.5)
    for _ in range(15):  # only last 10 retained
        c.record(p_pred=0.30, sigma=0.05, drift=0.10, won=False)
    assert len(c.history) == 10


def test_gap_summary_emits_string():
    c = Calibrator2(window=200, min_n=5, blend=0.4)
    for _ in range(10):
        c.record(p_pred=0.42, sigma=0.15, drift=0.42, won=False)
    out = c.gap_summary(top_k=3)
    assert "n=10" in out
    assert "gap=+0.42" in out  # avg_p=0.42, actual_WR=0.0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            raise
    print(f"\n{len(tests)}/{len(tests)} tests passed")
