"""Read/write logic for the historical tracking database. All raw SQL
lives here -- callers elsewhere in the codebase never write SQL inline
(see database/schema.py's module docstring).

Covers:
    save_daily_rankings(conn, report)  -- persist a build_final_rankings()
        result, one row per player
    record_result(conn, ...)            -- log whether a player actually
        hit a HR after the fact
    get_date_summary(conn)              -- overview across every date with
        saved data: players saved, results recorded, HRs hit
    get_score_tier_summary(conn)        -- THE Phase 5 validation query:
        hit rate and ROI by Alpha Score tier, across all recorded results
    get_cached_odds / save_odds_cache   -- API-quota-conserving odds cache
        (see database/schema.py's odds_cache table docstring for why)
    get_category_breakdown_summary(conn) -- empirical diagnostic: for
        every sub-metric across all 4 scoring categories, what fraction
        of saved rows actually had data for it, and how many points it
        captured on average. Answers "why is nobody scoring above ~45"
        with real numbers instead of assumptions.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from ingestion.odds import american_odds_to_profit_multiple, american_odds_to_implied_probability, HrOddsQuote

# Alpha Score tiers for backtesting, matching the project spec's own
# framing ("expected hit rate by Alpha Score tier", "does a 90+ score
# outperform lower scores?").
SCORE_TIERS = [
    (0, 59, "0-59"),
    (60, 69, "60-69"),
    (70, 79, "70-79"),
    (80, 89, "80-89"),
    (90, 100, "90-100"),
]


def save_daily_rankings(conn: sqlite3.Connection, report: dict) -> int:
    """Persist a reports.daily_report.build_final_rankings() result, one
    row per player. Uses INSERT OR REPLACE keyed on (date, player) --
    re-running the report for the same day overwrites that day's
    snapshot rather than duplicating rows.

    Returns the number of rows written.
    """
    rows_written = 0
    for r in report.get("rankings", []):
        conn.execute(
            """
            INSERT OR REPLACE INTO alpha_scores
                (date, player, person_id, game_id, venue, matchup, alpha_score,
                 model_probability, implied_probability, edge_pct,
                 expected_value, odds, book, category_breakdown_json,
                 strengths_json, risks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["date"], r["player"], r.get("person_id"), r.get("game_id"), r.get("venue"), r.get("matchup"),
                r["alpha_score"], r["model_probability"], r["implied_probability"], r["edge_pct"],
                r["expected_value"], r["odds"], r["book"],
                json.dumps(r["category_breakdown"]), json.dumps(r["strengths"]), json.dumps(r["risks"]),
            ),
        )
        rows_written += 1
    conn.commit()
    return rows_written


