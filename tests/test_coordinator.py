"""
Unit tests for app.coordinator — pure aggregation logic, no LLM,
no network. Uses a temporary SQLite file (via storage.DB_PATH
monkeypatch) so tests never touch real data.

Run with: pytest tests/test_coordinator.py -v
"""

import importlib
import os
import tempfile
from datetime import datetime, timedelta, timezone

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes_ago_iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opinion(direction, confidence, flags=None, timestamp=None):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasoning": "test",
        "key_data": {},
        "flags": flags or [],
        "timestamp": timestamp or _now_iso(),  # fresh by default — matches real agent output shape
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
    # available_weight = 0.4 + 0.25 + 0.15 = 0.80 (timing missing) -> clears the 0.6 minimum
    # score = 47 / 0.80 = 58.75
    assert decision.decision == "enter_long"
    assert decision.direction == "bullish"
    assert decision.score == pytest.approx(58.75, abs=0.01)
    assert "timing" in decision.missing_agents


def test_strong_bearish_agreement_triggers_enter_short(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bearish", 90))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bearish", 70))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_short"
    assert decision.direction == "bearish"
    assert decision.score < 0


def test_weak_signal_stays_no_trade(fresh_storage):
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 20))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("neutral", 50))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum, so this
    # is genuinely testing "enough evidence but not enough conviction",
    # not just "not enough evidence" (that's the min-weight test below).
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
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 20))
    # available_weight = 0.4 + 0.25 = 0.65 -> clears the 0.6 minimum
    # weighted_sum = 20*0.4 + 20*0.25 = 8 + 5 = 13; score = 13/0.65 = 20, well over threshold 5
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"


# --- Tier-1 safety fixes (external review, Aug 2026) ---------------------


def test_analysis_alone_is_insufficient_data(fresh_storage):
    """The specific scenario an external review flagged: Analysis
    alone (40% weight) used to be able to single-handedly trigger a
    trade. It no longer can — 40% < the 60% minimum combined weight
    required before any directional call is made."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "insufficient_data"
    assert decision.score == 0.0


def test_min_available_weight_configurable_via_env(fresh_storage, monkeypatch):
    storage, coordinator = fresh_storage
    monkeypatch.setenv("MIN_AVAILABLE_WEIGHT", "0.3")
    importlib.reload(coordinator)

    # Analysis alone is 40% weight, now above the lowered 30% minimum
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert decision.decision == "enter_long"


def test_stale_opinion_excluded_like_missing(fresh_storage, monkeypatch):
    """An opinion older than its type's max age is treated as if the
    agent never ran — not used in the score, and reported separately
    from a genuinely-missing agent. Min-weight lowered here so this
    test isolates the staleness behavior specifically, independent of
    the separate min-available-weight safeguard tested above."""
    storage, coordinator = fresh_storage
    monkeypatch.setenv("MIN_AVAILABLE_WEIGHT", "0.2")
    importlib.reload(coordinator)

    stale_ts = _minutes_ago_iso(999)  # far older than ANALYSIS_MAX_AGE_MINUTES (15)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=stale_ts))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.stale_agents
    assert "analysis" not in decision.contributions  # excluded from the score entirely
    assert "news" in decision.contributions  # the fresh one still counts


def test_unparseable_timestamp_treated_as_stale(fresh_storage):
    """An opinion with a timestamp that isn't valid ISO-8601 is
    treated as stale (untrustworthy), not silently assumed fresh."""
    storage, coordinator = fresh_storage
    storage.save_opinion(
        "analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp="not-a-real-timestamp")
    )
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert "analysis" in decision.stale_agents


def test_stale_and_missing_are_disjoint(fresh_storage):
    """A second external review caught that a stale opinion used to
    appear in BOTH missing_agents and stale_agents (missing_agents was
    computed as "not in the filtered opinions dict", which included
    agents removed for staleness). They must now be mutually exclusive
    — an agent is either present, missing (never ran), or stale (ran,
    too old), never two of these at once."""
    storage, coordinator = fresh_storage
    stale_ts = _minutes_ago_iso(999)
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=stale_ts))
    # news never runs at all -> genuinely missing
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.stale_agents
    assert "analysis" not in decision.missing_agents
    assert "news" in decision.missing_agents
    assert "news" not in decision.stale_agents
    # no agent appears in both lists
    assert set(decision.stale_agents).isdisjoint(set(decision.missing_agents))


def test_future_timestamp_treated_as_stale(fresh_storage):
    """A materially future-dated opinion is a data integrity problem,
    not a fast clock — must not be treated as fresh."""
    storage, coordinator = fresh_storage
    from datetime import datetime, timedelta, timezone
    future_ts = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 90, timestamp=future_ts))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")
    assert "analysis" in decision.stale_agents


def test_opinions_used_captures_exact_snapshot(fresh_storage):
    """CoordinatorDecision.opinions_used carries exactly the opinions
    the score was computed from — this is what lets a trade candidate
    be built as an atomic snapshot, without a second, separately-timed
    database read that could see different data."""
    storage, coordinator = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    decision = coordinator.compute_decision(symbol="TEST", timeframe="5m")

    assert "analysis" in decision.opinions_used
    assert "news" in decision.opinions_used
    assert decision.opinions_used["analysis"]["confidence"] == 80
