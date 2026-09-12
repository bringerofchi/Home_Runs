"""Ingestion layer: pulls raw daily data into data/raw/.

Modules:
    mlb_data           -- game, pitcher, and hitter stats (official MLB Stats API)
    statcast            -- barrel%, hard-hit%, exit velocity, xSLG (unofficial
                            Baseball Savant leaderboard export -- see module
                            docstring for stability caveats)
    weather              -- wind, temperature, humidity, roof status
                            (Open-Meteo, free/no-key, real documented API)
    ballpark_reference   -- static coordinates/roof-type/orientation data
                            used by weather.py
    odds                 -- FanDuel HR odds and implied probability conversion
"""
