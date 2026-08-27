"""
Unit tests for app.backtest — Tier 3.10 (ATR-barrier benchmark). No
LLM, no network. Uses a temporary SQLite file only for the forward
bars the barrier simulation walks through (get_bars_after reads real
storage); candidates themselves are plain dicts built in-test, since
run_barrier_backtest() takes a candidate list directly rather than
querying storage itself.

Run with: pytest tests/test_backtest.py -v
"""

import importlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def fresh_env(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.paper_trades as paper_trades
    importlib.reload(paper_trades)

    import app.outcomes as outcomes
    importlib.reload(outcomes)

    import app.backtest as backtest
    importlib.reload(backtest)

    yield storage, backtest

    os.unlink(tmp.name)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_bar(storage, symbol, timeframe, timestamp_dt, open_, high, low, close, atr=None):
    conn = storage.get_connection()
    payload = {
        "event_id": f"{symbol}:{timeframe}:{_iso(timestamp_dt)}",
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": _iso(timestamp_dt),
        "open": open_, "high": high, "low": low, "close": close,
        "atr": atr,
    }
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (payload["event_id"], symbol, timeframe, payload["timestamp"], json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def _candidate(candidate_id, symbol, timeframe, anchor_dt, atr, decision="enter_long",
                analysis_direction="bullish", vwap_distance=None,
                trading_date=None, session_name=None, timing_session_label=None,
                event_id=None):
    bar = {"timestamp": _iso(anchor_dt), "atr": atr, "distance_from_vwap_points": vwap_distance}
    if trading_date is not None:
        bar["trading_date"] = trading_date
    if session_name is not None:
        bar["session_name"] = session_name
    if event_id is not None:
        bar["event_id"] = event_id
    decision_dict = {
        "decision": decision,
        "timestamp": _iso(anchor_dt),
        "opinions_used": {
            "analysis": {"direction": analysis_direction, "timestamp": _iso(anchor_dt)},
        },
    }
    if timing_session_label is not None:
        decision_dict["timing_context"] = {"session_label": timing_session_label}
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "bar": bar,
        "decision": decision_dict,
    }


# ---------------------------------------------------------------------------
# compute_atr_stop_target
# ---------------------------------------------------------------------------

def test_atr_geometry_bullish_places_stop_below_and_target_above(fresh_env):
    _, backtest = fresh_env
    stop, target = backtest.compute_atr_stop_target("bullish", entry_price=100.0, atr=2.0, stop_mult=1.5, target_mult=2.5)
    assert stop == 97.0
    assert target == 105.0


def test_atr_geometry_bearish_places_stop_above_and_target_below(fresh_env):
    _, backtest = fresh_env
    stop, target = backtest.compute_atr_stop_target("bearish", entry_price=100.0, atr=2.0, stop_mult=1.5, target_mult=2.5)
    assert stop == 103.0
    assert target == 95.0


# ---------------------------------------------------------------------------
# simulate_barrier_trade
# ---------------------------------------------------------------------------

def test_simulate_target_hit_first_is_a_win(fresh_env):
    _, backtest = fresh_env
    forward = [{"timestamp": "t1", "open": 100.0, "high": 106.0, "low": 99.5, "close": 105.5}]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=97.0, target_price=105.0, forward_bars=forward,
    )
    assert result["exit_reason"] == "target_hit"
    assert result["exit_price"] == 105.0
    assert result["pnl_usd"] > 0


def test_simulate_stop_hit_first_is_a_loss(fresh_env):
    _, backtest = fresh_env
    forward = [{"timestamp": "t1", "open": 100.0, "high": 101.0, "low": 96.0, "close": 96.5}]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=97.0, target_price=105.0, forward_bars=forward,
    )
    assert result["exit_reason"] == "stop_hit"
    assert result["pnl_usd"] < 0


def test_simulate_same_bar_stop_and_target_both_touched_stop_wins(fresh_env):
    """Standing project convention (matches process_new_bar): when a
    single bar's range contains both the stop and the target, assume
    the stop was hit first — never assume the better outcome."""
    _, backtest = fresh_env
    forward = [{"timestamp": "t1", "open": 100.0, "high": 106.0, "low": 96.0, "close": 101.0}]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=97.0, target_price=105.0, forward_bars=forward,
    )
    assert result["exit_reason"] == "stop_hit"


def test_simulate_gap_through_stop_uses_the_worse_open_price(fresh_env):
    _, backtest = fresh_env
    # Bar opens already below the stop (a gap down) — realistic exit
    # is the open, not the stop price itself.
    forward = [{"timestamp": "t1", "open": 90.0, "high": 90.5, "low": 89.0, "close": 89.5}]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=97.0, target_price=105.0, forward_bars=forward,
    )
    assert result["exit_reason"] == "stop_hit"
    # exit_price = open (90.0) minus slippage, i.e. even worse than 90.0
    assert result["exit_price"] < 90.0


def test_simulate_expires_and_marks_to_last_seen_close(fresh_env):
    _, backtest = fresh_env
    forward = [
        {"timestamp": "t1", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5},
        {"timestamp": "t2", "open": 100.5, "high": 101.5, "low": 99.5, "close": 101.0},
    ]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=90.0, target_price=200.0,
        forward_bars=forward, expiry_bars=2,
    )
    assert result["exit_reason"] == "expired"
    # Tier 3.12: an expiry close-out is a market order too, so it now
    # carries the same against-the-trader slippage every other exit
    # type in this function already did (101.0 - SLIPPAGE_POINTS).
    assert result["exit_price"] == 100.75
    assert result["bars_held"] == 2


def test_simulate_no_forward_bars_is_no_data(fresh_env):
    _, backtest = fresh_env
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=97.0, target_price=105.0, forward_bars=[],
    )
    assert result["exit_reason"] == "no_data"
    assert result["pnl_usd"] is None


def test_simulate_tracks_mfe_and_mae(fresh_env):
    _, backtest = fresh_env
    forward = [
        {"timestamp": "t1", "open": 100.0, "high": 103.0, "low": 98.0, "close": 101.0},
        {"timestamp": "t2", "open": 101.0, "high": 107.0, "low": 100.0, "close": 106.0},  # target hit here (>=105)
    ]
    result = backtest.simulate_barrier_trade(
        direction="bullish", entry_price=100.0, stop_price=90.0, target_price=105.0, forward_bars=forward,
    )
    assert result["exit_reason"] == "target_hit"
    # MFE should reflect the highest favorable excursion seen across
    # bars actually walked before exit (bar 2's high of 107 -> 7 pts).
    assert result["mfe_points"] == 7.0


# ---------------------------------------------------------------------------
# run_barrier_backtest (end to end against real stored bars)
# ---------------------------------------------------------------------------

def test_run_barrier_backtest_end_to_end_wins_on_coordinator_direction(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")

    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator")
    assert summary["trades_taken"] == 1
    assert summary["wins"] == 1
    assert summary["skipped_no_direction"] == 0
    assert summary["win_rate"] == 1.0


def test_run_barrier_backtest_skips_candidate_with_no_atr(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=None, decision="enter_long")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator")
    assert summary["trades_taken"] == 0
    assert summary["skipped_no_atr"] == 1


def test_run_barrier_backtest_skips_candidate_with_no_forward_bars(fresh_env):
    _, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")
    # No bars saved after the anchor at all.
    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator")
    assert summary["trades_taken"] == 0
    assert summary["skipped_no_forward_data"] == 1


def test_run_barrier_backtest_skips_non_directional_candidate(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator")
    assert summary["trades_taken"] == 0
    assert summary["skipped_no_direction"] == 1


def test_run_barrier_backtest_non_overlapping_skips_a_second_candidate_before_the_first_resolves(fresh_env):
    storage, backtest = fresh_env
    anchor1 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    anchor2 = anchor1 + timedelta(minutes=5)  # still "inside" trade 1's life, before it resolves

    c1 = _candidate("c1", "TEST", "5m", anchor1, atr=2.0, decision="enter_long")
    c2 = _candidate("c2", "TEST", "5m", anchor2, atr=2.0, decision="enter_long")

    # Trade 1 doesn't resolve (target/stop) until well after trade 2's
    # own anchor timestamp — several quiet bars, then a big move.
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=5), open_=100.0, high=100.5, low=99.5, close=100.2)
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=10), open_=100.2, high=100.6, low=99.6, close=100.3)
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=15), open_=100.3, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([c1, c2], direction_source="coordinator", non_overlapping=True)
    assert summary["trades_taken"] == 1
    assert summary["skipped_overlapping"] == 1


