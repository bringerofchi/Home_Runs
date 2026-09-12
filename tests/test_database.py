import sqlite3
import time

from database.schema import SCHEMA_SQL
from database.repository import (
    save_daily_rankings, record_result, get_results_for_date, get_score_tier_summary,
    save_odds_cache, get_cached_odds, get_date_summary, get_category_breakdown_summary,
)
from ingestion.odds import HrOddsQuote


def _in_memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _mock_ranking(player, alpha_score, odds=300, edge_pct=5.0):
    return {
        "player": player, "alpha_score": alpha_score, "odds": odds, "book": "fanduel",
        "implied_probability": 0.25, "model_probability": 0.30, "edge_pct": edge_pct,
        "expected_value": 0.1, "game_id": 999001, "venue": "Yankee Stadium",
        "matchup": f"{player} vs Some Pitcher",
        "category_breakdown": {"pitcher_vulnerability": {"score": 20}},
        "strengths": ["Elite barrel rate"], "risks": [],
    }


# ---------------------------------------------------------------------------
# save_daily_rankings
# ---------------------------------------------------------------------------

def test_save_daily_rankings_writes_rows():
    conn = _in_memory_conn()
    report = {
        "date": "2026-07-08",
        "rankings": [_mock_ranking("Player A", 85.0), _mock_ranking("Player B", 60.0)],
    }
    written = save_daily_rankings(conn, report)
    assert written == 2

    row = conn.execute("SELECT * FROM alpha_scores WHERE player = 'Player A'").fetchone()
    assert row["alpha_score"] == 85.0
    assert row["odds"] == 300


def test_save_daily_rankings_upserts_on_rerun():
    conn = _in_memory_conn()
    report_v1 = {"date": "2026-07-08", "rankings": [_mock_ranking("Player A", 70.0)]}
    report_v2 = {"date": "2026-07-08", "rankings": [_mock_ranking("Player A", 88.0)]}

    save_daily_rankings(conn, report_v1)
    save_daily_rankings(conn, report_v2)

    rows = conn.execute("SELECT * FROM alpha_scores WHERE player = 'Player A'").fetchall()
    assert len(rows) == 1  # not duplicated
    assert rows[0]["alpha_score"] == 88.0  # latest value wins


# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------

def test_record_result_computes_profit_loss_on_win():
    conn = _in_memory_conn()
    record_result(conn, "2026-07-08", "Player A", hit_hr=True, odds_used=300)

    row = conn.execute("SELECT * FROM results WHERE player = 'Player A'").fetchone()
    assert row["hit_hr"] == 1
    assert row["profit_loss"] == 3.0  # +300 odds -> profit multiple 3.0


def test_record_result_computes_profit_loss_on_loss():
    conn = _in_memory_conn()
    record_result(conn, "2026-07-08", "Player A", hit_hr=False, odds_used=300)

    row = conn.execute("SELECT * FROM results WHERE player = 'Player A'").fetchone()
    assert row["hit_hr"] == 0
    assert row["profit_loss"] == -1.0


def test_record_result_computes_closing_line_value():
    conn = _in_memory_conn()
    # Bet at +350 (worse number, lower implied prob), closed at +300
    # (better number, higher implied prob) -- got a worse price than close.
    record_result(conn, "2026-07-08", "Player A", hit_hr=True, odds_used=350, closing_odds=300)

    row = conn.execute("SELECT * FROM results WHERE player = 'Player A'").fetchone()
    assert row["closing_line_value"] is not None
    assert row["closing_line_value"] > 0  # closing implied prob > bet-time implied prob


def test_record_result_without_odds_leaves_profit_loss_null():
    conn = _in_memory_conn()
    record_result(conn, "2026-07-08", "Player A", hit_hr=True)  # no odds_used

    row = conn.execute("SELECT * FROM results WHERE player = 'Player A'").fetchone()
    assert row["profit_loss"] is None


# ---------------------------------------------------------------------------
# get_results_for_date
# ---------------------------------------------------------------------------

