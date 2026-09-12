"""Static reference data for MLB ballparks: coordinates (for weather
lookups), roof type, and approximate orientation (for wind-direction
scoring).

CAVEATS -- read before trusting this data:

1. Coordinates and roof_type are well-established, stable facts (low
   risk of being wrong).

2. orientation_deg (the compass bearing from home plate toward center
   field, in degrees clockwise from north) is APPROXIMATE, compiled
   from general public references, not verified against an authoritative
   source in this build. Wind-direction scoring depends on this being
   roughly right -- worth double-checking against a canonical ballpark
   orientation chart before trusting it for real decisions.

3. Team-to-venue mapping can change (relocations, temporary/damaged
   stadiums, sponsor renamings, etc.) and this table is not guaranteed
   current -- confirmed live: MLB Stats API returned "loanDepot park"
   (lowercase p, a casing mismatch against this table), "Rate Field"
   (Guaranteed Rate Field's 2025 sponsor rebrand), "UNIQLO Field at
   Dodger Stadium" (a 2026 partial-naming addition to Dodger Stadium),
   and "Sutter Health Park" (the Athletics' temporary home in West
   Sacramento while their new stadium is built -- a genuinely different
   physical location from Oakland Coliseum, not a rename of it).

   Two defenses against this, neither of which requires catching every
   future rename by hand:
     a) Lookups are now case-insensitive (fixes the loanDepot casing
        class of issue automatically, going forward, for any park).
     b) ALIASES below maps known alternate/sponsor names to a canonical
        BALLPARKS entry -- add a line here when a new rename is found,
        rather than needing to touch coordinates/roof/orientation again.

   Callers should look up by the venue NAME returned live by
   ingestion.mlb_data.fetch_games() (not by team), and
   ingestion.weather.get_game_weather() is written to fail gracefully
   (return an "unknown venue" result, not crash) if a venue name still
   isn't resolvable -- exactly to guard against this table going stale
   again in some new way.

roof_type values:
    "open_air"    -- no roof
    "fixed_dome"  -- permanently closed roof (fully climate-controlled)
    "retractable" -- can be open or closed; THIS MODULE HAS NO WAY TO
                     KNOW the actual per-game status (no free public API
                     for it) and defaults to assuming CLOSED for these
                     parks. See ingestion/weather.py for the TODO on
                     sourcing real roof status.
"""

