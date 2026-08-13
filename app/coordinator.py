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
0 magnitude, regardless of its confidence. A second external review
correctly pointed out this makes MIN_AVAILABLE_WEIGHT partly
ineffective: Analysis (40%) + a present-but-neutral Timing (20%) =
exactly 60%, clearing the minimum even though Timing added zero real
evidence. Deliberately not restructured in this pass (would mean
redesigning the weighting scheme itself, tracked as Tier 2.8) — but
documented here so it's not mistaken for solved.

Conflict handling: if Analysis and News point in opposite directions
and News is flagged "urgent", the score is dampened rather than
letting one agent's confidence silently cancel the other's — this
matches the "never ignore the flag" rule from the agreed prompts.

Missing opinions (an agent that hasn't run yet, or ran too long ago)
are excluded rather than treated as neutral. Two genuinely different
"don't know" cases are now tracked separately:
  - missing_agents: the agent has NEVER produced an opinion for this
    symbol/timeframe (get_latest_opinion returned nothing at all).
  - stale_agents: an opinion exists but is older than its type's max
    age (or has an unparseable/future timestamp — clock skew or a
    corrupted write, treated conservatively as untrustworthy either
    way), so it's excluded from the score exactly like a missing one,
    but the two are no longer conflated in the reported lists.

MIN_AVAILABLE_WEIGHT: if the combined weight of agents that actually
have a current opinion falls below this fraction of the total, the
decision is "insufficient_data" rather than trading on a lopsided
subset. Known incomplete (see Timing note above) — not re-tuned in
this pass, since the fix is a weighting redesign, not a bigger number.

opinions_used: every CoordinatorDecision now carries the exact
opinions dict it was scored from. This is what makes a "trade
candidate" (see app/candidates.py) an atomic snapshot instead of
downstream stages re-querying "latest" independently and risking a
mismatched combination.
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

# Minimum fraction of total weight that must actually be available
# (fresh opinions present) before the Coordinator will make any
# directional call at all. Below this, it's "insufficient_data"
# regardless of how confident the available agents are.
MIN_AVAILABLE_WEIGHT = float(os.environ.get("MIN_AVAILABLE_WEIGHT", "0.6"))

# How old an opinion can be before it's treated as if the agent never
# ran — separate thresholds since Analysis is bar-driven (every 5min
# in session) while News/Macro run on a slower scheduler (60min).
# Known-blunt (flagged by review): not yet timeframe-aware for
# Analysis, not yet event/regime-aware for News/Macro — tracked as
# Tier 2 work, not fixed in this pass.
ANALYSIS_MAX_AGE_MINUTES = int(os.environ.get("ANALYSIS_MAX_AGE_MINUTES", "15"))
NEWS_MACRO_MAX_AGE_MINUTES = int(os.environ.get("NEWS_MACRO_MAX_AGE_MINUTES", "90"))
_MAX_AGE_MINUTES = {
    "analysis": ANALYSIS_MAX_AGE_MINUTES,
    "news": NEWS_MACRO_MAX_AGE_MINUTES,
    "macro": NEWS_MACRO_MAX_AGE_MINUTES,
}

# How far in the future a timestamp can be (clock skew tolerance)
# before it's treated as suspect rather than fresh. A materially
# future-dated opinion is a data integrity problem, not a fast clock.
_FUTURE_SKEW_TOLERANCE_MINUTES = 2

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
    stale_agents: list[str]
    conflict_flags: list[str]
    summary: str
    opinions_used: dict = field(default_factory=dict)
    # Tier 2.5 (versioning): the exact weights/threshold/min_available_weight
    # this decision was scored under — resolved ONCE at scoring time, not
    # whatever the env vars happen to be whenever this is read back later.
    # Without this, a replay has no reliable way to know what config
    # actually produced a historical decision.
    config_version: dict = field(default_factory=dict)

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
            "stale_agents": self.stale_agents,
            "conflict_flags": self.conflict_flags,
            "summary": self.summary,
            "opinions_used": self.opinions_used,
            "config_version": self.config_version,
        }