def test_get_results_for_date_joins_rankings_with_results():
    conn = _in_memory_conn()
    save_daily_rankings(conn, {"date": "2026-07-08", "rankings": [_mock_ranking("Player A", 85.0)]})
    record_result(conn, "2026-07-08", "Player A", hit_hr=True, odds_used=300)

    results = get_results_for_date(conn, "2026-07-08")
    assert len(results) == 1
    assert results[0]["player"] == "Player A"
    assert results[0]["hit_hr"] == 1
    assert results[0]["alpha_score"] == 85.0


def test_get_results_for_date_includes_unrecorded_rankings():
    conn = _in_memory_conn()
    save_daily_rankings(conn, {"date": "2026-07-08", "rankings": [_mock_ranking("Player A", 85.0)]})
    # No result recorded yet.

    results = get_results_for_date(conn, "2026-07-08")
    assert len(results) == 1
    assert results[0]["hit_hr"] is None  # legitimate "not yet known" state


# ---------------------------------------------------------------------------
# get_score_tier_summary -- the Phase 5 backtesting query
# ---------------------------------------------------------------------------

def test_score_tier_summary_computes_hit_rate_and_roi():
    conn = _in_memory_conn()

    # Two 90+ tier players: one hit, one didn't.
    save_daily_rankings(conn, {"date": "2026-07-08", "rankings": [
        _mock_ranking("High Score Hit", 92.0, odds=300),
        _mock_ranking("High Score Miss", 91.0, odds=250),
    ]})
    record_result(conn, "2026-07-08", "High Score Hit", hit_hr=True, odds_used=300)
    record_result(conn, "2026-07-08", "High Score Miss", hit_hr=False, odds_used=250)

    # One low-tier player, hit.
    save_daily_rankings(conn, {"date": "2026-07-08", "rankings": [
        _mock_ranking("Low Score Hit", 40.0, odds=500),
    ]})
    record_result(conn, "2026-07-08", "Low Score Hit", hit_hr=True, odds_used=500)

    summary = get_score_tier_summary(conn)
    tiers = {t["tier"]: t for t in summary}

    assert tiers["90-100"]["sample_count"] == 2
    assert tiers["90-100"]["hit_rate"] == 0.5  # 1 of 2 hit

    assert tiers["0-59"]["sample_count"] == 1
    assert tiers["0-59"]["hit_rate"] == 1.0

    # Tiers with zero sample size shouldn't crash, just report None/0.
    assert tiers["70-79"]["sample_count"] == 0
    assert tiers["70-79"]["hit_rate"] is None


# ---------------------------------------------------------------------------
# odds_cache -- conserves The Odds API quota
# ---------------------------------------------------------------------------

def _mock_quotes():
    return [
        HrOddsQuote(player="Player A", book="fanduel", opening_odds=300, current_odds=300, closing_odds=None),
        HrOddsQuote(player="Player B", book="fanduel", opening_odds=250, current_odds=250, closing_odds=None),
    ]


def test_get_cached_odds_returns_none_when_nothing_cached():
    conn = _in_memory_conn()
    assert get_cached_odds(conn, "2026-07-08", "fanduel") is None


def test_save_and_get_cached_odds_round_trips():
    conn = _in_memory_conn()
    save_odds_cache(conn, "2026-07-08", "fanduel", _mock_quotes(), quota_remaining=480, quota_used=20)

    cached = get_cached_odds(conn, "2026-07-08", "fanduel")
    assert cached is not None
    assert len(cached["quotes"]) == 2
    assert cached["quotes"][0].player == "Player A"
    assert cached["quotes"][0].current_odds == 300
    assert cached["quota_remaining"] == 480
    assert cached["age_minutes"] < 1  # just saved


def test_cached_odds_respects_max_age():
    conn = _in_memory_conn()
    save_odds_cache(conn, "2026-07-08", "fanduel", _mock_quotes())

    # Manually age the cache entry beyond any reasonable freshness window.
    conn.execute(
        "UPDATE odds_cache SET fetched_at = datetime('now', '-2 hours') WHERE date = ? AND book = ?",
        ("2026-07-08", "fanduel"),
    )
    conn.commit()

    assert get_cached_odds(conn, "2026-07-08", "fanduel", max_age_minutes=30) is None
    # But a caller asking for a wider window should still get it.
    assert get_cached_odds(conn, "2026-07-08", "fanduel", max_age_minutes=180) is not None


