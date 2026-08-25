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

from app.backtest import _RISK_FILTER_MACRO_VETO_FLAGS, _RISK_FILTER_NEWS_VETO_FLAGS
from app.coordinator import DIRECTIONAL_AGENTS, WEIGHTS, _TIMING_DAMPEN_FLAG, _TIMING_VETO_FLAG
from app.economic_calendar import events_overlapping_range, is_within_blackout_window
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

# ---------------------------------------------------------------------------
# News urgent-vs-directional decomposition — Tier 3.27
#
# Sixth external review, responding to real Tier 3.26 production numbers
# (News: 107 threshold_crossing cases, 86 of them — ~80% — carrying
# News's "urgent" flag): the review's central correctness point is that
# Tier 3.26's threshold_crossing measurement CONFLATES two mechanically
# separate effects whenever "urgent" is set. app/coordinator.py's
# _score_opinions() does two different things with a present News
# opinion: (1) folds its direction*confidence*weight into the weighted
# score sum (the genuine "directional influence" the reviewer wants
# measured), and (2) independently multiplies the WHOLE score by 0.5
# whenever "urgent" is in News's flags, regardless of direction or
# agreement with Analysis (a scoring-mechanic side effect, not a
# directional read). _ablate_agent() (Tier 3.17/3.26) removes News's
# opinion entirely, which removes BOTH effects at once — so a
# threshold_crossing case caused ENTIRELY by the 0.5x dampen (no real
# directional disagreement at all) looks identical, from that one
# measurement, to one caused entirely by News's own bullish/bearish read
# outweighing the others. Reported as "News's directional influence,"
# that overstates what was actually shown, exactly as the review argues.
#
# The fix doesn't need to touch coordinator.py's live scoring at all —
# it only needs two NEW partial-modification variants of a candidate's
# frozen opinions_used, replayed the same offline way _ablate_agent()
# already is:
#   - _news_direction_only_removed(): keeps News's opinion present (so
#     its flags, including "urgent", still apply the dampen) but forces
#     direction to "neutral" — _DIRECTION_VALUE["neutral"] is 0, so its
#     weighted contribution to the score becomes exactly zero while
#     everything else about its presence (quorum availability, the
#     urgent dampen) is untouched. Isolates the dampen's effect ALONE.
#   - _news_urgent_flag_only_removed(): keeps News's real direction and
#     confidence (so its weighted contribution is unchanged) but strips
#     "urgent" out of its flags list, so the 0.5x dampen no longer fires.
#     Isolates the directional contribution's effect ALONE.
# Comparing which of these two variants alone reproduces the original
# full-removal's category-changing effect (compute_news_urgent_
# decomposition()'s "attribution" field: "direction_alone" /
# "urgent_dampen_alone" / "both_independently_sufficient" /
# "only_combination_sufficient" for a genuine interaction) answers the
# review's exact three-way ask ("removal of direction alone" / "removal
# of urgent alone" / "removal of both, the existing measurement") without
# any change to live scoring — both variants are throwaway per-candidate
# copies for one offline replay each, same guarantee _ablate_agent() and
# every other function in this module already give.
#
# compute_news_urgent_prevalence() answers the review's second, equally
# important correction: 86/107 is NOT News's overall "urgent rate" — it's
# the rate WITHIN a sample that's already pre-selected by threshold_
# crossing, and urgent itself helps pull a candidate INTO that sample (by
# depressing its score toward the boundary). This instead reports urgent's
# unconditional share across every News-present candidate (how often a
# webhook-triggered decision saw an urgent News opinion) and, separately,
# across every DISTINCT News opinion (the honest denominator — one urgent
# LLM call can be reused across many candidates while fresh, per Tier
# 3.6/3.26's reuse concern), so the 86/107 figure can be read against the
# real base rate instead of the pre-filtered one.
#
# Scoped to News only — Macro's flag vocabulary (risk_off/conflicting_
# signals/stale_data, confirmed via app/macro_agent.py) has no "urgent"
# concept at all, so this decomposition has nothing to isolate there.
# Read-only, offline, no LLM calls, no candidate mutated, coordinator.py
# untouched — same guarantee as the rest of this module.
# ---------------------------------------------------------------------------

def _news_direction_only_removed(candidate: dict) -> dict:
    decision = candidate["decision"]
    opinions_used = dict(decision.get("opinions_used") or {})
    news_opinion = opinions_used.get("news")
    if news_opinion is None:
        return candidate  # no-op: caller only invokes this when news is present
    opinions_used["news"] = {**news_opinion, "direction": "neutral"}
    return {**candidate, "decision": {**decision, "opinions_used": opinions_used}}


def _news_urgent_flag_only_removed(candidate: dict) -> dict:
    decision = candidate["decision"]
    opinions_used = dict(decision.get("opinions_used") or {})
    news_opinion = opinions_used.get("news")
    if news_opinion is None:
        return candidate
    stripped_flags = [f for f in (news_opinion.get("flags") or []) if f != "urgent"]
    opinions_used["news"] = {**news_opinion, "flags": stripped_flags}
    return {**candidate, "decision": {**decision, "opinions_used": opinions_used}}


def _attribute_urgent_vs_direction(direction_only_changed: bool, urgent_only_changed: bool) -> str:
    if direction_only_changed and not urgent_only_changed:
        return "direction_alone"
    if urgent_only_changed and not direction_only_changed:
        return "urgent_dampen_alone"
    if direction_only_changed and urgent_only_changed:
        return "both_independently_sufficient"
    return "only_combination_sufficient"


