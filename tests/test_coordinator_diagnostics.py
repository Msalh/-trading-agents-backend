"""
Unit tests for app.coordinator_diagnostics — Tier 3.16 (Coordinator/
Analysis divergence + ablation). No LLM, no network: builds real
CoordinatorDecision objects via coordinator._score_opinions() (the
exact same scoring path a real candidate goes through) so candidate
fixtures here are indistinguishable in shape from what
app/candidates.create_candidate() actually persists.

Run with: pytest tests/test_coordinator_diagnostics.py -v
"""

import importlib
import itertools
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.coordinator import MIN_AVAILABLE_WEIGHT, WEIGHTS, _score_opinions
from app.coordinator_diagnostics import (
    _classify_ablation_change,
    _opinion_level_day_blocked_summary,
    compute_coordinator_divergence_report,
    compute_news_urgent_analysis,
    compute_news_urgent_decomposition,
    compute_news_urgent_prevalence,
    compute_news_urgent_vs_calendar_blackout,
    compute_risk_filter_veto_attribution,
    compute_threshold_crossing_deep_dive,
    compute_veto_decision_transitions,
)

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


# ---------------------------------------------------------------------------
# compute_threshold_crossing_deep_dive — Tier 3.26
#
# Needs real storage (compute_outcome_for_candidate reads trade rows and
# outcomes.compute_outcome_at_horizon reads market_state bars), unlike
# the tests above -- same temp-DB "fresh_env" pattern as test_replay.py/
# test_outcomes.py, reloading storage -> coordinator -> outcomes ->
# replay -> coordinator_diagnostics in dependency order so every
# already-bound `from X import Y` name in this chain points at the
# fresh DB, not whatever DB_PATH an earlier test file left behind.
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.coordinator as coordinator
    importlib.reload(coordinator)

    import app.outcomes as outcomes
    importlib.reload(outcomes)

    import app.replay as replay
    importlib.reload(replay)

    import app.coordinator_diagnostics as coordinator_diagnostics
    importlib.reload(coordinator_diagnostics)

    yield storage, coordinator, outcomes, replay, coordinator_diagnostics


def _dd_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dd_save_bar(storage, symbol, timeframe, timestamp_dt, close):
    import json
    conn = storage.get_connection()
    payload = {
        "event_id": f"{symbol}:{timeframe}:{_dd_iso(timestamp_dt)}",
        "symbol": symbol, "timeframe": timeframe, "timestamp": _dd_iso(timestamp_dt),
        "close": close, "high": close, "low": close, "open": close,
    }
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (payload["event_id"], symbol, timeframe, payload["timestamp"], json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def _dd_opinion(direction, confidence, timestamp, flags=None):
    return {
        "direction": direction, "confidence": confidence, "reasoning": "test",
        "key_data": {}, "flags": flags or [], "timestamp": timestamp,
    }


def _dd_candidate(coordinator, candidate_id, symbol, timeframe, decision_timestamp, analysis=None, news=None, macro=None):
    """Builds a real CoordinatorDecision (via the reloaded coordinator
    module's own _score_opinions, live WEIGHTS/DECISION_THRESHOLD/
    MIN_AVAILABLE_WEIGHT) and wraps it candidate-shaped, same as the
    top-of-file _candidate() helper but scoped to the reloaded module
    so ablation replays inside compute_threshold_crossing_deep_dive see
    the exact same live config this fixture set up."""
    opinions = {}
    if analysis is not None:
        opinions["analysis"] = analysis
    if news is not None:
        opinions["news"] = news
    if macro is not None:
        opinions["macro"] = macro
    decision = coordinator._score_opinions(
        symbol=symbol, timeframe=timeframe, opinions=opinions,
        missing_agents=[], stale_agents=[],
        weights=coordinator.WEIGHTS, threshold=coordinator.DECISION_THRESHOLD,
        min_available_weight=coordinator.MIN_AVAILABLE_WEIGHT,
    ).to_dict()
    decision["timestamp"] = decision_timestamp
    return {"candidate_id": candidate_id, "symbol": symbol, "timeframe": timeframe, "decision": decision}


def _dd_trade(candidate_id, symbol, timeframe, direction, entry, opened_at):
    return {
        "trade_id": f"trade-{candidate_id}", "candidate_id": candidate_id,
        "symbol": symbol, "timeframe": timeframe, "direction": direction, "size": 1,
        "order_type": "market", "entry_price": entry, "stop_loss": entry - 10,
        "targets": [entry + 20], "status": "open", "opened_at": opened_at, "fill_price": entry,
    }


def test_deep_dive_rejects_unknown_agent(fresh_env):
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    with pytest.raises(ValueError):
        coordinator_diagnostics.compute_threshold_crossing_deep_dive([], agent="timing")


def test_deep_dive_agent_enabled_trade_hypothetical_outcome(fresh_env):
    """News's presence alone crosses the threshold (analysis 20 +
    macro 10 alone stay well under it, per _score_opinions with live
    WEIGHTS -- verified empirically while designing this test) -- a
    clean agent_enabled_trade case with no real trade attached, so the
    outcome falls back to the existing hypothetical horizon estimate."""
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    horizon_time = decision_time + timedelta(minutes=15)
    _dd_save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _dd_save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 20, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 70, _dd_iso(decision_time)),
        macro=_dd_opinion("bullish", 10, _dd_iso(decision_time)),
    )
    assert candidate["decision"]["decision"] == "enter_long"

    result = coordinator_diagnostics.compute_threshold_crossing_deep_dive([candidate], agent="news", horizons=[15])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["side"] == "agent_enabled_trade"
    assert case["agreement_with_analysis"] == "agree"
    assert case["outcome"]["kind"] == "hypothetical"
    assert case["outcome"]["by_horizon"][15] == "correct"  # bullish call, price rose
    assert result["summary"]["by_side"] == {"agent_enabled_trade": 1}
    assert result["summary"]["agent_enabled_trade_hypothetical_outcomes_by_horizon"][15] == {"correct": 1}


def test_deep_dive_agent_enabled_trade_real_trade_outcome(fresh_env):
    """Same enabling scenario as above, but this candidate actually
    became a real, closed paper trade -- the outcome must come from the
    real P&L (compute_outcome_for_candidate's preferred source), not
    the hypothetical horizon estimate."""
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)

    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 20, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 70, _dd_iso(decision_time)),
        macro=_dd_opinion("bullish", 10, _dd_iso(decision_time)),
    )
    trade = _dd_trade("c1", "TEST", "5m", "bullish", 20020.0, _dd_iso(decision_time))
    storage.save_paper_trade(trade)
    storage.close_trade(trade["trade_id"], exit_price=20100.0, exit_reason="target_hit", pnl_usd=160.0, closed_at=_dd_iso(datetime.now(timezone.utc)))

    result = coordinator_diagnostics.compute_threshold_crossing_deep_dive([candidate], agent="news")
    case = result["cases"][0]
    assert case["outcome"] == {"kind": "real_trade", "status": "closed", "outcome": "win", "pnl_usd": 160.0}
    assert result["summary"]["agent_enabled_trade_real_outcomes"] == {"win": 1}


