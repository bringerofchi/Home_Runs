"""Browser dashboard for MLB HR Edge Lab.

Run this by double-clicking start_windows.bat (Windows) or
start_mac.command (Mac), or with `python webapp/app.py` from the
project root. A browser tab opens automatically at
http://127.0.0.1:5050 once the server is up.

Your Odds API key is saved to a local .env file in the project root
and is never sent anywhere except the-odds-api.com.
"""

import os
import sys
import threading
import webbrowser
from datetime import date

from dotenv import load_dotenv, set_key
from flask import Flask, jsonify, render_template, request

# Allow `from ingestion.odds import ...` when running this file directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.odds import TheOddsApiSource, fetch_odds_with_fallback, get_game_totals  # noqa: E402
from ingestion.mlb_data import fetch_games, fetch_player_info, fetch_starting_lineup, debug_bulk_stats_response  # noqa: E402
from ingestion.statcast import diagnose_batter_leaderboard, diagnose_pitcher_leaderboard  # noqa: E402
from reports.daily_report import build_watch_list, build_final_rankings, auto_record_results  # noqa: E402
from models.calibration import run_calibration_audit  # noqa: E402
from database.schema import get_connection  # noqa: E402
from database.repository import (  # noqa: E402
    save_daily_rankings, record_result, get_results_for_date, get_score_tier_summary,
    save_odds_cache, get_cached_odds, get_date_summary, get_category_breakdown_summary,
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

if not os.path.exists(ENV_PATH):
    open(ENV_PATH, "a").close()
load_dotenv(ENV_PATH)

app = Flask(__name__)


PROVIDER_KEY_NAMES = {
    "odds_api": "ODDS_API_KEY",
    "parlay_api": "PARLAY_API_KEY",
    "sharp_api": "SHARPAPI_KEY",
}


@app.route("/")
def index():
    has_keys = {
        provider: bool(os.environ.get(env_var))
        for provider, env_var in PROVIDER_KEY_NAMES.items()
    }
    return render_template("index.html", has_keys=has_keys)


@app.route("/api/config", methods=["POST"])
def save_config():
    body = request.get_json(silent=True) or {}
    provider = body.get("provider", "odds_api")
    api_key = (body.get("api_key") or "").strip()

    if provider not in PROVIDER_KEY_NAMES:
        return jsonify({"error": f"Unknown provider: {provider!r}"}), 400
    if not api_key:
        return jsonify({"error": "API key cannot be empty"}), 400

    env_var = PROVIDER_KEY_NAMES[provider]
    set_key(ENV_PATH, env_var, api_key)
    os.environ[env_var] = api_key
    return jsonify({"status": "saved", "provider": provider})


@app.route("/api/fetch-odds")
def fetch_odds():
    game_date_str = request.args.get("date")
    book = request.args.get("book", "fanduel")
    force_refresh = request.args.get("refresh") == "true"

    try:
        game_date = date.fromisoformat(game_date_str) if game_date_str else date.today()
    except ValueError:
        return jsonify({"error": f"Invalid date: {game_date_str!r}"}), 400

    date_str = game_date.isoformat()

    # Check the local cache first -- The Odds API bills by credits
    # (roughly 1 per game per call), so avoid re-fetching on every
    # repeat click. See database/schema.py's odds_cache docstring.
    conn = get_connection()
    try:
        if not force_refresh:
            cached = get_cached_odds(conn, date_str, book)
            if cached is not None:
                results = [
                    {
                        "player": q.player, "book": q.book, "odds": q.current_odds,
                        "implied_probability": round(q.current_implied_probability * 100, 1),
                    }
                    for q in cached["quotes"]
                ]
                results.sort(key=lambda r: r["implied_probability"], reverse=True)
                return jsonify({
                    "date": date_str, "book": book, "results": results,
                    "from_cache": True, "cache_age_minutes": cached["age_minutes"],
                    "api_quota_remaining": cached["quota_remaining"], "api_quota_used": cached["quota_used"],
                })

        try:
            quotes, fallback_diagnostics = fetch_odds_with_fallback(game_date, book)
        except Exception as exc:  # surface any unexpected error to the UI
            return jsonify({"error": str(exc)}), 500

        if fallback_diagnostics["provider_used"] is None:
            skipped_only = all(a["status"] == "skipped_no_key" for a in fallback_diagnostics["attempts"])
            if skipped_only:
                return jsonify({"error": "No API key saved yet -- add at least one provider key above"}), 400

        provider_diag = fallback_diagnostics.get("provider_diagnostics", {})
        save_odds_cache(
            conn, date_str, book, quotes,
            quota_remaining=provider_diag.get("api_quota_remaining"),
            quota_used=provider_diag.get("api_quota_used"),
        )
    finally:
        conn.close()

    diagnostics = provider_diag

    results = [
        {
            "player": q.player,
            "book": q.book,
            "odds": q.current_odds,
            "implied_probability": round(q.current_implied_probability * 100, 1),
        }
        for q in quotes
    ]
    results.sort(key=lambda r: r["implied_probability"], reverse=True)

    return jsonify({
        "date": game_date.isoformat(),
        "book": book,
        "results": results,
        "diagnostics": diagnostics,
        "provider_used": fallback_diagnostics["provider_used"],
        "fallback_attempts": fallback_diagnostics["attempts"],
        "from_cache": False,
    })


@app.route("/api/debug-markets")
def debug_markets():
    event_id = request.args.get("event_id")
    book = request.args.get("book")
    if not event_id:
        return jsonify({"error": "event_id is required"}), 400

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return jsonify({"error": "No API key saved yet -- add one above first"}), 400

    try:
        source = TheOddsApiSource(api_key=api_key)
        raw = source.list_event_markets(event_id, book=book if book and book != "any" else None)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(raw)


@app.route("/api/debug-odds-api")
def debug_odds_api():
    """Whole-slate diagnostic for The Odds API specifically, bypassing
    fetch_odds_with_fallback() entirely.

    Why this exists: when fetch_odds_with_fallback() falls through The
    Odds API to a backup provider, it only records
    {"provider": "odds_api", "status": "empty"} (or "error") in
    fallback_attempts -- the rich per-game diagnostics that
    TheOddsApiSource.fetch_all() actually builds (games_found, and for
    each game: books_with_hr_market, requested_book_has_hr_market,
    book_used) are computed but thrown away, since fetch_odds_with_fallback
    only returns provider_diagnostics for whichever provider succeeds.

    Use this to answer "why did odds_api come back empty/error" without
    guessing -- it shows, per game, whether ANY book has posted the HR
    market yet vs. specifically whether FanDuel has, plus quota usage.
    Equivalent to calling /api/debug-markets once per event_id, but for
    the whole day's slate in one call.
    """
    date_str = request.args.get("date")
    book = request.args.get("book", "fanduel")

    try:
        game_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str!r}"}), 400

    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        return jsonify({"error": "No Odds API key saved yet -- add one above first"}), 400

    try:
        source = TheOddsApiSource(api_key=api_key)
        quotes, diagnostics = source.fetch_all(game_date, book=book)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({
        "date": game_date.isoformat(),
        "book": book,
        "quotes_found": len(quotes),
        "diagnostics": diagnostics,
    })