def test_run_barrier_backtest_allows_overlap_when_disabled(fresh_env):
    storage, backtest = fresh_env
    anchor1 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    anchor2 = anchor1 + timedelta(minutes=5)

    c1 = _candidate("c1", "TEST", "5m", anchor1, atr=2.0, decision="enter_long")
    c2 = _candidate("c2", "TEST", "5m", anchor2, atr=2.0, decision="enter_long")

    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", anchor2 + timedelta(minutes=5), open_=105.5, high=112.0, low=105.0, close=111.5)

    summary = backtest.run_barrier_backtest([c1, c2], direction_source="coordinator", non_overlapping=False)
    assert summary["trades_taken"] == 2
    assert summary["skipped_overlapping"] == 0


def test_vwap_source_uses_bar_distance_sign(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", vwap_distance=3.5)
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="vwap")
    assert summary["trades_taken"] == 1
    assert summary["wins"] == 1  # positive vwap distance -> bullish -> this bar's rally is a win


def test_inverse_analysis_flips_analysis_direction(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # Analysis said bullish; the market actually fell -- inverse
    # (bearish) should be the one that wins here.
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=100.5, low=94.0, close=94.5)

    analysis_summary = backtest.run_barrier_backtest([candidate], direction_source="analysis")
    inverse_summary = backtest.run_barrier_backtest([candidate], direction_source="inverse_analysis")
    assert analysis_summary["losses"] == 1
    assert inverse_summary["wins"] == 1


def test_unknown_direction_source_raises(fresh_env):
    _, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0)
    with pytest.raises(ValueError):
        backtest.run_barrier_backtest([candidate], direction_source="not_a_real_source")


# ---------------------------------------------------------------------------
# analysis_risk_filtered source -- Tier 3.30
# ---------------------------------------------------------------------------

def _add_opinion(candidate, agent, direction, flags):
    """Injects a News/Macro opinion into a candidate built by the plain
    _candidate() helper above, which only sets up "analysis" -- these
    tests need News/Macro flags too, and extending the shared fixture
    risks changing what every other test in this file already asserts
    about opinions_used's exact shape."""
    candidate["decision"]["opinions_used"][agent] = {
        "direction": direction, "timestamp": candidate["bar"]["timestamp"], "flags": flags,
    }
    return candidate


def test_analysis_risk_filtered_matches_analysis_when_no_news_or_macro():
    """No News/Macro opinion at all -- an agent that never ran can't
    veto anything, so this source's direction call is identical to the
    plain "analysis" source's."""
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    direction, ts = backtest._direction_for_source("analysis_risk_filtered", candidate)
    assert direction == "bullish"
    assert ts is not None


