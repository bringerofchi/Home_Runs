import datetime
import math

from ingestion.mlb_data import (
    fetch_games,
    fetch_starting_lineup,
    fetch_game_hr_results,
    fetch_pitching_stats,
    fetch_hitting_stats,
    fetch_league_pitching_stats,
    fetch_league_hitting_stats,
    debug_bulk_stats_response,
)


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def test_fetch_games_parses_schedule(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    schedule_payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 999001,
                        "gameDate": "2026-07-08T23:05:00Z",
                        "status": {"detailedState": "Scheduled"},
                        "venue": {"id": 3313, "name": "Yankee Stadium"},
                        "teams": {
                            "home": {
                                "team": {"id": 147, "name": "New York Yankees"},
                                "probablePitcher": {"id": 111, "fullName": "Home Pitcher"},
                            },
                            "away": {
                                "team": {"id": 139, "name": "Tampa Bay Rays"},
                                "probablePitcher": {"id": 222, "fullName": "Away Pitcher"},
                            },
                        },
                    }
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        assert url.endswith("/schedule")
        return FakeResponse(schedule_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    games = fetch_games(datetime.date(2026, 7, 8))
    assert len(games) == 1
    game = games[0]
    assert game["game_id"] == 999001
    assert game["home_team"] == "New York Yankees"
    assert game["away_team"] == "Tampa Bay Rays"
    assert game["venue"] == "Yankee Stadium"
    assert game["home_probable_pitcher"] == {"id": 111, "name": "Home Pitcher"}
    assert game["away_probable_pitcher"] == {"id": 222, "name": "Away Pitcher"}


def test_fetch_pitching_stats_derives_hr9_and_fip(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    stats_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "stat": {
                            "era": "3.50",
                            "whip": "1.15",
                            "inningsPitched": "180.0",
                            "battersFaced": 720,
                            "strikeOuts": 200,
                            "baseOnBalls": 50,
                            "homeRuns": 20,
                            "hitByPitch": 5,
                        }
                    }
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(stats_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    stats = fetch_pitching_stats(person_id=123, season=2026)
    assert stats["has_stats"] is True
    assert math.isclose(stats["hr9"], 20 / 180.0 * 9, rel_tol=1e-6)
    assert math.isclose(stats["k_pct"], 200 / 720, rel_tol=1e-6)
    assert math.isclose(stats["bb_pct"], 50 / 720, rel_tol=1e-6)
    # FIP = ((13*HR)+(3*(BB+HBP))-(2*K))/IP + constant
    expected_fip = ((13 * 20) + (3 * (50 + 5)) - (2 * 200)) / 180.0 + 3.20
    assert math.isclose(stats["fip"], expected_fip, rel_tol=1e-6)
    assert stats["xfip"] is None  # documented TODO


def test_fetch_pitching_stats_no_data_returns_has_stats_false(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    def fake_get(url, params=None, timeout=None):
        return FakeResponse({"stats": [{"splits": []}]})

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    stats = fetch_pitching_stats(person_id=999, season=2026)
    assert stats["has_stats"] is False


def test_fetch_hitting_stats_derives_iso(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    stats_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "stat": {
                            "avg": ".280",
                            "obp": ".360",
                            "slg": ".520",
                            "homeRuns": 30,
                            "atBats": 550,
                            "plateAppearances": 620,
                            "strikeOuts": 140,
                            "baseOnBalls": 60,
                        }
                    }
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(stats_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    stats = fetch_hitting_stats(person_id=456, season=2026)
    assert stats["has_stats"] is True
    assert math.isclose(stats["iso"], 0.520 - 0.280, abs_tol=1e-6)
    assert math.isclose(stats["k_pct"], 140 / 620, rel_tol=1e-6)
    assert math.isclose(stats["bb_pct"], 60 / 620, rel_tol=1e-6)


def test_fetch_starting_lineup_parses_confirmed_lineup(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    boxscore_payload = {
        "teams": {
            "home": {
                "team": {"name": "New York Yankees"},
                "players": {
                    "ID111": {
                        "person": {"id": 111, "fullName": "Leadoff Hitter"},
                        "position": {"abbreviation": "CF"},
                        "battingOrder": "100",
                    },
                    "ID222": {
                        "person": {"id": 222, "fullName": "Cleanup Hitter"},
                        "position": {"abbreviation": "1B"},
                        "battingOrder": "400",
                    },
                    "ID333": {
                        "person": {"id": 333, "fullName": "Bench Guy"},
                        "position": {"abbreviation": "IF"},
                        # no battingOrder -- not a starter
                    },
                    "ID444": {
                        "person": {"id": 444, "fullName": "Mid-Game Sub"},
                        "position": {"abbreviation": "2B"},
                        "battingOrder": "201",  # substitution, not original starter
                    },
                },
            },
            "away": {
                "team": {"name": "Tampa Bay Rays"},
                "players": {},  # away lineup not posted yet
            },
        }
    }

    def fake_get(url, params=None, timeout=None):
        assert "/boxscore" in url
        return FakeResponse(boxscore_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    lineup = fetch_starting_lineup(game_id=999001)
    home = next(side for side in lineup if side["side"] == "home")
    away = next(side for side in lineup if side["side"] == "away")

    assert [b["batting_order"] for b in home["batters"]] == [1, 4]
    assert home["batters"][0]["name"] == "Leadoff Hitter"
    assert away["batters"] == []  # not posted yet -- legitimate empty state, not an error


def test_fetch_game_hr_results_parses_boxscore(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    boxscore_payload = {
        "teams": {
            "home": {
                "players": {
                    "ID111": {
                        "person": {"id": 111, "fullName": "Two Homer Guy"},
                        "stats": {"batting": {"homeRuns": 2, "atBats": 4}},
                    },
                    "ID222": {
                        "person": {"id": 222, "fullName": "No Homer Guy"},
                        "stats": {"batting": {"homeRuns": 0, "atBats": 3}},
                    },
                    "ID333": {
                        "person": {"id": 333, "fullName": "Relief Pitcher"},
                        "stats": {"pitching": {"strikeOuts": 2}},  # no batting stat line at all
                    },
                },
            },
            "away": {
                "players": {
                    "ID444": {
                        "person": {"id": 444, "fullName": "Away Homer Guy"},
                        "stats": {"batting": {"homeRuns": 1, "atBats": 4}},
                    },
                },
            },
        }
    }

    def fake_get(url, params=None, timeout=None):
        assert "/boxscore" in url
        return FakeResponse(boxscore_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    results = fetch_game_hr_results(game_id=999001)

    assert results["111"]["home_runs"] == 2
    assert results["222"]["home_runs"] == 0
    assert "333" not in results  # no batting appearance -- correctly excluded, not defaulted to 0
    assert results["444"]["home_runs"] == 1


def test_fetch_league_pitching_stats_bulk_pull(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    bulk_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "player": {"id": 111, "fullName": "Pitcher One"},
                        "stat": {
                            "inningsPitched": "180.0", "battersFaced": 720,
                            "strikeOuts": 200, "baseOnBalls": 50, "homeRuns": 20, "hitByPitch": 5,
                        },
                    },
                    {
                        "player": {"id": 222, "fullName": "Pitcher Zero Innings"},
                        "stat": {"inningsPitched": "0.0", "battersFaced": 0},
                    },
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        assert url.endswith("/stats")
        assert "personId" not in (params or {})  # bulk pull, no single-player filter
        assert params["playerPool"] == "ALL"  # regression: the confirmed qualified-filter fix
        return FakeResponse(bulk_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    results = fetch_league_pitching_stats(2026)
    assert len(results) == 1  # zero-inning pitcher correctly excluded
    assert results[0]["person_id"] == 111
    assert math.isclose(results[0]["hr9"], 20 / 180.0 * 9, rel_tol=1e-6)


def test_fetch_league_hitting_stats_bulk_pull(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    bulk_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "player": {"id": 456, "fullName": "Hitter One"},
                        "stat": {
                            "avg": ".280", "slg": ".520", "plateAppearances": 620,
                            "strikeOuts": 140, "baseOnBalls": 60,
                        },
                    },
                    {
                        "player": {"id": 789, "fullName": "Hitter No PA"},
                        "stat": {"plateAppearances": 0},
                    },
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        assert params["playerPool"] == "ALL"  # regression: the confirmed qualified-filter fix
        return FakeResponse(bulk_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    results = fetch_league_hitting_stats(2026)
    assert len(results) == 1  # zero-PA hitter correctly excluded
    assert results[0]["person_id"] == 456
    assert math.isclose(results[0]["iso"], 0.520 - 0.280, abs_tol=1e-6)


def test_fetch_league_pitching_stats_respects_min_innings_pitched(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    bulk_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "player": {"id": 111, "fullName": "Real Starter"},
                        "stat": {
                            "inningsPitched": "180.0", "battersFaced": 720,
                            "strikeOuts": 200, "baseOnBalls": 50, "homeRuns": 20, "hitByPitch": 5,
                        },
                    },
                    {
                        "player": {"id": 222, "fullName": "One Inning Call-Up"},
                        "stat": {
                            "inningsPitched": "1.0", "battersFaced": 4,
                            "strikeOuts": 1, "baseOnBalls": 0, "homeRuns": 0, "hitByPitch": 0,
                        },
                    },
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(bulk_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    # Default (0) keeps both.
    results_unfiltered = fetch_league_pitching_stats(2026)
    assert len(results_unfiltered) == 2

    # With a min_innings_pitched threshold, the 1-inning call-up should
    # be excluded, the real starter kept -- this is the exact mechanism
    # for testing whether small-sample outliers explain a high crush rate.
    results_filtered = fetch_league_pitching_stats(2026, min_innings_pitched=20.0)
    assert len(results_filtered) == 1
    assert results_filtered[0]["person_id"] == 111


def test_fetch_league_hitting_stats_respects_min_plate_appearances(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    bulk_payload = {
        "stats": [
            {
                "splits": [
                    {
                        "player": {"id": 456, "fullName": "Everyday Player"},
                        "stat": {
                            "avg": ".280", "slg": ".520", "plateAppearances": 620,
                            "strikeOuts": 140, "baseOnBalls": 60,
                        },
                    },
                    {
                        "player": {"id": 789, "fullName": "Pinch Hitter"},
                        "stat": {
                            "avg": "1.000", "slg": "4.000", "plateAppearances": 3,
                            "strikeOuts": 0, "baseOnBalls": 0,
                        },
                    },
                ]
            }
        ]
    }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(bulk_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    results_unfiltered = fetch_league_hitting_stats(2026)
    assert len(results_unfiltered) == 2

    results_filtered = fetch_league_hitting_stats(2026, min_plate_appearances=50)
    assert len(results_filtered) == 1
    assert results_filtered[0]["person_id"] == 456


def test_debug_bulk_stats_response_distinguishes_api_vs_filter_drop(monkeypatch):
    # Scenario: API returns 5 rows, but 2 have zero plate appearances --
    # this should clearly separate "what the API sent" (5) from "what
    # our filter would keep" (3), rather than conflating the two.
    import ingestion.mlb_data as mlb_data_mod

    bulk_payload = {
        "stats": [
            {
                "splits": [
                    {"player": {"id": 1, "fullName": "A"}, "stat": {"plateAppearances": 500}},
                    {"player": {"id": 2, "fullName": "B"}, "stat": {"plateAppearances": 300}},
                    {"player": {"id": 3, "fullName": "C"}, "stat": {"plateAppearances": 0}},
                    {"player": {"id": 4, "fullName": "D"}, "stat": {}},  # missing field entirely
                    {"player": {"id": 5, "fullName": "E"}, "stat": {"plateAppearances": 150}},
                ]
            }
        ],
        "totalItems": 5,  # simulated pagination-style metadata field
    }

    def fake_get(url, params=None, timeout=None):
        assert url.endswith("/stats")
        return FakeResponse(bulk_payload)

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    result = debug_bulk_stats_response("hitting", 2026)

    assert result["splits_count_raw"] == 5  # what the API actually sent
    assert result["splits_with_positive_pa_or_ip"] == 3  # what our filter keeps
    assert result["splits_with_zero_or_missing_pa_or_ip"] == 2
    assert "totalItems" in result["other_top_level_fields"]  # surfaces unread metadata
    assert len(result["sample_splits"]) == 5  # unfiltered raw sample


def test_debug_bulk_stats_response_handles_empty_stats_array(monkeypatch):
    import ingestion.mlb_data as mlb_data_mod

    def fake_get(url, params=None, timeout=None):
        return FakeResponse({"stats": []})

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    result = debug_bulk_stats_response("pitching", 2026)
    assert result["splits_count_raw"] == 0
    assert result["stats_array_length"] == 0


def test_debug_bulk_stats_response_applies_extra_params(monkeypatch):
    # Confirms extra_params overrides/adds to the request -- this is how
    # a hypothesis like {"playerPool": "ALL"} gets tested without any
    # code change, per the real investigation of the 149-row ISO gap.
    import ingestion.mlb_data as mlb_data_mod

    captured_params = {}

    def fake_get(url, params=None, timeout=None):
        captured_params.update(params)
        return FakeResponse({"stats": [{"splits": []}]})

    monkeypatch.setattr(mlb_data_mod.requests, "get", fake_get)

    debug_bulk_stats_response("hitting", 2026, extra_params={"playerPool": "ALL"})
    assert captured_params["playerPool"] == "ALL"
    assert captured_params["group"] == "hitting"  # base params still present