@app.route("/api/validate-mlb-data")
def validate_mlb_data():
    """Live-validation diagnostics for the MLB Stats API pipeline: for
    each game on a date, checks that probable pitchers are present and
    resolve to a valid handedness, and reports whether a starting lineup
    has been confirmed yet. Every per-player lookup is wrapped so one
    bad player doesn't kill the whole report -- failures show up as
    warnings on that specific game instead.
    """
    date_str = request.args.get("date")
    try:
        game_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str!r}"}), 400

    try:
        games = fetch_games(game_date)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    results = []
    for g in games:
        game_diag = {
            "matchup": f"{g['away_team']} @ {g['home_team']}",
            "game_id": g["game_id"],
            "venue": g["venue"],
            "status": g["status"],
            "probable_pitchers": {},
            "lineup_status": None,
            "lineup": None,
            "warnings": [],
        }

        for side in ("home", "away"):
            pitcher = g.get(f"{side}_probable_pitcher")
            if not pitcher:
                game_diag["probable_pitchers"][side] = None
                game_diag["warnings"].append(f"{side}: no probable pitcher listed yet")
                continue

            entry = {"id": pitcher["id"], "name": pitcher["name"], "hand": None}
            try:
                info = fetch_player_info(pitcher["id"])
                entry["hand"] = info.get("pitch_hand")
                if not entry["hand"]:
                    game_diag["warnings"].append(
                        f"{side} pitcher {pitcher['name']}: pitch_hand field missing"
                    )
            except Exception as exc:
                entry["error"] = str(exc)
                game_diag["warnings"].append(f"{side} pitcher lookup failed: {exc}")
            game_diag["probable_pitchers"][side] = entry

        try:
            lineup = fetch_starting_lineup(g["game_id"])
            total_batters = sum(len(side_data["batters"]) for side_data in lineup)
            if total_batters >= 18:
                game_diag["lineup_status"] = "confirmed"
            elif total_batters > 0:
                game_diag["lineup_status"] = "partial"
                game_diag["warnings"].append("only one side's lineup posted so far")
            else:
                game_diag["lineup_status"] = "not_yet_posted"
            game_diag["lineup"] = lineup
        except Exception as exc:
            game_diag["lineup_status"] = "error"
            game_diag["warnings"].append(f"lineup fetch failed: {exc}")

        results.append(game_diag)

    return jsonify({"date": game_date.isoformat(), "games_found": len(results), "games": results})


