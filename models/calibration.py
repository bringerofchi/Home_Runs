"""Benchmark range calibration audit.

Every scoring sub-metric in models/pitcher_model.py and
models/hitter_model.py is scored by linearly normalizing a raw stat
against a hardcoded (low, high) benchmark range -- see those modules'
own docstrings. Those ranges were originally rough estimates, not
derived from real data. One of them (FLY_BALL_PCT_RANGE) was found to
be significantly miscalibrated after live testing -- real pitcher
fly-ball rates (15-28% in a real sample) clustered well below the
range's assumed floor (30%), silently zeroing out that sub-metric's
contribution for nearly every pitcher.

This module answers, for EVERY benchmark range across both files, using
real data pulled live: is the current range roughly right, or is it
miscalibrated the same way FLY_BALL_PCT_RANGE was?

Design choice: this imports the actual live constants from
models.pitcher_model / models.hitter_model directly, rather than
hardcoding a second copy of them here -- so this audit can never
silently drift out of sync with what's actually running in production.

This is a diagnostic/audit tool only. It does NOT modify any scoring
code or apply its own suggestions automatically -- see
run_calibration_audit()'s return value for suggested ranges, which are
for human review, not auto-applied.
"""

from __future__ import annotations

import math

from ingestion import mlb_data, statcast
from models import pitcher_model, hitter_model

# Each entry describes one benchmark range to audit: which model constant
# it corresponds to, which data source/field to pull real values from.
_METRIC_SPECS = [
    {
        "metric": "hr9",
        "model": "pitcher_model.HR9_RANGE",
        "current_range": pitcher_model.HR9_RANGE,
        "source": "mlb_stats_api_bulk_pitching",
        "field": "hr9",
    },
    {
        "metric": "barrel_pct_allowed",
        "model": "pitcher_model.BARREL_PCT_ALLOWED_RANGE",
        "current_range": pitcher_model.BARREL_PCT_ALLOWED_RANGE,
        "source": "statcast_pitcher",
        "field": "barrel_batted_rate",
    },
    {
        "metric": "hard_hit_pct_allowed",
        "model": "pitcher_model.HARD_HIT_PCT_ALLOWED_RANGE",
        "current_range": pitcher_model.HARD_HIT_PCT_ALLOWED_RANGE,
        "source": "statcast_pitcher",
        "field": "hard_hit_percent",
    },
    {
        "metric": "fly_ball_pct",
        "model": "pitcher_model.FLY_BALL_PCT_RANGE",
        "current_range": pitcher_model.FLY_BALL_PCT_RANGE,
        "source": "statcast_pitcher",
        "field": "flyballs_percent",
    },
    {
        "metric": "barrel_pct",
        "model": "hitter_model.BARREL_PCT_RANGE",
        "current_range": hitter_model.BARREL_PCT_RANGE,
        "source": "statcast_batter",
        "field": "barrel_batted_rate",
    },
    {
        "metric": "hard_hit_pct",
        "model": "hitter_model.HARD_HIT_PCT_RANGE",
        "current_range": hitter_model.HARD_HIT_PCT_RANGE,
        "source": "statcast_batter",
        "field": "hard_hit_percent",
    },
    {
        "metric": "iso",
        "model": "hitter_model.ISO_RANGE",
        "current_range": hitter_model.ISO_RANGE,
        "source": "mlb_stats_api_bulk_hitting",
        "field": "iso",
    },
    {
        "metric": "xslg",
        "model": "hitter_model.XSLG_RANGE",
        "current_range": hitter_model.XSLG_RANGE,
        "source": "statcast_batter",
        "field": "xslg",
    },
    {
        "metric": "exit_velocity_avg",
        "model": "hitter_model.EXIT_VELOCITY_RANGE",
        "current_range": hitter_model.EXIT_VELOCITY_RANGE,
        "source": "statcast_batter",
        "field": "exit_velocity_avg",
    },
]


