"""
Unit tests for app.coordinator_diagnostics — Tier 3.16 (Coordinator/
Analysis divergence + ablation). No LLM, no network: builds real
CoordinatorDecision objects via coordinator._score_opinions() (the
exact same scoring path a real candidate goes through) so candidate
fixtures here are indistinguishable in shape from what
app/candidates.create_candidate() actually persists.

Run with: pytest tests/test_coordinator_diagnostics.py -v
"""

import itertools

from app.coordinator import MIN_AVAILABLE_WEIGHT, WEIGHTS, _score_opinions
from app.coordinator_diagnostics import _classify_ablation_change, compute_coordinator_divergence_report

_candidate_ids = itertools.count(1)


def _opinion(direction, confidence):
    return {"direction": direction, "confidence": confidence, "timestamp": "2026-08-16T14:00:00Z"}


def _candidate(
    symbol="TEST",
    timeframe="5m",
    analysis=None,
    news=None,
    macro=None,
    missing_agents=None,
    stale_agents=None,
    threshold=25.0,
    min_available_weight=0.0,
):
    """Builds a candidate-shaped dict {"symbol", "timeframe", "decision"}
    around a REAL CoordinatorDecision computed via _score_opinions —
    same function every real candidate is scored through — so tests
    exercise compute_coordinator_divergence_report() against the exact
    shape it sees in production, not a hand-rolled approximation.

    min_available_weight defaults to 0.0 to keep most fixtures simple
    (not testing the availability gate) -- BUT note that ablation
    replays always run under the LIVE MIN_AVAILABLE_WEIGHT (app.
    coordinator.MIN_AVAILABLE_WEIGHT, 0.6 by default) regardless of
    this override, since replay_candidate() falls back to the live
    global whenever a replay doesn't explicitly pass one. Any fixture
    whose ablation behavior is under test should pass
    min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT here so the
    original decision and its replay are scored under the same gate."""
    opinions = {}
    if analysis is not None:
        opinions["analysis"] = analysis
    if news is not None:
        opinions["news"] = news
    if macro is not None:
        opinions["macro"] = macro

    decision = _score_opinions(
        symbol=symbol,
        timeframe=timeframe,
        opinions=opinions,
        missing_agents=missing_agents or [],
        stale_agents=stale_agents or [],
        weights=WEIGHTS,
        threshold=threshold,
        min_available_weight=min_available_weight,
    )
    return {
        "candidate_id": f"test-candidate-{next(_candidate_ids)}",
        "symbol": symbol,
        "timeframe": timeframe,
        "decision": decision.to_dict(),
    }


def test_same_direction_category(fresh_env=None):
    # Analysis strongly bullish, News/Macro agree -- Coordinator should
    # land on enter_long, same direction as Analysis.
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_opinion("bullish", 80),
        macro=_opinion("bullish", 70),
    )
    report = compute_coordinator_divergence_report([candidate])
    assert report["named_categories"] == {"analysis_directional_coordinator_same_direction": 1}
    assert report["cross_tab"]["directional"]["enter_long"] == 1


def test_opposite_direction_category():
    # Analysis bearish but only mildly confident; News/Macro are both
    # strongly bullish and together outweigh Analysis's (weight 0.4)
    # opposing lean enough to flip the blended score to enter_long.
    # score = (-0.4*40 + 0.25*95 + 0.15*95) / 0.8 = 27.5 -> enter_long
    candidate = _candidate(
        analysis=_opinion("bearish", 40),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 95),
    )
    report = compute_coordinator_divergence_report([candidate])
    assert report["named_categories"].get("analysis_directional_coordinator_opposite_direction") == 1


def test_no_trade_category():
    # Weak, roughly balanced signals -- should stay under threshold.
    candidate = _candidate(
        analysis=_opinion("bullish", 20),
        news=_opinion("bearish", 15),
    )
    report = compute_coordinator_divergence_report([candidate])
    assert report["named_categories"].get("analysis_directional_coordinator_no_trade") == 1


def test_neutral_analysis_coordinator_directional_category():
    # Analysis itself is neutral (no lean), but News/Macro are strong
    # enough alone to push the blended score past threshold.
    candidate = _candidate(
        analysis=_opinion("neutral", 50),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 95),
    )
    report = compute_coordinator_divergence_report([candidate])
    assert report["named_categories"].get("analysis_neutral_coordinator_directional") == 1


def test_analysis_unavailable_when_missing():
    candidate = _candidate(
        news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
        missing_agents=["analysis"],
    )
    report = compute_coordinator_divergence_report([candidate])
    assert "unavailable" in report["cross_tab"]
    assert sum(report["cross_tab"]["unavailable"].values()) == 1


