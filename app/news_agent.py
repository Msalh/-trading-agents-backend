"""
News Agent — Sprint 4.

Unlike Analysis (which reads stored market_state bars), News needs
live, current information — economic calendar events, breaking
headlines, sentiment. Rather than wiring up a separate news API and
managing another set of credentials, this uses Claude's own hosted
web_search tool: the model searches live and reasons over the
results in one call.

Not bar-dependent, so opinions are keyed by symbol only. Runs on its
own schedule (see scheduler.py), independent of the webhook.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from app.text_utils import clean_opinion_text_fields

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You track news and the economic calendar relevant to Nasdaq/tech and US macro data (Fed, CPI, NFP, jobless claims, PCE, and similar releases).

Your job only: assess near-term news risk or sentiment impact — not chart analysis, not price levels.

Search for what's current before answering. Determine:
- Whether a scheduled economic event or a live breaking news story in the next few hours could spike volatility meaningfully for Nasdaq/tech.
- Current sentiment toward tech/Nasdaq right now (bullish, bearish, or neutral), based on what you find.
- If a major event is imminent (within the next 2-3 hours) or already breaking, flag it "urgent" and lower your confidence accordingly — urgent doesn't mean directionally bullish or bearish, it means "expect volatility, be cautious".

Respond with a single JSON object ONLY, no other text, no markdown code fences, matching exactly this shape:
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 short sentences, cite what you found>",
  "key_data": {
    "headlines": [<up to 3 short headline summaries in your own words>],
    "upcoming_events": [<up to 3 short descriptions of near-term scheduled events, or empty list>],
    "sentiment_summary": "<short phrase>"
  },
  "flags": [<zero or more of: "urgent", "low_data", "stale_data">]
}"""


@dataclass
class NewsOpinion:
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


class NewsAgentError(Exception):
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
        raise NewsAgentError(f"model did not return valid JSON: {e}\nraw: {raw_text[:500]}")


def run_news(symbol: str) -> NewsOpinion:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise NewsAgentError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"Symbol context: {symbol} (Nasdaq-100 futures). "
        f"Search for current Nasdaq/tech-relevant news and the economic "
        f"calendar for the next few hours, then respond with the JSON "
        f"object only, as instructed."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_message}],
    )

    # With a hosted (server-side) tool, Claude executes the search itself
    # and the final assistant turn's text blocks contain the answer —
    # we only concatenate "text" blocks, skipping tool_use/tool_result
    # blocks that also appear in the response.
    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    if not raw_text.strip():
        raise NewsAgentError("model returned no text content (only tool-use blocks)")

    parsed = _parse_response(raw_text)
    parsed = clean_opinion_text_fields(parsed)

    required = {"direction", "confidence", "reasoning", "key_data", "flags"}
    missing = required - parsed.keys()
    if missing:
        raise NewsAgentError(f"model response missing required fields: {missing}")

    return NewsOpinion(
        agent="news",
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        symbol=symbol,
        direction=parsed["direction"],
        confidence=int(parsed["confidence"]),
        reasoning=parsed["reasoning"],
        key_data=parsed["key_data"],
        flags=parsed["flags"],
    )
