"""SQLite schema for the standalone HR Edge Lab historical store.

Uses its own database file (default: database/hr_edge_lab.db) -- never
touches any existing MLB Edge Lab database. Plain sqlite3 (stdlib), not
an ORM -- keeps this transparent and dependency-free, consistent with
the rest of the project's "no black box" philosophy.

Scoped to the two tables that actually answer the project's Phase 5
validation question ("do Alpha Scores correlate with real HR results?"):

    alpha_scores -- one row per player per day, snapshotting the full
        HR Alpha Score output from models.alpha_score.calculate_alpha_score
        (via reports.daily_report.build_final_rankings) at the time it
        was generated. category_breakdown/strengths/risks are stored as
        JSON text -- SQLite has no native nested-object type, and this
        keeps the raw scoring detail intact for later analysis without
        needing a dozen more columns.

    results -- one row per player per day, recorded AFTER the game to
        say whether they actually hit a home run. Joined against
        alpha_scores by (date, player) to compute hit rate / ROI by
        Alpha Score tier -- see database/repository.py.

    odds_cache -- caches a day's odds fetch (keyed by date + book) so
        repeated dashboard clicks (re-running a report, re-fetching odds
        while debugging, etc.) don't re-burn API quota. The Odds API's
        free tier is credit-metered (markets x regions per call, ~1
        credit per game for our HR-prop pulls) -- a full MLB slate is
        ~15 credits per pull, so uncached repeat clicks exhaust a
        500-credit/month free tier fast. See database/repository.py's
        get_cached_odds / save_odds_cache.

NOT included yet (future expansion, not needed for the core backtesting
question above): raw games/pitchers/hitters snapshot tables. Actual
read/write logic lives in database/repository.py, not here -- this file
only defines schema + connection setup, so callers never write raw SQL
inline elsewhere in the codebase.
"""

from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "hr_edge_lab.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS alpha_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    player TEXT NOT NULL,
    person_id INTEGER,
    game_id INTEGER,
    venue TEXT,
    matchup TEXT,
    alpha_score REAL NOT NULL,
    model_probability REAL,
    implied_probability REAL,
    edge_pct REAL,
    expected_value REAL,
    odds INTEGER,
    book TEXT,
    category_breakdown_json TEXT,
    strengths_json TEXT,
    risks_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, player)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    player TEXT NOT NULL,
    hit_hr INTEGER NOT NULL,          -- 0 or 1
    odds_used INTEGER,                 -- the American odds a $1 bet would have used
    profit_loss REAL,                  -- for a flat $1 stake at odds_used
    closing_odds INTEGER,
    closing_line_value REAL,           -- implied_prob(odds_used) - implied_prob(closing_odds)
                                        -- positive = got a better number than the closing line
    source TEXT NOT NULL DEFAULT 'manual',  -- 'auto_boxscore' or 'manual'
    recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, player)
);

CREATE TABLE IF NOT EXISTS odds_cache (
    date TEXT NOT NULL,
    book TEXT NOT NULL,
    quotes_json TEXT NOT NULL,          -- serialized list of HrOddsQuote-shaped dicts
    quota_remaining INTEGER,
    quota_used INTEGER,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (date, book)
);

CREATE INDEX IF NOT EXISTS idx_alpha_scores_date ON alpha_scores(date);
CREATE INDEX IF NOT EXISTS idx_results_date ON results(date);
"""

# Backward-compatible migrations for databases created before a column
# existed. Each is idempotent (guarded by try/except on the "duplicate
# column" error) so get_connection() stays safe to call every time.
_MIGRATIONS = [
    "ALTER TABLE alpha_scores ADD COLUMN person_id INTEGER",
    "ALTER TABLE results ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
]


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the HR Edge Lab SQLite database and
    ensure the schema exists. Safe to call every time -- CREATE TABLE
    IF NOT EXISTS is idempotent, and _MIGRATIONS are individually
    guarded against re-applying to an already-migrated database.
    """
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    for migration_sql in _MIGRATIONS:
        try:
            conn.execute(migration_sql)
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise  # a real problem, don't swallow it

    return conn
