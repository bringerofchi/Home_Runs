"""Collects sportsbook HR odds (FanDuel primary) and converts to
implied probability.

This module has two halves:

1. Pure math -- ``american_odds_to_implied_probability`` /
   ``implied_probability_to_american_odds``. Fully implemented, no
   external dependencies, safe to unit test in isolation.

2. Data access -- ``get_hr_odds``. No live sportsbook feed is wired up
   yet. FanDuel does not expose a public odds API; a real integration
   would sit behind a licensed odds-data provider (e.g. The Odds API,
   SportsDataIO) or an approved partner feed. For now ``get_hr_odds``
   is a pluggable interface backed by a ``MockOddsSource`` so the rest
   of the pipeline (models, reports) can be built and tested against a
   stable contract. Swap in a real ``OddsSource`` implementation later
   without touching any calling code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date as _date
from typing import Protocol

import requests


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

def american_odds_to_implied_probability(odds: int) -> float:
    """Convert American odds to implied probability (0.0-1.0).

    positive odds:  100 / (odds + 100)
    negative odds:  -odds / (-odds + 100)

    Example: +300 -> 0.25 (25%)
             -150 -> 0.60 (60%)

    Raises:
        ValueError: if odds == 0 (not a valid American odds value).
    """
    if odds == 0:
        raise ValueError("American odds cannot be 0")

    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)


def implied_probability_to_american_odds(probability: float) -> int:
    """Inverse of american_odds_to_implied_probability.

    Useful for sanity-checking model output against a hypothetical
    "fair" line, or for displaying model probability in odds terms.

    Raises:
        ValueError: if probability is not in the open interval (0, 1).
    """
    if not 0 < probability < 1:
        raise ValueError("probability must be between 0 and 1 (exclusive)")

    if probability >= 0.5:
        odds = -round(probability / (1 - probability) * 100)
    else:
        odds = round((1 - probability) / probability * 100)
    return odds


def american_odds_to_profit_multiple(odds: int) -> float:
    """Profit per $1 staked if the bet wins (not counting the returned
    stake). Used for expected-value calculations.

    positive odds:  odds / 100        (e.g. +300 -> profit 3.00 per $1)
    negative odds:  100 / -odds       (e.g. -150 -> profit 0.667 per $1)
    """
    if odds == 0:
        raise ValueError("American odds cannot be 0")
    return odds / 100 if odds > 0 else 100 / -odds


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

@dataclass
class HrOddsQuote:
    player: str
    book: str
    opening_odds: int
    current_odds: int
    closing_odds: int | None  # None until the game has started

    @property
    def opening_implied_probability(self) -> float:
        return american_odds_to_implied_probability(self.opening_odds)

    @property
    def current_implied_probability(self) -> float:
        return american_odds_to_implied_probability(self.current_odds)

    @property
    def closing_implied_probability(self) -> float | None:
        if self.closing_odds is None:
            return None
        return american_odds_to_implied_probability(self.closing_odds)


class OddsSource(Protocol):
    """Interface any real odds provider integration should implement."""

    def fetch(self, player: str, game_date: _date, book: str) -> HrOddsQuote:
        ...


class MockOddsSource:
    """Placeholder odds source for development/testing.

    Returns deterministic, clearly-fake odds so downstream code and
    tests never mistake this for real market data.
    """

    def fetch(self, player: str, game_date: _date, book: str = "fanduel") -> HrOddsQuote:
        return HrOddsQuote(
            player=player,
            book=book,
            opening_odds=340,
            current_odds=320,
            closing_odds=None,
        )


class TheOddsApiSource:
    """Live odds source backed by The Odds API (https://the-odds-api.com).

    Requires an API key -- pass it directly or set ODDS_API_KEY in your
    environment/.env file.

    Verified against The Odds API's published v4 docs: the anytime-HR
    prop market key is ``batter_home_runs``, and its outcomes come back
    as ``{"name": "Over", "description": "<player>", "price": ..., "point": 0.5}``
    per player (confirmed against a real documented BetMGM MLB response).
    "Yes" is kept as a fallback in case a specific book formats it
    differently.

    Docs: https://the-odds-api.com/liveapi/guides/v4/#player-props
    """

    BASE_URL = "https://api.the-odds-api.com/v4"
    SPORT_KEY = "baseball_mlb"
    HR_MARKET_KEY = "batter_home_runs"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No Odds API key found. Pass api_key=, or set ODDS_API_KEY "
                "in your .env file."
            )

    def _get_events(self, game_date: _date) -> list[dict]:
        """List MLB events (games) happening on game_date (UTC calendar
        date of commence_time).
        """
        resp = requests.get(
            f"{self.BASE_URL}/sports/{self.SPORT_KEY}/events",
            params={"apiKey": self.api_key},
            timeout=15,
        )
        resp.raise_for_status()
        events = resp.json()
        return [
            e for e in events
            if e.get("commence_time", "")[:10] == game_date.isoformat()
        ]

    def list_event_markets(self, event_id: str, book: str | None = None) -> dict:
        """Debug helper: list every market key (and last_update timestamp)
        a bookmaker has currently posted for a specific event, using
        The Odds API's lightweight /markets endpoint (no odds/prices,
        just which markets exist).

        Use this when you believe a book (e.g. FanDuel) has a market
        posted but it's not showing up in fetch_all -- it'll reveal
        whether the market is under a different key (e.g.
        batter_home_runs_alternate instead of batter_home_runs) or
        whether The Odds API simply hasn't picked it up yet (check
        last_update).

        Returns the raw parsed JSON: a dict with "bookmakers", each
        containing "markets": [{"key": ..., "last_update": ...}, ...].
        """
        params = {"apiKey": self.api_key, "regions": "us"}
        if book:
            params["bookmakers"] = book
        resp = requests.get(
            f"{self.BASE_URL}/sports/{self.SPORT_KEY}/events/{event_id}/markets",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # Preference order used only when book="any" -- picks the first of
    # these that has posted the market for a given game, falling back to
    # whatever else is available.
    PREFERRED_BOOK_ORDER = ["fanduel", "draftkings", "betmgm", "betrivers", "espnbet"]

    # Some books (confirmed live: FanDuel, DraftKings, BetMGM, Bovada,
    # MyBookie, BetOnline, Fanatics) don't post the single-line
    # ``batter_home_runs`` market at all -- they only post
    # ``batter_home_runs_alternate``, a milestone market with separate
    # Over 0.5 / Over 1.5 / Over 2.5 ... HR lines per player. The
    # Over 0.5 line on that alternate market means exactly the same
    # thing as the base market's Over/Yes outcome (at least one HR), so
    # it's used as a fallback when a book only has the alternate.
    # Other books (confirmed live: BetRivers, Caesars) post the plain
    # ``batter_home_runs`` market directly -- that's preferred when
    # available since it's a direct read with no line-filtering needed.
    HR_MARKET_ALTERNATE_KEY = "batter_home_runs_alternate"

    def fetch_all(self, game_date: _date, book: str = "fanduel"
                   ) -> tuple[list[HrOddsQuote], dict]:
        """Fetch anytime-HR prop odds for every player across every MLB
        game on game_date.

        Args:
            book: a specific bookmaker key (e.g. "fanduel"), or "any" to
                auto-pick whichever US book has posted the market for
                each game (preferring PREFERRED_BOOK_ORDER, falling back
                to the first available book otherwise). Useful early in
                the day before your preferred book has posted lines.

        NOTE: this deliberately does NOT pass a ``bookmakers=`` filter to
        the API -- it requests the market for the whole ``us`` region and
        filters client-side. That way diagnostics can tell the difference
        between "no book has posted this market yet" and "your specific
        book hasn't, but others have" (a filter bug vs. a data-timing
        issue).

        Both ``batter_home_runs`` and ``batter_home_runs_alternate`` are
        requested and considered -- see HR_MARKET_ALTERNATE_KEY's comment
        for why. A book's base-market entry always wins if both are
        present for that book, since it's a direct one-line read.

        Returns:
            (quotes, diagnostics) -- diagnostics explains *why* quotes
            might be empty even when the request itself succeeded:
                {
                    "games_found": int,
                    "games": [
                        {
                            "matchup": "Away @ Home",
                            "event_id": str,
                            "http_status": int,
                            "books_with_hr_market": [str, ...],  # ANY book, region-wide, base OR alternate
                            "requested_book_has_hr_market": bool,
                            "book_used": str | None,  # which book this game's quotes came from
                            "market_type_used": "base" | "alternate" | None,
                        },
                        ...
                    ],
                }
        """
        quotes: list[HrOddsQuote] = []
        events = self._get_events(game_date)
        diagnostics: dict = {
            "games_found": len(events),
            "games": [],
            "api_quota_remaining": None,
            "api_quota_used": None,
        }

        for event in events:
            game_diag = {
                "matchup": f"{event.get('away_team')} @ {event.get('home_team')}",
                "event_id": event.get("id"),
                "http_status": None,
                "books_with_hr_market": [],
                "requested_book_has_hr_market": False,
                "book_used": None,
                "market_type_used": None,
            }

            resp = requests.get(
                f"{self.BASE_URL}/sports/{self.SPORT_KEY}/events/{event['id']}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": f"{self.HR_MARKET_KEY},{self.HR_MARKET_ALTERNATE_KEY}",
                    "oddsFormat": "american",
                    # No bookmakers= filter here on purpose -- see docstring.
                },
                timeout=15,
            )
            game_diag["http_status"] = resp.status_code
            if "x-requests-remaining" in resp.headers:
                diagnostics["api_quota_remaining"] = resp.headers.get("x-requests-remaining")
                diagnostics["api_quota_used"] = resp.headers.get("x-requests-used")

            if resp.status_code != 200:
                game_diag["error"] = resp.text[:300]
                diagnostics["games"].append(game_diag)
                continue

            data = resp.json()
            bookmakers = data.get("bookmakers", [])

            # Map of book key -> {"market": dict, "type": "base"|"alternate"},
            # for books that have either HR market. Base always wins over
            # alternate for a given book if both are present.
            markets_by_book: dict[str, dict] = {}
            for bookmaker in bookmakers:
                bm_key = bookmaker.get("key")
                for market in bookmaker.get("markets", []):
                    market_key = market.get("key")
                    if market_key == self.HR_MARKET_KEY:
                        markets_by_book[bm_key] = {"market": market, "type": "base"}
                    elif market_key == self.HR_MARKET_ALTERNATE_KEY and bm_key not in markets_by_book:
                        markets_by_book[bm_key] = {"market": market, "type": "alternate"}

            game_diag["books_with_hr_market"] = list(markets_by_book.keys())
            game_diag["requested_book_has_hr_market"] = book in markets_by_book

            if book == "any":
                target_book = next(
                    (b for b in self.PREFERRED_BOOK_ORDER if b in markets_by_book),
                    next(iter(markets_by_book), None),
                )
            else:
                target_book = book if book in markets_by_book else None

            if target_book is None:
                diagnostics["games"].append(game_diag)
                continue

            game_diag["book_used"] = target_book
            entry = markets_by_book[target_book]
            game_diag["market_type_used"] = entry["type"]

            for outcome in entry["market"].get("outcomes", []):
                if entry["type"] == "alternate":
                    # Alternate market carries multiple lines (0.5, 1.5,
                    # 2.5 HRs...) per player -- Over 0.5 is "at least one
                    # HR", the same thing the base market's Over/Yes means.
                    if outcome.get("name") != "Over" or outcome.get("point") != 0.5:
                        continue
                else:
                    if outcome.get("name") not in ("Over", "Yes"):
                        continue
                player_name = (
                    outcome.get("description")
                    or outcome.get("participant")
                    or "Unknown"
                )
                quotes.append(HrOddsQuote(
                    player=player_name,
                    book=target_book,
                    opening_odds=outcome["price"],
                    current_odds=outcome["price"],
                    closing_odds=None,
                ))

            diagnostics["games"].append(game_diag)

        return quotes, diagnostics

    def fetch(self, player: str, game_date: _date, book: str = "fanduel") -> HrOddsQuote:
        quotes, _ = self.fetch_all(game_date, book)
        for quote in quotes:
            if quote.player.lower() == player.lower():
                return quote
        raise LookupError(f"No HR odds found for {player!r} on {game_date}")

    # -----------------------------------------------------------------
    # Game totals (Vegas runs total) -- a GAME-level market, not a
    # per-player one, so the return shape below is deliberately
    # different from fetch_all's: one number per game, keyed by team
    # pair rather than by player name.
    # -----------------------------------------------------------------

    GAME_TOTALS_MARKET_KEY = "totals"

    def fetch_game_totals(
        self, game_date: _date, book: str = "fanduel",
    ) -> tuple[dict[frozenset, float], dict]:
        """Fetch the Vegas game (runs) total for every MLB game on
        game_date.

        Returns a lookup keyed by frozenset({home_team, away_team}) --
        an order-independent team pair -- using the exact team-name
        strings The Odds API returns for this sport. Callers (e.g.
        reports.daily_report) should build the matching key from
        ingestion.mlb_data.fetch_games()'s home_team/away_team fields,
        normalized the same way (see reports.daily_report._normalize_team_name).
        The Odds API and MLB Stats API have been observed to agree on
        full team names for MLB (e.g. "New York Yankees") -- lighter
        normalization is expected to be enough here than the accent/
        period handling player-name matching needed in fetch_all, but
        this is UNVERIFIED against a live response (see module
        docstring) -- spot-check before trusting it fully.

        If the requested book hasn't posted a totals line for a game
        yet but another book has, this falls back to whichever book DID
        post one -- unlike fetch_all's book="any" path (which is opt-in
        because individual player HR prices can vary meaningfully book
        to book), this fallback is always on for totals: a game-level
        number varies far less by book, and "some real number" beats
        "None" for an environment sub-metric that's otherwise 0% covered
        today.

        Returns:
            (totals_by_matchup, diagnostics) -- diagnostics follows the
            same per-game shape as fetch_all's:
                {
                    "games_found": int,
                    "games": [
                        {
                            "matchup": "Away @ Home",
                            "event_id": str,
                            "http_status": int,
                            "books_with_totals_market": [str, ...],
                            "requested_book_has_totals_market": bool,
                            "total_used": float | None,
                        },
                        ...
                    ],
                }
        """
        totals_by_matchup: dict[frozenset, float] = {}
        events = self._get_events(game_date)
        diagnostics: dict = {"games_found": len(events), "games": []}

        for event in events:
            home = event.get("home_team")
            away = event.get("away_team")
            game_diag = {
                "matchup": f"{away} @ {home}",
                "event_id": event.get("id"),
                "http_status": None,
                "books_with_totals_market": [],
                "requested_book_has_totals_market": False,
                "total_used": None,
            }

            resp = requests.get(
                f"{self.BASE_URL}/sports/{self.SPORT_KEY}/events/{event['id']}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": self.GAME_TOTALS_MARKET_KEY,
                    "oddsFormat": "american",
                },
                timeout=15,
            )
            game_diag["http_status"] = resp.status_code

            if resp.status_code != 200:
                game_diag["error"] = resp.text[:300]
                diagnostics["games"].append(game_diag)
                continue

            data = resp.json()
            totals_by_book: dict[str, float] = {}
            for bookmaker in data.get("bookmakers", []):
                bm_key = bookmaker.get("key")
                for market in bookmaker.get("markets", []):
                    if market.get("key") != self.GAME_TOTALS_MARKET_KEY:
                        continue
                    for outcome in market.get("outcomes", []):
                        # "Over"'s point is the total itself -- "Under"
                        # carries the identical point value, so only one
                        # side needs reading.
                        if outcome.get("name") == "Over" and outcome.get("point") is not None:
                            totals_by_book[bm_key] = outcome["point"]
                            break

            game_diag["books_with_totals_market"] = list(totals_by_book.keys())
            game_diag["requested_book_has_totals_market"] = book in totals_by_book

            target_total = totals_by_book.get(book)
            if target_total is None and totals_by_book:
                target_total = next(iter(totals_by_book.values()))

            if target_total is not None and home and away:
                totals_by_matchup[frozenset({home, away})] = target_total
                game_diag["total_used"] = target_total

            diagnostics["games"].append(game_diag)

        return totals_by_matchup, diagnostics


class ParlayApiSource:
    """Backup/alternate odds source using ParlayAPI (parlay-api.com) --
    marketed as a drop-in-compatible, cheaper alternative to The Odds
    API. Requires an API key -- pass it directly or set PARLAY_API_KEY
    in your .env file.

    HIGHER CONFIDENCE than usual for a backup source: ParlayAPI's public
    docs (https://parlay-api.com/docs) explicitly document the
    player_home_runs market with a real example response (player name,
    over/under price, line=0.5, bookmaker) -- this was verified against
    that documentation, not guessed at. Still NOT tested against a live
    response in this dev environment (no network access to
    parlay-api.com here).

    Design note: unlike TheOddsApiSource, this uses a single /props call
    (3 credits, covers every book/game at once) rather than looping per
    event -- ParlayAPI doesn't expose a date filter on that endpoint, so
    date matching happens client-side against each row's commence_time.
    """

    BASE_URL = "https://parlay-api.com/v1"
    SPORT_KEY = "baseball_mlb"
    HR_MARKET_KEY = "player_home_runs"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("PARLAY_API_KEY")
        if not self.api_key:
            raise ValueError(
                "No ParlayAPI key found. Pass api_key=, or set PARLAY_API_KEY "
                "in your .env file."
            )

    def fetch_all(self, game_date: _date, book: str = "any") -> tuple[list[HrOddsQuote], dict]:
        """Fetch anytime-HR prop odds for game_date, across every book
        at once (or filtered to one book).

        Returns (quotes, diagnostics) -- diagnostics includes HTTP
        status, quota headers if present, raw row count, and which books
        were actually seen, following the same pattern as
        TheOddsApiSource.fetch_all.
        """
        resp = requests.get(
            f"{self.BASE_URL}/sports/{self.SPORT_KEY}/props",
            headers={"X-API-Key": self.api_key},
            params={"markets": self.HR_MARKET_KEY},
            timeout=20,
        )

        diagnostics = {
            "provider": "parlay_api",
            "http_status": resp.status_code,
            "api_quota_remaining": resp.headers.get("x-requests-remaining"),
            "api_quota_used": resp.headers.get("x-requests-used"),
        }

        if resp.status_code != 200:
            diagnostics["error"] = resp.text[:300]
            return [], diagnostics

        rows = resp.json()
        date_str = game_date.isoformat()
        quotes: list[HrOddsQuote] = []
        books_seen = set()

        for row in rows:
            if row.get("market_key") != self.HR_MARKET_KEY:
                continue
            commence_time = row.get("commence_time", "")
            if commence_time[:10] != date_str:
                continue
            bookmaker = row.get("bookmaker")
            if book != "any" and bookmaker != book:
                continue
            over_price = row.get("over_price")
            player_name = row.get("player")
            if over_price is None or player_name is None:
                continue

            quotes.append(HrOddsQuote(
                player=player_name,
                book=bookmaker,
                opening_odds=over_price,
                current_odds=over_price,
                closing_odds=None,
            ))
            books_seen.add(bookmaker)

        diagnostics["rows_returned_raw"] = len(rows)
        diagnostics["quotes_matched_to_date"] = len(quotes)
        diagnostics["books_seen"] = sorted(books_seen)
        return quotes, diagnostics

    # -----------------------------------------------------------------
    # Game totals -- a SEPARATE, "TOA-compatible" endpoint from the
    # /props market above, per ParlayAPI's docs and PyPI page.
    # -----------------------------------------------------------------

    TOTALS_MARKET_KEY = "totals"

    def fetch_game_totals(self, game_date: _date, book: str = "any") -> tuple[dict[frozenset, float], dict]:
        """Fetch Vegas game (runs) totals for game_date.

        LOWER CONFIDENCE than fetch_all above -- flagged explicitly.
        ParlayAPI's public docs and PyPI page describe a separate,
        "TOA-compatible" bulk odds endpoint (their SDK's
        client.odds(sport, regions=..., markets="h2h,spreads,totals"))
        that's marketed as mirroring The Odds API's own request/response
        shape -- one call returns every game's odds for the requested
        markets, unlike TheOddsApiSource.fetch_game_totals's per-event
        loop. This method assumes:
            1. The REST path is {BASE_URL}/sports/{sport}/odds (same
               base as the confirmed-working /props path above, with
               the documented query params: regions, markets, oddsFormat).
            2. The JSON response shape mirrors The Odds API's own bulk
               odds response: a list of event dicts, each with
               home_team, away_team, commence_time, and
               bookmakers -> markets -> outcomes.
        NEITHER assumption has been confirmed against a live response in
        this dev environment (no network access to parlay-api.com here).
        Check diagnostics["sample_event"] against a real response before
        trusting this in production -- an empty result could mean "no
        totals posted yet" OR "one of the two assumptions above is
        wrong," and this function can't tell those apart on its own.

        Returns:
            (totals_by_matchup, diagnostics) -- totals_by_matchup keyed
            by frozenset({home_team, away_team}), the SAME shape as
            TheOddsApiSource.fetch_game_totals, so callers don't need
            to know which provider supplied the data.
        """
        resp = requests.get(
            f"{self.BASE_URL}/sports/{self.SPORT_KEY}/odds",
            headers={"X-API-Key": self.api_key},
            params={"regions": "us", "markets": self.TOTALS_MARKET_KEY, "oddsFormat": "american"},
            timeout=20,
        )

        diagnostics = {
            "provider": "parlay_api",
            "http_status": resp.status_code,
            "api_quota_remaining": resp.headers.get("x-requests-remaining"),
            "api_quota_used": resp.headers.get("x-requests-used"),
        }

        if resp.status_code != 200:
            diagnostics["error"] = resp.text[:300]
            return {}, diagnostics

        try:
            events = resp.json()
        except ValueError:
            diagnostics["error"] = "response was not valid JSON -- endpoint/shape assumption may be wrong"
            return {}, diagnostics

        if not isinstance(events, list):
            diagnostics["error"] = "expected a list of events -- response shape assumption may be wrong"
            diagnostics["sample_event"] = events
            return {}, diagnostics

        diagnostics["events_found"] = len(events)
        diagnostics["sample_event"] = events[0] if events else None

        date_str = game_date.isoformat()
        totals_by_matchup: dict[frozenset, float] = {}

        for event in events:
            if event.get("commence_time", "")[:10] != date_str:
                continue
            home = event.get("home_team")
            away = event.get("away_team")
            if not home or not away:
                continue

            totals_by_book: dict[str, float] = {}
            for bookmaker in event.get("bookmakers", []):
                bm_key = bookmaker.get("key") or bookmaker.get("bookmaker")
                for market in bookmaker.get("markets", []):
                    if market.get("key") != self.TOTALS_MARKET_KEY:
                        continue
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over" and outcome.get("point") is not None:
                            totals_by_book[bm_key] = outcome["point"]
                            break

            target_total = totals_by_book.get(book)
            if target_total is None and totals_by_book:
                target_total = next(iter(totals_by_book.values()))

            if target_total is not None:
                totals_by_matchup[frozenset({home, away})] = target_total

        diagnostics["totals_matched"] = len(totals_by_matchup)
        return totals_by_matchup, diagnostics


class SharpApiSource:
    """Backup/alternate odds source using SharpAPI (sharpapi.io).

    LOWER CONFIDENCE than ParlayApiSource above -- flagged explicitly.
    SharpAPI's public docs confirm HR player props are covered as a
    market ("Strikeouts, hits, home runs, RBIs, total bases, walks, and
    various combo props"), but I could not find the exact market_type
    string to filter on in their published reference, nor a documented
    example response shape specifically for a player-prop row (only a
    moneyline example was available). HR_MARKET_TYPE and the row-parsing
    logic below are BEST-EFFORT GUESSES, not verified -- use
    fetch_all()'s diagnostics (raw row count, and a sample row if
    parsing fails to find odds) to check a live response before trusting
    this in production. Requires an API key -- pass it directly or set
    SHARPAPI_KEY in your .env file.

    Free tier note (from general research, not this account specifically):
    rate-limited (~12 req/min) rather than credit-metered, but only 2
    sportsbooks included on the free tier -- expect thinner book coverage
    than the other two sources.
    """

    BASE_URL = "https://api.sharpapi.io/api/v1"
    HR_MARKET_TYPE = "player_home_runs"  # UNVERIFIED GUESS -- confirm against a live response

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SHARPAPI_KEY")
        if not self.api_key:
            raise ValueError(
                "No SharpAPI key found. Pass api_key=, or set SHARPAPI_KEY "
                "in your .env file."
            )

    def fetch_all(self, game_date: _date, book: str = "any") -> tuple[list[HrOddsQuote], dict]:
        """Fetch anytime-HR prop odds for game_date.

        NOTE: this endpoint's params (sport/league/market naming) follow
        SharpAPI's documented Odds endpoint pattern for game-level
        markets -- whether "market" accepts a player-prop key the same
        way is unverified. If this returns 0 rows with a 200 status
        (not an error), that's the first thing to check via
        diagnostics["sample_row"].
        """
        params = {"sport": "baseball", "league": "mlb", "market": self.HR_MARKET_TYPE}
        if book != "any":
            params["sportsbook"] = book

        resp = requests.get(
            f"{self.BASE_URL}/odds",
            headers={"X-API-Key": self.api_key},
            params=params,
            timeout=20,
        )

        diagnostics = {"provider": "sharpapi", "http_status": resp.status_code}

        if resp.status_code != 200:
            diagnostics["error"] = resp.text[:300]
            return [], diagnostics

        data = resp.json()
        rows = data.get("data", [])
        diagnostics["rows_returned_raw"] = len(rows)
        diagnostics["meta"] = data.get("meta", {})
        diagnostics["sample_row"] = rows[0] if rows else None

        date_str = game_date.isoformat()
        quotes: list[HrOddsQuote] = []

        for row in rows:
            # Best-effort field guesses -- see class docstring. Adjust
            # based on diagnostics["sample_row"] if these don't match.
            commence_time = row.get("commence_time", "") or row.get("game_time", "")
            if commence_time and commence_time[:10] != date_str:
                continue
            price = row.get("odds_american")
            player_name = row.get("player") or row.get("selection")
            if price is None or player_name is None:
                continue

            quotes.append(HrOddsQuote(
                player=player_name,
                book=row.get("sportsbook", "unknown"),
                opening_odds=price,
                current_odds=price,
                closing_odds=None,
            ))

        diagnostics["quotes_parsed"] = len(quotes)
        return quotes, diagnostics


# Providers tried in order, paired with their env var and class. This is
# the single source of truth for fallback order -- add a new backup
# provider here and it's automatically included in fetch_odds_with_fallback.
_PROVIDER_CHAIN = [
    ("odds_api", "ODDS_API_KEY", TheOddsApiSource),
    ("parlay_api", "PARLAY_API_KEY", ParlayApiSource),
    ("sharp_api", "SHARPAPI_KEY", SharpApiSource),
]


def fetch_odds_with_fallback(game_date: _date, book: str = "any") -> tuple[list[HrOddsQuote], dict]:
    """Try each configured odds provider in order -- The Odds API, then
    ParlayAPI, then SharpAPI -- falling through to the next one if a
    provider has no key configured, errors out, or returns zero quotes.

    This is the actual point of configuring backup provider keys: without
    this function, additional keys just sit unused while every call still
    goes to whichever provider is hardcoded first. Both webapp routes
    that fetch odds (/api/fetch-odds and /api/daily-report) should call
    this instead of instantiating TheOddsApiSource directly.

    A provider is skipped (not counted as a failure) if its key isn't
    set -- that's the expected state for anyone who's only configured
    the primary. An empty result (0 quotes, e.g. nobody's posted lines
    yet) is treated the same as an error for fallback purposes: no
    quotes means "try the next provider," even if the HTTP call itself
    succeeded.

    Returns:
        (quotes, diagnostics) where diagnostics is:
            {
                "provider_used": str | None,  # None if every provider failed/was unconfigured
                "attempts": [
                    {"provider": str, "status": "skipped_no_key" | "error" | "empty" | "success",
                     "error": str (if status == "error"),
                     "quotes_found": int (if status == "success")},
                    ...
                ],
                "provider_diagnostics": dict,  # the successful provider's own diagnostics, if any
            }
    """
    attempts = []

    for provider_name, env_var, source_cls in _PROVIDER_CHAIN:
        api_key = os.environ.get(env_var)
        if not api_key:
            attempts.append({"provider": provider_name, "status": "skipped_no_key"})
            continue

        try:
            source = source_cls(api_key=api_key)
            quotes, provider_diagnostics = source.fetch_all(game_date, book)
        except Exception as exc:
            attempts.append({"provider": provider_name, "status": "error", "error": str(exc)})
            continue

        if not quotes:
            attempts.append({"provider": provider_name, "status": "empty"})
            continue

        attempts.append({"provider": provider_name, "status": "success", "quotes_found": len(quotes)})
        return quotes, {
            "provider_used": provider_name,
            "attempts": attempts,
            "provider_diagnostics": provider_diagnostics,
        }

    return [], {"provider_used": None, "attempts": attempts, "provider_diagnostics": {}}


def get_hr_odds(player: str, game_date: _date, book: str = "fanduel",
                 source: OddsSource | None = None) -> HrOddsQuote:
    """Fetch HR odds for a player on a given date.

    Args:
        player: player name.
        game_date: date of the game.
        book: sportsbook name. FanDuel is the primary/default source.
        source: an OddsSource implementation. Defaults to
            MockOddsSource() when not provided -- swap this for a real
            provider once one is wired up.

    Returns:
        HrOddsQuote with opening/current/closing American odds and
        implied-probability properties.
    """
    if source is None:
        source = MockOddsSource()
    return source.fetch(player, game_date, book)


def get_game_totals(game_date: _date, book: str = "fanduel") -> tuple[dict, dict]:
    """Fetch Vegas game (runs) totals for game_date, trying providers in
    order and falling through if one is unconfigured, errors out, or
    returns nothing -- same "try each configured provider" pattern as
    fetch_odds_with_fallback, but scoped to only the two providers with
    SOME documented basis for a totals market:

        1. The Odds API (ODDS_API_KEY) -- confirmed, tested pattern,
           see TheOddsApiSource.fetch_game_totals.
        2. ParlayAPI (PARLAY_API_KEY) -- documented but UNVERIFIED, see
           ParlayApiSource.fetch_game_totals's own docstring for the
           two assumptions it makes.

    SharpAPI is deliberately NOT included here -- unlike ParlayAPI, its
    docs never confirmed a totals market (only a moneyline example was
    documented anywhere found), so adding it would be a pure guess
    stacked on an already-unverified endpoint just to have a third try.

    An empty result from a provider (0 games matched, e.g. nobody's
    posted totals yet) is treated the same as an error for fallback
    purposes -- try the next provider, even if the HTTP call itself
    succeeded, same logic fetch_odds_with_fallback uses.

    Returns:
        (totals, diagnostics) where diagnostics is:
            {
                "provider_used": str | None,  # None if every provider failed/was unconfigured/empty
                "attempts": [
                    {"provider": str, "status": "skipped_no_key" | "error" | "empty" | "success",
                     "error": str (if status == "error"),
                     "totals_found": int (if status == "success")},
                    ...
                ],
                "provider_diagnostics": dict,  # the successful provider's own diagnostics, if any
            }
    """
    attempts = []

    odds_api_key = os.environ.get("ODDS_API_KEY")
    if not odds_api_key:
        attempts.append({"provider": "odds_api", "status": "skipped_no_key"})
    else:
        try:
            totals, provider_diagnostics = TheOddsApiSource(api_key=odds_api_key).fetch_game_totals(game_date, book)
        except Exception as exc:
            attempts.append({"provider": "odds_api", "status": "error", "error": str(exc)})
        else:
            if totals:
                attempts.append({"provider": "odds_api", "status": "success", "totals_found": len(totals)})
                return totals, {"provider_used": "odds_api", "attempts": attempts, "provider_diagnostics": provider_diagnostics}
            attempts.append({"provider": "odds_api", "status": "empty", "provider_diagnostics": provider_diagnostics})

    parlay_api_key = os.environ.get("PARLAY_API_KEY")
    if not parlay_api_key:
        attempts.append({"provider": "parlay_api", "status": "skipped_no_key"})
    else:
        try:
            # book="any" here -- ParlayApiSource.fetch_game_totals falls
            # back to whatever book has posted a total regardless, and
            # this backup path isn't trying to prefer a specific book.
            totals, provider_diagnostics = ParlayApiSource(api_key=parlay_api_key).fetch_game_totals(game_date, book="any")
        except Exception as exc:
            attempts.append({"provider": "parlay_api", "status": "error", "error": str(exc)})
        else:
            if totals:
                attempts.append({"provider": "parlay_api", "status": "success", "totals_found": len(totals)})
                return totals, {"provider_used": "parlay_api", "attempts": attempts, "provider_diagnostics": provider_diagnostics}
            attempts.append({"provider": "parlay_api", "status": "empty", "provider_diagnostics": provider_diagnostics})

    return {}, {"provider_used": None, "attempts": attempts, "provider_diagnostics": {}}
