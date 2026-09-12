"""Collects game, pitcher, and hitter data from the official MLB Stats API
(https://statsapi.mlb.com -- free, no API key required, stable and
documented, unlike Baseball Savant -- see ingestion/statcast.py for the
barrel%/hard-hit%/exit-velocity metrics that only Statcast has).

Covers:
    fetch_games(date)                  -- today's schedule + probable pitchers
    fetch_starting_lineup(game_id)     -- confirmed batting order (once posted)
    fetch_team_roster(team_id)         -- active roster for a team
    fetch_player_info(person_id)       -- name, handedness, position
    fetch_pitching_stats(person_id, season)  -- ERA, WHIP, HR/9, K%, BB%, FIP
    fetch_hitting_stats(person_id, season)   -- AVG, ISO, HR, K%, BB%
    fetch_league_pitching_stats(season)      -- bulk HR/9 etc for EVERY
        pitcher in one call -- for benchmark calibration (see
        models/calibration.py), not per-game scoring
    fetch_league_hitting_stats(season)        -- bulk ISO etc for EVERY
        hitter in one call, same purpose

NOT covered here (see ingestion/statcast.py instead):
    barrel%, hard-hit%, exit velocity, launch angle, xSLG -- these are
    Statcast-only metrics not exposed by the Stats API.

NOT yet implemented anywhere (TODO for a future pass):
    xFIP            -- needs league-average HR/FB rate as an input
    pitch arsenal usage / pitch-type run values -- needs Statcast
                        pitch-level data, more involved than a leaderboard
    park HR factor  -- needs Savant's park-factors leaderboard (same
                        unofficial-CSV caveats as statcast.py)
    platoon splits (HR/ISO vs LHP/RHP) -- Stats API supports this via
                        stats=statSplits but isn't wired up yet

This has been written against the Stats API's documented response shape
but not tested live -- this dev environment has no network access to
statsapi.mlb.com. If a field comes back missing/renamed, print the raw
JSON for that endpoint and adjust the parsing below.

Docs (unofficial but widely used/stable):
https://github.com/toddrob99/MLB-StatsAPI/wiki/Endpoints
"""

from __future__ import annotations

from datetime import date as _date

import requests

BASE_URL = "https://statsapi.mlb.com/api/v1"
MLB_SPORT_ID = 1


def fetch_games(game_date: _date) -> list[dict]:
    """Fetch the MLB schedule for a given date, including probable
    starting pitchers and venue.

    Returns a list of dicts:
        {
            "date": "2026-07-08",
            "game_id": int,               # gamePk
            "start_time": "2026-07-08T23:05:00Z",
            "status": "Scheduled",
            "home_team": "New York Yankees",
            "home_team_id": int,
            "away_team": "Tampa Bay Rays",
            "away_team_id": int,
            "venue": "Yankee Stadium",
            "venue_id": int,
            "home_probable_pitcher": {"id": int, "name": str} | None,
            "away_probable_pitcher": {"id": int, "name": str} | None,
        }

    NOTE: park HR factor, Vegas game total, and team implied runs are
    NOT included here -- park factor needs Savant (see statcast.py TODO),
    and Vegas totals belong in ingestion/odds.py (game-level markets like
    "totals" aren't implemented there yet either -- current odds.py only
    covers the HR player-prop market).
    """
    resp = requests.get(
        f"{BASE_URL}/schedule",
        params={
            "sportId": MLB_SPORT_ID,
            "date": game_date.isoformat(),
            "hydrate": "probablePitcher,venue,team",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    games = []
    for date_block in data.get("dates", []):
        for game in date_block.get("games", []):
            teams = game.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})

            def _probable_pitcher(side: dict) -> dict | None:
                pitcher = side.get("probablePitcher")
                if not pitcher:
                    return None
                return {"id": pitcher.get("id"), "name": pitcher.get("fullName")}

            games.append({
                "date": game_date.isoformat(),
                "game_id": game.get("gamePk"),
                "start_time": game.get("gameDate"),
                "status": game.get("status", {}).get("detailedState"),
                "home_team": home.get("team", {}).get("name"),
                "home_team_id": home.get("team", {}).get("id"),
                "away_team": away.get("team", {}).get("name"),
                "away_team_id": away.get("team", {}).get("id"),
                "venue": game.get("venue", {}).get("name"),
                "venue_id": game.get("venue", {}).get("id"),
                "home_probable_pitcher": _probable_pitcher(home),
                "away_probable_pitcher": _probable_pitcher(away),
            })

    return games