def test_analysis_risk_filtered_vetoes_on_news_urgent(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _add_opinion(candidate, "news", "bullish", ["urgent"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="analysis_risk_filtered")
    assert summary["trades_taken"] == 0
    # The plain "analysis" source (no veto concept) still takes it.
    analysis_summary = backtest.run_barrier_backtest([candidate], direction_source="analysis")
    assert analysis_summary["trades_taken"] == 1


def test_analysis_risk_filtered_vetoes_on_macro_risk_off(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _add_opinion(candidate, "macro", "neutral", ["risk_off"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="analysis_risk_filtered")
    assert summary["trades_taken"] == 0


def test_analysis_risk_filtered_does_not_veto_on_other_news_flags(fresh_env):
    # "low_data"/"stale_data" are about data quality, not risk -- the
    # project-owner-confirmed veto scope is "urgent" only for News.
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _add_opinion(candidate, "news", "bullish", ["low_data", "stale_data"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="analysis_risk_filtered")
    assert summary["trades_taken"] == 1


def test_analysis_risk_filtered_does_not_veto_on_other_macro_flags(fresh_env):
    # "conflicting_signals"/"stale_data" are not the confirmed veto flag
    # ("risk_off" only) for Macro.
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _add_opinion(candidate, "macro", "neutral", ["conflicting_signals", "stale_data"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="analysis_risk_filtered")
    assert summary["trades_taken"] == 1


def test_analysis_risk_filtered_requires_analysis_directional():
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="neutral")
    direction, ts = backtest._direction_for_source("analysis_risk_filtered", candidate)
    assert direction is None
    assert ts is None


def test_analysis_risk_filtered_is_in_direction_sources():
    from app import backtest
    assert "analysis_risk_filtered" in backtest.DIRECTION_SOURCES


# ---------------------------------------------------------------------------
# coordinator_veto_filtered / coordinator_quorum_bypass sources -- Tier 3.33
# (exploratory 4-way factorial, eighth external review)
# ---------------------------------------------------------------------------

def test_coordinator_veto_filtered_matches_coordinator_when_no_flags():
    """No News/Macro veto flags present -- isolates nothing, so this
    source's call is identical to the real historical "coordinator"
    decision."""
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", analysis_direction="bullish")
    direction, ts = backtest._direction_for_source("coordinator_veto_filtered", candidate)
    assert direction == "bullish"
    assert ts is not None


def test_coordinator_veto_filtered_vetoes_on_news_urgent_even_though_coordinator_traded(fresh_env):
    """The real historical Coordinator entered the trade (its decision
    field says enter_long) -- but the isolated veto-filter effect
    should still block it here, same veto scope as
    analysis_risk_filtered."""
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", analysis_direction="bullish")
    _add_opinion(candidate, "news", "bullish", ["urgent"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator_veto_filtered")
    assert summary["trades_taken"] == 0
    # The plain "coordinator" source (no veto concept layered on) still takes it.
    coordinator_summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator")
    assert coordinator_summary["trades_taken"] == 1


def test_coordinator_veto_filtered_vetoes_on_macro_risk_off(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", analysis_direction="bullish")
    _add_opinion(candidate, "macro", "neutral", ["risk_off"])
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    summary = backtest.run_barrier_backtest([candidate], direction_source="coordinator_veto_filtered")
    assert summary["trades_taken"] == 0


def test_coordinator_veto_filtered_requires_a_real_trade_decision():
    """The real historical Coordinator said no_trade -- there's no
    trade for the veto filter to isolate, so this must not fabricate
    one from Analysis's opinion the way analysis_risk_filtered does."""
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    direction, ts = backtest._direction_for_source("coordinator_veto_filtered", candidate)
    assert direction is None
    assert ts is None


def _set_analysis_confidence(candidate, confidence):
    """_candidate() doesn't set a confidence field on its analysis
    opinion (0-100 scale, per app/analysis_agent.py) -- _score_opinions()
    defaults a missing confidence to 0, which would score every replay
    call in these tests as 0 regardless of direction. These
    coordinator_quorum_bypass tests re-score for real (unlike
    analysis_risk_filtered's tests above, which only ever read the
    frozen direction), so they need a real, high confidence for the
    blended score to actually cross COORDINATOR_THRESHOLD."""
    candidate["decision"]["opinions_used"]["analysis"]["confidence"] = confidence
    return candidate


def test_coordinator_quorum_bypass_trades_when_analysis_alone_would_fail_quorum():
    """Only Analysis (weight 0.40) is present in opinions_used -- below
    the live 0.6 MIN_AVAILABLE_WEIGHT floor, so the real historical
    Coordinator call would have been insufficient_data. The
    quorum-bypass source re-scores with min_available_weight=0.0 and
    should get a real directional call from Analysis alone."""
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _set_analysis_confidence(candidate, 80)
    direction, ts = backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert direction == "bullish"
    assert ts is not None


def test_coordinator_quorum_bypass_still_respects_timing_veto():
    """Timing's market_closed flag zeroes the blended score inside
    _score_opinions() regardless of min_available_weight -- the
    quorum-bypass override only changes the availability floor, not
    Timing's separate post-score veto, so this must still come back
    as no direction even though Analysis alone (confidence=80) would
    otherwise clear the threshold easily."""
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _set_analysis_confidence(candidate, 80)
    candidate["decision"]["opinions_used"]["timing"] = {
        "direction": "neutral", "timestamp": candidate["bar"]["timestamp"], "flags": ["market_closed"],
    }
    direction, ts = backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert direction is None
    assert ts is None


def test_coordinator_quorum_bypass_is_offline_and_does_not_mutate_live_constant():
    """The replay call passes min_available_weight=0.0 as a one-off
    hypothetical override -- app.coordinator.MIN_AVAILABLE_WEIGHT
    itself (the live value every OTHER source's real Coordinator
    decisions were made under) must be untouched by calling this
    source."""
    from app import backtest
    from app import coordinator
    before = coordinator.MIN_AVAILABLE_WEIGHT
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="no_trade", analysis_direction="bullish")
    _set_analysis_confidence(candidate, 80)
    backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert coordinator.MIN_AVAILABLE_WEIGHT == before


def test_coordinator_veto_filtered_and_quorum_bypass_are_in_direction_sources():
    from app import backtest
    assert "coordinator_veto_filtered" in backtest.DIRECTION_SOURCES
    assert "coordinator_quorum_bypass" in backtest.DIRECTION_SOURCES


# ---------------------------------------------------------------------------
# Structural invariants for coordinator_veto_filtered/coordinator_
# quorum_bypass -- Tier 3.35 (tenth external review)
# ---------------------------------------------------------------------------
#
# The reviewer's item 5: these two sources' guarantees are worth locking
# in with explicit tests rather than only checked ad hoc, since every
# future diagnostic pull leans on them holding. Uses REAL
# app.coordinator._score_opinions() (not the hand-set "decision" string
# _candidate() above uses) so the fixture's own "decision" field is
# provably consistent with what a fresh replay would independently
# recompute -- coordinator_quorum_bypass calls app.replay.replay_
# candidate() internally, which re-scores from opinions_used and ignores
# whatever "decision" string a fixture happens to have set.
#
# CAVEAT (documented per the reviewer's own point, not just in this
# comment): these invariants hold because the tests below score fixtures
# under the SAME live WEIGHTS/DECISION_THRESHOLD/MIN_AVAILABLE_WEIGHT/
# ANALYSIS_REQUIRED that replay_candidate() falls back to when no
# override is passed. A real historical candidate scored under a
# DIFFERENT config (before a past WEIGHTS/threshold change) would not
# necessarily satisfy "coordinator_quorum_bypass matches the original
# direction" under today's replay -- that's expected config drift, not a
# bug, and is exactly why replay_candidate()'s own docstring already
# frames every hypothetical override as "under TODAY's live value,"
# never the original candidate's own frozen config.

def _opinion(direction, confidence):
    return {"direction": direction, "confidence": confidence, "timestamp": "2026-08-11T14:00:00Z"}


def _live_scored_candidate(candidate_id, symbol, timeframe, anchor_dt, atr, opinions, min_available_weight=None):
    from app.coordinator import (
        ANALYSIS_REQUIRED as _LIVE_ANALYSIS_REQUIRED,
        DECISION_THRESHOLD as _LIVE_THRESHOLD,
        MIN_AVAILABLE_WEIGHT as _LIVE_MIN_AVAILABLE_WEIGHT,
        WEIGHTS as _LIVE_WEIGHTS,
        _score_opinions,
    )
    use_min_weight = min_available_weight if min_available_weight is not None else _LIVE_MIN_AVAILABLE_WEIGHT
    decision = _score_opinions(
        symbol=symbol, timeframe=timeframe, opinions=opinions, missing_agents=[], stale_agents=[],
        weights=_LIVE_WEIGHTS, threshold=_LIVE_THRESHOLD,
        min_available_weight=use_min_weight, analysis_required=_LIVE_ANALYSIS_REQUIRED,
    )
    return {
        "candidate_id": candidate_id, "symbol": symbol, "timeframe": timeframe,
        "bar": {"timestamp": _iso(anchor_dt), "atr": atr},
        "decision": decision.to_dict(),
    }


def test_invariant_veto_filtered_never_trades_when_real_coordinator_did_not():
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # Quorum-blocked: Analysis alone, well below MIN_AVAILABLE_WEIGHT.
    quorum_blocked = _live_scored_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0, opinions={"analysis": _opinion("bullish", 90)},
    )
    assert quorum_blocked["decision"]["decision"] == "insufficient_data"
    direction, ts = backtest._direction_for_source("coordinator_veto_filtered", quorum_blocked)
    assert direction is None and ts is None

    # Below-threshold: quorum fine, score too low to cross threshold.
    below_threshold = _live_scored_candidate(
        "c2", "TEST", "5m", anchor, atr=2.0,
        opinions={"analysis": _opinion("bullish", 20), "news": _opinion("bullish", 20)},
    )
    assert below_threshold["decision"]["decision"] == "no_trade"
    direction, ts = backtest._direction_for_source("coordinator_veto_filtered", below_threshold)
    assert direction is None and ts is None


def test_invariant_veto_filtered_matches_coordinator_exactly_when_no_flags():
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _live_scored_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0,
        opinions={"analysis": _opinion("bearish", 90), "news": _opinion("bearish", 90)},
    )
    assert candidate["decision"]["decision"] == "enter_short"
    assert candidate["decision"]["direction"] == "bearish"
    direction, ts = backtest._direction_for_source("coordinator_veto_filtered", candidate)
    assert direction == candidate["decision"]["direction"]
    assert ts is not None


def test_invariant_quorum_bypass_matches_real_direction_when_quorum_already_sufficient():
    # Monotonicity: when the real Coordinator already had enough quorum
    # to trade, loosening min_available_weight further to 0.0 changes
    # NOTHING about the score computation -- quorum_bypass must return
    # the exact same direction the real historical decision had.
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _live_scored_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0,
        opinions={"analysis": _opinion("bullish", 90), "news": _opinion("bullish", 90)},
    )
    assert candidate["decision"]["decision"] == "enter_long"
    direction, ts = backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert direction == candidate["decision"]["direction"] == "bullish"
    assert ts is not None


def test_invariant_quorum_bypass_stays_flat_when_real_reason_was_below_threshold_not_quorum():
    # The other half of the monotonicity property: if the real
    # Coordinator's decision was "no_trade" (quorum already sufficient,
    # score just didn't cross threshold), quorum_bypass must ALSO stay
    # flat -- lifting the availability floor cannot manufacture a trade
    # out of a score that was never close in the first place.
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _live_scored_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0,
        opinions={"analysis": _opinion("bullish", 20), "news": _opinion("bullish", 20)},
    )
    assert candidate["decision"]["decision"] == "no_trade"
    direction, ts = backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert direction is None and ts is None


def test_invariant_quorum_bypass_extra_trades_only_originate_from_insufficient_data():
    # The positive half: quorum_bypass DOES add a trade when the real
    # reason was insufficient_data (Analysis alone, confidence high
    # enough that lifting the floor lets its own score through) --
    # combined with the two tests above, this fully establishes "any
    # extra trade from quorum_bypass traces back to insufficient_data,
    # never to a genuine no_trade."
    from app import backtest
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _live_scored_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0, opinions={"analysis": _opinion("bullish", 80)},
    )
    assert candidate["decision"]["decision"] == "insufficient_data"
    direction, ts = backtest._direction_for_source("coordinator_quorum_bypass", candidate)
    assert direction == "bullish"
    assert ts is not None


# ---------------------------------------------------------------------------
# compute_backtest_comparison
# ---------------------------------------------------------------------------

def test_compute_backtest_comparison_runs_every_source_by_default(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", vwap_distance=1.0)
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_backtest_comparison([candidate])
    assert set(result["by_source"].keys()) == set(backtest.DIRECTION_SOURCES)
    assert result["config"]["candidates_considered"] == 1


def test_compute_backtest_comparison_respects_requested_sources_subset(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_backtest_comparison([candidate], sources=["coordinator", "always_bearish"])
    assert set(result["by_source"].keys()) == {"coordinator", "always_bearish"}


# ---------------------------------------------------------------------------
# split_candidates_chronologically (Tier 3.11)
# ---------------------------------------------------------------------------

def test_split_chronologically_puts_earliest_in_calibration_and_latest_in_validation(fresh_env):
    _, backtest = fresh_env
    base = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # 10 candidates, 1 minute apart, given to the function OUT OF
    # ORDER (newest-first, matching get_candidate_history's real
    # ordering) to confirm the split re-sorts chronologically itself.
    candidates = [
        _candidate(f"c{i}", "TEST", "5m", base + timedelta(minutes=i), atr=2.0)
        for i in reversed(range(10))
    ]
    calibration, validation = backtest.split_candidates_chronologically(candidates, holdout_fraction=0.3)
    assert len(calibration) == 7
    assert len(validation) == 3
    assert [c["candidate_id"] for c in calibration] == [f"c{i}" for i in range(7)]
    assert [c["candidate_id"] for c in validation] == [f"c{i}" for i in range(7, 10)]


def test_split_chronologically_drops_candidates_with_no_resolvable_anchor(fresh_env):
    _, backtest = fresh_env
    base = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    good = _candidate("c1", "TEST", "5m", base, atr=2.0)
    bad = {"candidate_id": "c2", "symbol": "TEST", "timeframe": "5m", "bar": {}, "decision": {}}
    calibration, validation = backtest.split_candidates_chronologically([good, bad], holdout_fraction=0.5)
    total = len(calibration) + len(validation)
    assert total == 1  # "bad" had no anchor timestamp anywhere and was dropped


def test_split_chronologically_rejects_out_of_range_holdout_fraction(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.split_candidates_chronologically([], holdout_fraction=0.0)
    with pytest.raises(ValueError):
        backtest.split_candidates_chronologically([], holdout_fraction=1.0)


# ---------------------------------------------------------------------------
# compute_champion_challenger_report (Tier 3.11)
# ---------------------------------------------------------------------------

def test_champion_challenger_report_shape_has_calibration_and_validation_per_source(fresh_env):
    storage, backtest = fresh_env
    base = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidates = [
        _candidate(f"c{i}", "TEST", "5m", base + timedelta(minutes=i * 20), atr=2.0, decision="enter_long")
        for i in range(6)
    ]
    for i in range(6):
        _save_bar(
            storage, "TEST", "5m", base + timedelta(minutes=i * 20 + 5),
            open_=100.0, high=106.0, low=99.5, close=105.5,
        )

    # expiry_bars=1 keeps each candidate's forward window to its own
    # single saved bar -- with the default expiry_bars=24 and only one
    # sparse bar per 20-minute-spaced candidate, get_bars_after's
    # bar-COUNT limit (not a time cap) would sweep in LATER candidates'
    # bars too and trip the Tier 3.12 boundary embargo in a way that's
    # an artifact of this sparse synthetic data, not something this
    # shape-only test is meant to exercise (see the dedicated embargo
    # tests below for that).
    report = backtest.compute_champion_challenger_report(
        candidates, champion="coordinator", challengers=["always_bullish", "inverse_analysis"],
        holdout_fraction=0.5, expiry_bars=1,
    )
    assert report["champion"] == "coordinator"
    assert set(report["challengers"]) == {"always_bullish", "inverse_analysis"}
    assert set(report["by_source"].keys()) == {"coordinator", "always_bullish", "inverse_analysis"}
    assert set(report.keys()) >= {"base_rate", "config", "champion", "challengers", "by_source"}
    assert set(report["base_rate"].keys()) == {"calibration", "validation"}
    for source_result in report["by_source"].values():
        assert set(source_result.keys()) == {"calibration", "validation"}
        assert "trades_taken" in source_result["calibration"]
        assert "trades_taken" in source_result["validation"]
    assert report["config"]["calibration_candidates"] + report["config"]["validation_candidates"] == 6


def test_champion_challenger_report_defaults_challengers_to_every_other_source(fresh_env):
    storage, backtest = fresh_env
    base = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", base, atr=2.0, decision="enter_long")
    _save_bar(storage, "TEST", "5m", base + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    report = backtest.compute_champion_challenger_report(
        [candidate], champion="coordinator", holdout_fraction=0.5,
    )
    assert set(report["challengers"]) == set(backtest.DIRECTION_SOURCES) - {"coordinator"}
    assert "coordinator" not in report["challengers"]  # champion never listed as its own challenger


def test_champion_challenger_report_rejects_unknown_champion_or_challenger(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.compute_champion_challenger_report([], champion="not_a_real_source")
    with pytest.raises(ValueError):
        backtest.compute_champion_challenger_report([], champion="coordinator", challengers=["not_a_real_source"])


# ---------------------------------------------------------------------------
# Tier 3.12: holdout-boundary embargo
# ---------------------------------------------------------------------------

def test_split_embargoes_calibration_candidate_whose_forward_window_crosses_boundary(fresh_env):
    storage, backtest = fresh_env
    t0 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    c1 = _candidate("c1", "TEST", "5m", t0, atr=2.0)
    c2 = _candidate("c2", "TEST", "5m", t0 + timedelta(minutes=10), atr=2.0)  # becomes validation's first candidate

    # c1's own 2-bar forward window (expiry_bars=2) reaches PAST c2's
    # anchor (the validation cutoff) -- t0+15min > t0+10min.
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=5), open_=100.0, high=101.0, low=99.0, close=100.5)
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=15), open_=100.5, high=101.5, low=99.5, close=101.0)

    calibration, validation = backtest.split_candidates_chronologically(
        [c1, c2], holdout_fraction=0.5, expiry_bars=2,
    )
    assert calibration == []  # c1 was embargoed -- its forward window bled into validation's period
    assert [c["candidate_id"] for c in validation] == ["c2"]


def test_split_does_not_embargo_calibration_candidate_that_resolves_before_boundary(fresh_env):
    storage, backtest = fresh_env
    t0 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    c1 = _candidate("c1", "TEST", "5m", t0, atr=2.0)
    c2 = _candidate("c2", "TEST", "5m", t0 + timedelta(minutes=30), atr=2.0)

    # c1's forward window resolves well before c2's anchor this time.
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=5), open_=100.0, high=101.0, low=99.0, close=100.5)

    calibration, validation = backtest.split_candidates_chronologically(
        [c1, c2], holdout_fraction=0.5, expiry_bars=1,
    )
    assert [c["candidate_id"] for c in calibration] == ["c1"]
    assert [c["candidate_id"] for c in validation] == ["c2"]


def test_split_without_expiry_bars_skips_the_embargo_check(fresh_env):
    storage, backtest = fresh_env
    t0 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    c1 = _candidate("c1", "TEST", "5m", t0, atr=2.0)
    c2 = _candidate("c2", "TEST", "5m", t0 + timedelta(minutes=10), atr=2.0)
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=5), open_=100.0, high=101.0, low=99.0, close=100.5)
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=15), open_=100.5, high=101.5, low=99.5, close=101.0)

    # Same data as the embargo test above, but expiry_bars omitted --
    # backward-compatible default: no embargo check runs at all.
    calibration, validation = backtest.split_candidates_chronologically([c1, c2], holdout_fraction=0.5)
    assert [c["candidate_id"] for c in calibration] == ["c1"]


