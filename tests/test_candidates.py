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

from app.models import MarketStatePayload


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


# ---------------------------------------------------------------------------
# Tier 3.1: causal integrity — event-anchored candidates, write-once
# risk/execution attach after a paper trade is committed.
# ---------------------------------------------------------------------------

def _bar(event_id, timestamp, close=100.0):
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "symbol": "TEST",
        "source": "pine",
        "timeframe": "5m",
        "timestamp": timestamp,
        "bar_status": "closed",
        "event_type": "bar_closed",
        "open": close, "high": close + 1, "low": close - 1, "close": close,
        "session_name": "RTH", "is_rth": True, "trading_date": timestamp[:10],
    }


def test_create_candidate_uses_the_explicit_bar_anchor_not_latest(fresh_storage):
    """The webhook-triggered path passes bar= explicitly. Even if a
    NEWER bar has already landed in storage by the time this runs
    (simulating a second webhook arriving mid-flight), the candidate
    must freeze the anchor bar it was given, not silently pick up
    whatever's newest — this is the actual causal-integrity fix."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)

    old_bar = _bar("evt-old", "2026-08-13T10:00:00Z", close=100.0)
    new_bar = _bar("evt-new", "2026-08-13T10:05:00Z", close=999.0)
    storage.save_event(MarketStatePayload(**old_bar, secret="x"))
    storage.save_event(MarketStatePayload(**new_bar, secret="x"))

    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m", bar=old_bar)
    assert candidate["bar"]["event_id"] == "evt-old"
    assert candidate["bar"]["close"] == 100.0


def test_create_candidate_uses_the_explicit_analysis_opinion_not_latest(fresh_storage):
    """Same guarantee for the Analysis opinion actually scored: even if
    a DIFFERENT (newer) Analysis opinion already exists in storage, the
    candidate scores exactly the opinion it was handed."""
    storage, coordinator, candidates = fresh_storage
    storage.save_opinion("analysis", "TEST", "5m", "t-newer", _opinion("bearish", 90, [1.0]))
    storage.save_opinion("news", "TEST", "global", "t1", _opinion("bullish", 60))
    storage.save_opinion("macro", "TEST", "global", "t1", _opinion("neutral", 50))

    anchor_opinion = _opinion("bullish", 80, [105.0])
    candidate = candidates.create_candidate(
        symbol="TEST", timeframe="5m", analysis_opinion=anchor_opinion
    )
    assert candidate["decision"]["opinions_used"]["analysis"]["direction"] == "bullish"
    assert candidate["decision"]["opinions_used"]["analysis"]["confidence"] == 80


def test_create_candidate_without_anchor_still_falls_back_to_latest(fresh_storage):
    """The manual /coordinator/decide path (no bar= passed) keeps the
    pre-Tier-3.1 behavior exactly — this is what backward compat means
    here."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    assert candidate["decision"]["decision"] == "enter_long"


def test_risk_history_preserves_gate_opinion_after_size_overwrites_current(fresh_storage):
    """The original review finding: sizing used to destroy the gate
    opinion because both shared one column. risk_history_json must
    keep both, in order, even though candidate["risk"] (the current/
    display value) becomes the size opinion."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    cid = candidate["candidate_id"]

    candidates.record_risk_result(cid, {"decision": "pending_execution", "stage": "gate"})
    candidates.record_risk_result(cid, {"decision": "approve", "stage": "size", "suggested_size": 1})

    current = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert current["risk"]["stage"] == "size"  # current/display value is the latest
    assert [r["stage"] for r in current["risk_history"]] == ["gate", "size"]  # nothing lost


def test_execution_history_preserves_a_retried_attempt(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    cid = candidate["candidate_id"]

    candidates.record_execution_result(cid, {"status": "invalid", "validation_error": "bad stop"})
    candidates.record_execution_result(cid, {"status": "planned", "entry_price": 100.5})

    current = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert current["execution"]["status"] == "planned"
    assert [e["status"] for e in current["execution_history"]] == ["invalid", "planned"]


def test_record_risk_result_locked_once_a_trade_is_committed(fresh_storage):
    """The second review's core finding: once a paper trade exists for
    a candidate, Risk/Execution must never be allowed to silently
    rewrite that candidate's recorded state out from under the trade
    that was actually taken."""
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    cid = candidate["candidate_id"]

    candidates.record_risk_result(cid, {"decision": "approve", "suggested_size": 1})
    trade = {
        "trade_id": "t1", "candidate_id": cid, "symbol": "TEST", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 100.0, "stop_loss": 95.0, "targets": [110.0],
        "status": "open", "opened_at": _now_iso(), "fill_price": 100.0,
    }
    storage.save_paper_trade(trade)

    assert candidates.get_committed_trade(cid)["trade_id"] == "t1"
    with pytest.raises(candidates.CandidateLockedError):
        candidates.record_risk_result(cid, {"decision": "modify", "suggested_size": 5})
    with pytest.raises(candidates.CandidateLockedError):
        candidates.record_execution_result(cid, {"status": "planned", "entry_price": 250.0})

    # unchanged — still the original approve/size=1, not the rejected rewrite attempt
    unchanged = candidates.get_current_candidate(symbol="TEST", timeframe="5m")
    assert unchanged["risk"]["suggested_size"] == 1
    assert unchanged["execution"] is None


def test_get_committed_trade_returns_none_before_any_trade_exists(fresh_storage):
    storage, coordinator, candidates = fresh_storage
    _seed_bullish_scenario(storage)
    candidate = candidates.create_candidate(symbol="TEST", timeframe="5m")
    assert candidates.get_committed_trade(candidate["candidate_id"]) is None