def compute_news_urgent_prevalence(candidates: list[dict]) -> dict:
    """Reviewer correction (sixth external review) — see module docstring
    section above. Returns urgent's unconditional share at both the
    candidate level and the distinct-News-opinion level, across every
    News-present candidate in the input (not just a threshold_crossing
    subset)."""
    candidate_total = 0
    candidate_urgent = 0
    opinion_is_urgent: dict[str, bool] = {}

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        news_opinion = opinions_used.get("news")
        if not news_opinion:
            continue
        candidate_total += 1
        is_urgent = "urgent" in (news_opinion.get("flags") or [])
        if is_urgent:
            candidate_urgent += 1
        timestamp = news_opinion.get("timestamp")
        if timestamp:
            opinion_is_urgent[timestamp] = opinion_is_urgent.get(timestamp, False) or is_urgent

    distinct_total = len(opinion_is_urgent)
    distinct_urgent = sum(1 for v in opinion_is_urgent.values() if v)

    return {
        "candidate_level": {
            "news_present_candidates": candidate_total,
            "urgent_candidates": candidate_urgent,
            "urgent_rate": round(candidate_urgent / candidate_total, 3) if candidate_total else None,
        },
        "distinct_opinion_level": {
            "distinct_news_opinions": distinct_total,
            "distinct_urgent_opinions": distinct_urgent,
            "urgent_rate": round(distinct_urgent / distinct_total, 3) if distinct_total else None,
        },
    }


def compute_news_urgent_decomposition(candidates: list[dict], horizons: list[int] = None) -> dict:
    """Tier 3.27 — see module docstring section above for the full
    rationale. Walks the same threshold_crossing subset Tier 3.26's
    compute_threshold_crossing_deep_dive(agent="news") would (full
    _ablate_agent removal, classified via _classify_ablation_change),
    filtered further to cases where News's opinion actually carried the
    "urgent" flag (decomposition is meaningless otherwise — without
    urgent, direction-only-removed IS full removal). For each, additionally
    replays the two partial-modification variants and reports which one
    alone reproduces the original full-removal's changed classification —
    the causal attribution the review asked for."""
    horizons = horizons or HORIZON_MINUTES_DEFAULT
    cases = []
    distinct_opinion_timestamps: set[str] = set()

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        news_opinion = opinions_used.get("news")
        if not news_opinion:
            continue
        if "urgent" not in (news_opinion.get("flags") or []):
            continue

        conflict_flags = decision.get("conflict_flags") or []

        full_removed = replay_candidate(_ablate_agent(candidate, "news"), weights=WEIGHTS)
        full_classified = _classify_ablation_change(conflict_flags, full_removed)
        if full_classified["category"] != "threshold_crossing":
            continue

        direction_only = replay_candidate(_news_direction_only_removed(candidate), weights=WEIGHTS)
        direction_only_classified = _classify_ablation_change(conflict_flags, direction_only)

        urgent_only = replay_candidate(_news_urgent_flag_only_removed(candidate), weights=WEIGHTS)
        urgent_only_classified = _classify_ablation_change(conflict_flags, urgent_only)

        attribution = _attribute_urgent_vs_direction(
            direction_only_classified["changed"], urgent_only_classified["changed"],
        )

        opinion_timestamp = news_opinion.get("timestamp")
        if opinion_timestamp:
            distinct_opinion_timestamps.add(opinion_timestamp)

        cases.append({
            "candidate_id": candidate.get("candidate_id"),
            "attribution": attribution,
            "opinion_timestamp": opinion_timestamp,
            "trading_date": (candidate.get("bar") or {}).get("trading_date"),
            "full_removal": {
                "changed": full_classified["changed"],
                "category": full_classified["category"],
                "score_delta": full_classified["score_delta"],
            },
            "direction_only_removed": {
                "changed": direction_only_classified["changed"],
                "category": direction_only_classified["category"],
                "score_delta": direction_only_classified["score_delta"],
            },
            "urgent_only_removed": {
                "changed": urgent_only_classified["changed"],
                "category": urgent_only_classified["category"],
                "score_delta": urgent_only_classified["score_delta"],
            },
        })

    attribution_counts: dict[str, int] = {}
    for case in cases:
        attribution_counts[case["attribution"]] = attribution_counts.get(case["attribution"], 0) + 1

    return {
        "cases_considered": len(cases),
        "distinct_opinion_timestamps": len(distinct_opinion_timestamps),
        "cases": cases,
        "summary": {"by_attribution": attribution_counts},
        "opinion_level_day_blocked": _opinion_level_day_blocked_summary(
            cases, category_field="attribution", opinion_field="opinion_timestamp", day_field="trading_date",
        ),
    }


def compute_news_urgent_analysis(candidates: list[dict], horizons: list[int] = None) -> dict:
    """Tier 3.27 combined entry point: prevalence (the honest, un-pre-
    filtered urgent base rate) alongside the causal decomposition (which
    of direction/urgent-dampen/both actually drove each urgent-tagged
    threshold_crossing case) — see module docstring section above."""
    return {
        "prevalence": compute_news_urgent_prevalence(candidates),
        "decomposition": compute_news_urgent_decomposition(candidates, horizons=horizons),
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
            "trading_date": (candidate.get("bar") or {}).get("trading_date"),
            "outcome": outcome,
        })

    return {
        "agent": agent,
        "cases_considered": len(cases),
        "distinct_opinion_timestamps": len(distinct_opinion_timestamps),
        "cases": cases,
        "summary": _summarize_deep_dive(cases, horizons),
        "opinion_level_day_blocked": _opinion_level_day_blocked_summary(
            cases, category_field="side", opinion_field="agent_opinion_timestamp", day_field="trading_date",
        ),
    }