def test_champion_challenger_report_reports_purged_at_boundary_count(fresh_env):
    storage, backtest = fresh_env
    t0 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    c1 = _candidate("c1", "TEST", "5m", t0, atr=2.0, decision="enter_long")
    c2 = _candidate("c2", "TEST", "5m", t0 + timedelta(minutes=10), atr=2.0, decision="enter_long")
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=5), open_=100.0, high=101.0, low=99.0, close=100.5)
    _save_bar(storage, "TEST", "5m", t0 + timedelta(minutes=15), open_=100.5, high=101.5, low=99.5, close=101.0)

    report = backtest.compute_champion_challenger_report(
        [c1, c2], champion="coordinator", challengers=[], holdout_fraction=0.5, expiry_bars=2,
    )
    assert report["config"]["purged_at_boundary"] == 1
    assert report["config"]["calibration_candidates"] == 0


# ---------------------------------------------------------------------------
# Tier 3.12: paired signal comparison
# ---------------------------------------------------------------------------

def test_paired_backtest_uses_a_shared_entry_for_every_source(fresh_env):
    """Same anchor, same ATR, same entry price for every source --
    only the direction (and therefore stop/target sign) differs. This
    bar is deliberately built so a bullish read wins cleanly (target
    hit, stop never touched) and a bearish read on the SAME bar loses
    cleanly (stop hit, target never touched)."""
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0)
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=98.0, close=105.5)

    report = backtest.run_paired_barrier_backtest([candidate], sources=["always_bullish", "always_bearish"])
    assert report["config"]["eligible_candidates"] == 1
    assert report["config"]["accepted_candidates"] == 1
    assert report["by_source"]["always_bullish"]["wins"] == 1
    assert report["by_source"]["always_bearish"]["losses"] == 1
    assert report["by_source"]["always_bullish"]["trades_taken"] == report["by_source"]["always_bearish"]["trades_taken"] == 1


def test_paired_backtest_excludes_candidates_ineligible_for_any_requested_source(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # No analysis opinion at all -- eligible for always_bullish, but
    # NOT for "analysis", so paired mode must drop it entirely rather
    # than let always_bullish trade it alone.
    candidate = {
        "candidate_id": "c1", "symbol": "TEST", "timeframe": "5m",
        "bar": {"timestamp": "2026-08-11T14:00:00Z", "atr": 2.0},
        "decision": {"decision": "no_trade", "timestamp": "2026-08-11T14:00:00Z", "opinions_used": {}},
    }
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=98.0, close=105.5)

    report = backtest.run_paired_barrier_backtest([candidate], sources=["always_bullish", "analysis"])
    assert report["config"]["eligible_candidates"] == 0
    assert report["by_source"]["always_bullish"]["trades_taken"] == 0


