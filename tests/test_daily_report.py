import datetime
import sqlite3

from ingestion.odds import HrOddsQuote
from database.schema import SCHEMA_SQL
from database.repository import save_daily_rankings
from reports.daily_report import (
    build_watch_list, build_final_rankings, auto_record_results,
    _build_environment_dict, _build_totals_lookup, _lookup_game_total,
)


def _mock_game():
    return {
        "date": "2026-07-08",
        "game_id": 999001,
        "start_time": "2026-07-08T23:05:00Z",
        "status": "Scheduled",
        "home_team": "New York Yankees",
        "home_team_id": 147,
        "away_team": "Tampa Bay Rays",
        "away_team_id": 139,
        "venue": "Yankee Stadium",
        "venue_id": 3313,
        "home_probable_pitcher": {"id": 1001, "name": "Home Pitcher"},
        "away_probable_pitcher": {"id": 1002, "name": "Away Pitcher"},
    }


def _mock_lineup():
    return [
        {
            "side": "home",
            "team": "New York Yankees",
            "batters": [
                {"batting_order": 1, "person_id": 2001, "name": "Home Leadoff", "position": "CF"},
                {"batting_order": 4, "person_id": 2002, "name": "Home Cleanup", "position": "1B"},
            ],
        },
        {
            "side": "away",
            "team": "Tampa Bay Rays",
            "batters": [
                {"batting_order": 1, "person_id": 3001, "name": "Away Leadoff", "position": "CF"},
            ],
        },
    ]


def _mock_weather():
    return {
        "venue": "Yankee Stadium", "roof_type": "open_air", "roof_status": "open_air",
        "data_source": "open-meteo", "temperature": 85.0, "humidity_pct": 65.0,
        "wind_speed_mph": 10.0, "wind_direction_deg": 200, "wind_out_mph": 8.0,
        "precipitation_probability_pct": 10,
    }


def _patch_common(monkeypatch):
    import reports.daily_report as report_mod

    monkeypatch.setattr(report_mod.mlb_data, "fetch_games", lambda d: [_mock_game()])
    monkeypatch.setattr(report_mod.mlb_data, "fetch_starting_lineup", lambda gid: _mock_lineup())
    monkeypatch.setattr(report_mod.mlb_data, "fetch_player_info",
                         lambda pid: {"person_id": pid, "name": "P", "bat_side": "R", "pitch_hand": "R"})
    monkeypatch.setattr(report_mod.mlb_data, "fetch_pitching_stats",
                         lambda pid, season: {"has_stats": True, "hr9": 1.5})
    monkeypatch.setattr(report_mod.mlb_data, "fetch_hitting_stats",
                         lambda pid, season: {"has_stats": True, "iso": 0.240})
    monkeypatch.setattr(report_mod.mlb_data, "fetch_team_roster", lambda team_id: [
        {"person_id": 2001, "name": "Home Leadoff", "position": "CF"},
        {"person_id": 2002, "name": "Home Cleanup", "position": "1B"},
        {"person_id": 9999, "name": "Team Pitcher", "position": "P"},  # should be excluded from watch list
    ])
    monkeypatch.setattr(report_mod.statcast, "fetch_batter_power_metrics", lambda season: [
        {"player_id": "2001", "player_name": "Home Leadoff", "barrel_batted_rate": "15.0",
         "hard_hit_percent": "45.0", "xslg": "0.520", "exit_velocity_avg": "91.0"},
    ])
    monkeypatch.setattr(report_mod.statcast, "fetch_pitcher_contact_metrics", lambda season: [
        {"player_id": "1001", "player_name": "Home Pitcher", "barrel_batted_rate": "9.0",
         "hard_hit_percent": "40.0", "flyballs_percent": "42.0"},
        {"player_id": "1002", "player_name": "Away Pitcher", "barrel_batted_rate": "8.0",
         "hard_hit_percent": "38.0", "flyballs_percent": "40.0"},
    ])
    monkeypatch.setattr(report_mod.weather, "get_game_weather", lambda venue, start: _mock_weather())
    return report_mod


# ---------------------------------------------------------------------------
# build_final_rankings
# ---------------------------------------------------------------------------

