"""
Unit tests for app.coordinator — pure aggregation logic, no LLM,
no network. Uses a temporary SQLite file (via storage.DB_PATH
monkeypatch) so tests never touch real data.

Run with: pytest tests/test_coordinator.py -v
"""

import importlib
import os
import tempfile

import pytest


@pytest.fixture
def fresh_storage(monkeypatch):
    """Point storage at a throwaway temp DB file for each test, and
    reload coordinator so it picks up the patched storage module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    import app.coordinator as coordinator
    importlib.reload(coordinator)

    yield storage, coordinator

    os.unlink(tmp.name)


def _opinion(direction, confidence, flags=None):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasoning": "test",
        "key_data": {},
        "flags": flags or [],
    }


def test_no_opinions_returns_insufficient_data(fresh_storage):
    storage, coordinator = fresh_storage
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "insufficient_data"
    assert decision.score == 0.0
    assert set(decision.missing_agents) == {"analysis", "news", "timing", "macro"}


def test_strong_agreement_triggers_enter_long(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))
    # No market_state bar posted -> Timing is simply absent, not an error
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    # weighted_sum = 80*0.4 + 60*0.25 + 50*0*0.15(neutral=0) = 32 + 15 + 0 = 47
    # available_weight = 0.4 + 0.25 + 0.15 = 0.80 (timing missing)
    # score = 47 / 0.80 = 58.75
    assert decision.decision == "enter_long"
    assert decision.direction == "bullish"
    assert decision.score == pytest.approx(58.75, abs=0.01)
    assert "timing" in decision.missing_agents


def test_strong_bearish_agreement_triggers_enter_short(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bearish", 90))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_short"
    assert decision.direction == "bearish"
    assert decision.score < 0


def test_weak_signal_stays_no_trade(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 20))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "no_trade"
    assert decision.direction == "neutral"


def test_urgent_conflict_dampens_score(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70, flags=["urgent"]))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis_news_conflict_urgent_dampened" in decision.conflict_flags
    # undampened score would be (32 - 17.5) / 0.65 = 22.3; dampened halves it
    assert decision.score == pytest.approx(11.15, abs=0.5)


def test_non_urgent_conflict_flagged_but_not_dampened(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70))  # no urgent flag
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis_news_conflict" in decision.conflict_flags
    assert "analysis_news_conflict_urgent_dampened" not in decision.conflict_flags


def test_missing_agents_reported_correctly(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert set(decision.missing_agents) == {"analysis", "timing", "macro"}


def test_threshold_is_configurable_via_env(fresh_storage, monkeypatch):
    storage, coordinator = fresh_storage
    monkeypatch.setenv("COORDINATOR_THRESHOLD", "5")
    importlib.reload(coordinator)

    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 20))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    # score = 20*0.4/0.4 = 20, well over a threshold of 5
    assert decision.decision == "enter_long"
