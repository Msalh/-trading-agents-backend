"""
Focused endpoint tests for app.main — the Tier 2.9 (calendar
integrity) calendar_warning field on the webhook response, and the
Tier 2.10 (account-level risk controls) GET /account/risk endpoint.
Not a full endpoint test suite (that's covered piecemeal by the
per-module test files plus this project's ad-hoc smoke tests); this
file exists specifically for behavior wired at the main.py handler
level rather than inside a module that already has its own test file.

Run with: pytest tests/test_main.py -v
"""

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    for mod_name in (
        "app.timing_agent",
        "app.trading_calendar",
        "app.coordinator",
        "app.candidates",
        "app.outcomes",
        "app.replay",
        "app.paper_trades",
        "app.risk_agent",
        "app.account_risk",
        "app.main",
    ):
        importlib.reload(importlib.import_module(mod_name))

    import app.main as main
    yield TestClient(main.app)

    os.unlink(tmp.name)


def _payload(timestamp, trading_date, event_id="evt-1"):
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "symbol": "MNQ1!",
        "source": "pine",
        "timeframe": "5m",
        "timestamp": timestamp,
        "bar_status": "closed",
        "event_type": "bar_closed",
        "secret": "test-secret",
        "open": 20000.0, "high": 20010.0, "low": 19995.0, "close": 20005.0,
        "session_name": "RTH", "is_rth": True, "trading_date": trading_date,
    }


def test_webhook_calendar_warning_none_when_consistent(client):
    r = client.post("/webhook/tradingview", json=_payload("2026-08-11T14:00:00Z", "2026-08-11"))
    assert r.status_code == 200
    assert r.json()["calendar_warning"] is None


