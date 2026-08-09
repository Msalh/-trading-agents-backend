"""
Timing/Session Agent — Sprint 2.

Deliberately NOT an LLM call. This is pure, deterministic logic: given
a timestamp, determine whether it falls inside the London session,
the New York session, both (overlap), or neither, and whether it's a
weekday at all. Runs on every webhook before anything expensive
(Analysis Agent, once it exists) is triggered.

Session windows are configurable below. These are common trading
liquidity windows, not official exchange hours — MNQ itself trades
nearly 24 hours on Globex. Adjust LONDON_* / NY_* if you want a
tighter or wider window.
"""

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

LONDON_TZ = ZoneInfo("Europe/London")
NY_TZ = ZoneInfo("America/New_York")

LONDON_SESSION_START = time(8, 0)
LONDON_SESSION_END = time(16, 30)

NY_SESSION_START = time(8, 0)
NY_SESSION_END = time(17, 0)


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
    """Evaluate session timing for a given ISO-8601 UTC timestamp.

    Direction is always "neutral" — timing quality has no directional
    bias, it only gates whether now is a reasonable time to act at all.
    Confidence reflects liquidity quality: highest during the
    London/NY overlap, lower in a single session, lowest outside both.
    """
    dt_utc = _parse_utc(timestamp)
    is_weekday = dt_utc.weekday() < 5  # Mon=0 ... Sun=6

    london_local = dt_utc.astimezone(LONDON_TZ)
    ny_local = dt_utc.astimezone(NY_TZ)

    in_london = is_weekday and _in_window(london_local, LONDON_SESSION_START, LONDON_SESSION_END)
    in_ny = is_weekday and _in_window(ny_local, NY_SESSION_START, NY_SESSION_END)
    in_overlap = in_london and in_ny

    flags: list[str] = []

    if not is_weekday:
        confidence = 0
        session_label = "weekend"
        reasoning = "Weekend — no London or New York session active."
        flags.append("market_closed")
    elif in_overlap:
        confidence = 100
        session_label = "london_ny_overlap"
        reasoning = "Inside the London/New York overlap — highest expected liquidity window."
    elif in_london:
        confidence = 65
        session_label = "london"
        reasoning = "Inside the London session, outside the New York session."
    elif in_ny:
        confidence = 65
        session_label = "new_york"
        reasoning = "Inside the New York session, outside the London session."
    else:
        confidence = 20
        session_label = "outside_sessions"
        reasoning = "Outside both the London and New York sessions — low expected liquidity."
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
            "in_london_session": in_london,
            "in_ny_session": in_ny,
            "in_overlap": in_overlap,
            "london_local_time": london_local.strftime("%H:%M %Z"),
            "ny_local_time": ny_local.strftime("%H:%M %Z"),
        },
        flags=flags,
    )


def should_run_analysis(timing: TimingOpinion) -> bool:
    """The gate Sprint 2 exists to build: should the (future) Analysis
    Agent even run for this bar? Currently: yes if we're inside either
    session on a weekday."""
    return timing.key_data["in_london_session"] or timing.key_data["in_ny_session"]
