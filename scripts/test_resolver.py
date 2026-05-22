"""Tests for resolver.touched_band. Run with: pytest scripts/test_resolver.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resolver import segment_crosses, touched_band


def _t(ts, p):
    return (ts, p)


def test_empty():
    assert touched_band([], 2140.0, 2140.5) is False


def test_single_in_band():
    assert touched_band([_t(0, 2140.25)], 2140.0, 2140.5) is True


def test_single_below_band():
    assert touched_band([_t(0, 2139.5)], 2140.0, 2140.5) is False


def test_single_at_upper_open_edge():
    # hi is open: 2140.5 is NOT in [2140.0, 2140.5)
    assert touched_band([_t(0, 2140.5)], 2140.0, 2140.5) is False


def test_single_at_lower_closed_edge():
    # lo is closed: 2140.0 IS in [2140.0, 2140.5)
    assert touched_band([_t(0, 2140.0)], 2140.0, 2140.5) is True


def test_two_ticks_straddle_band_no_inside_sample():
    # The intra-second-touch bug: neither endpoint is in band, but the
    # segment between them crosses the band.
    ticks = [_t(0, 2139.95), _t(100, 2140.55)]
    assert touched_band(ticks, 2140.0, 2140.5) is True


def test_two_ticks_both_below():
    ticks = [_t(0, 2139.5), _t(100, 2139.7)]
    assert touched_band(ticks, 2140.0, 2140.5) is False


def test_two_ticks_both_above_open_edge():
    # 2140.5 is on the open upper edge; if both endpoints are at or above hi,
    # the segment lies entirely outside the half-open band.
    ticks = [_t(0, 2140.5), _t(100, 2140.6)]
    assert touched_band(ticks, 2140.0, 2140.5) is False


def test_two_ticks_one_in_band():
    ticks = [_t(0, 2139.5), _t(100, 2140.2)]
    assert touched_band(ticks, 2140.0, 2140.5) is True


def test_three_ticks_middle_in_band():
    ticks = [_t(0, 2139.5), _t(50, 2140.2), _t(100, 2141.0)]
    assert touched_band(ticks, 2140.0, 2140.5) is True


def test_three_ticks_passes_over_band():
    # Path goes 2139.5 -> 2140.2 -> 2141.0. The first segment enters the band.
    ticks = [_t(0, 2139.5), _t(50, 2140.2), _t(100, 2141.0)]
    assert touched_band(ticks, 2140.0, 2140.5) is True


def test_path_crosses_band_via_long_jump():
    # Single large jump from below to above band (no inside-band sample).
    ticks = [_t(0, 2138.0), _t(100, 2142.0)]
    assert touched_band(ticks, 2140.0, 2140.5) is True


def test_segment_crosses_at_exactly_lo():
    # Lower edge is closed: segment that just touches lo at endpoint counts.
    assert segment_crosses(2139.5, 2140.0, 2140.0, 2140.5) is True


def test_segment_crosses_at_exactly_hi():
    # Upper edge is open: segment that ends exactly at hi from below does NOT count
    # unless it actually went below hi (in which case it must have been at < hi
    # for some interior, which still counts).
    # p1=2140.5, p2=2140.5: max < hi? 2140.5 < 2140.5 = False -> not crossed
    assert segment_crosses(2140.5, 2140.5, 2140.0, 2140.5) is False
    # But p1=2140.4, p2=2140.5 DOES cross (interior is in band)
    assert segment_crosses(2140.4, 2140.5, 2140.0, 2140.5) is True


if __name__ == "__main__":
    import sys as _sys
    failures = 0
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            print(f"FAIL  {name}: {e}")
            failures += 1
        except Exception as e:
            print(f"ERROR {name}: {e}")
            failures += 1
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    _sys.exit(1 if failures else 0)
