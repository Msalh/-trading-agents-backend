"""
Real US economic-calendar event registry — Tier 3.28 (News urgent vs.
deterministic economic-calendar blackout baseline, sixth external
review, ranked backlog item #2).

The reviewer's exact ask (relayed verbatim): "قارنه بحظر بسيط مبني على
تقويم اقتصادي موثوق: امتنع قبل/بعد CPI/FOMC/NFP. إذا كان LLM لا يتفوق
على blackout ثابت، فلا يوجد سبب لدفع تكلفته أو الاعتماد على تصنيفه
الحر." (Compare News's "urgent" flag against a simple blackout built on
a trustworthy economic calendar: abstain before/after CPI/FOMC/NFP. If
the LLM doesn't outperform a fixed blackout, there's no reason to pay
its cost or rely on its free-text classification.)

This module is the "trustworthy economic calendar" half of that ask —
a hardcoded, source-cited registry of real 2026 CPI/NFP/FOMC release
timestamps, plus a deterministic in_blackout_window check. It is
intentionally NOT derived from anything News or Macro ever said; it
exists so app.coordinator_diagnostics.compute_news_urgent_vs_calendar_
blackout() can compare News's self-reported "urgent" flag against a
classifier that has no access to News's own reasoning at all.

Every date/time below was pulled directly from the three official
primary sources for calendar-year 2026 (not estimated, not
extrapolated from a prior year's day-of-month pattern):

  - CPI and the Employment Situation report (nonfarm payrolls, "NFP"):
    the official BLS release schedule (https://www.bls.gov/schedule/),
    cross-checked against the White House's "Schedule of Release Dates
    for Principal Federal Economic Indicators, CY2026" PDF
    (https://www.whitehouse.gov/wp-content/uploads/2025/09/pfei_schedule_release_dates_cy2026.pdf).
    Both list every 2026 release for these two series at 8:30 AM
    Eastern Time — that convention is applied uniformly below, each
    converted to UTC for that specific date accounting for US DST
    (starts 2026-03-08, ends 2026-11-01; date-specific, not a fixed
    offset, since three CPI dates and three NFP dates below straddle
    a DST boundary relative to each other within the same year).
  - FOMC: the Federal Reserve's own 2026 meeting calendar
    (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm),
    cross-checked against the per-month press-release calendar pages
    (e.g. https://www.federalreserve.gov/newsevents/2026-august.htm)
    for meetings near the current data window. All eight 2026 meetings
    are two-day sessions; the rate decision and press conference land
    on the SECOND day at 2:00 PM Eastern — that is the timestamp
    recorded below (not the meeting's first day), since that is the
    actual moment new information reaches the market. August 2026 has
    no FOMC meeting at all (the July 28-29 meeting's minutes release
    on August 19 is not a new decision and is deliberately NOT included
    below — it carries nowhere near the same market-moving weight as a
    live rate decision, and including it would blur the exact
    CPI/FOMC/NFP scope the reviewer asked for).

Retrieved 2026-08-24 for this tier's build, covering the full 2026
calendar year so the registry keeps working as more trading data
accumulates without another research pass for several months. If a
future session needs 2027+ dates, re-pull from the same three sources
rather than extrapolating a pattern forward — release schedules are
set year by year and are not on a fixed day-of-month rule (see how
irregularly the dates below actually fall).

Cross-checked against the live production candidate history at build
time: the current data window (2026-08-12 through 2026-08-24, 9
trading days) contains exactly ONE of these events — the 2026-08-12
CPI release, which is also the very first day of the observed window.
No FOMC meeting falls in August 2026, and the closest NFP release
(2026-08-07) falls before the window starts. This means any
comparison run against the current live data will have very thin
statistical power (at most a handful of candidates near a single real
event) — see compute_news_urgent_vs_calendar_blackout()'s
calendar_coverage field, which reports this honestly rather than
letting a thin sample read as a confident result. Statistical power
improves automatically as more weeks of data accumulate: 2026-09-04
(NFP), 2026-09-11 (CPI), and 2026-09-15/16 (FOMC) are all real events
already in the registry below, waiting for the trading window to reach
them.
"""

from datetime import datetime, timedelta