def _population_description(
    source: str, statcast_min_sample: str, min_innings_pitched: float = 0.0, min_plate_appearances: int = 0,
) -> str:
    """Every metric here is benchmarked against a real but DIFFERENT
    population than the one production scoring actually sees -- see
    module docstring. This makes that scope explicit in the audit
    output itself, rather than something that has to be remembered or
    re-explained each time the audit runs. A "Reasonable" verdict means
    reasonable FOR THIS POPULATION, not proven-optimal for the model's
    real scoring population (confirmed starters facing a real opposing
    pitcher on a given day) -- that would need a separate audit built
    off the alpha_scores table itself, once enough real scored rows
    have accumulated. Not built yet; see TODO at bottom of file.
    """
    if source in ("statcast_pitcher", "statcast_batter"):
        return (
            f"Statcast leaderboard, min_sample={statcast_min_sample} "
            f"(players with at least that many batted-ball events -- "
            f"broader than confirmed starters only)"
        )
    if source == "mlb_stats_api_bulk_pitching":
        threshold_note = (
            f", filtered to >{min_innings_pitched} IP for this audit run" if min_innings_pitched > 0 else ""
        )
        return (
            f"Every pitcher with any recorded IP this season (playerPool=ALL){threshold_note} -- "
            f"{'without the filter, ' if not threshold_note else ''}"
            f"includes part-time pitchers and extreme small-sample outliers. "
            f"Much broader than the model's real scoring population unless filtered above."
        )
    if source == "mlb_stats_api_bulk_hitting":
        threshold_note = (
            f", filtered to >{min_plate_appearances} PA for this audit run" if min_plate_appearances > 0 else ""
        )
        return (
            f"Every hitter with any recorded PA this season (playerPool=ALL){threshold_note} -- "
            f"{'without the filter, ' if not threshold_note else ''}"
            f"includes part-time players and extreme small-sample outliers. "
            f"Much broader than the model's real scoring population unless filtered above."
        )
    return "Unknown source -- population scope not documented for this metric."


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile, no external dependency (keeps
    this transparent and dependency-free, consistent with the rest of
    the project). pct in [0, 100]. Returns None for an empty list.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]

    k = (len(sorted_values) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _extract_values(rows: list[dict], field: str) -> list[float]:
    """Pull field from every row, casting to float, skipping missing/
    non-numeric values. Rows here may come from Statcast (raw strings
    from CSV) or MLB Stats API bulk pulls (already floats) -- handle both.
    """
    values = []
    for row in rows:
        raw = row.get(field)
        if raw is None or raw == "":
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return sorted(values)


def _statistical_verdict(current_low: float, current_high: float, p10: float, p50: float, p75: float) -> str:
    """Dimension 1: how far off is the range, statistically?

    This alone MISSED the real severity of the fly_ball_pct bug --  it
    only classified that case as "POSSIBLY TOO NARROW/HIGH" (the mild
    tier), while production data showed a 99.1% scoring collapse. That
    gap is exactly why _production_impact() below exists as a SEPARATE
    dimension, not folded into this one -- a range can overlap the real
    distribution just enough to read as merely "possibly too narrow"
    while still crushing nearly every real player to the floor. This
    function is intentionally left as pure statistical description; it
    does not try to also encode severity of real-world harm.
    """
    if current_low > p75:
        return (
            f"MISCALIBRATED: current low ({current_low}) is above the real 75th percentile "
            f"({p75:.1f}) -- most real values fall below this range."
        )
    if current_high < p50:
        return (
            f"MISCALIBRATED: current high ({current_high}) is below the real median ({p50:.1f}) "
            f"-- over half of real players would max out this sub-metric."
        )
    if current_low > p50:
        return (
            f"POSSIBLY TOO NARROW/HIGH: current low ({current_low}) is above the real median "
            f"({p50:.1f})."
        )
    if current_high < p75:
        return (
            f"POSSIBLY TOO NARROW/LOW: current high ({current_high}) is below the real 75th "
            f"percentile ({p75:.1f})."
        )
    return "Reasonable -- current range roughly brackets the real distribution."


# Production-impact severity tiers, based on the % of real values that
# get crushed to a hard 0 or hard max under the CURRENT range. These
# thresholds are a judgment call, not derived from anything -- tune them
# if they don't feel right once you've seen a few real audits. The
# fly_ball_pct incident (99.1% collapse) anchors the top of the scale.
_IMPACT_TIERS = [
    (90.0, "CRITICAL"),
    (50.0, "SEVERE"),
    (20.0, "MODERATE"),
    (5.0, "MINOR"),
    (0.0, "NEGLIGIBLE"),
]


def _production_impact(values: list[float], current_low: float, current_high: float) -> dict:
    """Dimension 2: regardless of what the statistics say, how many real
    players actually get crushed to a hard 0 or hard max points under
    the CURRENT live range? This is the number that would have
    correctly flagged fly_ball_pct as severe on day one -- 99.1% of real
    pitchers scored a hard 0, which no purely-statistical description
    (current_low vs. percentiles) captured as clearly.

    Returns:
        {
            "pct_crushed_floor": float,   # % of values scoring exactly 0
            "pct_crushed_ceiling": float,  # % of values scoring exactly max
            "pct_crushed_total": float,
            "severity": str,  # one of _IMPACT_TIERS' labels
        }
    """
    if not values or current_low == current_high:
        return {"pct_crushed_floor": 0.0, "pct_crushed_ceiling": 0.0, "pct_crushed_total": 0.0, "severity": "NEGLIGIBLE"}

    crushed_floor = 0
    crushed_ceiling = 0
    for v in values:
        fraction = (v - current_low) / (current_high - current_low)
        if fraction <= 0.0:
            crushed_floor += 1
        elif fraction >= 1.0:
            crushed_ceiling += 1

    total = len(values)
    pct_floor = round(crushed_floor / total * 100, 1)
    pct_ceiling = round(crushed_ceiling / total * 100, 1)
    pct_total = round(pct_floor + pct_ceiling, 1)

    severity = next(label for threshold, label in _IMPACT_TIERS if pct_total >= threshold)

    return {
        "pct_crushed_floor": pct_floor,
        "pct_crushed_ceiling": pct_ceiling,
        "pct_crushed_total": pct_total,
        "severity": severity,
    }


def run_calibration_audit(
    season: int,
    statcast_min_sample: str = statcast.DEFAULT_MIN_SAMPLE,
    min_innings_pitched: float = 0.0,
    min_plate_appearances: int = 0,
) -> dict:
    """Pull real league-wide distributions for every benchmark range in
    pitcher_model.py and hitter_model.py, and report whether each one
    looks calibrated.

    Args:
        season: year.
        statcast_min_sample: passed through to the Statcast leaderboard
            pulls -- defaults to the same threshold production scoring
            actually uses (see ingestion.statcast.DEFAULT_MIN_SAMPLE),
            so this audits against the SAME population the model scores,
            not an artificially larger or smaller one.
        min_innings_pitched / min_plate_appearances: passed through to
            the two MLB Stats API bulk pulls (hr9, iso) ONLY -- these
            two metrics use playerPool=ALL, which includes extreme
            small-sample outliers (e.g. a 1-inning call-up) that Statcast's
            own min_sample filter already screens out for the other 7
            metrics. Default 0 = no additional filter (matches production
            behavior). Raise these to test whether a high production-
            impact crush rate on hr9/iso is a genuine range-calibration
            problem or an artifact of including these extreme outliers --
            see the real finding that motivated this: hr9/iso both showed
            ~40% crush rate at MODERATE severity with these at 0, despite
            "Reasonable" statistical verdicts.

    Returns:
        {
            "season": int,
            "metrics": [
                {
                    "metric": str, "model": str,
                    "current_low": float, "current_high": float,
                    "sample_count": int, "population": str,
                    "real_min": float, "p10": float, "p25": float,
                    "p50": float, "p75": float, "p90": float, "real_max": float,
                    "suggested_low": float,   # real p10 -- a starting point for
                    "suggested_high": float,  # human review, NOT auto-applied
                    "statistical_verdict": str,
                    "pct_crushed_floor": float, "pct_crushed_ceiling": float,
                    "pct_crushed_total": float, "production_impact_severity": str,
                    "verdict": str,  # combined, human-readable
                },
                ...
            ],
            "warnings": [str, ...],
        }
    """
    warnings = []

    # Pull all four data sources once, up front -- multiple metrics share
    # the same source (e.g. 3 pitcher metrics all come from one Statcast
    # pitcher leaderboard pull).
    sources: dict[str, list[dict]] = {}

    try:
        sources["statcast_pitcher"] = statcast.fetch_pitcher_contact_metrics(season, min_sample=statcast_min_sample)
    except Exception as exc:
        sources["statcast_pitcher"] = []
        warnings.append(f"Statcast pitcher leaderboard unavailable: {exc}")

    try:
        sources["statcast_batter"] = statcast.fetch_batter_power_metrics(season, min_sample=statcast_min_sample)
    except Exception as exc:
        sources["statcast_batter"] = []
        warnings.append(f"Statcast batter leaderboard unavailable: {exc}")

    try:
        sources["mlb_stats_api_bulk_pitching"] = mlb_data.fetch_league_pitching_stats(
            season, min_innings_pitched=min_innings_pitched,
        )
    except Exception as exc:
        sources["mlb_stats_api_bulk_pitching"] = []
        warnings.append(f"MLB Stats API bulk pitching stats unavailable: {exc}")

    try:
        sources["mlb_stats_api_bulk_hitting"] = mlb_data.fetch_league_hitting_stats(
            season, min_plate_appearances=min_plate_appearances,
        )
    except Exception as exc:
        sources["mlb_stats_api_bulk_hitting"] = []
        warnings.append(f"MLB Stats API bulk hitting stats unavailable: {exc}")

    metrics = []
    for spec in _METRIC_SPECS:
        rows = sources.get(spec["source"], [])
        values = _extract_values(rows, spec["field"])
        population = _population_description(
            spec["source"], statcast_min_sample, min_innings_pitched, min_plate_appearances,
        )

        if not values:
            metrics.append({
                "metric": spec["metric"], "model": spec["model"],
                "current_low": spec["current_range"][0], "current_high": spec["current_range"][1],
                "sample_count": 0, "population": population,
                "real_min": None, "p10": None, "p25": None, "p50": None, "p75": None, "p90": None, "real_max": None,
                "suggested_low": None, "suggested_high": None,
                "statistical_verdict": "NO DATA -- source unavailable, cannot calibrate this metric right now.",
                "pct_crushed_floor": None, "pct_crushed_ceiling": None, "pct_crushed_total": None,
                "production_impact_severity": "UNKNOWN",
                "verdict": "NO DATA -- source unavailable, cannot calibrate this metric right now.",
            })
            continue

        p10 = _percentile(values, 10)
        p25 = _percentile(values, 25)
        p50 = _percentile(values, 50)
        p75 = _percentile(values, 75)
        p90 = _percentile(values, 90)

        current_low, current_high = spec["current_range"]
        statistical_verdict = _statistical_verdict(current_low, current_high, p10, p50, p75)
        impact = _production_impact(values, current_low, current_high)

        # Combined human-readable verdict -- surfaces BOTH dimensions
        # together, exactly the format that would have correctly flagged
        # fly_ball_pct as urgent from the very first audit: a mild-sounding
        # statistical read paired with a severe production-impact number.
        combined_verdict = f"{statistical_verdict} | Production impact: {impact['severity']}"
        if impact["pct_crushed_total"] > 0:
            combined_verdict += f" -- {impact['pct_crushed_total']}% of real values crushed to floor/ceiling"

        metrics.append({
            "metric": spec["metric"], "model": spec["model"],
            "current_low": current_low, "current_high": current_high,
            "sample_count": len(values), "population": population,
            "real_min": round(values[0], 3), "p10": round(p10, 3), "p25": round(p25, 3),
            "p50": round(p50, 3), "p75": round(p75, 3), "p90": round(p90, 3), "real_max": round(values[-1], 3),
            "suggested_low": round(p10, 3), "suggested_high": round(p90, 3),
            "statistical_verdict": statistical_verdict,
            "pct_crushed_floor": impact["pct_crushed_floor"],
            "pct_crushed_ceiling": impact["pct_crushed_ceiling"],
            "pct_crushed_total": impact["pct_crushed_total"],
            "production_impact_severity": impact["severity"],
            "verdict": combined_verdict,
        })

    return {"season": season, "metrics": metrics, "warnings": warnings}


# TODO (flagged during real usage, not yet built): every metric here is
# benchmarked against a real but different population than what
# production scoring actually sees (see _population_description above).
# A more precise audit would pull distributions from the alpha_scores
# table itself -- real confirmed starters, on real scoring days, exactly
# the population the model evaluates -- instead of an external
# leaderboard. That needs enough accumulated real rows to be meaningful
# (per this project's own Phase 5 discipline: don't over-read small
# samples), so it's intentionally deferred rather than built now. When
# revisited: a "Reasonable" verdict from THIS audit should not be read
# as "optimized for the real scoring population" until that follow-up
# exists -- it only means reasonable for the broader population checked
# here.
#
# STATUS UPDATE (live investigation, hr9 and iso specifically): both
# showed "Reasonable" statistical verdicts but MODERATE production-impact
# crush against the full playerPool=ALL population (hr9: 39.7%, iso:
# 42.9%). Re-ran filtered to min_innings_pitched=20 / min_plate_appearances=50
# (excludes true small-sample flukes like a 1-inning call-up) to test
# whether that explained it:
#     hr9: 743 -> 420 samples, crush 39.7% -> 20.2% (roughly halved)
#     iso: 597 -> 461 samples, crush 42.9% -> 31.7% (cut by about a quarter)
# So small-sample outliers explain PART of the effect, but not all of it
# -- both still show MODERATE crush even filtered to real, meaningful-
# sample players. hr9's crush looks roughly balanced between floor and
# ceiling (range is centered right but slightly narrow both ends); iso's
# looks concentrated almost entirely at the floor (real p10=0.079 well
# below the current floor of 0.12, while real p90=0.23 sits comfortably
# inside the current ceiling of 0.28 -- the ceiling is rarely reached at
# all).
#
# DECISION: hold HR9_RANGE and ISO_RANGE as-is for now, rather than
# adjust off this filtered-but-still-not-exact population. Revisit once
# the alpha_scores-based real-population audit above exists -- this
# signal is real enough to be worth re-checking then, not clean enough
# (yet) to act on with the same confidence as the fly_ball_pct fix, which
# had an unambiguous near-total collapse rather than a partial 20-30%
# effect on an approximated population.
