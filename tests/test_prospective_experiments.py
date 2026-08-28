"""
Unit tests for app.prospective_experiments -- Tier 3.43 (sixteenth
external review, 2026-08-27: a small, separate, immutable pre-
registration record for the Tier 3.42 3-arm prospective overlap
comparison, closing the "manually recorded watermark in an untracked
package is not an enforceable freeze" gap the review raised).

Same real-temporary-SQLite-DB philosophy as tests/test_experiments.py:
this module's entire point is a REAL rowid-based no-peeking boundary
and REAL config-drift detection against REAL persisted candidates, so
the tests exercise real storage, not a stand-in.

Run with: pytest tests/test_prospective_experiments.py -v
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

    import app.news_agent as news_agent
    importlib.reload(news_agent)

    import app.macro_agent as macro_agent
    importlib.reload(macro_agent)

    import app.experiments as experiments
    importlib.reload(experiments)

    import app.prospective_experiments as prospective_experiments
    importlib.reload(prospective_experiments)

    yield storage, backtest, prospective_experiments

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


def _save_prospective_candidate(
    storage, candidate_id, symbol, timeframe, anchor_dt, decision="enter_long", direction="bullish",
    news_urgent=False, macro_risk_off=False, trading_date=None,
    news_model=None, news_prompt_version=None, macro_model=None, macro_prompt_version=None,
    include_macro=True,
):
    """Builds a candidate carrying Analysis+News(+Macro) opinions, with
    News/Macro opinions defaulting to the CURRENT live model/prompt_
    version (so they "match" a fresh registration by default) unless a
    test explicitly overrides them to simulate drift. include_macro=False
    omits the Macro opinion entirely, simulating a pre-existing "absent"
    case."""
    from app.news_agent import MODEL as LIVE_NEWS_MODEL, PROMPT_VERSION as LIVE_NEWS_PROMPT_VERSION
    from app.macro_agent import MODEL as LIVE_MACRO_MODEL, PROMPT_VERSION as LIVE_MACRO_PROMPT_VERSION

    anchor_iso = _iso(anchor_dt)
    # trading_date defaults to the anchor's own calendar date -- Tier
    # 3.39's _veto_pnl_flags reads bar.get("trading_date") directly, no
    # fallback recompute from the anchor timestamp (unlike
    # _candidate_trading_date elsewhere in app.backtest), so a test that
    # wants non-zero distinct_trading_days must always have this set.
    bar = {"timestamp": anchor_iso, "atr": 2.0, "trading_date": trading_date or anchor_dt.strftime("%Y-%m-%d")}
    # News/Macro directions match `direction` (not "neutral") so real
    # live-weight Coordinator re-scoring (app.prospective_experiments
    # always rescores under locked config before computing arms, exactly
    # like app.experiments) actually clears MIN_AVAILABLE_WEIGHT and the
    # score threshold -- same working pattern tests/test_experiments.py
    # already established (analysis 90 + news 80, both directional).
    news_opinion = {
        "direction": direction, "confidence": 80, "timestamp": anchor_iso,
        "flags": ["urgent"] if news_urgent else [],
        "model": news_model if news_model is not None else LIVE_NEWS_MODEL,
        "prompt_version": news_prompt_version if news_prompt_version is not None else LIVE_NEWS_PROMPT_VERSION,
    }
    opinions_used = {
        "analysis": {"direction": direction, "confidence": 90, "timestamp": anchor_iso},
        "news": news_opinion,
    }
    if include_macro:
        opinions_used["macro"] = {
            "direction": direction, "confidence": 80, "timestamp": anchor_iso,
            "flags": ["risk_off"] if macro_risk_off else [],
            "model": macro_model if macro_model is not None else LIVE_MACRO_MODEL,
            "prompt_version": macro_prompt_version if macro_prompt_version is not None else LIVE_MACRO_PROMPT_VERSION,
        }
    decision_dict = {
        "decision": decision, "timestamp": anchor_iso,
        "direction": direction if decision != "no_trade" else None,
        "score": 90.0, "threshold": 25.0,
        "opinions_used": opinions_used,
    }
    storage.save_candidate(candidate_id=candidate_id, symbol=symbol, timeframe=timeframe, bar=bar, decision=decision_dict)


_INCREMENTAL_PNL_METRICS = {
    "treatment_arm": "overlap_only", "baseline_arm": "none", "metric": "total_pnl_usd",
    "comparison_mode": "incremental", "comparator": ">", "success_threshold": -1e9,
}


# ---------------------------------------------------------------------------
# register_prospective_experiment
# ---------------------------------------------------------------------------

def test_register_freezes_arms_and_current_live_config(fresh_env):
    _, _, prospective = fresh_env
    from app.coordinator import DECISION_THRESHOLD, MIN_AVAILABLE_WEIGHT, WEIGHTS, ANALYSIS_REQUIRED
    from app.backtest import ATR_STOP_MULT, ATR_TARGET_MULT, BACKTEST_LOGIC_VERSION, EXPIRY_BARS, PROSPECTIVE_ARMS
    from app.paper_trades import COMMISSION_PER_CONTRACT, SLIPPAGE_POINTS
    from app.news_agent import MODEL as NEWS_MODEL, PROMPT_VERSION as NEWS_PROMPT_VERSION
    from app.macro_agent import MODEL as MACRO_MODEL, PROMPT_VERSION as MACRO_PROMPT_VERSION

    result = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="overlap outperforms baseline",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 2},
    )
    assert result["status"] == "active"
    assert result["resolved_at"] is None
    assert result["resolution"] is None
    assert result["arms"] == list(PROSPECTIVE_ARMS)
    assert result["locked_config"] == {
        "coordinator_threshold": DECISION_THRESHOLD,
        "weights": dict(WEIGHTS),
        "min_available_weight": MIN_AVAILABLE_WEIGHT,
        "analysis_required": ANALYSIS_REQUIRED,
        "backtest_geometry": {
            "atr_stop_mult": ATR_STOP_MULT,
            "atr_target_mult": ATR_TARGET_MULT,
            "expiry_bars": EXPIRY_BARS,
            "slippage_points": SLIPPAGE_POINTS,
            "commission_per_contract": COMMISSION_PER_CONTRACT,
            "backtest_logic_version": BACKTEST_LOGIC_VERSION,
        },
        "news_agent": {"model": NEWS_MODEL, "prompt_version": NEWS_PROMPT_VERSION},
        "macro_agent": {"model": MACRO_MODEL, "prompt_version": MACRO_PROMPT_VERSION},
    }


def test_register_watermark_is_current_max_rowid(fresh_env):
    storage, _, prospective = fresh_env
    _save_prospective_candidate(storage, "before-1", "TEST", "5m", datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc))
    _save_prospective_candidate(storage, "before-2", "TEST", "5m", datetime(2026, 8, 2, 14, 0, tzinfo=timezone.utc))

    result = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    assert result["registered_watermark_rowid"] == 2


@pytest.mark.parametrize("kwargs,expected_fragment", [
    ({"hypothesis": "   "}, "hypothesis"),
    ({"target_metrics": {}}, "target_metrics"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "treatment_arm": "not_a_real_arm"}}, "treatment_arm"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "baseline_arm": "not_a_real_arm"}}, "baseline_arm"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "treatment_arm": "none", "baseline_arm": "none"}}, "must differ"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "metric": "not_a_real_metric"}}, "metric"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "comparison_mode": "bogus"}}, "comparison_mode"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "comparator": "!="}}, "comparator"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "success_threshold": "not a number"}}, "success_threshold"),
    ({"target_metrics": {**_INCREMENTAL_PNL_METRICS, "secondary_metrics": ["bogus"]}}, "secondary_metrics"),
    ({"stopping_rule": {}}, "stopping_rule"),
    ({"stopping_rule": {"bogus_key": 1}}, "stopping_rule"),
])
def test_register_rejects_incomplete_or_invalid_input(fresh_env, kwargs, expected_fragment):
    _, _, prospective = fresh_env
    base = dict(
        symbol="TEST", timeframe="5m", hypothesis="some hypothesis",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 2},
    )
    base.update(kwargs)
    with pytest.raises(prospective.ProspectiveExperimentError, match=expected_fragment):
        prospective.register_prospective_experiment(**base)


# ---------------------------------------------------------------------------
# config-drift classification
# ---------------------------------------------------------------------------

def test_classify_opinion_drift_states():
    import app.prospective_experiments as prospective
    assert prospective._classify_opinion_drift(None, "m1", "v1") == "absent"
    assert prospective._classify_opinion_drift({"direction": "neutral"}, "m1", "v1") == "unversioned"
    assert prospective._classify_opinion_drift({"model": "m1", "prompt_version": "v1"}, "m1", "v1") == "matches"
    assert prospective._classify_opinion_drift({"model": "m1", "prompt_version": "v2"}, "m1", "v1") == "drifted"
    assert prospective._classify_opinion_drift({"model": "m2", "prompt_version": "v1"}, "m1", "v1") == "drifted"


def test_split_by_drift_excludes_drifted_keeps_unversioned_and_matches(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    _save_prospective_candidate(storage, "matches", "TEST", "5m", anchor)
    _save_prospective_candidate(storage, "drifted", "TEST", "5m", anchor, news_prompt_version="99-different")
    _save_prospective_candidate(storage, "unversioned", "TEST", "5m", anchor, include_macro=True)
    # simulate a genuinely pre-Tier-3.43 opinion (no model/prompt_version
    # keys at all) by hand-editing the stored candidate's JSON directly.
    conn = storage.get_connection()
    row = conn.execute("SELECT decision_json FROM trade_candidates WHERE candidate_id = ?", ("unversioned",)).fetchone()
    decision = json.loads(row["decision_json"])
    del decision["opinions_used"]["news"]["model"]
    del decision["opinions_used"]["news"]["prompt_version"]
    conn.execute("UPDATE trade_candidates SET decision_json = ? WHERE candidate_id = ?", (json.dumps(decision), "unversioned"))
    conn.commit()
    conn.close()

    candidates = prospective._prospective_candidates(experiment)
    clean, drifted_count, unversioned_count = prospective._split_by_drift(candidates, experiment)
    assert {c["candidate_id"] for c in clean} == {"matches", "unversioned"}
    assert drifted_count == 1
    assert unversioned_count == 1


# ---------------------------------------------------------------------------
# evaluate_prospective_stopping_rule -- counts only, never P&L
# ---------------------------------------------------------------------------

def test_stopping_rule_status_never_includes_pnl_fields(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS),
        stopping_rule={"min_distinct_trading_days": 5, "min_baseline_portfolio_trades": 5, "min_overlap_joint_opinion_pairs": 5},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc), news_urgent=True, macro_risk_off=True)
    _save_bar(storage, "TEST", "5m", datetime(2026, 8, 11, 14, 5, tzinfo=timezone.utc), open_=100.0, high=106.0, low=99.5, close=105.5)

    status = prospective.evaluate_prospective_stopping_rule(experiment)
    serialized = json.dumps(status)
    for forbidden in ("total_pnl_usd", "win_rate", "profit_factor", "max_drawdown_usd", "gross_profit_usd", "gross_loss_usd"):
        assert forbidden not in serialized
    assert "checks" in status
    assert "stopping_rule_met" in status
    assert "config_drift" in status


def test_stopping_rule_checks_all_three_named_components(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS),
        stopping_rule={"min_distinct_trading_days": 2, "min_baseline_portfolio_trades": 2, "min_overlap_joint_opinion_pairs": 1},
    )
    day1 = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    # "none" arm gets both candidates (full window); only c1 is overlap
    # (both flags), giving it 1 distinct joint opinion pair.
    _save_prospective_candidate(storage, "c1", "TEST", "5m", day1, news_urgent=True, macro_risk_off=True, trading_date="2026-08-11")
    _save_prospective_candidate(storage, "c2", "TEST", "5m", day2, trading_date="2026-08-12")
    _save_bar(storage, "TEST", "5m", day1 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", day2 + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    status = prospective.evaluate_prospective_stopping_rule(experiment)
    assert status["checks"]["min_distinct_trading_days"] == {"required": 2, "actual": 2, "met": True}
    assert status["checks"]["min_baseline_portfolio_trades"]["actual"] >= 1
    assert status["checks"]["min_overlap_joint_opinion_pairs"] == {"required": 1, "actual": 1, "met": True}


def test_stopping_rule_not_met_until_all_checks_pass(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS),
        stopping_rule={"min_distinct_trading_days": 1, "min_overlap_joint_opinion_pairs": 5},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    status = prospective.evaluate_prospective_stopping_rule(experiment)
    assert status["checks"]["min_distinct_trading_days"]["met"] is True
    assert status["checks"]["min_overlap_joint_opinion_pairs"]["met"] is False
    assert status["stopping_rule_met"] is False


def test_stopping_rule_surfaces_geometry_drift(fresh_env, monkeypatch):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(prospective, "COMMISSION_PER_CONTRACT", prospective.COMMISSION_PER_CONTRACT + 100.0)

    status = prospective.evaluate_prospective_stopping_rule(experiment)
    assert status["geometry_drift"] is not None
    assert "commission_per_contract" in status["geometry_drift"]


def test_stopping_rule_read_only_never_mutates(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 5},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    prospective.evaluate_prospective_stopping_rule(experiment)
    reloaded = prospective.get_prospective_experiment_by_id(experiment["experiment_id"])
    assert reloaded["status"] == "active"


# ---------------------------------------------------------------------------
# resolve_prospective_experiment -- one-time, refuses early, idempotent
# ---------------------------------------------------------------------------

def test_resolve_refuses_when_stopping_rule_not_met(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 5},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    with pytest.raises(prospective.ProspectiveExperimentError, match="not yet met"):
        prospective.resolve_prospective_experiment(experiment["experiment_id"])

    reloaded = prospective.get_prospective_experiment_by_id(experiment["experiment_id"])
    assert reloaded["status"] == "active"
    assert reloaded["resolution"] is None


def test_resolve_raises_for_unknown_id(fresh_env):
    _, _, prospective = fresh_env
    with pytest.raises(prospective.ProspectiveExperimentError, match="no prospective experiment"):
        prospective.resolve_prospective_experiment("does-not-exist")


def test_resolve_writes_once_and_never_recomputes(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    _save_prospective_candidate(storage, "c1", "TEST", "5m", datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc))

    resolved = prospective.resolve_prospective_experiment(experiment["experiment_id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None
    first_resolution = resolved["resolution"]
    assert first_resolution["resolved_from_candidates_considered"] == 1

    _save_prospective_candidate(storage, "c2", "TEST", "5m", datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc))
    resolved_again = prospective.resolve_prospective_experiment(experiment["experiment_id"])
    assert resolved_again["resolution"] == first_resolution
    assert resolved_again["resolution"]["resolved_from_candidates_considered"] == 1


def test_resolve_end_to_end_computes_incremental_target_metric_and_excludes_drift(fresh_env):
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="overlap beats baseline",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS),
        stopping_rule={"min_distinct_trading_days": 1, "min_baseline_portfolio_trades": 1},
    )
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    _save_prospective_candidate(storage, "clean", "TEST", "5m", anchor, decision="enter_long", direction="bullish")
    _save_prospective_candidate(
        storage, "drifted", "TEST", "5m", anchor + timedelta(minutes=1), decision="enter_short", direction="bearish",
        macro_prompt_version="99-different",
    )
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=6), open_=100.0, high=106.0, low=99.5, close=105.5)

    resolved = prospective.resolve_prospective_experiment(experiment["experiment_id"])
    resolution = resolved["resolution"]
    assert resolution["resolved_from_candidates_considered"] == 2
    # the drifted candidate is excluded from the CLEAN population the
    # actual arms/target-metric computation runs against.
    assert resolution["resolved_from_clean_candidates"] == 1
    assert resolution["config_drift"]["drifted_excluded_count"] == 1
    assert "none" in resolution["arms_results"]
    assert "overlap_only" in resolution["arms_results"]
    target_result = resolution["target_metrics_result"]
    assert target_result["treatment_arm"] == "overlap_only"
    assert target_result["baseline_arm"] == "none"
    assert target_result["comparison_mode"] == "incremental"
    assert target_result["actual"] is not None  # both values defined -> incremental always computable here
    assert resolution["geometry_drift"] is None


def test_resolve_target_metrics_inconclusive_when_metric_undefined(fresh_env):
    """profit_factor is None with zero losses -- met must be None, not
    False, same convention as app.experiments."""
    storage, _, prospective = fresh_env
    experiment = prospective.register_prospective_experiment(
        symbol="TEST", timeframe="5m", hypothesis="h",
        target_metrics={
            "treatment_arm": "none", "baseline_arm": "solo_veto_only", "metric": "profit_factor",
            "comparison_mode": "treatment_only", "comparator": ">=", "success_threshold": 1.0,
        },
        stopping_rule={"min_distinct_trading_days": 1},
    )
    anchor = datetime(2026, 8, 11, 14, 0, tzinfo=timezone.utc)
    _save_prospective_candidate(storage, "c1", "TEST", "5m", anchor, decision="enter_long", direction="bullish")
    # a winning trade only -> gross_loss_usd stays 0 -> profit_factor None
    _save_bar(storage, "TEST", "5m", anchor + timedelta(minutes=5), open_=100.0, high=106.0, low=99.5, close=105.5)

    resolved = prospective.resolve_prospective_experiment(experiment["experiment_id"])
    target_result = resolved["resolution"]["target_metrics_result"]
    assert target_result["actual"] is None
    assert target_result["met"] is None


# ---------------------------------------------------------------------------
# list_prospective_experiments
# ---------------------------------------------------------------------------

def test_list_newest_first_and_filterable(fresh_env):
    _, _, prospective = fresh_env
    prospective.register_prospective_experiment(
        symbol="AAA", timeframe="5m", hypothesis="first",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )
    second = prospective.register_prospective_experiment(
        symbol="BBB", timeframe="5m", hypothesis="second",
        target_metrics=dict(_INCREMENTAL_PNL_METRICS), stopping_rule={"min_distinct_trading_days": 1},
    )

    all_experiments = prospective.list_prospective_experiments()
    assert len(all_experiments) == 2
    assert all_experiments[0]["experiment_id"] == second["experiment_id"]

    scoped = prospective.list_prospective_experiments(symbol="BBB", timeframe="5m")
    assert [e["hypothesis"] for e in scoped] == ["second"]
