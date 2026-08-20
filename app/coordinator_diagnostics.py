"""
Coordinator/Analysis divergence + ablation diagnostic — Tier 3.16.

Every backtest-lite result since Tier 3.10 has shown Coordinator's own
blended decision performing at or below Analysis alone, and Tier 3.12/
3.14's paired and grid comparisons went further: on the current
(small) accepted-candidate sets, Coordinator and Analysis produced
byte-for-byte IDENTICAL trade outcomes. The third external review
endorsed investigating this directly, but pushed back on doing it as a
shallow "are the two directions identical?" check — that conflates
several genuinely different situations (Coordinator agreeing with
Analysis, Coordinator abstaining where Analysis had an opinion,
Coordinator actively overriding Analysis, Coordinator unable to decide
at all) into one number, and misses the more important question: does
Coordinator's blending (News/Macro/Timing on top of Analysis) ever
actually change what would have happened, and is that change additive
(catches bad Analysis calls, adds real signal) or just noise/cost?

This module answers that with tooling that already exists rather than
new scoring logic: every trade candidate already freezes its full
opinions_used/contributions/conflict_flags snapshot (Tier 2.1), and
app/replay.py's replay_candidate() can already re-score that frozen
snapshot under a hypothetical weights dict entirely offline. The
"ablation" here is a per-candidate causal replay: remove one
directional agent's actual opinion from the frozen snapshot (as if it
had never been gathered for that specific decision) and see whether
the final decision changes — a real causal answer ("would this
specific decision have been different without News?"), not just a
correlational one ("how often do Analysis and Coordinator happen to
agree?").

Tier 3.17 correction: the original Tier 3.16 implementation modeled
"remove agent X" by zeroing X's weight in the WEIGHTS dict passed to
replay_candidate (weights={**WEIGHTS, X: 0.0}). That looked
equivalent but wasn't: _score_opinions' MIN_AVAILABLE_WEIGHT gate
divides by directional_weight_total = sum(weights[a] for a in
DIRECTIONAL_AGENTS), computed over ALL three agents regardless of
whether they were actually present for a given candidate. Zeroing
X's weight shrinks that denominator for EVERY candidate being
replayed, including ones where X was never present to begin with —
which can push a previously-insufficient_data candidate over the
0.6 availability bar through pure renormalization, with nothing to
do with X's real influence. Checked against production data
(2026-08-16, 197 candidates): 36 candidates where only Analysis was
present (News and Macro both absent) flipped out of insufficient_data
under BOTH the news-ablation AND macro-ablation pass, with identical
transition splits — a clear signature of this artifact, not a real
finding about News or Macro's influence.

The fix (_ablate_agent below): instead of zeroing a weight in the
WEIGHTS config, remove the ablated agent's opinion from the frozen
opinions_used snapshot and add it to missing_agents — exactly modeling
"this agent's input was genuinely unavailable for this decision" the
same way a real missing-agent scenario already works, under the
UNMODIFIED live WEIGHTS/directional_weight_total. A candidate where
the agent was never present to begin with is now correctly a no-op
(the modified snapshot is identical to the original), so it can no
longer produce a false "changed" result. Each ablation entry also now
reports agent_present_count — how many candidates actually had that
agent's opinion to remove — so decision_changed can never exceed it,
a built-in sanity check the old zeroed-weight approach didn't have.

Read-only, offline, no LLM calls, no new candidates or trades — this
walks candidate history exactly as backtest.py and replay.py already
do. COORDINATOR_THRESHOLD and Coordinator scoring are untouched; the
per-candidate opinion removal here is a throwaway copy used only for
one offline replay, never persisted and never touching the live
WEIGHTS config or a stored candidate.

Tier 3.21 (ablation reclassification, fourth external review,
2026-08-18): Tier 3.17 fixed the false-positive renormalization
artifact, but the review pointed out the surviving raw decision_changed
percentages (82%/47%/21% for analysis/news/macro) still conflate two
genuinely different effects: an agent's removal dropping available
evidence below MIN_AVAILABLE_WEIGHT (a "quorum" effect — the decision
becomes insufficient_data because there wasn't ENOUGH data left, which
says nothing about whether that agent's own DIRECTION was useful) vs
an agent's removal shifting the weighted score enough to cross the
±threshold boundary among candidates that stayed data-sufficient on
both sides (a real directional-influence effect). Analysis's 82% is
almost entirely the former (removing 40% of the directional weight
pool from an 80%-wide total very often crosses the 0.6 availability
floor by itself) — reporting it as one "changed" number reads as "82%
of the time Analysis's DIRECTION mattered," which overstates what was
actually measured.

_classify_ablation_change() below splits every changed decision into
exactly one of three mutually exclusive categories: "to_insufficient_
data" (the ablated agent's removal alone dropped the candidate below
the availability gate — ablation is monotonic in this one direction,
since removing evidence can only shrink availability, never grow it,
so this category can never appear on the ORIGINAL side), "direction_
flipped" (both sides stayed data-sufficient and directional, but the
call reversed — bullish became bearish or vice versa, the strongest
form of "this agent's direction mattered"), and "threshold_crossing"
(everything else that changed — e.g. enter_long <-> no_trade — the
weighted score moved across just one boundary without reversing sign).
Each ablation entry also reports conflict_flags_changed_count (how
often removing the agent also changed which conflict/timing flags
fired — relevant mainly for news, since analysis_news_conflict can
only exist when both are present) and avg_abs_score_delta_when_changed/
_when_unchanged (the raw magnitude of score movement either way, since
even a decision that didn't change categories can still show the
agent moved the score by a meaningful amount, or an agent that "never
changed anything" by category can still be shown to have near-zero
score influence, closing the loop the fourth review specifically named
open: "Macro غيّر 21% من القرارات قد يعني فقط Macro كان مطلوبًا
لإكمال النصاب"). transitions (the raw {original}->{replayed} decision
pair counts, unchanged since Tier 3.16) is kept alongside the new
category breakdown, not replaced by it — this is additive detail, not
a redefinition of any existing field.

Tier 3.26 (News/Macro threshold-crossing deep dive, fifth external
review, item #6): Tier 3.21 named threshold_crossing as "everything
that isn't a quorum effect or a direction reversal" but stopped at the
count — 32/223-present for News, 2/215-present for Macro (production,
confirmed prior to this tier). A count alone doesn't say whether those
crossings were GOOD for the strategy (the agent's presence stopped a
losing trade, or added a winning one Analysis alone would have
missed) or just noise. compute_threshold_crossing_deep_dive() answers
that for one agent at a time by re-walking ONLY that agent's
threshold_crossing subset (reusing _ablate_agent/_classify_ablation_
change, not new scoring logic) and adding four dimensions the raw
count doesn't have:

  - side: every threshold_crossing case is either the ablated agent's
    presence being the reason a real trade WAS taken
    ("agent_enabled_trade" — original decision was directional,
    replayed-without-the-agent was no_trade) or the reason a trade was
    NOT taken ("agent_prevented_trade" — the reverse). These need
    different outcome machinery: an agent_enabled_trade candidate is a
    REAL historical decision that may have become a real paper trade,
    so its outcome comes from app.outcomes.compute_outcome_for_candidate()
    (prefers real closed-trade P&L, same as everywhere else in this
    project). An agent_prevented_trade candidate never happened — there
    is no real trade to look up — so its outcome comes from the
    REPLAYED decision's own hypothetical horizon estimate, which
    replay_candidate(include_outcome=True) already computes anchored to
    the original decision's timestamp. Because ablation only ever
    removes evidence, a to_insufficient_data or unrelated transition
    can never land in this "side" split; a defensive "other" bucket
    catches anything that doesn't fit either pattern instead of
    silently mis-tagging it.
  - agreement_with_analysis: whether the ablated agent's own real
    opinion direction (before ablation) agreed or opposed Analysis's
    own real opinion direction on that same candidate — same
    same-direction/opposing-direction question news_impact/macro_impact
    already ask in compute_coordinator_divergence_report, but scoped to
    just this agent's threshold-crossing subset.
  - the agent's own self-reported flags (e.g. News's "urgent"/
    "low_data"/"stale_data" or Macro's "risk_off"/"conflicting_
    signals"/"stale_data" — app/news_agent.py and app/macro_agent.py
    define different vocabularies, so no flag name is assumed to mean
    the same thing for both agents). urgent_flag_count in the summary
    specifically counts "urgent" — a flag only News's prompt defines —
    so it reads 0 for Macro by construction (Macro has no comparable
    urgency concept in its own vocabulary), not because urgency never
    matters for Macro.
  - distinct_opinion_timestamps: how many of the cases in this subset
    actually trace back to distinct underlying agent opinions, vs. the
    same slow-cadence News/Macro call (NEWS_INTERVAL_MINUTES/
    MACRO_INTERVAL_MINUTES, default 60, reusable up to
    NEWS_MACRO_MAX_AGE_MINUTES, default 90) being counted once per
    candidate that reused it — the same duplication concern Tier 3.6
    raised for per-agent accuracy, applied here.

Outcome vocabulary is deliberately NOT collapsed into a single win/
loss boolean, consistent with the rest of this project: an
agent_enabled_trade candidate reports either a real trade's
win/loss/breakeven/pending/cancelled status (with real pnl_usd when
closed) or, if it never became a trade, the same per-horizon
hypothetical correct/incorrect/flat/pending/no_data breakdown
outcomes.py already uses elsewhere. An agent_prevented_trade candidate
reports a per-horizon breakdown too, but relabeled for what "correct"/
"incorrect" mean when the trade never happened: the hypothetical
direction being "correct" means the prevented trade WOULD have won
(prevented_win — a missed opportunity), and "incorrect" means the
trade would have lost, i.e. the agent's real presence correctly
avoided it (prevented_loss). "flat"/"pending"/"no_data" keep their
existing meaning.

Read-only, offline, no LLM calls, no new candidates or trades — same
guarantee as the rest of this module and app/replay.py.
"""

