"""
Unit tests for app.experiments -- Tier 3.20 (experiment registry,
fourth external review, 2026-08-18). Uses a real temporary SQLite DB
(not just in-memory dicts) since this module's entire point is
filtering REAL persisted candidates by their REAL created_at against a
REAL persisted experiment's registered_at -- exactly the boundary this
module exists to enforce, so the test has to exercise real storage,
not a stand-in.

created_at is backdated directly via SQL after insert (SQLite's
datetime('now') default has no per-test control otherwise) so tests
can deterministically place candidates either safely BEFORE or safely
AFTER registration, regardless of how fast the test itself runs. Each
candidate's own market-anchor timestamp (bar.timestamp / the decision
timestamp) is left alone -- that's a different, unrelated field, and
day-session/backtest logic keys off it, not off created_at.

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
                     decision="enter_long", analysis_direction="bullish", created_at=None):
    bar = {"timestamp": _iso(anchor_dt), "atr": atr}
    decision_dict = {
        "decision": decision, "timestamp": _iso(anchor_dt),
        "opinions_used": {"analysis": {"direction": analysis_direction, "timestamp": _iso(anchor_dt)}},
    }
    storage.save_candidate(candidate_id=candidate_id, symbol=symbol, timeframe=timeframe, bar=bar, decision=decision_dict)
    if created_at is not None:
        conn = storage.get_connection()
        conn.execute("UPDATE trade_candidates SET created_at = ? WHERE candidate_id = ?", (created_at, candidate_id))
        conn.commit()
        conn.close()


_LONG_AGO = "2020-01-01 00:00:00"  # unambiguously before any real registered_at
_FAR_FUTURE = "2099-01-01 00:00:00"  # unambiguously after any real registered_at


# ---------------------------------------------------------------------------
# register_experiment
# ---------------------------------------------------------------------------

def test_register_experiment_freezes_current_live_config(fresh_env):
    _, _, experiments = fresh_env
    from app.coordinator import DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS

    result = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="Coordinator beats a coin flip",
        target_metrics=["win_rate"], stopping_rule={"min_distinct_trading_days": 2},
    )
    assert result["status"] == "active"
    assert result["resolved_at"] is None
    assert result["resolution"] is None
    assert result["direction_source"] == "coordinator"
    assert result["locked_config"] == {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
    }


@pytest.mark.parametrize("kwargs,expected_fragment", [
    ({"hypothesis": "   "}, "hypothesis"),
    ({"target_metrics": []}, "target_metrics"),
    ({"stopping_rule": {}}, "stopping_rule"),
    ({"stopping_rule": {"bogus_key": 1}}, "stopping_rule"),
    ({"direction_source": "not_a_real_source"}, "direction_source"),
])
def test_register_experiment_rejects_incomplete_or_invalid_input(fresh_env, kwargs, expected_fragment):
    _, _, experiments = fresh_env
    base = dict(
        symbol="TEST", timeframe="5m", hypothesis="some hypothesis",
        target_metrics=["win_rate"], stopping_rule={"min_distinct_trading_days": 2},
    )
    base.update(kwargs)
    with pytest.raises(experiments.ExperimentError, match=expected_fragment):
        experiments.register_experiment(**base)


# ---------------------------------------------------------------------------
# _prospective_candidates -- the core no-peeking boundary
# ---------------------------------------------------------------------------

def test_prospective_candidates_excludes_pre_registration_includes_post(fresh_env):
    storage, _, experiments = fresh_env
    _save_candidate(storage, "old-1", "TEST", "5m", datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc), created_at=_LONG_AGO)

    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1},
    )

    _save_candidate(storage, "new-1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

    prospective = experiments._prospective_candidates(experiment)
    assert [c["candidate_id"] for c in prospective] == ["new-1"]


def test_prospective_candidates_scoped_to_symbol_and_timeframe(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_candidate(storage, "wrong-symbol", "OTHER", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)
    _save_candidate(storage, "right-symbol", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

    prospective = experiments._prospective_candidates(experiment)
    assert [c["candidate_id"] for c in prospective] == ["right-symbol"]


# ---------------------------------------------------------------------------
# evaluate_stopping_rule -- read-only, never mutates
# ---------------------------------------------------------------------------

def test_evaluate_stopping_rule_reports_progress_without_resolving(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 2},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

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
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 2},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)
    _save_candidate(storage, "c2", "TEST", "5m", datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["checks"]["min_distinct_trading_days"]["actual"] == 2
    assert status["stopping_rule_met"] is True


def test_evaluate_stopping_rule_requires_all_set_checks_to_pass(fresh_env):
    storage, _, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1, "min_accepted_trades": 5},
    )
    # one candidate, no forward bars -- satisfies the day requirement
    # but not the accepted-trades requirement (0 trades resolved).
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

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
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 5},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

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
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)

    resolved = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    assert resolved["resolution"]["resolved_from_candidates_considered"] == 1
    first_resolution = resolved["resolution"]

    # more prospective data arrives after resolution -- a second call
    # must return the SAME resolution, not recompute against the
    # now-larger candidate set.
    _save_candidate(storage, "c2", "TEST", "5m", datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc), created_at=_FAR_FUTURE)
    resolved_again = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved_again["resolution"] == first_resolution
    assert resolved_again["resolution"]["resolved_from_candidates_considered"] == 1


def test_resolve_experiment_end_to_end_with_accepted_trades_stopping_rule(fresh_env):
    # Exercises the min_accepted_trades path with a real forward bar so
    # a trade actually resolves (target_hit), not just distinct-day
    # counting -- proves resolve_experiment's reuse of
    # compute_backtest_comparison actually runs the barrier simulation.
    storage, backtest, experiments = fresh_env
    experiment = experiments.register_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h", target_metrics=["win_rate", "profit_factor"],
        stopping_rule={"min_accepted_trades": 1}, direction_source="coordinator",
    )
    anchor = datetime(2026, 8, 11, 14, 0, 0, tzinfo=timezone.utc)
    _save_candidate(storage, "c1", "TEST", "5m", anchor, atr=2.0, decision="enter_long", created_at=_FAR_FUTURE)
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    status = experiments.evaluate_stopping_rule(experiment)
    assert status["checks"]["min_accepted_trades"]["actual"] == 1
    assert status["stopping_rule_met"] is True

    resolved = experiments.resolve_experiment(experiment["experiment_id"])
    assert resolved["resolution"]["backtest"]["by_source"]["coordinator"]["trades_taken"] == 1


# ---------------------------------------------------------------------------
# list_experiments
# ---------------------------------------------------------------------------

def test_list_experiments_newest_first_and_filterable(fresh_env):
    _, _, experiments = fresh_env
    experiments.register_experiment(
        symbol="AAA", timeframe="5m", hypothesis="first", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1},
    )
    second = experiments.register_experiment(
        symbol="BBB", timeframe="5m", hypothesis="second", target_metrics=["win_rate"],
        stopping_rule={"min_distinct_trading_days": 1},
    )

    all_experiments = experiments.list_experiments()
    assert len(all_experiments) == 2
    assert all_experiments[0]["experiment_id"] == second["experiment_id"]  # newest first

    scoped = experiments.list_experiments(symbol="BBB", timeframe="5m")
    assert [e["hypothesis"] for e in scoped] == ["second"]
