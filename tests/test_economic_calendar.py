"""
Unit tests for app.economic_calendar — Tier 3.28. No LLM, no network,
no DB: pure date/time math over the hardcoded registry.

Run with: pytest tests/test_economic_calendar.py -v
"""

from app.economic_calendar import (
    MAJOR_US_ECONOMIC_EVENTS_2026,
    events_overlapping_range,
    is_within_blackout_window,
)


def test_registry_has_expected_event_counts():
    # 12 months x (1 CPI + 1 NFP) + 8 FOMC meetings in 2026.
    by_type = {}
    for event in MAJOR_US_ECONOMIC_EVENTS_2026:
        by_type[event["event"]] = by_type.get(event["event"], 0) + 1
    assert by_type == {"CPI": 12, "NFP": 12, "FOMC": 8}
    assert len(MAJOR_US_ECONOMIC_EVENTS_2026) == 32


def test_registry_is_sorted_chronologically():
    timestamps = [e["timestamp_utc"] for e in MAJOR_US_ECONOMIC_EVENTS_2026]
    assert timestamps == sorted(timestamps)


def test_registry_entries_have_consistent_shape():
    for event in MAJOR_US_ECONOMIC_EVENTS_2026:
        assert set(event.keys()) == {"event", "date", "timestamp_utc"}
        assert event["event"] in ("CPI", "NFP", "FOMC")
        assert event["timestamp_utc"].startswith(event["date"])
        assert event["timestamp_utc"].endswith("Z")


def test_within_blackout_window_true_shortly_after_event():
    # 30 minutes after the real 2026-08-12 CPI release (12:30:00Z).
    result = is_within_blackout_window("2026-08-12T13:00:00Z", window_hours=2.0)
    assert result["in_blackout"] is True
    assert result["nearest_event"] == {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"}
    assert result["distance_hours"] == 0.5


def test_within_blackout_window_true_shortly_before_event():
    # 45 minutes BEFORE the same CPI release -- the reviewer's ask was
    # explicitly "before OR after" (قبل/بعد).
    result = is_within_blackout_window("2026-08-12T11:45:00Z", window_hours=2.0)
    assert result["in_blackout"] is True
    assert result["nearest_event"]["date"] == "2026-08-12"
    assert result["distance_hours"] == 0.75


def test_within_blackout_window_false_when_far_from_any_event():
    # Same calendar day as the CPI release, but 7.5 hours later --
    # outside the default 2-hour window and not close to any other
    # registry event either.
    result = is_within_blackout_window("2026-08-12T20:00:00Z", window_hours=2.0)
    assert result["in_blackout"] is False
    # Still reports the nearest event and its distance, even though it
    # isn't a blackout -- callers can inspect near-misses.
    assert result["nearest_event"]["date"] == "2026-08-12"
    assert result["distance_hours"] == 7.5


def test_within_blackout_window_boundary_is_inclusive():
    # Exactly at the window edge (2.0 hours after the CPI release).
    result = is_within_blackout_window("2026-08-12T14:30:00Z", window_hours=2.0)
    assert result["distance_hours"] == 2.0
    assert result["in_blackout"] is True
    # Just past the edge -- no longer in blackout.
    result = is_within_blackout_window("2026-08-12T14:30:01Z", window_hours=2.0)
    assert result["in_blackout"] is False


def test_within_blackout_window_respects_custom_width():
    # 1.5 hours after the CPI release: inside a 2-hour window, outside
    # a tighter 1-hour window.
    timestamp = "2026-08-12T14:00:00Z"
    assert is_within_blackout_window(timestamp, window_hours=2.0)["in_blackout"] is True
    assert is_within_blackout_window(timestamp, window_hours=1.0)["in_blackout"] is False


def test_within_blackout_window_picks_the_nearer_of_two_events():
    # 2026-01-28T19:00:00Z (FOMC) is much closer to 2026-01-28T20:00:00Z
    # than 2026-01-13T13:30:00Z (CPI, over two weeks earlier) is.
    result = is_within_blackout_window("2026-01-28T20:00:00Z", window_hours=2.0)
    assert result["nearest_event"] == {"event": "FOMC", "date": "2026-01-28", "timestamp_utc": "2026-01-28T19:00:00Z"}
    assert result["distance_hours"] == 1.0


def test_events_overlapping_range_finds_one_real_event():
    # A narrow range around the CPI release's blackout window.
    overlapping = events_overlapping_range("2026-08-12T10:00:00Z", "2026-08-12T15:00:00Z", window_hours=2.0)
    assert len(overlapping) == 1
    assert overlapping[0]["date"] == "2026-08-12"


def test_events_overlapping_range_empty_when_no_event_nearby():
    overlapping = events_overlapping_range("2026-08-12T18:00:00Z", "2026-08-12T22:00:00Z", window_hours=2.0)
    assert overlapping == []


def test_events_overlapping_range_matches_the_full_production_window():
    # Cross-checked against live production at build time (2026-08-24):
    # the actual 9-trading-day candidate history window (2026-08-12
    # through 2026-08-24) should surface exactly the one CPI event on
    # its very first day, and nothing else -- no August FOMC meeting,
    # and the nearest NFP (2026-08-07) falls before the window starts.
    overlapping = events_overlapping_range("2026-08-12T00:00:00Z", "2026-08-24T23:59:59Z", window_hours=2.0)
    assert len(overlapping) == 1
    assert overlapping[0] == {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"}