from app.coordinator import DIRECTIONAL_AGENTS, WEIGHTS
from app.outcomes import HORIZON_MINUTES_DEFAULT, compute_outcome_for_candidate
from app.replay import replay_candidate

_DIRECTIONAL_DECISIONS = ("enter_long", "enter_short")
_DECISION_DIRECTION = {"enter_long": "bullish", "enter_short": "bearish"}
_PREVENTED_OUTCOME_LABEL = {
    "correct": "prevented_win",
    "incorrect": "prevented_loss",
    "flat": "flat",
    "pending": "pending",
    "no_data": "no_data",
}


def _analysis_bucket(opinions_used: dict, missing_agents: list[str], stale_agents: list[str]) -> str:
    """Three-way bucket for what Analysis contributed to this specific
    candidate: "directional" (a real bullish/bearish opinion was used),
    "neutral" (Analysis ran and returned "neutral" — a real look, no
    lean), or "unavailable" (missing or stale — Coordinator scored
    without it entirely, same distinction Tier 3.8 already tracks)."""
    if "analysis" in missing_agents or "analysis" in stale_agents:
        return "unavailable"
    analysis_opinion = opinions_used.get("analysis")
    if not analysis_opinion:
        return "unavailable"
    direction = analysis_opinion.get("direction")
    if direction in ("bullish", "bearish"):
        return "directional"
    return "neutral"