# Sorted chronologically. "event" is one of "CPI", "NFP", "FOMC".
# "timestamp_utc" is the exact release/decision moment in UTC.
MAJOR_US_ECONOMIC_EVENTS_2026 = [
    {"event": "NFP", "date": "2026-01-09", "timestamp_utc": "2026-01-09T13:30:00Z"},
    {"event": "CPI", "date": "2026-01-13", "timestamp_utc": "2026-01-13T13:30:00Z"},
    {"event": "FOMC", "date": "2026-01-28", "timestamp_utc": "2026-01-28T19:00:00Z"},
    {"event": "NFP", "date": "2026-02-06", "timestamp_utc": "2026-02-06T13:30:00Z"},
    {"event": "CPI", "date": "2026-02-11", "timestamp_utc": "2026-02-11T13:30:00Z"},
    {"event": "NFP", "date": "2026-03-06", "timestamp_utc": "2026-03-06T13:30:00Z"},
    {"event": "CPI", "date": "2026-03-11", "timestamp_utc": "2026-03-11T12:30:00Z"},
    {"event": "FOMC", "date": "2026-03-18", "timestamp_utc": "2026-03-18T18:00:00Z"},
    {"event": "NFP", "date": "2026-04-03", "timestamp_utc": "2026-04-03T12:30:00Z"},
    {"event": "CPI", "date": "2026-04-10", "timestamp_utc": "2026-04-10T12:30:00Z"},
    {"event": "FOMC", "date": "2026-04-29", "timestamp_utc": "2026-04-29T18:00:00Z"},
    {"event": "NFP", "date": "2026-05-08", "timestamp_utc": "2026-05-08T12:30:00Z"},
    {"event": "CPI", "date": "2026-05-12", "timestamp_utc": "2026-05-12T12:30:00Z"},
    {"event": "NFP", "date": "2026-06-05", "timestamp_utc": "2026-06-05T12:30:00Z"},
    {"event": "CPI", "date": "2026-06-10", "timestamp_utc": "2026-06-10T12:30:00Z"},
    {"event": "FOMC", "date": "2026-06-17", "timestamp_utc": "2026-06-17T18:00:00Z"},
    {"event": "NFP", "date": "2026-07-02", "timestamp_utc": "2026-07-02T12:30:00Z"},
    {"event": "CPI", "date": "2026-07-14", "timestamp_utc": "2026-07-14T12:30:00Z"},
    {"event": "FOMC", "date": "2026-07-29", "timestamp_utc": "2026-07-29T18:00:00Z"},
    {"event": "NFP", "date": "2026-08-07", "timestamp_utc": "2026-08-07T12:30:00Z"},
    {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"},
    {"event": "NFP", "date": "2026-09-04", "timestamp_utc": "2026-09-04T12:30:00Z"},
    {"event": "CPI", "date": "2026-09-11", "timestamp_utc": "2026-09-11T12:30:00Z"},
    {"event": "FOMC", "date": "2026-09-16", "timestamp_utc": "2026-09-16T18:00:00Z"},
    {"event": "NFP", "date": "2026-10-02", "timestamp_utc": "2026-10-02T12:30:00Z"},
    {"event": "CPI", "date": "2026-10-14", "timestamp_utc": "2026-10-14T12:30:00Z"},
    {"event": "FOMC", "date": "2026-10-28", "timestamp_utc": "2026-10-28T18:00:00Z"},
    {"event": "NFP", "date": "2026-11-06", "timestamp_utc": "2026-11-06T13:30:00Z"},
    {"event": "CPI", "date": "2026-11-10", "timestamp_utc": "2026-11-10T13:30:00Z"},
    {"event": "NFP", "date": "2026-12-04", "timestamp_utc": "2026-12-04T13:30:00Z"},
    {"event": "FOMC", "date": "2026-12-09", "timestamp_utc": "2026-12-09T19:00:00Z"},
    {"event": "CPI", "date": "2026-12-10", "timestamp_utc": "2026-12-10T13:30:00Z"},
]


def _parse(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def is_within_blackout_window(bar_timestamp: str, window_hours: float = 2.0) -> dict:
    """Deterministic, News-independent check: is `bar_timestamp` within
    `window_hours` of the NEAREST real CPI/NFP/FOMC event in the
    registry above (before or after — the reviewer's "قبل/بعد" asked
    for both directions)? Always returns the nearest event and its
    distance even when not in blackout, so callers can inspect
    near-misses rather than only a bare boolean."""
    target = _parse(bar_timestamp)
    nearest_event = None
    nearest_distance_hours = None
    for event in MAJOR_US_ECONOMIC_EVENTS_2026:
        event_time = _parse(event["timestamp_utc"])
        distance_hours = abs((target - event_time).total_seconds()) / 3600.0
        if nearest_distance_hours is None or distance_hours < nearest_distance_hours:
            nearest_distance_hours = distance_hours
            nearest_event = event
    in_blackout = nearest_distance_hours is not None and nearest_distance_hours <= window_hours
    return {
        "in_blackout": in_blackout,
        "window_hours": window_hours,
        "nearest_event": nearest_event,
        "distance_hours": round(nearest_distance_hours, 3) if nearest_distance_hours is not None else None,
    }


def events_overlapping_range(range_start_iso: str, range_end_iso: str, window_hours: float = 2.0) -> list[dict]:
    """Events whose blackout window (event_time ± window_hours)
    overlaps [range_start_iso, range_end_iso] — i.e. events that
    actually had a chance to produce an in_blackout=True result
    somewhere in this specific data pull, not merely events that fall
    calendar-wise inside the range. Used purely for honest coverage
    reporting (see module docstring): a caller comparing urgent vs.
    blackout over a short window should know up front how many real
    events, if any, that window could possibly have caught."""
    start = _parse(range_start_iso)
    end = _parse(range_end_iso)
    window = timedelta(hours=window_hours)
    overlapping = []
    for event in MAJOR_US_ECONOMIC_EVENTS_2026:
        event_time = _parse(event["timestamp_utc"])
        if event_time + window >= start and event_time - window <= end:
            overlapping.append(event)
    return overlapping
