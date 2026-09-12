"""Shared scoring helpers for models/pitcher_model.py and
models/hitter_model.py. Not part of the public ingestion/reporting
contract -- internal to the models package.
"""

from __future__ import annotations


def normalize(value: float | None, low: float, high: float) -> float:
    """Linearly map value from [low, high] to [0.0, 1.0], clamped at the
    ends. `low` is the benchmark for "weakest" and `high` for
    "strongest" on this metric -- for metrics where a bigger raw number
    is actually worse (e.g. ERA), pass low > high and the direction
    still works out (the clamp handles both orderings).

    Returns 0.0 if value is None (missing data scores as "no signal",
    not as "worst possible" -- that would unfairly punish players with
    incomplete stat lines).
    """
    if value is None:
        return 0.0
    if low == high:
        raise ValueError("low and high benchmarks cannot be equal")

    fraction = (value - low) / (high - low)
    return max(0.0, min(1.0, fraction))
