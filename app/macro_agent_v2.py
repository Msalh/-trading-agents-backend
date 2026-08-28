"""
Macro Agent v2 — shadow schema (Tier 3.44, sixteenth external review
item #5: "only after items 1-4 [of the prospective-experiment
registry] are done, start Macro v2 as a separate shadow").

That gate is now satisfied: Tier 3.43 shipped (immutable record,
required stopping rule, no silent overrides, config/prompt/model drift
detection) and the real overlap-vs-baseline prospective experiment is
registered on production (experiment_id=
51c4fadb-5a90-408e-a106-b41117417c1d, symbol=MNQ1!, timeframe=5m,
registered_watermark_rowid=1276).

This module is a completely separate, purely-additive exploration of a
richer four-axis macro read. Per the review's own repeated caution
(and the user's explicit choice, when asked, for an on-demand-only
trigger rather than an automatic shadow call on every live Macro run):

- It is NEVER wired into app.coordinator scoring or any live trading
  decision. Nothing in the webhook / candidate-creation pipeline calls
  run_macro_shadow_v2() — it only runs when a caller explicitly hits
  the new secret-protected POST /agents/macro-shadow-v2/run endpoint.
  This avoids doubling live Macro LLM calls/cost until the schema's
  usefulness is actually validated against real outcomes.
- It NEVER modifies app/macro_agent.py. SYSTEM_PROMPT, MacroOpinion,
  run_macro(), MODEL, and PROMPT_VERSION="1" are all untouched (verify
  via git diff --stat). The live "direction"/"flags" fields — in
  particular "risk_off" — stay exactly as they were when the Tier 3.43
  prospective experiment above locked them into its locked_config.
- It is stored in its OWN separate table (macro_shadow_opinions_v2),
  never the shared agent_opinions table the live agents write to — so
  it is structurally impossible for this shadow data to be picked up
  by any existing query that reads agent_opinions by symbol/timeframe
  (candidate creation, get_recent_opinions, app.replay.replay_candidate,
  etc.). It cannot silently blend into any live decision or into the
  registered experiment's replay-under-locked-config computation.
- It carries its own MACRO_V2_SCHEMA_VERSION="2" marker — the same
  hand-maintained-marker convention as BACKTEST_LOGIC_VERSION /
  app.news_agent.PROMPT_VERSION / app.macro_agent.PROMPT_VERSION —
  entirely independent of macro_agent.PROMPT_VERSION. Bumping one
  implies nothing about the other.

Four axes — deliberately NOT named "direction"/"flags", so this can
never be confused with or accidentally substituted for the live v1
fields the Coordinator and the registered prospective experiment
already depend on:

- directional_bias: "bullish" | "bearish" | "neutral" — same
  vocabulary as v1's `direction` for readability, but a distinct field
  name on a distinct object.
- tradeability: "favorable" | "choppy" | "avoid" — is the macro
  backdrop itself supportive of acting on ANY directional signal right
  now, independent of which direction that signal points. This is the
  question v1's single "risk_off" flag was standing in for.
- risk_cause: "none" | "monetary_policy" | "geopolitical" | "liquidity"
  | "data_release" | "positioning_flows" | "other" — classifies WHY,
  whenever tradeability is impaired by macro risk, instead of v1's
  single unexplained "risk_off" boolean.
- data_quality: "fresh" | "degraded" | "stale" — a 3-level grading of
  how much the search actually found, replacing v1's binary
  "stale_data" flag with a graded read.

This is exploratory data collection, not a decision yet. Whether any
of this schema ever becomes the live Macro schema — and how that
migration would be done safely — is a separate, later decision.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from app.llm_telemetry import track_llm_call
from app.text_utils import clean_opinion_text_fields

MODEL = "claude-sonnet-5"

# Independent of app.macro_agent.PROMPT_VERSION — see module docstring.
MACRO_V2_SCHEMA_VERSION = "2"

DIRECTIONAL_BIAS_VALUES = ("bullish", "bearish", "neutral")
TRADEABILITY_VALUES = ("favorable", "choppy", "avoid")
RISK_CAUSE_VALUES = (
    "none",
    "monetary_policy",
    "geopolitical",
    "liquidity",
    "data_release",
    "positioning_flows",
    "other",
)
DATA_QUALITY_VALUES = ("fresh", "degraded", "stale")

SYSTEM_PROMPT_V2 = """You monitor the broader macro context relevant to Nasdaq-100 futures (NQ/MNQ): the US Dollar Index (DXY), US 10-Year Treasury yields, and the correlation/behavior between SPX and NDX (Nasdaq-100).

This is an EXPLORATORY four-axis read — it is not used for any live trading decision. Answer each axis independently and honestly; do not force consistency between them beyond what the evidence actually supports.

Search for current levels before answering. Then determine, as four separate axes:

