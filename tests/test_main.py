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
# Tier 3.22 (fifth external review): trade provenance
# ---------------------------------------------------------------------------

def _closed_trade(trade_id, candidate_id, pnl_usd, provenance):
    return {
        "trade_id": trade_id, "candidate_id": candidate_id, "symbol": "MNQ1!", "timeframe": "5m",
        "direction": "bullish", "size": 1, "order_type": "market",
        "entry_price": 20000.0, "stop_loss": 19980.0, "targets": [20050.0],
        "status": "open", "opened_at": "2026-08-11T14:00:00Z", "fill_price": 20000.0,
        "provenance": provenance,
    }


def test_account_risk_reports_closed_trades_by_provenance(client):
    import app.storage as storage
    storage.save_paper_trade(_closed_trade("t1", "c1", -40.0, "manual_dashboard"))
    storage.close_trade("t1", exit_price=19980.0, exit_reason="stop_hit", pnl_usd=-40.0, closed_at="2026-08-11T14:30:00Z")
    storage.save_paper_trade(_closed_trade("t2", "c2", 20.0, "auto_policy"))
    storage.close_trade("t2", exit_price=20010.0, exit_reason="target_hit", pnl_usd=20.0, closed_at="2026-08-11T15:00:00Z")

    r = client.get("/account/risk")
    body = r.json()
    assert body["closed_trades_considered"] == 2
    assert body["closed_trades_by_provenance"] == {"manual_dashboard": 1, "auto_policy": 1}
    # Deliberately unfiltered: both trades still fed the real
    # current_drawdown_used/daily_loss_used computation above (not
    # re-asserted here since the exact drawdown formula isn't this
    # test's concern) -- this tier only reports provenance, it doesn't
    # change risk-gating behavior.


def test_trades_history_default_includes_every_provenance(client):
    import app.storage as storage
    storage.save_paper_trade(_closed_trade("t1", "c1", -40.0, "manual_dashboard"))
    storage.close_trade("t1", exit_price=19980.0, exit_reason="stop_hit", pnl_usd=-40.0, closed_at="2026-08-11T14:30:00Z")
    storage.save_paper_trade(_closed_trade("t2", "c2", 20.0, "auto_policy"))
    storage.close_trade("t2", exit_price=20010.0, exit_reason="target_hit", pnl_usd=20.0, closed_at="2026-08-11T15:00:00Z")

    r = client.get("/trades/history", params={"symbol": "MNQ1!", "timeframe": "5m"})
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_trades_history_exclude_provenance_filters_manual(client):
    import app.storage as storage
    storage.save_paper_trade(_closed_trade("t1", "c1", -40.0, "manual_dashboard"))
    storage.close_trade("t1", exit_price=19980.0, exit_reason="stop_hit", pnl_usd=-40.0, closed_at="2026-08-11T14:30:00Z")
    storage.save_paper_trade(_closed_trade("t2", "c2", 20.0, "auto_policy"))
    storage.close_trade("t2", exit_price=20010.0, exit_reason="target_hit", pnl_usd=20.0, closed_at="2026-08-11T15:00:00Z")

    r = client.get("/trades/history", params={"symbol": "MNQ1!", "timeframe": "5m", "exclude_provenance": "manual_dashboard"})
    body = r.json()
    assert len(body) == 1
    assert body[0]["trade_id"] == "t2"
    assert body[0]["provenance"] == "auto_policy"


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


# ---------------------------------------------------------------------------
# Tier 3.9: auto-execution (opt-in, off by default)
# ---------------------------------------------------------------------------

def _seed_directional_candidate(storage, candidate_id="cand-auto-1", decision="enter_long", direction="bullish"):
    bar = {
        "event_id": "evt-auto-1", "symbol": "MNQ1!", "timeframe": "5m",
        "timestamp": "2026-08-11T14:00:00Z", "close": 20000.0,
    }
    decision_json = {
        "decision": decision,
        "direction": direction,
        "timestamp": "2026-08-11T14:00:01Z",
        "opinions_used": {
            "analysis": {"key_data": {"key_levels": [20050.0, 19950.0]}},
        },
    }
    storage.save_candidate(
        candidate_id=candidate_id, symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision_json
    )
    return storage.get_candidate_by_id(candidate_id)


def _fake_planned_execution(**kwargs):
    from app.execution_agent import ExecutionOpinion
    return ExecutionOpinion(
        agent="execution", timestamp="2026-08-11T14:00:02Z", symbol="MNQ1!", timeframe="5m",
        status="planned", direction="bullish", order_type="market",
        entry_price=20000.0, stop_loss=19950.0, targets=[20100.0], ready_now=True,
        reasoning="t", flags=[], validation_error=None,
    )


def test_auto_execute_disabled_by_default(client):
    """AUTO_EXECUTE_ENABLED must default to false — the webhook flow
    must never auto-open trades unless it's explicitly set."""
    import app.main as main

    assert main.AUTO_EXECUTE_ENABLED is False


