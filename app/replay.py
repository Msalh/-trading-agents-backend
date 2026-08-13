"""
Replay / versioning — Tier 2.5 (external review, Aug 2026). Extended
in Tier 3.4 (COORDINATOR_THRESHOLD tuning) with sweep_thresholds() —
see that function's docstring below.

The problem this solves: COORDINATOR_THRESHOLD, the four agent
WEIGHTS, and MIN_AVAILABLE_WEIGHT have all changed over this
project's lifetime, set via env vars. Nothing recorded which config
produced a given historical decision, so old candidates couldn't
self-document what actually decided them — and there was no way to
ask "what would the Coordinator have decided on this same historical
moment under a DIFFERENT config?" without a live agent re-run
(impossible — the moment has passed, the LLM opinions from that
moment either matches what's frozen or is gone).

Two things make this possible now:
  - Tier 2.1 (app/candidates.py) already freezes opinions_used /
    missing_agents / stale_agents on every trade candidate — an
    atomic, immutable snapshot of exactly what the Coordinator saw.
  - Tier 2.5 pulled the actual scoring math out of compute_decision()
    into coordinator._score_opinions(), parameterized by
    weights/threshold/min_available_weight instead of reading module-
    level env vars directly, and coordinator.CoordinatorDecision now
    carries a config_version field recording exactly which config
    produced it.

replay_candidate() re-runs _score_opinions() against a candidate's
frozen snapshot under either the CURRENT live config (default — "what
would this decide today?") or an explicit hypothetical config passed
by the caller ("what if threshold had been 35?") — entirely offline,
no new market data, no LLM calls, no mutation of the original
candidate row. The original decision is never touched or recomputed
in place; replay only ever returns a new, separate result for
comparison.

Candidates created before this tier has no config_version recorded
(field defaults to {} via dataclass field(default_factory=dict)) —
surfaced as None to the caller rather than an empty dict pretending
to be a real answer, so it's never confused with a config that was
deliberately empty.
"""

from app.candidates import get_candidate_history
from app.coordinator import DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS, _score_opinions
from app.outcomes import HORIZON_MINUTES_DEFAULT, compute_outcomes_for_decision

_DIRECTIONAL_DECISIONS = ("enter_long", "enter_short")


def replay_candidate(
    candidate: dict,
    weights: dict = None,
    threshold: float = None,
    min_available_weight: float = None,
    include_outcome: bool = False,
    outcome_horizons: list[int] = None,
) -> dict:
    """Re-scores one candidate's frozen opinions_used under a config —
    live config by default, or an explicit hypothetical override for
    any of weights/threshold/min_available_weight (omitted fields fall
    back to the current live value, not the original candidate's
    config, since "what would this decide under today's threshold but
    the original weights" is also a valid question to ask).

    include_outcome=True additionally computes the ORIGINAL Sprint 14
    hypothetical horizon estimate (price-direction only, not a real
    trade — replay never opens or touches paper trades) anchored to
    the candidate's original decision timestamp, for whichever
    direction the REPLAYED decision points — answering "if the
    Coordinator had made this call instead, would price have agreed?"
    Only computed when the replayed decision is directional; no-op
    otherwise (nothing to evaluate)."""
    decision = candidate["decision"]
    opinions_used = decision.get("opinions_used") or {}
    missing_agents = decision.get("missing_agents") or []
    stale_agents = decision.get("stale_agents") or []
    original_config = decision.get("config_version") or None

    use_weights = weights if weights is not None else WEIGHTS
    use_threshold = threshold if threshold is not None else DECISION_THRESHOLD
    use_min_weight = min_available_weight if min_available_weight is not None else MIN_AVAILABLE_WEIGHT

    replayed = _score_opinions(
        symbol=candidate["symbol"],
        timeframe=candidate["timeframe"],
        opinions=opinions_used,
        missing_agents=missing_agents,
        stale_agents=stale_agents,
        weights=use_weights,
        threshold=use_threshold,
        min_available_weight=use_min_weight,
    ).to_dict()

    result = {
        "candidate_id": candidate["candidate_id"],
        "symbol": candidate["symbol"],
        "timeframe": candidate["timeframe"],
        "original_decision_timestamp": decision.get("timestamp"),
        "original": {
            "decision": decision.get("decision"),
            "direction": decision.get("direction"),
            "score": decision.get("score"),
            "threshold": decision.get("threshold"),
            "config_version": original_config,
        },
        "replayed": replayed,
        "changed": replayed["decision"] != decision.get("decision"),
    }

    if include_outcome and replayed["decision"] in _DIRECTIONAL_DECISIONS:
        result["replayed_hypothetical_outcome"] = compute_outcomes_for_decision(
            symbol=candidate["symbol"],
            timeframe=candidate["timeframe"],
            decision={"decision": replayed["decision"], "timestamp": decision.get("timestamp")},
            horizons=outcome_horizons,
        )

    return result


