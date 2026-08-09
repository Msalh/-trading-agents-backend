"""
Coordinator — Sprint 6.

Pure aggregation logic — no LLM call of its own. Reads whatever the
four agents last reported (Analysis, News, Timing, Macro) and
combines them into a weighted score and a preliminary decision.

Fixed weights, as agreed: Analysis 40% / News 25% / Timing 20% / Macro 15%.

Important design note on Timing: its "direction" is always "neutral"
by design (see timing_agent.py) — timing quality has no directional
opinion, it only gates whether now is a reasonable time to act at
all. That means Timing's slot in the weighted sum always contributes
0 magnitude, regardless of its confidence. This is not a bug: Timing
already did its real job earlier, as the gate that decides whether
Analysis runs in the first place. Its presence here is for schema
symmetry with the roadmap's original agent table, not because it
swings the score.

Conflict handling: if Analysis and News point in opposite directions
and News is flagged "urgent", the score is dampened rather than
letting one agent's confidence silently cancel the other's — this
matches the "never ignore the flag" rule from the agreed prompts.

Missing opinions (an agent that hasn't run yet, or ran too long ago)
are excluded rather than treated as neutral — a genuine "don't know"
is not the same as "neutral", so weights are re-normalized across
only the agents that actually have a current opinion.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.storage import get_latest, get_latest_opinion
from app.timing_agent import evaluate_timing

WEIGHTS = {
    "analysis": 0.40,
    "news": 0.25,
    "timing": 0.20,
    "macro": 0.15,
}

# Placeholder — the roadmap explicitly calls out that this needs
# tuning against real/replayed data before going live. 25 means: the
# weighted, confidence-scaled score (range -100..100) must exceed
# this magnitude before the Coordinator prefers a direction at all.
DECISION_THRESHOLD = float(os.environ.get("COORDINATOR_THRESHOLD", "25"))

_DIRECTION_VALUE = {"bullish": 1, "neutral": 0, "bearish": -1}


@dataclass
class CoordinatorDecision:
    symbol: str
    timeframe: str
    timestamp: str
    score: float
    threshold: float
    decision: str  # "enter_long" | "enter_short" | "no_trade" | "insufficient_data"
    direction: str  # "bullish" | "bearish" | "neutral"
    contributions: dict
    missing_agents: list[str]
    conflict_flags: list[str]
    summary: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "score": round(self.score, 2),
            "threshold": self.threshold,
            "decision": self.decision,
            "direction": self.direction,
            "contributions": self.contributions,
            "missing_agents": self.missing_agents,
            "conflict_flags": self.conflict_flags,
            "summary": self.summary,
        }


def _gather_opinions(symbol: str, timeframe: str) -> dict:
    """Collect the latest opinion from each agent. Analysis is keyed
    by symbol+timeframe (bar-dependent); News/Macro are keyed by
    symbol+"global" (not bar-dependent); Timing is computed fresh
    from the latest market_state timestamp (it's pure logic, always
    available if we have any market data at all)."""
    opinions: dict = {}

    analysis = get_latest_opinion(agent="analysis", symbol=symbol, timeframe=timeframe)
    if analysis is not None:
        opinions["analysis"] = analysis

    news = get_latest_opinion(agent="news", symbol=symbol, timeframe="global")
    if news is not None:
        opinions["news"] = news

    macro = get_latest_opinion(agent="macro", symbol=symbol, timeframe="global")
    if macro is not None:
        opinions["macro"] = macro

    latest_bar = get_latest(symbol=symbol, timeframe=timeframe)
    if latest_bar is not None:
        timing = evaluate_timing(latest_bar["timestamp"])
        opinions["timing"] = timing.to_dict()

    return opinions


def compute_decision(symbol: str, timeframe: str) -> CoordinatorDecision:
    opinions = _gather_opinions(symbol=symbol, timeframe=timeframe)
    missing_agents = [a for a in WEIGHTS if a not in opinions]

    available_weight = sum(WEIGHTS[a] for a in opinions)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if available_weight == 0:
        return CoordinatorDecision(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=now_iso,
            score=0.0,
            threshold=DECISION_THRESHOLD,
            decision="insufficient_data",
            direction="neutral",
            contributions={},
            missing_agents=missing_agents,
            conflict_flags=[],
            summary="No agent opinions available yet — nothing to aggregate.",
        )

    contributions = {}
    weighted_sum = 0.0
    for agent, opinion in opinions.items():
        direction_value = _DIRECTION_VALUE.get(opinion.get("direction", "neutral"), 0)
        confidence = opinion.get("confidence", 0)
        weight = WEIGHTS[agent]
        contribution = direction_value * confidence * weight
        weighted_sum += contribution
        contributions[agent] = {
            "direction": opinion.get("direction"),
            "confidence": confidence,
            "weight": weight,
            "contribution": round(contribution, 2),
        }

    # Re-normalize by the weight actually available, so a missing
    # agent doesn't silently drag the score toward zero just because
    # its slot was empty.
    score = weighted_sum / available_weight

    conflict_flags: list[str] = []
    if "analysis" in opinions and "news" in opinions:
        a_dir = opinions["analysis"].get("direction")
        n_dir = opinions["news"].get("direction")
        n_flags = opinions["news"].get("flags", [])
        opposing = (
            a_dir in ("bullish", "bearish")
            and n_dir in ("bullish", "bearish")
            and a_dir != n_dir
        )
        if opposing and "urgent" in n_flags:
            score *= 0.5
            conflict_flags.append("analysis_news_conflict_urgent_dampened")
        elif opposing:
            conflict_flags.append("analysis_news_conflict")

    if score > DECISION_THRESHOLD:
        decision = "enter_long"
        direction = "bullish"
    elif score < -DECISION_THRESHOLD:
        decision = "enter_short"
        direction = "bearish"
    else:
        decision = "no_trade"
        direction = "neutral"

    summary_bits = [f"score={score:.1f} (threshold={DECISION_THRESHOLD})"]
    if missing_agents:
        summary_bits.append(f"missing: {', '.join(missing_agents)}")
    if conflict_flags:
        summary_bits.append(f"conflicts: {', '.join(conflict_flags)}")
    summary = f"{decision} — " + "; ".join(summary_bits)

    return CoordinatorDecision(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=now_iso,
        score=score,
        threshold=DECISION_THRESHOLD,
        decision=decision,
        direction=direction,
        contributions=contributions,
        missing_agents=missing_agents,
        conflict_flags=conflict_flags,
        summary=summary,
    )