# ---------------------------------------------------------------------------
# News urgent vs. deterministic economic-calendar blackout — Tier 3.28
# ---------------------------------------------------------------------------
#
# Sixth external review, ranked backlog item #2 (relayed verbatim):
# "قارنه بحظر بسيط مبني على تقويم اقتصادي موثوق: امتنع قبل/بعد
# CPI/FOMC/NFP. إذا كان LLM لا يتفوق على blackout ثابت، فلا يوجد سبب
# لدفع تكلفته أو الاعتماد على تصنيفه الحر." (Compare News's "urgent"
# flag against a simple blackout built on a trustworthy economic
# calendar: abstain before/after CPI/FOMC/NFP. If the LLM doesn't
# outperform a fixed blackout, there's no reason to pay its cost or
# rely on its free-text classification.)
#
# Tier 3.27 established that "urgent" independently dampens the
# blended score regardless of WHY News called something urgent — News's
# own reasoning has cited real CPI/NFP prints, but just as often cites
# things a fixed CPI/FOMC/NFP calendar has no concept of at all
# (earnings releases, Fed-official speeches, geopolitical headlines —
# see the Aug 24 production example in app/economic_calendar.py's
# docstring: News flagged "urgent" partly for a Treasury Secretary
# press conference and an upcoming Jackson Hole speech, neither of
# which is CPI/FOMC/NFP). Whether that broader judgment is adding real
# value or just adding noise/cost relative to a free, deterministic
# calendar check is exactly the reviewer's question — this function
# answers it by tagging every News-present candidate with BOTH signals,
# independently computed, and cross-tabulating them.
#
# app.economic_calendar.is_within_blackout_window() has zero access to
# News's opinion, reasoning, or flags — it only ever looks at the
# candidate's own bar timestamp against the hardcoded, source-cited
# 2026 CPI/NFP/FOMC registry. Read-only, offline, no LLM calls, no
# mutation of any stored candidate, COORDINATOR_THRESHOLD/WEIGHTS
# untouched — same guarantee as the rest of this module.


def compute_news_urgent_vs_calendar_blackout(
    candidates: list[dict], window_hours: float = 2.0, horizons: list[int] = None,
) -> dict:
    """For every candidate with a News opinion present, tags it with two
    INDEPENDENTLY-computed booleans — news_urgent (News's own
    self-reported "urgent" flag) and calendar_blackout (whether the
    candidate's bar timestamp falls within `window_hours` of the
    nearest real CPI/NFP/FOMC release in app.economic_calendar's
    registry) — and buckets every case into one of four quadrants:
    both_flagged, news_urgent_only, calendar_blackout_only,
    neither_flagged. `agreement_rate` is the share of cases where the
    two signals agreed (both_flagged + neither_flagged).

    For candidates that actually reached a directional decision
    (enter_long/enter_short), also attaches an outcome — real
    closed-trade result when one exists (app.outcomes.
    compute_outcome_for_candidate(), same preference as every other
    outcome-aware endpoint in this project), the existing per-horizon
    hypothetical estimate otherwise — bucketed per quadrant in
    outcomes_by_quadrant, so a reader can see whether trading through a
    calendar-blackout window (regardless of what News said) actually
    correlated with worse outcomes than News's own "urgent" judgment
    did.

    calendar_coverage reports, honestly, how many real events from the
    registry could even have produced an in_blackout=True result
    somewhere in this specific data pull (via app.economic_calendar.
    events_overlapping_range on the observed candidates' own min/max
    bar timestamps) — see app/economic_calendar.py's module docstring:
    at this tier's build time the live 9-trading-day window contained
    exactly one such event (2026-08-12 CPI), so any single run of this
    endpoint against current production data should be read as a very
    thin sample, not a confirmatory result. That coverage count is
    exactly why this field exists instead of only reporting a
    cross_tab that could otherwise be mistaken for a well-powered
    comparison.

    Entirely offline (no LLM calls, no candidate mutated,
    COORDINATOR_THRESHOLD/WEIGHTS untouched); the outcome lookup reads
    real trade rows the same way every other outcome-aware endpoint in
    this project already does."""
    horizons = horizons or HORIZON_MINUTES_DEFAULT
    cases = []
    bar_timestamps: list[str] = []

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        news_opinion = opinions_used.get("news")
        if not news_opinion:
            continue
        bar_timestamp = (candidate.get("bar") or {}).get("timestamp")
        if not bar_timestamp:
            continue
        bar_timestamps.append(bar_timestamp)

        blackout = is_within_blackout_window(bar_timestamp, window_hours=window_hours)
        news_urgent = "urgent" in (news_opinion.get("flags") or [])
        calendar_blackout = blackout["in_blackout"]
        if news_urgent and calendar_blackout:
            quadrant = "both_flagged"
        elif news_urgent and not calendar_blackout:
            quadrant = "news_urgent_only"
        elif not news_urgent and calendar_blackout:
            quadrant = "calendar_blackout_only"
        else:
            quadrant = "neither_flagged"

        outcome = None
        if decision.get("decision") in _DIRECTIONAL_DECISIONS:
            outcome_result = compute_outcome_for_candidate(candidate, horizons=horizons)
            if outcome_result is not None:
                if outcome_result["source"] == "actual_trade":
                    outcome = {
                        "kind": "real_trade",
                        "status": outcome_result["status"],
                        "outcome": outcome_result["outcome"],
                    }
                    if "pnl_usd" in outcome_result:
                        outcome["pnl_usd"] = outcome_result["pnl_usd"]
                else:
                    outcome = {
                        "kind": "hypothetical",
                        "by_horizon": {h: o["outcome"] for h, o in (outcome_result.get("horizons") or {}).items()},
                    }

        cases.append({
            "candidate_id": candidate.get("candidate_id"),
            "bar_timestamp": bar_timestamp,
            "news_opinion_timestamp": news_opinion.get("timestamp"),
            "trading_date": (candidate.get("bar") or {}).get("trading_date"),
            "decision": decision.get("decision"),
            "quadrant": quadrant,
            "news_urgent": news_urgent,
            "calendar_blackout": calendar_blackout,
            "nearest_event": blackout["nearest_event"],
            "distance_hours": blackout["distance_hours"],
            "outcome": outcome,
        })

    cross_tab: dict[str, int] = {}
    outcomes_by_quadrant: dict[str, dict] = {}
    for case in cases:
        quadrant = case["quadrant"]
        cross_tab[quadrant] = cross_tab.get(quadrant, 0) + 1
        outcome = case["outcome"]
        if outcome is None:
            continue
        bucket = outcomes_by_quadrant.setdefault(
            quadrant, {"real_trade": {}, "hypothetical_by_horizon": {h: {} for h in horizons}},
        )
        if outcome["kind"] == "real_trade":
            key = outcome["outcome"]
            bucket["real_trade"][key] = bucket["real_trade"].get(key, 0) + 1
        else:
            for h, o in outcome["by_horizon"].items():
                horizon_bucket = bucket["hypothetical_by_horizon"].setdefault(h, {})
                horizon_bucket[o] = horizon_bucket.get(o, 0) + 1

    agreeing = cross_tab.get("both_flagged", 0) + cross_tab.get("neither_flagged", 0)
    agreement_rate = round(agreeing / len(cases), 3) if cases else None

    if bar_timestamps:
        range_start = min(bar_timestamps)
        range_end = max(bar_timestamps)
        coverage_events = events_overlapping_range(range_start, range_end, window_hours=window_hours)
    else:
        range_start = None
        range_end = None
        coverage_events = []

    return {
        "window_hours": window_hours,
        "news_present_candidates": len(cases),
        "data_range": {"start": range_start, "end": range_end},
        "calendar_coverage": {
            "events_overlapping_data_range": coverage_events,
            "event_count": len(coverage_events),
        },
        "cross_tab": cross_tab,
        "agreement_rate": agreement_rate,
        "outcomes_by_quadrant": outcomes_by_quadrant,
        "cases": cases,
        "opinion_level_day_blocked": _opinion_level_day_blocked_summary(
            cases, category_field="quadrant", opinion_field="news_opinion_timestamp", day_field="trading_date",
        ),
    }


