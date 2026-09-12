"""Pulls Statcast power/quality-of-contact metrics from Baseball Savant.

IMPORTANT -- unofficial data source: unlike ingestion/mlb_data.py (the
official, documented MLB Stats API), Baseball Savant does NOT publish a
supported public API. This module works by requesting a CSV export of
its "Custom Leaderboard" web page, which happens to work today but is
not a stable contract -- Savant can rename/remove columns or change the
export mechanism without notice. If this breaks:
    1. Open https://baseballsavant.mlb.com/leaderboard/custom in a
       browser, build the leaderboard you want, and check the column
       names shown in the exported CSV.
    2. Update the `selections` list and CSV_COLUMNS mapping below to match.

This has not been tested live -- this dev environment has no network
access to baseballsavant.mlb.com. Run it locally and compare a few rows
against the Baseball Savant website before trusting it in the model.

Covers:
    fetch_batter_power_metrics(season)  -- barrel%, hard-hit%, avg/max
        exit velocity, launch angle, xSLG for MLB batters. Defaults to
        a low minimum-sample filter (see DEFAULT_MIN_SAMPLE below), not
        Savant's own "qualified" threshold -- that excluded most of the
        players this project actually cares about (platoon bats,
        rookies, part-time power hitters).
    fetch_pitcher_contact_metrics(season) -- barrel% allowed, hard-hit%
        allowed, GB/LD/FB% allowed for MLB pitchers. Same min-sample
        default as above.

Both return a list of dicts keyed by MLB player ID (mlbam_id) so they
can be joined against ingestion.mlb_data results.
"""

from __future__ import annotations

import csv
import io

import requests

LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/custom"

# Minimum sample size passed to Savant's leaderboard "min" filter.
#
# Savant's own "q" (qualified, ~3.1 PA/team-game for hitters) threshold
# was the default here originally. This project's own sub-metric
# breakdown diagnostic (run against 642 real saved player-rows) found
# that only ~47.5% of scored hitters and ~33.6% of scored pitchers had
# ANY Statcast data at the "q" threshold -- because HR analysis
# specifically targets platoon players, rookies, recent call-ups, and
# part-time power bats, which is exactly the population "q" excludes.
#
# DEFAULT_MIN_SAMPLE lowers that bar substantially to capture those
# players. TRADEOFF, stated plainly: at a low sample size, rates get
# noisy fast -- a player with 3 PA and one lucky home run can show a
# wildly inflated barrel rate, and nothing in the current scoring model
# (models/pitcher_model.py, models/hitter_model.py) discounts for small
# sample size. 10 is a compromise value: low enough to include most
# rostered players with a few weeks of any playing time, high enough to
# filter out true 1-2-PA flukes. Tune this if real recorded results
# suggest it's still too noisy (raise it) or still too exclusive (lower
# it further) -- that's exactly the kind of question this project's own
# Phase 3 (factor validation) is meant to answer with real data rather
# than a guess.
#
# NOTE: Savant's accepted values for this parameter aren't officially
# documented (see module docstring) -- "q" and small integers are both
# known to work from the leaderboard UI, but a numeric minimum hasn't
# been verified against a live response in this dev environment (no
# network access to baseballsavant.mlb.com here). Spot-check this
# against the real website before trusting it fully.
DEFAULT_MIN_SAMPLE = "10"

# Column keys requested from Savant's custom leaderboard, for batters.
BATTER_SELECTIONS = [
    "player_id", "player_name", "barrel_batted_rate", "hard_hit_percent",
    "avg_hyper_speed", "exit_velocity_avg", "exit_velocity_max",
    "launch_angle_avg", "xslg",
]

PITCHER_SELECTIONS = [
    "player_id", "player_name", "barrel_batted_rate", "hard_hit_percent",
    "groundballs_percent", "flyballs_percent", "linedrives_percent",
]


