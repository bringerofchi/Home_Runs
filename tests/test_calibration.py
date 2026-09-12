import math

from models.calibration import (
    run_calibration_audit, _percentile, _statistical_verdict, _production_impact,
)
from models import pitcher_model, hitter_model


# ---------------------------------------------------------------------------
# _percentile -- pure math, no mocking needed
# ---------------------------------------------------------------------------

def test_percentile_basic_cases():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _percentile(values, 0) == 10.0
    assert _percentile(values, 50) == 30.0
    assert _percentile(values, 100) == 50.0


def test_percentile_empty_list_returns_none():
    assert _percentile([], 50) is None


def test_percentile_single_value():
    assert _percentile([42.0], 50) == 42.0


# ---------------------------------------------------------------------------
# run_calibration_audit -- the real audit logic
# ---------------------------------------------------------------------------

def _patch_all_sources(monkeypatch, statcast_pitcher_values, statcast_batter_values, league_pitching_hr9, league_hitting_iso):
    """Patch every data source the audit pulls from, keyed off the real
    module objects the calibration module imports.
    """
    import models.calibration as cal_mod

    def fake_statcast_pitcher(season, min_sample=None):
        return [
            {"player_id": str(i), "player_name": f"Pitcher {i}",
             "barrel_batted_rate": "8.0", "hard_hit_percent": "38.0",
             "flyballs_percent": str(v)}
            for i, v in enumerate(statcast_pitcher_values)
        ]

    def fake_statcast_batter(season, min_sample=None):
        return [
            {"player_id": str(i), "player_name": f"Batter {i}",
             "barrel_batted_rate": "10.0", "hard_hit_percent": "40.0",
             "xslg": "0.450", "exit_velocity_avg": str(v)}
            for i, v in enumerate(statcast_batter_values)
        ]

    def fake_league_pitching(season, limit=1000, min_innings_pitched=0.0):
        return [{"person_id": i, "name": f"P{i}", "hr9": v} for i, v in enumerate(league_pitching_hr9)]

    def fake_league_hitting(season, limit=1000, min_plate_appearances=0):
        return [{"person_id": i, "name": f"H{i}", "iso": v} for i, v in enumerate(league_hitting_iso)]

    monkeypatch.setattr(cal_mod.statcast, "fetch_pitcher_contact_metrics", fake_statcast_pitcher)
    monkeypatch.setattr(cal_mod.statcast, "fetch_batter_power_metrics", fake_statcast_batter)
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_pitching_stats", fake_league_pitching)
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_hitting_stats", fake_league_hitting)


def test_audit_detects_a_clearly_miscalibrated_range(monkeypatch):
    # Uses fabricated values placed far below whatever the live
    # FLY_BALL_PCT_RANGE currently is, so this test stays meaningful
    # even after the range gets recalibrated again in the future --
    # doesn't couple to one specific historical incident's exact numbers.
    clearly_low_values = [5.0, 6.0, 7.0, 8.0, 9.0]

    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=clearly_low_values,
        statcast_batter_values=[90.0, 91.0, 92.0, 93.0, 94.0],
        league_pitching_hr9=[1.0, 1.2, 1.4, 1.6, 1.8],
        league_hitting_iso=[0.150, 0.180, 0.200, 0.220, 0.250],
    )

    result = run_calibration_audit(2026)
    fly_ball = next(m for m in result["metrics"] if m["metric"] == "fly_ball_pct")

    assert "MISCALIBRATED" in fly_ball["verdict"]
    assert fly_ball["sample_count"] == 5
    assert fly_ball["suggested_high"] < fly_ball["current_low"]


def test_audit_confirms_fly_ball_pct_fix_now_reads_as_calibrated(monkeypatch):
    # Regression test for the actual fix: feed the audit a distribution
    # shaped like the real 523-pitcher sample that drove the fix
    # (p10~19, p50~26, p90~35) and confirm it no longer reads as
    # MISCALIBRATED against the current (post-fix) FLY_BALL_PCT_RANGE.
    realistic_post_fix_values = [15.0, 19.0, 22.0, 26.2, 30.0, 32.0, 34.76, 20.0, 24.0, 28.0]

    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=realistic_post_fix_values,
        statcast_batter_values=[90.0, 91.0, 92.0, 93.0, 94.0],
        league_pitching_hr9=[1.0, 1.2, 1.4, 1.6, 1.8],
        league_hitting_iso=[0.150, 0.180, 0.200, 0.220, 0.250],
    )

    result = run_calibration_audit(2026)
    fly_ball = next(m for m in result["metrics"] if m["metric"] == "fly_ball_pct")

    assert "MISCALIBRATED" not in fly_ball["verdict"]