def test_auto_execute_opens_a_trade_for_a_qualifying_candidate(client, monkeypatch):
    """The main happy path: a directional candidate, walked through
    the real evaluate_risk_gate/size_position (only plan_execution is
    mocked, same as the manual-flow test above), ends with a committed
    paper trade — no human click involved."""
    import app.main as main
    import app.storage as storage

    candidate = _seed_directional_candidate(storage)
    monkeypatch.setattr(main, "plan_execution", _fake_planned_execution)

    main._auto_execute_candidate(candidate)

    trade = storage.get_trade_by_candidate_id(candidate["candidate_id"])
    assert trade is not None
    assert trade["status"] == "pending_fill"
    assert trade["entry_price"] == 20000.0

    refreshed = storage.get_candidate_by_id(candidate["candidate_id"])
    assert refreshed["risk"]["decision"] == "approve"
    assert refreshed["risk"]["stage"] == "size"
    assert refreshed["execution"]["status"] == "planned"


def test_auto_execute_skips_non_directional_candidate(client, monkeypatch):
    import app.main as main
    import app.storage as storage

    candidate = _seed_directional_candidate(
        storage, candidate_id="cand-auto-nodir", decision="no_trade", direction=None
    )

    def fail_if_called(*a, **k):
        raise AssertionError("evaluate_risk_gate should never be reached for a non-directional decision")

    monkeypatch.setattr(main, "evaluate_risk_gate", fail_if_called)

    main._auto_execute_candidate(candidate)

    refreshed = storage.get_candidate_by_id(candidate["candidate_id"])
    assert refreshed["risk"] is None
    assert refreshed["execution"] is None


def test_auto_execute_stops_at_gate_reject_without_calling_execution(client, monkeypatch):
    import app.main as main
    import app.storage as storage
    from app.risk_agent import RiskOpinion

    candidate = _seed_directional_candidate(storage, candidate_id="cand-auto-reject")

    def fail_if_called(**kwargs):
        raise AssertionError("plan_execution should never be reached when the gate rejects")

    monkeypatch.setattr(main, "plan_execution", fail_if_called)
    monkeypatch.setattr(
        main,
        "evaluate_risk_gate",
        lambda **kwargs: RiskOpinion(
            agent="risk", timestamp="2026-08-11T14:00:01Z", symbol="MNQ1!", timeframe="5m",
            stage="gate", decision="reject", original_size=1, suggested_size=None,
            reasoning="test reject", key_data={}, flags=["max_positions_reached"],
        ),
    )

    main._auto_execute_candidate(candidate)

    refreshed = storage.get_candidate_by_id(candidate["candidate_id"])
    assert refreshed["risk"]["decision"] == "reject"
    assert refreshed["execution"] is None


def test_auto_execute_does_not_size_when_execution_declines(client, monkeypatch):
    import app.main as main
    import app.storage as storage

    candidate = _seed_directional_candidate(storage, candidate_id="cand-auto-noexec")

    def fake_no_action(**kwargs):
        from app.execution_agent import ExecutionOpinion
        return ExecutionOpinion(
            agent="execution", timestamp="2026-08-11T14:00:02Z", symbol="MNQ1!", timeframe="5m",
            status="no_action", direction=None, order_type=None, entry_price=None, stop_loss=None,
            targets=None, ready_now=None, reasoning="no clean setup", flags=[], validation_error=None,
        )

    monkeypatch.setattr(main, "plan_execution", fake_no_action)

    main._auto_execute_candidate(candidate)

    trade = storage.get_trade_by_candidate_id(candidate["candidate_id"])
    assert trade is None
    refreshed = storage.get_candidate_by_id(candidate["candidate_id"])
    assert refreshed["risk"]["decision"] == "pending_execution"  # still gate stage — size never ran
    assert refreshed["execution"]["status"] == "no_action"


def test_auto_execute_never_raises_on_internal_error(client, monkeypatch):
    import app.main as main
    import app.storage as storage

    candidate = _seed_directional_candidate(storage, candidate_id="cand-auto-err")

    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "evaluate_risk_gate", boom)

    main._auto_execute_candidate(candidate)  # must not raise


def test_auto_execute_skips_when_already_committed(client, monkeypatch):
    import app.main as main
    import app.storage as storage

    candidate = _seed_directional_candidate(storage, candidate_id="cand-auto-locked")
    monkeypatch.setattr(main, "plan_execution", _fake_planned_execution)
    main._auto_execute_candidate(candidate)
    assert storage.get_trade_by_candidate_id(candidate["candidate_id"]) is not None

    def fail_if_called(**kwargs):
        raise AssertionError("evaluate_risk_gate should never re-run once a trade is committed")

    monkeypatch.setattr(main, "evaluate_risk_gate", fail_if_called)
    main._auto_execute_candidate(candidate)  # same (now-stale) candidate dict — candidate_id is what matters


def test_system_status_reports_auto_execute_enabled_flag(client):
    r = client.get("/system/status")
    assert r.json()["auto_execute_enabled"] is False