1. directional_bias — does the macro backdrop lean "bullish", "bearish", or "neutral" for NQ right now?
2. tradeability — independent of direction: is this a "favorable" backdrop for acting on ANY directional signal, a "choppy" one (mixed/conflicting signals, elevated but not extreme risk), or one you'd flag to "avoid" trading altogether (extreme volatility, a major risk event in progress, dangerously thin liquidity)?
3. risk_cause — if tradeability is "choppy" or "avoid", classify the primary driver as exactly one of: "monetary_policy" (rate decisions, central bank commentary), "geopolitical" (conflict, sanctions, elections), "liquidity" (thin markets, holiday/off-hours, unusual spreads), "data_release" (a major scheduled economic release), "positioning_flows" (crowded positioning, large fund flows, correlation breakdowns not explained by news), or "other" (explain in risk_cause_detail). If tradeability is "favorable", use "none".
4. data_quality — grade how much you actually found via search: "fresh" (current, specific levels found for DXY/yields/SPX-NDX), "degraded" (found some but not all, or somewhat dated), or "stale" (search failed to return meaningfully current data).

Respond with a single JSON object ONLY, no other text, no markdown code fences, matching exactly this shape:
{
  "directional_bias": "bullish" | "bearish" | "neutral",
  "tradeability": "favorable" | "choppy" | "avoid",
  "risk_cause": "none" | "monetary_policy" | "geopolitical" | "liquidity" | "data_release" | "positioning_flows" | "other",
  "risk_cause_detail": "<short free text, or null when risk_cause is 'none'>",
  "data_quality": "fresh" | "degraded" | "stale",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 short sentences, cite what you found>",
  "key_data": {
    "dxy_read": "<short description of dollar strength/direction>",
    "yields_read": "<short description of US10Y level/direction>",
    "spx_ndx_correlation": "<'in_sync' | 'diverging' | 'unclear'>",
    "notes": "<short free text or null>"
  }
}"""


@dataclass
class MacroShadowOpinionV2:
    schema_version: str
    model: str
    timestamp: str
    symbol: str
    candidate_id: str | None
    directional_bias: str
    tradeability: str
    risk_cause: str
    risk_cause_detail: str | None
    data_quality: str
    confidence: int
    reasoning: str
    key_data: dict

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "candidate_id": self.candidate_id,
            "directional_bias": self.directional_bias,
            "tradeability": self.tradeability,
            "risk_cause": self.risk_cause,
            "risk_cause_detail": self.risk_cause_detail,
            "data_quality": self.data_quality,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "key_data": self.key_data,
        }


class MacroAgentV2Error(Exception):
    pass


def _parse_response_v2(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise MacroAgentV2Error(f"model did not return valid JSON: {e}\nraw: {raw_text[:500]}")


def _validate_axes(parsed: dict) -> None:
    if parsed["directional_bias"] not in DIRECTIONAL_BIAS_VALUES:
        raise MacroAgentV2Error(f"invalid directional_bias: {parsed['directional_bias']!r}")
    if parsed["tradeability"] not in TRADEABILITY_VALUES:
        raise MacroAgentV2Error(f"invalid tradeability: {parsed['tradeability']!r}")
    if parsed["risk_cause"] not in RISK_CAUSE_VALUES:
        raise MacroAgentV2Error(f"invalid risk_cause: {parsed['risk_cause']!r}")
    if parsed["data_quality"] not in DATA_QUALITY_VALUES:
        raise MacroAgentV2Error(f"invalid data_quality: {parsed['data_quality']!r}")


def run_macro_shadow_v2(symbol: str, candidate_id: str | None = None) -> MacroShadowOpinionV2:
    """Runs the exploratory v2 shadow read. `candidate_id` is optional
    and purely a label for later joining this read against a specific
    candidate's actual outcome — it is NEVER validated against
    storage here (this module doesn't import app.storage, deliberately,
    to keep it obviously incapable of touching the live candidate/
    opinion pipeline); the caller (the endpoint) is responsible for
    404ing on an unknown candidate_id before calling this."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise MacroAgentV2Error("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"Symbol context: {symbol} (Nasdaq-100 futures). "
        f"Search for the current DXY level/direction, US 10-Year Treasury "
        f"yield level/direction, and how SPX and NDX are behaving relative "
        f"to each other today, then respond with the JSON object only, as "
        f"instructed."
    )

    with track_llm_call("macro_shadow_v2", MODEL, trigger_context=symbol) as call:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT_V2,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": user_message}],
        )
        call.record(response)

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    if not raw_text.strip():
        raise MacroAgentV2Error("model returned no text content (only tool-use blocks)")

    parsed = _parse_response_v2(raw_text)
    parsed = clean_opinion_text_fields(parsed)

    required = {
        "directional_bias", "tradeability", "risk_cause", "risk_cause_detail",
        "data_quality", "confidence", "reasoning", "key_data",
    }
    missing = required - parsed.keys()
    if missing:
        raise MacroAgentV2Error(f"model response missing required fields: {missing}")
    _validate_axes(parsed)

    return MacroShadowOpinionV2(
        schema_version=MACRO_V2_SCHEMA_VERSION,
        model=MODEL,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        symbol=symbol,
        candidate_id=candidate_id,
        directional_bias=parsed["directional_bias"],
        tradeability=parsed["tradeability"],
        risk_cause=parsed["risk_cause"],
        risk_cause_detail=parsed["risk_cause_detail"],
        data_quality=parsed["data_quality"],
        confidence=int(parsed["confidence"]),
        reasoning=parsed["reasoning"],
        key_data=parsed["key_data"],
    )
