import datetime
import math

import pytest

from ingestion.odds import (
    american_odds_to_implied_probability,
    implied_probability_to_american_odds,
    american_odds_to_profit_multiple,
    get_hr_odds,
    HrOddsQuote,
    MockOddsSource,
)


def test_profit_multiple_positive_odds():
    assert math.isclose(american_odds_to_profit_multiple(300), 3.0, rel_tol=1e-9)
    assert math.isclose(american_odds_to_profit_multiple(100), 1.0, rel_tol=1e-9)


def test_profit_multiple_negative_odds():
    assert math.isclose(american_odds_to_profit_multiple(-150), 100 / 150, rel_tol=1e-9)
    assert math.isclose(american_odds_to_profit_multiple(-100), 1.0, rel_tol=1e-9)


def test_profit_multiple_zero_raises():
    with pytest.raises(ValueError):
        american_odds_to_profit_multiple(0)


def test_positive_odds_examples():
    assert math.isclose(american_odds_to_implied_probability(300), 0.25, rel_tol=1e-9)
    assert math.isclose(american_odds_to_implied_probability(100), 0.5, rel_tol=1e-9)
    assert math.isclose(american_odds_to_implied_probability(500), 1 / 6, rel_tol=1e-9)


def test_negative_odds_examples():
    assert math.isclose(american_odds_to_implied_probability(-150), 0.6, rel_tol=1e-9)
    assert math.isclose(american_odds_to_implied_probability(-100), 0.5, rel_tol=1e-9)
    assert math.isclose(american_odds_to_implied_probability(-300), 0.75, rel_tol=1e-9)


def test_zero_odds_raises():
    with pytest.raises(ValueError):
        american_odds_to_implied_probability(0)


def test_round_trip_positive():
    prob = american_odds_to_implied_probability(320)
    odds = implied_probability_to_american_odds(prob)
    assert odds == 320


def test_round_trip_negative():
    prob = american_odds_to_implied_probability(-150)
    odds = implied_probability_to_american_odds(prob)
    assert odds == -150


def test_probability_out_of_range_raises():
    with pytest.raises(ValueError):
        implied_probability_to_american_odds(0)
    with pytest.raises(ValueError):
        implied_probability_to_american_odds(1)
    with pytest.raises(ValueError):
        implied_probability_to_american_odds(1.5)


def test_hr_odds_quote_properties():
    quote = HrOddsQuote(
        player="Kyle Schwarber",
        book="fanduel",
        opening_odds=340,
        current_odds=320,
        closing_odds=300,
    )
    # Direct checks against the conversion function (source of truth)
    assert quote.opening_implied_probability == american_odds_to_implied_probability(340)
    assert quote.current_implied_probability == american_odds_to_implied_probability(320)
    assert quote.closing_implied_probability == american_odds_to_implied_probability(300)


def test_hr_odds_quote_no_closing_yet():
    quote = HrOddsQuote(
        player="Kyle Schwarber",
        book="fanduel",
        opening_odds=340,
        current_odds=320,
        closing_odds=None,
    )
    assert quote.closing_implied_probability is None


def test_get_hr_odds_default_mock_source():
    quote = get_hr_odds("Kyle Schwarber", datetime.date(2026, 7, 8))
    assert isinstance(quote, HrOddsQuote)
    assert quote.book == "fanduel"
    assert quote.player == "Kyle Schwarber"


def test_get_hr_odds_explicit_source():
    quote = get_hr_odds(
        "Aaron Judge", datetime.date(2026, 7, 8), source=MockOddsSource()
    )
    assert quote.player == "Aaron Judge"


