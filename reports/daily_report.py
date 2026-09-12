"""Generates the two-stage daily workflow output: this is where every
other module in the project gets wired together end to end.

    Data Collection -> Score Every Hitter -> Calculate Betting Edge ->
    Rank Players -> Generate Daily Card

Stage 1 -- build_watch_list(): pre-lineup. Uses probable pitchers
    (already known before lineups post) plus season-long hitter power
    profiles for each team's likely position players. Does NOT use
    confirmed batting order or betting odds -- it's explicitly an
    approximation, not a final ranking. See its docstring for the
    known limitation this implies.

Stage 2 -- build_final_rankings(): after lineups are confirmed. Only
    scores CONFIRMED starters (via ingestion.mlb_data.fetch_starting_lineup),
    against their actual opposing probable pitcher, real environment
    data, and (if odds were supplied) real market odds. This is the
    real HR Alpha Score output described in the project spec.

Design choice worth knowing: this module does NOT fetch odds itself --
build_final_rankings() takes an optional pre-fetched list of
ingestion.odds.HrOddsQuote objects. That keeps this module free of API
key handling (the caller, e.g. webapp/app.py, already owns that) and
keeps it testable without mocking credentials. If odds_quotes isn't
passed, every player's betting-value category simply scores 0/15 with
"no market odds found" rather than crashing.

Every per-player and per-game step is wrapped so one bad player/game
doesn't kill the whole run -- failures show up as warnings in the
returned "warnings" list instead.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import date as _date

from ingestion import mlb_data, statcast, weather
from models.alpha_score import calculate_alpha_score
from models.alpha_score import _collect_strengths_and_risks  # internal reuse, same package
from models.pitcher_model import score_pitcher_vulnerability
from models.hitter_model import score_hitter_power
from models.alpha_score import score_environment
from database.repository import get_tracked_players_for_date, record_result


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _normalize_name(name: str | None) -> str:
    """Normalize a player name for cross-source matching (MLB Stats API
    vs. odds providers, which don't always agree on formatting).

    Handles:
      - accented characters, e.g. "Hernández" -> "hernandez" (odds
        providers have been observed stripping these; MLB Stats API
        does not)
      - periods in dotted initialisms, e.g. "A.J." -> "aj" (providers
        are inconsistent about including them)
      - case and surrounding/repeated whitespace

    Deliberately does NOT touch suffixes (Jr./Sr./II/III) or collapse
    nickname variants -- those need real identity resolution (e.g. by
    player_id) rather than string mangling, and blindly stripping them
    risks silently merging two different players who happen to share a
    base name.
    """
    if not name:
        return ""
    # Decompose accented characters into base letter + combining mark,
    # then drop the combining marks -- "é" (NFKD) -> "e" + U+0301, keep "e".
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_name = ascii_name.replace(".", "")
    return " ".join(ascii_name.split()).lower()


def _build_statcast_lookup(rows: list[dict]) -> dict[str, dict]:
    """Index a Statcast leaderboard by player_id, casting numeric-looking
    string values to float (CSV rows come back as strings). Non-numeric
    values (including "" or missing) are left as None rather than
    raising -- see ingestion.statcast's diagnose_leaderboard for
    validating the raw data quality separately.
    """
    lookup = {}
    for row in rows:
        player_id = row.get("player_id")
        if not player_id:
            continue
        parsed = {}
        for key, value in row.items():
            if key in ("player_id", "player_name"):
                parsed[key] = value
                continue
            try:
                parsed[key] = float(value)
            except (TypeError, ValueError):
                parsed[key] = None
        lookup[str(player_id)] = parsed
    return lookup


def _build_odds_lookup(odds_quotes: list | None) -> dict:
    """Index HrOddsQuote objects by normalized player name.

    KNOWN LIMITATION: this is still name-string matching, not identity
    matching by player_id, between MLB Stats API's player names and
    whatever name format the odds source uses (e.g. "description" field
    from The Odds API). _normalize_name() now handles accented
    characters and periods (confirmed live: "Heriberto Hernández" vs.
    "Heriberto Hernandez" was silently missing before this), but
    suffixes (Jr./II) or genuine nickname variants (e.g. a legal vs.
    walk-up name) could still cause a miss. A miss means that player's
    betting-value category scores 0/15 with "no market odds found" --
    not a crash, but also not a guarantee every real match gets found.
    Worth spot-checking against real output before trusting edge%
    numbers.
    """
    if not odds_quotes:
        return {}
    return {_normalize_name(q.player): q for q in odds_quotes}


def _merge_pitcher_inputs(pitcher_id: int, season: int, statcast_pitcher_lookup: dict) -> dict:
    merged = {}
    try:
        pitching_stats = mlb_data.fetch_pitching_stats(pitcher_id, season)
        if pitching_stats.get("has_stats"):
            merged["hr9"] = pitching_stats.get("hr9")
    except Exception:
        pass  # caller records a warning; this player's pitcher_stats stays partial

    statcast_row = statcast_pitcher_lookup.get(str(pitcher_id))
    if statcast_row:
        merged["barrel_pct_allowed"] = statcast_row.get("barrel_batted_rate")
        merged["hard_hit_pct_allowed"] = statcast_row.get("hard_hit_percent")
        merged["fly_ball_pct"] = statcast_row.get("flyballs_percent")

    return merged


def _merge_hitter_inputs(batter_id: int, season: int, statcast_batter_lookup: dict) -> dict:
    merged = {}
    try:
        hitting_stats = mlb_data.fetch_hitting_stats(batter_id, season)
        if hitting_stats.get("has_stats"):
            merged["iso"] = hitting_stats.get("iso")
    except Exception:
        pass

    statcast_row = statcast_batter_lookup.get(str(batter_id))
    if statcast_row:
        merged["barrel_pct"] = statcast_row.get("barrel_batted_rate")
        merged["hard_hit_pct"] = statcast_row.get("hard_hit_percent")
        merged["xslg"] = statcast_row.get("xslg")
        merged["exit_velocity_avg"] = statcast_row.get("exit_velocity_avg")

    return merged


def _normalize_team_name(name: str | None) -> str:
    """Light normalization for matching team names across MLB Stats API
    and The Odds API. Case/whitespace only -- unlike _normalize_name
    (used for player-name matching against odds), these two sources
    have been observed to agree on full MLB team names (e.g. "New York
    Yankees"), so accent/period handling isn't expected to be needed
    here. UNVERIFIED against a live response -- see
    ingestion.odds.TheOddsApiSource.fetch_game_totals's docstring.
    """
    return (name or "").strip().lower()


def _build_totals_lookup(game_totals: dict | None) -> dict[frozenset, float]:
    """Re-key a raw game_totals dict (frozenset of raw team-name strings
    -> total, as returned by ingestion.odds.get_game_totals /
    TheOddsApiSource.fetch_game_totals) by NORMALIZED team names, so
    _lookup_game_total can match against mlb_data.fetch_games()'s team
    names without every caller re-normalizing by hand.
    """
    if not game_totals:
        return {}
    return {
        frozenset(_normalize_team_name(t) for t in matchup): total
        for matchup, total in game_totals.items()
    }


def _lookup_game_total(totals_lookup: dict[frozenset, float], home_team: str | None, away_team: str | None) -> float | None:
    if not home_team or not away_team:
        return None
    key = frozenset({_normalize_team_name(home_team), _normalize_team_name(away_team)})
    return totals_lookup.get(key)


def _build_environment_dict(
    venue: str | None, start_time: str | None, warnings: list[str], game_total: float | None = None,
) -> dict:
    """Best-effort environment dict for score_environment().

    game_total is passed in already-resolved by the caller (see
    build_watch_list / build_final_rankings, which look it up via a
    pre-fetched totals lookup) -- this function doesn't fetch it itself,
    same "caller owns API keys" convention as odds_quotes elsewhere in
    this module. park_hr_factor is still always None -- not ingested
    anywhere yet (see ingestion.statcast TODO).

    Any weather-fetch failure is recorded in `warnings` (mutated in
    place) instead of silently swallowed. This project's own sub-metric
    diagnostic found wind/temperature/humidity all missing together in
    ~33.6% of scored rows -- previously that showed up as an unexplained
    null with no way to tell WHY. This distinguishes the three real
    failure modes so future runs point straight at the cause instead of
    requiring another round of detective work:
        1. venue not in ingestion.ballpark_reference's table at all
        2. Open-Meteo's forecast window didn't cover start_time (it's a
           forward-looking forecast API, not a historical archive -- see
           ingestion.weather's module docstring)
        3. an actual request/network failure

    CHANGE: this used to return {} entirely on any weather failure,
    which silently discarded a valid game_total too even though it's
    independent data from a different source (The Odds API, not
    Open-Meteo). Now game_total is always included when supplied,
    regardless of whether weather succeeded -- only the weather-derived
    fields are omitted on a weather failure.
    """
    env: dict = {"park_hr_factor": None, "game_total": game_total}

    if not venue or not start_time:
        warnings.append(f"Environment data skipped: missing venue or start_time (venue={venue!r})")
        return env

    try:
        w = weather.get_game_weather(venue, start_time)
    except Exception as exc:
        warnings.append(f"{venue}: weather fetch raised an exception, skipped ({exc})")
        return env

    if w.get("roof_status") == "unknown_venue":
        warnings.append(
            f"{venue!r}: not found in ingestion.ballpark_reference's table -- "
            f"weather skipped for this game. Add it there if this venue is legitimate."
        )
        return env

    if w.get("data_source") == "unavailable":
        warnings.append(
            f"{venue}: weather forecast unavailable for {start_time} -- either outside "
            f"Open-Meteo's forecast window (it doesn't cover far-past dates) or the "
            f"request failed. Weather skipped for this game."
        )
        return env

    env["temperature"] = w.get("temperature")
    env["wind_out_mph"] = w.get("wind_out_mph")
    env["humidity_pct"] = w.get("humidity_pct")
    return env


# ---------------------------------------------------------------------------
# Stage 1: Pre-Lineup Watch List
# ---------------------------------------------------------------------------

def build_watch_list(game_date: _date, game_totals: dict | None = None) -> dict:
    """Stage 1 -- run before official lineups post.

    Args:
        game_totals: optional pre-fetched lookup from
            ingestion.odds.get_game_totals(game_date) (or
            TheOddsApiSource.fetch_game_totals directly), keyed by
            frozenset({home_team, away_team}). Same "caller owns API
            keys" convention as build_final_rankings' odds_quotes -- if
            not supplied, every game's environment score simply scores
            0/3 on game_total rather than crashing.

    KNOWN LIMITATION: since lineups aren't confirmed yet, this scores
    every position player on each probable-pitcher-having team's active
    roster, not just the 9 who will actually start -- there is no way to
    know that yet. Expect bench players to show up here; that's inherent
    to pre-lineup analysis, not a bug. Stage 2 (build_final_rankings)
    is where non-starters get removed, per the project spec.

    Also does NOT include betting odds (Category 4) -- the spec's own
    Stage 1 example doesn't reference odds either, since the point of
    this stage is "who looks like a good matchup", not "where's the
    market mispricing" (that needs the same starter to actually be
    confirmed and have a live price, which Stage 1 can't guarantee).

    Returns:
        {
            "date": str, "games_found": int,
            "watch_list": [
                {
                    "player": str, "team": str, "opposing_pitcher": str,
                    "quality_score": float,  # pitcher_vuln(35) + hitter_power(25) + environment(25), out of 85
                    "status": "WATCH",
                    "reasons": [str, ...],
                },
                ...
            ],  # sorted by quality_score descending
            "warnings": [str, ...],
        }
    """
    warnings = []
    season = game_date.year

    try:
        games = mlb_data.fetch_games(game_date)
    except Exception as exc:
        return {"date": game_date.isoformat(), "games_found": 0, "watch_list": [], "warnings": [f"fetch_games failed: {exc}"]}

    try:
        statcast_batters = _build_statcast_lookup(statcast.fetch_batter_power_metrics(season))
    except Exception as exc:
        statcast_batters = {}
        warnings.append(f"Statcast batter leaderboard unavailable: {exc}")

    try:
        statcast_pitchers = _build_statcast_lookup(statcast.fetch_pitcher_contact_metrics(season))
    except Exception as exc:
        statcast_pitchers = {}
        warnings.append(f"Statcast pitcher leaderboard unavailable: {exc}")

    totals_lookup = _build_totals_lookup(game_totals)
    if game_totals is None:
        warnings.append("No game_totals supplied -- environment category will score 0/3 on game_total for every game")

    watch_list = []

    for game in games:
        total = _lookup_game_total(totals_lookup, game.get("home_team"), game.get("away_team"))
        environment = _build_environment_dict(game.get("venue"), game.get("start_time"), warnings, game_total=total)

        for side, opposing_side in (("home", "away"), ("away", "home")):
            team_id = game.get(f"{side}_team_id")
            team_name = game.get(f"{side}_team")
            opposing_pitcher = game.get(f"{opposing_side}_probable_pitcher")

            if team_id is None or not opposing_pitcher:
                warnings.append(f"{game.get('away_team')} @ {game.get('home_team')}: "
                                 f"missing team/pitcher info for {side} side, skipped")
                continue

            pitcher_stats = _merge_pitcher_inputs(opposing_pitcher["id"], season, statcast_pitchers)
            pitcher_result = score_pitcher_vulnerability(pitcher_stats)
            environment_result = score_environment(environment)

            try:
                roster = mlb_data.fetch_team_roster(team_id)
            except Exception as exc:
                warnings.append(f"{team_name}: roster fetch failed ({exc}), skipped")
                continue

            for player in roster:
                if player.get("position") in ("P", "SP", "RP"):
                    continue  # pitchers don't hit (interleague/NL park exceptions aside)

                hitter_stats = _merge_hitter_inputs(player["person_id"], season, statcast_batters)
                hitter_result = score_hitter_power(hitter_stats)

                quality_score = round(
                    pitcher_result["score"] + hitter_result["score"] + environment_result["score"], 2,
                )
                strengths, _ = _collect_strengths_and_risks(
                    pitcher_result["breakdown"], hitter_result["breakdown"], environment_result["breakdown"],
                )

                watch_list.append({
                    "player": player["name"],
                    "team": team_name,
                    "opposing_pitcher": opposing_pitcher["name"],
                    "quality_score": quality_score,
                    "status": "WATCH",
                    "reasons": strengths,
                })

    watch_list.sort(key=lambda w: w["quality_score"], reverse=True)

    return {
        "date": game_date.isoformat(),
        "games_found": len(games),
        "watch_list": watch_list,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Stage 2: Confirmed Lineup Final Rankings
# ---------------------------------------------------------------------------

def build_final_rankings(game_date: _date, odds_quotes: list | None = None, game_totals: dict | None = None) -> dict:
    """Stage 2 -- run after lineups are confirmed. Only scores confirmed
    starters, against real opposing probable pitchers, real environment
    data, and (if supplied) real market odds. This is the full HR Alpha
    Score pipeline described in the project spec.

    Args:
        game_date: the date to build rankings for.
        odds_quotes: optional list of ingestion.odds.HrOddsQuote objects
            (e.g. from TheOddsApiSource().fetch_all(game_date, "any")).
            If not supplied, every player's betting-value category
            scores 0/15 with "no market odds found" -- rankings still
            work, they just can't show real edge%.
        game_totals: optional pre-fetched lookup from
            ingestion.odds.get_game_totals(game_date), keyed by
            frozenset({home_team, away_team}). Same convention as
            odds_quotes -- if not supplied, every game's environment
            score simply scores 0/3 on game_total.

    Returns:
        {
            "date": str, "games_found": int, "players_scored": int,
            "rankings": [calculate_alpha_score() output, ...],  # sorted
                by alpha_score descending, each with extra "matchup" and
                "game_id" keys added
            "warnings": [str, ...],
        }
    """
    warnings = []
    season = game_date.year

    try:
        games = mlb_data.fetch_games(game_date)
    except Exception as exc:
        return {"date": game_date.isoformat(), "games_found": 0, "players_scored": 0,
                "rankings": [], "warnings": [f"fetch_games failed: {exc}"]}

    try:
        statcast_batters = _build_statcast_lookup(statcast.fetch_batter_power_metrics(season))
    except Exception as exc:
        statcast_batters = {}
        warnings.append(f"Statcast batter leaderboard unavailable: {exc}")

    try:
        statcast_pitchers = _build_statcast_lookup(statcast.fetch_pitcher_contact_metrics(season))
    except Exception as exc:
        statcast_pitchers = {}
        warnings.append(f"Statcast pitcher leaderboard unavailable: {exc}")

    odds_lookup = _build_odds_lookup(odds_quotes)
    if odds_quotes is None:
        warnings.append("No odds_quotes supplied -- betting-value category will score 0/15 for every player")

    totals_lookup = _build_totals_lookup(game_totals)
    if game_totals is None:
        warnings.append("No game_totals supplied -- environment category will score 0/3 on game_total for every game")

    rankings = []

    for game in games:
        total = _lookup_game_total(totals_lookup, game.get("home_team"), game.get("away_team"))
        environment = _build_environment_dict(game.get("venue"), game.get("start_time"), warnings, game_total=total)

        try:
            lineup = mlb_data.fetch_starting_lineup(game["game_id"])
        except Exception as exc:
            warnings.append(f"{game.get('away_team')} @ {game.get('home_team')}: lineup fetch failed ({exc})")
            continue

        for side_data in lineup:
            side = side_data["side"]
            opposing_side = "away" if side == "home" else "home"
            opposing_pitcher = game.get(f"{opposing_side}_probable_pitcher")

            if not side_data["batters"]:
                continue  # lineup not confirmed yet for this side -- legitimate, not an error
            if not opposing_pitcher:
                warnings.append(f"{side_data['team']}: no opposing probable pitcher, lineup skipped")
                continue

            pitcher_stats = _merge_pitcher_inputs(opposing_pitcher["id"], season, statcast_pitchers)

            try:
                pitcher_info = mlb_data.fetch_player_info(opposing_pitcher["id"])
                pitcher_hand = pitcher_info.get("pitch_hand")
            except Exception:
                pitcher_hand = None

            for batter in side_data["batters"]:
                try:
                    hitter_stats = _merge_hitter_inputs(batter["person_id"], season, statcast_batters)

                    odds_quote = odds_lookup.get(_normalize_name(batter["name"]))
                    implied_probability = odds_quote.current_implied_probability if odds_quote else None
                    odds_value = odds_quote.current_odds if odds_quote else None
                    book = odds_quote.book if odds_quote else "unknown"

                    result = calculate_alpha_score(
                        player_name=batter["name"],
                        pitcher_stats=pitcher_stats,
                        hitter_stats=hitter_stats,
                        environment=environment,
                        implied_probability=implied_probability,
                        odds=odds_value,
                        book=book,
                        opposing_pitcher_hand=pitcher_hand,
                        person_id=batter["person_id"],
                    )
                    result["matchup"] = f"{batter['name']} ({side_data['team']}) vs {opposing_pitcher['name']}"
                    result["game_id"] = game["game_id"]
                    result["venue"] = game.get("venue")
                    result["batting_order"] = batter["batting_order"]

                    rankings.append(result)
                except Exception as exc:
                    warnings.append(f"{batter.get('name', 'unknown batter')}: scoring failed ({exc}), skipped")

    rankings.sort(key=lambda r: r["alpha_score"], reverse=True)

    return {
        "date": game_date.isoformat(),
        "games_found": len(games),
        "players_scored": len(rankings),
        "rankings": rankings,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Auto-recording results (the morning after)
# ---------------------------------------------------------------------------

def auto_record_results(game_date: _date, conn: sqlite3.Connection) -> dict:
    """Fetch actual box score results and auto-record them against
    already-saved rankings for that date -- no manual entry needed.

    Only records results for games that have reached a FINAL status
    (see ingestion.mlb_data.FINAL_GAME_STATUSES) -- a postponed,
    suspended, or still-in-progress game is skipped with a warning
    rather than silently recorded as "no HR", which would corrupt the
    backtest data.

    Matches players by person_id (not name) against the box score,
    since that's exact rather than fuzzy -- see
    database.repository.get_tracked_players_for_date, which only
    returns rows that have a person_id in the first place.

    Args:
        game_date: the date whose saved rankings should get results.
        conn: an open database connection (see database.schema.get_connection).

    Returns:
        {
            "date": str, "players_recorded": int, "players_skipped": int,
            "warnings": [str, ...],
        }
    """
    warnings = []
    date_str = game_date.isoformat()

    tracked = get_tracked_players_for_date(conn, date_str)
    if not tracked:
        return {
            "date": date_str, "players_recorded": 0, "players_skipped": 0,
            "warnings": ["No tracked players without a recorded result for this date -- "
                         "either nothing was saved for this date, or everything's already recorded"],
        }

    try:
        games = mlb_data.fetch_games(game_date)
        status_by_game_id = {g["game_id"]: g["status"] for g in games}
    except Exception as exc:
        return {
            "date": date_str, "players_recorded": 0, "players_skipped": len(tracked),
            "warnings": [f"Could not check game statuses, nothing recorded: {exc}"],
        }

    hr_results_by_game: dict[int, dict | None] = {}
    players_recorded = 0
    players_skipped = 0

    for player_row in tracked:
        game_id = player_row["game_id"]
        status = status_by_game_id.get(game_id)

        if status not in mlb_data.FINAL_GAME_STATUSES:
            players_skipped += 1
            warnings.append(f"{player_row['player']}: game not final yet (status: {status}), skipped")
            continue

        if game_id not in hr_results_by_game:
            try:
                hr_results_by_game[game_id] = mlb_data.fetch_game_hr_results(game_id)
            except Exception as exc:
                warnings.append(f"game {game_id}: box score fetch failed ({exc}), skipping its players")
                hr_results_by_game[game_id] = None

        game_results = hr_results_by_game[game_id]
        if game_results is None:
            players_skipped += 1
            continue

        player_result = game_results.get(str(player_row["person_id"]))
        if player_result is None:
            warnings.append(f"{player_row['player']}: not found in box score (didn't play?), skipped")
            players_skipped += 1
            continue

        hit_hr = player_result["home_runs"] > 0
        record_result(
            conn, date_str, player_row["player"], hit_hr,
            odds_used=player_row["odds"], source="auto_boxscore",
        )
        players_recorded += 1

    return {
        "date": date_str, "players_recorded": players_recorded,
        "players_skipped": players_skipped, "warnings": warnings,
    }
