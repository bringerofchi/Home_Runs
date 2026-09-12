import math

from models.pitcher_model import score_pitcher_vulnerability, WEIGHTS as PITCHER_WEIGHTS


def test_worst_case_pitcher_scores_max_points():
    # At/beyond the "most vulnerable" end of every benchmark range.
    pitcher = {
        "hr9": 3.0,               # range top is 2.0
        "barrel_pct_allowed": 15.0,  # range top is 12.0
        "hard_hit_pct_allowed": 50.0,  # range top is 45.0
        "fly_ball_pct": 60.0,       # range top is 34.76 (calibrated, see FLY_BALL_PCT_RANGE)
    }
    result = score_pitcher_vulnerability(pitcher)
    expected_max = sum(v for k, v in PITCHER_WEIGHTS.items() if k != "pitch_type_weakness")
    assert math.isclose(result["score"], expected_max, rel_tol=1e-6)


def test_best_case_pitcher_scores_zero():
    pitcher = {
        "hr9": 0.0,
        "barrel_pct_allowed": 0.0,
        "hard_hit_pct_allowed": 0.0,
        "fly_ball_pct": 0.0,
    }
    result = score_pitcher_vulnerability(pitcher)
    assert result["score"] == 0.0


def test_missing_data_scores_zero_not_penalized():
    result = score_pitcher_vulnerability({})
    assert result["score"] == 0.0
    assert result["breakdown"]["hr9"]["raw"] is None
    assert result["breakdown"]["hr9"]["points"] == 0.0


def test_score_never_exceeds_35():
    pitcher = {
        "hr9": 100,
        "barrel_pct_allowed": 100,
        "hard_hit_pct_allowed": 100,
        "fly_ball_pct": 100,
    }
    result = score_pitcher_vulnerability(pitcher)
    assert result["score"] <= 35


def test_breakdown_max_points_sum_to_35():
    result = score_pitcher_vulnerability({})
    total_max = sum(v["max_points"] for v in result["breakdown"].values())
    assert total_max == 35


def test_median_real_fly_ball_rate_no_longer_collapses_to_zero():
    # Regression test for the confirmed production bug: the old range
    # (30.0, 50.0) put its floor above the real MLB median, so a
    # perfectly average pitcher (26.2% fly-ball rate, the real p50 from
    # 523 real 2026-season pitchers per the calibration audit) scored a
    # hard 0 on this sub-metric. After the fix, a median pitcher should
    # land roughly in the middle of the point range, not at the floor.
    from models.pitcher_model import FLY_BALL_PCT_RANGE, WEIGHTS

    result = score_pitcher_vulnerability({"fly_ball_pct": 26.2})
    fly_ball_points = result["breakdown"]["fly_ball_pct"]["points"]

    assert FLY_BALL_PCT_RANGE == (19.0, 34.76)  # locks in the calibrated range
    assert fly_ball_points > 0  # the old bug: this would have been exactly 0
    # Roughly mid-range -- (26.2 - 19.0) / (34.76 - 19.0) ~= 0.457 of max.
    # abs_tol accounts for the source code's intentional round(x, 2).
    expected_fraction = (26.2 - 19.0) / (34.76 - 19.0)
    assert math.isclose(fly_ball_points, expected_fraction * WEIGHTS["fly_ball_pct"], abs_tol=0.01)
