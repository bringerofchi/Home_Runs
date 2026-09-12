# MLB HR Edge Lab

A **standalone** MLB Home Run Edge analysis application.

## Purpose

Identify MLB hitters whose true home run probability is higher than the
probability implied by sportsbook odds — i.e. find betting edge, not
guaranteed outcomes.

## Independence

This project is completely independent from the existing **MLB Edge Lab**
project. It does not modify, merge, read from, or write to:

- Existing MLB Edge Lab code
- Existing databases
- Existing APIs
- Existing scoring systems
- Existing reports

If this project proves out, it may later **export** data for MLB Edge Lab
to consume — but that is a one-way, opt-in integration point, not a
dependency.

## Project Structure

```
MLB-HR-Edge-Lab/
├── data/
│   ├── raw/            # Unprocessed daily pulls (games, players, odds, weather)
│   └── processed/       # Cleaned/joined data ready for modeling
├── database/            # DB schema, connection, and models (SQLite to start)
├── ingestion/
│   ├── mlb_data.py      # Game/pitcher/hitter stats from MLB sources
│   ├── weather.py       # Park + weather conditions
│   └── odds.py          # FanDuel HR odds -> implied probability
├── models/
│   ├── pitcher_model.py # Pitcher HR vulnerability scoring
│   ├── hitter_model.py  # Hitter power profile scoring
│   └── alpha_score.py   # Combines pitcher + hitter + environment + odds -> 0-100 HR Alpha Score
├── reports/
│   └── daily_report.py  # Pre-lineup watch list + confirmed lineup rankings
├── api/                  # Optional API layer for serving results
├── tests/                # Unit tests for each module
└── README.md
```

## Workflow

1. **Stage 1 — Pre-Lineup Analysis**: run before official lineups post.
   Produces an HR Watch List (non-final).
2. **Stage 2 — Confirmed Lineup Analysis**: after lineups are released.
   Updates starters/order/PA projections and produces final rankings.

## HR Alpha Score (0–100, fully transparent)

| Category               | Points |
|-------------------------|-------|
| Pitcher Vulnerability   | 35    |
| Hitter Power Profile    | 25    |
| Environment             | 25    |
| Betting Value           | 15    |

Every score is broken down into its category contributions plus a list of
strengths/risks — never a black box.

## Status

Project scaffold only. Modules are stubbed with clear responsibilities and
TODOs. No live data collection has been implemented yet.

## Future Expansion (not built yet)

Live HR probability updates, cash-out evaluator, hedge calculator, Kelly
sizing, bankroll management, automated betting slip evaluation, and the
eventual MLB Edge Lab export/integration point.