def test_deep_dive_agent_prevented_trade(fresh_env):
    """News bearish against an otherwise-bullish analysis+macro drags
    the blended score under threshold (no_trade); removing News alone
    crosses it back over (enter_long) -- an agent_prevented_trade case,
    with News opposing Analysis's own direction. There's no real trade
    to look up (it never happened) -- outcome must come from the
    REPLAYED decision's hypothetical estimate, relabeled prevented_*."""
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    horizon_time = decision_time + timedelta(minutes=15)
    _dd_save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _dd_save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 60, _dd_iso(decision_time)),
        news=_dd_opinion("bearish", 40, _dd_iso(decision_time)),
        macro=_dd_opinion("bullish", 30, _dd_iso(decision_time)),
    )
    assert candidate["decision"]["decision"] == "no_trade"

    result = coordinator_diagnostics.compute_threshold_crossing_deep_dive([candidate], agent="news", horizons=[15])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["side"] == "agent_prevented_trade"
    assert case["agreement_with_analysis"] == "oppose"
    assert case["outcome"]["kind"] == "prevented_hypothetical"
    # The replayed (would-have-been) decision is enter_long; price rose,
    # so the prevented trade WOULD have won -- a missed opportunity.
    assert case["outcome"]["by_horizon"][15] == "prevented_win"
    assert result["summary"]["by_side"] == {"agent_prevented_trade": 1}
    assert result["summary"]["agent_prevented_trade_hypothetical_outcomes_by_horizon"][15] == {"prevented_win": 1}


def test_deep_dive_skips_candidates_where_agent_absent(fresh_env):
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 90, _dd_iso(decision_time)),
    )
    result = coordinator_diagnostics.compute_threshold_crossing_deep_dive([candidate], agent="news")
    assert result["cases_considered"] == 0
    assert result["cases"] == []


def test_deep_dive_distinct_opinion_timestamps_dedupes_reused_opinion(fresh_env):
    """Two candidates that both reused the SAME News opinion (identical
    opinion_timestamp, the real News/Macro reuse pattern -- see the
    Tier 3.6 note in app/outcomes.py) must count as ONE distinct
    opinion, not two, even though both surface as separate cases."""
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time_1 = datetime.now(timezone.utc) - timedelta(hours=3)
    decision_time_2 = datetime.now(timezone.utc) - timedelta(hours=2)
    shared_news_timestamp = _dd_iso(decision_time_1)

    candidate_1 = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time_1),
        analysis=_dd_opinion("bullish", 20, _dd_iso(decision_time_1)),
        news=_dd_opinion("bullish", 70, shared_news_timestamp),
        macro=_dd_opinion("bullish", 10, _dd_iso(decision_time_1)),
    )
    candidate_2 = _dd_candidate(
        coordinator, "c2", "TEST", "5m", _dd_iso(decision_time_2),
        analysis=_dd_opinion("bullish", 20, _dd_iso(decision_time_2)),
        news=_dd_opinion("bullish", 70, shared_news_timestamp),
        macro=_dd_opinion("bullish", 10, _dd_iso(decision_time_2)),
    )

    result = coordinator_diagnostics.compute_threshold_crossing_deep_dive(
        [candidate_1, candidate_2], agent="news", horizons=[15],
    )
    assert result["cases_considered"] == 2
    assert result["distinct_opinion_timestamps"] == 1


def test_deep_dive_urgent_flag_counted_for_news_not_forced_for_macro(fresh_env):
    """News's "urgent" flag isn't just descriptive metadata here -- it
    also halves the blended score (coordinator.py's own dampening
    rule), which is itself capable of turning what would have been an
    agent_enabled_trade case into an agent_prevented_trade one even
    though News and Analysis AGREE on direction (the dampen, not
    opposition, is what dropped the original score under threshold).
    urgent_flag_count must still count it. Ablating macro on a
    candidate where macro carries a DIFFERENT flag vocabulary (no
    "urgent" concept at all, see app/macro_agent.py) must show
    urgent_flag_count == 0 -- by construction, not because urgency was
    checked and found absent for that specific case."""
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    horizon_time = decision_time + timedelta(minutes=15)
    _dd_save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _dd_save_bar(storage, "TEST", "5m", horizon_time, 95.0)

    news_urgent_candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 30, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 100, _dd_iso(decision_time), flags=["urgent"]),
        macro=_dd_opinion("bullish", 20, _dd_iso(decision_time)),
    )
    assert news_urgent_candidate["decision"]["decision"] == "no_trade"  # urgent-dampened below threshold

    news_result = coordinator_diagnostics.compute_threshold_crossing_deep_dive(
        [news_urgent_candidate], agent="news", horizons=[15],
    )
    assert news_result["cases_considered"] == 1
    case = news_result["cases"][0]
    assert case["side"] == "agent_prevented_trade"  # without the dampen, this would have crossed
    assert case["agreement_with_analysis"] == "agree"  # News and Analysis both bullish -- the dampen, not opposition, blocked it
    assert news_result["summary"]["urgent_flag_count"] == 1

    macro_candidate = _dd_candidate(
        coordinator, "c2", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 10, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 15, _dd_iso(decision_time)),
        macro=_dd_opinion("bullish", 90, _dd_iso(decision_time), flags=["risk_off"]),
    )
    assert macro_candidate["decision"]["decision"] == "enter_long"
    macro_result = coordinator_diagnostics.compute_threshold_crossing_deep_dive(
        [macro_candidate], agent="macro", horizons=[15],
    )
    assert macro_result["cases_considered"] == 1
    assert macro_result["cases"][0]["side"] == "agent_enabled_trade"
    assert macro_result["summary"]["urgent_flag_count"] == 0


# ---------------------------------------------------------------------------
# compute_news_urgent_prevalence / compute_news_urgent_decomposition — Tier 3.27
#
# No DB needed (unlike the Tier 3.26 tests above) -- neither function
# calls outcomes.py, so the plain top-of-file _candidate()/_opinion()
# helpers and live coordinator._score_opinions() are enough. Parameter
# combinations below were found by brute-force search over the real live
# WEIGHTS/DECISION_THRESHOLD/MIN_AVAILABLE_WEIGHT math (not hand-derived)
# to hit each of the four attribution categories -- see the Tier 3.27
# module docstring in app/coordinator_diagnostics.py for what each
# category means.
# ---------------------------------------------------------------------------

def _flagged_opinion(direction, confidence, flags):
    return {"direction": direction, "confidence": confidence, "timestamp": "2026-08-16T14:00:00Z", "flags": flags}


def test_urgent_prevalence_candidate_and_distinct_levels():
    # 3 candidates: 2 share the SAME reused urgent News opinion
    # (timestamp "t1"), 1 has a distinct non-urgent News opinion --
    # candidate-level should count all 3, distinct-opinion-level should
    # count only 2 (the reuse collapses to one).
    urgent_opinion = {"direction": "bullish", "confidence": 70, "timestamp": "t1", "flags": ["urgent"]}
    calm_opinion = {"direction": "bearish", "confidence": 40, "timestamp": "t2", "flags": []}
    c1 = _candidate(analysis=_opinion("bullish", 50), news=urgent_opinion, macro=_opinion("bullish", 50))
    c2 = _candidate(analysis=_opinion("bullish", 50), news=urgent_opinion, macro=_opinion("bullish", 50))
    c3 = _candidate(analysis=_opinion("bullish", 50), news=calm_opinion, macro=_opinion("bullish", 50))

    result = compute_news_urgent_prevalence([c1, c2, c3])
    assert result["candidate_level"] == {
        "news_present_candidates": 3, "urgent_candidates": 2, "urgent_rate": round(2 / 3, 3),
    }
    assert result["distinct_opinion_level"] == {
        "distinct_news_opinions": 2, "distinct_urgent_opinions": 1, "urgent_rate": 0.5,
    }


