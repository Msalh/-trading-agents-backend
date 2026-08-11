"""
Execution Agent — Sprint 7 (final agent), paper-only.

LLM-backed, per the originally agreed design — unlike Risk/Timing,
this genuinely benefits from judgment (choosing a sensible entry
zone relative to recent structure, picking realistic targets from
Analysis's key_levels) rather than pure arithmetic. It never
reopens "should we trade" — that's fully decided before this runs;
its only job is turning an already-approved decision into a
concrete, well-formed order.

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

SYSTEM_PROMPT = """You handle execution. You receive the final decision after Risk Agent approval/modification (direction, size, account risk context), the current price and recent structure, and the key levels Analysis identified.

Your job only: turn the decision into a concrete order — the decision to trade has already been made before you, and Risk has already approved a size. Do not reopen whether to trade.

Determine:
- order_type: "market" if the current price is already a reasonable entry, or "limit" if a specific nearby level makes more sense (e.g. waiting for a pullback to a key level in a trending move).
- entry_price: the actual price for the order (current price for market, the specific level for limit).
- stop_loss: an actual price, placed using the ATR and/or nearest structural level against the trade — not an arbitrary round number.
- targets: 1-3 actual price levels, drawn from or reasoned from the key_levels you were given, in the direction of the trade.
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
    status: str  # "planned" | "no_action" | "error"
    direction: str | None
    size: int | None
    order_type: str | None
    entry_price: float | None
    stop_loss: float | None
    targets: list[float] | None
    ready_now: bool | None
    reasoning: str
    flags: list[str]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "direction": self.direction,
            "size": self.size,
            "order_type": self.order_type,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "targets": self.targets,
            "ready_now": self.ready_now,
            "reasoning": self.reasoning,
            "flags": self.flags,
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
        size=None,
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


def plan_execution(
    symbol: str,
    timeframe: str,
    risk_evaluation: dict,
    coordinator_decision: dict,
    latest_bar: dict | None,
    analysis_key_levels: list | None,
) -> ExecutionOpinion:
    risk_decision = risk_evaluation.get("decision")
    if risk_decision not in ("approve", "modify"):
        # Nothing to execute — Risk rejected it or there was nothing
        # for Risk to evaluate in the first place. No LLM call, no cost.
        return _no_action(
            symbol, timeframe,
            f"Risk decision is '{risk_decision}' — nothing to execute.",
        )

    direction = coordinator_decision.get("direction")
    size = risk_evaluation.get("suggested_size")
    if direction not in ("bullish", "bearish") or not size:
        return _no_action(
            symbol, timeframe,
            "Missing direction or approved size from upstream decisions — cannot plan an order.",
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
            "approved_size_contracts": size,
            "current_price": latest_bar.get("close"),
            "atr": latest_bar.get("atr"),
            "vwap": latest_bar.get("vwap"),
            "key_levels": analysis_key_levels or [],
            "risk_reasoning": risk_evaluation.get("reasoning"),
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

    return ExecutionOpinion(
        agent="execution",
        timestamp=_now_iso(),
        symbol=symbol,
        timeframe=timeframe,
        status="planned",
        direction=direction,
        size=size,
        order_type=parsed["order_type"],
        entry_price=parsed["entry_price"],
        stop_loss=parsed["stop_loss"],
        targets=parsed["targets"],
        ready_now=parsed["ready_now"],
        reasoning=parsed["reasoning"],
        flags=[],
    )
