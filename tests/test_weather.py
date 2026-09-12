import math

from ingestion.weather import (
    compute_wind_out_component,
    get_game_weather,
    _parse_iso_utc,
)


# ---------------------------------------------------------------------------
# Pure wind-direction physics -- no network needed
# ---------------------------------------------------------------------------

def test_wind_blowing_straight_out_gives_full_positive_speed():
    # Park orientation 0 (center field due north). Wind blowing straight
    # out to center means it's blowing FROM due south (180) TOWARD north.
    result = compute_wind_out_component(wind_speed_mph=10.0, wind_direction_deg=180, park_orientation_deg=0)
    assert math.isclose(result, 10.0, abs_tol=1e-6)


def test_wind_blowing_straight_in_gives_full_negative_speed():
    # Wind blowing FROM due north (0) TOWARD south -- blowing in from
    # center field toward home plate.
    result = compute_wind_out_component(wind_speed_mph=10.0, wind_direction_deg=0, park_orientation_deg=0)
    assert math.isclose(result, -10.0, abs_tol=1e-6)


def test_crosswind_gives_near_zero_component():
    # Wind blowing FROM due east (90) toward west -- pure crosswind
    # relative to a park oriented due north.
    result = compute_wind_out_component(wind_speed_mph=10.0, wind_direction_deg=90, park_orientation_deg=0)
    assert math.isclose(result, 0.0, abs_tol=1e-6)


def test_parse_iso_utc_handles_z_suffix():
    dt = _parse_iso_utc("2026-07-08T23:05:00Z")
    assert dt.year == 2026 and dt.month == 7 and dt.day == 8 and dt.hour == 23


# ---------------------------------------------------------------------------
# get_game_weather -- dome short-circuit (no network call) and unknown venue
# ---------------------------------------------------------------------------

def test_dome_park_returns_defaults_without_network_call(monkeypatch):
    import ingestion.weather as weather_mod

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not call the network for a dome park")

    monkeypatch.setattr(weather_mod.requests, "get", fail_if_called)

    result = get_game_weather("Tropicana Field", "2026-07-08T23:05:00Z")
    assert result["roof_type"] == "fixed_dome"
    assert result["roof_status"] == "closed_assumed"
    assert result["data_source"] == "dome_default"
    assert result["wind_out_mph"] == 0.0


def test_unknown_venue_fails_gracefully():
    result = get_game_weather("Not A Real Stadium", "2026-07-08T23:05:00Z")
    assert result["roof_status"] == "unknown_venue"
    assert result["data_source"] == "unavailable"
    assert result["temperature"] is None


# ---------------------------------------------------------------------------
# get_game_weather -- open-air park with a mocked Open-Meteo response
# ---------------------------------------------------------------------------

def test_open_air_park_parses_mocked_forecast(monkeypatch):
    import ingestion.weather as weather_mod

    class FakeResponse:
        def __init__(self, json_data):
            self._json = json_data
            self.status_code = 200

        def json(self):
            return self._json

        def raise_for_status(self):
            pass

    forecast_payload = {
        "hourly": {
            "time": ["2026-07-08T22:00", "2026-07-08T23:00", "2026-07-09T00:00"],
            "temperature_2m": [78.0, 80.0, 79.0],
            "relative_humidity_2m": [55, 58, 60],
            "wind_speed_10m": [8.0, 12.0, 10.0],
            "wind_direction_10m": [180, 190, 200],
            "precipitation_probability": [5, 10, 15],
        }
    }

    def fake_get(url, params=None, timeout=None):
        return FakeResponse(forecast_payload)

    monkeypatch.setattr(weather_mod.requests, "get", fake_get)

    result = get_game_weather("Yankee Stadium", "2026-07-08T23:05:00Z")
    assert result["data_source"] == "open-meteo"
    assert result["temperature"] == 80.0
    assert result["humidity_pct"] == 58
    assert result["wind_speed_mph"] == 12.0
    assert result["wind_out_mph"] is not None