# ---------------------------------------------------------------------------
# Risk-filter veto attribution — Tier 3.31
# ---------------------------------------------------------------------------
#
# Seventh external review, reacting to Tier 3.30's analysis_risk_filtered
# backtest source: that source bundles FOUR changes into one policy at
# once — removing News from the directional vote, removing Macro from the
# directional vote, removing the MIN_AVAILABLE_WEIGHT quorum gate, and
# removing Timing's session/liquidity gating entirely (app/backtest.py's
# _direction_for_source("analysis_risk_filtered", ...) never even reads
# Timing) — so a trade-count difference against the live Coordinator can't
# yet be attributed specifically to "News/Macro became risk filters." This
# diagnostic separates the four out with real numbers, reusing the exact
# gating logic already computed and FROZEN on every stored candidate
# (Tier 2.1's opinions_used/conflict_flags snapshot) rather than a new
# replay — the real historical Coordinator decision already encodes
# whether Timing vetoed/dampened it (app.coordinator._score_opinions'
# "timing_market_closed"/"timing_low_liquidity_dampened" conflict_flags,
# set deterministically from Timing's own flags — see app/timing_agent.py)
# and whether the quorum gate fired (decision == "insufficient_data" is
# only possible in this function's subpopulation, where Analysis is
# already confirmed present and directional, when News AND Macro are BOTH
# missing/stale: Analysis alone is 0.40/0.80 = 50% of DIRECTIONAL_AGENTS'
# combined weight, below MIN_AVAILABLE_WEIGHT's live 60% floor either way
# — analysis_required is trivially satisfied by this function's own
# precondition, so it can't be the cause of an insufficient_data case
# here).
#
# For every candidate with a directional (bullish/bearish) Analysis
# opinion, each case is attributed to exactly one bucket, in the same
# priority order app.backtest._direction_for_source checks them:
#
#   analysis_not_directional   -- Analysis missing/neutral (excluded from
#                                  every bucket below; both policies skip
#                                  these identically, not attributable to
#                                  any veto)
#   news_urgent_veto           -- News present with the "urgent" flag
#                                  (_RISK_FILTER_NEWS_VETO_FLAGS) --
#                                  analysis_risk_filtered skips this
#   macro_risk_off_veto        -- Macro present with the "risk_off" flag
#                                  (_RISK_FILTER_MACRO_VETO_FLAGS), News
#                                  didn't already veto -- analysis_risk_
#                                  filtered skips this
#   coordinator_agrees         -- neither veto fires (analysis_risk_
#                                  filtered WOULD trade this candidate's
#                                  Analysis direction) AND the real
#                                  Coordinator also traded the same
#                                  direction -- no blocking difference
#   coordinator_opposite_direction -- neither veto fires, but the real
#                                  Coordinator traded the OPPOSITE
#                                  direction (structurally rare/unproven
#                                  absent under live weights per Tier
#                                  3.21's direction_flipped proof, but not
#                                  assumed impossible here)
#   coordinator_quorum_block   -- neither veto fires, Coordinator's real
#                                  decision was insufficient_data (News
#                                  AND Macro both missing/stale)
#   timing_market_closed_block -- neither veto fires, quorum was fine,
#                                  but Timing's own market_closed flag
#                                  forced the real Coordinator's score to
#                                  zero
#   timing_low_liquidity_block -- neither veto fires, quorum was fine, no
#                                  market_closed veto, but Timing's own
#                                  low_liquidity flag halved the real
#                                  Coordinator's score enough to keep it
#                                  under threshold
#   coordinator_score_below_threshold_other -- neither veto fires, quorum
#                                  was fine, no Timing flag applied at
#                                  all -- the real Coordinator's blended
#                                  score simply didn't cross +-threshold.
#                                  See the Tier 3.32 note below: this is a
#                                  genuine residual/catch-all, not proof
#                                  of "News/Macro opposition" by itself.
#
# This is NOT wired into _opinion_level_day_blocked_summary using News or
# Macro's own opinion identity, unlike the three Tier 3.26-3.28
# diagnostics above -- this diagnostic's subject is the CANDIDATE-level
# comparison between two whole policies, not one reused agent's opinion,
# so Analysis's own opinion_timestamp (the one field every case here is
# guaranteed to have, by construction) is the correct identity to weight
# by instead.
#
# Entirely offline: reads already-stored candidate.decision fields only,
# no replay, no LLM calls, no candidate mutated. COORDINATOR_THRESHOLD/
# WEIGHTS/analysis_risk_filtered's own veto scope are all untouched.
#
# Tier 3.32 correction (eighth external review): two real problems with
# the original Tier 3.31 shipment.
#
# (1) The scoped Timing finding was overstated. Tier 3.31's comment
# claimed Timing veto/dampen flags can NEVER co-occur with a directional
# Analysis opinion -- true only for the AUTO-GENERATED webhook candidate
# path (should_run_analysis() gates real-time Analysis runs to inside a
# kill zone), not a general system-wide impossibility: POST
# /agents/analysis/run?ignore_timing_gate=true is a real manual-testing
# path that can produce a candidate with both a directional Analysis
# opinion AND an outside-kill-zone Timing state. The finding stands for
# every candidate this diagnostic actually sees in production (all
# auto-generated), but the module comment above and this one are now
# explicit about that scope rather than implying an unconditional proof.
#
# (2) `news_macro_opposition_block` (renamed `coordinator_score_below_
# threshold_other` above) claimed more than the code proved. All that
# bucket's precondition actually establishes is: no veto fired, quorum
# passed, no Timing flag applied, decision == "no_trade" -- i.e. the
# blended |score| simply didn't cross the threshold. That could be
# genuine directional opposition, but could equally be every present
# agent AGREEING with Analysis at collectively low confidence, or News/
# Macro present but NEUTRAL (diluting the renormalized average toward
# zero without opposing anything). `score_below_threshold_reason` below
# splits this residual bucket, using only each present agent's own
# stored direction (no new replay): "directional_opposition" (News or
# Macro present with a direction that OPPOSES Analysis's), else
# "neutral_dilution" (News or Macro present with direction "neutral"),
# else "agreement_low_confidence" (every present other agent agrees with
# Analysis's direction -- score fell short purely on confidence/
# weighting, not disagreement). These three are exhaustive over every
# present other agent's direction (bullish/bearish either opposes or
# agrees; neutral is its own case) given at least one of News/Macro is
# guaranteed present here (quorum already passed) -- "other" is kept as
# a defensive fallback, not because it's expected to ever fire.
#
# Also added: `flag_prevalence` at the top level reports each veto
# flag's TRUE independent count (`news_urgent_total`, `macro_risk_off_
# total`) and `both_flags_overlap` -- the priority order above (News
# checked before Macro, matching app.backtest._direction_for_source's
# own check order) means `summary.macro_risk_off_veto` UNDERSTATES
# Macro's real prevalence whenever both flags co-occur on the same
# candidate (that case is bucketed under news_urgent_veto instead).
# `flag_prevalence` makes the true prevalence of each flag, and their
# overlap, visible independent of which bucket a co-occurring case
# landed in.