def test_paired_backtest_requires_at_least_one_source(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.run_paired_barrier_backtest([], sources=[])


def test_paired_backtest_rejects_unknown_source(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.run_paired_barrier_backtest([], sources=["not_a_real_source"])


# ---------------------------------------------------------------------------
# Tier 3.13: small-sample statistics (Wilson CI, median, max drawdown)
# ---------------------------------------------------------------------------

def test_wilson_score_interval_is_none_for_zero_trades(fresh_env):
    _, backtest = fresh_env
    assert backtest._wilson_score_interval(0, 0) is None


def test_wilson_score_interval_brackets_the_point_estimate(fresh_env):
    _, backtest = fresh_env
    lower, upper = backtest._wilson_score_interval(wins=5, n=10)
    assert 0.0 <= lower < 0.5 < upper <= 1.0


def test_wilson_score_interval_narrows_as_sample_size_grows(fresh_env):
    _, backtest = fresh_env
    small_low, small_high = backtest._wilson_score_interval(wins=5, n=10)
    large_low, large_high = backtest._wilson_score_interval(wins=500, n=1000)
    # Same 50% point estimate either way -- the interval around it must
    # be tighter with 100x the evidence, exactly the point of reporting
    # a CI instead of the bare win_rate at these small trade counts.
    assert (large_high - large_low) < (small_high - small_low)


def test_wilson_score_interval_is_bounded_at_the_extremes(fresh_env):
    _, backtest = fresh_env
    lower, upper = backtest._wilson_score_interval(wins=0, n=3)
    assert lower == 0.0
    assert 0.0 < upper < 1.0
    lower, upper = backtest._wilson_score_interval(wins=3, n=3)
    assert 0.0 < lower < 1.0
    assert upper == 1.0


def test_median_of_empty_list_is_none(fresh_env):
    _, backtest = fresh_env
    assert backtest._median([]) is None


def test_median_odd_length_is_the_middle_value(fresh_env):
    _, backtest = fresh_env
    assert backtest._median([10.0, -5.0, 20.0]) == 10.0


def test_median_even_length_averages_the_two_middle_values(fresh_env):
    _, backtest = fresh_env
    assert backtest._median([10.0, -5.0, 20.0, 30.0]) == 15.0


def test_max_drawdown_of_all_wins_is_zero(fresh_env):
    _, backtest = fresh_env
    assert backtest._max_drawdown([10.0, 20.0, 5.0]) == 0.0


def test_max_drawdown_computes_deepest_peak_to_trough_dip(fresh_env):
    _, backtest = fresh_env
    # Equity curve: +100, +50 (peak 150), -80 (down to 70 -- dd=80),
    # +10 (80), -120 (down to -40 -- dd=190, the deepest point measured
    # from the running peak of 150).
    dd = backtest._max_drawdown([100.0, 50.0, -80.0, 10.0, -120.0])
    assert dd == 190.0


def test_run_barrier_backtest_reports_median_drawdown_and_confidence_interval(fresh_env):
    storage, backtest = fresh_env
    anchor1 = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    anchor2 = anchor1 + timedelta(hours=1)
    c1 = _candidate("c1", "TEST", "5m", anchor1, atr=2.0, decision="enter_long")
    c2 = _candidate("c2", "TEST", "5m", anchor2, atr=2.0, decision="enter_long")

    # c1 wins (target hit), c2 loses (stop hit).
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", anchor2 + timedelta(minutes=5), open_=100.0, high=100.5, low=94.0, close=94.5)

    summary = backtest.run_barrier_backtest([c1, c2], direction_source="coordinator")
    assert summary["trades_taken"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    # A CI on a 1/2 point estimate must straddle it.
    assert summary["win_rate_ci95_low"] is not None
    assert summary["win_rate_ci95_low"] < summary["win_rate"] < summary["win_rate_ci95_high"]
    assert summary["median_pnl_usd"] is not None
    assert summary["max_drawdown_usd"] is not None
    # Internal scratch list must never leak into the public summary shape.
    assert "_pnl_sequence" not in summary


def test_paired_backtest_reports_small_sample_stats_per_source(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", analysis_direction="bullish")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.run_paired_barrier_backtest([candidate], sources=["analysis", "coordinator"])
    for source_summary in result["by_source"].values():
        assert "win_rate_ci95_low" in source_summary
        assert "median_pnl_usd" in source_summary
        assert "max_drawdown_usd" in source_summary
        assert "_pnl_sequence" not in source_summary


# ---------------------------------------------------------------------------
# Tier 3.14: pre-registered parameter sensitivity grid
# ---------------------------------------------------------------------------

def test_sensitivity_grid_runs_every_combination_in_the_default_grid(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")
    # Plenty of forward bars so every (stop, target, expiry) combo in
    # the default grid -- up to 24 bars out -- has data to walk.
    for i in range(1, 25):
        _save_bar(
            storage, "TEST", "5m", anchor + timedelta(minutes=5 * i),
            open_=100.0 + i * 0.1, high=100.5 + i * 0.1, low=99.5 + i * 0.1, close=100.2 + i * 0.1,
        )

    result = backtest.run_sensitivity_grid([candidate], sources=["coordinator"])
    # Default grid: 3 stop mults x 3 target mults x 3 expiry values.
    assert result["grid"]["total_combinations"] == 27
    assert len(result["combinations"]) == 27
    assert set(result["sources"]) == {"coordinator"}


def test_sensitivity_grid_respects_a_custom_grid(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.run_sensitivity_grid(
        [candidate], sources=["coordinator"],
        stop_mults=(1.5,), target_mults=(2.5,), expiry_bars_list=(24,),
    )
    assert result["grid"]["total_combinations"] == 1
    combo = list(result["combinations"].values())[0]
    assert combo["stop_mult"] == 1.5
    assert combo["target_mult"] == 2.5
    assert combo["expiry_bars"] == 24
    assert combo["by_source"]["coordinator"]["trades_taken"] == 1


def test_sensitivity_grid_reports_a_robustness_summary_per_source(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.run_sensitivity_grid(
        [candidate], sources=["coordinator"],
        stop_mults=(1.0, 1.5), target_mults=(2.0,), expiry_bars_list=(24,),
    )
    robustness = result["robustness"]["coordinator"]
    assert robustness["combinations_run"] == 2
    assert "combinations_with_positive_pnl" in robustness
    assert "combinations_with_profit_factor_above_1" in robustness
    assert "median_win_rate_across_grid" in robustness
    assert "min_total_pnl_usd" in robustness
    assert "max_total_pnl_usd" in robustness


def test_sensitivity_grid_requires_at_least_one_source(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.run_sensitivity_grid([], sources=[])


def test_sensitivity_grid_rejects_unknown_source(fresh_env):
    _, backtest = fresh_env
    with pytest.raises(ValueError):
        backtest.run_sensitivity_grid([], sources=["not_a_real_source"])


# ---------------------------------------------------------------------------
# Tier 3.18: compute_day_session_breakdown + wiring into every report
# ---------------------------------------------------------------------------

def test_day_session_breakdown_counts_distinct_days_and_per_day_distribution(fresh_env):
    _, backtest = fresh_env
    day_a_1 = _candidate("c1", "TEST", "5m", datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc), atr=2.0, trading_date="2026-08-10")
    day_a_2 = _candidate("c2", "TEST", "5m", datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc), atr=2.0, trading_date="2026-08-10")
    day_b_1 = _candidate("c3", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0, trading_date="2026-08-11")

    result = backtest.compute_day_session_breakdown([day_a_1, day_a_2, day_b_1])
    assert result["candidates_considered"] == 3
    assert result["distinct_trading_days"] == 2
    assert result["candidates_per_day"] == {"min": 1, "median": 1.5, "max": 2}
    assert result["unknown_trading_date_count"] == 0


def test_day_session_breakdown_prefers_bar_trading_date_over_recomputing(fresh_env):
    # Two candidates on genuinely DIFFERENT real calendar days (Aug 11
    # and Aug 5), but sharing the SAME explicit (deliberately
    # implausible) bar.trading_date. If the stored field is correctly
    # preferred over recomputing from the anchor timestamp, they count
    # as ONE distinct trading day, not two -- proving the stored value
    # wins rather than being silently ignored/recomputed.
    _, backtest = fresh_env
    candidate_a = _candidate(
        "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2099-01-01",
    )
    candidate_b = _candidate(
        "c2", "TEST", "5m", datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2099-01-01",
    )
    result = backtest.compute_day_session_breakdown([candidate_a, candidate_b])
    assert result["distinct_trading_days"] == 1
    assert result["candidates_per_day"] == {"min": 2, "median": 2.0, "max": 2}


def test_day_session_breakdown_falls_back_to_expected_trading_date_when_bar_has_none(fresh_env):
    # No trading_date on the bar -- falls back to
    # app.trading_calendar.expected_trading_date() from the anchor
    # timestamp. 14:00Z = 10:00 NY (well before the 18:00 rollover), so
    # the expected trading day is the same calendar date.
    _, backtest = fresh_env
    candidate = _candidate("c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0)
    result = backtest.compute_day_session_breakdown([candidate])
    assert result["distinct_trading_days"] == 1
    assert result["unknown_trading_date_count"] == 0


def test_day_session_breakdown_counts_unknown_when_no_bar_and_no_resolvable_anchor(fresh_env):
    _, backtest = fresh_env
    candidate = {"candidate_id": "c1", "symbol": "TEST", "timeframe": "5m", "bar": None, "decision": {}}
    result = backtest.compute_day_session_breakdown([candidate])
    assert result["distinct_trading_days"] == 0
    assert result["unknown_trading_date_count"] == 1


def test_day_session_breakdown_reports_session_name_and_timing_session_label(fresh_env):
    _, backtest = fresh_env
    rth = _candidate(
        "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2026-08-11", session_name="RTH", timing_session_label="new_york",
    )
    overnight = _candidate(
        "c2", "TEST", "5m", datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2026-08-11", session_name="OVERNIGHT", timing_session_label="london",
    )
    result = backtest.compute_day_session_breakdown([rth, overnight])
    assert result["by_session_name"] == {"RTH": 1, "OVERNIGHT": 1}
    assert result["by_timing_session_label"] == {"new_york": 1, "london": 1}


def test_day_session_breakdown_empty_candidates_returns_zeroed_shape(fresh_env):
    _, backtest = fresh_env
    result = backtest.compute_day_session_breakdown([])
    assert result == {
        "candidates_considered": 0,
        "distinct_trading_days": 0,
        "candidates_per_day": {"min": None, "median": None, "max": None},
        "by_session_name": {},
        "by_timing_session_label": {},
        "unknown_trading_date_count": 0,
    }


def test_compute_backtest_comparison_includes_day_session_breakdown(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", trading_date="2026-08-11")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_backtest_comparison([candidate], sources=["coordinator"])
    assert result["day_session"]["candidates_considered"] == 1
    assert result["day_session"]["distinct_trading_days"] == 1


def test_paired_backtest_includes_day_session_breakdown(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", trading_date="2026-08-11")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.run_paired_barrier_backtest([candidate], sources=["coordinator"])
    assert result["day_session"]["candidates_considered"] == 1
    assert result["day_session"]["distinct_trading_days"] == 1


def test_sensitivity_grid_includes_day_session_breakdown(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidate = _candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", trading_date="2026-08-11")
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.run_sensitivity_grid(
        [candidate], sources=["coordinator"], stop_mults=(1.5,), target_mults=(2.5,), expiry_bars_list=(24,),
    )
    assert result["day_session"]["candidates_considered"] == 1
    assert result["day_session"]["distinct_trading_days"] == 1


def test_champion_challenger_report_includes_day_session_breakdown_per_window(fresh_env):
    storage, backtest = fresh_env
    older = datetime(2026, 8, 5, 14, 0, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    candidates = [
        _candidate("c1", "TEST", "5m", older, atr=2.0, decision="enter_long", trading_date="2026-08-05"),
        _candidate("c2", "TEST", "5m", newer, atr=2.0, decision="enter_long", trading_date="2026-08-11"),
    ]
    _save_bar(storage, "TEST", "5m", older + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", newer + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    # expiry_bars=1 keeps the calibration candidate's forward walk from
    # reaching all the way to the newer candidate's own saved bar
    # (get_bars_after has no upper time bound besides `limit`, so a
    # larger expiry_bars here would pull in the validation candidate's
    # bar too and trip the boundary embargo -- not what this test is
    # checking).
    result = backtest.compute_champion_challenger_report(
        candidates, champion="coordinator", holdout_fraction=0.5, expiry_bars=1,
    )
    assert set(result["day_session"].keys()) == {"calibration", "validation"}
    assert result["day_session"]["calibration"]["candidates_considered"] == 1
    assert result["day_session"]["validation"]["candidates_considered"] == 1


# ---------------------------------------------------------------------------
# Tier 3.19: compute_trading_date_integrity_report (fourth external review,
# 2026-08-18) -- cross-checks payload trading_date against a recomputed one
# and a third, fully independent plain-UTC-calendar-date view.
# ---------------------------------------------------------------------------

def test_trading_date_integrity_reports_no_mismatch_when_payload_matches_computed(fresh_env):
    _, backtest = fresh_env
    # 14:00Z = 10:00 NY (well before the 18:00 rollover hour), so the
    # expected trading day is the same calendar date as the payload's.
    candidate = _candidate(
        "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2026-08-11", event_id="evt-1",
    )
    result = backtest.compute_trading_date_integrity_report([candidate])
    assert result["candidates_considered"] == 1
    assert result["mismatch_count"] == 0
    assert result["mismatch_examples"] == []
    assert result["payload_trading_dates"] == {"2026-08-11": 1}
    assert result["computed_trading_dates"] == {"2026-08-11": 1}
    assert result["utc_calendar_dates"] == {"2026-08-11": 1}
    assert result["distinct_payload_trading_days"] == 1
    assert result["distinct_computed_trading_days"] == 1
    assert result["distinct_utc_calendar_dates"] == 1


def test_trading_date_integrity_detects_mismatch_and_reports_example(fresh_env):
    _, backtest = fresh_env
    # Deliberately implausible payload trading_date -- the anchor
    # timestamp implies 2026-08-11, but the payload claims 2099-01-01.
    candidate = _candidate(
        "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2099-01-01", event_id="evt-mismatch",
    )
    result = backtest.compute_trading_date_integrity_report([candidate])
    assert result["mismatch_count"] == 1
    assert result["mismatch_examples"] == [
        {
            "candidate_id": "c1",
            "event_id": "evt-mismatch",
            "anchor_timestamp": "2026-08-11T14:00:00Z",
            "payload_trading_date": "2099-01-01",
            "computed_trading_date": "2026-08-11",
        }
    ]
    assert result["mismatch_examples_truncated"] is False
    assert result["payload_trading_dates"] == {"2099-01-01": 1}
    assert result["computed_trading_dates"] == {"2026-08-11": 1}


def test_trading_date_integrity_utc_calendar_view_is_independent_of_rollover(fresh_env):
    # 23:00Z = 19:00 EDT (Aug is daylight time, UTC-4) -- at/after the
    # 18:00 NY rollover hour, so expected_trading_date() rolls this to
    # the NEXT calendar day, while the plain UTC-calendar-date view
    # (no rollover logic at all) stays on the timestamp's own date.
    # This is the exact "third independent view" the report exists to
    # provide -- these two are EXPECTED to differ here, that's not a
    # bug in the report.
    _, backtest = fresh_env
    candidate = _candidate(
        "c1", "TEST", "5m", datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc), atr=2.0,
        trading_date="2026-08-12",  # matches the rollover-aware computed date, not UTC date
    )
    result = backtest.compute_trading_date_integrity_report([candidate])
    assert result["payload_trading_dates"] == {"2026-08-12": 1}
    assert result["computed_trading_dates"] == {"2026-08-12": 1}
    assert result["utc_calendar_dates"] == {"2026-08-11": 1}
    assert result["mismatch_count"] == 0  # payload agrees with the rollover-aware computed date


def test_trading_date_integrity_counts_missing_bar_and_missing_trading_date_field(fresh_env):
    _, backtest = fresh_env
    no_bar = {"candidate_id": "c1", "symbol": "TEST", "timeframe": "5m", "bar": None, "decision": {}}
    bar_no_date = _candidate("c2", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0)
    result = backtest.compute_trading_date_integrity_report([no_bar, bar_no_date])
    assert result["candidates_missing_bar"] == 1
    assert result["candidates_bar_missing_trading_date"] == 1
    assert result["payload_trading_dates"] == {}
    # bar_no_date still has a resolvable anchor timestamp, so it still
    # contributes to the recomputed/UTC views even with no payload value.
    assert result["computed_trading_dates"] == {"2026-08-11": 1}
    assert result["utc_calendar_dates"] == {"2026-08-11": 1}


def test_trading_date_integrity_reports_earliest_and_latest_anchor_timestamp(fresh_env):
    _, backtest = fresh_env
    older = _candidate("c1", "TEST", "5m", datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc), atr=2.0, trading_date="2026-08-05")
    newer = _candidate("c2", "TEST", "5m", datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc), atr=2.0, trading_date="2026-08-11")
    result = backtest.compute_trading_date_integrity_report([older, newer])
    assert result["earliest_anchor_timestamp"] == "2026-08-05T14:00:00Z"
    assert result["latest_anchor_timestamp"] == "2026-08-11T09:00:00Z"


def test_trading_date_integrity_caps_mismatch_examples_but_not_mismatch_count(fresh_env):
    _, backtest = fresh_env
    candidates = [
        _candidate(
            f"c{i}", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), atr=2.0,
            trading_date="2099-01-01", event_id=f"evt-{i}",
        )
        for i in range(25)
    ]
    result = backtest.compute_trading_date_integrity_report(candidates)
    assert result["mismatch_count"] == 25
    assert len(result["mismatch_examples"]) == backtest.TRADING_DATE_MISMATCH_EXAMPLE_LIMIT
    assert result["mismatch_examples_truncated"] is True


def test_trading_date_integrity_empty_candidates_returns_zeroed_shape(fresh_env):
    _, backtest = fresh_env
    result = backtest.compute_trading_date_integrity_report([])
    assert result == {
        "candidates_considered": 0,
        "candidates_missing_bar": 0,
        "candidates_bar_missing_trading_date": 0,
        "payload_trading_dates": {},
        "computed_trading_dates": {},
        "utc_calendar_dates": {},
        "distinct_payload_trading_days": 0,
        "distinct_computed_trading_days": 0,
        "distinct_utc_calendar_dates": 0,
        "mismatch_count": 0,
        "mismatch_examples": [],
        "mismatch_examples_truncated": False,
        "earliest_anchor_timestamp": None,
        "latest_anchor_timestamp": None,
    }


# ---------------------------------------------------------------------------
# Tier 3.39: factorial incremental P&L (thirteenth external review)
# ---------------------------------------------------------------------------

def _veto_candidate(
    candidate_id, symbol, timeframe, anchor_dt, atr, decision, direction,
    news_urgent=False, macro_risk_off=False, macro_direction=None,
    news_opinion_ts=None, macro_opinion_ts=None, trading_date=None, session_name=None,
):
    """Like _candidate() above, but carries News/Macro opinions with
    flags/direction so Tier 3.39's compute_veto_incremental_pnl() has
    something to key its policies/breakdowns off of. news_opinion_ts/
    macro_opinion_ts default to the candidate's own anchor timestamp
    (a fresh opinion) unless overridden to simulate a REUSED opinion
    shared across multiple candidates."""
    anchor_iso = _iso(anchor_dt)
    bar = {"timestamp": anchor_iso, "atr": atr}
    if trading_date is not None:
        bar["trading_date"] = trading_date
    if session_name is not None:
        bar["session_name"] = session_name
    opinions_used = {
        "analysis": {"direction": direction, "timestamp": anchor_iso},
        "news": {
            "direction": "neutral", "timestamp": news_opinion_ts or anchor_iso,
            "flags": ["urgent"] if news_urgent else [],
        },
        "macro": {
            "direction": macro_direction or "neutral", "timestamp": macro_opinion_ts or anchor_iso,
            "flags": ["risk_off"] if macro_risk_off else [],
        },
    }
    return {
        "candidate_id": candidate_id, "symbol": symbol, "timeframe": timeframe,
        "bar": bar,
        "decision": {"decision": decision, "timestamp": anchor_iso, "opinions_used": opinions_used},
    }


def test_veto_pnl_population_excludes_non_directional_and_non_traded(fresh_env):
    _, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    traded = _veto_candidate("c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish")
    no_trade = _veto_candidate("c2", "TEST", "5m", anchor, atr=2.0, decision="no_trade", direction="bullish")
    neutral_analysis = _veto_candidate("c3", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="neutral")

    population = backtest._veto_pnl_population([traded, no_trade, neutral_analysis])
    assert [c["candidate_id"] for c in population] == ["c1"]


def test_veto_pnl_four_policies_partition_correctly(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    clean = _veto_candidate("clean", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish")
    urgent_only = _veto_candidate(
        "urgent", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish", news_urgent=True,
    )
    risk_off_only = _veto_candidate(
        "risk_off", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish", macro_risk_off=True,
    )
    both_flags = _veto_candidate(
        "both", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([clean, urgent_only, risk_off_only, both_flags])
    assert result["population"]["coordinator_traded_population"] == 4
    assert result["population"]["short"] == 2
    assert result["population"]["long"] == 2

    # "none" includes everyone; "both" only the flag-free candidate.
    assert result["decision_level"]["none"]["overall"]["candidates_in_subset"] == 4
    assert result["decision_level"]["both"]["overall"]["candidates_in_subset"] == 1
    # "urgent_only" drops the urgent-flagged pair (urgent, both) -> 2 remain.
    assert result["decision_level"]["urgent_only"]["overall"]["candidates_in_subset"] == 2
    # "risk_off_only" drops the risk_off-flagged pair (risk_off, both) -> 2 remain.
    assert result["decision_level"]["risk_off_only"]["overall"]["candidates_in_subset"] == 2


def test_veto_pnl_attribution_solo_plus_overlap_equals_union(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    clean = _veto_candidate("clean", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish")
    urgent_only = _veto_candidate(
        "urgent", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish", news_urgent=True,
    )
    risk_off_only = _veto_candidate(
        "risk_off", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish", macro_risk_off=True,
    )
    both_flags = _veto_candidate(
        "both", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([clean, urgent_only, risk_off_only, both_flags])
    attribution = result["attribution"]
    solo_urgent = attribution["urgent_solo_excluded"]["decision_level"]["candidates_in_subset"]
    solo_risk_off = attribution["risk_off_solo_excluded"]["decision_level"]["candidates_in_subset"]
    overlap = attribution["both_excluded_overlap"]["decision_level"]["candidates_in_subset"]
    union = attribution["any_excluded_union"]["decision_level"]["candidates_in_subset"]
    assert (solo_urgent, solo_risk_off, overlap) == (1, 1, 1)
    assert solo_urgent + solo_risk_off + overlap == union == 3


def test_veto_pnl_portfolio_level_respects_non_overlap_but_decision_level_does_not(fresh_env):
    storage, backtest = fresh_env
    anchor1 = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    anchor2 = anchor1 + timedelta(minutes=5)  # inside trade 1's life, before it resolves

    c1 = _veto_candidate("c1", "TEST", "5m", anchor1, atr=2.0, decision="enter_long", direction="bullish")
    c2 = _veto_candidate("c2", "TEST", "5m", anchor2, atr=2.0, decision="enter_long", direction="bullish")

    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=5), open_=100.0, high=100.5, low=99.5, close=100.2)
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=10), open_=100.2, high=100.6, low=99.6, close=100.3)
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=15), open_=100.3, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([c1, c2])
    # decision_level: both simulated independently, no scheduling conflict.
    assert result["decision_level"]["none"]["overall"]["trades_taken"] == 2
    # portfolio_level: c2's anchor falls before c1 resolves -> skipped.
    assert result["portfolio_level"]["none"]["overall"]["trades_taken"] == 1
    assert result["portfolio_level"]["none"]["overall"]["skipped_overlapping"] == 1


def test_veto_pnl_macro_direction_breakdown_splits_risk_off_population(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    bearish_macro = _veto_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        macro_risk_off=True, macro_direction="bearish",
    )
    neutral_macro = _veto_candidate(
        "c2", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        macro_risk_off=True, macro_direction="neutral",
    )
    not_risk_off = _veto_candidate(
        "c3", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish",
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([bearish_macro, neutral_macro, not_risk_off])
    breakdown = result["macro_direction_breakdown"]
    assert breakdown["bearish"]["candidates"] == 1
    assert breakdown["neutral"]["candidates"] == 1
    assert "bullish" not in breakdown  # not_risk_off never carried the risk_off flag


def test_veto_pnl_conservative_view_dedupes_reused_opinion(fresh_env):
    storage, backtest = fresh_env
    anchor1 = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    anchor2 = anchor1 + timedelta(minutes=20)
    shared_opinion_ts = _iso(anchor1)  # both candidates cite the SAME Macro opinion timestamp

    c1 = _veto_candidate(
        "c1", "TEST", "5m", anchor1, atr=2.0, decision="enter_short", direction="bearish",
        macro_risk_off=True, macro_opinion_ts=shared_opinion_ts, trading_date="2026-08-11",
    )
    c2 = _veto_candidate(
        "c2", "TEST", "5m", anchor2, atr=2.0, decision="enter_short", direction="bearish",
        macro_risk_off=True, macro_opinion_ts=shared_opinion_ts, trading_date="2026-08-11",
    )
    _save_bar(storage, "TEST", "5m", anchor1 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", anchor2 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([c1, c2])
    conservative = result["conservative_opinion_level"]["risk_off_excluded"]
    assert conservative["candidates_before_dedup"] == 2
    assert conservative["first_per_day_and_opinion"]["candidates_after_dedup"] == 1
    assert conservative["first_per_opinion_global"]["candidates_after_dedup"] == 1


def test_veto_pnl_diversity_fields_report_distinct_days_and_opinions(fresh_env):
    # Tier 3.40 (fourteenth external review): every summary dict must
    # report its own day/opinion diversity alongside P&L, not just a
    # candidate count -- so a reader can't mistake a handful of reused
    # judgment calls for many independent ones.
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    day1_ts = _iso(anchor)
    day2_ts = _iso(anchor + timedelta(days=1))

    c1 = _veto_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish",
        trading_date="2026-08-11", news_opinion_ts=day1_ts, macro_opinion_ts=day1_ts,
    )
    c2 = _veto_candidate(
        "c2", "TEST", "5m", anchor + timedelta(minutes=5), atr=2.0, decision="enter_long", direction="bullish",
        trading_date="2026-08-11", news_opinion_ts=day1_ts, macro_opinion_ts=day1_ts,
    )
    c3 = _veto_candidate(
        "c3", "TEST", "5m", anchor + timedelta(days=1), atr=2.0, decision="enter_long", direction="bullish",
        trading_date="2026-08-12", news_opinion_ts=day2_ts, macro_opinion_ts=day2_ts,
    )
    for dt in (anchor + timedelta(minutes=5), anchor + timedelta(minutes=10),
               anchor + timedelta(days=1, minutes=5)):
        _save_bar(storage, "TEST", "5m", dt, open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_veto_incremental_pnl([c1, c2, c3])
    overall = result["decision_level"]["none"]["overall"]
    assert overall["candidates_in_subset"] == 3
    # c1/c2 share the same trading_date and the same News/Macro opinion
    # identity (a reused opinion); c3 is a genuinely separate day/opinion.
    assert overall["distinct_trading_days"] == 2
    assert overall["distinct_news_opinions"] == 2
    assert overall["distinct_macro_opinions"] == 2
    assert overall["distinct_joint_news_macro_opinions"] == 2


def test_veto_pnl_joint_opinion_pairs_distinguishes_from_per_flag_counts(fresh_env):
    # Tier 3.40: the fourteenth review's specific ask -- "16 opinions per
    # flag" doesn't say how many distinct (news, macro) PAIRS those 16
    # actually form. Build 3 overlap candidates where the News opinion is
    # reused across two different Macro opinions, so distinct_news_
    # opinions (1) and distinct_joint_news_macro_opinions (2) must differ.
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    shared_news_ts = _iso(anchor)
    macro_ts_a = _iso(anchor)
    macro_ts_b = _iso(anchor + timedelta(minutes=30))

    c1 = _veto_candidate(
        "c1", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
        news_opinion_ts=shared_news_ts, macro_opinion_ts=macro_ts_a,
    )
    c2 = _veto_candidate(
        "c2", "TEST", "5m", anchor + timedelta(minutes=5), atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
        news_opinion_ts=shared_news_ts, macro_opinion_ts=macro_ts_a,
    )
    c3 = _veto_candidate(
        "c3", "TEST", "5m", anchor + timedelta(minutes=35), atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
        news_opinion_ts=shared_news_ts, macro_opinion_ts=macro_ts_b,
    )
    for dt in (anchor + timedelta(minutes=5), anchor + timedelta(minutes=10),
               anchor + timedelta(minutes=40)):
        _save_bar(storage, "TEST", "5m", dt, open_=100.0, high=100.5, low=94.0, close=94.5)

    result = backtest.compute_veto_incremental_pnl([c1, c2, c3])
    overlap = result["attribution"]["both_excluded_overlap"]["decision_level"]
    assert overlap["candidates_in_subset"] == 3
    # Same News opinion reused across all 3 -> only 1 distinct News opinion...
    assert overlap["distinct_news_opinions"] == 1
    # ...but 2 distinct Macro opinions (macro_ts_a shared by c1/c2, macro_ts_b for c3)...
    assert overlap["distinct_macro_opinions"] == 2
    # ...so the JOINT pair count is 2, not 1 -- the exact distinction the
    # review asked for, since a per-flag-only count would have hidden this.
    assert overlap["distinct_joint_news_macro_opinions"] == 2


def test_data_range_metadata_flags_truncation_by_comparing_against_true_total(fresh_env):
    # Tier 3.41 (fifteenth external review): compute_data_range_metadata
    # must report hit_limit_ceiling by comparing returned_count against
    # a TRUE total (total_in_storage), never by inferring from
    # returned_count == requested_limit -- the exact trap that let a
    # partial pull look plausible earlier this tier.
    _, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    c1 = _candidate("c1", "TEST", "5m", anchor, atr=2.0, trading_date="2026-08-11")
    c2 = _candidate("c2", "TEST", "5m", anchor + timedelta(days=1), atr=2.0, trading_date="2026-08-12")

    # Simulates a `limit`-truncated pull: only 2 of 5 "true" rows returned.
    meta = backtest.compute_data_range_metadata([c1, c2], total_in_storage=5, requested_limit=2)
    assert meta["total_candidates_in_storage"] == 5
    assert meta["requested_limit"] == 2
    assert meta["returned_count"] == 2
    assert meta["hit_limit_ceiling"] is True
    assert meta["distinct_trading_days_in_window"] == 2
    assert meta["earliest_candidate_timestamp"] < meta["latest_candidate_timestamp"]

    # Same 2 candidates, but total_in_storage now says nothing more
    # exists -- NOT truncated, even though returned_count == requested_limit
    # would have suggested otherwise if inferred that way.
    complete = backtest.compute_data_range_metadata([c1, c2], total_in_storage=2, requested_limit=2)
    assert complete["hit_limit_ceiling"] is False


def test_data_range_metadata_handles_empty_candidate_list(fresh_env):
    _, backtest = fresh_env
    meta = backtest.compute_data_range_metadata([], total_in_storage=0, requested_limit=300)
    assert meta["returned_count"] == 0
    assert meta["hit_limit_ceiling"] is False
    assert meta["earliest_candidate_timestamp"] is None
    assert meta["latest_candidate_timestamp"] is None
    assert meta["distinct_trading_days_in_window"] == 0


# ---------------------------------------------------------------------------
# Tier 3.42: frozen prospective 3-arm comparison (fifteenth external review)
# ---------------------------------------------------------------------------

def test_prospective_arm_included_partitions_none_solo_and_overlap():
    # none: everyone. solo_veto_only: keeps both-flagged AND neither-flagged,
    # drops only the exactly-one-flag cases. overlap_only: keeps only the
    # both-flagged case.
    from app import backtest

    cases = [
        (False, False),  # neither flag
        (True, False),   # solo urgent
        (False, True),   # solo risk_off
        (True, True),    # both (overlap)
    ]
    for news_urgent, macro_risk_off in cases:
        assert backtest._prospective_arm_included("none", news_urgent, macro_risk_off) is True

    included_solo = [backtest._prospective_arm_included("solo_veto_only", u, r) for u, r in cases]
    assert included_solo == [True, False, False, True]

    included_overlap = [backtest._prospective_arm_included("overlap_only", u, r) for u, r in cases]
    assert included_overlap == [False, False, False, True]


def test_prospective_arm_included_rejects_unknown_arm():
    from app import backtest

    with pytest.raises(ValueError):
        backtest._prospective_arm_included("not_a_real_arm", True, True)


def test_prospective_overlap_comparison_partitions_population_by_arm(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)

    clean = _veto_candidate("clean", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish")
    urgent_only = _veto_candidate(
        "urgent", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish", news_urgent=True,
    )
    risk_off_only = _veto_candidate(
        "risk_off", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish", macro_risk_off=True,
    )
    both_flags = _veto_candidate(
        "both", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
        news_urgent=True, macro_risk_off=True,
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_prospective_overlap_comparison([clean, urgent_only, risk_off_only, both_flags])
    assert result["arms"] == ["none", "solo_veto_only", "overlap_only"]
    assert result["population"]["coordinator_traded_population"] == 4

    # none: everyone.
    assert result["results"]["none"]["candidates_in_arm"] == 4
    # solo_veto_only: drops the two solo-flagged (urgent, risk_off) -> clean + both remain.
    assert result["results"]["solo_veto_only"]["candidates_in_arm"] == 2
    # overlap_only: only the both-flagged candidate.
    assert result["results"]["overlap_only"]["candidates_in_arm"] == 1

    overlap_overall = result["results"]["overlap_only"]["decision_level"]["overall"]
    assert overlap_overall["candidates_in_subset"] == 1
    assert "distinct_trading_days" in overlap_overall
    assert "distinct_joint_news_macro_opinions" in overlap_overall


def test_prospective_overlap_comparison_short_long_splits_reuse_same_arm_ids(fresh_env):
    storage, backtest = fresh_env
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    long_clean = _veto_candidate("long", "TEST", "5m", anchor, atr=2.0, decision="enter_long", direction="bullish")
    short_clean = _veto_candidate(
        "short", "TEST", "5m", anchor, atr=2.0, decision="enter_short", direction="bearish",
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    result = backtest.compute_prospective_overlap_comparison([long_clean, short_clean])
    none_arm = result["results"]["none"]
    assert none_arm["decision_level"]["overall"]["candidates_in_subset"] == 2
    assert none_arm["decision_level"]["long"]["candidates_in_subset"] == 1
    assert none_arm["decision_level"]["short"]["candidates_in_subset"] == 1