def _named_category(analysis_bucket: str, analysis_direction: str | None, coordinator_decision: str, coordinator_direction: str | None) -> str:
    """The reviewer's five specific named categories, derived from the
    (analysis_bucket, coordinator_decision) pair — kept separate from
    the full cross_tab below so a reader gets a direct answer to the
    exact categories asked for without having to reassemble them."""
    if analysis_bucket == "directional":
        if coordinator_decision == "insufficient_data":
            return "analysis_directional_coordinator_insufficient_data"
        if coordinator_decision == "no_trade":
            return "analysis_directional_coordinator_no_trade"
        if coordinator_decision in _DIRECTIONAL_DECISIONS:
            if coordinator_direction == analysis_direction:
                return "analysis_directional_coordinator_same_direction"
            return "analysis_directional_coordinator_opposite_direction"
    if analysis_bucket == "neutral" and coordinator_decision in _DIRECTIONAL_DECISIONS:
        return "analysis_neutral_coordinator_directional"
    return f"analysis_{analysis_bucket}_coordinator_{coordinator_decision}"


def _ablate_agent(candidate: dict, agent: str) -> dict:
    """Returns a candidate-shaped dict identical to `candidate` except
    with `agent`'s opinion removed from opinions_used and added to
    missing_agents — modeling "this agent's input was genuinely
    unavailable for this decision" under the SAME live WEIGHTS/
    directional_weight_total as everything else, rather than zeroing
    the agent's weight in the WEIGHTS config (see the Tier 3.17 note
    in the module docstring for why that alternative is wrong: it
    also shrinks the availability gate's denominator for candidates
    where the agent was never even present). A candidate where `agent`
    wasn't in opinions_used to begin with comes back unchanged — a
    true no-op, not a false flip. Never mutates the input candidate."""
    decision = candidate["decision"]
    opinions_used = dict(decision.get("opinions_used") or {})
    missing_agents = list(decision.get("missing_agents") or [])
    opinions_used.pop(agent, None)
    if agent not in missing_agents:
        missing_agents.append(agent)
    return {
        **candidate,
        "decision": {
            **decision,
            "opinions_used": opinions_used,
            "missing_agents": missing_agents,
        },
    }


