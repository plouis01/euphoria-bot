"""
Segment-crossing band-touch resolver.

Used by both the live bot (`dryrun_bot_v3.py`) and the offline log re-scorer
(`scripts/rescore_logs.py`) so both produce identical answers.

Why this exists: the original resolver checks `any(lo <= p < hi for _, p in
ticks)`, which only finds touches that left a sampled tick *inside* the band.
At ~5-50 Hz sampling, a path can cross the band between two observed ticks
without leaving a sample inside — undercounting wins by exactly the
proportion of touches whose only crossings happen between samples.

`touched_band` returns True whenever the piecewise-linear interpolation of
the tick sequence enters the half-open band [lo, hi). That's a deterministic
lower bound on true touch (it still misses path excursions that exit and
re-enter unobserved between two ticks, which a Brownian bridge says is rare
at 5-50 Hz).
"""
from typing import Iterable, Tuple


Tick = Tuple[int, float]  # (ts_ms, price)


def segment_crosses(p1: float, p2: float, lo: float, hi: float) -> bool:
    """True iff the linear segment from p1 to p2 enters [lo, hi)."""
    return min(p1, p2) < hi and max(p1, p2) >= lo


def touched_band(ticks: Iterable[Tick], lo: float, hi: float) -> bool:
    """Return True iff the piecewise-linear path through `ticks` enters [lo, hi).

    `ticks` must be in chronological order. Each tick is (ts_ms, price).
    """
    ticks = list(ticks)
    if not ticks:
        return False
    if len(ticks) == 1:
        return lo <= ticks[0][1] < hi
    for (_, p1), (_, p2) in zip(ticks, ticks[1:]):
        if segment_crosses(p1, p2, lo, hi):
            return True
    return False
