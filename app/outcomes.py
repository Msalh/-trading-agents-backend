"""
Outcome tracking — evaluates whether a past Coordinator decision was
directionally correct.

Sprint 14 (original): computed on-demand (a query, not a background
job or a stored column) whether price actually moved the predicted
way at several fixed time horizons after the decision, by comparing
market_state bars already stored — no new scheduling, no persisted
column. This was always a HYPOTHETICAL estimate: it existed because,
at the time, no decision ever became a real, trackable trade — there
was nothing better to measure against.

Tier 2.3 rebuild: that's no longer true. app/paper_trades.py now
opens and closes real paper trades with a real, computed pnl_usd. For
any candidate that actually became a trade, that closed trade's P&L
IS the outcome — an exact answer, not a proxy. The horizon estimate
below is kept and still computed, but only as a FALLBACK for
candidates that never became a trade at all (rejected by Risk, never
manually run, Execution failed, etc.) — useful for near-miss analysis
("would this have worked out anyway?") even though it's not a real
result. compute_outcome_for_candidate() is the entry point that
chooses between the two and labels which one it used via a "source"
field ("actual_trade" | "hypothetical") so a caller never confuses
a guess for a fact.

Only enter_long / enter_short decisions have anything to evaluate —
no_trade and insufficient_data made no directional call, so there's
nothing to score them against, in either mode.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.storage import get_bar_at_or_after, get_bar_at_or_before, get_trade_by_candidate_id

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
    (nothing to evaluate). Kept for backward compatibility with
    /coordinator/history/outcomes, which reads the older
    coordinator_decisions table (no candidate_id, so it can never
    check for a real trade) — new callers should use
    compute_outcome_for_candidate() instead, which prefers real trade
    P&L when it's available."""
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


def compute_outcome_for_candidate(candidate: dict, horizons: list[int] = None) -> dict | None:
    """Tier 2.3: the rebuilt entry point. Returns None for
    no_trade/insufficient_data candidates — same "nothing to score"
    rule as before. Otherwise checks whether this candidate ever
    became a real paper trade (linked by candidate_id):

      - A closed trade exists -> real outcome. "outcome" is
        "win"/"loss"/"breakeven" from the trade's actual pnl_usd, no
        estimation involved.
      - A trade exists but hasn't closed yet (pending_fill/open) ->
        "outcome": "pending" — genuinely unresolved, not a data gap.
      - No trade was ever opened for this candidate -> falls back to
        the original hypothetical horizon-based price estimate, so
        near-miss candidates (rejected by Risk, never acted on) are
        still visible for threshold-tuning analysis. Clearly labeled
        "source": "hypothetical" so it's never mistaken for a real
        result.
    """
    decision = candidate["decision"]
    if decision.get("decision") not in _DIRECTION_TO_DECISION:
        return None

    trade = get_trade_by_candidate_id(candidate["candidate_id"])

    if trade is not None and trade["status"] == "closed":
        pnl = trade["pnl_usd"]
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "breakeven")
        return {
            "source": "actual_trade",
            "trade_id": trade["trade_id"],
            "status": "closed",
            "exit_reason": trade["exit_reason"],
            "fill_price": trade["fill_price"],
            "exit_price": trade["exit_price"],
            "pnl_usd": pnl,
            "outcome": outcome,
        }

    if trade is not None and trade["status"] == "cancelled":
        # Tier 3.2: an order that expired before it ever filled
        # (ORDER_EXPIRY_MINUTES, app/paper_trades.py) — a real trade
        # record exists, but no position was ever taken. Distinct from
        # both "pending" (might still resolve) and a real win/loss/
        # breakeven (nothing was ever filled to realize a P&L against).
        return {
            "source": "actual_trade",
            "trade_id": trade["trade_id"],
            "status": "cancelled",
            "exit_reason": trade["exit_reason"],
            "outcome": "cancelled",
        }

    if trade is not None:
        # pending_fill or open — a real trade exists, it just hasn't
        # resolved yet. Distinct from "hypothetical": this candidate
        # DID become a trade, we just don't know the ending yet.
        return {
            "source": "actual_trade",
            "trade_id": trade["trade_id"],
            "status": trade["status"],
            "outcome": "pending",
        }

    horizons_dict = compute_outcomes_for_decision(
        symbol=candidate["symbol"],
        timeframe=candidate["timeframe"],
        decision=decision,
        horizons=horizons,
    )
    return {
        "source": "hypothetical",
        "status": "no_trade_opened",
        "horizons": horizons_dict,
    }


def summarize_outcomes(outcomes: list[dict | None]) -> dict:
    """Aggregates a list of compute_outcome_for_candidate() results
    (None entries — no_trade/insufficient_data candidates — are
    ignored) into the numbers actually useful for
    COORDINATOR_THRESHOLD tuning: real win rate/P&L from closed
    trades, kept separate from hypothetical horizon accuracy for
    candidates that never became a trade. The two are never blended
    into one number — a hypothetical guess and a real result answer
    different questions."""
    closed = [o for o in outcomes if o and o.get("source") == "actual_trade" and o.get("status") == "closed"]
    # Tier 3.2: an expired/cancelled order never took a position — it
    # belongs in neither "closed" (no P&L was ever realized) nor
    # "still_open_or_pending" (it's resolved, just resolved as
    # "never happened"), so it gets its own bucket.
    cancelled = [o for o in outcomes if o and o.get("source") == "actual_trade" and o.get("status") == "cancelled"]
    pending_trades = [
        o for o in outcomes
        if o and o.get("source") == "actual_trade" and o.get("status") not in ("closed", "cancelled")
    ]
    hypothetical = [o for o in outcomes if o and o.get("source") == "hypothetical"]

    wins = [o for o in closed if o["outcome"] == "win"]
    losses = [o for o in closed if o["outcome"] == "loss"]
    total_pnl = round(sum(o["pnl_usd"] for o in closed), 2) if closed else 0.0

    real = {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(closed) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "total_pnl_usd": total_pnl,
        "avg_pnl_usd": round(total_pnl / len(closed), 2) if closed else None,
        "still_open_or_pending": len(pending_trades),
        "cancelled_unfilled": len(cancelled),
    }

    horizon_accuracy: dict[int, dict] = {}
    for o in hypothetical:
        for h_str, h_outcome in (o.get("horizons") or {}).items():
            h = int(h_str)
            bucket = horizon_accuracy.setdefault(h, {"correct": 0, "incorrect": 0, "flat": 0, "pending": 0, "no_data": 0})
            bucket[h_outcome["outcome"]] = bucket.get(h_outcome["outcome"], 0) + 1

    hypothetical_summary = {}
    for h, counts in horizon_accuracy.items():
        resolved = counts["correct"] + counts["incorrect"]
        hypothetical_summary[h] = {
            **counts,
            "accuracy": round(counts["correct"] / resolved, 3) if resolved else None,
        }

    return {
        "real_trades": real,
        "hypothetical_never_traded": {
            "candidates": len(hypothetical),
            "by_horizon_minutes": hypothetical_summary,
        },
    }