def replay_candidates_for_symbol(
    symbol: str,
    timeframe: str,
    weights: dict = None,
    threshold: float = None,
    min_available_weight: float = None,
    limit: int = 50,
    only_changed: bool = False,
    include_outcome: bool = False,
    outcome_horizons: list[int] = None,
) -> list[dict]:
    """Bulk replay over recent candidate history — same ordering as
    get_candidate_history (most recent first). only_changed=True
    filters to candidates whose replayed decision differs from what
    actually happened at the time, the cases worth looking at when
    tuning a config change."""
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)
    results = [
        replay_candidate(
            c,
            weights=weights,
            threshold=threshold,
            min_available_weight=min_available_weight,
            include_outcome=include_outcome,
            outcome_horizons=outcome_horizons,
        )
        for c in candidates
    ]
    if only_changed:
        results = [r for r in results if r["changed"]]
    return results


def summarize_replay(results: list[dict]) -> dict:
    """Aggregates a list of replay_candidate() results into transition
    counts — how many decisions would flip, and to/from what — useful
    at a glance before reading every individual candidate."""
    total = len(results)
    changed = [r for r in results if r["changed"]]
    transitions: dict[str, int] = {}
    for r in changed:
        key = f"{r['original']['decision']} -> {r['replayed']['decision']}"
        transitions[key] = transitions.get(key, 0) + 1

    return {
        "total_candidates": total,
        "changed": len(changed),
        "unchanged": total - len(changed),
        "transitions": transitions,
    }


def _summarize_directional_accuracy(replay_results: list[dict], horizons: list[int]) -> dict:
    """Shared aggregation for one threshold's worth of replay results
    — same per-horizon shape outcomes.summarize_outcomes() already
    uses for its hypothetical bucket (correct/incorrect/flat/pending/
    no_data counts, plus accuracy = correct / (correct + incorrect)),
    reused here rather than reinvented so the two "hypothetical
    accuracy" numbers this project produces are always computed the
    same way."""
    directional = [r for r in replay_results if r["replayed"]["decision"] in _DIRECTIONAL_DECISIONS]
    horizon_counts = {
        h: {"correct": 0, "incorrect": 0, "flat": 0, "pending": 0, "no_data": 0} for h in horizons
    }
    for r in directional:
        outcome_by_horizon = r.get("replayed_hypothetical_outcome") or {}
        for h in horizons:
            outcome = outcome_by_horizon.get(h)
            if outcome is None:
                continue
            bucket = horizon_counts[h]
            bucket[outcome["outcome"]] = bucket.get(outcome["outcome"], 0) + 1

    by_horizon = {}
    for h, counts in horizon_counts.items():
        resolved = counts["correct"] + counts["incorrect"]
        by_horizon[h] = {**counts, "accuracy": round(counts["correct"] / resolved, 3) if resolved else None}

    return {"directional_candidates": len(directional), "by_horizon_minutes": by_horizon}


def sweep_thresholds(
    symbol: str,
    timeframe: str,
    thresholds: list[float],
    limit: int = 100,
    weights: dict = None,
    min_available_weight: float = None,
    horizons: list[int] = None,
) -> dict:
    """Tier 3.4 (COORDINATOR_THRESHOLD tuning) — the actual tool for
    the question this whole replay/outcome machinery exists to answer:
    "across a range of threshold values, how does directional decision
    volume and hypothetical accuracy change?" Built for exactly the
    case app/main.py's other summary endpoints already handle —
    everything replay_candidate() already does (offline re-scoring of
    frozen opinions_used, no LLM calls, no mutation, no trade side
    effects) plus outcomes' hypothetical horizon estimate, just swept
    across many threshold values in one call and pre-aggregated so the
    caller gets a compact per-threshold summary instead of having to
    fetch and tally N full candidate lists themselves — this matters
    in practice, since the raw per-candidate replay list can be too
    large for some callers (e.g. an LLM-mediated fetch) to reliably
    process whole.

    weights/min_available_weight are held FIXED across the whole sweep
    (only threshold varies) — sweeping more than one axis at once
    would confound which change caused an accuracy shift; a caller who
    wants to sweep weights too should call this once per weights
    config and compare the results.

    Real trade P&L is deliberately NOT part of this: a replayed
    decision under a hypothetical threshold was never actually
    executed, so there's no real fill/slippage/size to attribute to
    it — only the same hypothetical horizon price-direction estimate
    outcomes.py already uses as its fallback for candidates that never
    became a trade. This is an accuracy proxy for tuning, not a
    backtest of what P&L would have been."""
    horizons = horizons or HORIZON_MINUTES_DEFAULT
    candidates = get_candidate_history(symbol=symbol, timeframe=timeframe, limit=limit)

    sweep = {}
    for threshold in thresholds:
        replay_results = [
            replay_candidate(
                c,
                weights=weights,
                threshold=threshold,
                min_available_weight=min_available_weight,
                include_outcome=True,
                outcome_horizons=horizons,
            )
            for c in candidates
        ]
        sweep[threshold] = _summarize_directional_accuracy(replay_results, horizons)

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candidates_considered": len(candidates),
        "weights_held_fixed": weights if weights is not None else WEIGHTS,
        "min_available_weight_held_fixed": (
            min_available_weight if min_available_weight is not None else MIN_AVAILABLE_WEIGHT
        ),
        "sweep": sweep,
    }
