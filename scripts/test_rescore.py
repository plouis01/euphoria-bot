"""Tests for rescore_logs.rescore(). Run with: pytest scripts/test_rescore.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.rescore_logs import rescore, load_settlements  # noqa: E402


def _line(eu="LOSS", pnl=-1.0, cx=151, cy=4280, band=(2140.00, 2140.50)):
    lo, hi = band
    return (f"[c30s] [bot] resolve cell=({cx},{cy}) band=[{lo:.2f},{hi:.2f}) "
            f"strat=c30-strong eu_n=12 cb_n=43 EU={eu} CB=LOSS "
            f"p_pred=0.231 ev_pred=+0.045 σ_at_bet=0.124 "
            f"drift_at_bet=$+0.41 pnl=${pnl:+.4f}\n")


def _entry(cx, cy, ticks):
    return {"tradeId": "abc", "matched_current_tap": True,
            "cellX": cx, "cellY": cy, "priceTicks": ticks}


def test_no_ticks_passthrough():
    line = _line(eu="LOSS", pnl=-1.0)
    out, marker = rescore(line, {})
    assert marker == "no_ticks"
    assert "RESCORE=no_ticks" in out


def test_flip_loss_to_win_via_segment_crossing():
    # The original verdict was LOSS but ticks straddle the band: should flip.
    ticks = [(1000, 2139.95), (1100, 2140.55)]  # crosses [2140.0, 2140.5)
    by_cell = {(151, 4280): [_entry(151, 4280, ticks)]}
    line = _line(eu="LOSS", pnl=-1.0)
    out, marker = rescore(line, by_cell)
    assert marker == "changed", out
    assert "EU=WIN" in out
    # mult is unknown from a LOSS line; emit the flag and leave pnl as-is.
    assert "RESCORE=changed_mult_unknown" in out


def test_keep_win_when_ticks_confirm_in_band():
    ticks = [(1000, 2140.20)]  # already in band
    by_cell = {(151, 4280): [_entry(151, 4280, ticks)]}
    line = _line(eu="WIN", pnl=1.4)  # implies mult=2.4
    out, marker = rescore(line, by_cell)
    assert marker == "same", out
    assert "EU=WIN" in out


def test_flip_win_to_loss_recomputes_pnl_to_minus_stake():
    # WIN original pnl encodes mult=2.4; ticks say it never touched -> flip to LOSS.
    ticks = [(1000, 2139.5), (1100, 2139.7)]  # both below band
    by_cell = {(151, 4280): [_entry(151, 4280, ticks)]}
    line = _line(eu="WIN", pnl=1.4)
    out, marker = rescore(line, by_cell)
    assert marker == "changed", out
    assert "EU=LOSS" in out
    assert "pnl=$-1.0000" in out


def test_load_settlements_handles_garbage_lines():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"tradeId": "a"}) + "\n")
        f.write("not json\n")
        f.write("\n")
        f.write(json.dumps({"tradeId": "b"}) + "\n")
        path = f.name
    try:
        entries = load_settlements(path)
        assert len(entries) == 2
        assert entries[0]["tradeId"] == "a"
        assert entries[1]["tradeId"] == "b"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    import sys as _sys
    failures = 0
    tests = [(n, fn) for n, fn in globals().items() if n.startswith("test_") and callable(fn)]
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
