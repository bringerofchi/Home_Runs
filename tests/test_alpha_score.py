import math

from models.alpha_score import (
    score_environment,
    estimate_model_probability,
    score_betting_value,
    calculate_alpha_score,
    ENV_WEIGHTS,
    BETTING_WEIGHTS,
    QUALITY_PROBABILITY_INTERCEPT,
    QUALITY_PROBABILITY_SLOPE,
    MIN_CALIBRATED_PROBABILITY,
    MAX_CALIBRATED_PROBABILITY,
)


# ---------------------------------------------------------------------------
# score_environment
# ---------------------------------------------------------------------------

def test_environment_missing_data_scores_zero():
    result = score_environment({})
    assert result["score"] == 0.0


def test_environment_best_case_hits_max():
    game = {
        "park_hr_factor": 130.0,   # range top 115
        "wind_out_mph": 25.0,       # range top 20
        "temperature": 100.0,       # range top 95
        "humidity_pct": 95.0,       # range top 90
        "game_total": 12.0,         # range top 11
    }
    result = score_environment(game)
    expected_max = sum(ENV_WEIGHTS.values())
    assert math.isclose(result["score"], expected_max, rel_tol=1e-6)


def test_environment_breakdown_max_points_sum_to_25():
    result = score_environment({})
    total_max = sum(v["max_points"] for v in result["breakdown"].values())
    assert total_max == 25


# ---------------------------------------------------------------------------
# estimate_model_probability
# ---------------------------------------------------------------------------

def test_model_probability_increases_with_quality():
    weak = estimate_model_probability(pitcher_score=0, hitter_score=0, environment_score=0)
    strong = estimate_model_probability(pitcher_score=35, hitter_score=25, environment_score=25)
    assert 0 <= weak < strong < 1


def test_model_probability_within_sane_bounds():
    prob = estimate_model_probability(pitcher_score=35, hitter_score=25, environment_score=25)
    assert 0 < prob < 1


def test_model_probability_matches_calibrated_linear_fit():
    # Phase 5 calibration: probability = intercept + slope * quality_score,
    # where quality_score = pitcher + hitter + environment (0-85 scale).
    prob = estimate_model_probability(pitcher_score=20, hitter_score=15, environment_score=10)
    quality_score = 20 + 15 + 10
    expected = QUALITY_PROBABILITY_INTERCEPT + QUALITY_PROBABILITY_SLOPE * quality_score
    assert math.isclose(prob, expected, rel_tol=1e-9)


def test_model_probability_zero_quality_returns_intercept():
    prob = estimate_model_probability(pitcher_score=0, hitter_score=0, environment_score=0)
    assert math.isclose(prob, QUALITY_PROBABILITY_INTERCEPT, rel_tol=1e-9)


def test_model_probability_respects_calibrated_bounds():
    # Even at the theoretical max (85), the calibrated fit itself stays
    # in-bounds, but this confirms the clamp is doing its job too.
    prob = estimate_model_probability(pitcher_score=35, hitter_score=25, environment_score=25)
    assert MIN_CALIBRATED_PROBABILITY <= prob <= MAX_CALIBRATED_PROBABILITY


def test_model_probability_projected_pa_is_now_a_documented_no_op():
    # projected_pa no longer affects the result -- the calibration is
    # fit directly against real per-game outcomes, so the old per-PA
    # compounding step is gone. This pins that behavior explicitly so a
    # future change doesn't silently reintroduce PA-sensitivity without
    # updating this test and the docstring together.
    a = estimate_model_probability(pitcher_score=20, hitter_score=15, environment_score=10, projected_pa=3.0)
    b = estimate_model_probability(pitcher_score=20, hitter_score=15, environment_score=10, projected_pa=5.5)
    assert a == b


# ---------------------------------------------------------------------------
# score_betting_value
# ---------------------------------------------------------------------------

