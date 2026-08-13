"""
Unit tests for app.timing_agent — ICT Kill Zone evaluation, including
the Tier 2.9 (calendar integrity) holiday-awareness fix. Pure logic,
no LLM, no network, no DB.

Run with: pytest tests/test_timing_agent.py -v
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.timing_agent import evaluate_timing, should_run_analysis

_NY_TZ = ZoneInfo("America/New_York")


def _ny_time_to_utc_iso(year, month, day, hour, minute) -> str:
    ny_dt = datetime(year, month, day, hour, minute, tzinfo=_NY_TZ)
    return ny_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Baseline kill-zone behavior (unaffected by Tier 2.9, but previously
# untested — establishing a baseline before the holiday tests below)
# ---------------------------------------------------------------------------

def test_ordinary_weekday_in_ny_kill_zone():
    ts = _ny_time_to_utc_iso(2026, 8, 11, 10, 0)  # Tuesday, NY AM kill zone
    timing = evaluate_timing(ts)
    assert timing.key_data["session_label"] == "new_york"
    assert timing.key_data["in_ny_session"] is True
    assert timing.confidence == 65
    assert timing.flags == []
    assert should_run_analysis(timing) is True


def test_ordinary_weekday_outside_kill_zones():
    ts = _ny_time_to_utc_iso(2026, 8, 11, 12, 0)  # Tuesday, gap between AM/PM kill zones
    timing = evaluate_timing(ts)
    assert timing.key_data["session_label"] == "outside_sessions"
    assert "low_liquidity" in timing.flags
    assert should_run_analysis(timing) is False


def test_weekend():
    ts = _ny_time_to_utc_iso(2026, 8, 15, 10, 0)  # Saturday
    timing = evaluate_timing(ts)
    assert timing.key_data["session_label"] == "weekend"
    assert timing.key_data["is_weekday"] is False
    assert "market_closed" in timing.flags
    assert should_run_analysis(timing) is False


# ---------------------------------------------------------------------------
# Tier 2.9: holiday awareness
# ---------------------------------------------------------------------------

def test_holiday_during_nominal_kill_zone_hours_is_flagged_market_closed():
    """Thanksgiving 2026 (Nov 26) is a Thursday — an ordinary weekday
    that would otherwise fall inside the NY AM kill zone at 10:00 NY
    time. Before Tier 2.9 this scored as a normal, full-confidence
    session; it must now be treated like a market closure."""
    ts = _ny_time_to_utc_iso(2026, 11, 26, 10, 0)
    timing = evaluate_timing(ts)
    assert timing.key_data["is_weekday"] is True
    assert timing.key_data["is_holiday"] is True
    assert timing.key_data["session_label"] == "holiday"
    assert timing.confidence == 0
    assert "market_closed" in timing.flags


def test_holiday_zeroes_out_every_kill_zone_flag():
    """The part that actually matters operationally: in_*_session must
    be False on a holiday, not just the display label — otherwise
    should_run_analysis() would still trigger a paid LLM call."""
    ts = _ny_time_to_utc_iso(2026, 11, 26, 10, 0)  # Thanksgiving, nominal NY AM kill zone hour
    timing = evaluate_timing(ts)
    assert timing.key_data["in_london_session"] is False
    assert timing.key_data["in_ny_session"] is False
    assert timing.key_data["in_ny_pm_session"] is False
    assert should_run_analysis(timing) is False


def test_non_holiday_weekday_still_runs_analysis_in_kill_zone():
    """Sanity check the fix didn't over-fire — an ordinary Tuesday one
    week before Thanksgiving still triggers analysis as normal."""
    ts = _ny_time_to_utc_iso(2026, 11, 19, 10, 0)  # ordinary Thursday-1-week, NY AM kill zone
    timing = evaluate_timing(ts)
    assert timing.key_data["is_holiday"] is False
    assert timing.key_data["in_ny_session"] is True
    assert should_run_analysis(timing) is True


def test_holiday_that_falls_on_weekend_stays_labeled_weekend_not_holiday():
    """July 4th 2026 is a Saturday — already weekend-closed regardless
    of the holiday calendar. Confirms the two checks don't double-flag
    or fight each other; "weekend" takes precedence since is_holiday
    is itself gated on is_weekday."""
    ts = _ny_time_to_utc_iso(2026, 7, 4, 10, 0)
    timing = evaluate_timing(ts)
    assert timing.key_data["session_label"] == "weekend"
    assert timing.key_data["is_holiday"] is False


def test_observed_holiday_the_friday_before_a_saturday_july_4th():
    """The OBSERVED holiday (Friday July 3rd 2026) is the day that
    actually needs the market_closed treatment, since it's a real
    weekday."""
    ts = _ny_time_to_utc_iso(2026, 7, 3, 10, 0)
    timing = evaluate_timing(ts)
    assert timing.key_data["is_weekday"] is True
    assert timing.key_data["is_holiday"] is True
    assert timing.key_data["session_label"] == "holiday"
