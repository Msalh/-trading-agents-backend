"""
Execution Agent — Sprint 7 (final agent), paper-only.

LLM-backed, per the originally agreed design — unlike Risk/Timing,
this genuinely benefits from judgment (choosing a sensible entry
zone relative to recent structure, picking realistic targets from
Analysis's key_levels) rather than pure arithmetic. It never
reopens "should we trade" — that's fully decided before this runs;
its only job is turning an already-approved decision into a
concrete, well-formed order.

An external review correctly pointed out that "valid JSON" was being
treated as "valid trade" — the model's stop/target prices were never
checked against basic trade geometry. This module now runs a
deterministic validation pass after parsing: stop on the correct
side of entry, targets on the profitable side, and a minimum
reward:risk ratio. A geometrically invalid proposal is never
silently accepted — it's returned with status="invalid" and the
specific reason, so it's visible on the dashboard rather than acted
on as if it were a normal plan.

Tier 2.2 (external review): this agent used to run AFTER Risk and
receive an already-approved contract size, because Risk needed
*something* to size against and ATR was the only number available
before a real stop existed. That made Risk's size a guess against an
estimate. Execution no longer takes or reasons about size at all — it
proposes order geometry only (order_type/entry/stop/targets), and
Risk now runs a second time afterward (see app/risk_agent.py's
size_position) to size the position from THIS agent's actual stop
distance. Sizing is Risk's job, not Execution's; this agent's only
output is where the trade is, not how big.

Paper only. Nothing here places a real order or talks to a broker —
it produces a JSON order spec for the dashboard to display, exactly
like every other agent's opinion.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-5"

# Minimum acceptable reward:risk, measured against the NEAREST target
# (the most conservative of however many targets were proposed) — a
# plan that doesn't clear this bar even to its closest target is
# rejected rather than displayed as a normal, actionable plan.
MIN_REWARD_RISK_RATIO = float(os.environ.get("MIN_REWARD_RISK_RATIO", "1.0"))

SYSTEM_PROMPT = """You handle execution. You receive the trade direction Coordinator/Risk already decided on, the current price and recent structure, and the key levels Analysis identified.

Your job only: turn the direction into concrete order geometry — the decision to trade has already been made before you. Do not reopen whether to trade. Do not decide position size — Risk sizes the trade afterward based on the stop distance you propose here; your job is where the trade is, not how big.

Determine:
- order_type: "market" if the current price is already a reasonable entry, or "limit" if a specific nearby level makes more sense (e.g. waiting for a pullback to a key level in a trending move).
- entry_price: the actual price for the order (current price for market, the specific level for limit).
- stop_loss: an actual price, placed using the ATR and/or nearest structural level against the trade — not an arbitrary round number. The stop MUST be on the losing side of entry (below entry for a long, above entry for a short).
- targets: 1-3 actual price levels, drawn from or reasoned from the key_levels you were given, in the direction of the trade. Targets MUST be on the profitable side of entry (above entry for a long, below entry for a short).
- ready_now: true if this is a market order or the limit price is at/near current price; false if genuinely waiting for price to reach a limit zone.

Respond with a single JSON object ONLY, no other text, no markdown code fences, matching exactly this shape:
{
  "order_type": "market" | "limit",
  "entry_price": <number>,
  "stop_loss": <number>,
  "targets": [<number>, ...],
  "ready_now": true | false,
  "reasoning": "<2-3 short sentences>"
}