def test_webhook_background_task_calls_auto_execute_when_enabled(monkeypatch):
    """AUTO_EXECUTE_ENABLED is read once at app.main import time — this
    test reloads app.main with it set to true and confirms the
    webhook's background task actually invokes _auto_execute_candidate
    after a successful candidate creation, wiring verified independent
    of _auto_execute_candidate's own internal behavior (covered above)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("DB_PATH", tmp.name)
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("AUTO_EXECUTE_ENABLED", "true")

    import app.storage as storage
    importlib.reload(storage)
    storage.init_db()

    for mod_name in (
        "app.timing_agent", "app.trading_calendar", "app.coordinator", "app.candidates",
        "app.outcomes", "app.replay", "app.paper_trades", "app.risk_agent", "app.account_risk",
        "app.main",
    ):
        importlib.reload(importlib.import_module(mod_name))

    import app.main as main

    assert main.AUTO_EXECUTE_ENABLED is True

    called = {"n": 0, "candidate": None}

    def fake_auto_execute(candidate):
        called["n"] += 1
        called["candidate"] = candidate

    monkeypatch.setattr(main, "_auto_execute_candidate", fake_auto_execute)

    def fake_run_analysis(symbol, timeframe, bars):
        from app.analysis_agent import AnalysisOpinion
        from datetime import datetime, timezone
        return AnalysisOpinion(
            agent="analysis", timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            symbol=symbol, timeframe=timeframe, direction="bullish", confidence=90,
            reasoning="test", key_data={"key_levels": []}, flags=[],
        )

    monkeypatch.setattr(main, "run_analysis", fake_run_analysis)

    # TestClient runs BackgroundTasks synchronously as part of the
    # request/response cycle, so the webhook POST alone is enough to
    # exercise _run_auto_analysis_and_coordinator — no need to also
    # call it directly (that would run it a second time).
    test_client = TestClient(main.app)
    test_client.post(
        "/webhook/tradingview",
        json=_payload("2026-08-11T14:00:00Z", "2026-08-11", event_id="evt-auto-webhook"),
    )

    assert called["n"] == 1
    assert called["candidate"]["symbol"] == "MNQ1!"

    os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Tier 3.10: ATR-barrier backtest-lite
# ---------------------------------------------------------------------------

def _save_market_bar(storage, symbol, timeframe, timestamp, open_, high, low, close, atr=None):
    import json as _json
    conn = storage.get_connection()
    payload = {
        "event_id": f"{symbol}:{timeframe}:{timestamp}",
        "symbol": symbol, "timeframe": timeframe, "timestamp": timestamp,
        "open": open_, "high": high, "low": low, "close": close, "atr": atr,
    }
    conn.execute(
        "INSERT INTO market_state (event_id, symbol, timeframe, timestamp, payload_json) VALUES (?, ?, ?, ?, ?)",
        (payload["event_id"], symbol, timeframe, timestamp, _json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def test_backtest_lite_endpoint_returns_all_sources_by_default(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-bt-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
    }
    storage.save_candidate(candidate_id="cand-bt-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
    _save_market_bar(
        storage, "MNQ1!", "5m", "2026-08-11T14:05:00Z",
        open_=20000.0, high=20060.0, low=19995.0, close=20055.0,
    )

    r = client.get("/candidates/history/backtest-lite", params={"symbol": "MNQ1!", "timeframe": "5m"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["by_source"].keys()) == {
        "analysis", "coordinator", "inverse_analysis", "always_bullish", "always_bearish", "vwap",
        "analysis_risk_filtered", "coordinator_veto_filtered", "coordinator_quorum_bypass",
    }
    assert body["by_source"]["coordinator"]["trades_taken"] == 1


def test_backtest_lite_endpoint_rejects_unknown_source(client):
    r = client.get(
        "/candidates/history/backtest-lite",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "not_a_real_source"},
    )
    assert r.status_code == 400


def test_backtest_lite_endpoint_accepts_a_source_subset(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-bt-2", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
    decision = {"decision": "enter_short", "timestamp": anchor, "opinions_used": {}}
    storage.save_candidate(candidate_id="cand-bt-2", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
    # Bearish stop sits at entry+1.5*ATR=20003, target at
    # entry-2.5*ATR=19995 — keep high below the stop so only the
    # target is touched this bar (a clean win, not an ambiguous
    # same-bar stop/target tie).
    _save_market_bar(
        storage, "MNQ1!", "5m", "2026-08-11T14:05:00Z",
        open_=20000.0, high=20001.0, low=19940.0, close=19945.0,
    )

    r = client.get(
        "/candidates/history/backtest-lite",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "coordinator,always_bullish"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["by_source"].keys()) == {"coordinator", "always_bullish"}
    assert body["by_source"]["coordinator"]["wins"] == 1


# ---------------------------------------------------------------------------
# Tier 3.11: champion/challenger (out-of-sample)
# ---------------------------------------------------------------------------

def test_champion_challenger_endpoint_returns_calibration_and_validation(client):
    import app.storage as storage

    base = "2026-08-11T14:00:00Z"
    for i in range(6):
        anchor = f"2026-08-11T{14 + i // 3}:{(i % 3) * 20:02d}:00Z"
        bar = {"event_id": f"evt-cc-{i}", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
        decision = {
            "decision": "enter_long", "timestamp": anchor,
            "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
        }
        storage.save_candidate(candidate_id=f"cand-cc-{i}", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
        _save_market_bar(
            storage, "MNQ1!", "5m", f"2026-08-11T{14 + i // 3}:{(i % 3) * 20 + 5:02d}:00Z",
            open_=20000.0, high=20060.0, low=19995.0, close=20055.0,
        )

    r = client.get(
        "/candidates/history/backtest-lite/champion-challenger",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "champion": "coordinator",
            "challengers": "always_bullish,inverse_analysis", "holdout_fraction": 0.5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["champion"] == "coordinator"
    assert set(body["challengers"]) == {"always_bullish", "inverse_analysis"}
    for source_result in body["by_source"].values():
        assert set(source_result.keys()) == {"calibration", "validation"}


def test_champion_challenger_endpoint_rejects_unknown_source(client):
    r = client.get(
        "/candidates/history/backtest-lite/champion-challenger",
        params={"symbol": "MNQ1!", "timeframe": "5m", "champion": "not_a_real_source"},
    )
    assert r.status_code == 400


def test_champion_challenger_endpoint_rejects_out_of_range_holdout_fraction(client):
    r = client.get(
        "/candidates/history/backtest-lite/champion-challenger",
        params={"symbol": "MNQ1!", "timeframe": "5m", "holdout_fraction": 1.5},
    )
    assert r.status_code == 422  # FastAPI's own gt/lt Query validation, before our function ever runs


# ---------------------------------------------------------------------------
# Tier 3.12: paired signal comparison (backtest-lite methodology fix)
# ---------------------------------------------------------------------------

def test_paired_backtest_endpoint_returns_shared_trade_counts_per_source(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-paired-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
    }
    storage.save_candidate(candidate_id="cand-paired-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
    _save_market_bar(
        storage, "MNQ1!", "5m", "2026-08-11T14:05:00Z",
        open_=20000.0, high=20060.0, low=19995.0, close=20055.0,
    )

    r = client.get(
        "/candidates/history/backtest-lite/paired",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "analysis,coordinator"},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["by_source"].keys()) == {"analysis", "coordinator"}
    # Same candidate, same shared entry/schedule, both sources bullish here
    # -- both must report the identical accepted trade count.
    assert body["config"]["accepted_candidates"] == 1
    assert body["by_source"]["analysis"]["trades_taken"] == body["by_source"]["coordinator"]["trades_taken"] == 1


def test_paired_backtest_endpoint_rejects_unknown_source(client):
    r = client.get(
        "/candidates/history/backtest-lite/paired",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "not_a_real_source"},
    )
    assert r.status_code == 400


def test_paired_backtest_endpoint_requires_at_least_one_source(client):
    r = client.get(
        "/candidates/history/backtest-lite/paired",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": ""},
    )
    assert r.status_code == 400


def test_paired_backtest_endpoint_excludes_candidates_ineligible_for_any_source(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-paired-2", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
    # No "analysis" opinion recorded, so the "analysis" source can't
    # resolve a direction for this candidate -- it must be excluded
    # from the shared (intersected) eligible set entirely.
    decision = {"decision": "enter_long", "timestamp": anchor, "opinions_used": {}}
    storage.save_candidate(candidate_id="cand-paired-2", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
    _save_market_bar(
        storage, "MNQ1!", "5m", "2026-08-11T14:05:00Z",
        open_=20000.0, high=20060.0, low=19995.0, close=20055.0,
    )

    r = client.get(
        "/candidates/history/backtest-lite/paired",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "analysis,coordinator"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["eligible_candidates"] == 0
    assert body["by_source"]["analysis"]["trades_taken"] == 0
    assert body["by_source"]["coordinator"]["trades_taken"] == 0


# ---------------------------------------------------------------------------
# Tier 3.14: pre-registered parameter sensitivity grid
# ---------------------------------------------------------------------------

def test_sensitivity_grid_endpoint_runs_the_default_27_combination_grid(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-grid-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "atr": 2.0}
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
    }
    storage.save_candidate(candidate_id="cand-grid-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)
    for i in range(1, 25):
        _save_market_bar(
            storage, "MNQ1!", "5m", f"2026-08-11T14:{5 * i:02d}:00Z" if 5 * i < 60 else f"2026-08-11T15:{5 * i - 60:02d}:00Z",
            open_=20000.0, high=20060.0, low=19995.0, close=20055.0,
        )

    r = client.get(
        "/candidates/history/backtest-lite/sensitivity-grid",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "coordinator"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["grid"]["total_combinations"] == 27
    assert len(body["combinations"]) == 27
    assert "coordinator" in body["robustness"]


def test_sensitivity_grid_endpoint_rejects_unknown_source(client):
    r = client.get(
        "/candidates/history/backtest-lite/sensitivity-grid",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": "not_a_real_source"},
    )
    assert r.status_code == 400


def test_sensitivity_grid_endpoint_requires_at_least_one_source(client):
    r = client.get(
        "/candidates/history/backtest-lite/sensitivity-grid",
        params={"symbol": "MNQ1!", "timeframe": "5m", "sources": ""},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Tier 3.15: LLM call cost/usage telemetry
# ---------------------------------------------------------------------------

def test_llm_usage_endpoint_reports_overall_and_by_agent(client):
    import app.storage as storage

    storage.record_llm_call(
        agent="analysis", model="claude-sonnet-5", trigger_context="MNQ1!/5m",
        success=True, error_message=None, latency_ms=150.0,
        input_tokens=300, output_tokens=120, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=0, estimated_cost_usd=0.0018, pricing_version="1",
    )
    storage.record_llm_call(
        agent="news", model="claude-sonnet-5", trigger_context="MNQ1!",
        success=False, error_message="timeout", latency_ms=6000.0,
        input_tokens=None, output_tokens=None, cache_creation_input_tokens=None,
        cache_read_input_tokens=None, web_search_requests=None, estimated_cost_usd=None, pricing_version="1",
    )

    r = client.get("/system/llm-usage")
    assert r.status_code == 200
    body = r.json()
    assert body["overall"]["total_calls"] == 2
    assert body["overall"]["successful_calls"] == 1
    assert body["overall"]["failed_calls"] == 1
    assert body["by_agent"]["analysis"]["total_input_tokens"] == 300
    assert len(body["recent_calls"]) == 2
    assert body["pricing_versions_present"] == ["1"]


def test_llm_usage_endpoint_reports_telemetry_health(client):
    """Tier 3.25: telemetry_health is present and self-consistent even
    with zero calls made through the ACTUAL track_llm_call wrapper in
    this test run (the two rows above were inserted directly via
    storage.record_llm_call, which doesn't touch the in-process
    attempted/written/failed counters -- those only move via
    track_llm_call itself, see test_llm_telemetry.py)."""
    r = client.get("/system/llm-usage")
    assert r.status_code == 200
    body = r.json()
    health = body["telemetry_health"]
    assert "telemetry_started_at" in health
    assert "pricing_version" in health
    assert health["attempted"] >= 0
    assert health["written"] >= 0
    assert health["failed"] >= 0
    assert (health["write_success_rate"] is None) or (0.0 <= health["write_success_rate"] <= 1.0)


def test_llm_usage_endpoint_filters_recent_calls_by_agent(client):
    import app.storage as storage

    storage.record_llm_call(
        agent="macro", model="claude-sonnet-5", trigger_context="MNQ1!",
        success=True, error_message=None, latency_ms=400.0,
        input_tokens=50, output_tokens=20, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=1, estimated_cost_usd=0.0005, pricing_version="1",
    )
    storage.record_llm_call(
        agent="execution", model="claude-sonnet-5", trigger_context="MNQ1!/5m",
        success=True, error_message=None, latency_ms=900.0,
        input_tokens=200, output_tokens=90, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, web_search_requests=0, estimated_cost_usd=0.0013, pricing_version="1",
    )

    r = client.get("/system/llm-usage", params={"recent_agent": "macro"})
    assert r.status_code == 200
    body = r.json()
    assert all(c["agent"] == "macro" for c in body["recent_calls"])


# ---------------------------------------------------------------------------
# Tier 3.16: Coordinator/Analysis divergence + ablation diagnostic
# ---------------------------------------------------------------------------

def test_coordinator_divergence_endpoint_returns_report_shape(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-cd-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor}
    decision = {
        "decision": "enter_long",
        "score": 30.0,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 80, "timestamp": anchor},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {
            "analysis": {"direction": "bullish", "confidence": 80, "weight": 0.4, "contribution": 32.0},
        },
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-cd-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/coordinator-divergence",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["timeframe"] == "5m"
    assert body["candidates_considered"] == 1
    assert body["named_categories"] == {"analysis_directional_coordinator_same_direction": 1}
    assert set(body["ablation"].keys()) == {"analysis_removed", "news_removed", "macro_removed"}
    assert body["ablation"]["analysis_removed"]["candidates_considered"] == 1


def test_coordinator_divergence_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/coordinator-divergence",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_considered"] == 0
    assert body["named_categories"] == {}


# ---------------------------------------------------------------------------
# Tier 3.26: News/Macro threshold-crossing deep dive
# ---------------------------------------------------------------------------

def test_threshold_crossing_deep_dive_endpoint_returns_case_shape(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {
        "event_id": "evt-tcd-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-11",
    }
    # analysis=20/news=70/macro=10 bullish, live WEIGHTS -- News alone crosses
    # the threshold (enter_long); ablating News alone (analysis+macro only)
    # falls back to no_trade -- a clean agent_enabled_trade threshold_crossing
    # case, same scenario verified in test_coordinator_diagnostics.py.
    decision = {
        "decision": "enter_long",
        "score": 33.75,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 20, "timestamp": anchor, "flags": []},
            "news": {"direction": "bullish", "confidence": 70, "timestamp": anchor, "flags": []},
            "macro": {"direction": "bullish", "confidence": 10, "timestamp": anchor, "flags": []},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {},
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-tcd-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/threshold-crossing-deep-dive",
        params={"symbol": "MNQ1!", "timeframe": "5m", "agent": "news"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["agent"] == "news"
    assert body["cases_considered"] == 1
    case = body["cases"][0]
    assert case["side"] == "agent_enabled_trade"
    assert case["agreement_with_analysis"] == "agree"
    assert case["trading_date"] == "2026-08-11"
    assert "summary" in body
    olb = body["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"agent_enabled_trade": 1}
    assert olb["opinion_weighted_totals"] == {"agent_enabled_trade": 1.0}
    assert olb["by_day"]["2026-08-11"]["distinct_opinions"] == 1


def test_threshold_crossing_deep_dive_endpoint_rejects_unknown_agent(client):
    r = client.get(
        "/candidates/history/threshold-crossing-deep-dive",
        params={"symbol": "MNQ1!", "timeframe": "5m", "agent": "timing"},
    )
    assert r.status_code == 400


def test_threshold_crossing_deep_dive_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/threshold-crossing-deep-dive",
        params={"symbol": "NOSUCH", "timeframe": "5m", "agent": "macro"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cases_considered"] == 0
    assert body["cases"] == []


# ---------------------------------------------------------------------------
# Tier 3.27: News urgent-vs-directional decomposition
# ---------------------------------------------------------------------------

def test_news_urgent_decomposition_endpoint_returns_shape(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {
        "event_id": "evt-nud-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-11",
    }
    # Same urgent_dampen_alone scenario verified in test_coordinator_diagnostics.py:
    # analysis=bullish30, news=bullish80(urgent), macro=bullish20 -> no_trade;
    # removing News entirely crosses to enter_long, and it's the urgent
    # dampen alone (not the directional contribution) that's responsible.
    decision = {
        "decision": "no_trade",
        "score": 21.88,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 30, "timestamp": anchor, "flags": []},
            "news": {"direction": "bullish", "confidence": 80, "timestamp": anchor, "flags": ["urgent"]},
            "macro": {"direction": "bullish", "confidence": 20, "timestamp": anchor, "flags": []},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {},
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-nud-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/news-urgent-decomposition",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert "prevalence" in body
    assert "decomposition" in body
    assert body["prevalence"]["candidate_level"]["news_present_candidates"] == 1
    assert body["decomposition"]["cases_considered"] == 1
    assert body["decomposition"]["cases"][0]["attribution"] == "urgent_dampen_alone"
    assert body["decomposition"]["cases"][0]["trading_date"] == "2026-08-11"
    olb = body["decomposition"]["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"urgent_dampen_alone": 1}
    assert olb["by_day"]["2026-08-11"]["distinct_opinions"] == 1


def test_news_urgent_decomposition_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/news-urgent-decomposition",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["prevalence"]["candidate_level"]["news_present_candidates"] == 0
    assert body["decomposition"]["cases_considered"] == 0


# ---------------------------------------------------------------------------
# Tier 3.28: News urgent vs. deterministic economic-calendar blackout
# ---------------------------------------------------------------------------

def test_news_urgent_vs_calendar_blackout_endpoint_returns_shape(client):
    import app.storage as storage

    # 2026-08-12T13:00:00Z is 30 minutes after the real 2026-08-12 CPI
    # release (2026-08-12T12:30:00Z, app/economic_calendar.py) -- inside
    # the default 2-hour blackout window. News is also flagged urgent,
    # so this candidate is a clean "both_flagged" case.
    anchor = "2026-08-12T13:00:00Z"
    bar = {
        "event_id": "evt-nucb-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-12",
    }
    decision = {
        "decision": "no_trade",
        "score": 5.0,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 10, "timestamp": anchor, "flags": []},
            "news": {"direction": "bullish", "confidence": 10, "timestamp": anchor, "flags": ["urgent"]},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {},
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-nucb-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/news-urgent-vs-calendar-blackout",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["news_present_candidates"] == 1
    case = body["cases"][0]
    assert case["quadrant"] == "both_flagged"
    assert case["nearest_event"]["date"] == "2026-08-12"
    assert case["trading_date"] == "2026-08-12"
    assert body["cross_tab"] == {"both_flagged": 1}
    assert body["calendar_coverage"]["event_count"] == 1
    olb = body["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"both_flagged": 1}
    assert olb["by_day"]["2026-08-12"]["distinct_opinions"] == 1


def test_news_urgent_vs_calendar_blackout_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/news-urgent-vs-calendar-blackout",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["news_present_candidates"] == 0
    assert body["cases"] == []
    assert body["cross_tab"] == {}
    assert body["agreement_rate"] is None


# ---------------------------------------------------------------------------
# Tier 3.31: risk-filter veto attribution
# ---------------------------------------------------------------------------

def test_risk_filter_veto_attribution_endpoint_returns_shape(client):
    import app.storage as storage

    anchor = "2026-08-16T14:00:00Z"
    bar = {
        "event_id": "evt-rfva-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-16",
    }
    decision = {
        "decision": "enter_long",
        "direction": "bullish",
        "score": 90.0,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 90, "timestamp": anchor, "flags": []},
            "news": {"direction": "bullish", "confidence": 90, "timestamp": anchor, "flags": []},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {},
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-rfva-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/risk-filter-veto-attribution",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["candidates_considered"] == 1
    assert body["analysis_directional_candidates"] == 1
    assert body["summary"] == {"coordinator_agrees": 1}
    assert body["flag_prevalence"] == {"news_urgent_total": 0, "macro_risk_off_total": 0, "both_flags_overlap": 0}
    assert body["score_below_threshold_breakdown"] == {}
    case = body["cases"][0]
    assert case["attribution"] == "coordinator_agrees"
    assert case["trading_date"] == "2026-08-16"
    olb = body["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"coordinator_agrees": 1}


def test_risk_filter_veto_attribution_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/risk-filter-veto-attribution",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_considered"] == 0
    assert body["analysis_not_directional_excluded"] == 0
    assert body["summary"] == {}
    assert body["flag_prevalence"] == {"news_urgent_total": 0, "macro_risk_off_total": 0, "both_flags_overlap": 0}
    assert body["score_below_threshold_breakdown"] == {}
    assert body["cases"] == []


def test_veto_decision_transitions_endpoint_returns_shape(client):
    import app.storage as storage

    anchor = "2026-08-16T14:00:00Z"
    bar = {
        "event_id": "evt-vdt-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-16",
    }
    decision = {
        "decision": "enter_long",
        "direction": "bullish",
        "score": 90.0,
        "threshold": 25.0,
        "opinions_used": {
            "analysis": {"direction": "bullish", "confidence": 90, "timestamp": anchor, "flags": []},
            "news": {"direction": "bullish", "confidence": 90, "timestamp": anchor, "flags": []},
        },
        "missing_agents": [],
        "stale_agents": [],
        "contributions": {},
        "conflict_flags": [],
        "timestamp": anchor,
    }
    storage.save_candidate(candidate_id="cand-vdt-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/veto-decision-transitions",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["candidates_considered"] == 1
    assert body["analysis_directional_candidates"] == 1
    assert body["transition_summary"] == {"coordinator_trade_veto_survives": 1}
    assert body["flag_basis_by_transition"] == {"coordinator_trade_veto_survives": {"neither": 1}}
    assert body["direction_flag_basis_by_transition"] == {
        "coordinator_trade_veto_survives": {"bullish": {"neither": 1}},
    }
    assert body["coordinator_skip_reason_by_transition"] == {}
    case = body["cases"][0]
    assert case["transition"] == "coordinator_trade_veto_survives"
    assert case["trading_date"] == "2026-08-16"
    assert case["coordinator_direction"] == "bullish"
    assert case["news_opinion_timestamp"] == anchor
    assert case["macro_opinion_timestamp"] is None
    assert case["news_direction"] == "bullish"
    assert case["macro_direction"] is None  # Macro didn't run on this candidate
    olb = body["opinion_level_day_blocked"]
    assert olb["candidate_level_totals"] == {"coordinator_trade_veto_survives": 1}
    news_olb = body["news_opinion_level_day_blocked"]
    assert news_olb["candidate_level_totals"] == {"coordinator_trade_veto_survives": 1}
    macro_olb = body["macro_opinion_level_day_blocked"]
    assert macro_olb["uncategorized_count"] == 1  # Macro didn't run on this candidate
    # Tier 3.36: no risk_off/urgent flags present anywhere in this fixture,
    # so all three new aggregates stay empty rather than fabricating buckets.
    assert body["macro_risk_off_direction_crosstab"] == {}
    assert body["macro_opinion_diversity"] == {}
    assert body["news_opinion_diversity"] == {}


def test_veto_decision_transitions_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/veto-decision-transitions",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_considered"] == 0
    assert body["analysis_not_directional_excluded"] == 0
    assert body["transition_summary"] == {}
    assert body["flag_basis_by_transition"] == {}
    assert body["direction_flag_basis_by_transition"] == {}
    assert body["coordinator_skip_reason_by_transition"] == {}
    assert body["macro_risk_off_direction_crosstab"] == {}
    assert body["macro_opinion_diversity"] == {}
    assert body["news_opinion_diversity"] == {}
    assert body["cases"] == []


# ---------------------------------------------------------------------------
# Tier 3.18: day/session reporting
# ---------------------------------------------------------------------------

def test_day_session_report_endpoint_returns_report_shape(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {
        "event_id": "evt-ds-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2026-08-11", "session_name": "RTH",
    }
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
        "timing_context": {"session_label": "new_york"},
    }
    storage.save_candidate(candidate_id="cand-ds-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/day-session-report",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["timeframe"] == "5m"
    assert body["candidates_considered"] == 1
    assert body["distinct_trading_days"] == 1
    assert body["by_session_name"] == {"RTH": 1}
    assert body["by_timing_session_label"] == {"new_york": 1}


def test_day_session_report_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/day-session-report",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_considered"] == 0
    assert body["distinct_trading_days"] == 0


# ---------------------------------------------------------------------------
# Tier 3.19: trading-date integrity (fourth external review, 2026-08-18)
# ---------------------------------------------------------------------------

def test_trading_date_integrity_endpoint_returns_report_shape(client):
    import app.storage as storage

    anchor = "2026-08-11T14:00:00Z"
    bar = {
        "event_id": "evt-tdi-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor,
        "trading_date": "2099-01-01",  # deliberately mismatched vs. the anchor timestamp
    }
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
    }
    storage.save_candidate(candidate_id="cand-tdi-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.get(
        "/candidates/history/trading-date-integrity",
        params={"symbol": "MNQ1!", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "MNQ1!"
    assert body["timeframe"] == "5m"
    assert body["candidates_considered"] == 1
    assert body["mismatch_count"] == 1
    assert body["mismatch_examples"][0]["candidate_id"] == "cand-tdi-1"
    assert body["mismatch_examples"][0]["event_id"] == "evt-tdi-1"
    assert body["mismatch_examples"][0]["payload_trading_date"] == "2099-01-01"
    assert body["mismatch_examples"][0]["computed_trading_date"] == "2026-08-11"


def test_trading_date_integrity_endpoint_empty_history(client):
    r = client.get(
        "/candidates/history/trading-date-integrity",
        params={"symbol": "NOSUCH", "timeframe": "5m"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["candidates_considered"] == 0
    assert body["mismatch_count"] == 0


# ---------------------------------------------------------------------------
# Tier 3.20: experiment registry (fourth external review, 2026-08-18)
# ---------------------------------------------------------------------------

_WIN_RATE_TARGET_PARAMS = {"primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5}


def test_register_experiment_endpoint_requires_secret(client):
    r = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 1,
        },
    )
    assert r.status_code == 401


def test_register_experiment_endpoint_returns_locked_config(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    r = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "Coordinator beats a coin flip",
            "primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5,
            "secondary_metrics": ["profit_factor"], "min_distinct_trading_days": 2,
        },
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["hypothesis"] == "Coordinator beats a coin flip"
    assert body["target_metrics"] == {
        "primary_metric": "win_rate", "comparator": ">=", "success_threshold": 0.5,
        "secondary_metrics": ["profit_factor"],
    }
    assert body["stopping_rule"] == {"min_distinct_trading_days": 2}
    assert "coordinator_threshold" in body["locked_config"]
    assert "weights" in body["locked_config"]
    assert "backtest_geometry" in body["locked_config"]
    assert "registered_watermark_rowid" in body


def test_register_experiment_endpoint_rejects_missing_stopping_rule(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    r = client.post(
        "/experiments",
        params={"symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h", **_WIN_RATE_TARGET_PARAMS},
        headers=headers,
    )
    assert r.status_code == 400


def test_experiments_list_and_detail_endpoints(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    registered = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 5,
        },
        headers=headers,
    ).json()
    experiment_id = registered["experiment_id"]

    listed = client.get("/experiments", params={"symbol": "MNQ1!", "timeframe": "5m"})
    assert listed.status_code == 200
    assert [e["experiment_id"] for e in listed.json()["experiments"]] == [experiment_id]

    detail = client.get(f"/experiments/{experiment_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["experiment_id"] == experiment_id
    assert "stopping_rule_status" in body
    assert body["stopping_rule_status"]["stopping_rule_met"] is False


def test_experiment_detail_endpoint_404_for_unknown_id(client):
    r = client.get("/experiments/does-not-exist")
    assert r.status_code == 404


def test_resolve_experiment_endpoint_requires_secret(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    registered = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 1,
        },
        headers=headers,
    ).json()

    r = client.post(f"/experiments/{registered['experiment_id']}/resolve")
    assert r.status_code == 401


def test_resolve_experiment_endpoint_409_when_stopping_rule_not_met(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    registered = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 5,
        },
        headers=headers,
    ).json()

    r = client.post(f"/experiments/{registered['experiment_id']}/resolve", headers=headers)
    assert r.status_code == 409


def test_resolve_experiment_endpoint_404_for_unknown_id(client):
    headers = {"X-Webhook-Secret": "test-secret"}
    r = client.post("/experiments/does-not-exist/resolve", headers=headers)
    assert r.status_code == 404


def test_experiment_endpoints_500_past_the_safety_ceiling(client, monkeypatch):
    import app.experiments as experiments
    import app.storage as storage

    monkeypatch.setattr(experiments, "EXPERIMENT_MAX_PROSPECTIVE_CANDIDATES", 1)
    headers = {"X-Webhook-Secret": "test-secret"}
    registered = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 1,
        },
        headers=headers,
    ).json()
    for i in range(2):
        anchor = f"2026-08-{11 + i:02d}T14:00:00Z"
        bar = {"event_id": f"evt-ceiling-{i}", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor}
        decision = {"decision": "no_trade", "timestamp": anchor, "opinions_used": {}}
        storage.save_candidate(candidate_id=f"cand-ceiling-{i}", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    detail = client.get(f"/experiments/{registered['experiment_id']}")
    assert detail.status_code == 500

    resolve = client.post(f"/experiments/{registered['experiment_id']}/resolve", headers=headers)
    assert resolve.status_code == 500


def test_resolve_experiment_endpoint_succeeds_once_stopping_rule_met(client):
    import app.storage as storage

    headers = {"X-Webhook-Secret": "test-secret"}
    registered = client.post(
        "/experiments",
        params={
            "symbol": "MNQ1!", "timeframe": "5m", "hypothesis": "h",
            **_WIN_RATE_TARGET_PARAMS, "min_distinct_trading_days": 1,
        },
        headers=headers,
    ).json()

    # A candidate inserted AFTER registration -- Tier 3.23's boundary is
    # insertion order (rowid) relative to registration, not a timestamp.
    anchor = "2026-08-11T14:00:00Z"
    bar = {"event_id": "evt-exp-1", "symbol": "MNQ1!", "timeframe": "5m", "timestamp": anchor, "trading_date": "2026-08-11"}
    decision = {
        "decision": "enter_long", "timestamp": anchor,
        "opinions_used": {"analysis": {"direction": "bullish", "timestamp": anchor}},
    }
    storage.save_candidate(candidate_id="cand-exp-1", symbol="MNQ1!", timeframe="5m", bar=bar, decision=decision)

    r = client.post(f"/experiments/{registered['experiment_id']}/resolve", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "resolved"
    assert body["resolution"]["resolved_from_candidates_considered"] == 1
    assert "target_metrics_result" in body["resolution"]

    # idempotent: resolving again returns the same resolution, 200 not 409
    r2 = client.post(f"/experiments/{registered['experiment_id']}/resolve", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["resolution"] == body["resolution"]