def _classify_ablation_change(original_conflict_flags: list[str], replay_result: dict) -> dict:
    """Tier 3.21: turns one replay_candidate() result into a single
    classified row — see the module docstring's Tier 3.21 paragraph
    for the full category definitions. original_conflict_flags is
    passed in separately (not read off replay_result["original"])
    because replay_candidate()'s "original" sub-dict is a deliberately
    trimmed subset (decision/direction/score/threshold/config_version
    only) — the real original conflict_flags live on the candidate's
    own persisted decision."""
    original = replay_result["original"]
    replayed = replay_result["replayed"]
    changed = replay_result["changed"]
    original_decision = original.get("decision")
    replayed_decision = replayed.get("decision")

    category = None
    if changed:
        if replayed_decision == "insufficient_data":
            category = "to_insufficient_data"
        elif (
            original_decision in _DIRECTIONAL_DECISIONS
            and replayed_decision in _DIRECTIONAL_DECISIONS
            and original.get("direction") != replayed.get("direction")
        ):
            category = "direction_flipped"
        else:
            category = "threshold_crossing"

    original_score = original.get("score") or 0.0
    replayed_score = replayed.get("score") or 0.0

    return {
        "changed": changed,
        "category": category,
        "original_decision": original_decision,
        "replayed_decision": replayed_decision,
        "score_delta": round(replayed_score - original_score, 2),
        "conflict_flags_changed": sorted(original_conflict_flags or []) != sorted(replayed.get("conflict_flags") or []),
    }


def _ablation_summary(classified: list[dict], agent_present_count: int) -> dict:
    changed = [c for c in classified if c["changed"]]
    unchanged = [c for c in classified if not c["changed"]]

    transitions: dict[str, int] = {}
    categories: dict[str, int] = {}
    for c in changed:
        key = f"{c['original_decision']} -> {c['replayed_decision']}"
        transitions[key] = transitions.get(key, 0) + 1
        categories[c["category"]] = categories.get(c["category"], 0) + 1

    def _avg_abs_score_delta(rows: list[dict]) -> float | None:
        if not rows:
            return None
        return round(sum(abs(r["score_delta"]) for r in rows) / len(rows), 2)

    return {
        "candidates_considered": len(classified),
        "agent_present_count": agent_present_count,
        "decision_changed": len(changed),
        "decision_unchanged": len(unchanged),
        "decision_changed_by_category": categories,
        "conflict_flags_changed_count": sum(1 for c in classified if c["conflict_flags_changed"]),
        "avg_abs_score_delta_when_changed": _avg_abs_score_delta(changed),
        "avg_abs_score_delta_when_unchanged": _avg_abs_score_delta(unchanged),
        "transitions": transitions,
    }


