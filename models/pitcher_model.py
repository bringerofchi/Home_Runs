"""Pitcher Vulnerability model -- Category 1 of the HR Alpha Score (35 pts).

Scoring approach: each metric is linearly normalized against a fixed,
documented MLB benchmark range (roughly "elite" to "vulnerable" for
recent-era league norms), then weighted into a point total. This is
deliberately simple and fully transparent -- every point is traceable
to a raw stat and a stated benchmark. It is NOT percentile-based yet:
that would require pulling the full qualified-pitcher leaderboard on
every run and ranking against the live field, which is a reasonable
future upgrade (see TODO at the bottom) but overkill for a first pass.

Point allocation (35 total):
    HR/9                 12 pts  -- most direct HR-vulnerability signal
    Barrel% allowed       10 pts  -- quality-of-contact allowed
    Hard-hit% allowed      8 pts  -- quality-of-contact allowed
    Fly-ball% allowed       5 pts  -- HR requires a fly ball
    Pitch-type weakness     0 pts  -- TODO, see bottom of file

Benchmarks are intentionally documented inline so they can be tuned
without hunting through the function body.
"""

from __future__ import annotations

from models._scoring_utils import normalize

# (low=weakest/least vulnerable, high=strongest/most vulnerable) benchmarks.
# Most of these are rough recent-era MLB estimates, not derived from a
# specific season's data -- revisit each via models/calibration.py's
# audit tool once real league-wide leaderboards are wired in (see TODO
# at bottom).
HR9_RANGE = (0.5, 2.0)            # HR per 9 innings
BARREL_PCT_ALLOWED_RANGE = (4.0, 12.0)   # percent
HARD_HIT_PCT_ALLOWED_RANGE = (30.0, 45.0)  # percent

# FLY_BALL_PCT_RANGE: CALIBRATED from real data, not an estimate.
# Originally (30.0, 50.0) -- an untested assumption that turned out to
# be badly wrong. Live production data (642 saved player-rows) showed
# this range was capturing only 0.9% of its available points on
# average, versus 16-18% for sibling metrics from the same data pull --
# a near-total scoring collapse. Root cause, confirmed via
# models/calibration.py's audit against 523 real 2026-season pitchers
# (Baseball Savant): real fly-ball rates run p10=19.0, p50=26.2,
# p90=34.76 -- the old range's floor (30.0) sat above the real MEDIAN,
# so most pitchers scored a hard 0 on this sub-metric regardless of
# their actual fly-ball tendency. Updated to bracket the real p10-p90
# spread. Re-run the calibration audit periodically to confirm this
# still holds as more of the season accumulates.
FLY_BALL_PCT_RANGE = (19.0, 34.76)        # percent

WEIGHTS = {
    "hr9": 12,
    "barrel_pct_allowed": 10,
    "hard_hit_pct_allowed": 8,
    "fly_ball_pct": 5,
    "pitch_type_weakness": 0,  # TODO -- not implemented yet
}


def score_pitcher_vulnerability(pitcher: dict) -> dict:
    """Score a pitcher's HR vulnerability out of 35 points.

    Args:
        pitcher: a dict expected to contain (missing keys score as 0
            contribution for that sub-metric, not as a penalty):
                "hr9": float                    -- from ingestion.mlb_data.fetch_pitching_stats
                "barrel_pct_allowed": float      -- from ingestion.statcast.fetch_pitcher_contact_metrics
                "hard_hit_pct_allowed": float    -- from ingestion.statcast.fetch_pitcher_contact_metrics
                "fly_ball_pct": float             -- from ingestion.statcast.fetch_pitcher_contact_metrics
            Field names deliberately don't match the raw Statcast CSV
            column names 1:1 -- map them when joining ingestion output
            into this model (barrel_batted_rate -> barrel_pct_allowed,
            hard_hit_percent -> hard_hit_pct_allowed, flyballs_percent
            -> fly_ball_pct).

    Returns:
        {
            "score": float,  # 0-35
            "breakdown": {
                "hr9": {"raw": float | None, "points": float, "max_points": int},
                "barrel_pct_allowed": {...},
                "hard_hit_pct_allowed": {...},
                "fly_ball_pct": {...},
                "pitch_type_weakness": {"raw": None, "points": 0, "max_points": 0,
                                          "note": "not implemented -- needs pitch arsenal data"},
            },
        }
    """
    breakdown = {}

    hr9 = pitcher.get("hr9")
    hr9_points = normalize(hr9, *HR9_RANGE) * WEIGHTS["hr9"]
    breakdown["hr9"] = {"raw": hr9, "points": round(hr9_points, 2), "max_points": WEIGHTS["hr9"]}

    barrel = pitcher.get("barrel_pct_allowed")
    barrel_points = normalize(barrel, *BARREL_PCT_ALLOWED_RANGE) * WEIGHTS["barrel_pct_allowed"]
    breakdown["barrel_pct_allowed"] = {
        "raw": barrel, "points": round(barrel_points, 2), "max_points": WEIGHTS["barrel_pct_allowed"],
    }

    hard_hit = pitcher.get("hard_hit_pct_allowed")
    hard_hit_points = normalize(hard_hit, *HARD_HIT_PCT_ALLOWED_RANGE) * WEIGHTS["hard_hit_pct_allowed"]
    breakdown["hard_hit_pct_allowed"] = {
        "raw": hard_hit, "points": round(hard_hit_points, 2), "max_points": WEIGHTS["hard_hit_pct_allowed"],
    }

    fly_ball = pitcher.get("fly_ball_pct")
    fly_ball_points = normalize(fly_ball, *FLY_BALL_PCT_RANGE) * WEIGHTS["fly_ball_pct"]
    breakdown["fly_ball_pct"] = {
        "raw": fly_ball, "points": round(fly_ball_points, 2), "max_points": WEIGHTS["fly_ball_pct"],
    }

    breakdown["pitch_type_weakness"] = {
        "raw": None, "points": 0, "max_points": 0,
        "note": "not implemented -- needs pitch arsenal / pitch-type run-value data",
    }

    total = round(
        hr9_points + barrel_points + hard_hit_points + fly_ball_points, 2
    )

    return {"score": total, "breakdown": breakdown}


# TODO (future upgrade): switch from fixed benchmarks to percentile-rank
# scoring against the live qualified-pitcher field for the season. That
# needs the full leaderboard (ingestion.statcast.fetch_pitcher_contact_metrics
# already returns it for every qualified pitcher) -- compute each
# pitcher's percentile within that list instead of comparing to the
# fixed HR9_RANGE-style constants above. More accurate season-to-season,
# but couples this module to always having the full leaderboard on hand.

# TODO: pitch-type weakness sub-score (currently 0 pts, contributes
# nothing) needs pitch arsenal usage data + hitter success rate by pitch
# type -- not available from either ingestion.mlb_data or
# ingestion.statcast as currently implemented.