def test_build_final_rankings_full_pipeline(monkeypatch):
    _patch_common(monkeypatch)

    odds_quotes = [
        HrOddsQuote(player="Home Leadoff", book="fanduel", opening_odds=320, current_odds=300, closing_odds=None),
    ]

    result = build_final_rankings(datetime.date(2026, 7, 8), odds_quotes=odds_quotes)

    assert result["games_found"] == 1
    # 2 home batters + 1 away batter = 3 confirmed starters scored
    assert result["players_scored"] == 3

    players = {r["player"]: r for r in result["rankings"]}
    assert "Home Leadoff" in players
    assert "Home Cleanup" in players
    assert "Away Leadoff" in players

    # Matched odds -- should have real implied_probability/edge
    matched = players["Home Leadoff"]
    assert matched["implied_probability"] is not None
    assert matched["odds"] == 300

    # Unmatched -- should degrade gracefully, not crash
    unmatched = players["Home Cleanup"]
    assert unmatched["implied_probability"] is None
    assert unmatched["odds"] is None
    assert unmatched["category_breakdown"]["betting_value"]["score"] == 0.0

    # matchup metadata attached
    assert "vs" in matched["matchup"]
    assert matched["game_id"] == 999001

    # Sorted descending by alpha_score
    scores = [r["alpha_score"] for r in result["rankings"]]
    assert scores == sorted(scores, reverse=True)


def test_build_final_rankings_without_odds_quotes_still_works(monkeypatch):
    _patch_common(monkeypatch)

    result = build_final_rankings(datetime.date(2026, 7, 8))  # no odds_quotes

    assert result["players_scored"] == 3
    assert all(r["implied_probability"] is None for r in result["rankings"])
    assert any("No odds_quotes supplied" in w for w in result["warnings"])


def test_build_final_rankings_unconfirmed_lineup_side_is_skipped(monkeypatch):
    import reports.daily_report as report_mod
    _patch_common(monkeypatch)

    # Away side has no batters posted yet.
    def lineup_with_unconfirmed_away(gid):
        lineup = _mock_lineup()
        for side in lineup:
            if side["side"] == "away":
                side["batters"] = []
        return lineup

    monkeypatch.setattr(report_mod.mlb_data, "fetch_starting_lineup", lineup_with_unconfirmed_away)

    result = build_final_rankings(datetime.date(2026, 7, 8))
    assert result["players_scored"] == 2  # only home side's 2 confirmed batters
    assert all("Away" not in r["player"] for r in result["rankings"])


def test_build_final_rankings_bad_game_data_produces_warning_not_crash(monkeypatch):
    import reports.daily_report as report_mod
    _patch_common(monkeypatch)

    def failing_lineup(gid):
        raise RuntimeError("boxscore endpoint down")

    monkeypatch.setattr(report_mod.mlb_data, "fetch_starting_lineup", failing_lineup)

    result = build_final_rankings(datetime.date(2026, 7, 8))
    assert result["players_scored"] == 0
    assert any("lineup fetch failed" in w for w in result["warnings"])


def test_weather_unknown_venue_produces_specific_warning(monkeypatch):
    import reports.daily_report as report_mod
    _patch_common(monkeypatch)

    monkeypatch.setattr(report_mod.weather, "get_game_weather", lambda venue, start: {
        "venue": venue, "roof_type": "unknown", "roof_status": "unknown_venue",
        "data_source": "unavailable", "temperature": None, "humidity_pct": None,
        "wind_speed_mph": None, "wind_direction_deg": None, "wind_out_mph": None,
        "precipitation_probability_pct": None,
    })

    result = build_final_rankings(datetime.date(2026, 7, 8))
    assert any("not found in ingestion.ballpark_reference" in w for w in result["warnings"])
    # Scoring should still proceed (environment just comes back empty), not crash.
    assert result["players_scored"] == 3