def _fetch_leaderboard_csv_raw(
    player_type: str, season: int, selections: list[str], min_sample: str = DEFAULT_MIN_SAMPLE,
) -> str:
    """Request a Savant custom leaderboard and return the raw CSV text
    (before parsing) -- used both by the normal fetch functions and by
    diagnose_leaderboard() below, which needs to inspect the actual
    header row Savant returns.

    IMPORTANT: decodes as utf-8-sig, not resp.text. Savant's CSV export
    starts with a UTF-8 byte-order-mark (BOM), which resp.text does NOT
    strip. A leading BOM breaks Python's csv module's quote detection on
    the very first header field -- Savant's actual first column is a
    single quoted field containing a comma (something like
    "last_name, first_name"), and with the BOM in front, csv doesn't
    recognize the opening quote, so the comma inside gets treated as a
    real delimiter. That splits one column into two garbage ones AND
    shifts every subsequent column's values by one position for that
    row. utf-8-sig strips a leading BOM if present (and is a safe no-op
    if it isn't).
    """
    resp = requests.get(
        LEADERBOARD_URL,
        params={
            "year": season,
            "type": player_type,       # "batter" or "pitcher"
            "filter": "",
            "min": min_sample,          # see DEFAULT_MIN_SAMPLE docstring for the tradeoff
            "selections": ",".join(selections),
            "chart": "false",
            "sort": selections[2] if len(selections) > 2 else "player_id",
            "sortDir": "desc",
            "csv": "true",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def _parse_csv_text(csv_text: str) -> list[dict]:
    # Defensive second safety net -- in case a BOM survives some other
    # code path into this function directly.
    csv_text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader)


def _fetch_leaderboard_csv(
    player_type: str, season: int, selections: list[str], min_sample: str = DEFAULT_MIN_SAMPLE,
) -> list[dict]:
    """Shared helper: request a Savant custom leaderboard as CSV and
    parse it into a list of dicts.
    """
    csv_text = _fetch_leaderboard_csv_raw(player_type, season, selections, min_sample)
    return _parse_csv_text(csv_text)


def fetch_batter_power_metrics(season: int, min_sample: str = DEFAULT_MIN_SAMPLE) -> list[dict]:
    """Barrel%, hard-hit%, exit velocity, launch angle, xSLG for MLB
    batters in `season`.

    Args:
        season: year.
        min_sample: Savant's leaderboard "min" filter -- defaults to
            DEFAULT_MIN_SAMPLE (see that constant's docstring for the
            small-sample-size tradeoff). Pass "q" for Savant's own
            qualified threshold, or a different integer to tune.

    Returns a list of dicts with (at minimum -- exact keys depend on
    what Savant's CSV export currently names its columns; print one row
    to confirm before relying on this):
        {
            "player_id": str,      # MLB Advanced Media player id
            "player_name": str,
            "barrel_batted_rate": str,   # numeric strings from CSV --
            "hard_hit_percent": str,     # cast to float before use
            "exit_velocity_avg": str,
            "exit_velocity_max": str,
            "launch_angle_avg": str,
            "xslg": str,
        }
    """
    return _fetch_leaderboard_csv("batter", season, BATTER_SELECTIONS, min_sample)


def fetch_pitcher_contact_metrics(season: int, min_sample: str = DEFAULT_MIN_SAMPLE) -> list[dict]:
    """Barrel% allowed, hard-hit% allowed, and batted-ball-type% allowed
    for MLB pitchers in `season`.

    Args:
        season: year.
        min_sample: see fetch_batter_power_metrics's docstring.

    Returns a list of dicts, same caveats as fetch_batter_power_metrics
    -- confirm the exact CSV column names locally before trusting them.
    """
    return _fetch_leaderboard_csv("pitcher", season, PITCHER_SELECTIONS, min_sample)


# ---------------------------------------------------------------------------
# Live-validation diagnostics -- this is the highest-risk ingestion module
# in the project (unofficial data source, no stable contract). This
# function is meant to be run against real data and inspected, not
# trusted blindly. It checks two things a silent column rename or format
# change would otherwise hide:
#   1. Do the columns we asked for (selections) actually come back?
#      Missing columns mean Savant renamed/removed something.
#   2. Do the values that DO come back look like plausible percentages/
#      velocities/etc, or has something clearly gone wrong (non-numeric
#      junk, wildly out-of-range numbers)?
# ---------------------------------------------------------------------------

# (low, high) plausible-value ranges for sanity-checking known fields.
# These are deliberately generous MLB-wide bounds, not tight benchmarks
# -- the goal is catching "this is obviously broken" (e.g. a percentage
# column that's actually returning player IDs), not flagging legitimate
# outliers.
_SANITY_RANGES = {
    "barrel_batted_rate": (0, 100),
    "hard_hit_percent": (0, 100),
    "exit_velocity_avg": (60, 110),
    "exit_velocity_max": (60, 125),
    "launch_angle_avg": (-30, 60),
    "xslg": (0, 1.3),
    "groundballs_percent": (0, 100),
    "flyballs_percent": (0, 100),
    "linedrives_percent": (0, 100),
}


def diagnose_leaderboard(
    player_type: str, season: int, selections: list[str],
    sample_size: int = 20, min_sample: str = DEFAULT_MIN_SAMPLE,
) -> dict:
    """Fetch a leaderboard and report on data-quality issues instead of
    just returning parsed rows.

    Args:
        min_sample: Savant's leaderboard "min" filter -- see
            DEFAULT_MIN_SAMPLE's docstring for the tradeoff. Not to be
            confused with `sample_size`, which controls how many ROWS
            get range-checked below, unrelated to Savant's own filter.

    Returns:
        {
            "player_type": str, "season": int, "row_count": int,
            "expected_columns": [str, ...],   # what we asked Savant for
            "actual_columns": [str, ...],      # what actually came back
            "missing_columns": [str, ...],     # expected but not present -- BAD
            "extra_columns": [str, ...],       # present but not expected -- informational
            "sample_rows": [dict, ...],         # first 5 raw rows, for eyeballing
            "range_warnings": [str, ...],       # values outside plausible bounds
                                                  # or non-numeric, checked across
                                                  # the first `sample_size` rows
        }
    """
    csv_text = _fetch_leaderboard_csv_raw(player_type, season, selections, min_sample)
    rows = _parse_csv_text(csv_text)

    actual_columns = list(rows[0].keys()) if rows else []
    missing_columns = [c for c in selections if c not in actual_columns]
    extra_columns = [c for c in actual_columns if c not in selections]

    range_warnings = []
    for row in rows[:sample_size]:
        player_label = row.get("player_name", "unknown player")
        for field, (low, high) in _SANITY_RANGES.items():
            if field not in row or row[field] in (None, ""):
                continue
            try:
                value = float(row[field])
            except ValueError:
                range_warnings.append(f"{field}={row[field]!r} is non-numeric for {player_label}")
                continue
            if not (low <= value <= high):
                range_warnings.append(
                    f"{field}={value} outside plausible range [{low}, {high}] for {player_label}"
                )

    return {
        "player_type": player_type,
        "season": season,
        "row_count": len(rows),
        "expected_columns": selections,
        "actual_columns": actual_columns,
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "sample_rows": rows[:5],
        "range_warnings": range_warnings,
    }


def diagnose_batter_leaderboard(season: int, min_sample: str = DEFAULT_MIN_SAMPLE) -> dict:
    return diagnose_leaderboard("batter", season, BATTER_SELECTIONS, min_sample=min_sample)


def diagnose_pitcher_leaderboard(season: int, min_sample: str = DEFAULT_MIN_SAMPLE) -> dict:
    return diagnose_leaderboard("pitcher", season, PITCHER_SELECTIONS, min_sample=min_sample)
