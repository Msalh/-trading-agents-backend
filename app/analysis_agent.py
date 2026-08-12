"""
Analysis Agent — Sprint 3 (first LLM agent).

Reads recent market_state bars for a symbol/timeframe and asks Claude
for a technical read: direction, confidence, key levels, reasoning.
Isolated by design — this agent never sees News/Macro/Timing opinions,
matching the "no anchoring" rule from the roadmap.

Output is forced into the unified agent schema (direction, confidence,
reasoning, key_data, flags) so the future Coordinator can consume it
the same way it will consume every other agent's opinion.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are a technical analyst specialized in MNQ (Micro E-mini Nasdaq-100). You receive recent price bars across the timeframe given to you.

Your job only: analyze price action and structure — not news, not risk.
Determine: overall direction (bullish, bearish, or neutral), the most important key levels visible in the data (support/resistance, recent highs/lows, VWAP relationship), and whether there's a clear setup aligned with the recent trend.

Each bar also includes five boolean setup flags computed directly from price/volume (not your judgment call): liquidity_sweep (a recent high/low was taken out intrabar then closed back on the other side — a stop-hunt-then-reverse signature), reclaim (price closed back on the "correct" side of VWAP after being on the wrong side), rejection (a long wick against the close near a tracked reference level), displacement (an unusually large-range bar relative to ATR — an impulsive move), and volume_spike (volume meaningfully above its recent average). Weigh these directly in your reasoning when they appear on recent bars — a sweep+reclaim combo near a key level is a stronger structural signal than the same price action without them, and you should say so explicitly when relevant rather than only describing raw OHLC movement.

Be specific with actual price levels from the data given — never invent numbers not present in or reasonably derivable from the bars you were given.

Respond with a single JSON object ONLY, no other text, no markdown code fences, matching exactly this shape:
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 short sentences>",
  "key_data": {
    "key_levels": [<numbers>],
    "pattern": "<short description or null>",
    "trend_alignment": "<short description of how lower/higher timeframe trends line up, or null>"
  },
  "flags": [<zero or more of: "low_data", "conflicting_signals", "choppy">]
}"""


@dataclass
class AnalysisOpinion:
    agent: str
    timestamp: str
    symbol: str
    timeframe: str
    direction: str
    confidence: int
    reasoning: str
    key_data: dict
    flags: list[str]

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
            "flags": self.flags,
        }


class AnalysisAgentError(Exception):
    pass


def _build_user_message(symbol: str, timeframe: str, bars: list[dict]) -> str:
    # Keep only the fields actually useful for technical analysis —
    # no need to spend tokens on secret/event_id/source etc.
    trimmed = [
        {
            "timestamp": b["timestamp"],
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": b.get("volume"),
            "session_name": b.get("session_name"),
            "vwap": b.get("vwap"),
            "distance_from_vwap_points": b.get("distance_from_vwap_points"),
            "atr": b.get("atr"),
            "previous_day_high": b.get("previous_day_high"),
            "previous_day_low": b.get("previous_day_low"),
            "overnight_high": b.get("overnight_high"),
            "overnight_low": b.get("overnight_low"),
            "nearest_liquidity_level": b.get("nearest_liquidity_level"),
            "nearest_liquidity_type": b.get("nearest_liquidity_type"),
            "trend_1m": b.get("trend_1m"),
            "trend_5m": b.get("trend_5m"),
            "trend_15m": b.get("trend_15m"),
            "trend_1h": b.get("trend_1h"),
            "liquidity_sweep": b.get("liquidity_sweep"),
            "reclaim": b.get("reclaim"),
            "rejection": b.get("rejection"),
            "displacement": b.get("displacement"),
            "volume_spike": b.get("volume_spike"),
        }
        for b in bars
    ]
    return (
        f"Symbol: {symbol}\nTimeframe: {timeframe}\n"
        f"Most recent bar last. {len(trimmed)} bars provided.\n\n"
        f"{json.dumps(trimmed, indent=2)}"
    )


def _parse_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        # strip an accidental code fence even though the prompt forbids it
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AnalysisAgentError(f"model did not return valid JSON: {e}\nraw: {raw_text[:500]}")


def run_analysis(symbol: str, timeframe: str, bars: list[dict]) -> AnalysisOpinion:
    if not bars:
        raise AnalysisAgentError("no market_state bars available to analyze")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise AnalysisAgentError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)
    user_message = _build_user_message(symbol, timeframe, bars)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    parsed = _parse_response(raw_text)

    required = {"direction", "confidence", "reasoning", "key_data", "flags"}
    missing = required - parsed.keys()
    if missing:
        raise AnalysisAgentError(f"model response missing required fields: {missing}")

    return AnalysisOpinion(
        agent="analysis",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        symbol=symbol,
        timeframe=timeframe,
        direction=parsed["direction"],
        confidence=int(parsed["confidence"]),
        reasoning=parsed["reasoning"],
        key_data=parsed["key_data"],
        flags=parsed["flags"],
    )