@app.route("/api/validate-statcast")
def validate_statcast():
    """Live-validation diagnostics for the Statcast/Baseball Savant
    pipeline -- the highest-risk ingestion module in the project, since
    Savant has no official API contract. See
    ingestion.statcast.diagnose_leaderboard for what's actually checked.
    """
    player_type = request.args.get("type", "batter")
    if player_type not in ("batter", "pitcher"):
        return jsonify({"error": "type must be 'batter' or 'pitcher'"}), 400

    season_str = request.args.get("season")
    try:
        season = int(season_str) if season_str else date.today().year
    except ValueError:
        return jsonify({"error": f"Invalid season: {season_str!r}"}), 400

    try:
        if player_type == "pitcher":
            result = diagnose_pitcher_leaderboard(season)
        else:
            result = diagnose_batter_leaderboard(season)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


def _summarize_totals_diagnostics(totals_diag: dict) -> str | None:
    """Build a one-line warning from get_game_totals()'s diagnostics,
    matching the style already used for odds_warning below (distinguish
    "nobody configured" from "everybody failed/came back empty").
    Returns None if a provider succeeded (no warning needed).
    """
    if totals_diag.get("provider_used") is not None:
        return None
    attempts = totals_diag.get("attempts", [])
    if not attempts:
        return "No game totals available"
    if all(a["status"] == "skipped_no_key" for a in attempts):
        return "No odds provider key saved -- continuing without game totals"
    return "All configured game-totals providers failed or returned no totals -- continuing without them"


