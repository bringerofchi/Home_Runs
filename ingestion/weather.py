"""Collects weather conditions relevant to home run probability.

Data source: Open-Meteo (https://open-meteo.com) -- free, no API key
required, documented, stable. Unlike Baseball Savant, this is a real
public API contract, not a scraped CSV export.

Covers:
    get_game_weather(venue_name, start_time) -- wind speed/direction
        (resolved into a "blowing out to center field" component),
        temperature, humidity, precipitation probability, and roof
        status for a specific game.

NOT covered here:
    get_park_factor() -- park HR factor is NOT a weather metric, it
        belongs with Statcast data (see ingestion/statcast.py's TODO
        for Savant's park-factor leaderboard). Not implemented in this
        module on purpose.

KNOWN LIMITATION -- roof status: there is no free public API for a
retractable roof's actual per-game open/closed state. This module
defaults every "retractable" park (see ingestion/ballpark_reference.py)
to CLOSED and returns climate-controlled default values, clearly
labeled roof_status="closed_assumed". If real roof status matters to
your model, that needs a different source (team beat writers/social
media, or a paid data feed) -- there isn't a reliable free API for it
as of this build. TODO for a future pass.

This has been written against Open-Meteo's documented forecast API
response shape but not tested live -- this dev environment has no
network access to open-meteo.com. If a field comes back missing/
different, print the raw JSON and adjust the parsing below.

Docs: https://open-meteo.com/en/docs
"""

from __future__ import annotations

import math
from datetime import datetime

import requests

from ingestion.ballpark_reference import get_ballpark_reference

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Default conditions assumed for domes / closed retractable roofs --
# a climate-controlled indoor environment. 72F / 50% humidity / no wind
# are reasonable defaults for an air-conditioned stadium; adjust if you
# have better data for a specific park.
DOME_DEFAULT_CONDITIONS = {
    "temperature": 72.0,
    "humidity_pct": 50.0,
    "wind_speed_mph": 0.0,
    "wind_direction_deg": None,
    "wind_out_mph": 0.0,
    "precipitation_probability_pct": 0.0,
}


def _parse_iso_utc(timestamp: str) -> datetime:
    """Parse an ISO 8601 timestamp (as returned by MLB Stats API's
    gameDate field, e.g. "2026-07-08T23:05:00Z") into a datetime.
    """
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def compute_wind_out_component(
    wind_speed_mph: float, wind_direction_deg: float, park_orientation_deg: float,
) -> float:
    """Resolve a raw wind speed/direction into the component blowing OUT
    toward center field for a park with a given orientation.

    Args:
        wind_speed_mph: raw wind speed.
        wind_direction_deg: meteorological wind direction -- the
            direction the wind is blowing FROM (Open-Meteo's convention:
            0=N, 90=E, 180=S, 270=W).
        park_orientation_deg: compass bearing from home plate toward
            center field (see ingestion.ballpark_reference).

    Returns:
        Positive value = wind blowing out toward center (helps HR).
        Negative value = wind blowing in from center (suppresses HR).
        Values near 0 = crosswind or calm.

    Physics: wind blowing "out" to center means it's blowing TOWARD the
    park_orientation_deg bearing, i.e. blowing FROM the opposite bearing
    (orientation + 180). We take the angle between the wind's actual
    "blowing toward" direction and the park's orientation, and project
    the wind speed onto that axis with cosine (full credit when aligned,
    zero at 90 degrees, negative when opposed).
    """
    blowing_toward_deg = (wind_direction_deg + 180) % 360
    angle_diff = abs(blowing_toward_deg - park_orientation_deg) % 360
    if angle_diff > 180:
        angle_diff = 360 - angle_diff

    return wind_speed_mph * math.cos(math.radians(angle_diff))


