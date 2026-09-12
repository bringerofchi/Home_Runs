"""Combines all four categories into the final transparent 0-100 HR
Alpha Score.

Category weights:
    Pitcher Vulnerability : 35 pts (models.pitcher_model)
    Hitter Power Profile  : 25 pts (models.hitter_model)
    Environment           : 25 pts (score_environment, this file)
    Betting Value         : 15 pts (score_betting_value, this file)

model_probability IS NOW CALIBRATED against real results (Phase 5
validation complete -- see estimate_model_probability()'s own docstring
and the comment block above it for the backtest and fit). It was
previously a heuristic multiplier on a league-average HR-per-PA rate;
that heuristic was replaced once 8,189 real (score, outcome) pairs
confirmed it was directionally right but systematically overconfident
at every score tier. Still treat this as v1 of a simple linear fit, not
a finished model -- revisit periodically as more real dates accumulate.
"""

from __future__ import annotations

from ingestion.odds import american_odds_to_profit_multiple
from models._scoring_utils import normalize
from models.hitter_model import score_hitter_power
from models.pitcher_model import score_pitcher_vulnerability

# ---------------------------------------------------------------------------
# Category 3: Environment (25 pts)
# ---------------------------------------------------------------------------

# (low=suppresses HRs, high=boosts HRs) benchmarks.
PARK_HR_FACTOR_RANGE = (90.0, 115.0)   # 100 = neutral park
TEMPERATURE_RANGE = (55.0, 95.0)        # deg F -- warmer air carries the ball further
WIND_OUT_MPH_RANGE = (0.0, 20.0)        # only meaningful when blowing out to the field
GAME_TOTAL_RANGE = (7.0, 11.0)          # Vegas game total (runs)
HUMIDITY_RANGE = (30.0, 90.0)           # percent -- humid air is LESS dense, ball carries
                                          # further (sabermetric consensus, though this is a
                                          # smaller effect than wind/temperature)

ENV_WEIGHTS = {
    "park_hr_factor": 8,
    "wind": 6,
    "temperature": 4,
    "weather": 4,   # humidity, via ingestion.weather.get_game_weather
    "game_total": 3,
}


def score_environment(game: dict) -> dict:
    """Score environmental HR favorability out of 25 points.

    Args:
        game: expected keys (all optional -- missing scores as 0
            contribution, not a penalty). Typically built by merging
            ingestion.mlb_data game info with ingestion.weather.get_game_weather()
            output:
                "park_hr_factor": float   -- 100 = neutral, >100 favors HR.
                    NOT YET INGESTED anywhere (needs Savant's park-factor
                    leaderboard, same unofficial-CSV caveats as
                    ingestion/statcast.py -- not built yet).
                "temperature": float       -- deg F. From
                    ingestion.weather.get_game_weather().
                "wind_out_mph": float      -- wind speed component blowing
                    OUT toward the field (0 if blowing in/across/calm).
                    From ingestion.weather.get_game_weather().
                "humidity_pct": float      -- from ingestion.weather.get_game_weather().
                "game_total": float        -- Vegas total runs line. Would
                    come from a "totals" market in ingestion.odds, which
                    currently only implements the HR player-prop market.

    Returns:
        {"score": float, "breakdown": {...}} -- same shape convention as
        score_pitcher_vulnerability / score_hitter_power.
    """
    breakdown = {}

    park_factor = game.get("park_hr_factor")
    park_points = normalize(park_factor, *PARK_HR_FACTOR_RANGE) * ENV_WEIGHTS["park_hr_factor"]
    breakdown["park_hr_factor"] = {
        "raw": park_factor, "points": round(park_points, 2), "max_points": ENV_WEIGHTS["park_hr_factor"],
    }

    wind_out = game.get("wind_out_mph")
    wind_points = normalize(wind_out, *WIND_OUT_MPH_RANGE) * ENV_WEIGHTS["wind"]
    breakdown["wind"] = {"raw": wind_out, "points": round(wind_points, 2), "max_points": ENV_WEIGHTS["wind"]}

    temperature = game.get("temperature")
    temp_points = normalize(temperature, *TEMPERATURE_RANGE) * ENV_WEIGHTS["temperature"]
    breakdown["temperature"] = {
        "raw": temperature, "points": round(temp_points, 2), "max_points": ENV_WEIGHTS["temperature"],
    }

    humidity = game.get("humidity_pct")
    humidity_points = normalize(humidity, *HUMIDITY_RANGE) * ENV_WEIGHTS["weather"]
    breakdown["weather"] = {
        "raw": humidity, "points": round(humidity_points, 2), "max_points": ENV_WEIGHTS["weather"],
        "note": "humidity only -- precipitation risk not scored, see ingestion.weather",
    }

    game_total = game.get("game_total")
    total_points = normalize(game_total, *GAME_TOTAL_RANGE) * ENV_WEIGHTS["game_total"]
    breakdown["game_total"] = {
        "raw": game_total, "points": round(total_points, 2), "max_points": ENV_WEIGHTS["game_total"],
    }

    total = round(park_points + wind_points + temp_points + humidity_points + total_points, 2)
    return {"score": total, "breakdown": breakdown}