def record_result(
    conn: sqlite3.Connection, date: str, player: str, hit_hr: bool,
    odds_used: int | None = None, closing_odds: int | None = None,
    source: str = "manual",
) -> None:
    """Log whether a player actually hit a HR, after the fact.

    profit_loss is computed for a flat $1 stake at odds_used (None if
    odds_used wasn't provided -- can't compute P/L without knowing the
    price). closing_line_value compares odds_used's implied probability
    against closing_odds's, if both are given -- positive means you got
    a better number than where the market closed (a good sign your
    timing/process has value independent of whether this specific bet won).

    source: "auto_boxscore" (fetched automatically from the MLB Stats
        API the morning after) or "manual" (typed in). Purely for
        auditability -- doesn't affect any calculation.
    """
    profit_loss = None
    if odds_used is not None:
        if hit_hr:
            profit_loss = american_odds_to_profit_multiple(odds_used)
        else:
            profit_loss = -1.0

    closing_line_value = None
    if odds_used is not None and closing_odds is not None:
        closing_line_value = round(
            american_odds_to_implied_probability(closing_odds)
            - american_odds_to_implied_probability(odds_used), 4,
        )

    conn.execute(
        """
        INSERT OR REPLACE INTO results
            (date, player, hit_hr, odds_used, profit_loss, closing_odds, closing_line_value, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (date, player, int(hit_hr), odds_used, profit_loss, closing_odds, closing_line_value, source),
    )
    conn.commit()


def get_tracked_players_for_date(conn: sqlite3.Connection, date: str) -> list[dict]:
    """All players saved (via save_daily_rankings) for a date, excluding
    ones that already have a recorded result -- i.e. exactly the set
    that still needs a result. Used by the auto-fetch-results flow.

    Returns rows with person_id, player, game_id, odds -- player rows
    with no person_id (shouldn't normally happen from a real report, but
    possible from old data pre-dating the person_id column) are excluded
    since there'd be nothing reliable to match against box score data.
    """
    rows = conn.execute(
        """
        SELECT a.player, a.person_id, a.game_id, a.odds
        FROM alpha_scores a
        LEFT JOIN results r ON a.date = r.date AND a.player = r.player
        WHERE a.date = ? AND r.id IS NULL AND a.person_id IS NOT NULL
        """,
        (date,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_results_for_date(conn: sqlite3.Connection, date: str) -> list[dict]:
    """All alpha_scores rows for a date, left-joined with results (if
    recorded yet). Useful for reviewing a specific day's card against
    what actually happened.
    """
    rows = conn.execute(
        """
        SELECT a.*, r.hit_hr, r.profit_loss, r.closing_line_value
        FROM alpha_scores a
        LEFT JOIN results r ON a.date = r.date AND a.player = r.player
        WHERE a.date = ?
        ORDER BY a.alpha_score DESC
        """,
        (date,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_date_summary(conn: sqlite3.Connection) -> list[dict]:
    """Overview across every date with saved data -- the first thing
    worth looking at once you've been running the dashboard for a while:
    which days have data, how much, and how much of it has results
    recorded yet.

    Returns one row per date, most recent first:
        {
            "date": str,
            "players_saved": int,       # distinct players scored that day
            "players_with_results": int, # how many have a recorded outcome
            "hrs_hit": int,               # how many of those actually homered
            "avg_alpha_score": float,
            "top_alpha_score": float,
        }
    """
    rows = conn.execute(
        """
        SELECT
            a.date,
            COUNT(DISTINCT a.player) AS players_saved,
            COUNT(DISTINCT r.player) AS players_with_results,
            COALESCE(SUM(r.hit_hr), 0) AS hrs_hit,
            AVG(a.alpha_score) AS avg_alpha_score,
            MAX(a.alpha_score) AS top_alpha_score
        FROM alpha_scores a
        LEFT JOIN results r ON a.date = r.date AND a.player = r.player
        GROUP BY a.date
        ORDER BY a.date DESC
        """
    ).fetchall()

    return [
        {
            "date": row["date"],
            "players_saved": row["players_saved"],
            "players_with_results": row["players_with_results"],
            "hrs_hit": row["hrs_hit"],
            "avg_alpha_score": round(row["avg_alpha_score"], 1) if row["avg_alpha_score"] is not None else None,
            "top_alpha_score": row["top_alpha_score"],
        }
        for row in rows
    ]


def get_score_tier_summary(conn: sqlite3.Connection) -> list[dict]:
    """THE Phase 5 validation query: across every result ever recorded,
    what's the actual hit rate and ROI by Alpha Score tier?

    This is the honest answer to whether the scoring system's weights
    (currently the placeholder 35/25/25/15 split -- see models/alpha_score.py)
    are doing anything real. Needs a meaningful sample size per tier
    before the numbers mean much -- sample_count is included precisely
    so small-sample tiers aren't over-interpreted.

    Returns one dict per tier:
        {
            "tier": "90-100", "sample_count": int,
            "hit_rate": float | None,        # fraction that hit a HR
            "avg_alpha_score": float | None,
            "avg_edge_pct": float | None,     # avg edge at time of recommendation
            "roi": float | None,               # avg profit_loss per $1 staked
        }
    """
    summary = []
    for low, high, label in SCORE_TIERS:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as sample_count,
                AVG(r.hit_hr) as hit_rate,
                AVG(a.alpha_score) as avg_alpha_score,
                AVG(a.edge_pct) as avg_edge_pct,
                AVG(r.profit_loss) as roi
            FROM alpha_scores a
            JOIN results r ON a.date = r.date AND a.player = r.player
            WHERE a.alpha_score >= ? AND a.alpha_score <= ?
            """,
            (low, high),
        ).fetchone()

        summary.append({
            "tier": label,
            "sample_count": row["sample_count"],
            "hit_rate": round(row["hit_rate"], 4) if row["hit_rate"] is not None else None,
            "avg_alpha_score": round(row["avg_alpha_score"], 2) if row["avg_alpha_score"] is not None else None,
            "avg_edge_pct": round(row["avg_edge_pct"], 2) if row["avg_edge_pct"] is not None else None,
            "roi": round(row["roi"], 4) if row["roi"] is not None else None,
        })

    return summary


# ---------------------------------------------------------------------------
# Odds cache -- conserves The Odds API quota (see schema.py's odds_cache
# table docstring for the credit math on why this matters)
# ---------------------------------------------------------------------------

def save_odds_cache(
    conn: sqlite3.Connection, date: str, book: str, quotes: list[HrOddsQuote],
    quota_remaining: int | None = None, quota_used: int | None = None,
) -> None:
    """Cache a day's odds fetch. Stores just player/book/current_odds --
    enough to reconstruct usable HrOddsQuote objects later (opening_odds
    and closing_odds aren't tracked by this cache, since its whole
    purpose is short-lived reuse within the same session, not historical
    odds movement -- that's what the alpha_scores table's own "odds"
    column already captures once a report is generated).
    """
    quotes_data = [{"player": q.player, "book": q.book, "current_odds": q.current_odds} for q in quotes]
    conn.execute(
        """
        INSERT OR REPLACE INTO odds_cache (date, book, quotes_json, quota_remaining, quota_used, fetched_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        """,
        (date, book, json.dumps(quotes_data), quota_remaining, quota_used),
    )
    conn.commit()