def test_urgent_prevalence_ignores_candidates_without_news():
    c = _candidate(analysis=_opinion("bullish", 50), macro=_opinion("bullish", 50))  # no news
    result = compute_news_urgent_prevalence([c])
    assert result["candidate_level"]["news_present_candidates"] == 0
    assert result["candidate_level"]["urgent_rate"] is None
    assert result["distinct_opinion_level"]["urgent_rate"] is None


def test_decomposition_attributes_urgent_dampen_alone():
    # analysis=bullish30, news=bullish80(urgent), macro=bullish20 --
    # original stays no_trade (urgent-dampened); removing ONLY News's
    # directional contribution (keeping the dampen) still no_trade;
    # removing ONLY the urgent flag (keeping News's real bullish read)
    # crosses to enter_long -- the dampen alone is what mattered.
    candidate = _candidate(
        analysis=_opinion("bullish", 30),
        news=_flagged_opinion("bullish", 80, ["urgent"]),
        macro=_opinion("bullish", 20),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    assert candidate["decision"]["decision"] == "no_trade"

    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["attribution"] == "urgent_dampen_alone"
    assert case["full_removal"]["changed"] is True
    assert case["full_removal"]["category"] == "threshold_crossing"
    assert case["direction_only_removed"]["changed"] is False
    assert case["urgent_only_removed"]["changed"] is True
    assert result["summary"]["by_attribution"] == {"urgent_dampen_alone": 1}
    assert result["distinct_opinion_timestamps"] == 1


def test_decomposition_attributes_only_combination_sufficient():
    # analysis=bullish40, news=bearish20(urgent) opposing, macro=bullish10
    # -- neither partial variant alone reproduces the full-removal
    # crossing; only removing BOTH News's opposing direction AND its
    # dampen together does.
    candidate = _candidate(
        analysis=_opinion("bullish", 40),
        news=_flagged_opinion("bearish", 20, ["urgent"]),
        macro=_opinion("bullish", 10),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    assert candidate["decision"]["decision"] == "no_trade"

    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["attribution"] == "only_combination_sufficient"
    assert case["direction_only_removed"]["changed"] is False
    assert case["urgent_only_removed"]["changed"] is False


def test_decomposition_attributes_direction_alone():
    # analysis=bullish70, news=bearish90(urgent) opposing, macro=bullish90
    # -- removing ONLY News's opposing direction (keeping the dampen)
    # crosses to enter_long; removing ONLY the urgent flag (keeping
    # News's strong bearish opposition) does NOT -- the directional
    # opposition alone is what held the original at no_trade.
    candidate = _candidate(
        analysis=_opinion("bullish", 70),
        news=_flagged_opinion("bearish", 90, ["urgent"]),
        macro=_opinion("bullish", 90),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    assert candidate["decision"]["decision"] == "no_trade"

    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["attribution"] == "direction_alone"
    assert case["direction_only_removed"]["changed"] is True
    assert case["urgent_only_removed"]["changed"] is False


def test_decomposition_attributes_both_independently_sufficient():
    # analysis=bullish70, news=bearish10(urgent, weak opposition),
    # macro=bullish90 -- BOTH partial variants alone already reproduce
    # the crossing (News's small opposing contribution and its dampen
    # are each independently enough).
    candidate = _candidate(
        analysis=_opinion("bullish", 70),
        news=_flagged_opinion("bearish", 10, ["urgent"]),
        macro=_opinion("bullish", 90),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    assert candidate["decision"]["decision"] == "no_trade"

    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 1
    case = result["cases"][0]
    assert case["attribution"] == "both_independently_sufficient"
    assert case["direction_only_removed"]["changed"] is True
    assert case["urgent_only_removed"]["changed"] is True


def test_decomposition_skips_cases_without_urgent_flag():
    # Same shape as the urgent_dampen_alone scenario, minus the urgent
    # flag -- decomposition is meaningless without it (direction-only-
    # removed would just be full removal), so this candidate must be
    # excluded even though its full ablation IS threshold_crossing.
    candidate = _candidate(
        analysis=_opinion("bullish", 30),
        news=_opinion("bullish", 80),  # no urgent flag
        macro=_opinion("bullish", 20),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 0
    assert result["cases"] == []


def test_decomposition_skips_non_threshold_crossing_full_removal():
    # News present and urgent, but removing it entirely drops the
    # candidate to insufficient_data (a quorum effect), not
    # threshold_crossing -- must be excluded from the decomposition,
    # same subset restriction Tier 3.26's deep dive already applies.
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        min_available_weight=MIN_AVAILABLE_WEIGHT,  # only analysis+news present
    )
    full_check = compute_threshold_crossing_deep_dive([candidate], agent="news")
    assert full_check["cases_considered"] == 0  # confirms this candidate is NOT threshold_crossing

    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 0


def test_news_urgent_analysis_combines_prevalence_and_decomposition():
    candidate = _candidate(
        analysis=_opinion("bullish", 30),
        news=_flagged_opinion("bullish", 80, ["urgent"]),
        macro=_opinion("bullish", 20),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    result = compute_news_urgent_analysis([candidate])
    assert set(result.keys()) == {"prevalence", "decomposition"}
    assert result["prevalence"]["candidate_level"]["news_present_candidates"] == 1
    assert result["decomposition"]["cases_considered"] == 1


# ---------------------------------------------------------------------------
# News urgent vs. deterministic economic-calendar blackout -- Tier 3.28
# ---------------------------------------------------------------------------
#
# Fixed real reference points from app.economic_calendar's registry:
# the 2026-08-12 CPI release is at 2026-08-12T12:30:00Z. "Inside" below
# is 30 minutes after that (well within the default 2-hour window);
# "far" is the same calendar day but 7.5 hours later, which is not
# within 2 hours of that or any other registry event.
_CAL_INSIDE_BLACKOUT = "2026-08-12T13:00:00Z"
_CAL_FAR_FROM_EVENTS = "2026-08-12T20:00:00Z"


def _cal_candidate(news_direction, news_confidence, news_flags, bar_timestamp, with_bar=True):
    """A weak-confidence, news-only candidate (score stays well under
    DECISION_THRESHOLD -> "no_trade", never directional) so these
    quadrant/cross-tab tests never trigger compute_outcome_for_candidate
    -- which would otherwise hit real storage -- keeping them as fast
    and isolated as compute_news_urgent_prevalence/_decomposition's own
    tests above. with_bar=False omits the "bar" key entirely, to test
    the skip-on-missing-timestamp path."""
    candidate = _candidate(news=_flagged_opinion(news_direction, news_confidence, news_flags))
    assert candidate["decision"]["decision"] not in ("enter_long", "enter_short")
    if with_bar:
        candidate["bar"] = {"timestamp": bar_timestamp}
    return candidate


def test_calendar_blackout_both_flagged():
    candidate = _cal_candidate("bullish", 10, ["urgent"], _CAL_INSIDE_BLACKOUT)
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    assert result["news_present_candidates"] == 1
    case = result["cases"][0]
    assert case["quadrant"] == "both_flagged"
    assert case["news_urgent"] is True
    assert case["calendar_blackout"] is True
    assert case["nearest_event"] == {"event": "CPI", "date": "2026-08-12", "timestamp_utc": "2026-08-12T12:30:00Z"}
    assert result["cross_tab"] == {"both_flagged": 1}


def test_calendar_blackout_news_urgent_only():
    candidate = _cal_candidate("bullish", 10, ["urgent"], _CAL_FAR_FROM_EVENTS)
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    case = result["cases"][0]
    assert case["quadrant"] == "news_urgent_only"
    assert case["news_urgent"] is True
    assert case["calendar_blackout"] is False


def test_calendar_blackout_calendar_only():
    candidate = _cal_candidate("bullish", 10, [], _CAL_INSIDE_BLACKOUT)
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    case = result["cases"][0]
    assert case["quadrant"] == "calendar_blackout_only"
    assert case["news_urgent"] is False
    assert case["calendar_blackout"] is True


def test_calendar_blackout_neither_flagged():
    candidate = _cal_candidate("bullish", 10, [], _CAL_FAR_FROM_EVENTS)
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    case = result["cases"][0]
    assert case["quadrant"] == "neither_flagged"
    assert case["news_urgent"] is False
    assert case["calendar_blackout"] is False


def test_calendar_blackout_ignores_candidates_without_news():
    candidate = _candidate(analysis=_opinion("bullish", 50))  # no news opinion at all
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    assert result["news_present_candidates"] == 0
    assert result["cases"] == []
    assert result["cross_tab"] == {}
    assert result["agreement_rate"] is None


def test_calendar_blackout_ignores_candidates_without_bar_timestamp():
    candidate = _cal_candidate("bullish", 10, ["urgent"], bar_timestamp=None, with_bar=False)
    assert "bar" not in candidate
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    assert result["news_present_candidates"] == 0
    assert result["cases"] == []


def test_calendar_blackout_agreement_rate_and_cross_tab():
    candidates = [
        _cal_candidate("bullish", 10, ["urgent"], _CAL_INSIDE_BLACKOUT),   # both_flagged
        _cal_candidate("bullish", 10, ["urgent"], _CAL_INSIDE_BLACKOUT),   # both_flagged
        _cal_candidate("bullish", 10, ["urgent"], _CAL_FAR_FROM_EVENTS),   # news_urgent_only
        _cal_candidate("bullish", 10, [], _CAL_FAR_FROM_EVENTS),           # neither_flagged
    ]
    result = compute_news_urgent_vs_calendar_blackout(candidates)
    assert result["cross_tab"] == {"both_flagged": 2, "news_urgent_only": 1, "neither_flagged": 1}
    assert result["agreement_rate"] == 0.75  # (2 both_flagged + 1 neither_flagged) / 4


def test_calendar_blackout_coverage_reports_overlapping_events():
    candidates = [
        _cal_candidate("bullish", 10, [], _CAL_INSIDE_BLACKOUT),
        _cal_candidate("bullish", 10, [], _CAL_FAR_FROM_EVENTS),
    ]
    result = compute_news_urgent_vs_calendar_blackout(candidates)
    assert result["data_range"] == {"start": _CAL_INSIDE_BLACKOUT, "end": _CAL_FAR_FROM_EVENTS}
    # min()/max() on ISO8601 "Z" strings is chronological because the
    # format is fixed-width -- "13:00" < "20:00" lexicographically too,
    # so start/end land correctly without parsing back to datetimes.
    assert result["calendar_coverage"]["event_count"] == 1
    assert result["calendar_coverage"]["events_overlapping_data_range"][0]["date"] == "2026-08-12"


def test_calendar_blackout_coverage_empty_when_no_event_nearby():
    candidates = [_cal_candidate("bullish", 10, [], _CAL_FAR_FROM_EVENTS)]
    result = compute_news_urgent_vs_calendar_blackout(candidates)
    assert result["calendar_coverage"] == {"events_overlapping_data_range": [], "event_count": 0}


def test_calendar_blackout_hypothetical_outcome_directional_decision(fresh_env):
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)
    horizon_time = decision_time + timedelta(minutes=15)
    _dd_save_bar(storage, "TEST", "5m", decision_time, 100.0)
    _dd_save_bar(storage, "TEST", "5m", horizon_time, 105.0)

    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 80, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 50, _dd_iso(decision_time), flags=["urgent"]),
        macro=_dd_opinion("neutral", 50, _dd_iso(decision_time)),
    )
    assert candidate["decision"]["decision"] == "enter_long"
    candidate["bar"] = {"timestamp": _CAL_FAR_FROM_EVENTS}

    result = coordinator_diagnostics.compute_news_urgent_vs_calendar_blackout([candidate], horizons=[15])
    assert result["news_present_candidates"] == 1
    case = result["cases"][0]
    assert case["quadrant"] == "news_urgent_only"
    assert case["outcome"]["kind"] == "hypothetical"
    assert case["outcome"]["by_horizon"][15] == "correct"  # bullish call, price rose
    assert result["outcomes_by_quadrant"]["news_urgent_only"]["hypothetical_by_horizon"][15] == {"correct": 1}


def test_calendar_blackout_real_trade_outcome(fresh_env):
    storage, coordinator, outcomes, replay, coordinator_diagnostics = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(hours=2)

    candidate = _dd_candidate(
        coordinator, "c1", "TEST", "5m", _dd_iso(decision_time),
        analysis=_dd_opinion("bullish", 80, _dd_iso(decision_time)),
        news=_dd_opinion("bullish", 50, _dd_iso(decision_time)),  # not urgent
        macro=_dd_opinion("neutral", 50, _dd_iso(decision_time)),
    )
    assert candidate["decision"]["decision"] == "enter_long"
    candidate["bar"] = {"timestamp": _CAL_INSIDE_BLACKOUT}

    trade = _dd_trade("c1", "TEST", "5m", "bullish", 20020.0, _dd_iso(decision_time))
    storage.save_paper_trade(trade)
    storage.close_trade(
        trade["trade_id"], exit_price=20100.0, exit_reason="target_hit",
        pnl_usd=160.0, closed_at=_dd_iso(datetime.now(timezone.utc)),
    )

    result = coordinator_diagnostics.compute_news_urgent_vs_calendar_blackout([candidate])
    case = result["cases"][0]
    assert case["quadrant"] == "calendar_blackout_only"
    assert case["outcome"] == {"kind": "real_trade", "status": "closed", "outcome": "win", "pnl_usd": 160.0}
    assert result["outcomes_by_quadrant"]["calendar_blackout_only"]["real_trade"] == {"win": 1}


# ---------------------------------------------------------------------------
# Opinion-level, day-blocked re-aggregation -- Tier 3.29
# ---------------------------------------------------------------------------

def test_opinion_level_day_blocked_basic_weighting():
    # Two cases share (d1, o1) with different categories -- each
    # contributes weight 0.5 to its own category, summing to 1 total
    # for that opinion. A third case is a distinct opinion (o2) within
    # the same day, contributing its full weight of 1.
    cases = [
        {"cat": "A", "day": "d1", "op": "o1"},
        {"cat": "B", "day": "d1", "op": "o1"},
        {"cat": "A", "day": "d1", "op": "o2"},
    ]
    result = _opinion_level_day_blocked_summary(cases, category_field="cat", opinion_field="op", day_field="day")
    assert result["days_considered"] == 1
    assert result["distinct_opinions_total"] == 2
    assert result["uncategorized_count"] == 0
    assert result["candidate_level_totals"] == {"A": 2, "B": 1}
    assert result["opinion_weighted_totals"] == {"A": 1.5, "B": 0.5}
    day = result["by_day"]["d1"]
    assert day["candidates_considered"] == 3
    assert day["distinct_opinions"] == 2
    assert day["category_counts_candidate_level"] == {"A": 2, "B": 1}
    assert day["category_counts_opinion_weighted"] == {"A": 1.5, "B": 0.5}


def test_opinion_level_day_blocked_splits_by_day():
    cases = [
        {"cat": "X", "day": "d1", "op": "o1"},
        {"cat": "Y", "day": "d2", "op": "o2"},
    ]
    result = _opinion_level_day_blocked_summary(cases, category_field="cat", opinion_field="op", day_field="day")
    assert result["days_considered"] == 2
    assert result["distinct_opinions_total"] == 2
    assert set(result["by_day"].keys()) == {"d1", "d2"}
    assert result["by_day"]["d1"]["category_counts_opinion_weighted"] == {"X": 1.0}
    assert result["by_day"]["d2"]["category_counts_opinion_weighted"] == {"Y": 1.0}
    assert result["opinion_weighted_totals"] == {"X": 1.0, "Y": 1.0}


def test_opinion_level_day_blocked_same_opinion_spanning_two_days():
    # An opinion reused right at a trading-day boundary genuinely
    # appears in both days -- distinct_opinions_total counts it ONCE
    # globally (1), even though it contributes to two separate by_day
    # entries (each with its own distinct_opinions=1) and a combined
    # per-day distinct-count sum of 2. Both numbers are correct, not a
    # contradiction: the module docstring calls this out explicitly.
    cases = [
        {"cat": "X", "day": "d1", "op": "o_shared"},
        {"cat": "Y", "day": "d2", "op": "o_shared"},
    ]
    result = _opinion_level_day_blocked_summary(cases, category_field="cat", opinion_field="op", day_field="day")
    assert result["distinct_opinions_total"] == 1
    assert result["by_day"]["d1"]["distinct_opinions"] == 1
    assert result["by_day"]["d2"]["distinct_opinions"] == 1
    assert result["opinion_weighted_totals"] == {"X": 1.0, "Y": 1.0}


def test_opinion_level_day_blocked_uncategorized_cases():
    cases = [
        {"cat": "X", "day": "d1", "op": "o1"},       # counted
        {"cat": "Y", "day": None, "op": "o2"},        # missing day
        {"cat": "Z", "day": "d1", "op": None},        # missing opinion
        {"cat": None, "day": "d1", "op": "o3"},       # missing category
    ]
    result = _opinion_level_day_blocked_summary(cases, category_field="cat", opinion_field="op", day_field="day")
    assert result["uncategorized_count"] == 3
    assert result["candidate_level_totals"] == {"X": 1}
    assert result["distinct_opinions_total"] == 1
    assert result["by_day"]["d1"]["candidates_considered"] == 1


def test_opinion_level_day_blocked_rounds_fractional_weights():
    cases = [
        {"cat": "A", "day": "d1", "op": "o1"},
        {"cat": "B", "day": "d1", "op": "o1"},
        {"cat": "C", "day": "d1", "op": "o1"},
    ]
    result = _opinion_level_day_blocked_summary(cases, category_field="cat", opinion_field="op", day_field="day")
    assert result["opinion_weighted_totals"] == {"A": 0.333, "B": 0.333, "C": 0.333}
    assert result["by_day"]["d1"]["category_counts_opinion_weighted"] == {"A": 0.333, "B": 0.333, "C": 0.333}


def test_opinion_level_day_blocked_empty_cases():
    result = _opinion_level_day_blocked_summary([], category_field="cat", opinion_field="op", day_field="day")
    assert result == {
        "days_considered": 0,
        "distinct_opinions_total": 0,
        "uncategorized_count": 0,
        "by_day": {},
        "candidate_level_totals": {},
        "opinion_weighted_totals": {},
    }


def test_deep_dive_opinion_level_day_blocked_wiring():
    # Same News-is-the-deciding-factor fixture as
    # test_ablation_reports_a_changed_decision_when_removing_the_
    # deciding_agent above -- a clean single agent_enabled_trade,
    # threshold_crossing case, now checked for the new key's wiring.
    candidate = _candidate(
        analysis=_opinion("bearish", 10),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 20),
    )
    candidate["bar"] = {"trading_date": "2026-08-16"}
    result = compute_threshold_crossing_deep_dive([candidate], agent="news")
    assert result["cases_considered"] == 1
    olb = result["opinion_level_day_blocked"]
    assert olb["days_considered"] == 1
    assert olb["distinct_opinions_total"] == 1
    assert olb["uncategorized_count"] == 0
    assert olb["candidate_level_totals"] == {"agent_enabled_trade": 1}
    assert olb["opinion_weighted_totals"] == {"agent_enabled_trade": 1.0}
    assert olb["by_day"]["2026-08-16"]["candidates_considered"] == 1


def test_deep_dive_opinion_level_day_blocked_uncategorized_without_bar():
    # No "bar" key at all (the plain _candidate() helper doesn't add
    # one) -- trading_date can't be determined, so the case is
    # uncategorized rather than silently guessed into a day.
    candidate = _candidate(
        analysis=_opinion("bearish", 10),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 20),
    )
    result = compute_threshold_crossing_deep_dive([candidate], agent="news")
    olb = result["opinion_level_day_blocked"]
    assert olb["uncategorized_count"] == 1
    assert olb["days_considered"] == 0
    assert olb["by_day"] == {}