def fetch_hourly_forecast(lat: float, lon: float, target_time: datetime) -> dict | None:
    """Fetch Open-Meteo's hourly forecast for a location and return the
    single hour closest to target_time.

    Returns None if the API call fails or target_time is outside the
    forecast window Open-Meteo returns (it's a forecast API, not a
    historical archive -- see module docstring; for backtesting past
    games, Open-Meteo's separate historical/archive API would be needed
    instead, not implemented here).
    """
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                       "wind_direction_10m,precipitation_probability",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "UTC",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return None

    target_str_prefix = target_time.strftime("%Y-%m-%dT%H")
    for i, t in enumerate(times):
        if t.startswith(target_str_prefix):
            return {
                "temperature": hourly.get("temperature_2m", [None] * len(times))[i],
                "humidity_pct": hourly.get("relative_humidity_2m", [None] * len(times))[i],
                "wind_speed_mph": hourly.get("wind_speed_10m", [None] * len(times))[i],
                "wind_direction_deg": hourly.get("wind_direction_10m", [None] * len(times))[i],
                "precipitation_probability_pct": hourly.get("precipitation_probability", [None] * len(times))[i],
            }

    return None  # target_time outside the returned forecast window


def get_game_weather(venue_name: str, start_time: str) -> dict:
    """Get normalized weather conditions for a specific game.

    Args:
        venue_name: must match a key in ingestion.ballpark_reference.BALLPARKS
            (this is the venue name MLB Stats API returns -- see
            ingestion.mlb_data.fetch_games()'s "venue" field).
        start_time: ISO 8601 UTC timestamp string (MLB Stats API's
            gameDate field format, e.g. "2026-07-08T23:05:00Z").

    Returns:
        {
            "venue": str,
            "roof_type": "open_air" | "fixed_dome" | "retractable" | "unknown",
            "roof_status": "open_air" | "closed_assumed" | "unknown_venue",
            "data_source": "open-meteo" | "dome_default" | "unavailable",
            "temperature": float | None,
            "humidity_pct": float | None,
            "wind_speed_mph": float | None,
            "wind_direction_deg": float | None,
            "wind_out_mph": float | None,   # see compute_wind_out_component
            "precipitation_probability_pct": float | None,
        }

    Fails gracefully (never raises) for unknown venues -- see
    ingestion.ballpark_reference module docstring for why this matters
    (team/venue assignments can change and this table can go stale).
    """
    park = get_ballpark_reference(venue_name)
    if park is None:
        return {
            "venue": venue_name,
            "roof_type": "unknown",
            "roof_status": "unknown_venue",
            "data_source": "unavailable",
            **{k: None for k in DOME_DEFAULT_CONDITIONS if k != "wind_direction_deg"},
            "wind_direction_deg": None,
        }

    if park["roof_type"] in ("fixed_dome", "retractable"):
        return {
            "venue": venue_name,
            "roof_type": park["roof_type"],
            "roof_status": "closed_assumed",
            "data_source": "dome_default",
            **DOME_DEFAULT_CONDITIONS,
        }

    target_dt = _parse_iso_utc(start_time)
    forecast = fetch_hourly_forecast(park["lat"], park["lon"], target_dt)

    if forecast is None:
        return {
            "venue": venue_name,
            "roof_type": park["roof_type"],
            "roof_status": "open_air",
            "data_source": "unavailable",
            **{k: None for k in DOME_DEFAULT_CONDITIONS if k != "wind_direction_deg"},
            "wind_direction_deg": None,
        }

    wind_out_mph = None
    if forecast["wind_speed_mph"] is not None and forecast["wind_direction_deg"] is not None:
        wind_out_mph = compute_wind_out_component(
            forecast["wind_speed_mph"], forecast["wind_direction_deg"], park["orientation_deg"],
        )

    return {
        "venue": venue_name,
        "roof_type": park["roof_type"],
        "roof_status": "open_air",
        "data_source": "open-meteo",
        "temperature": forecast["temperature"],
        "humidity_pct": forecast["humidity_pct"],
        "wind_speed_mph": forecast["wind_speed_mph"],
        "wind_direction_deg": forecast["wind_direction_deg"],
        "wind_out_mph": round(wind_out_mph, 2) if wind_out_mph is not None else None,
        "precipitation_probability_pct": forecast["precipitation_probability_pct"],
    }


# TODO: roof status for retractable-roof parks -- see module docstring.
# No free public API found for real per-game roof state; this always
# assumes closed for those parks. Revisit if a reliable source turns up.
