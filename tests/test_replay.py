"""
Unit tests for app.replay — Tier 2.5 (replay/versioning) and Tier 3.4
(sweep_thresholds(), COORDINATOR_THRESHOLD tuning). No LLM, no
network. Uses a temporary SQLite file, same pattern as
test_candidates.py.

Run with: pytest tests/test_replay.py -v
"""

import importlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from app.models import MarketStatePayload


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

    import app.candidates as candidates
    importlib.reload(candidates)

    import app.outcomes as outcomes
    importlib.reload(outcomes)

    import app.replay as replay
    importlib.reload(replay)

    yield storage, coordinator, candidates, outcomes, replay

    os.unlink(tmp.name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_bar(storage, symbol, timeframe, timestamp_dt, close):
    storage.save_event(
        MarketStatePayload(
            schema_version="1.0", event_id=f"{symbol}:{timeframe}:{_iso(timestamp_dt)}",
            symbol=symbol, source="pine", timeframe=timeframe, timestamp=_iso(timestamp_dt),
            bar_status="closed", event_type="bar_closed", secret="x",
            open=close, high=close + 1, low=close - 1, close=close,
            session_name="RTH", is_rth=True, trading_date=_iso(timestamp_dt)[:10],
        )
    )


def _opinion(direction, confidence):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasoning": "test",
        "key_data": {},
        "flags": [],
        "timestamp": _now_iso(),
    }


def test_replay_under_live_config_matches_original(fresh_env):
    """Replaying with no overrides (live config) against a candidate
    that was itself just created under the live config must reproduce
    the exact same decision — the baseline sanity check."""
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")

    result = replay.replay_candidate(candidate)

    assert result["changed"] is False
    assert result["replayed"]["decision"] == result["original"]["decision"]
    assert result["original"]["decision"] == "enter_long"