def test_decomposition_opinion_level_day_blocked_wiring():
    candidate = _candidate(
        analysis=_opinion("bullish", 30),
        news=_flagged_opinion("bullish", 80, ["urgent"]),
        macro=_opinion("bullish", 20),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    candidate["bar"] = {"trading_date": "2026-08-16"}
    result = compute_news_urgent_decomposition([candidate])
    assert result["cases_considered"] == 1
    assert result["cases"][0]["opinion_timestamp"] == "2026-08-16T14:00:00Z"
    assert result["cases"][0]["trading_date"] == "2026-08-16"
    olb = result["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"urgent_dampen_alone": 1}
    assert olb["opinion_weighted_totals"] == {"urgent_dampen_alone": 1.0}
    assert olb["by_day"]["2026-08-16"]["distinct_opinions"] == 1


def test_calendar_blackout_opinion_level_day_blocked_wiring():
    candidate = _candidate(news=_flagged_opinion("bullish", 10, ["urgent"]))
    candidate["bar"] = {"timestamp": _CAL_INSIDE_BLACKOUT, "trading_date": "2026-08-12"}
    result = compute_news_urgent_vs_calendar_blackout([candidate])
    assert result["news_present_candidates"] == 1
    case = result["cases"][0]
    assert case["quadrant"] == "both_flagged"
    assert case["news_opinion_timestamp"] == "2026-08-16T14:00:00Z"
    assert case["trading_date"] == "2026-08-12"
    olb = result["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"both_flagged": 1}
    assert olb["opinion_weighted_totals"] == {"both_flagged": 1.0}
    assert olb["by_day"]["2026-08-12"]["candidates_considered"] == 1


# ---------------------------------------------------------------------------
# Tier 3.31: risk-filter veto attribution
# ---------------------------------------------------------------------------

def test_veto_attribution_excludes_non_directional_analysis():
    c1 = _candidate(analysis=_opinion("neutral", 50))
    c2 = _candidate()  # analysis missing entirely
    result = compute_risk_filter_veto_attribution([c1, c2])
    assert result["candidates_considered"] == 2
    assert result["analysis_not_directional_excluded"] == 2
    assert result["analysis_directional_candidates"] == 0
    assert result["summary"] == {}
    assert result["cases"] == []


def test_veto_attribution_news_urgent_veto_takes_priority():
    # Analysis and News agree bullish (would otherwise be
    # coordinator_agrees) but News is flagged urgent -- analysis_risk_
    # filtered vetoes regardless of what the real Coordinator decided.
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_opinion("bullish", 90),
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"news_urgent_veto": 1}
    assert result["cases"][0]["news_urgent"] is True
    assert result["cases"][0]["macro_risk_off"] is False


def test_veto_attribution_macro_risk_off_veto():
    candidate = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("bearish", 90, ["risk_off"]),
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"macro_risk_off_veto": 1}


def test_veto_attribution_macro_other_flags_do_not_veto():
    # conflicting_signals/stale_data are quality flags, not risk vetoes
    # -- must NOT trigger macro_risk_off_veto (Tier 3.30's scoped design).
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_opinion("bullish", 90),
        macro=_flagged_opinion("bullish", 90, ["conflicting_signals"]),
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_agrees": 1}


def test_veto_attribution_coordinator_agrees():
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_opinion("bullish", 90),
        macro=_opinion("bullish", 90),
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_agrees": 1}
    case = result["cases"][0]
    assert case["analysis_direction"] == "bullish"
    assert case["coordinator_direction"] == "bullish"


def test_veto_attribution_coordinator_opposite_direction():
    # Analysis leans bearish but only mildly confident; News/Macro
    # strongly bullish outweigh it -- same setup as
    # test_opposite_direction_category above, score=27.5 -> enter_long
    # while Analysis itself is bearish.
    candidate = _candidate(
        analysis=_opinion("bearish", 40),
        news=_opinion("bullish", 95),
        macro=_opinion("bullish", 95),
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_opposite_direction": 1}


def test_veto_attribution_coordinator_quorum_block():
    # Analysis present alone (0.4/0.8 = 50% of directional weight) --
    # below the live MIN_AVAILABLE_WEIGHT (60%) -- real Coordinator
    # decision is insufficient_data, News/Macro both missing.
    candidate = _candidate(
        analysis=_opinion("bullish", 80),
        min_available_weight=MIN_AVAILABLE_WEIGHT,
    )
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_quorum_block": 1}
    assert result["cases"][0]["coordinator_decision"] == "insufficient_data"


def _timing_candidate(analysis, news, timing_flags, min_available_weight=0.0):
    opinions = {"analysis": analysis, "news": news}
    opinions["timing"] = {
        "direction": "neutral", "confidence": 50, "timestamp": "2026-08-16T14:00:00Z",
        "key_data": {"session_label": "new_york"}, "flags": timing_flags,
    }
    decision = _score_opinions(
        symbol="TEST", timeframe="5m", opinions=opinions, missing_agents=[], stale_agents=[],
        weights=WEIGHTS, threshold=25.0, min_available_weight=min_available_weight,
    )
    return {"candidate_id": f"test-candidate-{next(_candidate_ids)}", "symbol": "TEST", "timeframe": "5m",
            "decision": decision.to_dict()}


def test_veto_attribution_timing_market_closed_block():
    # Quorum is fine (analysis+news = 0.65/0.8 = 81.25%), but Timing's
    # market_closed flag forces the real Coordinator's score to zero.
    candidate = _timing_candidate(_opinion("bullish", 90), _opinion("bullish", 90), ["market_closed"])
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"timing_market_closed_block": 1}
    assert result["cases"][0]["coordinator_decision"] == "no_trade"


def test_veto_attribution_timing_low_liquidity_block():
    # weighted_sum = 0.4*30 + 0.25*30 = 19.5, available_weight=0.65,
    # undampened score=30.0 (> threshold 25 -- would enter_long), but
    # Timing's low_liquidity flag halves it to 15.0 (< threshold).
    candidate = _timing_candidate(_opinion("bullish", 30), _opinion("bullish", 30), ["low_liquidity"])
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"timing_low_liquidity_block": 1}
    assert result["cases"][0]["coordinator_decision"] == "no_trade"