@app.route("/api/daily-report")
def daily_report():
    """The end-to-end pipeline: Stage 1 watch list or Stage 2 final
    rankings, per the project spec's two-stage daily workflow.
    """
    date_str = request.args.get("date")
    stage = request.args.get("stage", "final")

    try:
        game_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str!r}"}), 400

    if stage == "watch":
        watch_book = request.args.get("book", "fanduel")
        game_totals = None
        totals_warning = None
        try:
            game_totals, totals_diag = get_game_totals(game_date, book=watch_book)
            totals_warning = _summarize_totals_diagnostics(totals_diag)
        except Exception as exc:
            game_totals = None
            totals_warning = f"Game totals fetch failed, continuing without them: {exc}"

        try:
            result = build_watch_list(game_date, game_totals=game_totals)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        if totals_warning:
            result.setdefault("warnings", []).append(totals_warning)
        return jsonify(result)

    if stage != "final":
        return jsonify({"error": "stage must be 'watch' or 'final'"}), 400

    force_refresh = request.args.get("refresh") == "true"
    date_str = game_date.isoformat()
    book = request.args.get("book", "fanduel")

    conn = get_connection()
    try:
        # Stage 2: try to pull real odds if a key is saved, but degrade
        # gracefully (rankings without edge%) rather than failing the
        # whole report if odds aren't available. Cache-first to avoid
        # re-burning API quota on every report regeneration -- see
        # database/schema.py's odds_cache docstring.
        odds_quotes = None
        odds_warning = None
        used_cache = False
        odds_provider_used = None
        odds_fallback_attempts = []

        if not force_refresh:
            cached = get_cached_odds(conn, date_str, book)
            if cached is not None:
                odds_quotes = cached["quotes"]
                used_cache = True
                odds_provider_used = "cached"
                odds_warning = (
                    f"Using cached odds from {cached['age_minutes']} min ago "
                    f"(quota: {cached['quota_used']} used, {cached['quota_remaining']} remaining). "
                    f"Add &refresh=true to force a live re-fetch."
                )

        if odds_quotes is None:
            try:
                odds_quotes, fallback_diag = fetch_odds_with_fallback(game_date, book=book)
            except Exception as exc:
                odds_quotes = None
                odds_warning = f"Odds fetch failed, continuing without market odds: {exc}"
            else:
                odds_fallback_attempts = fallback_diag["attempts"]
                provider_used = fallback_diag["provider_used"]
                odds_provider_used = provider_used
                if provider_used is not None:
                    provider_diag = fallback_diag.get("provider_diagnostics", {})
                    save_odds_cache(
                        conn, date_str, book, odds_quotes,
                        quota_remaining=provider_diag.get("api_quota_remaining"),
                        quota_used=provider_diag.get("api_quota_used"),
                    )
                    fell_back = fallback_diag["attempts"][0]["provider"] != provider_used
                    if fell_back:
                        odds_warning = (
                            f"Primary odds provider unavailable -- used backup provider "
                            f"'{provider_used}' instead. See fallback_attempts for details."
                        )
                else:
                    skipped_only = all(a["status"] == "skipped_no_key" for a in fallback_diag["attempts"])
                    if skipped_only:
                        odds_warning = "No odds provider key saved -- continuing without market odds"
                    else:
                        odds_warning = (
                            "All configured odds providers failed or returned no odds -- "
                            "continuing without market odds. See fallback_attempts for details."
                        )

        # Game totals -- separate market from the HR player-prop odds
        # above, always fetched live (NOT cached yet -- odds_cache /
        # get_cached_odds are HrOddsQuote-specific today; caching totals
        # the same way is a real follow-up, not built here). Degrades
        # gracefully to no totals rather than failing the report.
        game_totals = None
        totals_warning = None
        try:
            game_totals, totals_diag = get_game_totals(game_date, book=book)
            totals_warning = _summarize_totals_diagnostics(totals_diag)
        except Exception as exc:
            game_totals = None
            totals_warning = f"Game totals fetch failed, continuing without them: {exc}"

        try:
            result = build_final_rankings(game_date, odds_quotes=odds_quotes, game_totals=game_totals)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        if totals_warning:
            result.setdefault("warnings", []).append(totals_warning)

        if odds_warning:
            result["warnings"].append(odds_warning)
        result["odds_from_cache"] = used_cache
        result["odds_provider_used"] = odds_provider_used
        result["odds_fallback_attempts"] = odds_fallback_attempts

        # Persist to the historical database so it's available for later
        # backtesting -- see database/repository.py.
        try:
            save_daily_rankings(conn, result)
        except Exception as exc:
            result["warnings"].append(f"Could not save to historical database: {exc}")
    finally:
        conn.close()

    return jsonify(result)


@app.route("/api/results-for-date")
def results_for_date_route():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400

    conn = get_connection()
    try:
        rows = get_results_for_date(conn, date_str)
    finally:
        conn.close()

    return jsonify({"date": date_str, "results": rows})


@app.route("/api/auto-record-results", methods=["POST"])
def auto_record_results_route():
    """Fetch real box score results and auto-record them against saved
    rankings -- no manual entry needed. See
    reports.daily_report.auto_record_results for the details (only
    records for games that have actually reached a final status).
    """
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date is required"}), 400

    try:
        game_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": f"Invalid date: {date_str!r}"}), 400

    conn = get_connection()
    try:
        result = auto_record_results(game_date, conn)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()

    return jsonify(result)


