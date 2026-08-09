"""
Macro/Correlation Agent — Sprint 5.

Same pattern as News: not bar-dependent, needs live current data
(DXY level, US10Y yield, SPX behavior), so it uses Claude's hosted
web_search tool rather than reading stored market_state bars. Runs
on its own scheduler interval, independent of the webhook.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from app.text_utils import clean_opinion_text_fields

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You monitor the broader macro context relevant to Nasdaq-100 futures (NQ/MNQ): the US Dollar Index (DXY), US 10-Year Treasury yields, and the correlation/behavior between SPX and NDX (Nasdaq-100).

Your job only: does the current macro backdrop support or contradict an expected move in NQ — not chart pattern analysis, not news events.

Search for current levels before answering. Determine:
- Dollar strength or weakness right now (DXY direction) and its typical expected effect on Nasdaq (inverse relationship is the common pattern, but check whether that's holding today).
- Whether SPX and NDX appear to be moving in sync or diverging in a way worth flagging.
- Any macro signal (yields spiking, dollar surging, a risk-off/risk-on shift) that would contradict a purely technical bullish or bearish read.

Respond with a single JSON object ONLY, no other text, no markdown code fences, matching exactly this shape:
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 short sentences, cite what you found>",
  "key_data": {
    "dxy_read": "<short description of dollar strength/direction>",
    "yields_read": "<short description of US10Y level/direction>",
    "spx_ndx_correlation": "<'in_sync' | 'diverging' | 'unclear'>",
    "notes": "<short free text or null>"
  },
  "flags": [<zero or more of: "risk_off", "conflicting_signals", "stale_data">]
}"""


@dataclass
class MacroOpinion:
    agent: str
    timestamp: str
    symbol: str
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
            "direction": self.direction,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
            "flags": self.flags,
        }


class MacroAgentError(Exception):
    pass


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
        raise MacroAgentError(f"model did not return valid JSON: {e}\nraw: {raw_text[:500]}")


def run_macro(symbol: str) -> MacroOpinion:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise MacroAgentError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"Symbol context: {symbol} (Nasdaq-100 futures). "
        f"Search for the current DXY level/direction, US 10-Year Treasury "
        f"yield level/direction, and how SPX and NDX are behaving relative "
        f"to each other today, then respond with the JSON object only, as "
        f"instructed."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    if not raw_text.strip():
        raise MacroAgentError("model returned no text content (only tool-use blocks)")

    parsed = _parse_response(raw_text)
    parsed = clean_opinion_text_fields(parsed)

    required = {"direction", "confidence", "reasoning", "key_data", "flags"}
    missing = required - parsed.keys()
    if missing:
        raise MacroAgentError(f"model response missing required fields: {missing}")

    return MacroOpinion(
        agent="macro",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        symbol=symbol,
        direction=parsed["direction"],
        confidence=int(parsed["confidence"]),
        reasoning=parsed["reasoning"],
        key_data=parsed["key_data"],
        flags=parsed["flags"],
    )