def _score_below_threshold_reason(analysis_direction: str, news_opinion: dict | None, macro_opinion: dict | None) -> str:
    """Tier 3.32: splits the coordinator_score_below_threshold_other
    bucket using only each present other agent's own stored direction —
    see the Tier 3.32 module comment above for why these three are
    exhaustive given at least one of news_opinion/macro_opinion is
    guaranteed present (quorum already passed by the time this is
    called)."""
    others = [op for op in (news_opinion, macro_opinion) if op is not None]
    if any(op.get("direction") in ("bullish", "bearish") and op.get("direction") != analysis_direction for op in others):
        return "directional_opposition"
    if any(op.get("direction") == "neutral" for op in others):
        return "neutral_dilution"
    if others and all(op.get("direction") == analysis_direction for op in others):
        return "agreement_low_confidence"
    return "other"  # defensive fallback; should be unreachable per the module comment's proof


def compute_risk_filter_veto_attribution(candidates: list[dict]) -> dict:
    """See the Tier 3.31/3.32 module comments above for the full bucket
    definitions and reasoning. Returns candidates_considered, the
    analysis_not_directional exclusion count, and — for the remaining
    analysis-directional subpopulation — a `summary` dict of bucket ->
    count, `flag_prevalence` (true independent News urgent / Macro
    risk_off prevalence plus their overlap, since the bucket priority
    order alone understates Macro's), the full per-candidate `cases`
    list, and `opinion_level_day_blocked` (Analysis-opinion identity,
    day-blocked)."""
    cases = []
    excluded_not_directional = 0
    news_urgent_total = 0
    macro_risk_off_total = 0
    both_flags_overlap = 0

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        analysis_opinion = opinions_used.get("analysis")
        analysis_direction = (analysis_opinion or {}).get("direction")
        if not analysis_opinion or analysis_direction not in ("bullish", "bearish"):
            excluded_not_directional += 1
            continue

        news_opinion = opinions_used.get("news")
        macro_opinion = opinions_used.get("macro")
        news_urgent = bool(
            news_opinion and _RISK_FILTER_NEWS_VETO_FLAGS & set(news_opinion.get("flags") or [])
        )
        macro_risk_off = bool(
            macro_opinion and _RISK_FILTER_MACRO_VETO_FLAGS & set(macro_opinion.get("flags") or [])
        )
        if news_urgent:
            news_urgent_total += 1
        if macro_risk_off:
            macro_risk_off_total += 1
        if news_urgent and macro_risk_off:
            both_flags_overlap += 1

        coordinator_decision = decision.get("decision")
        coordinator_direction = decision.get("direction")
        conflict_flags = decision.get("conflict_flags") or []

        score_below_threshold_reason = None
        if news_urgent:
            attribution = "news_urgent_veto"
        elif macro_risk_off:
            attribution = "macro_risk_off_veto"
        elif coordinator_decision in _DIRECTIONAL_DECISIONS:
            attribution = (
                "coordinator_agrees"
                if coordinator_direction == analysis_direction
                else "coordinator_opposite_direction"
            )
        elif coordinator_decision == "insufficient_data":
            attribution = "coordinator_quorum_block"
        elif _TIMING_VETO_FLAG == "market_closed" and "timing_market_closed" in conflict_flags:
            attribution = "timing_market_closed_block"
        elif _TIMING_DAMPEN_FLAG == "low_liquidity" and "timing_low_liquidity_dampened" in conflict_flags:
            attribution = "timing_low_liquidity_block"
        else:
            attribution = "coordinator_score_below_threshold_other"
            score_below_threshold_reason = _score_below_threshold_reason(
                analysis_direction, news_opinion, macro_opinion,
            )

        cases.append({
            "candidate_id": candidate.get("candidate_id"),
            "bar_timestamp": (candidate.get("bar") or {}).get("timestamp"),
            "trading_date": (candidate.get("bar") or {}).get("trading_date"),
            "analysis_opinion_timestamp": analysis_opinion.get("timestamp"),
            "analysis_direction": analysis_direction,
            "coordinator_decision": coordinator_decision,
            "coordinator_direction": coordinator_direction,
            "news_urgent": news_urgent,
            "macro_risk_off": macro_risk_off,
            "attribution": attribution,
            "score_below_threshold_reason": score_below_threshold_reason,
        })

    summary: dict[str, int] = {}
    score_below_threshold_breakdown: dict[str, int] = {}
    for case in cases:
        summary[case["attribution"]] = summary.get(case["attribution"], 0) + 1
        if case["score_below_threshold_reason"] is not None:
            reason = case["score_below_threshold_reason"]
            score_below_threshold_breakdown[reason] = score_below_threshold_breakdown.get(reason, 0) + 1

    return {
        "candidates_considered": len(candidates),
        "analysis_not_directional_excluded": excluded_not_directional,
        "analysis_directional_candidates": len(cases),
        "flag_prevalence": {
            "news_urgent_total": news_urgent_total,
            "macro_risk_off_total": macro_risk_off_total,
            "both_flags_overlap": both_flags_overlap,
        },
        "score_below_threshold_breakdown": score_below_threshold_breakdown,
        "summary": summary,
        "cases": cases,
        "opinion_level_day_blocked": _opinion_level_day_blocked_summary(
            cases, category_field="attribution", opinion_field="analysis_opinion_timestamp", day_field="trading_date",
        ),
    }


