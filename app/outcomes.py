"""
Outcome tracking — evaluates whether a past Coordinator decision was
directionally correct, at several time horizons after the decision.

Deliberately computed on-demand (a query, not a background job or a
stored column) — market_state bars are already retained, so this is
just a lookup + comparison against data we already have. No new
scheduling, no write-back races with the decisions table.

Only enter_long / enter_short decisions have anything to evaluate —
no_trade and insufficient_data made no directional call, so there's
nothing to score them against.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.storage import get_bar_at_or_after, get_bar_at_or_before

HORIZON_MINUTES_DEFAULT = [15, 30, 60]

_DIRECTION_TO_DECISION = {"enter_long": "bullish", "enter_short": "bearish"}


@dataclass
class HorizonOutcome:
    horizon_minutes: int
    price_at_decision: float | None
    price_at_horizon: float | None
    price_change: float | None
    outcome: str  # "correct" | "incorrect" | "flat" | "pending" | "no_data"

    def to_dict(self) -> dict:
        return {
            "horizon_minutes": self.horizon_minutes,
            "price_at_decision": self.price_at_decision,
            "price_at_horizon": self.price_at_horizon,
            "price_change": self.price_change,
            "outcome": self.outcome,
        }


def _parse_utc(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _fmt_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_outcome_at_horizon(
    symbol: str,
    timeframe: str,
    decision_timestamp: str,
    decision_direction: str,  # "enter_long" or "enter_short"
    horizon_minutes: int,
) -> HorizonOutcome:
    decision_dt = _parse_utc(decision_timestamp)
    target_dt = decision_dt + timedelta(minutes=horizon_minutes)
    now = datetime.now(timezone.utc)

    bar_at_decision = get_bar_at_or_before(symbol, timeframe, decision_timestamp)
    if bar_at_decision is None:
        return HorizonOutcome(horizon_minutes, None, None, None, "no_data")
    price_at_decision = bar_at_decision["close"]

    if now < target_dt:
        # Not enough real time has passed yet to know the answer.
        return HorizonOutcome(horizon_minutes, price_at_decision, None, None, "pending")

    bar_at_horizon = get_bar_at_or_after(symbol, timeframe, _fmt_utc(target_dt))
    if bar_at_horizon is None:
        # Time has passed, but no bar ever arrived at/after that point
        # (e.g. a session gap) — genuinely unknown, not "pending".
        return HorizonOutcome(horizon_minutes, price_at_decision, None, None, "no_data")

    price_at_horizon = bar_at_horizon["close"]
    price_change = round(price_at_horizon - price_at_decision, 2)

    expected_direction = _DIRECTION_TO_DECISION.get(decision_direction)
    if price_change == 0:
        outcome = "flat"
    elif expected_direction == "bullish":
        outcome = "correct" if price_change > 0 else "incorrect"
    elif expected_direction == "bearish":
        outcome = "correct" if price_change < 0 else "incorrect"
    else:
        outcome = "no_data"

    return HorizonOutcome(horizon_minutes, price_at_decision, price_at_horizon, price_change, outcome)


def compute_outcomes_for_decision(
    symbol: str,
    timeframe: str,
    decision: dict,
    horizons: list[int] = None,
) -> dict[int, dict] | None:
    """Returns {horizon_minutes: outcome_dict} for a directional
    decision, or None if the decision was no_trade/insufficient_data
    (nothing to evaluate)."""
    if decision.get("decision") not in _DIRECTION_TO_DECISION:
        return None

    horizons = horizons or HORIZON_MINUTES_DEFAULT
    return {
        h: compute_outcome_at_horizon(
            symbol=symbol,
            timeframe=timeframe,
            decision_timestamp=decision["timestamp"],
            decision_direction=decision["decision"],
            horizon_minutes=h,
        ).to_dict()
        for h in horizons
    }
