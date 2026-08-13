"""
Risk Agent — Sprint 7, reworked in Tier 2.2.

Deliberately NOT an LLM call, unlike Analysis/News/Macro. Money math
needs to be exact, not "probably right" — the same reasoning that
made Timing Agent pure logic applies even more strongly here.

Account state is static/manual (the system doesn't have a live broker
connection yet) — set via environment variables and updated by hand as
the real account balance/drawdown changes. See .env.example for
ACCOUNT_BALANCE / MAX_DRAWDOWN / CURRENT_DRAWDOWN_USED /
CURRENT_OPEN_POSITIONS.

Tier 2.2 (external review): the original design sized every position
using ATR as a *proxy* for stop distance, because Execution — which
actually picks the real stop — used to run only AFTER Risk had already
approved a size. A real stop chosen afterward could be materially
tighter or wider than ATR, so the position Risk "approved" often
didn't carry the dollar risk Risk thought it did. The two are now
genuinely different numbers computed at different times; ATR is a
volatility read, not a stop-placement decision.

Fixed by splitting Risk into two stages that run around Execution
instead of entirely before it:

  1. evaluate_risk_gate() — runs immediately after a trade candidate
     exists, before any paid LLM call. Checks only the two hard
     constraints that don't need a stop price at all: are we already
     at the position limit, is there any drawdown room left. No ATR
     anywhere in this stage — it doesn't estimate risk, it only checks
     whether spending an Execution LLM call is even worth it. Result
     is "reject" (hard block), "no_action" (nothing to evaluate), or
     "pending_execution" (cleared — Execution can now run).
  2. size_position() — runs once Execution has attached a real
     entry_price/stop_loss to the SAME candidate. Computes
     risk_per_contract = abs(entry_price - stop_loss) * point_value
     from that actual proposed stop, then decides
     approve/modify/reject exactly as before — just against a real
     number instead of an ATR-derived estimate.

Both stages are read through the same /agents/risk/evaluate endpoint
(see main.py) — it inspects the candidate to decide which stage to
run, so calling it twice across one candidate's lifecycle (once before
Execution, once after) is the intended flow.

Tier 2.3 (paper fill/P&L lifecycle, app/paper_trades.py): the gate
stage's position-limit check now takes the live count of open/pending
paper trades from the caller instead of only the static
CURRENT_OPEN_POSITIONS env var — see evaluate_risk_gate's
current_open_positions parameter.

Tier 2.10 (account-level risk controls, app/account_risk.py): two more
account-level gaps closed the same way Tier 2.3 closed the open-
position one. First, CURRENT_DRAWDOWN_USED was always a hand-updated
env var that could silently drift from reality — both evaluate_risk_gate()
and size_position() now accept an optional current_drawdown_used
parameter, with callers (main.py) passing the LIVE peak-to-trough
figure computed from real closed paper trades
(account_risk.compute_current_drawdown_used()); the env var is kept
only as the fallback default, exactly like CURRENT_OPEN_POSITIONS.
Second, a new DAILY_LOSS_LIMIT — a faster, time-boxed circuit breaker
distinct from the account-wide MAX_DRAWDOWN, so one very bad trading
day can't quietly consume the whole drawdown budget. Both stages
accept an optional daily_loss_used parameter the same way, defaulting
to 0.0 when not supplied (there's no prior env var for this — it's a
new control, not a migration of an existing manual one).
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
# Tier 2.10: a time-boxed circuit breaker distinct from MAX_DRAWDOWN —
# no prior env var to stay compatible with, since this control is new.
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "1000"))


@dataclass
class RiskOpinion:
    agent: str
    timestamp: str
    symbol: str
    timeframe: str
    stage: str  # "gate" | "size"
    decision: str  # "no_action" | "reject" | "pending_execution" | "approve" | "modify"
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
            "stage": self.stage,
            "decision": self.decision,
            "original_size": self.original_size,
            "suggested_size": self.suggested_size,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
            "flags": self.flags,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _account_snapshot(
    current_open_positions: int | None = None,
    current_drawdown_used: float | None = None,
    daily_loss_used: float | None = None,
) -> dict:
    open_positions = CURRENT_OPEN_POSITIONS if current_open_positions is None else current_open_positions
    drawdown_used = CURRENT_DRAWDOWN_USED if current_drawdown_used is None else current_drawdown_used
    loss_used_today = 0.0 if daily_loss_used is None else daily_loss_used
    remaining_room = MAX_DRAWDOWN - drawdown_used
    remaining_daily_loss_room = DAILY_LOSS_LIMIT - loss_used_today
    return {
        "account_balance": ACCOUNT_BALANCE,
        "max_drawdown": MAX_DRAWDOWN,
        "current_drawdown_used": drawdown_used,
        "remaining_drawdown_room": round(remaining_room, 2),
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "daily_loss_used": loss_used_today,
        "remaining_daily_loss_room": round(remaining_daily_loss_room, 2),
        "max_open_positions": MAX_OPEN_POSITIONS,
        "current_open_positions": open_positions,
    }


def evaluate_risk_gate(
    symbol: str,
    timeframe: str,
    coordinator_decision: dict,
    current_open_positions: int | None = None,
    current_drawdown_used: float | None = None,
    daily_loss_used: float | None = None,
) -> RiskOpinion:
    """Stage 1. No stop price involved — this only decides whether
    it's worth letting Execution spend a paid LLM call at all.

    Tier 2.3: current_open_positions is now the LIVE count of paper
    trades still pending_fill/open (see app/paper_trades.py), passed
    in by the caller (main.py) — not read from this module's static
    CURRENT_OPEN_POSITIONS env var, which used to require updating by
    hand and could silently drift from reality. The env var is kept
    only as a fallback default for callers that don't track paper
    trades (e.g. standalone tests, or a future non-paper deployment).

    Tier 2.10: current_drawdown_used and daily_loss_used follow the
    exact same pattern — main.py passes the LIVE values computed by
    app/account_risk.py from real closed paper trades; the module-level
    CURRENT_DRAWDOWN_USED env var and 0.0 (respectively) are the
    fallback defaults for callers that don't compute them."""
    now_iso = _now_iso()
    trade_decision = coordinator_decision.get("decision")

    if trade_decision not in ("enter_long", "enter_short"):
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="gate",
            decision="no_action",
            original_size=0,
            suggested_size=None,
            reasoning=f"Coordinator decision is '{trade_decision}' — nothing for Risk to evaluate.",
            key_data={},
            flags=[],
        )

    open_positions = CURRENT_OPEN_POSITIONS if current_open_positions is None else current_open_positions
    account_snapshot = _account_snapshot(open_positions, current_drawdown_used, daily_loss_used)

    if open_positions >= MAX_OPEN_POSITIONS:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="gate",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=(
                f"Already at the open-position limit ({open_positions}/"
                f"{MAX_OPEN_POSITIONS}) — no room for a new position regardless of size."
            ),
            key_data=account_snapshot,
            flags=["max_positions_reached"],
        )

    remaining_daily_loss_room = account_snapshot["remaining_daily_loss_room"]
    if remaining_daily_loss_room <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="gate",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=(
                f"Today's realized loss (${account_snapshot['daily_loss_used']:.2f}) has reached "
                f"the ${DAILY_LOSS_LIMIT:.2f} daily loss limit — no new trades until the next "
                "trading day, regardless of remaining overall drawdown room."
            ),
            key_data=account_snapshot,
            flags=["daily_loss_limit_reached"],
        )

    remaining_room = account_snapshot["remaining_drawdown_room"]
    if remaining_room <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="gate",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=f"No drawdown room remaining (${remaining_room:.2f} of ${MAX_DRAWDOWN:.2f} left).",
            key_data=account_snapshot,
            flags=["drawdown_exhausted"],
        )

    return RiskOpinion(
        agent="risk",
        timestamp=now_iso,
        symbol=symbol,
        timeframe=timeframe,
        stage="gate",
        decision="pending_execution",
        original_size=BASE_POSITION_SIZE,
        suggested_size=None,
        reasoning=(
            "Position limits, daily loss limit, and drawdown room are all clear — proceeding "
            "to Execution to determine the actual entry/stop before sizing this trade."
        ),
        key_data=account_snapshot,
        flags=[],
    )