# ---------------------------------------------------------------------------
# Decision-level veto transitions — Tier 3.34 (ninth external review)
# ---------------------------------------------------------------------------
#
# The reviewer's core correction to Package #9: Tier 3.33's
# "coordinator_veto_filtered" trade-count delta against plain
# "coordinator" (e.g. 14 -> 5 in one production pull) is NOT a clean
# decision-level count of "how many real trades did the veto kill." Two
# separate problems, both real:
#
#   1. app.backtest.run_barrier_backtest applies `non_overlapping`
#      scheduling INDEPENDENTLY per source (each source walks its own
#      resolved-direction candidates and skips one whose anchor falls
#      before its own previous simulated trade resolved, mirroring the
#      live MAX_OPEN_POSITIONS=1 constraint). Removing an early veto'd
#      trade can free schedule capacity for a LATER candidate the
#      original (un-vetoed) schedule would have skipped as overlapping
#      — so a raw trades_taken delta conflates the veto's direct,
#      per-decision effect with this portfolio-level, path-dependent
#      rescheduling effect. Tier 3.33's backtest-lite numbers remain
#      valid for what they actually measure — realized P&L under each
#      policy's own trade schedule — just not for "how many decisions
#      did the veto attribute to itself" claims.
#   2. Tier 3.31's own "10%" figure (`news_macro_opposition_block`, now
#      `coordinator_score_below_threshold_other`) was separately
#      mislabeled in Package #9 as "the veto's share of the gap" — it
#      never was. It's the share of analysis_risk_filtered's EXTRA
#      trades (beyond what the real Coordinator did) attributable to
#      something other than quorum-bypass; Tier 3.31's bucket priority
#      order sends every news_urgent_veto/macro_risk_off_veto candidate
#      straight to its own bucket BEFORE checking what the real
#      Coordinator decision actually was, so those 239+64 veto-bucketed
#      candidates were never cross-tabulated against whether Coordinator
#      traded them or not. That cross-tabulation is exactly this
#      diagnostic's job.
#
# compute_veto_decision_transitions() answers the reviewer's literal
# request directly: for the SAME analysis-directional population Tier
# 3.31 used (so the two reports are comparable candidate-for-candidate,
# not just headline-number-for-headline-number), a plain 2x2 transition
# between the real historical Coordinator decision (traded vs. not) and
# the hypothetical post-hoc urgent/risk_off veto (would-skip vs.
# wouldn't) — reading only already-frozen candidate.decision fields, no
# replay, no barrier simulation, no non_overlapping scheduling:
#
#   coordinator_trade_veto_would_skip    -- real Coordinator traded, but
#                                            News urgent or Macro risk_off
#                                            was present -- the veto's
#                                            true direct decision-level
#                                            kill count
#   coordinator_trade_veto_survives      -- real Coordinator traded, no
#                                            veto flag present -- what
#                                            coordinator_veto_filtered's
#                                            decision-level trade count
#                                            should equal (before any
#                                            barrier-sim/non_overlapping
#                                            path effects reshape it)
#   coordinator_skip_veto_would_also_skip -- real Coordinator did NOT
#                                            trade (no_trade or
#                                            insufficient_data) AND a
#                                            veto flag was ALSO present
#                                            -- the veto was redundant
#                                            here, changed nothing
#   coordinator_skip_veto_irrelevant     -- real Coordinator did NOT
#                                            trade and no veto flag was
#                                            present either -- the veto
#                                            plays no role in this case
#
# `flag_basis_by_transition` splits each transition by which flag(s)
# were actually responsible (`news_urgent_only` / `macro_risk_off_only`
# / `both` / `neither`), giving the explicit urgent-vs-risk_off-vs-
# overlap visibility the reviewer asked for, at the transition level
# rather than folded into one priority-ordered bucket.
# `coordinator_skip_reason_by_transition` further splits the two
# "Coordinator did not trade" transitions by the real historical reason
# (`no_trade` vs `insufficient_data`), since "the veto was redundant"
# means something different depending on whether Coordinator was already
# going to skip for a quorum reason vs. a below-threshold-score reason.
#
# Structural note specific to the LIVE WEIGHTS/MIN_AVAILABLE_WEIGHT
# configuration (0.40/0.25/0.20/0.15, 60% floor) — NOT a universal
# guarantee the way Tier 3.31's Timing zero-count proof is, and would
# need re-checking if those constants ever change: in this
# analysis-directional population Analysis is always present, so
# `insufficient_data` can only occur when News AND Macro are BOTH
# absent (Analysis alone is 0.40/0.80=50% < 60%; adding either agent
# alone already clears the floor — 0.65/0.80=81% with News, 0.55/0.80=
# 69% with Macro). A veto flag requires its agent to be PRESENT, so
# `coordinator_skip_veto_would_also_skip` can never carry an
# `insufficient_data` reason today — only `no_trade`. See the test
# suite for the explicit proof this doesn't silently happen.
#
# Entirely offline: reads already-stored candidate.decision fields only,
# no replay, no LLM calls, no candidate mutated, no barrier-backtest
# simulation involved at all. COORDINATOR_THRESHOLD/WEIGHTS/
# MIN_AVAILABLE_WEIGHT/analysis_risk_filtered's own veto scope all
# untouched.


