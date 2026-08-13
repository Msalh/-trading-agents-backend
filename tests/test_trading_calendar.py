"""
Unit tests for app.trading_calendar — Tier 2.9 (calendar integrity).
Pure date math, no LLM, no network, no DB.

Run with: pytest tests/test_trading_calendar.py -v
"""

from datetime import date

from app.trading_calendar import (
    check_trading_date,
    expected_trading_date,
    is_us_market_holiday,
    us_market_holidays,
)


# ---------------------------------------------------------------------------
# us_market_holidays / is_us_market_holiday
# ---------------------------------------------------------------------------

def test_2026_fixed_and_floating_holidays_match_known_dates():
    """Cross-checked against the real 2026 NYSE holiday calendar."""
    holidays = us_market_holidays(2026)
    assert date(2026, 1, 1) in holidays    # New Year's Day (Thursday)
    assert date(2026, 1, 19) in holidays   # MLK Day (3rd Mon Jan)
    assert date(2026, 2, 16) in holidays   # Presidents Day (3rd Mon Feb)
    assert date(2026, 4, 3) in holidays    # Good Friday
    assert date(2026, 5, 25) in holidays   # Memorial Day (last Mon May)
    assert date(2026, 6, 19) in holidays   # Juneteenth (Friday)
    assert date(2026, 9, 7) in holidays    # Labor Day (1st Mon Sep)
    assert date(2026, 11, 26) in holidays  # Thanksgiving (4th Thu Nov)
    assert len(holidays) == 10


def test_holiday_on_weekend_is_observed_on_nearest_weekday():
    """July 4th 2026 falls on a Saturday -> observed Friday July 3rd,
    not the Saturday date itself (which is already a weekend anyway)."""
    holidays_2026 = us_market_holidays(2026)
    assert date(2026, 7, 3) in holidays_2026   # observed
    assert date(2026, 7, 4) not in holidays_2026  # actual date, but it's a Saturday

    # Christmas 2027 falls on a Saturday -> observed Friday Dec 24th
    holidays_2027 = us_market_holidays(2027)
    assert date(2027, 12, 24) in holidays_2027
    assert date(2027, 12, 25) not in holidays_2027


def test_is_us_market_holiday_true_and_false_cases():
    assert is_us_market_holiday(date(2026, 11, 26)) is True   # Thanksgiving
    assert is_us_market_holiday(date(2026, 11, 25)) is False  # day before, ordinary Wednesday
    assert is_us_market_holiday(date(2026, 8, 11)) is False   # an ordinary Tuesday


def test_holidays_computed_correctly_across_multiple_years():
    """Easter/Good Friday is the trickiest (floating, algorithmic) —
    spot-check a few more years against known real dates."""
    assert date(2025, 4, 18) in us_market_holidays(2025)  # Good Friday 2025
    assert date(2027, 3, 26) in us_market_holidays(2027)  # Good Friday 2027
    assert date(2025, 11, 27) in us_market_holidays(2025)  # Thanksgiving 2025
    assert date(2025, 9, 1) in us_market_holidays(2025)    # Labor Day 2025


# ---------------------------------------------------------------------------
# expected_trading_date / check_trading_date
# ---------------------------------------------------------------------------

def test_expected_trading_date_same_day_before_rollover():
    # 10:00 NY time (14:00 UTC in EDT) -> well before the 18:00 rollover
    assert expected_trading_date("2026-08-11T14:00:00Z") == "2026-08-11"


def test_expected_trading_date_rolls_forward_after_rollover_hour():
    # 19:30 NY time (23:30 UTC in EDT) -> after 18:00 rollover -> next day's session
    assert expected_trading_date("2026-08-11T23:30:00Z") == "2026-08-12"


def test_check_trading_date_none_when_consistent():
    assert check_trading_date("2026-08-11T14:00:00Z", "2026-08-11") is None


def test_check_trading_date_warns_on_mismatch():
    warning = check_trading_date("2026-08-11T23:30:00Z", "2026-08-11")
    assert warning is not None
    assert "2026-08-11" in warning
    assert "2026-08-12" in warning  # the expected date is named in the message


def test_check_trading_date_handles_unparseable_timestamp_without_raising():
    warning = check_trading_date("not-a-timestamp", "2026-08-11")
    assert warning is not None
    assert "could not parse" in warning