def test_cached_odds_is_keyed_by_date_and_book_separately():
    conn = _in_memory_conn()
    save_odds_cache(conn, "2026-07-08", "fanduel", _mock_quotes())

    assert get_cached_odds(conn, "2026-07-08", "draftkings") is None  # different book
    assert get_cached_odds(conn, "2026-07-09", "fanduel") is None      # different date
    assert get_cached_odds(conn, "2026-07-08", "fanduel") is not None  # original still there


def test_save_odds_cache_overwrites_on_rerun():
    conn = _in_memory_conn()
    save_odds_cache(conn, "2026-07-08", "fanduel", _mock_quotes(), quota_remaining=480)
    save_odds_cache(conn, "2026-07-08", "fanduel", _mock_quotes(), quota_remaining=465)

    rows = conn.execute("SELECT * FROM odds_cache WHERE date = ? AND book = ?", ("2026-07-08", "fanduel")).fetchall()
    assert len(rows) == 1  # not duplicated
    assert rows[0]["quota_remaining"] == 465  # latest value wins


# ---------------------------------------------------------------------------
# get_date_summary
# ---------------------------------------------------------------------------

def test_get_date_summary_across_multiple_dates():
    conn = _in_memory_conn()

    save_daily_rankings(conn, {"date": "2026-07-08", "rankings": [
        _mock_ranking("Player A", 85.0), _mock_ranking("Player B", 60.0),
    ]})
    record_result(conn, "2026-07-08", "Player A", hit_hr=True, odds_used=300)
    # Player B has no result recorded yet.

    save_daily_rankings(conn, {"date": "2026-07-09", "rankings": [_mock_ranking("Player C", 90.0)]})
    # No results recorded for this date at all.

    summary = get_date_summary(conn)
    by_date = {row["date"]: row for row in summary}

    assert by_date["2026-07-08"]["players_saved"] == 2
    assert by_date["2026-07-08"]["players_with_results"] == 1
    assert by_date["2026-07-08"]["hrs_hit"] == 1
    assert by_date["2026-07-08"]["top_alpha_score"] == 85.0

    assert by_date["2026-07-09"]["players_saved"] == 1
    assert by_date["2026-07-09"]["players_with_results"] == 0
    assert by_date["2026-07-09"]["hrs_hit"] == 0

    # Most recent date first
    assert summary[0]["date"] == "2026-07-09"


def test_get_date_summary_empty_database_returns_empty_list():
    conn = _in_memory_conn()
    assert get_date_summary(conn) == []


# ---------------------------------------------------------------------------
# get_category_breakdown_summary -- the sub-metric diagnostic
# ---------------------------------------------------------------------------

def _mock_breakdown_row(park_factor_available: bool, game_total_available: bool, odds_available: bool):
    """Mimics the real shape calculate_alpha_score() produces, with the
    specific pattern we're testing: park_hr_factor and game_total are
    currently always unavailable (not ingested anywhere yet).
    """
    return {
        "pitcher_vulnerability": {
            "score": 20.0,
            "breakdown": {
                "hr9": {"raw": 1.5, "points": 7.0, "max_points": 12},
                "barrel_pct_allowed": {"raw": 9.0, "points": 6.0, "max_points": 10},
                "hard_hit_pct_allowed": {"raw": 40.0, "points": 5.0, "max_points": 8},
                "fly_ball_pct": {"raw": 42.0, "points": 2.0, "max_points": 5},
                "pitch_type_weakness": {"raw": None, "points": 0, "max_points": 0, "note": "not implemented"},
            },
        },
        "hitter_power": {
            "score": 15.0,
            "breakdown": {
                "barrel_pct": {"raw": 12.0, "points": 5.0, "max_points": 8},
                "hard_hit_pct": {"raw": 42.0, "points": 4.0, "max_points": 6},
                "iso": {"raw": 0.2, "points": 3.0, "max_points": 5},
                "xslg": {"raw": 0.45, "points": 2.0, "max_points": 4},
                "exit_velocity_avg": {"raw": 90.0, "points": 1.0, "max_points": 2},
                "recent_form": {"raw": None, "points": 0, "max_points": 0, "note": "not implemented"},
            },
        },
        "environment": {
            "score": 10.0,
            "breakdown": {
                "park_hr_factor": (
                    {"raw": 105.0, "points": 6.0, "max_points": 8} if park_factor_available
                    else {"raw": None, "points": 0, "max_points": 8}
                ),
                "wind": {"raw": 8.0, "points": 3.0, "max_points": 6},
                "temperature": {"raw": 80.0, "points": 2.0, "max_points": 4},
                "weather": {"raw": 55.0, "points": 2.0, "max_points": 4},
                "game_total": (
                    {"raw": 9.0, "points": 2.0, "max_points": 3} if game_total_available
                    else {"raw": None, "points": 0, "max_points": 3}
                ),
            },
        },
        "betting_value": {
            "score": 0.0 if not odds_available else 5.0,
            "breakdown": {
                "edge": (
                    {"raw": 3.0, "points": 3.0, "max_points": 10} if odds_available
                    else {"raw": None, "points": 0, "max_points": 10, "available": False}
                ),
                "expected_value": (
                    {"raw": 0.1, "points": 2.0, "max_points": 5} if odds_available
                    else {"raw": None, "points": 0, "max_points": 5, "available": False}
                ),
            },
        },
    }