def test_weather_forecast_unavailable_produces_specific_warning(monkeypatch):
    import reports.daily_report as report_mod
    _patch_common(monkeypatch)

    monkeypatch.setattr(report_mod.weather, "get_game_weather", lambda venue, start: {
        "venue": venue, "roof_type": "open_air", "roof_status": "open_air",
        "data_source": "unavailable", "temperature": None, "humidity_pct": None,
        "wind_speed_mph": None, "wind_direction_deg": None, "wind_out_mph": None,
        "precipitation_probability_pct": None,
    })

    result = build_final_rankings(datetime.date(2026, 7, 8))
    assert any("forecast unavailable" in w for w in result["warnings"])
    assert result["players_scored"] == 3


def test_weather_exception_produces_specific_warning_not_silent(monkeypatch):
    import reports.daily_report as report_mod
    _patch_common(monkeypatch)

    def failing_weather(venue, start):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(report_mod.weather, "get_game_weather", failing_weather)

    result = build_final_rankings(datetime.date(2026, 7, 8))
    assert any("weather fetch raised an exception" in w and "network unreachable" in w
               for w in result["warnings"])
    assert result["players_scored"] == 3  # still doesn't crash the whole run


# ---------------------------------------------------------------------------
# build_watch_list
# ---------------------------------------------------------------------------