def test_the_odds_api_any_book_picks_preferred_available(monkeypatch):
    """book='any' should pick fanduel when it's available, and fall back
    to whatever else has the market when fanduel isn't.
    """
    import ingestion.odds as odds_mod

    events_payload = [
        {"id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "Home A", "away_team": "Away A"},
        {"id": "evt2", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "Home B", "away_team": "Away B"},
    ]

    odds_payloads = {
        "evt1": {
            "bookmakers": [
                {"key": "betrivers", "markets": [
                    {"key": "batter_home_runs", "outcomes": [
                        {"name": "Over", "description": "Player One", "price": 350},
                    ]},
                ]},
                {"key": "fanduel", "markets": [
                    {"key": "batter_home_runs", "outcomes": [
                        {"name": "Over", "description": "Player One", "price": 320},
                    ]},
                ]},
            ]
        },
        "evt2": {
            "bookmakers": [
                {"key": "betrivers", "markets": [
                    {"key": "batter_home_runs", "outcomes": [
                        {"name": "Over", "description": "Player Two", "price": 400},
                    ]},
                ]},
            ]
        },
    }

    class FakeResponse:
        def __init__(self, json_data, status_code=200):
            self._json = json_data
            self.status_code = status_code
            self.headers = {}
            self.text = ""

        def json(self):
            return self._json

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/events"):
            return FakeResponse(events_payload)
        for event_id, payload in odds_payloads.items():
            if event_id in url:
                return FakeResponse(payload)
        return FakeResponse({}, status_code=404)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.TheOddsApiSource(api_key="fake_key")
    quotes, diagnostics = source.fetch_all(datetime.date(2026, 7, 8), book="any")

    quotes_by_player = {q.player: q for q in quotes}
    # evt1 has fanduel available -- preferred order should pick it.
    assert quotes_by_player["Player One"].book == "fanduel"
    assert quotes_by_player["Player One"].current_odds == 320
    # evt2 only has betrivers -- should fall back to it.
    assert quotes_by_player["Player Two"].book == "betrivers"
    assert diagnostics["games_found"] == 2


# ---------------------------------------------------------------------------
# ParlayApiSource -- backup provider #1
# ---------------------------------------------------------------------------

class _FakeJsonResponse:
    def __init__(self, json_data, status_code=200, headers=None, text=""):
        self._json = json_data
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


def test_parlay_api_requires_key():
    import ingestion.odds as odds_mod
    import os
    old = os.environ.pop("PARLAY_API_KEY", None)
    try:
        with pytest.raises(ValueError):
            odds_mod.ParlayApiSource()
    finally:
        if old is not None:
            os.environ["PARLAY_API_KEY"] = old


def test_parlay_api_parses_real_documented_response_shape(monkeypatch):
    import ingestion.odds as odds_mod

    # Matches ParlayAPI's actual documented example response shape.
    props_payload = [
        {
            "bookmaker": "draftkings", "bookmaker_title": "DraftKings",
            "player": "Aaron Judge", "market_key": "player_home_runs", "market": "Home Runs",
            "line": 0.5, "over_price": 290, "under_price": -370,
            "home_team": "New York Yankees", "away_team": "Tampa Bay Rays",
            "canonical_event_id": "evt123", "commence_time": "2026-07-08T23:05:00Z",
            "last_update": "2026-07-08T18:00:00Z",
        },
        {
            "bookmaker": "fanduel", "bookmaker_title": "FanDuel",
            "player": "Yordan Alvarez", "market_key": "player_home_runs", "market": "Home Runs",
            "line": 0.5, "over_price": 340, "under_price": -450,
            "home_team": "Houston Astros", "away_team": "Seattle Mariners",
            "canonical_event_id": "evt456", "commence_time": "2026-07-09T02:10:00Z",  # different date
            "last_update": "2026-07-08T18:00:00Z",
        },
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["X-API-Key"] == "fake_parlay_key"
        assert params["markets"] == "player_home_runs"
        return _FakeJsonResponse(props_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_parlay_key")
    quotes, diagnostics = source.fetch_all(datetime.date(2026, 7, 8))

    assert len(quotes) == 1  # only Judge's row matches the requested date
    assert quotes[0].player == "Aaron Judge"
    assert quotes[0].current_odds == 290
    assert quotes[0].book == "draftkings"
    assert diagnostics["rows_returned_raw"] == 2
    assert diagnostics["quotes_matched_to_date"] == 1


def test_parlay_api_filters_by_specific_book(monkeypatch):
    import ingestion.odds as odds_mod

    props_payload = [
        {"bookmaker": "draftkings", "player": "Player A", "market_key": "player_home_runs",
         "over_price": 300, "commence_time": "2026-07-08T23:00:00Z"},
        {"bookmaker": "fanduel", "player": "Player B", "market_key": "player_home_runs",
         "over_price": 250, "commence_time": "2026-07-08T23:00:00Z"},
    ]

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse(props_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_key")
    quotes, _ = source.fetch_all(datetime.date(2026, 7, 8), book="fanduel")

    assert len(quotes) == 1
    assert quotes[0].player == "Player B"


def test_parlay_api_handles_error_status_gracefully(monkeypatch):
    import ingestion.odds as odds_mod

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse({}, status_code=401, text="Unauthorized")

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="bad_key")
    quotes, diagnostics = source.fetch_all(datetime.date(2026, 7, 8))

    assert quotes == []
    assert diagnostics["http_status"] == 401
    assert "error" in diagnostics


# ---------------------------------------------------------------------------
# SharpApiSource -- backup provider #2 (lower confidence, best-effort)
# ---------------------------------------------------------------------------

def test_sharp_api_requires_key():
    import ingestion.odds as odds_mod
    import os
    old = os.environ.pop("SHARPAPI_KEY", None)
    try:
        with pytest.raises(ValueError):
            odds_mod.SharpApiSource()
    finally:
        if old is not None:
            os.environ["SHARPAPI_KEY"] = old


def test_sharp_api_parses_best_effort_shape(monkeypatch):
    import ingestion.odds as odds_mod

    odds_payload = {
        "data": [
            {
                "sportsbook": "draftkings", "player": "Aaron Judge",
                "odds_american": 290, "commence_time": "2026-07-08T23:05:00Z",
            },
        ],
        "meta": {"count": 1, "total": 1, "books_available": ["draftkings"]},
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        assert headers["X-API-Key"] == "fake_sharp_key"
        assert params["market"] == "player_home_runs"
        return _FakeJsonResponse(odds_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.SharpApiSource(api_key="fake_sharp_key")
    quotes, diagnostics = source.fetch_all(datetime.date(2026, 7, 8))

    assert len(quotes) == 1
    assert quotes[0].player == "Aaron Judge"
    assert quotes[0].current_odds == 290
    assert diagnostics["rows_returned_raw"] == 1
    assert diagnostics["sample_row"] is not None  # available for live-verification debugging


def test_sharp_api_empty_response_surfaces_sample_row_as_none(monkeypatch):
    import ingestion.odds as odds_mod

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse({"data": [], "meta": {}})

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.SharpApiSource(api_key="fake_key")
    quotes, diagnostics = source.fetch_all(datetime.date(2026, 7, 8))

    assert quotes == []
    assert diagnostics["sample_row"] is None
    assert diagnostics["rows_returned_raw"] == 0


# ---------------------------------------------------------------------------
# fetch_odds_with_fallback -- the actual point of configuring backup keys
# ---------------------------------------------------------------------------

def _clear_provider_keys(monkeypatch):
    for env_var in ("ODDS_API_KEY", "PARLAY_API_KEY", "SHARPAPI_KEY"):
        monkeypatch.delenv(env_var, raising=False)


def test_fallback_uses_primary_when_it_succeeds(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")
    monkeypatch.setenv("PARLAY_API_KEY", "backup_key")

    called_providers = []

    def fake_odds_api_fetch_all(self, game_date, book="any"):
        called_providers.append("odds_api")
        return ([HrOddsQuote(player="A", book="fanduel", opening_odds=300, current_odds=300, closing_odds=None)],
                {"games_found": 1})

    def fake_parlay_fetch_all(self, game_date, book="any"):
        called_providers.append("parlay_api")
        return ([], {})

    monkeypatch.setattr(odds_mod.TheOddsApiSource, "fetch_all", fake_odds_api_fetch_all)
    monkeypatch.setattr(odds_mod.ParlayApiSource, "fetch_all", fake_parlay_fetch_all)

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert len(quotes) == 1
    assert diagnostics["provider_used"] == "odds_api"
    assert called_providers == ["odds_api"]  # backup never called -- primary succeeded
    assert diagnostics["attempts"][0]["status"] == "success"


def test_fallback_falls_through_on_primary_error(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")
    monkeypatch.setenv("PARLAY_API_KEY", "backup_key")

    def fake_odds_api_fetch_all(self, game_date, book="any"):
        raise ConnectionError("primary is down")

    def fake_parlay_fetch_all(self, game_date, book="any"):
        return ([HrOddsQuote(player="B", book="draftkings", opening_odds=250, current_odds=250, closing_odds=None)],
                {"rows_returned_raw": 1})

    monkeypatch.setattr(odds_mod.TheOddsApiSource, "fetch_all", fake_odds_api_fetch_all)
    monkeypatch.setattr(odds_mod.ParlayApiSource, "fetch_all", fake_parlay_fetch_all)

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert len(quotes) == 1
    assert quotes[0].player == "B"
    assert diagnostics["provider_used"] == "parlay_api"
    assert diagnostics["attempts"][0] == {"provider": "odds_api", "status": "error", "error": "primary is down"}
    assert diagnostics["attempts"][1]["status"] == "success"


def test_fallback_falls_through_on_empty_result_not_just_errors(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")
    monkeypatch.setenv("SHARPAPI_KEY", "backup2_key")

    def fake_odds_api_fetch_all(self, game_date, book="any"):
        return ([], {"games_found": 3})  # HTTP succeeded but nobody's posted lines yet

    def fake_sharp_fetch_all(self, game_date, book="any"):
        return ([HrOddsQuote(player="C", book="fanduel", opening_odds=200, current_odds=200, closing_odds=None)], {})

    monkeypatch.setattr(odds_mod.TheOddsApiSource, "fetch_all", fake_odds_api_fetch_all)
    monkeypatch.setattr(odds_mod.SharpApiSource, "fetch_all", fake_sharp_fetch_all)

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert len(quotes) == 1
    assert diagnostics["provider_used"] == "sharp_api"
    assert diagnostics["attempts"][0]["status"] == "empty"


def test_fallback_skips_providers_with_no_key_configured(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")
    # PARLAY_API_KEY and SHARPAPI_KEY intentionally not set

    def fake_odds_api_fetch_all(self, game_date, book="any"):
        return ([], {})

    monkeypatch.setattr(odds_mod.TheOddsApiSource, "fetch_all", fake_odds_api_fetch_all)

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert quotes == []
    assert diagnostics["provider_used"] is None
    statuses = {a["provider"]: a["status"] for a in diagnostics["attempts"]}
    assert statuses["odds_api"] == "empty"
    assert statuses["parlay_api"] == "skipped_no_key"
    assert statuses["sharp_api"] == "skipped_no_key"


def test_fallback_with_no_keys_at_all_returns_empty_gracefully(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert quotes == []
    assert diagnostics["provider_used"] is None
    assert all(a["status"] == "skipped_no_key" for a in diagnostics["attempts"])


def test_fallback_exhausts_all_three_and_reports_each_attempt(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "k1")
    monkeypatch.setenv("PARLAY_API_KEY", "k2")
    monkeypatch.setenv("SHARPAPI_KEY", "k3")

    monkeypatch.setattr(odds_mod.TheOddsApiSource, "fetch_all",
                         lambda self, game_date, book="any": (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(odds_mod.ParlayApiSource, "fetch_all", lambda self, game_date, book="any": ([], {}))
    monkeypatch.setattr(odds_mod.SharpApiSource, "fetch_all", lambda self, game_date, book="any": ([], {}))

    quotes, diagnostics = odds_mod.fetch_odds_with_fallback(datetime.date(2026, 7, 8))

    assert quotes == []
    assert diagnostics["provider_used"] is None
    assert len(diagnostics["attempts"]) == 3
    assert diagnostics["attempts"][0]["status"] == "error"
    assert diagnostics["attempts"][1]["status"] == "empty"
    assert diagnostics["attempts"][2]["status"] == "empty"


# ---------------------------------------------------------------------------
# TheOddsApiSource.fetch_game_totals / get_game_totals -- Vegas game totals
# ---------------------------------------------------------------------------

class _FakeTotalsResponse:
    def __init__(self, json_data, status_code=200, text=""):
        self._json = json_data
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        pass  # _get_events (used by fetch_game_totals) calls this on the /events response


def test_fetch_game_totals_basic(monkeypatch):
    import ingestion.odds as odds_mod

    events_payload = [
        {"id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "New York Yankees", "away_team": "Tampa Bay Rays"},
    ]
    odds_payload = {
        "bookmakers": [
            {"key": "fanduel", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 8.5, "price": -110},
                    {"name": "Under", "point": 8.5, "price": -110},
                ]},
            ]},
        ]
    }

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/events"):
            return _FakeTotalsResponse(events_payload)
        return _FakeTotalsResponse(odds_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.TheOddsApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    key = frozenset({"New York Yankees", "Tampa Bay Rays"})
    assert totals[key] == 8.5
    assert diagnostics["games_found"] == 1
    assert diagnostics["games"][0]["total_used"] == 8.5
    assert diagnostics["games"][0]["requested_book_has_totals_market"] is True


def test_fetch_game_totals_falls_back_to_any_available_book(monkeypatch):
    """Unlike fetch_all's HR odds, totals always falls back to whatever
    book has posted, even if the requested book hasn't.
    """
    import ingestion.odds as odds_mod

    events_payload = [
        {"id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "Boston Red Sox", "away_team": "Baltimore Orioles"},
    ]
    odds_payload = {
        "bookmakers": [
            {"key": "betrivers", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 9.0, "price": -105},
                ]},
            ]},
        ]
    }

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/events"):
            return _FakeTotalsResponse(events_payload)
        return _FakeTotalsResponse(odds_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.TheOddsApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    key = frozenset({"Boston Red Sox", "Baltimore Orioles"})
    assert totals[key] == 9.0
    assert diagnostics["games"][0]["requested_book_has_totals_market"] is False


def test_fetch_game_totals_no_market_posted_yet(monkeypatch):
    import ingestion.odds as odds_mod

    events_payload = [
        {"id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "Chicago Cubs", "away_team": "St. Louis Cardinals"},
    ]

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/events"):
            return _FakeTotalsResponse(events_payload)
        return _FakeTotalsResponse({"bookmakers": []})

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.TheOddsApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    assert totals == {}
    assert diagnostics["games"][0]["total_used"] is None


def test_fetch_game_totals_handles_error_status(monkeypatch):
    import ingestion.odds as odds_mod

    events_payload = [
        {"id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
         "home_team": "Seattle Mariners", "away_team": "Houston Astros"},
    ]

    def fake_get(url, params=None, timeout=None):
        if url.endswith("/events"):
            return _FakeTotalsResponse(events_payload)
        return _FakeTotalsResponse({}, status_code=500, text="server error")

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.TheOddsApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    assert totals == {}
    assert diagnostics["games"][0]["http_status"] == 500


def test_get_game_totals_no_key_configured(monkeypatch):
    import ingestion.odds as odds_mod
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("PARLAY_API_KEY", raising=False)

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert diagnostics["provider_used"] is None


def test_get_game_totals_success(monkeypatch):
    import ingestion.odds as odds_mod
    monkeypatch.setenv("ODDS_API_KEY", "fake_key")

    fake_totals = {frozenset({"New York Mets", "Atlanta Braves"}): 8.0}
    monkeypatch.setattr(
        odds_mod.TheOddsApiSource, "fetch_game_totals",
        lambda self, game_date, book="fanduel": (fake_totals, {"games_found": 1, "games": []}),
    )

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == fake_totals
    assert diagnostics["provider_used"] == "odds_api"


def test_get_game_totals_handles_exception_gracefully(monkeypatch):
    import ingestion.odds as odds_mod
    monkeypatch.setenv("ODDS_API_KEY", "fake_key")
    monkeypatch.setattr(
        odds_mod.TheOddsApiSource, "fetch_game_totals",
        lambda self, game_date, book="fanduel": (_ for _ in ()).throw(RuntimeError("down")),
    )

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert diagnostics["provider_used"] is None


# ---------------------------------------------------------------------------
# ParlayApiSource.fetch_game_totals -- backup provider's totals support
# ---------------------------------------------------------------------------

def test_parlay_fetch_game_totals_basic(monkeypatch):
    import ingestion.odds as odds_mod

    events_payload = [
        {
            "id": "evt1", "commence_time": "2026-07-08T23:00:00Z",
            "home_team": "New York Yankees", "away_team": "Tampa Bay Rays",
            "bookmakers": [
                {"key": "fanduel", "markets": [
                    {"key": "totals", "outcomes": [
                        {"name": "Over", "point": 8.5, "price": -110},
                        {"name": "Under", "point": 8.5, "price": -110},
                    ]},
                ]},
            ],
        },
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeJsonResponse(events_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    key = frozenset({"New York Yankees", "Tampa Bay Rays"})
    assert totals[key] == 8.5
    assert diagnostics["totals_matched"] == 1


def test_parlay_fetch_game_totals_filters_by_date(monkeypatch):
    import ingestion.odds as odds_mod

    events_payload = [
        {
            "id": "evt1", "commence_time": "2026-07-09T23:00:00Z",  # wrong date
            "home_team": "Boston Red Sox", "away_team": "Baltimore Orioles",
            "bookmakers": [{"key": "fanduel", "markets": [
                {"key": "totals", "outcomes": [{"name": "Over", "point": 9.0}]},
            ]}],
        },
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeJsonResponse(events_payload)

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8), book="fanduel")

    assert totals == {}


def test_parlay_fetch_game_totals_handles_error_status(monkeypatch):
    import ingestion.odds as odds_mod

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeJsonResponse({}, status_code=500, text="server error")

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert diagnostics["http_status"] == 500


def test_parlay_fetch_game_totals_handles_unexpected_shape_gracefully(monkeypatch):
    """If the response isn't a list of events (the documented-but-
    unverified shape assumption is wrong), this should surface a clear
    error in diagnostics rather than crash.
    """
    import ingestion.odds as odds_mod

    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeJsonResponse({"unexpected": "shape"})

    monkeypatch.setattr(odds_mod.requests, "get", fake_get)

    source = odds_mod.ParlayApiSource(api_key="fake_key")
    totals, diagnostics = source.fetch_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert "error" in diagnostics


# ---------------------------------------------------------------------------
# get_game_totals -- now falls through from odds_api to parlay_api
# ---------------------------------------------------------------------------

def test_get_game_totals_falls_through_to_parlay_when_odds_api_empty(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")
    monkeypatch.setenv("PARLAY_API_KEY", "backup_key")

    monkeypatch.setattr(
        odds_mod.TheOddsApiSource, "fetch_game_totals",
        lambda self, game_date, book="fanduel": ({}, {"games_found": 0, "games": []}),
    )
    fake_totals = {frozenset({"Chicago Cubs", "Miami Marlins"}): 7.5}
    monkeypatch.setattr(
        odds_mod.ParlayApiSource, "fetch_game_totals",
        lambda self, game_date, book="any": (fake_totals, {"events_found": 1}),
    )

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == fake_totals
    assert diagnostics["provider_used"] == "parlay_api"
    assert diagnostics["attempts"][0]["provider"] == "odds_api"
    assert diagnostics["attempts"][0]["status"] == "empty"
    assert diagnostics["attempts"][1]["provider"] == "parlay_api"
    assert diagnostics["attempts"][1]["status"] == "success"


def test_get_game_totals_skips_parlay_without_key(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "primary_key")

    monkeypatch.setattr(
        odds_mod.TheOddsApiSource, "fetch_game_totals",
        lambda self, game_date, book="fanduel": ({}, {"games_found": 0, "games": []}),
    )

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert diagnostics["provider_used"] is None
    assert diagnostics["attempts"][1] == {"provider": "parlay_api", "status": "skipped_no_key"}


def test_get_game_totals_all_providers_empty_returns_gracefully(monkeypatch):
    import ingestion.odds as odds_mod
    _clear_provider_keys(monkeypatch)
    monkeypatch.setenv("ODDS_API_KEY", "k1")
    monkeypatch.setenv("PARLAY_API_KEY", "k2")

    monkeypatch.setattr(
        odds_mod.TheOddsApiSource, "fetch_game_totals",
        lambda self, game_date, book="fanduel": ({}, {}),
    )
    monkeypatch.setattr(
        odds_mod.ParlayApiSource, "fetch_game_totals",
        lambda self, game_date, book="any": ({}, {}),
    )

    totals, diagnostics = odds_mod.get_game_totals(datetime.date(2026, 7, 8))

    assert totals == {}
    assert diagnostics["provider_used"] is None
    assert len(diagnostics["attempts"]) == 2
