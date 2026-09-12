"""Hitter Power Profile model -- Category 2 of the HR Alpha Score (25 pts).

Same approach as models/pitcher_model.py: linear normalization against
documented fixed MLB benchmarks, not percentile-based yet (see TODO at
bottom -- same upgrade path applies here).

Point allocation (25 total):
    Barrel%              8 pts
    Hard-hit%            6 pts
    ISO                  5 pts
    xSLG                 4 pts
    Avg exit velocity    2 pts
    Recent quality contact / recent form   0 pts  -- TODO, see bottom

Platoon splits (HR/ISO vs LHP/RHP) are NOT applied yet -- this function
accepts opposing_pitcher_hand as a documented placeholder parameter so
callers don't need to change their call signature once splits data is
wired in, but it currently has no effect on the score. See TODO.
"""

from __future__ import annotations

from models._scoring_utils import normalize

BARREL_PCT_RANGE = (6.0, 18.0)         # percent
HARD_HIT_PCT_RANGE = (30.0, 50.0)      # percent
ISO_RANGE = (0.120, 0.280)
XSLG_RANGE = (0.350, 0.600)
EXIT_VELOCITY_RANGE = (87.0, 95.0)     # mph

WEIGHTS = {
    "barrel_pct": 8,
    "hard_hit_pct": 6,
    "iso": 5,
    "xslg": 4,
    "exit_velocity_avg": 2,
    "recent_form": 0,  # TODO -- not implemented yet
}


def score_hitter_power(hitter: dict, opposing_pitcher_hand: str | None = None) -> dict:
    """Score a hitter's power profile out of 25 points.

    Args:
        hitter: a dict expected to contain (missing keys score as 0
            contribution, not a penalty):
                "barrel_pct": float          -- ingestion.statcast (barrel_batted_rate)
                "hard_hit_pct": float        -- ingestion.statcast (hard_hit_percent)
                "iso": float                  -- ingestion.mlb_data.fetch_hitting_stats
                "xslg": float                 -- ingestion.statcast (xslg)
                "exit_velocity_avg": float    -- ingestion.statcast (exit_velocity_avg)
        opposing_pitcher_hand: "L" or "R". Documented placeholder --
            platoon-split adjustment is not implemented yet (see module
            docstring), this currently has no effect on the score.

    Returns:
        {
            "score": float,  # 0-25
            "breakdown": {
                "barrel_pct": {"raw": float | None, "points": float, "max_points": int},
                "hard_hit_pct": {...},
                "iso": {...},
                "xslg": {...},
                "exit_velocity_avg": {...},
                "recent_form": {"raw": None, "points": 0, "max_points": 0,
                                  "note": "not implemented -- needs rolling/recent Statcast data"},
            },
        }
    """
    breakdown = {}

    barrel = hitter.get("barrel_pct")
    barrel_points = normalize(barrel, *BARREL_PCT_RANGE) * WEIGHTS["barrel_pct"]
    breakdown["barrel_pct"] = {"raw": barrel, "points": round(barrel_points, 2), "max_points": WEIGHTS["barrel_pct"]}

    hard_hit = hitter.get("hard_hit_pct")
    hard_hit_points = normalize(hard_hit, *HARD_HIT_PCT_RANGE) * WEIGHTS["hard_hit_pct"]
    breakdown["hard_hit_pct"] = {"raw": hard_hit, "points": round(hard_hit_points, 2), "max_points": WEIGHTS["hard_hit_pct"]}

    iso = hitter.get("iso")
    iso_points = normalize(iso, *ISO_RANGE) * WEIGHTS["iso"]
    breakdown["iso"] = {"raw": iso, "points": round(iso_points, 2), "max_points": WEIGHTS["iso"]}

    xslg = hitter.get("xslg")
    xslg_points = normalize(xslg, *XSLG_RANGE) * WEIGHTS["xslg"]
    breakdown["xslg"] = {"raw": xslg, "points": round(xslg_points, 2), "max_points": WEIGHTS["xslg"]}

    exit_velo = hitter.get("exit_velocity_avg")
    exit_velo_points = normalize(exit_velo, *EXIT_VELOCITY_RANGE) * WEIGHTS["exit_velocity_avg"]
    breakdown["exit_velocity_avg"] = {
        "raw": exit_velo, "points": round(exit_velo_points, 2), "max_points": WEIGHTS["exit_velocity_avg"],
    }

    breakdown["recent_form"] = {
        "raw": None, "points": 0, "max_points": 0,
        "note": "not implemented -- needs rolling/recent-game Statcast data, "
                "season leaderboards don't carry a recency signal",
    }

    total = round(
        barrel_points + hard_hit_points + iso_points + xslg_points + exit_velo_points, 2
    )

    return {"score": total, "breakdown": breakdown}


# TODO (future upgrade): same percentile-vs-fixed-benchmark upgrade path
# as models/pitcher_model.py -- see that file's matching TODO.

# TODO: platoon splits. opposing_pitcher_hand is accepted here but
# unused. To implement: pull HR/ISO vs LHP and vs RHP splits (MLB Stats
# API supports this via stats=statSplits, not yet wired into
# ingestion.mlb_data.fetch_hitting_stats), then select the relevant
# split's ISO based on opposing_pitcher_hand before scoring instead of
# always using the season-overall ISO.

# TODO: recent_form sub-score (currently 0 pts) needs a rolling window
# of recent Statcast data (e.g. last 15 games) rather than a full-season
# leaderboard -- ingestion.statcast as currently implemented only pulls
# season-long leaderboards.
