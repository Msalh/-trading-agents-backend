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
                analysis_direction="bullish", vwap_distance=None):
    return {
        "candidate_id": candidate_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "bar": {"timestamp": _iso(anchor_dt), "atr": atr, "distance_from_vwap_points": vwap_distance},
        "decision": {
            "decision": decision,
            "timestamp": _iso(anchor_dt),
            "opinions_used": {
                "analysis": {"direction": analysis_direction, "timestamp": _iso(anchor_dt)},
            },
        },
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
    assert result["exit_price"] == 101.0
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

    report = backtest.compute_champion_challenger_report(
        candidates, champion="coordinator", challengers=["always_bullish", "inverse_analysis"], holdout_fraction=0.5,
    )
    assert report["champion"] == "coordinator"
    assert set(report["challengers"]) == {"always_bullish", "inverse_analysis"}
    assert set(report["by_source"].keys()) == {"coordinator", "always_bullish", "inverse_analysis"}
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
