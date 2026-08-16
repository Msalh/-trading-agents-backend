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

from app.coordinator import WEIGHTS, _score_opinions
from app.coordinator_diagnostics import compute_coordinator_divergence_report

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
):
    """Builds a candidate-shaped dict {"symbol", "timeframe", "decision"}
    around a REAL CoordinatorDecision computed via _score_opinions —
    same function every real candidate is scored through — so tests
    exercise compute_coordinator_divergence_report() against the exact
    shape it sees in production, not a hand-rolled approximation."""
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
        min_available_weight=0.0,  # keep test fixtures simple -- not testing the gate here
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
    # News alone is what pushes Coordinator over threshold; Analysis's
    # much weaker opposing lean isn't enough by itself.
    # original score = (-0.4*10 + 0.25*95) / 0.65 = 30.4 -> enter_long
    # with News's weight zeroed: (-0.4*10) / 0.4 = -10 -> no_trade
    candidate = _candidate(
        analysis=_opinion("bearish", 10),
        news=_opinion("bullish", 95),
    )
    report = compute_coordinator_divergence_report([candidate])
    ablation = report["ablation"]["news_removed"]
    assert ablation["candidates_considered"] == 1
    assert ablation["decision_changed"] == 1


def test_ablation_reports_unchanged_when_agent_was_never_pivotal():
    # Analysis alone already clears the threshold in the same
    # direction News points -- removing News's weight shouldn't flip
    # anything here.
    candidate = _candidate(
        analysis=_opinion("bullish", 95),
        news=_opinion("bullish", 30),
    )
    report = compute_coordinator_divergence_report([candidate])
    ablation = report["ablation"]["news_removed"]
    assert ablation["decision_changed"] == 0
    assert ablation["decision_unchanged"] == 1


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
