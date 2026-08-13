"""
Trading Calendar — Tier 2.9 (calendar integrity, external review, Aug
2026).

Centralizes two related but previously missing/incomplete concerns:

1. US market holidays. timing_agent.py's weekday check (Mon-Fri) is
   necessary but not sufficient — a fixed-date or floating US market
   holiday (Thanksgiving, July 4th, Christmas, etc.) is a WEEKDAY on
   which the cash equity market ICT Kill Zones are built around is
   fully closed. Before this tier, a stray or legitimate Globex bar
   timestamped on a holiday during nominal kill-zone hours would still
   be scored as a normal, full-confidence kill zone — both wasting a
   paid Analysis LLM call (should_run_analysis() would say yes) and
   letting the Coordinator treat a shut market as a normal trading
   session. Deterministic, no network/LLM: holidays are computed
   per-year from well-known US holiday rules (fixed dates observed on
   the nearest weekday when they fall on a weekend, "nth weekday of
   month" floating holidays, and Good Friday via the Anonymous
   Gregorian Easter algorithm) — no calendar file or external
   dependency to keep in sync.

2. Trading-day/timestamp consistency. Every incoming bar carries a
   Pine-Script-computed `trading_date` field alongside `timestamp`.
   Nothing previously verified the two actually agree — a Pine Script
   bug, a DST edge case, or clock skew on the sending side could
   silently send a bar whose trading_date doesn't match what its own
   timestamp implies, corrupting anything keyed by trading date later
   (the dashboard, any future per-day P&L rollup). check_trading_date()
   applies the standard CME/Globex session-rollover convention (NY
   local time at/after 18:00 belongs to the NEXT calendar day's
   trading session) and flags a mismatch — surfaced as a warning on
   the webhook response and in the logs, not silently rejected, since
   failing ingestion outright over a data source we don't control is
   worse than a flagged anomaly a human can go check.

Known limitation, stated plainly rather than left implicit: this
models the NYSE-style ten-holiday full-closure calendar (New Year's,
MLK Day, Presidents Day, Good Friday, Memorial Day, Juneteenth, July
4th, Labor Day, Thanksgiving, Christmas). Real CME Globex futures
trading (MNQ specifically trades a near-24/5 week with only short
daily maintenance breaks) actually observes a more nuanced schedule —
early closes on several of these rather than full closures, and it
may still see some overnight activity even on a "closed" cash-market
holiday. Treating all ten as full closures is conservative (flags more
than CME technically halts for) rather than exact — good enough to
stop a holiday being scored as an ordinary kill zone; a maintainer
wanting exact CME hours should replace _holiday computation with a
real CME calendar feed.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

# CME/Globex session rollover: a bar timestamped at/after this NY
# local hour belongs to the NEXT calendar day's trading session —
# mirrors the real futures convention (Sunday ~6pm ET open, and each
# weekday's session rolling into the next calendar day's session after
# the daily maintenance break).
_SESSION_ROLLOVER_HOUR = 18


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the n-th occurrence of `weekday` (Mon=0..Sun=6) in
    a given month/year — e.g. n=3 for "3rd Monday of January" (MLK
    Day)."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """e.g. "last Monday of May" (Memorial Day)."""
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (a.k.a. Meeus/Jones/Butcher) —
    computes Easter Sunday with plain integer arithmetic, no external
    dependency. Good Friday is two days before it."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """Standard NYSE convention for a fixed-date holiday: observed the
    preceding Friday if it falls on a Saturday, the following Monday
    if it falls on a Sunday, otherwise on the date itself."""
    if d.weekday() == 5:  # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def us_market_holidays(year: int) -> set[date]:
    """The observed dates of the ten NYSE-style full-closure holidays
    for a given year. Computed fresh on every call — cheap (ten date
    calculations) and callers are infrequent (at most once per webhook
    bar), so there's no need to cache/memoize by year."""
    good_friday = _easter_sunday(year) - timedelta(days=2)
    return {
        _observed(date(year, 1, 1)),            # New Year's Day
        _nth_weekday_of_month(year, 1, 0, 3),    # MLK Day (3rd Mon Jan)
        _nth_weekday_of_month(year, 2, 0, 3),    # Presidents Day (3rd Mon Feb)
        good_friday,
        _last_weekday_of_month(year, 5, 0),      # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),            # Juneteenth
        _observed(date(year, 7, 4)),             # Independence Day
        _nth_weekday_of_month(year, 9, 0, 1),    # Labor Day (1st Mon Sep)
        _nth_weekday_of_month(year, 11, 3, 4),   # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),           # Christmas
    }


def is_us_market_holiday(d: date) -> bool:
    return d in us_market_holidays(d.year)


def _parse_utc(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def expected_trading_date(timestamp: str) -> str:
    """The CME/Globex trading-day a bar's timestamp belongs to, as a
    "YYYY-MM-DD" string: NY local time at/after the session-rollover
    hour (18:00) belongs to the NEXT calendar day's session — the same
    convention Pine's own trading_date field is expected to follow."""
    ny_local = _parse_utc(timestamp).astimezone(NY_TZ)
    trading_day = ny_local.date()
    if ny_local.hour >= _SESSION_ROLLOVER_HOUR:
        trading_day += timedelta(days=1)
    return trading_day.strftime("%Y-%m-%d")


def check_trading_date(timestamp: str, trading_date: str) -> str | None:
    """Returns None if trading_date matches what the timestamp implies
    under the CME/Globex rollover convention above, else a short
    human-readable warning string describing the mismatch. Never
    raises — an unparseable timestamp is itself the finding, reported
    as a warning rather than crashing bar ingestion over a malformed
    field."""
    try:
        expected = expected_trading_date(timestamp)
    except (ValueError, AttributeError, TypeError):
        return f"could not parse timestamp {timestamp!r} to validate trading_date"
    if trading_date != expected:
        return (
            f"trading_date={trading_date!r} does not match the date implied by "
            f"timestamp={timestamp!r} under the CME/Globex session-rollover "
            f"convention (expected {expected!r})"
        )
    return None