def test_audit_marks_well_calibrated_range_as_reasonable(monkeypatch):
    # exit_velocity_avg values that comfortably bracket the real
    # EXIT_VELOCITY_RANGE (87.0, 95.0).
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[35.0, 36.0, 37.0, 38.0, 39.0],  # inside FLY_BALL_PCT_RANGE this time
        statcast_batter_values=[87.5, 89.0, 90.5, 92.0, 94.0],
        league_pitching_hr9=[1.0, 1.2, 1.4, 1.6, 1.8],
        league_hitting_iso=[0.150, 0.180, 0.200, 0.220, 0.250],
    )

    result = run_calibration_audit(2026)
    exit_velo = next(m for m in result["metrics"] if m["metric"] == "exit_velocity_avg")
    assert "Reasonable" in exit_velo["verdict"]


def test_audit_handles_missing_source_gracefully(monkeypatch):
    import models.calibration as cal_mod

    def failing_source(season, min_sample=None):
        raise ConnectionError("Savant unreachable")

    monkeypatch.setattr(cal_mod.statcast, "fetch_pitcher_contact_metrics", failing_source)
    monkeypatch.setattr(cal_mod.statcast, "fetch_batter_power_metrics", lambda season, min_sample=None: [])
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_pitching_stats", lambda season, limit=1000: [])
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_hitting_stats", lambda season, limit=1000: [])

    result = run_calibration_audit(2026)

    assert any("Savant unreachable" in w for w in result["warnings"])
    fly_ball = next(m for m in result["metrics"] if m["metric"] == "fly_ball_pct")
    assert fly_ball["sample_count"] == 0
    assert "NO DATA" in fly_ball["verdict"]
    # The rest of the audit should still run, not crash entirely.
    assert len(result["metrics"]) == 9


def test_audit_current_range_always_reflects_live_constants(monkeypatch):
    # Guards against the exact failure mode this module was designed to
    # avoid: a copy-pasted range silently drifting out of sync with
    # production. If someone changes HR9_RANGE in pitcher_model.py, this
    # audit must reflect that change automatically.
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[35.0],
        statcast_batter_values=[90.0],
        league_pitching_hr9=[1.5],
        league_hitting_iso=[0.200],
    )

    result = run_calibration_audit(2026)
    hr9 = next(m for m in result["metrics"] if m["metric"] == "hr9")
    assert hr9["current_low"] == pitcher_model.HR9_RANGE[0]
    assert hr9["current_high"] == pitcher_model.HR9_RANGE[1]

    iso = next(m for m in result["metrics"] if m["metric"] == "iso")
    assert iso["current_low"] == hitter_model.ISO_RANGE[0]
    assert iso["current_high"] == hitter_model.ISO_RANGE[1]


# ---------------------------------------------------------------------------
# population labeling -- makes the "different populations" nuance explicit
# in the audit output itself, instead of something that has to be
# remembered separately
# ---------------------------------------------------------------------------

def test_every_metric_has_a_population_description(monkeypatch):
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[35.0],
        statcast_batter_values=[90.0],
        league_pitching_hr9=[1.5],
        league_hitting_iso=[0.200],
    )

    result = run_calibration_audit(2026)
    for m in result["metrics"]:
        assert m["population"], f"{m['metric']} is missing a population description"