def test_news_impact_counts_presence_and_opposition():
    candidate = _candidate(
        analysis=_opinion("bullish", 80),
        news=_opinion("bearish", 60),  # opposes Analysis's direction
    )
    report = compute_coordinator_divergence_report([candidate])
    assert report["news_impact"]["present_and_directional"] == 1
    assert report["news_impact"]["opposed_analysis_direction"] == 1
    assert report["news_impact"]["avg_abs_contribution_when_present"] > 0


def test_macro_impact_zero_when_never_present():
    candidate = _candidate(analysis=_opinion("bullish", 80))
    report = compute_coordinator_divergence_report([candidate])
    assert report["macro_impact"]["present_and_directional"] == 0
    assert report["macro_impact"]["avg_abs_contribution_when_present"] is None


def test_ablation_reports_a_changed_decision_when_removing_the_deciding_agent():
    # News is the deciding factor: Analysis and Macro's weak opposition
    # (-0.4*10 + 0.15*20 = -1) alone wouldn't clear the threshold, but
    # News's strong confirmation pushes it over.
    # All three present, so Tier 3.17's fix (removing News's OPINION,
    # not zeroing its weight) leaves Analysis+Macro's combined weight
    # (0.55/0.8 = 0.6875) safely above the 0.6 availability gate --
    # this candidate is a clean test of News's real influence, with no
    # availability-gate side effect muddying the result.
    # original: (-0.4*10 + 0.25*95 + 0.15*20) / 0.8 = 28.4 -> enter_long
    # News's opinion removed: (-0.4*10 + 0.15*20) / 0.8 = -1.0 -> no_trade
    candidate = _candidate(
        analysis=_opinion("bearish", 10),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 20),
    )
    report = compute_coordinator_divergence_report([candidate])
    ablation = report["ablation"]["news_removed"]
    assert ablation["candidates_considered"] == 1
    assert ablation["agent_present_count"] == 1
    assert ablation["decision_changed"] == 1
    # Tier 3.21: enter_long -> no_trade crosses one threshold boundary
    # without reversing sign and both sides stayed data-sufficient --
    # neither to_insufficient_data nor direction_flipped applies.
    assert ablation["decision_changed_by_category"] == {"threshold_crossing": 1}
    assert ablation["transitions"] == {"enter_long -> no_trade": 1}


def test_ablation_reports_unchanged_when_agent_was_never_pivotal():
    # Analysis+Macro alone already clear the threshold in the same
    # direction News points, and stay above the 0.6 availability gate
    # without News (0.55/0.8 = 0.6875) -- removing News's opinion
    # shouldn't flip anything here.
    # original: (0.4*95 + 0.25*10 + 0.15*80) / 0.8 = 65.625 -> enter_long
    # News's opinion removed: (0.4*95 + 0.15*80) / 0.8 = 62.5 -> enter_long
    candidate = _candidate(
        analysis=_opinion("bullish", 95),
        news=_opinion("bullish", 10),
        macro=_opinion("bullish", 80),
    )
    report = compute_coordinator_divergence_report([candidate])
    ablation = report["ablation"]["news_removed"]
    assert ablation["agent_present_count"] == 1
    assert ablation["decision_changed"] == 0
    assert ablation["decision_unchanged"] == 1
    assert ablation["decision_changed_by_category"] == {}
    assert ablation["avg_abs_score_delta_when_changed"] is None
    assert ablation["avg_abs_score_delta_when_unchanged"] is not None


def test_ablation_never_flips_a_candidate_where_the_agent_was_absent():
    # Tier 3.17 regression test: the bug this tier fixed. Only Analysis
    # is present (News/Macro both absent) -- available_fraction is
    # 0.4/0.8 = 0.5, below the live 0.6 gate, so this candidate is
    # itself insufficient_data (built with min_available_weight=
    # MIN_AVAILABLE_WEIGHT so the original decision is scored under the
    # SAME gate the ablation replay will use). Ablating News or Macro
    # -- neither ever present -- must be a true no-op: it must NOT flip
    # this out of insufficient_data purely from the availability gate's
    # denominator shrinking (the exact production bug this tier fixed:
    # 36/197 real candidates falsely flipped this way pre-fix).
    candidate = _candidate(analysis=_opinion("bullish", 95), min_available_weight=MIN_AVAILABLE_WEIGHT)
    assert candidate["decision"]["decision"] == "insufficient_data"
    report = compute_coordinator_divergence_report([candidate])
    for key in ("news_removed", "macro_removed"):
        ablation = report["ablation"][key]
        assert ablation["agent_present_count"] == 0
        assert ablation["decision_changed"] == 0
        assert ablation["decision_changed_by_category"] == {}


