from ingestion.ballpark_reference import BALLPARKS, ALIASES, get_ballpark_reference


def test_all_ballparks_have_required_fields():
    # 31, not 30 -- Sutter Health Park (Athletics' temporary home) was
    # added as a genuinely new, distinct entry alongside Oakland
    # Coliseum, not a replacement for it.
    assert len(BALLPARKS) == 31
    for venue, data in BALLPARKS.items():
        assert "lat" in data and -90 <= data["lat"] <= 90
        assert "lon" in data and -180 <= data["lon"] <= 180
        assert data["roof_type"] in ("open_air", "fixed_dome", "retractable")
        assert 0 <= data["orientation_deg"] < 360


def test_get_ballpark_reference_known_venue():
    result = get_ballpark_reference("Yankee Stadium")
    assert result is not None
    assert result["roof_type"] == "open_air"


def test_get_ballpark_reference_unknown_venue_returns_none():
    assert get_ballpark_reference("Not A Real Stadium") is None


def test_aliases_all_resolve_to_real_ballparks():
    for alias, canonical in ALIASES.items():
        assert canonical in BALLPARKS, f"alias {alias!r} points at missing key {canonical!r}"


def test_sponsor_rename_alias_resolves(monkeypatch=None):
    # Regression test: the exact venue name MLB Stats API returned live
    # for the White Sox's park after its 2025 sponsor rebrand.
    result = get_ballpark_reference("Rate Field")
    assert result is not None
    legacy_result = get_ballpark_reference("Guaranteed Rate Field")
    assert legacy_result == result  # same underlying data either name


def test_dodger_stadium_naming_addition_alias_resolves():
    result = get_ballpark_reference("UNIQLO Field at Dodger Stadium")
    assert result is not None
    assert result == get_ballpark_reference("Dodger Stadium")


def test_lookup_is_case_insensitive():
    # Regression test: MLB Stats API returned "loanDepot park" (lowercase
    # p) live, which didn't match this table's "loanDepot Park" (capital P).
    result = get_ballpark_reference("loanDepot park")
    assert result is not None
    assert result == get_ballpark_reference("loanDepot Park")


def test_case_insensitive_lookup_also_covers_aliases():
    result = get_ballpark_reference("rate field")  # lowercase, no direct match
    assert result is not None
    assert result == get_ballpark_reference("Rate Field")


def test_sutter_health_park_is_a_real_distinct_entry():
    # Confirms it's genuinely a new, separate venue -- not accidentally
    # aliased to Oakland Coliseum (different physical location).
    sutter = get_ballpark_reference("Sutter Health Park")
    oakland = get_ballpark_reference("Oakland Coliseum")
    assert sutter is not None
    assert oakland is not None
    assert sutter != oakland