def test_veto_attribution_score_below_threshold_directional_opposition():
    # Quorum fine, no Timing flags at all, but News's strong opposing
    # direction keeps the blended score under threshold.
    candidate = _candidate(analysis=_opinion("bullish", 30), news=_opinion("bearish", 90))
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_score_below_threshold_other": 1}
    assert result["score_below_threshold_breakdown"] == {"directional_opposition": 1}
    assert result["cases"][0]["score_below_threshold_reason"] == "directional_opposition"


def test_veto_attribution_score_below_threshold_neutral_dilution():
    # News present but neutral -- contributes 0, diluting the
    # renormalized average without opposing Analysis's direction.
    # score = 0.4*30 / 0.65 = 18.46 < 25 -> no_trade.
    candidate = _candidate(analysis=_opinion("bullish", 30), news=_opinion("neutral", 50))
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_score_below_threshold_other": 1}
    assert result["score_below_threshold_breakdown"] == {"neutral_dilution": 1}


def test_veto_attribution_score_below_threshold_agreement_low_confidence():
    # News agrees with Analysis's direction, but both confidences are
    # low enough that the blended score still doesn't cross threshold.
    # score = (0.4*20 + 0.25*20) / 0.65 = 20.0 < 25 -> no_trade.
    candidate = _candidate(analysis=_opinion("bullish", 20), news=_opinion("bullish", 20))
    result = compute_risk_filter_veto_attribution([candidate])
    assert result["summary"] == {"coordinator_score_below_threshold_other": 1}
    assert result["score_below_threshold_breakdown"] == {"agreement_low_confidence": 1}