def test_positive_edge_scores_points():
    # Model thinks 35%, book implies 25% (+300) -- real positive edge.
    result = score_betting_value(model_probability=0.35, implied_probability=0.25, odds=300)
    assert result["edge_pct"] > 0
    assert result["score"] > 0


def test_negative_edge_scores_zero():
    # Model thinks 15%, book implies 25% -- no edge, shouldn't be rewarded.
    result = score_betting_value(model_probability=0.15, implied_probability=0.25, odds=300)
    assert result["edge_pct"] < 0
    assert result["score"] == 0.0


def test_betting_value_never_exceeds_15():
    result = score_betting_value(model_probability=0.99, implied_probability=0.01, odds=1000)
    assert result["score"] <= sum(BETTING_WEIGHTS.values())


# ---------------------------------------------------------------------------
# calculate_alpha_score (full pipeline)
# ---------------------------------------------------------------------------

def test_calculate_alpha_score_full_pipeline():
    pitcher_stats = {
        "hr9": 1.8, "barrel_pct_allowed": 10.0,
        "hard_hit_pct_allowed": 42.0, "fly_ball_pct": 45.0,
    }
    hitter_stats = {
        "barrel_pct": 16.0, "hard_hit_pct": 48.0,
        "iso": 0.260, "xslg": 0.560, "exit_velocity_avg": 93.0,
    }
    environment = {"park_hr_factor": 110.0, "wind_out_mph": 12.0, "temperature": 85.0, "game_total": 9.5}

    result = calculate_alpha_score(
        player_name="Test Player",
        pitcher_stats=pitcher_stats,
        hitter_stats=hitter_stats,
        environment=environment,
        implied_probability=0.238,  # roughly +320
        odds=320,
        book="fanduel",
    )

    assert result["player"] == "Test Player"
    assert 0 <= result["alpha_score"] <= 100
    assert 0 < result["model_probability"] < 1
    assert result["odds"] == 320
    assert result["book"] == "fanduel"
    # This scenario is designed to be a strong matchup -- expect at
    # least some strengths surfaced.
    assert len(result["strengths"]) > 0
    # category_breakdown should carry all four category results
    assert set(result["category_breakdown"].keys()) == {
        "pitcher_vulnerability", "hitter_power", "environment", "betting_value",
    }


def test_calculate_alpha_score_weak_matchup_has_risks():
    result = calculate_alpha_score(
        player_name="Weak Matchup Player",
        pitcher_stats={},
        hitter_stats={},
        environment={},
        implied_probability=0.30,
        odds=-150,
        book="fanduel",
    )
    # Everything is missing/weak -- alpha score should be very low and
    # risks should be flagged for the weak sub-metrics that DO have
    # max_points (i.e. everything except the not-implemented ones).
    assert result["alpha_score"] < 10
    assert len(result["risks"]) > 0


def test_calculate_alpha_score_handles_missing_odds_gracefully():
    # No implied_probability/odds passed -- should not crash, betting
    # value should score 0 without being flagged as a strength or risk.
    result = calculate_alpha_score(
        player_name="No Odds Player",
        pitcher_stats={"hr9": 1.8, "barrel_pct_allowed": 10.0, "hard_hit_pct_allowed": 42.0, "fly_ball_pct": 45.0},
        hitter_stats={"barrel_pct": 16.0, "hard_hit_pct": 48.0, "iso": 0.260, "xslg": 0.560, "exit_velocity_avg": 93.0},
        environment={"park_hr_factor": 110.0, "wind_out_mph": 12.0, "temperature": 85.0, "humidity_pct": 60.0},
    )
    assert result["implied_probability"] is None
    assert result["edge_pct"] is None
    assert result["odds"] is None
    assert result["category_breakdown"]["betting_value"]["score"] == 0.0
    assert "Betting edge vs. sportsbook" not in result["strengths"]
    assert "Betting edge vs. sportsbook" not in result["risks"]
    assert "Expected value" not in result["risks"]