def fetch_starting_lineup(game_id: int) -> list[dict]:
    """Confirmed starting batting order for both teams in a game, once
    lineups have been posted (typically ~1-3 hours before first pitch --
    before that, this returns empty batter lists, which is a legitimate
    "not yet confirmed" state, not an error).

    Uses the boxscore endpoint's per-player "battingOrder" field.
    battingOrder values are strings like "100", "200", ... "900" for the
    9 starting lineup slots; a mid-game substitution gets a suffixed
    value like "101" for whoever replaced the leadoff hitter. This only
    returns the ORIGINAL starters (battingOrder ending in "00"), not
    in-game substitutions.

    Returns:
        [
            {
                "side": "home" | "away",
                "team": str,
                "batters": [
                    {"batting_order": 1, "person_id": int, "name": str, "position": str},
                    ...  # sorted 1-9, empty list if lineup not posted yet
                ],
            },
            ...  # one entry per side
        ]
    """
    resp = requests.get(f"{BASE_URL}/game/{game_id}/boxscore", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    lineup = []
    for side in ("home", "away"):
        team_data = data.get("teams", {}).get(side, {})
        players = team_data.get("players", {})

        batters = []
        for player_data in players.values():
            batting_order = player_data.get("battingOrder")
            if not batting_order or not batting_order.endswith("00"):
                continue  # not a starter (bench, pitcher, or a substitution slot)
            batters.append({
                "batting_order": int(batting_order) // 100,
                "person_id": player_data.get("person", {}).get("id"),
                "name": player_data.get("person", {}).get("fullName"),
                "position": player_data.get("position", {}).get("abbreviation"),
            })
        batters.sort(key=lambda b: b["batting_order"])

        lineup.append({
            "side": side,
            "team": team_data.get("team", {}).get("name"),
            "batters": batters,
        })

    return lineup


# Statuses considered "final" enough to trust box score stats for
# result-recording. MLB Stats API uses several variants for a completed
# game; anything not in here is treated as "not final yet" rather than
# guessed at (a postponed/suspended/in-progress game should never be
# silently recorded as "no HR"). Game status is available from the
# "status" field fetch_games() already returns -- no separate call needed.
FINAL_GAME_STATUSES = {"Final", "Game Over", "Completed Early"}


def fetch_game_hr_results(game_id: int) -> dict[str, dict]:
    """How many home runs each player in a specific game actually hit,
    from the box score. Used the morning after to auto-record results
    against saved HR Alpha Score rankings, instead of manual entry.

    Returns a dict keyed by person_id (as a string, to match how
    database/repository.py stores/looks up player IDs elsewhere):
        {
            "592450": {"name": "Aaron Judge", "home_runs": 2},
            ...
        }
    Players who appeared in the game but didn't homer are still included
    (home_runs: 0) -- callers should treat a MISSING person_id (not in
    this dict at all) differently from a 0 -- missing means they likely
    didn't play, not that they went homerless.
    """
    resp = requests.get(f"{BASE_URL}/game/{game_id}/boxscore", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = {}
    for side in ("home", "away"):
        players = data.get("teams", {}).get(side, {}).get("players", {})
        for player_data in players.values():
            person = player_data.get("person", {})
            person_id = person.get("id")
            if person_id is None:
                continue
            batting_stats = player_data.get("stats", {}).get("batting", {})
            # A player with no batting appearance (e.g. a pitcher who
            # didn't bat) won't have a "homeRuns" key at all -- default
            # to 0 only when they DID have a batting stat line.
            if not batting_stats:
                continue
            results[str(person_id)] = {
                "name": person.get("fullName"),
                "home_runs": batting_stats.get("homeRuns", 0),
            }

    return results


def fetch_team_roster(team_id: int, roster_type: str = "active") -> list[dict]:
    """Active roster for a team.

    Returns list of {"person_id": int, "name": str, "position": str}.
    """
    resp = requests.get(
        f"{BASE_URL}/teams/{team_id}/roster",
        params={"rosterType": roster_type},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    return [
        {
            "person_id": entry.get("person", {}).get("id"),
            "name": entry.get("person", {}).get("fullName"),
            "position": entry.get("position", {}).get("abbreviation"),
        }
        for entry in data.get("roster", [])
    ]


def fetch_player_info(person_id: int) -> dict:
    """Name, bat side, and pitch hand for a player.

    Returns:
        {
            "person_id": int,
            "name": str,
            "bat_side": "L" | "R" | "S" | None,
            "pitch_hand": "L" | "R" | None,
        }
    """
    resp = requests.get(f"{BASE_URL}/people/{person_id}", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    people = data.get("people", [])
    if not people:
        raise LookupError(f"No player found for person_id={person_id}")

    person = people[0]
    return {
        "person_id": person_id,
        "name": person.get("fullName"),
        "bat_side": person.get("batSide", {}).get("code"),
        "pitch_hand": person.get("pitchHand", {}).get("code"),
    }


def fetch_pitching_stats(person_id: int, season: int) -> dict:
    """Season pitching stats for one pitcher, plus a few derived metrics
    the raw API doesn't compute directly (HR/9, K%, BB%, and a
    standard-formula FIP).

    Returns a dict with both raw Stats API fields and derived ones, e.g.:
        {
            "person_id": int, "season": int,
            "era": float, "whip": float,
            "innings_pitched": float, "batters_faced": int,
            "strikeouts": int, "walks": int, "home_runs_allowed": int,
            "hr9": float, "k_pct": float, "bb_pct": float,
            "fip": float,  # standard formula, league-average FIP
                            # constant approximated at 3.20 -- swap in
                            # the actual seasonal constant if precision
                            # matters for your model
            "xfip": None,  # TODO: needs league-average HR/FB rate
        }

    Returns an empty-ish dict with stat fields as None if the player has
    no pitching stats for that season (e.g. a position player).
    """
    resp = requests.get(
        f"{BASE_URL}/people/{person_id}/stats",
        params={"stats": "season", "group": "pitching", "season": season},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return {"person_id": person_id, "season": season, "has_stats": False}

    s = splits[0].get("stat", {})

    innings_pitched = float(s.get("inningsPitched", 0) or 0)
    batters_faced = s.get("battersFaced", 0) or 0
    strikeouts = s.get("strikeOuts", 0) or 0
    walks = s.get("baseOnBalls", 0) or 0
    hr_allowed = s.get("homeRuns", 0) or 0
    hit_by_pitch = s.get("hitByPitch", 0) or 0

    hr9 = (hr_allowed / innings_pitched * 9) if innings_pitched else None
    k_pct = (strikeouts / batters_faced) if batters_faced else None
    bb_pct = (walks / batters_faced) if batters_faced else None

    # Standard FIP formula. 3.20 is a reasonable recent-era MLB-wide FIP
    # constant used as a default -- for precision, replace with the
    # actual constant for `season` (published by FanGraphs each year).
    fip_constant = 3.20
    fip = (
        ((13 * hr_allowed) + (3 * (walks + hit_by_pitch)) - (2 * strikeouts))
        / innings_pitched + fip_constant
    ) if innings_pitched else None

    return {
        "person_id": person_id,
        "season": season,
        "has_stats": True,
        "era": s.get("era"),
        "whip": s.get("whip"),
        "innings_pitched": innings_pitched,
        "batters_faced": batters_faced,
        "strikeouts": strikeouts,
        "walks": walks,
        "home_runs_allowed": hr_allowed,
        "hr9": hr9,
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "fip": fip,
        "xfip": None,  # TODO
    }


def fetch_hitting_stats(person_id: int, season: int) -> dict:
    """Season hitting stats for one batter, plus ISO/K%/BB% derived
    metrics.

    Returns:
        {
            "person_id": int, "season": int,
            "avg": float, "obp": float, "slg": float, "iso": float,
            "home_runs": int, "at_bats": int, "plate_appearances": int,
            "strikeouts": int, "walks": int,
            "k_pct": float, "bb_pct": float,
        }
    """
    resp = requests.get(
        f"{BASE_URL}/people/{person_id}/stats",
        params={"stats": "season", "group": "hitting", "season": season},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = data.get("stats", [{}])[0].get("splits", [])
    if not splits:
        return {"person_id": person_id, "season": season, "has_stats": False}

    s = splits[0].get("stat", {})

    avg = float(s.get("avg", 0) or 0)
    slg = float(s.get("slg", 0) or 0)
    plate_appearances = s.get("plateAppearances", 0) or 0
    strikeouts = s.get("strikeOuts", 0) or 0
    walks = s.get("baseOnBalls", 0) or 0

    return {
        "person_id": person_id,
        "season": season,
        "has_stats": True,
        "avg": avg,
        "obp": s.get("obp"),
        "slg": slg,
        "iso": round(slg - avg, 3),
        "home_runs": s.get("homeRuns"),
        "at_bats": s.get("atBats"),
        "plate_appearances": plate_appearances,
        "strikeouts": strikeouts,
        "walks": walks,
        "k_pct": (strikeouts / plate_appearances) if plate_appearances else None,
        "bb_pct": (walks / plate_appearances) if plate_appearances else None,
    }


def fetch_league_pitching_stats(season: int, limit: int = 1000, min_innings_pitched: float = 0.0) -> list[dict]:
    """Bulk season pitching stats for EVERY pitcher with a stat line in
    `season`, in a single API call -- used for calibrating benchmark
    ranges against real league-wide distributions (see
    models/calibration.py). NOT used for per-game scoring -- that still
    calls fetch_pitching_stats for one known pitcher at a time.

    Uses the same derived-metric logic as fetch_pitching_stats (HR/9,
    K%, BB%, FIP), applied across every returned row instead of one.

    CONFIRMED FIX (live-verified for the hitting side; applied here on
    the reasonable assumption the same endpoint behavior holds for
    pitching -- see caveat below): without playerPool=ALL, this endpoint
    defaults to returning only "qualified" players (batting/ERA-title
    style thresholds), the same failure pattern as Baseball Savant's
    min=q default. For hitting, this was empirically confirmed: 149 raw
    rows without the override, 658 with it (597 with usable plate
    appearances) -- see ingestion.mlb_data.debug_bulk_stats_response,
    the tool that found this. This project's own calibration audit
    independently flagged the same symptom on the pitching side before
    this fix (hr9's sample size was a suspiciously low 62, versus 500+
    for Statcast-sourced pitcher metrics from the same population) --
    consistent with, though not itself proof of, the same root cause.

    CAVEAT: the playerPool=ALL fix itself was only directly verified
    against the hitting endpoint in this project's live testing, not
    the pitching one specifically. If hr9's sample size doesn't jump
    similarly after this change (re-run the calibration audit to check),
    revisit this.

    Args:
        season: year.
        limit: max rows to request. 1000 comfortably covers every
            pitcher who's thrown a pitch in a season (~450-500 typically).
        min_innings_pitched: excludes pitchers below this IP threshold.
            Default 0.0 preserves the original "any innings" behavior.
            Raising this is a calibration tool, not a production-scoring
            change -- see models.calibration's use of this param to test
            whether small-sample outliers (e.g. a September call-up's
            1 IP, 0 HR allowed -> hr9=0) are inflating apparent
            production-impact crush rates in the full playerPool=ALL
            population, versus a genuine range-calibration problem.

    Returns:
        List of dicts, one per pitcher with innings pitched above the
        threshold:
            {"person_id": int, "name": str, "innings_pitched": float,
             "hr9": float, "k_pct": float | None, "bb_pct": float | None,
             "fip": float}
    """
    resp = requests.get(
        f"{BASE_URL}/stats",
        params={
            "stats": "season", "group": "pitching", "season": season, "sportId": MLB_SPORT_ID,
            "limit": limit, "playerPool": "ALL",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = data.get("stats", [{}])[0].get("splits", [])
    fip_constant = 3.20  # see fetch_pitching_stats's docstring for the same caveat

    results = []
    for split in splits:
        s = split.get("stat", {})
        person = split.get("player", {})

        innings_pitched = float(s.get("inningsPitched", 0) or 0)
        if innings_pitched <= min_innings_pitched:
            continue  # can't compute meaningful rate stats below the threshold

        batters_faced = s.get("battersFaced", 0) or 0
        strikeouts = s.get("strikeOuts", 0) or 0
        walks = s.get("baseOnBalls", 0) or 0
        hr_allowed = s.get("homeRuns", 0) or 0
        hit_by_pitch = s.get("hitByPitch", 0) or 0

        fip = ((13 * hr_allowed) + (3 * (walks + hit_by_pitch)) - (2 * strikeouts)) / innings_pitched + fip_constant

        results.append({
            "person_id": person.get("id"),
            "name": person.get("fullName"),
            "innings_pitched": innings_pitched,
            "hr9": hr_allowed / innings_pitched * 9,
            "k_pct": (strikeouts / batters_faced) if batters_faced else None,
            "bb_pct": (walks / batters_faced) if batters_faced else None,
            "fip": fip,
        })

    return results


def debug_bulk_stats_response(
    group: str, season: int, limit: int = 1000, extra_params: dict | None = None,
) -> dict:
    """Raw-response inspector for the bulk /stats endpoint -- answers,
    in one call, exactly what a manual investigation would check by hand:
        - how many rows the API actually returned
        - any top-level metadata fields we might be ignoring (pagination,
          total-record counts, truncation indicators)
        - the exact request params sent
        - whether OUR code's plate_appearances/innings_pitched filter is
          what's cutting rows, vs. the API only returning a small set to
          begin with

    Use this BEFORE assuming fetch_league_hitting_stats()/
    fetch_league_pitching_stats()'s row counts mean what you think they
    mean -- exactly the discipline that caught the fly_ball_pct units
    question (which turned out not to be a units bug at all).

    Args:
        group: "hitting" or "pitching".
        season: year.
        limit: same param passed to the real fetch functions.
        extra_params: additional/override query params to test hypotheses
            about the API's default behavior. CONFIRMED via this tool:
            this endpoint defaults to "qualified" players only (149 of
            658 real hitters) unless playerPool=ALL is passed -- that
            fix is now applied by default in fetch_league_hitting_stats()
            and fetch_league_pitching_stats(). This param remains here
            for investigating any *other* future row-count discrepancies
            without needing a code change to test each hypothesis.

    Returns:
        {
            "request_url": str, "request_params": dict,
            "top_level_keys": [str, ...],       # anything besides "stats"
                                                  # here could be pagination
                                                  # metadata we're not reading
            "other_top_level_fields": dict,      # their actual values
            "stats_array_length": int,           # usually 1 -- more would
                                                  # itself be a surprise
            "splits_count_raw": int,             # BEFORE any filtering --
                                                  # this is what the API
                                                  # actually sent us
            "splits_with_positive_pa_or_ip": int,  # what our filter keeps
            "splits_with_zero_or_missing_pa_or_ip": int,  # what our filter drops
            "sample_splits": [dict, ...],         # first 5 raw, completely
                                                    # unfiltered, for eyeballing
        }
    """
    url = f"{BASE_URL}/stats"
    params = {"stats": "season", "group": group, "season": season, "sportId": MLB_SPORT_ID, "limit": limit}
    if extra_params:
        params.update(extra_params)

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    stats_array = data.get("stats", [])
    splits = stats_array[0].get("splits", []) if stats_array else []

    pa_or_ip_field = "plateAppearances" if group == "hitting" else "inningsPitched"

    positive_count = 0
    zero_or_missing_count = 0
    for split in splits:
        raw_value = split.get("stat", {}).get(pa_or_ip_field)
        try:
            numeric = float(raw_value) if raw_value is not None else 0.0
        except (TypeError, ValueError):
            numeric = 0.0
        if numeric > 0:
            positive_count += 1
        else:
            zero_or_missing_count += 1

    return {
        "request_url": url,
        "request_params": params,
        "top_level_keys": list(data.keys()),
        "other_top_level_fields": {k: v for k, v in data.items() if k != "stats"},
        "stats_array_length": len(stats_array),
        "splits_count_raw": len(splits),
        "splits_with_positive_pa_or_ip": positive_count,
        "splits_with_zero_or_missing_pa_or_ip": zero_or_missing_count,
        "sample_splits": splits[:5],
    }


def fetch_league_hitting_stats(season: int, limit: int = 1000, min_plate_appearances: int = 0) -> list[dict]:
    """Bulk season hitting stats for EVERY hitter with a stat line in
    `season`, in a single API call -- same calibration purpose as
    fetch_league_pitching_stats above.

    CONFIRMED FIX, live-verified: without playerPool=ALL, this endpoint
    defaults to returning only "qualified" hitters (batting-title style
    threshold, ~3.1 PA/team-game) -- 149 raw rows without the override,
    658 with it (597 with usable plate appearances), confirmed via
    ingestion.mlb_data.debug_bulk_stats_response against a live pull.
    Same failure pattern as Baseball Savant's min=q default, and the
    reason ISO's calibration-audit sample size (149) was far below the
    other hitter metrics' (~565, sourced from Statcast instead).

    Args:
        season: year.
        limit: max rows to request.
        min_plate_appearances: excludes hitters below this PA threshold.
            Default 0 preserves the original "any plate appearance"
            behavior. Same calibration-tool purpose as
            fetch_league_pitching_stats's min_innings_pitched -- see
            its docstring.

    Returns:
        List of dicts, one per hitter with plate appearances above the
        threshold:
            {"person_id": int, "name": str, "plate_appearances": int,
             "iso": float, "k_pct": float | None, "bb_pct": float | None}
    """
    resp = requests.get(
        f"{BASE_URL}/stats",
        params={
            "stats": "season", "group": "hitting", "season": season, "sportId": MLB_SPORT_ID,
            "limit": limit, "playerPool": "ALL",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = data.get("stats", [{}])[0].get("splits", [])
    results = []
    for split in splits:
        s = split.get("stat", {})
        person = split.get("player", {})

        plate_appearances = s.get("plateAppearances", 0) or 0
        if plate_appearances <= min_plate_appearances:
            continue

        avg = float(s.get("avg", 0) or 0)
        slg = float(s.get("slg", 0) or 0)
        strikeouts = s.get("strikeOuts", 0) or 0
        walks = s.get("baseOnBalls", 0) or 0

        results.append({
            "person_id": person.get("id"),
            "name": person.get("fullName"),
            "plate_appearances": plate_appearances,
            "iso": round(slg - avg, 3),
            "k_pct": (strikeouts / plate_appearances) if plate_appearances else None,
            "bb_pct": (walks / plate_appearances) if plate_appearances else None,
        })

    return results