def test_replay_records_original_config_version(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")

    result = replay.replay_candidate(candidate)
    assert result["original"]["config_version"]["threshold"] == coordinator.DECISION_THRESHOLD


def test_replay_with_hypothetical_weights_can_flip_decision(fresh_env):
    """The core Tier 2.5 scenario: analysis alone never clears the
    live 60% min-available-weight, so the original candidate is
    insufficient_data — but replaying with a hypothetical config that
    gives analysis full weight and a lower minimum flips it."""
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    assert candidate["decision"]["decision"] == "insufficient_data"

    result = replay.replay_candidate(
        candidate,
        weights={"analysis": 1.0},
        threshold=25.0,
        min_available_weight=0.5,
    )

    assert result["changed"] is True
    assert result["original"]["decision"] == "insufficient_data"
    assert result["replayed"]["decision"] == "enter_long"
    assert result["replayed"]["config_version"]["weights"] == {"analysis": 1.0}


def test_replay_does_not_mutate_original_candidate(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    original_decision = dict(candidate["decision"])

    replay.replay_candidate(candidate, weights={"analysis": 1.0}, threshold=25.0, min_available_weight=0.5)

    refetched = candidates.get_candidate_history(symbol="TEST", timeframe="5m", limit=1)[0]
    assert refetched["decision"]["decision"] == original_decision["decision"] == "insufficient_data"


def test_replay_does_not_open_a_trade(fresh_env):
    """Replay is a pure read-only recompute — it must never open a
    paper trade, even when the replayed decision is directional."""
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")

    replay.replay_candidate(candidate, weights={"analysis": 1.0}, threshold=25.0, min_available_weight=0.5)

    assert storage.get_trade_by_candidate_id(candidate["candidate_id"]) is None


def test_replay_include_outcome_only_for_directional_replayed_decision(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 30))  # stays no_trade
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")

    result = replay.replay_candidate(candidate, include_outcome=True)
    assert result["replayed"]["decision"] in ("no_trade", "insufficient_data")
    assert "replayed_hypothetical_outcome" not in result


def test_replay_no_trade_pre_tier_2_5_config_version_is_none(fresh_env):
    """A candidate whose decision dict predates config_version (older
    data, field simply absent) must surface as None, not an empty
    dict pretending to be a real (if empty) config."""
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    # simulate a pre-Tier-2.5 row: strip config_version from the decision dict
    del candidate["decision"]["config_version"]

    result = replay.replay_candidate(candidate)
    assert result["original"]["config_version"] is None


def test_replay_candidates_for_symbol_only_changed_filter(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    candidates.create_candidate(symbol="TEST", timeframe="5m")  # insufficient_data under live config

    storage.save_opinion("news", "TEST", "global", "t2", _opinion("bullish", 60))
    candidates.create_candidate(symbol="TEST", timeframe="5m")  # analysis(90) + news(60) -> enter_long under live config

    all_results = replay.replay_candidates_for_symbol(symbol="TEST", timeframe="5m", limit=10)
    assert len(all_results) == 2

    hypothetical_results = replay.replay_candidates_for_symbol(
        symbol="TEST", timeframe="5m", limit=10,
        weights={"analysis": 1.0}, threshold=25.0, min_available_weight=0.5,
        only_changed=True,
    )
    # only the first candidate (originally insufficient_data) flips under the hypothetical config
    assert len(hypothetical_results) == 1
    assert hypothetical_results[0]["original"]["decision"] == "insufficient_data"
    assert hypothetical_results[0]["replayed"]["decision"] == "enter_long"


def test_summarize_replay_counts_transitions(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    candidates.create_candidate(symbol="TEST", timeframe="5m")

    results = replay.replay_candidates_for_symbol(
        symbol="TEST", timeframe="5m", limit=10,
        weights={"analysis": 1.0}, threshold=25.0, min_available_weight=0.5,
    )
    summary = replay.summarize_replay(results)

    assert summary["total_candidates"] == 1
    assert summary["changed"] == 1
    assert summary["unchanged"] == 0
    assert summary["transitions"] == {"insufficient_data -> enter_long": 1}


# ---------------------------------------------------------------------------
# sweep_thresholds — Tier 3.4 (COORDINATOR_THRESHOLD tuning)
# ---------------------------------------------------------------------------

def _directional_candidate(candidate_id, decision_time):
    """Builds and saves a candidate row directly (bypassing
    create_candidate's real-time stamping) so its decision timestamp
    can be placed safely in the past -- outcomes.compute_outcome_at_horizon
    reports "pending" for anything whose horizon hasn't passed relative
    to real now(), which a freshly-created candidate never satisfies."""
    opinions_used = {"analysis": _opinion("bullish", 90), "news": _opinion("bullish", 40)}
    decision = {
        "decision": "enter_long",
        "direction": "bullish",
        "score": 50.0,
        "threshold": 25.0,
        "timestamp": _iso(decision_time),
        "opinions_used": opinions_used,
        "missing_agents": [],
        "stale_agents": [],
        "config_version": {"weights": {"analysis": 0.40, "news": 0.25, "timing": 0.20, "macro": 0.15}, "threshold": 25.0, "min_available_weight": 0.6},
    }
    return decision, candidate_id


def test_sweep_thresholds_directional_count_shrinks_as_threshold_rises(fresh_env):
    """The core Tier 3.4 behavior: a low threshold clears (both
    opinions are bullish -- score is positive), an unreasonably high
    one never can, regardless of the exact scoring formula's
    magnitude."""
    storage, coordinator, candidates, outcomes, replay = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    decision, candidate_id = _directional_candidate("c1", decision_time)
    storage.save_candidate(candidate_id=candidate_id, symbol="TEST", timeframe="5m", bar=None, decision=decision)

    result = replay.sweep_thresholds("TEST", "5m", thresholds=[0.0, 1000.0], limit=10, horizons=[15])

    assert result["candidates_considered"] == 1
    assert result["sweep"][0.0]["directional_candidates"] == 1
    assert result["sweep"][1000.0]["directional_candidates"] == 0


def test_sweep_thresholds_computes_hypothetical_accuracy_per_horizon(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    horizon_time = decision_time + timedelta(minutes=15)
    _save_bar(storage, "TEST", "5m", decision_time, close=100.0)
    _save_bar(storage, "TEST", "5m", horizon_time, close=105.0)  # price rose -> correct for the replayed long

    decision, candidate_id = _directional_candidate("c1", decision_time)
    storage.save_candidate(candidate_id=candidate_id, symbol="TEST", timeframe="5m", bar=None, decision=decision)

    result = replay.sweep_thresholds("TEST", "5m", thresholds=[0.0], limit=10, horizons=[15])

    by_horizon = result["sweep"][0.0]["by_horizon_minutes"][15]
    assert by_horizon["correct"] == 1
    assert by_horizon["incorrect"] == 0
    assert by_horizon["accuracy"] == 1.0


def test_sweep_thresholds_weights_and_min_available_weight_held_fixed_are_reported(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    result = replay.sweep_thresholds(
        "TEST", "5m", thresholds=[10.0, 20.0], limit=10,
        weights={"analysis": 1.0}, min_available_weight=0.5,
    )
    assert result["weights_held_fixed"] == {"analysis": 1.0}
    assert result["min_available_weight_held_fixed"] == 0.5
    assert set(result["sweep"].keys()) == {10.0, 20.0}


def test_sweep_thresholds_empty_history_returns_zero_candidates(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    result = replay.sweep_thresholds("TEST", "5m", thresholds=[10.0, 25.0], limit=10)
    assert result["candidates_considered"] == 0
    assert result["sweep"][10.0]["directional_candidates"] == 0
    assert result["sweep"][25.0]["directional_candidates"] == 0


def test_sweep_thresholds_never_opens_a_trade_or_mutates_candidates(fresh_env):
    storage, coordinator, candidates, outcomes, replay = fresh_env
    decision_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    decision, candidate_id = _directional_candidate("c1", decision_time)
    storage.save_candidate(candidate_id=candidate_id, symbol="TEST", timeframe="5m", bar=None, decision=decision)

    replay.sweep_thresholds("TEST", "5m", thresholds=[0.0, 5.0, 10.0], limit=10)

    assert storage.get_trade_by_candidate_id("c1") is None
    refetched = storage.get_candidate_by_id("c1")
    assert refetched["decision"]["decision"] == "enter_long"  # untouched original
