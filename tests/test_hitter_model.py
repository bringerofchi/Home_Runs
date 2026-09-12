import math

from models.hitter_model import score_hitter_power, WEIGHTS as HITTER_WEIGHTS


def test_elite_hitter_scores_max_points():
    hitter = {
        "barrel_pct": 20.0,       # range top is 18.0
        "hard_hit_pct": 55.0,     # range top is 50.0
        "iso": 0.320,             # range top is 0.280
        "xslg": 0.650,            # range top is 0.600
        "exit_velocity_avg": 97.0,  # range top is 95.0
    }
    result = score_hitter_power(hitter)
    expected_max = sum(v for k, v in HITTER_WEIGHTS.items() if k != "recent_form")
    assert math.isclose(result["score"], expected_max, rel_tol=1e-6)


def test_weak_hitter_scores_zero():
    hitter = {
        "barrel_pct": 0.0,
        "hard_hit_pct": 0.0,
        "iso": 0.0,
        "xslg": 0.0,
        "exit_velocity_avg": 0.0,
    }
    result = score_hitter_power(hitter)
    assert result["score"] == 0.0


def test_missing_data_scores_zero_not_penalized():
    result = score_hitter_power({})
    assert result["score"] == 0.0
    assert result["breakdown"]["barrel_pct"]["raw"] is None


def test_score_never_exceeds_25():
    hitter = {
        "barrel_pct": 1000, "hard_hit_pct": 1000, "iso": 1000,
        "xslg": 1000, "exit_velocity_avg": 1000,
    }
    result = score_hitter_power(hitter)
    assert result["score"] <= 25


def test_breakdown_max_points_sum_to_25():
    result = score_hitter_power({})
    total_max = sum(v["max_points"] for v in result["breakdown"].values())
    assert total_max == 25


def test_opposing_pitcher_hand_accepted_but_documented_as_noop():
    hitter = {"barrel_pct": 10.0}
    result_vs_l = score_hitter_power(hitter, opposing_pitcher_hand="L")
    result_vs_r = score_hitter_power(hitter, opposing_pitcher_hand="R")
    # Platoon splits aren't implemented yet -- score should be identical
    # regardless of handedness until that TODO is picked up.
    assert result_vs_l["score"] == result_vs_r["score"]