def _parse_utc(timestamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _is_stale(opinion_timestamp: str | None, max_age_minutes: int) -> bool:
    """An opinion with no parseable timestamp is treated as stale —
    untrustworthy, not "assume it's fine". A materially future-dated
    timestamp is also stale (data integrity issue, not freshness)."""
    dt = _parse_utc(opinion_timestamp) if opinion_timestamp else None
    if dt is None:
        return True
    age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if age_minutes < -_FUTURE_SKEW_TOLERANCE_MINUTES:
        return True
    return age_minutes > max_age_minutes


def _gather_opinions(symbol: str, timeframe: str) -> tuple[dict, list[str], list[str]]:
    """Collect the latest opinion from each agent. Analysis is keyed
    by symbol+timeframe (bar-dependent); News/Macro are keyed by
    symbol+"global" (not bar-dependent); Timing is computed fresh
    from the latest market_state timestamp (it's pure logic, always
    available and always current if we have any market data at all —
    never subject to the staleness check below, though the market
    bar it's derived from has no freshness check of its own either —
    a known gap if the webhook stops delivering, tracked as Tier 2).

    Returns (opinions, missing_agents, stale_agents) — three disjoint
    sets: an agent is in exactly one of "present in opinions",
    missing (never produced an opinion at all), or stale (produced
    one, but it's too old/unparseable/future-dated to trust)."""
    opinions: dict = {}
    missing_agents: list[str] = []
    stale_agents: list[str] = []

    for agent_name, timeframe_key, max_age in (
        ("analysis", timeframe, _MAX_AGE_MINUTES["analysis"]),
        ("news", "global", _MAX_AGE_MINUTES["news"]),
        ("macro", "global", _MAX_AGE_MINUTES["macro"]),
    ):
        opinion = get_latest_opinion(agent=agent_name, symbol=symbol, timeframe=timeframe_key)
        if opinion is None:
            missing_agents.append(agent_name)
        elif _is_stale(opinion.get("timestamp"), max_age):
            stale_agents.append(agent_name)
        else:
            opinions[agent_name] = opinion

    latest_bar = get_latest(symbol=symbol, timeframe=timeframe)
    if latest_bar is not None:
        timing = evaluate_timing(latest_bar["timestamp"])
        opinions["timing"] = timing.to_dict()
    else:
        missing_agents.append("timing")

    return opinions, missing_agents, stale_agents


def _score_opinions(
    symbol: str,
    timeframe: str,
    opinions: dict,
    missing_agents: list[str],
    stale_agents: list[str],
    weights: dict,
    threshold: float,
    min_available_weight: float,
) -> CoordinatorDecision:
    """The actual scoring math — pulled out of compute_decision so it
    can run against ANY opinions/missing/stale snapshot under ANY
    weights/threshold/min_available_weight, not just the live env-var
    config against a fresh DB read. This is what makes replay
    (app/replay.py, Tier 2.5) possible: a trade candidate already
    freezes opinions_used/missing_agents/stale_agents (Tier 2.1), so
    re-running this function against that frozen snapshot with a
    hypothetical config recomputes exactly what the Coordinator would
    have decided — entirely offline, no new data, no LLM calls.

    weights.get(agent, 0) (not weights[agent]) deliberately tolerates
    a replay's hypothetical weights dict omitting an agent — that's a
    valid way to ask "what if this agent's weight were 0", not a bug
    to guard against."""
    available_weight = sum(weights.get(a, 0) for a in opinions)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config_version = {
        "weights": dict(weights),
        "threshold": threshold,
        "min_available_weight": min_available_weight,
    }

    if available_weight < min_available_weight:
        reason = (
            "No agent opinions available yet — nothing to aggregate."
            if available_weight == 0
            else (
                f"Only {available_weight:.0%} of total agent weight is currently available "
                f"(minimum {min_available_weight:.0%} required) — not enough combined evidence "
                f"to trade on, regardless of how confident the available agents are."
            )
        )
        return CoordinatorDecision(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=now_iso,
            score=0.0,
            threshold=threshold,
            decision="insufficient_data",
            direction="neutral",
            contributions={},
            missing_agents=missing_agents,
            stale_agents=stale_agents,
            conflict_flags=[],
            summary=reason,
            opinions_used=opinions,
            config_version=config_version,
        )

    contributions = {}
    weighted_sum = 0.0
    for agent, opinion in opinions.items():
        direction_value = _DIRECTION_VALUE.get(opinion.get("direction", "neutral"), 0)
        confidence = opinion.get("confidence", 0)
        weight = weights.get(agent, 0)
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

    if score > threshold:
        decision = "enter_long"
        direction = "bullish"
    elif score < -threshold:
        decision = "enter_short"
        direction = "bearish"
    else:
        decision = "no_trade"
        direction = "neutral"

    summary_bits = [f"score={score:.1f} (threshold={threshold})"]
    if missing_agents:
        summary_bits.append(f"missing: {', '.join(missing_agents)}")
    if stale_agents:
        summary_bits.append(f"stale: {', '.join(stale_agents)}")
    if conflict_flags:
        summary_bits.append(f"conflicts: {', '.join(conflict_flags)}")
    summary = f"{decision} — " + "; ".join(summary_bits)

    return CoordinatorDecision(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=now_iso,
        score=score,
        threshold=threshold,
        decision=decision,
        direction=direction,
        contributions=contributions,
        missing_agents=missing_agents,
        stale_agents=stale_agents,
        conflict_flags=conflict_flags,
        summary=summary,
        opinions_used=opinions,
        config_version=config_version,
    )


def compute_decision(symbol: str, timeframe: str) -> CoordinatorDecision:
    opinions, missing_agents, stale_agents = _gather_opinions(symbol=symbol, timeframe=timeframe)
    return _score_opinions(
        symbol=symbol,
        timeframe=timeframe,
        opinions=opinions,
        missing_agents=missing_agents,
        stale_agents=stale_agents,
        weights=WEIGHTS,
        threshold=DECISION_THRESHOLD,
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