def compute_coordinator_divergence_report(candidates: list[dict]) -> dict:
    """Walks candidate history once and produces:

    - cross_tab: analysis_bucket -> coordinator_decision -> count (the
      complete picture, every candidate falls into exactly one cell).
    - named_categories: the reviewer's five specific categories, read
      directly off the cross_tab.
    - news_impact / macro_impact: how often each agent was present and
      directional, its average |contribution| to the weighted score
      when present, and how often its direction opposed Analysis's own
      direction (a same-direction contribution mostly just reinforces
      Analysis; an opposing one is where blending could actually change
      the outcome).
    - timing_blocked: how many candidates had Timing's veto (market
      closed) or dampen (low liquidity) flag actually fire.
    - ablation: for each of analysis/news/macro, replay every candidate
      with that agent's actual opinion removed from the frozen snapshot
      (Tier 3.17 — not a zeroed weight; see module docstring) and
      report how many final decisions actually change — a real causal
      measure of whether that agent's presence changes outcomes, not
      just how often it agrees with Analysis. agent_present_count says
      how many candidates actually had that agent's opinion to remove;
      decision_changed can never exceed it. Tier 3.21:
      decision_changed_by_category splits every change into
      to_insufficient_data (a quorum/availability effect — removing
      this agent alone dropped available evidence below the gate) vs
      direction_flipped (the call reversed bullish<->bearish) vs
      threshold_crossing (the score moved across one boundary without
      reversing) — see module docstring for why this separation
      matters. conflict_flags_changed_count and avg_abs_score_delta_
      when_changed/_when_unchanged give the raw magnitude of an
      agent's influence even on candidates whose decision category
      didn't change. transitions (the raw {original}->{replayed}
      decision pairs) is unchanged since Tier 3.16."""
    cross_tab: dict[str, dict[str, int]] = {}
    named_categories: dict[str, int] = {}

    news_present_directional = 0
    news_opposing_analysis = 0
    news_contribution_sum = 0.0
    macro_present_directional = 0
    macro_opposing_analysis = 0
    macro_contribution_sum = 0.0

    timing_blocked = 0

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        missing_agents = decision.get("missing_agents") or []
        stale_agents = decision.get("stale_agents") or []
        contributions = decision.get("contributions") or {}
        conflict_flags = decision.get("conflict_flags") or []
        coordinator_decision = decision.get("decision") or "unknown"
        coordinator_direction = _DECISION_DIRECTION.get(coordinator_decision)

        analysis_bucket = _analysis_bucket(opinions_used, missing_agents, stale_agents)
        analysis_direction = (opinions_used.get("analysis") or {}).get("direction")

        cross_tab.setdefault(analysis_bucket, {})
        cross_tab[analysis_bucket][coordinator_decision] = (
            cross_tab[analysis_bucket].get(coordinator_decision, 0) + 1
        )

        category = _named_category(analysis_bucket, analysis_direction, coordinator_decision, coordinator_direction)
        named_categories[category] = named_categories.get(category, 0) + 1

        for agent_name, present_ctr, opposing_ctr, contrib_sum_name in (
            ("news", "news_present_directional", "news_opposing_analysis", "news_contribution_sum"),
            ("macro", "macro_present_directional", "macro_opposing_analysis", "macro_contribution_sum"),
        ):
            agent_contribution = contributions.get(agent_name)
            if agent_contribution and agent_contribution.get("direction") in ("bullish", "bearish"):
                if agent_name == "news":
                    news_present_directional += 1
                    news_contribution_sum += abs(agent_contribution.get("contribution") or 0.0)
                    if analysis_direction in ("bullish", "bearish") and agent_contribution["direction"] != analysis_direction:
                        news_opposing_analysis += 1
                else:
                    macro_present_directional += 1
                    macro_contribution_sum += abs(agent_contribution.get("contribution") or 0.0)
                    if analysis_direction in ("bullish", "bearish") and agent_contribution["direction"] != analysis_direction:
                        macro_opposing_analysis += 1

        if any(f in conflict_flags for f in ("timing_market_closed", "timing_low_liquidity_dampened")):
            timing_blocked += 1

    def _agent_impact(present: int, opposing: int, contribution_sum: float) -> dict:
        return {
            "present_and_directional": present,
            "opposed_analysis_direction": opposing,
            "avg_abs_contribution_when_present": round(contribution_sum / present, 3) if present else None,
        }

    ablation = {}
    for agent in sorted(DIRECTIONAL_AGENTS):
        classified = [
            _classify_ablation_change(
                (candidate.get("decision") or {}).get("conflict_flags") or [],
                replay_candidate(_ablate_agent(candidate, agent), weights=WEIGHTS),
            )
            for candidate in candidates
        ]
        agent_present_count = sum(
            1 for c in candidates
            if agent in ((c.get("decision") or {}).get("opinions_used") or {})
        )
        ablation[f"{agent}_removed"] = _ablation_summary(classified, agent_present_count)

    return {
        "candidates_considered": len(candidates),
        "cross_tab": cross_tab,
        "named_categories": named_categories,
        "news_impact": _agent_impact(news_present_directional, news_opposing_analysis, news_contribution_sum),
        "macro_impact": _agent_impact(macro_present_directional, macro_opposing_analysis, macro_contribution_sum),
        "timing_blocked_count": timing_blocked,
        "ablation": ablation,
    }