# ---------------------------------------------------------------------------
# Model probability -- CALIBRATED from real data (Phase 5 validation)
# ---------------------------------------------------------------------------

# CALIBRATED from real production data, not a heuristic multiplier.
# Previously this converted the three "quality" category scores into a
# probability via a documented but UNTESTED 0.4x-2.2x multiplier on a
# league-average per-PA HR rate, then compounded across projected plate
# appearances. That heuristic was directionally reasonable (higher scores
# did correlate with more real HRs) but consistently OVERCONFIDENT at
# every score tier once checked against real results -- see backtest below.
#
# BACKTEST: 8,189 real (quality_score, actual HR-or-not) pairs from the
# alpha_scores/results tables, 2026-07-17 through 2026-09-05 (41 game
# dates, auto-recorded from box scores). quality_score here =
# pitcher_vulnerability.score + hitter_power.score + environment.score
# (0-85 scale) -- deliberately EXCLUDES betting_value: betting_value's
# edge/EV points are themselves derived from model_probability, so
# calibrating against the full 0-100 alpha_score would be circular.
#
# Real per-game HR rate by quality-score decile (n=~819/decile), confirming
# a real, monotonic signal (point-biserial r=0.09 -- weak individual
# predictor, but highly significant at n=8,189, and the old heuristic's
# 0.4x-2.2x span is wider than what real outcomes actually support):
#   0.0-15.9  -> 7.20%     31.1-34.0 -> 13.46%
#   15.9-20.0 -> 6.84%     34.0-37.3 -> 11.71%
#   20.0-23.0 -> 6.96%     37.3-42.0 -> 14.25%
#   23.0-25.8 -> 10.40%    42.0-61.3 -> 15.89%  (top decile; real quality
#   25.8-28.4 -> 10.12%                          scores topped out at 61.3,
#   28.4-31.1 -> 13.19%                          not the theoretical 85 --
#                                                 see MAX_CALIBRATED_
#                                                 PROBABILITY note below)
#
# FIT: simple ordinary-least-squares LINEAR regression of quality_score
# against actual hit_hr (0/1) -- same "linear normalize()" philosophy as
# every other benchmark in this project, not a logistic/GLM fit, to stay
# dependency-free and fully transparent (see this project's own
# no-external-dependency convention, e.g. calibration.py's _percentile()):
#   hit_probability = 0.0297 + 0.002798 * quality_score
# R^2 = 0.008 -- LOW, as expected: a single game's HR outcome is a noisy
# binary event, and quality_score is one input among many real-world
# factors (recent form, actual PA count, etc.) this project doesn't
# capture yet. Read this fit as "meaningfully better calibrated than the
# old uncalibrated heuristic," not as "predicts outcomes with confidence."
#
# This REPLACES the old per-PA-compounding approach entirely: since this
# fit is against real PER-GAME outcomes directly (did the player homer
# that game, yes/no), no per-PA compounding step is needed or correct
# anymore -- see projected_pa's docstring below.
#
# Revisit this fit periodically as more dates accumulate in
# alpha_scores/results -- treat this as v1, not final. Bump
# config/model_version.json when it's refit.
QUALITY_PROBABILITY_INTERCEPT = 0.0297
QUALITY_PROBABILITY_SLOPE = 0.002798
MAX_QUALITY_SCORE = 85  # pitcher(35) + hitter(25) + environment(25)

