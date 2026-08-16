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