# ---------------------------------------------------------------------------
# Threshold-crossing deep dive — Tier 3.26
# ---------------------------------------------------------------------------

def _threshold_crossing_side(original_decision: str, replayed_decision: str) -> str:
    """Which direction a threshold_crossing case moved in. Ablation can
    only ever REMOVE evidence, so a to_insufficient_data transition is
    already excluded by the caller (only threshold_crossing-classified
    cases reach this function) — the two patterns below are the only
    ones a live production run has ever produced, but "other" is kept
    as an honest fallback rather than assuming that's exhaustive."""
    if original_decision in _DIRECTIONAL_DECISIONS and replayed_decision == "no_trade":
        return "agent_enabled_trade"
    if original_decision == "no_trade" and replayed_decision in _DIRECTIONAL_DECISIONS:
        return "agent_prevented_trade"
    return "other"


def _real_outcome_bucket(candidate: dict, horizons: list[int]) -> dict:
    """For an agent_enabled_trade case: the ORIGINAL candidate actually
    happened, so its outcome comes from outcomes.compute_outcome_for_candidate()
    — real closed-trade P&L when a trade exists, the existing
    hypothetical horizon fallback otherwise. Never touches the ablated
    copy; a real trade lookup is keyed by the original candidate_id."""
    outcome = compute_outcome_for_candidate(candidate, horizons=horizons)
    if outcome is None:
        # Defensive only — an agent_enabled_trade case's original
        # decision is directional by construction (that's what makes it
        # "enabled"), so compute_outcome_for_candidate() should never
        # return None (its only None case is a non-directional decision).
        return {"kind": "no_outcome_evaluable"}
    if outcome["source"] == "actual_trade":
        result = {"kind": "real_trade", "status": outcome["status"], "outcome": outcome["outcome"]}
        if "pnl_usd" in outcome:
            result["pnl_usd"] = outcome["pnl_usd"]
        return result
    return {"kind": "hypothetical", "by_horizon": {h: o["outcome"] for h, o in (outcome.get("horizons") or {}).items()}}


def _prevented_outcome_bucket(replay_result: dict) -> dict:
    """For an agent_prevented_trade case: the replayed decision never
    became a real trade, so there's nothing to look up — reuses
    replay_candidate()'s own replayed_hypothetical_outcome (anchored to
    the ORIGINAL decision's timestamp, scored for the REPLAYED
    direction — exactly "if this had been taken instead, would price
    have agreed?"), relabeling correct/incorrect into prevented_win/
    prevented_loss (see module docstring for why)."""
    hypothetical = replay_result.get("replayed_hypothetical_outcome") or {}
    return {
        h: _PREVENTED_OUTCOME_LABEL.get(o["outcome"], o["outcome"])
        for h, o in hypothetical.items()
    }


def _summarize_deep_dive(cases: list[dict], horizons: list[int]) -> dict:
    by_side: dict[str, int] = {}
    by_agreement: dict[str, int] = {}
    urgent_count = 0

    enabled_real: dict[str, int] = {}
    enabled_hypothetical_by_horizon = {h: {} for h in horizons}
    prevented_by_horizon = {h: {} for h in horizons}

    for case in cases:
        by_side[case["side"]] = by_side.get(case["side"], 0) + 1
        by_agreement[case["agreement_with_analysis"]] = by_agreement.get(case["agreement_with_analysis"], 0) + 1
        if "urgent" in (case["agent_flags"] or []):
            urgent_count += 1

        outcome = case["outcome"]
        if case["side"] == "agent_enabled_trade":
            if outcome["kind"] == "real_trade":
                key = outcome["outcome"]
                enabled_real[key] = enabled_real.get(key, 0) + 1
            elif outcome["kind"] == "hypothetical":
                for h, o in outcome["by_horizon"].items():
                    bucket = enabled_hypothetical_by_horizon.setdefault(h, {})
                    bucket[o] = bucket.get(o, 0) + 1
        elif case["side"] == "agent_prevented_trade":
            for h, o in outcome["by_horizon"].items():
                bucket = prevented_by_horizon.setdefault(h, {})
                bucket[o] = bucket.get(o, 0) + 1

    return {
        "by_side": by_side,
        "by_agreement_with_analysis": by_agreement,
        "urgent_flag_count": urgent_count,
        "agent_enabled_trade_real_outcomes": enabled_real,
        "agent_enabled_trade_hypothetical_outcomes_by_horizon": enabled_hypothetical_by_horizon,
        "agent_prevented_trade_hypothetical_outcomes_by_horizon": prevented_by_horizon,
    }