def size_position(
    symbol: str,
    timeframe: str,
    entry_price: float,
    stop_loss: float,
    current_drawdown_used: float | None = None,
    daily_loss_used: float | None = None,
) -> RiskOpinion:
    """Stage 2. Sizes the position from Execution's real proposed stop
    distance — never ATR. Callers (see main.py) are responsible for
    only reaching this stage once the gate has cleared and Execution
    has produced a validated (status="planned") order.

    Tier 2.10: current_drawdown_used/daily_loss_used accept the same
    live-computed values evaluate_risk_gate() does — re-checked here
    too (not just at the gate) since Execution's LLM call happens in
    between the two stages, during which another trade could close and
    change either figure. Mirrors the existing double-check pattern
    remaining_room already used before this tier."""
    now_iso = _now_iso()
    account_snapshot = _account_snapshot(
        current_drawdown_used=current_drawdown_used, daily_loss_used=daily_loss_used
    )
    remaining_room = account_snapshot["remaining_drawdown_room"]

    risk_per_unit = abs(entry_price - stop_loss)
    if risk_per_unit <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="size",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=(
                f"Zero or invalid stop distance (entry={entry_price}, stop={stop_loss}) — "
                "cannot size a position against it."
            ),
            key_data=account_snapshot,
            flags=["invalid_stop_distance"],
        )

    remaining_daily_loss_room = account_snapshot["remaining_daily_loss_room"]
    if remaining_daily_loss_room <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="size",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=(
                f"Today's realized loss (${account_snapshot['daily_loss_used']:.2f}) reached the "
                f"${DAILY_LOSS_LIMIT:.2f} daily loss limit between the gate check and now — "
                "no new trades until the next trading day."
            ),
            key_data=account_snapshot,
            flags=["daily_loss_limit_reached"],
        )

    if remaining_room <= 0:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="size",
            decision="reject",
            original_size=BASE_POSITION_SIZE,
            suggested_size=None,
            reasoning=f"No drawdown room remaining (${remaining_room:.2f} of ${MAX_DRAWDOWN:.2f} left).",
            key_data=account_snapshot,
            flags=["drawdown_exhausted"],
        )

    risk_per_contract = risk_per_unit * MNQ_POINT_VALUE
    proposed_risk = risk_per_contract * BASE_POSITION_SIZE
    budget_for_trade = remaining_room * RISK_FRACTION_PER_TRADE

    account_snapshot.update(
        {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "stop_distance_points": round(risk_per_unit, 2),
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
            stage="size",
            decision="approve",
            original_size=BASE_POSITION_SIZE,
            suggested_size=BASE_POSITION_SIZE,
            reasoning=(
                f"Estimated risk ${proposed_risk:.2f} for {BASE_POSITION_SIZE} contract(s), sized from "
                f"the actual stop distance ({risk_per_unit:.2f} pts), is within the ${budget_for_trade:.2f} "
                f"budget for this trade ({RISK_FRACTION_PER_TRADE:.0%} of remaining drawdown room)."
            ),
            key_data=account_snapshot,
            flags=["sized_from_actual_stop"],
        )

    max_affordable_size = int(budget_for_trade // risk_per_contract)
    if max_affordable_size >= 1:
        return RiskOpinion(
            agent="risk",
            timestamp=now_iso,
            symbol=symbol,
            timeframe=timeframe,
            stage="size",
            decision="modify",
            original_size=BASE_POSITION_SIZE,
            suggested_size=max_affordable_size,
            reasoning=(
                f"{BASE_POSITION_SIZE} contract(s) at the actual stop distance ({risk_per_unit:.2f} pts) "
                f"would risk ${proposed_risk:.2f}, over the ${budget_for_trade:.2f} budget. Reducing to "
                f"{max_affordable_size} contract(s) (~${risk_per_contract * max_affordable_size:.2f}) "
                "stays within budget."
            ),
            key_data=account_snapshot,
            flags=["size_reduced", "sized_from_actual_stop"],
        )

    return RiskOpinion(
        agent="risk",
        timestamp=now_iso,
        symbol=symbol,
        timeframe=timeframe,
        stage="size",
        decision="reject",
        original_size=BASE_POSITION_SIZE,
        suggested_size=None,
        reasoning=(
            f"Even 1 contract at the actual stop distance ({risk_per_unit:.2f} pts, ~${risk_per_contract:.2f}) "
            f"exceeds the ${budget_for_trade:.2f} budget for this trade — no safe size available right now."
        ),
        key_data=account_snapshot,
        flags=["budget_too_small_for_min_size", "sized_from_actual_stop"],
    )