def compute_veto_decision_transitions(candidates: list[dict]) -> dict:
    """See the Tier 3.34 module comment above. Same analysis-directional
    population scope as compute_risk_filter_veto_attribution (Tier
    3.31), for direct candidate-for-candidate comparability. Returns
    candidates_considered, the analysis_not_directional exclusion count,
    `transition_summary` (the 2x2 transition -> count), `flag_basis_by_
    transition` (transition -> {news_urgent_only/macro_risk_off_only/
    both/neither -> count}), `coordinator_skip_reason_by_transition`
    (the two non-trade transitions split by no_trade vs
    insufficient_data), the full per-candidate `cases` list, and
    `opinion_level_day_blocked` (Analysis-opinion identity, day-blocked,
    same convention as Tier 3.31's own report)."""
    cases = []
    excluded_not_directional = 0

    for candidate in candidates:
        decision = candidate.get("decision") or {}
        opinions_used = decision.get("opinions_used") or {}
        analysis_opinion = opinions_used.get("analysis")
        analysis_direction = (analysis_opinion or {}).get("direction")
        if not analysis_opinion or analysis_direction not in ("bullish", "bearish"):
            excluded_not_directional += 1
            continue

        news_opinion = opinions_used.get("news")
        macro_opinion = opinions_used.get("macro")
        news_urgent = bool(
            news_opinion and _RISK_FILTER_NEWS_VETO_FLAGS & set(news_opinion.get("flags") or [])
        )
        macro_risk_off = bool(
            macro_opinion and _RISK_FILTER_MACRO_VETO_FLAGS & set(macro_opinion.get("flags") or [])
        )
        veto_would_apply = news_urgent or macro_risk_off
        if news_urgent and macro_risk_off:
            flag_basis = "both"
        elif news_urgent:
            flag_basis = "news_urgent_only"
        elif macro_risk_off:
            flag_basis = "macro_risk_off_only"
        else:
            flag_basis = "neither"

        coordinator_decision = decision.get("decision")
        coordinator_traded = coordinator_decision in _DIRECTIONAL_DECISIONS

        if coordinator_traded and veto_would_apply:
            transition = "coordinator_trade_veto_would_skip"
        elif coordinator_traded and not veto_would_apply:
            transition = "coordinator_trade_veto_survives"
        elif not coordinator_traded and veto_would_apply:
            transition = "coordinator_skip_veto_would_also_skip"
        else:
            transition = "coordinator_skip_veto_irrelevant"

        cases.append({
            "candidate_id": candidate.get("candidate_id"),
            "bar_timestamp": (candidate.get("bar") or {}).get("timestamp"),
            "trading_date": (candidate.get("bar") or {}).get("trading_date"),
            "analysis_opinion_timestamp": analysis_opinion.get("timestamp"),
            "analysis_direction": analysis_direction,
            "coordinator_decision": coordinator_decision,
            "news_urgent": news_urgent,
            "macro_risk_off": macro_risk_off,
            "flag_basis": flag_basis,
            "transition": transition,
        })

    transition_summary: dict[str, int] = {}
    flag_basis_by_transition: dict[str, dict[str, int]] = {}
    coordinator_skip_reason_by_transition: dict[str, dict[str, int]] = {}
    for case in cases:
        t = case["transition"]
        transition_summary[t] = transition_summary.get(t, 0) + 1

        fb = case["flag_basis"]
        flag_basis_by_transition.setdefault(t, {})
        flag_basis_by_transition[t][fb] = flag_basis_by_transition[t].get(fb, 0) + 1

        if t in ("coordinator_skip_veto_would_also_skip", "coordinator_skip_veto_irrelevant"):
            reason = case["coordinator_decision"]  # "no_trade" or "insufficient_data"
            coordinator_skip_reason_by_transition.setdefault(t, {})
            coordinator_skip_reason_by_transition[t][reason] = (
                coordinator_skip_reason_by_transition[t].get(reason, 0) + 1
            )

    return {
        "candidates_considered": len(candidates),
        "analysis_not_directional_excluded": excluded_not_directional,
        "analysis_directional_candidates": len(cases),
        "transition_summary": transition_summary,
        "flag_basis_by_transition": flag_basis_by_transition,
        "coordinator_skip_reason_by_transition": coordinator_skip_reason_by_transition,
        "cases": cases,
        "opinion_level_day_blocked": _opinion_level_day_blocked_summary(
            cases, category_field="transition", opinion_field="analysis_opinion_timestamp", day_field="trading_date",
        ),
    }


