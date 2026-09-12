r"""Standalone diagnostic: shows exactly why get_game_totals() returned
nothing for a given date, instead of just the one-line warning.

Usage (from C:\Projects\home runs):
    python check_totals.py 2026-09-06
"""
import os
import sys
import json
from datetime import date

from dotenv import load_dotenv

# Load .env from this script's own folder (the project root) -- same
# ENV_PATH pattern webapp/app.py uses. Without this, PARLAY_API_KEY /
# ODDS_API_KEY only get picked up if they happen to already be set as
# real OS environment variables, not just sitting in .env.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH)

from ingestion.odds import get_game_totals

if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    game_date = date.fromisoformat(date_str)

    print(f".env loaded from: {ENV_PATH}")
    print(f"ODDS_API_KEY set: {bool(os.environ.get('ODDS_API_KEY'))}")
    print(f"PARLAY_API_KEY set: {bool(os.environ.get('PARLAY_API_KEY'))}")
    print()

    totals, diagnostics = get_game_totals(game_date, book="fanduel")

    print(f"Date checked: {date_str}")
    print(f"Totals found: {len(totals)}")
    print()
    print("Full diagnostics:")
    print(json.dumps(diagnostics, indent=2, default=str))