def test_bulk_stats_metrics_flagged_as_broader_population(monkeypatch):
    # iso/hr9 come from playerPool=ALL -- should be explicitly labeled as
    # broader than the Statcast-sourced metrics, per the nuance raised
    # after reviewing real p10=0.011 ISO values (near pitcher-level,
    # consistent with an unfiltered population).
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[35.0],
        statcast_batter_values=[90.0],
        league_pitching_hr9=[1.5],
        league_hitting_iso=[0.200],
    )

    result = run_calibration_audit(2026)
    iso = next(m for m in result["metrics"] if m["metric"] == "iso")
    barrel_pct = next(m for m in result["metrics"] if m["metric"] == "barrel_pct")

    assert "playerPool=ALL" in iso["population"]
    assert "broader" in iso["population"].lower()
    assert "min_sample" in barrel_pct["population"]


def test_population_description_reflects_actual_min_sample_used(monkeypatch):
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[35.0],
        statcast_batter_values=[90.0],
        league_pitching_hr9=[1.5],
        league_hitting_iso=[0.200],
    )

    result = run_calibration_audit(2026, statcast_min_sample="25")
    barrel_pct = next(m for m in result["metrics"] if m["metric"] == "barrel_pct")
    assert "min_sample=25" in barrel_pct["population"]


# ---------------------------------------------------------------------------
# Two-dimensional severity classifier -- the actual point of this upgrade:
# catch cases where the statistical read looks mild but production impact
# is severe, exactly the gap that undersold the real fly_ball_pct bug.
# ---------------------------------------------------------------------------

def test_production_impact_reproduces_the_original_fly_ball_pct_severity():
    # This is the actual real-world case that motivated this upgrade: the
    # OLD range (30.0, 50.0) against real fly-ball values (15-28%, all
    # below the floor) statistically only qualified as "POSSIBLY TOO
    # NARROW/HIGH" -- but production impact should independently show
    # this as CRITICAL, matching the real 642-row diagnostic finding of
    # 99%+ collapse.
    real_fly_ball_values = sorted([17.9, 18.9, 15.6, 28.4, 17.8] * 20)  # simulate a larger sample

    old_broken_low, old_broken_high = 30.0, 50.0
    impact = _production_impact(real_fly_ball_values, old_broken_low, old_broken_high)

    assert impact["severity"] == "CRITICAL"
    assert impact["pct_crushed_floor"] > 90.0
    assert impact["pct_crushed_total"] > 90.0


def test_statistical_verdict_can_undersell_severe_production_impact():
    # Documents WHY the two-dimensional split was necessary. Constructed
    # so the statistical check stays in its MILD tier (current_low is
    # above the median but a real right-tail keeps p75 above current_low,
    # so it never reaches "MISCALIBRATED") while the real crush rate is
    # severe: 70% of real values sit below the range and score a hard 0.
    values = sorted([20.0] * 70 + [35.0] * 30)  # 70% below current_low=30, 30% above
    p10, p50, p75 = _percentile(values, 10), _percentile(values, 50), _percentile(values, 75)

    statistical = _statistical_verdict(30.0, 50.0, p10, p50, p75)
    impact = _production_impact(values, 30.0, 50.0)

    assert "MISCALIBRATED" not in statistical  # stays in the milder tier
    assert "POSSIBLY TOO NARROW" in statistical
    assert impact["severity"] == "SEVERE"  # but production impact correctly flags it
    assert impact["pct_crushed_floor"] == 70.0


def test_production_impact_negligible_for_well_calibrated_range():
    values = [90.0, 91.0, 92.0, 93.0, 94.0]  # comfortably inside (87, 95)
    impact = _production_impact(values, 87.0, 95.0)
    assert impact["severity"] == "NEGLIGIBLE"
    assert impact["pct_crushed_total"] == 0.0


def test_production_impact_distinguishes_floor_from_ceiling_crush():
    # All values above the range -- should crush to ceiling, not floor.
    values = [100.0, 110.0, 120.0]
    impact = _production_impact(values, 0.0, 50.0)
    assert impact["pct_crushed_ceiling"] == 100.0
    assert impact["pct_crushed_floor"] == 0.0


def test_production_impact_handles_empty_values():
    impact = _production_impact([], 30.0, 50.0)
    assert impact["severity"] == "NEGLIGIBLE"
    assert impact["pct_crushed_total"] == 0.0


