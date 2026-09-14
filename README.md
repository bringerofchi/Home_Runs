# MLB HR Alpha Lab

MLB HR Alpha Lab is a standalone local application for ranking MLB hitters for home-run research. It combines pitcher vulnerability, hitter power, environment, and market price into a transparent 0–100 HR Alpha Score. It does not place bets or guarantee outcomes.

## Workflow

The dashboard supports two stages:

1. **Watch list:** pre-lineup candidates using probable pitchers and likely player context.
2. **Final rankings:** confirmed starters and batting orders, actual opposing pitcher, available environment data, and available HR market prices.

It can retrieve MLB schedule, player, lineup, and box-score data; inspect Statcast/MLB ingestion health; pull player-HR odds from configured providers; cache odds to conserve API quota; save rankings; and automatically record final HR outcomes where player identity can be matched.

## Alpha Score

The transparent score has four categories:

- Pitcher vulnerability: 35 points
- Hitter power: 25 points
- Environment: 25 points
- Betting value: 15 points

The current model probability is a deliberately simple calibrated linear fit over the first three quality categories. It should be treated as an early calibration layer, not a finished predictive model. Some environmental inputs remain unavailable or partial, and missing data produces no contribution rather than invented values.

## Odds and results

The Odds API is the primary HR-prop source. ParlayAPI and SharpAPI are configured as optional fallbacks, but their own source docstrings mark both integrations as unverified/best-effort against a live API response — treat them as speculative until confirmed, not as confirmed-working backups. The application degrades gracefully when odds are unavailable: it can still rank players, while clearly omitting market-value fields.

Its own SQLite store, `database/hr_edge_lab.db`, holds:

- Daily Alpha Score snapshots and category-level explanations
- Model and implied probabilities, edge, expected value, odds, book, matchup, and player/game identifiers
- Postgame HR outcomes, P&L, entered and closing odds, CLV, and outcome source
- Short-lived odds cache entries

## Backtesting and evidence

The dashboard exposes date summaries, score-tier summaries, category-coverage diagnostics, and calibration audit tools. Source comments describe a calibration fit built from 8,189 historical score/outcome pairs, but the actual SQLite database and raw/processed data are intentionally not committed here. Therefore that sample and its results must be re-extracted from the local database before being represented as independently reproducible evidence.

## Independence

This application is independent from MLB Edge Lab (currently at `C:\Projects\baseball stats\mlb_edge_lab`) and has its own schema, storage, scoring, and runtime. Any later cross-project research should use an explicit export—not shared tables or implicit dependencies.

## Limitations

- HR is a highly variable binary outcome; score tiers need adequate samples.
- Some provider integrations and availability vary by slate and bookmaker.
- Missing park/weather or player data reduces score coverage.
- Closing-price capture is not yet a full historical market-snapshot system.