@app.route("/api/record-result", methods=["POST"])
def record_result_route():
    """Manual fallback -- only needed if a player can't be auto-matched
    (e.g. missing person_id from an older saved report). Prefer
    /api/auto-record-results for normal use.
    """
    body = request.get_json(silent=True) or {}
    date_str = body.get("date")
    player = body.get("player")
    hit_hr = body.get("hit_hr")
    odds_used = body.get("odds_used")
    closing_odds = body.get("closing_odds")

    if not date_str or not player or hit_hr is None:
        return jsonify({"error": "date, player, and hit_hr are required"}), 400

    conn = get_connection()
    try:
        record_result(conn, date_str, player, bool(hit_hr), odds_used, closing_odds)
    finally:
        conn.close()

    return jsonify({"status": "saved"})


@app.route("/api/backtest-summary")
def backtest_summary_route():
    conn = get_connection()
    try:
        summary = get_score_tier_summary(conn)
    finally:
        conn.close()

    return jsonify({"tiers": summary})


@app.route("/api/date-summary")
def date_summary_route():
    conn = get_connection()
    try:
        summary = get_date_summary(conn)
    finally:
        conn.close()

    return jsonify({"dates": summary})


@app.route("/api/breakdown-diagnostic")
def breakdown_diagnostic_route():
    """Empirical diagnostic: which sub-metrics are structurally
    unreachable (no data ingested for them yet) vs which have data but
    are scoring low/high. See database.repository.get_category_breakdown_summary.
    """
    conn = get_connection()
    try:
        summary = get_category_breakdown_summary(conn)
    finally:
        conn.close()

    return jsonify(summary)


@app.route("/api/calibration-audit")
def calibration_audit_route():
    """Audits every hardcoded benchmark range in models/pitcher_model.py
    and models/hitter_model.py against real live league distributions.
    See models.calibration for the full explanation -- this is what
    caught FLY_BALL_PCT_RANGE being miscalibrated in the first place.
    """
    season_str = request.args.get("season")
    try:
        season = int(season_str) if season_str else date.today().year
    except ValueError:
        return jsonify({"error": f"Invalid season: {season_str!r}"}), 400

    try:
        min_ip = float(request.args.get("min_ip", 0) or 0)
        min_pa = int(request.args.get("min_pa", 0) or 0)
    except ValueError:
        return jsonify({"error": "min_ip and min_pa must be numeric"}), 400

    try:
        result = run_calibration_audit(season, min_innings_pitched=min_ip, min_plate_appearances=min_pa)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


@app.route("/api/debug-bulk-stats")
def debug_bulk_stats_route():
    """Raw-response inspector for MLB Stats API's bulk /stats endpoint --
    see ingestion.mlb_data.debug_bulk_stats_response for what it checks.
    Use this to investigate row-count discrepancies (e.g. the ISO
    sample-size gap found by the calibration audit) before assuming
    where the drop is happening.
    """
    group = request.args.get("group", "hitting")
    if group not in ("hitting", "pitching"):
        return jsonify({"error": "group must be 'hitting' or 'pitching'"}), 400

    season_str = request.args.get("season")
    try:
        season = int(season_str) if season_str else date.today().year
    except ValueError:
        return jsonify({"error": f"Invalid season: {season_str!r}"}), 400

    # Optional hypothesis-testing param, e.g. extra_params=playerPool=ALL
    # -- see ingestion.mlb_data.debug_bulk_stats_response's docstring.
    extra_params = None
    extra_params_str = request.args.get("extra_params", "").strip()
    if extra_params_str:
        if "=" not in extra_params_str:
            return jsonify({"error": "extra_params must be in key=value format, e.g. playerPool=ALL"}), 400
        key, _, value = extra_params_str.partition("=")
        extra_params = {key.strip(): value.strip()}

    try:
        result = debug_bulk_stats_response(group, season, extra_params=extra_params)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(result)


def _open_browser():
    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    threading.Timer(1.2, _open_browser).start()
    app.run(host="127.0.0.1", port=5050, debug=False)