# Sanity bounds. The fit itself stays within these across the full 0-85
# domain (0.030 at quality=0, 0.267 at quality=85) without needing a
# clamp -- but real accumulated data has only ever reached quality=61.3
# so far, so this guards against trusting extrapolation past what's
# actually been observed, same spirit as the old per_pa_prob cap it
# replaces.
MIN_CALIBRATED_PROBABILITY = 0.02
MAX_CALIBRATED_PROBABILITY = 0.35


def estimate_model_probability(
    pitcher_score: float, hitter_score: float, environment_score: float,
    projected_pa: float = 4.2,
) -> float:
    """Convert the three quality-category scores into a calibrated
    per-game HR probability estimate.

    CALIBRATED (Phase 5 validation complete) -- see the module-level
    comment block above this function for the full methodology, backtest
    sample, and fit coefficients. This is a simple linear fit against
    real outcomes, not a black box.

    Args:
        pitcher_score: 0-35, from score_pitcher_vulnerability.
        hitter_score: 0-25, from score_hitter_power.
        environment_score: 0-25, from score_environment.
        projected_pa: DEPRECATED -- kept only so existing callers don't
            need to change their call signature, but no longer used.
            The old heuristic compounded a per-PA rate across projected
            plate appearances to estimate a per-game probability; this
            calibration is fit directly against real per-game outcomes,
            so no compounding step applies. Documented no-op, same
            pattern as models.hitter_model.score_hitter_power's
            opposing_pitcher_hand placeholder.

    Returns:
        Calibrated probability, clamped to
        [MIN_CALIBRATED_PROBABILITY, MAX_CALIBRATED_PROBABILITY], that
        this player hits at least one HR in the game.
    """
    quality_score = pitcher_score + hitter_score + environment_score
    quality_score = max(0.0, min(MAX_QUALITY_SCORE, quality_score))

    probability = QUALITY_PROBABILITY_INTERCEPT + QUALITY_PROBABILITY_SLOPE * quality_score
    return max(MIN_CALIBRATED_PROBABILITY, min(MAX_CALIBRATED_PROBABILITY, probability))


# ---------------------------------------------------------------------------
# Category 4: Betting Value (15 pts)
# ---------------------------------------------------------------------------

EDGE_PCT_RANGE = (0.0, 15.0)   # percentage points of edge, negative edge scores 0
EV_RANGE = (0.0, 0.30)          # expected profit per $1 staked, negative EV scores 0

BETTING_WEIGHTS = {"edge": 10, "expected_value": 5}