def test_veto_attribution_flag_prevalence_and_overlap():
    # Both flags fire on the same candidate -- news_urgent_veto wins the
    # bucket (priority order matches app.backtest._direction_for_source),
    # but flag_prevalence must still show Macro's TRUE independent count
    # and the overlap, not just the bucketed count.
    both = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_flagged_opinion("bullish", 90, ["risk_off"]),
    )
    macro_only = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("bearish", 90, ["risk_off"]),
    )
    result = compute_risk_filter_veto_attribution([both, macro_only])
    assert result["summary"] == {"news_urgent_veto": 1, "macro_risk_off_veto": 1}
    assert result["flag_prevalence"] == {
        "news_urgent_total": 1, "macro_risk_off_total": 2, "both_flags_overlap": 1,
    }


def test_veto_attribution_opinion_level_day_blocked_wiring():
    candidate = _candidate(
        analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
    )
    candidate["bar"] = {"timestamp": "2026-08-16T14:05:00Z", "trading_date": "2026-08-16"}
    result = compute_risk_filter_veto_attribution([candidate])
    olb = result["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"coordinator_agrees": 1}
    assert olb["opinion_weighted_totals"] == {"coordinator_agrees": 1.0}
    assert olb["by_day"]["2026-08-16"]["distinct_opinions"] == 1


# ---------------------------------------------------------------------------
# Tier 3.34: decision-level veto transitions (ninth external review)
# ---------------------------------------------------------------------------

def test_veto_transitions_excludes_non_directional_analysis():
    c1 = _candidate(analysis=_opinion("neutral", 50))
    c2 = _candidate()  # analysis missing entirely
    result = compute_veto_decision_transitions([c1, c2])
    assert result["candidates_considered"] == 2
    assert result["analysis_not_directional_excluded"] == 2
    assert result["analysis_directional_candidates"] == 0
    assert result["transition_summary"] == {}
    assert result["cases"] == []


def test_veto_transitions_trade_would_be_vetoed():
    # Real Coordinator traded (all three agree bullish, well above
    # threshold) but News carries "urgent" -- the veto's true direct
    # decision-level kill count.
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_opinion("bullish", 90),
    )
    result = compute_veto_decision_transitions([candidate])
    assert result["transition_summary"] == {"coordinator_trade_veto_would_skip": 1}
    assert result["flag_basis_by_transition"] == {
        "coordinator_trade_veto_would_skip": {"news_urgent_only": 1},
    }
    case = result["cases"][0]
    assert case["coordinator_decision"] == "enter_long"
    assert case["news_urgent"] is True
    assert case["flag_basis"] == "news_urgent_only"