# venue name (as returned by MLB Stats API) -> reference data
BALLPARKS = {
    "Angel Stadium":            {"lat": 33.8003, "lon": -117.8827, "roof_type": "open_air", "orientation_deg": 20},
    "Chase Field":               {"lat": 33.4455, "lon": -112.0667, "roof_type": "retractable", "orientation_deg": 5},
    "Truist Park":                {"lat": 33.8908, "lon": -84.4678, "roof_type": "open_air", "orientation_deg": 155},
    "Oriole Park at Camden Yards": {"lat": 39.2839, "lon": -76.6218, "roof_type": "open_air", "orientation_deg": 25},
    "Fenway Park":                 {"lat": 42.3467, "lon": -71.0972, "roof_type": "open_air", "orientation_deg": 45},
    "Wrigley Field":                {"lat": 41.9484, "lon": -87.6553, "roof_type": "open_air", "orientation_deg": 30},
    "Rate Field":                     {"lat": 41.8299, "lon": -87.6338, "roof_type": "open_air", "orientation_deg": 155},
    "Great American Ball Park":       {"lat": 39.0979, "lon": -84.5066, "roof_type": "open_air", "orientation_deg": 100},
    "Progressive Field":               {"lat": 41.4962, "lon": -81.6852, "roof_type": "open_air", "orientation_deg": 5},
    "Coors Field":                      {"lat": 39.7559, "lon": -104.9942, "roof_type": "open_air", "orientation_deg": 15},
    "Comerica Park":                     {"lat": 42.3390, "lon": -83.0485, "roof_type": "open_air", "orientation_deg": 140},
    "Minute Maid Park":                   {"lat": 29.7573, "lon": -95.3555, "roof_type": "retractable", "orientation_deg": 345},
    "Kauffman Stadium":                    {"lat": 39.0517, "lon": -94.4803, "roof_type": "open_air", "orientation_deg": 40},
    "Dodger Stadium":                       {"lat": 34.0739, "lon": -118.2400, "roof_type": "open_air", "orientation_deg": 30},
    "loanDepot Park":                        {"lat": 25.7781, "lon": -80.2196, "roof_type": "retractable", "orientation_deg": 40},
    "American Family Field":                  {"lat": 43.0280, "lon": -87.9712, "roof_type": "retractable", "orientation_deg": 130},
    "Target Field":                            {"lat": 44.9817, "lon": -93.2776, "roof_type": "open_air", "orientation_deg": 90},
    "Citi Field":                               {"lat": 40.7571, "lon": -73.8458, "roof_type": "open_air", "orientation_deg": 30},
    "Yankee Stadium":                            {"lat": 40.8296, "lon": -73.9262, "roof_type": "open_air", "orientation_deg": 75},
    "Oakland Coliseum":                           {"lat": 37.7516, "lon": -122.2005, "roof_type": "open_air", "orientation_deg": 55},
    # Athletics' temporary home (2025+) while their Las Vegas stadium is
    # built -- a genuinely different physical location from Oakland
    # Coliseum above, not a rename of it. Coordinates approximate
    # (West Sacramento riverfront); orientation_deg is a LOW-CONFIDENCE
    # guess, not sourced from anything -- this is a minor-league park
    # (Sacramento River Cats' home) not previously in this table at all,
    # so treat wind scoring for this venue with extra skepticism until
    # verified.
    "Sutter Health Park":                         {"lat": 38.5805, "lon": -121.5122, "roof_type": "open_air", "orientation_deg": 45},
    "Citizens Bank Park":                          {"lat": 39.9061, "lon": -75.1665, "roof_type": "open_air", "orientation_deg": 15},
    "PNC Park":                                     {"lat": 40.4469, "lon": -80.0057, "roof_type": "open_air", "orientation_deg": 110},
    "Petco Park":                                    {"lat": 32.7073, "lon": -117.1566, "roof_type": "open_air", "orientation_deg": 340},
    "Oracle Park":                                    {"lat": 37.7786, "lon": -122.3893, "roof_type": "open_air", "orientation_deg": 95},
    "T-Mobile Park":                                   {"lat": 47.5914, "lon": -122.3325, "roof_type": "retractable", "orientation_deg": 45},
    "Busch Stadium":                                    {"lat": 38.6226, "lon": -90.1928, "roof_type": "open_air", "orientation_deg": 60},
    "Tropicana Field":                                   {"lat": 27.7683, "lon": -82.6534, "roof_type": "fixed_dome", "orientation_deg": 45},
    "Globe Life Field":                                   {"lat": 32.7473, "lon": -97.0842, "roof_type": "retractable", "orientation_deg": 30},
    "Rogers Centre":                                       {"lat": 43.6414, "lon": -79.3894, "roof_type": "retractable", "orientation_deg": 75},
    "Nationals Park":                                       {"lat": 38.8730, "lon": -77.0074, "roof_type": "open_air", "orientation_deg": 30},
}

# Alternate/sponsor/legacy names -> canonical BALLPARKS key. Add a line
# here whenever a new venue-name mismatch is found in live warnings --
# no need to duplicate coordinates/roof/orientation, just point at the
# existing entry.
ALIASES = {
    "Guaranteed Rate Field": "Rate Field",           # 2025 sponsor rebrand
    "UNIQLO Field at Dodger Stadium": "Dodger Stadium",  # 2026 naming addition
    "Daikin Park": "Minute Maid Park",               # 2025 Astros sponsor rebrand
}


def get_ballpark_reference(venue_name: str) -> dict | None:
    """Look up a venue by name. Returns None if not found rather than
    raising -- callers should handle unknown venues gracefully (see
    module docstring caveat #3).

    Resolution order:
        1. Exact match against BALLPARKS.
        2. Exact match against ALIASES (sponsor renames, legacy names).
        3. Case-insensitive match against both of the above (handles
           casing drift like "loanDepot park" vs "loanDepot Park"
           automatically, without needing an explicit alias entry).
    """
    if venue_name in BALLPARKS:
        return BALLPARKS[venue_name]
    if venue_name in ALIASES:
        return BALLPARKS[ALIASES[venue_name]]

    venue_lower = venue_name.lower()
    for key, data in BALLPARKS.items():
        if key.lower() == venue_lower:
            return data
    for alias, canonical in ALIASES.items():
        if alias.lower() == venue_lower:
            return BALLPARKS[canonical]

    return None