def test_ablation_categorizes_a_change_to_insufficient_data():
    # Analysis+News present (no Macro): 0.65/0.80 = 0.8125, safely above
    # the 0.6 gate -- a real directional decision. Ablating News alone
    # drops available directional weight to just Analysis (0.40/0.80 =
    # 0.5 < 0.6) -- a clean, natural "to_insufficient_data" case (the
    # quorum effect the fourth review specifically wanted separated out
    # from a genuine directional-influence change).
    candidate = _candidate(
        analysis=_opinion("bullish", 95),
        news=_opinion("bullish", 10),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    assert candidate["decision"]["decision"] in ("enter_long", "enter_short", "no_trade")
    report = compute_coordinator_divergence_report([candidate])
    ablation = report["ablation"]["news_removed"]
    assert ablation["decision_changed"] == 1
    assert ablation["decision_changed_by_category"] == {"to_insufficient_data": 1}


# ---------------------------------------------------------------------------
# Tier 3.21: _classify_ablation_change -- direct unit tests. Exercised
# against hand-built replay_candidate()-shaped dicts rather than the
# full pipeline: under the LIVE weights/threshold, a "direction_flipped"
# outcome turns out to be mathematically unreachable for any single
# agent's ablation (removing one agent's raw contribution, even at full
# confidence, is never enough to both let the original cross +threshold
# AND flip the post-ablation score past -threshold once you work through
# the renormalized-denominator algebra for each agent) -- so this
# category is tested directly against the classifier function, not
# coaxed out of real confidence values that cannot actually produce it
# under the current config. That asymmetry (analysis/news/macro's
# weights relative to COORDINATOR_THRESHOLD=25 and MIN_AVAILABLE_WEIGHT
# =0.6 structurally forbid a full reversal from ablating just one
# agent) is itself a notable finding, not a testing inconvenience.
# ---------------------------------------------------------------------------

def _replay_result(original_decision, original_direction, original_score,
                    replayed_decision, replayed_direction, replayed_score,
                    replayed_conflict_flags=None):
    return {
        "changed": original_decision != replayed_decision,
        "original": {
            "decision": original_decision, "direction": original_direction,
            "score": original_score, "threshold": 25.0, "config_version": {},
        },
        "replayed": {
            "decision": replayed_decision, "direction": replayed_direction,
            "score": replayed_score, "conflict_flags": replayed_conflict_flags or [],
        },
    }


def test_classify_ablation_change_unchanged_has_no_category():
    result = _replay_result("enter_long", "bullish", 30.0, "enter_long", "bullish", 28.0)
    classified = _classify_ablation_change([], result)
    assert classified["changed"] is False
    assert classified["category"] is None
    assert classified["score_delta"] == -2.0


def test_classify_ablation_change_to_insufficient_data():
    result = _replay_result("enter_long", "bullish", 30.0, "insufficient_data", "neutral", 0.0)
    classified = _classify_ablation_change([], result)
    assert classified["category"] == "to_insufficient_data"


def test_classify_ablation_change_direction_flipped():
    result = _replay_result("enter_long", "bullish", 30.0, "enter_short", "bearish", -30.0)
    classified = _classify_ablation_change([], result)
    assert classified["category"] == "direction_flipped"


def test_classify_ablation_change_threshold_crossing():
    result = _replay_result("enter_long", "bullish", 30.0, "no_trade", "neutral", 10.0)
    classified = _classify_ablation_change([], result)
    assert classified["category"] == "threshold_crossing"


def test_classify_ablation_change_detects_conflict_flags_changed():
    result = _replay_result(
        "enter_long", "bullish", 30.0, "no_trade", "neutral", 10.0,
        replayed_conflict_flags=["timing_low_liquidity_dampened"],
    )
    unchanged_flags = _classify_ablation_change(["timing_low_liquidity_dampened"], result)
    assert unchanged_flags["conflict_flags_changed"] is False

    changed_flags = _classify_ablation_change(["analysis_news_conflict"], result)
    assert changed_flags["conflict_flags_changed"] is True


def test_timing_blocked_count(monkeypatch):
    candidate = _candidate(analysis=_opinion("bullish", 90))
    candidate["decision"]["conflict_flags"] = ["timing_market_closed"]
    report = compute_coordinator_divergence_report([candidate])
    assert report["timing_blocked_count"] == 1


def test_empty_candidate_list_returns_zeroed_report():
    report = compute_coordinator_divergence_report([])
    assert report["candidates_considered"] == 0
    assert report["cross_tab"] == {}
    assert report["named_categories"] == {}
    assert report["timing_blocked_count"] == 0
    for agent_key in ("analysis_removed", "news_removed", "macro_removed"):
        assert report["ablation"][agent_key]["candidates_considered"] == 0
        assert report["ablation"][agent_key]["agent_present_count"] == 0