def test_veto_transitions_trade_survives_veto():
    candidate = _candidate(
        analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
    )
    result = compute_veto_decision_transitions([candidate])
    assert result["transition_summary"] == {"coordinator_trade_veto_survives": 1}
    assert result["flag_basis_by_transition"] == {
        "coordinator_trade_veto_survives": {"neither": 1},
    }


def test_veto_transitions_quorum_block_can_never_carry_a_veto_flag():
    # Structural fact specific to the LIVE WEIGHTS/MIN_AVAILABLE_WEIGHT
    # configuration (0.40/0.25/0.20/0.15, 60% floor): in this
    # analysis-directional population, Analysis is always present, so
    # insufficient_data can only occur when News AND Macro are BOTH
    # absent (Analysis alone is 0.40/0.80=50% < 60%; adding either News
    # alone -- 0.65/0.80=81% -- or Macro alone -- 0.55/0.80=69% -- already
    # clears the floor). A veto flag requires the flagged agent to be
    # PRESENT, so "coordinator_skip_veto_would_also_skip" can never carry
    # an insufficient_data skip reason here -- only "no_trade" (see the
    # test below). This is a live-config-dependent finding, not a
    # universal guarantee like Tier 3.31's Timing zero-count proof -- it
    # would need re-checking if WEIGHTS/MIN_AVAILABLE_WEIGHT ever change.
    candidate = _candidate(analysis=_opinion("bullish", 80), min_available_weight=MIN_AVAILABLE_WEIGHT)
    result = compute_veto_decision_transitions([candidate])
    assert result["cases"][0]["coordinator_decision"] == "insufficient_data"
    assert result["transition_summary"] == {"coordinator_skip_veto_irrelevant": 1}
    assert result["flag_basis_by_transition"] == {"coordinator_skip_veto_irrelevant": {"neither": 1}}
    assert result["coordinator_skip_reason_by_transition"] == {
        "coordinator_skip_veto_irrelevant": {"insufficient_data": 1},
    }


def test_veto_transitions_skip_veto_would_also_skip_below_threshold_reason():
    # Quorum is fine but the blended score doesn't cross threshold
    # (no_trade) -- Macro also carries risk_off. Redundant veto, but a
    # DIFFERENT skip reason than the quorum case above.
    candidate = _candidate(analysis=_opinion("bullish", 20), macro=_flagged_opinion("neutral", 50, ["risk_off"]))
    result = compute_veto_decision_transitions([candidate])
    assert result["transition_summary"] == {"coordinator_skip_veto_would_also_skip": 1}
    assert result["coordinator_skip_reason_by_transition"] == {
        "coordinator_skip_veto_would_also_skip": {"no_trade": 1},
    }


def test_veto_transitions_skip_veto_irrelevant():
    # Real Coordinator's score doesn't cross threshold, no veto flag
    # present at all -- the veto plays no role here either way.
    candidate = _candidate(analysis=_opinion("bullish", 20), news=_opinion("bullish", 20))
    result = compute_veto_decision_transitions([candidate])
    assert result["transition_summary"] == {"coordinator_skip_veto_irrelevant": 1}
    assert result["coordinator_skip_reason_by_transition"] == {
        "coordinator_skip_veto_irrelevant": {"no_trade": 1},
    }


def test_veto_transitions_both_flags_overlap_basis():
    candidate = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_flagged_opinion("bullish", 90, ["risk_off"]),
    )
    result = compute_veto_decision_transitions([candidate])
    assert result["transition_summary"] == {"coordinator_trade_veto_would_skip": 1}
    assert result["flag_basis_by_transition"] == {
        "coordinator_trade_veto_would_skip": {"both": 1},
    }
    case = result["cases"][0]
    assert case["news_urgent"] is True
    assert case["macro_risk_off"] is True
    assert case["flag_basis"] == "both"


def test_veto_transitions_same_population_as_risk_filter_veto_attribution():
    # Same analysis-directional exclusion precondition -- built from the
    # exact same candidate list, both endpoints must agree on how many
    # candidates are in vs. excluded.
    candidates = [
        _candidate(analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90)),
        _candidate(analysis=_opinion("neutral", 50)),
        _candidate(analysis=_opinion("bearish", 90), news=_flagged_opinion("bearish", 90, ["urgent"])),
    ]
    attribution = compute_risk_filter_veto_attribution(candidates)
    transitions = compute_veto_decision_transitions(candidates)
    assert transitions["candidates_considered"] == attribution["candidates_considered"]
    assert transitions["analysis_not_directional_excluded"] == attribution["analysis_not_directional_excluded"]
    assert transitions["analysis_directional_candidates"] == attribution["analysis_directional_candidates"]
    assert sum(transitions["transition_summary"].values()) == transitions["analysis_directional_candidates"]


def test_veto_transitions_opinion_level_day_blocked_wiring():
    candidate = _candidate(
        analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
    )
    candidate["bar"] = {"timestamp": "2026-08-16T14:05:00Z", "trading_date": "2026-08-16"}
    result = compute_veto_decision_transitions([candidate])
    olb = result["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"coordinator_trade_veto_survives": 1}
    assert olb["opinion_weighted_totals"] == {"coordinator_trade_veto_survives": 1.0}
    assert olb["by_day"]["2026-08-16"]["distinct_opinions"] == 1


# ---------------------------------------------------------------------------
# Tier 3.35: direction/flag-opinion breakdown (tenth external review)
# ---------------------------------------------------------------------------

def test_veto_transitions_direction_flag_basis_answers_shorts_killed_by_risk_off():
    # The reviewer's central question: how many SHORT (bearish) trades
    # would be killed by risk_off specifically? Build one bearish case
    # killed by risk_off alone and one bullish case killed by urgent
    # alone -- direction_flag_basis_by_transition must keep them
    # separate, not just pool both under the same transition.
    short_killed_by_risk_off = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("neutral", 50, ["risk_off"]),
    )
    long_killed_by_urgent = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_opinion("bullish", 90),
    )
    result = compute_veto_decision_transitions([short_killed_by_risk_off, long_killed_by_urgent])
    assert result["transition_summary"] == {"coordinator_trade_veto_would_skip": 2}
    dfb = result["direction_flag_basis_by_transition"]["coordinator_trade_veto_would_skip"]
    assert dfb["bearish"] == {"macro_risk_off_only": 1}
    assert dfb["bullish"] == {"news_urgent_only": 1}