def test_audit_output_includes_both_verdict_dimensions(monkeypatch):
    _patch_all_sources(
        monkeypatch,
        statcast_pitcher_values=[5.0, 6.0, 7.0, 8.0, 9.0],  # far below any reasonable range -> severe
        statcast_batter_values=[90.0, 91.0, 92.0, 93.0, 94.0],
        league_pitching_hr9=[1.0, 1.2, 1.4, 1.6, 1.8],
        league_hitting_iso=[0.150, 0.180, 0.200, 0.220, 0.250],
    )

    result = run_calibration_audit(2026)
    fly_ball = next(m for m in result["metrics"] if m["metric"] == "fly_ball_pct")

    assert "statistical_verdict" in fly_ball
    assert "production_impact_severity" in fly_ball
    assert "pct_crushed_total" in fly_ball
    assert fly_ball["production_impact_severity"] in ("CRITICAL", "SEVERE")
    # Combined verdict string surfaces both dimensions together
    assert "Production impact:" in fly_ball["verdict"]


# ---------------------------------------------------------------------------
# Small-sample filter for hr9/iso -- tests whether extreme small-sample
# outliers (e.g. a 1-inning call-up) in the playerPool=ALL population are
# inflating apparent production-impact crush rates
# ---------------------------------------------------------------------------

def test_min_innings_and_min_pa_thread_through_to_bulk_fetch(monkeypatch):
    import models.calibration as cal_mod

    captured = {}

    def fake_pitching(season, limit=1000, min_innings_pitched=0.0):
        captured["min_ip"] = min_innings_pitched
        return [{"person_id": 1, "name": "P", "hr9": 1.5}]

    def fake_hitting(season, limit=1000, min_plate_appearances=0):
        captured["min_pa"] = min_plate_appearances
        return [{"person_id": 2, "name": "H", "iso": 0.2}]

    monkeypatch.setattr(cal_mod.statcast, "fetch_pitcher_contact_metrics", lambda season, min_sample=None: [])
    monkeypatch.setattr(cal_mod.statcast, "fetch_batter_power_metrics", lambda season, min_sample=None: [])
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_pitching_stats", fake_pitching)
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_hitting_stats", fake_hitting)

    run_calibration_audit(2026, min_innings_pitched=20.0, min_plate_appearances=50)
    assert captured["min_ip"] == 20.0
    assert captured["min_pa"] == 50


def test_filtering_out_small_samples_reduces_crush_rate():
    # Simulates the real hypothesis directly: a population mixing real
    # regulars with extreme small-sample outliers (hr9=0 from 1 IP) shows
    # a high crush rate; filtering those outliers out should reduce it.
    mixed_population = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7] + [0.0] * 6  # 6 of 14 are outlier zeros
    filtered_population = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7]  # outliers excluded

    unfiltered_impact = _production_impact(mixed_population, 0.5, 2.0)
    filtered_impact = _production_impact(filtered_population, 0.5, 2.0)

    assert unfiltered_impact["pct_crushed_total"] > filtered_impact["pct_crushed_total"]
    assert filtered_impact["pct_crushed_total"] == 0.0  # confirms the hypothesis mechanism works as expected


def test_population_description_reflects_filter_when_applied(monkeypatch):
    import models.calibration as cal_mod

    monkeypatch.setattr(cal_mod.statcast, "fetch_pitcher_contact_metrics", lambda season, min_sample=None: [])
    monkeypatch.setattr(cal_mod.statcast, "fetch_batter_power_metrics", lambda season, min_sample=None: [])
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_pitching_stats",
                         lambda season, limit=1000, min_innings_pitched=0.0: [{"person_id": 1, "hr9": 1.5}])
    monkeypatch.setattr(cal_mod.mlb_data, "fetch_league_hitting_stats",
                         lambda season, limit=1000, min_plate_appearances=0: [{"person_id": 2, "iso": 0.2}])

    result = run_calibration_audit(2026, min_innings_pitched=20.0, min_plate_appearances=50)
    hr9 = next(m for m in result["metrics"] if m["metric"] == "hr9")
    iso = next(m for m in result["metrics"] if m["metric"] == "iso")

    assert "20.0 IP" in hr9["population"]
    assert "50 PA" in iso["population"]