def test_webhook_calendar_warning_set_on_mismatch(client):
    """19:30 NY time (23:30Z in EDT) belongs to the NEXT trading day
    under the CME/Globex rollover convention -- a payload claiming the
    SAME day is a data-integrity mismatch."""
    r = client.post(
        "/webhook/tradingview",
        json=_payload("2026-08-11T23:30:00Z", "2026-08-11", event_id="evt-2"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["calendar_warning"] is not None
    assert "2026-08-11" in body["calendar_warning"]
    assert "2026-08-12" in body["calendar_warning"]
    # the bar is still stored despite the warning -- flagged, not rejected
    assert body["status"] == "stored"


def test_webhook_still_stores_bar_despite_calendar_mismatch(client):
    r = client.post(
        "/webhook/tradingview",
        json=_payload("2026-08-11T23:30:00Z", "2026-08-11", event_id="evt-3"),
    )
    assert r.status_code == 200
    import app.storage as storage
    bar = storage.get_latest(symbol="MNQ1!", timeframe="5m")
    assert bar is not None
    assert bar["event_id"] == "evt-3"


# ---------------------------------------------------------------------------
# Tier 2.10: GET /account/risk
# ---------------------------------------------------------------------------

def test_account_risk_no_trades_yet(client):
    r = client.get("/account/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["current_drawdown_used"] == 0.0
    assert body["daily_loss_used"] == 0.0
    assert body["remaining_drawdown_room"] == body["max_drawdown"]
    assert body["remaining_daily_loss_room"] == body["daily_loss_limit"]
    assert body["closed_trades_considered"] == 0


def test_account_risk_no_secret_required(client):
    """Read-only, same pattern as /trades/* and /candidates/* — must
    NOT 401 without X-Webhook-Secret."""
    r = client.get("/account/risk")
    assert r.status_code == 200


def test_account_risk_reflects_a_closed_losing_trade(client):
    import app.storage as storage
    trade = {
        "trade_id": "t1", "candidate_id": "c1", "symbol": "MNQ1!", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 20000.0, "stop_loss": 19980.0, "targets": [20050.0],
        "status": "open", "opened_at": "2026-08-11T14:00:00Z", "fill_price": 20000.0,
    }
    storage.save_paper_trade(trade)
    storage.close_trade(
        "t1", exit_price=19980.0, exit_reason="stop_hit", pnl_usd=-40.0,
        closed_at="2026-08-11T14:30:00Z",
    )

    r = client.get("/account/risk")
    body = r.json()
    assert body["current_drawdown_used"] == 40.0
    assert body["closed_trades_considered"] == 1


# ---------------------------------------------------------------------------
# Tier 3.1: causal integrity
# ---------------------------------------------------------------------------

def test_auto_analysis_anchors_to_triggering_event_not_a_newer_bar(client, monkeypatch):
    """The actual causal-integrity regression test: even though a NEWER
    bar already exists in storage by the time this runs (simulating a
    second webhook landing while the first's background task was still
    queued), the anchored run must only ever see bars at-or-before its
    own event_id's bar, and the resulting candidate must freeze that
    same anchor bar — never the newer one."""
    import app.main as main
    import app.storage as storage

    client.post("/webhook/tradingview", json=_payload("2026-08-11T14:00:00Z", "2026-08-11", event_id="evt-old"))
    client.post("/webhook/tradingview", json=_payload("2026-08-11T14:05:00Z", "2026-08-11", event_id="evt-new"))

    captured = {}

    def fake_run_analysis(symbol, timeframe, bars):
        captured["bars"] = bars
        from app.analysis_agent import AnalysisOpinion
        return AnalysisOpinion(
            agent="analysis", timestamp="2026-08-11T14:00:05Z", symbol=symbol, timeframe=timeframe,
            direction="bullish", confidence=70, reasoning="test", key_data={"key_levels": []}, flags=[],
        )

    monkeypatch.setattr(main, "run_analysis", fake_run_analysis)

    main._run_auto_analysis_and_coordinator("MNQ1!", "5m", "evt-old")

    seen_event_ids = {b["event_id"] for b in captured["bars"]}
    assert "evt-old" in seen_event_ids
    assert "evt-new" not in seen_event_ids  # the whole point of anchoring

    candidate = storage.get_latest_candidate(symbol="MNQ1!", timeframe="5m")
    assert candidate["bar"]["event_id"] == "evt-old"


def test_auto_analysis_logs_and_returns_when_anchor_bar_missing(client, monkeypatch, caplog):
    """Defensive path: an event_id that doesn't resolve to a stored bar
    (shouldn't normally happen) must not crash the background task."""
    import app.main as main

    def fail_if_called(*args, **kwargs):
        raise AssertionError("run_analysis should never be reached without an anchor bar")

    monkeypatch.setattr(main, "run_analysis", fail_if_called)
    main._run_auto_analysis_and_coordinator("MNQ1!", "5m", "no-such-event-id")  # must not raise


def test_risk_and_execution_locked_after_trade_committed(client, monkeypatch):
    """End-to-end: once Risk's size stage commits a paper trade from a
    candidate, re-calling /agents/execution/plan or /agents/risk/evaluate
    on that same still-current candidate must short-circuit to the
    trade's real, already-committed state — never spend a second paid
    Execution call, and never let the candidate's recorded risk/
    execution describe something other than what was actually taken."""
    import app.main as main
    import app.storage as storage
    from datetime import datetime, timezone

    headers = {"X-Webhook-Secret": "test-secret"}
    client.post("/webhook/tradingview", json=_payload("2026-08-11T14:00:00Z", "2026-08-11", event_id="evt-1"))

    def _opinion(direction, confidence):
        # Fresh (current-time) timestamps — the bar itself can be an
        # old fixed test date (Timing only reads its time-of-day/
        # day-of-week), but opinion freshness IS checked against the
        # real clock (ANALYSIS_MAX_AGE_MINUTES/NEWS_MACRO_MAX_AGE_MINUTES).
        return {
            "direction": direction, "confidence": confidence, "reasoning": "t",
            "key_data": {"key_levels": []}, "flags": [],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    storage.save_opinion("analysis", "MNQ1!", "5m", "t1", _opinion("bullish", 90))
    storage.save_opinion("news", "MNQ1!", "global", "t1", _opinion("bullish", 80))
    storage.save_opinion("macro", "MNQ1!", "global", "t1", _opinion("bullish", 70))

    r = client.get("/coordinator/decide", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    assert r.json()["decision"] == "enter_long"

    r = client.get("/agents/risk/evaluate", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    assert r.json()["risk_opinion"]["decision"] == "pending_execution"

    call_count = {"n": 0}

    def fake_plan_execution(**kwargs):
        call_count["n"] += 1
        from app.execution_agent import ExecutionOpinion
        return ExecutionOpinion(
            agent="execution", timestamp="2026-08-11T14:00:01Z", symbol="MNQ1!", timeframe="5m",
            status="planned", direction="bullish", order_type="market",
            entry_price=100.0, stop_loss=95.0, targets=[110.0], ready_now=True,
            reasoning="t", flags=[], validation_error=None,
        )

    monkeypatch.setattr(main, "plan_execution", fake_plan_execution)

    r = client.get("/agents/execution/plan", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    assert r.json()["execution_opinion"]["status"] == "planned"
    assert call_count["n"] == 1

    r = client.get("/agents/risk/evaluate", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    body = r.json()
    assert body["risk_opinion"]["decision"] == "approve"
    assert body["trade"] is not None
    trade_id = body["trade"]["trade_id"]

    # Re-run execution: must NOT call plan_execution again (no wasted
    # LLM spend), must report locked and return the SAME trade.
    r = client.get("/agents/execution/plan", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    body = r.json()
    assert body["locked"] is True
    assert body["trade"]["trade_id"] == trade_id
    assert call_count["n"] == 1

    # Re-run risk: same guarantee.
    r = client.get("/agents/risk/evaluate", params={"symbol": "MNQ1!", "timeframe": "5m"}, headers=headers)
    body = r.json()
    assert body["locked"] is True
    assert body["trade"]["trade_id"] == trade_id
    assert body["risk_opinion"]["decision"] == "approve"

    # And the candidate's persisted state genuinely still matches the
    # trade that was actually committed.
    candidate = storage.get_latest_candidate(symbol="MNQ1!", timeframe="5m")
    assert candidate["execution"]["entry_price"] == 100.0
    assert candidate["risk"]["decision"] == "approve"