def test_veto_transitions_news_and_macro_opinion_level_day_blocked_are_independent():
    # News's opinion is reused across two candidates (same timestamp);
    # Macro only ever appears on one of them. The News-keyed
    # re-aggregation should see 1 distinct News opinion reused twice;
    # the Macro-keyed one should see only 1 case total (the other
    # excluded automatically since Macro didn't run on it -- no
    # macro_opinion_timestamp to key by).
    shared_news = _opinion("bullish", 90)
    c1 = _candidate(analysis=_opinion("bullish", 90), news=shared_news, macro=_opinion("bullish", 90))
    c2 = _candidate(analysis=_opinion("bullish", 85), news=shared_news)  # macro absent this time
    c1["bar"] = {"timestamp": "2026-08-16T14:00:00Z", "trading_date": "2026-08-16"}
    c2["bar"] = {"timestamp": "2026-08-16T14:05:00Z", "trading_date": "2026-08-16"}
    result = compute_veto_decision_transitions([c1, c2])

    news_olb = result["news_opinion_level_day_blocked"]
    assert news_olb["distinct_opinions_total"] == 1
    assert news_olb["by_day"]["2026-08-16"]["candidates_considered"] == 2

    macro_olb = result["macro_opinion_level_day_blocked"]
    assert macro_olb["distinct_opinions_total"] == 1
    assert macro_olb["by_day"]["2026-08-16"]["candidates_considered"] == 1
    assert macro_olb["uncategorized_count"] == 1  # c2, Macro didn't run


def test_veto_transitions_cases_carry_direction_and_session_fields():
    candidate = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("neutral", 50, ["risk_off"]),
    )
    candidate["bar"] = {
        "timestamp": "2026-08-16T14:00:00Z", "trading_date": "2026-08-16", "session_name": "OVERNIGHT",
    }
    result = compute_veto_decision_transitions([candidate])
    case = result["cases"][0]
    assert case["coordinator_direction"] == "bearish"
    assert case["session_name"] == "OVERNIGHT"
    assert case["news_opinion_timestamp"] == "2026-08-16T14:00:00Z"
    assert case["macro_opinion_timestamp"] == "2026-08-16T14:00:00Z"


# ---------------------------------------------------------------------------
# Tier 3.36: macro/news direction fields + risk_off direction crosstab +
# opinion-diversity aggregation (eleventh external review, items #2/#3)
# ---------------------------------------------------------------------------

def _flagged_opinion_at(direction, confidence, flags, timestamp):
    return {"direction": direction, "confidence": confidence, "timestamp": timestamp, "flags": flags}


def test_veto_transitions_cases_carry_macro_and_news_direction_fields():
    # macro_direction/news_direction mirror each agent's own directional
    # opinion; None when that agent didn't run at all (distinct from
    # "ran but was neutral").
    candidate = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
    )
    result = compute_veto_decision_transitions([candidate])
    case = result["cases"][0]
    assert case["news_direction"] == "bearish"
    assert case["macro_direction"] is None  # Macro never ran on this candidate


def test_veto_transitions_macro_risk_off_direction_crosstab():
    # Three risk_off-flagged Macro cases: one where Macro itself read
    # bearish (the endogenous, expected case), one where Macro read
    # bullish, and one where Macro read neutral -- yet all three still
    # produced a bearish or bullish Coordinator decision. The crosstab
    # must key strictly off macro_direction, separate from
    # coordinator_direction, and must NOT include the non-risk_off case.
    macro_bearish_coordinator_bearish = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("bearish", 90, ["risk_off"]),
    )
    macro_bullish_coordinator_bullish = _candidate(
        analysis=_opinion("bullish", 90),
        news=_opinion("bullish", 90),
        macro=_flagged_opinion("bullish", 90, ["risk_off"]),
    )
    macro_neutral_coordinator_bearish = _candidate(
        analysis=_opinion("bearish", 90),
        news=_opinion("bearish", 90),
        macro=_flagged_opinion("neutral", 50, ["risk_off"]),
    )
    not_risk_off_at_all = _candidate(
        analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
    )
    result = compute_veto_decision_transitions([
        macro_bearish_coordinator_bearish,
        macro_bullish_coordinator_bullish,
        macro_neutral_coordinator_bearish,
        not_risk_off_at_all,
    ])
    assert result["macro_risk_off_direction_crosstab"] == {
        "bearish": {"bearish": 1},
        "bullish": {"bullish": 1},
        "neutral": {"bearish": 1},
    }


def test_veto_transitions_macro_opinion_diversity_counts_distinct_opinions_not_candidates():
    # Same Macro opinion (same timestamp) reused across two candidates,
    # a third candidate carries a genuinely distinct Macro opinion.
    # distinct_opinions must count 2, not 3 -- candidates must count 3.
    shared_macro = _flagged_opinion_at("bearish", 90, ["risk_off"], "2026-08-16T14:00:00Z")
    distinct_macro = _flagged_opinion_at("bearish", 90, ["risk_off"], "2026-08-16T15:00:00Z")
    c1 = _candidate(analysis=_opinion("bearish", 90), news=_opinion("bearish", 90), macro=shared_macro)
    c2 = _candidate(analysis=_opinion("bearish", 85), news=_opinion("bearish", 85), macro=shared_macro)
    c3 = _candidate(analysis=_opinion("bearish", 80), news=_opinion("bearish", 80), macro=distinct_macro)
    c1["bar"] = {"trading_date": "2026-08-16"}
    c2["bar"] = {"trading_date": "2026-08-16"}
    c3["bar"] = {"trading_date": "2026-08-17"}
    result = compute_veto_decision_transitions([c1, c2, c3])
    diversity = result["macro_opinion_diversity"]["coordinator_trade_veto_would_skip"]["bearish"]
    assert diversity == {"candidates": 3, "distinct_opinions": 2, "distinct_trading_days": 2}


def test_veto_transitions_news_opinion_diversity_scoped_to_urgent_only():
    # news_opinion_diversity must only include news_urgent cases -- a
    # non-urgent candidate in the same population must not leak in.
    urgent_case = _candidate(
        analysis=_opinion("bullish", 90),
        news=_flagged_opinion("bullish", 90, ["urgent"]),
        macro=_opinion("bullish", 90),
    )
    non_urgent_case = _candidate(
        analysis=_opinion("bullish", 90), news=_opinion("bullish", 90), macro=_opinion("bullish", 90),
    )
    result = compute_veto_decision_transitions([urgent_case, non_urgent_case])
    diversity = result["news_opinion_diversity"]
    assert diversity["coordinator_trade_veto_would_skip"]["bullish"] == {
        "candidates": 1, "distinct_opinions": 1, "distinct_trading_days": 0,
    }
    # non_urgent_case landed in a different transition and must not
    # appear in news_opinion_diversity at all (no news_urgent flag).
    assert "coordinator_trade_veto_survives" not in diversity