def compute_threshold_crossing_deep_dive(candidates: list[dict], agent: str, horizons: list[int] = None) -> dict:
    """Tier 3.26 — see module docstring for full rationale. Re-walks
    candidate history, ablates `agent` on every candidate that actually
    had an opinion from it (a candidate where the agent was never
    present is a true ablation no-op — skipped, not just filtered out
    after the fact), keeps only the threshold_crossing-classified
    subset (same classifier compute_coordinator_divergence_report's
    ablation pass already uses), and reports each surviving case's
    side/outcome/agreement-with-Analysis/flags plus a distinct-opinion-
    timestamp count and an aggregate summary.

    `agent` must be one of coordinator.DIRECTIONAL_AGENTS
    (analysis/news/macro) — built generically even though the fifth
    review specifically asked about News/Macro, since Analysis's own
    threshold_crossing subset (typically tiny; most of its ablation
    impact is to_insufficient_data per Tier 3.21) is a well-defined
    question under the exact same machinery, not a special case.

    Entirely offline for the ablation/replay step; compute_outcome_
    for_candidate() (agent_enabled_trade cases only) does read real
    trade rows via storage, same as every other outcome-aware endpoint
    in this project — still no LLM calls, no new candidates or trades,
    no mutation of anything stored."""
    if agent not in DIRECTIONAL_AGENTS:
        raise ValueError(f"agent must be one of {sorted(DIRECTIONAL_AGENTS)}, got {agent!r}")
    horizons = horizons or HORIZON_MINUTES_DEFAULT

    cases = []
    distinct_opinion_timestamps: set[str] = set()

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        agent_opinion = opinions_used.get(agent)
        if not agent_opinion:
            continue  # agent wasn't present -- ablation would be a no-op

        replay_result = replay_candidate(
            _ablate_agent(candidate, agent),
            weights=WEIGHTS,
            include_outcome=True,
            outcome_horizons=horizons,
        )
        classified = _classify_ablation_change(decision.get("conflict_flags") or [], replay_result)
        if classified["category"] != "threshold_crossing":
            continue

        side = _threshold_crossing_side(classified["original_decision"], classified["replayed_decision"])

        analysis_opinion = opinions_used.get("analysis")
        analysis_direction = analysis_opinion.get("direction") if analysis_opinion else None
        agent_direction = agent_opinion.get("direction")
        if analysis_direction in ("bullish", "bearish") and agent_direction in ("bullish", "bearish"):
            agreement = "agree" if agent_direction == analysis_direction else "oppose"
        else:
            agreement = "analysis_not_directional_or_absent"

        opinion_timestamp = agent_opinion.get("timestamp")
        if opinion_timestamp:
            distinct_opinion_timestamps.add(opinion_timestamp)

        if side == "agent_enabled_trade":
            outcome = _real_outcome_bucket(candidate, horizons)
        elif side == "agent_prevented_trade":
            outcome = {"kind": "prevented_hypothetical", "by_horizon": _prevented_outcome_bucket(replay_result)}
        else:
            outcome = {"kind": "unclassified_side"}

        cases.append({
            "candidate_id": candidate.get("candidate_id"),
            "side": side,
            "score_delta": classified["score_delta"],
            "agreement_with_analysis": agreement,
            "agent_flags": agent_opinion.get("flags") or [],
            "agent_opinion_timestamp": opinion_timestamp,
            "outcome": outcome,
        })

    return {
        "agent": agent,
        "cases_considered": len(cases),
        "distinct_opinion_timestamps": len(distinct_opinion_timestamps),
        "cases": cases,
        "summary": _summarize_deep_dive(cases, horizons),
    }
