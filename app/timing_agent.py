"""
Timing/Session Agent — Sprint 2, redefined to ICT Kill Zones.

Deliberately NOT an LLM call. This is pure, deterministic logic: given
a timestamp, determine whether it falls inside the London Kill Zone,
the New York Kill Zone, both, or neither, and whether it's a weekday
at all. Runs on every webhook before anything expensive (Analysis
Agent) is triggered.

ICT Kill Zones are narrower, higher-conviction windows than generic
"session hours" — both are defined and quoted in New York time,
fixed year-round (ICT teaches these as NY wall-clock time; it does
NOT shift for London's own DST changes, only for NY's own EST/EDT
transitions, which zoneinfo handles automatically since we convert
through America/New_York).

  - London Kill Zone: 02:00–05:00 New York time
  - New York AM Kill Zone: 09:30–11:00 New York time (equity open)
  - New York PM Kill Zone: 13:00–15:00 New York time (afternoon session)

There are genuine gaps between them (05:00–09:30 and 11:00–13:00 NY
time) that are NOT part of any kill zone — deliberately left as dead
time, not patched with additional zones. Because the windows don't
touch, an "overlap" essentially never occurs with this narrower
definition; the field is kept for shape-compatibility with the rest
of the system but will rarely if ever be true.

Tier 2.9 (calendar integrity): the weekday check above is necessary
but not sufficient — a US market holiday (Thanksgiving, July 4th,
Christmas, etc.) is a WEEKDAY on which the cash equity market these
kill zones are built around is closed. Before this tier, a bar
timestamped during nominal kill-zone hours on a holiday was scored as
a normal, full-confidence session — wasting a paid Analysis LLM call
(should_run_analysis() would say yes) and letting the Coordinator
treat a shut market as an ordinary trading day. app/trading_calendar.py
now supplies a deterministic US holiday calendar; is_holiday folds
into is_london/is_ny/is_ny_pm exactly the same way is_weekday already
did, so a holiday correctly zeroes out every kill zone rather than
only affecting the display label.
"""

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.trading_calendar import is_us_market_holiday

NY_TZ = ZoneInfo("America/New_York")

LONDON_SESSION_START = time(2, 0)
LONDON_SESSION_END = time(5, 0)

NY_SESSION_START = time(9, 30)
NY_SESSION_END = time(11, 0)

NY_PM_SESSION_START = time(13, 0)
NY_PM_SESSION_END = time(15, 0)


@dataclass
class TimingOpinion:
    agent: str
    timestamp: str
    direction: str
    confidence: int
    reasoning: str
    key_data: dict
    flags: list[str]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "direction": self.direction,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
            "flags": self.flags,
        }


def _parse_utc(timestamp: str) -> datetime:
    """Parse an ISO-8601 'Z' timestamp (as sent by the Pine Script) into
    an aware UTC datetime."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _in_window(local_dt: datetime, start: time, end: time) -> bool:
    return start <= local_dt.time() <= end


def evaluate_timing(timestamp: str) -> TimingOpinion:
    """Evaluate ICT Kill Zone timing for a given ISO-8601 UTC timestamp.

    Direction is always "neutral" — timing quality has no directional
    bias, it only gates whether now is a reasonable time to act at all.
    Confidence reflects liquidity quality: highest during a kill zone,
    lowest outside all of them (including the dead gaps between them).
    """
    dt_utc = _parse_utc(timestamp)
    is_weekday = dt_utc.weekday() < 5  # Mon=0 ... Sun=6

    ny_local = dt_utc.astimezone(NY_TZ)
    # Kept only for informational display — all kill zones are
    # actually evaluated against NY time now, not London local time.
    london_local = dt_utc.astimezone(ZoneInfo("Europe/London"))

    # Tier 2.9: a US market holiday is a weekday the underlying cash
    # market is still closed on — folded into is_trading_day exactly
    # like is_weekday, so every in_* kill-zone flag (and therefore
    # should_run_analysis() below) is correctly False on a holiday,
    # not just the display label.
    is_holiday = is_weekday and is_us_market_holiday(ny_local.date())
    is_trading_day = is_weekday and not is_holiday

    in_london = is_trading_day and _in_window(ny_local, LONDON_SESSION_START, LONDON_SESSION_END)
    in_ny = is_trading_day and _in_window(ny_local, NY_SESSION_START, NY_SESSION_END)
    in_ny_pm = is_trading_day and _in_window(ny_local, NY_PM_SESSION_START, NY_PM_SESSION_END)
    in_overlap = in_london and in_ny  # structurally near-impossible with these windows

    flags: list[str] = []

    if not is_weekday:
        confidence = 0
        session_label = "weekend"
        reasoning = "Weekend — no kill zone active."
        flags.append("market_closed")
    elif is_holiday:
        confidence = 0
        session_label = "holiday"
        reasoning = "US market holiday — no kill zone active."
        flags.append("market_closed")
    elif in_overlap:
        confidence = 100
        session_label = "london_ny_overlap"
        reasoning = "Inside both the London and New York kill zones."
    elif in_london:
        confidence = 65
        session_label = "london"
        reasoning = "Inside the London Kill Zone (02:00-05:00 NY time)."
    elif in_ny:
        confidence = 65
        session_label = "new_york"
        reasoning = "Inside the New York AM Kill Zone (09:30-11:00 NY time)."
    elif in_ny_pm:
        confidence = 65
        session_label = "new_york_pm"
        reasoning = "Inside the New York PM Kill Zone (13:00-15:00 NY time)."
    else:
        confidence = 20
        session_label = "outside_sessions"
        reasoning = "Outside all kill zones — low expected liquidity/conviction."
        flags.append("low_liquidity")

    return TimingOpinion(
        agent="timing",
        timestamp=timestamp,
        direction="neutral",
        confidence=confidence,
        reasoning=reasoning,
        key_data={
            "session_label": session_label,
            "is_weekday": is_weekday,
            "is_holiday": is_holiday,
            "in_london_session": in_london,
            "in_ny_session": in_ny,
            "in_ny_pm_session": in_ny_pm,
            "in_overlap": in_overlap,
            "london_local_time": london_local.strftime("%H:%M %Z"),
            "ny_local_time": ny_local.strftime("%H:%M %Z"),
        },
        flags=flags,
    )


def should_run_analysis(timing: TimingOpinion) -> bool:
    """The gate Sprint 2 exists to build: should the (future) Analysis
    Agent even run for this bar? Currently: yes if we're inside any
    kill zone on a weekday that isn't a US market holiday (Tier 2.9 —
    in_*_session are already False on a holiday, so no separate
    is_holiday check is needed here)."""
    return (
        timing.key_data["in_london_session"]
        or timing.key_data["in_ny_session"]
        or timing.key_data["in_ny_pm_session"]
    )