If you are missing data you genuinely need (e.g. no ATR, no key levels at all), respond instead with:
{"error": "<what's missing>"}
rather than guessing a number."""


@dataclass
class ExecutionOpinion:
    agent: str
    timestamp: str
    symbol: str
    timeframe: str
    status: str  # "planned" | "no_action" | "error" | "invalid"
    direction: str | None
    order_type: str | None
    entry_price: float | None
    stop_loss: float | None
    targets: list[float] | None
    ready_now: bool | None
    reasoning: str
    flags: list[str]
    validation_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "direction": self.direction,
            "order_type": self.order_type,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "targets": self.targets,
            "ready_now": self.ready_now,
            "reasoning": self.reasoning,
            "flags": self.flags,
            "validation_error": self.validation_error,
        }


class ExecutionAgentError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _no_action(symbol: str, timeframe: str, reasoning: str) -> ExecutionOpinion:
    return ExecutionOpinion(
        agent="execution",
        timestamp=_now_iso(),
        symbol=symbol,
        timeframe=timeframe,
        status="no_action",
        direction=None,
        order_type=None,
        entry_price=None,
        stop_loss=None,
        targets=None,
        ready_now=None,
        reasoning=reasoning,
        flags=[],
    )


def _parse_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ExecutionAgentError(f"model did not return valid JSON: {e}\nraw: {raw_text[:500]}")


def _validate_trade_geometry(
    direction: str, entry_price: float, stop_loss: float, targets: list[float]
) -> str | None:
    """Deterministic sanity check on the LLM's proposed prices.
    Returns None if valid, or a human-readable reason if not. Checks
    only geometry (which side of entry things are on) and minimum
    reward:risk — it has no opinion on whether the trade itself is a
    good idea, that's the Coordinator/Risk's job upstream."""
    if not targets:
        return "no targets provided"

    if direction == "bullish":
        if not (stop_loss < entry_price):
            return f"stop_loss ({stop_loss}) must be below entry_price ({entry_price}) for a long"
        if any(t <= entry_price for t in targets):
            return f"all targets must be above entry_price ({entry_price}) for a long, got {targets}"
        nearest_target = min(targets)
        reward = nearest_target - entry_price
        risk = entry_price - stop_loss
    elif direction == "bearish":
        if not (stop_loss > entry_price):
            return f"stop_loss ({stop_loss}) must be above entry_price ({entry_price}) for a short"
        if any(t >= entry_price for t in targets):
            return f"all targets must be below entry_price ({entry_price}) for a short, got {targets}"
        nearest_target = max(targets)
        reward = entry_price - nearest_target
        risk = stop_loss - entry_price
    else:
        return f"unrecognized direction '{direction}'"

    if risk <= 0:
        return f"zero or negative risk distance (entry={entry_price}, stop={stop_loss})"

    reward_risk_ratio = reward / risk
    if reward_risk_ratio < MIN_REWARD_RISK_RATIO:
        return (
            f"reward:risk to nearest target is {reward_risk_ratio:.2f}, "
            f"below the minimum {MIN_REWARD_RISK_RATIO:.2f}"
        )

    return None


def plan_execution(
    symbol: str,
    timeframe: str,
    coordinator_decision: dict,
    latest_bar: dict | None,
    analysis_key_levels: list | None,
) -> ExecutionOpinion:
    """Tier 2.2: no longer takes a Risk evaluation or a size — Risk's
    gate stage (see app/risk_agent.py) is what decides whether it's
    worth calling this at all (checked by the caller in main.py before
    reaching here), and Risk's size stage runs AFTER this, against the
    entry_price/stop_loss this function proposes. This function's only
    inputs are the direction Coordinator already decided on and the
    market context needed to place a sensible order."""
    direction = coordinator_decision.get("direction")
    if direction not in ("bullish", "bearish"):
        return _no_action(
            symbol, timeframe,
            f"Coordinator direction is '{direction}' — nothing to execute.",
        )

    if latest_bar is None:
        raise ExecutionAgentError("no market_state bar available to plan execution against")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ExecutionAgentError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = json.dumps(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "current_price": latest_bar.get("close"),
            "atr": latest_bar.get("atr"),
            "vwap": latest_bar.get("vwap"),
            "key_levels": analysis_key_levels or [],
        },
        indent=2,
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
    parsed = _parse_response(raw_text)

    if "error" in parsed:
        raise ExecutionAgentError(f"model reported missing data: {parsed['error']}")

    required = {"order_type", "entry_price", "stop_loss", "targets", "ready_now", "reasoning"}
    missing = required - parsed.keys()
    if missing:
        raise ExecutionAgentError(f"model response missing required fields: {missing}")

    validation_error = _validate_trade_geometry(
        direction=direction,
        entry_price=parsed["entry_price"],
        stop_loss=parsed["stop_loss"],
        targets=parsed["targets"],
    )

    return ExecutionOpinion(
        agent="execution",
        timestamp=_now_iso(),
        symbol=symbol,
        timeframe=timeframe,
        status="invalid" if validation_error else "planned",
        direction=direction,
        order_type=parsed["order_type"],
        entry_price=parsed["entry_price"],
        stop_loss=parsed["stop_loss"],
        targets=parsed["targets"],
        ready_now=parsed["ready_now"],
        reasoning=parsed["reasoning"],
        flags=["failed_geometry_validation"] if validation_error else [],
        validation_error=validation_error,
    )