def score_betting_value(
    model_probability: float, implied_probability: float | None = None, odds: int | None = None,
) -> dict:
    """Score betting value out of 15 points.

    Args:
        model_probability: this system's estimated HR probability (0-1).
        implied_probability: sportsbook's implied probability (0-1),
            e.g. from ingestion.odds.HrOddsQuote.current_implied_probability.
            None if no market odds could be found for this player (e.g.
            a name-matching miss against the odds feed) -- see note below.
        odds: the American odds this implied_probability came from. None
            has the same meaning as implied_probability=None.

    Returns:
        {
            "score": float,  # 0-15
            "edge_pct": float | None,          # percentage points, can be negative
            "expected_value": float | None,    # profit per $1 staked, can be negative
            "breakdown": {"edge": {...}, "expected_value": {...}},
        }

    If implied_probability or odds is None, this returns a 0 score with
    "available": False on each sub-metric rather than doing arithmetic on
    missing data -- callers (see models._collect_strengths_and_risks)
    treat "available": False as "don't flag this as a strength or risk",
    since a missing market price isn't the same thing as a bad price.
    """
    if implied_probability is None or odds is None:
        return {
            "score": 0.0,
            "edge_pct": None,
            "expected_value": None,
            "breakdown": {
                "edge": {
                    "raw": None, "points": 0, "max_points": BETTING_WEIGHTS["edge"],
                    "available": False, "note": "no market odds found for this player",
                },
                "expected_value": {
                    "raw": None, "points": 0, "max_points": BETTING_WEIGHTS["expected_value"],
                    "available": False, "note": "no market odds found for this player",
                },
            },
        }

    edge_pct = (model_probability - implied_probability) * 100

    profit_multiple = american_odds_to_profit_multiple(odds)
    expected_value = (
        model_probability * profit_multiple - (1 - model_probability) * 1
    )

    edge_points = normalize(edge_pct, *EDGE_PCT_RANGE) * BETTING_WEIGHTS["edge"]
    ev_points = normalize(expected_value, *EV_RANGE) * BETTING_WEIGHTS["expected_value"]

    breakdown = {
        "edge": {"raw": round(edge_pct, 2), "points": round(edge_points, 2), "max_points": BETTING_WEIGHTS["edge"]},
        "expected_value": {
            "raw": round(expected_value, 3), "points": round(ev_points, 2),
            "max_points": BETTING_WEIGHTS["expected_value"],
        },
    }

    total = round(edge_points + ev_points, 2)
    return {
        "score": total,
        "edge_pct": round(edge_pct, 2),
        "expected_value": round(expected_value, 3),
        "breakdown": breakdown,
    }


# ---------------------------------------------------------------------------
# Combine everything
# ---------------------------------------------------------------------------

STRENGTH_THRESHOLD = 0.7  # >=70% of a sub-metric's max points -> call it a strength
RISK_THRESHOLD = 0.2      # <=20% of a sub-metric's max points -> call it a risk

# Human-readable labels for strengths/risks, keyed by breakdown sub-metric name.
_METRIC_LABELS = {
    "hr9": "Pitcher's HR/9 rate",
    "barrel_pct_allowed": "Pitcher's barrel rate allowed",
    "hard_hit_pct_allowed": "Pitcher's hard-hit rate allowed",
    "fly_ball_pct": "Pitcher's fly-ball rate",
    "barrel_pct": "Hitter's barrel rate",
    "hard_hit_pct": "Hitter's hard-hit rate",
    "iso": "Hitter's ISO (power)",
    "xslg": "Hitter's xSLG",
    "exit_velocity_avg": "Hitter's exit velocity",
    "park_hr_factor": "Park HR factor",
    "wind": "Wind conditions",
    "temperature": "Temperature",
    "weather": "Humidity",
    "game_total": "Vegas game total",
    "edge": "Betting edge vs. sportsbook",
    "expected_value": "Expected value",
}


def _collect_strengths_and_risks(*breakdowns: dict) -> tuple[list[str], list[str]]:
    strengths, risks = [], []
    for breakdown in breakdowns:
        for key, entry in breakdown.items():
            max_points = entry.get("max_points", 0)
            if not max_points:
                continue  # not-implemented sub-metrics (0 max_points) are never strengths/risks
            if entry.get("available") is False:
                continue  # missing data (e.g. no market odds found) isn't a strength or a risk
            fraction = entry["points"] / max_points
            label = _METRIC_LABELS.get(key, key)
            if fraction >= STRENGTH_THRESHOLD:
                strengths.append(label)
            elif fraction <= RISK_THRESHOLD:
                risks.append(label)
    return strengths, risks