def test_build_watch_list_excludes_pitchers_and_sorts(monkeypatch):
    _patch_common(monkeypatch)

    result = build_watch_list(datetime.date(2026, 7, 8))

    names = [w["player"] for w in result["watch_list"]]
    assert "Team Pitcher" not in names  # pitchers excluded from hitter watch list
    assert all(w["status"] == "WATCH" for w in result["watch_list"])

    scores = [w["quality_score"] for w in result["watch_list"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# auto_record_results
# ---------------------------------------------------------------------------

def _in_memory_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _save_test_rankings(conn, date_str="2026-07-08"):
    """Save two players (with person_id, matching _mock_lineup's batters)
    so auto_record_results has something to work against.
    """
    report = {
        "date": date_str,
        "rankings": [
            {
                "player": "Home Leadoff", "person_id": 2001, "alpha_score": 85.0,
                "odds": 300, "book": "fanduel", "implied_probability": 0.25,
                "model_probability": 0.3, "edge_pct": 5.0, "expected_value": 0.1,
                "game_id": 999001, "venue": "Yankee Stadium", "matchup": "Home Leadoff vs Away Pitcher",
                "category_breakdown": {}, "strengths": [], "risks": [],
            },
            {
                "player": "Home Cleanup", "person_id": 2002, "alpha_score": 70.0,
                "odds": 400, "book": "fanduel", "implied_probability": 0.2,
                "model_probability": 0.22, "edge_pct": 2.0, "expected_value": 0.05,
                "game_id": 999001, "venue": "Yankee Stadium", "matchup": "Home Cleanup vs Away Pitcher",
                "category_breakdown": {}, "strengths": [], "risks": [],
            },
        ],
    }
    save_daily_rankings(conn, report)


def test_auto_record_results_records_final_game(monkeypatch):
    import reports.daily_report as report_mod
    conn = _in_memory_conn()
    _save_test_rankings(conn)

    monkeypatch.setattr(report_mod.mlb_data, "fetch_games", lambda d: [
        {"game_id": 999001, "status": "Final"},
    ])
    monkeypatch.setattr(report_mod.mlb_data, "fetch_game_hr_results", lambda gid: {
        "2001": {"name": "Home Leadoff", "home_runs": 1},
        "2002": {"name": "Home Cleanup", "home_runs": 0},
    })

    result = auto_record_results(datetime.date(2026, 7, 8), conn)

    assert result["players_recorded"] == 2
    assert result["players_skipped"] == 0

    row = conn.execute("SELECT * FROM results WHERE player = 'Home Leadoff'").fetchone()
    assert row["hit_hr"] == 1
    assert row["source"] == "auto_boxscore"
    assert row["profit_loss"] == 3.0  # +300 odds, hit

    row2 = conn.execute("SELECT * FROM results WHERE player = 'Home Cleanup'").fetchone()
    assert row2["hit_hr"] == 0
    assert row2["profit_loss"] == -1.0


def test_auto_record_results_skips_non_final_game(monkeypatch):
    import reports.daily_report as report_mod
    conn = _in_memory_conn()
    _save_test_rankings(conn)

    monkeypatch.setattr(report_mod.mlb_data, "fetch_games", lambda d: [
        {"game_id": 999001, "status": "In Progress"},
    ])

    def fail_if_called(gid):
        raise AssertionError("should not fetch box score for a non-final game")
    monkeypatch.setattr(report_mod.mlb_data, "fetch_game_hr_results", fail_if_called)

    result = auto_record_results(datetime.date(2026, 7, 8), conn)

    assert result["players_recorded"] == 0
    assert result["players_skipped"] == 2
    assert any("not final yet" in w for w in result["warnings"])

    # nothing should have been written
    count = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
    assert count == 0


def test_auto_record_results_player_missing_from_boxscore(monkeypatch):
    import reports.daily_report as report_mod
    conn = _in_memory_conn()
    _save_test_rankings(conn)

    monkeypatch.setattr(report_mod.mlb_data, "fetch_games", lambda d: [
        {"game_id": 999001, "status": "Final"},
    ])
    # Box score only has one of the two tracked players.
    monkeypatch.setattr(report_mod.mlb_data, "fetch_game_hr_results", lambda gid: {
        "2001": {"name": "Home Leadoff", "home_runs": 2},
    })

    result = auto_record_results(datetime.date(2026, 7, 8), conn)

    assert result["players_recorded"] == 1
    assert result["players_skipped"] == 1
    assert any("Home Cleanup" in w and "not found in box score" in w for w in result["warnings"])


def test_auto_record_results_no_tracked_players_returns_informative_warning():
    conn = _in_memory_conn()  # nothing saved
    result = auto_record_results(datetime.date(2026, 7, 8), conn)

    assert result["players_recorded"] == 0
    assert len(result["warnings"]) == 1
    assert "No tracked players" in result["warnings"][0]


def test_auto_record_results_does_not_reprocess_already_recorded_players(monkeypatch):
    import reports.daily_report as report_mod
    conn = _in_memory_conn()
    _save_test_rankings(conn)

    monkeypatch.setattr(report_mod.mlb_data, "fetch_games", lambda d: [
        {"game_id": 999001, "status": "Final"},
    ])
    monkeypatch.setattr(report_mod.mlb_data, "fetch_game_hr_results", lambda gid: {
        "2001": {"name": "Home Leadoff", "home_runs": 1},
        "2002": {"name": "Home Cleanup", "home_runs": 0},
    })

    first_run = auto_record_results(datetime.date(2026, 7, 8), conn)
    assert first_run["players_recorded"] == 2

    # Second run -- both players already have results, get_tracked_players_for_date
    # should exclude them, so nothing new happens.
    second_run = auto_record_results(datetime.date(2026, 7, 8), conn)
    assert second_run["players_recorded"] == 0
    assert "No tracked players" in second_run["warnings"][0]


# ---------------------------------------------------------------------------
# game_total wiring
# ---------------------------------------------------------------------------

def test_environment_dict_includes_game_total_on_success(monkeypatch):
    import reports.daily_report as dr_mod
    monkeypatch.setattr(
        dr_mod.weather, "get_game_weather",
        lambda venue, start_time: {"temperature": 80.0, "wind_out_mph": 5.0, "humidity_pct": 50.0},
    )

    warnings = []
    env = _build_environment_dict("Yankee Stadium", "2026-07-08T23:00:00Z", warnings, game_total=9.5)

    assert env["game_total"] == 9.5
    assert env["temperature"] == 80.0
    assert env["park_hr_factor"] is None


def test_environment_dict_keeps_game_total_when_weather_fails(monkeypatch):
    """A weather-fetch failure (a different data source entirely) should
    NOT discard a valid game_total -- this was a real gap: the function
    used to return {} on any weather failure, throwing away independent
    market data along with it.
    """
    import reports.daily_report as dr_mod

    def raise_error(venue, start_time):
        raise RuntimeError("network down")

    monkeypatch.setattr(dr_mod.weather, "get_game_weather", raise_error)

    warnings = []
    env = _build_environment_dict("Yankee Stadium", "2026-07-08T23:00:00Z", warnings, game_total=8.5)

    assert env["game_total"] == 8.5
    assert "temperature" not in env
    assert len(warnings) == 1


def test_environment_dict_keeps_game_total_on_unknown_venue(monkeypatch):
    import reports.daily_report as dr_mod
    monkeypatch.setattr(
        dr_mod.weather, "get_game_weather",
        lambda venue, start_time: {"roof_status": "unknown_venue"},
    )

    warnings = []
    env = _build_environment_dict("Not A Real Park", "2026-07-08T23:00:00Z", warnings, game_total=7.5)

    assert env["game_total"] == 7.5


def test_environment_dict_game_total_none_when_not_supplied():
    warnings = []
    env = _build_environment_dict(None, None, warnings)
    assert env["game_total"] is None


def test_totals_lookup_matches_regardless_of_case_and_whitespace():
    raw = {frozenset({" New York Yankees ", "Tampa Bay Rays"}): 8.5}
    lookup = _build_totals_lookup(raw)

    result = _lookup_game_total(lookup, "new york yankees", "TAMPA BAY RAYS")
    assert result == 8.5


def test_totals_lookup_no_match_returns_none():
    lookup = _build_totals_lookup({frozenset({"Boston Red Sox", "Baltimore Orioles"}): 9.0})
    assert _lookup_game_total(lookup, "Chicago Cubs", "St. Louis Cardinals") is None


def test_totals_lookup_handles_none_input():
    assert _build_totals_lookup(None) == {}
    assert _lookup_game_total({}, None, None) is None


def test_build_final_rankings_uses_game_totals_when_supplied(monkeypatch):
    """End-to-end: a supplied game_totals lookup should flow through to
    each player's environment category score, matched by team names.
    """
    import reports.daily_report as dr_mod

    fake_games = [{
        "date": "2026-07-08", "game_id": 1, "start_time": "2026-07-08T23:00:00Z",
        "status": "Scheduled", "home_team": "New York Yankees", "home_team_id": 1,
        "away_team": "Tampa Bay Rays", "away_team_id": 2, "venue": "Yankee Stadium", "venue_id": 1,
        "home_probable_pitcher": {"id": 100, "name": "Pitcher Home"},
        "away_probable_pitcher": {"id": 200, "name": "Pitcher Away"},
    }]
    fake_lineup = [
        {"side": "home", "team": "New York Yankees",
         "batters": [{"batting_order": 1, "person_id": 10, "name": "Hitter One", "position": "OF"}]},
        {"side": "away", "team": "Tampa Bay Rays", "batters": []},
    ]

    monkeypatch.setattr(dr_mod.mlb_data, "fetch_games", lambda game_date: fake_games)
    monkeypatch.setattr(dr_mod.mlb_data, "fetch_starting_lineup", lambda game_id: fake_lineup)
    monkeypatch.setattr(dr_mod.mlb_data, "fetch_player_info", lambda pid: {"pitch_hand": "R"})
    monkeypatch.setattr(dr_mod.mlb_data, "fetch_pitching_stats", lambda pid, season: {"has_stats": False})
    monkeypatch.setattr(dr_mod.mlb_data, "fetch_hitting_stats", lambda pid, season: {"has_stats": False})
    monkeypatch.setattr(dr_mod.statcast, "fetch_batter_power_metrics", lambda season: [])
    monkeypatch.setattr(dr_mod.statcast, "fetch_pitcher_contact_metrics", lambda season: [])
    monkeypatch.setattr(dr_mod.weather, "get_game_weather", lambda venue, start_time: {"data_source": "unavailable"})

    game_totals = {frozenset({"New York Yankees", "Tampa Bay Rays"}): 10.5}

    result = build_final_rankings(datetime.date(2026, 7, 8), game_totals=game_totals)

    assert result["players_scored"] == 1
    env_breakdown = result["rankings"][0]["category_breakdown"]["environment"]["breakdown"]
    assert env_breakdown["game_total"]["raw"] == 10.5
    assert not any("No game_totals supplied" in w for w in result["warnings"])


def test_build_final_rankings_warns_when_game_totals_not_supplied(monkeypatch):
    import reports.daily_report as dr_mod
    monkeypatch.setattr(dr_mod.mlb_data, "fetch_games", lambda game_date: [])

    result = build_final_rankings(datetime.date(2026, 7, 8))

    assert any("No game_totals supplied" in w for w in result["warnings"])
