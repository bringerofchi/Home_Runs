from ingestion.statcast import (
    fetch_batter_power_metrics,
    fetch_pitcher_contact_metrics,
    diagnose_batter_leaderboard,
    diagnose_leaderboard,
    DEFAULT_MIN_SAMPLE,
)


class FakeCsvResponse:
    def __init__(self, csv_text, status_code=200, encoding="utf-8"):
        self.content = csv_text.encode(encoding)
        self.status_code = status_code

    def raise_for_status(self):
        pass


def test_fetch_batter_power_metrics_parses_csv(monkeypatch):
    import ingestion.statcast as statcast_mod

    csv_text = (
        "player_id,player_name,barrel_batted_rate,hard_hit_percent,"
        "avg_hyper_speed,exit_velocity_avg,exit_velocity_max,"
        "launch_angle_avg,xslg\n"
        "592450,Aaron Judge,20.1,55.3,102.5,95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        assert params["type"] == "batter"
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    rows = fetch_batter_power_metrics(2026)
    assert len(rows) == 1
    assert rows[0]["player_name"] == "Aaron Judge"
    assert rows[0]["barrel_batted_rate"] == "20.1"


def test_fetch_pitcher_contact_metrics_parses_csv(monkeypatch):
    import ingestion.statcast as statcast_mod

    csv_text = (
        "player_id,player_name,barrel_batted_rate,hard_hit_percent,"
        "groundballs_percent,flyballs_percent,linedrives_percent\n"
        "543037,Sample Pitcher,7.2,38.1,44.0,33.0,23.0\n"
    )

    def fake_get(url, params=None, timeout=None):
        assert params["type"] == "pitcher"
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    rows = fetch_pitcher_contact_metrics(2026)
    assert len(rows) == 1
    assert rows[0]["player_name"] == "Sample Pitcher"
    assert rows[0]["flyballs_percent"] == "33.0"


# ---------------------------------------------------------------------------
# diagnose_leaderboard -- proves the diagnostic actually catches problems
# ---------------------------------------------------------------------------

def test_diagnose_leaderboard_clean_response_has_no_warnings(monkeypatch):
    import ingestion.statcast as statcast_mod

    csv_text = (
        "player_id,player_name,barrel_batted_rate,hard_hit_percent,"
        "avg_hyper_speed,exit_velocity_avg,exit_velocity_max,"
        "launch_angle_avg,xslg\n"
        "592450,Aaron Judge,20.1,55.3,102.5,95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    result = diagnose_batter_leaderboard(2026)
    assert result["row_count"] == 1
    assert result["missing_columns"] == []
    assert result["range_warnings"] == []


def test_diagnose_leaderboard_catches_missing_column(monkeypatch):
    import ingestion.statcast as statcast_mod

    # Savant renamed "barrel_batted_rate" to something else -- our
    # expected column is now missing from the actual response.
    csv_text = (
        "player_id,player_name,barrel_rate_renamed,hard_hit_percent,"
        "avg_hyper_speed,exit_velocity_avg,exit_velocity_max,"
        "launch_angle_avg,xslg\n"
        "592450,Aaron Judge,20.1,55.3,102.5,95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    result = diagnose_batter_leaderboard(2026)
    assert "barrel_batted_rate" in result["missing_columns"]
    assert "barrel_rate_renamed" in result["extra_columns"]


def test_diagnose_leaderboard_catches_out_of_range_value(monkeypatch):
    import ingestion.statcast as statcast_mod

    # hard_hit_percent of 550 is nonsensical for a 0-100 percentage --
    # should be flagged as a range warning.
    csv_text = (
        "player_id,player_name,barrel_batted_rate,hard_hit_percent,"
        "avg_hyper_speed,exit_velocity_avg,exit_velocity_max,"
        "launch_angle_avg,xslg\n"
        "592450,Aaron Judge,20.1,550.0,102.5,95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    result = diagnose_batter_leaderboard(2026)
    assert any("hard_hit_percent" in w for w in result["range_warnings"])


def test_diagnose_leaderboard_catches_non_numeric_value(monkeypatch):
    import ingestion.statcast as statcast_mod

    csv_text = (
        "player_id,player_name,barrel_batted_rate,hard_hit_percent,"
        "avg_hyper_speed,exit_velocity_avg,exit_velocity_max,"
        "launch_angle_avg,xslg\n"
        "592450,Aaron Judge,N/A,55.3,102.5,95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    result = diagnose_batter_leaderboard(2026)
    assert any("non-numeric" in w for w in result["range_warnings"])


def test_diagnose_leaderboard_handles_bom_and_quoted_comma_column(monkeypatch):
    """Regression test for a real bug found via live use: Savant's CSV
    response starts with a UTF-8 BOM, and its actual leading column is a
    single quoted field containing a comma (e.g. "last_name, first_name").
    With the BOM in front, naive parsing (resp.text + csv.DictReader)
    fails to recognize the opening quote, splits that one column into
    two garbage ones, and shifts every other value in the row by one
    position -- which is exactly what showed up live as nonsensical
    exit_velocity_max values. This confirms the utf-8-sig fix resolves it.
    """
    import ingestion.statcast as statcast_mod

    csv_text = (
        '\ufeff"last_name, first_name",player_id,player_name,year,'
        "barrel_batted_rate,hard_hit_percent,avg_hyper_speed,"
        "exit_velocity_avg,exit_velocity_max,launch_angle_avg,xslg\n"
        '"Judge, Aaron",592450,Aaron Judge,2026,20.1,55.3,102.5,'
        "95.2,118.3,15.4,0.650\n"
    )

    def fake_get(url, params=None, timeout=None):
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    result = diagnose_batter_leaderboard(2026)

    # The compound name column should parse as ONE clean column, not
    # split into garbage fragments with stray quote characters in the name.
    assert "last_name, first_name" in result["actual_columns"]
    assert not any('"' in c for c in result["actual_columns"])

    # All our real requested columns should have correct, non-shifted values.
    assert result["missing_columns"] == []
    assert result["range_warnings"] == []
    assert result["sample_rows"][0]["exit_velocity_max"] == "118.3"


# ---------------------------------------------------------------------------
# min_sample -- the qualified-player-filter fix (finding #3 from the
# sub-metric breakdown diagnostic)
# ---------------------------------------------------------------------------

def test_default_min_sample_is_not_the_old_qualified_filter():
    # Regression guard: this project's diagnostic found "q" excludes
    # ~52.5% of hitters and ~66.4% of pitchers. Make sure nobody
    # accidentally reverts the default back to "q".
    assert DEFAULT_MIN_SAMPLE != "q"


def test_fetch_batter_power_metrics_uses_default_min_sample(monkeypatch):
    import ingestion.statcast as statcast_mod
    csv_text = "player_id,player_name\n123,Test Player\n"
    captured_params = {}

    def fake_get(url, params=None, timeout=None):
        captured_params.update(params)
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    fetch_batter_power_metrics(2026)
    assert captured_params["min"] == DEFAULT_MIN_SAMPLE


def test_fetch_batter_power_metrics_min_sample_is_overridable(monkeypatch):
    import ingestion.statcast as statcast_mod
    csv_text = "player_id,player_name\n123,Test Player\n"
    captured_params = {}

    def fake_get(url, params=None, timeout=None):
        captured_params.update(params)
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    fetch_batter_power_metrics(2026, min_sample="q")
    assert captured_params["min"] == "q"


def test_diagnose_leaderboard_passes_through_min_sample(monkeypatch):
    import ingestion.statcast as statcast_mod
    csv_text = "player_id,player_name\n123,Test Player\n"
    captured_params = {}

    def fake_get(url, params=None, timeout=None):
        captured_params.update(params)
        return FakeCsvResponse(csv_text)

    monkeypatch.setattr(statcast_mod.requests, "get", fake_get)

    diagnose_batter_leaderboard(2026, min_sample="25")
    assert captured_params["min"] == "25"