def calculate_alpha_score(
    player_name: str,
    pitcher_stats: dict,
    hitter_stats: dict,
    environment: dict,
    implied_probability: float | None = None,
    odds: int | None = None,
    book: str = "unknown",
    opposing_pitcher_hand: str | None = None,
    projected_pa: float = 4.2,
    person_id: int | None = None,
) -> dict:
    """Run all four category scorers and return the full transparent
    HR Alpha Score result for one player.

    Args:
        player_name: display name.
        pitcher_stats: see models.pitcher_model.score_pitcher_vulnerability.
        hitter_stats: see models.hitter_model.score_hitter_power.
        environment: see score_environment above.
        implied_probability: sportsbook implied probability (0-1), e.g.
            from an ingestion.odds.HrOddsQuote's .current_implied_probability.
            Pass None if no market odds could be matched to this player
            (e.g. a name-matching miss) -- betting value then scores 0/15
            with "available": False rather than crashing or faking a number.
        odds: the American odds implied_probability came from. None has
            the same meaning as implied_probability=None.
        book: which sportsbook the odds came from (for display only).
        opposing_pitcher_hand: passed through to score_hitter_power
            (currently a documented no-op -- see that module).
        projected_pa: passed through to estimate_model_probability.
        person_id: MLB Advanced Media player ID, if known. Not used in
            scoring -- carried through purely so callers (e.g.
            reports.daily_report, database.repository) can reliably
            match this result to real game results later without
            relying on name-matching.

    Returns:
        {
            "player": str,
            "alpha_score": float,  # 0-100
            "odds": int | None,
            "book": str,
            "implied_probability": float | None,
            "model_probability": float,   # heuristic, see module docstring
            "edge_pct": float | None,
            "expected_value": float | None,
            "category_breakdown": {
                "pitcher_vulnerability": {...},  # full score_pitcher_vulnerability() output
                "hitter_power": {...},
                "environment": {...},
                "betting_value": {...},
            },
            "strengths": [str, ...],
            "risks": [str, ...],
        }
    """
    pitcher_result = score_pitcher_vulnerability(pitcher_stats)
    hitter_result = score_hitter_power(hitter_stats, opposing_pitcher_hand)
    environment_result = score_environment(environment)

    model_probability = estimate_model_probability(
        pitcher_result["score"], hitter_result["score"], environment_result["score"], projected_pa,
    )

    betting_result = score_betting_value(model_probability, implied_probability, odds)

    alpha_score = round(
        pitcher_result["score"] + hitter_result["score"]
        + environment_result["score"] + betting_result["score"], 2,
    )

    strengths, risks = _collect_strengths_and_risks(
        pitcher_result["breakdown"], hitter_result["breakdown"],
        environment_result["breakdown"], betting_result["breakdown"],
    )

    return {
        "player": player_name,
        "person_id": person_id,
        "alpha_score": alpha_score,
        "odds": odds,
        "book": book,
        "implied_probability": round(implied_probability, 4) if implied_probability is not None else None,
        "model_probability": round(model_probability, 4),
        "edge_pct": betting_result["edge_pct"],
        "expected_value": betting_result["expected_value"],
        "category_breakdown": {
            "pitcher_vulnerability": pitcher_result,
            "hitter_power": hitter_result,
            "environment": environment_result,
            "betting_value": betting_result,
        },
        "strengths": strengths,
        "risks": risks,
    }


# DONE (Phase 5 validation, per the project spec): backtested against
# 8,189 real (quality_score, actual HR-or-not) pairs -- the old 0.4x-2.2x
# heuristic multiplier DID correlate directionally with real outcomes but
# was systematically overconfident at every tier. Replaced with a
# calibrated linear fit -- see the comment block above
# estimate_model_probability() for the full methodology and coefficients.
#
# TODO (next calibration pass): this v1 fit is linear and was trained on
# quality scores that topped out at 61.3 (theoretical max is 85) -- as
# more real dates accumulate, especially at the high end of the score
# range, refit and consider whether a logistic fit or a piecewise/binned
# lookup (matching this project's own percentile-based approach in
# models/calibration.py) tracks the real relationship better than a
# straight line. Also worth periodically re-running the decile breakdown
# above to check whether the fit still holds as the season progresses.