def get_cached_odds(conn: sqlite3.Connection, date: str, book: str, max_age_minutes: int = 30) -> dict | None:
    """Return a cached odds fetch if one exists and is fresh enough,
    else None (meaning: go fetch live).

    Returns:
        {
            "quotes": [HrOddsQuote, ...],
            "fetched_at": str, "age_minutes": float,
            "quota_remaining": int | None, "quota_used": int | None,
        }
        or None if there's no cache entry, or it's older than max_age_minutes.
    """
    row = conn.execute(
        "SELECT * FROM odds_cache WHERE date = ? AND book = ?", (date, book),
    ).fetchone()
    if row is None:
        return None

    fetched_at = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return None

    quotes_data = json.loads(row["quotes_json"])
    quotes = [
        HrOddsQuote(
            player=q["player"], book=q["book"],
            opening_odds=q["current_odds"], current_odds=q["current_odds"], closing_odds=None,
        )
        for q in quotes_data
    ]

    return {
        "quotes": quotes,
        "fetched_at": row["fetched_at"],
        "age_minutes": round(age_minutes, 1),
        "quota_remaining": row["quota_remaining"],
        "quota_used": row["quota_used"],
    }


# ---------------------------------------------------------------------------
# Category breakdown diagnostic -- answers "why is nobody scoring above
# ~45" empirically, using real saved data instead of assumptions.
# ---------------------------------------------------------------------------

def get_category_breakdown_summary(conn: sqlite3.Connection) -> dict:
    """Aggregate every sub-metric's contribution across all saved
    alpha_scores rows, parsed from category_breakdown_json.

    For each sub-metric (e.g. "hr9", "park_hr_factor", "edge"), reports:
        - avg_points: mean points captured across all rows
        - avg_pct_of_max: avg_points as a % of that sub-metric's max_points
        - pct_rows_with_data: % of rows where raw data existed at all for
          this sub-metric (raw is not None). This is THE number that
          distinguishes "structurally unreachable because we don't
          ingest this yet" (near 0%) from "data exists, it's just
          scoring low/high on average" (near 100%, but avg_pct_of_max
          tells the real story).

    Sub-metrics with max_points == 0 (e.g. pitch_type_weakness,
    recent_form -- explicitly not-implemented, not a data gap) are
    included for completeness but avg_pct_of_max is None for them since
    the percentage is meaningless when there's no max to divide by.

    Returns:
        {
            "sample_count": int,  # alpha_scores rows examined
            "categories": {
                "pitcher_vulnerability": [
                    {"sub_metric": "hr9", "max_points": 12,
                     "avg_points": 7.4, "avg_pct_of_max": 61.7,
                     "pct_rows_with_data": 98.5},
                    ...
                ],
                "hitter_power": [...], "environment": [...], "betting_value": [...],
            },
        }
    """
    rows = conn.execute(
        "SELECT category_breakdown_json FROM alpha_scores WHERE category_breakdown_json IS NOT NULL"
    ).fetchall()

    # accumulator[category][sub_metric] = {"points_sum", "max_points", "data_count", "row_count"}
    accumulator: dict[str, dict[str, dict]] = {}
    sample_count = 0

    for row in rows:
        try:
            breakdown = json.loads(row["category_breakdown_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        sample_count += 1

        for category_name, category_result in breakdown.items():
            sub_breakdown = category_result.get("breakdown", {})
            cat_acc = accumulator.setdefault(category_name, {})

            for sub_metric, entry in sub_breakdown.items():
                acc = cat_acc.setdefault(sub_metric, {
                    "points_sum": 0.0,
                    "max_points": entry.get("max_points", 0),
                    "data_count": 0,
                    "row_count": 0,
                })
                acc["row_count"] += 1
                acc["points_sum"] += entry.get("points", 0) or 0
                if entry.get("raw") is not None:
                    acc["data_count"] += 1

    categories = {}
    for category_name, subs in accumulator.items():
        sub_list = []
        for sub_metric, acc in subs.items():
            max_points = acc["max_points"]
            row_count = acc["row_count"]
            avg_points = acc["points_sum"] / row_count if row_count else 0.0
            avg_pct_of_max = round(avg_points / max_points * 100, 1) if max_points else None
            pct_rows_with_data = round(acc["data_count"] / row_count * 100, 1) if row_count else 0.0

            sub_list.append({
                "sub_metric": sub_metric,
                "max_points": max_points,
                "avg_points": round(avg_points, 2),
                "avg_pct_of_max": avg_pct_of_max,
                "pct_rows_with_data": pct_rows_with_data,
            })

        # Surface the most diagnostically interesting rows first: no
        # data at all, then lowest-scoring.
        sub_list.sort(key=lambda s: (s["pct_rows_with_data"], s["avg_pct_of_max"] if s["avg_pct_of_max"] is not None else 999))
        categories[category_name] = sub_list

    return {"sample_count": sample_count, "categories": categories}
