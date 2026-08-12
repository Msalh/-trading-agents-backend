"""
Unit tests for app.candidates — the Tier 2.1 immutable trade-candidate
lifecycle. No LLM, no network. Uses a temporary SQLite file so tests
never touch real data.

Run with: pytest tests/test_candidates.py -v
"""

import importlib
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture
def fresh_storage(monkeypatch):
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

    yield storage, coordinator, candidates

    os.unlink(tmp.name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _opinion(direction, confidence, key_levels=None):
    return {
        "direction": direction,
        "confidence": confidence,
        "reasoning": "test",
        "key_data": {"key_levels": key_levels or []},
        "flags": [],
        "timestamp": _now_iso(),
    }


def _seed_bullish_scenario(storage):
    storage.save_opinion("analysis", "TEST", "5m", "t1", _opinion("bullish", 80, [105.0, 110.0]))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))


def test_create_candidate_even_for_no_trade(fresh_storage):
    """A candidate is created for every Coordinator run, not just
    ones that become a trade — cheap, and preserves context for
    later analysis of near-misses."""
    storage, coordinator, candidates = fresh_storage
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    assert candidate["decision"]["decision"] == "insufficient_data"
    assert candidate["candidate_id"]  # a real id was generated


def test_candidate_freezes_opinions_used(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")

    assert candidate["decision"]["decision"] == "enter_long"
    assert "analysis" in candidate["decision"]["opinions_used"]
    assert candidate["decision"]["opinions_used"]["analysis"]["confidence"] == 80


def test_get_current_candidate_returns_the_latest(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    created = candidates.create_candidate(symbol="TEST", timeframe="5m")

    current = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert current["candidate_id"] == created["candidate_id"]


def test_get_current_candidate_raises_when_none_exists(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    with pytest.raises(candidates.CandidateError):
        candidates.get_current_candidate(symbol="TEST", timeframe="5m")


def test_get_current_candidate_rejects_stale_one(fresh_storage, monkeypatch):
    storage, coordinator, candidates = fresh_storage
    monkeypatch.setenv("CANDIDATE_MAX_AGE_MINUTES", "20")
    importlib.reload(candidates)

    _seed_bullish_scenario(storage)
    candidates.create_candidate(symbol="TEST", timeframe="5m")

    # Manually age the candidate past the limit
    conn = storage.get_connection()
    conn.execute("UPDATE trade_candidates SET created_at = datetime('now', '-999 minutes')")
    conn.commit()
    conn.close()

    with pytest.raises(candidates.CandidateError, match="older than"):
        candidates.get_current_candidate(symbol="TEST", timeframe="5m")


def test_risk_and_execution_attach_to_the_same_row(fresh_storage):
    """The core Tier 2.1 guarantee: Risk and Execution results land
    on the SAME candidate row, not new independent records — so
    reading the candidate back always shows a self-consistent lineage."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    cid = candidate["candidate_id"]

    assert candidate["risk"] is None
    assert candidate["execution"] is None

    candidates.record_risk_result(cid, {"decision": "approve", "suggested_size": 1})
    after_risk = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert after_risk["candidate_id"] == cid  # still the same candidate
    assert after_risk["risk"]["decision"] == "approve"
    assert after_risk["execution"] is None  # untouched

    candidates.record_execution_result(cid, {"status": "planned", "entry_price": 100.5})
    after_exec = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert after_exec["candidate_id"] == cid
    assert after_exec["risk"]["decision"] == "approve"  # still there
    assert after_exec["execution"]["status"] == "planned"


def test_recording_result_on_unknown_candidate_raises(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    with pytest.raises(candidates.CandidateError):
        candidates.record_risk_result("not-a-real-id", {"decision": "approve"})


def test_new_candidate_created_by_later_run_does_not_retroactively_gain_earlier_risk(fresh_storage):
    """A second, later Coordinator run creates a brand-new candidate
    with its own empty risk/execution slots — it must never inherit
    or be confused with a previous candidate's Risk approval, even
    for the same symbol/timeframe."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    first = candidates.create_candidate(symbol="TEST", timeframe="5m")
    candidates.record_risk_result(first["candidate_id"], {"decision": "approve", "suggested_size": 1})

    second = candidates.create_candidate(symbol="TEST", timeframe="5m")
    assert second["candidate_id"] != first["candidate_id"]
    assert second["risk"] is None  # brand new, not inherited from `first`
