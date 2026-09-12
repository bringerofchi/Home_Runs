"""Modeling layer: turns raw ingested data into scores and probabilities.

Modules:
    pitcher_model -- pitcher HR vulnerability score (of 35)
    hitter_model  -- hitter power profile score (of 25)
    alpha_score   -- combines pitcher + hitter + environment + betting
                     value into the final 0-100 HR Alpha Score
"""
