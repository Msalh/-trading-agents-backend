"""
Unit tests for app.experiments -- Tier 3.20 (experiment registry,
fourth external review, 2026-08-18), hardened in Tier 3.23 (fifth
external review, 2026-08-19: locked_config actually enforced via
re-scoring, structured target_metrics, rowid-based no-peeking
boundary, no silent prospective-window truncation, geometry-drift
detection). Uses a real temporary SQLite DB (not just in-memory dicts)
since this module's entire point is filtering REAL persisted
candidates by their REAL insertion order against a REAL persisted
experiment's registered_watermark_rowid -- exactly the boundary this
module exists to enforce, so the test has to exercise real storage,
not a stand-in.

Tier 3.23 changed WHAT controls the prospective/exploratory boundary:
it's now the candidate's rowid (true insertion order into
trade_candidates) relative to register_experiment()'s call, not a
created_at timestamp comparison. Tests below place candidates before/
after registration by literally calling _save_candidate() before/after
experiments.register_experiment() in the test body -- no more backdating
hack needed for the core boundary tests. created_at backdating is kept
available (and explicitly tested) to prove the OLD Tier 3.20 bug this
tier fixed stays fixed: a candidate's created_at value must no longer
be able to smuggle it across the boundary either way.

Run with: pytest tests/test_experiments.py -v
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

    import app.backtest as backtest
    importlib.reload(backtest)

    import app.experiments as experiments
    importlib.reload(experiments)

    yield storage, backtest, experiments

    os.unlink(tmp.name)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_bar(storage, symbol, timeframe, timestamp_dt, open_, high, low, close, atr=2.0):
    conn = storage.get_connection()
    payload = {
        "event_id": f"{symbol}:{timeframe}:{_iso(timestamp_dt)}",
        "symbol": symbol, "timeframe": timeframe, "timestamp": _iso(timestamp_dt),
        "open": open_, "high": high, "low": low, "close": close, "atr": atr,
    }
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (payload["event_id"], symbol, timeframe, payload["timestamp"], json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def _save_candidate(storage, candidate_id, symbol, timeframe, anchor_dt, atr=2.0,
                     decision="enter_long", analysis_direction="bullish", created_at=None,
                     opinions_used=None):
    bar = {"timestamp": _iso(anchor_dt), "atr": atr}
    decision_dict = {
        "decision": decision, "timestamp": _iso(anchor_dt),
        "direction": "bullish" if decision == "enter_long" else ("bearish" if decision == "enter_short" else None),
        "score": 30.0 if decision == "enter_long" else (-30.0 if decision == "enter_short" else 0.0),
        "threshold": 25.0,
        "opinions_used": opinions_used or {"analysis": {"direction": analysis_direction, "timestamp": _iso(anchor_dt)}},
    }
    storage.save_candidate(candidate_id=candidate_id, symbol=symbol, timeframe=timeframe, bar=bar, decision=decision_dict)
    if created_at is not None:
        conn = storage.get_connection()
        conn.execute("UPDATE trade_candidates SET created_at = ? WHERE candidate_id = ?", (created_at, candidate_id))
        conn.commit()
        conn.close()


_LONG_AGO = "2020-01-01 00:00:00"
_FAR_FUTURE = "2099-01-01 00:00:00"

_WIN_RATE_METRICS = {"primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5}


# ---------------------------------------------------------------------------
# register_experiment
# ---------------------------------------------------------------------------

def test_register_experiment_freezes_current_live_config_and_geometry(fresh_env):
    _, _, experiments = fresh_env
    from app.coordinator import DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS
    from app.backtest import ATR_STOP_MULT, ATR_TARGET_MULT, BACKTEST_LOGIC_VERSION, EXPIRY_BARS
    from app.paper_trades import COMMISSION_PER_CONTRACT, SLIPPAGE_POINTS

    result = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="Coordinator beats a coin flip",
        target_metrics=_WIN_RATE_METRICS, stopping_rule={"min_distinct_trading_days": 2},
    )
    assert result["status"] == "active"
    assert result["resolved_at"] is None
    assert result["resolution"] is None
    assert result["direction_source"] == "coordinator"
    assert result["locked_config"] == {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
        "backtest_geometry": {
            "atr_stop_mult": ATR_STOP_MULT,
            "atr_target_mult": ATR_TARGET_MULT,
            "expiry_bars": EXPIRY_BARS,
            "non_overlapping": True,
            "slippage_points": SLIPPAGE_POINTS,
            "commission_per_contract": COMMISSION_PER_CONTRACT,
            "backtest_logic_version": BACKTEST_LOGIC_VERSION,
        },
    }
    assert result["target_metrics"] == {
        "primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5, "secondary_metrics": [],
    }


def test_register_experiment_watermark_is_current_max_rowid(fresh_env):
    storage, _, experiments = fresh_env
    _save_candidate(storage, "before-1", "TEST", "5m", datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc))
    _save_candidate(storage, "before-2", "TEST", "5m", datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc))

    result = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=_WIN_RATE_METRICS, stopping_rule={"min_distinct_trading_days": 1},
    )
    assert result["registered_watermark_rowid"] == 2  # two candidates already existed


@pytest.mark.parametrize("kwargs,expected_fragment", [
    ({"hypothesis": "   "}, "hypothesis"),
    ({"target_metrics": {}}, "target_metrics"),
    ({"target_metrics": {"primary_metric": "not_a_real_metric", "comparator": ">=", "success_threshold": 0.5}}, "primary_metric"),
    ({"target_metrics": {"primary_metric": "win_rate", "comparator": "!=", "success_threshold": 0.5}}, "comparator"),
    ({"target_metrics": {"primary_metric": "win_rate", "comparator": ">=", "success_threshold": "not a number"}}, "success_threshold"),
    ({"target_metrics": {"primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5, "secondary_metrics": ["bogus"]}}, "secondary_metrics"),
    ({"stopping_rule": {}}, "stopping_rule"),
    ({"stopping_rule": {"bogus_key": 1}}, "stopping_rule"),
    ({"direction_source": "not_a_real_source"}, "direction_source"),
])
def test_register_experiment_rejects_incomplete_or_invalid_input(fresh_env, kwargs, expected_fragment):
    _, _, experiments = fresh_env
    base = dict(
        symbol="TEST", timeframe="5m", hypothesis="some hypothesis",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_distinct_trading_days": 2},
    )
    base.update(kwargs)
    with pytest.raises(experiments.ExperimentError, match=expected_fragment):
        experiments.register_experiment(**base)


# ---------------------------------------------------------------------------
# _prospective_candidates -- the core no-peeking boundary (Tier 3.23: rowid, not created_at)
# ---------------------------------------------------------------------------

def test_prospective_candidates_excludes_pre_registration_includes_post(fresh_env):
    storage, _, experiments = fresh_env
    _save_candidate(storage, "old-1", "TEST", "5m", datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc))

    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )

    _save_candidate(storage, "new-1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    prospective = experiments._prospective_candidates(experiment)
    assert [c["candidate_id"] for c in prospective] == ["new-1"]


def test_prospective_candidates_ignore_created_at_and_key_off_insertion_order(fresh_env):
    """Regression test for the exact Tier 3.20 gap Tier 3.23 fixed: a
    candidate's created_at value must NOT be able to move it across the
    boundary -- only true insertion order (rowid) relative to
    registration decides. A candidate inserted BEFORE registration but
    with a backdated created_at claiming to be in the future must still
    be excluded; one inserted AFTER registration but backdated to claim
    the deep past must still be included."""
    storage, _, experiments = fresh_env
    _save_candidate(storage, "old-but-claims-future", "TEST", "5m",
                     datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )

    _save_candidate(storage, "new-but-claims-past", "TEST", "5m",
                     datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_LONG_AGO)

    prospective = experiments._prospective_candidates(experiment)
    assert [c["candidate_id"] for c in prospective] == ["new-but-claims-past"]


def test_prospective_candidates_scoped_to_symbol_and_timeframe(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_candidate(storage, "wrong-symbol", "OTHER", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))
    _save_candidate(storage, "right-symbol", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    prospective = experiments._prospective_candidates(experiment)
    assert [c["candidate_id"] for c in prospective] == ["right-symbol"]


def test_prospective_candidates_raises_loudly_past_the_safety_ceiling(fresh_env, monkeypatch):
    storage, _, experiments = fresh_env
    monkeypatch.setattr(experiments, "EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES", 2)
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )
    for i in range(3):
        _save_candidate(storage, f"c{i}", "TEST", "5m", datetime(2026, 8, 11 + i, 14, 0, tzinfo=timezone.utc))

    with pytest.raises(experiments.ExperimentError, match="EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES"):
        experiments._prospective_candidates(experiment)


# ---------------------------------------------------------------------------
# locked_config re-scoring (Tier 3.23, point (a))
# ---------------------------------------------------------------------------

def test_evaluate_stopping_rule_rescores_under_locked_config_not_live(fresh_env, monkeypatch):
    """The core Tier 3.23 fix: a candidate's STORED decision was
    "enter_long" under the config live when it was created. If live
    weights/threshold change AFTER registration (simulating a config
    change mid-experiment), the experiment's locked_config must still
    be what's used -- so the stopping rule's trade count must reflect
    the ORIGINAL (locked) config's call, not whatever live config would
    produce now."""
    storage, backtest, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_accepted_trades": 1},
    )
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # A candidate whose ONLY opinion is Analysis bullish -- under the
    # locked (real live-at-registration) weights this alone is
    # insufficient_data (Analysis alone can't clear MIN_AVAILABLE_WEIGHT),
    # so the STORED decision below ("enter_long") is deliberately
    # fabricated/wrong on purpose -- proving resolve/evaluate re-derive
    # the decision from opinions_used via replay rather than trusting
    # the stored "decision" field at all.
    _save_candidate(
        storage, "c1", "TEST", "5m", anchor, decision="enter_long",
        opinions_used={"analysis": {"direction": "bullish", "confidence": 90, "timestamp": _iso(anchor)}},
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    status = experiments.evaluate_stopping_rule(experiment)
    # Analysis alone is insufficient_data under real live weights -- so
    # despite the stored (fabricated) "enter_long" decision, re-scoring
    # under locked_config must find ZERO accepted trades, proving the
    # stored decision was NOT trusted.
    assert status["checks"]["min_accepted_trades"]["actual"] == 0
    assert status["stopping_rule_met"] is False


def test_rescore_is_a_noop_for_non_coordinator_direction_source(fresh_env):
    """analysis/inverse_analysis/etc never depended on Coordinator
    weights -- _rescore_under_locked_config must return candidates
    completely unchanged for those, not call replay_candidate() at all
    (which would be a wasted no-op at best)."""
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_accepted_trades": 1},
        direction_source="analysis",
    )
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    _save_candidate(storage, "c1", "TEST", "5m", anchor, decision="no_trade", analysis_direction="bullish")
    candidates = experiments._prospective_candidates(experiment)
    rescored = experiments._rescore_under_locked_config(candidates, experiment)
    assert rescored == candidates


# ---------------------------------------------------------------------------
# geometry drift detection (Tier 3.23, point (b))
# ---------------------------------------------------------------------------

def test_geometry_drift_none_when_nothing_changed(fresh_env):
    _, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    assert experiments._geometry_drift(experiment["locked_config"]) is None


def test_geometry_drift_detected_when_slippage_changes_live(fresh_env, monkeypatch):
    _, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    monkeypatch.setattr(experiments, "SLIPPAGE_POINTS", experiments.SLIPPAGE_POINTS + 10.0)
    drift = experiments._geometry_drift(experiment["locked_config"])
    assert drift is not None
    assert "slippage_points" in drift
    assert drift["slippage_points"]["live"] == experiments.SLIPPAGE_POINTS


def test_evaluate_stopping_rule_surfaces_geometry_drift(fresh_env, monkeypatch):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_WIN_RATE_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(experiments, "COMMISSION_PER_CONTRACT", experiments.COMMISSION_PER_CONTRACT + 100.0)

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["geometry_drift"] is not None
    assert "commission_per_contract" in status["geometry_drift"]


# ---------------------------------------------------------------------------
# evaluate_stopping_rule -- read-only, never mutates
# ---------------------------------------------------------------------------

def test_evaluate_stopping_rule_reports_progress_without_resolving(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 2},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["prospective_candidates_considered"] == 1
    assert status["checks"]["min_distinct_trading_days"] == {"required": 2, "actual": 1, "met": False}
    assert status["stopping_rule_met"] is False

    # side-effect-free: the persisted experiment is still active
    reloaded = experiments.get_experiment_by_id(experiment["experiment_id"])
    assert reloaded["status"] == "active"


def test_evaluate_stopping_rule_met_once_thresholds_reached(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 2},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))
    _save_candidate(storage, "c2", "TEST", "5m", datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc))

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["checks"]["min_distinct_trading_days"]["actual"] == 2
    assert status["stopping_rule_met"] is True


def test_evaluate_stopping_rule_requires_all_set_checks_to_pass(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1, "min_accepted_trades": 5},
    )
    # one candidate, no forward bars -- satisfies the day requirement
    # but not the accepted-trades requirement (0 trades resolved).
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["checks"]["min_distinct_trading_days"]["met"] is True
    assert status["checks"]["min_accepted_trades"]["met"] is False
    assert status["stopping_rule_met"] is False


# ---------------------------------------------------------------------------
# resolve_experiment -- one-time, refuses early, idempotent once resolved
# ---------------------------------------------------------------------------

def test_resolve_experiment_refuses_when_stopping_rule_not_met(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 5},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    with pytest.raises(experiments.ExperimentError, match="not yet met"):
        experiments.resolve_experiment(experiment["experiment_id"])

    # refusing to resolve must not have mutated anything
    reloaded = experiments.get_experiment_by_id(experiment["experiment_id"])
    assert reloaded["status"] == "active"
    assert reloaded["resolution"] is None


def test_resolve_experiment_raises_for_unknown_id(fresh_env):
    _, _, experiments = fresh_env
    with pytest.raises(experiments.ExperimentError, match="no experiment"):
        experiments.resolve_experiment("does-not-exist")


def test_resolve_experiment_writes_once_and_never_recomputes(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    resolved = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    assert resolved["resolution"]["resolved_from_candidates_considered"] == 1
    first_resolution = resolved["resolution"]

    # more prospective data arrives after resolution -- a second call
    # must return the SAME resolution, not recompute against the
    # now-larger candidate set.
    _save_candidate(storage, "c2", "TEST", "5m", datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc))
    resolved_again = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved_again["resolution"] == first_resolution
    assert resolved_again["resolution"]["resolved_from_candidates_considered"] == 1


def test_resolve_experiment_end_to_end_with_accepted_trades_stopping_rule(fresh_env):
    # Exercises the min_accepted_trades path with a real forward bar so
    # a trade actually resolves (target_hit), not just distinct-day
    # counting -- proves resolve_experiment's reuse of
    # compute_backtest_comparison actually runs the barrier simulation,
    # AND that a Coordinator-sufficient candidate (both Analysis and a
    # second directional-ish input) survives locked-config re-scoring.
    storage, backtest, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics={"primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.0, "secondary_metrics": ["profit_factor"]},
        stopping_rule={"min_accepted_trades": 1}, direction_source="coordinator",
    )
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    # Analysis + News both present and bullish -- clears MIN_AVAILABLE_WEIGHT
    # (0.40+0.25=0.65 >= 0.6) under the real live/locked weights, so this
    # is a genuinely coordinator-sufficient candidate, not a fabricated one.
    _save_candidate(
        storage, "c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long",
        opinions_used={
            "analysis": {"direction": "bullish", "confidence": 90, "timestamp": _iso(anchor)},
            "news": {"direction": "bullish", "confidence": 80, "timestamp": _iso(anchor)},
        },
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["checks"]["min_accepted_trades"]["actual"] == 1
    assert status["stopping_rule_met"] is True

    resolved = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved["resolution"]["backtest"]["by_source"]["coordinator"]["trades_taken"] == 1
    target_result = resolved["resolution"]["target_metrics_result"]
    assert target_result["primary_metric"] == "win_rate"
    assert target_result["met"] is True  # win_rate (1.0) >= 0.0
    assert "profit_factor" in target_result["secondary_metrics"]
    assert resolved["resolution"]["geometry_drift"] is None


def test_resolve_experiment_target_metrics_result_inconclusive_when_metric_undefined(fresh_env):
    """profit_factor is None when there's no gross_loss yet (all wins,
    or zero trades) -- met must be None (inconclusive), not False."""
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics={"primary_metric": "profit_factor", "comparator": ">=", "success_threshold": 1.0},
        stopping_rule={"min_accepted_trades": 1}, direction_source="coordinator",
    )
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    _save_candidate(
        storage, "c1", "TEST", "5m", anchor, decision="enter_long",
        opinions_used={
            "analysis": {"direction": "bullish", "confidence": 90, "timestamp": _iso(anchor)},
            "news": {"direction": "bullish", "confidence": 80, "timestamp": _iso(anchor)},
        },
    )
    # A winning trade only (target hit, no loss) -> gross_loss_usd stays
    # 0 -> profit_factor stays None (see app.backtest._finalize_summary).
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    resolved = experiments.resolve_experiment(experiment["experiment_id"])
    target_result = resolved["resolution"]["target_metrics_result"]
    assert target_result["actual"] is None
    assert target_result["met"] is None


# ---------------------------------------------------------------------------
# list_experiments
# ---------------------------------------------------------------------------

def test_list_experiments_newest_first_and_filterable(fresh_env):
    _, _, experiments = fresh_env
    experiments.register_experiment(
        symbol="AAA", timeframe="5m", hypothesis="first", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )
    second = experiments.register_experiment(
        symbol="BBB", timeframe="5m", hypothesis="second", target_metrics=dict(_WIN_RATE_METRICS),
        stopping_rule={"min_distinct_trading_days": 1},
    )

    all_experiments = experiments.list_experiments()
    assert len(all_experiments) == 2
    assert all_experiments[0]["experiment_id"] == second["experiment_id"]  # newest first

    scoped = experiments.list_experiments(symbol="BBB", timeframe="5m")
    assert [e["hypothesis"] for e in scoped] == ["second"]
