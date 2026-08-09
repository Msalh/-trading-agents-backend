"""
Risk Agent — Sprint 7.

Deliberately NOT an LLM call, unlike Analysis/News/Macro. Money math
needs to be exact, not "probably right" — the same reasoning that
made Timing Agent pure logic applies even more strongly here. Given
a Coordinator decision plus the account state, this computes whether
the proposed trade fits within the remaining risk budget.

Account state is static/manual for Sprint 7 (the system doesn't have
a live broker connection yet) — set via environment variables and
updated by hand as the real account balance/drawdown changes. See
.env.example for ACCOUNT_BALANCE / MAX_DRAWDOWN / CURRENT_DRAWDOWN_USED
/ CURRENT_OPEN_POSITIONS.

Position size is estimated in dollars using ATR (from the latest
market_state bar) as a proxy for stop distance — there's no explicit
stop price elsewhere in the system yet, so this is a conservative
approximation: risk_per_contract ≈ ATR (points) × point_value.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

MNQ_POINT_VALUE = 2.0  # USD per index point per contract (Micro E-mini Nasdaq-100)

ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "50000"))
MAX_DRAWDOWN = float(os.environ.get("MAX_DRAWDOWN", "2000"))
CURRENT_DRAWDOWN_USED = float(os.environ.get("CURRENT_DRAWDOWN_USED", "0"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "1"))
CURRENT_OPEN_POSITIONS = int(os.environ.get("CURRENT_OPEN_POSITIONS", "0"))
BASE_POSITION_SIZE = int(os.environ.get("BASE_POSITION_SIZE", "1"))
RISK_FRACTION_PER_TRADE = float(os.environ.get("RISK_FRACTION_PER_TRADE", "0.5"))


@dataclass
class RiskOpinion:
    agent: str
    timestamp: str
    symbol: str
    timeframe: str
    decision: str  # "approve" | "modify" | "reject" | "no_action"
    original_size: int
    suggested_size: int | None
    reasoning: str
    key_data: dict
    flags: list[str]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "decision": self.decision,
            "original_size": self.original_size,
            "suggested_size": self.suggested_size,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
            "flags": self.flags,
        }


def evaluate_risk(symbol: str, timeframe: str, coordinator_decision: dict, latest_bar: dict | None) -> RiskOpinion:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    trade_decision = coordinator_decision.get("decision")

    if trade_decision not in ("enter_long", "enter_short"):
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="no_action",
            original_size=0,
            suggested_size=None,
            reasoning=f"Coordinator decision is '{trade_decision}' — nothing for Risk to evaluate.",
            key_data={},
            flags=[],
        )

    remaining_room = MAX_DRAWDOWN - CURRENT_DRAWDOWN_USED
    account_snapshot = {
        "account_balance": ACCOUNT_BALANCE,
        "max_drawdown": MAX_DRAWDOWN,
        "current_drawdown_used": CURRENT_DRAWDOWN_USED,
        "remaining_drawdown_room": round(remaining_room, 2),
        "max_open_positions": MAX_OPEN_POSITIONS,
        "current_open_positions": CURRENT_OPEN_POSITIONS,
    }

    if CURRENT_OPEN_POSITIONS >= MAX_OPEN_POSITIONS:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=(
                f"Already at the open-position limit ({CURRENT_OPEN_POSITIONS}/"
                f"{MAX_OPEN_POSITIONS}) — no room for a new position regardless of size."
            ),
            key_data=account_snapshot,
            flags=["max_positions_reached"],
        )

    if remaining_room <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=f"No drawdown room remaining (${remaining_room:.2f} of ${MAX_DRAWDOWN:.2f} left).",
            key_data=account_snapshot,
            flags=["drawdown_exhausted"],
        )

    atr = (latest_bar or {}).get("atr")
    if atr is None or atr <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning="No valid ATR available to estimate risk per contract — rejecting rather than guessing.",
            key_data=account_snapshot,
            flags=["insufficient_data"],
        )

    risk_per_contract = atr * MNQ_POINT_VALUE
    proposed_risk = risk_per_contract * BASE_POSITION_SIZE
    budget_for_trade = remaining_room * RISK_FRACTION_PER_TRADE

    account_snapshot.update(
        {
            "atr_points": atr,
            "risk_per_contract_usd": round(risk_per_contract, 2),
            "budget_for_this_trade_usd": round(budget_for_trade, 2),
        }
    )

    if proposed_risk <= budget_for_trade:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="approve",
            original_size=BASE_POSITION_SIZE,
            suggested_size=BASE_POSITION_SIZE,
            reasoning=(
                f"Estimated risk ${proposed_risk:.2f} for {BASE_POSITION_SIZE} contract(s) is within "
                f"the ${budget_for_trade:.2f} budget for this trade "
                f"({RISK_FRACTION_PER_TRADE:.0%} of remaining drawdown room)."
            ),
            key_data=account_snapshot,
            flags=[],
        )

    max_affordable_size = int(budget_for_trade // risk_per_contract)
    if max_affordable_size >= 1:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            decision="modify",
            original_size=BASE_POSITION_SIZE,
            suggested_size=max_affordable_size,
            reasoning=(
                f"{BASE_POSITION_SIZE} contract(s) would risk ${proposed_risk:.2f}, over the "
                f"${budget_for_trade:.2f} budget. Reducing to {max_affordable_size} "
                f"contract(s) (~${risk_per_contract * max_affordable_size:.2f}) stays within budget."
            ),
            key_data=account_snapshot,
            flags=["size_reduced"],
        )

    return RiskOpinion(
        agent="risk",
        timestamp=now_iso,
        symbol=symbol,
        timeframe=timeframe,
        decision="reject",
        original_size=BASE_POSITION_SIZE,
        suggested_size=None,
        reasoning=(
            f"Even 1 contract (~${risk_per_contract:.2f} estimated risk) exceeds the "
            f"${budget_for_trade:.2f} budget for this trade — no safe size available right now."
        ),
        key_data=account_snapshot,
        flags=["budget_too_small_for_min_size"],
    )