def test_category_breakdown_confirms_structural_gap_hypothesis():
    conn = _in_memory_conn()

    # Simulate 3 real days' worth of rows -- park_hr_factor and
    # game_total are NEVER available (matches real current ingestion
    # state), everything else usually is.
    for i in range(3):
        report = {
            "date": f"2026-07-{10+i}",
            "rankings": [
                {**_mock_ranking(f"Player {i}", 45.0),
                 "category_breakdown": _mock_breakdown_row(
                     park_factor_available=False, game_total_available=False, odds_available=False,
                 )},
            ],
        }
        save_daily_rankings(conn, report)

    summary = get_category_breakdown_summary(conn)
    assert summary["sample_count"] == 3

    env_by_metric = {s["sub_metric"]: s for s in summary["categories"]["environment"]}
    assert env_by_metric["park_hr_factor"]["pct_rows_with_data"] == 0.0
    assert env_by_metric["park_hr_factor"]["avg_pct_of_max"] == 0.0
    assert env_by_metric["game_total"]["pct_rows_with_data"] == 0.0
    assert env_by_metric["wind"]["pct_rows_with_data"] == 100.0

    betting_by_metric = {s["sub_metric"]: s for s in summary["categories"]["betting_value"]}
    assert betting_by_metric["edge"]["pct_rows_with_data"] == 0.0  # no odds matched any of these rows


def test_category_breakdown_zero_max_points_gives_none_pct():
    conn = _in_memory_conn()
    report = {
        "date": "2026-07-08",
        "rankings": [
            {**_mock_ranking("Player A", 45.0),
             "category_breakdown": _mock_breakdown_row(True, True, True)},
        ],
    }
    save_daily_rankings(conn, report)

    summary = get_category_breakdown_summary(conn)
    pitcher_by_metric = {s["sub_metric"]: s for s in summary["categories"]["pitcher_vulnerability"]}
    assert pitcher_by_metric["pitch_type_weakness"]["max_points"] == 0
    assert pitcher_by_metric["pitch_type_weakness"]["avg_pct_of_max"] is None


def test_category_breakdown_sorts_least_available_first():
    conn = _in_memory_conn()
    report = {
        "date": "2026-07-08",
        "rankings": [
            {**_mock_ranking("Player A", 45.0),
             "category_breakdown": _mock_breakdown_row(False, False, True)},
        ],
    }
    save_daily_rankings(conn, report)

    summary = get_category_breakdown_summary(conn)
    env_list = summary["categories"]["environment"]
    # park_hr_factor and game_total (0% data) should sort before wind/temp/weather (100% data)
    assert env_list[0]["pct_rows_with_data"] == 0.0
    assert env_list[-1]["pct_rows_with_data"] == 100.0


def test_category_breakdown_empty_database():
    conn = _in_memory_conn()
    summary = get_category_breakdown_summary(conn)
    assert summary["sample_count"] == 0
    assert summary["categories"] == {}