# ---------------------------------------------------------------------------
# Opinion-level, day-blocked re-aggregation — Tier 3.29
# ---------------------------------------------------------------------------
#
# Sixth external review, ranked backlog item #3 (relayed verbatim): the
# reviewer asked to redo Tier 3.26/3.27's headline aggregation at the
# opinion level and day-blocked, instead of pooling every CANDIDATE as
# if it were an independent data point. The same concern applies to
# Tier 3.28's cross_tab, built after the review was written but with an
# identical shape.
#
# Every one of those three diagnostics already reports one clean
# categorical label per case (side / attribution / quadrant) and pools
# it across however many candidates landed in its subset. That pooled
# count conflates two things worth separating:
#
#   1. How many genuinely INDEPENDENT LLM opinions actually drove the
#      split. News/Macro run on a slow cadence (NEWS_INTERVAL_MINUTES/
#      MACRO_INTERVAL_MINUTES, default 60, reusable up to
#      NEWS_MACRO_MAX_AGE_MINUTES, default 90) and get reused across
#      many consecutive candidates while fresh — the same duplication
#      concern Tier 3.6 first raised for per-agent accuracy, and Tier
#      3.27's own `prevalence` section already answered for urgent's
#      base rate specifically. This generalizes that fix to any
#      categorical split these diagnostics report.
#   2. Whether the split is a broad pattern across many TRADING DAYS or
#      an artifact of one unusually active or volatile day dominating
#      the pool.
#
# _opinion_level_day_blocked_summary() below is a single shared
# aggregator, wired into all three diagnostics' return dicts as a new
# "opinion_level_day_blocked" key — additive, not a replacement for the
# existing candidate-level cross_tab/summary/by_attribution fields those
# functions already return, so nothing that already depended on their
# shape breaks. It is read-only, pure post-processing over each
# diagnostic's own already-computed `cases` list — no new replays, no
# LLM calls, no mutation of anything stored.


def _opinion_level_day_blocked_summary(
    cases: list[dict], category_field: str, opinion_field: str = "opinion_timestamp", day_field: str = "trading_date",
) -> dict:
    """Groups `cases` by (day_field, opinion_field) and re-tabulates
    `category_field` two ways per day: `category_counts_candidate_level`
    (the existing raw per-candidate count, kept for direct comparison)
    and `category_counts_opinion_weighted` (each case weighted
    1 / how-many-cases-share-its-exact-(day, opinion)-pair, so a reused
    opinion's total weight within a day always sums to exactly 1 —
    split fractionally across whichever categories its various
    candidates actually landed in, if a reused opinion combined with
    different Analysis/Macro context to different effect on different
    candidates, rather than silently excluded or double-counted as N
    independent data points).

    `opinion_weighted_totals` / `candidate_level_totals` are the same
    breakdown pooled across every day, so the honest (opinion-weighted)
    figure sits right next to the existing raw candidate-pooled one for
    direct comparison. `distinct_opinions_total` counts unique
    opinion_field values across ALL cases (not the sum of each day's own
    distinct count) — the two can differ by a handful when an opinion
    reused near a trading-day boundary genuinely appears in both days,
    which is correct, not a bug: that opinion really was used on both
    days.

    A case missing `day_field`, `opinion_field`, or `category_field`
    entirely is counted in `uncategorized_count` and excluded from
    `by_day` and both weighted totals — never silently dropped without a
    trace, never guessed into a bucket."""
    by_day: dict[str, dict] = {}
    all_opinions: set[str] = set()
    candidate_level_totals: dict[str, int] = {}
    opinion_weighted_totals: dict[str, float] = {}
    uncategorized_count = 0

    day_opinion_case_counts: dict[tuple, int] = {}
    for case in cases:
        day = case.get(day_field)
        opinion = case.get(opinion_field)
        if not day or not opinion:
            continue
        key = (day, opinion)
        day_opinion_case_counts[key] = day_opinion_case_counts.get(key, 0) + 1

    for case in cases:
        category = case.get(category_field)
        day = case.get(day_field)
        opinion = case.get(opinion_field)
        if not day or not opinion or category is None:
            uncategorized_count += 1
            continue

        all_opinions.add(opinion)
        candidate_level_totals[category] = candidate_level_totals.get(category, 0) + 1

        day_bucket = by_day.setdefault(day, {
            "candidates_considered": 0,
            "distinct_opinions": set(),
            "category_counts_candidate_level": {},
            "category_counts_opinion_weighted": {},
        })
        day_bucket["candidates_considered"] += 1
        day_bucket["distinct_opinions"].add(opinion)
        day_bucket["category_counts_candidate_level"][category] = (
            day_bucket["category_counts_candidate_level"].get(category, 0) + 1
        )

        weight = 1.0 / day_opinion_case_counts[(day, opinion)]
        day_bucket["category_counts_opinion_weighted"][category] = (
            day_bucket["category_counts_opinion_weighted"].get(category, 0.0) + weight
        )
        opinion_weighted_totals[category] = opinion_weighted_totals.get(category, 0.0) + weight

    for day_bucket in by_day.values():
        day_bucket["distinct_opinions"] = len(day_bucket["distinct_opinions"])
        day_bucket["category_counts_opinion_weighted"] = {
            k: round(v, 3) for k, v in day_bucket["category_counts_opinion_weighted"].items()
        }

    return {
        "days_considered": len(by_day),
        "distinct_opinions_total": len(all_opinions),
        "uncategorized_count": uncategorized_count,
        "by_day": by_day,
        "candidate_level_totals": candidate_level_totals,
        "opinion_weighted_totals": {k: round(v, 3) for k, v in opinion_weighted_totals.items()},
    }
